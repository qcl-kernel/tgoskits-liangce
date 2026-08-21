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
        lines.append("\x1b[m[VM 1] virtio-net RX delivered=1 to mac=02:00:00:00:00:01, asserting IRQ")
    for _ in range(n_del2):
        lines.append("virtio-net RX delivered=1 to mac=02:00:00:00:00:02, asserting IRQ")
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

    # P4-EVID-01B: frame evidence lines -> frames.jsonl + capture.pcap
    frame1 = bytes.fromhex("020000000001020000000002080045" + "00" * 83)  # 98 bytes
    frame2 = bytes.fromhex("0200000000020200000000010806" + "00" * 28)  # 42 bytes
    frame_log = (
        f"virtio-net TX from vm=0 len=98 dst=02:00:00:00:00:02 src=02:00:00:00:00:01\r\n"
        f"virtio-net frame vm=0 dir=tx len=98 hex={frame1.hex()}\r\n"
        "\x1b[m[VM 2] virtio-net RX delivered=1 to mac=02:00:00:00:00:02, asserting IRQ\r\n"
        f"virtio-net frame vm=1 dir=tx len=42 hex={frame2.hex()}\r\n"
        "virtio-net frame vm=9 dir=tx len=20 hex=00112233445566778899aabbccddeeff00112233\r\n"
    )
    rows, skipped = pub.derive_frames(frame_log, run_id="r1", session_id="s1")
    assert len(rows) == 3 and skipped == 0, (rows, skipped)
    assert rows[0]["ingress_port"] == 0 and rows[1]["ingress_port"] == 1, rows
    assert rows[2]["ingress_port"] == 9, rows  # unknown vm kept verbatim
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
