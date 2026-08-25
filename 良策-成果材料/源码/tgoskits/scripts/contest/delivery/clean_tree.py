#!/usr/bin/env python3
"""Read-only clean-tree/base preflight for P7 local delivery review."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_vm_carveout_io import publish_new_file  # noqa: E402


REVISION = re.compile(r"[0-9a-f]{40}\Z")
SCHEMA = "p7-clean-tree-v1"
STATUS_SCHEMA = "p7-clean-tree-status-v1"


class CleanTreeError(ValueError):
    """Raised when a clean-tree preflight is unsafe or incomplete."""


def _revision(value: str, field: str) -> str:
    if REVISION.fullmatch(value) is None:
        raise CleanTreeError(f"{field} must be a 40-hex revision")
    return value


def build_report(
    *,
    repository: Path,
    source_revision: str,
    base_revision: str,
    head: str,
    porcelain_status: str,
    base_is_ancestor: bool = True,
    git_warnings: str = "",
) -> dict[str, Any]:
    repository = repository.resolve()
    if not repository.is_dir():
        raise CleanTreeError(f"repository is not a directory: {repository}")
    source_revision = _revision(source_revision, "source_revision")
    base_revision = _revision(base_revision, "base_revision")
    head = _revision(head, "head")
    dirty_paths = [line for line in porcelain_status.splitlines() if line.strip()]
    warnings = [line for line in git_warnings.splitlines() if line.strip()]
    revision_match = source_revision == head
    clean = not dirty_paths and not warnings and revision_match and base_is_ancestor
    return {
        "schema_version": SCHEMA,
        "repository": str(repository),
        "source_revision": source_revision,
        "base_revision": base_revision,
        "head": head,
        "revision_match": revision_match,
        "base_is_ancestor": base_is_ancestor,
        "clean": clean,
        "dirty_paths": dirty_paths,
        "git_warnings": warnings,
        "status": "clean_tree_verified" if clean else "clean_tree_failed",
        "pushPerformed": False,
        "prPolicy": "forbidden",
        "non_claims": [
            "read-only local preflight; no push, PR, clone, reset, or cleanup was performed",
            "a clean tree does not prove target build, runtime, or qualification",
        ],
    }


def _git(repository: Path, *args: str) -> tuple[str, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *args],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise CleanTreeError(f"git read-only preflight failed: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise CleanTreeError(
            f"git read-only preflight failed ({completed.returncode}): {detail}"
        )
    return completed.stdout.strip(), completed.stderr.strip()


def _is_ancestor(repository: Path, base_revision: str, head: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "merge-base", "--is-ancestor", base_revision, head],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as error:
        raise CleanTreeError(f"git ancestry preflight failed: {error}") from error
    return completed.returncode == 0


def read_repository_report(
    repository: Path, source_revision: str, base_revision: str
) -> dict[str, Any]:
    """Read the current Git state into the same fail-closed report contract."""

    repository = repository.resolve()
    head, head_warnings = _git(repository, "rev-parse", "HEAD")
    porcelain_status, status_warnings = _git(
        repository, "status", "--porcelain=v1", "--untracked-files=all"
    )
    return build_report(
        repository=repository,
        source_revision=source_revision,
        base_revision=base_revision,
        head=head,
        porcelain_status=porcelain_status,
        base_is_ancestor=_is_ancestor(repository, base_revision, head),
        git_warnings="\n".join(item for item in (head_warnings, status_warnings) if item),
    )


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    if output_dir.exists():
        raise CleanTreeError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    publish_new_file(
        output_dir / "clean-tree.json",
        (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        error_type=CleanTreeError,
    )
    publish_new_file(
        output_dir / "status.json",
        (
            json.dumps(
            {
                "schema_version": STATUS_SCHEMA,
                "success": report["clean"],
                "status": report["status"],
                "source_revision": report["source_revision"],
                "base_revision": report["base_revision"],
                "dirty_paths": report["dirty_paths"],
                "git_warnings": report["git_warnings"],
                "pushPerformed": False,
                "prPolicy": "forbidden",
                "statusLast": True,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
        error_type=CleanTreeError,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--base-revision", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.dry_run:
            raise CleanTreeError("clean-tree preflight is read-only; pass --dry-run")
        repository = args.repository.resolve()
        output_dir = args.output_dir.resolve()
        try:
            output_dir.relative_to(repository)
        except ValueError:
            pass
        else:
            raise CleanTreeError("output directory must be outside the source repository")
        report = read_repository_report(
            repository, args.source_revision, args.base_revision
        )
        write_report(report, output_dir)
    except (OSError, CleanTreeError) as error:
        print(f"P7_CLEAN_TREE_BLOCKED: {error}")
        return 1
    print(f"P7_CLEAN_TREE_{'PASS' if report['clean'] else 'FAIL'} clean={report['clean']}")
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
