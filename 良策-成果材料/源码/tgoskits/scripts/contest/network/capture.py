#!/usr/bin/env python3
"""Bounded host-owned Ethernet frame capture and standard PCAP export.

The capture is an observational ring.  A full ring drops only evidence frames
and never changes a caller's forwarding decision.  Each accepted record is
validated before publication and carries the session, port generation,
direction, monotonic timestamp, exact length, bytes, and SHA-256 digest.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

try:
    from ..host_vm_carveout_io import publish_new_file
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from host_vm_carveout_io import publish_new_file  # type: ignore[no-redef]


SCHEMA_VERSION = "p4-network-frame-v1"
PCAP_LINKTYPE_ETHERNET = 1
MIN_ETHERNET_FRAME_LENGTH = 14
MAX_ETHERNET_FRAME_LENGTH = 1514
VALID_PORTS = (0, 1)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")


class CaptureError(ValueError):
    """Raised when a frame or a capture artifact violates its contract."""


def _require_int(value: Any, field: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CaptureError(f"{field} must be an integer")
    if value < minimum:
        raise CaptureError(f"{field} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise CaptureError(f"{field} must be <= {maximum}")
    return value


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise CaptureError(f"{field} must be a safe non-empty identifier")
    return value


def direction_for_port(port: int) -> str:
    """Return the only legal switch direction for an ingress port."""

    if port not in VALID_PORTS:
        raise CaptureError("ingress_port must be 0 or 1")
    return f"port{port}_to_port{1 - port}"


@dataclass(frozen=True)
class CapturedFrame:
    """One immutable frame record kept in the bounded capture ring."""

    sequence: int
    run_id: str
    session_id: str
    ingress_port: int
    generation: int
    direction: str
    monotonic_ns: int
    frame: bytes

    @property
    def length(self) -> int:
        return len(self.frame)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.frame).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "ingress_port": self.ingress_port,
            "generation": self.generation,
            "direction": self.direction,
            "monotonic_ns": self.monotonic_ns,
            "length": self.length,
            "sha256": self.sha256,
            "frame_hex": self.frame.hex(),
        }


class FrameCapture:
    """A fixed-capacity, single-session frame capture ring."""

    def __init__(
        self,
        max_frames: int,
        *,
        max_frame_length: int = MAX_ETHERNET_FRAME_LENGTH,
        run_id: str = "",
    ) -> None:
        self.max_frames = _require_int(max_frames, "max_frames", minimum=1)
        self.max_frame_length = _require_int(
            max_frame_length,
            "max_frame_length",
            minimum=MIN_ETHERNET_FRAME_LENGTH,
            maximum=MAX_ETHERNET_FRAME_LENGTH,
        )
        if run_id and not _SAFE_ID.fullmatch(run_id):
            raise CaptureError("run_id must be a safe non-empty identifier")
        self.run_id = run_id
        self._session_id: str | None = None
        self._frames: list[CapturedFrame] = []
        self._last_timestamp_ns: int | None = None
        self._last_generation: dict[int, int] = {}
        self._capture_drops = 0

    @property
    def capture_drops(self) -> int:
        return self._capture_drops

    @property
    def frames(self) -> tuple[CapturedFrame, ...]:
        return tuple(self._frames)

    @property
    def captured_bytes(self) -> int:
        return sum(frame.length for frame in self._frames)

    def record(
        self,
        frame: bytes,
        *,
        session_id: str,
        ingress_port: int,
        generation: int,
        direction: str,
        monotonic_ns: int,
        run_id: str | None = None,
    ) -> CapturedFrame | None:
        """Validate and append one frame, or count an evidence-ring drop.

        A return value of ``None`` means that the bounded capture ring was full;
        it is not a forwarding failure.  Invalid input always raises.
        """

        if not isinstance(frame, (bytes, bytearray, memoryview)):
            raise CaptureError("frame must be bytes-like")
        frame_bytes = bytes(frame)
        if not MIN_ETHERNET_FRAME_LENGTH <= len(frame_bytes) <= self.max_frame_length:
            raise CaptureError(
                f"frame length must be {MIN_ETHERNET_FRAME_LENGTH}..{self.max_frame_length}"
            )
        session_id = _require_id(session_id, "session_id")
        if self._session_id is None:
            self._session_id = session_id
        elif self._session_id != session_id:
            raise CaptureError("session_id changed inside one capture")
        if run_id is not None:
            run_id = _require_id(run_id, "run_id")
            if not self.run_id:
                self.run_id = run_id
            elif self.run_id != run_id:
                raise CaptureError("run_id changed inside one capture")
        if not self.run_id:
            raise CaptureError("run_id must be bound before exporting evidence")
        if ingress_port not in VALID_PORTS:
            raise CaptureError("ingress_port must be 0 or 1")
        generation = _require_int(generation, "generation", maximum=(1 << 64) - 1)
        monotonic_ns = _require_int(monotonic_ns, "monotonic_ns", maximum=(1 << 64) - 1)
        expected_direction = direction_for_port(ingress_port)
        if direction != expected_direction:
            raise CaptureError(
                f"direction {direction!r} does not match ingress_port {ingress_port}"
            )
        previous_generation = self._last_generation.get(ingress_port)
        if previous_generation is not None:
            if generation < previous_generation:
                raise CaptureError("stale port generation")
            if generation > previous_generation + 1:
                raise CaptureError("port generation skipped a value")
        if self._last_timestamp_ns is not None and monotonic_ns < self._last_timestamp_ns:
            raise CaptureError("monotonic_ns moved backwards")

        self._last_generation[ingress_port] = generation
        self._last_timestamp_ns = monotonic_ns
        if len(self._frames) >= self.max_frames:
            self._capture_drops += 1
            return None
        captured = CapturedFrame(
            sequence=len(self._frames),
            run_id=self.run_id,
            session_id=session_id,
            ingress_port=ingress_port,
            generation=generation,
            direction=direction,
            monotonic_ns=monotonic_ns,
            frame=frame_bytes,
        )
        self._frames.append(captured)
        return captured

    def counters(self) -> dict[str, int]:
        """Return counters suitable for the host network session validator."""

        return {
            "capture_frames": len(self._frames),
            "capture_bytes": self.captured_bytes,
            "capture_drops": self.capture_drops,
        }

    def write_jsonl(self, path: Path) -> None:
        """Publish the validated capture records without replacing a file."""

        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(frame.as_dict(), sort_keys=True) + "\n"
            for frame in self._frames
        ).encode("utf-8")
        publish_new_file(path, payload, error_type=CaptureError)

    def write_pcap(self, path: Path, *, timestamp_origin_ns: int | None = None) -> None:
        """Publish a little-endian, microsecond-resolution Ethernet PCAP.

        Monotonic clocks are not UTC.  Unless an explicit origin is supplied,
        the first capture timestamp becomes PCAP second zero; the artifact is
        structurally standard but retains the monotonic origin in the session
        metadata rather than inventing wall-clock time.
        """

        write_pcap(self._frames, path, timestamp_origin_ns=timestamp_origin_ns)


def write_pcap(
    frames: Iterable[CapturedFrame],
    path: Path,
    *,
    timestamp_origin_ns: int | None = None,
) -> None:
    """Write validated Ethernet frames using the classic PCAP file format."""

    records = tuple(frames)
    if timestamp_origin_ns is None:
        timestamp_origin_ns = records[0].monotonic_ns if records else 0
    timestamp_origin_ns = _require_int(
        timestamp_origin_ns,
        "timestamp_origin_ns",
        maximum=(1 << 64) - 1,
    )
    for frame in records:
        if frame.monotonic_ns < timestamp_origin_ns:
            raise CaptureError("PCAP timestamp origin is after a frame timestamp")
    encoded = io.BytesIO()
    encoded.write(
        struct.pack(
            "<IHHIIII",
            0xA1B2C3D4,
            2,
            4,
            0,
            0,
            MAX_ETHERNET_FRAME_LENGTH,
            PCAP_LINKTYPE_ETHERNET,
        )
    )
    previous_timestamp: tuple[int, int] | None = None
    for frame in records:
        relative_ns = frame.monotonic_ns - timestamp_origin_ns
        seconds, remainder_ns = divmod(relative_ns, 1_000_000_000)
        microseconds = remainder_ns // 1_000
        if seconds > 0xFFFFFFFF:
            raise CaptureError("PCAP timestamp seconds exceed the standard u32 field")
        timestamp = (seconds, microseconds)
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise CaptureError("PCAP timestamps moved backwards")
        previous_timestamp = timestamp
        frame_bytes = frame.frame
        encoded.write(
            struct.pack(
                "<IIII",
                seconds,
                microseconds,
                len(frame_bytes),
                len(frame_bytes),
            )
        )
        encoded.write(frame_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    publish_new_file(path, encoded.getvalue(), error_type=CaptureError)


def read_pcap(path: Path) -> tuple[tuple[int, int, bytes], ...]:
    """Read and strictly validate a classic Ethernet PCAP artifact."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise CaptureError(f"cannot read PCAP {path}: {error}") from error
    if len(raw) < 24:
        raise CaptureError("PCAP global header is truncated")
    magic = raw[:4]
    if magic == b"\xd4\xc3\xb2\xa1":
        endian = "<"
    elif magic == b"\xa1\xb2\xc3\xd4":
        endian = ">"
    else:
        raise CaptureError("unsupported PCAP magic")
    version_major, version_minor, timezone, sigfigs, snaplen, linktype = struct.unpack(
        f"{endian}HHIIII", raw[4:24]
    )
    if (version_major, version_minor) != (2, 4):
        raise CaptureError("PCAP version must be 2.4")
    if timezone != 0 or sigfigs != 0:
        raise CaptureError("PCAP timezone and sigfigs must be zero")
    if snaplen < MAX_ETHERNET_FRAME_LENGTH or linktype != PCAP_LINKTYPE_ETHERNET:
        raise CaptureError("PCAP must be Ethernet with a 1514-byte snaplen")
    records: list[tuple[int, int, bytes]] = []
    offset = 24
    previous_timestamp: tuple[int, int] | None = None
    while offset < len(raw):
        if len(raw) - offset < 16:
            raise CaptureError("PCAP packet header is truncated")
        seconds, microseconds, included_length, original_length = struct.unpack(
            f"{endian}IIII", raw[offset : offset + 16]
        )
        offset += 16
        if microseconds >= 1_000_000:
            raise CaptureError("PCAP microsecond field is out of range")
        if included_length > original_length or included_length > snaplen:
            raise CaptureError("PCAP packet lengths are invalid")
        if len(raw) - offset < included_length:
            raise CaptureError("PCAP packet bytes are truncated")
        timestamp = (seconds, microseconds)
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise CaptureError("PCAP timestamps moved backwards")
        previous_timestamp = timestamp
        records.append((seconds, microseconds, raw[offset : offset + included_length]))
        offset += included_length
    return tuple(records)


