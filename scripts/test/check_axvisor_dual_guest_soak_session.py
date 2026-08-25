#!/usr/bin/env python3
"""Contract tests for the no-overwrite dual-Guest soak-session validator."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTEST = ROOT / "scripts" / "contest"
sys.path.insert(0, str(CONTEST))
import validate_dual_guest_soak_session as validator  # noqa: E402


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def session(linux: bytes, zephyr: bytes) -> dict[str, object]:
    nonce = "0123456789abcdef0123456789abcdef"
    identity = {
        "bootId": "boot-20260810-a",
        "qemuPid": 4242,
        "qemuStartMonotonicNs": 100_000,
        "qemuName": f"axvisor-dual-soak-{nonce}",
        "sessionNonce": nonce,
    }
    start = 1_000_000_000
    end = start + validator.MIN_DURATION_NS

    def health(phase: str, monotonic: int) -> dict[str, object]:
        return {
            "phase": phase,
            "monotonicNs": monotonic,
            **identity,
            "marker": validator._health_marker(phase, monotonic, identity),
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
            {"vmId": 1, "guest": "linux", "vmConfig": {"path": "linux.toml", "sha256": sha(linux), "size": len(linux)}, "guestDtbMarker": "AXVISOR_GUEST_DTB_READY vm=1 gpa=0x40000000 size=65536 hpa_segments=0x40000000:32768,0x40008000:32768", "readyMarker": "AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=boot-20260810-a"},
            {"vmId": 2, "guest": "zephyr", "vmConfig": {"path": "zephyr.toml", "sha256": sha(zephyr), "size": len(zephyr)}, "guestDtbMarker": "AXVISOR_GUEST_DTB_READY vm=2 gpa=0x50000000 size=65536 hpa_segments=0x50000000:65536", "readyMarker": "AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=boot-20260810-a"},
        ],
        "healthMarkers": [health("start", start), health("end", end)],
        "events": [],
    }


def invoke(base: Path, payload: object, name: str, *, output: str | None = None) -> tuple[int, str]:
    session_path = base / name
    write(session_path, payload)
    capture = io.StringIO()
    with contextlib.redirect_stderr(capture):
        code = validator.main(["--session", str(session_path), "--linux-vm-config", str(base / "linux.toml"), "--zephyr-vm-config", str(base / "zephyr.toml"), "--output", str(base / (output or f"{name}.result.json"))])
    return code, capture.getvalue()


def must_fail(base: Path, payload: object, name: str, needle: str) -> None:
    code, stderr = invoke(base, payload, name)
    assert code == 1 and needle in stderr, (name, code, stderr)


def main() -> int:
    base = ROOT / "target" / "contract-tests" / f"dual-guest-soak-contract-{uuid.uuid4().hex}"
    base.mkdir(parents=True)
    linux = b"[base]\nid = 1\ncpu_num = 2\nphys_cpu_ids = [0, 1]\n"
    zephyr = b"[base]\nid = 2\ncpu_num = 1\nphys_cpu_ids = [2]\n"
    (base / "linux.toml").write_bytes(linux)
    (base / "zephyr.toml").write_bytes(zephyr)
    good = session(linux, zephyr)
    try:
        code, stderr = invoke(base, good, "good.json", output="result.json")
        assert code == 0, stderr
        result = json.loads((base / "result.json").read_text(encoding="utf-8"))
        assert result["status"] == "dual_guest_30min_coexistence_observed"
        assert result["durationNs"] == validator.MIN_DURATION_NS
        assert result["doesNotProve"] == ["Linux and Zephyr IP connectivity", "DMA was executed", "DMA isolation", "end-to-end latency", "AI closed-loop control"]
        alternate_linux = b"[base]\nid = 1\ncpu_num = 2\nphys_cpu_ids = [7, 8]\n"
        alternate_cpu_sets = copy.deepcopy(good)
        alternate_cpu_sets["cpuSets"] = {"linux": [7, 8], "zephyr": [2]}
        alternate_cpu_sets["guests"][0]["vmConfig"] = {"path": "linux.toml", "sha256": sha(alternate_linux), "size": len(alternate_linux)}
        alternate_validated = validator.validate_session(alternate_cpu_sets, linux_config=base / "linux.toml", linux_bytes=alternate_linux, zephyr_config=base / "zephyr.toml", zephyr_bytes=zephyr)
        assert alternate_validated["cpuSets"] == {"linux": [7, 8], "zephyr": [2]}
        code, stderr = invoke(base, good, "again.json", output="result.json")
        assert code == 1 and "already exists" in stderr, stderr

        short = copy.deepcopy(good); short["endMonotonicNs"] = int(short["startMonotonicNs"]) + validator.MIN_DURATION_NS - 1
        short["healthMarkers"][1]["monotonicNs"] = short["endMonotonicNs"]
        short["healthMarkers"][1]["marker"] = validator._health_marker("end", short["endMonotonicNs"], short["identity"])
        must_fail(base, short, "short.json", "at least 1800 seconds")

        identity = copy.deepcopy(good); identity["healthMarkers"][1]["qemuPid"] = 999
        must_fail(base, identity, "identity.json", "does not bind the QEMU identity")
        duplicate = copy.deepcopy(good); duplicate["guests"].append(copy.deepcopy(duplicate["guests"][0]))
        must_fail(base, duplicate, "duplicate.json", "exactly VM1 Linux and VM2 Zephyr")
        restart = copy.deepcopy(good); restart["events"] = [{"kind": "restart"}]
        must_fail(base, restart, "restart.json", "forbidden")
        wrong_cpu_sets = copy.deepcopy(good); wrong_cpu_sets["cpuSets"] = {"linux": [0], "zephyr": [1]}
        must_fail(base, wrong_cpu_sets, "wrong-cpu-sets.json", "supplied VM configs")
        wrong_vm_id = copy.deepcopy(good)
        wrong_linux = b"[base]\nid = 2\ncpu_num = 2\nphys_cpu_ids = [0, 1]\n"
        wrong_vm_id["guests"][0]["vmConfig"] = {
            "path": "linux.toml",
            "sha256": sha(wrong_linux),
            "size": len(wrong_linux),
        }
        validated_error = None
        try:
            validator.validate_session(
                wrong_vm_id,
                linux_config=base / "linux.toml",
                linux_bytes=wrong_linux,
                zephyr_config=base / "zephyr.toml",
                zephyr_bytes=zephyr,
            )
        except validator.SoakError as error:
            validated_error = str(error)
        assert validated_error and "base.id must be 1" in validated_error
        wrong_cpu_num = copy.deepcopy(good)
        wrong_linux = b"[base]\nid = 1\ncpu_num = 1\nphys_cpu_ids = [0, 1]\n"
        wrong_cpu_num["guests"][0]["vmConfig"] = {
            "path": "linux.toml",
            "sha256": sha(wrong_linux),
            "size": len(wrong_linux),
        }
        validated_error = None
        try:
            validator.validate_session(
                wrong_cpu_num,
                linux_config=base / "linux.toml",
                linux_bytes=wrong_linux,
                zephyr_config=base / "zephyr.toml",
                zephyr_bytes=zephyr,
            )
        except validator.SoakError as error:
            validated_error = str(error)
        assert validated_error and "base.cpu_num" in validated_error
        legacy_hpa_segments = copy.deepcopy(good); legacy_hpa_segments["guests"][0]["guestDtbMarker"] = "AXVISOR_GUEST_DTB_READY vm=1 gpa=0x40000000 size=65536 hpa_segments=1"
        must_fail(base, legacy_hpa_segments, "legacy-hpa-segments.json", "Guest-DTB marker")
        reversed_health = copy.deepcopy(good); reversed_health["healthMarkers"].reverse()
        must_fail(base, reversed_health, "reversed.json", "start then end")
        missing = copy.deepcopy(good); del missing["guests"][1]["readyMarker"]
        must_fail(base, missing, "missing.json", "guest keys")
        duplicate_json = base / "duplicate-key.json"
        duplicate_json.write_text('{"schemaVersion":1,"schemaVersion":1}\n', encoding="utf-8")
        capture = io.StringIO()
        with contextlib.redirect_stderr(capture):
            code = validator.main(["--session", str(duplicate_json), "--linux-vm-config", str(base / "linux.toml"), "--zephyr-vm-config", str(base / "zephyr.toml"), "--output", str(base / "dup-result.json")])
        assert code == 1 and "duplicate JSON key" in capture.getvalue()
        (base / "linux.toml").write_bytes(b"[base\nphys_cpu_ids = [0, 1]\n")
        must_fail(base, good, "invalid-linux-config.json", "not valid UTF-8 TOML")
    finally:
        # Keep the uniquely named fixture for post-failure inspection. The
        # contract test never deletes or overwrites pre-existing evidence.
        pass
    print("dual-guest soak-session contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
