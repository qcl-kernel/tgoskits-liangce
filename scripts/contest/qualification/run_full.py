#!/usr/bin/env python3
"""Aggregate three already-produced P6 run directories without running guests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    import qualification_contract as contract
except ImportError:  # pragma: no cover - package import path
    from . import qualification_contract as contract


def aggregate_runs(run_directories: list[Path]) -> dict[str, Any]:
    if len(run_directories) != 3:
        raise contract.QualificationError("P6 full aggregation requires exactly three run directories")
    reports = [contract.validate_run_directory(path) for path in run_directories]
    seeds: list[int] = []
    identities: set[str] = set()
    nonces: set[str] = set()
    revisions: set[str] = set()
    run_ids: set[str] = set()
    for report, path in zip(reports, run_directories):
        scenario = contract.read_json(path / "input" / "scenario.json", "scenario")
        seed = scenario.get("seed")
        if seed not in contract.SEEDS:
            raise contract.QualificationError(f"run {report['run_id']} has an invalid seed")
        if seed in seeds:
            raise contract.QualificationError(f"duplicate P6 seed: {seed}")
        seeds.append(seed)
        identity = scenario.get("qemu_identity")
        if identity in identities:
            raise contract.QualificationError("duplicate QEMU identity across P6 runs")
        identities.add(identity)
        nonce = scenario.get("nonce")
        if nonce in nonces or nonce == "unbound":
            raise contract.QualificationError("P6 aggregate requires unique bound nonces")
        nonces.add(nonce)
        revision = scenario.get("source_revision")
        if revision == "unbound":
            raise contract.QualificationError("P6 aggregate requires a bound source revision")
        revisions.add(revision)
        run_id = scenario.get("run_id")
        if run_id in run_ids:
            raise contract.QualificationError("duplicate P6 run_id")
        run_ids.add(run_id)
    if sorted(seeds) != list(contract.SEEDS):
        raise contract.QualificationError("P6 aggregate must contain seeds 7, 19, and 43")
    return {
        "schema_version": contract.AGGREGATE_SCHEMA,
        "profile": contract.PROFILE,
        "qualified": True,
        "sessions": [
            {"run_id": report["run_id"], "seed": seed, "manifest_sha256": report["manifest_sha256"]}
            for report, seed in sorted(zip(reports, seeds), key=lambda item: item[1])
        ],
        "non_claims": [
            "aggregate only revalidates the three supplied immutable run directories",
            "no private-repository push or PR operation is performed",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, type=Path, dest="runs")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.dry_run:
            raise contract.QualificationError("P6 full execution is disabled; pass --dry-run")
        aggregate = aggregate_runs(args.runs)
        # This command is a host-only dry-run.  Even if callers provide three
        # already-qualified historical bundles, a dry-run must never mint the
        # formal completion token or qualified=true.
        aggregate["qualified"] = False
        aggregate["execution_kind"] = "host_contract"
        aggregate["evidence_level"] = "L2 host contract"
        aggregate["status"] = "qualification_smoke_completed"
        aggregate["blockedReason"] = contract.AGGREGATE_PREFLIGHT_BLOCKED_REASON
        if args.output_dir is not None:
            if args.output_dir.exists():
                raise contract.QualificationError(f"output directory already exists: {args.output_dir}")
            args.output_dir.mkdir(parents=True)
            contract.write_json(args.output_dir / "aggregate.json", aggregate)
            contract.write_json(
                args.output_dir / "status.json",
                {
                    "schema_version": contract.STATUS_SCHEMA,
                    "profile": contract.PROFILE,
                    "scenario": contract.PROFILE,
                    "session_ids": [session["run_id"] for session in aggregate["sessions"]],
                    "aggregate_sha256": contract.sha256_file(args.output_dir / "aggregate.json"),
                    "success": False,
                    "qualified": False,
                    "status": "qualification_smoke_completed",
                    "blockedReason": contract.AGGREGATE_PREFLIGHT_BLOCKED_REASON,
                    "statusLast": True,
                    "pushPerformed": False,
                    "prPolicy": "forbidden",
                },
            )
            contract.validate_aggregate_preflight_package(args.output_dir)
    except (OSError, contract.QualificationError) as error:
        print(f"P6 full qualification failed: {error}")
        return 1
    print("P6_FULL_DRY_RUN_PASS qualified=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
