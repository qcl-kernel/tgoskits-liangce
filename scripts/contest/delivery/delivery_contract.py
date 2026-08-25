#!/usr/bin/env python3
"""Local-only P7 package scan, hash, and no-push contract."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "p7-delivery-manifest-v1"
STATUS_SCHEMA = "p7-delivery-status-v1"
REVIEW_PACKAGE_FILES = {
    "review-manifest.json",
    "checksums.sha256",
    "status.json",
}
SHA = re.compile(r"[0-9a-f]{40}\Z")

FORBIDDEN_DIRS = {
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "target",
    "results",
    "node_modules",
}
SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.json",
}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".secret")
FORBIDDEN_SUFFIXES = (".pyc", ".o", ".a", ".so", ".img", ".qcow2", ".iso", ".pcap")


class DeliveryError(ValueError):
    """Raised when a source tree cannot form a safe local delivery package."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the stable bytes used for delivery manifest self-hashes."""

    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def manifest_digest(manifest: dict[str, Any]) -> str:
    """Hash a manifest after removing its non-self-referential digest field."""

    unsigned = dict(manifest)
    unsigned.pop("manifestSha256", None)
    return hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()


def validate_manifest_digest(manifest: dict[str, Any]) -> str:
    expected = manifest.get("manifestSha256")
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise DeliveryError("manifestSha256 must be a lowercase SHA-256")
    actual = manifest_digest(manifest)
    if actual != expected:
        raise DeliveryError("review manifest self-hash mismatch")
    return actual


