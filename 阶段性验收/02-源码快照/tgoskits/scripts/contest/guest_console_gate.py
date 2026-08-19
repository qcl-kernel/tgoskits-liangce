#!/usr/bin/env python3
"""Bounded live gate for VM-1 framed guest-console READY markers.

This module is the ``linux-smp2-v1`` framed-console gate used by the
single-Guest live-capture harness.  It consumes only the public incremental
frame parser from :mod:`demux_guest_console_frames` and never mutates shared
demux state: it requires the three boot-id-bound VM-1 markers in order, after
an immutable READY prefix, with contiguous per-generation frame sequences and
zero dropped/DMA counters.
"""

from __future__ import annotations

import errno
import hashlib
import json
import re
import runpy
import time
from pathlib import Path
from typing import Any, Callable

_DEMUX_API = runpy.run_path(
    str(Path(__file__).resolve().parent / "demux_guest_console_frames.py")
)
GuestConsoleDemuxError = _DEMUX_API["GuestConsoleDemuxError"]
iter_host_log_frames = _DEMUX_API["iter_host_log_frames"]


GUEST_CONSOLE_PROFILE = "linux-smp2-v1"
GUEST_CONSOLE_VM_ID = 1
GUEST_CONSOLE_NAME = "linux"
GUEST_CONSOLE_MARKERS = (
    b"AXVISOR_LINUX_INIT_ENTER",
    b"AXVISOR_LINUX_DEV_CONSOLE_READY",
    b"AXVISOR_DUAL_GUEST_LINUX_READY",
)
GUEST_CONSOLE_FAIL_MARKER = b"AXVISOR_LINUX_CONSOLE_FAIL"
BOOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")
MAX_GUEST_CONSOLE_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_SOURCE_LOG_BYTES = 512 * 1024 * 1024
DEFAULT_GUEST_CONSOLE_READY_TIMEOUT_SECONDS = 300.0


