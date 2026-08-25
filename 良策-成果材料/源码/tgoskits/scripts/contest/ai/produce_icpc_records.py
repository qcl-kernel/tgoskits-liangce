#!/usr/bin/env python3
"""Produce TEST-017 ICPC records from raw injection-point attempts.

The producer consumes raw wire bytes captured immediately before the fault
injector and the already validated P4 frame rows.  It never reconstructs a
missing CONTROL from a completion marker, trajectory, or summary.  A success
from this tool is still a host/source contract; the full qualification
validator must bind the records to trajectory and endpoint events.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_vm_carveout_io import publish_new_file  # noqa: E402

try:
    from . import closed_loop_contract
    from . import validate_closed_loop
except ImportError:  # pragma: no cover - direct script execution
    import closed_loop_contract  # type: ignore[no-redef]
    import validate_closed_loop  # type: ignore[no-redef]


ATTEMPT_SCHEMA = "p5-ai-icpc-attempt-v1"
TRANSCRIPT_SCHEMA = "p5-ai-injection-transcript-v1"
ATTEMPT_FIELDS = {
    "schema_version",
    "run_id",
    "session_id",
    "direction",
    "attempt",
    "fault_disposition",
    "timestamp_ms",
    "monotonic_ns",
    "wire_hex",
}
MESSAGE_NAMES = {
    closed_loop_contract.ICPC_MESSAGE_CONTROL: "control",
    closed_loop_contract.ICPC_MESSAGE_ACK: "ack",
    closed_loop_contract.ICPC_MESSAGE_STATUS: "status",
}


class IcpcProducerError(ValueError):
    """Raised when raw injection evidence cannot be bound safely."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise IcpcProducerError(message)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_jsonl_bytes(data: bytes, label: str) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise IcpcProducerError(f"{label} is not UTF-8") from error
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise IcpcProducerError(f"{label}:{line_number} is not JSON: {error}") from error
        if not isinstance(value, dict):
            raise IcpcProducerError(f"{label}:{line_number} must be an object")
        rows.append(value)
    return rows


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise IcpcProducerError(f"cannot read {path}: {error}") from error
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise IcpcProducerError(f"{path}:{line_number} is not JSON: {error}") from error
        if not isinstance(value, dict):
            raise IcpcProducerError(f"{path}:{line_number} must be an object")
        rows.append(value)
    return rows


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IcpcProducerError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise IcpcProducerError(f"JSON object required: {path}")
    return value


def _decode_attempt(row: Mapping[str, Any], index: int) -> tuple[dict[str, Any], bytes]:
    _require(set(row) == ATTEMPT_FIELDS, f"attempt {index} field set mismatch")
    _require(row["schema_version"] == ATTEMPT_SCHEMA, f"attempt {index} schema mismatch")
    _require(isinstance(row["run_id"], str) and row["run_id"], f"attempt {index} run_id invalid")
    _require(type(row["session_id"]) is int and row["session_id"] > 0, f"attempt {index} session_id invalid")
    _require(row["direction"] in ("linux_to_zephyr", "zephyr_to_linux"), f"attempt {index} direction invalid")
    _require(type(row["attempt"]) is int and row["attempt"] >= 0, f"attempt {index} attempt invalid")
    _require(row["fault_disposition"] in ("forwarded", "profile_drop"), f"attempt {index} fault disposition invalid")
    for field in ("timestamp_ms", "monotonic_ns"):
        _require(type(row[field]) is int and row[field] >= 0, f"attempt {index} {field} invalid")
    try:
        wire = bytes.fromhex(str(row["wire_hex"]))
    except (TypeError, ValueError) as error:
        raise IcpcProducerError(f"attempt {index} wire_hex invalid") from error
    try:
        decoded = closed_loop_contract._decode_icpc_wire(wire)
    except (TypeError, ValueError) as error:
        raise IcpcProducerError(f"attempt {index} ICPC wire invalid: {error}") from error
    _require(decoded["session_id"] == row["session_id"], f"attempt {index} session mismatch")
    expected_direction = (
        "linux_to_zephyr"
        if decoded["message_type"] == closed_loop_contract.ICPC_MESSAGE_CONTROL
        else "zephyr_to_linux"
    )
    _require(row["direction"] == expected_direction, f"attempt {index} direction/message mismatch")
    if row["fault_disposition"] == "profile_drop":
        _require(decoded["message_type"] == closed_loop_contract.ICPC_MESSAGE_CONTROL, f"attempt {index} only CONTROL may be profile-dropped")
        _require(row["attempt"] == 0, f"attempt {index} profile drop must be attempt zero")
    else:
        if decoded["message_type"] == closed_loop_contract.ICPC_MESSAGE_CONTROL:
            _require(row["attempt"] in (0, 1), f"attempt {index} CONTROL retry index invalid")
            expected_flags = closed_loop_contract.ICPC_FLAG_ACK_REQUIRED | (
                closed_loop_contract.ICPC_FLAG_RETRANSMISSION if row["attempt"] == 1 else 0
            )
            _require(decoded["flags"] == expected_flags, f"attempt {index} CONTROL retry flags invalid")
        else:
            _require(row["attempt"] == 0, f"attempt {index} ACK/STATUS attempt must be zero")
    return decoded, wire


