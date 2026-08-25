#!/usr/bin/env python3
"""Fail-closed host contract for the current upstream convergence point.

The compatibility filename is retained because c82 introduced the configured
VirtIO-net architecture.  The manifest locks the historical f964 convergence
point and verifies every commit, artifact, and path in the c82..f964 delta.
The current tree may legitimately advance beyond f964, so compatibility is
checked through ancestry plus the configured-device, typed-FDT and edge/pulse
structural contracts rather than byte-for-byte equality with every historical
delta path.  It deliberately does not
claim a Rust target build, Guest enumeration, Guest-IP, AI-loop, real-time
improvement, or DMA isolation.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "configs" / "contest" / "upstream-p4-c82-baseline.json"
TARGET = (
    ROOT
    / "virtualization"
    / "axvm"
    / "src"
    / "configured"
    / "devices"
    / "virtio_net.rs"
)
DEVICE_MODULE = TARGET.parent / "mod.rs"
FDT_RESOLVER = ROOT / "virtualization" / "axvm" / "src" / "boot" / "fdt" / "device.rs"
FDT_CREATE = (
    ROOT
    / "virtualization"
    / "axvm"
    / "src"
    / "boot"
    / "fdt"
    / "core"
    / "create.rs"
)
LEGACY_PATHS = (
    ROOT / "os" / "axvisor" / "src" / "virtio_net.rs",
    ROOT / "virtualization" / "axdevice" / "src" / "virtio_net",
)


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
        _require(
            manifest["artifactStatus"] == "current-integration-reference",
            "artifact status drift",
        )
        _require(
            manifest["status"] == "p4_f964_convergence_locked",
            "convergence status drift",
        )
        previous = manifest["previousUpstreamDevSha"]
        baseline = manifest["upstreamDevSha"]
        _require(_git("cat-file", "-t", previous) == "commit", "missing c82 anchor")
        _require(_git("cat-file", "-t", baseline) == "commit", "missing f964 commit")
        _require(
            _git("merge-base", "HEAD", baseline) == baseline,
            "current history does not contain the locked f964 official baseline",
        )
        commits = _git("rev-list", "--reverse", f"{previous}..{baseline}").splitlines()
        _require(
            commits == manifest["latestDeltaCommits"],
            "c82..f964 official commit sequence drift",
        )
        _require(
            len(commits) == manifest["latestDeltaCommitCount"],
            "c82..f964 official commit count drift",
        )
        delta_paths = _git("diff", "--name-only", previous, baseline).splitlines()
        _require(
            len(delta_paths) == manifest["latestDeltaPathCount"],
            "c82..f964 official path count drift",
        )
        for artifact in manifest["officialArtifacts"]:
            content = _git_bytes(baseline, artifact["path"])
            _require(len(content) == artifact["size"], f"size drift: {artifact['path']}")
            _require(
                hashlib.sha256(content).hexdigest() == artifact["sha256"],
                f"hash drift: {artifact['path']}",
            )

        _require(TARGET.is_file(), "missing configured virtio-net backend")
        for path in LEGACY_PATHS:
            _require(not path.exists(), f"legacy second backend remains: {path.relative_to(ROOT)}")

        source = TARGET.read_text(encoding="utf-8")
        _require(
            source.count("pub const REGISTRATION: ConfiguredModelRegistration") == 1,
            "virtio-net registration count is not exactly one",
        )
        _require('model: "virtio-net"' in source, "official model name is missing")
        _require(
            source.count("ResourceRequest::Auto") >= 2,
            "MMIO and IRQ are not both Auto-planned",
        )
        _require(
            source.count("InterruptTrigger::EdgeTriggered") >= 2,
            "official edge-triggered binding/resource contract is incomplete",
        )
        _require(
            "InterruptTrigger::LevelTriggered" not in source,
            "unverified level-triggered rollback was reintroduced",
        )
        _require(not re.search(r"self\.irq\s*\.assert\(", source), "level assert reintroduced")
        _require(not re.search(r"self\.irq\s*\.deassert\(", source), "level deassert reintroduced")
        _require(
            len(re.findall(r"self\.irq\s*\.pulse\(", source)) >= 2,
            "RX and MMIO pending paths do not both retain edge pulses",
        )
        _require(
            "FdtContributionSpec::Conventional" in source
            and 'FdtNodeSpec::new("virtio_mmio")' in source,
            "virtio-net firmware is not expressed through typed FDT specs",
        )
        for hook in (
            "EgressOutcome::Dropped",
            "SwitchPortId::new(self.vm_id, next_port_generation(self.vm_id), 0)",
            "impl Drop for VirtioNetRuntimeDevice",
            "self.endpoint.deactivate()",
            "frame.get(..12)",
            "MAX_CONTEST_FRAME_SIZE: usize = 1514",
            "virtio-net frame vm=",
            "rx_delivered.fetch_add(1, Ordering::Relaxed)",
            "rx_no_guest_buffer",
            "rx_errors",
        ):
            _require(hook in source, f"missing contest-only evidence hook: {hook}")
        _require(
            source.index("let outcome = self.switch.switch_from_port")
            < source.index('"virtio-net frame vm={} generation={} port={}'),
            "exact frame evidence is emitted before switch validation",
        )

        module = DEVICE_MODULE.read_text(encoding="utf-8")
        _require(module.count("mod virtio_net;") == 1, "virtio-net module count drift")
        _require(
            module.count("virtio_net::register(catalog)") == 1,
            "virtio-net catalog registration count drift",
        )

        resolver = FDT_RESOLVER.read_text(encoding="utf-8")
        creator = FDT_CREATE.read_text(encoding="utf-8")
        _require(
            "struct ResolvedFdtDevice" in resolver
            and "resolve_fdt_firmware" in resolver,
            "typed FDT resolver missing",
        )
        _require(
            "devices: &[ResolvedFdtDevice]" in creator,
            "FDT creation is not consuming typed resolved devices",
        )

        decision = manifest["productionDecision"]
        _require(
            decision["deviceOwner"]
            == "virtualization/axvm/src/configured/devices/virtio_net.rs",
            "manifest device owner drift",
        )
        _require(
            decision["interrupt"] == "official-edge-triggered-pulse-baseline",
            "manifest IRQ decision drift",
        )
        non_claims = set(manifest["doesNotProve"])
        _require(
            "Linux plus Zephyr device enumeration on f964" in non_claims,
            "missing Guest runtime non-claim",
        )
        _require("DMA isolation" in non_claims, "missing DMA isolation non-claim")
    except (AssertionError, KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"P4 f964 convergence preflight failed: {error}", file=sys.stderr)
        return 1

    print("P4_F964_CONVERGENCE_PREFLIGHT_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