def validate_review_bundle(path: Path) -> dict[str, Any]:
    """Independently consume the local-only P7 review preflight package."""

    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_dir():
        raise DeliveryError(f"review package must be a regular directory: {path}")
    root = candidate.resolve()
    actual_files: set[str] = set()
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise DeliveryError(f"review package contains a symlink: {entry}")
        relative = entry.relative_to(root).as_posix()
        if not entry.is_file():
            raise DeliveryError(f"review package contains a non-file entry: {relative}")
        actual_files.add(relative)
    if actual_files != REVIEW_PACKAGE_FILES:
        missing = sorted(REVIEW_PACKAGE_FILES - actual_files)
        extra = sorted(actual_files - REVIEW_PACKAGE_FILES)
        raise DeliveryError(f"review package file set mismatch: missing={missing}, extra={extra}")

    try:
        manifest = json.loads(
            (root / "review-manifest.json").read_text(encoding="utf-8")
        )
        status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeliveryError(f"review package JSON is invalid: {error}") from error
    if not isinstance(manifest, dict) or not isinstance(status, dict):
        raise DeliveryError("review package JSON roots must be objects")
    expected_manifest_keys = {
        "schema_version",
        "repository",
        "base_revision",
        "source_revision",
        "files",
        "excluded",
        "errors",
        "review_metadata",
        "prPolicy",
        "pushPerformed",
        "status",
        "source_repository",
        "head",
        "base_is_ancestor",
        "clean_tree_status",
        "manifestSha256",
    }
    if set(manifest) != expected_manifest_keys:
        raise DeliveryError("review manifest fields drifted")
    validate_manifest_digest(manifest)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise DeliveryError("review manifest schema drifted")
    _git_revision(str(manifest.get("source_revision")), "source_revision")
    _git_revision(str(manifest.get("base_revision")), "base_revision")
    if manifest.get("prPolicy") != "forbidden" or manifest.get("pushPerformed") is not False:
        raise DeliveryError("review manifest remote-write policy drifted")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise DeliveryError("review manifest files must be non-empty")
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise DeliveryError(f"review manifest files[{index}] shape is invalid")
        relative = record.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or relative in seen
            or any(part in {"", ".", ".."} for part in Path(relative).parts)
        ):
            raise DeliveryError(f"review manifest files[{index}].path is invalid")
        if not isinstance(record.get("size"), int) or record["size"] < 0:
            raise DeliveryError(f"review manifest files[{index}].size is invalid")
        if not isinstance(record.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None:
            raise DeliveryError(f"review manifest files[{index}].sha256 is invalid")
        seen.add(relative)
    checksum_lines = "".join(
        f"{record['sha256']}  {record['path']}\n" for record in records
    )
    if (root / "checksums.sha256").read_text(encoding="utf-8") != checksum_lines:
        raise DeliveryError("checksums.sha256 does not match review manifest")

    expected_status_keys = {
        "schema_version",
        "success",
        "status",
        "repository",
        "source_revision",
        "base_revision",
        "source_repository",
        "head",
        "base_is_ancestor",
        "clean_tree_status",
        "review_metadata",
        "finalDeliveryReady",
        "manifestSha256",
        "pushPerformed",
        "prPolicy",
        "statusLast",
    }
    if set(status) != expected_status_keys:
        raise DeliveryError("review status fields drifted")
    for field in (
        "repository",
        "source_revision",
        "base_revision",
        "source_repository",
        "head",
        "base_is_ancestor",
        "clean_tree_status",
        "review_metadata",
    ):
        if status.get(field) != manifest.get(field):
            raise DeliveryError(f"review status {field} is not manifest-bound")
    if (
        status.get("schema_version") != STATUS_SCHEMA
        or status.get("success") is not (not bool(manifest["errors"]))
        or status.get("status") != manifest.get("status")
        or status.get("finalDeliveryReady") is not False
        or status.get("manifestSha256") != manifest.get("manifestSha256")
        or status.get("pushPerformed") is not False
        or status.get("prPolicy") != "forbidden"
        or status.get("statusLast") is not True
    ):
        raise DeliveryError("review status is not fail-closed or manifest-bound")
    return {
        "schema_version": "p7-delivery-validation-v1",
        "repository": manifest["repository"],
        "source_revision": manifest["source_revision"],
        "status": status["status"],
        "success": status["success"],
        "manifest_sha256": status["manifestSha256"],
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision(value: str, field: str) -> str:
    if SHA.fullmatch(value) is None:
        raise DeliveryError(f"{field} must be a 40-hex revision")
    return value


def _reason(relative: Path) -> str | None:
    parts = relative.parts
    lowered = relative.name.lower()
    if ".git" in parts:
        if parts == (".git",) or parts[0] == ".git":
            return "top-level repository metadata excluded"
        return "nested .git is forbidden"
    if any(part.lower() in FORBIDDEN_DIRS for part in parts[:-1]):
        return "forbidden build/evidence directory"
    if lowered in SECRET_NAMES or lowered.startswith("secret") or lowered.startswith("token"):
        return "secret-like file excluded"
    if lowered.endswith(SECRET_SUFFIXES):
        return "secret-like suffix excluded"
    if lowered.endswith(FORBIDDEN_SUFFIXES) or fnmatch.fnmatch(lowered, "*.log"):
        return "build/raw-evidence file excluded"
    return None


def collect_files(source_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    source_root = Path(source_root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise DeliveryError(f"source root must be a regular directory: {source_root}")
    root = source_root.resolve()
    files: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    errors: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            errors.append(f"symlink is forbidden: {relative.as_posix()}")
            continue
        if path.is_dir():
            reason = _reason(relative)
            if reason:
                excluded.append({"path": relative.as_posix(), "reason": reason})
                if reason == "nested .git is forbidden":
                    errors.append(f"{relative.as_posix()}: {reason}")
            continue
        if not path.is_file():
            errors.append(f"unsupported filesystem entry: {relative.as_posix()}")
            continue
        reason = _reason(relative)
        if reason:
            excluded.append({"path": relative.as_posix(), "reason": reason})
            if reason != "top-level repository metadata excluded":
                errors.append(f"{relative.as_posix()}: {reason}")
            continue
        files.append(
            {
                "path": relative.as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if not files:
        errors.append("source root contains no deliverable files")
    return files, excluded, errors


def build_preflight(
    source_root: Path,
    *,
    repository: str,
    source_revision: str,
    base_revision: str,
) -> dict[str, Any]:
    source_revision = _git_revision(source_revision, "source_revision")
    base_revision = _git_revision(base_revision, "base_revision")
    files, excluded, errors = collect_files(source_root)
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "base_revision": base_revision,
        "source_revision": source_revision,
        "files": files,
        "excluded": excluded,
        "errors": errors,
        "review_metadata": {
            "commit_series": {"status": "not_generated", "items": []},
            "patches": {"status": "not_generated", "items": []},
            "apply_transcript": {"status": "not_generated", "sha256": None},
            "reviewer": {"status": "not_assigned", "name": None},
            "final_delivery_ready": False,
            "non_claims": [
                "no clean-clone git am transcript was produced",
                "no reviewer approval or remote ref was verified",
            ],
        },
        "prPolicy": "forbidden",
        "pushPerformed": False,
        "status": "delivery_preflight_ready" if not errors else "delivery_preflight_failed",
    }
