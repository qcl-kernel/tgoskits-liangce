#!/usr/bin/env python3
"""Run one AxVisor Guest until its final DTB is QMP-captured and sealed.

This is a deliberately narrow POSIX/WSL harness.  It starts a single-Guest
``cargo xtask qemu`` invocation in its own process group, owns its combined
stdout/stderr log, waits for exactly one complete VM-1 READY marker, delegates
the paused same-session capture to :mod:`capture_live_guest_dtbs`, confirms
that QEMU resumed with the original identity, and only then sends QEMU SIGINT.

``capture-chain.json`` remains the final manifest inside ``live-capture``.
The outer ``status.json`` is published last for the complete launch/capture/
shutdown attempt.  It is evidence packaging, not a claim that the Guest
booted, DMA is isolated, or any network path works.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import secrets
import select
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, Callable


SCRIPT_DIR = Path(__file__).resolve().parent
CAPTURE_API = __import__("runpy").run_path(
    str(SCRIPT_DIR / "capture_live_guest_dtbs.py")
)

LiveCaptureError = CAPTURE_API["LiveCaptureError"]
MAX_SOURCE_LOG_BYTES = int(CAPTURE_API["MAX_SOURCE_LOG_BYTES"])
_checked_directory = CAPTURE_API["_checked_directory"]
_checked_input = CAPTURE_API["_checked_input"]
_checked_lstat = CAPTURE_API["_checked_lstat"]
_path_exists = CAPTURE_API["_path_exists"]
_publish_json_new = CAPTURE_API["_publish_json_new"]
_read_pidfile = CAPTURE_API["_read_pidfile"]
_read_bounded_regular_file = CAPTURE_API["IO_API"]["read_bounded_regular_file"]
_sha256 = CAPTURE_API["_sha256"]
_stable_process = CAPTURE_API["_stable_process"]
_option_value = CAPTURE_API["_option_value"]
freeze_ready_prefix = CAPTURE_API["freeze_ready_prefix"]
read_process_identity = CAPTURE_API["read_process_identity"]
run_live_capture = CAPTURE_API["run_live_capture"]
validate_launch_identity = CAPTURE_API["validate_launch_identity"]


RUN_SCHEMA_VERSION = 1
READY_VM_ID = 1
READY_PREFIX = b"AXVISOR_GUEST_DTB_READY"
QEMU_NAME_PREFIX = "axvisor-guest-dtb-"
MAX_QMP_SOCKET_PATH_BYTES = 100
MAX_QEMU_CONFIG_BYTES = 4 * 1024 * 1024
MAX_HOST_DTB_BYTES = 64 * 1024 * 1024
MAX_POST_RESUME_MARKER_BYTES = 256
DEFAULT_POST_RESUME_TIMEOUT_SECONDS = 30.0
DEFAULT_RUNTIME_PREFIX = "axgdtb-"
SIGKILL = getattr(signal, "SIGKILL", 9)


class HarnessError(ValueError):
    """The single-Guest live-capture harness cannot establish its contract."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bound_input_artifact(path: Path, *, field: str) -> dict[str, Any]:
    """Bind an immutable input by path, stable identity, byte count, and digest."""

    before = _checked_lstat(path, field=field, kind="file")
    digest = _sha256_file(path)
    after = _checked_lstat(path, field=field, kind="file")
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise HarnessError(f"{field} changed while its evidence binding was calculated")
    return {"path": str(path), "size": int(after.st_size), "sha256": digest}


def _checked_host_dtb(path: Path) -> Path:
    """Resolve a non-link Host DTB without accepting a path indirection."""

    checked = _checked_file(path, field="Host DTB")
    try:
        resolved = checked.resolve(strict=True)
    except OSError as error:
        raise HarnessError(f"could not resolve Host DTB: {error}") from error
    if not resolved.is_absolute():
        raise HarnessError("Host DTB path must resolve to an absolute path")
    # _checked_file rejects links/reparse points for both the leaf and parent
    # chain.  Re-check the resolved leaf so the binding never silently follows
    # a replacement after resolution.
    _checked_lstat(resolved, field="Host DTB", kind="file")
    return resolved


def _bound_host_dtb(path: Path) -> dict[str, Any]:
    """Read and bind the Host DTB through the no-follow regular-file primitive."""

    checked = _checked_host_dtb(path)
    before = _checked_lstat(checked, field="Host DTB", kind="file")
    try:
        data, _ = _read_bounded_regular_file(
            checked, byte_limit=MAX_HOST_DTB_BYTES, field="Host DTB"
        )
    except LiveCaptureError as error:
        raise HarnessError(str(error)) from error
    after = _checked_lstat(checked, field="Host DTB", kind="file")
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise HarnessError("Host DTB changed while its evidence binding was calculated")
    return {"path": str(checked), "size": len(data), "sha256": _sha256(data)}


def _verify_bound_host_dtb(path: Path, binding: dict[str, Any]) -> None:
    """Require the success-published Host DTB bytes to equal launch bytes."""

    if _bound_host_dtb(path) != binding:
        raise HarnessError("Host DTB changed after QEMU launch")


