#!/usr/bin/env python3
"""Strictly demux framed guest-console bytes from an existing AxVisor host log."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FRAME_MARKER = b"AXVISOR_GUEST_CONSOLE_FRAME"
FRAME_PREFIX = FRAME_MARKER + b" "
FRAME_VERSION = 1
FIELD_ORDER = (
    "v",
    "vm",
    "name",
    "gen",
    "seq",
    "len",
    "total",
    "dropped",
    "dma",
    "hex",
)
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
UNSIGNED_PATTERN = re.compile(rb"(?:0|[1-9][0-9]*)\Z")
LOWER_HEX_PATTERN = re.compile(rb"(?:[0-9a-f]{2})+\Z")
MAX_VM_ID = 65_535
MAX_U64 = (1 << 64) - 1
MAX_FRAME_PAYLOAD_BYTES = 4096
MAX_HOST_LOG_BYTES = 512 * 1024 * 1024
MAX_CONSOLE_BYTES_PER_VM = 64 * 1024 * 1024
MANIFEST_NAME = "console-manifest.json"
LOCK_NAME = ".console-demux.lock"
STAGING_SUFFIX = ".demux-part"


class GuestConsoleDemuxError(ValueError):
    """The input frames or requested evidence publication are unsafe."""


@dataclass(frozen=True)
class GenerationSummary:
    """Sequence and byte boundaries for one VM device generation."""

    generation: int
    records: int
    first_sequence: int
    last_sequence: int
    byte_start: int
    byte_end: int


@dataclass(frozen=True)
class DemuxedGuest:
    """Exact reconstructed bytes and bounded metadata for one expected VM."""

    vm_id: int
    name: str
    data: bytes
    records: int
    generations: tuple[GenerationSummary, ...]


@dataclass(frozen=True)
class FrameRecord:
    """One validated console frame with its exact source line number."""

    line_number: int
    vm_id: int
    name: str
    generation: int
    sequence: int
    payload: bytes
    total: int
    dropped: int
    dma: int


@dataclass(frozen=True)
class DemuxResult:
    """All expected VM console streams reconstructed from one host log."""

    guests: dict[int, DemuxedGuest]
    frame_count: int
    trailing_truncated: dict[str, Any] | None = None


@dataclass(frozen=True)
class _ParsedFrame:
    vm_id: int
    name: str
    generation: int
    sequence: int
    payload: bytes
    total: int
    dropped: int
    dma: int


@dataclass
class _GenerationState:
    generation: int
    byte_start: int
    records: int = 0
    total: int = 0
    last_sequence: int = -1


@dataclass
class _GuestState:
    vm_id: int
    name: str
    data: bytearray
    generations: list[_GenerationState]
    records: int = 0


def _parse_unsigned(
    value: bytes,
    *,
    field: str,
    line_number: int,
    maximum: int = MAX_U64,
) -> int:
    if UNSIGNED_PATTERN.fullmatch(value) is None:
        raise GuestConsoleDemuxError(
            f"line {line_number} field {field} is not a canonical unsigned integer"
        )
    parsed = int(value)
    if parsed > maximum:
        raise GuestConsoleDemuxError(
            f"line {line_number} field {field} exceeds {maximum}"
        )
    return parsed


def _validated_expected_vms(expected_vms: Mapping[int, str]) -> dict[int, str]:
    if not isinstance(expected_vms, Mapping) or not expected_vms:
        raise GuestConsoleDemuxError("at least one expected VM is required")
    validated: dict[int, str] = {}
    names: set[str] = set()
    for vm_id, name in expected_vms.items():
        if isinstance(vm_id, bool) or not isinstance(vm_id, int):
            raise GuestConsoleDemuxError("expected VM ids must be integers")
        if not 1 <= vm_id <= MAX_VM_ID:
            raise GuestConsoleDemuxError(
                f"expected VM id {vm_id} is outside 1..{MAX_VM_ID}"
            )
        if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
            raise GuestConsoleDemuxError(
                f"expected VM {vm_id} name must match {NAME_PATTERN.pattern}"
            )
        if name in names:
            raise GuestConsoleDemuxError(f"expected VM name {name} is duplicated")
        validated[vm_id] = name
        names.add(name)
    return dict(sorted(validated.items()))


def _parse_frame(line: bytes, *, line_number: int) -> _ParsedFrame:
    if len(line) > len(FRAME_PREFIX) + 512 + (MAX_FRAME_PAYLOAD_BYTES * 2):
        raise GuestConsoleDemuxError(f"line {line_number} frame exceeds the size limit")
    fields = line[len(FRAME_PREFIX) :].split(b" ")
    if len(fields) != len(FIELD_ORDER) or any(not field for field in fields):
        raise GuestConsoleDemuxError(
            f"line {line_number} frame must contain exactly {len(FIELD_ORDER)} fields"
        )

    values: dict[str, bytes] = {}
    for expected_key, token in zip(FIELD_ORDER, fields):
        key, separator, value = token.partition(b"=")
        if not separator or not key or not value:
            raise GuestConsoleDemuxError(
                f"line {line_number} has a malformed {expected_key} field"
            )
        try:
            decoded_key = key.decode("ascii")
        except UnicodeDecodeError as error:
            raise GuestConsoleDemuxError(
                f"line {line_number} contains a non-ASCII field name"
            ) from error
        if decoded_key != expected_key:
            raise GuestConsoleDemuxError(
                f"line {line_number} expected field {expected_key}, got {decoded_key}"
            )
        values[expected_key] = value

    version = _parse_unsigned(
        values["v"], field="v", line_number=line_number, maximum=FRAME_VERSION
    )
    if version != FRAME_VERSION:
        raise GuestConsoleDemuxError(
            f"line {line_number} uses unsupported frame version {version}"
        )
    vm_id = _parse_unsigned(
        values["vm"], field="vm", line_number=line_number, maximum=MAX_VM_ID
    )
    if vm_id == 0:
        raise GuestConsoleDemuxError(f"line {line_number} VM id must be positive")
    try:
        name = values["name"].decode("ascii")
    except UnicodeDecodeError as error:
        raise GuestConsoleDemuxError(
            f"line {line_number} VM {vm_id} name is not ASCII"
        ) from error
    if NAME_PATTERN.fullmatch(name) is None:
        raise GuestConsoleDemuxError(
            f"line {line_number} VM {vm_id} name must match {NAME_PATTERN.pattern}"
        )
    generation = _parse_unsigned(
        values["gen"], field="gen", line_number=line_number
    )
    sequence = _parse_unsigned(
        values["seq"], field="seq", line_number=line_number
    )
    declared_length = _parse_unsigned(
        values["len"],
        field="len",
        line_number=line_number,
        maximum=MAX_FRAME_PAYLOAD_BYTES,
    )
    if declared_length == 0:
        raise GuestConsoleDemuxError(
            f"line {line_number} VM {vm_id} contains an empty frame"
        )
    total = _parse_unsigned(
        values["total"], field="total", line_number=line_number
    )
    dropped = _parse_unsigned(
        values["dropped"], field="dropped", line_number=line_number
    )
    dma = _parse_unsigned(values["dma"], field="dma", line_number=line_number)
    encoded_payload = values["hex"]
    if LOWER_HEX_PATTERN.fullmatch(encoded_payload) is None:
        raise GuestConsoleDemuxError(
            f"line {line_number} VM {vm_id} payload is not canonical lowercase hex"
        )
    payload = bytes.fromhex(encoded_payload.decode("ascii"))
    if len(payload) != declared_length:
        raise GuestConsoleDemuxError(
            f"line {line_number} VM {vm_id} payload length {len(payload)} "
            f"does not match declared length {declared_length}"
        )
    return _ParsedFrame(
        vm_id=vm_id,
        name=name,
        generation=generation,
        sequence=sequence,
        payload=payload,
        total=total,
        dropped=dropped,
        dma=dma,
    )


def _append_frame(
    state: _GuestState, frame: _ParsedFrame, *, line_number: int
) -> None:
    if frame.dropped:
        raise GuestConsoleDemuxError(
            f"line {line_number} VM {frame.vm_id} reports {frame.dropped} dropped bytes"
        )
    if frame.dma:
        raise GuestConsoleDemuxError(
            f"line {line_number} VM {frame.vm_id} reports {frame.dma} DMA enable attempts"
        )

    if not state.generations:
        if frame.generation != 0:
            raise GuestConsoleDemuxError(
                f"line {line_number} VM {frame.vm_id} must start at generation 0"
            )
        if frame.sequence != 0:
            raise GuestConsoleDemuxError(
                f"line {line_number} VM {frame.vm_id} generation 0 must start at sequence 0"
            )
        state.generations.append(
            _GenerationState(generation=0, byte_start=len(state.data))
        )
    else:
        current = state.generations[-1]
        if frame.generation < current.generation:
            raise GuestConsoleDemuxError(
                f"line {line_number} VM {frame.vm_id} has stale generation "
                f"{frame.generation} after generation {current.generation}"
            )
        if frame.generation > current.generation + 1:
            raise GuestConsoleDemuxError(
                f"line {line_number} VM {frame.vm_id} has generation {frame.generation}; "
                f"expected generation {current.generation} or {current.generation + 1}"
            )
        if frame.generation == current.generation + 1:
            if frame.sequence != 0:
                raise GuestConsoleDemuxError(
                    f"line {line_number} VM {frame.vm_id} generation "
                    f"{frame.generation} must start at sequence 0"
                )
            state.generations.append(
                _GenerationState(
                    generation=frame.generation, byte_start=len(state.data)
                )
            )

    current = state.generations[-1]
    expected_sequence = current.last_sequence + 1
    if frame.sequence != expected_sequence:
        raise GuestConsoleDemuxError(
            f"line {line_number} VM {frame.vm_id} generation {frame.generation} "
            f"has sequence {frame.sequence}, expected sequence {expected_sequence}"
        )
    expected_total = current.total + len(frame.payload)
    if frame.total != expected_total:
        raise GuestConsoleDemuxError(
            f"line {line_number} VM {frame.vm_id} generation {frame.generation} "
            f"declares total {frame.total}, expected {expected_total}"
        )
    if len(state.data) + len(frame.payload) > MAX_CONSOLE_BYTES_PER_VM:
        raise GuestConsoleDemuxError(
            f"VM {frame.vm_id} reconstructed console exceeds "
            f"{MAX_CONSOLE_BYTES_PER_VM} bytes"
        )

    state.data.extend(frame.payload)
    state.records += 1
    current.records += 1
    current.total = expected_total
    current.last_sequence = frame.sequence


def iter_host_log_frames(host_log: bytes) -> Iterator[FrameRecord]:
    """Yield validated console-frame records from host log bytes.

    Each record keeps the exact 1-based line number used by
    :func:`demux_host_log`.  Non-frame lines are skipped; a line that contains
    the frame marker at a non-leading position, a malformed prefix, or an
    invalid frame payload raises :class:`GuestConsoleDemuxError` with the same
    line-numbered diagnostics as the offline demux path.  Dropped-byte and
    DMA-attempt counters are surfaced verbatim so a live consumer can fail
    closed without mutating the shared demux state machine.
    """

    if not isinstance(host_log, bytes):
        raise GuestConsoleDemuxError("host log data must be bytes")
    if len(host_log) > MAX_HOST_LOG_BYTES:
        raise GuestConsoleDemuxError(
            f"host log exceeds the {MAX_HOST_LOG_BYTES}-byte limit"
        )
    for line_number, raw_line in enumerate(host_log.split(b"\n"), start=1):
        line = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line
        marker_offset = line.find(FRAME_MARKER)
        if marker_offset < 0:
            continue
        if marker_offset != 0:
            raise GuestConsoleDemuxError(
                f"line {line_number} frame marker is not at the start of its line"
            )
        if not line.startswith(FRAME_PREFIX):
            raise GuestConsoleDemuxError(
                f"line {line_number} frame marker is not followed by one space"
            )
        frame = _parse_frame(line, line_number=line_number)
        yield FrameRecord(
            line_number=line_number,
            vm_id=frame.vm_id,
            name=frame.name,
            generation=frame.generation,
            sequence=frame.sequence,
            payload=frame.payload,
            total=frame.total,
            dropped=frame.dropped,
            dma=frame.dma,
        )


def demux_host_log(
    host_log_data: bytes,
    expected_vms: Mapping[int, str],
    *,
    allow_trailing_truncation: bool = False,
) -> DemuxResult:
    """Validate all protocol frames and reconstruct exact per-VM byte streams.

    With ``allow_trailing_truncation`` the final line of the log may carry a
    frame whose payload is shorter than declared: this is a shutdown artifact
    (hypervisor-buffered bytes lost when QEMU exits), not a runtime corruption.
    The truncated frame is dropped and reported in the manifest.
    """

    if not isinstance(host_log_data, bytes):
        raise GuestConsoleDemuxError("host log data must be bytes")
    if len(host_log_data) > MAX_HOST_LOG_BYTES:
        raise GuestConsoleDemuxError(
            f"host log exceeds the {MAX_HOST_LOG_BYTES}-byte limit"
        )
    expected = _validated_expected_vms(expected_vms)
    states = {
        vm_id: _GuestState(vm_id, name, bytearray(), [])
        for vm_id, name in expected.items()
    }
    frame_count = 0
    trailing_truncated: dict[str, Any] | None = None
    lines = host_log_data.split(b"\n")
    last_frame_line = 0
    for idx, raw_line in enumerate(lines, start=1):
        if FRAME_MARKER in raw_line:
            last_frame_line = idx

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line
        marker_offset = line.find(FRAME_MARKER)
        if marker_offset < 0:
            continue
        trailing_allowed = allow_trailing_truncation and line_number == last_frame_line
        try:
            if marker_offset != 0:
                raise GuestConsoleDemuxError(
                    f"line {line_number} frame marker is not at the start of its line"
                )
            if not line.startswith(FRAME_PREFIX):
                raise GuestConsoleDemuxError(
                    f"line {line_number} frame marker is not followed by one space"
                )
            frame = _parse_frame(line, line_number=line_number)
        except GuestConsoleDemuxError as error:
            if trailing_allowed:
                trailing_truncated = {"line": line_number, "error": str(error)}
                continue
            raise
        if frame.vm_id not in expected:
            raise GuestConsoleDemuxError(
                f"line {line_number} contains unexpected VM {frame.vm_id}"
            )
        expected_name = expected[frame.vm_id]
        if frame.name != expected_name:
            raise GuestConsoleDemuxError(
                f"line {line_number} VM {frame.vm_id} name {frame.name} does not match "
                f"expected name {expected_name}"
            )
        _append_frame(states[frame.vm_id], frame, line_number=line_number)
        frame_count += 1

    for vm_id, state in states.items():
        if not state.generations:
            raise GuestConsoleDemuxError(f"missing frames for expected VM {vm_id}")

    guests: dict[int, DemuxedGuest] = {}
    for vm_id, state in states.items():
        generations = tuple(
            GenerationSummary(
                generation=item.generation,
                records=item.records,
                first_sequence=0,
                last_sequence=item.last_sequence,
                byte_start=item.byte_start,
                byte_end=item.byte_start + item.total,
            )
            for item in state.generations
        )
        guests[vm_id] = DemuxedGuest(
            vm_id=vm_id,
            name=state.name,
            data=bytes(state.data),
            records=state.records,
            generations=generations,
        )
    return DemuxResult(
        guests=guests, frame_count=frame_count, trailing_truncated=trailing_truncated
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plain_filename(value: str, *, field: str) -> str:
    if not value or Path(value).name != value or value in {".", ".."}:
        raise GuestConsoleDemuxError(f"{field} must be a plain filename")
    return value


def _output_name(vm_id: int) -> str:
    return f"guest-vm-{vm_id}.console.log"


def _build_manifest(
    result: DemuxResult, *, host_log_name: str, host_log_data: bytes
) -> dict[str, Any]:
    guests: list[dict[str, Any]] = []
    for vm_id, guest in sorted(result.guests.items()):
        generations = [
            {
                "generation": item.generation,
                "records": item.records,
                "firstSequence": item.first_sequence,
                "lastSequence": item.last_sequence,
                "byteStart": item.byte_start,
                "byteEnd": item.byte_end,
                "bytes": item.byte_end - item.byte_start,
            }
            for item in guest.generations
        ]
        guests.append(
            {
                "vmId": vm_id,
                "name": guest.name,
                "path": _output_name(vm_id),
                "sha256": _sha256(guest.data),
                "bytes": len(guest.data),
                "records": guest.records,
                "firstGeneration": guest.generations[0].generation,
                "lastGeneration": guest.generations[-1].generation,
                "droppedBytes": 0,
                "dmaEnableAttempts": 0,
                "generations": generations,
            }
        )
    return {
        "schemaVersion": 1,
        "artifactStatus": "host-log-derived-unreviewed",
        "status": "console_frames_demuxed",
        "proofScope": (
            "provided-host-log-frame-validation-and-per-vm-byte-reconstruction"
        ),
        "trailingTruncatedFrame": result.trailing_truncated,
        "doesNotProve": [
            "the frames were produced by a real AxVisor execution",
            "either guest booted",
            "guest PL011 MMIO accesses occurred",
            "passthrough DMA is isolated",
            "Linux and Zephyr have IP connectivity",
        ],
        "sourcePathBase": "console-manifest.json parent directory",
        "source": {
            "hostLog": {
                "path": host_log_name,
                "sha256": _sha256(host_log_data),
                "bytes": len(host_log_data),
            }
        },
        "frameProtocol": {
            "marker": FRAME_MARKER.decode("ascii"),
            "version": FRAME_VERSION,
            "encoding": "one ASCII record per line with lowercase hex payload",
            "fieldOrder": list(FIELD_ORDER),
            "sequenceScope": "per VM and generation, contiguous from zero",
            "totalScope": "per VM generation, exact cumulative payload bytes",
        },
        "frameCount": result.frame_count,
        "guests": guests,
        "publication": "guest console files published first; this manifest published last",
    }


@contextmanager
def _filesystem_lock(directory: Path) -> Iterator[None]:
    lock_path = directory / LOCK_NAME
    descriptor: int | None = None
    lock_owned = False
    try:
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as error:
            raise GuestConsoleDemuxError(
                f"demux lock {LOCK_NAME} already exists"
            ) from error
        lock_owned = True
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if lock_owned:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


class FilesystemDemuxStorage:
    """No-overwrite transactional publication in one existing directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()

    def lock(self) -> Any:
        return _filesystem_lock(self.directory)

    def ensure_absent(self, paths: list[Path]) -> None:
        for path in paths:
            if path.exists() or path.is_symlink():
                raise GuestConsoleDemuxError(f"demux path {path.name} already exists")

    def write_staging(self, path: Path, data: bytes) -> None:
        if path.parent.resolve() != self.directory:
            raise GuestConsoleDemuxError(
                f"staging path {path.name} escapes the output directory"
            )
        try:
            with path.open("xb") as destination:
                destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
        except FileExistsError as error:
            raise GuestConsoleDemuxError(
                f"staging path {path.name} already exists"
            ) from error

    def publish(self, staging_path: Path, final_path: Path) -> None:
        if staging_path.is_symlink() or not staging_path.is_file():
            raise GuestConsoleDemuxError(
                f"staging path {staging_path.name} is not a regular file"
            )
        if final_path.exists() or final_path.is_symlink():
            raise GuestConsoleDemuxError(
                f"demux path {final_path.name} already exists"
            )
        try:
            os.link(staging_path, final_path, follow_symlinks=False)
        except FileExistsError as error:
            raise GuestConsoleDemuxError(
                f"demux path {final_path.name} already exists"
            ) from error
        try:
            staging_path.unlink()
        except OSError:
            try:
                final_path.unlink(missing_ok=True)
            finally:
                raise

    def cleanup(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def publish_demux_evidence(
    result: DemuxResult,
    *,
    output_directory: Path,
    host_log_name: str,
    host_log_data: bytes,
    storage: Any | None = None,
) -> dict[str, Any]:
    """Publish raw VM streams first and the bounded manifest last, without overwrite."""

    if not isinstance(result, DemuxResult) or not result.guests:
        raise GuestConsoleDemuxError("demux result is empty or malformed")
    if not isinstance(host_log_data, bytes):
        raise GuestConsoleDemuxError("host log data must be bytes")
    source_name = _plain_filename(host_log_name, field="host log name")
    directory = output_directory.resolve()
    if storage is None:
        if output_directory.is_symlink() or not directory.is_dir():
            raise GuestConsoleDemuxError(
                "output directory must be an existing non-symlink directory"
            )
        demux_storage: Any = FilesystemDemuxStorage(directory)
    else:
        demux_storage = storage

    final_paths = [
        directory / _output_name(vm_id) for vm_id in sorted(result.guests)
    ]
    manifest_path = directory / MANIFEST_NAME
    staging_paths = [
        directory / f".{path.name}{STAGING_SUFFIX}" for path in final_paths
    ]
    manifest_staging = directory / f".{MANIFEST_NAME}{STAGING_SUFFIX}"
    reserved_names = {
        path.name
        for path in [*final_paths, manifest_path, *staging_paths, manifest_staging]
    }
    if source_name in reserved_names:
        raise GuestConsoleDemuxError("host log name collides with demux output")

    manifest = _build_manifest(
        result, host_log_name=source_name, host_log_data=host_log_data
    )
    serialized_manifest = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    published_paths: list[Path] = []
    committed = False

    with demux_storage.lock():
        try:
            demux_storage.ensure_absent(
                [*staging_paths, manifest_staging, *final_paths, manifest_path]
            )
            for vm_id, staging_path in zip(sorted(result.guests), staging_paths):
                demux_storage.write_staging(
                    staging_path, result.guests[vm_id].data
                )
            demux_storage.write_staging(manifest_staging, serialized_manifest)

            for staging_path, final_path in zip(staging_paths, final_paths):
                demux_storage.publish(staging_path, final_path)
                published_paths.append(final_path)
            demux_storage.publish(manifest_staging, manifest_path)
            published_paths.append(manifest_path)
            committed = True
        finally:
            if not committed:
                for path in [*staging_paths, manifest_staging]:
                    demux_storage.cleanup(path)
                for path in reversed(published_paths):
                    demux_storage.cleanup(path)
    return manifest


def demux_and_publish(
    host_log_path: Path,
    output_directory: Path,
    expected_vms: Mapping[int, str],
    *,
    allow_trailing_truncation: bool = False,
) -> dict[str, Any]:
    """Read one sibling host log, demux it, and publish a new evidence bundle."""

    if host_log_path.is_symlink():
        raise GuestConsoleDemuxError("host log must not be a symlink")
    try:
        source = host_log_path.resolve(strict=True)
    except FileNotFoundError as error:
        raise GuestConsoleDemuxError("host log is missing") from error
    directory = output_directory.resolve()
    if output_directory.is_symlink() or not directory.is_dir():
        raise GuestConsoleDemuxError(
            "output directory must be an existing non-symlink directory"
        )
    if not source.is_file():
        raise GuestConsoleDemuxError("host log is not a regular file")
    if source.parent != directory:
        raise GuestConsoleDemuxError(
            "host log and outputs must share one evidence directory"
        )
    if source.stat().st_size > MAX_HOST_LOG_BYTES:
        raise GuestConsoleDemuxError(
            f"host log exceeds the {MAX_HOST_LOG_BYTES}-byte limit"
        )
    host_log_data = source.read_bytes()
    result = demux_host_log(
        host_log_data, expected_vms, allow_trailing_truncation=allow_trailing_truncation
    )
    return publish_demux_evidence(
        result,
        output_directory=directory,
        host_log_name=source.name,
        host_log_data=host_log_data,
    )


def parse_expected_vms(values: Sequence[str]) -> dict[int, str]:
    """Parse repeated CLI values of the form VM_ID:NAME."""

    expected: dict[int, str] = {}
    for value in values:
        vm_text, separator, name = value.partition(":")
        try:
            encoded_vm = vm_text.encode("ascii")
        except UnicodeEncodeError:
            encoded_vm = b""
        if not separator or UNSIGNED_PATTERN.fullmatch(encoded_vm) is None:
            raise GuestConsoleDemuxError(
                f"--expect-vm value {value!r} must be VM_ID:NAME"
            )
        vm_id = int(vm_text)
        if vm_id in expected:
            raise GuestConsoleDemuxError(f"expected VM id {vm_id} is duplicated")
        expected[vm_id] = name
    return _validated_expected_vms(expected)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Strictly validate AXVISOR_GUEST_CONSOLE_FRAME records, reconstruct "
            "one raw byte stream per expected VM, and publish the manifest last"
        )
    )
    parser.add_argument("--host-log", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--expect-vm",
        action="append",
        required=True,
        metavar="VM_ID:NAME",
        help="expected VM id and exact framed name; repeat once per VM",
    )
    parser.add_argument(
        "--allow-trailing-truncation",
        action="store_true",
        help="tolerate a payload-short final frame (shutdown artifact) and record it",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        expected_vms = parse_expected_vms(args.expect_vm)
        manifest = demux_and_publish(
            args.host_log,
            args.output_dir,
            expected_vms,
            allow_trailing_truncation=args.allow_trailing_truncation,
        )
    except GuestConsoleDemuxError as error:
        print(f"Guest console demux failed: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Could not read/write guest console evidence: {error}", file=sys.stderr)
        return 2

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