class GuestConsoleGateError(ValueError):
    """The framed VM-1 console gate cannot establish or keep its contract."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_log_snapshot(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as error:
        transient = {errno.EINTR, errno.EAGAIN, getattr(errno, "ENODATA", 61)}
        if error.errno in transient:
            return b""
        raise GuestConsoleGateError(
            f"could not read owned AxVisor log: {error}"
        ) from error
    if len(data) > MAX_SOURCE_LOG_BYTES:
        raise GuestConsoleGateError("owned AxVisor log exceeds the capture byte limit")
    return data


def _validated_boot_id(value: str | None) -> str | None:
    if value is None:
        return None
    if BOOT_ID_PATTERN.fullmatch(value) is None:
        raise GuestConsoleGateError(
            "guest-console boot-id must match [A-Za-z0-9._-]{1,64}"
        )
    return value


class _FramedMarkerTracker:
    """Incrementally consume one VM's framed payloads and require markers.

    The PL011 TX model emits one frame per written byte, so markers span many
    frames.  Payload bytes are therefore appended to one stream in source
    order and matched line by line, never frame by frame.
    """

    def __init__(
        self,
        *,
        boot_id: str,
        vm_id: int = GUEST_CONSOLE_VM_ID,
        name: str = GUEST_CONSOLE_NAME,
        markers: tuple[bytes, ...] = GUEST_CONSOLE_MARKERS,
        fail_marker: bytes = GUEST_CONSOLE_FAIL_MARKER,
    ) -> None:
        self.boot_id = boot_id
        self.vm_id = vm_id
        self.name = name
        self.markers = markers
        self.fail_marker = fail_marker
        self.frames = 0
        self.payload_bytes = 0
        self.dropped = 0
        self.dma = 0
        self.last_sequence: dict[int, int] = {}
        self.occurrences: list[dict[str, Any]] = []
        self._seen: set[bytes] = set()
        self._stream = bytearray()
        self._stream_pos = 0

    def consume(self, record: Any, *, line_number: int) -> None:
        if record.vm_id != self.vm_id or record.name != self.name:
            raise GuestConsoleGateError(
                f"line {line_number} unexpected guest console frame "
                f"vm={record.vm_id} name={record.name}"
            )
        if record.dropped or record.dma:
            raise GuestConsoleGateError(
                f"line {line_number} guest console frame reports "
                f"dropped={record.dropped} dma={record.dma}"
            )
        previous = self.last_sequence.get(record.generation, -1)
        if record.sequence != previous + 1:
            raise GuestConsoleGateError(
                f"line {line_number} guest console generation {record.generation} "
                f"has sequence gap: expected {previous + 1}, got {record.sequence}"
            )
        self.last_sequence[record.generation] = record.sequence
        self.frames += 1
        self.payload_bytes += len(record.payload)
        if self.payload_bytes > MAX_GUEST_CONSOLE_PAYLOAD_BYTES:
            raise GuestConsoleGateError(
                "framed guest console payload exceeds the bounded evidence limit"
            )
        self._stream.extend(record.payload)
        while True:
            newline = self._stream.find(b"\n", self._stream_pos)
            if newline < 0:
                break
            line = bytes(self._stream[self._stream_pos : newline])
            self._stream_pos = newline + 1
            if line:
                self._consume_payload_line(line, line_number=line_number)

    def _consume_payload_line(self, payload_line: bytes, *, line_number: int) -> None:
        if self.fail_marker in payload_line:
            raise GuestConsoleGateError(
                f"line {line_number} guest console reported "
                f"{self.fail_marker.decode('ascii')}"
            )
        boot_id_token = f"boot_id={self.boot_id}".encode("ascii")
        for marker in self.markers:
            marker_text = marker.decode("ascii")
            if marker not in payload_line:
                continue
            if payload_line.count(marker) != 1:
                raise GuestConsoleGateError(
                    f"line {line_number} guest console marker {marker_text} "
                    "appears more than once on one line"
                )
            if boot_id_token not in payload_line:
                raise GuestConsoleGateError(
                    f"line {line_number} guest console marker {marker_text} "
                    f"does not bind boot_id={self.boot_id}"
                )
            if marker in self._seen:
                raise GuestConsoleGateError(
                    f"line {line_number} guest console marker {marker_text} repeated"
                )
            self._seen.add(marker)
            try:
                text = payload_line.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise GuestConsoleGateError(
                    f"line {line_number} guest console marker payload is not UTF-8"
                ) from error
            self.occurrences.append(
                {"name": marker_text, "line": line_number, "lineText": text}
            )

    def complete(self) -> dict[str, Any] | None:
        if len(self.occurrences) != len(self.markers):
            return None
        names = [occurrence["name"] for occurrence in self.occurrences]
        if names != [marker.decode("ascii") for marker in self.markers]:
            raise GuestConsoleGateError(
                f"guest console markers observed out of order: {names}"
            )
        return {
            "vmId": self.vm_id,
            "name": self.name,
            "bootId": self.boot_id,
            "markers": self.occurrences,
            "frames": self.frames,
            "payloadBytes": self.payload_bytes,
            "dropped": self.dropped,
            "dma": self.dma,
        }


def publish_profile_demux(directory: Path, log_path: Path) -> dict[str, Any]:
    """Demux the sealed host log into per-VM console evidence for this gate."""

    manifest = _DEMUX_API["demux_and_publish"](
        host_log_path=log_path,
        output_directory=directory,
        expected_vms={GUEST_CONSOLE_VM_ID: GUEST_CONSOLE_NAME},
    )
    return {
        "manifest": {
            "path": "console-manifest.json",
            "sha256": hashlib.sha256(
                (directory / "console-manifest.json").read_bytes()
            ).hexdigest(),
        },
        "frameCount": manifest.get("frameCount"),
        "guestVmIds": [
            guest.get("vmId")
            for guest in manifest.get("guests", [])
            if isinstance(guest, dict) and isinstance(guest.get("vmId"), int)
        ],
        "guestBytes": {
            guest.get("vmId"): guest.get("bytes")
            for guest in manifest.get("guests", [])
            if isinstance(guest, dict) and isinstance(guest.get("vmId"), int)
        },
    }


def publish_console_result(
    directory: Path, vm_config: Path, rootfs_plan: Path, capture_status: dict
) -> dict[str, Any]:
    """Publish the runner-side linux-console-result.json with the review validator."""

    validator = runpy.run_path(
        str(Path(__file__).resolve().parent / "validate_linux_guest_console_session.py")
    )
    result = validator["validate_console_session"](
        capture_status_path=capture_status,
        capture_chain_path=directory / "live-capture" / "capture-chain.json",
        guest_dtb_path=directory / "live-capture" / "guest-vm-1.final.dtb",
        guest_dts_path=directory / "live-capture" / "guest-vm-1.final.dts",
        guest_semantic_path=directory / "live-capture" / "guest-vm-1.semantic.json",
        vm_config_path=vm_config,
        rootfs_plan_path=rootfs_plan,
        console_manifest_path=directory / "console-manifest.json",
        console_log_path=directory / "guest-vm-1.console.log",
    )
    serialized = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    target = directory / "linux-console-result.json"
    if target.exists():
        raise GuestConsoleGateError("linux-console-result.json already exists")
    target.write_bytes(serialized)
    return {
        "path": target.name,
        "size": len(serialized),
        "sha256": hashlib.sha256(serialized).hexdigest(),
        "status": result.get("status"),
    }


def wait_for_framed_guest_ready(
    *,
    log_path: Path,
    launcher: Any,
    ready_prefix_bytes: int,
    ready_prefix_sha256: str,
    boot_id: str,
    timeout_seconds: float,
    process_exited: Callable[[], bool] = lambda: False,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait for the three VM-1 framed console markers after QMP resume.

    Only ``vm=1 name=linux`` frames whose payload is new after the frozen
    READY prefix can unlock this gate; host plain text, other VMs, stale
    generations, dropped bytes, DMA-attempt counters and a repeated or
    out-of-order marker all fail closed.
    """

    if boot_id is None or _validated_boot_id(boot_id) is None:
        raise GuestConsoleGateError("guest-console boot-id is required")
    if not 0 < timeout_seconds <= 86_400:
        raise GuestConsoleGateError(
            "guest-console ready timeout must be greater than zero and at most 86400"
        )
    tracker = _FramedMarkerTracker(boot_id=boot_id)
    processed_bytes = 0
    line_offset = 0
    deadline = now() + timeout_seconds
    while True:
        data = _read_log_snapshot(log_path)
        if len(data) < ready_prefix_bytes:
            # DrvFS can transiently return a partial snapshot of an actively
            # appended log; wait for the full immutable prefix instead of
            # mistaking a short read for a truncated file.
            if process_exited():
                raise GuestConsoleGateError("QEMU exited before the framed console READY markers")
            exit_code = launcher.poll()
            if exit_code is not None:
                raise GuestConsoleGateError(
                    f"QEMU launcher exited before the framed console READY markers: {exit_code}"
                )
            if now() >= deadline:
                raise GuestConsoleGateError(
                    "timed out waiting for the three VM-1 framed console READY markers"
                )
            sleep(0.10)
            continue
        ready_prefix = data[:ready_prefix_bytes]
        if _sha256(ready_prefix) != ready_prefix_sha256:
            raise GuestConsoleGateError(
                "owned AxVisor log READY prefix changed after QMP resume"
            )
        region = data[ready_prefix_bytes:]
        complete_end = region.rfind(b"\n") + 1
        complete = region[:complete_end]
        if len(complete) > processed_bytes:
            delta = complete[processed_bytes:]
            delta_lines = delta.count(b"\n")
            try:
                for record in iter_host_log_frames(delta):
                    tracker.consume(record, line_number=line_offset + record.line_number)
            except GuestConsoleDemuxError as error:
                raise GuestConsoleGateError(str(error)) from error
            line_offset += delta_lines
            processed_bytes = len(complete)
            observation = tracker.complete()
            if observation is not None:
                return observation
        if process_exited():
            raise GuestConsoleGateError("QEMU exited before the framed console READY markers")
        exit_code = launcher.poll()
        if exit_code is not None:
            raise GuestConsoleGateError(
                f"QEMU launcher exited before the framed console READY markers: {exit_code}"
            )
        if now() >= deadline:
            raise GuestConsoleGateError(
                "timed out waiting for the three VM-1 framed console READY markers"
            )
        sleep(0.10)