def _decode_frame(record: Mapping[str, Any], location: str) -> bytes:
    frame_hex = record.get("frame_hex")
    frame_base64 = record.get("frame_base64")
    if (frame_hex is None) == (frame_base64 is None):
        raise CaptureError(f"{location}: exactly one of frame_hex/frame_base64 is required")
    try:
        if frame_hex is not None:
            if not isinstance(frame_hex, str) or len(frame_hex) % 2:
                raise ValueError("frame_hex must be an even-length string")
            return bytes.fromhex(frame_hex)
        if not isinstance(frame_base64, str):
            raise ValueError("frame_base64 must be a string")
        return base64.b64decode(frame_base64, validate=True)
    except (ValueError, binascii.Error) as error:
        raise CaptureError(f"{location}: invalid frame encoding: {error}") from error


def iter_jsonl_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield strict JSON objects from a capture input stream."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise CaptureError(f"cannot read capture input {path}: {error}") from error
    for line_number, line in enumerate(lines, start=1):
        location = f"{path}:{line_number}"
        if not line.strip():
            raise CaptureError(f"{location}: blank lines are not allowed")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise CaptureError(f"{location}: invalid JSON: {error.msg}") from error
        if not isinstance(value, dict):
            raise CaptureError(f"{location}: record must be an object")
        yield value


