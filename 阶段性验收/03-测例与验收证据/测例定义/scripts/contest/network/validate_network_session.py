#!/usr/bin/env python3
"""Fail-closed validation for a host-only P4 network session fixture.

This validator intentionally stops at the host evidence boundary.  It checks
identity, the frozen vnet0 endpoint contract, frame/counter consistency, the
canonical deterministic fault manifest, and status-last publication.  It does
not start a Guest, inspect QEMU, or infer Guest IP connectivity from host data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

try:
    from capture import CaptureError, read_pcap
    from fault_profile import FaultProfileError, validate_fault_manifest
except ImportError:  # pragma: no cover - used when imported as a package module
    from .capture import CaptureError, read_pcap
    from .fault_profile import FaultProfileError, validate_fault_manifest


SESSION_SCHEMA_VERSION = "p4-network-session-v1"
MANIFEST_SCHEMA_VERSION = "p4-network-manifest-v1"
COUNTERS_SCHEMA_VERSION = "p4-network-counters-v1"
STATUS_SCHEMA_VERSION = "p4-network-status-v1"
VALID_STATUS = {
    "guest_network_completed",
    "guest_network_failed",
    "guest_network_blocked",
}
SUCCESS_CHECKS = {"oracle", "hashes", "validator", "cleanup"}
FRAME_SCHEMA_VERSION = "p4-network-frame-v1"
FRAME_COUNTER_KEYS = (
    "capture_frames",
    "capture_bytes",
    "capture_drops",
    "switch_enqueue",
    "switch_deliver",
    "switch_drop",
    "fault_drop",
    "fault_duplicate",
    "fault_reorder",
    "fault_corrupt",
)
FRAME_FIELDS = (
    "schema_version",
    "sequence",
    "run_id",
    "session_id",
    "ingress_port",
    "generation",
    "direction",
    "monotonic_ns",
    "length",
    "sha256",
    "frame_hex",
)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TEST_ID = re.compile(r"TEST-[0-9]{3}\Z")
_U64_MAX = (1 << 64) - 1

EXPECTED_NETWORK = {
    "name": "vnet0",
    "cidr": "10.77.0.0/24",
    "mtu": 1500,
    "default_route": False,
    "gateway": None,
    "dns": [],
    "outer_nic": False,
    "host_uplink": False,
    "bridge": False,
    "nat": False,
    "tap": False,
    "proxy": False,
}
EXPECTED_ENDPOINTS = {
    "linux": {
        "vm_id": 1,
        "port": 0,
        "mac": "02:00:00:00:00:01",
        "ip": "10.77.0.1",
        "prefix_length": 24,
        "connected_route": "10.77.0.0/24",
        "udp_port": 46000,
        "tcp_port": 46001,
        "default_route": False,
        "gateway": None,
        "dns": [],
    },
    "zephyr": {
        "vm_id": 2,
        "port": 1,
        "mac": "02:00:00:00:00:02",
        "ip": "10.77.0.2",
        "prefix_length": 24,
        "connected_route": "10.77.0.0/24",
        "udp_port": 46000,
        "tcp_port": 46001,
        "default_route": False,
        "gateway": None,
        "dns": [],
    },
}


class ValidationError(ValueError):
    """Raised for any incomplete, mismatched, or out-of-scope session."""


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if extra:
            details.append(f"extra={','.join(extra)}")
        raise ValidationError(f"{field} keys mismatch ({'; '.join(details)})")


def _require_fields(value: Mapping[str, Any], required: set[str], field: str) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise ValidationError(f"{field} is missing required fields: {','.join(missing)}")


def _require_int(value: Any, field: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
        raise ValidationError(f"{field} must be in {bound}")
    return value


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValidationError(f"{field} must be a safe non-empty identifier")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _read_json(path: Path, field: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"{field} is missing or is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"{field} is not valid JSON: {error}") from error
    return _require_mapping(value, field)


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ValidationError(f"cannot hash {path}: {error}") from error


def _session_root(session: Path) -> Path:
    if session.is_symlink():
        raise ValidationError("session path must not be a symlink")
    if session.is_file():
        if session.name != "session.json":
            raise ValidationError("--session must be a session directory or session.json")
        root = session.parent
    else:
        root = session
    if root.is_symlink() or not root.is_dir():
        raise ValidationError(f"session root is not a regular directory: {root}")
    return root.resolve()


def _safe_artifact(root: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError(f"{field} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError(f"{field} must stay below the session directory")
    unresolved = root / relative
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValidationError(f"{field} must not use symlink components")
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValidationError(f"{field} escapes the session directory") from error
    return candidate


def _validate_identity(session: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "run_id",
        "session_id",
        "nonce",
        "test_id",
        "profile_id",
        "scenario",
        "host_only",
        "evidence_level",
        "transport",
        "clock_domain",
        "network",
        "endpoints",
        "execution",
        "identity",
        "artifacts",
        "fault_profile_id",
        "fault_packet_count",
        "fault_manifest_sha256",
        "frame_counters",
        "manifest_sha256",
    }
    _require_exact_keys(session, required, "session")
    if session["schema_version"] != SESSION_SCHEMA_VERSION:
        raise ValidationError("unsupported session schema_version")
    run_id = _require_id(session["run_id"], "session.run_id")
    session_id = _require_id(session["session_id"], "session.session_id")
    nonce = _require_id(session["nonce"], "session.nonce")
    if session_id == "0" or nonce == "0":
        raise ValidationError("session_id and nonce must be non-zero")
    if not isinstance(session["test_id"], str) or not _TEST_ID.fullmatch(session["test_id"]):
        raise ValidationError("session.test_id must be TEST-011..TEST-999")
    _require_id(session["profile_id"], "session.profile_id")
    _require_id(session["scenario"], "session.scenario")
    if session["host_only"] is not True or session["evidence_level"] != "L2 host":
        raise ValidationError("session is not explicitly host-only L2 evidence")
    if session["transport"] not in {"icmp", "udp", "tcp"}:
        raise ValidationError("session.transport is not an allowed host transport")
    if session["clock_domain"] != "monotonic_ns":
        raise ValidationError("session.clock_domain must be monotonic_ns")

    execution = _require_mapping(session["execution"], "session.execution")
    _require_exact_keys(execution, {"host_only", "qemu", "wsl", "guest_runtime"}, "session.execution")
    if execution != {"host_only": True, "qemu": False, "wsl": False, "guest_runtime": False}:
        raise ValidationError("session execution identity is not host-only")

    identity = _require_mapping(session["identity"], "session.identity")
    _require_exact_keys(identity, {"producer", "run_id", "session_id", "nonce"}, "session.identity")
    if identity["producer"] != "p4-network-host":
        raise ValidationError("unexpected host network producer")
    if (identity["run_id"], identity["session_id"], identity["nonce"]) != (
        run_id,
        session_id,
        nonce,
    ):
        raise ValidationError("session identity fields do not agree")

    _require_id(session["fault_profile_id"], "session.fault_profile_id")
    _require_int(session["fault_packet_count"], "session.fault_packet_count")
    _require_sha256(session["fault_manifest_sha256"], "session.fault_manifest_sha256")
    _require_sha256(session["manifest_sha256"], "session.manifest_sha256")


def _validate_network(session: Mapping[str, Any]) -> None:
    network = _require_mapping(session["network"], "session.network")
    _require_exact_keys(network, set(EXPECTED_NETWORK), "session.network")
    if dict(network) != EXPECTED_NETWORK:
        raise ValidationError("session network is not the frozen vnet0 topology")

    endpoints = _require_mapping(session["endpoints"], "session.endpoints")
    _require_exact_keys(endpoints, set(EXPECTED_ENDPOINTS), "session.endpoints")
    for name, expected in EXPECTED_ENDPOINTS.items():
        endpoint = _require_mapping(endpoints[name], f"session.endpoints.{name}")
        _require_exact_keys(endpoint, set(expected), f"session.endpoints.{name}")
        if dict(endpoint) != expected:
            raise ValidationError(f"endpoint {name} does not match the fixed VM/port/IP contract")


def _validate_counter_object(counters: Mapping[str, Any], field: str) -> dict[str, int]:
    _require_exact_keys(counters, set(FRAME_COUNTER_KEYS), field)
    result: dict[str, int] = {}
    for key in FRAME_COUNTER_KEYS:
        result[key] = _require_int(counters[key], f"{field}.{key}")
    return result


def _validate_artifact_map(root: Path, session: Mapping[str, Any]) -> dict[str, Path]:
    artifacts = _require_mapping(session["artifacts"], "session.artifacts")
    expected_names = {"manifest", "frames", "pcap", "counters", "fault_manifest"}
    _require_exact_keys(artifacts, expected_names, "session.artifacts")
    paths = {name: _safe_artifact(root, value, f"session.artifacts.{name}") for name, value in artifacts.items()}
    for name, path in paths.items():
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"artifact {name} is missing or not a regular file")
    return paths


def _validate_manifest(
    manifest_path: Path, session: Mapping[str, Any], artifacts: Mapping[str, Path]
) -> None:
    manifest = _read_json(manifest_path, "manifest")
    _require_exact_keys(
        manifest,
        {"schema_version", "producer", "run_id", "session_id", "nonce", "profile_id", "artifacts"},
        "manifest",
    )
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ValidationError("unsupported network manifest schema_version")
    if manifest["producer"] != "p4-network-host":
        raise ValidationError("manifest producer is not host-only")
    for field in ("run_id", "session_id", "nonce", "profile_id"):
        if manifest[field] != session[field]:
            raise ValidationError(f"manifest {field} does not match session")
    manifest_artifacts = _require_mapping(manifest["artifacts"], "manifest.artifacts")
    if dict(manifest_artifacts) != dict(session["artifacts"]):
        raise ValidationError("manifest artifact paths do not match session")
    if _sha256_file(manifest_path) != session["manifest_sha256"]:
        raise ValidationError("session manifest_sha256 does not match manifest bytes")


def _validate_fault_manifest(
    paths: Mapping[str, Path], session: Mapping[str, Any]
) -> dict[str, int]:
    fault_path = paths["fault_manifest"]
    manifest = _read_json(fault_path, "fault manifest")
    if _sha256_file(fault_path) != session["fault_manifest_sha256"]:
        raise ValidationError("session fault_manifest_sha256 does not match fault manifest bytes")
    try:
        plan = validate_fault_manifest(
            manifest,
            expected_packet_count=session["fault_packet_count"],
            expected_profile_id=session["fault_profile_id"],
        )
    except FaultProfileError as error:
        raise ValidationError(f"fault manifest is invalid: {error}") from error
    return plan.expected_counts()


def _decode_frame_hex(value: Any, field: str) -> bytes:
    if not isinstance(value, str) or len(value) % 2:
        raise ValidationError(f"{field} must be an even-length hex string")
    try:
        return bytes.fromhex(value)
    except ValueError as error:
        raise ValidationError(f"{field} is not valid hexadecimal") from error


def _validate_frames(path: Path, session: Mapping[str, Any]) -> tuple[tuple[bytes, ...], int]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValidationError(f"cannot read frame capture: {error}") from error
    frames: list[bytes] = []
    total_bytes = 0
    last_timestamp: int | None = None
    last_generation: dict[int, int] = {}
    for line_number, line in enumerate(lines, start=1):
        location = f"{path}:{line_number}"
        if not line.strip():
            raise ValidationError(f"{location}: blank lines are not allowed")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValidationError(f"{location}: invalid JSON: {error.msg}") from error
        record = _require_mapping(record, location)
        _require_exact_keys(record, set(FRAME_FIELDS), location)
        if record["schema_version"] != FRAME_SCHEMA_VERSION:
            raise ValidationError(f"{location}: unsupported frame schema_version")
        sequence = _require_int(record["sequence"], f"{location}.sequence")
        if sequence != len(frames):
            raise ValidationError(f"{location}: sequence is not contiguous from zero")
        if record["run_id"] != session["run_id"] or record["session_id"] != session["session_id"]:
            raise ValidationError(f"{location}: frame identity does not match session")
        port = _require_int(record["ingress_port"], f"{location}.ingress_port", maximum=1)
        generation = _require_int(record["generation"], f"{location}.generation", maximum=_U64_MAX)
        direction = record["direction"]
        if direction != f"port{port}_to_port{1 - port}":
            raise ValidationError(f"{location}: direction does not match ingress port")
        monotonic_ns = _require_int(
            record["monotonic_ns"], f"{location}.monotonic_ns", maximum=_U64_MAX
        )
        if last_timestamp is not None and monotonic_ns < last_timestamp:
            raise ValidationError(f"{location}: monotonic_ns moved backwards")
        previous_generation = last_generation.get(port)
        if previous_generation is not None:
            if generation < previous_generation or generation > previous_generation + 1:
                raise ValidationError(f"{location}: invalid port generation transition")
        last_timestamp = monotonic_ns
        last_generation[port] = generation
        frame = _decode_frame_hex(record["frame_hex"], f"{location}.frame_hex")
        if not 14 <= len(frame) <= 1514:
            raise ValidationError(f"{location}: Ethernet frame length is outside 14..1514")
        if record["length"] != len(frame):
            raise ValidationError(f"{location}: length does not match frame bytes")
        if record["sha256"] != hashlib.sha256(frame).hexdigest():
            raise ValidationError(f"{location}: sha256 does not match frame bytes")
        frames.append(frame)
        total_bytes += len(frame)
    return tuple(frames), total_bytes


def _validate_pcap(path: Path, frames: tuple[bytes, ...]) -> None:
    try:
        records = read_pcap(path)
    except CaptureError as error:
        raise ValidationError(f"PCAP is invalid: {error}") from error
    if len(records) != len(frames):
        raise ValidationError("PCAP packet count does not match frame capture")
    for index, ((_, _, pcap_frame), frame) in enumerate(zip(records, frames)):
        if pcap_frame != frame:
            raise ValidationError(f"PCAP packet {index} does not match frame capture")


def _validate_counters(
    paths: Mapping[str, Path], session: Mapping[str, Any], frames: tuple[bytes, ...], frame_bytes: int, fault_counts: Mapping[str, int]
) -> dict[str, int]:
    counters_document = _read_json(paths["counters"], "counters")
    _require_exact_keys(
        counters_document,
        {"schema_version", "run_id", "session_id", "counters"},
        "counters",
    )
    if counters_document["schema_version"] != COUNTERS_SCHEMA_VERSION:
        raise ValidationError("unsupported counters schema_version")
    if counters_document["run_id"] != session["run_id"] or counters_document["session_id"] != session["session_id"]:
        raise ValidationError("counters identity does not match session")
    counters = _validate_counter_object(
        _require_mapping(counters_document["counters"], "counters.counters"),
        "counters.counters",
    )
    session_counters = _validate_counter_object(
        _require_mapping(session["frame_counters"], "session.frame_counters"),
        "session.frame_counters",
    )
    if counters != session_counters:
        raise ValidationError("session frame_counters do not match counters artifact")
    if counters["capture_frames"] != len(frames):
        raise ValidationError("capture_frames does not match frame JSONL count")
    if counters["capture_bytes"] != frame_bytes:
        raise ValidationError("capture_bytes does not match frame JSONL lengths")
    if counters["fault_drop"] != fault_counts["drop"]:
        raise ValidationError("fault_drop does not match the canonical fault manifest")
    if counters["fault_duplicate"] != fault_counts["duplicate"]:
        raise ValidationError("fault_duplicate does not match the canonical fault manifest")
    if counters["fault_reorder"] != fault_counts["reorder"]:
        raise ValidationError("fault_reorder does not match the canonical fault manifest")
    if counters["fault_corrupt"] != fault_counts["corrupt"]:
        raise ValidationError("fault_corrupt does not match the canonical fault manifest")
    return counters


def _validate_status(root: Path, session: Mapping[str, Any], paths: Mapping[str, Path]) -> Mapping[str, Any]:
    status_path = root / "status.json"
    status = _read_json(status_path, "status")
    required_status_fields = {
        "schema_version",
        "success",
        "status",
        "primaryError",
        "cleanupError",
        "completedChecks",
        "manifestSha256",
        "statusLast",
    }
    _require_exact_keys(
        status,
        required_status_fields
        | ({"blockedReason"} if status.get("status") == "guest_network_blocked" else set()),
        "status",
    )
    if status["schema_version"] != STATUS_SCHEMA_VERSION:
        raise ValidationError("unsupported status schema_version")
    if not isinstance(status["success"], bool) or status["status"] not in VALID_STATUS:
        raise ValidationError("status success/token combination is invalid")
    if status["manifestSha256"] != session["manifest_sha256"]:
        raise ValidationError("status manifestSha256 does not match session")
    if status["statusLast"] is not True:
        raise ValidationError("status.json is not marked status-last")
    checks = status["completedChecks"]
    if not isinstance(checks, list) or any(not isinstance(item, str) for item in checks):
        raise ValidationError("status completedChecks must be a string list")
    if len(set(checks)) != len(checks):
        raise ValidationError("status completedChecks contains duplicates")
    if status["success"]:
        if status["status"] != "guest_network_completed":
            raise ValidationError("successful session has the wrong status token")
        if status["primaryError"] is not None or status["cleanupError"] is not None:
            raise ValidationError("successful session contains an error")
        if not SUCCESS_CHECKS.issubset(checks):
            raise ValidationError("successful session is missing a completed check")
    elif status["status"] == "guest_network_blocked":
        if not isinstance(status["primaryError"], str) or not status["primaryError"]:
            raise ValidationError("blocked session requires primaryError")
        if not isinstance(status.get("blockedReason"), str) or not status["blockedReason"]:
            raise ValidationError("blocked session requires blockedReason")
    elif status["status"] == "guest_network_failed":
        if not isinstance(status["primaryError"], str) or not status["primaryError"]:
            raise ValidationError("failed session requires primaryError")

    try:
        status_mtime = status_path.stat().st_mtime_ns
    except OSError as error:
        raise ValidationError(f"cannot stat status: {error}") from error
    for name, path in {"session": root / "session.json", **paths}.items():
        try:
            if status_mtime < path.stat().st_mtime_ns:
                raise ValidationError(f"status.json was written before {name}")
        except OSError as error:
            raise ValidationError(f"cannot stat {name}: {error}") from error
    return status


def _validate_profile(profile_path: Path | None, session: Mapping[str, Any]) -> None:
    if profile_path is None:
        return
    profile = _read_json(profile_path, "network profile")
    required = {"schema_version", "profile_id", "test_id", "scenario", "host_only", "evidence_level", "transport", "network", "endpoints", "fault"}
    _require_fields(profile, required, "network profile")
    if profile["schema_version"] != "p4-network-profile-v1":
        raise ValidationError("unsupported network profile schema_version")
    for field in ("profile_id", "test_id", "scenario", "transport"):
        if profile[field] != session[field if field != "profile_id" else "profile_id"]:
            raise ValidationError(f"network profile {field} does not match session")
    if profile["host_only"] is not True or profile["evidence_level"] != "L2 host":
        raise ValidationError("network profile is not host-only")
    if dict(_require_mapping(profile["network"], "network profile.network")) != EXPECTED_NETWORK:
        raise ValidationError("network profile topology is not frozen")
    endpoints = _require_mapping(profile["endpoints"], "network profile.endpoints")
    if dict(endpoints) != EXPECTED_ENDPOINTS or dict(session["endpoints"]) != EXPECTED_ENDPOINTS:
        raise ValidationError("network profile endpoints are not frozen")
    try:
        from fault_profile import parse_fault_profile
    except ImportError:  # pragma: no cover
        from .fault_profile import parse_fault_profile
    try:
        fault = parse_fault_profile(profile)
    except FaultProfileError as error:
        raise ValidationError(f"network profile fault section is invalid: {error}") from error
    if fault.profile_id != session["fault_profile_id"]:
        raise ValidationError("network profile fault profile_id does not match session")


def validate_session(session: Path, profile_path: Path | None = None) -> dict[str, Any]:
    """Validate one immutable host-only session directory and return a report."""

    root = _session_root(session)
    session_document = _read_json(root / "session.json", "session")
    _validate_identity(session_document)
    _validate_network(session_document)
    _validate_profile(profile_path, session_document)
    paths = _validate_artifact_map(root, session_document)
    _validate_manifest(paths["manifest"], session_document, paths)
    fault_counts = _validate_fault_manifest(paths, session_document)
    frames, frame_bytes = _validate_frames(paths["frames"], session_document)
    _validate_pcap(paths["pcap"], frames)
    counters = _validate_counters(paths, session_document, frames, frame_bytes, fault_counts)
    status = _validate_status(root, session_document, paths)
    return {
        "schema_version": "p4-network-session-validation-v1",
        "valid": True,
        "run_id": session_document["run_id"],
        "session_id": session_document["session_id"],
        "test_id": session_document["test_id"],
        "profile_id": session_document["profile_id"],
        "evidence_level": session_document["evidence_level"],
        "status": status["status"],
        "capture_frames": counters["capture_frames"],
        "capture_bytes": counters["capture_bytes"],
        "capture_drops": counters["capture_drops"],
        "fault_counts": fault_counts,
        "manifest_sha256": session_document["manifest_sha256"],
    }


def _write_result(path: Path, report: Mapping[str, Any]) -> None:
    if path.exists():
        raise ValidationError(f"refusing to overwrite validation output {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate_session(args.session, args.profile)
        if args.output is not None:
            _write_result(args.output, report)
    except (ValidationError, OSError) as error:
        print(f"P4 network session contract failed: {error}", file=sys.stderr)
        return 1
    print(
        "P4_NETWORK_SESSION_PASS "
        f"test={report['test_id']} frames={report['capture_frames']} "
        f"status={report['status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
