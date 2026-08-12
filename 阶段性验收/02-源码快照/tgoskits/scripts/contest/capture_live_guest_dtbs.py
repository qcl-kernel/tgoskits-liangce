#!/usr/bin/env python3
"""Capture final Guest DTBs from one identity-bound, paused QEMU session.

The chain is published only after the QMP peer, pidfile, process start time,
boot id, executable, command line, Unix socket, QEMU name, and run nonce agree
before and after the bounded ``pmemsave`` window.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import sys
import time
from pathlib import Path
from typing import Any, Callable


SCRIPT_DIR = Path(__file__).resolve().parent
IO_API = runpy.run_path(str(SCRIPT_DIR / "guest_dtb_capture_io.py"))
PLANNER_API = runpy.run_path(str(SCRIPT_DIR / "plan_guest_dtb_capture.py"))
EXECUTOR_API = runpy.run_path(str(SCRIPT_DIR / "execute_guest_dtb_capture.py"))
ASSEMBLER_API = runpy.run_path(str(SCRIPT_DIR / "assemble_guest_dtb_capture.py"))

LiveCaptureError = IO_API["GuestDtbCaptureError"]
MAX_SOURCE_LOG_BYTES = int(IO_API["MAX_SOURCE_LOG_BYTES"])
MAX_CAPTURE_PLAN_BYTES = int(IO_API["MAX_CAPTURE_PLAN_BYTES"])
_checked_lstat = IO_API["checked_lstat"]
_path_exists = IO_API["path_exists"]
_publish_bytes_new = IO_API["publish_bytes_new"]
_publish_json_new = IO_API["publish_json_new"]
_read_bounded_regular_file = IO_API["read_bounded_regular_file"]
_reject_reparse_chain = IO_API["_reject_reparse_chain"]

QEMU_NAME_PATTERN = re.compile(
    r"^axvisor-guest-dtb-(?P<nonce>[0-9a-f]{32})$"
)
BOOT_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
FIXED_OUTPUT_NAMES = (
    "axvisor-ready-prefix.log",
    "capture-plan.json",
    "capture-execution.json",
    "capture-identity.json",
    "capture-result.json",
    "capture-chain.json",
)
MAX_PROC_FILE_BYTES = 1024 * 1024


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _serialized(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _checked_directory(path: Path) -> Path:
    absolute = _absolute(path)
    _reject_reparse_chain(absolute)
    _checked_lstat(absolute, field="evidence directory", kind="directory")
    return absolute.resolve(strict=True)


def _checked_input(path: Path, *, field: str, kind: str) -> Path:
    absolute = _absolute(path)
    _reject_reparse_chain(absolute.parent)
    _checked_lstat(absolute, field=field, kind=kind)
    return absolute


def _read_pidfile(path: Path) -> int:
    data, _ = _read_bounded_regular_file(path, byte_limit=64, field="QEMU pidfile")
    if re.fullmatch(rb"[1-9][0-9]{0,9}\n?", data) is None:
        raise LiveCaptureError("QEMU pidfile must contain one positive decimal PID")
    pid = int(data.strip(), 10)
    if pid > (1 << 31) - 1:
        raise LiveCaptureError("QEMU pidfile PID is outside the supported range")
    return pid


def _read_proc_file(path: Path, *, limit: int = MAX_PROC_FILE_BYTES) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise LiveCaptureError(f"could not open process identity file {path}: {error}") from error
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            data = source.read(limit + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > limit:
        raise LiveCaptureError(f"process identity file {path} exceeds its byte limit")
    return data


def _parse_start_time(stat_data: bytes, *, pid: int) -> tuple[str, int]:
    left = stat_data.find(b"(")
    right = stat_data.rfind(b")")
    if left <= 0 or right <= left or not stat_data[:left].strip().isdigit():
        raise LiveCaptureError("QEMU /proc stat record is malformed")
    if int(stat_data[:left].strip(), 10) != pid:
        raise LiveCaptureError("QEMU /proc stat PID does not match pidfile")
    fields = stat_data[right + 1 :].strip().split()
    if len(fields) < 20:
        raise LiveCaptureError("QEMU /proc stat record is truncated")
    try:
        name = stat_data[left + 1 : right].decode("utf-8", errors="strict")
        start_time = int(fields[19], 10)
    except (UnicodeError, ValueError) as error:
        raise LiveCaptureError("QEMU /proc stat identity is malformed") from error
    if start_time <= 0:
        raise LiveCaptureError("QEMU process start time must be positive")
    return name, start_time


def _parse_uids(status_data: bytes) -> list[int]:
    uid_lines = [line for line in status_data.splitlines() if line.startswith(b"Uid:")]
    if len(uid_lines) != 1:
        raise LiveCaptureError("QEMU /proc status has no unique Uid record")
    fields = uid_lines[0].split()[1:]
    if len(fields) != 4 or any(not field.isdigit() for field in fields):
        raise LiveCaptureError("QEMU /proc status Uid record is malformed")
    return [int(field, 10) for field in fields]


def _decode_cmdline(data: bytes) -> list[str]:
    if not data or not data.endswith(b"\0"):
        raise LiveCaptureError("QEMU command line is empty or not NUL-terminated")
    raw_arguments = data[:-1].split(b"\0")
    if not raw_arguments or any(not argument for argument in raw_arguments):
        raise LiveCaptureError("QEMU command line contains an empty argument")
    try:
        return [argument.decode("utf-8", errors="strict") for argument in raw_arguments]
    except UnicodeError as error:
        raise LiveCaptureError("QEMU command line is not strict UTF-8") from error


def build_process_identity(
    *,
    pid: int,
    stat_data: bytes,
    status_data: bytes,
    cmdline_data: bytes,
    boot_id_data: bytes,
    executable: str,
) -> dict[str, Any]:
    """Build the stable process tuple used at every capture checkpoint."""

    process_name, start_time = _parse_start_time(stat_data, pid=pid)
    uids = _parse_uids(status_data)
    argv = _decode_cmdline(cmdline_data)
    try:
        boot_id = boot_id_data.decode("ascii", errors="strict").strip()
    except UnicodeError as error:
        raise LiveCaptureError("kernel boot id is not ASCII") from error
    if BOOT_ID_PATTERN.fullmatch(boot_id) is None:
        raise LiveCaptureError("kernel boot id is malformed")
    if not executable or executable.endswith(" (deleted)"):
        raise LiveCaptureError("QEMU executable link is missing or deleted")
    if not Path(executable).name.startswith("qemu-system-"):
        raise LiveCaptureError("QMP peer executable is not qemu-system-*")
    return {
        "pid": pid,
        "startTimeTicks": start_time,
        "bootId": boot_id,
        "uids": uids,
        "executable": executable,
        "processName": process_name,
        "argv": argv,
        "cmdlineSha256": _sha256(cmdline_data),
    }


def read_process_identity(pid: int, *, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    process = proc_root / str(pid)
    try:
        executable = os.readlink(process / "exe")
    except OSError as error:
        raise LiveCaptureError(f"could not read QEMU executable identity: {error}") from error
    return build_process_identity(
        pid=pid,
        stat_data=_read_proc_file(process / "stat"),
        status_data=_read_proc_file(process / "status"),
        cmdline_data=_read_proc_file(process / "cmdline"),
        boot_id_data=_read_proc_file(proc_root / "sys/kernel/random/boot_id", limit=128),
        executable=executable,
    )


def _option_value(argv: list[str], option: str) -> str:
    equal_form = [argument for argument in argv if argument.startswith(f"{option}=")]
    positions = [index for index, argument in enumerate(argv) if argument == option]
    if equal_form or len(positions) != 1:
        raise LiveCaptureError(f"QEMU command line must contain one exact {option} pair")
    position = positions[0]
    if position + 1 >= len(argv) or argv[position + 1].startswith("-"):
        raise LiveCaptureError(f"QEMU command line has no value for {option}")
    return argv[position + 1]


def validate_launch_identity(
    identity: dict[str, Any], *, qmp_socket: Path, pidfile: Path, qemu_name: str
) -> str:
    match = QEMU_NAME_PATTERN.fullmatch(qemu_name)
    if match is None:
        raise LiveCaptureError(
            "QEMU name must be axvisor-guest-dtb- followed by a 128-bit lowercase hex nonce"
        )
    argv = identity.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise LiveCaptureError("QEMU process identity has no validated argv")
    expected_qmp = f"unix:{qmp_socket},server=on,wait=off"
    if _option_value(argv, "-qmp") != expected_qmp:
        raise LiveCaptureError("QEMU command line QMP socket does not match --qmp-socket")
    if _option_value(argv, "-pidfile") != str(pidfile):
        raise LiveCaptureError("QEMU command line pidfile does not match --qemu-pidfile")
    if _option_value(argv, "-name") != qemu_name:
        raise LiveCaptureError("QEMU command line name does not match --qemu-name")
    return match.group("nonce")


def _stable_process(
    baseline: dict[str, Any], current: dict[str, Any], *, checkpoint: str
) -> None:
    if current != baseline:
        raise LiveCaptureError(f"QEMU process identity changed at {checkpoint}")


def _socket_identity(path: Path) -> dict[str, int]:
    info = _checked_lstat(path, field="QMP socket", kind="socket")
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "uid": int(info.st_uid),
        "gid": int(info.st_gid),
    }


def _response_object(response: dict[str, Any], *, command: str) -> dict[str, Any]:
    value = response.get("return")
    if not isinstance(value, dict):
        raise LiveCaptureError(f"QMP {command} return value is not an object")
    return value


def _query_name(session: Any) -> str:
    response = session.execute_control("query-name", request_id="live-query-name")
    name = _response_object(response, command="query-name").get("name")
    if not isinstance(name, str):
        raise LiveCaptureError("QMP query-name did not return a string name")
    return name


def _query_status(session: Any, *, request_id: str) -> str:
    response = session.execute_control("query-status", request_id=request_id)
    status = _response_object(response, command="query-status").get("status")
    if not isinstance(status, str):
        raise LiveCaptureError("QMP query-status did not return a string status")
    return status


def _wait_status(session: Any, expected: str, *, prefix: str) -> None:
    for attempt in range(20):
        if _query_status(session, request_id=f"{prefix}-{attempt:02d}") == expected:
            return
        time.sleep(0.05)
    raise LiveCaptureError(f"QMP did not enter expected state {expected}")


def freeze_ready_prefix(
    log_data: bytes, *, expected_vm_ids: set[int]
) -> tuple[bytes, dict[str, Any]]:
    """Freeze complete log lines through the final expected ready marker."""

    if not expected_vm_ids or any(vm_id <= 0 for vm_id in expected_vm_ids):
        raise LiveCaptureError("expected VM ids must be distinct positive integers")
    newline_end = log_data.rfind(b"\n") + 1
    if newline_end <= 0:
        raise LiveCaptureError("AxVisor log has no newline-complete prefix")
    complete = log_data[:newline_end]
    try:
        text = complete.decode("utf-8", errors="strict")
        markers = PLANNER_API["parse_guest_dtb_markers"](
            text, expected_vm_ids=expected_vm_ids
        )
    except (UnicodeError, ValueError) as error:
        raise LiveCaptureError(f"AxVisor ready marker prefix is invalid: {error}") from error
    last_line = max(int(marker["sourceLine"]) for marker in markers)
    lines = complete.splitlines(keepends=True)
    prefix = b"".join(lines[:last_line])
    if not prefix.endswith(b"\n"):
        raise LiveCaptureError("final AxVisor ready marker is not newline-complete")
    try:
        canonical_markers = PLANNER_API["parse_guest_dtb_markers"](
            prefix.decode("utf-8", errors="strict"), expected_vm_ids=expected_vm_ids
        )
        plan = PLANNER_API["build_capture_plan"](
            markers=canonical_markers,
            source_log_name=FIXED_OUTPUT_NAMES[0],
            source_log_sha256=_sha256(prefix),
        )
    except (UnicodeError, ValueError) as error:
        raise LiveCaptureError(f"frozen AxVisor ready prefix is invalid: {error}") from error
    return prefix, plan


def _ensure_outputs_absent(directory: Path, plan: dict[str, Any]) -> None:
    names = set(FIXED_OUTPUT_NAMES)
    for guest in plan["guests"]:
        names.add(str(guest["assembledFileName"]))
        names.update(str(segment["fileName"]) for segment in guest["segments"])
    for name in names:
        if Path(name).name != name:
            raise LiveCaptureError("capture plan contains a non-plain evidence filename")
        if _path_exists(directory / name):
            raise LiveCaptureError(f"capture evidence path already exists: {name}")


def _read_evidence(path: Path, *, limit: int = MAX_SOURCE_LOG_BYTES) -> bytes:
    data, _ = _read_bounded_regular_file(path, byte_limit=limit, field=path.name)
    return data


def _artifact(path: Path) -> dict[str, Any]:
    data = _read_evidence(path)
    return {"path": path.name, "size": len(data), "sha256": _sha256(data)}


def build_capture_chain(
    *,
    directory: Path,
    artifact_names: list[str],
    nonce: str,
    artifact_reader: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    artifacts = []
    for name in sorted(set(artifact_names)):
        if Path(name).name != name:
            raise LiveCaptureError("capture chain artifact must be a plain filename")
        if artifact_reader is None:
            artifacts.append(_artifact(directory / name))
            continue
        data = artifact_reader(name)
        if not isinstance(data, bytes) or len(data) > MAX_SOURCE_LOG_BYTES:
            raise LiveCaptureError("capture chain artifact reader returned invalid bytes")
        artifacts.append({"path": name, "size": len(data), "sha256": _sha256(data)})
    return {
        "schemaVersion": 1,
        "artifactStatus": "capture-generated-unreviewed",
        "status": "identity_bound_live_qmp_capture_completed",
        "proofScope": "same-session-paused-qmp-final-guest-dtb-capture-chain",
        "doesNotProve": [
            "the AxVisor log writer belongs to QEMU when stdout is relayed through tee",
            "device-tree semantic validity",
            "captured device ownership matches the VM configuration",
            "both guests booted",
            "passthrough DMA is isolated",
            "Linux and Zephyr have IP connectivity",
        ],
        "sourcePathBase": "capture-chain.json parent directory",
        "sessionNonce": nonce,
        "publication": "all bound artifacts published before this chain manifest",
        "artifacts": artifacts,
    }


def _assemble(
    *, directory: Path, plan: dict[str, Any], plan_sha: str
) -> tuple[dict[str, Any], list[str]]:
    execution_path = directory / "capture-execution.json"
    execution_bytes = _read_evidence(execution_path, limit=MAX_CAPTURE_PLAN_BYTES)
    execution = ASSEMBLER_API["decode_evidence_json"](
        execution_bytes, field="capture execution"
    )
    execution_sha = _sha256(execution_bytes)
    assemblies = ASSEMBLER_API["assemble_guest_dtbs"](
        plan,
        execution,
        directory,
        capture_plan_name="capture-plan.json",
        capture_plan_sha256=plan_sha,
    )
    result = ASSEMBLER_API["build_capture_result"](
        assemblies=assemblies,
        capture_plan_name="capture-plan.json",
        capture_plan_sha256=plan_sha,
        capture_execution_name="capture-execution.json",
        capture_execution_sha256=execution_sha,
    )
    ASSEMBLER_API["publish_capture_result"](
        assemblies,
        _serialized(result),
        directory,
        directory / "capture-result.json",
    )
    return result, [str(item["outputFileName"]) for item in assemblies]


def run_live_capture(
    *,
    live_log: Path,
    evidence_directory: Path,
    expected_vm_ids: set[int],
    qmp_socket: Path,
    qemu_pidfile: Path,
    qemu_name: str,
    timeout_seconds: float,
    process_reader: Callable[[int], dict[str, Any]] = read_process_identity,
    session_factory: Any = None,
) -> dict[str, Any]:
    """Run the fail-closed live capture; dependencies are injectable for tests."""

    directory = _checked_directory(evidence_directory)
    log_path = _checked_input(live_log, field="live AxVisor log", kind="file")
    socket_path = _checked_input(qmp_socket, field="QMP socket", kind="socket")
    pidfile_path = _checked_input(qemu_pidfile, field="QEMU pidfile", kind="file")
    if log_path.parent == directory and log_path.name in FIXED_OUTPUT_NAMES:
        raise LiveCaptureError("live log collides with a capture output")
    pid = _read_pidfile(pidfile_path)
    baseline = process_reader(pid)
    nonce = validate_launch_identity(
        baseline,
        qmp_socket=socket_path,
        pidfile=pidfile_path,
        qemu_name=qemu_name,
    )
    initial_socket = _socket_identity(socket_path)
    factory = session_factory or EXECUTOR_API["UnixQmpSession"]
    checkpoints: dict[str, dict[str, Any]] = {"beforeConnect": baseline}
    states: list[str] = []
    plan: dict[str, Any] | None = None
    plan_sha = ""
    prefix = b""
    execution: dict[str, Any] | None = None
    peer: tuple[int, int, int] | None = None
    stop_issued = False
    primary_error: BaseException | None = None
    resume_error: BaseException | None = None

    with factory(socket_path, timeout_seconds=timeout_seconds) as session:
        peer = session.peer_credentials()
        if peer[0] != pid or peer[1] != baseline["uids"][1]:
            raise LiveCaptureError("QMP SO_PEERCRED does not match pidfile PID/effective UID")
        if _socket_identity(socket_path) != initial_socket:
            raise LiveCaptureError("QMP socket identity changed during connect")
        connected = process_reader(pid)
        _stable_process(baseline, connected, checkpoint="QMP connect")
        checkpoints["afterConnect"] = connected
        session.negotiate()
        if _query_name(session) != qemu_name:
            raise LiveCaptureError("QMP query-name does not match the nonce-bound QEMU name")
        initial_status = _query_status(session, request_id="live-status-before-stop")
        if initial_status != "running":
            raise LiveCaptureError(f"QEMU must be running before capture, got {initial_status}")
        states.append(initial_status)
        try:
            stop_issued = True
            session.execute_control("stop", request_id="live-stop")
            _wait_status(session, "paused", prefix="live-paused")
            states.append("paused")
            paused = process_reader(pid)
            _stable_process(baseline, paused, checkpoint="paused capture window")
            checkpoints["paused"] = paused
            log_data, _ = _read_bounded_regular_file(
                log_path, byte_limit=MAX_SOURCE_LOG_BYTES, field="live AxVisor log"
            )
            prefix, plan = freeze_ready_prefix(
                log_data, expected_vm_ids=expected_vm_ids
            )
            _ensure_outputs_absent(directory, plan)
            plan_bytes = _serialized(plan)
            plan_sha = _sha256(plan_bytes)
            _publish_bytes_new(prefix, directory / FIXED_OUTPUT_NAMES[0])
            _publish_bytes_new(plan_bytes, directory / "capture-plan.json")
            execution = EXECUTOR_API["execute_capture"](
                plan,
                directory,
                capture_plan_name="capture-plan.json",
                capture_plan_sha256=plan_sha,
                output_path=directory / "capture-execution.json",
                session=session,
                negotiate_session=False,
            )
            captured = process_reader(pid)
            _stable_process(baseline, captured, checkpoint="post-pmemsave")
            checkpoints["postPmemsave"] = captured
        except BaseException as error:  # noqa: BLE001 - resume before propagating.
            primary_error = error
        finally:
            if stop_issued:
                try:
                    session.execute_control("cont", request_id="live-cont")
                    _wait_status(session, "running", prefix="live-running")
                    states.append("running")
                    resumed = process_reader(pid)
                    _stable_process(baseline, resumed, checkpoint="resume")
                    checkpoints["resumed"] = resumed
                except BaseException as error:  # noqa: BLE001 - preserve both failures.
                    resume_error = error

    if resume_error is not None:
        if primary_error is not None:
            raise LiveCaptureError(
                f"live capture failed ({primary_error}); QEMU resume also failed: {resume_error}"
            ) from primary_error
        raise LiveCaptureError(f"QEMU resume failed after capture: {resume_error}") from resume_error
    if primary_error is not None:
        if isinstance(primary_error, LiveCaptureError):
            raise primary_error
        raise LiveCaptureError(f"live capture failed: {primary_error}") from primary_error
    if plan is None or execution is None or peer is None:
        raise LiveCaptureError("live capture ended without a complete execution result")
    if _read_pidfile(pidfile_path) != pid:
        raise LiveCaptureError("QEMU pidfile changed after capture")
    if _socket_identity(socket_path) != initial_socket:
        raise LiveCaptureError("QMP socket identity changed after capture")

    execution_bytes = _read_evidence(
        directory / "capture-execution.json", limit=MAX_CAPTURE_PLAN_BYTES
    )
    identity = {
        "schemaVersion": 1,
        "artifactStatus": "capture-generated-unreviewed",
        "status": "qmp_peer_process_and_pause_window_bound",
        "proofScope": "qmp-socket-pid-start-boot-uid-exe-cmdline-name-nonce-continuity",
        "doesNotProve": [
            "the AxVisor log writer belongs to QEMU when stdout is relayed through tee",
            "Guest boot success",
            "passthrough DMA isolation",
            "Linux and Zephyr IP connectivity",
        ],
        "sessionNonce": nonce,
        "qemuName": qemu_name,
        "qmpQueryName": qemu_name,
        "pidfile": {"path": str(pidfile_path), "pid": pid},
        "qmpSocket": {"path": str(socket_path), **initial_socket},
        "qmpPeerCredentials": {"pid": peer[0], "uid": peer[1], "gid": peer[2]},
        "qmpStates": states,
        "processCheckpoints": checkpoints,
        "sources": {
            "readyPrefix": {
                "path": FIXED_OUTPUT_NAMES[0],
                "size": len(prefix),
                "sha256": _sha256(prefix),
            },
            "capturePlan": {"path": "capture-plan.json", "sha256": plan_sha},
            "captureExecution": {
                "path": "capture-execution.json",
                "sha256": _sha256(execution_bytes),
            },
        },
    }
    _publish_json_new(identity, directory / "capture-identity.json")
    result, final_dtb_names = _assemble(directory=directory, plan=plan, plan_sha=plan_sha)
    segment_names = [
        str(segment["fileName"])
        for guest in plan["guests"]
        for segment in guest["segments"]
    ]
    chain_names = [
        *FIXED_OUTPUT_NAMES[:5],
        *segment_names,
        *final_dtb_names,
    ]
    chain = build_capture_chain(directory=directory, artifact_names=chain_names, nonce=nonce)
    _publish_json_new(chain, directory / "capture-chain.json")
    return {"identity": identity, "execution": execution, "result": result, "chain": chain}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture final Guest DTBs from one identity-bound live QEMU session"
    )
    parser.add_argument("--live-log", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--expected-vm", required=True, action="append", type=int)
    parser.add_argument("--qmp-socket", required=True, type=Path)
    parser.add_argument("--qemu-pidfile", required=True, type=Path)
    parser.add_argument("--qemu-name", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    expected_vm_ids = set(args.expected_vm)
    if len(expected_vm_ids) != len(args.expected_vm):
        print("Live Guest DTB capture failed: expected VM ids repeat", file=sys.stderr)
        return 1
    try:
        result = run_live_capture(
            live_log=args.live_log,
            evidence_directory=args.evidence_dir,
            expected_vm_ids=expected_vm_ids,
            qmp_socket=args.qmp_socket,
            qemu_pidfile=args.qemu_pidfile,
            qemu_name=args.qemu_name,
            timeout_seconds=args.timeout_seconds,
        )
    except (LiveCaptureError, ValueError) as error:
        print(f"Live Guest DTB capture failed: {error}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"Could not read/write live Guest DTB evidence: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result["chain"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