def _capture_from_jsonl(input_path: Path, max_frames: int, max_frame_length: int) -> FrameCapture:
    capture: FrameCapture | None = None
    for line_number, record in enumerate(iter_jsonl_records(input_path), start=1):
        location = f"{input_path}:{line_number}"
        if capture is None:
            run_id = record.get("run_id")
            capture = FrameCapture(
                max_frames,
                max_frame_length=max_frame_length,
                run_id=run_id if isinstance(run_id, str) else "",
            )
        try:
            capture.record(
                _decode_frame(record, location),
                run_id=record.get("run_id"),
                session_id=record["session_id"],
                ingress_port=record["ingress_port"],
                generation=record["generation"],
                direction=record["direction"],
                monotonic_ns=record["monotonic_ns"],
            )
        except (KeyError, CaptureError) as error:
            raise CaptureError(f"{location}: {error}") from error
    if capture is None:
        raise CaptureError("capture input is empty")
    return capture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="JSONL frame input")
    parser.add_argument("--output-pcap", required=True, type=Path)
    parser.add_argument("--output-frames", type=Path)
    parser.add_argument("--max-frames", type=int, default=256)
    parser.add_argument("--max-frame-length", type=int, default=MAX_ETHERNET_FRAME_LENGTH)
    parser.add_argument("--timestamp-origin-ns", type=int)
    args = parser.parse_args(argv)
    try:
        capture = _capture_from_jsonl(args.input, args.max_frames, args.max_frame_length)
        capture.write_pcap(args.output_pcap, timestamp_origin_ns=args.timestamp_origin_ns)
        if args.output_frames is not None:
            capture.write_jsonl(args.output_frames)
    except (CaptureError, OSError) as error:
        print(f"P4 network capture failed: {error}", file=sys.stderr)
        return 1
    print(
        "P4_NETWORK_CAPTURE_PASS "
        f"frames={len(capture.frames)} capture_drops={capture.capture_drops}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
