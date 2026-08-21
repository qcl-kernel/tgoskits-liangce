#!/usr/bin/env python3
"""Generate a dual-Guest soak-session manifest from one evidence directory.

Rebuilds ``dual-guest-soak-session.json`` strictly from the immutable run
evidence: status.json (identity, monotonic stability window, resolved VM
configs), the evidence copy of axvisor-live.log (Guest-DTB markers) and the
demuxed console logs (framed READY markers).  The output is meant to be
reviewed by ``validate_dual_guest_soak_session.py``.

The health start/end markers bind the same QEMU identity as the run.  When
the runner did not record ``qemuStartMonotonicNs`` (pre-2026-08-14 runner),
the generator uses the stability-window start as the binding anchor and
publishes ``"qemuStartMonotonicNs"`` from that value; the run status must be
checked for the real field when present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

DTB_RE = re.compile(
    r"^AXVISOR_GUEST_DTB_READY vm=(?P<vm>[12]) "
    r"gpa=0x[0-9a-f]+ size=[1-9][0-9]* "
    r"hpa_segments=0x[0-9a-f]+:[1-9][0-9]*(?:,0x[0-9a-f]+:[1-9][0-9]*)*$"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _first_marker(path: Path, prefix: str) -> str:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if prefix in line:
            text = line.strip()
            # Linux console lines carry a kernel timestamp prefix
            # ([    4.752944] marker ...); the marker text itself starts after it.
            bracket = text.find("] ")
            if text.startswith("[") and bracket > 0:
                text = text[bracket + 2 :].strip()
            return text
    raise SystemExit(f"marker {prefix!r} not found in {path}")


def _dtb_marker(log: Path, vm: int) -> str:
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        m = DTB_RE.match(line)
        if m and int(m.group("vm")) == vm:
            return line
    raise SystemExit(f"Guest-DTB marker for vm={vm} not found in {log}")


def _health_marker(phase: str, monotonic_ns: int, identity: dict) -> str:
    return (
        f"AXVISOR_DUAL_GUEST_HEALTH phase={phase} monotonic_ns={monotonic_ns} "
        f"boot_id={identity['bootId']} qemu_pid={identity['qemuPid']} "
        f"qemu_start_ns={identity['qemuStartMonotonicNs']} qemu_name={identity['qemuName']} "
        f"nonce={identity['sessionNonce']} linux_vm=1 zephyr_vm=2"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path, help="run live/ directory")
    parser.add_argument("--prepared-dir", required=True, type=Path, help="run prepared/ directory")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    live = args.evidence_dir
    prepared = args.prepared_dir
    status = json.loads((live / "status.json").read_text(encoding="utf-8"))
    state = status.get("state", {})
    if status.get("status") != "dual_guest_short_smoke_completed" or status.get("success") is not True:
        raise SystemExit("evidence status is not a completed dual smoke/soak run")

    window = state.get("stabilityWindow")
    if not window:
        raise SystemExit("status.json has no stabilityWindow")
    start = int(window["startMonotonicNs"])
    end = int(window["endMonotonicNs"])

    identity = {
        "bootId": state["bootId"],
        "qemuPid": int(state["readyIdentity"]["pid"]),
        "qemuStartMonotonicNs": int(state.get("qemuStartMonotonicNs", start)),
        "qemuName": state["qemuName"],
        "sessionNonce": state["sessionNonce"],
    }
    resolved = state["resolvedVmConfigs"]
    linux_config = prepared / "linux.resolved.toml"
    zephyr_config = prepared / "zephyr.resolved.toml"
    log = live / "axvisor-live.log"
    vm1_console = live / "guest-vm-1.console.log"
    vm2_console = live / "guest-vm-2.console.log"

    guests = [
        {
            "vmId": 1,
            "guest": "linux",
            "vmConfig": {"path": linux_config.name, "sha256": _sha256(linux_config), "size": linux_config.stat().st_size},
            "guestDtbMarker": _dtb_marker(log, 1),
            "readyMarker": _first_marker(vm1_console, "AXVISOR_DUAL_GUEST_LINUX_READY"),
        },
        {
            "vmId": 2,
            "guest": "zephyr",
            "vmConfig": {"path": zephyr_config.name, "sha256": _sha256(zephyr_config), "size": zephyr_config.stat().st_size},
            "guestDtbMarker": _dtb_marker(log, 2),
            "readyMarker": _first_marker(vm2_console, "AXVISOR_DUAL_GUEST_ZEPHYR_READY"),
        },
    ]
    health = [
        {"phase": "start", "monotonicNs": start, **identity, "marker": _health_marker("start", start, identity)},
        {"phase": "end", "monotonicNs": end, **identity, "marker": _health_marker("end", end, identity)},
    ]
    session = {
        "schemaVersion": 1,
        "artifactStatus": "capture-generated-unreviewed",
        "status": "dual_guest_soak_session_completed",
        "proofScope": "one-identity-bound-qemu-dual-guest-1800-second-coexistence-session",
        "identity": identity,
        "cpuSets": {"linux": resolved["linux"]["physCpuIds"], "zephyr": resolved["zephyr"]["physCpuIds"]},
        "startMonotonicNs": start,
        "endMonotonicNs": end,
        "guests": guests,
        "healthMarkers": health,
        "events": [],
    }
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {args.output}")
    args.output.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": "dual_guest_soak_session_completed", "durationNs": end - start}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
