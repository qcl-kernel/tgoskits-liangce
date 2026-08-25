#!/usr/bin/env python3
"""Host-only positive/negative contract for the TEST-017 bundle validator.

The synthetic fixture exercises schema and tamper detection only. It is never
runtime, Guest-IP, or AI-loop evidence.
"""

from __future__ import annotations

import csv
import hashlib
import json
import secrets
import struct
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "scripts" / "contest" / "ai"
PROFILE_PATH = ROOT / "configs" / "contest" / "ai" / "qualification-v1.json"
SOURCE_MODEL = ROOT / "apps" / "contest" / "linux-ai-controller" / "model"
sys.path.insert(0, str(AI_DIR))

import closed_loop_contract as contract  # noqa: E402
import compute_metrics  # noqa: E402
import produce_icpc_records  # noqa: E402
import validate_closed_loop as validator  # noqa: E402


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    write_text(path, "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def event(
    endpoint: str,
    name: str,
    request_id: int,
    sample_index: int,
    monotonic_ns: int,
    *,
    value=None,
    unit=None,
    outcome: str = "ok",
) -> dict[str, Any]:
    sequences = {
        ("linux", "input_receive"): request_id * 10 + 1,
        ("linux", "packet_send"): request_id * 10 + 2,
        ("linux", "ack_receive"): request_id * 10 + 3,
        ("linux", "feedback_receive"): request_id * 10 + 4,
        ("zephyr", "packet_receive"): request_id * 10 + 2,
        ("zephyr", "ack_send"): request_id * 10 + 3,
        ("zephyr", "control_apply"): request_id * 10 + 2,
        ("zephyr", "packet_send"): request_id * 10 + 4,
    }
    return {
        "schema_version": "p5-ai-event-v1",
        "run_id": "phase5-ai-fixed-s7-validator-contract",
        "endpoint": endpoint,
        "scenario": "test-017",
        "transport": "udp",
        "session_id": 17,
        "sequence": sequences.get((endpoint, name)),
        "request_id": request_id,
        "sample_index": sample_index,
        "event": name,
        "monotonic_ns": monotonic_ns,
        "value": value,
        "unit": unit,
        "outcome": outcome,
    }


def crc32c(data: bytes) -> int:
    crc = 0xFFFF_FFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F6_3B78 if crc & 1 else 0)
    return (~crc) & 0xFFFF_FFFF


def internet_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def ethernet_ipv4_udp(
    *,
    source_mac: bytes,
    destination_mac: bytes,
    source_ipv4: bytes,
    destination_ipv4: bytes,
    payload: bytes,
    identification: int,
) -> bytes:
    udp_length = 8 + len(payload)
    udp = bytearray(
        (46000).to_bytes(2, "big")
        + (46000).to_bytes(2, "big")
        + udp_length.to_bytes(2, "big")
        + b"\0\0"
        + payload
    )
    pseudo_header = (
        source_ipv4
        + destination_ipv4
        + b"\0\x11"
        + udp_length.to_bytes(2, "big")
    )
    udp_checksum = internet_checksum(pseudo_header + udp)
    udp[6:8] = (udp_checksum or 0xFFFF).to_bytes(2, "big")
    ipv4 = bytearray(
        b"\x45\0"
        + (20 + udp_length).to_bytes(2, "big")
        + identification.to_bytes(2, "big")
        + b"\x40\0"
        + b"\x40\x11"
        + b"\0\0"
        + source_ipv4
        + destination_ipv4
    )
    ipv4[10:12] = internet_checksum(ipv4).to_bytes(2, "big")
    return destination_mac + source_mac + b"\x08\0" + ipv4 + udp


def icpc_wire(
    message_type: int,
    flags: int,
    sequence: int,
    ack_sequence: int,
    timestamp_ms: int,
    payload: bytes,
) -> bytes:
    header = (
        b"ICPC"
        + bytes((1, 36, message_type, flags))
        + (17).to_bytes(4, "big")
        + sequence.to_bytes(4, "big")
        + ack_sequence.to_bytes(4, "big")
        + timestamp_ms.to_bytes(8, "big")
        + len(payload).to_bytes(2, "big")
        + b"\0\0\0\0\0\0"
    )
    packet = bytearray(header + payload)
    packet[32:36] = crc32c(bytes(packet)).to_bytes(4, "big")
    return bytes(packet)


