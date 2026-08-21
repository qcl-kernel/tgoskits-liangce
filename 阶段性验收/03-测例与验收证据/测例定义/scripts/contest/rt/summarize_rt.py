#!/usr/bin/env python3
"""Recompute P3 latency and jitter summaries from validated JSONL events."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import validate_rt_events


def nearest_rank(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def metric(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {
            "n": 0,
            "mean_ns": None,
            "min_ns": None,
            "max_ns": None,
            "p99_ns": None,
            "p99_9_ns": None,
        }
    return {
        "n": len(values),
        "mean_ns": sum(values) // len(values),
        "min_ns": min(values),
        "max_ns": max(values),
        "p99_ns": nearest_rank(values, 0.99),
        "p99_9_ns": nearest_rank(values, 0.999),
    }


def summarize_group(events: list[dict[str, Any]], period_ns: int) -> dict[str, Any]:
    by_sample: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for event in events:
        sample_index = event["sample_index"]
        if sample_index is None:
            continue
        event_name = event["event"]
        if event_name in by_sample[sample_index]:
            raise ValueError(
                f"duplicate {event_name} for sample_index={sample_index}"
            )
        by_sample[sample_index][event_name] = event

    starts = [
        by_sample[index]["period_start"]["monotonic_ns"]
        for index in sorted(by_sample)
        if "period_start" in by_sample[index]
    ]
    releases = {
        index: sample["period_release"]["monotonic_ns"]
        for index, sample in by_sample.items()
        if "period_release" in sample
    }
    starts_by_index = {
        index: sample["period_start"]["monotonic_ns"]
        for index, sample in by_sample.items()
        if "period_start" in sample
    }
    finishes_by_index = {
        index: sample["period_finish"]["monotonic_ns"]
        for index, sample in by_sample.items()
        if "period_finish" in sample
    }
    jitter = [
        starts[index] - starts[index - 1] - period_ns
        for index in range(1, len(starts))
    ]
    absolute_jitter = [abs(value) for value in jitter]
    wakeup_latency = [
        starts_by_index[index] - release_time
        for index, release_time in releases.items()
        if index in starts_by_index
    ]
    execution_time = [
        finishes_by_index[index] - starts_by_index[index]
        for index in starts_by_index
        if index in finishes_by_index
    ]
    deadline_miss_count = sum(
        1 for event in events if event["event"] == "deadline_miss"
    )
    return {
        "sample_count": len(starts),
        "period_jitter_ns": metric(jitter),
        "absolute_jitter_ns": metric(absolute_jitter),
        "wakeup_latency_ns": metric(wakeup_latency),
        "execution_time_ns": metric(execution_time),
        "deadline_miss_count": deadline_miss_count,
    }


def summarize_run(run_directory: Path, schema_path: Path, period_ns: int) -> dict[str, Any]:
    validation = validate_rt_events.validate_run(run_directory, schema_path)
    run_id, events, _ = validate_rt_events.read_events(run_directory)
    grouped: dict[tuple[str, str, int | None, int | None], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[
            (
                event["endpoint"],
                event["scenario"],
                event["vm_id"],
                event["vcpu_id"],
            )
        ].append(event)
    groups = []
    for (endpoint, scenario, vm_id, vcpu_id), group_events in sorted(grouped.items()):
        groups.append(
            {
                "endpoint": endpoint,
                "scenario": scenario,
                "vm_id": vm_id,
                "vcpu_id": vcpu_id,
                "metrics": summarize_group(group_events, period_ns),
            }
        )
    return {
        "schema_version": "p3-rt-summary-v1",
        "run_id": run_id,
        "period_ns": period_ns,
        "validation": validation,
        "groups": groups,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--period-ns", type=int, default=100_000_000)
    args = parser.parse_args(argv)
    try:
        if args.period_ns <= 0:
            raise ValueError("period must be positive")
        if args.output.exists():
            raise ValueError(f"output already exists: {args.output}")
        summary = summarize_run(args.run, args.schema, args.period_ns)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError) as error:
        print(f"P3 realtime statistics failed: {error}")
        return 1
    print("P3_RT_STATISTICS_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
