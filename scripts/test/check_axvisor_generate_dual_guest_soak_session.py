#!/usr/bin/env python3
"""Contract tests for the dual-Guest soak-session manifest generator."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTEST = ROOT / "scripts" / "contest"
CONTRACT_TEST_ROOT = ROOT / "target" / "contract-tests"
sys.path.insert(0, str(CONTEST))
import generate_dual_guest_soak_session as generator  # noqa: E402


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def make_evidence(root: Path) -> tuple[Path, Path]:
    live = root / "live"
    prepared = root / "prepared"
    live.mkdir()
    prepared.mkdir()
    (prepared / "linux.resolved.toml").write_text("[base]\nid = 1\ncpu_num = 2\nphys_cpu_ids = [0, 1]\n", encoding="utf-8")
    (prepared / "zephyr.resolved.toml").write_text("[base]\nid = 2\ncpu_num = 1\nphys_cpu_ids = [2]\n", encoding="utf-8")
    (live / "axvisor-live.log").write_text(
        "AXVISOR_GUEST_DTB_READY vm=1 gpa=0x234e00000 size=2060 hpa_segments=0x234e00000:2060\n"
        "AXVISOR_GUEST_DTB_READY vm=2 gpa=0x47e00000 size=1620 hpa_segments=0x23ce00000:1620\n",
        encoding="utf-8",
    )
    (live / "guest-vm-1.console.log").write_text(
        "[    4.752944] AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=soak-boot-1\n",
        encoding="utf-8",
    )
    (live / "guest-vm-2.console.log").write_text(
        "AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=soak-boot-1\n",
        encoding="utf-8",
    )
    status = {
        "schemaVersion": 1,
        "artifactStatus": "run-complete",
        "status": "dual_guest_short_smoke_completed",
        "success": True,
        "state": {
            "bootId": "soak-boot-1",
            "sessionNonce": "0123456789abcdef0123456789abcdef",
            "qemuName": "axvisor-dual-soak-0123456789abcdef0123456789abcdef",
            "readyIdentity": {"pid": 4242},
            "qemuStartMonotonicNs": 500_000,
            "stabilityWindow": {"startMonotonicNs": 1_000_000, "endMonotonicNs": 2_800_000_000},
            "resolvedVmConfigs": {
                "linux": {"vmId": 1, "cpuNum": 2, "physCpuIds": [0, 1]},
                "zephyr": {"vmId": 2, "cpuNum": 1, "physCpuIds": [2]},
            },
        },
    }
    write(live / "status.json", status)
    return live, prepared


def main() -> int:
    errors: list[str] = []
    work = CONTRACT_TEST_ROOT / f"axvisor-soak-gen-contract-{uuid.uuid4().hex}"
    work.mkdir(parents=True)
    try:
        live, prepared = make_evidence(work)

        # 1. failed status must be rejected
        failed = json.loads((live / "status.json").read_text(encoding="utf-8"))
        failed["success"] = False
        write(live / "status.json", failed)
        try:
            generator.main(["--evidence-dir", str(live), "--prepared-dir", str(prepared), "--output", str(work / "out.json")])
        except SystemExit as e:
            if e.code == 0:
                errors.append("generator accepts a failed run status")
        else:
            errors.append("generator accepts a failed run status")
        write(live / "status.json", json.loads((work / "orig-status.json").read_text()) if False else json.loads(json.dumps({
            "schemaVersion": 1, "artifactStatus": "run-complete", "status": "dual_guest_short_smoke_completed", "success": True,
            "state": {
                "bootId": "soak-boot-1", "sessionNonce": "0123456789abcdef0123456789abcdef",
                "qemuName": "axvisor-dual-soak-0123456789abcdef0123456789abcdef",
                "readyIdentity": {"pid": 4242}, "qemuStartMonotonicNs": 500_000,
                "stabilityWindow": {"startMonotonicNs": 1_000_000, "endMonotonicNs": 2_800_000_000},
                "resolvedVmConfigs": {"linux": {"vmId": 1, "cpuNum": 2, "physCpuIds": [0, 1]}, "zephyr": {"vmId": 2, "cpuNum": 1, "physCpuIds": [2]}},
            },
        })))

        # 2. valid input generates a bound session
        out = work / "out.json"
        generator.main(["--evidence-dir", str(live), "--prepared-dir", str(prepared), "--output", str(out)])
        session = json.loads(out.read_text(encoding="utf-8"))
        identity = session["identity"]
        if identity["sessionNonce"] != "0123456789abcdef0123456789abcdef":
            errors.append("generated session does not bind the session nonce")
        if identity["qemuName"] != "axvisor-dual-soak-0123456789abcdef0123456789abcdef":
            errors.append("generated session does not bind the QEMU name")
        if session["cpuSets"] != {"linux": [0, 1], "zephyr": [2]}:
            errors.append("generated cpuSets are wrong")
        if session["startMonotonicNs"] != 1_000_000 or session["endMonotonicNs"] != 2_800_000_000:
            errors.append("generated monotonic window is wrong")
        if session["guests"][0]["readyMarker"] != "AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=soak-boot-1":
            errors.append("generated Linux ready marker must strip the kernel timestamp prefix")
        if session["guests"][1]["guestDtbMarker"] != "AXVISOR_GUEST_DTB_READY vm=2 gpa=0x47e00000 size=1620 hpa_segments=0x23ce00000:1620":
            errors.append("generated Zephyr DTB marker is wrong")
        if session["events"] != [] or len(session["healthMarkers"]) != 2:
            errors.append("generated session health/events are wrong")

        # 3. overwrite must be refused
        try:
            generator.main(["--evidence-dir", str(live), "--prepared-dir", str(prepared), "--output", str(out)])
        except SystemExit:
            pass
        else:
            errors.append("generator overwrites an existing output")
    finally:
        shutil.rmtree(work)
    if errors:
        print("Dual-guest soak-session generator contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("dual-guest soak-session generator contract passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
