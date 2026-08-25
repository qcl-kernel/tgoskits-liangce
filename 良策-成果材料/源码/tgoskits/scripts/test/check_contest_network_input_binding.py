#!/usr/bin/env python3
"""Contract checks for P2 soak provenance -> fresh P4 input binding."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NETWORK = ROOT / "scripts" / "contest" / "network"
CONTEST = ROOT / "scripts" / "contest"
sys.path.insert(0, str(NETWORK))
sys.path.insert(0, str(CONTEST))

import input_binding as binding  # noqa: E402
import validate_dual_guest_soak_session as soak  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: object) -> bytes:
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(data)
    return data


def _claim(path: str, data: bytes) -> dict[str, object]:
    return {"path": path, "size": len(data), "sha256": _sha(data)}


def _session(linux: bytes, zephyr: bytes) -> dict[str, object]:
    nonce = "0123456789abcdef0123456789abcdef"
    identity = {
        "bootId": "dual-binding-contract",
        "qemuPid": 4242,
        "qemuStartMonotonicNs": 100_000,
        "qemuName": f"axvisor-dual-soak-{nonce}",
        "sessionNonce": nonce,
    }
    start = 1_000_000_000
    end = start + soak.MIN_DURATION_NS

    def health(phase: str, monotonic_ns: int) -> dict[str, object]:
        return {
            "phase": phase,
            "monotonicNs": monotonic_ns,
            **identity,
            "marker": soak._health_marker(phase, monotonic_ns, identity),
        }

    return {
        "schemaVersion": 1,
        "artifactStatus": "capture-generated-unreviewed",
        "status": "dual_guest_soak_session_completed",
        "proofScope": "one-identity-bound-qemu-dual-guest-1800-second-coexistence-session",
        "identity": identity,
        "cpuSets": {"linux": [0, 1], "zephyr": [2]},
        "startMonotonicNs": start,
        "endMonotonicNs": end,
        "guests": [
            {
                "vmId": 1,
                "guest": "linux",
                "vmConfig": _claim("linux.resolved.toml", linux),
                "guestDtbMarker": "AXVISOR_GUEST_DTB_READY vm=1 gpa=0x40000000 size=65536 hpa_segments=0x40000000:65536",
                "readyMarker": "AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=dual-binding-contract",
            },
            {
                "vmId": 2,
                "guest": "zephyr",
                "vmConfig": _claim("zephyr.resolved.toml", zephyr),
                "guestDtbMarker": "AXVISOR_GUEST_DTB_READY vm=2 gpa=0x50000000 size=65536 hpa_segments=0x50000000:65536",
                "readyMarker": "AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=dual-binding-contract",
            },
        ],
        "healthMarkers": [health("start", start), health("end", end)],
        "events": [],
    }


def _make_run(*, wrong_live_rootfs: bool = False) -> Path:
    root = (
        ROOT
        / "target"
        / "contract-tests"
        / f"p4-input-binding-{uuid.uuid4().hex}"
    )
    prepared = root / "prepared"
    live = root / "live"
    prepared.mkdir(parents=True)
    live.mkdir()

    linux = b"[base]\nid = 1\ncpu_num = 2\nphys_cpu_ids = [0, 1]\n"
    zephyr = b"[base]\nid = 2\ncpu_num = 1\nphys_cpu_ids = [2]\n"
    qemu = b'args = ["-nographic"]\n'
    current_runtime_rootfs = b"post-run-mutable-rootfs"
    launch_rootfs = b"pre-run-rootfs"
    reusable_source = b"immutable-source-rootfs"
    zephyr_manifest = b"{}\n"
    (prepared / "linux.resolved.toml").write_bytes(linux)
    (prepared / "zephyr.resolved.toml").write_bytes(zephyr)
    (prepared / "qemu.no-dataplane.toml").write_bytes(qemu)
    (prepared / "linux-console-rootfs.ext4").write_bytes(current_runtime_rootfs)
    (prepared / "zephyr-build-manifest.json").write_bytes(zephyr_manifest)

    rootfs_plan = {
        "schemaVersion": 1,
        "artifactStatus": "prepared-rootfs-only",
        "status": "dual_guest_linux_rootfs_prepared",
        "bootId": "dual-binding-contract",
        "sourceRootfs": _claim("rootfs.img", reusable_source),
        "outputRootfs": {
            **_claim("linux-console-rootfs.ext4", launch_rootfs),
            "copyOnly": True,
        },
    }
    rootfs_plan_bytes = _write_json(
        prepared / "linux-rootfs-plan.json", rootfs_plan
    )

    session = _session(linux, zephyr)
    session_bytes = _write_json(root / "dual-guest-soak-session.json", session)
    validated = soak.validate_session(
        session,
        linux_config=prepared / "linux.resolved.toml",
        linux_bytes=linux,
        zephyr_config=prepared / "zephyr.resolved.toml",
        zephyr_bytes=zephyr,
    )
    result = {
        "schemaVersion": 1,
        "artifactStatus": "observation-derived",
        "status": "dual_guest_30min_coexistence_observed",
        "proofScope": "one-identity-bound-qemu-dual-guest-1800-second-coexistence-session",
        "doesNotProve": [],
        "durationNs": validated["durationNs"],
        "guests": validated["guests"],
        "cpuSets": validated["cpuSets"],
        "sessionIdentity": validated["identity"],
        "sources": {
            "session": _claim("dual-guest-soak-session.json", session_bytes),
            "linuxVmConfig": _claim("linux.resolved.toml", linux),
            "zephyrVmConfig": _claim("zephyr.resolved.toml", zephyr),
        },
    }
    _write_json(root / "dual-guest-soak-result.json", result)

    launched_claim = _claim("linux-console-rootfs.ext4", launch_rootfs)
    prepared_inputs = {
        "schemaVersion": 1,
        "artifactStatus": "prepared-inputs-only",
        "status": "dual_guest_smoke_inputs_prepared",
        "proofScope": "one-run-linux-zephyr-dual-guest-smoke-input-bundle",
        "doesNotProve": [],
        "bootId": "dual-binding-contract",
        "sources": {
            "linuxKernel": {"path": "/cache/linux", "sha256": "1" * 64},
            "sourceRootfs": {
                "path": "/cache/rootfs.img",
                "sha256": _sha(reusable_source),
            },
            "zephyrBinary": {"path": "/cache/zephyr.bin", "sha256": "2" * 64},
        },
        "outputs": {
            "linux.resolved.toml": _claim("linux.resolved.toml", linux),
            "zephyr.resolved.toml": _claim("zephyr.resolved.toml", zephyr),
            "qemu.no-dataplane.toml": _claim("qemu.no-dataplane.toml", qemu),
            "linux-console-rootfs.ext4": launched_claim,
            "linux-rootfs-plan.json": _claim(
                "linux-rootfs-plan.json", rootfs_plan_bytes
            ),
            "zephyr-build-manifest.json": _claim(
                "zephyr-build-manifest.json", zephyr_manifest
            ),
        },
        "resolvedZephyr": {"vmId": 2, "cpuNum": 1, "physCpuIds": [2]},
        "init": {"size": 10, "sha256": "3" * 64},
    }
    _write_json(prepared / "dual-inputs.json", prepared_inputs)

    live_rootfs_claim = dict(launched_claim)
    live_rootfs_claim["path"] = "/run/prepared/linux-console-rootfs.ext4"
    if wrong_live_rootfs:
        live_rootfs_claim["sha256"] = "f" * 64
    identity = validated["identity"]
    live_status = {
        "schemaVersion": 1,
        "artifactStatus": "run-complete",
        "status": "dual_guest_short_smoke_completed",
        "success": True,
        "error": None,
        "state": {
            "bootId": identity["bootId"],
            "qemuName": identity["qemuName"],
            "sessionNonce": identity["sessionNonce"],
            "qemuStartMonotonicNs": identity["qemuStartMonotonicNs"],
            "readyIdentity": {"pid": identity["qemuPid"]},
            "inputArtifacts": {
                "qemuConfig": _claim("/run/prepared/qemu.no-dataplane.toml", qemu),
                "linuxVmconfig": _claim("/run/prepared/linux.resolved.toml", linux),
                "zephyrVmconfig": _claim("/run/prepared/zephyr.resolved.toml", zephyr),
                "linuxRootfs": live_rootfs_claim,
            },
            "resolvedVmConfigs": {
                "linux": {"vmId": 1, "cpuNum": 2, "physCpuIds": [0, 1]},
                "zephyr": {"vmId": 2, "cpuNum": 1, "physCpuIds": [2]},
            },
            "stabilityWindow": {
                "startMonotonicNs": 1_000_000_000,
                "endMonotonicNs": 1_000_000_000 + soak.MIN_DURATION_NS,
                "durationSeconds": 1800.0,
            },
            "dualGuestReady": {"bootId": identity["bootId"]},
        },
    }
    _write_json(live / "status.json", live_status)
    return root


def _mutate_json(path: Path, mutate) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    _write_json(path, value)


def _expect_failure(callable_, needle: str) -> None:
    try:
        callable_()
    except binding.SoakBindingError as error:
        assert needle in str(error), (needle, str(error))
        return
    raise AssertionError(f"expected SoakBindingError containing {needle!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--soak-run", type=Path)
    args = parser.parse_args(argv)

    good = binding.validate_soak_binding(_make_run())
    assert good["status"] == "p2_soak_input_bound"
    assert good["durationNs"] == soak.MIN_DURATION_NS
    assert good["historicalRuntimeRootfs"]["currentMatchesLaunchClaim"] is False
    assert good["historicalRuntimeRootfs"]["reusable"] is False
    assert good["reusableLinuxSourceRootfs"]["sha256"] == _sha(
        b"immutable-source-rootfs"
    )

    # When the historical r31 evidence checkout is present, prove that the
    # repository's frozen provenance metadata was generated by this validator,
    # rather than trusting a copied binding token.
    r31 = (
        ROOT.parent
        / "tgoskits"
        / "results"
        / "baseline"
        / "runs"
        / "phase2-dual-soak-20260814T072549Z-15df1f65d-dirty-r31"
    )
    canonical = ROOT / "configs" / "contest" / "network" / "p2-r31-soak-provenance.json"
    if r31.is_dir() and canonical.is_file():
        expected = json.loads(canonical.read_text(encoding="utf-8"))
        assert binding.validate_soak_binding(r31) == expected
        print("  [PASS] frozen r31 provenance equals input_binding output")

    _expect_failure(
        lambda: binding.validate_soak_binding(
            _make_run(wrong_live_rootfs=True)
        ),
        "live Linux rootfs claim disagrees",
    )
    invalid_claim = _make_run()
    _mutate_json(
        invalid_claim / "dual-guest-soak-result.json",
        lambda value: value["sources"]["session"].update({"size": True}),
    )
    _expect_failure(
        lambda: binding.validate_soak_binding(invalid_claim),
        "not a valid byte claim",
    )
    qemu_dir = (
        ROOT
        / "target"
        / "contract-tests"
        / f"p4-qemu-binding-{uuid.uuid4().hex}"
    )
    qemu_dir.mkdir(parents=True)
    good_qemu = qemu_dir / "good.toml"
    good_qemu.write_text('args = ["-nographic", "-nic", "none"]\n', encoding="utf-8")
    binding.validate_current_qemu_config(good_qemu)
    bad_qemu = qemu_dir / "bad.toml"
    bad_qemu.write_text('args = ["-nic", "user"]\n', encoding="utf-8")
    _expect_failure(
        lambda: binding.validate_current_qemu_config(bad_qemu),
        "must use only '-nic none'",
    )

    if args.soak_run is not None:
        actual = binding.validate_soak_binding(args.soak_run)
        print(
            "  [PASS] actual soak "
            f"run={actual['sourceRun']} durationNs={actual['durationNs']} "
            f"binding={actual['bindingSha256']}"
        )
    print("CONTEST_NETWORK_INPUT_BINDING_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
