#!/usr/bin/env python3
"""P4-EVID-01B contract: Guest-runtime v2 bundle publisher, fail-closed.

Deterministic host contract over v2_publisher.publish_v2:
  - a complete synthetic TEST-011 v2 bundle is published as
    `guest_network_qualified` with the exact v2 status/session/manifest fields;
  - every deficiency (missing DTBs, missing frames/pcap, residual processes,
    tampered counters) is published fail-closed as
    `guest_network_smoke_completed` with qualified=false and the qualification
    error preserved -- never a silent upgrade;
  - evidence artifacts are treated as read-only inputs: publishing a complete
    bundle never rewrites dtb/network/metrics/logs files;
  - session.json carries exactly the p4-network-session-v2 field set (the
    qualification validator rejects unknown fields, so a valid result proves
    the field set is exact).

Pure host Python: never launches QEMU, never touches real evidence.
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

import v2_publisher as pub  # noqa: E402

READY_LINUX = "AXVISOR_DUAL_GUEST_LINUX_READY"
READY_ZEPHYR = "AXVISOR_DUAL_GUEST_ZEPHYR_READY"
PROFILE = ROOT / "configs" / "contest" / "network" / "test-011-v2.json"

SESSION_FIELDS = {
    "schema_version",
    "run_id",
    "session_id",
    "nonce",
    "test_id",
    "profile_id",
    "scenario",
    "host_only",
    "evidence_level",
    "transport",
    "clock_domain",
    "network",
    "endpoints",
    "execution",
    "identity",
    "manifest_sha256",
}
STATUS_FIELDS = {
    "schema_version",
    "success",
    "status",
    "qualified",
    "primaryError",
    "cleanupError",
    "completedChecks",
    "manifestSha256",
    "statusLast",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frame(seq: int, direction: str, port: int, run_id: str, session_id: str) -> dict:
    payload = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x00]) + bytes(8)
    return {
        "schema_version": "p4-network-frame-v1",
        "sequence": seq,
        "run_id": run_id,
        "session_id": session_id,
        "ingress_port": port,
        "generation": 0,
        "direction": direction,
        "monotonic_ns": 1_000_000_000 + seq * 1_000_000,
        "length": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "frame_hex": payload.hex(),
    }


def make_bundle(complete: bool) -> tuple[Path, dict[str, str]]:
    """Build a synthetic TEST-011 bundle; returns (root, meta)."""
    run_id = f"p4net-pub-{secrets.token_hex(6)}"
    session_id = f"p4net-{secrets.token_hex(8)}"
    nonce = secrets.token_hex(16)
    root = ROOT / "target" / "contract-tests" / f"p4-v2pub-{secrets.token_hex(4)}" / run_id
    for sub in ("logs", "dtb", "network", "metrics"):
        (root / sub).mkdir(parents=True)

    (root / "logs" / "axvisor.raw.log").write_text(
        f"{READY_LINUX}\n{READY_ZEPHYR}\n", encoding="utf-8"
    )
    (root / "logs" / "linux.raw.log").write_text(
        f"{READY_LINUX}\nTGOS_LINUX_L3_SMOKE sent=100 received=100 loss=0\n",
        encoding="utf-8",
    )
    (root / "logs" / "zephyr.raw.log").write_text(f"{READY_ZEPHYR}\n", encoding="utf-8")

    if complete:
        for stem in ("linux-final", "zephyr-final"):
            (root / "dtb" / f"{stem}.dtb").write_bytes(b"\xd0\x0d\xfe\xed" * 8)
            (root / "dtb" / f"{stem}.dts").write_text(
                f"/dts-v1/; / {{}}; /* {stem} */\n", encoding="utf-8"
            )
        with (root / "network" / "frames.jsonl").open("w", encoding="utf-8") as handle:
            for seq in range(400):
                direction = "tx" if seq % 2 == 0 else "rx"
                port = seq % 2
                handle.write(
                    json.dumps(_frame(seq, direction, port, run_id, session_id))
                    + "\n"
                )
        (root / "network" / "capture.pcap").write_bytes(b"\xd4\xc3\xb2\xa1" + bytes(20))
        (root / "network" / "counters.json").write_text(
            json.dumps(
                {
                    "schema_version": "p4-network-counters-v1",
                    "capture_frames": 400,
                    "capture_bytes": 5600,
                    "capture_drops": 0,
                    "switch_enqueue": 400,
                    "switch_deliver": 400,
                    "switch_drop": 0,
                },
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "metrics" / "raw.jsonl").write_text(
            json.dumps({"scenario": "test-011", "sent": 100, "received": 100, "loss": 0})
            + "\n",
            encoding="utf-8",
        )
        (root / "metrics" / "summary.json").write_text(
            json.dumps({"scenario": "test-011", "success_rate": 1.0}, indent=1)
            + "\n",
            encoding="utf-8",
        )
    return root, {"run_id": run_id, "session_id": session_id, "nonce": nonce}


def _publish(root: Path, meta: dict[str, str], **extra) -> dict:
    profile_data = json.loads(PROFILE.read_text(encoding="utf-8"))
    return pub.publish_v2(
        root,
        PROFILE,
        run_id=meta["run_id"],
        session_id=meta["session_id"],
        nonce=meta["nonce"],
        test_id=profile_data["test_id"],
        profile_id=profile_data["profile_id"],
        scenario=profile_data["scenario"],
        evidence_level=profile_data["evidence_level"],
        transport=profile_data["transport"],
        network=profile_data["network"],
        endpoints=profile_data["endpoints"],
        **extra,
    )


def main() -> int:
    failures: list[str] = []

    def expect(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    # 1. Complete bundle -> guest_network_qualified with exact field sets.
    root, meta = make_bundle(complete=True)
    result = _publish(root, meta)
    expect(
        result["valid"] and result["status"] == "guest_network_qualified",
        f"[FAIL-CLOSED] complete bundle must qualify, got {result}",
    )
    session = json.loads((root / "session.json").read_text(encoding="utf-8"))
    expect(set(session) == SESSION_FIELDS, "[FAIL-CLOSED] session field set not exact")
    expect(session["host_only"] is False, "[FAIL-CLOSED] session host_only must be false")
    expect(session["identity"]["producer"] == "p4-network-guest", "[FAIL-CLOSED] identity producer")
    expect(
        session["identity"]["nonce"] == meta["nonce"], "[FAIL-CLOSED] identity nonce mismatch"
    )
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    expect(
        manifest["schema_version"] == "p4-network-manifest-v1",
        "[FAIL-CLOSED] manifest schema",
    )
    expect(
        session["manifest_sha256"] == _sha(root / "manifest.json"),
        "[FAIL-CLOSED] session manifest binding",
    )
    status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    expect(set(status) == STATUS_FIELDS, "[FAIL-CLOSED] status field set not exact")
    expect(status["status"] == "guest_network_qualified", "[FAIL-CLOSED] status token")
    expect(status["qualified"] is True, "[FAIL-CLOSED] status qualified")
    expect(status["statusLast"] is True, "[FAIL-CLOSED] status not last")
    expect(
        set(status["completedChecks"]) == {"oracle", "hashes", "validator", "cleanup"},
        "[FAIL-CLOSED] completedChecks must be the four checks",
    )
    expect(
        status["manifestSha256"] == session["manifest_sha256"],
        "[FAIL-CLOSED] status manifest binding",
    )
    # Artifacts are read-only inputs: publishing must not rewrite them.
    before = {
        rel: _sha(root / rel)
        for rel in (
            "dtb/linux-final.dtb",
            "network/frames.jsonl",
            "network/capture.pcap",
            "network/counters.json",
        )
    }
    _publish(root, meta)
    after = {
        rel: _sha(root / rel)
        for rel in (
            "dtb/linux-final.dtb",
            "network/frames.jsonl",
            "network/capture.pcap",
            "network/counters.json",
        )
    }
    expect(before == after, "[FAIL-CLOSED] publish rewrote evidence artifacts")

    # 2. Incomplete bundle (missing DTBs + frames/pcap) -> fail-closed smoke.
    root, meta = make_bundle(complete=False)
    result = _publish(root, meta)
    expect(
        result["valid"] is False and result["status"] == "guest_network_smoke_completed",
        f"[FAIL-CLOSED] incomplete bundle must stay smoke, got {result}",
    )
    status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    expect(status["qualified"] is False, "[FAIL-CLOSED] incomplete bundle marked qualified")
    expect(
        status["statusLast"] is True, "[FAIL-CLOSED] fail-closed status must be status-last"
    )
    expect(
        "qualification failed" in (status["primaryError"] or ""),
        "[FAIL-CLOSED] qualification error not preserved",
    )
    expect(
        set(status["completedChecks"]) <= {"oracle", "cleanup"},
        "[FAIL-CLOSED] fail-closed completedChecks overclaim",
    )

    # 3. Residual process -> fail-closed.
    root, meta = make_bundle(complete=True)
    result = _publish(root, meta, residual_processes=["qemu-system-aarch64: 12345"])
    expect(
        result["valid"] is False, "[FAIL-CLOSED] residual process must fail qualification"
    )
    status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    expect(status["qualified"] is False, "[FAIL-CLOSED] residual process marked qualified")

    # 4. Tampered counters (capture_frames mismatch) -> fail-closed.
    root, meta = make_bundle(complete=True)
    counters = root / "network" / "counters.json"
    counters.write_text(
        counters.read_text(encoding="utf-8").replace('"capture_frames": 400', '"capture_frames": 399'),
        encoding="utf-8",
    )
    result = _publish(root, meta)
    expect(
        result["valid"] is False,
        f"[FAIL-CLOSED] tampered counters must fail, got {result}",
    )

    if failures:
        print("\n".join(failures))
        return 1
    print("CONTEST_NETWORK_V2_PUBLISHER_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
