#!/usr/bin/env python3
"""Recompute host-visible P6 stream metrics without trusting summaries.

This is a deterministic evidence reader.  It does not start a Guest, infer
missing samples, or publish a qualification result.  A successful result is
an L2 recomputation report with ``qualified=false``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    import qualification_contract as contract
except ImportError:  # pragma: no cover - package import path
    from . import qualification_contract as contract


STREAMS = {
    "events": "raw/events.jsonl",
    "rt_samples": "raw/rt-samples.jsonl",
    "icpc": "raw/icpc.jsonl",
    "frames": "raw/frames.jsonl",
    "trajectory": "raw/trajectory.jsonl",
}


def _read_rows(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise contract.QualificationError(f"cannot read {path}: {error}") from error
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise contract.QualificationError(f"invalid JSONL {path}:{number}") from error
        if not isinstance(value, dict):
            raise contract.QualificationError(f"JSONL row is not an object: {path}:{number}")
        rows.append(value)
    if not rows:
        raise contract.QualificationError(f"raw stream is empty: {path}")
    return rows


def recompute_metrics(run_directory: Path) -> dict[str, Any]:
    root = contract.require_run_directory(run_directory)
    scenario = contract.read_json(root / "input" / "scenario.json", "input/scenario.json")
    run_id = scenario.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise contract.QualificationError("scenario.run_id is missing")

    stream_counts: dict[str, int] = {}
    unique_sequences: dict[str, int] = {}
    request_counts: dict[str, int] = {}
    rows_by_stream: dict[str, list[dict[str, Any]]] = {}
    for name, relative in STREAMS.items():
        rows = _read_rows(root / relative)
        rows_by_stream[name] = rows
        # Reuse the common sequence/identity oracle; no summary is consulted.
        stream_counts[name] = contract._validate_stream(
            root / relative, run_id, fault=False
        )
        keys = {(str(row.get("stream", name)), row["sequence"]) for row in rows}
        unique_sequences[name] = len(keys)
        if unique_sequences[name] != stream_counts[name]:
            raise contract.QualificationError(f"duplicate sequence in raw/{name}.jsonl")
        request_counts[name] = len(
            {
                row["request_id"]
                for row in rows
                if isinstance(row.get("request_id"), int)
                and not isinstance(row.get("request_id"), bool)
            }
        )

    fault_path = root / "raw" / "faults.jsonl"
    fault_count = contract._validate_stream(fault_path, run_id, fault=True)
    fault_rows = _read_rows(fault_path)
    fault_cases = sorted({str(row["case"]) for row in fault_rows})
    return {
        "schema_version": "p6-qualification-metrics-v1",
        "run_id": run_id,
        "profile": contract.PROFILE,
        "source": "raw/events.jsonl,raw/rt-samples.jsonl,raw/icpc.jsonl,raw/frames.jsonl,raw/trajectory.jsonl,raw/faults.jsonl",
        "stream_counts": {**stream_counts, "faults": fault_count},
        "unique_sequence_counts": {**unique_sequences, "faults": fault_count},
        "request_counts": request_counts,
        "fault_cases": fault_cases,
        "metrics_recomputed": True,
        "qualified": False,
        "evidence_level": "L2 host contract",
        "non_claims": [
            "no P3 tail metrics or Guest runtime timing was inferred",
            "this report cannot publish contest_qualification_completed",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = recompute_metrics(args.run)
        if args.output is not None:
            contract.write_json(args.output, report)
    except (OSError, json.JSONDecodeError, contract.QualificationError) as error:
        print(f"P6 metrics recomputation failed closed: {error}")
        return 1
    print(
        f"P6_METRICS_RECOMPUTED run={report['run_id']} "
        f"streams={sum(report['stream_counts'].values())} qualified=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
