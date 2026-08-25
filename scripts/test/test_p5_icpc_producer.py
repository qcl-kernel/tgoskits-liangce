from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "scripts" / "contest" / "ai"
sys.path.insert(0, str(AI_DIR))

import closed_loop_contract  # noqa: E402
import produce_icpc_records  # noqa: E402


def wire_control(*, session_id: int, sequence: int, request_id: int, flags: int = 1) -> bytes:
    payload = bytearray(24)
    payload[0:3] = bytes((1, 1, 1))
    payload[4:8] = request_id.to_bytes(4, "big")
    payload[8:12] = (100).to_bytes(4, "big", signed=True)
    payload[12:16] = (55_000).to_bytes(4, "big", signed=True)
    payload[16:20] = (731_924_617).to_bytes(4, "big")
    payload[20:22] = (500).to_bytes(2, "big")
    header = bytearray(
        b"ICPC"
        + bytes((1, 36, 1, flags))
        + session_id.to_bytes(4, "big")
        + sequence.to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + (1_000).to_bytes(8, "big")
        + len(payload).to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + (0).to_bytes(4, "big")
    )
    header[32:36] = closed_loop_contract._crc32c(bytes(header)[:32] + b"\0\0\0\0" + payload).to_bytes(4, "big")
    return bytes(header) + bytes(payload)