def _validated_qemu_host_dtb_args(qemu_config: Path, *, host_dtb: Path) -> list[str]:
    """Require one exact raw ``-dtb PATH`` pair in the launcher QEMU config."""

    try:
        data, _ = _read_bounded_regular_file(
            qemu_config, byte_limit=MAX_QEMU_CONFIG_BYTES, field="QEMU config"
        )
        parsed = tomllib.loads(data.decode("utf-8", errors="strict"))
    except (LiveCaptureError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise HarnessError(f"could not parse QEMU config for Host DTB binding: {error}") from error
    args = parsed.get("args") if isinstance(parsed, dict) else None
    if not isinstance(args, list) or not all(isinstance(argument, str) for argument in args):
        raise HarnessError("QEMU config args must be a string list for Host DTB binding")
    positions = [index for index, argument in enumerate(args) if argument == "-dtb"]
    if len(positions) != 1 or any(argument.startswith("-dtb=") for argument in args):
        raise HarnessError("QEMU config must contain exactly one standalone -dtb option")
    position = positions[0]
    if position + 1 >= len(args) or args[position + 1] != str(host_dtb):
        raise HarnessError("QEMU config -dtb value does not exactly match --host-dtb")
    return list(args)


def _validate_host_dtb_process_identity(identity: dict[str, Any], *, host_dtb: Path | None) -> None:
    """Bind the real QEMU argv to the same exact DTB option, never a substring."""

    if host_dtb is None:
        return
    argv = identity.get("argv")
    if not isinstance(argv, list) or not all(isinstance(argument, str) for argument in argv):
        raise HarnessError("QEMU process identity has no validated argv for Host DTB binding")
    try:
        value = _option_value(argv, "-dtb")
    except LiveCaptureError as error:
        raise HarnessError(f"QEMU process Host DTB option is malformed: {error}") from error
    if value != str(host_dtb):
        raise HarnessError("QEMU process -dtb value does not exactly match --host-dtb")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not seconds > 0 or seconds > 86_400:
        raise argparse.ArgumentTypeError("must be greater than zero and at most 86400")
    return seconds


def _post_resume_marker(value: str) -> str:
    """Argparse validator for one bounded printable-ASCII byte marker."""

    try:
        encoded = value.encode("ascii", errors="strict")
    except UnicodeError as error:
        raise argparse.ArgumentTypeError("must contain printable ASCII only") from error
    if not encoded:
        raise argparse.ArgumentTypeError("must not be empty")
    if len(encoded) > MAX_POST_RESUME_MARKER_BYTES:
        raise argparse.ArgumentTypeError(
            f"must be at most {MAX_POST_RESUME_MARKER_BYTES} bytes"
        )
    if any(byte < 0x20 or byte > 0x7E for byte in encoded):
        raise argparse.ArgumentTypeError("must be one line of printable ASCII")
    return value


def _validated_post_resume_marker(value: str | None) -> bytes | None:
    if value is None:
        return None
    try:
        return _post_resume_marker(value).encode("ascii", errors="strict")
    except argparse.ArgumentTypeError as error:
        raise HarnessError(f"invalid post-resume marker: {error}") from error


def _new_evidence_directory(path: Path) -> Path:
    directory = _absolute(path)
    if _path_exists(directory):
        raise HarnessError("--evidence-dir must not exist before a live capture attempt")
    parent = _checked_directory(directory.parent)
    try:
        directory.mkdir(mode=0o700)
    except OSError as error:
        raise HarnessError(f"could not create live-capture evidence directory: {error}") from error
    try:
        return _checked_directory(directory)
    except Exception:
        # Do not remove a directory whose identity cannot be proven after creation.
        raise


def _new_directory(parent: Path, name: str) -> Path:
    path = parent / name
    if _path_exists(path):
        raise HarnessError(f"live-capture output path already exists: {name}")
    try:
        path.mkdir(mode=0o700)
    except OSError as error:
        raise HarnessError(f"could not create live-capture path {name}: {error}") from error
    return _checked_directory(path)


def _checked_file(path: Path, *, field: str) -> Path:
    return _checked_input(path, field=field, kind="file")


def _checked_runtime_directory(path: Path) -> Path:
    directory = _absolute(path)
    if _path_exists(directory):
        raise HarnessError("runtime directory must not exist before a live capture attempt")
    _checked_directory(directory.parent)
    try:
        directory.mkdir(mode=0o700)
    except OSError as error:
        raise HarnessError(f"could not create runtime directory: {error}") from error
    return _checked_directory(directory)


def _socket_path_within_limit(runtime: Path) -> Path:
    socket_path = runtime / "qmp.sock"
    try:
        encoded = str(socket_path).encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise HarnessError("QMP socket path is not UTF-8 encodable") from error
    if len(encoded) > MAX_QMP_SOCKET_PATH_BYTES:
        raise HarnessError(
            f"QMP socket path exceeds {MAX_QMP_SOCKET_PATH_BYTES} UTF-8 bytes"
        )
    return socket_path


def _new_runtime_directory(
    *, evidence_directory: Path, runtime_directory: Path | None, nonce: str
) -> Path:
    candidate = (
        Path("/tmp") / f"{DEFAULT_RUNTIME_PREFIX}{nonce}"
        if runtime_directory is None
        else _absolute(runtime_directory)
    )
    _socket_path_within_limit(candidate)
    evidence = evidence_directory.resolve(strict=True)
    if candidate == evidence or evidence in candidate.parents:
        raise HarnessError("runtime directory must be separate from --evidence-dir")
    runtime = _checked_runtime_directory(candidate)
    return runtime


def build_qemu_command(
    *,
    cargo_bin: str,
    build_config: Path,
    qemu_config: Path,
    vmconfig: Path,
    qmp_socket: Path,
    qemu_pidfile: Path,
    qemu_name: str,
    rootfs: Path | None = None,
) -> list[str]:
    """Build the one supported xtask invocation without shell interpolation."""

    if not cargo_bin or "/" in cargo_bin or "\\" in cargo_bin:
        raise HarnessError("--cargo-bin must be a PATH command name without a path separator")
    if not all(path.as_posix().startswith("/") for path in (build_config, qemu_config, vmconfig)):
        raise HarnessError("all QEMU input paths must be absolute")
    if not all(path.as_posix().startswith("/") for path in (qmp_socket, qemu_pidfile)):
        raise HarnessError("QMP socket and pidfile paths must be absolute")
    if rootfs is not None and not rootfs.as_posix().startswith("/"):
        raise HarnessError("--rootfs path must be absolute")
    qemu_nonce = qemu_name.removeprefix(QEMU_NAME_PREFIX)
    if (
        not qemu_name.startswith(QEMU_NAME_PREFIX)
        or len(qemu_nonce) != 32
        or any(byte not in "0123456789abcdef" for byte in qemu_nonce)
    ):
        raise HarnessError("QEMU name must contain an exact 128-bit nonce")
    command = [
        cargo_bin,
        "xtask",
        "qemu",
        "--config",
        str(build_config),
        "--qemu-config",
        str(qemu_config),
        "--vmconfigs",
        str(vmconfig),
        "--qmp-socket",
        str(qmp_socket),
        "--qemu-pidfile",
        str(qemu_pidfile),
        "--qemu-name",
        qemu_name,
    ]
    if rootfs is not None:
        command.extend(["--rootfs", str(rootfs)])
    return command


def _read_log_snapshot(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as error:
        transient = {errno.EINTR, errno.EAGAIN, getattr(errno, "ENODATA", 61)}
        if error.errno in transient:
            return b""
        raise HarnessError(f"could not read owned AxVisor log: {error}") from error
    if len(data) > MAX_SOURCE_LOG_BYTES:
        raise HarnessError("owned AxVisor log exceeds the capture byte limit")
    return data


def wait_for_unique_ready(
    *,
    log_path: Path,
    launcher: Any,
    timeout_seconds: float,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait until one newline-complete, parser-validated VM-1 marker exists.

    A READY-looking complete line which fails the canonical parser is terminal;
    a partial final line is intentionally ignored until it becomes complete.
    """

    deadline = now() + timeout_seconds
    while True:
        data = _read_log_snapshot(log_path)
        complete_end = data.rfind(b"\n") + 1
        complete = data[:complete_end]
        if READY_PREFIX in complete:
            try:
                prefix, plan = freeze_ready_prefix(data, expected_vm_ids={READY_VM_ID})
            except LiveCaptureError as error:
                raise HarnessError(f"READY marker is not uniquely captureable: {error}") from error
            return {
                "readyPrefixBytes": len(prefix),
                "readyPrefixSha256": _sha256(prefix),
                "capturePlanGuestIds": [guest["vmId"] for guest in plan["guests"]],
            }
        exit_code = launcher.poll()
        if exit_code is not None:
            raise HarnessError(f"QEMU launcher exited before the unique READY marker: {exit_code}")
        if now() >= deadline:
            raise HarnessError("timed out waiting for a unique newline-complete VM-1 READY marker")
        sleep(0.10)


def _post_resume_marker_observation(
    *,
    data: bytes,
    ready_prefix_bytes: int,
    ready_prefix_sha256: str,
    marker: bytes,
) -> dict[str, Any] | None:
    """Validate the immutable READY prefix and count one exact later marker."""

    if ready_prefix_bytes <= 0 or len(data) < ready_prefix_bytes:
        raise HarnessError("owned AxVisor log shrank below its immutable READY prefix")
    ready_prefix = data[:ready_prefix_bytes]
    if _sha256(ready_prefix) != ready_prefix_sha256:
        raise HarnessError("owned AxVisor log READY prefix changed after QMP resume")
    if marker in ready_prefix:
        raise HarnessError("post-resume marker already exists in the READY prefix")
    post_resume = data[ready_prefix_bytes:]
    first = post_resume.find(marker)
    if first >= 0 and post_resume.find(marker, first + 1) >= 0:
        raise HarnessError("post-resume marker appears more than once")
    if first < 0:
        return None
    offset = ready_prefix_bytes + first
    return {
        "marker": marker.decode("ascii", errors="strict"),
        "byteOffset": offset,
        "markerBytes": len(marker),
        "readyPrefixBytes": ready_prefix_bytes,
        "observedPrefixBytes": len(data),
        "observedPrefixSha256": _sha256(data),
        "occurrencesInPostResumeRegion": 1,
        "evidenceMeaning": "exact-byte-substring-observation-only",
    }


def wait_for_unique_post_resume_marker(
    *,
    log_path: Path,
    launcher: Any,
    ready_prefix_bytes: int,
    ready_prefix_sha256: str,
    marker: bytes,
    timeout_seconds: float,
    process_exited: Callable[[], bool] = lambda: False,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait for exactly one byte marker while both launcher and QEMU live."""

    if not marker or len(marker) > MAX_POST_RESUME_MARKER_BYTES or any(
        byte < 0x20 or byte > 0x7E for byte in marker
    ):
        raise HarnessError("post-resume marker must be bounded printable ASCII")
    if not 0 < timeout_seconds <= 86_400:
        raise HarnessError("post-resume timeout must be greater than zero and at most 86400")
    deadline = now() + timeout_seconds
    while True:
        data = _read_log_snapshot(log_path)
        observation = (
            None
            if len(data) < ready_prefix_bytes
            else _post_resume_marker_observation(
                data=data,
                ready_prefix_bytes=ready_prefix_bytes,
                ready_prefix_sha256=ready_prefix_sha256,
                marker=marker,
            )
        )
        exit_code = launcher.poll()
        try:
            qemu_exited = bool(process_exited())
        except OSError as error:
            raise HarnessError(f"could not inspect QEMU while waiting for marker: {error}") from error
        if exit_code is not None or qemu_exited:
            raise HarnessError("QEMU exited before the post-resume marker was sealed")
        if observation is not None:
            return observation
        if now() >= deadline:
            raise HarnessError("timed out waiting for the unique post-resume marker")
        sleep(0.10)


def _pidfd_exited(pidfd: int, *, poll_factory: Callable[[], Any] | None = None) -> bool:
    factory = poll_factory or getattr(select, "poll", None)
    if factory is None:
        raise HarnessError("Linux pidfd polling support is required")
    poller = factory()
    poller.register(
        pidfd,
        getattr(select, "POLLIN", 0x001)
        | getattr(select, "POLLERR", 0x008)
        | getattr(select, "POLLHUP", 0x010),
    )
    return bool(poller.poll(0))


def _bound_running_identity(
    *, qmp_socket: Path, pidfile: Path, qemu_name: str, host_dtb: Path | None = None
) -> tuple[int, dict[str, Any]]:
    pid = _read_pidfile(pidfile)
    identity = read_process_identity(pid)
    validate_launch_identity(
        identity, qmp_socket=qmp_socket, pidfile=pidfile, qemu_name=qemu_name
    )
    _validate_host_dtb_process_identity(identity, host_dtb=host_dtb)
    return pid, identity


def _nested_resumed_identity(nested: dict[str, Any]) -> dict[str, Any]:
    try:
        identity = nested["identity"]
        checkpoints = identity["processCheckpoints"]
        resumed = checkpoints["resumed"]
    except (KeyError, TypeError) as error:
        raise HarnessError("nested capture result has no resumed process identity") from error
    if not isinstance(resumed, dict):
        raise HarnessError("nested capture resumed process identity is malformed")
    return resumed


def _verify_nested_resume_continuity(
    *, nested: dict[str, Any], current_pid: int, current_identity: dict[str, Any]
) -> dict[str, Any]:
    resumed = _nested_resumed_identity(nested)
    if resumed.get("pid") != current_pid:
        raise HarnessError("nested resumed identity PID does not match current QEMU PID")
    _stable_process(resumed, current_identity, checkpoint="nested capture resume")
    return resumed


def _open_verified_pidfd(
    *, qmp_socket: Path, pidfile: Path, qemu_name: str, host_dtb: Path | None = None
) -> tuple[int, int, dict[str, Any]]:
    """Bind the final SIGINT to a process descriptor, not a reusable PID."""

    if sys.platform != "linux" or not hasattr(os, "pidfd_open") or not hasattr(
        signal, "pidfd_send_signal"
    ):
        raise HarnessError("Linux pidfd_open and signal.pidfd_send_signal are required")
    pid, before = _bound_running_identity(
        qmp_socket=qmp_socket, pidfile=pidfile, qemu_name=qemu_name, host_dtb=host_dtb
    )
    try:
        pidfd = os.pidfd_open(pid, 0)
    except OSError as error:
        raise HarnessError(f"could not open pidfd for identity-bound QEMU exit: {error}") from error
    try:
        after_pid, after = _bound_running_identity(
            qmp_socket=qmp_socket, pidfile=pidfile, qemu_name=qemu_name, host_dtb=host_dtb
        )
        if after_pid != pid:
            raise HarnessError("QEMU pidfile PID changed while opening pidfd")
        _stable_process(before, after, checkpoint="pidfd open")
    except Exception:
        os.close(pidfd)
        raise
    return pidfd, pid, after


def _wait_for_pidfd_exit(
    *, pidfd: int, timeout_seconds: float, poll_factory: Callable[[], Any] | None = None
) -> None:
    factory = poll_factory or getattr(select, "poll", None)
    if factory is None:
        raise HarnessError("Linux pidfd polling support is required")
    poller = factory()
    exit_events = (
        getattr(select, "POLLIN", 0x001)
        | getattr(select, "POLLERR", 0x008)
        | getattr(select, "POLLHUP", 0x010)
    )
    poller.register(pidfd, exit_events)
    if not poller.poll(max(1, int(timeout_seconds * 1000))):
        raise HarnessError("QEMU did not exit within the controlled shutdown timeout")


def _resolve_killpg(killpg: Callable[[int, int], None] | None) -> Callable[[int, int], None]:
    if killpg is not None:
        return killpg
    candidate = getattr(os, "killpg", None)
    if candidate is None:
        raise HarnessError("POSIX process-group signaling is required")
    return candidate


def _group_is_gone(
    pgid: int, *, killpg: Callable[[int, int], None] | None = None
) -> bool:
    killpg = _resolve_killpg(killpg)
    try:
        killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError as error:
        raise HarnessError("could not inspect owned QEMU process group") from error
    except OSError as error:
        raise HarnessError(f"could not inspect owned QEMU process group: {error}") from error
    return False


def _wait_for_group_exit(
    *,
    pgid: int,
    timeout_seconds: float,
    killpg: Callable[[int, int], None] | None = None,
    reap_launcher: Callable[[], Any] | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    killpg = _resolve_killpg(killpg)
    deadline = now() + timeout_seconds
    while True:
        if reap_launcher is not None:
            reap_launcher()
        if _group_is_gone(pgid, killpg=killpg):
            return True
        if now() >= deadline:
            return False
        sleep(0.10)


def _cleanup_owned_group(
    *,
    pgid: int,
    timeout_seconds: float,
    killpg: Callable[[int, int], None] | None = None,
    reap_launcher: Callable[[], Any] | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Terminate and prove disappearance of the saved harness-created PGID."""

    killpg = _resolve_killpg(killpg)
    result: dict[str, Any] = {
        "pgid": pgid,
        "signalSent": None,
        "groupExited": False,
        "escalated": False,
    }
    if reap_launcher is not None:
        reap_launcher()
    if _group_is_gone(pgid, killpg=killpg):
        result["groupExited"] = True
        result["signalSent"] = "none-already-exited"
        return result
    try:
        killpg(pgid, signal.SIGTERM)
        result["signalSent"] = "SIGTERM"
    except ProcessLookupError:
        result["groupExited"] = True
        result["signalSent"] = "none-raced-exit"
        return result
    if _wait_for_group_exit(
        pgid=pgid,
        timeout_seconds=timeout_seconds,
        killpg=killpg,
        reap_launcher=reap_launcher,
        now=now,
        sleep=sleep,
    ):
        result["groupExited"] = True
        return result
    try:
        killpg(pgid, SIGKILL)
        result["escalated"] = True
    except ProcessLookupError:
        result["groupExited"] = True
        return result
    if not _wait_for_group_exit(
        pgid=pgid,
        timeout_seconds=timeout_seconds,
        killpg=killpg,
        reap_launcher=reap_launcher,
        now=now,
        sleep=sleep,
    ):
        raise HarnessError("owned QEMU process group survived SIGTERM and SIGKILL")
    result["groupExited"] = True
    return result


def _wait_launcher(launcher: Any, *, timeout_seconds: float) -> int:
    try:
        return int(launcher.wait(timeout=timeout_seconds))
    except subprocess.TimeoutExpired as error:
        raise HarnessError("QEMU launcher outlived the controlled QEMU exit") from error


def _cleanup_runtime_directory(
    *, runtime: Path, qmp_socket: Path, pidfile: Path, group_result: dict[str, Any]
) -> dict[str, Any]:
    """Remove only this attempt's expected runtime paths after group exit."""

    if group_result.get("groupExited") is not True:
        raise HarnessError("refusing runtime cleanup before owned process group exit")
    removed: list[str] = []
    for path, kind in ((qmp_socket, "socket"), (pidfile, "file")):
        if not _path_exists(path):
            continue
        _checked_lstat(path, field=f"owned runtime {path.name}", kind=kind)
        try:
            path.unlink()
        except OSError as error:
            raise HarnessError(f"could not remove owned runtime {path.name}: {error}") from error
        removed.append(path.name)
    try:
        runtime.rmdir()
    except OSError as error:
        raise HarnessError(
            "runtime directory is not empty after owned-path cleanup; unknown files were retained"
        ) from error
    return {"removed": removed, "directoryRemoved": True}


def _status_payload(
    *,
    started_at: str,
    finished_at: str,
    state: dict[str, Any],
    success: bool,
    error: str | None,
) -> dict[str, Any]:
    return {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "artifactStatus": "capture-generated-unreviewed",
        "status": "single_guest_live_capture_passed" if success else "single_guest_live_capture_failed",
        "proofScope": "one-guest-launch-ready-same-session-qmp-capture-resume-identity-exit",
        "doesNotProve": [
            "the unique READY marker was only observed in the launcher-owned combined log and does not prove an AxVisor final-DTB producer emitted it",
            "device-tree semantic validity beyond the inner capture result",
            "a second guest booted",
            "passthrough DMA is isolated",
            "Linux and Zephyr have IP connectivity",
        ],
        "startedAtUtc": started_at,
        "finishedAtUtc": finished_at,
        "success": success,
        "error": error,
        "publication": (
            "published last after confirmed group exit and runtime cleanup"
            if success
            else "published after attempted failure cleanup; no nested capture-chain finality is claimed"
        ),
        "state": state,
    }


def run_harness(
    *,
    axvisor_dir: Path,
    build_config: Path,
    qemu_config: Path,
    vmconfig: Path,
    evidence_directory: Path,
    runtime_directory: Path | None,
    rootfs: Path | None,
    host_dtb: Path | None = None,
    post_resume_marker: str | None = None,
    post_resume_timeout_seconds: float = DEFAULT_POST_RESUME_TIMEOUT_SECONDS,
    cargo_bin: str,
    ready_timeout_seconds: float,
    capture_timeout_seconds: float,
    shutdown_timeout_seconds: float,
    popen_factory: Any = subprocess.Popen,
) -> dict[str, Any]:
    """Execute the one-Guest capture lifecycle and publish outer status last."""

    if os.name != "posix":
        raise HarnessError("live QEMU capture harness requires POSIX/WSL")
    axvisor = _checked_directory(axvisor_dir)
    build = _checked_file(build_config, field="AxVisor build config")
    qemu = _checked_file(qemu_config, field="QEMU config")
    guest = _checked_file(vmconfig, field="single-Guest VM config")
    rootfs_path = None if rootfs is None else _checked_file(rootfs, field="QEMU rootfs")
    host_dtb_path = None if host_dtb is None else _checked_host_dtb(host_dtb)
    post_resume_marker_bytes = _validated_post_resume_marker(post_resume_marker)
    if post_resume_marker_bytes is not None and not (
        0 < post_resume_timeout_seconds <= 86_400
    ):
        raise HarnessError(
            "post-resume timeout must be greater than zero and at most 86400"
        )
    launcher_qemu_args: list[str] | None = None
    directory = _new_evidence_directory(evidence_directory)
    capture_directory = _new_directory(directory, "live-capture")
    log_path = directory / "axvisor-live.log"
    command_path = directory / "launch-command.json"
    status_path = directory / "status.json"
    nonce = secrets.token_hex(16)
    runtime = _new_runtime_directory(
        evidence_directory=directory, runtime_directory=runtime_directory, nonce=nonce
    )
    qmp_socket = _socket_path_within_limit(runtime)
    pidfile = runtime / "qemu.pid"
    qemu_name = f"{QEMU_NAME_PREFIX}{nonce}"
    command = build_qemu_command(
        cargo_bin=cargo_bin,
        build_config=build,
        qemu_config=qemu,
        vmconfig=guest,
        qmp_socket=qmp_socket,
        qemu_pidfile=pidfile,
        qemu_name=qemu_name,
        rootfs=rootfs_path,
    )
    state: dict[str, Any] = {
        "expectedVmId": READY_VM_ID,
        "sessionNonce": nonce,
        "qemuName": qemu_name,
        "paths": {
            "axvisorDirectory": str(axvisor),
            "buildConfig": str(build),
            "qemuConfig": str(qemu),
            "vmconfig": str(guest),
            "rootfs": None if rootfs_path is None else str(rootfs_path),
            "hostDtb": None if host_dtb_path is None else str(host_dtb_path),
            "liveLog": log_path.name,
            "nestedCaptureDirectory": capture_directory.name,
            "qmpSocket": str(qmp_socket),
            "qemuPidfile": str(pidfile),
        },
        "command": command,
    }
    started_at = _utc_now()
    launcher: Any | None = None
    launcher_pgid: int | None = None
    group_result: dict[str, Any] | None = None
    log_handle: Any | None = None
    success = False
    failure: BaseException | None = None
    try:
        state["inputArtifacts"] = {
            "buildConfig": _bound_input_artifact(build, field="AxVisor build config"),
            "qemuConfig": _bound_input_artifact(qemu, field="QEMU config"),
            "vmconfig": _bound_input_artifact(guest, field="single-Guest VM config"),
            "rootfs": (
                None
                if rootfs_path is None
                else _bound_input_artifact(rootfs_path, field="QEMU rootfs")
            ),
            "hostDtb": (
                None
                if host_dtb_path is None
                else _bound_host_dtb(host_dtb_path)
            ),
        }
        if host_dtb_path is not None:
            # Parse only after QEMU config binding, then preserve the exact
            # option vector that the Cargo launcher is required to materialize.
            launcher_qemu_args = _validated_qemu_host_dtb_args(
                qemu, host_dtb=host_dtb_path
            )
            state["launcherQemuArgs"] = launcher_qemu_args
        _publish_json_new(
            {
                "schemaVersion": 1,
                "command": command,
                "launcherQemuArgs": launcher_qemu_args,
            },
            command_path,
        )
        log_handle = log_path.open("xb", buffering=0)
        launcher = popen_factory(
            command,
            cwd=axvisor,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        state["launcherPid"] = int(launcher.pid)
        launcher_pgid = int(launcher.pid)
        state["launcherProcessGroup"] = launcher_pgid
        state["ready"] = wait_for_unique_ready(
            log_path=log_path,
            launcher=launcher,
            timeout_seconds=ready_timeout_seconds,
        )
        if host_dtb_path is not None:
            ready_pid, ready_identity = _bound_running_identity(
                qmp_socket=qmp_socket,
                pidfile=pidfile,
                qemu_name=qemu_name,
                host_dtb=host_dtb_path,
            )
            state["hostDtbReadyIdentity"] = {
                "pid": ready_pid,
                "startTimeTicks": ready_identity["startTimeTicks"],
                "cmdlineSha256": ready_identity["cmdlineSha256"],
                "dtbOption": str(host_dtb_path),
            }
        # The nested helper owns stop/pmemsave/cont and publishes its chain last.
        nested = run_live_capture(
            live_log=log_path,
            evidence_directory=capture_directory,
            expected_vm_ids={READY_VM_ID},
            qmp_socket=qmp_socket,
            qemu_pidfile=pidfile,
            qemu_name=qemu_name,
            timeout_seconds=capture_timeout_seconds,
        )
        state["nestedCaptureChain"] = {
            "path": "live-capture/capture-chain.json",
            "sha256": _sha256_file(capture_directory / "capture-chain.json"),
            "status": nested["chain"]["status"],
        }
        pidfd, pid, post_capture_identity = _open_verified_pidfd(
            qmp_socket=qmp_socket,
            pidfile=pidfile,
            qemu_name=qemu_name,
            host_dtb=host_dtb_path,
        )
        try:
            _verify_nested_resume_continuity(
                nested=nested, current_pid=pid, current_identity=post_capture_identity
            )
            state["postResumeIdentity"] = {
                "pid": pid,
                "startTimeTicks": post_capture_identity["startTimeTicks"],
                "cmdlineSha256": post_capture_identity["cmdlineSha256"],
                "pidfdBound": True,
            }
            if post_resume_marker_bytes is not None:
                state["postResumeMarker"] = wait_for_unique_post_resume_marker(
                    log_path=log_path,
                    launcher=launcher,
                    ready_prefix_bytes=state["ready"]["readyPrefixBytes"],
                    ready_prefix_sha256=state["ready"]["readyPrefixSha256"],
                    marker=post_resume_marker_bytes,
                    timeout_seconds=post_resume_timeout_seconds,
                    process_exited=lambda: _pidfd_exited(pidfd),
                )
                marker_pid, marker_identity = _bound_running_identity(
                    qmp_socket=qmp_socket,
                    pidfile=pidfile,
                    qemu_name=qemu_name,
                    host_dtb=host_dtb_path,
                )
                if marker_pid != pid:
                    raise HarnessError("QEMU pidfile PID changed while sealing post-resume marker")
                _stable_process(
                    post_capture_identity,
                    marker_identity,
                    checkpoint="post-resume marker observation",
                )
                sealed_observation = wait_for_unique_post_resume_marker(
                    log_path=log_path,
                    launcher=launcher,
                    ready_prefix_bytes=state["ready"]["readyPrefixBytes"],
                    ready_prefix_sha256=state["ready"]["readyPrefixSha256"],
                    marker=post_resume_marker_bytes,
                    timeout_seconds=min(post_resume_timeout_seconds, 5.0),
                    process_exited=lambda: _pidfd_exited(pidfd),
                )
                if (
                    sealed_observation["byteOffset"]
                    != state["postResumeMarker"]["byteOffset"]
                ):
                    raise HarnessError(
                        "post-resume marker offset changed before controlled QEMU shutdown"
                    )
                state["postResumeMarker"]["sealedBeforeSigintPrefixBytes"] = (
                    sealed_observation["observedPrefixBytes"]
                )
                state["postResumeMarker"]["sealedBeforeSigintPrefixSha256"] = (
                    sealed_observation["observedPrefixSha256"]
                )
            signal.pidfd_send_signal(pidfd, signal.SIGINT)
            _wait_for_pidfd_exit(
                pidfd=pidfd, timeout_seconds=shutdown_timeout_seconds
            )
        finally:
            os.close(pidfd)
        state["qemuExit"] = "SIGINT sent through pidfd after nested resumed identity revalidation"
        launcher_exit = _wait_launcher(
            launcher, timeout_seconds=shutdown_timeout_seconds
        )
        state["launcherExitCode"] = launcher_exit
        if launcher_exit != 0:
            raise HarnessError(
                f"QEMU launcher exited {launcher_exit} after controlled QEMU shutdown"
            )
        group_result = _cleanup_owned_group(
            pgid=launcher_pgid,
            timeout_seconds=shutdown_timeout_seconds,
            reap_launcher=launcher.poll,
        )
        state["groupCleanup"] = group_result
        state["runtimeCleanup"] = _cleanup_runtime_directory(
            runtime=runtime,
            qmp_socket=qmp_socket,
            pidfile=pidfile,
            group_result=group_result,
        )
        if host_dtb_path is not None:
            _verify_bound_host_dtb(
                host_dtb_path, state["inputArtifacts"]["hostDtb"]
            )
        success = True
    except BaseException as error:  # noqa: BLE001 - package all failure evidence.
        failure = error
        if launcher_pgid is not None:
            try:
                group_result = _cleanup_owned_group(
                    pgid=launcher_pgid,
                    timeout_seconds=shutdown_timeout_seconds,
                    reap_launcher=None if launcher is None else launcher.poll,
                )
                state["groupCleanup"] = group_result
            except Exception as cleanup_error:  # noqa: BLE001 - retain primary failure.
                state["groupCleanup"] = {"error": str(cleanup_error), "groupExited": False}
        else:
            group_result = {
                "pgid": None,
                "signalSent": "none-not-launched",
                "groupExited": True,
                "escalated": False,
            }
            state["groupCleanup"] = group_result
        if group_result is not None and group_result.get("groupExited") is True:
            try:
                state["runtimeCleanup"] = _cleanup_runtime_directory(
                    runtime=runtime,
                    qmp_socket=qmp_socket,
                    pidfile=pidfile,
                    group_result=group_result,
                )
            except Exception as cleanup_error:  # noqa: BLE001 - retain primary failure.
                state["runtimeCleanup"] = {"error": str(cleanup_error), "completed": False}
    finally:
        if log_handle is not None:
            try:
                log_handle.flush()
                os.fsync(log_handle.fileno())
            finally:
                log_handle.close()
    if log_path.exists():
        state["liveLog"] = {"path": log_path.name, "sha256": _sha256_file(log_path)}
    finished_at = _utc_now()
    status = _status_payload(
        started_at=started_at,
        finished_at=finished_at,
        state=state,
        success=success,
        error=None if failure is None else str(failure),
    )
    _publish_json_new(status, status_path)
    if failure is not None:
        raise HarnessError(f"single-Guest live capture failed: {failure}") from failure
    return status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch one AxVisor Guest and seal an identity-bound live Guest-DTB capture"
    )
    parser.add_argument("--axvisor-dir", required=True, type=Path)
    parser.add_argument("--build-config", required=True, type=Path)
    parser.add_argument("--qemu-config", required=True, type=Path)
    parser.add_argument("--vmconfig", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--rootfs", type=Path)
    parser.add_argument("--host-dtb", type=Path)
    parser.add_argument("--post-resume-marker", type=_post_resume_marker)
    parser.add_argument("--post-resume-timeout-seconds", type=_positive_seconds)
    parser.add_argument("--cargo-bin", default="cargo")
    parser.add_argument("--ready-timeout-seconds", type=_positive_seconds, default=1800.0)
    parser.add_argument("--capture-timeout-seconds", type=_positive_seconds, default=10.0)
    parser.add_argument("--shutdown-timeout-seconds", type=_positive_seconds, default=30.0)
    args = parser.parse_args(argv)
    if (
        args.post_resume_marker is None
        and args.post_resume_timeout_seconds is not None
    ):
        parser.error("--post-resume-timeout-seconds requires --post-resume-marker")
    if args.post_resume_timeout_seconds is None:
        args.post_resume_timeout_seconds = DEFAULT_POST_RESUME_TIMEOUT_SECONDS
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        status = run_harness(
            axvisor_dir=args.axvisor_dir,
            build_config=args.build_config,
            qemu_config=args.qemu_config,
            vmconfig=args.vmconfig,
            evidence_directory=args.evidence_dir,
            runtime_directory=args.runtime_dir,
            rootfs=args.rootfs,
            host_dtb=args.host_dtb,
            post_resume_marker=args.post_resume_marker,
            post_resume_timeout_seconds=args.post_resume_timeout_seconds,
            cargo_bin=args.cargo_bin,
            ready_timeout_seconds=args.ready_timeout_seconds,
            capture_timeout_seconds=args.capture_timeout_seconds,
            shutdown_timeout_seconds=args.shutdown_timeout_seconds,
        )
    except (HarnessError, LiveCaptureError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"Single-Guest live capture failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
