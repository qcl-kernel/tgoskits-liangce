#!/usr/bin/env python3
"""Select P3 production candidates from a frozen discovery matrix.

The matrix format is intentionally small and host-only.  Each candidate must
contain three seed results with the native, AxVisor-off, and measured path
P99.9 values in nanoseconds.  This tool ranks only candidates that pass every
seed's relative or absolute threshold; it never changes a threshold after
observing the measurements.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


SEEDS = (7, 19, 43)
MIN_RELATIVE_CONTRIBUTION = 0.15
MIN_ABSOLUTE_CONTRIBUTION_NS = 20_000


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    if not math.isfinite(float(value)):
        raise ValueError(f"{field} must be finite")
    return float(value)


def _passes(relative: float | None, absolute_ns: float) -> bool:
    return absolute_ns >= MIN_ABSOLUTE_CONTRIBUTION_NS or (
        relative is not None and relative >= MIN_RELATIVE_CONTRIBUTION
    )


def select_candidates(matrix: dict[str, Any]) -> dict[str, Any]:
    if matrix.get("schema_version") != "p3-rt-matrix-v1":
        raise ValueError("matrix schema_version must be p3-rt-matrix-v1")
    candidates = matrix.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("matrix candidates must be a non-empty list")

    evaluated: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("each candidate must be an object")
        path_id = candidate.get("path_id")
        primary_metric = candidate.get("primary_metric")
        seed_results = candidate.get("seed_results")
        if not isinstance(path_id, str) or not path_id:
            raise ValueError("candidate path_id must be non-empty")
        if not isinstance(primary_metric, str) or not primary_metric:
            raise ValueError(f"{path_id}: primary_metric must be non-empty")
        if not isinstance(seed_results, list):
            raise ValueError(f"{path_id}: seed_results must be a list")
        by_seed = {result.get("seed"): result for result in seed_results if isinstance(result, dict)}
        if set(by_seed) != set(SEEDS):
            raise ValueError(f"{path_id}: seed_results must contain exactly {SEEDS}")

        evaluated_seeds: list[dict[str, Any]] = []
        rejection_reasons: list[str] = []
        for seed in SEEDS:
            result = by_seed[seed]
            native = _number(result.get("native_p99_9_ns"), f"{path_id}/{seed}/native_p99_9_ns")
            axvisor_off = _number(result.get("axvisor_off_p99_9_ns"), f"{path_id}/{seed}/axvisor_off_p99_9_ns")
            segment = _number(result.get("segment_p99_9_ns"), f"{path_id}/{seed}/segment_p99_9_ns")
            if native <= 0:
                raise ValueError(f"{path_id}/{seed}: native p99.9 must be positive")
            total_delta = axvisor_off - native
            total_relative = total_delta / native
            segment_relative = segment / total_delta if total_delta > 0 else None
            total_pass = _passes(total_relative, total_delta)
            segment_pass = _passes(segment_relative, segment)
            if not total_pass:
                rejection_reasons.append(f"seed {seed}: total degradation below threshold")
            if not segment_pass:
                rejection_reasons.append(f"seed {seed}: candidate contribution below threshold")
            evaluated_seeds.append(
                {
                    "seed": seed,
                    "native_p99_9_ns": native,
                    "axvisor_off_p99_9_ns": axvisor_off,
                    "segment_p99_9_ns": segment,
                    "total_delta_ns": total_delta,
                    "total_relative": total_relative,
                    "segment_relative": segment_relative,
                    "total_threshold_pass": total_pass,
                    "segment_threshold_pass": segment_pass,
                }
            )
        accepted = not rejection_reasons
        relative_values = [
            value["segment_relative"]
            for value in evaluated_seeds
            if value["segment_relative"] is not None
        ]
        absolute_values = [value["segment_p99_9_ns"] for value in evaluated_seeds]
        evaluated.append(
            {
                "path_id": path_id,
                "primary_metric": primary_metric,
                "accepted": accepted,
                "rejection_reasons": rejection_reasons,
                "seed_results": evaluated_seeds,
                "median_segment_relative": _median(relative_values),
                "median_segment_p99_9_ns": _median(absolute_values),
            }
        )

    accepted = [candidate for candidate in evaluated if candidate["accepted"]]
    accepted.sort(
        key=lambda candidate: (
            -candidate["median_segment_relative"],
            -candidate["median_segment_p99_9_ns"],
            candidate["path_id"],
        )
    )
    selected_paths = [candidate["path_id"] for candidate in accepted[:2]]
    return {
        "schema_version": "p3-rt-selection-v1",
        "decision": "selected" if selected_paths else "no_candidate",
        "thresholds": {
            "minimum_relative_contribution": MIN_RELATIVE_CONTRIBUTION,
            "minimum_absolute_contribution_ns": MIN_ABSOLUTE_CONTRIBUTION_NS,
            "seeds": list(SEEDS),
            "maximum_selected_paths": 2,
        },
        "selected_paths": selected_paths,
        "candidates": evaluated,
    }


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError(f"output already exists: {args.output}")
        matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
        result = select_candidates(matrix)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"P3 candidate selection failed: {error}")
        return 1
    print(f"P3_RT_CANDIDATE_SELECTION_PASS decision={result['decision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
