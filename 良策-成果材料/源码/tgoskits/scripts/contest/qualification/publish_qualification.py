#!/usr/bin/env python3
"""Create a non-qualified P6 evidence publication preflight.

The final P6 publisher is intentionally unavailable until real runtime bundles
and their independent validator are present.  This command only hashes an
existing run directory and records why no formal completion token is emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    import qualification_contract as contract
except ImportError:  # pragma: no cover - package import path
    from . import qualification_contract as contract


SCHEMA = "p6-publication-preflight-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_preflight(run_directory: Path) -> dict[str, Any]:
    root = contract.require_run_directory(run_directory)
    scenario = contract.read_json(root / "input" / "scenario.json", "input/scenario.json")
    run_id = scenario.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise contract.QualificationError("scenario.run_id is missing")
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"manifest.json", "status.json"}:
            continue
        relative = path.relative_to(root).as_posix()
        records.append(
            {"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)}
        )
    return {
        "schema_version": SCHEMA,
        "run_id": run_id,
        "source_revision": scenario.get("source_revision"),
        "qemu_identity": scenario.get("qemu_identity"),
        "nonce": scenario.get("nonce"),
        "files": records,
        "status": "qualification_smoke_completed",
        "qualified": False,
        "finalDeliveryReady": False,
        "non_claims": list(contract.PUBLICATION_NON_CLAIMS),
    }


def write_preflight(plan: dict[str, Any], output_dir: Path) -> None:
    if output_dir.exists():
        raise contract.QualificationError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    contract.write_json(output_dir / "publication-preflight.json", plan)
    checksums = "".join(
        f"{item['sha256']}  {item['path']}\n" for item in plan["files"]
    )
    contract.publish_new_file(
        output_dir / "checksums.sha256",
        checksums.encode("utf-8"),
        error_type=contract.QualificationError,
    )
    manifest = {
        "schema_version": contract.MANIFEST_SCHEMA,
        "run_id": plan["run_id"],
        "files": plan["files"],
        "publication": "host_preflight_only",
    }
    contract.write_json(output_dir / "manifest.json", manifest)
    contract.write_json(
        output_dir / "status.json",
        {
            "schema_version": contract.STATUS_SCHEMA,
            "run_id": plan["run_id"],
            "success": False,
            "qualified": False,
            "status": "qualification_smoke_completed",
            "blockedReason": contract.PUBLICATION_PREFLIGHT_BLOCKED_REASON,
            "manifest_sha256": contract.sha256_file(output_dir / "manifest.json"),
            "finalDeliveryReady": False,
            "statusLast": True,
        },
    )
    contract.validate_publication_preflight_package(output_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.dry_run:
            raise contract.QualificationError(
                "P6 publication is host-preflight only; pass --dry-run"
            )
        plan = build_preflight(args.run)
        write_preflight(plan, args.output_dir)
    except (OSError, json.JSONDecodeError, contract.QualificationError) as error:
        print(f"P6 publication preflight failed closed: {error}")
        return 1
    print(f"P6_PUBLICATION_DRY_RUN_PASS run={plan['run_id']} qualified=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
