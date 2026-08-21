#!/usr/bin/env python3
"""Contract tests for the deterministic P3 candidate selector."""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RT_DIR = ROOT / "scripts" / "contest" / "rt"
sys.path.insert(0, str(RT_DIR))

import select_rt_candidates  # noqa: E402


def make_contract_root() -> Path:
    root = ROOT / "target" / "contract-tests" / f"p3-selection-{secrets.token_hex(8)}"
    root.mkdir(parents=True)
    return root


def candidate(
    path_id: str, segment_values: list[int], off_values: list[int]
) -> dict[str, object]:
    return {
        "path_id": path_id,
        "primary_metric": "abs_jitter_ns.p99_9",
        "seed_results": [
            {
                "seed": seed,
                "native_p99_9_ns": 100_000,
                "axvisor_off_p99_9_ns": off,
                "segment_p99_9_ns": segment,
            }
            for seed, segment, off in zip((7, 19, 43), segment_values, off_values)
        ],
    }


def main() -> int:
    matrix = {
        "schema_version": "p3-rt-matrix-v1",
        "candidates": [
            candidate(
                "path-slow", [30_000, 35_000, 40_000], [130_000, 135_000, 140_000]
            ),
            candidate("path-fast", [60_000, 60_000, 60_000], [160_000, 160_000, 160_000]),
            candidate(
                "path-rejected", [30_000, 1_000, 30_000], [130_000, 101_000, 130_000]
            ),
        ],
    }
    result = select_rt_candidates.select_candidates(matrix)
    assert result["decision"] == "selected"
    assert result["selected_paths"] == ["path-fast", "path-slow"]
    rejected = next(
        item for item in result["candidates"] if item["path_id"] == "path-rejected"
    )
    assert rejected["accepted"] is False
    assert rejected["rejection_reasons"]

    no_candidate = {
        "schema_version": "p3-rt-matrix-v1",
        "candidates": [
            candidate("path-none", [1_000, 1_000, 1_000], [101_000, 101_000, 101_000])
        ],
    }
    assert select_rt_candidates.select_candidates(no_candidate)["decision"] == "no_candidate"

    root = make_contract_root()
    matrix_path = root / "matrix.json"
    output_path = root / "selection.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    assert select_rt_candidates.main(
        ["--matrix", str(matrix_path), "--output", str(output_path)]
    ) == 0
    assert output_path.exists()
    assert select_rt_candidates.main(
        ["--matrix", str(matrix_path), "--output", str(output_path)]
    ) == 1
    print("P3_RT_CANDIDATE_SELECTION_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"P3 candidate selection contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
