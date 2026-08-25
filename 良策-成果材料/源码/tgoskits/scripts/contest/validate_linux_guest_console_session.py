#!/usr/bin/env python3
"""Independent review of one linux-smp2-v1 framed-console gate session.

The same :func:`validate_console_session` is invoked by the live-capture
harness (as ``linux-console-result.json``) and by this CLI (as the review
copy), so the runner and reviewer byte-compare identical artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from host_vm_carveout_io import publish_new_file, read_regular_bytes


PROFILE = "linux-smp2-v1"
VM_ID = 1
CPU_NUM = 2
PHYS_CPU_IDS = [0, 1]
CONSOLE_GPA = 0x0900_0000
CONSOLE_SIZE = 0x1000
ROOT_SLOT_PATH = "/virtio_mmio@a000000"
NET_SLOT_PATH = "/virtio_mmio@a000200"
EXPECTED_EXCLUDED = ("/intc@8000000/its@8080000", "/pl011@9000000", "/virtio_mmio@a000400")
MARKERS = (
    "AXVISOR_LINUX_INIT_ENTER",
    "AXVISOR_LINUX_DEV_CONSOLE_READY",
    "AXVISOR_DUAL_GUEST_LINUX_READY",
)
FAIL_MARKER = "AXVISOR_LINUX_CONSOLE_FAIL"
MAX_CONSOLE_LOG_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024


class ConsoleSessionError(ValueError):
    """The console-gate session evidence does not satisfy its contract."""


def _sha256_file(path: Path) -> str:
    try:
        data = read_regular_bytes(
            path,
            label=f"console evidence {path.name}",
            error_type=ConsoleSessionError,
        )
    except ConsoleSessionError:
        raise
    except OSError as error:
        raise ConsoleSessionError(f"console evidence {path.name} is unreadable: {error}") from error
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        data = read_regular_bytes(path, label=label, error_type=ConsoleSessionError)
    except ConsoleSessionError:
        raise
    except OSError as error:
        raise ConsoleSessionError(f"{label} is missing: {error}") from error
    if len(data) > MAX_JSON_BYTES:
        raise ConsoleSessionError(f"{label} exceeds the JSON byte limit")
    try:
        value = json.loads(data)
    except json.JSONDecodeError as error:
        raise ConsoleSessionError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ConsoleSessionError(f"{label} must be a JSON object")
    return value


def _require(value: object, *, label: str, expected: object) -> None:
    if value != expected:
        raise ConsoleSessionError(f"{label} must be {expected!r}, got {value!r}")


def _validate_capture_status(path: Path | dict) -> dict[str, Any]:
    if isinstance(path, dict):
        status = path
    else:
        status = _read_json(path, label="capture status")
    _require(status.get("status"), label="capture status.status", expected="linux_guest_console_completed")
    _require(status.get("success"), label="capture status.success", expected=True)
    state = status.get("state")
    if not isinstance(state, dict):
        raise ConsoleSessionError("capture status.state is missing")
    console = state.get("guestConsole")
    if not isinstance(console, dict) or not isinstance(console.get("bootId"), str):
        raise ConsoleSessionError("capture status.state.guestConsole is missing")
    demux = state.get("guestConsoleDemux")
    if not isinstance(demux, dict) or demux.get("guestVmIds") != [VM_ID]:
        raise ConsoleSessionError("capture status.state.guestConsoleDemux is incomplete")
    return {"bootId": console["bootId"], "demux": demux, "console": console}


def _validate_console_evidence(manifest_path: Path, log_path: Path, *, boot_id: str) -> dict[str, Any]:
    manifest = _read_json(manifest_path, label="console manifest")
    _require(manifest.get("status"), label="console manifest.status", expected="console_frames_demuxed")
    guests = manifest.get("guests")
    if not isinstance(guests, list) or len(guests) != 1:
        raise ConsoleSessionError("console manifest must contain exactly one guest")
    guest = guests[0]
    if not isinstance(guest, dict):
        raise ConsoleSessionError("console manifest guest entry is malformed")
    _require(guest.get("vmId"), label="console guest vmId", expected=VM_ID)
    _require(guest.get("name"), label="console guest name", expected="linux")
    _require(guest.get("path"), label="console guest path", expected="guest-vm-1.console.log")
    _require(guest.get("droppedBytes"), label="console guest droppedBytes", expected=0)
    _require(guest.get("dmaEnableAttempts"), label="console guest dmaEnableAttempts", expected=0)
    if not isinstance(guest.get("records"), int) or guest["records"] <= 0:
        raise ConsoleSessionError("console guest must contain at least one frame record")
    if _sha256_file(log_path) != guest.get("sha256"):
        raise ConsoleSessionError("console log sha256 does not match the console manifest")
    try:
        data = read_regular_bytes(
            log_path,
            label="console log",
            error_type=ConsoleSessionError,
        )
    except ConsoleSessionError:
        raise
    except OSError as error:
        raise ConsoleSessionError(f"console log is missing: {error}") from error
    if len(data) > MAX_CONSOLE_LOG_BYTES:
        raise ConsoleSessionError("console log exceeds the evidence byte limit")
    if FAIL_MARKER in data.decode("utf-8", errors="strict"):
        raise ConsoleSessionError("console log contains AXVISOR_LINUX_CONSOLE_FAIL")
    boot_token = f"boot_id={boot_id}"
    text = data.decode("utf-8", errors="strict")
    positions = []
    for marker in MARKERS:
        count = text.count(marker)
        if count != 1:
            raise ConsoleSessionError(f"console marker {marker} must appear exactly once, got {count}")
        index = text.find(marker)
        if boot_token not in text[index : index + 512]:
            raise ConsoleSessionError(f"console marker {marker} does not bind boot_id={boot_id}")
        positions.append(index)
    if positions != sorted(positions):
        raise ConsoleSessionError("console markers are not in INIT_ENTER -> DEV_CONSOLE_READY -> DUAL_GUEST_LINUX_READY order")
    return {
        "guest": guest,
        "logSha256": _sha256_file(log_path),
        "logBytes": len(data),
        "markers": positions,
    }


def _validate_capture_chain(path: Path) -> dict[str, Any]:
    chain = _read_json(path, label="capture chain")
    guests = chain.get("guests")
    if isinstance(guests, list):
        ids = [item.get("vmId") for item in guests if isinstance(item, dict)]
        if VM_ID not in ids:
            raise ConsoleSessionError("capture chain does not contain the expected VM-1 guest")
    return {"chainStatus": chain.get("status")}


def _validate_guest_dtb(
    dtb_path: Path, dts_path: Path | None, semantic_path: Path | None
) -> dict[str, Any]:
    try:
        dtb_data = read_regular_bytes(
            dtb_path,
            label="guest DTB",
            error_type=ConsoleSessionError,
        )
        size = len(dtb_data)
    except ConsoleSessionError:
        raise
    except OSError as error:
        raise ConsoleSessionError(f"guest DTB is missing: {error}") from error
    if size <= 0:
        raise ConsoleSessionError("guest DTB is empty")
    result: dict[str, Any] = {"dtbSha256": hashlib.sha256(dtb_data).hexdigest()}
    if semantic_path is not None and semantic_path.exists():
        semantic = _read_json(semantic_path, label="guest semantic")
        result["semanticStatus"] = semantic.get("status")
    else:
        result["semanticStatus"] = None
    if dts_path is not None and dts_path.exists():
        result["dtsSha256"] = _sha256_file(dts_path)
    else:
        result["dtsSha256"] = None
    return result


def _validate_vm_config(path: Path) -> dict[str, Any]:
    try:
        config = tomllib.loads(
            read_regular_bytes(
                path,
                label="resolved VM config",
                error_type=ConsoleSessionError,
            ).decode("utf-8")
        )
    except ConsoleSessionError:
        raise
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConsoleSessionError(f"cannot parse resolved VM config: {error}") from error
    base = config.get("base")
    kernel = config.get("kernel")
    devices = config.get("devices")
    if not isinstance(base, dict) or base.get("id") != VM_ID or base.get("cpu_num") != CPU_NUM or base.get("phys_cpu_ids") != PHYS_CPU_IDS:
        raise ConsoleSessionError("resolved VM config must be Linux VM1 with 2 vCPUs on pCPU [0,1]")
    if not isinstance(kernel, dict) or kernel.get("image_location") != "memory":
        raise ConsoleSessionError("resolved VM config must load the kernel from memory")
    if not isinstance(kernel.get("memory_regions"), list) or not kernel["memory_regions"]:
        raise ConsoleSessionError("resolved VM config must declare the identity RAM region")
    if not isinstance(devices, dict):
        raise ConsoleSessionError("resolved VM config devices section is missing")
    passthrough = [tuple(item) for item in devices.get("passthrough_devices", [])]
    if (NET_SLOT_PATH,) in passthrough:
        raise ConsoleSessionError("resolved VM config keeps the outer network slot")
    if (ROOT_SLOT_PATH,) not in passthrough:
        raise ConsoleSessionError("resolved VM config drops the root block slot")
    excluded = [tuple(item) for item in devices.get("excluded_devices", [])]
    for expected in EXPECTED_EXCLUDED:
        if (expected,) not in excluded:
            raise ConsoleSessionError(f"resolved VM config must exclude {expected}")
    emu = devices.get("emu_devices", [])
    consoles = [item for item in emu if isinstance(item, list) and len(item) >= 2 and item[1] == CONSOLE_GPA]
    if len(consoles) != 1:
        raise ConsoleSessionError("resolved VM config must declare exactly one VM-local console")
    return {"vmId": VM_ID, "consoleGpa": CONSOLE_GPA, "consoleSize": CONSOLE_SIZE}


def _validate_rootfs_plan(path: Path, *, boot_id: str) -> dict[str, Any]:
    plan = _read_json(path, label="rootfs plan")
    _require(plan.get("status"), label="rootfs plan.status", expected="dual_guest_linux_rootfs_prepared")
    if plan.get("bootId") != boot_id:
        raise ConsoleSessionError("rootfs plan boot-id does not match the capture session")
    init = plan.get("init")
    if not isinstance(init, dict) or init.get("mode") != "0755":
        raise ConsoleSessionError("rootfs plan init contract is missing")
    return {"bootId": boot_id}


def validate_console_session(
    *,
    capture_status_path: Path | dict,
    capture_chain_path: Path,
    guest_dtb_path: Path,
    guest_dts_path: Path | None,
    guest_semantic_path: Path | None,
    vm_config_path: Path,
    rootfs_plan_path: Path,
    console_manifest_path: Path,
    console_log_path: Path,
) -> dict[str, Any]:
    capture = _validate_capture_status(capture_status_path)
    console = _validate_console_evidence(
        console_manifest_path, console_log_path, boot_id=capture["bootId"]
    )
    chain = _validate_capture_chain(capture_chain_path)
    dtb = _validate_guest_dtb(guest_dtb_path, guest_dts_path, guest_semantic_path)
    vm = _validate_vm_config(vm_config_path)
    plan = _validate_rootfs_plan(rootfs_plan_path, boot_id=capture["bootId"])
    return {
        "schemaVersion": 1,
        "artifactStatus": "reviewed",
        "status": "linux_guest_console_observed",
        "proofScope": "one-run-framed-vm1-console-gate-session-review",
        "doesNotProve": [
            "a regular Linux tty or boot-console handoff beyond the observed markers",
            "a second guest booted",
            "DMA isolation",
            "dual-Guest execution",
            "Linux and Zephyr IP connectivity",
        ],
        "profile": PROFILE,
        "capture": capture,
        "console": console,
        "captureChain": chain,
        "guestDtb": dtb,
        "resolvedVmConfig": vm,
        "rootfsPlan": plan,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Independently review one linux-smp2-v1 framed-console gate session"
    )
    parser.add_argument("--capture-status", required=True, type=Path)
    parser.add_argument("--capture-chain", required=True, type=Path)
    parser.add_argument("--guest-dtb", required=True, type=Path)
    parser.add_argument("--guest-dts", required=True, type=Path)
    parser.add_argument("--guest-semantic", required=True, type=Path)
    parser.add_argument("--vm-config", required=True, type=Path)
    parser.add_argument("--rootfs-plan", required=True, type=Path)
    parser.add_argument("--console-manifest", required=True, type=Path)
    parser.add_argument("--console-log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = validate_console_session(
            capture_status_path=args.capture_status,
            capture_chain_path=args.capture_chain,
            guest_dtb_path=args.guest_dtb,
            guest_dts_path=args.guest_dts,
            guest_semantic_path=args.guest_semantic,
            vm_config_path=args.vm_config,
            rootfs_plan_path=args.rootfs_plan,
            console_manifest_path=args.console_manifest,
            console_log_path=args.console_log,
        )
    except (OSError, ConsoleSessionError, UnicodeError, json.JSONDecodeError) as error:
        print(f"Linux console-gate session validation failed: {error}", file=sys.stderr)
        return 1
    publish_new_file(
        args.output,
        (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        error_type=ConsoleSessionError,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
