#!/usr/bin/env python3
"""Create a non-running, status-last P6 core preflight package."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    import qualification_contract as contract
except ImportError:  # pragma: no cover - package import path
    from . import qualification_contract as contract


def write_core_preflight(run_id: str, output_dir: Path) -> None:
    if output_dir.exists():
        raise contract.QualificationError(f"output directory already exists: {output_dir}")
    if not contract.SAFE_ID.fullmatch(run_id):
        raise contract.QualificationError("run-id must be a safe identifier")
    output_dir.mkdir(parents=True)
    contract.read_json  # keep the module's public helper visible to callers
    contract.write_json(
        output_dir / "preflight.json",
        {
            "schema_version": contract.SCHEMA_VERSION,
            "run_id": run_id,
            "profile": contract.PROFILE,
            "duration_s": contract.DURATION_S,
            "seeds": list(contract.SEEDS),
            "execution": "not_started",
            "reason": "host-only candidate; P6 runtime orchestration is disabled",
        },
    )
    contract.publish_new_file(
        output_dir / "commands.jsonl",
        b'{"execution":"not_started","argv":[],"reason":"WSL/QEMU requires explicit authorization"}\n',
        error_type=contract.QualificationError,
    )
    contract.write_json(
        output_dir / "cleanup.json",
        {"residualProcesses": [], "residualSockets": [], "residualFiles": []},
    )
    contract.write_json(
        output_dir / "status.json",
        {
            "schema_version": contract.STATUS_SCHEMA,
            "run_id": run_id,
            "profile": contract.PROFILE,
            "scenario": contract.PROFILE,
            "success": False,
            "qualified": False,
            "status": "qualification_smoke_completed",
            "blockedReason": "host-only dry-run; no Guest evidence was produced",
            "statusLast": True,
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.dry_run:
            raise contract.QualificationError("P6 core execution is disabled; pass --dry-run")
        write_core_preflight(args.run_id, args.output_dir)
    except (OSError, contract.QualificationError) as error:
        print(f"P6 core preflight failed: {error}")
        return 1
    print(f"P6_CORE_DRY_RUN_PASS run={args.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
