#!/usr/bin/env python3
"""Recompute P5 controller metrics from a raw trajectory CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_vm_carveout_io import publish_new_file  # noqa: E402


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


def compute_metrics(
    rows: list[dict[str, Any]],
    target_mC: int = 55_000,
    band_mC: int = 1_000,
    hold_ticks: int = 50,
    *,
    expected_ticks: int | None = None,
    recovery_start_tick: int = 900,
    overshoot_step_mC: int = 30_000,
) -> dict[str, Any]:
    if not rows:
        raise ValueError("trajectory must contain at least one row")
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        controller = row.get("controller")
        seed = row.get("seed")
        if controller in (None, "") or seed in (None, ""):
            raise ValueError("trajectory rows require controller and seed")
        for field in ("sample_index", "measured_mC"):
            if row.get(field) in (None, ""):
                raise ValueError(f"trajectory row is missing {field}")
            int(row[field])
        groups[(str(controller), str(seed))].append(row)

    results = []
    for (controller, seed), group in sorted(groups.items()):
        group.sort(key=lambda row: int(row["sample_index"]))
        sample_indices = [int(row["sample_index"]) for row in group]
        if expected_ticks is not None:
            if len(group) != expected_ticks:
                raise ValueError(
                    f"{controller}/seed-{seed} requires exactly {expected_ticks} samples, "
                    f"got {len(group)}"
                )
            if sample_indices != list(range(expected_ticks)):
                raise ValueError(
                    f"{controller}/seed-{seed} sample_index must be continuous 0..{expected_ticks - 1}"
                )
        errors = [target_mC - int(row["measured_mC"]) for row in group]
        squared = [error * error for error in errors]
        rtts = [
            int(row["closed_loop_rtt_ns"])
            for row in group
            if row.get("closed_loop_rtt_ns") not in (None, "")
        ]
        action_latencies = [
            int(row["action_latency_ns"])
            for row in group
            if row.get("action_latency_ns") not in (None, "")
        ]
        feedback = [
            int(row["feedback_confirmed"])
            for row in group
            if row.get("feedback_confirmed") not in (None, "")
        ]
        overshoot = max(0, max(int(row["measured_mC"]) for row in group) - target_mC)
        initial_window_end = min(recovery_start_tick, len(errors))
        initial_settling = settling_tick(errors[:initial_window_end], band_mC, hold_ticks)
        recovery_settling = settling_tick(errors[recovery_start_tick:], band_mC, hold_ticks)
        results.append(
            {
                "controller": controller,
                "seed": int(seed),
                "sample_count": len(group),
                "rmse_mC": math.sqrt(sum(squared) / len(squared)),
                "iae_mC_s": sum(abs(error) for error in errors) * 0.1,
                "mae_mC": sum(abs(error) for error in errors) / len(errors),
                "overshoot_mC": overshoot,
                "overshoot_percent": overshoot * 100.0 / overshoot_step_mC,
                "settling_tick": initial_settling,
                "initial_settling_tick": initial_settling,
                "recovery_settling_tick": recovery_settling,
                "success_rate": (
                    sum(1 for value in feedback if value == 1) / len(group)
                    if feedback
                    else None
                ),
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
        "expected_ticks": expected_ticks,
        "recovery_start_tick": recovery_start_tick,
        "overshoot_step_mC": overshoot_step_mC,
        "groups": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-mC", type=int, default=55_000)
    parser.add_argument("--band-mC", type=int, default=1_000)
    parser.add_argument("--hold-ticks", type=int, default=50)
    parser.add_argument("--expected-ticks", type=int, default=1_800)
    parser.add_argument("--recovery-start-tick", type=int, default=900)
    parser.add_argument("--overshoot-step-mC", type=int, default=30_000)
    args = parser.parse_args(argv)
    try:
        with args.input.open(newline="", encoding="utf-8") as source:
            result = compute_metrics(
                list(csv.DictReader(source)),
                args.target_mC,
                args.band_mC,
                args.hold_ticks,
                expected_ticks=args.expected_ticks,
                recovery_start_tick=args.recovery_start_tick,
                overshoot_step_mC=args.overshoot_step_mC,
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        publish_new_file(
            args.output,
            (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            error_type=ValueError,
        )
    except (OSError, ValueError, csv.Error) as error:
        print(f"P5 metrics computation failed: {error}")
        return 1
    print("P5_METRICS_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
