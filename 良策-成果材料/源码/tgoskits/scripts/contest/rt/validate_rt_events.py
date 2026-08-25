#!/usr/bin/env python3
"""Fail-closed validation for the host-only P3 realtime event stream."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_vm_carveout_io import publish_new_file  # noqa: E402
import clock_contract  # noqa: E402


REQUIRED_FIELDS = (
    "schema_version",
    "run_id",
    "endpoint",
    "scenario",
    "transport",
    "session_id",
    "sequence",
    "request_id",
    "sample_index",
    "event",
    "monotonic_ns",
    "value",
    "unit",
    "outcome",
    "clock_domain",
    "raw_counter_ticks",
    "counter_frequency_hz",
    "clock_mapping_id",
    "vm_id",
    "vcpu_id",
    "pcpu_id",
    "interrupt_id",
    "path_variant",
    "path_id",
    "feature_state",
    "trace_sequence",
)

EVENT_NAMES = {
    "period_release",
    "period_start",
    "period_finish",
    "guest_handler_enter",
    "timer_expiry",
    "virq_enqueue",
    "vcpu_wake_request",
    "vcpu_wake_complete",
    "pending_irq_drain",
    "vcpu_reentry",
    "warmup_end",
    "load_start",
    "load_stop",
    "trace_drop",
    "deadline_miss",
    "packet_send",
    "packet_receive",
    "inference_start",
    "inference_finish",
    "control_apply",
    "safe_enter",
    "safe_exit",
    "feedback_receive",
}
VALID_UNITS = {None, "ns"}


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_optional_nonnegative(value: Any, field: str, location: str) -> None:
    if value is not None and (not _is_integer(value) or value < 0):
        raise ValueError(f"{location}: {field} must be a non-negative integer or null")


def _check_string_or_null(value: Any, field: str, location: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{location}: {field} must be a non-empty string or null")


def _validate_event(event: dict[str, Any], location: str) -> None:
    missing = [field for field in REQUIRED_FIELDS if field not in event]
    if missing:
        raise ValueError(f"{location}: missing fields {', '.join(missing)}")
    if event["schema_version"] != "p3-rt-event-v1":
        raise ValueError(f"{location}: unsupported schema_version")
    for field in ("run_id", "endpoint", "scenario", "clock_domain", "clock_mapping_id", "path_variant", "path_id", "feature_state"):
        if not isinstance(event[field], str) or not event[field]:
            raise ValueError(f"{location}: {field} must be a non-empty string")
    if event["event"] not in EVENT_NAMES:
        raise ValueError(f"{location}: unknown event {event['event']!r}")
    if not _is_integer(event["monotonic_ns"]) or event["monotonic_ns"] < 0:
        raise ValueError(f"{location}: monotonic_ns must be a non-negative integer")
    if not _is_integer(event["raw_counter_ticks"]) or event["raw_counter_ticks"] < 0:
        raise ValueError(f"{location}: raw_counter_ticks must be a non-negative integer")
    if not _is_integer(event["counter_frequency_hz"]) or event["counter_frequency_hz"] <= 0:
        raise ValueError(f"{location}: counter_frequency_hz must be positive")
    if not _is_integer(event["trace_sequence"]) or event["trace_sequence"] < 0:
        raise ValueError(f"{location}: trace_sequence must be a non-negative integer")
    for field in ("session_id", "sequence", "request_id", "sample_index", "vm_id", "vcpu_id", "pcpu_id", "interrupt_id"):
        _check_optional_nonnegative(event[field], field, location)
    _check_string_or_null(event["transport"], "transport", location)
    _check_string_or_null(event["unit"], "unit", location)
    if event["unit"] not in VALID_UNITS:
        raise ValueError(f"{location}: unit must be ns or null")
    _check_string_or_null(event["outcome"], "outcome", location)
    if event["event"] == "trace_drop":
        raise ValueError(f"{location}: trace_drop is not admissible in a valid run")


def read_events(run_directory: Path) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Read and validate every JSONL event file in a run directory."""

    event_directory = run_directory / "events"
    if not event_directory.is_dir():
        raise ValueError(f"missing events directory: {event_directory}")
    event_files = sorted(event_directory.glob("*.jsonl"))
    if not event_files:
        raise ValueError(f"no JSONL event files found in {event_directory}")

    run_id: str | None = None
    events: list[dict[str, Any]] = []
    for event_file in event_files:
        previous_monotonic_ns: int | None = None
        previous_trace_sequence: int | None = None
        try:
            lines = event_file.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise ValueError(f"cannot read {event_file}: {error}") from error
        if not lines:
            raise ValueError(f"empty event file: {event_file}")
        for line_number, line in enumerate(lines, start=1):
            location = f"{event_file}:{line_number}"
            if not line.strip():
                raise ValueError(f"{location}: blank lines are not allowed")
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{location}: invalid JSON: {error.msg}") from error
            if not isinstance(event, dict):
                raise ValueError(f"{location}: event must be a JSON object")
            _validate_event(event, location)
            if run_id is None:
                run_id = event["run_id"]
            elif event["run_id"] != run_id:
                raise ValueError(f"{location}: run_id differs from the event stream")
            if previous_monotonic_ns is not None and event["monotonic_ns"] < previous_monotonic_ns:
                raise ValueError(f"{location}: monotonic_ns moved backwards")
            if previous_trace_sequence is not None and event["trace_sequence"] <= previous_trace_sequence:
                raise ValueError(f"{location}: trace_sequence is not strictly increasing")
            previous_monotonic_ns = event["monotonic_ns"]
            previous_trace_sequence = event["trace_sequence"]
            events.append(event)
    assert run_id is not None
    return run_id, events, [str(path.relative_to(run_directory)) for path in event_files]


def validate_schema_file(schema_path: Path) -> None:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid schema file {schema_path}: {error}") from error
    if schema.get("$id") != "p3-rt-event-v1":
        raise ValueError("schema $id must be p3-rt-event-v1")
    if schema.get("required") != list(REQUIRED_FIELDS):
        raise ValueError("schema required fields do not match the validator")


def validate_run(
    run_directory: Path,
    schema_path: Path,
    clock_path: Path | None = None,
    *,
    require_calibration: bool = False,
) -> dict[str, Any]:
    validate_schema_file(schema_path)
    run_id, events, event_files = read_events(run_directory)
    result: dict[str, Any] = {
        "schema_version": "p3-rt-validation-v1",
        "run_id": run_id,
        "event_files": event_files,
        "event_count": len(events),
        "valid": True,
    }
    if clock_path is not None:
        try:
            clock_document = json.loads(clock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid clock file {clock_path}: {error}") from error
        clock_summary = clock_contract.validate_clock_document(
            clock_document,
            require_calibration=require_calibration,
            location=str(clock_path),
        )
        event_summary = clock_contract.validate_events_against_clock(
            events, clock_document, location="events"
        )
        result["clock"] = {
            "path": str(clock_path),
            "schema_version": clock_summary["schema_version"],
            "clock_mapping_id": event_summary["clock_mapping_id"],
            "calibration_sample_count": event_summary["calibration_sample_count"],
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--clock", type=Path)
    parser.add_argument("--require-calibration", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = validate_run(
            args.run,
            args.schema,
            args.clock,
            require_calibration=args.require_calibration,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            publish_new_file(
                args.output,
                (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                error_type=ValueError,
            )
    except (OSError, ValueError) as error:
        print(f"P3 realtime event contract failed: {error}")
        return 1
    print("P3_RT_EVENT_SCHEMA_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