DUAL_GUEST_READY_MARKERS = {
    1: (b"AXVISOR_DUAL_GUEST_LINUX_READY",),
    2: (b"AXVISOR_DUAL_GUEST_ZEPHYR_READY",),
}
DUAL_GUEST_NAMES = {1: "linux", 2: "zephyr"}
# VM 1 (Linux) READY must appear after the resume prefix; VM 2 (Zephyr)
# READY is emitted once at boot and may predate the prefix, so its frames
# are consumed from the full log.
DUAL_GUEST_VM_IDS = frozenset(DUAL_GUEST_NAMES)


def wait_for_dual_guest_ready(
    *,
    log_path: Path,
    launcher: Any,
    ready_prefix_bytes: int,
    ready_prefix_sha256: str,
    boot_id: str,
    timeout_seconds: float,
    process_exited: Callable[[], bool] = lambda: False,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait for the Linux and Zephyr framed READY markers after QMP resume.

    Frames are split per VM; each VM must yield exactly one boot-id-bound
    READY marker (Linux also emits its /dev/console marker chain first).
    Cross-VM frames, stale generations, dropped bytes and DMA-attempt
    counters all fail closed.

    The Linux (VM 1) READY marker must appear in payload bytes written
    after the QMP-resume freeze prefix: it proves the VM-local console
    keeps delivering from early output through the explicit READY.
    The Zephyr (VM 2) READY marker is emitted once at boot, which for a
    normal dual run happens *before* the freeze prefix is taken, so VM 2
    frames are consumed from the full log instead of only the resumed
    region.
    """

    if boot_id is None or _validated_boot_id(boot_id) is None:
        raise GuestConsoleGateError("dual-guest boot-id is required")
    if not 0 < timeout_seconds <= 86_400:
        raise GuestConsoleGateError(
            "dual-guest ready timeout must be greater than zero and at most 86400"
        )
    trackers = {
        vm_id: _FramedMarkerTracker(
            boot_id=boot_id,
            vm_id=vm_id,
            name=DUAL_GUEST_NAMES[vm_id],
            markers=DUAL_GUEST_READY_MARKERS[vm_id],
        )
        for vm_id in DUAL_GUEST_READY_MARKERS
    }
    processed_bytes = 0  # resumed (post-prefix) region cursor for VM 1
    processed_bytes_all = 0  # full-log cursor for VM 2
    line_offset = 0
    deadline = now() + timeout_seconds
    while True:
        data = _read_log_snapshot(log_path)
        if len(data) < ready_prefix_bytes:
            if process_exited():
                raise GuestConsoleGateError("QEMU exited before the dual READY markers")
            exit_code = launcher.poll()
            if exit_code is not None:
                raise GuestConsoleGateError(
                    f"QEMU launcher exited before the dual READY markers: {exit_code}"
                )
            if now() >= deadline:
                raise GuestConsoleGateError(
                    "timed out waiting for the Linux and Zephyr framed READY markers"
                )
            sleep(0.10)
            continue
        ready_prefix = data[:ready_prefix_bytes]
        if _sha256(ready_prefix) != ready_prefix_sha256:
            raise GuestConsoleGateError(
                "owned AxVisor log READY prefix changed after QMP resume"
            )
        # VM 1 (Linux): only frames written after the resume prefix count,
        # so the READY proves console delivery after the QMP capture window.
        region = data[ready_prefix_bytes:]
        complete_end = region.rfind(b"\n") + 1
        complete = region[:complete_end]
        if len(complete) > processed_bytes:
            delta = complete[processed_bytes:]
            delta_lines = delta.count(b"\n")
            try:
                for record in iter_host_log_frames(delta):
                    if record.vm_id != 1:
                        continue
                    tracker = trackers.get(record.vm_id)
                    if tracker is None or tracker.name != record.name:
                        raise GuestConsoleGateError(
                            f"line {line_offset + record.line_number} unexpected guest "
                            f"console frame vm={record.vm_id} name={record.name}"
                        )
                    tracker.consume(record, line_number=line_offset + record.line_number)
            except GuestConsoleDemuxError as error:
                raise GuestConsoleGateError(str(error)) from error
            line_offset += delta_lines
            processed_bytes = len(complete)
        # VM 2 (Zephyr): consume from the full log so a boot-time READY
        # that was already written before the resume prefix still counts.
        complete_all_end = data.rfind(b"\n") + 1
        complete_all = data[:complete_all_end]
        if len(complete_all) > processed_bytes_all:
            delta_all = complete_all[processed_bytes_all:]
            try:
                for record in iter_host_log_frames(delta_all):
                    if record.vm_id != 2:
                        continue
                    tracker = trackers.get(record.vm_id)
                    if tracker is None or tracker.name != record.name:
                        raise GuestConsoleGateError(
                            f"line {record.line_number} unexpected guest "
                            f"console frame vm={record.vm_id} name={record.name}"
                        )
                    tracker.consume(record, line_number=record.line_number)
            except GuestConsoleDemuxError as error:
                raise GuestConsoleGateError(str(error)) from error
            processed_bytes_all = len(complete_all)
        observations = {
            vm_id: tracker.complete()
            for vm_id, tracker in trackers.items()
        }
        if all(observation is not None for observation in observations.values()):
                return {
                    "bootId": boot_id,
                    "guests": {vm_id: observation for vm_id, observation in observations.items()},
                }
        if process_exited():
            raise GuestConsoleGateError("QEMU exited before the dual READY markers")
        exit_code = launcher.poll()
        if exit_code is not None:
            raise GuestConsoleGateError(
                f"QEMU launcher exited before the dual READY markers: {exit_code}"
            )
        if now() >= deadline:
            raise GuestConsoleGateError(
                "timed out waiting for the Linux and Zephyr framed READY markers"
            )
        sleep(0.10)
