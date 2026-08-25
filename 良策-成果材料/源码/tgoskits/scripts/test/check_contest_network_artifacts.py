#!/usr/bin/env python3
"""P4-EVID-01B contract: structured scenario counters/metrics publishers.

Deterministic host contract over publish_network_artifacts.py:
  - synthetic merged-console log -> exact counters (TX/RX/drop per direction)
  - scenario metrics math (100/100 -> 100%, 85/100 -> 85%)
  - no-overwrite on any existing artifact file
  - empty log -> valid zeroed counters (not an error)
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

import publish_network_artifacts as pub  # noqa: E402


def make_log(n_tx01: int, n_tx02: int, n_del1: int, n_del2: int, n_drop: int) -> str:
    lines = []
    for _ in range(n_tx01):
        lines.append("virtio-net TX from vm=0 len=98 dst=02:00:00:00:00:02 src=02:00:00:00:00:01")
        lines.append("virtio-net frame vm=0 dir=tx len=98 hex=" + "02" * 98)
    for _ in range(n_tx02):
        lines.append("virtio-net TX from vm=1 len=42 dst=ff:ff:ff:ff:ff:ff src=02:00:00:00:00:02")
        lines.append("virtio-net frame vm=1 dir=tx len=42 hex=" + "02" * 42)
    for _ in range(n_del1):
        lines.append("\x1b[m[VM 1] virtio-net RX delivered=1 to mac=02:00:00:00:00:01, pulsing IRQ")
    for _ in range(n_del2):
        lines.append("virtio-net RX delivered=1 to mac=02:00:00:00:00:02, pulsing IRQ")
    for _ in range(n_drop):
        lines.append("virtio-net switch drop from vm=1 reason=DestinationFull")
    return "\r\n".join(lines) + "\r\n"


def main() -> int:
    log = make_log(100, 104, 192, 104, 3)
    counters = pub.derive_counters(log)
    assert counters["capture_frames"] == 204, counters
    assert counters["capture_bytes"] == 100 * 98 + 104 * 42, counters
    assert counters["switch_deliver"] == 192 + 104, counters
    assert counters["switch_drop"] == 3, counters
    assert counters["delivered_linux"] == 192, counters
    assert counters["delivered_zephyr"] == 104, counters
    assert counters["schema_version"] == "p4-network-counters-v1"
    print("  [PASS] counters derive: frames=204 deliver=296 drop=3")

    # A concurrent UART writer can split a host RX line between physical
    # lines. The counters parser must apply the same repair as frame/TX
    # parsing and must count both edge-pulse and notify-suppressed outcomes.
    fragmented_rx = (
        "virtio-net RX delivered=17 to mac=02:00:\n"
        "00:00:00:01, pulsing IRQ\r\n"
        "virtio-net RX delivered=18 to mac=02:00:00:00:00:02, notify=false\r\n"
    )
    fragmented = pub.derive_counters(fragmented_rx)
    assert fragmented["delivered_linux"] == 1, fragmented
    assert fragmented["delivered_zephyr"] == 1, fragmented
    print("  [PASS] fragmented/pulse/notify=false RX lines are counted")

    # Endpoint teardown publishes exact per-generation totals. The publisher
    # must keep saturation separate from stale teardown rejection and must not
    # double-count a repeated final line for the same endpoint identity. A
    # stale worker may emit requeue counters after final teardown logging, so
    # those canonical-prefix lines must still be parsed for the same endpoint.
    ingress_totals = pub.derive_counters(
        "virtio-net ingress counters final vm=1 generation=3 "
        "full_drop=5 inactive_drop=2\r\n"
        "virtio-net ingress counters final vm=2 generation=7 "
        "full_drop=3 inactive_drop=1\r\n"
        "virtio-net ingress counters final vm=1 generation=3 "
        "full_drop=5 inactive_drop=2\r\n"
        "virtio-net ingress full-drop count=6 vm=1 generation=3 "
        "capacity=64 source=requeue\r\n"
        "virtio-net ingress inactive-drop count=3 vm=1 generation=3 "
        "source=requeue\r\n"
    )
    assert ingress_totals["ingress_full_drop"] == 9, ingress_totals
    assert ingress_totals["ingress_inactive_drop"] == 4, ingress_totals
    assert ingress_totals["ingress_counter_endpoints_finalized"] == 2, ingress_totals
    print("  [PASS] full/inactive ingress totals include late requeue diagnostics")

    aggregate_totals = pub.derive_counters(
        "virtio-net frame vm=1 generation=3 port=0 dir=port0_to_port1 "
        "len=14 hex=ffffffffffff0200000000010800 "
        "ingress_full_drop=0 inactive_target_drop=0\r\n"
        "virtio-net switch drop from vm=1 generation=3 reason=IngressFull "
        "ingress_full_drop=1 inactive_target_drop=0\r\n"
        "virtio-net frame evidence skipped from vm=1 generation=3 "
        "local_deliveries=0 uplink_requested=true "
        "ingress_full_drop=2 inactive_target_drop=1\r\n"
    )
    assert aggregate_totals["ingress_full_drop"] == 2, aggregate_totals
    assert aggregate_totals["ingress_inactive_drop"] == 1, aggregate_totals
    assert aggregate_totals["ingress_counter_observations"] == 3, aggregate_totals
    print("  [PASS] monotonic switch totals remain observable without graceful teardown")

    # metrics math
    raw, summary = pub.derive_metrics("test-011", "icmp", 100, 100, 0)
    assert summary["success_percent"] == 100.0, summary
    raw2, sum2 = pub.derive_metrics("test-015", "udp", 100, 85, 15)
    assert sum2["success_percent"] == 85.0, sum2
    print("  [PASS] metrics math: 100% and 85%")

    # write artifacts into a fresh bundle root
    root = ROOT / "target" / "contract-tests" / f"p4-artifacts-{secrets.token_hex(8)}"
    (root / "network").mkdir(parents=True)
    paths = pub.write_network_artifacts(
        root, scenario="test-011", transport="icmp", sent=100, received=100,
        loss=0, log_text=log,
    )
    for p in paths.values():
        assert p.is_file(), p
    counters_on_disk = json.loads(paths["counters"].read_text())
    assert counters_on_disk["capture_frames"] == 204
    summary_on_disk = json.loads(paths["summary"].read_text())
    assert summary_on_disk["success_percent"] == 100.0
    print("  [PASS] artifacts written into fresh bundle")

    # no-overwrite
    try:
        pub.write_network_artifacts(
            root, scenario="test-011", transport="icmp", sent=100, received=100,
            loss=0, log_text=log,
        )
    except FileExistsError as error:
        print(f"  [FAIL-CLOSED] no-overwrite: {error}")
    else:
        raise AssertionError("no-overwrite was not enforced")

    # empty log -> zeroed counters (valid, not an error)
    empty = pub.derive_counters("")
    assert empty["capture_frames"] == 0 and empty["switch_deliver"] == 0
    print("  [PASS] empty log yields zeroed counters")

    # A pre-validation TX diagnostic followed by a switch drop is not frame
    # evidence and must not inflate capture/enqueue counters.
    rejected = pub.derive_counters(
        "virtio-net TX from vm=1 len=14 dst=02:00:00:00:00:02 "
        "src=02:00:00:00:00:ff\r\n"
        "virtio-net switch drop from vm=1 generation=0 "
        "reason=SourceMacViolation\r\n"
    )
    assert rejected["capture_frames"] == 0, rejected
    assert rejected["capture_bytes"] == 0, rejected
    assert rejected["switch_enqueue"] == 0 and rejected["switch_drop"] == 1, rejected
    print("  [PASS] rejected TX diagnostics never become capture evidence")

    # P4-EVID-01B: frame evidence lines -> frames.jsonl + capture.pcap
    frame1 = bytes.fromhex("020000000001020000000002080045" + "00" * 83)  # 98 bytes
    frame2 = bytes.fromhex("0200000000020200000000010806" + "00" * 28)  # 42 bytes
    frame_log = (
        f"virtio-net TX from vm=0 len=98 dst=02:00:00:00:00:02 src=02:00:00:00:00:01\r\n"
        f"virtio-net frame vm=1 generation=7 port=0 dir=port0_to_port1 len=98 hex={frame1.hex()}\r\n"
        "\x1b[m[VM 2] virtio-net RX delivered=1 to mac=02:00:00:00:00:02, pulsing IRQ\r\n"
        f"virtio-net frame vm=2 generation=3 port=1 dir=port1_to_port0 len=42 hex={frame2.hex()}\r\n"
        "virtio-net frame vm=9 dir=tx len=20 hex=00112233445566778899aabbccddeeff00112233\r\n"
    )
    rows, skipped = pub.derive_frames(frame_log, run_id="r1", session_id="s1")
    assert len(rows) == 3 and skipped == 0, (rows, skipped)
    assert [row["sequence"] for row in rows] == [0, 1, 2], rows
    assert rows[0]["ingress_port"] == 0 and rows[1]["ingress_port"] == 1, rows
    assert rows[2]["ingress_port"] == 9, rows  # unknown vm kept verbatim
    assert rows[0]["generation"] == 7 and rows[1]["generation"] == 3, rows
    assert [rows[0]["direction"], rows[1]["direction"]] == [
        "port0_to_port1",
        "port1_to_port0",
    ], rows
    assert rows[0]["length"] == 98 and rows[0]["sha256"] == hashlib.sha256(bytes.fromhex(rows[0]["frame_hex"])).hexdigest()
    assert all(row["monotonic_ns"] > 1_000_000_000 for row in rows)
    print("  [PASS] frames derive: 3 rows, ports mapped, sha256 exact")

    # corrupt frame line (hex length mismatch) is skipped; a qualification
    # validator then fails closed on the counters/frames mismatch.
    bad = "virtio-net frame vm=1 dir=tx len=98 hex=deadbeef\n"
    rows_bad, skipped_bad = pub.derive_frames(bad, run_id="r", session_id="s")
    assert rows_bad == [] and skipped_bad == 1, (rows_bad, skipped_bad)
    print("  [PASS] corrupt frame line counted as skipped (fail-closed downstream)")

    # write frames artifacts (no-overwrite)
    frames_root = ROOT / "target" / "contract-tests" / f"p4-frames-{secrets.token_hex(8)}"
    frames_root.mkdir(parents=True)
    written = pub.write_frames_artifacts(
        frames_root, frame_log, run_id="r1", session_id="s1"
    )
    assert "frames" in written and "pcap" in written, written
    pcap = written["pcap"].read_bytes()
    assert pcap[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4"), pcap[:4]
    jsonl = written["frames"].read_text().strip().splitlines()
    assert len(jsonl) == 3, jsonl
    try:
        pub.write_frames_artifacts(frames_root, frame_log, run_id="r1", session_id="s1")
    except FileExistsError as error:
        print(f"  [FAIL-CLOSED] frames no-overwrite: {error}")
    else:
        raise AssertionError("frames no-overwrite was not enforced")

    # legacy v1 log without frame lines writes nothing (validator fails closed)
    legacy = pub.write_frames_artifacts(
        ROOT / "target" / "contract-tests" / f"p4-frames-empty-{secrets.token_hex(8)}",
        "virtio-net TX from vm=1 len=90 dst=33:33:00:00:00:16 src=02:00:00:00:00:02\n",
        run_id="r1", session_id="s1",
    )
    assert legacy == {}, legacy
    print("  [PASS] legacy log without frame lines writes nothing")

    print("CONTEST_NETWORK_ARTIFACTS_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
