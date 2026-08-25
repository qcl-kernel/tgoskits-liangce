#!/usr/bin/env python3
"""P4-EVID-01B: Guest-runtime v2 bundle publisher (host side, fail-closed).

Turns a runtime-smoke evidence bundle into the v2 artifacts the qualification
validator requires and then runs the fail-closed qualification gate:

  session.json  (p4-network-session-v2, binds run/session/nonce/identity)
  manifest.json (p4-network-manifest-v1, byte-exact sha256 bound to session)
  cleanup.json  (residual process/socket report)
  status.json   (p4-network-status-v2, written LAST, statusLast=true)

Rules:
  * Pure host Python: never launches QEMU, never mutates evidence artifacts
    (dtb/network/metrics/logs/configs/commands.jsonl are read-only inputs).
  * session/manifest/cleanup/status are runner state files and may be
    re-published by this publisher, always in the order manifest -> session ->
    cleanup -> status, so status.json is the last file written.
  * Never upgrades an evidence level: `guest_network_qualified` is only
    emitted by the qualification validator when every gate holds. Anything
    short of that is written as a fail-closed smoke status with
    qualified=false and the qualification error recorded as primaryError.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from ..host_vm_carveout_io import REPARSE_FLAG, publish_new_file
except ImportError:  # pragma: no cover - direct script execution
    from host_vm_carveout_io import REPARSE_FLAG, publish_new_file

SESSION_SCHEMA_VERSION = "p4-network-session-v2"
STATUS_SCHEMA_VERSION = "p4-network-status-v2"
MANIFEST_SCHEMA_VERSION = "p4-network-manifest-v1"

SUCCESS_CHECKS = {"oracle", "hashes", "validator", "cleanup"}
QUALIFIED_STATUS = "guest_network_qualified"
SMOKE_STATUS = "guest_network_smoke_completed"

# Artifacts the qualification validator demands (relative to the bundle root).
REQUIRED_ARTIFACTS = (
    "configs/profile.json",
    "configs/soak-provenance.json",
    "dtb/linux-final.dtb",
    "dtb/linux-final.dts",
    "dtb/zephyr-final.dtb",
    "dtb/zephyr-final.dts",
    "network/frames.jsonl",
    "network/capture.pcap",
    "network/counters.json",
    "metrics/raw.jsonl",
    "metrics/summary.json",
)

_SAFE_ID = __import__("re").compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")


class V2PublisherError(ValueError):
    """A v2 bundle transition violates the evidence contract."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, obj: Any) -> None:
    """Publish runner state without following links; allow owned-state refresh."""

    data = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        publish_new_file(path, data, error_type=V2PublisherError)
        return
    if (
        stat.S_ISLNK(existing.st_mode)
        or bool(int(getattr(existing, "st_file_attributes", 0)) & REPARSE_FLAG)
        or not stat.S_ISREG(existing.st_mode)
    ):
        raise V2PublisherError(f"runner state output is not a regular file: {path}")

    staged = path.with_name(f".{path.name}.replace-{os.getpid()}-{secrets.token_hex(8)}")
    publish_new_file(staged, data, error_type=V2PublisherError)
    try:
        current = path.lstat()
        if (
            stat.S_ISLNK(current.st_mode)
            or bool(int(getattr(current, "st_file_attributes", 0)) & REPARSE_FLAG)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (existing.st_dev, existing.st_ino)
        ):
            raise V2PublisherError(f"runner state output changed before replacement: {path}")
        os.replace(staged, path)
    finally:
        if staged.exists():
            staged.unlink()


