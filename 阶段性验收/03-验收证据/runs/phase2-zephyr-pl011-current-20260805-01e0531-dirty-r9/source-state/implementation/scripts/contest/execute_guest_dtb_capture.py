#!/usr/bin/env python3
"""Plan or execute bounded QMP pmemsave captures for final Guest DTBs."""

from __future__ import annotations

import argparse
import json
import math
import re
import runpy
import socket
import struct
import sys
from pathlib import Path
from typing import Any


MAX_TOTAL_CAPTURE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_QMP_COMMANDS = 8192
MAX_GUESTS = 8
MAX_QMP_INT = (1 << 63) - 1
MAX_QMP_MESSAGE_BYTES = 1024 * 1024
MAX_QMP_ASYNC_MESSAGES = 128
MAX_QMP_TIMEOUT_SECONDS = 300.0
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IO_API = runpy.run_path(str(Path(__file__).with_name("guest_dtb_capture_io.py")))
PLANNER_API = runpy.run_path(
    str(Path(__file__).with_name("plan_guest_dtb_capture.py"))
)

GuestDtbCaptureError = IO_API["GuestDtbCaptureError"]
MAX_CAPTURE_PLAN_BYTES = int(IO_API["MAX_CAPTURE_PLAN_BYTES"])
MAX_SOURCE_LOG_BYTES = int(IO_API["MAX_SOURCE_LOG_BYTES"])
decode_capture_plan = IO_API["decode_capture_plan"]
read_capture_plan = IO_API["read_capture_plan"]
FilesystemCaptureStorage = IO_API["FilesystemCaptureStorage"]
_sha256 = IO_API["sha256_bytes"]
_read_bounded_regular_file = IO_API["read_bounded_regular_file"]
_path_exists = IO_API["path_exists"]
_checked_lstat = IO_API["checked_lstat"]
_unique_json_object = IO_API["unique_json_object"]
_reject_json_constant = IO_API["reject_json_constant"]
_publish_json_new = IO_API["publish_json_new"]
_prepare_cli_paths = IO_API["prepare_cli_paths"]


