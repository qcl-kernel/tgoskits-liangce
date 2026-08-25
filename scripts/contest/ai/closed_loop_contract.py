#!/usr/bin/env python3
"""Frozen, fail-closed contracts shared by TEST-017 producers and validators."""

from __future__ import annotations

import hashlib
import functools
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:  # Support both direct script execution and package imports in tests/tools.
    from .reference import FixedPiController, Pcg32, plant_step
except ImportError:  # pragma: no cover - exercised by direct CLI entry points.
    from reference import FixedPiController, Pcg32, plant_step


PROFILE_SCHEMA = "p5-ai-qualification-v1"
CONTROLLERS = ["fixed", "mlp"]
SEEDS = [7, 19, 43]
TICK_PERIOD_MS = 100
TICKS = 1800
TARGET_MC = 55_000
DISTURBANCE_START_TICK = 600
DISTURBANCE_END_TICK = 899
SETTLING_HOLD_TICKS = 50
ERROR_BAND_MC = 1_000
MODEL_VERSION = 731_924_617
MODEL_SHA256 = "2ba04889fddb129be4fb42c76c595513896ba15b3aee3b65b4d6ff3dca46d50f"
MODEL_METADATA_SHA256 = "c28c61bb954221c1e48bba6ca94339d081678905241151cf05b0256034d79938"
GOLDEN_VECTORS_SHA256 = "7889009fcc62e92967dbf975113704f7c07a9a683cb8d29809999d30e19fb86b"
DATASET_MANIFEST_SHA256 = "a9957815013e3382123621e34e66626b2d10d1d47ea8b68ac1deca6b1d751aa5"
ICPC_RECORD_SCHEMA = "p5-ai-icpc-record-v1"
ICPC_HEADER_SIZE = 36
ICPC_FLAG_ACK_REQUIRED = 0x01
ICPC_FLAG_RETRANSMISSION = 0x02
ICPC_MESSAGE_CONTROL = 1
ICPC_MESSAGE_STATUS = 2
ICPC_MESSAGE_ACK = 4

_ICPC_RECORD_FIELDS = {
    "schema_version",
    "record_index",
    "run_id",
    "session_id",
    "frame_sequence",
    "direction",
    "attempt",
    "fault_disposition",
    "message_type",
    "flags",
    "sequence",
    "ack_sequence",
    "request_id",
    "sample_index",
    "timestamp_ms",
    "monotonic_ns",
    "payload_length",
    "payload_sha256",
    "payload_hex",
    "wire_length",
    "wire_sha256",
    "wire_hex",
}

_COMPLETION = re.compile(
    r"^TGOS_LINUX_TRAJ_DONE ticks=(?P<ticks>\d+) "
    r"mode=(?P<mode>fixed|mlp) seed=(?P<seed>\d+) "
    r"acked=(?P<acked>\d+) feedback=(?P<feedback>\d+)$"
)

_NUMERIC_FIELDS = (
    "sample_index",
    "measured_mC",
    "target_mC",
    "duty_q16_16",
    "model_version",
    "health_flags",
    "applied_request_id",
    "feedback_confirmed",
    "closed_loop_rtt_ns",
    "action_latency_ns",
)

_EVENT_FIELDS = (
    "schema_version",
    "run_id",
    "endpoint",
    "scenario",
    "transport",
    "session_id",
    "sequence",
    "request_id",
    "sample_index",
    "event",
    "monotonic_ns",
    "value",
    "unit",
    "outcome",
)

_LINUX_COMMON_EVENTS = {
    "period_release",
    "period_start",
    "input_receive",
    "packet_send",
    "ack_receive",
    "feedback_receive",
    "period_finish",
}

_LINUX_MLP_EVENTS = {"inference_start", "inference_finish"}

_ZEPHYR_REQUEST_EVENTS = {
    "period_release",
    "period_start",
    "packet_receive",
    "ack_send",
    "control_apply",
    "packet_send",
    "period_finish",
}

