#!/usr/bin/env python3
"""Turn AxVisor final-Guest-DTB markers into a bounded capture plan.

This tool does not issue QMP commands or claim that bytes were captured.  It
validates the physical ranges before a runner is allowed to call ``pmemsave``.
"""

from __future__ import annotations

import argparse
import json
import re
import runpy
import sys
from pathlib import Path
from typing import Any


MARKER_PREFIX = "AXVISOR_GUEST_DTB_READY"
MAX_DTB_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_U64 = (1 << 64) - 1
MARKER_PATTERN = re.compile(
    r"^AXVISOR_GUEST_DTB_READY "
    r"vm=(?P<vm>[1-9][0-9]*) "
    r"gpa=(?P<gpa>0x[0-9a-fA-F]+) "
    r"size=(?P<size>[0-9]+) "
    r"hpa_segments=(?P<segments>[^\s]+)$"
)
SEGMENT_PATTERN = re.compile(
    r"^(?P<hpa>0x[0-9a-fA-F]+):(?P<length>[0-9]+)$"
)
IO_API = runpy.run_path(str(Path(__file__).with_name("guest_dtb_capture_io.py")))
IoContractError = IO_API["GuestDtbCaptureError"]
MAX_SOURCE_LOG_BYTES = int(IO_API["MAX_SOURCE_LOG_BYTES"])
_prepare_cli_paths = IO_API["prepare_cli_paths"]
_publish_json_new = IO_API["publish_json_new"]
_read_bounded_regular_file = IO_API["read_bounded_regular_file"]
_sha256_bytes = IO_API["sha256_bytes"]


class GuestDtbMarkerError(ValueError):
    """The runtime marker stream cannot produce a safe capture plan."""


def _parse_u64(value: str, *, base: int, field: str) -> int:
    try:
        parsed = int(value, base)
    except ValueError as error:
        raise GuestDtbMarkerError(f"invalid {field}: {value}") from error
    if parsed < 0 or parsed > MAX_U64:
        raise GuestDtbMarkerError(f"{field} is outside the 64-bit address space")
    return parsed


def _parse_segments(text: str, *, vm_id: int, line_number: int) -> list[dict[str, int]]:
    segments: list[dict[str, int]] = []
    for index, raw_segment in enumerate(text.split(",")):
        match = SEGMENT_PATTERN.fullmatch(raw_segment)
        if match is None:
            raise GuestDtbMarkerError(
                f"malformed HPA segment for VM {vm_id} on line {line_number}: "
                f"{raw_segment!r}"
            )
        segments.append(
            {
                "hpa": _parse_u64(
                    match.group("hpa"),
                    base=16,
                    field=f"VM {vm_id} segment {index} HPA",
                ),
                "length": _parse_u64(
                    match.group("length"),
                    base=10,
                    field=f"VM {vm_id} segment {index} length",
                ),
            }
        )
    if not segments:
        raise GuestDtbMarkerError(f"VM {vm_id} marker has no physical segments")
    return segments


def parse_guest_dtb_markers(
    log_text: str, *, expected_vm_ids: set[int]
) -> list[dict[str, Any]]:
    """Parse exactly one strict marker for every expected VM."""

    if not expected_vm_ids or any(vm_id <= 0 for vm_id in expected_vm_ids):
        raise GuestDtbMarkerError("expected VM ids must be distinct positive integers")

    markers: dict[int, dict[str, Any]] = {}
    for line_number, raw_line in enumerate(log_text.splitlines(), start=1):
        if MARKER_PREFIX not in raw_line:
            continue
        line = raw_line.strip()
        match = MARKER_PATTERN.fullmatch(line)
        if match is None:
            raise GuestDtbMarkerError(
                f"malformed Guest DTB marker on line {line_number}: {line!r}"
            )

        vm_id = _parse_u64(match.group("vm"), base=10, field="VM id")
        if vm_id not in expected_vm_ids:
            raise GuestDtbMarkerError(f"unexpected marker for VM {vm_id}")
        if vm_id in markers:
            raise GuestDtbMarkerError(f"duplicate marker for VM {vm_id}")

        markers[vm_id] = {
            "vmId": vm_id,
            "gpa": _parse_u64(
                match.group("gpa"), base=16, field=f"VM {vm_id} Guest DTB GPA"
            ),
            "size": _parse_u64(
                match.group("size"), base=10, field=f"VM {vm_id} Guest DTB size"
            ),
            "segments": _parse_segments(
                match.group("segments"), vm_id=vm_id, line_number=line_number
            ),
            "sourceLine": line_number,
        }

    missing = sorted(expected_vm_ids - markers.keys())
    if missing:
        formatted = ", ".join(str(vm_id) for vm_id in missing)
        raise GuestDtbMarkerError(f"missing markers for VM ids: {formatted}")
    return [markers[vm_id] for vm_id in sorted(markers)]


def _checked_range_end(start: int, length: int, *, label: str) -> int:
    if length <= 0:
        raise GuestDtbMarkerError(f"{label} length must be positive")
    end = start + length
    if end > MAX_U64 + 1:
        raise GuestDtbMarkerError(f"{label} overflows 64-bit address space")
    return end


