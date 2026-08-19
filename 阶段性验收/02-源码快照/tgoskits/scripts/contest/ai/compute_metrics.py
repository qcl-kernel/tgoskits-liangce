#!/usr/bin/env python3
"""Recompute P5 controller metrics from a raw trajectory CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


def nearest_rank(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def settling_tick(errors: list[int], band_mC: int, hold_ticks: int) -> int | None:
    if len(errors) < hold_ticks:
        return None
    for index in range(len(errors) - hold_ticks + 1):
        if all(abs(error) <= band_mC for error in errors[index : index + hold_ticks]):
            return index
    return None


def compute_metrics(rows: list[dict[str, str]], target_mC: int = 55_000, band_mC: int = 1_000, hold_ticks: int = 50) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        controller = row.get("controller")
        seed = row.get("seed")
        if not controller or not seed:
            raise ValueError("trajectory rows require controller and seed")
        for field in ("sample_index", "measured_mC"):
            if not row.get(field):
                raise ValueError(f"trajectory row is missing {field}")
            int(row[field])
        groups[(controller, seed)].append(row)

    results = []
    for (controller, seed), group in sorted(groups.items()):
        group.sort(key=lambda row: int(row["sample_index"]))
        errors = [target_mC - int(row["measured_mC"]) for row in group]
        squared = [error * error for error in errors]
        rtts = [int(row["closed_loop_rtt_ns"]) for row in group if row.get("closed_loop_rtt_ns")]
        action_latencies = [int(row["action_latency_ns"]) for row in group if row.get("action_latency_ns")]
        overshoot = max(0, max(int(row["measured_mC"]) for row in group) - target_mC)
        results.append(
            {
                "controller": controller,
                "seed": int(seed),
                "sample_count": len(group),
                "rmse_mC": math.sqrt(sum(squared) / len(squared)),
                "iae_mC_s": sum(abs(error) for error in errors) * 0.1,
                "mae_mC": sum(abs(error) for error in errors) / len(errors),
                "overshoot_mC": overshoot,
                "settling_tick": settling_tick(errors, band_mC, hold_ticks),
                "closed_loop_rtt_ns": {
                    "n": len(rtts),
                    "p99_ns": nearest_rank(rtts, 0.99),
                    "max_ns": max(rtts) if rtts else None,
                },
                "action_latency_ns": {
                    "n": len(action_latencies),
                    "p99_ns": nearest_rank(action_latencies, 0.99),
                    "max_ns": max(action_latencies) if action_latencies else None,
                },
            }
        )
    return {
        "schema_version": "p5-metrics-v1",
        "target_mC": target_mC,
        "error_band_mC": band_mC,
        "settling_hold_ticks": hold_ticks,
        "groups": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-mC", type=int, default=55_000)
    parser.add_argument("--band-mC", type=int, default=1_000)
    parser.add_argument("--hold-ticks", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError(f"output already exists: {args.output}")
        with args.input.open(newline="", encoding="utf-8") as source:
            result = compute_metrics(
                list(csv.DictReader(source)), args.target_mC, args.band_mC, args.hold_ticks
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError, csv.Error) as error:
        print(f"P5 metrics computation failed: {error}")
        return 1
    print("P5_METRICS_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