def _require_safe(value: str, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise V2PublisherError(f"{field} must be a non-empty safe identifier")
    return value


def write_manifest_v1(
    root: Path, *, extra_files: Sequence[Path] = ()
) -> tuple[Path, str]:
    """Write p4-network-manifest-v1 covering bundle artifacts + logs/configs.

    Returns (manifest_path, manifest_sha256). The manifest file itself is not
    self-referentially hashed; session.json binds its byte-exact sha256.
    status.json and cleanup.json are excluded because they are published after
    this file (status must be last).
    """
    files: dict[str, str] = {}
    for rel in REQUIRED_ARTIFACTS:
        path = root / rel
        if path.is_file():
            files[rel] = _sha256_file(path)
    for rel in (
        "commands.jsonl",
        "logs/axvisor.raw.log",
        "logs/linux.raw.log",
        "logs/zephyr.raw.log",
    ):
        path = root / rel
        if path.is_file():
            files[rel] = _sha256_file(path)
    for path in extra_files:
        rel = path.relative_to(root).as_posix()
        if path.is_file() and rel not in files:
            files[rel] = _sha256_file(path)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "files": files,
    }
    manifest_path = root / "manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path, _sha256_file(manifest_path)


def build_session_v2(
    *,
    run_id: str,
    session_id: str,
    nonce: str,
    test_id: str,
    profile_id: str,
    scenario: str,
    evidence_level: str,
    transport: str,
    network: Mapping[str, Any],
    endpoints: Mapping[str, Any],
    manifest_sha256: str,
) -> dict[str, Any]:
    """Build the p4-network-session-v2 document (field-exact schema)."""
    for field, value in (
        ("run_id", run_id),
        ("session_id", session_id),
        ("nonce", nonce),
        ("test_id", test_id),
        ("profile_id", profile_id),
        ("scenario", scenario),
        ("transport", transport),
    ):
        _require_safe(str(value), f"session.{field}")
    if evidence_level not in {"L6 stability", "L7 Guest-IP"}:
        raise V2PublisherError(f"session.evidence_level must be L6/L7 Guest-IP: {evidence_level!r}")
    return {
        "schema_version": SESSION_SCHEMA_VERSION,
        "run_id": run_id,
        "session_id": session_id,
        "nonce": nonce,
        "test_id": test_id,
        "profile_id": profile_id,
        "scenario": scenario,
        "host_only": False,
        "evidence_level": evidence_level,
        "transport": transport,
        "clock_domain": "monotonic_ns",
        "network": dict(network),
        "endpoints": dict(endpoints),
        "execution": {
            "host_only": False,
            "qemu": True,
            "wsl": True,
            "guest_runtime": True,
        },
        "identity": {
            "producer": "p4-network-guest",
            "run_id": run_id,
            "session_id": session_id,
            "nonce": nonce,
        },
        "manifest_sha256": manifest_sha256,
    }


def write_cleanup_v1(
    root: Path,
    *,
    residual_processes: Sequence[str] = (),
    residual_sockets: Sequence[str] = (),
    residual_files: Sequence[str] = (),
) -> Path:
    """Write cleanup.json; residual processes/sockets fail the v2 gate."""
    cleanup = {
        "schema_version": "p4-network-cleanup-v1",
        "residualProcesses": list(residual_processes),
        "residualSockets": list(residual_sockets),
        "residualFiles": list(residual_files),
    }
    path = root / "cleanup.json"
    _write_json(path, cleanup)
    return path


def write_status_v2(
    root: Path,
    *,
    success: bool,
    status: str,
    qualified: bool,
    primary_error: str | None,
    cleanup_error: str | None,
    completed_checks: Sequence[str],
    manifest_sha256: str,
) -> Path:
    """Write p4-network-status-v2. Must be the LAST artifact written."""
    document = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "success": success,
        "status": status,
        "qualified": qualified,
        "primaryError": primary_error,
        "cleanupError": cleanup_error,
        "completedChecks": sorted(completed_checks),
        "manifestSha256": manifest_sha256,
        "statusLast": True,
    }
    path = root / "status.json"
    _write_json(path, document)
    return path


