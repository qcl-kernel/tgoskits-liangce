#!/usr/bin/env python3
"""Contract tests for P5 trajectory metric recomputation."""

from __future__ import annotations

import csv
import os
import secrets
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "scripts" / "contest" / "ai"
CONTRACT_TEST_ROOT = ROOT / "target" / "contract-tests"
sys.path.insert(0, str(AI_DIR))

import compute_metrics  # noqa: E402


def make_contract_directory(prefix: str) -> Path:
    CONTRACT_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    directory = CONTRACT_TEST_ROOT / f"{prefix}-{os.getpid()}-{secrets.token_hex(8)}"
    directory.mkdir()
    return directory


def main() -> int:
    rows = [
        {
            "controller": "fixed",
            "seed": "7",
            "sample_index": str(index),
            "measured_mC": str(54_000 + index * 500),
            "closed_loop_rtt_ns": str(100 + index * 100),
            "action_latency_ns": str(50 + index * 10),
        }
        for index in range(4)
    ]
    result = compute_metrics.compute_metrics(rows, target_mC=55_000, band_mC=1_000, hold_ticks=2)
    group = result["groups"][0]
    assert group["sample_count"] == 4
    assert group["overshoot_mC"] == 500
    assert group["settling_tick"] == 0
    assert group["closed_loop_rtt_ns"]["p99_ns"] == 400
    assert group["action_latency_ns"]["max_ns"] == 80

    path = make_contract_directory("p5-metrics-contract") / "trajectory.csv"
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    broken = dict(rows[0])
    del broken["measured_mC"]
    try:
        compute_metrics.compute_metrics([broken])
    except ValueError:
        pass
    else:
        raise AssertionError("metrics accepted a row without measured_mC")
    try:
        compute_metrics.compute_metrics([])
    except ValueError:
        pass
    else:
        raise AssertionError("metrics accepted an empty trajectory")
    print("P5_METRICS_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"P5 metrics contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
