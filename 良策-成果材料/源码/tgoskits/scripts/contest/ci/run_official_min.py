#!/usr/bin/env python3
"""Run the manifest-driven OFFICIAL-MIN CI subset.

This runner deliberately reuses the upstream planner and TOML manifests.  It
does not maintain a second command list, and it never turns a dry-run, skipped
row, timeout, or failed command into ``official_min_passed``.

The runner is host-side orchestration only.  It does not launch WSL or QEMU by
itself; the selected AxVisor row may do so when the caller supplies an
appropriate shell and has separately authorized that environment.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[3]
CI_TEST_ROOT = ROOT / "scripts" / "test"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_vm_carveout_io import REPARSE_FLAG, publish_new_file  # noqa: E402
sys.path.insert(0, str(CI_TEST_ROOT))

import ci_plan  # noqa: E402


SCHEMA_VERSION = "official-min-run-v1"
MANIFEST_SCHEMA_VERSION = "official-min-manifest-v1"
OFFICIAL_MIN_CORE_ID = "test-axvisor-aarch64-qemu-smoke-axtest-timer-stress"
OFFICIAL_MIN_FIXED_IDS = (
    "check-formatting",
    "run-sync-lint",
    "run-clippy",
    "test-with-std",
)
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")


class OfficialMinError(ValueError):
    """The planner or execution state violates the OFFICIAL-MIN contract."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def write_json(path: Path, value: Any) -> None:
    publish_new_file(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        error_type=OfficialMinError,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OfficialMinError(f"invalid JSON artifact {path}: {error}") from error
    if not isinstance(value, dict):
        raise OfficialMinError(f"JSON artifact is not an object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise OfficialMinError(f"cannot hash artifact {path}: {error}") from error
    return digest.hexdigest()


def _run_artifact_files(run_dir: Path) -> list[tuple[str, Path]]:
    """List package files excluded from the manifest/status self-reference."""

    artifacts: list[tuple[str, Path]] = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_symlink():
            raise OfficialMinError(f"run package contains symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(run_dir).as_posix()
        if relative in {"manifest.json", "status.json"}:
            continue
        artifacts.append((relative, path))
    return artifacts


def write_run_manifest(run_dir: Path, common: dict[str, Any]) -> str:
    """Write the immutable pre-status artifact manifest and return its SHA-256."""

    artifacts = _run_artifact_files(run_dir)
    artifact_paths = {relative for relative, _path in artifacts}
    required = {"plan.json", "commands.jsonl", "summary.json"}
    missing = sorted(required - artifact_paths)
    if missing:
        raise OfficialMinError(f"run package is missing pre-status artifacts: {missing}")
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": common["run_id"],
        "source": common["source"],
        "planner_count": common["planner_count"],
        "planner_ids": common["planner_ids"],
        "official_min_ids": common["official_min_ids"],
        "missing_ids": common["missing_ids"],
        "files": [
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            for relative, path in artifacts
        ],
        "status_file": "status.json",
        "status_last": True,
    }
    manifest_path = run_dir / "manifest.json"
    write_json(manifest_path, manifest)
    return _sha256_file(manifest_path)


def _manifest_artifact_path(run_dir: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative:
        raise OfficialMinError("manifest contains an invalid artifact path")
    if relative.startswith("/") or "\\" in relative:
        raise OfficialMinError(f"manifest artifact path is not normalized: {relative}")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise OfficialMinError(f"manifest artifact path escapes package: {relative}")
    path = run_dir.joinpath(*parts)
    if path.is_symlink() or not path.is_file():
        raise OfficialMinError(f"manifest artifact is missing or linked: {relative}")
    return path


def validate_official_min_run(run_dir: Path) -> dict[str, Any]:
    """Consume a completed run package without trusting its producer claims."""

    candidate_dir = Path(run_dir)
    if candidate_dir.is_symlink() or not candidate_dir.is_dir():
        raise OfficialMinError(f"run package directory must be a regular directory: {run_dir}")
    run_dir = candidate_dir.resolve()
    required = [
        run_dir / "plan.json",
        run_dir / "commands.jsonl",
        run_dir / "summary.json",
        run_dir / "manifest.json",
        run_dir / "status.json",
    ]
    missing = [str(path.name) for path in required if not path.is_file()]
    if missing:
        raise OfficialMinError(f"run package is missing required artifacts: {missing}")

    manifest = _read_json(run_dir / "manifest.json")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise OfficialMinError("run manifest schema drifted")
    if manifest.get("status_file") != "status.json" or manifest.get("status_last") is not True:
        raise OfficialMinError("run manifest status-last contract drifted")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise OfficialMinError("run manifest files must be a list")
    entry_paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise OfficialMinError("run manifest contains a non-object file entry")
        relative = entry.get("path")
        path = _manifest_artifact_path(run_dir, relative)
        if relative in entry_paths:
            raise OfficialMinError(f"run manifest contains duplicate artifact: {relative}")
        entry_paths.append(relative)
        if entry.get("size") != path.stat().st_size:
            raise OfficialMinError(f"run artifact size drifted: {relative}")
        if entry.get("sha256") != _sha256_file(path):
            raise OfficialMinError(f"run artifact hash drifted: {relative}")
    actual_paths = {relative for relative, _path in _run_artifact_files(run_dir)}
    if set(entry_paths) != actual_paths:
        raise OfficialMinError("run package artifact set drifted")
    actual_directories: set[str] = set()
    for entry in run_dir.rglob("*"):
        if entry.is_symlink():
            raise OfficialMinError(f"run package contains symlink: {entry}")
        if entry.is_dir():
            actual_directories.add(entry.relative_to(run_dir).as_posix())
    expected_directories: set[str] = set()
    for relative in entry_paths:
        parts = relative.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            expected_directories.add("/".join(parts[:index]))
    if actual_directories != expected_directories:
        raise OfficialMinError("run package directory set drifted")

    plan = _read_json(run_dir / "plan.json")
    summary = _read_json(run_dir / "summary.json")
    status = _read_json(run_dir / "status.json")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise OfficialMinError("run plan schema drifted")
    if summary.get("schema_version") != SCHEMA_VERSION:
        raise OfficialMinError("run summary schema drifted")
    if manifest.get("run_id") != plan.get("run_id"):
        raise OfficialMinError("run manifest identity drifted")
    for key in (
        "source",
        "planner_count",
        "planner_ids",
        "official_min_ids",
        "missing_ids",
    ):
        if manifest.get(key) != plan.get(key):
            raise OfficialMinError(f"run manifest {key} claim drifted")

    identity_keys = (
        "schema_version",
        "run_id",
        "source",
        "planner_count",
        "planner_ids",
        "official_min_ids",
        "missing_ids",
        "started_at",
    )
    for artifact_name, artifact in (("summary", summary), ("status", status)):
        for key in identity_keys:
            expected = SCHEMA_VERSION if key == "schema_version" else plan.get(key)
            if artifact.get(key) != expected:
                raise OfficialMinError(f"run {artifact_name} {key} claim drifted")

    records = summary.get("records")
    if not isinstance(records, list):
        raise OfficialMinError("run summary records must be a list")
    command_records: list[dict[str, Any]] = []
    try:
        command_lines = (run_dir / "commands.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise OfficialMinError(f"cannot read commands.jsonl: {error}") from error
    for line in command_lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise OfficialMinError(f"commands.jsonl contains invalid JSON: {error}") from error
        if not isinstance(record, dict):
            raise OfficialMinError("commands.jsonl contains a non-object record")
        command_records.append(record)
    if command_records != records:
        raise OfficialMinError("commands.jsonl and summary records diverged")

    selected_ids = plan.get("official_min_ids")
    if not isinstance(selected_ids, list) or any(not isinstance(item, str) for item in selected_ids):
        raise OfficialMinError("run plan official_min_ids is invalid")
    record_ids = [record.get("id") for record in records]
    if any(not isinstance(item, str) for item in record_ids) or len(record_ids) != len(set(record_ids)):
        raise OfficialMinError("run records contain duplicate or invalid ids")
    if any(item not in selected_ids for item in record_ids):
        raise OfficialMinError("run records contain an unselected check")
    artifact_paths = set(entry_paths)
    for record in records:
        log = record.get("log")
        if log not in artifact_paths:
            raise OfficialMinError(f"run record log is not packaged: {log}")

    passed_ids = [record["id"] for record in records if record.get("status") == "passed"]
    skipped_ids = [check_id for check_id in selected_ids if check_id not in passed_ids]
    failed_records = [record for record in records if record.get("status") != "passed"]
    success = not plan.get("missing_ids") and not skipped_ids and not failed_records
    expected_status = "official_min_passed" if success else "official_min_failed"
    if status.get("status") != expected_status:
        raise OfficialMinError("run status token does not match records")
    if status.get("success") is not success or status.get("official_min_passed") is not success:
        raise OfficialMinError("run success claim does not match records")
    if status.get("completed_ids") != passed_ids:
        raise OfficialMinError("run completed_ids drifted")
    if status.get("skipped_ids") != skipped_ids:
        raise OfficialMinError("run skipped_ids drifted")
    if status.get("failed") != failed_records:
        raise OfficialMinError("run failed records drifted")
    if status.get("statusLast") is not True:
        raise OfficialMinError("run status is not marked last")
    if status.get("manifest_sha256") != _sha256_file(run_dir / "manifest.json"):
        raise OfficialMinError("run status manifest hash drifted")
    return {
        "valid": True,
        "status": status["status"],
        "success": success,
        "completed_ids": passed_ids,
        "skipped_ids": skipped_ids,
    }


def flatten_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return planner rows in stable matrix order."""

    matrix_names = (
        "static_matrix",
        "workspace_matrix",
        "arceos_matrix",
        "starry_matrix",
        "axvisor_matrix",
    )
    rows: list[dict[str, Any]] = []
    for matrix_name in matrix_names:
        matrix = plan.get(matrix_name)
        if not isinstance(matrix, dict) or not isinstance(matrix.get("include"), list):
            raise OfficialMinError(f"planner output is missing {matrix_name}.include")
        for row in matrix["include"]:
            if not isinstance(row, dict):
                raise OfficialMinError(
                    f"planner output contains a non-object row in {matrix_name}"
                )
            if not isinstance(row.get("id"), str) or not row["id"]:
                raise OfficialMinError(
                    f"planner output contains a row without a non-empty id in {matrix_name}"
                )
            if not isinstance(row.get("command"), str) or not row["command"].strip():
                raise OfficialMinError(
                    f"planner row {row['id']} has no executable command"
                )
            rows.append(row)
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise OfficialMinError("planner output contains duplicate check ids")
    return rows


def build_official_plan(
    *,
    repository: str,
    repository_owner: str,
    event_name: str,
    base_ref: str,
    since_ref: str,
    boolean_inputs: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the same main plan as ``scripts/test/ci_plan.py``."""

    impact = None
    if event_name == "pull_request":
        impact = ci_plan.analyze_pull_request(ci_plan.WORKSPACE_ROOT, since_ref)
    context = ci_plan.PlanContext(
        repository=repository,
        repository_owner=repository_owner,
        event_name=event_name,
        base_ref=base_ref,
        enabled_boolean_inputs=frozenset(boolean_inputs),
        impact=impact,
    )
    return ci_plan.build_main_plan(context)


def select_official_min_rows(plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Select the exact minimum subset and return (rows, missing_ids).

    The Contest portion is derived from the planner's Static matrix, so adding
    or removing a manifest-host check cannot silently leave this runner stale.
    """

    rows = flatten_plan(plan)
    by_id = {row["id"]: row for row in rows}
    static_rows = plan["static_matrix"]["include"]
    contest_ids = [
        row["id"] for row in static_rows if row.get("group") == "Contest"
    ]
    if len(contest_ids) != len(set(contest_ids)):
        raise OfficialMinError("planner Contest matrix contains duplicate ids")

    required_ids = list(OFFICIAL_MIN_FIXED_IDS) + contest_ids + [
        OFFICIAL_MIN_CORE_ID
    ]
    required_ids = list(dict.fromkeys(required_ids))
    missing = [check_id for check_id in required_ids if check_id not in by_id]
    selected = [by_id[check_id] for check_id in required_ids if check_id in by_id]
    return selected, missing


def _resolve_run_dir(output_root: Path, run_id: str) -> Path:
    if SAFE_ID.fullmatch(run_id) is None:
        raise OfficialMinError("run_id must be a safe non-empty identifier")
    root = output_root if output_root.is_absolute() else ROOT / output_root
    run_dir = root.resolve() / run_id
    if run_dir.exists():
        raise OfficialMinError(f"fresh run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    (run_dir / "logs").mkdir()
    return run_dir


def _run_one(
    row: dict[str, Any],
    *,
    run_dir: Path,
    environment: dict[str, str],
    shell: str,
    index: int,
) -> dict[str, Any]:
    check_id = row["id"]
    log_path = run_dir / "logs" / f"{index:03d}-{check_id}.log"
    command = row["command"]
    timeout_minutes = int(row.get("timeout_minutes", 360))
    started_at = utc_now()
    monotonic_start = time.monotonic()
    record: dict[str, Any] = {
        "id": check_id,
        "name": row.get("name", check_id),
        "source": row.get("source"),
        "command": command,
        "timeout_minutes": timeout_minutes,
        "log": log_path.relative_to(run_dir).as_posix(),
        "started_at": started_at,
    }

    log_fd = os.open(
        log_path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(log_fd, "w", encoding="utf-8", newline="") as log:
        log.write(f"run_id={run_dir.name}\n")
        log.write(f"check_id={check_id}\n")
        log.write(f"started_at={started_at}\n")
        log.write(f"timeout_minutes={timeout_minutes}\n")
        log.write("command:\n")
        log.write(command)
        log.write("\n\noutput:\n")
        log.flush()
        try:
            process = subprocess.Popen(
                [shell, "-lc", command],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=os.name != "nt",
            )
        except OSError as error:
            record.update(
                {
                    "status": "runner_error",
                    "exit_code": None,
                    "error": str(error),
                }
            )
        else:
            try:
                exit_code = process.wait(timeout=timeout_minutes * 60)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                exit_code = process.wait()
                record.update(
                    {
                        "status": "timeout",
                        "exit_code": exit_code,
                        "error": f"command exceeded {timeout_minutes} minutes",
                    }
                )
            else:
                record.update(
                    {
                        "status": "passed" if exit_code == 0 else "failed",
                        "exit_code": exit_code,
                    }
                )
    record.update(
        {
            "finished_at": utc_now(),
            "duration_seconds": round(time.monotonic() - monotonic_start, 3),
        }
    )
    return record


def _write_command_record(path: Path, record: dict[str, Any]) -> None:
    try:
        initial = path.lstat()
    except OSError as error:
        raise OfficialMinError(f"commands artifact is unavailable: {path}") from error
    if (
        stat.S_ISLNK(initial.st_mode)
        or bool(int(getattr(initial, "st_file_attributes", 0)) & REPARSE_FLAG)
        or not stat.S_ISREG(initial.st_mode)
    ):
        raise OfficialMinError(f"commands artifact is not a regular file: {path}")
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
            raise OfficialMinError(f"commands artifact changed while opening: {path}")
        payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        with os.fdopen(descriptor, "ab", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def run(args: argparse.Namespace) -> int:
    plan = build_official_plan(
        repository=args.repository,
        repository_owner=args.repository_owner,
        event_name=args.event_name,
        base_ref=args.base_ref,
        since_ref=args.since_ref,
        boolean_inputs=args.boolean_input,
    )
    all_rows = flatten_plan(plan)
    selected_rows, missing_ids = select_official_min_rows(plan)
    selected_ids = [row["id"] for row in selected_rows]
    planner_ids = [row["id"] for row in all_rows]

    if args.dry_run:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "official_min_ids": selected_ids,
                    "missing_ids": missing_ids,
                    "planner_count": len(planner_ids),
                    "planner_ids": planner_ids,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if not missing_ids else 1

    run_dir = _resolve_run_dir(Path(args.output_root), args.run_id)
    commands_path = run_dir / "commands.jsonl"
    publish_new_file(commands_path, b"", error_type=OfficialMinError)
    started_at = utc_now()
    common = {
        "schema_version": SCHEMA_VERSION,
        "run_id": args.run_id,
        "source": {
            "repository": args.repository,
            "repository_owner": args.repository_owner,
            "event_name": args.event_name,
            "base_ref": args.base_ref,
            "since_ref": args.since_ref,
            "head": _git_head(),
        },
        "planner_count": len(planner_ids),
        "planner_ids": planner_ids,
        "official_min_ids": selected_ids,
        "missing_ids": missing_ids,
        "started_at": started_at,
    }
    write_json(run_dir / "plan.json", common | {"planner": plan})

    records: list[dict[str, Any]] = []
    if not missing_ids:
        environment = os.environ.copy()
        environment.update(
            {
                "CI": "true",
                "SINCE_REF": args.since_ref,
                "OFFICIAL_MIN_RUN_ID": args.run_id,
            }
        )
        for index, row in enumerate(selected_rows, start=1):
            print(f"[{index}/{len(selected_rows)}] {row['id']}")
            record = _run_one(
                row,
                run_dir=run_dir,
                environment=environment,
                shell=args.shell,
                index=index,
            )
            records.append(record)
            _write_command_record(commands_path, record)
            if record["status"] != "passed":
                break

    passed_ids = [record["id"] for record in records if record["status"] == "passed"]
    skipped_ids = [check_id for check_id in selected_ids if check_id not in passed_ids]
    failed_records = [
        record for record in records if record["status"] != "passed"
    ]
    success = not missing_ids and not skipped_ids and not failed_records
    status = "official_min_passed" if success else "official_min_failed"
    final = common | {
        "status": status,
        "success": success,
        "official_min_passed": success,
        "completed_ids": passed_ids,
        "skipped_ids": skipped_ids,
        "failed": failed_records,
        "manifest_sha256": "",
        "finished_at": utc_now(),
        "statusLast": True,
    }
    write_json(run_dir / "summary.json", common | {"records": records})
    manifest_sha256 = write_run_manifest(run_dir, common)
    final["manifest_sha256"] = manifest_sha256
    write_json(run_dir / "status.json", final)
    validate_official_min_run(run_dir)
    print(f"{status}: {run_dir}")
    return 0 if success else 1


def _git_head() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="starryzac/tgoskits")
    parser.add_argument("--repository-owner", default="starryzac")
    parser.add_argument("--event-name", default="pull_request")
    parser.add_argument("--base-ref", default="dev")
    parser.add_argument("--since-ref", required=True)
    parser.add_argument("--boolean-input", action="append", default=[])
    parser.add_argument("--shell", default="bash")
    parser.add_argument("--run-id", default="official-min-local")
    parser.add_argument(
        "--output-root", type=Path, default=Path("results/baseline/runs")
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except (OSError, ci_plan.PlanError, OfficialMinError) as error:
        print(f"OFFICIAL-MIN runner failed closed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