def publish_v2(
    bundle_root: Path,
    profile_path: Path,
    *,
    source_soak_session: Path,
    run_id: str,
    session_id: str,
    nonce: str,
    test_id: str,
    profile_id: str,
    scenario: str,
    evidence_level: str,
    transport: str,
    network: Mapping[str, Any],
    endpoints: Mapping[str, Any],
    residual_processes: Sequence[str] = (),
    residual_sockets: Sequence[str] = (),
    residual_files: Sequence[str] = (),
    cleanup_error: str | None = None,
    extra_files: Sequence[Path] = (),
) -> dict[str, Any]:
    """Publish the v2 bundle and run the fail-closed qualification gate.

    Order: manifest.json -> session.json -> cleanup.json -> qualify ->
    status.json (last). Returns a structured result; on any qualification
    failure the bundle is written as a fail-closed smoke (qualified=false)
    and the error is preserved -- never a silent upgrade.
    """
    root = Path(bundle_root)
    if not root.is_dir():
        raise V2PublisherError(f"bundle root is not a directory: {root}")

    manifest_path, manifest_sha256 = write_manifest_v1(
        root, extra_files=extra_files
    )
    session = build_session_v2(
        run_id=run_id,
        session_id=session_id,
        nonce=nonce,
        test_id=test_id,
        profile_id=profile_id,
        scenario=scenario,
        evidence_level=evidence_level,
        transport=transport,
        network=network,
        endpoints=endpoints,
        manifest_sha256=manifest_sha256,
    )
    _write_json(root / "session.json", session)
    write_cleanup_v1(
        root,
        residual_processes=residual_processes,
        residual_sockets=residual_sockets,
        residual_files=residual_files,
    )

    # Claim the qualified token first, then let the independent qualification
    # validator re-check the whole bundle.  status.json is written before the
    # check and (if the check fails) rewritten after it, so status.json is
    # always the last file written in the bundle and stays status-last.
    write_status_v2(
        root,
        success=True,
        status=QUALIFIED_STATUS,
        qualified=True,
        primary_error=None,
        cleanup_error=cleanup_error,
        completed_checks=sorted(SUCCESS_CHECKS),
        manifest_sha256=manifest_sha256,
    )

    try:
        from qualify_network_session import qualify_guest_session

        result = qualify_guest_session(
            root, profile_path, source_soak=source_soak_session
        )
    except Exception as error:  # noqa: BLE001 - any gate failure is fail-closed
        qualified_error = str(error)
        write_status_v2(
            root,
            success=True,
            status=SMOKE_STATUS,
            qualified=False,
            primary_error=f"qualification failed: {qualified_error}",
            cleanup_error=cleanup_error,
            completed_checks=("oracle", "cleanup"),
            manifest_sha256=manifest_sha256,
        )
        return {
            "valid": False,
            "status": SMOKE_STATUS,
            "qualified": False,
            "error": qualified_error,
        }

    return {
        "valid": True,
        "status": QUALIFIED_STATUS,
        "qualified": True,
        "error": None,
        "capture_frames": result.get("capture_frames"),
        "manifest_sha256": manifest_sha256,
        "manifest_path": str(manifest_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--from-soak-session", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--evidence-level", required=True)
    parser.add_argument("--transport", required=True)
    parser.add_argument("--network-json", required=True, type=Path)
    parser.add_argument("--endpoints-json", required=True, type=Path)
    args = parser.parse_args(argv)

    network = json.loads(args.network_json.read_text(encoding="utf-8"))
    endpoints = json.loads(args.endpoints_json.read_text(encoding="utf-8"))
    result = publish_v2(
        args.bundle,
        args.profile,
        source_soak_session=args.from_soak_session,
        run_id=args.run_id,
        session_id=args.session_id,
        nonce=args.nonce,
        test_id=args.test_id,
        profile_id=args.profile_id,
        scenario=args.scenario,
        evidence_level=args.evidence_level,
        transport=args.transport,
        network=network,
        endpoints=endpoints,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