_LOCAL_ONLY_EVENTS = {
    "period_release",
    "period_start",
    "period_finish",
    "inference_start",
    "inference_finish",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_qualification_profile(path: Path) -> dict[str, Any]:
    """Load the canonical P5 profile and reject silent contract drift."""

    document = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(document, dict), "qualification profile must be an object")
    _require(document.get("schema_version") == PROFILE_SCHEMA, "invalid P5 profile schema")
    _require(document.get("profile_id") == "qualification-v1", "invalid profile_id")
    _require(document.get("test_id") == "TEST-017", "profile is not TEST-017")
    _require(document.get("controllers") == CONTROLLERS, "controllers must be fixed,mlp")
    _require(document.get("seeds") == SEEDS, "seeds must be 7,19,43")

    timing = document.get("timing")
    _require(isinstance(timing, dict), "profile timing must be an object")
    _require(timing.get("tick_period_ms") == TICK_PERIOD_MS, "tick period must be 100 ms")
    _require(timing.get("ticks") == TICKS, "qualification must contain 1800 ticks")
    _require(timing.get("duration_seconds") == 180, "qualification duration must be 180 s")
    _require(timing.get("control_validity_ms") == 500, "CONTROL validity must be 500 ms")
    _require(timing.get("udp_retry_ms") == [0, 100, 300], "UDP retry schedule drift")
    _require(timing.get("udp_cancel_ms") == 500, "UDP cancel deadline must be 500 ms")

    plant = document.get("plant")
    _require(isinstance(plant, dict), "profile plant must be an object")
    expected_plant = {
        "initial_temperature_mC": 25_000,
        "ambient_temperature_mC": 25_000,
        "target_mC": TARGET_MC,
        "heater_rate_mC_per_second": 4_000,
        "heat_loss_divisor": 100,
        "disturbance_start_tick": DISTURBANCE_START_TICK,
        "disturbance_end_tick": DISTURBANCE_END_TICK,
        "disturbance_mC_per_tick": -150,
    }
    _require(plant == expected_plant, "frozen plant parameters drifted")

    metrics = document.get("metrics")
    _require(isinstance(metrics, dict), "profile metrics must be an object")
    _require(metrics.get("error_band_mC") == ERROR_BAND_MC, "error band must be 1000 mC")
    _require(
        metrics.get("settling_hold_ticks") == SETTLING_HOLD_TICKS,
        "settling hold must be 50 ticks",
    )
    _require(metrics.get("recovery_start_tick") == 900, "recovery must start at tick 900")
    _require(metrics.get("overshoot_step_mC") == 30_000, "overshoot step must be 30000 mC")
    _require(
        metrics.get("require_feedback_for_every_request") is True,
        "every logical request must have feedback",
    )

    fault_stream = document.get("fault_stream")
    expected_fault_stream = {
        "algorithm": "pcg-xsh-rr-64-32",
        "discard_first_output": True,
        "outputs_per_request": 1,
        "first_control_drop_modulus": 100,
        "first_control_drop_residue": 0,
        "drop_attempt_index": 0,
        "drop_ack": False,
        "drop_status": False,
        "duplicate": False,
        "reorder": False,
        "scheduling_jitter_us": 0,
        "paired_manifest_required": True,
    }
    _require(fault_stream == expected_fault_stream, "qualification fault stream drifted")
    fault_golden = document.get("fault_golden")
    _require(isinstance(fault_golden, dict), "fault_golden must be an object")
    _require(set(fault_golden) == {"7", "19", "43"}, "fault golden seeds drifted")
    for seed_key in ("7", "19", "43"):
        golden = fault_golden[seed_key]
        _require(isinstance(golden, dict), f"fault golden {seed_key} is invalid")
        _require(
            isinstance(golden.get("outputs_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", golden["outputs_sha256"]) is not None,
            f"fault golden {seed_key} output hash is invalid",
        )
        indices = golden.get("drop_first_control_request_indices")
        _require(
            isinstance(indices, list)
            and indices == sorted(set(indices))
            and all(isinstance(index, int) and 0 <= index < TICKS for index in indices),
            f"fault golden {seed_key} indices are invalid",
        )

    model = document.get("model")
    _require(isinstance(model, dict), "profile model must be an object")
    _require(model.get("schema_version") == "p5-mlp-model-v1", "model schema drift")
    _require(model.get("model_version") == MODEL_VERSION, "model_version drift")
    _require(model.get("model_sha256") == MODEL_SHA256, "model SHA-256 drift")
    _require(
        model.get("metadata_sha256") == MODEL_METADATA_SHA256,
        "model metadata SHA-256 drift",
    )
    _require(
        model.get("golden_vectors_sha256") == GOLDEN_VECTORS_SHA256,
        "golden vectors SHA-256 drift",
    )
    _require(
        model.get("dataset_manifest_sha256") == DATASET_MANIFEST_SHA256,
        "dataset manifest SHA-256 drift",
    )
    _require(model.get("golden_vector_count") == 256, "golden vector count drift")

    evidence = document.get("evidence")
    _require(isinstance(evidence, dict), "profile evidence must be an object")
    _require(evidence.get("level") == "L8 AI-loop", "TEST-017 must use L8 evidence")
    _require(evidence.get("success_status") == "ai_control_completed", "success token drift")
    _require(evidence.get("failed_status") == "ai_control_failed", "failure token drift")
    _require(evidence.get("blocked_status") == "ai_control_blocked", "blocked token drift")
    return document


def generate_fault_manifest(*, profile: Mapping[str, Any], seed: int) -> dict[str, Any]:
    """Expand one paired seed into the frozen first-CONTROL-drop manifest."""

    validate_run_identity(profile=profile, controller="fixed", seed=seed)
    stream = profile["fault_stream"]
    generator = Pcg32(seed)
    if stream["discard_first_output"]:
        generator.next_u32()
    dropped: list[int] = []
    outputs: list[int] = []
    ticks = int(profile["timing"]["ticks"])
    modulus = int(stream["first_control_drop_modulus"])
    residue = int(stream["first_control_drop_residue"])
    for request_index in range(ticks):
        value = generator.next_u32()
        outputs.append(value)
        if value % modulus == residue:
            dropped.append(request_index)
    manifest = {
        "schema_version": "p5-ai-fault-manifest-v1",
        "profile_id": profile["profile_id"],
        "seed": seed,
        "request_count": ticks,
        "outputs_sha256": _u32_stream_sha256(outputs),
        "drop_attempt_index": int(stream["drop_attempt_index"]),
        "drop_first_control_count": len(dropped),
        "drop_first_control_request_indices": dropped,
        "drop_ack": False,
        "drop_status": False,
        "duplicate": False,
        "reorder": False,
        "scheduling_jitter_us": 0,
    }
    golden = profile["fault_golden"][str(seed)]
    _require(
        manifest["outputs_sha256"] == golden["outputs_sha256"],
        f"PCG output stream drifted for seed {seed}",
    )
    _require(
        manifest["drop_first_control_request_indices"]
        == golden["drop_first_control_request_indices"],
        f"PCG drop indices drifted for seed {seed}",
    )
    return manifest


def _u32_stream_sha256(values: Iterable[int]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(int(value).to_bytes(4, byteorder="big", signed=False))
    return digest.hexdigest()


def _crc32c(data: bytes) -> int:
    """Return the ICPC v1 Castagnoli CRC used by the portable C codec."""

    crc = 0xFFFF_FFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F6_3B78 if crc & 1 else 0)
    return (~crc) & 0xFFFF_FFFF


@functools.lru_cache(maxsize=None)
def _decode_icpc_wire(wire: bytes) -> dict[str, Any]:
    """Decode and fail-close the frozen ICPC v1 header and control payloads."""

    _require(len(wire) >= ICPC_HEADER_SIZE, "ICPC wire record is shorter than 36 bytes")
    _require(wire[:4] == b"ICPC", "ICPC wire magic mismatch")
    _require(wire[4] == 1, "ICPC wire version mismatch")
    _require(wire[5] == ICPC_HEADER_SIZE, "ICPC wire header length mismatch")
    message_type = wire[6]
    flags = wire[7]
    session_id = int.from_bytes(wire[8:12], "big")
    sequence = int.from_bytes(wire[12:16], "big")
    ack_sequence = int.from_bytes(wire[16:20], "big")
    timestamp_ms = int.from_bytes(wire[20:28], "big")
    payload_length = int.from_bytes(wire[28:30], "big")
    error_code = int.from_bytes(wire[30:32], "big")
    checksum = int.from_bytes(wire[32:36], "big")
    _require(len(wire) == ICPC_HEADER_SIZE + payload_length, "ICPC payload length mismatch")
    _require(flags & ~(ICPC_FLAG_ACK_REQUIRED | ICPC_FLAG_RETRANSMISSION) == 0, "ICPC flags are invalid")
    _require(session_id > 0 and sequence > 0, "ICPC session/sequence must be nonzero")
    checksum_input = bytearray(wire)
    checksum_input[32:36] = b"\0\0\0\0"
    _require(_crc32c(bytes(checksum_input)) == checksum, "ICPC CRC32C mismatch")
    payload = wire[ICPC_HEADER_SIZE:]

    if message_type == ICPC_MESSAGE_CONTROL:
        _require(ack_sequence == 0 and error_code == 0, "CONTROL header fields are invalid")
        _require(flags & ICPC_FLAG_ACK_REQUIRED != 0, "CONTROL must require ACK")
        _require(payload_length == 24, "CONTROL payload length must be 24")
        _require(payload[0] == 1 and payload[1] == 1, "CONTROL schema/command mismatch")
        _require(payload[2] in (0, 1), "CONTROL mode is invalid")
        _require(payload[3] == 0 and payload[22:24] == b"\0\0", "CONTROL reserved bytes are nonzero")
        decoded_payload = {
            "control_mode": payload[2],
            "request_id": int.from_bytes(payload[4:8], "big"),
            "duty_q16_16": int.from_bytes(payload[8:12], "big", signed=True),
            "target_mC": int.from_bytes(payload[12:16], "big", signed=True),
            "model_version": int.from_bytes(payload[16:20], "big"),
            "validity_ms": int.from_bytes(payload[20:22], "big"),
        }
    elif message_type == ICPC_MESSAGE_ACK:
        _require(flags == 0 and ack_sequence > 0 and error_code == 0, "ACK header fields are invalid")
        _require(payload_length == 0, "ACK payload must be empty")
        decoded_payload = {}
    elif message_type == ICPC_MESSAGE_STATUS:
        _require(flags == 0 and ack_sequence == 0 and error_code == 0, "STATUS header fields are invalid")
        _require(payload_length == 32, "STATUS payload length must be 32")
        _require(payload[0] == 1 and payload[1] in (0, 1), "STATUS schema/mode mismatch")
        decoded_payload = {
            "control_mode": payload[1],
            "health_flags": int.from_bytes(payload[2:4], "big"),
            "request_id": int.from_bytes(payload[4:8], "big"),
            "sample_index": int.from_bytes(payload[8:16], "big"),
            "measured_mC": int.from_bytes(payload[16:20], "big", signed=True),
            "target_mC": int.from_bytes(payload[20:24], "big", signed=True),
            "duty_q16_16": int.from_bytes(payload[24:28], "big", signed=True),
            "control_error_mC": int.from_bytes(payload[28:32], "big", signed=True),
        }
    else:
        raise ValueError(f"TEST-017 ICPC message type {message_type} is not CONTROL/ACK/STATUS")

    return {
        "message_type": message_type,
        "flags": flags,
        "session_id": session_id,
        "sequence": sequence,
        "ack_sequence": ack_sequence,
        "timestamp_ms": timestamp_ms,
        "payload": payload,
        "payload_length": payload_length,
        **decoded_payload,
    }


def validate_icpc_records(
    records: Iterable[Mapping[str, Any]],
    frames: Iterable[Mapping[str, Any]],
    *,
    trajectory: Iterable[Mapping[str, Any]],
    linux_events: Iterable[Mapping[str, Any]],
    zephyr_events: Iterable[Mapping[str, Any]],
    profile: Mapping[str, Any],
    fault_manifest: Mapping[str, Any],
    run_id: str,
    session_id: int,
    controller: str,
) -> None:
    """Bind every TEST-017 attempt to ICPC bytes, capture, faults and events.

    A profile-dropped first CONTROL has no PCAP frame but still carries the
    exact pre-injection wire bytes. Every forwarded CONTROL/ACK/STATUS must
    link to one unique parsed Ethernet/IPv4/UDP frame whose payload is exactly
    those ICPC bytes. Every captured ICPC frame must be referenced once.
    """

    _require(controller in CONTROLLERS, "ICPC controller is invalid")
    rows = [dict(row) for row in trajectory]
    _require(len(rows) == TICKS, "ICPC validation requires the full trajectory")
    trajectory_by_request = {int(row["applied_request_id"]): row for row in rows}
    _require(len(trajectory_by_request) == TICKS, "ICPC trajectory request IDs are not unique")
    dropped = set(fault_manifest.get("drop_first_control_request_indices", []))
    _require(
        dropped == set(profile["fault_golden"][str(int(rows[0]["seed"]))]["drop_first_control_request_indices"]),
        "ICPC fault manifest does not match the paired profile",
    )

    frame_by_sequence: dict[int, Mapping[str, Any]] = {}
    icpc_frame_sequences: set[int] = set()
    for frame in frames:
        sequence = frame.get("sequence")
        _require(isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0, "frame sequence is invalid")
        _require(sequence not in frame_by_sequence, "duplicate frame sequence")
        frame_by_sequence[sequence] = frame
        if frame.get("_icpc_wire_hex") is not None:
            icpc_frame_sequences.add(sequence)

    linux = [dict(event) for event in linux_events]
    zephyr = [dict(event) for event in zephyr_events]

    def index_events(
        endpoint_rows: list[dict[str, Any]],
    ) -> dict[tuple[int, str], list[dict[str, Any]]]:
        indexed: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for event in endpoint_rows:
            request_id = event.get("request_id")
            name = event.get("event")
            if isinstance(request_id, int) and isinstance(name, str):
                indexed.setdefault((request_id, name), []).append(event)
        return indexed

    linux_by_event = index_events(linux)
    zephyr_by_event = index_events(zephyr)

    def events(
        endpoint_index: Mapping[tuple[int, str], list[dict[str, Any]]],
        request_id: int,
        name: str,
    ) -> list[dict[str, Any]]:
        return endpoint_index.get((request_id, name), [])

    by_request: dict[int, dict[str, list[dict[str, Any]]]] = {}
    used_frames: set[int] = set()
    previous_monotonic_ns = {
        "linux_to_zephyr": -1,
        "zephyr_to_linux": -1,
    }
    names = {ICPC_MESSAGE_CONTROL: "control", ICPC_MESSAGE_ACK: "ack", ICPC_MESSAGE_STATUS: "status"}
    materialized = [dict(record) for record in records]
    _require(bool(materialized), "network/icpc.jsonl must not be empty")
    for expected_index, record in enumerate(materialized, 1):
        _require(set(record) == _ICPC_RECORD_FIELDS, f"ICPC record {expected_index} field set mismatch")
        _require(record["schema_version"] == ICPC_RECORD_SCHEMA, f"ICPC record {expected_index} schema mismatch")
        _require(record["record_index"] == expected_index, f"ICPC record {expected_index} index mismatch")
        _require(record["run_id"] == run_id and record["session_id"] == session_id, f"ICPC record {expected_index} identity mismatch")
        monotonic_ns = record["monotonic_ns"]
        direction = record["direction"]
        _require(direction in previous_monotonic_ns, f"ICPC record {expected_index} direction invalid")
        _require(isinstance(monotonic_ns, int) and not isinstance(monotonic_ns, bool) and monotonic_ns >= previous_monotonic_ns[direction], f"ICPC record {expected_index} {direction} time moved backwards")
        previous_monotonic_ns[direction] = monotonic_ns
        try:
            wire = bytes.fromhex(record["wire_hex"])
            payload_bytes = bytes.fromhex(record["payload_hex"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"ICPC record {expected_index} has invalid hex") from error
        decoded = _decode_icpc_wire(wire)
        payload = decoded["payload"]
        _require(payload_bytes == payload, f"ICPC record {expected_index} payload bytes mismatch")
        _require(record["wire_length"] == len(wire) and record["wire_sha256"] == hashlib.sha256(wire).hexdigest(), f"ICPC record {expected_index} wire length/hash mismatch")
        _require(record["payload_length"] == len(payload) and record["payload_sha256"] == hashlib.sha256(payload).hexdigest(), f"ICPC record {expected_index} payload length/hash mismatch")
        for field in ("session_id", "flags", "sequence", "ack_sequence", "timestamp_ms"):
            _require(record[field] == decoded[field], f"ICPC record {expected_index} {field} mismatch")
        message_name = names[decoded["message_type"]]
        _require(record["message_type"] == message_name, f"ICPC record {expected_index} message_type mismatch")
        request_id = record["request_id"]
        sample_index = record["sample_index"]
        attempt = record["attempt"]
        _require(isinstance(request_id, int) and not isinstance(request_id, bool) and request_id in trajectory_by_request, f"ICPC record {expected_index} request_id invalid")
        _require(sample_index == request_id - 1, f"ICPC record {expected_index} sample association mismatch")
        _require(isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 0, f"ICPC record {expected_index} attempt invalid")
        disposition = record["fault_disposition"]
        _require(disposition in ("forwarded", "profile_drop"), f"ICPC record {expected_index} disposition invalid")
        direction = record["direction"]
        _require(direction in ("linux_to_zephyr", "zephyr_to_linux"), f"ICPC record {expected_index} direction invalid")

        if disposition == "forwarded":
            frame_sequence = record["frame_sequence"]
            _require(isinstance(frame_sequence, int) and not isinstance(frame_sequence, bool) and frame_sequence >= 0, f"ICPC record {expected_index} has no frame link")
            _require(frame_sequence not in used_frames, f"ICPC record {expected_index} reuses a frame")
            frame = frame_by_sequence.get(frame_sequence)
            _require(frame is not None, f"ICPC record {expected_index} frame link is missing")
            assert frame is not None
            _require(frame.get("run_id") == run_id and frame.get("session_id") == str(session_id), f"ICPC record {expected_index} frame identity mismatch")
            expected_port = 0 if direction == "linux_to_zephyr" else 1
            _require(frame.get("ingress_port") == expected_port, f"ICPC record {expected_index} frame port mismatch")
            _require(
                frame.get("_icpc_wire_hex") == wire.hex(),
                f"ICPC record {expected_index} is not the exact linked UDP payload",
            )
            used_frames.add(frame_sequence)
        else:
            _require(record["frame_sequence"] is None, f"ICPC record {expected_index} dropped attempt has a frame")
            _require(message_name == "control" and attempt == 0, f"ICPC record {expected_index} only first CONTROL may be profile-dropped")

        bucket = by_request.setdefault(request_id, {"control": [], "ack": [], "status": []})
        bucket[message_name].append({**record, "decoded": decoded})

    _require(
        used_frames == icpc_frame_sequences,
        "captured ICPC frames are missing or not indexed exactly once",
    )

    expected_mode = 0 if controller == "fixed" else 1
    previous_control_sequence = 0
    previous_ack_sequence = 0
    previous_status_sequence = 0
    for request_id in range(1, TICKS + 1):
        sample_index = request_id - 1
        row = trajectory_by_request[request_id]
        bucket = by_request.get(request_id)
        _require(bucket is not None, f"missing ICPC records for request {request_id}")
        assert bucket is not None
        controls = sorted(bucket["control"], key=lambda item: item["attempt"])
        should_drop = sample_index in dropped
        expected_attempts = [0, 1] if should_drop else [0]
        _require([item["attempt"] for item in controls] == expected_attempts, f"CONTROL attempts mismatch for request {request_id}")
        expected_dispositions = ["profile_drop", "forwarded"] if should_drop else ["forwarded"]
        _require([item["fault_disposition"] for item in controls] == expected_dispositions, f"CONTROL fault dispositions mismatch for request {request_id}")
        control_sequences = {item["sequence"] for item in controls}
        _require(len(control_sequences) == 1, f"CONTROL retry changed sequence for request {request_id}")
        control_sequence = next(iter(control_sequences))
        _require(control_sequence > previous_control_sequence, f"CONTROL sequence did not advance at request {request_id}")
        previous_control_sequence = control_sequence

        for item in controls:
            decoded = item["decoded"]
            expected_flags = ICPC_FLAG_ACK_REQUIRED | (ICPC_FLAG_RETRANSMISSION if item["attempt"] else 0)
            _require(decoded["flags"] == expected_flags, f"CONTROL retry flags mismatch for request {request_id}")
            _require(item["direction"] == "linux_to_zephyr", f"CONTROL direction mismatch for request {request_id}")
            _require(decoded["request_id"] == request_id, f"CONTROL payload request mismatch for request {request_id}")
            _require(decoded["control_mode"] == expected_mode, f"CONTROL mode mismatch for request {request_id}")
            _require(decoded["duty_q16_16"] == int(row["duty_q16_16"]), f"CONTROL duty mismatch for request {request_id}")
            _require(decoded["target_mC"] == int(row["target_mC"]), f"CONTROL target mismatch for request {request_id}")
            _require(decoded["model_version"] == int(row["model_version"]), f"CONTROL model mismatch for request {request_id}")
            _require(decoded["validity_ms"] == int(profile["timing"]["control_validity_ms"]), f"CONTROL validity mismatch for request {request_id}")
        _require(len(bucket["ack"]) == 1, f"request {request_id} requires exactly one ACK")
        ack = bucket["ack"][0]
        _require(ack["attempt"] == 0 and ack["fault_disposition"] == "forwarded" and ack["direction"] == "zephyr_to_linux", f"ACK disposition mismatch for request {request_id}")
        _require(ack["decoded"]["ack_sequence"] == control_sequence, f"ACK does not acknowledge CONTROL for request {request_id}")
        _require(ack["sequence"] > previous_ack_sequence, f"ACK sequence did not advance at request {request_id}")
        previous_ack_sequence = ack["sequence"]
        _require(len(bucket["status"]) == 1, f"request {request_id} requires exactly one STATUS")
        status = bucket["status"][0]
        decoded_status = status["decoded"]
        _require(status["attempt"] == 0 and status["fault_disposition"] == "forwarded" and status["direction"] == "zephyr_to_linux", f"STATUS disposition mismatch for request {request_id}")
        _require(status["sequence"] > previous_status_sequence, f"STATUS sequence did not advance at request {request_id}")
        previous_status_sequence = status["sequence"]
        expected_status = {
            "control_mode": expected_mode,
            "health_flags": int(row["health_flags"]),
            "request_id": request_id,
            "sample_index": sample_index,
            "measured_mC": int(row["measured_mC"]),
            "target_mC": int(row["target_mC"]),
            "duty_q16_16": int(row["duty_q16_16"]),
            "control_error_mC": int(row["target_mC"]) - int(row["measured_mC"]),
        }
        _require(all(decoded_status[key] == value for key, value in expected_status.items()), f"STATUS payload mismatch for request {request_id}")

        linux_sends = sorted(events(linux_by_event, request_id, "packet_send"), key=lambda item: int(item["monotonic_ns"]))
        expected_outcomes = ["fault_drop", "retry_sent"] if should_drop else ["sent"]
        _require([item.get("outcome") for item in linux_sends] == expected_outcomes, f"Linux CONTROL attempt events mismatch for request {request_id}")
        _require(all(int(item["sequence"]) == control_sequence for item in linux_sends), f"Linux CONTROL event sequence mismatch for request {request_id}")
        for endpoint_rows, name, sequence, value, label in (
            (zephyr, "packet_receive", control_sequence, int(row["duty_q16_16"]), "Zephyr CONTROL receive"),
            (zephyr, "control_apply", control_sequence, int(row["duty_q16_16"]), "Zephyr control apply"),
            (zephyr, "ack_send", ack["sequence"], control_sequence, "Zephyr ACK send"),
            (linux, "ack_receive", ack["sequence"], control_sequence, "Linux ACK receive"),
            (zephyr, "packet_send", status["sequence"], int(row["measured_mC"]), "Zephyr STATUS send"),
            (linux, "feedback_receive", status["sequence"], int(row["measured_mC"]), "Linux STATUS receive"),
        ):
            endpoint_index = (
                linux_by_event if endpoint_rows is linux else zephyr_by_event
            )
            matches = events(endpoint_index, request_id, name)
            _require(len(matches) == 1, f"{label} count mismatch for request {request_id}")
            _require(int(matches[0]["sequence"]) == sequence and int(matches[0]["value"]) == value, f"{label} byte/event association mismatch for request {request_id}")


def validate_run_identity(
    *, profile: Mapping[str, Any], controller: str, seed: int
) -> None:
    _require(controller in profile["controllers"], f"unsupported controller {controller!r}")
    _require(seed in profile["seeds"], f"unsupported seed {seed}")


def validate_trajectory(
    rows: Iterable[Mapping[str, Any]],
    *,
    profile: Mapping[str, Any],
    controller: str,
    seed: int,
    mlp_infer: Callable[[int, int, int], int] | None = None,
) -> list[dict[str, Any]]:
    """Validate one qualification trajectory before any metric is trusted."""

    validate_run_identity(profile=profile, controller=controller, seed=seed)
    materialized = [dict(row) for row in rows]
    expected_ticks = int(profile["timing"]["ticks"])
    _require(
        len(materialized) == expected_ticks,
        f"trajectory requires exactly {expected_ticks} rows, got {len(materialized)}",
    )

    checked: list[dict[str, Any]] = []
    for expected_index, row in enumerate(materialized):
        for field in ("controller", "seed", *_NUMERIC_FIELDS):
            _require(row.get(field) not in (None, ""), f"trajectory row is missing {field}")
        _require(row["controller"] == controller, "trajectory controller does not match run")
        try:
            row_seed = int(row["seed"])
            numeric = {field: int(row[field]) for field in _NUMERIC_FIELDS}
        except (TypeError, ValueError) as error:
            raise ValueError(f"trajectory row has a non-integer field: {error}") from error
        _require(row_seed == seed, "trajectory seed does not match run")
        _require(
            numeric["sample_index"] == expected_index,
            "trajectory must have continuous sample_index 0..1799",
        )
        _require(numeric["target_mC"] == TARGET_MC, "trajectory target drifted")
        _require(0 <= numeric["duty_q16_16"] <= 65_536, "trajectory duty out of range")
        _require(
            numeric["applied_request_id"] == expected_index + 1,
            "qualification request_id must be continuous 1..1800",
        )
        _require(numeric["feedback_confirmed"] == 1, "trajectory feedback is incomplete")
        _require(numeric["health_flags"] == 0, "qualification trajectory has health flags")
        expected_model_version = 0 if controller == "fixed" else MODEL_VERSION
        _require(
            numeric["model_version"] == expected_model_version,
            "trajectory model_version does not match controller",
        )
        _require(numeric["closed_loop_rtt_ns"] >= 0, "negative closed-loop RTT")
        _require(numeric["action_latency_ns"] >= 0, "negative action latency")
        checked.append({**row, **numeric, "seed": row_seed})

    # The Zephyr periodic task applies the duty, advances the frozen integer
    # plant, then publishes the STATUS sample for that tick. Recompute the
    # complete trajectory instead of accepting physically impossible rows.
    temperature_mC = int(profile["plant"]["initial_temperature_mC"])
    previous_duty_q16_16 = 0
    fixed_controller = FixedPiController()
    for row in checked:
        sample_index = int(row["sample_index"])
        if controller == "fixed":
            expected_duty = fixed_controller.update(temperature_mC, TARGET_MC)
        else:
            _require(mlp_infer is not None, "MLP trajectory requires canonical inference")
            assert mlp_infer is not None
            expected_duty = int(
                mlp_infer(temperature_mC, TARGET_MC, previous_duty_q16_16)
            )
        _require(
            int(row["duty_q16_16"]) == expected_duty,
            f"trajectory controller output mismatch at sample {sample_index}",
        )
        temperature_mC = plant_step(
            temperature_mC, expected_duty, sample_index
        )
        _require(
            int(row["measured_mC"]) == temperature_mC,
            f"trajectory plant recurrence mismatch at sample {sample_index}",
        )
        previous_duty_q16_16 = expected_duty
    return checked


def parse_completion_marker(line: str) -> dict[str, Any]:
    match = _COMPLETION.fullmatch(line.strip())
    _require(match is not None, "invalid TEST-017 completion marker")
    assert match is not None
    fields: dict[str, Any] = match.groupdict()
    for name in ("ticks", "seed", "acked", "feedback"):
        fields[name] = int(fields[name])
    return fields


def validate_completion_fields(
    fields: Mapping[str, Any],
    *,
    profile: Mapping[str, Any],
    controller: str,
    seed: int,
) -> None:
    validate_run_identity(profile=profile, controller=controller, seed=seed)
    expected_ticks = int(profile["timing"]["ticks"])
    _require(fields.get("mode") == controller, "completion controller does not match run")
    _require(fields.get("seed") == seed, "completion seed does not match run")
    _require(fields.get("ticks") == expected_ticks, "completion tick count is not 1800")
    _require(fields.get("acked") == expected_ticks, "completion ACK count is not 1800")
    _require(fields.get("feedback") == expected_ticks, "completion feedback count is not 1800")


def _index_request_events(
    events: Iterable[Mapping[str, Any]],
    *,
    endpoint: str,
    run_id: str,
    required_names: set[str],
    allowed_names: set[str] | None = None,
) -> tuple[dict[tuple[int, str], list[Mapping[str, Any]]], set[str]]:
    indexed: dict[tuple[int, str], list[Mapping[str, Any]]] = {}
    seen_names: set[str] = set()
    allowed = required_names if allowed_names is None else allowed_names
    previous_time = -1
    for event in events:
        for field in _EVENT_FIELDS:
            _require(field in event, f"{endpoint} event is missing {field}")
        _require(event["schema_version"] == "p5-ai-event-v1", f"{endpoint} event schema drift")
        _require(event["run_id"] == run_id, f"{endpoint} event run_id mismatch")
        _require(event["endpoint"] == endpoint, f"{endpoint} event endpoint mismatch")
        _require(event["scenario"] == "test-017", f"{endpoint} event scenario mismatch")
        _require(event["transport"] in ("udp", "tcp"), f"{endpoint} event transport invalid")
        try:
            monotonic_ns = int(event["monotonic_ns"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"{endpoint} event monotonic_ns is invalid") from error
        _require(monotonic_ns >= previous_time, f"{endpoint} event time moved backwards")
        previous_time = monotonic_ns

        name = str(event["event"])
        _require(name in allowed, f"unknown {endpoint} event {name!r}")
        seen_names.add(name)
        if name not in required_names:
            continue
        try:
            request_id = int(event["request_id"])
            sample_index = int(event["sample_index"])
            session_id = int(event["session_id"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"{endpoint} request event identity is invalid") from error
        _require(request_id > 0, f"{endpoint} request_id must be nonzero")
        _require(session_id > 0, f"{endpoint} session_id must be nonzero")
        _require(sample_index >= 0, f"{endpoint} sample_index must be nonnegative")
        if name in _LOCAL_ONLY_EVENTS:
            _require(event["sequence"] is None, f"{endpoint} local event sequence must be null")
        else:
            try:
                sequence = int(event["sequence"])
            except (TypeError, ValueError) as error:
                raise ValueError(f"{endpoint} packet event sequence is invalid") from error
            _require(sequence > 0, f"{endpoint} sequence must be nonzero")
        indexed.setdefault((request_id, name), []).append(event)
    return indexed, seen_names


def validate_event_associations(
    linux_events: Iterable[Mapping[str, Any]],
    zephyr_events: Iterable[Mapping[str, Any]],
    *,
    trajectory: Iterable[Mapping[str, Any]],
    run_id: str,
    controller: str,
) -> None:
    """Require raw Linux/Zephyr evidence for every applied trajectory request."""

    _require(bool(run_id), "run_id must not be empty")
    _require(controller in CONTROLLERS, "event controller is invalid")
    rows = list(trajectory)
    linux_rows = [dict(event) for event in linux_events]
    zephyr_rows = [dict(event) for event in zephyr_events]
    linux_required = set(_LINUX_COMMON_EVENTS)
    if controller == "mlp":
        linux_required.update(_LINUX_MLP_EVENTS)
    linux, _linux_seen = _index_request_events(
        linux_rows,
        endpoint="linux",
        run_id=run_id,
        required_names=linux_required,
    )
    zephyr, zephyr_seen = _index_request_events(
        zephyr_rows,
        endpoint="zephyr",
        run_id=run_id,
        required_names=_ZEPHYR_REQUEST_EVENTS,
        allowed_names=_ZEPHYR_REQUEST_EVENTS | {"safe_enter", "safe_exit"},
    )
    _require("safe_enter" in zephyr_seen, "Zephyr event log is missing safe_enter")
    _require("safe_exit" in zephyr_seen, "Zephyr event log is missing safe_exit")

    seen_requests: set[int] = set()
    run_session_id: int | None = None
    previous_control_sequence = 0
    previous_status_sequence = 0
    for row in rows:
        request_id = int(row["applied_request_id"])
        sample_index = int(row["sample_index"])
        _require(request_id not in seen_requests, "trajectory reused an applied_request_id")
        seen_requests.add(request_id)

        for name in linux_required:
            matches = linux.get((request_id, name), [])
            minimum = 1
            _require(len(matches) >= minimum, f"missing Linux {name} for request {request_id}")
            if name != "packet_send":
                _require(len(matches) == 1, f"duplicate Linux {name} for request {request_id}")
            _require(
                all(int(item["sample_index"]) == sample_index for item in matches),
                f"Linux {name} sample association mismatch for request {request_id}",
            )

        for name in _ZEPHYR_REQUEST_EVENTS:
            matches = zephyr.get((request_id, name), [])
            _require(len(matches) >= 1, f"missing Zephyr {name} for request {request_id}")
            if name != "packet_send":
                _require(len(matches) == 1, f"duplicate Zephyr control_apply for request {request_id}")
            _require(
                all(int(item["sample_index"]) == sample_index for item in matches),
                f"Zephyr {name} sample association mismatch for request {request_id}",
            )

        request_events = [
            item
            for name in linux_required
            for item in linux.get((request_id, name), [])
        ] + [
            item
            for name in _ZEPHYR_REQUEST_EVENTS
            for item in zephyr.get((request_id, name), [])
        ]
        session_ids = {int(item["session_id"]) for item in request_events}
        _require(len(session_ids) == 1, f"cross-endpoint session mismatch for request {request_id}")
        current_session_id = next(iter(session_ids))
        if run_session_id is None:
            run_session_id = current_session_id
        _require(
            current_session_id == run_session_id,
            f"session changed within qualification at request {request_id}",
        )

        control_sequences = {
            int(item["sequence"])
            for item in linux[(request_id, "packet_send")]
        }
        _require(
            len(control_sequences) == 1,
            f"CONTROL retries changed sequence for request {request_id}",
        )
        control_sequence = next(iter(control_sequences))
        _require(
            int(zephyr[(request_id, "packet_receive")][0]["sequence"])
            == control_sequence,
            f"CONTROL receive sequence mismatch for request {request_id}",
        )
        _require(
            int(zephyr[(request_id, "control_apply")][0]["sequence"])
            == control_sequence,
            f"control_apply sequence mismatch for request {request_id}",
        )
        _require(
            control_sequence > previous_control_sequence,
            f"CONTROL sequence did not advance for request {request_id}",
        )
        previous_control_sequence = control_sequence

        ack_sends = zephyr[(request_id, "ack_send")]
        ack_receives = linux[(request_id, "ack_receive")]
        ack_sequence = int(ack_sends[0]["sequence"])
        _require(
            int(ack_receives[0]["sequence"]) == ack_sequence,
            f"ACK packet sequence mismatch for request {request_id}",
        )

        status_sends_for_sequence = [
            item
            for item in zephyr[(request_id, "packet_send")]
            if item["outcome"] == "status"
        ]
        _require(
            len(status_sends_for_sequence) == 1,
            f"Zephyr request {request_id} requires one STATUS sequence",
        )
        status_sequence = int(status_sends_for_sequence[0]["sequence"])
        _require(
            int(linux[(request_id, "feedback_receive")][0]["sequence"])
            == status_sequence,
            f"STATUS feedback sequence mismatch for request {request_id}",
        )
        _require(
            status_sequence > previous_status_sequence,
            f"STATUS sequence did not advance for request {request_id}",
        )
        previous_status_sequence = status_sequence

        def first_time(indexed: Mapping[tuple[int, str], list[Mapping[str, Any]]], name: str) -> int:
            return min(int(item["monotonic_ns"]) for item in indexed[(request_id, name)])

        linux_order = ["period_release", "period_start", "input_receive"]
        if controller == "mlp":
            linux_order.extend(("inference_start", "inference_finish"))
        linux_order.extend(
            ("packet_send", "ack_receive", "feedback_receive", "period_finish")
        )
        linux_times = [first_time(linux, name) for name in linux_order]
        _require(
            linux_times == sorted(linux_times),
            f"Linux event causality mismatch for request {request_id}",
        )
        zephyr_order = (
            "period_release",
            "period_start",
            "packet_receive",
            "ack_send",
            "control_apply",
            "packet_send",
            "period_finish",
        )
        zephyr_times = [first_time(zephyr, name) for name in zephyr_order]
        _require(
            zephyr_times == sorted(zephyr_times),
            f"Zephyr event causality mismatch for request {request_id}",
        )
        expected_rtt = first_time(linux, "feedback_receive") - first_time(
            linux, "input_receive"
        )
        expected_action = first_time(zephyr, "control_apply") - first_time(
            zephyr, "packet_receive"
        )
        _require(
            int(row["closed_loop_rtt_ns"]) == expected_rtt,
            f"closed-loop RTT does not match raw Linux events for request {request_id}",
        )
        _require(
            int(row["action_latency_ns"]) == expected_action,
            f"action latency does not match raw Zephyr events for request {request_id}",
        )

        measured_mC = int(row["measured_mC"])
        duty_q16_16 = int(row["duty_q16_16"])
        input_mC = (
            25_000
            if sample_index == 0
            else int(rows[sample_index - 1]["measured_mC"])
        )

        def require_value(
            item: Mapping[str, Any], value: int, unit: str, outcome: str, label: str
        ) -> None:
            try:
                actual_value = int(item["value"])
            except (TypeError, ValueError) as error:
                raise ValueError(f"{label} has no integer value") from error
            _require(actual_value == value, f"{label} value mismatch")
            _require(item["unit"] == unit, f"{label} unit mismatch")
            _require(item["outcome"] == outcome, f"{label} outcome mismatch")

        require_value(
            linux[(request_id, "input_receive")][0],
            input_mC,
            "mC",
            "status",
            f"Linux input_receive request {request_id}",
        )
        require_value(
            linux[(request_id, "feedback_receive")][0],
            measured_mC,
            "mC",
            "status_applied",
            f"Linux feedback_receive request {request_id}",
        )
        require_value(
            linux[(request_id, "ack_receive")][0],
            control_sequence,
            "sequence",
            "ack",
            f"Linux ack_receive request {request_id}",
        )
        for item in linux[(request_id, "packet_send")]:
            try:
                send_value = int(item["value"])
            except (TypeError, ValueError) as error:
                raise ValueError(f"Linux packet_send request {request_id} has no duty") from error
            _require(send_value == duty_q16_16, "Linux packet_send duty mismatch")
            _require(item["unit"] == "q16_16", "Linux packet_send unit mismatch")
            _require(
                item["outcome"] in ("sent", "retry_sent", "fault_drop"),
                "Linux packet_send outcome invalid",
            )
        if controller == "mlp":
            require_value(
                linux[(request_id, "inference_finish")][0],
                duty_q16_16,
                "q16_16",
                "ok",
                f"Linux inference_finish request {request_id}",
            )
        require_value(
            zephyr[(request_id, "packet_receive")][0],
            duty_q16_16,
            "q16_16",
            "control_valid",
            f"Zephyr packet_receive request {request_id}",
        )
        require_value(
            zephyr[(request_id, "control_apply")][0],
            duty_q16_16,
            "q16_16",
            "applied",
            f"Zephyr control_apply request {request_id}",
        )
        require_value(
            zephyr[(request_id, "ack_send")][0],
            control_sequence,
            "sequence",
            "ack",
            f"Zephyr ack_send request {request_id}",
        )
        status_sends = [
            item
            for item in zephyr[(request_id, "packet_send")]
            if item["outcome"] == "status"
        ]
        _require(
            len(status_sends) == 1,
            f"Zephyr request {request_id} requires exactly one STATUS send event",
        )
        require_value(
            status_sends[0],
            measured_mC,
            "mC",
            "status",
            f"Zephyr packet_send STATUS request {request_id}",
        )

    _require(run_session_id is not None, "TEST-017 event log has no request session")
    safe_by_name = {
        name: [event for event in zephyr_rows if event.get("event") == name]
        for name in ("safe_enter", "safe_exit")
    }
    for name, matches in safe_by_name.items():
        _require(len(matches) == 1, f"Zephyr {name} must occur exactly once")
        item = matches[0]
        _require(
            item.get("session_id") == run_session_id,
            f"Zephyr {name} session mismatch",
        )
        _require(
            item.get("sequence") is None
            and item.get("request_id") is None
            and item.get("sample_index") is None,
            f"Zephyr {name} request identity must be null",
        )
    _require(
        int(safe_by_name["safe_enter"][0]["monotonic_ns"])
        < int(safe_by_name["safe_exit"][0]["monotonic_ns"]),
        "Zephyr safe_enter must precede safe_exit",
    )

    for request_id, name in linux:
        _require(
            request_id in seen_requests,
            f"Linux {name} references unknown request {request_id}",
        )
    for request_id, name in zephyr:
        _require(
            request_id in seen_requests,
            f"Zephyr {name} references unknown request {request_id}",
        )
