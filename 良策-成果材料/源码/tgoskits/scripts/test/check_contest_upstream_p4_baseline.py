#!/usr/bin/env python3
"""Validate the immutable P4 upstream convergence preflight manifest."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "configs/contest/upstream-p4-baseline.json"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _git_bytes(revision: str, path: str) -> bytes:
    completed = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return completed.stdout


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    try:
        manifest = json.loads(
            MANIFEST.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        _require(manifest["schemaVersion"] == 1, "unexpected manifest schema")
        _require(manifest["artifactStatus"] == "preflight-only", "artifact status drift")
        _require(
            manifest["status"] == "p4_upstream_baseline_locked",
            "baseline status drift",
        )
        baseline = manifest["upstreamDevSha"]
        _require(
            _git("merge-base", "HEAD", baseline) == baseline,
            "integration history is not based on the locked upstream baseline",
        )
        for commit in manifest["officialCapabilityCommits"]:
            _require(
                _git("cat-file", "-t", commit) == "commit",
                f"missing official capability commit {commit}",
            )
        for artifact in manifest["officialArtifacts"]:
            content = _git_bytes(baseline, artifact["path"])
            _require(len(content) == artifact["size"], f"size drift: {artifact['path']}")
            _require(
                hashlib.sha256(content).hexdigest() == artifact["sha256"],
                f"hash drift: {artifact['path']}",
            )
        for relative in manifest["migratedHostContracts"]:
            _require((ROOT / relative).is_file(), f"missing migrated host contract {relative}")
        _require(
            not (ROOT / "virtualization/axdevice/src/virtio_net").exists(),
            "legacy contest virtio-net backend was copied into the upstream baseline",
        )
        decision = manifest["productionDecision"]
        _require(
            decision["virtioCore"] == "reuse-official-axvirtio-common-and-axvirtio-net",
            "official VirtIO core is not the sole production decision",
        )
        collision = manifest["resourceCompatibility"]
        _require(
            collision["status"] == "blocked_resource_collision",
            "resource collision was silently reclassified",
        )
        _require(
            collision["officialVirtioNetGuestMmio"]
            == collision["linuxRootBlockGuestMmio"],
            "recorded collision inputs no longer describe the same guest MMIO address",
        )
        non_claims = set(manifest["doesNotProve"])
        _require("Linux plus Zephyr device enumeration" in non_claims, "missing Guest non-claim")
        _require("DMA isolation" in non_claims, "missing DMA non-claim")
    except (AssertionError, KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"P4 upstream baseline preflight failed: {error}", file=sys.stderr)
        return 1
    print("P4_UPSTREAM_BASELINE_PREFLIGHT_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
