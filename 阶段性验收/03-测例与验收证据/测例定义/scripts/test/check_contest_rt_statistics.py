#!/usr/bin/env python3
"""Contract tests for recomputable P3 timing statistics."""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RT_DIR = ROOT / "scripts" / "contest" / "rt"
sys.path.insert(0, str(RT_DIR))

import summarize_rt  # noqa: E402


def make_contract_root() -> Path:
    root = ROOT / "target" / "contract-tests" / f"p3-statistics-{secrets.token_hex(8)}"
    root.mkdir(parents=True)
    return root


def make_event(
    sample_index: int, event_name: str, timestamp: int, sequence: int
) -> dict[str, object]:
    return {
        "schema_version": "p3-rt-event-v1",
        "run_id": "statistics-run",
        "endpoint": "zephyr",
        "scenario": "RT-NATIVE",
        "transport": None,
        "session_id": None,
        "sequence": None,
        "request_id": None,
        "sample_index": sample_index,
        "event": event_name,
        "monotonic_ns": timestamp,
        "value": None,
        "unit": "ns",
        "outcome": "observed",
        "clock_domain": "guest",
        "raw_counter_ticks": timestamp,
        "counter_frequency_hz": 1_000_000_000,
        "clock_mapping_id": "clock-statistics",
        "vm_id": 2,
        "vcpu_id": 0,
        "pcpu_id": 2,
        "interrupt_id": None,
        "path_variant": "passthrough",
        "path_id": "guest-period",
        "feature_state": "off",
        "trace_sequence": sequence,
    }


def main() -> int:
    schema = RT_DIR / "schema" / "p3-rt-event-v1.schema.json"
    root = make_contract_root()
    run = root / "run"
    (run / "events").mkdir(parents=True)
    records: list[dict[str, object]] = []
    sequence = 0
    starts = [1_000, 100_001_100, 200_000_900, 300_001_050]
    for sample_index, start in enumerate(starts):
        for event_name, timestamp in (
            ("period_release", start - 1_000),
            ("period_start", start),
            ("period_finish", start + 500),
        ):
            records.append(make_event(sample_index, event_name, timestamp, sequence))
            sequence += 1
    (run / "events" / "zephyr.jsonl").write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    output = root / "summary.json"
    assert summarize_rt.main(
        ["--run", str(run), "--schema", str(schema), "--output", str(output)]
    ) == 0
    summary = json.loads(output.read_text(encoding="utf-8"))
    metrics = summary["groups"][0]["metrics"]
    assert metrics["sample_count"] == 4
    assert metrics["absolute_jitter_ns"]["max_ns"] == 200
    assert metrics["wakeup_latency_ns"]["max_ns"] == 1_000
    assert metrics["execution_time_ns"]["max_ns"] == 500
    if summarize_rt.main(
        ["--run", str(run), "--schema", str(schema), "--output", str(output)]
    ) == 0:
        raise AssertionError("statistics command accepted an existing output")
    print("P3_RT_STATISTICS_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"P3 statistics contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
