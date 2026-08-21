#!/usr/bin/env python3
"""Contract tests for the P3 event validator."""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RT_DIR = ROOT / "scripts" / "contest" / "rt"
sys.path.insert(0, str(RT_DIR))

import validate_rt_events  # noqa: E402


def make_contract_root() -> Path:
    root = ROOT / "target" / "contract-tests" / f"p3-event-{secrets.token_hex(8)}"
    root.mkdir(parents=True)
    return root


def event(run_id: str, trace_sequence: int, monotonic_ns: int) -> dict[str, object]:
    return {
        "schema_version": "p3-rt-event-v1",
        "run_id": run_id,
        "endpoint": "zephyr",
        "scenario": "RT-NATIVE",
        "transport": None,
        "session_id": None,
        "sequence": None,
        "request_id": None,
        "sample_index": trace_sequence,
        "event": "period_start",
        "monotonic_ns": monotonic_ns,
        "value": None,
        "unit": "ns",
        "outcome": "observed",
        "clock_domain": "guest",
        "raw_counter_ticks": monotonic_ns,
        "counter_frequency_hz": 1_000_000_000,
        "clock_mapping_id": "clock-1",
        "vm_id": 2,
        "vcpu_id": 0,
        "pcpu_id": 2,
        "interrupt_id": None,
        "path_variant": "passthrough",
        "path_id": "guest-period",
        "feature_state": "off",
        "trace_sequence": trace_sequence,
    }


def write_run(root: Path, events: list[dict[str, object]]) -> Path:
    run = root / "run"
    (run / "events").mkdir(parents=True)
    (run / "events" / "zephyr.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in events),
        encoding="utf-8",
    )
    return run


def main() -> int:
    schema = RT_DIR / "schema" / "p3-rt-event-v1.schema.json"
    root = make_contract_root()
    valid_run = write_run(root, [event("run-1", 0, 0), event("run-1", 1, 100)])
    result = validate_rt_events.validate_run(valid_run, schema)
    assert result["valid"] is True
    assert result["event_count"] == 2

    invalid_run = write_run(
        root / "invalid", [event("run-2", 0, 100), event("run-2", 1, 99)]
    )
    try:
        validate_rt_events.validate_run(invalid_run, schema)
    except ValueError:
        pass
    else:
        raise AssertionError("validator accepted backwards monotonic time")

    missing_run = root / "missing"
    (missing_run / "events").mkdir(parents=True)
    (missing_run / "events" / "bad.jsonl").write_text(
        json.dumps({"schema_version": "p3-rt-event-v1"}) + "\n",
        encoding="utf-8",
    )
    try:
        validate_rt_events.validate_run(missing_run, schema)
    except ValueError:
        pass
    else:
        raise AssertionError("validator accepted missing event fields")
    print("P3_RT_EVENT_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"P3 event contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
