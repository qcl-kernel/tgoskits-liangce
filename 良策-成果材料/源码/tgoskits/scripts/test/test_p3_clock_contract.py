#!/usr/bin/env python3
"""Unit tests for the P3 clock.json and raw-event mapping contract."""

from __future__ import annotations

import json
import secrets
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RT_DIR = ROOT / "scripts" / "contest" / "rt"
sys.path.insert(0, str(RT_DIR))

import clock_contract  # noqa: E402
import validate_rt_events  # noqa: E402


def make_clock(offset: int = 100) -> dict[str, Any]:
    return {
        "schema_version": "p3-clock-v1",
        "counter_frequency_hz": 1_000_000_000,
        "host_counter": "CNTPCT_EL0",
        "guest_counter": "CNTVCT_EL0",
        "cntvoff_ticks_by_vcpu": {"2:0": offset},
        "mapping": "host_ticks = guest_ticks + cntvoff_ticks",
        "calibration_samples": [
            {
                "vm_id": 2,
                "vcpu_id": 0,
                "host_counter_ticks": 300,
                "guest_counter_ticks": 200,
                "residual_ns": 0,
            }
        ],
        "max_mapping_residual_ns": 0,
        "clock_mapping_id": "clock-contract-1",
    }


def make_event(raw_ticks: int = 200, monotonic_ns: int = 300) -> dict[str, Any]:
    return {
        "schema_version": "p3-rt-event-v1",
        "run_id": "clock-run",
        "endpoint": "zephyr",
        "scenario": "RT-NATIVE",
        "transport": None,
        "session_id": None,
        "sequence": None,
        "request_id": None,
        "sample_index": 0,
        "event": "period_start",
        "monotonic_ns": monotonic_ns,
        "value": None,
        "unit": "ns",
        "outcome": "observed",
        "clock_domain": "guest",
        "raw_counter_ticks": raw_ticks,
        "counter_frequency_hz": 1_000_000_000,
        "clock_mapping_id": "clock-contract-1",
        "vm_id": 2,
        "vcpu_id": 0,
        "pcpu_id": 2,
        "interrupt_id": None,
        "path_variant": "passthrough",
        "path_id": "guest-period",
        "feature_state": "off",
        "trace_sequence": 0,
    }


def make_contract_root() -> Path:
    root = ROOT / "target" / "contract-tests" / f"p3-clock-{secrets.token_hex(8)}"
    root.mkdir(parents=True)
    return root


class P3ClockContractTests(unittest.TestCase):
    def test_integer_mapping_and_calibration_residual(self) -> None:
        summary = clock_contract.validate_clock_document(make_clock())
        self.assertEqual(summary["calibration_sample_count"], 1)
        self.assertEqual(
            clock_contract.map_counter_to_monotonic_ns(200, 1_000_000_000, 100),
            300,
        )

    def test_rejects_unknown_mapping_and_duplicate_sample(self) -> None:
        wrong_mapping = make_clock()
        wrong_mapping["mapping"] = "host_ticks = guest_ticks"
        with self.assertRaises(clock_contract.ClockContractError):
            clock_contract.validate_clock_document(wrong_mapping)

        duplicate = make_clock()
        duplicate["calibration_samples"].append(
            duplicate["calibration_samples"][0].copy()
        )
        with self.assertRaises(clock_contract.ClockContractError):
            clock_contract.validate_clock_document(duplicate)

        residual_over_threshold = make_clock()
        residual_over_threshold["calibration_samples"][0]["host_counter_ticks"] = 301
        residual_over_threshold["calibration_samples"][0]["residual_ns"] = 1
        with self.assertRaises(clock_contract.ClockContractError):
            clock_contract.validate_clock_document(residual_over_threshold)

        duplicate_mapping = make_clock()
        duplicate_mapping["cntvoff_ticks_by_vcpu"]["02:00"] = 100
        with self.assertRaises(clock_contract.ClockContractError):
            clock_contract.validate_clock_document(duplicate_mapping)

    def test_validate_run_recomputes_event_timestamp(self) -> None:
        schema = RT_DIR / "schema" / "p3-rt-event-v1.schema.json"
        root = make_contract_root()
        run = root / "run"
        events = run / "events"
        events.mkdir(parents=True)
        (events / "zephyr.jsonl").write_text(
            json.dumps(make_event(), sort_keys=True) + "\n", encoding="utf-8"
        )
        clock_path = root / "clock.json"
        clock_path.write_text(
            json.dumps(make_clock(), sort_keys=True) + "\n", encoding="utf-8"
        )
        result = validate_rt_events.validate_run(run, schema, clock_path)
        self.assertEqual(result["clock"]["calibration_sample_count"], 1)

        wrong_event = make_event(monotonic_ns=299)
        (events / "zephyr.jsonl").write_text(
            json.dumps(wrong_event, sort_keys=True) + "\n", encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            validate_rt_events.validate_run(run, schema, clock_path)

    def test_rejects_unbound_vcpu_and_empty_required_calibration(self) -> None:
        document = make_clock()
        document["calibration_samples"] = []
        with self.assertRaises(clock_contract.ClockContractError):
            clock_contract.validate_clock_document(document, require_calibration=True)

        event = make_event()
        event["vcpu_id"] = 1
        with self.assertRaises(clock_contract.ClockContractError):
            clock_contract.validate_events_against_clock([event], make_clock())

    def test_rejects_non_nanosecond_event_unit(self) -> None:
        invalid = make_event()
        invalid["unit"] = "ms"
        with self.assertRaises(ValueError):
            validate_rt_events._validate_event(invalid, "fixture")


if __name__ == "__main__":
    unittest.main()
