#!/usr/bin/env python3
"""Run one fail-closed, bounded virtio-blk effect observation on POSIX/WSL.

This runner deliberately has no QMP guest-memory write path.  It owns one
launcher process group and one QMP connection, makes two stopped windows for
``pmemsave`` only, sends exactly ``GO <nonce>\n`` on a private VirtIO-console
Unix socket, and asks the separate validator to derive the final observation.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import socket
import runpy
import signal
import stat
import struct
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, Callable

from host_vm_carveout_io import publish_new_file, read_regular_bytes
from prepare_virtio_dma_effect_request import (
    PreparedVirtioDmaRequestError,
    _control,
    _device,
    _range,
    _uint,
    build_request,
)
from validate_virtio_dma_effect_probe import VirtioDmaEffectError, validate

SCRIPT_DIR = Path(__file__).resolve().parent
EXECUTOR = runpy.run_path(str(SCRIPT_DIR / "execute_guest_dtb_capture.py"))
UnixQmpSession = EXECUTOR["UnixQmpSession"]
GuestDtbCaptureError = EXECUTOR["GuestDtbCaptureError"]
NONCE = re.compile(r"[0-9a-f]{32}\Z")
READY = re.compile(
    r"^AXVISOR_VIRTIO_BLK_ODIRECT_READY nonce=([0-9a-f]{32}) gpa=(0x[0-9a-f]+) size=512 sector=([0-9]+) device=(/dev/[A-Za-z][A-Za-z0-9_.-]{0,63}) control_device=(/dev/[A-Za-z][A-Za-z0-9_.-]{0,63})$"
)
DONE = re.compile(
    r"^AXVISOR_VIRTIO_BLK_ODIRECT_DONE nonce=([0-9a-f]{32}) bytes=512 gpa=(0x[0-9a-f]+)$"
)
KERNEL_PREREQ_STATUS = "guest_kernel_virtio_console_prerequisites_verified"
KERNEL_REQUIRED_BUILT_INS = (
    "CONFIG_VIRTIO",
    "CONFIG_VIRTIO_MMIO",
    "CONFIG_VIRTIO_CONSOLE",
    "CONFIG_HVC_DRIVER",
    "CONFIG_DEVTMPFS",
    "CONFIG_DEVTMPFS_MOUNT",
)
DTB_READY = re.compile(
    r"^AXVISOR_GUEST_DTB_READY vm=1 gpa=0x[0-9a-f]+ size=[1-9][0-9]* hpa_segments=[^\s]+$"
)


class EffectRunError(ValueError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source(path: Path, data: bytes) -> dict[str, object]:
    return {"path": path.name, "size": len(data), "sha256": _sha(data)}


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise EffectRunError(f"duplicate JSON member {key!r}")
        value[key] = item
    return value


def _json_bytes(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8", "strict"),
            object_pairs_hook=_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EffectRunError(f"{label} is not strict JSON: {error}") from error
    if not isinstance(value, dict):
        raise EffectRunError(f"{label} must be an object")
    return value


def _json(path: Path, label: str) -> dict[str, Any]:
    return _json_bytes(
        read_regular_bytes(path, label=label, error_type=EffectRunError), label
    )


def _kernel_path_from_vmconfig(vmconfig: bytes) -> Path:
    """Resolve the exact memory-backed Guest kernel path from one VM TOML."""

    try:
        config = tomllib.loads(vmconfig.decode("utf-8", "strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise EffectRunError(f"VM config is not strict TOML: {error}") from error
    kernel = config.get("kernel")
    if not isinstance(kernel, dict):
        raise EffectRunError("VM config must contain one [kernel] table")
    if kernel.get("image_location") != "memory":
        raise EffectRunError(
            "VM config kernel.image_location must be exactly 'memory' for kernel binding"
        )
    path_text = kernel.get("kernel_path")
    if not isinstance(path_text, str) or not path_text:
        raise EffectRunError("VM config kernel.kernel_path must be a non-empty string")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in path_text):
        raise EffectRunError(
            "VM config kernel.kernel_path contains a control character"
        )
    path = Path(path_text)
    if not path.is_absolute():
        raise EffectRunError(
            "VM config kernel.kernel_path must be absolute for unambiguous kernel binding"
        )
    return path


def _require_kernel_prerequisites(
    *, manifest_path: Path, vmconfig_path: Path
) -> tuple[bytes, bytes, Path, bytes]:
    """Bind the verifier's duplicate-free manifest to the VM-selected Image."""

    manifest = read_regular_bytes(
        manifest_path, label="kernel prerequisite manifest", error_type=EffectRunError
    )
    value = _json_bytes(manifest, "kernel prerequisite manifest")
    if (
        value.get("schemaVersion") != 1
        or value.get("artifactStatus") != "preflight-only"
        or value.get("status") != KERNEL_PREREQ_STATUS
    ):
        raise EffectRunError(
            "kernel prerequisite manifest schema/status is not the VirtIO-console verifier output"
        )
    kernel_record = value.get("kernel")
    embedded = value.get("embeddedConfig")
    required = embedded.get("requiredBuiltIns") if isinstance(embedded, dict) else None
    if (
        not isinstance(kernel_record, dict)
        or not isinstance(kernel_record.get("path"), str)
        or not kernel_record.get("path")
        or not isinstance(kernel_record.get("size"), int)
        or isinstance(kernel_record.get("size"), bool)
        or kernel_record["size"] < 1
        or not isinstance(kernel_record.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", kernel_record["sha256"]) is None
    ):
        raise EffectRunError("kernel prerequisite manifest kernel record is invalid")
    if not isinstance(required, dict) or required != {
        symbol: "y" for symbol in KERNEL_REQUIRED_BUILT_INS
    }:
        raise EffectRunError(
            "kernel prerequisite manifest requiredBuiltIns is not the exact built-in VirtIO-console set"
        )
    vmconfig = read_regular_bytes(
        vmconfig_path, label="VM config", error_type=EffectRunError
    )
    kernel_path = _kernel_path_from_vmconfig(vmconfig)
    kernel = read_regular_bytes(
        kernel_path, label="VM-selected Guest kernel", error_type=EffectRunError
    )
    if kernel_record["size"] != len(kernel) or kernel_record["sha256"] != _sha(kernel):
        raise EffectRunError(
            "kernel prerequisite manifest does not bind the VM-selected Guest kernel"
        )
    return manifest, vmconfig, kernel_path, kernel


def _require_plan_inputs(
    *,
    rootfs_plan: Path,
    qemu_plan: Path,
    rootfs: Path,
    qemu: Path,
    expected: Path,
    host_dtb: Path,
    nonce: str,
) -> dict[str, Any]:
    root = _json(rootfs_plan, "rootfs plan")
    qplan = _json(qemu_plan, "QEMU plan")
    if (
        root.get("schemaVersion") != 1
        or root.get("status") != "disposable_virtio_dma_effect_guest_rootfs_prepared"
    ):
        raise EffectRunError(
            "rootfs plan schema/status is not the disposable DMA-effect preparation"
        )
    if (
        qplan.get("schemaVersion") != 1
        or qplan.get("status") != "single_guest_virtio_blk_dma_effect_qemu_prepared"
    ):
        raise EffectRunError(
            "QEMU plan schema/status is not the prepared DMA-effect topology"
        )
    if root.get("sessionNonce") != nonce or qplan.get("sessionNonce") != nonce:
        raise EffectRunError("--nonce must exactly match both preparation plans")
    boot = root.get("bootContract")
    probe = (
        qplan.get("qemu", {}).get("probe")
        if isinstance(qplan.get("qemu"), dict)
        else None
    )
    if (
        not isinstance(boot, dict)
        or boot.get("device") != "/dev/vdb"
        or boot.get("controlDevice") != "/dev/hvc0"
        or boot.get("sector") != 0
    ):
        raise EffectRunError("rootfs plan must bind helper to /dev/vdb and /dev/hvc0")
    if (
        not isinstance(probe, dict)
        or probe.get("guestDevice") != "/dev/vdb"
        or probe.get("sector") != 0
    ):
        raise EffectRunError("QEMU plan must bind probe to /dev/vdb sector 0")
    outputs = qplan.get("outputs")
    inputs = qplan.get("inputs")
    if not isinstance(outputs, dict) or not isinstance(inputs, dict):
        raise EffectRunError("QEMU plan inputs/outputs missing")
    for plan_item, path, label in (
        (outputs.get("qemuConfig"), qemu, "QEMU config"),
        (outputs.get("probeDisk"), expected, "expected sector"),
        (inputs.get("hostDtb"), host_dtb, "host DTB"),
    ):
        data = read_regular_bytes(path, label=label, error_type=EffectRunError)
        if (
            not isinstance(plan_item, dict)
            or plan_item.get("sha256") != _sha(data)
            or plan_item.get("size") != len(data)
        ):
            raise EffectRunError(f"{label} disagrees with QEMU plan")
    root_out = root.get("outputRootfs")
    root_bytes = read_regular_bytes(rootfs, label="rootfs", error_type=EffectRunError)
    if (
        not isinstance(root_out, dict)
        or root_out.get("sha256") != _sha(root_bytes)
        or root_out.get("size") != len(root_bytes)
    ):
        raise EffectRunError("rootfs disagrees with rootfs plan")
    qemu_root = inputs.get("disposableRootfs")
    if (
        not isinstance(qemu_root, dict)
        or qemu_root.get("sha256") != _sha(root_bytes)
        or qemu_root.get("size") != len(root_bytes)
    ):
        raise EffectRunError("QEMU plan disposableRootfs does not bind actual rootfs")
    return probe


def _marker(
    data: bytes, pattern: re.Pattern[str], *, label: str
) -> tuple[dict[str, object], tuple[str, ...]]:
    found: list[tuple[int, int, tuple[str, ...]]] = []
    offset = 0
    for line_no, raw in enumerate(data.splitlines(keepends=True), 1):
        line = raw.rstrip(b"\r\n")
        try:
            text = line.decode("ascii", "strict")
        except UnicodeDecodeError:
            text = ""
        match = pattern.fullmatch(text)
        if match:
            found.append((line_no, offset, match.groups()))
        offset += len(raw)
    if len(found) > 1:
        raise EffectRunError(f"raw log contains duplicate {label} markers")
    if not found:
        raise EffectRunError(f"raw log has no {label} marker yet")
    line, offset, groups = found[0]
    text = pattern.pattern  # avoid unbounded log fields in evidence
    del text
    return {"line": line, "byteOffset": offset}, groups


def _wait_log(
    log: Path,
    launcher: Any,
    *,
    want_ready: bool,
    timeout: float,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    deadline = now() + timeout
    while True:
        try:
            data = log.read_bytes() if log.exists() else b""
        except OSError as error:
            if error.errno != getattr(errno, "ENODATA", 61):
                raise EffectRunError(
                    f"active-log read failed errno={error.errno}: {error}"
                ) from error
            if launcher.poll() is not None:
                raise EffectRunError(
                    "active-log ENODATA after launcher exit"
                ) from error
            if now() >= deadline:
                raise EffectRunError(
                    "active-log ENODATA timed out while launcher remained active"
                ) from error
            sleep(0.05)
            continue
        try:
            record, groups = _marker(
                data,
                READY if want_ready else DONE,
                label="READY" if want_ready else "DONE",
            )
            del record, groups
            if want_ready:
                dtb_ready = sum(
                    DTB_READY.fullmatch(line.decode("ascii", "ignore").rstrip("\r\n"))
                    is not None
                    for line in data.splitlines(keepends=True)
                )
                if dtb_ready > 1:
                    raise EffectRunError(
                        "raw log contains duplicate Guest-DTB READY markers"
                    )
                if dtb_ready == 0:
                    raise EffectRunError("raw log has no Guest-DTB READY marker yet")
            return data
        except EffectRunError as error:
            if "duplicate" in str(error):
                raise
            if launcher.poll() is not None:
                raise EffectRunError(
                    "launcher exited before required unique Guest marker"
                )
            if now() >= deadline:
                raise EffectRunError(
                    "timed out waiting for required unique Guest marker"
                )
            sleep(0.05)


def _freeze_observed_prefix(
    *, output: Path, raw_log: bytes, done_marker: str
) -> tuple[Path, bytes]:
    """Seal bytes through the one newline-complete DONE line before shutdown."""
    observation, _ = _marker(raw_log, DONE, label="DONE")
    start = int(observation["byteOffset"])
    end = start + len(done_marker.encode("ascii"))
    if raw_log[end : end + 2] == b"\r\n":
        end += 2
    elif raw_log[end : end + 1] == b"\n":
        end += 1
    else:
        raise EffectRunError("DONE marker is not newline-complete")
    prefix = raw_log[:end]
    path = output / "observed-log-prefix.bin"
    publish_new_file(path, prefix, error_type=EffectRunError)
    return path, prefix


def _qmp_status(session: Any, ident: str) -> str:
    reply = session.execute_control("query-status", request_id=ident)
    value = reply.get("return") if isinstance(reply, dict) else None
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        raise EffectRunError("QMP query-status response malformed")
    return value["status"]


def _qmp_name(session: Any) -> str:
    reply = session.execute_control("query-name", request_id="dma-query-name")
    value = reply.get("return") if isinstance(reply, dict) else None
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise EffectRunError("QMP query-name response malformed")
    return value["name"]


def _send_virtio_console_go(
    *, socket_path: Path, command: bytes, pid: int, uid: int, timeout: float
) -> tuple[dict[str, int], socket.socket]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            info = socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode):
                raise EffectRunError("virtio-console control path is not a socket")
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connection.settimeout(min(0.5, timeout))
                connection.connect(str(socket_path))
                raw = connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                )
                peer = struct.unpack("3i", raw)
                if peer[:2] != (pid, uid):
                    raise EffectRunError(
                        "virtio-console SO_PEERCRED does not match QEMU peer"
                    )
                connection.sendall(command)
                return {"pid": peer[0], "uid": peer[1]}, connection
            except Exception:
                connection.close()
                raise
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise EffectRunError(
                    "timed out waiting for virtio-console control socket"
                )
            time.sleep(0.05)