def make_pcap(path: Path, packets: list[bytes]) -> None:
    # Little-endian microsecond Ethernet PCAP with complete packet records.
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = bytearray(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    for index, packet in enumerate(packets, 1):
        encoded.extend(struct.pack("<IIII", index, 0, len(packet), len(packet)))
        encoded.extend(packet)
    path.write_bytes(encoded)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_fixture(root: Path) -> None:
    profile = contract.load_qualification_profile(PROFILE_PATH)
    run_id = "phase5-ai-fixed-s7-validator-contract"
    write_json(root / "configs" / "qualification-v1.json", profile)
    fault_manifest = contract.generate_fault_manifest(profile=profile, seed=7)
    write_json(root / "configs" / "fault-manifest.json", fault_manifest)
    write_json(root / "configs" / "linux-app-config.json", {"controller": "fixed", "seed": 7})
    write_text(root / "configs" / "zephyr-dotconfig", "CONFIG_NUM_PREEMPT_PRIORITIES=8\n")
    write_text(root / "configs" / "zephyr.dts", "/dts-v1/; / { model = \"contract\"; };\n")

    for name in ("model.bin", "metadata.json", "golden-vectors.json"):
        destination = root / "model" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((SOURCE_MODEL / name).read_bytes())
    (root / "model" / "dataset-manifest.json").write_bytes(
        (SOURCE_MODEL / "dataset-manifest.json").read_bytes()
    )

    temperature_mC = int(profile["plant"]["initial_temperature_mC"])
    trajectory: list[dict[str, Any]] = []
    linux_events: list[dict[str, Any]] = []
    zephyr_safe_enter = event("zephyr", "safe_enter", 1, 0, 0)
    zephyr_safe_enter.update(sequence=None, request_id=None, sample_index=None)
    zephyr_safe_exit = event("zephyr", "safe_exit", 1, 0, 1)
    zephyr_safe_exit.update(sequence=None, request_id=None, sample_index=None)
    zephyr_events: list[dict[str, Any]] = [zephyr_safe_enter, zephyr_safe_exit]
    frames: list[dict[str, Any]] = []
    packets: list[bytes] = []
    icpc_records: list[dict[str, Any]] = []
    dropped_samples = set(
        profile["fault_golden"]["7"]["drop_first_control_request_indices"]
    )

    def add_icpc_record(
        *,
        request_id: int,
        sample_index: int,
        direction: str,
        attempt: int,
        disposition: str,
        message_type: str,
        flags: int,
        sequence: int,
        ack_sequence: int,
        timestamp_ms: int,
        monotonic_ns: int,
        payload: bytes,
    ) -> None:
        numeric_type = {"control": 1, "status": 2, "ack": 4}[message_type]
        wire = icpc_wire(
            numeric_type,
            flags,
            sequence,
            ack_sequence,
            timestamp_ms,
            payload,
        )
        frame_sequence: int | None = None
        if disposition == "forwarded":
            frame_sequence = len(frames)
            linux_to_zephyr = direction == "linux_to_zephyr"
            destination = bytes.fromhex(
                "020000000002" if linux_to_zephyr else "020000000001"
            )
            source = bytes.fromhex(
                "020000000001" if linux_to_zephyr else "020000000002"
            )
            source_ipv4 = bytes((10, 77, 0, 1 if linux_to_zephyr else 2))
            destination_ipv4 = bytes((10, 77, 0, 2 if linux_to_zephyr else 1))
            frame = ethernet_ipv4_udp(
                source_mac=source,
                destination_mac=destination,
                source_ipv4=source_ipv4,
                destination_ipv4=destination_ipv4,
                payload=wire,
                identification=frame_sequence,
            )
            packets.append(frame)
            frames.append(
                {
                    "schema_version": "p4-network-frame-v1",
                    "sequence": frame_sequence,
                    "run_id": run_id,
                    "session_id": "17",
                    "ingress_port": 0 if linux_to_zephyr else 1,
                    "generation": 0,
                    "direction": (
                        "port0_to_port1" if linux_to_zephyr else "port1_to_port0"
                    ),
                    "monotonic_ns": monotonic_ns,
                    "length": len(frame),
                    "sha256": hashlib.sha256(frame).hexdigest(),
                    "frame_hex": frame.hex(),
                }
            )
        icpc_records.append(
            {
                "schema_version": "p5-ai-icpc-record-v1",
                "record_index": len(icpc_records) + 1,
                "run_id": run_id,
                "session_id": 17,
                "frame_sequence": frame_sequence,
                "direction": direction,
                "attempt": attempt,
                "fault_disposition": disposition,
                "message_type": message_type,
                "flags": flags,
                "sequence": sequence,
                "ack_sequence": ack_sequence,
                "request_id": request_id,
                "sample_index": sample_index,
                "timestamp_ms": timestamp_ms,
                "monotonic_ns": monotonic_ns,
                "payload_length": len(payload),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "payload_hex": payload.hex(),
                "wire_length": len(wire),
                "wire_sha256": hashlib.sha256(wire).hexdigest(),
                "wire_hex": wire.hex(),
            }
        )
    linux_names = (
        "period_release",
        "period_start",
        "input_receive",
        "packet_send",
        "ack_receive",
        "feedback_receive",
        "period_finish",
    )
    zephyr_names = (
        "period_release",
        "period_start",
        "packet_receive",
        "ack_send",
        "control_apply",
        "packet_send",
        "period_finish",
    )
    linux_offsets = {
        "period_release": 0,
        "period_start": 10_000,
        "input_receive": 20_000,
        "packet_send": 50_000,
        "ack_receive": 150_000,
        "feedback_receive": 220_000,
        "period_finish": 230_000,
    }
    zephyr_offsets = {
        "period_release": 0,
        "period_start": 10_000,
        "packet_receive": 20_000,
        "ack_send": 30_000,
        "control_apply": 120_000,
        "packet_send": 130_000,
        "period_finish": 140_000,
    }
    fixed_controller = contract.FixedPiController()
    for sample_index in range(1800):
        input_temperature_mC = temperature_mC
        duty_q16_16 = fixed_controller.update(input_temperature_mC, 55_000)
        temperature_mC = contract.plant_step(
            temperature_mC, duty_q16_16, sample_index
        )
        request_id = sample_index + 1
        trajectory.append(
            {
                "controller": "fixed",
                "seed": 7,
                "sample_index": sample_index,
                "measured_mC": temperature_mC,
                "target_mC": 55_000,
                "duty_q16_16": duty_q16_16,
                "model_version": 0,
                "health_flags": 0,
                "applied_request_id": request_id,
                "feedback_confirmed": 1,
                "closed_loop_rtt_ns": 200_000,
                "action_latency_ns": 100_000,
            }
        )
        base = sample_index * 100_000_000 + 10
        should_drop = sample_index in dropped_samples
        linux_request_events = [
            event(
                "linux",
                name,
                request_id,
                sample_index,
                base + linux_offsets[name],
                value=(
                    input_temperature_mC
                    if name == "input_receive"
                    else temperature_mC
                    if name == "feedback_receive"
                    else duty_q16_16
                    if name == "packet_send"
                    else request_id * 10 + 2
                    if name == "ack_receive"
                    else None
                ),
                unit=(
                    "mC"
                    if name in ("input_receive", "feedback_receive")
                    else "q16_16"
                    if name == "packet_send"
                    else "sequence"
                    if name == "ack_receive"
                    else None
                ),
                outcome=(
                    "status"
                    if name == "input_receive"
                    else "status_applied"
                    if name == "feedback_receive"
                    else ("fault_drop" if should_drop else "sent")
                    if name == "packet_send"
                    else "ack"
                    if name == "ack_receive"
                    else "ok"
                ),
            )
            for name in linux_names
        ]
        if should_drop:
            linux_request_events.insert(
                4,
                event(
                    "linux",
                    "packet_send",
                    request_id,
                    sample_index,
                    base + 60_000,
                    value=duty_q16_16,
                    unit="q16_16",
                    outcome="retry_sent",
                ),
            )
        linux_events.extend(linux_request_events)
        zephyr_events.extend(
            event(
                "zephyr",
                name,
                request_id,
                sample_index,
                base + zephyr_offsets[name],
                value=(
                    duty_q16_16
                    if name in ("packet_receive", "control_apply")
                    else request_id * 10 + 2
                    if name == "ack_send"
                    else temperature_mC
                    if name == "packet_send"
                    else None
                ),
                unit=(
                    "q16_16"
                    if name in ("packet_receive", "control_apply")
                    else "sequence"
                    if name == "ack_send"
                    else "mC"
                    if name == "packet_send"
                    else None
                ),
                outcome=(
                    "control_valid"
                    if name == "packet_receive"
                    else "ack"
                    if name == "ack_send"
                    else "applied"
                    if name == "control_apply"
                    else "status"
                    if name == "packet_send"
                    else "ok"
                ),
            )
            for name in zephyr_names
        )

        control_payload = (
            bytes((1, 1, 0, 0))
            + request_id.to_bytes(4, "big")
            + duty_q16_16.to_bytes(4, "big", signed=True)
            + (55_000).to_bytes(4, "big", signed=True)
            + (0).to_bytes(4, "big")
            + (500).to_bytes(2, "big")
            + b"\0\0"
        )
        control_sequence = request_id * 10 + 2
        add_icpc_record(
            request_id=request_id,
            sample_index=sample_index,
            direction="linux_to_zephyr",
            attempt=0,
            disposition="profile_drop" if should_drop else "forwarded",
            message_type="control",
            flags=1,
            sequence=control_sequence,
            ack_sequence=0,
            timestamp_ms=sample_index * 100,
            monotonic_ns=base + 50_000,
            payload=control_payload,
        )
        if should_drop:
            add_icpc_record(
                request_id=request_id,
                sample_index=sample_index,
                direction="linux_to_zephyr",
                attempt=1,
                disposition="forwarded",
                message_type="control",
                flags=3,
                sequence=control_sequence,
                ack_sequence=0,
                timestamp_ms=sample_index * 100 + 100,
                monotonic_ns=base + 60_000,
                payload=control_payload,
            )
        ack_sequence = request_id * 10 + 3
        add_icpc_record(
            request_id=request_id,
            sample_index=sample_index,
            direction="zephyr_to_linux",
            attempt=0,
            disposition="forwarded",
            message_type="ack",
            flags=0,
            sequence=ack_sequence,
            ack_sequence=control_sequence,
            timestamp_ms=sample_index * 100 + 1,
            monotonic_ns=base + 70_000,
            payload=b"",
        )
        status_payload = (
            bytes((1, 0))
            + (0).to_bytes(2, "big")
            + request_id.to_bytes(4, "big")
            + sample_index.to_bytes(8, "big")
            + temperature_mC.to_bytes(4, "big", signed=True)
            + (55_000).to_bytes(4, "big", signed=True)
            + duty_q16_16.to_bytes(4, "big", signed=True)
            + (55_000 - temperature_mC).to_bytes(4, "big", signed=True)
        )
        add_icpc_record(
            request_id=request_id,
            sample_index=sample_index,
            direction="zephyr_to_linux",
            attempt=0,
            disposition="forwarded",
            message_type="status",
            flags=0,
            sequence=request_id * 10 + 4,
            ack_sequence=0,
            timestamp_ms=sample_index * 100 + 2,
            monotonic_ns=base + 80_000,
            payload=status_payload,
        )

    metrics_dir = root / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    with (metrics_dir / "trajectory.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(trajectory[0]))
        writer.writeheader()
        writer.writerows(trajectory)
    write_jsonl(metrics_dir / "linux-events.jsonl", linux_events)
    write_jsonl(metrics_dir / "zephyr-events.jsonl", zephyr_events)
    summary = compute_metrics.compute_metrics(
        trajectory,
        expected_ticks=1800,
        recovery_start_tick=900,
        overshoot_step_mC=30_000,
    )
    write_json(metrics_dir / "summary.json", summary)
    write_text(metrics_dir / "recompute.txt", "P5_METRICS_PASS\n")

    write_text(
        root / "logs" / "linux.raw.log",
        "AXVISOR_DUAL_GUEST_LINUX_APP_READY iface=eth0\n"
        "TGOS_LINUX_TRAJ_DONE ticks=1800 mode=fixed seed=7 acked=1800 feedback=1800\n",
    )
    write_text(
        root / "logs" / "zephyr.raw.log",
        "AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 iface=eth0\n",
    )
    write_text(
        root / "logs" / "axvisor.raw.log",
        "virtio-net TX from vm=0 len=64 dst=02:00:00:00:00:02 src=02:00:00:00:00:01\n"
        "virtio-net RX delivered=1 to mac=02:00:00:00:00:02, pulsing IRQ\n",
    )
    make_pcap(root / "network" / "capture.pcap", packets)
    write_jsonl(root / "network" / "frames.jsonl", frames)
    write_jsonl(root / "network" / "icpc.jsonl", icpc_records)
    write_text(root / "figures" / "temperature.svg", "<svg xmlns=\"http://www.w3.org/2000/svg\"/>\n")
    write_text(root / "figures" / "duty.svg", "<svg xmlns=\"http://www.w3.org/2000/svg\"/>\n")
    write_jsonl(
        root / "commands.jsonl",
        [{
            "argv": ["contract-fixture"],
            "cwd": str(ROOT),
            "started_monotonic_ns": 1,
            "finished_monotonic_ns": 2,
            "exit_code": 0,
        }],
    )
    write_json(
        root / "cleanup.json",
        {"residualProcesses": [], "residualSockets": [], "residualFiles": []},
    )

    excluded = {"session.json", "manifest.json", "status.json"}
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        files[relative] = {
            "size": path.stat().st_size,
            "sha256": sha256(path),
            "producer": "host-contract-fixture",
        }
    write_json(
        root / "manifest.json",
        {"schema_version": "p5-ai-manifest-v1", "run_id": run_id, "files": files},
    )
    manifest_sha256 = sha256(root / "manifest.json")
    write_json(
        root / "session.json",
        {
            "schema_version": "p5-ai-session-v1",
            "run_id": run_id,
            "session_id": 17,
            "scenario": "test-017",
            "controller": "fixed",
            "seed": 7,
            "profile_id": "qualification-v1",
            "execution_kind": "host_contract",
            "evidence_level": "L2 host contract",
            "manifest_sha256": manifest_sha256,
        },
    )
    write_json(
        root / "status.json",
        {
            "schema_version": "p5-ai-status-v1",
            "success": True,
            "status": "host_contract_valid",
            "primaryError": None,
            "cleanupError": None,
            "completedChecks": ["oracle", "hashes", "validator", "cleanup"],
            "manifestSha256": manifest_sha256,
            "statusLast": True,
        },
    )


def expect_reject(
    root: Path, contains: str, *, qualification: bool = False
) -> None:
    try:
        validator.validate_bundle(
            root,
            PROFILE_PATH,
            controller="fixed",
            seed=7,
            qualification=qualification,
        )
    except (ValueError, OSError) as error:
        assert contains in str(error), str(error)
    else:
        raise AssertionError(f"validator accepted invalid fixture: {contains}")


def main() -> int:
    root = ROOT / "target" / "contract-tests" / f"p5-bundle-{secrets.token_hex(8)}"
    build_fixture(root)
    result = validator.validate_bundle(
        root,
        PROFILE_PATH,
        controller="fixed",
        seed=7,
        qualification=False,
    )
    assert (
        result["valid"] is True
        and result["ticks"] == 1800
        and result["status"] == "host_contract_valid"
        and result["evidence_level"] == "L2 host contract"
    ), result
    print("  [PASS] coherent host-only bundle fixture")
    (root / "unindexed.txt").write_text("drift\n", encoding="utf-8")
    expect_reject(root, "bundle file set mismatch", qualification=False)
    (root / "unindexed.txt").unlink()
    print("  [FAIL-CLOSED] unindexed bundle artifact is rejected")
    expect_reject(root, "session execution_kind mismatch", qualification=True)
    print("  [FAIL-CLOSED] host fixture cannot publish qualification success")

    icpc_records = [
        json.loads(line)
        for line in (root / "network" / "icpc.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    raw_attempts = [
        {
            "schema_version": "p5-ai-icpc-attempt-v1",
            "run_id": row["run_id"],
            "session_id": row["session_id"],
            "direction": row["direction"],
            "attempt": row["attempt"],
            "fault_disposition": row["fault_disposition"],
            "timestamp_ms": row["timestamp_ms"],
            "monotonic_ns": row["monotonic_ns"],
            "wire_hex": row["wire_hex"],
        }
        for row in icpc_records
    ]
    attempts_path = root / "network" / "icpc-attempts.jsonl"
    write_jsonl(attempts_path, raw_attempts)
    fault_manifest_path = root / "configs" / "fault-manifest.json"
    fault_manifest = json.loads(fault_manifest_path.read_text(encoding="utf-8"))
    transcript = produce_icpc_records.build_injection_transcript(
        icpc_records,
        run_id="phase5-ai-fixed-s7-validator-contract",
        session_id=17,
        raw_attempts=attempts_path.read_bytes(),
        icpc_records=(root / "network" / "icpc.jsonl").read_bytes(),
        fault_manifest_bytes=fault_manifest_path.read_bytes(),
        fault_manifest=fault_manifest,
    )
    transcript_path = root / "network" / "injection-transcript.json"
    write_json(transcript_path, transcript)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for path in (attempts_path, transcript_path):
        relative = path.relative_to(root).as_posix()
        manifest["files"][relative] = {
            "size": path.stat().st_size,
            "sha256": sha256(path),
            "producer": "host-contract-fixture",
        }
    write_json(manifest_path, manifest)
    manifest_sha256 = sha256(manifest_path)

    session_path = root / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["execution_kind"] = "guest_runtime"
    session["evidence_level"] = "L8 AI-loop"
    session["manifest_sha256"] = manifest_sha256
    write_json(session_path, session)
    status_path = root / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["status"] = "ai_control_completed"
    status["manifestSha256"] = manifest_sha256
    write_json(status_path, status)
    guest_result = validator.validate_bundle(
        root,
        PROFILE_PATH,
        controller="fixed",
        seed=7,
        qualification=True,
    )
    assert (
        guest_result["valid"] is True
        and guest_result["status"] == "ai_control_completed"
        and guest_result["evidence_level"] == "L8 AI-loop"
    ), guest_result
    print("  [PASS] qualification bundle independently consumes injection transcript")
    transcript_path = root / "network" / "injection-transcript.json"
    valid_transcript_text = transcript_path.read_text(encoding="utf-8")
    tampered_transcript = json.loads(valid_transcript_text)
    tampered_transcript["counts"]["attempts"] += 1
    write_json(transcript_path, tampered_transcript)
    expect_reject(root, "does not equal independent recomputation", qualification=True)
    print("  [FAIL-CLOSED] tampered injection transcript is rejected")
    write_text(transcript_path, valid_transcript_text)
    session["execution_kind"] = "host_contract"
    session["evidence_level"] = "L2 host contract"
    write_json(session_path, session)
    status["status"] = "host_contract_valid"
    status["primaryError"] = None
    status["cleanupError"] = None
    write_json(status_path, status)

    pcap_path = root / "network" / "capture.pcap"
    frames_path = root / "network" / "frames.jsonl"
    valid_pcap_bytes = pcap_path.read_bytes()
    valid_frames_text = frames_path.read_text(encoding="utf-8")
    valid_packets = validator._validate_pcap(pcap_path)
    valid_frames = [json.loads(line) for line in valid_frames_text.splitlines()]

    def publish_network_variant(
        packets: list[bytes], frames: list[dict[str, Any]]
    ) -> None:
        make_pcap(pcap_path, packets)
        write_jsonl(frames_path, frames)

    def replace_first_packet(packet: bytes) -> None:
        frames = [dict(row) for row in valid_frames]
        frames[0].update(
            length=len(packet),
            sha256=hashlib.sha256(packet).hexdigest(),
            frame_hex=packet.hex(),
        )
        publish_network_variant([packet, *valid_packets[1:]], frames)

    wrong_ether_type = bytearray(valid_packets[0])
    wrong_ether_type[12:14] = b"\x08\x06"
    replace_first_packet(bytes(wrong_ether_type))
    expect_reject(root, "contains ICPC bytes outside IPv4")
    print("  [FAIL-CLOSED] ICPC bytes under a false EtherType are rejected")

    wrong_port = bytearray(valid_packets[0])
    wrong_port[14 + 20 : 14 + 20 + 2] = (46001).to_bytes(2, "big")
    replace_first_packet(bytes(wrong_port))
    expect_reject(root, "ICPC endpoint identity mismatch")
    print("  [FAIL-CLOSED] ICPC on a non-frozen UDP port is rejected")

    bad_udp_checksum = bytearray(valid_packets[0])
    bad_udp_checksum[14 + 20 + 6] ^= 0x01
    replace_first_packet(bytes(bad_udp_checksum))
    expect_reject(root, "UDP checksum mismatch")
    print("  [FAIL-CLOSED] invalid UDP checksum is rejected")

    extra_frame = dict(valid_frames[0])
    extra_frame.update(
        sequence=len(valid_frames),
        monotonic_ns=int(valid_frames[-1]["monotonic_ns"]) + 1,
    )
    publish_network_variant([*valid_packets, valid_packets[0]], [*valid_frames, extra_frame])
    expect_reject(root, "missing or not indexed exactly once")
    print("  [FAIL-CLOSED] unindexed extra ICPC frame is rejected")

    pcap_path.write_bytes(valid_pcap_bytes)
    write_text(frames_path, valid_frames_text)

    icpc_path = root / "network" / "icpc.jsonl"
    valid_icpc_text = icpc_path.read_text(encoding="utf-8")
    icpc_rows = [json.loads(line) for line in valid_icpc_text.splitlines()]
    icpc_rows[0]["fault_disposition"] = "profile_drop"
    icpc_rows[0]["frame_sequence"] = None
    write_jsonl(icpc_path, icpc_rows)
    expect_reject(root, "missing or not indexed exactly once")
    print("  [FAIL-CLOSED] unmanifested CONTROL drop is rejected")
    write_text(icpc_path, valid_icpc_text)

    summary_path = root / "metrics" / "summary.json"
    valid_summary_text = summary_path.read_text(encoding="utf-8")
    summary = json.loads(valid_summary_text)
    summary["groups"][0]["sample_count"] = 600
    write_json(summary_path, summary)
    expect_reject(root, "summary.json")
    print("  [FAIL-CLOSED] recomputed summary rejects tampering")

    # Restore the byte-identical summary so status validation is exercised as
    # an independent gate (status is intentionally outside its own manifest).
    write_text(summary_path, valid_summary_text)

    config_path = root / "configs" / "linux-app-config.json"
    valid_config_text = config_path.read_text(encoding="utf-8")
    write_text(config_path, valid_config_text + "\n")
    expect_reject(root, "manifest")
    print("  [FAIL-CLOSED] manifest rejects byte-level artifact tampering")
    write_text(config_path, valid_config_text)

    status_path = root / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["primaryError"] = "hidden failure"
    write_json(status_path, status)
    expect_reject(root, "successful status contains an error")
    print("  [FAIL-CLOSED] status cannot hide primaryError")
    print("P5_AI_BUNDLE_VALIDATOR_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, ValueError) as error:
        print(f"P5 AI bundle validator contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
