#!/usr/bin/env python3
"""Behavioral contract for identity-bound live QMP Guest-DTB capture."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


sys.dont_write_bytecode = True


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
LIVE_CAPTURE = WORKSPACE_ROOT / "scripts/contest/capture_live_guest_dtbs.py"
EXECUTOR = WORKSPACE_ROOT / "scripts/contest/execute_guest_dtb_capture.py"
AXVISOR_ARGS = WORKSPACE_ROOT / "scripts/axbuild/src/axvisor/mod.rs"
AXVISOR_QEMU = WORKSPACE_ROOT / "scripts/axbuild/src/axvisor/rootfs.rs"
XTASK = WORKSPACE_ROOT / "os/axvisor/xtask/src/main.rs"
README = WORKSPACE_ROOT / "scripts/contest/README.md"
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def process_fixture(module: ModuleType) -> dict[str, object]:
    pid = 4242
    qmp_socket = Path("/run/user/1000/axvisor-qmp.sock")
    pidfile = Path("/run/user/1000/axvisor-qemu.pid")
    name = "axvisor-guest-dtb-0123456789abcdef0123456789abcdef"
    argv = [
        "/usr/bin/qemu-system-aarch64",
        "-nographic",
        "-qmp",
        f"unix:{qmp_socket},server=on,wait=off",
        "-pidfile",
        str(pidfile),
        "-name",
        name,
    ]
    stat_fields = [b"S", *([b"0"] * 18), b"987654"]
    stat_data = f"{pid} (qemu-system-aar) ".encode() + b" ".join(stat_fields) + b"\n"
    identity = module.build_process_identity(
        pid=pid,
        stat_data=stat_data,
        status_data=b"Name:\tqemu\nUid:\t1000\t1000\t1000\t1000\n",
        cmdline_data=b"\0".join(argument.encode() for argument in argv) + b"\0",
        boot_id_data=b"11111111-2222-3333-4444-555555555555\n",
        executable="/usr/bin/qemu-system-aarch64",
    )
    return {
        "identity": identity,
        "qmp_socket": qmp_socket,
        "pidfile": pidfile,
        "name": name,
    }


def expect_live_rejected(
    errors: list[str], module: ModuleType, function, expected: str, label: str
) -> None:
    try:
        function()
    except module.LiveCaptureError as error:
        if expected not in str(error):
            errors.append(f"live capture reports the wrong {label} error: {error}")
    else:
        errors.append(f"live capture accepts {label}")


def check_identity_contract(errors: list[str], module: ModuleType) -> None:
    fixture = process_fixture(module)
    identity = fixture["identity"]
    try:
        nonce = module.validate_launch_identity(
            identity,
            qmp_socket=fixture["qmp_socket"],
            pidfile=fixture["pidfile"],
            qemu_name=fixture["name"],
        )
    except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
        errors.append(f"live capture rejects a valid process identity: {error}")
    else:
        if nonce != "0123456789abcdef0123456789abcdef":
            errors.append("live capture does not extract the 128-bit launch nonce")
        if identity.get("startTimeTicks") != 987654:
            errors.append("live capture parses the wrong /proc start-time field")
        if identity.get("uids") != [1000, 1000, 1000, 1000]:
            errors.append("live capture does not retain all /proc Uid fields")

    weak_name = lambda: module.validate_launch_identity(
        identity,
        qmp_socket=fixture["qmp_socket"],
        pidfile=fixture["pidfile"],
        qemu_name="axvisor-guest-dtb-predictable",
    )
    expect_live_rejected(errors, module, weak_name, "128-bit", "weak QEMU name")

    duplicate = copy.deepcopy(identity)
    duplicate["argv"].extend(["-qmp", "unix:/tmp/attacker,server=on,wait=off"])
    expect_live_rejected(
        errors,
        module,
        lambda: module.validate_launch_identity(
            duplicate,
            qmp_socket=fixture["qmp_socket"],
            pidfile=fixture["pidfile"],
            qemu_name=fixture["name"],
        ),
        "one exact -qmp pair",
        "duplicate QMP command-line option",
    )

    changed = copy.deepcopy(identity)
    changed["startTimeTicks"] = int(changed["startTimeTicks"]) + 1
    expect_live_rejected(
        errors,
        module,
        lambda: module._stable_process(identity, changed, checkpoint="test"),
        "changed at test",
        "PID reuse/start-time change",
    )


def check_prefix_contract(errors: list[str], module: ModuleType) -> None:
    marker_one = (
        b"AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=40 "
        b"hpa_segments=0x90000000:40\n"
    )
    marker_two = (
        b"AXVISOR_GUEST_DTB_READY vm=2 gpa=0x8ff00000 size=40 "
        b"hpa_segments=0x91000000:40\n"
    )
    log = b"booting\n" + marker_one + b"between\n" + marker_two + b"after\npartial"
    try:
        prefix, plan = module.freeze_ready_prefix(log, expected_vm_ids={1, 2})
    except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
        errors.append(f"live capture rejects valid newline-complete markers: {error}")
        return
    if not prefix.endswith(marker_two) or b"after" in prefix or b"partial" in prefix:
        errors.append("live capture does not freeze exactly through the final ready marker")
    if [guest.get("vmId") for guest in plan.get("guests", [])] != [1, 2]:
        errors.append("frozen prefix does not produce a deterministic two-VM plan")
    source = plan.get("source", {}).get("axvisorLog", {})
    if source.get("path") != "axvisor-ready-prefix.log":
        errors.append("live capture plan does not bind the immutable prefix filename")

    duplicate = b"boot\n" + marker_one + marker_one
    expect_live_rejected(
        errors,
        module,
        lambda: module.freeze_ready_prefix(duplicate, expected_vm_ids={1}),
        "duplicate marker",
        "duplicate ready marker",
    )


def check_chain_contract(errors: list[str], module: ModuleType) -> None:
    data = {"a.bin": b"alpha", "b.json": b'{"b": 2}\n'}
    first = module.build_capture_chain(
        directory=Path("unused"),
        artifact_names=["b.json", "a.bin"],
        nonce="0" * 32,
        artifact_reader=data.__getitem__,
    )
    second = module.build_capture_chain(
        directory=Path("unused"),
        artifact_names=["a.bin", "b.json"],
        nonce="0" * 32,
        artifact_reader=data.__getitem__,
    )
    if first != second:
        errors.append("live capture chain is not deterministic by artifact filename")
    if first.get("status") != "identity_bound_live_qmp_capture_completed":
        errors.append("live capture chain overstates or changes its narrow status")
    artifacts = first.get("artifacts", [])
    if [artifact.get("path") for artifact in artifacts] != ["a.bin", "b.json"]:
        errors.append("live capture chain does not sort and enumerate all artifacts")
    boundaries = set(first.get("doesNotProve", []))
    for boundary in (
        "device-tree semantic validity",
        "both guests booted",
        "passthrough DMA is isolated",
        "Linux and Zephyr have IP connectivity",
    ):
        if boundary not in boundaries:
            errors.append(f"live capture chain omits proof boundary `{boundary}`")


def check_executor_control_contract(errors: list[str], executor: ModuleType) -> None:
    session = object.__new__(executor.UnixQmpSession)
    session._negotiated = True
    requests: list[dict[str, object]] = []

    def request(payload: dict[str, object]) -> dict[str, object]:
        requests.append(payload)
        return {"return": {}, "id": payload["id"]}

    session._request = request
    for command in ("query-name", "query-status", "stop", "cont"):
        session.execute_control(command, request_id=f"test-{command}")
    if [request.get("execute") for request in requests] != [
        "query-name",
        "query-status",
        "stop",
        "cont",
    ]:
        errors.append("QMP control helper changes the allow-listed command order")
    try:
        session.execute_control("human-monitor-command", request_id="test-hmp")
    except executor.GuestDtbCaptureError:
        pass
    else:
        errors.append("QMP control helper accepts a non-allow-listed command")


def main() -> int:
    errors: list[str] = []
    if not LIVE_CAPTURE.is_file():
        errors.append("identity-bound live Guest-DTB capture helper is missing")
    else:
        live_source = LIVE_CAPTURE.read_text(encoding="utf-8")
        if len(live_source.splitlines()) >= 800:
            errors.append("live Guest-DTB capture helper exceeds 800 production lines")
        for token in (
            "peer_credentials()",
            "read_process_identity",
            'session.execute_control("stop"',
            'session.execute_control("cont"',
            "negotiate_session=False",
            '"qmpQueryName": qemu_name',
            'directory / "capture-chain.json"',
        ):
            if token not in live_source:
                errors.append(f"live Guest-DTB capture helper omits `{token}`")
        stop_index = live_source.find('session.execute_control("stop"')
        capture_index = live_source.find('EXECUTOR_API["execute_capture"]')
        cont_index = live_source.find('session.execute_control("cont"')
        if not 0 <= stop_index < capture_index < cont_index:
            errors.append("live capture does not bound pmemsave between stop and cont")
        try:
            module = load_module("guest_dtb_live_capture", LIVE_CAPTURE)
            executor = load_module("guest_dtb_live_executor", EXECUTOR)
        except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
            errors.append(f"live Guest-DTB capture modules cannot be imported: {error}")
        else:
            check_identity_contract(errors, module)
            check_prefix_contract(errors, module)
            check_chain_contract(errors, module)
            check_executor_control_contract(errors, executor)

    sources = {
        "AxVisor qemu CLI": AXVISOR_ARGS.read_text(encoding="utf-8"),
        "AxVisor qemu patch": AXVISOR_QEMU.read_text(encoding="utf-8"),
        "AxVisor xtask normalization": XTASK.read_text(encoding="utf-8"),
    }
    required = {
        "AxVisor qemu CLI": ["qmp_socket", "qemu_pidfile", "qemu_name", "requires_all"],
        "AxVisor qemu patch": [
            "patch_live_capture_identity_args",
            "unix:{qmp_socket},server=on,wait=off",
            "raw QEMU args conflict",
            "axvisor-guest-dtb-",
        ],
        "AxVisor xtask normalization": [
            "normalize_output_path(&mut args.qmp_socket",
            "normalize_output_path(&mut args.qemu_pidfile",
        ],
    }
    for label, tokens in required.items():
        for token in tokens:
            if token not in sources[label]:
                errors.append(f"{label} omits `{token}`")

    readme = README.read_text(encoding="utf-8")
    for token in (
        "capture_live_guest_dtbs.py",
        "--qemu-pidfile",
        "--qemu-name",
        "SO_PEERCRED",
        "stdout is relayed through `tee`",
    ):
        if token not in readme:
            errors.append(f"live capture README guidance omits `{token}`")

    ci = CI.read_text(encoding="utf-8")
    expected_ci = "python3 scripts/test/check_axvisor_guest_dtb_live_capture.py"
    if expected_ci not in ci:
        errors.append("identity-bound live capture contract is not wired into CI")

    if not errors:
        return 0
    print("AxVisor identity-bound live Guest-DTB capture contract failed:")
    for error in errors:
        print(f"  - {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
