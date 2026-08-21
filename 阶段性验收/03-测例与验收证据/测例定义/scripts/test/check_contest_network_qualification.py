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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NET_DIR = ROOT / "scripts" / "contest" / "network"
sys.path.insert(0, str(NET_DIR))

import qualify_network_session as q  # noqa: E402

FRAME_N = 400
READY_LINUX = "AXVISOR_DUAL_GUEST_LINUX_READY"
READY_ZEPHYR = "AXVISOR_DUAL_GUEST_ZEPHYR_READY"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_bundle() -> tuple[Path, str]:
    run_id = f"phase4-net-qualify-{secrets.token_hex(8)}"
    root = ROOT / "target" / "contract-tests" / f"p4-evid-{secrets.token_hex(4)}" / run_id
    for sub in ("logs", "dtb", "network", "metrics"):
        (root / sub).mkdir(parents=True)

    # logs with dual READY + completion
    (root / "logs" / "axvisor.raw.log").write_text(
        f"{READY_LINUX}\n{READY_ZEPHYR}\n", encoding="utf-8"
    )
    (root / "logs" / "linux.raw.log").write_text(
        f"{READY_LINUX}\nTGOS_LINUX_L3_SMOKE sent=100 received=100 loss=0\n",
        encoding="utf-8",
    )
    (root / "logs" / "zephyr.raw.log").write_text(
        f"{READY_ZEPHYR}\n", encoding="utf-8"
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
        "network": {"name": "vnet0"},
        "endpoints": {"linux": {"vm_id": 1}, "zephyr": {"vm_id": 2}},
        "execution": {"host_only": False, "qemu": True, "wsl": True, "guest_runtime": True},
        "identity": {"producer": "p4-network-guest", "run_id": run_id, "session_id": run_id, "nonce": "a1b2c3d4e5f60708"},
        "manifest_sha256": "0" * 64,
    }
    (root / "session.json").write_text(json.dumps(session, indent=1, sort_keys=True), encoding="utf-8")

    # manifest (hashes content artifacts only)
    artifacts = [
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


def _expect_pass(root: Path, profile: Path) -> None:
    report = q.qualify_guest_session(root, profile)
    if report["status"] != "guest_network_qualified" or not report["qualified"]:
        raise AssertionError(f"expected qualified, got {report}")
    print(f"  [PASS] qualified: run={report['run_id']} frames={report['capture_frames']}")


def _expect_fail(root: Path, profile: Path, label: str) -> None:
    try:
        q.qualify_guest_session(root, profile)
    except q.QualificationError as error:
        print(f"  [FAIL-CLOSED] {label}: {error}")
        return
    raise AssertionError(f"expected qualification failure for {label}")


def main() -> int:
    profile = ROOT / "configs" / "contest" / "network" / "test-011-v2.json"

    root, sha = make_bundle()
    _expect_pass(root, profile)

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

    # 4. missing zephyr DTB
    r, _ = make_bundle()
    (r / "dtb" / "zephyr-final.dtb").unlink()
    cases.append(("missing zephyr-final.dtb", r))

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

    # 10. missing pcap
    r, _ = make_bundle()
    (r / "network" / "capture.pcap").unlink()
    cases.append(("missing capture.pcap", r))

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

    for label, run_root in cases:
        _expect_fail(run_root, profile, label)

    print("CONTEST_NETWORK_QUALIFICATION_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
