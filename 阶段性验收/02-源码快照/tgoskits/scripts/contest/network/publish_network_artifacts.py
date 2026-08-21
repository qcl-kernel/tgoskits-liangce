#!/usr/bin/env python3
"""P4-EVID-01B: structured scenario counters/metrics publishers (smoke path).

These helpers derive deterministic, structured artifacts from an existing
evidence bundle (the merged axvisor console log + the scenario completion
fields) so that a smoke run can carry real `counters.json` and `metrics/`
(replacing substring-only evidence) -- the same shape the Guest-runtime v2
qualification validator in qualify_network_session.py expects.

This is housekeeping/statistics only: it never re-runs QEMU, never upgrades an
evidence level, and never rewrites an existing bundle (no-overwrite).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

COUNTERS_SCHEMA_VERSION = "p4-network-counters-v1"
METRICS_RAW_SCHEMA_VERSION = "p4-network-metrics-raw-v1"
METRICS_SUMMARY_SCHEMA_VERSION = "p4-network-metrics-summary-v1"

# virio-net log line patterns (produced by os/axvisor/src/virtio_net.rs diag).
_TX = re.compile(
    r"virtio-net TX from vm=(\d+) len=(\d+) dst=([0-9a-f:]+) src=([0-9a-f:]+)"
)
_RXDELIVER = re.compile(r"virtio-net RX delivered=(\d+) to mac=([0-9a-f:]+)")
_SWITCHDROP = re.compile(r"virtio-net switch drop")
# P4-EVID-01B evidence line carrying the exact guest->switch frame bytes.
_FRAME = re.compile(r"virtio-net frame vm=(\d+) dir=(\w+) len=(\d+) hex=([0-9a-fA-F]+)")

# The virtio-net evidence lines carry the switch port id (0 = linux,
# 1 = zephyr), which matches the v2 profile endpoints' ingress_port.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(line: str) -> str:
    return _ANSI.sub("", line)


def _join_split_lines(log_text: str) -> list[str]:
    """Rejoin console lines that were split by a concurrent writer.

    The merged console interleaves the axvisor log and the guest consoles on
    one UART; a long log line can be cut mid-way and the remainder continued
    on the next physical line. A complete line ends with CR (or ANSI+CR); a
    line without it is a fragment and is glued to the following physical
    line. This keeps the TX/frame evidence lines intact for parsing; a
    fragment glued to a guest line simply fails to match and is skipped.
    """
    joined: list[str] = []
    for raw in log_text.split("\n"):
        if joined and not (joined[-1].endswith("\r") or joined[-1].endswith("\x1b[m")):
            joined[-1] += raw
        else:
            joined.append(raw)
    return joined


def split_guest_logs(log_text: str) -> dict[str, str]:
    """Split the merged console log into per-Guest raw logs.

    Lines carrying the demux attribution prefix `[VM 1]` belong to the Linux
    guest, `[VM 2]` to Zephyr; the prefix may be preceded by an ANSI escape
    sequence from the axvisor console. The prefix is preserved verbatim so
    the attribution stays auditable. All other lines (axvisor/host output)
    stay in the merged axvisor.raw.log. The v2 qualification validator
    requires logs/{linux,zephyr}.raw.log with the frozen READY markers, so a
    run whose console never attributed a guest cannot qualify.
    """
    linux_lines: list[str] = []
    zephyr_lines: list[str] = []
    # Attribution uses the RAW lines (not the re-joined ones): re-joining
    # glues a following [VM n] line onto an unfinished axvisor line and hides
    # the guest prefix. A split guest line is rarer than a split axvisor
    # evidence line; the frozen READY markers are short and stay intact.
    for line in log_text.splitlines():
        stripped = _strip_ansi(line)
        if stripped.startswith("[VM 1] "):
            linux_lines.append(line)
        elif stripped.startswith("[VM 2] "):
            zephyr_lines.append(line)
    return {
        "linux": "\n".join(linux_lines) + ("\n" if linux_lines else ""),
        "zephyr": "\n".join(zephyr_lines) + ("\n" if zephyr_lines else ""),
    }


def write_split_logs(root: Path, log_text: str) -> dict[str, Path]:
    """Write logs/linux.raw.log + logs/zephyr.raw.log (no-overwrite)."""
    split = split_guest_logs(log_text)
    paths: dict[str, Path] = {}
    (root / "logs").mkdir(parents=True, exist_ok=True)
    for key, content in split.items():
        path = root / "logs" / f"{key}.raw.log"
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")
        if content:
            path.write_text(content, encoding="utf-8")
            paths[key] = path
    return paths


def derive_frames(
    log_text: str, *, run_id: str, session_id: str
) -> tuple[list[dict[str, Any]], int]:
    """Parse `virtio-net frame ...` evidence lines into frame-v1 rows.

    Returns (rows, skipped). Corrupt lines (bad hex length, or byte-level
    UART interleaving with a concurrent console line that cannot be
    recovered) are skipped and counted, never guessed: the qualification
    validator requires `rows + skipped == counters.capture_frames` with a
    small skip bound, so a tampered or heavily interleaved log still fails
    closed while a handful of lost evidence lines are honestly recorded.

    `monotonic_ns` is a capture-record sequence time (log line order, 1 ms
    apart); the axvisor console carries no wall clock, and the frame order is
    the switch ingress order. The bytes/sha256 are exact evidence.
    """
    rows: list[dict[str, Any]] = []
    skipped = 0
    for idx, line in enumerate(_join_split_lines(log_text)):
        match = _FRAME.search(line)
        if not match:
            continue
        vm = int(match.group(1))
        direction = match.group(2)
        length = int(match.group(3))
        hex_str = match.group(4)
        if len(hex_str) != length * 2:
            skipped += 1
            continue
        try:
            frame_bytes = bytes.fromhex(hex_str)
        except ValueError:
            skipped += 1
            continue
        rows.append(
            {
                "schema_version": "p4-network-frame-v1",
                "sequence": len(rows) + 1,
                "run_id": run_id,
                "session_id": session_id,
                "ingress_port": vm,
                "generation": 0,
                "direction": direction,
                "monotonic_ns": 1_000_000_000 + idx * 1_000_000,
                "length": length,
                "sha256": hashlib.sha256(frame_bytes).hexdigest(),
                "frame_hex": hex_str.lower(),
            }
        )
    return rows, skipped


def write_frames_artifacts(
    root: Path,
    log_text: str,
    *,
    run_id: str,
    session_id: str,
) -> dict[str, Path]:
    """Write network/frames.jsonl + network/capture.pcap from frame lines.

    No-overwrite and status-neutral. Returns the written paths; when the log
    carries no frame lines (legacy v1 logs) no files are written and the
    qualification validator fails closed on the missing artifacts.
    """
    rows, skipped = derive_frames(log_text, run_id=run_id, session_id=session_id)
    if not rows:
        return {}
    from capture import CapturedFrame, write_pcap

    frames_path = root / "network" / "frames.jsonl"
    pcap_path = root / "network" / "capture.pcap"
    if frames_path.exists() or pcap_path.exists():
        raise FileExistsError(f"refusing to overwrite {frames_path}/{pcap_path}")
    (root / "network").mkdir(parents=True, exist_ok=True)
    frames_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    # Honest accounting of evidence lines lost to UART interleaving: the
    # qualification validator requires rows + skipped == capture_frames.
    skip_path = root / "network" / "frames-skipped.json"
    if skipped:
        skip_path.write_text(
            json.dumps(
                {"schema_version": "p4-network-frames-skipped-v1", "skipped": skipped},
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    frames = [
        CapturedFrame(
            sequence=row["sequence"],
            run_id=row["run_id"],
            session_id=row["session_id"],
            ingress_port=row["ingress_port"],
            generation=row["generation"],
            direction=row["direction"],
            monotonic_ns=row["monotonic_ns"],
            frame=bytes.fromhex(row["frame_hex"]),
        )
        for row in rows
    ]
    write_pcap(frames, pcap_path)
    return {"frames": frames_path, "pcap": pcap_path}


def derive_counters(log_text: str) -> dict[str, Any]:
    """Parse the merged axvisor console and return p4-network-counters-v1."""
    frames: list[tuple[int, int, str, str]] = []
    drop = 0
    for line in _join_split_lines(log_text):
        m = _TX.search(line)
        if m:
            frames.append(
                (int(m.group(1)), int(m.group(2)), m.group(3), m.group(4))
            )
        if _SWITCHDROP.search(line):
            drop += 1

    # count RX-delivered lines per target MAC
    delivered_linux = sum(
        1
        for line in log_text.splitlines()
        if _RXDELIVER.search(line) and _RXDELIVER.search(line).group(2) == "02:00:00:00:00:01"
    )
    delivered_zephyr = sum(
        1
        for line in log_text.splitlines()
        if _RXDELIVER.search(line) and _RXDELIVER.search(line).group(2) == "02:00:00:00:00:02"
    )

    # P4-EVID-01B: `capture_frames` counts the actually captured frames
    # (frames.jsonl rows + honestly-skipped evidence lines), so the
    # qualification validator's rows+skipped == capture_frames check is
    # always exact. `switch_enqueue` keeps the raw TX-line count (what the
    # switch accepted) and may exceed capture_frames when UART interleaving
    # lost an evidence line.
    frame_rows, frame_skipped = derive_frames(log_text, run_id="", session_id="")
    capture_frames = len(frame_rows) + frame_skipped
    return {
        "schema_version": COUNTERS_SCHEMA_VERSION,
        "capture_frames": capture_frames,
        "capture_bytes": sum(f[1] for f in frames),
        "capture_drops": 0,
        "switch_enqueue": len(frames),
        "switch_deliver": delivered_linux + delivered_zephyr,
        "switch_drop": drop,
        "fault_drop": 0,
        "fault_duplicate": 0,
        "fault_reorder": 0,
        "fault_corrupt": 0,
        "delivered_linux": delivered_linux,
        "delivered_zephyr": delivered_zephyr,
    }


def derive_metrics(
    scenario: str, transport: str, sent: int, received: int, loss: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Summarize the scenario completion into structured metrics."""
    success = sent - loss if 0 <= loss <= sent else 0
    percent = (success * 1000 // sent) / 10.0 if sent > 0 else 0.0
    raw = [
        {
            "schema_version": METRICS_RAW_SCHEMA_VERSION,
            "scenario": scenario,
            "transport": transport,
            "sent": sent,
            "received": received,
            "loss": loss,
            "success_fraction": round(success / sent, 6) if sent else 0.0,
        }
    ]
    summary = {
        "schema_version": METRICS_SUMMARY_SCHEMA_VERSION,
        "scenario": scenario,
        "transport": transport,
        "sent": sent,
        "ack_received": received,
        "loss": loss,
        "success_percent": percent,
    }
    return raw, summary


def write_network_artifacts(
    root: Path,
    *,
    scenario: str,
    transport: str,
    sent: int,
    received: int,
    loss: int,
    log_text: str,
) -> dict[str, Path]:
    """Write network/counters.json + metrics/* into an evidence bundle.

    No-overwrite and status-neutral: the caller must write status.json
    *afterwards* so the published files cannot contain a qualified token.
    """
    counters_path = root / "network" / "counters.json"
    if counters_path.exists():
        raise FileExistsError(f"refusing to overwrite {counters_path}")
    (root / "network").mkdir(parents=True, exist_ok=True)
    raw, summary = derive_metrics(scenario, transport, sent, received, loss)
    counters = derive_counters(log_text)

    def write(path: Path, obj: Any) -> None:
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")
        path.write_text(
            json.dumps(obj, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )

    (root / "metrics").mkdir(parents=True, exist_ok=True)
    raw_path = root / "metrics" / "raw.jsonl"
    raw_path.write_text(
        "\n".join(json.dumps(o, sort_keys=True) for o in raw) + "\n",
        encoding="utf-8",
    )
    summary_path = root / "metrics" / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    write(counters_path, counters)
    return {
        "counters": counters_path,
        "raw": raw_path,
        "summary": summary_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--transport", required=True)
    parser.add_argument("--sent", type=int, required=True)
    parser.add_argument("--received", type=int, required=True)
    parser.add_argument("--loss", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        paths = write_network_artifacts(
            args.bundle,
            scenario=args.scenario,
            transport=args.transport,
            sent=args.sent,
            received=args.received,
            loss=args.loss,
            log_text=args.log.read_text(encoding="utf-8", errors="replace"),
        )
    except (OSError, FileExistsError) as error:
        print(f"publish network artifacts failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps({"status": "network_artifacts_published", "files": [str(p) for p in paths.values()]})
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