def validate_capture_ranges(markers: list[dict[str, Any]]) -> None:
    """Reject unbounded, truncated, overflowing, or overlapping captures."""

    seen_vm_ids: set[int] = set()
    physical_ranges: list[tuple[int, int, int, int]] = []
    for marker in markers:
        vm_id = int(marker["vmId"])
        if vm_id in seen_vm_ids:
            raise GuestDtbMarkerError(f"duplicate marker for VM {vm_id}")
        seen_vm_ids.add(vm_id)

        size = int(marker["size"])
        if size <= 0:
            raise GuestDtbMarkerError(f"VM {vm_id} DTB size must be positive")
        if size > MAX_DTB_CAPTURE_BYTES:
            raise GuestDtbMarkerError(
                f"VM {vm_id} DTB size {size} exceeds capture limit "
                f"{MAX_DTB_CAPTURE_BYTES}"
            )
        _checked_range_end(
            int(marker["gpa"]), size, label=f"VM {vm_id} Guest DTB GPA range"
        )

        segments = marker.get("segments")
        if not isinstance(segments, list) or not segments:
            raise GuestDtbMarkerError(f"VM {vm_id} marker has no physical segments")
        total = 0
        for index, segment in enumerate(segments):
            hpa = int(segment["hpa"])
            length = int(segment["length"])
            end = _checked_range_end(
                hpa, length, label=f"VM {vm_id} segment {index} HPA range"
            )
            total += length
            physical_ranges.append((hpa, end, vm_id, index))
        if total != size:
            raise GuestDtbMarkerError(
                f"VM {vm_id} segment lengths total {total}, expected {size}"
            )

    physical_ranges.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(physical_ranges, physical_ranges[1:]):
        if current[0] < previous[1]:
            raise GuestDtbMarkerError(
                "physical capture ranges overlap: "
                f"VM {previous[2]} segment {previous[3]} and "
                f"VM {current[2]} segment {current[3]}"
            )


def build_capture_plan(
    *,
    markers: list[dict[str, Any]],
    source_log_name: str,
    source_log_sha256: str,
) -> dict[str, Any]:
    """Build a command-free capture plan for a QMP-aware runner."""

    validate_capture_ranges(markers)
    if not source_log_name or Path(source_log_name).name != source_log_name:
        raise GuestDtbMarkerError("source log name must be a plain filename")
    if re.fullmatch(r"[0-9a-f]{64}", source_log_sha256) is None:
        raise GuestDtbMarkerError("source log SHA-256 must be lowercase hexadecimal")

    guests: list[dict[str, Any]] = []
    for marker in sorted(markers, key=lambda item: int(item["vmId"])):
        vm_id = int(marker["vmId"])
        segments = []
        for index, segment in enumerate(marker["segments"]):
            segments.append(
                {
                    "index": index,
                    "hpa": f"{int(segment['hpa']):#x}",
                    "length": int(segment["length"]),
                    "fileName": f"guest-vm-{vm_id}.segment-{index:03d}.bin",
                    "qmpOperation": "pmemsave",
                }
            )
        guests.append(
            {
                "vmId": vm_id,
                "gpa": f"{int(marker['gpa']):#x}",
                "size": int(marker["size"]),
                "sourceLine": int(marker["sourceLine"]),
                "assembledFileName": f"guest-vm-{vm_id}.final.dtb",
                "segments": segments,
            }
        )

    return {
        "schemaVersion": 1,
        "artifactStatus": "marker-derived-unreviewed",
        "status": "capture_planned",
        "proofScope": "final-guest-dtb-physical-capture-plan",
        "doesNotProve": [
            "physical bytes were captured",
            "captured bytes form valid flattened device trees",
            "both guests booted",
            "passthrough DMA is isolated",
            "Linux and Zephyr have IP connectivity",
        ],
        "sourcePathBase": "capture-plan.json parent directory",
        "source": {
            "axvisorLog": {
                "path": source_log_name,
                "sha256": source_log_sha256,
            }
        },
        "guests": guests,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate final Guest DTB runtime markers and emit a bounded, "
            "command-free physical capture plan"
        )
    )
    parser.add_argument("--log", required=True, type=Path, help="AxVisor runtime log")
    parser.add_argument(
        "--expected-vm",
        required=True,
        type=int,
        action="append",
        dest="expected_vms",
        help="expected positive VM id; repeat once per Guest",
    )
    parser.add_argument("--output", required=True, type=Path, help="capture-plan JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        log_path, output_path = _prepare_cli_paths(args.log, args.output)
        expected_vm_ids = set(args.expected_vms)
        if len(expected_vm_ids) != len(args.expected_vms):
            raise GuestDtbMarkerError("expected VM ids must be distinct positive integers")
        log_data, _ = _read_bounded_regular_file(
            log_path,
            byte_limit=MAX_SOURCE_LOG_BYTES,
            field="AxVisor log",
        )
        log_text = log_data.decode("utf-8", errors="strict")
        markers = parse_guest_dtb_markers(
            log_text, expected_vm_ids=expected_vm_ids
        )
        validate_capture_ranges(markers)
        payload = build_capture_plan(
            markers=markers,
            source_log_name=log_path.name,
            source_log_sha256=_sha256_bytes(log_data),
        )
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        _publish_json_new(payload, output_path)
    except GuestDtbMarkerError as error:
        print(f"Final Guest DTB capture planning failed: {error}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, IoContractError) as error:
        print(f"Could not read/write Guest DTB evidence: {error}", file=sys.stderr)
        return 2

    print(serialized, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
