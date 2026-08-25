#!/usr/bin/env python3
"""P4-EVID-01B contract: Guest-runtime v2 qualification, fail-closed.

Builds synthetic immutable evidence bundles and asserts:
  - a complete TEST-011 qualification bundle yields `guest_network_qualified`;
  - every deficiency (empty artifacts, wrong identity/DTB/READY, loss on the
    wire, tampered frames, status not-last/not-qualified, residual processes,
    missing pcap/manifest mismatch) raises QualificationError.

This is deterministic host Python; it never launches QEMU.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NET_DIR = ROOT / "scripts" / "contest" / "network"
sys.path.insert(0, str(NET_DIR))

import qualify_network_session as q  # noqa: E402
import input_binding as binding  # noqa: E402

FRAME_N = 400
READY_LINUX = "AXVISOR_DUAL_GUEST_LINUX_READY"
READY_ZEPHYR = "AXVISOR_DUAL_GUEST_ZEPHYR_READY"
PROFILE = ROOT / "configs" / "contest" / "network" / "test-011-v2.json"
CANONICAL_PROVENANCE = ROOT / "configs" / "contest" / "network" / "p2-r31-soak-provenance.json"
SYNTHETIC_SOURCE = ROOT / "target" / "contract-tests" / "canonical-p2-r31-source"
ACTUAL_R31_SOURCE = (
    ROOT.parent
    / "tgoskits"
    / "results"
    / "baseline"
    / "runs"
    / "phase2-dual-soak-20260814T072549Z-15df1f65d-dirty-r31"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _soak_provenance() -> dict:
    return json.loads(CANONICAL_PROVENANCE.read_text(encoding="utf-8"))


def _source_soak() -> Path:
    return SYNTHETIC_SOURCE


def _canonical_source_check(provenance: dict, _source: Path) -> dict:
    """Test-only source validator; byte revalidation is covered separately."""

    expected = json.loads(CANONICAL_PROVENANCE.read_text(encoding="utf-8"))
    if provenance != expected:
        raise binding.SoakBindingError("test source does not match canonical provenance")
    return expected


def _qualify(root: Path, profile: Path, source_soak: Path) -> dict:
    # The qualification contract test uses the checked-in canonical metadata so
    # it runs from a clean clone.  check_contest_network_input_binding.py adds
    # the real-r31 byte revalidation when that historical checkout is present.
    with mock.patch.object(
        q, "validate_provenance_against_source", side_effect=_canonical_source_check
    ):
        return q.qualify_guest_session(root, profile, source_soak=source_soak)


def make_bundle() -> tuple[Path, str]:
    run_id = f"phase4-net-qualify-{secrets.token_hex(8)}"
    root = ROOT / "target" / "contract-tests" / f"p4-evid-{secrets.token_hex(4)}" / run_id
    for sub in ("configs", "logs", "dtb", "network", "metrics"):
        (root / sub).mkdir(parents=True)

    profile_data = json.loads(PROFILE.read_text(encoding="utf-8"))
    (root / "configs" / "profile.json").write_text(
        json.dumps(profile_data, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    (root / "configs" / "soak-provenance.json").write_text(
        json.dumps(_soak_provenance(), indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # logs with dual READY + completion
    (root / "logs" / "axvisor.raw.log").write_text(
        f"{READY_LINUX} vm=1 boot_id={run_id}\n"
        f"{READY_ZEPHYR} vm=2 boot_id={run_id}\n",
        encoding="utf-8",
    )
    (root / "logs" / "linux.raw.log").write_text(
        f"{READY_LINUX} vm=1 boot_id={run_id}\n"
        "TGOS_LINUX_L3_SMOKE sent=100 received=100 loss=0\n",
        encoding="utf-8",
    )
    (root / "logs" / "zephyr.raw.log").write_text(
        f"{READY_ZEPHYR} vm=2 boot_id={run_id}\n", encoding="utf-8"
    )

    # dtb
    for stem in ("linux-final", "zephyr-final"):
        (root / "dtb" / f"{stem}.dtb").write_bytes(b"\xd0\x0d\xfe\xed" * 8)
        (root / "dtb" / f"{stem}.dts").write_text(f"/dts-v1/; / {{}}; /* {stem} */\n", encoding="utf-8")

    # frames.jsonl
    frame_lines = []
    for i in range(FRAME_N):
        payload = b"E" * (14 + (i % 30))
        bytez = bytes(1) * 0  # placeholder; build below
        frame = b"\xff\xff\xff\xff\xff\xff" + b"\x02\x00\x00\x00\x00\x01" + b"\x08\x00" + payload[12:]
        frame = frame[:14 + (i % 30)]
        digest = hashlib.sha256(frame).hexdigest()
        row = {
            "schema_version": "p4-network-frame-v1",
            "sequence": i,
            "run_id": run_id,
            "session_id": run_id,
            "ingress_port": i % 2,
            "generation": 1,
            "direction": "linux2zephyr" if i % 2 == 0 else "zephyr2linux",
            "monotonic_ns": 1_000_000_000 + i * 1_000_000,
            "length": len(frame),
            "sha256": digest,
            "frame_hex": frame.hex(),
        }
        frame_lines.append(row)
    (root / "network" / "frames.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in frame_lines) + "\n",
        encoding="utf-8",
    )

    # pcap (valid global header only; validator checks magic + presence)
    import struct
    pcap = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    (root / "network" / "capture.pcap").write_bytes(pcap)

    # counters
    counters = {
        "schema_version": "p4-network-counters-v1",
        "capture_frames": FRAME_N,
        "capture_bytes": FRAME_N * 44,
        "capture_drops": 0,
        "switch_enqueue": FRAME_N,
        "switch_deliver": FRAME_N,
        "switch_drop": 0,
        "ingress_full_drop": 0,
        "ingress_inactive_drop": 0,
        "ingress_counter_observations": FRAME_N,
        "ingress_counter_endpoints_finalized": 0,
        "fault_drop": 0,
        "fault_duplicate": 0,
        "fault_reorder": 0,
        "fault_corrupt": 0,
    }
    (root / "network" / "counters.json").write_text(json.dumps(counters, indent=1), encoding="utf-8")

    # metrics
    (root / "metrics" / "raw.jsonl").write_text(
        json.dumps({"t": 0, "rtt_us": 1000}) + "\n" + json.dumps({"t": 1, "rtt_us": 1001}) + "\n",
        encoding="utf-8",
    )
    (root / "metrics" / "summary.json").write_text(
        json.dumps({"scenario": "test-011", "rtt_mean_us": 1000, "rtt_p99_us": 1100}, indent=1),
        encoding="utf-8",
    )

    session = {
        "schema_version": "p4-network-session-v2",
        "run_id": run_id,
        "session_id": run_id,
        "nonce": "a1b2c3d4e5f60708",
        "test_id": "TEST-011",
        "profile_id": "test-011-v2",
        "scenario": "l3-smoke-qualification",
        "host_only": False,
        "evidence_level": "L7 Guest-IP",
        "transport": "icmp",
        "clock_domain": "monotonic_ns",
        "network": profile_data["network"],
        "endpoints": profile_data["endpoints"],
        "execution": {"host_only": False, "qemu": True, "wsl": True, "guest_runtime": True},
        "identity": {"producer": "p4-network-guest", "run_id": run_id, "session_id": run_id, "nonce": "a1b2c3d4e5f60708"},
        "manifest_sha256": "0" * 64,
    }
    (root / "session.json").write_text(json.dumps(session, indent=1, sort_keys=True), encoding="utf-8")

    # manifest (hashes content artifacts only)
    artifacts = [
        "configs/profile.json",
        "configs/soak-provenance.json",
        "logs/axvisor.raw.log",
        "logs/linux.raw.log",
        "logs/zephyr.raw.log",
        "dtb/linux-final.dtb",
        "dtb/linux-final.dts",
        "dtb/zephyr-final.dtb",
        "dtb/zephyr-final.dts",
        "network/frames.jsonl",
        "network/capture.pcap",
        "network/counters.json",
        "metrics/raw.jsonl",
        "metrics/summary.json",
    ]
    manifest = {
        "schema_version": "p4-network-manifest-v1",
        "run_id": run_id,
        "files": {rel: _sha(root / rel) for rel in artifacts},
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    session_manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    # rewrite session with real manifest_sha256 (manifest hashes no session.json)
    session["manifest_sha256"] = session_manifest_sha
    (root / "session.json").write_text(json.dumps(session, indent=1, sort_keys=True), encoding="utf-8")

    # cleanup
    (root / "cleanup.json").write_text(
        json.dumps({"residualProcesses": [], "residualSockets": []}, indent=1),
        encoding="utf-8",
    )

    # status - written last
    status = {
        "schema_version": "p4-network-status-v2",
        "success": True,
        "status": "guest_network_qualified",
        "qualified": True,
        "primaryError": None,
        "cleanupError": None,
        "completedChecks": ["oracle", "hashes", "validator", "cleanup"],
        "manifestSha256": session_manifest_sha,
        "statusLast": True,
    }
    (root / "status.json").write_text(json.dumps(status, indent=1, sort_keys=True), encoding="utf-8")
    return root, session_manifest_sha


def _expect_pass(root: Path, profile: Path, source_soak: Path) -> None:
    report = _qualify(root, profile, source_soak)
    if report["status"] != "guest_network_qualified" or not report["qualified"]:
        raise AssertionError(f"expected qualified, got {report}")
    print(f"  [PASS] qualified: run={report['run_id']} frames={report['capture_frames']}")


def _expect_fail(root: Path, profile: Path, label: str, source_soak: Path) -> None:
    try:
        _qualify(root, profile, source_soak)
    except q.QualificationError as error:
        print(f"  [FAIL-CLOSED] {label}: {error}")
        return
    raise AssertionError(f"expected qualification failure for {label}")


def main() -> int:
    profile = PROFILE
    source_soak = _source_soak()

    root, sha = make_bundle()
    _expect_pass(root, profile, source_soak)
    if ACTUAL_R31_SOURCE.is_dir():
        actual_report = q.qualify_guest_session(
            root, profile, source_soak=ACTUAL_R31_SOURCE
        )
        if not actual_report["qualified"]:
            raise AssertionError("actual r31 source did not pass qualification revalidation")
        print("  [PASS] actual r31 source revalidation")
    try:
        q.qualify_guest_session(root, profile)
    except TypeError as error:
        if "source_soak" not in str(error):
            raise AssertionError(f"missing-source failure was not explicit: {error}")
        print("  [FAIL-CLOSED] missing P2 source is rejected")
    else:
        raise AssertionError("qualification unexpectedly accepted a missing P2 source")

    cases: list[tuple[str, None | Path]] = []

    # 1. empty network/frames.jsonl
    r, _ = make_bundle()
    (r / "network" / "frames.jsonl").write_text("", encoding="utf-8")
    cases.append(("empty network/frames.jsonl", r))

    # 2. empty metrics/raw.jsonl
    r, _ = make_bundle()
    (r / "metrics" / "raw.jsonl").write_text("", encoding="utf-8")
    cases.append(("empty metrics/raw.jsonl", r))

    # 3. wrong nonce identity
    r, _ = make_bundle()
    doc = json.loads((r / "session.json").read_text())
    # Only the identity claim disagrees with the session-level nonce.
    doc["identity"]["nonce"] = "deadbeefdeadbeef"
    (r / "session.json").write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    cases.append(("wrong nonce identity", r))

    # 4. empty zephyr DTB (no delete: every fixture remains inspectable)
    r, _ = make_bundle()
    (r / "dtb" / "zephyr-final.dtb").write_bytes(b"")
    cases.append(("empty zephyr-final.dtb", r))

    # 5. missing one READY
    r, _ = make_bundle()
    (r / "logs" / "zephyr.raw.log").write_text("no ready marker\n", encoding="utf-8")
    cases.append(("missing zephyr READY", r))

    # 6. wire loss (switch_drop>0)
    r, _ = make_bundle()
    c = json.loads((r / "network" / "counters.json").read_text())
    c["switch_drop"] = 1
    (r / "network" / "counters.json").write_text(json.dumps(c, indent=1), encoding="utf-8")
    cases.append(("switch_drop>0 on TEST-011", r))

    # 7. status not-last
    r, _ = make_bundle()
    st = json.loads((r / "status.json").read_text())
    st["statusLast"] = False
    (r / "status.json").write_text(json.dumps(st, indent=1, sort_keys=True), encoding="utf-8")
    cases.append(("status not-last", r))

    # 8. status not qualified (smoke token)
    r, _ = make_bundle()
    st = json.loads((r / "status.json").read_text())
    st["status"] = "guest_network_smoke_completed"
    st["qualified"] = False
    (r / "status.json").write_text(json.dumps(st, indent=1, sort_keys=True), encoding="utf-8")
    cases.append(("status smoke token instead of qualified", r))

    # 9. residual process
    r, _ = make_bundle()
    (r / "cleanup.json").write_text(
        json.dumps({"residualProcesses": ["qemu-system-aarch64"], "residualSockets": []}, indent=1),
        encoding="utf-8",
    )
    cases.append(("residual process in cleanup", r))

    # 10. empty pcap (no delete: every fixture remains inspectable)
    r, _ = make_bundle()
    (r / "network" / "capture.pcap").write_bytes(b"")
    cases.append(("empty capture.pcap", r))

    # 11. manifest hash mismatch (tamper counters after manifest)
    r, _ = make_bundle()
    c = json.loads((r / "network" / "counters.json").read_text())
    c["capture_frames"] = 399
    (r / "network" / "counters.json").write_text(json.dumps(c, indent=1), encoding="utf-8")
    cases.append(("tampered counters vs manifest hash", r))

    # 12. tampered frame (sha mismatch)
    r, _ = make_bundle()
    lines = (r / "network" / "frames.jsonl").read_text().splitlines()
    row = json.loads(lines[0])
    row["frame_hex"] = (b"\x11" * len(bytes.fromhex(row["frame_hex"]))).hex()
    lines[0] = json.dumps(row, sort_keys=True)
    (r / "network" / "frames.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cases.append(("tampered frame sha mismatch", r))

    # 13. bounded ingress saturation is independently disallowed.
    r, _ = make_bundle()
    c = json.loads((r / "network" / "counters.json").read_text())
    c["ingress_full_drop"] = 1
    (r / "network" / "counters.json").write_text(json.dumps(c, indent=1), encoding="utf-8")
    cases.append(("ingress full-drop exceeds profile maximum", r))

    # 14. zero drop cannot be inferred when no runtime counter snapshot exists.
    r, _ = make_bundle()
    c = json.loads((r / "network" / "counters.json").read_text())
    c["ingress_counter_observations"] = 0
    (r / "network" / "counters.json").write_text(json.dumps(c, indent=1), encoding="utf-8")
    cases.append(("missing ingress counter observation", r))

    # 15. The manifest-bound source provenance cannot be replaced by a token.
    r, _ = make_bundle()
    provenance = json.loads(
        (r / "configs" / "soak-provenance.json").read_text(encoding="utf-8")
    )
    provenance["bindingSha256"] = "0" * 64
    (r / "configs" / "soak-provenance.json").write_text(
        json.dumps(provenance, indent=1, sort_keys=True), encoding="utf-8"
    )
    cases.append(("wrong P2 soak binding", r))

    # 16b. A self-consistent but forged source identity must still disagree
    # with the freshly validated P2 source.
    r, _ = make_bundle()
    provenance = json.loads(
        (r / "configs" / "soak-provenance.json").read_text(encoding="utf-8")
    )
    provenance["sourceIdentity"]["bootId"] = "forged-p2-identity"
    provenance["sourceIdentity"]["qemuName"] = (
        f"axvisor-dual-soak-{provenance['sourceIdentity']['sessionNonce']}"
    )
    provenance["bindingSha256"] = binding.recompute_binding_sha256(provenance)
    (r / "configs" / "soak-provenance.json").write_text(
        json.dumps(provenance, indent=1, sort_keys=True), encoding="utf-8"
    )
    cases.append(("forged P2 identity with recomputed binding", r))

    # 16c. CPU ownership and byte claims are also source-bound, even if an
    # attacker recomputes the digest after changing them.
    for mutate, label in (
        (
            lambda value: value["cpuSets"].update({"linux": [0, 3]}),
            "forged P2 CPU ownership with recomputed binding",
        ),
        (
            lambda value: value["artifacts"]["result"].update({"sha256": "f" * 64}),
            "forged P2 artifact claim with recomputed binding",
        ),
    ):
        r, _ = make_bundle()
        provenance = json.loads(
            (r / "configs" / "soak-provenance.json").read_text(encoding="utf-8")
        )
        mutate(provenance)
        provenance["bindingSha256"] = binding.recompute_binding_sha256(provenance)
        (r / "configs" / "soak-provenance.json").write_text(
            json.dumps(provenance, indent=1, sort_keys=True), encoding="utf-8"
        )
        cases.append((label, r))

    # 16. READY identity in per-Guest logs cannot be absent from the merged log.
    r, _ = make_bundle()
    (r / "logs" / "axvisor.raw.log").write_text(
        "AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=wrong-run\n"
        "AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=wrong-run\n",
        encoding="utf-8",
    )
    cases.append(("wrong READY identity in AxVisor log", r))

    # 16d. Qualified bundles have an exact root file/directory boundary.
    r, _ = make_bundle()
    (r / "unexpected.txt").write_text("unindexed\n", encoding="utf-8")
    cases.append(("unindexed qualification file", r))
    r, _ = make_bundle()
    (r / "unexpected").mkdir()
    cases.append(("unindexed qualification directory", r))

    for label, run_root in cases:
        _expect_fail(run_root, profile, label, source_soak)

    # 17. A same-id profile cannot weaken qualification by deleting a mandatory
    # ingress counter threshold.
    r, _ = make_bundle()
    weakened = json.loads(PROFILE.read_text(encoding="utf-8"))
    del weakened["oracle"]["structuredScenarioOracle"][
        "minimum_ingress_counter_observations"
    ]
    weakened_path = r / "weakened-profile.json"
    weakened_path.write_text(
        json.dumps(weakened, indent=1, sort_keys=True), encoding="utf-8"
    )
    (r / "configs" / "profile.json").write_text(
        json.dumps(weakened, indent=1, sort_keys=True), encoding="utf-8"
    )
    _expect_fail(r, weakened_path, "profile deleted mandatory ingress oracle", source_soak)

    # 18. A v2 smoke bundle cannot borrow another scenario's fault identity.
    r, _ = make_bundle()
    wrong_fault = json.loads(PROFILE.read_text(encoding="utf-8"))
    wrong_fault["fault"]["profile_id"] = "p4-test-012-disabled"
    wrong_fault_path = r / "wrong-fault-profile.json"
    wrong_fault_path.write_text(
        json.dumps(wrong_fault, indent=1, sort_keys=True), encoding="utf-8"
    )
    (r / "configs" / "profile.json").write_text(
        json.dumps(wrong_fault, indent=1, sort_keys=True), encoding="utf-8"
    )
    _expect_fail(r, wrong_fault_path, "wrong scenario fault identity", source_soak)

    # 19. Same-test-id profiles cannot lower any canonical oracle threshold or
    # timeout while retaining their profile identity.
    for mutate, label in (
        (
            lambda profile: profile["oracle"]["structuredScenarioOracle"].update(
                {"minimum_capture_frames": 0}
            ),
            "same-id weakened oracle",
        ),
        (
            lambda profile: profile.update({"timeoutSeconds": 1}),
            "same-id weakened timeout",
        ),
        (
            lambda profile: profile["fault"]["rules"].update({"drop_every": 1}),
            "same-id weakened fault",
        ),
    ):
        r, _ = make_bundle()
        weakened = json.loads(PROFILE.read_text(encoding="utf-8"))
        mutate(weakened)
        weakened_path = r / f"weakened-{label.replace(' ', '-')}.json"
        weakened_path.write_text(
            json.dumps(weakened, indent=1, sort_keys=True), encoding="utf-8"
        )
        (r / "configs" / "profile.json").write_text(
            json.dumps(weakened, indent=1, sort_keys=True), encoding="utf-8"
        )
        _expect_fail(r, weakened_path, label, source_soak)

    print("CONTEST_NETWORK_QUALIFICATION_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
