#!/usr/bin/env python3
"""Host-only P6 qualification contract and evidence validator helpers."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_vm_carveout_io import publish_new_file  # noqa: E402


SCHEMA_VERSION = "p6-qualification-v1"
PROFILE_SCHEMA = "p6-qualification-profile-v1"
MANIFEST_SCHEMA = "p6-qualification-manifest-v1"
STATUS_SCHEMA = "p6-qualification-status-v1"
AGGREGATE_SCHEMA = "p6-qualification-aggregate-v1"
PROFILE = "combined-v1"
DURATION_S = 7_200
SEEDS = (7, 19, 43)
FAULT_CASES = (
    "drop",
    "duplicate",
    "reorder",
    "corrupt",
    "linux_socket_stop",
    "network_pause",
    "endpoint_restart",
)
REQUIRED_TESTS = (
    "TEST-006",
    "TEST-007",
    "TEST-008",
    "TEST-009",
    "TEST-010",
    "TEST-011",
    "TEST-012",
    "TEST-013",
    "TEST-014",
    "TEST-015",
    "TEST-016",
    "TEST-017",
    "TEST-018",
)
PHASES = (
    ("warmup_idle", 0, 600, "dual_guest_ready"),
    ("cpu_net_fixed", 600, 1_800, "cpu_net_fixed"),
    ("cpu_net_mlp", 2_400, 1_800, "cpu_net_mlp"),
    ("fault_recovery", 4_200, 600, "fault_recovery"),
    ("post_recovery_soak", 4_800, 2_400, "post_recovery_soak"),
)
FAULT_TARGETS = {
    "drop": ("linux_to_zephyr.control", "drop"),
    "duplicate": ("linux_to_zephyr.control", "duplicate"),
    "reorder": ("linux_to_zephyr.control", "reorder"),
    "corrupt": ("linux_to_zephyr.control", "corrupt"),
    "linux_socket_stop": ("linux.control_socket", "stop"),
    "network_pause": ("p4.dual_guest_link", "pause"),
    "endpoint_restart": ("zephyr.control_endpoint", "restart"),
}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")

REQUIRED_FILES = (
    "input/scenario.json",
    "input/profile.json",
    "input/source.json",
    "input/images.json",
    "input/configs.json",
    "input/model.json",
    "raw/host.log",
    "raw/linux.log",
    "raw/zephyr.log",
    "raw/events.jsonl",
    "raw/rt-samples.jsonl",
    "raw/icpc.jsonl",
    "raw/frames.jsonl",
    "raw/capture.pcap",
    "raw/trajectory.jsonl",
    "raw/faults.jsonl",
    "derived/rt-metrics.json",
    "derived/network-metrics.json",
    "derived/control-metrics.json",
    "derived/qualification-summary.json",
    "cleanup.json",
    "checksums.sha256",
)

PREFLIGHT_FILES = (
    "preflight.json",
    "profile.json",
    "commands.jsonl",
    "cleanup.json",
    "manifest.json",
    "status.json",
)
PREFLIGHT_TERMINAL_FILES = ("manifest.json", "status.json")
PREFLIGHT_PRODUCER = "scripts/contest/qualification/run_qualification.py"
AGGREGATE_PREFLIGHT_FILES = ("aggregate.json", "status.json")
AGGREGATE_PREFLIGHT_STATUS = "qualification_smoke_completed"
AGGREGATE_PREFLIGHT_BLOCKED_REASON = "dry-run aggregator; no qualification runtime was executed"
AGGREGATE_NON_CLAIMS = [
    "aggregate only revalidates the three supplied immutable run directories",
    "no private-repository push or PR operation is performed",
]
PUBLICATION_PREFLIGHT_FILES = (
    "publication-preflight.json",
    "checksums.sha256",
    "manifest.json",
    "status.json",
)
PUBLICATION_PREFLIGHT_BLOCKED_REASON = (
    "runtime qualification publisher is disabled in host-only candidate"
)
PUBLICATION_NON_CLAIMS = [
    "hash inventory is not an independent qualification validator",
    "no summary or status is upgraded to contest_qualification_completed",
]


class QualificationError(ValueError):
    """Raised when a P6 run directory is not a qualified evidence bundle."""


def read_json(path: Path, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationError(f"{field} is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise QualificationError(f"{field} must be a JSON object")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write one deterministic JSON object without silently overwriting it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    publish_new_file(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        error_type=QualificationError,
    )


def _safe(value: Any, field: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise QualificationError(f"{field} must be a safe non-empty identifier")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise QualificationError(f"{field} must be a lowercase SHA-256")
    return value


def _nonempty_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise QualificationError(f"{field} must be a non-empty object")
    return value


def _validate_fault_manifest(value: Any, field: str) -> None:
    """Require every declared fault to name one exact injection target.

    P6 is an orchestrator, so this is intentionally a small schema check.  It
    does not duplicate P4/P5 packet semantics; it only prevents an empty or
    ambiguous fault declaration from being presented as a qualification plan.
    """

    manifest = _nonempty_mapping(value, field)
    if manifest.get("schema_version") != "p6-fault-manifest-v1":
        raise QualificationError(f"{field}.schema_version is invalid")
    if manifest.get("algorithm") != "explicit-case-v1":
        raise QualificationError(f"{field}.algorithm is invalid")
    targets = manifest.get("targets")
    if not isinstance(targets, list) or len(targets) != len(FAULT_CASES):
        raise QualificationError(
            f"{field}.targets must contain exactly {len(FAULT_CASES)} fault cases"
        )
    seen: set[str] = set()
    for index, target in enumerate(targets):
        if not isinstance(target, Mapping):
            raise QualificationError(f"{field}.targets[{index}] must be an object")
        case = target.get("case")
        if case not in FAULT_CASES or case in seen:
            raise QualificationError(f"{field}.targets[{index}].case is not unique/frozen")
        seen.add(case)
        expected_target, expected_action = FAULT_TARGETS[case]
        if target.get("target") != expected_target or target.get("action") != expected_action:
            raise QualificationError(f"{field}.targets[{index}] target/action drifted")
    if tuple(target.get("case") for target in targets) != FAULT_CASES:
        raise QualificationError(f"{field}.targets case order is not frozen")


def require_run_directory(path: Path) -> Path:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_dir():
        raise QualificationError(f"run directory must be a regular directory: {path}")
    resolved = candidate.resolve()
    if resolved.is_file() and resolved.name == "session.json":
        raise QualificationError(
            "P6 validator requires the run directory; session.json is not a run directory"
        )
    if not resolved.is_dir():
        raise QualificationError(f"run directory does not exist: {path}")
    for entry in resolved.rglob("*"):
        if entry.is_symlink():
            raise QualificationError(f"run directory contains a symlink: {entry}")
    return resolved


def _validate_phases(value: Any, field: str = "phases") -> None:
    if not isinstance(value, list) or len(value) != len(PHASES):
        raise QualificationError(f"{field} must contain exactly {len(PHASES)} phases")
    for index, (expected_name, expected_start, expected_duration, expected_load) in enumerate(PHASES):
        phase = value[index]
        if not isinstance(phase, Mapping):
            raise QualificationError(f"{field}[{index}] must be an object")
        if phase.get("name") != expected_name:
            raise QualificationError(f"{field}[{index}] name is not frozen")
        if phase.get("start_s") != expected_start or phase.get("duration_s") != expected_duration:
            raise QualificationError(f"{field}[{index}] has a gap, overlap, or duration drift")
        if phase.get("load") != expected_load:
            raise QualificationError(f"{field}[{index}].load is not frozen")


def _validate_hash_document(value: Mapping[str, Any], field: str) -> None:
    _safe(value.get("path"), f"{field}.path")
    _sha(value.get("sha256"), f"{field}.sha256")


def validate_scenario(
    scenario: Mapping[str, Any],
    profile: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    allow_unbound: bool = False,
) -> dict[str, Any]:
    if scenario.get("schema_version") != 1:
        raise QualificationError("scenario schema_version must be integer 1")
    if scenario.get("profile") != PROFILE or profile.get("profile") != PROFILE:
        raise QualificationError("scenario/profile must be combined-v1")
    if scenario.get("duration_s") != DURATION_S or profile.get("duration_s") != DURATION_S:
        raise QualificationError("qualification duration must be exactly 7200 seconds")
    if scenario.get("seeds") != list(SEEDS) or profile.get("seeds") != list(SEEDS):
        raise QualificationError(f"qualification seeds must be exactly {list(SEEDS)}")
    if scenario.get("seed") not in SEEDS:
        raise QualificationError("scenario.seed must identify one qualification session")
    _validate_phases(scenario.get("phases"), "scenario.phases")
    _validate_phases(profile.get("phases"), "profile.phases")
    for field in ("cpu_load", "network_profile", "ai_profile"):
        _nonempty_mapping(scenario.get(field), f"scenario.{field}")
        _nonempty_mapping(profile.get(field), f"profile.{field}")
    _validate_fault_manifest(scenario.get("fault_manifest"), "scenario.fault_manifest")
    _validate_fault_manifest(profile.get("fault_manifest"), "profile.fault_manifest")
    if scenario.get("required_tests") != list(REQUIRED_TESTS):
        raise QualificationError("scenario.required_tests is incomplete or reordered")
    if source.get("verified_tests") != list(REQUIRED_TESTS):
        raise QualificationError("source.verified_tests does not prove all P2-P5 prerequisites")
    _safe(scenario.get("run_id"), "scenario.run_id")
    _safe(scenario.get("session_id"), "scenario.session_id")
    if scenario.get("run_id") != scenario.get("session_id"):
        raise QualificationError("scenario run_id/session_id identity mismatch")
    for field in ("qemu_identity", "nonce", "source_revision"):
        _safe(scenario.get(field), f"scenario.{field}")
    if not allow_unbound and any(
        scenario.get(field) == "unbound"
        for field in ("qemu_identity", "nonce", "source_revision")
    ):
        raise QualificationError("formal qualification cannot use unbound identity/source fields")
    _safe(scenario.get("qemu_identity"), "scenario.qemu_identity")
    _safe(scenario.get("nonce"), "scenario.nonce")
    _safe(scenario.get("source_revision"), "scenario.source_revision")
    if profile.get("profile_id") != PROFILE:
        raise QualificationError("profile.profile_id must be combined-v1")
    for field in ("images", "configs", "model"):
        document = source.get(field)
        if not isinstance(document, Mapping) or not document:
            raise QualificationError(f"source.{field} must be a non-empty object")
        for name, claim in document.items():
            if not isinstance(name, str) or not isinstance(claim, Mapping):
                raise QualificationError(f"source.{field} contains an invalid claim")
            _validate_hash_document(claim, f"source.{field}.{name}")
    return {
        "profile": PROFILE,
        "run_id": scenario["run_id"],
        "session_id": scenario["session_id"],
        "qemu_identity": scenario["qemu_identity"],
        "nonce": scenario["nonce"],
        "source_revision": scenario["source_revision"],
    }


def _relative_file(root: Path, relative: str) -> Path:
    # Keep the caller's lexical root form so Windows 8.3 aliases do not make
    # an in-bundle file appear to escape after ``resolve()`` expands only one
    # side of the comparison. Symlinks are rejected by the bundle walk.
    candidate = (root / relative).absolute()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise QualificationError(f"manifest path escapes run directory: {relative}") from error
    if not candidate.is_file():
        raise QualificationError(f"manifest references missing file: {relative}")
    return candidate


def validate_manifest(root: Path, manifest: Mapping[str, Any], run_id: str) -> str:
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise QualificationError(f"manifest schema_version must be {MANIFEST_SCHEMA}")
    if manifest.get("run_id") != run_id:
        raise QualificationError("manifest run_id does not match scenario")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise QualificationError("manifest.files must be a list")
    by_path: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise QualificationError(f"manifest.files[{index}] must be an object")
        relative = record.get("path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise QualificationError(f"manifest.files[{index}].path is invalid")
        if relative in by_path or relative in {"manifest.json", "status.json"}:
            raise QualificationError(f"manifest file path is duplicated or terminal: {relative}")
        path = _relative_file(root, relative)
        if record.get("size") != path.stat().st_size:
            raise QualificationError(f"manifest size mismatch: {relative}")
        if record.get("sha256") != sha256_file(path):
            raise QualificationError(f"manifest SHA-256 mismatch: {relative}")
        _safe(record.get("producer"), f"manifest.files[{index}].producer")
        by_path[relative] = record
    missing = sorted(set(REQUIRED_FILES) - set(by_path))
    if missing:
        raise QualificationError(f"manifest is missing full-evidence files: {missing}")
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise QualificationError(f"run directory contains a symlink: {entry}")
        relative = entry.relative_to(root).as_posix()
        if entry.is_file():
            actual_files.add(relative)
        elif entry.is_dir():
            actual_dirs.add(relative)
        else:
            raise QualificationError(f"run directory contains an unsupported entry: {relative}")
    expected_files = set(by_path) | {"manifest.json", "status.json"}
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise QualificationError(
            f"run manifest file set mismatch: missing={missing}, extra={extra}"
        )
    expected_dirs = {
        Path(relative).parent.as_posix()
        for relative in expected_files
        if Path(relative).parent != Path(".")
    }
    if actual_dirs != expected_dirs:
        raise QualificationError("run manifest directory set drifted")
    return sha256_file(root / "manifest.json")


def _validate_stream(path: Path, run_id: str, *, fault: bool = False) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise QualificationError(f"cannot read {path}: {error}") from error
    if not lines:
        raise QualificationError(f"empty evidence stream: {path}")
    seen: set[tuple[str, int]] = set()
    fault_cases: set[str] = set()
    for number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise QualificationError(f"invalid JSONL {path}:{number}: {error}") from error
        if not isinstance(value, Mapping) or value.get("run_id") != run_id:
            raise QualificationError(f"run identity mismatch in {path}:{number}")
        sequence = value.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise QualificationError(f"invalid sequence in {path}:{number}")
        stream = str(value.get("stream", path.name))
        key = (stream, sequence)
        if key in seen:
            raise QualificationError(f"duplicate exactly-once record {key} in {path}")
        seen.add(key)
        if fault:
            case = value.get("case")
            if case not in FAULT_TARGETS:
                raise QualificationError(f"fault evidence has unknown case: {case}")
            expected_target, expected_action = FAULT_TARGETS[case]
            if value.get("fault_target") is not True:
                raise QualificationError(f"fault evidence is not marked target: {case}")
            if value.get("target") != expected_target or value.get("action") != expected_action:
                raise QualificationError(f"fault evidence target/action mismatch: {case}")
            fault_cases.add(case)
    if fault and fault_cases != set(FAULT_TARGETS):
        missing = sorted(set(FAULT_TARGETS) - fault_cases)
        raise QualificationError(f"fault evidence is missing exact cases: {missing}")
    return len(lines)


def _validate_cleanup(cleanup: Mapping[str, Any]) -> None:
    for field in ("residualProcesses", "residualSockets", "residualFiles"):
        if field not in cleanup:
            raise QualificationError(f"cleanup.{field} is required")
        value = cleanup[field]
        if not isinstance(value, list):
            raise QualificationError(f"cleanup.{field} must be a list")
        if value:
            raise QualificationError(f"cleanup residuals are non-empty: {field}")


def _require_preflight_directory(path: Path) -> Path:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_dir():
        raise QualificationError(f"preflight package must be a regular directory: {path}")
    root = candidate.resolve()
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise QualificationError(f"preflight package contains a symlink: {entry}")
    return root


def _validate_preflight_profile(profile: Mapping[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "profile",
        "profile_id",
        "duration_s",
        "seeds",
        "phases",
        "cpu_load",
        "network_profile",
        "ai_profile",
        "fault_manifest",
        "required_tests",
    }
    if set(profile) != expected_keys:
        raise QualificationError("preflight profile keys drifted")
    if profile.get("schema_version") != 1:
        raise QualificationError("preflight profile schema_version must be integer 1")
    if profile.get("profile") != PROFILE or profile.get("profile_id") != PROFILE:
        raise QualificationError("preflight profile identity is invalid")
    if profile.get("duration_s") != DURATION_S or profile.get("seeds") != list(SEEDS):
        raise QualificationError("preflight profile duration/seeds drifted")
    _validate_phases(profile.get("phases"), "profile.phases")
    for field in ("cpu_load", "network_profile", "ai_profile"):
        _nonempty_mapping(profile.get(field), f"profile.{field}")
    _validate_fault_manifest(profile.get("fault_manifest"), "profile.fault_manifest")
    if profile.get("required_tests") != list(REQUIRED_TESTS):
        raise QualificationError("preflight profile required_tests drifted")


def _read_single_jsonl(path: Path, field: str) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise QualificationError(f"{field} is unreadable: {error}") from error
    if len(lines) != 1 or not lines[0].strip():
        raise QualificationError(f"{field} must contain exactly one JSON record")
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise QualificationError(f"{field} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise QualificationError(f"{field} must contain a JSON object")
    return value


def validate_preflight_package(
    path: Path,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    """Consume the current run_qualification host-only preflight package.

    This validator deliberately accepts only the six-file, status-last package
    emitted by ``run_qualification.py``. It proves package self-consistency
    and fail-closed blocked semantics; it never upgrades runtime qualification.
    """

    root = _require_preflight_directory(path)
    expected_paths = set(PREFLIGHT_FILES)
    actual_paths: set[str] = set()
    for entry in root.rglob("*"):
        relative = entry.relative_to(root).as_posix()
        actual_paths.add(relative)
        if not entry.is_file():
            raise QualificationError(f"preflight package contains a non-file entry: {relative}")
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        extra = sorted(actual_paths - expected_paths)
        raise QualificationError(f"preflight package file set mismatch: missing={missing}, extra={extra}")

    preflight = read_json(root / "preflight.json", "preflight.json")
    profile = read_json(root / "profile.json", "profile.json")
    manifest = read_json(root / "manifest.json", "manifest.json")
    status = read_json(root / "status.json", "status.json")
    command = _read_single_jsonl(root / "commands.jsonl", "commands.jsonl")
    cleanup = read_json(root / "cleanup.json", "cleanup.json")

    expected_preflight_keys = {
        "schema_version",
        "scenario_schema_version",
        "run_id",
        "session_id",
        "seed",
        "profile",
        "duration_s",
        "phases",
        "source_revision",
        "qemu_identity",
        "nonce",
        "execution",
        "runtime_backend",
        "qualified",
        "blocked_reason",
    }
    if set(preflight) != expected_preflight_keys:
        raise QualificationError("preflight.json keys drifted")
    run_id = _safe(preflight.get("run_id"), "preflight.run_id")
    if expected_run_id is not None and run_id != expected_run_id:
        raise QualificationError("preflight run_id does not match expected_run_id")
    if preflight.get("schema_version") != SCHEMA_VERSION:
        raise QualificationError(f"preflight schema_version must be {SCHEMA_VERSION}")
    if preflight.get("scenario_schema_version") != 1:
        raise QualificationError("preflight scenario_schema_version must be integer 1")
    if preflight.get("session_id") != run_id or preflight.get("seed") not in SEEDS:
        raise QualificationError("preflight session/seed identity is invalid")
    if preflight.get("profile") != PROFILE or preflight.get("duration_s") != DURATION_S:
        raise QualificationError("preflight profile/duration is invalid")
    _validate_phases(preflight.get("phases"), "preflight.phases")
    for field in ("source_revision", "qemu_identity", "nonce"):
        _safe(preflight.get(field), f"preflight.{field}")
    if preflight.get("execution") != "not_started":
        raise QualificationError("preflight execution is not fail-closed")
    if preflight.get("runtime_backend") != "disabled" or preflight.get("qualified") is not False:
        raise QualificationError("preflight runtime/qualification state is invalid")
    blocked_reason = preflight.get("blocked_reason")
    if not isinstance(blocked_reason, str) or not blocked_reason.strip():
        raise QualificationError("preflight.blocked_reason must be non-empty")

    _validate_preflight_profile(profile)
    expected_command_keys = {"schema_version", "run_id", "seed", "execution", "argv", "reason"}
    if set(command) != expected_command_keys:
        raise QualificationError("commands.jsonl keys drifted")
    if (
        command.get("schema_version") != "p6-qualification-command-v1"
        or command.get("run_id") != run_id
        or command.get("seed") != preflight["seed"]
        or command.get("execution") != "not_started"
        or command.get("argv") != []
    ):
        raise QualificationError("commands.jsonl identity/execution contract is invalid")
    if not isinstance(command.get("reason"), str) or not command["reason"].strip():
        raise QualificationError("commands.jsonl reason must be non-empty")
    _validate_cleanup(cleanup)

    if manifest.get("schema_version") != MANIFEST_SCHEMA or manifest.get("run_id") != run_id:
        raise QualificationError("preflight manifest schema/run identity is invalid")
    if manifest.get("excluded_terminal_files") != list(PREFLIGHT_TERMINAL_FILES):
        raise QualificationError("preflight manifest terminal file policy drifted")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise QualificationError("preflight manifest.files must be a list")
    expected_manifest_paths = expected_paths - set(PREFLIGHT_TERMINAL_FILES)
    by_path: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise QualificationError(f"preflight manifest.files[{index}] must be an object")
        relative = record.get("path")
        if (
            not isinstance(relative, str)
            or relative not in expected_manifest_paths
            or Path(relative).is_absolute()
            or relative in by_path
        ):
            raise QualificationError(f"preflight manifest.files[{index}].path is invalid")
        file_path = root / relative
        if file_path.is_symlink() or not file_path.is_file():
            raise QualificationError(f"preflight manifest references invalid file: {relative}")
        if record.get("size") != file_path.stat().st_size or record.get("sha256") != sha256_file(file_path):
            raise QualificationError(f"preflight manifest hash/size mismatch: {relative}")
        if record.get("producer") != PREFLIGHT_PRODUCER:
            raise QualificationError(f"preflight manifest producer drifted: {relative}")
        by_path[relative] = record
    if set(by_path) != expected_manifest_paths:
        raise QualificationError("preflight manifest does not index exactly the non-terminal files")

    expected_status_keys = {
        "schema_version",
        "run_id",
        "seed",
        "profile",
        "scenario",
        "success",
        "qualified",
        "status",
        "blockedReason",
        "manifest_sha256",
        "runtimeEvidence",
        "pushPerformed",
        "prPolicy",
        "statusLast",
    }
    if set(status) != expected_status_keys:
        raise QualificationError("preflight status keys drifted")
    if (
        status.get("schema_version") != STATUS_SCHEMA
        or status.get("run_id") != run_id
        or status.get("seed") != preflight["seed"]
        or status.get("profile") != PROFILE
        or status.get("scenario") != PROFILE
        or status.get("success") is not False
        or status.get("qualified") is not False
        or status.get("status") != "qualification_smoke_completed"
        or status.get("blockedReason") != blocked_reason
        or status.get("manifest_sha256") != sha256_file(root / "manifest.json")
        or status.get("runtimeEvidence") is not False
        or status.get("pushPerformed") is not False
        or status.get("prPolicy") != "forbidden"
        or status.get("statusLast") is not True
    ):
        raise QualificationError("preflight status is not fail-closed or manifest-bound")

    return {
        "schema_version": "p6-qualification-preflight-report-v1",
        "run_id": run_id,
        "profile": PROFILE,
        "seed": preflight["seed"],
        "qualified": False,
        "status": status["status"],
        "manifest_sha256": status["manifest_sha256"],
    }


def validate_aggregate_preflight_package(path: Path) -> dict[str, Any]:
    """Consume the two-file host-only output emitted by ``run_full.py``."""

    root = _require_preflight_directory(path)
    expected_paths = set(AGGREGATE_PREFLIGHT_FILES)
    actual_paths: set[str] = set()
    for entry in root.rglob("*"):
        relative = entry.relative_to(root).as_posix()
        actual_paths.add(relative)
        if not entry.is_file():
            raise QualificationError(f"aggregate preflight contains a non-file entry: {relative}")
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        extra = sorted(actual_paths - expected_paths)
        raise QualificationError(
            f"aggregate preflight file set mismatch: missing={missing}, extra={extra}"
        )

    aggregate = read_json(root / "aggregate.json", "aggregate.json")
    status = read_json(root / "status.json", "status.json")
    expected_aggregate_keys = {
        "schema_version",
        "profile",
        "qualified",
        "sessions",
        "non_claims",
        "execution_kind",
        "evidence_level",
        "status",
        "blockedReason",
    }
    if set(aggregate) != expected_aggregate_keys:
        raise QualificationError("aggregate preflight keys drifted")
    if (
        aggregate.get("schema_version") != AGGREGATE_SCHEMA
        or aggregate.get("profile") != PROFILE
        or aggregate.get("qualified") is not False
        or aggregate.get("execution_kind") != "host_contract"
        or aggregate.get("evidence_level") != "L2 host contract"
        or aggregate.get("status") != AGGREGATE_PREFLIGHT_STATUS
        or aggregate.get("blockedReason") != AGGREGATE_PREFLIGHT_BLOCKED_REASON
        or aggregate.get("non_claims") != AGGREGATE_NON_CLAIMS
    ):
        raise QualificationError("aggregate preflight is not fail-closed")
    sessions = aggregate.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != len(SEEDS):
        raise QualificationError("aggregate preflight must contain exactly three sessions")
    session_ids: list[str] = []
    session_seeds: list[int] = []
    for index, session in enumerate(sessions):
        if not isinstance(session, Mapping) or set(session) != {"run_id", "seed", "manifest_sha256"}:
            raise QualificationError(f"aggregate session[{index}] shape is invalid")
        run_id = _safe(session.get("run_id"), f"aggregate.sessions[{index}].run_id")
        seed = session.get("seed")
        if seed not in SEEDS or seed in session_seeds:
            raise QualificationError(f"aggregate session[{index}] seed is not unique/frozen")
        _sha(session.get("manifest_sha256"), f"aggregate.sessions[{index}].manifest_sha256")
        session_ids.append(run_id)
        session_seeds.append(seed)
    if sorted(session_seeds) != list(SEEDS) or len(set(session_ids)) != len(SEEDS):
        raise QualificationError("aggregate preflight does not cover seeds 7, 19, and 43 exactly once")

    expected_status_keys = {
        "schema_version",
        "profile",
        "scenario",
        "session_ids",
        "aggregate_sha256",
        "success",
        "qualified",
        "status",
        "blockedReason",
        "statusLast",
        "pushPerformed",
        "prPolicy",
    }
    if set(status) != expected_status_keys:
        raise QualificationError("aggregate preflight status keys drifted")
    if (
        status.get("schema_version") != STATUS_SCHEMA
        or status.get("profile") != PROFILE
        or status.get("scenario") != PROFILE
        or status.get("session_ids") != session_ids
        or status.get("aggregate_sha256") != sha256_file(root / "aggregate.json")
        or status.get("success") is not False
        or status.get("qualified") is not False
        or status.get("status") != AGGREGATE_PREFLIGHT_STATUS
        or status.get("blockedReason") != AGGREGATE_PREFLIGHT_BLOCKED_REASON
        or status.get("statusLast") is not True
        or status.get("pushPerformed") is not False
        or status.get("prPolicy") != "forbidden"
    ):
        raise QualificationError("aggregate preflight status is not manifest-bound/fail-closed")
    return {
        "schema_version": "p6-qualification-aggregate-preflight-report-v1",
        "profile": PROFILE,
        "session_ids": session_ids,
        "seeds": sorted(session_seeds),
        "qualified": False,
        "status": status["status"],
        "aggregate_sha256": status["aggregate_sha256"],
    }


def validate_publication_preflight_package(path: Path) -> dict[str, Any]:
    """Consume the host-only publication hash inventory and its terminal status."""

    root = _require_preflight_directory(path)
    expected_paths = set(PUBLICATION_PREFLIGHT_FILES)
    actual_paths: set[str] = set()
    for entry in root.rglob("*"):
        relative = entry.relative_to(root).as_posix()
        actual_paths.add(relative)
        if not entry.is_file():
            raise QualificationError(f"publication preflight contains a non-file entry: {relative}")
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        extra = sorted(actual_paths - expected_paths)
        raise QualificationError(
            f"publication preflight file set mismatch: missing={missing}, extra={extra}"
        )

    plan = read_json(root / "publication-preflight.json", "publication-preflight.json")
    manifest = read_json(root / "manifest.json", "manifest.json")
    status = read_json(root / "status.json", "status.json")
    expected_plan_keys = {
        "schema_version",
        "run_id",
        "source_revision",
        "qemu_identity",
        "nonce",
        "files",
        "status",
        "qualified",
        "finalDeliveryReady",
        "non_claims",
    }
    if set(plan) != expected_plan_keys:
        raise QualificationError("publication preflight keys drifted")
    run_id = _safe(plan.get("run_id"), "publication.run_id")
    if (
        plan.get("schema_version") != "p6-publication-preflight-v1"
        or plan.get("status") != AGGREGATE_PREFLIGHT_STATUS
        or plan.get("qualified") is not False
        or plan.get("finalDeliveryReady") is not False
        or plan.get("non_claims") != PUBLICATION_NON_CLAIMS
    ):
        raise QualificationError("publication preflight is not fail-closed")
    records = plan.get("files")
    if not isinstance(records, list) or not records:
        raise QualificationError("publication preflight files must be non-empty")
    checksums: list[str] = []
    seen_paths: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, Mapping) or set(record) != {"path", "size", "sha256"}:
            raise QualificationError(f"publication files[{index}] shape is invalid")
        relative = record.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or relative in seen_paths
            or relative in {"manifest.json", "status.json"}
            or any(part in {"", ".", ".."} for part in Path(relative).parts)
        ):
            raise QualificationError(f"publication files[{index}].path is invalid")
        size = record.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise QualificationError(f"publication files[{index}].size is invalid")
        digest = _sha(record.get("sha256"), f"publication files[{index}].sha256")
        seen_paths.add(relative)
        checksums.append(f"{digest}  {relative}\n")

    if (root / "checksums.sha256").read_text(encoding="utf-8") != "".join(checksums):
        raise QualificationError("publication checksums.sha256 does not match plan files")
    if (
        manifest.get("schema_version") != MANIFEST_SCHEMA
        or manifest.get("run_id") != run_id
        or manifest.get("files") != records
        or manifest.get("publication") != "host_preflight_only"
    ):
        raise QualificationError("publication manifest is not bound to the plan")

    expected_status_keys = {
        "schema_version",
        "run_id",
        "success",
        "qualified",
        "status",
        "blockedReason",
        "manifest_sha256",
        "finalDeliveryReady",
        "statusLast",
    }
    if set(status) != expected_status_keys:
        raise QualificationError("publication status keys drifted")
    if (
        status.get("schema_version") != STATUS_SCHEMA
        or status.get("run_id") != run_id
        or status.get("success") is not False
        or status.get("qualified") is not False
        or status.get("status") != AGGREGATE_PREFLIGHT_STATUS
        or status.get("blockedReason") != PUBLICATION_PREFLIGHT_BLOCKED_REASON
        or status.get("manifest_sha256") != sha256_file(root / "manifest.json")
        or status.get("finalDeliveryReady") is not False
        or status.get("statusLast") is not True
    ):
        raise QualificationError("publication status is not manifest-bound/fail-closed")
    return {
        "schema_version": "p6-publication-preflight-report-v1",
        "run_id": run_id,
        "files": len(records),
        "qualified": False,
        "status": status["status"],
        "manifest_sha256": status["manifest_sha256"],
    }


def validate_run_directory(path: Path) -> dict[str, Any]:
    root = require_run_directory(path)
    scenario = read_json(root / "input" / "scenario.json", "input/scenario.json")
    profile = read_json(root / "input" / "profile.json", "input/profile.json")
    source = read_json(root / "input" / "source.json", "input/source.json")
    for name in ("images.json", "configs.json", "model.json"):
        read_json(root / "input" / name, f"input/{name}")
    identity = validate_scenario(scenario, profile, source)
    manifest = read_json(root / "manifest.json", "manifest.json")
    manifest_sha256 = validate_manifest(root, manifest, identity["run_id"])

    stream_counts = {
        "events": _validate_stream(root / "raw" / "events.jsonl", identity["run_id"]),
        "rt_samples": _validate_stream(root / "raw" / "rt-samples.jsonl", identity["run_id"]),
        "icpc": _validate_stream(root / "raw" / "icpc.jsonl", identity["run_id"]),
        "frames": _validate_stream(root / "raw" / "frames.jsonl", identity["run_id"]),
        "trajectory": _validate_stream(root / "raw" / "trajectory.jsonl", identity["run_id"]),
        "faults": _validate_stream(root / "raw" / "faults.jsonl", identity["run_id"], fault=True),
    }
    for relative in ("raw/host.log", "raw/linux.log", "raw/zephyr.log", "raw/capture.pcap"):
        if (root / relative).stat().st_size == 0:
            raise QualificationError(f"full-evidence artifact is empty: {relative}")
    cleanup = read_json(root / "cleanup.json", "cleanup.json")
    _validate_cleanup(cleanup)
    derived = read_json(root / "derived" / "qualification-summary.json", "qualification summary")
    if derived.get("schema_version") != SCHEMA_VERSION or derived.get("run_id") != identity["run_id"]:
        raise QualificationError("qualification summary identity/schema mismatch")
    if derived.get("profile") != PROFILE or derived.get("scenario") != PROFILE:
        raise QualificationError("qualification summary profile/scenario mismatch")
    if derived.get("full_evidence") is not True or derived.get("metrics_recomputed") is not True:
        raise QualificationError("qualification summary does not prove full evidence/recomputed metrics")
    if derived.get("unclassified_failures") != 0 or derived.get("cleanup_residuals") != 0:
        raise QualificationError("qualification summary contains unclassified failures/residuals")

    status = read_json(root / "status.json", "status.json")
    if status.get("schema_version") != STATUS_SCHEMA:
        raise QualificationError(f"status schema_version must be {STATUS_SCHEMA}")
    if status.get("run_id") != identity["run_id"] or status.get("profile") != PROFILE:
        raise QualificationError("status identity/profile mismatch")
    if status.get("scenario") != PROFILE:
        raise QualificationError("status scenario mismatch")
    if status.get("success") is not True or status.get("qualified") is not True:
        raise QualificationError("status is not qualified=true")
    if status.get("status") != "contest_qualification_completed":
        raise QualificationError("status does not carry the qualification completion token")
    if status.get("statusLast") is not True:
        raise QualificationError("status-last contract is false")
    if status.get("manifest_sha256") != manifest_sha256:
        raise QualificationError("status manifest SHA-256 does not match manifest.json")
    if (
        isinstance(status.get("actual_duration_s"), bool)
        or not isinstance(status.get("actual_duration_s"), (int, float))
        or status["actual_duration_s"] < DURATION_S
    ):
        raise QualificationError("actual monotonic qualification duration is below 7200 seconds")
    completed = status.get("completedChecks")
    if not isinstance(completed, list) or not {"scenario", "evidence", "metrics", "cleanup", "hashes"}.issubset(completed):
        raise QualificationError("status completedChecks is incomplete")

    return {
        "schema_version": "p6-qualification-report-v1",
        "run_id": identity["run_id"],
        "profile": PROFILE,
        "scenario": PROFILE,
        "qualified": True,
        "actual_duration_s": status["actual_duration_s"],
        "manifest_sha256": manifest_sha256,
        "stream_counts": stream_counts,
        "non_claims": [
            "qualification does not prove hardware real-time bounds or DMA isolation",
            "qualification only covers the supplied immutable run directory",
        ],
    }
