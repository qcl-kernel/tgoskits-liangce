#!/usr/bin/env python3
"""Prepare one fail-closed P6 qualification session.

The runtime backend is intentionally disabled in this Windows host candidate.
The command freezes the combined profile, session identity and phase plan, but
never emits ``contest_qualification_completed`` or ``qualified=true``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

try:
    import qualification_contract as contract
except ImportError:  # pragma: no cover - package import path
    from . import qualification_contract as contract


SCENARIO_SCHEMA = 1
STATUS = "qualification_smoke_completed"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe(value: Any, field: str) -> str:
    if not isinstance(value, str) or contract.SAFE_ID.fullmatch(value) is None:
        raise contract.QualificationError(f"{field} must be a safe identifier")
    return value


def build_preflight(
    *,
    run_id: str,
    seed: int,
    profile_path: Path,
    source_revision: str = "unbound",
    qemu_identity: str = "unbound",
    nonce: str = "unbound",
) -> dict[str, Any]:
    run_id = _safe(run_id, "run_id")
    if seed not in contract.SEEDS:
        raise contract.QualificationError(f"seed must be one of {contract.SEEDS}")
    source_revision = _safe(source_revision, "source_revision")
    qemu_identity = _safe(qemu_identity, "qemu_identity")
    nonce = _safe(nonce, "nonce")
    profile = contract.read_json(Path(profile_path), "combined qualification profile")

    # Reuse the contract's frozen checks by supplying a deliberately incomplete
    # source document only for identity validation.  The resulting artifact is
    # still a preflight and explicitly records that immutable inputs are not
    # bound until the target/runtime environment is available.
    scenario = dict(profile)
    scenario.update(
        {
            "schema_version": SCENARIO_SCHEMA,
            "run_id": run_id,
            "session_id": run_id,
            "seed": seed,
            "qemu_identity": qemu_identity,
            "nonce": nonce,
            "source_revision": source_revision,
            "required_tests": list(contract.REQUIRED_TESTS),
        }
    )
    source = {
        "verified_tests": [],
        "images": {},
        "configs": {},
        "model": {},
    }
    try:
        contract.validate_scenario(scenario, profile, source, allow_unbound=True)
    except contract.QualificationError as error:
        # A host preflight must be useful even before immutable target inputs
        # exist.  The only expected failure here is the deliberately empty
        # prerequisite source; profile/identity drift is still fatal.
        if "verified_tests does not prove" not in str(error):
            raise
    return {
        "schema_version": contract.SCHEMA_VERSION,
        "scenario_schema_version": SCENARIO_SCHEMA,
        "run_id": run_id,
        "session_id": run_id,
        "seed": seed,
        "profile": contract.PROFILE,
        "duration_s": contract.DURATION_S,
        "phases": list(profile["phases"]),
        "source_revision": source_revision,
        "qemu_identity": qemu_identity,
        "nonce": nonce,
        "execution": "not_started",
        "runtime_backend": "disabled",
        "qualified": False,
        "blocked_reason": "host-only preflight; WSL/QEMU/Guest prerequisites are unavailable",
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    contract.write_json(path, value)


def _write_manifest(root: Path, run_id: str) -> str:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"manifest.json", "status.json"}:
            continue
        relative = path.relative_to(root).as_posix()
        records.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "producer": "scripts/contest/qualification/run_qualification.py",
            }
        )
    manifest = {
        "schema_version": contract.MANIFEST_SCHEMA,
        "run_id": run_id,
        "files": records,
        "excluded_terminal_files": ["manifest.json", "status.json"],
    }
    _write_json(root / "manifest.json", manifest)
    return _sha256(root / "manifest.json")


def write_preflight(plan: Mapping[str, Any], profile_path: Path, output_dir: Path) -> None:
    if output_dir.exists():
        raise contract.QualificationError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    _write_json(output_dir / "preflight.json", plan)
    _write_json(
        output_dir / "profile.json",
        contract.read_json(profile_path, "combined qualification profile"),
    )
    contract.publish_new_file(
        output_dir / "commands.jsonl",
        (
            json.dumps(
                {
                    "schema_version": "p6-qualification-command-v1",
                    "run_id": plan["run_id"],
                    "seed": plan["seed"],
                    "execution": "not_started",
                    "argv": [],
                    "reason": "WSL/QEMU requires explicit authorization and target prerequisites",
                },
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
        error_type=contract.QualificationError,
    )
    _write_json(
        output_dir / "cleanup.json",
        {"residualProcesses": [], "residualSockets": [], "residualFiles": []},
    )
    manifest_sha256 = _write_manifest(output_dir, str(plan["run_id"]))
    _write_json(
        output_dir / "status.json",
        {
            "schema_version": contract.STATUS_SCHEMA,
            "run_id": plan["run_id"],
            "seed": plan["seed"],
            "profile": contract.PROFILE,
            "scenario": contract.PROFILE,
            "success": False,
            "qualified": False,
            "status": STATUS,
            "blockedReason": plan["blocked_reason"],
            "manifest_sha256": manifest_sha256,
            "runtimeEvidence": False,
            "pushPerformed": False,
            "prPolicy": "forbidden",
            "statusLast": True,
        },
    )
    contract.validate_preflight_package(output_dir, expected_run_id=str(plan["run_id"]))


def main(argv: list[str] | None = None) -> int:
    repository = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seed", choices=contract.SEEDS, type=int, required=True)
    parser.add_argument(
        "--profile",
        type=Path,
        default=repository / "configs" / "contest" / "qualification" / "combined-v1.json",
    )
    parser.add_argument("--source-revision", default="unbound")
    parser.add_argument("--qemu-identity", default="unbound")
    parser.add_argument("--nonce", default="unbound")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.dry_run:
            raise contract.QualificationError(
                "P6 runtime execution is disabled; pass --dry-run for host preflight"
            )
        plan = build_preflight(
            run_id=args.run_id,
            seed=args.seed,
            profile_path=args.profile,
            source_revision=args.source_revision,
            qemu_identity=args.qemu_identity,
            nonce=args.nonce,
        )
        write_preflight(plan, args.profile, args.output_dir)
    except (OSError, json.JSONDecodeError, contract.QualificationError) as error:
        print(f"P6 qualification preflight failed closed: {error}")
        return 1
    print(f"P6_QUALIFICATION_DRY_RUN_PASS seed={args.seed} qualified=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
