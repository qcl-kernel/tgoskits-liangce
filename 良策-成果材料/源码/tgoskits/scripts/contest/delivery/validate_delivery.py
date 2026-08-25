#!/usr/bin/env python3
"""Build a local P7 review manifest; never invokes push or PR operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_vm_carveout_io import publish_new_file  # noqa: E402

try:
    import delivery_contract as contract
    import clean_tree
except ImportError:  # pragma: no cover - package import path
    from . import delivery_contract as contract
    from . import clean_tree


def _write(path: Path, value: object) -> None:
    publish_new_file(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        error_type=contract.DeliveryError,
    )


def write_preflight(preflight: dict[str, object], output_dir: Path) -> None:
    if output_dir.exists():
        raise contract.DeliveryError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    review_manifest = dict(preflight)
    review_manifest["manifestSha256"] = contract.manifest_digest(preflight)
    _write(output_dir / "review-manifest.json", review_manifest)
    checksums = "".join(
        f"{entry['sha256']}  {entry['path']}\n" for entry in review_manifest["files"]  # type: ignore[index]
    )
    publish_new_file(
        output_dir / "checksums.sha256",
        checksums.encode("utf-8"),
        error_type=contract.DeliveryError,
    )
    status = {
        "schema_version": contract.STATUS_SCHEMA,
        "success": not bool(preflight["errors"]),
        "status": preflight["status"],
        "repository": preflight["repository"],
        "source_revision": preflight["source_revision"],
        "base_revision": preflight["base_revision"],
        "source_repository": preflight["source_repository"],
        "head": preflight["head"],
        "base_is_ancestor": preflight["base_is_ancestor"],
        "clean_tree_status": preflight["clean_tree_status"],
        "review_metadata": preflight["review_metadata"],
        "finalDeliveryReady": False,
        "manifestSha256": review_manifest["manifestSha256"],
        "pushPerformed": False,
        "prPolicy": "forbidden",
        "statusLast": True,
    }
    _write(output_dir / "status.json", status)
    contract.validate_review_bundle(output_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-repository", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--base-revision", required=True)
    parser.add_argument("--dry-run", action="store_true", help="document local-only intent")
    args = parser.parse_args(argv)
    try:
        if not args.dry_run:
            raise contract.DeliveryError("delivery preflight is local-only; pass --dry-run")
        source_report = clean_tree.read_repository_report(
            args.source_repository, args.source_revision, args.base_revision
        )
        if not source_report["clean"]:
            raise contract.DeliveryError(
                "source repository clean-tree failed: "
                + json.dumps(
                    {
                        "status": source_report["status"],
                        "dirty_paths": source_report["dirty_paths"],
                        "git_warnings": source_report["git_warnings"],
                        "head": source_report["head"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        if args.output_dir.resolve().is_relative_to(args.source_root.resolve()):
            raise contract.DeliveryError("output directory must not be inside source root")
        preflight = contract.build_preflight(
            args.source_root,
            repository=args.repository,
            source_revision=args.source_revision,
            base_revision=args.base_revision,
        )
        preflight.update(
            {
                "source_repository": str(args.source_repository.resolve()),
                "head": source_report["head"],
                "base_is_ancestor": source_report["base_is_ancestor"],
                "clean_tree_status": source_report["status"],
            }
        )
        write_preflight(preflight, args.output_dir)
    except (OSError, contract.DeliveryError) as error:
        print(f"P7 delivery preflight failed: {error}")
        return 1
    if preflight["errors"]:
        print(f"P7_DELIVERY_PREFLIGHT_FAIL errors={len(preflight['errors'])}")
        return 1
    print(f"P7_DELIVERY_PREFLIGHT_PASS files={len(preflight['files'])} push=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
