#!/usr/bin/env python3
"""Small, host-only contracts shared by the P3 matrix dry-run tools.

The existing event validator remains the source of truth for individual event
records.  This module adds the missing matrix/run identity and status-last
boundary without launching a guest or changing the existing P3 tools.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_vm_carveout_io import publish_new_file  # noqa: E402


SEEDS = (7, 19, 43)
MATRIX_SCHEMA = "p3-rt-matrix-v1"
RUN_SCHEMA = "p3-rt-run-v1"
SUMMARY_SCHEMA = "p3-rt-summary-v1"
STATUS_SCHEMA = "p3-rt-status-v1"
MATRIX_SUMMARY_SCHEMA = "p3-rt-matrix-summary-v1"
FEATURE_STATES = {"off", "on"}
MODES = {"native", "discovery", "production-ab"}
SUMMARY_STATUSES = {"complete", "blocked", "incomplete"}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class P3ContractError(ValueError):
    """Raised when a matrix, run identity, or summary is unsafe to use."""


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise P3ContractError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise P3ContractError(f"JSON root must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise P3ContractError(f"{field} must be a safe non-empty identifier")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise P3ContractError(f"{field} must be a lowercase SHA-256")
    return value


def _require_int(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise P3ContractError(f"{field} must be an integer >= {minimum}")
    return value


def _profile_scenarios(matrix: Mapping[str, Any]) -> set[str]:
    profiles = matrix.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise P3ContractError("matrix profiles must be a non-empty list")
    scenarios: set[str] = set()
    for index, profile in enumerate(profiles):
        if not isinstance(profile, Mapping):
            raise P3ContractError(f"profiles[{index}] must be an object")
        scenario = _safe_id(profile.get("scenario"), f"profiles[{index}].scenario")
        _sha256(profile.get("sha256"), f"profiles[{index}].sha256")
        if scenario in scenarios:
            raise P3ContractError(f"duplicate profile scenario: {scenario}")
        scenarios.add(scenario)
    return scenarios


def derive_run_id(matrix_id: str, run: Mapping[str, Any]) -> str:
    """Derive a stable identity from matrix-owned fields.

    The builder predates this contract and does not store run IDs.  Deriving
    them here lets the runner reject duplicate or missing A/B entries without
    rewriting the existing matrix builder.
    """

    path_id = run.get("path_id") or "none"
    return (
        f"{matrix_id}__{run['scenario']}__s{run['seed']}__"
        f"{path_id}__{run['feature_state']}"
    )


def _validate_run_record(
    run: Mapping[str, Any], scenarios: set[str], mode: str, index: int
) -> dict[str, Any]:
    scenario = _safe_id(run.get("scenario"), f"runs[{index}].scenario")
    if scenario not in scenarios:
        raise P3ContractError(f"runs[{index}] references an unknown scenario")
    seed = _require_int(run.get("seed"), f"runs[{index}].seed")
    if seed not in SEEDS:
        raise P3ContractError(f"runs[{index}].seed must be one of {SEEDS}")
    feature_state = run.get("feature_state")
    if feature_state not in FEATURE_STATES:
        raise P3ContractError(f"runs[{index}].feature_state must be off or on")
    path_id = run.get("path_id")
    if mode == "production-ab":
        _safe_id(path_id, f"runs[{index}].path_id")
    elif path_id is not None:
        raise P3ContractError(
            f"runs[{index}].path_id must be null outside production-ab"
        )
    if mode != "production-ab" and feature_state != "off":
        raise P3ContractError("native/discovery matrices may only contain feature_state=off")
    required_gates = run.get("required_gates", [])
    if not isinstance(required_gates, list) or any(
        not isinstance(gate, str) or not gate.startswith("TEST-")
        for gate in required_gates
    ):
        raise P3ContractError(f"runs[{index}].required_gates is invalid")
    return {
        "scenario": scenario,
        "seed": seed,
        "path_id": path_id,
        "feature_state": feature_state,
        "required_gates": list(required_gates),
    }


def validate_matrix(matrix: Mapping[str, Any]) -> dict[str, Any]:
    """Validate matrix identity and return a normalized, read-only plan."""

    if matrix.get("schema_version") != MATRIX_SCHEMA:
        raise P3ContractError(f"matrix schema_version must be {MATRIX_SCHEMA}")
    matrix_id = _safe_id(matrix.get("matrix_id"), "matrix_id")
    mode = matrix.get("mode")
    if mode not in MODES:
        raise P3ContractError(f"matrix mode must be one of {sorted(MODES)}")
    seeds = matrix.get("seeds")
    if seeds != list(SEEDS):
        raise P3ContractError(f"matrix seeds must be exactly {list(SEEDS)}")
    scenarios = _profile_scenarios(matrix)
    raw_runs = matrix.get("runs")
    if not isinstance(raw_runs, list) or not raw_runs:
        raise P3ContractError("matrix runs must be a non-empty list")

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str | None, str]] = set()
    for index, raw_run in enumerate(raw_runs):
        if not isinstance(raw_run, Mapping):
            raise P3ContractError(f"runs[{index}] must be an object")
        run = _validate_run_record(raw_run, scenarios, mode, index)
        key = (run["scenario"], run["seed"], run["path_id"], run["feature_state"])
        if key in seen:
            raise P3ContractError(f"duplicate matrix run identity: {key}")
        seen.add(key)
        normalized.append({**run, "run_id": derive_run_id(matrix_id, run)})

    if mode == "production-ab":
        for scenario in scenarios:
            for seed in SEEDS:
                paths = {
                    run["path_id"]
                    for run in normalized
                    if run["scenario"] == scenario
                    and run["seed"] == seed
                    and run["path_id"] != "combined"
                }
                if not paths:
                    raise P3ContractError(
                        f"production matrix has no candidate for {scenario}/seed={seed}"
                    )
                for path_id in paths:
                    states = {
                        run["feature_state"]
                        for run in normalized
                        if run["scenario"] == scenario
                        and run["seed"] == seed
                        and run["path_id"] == path_id
                    }
                    if states != {"off", "on"}:
                        raise P3ContractError(
                            f"{scenario}/seed={seed}/{path_id} must have exactly off and on"
                        )
        for run in normalized:
            if run["path_id"] == "combined" and run["feature_state"] != "on":
                raise P3ContractError("combined production run must be feature_state=on")

    return {
        "schema_version": RUN_SCHEMA,
        "matrix_id": matrix_id,
        "mode": mode,
        "seeds": list(SEEDS),
        "run_count": len(normalized),
        "runs": normalized,
    }


def validate_run_identity(run: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    for field in ("run_id", "matrix_id", "scenario", "seed", "path_id", "feature_state"):
        if run.get(field) != expected.get(field):
            raise P3ContractError(
                f"run identity mismatch for {field}: {run.get(field)!r} != {expected.get(field)!r}"
            )


def _metric_shape(value: Any, field: str) -> None:
    if not isinstance(value, Mapping):
        raise P3ContractError(f"summary.{field} must be an object")
    for key in ("n", "mean_ns", "min_ns", "max_ns", "p99_ns", "p99_9_ns"):
        item = value.get(key)
        if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0):
            raise P3ContractError(f"summary.{field}.{key} must be a non-negative integer or null")


def validate_summary(summary: Mapping[str, Any], expected_run_id: str | None = None) -> dict[str, Any]:
    if summary.get("schema_version") != SUMMARY_SCHEMA:
        raise P3ContractError(f"summary schema_version must be {SUMMARY_SCHEMA}")
    run_id = _safe_id(summary.get("run_id"), "summary.run_id")
    if expected_run_id is not None and run_id != expected_run_id:
        raise P3ContractError("summary run_id does not match matrix identity")
    _safe_id(summary.get("matrix_id"), "summary.matrix_id")
    _safe_id(summary.get("scenario"), "summary.scenario")
    seed = _require_int(summary.get("seed"), "summary.seed")
    if seed not in SEEDS:
        raise P3ContractError("summary.seed is not a frozen P3 seed")
    if summary.get("feature_state") not in FEATURE_STATES:
        raise P3ContractError("summary.feature_state is invalid")
    status = summary.get("status")
    if status not in SUMMARY_STATUSES:
        raise P3ContractError("summary.status is invalid")
    missing = summary.get("missing_events")
    if not isinstance(missing, list) or any(not isinstance(item, str) or not item for item in missing):
        raise P3ContractError("summary.missing_events must be a list of names")
    exactly_once = summary.get("exactly_once")
    if not isinstance(exactly_once, bool):
        raise P3ContractError("summary.exactly_once must be boolean")
    if status == "complete" and (missing or not exactly_once):
        raise P3ContractError("complete summary cannot contain missing events or failed exactly-once")
    if missing and status == "complete":
        raise P3ContractError("complete summary cannot mark events N/A")
    metrics = summary.get("metrics")
    if not isinstance(metrics, Mapping):
        raise P3ContractError("summary.metrics must be an object")
    for field in ("period_jitter_ns", "absolute_jitter_ns", "wakeup_latency_ns", "execution_time_ns"):
        _metric_shape(metrics.get(field), field)
    return dict(summary)


def validate_jsonl_identity(
    path: Path, run_id: str, *, require_fault_target: bool = False
) -> int:
    """Check non-empty JSONL, run identity, and exactly-once sequence keys."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise P3ContractError(f"cannot read event stream {path}: {error}") from error
    if not lines:
        raise P3ContractError(f"event stream is empty: {path}")
    seen: set[tuple[str, int]] = set()
    found_fault_target = False
    for line_number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise P3ContractError(f"invalid JSONL {path}:{line_number}: {error}") from error
        if not isinstance(record, Mapping):
            raise P3ContractError(f"JSONL record is not an object: {path}:{line_number}")
        if record.get("run_id") != run_id:
            raise P3ContractError(f"run_id mismatch in {path}:{line_number}")
        sequence = record.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise P3ContractError(f"sequence is invalid in {path}:{line_number}")
        key = (str(record.get("stream", path.name)), sequence)
        if key in seen:
            raise P3ContractError(f"duplicate exactly-once key {key} in {path}")
        seen.add(key)
        if record.get("fault_target"):
            found_fault_target = True
    if require_fault_target and not found_fault_target:
        raise P3ContractError(f"fault stream has no explicit fault_target: {path}")
    return len(lines)


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    publish_new_file(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        error_type=P3ContractError,
    )


def manifest_files(root: Path, excluded: Iterable[str] = ("status.json",)) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    excluded_set = set(excluded)
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise P3ContractError(f"manifest enumerator refuses symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded_set:
            continue
        entries[relative] = {"size": path.stat().st_size, "sha256": sha256_file(path)}
    return entries


def write_status_last(root: Path, status: Mapping[str, Any]) -> None:
    if (root / "status.json").exists():
        raise P3ContractError(f"status.json already exists: {root}")
    write_json(root / "status.json", status)