class P5IcpcProducerTests(unittest.TestCase):
    def test_profile_drop_preserves_raw_wire_without_frame(self) -> None:
        wire = wire_control(session_id=17, sequence=9, request_id=1)
        attempts = [
            {
                "schema_version": "p5-ai-icpc-attempt-v1",
                "run_id": "p5-producer-negative-drop",
                "session_id": 17,
                "direction": "linux_to_zephyr",
                "attempt": 0,
                "fault_disposition": "profile_drop",
                "timestamp_ms": 1000,
                "monotonic_ns": 100,
                "wire_hex": wire.hex(),
            }
        ]
        records = produce_icpc_records.produce_records(
            attempts,
            [],
            run_id="p5-producer-negative-drop",
            session_id=17,
        )
        self.assertEqual(records[0]["frame_sequence"], None)
        self.assertEqual(records[0]["message_type"], "control")
        self.assertEqual(records[0]["wire_hex"], wire.hex())

        with self.assertRaises(produce_icpc_records.IcpcProducerError):
            produce_icpc_records.produce_records(
                attempts,
                [],
                run_id="p5-producer-negative-drop",
                session_id=17,
                fault_manifest={"drop_first_control_request_indices": []},
            )

    def test_forwarded_attempt_binds_one_exact_capture_frame(self) -> None:
        wire = wire_control(session_id=17, sequence=9, request_id=1)
        attempts = [
            {
                "schema_version": "p5-ai-icpc-attempt-v1",
                "run_id": "p5-producer-forwarded",
                "session_id": 17,
                "direction": "linux_to_zephyr",
                "attempt": 0,
                "fault_disposition": "forwarded",
                "timestamp_ms": 1000,
                "monotonic_ns": 100,
                "wire_hex": wire.hex(),
            }
        ]
        frames = [
            {
                "run_id": "p5-producer-forwarded",
                "session_id": "17",
                "sequence": 4,
                "direction": "port0_to_port1",
                "_icpc_wire_hex": wire.hex(),
            }
        ]
        records = produce_icpc_records.produce_records(
            attempts, frames, run_id="p5-producer-forwarded", session_id=17
        )
        self.assertEqual(records[0]["frame_sequence"], 4)

        with self.assertRaises(produce_icpc_records.IcpcProducerError):
            produce_icpc_records.produce_records(
                attempts,
                frames + [dict(frames[0], sequence=5)],
                run_id="p5-producer-forwarded",
                session_id=17,
            )

    def test_control_sequence_cannot_map_to_multiple_requests(self) -> None:
        attempts = []
        for request_id in (1, 2):
            wire = wire_control(session_id=17, sequence=9, request_id=request_id)
            attempts.append(
                {
                    "schema_version": "p5-ai-icpc-attempt-v1",
                    "run_id": "p5-producer-sequence-identity",
                    "session_id": 17,
                    "direction": "linux_to_zephyr",
                    "attempt": 0,
                    "fault_disposition": "forwarded",
                    "timestamp_ms": request_id,
                    "monotonic_ns": request_id,
                    "wire_hex": wire.hex(),
                }
            )
        with self.assertRaises(produce_icpc_records.IcpcProducerError):
            produce_icpc_records.produce_records(
                attempts,
                [],
                run_id="p5-producer-sequence-identity",
                session_id=17,
            )

    def test_producer_rejects_marker_style_or_extra_fields(self) -> None:
        wire = wire_control(session_id=17, sequence=9, request_id=1)
        row = {
            "schema_version": "p5-ai-icpc-attempt-v1",
            "run_id": "p5-producer-negative-fields",
            "session_id": 17,
            "direction": "linux_to_zephyr",
            "attempt": 0,
            "fault_disposition": "profile_drop",
            "timestamp_ms": 1000,
            "monotonic_ns": 100,
            "wire_hex": wire.hex(),
            "completion_marker": "TGOS_LINUX_TRAJ_DONE ticks=1800",
        }
        with self.assertRaises(produce_icpc_records.IcpcProducerError):
            produce_icpc_records.produce_records(
                [row], [], run_id="p5-producer-negative-fields", session_id=17
            )

    def test_injection_transcript_counts_and_hash_binds_artifacts(self) -> None:
        dropped_wire = wire_control(session_id=17, sequence=9, request_id=1)
        retry_wire = wire_control(session_id=17, sequence=9, request_id=1, flags=3)
        run_id = "p5-producer-transcript"
        attempts = [
            {
                "schema_version": "p5-ai-icpc-attempt-v1",
                "run_id": run_id,
                "session_id": 17,
                "direction": "linux_to_zephyr",
                "attempt": 0,
                "fault_disposition": "profile_drop",
                "timestamp_ms": 1000,
                "monotonic_ns": 100,
                "wire_hex": dropped_wire.hex(),
            },
            {
                "schema_version": "p5-ai-icpc-attempt-v1",
                "run_id": run_id,
                "session_id": 17,
                "direction": "linux_to_zephyr",
                "attempt": 1,
                "fault_disposition": "forwarded",
                "timestamp_ms": 1001,
                "monotonic_ns": 101,
                "wire_hex": retry_wire.hex(),
            },
        ]
        frames = [
            {
                "run_id": run_id,
                "session_id": "17",
                "sequence": 4,
                "direction": "port0_to_port1",
                "_icpc_wire_hex": retry_wire.hex(),
            }
        ]
        fault_manifest = {"drop_first_control_request_indices": [0]}
        records = produce_icpc_records.produce_records(
            attempts,
            frames,
            run_id=run_id,
            session_id=17,
            fault_manifest=fault_manifest,
        )
        with tempfile.TemporaryDirectory(prefix="p5-icpc-transcript-") as name:
            root = Path(name)
            raw_bytes = "".join(json.dumps(row, sort_keys=True) + "\n" for row in attempts).encode("utf-8")
            icpc_path = root / "network" / "icpc.jsonl"
            write_manifest_path = root / "configs" / "fault-manifest.json"
            produce_icpc_records.write_records(records, icpc_path)
            write_manifest_path.parent.mkdir(parents=True)
            manifest_bytes = json.dumps(fault_manifest, sort_keys=True).encode("utf-8")
            write_manifest_path.write_bytes(manifest_bytes)
            transcript = produce_icpc_records.build_injection_transcript(
                records,
                run_id=run_id,
                session_id=17,
                raw_attempts=raw_bytes,
                icpc_records=icpc_path.read_bytes(),
                fault_manifest_bytes=manifest_bytes,
                fault_manifest=fault_manifest,
            )
            self.assertEqual(transcript["schema_version"], "p5-ai-injection-transcript-v1")
            self.assertEqual(transcript["qualified"], False)
            self.assertEqual(transcript["counts"], {
                "attempts": 2,
                "forwarded": 1,
                "profile_drop": 1,
                "control": 2,
                "control_retry": 1,
                "ack": 0,
                "status": 0,
            })
            self.assertEqual(transcript["dropped_request_indices"], [0])
            self.assertEqual(transcript["retry_request_indices"], [0])
            self.assertEqual(
                transcript["artifacts"]["raw_attempts"]["sha256"], hashlib.sha256(raw_bytes).hexdigest()
            )
            output = root / "network" / "injection-transcript.json"
            produce_icpc_records.write_transcript(transcript, output)
            with self.assertRaises(produce_icpc_records.IcpcProducerError):
                produce_icpc_records.write_transcript(transcript, output)

    def test_write_records_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-icpc-producer-") as name:
            output = Path(name) / "icpc.jsonl"
            output.write_text("old\n", encoding="utf-8")
            with self.assertRaises(produce_icpc_records.IcpcProducerError):
                produce_icpc_records.write_records([], output)


if __name__ == "__main__":
    unittest.main()