def _qmp(session: Any, command: str, arguments: dict[str, Any], ident: str) -> None:
    if command not in {"stop", "cont", "pmemsave"}:
        raise EffectRunError("forbidden QMP command")
    if command == "pmemsave":
        reply = session.execute(
            {"execute": command, "arguments": arguments, "id": ident}
        )
    else:
        if arguments:
            raise EffectRunError("QMP control commands must not take arguments")
        reply = session.execute_control(command, request_id=ident)
    if reply != {"return": {}, "id": ident}:
        raise EffectRunError(
            f"QMP {command} did not return the required id-bound empty success response"
        )


def _wait_qmp_status(
    session: Any,
    expected: str,
    *,
    label: str,
    attempts: int = 20,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    for attempt in range(attempts):
        if _qmp_status(session, f"{label}-{attempt:02d}") == expected:
            return
        sleep(0.05)
    raise EffectRunError(f"QMP did not reach {expected} at {label}")


def _cleanup_group(
    *,
    pgid: int,
    launcher: Any,
    pidfd: int | None,
    timeout: float = 15.0,
    proc_root: Path = Path("/proc"),
    killpg: Callable[[int, int], None] | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Bounded SIGINT/SIGTERM/SIGKILL cleanup for only this launch PGID."""
    if killpg is None:
        candidate = getattr(os, "killpg", None)
        if candidate is None:
            raise EffectRunError("POSIX process-group signaling is required")
        killpg = candidate
    signals: list[str] = []
    started = now()
    if launcher.poll() is None:
        if pidfd is not None:
            signal.pidfd_send_signal(pidfd, signal.SIGINT, None, 0)
        else:
            killpg(pgid, signal.SIGINT)
        signals.append("SIGINT")

    def members() -> tuple[list[int], list[int]]:
        live: list[int] = []
        zombies: list[int] = []
        try:
            entries = list(proc_root.iterdir())
        except OSError as error:
            raise EffectRunError(
                "could not enumerate /proc for owned PGID cleanup"
            ) from error
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                tail = (
                    (entry / "stat")
                    .read_text("ascii", errors="strict")
                    .rsplit(")", 1)[1]
                    .split()
                )
                state, process_group = tail[0], int(tail[2])
            except (OSError, IndexError, ValueError):
                continue
            if process_group != pgid:
                continue
            (zombies if state == "Z" else live).append(int(entry.name))
        return sorted(live), sorted(zombies)

    def group_state() -> tuple[bool, bool, list[int]]:
        live, zombies = members()
        if not live:
            try:
                killpg(pgid, 0)
            except ProcessLookupError:
                return True, True, zombies
            # Zombie-only groups can remain addressable until their parent
            # reaps them.  They cannot execute, but are recorded separately
            # instead of being mislabeled as an absent process group.
            return False, True, zombies
        try:
            killpg(pgid, 0)
        except ProcessLookupError:
            # A live /proc member with missing PGID is inconsistent; fail closed.
            raise EffectRunError(
                "owned PGID has live /proc members but killpg reports absent"
            )
        return False, False, zombies

    def cleanup_evidence(
        *, group_gone: bool, no_live_members: bool, zombies: list[int], polls: int = 0
    ) -> dict[str, object]:
        return {
            "pgid": pgid,
            "signals": signals,
            "launcherExitCode": launcher.poll(),
            "groupGone": group_gone,
            "noLiveMembers": no_live_members,
            "zombiePids": zombies,
            "reapPolls": polls,
            "reapElapsedSeconds": now() - started,
        }

    for signo, label in (
        (None, "wait"),
        (signal.SIGTERM, "SIGTERM"),
        (getattr(signal, "SIGKILL", 9), "SIGKILL"),
    ):
        try:
            launcher.wait(timeout=timeout if signo is None else 3.0)
            gone, no_live, zombies = group_state()
            if no_live:
                return cleanup_evidence(
                    group_gone=gone,
                    no_live_members=no_live,
                    zombies=zombies,
                )
        except subprocess.TimeoutExpired:
            pass
        if signo is not None:
            try:
                killpg(pgid, signo)
                signals.append(label)
            except ProcessLookupError:
                gone, no_live, zombies = group_state()
                if not no_live:
                    raise EffectRunError(
                        "owned PGID signal raced with a live group member"
                    )
                return cleanup_evidence(
                    group_gone=gone,
                    no_live_members=no_live,
                    zombies=zombies,
                )
            try:
                launcher.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                continue
            gone, no_live, zombies = group_state()
            if no_live:
                return cleanup_evidence(
                    group_gone=gone,
                    no_live_members=no_live,
                    zombies=zombies,
                )
    # WSL can reap the post-SIGKILL process group after the launcher itself
    # already reports an exit.  Poll /proc rather than treating one snapshot as
    # conclusive; only non-zombie members keep the group live.
    poll_deadline = now() + timeout
    polls = 0
    while True:
        gone, no_live, zombies = group_state()
        if no_live:
            return cleanup_evidence(
                group_gone=gone, no_live_members=True, zombies=zombies, polls=polls
            )
        if now() >= poll_deadline:
            raise EffectRunError(
                "owned QEMU launcher/process group retained live members after SIGKILL reap polling"
            )
        polls += 1
        sleep(0.05)


def _verify_qmp_peer(
    *,
    session: Any,
    pidfile: Path,
    qmp: Path,
    name: str,
    host_dtb: Path,
    launcher_pgid: int,
    control: dict[str, str],
) -> tuple[int, bytes]:
    """Bind the sole QMP connection to its pidfile and live QEMU argv."""
    try:
        pid = int(pidfile.read_text("ascii", errors="strict").strip())
    except (OSError, ValueError) as error:
        raise EffectRunError("QEMU pidfile is malformed") from error
    peer = session.peer_credentials()
    if (
        not isinstance(peer, tuple)
        or len(peer) != 3
        or peer[0] != pid
        or peer[1] != os.geteuid()
    ):
        raise EffectRunError(
            "QMP SO_PEERCRED PID/effective UID disagrees with pidfile/process"
        )
    try:
        if os.getpgid(pid) != launcher_pgid:
            raise EffectRunError("QMP peer is outside the owned launcher process group")
    except ProcessLookupError as error:
        raise EffectRunError("QMP peer disappeared during identity binding") from error
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1]
    except OSError as error:
        raise EffectRunError("could not read QEMU argv for QMP identity") from error
    expected = (
        (b"-qmp", f"unix:{qmp},server=on,wait=off".encode()),
        (b"-pidfile", str(pidfile).encode()),
        (b"-name", name.encode()),
        (b"-dtb", str(host_dtb).encode()),
    )
    for option, value in expected:
        indexes = [index for index, argument in enumerate(argv) if argument == option]
        if (
            len(indexes) != 1
            or indexes[0] + 1 >= len(argv)
            or argv[indexes[0] + 1] != value
        ):
            raise EffectRunError("QEMU argv disagrees with bound QMP/pidfile/name")
    control_pairs = (
        (
            b"-chardev",
            (
                f"socket,id={control['chardevId']},path={control['socketPath']},"
                "server=on,wait=off"
            ).encode(),
        ),
        (
            b"-device",
            (
                f"virtio-serial-device,id={control['serialDeviceId']},"
                f"bus={control['serialBus']}"
            ).encode(),
        ),
        (
            b"-device",
            (
                f"virtconsole,id={control['portId']},chardev={control['chardevId']},"
                f"name={control['portName']}"
            ).encode(),
        ),
    )
    for option, value in control_pairs:
        if (
            sum(
                argv[index] == option
                and index + 1 < len(argv)
                and argv[index + 1] == value
                for index in range(len(argv))
            )
            != 1
        ):
            raise EffectRunError(
                "QEMU argv does not contain the exact bound virtio-console topology"
            )
    return pid, b"\0".join(argv)


def execute_effect_session(
    *,
    session: Any,
    launcher: Any,
    log_path: Path,
    request: dict[str, Any],
    output: Path,
    timeout: float,
    wait_log: Callable[..., bytes] = _wait_log,
    control_sender: Callable[..., dict[str, int]] = _send_virtio_console_go,
) -> dict[str, Any]:
    """The injectable core; tests supply fake QMP/launcher and no QEMU."""
    nonce = str(request["sessionNonce"])
    name = str(request["qemuName"])
    markers = request["guestMarkers"]
    template = request["qmpCaptureTemplate"]["measurements"]
    session.negotiate()
    qmp_peer = session.peer_credentials()
    if _qmp_name(session) != name:
        raise EffectRunError("QMP query-name disagrees with request")
    _wait_qmp_status(session, "running", label="status-running-0")
    raw_before = wait_log(log_path, launcher, want_ready=True, timeout=timeout)
    _qmp(session, "stop", {}, "stop-before")
    _wait_qmp_status(session, "paused", label="status-paused-1")
    names = []
    for index in (0, 1):
        item = template[index]
        path = output / str(item["outputFileName"])
        names.append(path)
        _qmp(
            session,
            "pmemsave",
            {"val": item["hpa"], "size": item["size"], "filename": str(path)},
            f"capture-{index}",
        )
    _qmp(session, "cont", {}, "cont-before")
    _wait_qmp_status(session, "running", label="status-running-2")
    command = f"GO {nonce}\n".encode("ascii")
    peer, control_connection = control_sender(
        socket_path=Path(request["control"]["socketPath"]),
        command=command,
        pid=qmp_peer[0],
        uid=qmp_peer[1],
        timeout=timeout,
    )
    try:
        wait_log(log_path, launcher, want_ready=False, timeout=timeout)
    finally:
        # The helper performs its exact-message/no-trailing-data check before
        # pread and DONE.  Keep the channel live through DONE, then close it.
        control_connection.close()
    _qmp(session, "stop", {}, "stop-after")
    _wait_qmp_status(session, "paused", label="status-paused-3")
    # Re-read only after QEMU is paused: a second DONE appended during the
    # stop transition is evidence corruption, not a timeout condition.
    paused_log = read_regular_bytes(
        log_path, label="paused combined launcher log", error_type=EffectRunError
    )
    _marker(paused_log, READY, label="READY")
    dtb_ready_count = sum(
        DTB_READY.fullmatch(line.decode("ascii", "ignore").rstrip("\r\n")) is not None
        for line in paused_log.splitlines(keepends=True)
    )
    if dtb_ready_count != 1:
        raise EffectRunError(
            "paused raw log must contain exactly one Guest-DTB READY marker"
        )
    frozen_log, raw = _freeze_observed_prefix(
        output=output, raw_log=paused_log, done_marker=markers["done"]
    )
    ready_obs, ready_groups = _marker(raw, READY, label="READY")
    done_obs, done_groups = _marker(raw, DONE, label="DONE")
    if ready_groups != (
        nonce,
        f"{request['request']['payload']['gpa']:#x}",
        str(request["request"]["sector"]),
        request["device"]["guestPath"],
        request["control"]["guestPath"],
    ):
        raise EffectRunError("READY marker disagrees with request")
    if done_groups != (nonce, f"{request['request']['payload']['gpa']:#x}"):
        raise EffectRunError("DONE marker disagrees with request")
    ready_obs["marker"] = markers["ready"]
    done_obs["marker"] = markers["done"]
    for index in (2, 3):
        item = template[index]
        path = output / str(item["outputFileName"])
        names.append(path)
        _qmp(
            session,
            "pmemsave",
            {"val": item["hpa"], "size": item["size"], "filename": str(path)},
            f"capture-{index}",
        )
    _qmp(session, "cont", {}, "cont-after")
    _wait_qmp_status(session, "running", label="status-running-4")
    for item, path in zip(template, names):
        data = read_regular_bytes(path, label=path.name, error_type=EffectRunError)
        if len(data) != item["size"]:
            raise EffectRunError(f"{path.name} has wrong capture size")
    session_value = {
        "schemaVersion": 1,
        "artifactStatus": "capture-generated-unreviewed",
        "status": "identity_bound_single_virtio_blk_dma_effect_session_completed",
        "proofScope": "same-session-one-virtio-blk-read-paused-qmp-pmemsave-measurements",
        "sessionNonce": nonce,
        "qemuName": name,
        "device": request["device"],
        "request": _source(
            output / "request.json", (output / "request.json").read_bytes()
        ),
        "rawLog": _source(frozen_log, raw),
        "markerObservations": {"ready": ready_obs, "done": done_obs},
        "guestControl": {
            "channel": "virtio-console-unix-socket",
            "control": request["control"],
            "command": command.decode(),
            "bytes": len(command),
            "sha256": _sha(command),
            "peer": peer,
        },
        "mapping": {
            "payloadGpa": request["request"]["payload"]["gpa"],
            "payloadHpa": request["request"]["payload"]["hpa"],
            "payloadSize": request["request"]["payload"]["size"],
            "mapType": 2,
            "identity": True,
            "boundMapReservedRegion": request["request"]["boundMapReservedRegion"],
        },
        "qmp": {
            "capabilitiesNegotiated": True,
            "operations": ["query-name", "query-status", "stop", "cont", "pmemsave"],
            "queryName": name,
            "states": ["running", "paused", "running", "paused", "running"],
            "peerPid": qmp_peer[0],
            "peerUid": qmp_peer[1],
        },
        "timeline": [
            {"event": "qmp-state", "state": "running"},
            {"event": "guest-ready-marker", "marker": "ready"},
            {"event": "qmp-state", "state": "paused"},
            {
                "event": "capture-window",
                "phase": "before",
                "measurementIndexes": [0, 1],
            },
            {"event": "qmp-state", "state": "running"},
            {
                "event": "guest-control",
                "channel": "virtio-console-unix-socket",
                "commandSha256": _sha(command),
            },
            {"event": "guest-done-marker", "marker": "done"},
            {"event": "qmp-state", "state": "paused"},
            {"event": "capture-window", "phase": "after", "measurementIndexes": [2, 3]},
            {"event": "qmp-state", "state": "running"},
        ],
        "measurements": [
            {
                "phase": item["phase"],
                "target": item["target"],
                "operation": "pmemsave",
                "addressSpace": "host-physical",
                "hpa": item["hpa"],
                "size": item["size"],
                "source": _source(path, path.read_bytes()),
            }
            for item, path in zip(template, names)
        ],
    }
    return session_value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run one bounded POSIX/WSL virtio DMA-effect probe"
    )
    for name in (
        "repository",
        "axvisor_dir",
        "build_config",
        "qemu_config",
        "vmconfig",
        "rootfs",
        "rootfs_plan",
        "qemu_plan",
        "expected_sector",
        "host_dtb",
        "kernel_prereq_manifest",
        "evidence_dir",
    ):
        p.add_argument("--" + name.replace("_", "-"), required=True, type=Path)
    p.add_argument("--guard-hpa", required=True)
    p.add_argument("--guard-size", required=True)
    p.add_argument("--nonce", required=True)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--cargo-bin", default="cargo")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    try:
        success = False
        cleanup_complete = False
        if sys.platform != "linux":
            raise EffectRunError("runner requires POSIX/WSL Linux")
        if NONCE.fullmatch(a.nonce) is None or not 0 < a.timeout <= 86400:
            raise EffectRunError("invalid nonce or timeout")
        if a.evidence_dir.exists():
            raise EffectRunError("--evidence-dir must not already exist")
        probe = _require_plan_inputs(
            rootfs_plan=a.rootfs_plan,
            qemu_plan=a.qemu_plan,
            rootfs=a.rootfs,
            qemu=a.qemu_config,
            expected=a.expected_sector,
            host_dtb=a.host_dtb,
            nonce=a.nonce,
        )
        kernel_manifest, vmconfig_bytes, guest_kernel_path, guest_kernel = (
            _require_kernel_prerequisites(
                manifest_path=a.kernel_prereq_manifest, vmconfig_path=a.vmconfig
            )
        )
        # Bind immutable launch inputs before QEMU starts.  The rootfs is
        # intentionally excluded from that immutability claim: it is a rw
        # guest disk and gets separate pre/post records in final status.
        launch_inputs = {
            "rootfsPlan": (
                a.rootfs_plan,
                read_regular_bytes(
                    a.rootfs_plan, label="rootfs plan", error_type=EffectRunError
                ),
            ),
            "qemuPlan": (
                a.qemu_plan,
                read_regular_bytes(
                    a.qemu_plan, label="QEMU plan", error_type=EffectRunError
                ),
            ),
            "qemuConfig": (
                a.qemu_config,
                read_regular_bytes(
                    a.qemu_config, label="QEMU config", error_type=EffectRunError
                ),
            ),
            "buildConfig": (
                a.build_config,
                read_regular_bytes(
                    a.build_config, label="build config", error_type=EffectRunError
                ),
            ),
            "vmConfig": (
                a.vmconfig,
                vmconfig_bytes,
            ),
            "kernelPrerequisiteManifest": (
                a.kernel_prereq_manifest,
                kernel_manifest,
            ),
            "guestKernel": (
                guest_kernel_path,
                guest_kernel,
            ),
            "hostDtb": (
                a.host_dtb,
                read_regular_bytes(
                    a.host_dtb, label="host DTB", error_type=EffectRunError
                ),
            ),
            "expectedSector": (
                a.expected_sector,
                read_regular_bytes(
                    a.expected_sector,
                    label="expected sector",
                    error_type=EffectRunError,
                ),
            ),
        }
        prelaunch_rootfs = read_regular_bytes(
            a.rootfs, label="rootfs before launch", error_type=EffectRunError
        )
        a.evidence_dir.mkdir(mode=0o700)
        raw = a.evidence_dir / "axvisor.log"
        request_path = a.evidence_dir / "request.json"
        session_path = a.evidence_dir / "session.json"
        result_path = a.evidence_dir / "result.json"
        runtime = Path("/tmp") / ("axdma-" + a.nonce)
        runtime.mkdir(mode=0o700)
        qmp = runtime / "qmp.sock"
        pidfile = runtime / "qemu.pid"
        expected = launch_inputs["expectedSector"][1]
        qemu_bytes = launch_inputs["qemuConfig"][1]
        build_bytes = launch_inputs["buildConfig"][1]
        vm_bytes = launch_inputs["vmConfig"][1]
        # Runtime GPA arrives only in the exact READY marker, so request publication is deferred until it is bound.
        cmd = [
            a.cargo_bin,
            "xtask",
            "qemu",
            "--config",
            str(a.build_config),
            "--qemu-config",
            str(a.qemu_config),
            "--vmconfigs",
            str(a.vmconfig),
            "--qmp-socket",
            str(qmp),
            "--qemu-pidfile",
            str(pidfile),
            "--qemu-name",
            f"axvisor-dma-effect-{a.nonce}",
            "--rootfs",
            str(a.rootfs),
        ]
        launcher = None
        try:
            with raw.open("xb") as log:
                launcher = subprocess.Popen(
                    cmd,
                    cwd=a.axvisor_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception:
            # Popen/raw-log creation happens after our private runtime mkdir.
            # No process exists on this path, so remove only that empty owned dir.
            runtime.rmdir()
            raise
        primary_error: BaseException | None = None
        try:
            initial = _wait_log(raw, launcher, want_ready=True, timeout=a.timeout)
            _, groups = _marker(initial, READY, label="READY")
            marker_nonce, gpa_text, sector, guest, control_guest = groups
            if marker_nonce != a.nonce:
                raise EffectRunError("READY nonce disagrees with --nonce")
            device = _device(
                device_id=str(probe["deviceId"]),
                bus=str(probe["bus"]),
                guest_path=guest,
                drive_id=str(probe["driveId"]),
            )
            if control_guest != "/dev/hvc0":
                raise EffectRunError("READY control_device is not /dev/hvc0")
            request = build_request(
                repository=a.repository,
                qemu_config=(a.qemu_config, qemu_bytes),
                build_config=(a.build_config, build_bytes),
                vm_config=(a.vmconfig, vm_bytes),
                expected_payload=(a.expected_sector, expected),
                device=device,
                control=_control(
                    nonce=a.nonce, socket_path=str(runtime / "control.sock")
                ),
                sector=int(sector),
                payload=_range(hpa=int(gpa_text, 0), size=512, label="payload"),
                guard=_range(
                    hpa=_uint(a.guard_hpa, label="--guard-hpa", positive=True),
                    size=_uint(a.guard_size, label="--guard-size", positive=True),
                    label="guard",
                ),
                nonce=a.nonce,
            )
            publish_new_file(
                request_path,
                (json.dumps(request, indent=2) + "\n").encode(),
                error_type=EffectRunError,
            )
            with UnixQmpSession(qmp, timeout_seconds=a.timeout) as qsession:
                pid, identity = _verify_qmp_peer(
                    session=qsession,
                    pidfile=pidfile,
                    qmp=qmp,
                    name=request["qemuName"],
                    host_dtb=a.host_dtb,
                    launcher_pgid=launcher.pid,
                    control=request["control"],
                )
                if not hasattr(os, "pidfd_open") or not hasattr(
                    signal, "pidfd_send_signal"
                ):
                    raise EffectRunError("Linux pidfd signaling is required")
                pidfd = os.pidfd_open(pid, 0)
                completed = execute_effect_session(
                    session=qsession,
                    launcher=launcher,
                    log_path=raw,
                    request=request,
                    output=a.evidence_dir,
                    timeout=a.timeout,
                )
                if Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0") != identity:
                    raise EffectRunError("QEMU identity changed during effect session")
            session_bytes = (json.dumps(completed, indent=2) + "\n").encode()
            frozen = a.evidence_dir / "observed-log-prefix.bin"
            result = validate(
                request_path=request_path,
                request_bytes=request_path.read_bytes(),
                session_path=session_path,
                session_bytes=session_bytes,
                expected_payload_path=a.expected_sector,
                expected_payload=expected,
                before_guard=(
                    a.evidence_dir / "before-guard.bin",
                    (a.evidence_dir / "before-guard.bin").read_bytes(),
                ),
                after_guard=(
                    a.evidence_dir / "after-guard.bin",
                    (a.evidence_dir / "after-guard.bin").read_bytes(),
                ),
                raw_log=(frozen, frozen.read_bytes()),
                before_payload=(
                    a.evidence_dir / "before-payload.bin",
                    (a.evidence_dir / "before-payload.bin").read_bytes(),
                ),
                after_payload=(
                    a.evidence_dir / "after-payload.bin",
                    (a.evidence_dir / "after-payload.bin").read_bytes(),
                ),
            )
            publish_new_file(session_path, session_bytes, error_type=EffectRunError)
            publish_new_file(
                result_path,
                (json.dumps(result, indent=2) + "\n").encode(),
                error_type=EffectRunError,
            )
            success = True
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                cleanup = _cleanup_group(
                    pgid=launcher.pid,
                    launcher=launcher,
                    pidfd=pidfd if "pidfd" in locals() else None,
                )
                for path in (qmp, pidfile):
                    if path.exists():
                        path.unlink()
                runtime.rmdir()
                cleanup_complete = True
            except BaseException as cleanup_error:
                if primary_error is not None:
                    raise EffectRunError(
                        f"primary failure: {primary_error}; cleanup failure: {cleanup_error}"
                    ) from primary_error
                raise
            finally:
                if "pidfd" in locals():
                    os.close(pidfd)
        if not success:
            raise EffectRunError("runner reached cleanup without a validated result")
        for label, (path, launch_bytes) in launch_inputs.items():
            current = read_regular_bytes(
                path, label=f"{label} after run", error_type=EffectRunError
            )
            if current != launch_bytes:
                raise EffectRunError(
                    f"immutable launch input changed during run: {label}"
                )
        publish_new_file(
            a.evidence_dir / "status.json",
            (
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "artifactStatus": "run-complete",
                        "status": "virtio_dma_effect_probe_completed",
                        "success": True,
                        "publication": "published last after identity-bound pidfd shutdown and runtime cleanup",
                        "cleanup": cleanup,
                        "inputArtifacts": {
                            name: _source(path, data)
                            for name, (path, data) in launch_inputs.items()
                        },
                        "preLaunchRootfs": _source(a.rootfs, prelaunch_rootfs),
                        "postRunMutableRootfs": _source(
                            a.rootfs,
                            read_regular_bytes(
                                a.rootfs,
                                label="rootfs after run",
                                error_type=EffectRunError,
                            ),
                        ),
                        "outputs": {
                            name: _source(
                                a.evidence_dir / name,
                                (a.evidence_dir / name).read_bytes(),
                            )
                            for name in (
                                "request.json",
                                "session.json",
                                "result.json",
                                "before-guard.bin",
                                "before-payload.bin",
                                "after-guard.bin",
                                "after-payload.bin",
                                "observed-log-prefix.bin",
                                "axvisor.log",
                            )
                        },
                    },
                    indent=2,
                )
                + "\n"
            ).encode(),
            error_type=EffectRunError,
        )
        return 0
    except (
        OSError,
        EffectRunError,
        PreparedVirtioDmaRequestError,
        VirtioDmaEffectError,
        GuestDtbCaptureError,
        subprocess.SubprocessError,
    ) as e:
        # Failure is recorded last when the evidence directory was safely created;
        # no successful session/result is synthesized by this path.
        try:
            # A validator/cleanup failure is not a completed observation: remove
            # only our own unpublished-success candidates before status-last.
            for name in ("session.json", "result.json"):
                path = a.evidence_dir / name
                if path.is_file():
                    path.unlink()
            if (
                a.evidence_dir.is_dir()
                and not (a.evidence_dir / "status.json").exists()
            ):
                publish_new_file(
                    a.evidence_dir / "status.json",
                    (
                        json.dumps(
                            {
                                "schemaVersion": 1,
                                "artifactStatus": "failed-attempt",
                                "status": "virtio_dma_effect_probe_failed",
                                "success": False,
                                "error": str(e),
                                "publication": (
                                    "published last after confirmed failure cleanup; no successful session/result is claimed"
                                    if cleanup_complete
                                    else "cleanup did not complete; this status does not claim final publication after cleanup"
                                ),
                            },
                            indent=2,
                        )
                        + "\n"
                    ).encode(),
                    error_type=EffectRunError,
                )
        except Exception:
            pass
        print(f"virtio DMA-effect runner failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
