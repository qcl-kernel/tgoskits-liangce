#!/usr/bin/env python3
"""Prepare one immutable, non-executing virtio-blk DMA-effect request.

The resulting JSON is an input contract for a future runner.  It records no
DMA result and contains only read-only QMP ``pmemsave`` capture templates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from host_vm_carveout_io import publish_new_file, read_regular_bytes


MAX_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_QMP_ADDRESS = (1 << 63) - 1
NONCE = re.compile(r"[0-9a-f]{32}\Z")
DEVICE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
BUS = re.compile(r"virtio-mmio-bus\.[0-9]+\Z")
GUEST_DEVICE = re.compile(r"/dev/[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
CONTROL_SOCKET = re.compile(r"/tmp/axdma-[0-9a-f]{32}/control\.sock\Z")
GIT_HEAD = re.compile(r"[0-9a-f]{40,64}\Z")


class PreparedVirtioDmaRequestError(ValueError):
    """The supplied immutable inputs cannot form a safe prepared request."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _uint(value: str, *, label: str, positive: bool = False) -> int:
    try:
        parsed = int(value, 0)
    except ValueError as error:
        raise PreparedVirtioDmaRequestError(
            f"{label} must be decimal or 0x hexadecimal"
        ) from error
    if parsed < 0 or (positive and parsed == 0):
        raise PreparedVirtioDmaRequestError(
            f"{label} must be {'non-zero ' if positive else ''}unsigned"
        )
    return parsed


def _range(*, hpa: int, size: int, label: str) -> dict[str, int]:
    if size > MAX_CAPTURE_BYTES:
        raise PreparedVirtioDmaRequestError(
            f"{label} size exceeds {MAX_CAPTURE_BYTES} bytes"
        )
    if hpa > MAX_QMP_ADDRESS or size - 1 > MAX_QMP_ADDRESS - hpa:
        raise PreparedVirtioDmaRequestError(
            f"{label} exceeds QMP int64 host-physical range"
        )
    return {"hpa": hpa, "size": size}


def _overlap(first: dict[str, int], second: dict[str, int]) -> bool:
    return (
        first["hpa"] < second["hpa"] + second["size"]
        and second["hpa"] < first["hpa"] + first["size"]
    )


def _source(path: Path, data: bytes) -> dict[str, object]:
    return {"path": path.name, "size": len(data), "sha256": _sha(data)}


def _git(repository: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *args],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="strict",
        )
    except OSError as error:
        raise PreparedVirtioDmaRequestError(f"could not invoke git: {error}") from error
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "unknown git failure"
        )
        raise PreparedVirtioDmaRequestError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _repository(path: Path) -> dict[str, object]:
    if not path.is_dir():
        raise PreparedVirtioDmaRequestError("--repository must be a directory")
    root = _git(path, "rev-parse", "--show-toplevel").strip()
    requested_path = path.resolve()
    try:
        root_path = Path(root).resolve()
    except OSError as error:
        raise PreparedVirtioDmaRequestError(
            f"could not resolve Git worktree root: {error}"
        ) from error
    if root_path != requested_path:
        raise PreparedVirtioDmaRequestError(
            "--repository must be the Git worktree root"
        )
    head = _git(path, "rev-parse", "--verify", "HEAD").strip()
    if GIT_HEAD.fullmatch(head) is None:
        raise PreparedVirtioDmaRequestError(
            "Git HEAD is not a full lower-case object id"
        )
    dirty = bool(_git(path, "status", "--porcelain=v1", "--untracked-files=all"))
    return {
        "worktreeRoot": str(requested_path),
        "gitHead": head,
        "worktreeState": "dirty" if dirty else "clean",
    }


