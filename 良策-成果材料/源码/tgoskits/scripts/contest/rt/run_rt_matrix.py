#!/usr/bin/env python3
"""Host-only P3 realtime matrix planner.

This candidate intentionally stops before WSL/QEMU execution.  It validates
the frozen matrix, expands stable run identities, records the exact planned
order, and publishes a status-last blocked dry-run package.  A runtime backend
must be added only after the P3 clock and Guest gates are authorized.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    import p3_contract
except ImportError:  # pragma: no cover - package import path
    from . import p3_contract


MISSING_EVENTS = [
    "period_release",
    "period_start",
    "period_finish",
    "guest_handler_enter",
]


def _metric_na() -> dict[str, Any]:
    return {
        "n": 0,
        "mean_ns": None,
        "min_ns": None,
        "max_ns": None,
        "p99_ns": None,
        "p99_9_ns": None,
    }


def _summary(plan: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": p3_contract.SUMMARY_SCHEMA,
        "run_id": run["run_id"],
        "matrix_id": plan["matrix_id"],
        "scenario": run["scenario"],
        "seed": run["seed"],
        "path_id": run["path_id"],
        "feature_state": run["feature_state"],
        "status": "blocked",
        "missing_events": list(MISSING_EVENTS),
        "exactly_once": False,
        "metrics": {
            "period_jitter_ns": _metric_na(),
            "absolute_jitter_ns": _metric_na(),
            "wakeup_latency_ns": _metric_na(),
            "execution_time_ns": _metric_na(),
        },
        "non_claims": [
            "host-only dry-run; no Guest events were collected",
            "no runtime A/B or performance conclusion",
        ],
    }


def _command_record(plan: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "p3-rt-command-v1",
        "matrix_id": plan["matrix_id"],
        "run_id": run["run_id"],
        "argv": [],
        "execution": "not_started",
        "reason": "host-only dry-run; WSL/QEMU backend is intentionally disabled",
    }


def _write_package(output_dir: Path, matrix: dict[str, Any], plan: dict[str, Any]) -> None:
    if output_dir.exists():
        raise p3_contract.P3ContractError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    p3_contract.write_json(output_dir / "input-matrix.json", matrix)
    p3_contract.write_json(output_dir / "matrix-plan.json", plan)

    commands = output_dir / "commands.jsonl"
    p3_contract.publish_new_file(
        commands,
        "".join(
            json.dumps(_command_record(plan, run), sort_keys=True) + "\n"
            for run in plan["runs"]
        ).encode("utf-8"),
        error_type=p3_contract.P3ContractError,
    )
    for run in plan["runs"]:
        summary_path = output_dir / "summaries" / f"{run['run_id']}.json"
        summary = _summary(plan, run)
        p3_contract.validate_summary(summary, run["run_id"])
        p3_contract.write_json(summary_path, summary)

    matrix_summary = {
        "schema_version": p3_contract.MATRIX_SUMMARY_SCHEMA,
        "matrix_id": plan["matrix_id"],
        "mode": plan["mode"],
        "run_count": plan["run_count"],
        "completed_runs": 0,
        "blocked_runs": plan["run_count"],
        "status": "blocked",
        "exactly_once": False,
        "missing_events": "N/A",
        "non_claims": [
            "this package is a plan/static artifact only",
            "no WSL, QEMU, target build, Guest or A/B evidence",
        ],
    }
    p3_contract.write_json(output_dir / "summary.json", matrix_summary)
    p3_contract.write_json(
        output_dir / "cleanup.json",
        {"schema_version": "p3-rt-cleanup-v1", "residual_processes": [], "residual_sockets": []},
    )
    manifest = {
        "schema_version": "p3-rt-manifest-v1",
        "matrix_id": plan["matrix_id"],
        "files": p3_contract.manifest_files(output_dir),
        "excluded": ["status.json"],
    }
    p3_contract.write_json(output_dir / "manifest.json", manifest)
    p3_contract.write_status_last(
        output_dir,
        {
            "schema_version": p3_contract.STATUS_SCHEMA,
            "success": False,
            "status": "realtime_measurement_blocked",
            "blockedReason": "host-only dry-run; runtime backend disabled",
            "matrix_id": plan["matrix_id"],
            "planned_runs": plan["run_count"],
            "completed_runs": 0,
            "statusLast": True,
        },
    )
    validate_matrix_package(output_dir)


def _read_regular_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise p3_contract.P3ContractError(f"{label} must be a regular file: {path}")
    return p3_contract.read_json(path)


def validate_matrix_package(output_dir: Path) -> dict[str, Any]:
    """Consume one blocked P3 matrix package without upgrading its evidence."""

    candidate = Path(output_dir)
    if candidate.is_symlink() or not candidate.is_dir():
        raise p3_contract.P3ContractError(
            f"P3 package must be a regular directory: {output_dir}"
        )
    output = candidate.resolve()
    for entry in output.rglob("*"):
        if entry.is_symlink():
            raise p3_contract.P3ContractError(f"P3 package contains symlink: {entry}")

    matrix = _read_regular_json(output / "input-matrix.json", "input-matrix.json")
    plan = _read_regular_json(output / "matrix-plan.json", "matrix-plan.json")
    normalized_plan = p3_contract.validate_matrix(matrix)
    if plan != normalized_plan:
        raise p3_contract.P3ContractError("matrix-plan.json does not equal validated input-matrix.json")
    matrix_id = normalized_plan["matrix_id"]

    expected_plan_keys = {"schema_version", "matrix_id", "mode", "seeds", "run_count", "runs"}
    if set(plan) != expected_plan_keys:
        raise p3_contract.P3ContractError("matrix-plan.json key set drifted")

    commands_path = output / "commands.jsonl"
    if not commands_path.is_file() or commands_path.is_symlink():
        raise p3_contract.P3ContractError("commands.jsonl must be a regular file")
    try:
        command_lines = commands_path.read_text(encoding="utf-8").splitlines()
        command_records = [json.loads(line) for line in command_lines]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise p3_contract.P3ContractError(f"commands.jsonl is not valid JSONL: {error}") from error
    if len(command_records) != plan["run_count"]:
        raise p3_contract.P3ContractError("commands.jsonl count does not match matrix plan")
    for record, run in zip(command_records, plan["runs"], strict=True):
        if record != _command_record(plan, run):
            raise p3_contract.P3ContractError(f"command record drifted for {run['run_id']}")

    summaries_dir = output / "summaries"
    if not summaries_dir.is_dir() or summaries_dir.is_symlink():
        raise p3_contract.P3ContractError("summaries must be a regular directory")
    expected_summary_names = {f"{run['run_id']}.json" for run in plan["runs"]}
    actual_summary_names = {path.name for path in summaries_dir.iterdir() if path.is_file()}
    if actual_summary_names != expected_summary_names:
        raise p3_contract.P3ContractError("summary file set does not match matrix plan")
    for run in plan["runs"]:
        summary = _read_regular_json(summaries_dir / f"{run['run_id']}.json", "run summary")
        p3_contract.validate_summary(summary, run["run_id"])
        if summary.get("matrix_id") != matrix_id:
            raise p3_contract.P3ContractError(f"summary {run['run_id']} matrix identity drifted")
        for field in ("scenario", "seed", "path_id", "feature_state"):
            if summary.get(field) != run[field]:
                raise p3_contract.P3ContractError(
                    f"summary {run['run_id']} does not match planned {field}"
                )
        if summary.get("status") != "blocked" or summary.get("exactly_once") is not False:
            raise p3_contract.P3ContractError("host-only matrix package summary is not blocked")

    matrix_summary = _read_regular_json(output / "summary.json", "summary.json")
    expected_summary_keys = {
        "schema_version", "matrix_id", "mode", "run_count", "completed_runs",
        "blocked_runs", "status", "exactly_once", "missing_events", "non_claims",
    }
    if set(matrix_summary) != expected_summary_keys:
        raise p3_contract.P3ContractError("summary.json key set drifted")
    if (
        matrix_summary["schema_version"] != p3_contract.MATRIX_SUMMARY_SCHEMA
        or matrix_summary["matrix_id"] != matrix_id
        or matrix_summary["mode"] != plan["mode"]
        or matrix_summary["run_count"] != plan["run_count"]
        or matrix_summary["completed_runs"] != 0
        or matrix_summary["blocked_runs"] != plan["run_count"]
        or matrix_summary["status"] != "blocked"
        or matrix_summary["exactly_once"] is not False
        or matrix_summary["missing_events"] != "N/A"
    ):
        raise p3_contract.P3ContractError("summary.json blocked aggregate drifted")

    cleanup = _read_regular_json(output / "cleanup.json", "cleanup.json")
    if set(cleanup) != {"schema_version", "residual_processes", "residual_sockets"}:
        raise p3_contract.P3ContractError("cleanup.json key set drifted")
    if (
        cleanup["schema_version"] != "p3-rt-cleanup-v1"
        or cleanup["residual_processes"] != []
        or cleanup["residual_sockets"] != []
    ):
        raise p3_contract.P3ContractError("P3 package cleanup is not empty")

    manifest = _read_regular_json(output / "manifest.json", "manifest.json")
    if set(manifest) != {"schema_version", "matrix_id", "files", "excluded"}:
        raise p3_contract.P3ContractError("manifest.json key set drifted")
    if (
        manifest["schema_version"] != "p3-rt-manifest-v1"
        or manifest["matrix_id"] != matrix_id
        or manifest["excluded"] != ["status.json"]
    ):
        raise p3_contract.P3ContractError("manifest identity or exclusion drifted")
    actual_files = p3_contract.manifest_files(output, excluded=("status.json", "manifest.json"))
    if manifest["files"] != actual_files:
        raise p3_contract.P3ContractError("manifest file claims do not match package bytes")
    expected_directories: set[str] = set()
    for relative in actual_files:
        parts = relative.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            expected_directories.add("/".join(parts[:index]))
    actual_directories = {
        entry.relative_to(output).as_posix()
        for entry in output.rglob("*")
        if entry.is_dir()
    }
    if actual_directories != expected_directories:
        raise p3_contract.P3ContractError("P3 package directory set drifted")

    status = _read_regular_json(output / "status.json", "status.json")
    if set(status) != {
        "schema_version", "success", "status", "blockedReason", "matrix_id",
        "planned_runs", "completed_runs", "statusLast",
    }:
        raise p3_contract.P3ContractError("status.json key set drifted")
    if (
        status["schema_version"] != p3_contract.STATUS_SCHEMA
        or status["success"] is not False
        or status["status"] != "realtime_measurement_blocked"
        or not isinstance(status["blockedReason"], str)
        or not status["blockedReason"]
        or status["matrix_id"] != matrix_id
        or status["planned_runs"] != plan["run_count"]
        or status["completed_runs"] != 0
        or status["statusLast"] is not True
    ):
        raise p3_contract.P3ContractError("status.json blocked identity drifted")
    return {
        "schema_version": "p3-rt-package-validation-v1",
        "valid": True,
        "matrix_id": matrix_id,
        "run_count": plan["run_count"],
        "status": status["status"],
        "qualified": False,
    }


def run_matrix(matrix_path: Path, output_dir: Path | None = None) -> dict[str, Any]:
    matrix = p3_contract.read_json(matrix_path)
    plan = p3_contract.validate_matrix(matrix)
    if output_dir is not None:
        _write_package(output_dir, matrix, plan)
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--zephyr-base", type=Path)
    parser.add_argument("--zephyr-sdk-dir", type=Path)
    parser.add_argument("--west-bin", default="west")
    parser.add_argument("--cargo-bin", default="cargo")
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.jobs != 1:
            raise p3_contract.P3ContractError("P3 matrix runner requires --jobs 1")
        if not args.dry_run:
            raise p3_contract.P3ContractError(
                "runtime execution is disabled in the host-only candidate; pass --dry-run"
            )
        if args.repository is not None and not args.repository.is_dir():
            raise p3_contract.P3ContractError("--repository must be an existing directory")
        if args.evidence_root is not None and not args.evidence_root.is_dir():
            raise p3_contract.P3ContractError("--evidence-root must already exist")
        if args.output_dir is not None and args.evidence_root is not None:
            try:
                args.output_dir.resolve().relative_to(args.evidence_root.resolve())
            except ValueError as error:
                raise p3_contract.P3ContractError(
                    "--output-dir must be inside --evidence-root"
                ) from error
        plan = run_matrix(args.matrix, args.output_dir)
    except (OSError, p3_contract.P3ContractError) as error:
        print(f"P3 realtime matrix dry-run failed: {error}")
        return 1
    suffix = f" output={args.output_dir}" if args.output_dir is not None else ""
    print(f"P3_RT_MATRIX_DRY_RUN_PASS runs={plan['run_count']}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
