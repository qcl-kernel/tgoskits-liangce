#!/usr/bin/env python3
"""Windows/Python-only contract tests for the host P4 network prerequisites."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
NETWORK_DIR = WORKSPACE_ROOT / "scripts" / "contest" / "network"
PROFILE_DIR = WORKSPACE_ROOT / "configs" / "contest" / "network"
sys.path.insert(0, str(NETWORK_DIR))

import capture  # noqa: E402
import fault_profile  # noqa: E402
import validate_network_session  # noqa: E402


PROFILE_PATHS = {
    "TEST-011": PROFILE_DIR / "test-011-v1.json",
    "TEST-012": PROFILE_DIR / "test-012-v1.json",
    "TEST-013": PROFILE_DIR / "test-013-v1.json",
    "TEST-015": PROFILE_DIR / "test-015-v1.json",
}


def _fail(message: str) -> None:
    raise AssertionError(message)


def _expect_failure(action: Callable[[], Any], label: str) -> None:
    try:
        action()
    except (AssertionError, ValueError, OSError):
        return
    _fail(f"{label} unexpectedly succeeded")


def _write_json(path: Path, value: Any) -> None:
    if path.exists():
        _fail(f"test fixture would overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _ethernet_frame(source_port: int, marker: int) -> bytes:
    destination = bytes.fromhex("020000000002" if source_port == 0 else "020000000001")
    source = bytes.fromhex("020000000001" if source_port == 0 else "020000000002")
    payload = bytes((marker,)) + bytes(range(1, 32))
    frame = destination + source + bytes.fromhex("0800") + payload
    return frame + bytes(60 - len(frame))


def _build_capture(run_id: str, session_id: str) -> capture.FrameCapture:
    result = capture.FrameCapture(max_frames=8, run_id=run_id)
    for index in range(4):
        port = index % 2
        result.record(
            _ethernet_frame(port, index),
            run_id=run_id,
            session_id=session_id,
            ingress_port=port,
            generation=0,
            direction=capture.direction_for_port(port),
            monotonic_ns=1_000 + index,
        )
    return result


def _check_profiles() -> tuple[fault_profile.FaultProfile, fault_profile.FaultPlan]:
    for test_id, path in PROFILE_PATHS.items():
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["test_id"] != test_id or document["host_only"] is not True:
            _fail(f"{path} is not a host-only profile for {test_id}")
        if document["network"] != validate_network_session.EXPECTED_NETWORK:
            _fail(f"{path} network constants drifted")
        if document["endpoints"] != validate_network_session.EXPECTED_ENDPOINTS:
            _fail(f"{path} endpoint constants drifted")
        parsed = fault_profile.parse_fault_profile(path)
        if document["fault"]["profile_id"] != parsed.profile_id:
            _fail(f"{path} fault profile id was not parsed")

    profile = fault_profile.parse_fault_profile(PROFILE_PATHS["TEST-012"])
    plan = fault_profile.generate_fault_plan(profile, 100)
    if plan.entries[9].actions != ("drop",):
        _fail("packet 10 does not have the fixed drop action")
    if plan.entries[16].actions != ("duplicate",):
        _fail("packet 17 does not have the fixed duplicate action")
    if plan.entries[22].actions != ("reorder",):
        _fail("packet 23 does not have the fixed reorder action")
    if plan.entries[28].actions != ("corrupt",):
        _fail("packet 29 does not have the fixed corrupt action")
    expected = {"drop": 10, "duplicate": 5, "reorder": 4, "corrupt": 3, "output_frames": 95}
    if plan.expected_counts() != expected:
        _fail(f"unexpected deterministic fault counts: {plan.expected_counts()}")
    if plan.as_dict() != fault_profile.generate_fault_plan(profile, 100).as_dict():
        _fail("fault plan is not deterministic")

    frames = tuple(_ethernet_frame(0, index) for index in range(40))
    short_plan = fault_profile.generate_fault_plan(profile, len(frames))
    applied = fault_profile.apply_fault_plan(frames, short_plan)
    if len(applied) != 38:
        _fail(f"fault application produced {len(applied)} frames instead of 38")
    corrupted = next(item for item in applied if item.source_packet_index == 29)
    if corrupted.frame == frames[28]:
        _fail("corrupt action did not change host-owned bytes")
    if [item.source_packet_index for item in applied].count(17) != 2:
        _fail("duplicate action did not produce two packet-17 deliveries")
    if [item.source_packet_index for item in applied].count(10) != 0:
        _fail("drop action left packet 10 in the output")
    disabled = fault_profile.parse_fault_profile(PROFILE_PATHS["TEST-011"])
    disabled_plan = fault_profile.generate_fault_plan(disabled, 40)
    if any(entry.actions for entry in disabled_plan.entries):
        _fail("disabled profile produced fault actions")
    _expect_failure(
        lambda: fault_profile.parse_fault_profile(
            {
                "schema_version": fault_profile.SCHEMA_VERSION,
                "profile_id": "bad",
                "seed": 1,
                "enabled": True,
                "rules": {"drop_every": 9},
                "operation_order": list(fault_profile.OPERATION_ORDER),
            }
        ),
        "non-frozen fault interval",
    )
    return profile, plan


def _check_capture(temp_root: Path) -> None:
    run_id = "network-contract-run"
    session_id = "session-012"
    bounded = capture.FrameCapture(max_frames=2, run_id=run_id)
    for index in range(3):
        result = bounded.record(
            _ethernet_frame(0, index),
            run_id=run_id,
            session_id=session_id,
            ingress_port=0,
            generation=0,
            direction="port0_to_port1",
            monotonic_ns=index,
        )
        if index < 2 and result is None:
            _fail("bounded capture dropped a frame before reaching capacity")
        if index == 2 and result is not None:
            _fail("bounded capture did not report an evidence-ring drop")
    if bounded.capture_drops != 1 or len(bounded.frames) != 2:
        _fail("bounded capture counters are incorrect")
    _expect_failure(
        lambda: bounded.record(
            _ethernet_frame(0, 9),
            run_id=run_id,
            session_id=session_id,
            ingress_port=0,
            generation=0,
            direction="port1_to_port0",
            monotonic_ns=4,
        ),
        "direction mismatch",
    )
    _expect_failure(
        lambda: bounded.record(
            _ethernet_frame(0, 9),
            run_id=run_id,
            session_id=session_id,
            ingress_port=0,
            generation=2,
            direction="port0_to_port1",
            monotonic_ns=5,
        ),
        "generation skip",
    )
    _expect_failure(
        lambda: bounded.record(
            _ethernet_frame(0, 9),
            run_id=run_id,
            session_id=session_id,
            ingress_port=0,
            generation=0,
            direction="port0_to_port1",
            monotonic_ns=0,
        ),
        "backward monotonic timestamp",
    )

    valid = _build_capture(run_id, session_id)
    pcap_path = temp_root / "capture.pcap"
    frames_path = temp_root / "frames.jsonl"
    valid.write_pcap(pcap_path)
    valid.write_jsonl(frames_path)
    pcap_records = capture.read_pcap(pcap_path)
    if [record[2] for record in pcap_records] != [frame.frame for frame in valid.frames]:
        _fail("PCAP bytes do not match the bounded capture")
    if pcap_path.read_bytes()[:4] != bytes.fromhex("d4c3b2a1"):
        _fail("PCAP did not use the standard little-endian magic")


def _session_document(
    run_id: str,
    session_id: str,
    nonce: str,
    fault_profile_id: str,
    fault_packet_count: int,
    artifacts: dict[str, str],
    fault_manifest_sha256: str,
    manifest_sha256: str,
    counters: dict[str, int],
    *,
    bad_linux_ip: str | None = None,
) -> dict[str, Any]:
    endpoints = json.loads(json.dumps(validate_network_session.EXPECTED_ENDPOINTS))
    if bad_linux_ip is not None:
        endpoints["linux"]["ip"] = bad_linux_ip
    return {
        "schema_version": "p4-network-session-v1",
        "run_id": run_id,
        "session_id": session_id,
        "nonce": nonce,
        "test_id": "TEST-012",
        "profile_id": "test-012-v1",
        "scenario": "udp-echo",
        "host_only": True,
        "evidence_level": "L2 host",
        "transport": "udp",
        "clock_domain": "monotonic_ns",
        "network": validate_network_session.EXPECTED_NETWORK,
        "endpoints": endpoints,
        "execution": {"host_only": True, "qemu": False, "wsl": False, "guest_runtime": False},
        "identity": {
            "producer": "p4-network-host",
            "run_id": run_id,
            "session_id": session_id,
            "nonce": nonce,
        },
        "artifacts": artifacts,
        "fault_profile_id": fault_profile_id,
        "fault_packet_count": fault_packet_count,
        "fault_manifest_sha256": fault_manifest_sha256,
        "frame_counters": counters,
        "manifest_sha256": manifest_sha256,
    }


def _build_session_fixture(
    root: Path,
    plan: fault_profile.FaultPlan,
    *,
    bad_linux_ip: str | None = None,
    status_last: bool = True,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    network_root = root / "network"
    run_id = "phase4-net-test012-host-contract"
    session_id = "012-host-session"
    nonce = "012-host-nonce"
    valid_capture = _build_capture(run_id, session_id)
    frames_path = network_root / "frames.jsonl"
    pcap_path = network_root / "capture.pcap"
    counters_path = network_root / "counters.json"
    fault_path = network_root / "fault-manifest.json"
    manifest_path = root / "manifest.json"
    valid_capture.write_jsonl(frames_path)
    valid_capture.write_pcap(pcap_path)
    _write_json(fault_path, plan.as_dict())
    fault_manifest_sha256 = hashlib.sha256(fault_path.read_bytes()).hexdigest()
    counters = {
        "capture_frames": len(valid_capture.frames),
        "capture_bytes": valid_capture.captured_bytes,
        "capture_drops": 0,
        "switch_enqueue": len(valid_capture.frames),
        "switch_deliver": len(valid_capture.frames),
        "switch_drop": 0,
        "fault_drop": plan.expected_counts()["drop"],
        "fault_duplicate": plan.expected_counts()["duplicate"],
        "fault_reorder": plan.expected_counts()["reorder"],
        "fault_corrupt": plan.expected_counts()["corrupt"],
    }
    _write_json(
        counters_path,
        {
            "schema_version": "p4-network-counters-v1",
            "run_id": run_id,
            "session_id": session_id,
            "counters": counters,
        },
    )
    artifacts = {
        "manifest": "manifest.json",
        "frames": "network/frames.jsonl",
        "pcap": "network/capture.pcap",
        "counters": "network/counters.json",
        "fault_manifest": "network/fault-manifest.json",
    }
    _write_json(
        manifest_path,
        {
            "schema_version": "p4-network-manifest-v1",
            "producer": "p4-network-host",
            "run_id": run_id,
            "session_id": session_id,
            "nonce": nonce,
            "profile_id": "test-012-v1",
            "artifacts": artifacts,
        },
    )
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    _write_json(
        root / "session.json",
        _session_document(
            run_id,
            session_id,
            nonce,
            plan.profile.profile_id,
            plan.packet_count,
            artifacts,
            fault_manifest_sha256,
            manifest_sha256,
            counters,
            bad_linux_ip=bad_linux_ip,
        ),
    )
    time.sleep(0.03)
    status: dict[str, Any] = {
        "schema_version": "p4-network-status-v1",
        "success": True,
        "status": "guest_network_completed",
        "primaryError": None,
        "cleanupError": None,
        "completedChecks": ["oracle", "hashes", "validator", "cleanup"],
        "manifestSha256": manifest_sha256,
        "statusLast": status_last,
    }
    _write_json(root / "status.json", status)


def _check_validator(temp_root: Path, plan: fault_profile.FaultPlan) -> None:
    valid_root = temp_root / "valid-session"
    _build_session_fixture(valid_root, plan)
    report = validate_network_session.validate_session(
        valid_root, PROFILE_PATHS["TEST-012"]
    )
    if not report["valid"] or report["capture_frames"] != 4:
        _fail("valid host-only session did not validate")
    output = temp_root / "validation.json"
    if validate_network_session.main(
        [
            "--session",
            str(valid_root),
            "--profile",
            str(PROFILE_PATHS["TEST-012"]),
            "--output",
            str(output),
        ]
    ) != 0:
        _fail("validator CLI rejected its valid fixture")
    if json.loads(output.read_text(encoding="utf-8"))["valid"] is not True:
        _fail("validator CLI did not publish a valid report")

    bad_endpoint = temp_root / "bad-endpoint"
    _build_session_fixture(bad_endpoint, plan, bad_linux_ip="10.77.0.9")
    _expect_failure(
        lambda: validate_network_session.validate_session(
            bad_endpoint, PROFILE_PATHS["TEST-012"]
        ),
        "fixed endpoint mismatch",
    )
    bad_status = temp_root / "bad-status-last"
    _build_session_fixture(bad_status, plan, status_last=False)
    _expect_failure(
        lambda: validate_network_session.validate_session(
            bad_status, PROFILE_PATHS["TEST-012"]
        ),
        "status-last mismatch",
    )


def main() -> int:
    temp_root: Path | None = None
    try:
        profile, plan = _check_profiles()
        if profile.profile_id != "udp-deterministic-v1":
            _fail("TEST-012 did not select the deterministic UDP profile")
        temporary_root = WORKSPACE_ROOT / "target" / "contract-tests"
        temporary_root.mkdir(parents=True, exist_ok=True)
        temp_root = temporary_root / (
            f"tgoskits-network-contract-{os.getpid()}-{secrets.token_hex(8)}"
        )
        temp_root.mkdir()
        _check_capture(temp_root / "capture")
        _check_validator(temp_root, fault_profile.generate_fault_plan(profile, 40))
    except (AssertionError, ValueError, OSError) as error:
        print(f"Contest network contract check failed: {error}", file=sys.stderr)
        return 1
    finally:
        if temp_root is not None and temp_root.exists():
            shutil.rmtree(temp_root)
    print("CONTEST_NETWORK_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