def _plain_filename(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise GuestDtbCaptureError(f"{field} must be a plain filename")
    return value


def _read_log(
    directory: Path, filename: str, source_reader: Any | None
) -> bytes:
    if source_reader is None:
        data, _ = _read_bounded_regular_file(
            directory / filename,
            byte_limit=MAX_SOURCE_LOG_BYTES,
            field="AxVisor log",
        )
        return data
    try:
        data = source_reader(filename)
    except (KeyError, OSError) as error:
        raise GuestDtbCaptureError(
            f"could not read AxVisor log source {filename}: {error}"
        ) from error
    if not isinstance(data, bytes):
        raise GuestDtbCaptureError("AxVisor log source reader must return bytes")
    if len(data) > MAX_SOURCE_LOG_BYTES:
        raise GuestDtbCaptureError(
            f"AxVisor log exceeds byte limit {MAX_SOURCE_LOG_BYTES}"
        )
    return data


def _expected_vm_ids(plan: dict[str, Any]) -> list[int]:
    guests = plan.get("guests")
    if not isinstance(guests, list) or not guests or len(guests) > MAX_GUESTS:
        raise GuestDtbCaptureError("capture plan Guest list is outside bounds")
    vm_ids: list[int] = []
    for guest in guests:
        vm_id = guest.get("vmId") if isinstance(guest, dict) else None
        if not isinstance(vm_id, int) or isinstance(vm_id, bool) or vm_id <= 0:
            raise GuestDtbCaptureError("capture plan VM id must be a positive integer")
        vm_ids.append(vm_id)
    if len(set(vm_ids)) != len(vm_ids):
        raise GuestDtbCaptureError("capture plan repeats a VM id")
    return vm_ids


def validate_capture_plan(
    plan: dict[str, Any], evidence_directory: Path, *, source_reader: Any | None = None
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    source = plan.get("source")
    log = source.get("axvisorLog") if isinstance(source, dict) else None
    if not isinstance(log, dict):
        raise GuestDtbCaptureError("capture plan AxVisor log source is missing")
    log_name = _plain_filename(log.get("path"), field="AxVisor log path")
    expected_sha = log.get("sha256")
    if not isinstance(expected_sha, str) or not SHA256_PATTERN.fullmatch(expected_sha):
        raise GuestDtbCaptureError("AxVisor log SHA-256 is malformed")
    log_data = _read_log(evidence_directory.resolve(), log_name, source_reader)
    actual_sha = _sha256(log_data)
    if actual_sha != expected_sha:
        raise GuestDtbCaptureError("AxVisor log SHA-256 does not match capture plan")
    try:
        log_text = log_data.decode("utf-8", errors="strict")
        vm_ids = _expected_vm_ids(plan)
        markers = PLANNER_API["parse_guest_dtb_markers"](
            log_text, expected_vm_ids=set(vm_ids)
        )
        canonical = PLANNER_API["build_capture_plan"](
            markers=markers,
            source_log_name=log_name,
            source_log_sha256=actual_sha,
        )
    except (UnicodeError, ValueError) as error:
        raise GuestDtbCaptureError(
            f"capture plan does not match runtime markers: {error}"
        ) from error
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    plan_json = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    if plan_json != canonical_json:
        raise GuestDtbCaptureError("capture plan does not match runtime markers")

    normalized: list[dict[str, Any]] = []
    total_bytes = 0
    total_commands = 0
    for guest in canonical["guests"]:
        segments = []
        total_bytes += int(guest["size"])
        for segment in guest["segments"]:
            hpa = int(str(segment["hpa"]), 16)
            length = int(segment["length"])
            if hpa > MAX_QMP_INT or hpa + length - 1 > MAX_QMP_INT:
                raise GuestDtbCaptureError("capture HPA exceeds QMP int64 range")
            segments.append(
                {
                    "index": int(segment["index"]),
                    "hpa": hpa,
                    "hpaText": str(segment["hpa"]),
                    "length": length,
                    "fileName": str(segment["fileName"]),
                }
            )
            total_commands += 1
        normalized.append({**guest, "segments": segments})
    if total_bytes > MAX_TOTAL_CAPTURE_BYTES:
        raise GuestDtbCaptureError(
            f"capture plan exceeds total capture byte limit {MAX_TOTAL_CAPTURE_BYTES}"
        )
    if total_commands > MAX_TOTAL_QMP_COMMANDS:
        raise GuestDtbCaptureError(
            f"capture plan exceeds total QMP command limit {MAX_TOTAL_QMP_COMMANDS}"
        )
    return normalized, {"path": log_name, "sha256": actual_sha}


def build_qmp_command_plan(
    plan: dict[str, Any],
    evidence_directory: Path,
    *,
    capture_plan_name: str,
    capture_plan_sha256: str,
    source_reader: Any | None = None,
    staging_directory: Path | None = None,
) -> dict[str, Any]:
    directory = evidence_directory.resolve()
    plan_name = _plain_filename(capture_plan_name, field="capture plan name")
    if not SHA256_PATTERN.fullmatch(capture_plan_sha256):
        raise GuestDtbCaptureError("capture plan SHA-256 is malformed")
    guests, log_source = validate_capture_plan(
        plan, directory, source_reader=source_reader
    )
    if staging_directory is None:
        stage: Path | None = None
        stage_name = "${AXVISOR_QMP_STAGING}"
    else:
        stage = staging_directory.resolve()
        if stage.parent != directory:
            raise GuestDtbCaptureError("private staging directory escapes evidence bundle")
        stage_name = stage.name
    commands = []
    for guest in guests:
        vm_id = int(guest["vmId"])
        for segment in guest["segments"]:
            index = int(segment["index"])
            filename = str(segment["fileName"])
            request_id = f"guest-dtb-vm-{vm_id}-segment-{index:03d}"
            commands.append(
                {
                    "sequence": len(commands),
                    "vmId": vm_id,
                    "segmentIndex": index,
                    "hpa": str(segment["hpaText"]),
                    "length": int(segment["length"]),
                    "stagingFileName": filename,
                    "finalFileName": filename,
                    "request": {
                        "execute": "pmemsave",
                        "arguments": {
                            "val": int(segment["hpa"]),
                            "size": int(segment["length"]),
                            "filename": (
                                f"${{AXVISOR_QMP_STAGING}}/{filename}"
                                if stage is None
                                else str(stage / filename)
                            ),
                        },
                        "id": request_id,
                    },
                }
            )
    return {
        "schemaVersion": 1,
        "artifactStatus": "plan-generated-unreviewed",
        "status": "qmp_commands_planned",
        "proofScope": "final-guest-dtb-qmp-pmemsave-command-plan",
        "doesNotProve": [
            "physical bytes were captured",
            "captured bytes form valid flattened device trees",
            "outer QEMU dumpdtb output is a Guest DTB",
            "QMP socket belongs to the same QEMU run as the source log",
            "both guests booted",
            "passthrough DMA is isolated",
            "Linux and Zephyr have IP connectivity",
        ],
        "publication": "private staging; exact-size validation; manifest last",
        "sourcePathBase": "artifact parent directory",
        "source": {
            "capturePlan": {"path": plan_name, "sha256": capture_plan_sha256},
            "axvisorLog": log_source,
        },
        "stagingDirectory": stage_name,
        "guests": guests,
        "commands": commands,
    }


def _validated_qmp_response(response: object, request: dict[str, Any]) -> None:
    if not isinstance(response, dict) or response.get("id") != request.get("id"):
        raise GuestDtbCaptureError("QMP response has the wrong shape or id")
    if "error" in response:
        raise GuestDtbCaptureError(f"QMP request failed: {response['error']}")
    if "return" not in response:
        raise GuestDtbCaptureError("QMP response has no return value")


def _execution_result(command_plan: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    by_vm: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        by_vm.setdefault(int(record["vmId"]), []).append(record)
    guests = [
        {
            "vmId": int(guest["vmId"]),
            "gpa": guest["gpa"],
            "size": guest["size"],
            "segments": by_vm[int(guest["vmId"])],
        }
        for guest in command_plan["guests"]
    ]
    return {
        "schemaVersion": 1,
        "artifactStatus": "capture-generated-unreviewed",
        "status": "qmp_pmemsave_completed_exact_size",
        "proofScope": "final-guest-dtb-qmp-pmemsave-byte-capture",
        "doesNotProve": [
            "captured bytes form valid flattened device trees",
            "device-tree semantic validity",
            "captured device ownership matches the VM configuration",
            "QMP socket belongs to the same QEMU run as the source log",
            "both guests booted",
            "passthrough DMA is isolated",
            "Linux and Zephyr have IP connectivity",
        ],
        "publication": "segment files published first; this manifest published last",
        "sourcePathBase": "artifact parent directory",
        "source": command_plan["source"],
        "qmp": {
            "capabilitiesNegotiated": True,
            "operation": "pmemsave",
            "addressSpace": "host-physical",
        },
        "guests": guests,
    }


def _rollback(storage: Any, paths: list[Path], primary: Exception) -> None:
    errors = []
    for path in paths:
        try:
            storage.cleanup(path)
        except Exception as error:  # noqa: BLE001 - report every cleanup failure.
            errors.append(f"{path.name}: {error}")
    if errors:
        detail = "; ".join(errors)
        raise GuestDtbCaptureError(
            f"{primary}; rollback cleanup failed: {detail}"
        ) from primary


def execute_capture(
    plan: dict[str, Any],
    evidence_directory: Path,
    *,
    capture_plan_name: str,
    capture_plan_sha256: str,
    output_path: Path,
    session: Any,
    source_reader: Any | None = None,
    storage: Any | None = None,
    negotiate_session: bool = True,
) -> dict[str, Any]:
    directory = evidence_directory.resolve()
    if storage is None and not directory.is_dir():
        raise GuestDtbCaptureError("evidence directory is missing")
    capture_storage = storage or FilesystemCaptureStorage(directory)
    with capture_storage.transaction() as staging_directory:
        command_plan = build_qmp_command_plan(
            plan,
            directory,
            capture_plan_name=capture_plan_name,
            capture_plan_sha256=capture_plan_sha256,
            source_reader=source_reader,
            staging_directory=staging_directory,
        )
        if _path_exists(output_path):
            raise GuestDtbCaptureError("capture output already exists or is a link")
        result_path = output_path.resolve()
        if result_path.parent != directory:
            raise GuestDtbCaptureError("capture output escapes its evidence directory")
        manifest_stage = staging_directory / "capture-execution.manifest"
        staging_paths = [
            Path(str(command["request"]["arguments"]["filename"]))
            for command in command_plan["commands"]
        ]
        final_paths = [
            directory / str(command["finalFileName"])
            for command in command_plan["commands"]
        ]
        published: list[Path] = []
        try:
            capture_storage.ensure_absent([*final_paths, result_path])
            if negotiate_session:
                session.negotiate()
            records = []
            for command, staging_path in zip(command_plan["commands"], staging_paths):
                request = command["request"]
                response = session.execute(request)
                _validated_qmp_response(response, request)
                expected = int(command["length"])
                digest = capture_storage.validate_staging(staging_path, expected)
                records.append(
                    {
                        "vmId": int(command["vmId"]),
                        "index": int(command["segmentIndex"]),
                        "hpa": str(command["hpa"]),
                        "length": expected,
                        "path": str(command["finalFileName"]),
                        "sha256": digest,
                        "qmpOperation": "pmemsave",
                    }
                )
            result = _execution_result(command_plan, records)
            serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            manifest_bytes = serialized.encode("utf-8")
            capture_storage.write_staging(manifest_stage, manifest_bytes)
            for staging_path, final_path, record in zip(
                staging_paths, final_paths, records
            ):
                capture_storage.publish(
                    staging_path,
                    final_path,
                    expected_size=int(record["length"]),
                    expected_sha256=str(record["sha256"]),
                )
                published.append(final_path)
            capture_storage.sync_directory()
            capture_storage.publish(
                manifest_stage,
                result_path,
                expected_size=len(manifest_bytes),
                expected_sha256=_sha256(manifest_bytes),
            )
            published.append(result_path)
            capture_storage.sync_directory()
            return result
        except Exception as primary:  # noqa: BLE001 - rollback before normalization.
            _rollback(
                capture_storage,
                [manifest_stage, *staging_paths, *reversed(published)],
                primary,
            )
            if isinstance(primary, GuestDtbCaptureError):
                raise
            raise GuestDtbCaptureError(f"Guest DTB capture failed: {primary}") from primary


def publish_command_plan(payload: dict[str, Any], output_path: Path) -> None:
    _publish_json_new(payload, output_path)


class UnixQmpSession:
    """Minimal bounded client for an existing filesystem Unix QMP socket."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float = 10.0) -> None:
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > MAX_QMP_TIMEOUT_SECONDS
        ):
            raise GuestDtbCaptureError("QMP timeout must be finite and within bounds")
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds
        self._socket: socket.socket | None = None
        self._stream: Any | None = None
        self._negotiated = False

    def __enter__(self) -> "UnixQmpSession":
        _checked_lstat(self.socket_path, field="QMP socket", kind="socket")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout_seconds)
        try:
            connection.connect(str(self.socket_path))
            self._socket = connection
            self._stream = connection.makefile("rwb")
            greeting = self._read_message()
            if not isinstance(greeting.get("QMP"), dict):
                raise GuestDtbCaptureError("QMP greeting is missing the QMP object")
        except Exception:
            try:
                if self._stream is not None:
                    self._stream.close()
            finally:
                connection.close()
                self._stream = None
                self._socket = None
            raise
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
        finally:
            self._stream = None
            if self._socket is not None:
                self._socket.close()
                self._socket = None

    def _read_message(self) -> dict[str, Any]:
        if self._stream is None:
            raise GuestDtbCaptureError("QMP session is not connected")
        line = self._stream.readline(MAX_QMP_MESSAGE_BYTES + 1)
        if not line:
            raise GuestDtbCaptureError("QMP socket closed before a response")
        if len(line) > MAX_QMP_MESSAGE_BYTES:
            raise GuestDtbCaptureError("QMP response exceeds the message limit")
        try:
            message = json.loads(
                line.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError) as error:
            raise GuestDtbCaptureError(f"QMP response is malformed: {error}") from error
        if not isinstance(message, dict):
            raise GuestDtbCaptureError("QMP response is not an object")
        return message

    def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._stream is None:
            raise GuestDtbCaptureError("QMP session is not connected")
        encoded = json.dumps(request, separators=(",", ":")).encode() + b"\r\n"
        if len(encoded) > MAX_QMP_MESSAGE_BYTES:
            raise GuestDtbCaptureError("QMP request exceeds the message limit")
        self._stream.write(encoded)
        self._stream.flush()
        for _ in range(MAX_QMP_ASYNC_MESSAGES + 1):
            response = self._read_message()
            if "event" in response:
                continue
            if response.get("id") != request.get("id"):
                raise GuestDtbCaptureError("QMP response id does not match request")
            return response
        raise GuestDtbCaptureError("QMP emitted too many asynchronous events")

    def negotiate(self) -> None:
        if self._negotiated:
            raise GuestDtbCaptureError("QMP capabilities were already negotiated")
        request = {"execute": "qmp_capabilities", "id": "guest-dtb-capabilities"}
        response = self._request(request)
        _validated_qmp_response(response, request)
        self._negotiated = True

    def peer_credentials(self) -> tuple[int, int, int]:
        """Return the connected Unix peer PID/UID/GID (Linux SO_PEERCRED)."""

        if self._socket is None or not hasattr(socket, "SO_PEERCRED"):
            raise GuestDtbCaptureError("QMP peer credentials are unavailable")
        try:
            raw = self._socket.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
        except OSError as error:
            raise GuestDtbCaptureError(
                f"could not read QMP peer credentials: {error}"
            ) from error
        if len(raw) != struct.calcsize("3i"):
            raise GuestDtbCaptureError("QMP peer credentials have the wrong size")
        return struct.unpack("3i", raw)

    def execute_control(self, command: str, *, request_id: str) -> dict[str, Any]:
        """Execute one allow-listed control/query command on this session."""

        if not self._negotiated:
            raise GuestDtbCaptureError("QMP control request precedes negotiation")
        if command not in {"query-name", "query-status", "stop", "cont"}:
            raise GuestDtbCaptureError(f"QMP control command is not allowed: {command}")
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", request_id) is None:
            raise GuestDtbCaptureError("QMP control request id is malformed")
        request = {"execute": command, "id": request_id}
        response = self._request(request)
        _validated_qmp_response(response, request)
        return response

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._negotiated or request.get("execute") != "pmemsave":
            raise GuestDtbCaptureError("QMP executor accepts negotiated pmemsave only")
        return self._request(request)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan or execute Guest DTB pmemsave")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--commands-only", action="store_true")
    mode.add_argument("--qmp-socket", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan_path, output_path = _prepare_cli_paths(args.plan, args.output)
        plan, plan_sha = read_capture_plan(plan_path)
        if args.commands_only:
            payload = build_qmp_command_plan(
                plan,
                plan_path.parent,
                capture_plan_name=plan_path.name,
                capture_plan_sha256=plan_sha,
            )
            publish_command_plan(payload, output_path)
        else:
            with UnixQmpSession(
                args.qmp_socket, timeout_seconds=args.timeout_seconds
            ) as session:
                payload = execute_capture(
                    plan,
                    plan_path.parent,
                    capture_plan_name=plan_path.name,
                    capture_plan_sha256=plan_sha,
                    output_path=output_path,
                    session=session,
                )
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    except GuestDtbCaptureError as error:
        print(f"Final Guest DTB QMP capture failed: {error}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"Could not read/write Guest DTB QMP evidence: {error}", file=sys.stderr)
        return 2
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