def _parse_toml(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = tomllib.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PreparedVirtioDmaRequestError(
            f"{label} is not strict UTF-8 TOML: {error}"
        ) from error
    if not isinstance(value, dict):
        raise PreparedVirtioDmaRequestError(f"{label} must be a TOML table")
    return value


def _device(
    *, device_id: str, bus: str, guest_path: str, drive_id: str = "dma-probe-disk"
) -> dict[str, str]:
    if DEVICE_ID.fullmatch(device_id) is None:
        raise PreparedVirtioDmaRequestError("--device-id is malformed")
    if BUS.fullmatch(bus) is None:
        raise PreparedVirtioDmaRequestError("--bus must be virtio-mmio-bus.N")
    if GUEST_DEVICE.fullmatch(guest_path) is None:
        raise PreparedVirtioDmaRequestError(
            "--guest-device must be one safe absolute /dev path"
        )
    if DEVICE_ID.fullmatch(drive_id) is None:
        raise PreparedVirtioDmaRequestError("--drive-id is malformed")
    return {
        "kind": "virtio-blk-device",
        "id": device_id,
        "bus": bus,
        "driveId": drive_id,
        "guestPath": guest_path,
    }


def _bind_qemu_device(
    qemu: dict[str, Any], *, device: dict[str, str], expected_payload: Path
) -> None:
    args = qemu.get("args")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise PreparedVirtioDmaRequestError(
            "QEMU config must contain a string args list"
        )
    expected = {
        "virtio-blk-device",
        f"id={device['id']}",
        f"drive={device['driveId']}",
        f"bus={device['bus']}",
    }
    matches = [
        item
        for item in args
        if set(item.split(",")) >= expected
        and item.split(",")[0] == "virtio-blk-device"
    ]
    if len(matches) != 1:
        raise PreparedVirtioDmaRequestError(
            "QEMU config must contain exactly one matching virtio-blk-device id/drive/bus argument"
        )
    expected_drive = {
        f"id={device['driveId']}",
        "if=none",
        "format=raw",
        "readonly=on",
        f"file={expected_payload}",
    }
    drive_matches = [
        item
        for item in args
        if item.split(",")[0].startswith("id=")
        and set(item.split(",")) >= expected_drive
    ]
    if len(drive_matches) != 1:
        raise PreparedVirtioDmaRequestError(
            "QEMU config must contain exactly one readonly expected-payload drive binding"
        )


def _control(*, nonce: str, socket_path: str) -> dict[str, str]:
    if CONTROL_SOCKET.fullmatch(socket_path) is None or nonce not in socket_path:
        raise PreparedVirtioDmaRequestError(
            "control socket must be /tmp/axdma-<nonce>/control.sock"
        )
    return {
        "kind": "virtio-console",
        "chardevId": "dma-go-chardev",
        "serialDeviceId": "dma-go-serial",
        "serialBus": "virtio-mmio-bus.2",
        "portId": "dma-go-port",
        "portName": "dma-go",
        "guestPath": "/dev/hvc0",
        "socketPath": socket_path,
    }


def _bind_control(qemu: dict[str, Any], *, control: dict[str, str]) -> None:
    args = qemu.get("args")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise PreparedVirtioDmaRequestError(
            "QEMU config must contain a string args list"
        )
    required = (
        f"socket,id={control['chardevId']},path={control['socketPath']},server=on,wait=off",
        f"virtio-serial-device,id={control['serialDeviceId']},bus={control['serialBus']}",
        f"virtconsole,id={control['portId']},chardev={control['chardevId']},name={control['portName']}",
    )
    if any(sum(item == expected for item in args) != 1 for expected in required):
        raise PreparedVirtioDmaRequestError(
            "QEMU config must contain one exact virtio-console control topology"
        )


def _bind_build_config(build: dict[str, Any]) -> None:
    features = build.get("features")
    if not isinstance(features, list) or not all(
        isinstance(item, str) for item in features
    ):
        raise PreparedVirtioDmaRequestError(
            "build config must contain a string features list"
        )
    if "ax-driver/virtio-blk" not in features:
        raise PreparedVirtioDmaRequestError(
            "build config does not enable ax-driver/virtio-blk"
        )


def _bind_vm_config(vm: dict[str, Any]) -> tuple[int, list[dict[str, int]]]:
    base = vm.get("base")
    if not isinstance(base, dict):
        raise PreparedVirtioDmaRequestError("VM config has no [base] table")
    vm_id = base.get("id")
    if isinstance(vm_id, bool) or not isinstance(vm_id, int) or vm_id <= 0:
        raise PreparedVirtioDmaRequestError(
            "VM config [base].id must be a positive integer"
        )
    kernel = vm.get("kernel")
    if not isinstance(kernel, dict):
        raise PreparedVirtioDmaRequestError("VM config has no [kernel] table")
    regions = kernel.get("memory_regions")
    if not isinstance(regions, list) or not regions:
        raise PreparedVirtioDmaRequestError(
            "VM config must contain non-empty memory_regions"
        )
    parsed: list[dict[str, int]] = []
    for index, item in enumerate(regions):
        if not isinstance(item, list) or len(item) != 4:
            raise PreparedVirtioDmaRequestError(
                f"VM memory_regions[{index}] must contain base/size/flags/map type"
            )
        if any(isinstance(field, bool) or not isinstance(field, int) for field in item):
            raise PreparedVirtioDmaRequestError(
                f"VM memory_regions[{index}] must contain integers"
            )
        base, size, flags, map_type = item
        if base < 0 or size <= 0 or flags < 0 or map_type < 0:
            raise PreparedVirtioDmaRequestError(
                f"VM memory_regions[{index}] has an invalid unsigned value"
            )
        if base > MAX_QMP_ADDRESS or size - 1 > MAX_QMP_ADDRESS - base:
            raise PreparedVirtioDmaRequestError(
                f"VM memory_regions[{index}] exceeds QMP int64 range"
            )
        parsed.append(
            {
                "gpa": base,
                "hpa": base,
                "size": size,
                "flags": flags,
                "mapType": map_type,
            }
        )
    return vm_id, parsed


def _bound_map_reserved_region(
    *, payload: dict[str, int], regions: list[dict[str, int]]
) -> dict[str, object]:
    if payload["hpa"] % 512 or payload["size"] % 512:
        raise PreparedVirtioDmaRequestError(
            "payload GPA/HPA and size must be 512-byte aligned"
        )
    candidates = [
        region
        for region in regions
        if region["mapType"] == 2
        and region["gpa"] == region["hpa"]
        and region["gpa"] <= payload["hpa"]
        and payload["hpa"] + payload["size"] <= region["gpa"] + region["size"]
    ]
    if len(candidates) != 1:
        raise PreparedVirtioDmaRequestError(
            "payload must be wholly within exactly one identity MapReserved memory region"
        )
    region = candidates[0]
    return {
        "gpa": region["gpa"],
        "hpa": region["hpa"],
        "size": region["size"],
        "flags": region["flags"],
        "mapType": 2,
        "identity": True,
    }


def _template(*, payload: dict[str, int], guard: dict[str, int]) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for phase, target, value, filename in (
        ("before", "guard", guard, "before-guard.bin"),
        ("before", "payload", payload, "before-payload.bin"),
        ("after", "guard", guard, "after-guard.bin"),
        ("after", "payload", payload, "after-payload.bin"),
    ):
        records.append(
            {
                "phase": phase,
                "target": target,
                "operation": "pmemsave",
                "addressSpace": "host-physical",
                "hpa": value["hpa"],
                "size": value["size"],
                "outputFileName": filename,
            }
        )
    return {
        "allowedOperations": ["query-name", "query-status", "stop", "cont", "pmemsave"],
        "measurements": records,
    }


def build_request(
    *,
    repository: Path,
    qemu_config: tuple[Path, bytes],
    build_config: tuple[Path, bytes],
    vm_config: tuple[Path, bytes],
    expected_payload: tuple[Path, bytes],
    device: dict[str, str],
    control: dict[str, str],
    sector: int,
    payload: dict[str, int],
    guard: dict[str, int],
    nonce: str,
) -> dict[str, object]:
    qemu_path, qemu_bytes = qemu_config
    build_path, build_bytes = build_config
    vm_path, vm_bytes = vm_config
    payload_path, payload_bytes = expected_payload
    if len(payload_bytes) != 512:
        raise PreparedVirtioDmaRequestError(
            "expected payload must be exactly one 512-byte sector"
        )
    if payload["size"] != len(payload_bytes):
        raise PreparedVirtioDmaRequestError(
            "payload size must exactly equal expected payload byte count"
        )
    if _overlap(payload, guard):
        raise PreparedVirtioDmaRequestError(
            "payload range must be strictly outside the guard range"
        )
    _bind_qemu_device(
        _parse_toml(qemu_bytes, label="QEMU config"),
        device=device,
        expected_payload=payload_path.resolve(),
    )
    _bind_control(_parse_toml(qemu_bytes, label="QEMU config"), control=control)
    _bind_build_config(_parse_toml(build_bytes, label="build config"))
    vm_id, vm_regions = _bind_vm_config(_parse_toml(vm_bytes, label="VM config"))
    bound_region = _bound_map_reserved_region(payload=payload, regions=vm_regions)
    if NONCE.fullmatch(nonce) is None:
        raise PreparedVirtioDmaRequestError(
            "session nonce must be 128-bit lower-case hexadecimal"
        )
    return {
        "schemaVersion": 1,
        "artifactStatus": "request-prepared",
        "status": "single_virtio_blk_read_dma_effect_request_prepared",
        "proofScope": "one-virtio-blk-read-request-bound-to-one-qmp-session",
        "doesNotProve": [
            "the request was submitted or completed",
            "DMA was executed",
            "DMA isolation",
            "dual-Guest execution",
            "Linux and Zephyr IP connectivity",
        ],
        "sessionNonce": nonce,
        "qemuName": f"axvisor-dma-effect-{nonce}",
        "repository": _repository(repository),
        "inputs": {
            "qemuConfig": _source(qemu_path, qemu_bytes),
            "buildConfig": _source(build_path, build_bytes),
            "vmConfig": _source(vm_path, vm_bytes),
        },
        "device": device,
        "control": control,
        "request": {
            "requestId": f"virtio-blk-read-{nonce}",
            "operation": "read",
            "sector": sector,
            "vmId": vm_id,
            "payload": {
                "gpa": payload["hpa"],
                "hpa": payload["hpa"],
                "size": payload["size"],
            },
            "guard": guard,
            "boundMapReservedRegion": bound_region,
        },
        "guestMarkers": {
            "ready": f"AXVISOR_VIRTIO_BLK_ODIRECT_READY nonce={nonce} gpa={payload['hpa']:#x} size=512 sector={sector} device={device['guestPath']} control_device={control['guestPath']}",
            "done": f"AXVISOR_VIRTIO_BLK_ODIRECT_DONE nonce={nonce} bytes=512 gpa={payload['hpa']:#x}",
        },
        "expectedPayload": _source(payload_path, payload_bytes),
        "qmpCaptureTemplate": _template(payload=payload, guard=guard),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare one non-executing virtio-blk DMA-effect request"
    )
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--qemu-config", required=True, type=Path)
    parser.add_argument("--build-config", required=True, type=Path)
    parser.add_argument("--vm-config", required=True, type=Path)
    parser.add_argument("--expected-payload", required=True, type=Path)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--bus", required=True)
    parser.add_argument("--guest-device", required=True)
    parser.add_argument("--drive-id", required=True)
    parser.add_argument("--control-socket", required=True)
    parser.add_argument("--sector", required=True)
    parser.add_argument("--payload-hpa", required=True)
    parser.add_argument("--guard-hpa", required=True)
    parser.add_argument("--guard-size", required=True)
    parser.add_argument("--session-nonce", default=None)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        source_paths = [
            args.qemu_config,
            args.build_config,
            args.vm_config,
            args.expected_payload,
        ]
        if len(set(source_paths)) != len(source_paths) or args.output in source_paths:
            raise PreparedVirtioDmaRequestError(
                "all source paths and output must be distinct"
            )
        qemu = (
            args.qemu_config,
            read_regular_bytes(
                args.qemu_config,
                label="QEMU config",
                error_type=PreparedVirtioDmaRequestError,
            ),
        )
        build = (
            args.build_config,
            read_regular_bytes(
                args.build_config,
                label="build config",
                error_type=PreparedVirtioDmaRequestError,
            ),
        )
        vm = (
            args.vm_config,
            read_regular_bytes(
                args.vm_config,
                label="VM config",
                error_type=PreparedVirtioDmaRequestError,
            ),
        )
        expected = (
            args.expected_payload,
            read_regular_bytes(
                args.expected_payload,
                label="expected payload",
                error_type=PreparedVirtioDmaRequestError,
            ),
        )
        payload = _range(
            hpa=_uint(args.payload_hpa, label="--payload-hpa", positive=True),
            size=len(expected[1]),
            label="payload",
        )
        guard = _range(
            hpa=_uint(args.guard_hpa, label="--guard-hpa", positive=True),
            size=_uint(args.guard_size, label="--guard-size", positive=True),
            label="guard",
        )
        nonce = args.session_nonce or secrets.token_hex(16)
        result = build_request(
            repository=args.repository,
            qemu_config=qemu,
            build_config=build,
            vm_config=vm,
            expected_payload=expected,
            device=_device(
                device_id=args.device_id,
                bus=args.bus,
                guest_path=args.guest_device,
                drive_id=args.drive_id,
            ),
            control=_control(nonce=nonce, socket_path=args.control_socket),
            sector=_uint(args.sector, label="--sector"),
            payload=payload,
            guard=guard,
            nonce=nonce,
        )
        publish_new_file(
            args.output,
            (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode(),
            error_type=PreparedVirtioDmaRequestError,
        )
        return 0
    except (OSError, PreparedVirtioDmaRequestError) as error:
        print(f"virtio DMA-effect request preparation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