def produce_records(
    attempts: Iterable[Mapping[str, Any]],
    frames: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    session_id: int,
    fault_manifest: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Bind raw attempts to exact decoded bytes and unique capture frames."""

    raw_rows = [dict(row) for row in attempts]
    _require(raw_rows, "attempt input must not be empty")
    decoded_rows: list[tuple[dict[str, Any], bytes, dict[str, Any]]] = []
    previous_monotonic_ns = {
        "linux_to_zephyr": -1,
        "zephyr_to_linux": -1,
    }
    control_by_sequence: dict[int, int] = {}
    for index, row in enumerate(raw_rows, 1):
        _require(row.get("run_id") == run_id and row.get("session_id") == session_id, f"attempt {index} identity mismatch")
        decoded, wire = _decode_attempt(row, index)
        direction = str(row["direction"])
        _require(
            row["monotonic_ns"] >= previous_monotonic_ns[direction],
            f"attempt {index} {direction} monotonic clock moved backwards",
        )
        previous_monotonic_ns[direction] = row["monotonic_ns"]
        if decoded["message_type"] == closed_loop_contract.ICPC_MESSAGE_CONTROL:
            request_id = decoded["request_id"]
            _require(request_id > 0, f"attempt {index} request id is zero")
            previous_request_id = control_by_sequence.get(decoded["sequence"])
            _require(
                previous_request_id is None or previous_request_id == request_id,
                f"attempt {index} CONTROL sequence maps to multiple requests",
            )
            control_by_sequence[decoded["sequence"]] = request_id
        decoded_rows.append((decoded, wire, row))

    if fault_manifest is not None:
        dropped = fault_manifest.get("drop_first_control_request_indices")
        _require(isinstance(dropped, list), "fault manifest drop indices are missing")
        dropped_indices = {
            int(index) for index in dropped if type(index) is int and index >= 0
        }
        controls_by_request: dict[int, list[Mapping[str, Any]]] = {}
        control_sequences_by_request: dict[int, list[int]] = {}
        for decoded, _wire, row in decoded_rows:
            if decoded["message_type"] == closed_loop_contract.ICPC_MESSAGE_CONTROL:
                controls_by_request.setdefault(int(decoded["request_id"]), []).append(row)
                control_sequences_by_request.setdefault(int(decoded["request_id"]), []).append(int(decoded["sequence"]))
        for request_id, controls in controls_by_request.items():
            expected_attempts = [0, 1] if request_id - 1 in dropped_indices else [0]
            _require(
                [int(row["attempt"]) for row in controls] == expected_attempts,
                f"CONTROL attempts do not match fault manifest for request {request_id}",
            )
            expected_dispositions = (
                ["profile_drop", "forwarded"]
                if request_id - 1 in dropped_indices
                else ["forwarded"]
            )
            _require(
                [str(row["fault_disposition"]) for row in controls]
                == expected_dispositions,
                f"CONTROL fault disposition does not match fault manifest for request {request_id}",
            )
            _require(
                len(set(control_sequences_by_request[request_id])) == 1,
                f"CONTROL retry changed sequence for request {request_id}",
            )

    frame_rows = [dict(row) for row in frames]
    frame_by_wire: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for frame in frame_rows:
        wire_hex = frame.get("_icpc_wire_hex")
        if isinstance(wire_hex, str):
            key = (str(frame.get("direction")), wire_hex.lower())
            frame_by_wire.setdefault(key, []).append(frame)

    used_frames: set[int] = set()
    records: list[dict[str, Any]] = []
    for record_index, (decoded, wire, row) in enumerate(decoded_rows, 1):
        message_name = MESSAGE_NAMES.get(decoded["message_type"])
        _require(message_name is not None, f"attempt {record_index} message type is unsupported")
        if decoded["message_type"] == closed_loop_contract.ICPC_MESSAGE_ACK:
            request_id = control_by_sequence.get(decoded["ack_sequence"])
            _require(request_id is not None, f"attempt {record_index} ACK has no CONTROL association")
        else:
            request_id = decoded["request_id"]
        assert request_id is not None
        _require(request_id > 0, f"attempt {record_index} request id is zero")
        frame_sequence = None
        if row["fault_disposition"] == "forwarded":
            frame_direction = (
                "port0_to_port1" if row["direction"] == "linux_to_zephyr" else "port1_to_port0"
            )
            candidates = frame_by_wire.get((frame_direction, wire.hex()), [])
            _require(len(candidates) == 1, f"attempt {record_index} does not bind exactly one capture frame")
            frame = candidates[0]
            frame_sequence = frame.get("sequence")
            _require(type(frame_sequence) is int and frame_sequence >= 0, f"attempt {record_index} frame sequence invalid")
            _require(frame_sequence not in used_frames, f"attempt {record_index} reuses a capture frame")
            used_frames.add(frame_sequence)
        else:
            _require(not frame_by_wire.get(("port0_to_port1", wire.hex())) and not frame_by_wire.get(("port1_to_port0", wire.hex())), f"attempt {record_index} dropped wire has a capture frame")
        payload = decoded["payload"]
        records.append(
            {
                "schema_version": closed_loop_contract.ICPC_RECORD_SCHEMA,
                "record_index": record_index,
                "run_id": run_id,
                "session_id": session_id,
                "frame_sequence": frame_sequence,
                "direction": row["direction"],
                "attempt": row["attempt"],
                "fault_disposition": row["fault_disposition"],
                "message_type": message_name,
                "flags": decoded["flags"],
                "sequence": decoded["sequence"],
                "ack_sequence": decoded["ack_sequence"],
                "request_id": request_id,
                "sample_index": request_id - 1,
                "timestamp_ms": decoded["timestamp_ms"],
                "monotonic_ns": row["monotonic_ns"],
                "payload_length": len(payload),
                "payload_sha256": _sha256(payload),
                "payload_hex": payload.hex(),
                "wire_length": len(wire),
                "wire_sha256": _sha256(wire),
                "wire_hex": wire.hex(),
            }
        )
    return records


def write_records(records: Iterable[Mapping[str, Any]], output: Path) -> None:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    publish_new_file(
        output,
        "".join(json.dumps(dict(row), sort_keys=True) + "\n" for row in records).encode(
            "utf-8"
        ),
        error_type=IcpcProducerError,
    )


def build_injection_transcript(
    records: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    session_id: int,
    raw_attempts: bytes,
    icpc_records: bytes,
    fault_manifest_bytes: bytes,
    fault_manifest: Mapping[str, Any],
    raw_attempts_path: str = "network/icpc-attempts.jsonl",
    icpc_records_path: str = "network/icpc.jsonl",
    fault_manifest_path: str = "configs/fault-manifest.json",
) -> dict[str, Any]:
    """Build an independently hash-bound host/source injection transcript."""

    rows = [dict(row) for row in records]
    _require(rows, "transcript records must not be empty")
    for index, row in enumerate(rows, 1):
        _require(row.get("schema_version") == closed_loop_contract.ICPC_RECORD_SCHEMA, f"transcript record {index} schema mismatch")
        _require(row.get("run_id") == run_id and row.get("session_id") == session_id, f"transcript record {index} identity mismatch")
        _require(row.get("message_type") in ("control", "ack", "status"), f"transcript record {index} message type invalid")
        _require(type(row.get("request_id")) is int and row["request_id"] > 0, f"transcript record {index} request id invalid")
        _require(type(row.get("attempt")) is int and row["attempt"] >= 0, f"transcript record {index} attempt invalid")
        _require(row.get("fault_disposition") in ("forwarded", "profile_drop"), f"transcript record {index} fault disposition invalid")
        if row["fault_disposition"] == "profile_drop":
            _require(row["message_type"] == "control" and row["attempt"] == 0, f"transcript record {index} invalid profile drop")
        if row["message_type"] == "control":
            _require(row["attempt"] in (0, 1), f"transcript record {index} CONTROL retry invalid")

    raw_rows = _decode_jsonl_bytes(raw_attempts, raw_attempts_path)
    _require(len(raw_rows) == len(rows), "raw attempt count does not match ICPC record count")
    for index, (raw_row, record) in enumerate(zip(raw_rows, rows), 1):
        for field in ("run_id", "session_id", "direction", "attempt", "fault_disposition", "wire_hex"):
            actual = str(raw_row.get(field)).lower() if field == "wire_hex" else raw_row.get(field)
            expected = str(record.get(field)).lower() if field == "wire_hex" else record.get(field)
            _require(actual == expected, f"raw attempt {index} does not bind to transcript record")

    icpc_rows = _decode_jsonl_bytes(icpc_records, icpc_records_path)
    _require(icpc_rows == rows, "ICPC artifact does not bind to transcript records")
    try:
        manifest_from_bytes = json.loads(fault_manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IcpcProducerError(f"{fault_manifest_path} is not a valid JSON object") from error
    _require(isinstance(manifest_from_bytes, dict), f"{fault_manifest_path} must be a JSON object")
    _require(dict(manifest_from_bytes) == dict(fault_manifest), "fault manifest bytes do not match validated manifest")

    dropped_indices = [int(row["request_id"]) - 1 for row in rows if row["fault_disposition"] == "profile_drop"]
    retry_indices = [
        int(row["request_id"]) - 1
        for row in rows
        if row["message_type"] == "control" and row["attempt"] == 1
    ]
    _require(len(dropped_indices) == len(set(dropped_indices)), "duplicate dropped request index")
    _require(len(retry_indices) == len(set(retry_indices)), "duplicate retry request index")
    expected_raw = fault_manifest.get("drop_first_control_request_indices")
    _require(isinstance(expected_raw, list), "fault manifest drop indices are missing")
    _require(all(type(index) is int and index >= 0 for index in expected_raw), "fault manifest drop indices are invalid")
    expected_indices = [int(index) for index in expected_raw]
    _require(len(expected_indices) == len(set(expected_indices)), "fault manifest drop indices are duplicated")
    _require(sorted(dropped_indices) == sorted(expected_indices), "transcript dropped requests do not match fault manifest")
    _require(sorted(retry_indices) == sorted(expected_indices), "transcript retry pairing does not match fault manifest")

    controls_by_request: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if row["message_type"] == "control":
            controls_by_request.setdefault(int(row["request_id"]), []).append(row)
    for request_id, controls in controls_by_request.items():
        expected = [0, 1] if request_id - 1 in expected_indices else [0]
        _require([int(row["attempt"]) for row in controls] == expected, f"transcript CONTROL retry pairing invalid for request {request_id}")
        expected_dispositions = ["profile_drop", "forwarded"] if request_id - 1 in expected_indices else ["forwarded"]
        _require([row["fault_disposition"] for row in controls] == expected_dispositions, f"transcript CONTROL dispositions invalid for request {request_id}")
        _require(len({row["sequence"] for row in controls}) == 1, f"transcript CONTROL retry changed sequence for request {request_id}")

    counts = {
        "attempts": len(rows),
        "forwarded": sum(row["fault_disposition"] == "forwarded" for row in rows),
        "profile_drop": sum(row["fault_disposition"] == "profile_drop" for row in rows),
        "control": sum(row["message_type"] == "control" for row in rows),
        "control_retry": len(retry_indices),
        "ack": sum(row["message_type"] == "ack" for row in rows),
        "status": sum(row["message_type"] == "status" for row in rows),
    }
    return {
        "schema_version": TRANSCRIPT_SCHEMA,
        "run_id": run_id,
        "session_id": session_id,
        "evidence_class": "L2 host/source",
        "qualified": False,
        "status": "host_source_contract",
        "counts": counts,
        "dropped_request_indices": sorted(dropped_indices),
        "retry_request_indices": sorted(retry_indices),
        "artifacts": {
            "raw_attempts": {"path": raw_attempts_path, "sha256": _sha256(raw_attempts)},
            "icpc_records": {"path": icpc_records_path, "sha256": _sha256(icpc_records)},
            "fault_manifest": {"path": fault_manifest_path, "sha256": _sha256(fault_manifest_bytes)},
        },
        "does_not_prove": [
            "Guest execution",
            "target/QEMU runtime",
            "AI closed-loop qualification",
            "L8 realtime qualification",
        ],
    }


def write_transcript(transcript: Mapping[str, Any], output: Path) -> None:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    publish_new_file(
        output,
        (json.dumps(dict(transcript), indent=2, sort_keys=True) + "\n").encode("utf-8"),
        error_type=IcpcProducerError,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", required=True, type=Path)
    parser.add_argument("--attempts", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--transcript-output", type=Path)
    args = parser.parse_args(argv)
    root = args.run_directory
    try:
        session = json.loads((root / "session.json").read_text(encoding="utf-8"))
        run_id = session["run_id"]
        session_id = int(session["session_id"])
        attempts_path = args.attempts or root / "network" / "icpc-attempts.jsonl"
        output = args.output or root / "network" / "icpc.jsonl"
        fault_manifest_path = root / "configs" / "fault-manifest.json"
        fault_manifest = _load_json_object(fault_manifest_path)
        raw_attempts = attempts_path.read_bytes()
        fault_manifest_bytes = fault_manifest_path.read_bytes()
        attempt_rows = _load_jsonl(attempts_path)
        frames = validate_closed_loop._validate_structured_network(root, run_id, session_id)
        records = produce_records(
            attempt_rows,
            frames,
            run_id=run_id,
            session_id=session_id,
            fault_manifest=fault_manifest,
        )
        write_records(records, output)
        transcript = build_injection_transcript(
            records,
            run_id=run_id,
            session_id=session_id,
            raw_attempts=raw_attempts,
            icpc_records=output.read_bytes(),
            fault_manifest_bytes=fault_manifest_bytes,
            fault_manifest=fault_manifest,
            raw_attempts_path=str(attempts_path.relative_to(root).as_posix()) if attempts_path.is_relative_to(root) else attempts_path.name,
            icpc_records_path=str(output.relative_to(root).as_posix()) if output.is_relative_to(root) else output.name,
            fault_manifest_path=str(fault_manifest_path.relative_to(root).as_posix()),
        )
        transcript_output = args.transcript_output or root / "network" / "injection-transcript.json"
        write_transcript(transcript, transcript_output)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"P5_ICPC_PRODUCER_BLOCKED: {error}")
        return 1
    print(f"P5_ICPC_PRODUCER_PASS records={len(records)} qualified=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
