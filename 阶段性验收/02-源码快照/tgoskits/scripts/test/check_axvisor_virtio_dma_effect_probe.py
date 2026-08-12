#!/usr/bin/env python3
"""Behavioral contract for one bounded virtio-blk DMA-effect observation."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import secrets
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/contest"))
import validate_virtio_dma_effect_probe as validator  # noqa: E402


RAW_LOG_FOR_MUTATIONS: Path | None = None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run(args: list[str]) -> int:
    if "--raw-log" not in args:
        if RAW_LOG_FOR_MUTATIONS is None:
            raise RuntimeError("test raw log is not initialized")
        args = [*args, "--raw-log", str(RAW_LOG_FOR_MUTATIONS)]
    with contextlib.redirect_stderr(io.StringIO()):
        return validator.main(args)


def _source(path: Path, data: bytes) -> dict[str, object]:
    return {"path": path.name, "size": len(data), "sha256": _sha(data)}


def main() -> int:
    global RAW_LOG_FOR_MUTATIONS
    errors: list[str] = []
    results = ROOT / "results"
    made_results = not results.exists()
    results.mkdir(exist_ok=True)
    directory = (
        results / f".virtio-dma-effect-contract-{os.getpid()}-{secrets.token_hex(8)}"
    )
    directory.mkdir()
    try:
        nonce = "0123456789abcdef0123456789abcdef"
        expected = bytes(range(256)) * 2
        before_payload = b"\xa5" * len(expected)
        guard = b"\x5a" * 4096
        expected_path = directory / "expected.bin"
        before_guard_path = directory / "before-guard.bin"
        after_guard_path = directory / "after-guard.bin"
        before_payload_path = directory / "before-payload.bin"
        after_payload_path = directory / "after-payload.bin"
        request_path = directory / "request.json"
        session_path = directory / "session.json"
        raw_log_path = directory / "axvisor.log"
        output_path = directory / "result.json"
        expected_path.write_bytes(expected)
        before_guard_path.write_bytes(guard)
        after_guard_path.write_bytes(guard)
        before_payload_path.write_bytes(before_payload)
        after_payload_path.write_bytes(expected)

        device = {
            "kind": "virtio-blk-device",
            "id": "linux-block",
            "bus": "virtio-mmio-bus.0",
            "driveId": "dma-probe-disk",
            "guestPath": "/dev/vdb",
        }
        control = {
            "kind": "virtio-console",
            "chardevId": "dma-go-chardev",
            "serialDeviceId": "dma-go-serial",
            "serialBus": "virtio-mmio-bus.2",
            "portId": "dma-go-port",
            "portName": "dma-go",
            "guestPath": "/dev/hvc0",
            "socketPath": f"/tmp/axdma-{nonce}/control.sock",
        }
        payload_range = {"gpa": 0x180200000, "hpa": 0x180200000, "size": len(expected)}
        guard_range = {"hpa": 0x180000000, "size": len(guard)}
        bound_region = {
            "gpa": 0x180000000,
            "hpa": 0x180000000,
            "size": 0x4000000,
            "flags": 7,
            "mapType": 2,
            "identity": True,
        }
        capture_template = {
            "allowedOperations": [
                "query-name",
                "query-status",
                "stop",
                "cont",
                "pmemsave",
            ],
            "measurements": [
                {
                    "phase": phase,
                    "target": target,
                    "operation": "pmemsave",
                    "addressSpace": "host-physical",
                    "hpa": source["hpa"],
                    "size": source["size"],
                    "outputFileName": filename,
                }
                for phase, target, source, filename in (
                    ("before", "guard", guard_range, "before-guard.bin"),
                    ("before", "payload", payload_range, "before-payload.bin"),
                    ("after", "guard", guard_range, "after-guard.bin"),
                    ("after", "payload", payload_range, "after-payload.bin"),
                )
            ],
        }
        request_value = {
            "schemaVersion": 1,
            "artifactStatus": "request-prepared",
            "status": "single_virtio_blk_read_dma_effect_request_prepared",
            "proofScope": "one-virtio-blk-read-request-bound-to-one-qmp-session",
            "doesNotProve": [
                "the request was submitted or completed",
                "DMA was executed",
                "DMA isolation",
                "dual-Guest execution",
                "Linux and Zephyr IP connectivity",
            ],
            "sessionNonce": nonce,
            "qemuName": f"axvisor-dma-effect-{nonce}",
            "repository": {
                "worktreeRoot": str(ROOT),
                "gitHead": "0" * 40,
                "worktreeState": "dirty",
            },
            "inputs": {
                "qemuConfig": {"path": "qemu.toml", "size": 1, "sha256": "1" * 64},
                "buildConfig": {"path": "build.toml", "size": 1, "sha256": "2" * 64},
                "vmConfig": {"path": "vm.toml", "size": 1, "sha256": "3" * 64},
            },
            "device": device,
            "control": control,
            "request": {
                "requestId": f"virtio-blk-read-{nonce}",
                "operation": "read",
                "sector": 8,
                "vmId": 1,
                "payload": payload_range,
                "guard": guard_range,
                "boundMapReservedRegion": bound_region,
            },
            "guestMarkers": {
                "ready": f"AXVISOR_VIRTIO_BLK_ODIRECT_READY nonce={nonce} gpa=0x180200000 size=512 sector=8 device=/dev/vdb control_device=/dev/hvc0",
                "done": f"AXVISOR_VIRTIO_BLK_ODIRECT_DONE nonce={nonce} bytes=512 gpa=0x180200000",
            },
            "expectedPayload": _source(expected_path, expected),
            "qmpCaptureTemplate": capture_template,
        }
        request_bytes = json.dumps(request_value, sort_keys=True).encode()
        request_path.write_bytes(request_bytes)
        ready_marker = request_value["guestMarkers"]["ready"].encode("ascii")
        done_marker = request_value["guestMarkers"]["done"].encode("ascii")
        raw_log = b"booting\n" + ready_marker + b"\nrequesting\n" + done_marker + b"\n"
        raw_log_path.write_bytes(raw_log)
        RAW_LOG_FOR_MUTATIONS = raw_log_path
        marker_observations = {
            "ready": {
                "line": 2,
                "byteOffset": len(b"booting\n"),
                "marker": ready_marker.decode("ascii"),
            },
            "done": {
                "line": 4,
                "byteOffset": len(b"booting\n")
                + len(ready_marker)
                + len(b"\nrequesting\n"),
                "marker": done_marker.decode("ascii"),
            },
        }
        go_command = f"GO {nonce}\n"
        measurements = []
        for phase, target, path, data, hpa in (
            ("before", "guard", before_guard_path, guard, 0x180000000),
            ("before", "payload", before_payload_path, before_payload, 0x180200000),
            ("after", "guard", after_guard_path, guard, 0x180000000),
            ("after", "payload", after_payload_path, expected, 0x180200000),
        ):
            measurements.append(
                {
                    "phase": phase,
                    "target": target,
                    "operation": "pmemsave",
                    "addressSpace": "host-physical",
                    "hpa": hpa,
                    "size": len(data),
                    "source": _source(path, data),
                }
            )
        session_value = {
            "schemaVersion": 1,
            "artifactStatus": "capture-generated-unreviewed",
            "status": "identity_bound_single_virtio_blk_dma_effect_session_completed",
            "proofScope": "same-session-one-virtio-blk-read-paused-qmp-pmemsave-measurements",
            "sessionNonce": nonce,
            "qemuName": f"axvisor-dma-effect-{nonce}",
            "device": device,
            "request": _source(request_path, request_bytes),
            "rawLog": _source(raw_log_path, raw_log),
            "markerObservations": marker_observations,
            "guestControl": {
                "channel": "virtio-console-unix-socket",
                "control": control,
                "command": go_command,
                "bytes": len(go_command.encode("ascii")),
                "sha256": _sha(go_command.encode("ascii")),
                "peer": {"pid": 7, "uid": 1000},
            },
            "mapping": {
                "payloadGpa": payload_range["gpa"],
                "payloadHpa": payload_range["hpa"],
                "payloadSize": payload_range["size"],
                "mapType": 2,
                "identity": True,
                "boundMapReservedRegion": bound_region,
            },
            "qmp": {
                "capabilitiesNegotiated": True,
                "operations": [
                    "query-name",
                    "query-status",
                    "stop",
                    "cont",
                    "pmemsave",
                ],
                "queryName": f"axvisor-dma-effect-{nonce}",
                "states": ["running", "paused", "running", "paused", "running"],
                "peerPid": 7,
                "peerUid": 1000,
            },
            "timeline": [
                {"event": "qmp-state", "state": "running"},
                {"event": "guest-ready-marker", "marker": "ready"},
                {"event": "qmp-state", "state": "paused"},
                {
                    "event": "capture-window",
                    "phase": "before",
                    "measurementIndexes": [0, 1],
                },
                {"event": "qmp-state", "state": "running"},
                {
                    "event": "guest-control",
                    "channel": "virtio-console-unix-socket",
                    "commandSha256": _sha(go_command.encode("ascii")),
                },
                {"event": "guest-done-marker", "marker": "done"},
                {"event": "qmp-state", "state": "paused"},
                {
                    "event": "capture-window",
                    "phase": "after",
                    "measurementIndexes": [2, 3],
                },
                {"event": "qmp-state", "state": "running"},
            ],
            "measurements": measurements,
        }
        session_path.write_bytes(json.dumps(session_value, sort_keys=True).encode())
        common = [
            "--request",
            str(request_path),
            "--session",
            str(session_path),
            "--raw-log",
            str(raw_log_path),
            "--expected-payload",
            str(expected_path),
            "--before-guard",
            str(before_guard_path),
            "--after-guard",
            str(after_guard_path),
            "--before-payload",
            str(before_payload_path),
            "--after-payload",
            str(after_payload_path),
        ]
        if _run([*common, "--output", str(output_path)]) != 0:
            errors.append(
                "production validator rejects a byte-bound in-bounds virtio-blk effect"
            )
        else:
            result_bytes = output_path.read_bytes()
            result = json.loads(result_bytes)
            if result.get("status") != "controlled_virtio_blk_dma_effect_observed":
                errors.append(
                    "unchanged guard does not receive the narrow success status"
                )
            if result.get("effect", {}).get("guardUnchanged") is not True:
                errors.append("unchanged guard is not recorded")
            for forbidden in (
                "DMA isolation",
                "dual-Guest execution",
                "Linux and Zephyr IP connectivity",
            ):
                if forbidden not in result.get("doesNotProve", []):
                    errors.append(
                        f"result overstates the evidence by omitting {forbidden!r}"
                    )
            if (
                _run([*common, "--output", str(output_path)]) == 0
                or output_path.read_bytes() != result_bytes
            ):
                errors.append("validator overwrites existing output")
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    missing_raw = validator.main(
                        [
                            "--request",
                            str(request_path),
                            "--session",
                            str(session_path),
                            "--expected-payload",
                            str(expected_path),
                            "--before-guard",
                            str(before_guard_path),
                            "--after-guard",
                            str(after_guard_path),
                            "--before-payload",
                            str(before_payload_path),
                            "--after-payload",
                            str(after_payload_path),
                            "--output",
                            str(directory / "missing-raw.json"),
                        ]
                    )
                except SystemExit as error:
                    missing_raw = int(error.code)
            if missing_raw == 0:
                errors.append(
                    "validator accepts a session without an independently supplied raw log"
                )

        changed_guard = directory / "changed-guard.bin"
        changed_guard.write_bytes(b"\x00" + guard[1:])
        changed_session = directory / "changed-session.json"
        changed_output = directory / "changed-output.json"
        changed_value = json.loads(json.dumps(session_value))
        changed_value["measurements"][2]["source"] = _source(
            changed_guard, changed_guard.read_bytes()
        )
        changed_session.write_bytes(json.dumps(changed_value, sort_keys=True).encode())
        changed = [
            "--request",
            str(request_path),
            "--session",
            str(changed_session),
            "--expected-payload",
            str(expected_path),
            "--before-guard",
            str(before_guard_path),
            "--after-guard",
            str(changed_guard),
            "--before-payload",
            str(before_payload_path),
            "--after-payload",
            str(after_payload_path),
            "--output",
            str(changed_output),
        ]
        if _run(changed) != 0:
            errors.append("validator rejects an explicit out-of-bounds observation")
        elif (
            json.loads(changed_output.read_bytes()).get("status")
            != "controlled_virtio_blk_out_of_bounds_effect_observed"
        ):
            errors.append(
                "changed guard is not classified as an explicit out-of-bounds observation"
            )

        bad_ops_session = directory / "bad-ops.json"
        bad_ops_output = directory / "bad-ops-output.json"
        bad_ops = json.loads(json.dumps(session_value))
        bad_ops["qmp"]["operations"].append("memory-write")
        bad_ops_session.write_bytes(json.dumps(bad_ops, sort_keys=True).encode())
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(bad_ops_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(bad_ops_output),
                ]
            )
            == 0
        ):
            errors.append("validator accepts a QMP memory-write operation")

        overlap_request = directory / "overlap-request.json"
        overlap_session = directory / "overlap-session.json"
        overlap_output = directory / "overlap-output.json"
        overlap_value = json.loads(json.dumps(request_value))
        overlap_value["request"]["payload"]["gpa"] = 0x180000000
        overlap_value["request"]["payload"]["hpa"] = 0x180000000
        overlap_bytes = json.dumps(overlap_value, sort_keys=True).encode()
        overlap_request.write_bytes(overlap_bytes)
        overlap_session_value = json.loads(json.dumps(session_value))
        overlap_session_value["request"] = _source(overlap_request, overlap_bytes)
        overlap_session.write_bytes(
            json.dumps(overlap_session_value, sort_keys=True).encode()
        )
        if (
            _run(
                [
                    "--request",
                    str(overlap_request),
                    "--session",
                    str(overlap_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(overlap_output),
                ]
            )
            == 0
        ):
            errors.append("validator accepts a payload range overlapping the guard")

        bad_effect = directory / "bad-effect.bin"
        bad_effect_output = directory / "bad-effect-output.json"
        bad_effect.write_bytes(before_payload)
        bad_effect_session = directory / "bad-effect-session.json"
        bad_effect_value = json.loads(json.dumps(session_value))
        bad_effect_value["measurements"][3]["source"] = _source(
            bad_effect, before_payload
        )
        bad_effect_session.write_bytes(
            json.dumps(bad_effect_value, sort_keys=True).encode()
        )
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(bad_effect_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(bad_effect),
                    "--output",
                    str(bad_effect_output),
                ]
            )
            == 0
        ):
            errors.append(
                "validator accepts an after-payload that does not match expected bytes"
            )

        bad_identity_session = directory / "bad-identity-session.json"
        bad_identity_output = directory / "bad-identity-output.json"
        bad_identity = json.loads(json.dumps(session_value))
        bad_identity["qmp"]["queryName"] = f"axvisor-dma-effect-{'f' * 32}"
        bad_identity_session.write_bytes(
            json.dumps(bad_identity, sort_keys=True).encode()
        )
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(bad_identity_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(bad_identity_output),
                ]
            )
            == 0
        ):
            errors.append(
                "validator accepts a QMP query-name not bound to the request nonce"
            )

        bad_hash_session = directory / "bad-hash-session.json"
        bad_hash_output = directory / "bad-hash-output.json"
        bad_hash = json.loads(json.dumps(session_value))
        bad_hash["measurements"][0]["source"]["sha256"] = "0" * 64
        bad_hash_session.write_bytes(json.dumps(bad_hash, sort_keys=True).encode())
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(bad_hash_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(bad_hash_output),
                ]
            )
            == 0
        ):
            errors.append(
                "validator accepts a capture source hash that does not bind raw bytes"
            )

        bad_log_binding_session = directory / "bad-log-binding-session.json"
        bad_log_binding_output = directory / "bad-log-binding-output.json"
        bad_log_binding = json.loads(json.dumps(session_value))
        bad_log_binding["rawLog"]["sha256"] = "0" * 64
        bad_log_binding_session.write_bytes(
            json.dumps(bad_log_binding, sort_keys=True).encode()
        )
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(bad_log_binding_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(bad_log_binding_output),
                ]
            )
            == 0
        ):
            errors.append(
                "validator accepts a raw-log source hash that does not bind supplied bytes"
            )

        bad_control_session = directory / "bad-control-session.json"
        bad_control_output = directory / "bad-control-output.json"
        bad_control = json.loads(json.dumps(session_value))
        bad_control["guestControl"]["command"] = f"GO {'f' * 32}\n"
        bad_control_session.write_bytes(
            json.dumps(bad_control, sort_keys=True).encode()
        )
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(bad_control_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(bad_control_output),
                ]
            )
            == 0
        ):
            errors.append(
                "validator accepts a launcher stdin control command not bound to the nonce"
            )

        duplicate_log = directory / "duplicate-ready.log"
        duplicate_log.write_bytes(raw_log + ready_marker + b"\n")
        duplicate_log_session = directory / "duplicate-ready-session.json"
        duplicate_log_output = directory / "duplicate-ready-output.json"
        duplicate_session_value = json.loads(json.dumps(session_value))
        duplicate_session_value["rawLog"] = _source(
            duplicate_log, duplicate_log.read_bytes()
        )
        duplicate_log_session.write_bytes(
            json.dumps(duplicate_session_value, sort_keys=True).encode()
        )
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(duplicate_log_session),
                    "--raw-log",
                    str(duplicate_log),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(duplicate_log_output),
                ]
            )
            == 0
        ):
            errors.append("validator accepts duplicate READY marker observations")

        bad_address_session = directory / "bad-address-session.json"
        bad_address_output = directory / "bad-address-output.json"
        bad_address = json.loads(json.dumps(session_value))
        bad_address["measurements"][3]["hpa"] += 512
        bad_address_session.write_bytes(
            json.dumps(bad_address, sort_keys=True).encode()
        )
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(bad_address_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(bad_address_output),
                ]
            )
            == 0
        ):
            errors.append(
                "validator accepts a capture address outside the bound payload range"
            )

        bad_mapping_session = directory / "bad-mapping-session.json"
        bad_mapping_output = directory / "bad-mapping-output.json"
        bad_mapping = json.loads(json.dumps(session_value))
        bad_mapping["mapping"]["payloadHpa"] += 512
        bad_mapping_session.write_bytes(
            json.dumps(bad_mapping, sort_keys=True).encode()
        )
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(bad_mapping_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(bad_mapping_output),
                ]
            )
            == 0
        ):
            errors.append(
                "validator accepts a session mapping not bound to request payload GPA/HPA"
            )

        nonidentity_request = directory / "nonidentity-request.json"
        nonidentity_session = directory / "nonidentity-session.json"
        nonidentity_output = directory / "nonidentity-output.json"
        nonidentity_value = json.loads(json.dumps(request_value))
        nonidentity_value["request"]["payload"]["gpa"] += 512
        nonidentity_bytes = json.dumps(nonidentity_value, sort_keys=True).encode()
        nonidentity_request.write_bytes(nonidentity_bytes)
        nonidentity_session_value = json.loads(json.dumps(session_value))
        nonidentity_session_value["request"] = _source(
            nonidentity_request, nonidentity_bytes
        )
        nonidentity_session.write_bytes(
            json.dumps(nonidentity_session_value, sort_keys=True).encode()
        )
        if (
            _run(
                [
                    "--request",
                    str(nonidentity_request),
                    "--session",
                    str(nonidentity_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(nonidentity_output),
                ]
            )
            == 0
        ):
            errors.append("validator accepts non-identity payload GPA/HPA")

        mapalloc_request = directory / "mapalloc-request.json"
        mapalloc_session = directory / "mapalloc-session.json"
        mapalloc_output = directory / "mapalloc-output.json"
        mapalloc_value = json.loads(json.dumps(request_value))
        mapalloc_value["request"]["boundMapReservedRegion"]["mapType"] = 0
        mapalloc_bytes = json.dumps(mapalloc_value, sort_keys=True).encode()
        mapalloc_request.write_bytes(mapalloc_bytes)
        mapalloc_session_value = json.loads(json.dumps(session_value))
        mapalloc_session_value["request"] = _source(mapalloc_request, mapalloc_bytes)
        mapalloc_session.write_bytes(
            json.dumps(mapalloc_session_value, sort_keys=True).encode()
        )
        if (
            _run(
                [
                    "--request",
                    str(mapalloc_request),
                    "--session",
                    str(mapalloc_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(mapalloc_output),
                ]
            )
            == 0
        ):
            errors.append("validator accepts a non-MapReserved mapping")

        monitor_session = directory / "monitor-session.json"
        monitor_output = directory / "monitor-output.json"
        monitor = json.loads(json.dumps(session_value))
        monitor["qmp"]["operations"][-1] = "human-monitor-command"
        monitor_session.write_bytes(json.dumps(monitor, sort_keys=True).encode())
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(monitor_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(monitor_output),
                ]
            )
            == 0
        ):
            errors.append("validator accepts a human-monitor QMP operation")

        old_window_session = directory / "old-window-session.json"
        old_window_output = directory / "old-window-output.json"
        old_window = json.loads(json.dumps(session_value))
        old_window["qmp"]["states"] = ["running", "paused", "running"]
        old_window_session.write_bytes(json.dumps(old_window, sort_keys=True).encode())
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(old_window_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(old_window_output),
                ]
            )
            == 0
        ):
            errors.append("validator accepts the obsolete single-paused-window session")

        reordered_session = directory / "reordered-session.json"
        reordered_output = directory / "reordered-output.json"
        reordered = json.loads(json.dumps(session_value))
        reordered["measurements"][0], reordered["measurements"][1] = (
            reordered["measurements"][1],
            reordered["measurements"][0],
        )
        reordered_session.write_bytes(json.dumps(reordered, sort_keys=True).encode())
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(reordered_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(reordered_output),
                ]
            )
            == 0
        ):
            errors.append(
                "validator accepts reordered before/after capture measurements"
            )

        misplaced_marker_session = directory / "misplaced-marker-session.json"
        misplaced_marker_output = directory / "misplaced-marker-output.json"
        misplaced_marker = json.loads(json.dumps(session_value))
        misplaced_marker["timeline"][4], misplaced_marker["timeline"][6] = (
            misplaced_marker["timeline"][6],
            misplaced_marker["timeline"][4],
        )
        misplaced_marker_session.write_bytes(
            json.dumps(misplaced_marker, sort_keys=True).encode()
        )
        if (
            _run(
                [
                    "--request",
                    str(request_path),
                    "--session",
                    str(misplaced_marker_session),
                    "--expected-payload",
                    str(expected_path),
                    "--before-guard",
                    str(before_guard_path),
                    "--after-guard",
                    str(after_guard_path),
                    "--before-payload",
                    str(before_payload_path),
                    "--after-payload",
                    str(after_payload_path),
                    "--output",
                    str(misplaced_marker_output),
                ]
            )
            == 0
        ):
            errors.append(
                "validator accepts a request marker outside the two capture windows"
            )
    finally:
        shutil.rmtree(directory)
        if made_results:
            results.rmdir()
    if errors:
        print("Virtio DMA-effect evidence contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Virtio DMA-effect evidence contract passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
