#!/usr/bin/env python3
"""Validate one narrowly scoped virtio-blk DMA-effect observation.

The runner that produces the inputs may use QMP ``pmemsave`` only to *read*
four host-physical byte ranges before and after one Guest-issued virtio-blk
read.  This tool does not connect to QMP, start QEMU, or write guest memory.
It merely binds immutable byte captures to a nonce-bound request/session and
reports either an unchanged guard or an explicitly observed guard change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from host_vm_carveout_io import publish_new_file, read_regular_bytes


MAX_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_QMP_ADDRESS = (1 << 63) - 1
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
NONCE = re.compile(r"[0-9a-f]{32}\Z")
DEVICE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
BUS = re.compile(r"virtio-mmio-bus\.[0-9]+\Z")
GUEST_DEVICE = re.compile(r"/dev/[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
CONTROL_SOCKET = re.compile(r"/tmp/axdma-[0-9a-f]{32}/control\.sock\Z")
GIT_HEAD = re.compile(r"[0-9a-f]{40,64}\Z")


class VirtioDmaEffectError(ValueError):
    """Inputs cannot establish the deliberately narrow effect observation."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise VirtioDmaEffectError(f"duplicate JSON member {key!r}")
        value[key] = item
    return value


def _json(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"), object_pairs_hook=_pairs
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VirtioDmaEffectError(
            f"{label} is not strict duplicate-free UTF-8 JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise VirtioDmaEffectError(f"{label} must be a JSON object")
    return value


def _uint(value: object, *, label: str, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VirtioDmaEffectError(f"{label} must be an unsigned integer")
    if positive and value == 0:
        raise VirtioDmaEffectError(f"{label} must be non-zero")
    return value


def _sha_field(value: object, *, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise VirtioDmaEffectError(f"{label} must be a lower-case SHA-256 digest")
    return value


def _nonce(value: object, *, label: str) -> str:
    if not isinstance(value, str) or NONCE.fullmatch(value) is None:
        raise VirtioDmaEffectError(
            f"{label} must be a 128-bit lower-case hexadecimal nonce"
        )
    return value


def _source(value: object, *, label: str, path: Path, data: bytes) -> None:
    if not isinstance(value, dict):
        raise VirtioDmaEffectError(f"{label} must be an object")
    if value.get("path") != path.name:
        raise VirtioDmaEffectError(f"{label}.path must be the supplied basename")
    if _uint(value.get("size"), label=f"{label}.size") != len(data):
        raise VirtioDmaEffectError(f"{label}.size does not bind supplied bytes")
    if _sha_field(value.get("sha256"), label=f"{label}.sha256") != _sha(data):
        raise VirtioDmaEffectError(f"{label}.sha256 does not bind supplied bytes")


def _prepared_source(value: object, *, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"path", "size", "sha256"}:
        raise VirtioDmaEffectError(f"{label} must contain only path/size/sha256")
    if not isinstance(value.get("path"), str) or not value["path"]:
        raise VirtioDmaEffectError(f"{label}.path must be non-empty")
    _uint(value.get("size"), label=f"{label}.size")
    _sha_field(value.get("sha256"), label=f"{label}.sha256")


def _device(value: object, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "id",
        "bus",
        "driveId",
        "guestPath",
    }:
        raise VirtioDmaEffectError(
            f"{label} must contain only kind/id/bus/driveId/guestPath"
        )
    if value.get("kind") != "virtio-blk-device":
        raise VirtioDmaEffectError(f"{label}.kind must be virtio-blk-device")
    device_id = value.get("id")
    bus = value.get("bus")
    guest_path = value.get("guestPath")
    drive_id = value.get("driveId")
    if not isinstance(device_id, str) or DEVICE_ID.fullmatch(device_id) is None:
        raise VirtioDmaEffectError(f"{label}.id is malformed")
    if not isinstance(bus, str) or BUS.fullmatch(bus) is None:
        raise VirtioDmaEffectError(f"{label}.bus must identify one virtio-mmio bus")
    if not isinstance(guest_path, str) or GUEST_DEVICE.fullmatch(guest_path) is None:
        raise VirtioDmaEffectError(
            f"{label}.guestPath must be one safe absolute /dev path"
        )
    if not isinstance(drive_id, str) or DEVICE_ID.fullmatch(drive_id) is None:
        raise VirtioDmaEffectError(f"{label}.driveId is malformed")
    return {
        "kind": "virtio-blk-device",
        "id": device_id,
        "bus": bus,
        "driveId": drive_id,
        "guestPath": guest_path,
    }


def _control(value: object, *, label: str, nonce: str) -> dict[str, str]:
    keys = {
        "kind",
        "chardevId",
        "serialDeviceId",
        "serialBus",
        "portId",
        "portName",
        "guestPath",
        "socketPath",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise VirtioDmaEffectError(f"{label} has unexpected control fields")
    expected = {
        "kind": "virtio-console",
        "chardevId": "dma-go-chardev",
        "serialDeviceId": "dma-go-serial",
        "serialBus": "virtio-mmio-bus.2",
        "portId": "dma-go-port",
        "portName": "dma-go",
        "guestPath": "/dev/hvc0",
        "socketPath": f"/tmp/axdma-{nonce}/control.sock",
    }
    if (
        value != expected
        or CONTROL_SOCKET.fullmatch(str(value.get("socketPath"))) is None
    ):
        raise VirtioDmaEffectError(
            f"{label} is not the exact nonce-bound virtio-console control"
        )
    return expected


def _range(value: object, *, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"hpa", "size"}:
        raise VirtioDmaEffectError(f"{label} must contain only hpa/size")
    hpa = _uint(value.get("hpa"), label=f"{label}.hpa", positive=True)
    size = _uint(value.get("size"), label=f"{label}.size", positive=True)
    if size > MAX_CAPTURE_BYTES:
        raise VirtioDmaEffectError(
            f"{label}.size exceeds {MAX_CAPTURE_BYTES} byte capture limit"
        )
    if hpa > MAX_QMP_ADDRESS or size - 1 > MAX_QMP_ADDRESS - hpa:
        raise VirtioDmaEffectError(f"{label} exceeds QMP int64 host-physical range")
    return {"hpa": hpa, "size": size}


def _payload(value: object, *, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"gpa", "hpa", "size"}:
        raise VirtioDmaEffectError(f"{label} must contain only gpa/hpa/size")
    gpa = _uint(value.get("gpa"), label=f"{label}.gpa", positive=True)
    hpa = _uint(value.get("hpa"), label=f"{label}.hpa", positive=True)
    size = _uint(value.get("size"), label=f"{label}.size", positive=True)
    if gpa != hpa or gpa % 512 or size % 512:
        raise VirtioDmaEffectError(
            f"{label} must be a 512-byte aligned identity GPA/HPA range"
        )
    if (
        size > MAX_CAPTURE_BYTES
        or hpa > MAX_QMP_ADDRESS
        or size - 1 > MAX_QMP_ADDRESS - hpa
    ):
        raise VirtioDmaEffectError(f"{label} exceeds the bounded QMP capture range")
    return {"gpa": gpa, "hpa": hpa, "size": size}


def _map_reserved_region(
    value: object, *, label: str, payload: dict[str, int]
) -> dict[str, object]:
    expected = {"gpa", "hpa", "size", "flags", "mapType", "identity"}
    if not isinstance(value, dict) or set(value) != expected:
        raise VirtioDmaEffectError(
            f"{label} must contain the exact MapReserved mapping fields"
        )
    gpa = _uint(value.get("gpa"), label=f"{label}.gpa", positive=True)
    hpa = _uint(value.get("hpa"), label=f"{label}.hpa", positive=True)
    size = _uint(value.get("size"), label=f"{label}.size", positive=True)
    flags = _uint(value.get("flags"), label=f"{label}.flags")
    if value.get("mapType") != 2 or value.get("identity") is not True or gpa != hpa:
        raise VirtioDmaEffectError(
            f"{label} must describe an identity MapReserved(kind=2) region"
        )
    if size - 1 > MAX_QMP_ADDRESS - hpa or not (
        hpa <= payload["hpa"] and payload["hpa"] + payload["size"] <= hpa + size
    ):
        raise VirtioDmaEffectError(
            f"{label} does not fully contain the payload identity range"
        )
    return {
        "gpa": gpa,
        "hpa": hpa,
        "size": size,
        "flags": flags,
        "mapType": 2,
        "identity": True,
    }


def _overlaps(first: dict[str, int], second: dict[str, int]) -> bool:
    return (
        first["hpa"] < second["hpa"] + second["size"]
        and second["hpa"] < first["hpa"] + first["size"]
    )


def _request(
    value: dict[str, Any], *, expected_payload: Path, expected: bytes
) -> tuple[str, str, dict[str, str], dict[str, Any]]:
    expected_keys = {
        "schemaVersion",
        "artifactStatus",
        "status",
        "proofScope",
        "sessionNonce",
        "qemuName",
        "doesNotProve",
        "repository",
        "inputs",
        "device",
        "control",
        "request",
        "guestMarkers",
        "expectedPayload",
        "qmpCaptureTemplate",
    }
    if set(value) != expected_keys:
        raise VirtioDmaEffectError("request manifest has unexpected or missing fields")
    if value.get("schemaVersion") != 1:
        raise VirtioDmaEffectError("request schemaVersion must be 1")
    if value.get("artifactStatus") != "request-prepared":
        raise VirtioDmaEffectError("request artifactStatus must be request-prepared")
    if value.get("status") != "single_virtio_blk_read_dma_effect_request_prepared":
        raise VirtioDmaEffectError(
            "request status is not the single virtio-blk read request"
        )
    if (
        value.get("proofScope")
        != "one-virtio-blk-read-request-bound-to-one-qmp-session"
    ):
        raise VirtioDmaEffectError("request proofScope is unexpected")
    required_boundary = {
        "the request was submitted or completed",
        "DMA was executed",
        "DMA isolation",
        "dual-Guest execution",
        "Linux and Zephyr IP connectivity",
    }
    if (
        not isinstance(value.get("doesNotProve"), list)
        or set(value["doesNotProve"]) != required_boundary
    ):
        raise VirtioDmaEffectError(
            "request doesNotProve must retain the exact non-execution boundary"
        )
    repository = value.get("repository")
    if not isinstance(repository, dict) or set(repository) != {
        "worktreeRoot",
        "gitHead",
        "worktreeState",
    }:
        raise VirtioDmaEffectError("request repository binding is malformed")
    if (
        not isinstance(repository.get("worktreeRoot"), str)
        or not repository["worktreeRoot"]
    ):
        raise VirtioDmaEffectError("request repository worktreeRoot must be non-empty")
    if (
        not isinstance(repository.get("gitHead"), str)
        or GIT_HEAD.fullmatch(repository["gitHead"]) is None
    ):
        raise VirtioDmaEffectError("request repository gitHead is malformed")
    if repository.get("worktreeState") not in {"clean", "dirty"}:
        raise VirtioDmaEffectError("request repository worktreeState is malformed")
    inputs = value.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {
        "qemuConfig",
        "buildConfig",
        "vmConfig",
    }:
        raise VirtioDmaEffectError("request inputs must bind QEMU/build/VM configs")
    for key, item in inputs.items():
        _prepared_source(item, label=f"request.inputs.{key}")
    nonce = _nonce(value.get("sessionNonce"), label="request.sessionNonce")
    qemu_name = value.get("qemuName")
    if qemu_name != f"axvisor-dma-effect-{nonce}":
        raise VirtioDmaEffectError("request.qemuName does not bind the request nonce")
    device = _device(value.get("device"), label="request.device")
    control = _control(value.get("control"), label="request.control", nonce=nonce)
    request = value.get("request")
    if not isinstance(request, dict) or set(request) != {
        "requestId",
        "operation",
        "sector",
        "vmId",
        "payload",
        "guard",
        "boundMapReservedRegion",
    }:
        raise VirtioDmaEffectError("request.request has unexpected or missing fields")
    if (
        request.get("requestId") != f"virtio-blk-read-{nonce}"
        or request.get("operation") != "read"
    ):
        raise VirtioDmaEffectError("request must be one nonce-bound virtio-blk read")
    _uint(request.get("sector"), label="request.request.sector")
    _uint(request.get("vmId"), label="request.request.vmId", positive=True)
    payload = _payload(request.get("payload"), label="request.request.payload")
    guard = _range(request.get("guard"), label="request.request.guard")
    if payload["size"] != len(expected) or len(expected) != 512:
        raise VirtioDmaEffectError(
            "request payload must exactly match one 512-byte expected sector"
        )
    if _overlaps(payload, guard):
        raise VirtioDmaEffectError(
            "request payload must be strictly outside the guard range"
        )
    region = _map_reserved_region(
        request.get("boundMapReservedRegion"),
        label="request.request.boundMapReservedRegion",
        payload=payload,
    )
    markers = value.get("guestMarkers")
    expected_ready = f"AXVISOR_VIRTIO_BLK_ODIRECT_READY nonce={nonce} gpa={payload['gpa']:#x} size=512 sector={request['sector']} device={device['guestPath']} control_device={control['guestPath']}"
    expected_done = f"AXVISOR_VIRTIO_BLK_ODIRECT_DONE nonce={nonce} bytes=512 gpa={payload['gpa']:#x}"
    if not isinstance(markers, dict) or markers != {
        "ready": expected_ready,
        "done": expected_done,
    }:
        raise VirtioDmaEffectError(
            "request guest markers do not bind nonce/GPA/sector/device"
        )
    _source(
        value.get("expectedPayload"),
        label="request.expectedPayload",
        path=expected_payload,
        data=expected,
    )
    template = value.get("qmpCaptureTemplate")
    if not isinstance(template, dict) or set(template) != {
        "allowedOperations",
        "measurements",
    }:
        raise VirtioDmaEffectError("request qmpCaptureTemplate is malformed")
    if template.get("allowedOperations") != [
        "query-name",
        "query-status",
        "stop",
        "cont",
        "pmemsave",
    ]:
        raise VirtioDmaEffectError(
            "request qmpCaptureTemplate allows a write-capable QMP operation"
        )
    measurements = template.get("measurements")
    if not isinstance(measurements, list) or len(measurements) != 4:
        raise VirtioDmaEffectError(
            "request qmpCaptureTemplate must contain four captures"
        )
    expected_captures = [
        ("before", "guard", guard, "before-guard.bin"),
        ("before", "payload", payload, "before-payload.bin"),
        ("after", "guard", guard, "after-guard.bin"),
        ("after", "payload", payload, "after-payload.bin"),
    ]
    for index, (phase, target, expected_range, filename) in enumerate(
        expected_captures
    ):
        item = measurements[index]
        if not isinstance(item, dict) or set(item) != {
            "phase",
            "target",
            "operation",
            "addressSpace",
            "hpa",
            "size",
            "outputFileName",
        }:
            raise VirtioDmaEffectError(f"request capture template {index} is malformed")
        if (
            item.get("phase") != phase
            or item.get("target") != target
            or item.get("operation") != "pmemsave"
            or item.get("addressSpace") != "host-physical"
            or item.get("outputFileName") != filename
        ):
            raise VirtioDmaEffectError(
                f"request capture template {index} does not match the fixed read-only capture plan"
            )
        if (
            _uint(
                item.get("hpa"),
                label=f"request capture template {index}.hpa",
                positive=True,
            )
            != expected_range["hpa"]
            or _uint(
                item.get("size"),
                label=f"request capture template {index}.size",
                positive=True,
            )
            != expected_range["size"]
        ):
            raise VirtioDmaEffectError(
                f"request capture template {index} does not bind the request range"
            )
    return (
        nonce,
        qemu_name,
        device,
        {
            "requestId": request["requestId"],
            "guestMarkers": markers,
            "sector": request["sector"],
            "vmId": request["vmId"],
            "payload": payload,
            "guard": guard,
            "boundMapReservedRegion": region,
            "control": control,
        },
    )


def _measurement(
    value: object,
    *,
    label: str,
    phase: str,
    target: str,
    expected_range: dict[str, int],
    source_path: Path,
    source_bytes: bytes,
) -> None:
    expected_keys = {
        "phase",
        "target",
        "operation",
        "addressSpace",
        "hpa",
        "size",
        "source",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise VirtioDmaEffectError(f"{label} has unexpected or missing fields")
    if value.get("phase") != phase or value.get("target") != target:
        raise VirtioDmaEffectError(f"{label} has the wrong phase or target")
    if (
        value.get("operation") != "pmemsave"
        or value.get("addressSpace") != "host-physical"
    ):
        raise VirtioDmaEffectError(f"{label} is not a read-only host-physical pmemsave")
    if (
        _uint(value.get("hpa"), label=f"{label}.hpa", positive=True)
        != expected_range["hpa"]
    ):
        raise VirtioDmaEffectError(
            f"{label}.hpa does not match the bound request range"
        )
    if (
        _uint(value.get("size"), label=f"{label}.size", positive=True)
        != expected_range["size"]
    ):
        raise VirtioDmaEffectError(
            f"{label}.size does not match the bound request range"
        )
    _source(
        value.get("source"),
        label=f"{label}.source",
        path=source_path,
        data=source_bytes,
    )


def _marker_observation(
    raw_log: bytes, *, marker: str, label: str
) -> dict[str, object]:
    expected = marker.encode("ascii")
    matches: list[dict[str, object]] = []
    offset = 0
    for line_number, raw_line in enumerate(raw_log.splitlines(keepends=True), start=1):
        if raw_line.endswith(b"\r\n"):
            line = raw_line[:-2]
        elif raw_line.endswith(b"\n"):
            line = raw_line[:-1]
        else:
            line = raw_line
        if line == expected:
            matches.append(
                {"line": line_number, "byteOffset": offset, "marker": marker}
            )
        offset += len(raw_line)
    if len(matches) != 1:
        raise VirtioDmaEffectError(f"raw log must contain exactly one {label} marker")
    return matches[0]


def _session(
    value: dict[str, Any],
    *,
    request_path: Path,
    request_bytes: bytes,
    nonce: str,
    qemu_name: str,
    device: dict[str, str],
    request: dict[str, Any],
    raw_log: tuple[Path, bytes],
    before_guard: tuple[Path, bytes],
    after_guard: tuple[Path, bytes],
    before_payload: tuple[Path, bytes],
    after_payload: tuple[Path, bytes],
) -> None:
    expected_keys = {
        "schemaVersion",
        "artifactStatus",
        "status",
        "proofScope",
        "sessionNonce",
        "qemuName",
        "device",
        "request",
        "mapping",
        "rawLog",
        "markerObservations",
        "guestControl",
        "qmp",
        "timeline",
        "measurements",
    }
    if set(value) != expected_keys:
        raise VirtioDmaEffectError("session manifest has unexpected or missing fields")
    if (
        value.get("schemaVersion") != 1
        or value.get("artifactStatus") != "capture-generated-unreviewed"
    ):
        raise VirtioDmaEffectError(
            "session must be a schemaVersion 1 capture-generated artifact"
        )
    if (
        value.get("status")
        != "identity_bound_single_virtio_blk_dma_effect_session_completed"
    ):
        raise VirtioDmaEffectError("session status is unexpected")
    if (
        value.get("proofScope")
        != "same-session-one-virtio-blk-read-paused-qmp-pmemsave-measurements"
    ):
        raise VirtioDmaEffectError("session proofScope is unexpected")
    if value.get("sessionNonce") != nonce or value.get("qemuName") != qemu_name:
        raise VirtioDmaEffectError("session nonce or QEMU name disagrees with request")
    if _device(value.get("device"), label="session.device") != device:
        raise VirtioDmaEffectError("session device disagrees with request")
    _source(
        value.get("request"),
        label="session.request",
        path=request_path,
        data=request_bytes,
    )
    _source(
        value.get("rawLog"), label="session.rawLog", path=raw_log[0], data=raw_log[1]
    )
    ready_observation = _marker_observation(
        raw_log[1], marker=request["guestMarkers"]["ready"], label="READY"
    )
    done_observation = _marker_observation(
        raw_log[1], marker=request["guestMarkers"]["done"], label="DONE"
    )
    if int(ready_observation["byteOffset"]) >= int(done_observation["byteOffset"]):
        raise VirtioDmaEffectError("READY marker must precede DONE marker in raw log")
    if value.get("markerObservations") != {
        "ready": ready_observation,
        "done": done_observation,
    }:
        raise VirtioDmaEffectError(
            "session marker observations do not exactly bind unique raw-log READY/DONE markers"
        )
    guest_control = value.get("guestControl")
    command = f"GO {nonce}\n"
    expected_control = {
        "channel": "virtio-console-unix-socket",
        "control": request["control"],
        "command": command,
        "bytes": len(command.encode("ascii")),
        "sha256": _sha(command.encode("ascii")),
        "peer": {
            "pid": value.get("qmp", {}).get("peerPid"),
            "uid": value.get("qmp", {}).get("peerUid"),
        },
    }
    if guest_control != expected_control:
        raise VirtioDmaEffectError(
            "session guest control must be the exact virtio-console GO nonce command"
        )
    mapping = value.get("mapping")
    expected_mapping = {
        "payloadGpa": request["payload"]["gpa"],
        "payloadHpa": request["payload"]["hpa"],
        "payloadSize": request["payload"]["size"],
        "mapType": 2,
        "identity": True,
        "boundMapReservedRegion": request["boundMapReservedRegion"],
    }
    if mapping != expected_mapping:
        raise VirtioDmaEffectError(
            "session mapping evidence does not exactly bind the request MapReserved identity range"
        )
    qmp = value.get("qmp")
    if not isinstance(qmp, dict) or set(qmp) != {
        "capabilitiesNegotiated",
        "operations",
        "queryName",
        "states",
        "peerPid",
        "peerUid",
    }:
        raise VirtioDmaEffectError("session.qmp must bind QMP peer PID/UID")
    if qmp.get("capabilitiesNegotiated") is not True:
        raise VirtioDmaEffectError("session QMP capabilities were not negotiated")
    if qmp.get("operations") != [
        "query-name",
        "query-status",
        "stop",
        "cont",
        "pmemsave",
    ]:
        raise VirtioDmaEffectError(
            "session QMP operation set is not the read-only measurement allowlist"
        )
    if qmp.get("queryName") != qemu_name:
        raise VirtioDmaEffectError(
            "session QMP query-name does not bind the request QEMU name"
        )
    _uint(qmp.get("peerPid"), label="session.qmp.peerPid", positive=True)
    _uint(qmp.get("peerUid"), label="session.qmp.peerUid")
    if qmp.get("states") != ["running", "paused", "running", "paused", "running"]:
        raise VirtioDmaEffectError(
            "session QMP states must bind separate before/after paused capture windows"
        )
    timeline = value.get("timeline")
    expected_timeline = [
        {"event": "qmp-state", "state": "running"},
        {"event": "guest-ready-marker", "marker": "ready"},
        {"event": "qmp-state", "state": "paused"},
        {"event": "capture-window", "phase": "before", "measurementIndexes": [0, 1]},
        {"event": "qmp-state", "state": "running"},
        {
            "event": "guest-control",
            "channel": "virtio-console-unix-socket",
            "commandSha256": expected_control["sha256"],
        },
        {"event": "guest-done-marker", "marker": "done"},
        {"event": "qmp-state", "state": "paused"},
        {"event": "capture-window", "phase": "after", "measurementIndexes": [2, 3]},
        {"event": "qmp-state", "state": "running"},
    ]
    if timeline != expected_timeline:
        raise VirtioDmaEffectError(
            "session timeline must place the request completion marker between separate before/after capture windows"
        )
    measurements = value.get("measurements")
    if not isinstance(measurements, list) or len(measurements) != 4:
        raise VirtioDmaEffectError(
            "session must contain exactly four byte measurements"
        )
    sources = {
        ("before", "guard"): before_guard,
        ("after", "guard"): after_guard,
        ("before", "payload"): before_payload,
        ("after", "payload"): after_payload,
    }
    expected_measurements = [
        ("before", "guard"),
        ("before", "payload"),
        ("after", "guard"),
        ("after", "payload"),
    ]
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(measurements):
        if not isinstance(item, dict):
            raise VirtioDmaEffectError(f"measurement {index} is not an object")
        key = (item.get("phase"), item.get("target"))
        if key != expected_measurements[index]:
            raise VirtioDmaEffectError(
                "session measurements must retain fixed before-then-after capture order"
            )
        if key not in sources or key in seen:
            raise VirtioDmaEffectError(
                f"measurement {index} does not identify one expected phase/target"
            )
        seen.add(key)
        expected_range = request["guard"] if key[1] == "guard" else request["payload"]
        path, data = sources[key]
        _measurement(
            item,
            label=f"measurement {index}",
            phase=key[0],
            target=key[1],
            expected_range=expected_range,
            source_path=path,
            source_bytes=data,
        )
    if seen != set(sources):
        raise VirtioDmaEffectError("session omits one required byte measurement")


def _source_record(path: Path, data: bytes) -> dict[str, object]:
    return {"path": str(path), "size": len(data), "sha256": _sha(data)}


def validate(
    *,
    request_path: Path,
    request_bytes: bytes,
    session_path: Path,
    session_bytes: bytes,
    expected_payload_path: Path,
    expected_payload: bytes,
    before_guard: tuple[Path, bytes],
    after_guard: tuple[Path, bytes],
    raw_log: tuple[Path, bytes],
    before_payload: tuple[Path, bytes],
    after_payload: tuple[Path, bytes],
) -> dict[str, object]:
    if not expected_payload or len(expected_payload) > MAX_CAPTURE_BYTES:
        raise VirtioDmaEffectError(
            "expected payload must be non-empty and within capture limit"
        )
    request_value = _json(request_bytes, label="request manifest")
    nonce, qemu_name, device, request = _request(
        request_value, expected_payload=expected_payload_path, expected=expected_payload
    )
    for label, (_, data), expected_range in (
        ("before guard", before_guard, request["guard"]),
        ("after guard", after_guard, request["guard"]),
        ("before payload", before_payload, request["payload"]),
        ("after payload", after_payload, request["payload"]),
    ):
        if len(data) != expected_range["size"]:
            raise VirtioDmaEffectError(
                f"{label} byte count does not match its bound range"
            )
    _session(
        _json(session_bytes, label="session manifest"),
        request_path=request_path,
        request_bytes=request_bytes,
        nonce=nonce,
        qemu_name=qemu_name,
        device=device,
        request=request,
        raw_log=raw_log,
        before_guard=before_guard,
        after_guard=after_guard,
        before_payload=before_payload,
        after_payload=after_payload,
    )
    if before_payload[1] != b"\xa5" * len(expected_payload):
        raise VirtioDmaEffectError(
            "before-payload must bind the helper's exact 0xa5 initialization"
        )
    if after_payload[1] != expected_payload:
        raise VirtioDmaEffectError(
            "after-payload does not equal the exact expected payload"
        )
    guard_changed = before_guard[1] != after_guard[1]
    status = (
        "controlled_virtio_blk_dma_effect_observed"
        if not guard_changed
        else "controlled_virtio_blk_out_of_bounds_effect_observed"
    )
    return {
        "schemaVersion": 1,
        "artifactStatus": "derived-observation-unreviewed",
        "status": status,
        "proofScope": "one-virtio-blk-read-payload-and-guard-byte-observation",
        "doesNotProve": [
            "the observed bytes were written by a hardware DMA engine",
            "DMA isolation",
            "isolation for any other device, request, range, or session",
            "dual-Guest execution",
            "Linux and Zephyr IP connectivity",
        ],
        "binding": {
            "sessionNonce": nonce,
            "qemuName": qemu_name,
            "device": device,
            "request": request,
        },
        "mappingEvidence": {
            "payloadGpa": request["payload"]["gpa"],
            "payloadHpa": request["payload"]["hpa"],
            "payloadSize": request["payload"]["size"],
            "mapType": 2,
            "identity": True,
            "boundMapReservedRegion": request["boundMapReservedRegion"],
        },
        "guestControlEvidence": {
            "rawLog": _source_record(*raw_log),
            "markerObservations": _json(session_bytes, label="session manifest")[
                "markerObservations"
            ],
            "guestControl": _json(session_bytes, label="session manifest")[
                "guestControl"
            ],
        },
        "effect": {
            "payloadBeforeSha256": _sha(before_payload[1]),
            "payloadAfterSha256": _sha(after_payload[1]),
            "expectedPayloadSha256": _sha(expected_payload),
            "payloadMatchesExpected": True,
            "guardBeforeSha256": _sha(before_guard[1]),
            "guardAfterSha256": _sha(after_guard[1]),
            "guardUnchanged": not guard_changed,
            "observation": "in-bounds-payload-expected-guard-unchanged"
            if not guard_changed
            else "guard-changed-while-payload-range-is-strictly-outside-guard",
        },
        "sources": {
            "request": _source_record(request_path, request_bytes),
            "session": _source_record(session_path, session_bytes),
            "rawLog": _source_record(*raw_log),
            "expectedPayload": _source_record(expected_payload_path, expected_payload),
            "beforeGuard": _source_record(*before_guard),
            "afterGuard": _source_record(*after_guard),
            "beforePayload": _source_record(*before_payload),
            "afterPayload": _source_record(*after_payload),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate one bounded virtio-blk DMA-effect observation"
    )
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--raw-log", required=True, type=Path)
    parser.add_argument("--expected-payload", required=True, type=Path)
    parser.add_argument("--before-guard", required=True, type=Path)
    parser.add_argument("--after-guard", required=True, type=Path)
    parser.add_argument("--before-payload", required=True, type=Path)
    parser.add_argument("--after-payload", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        inputs = [
            args.request,
            args.session,
            args.raw_log,
            args.expected_payload,
            args.before_guard,
            args.after_guard,
            args.before_payload,
            args.after_payload,
        ]
        if len(set(inputs)) != len(inputs) or args.output in inputs:
            raise VirtioDmaEffectError(
                "all input paths and the output path must be distinct"
            )
        request = read_regular_bytes(
            args.request, label="request manifest", error_type=VirtioDmaEffectError
        )
        session = read_regular_bytes(
            args.session, label="session manifest", error_type=VirtioDmaEffectError
        )
        raw_log = (
            args.raw_log,
            read_regular_bytes(
                args.raw_log, label="raw log", error_type=VirtioDmaEffectError
            ),
        )
        expected = read_regular_bytes(
            args.expected_payload,
            label="expected payload",
            error_type=VirtioDmaEffectError,
        )
        before_guard = (
            args.before_guard,
            read_regular_bytes(
                args.before_guard, label="before guard", error_type=VirtioDmaEffectError
            ),
        )
        after_guard = (
            args.after_guard,
            read_regular_bytes(
                args.after_guard, label="after guard", error_type=VirtioDmaEffectError
            ),
        )
        before_payload = (
            args.before_payload,
            read_regular_bytes(
                args.before_payload,
                label="before payload",
                error_type=VirtioDmaEffectError,
            ),
        )
        after_payload = (
            args.after_payload,
            read_regular_bytes(
                args.after_payload,
                label="after payload",
                error_type=VirtioDmaEffectError,
            ),
        )
        result = validate(
            request_path=args.request,
            request_bytes=request,
            session_path=args.session,
            session_bytes=session,
            expected_payload_path=args.expected_payload,
            expected_payload=expected,
            raw_log=raw_log,
            before_guard=before_guard,
            after_guard=after_guard,
            before_payload=before_payload,
            after_payload=after_payload,
        )
        publish_new_file(
            args.output,
            (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode(),
            error_type=VirtioDmaEffectError,
        )
        return 0
    except (OSError, VirtioDmaEffectError) as error:
        print(f"virtio DMA-effect evidence validation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
