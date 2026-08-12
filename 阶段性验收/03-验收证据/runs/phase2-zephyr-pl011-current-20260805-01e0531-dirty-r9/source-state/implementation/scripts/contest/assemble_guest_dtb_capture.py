#!/usr/bin/env python3
"""Assemble bounded QMP segments and validate each final Guest DTB header."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import runpy
import struct
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


FDT_MAGIC = 0xD00DFEED
FDT_HEADER_SIZE = 40
MAX_DTB_CAPTURE_BYTES = 16 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IO_API = runpy.run_path(str(Path(__file__).with_name("guest_dtb_capture_io.py")))
PLANNER_API = runpy.run_path(
    str(Path(__file__).with_name("plan_guest_dtb_capture.py"))
)
IoContractError = IO_API["GuestDtbCaptureError"]
MAX_CAPTURE_PLAN_BYTES = int(IO_API["MAX_CAPTURE_PLAN_BYTES"])
MAX_SOURCE_LOG_BYTES = int(IO_API["MAX_SOURCE_LOG_BYTES"])
FilesystemAssemblyStorage = IO_API["FilesystemCaptureStorage"]
_checked_lstat = IO_API["checked_lstat"]
_prepare_cli_paths = IO_API["prepare_cli_paths"]
_read_bounded_regular_file = IO_API["read_bounded_regular_file"]
_strict_json = IO_API["decode_capture_plan"]


class GuestDtbAssemblyError(ValueError):
    """Captured segment files do not match the bounded evidence contract."""


def _plain_filename(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise GuestDtbAssemblyError(f"{field} must be a plain filename")
    return value


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exact_integer(value: object, expected: int) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool)
        and value == expected
    )


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def decode_evidence_json(data: bytes, *, field: str) -> dict[str, Any]:
    """Decode one bounded object while rejecting duplicate JSON keys."""

    try:
        return _strict_json(data)
    except IoContractError as error:
        raise GuestDtbAssemblyError(f"{field} is invalid: {error}") from error


def _read_evidence_file(
    directory: Path, filename: str, *, expected_size: int | None = None
) -> bytes:
    limit = expected_size if expected_size is not None else MAX_SOURCE_LOG_BYTES
    try:
        data, _ = _read_bounded_regular_file(
            directory / filename,
            byte_limit=limit,
            field=f"evidence file {filename}",
        )
    except IoContractError as error:
        raise GuestDtbAssemblyError(str(error)) from error
    if expected_size is not None and len(data) != expected_size:
        raise GuestDtbAssemblyError(
            f"evidence file {filename} has {len(data)} bytes, "
            f"expected {expected_size}"
        )
    return data


def _validated_fdt_header(data: bytes, *, vm_id: int) -> dict[str, int]:
    if len(data) < FDT_HEADER_SIZE:
        raise GuestDtbAssemblyError(
            f"VM {vm_id} capture is shorter than the {FDT_HEADER_SIZE}-byte FDT header"
        )
    (
        magic,
        totalsize,
        off_dt_struct,
        off_dt_strings,
        off_mem_rsvmap,
        version,
        last_comp_version,
        boot_cpuid_phys,
        size_dt_strings,
        size_dt_struct,
    ) = struct.unpack(">10I", data[:FDT_HEADER_SIZE])

    if magic != FDT_MAGIC:
        raise GuestDtbAssemblyError(
            f"VM {vm_id} capture does not start with flattened-DT magic"
        )
    if totalsize != len(data):
        raise GuestDtbAssemblyError(
            f"VM {vm_id} header totalsize {totalsize} does not match captured size "
            f"{len(data)}"
        )
    if version < 17 or last_comp_version > version:
        raise GuestDtbAssemblyError(
            f"VM {vm_id} has unsupported FDT version {version}/last-compatible "
            f"{last_comp_version}"
        )
    if not FDT_HEADER_SIZE <= off_mem_rsvmap <= totalsize:
        raise GuestDtbAssemblyError(
            f"VM {vm_id} memory-reservation block offset exceeds totalsize"
        )
    if (
        off_dt_struct < FDT_HEADER_SIZE
        or off_dt_struct > totalsize
        or size_dt_struct > totalsize - off_dt_struct
    ):
        raise GuestDtbAssemblyError(
            f"VM {vm_id} structure block exceeds totalsize"
        )
    if (
        off_dt_strings < FDT_HEADER_SIZE
        or off_dt_strings > totalsize
        or size_dt_strings > totalsize - off_dt_strings
    ):
        raise GuestDtbAssemblyError(f"VM {vm_id} strings block exceeds totalsize")

    return {
        "totalsize": totalsize,
        "offDtStruct": off_dt_struct,
        "offDtStrings": off_dt_strings,
        "offMemRsvmap": off_mem_rsvmap,
        "version": version,
        "lastCompatibleVersion": last_comp_version,
        "bootCpuId": boot_cpuid_phys,
        "sizeDtStrings": size_dt_strings,
        "sizeDtStruct": size_dt_struct,
    }


def _validated_source(
    plan: dict[str, Any],
    read_evidence: Callable[[str, int | None], bytes],
) -> tuple[dict[str, str], bytes]:
    source = plan.get("source")
    if not isinstance(source, dict):
        raise GuestDtbAssemblyError("capture plan source is missing")
    log = source.get("axvisorLog")
    if not isinstance(log, dict):
        raise GuestDtbAssemblyError("capture plan AxVisor log source is missing")
    filename = _plain_filename(log.get("path"), field="AxVisor log path")
    expected_sha = log.get("sha256")
    if not _valid_sha256(expected_sha):
        raise GuestDtbAssemblyError("AxVisor log SHA-256 is malformed")
    data = read_evidence(filename, None)
    actual_sha = _sha256_bytes(data)
    if actual_sha != expected_sha:
        raise GuestDtbAssemblyError(
            "AxVisor log SHA-256 does not match capture plan"
        )
    return {"path": filename, "sha256": actual_sha}, data


def _validate_canonical_plan(
    plan: dict[str, Any], log_source: dict[str, str], log_data: bytes
) -> None:
    guests = plan.get("guests")
    if not isinstance(guests, list) or not guests:
        raise GuestDtbAssemblyError("capture plan has no guests")
    vm_ids = []
    for guest in guests:
        vm_id = guest.get("vmId") if isinstance(guest, dict) else None
        if not isinstance(vm_id, int) or isinstance(vm_id, bool) or vm_id <= 0:
            raise GuestDtbAssemblyError("capture plan VM id must be a positive integer")
        vm_ids.append(vm_id)
    if len(set(vm_ids)) != len(vm_ids):
        raise GuestDtbAssemblyError("capture plan repeats a VM id")
    try:
        markers = PLANNER_API["parse_guest_dtb_markers"](
            log_data.decode("utf-8", errors="strict"), expected_vm_ids=set(vm_ids)
        )
        canonical = PLANNER_API["build_capture_plan"](
            markers=markers,
            source_log_name=log_source["path"],
            source_log_sha256=log_source["sha256"],
        )
    except (UnicodeError, ValueError) as error:
        raise GuestDtbAssemblyError(
            f"capture plan does not match runtime markers: {error}"
        ) from error
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    plan_json = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    if plan_json != canonical_json:
        raise GuestDtbAssemblyError("capture plan does not match runtime markers")


def _validated_execution_guests(
    plan: dict[str, Any],
    execution: dict[str, Any],
    *,
    capture_plan_name: str,
    capture_plan_sha256: str,
    log_source: dict[str, str],
) -> list[dict[str, Any]]:
    if not isinstance(execution, dict):
        raise GuestDtbAssemblyError("capture execution root is not an object")
    if not _exact_integer(execution.get("schemaVersion"), 1):
        raise GuestDtbAssemblyError("capture execution schemaVersion must be 1")
    if execution.get("artifactStatus") != "capture-generated-unreviewed":
        raise GuestDtbAssemblyError("capture execution artifactStatus is invalid")
    if execution.get("status") != "qmp_pmemsave_completed_exact_size":
        raise GuestDtbAssemblyError(
            "capture execution status must be "
            "qmp_pmemsave_completed_exact_size"
        )
    if (
        execution.get("proofScope")
        != "final-guest-dtb-qmp-pmemsave-byte-capture"
    ):
        raise GuestDtbAssemblyError("capture execution proofScope is not recognized")
    if execution.get("sourcePathBase") != "artifact parent directory":
        raise GuestDtbAssemblyError("capture execution sourcePathBase is invalid")
    plan_name = _plain_filename(capture_plan_name, field="capture plan name")
    if not _valid_sha256(capture_plan_sha256):
        raise GuestDtbAssemblyError("capture plan SHA-256 is malformed")
    source = execution.get("source")
    if not isinstance(source, dict):
        raise GuestDtbAssemblyError("capture execution source is missing")
    execution_plan = source.get("capturePlan")
    if not isinstance(execution_plan, dict):
        raise GuestDtbAssemblyError("capture execution plan source is missing")
    execution_plan_name = _plain_filename(
        execution_plan.get("path"), field="capture execution plan path"
    )
    execution_plan_sha = execution_plan.get("sha256")
    if not _valid_sha256(execution_plan_sha):
        raise GuestDtbAssemblyError("capture execution plan SHA-256 is malformed")
    if execution_plan_name != plan_name:
        raise GuestDtbAssemblyError(
            "capture execution plan path does not match current capture plan"
        )
    if execution_plan_sha != capture_plan_sha256:
        raise GuestDtbAssemblyError(
            "capture execution plan SHA-256 does not match current capture plan"
        )

    execution_log = source.get("axvisorLog")
    if not isinstance(execution_log, dict):
        raise GuestDtbAssemblyError("capture execution AxVisor log source is missing")
    execution_log_name = _plain_filename(
        execution_log.get("path"), field="capture execution AxVisor log path"
    )
    execution_log_sha = execution_log.get("sha256")
    if not _valid_sha256(execution_log_sha):
        raise GuestDtbAssemblyError(
            "capture execution AxVisor log SHA-256 is malformed"
        )
    if {
        "path": execution_log_name,
        "sha256": execution_log_sha,
    } != log_source:
        raise GuestDtbAssemblyError(
            "capture execution AxVisor log does not match capture plan"
        )
    qmp = execution.get("qmp")
    if not isinstance(qmp, dict):
        raise GuestDtbAssemblyError("capture execution QMP record is missing")
    if qmp.get("capabilitiesNegotiated") is not True:
        raise GuestDtbAssemblyError("capture execution lacks QMP negotiation")
    if qmp.get("operation") != "pmemsave":
        raise GuestDtbAssemblyError("capture execution operation must be pmemsave")
    if qmp.get("addressSpace") != "host-physical":
        raise GuestDtbAssemblyError("execution addressSpace must be host-physical")
    guests = execution.get("guests")
    plan_guests = plan.get("guests")
    if not isinstance(guests, list):
        raise GuestDtbAssemblyError("capture execution has no guests")
    if not isinstance(plan_guests, list) or len(guests) != len(plan_guests):
        raise GuestDtbAssemblyError("capture execution guest count does not match plan")
    return guests


def assemble_guest_dtbs(
    plan: dict[str, Any],
    execution: dict[str, Any],
    evidence_directory: Path,
    *,
    capture_plan_name: str,
    capture_plan_sha256: str,
    evidence_reader: Callable[[str, int | None], bytes] | None = None,
) -> list[dict[str, Any]]:
    """Read every planned segment only after validating the capture contract."""

    if plan.get("schemaVersion") != 1:
        raise GuestDtbAssemblyError("capture plan schemaVersion must be 1")
    if plan.get("artifactStatus") != "marker-derived-unreviewed":
        raise GuestDtbAssemblyError("capture plan artifactStatus is not recognized")
    if plan.get("status") != "capture_planned":
        raise GuestDtbAssemblyError("capture plan status must be capture_planned")
    if plan.get("proofScope") != "final-guest-dtb-physical-capture-plan":
        raise GuestDtbAssemblyError("capture plan proofScope is not recognized")
    if plan.get("sourcePathBase") != "capture-plan.json parent directory":
        raise GuestDtbAssemblyError("capture plan sourcePathBase is not recognized")
    directory = evidence_directory.resolve()
    if evidence_reader is None:
        if not directory.is_dir():
            raise GuestDtbAssemblyError("evidence directory is missing")

        def raw_reader(filename: str, expected_size: int | None) -> bytes:
            return _read_evidence_file(
                directory, filename, expected_size=expected_size
            )

    else:
        raw_reader = evidence_reader

    def read_evidence(filename: str, expected_size: int | None) -> bytes:
        try:
            data = raw_reader(filename, expected_size)
        except GuestDtbAssemblyError:
            raise
        except (KeyError, OSError) as error:
            raise GuestDtbAssemblyError(
                f"could not read evidence file {filename}: {error}"
            ) from error
        if not isinstance(data, bytes):
            raise GuestDtbAssemblyError(
                f"evidence reader returned non-bytes for {filename}"
            )
        if expected_size is not None and len(data) != expected_size:
            raise GuestDtbAssemblyError(
                f"evidence file {filename} has {len(data)} bytes, "
                f"expected {expected_size}"
            )
        return data

    log_source, log_data = _validated_source(plan, read_evidence)
    _validate_canonical_plan(plan, log_source, log_data)

    guests = plan.get("guests")
    if not isinstance(guests, list) or not guests:
        raise GuestDtbAssemblyError("capture plan has no guests")
    execution_guests = _validated_execution_guests(
        plan,
        execution,
        capture_plan_name=capture_plan_name,
        capture_plan_sha256=capture_plan_sha256,
        log_source=log_source,
    )

    seen_vm_ids: set[int] = set()
    seen_filenames: set[str] = set()
    assemblies: list[dict[str, Any]] = []
    for guest_position, guest in enumerate(guests):
        if not isinstance(guest, dict):
            raise GuestDtbAssemblyError("capture plan guest entry is not an object")
        vm_id = guest.get("vmId")
        if not isinstance(vm_id, int) or isinstance(vm_id, bool) or vm_id <= 0:
            raise GuestDtbAssemblyError("capture plan VM id must be a positive integer")
        if vm_id in seen_vm_ids:
            raise GuestDtbAssemblyError(f"capture plan repeats VM {vm_id}")
        seen_vm_ids.add(vm_id)

        size = guest.get("size")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < FDT_HEADER_SIZE
            or size > MAX_DTB_CAPTURE_BYTES
        ):
            raise GuestDtbAssemblyError(f"VM {vm_id} capture size is outside bounds")
        gpa = guest.get("gpa")
        if not isinstance(gpa, str) or re.fullmatch(r"0x[0-9a-f]+", gpa) is None:
            raise GuestDtbAssemblyError(f"VM {vm_id} GPA is malformed")
        execution_guest = execution_guests[guest_position]
        if not isinstance(execution_guest, dict):
            raise GuestDtbAssemblyError(
                "capture execution guest entry is not an object"
            )
        if not _exact_integer(execution_guest.get("vmId"), vm_id):
            raise GuestDtbAssemblyError(
                "capture execution guest VM id does not match capture plan"
            )
        if execution_guest.get("gpa") != gpa:
            raise GuestDtbAssemblyError(
                f"VM {vm_id} execution GPA does not match capture plan"
            )
        if not _exact_integer(execution_guest.get("size"), size):
            raise GuestDtbAssemblyError(
                f"VM {vm_id} execution size does not match capture plan"
            )
        output_name = _plain_filename(
            guest.get("assembledFileName"), field=f"VM {vm_id} output name"
        )
        if output_name != f"guest-vm-{vm_id}.final.dtb":
            raise GuestDtbAssemblyError(f"VM {vm_id} output name is not deterministic")
        if output_name in seen_filenames:
            raise GuestDtbAssemblyError(f"capture plan repeats filename {output_name}")
        seen_filenames.add(output_name)

        segments = guest.get("segments")
        if not isinstance(segments, list) or not segments:
            raise GuestDtbAssemblyError(f"VM {vm_id} capture has no segments")
        execution_segments = execution_guest.get("segments")
        if (
            not isinstance(execution_segments, list)
            or len(execution_segments) != len(segments)
        ):
            raise GuestDtbAssemblyError(
                f"VM {vm_id} execution segment count does not match capture plan"
            )
        segment_data: list[bytes] = []
        sources: list[dict[str, Any]] = []
        total = 0
        for expected_index, segment in enumerate(segments):
            if not isinstance(segment, dict) or segment.get("index") != expected_index:
                raise GuestDtbAssemblyError(
                    f"VM {vm_id} segment indexes are not contiguous"
                )
            filename = _plain_filename(
                segment.get("fileName"),
                field=f"VM {vm_id} segment {expected_index} filename",
            )
            if filename != f"guest-vm-{vm_id}.segment-{expected_index:03d}.bin":
                raise GuestDtbAssemblyError(
                    f"VM {vm_id} segment {expected_index} filename is not deterministic"
                )
            if filename in seen_filenames:
                raise GuestDtbAssemblyError(f"capture plan repeats filename {filename}")
            seen_filenames.add(filename)
            length = segment.get("length")
            if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
                raise GuestDtbAssemblyError(
                    f"VM {vm_id} segment {expected_index} length must be positive"
                )
            if segment.get("qmpOperation") != "pmemsave":
                raise GuestDtbAssemblyError(
                    f"VM {vm_id} segment {expected_index} is not a pmemsave capture"
                )
            hpa = segment.get("hpa")
            if not isinstance(hpa, str) or re.fullmatch(r"0x[0-9a-f]+", hpa) is None:
                raise GuestDtbAssemblyError(
                    f"VM {vm_id} segment {expected_index} HPA is malformed"
                )
            execution_segment = execution_segments[expected_index]
            if not isinstance(execution_segment, dict):
                raise GuestDtbAssemblyError(
                    f"VM {vm_id} execution segment is not an object"
                )
            if not _exact_integer(execution_segment.get("vmId"), vm_id):
                raise GuestDtbAssemblyError(
                    "execution segment VM id does not match capture plan"
                )
            if not _exact_integer(
                execution_segment.get("index"), expected_index
            ):
                raise GuestDtbAssemblyError(
                    "execution segment index does not match capture plan"
                )
            if execution_segment.get("hpa") != hpa:
                raise GuestDtbAssemblyError(
                    "execution segment HPA does not match capture plan"
                )
            if not _exact_integer(execution_segment.get("length"), length):
                raise GuestDtbAssemblyError(
                    "execution segment length does not match capture plan"
                )
            execution_filename = _plain_filename(
                execution_segment.get("path"),
                field=f"VM {vm_id} execution segment {expected_index} filename",
            )
            if execution_filename != filename:
                raise GuestDtbAssemblyError(
                    "execution segment filename does not match capture plan"
                )
            if execution_segment.get("qmpOperation") != "pmemsave":
                raise GuestDtbAssemblyError(
                    "execution segment operation does not match capture plan"
                )
            execution_sha = execution_segment.get("sha256")
            if (
                not isinstance(execution_sha, str)
                or SHA256_PATTERN.fullmatch(execution_sha) is None
            ):
                raise GuestDtbAssemblyError(
                    f"VM {vm_id} execution segment {expected_index} "
                    "SHA-256 is malformed"
                )
            data = read_evidence(filename, length)
            actual_sha = _sha256_bytes(data)
            if actual_sha != execution_sha:
                raise GuestDtbAssemblyError(
                    f"evidence file {filename} SHA-256 does not match "
                    "capture execution"
                )
            segment_data.append(data)
            total += length
            sources.append(
                {
                    "index": expected_index,
                    "hpa": hpa,
                    "length": length,
                    "path": filename,
                    "sha256": actual_sha,
                }
            )
        if total != size:
            raise GuestDtbAssemblyError(
                f"VM {vm_id} segment lengths total {total}, expected {size}"
            )

        data = b"".join(segment_data)
        header = _validated_fdt_header(data, vm_id=vm_id)
        assemblies.append(
            {
                "vmId": vm_id,
                "gpa": gpa,
                "size": size,
                "outputFileName": output_name,
                "sha256": _sha256_bytes(data),
                "header": header,
                "segmentSources": sources,
                "data": data,
            }
        )

    return sorted(assemblies, key=lambda item: int(item["vmId"]))


def build_capture_result(
    *,
    assemblies: list[dict[str, Any]],
    capture_plan_name: str,
    capture_plan_sha256: str,
    capture_execution_name: str,
    capture_execution_sha256: str,
) -> dict[str, Any]:
    """Build a manifest that deliberately excludes the in-memory DTB bytes."""

    plan_name = _plain_filename(capture_plan_name, field="capture plan name")
    if not _valid_sha256(capture_plan_sha256):
        raise GuestDtbAssemblyError("capture plan SHA-256 is malformed")
    execution_name = _plain_filename(capture_execution_name, field="execution name")
    if execution_name != "capture-execution.json":
        raise GuestDtbAssemblyError("execution name must be capture-execution.json")
    if not _valid_sha256(capture_execution_sha256):
        raise GuestDtbAssemblyError("capture execution SHA-256 is malformed")
    guests = [
        {key: value for key, value in assembly.items() if key != "data"}
        for assembly in sorted(assemblies, key=lambda item: int(item["vmId"]))
    ]
    return {
        "schemaVersion": 1,
        "artifactStatus": "capture-generated-unreviewed",
        "status": "dtb_bytes_assembled_header_validated",
        "proofScope": "final-guest-dtb-byte-assembly-and-header",
        "doesNotProve": [
            "device-tree semantic validity",
            "captured device ownership matches the VM configuration",
            "both guests booted",
            "passthrough DMA is isolated",
            "Linux and Zephyr have IP connectivity",
        ],
        "sourcePathBase": "capture-result.json parent directory",
        "source": {
            "capturePlan": {
                "path": plan_name,
                "sha256": capture_plan_sha256,
            },
            "captureExecution": {
                "path": execution_name,
                "sha256": capture_execution_sha256,
            },
        },
        "guests": guests,
    }


def publish_capture_result(
    assemblies: list[dict[str, Any]],
    serialized_result: bytes,
    evidence_directory: Path,
    output_path: Path,
    *,
    storage: Any | None = None,
) -> None:
    """Publish new DTBs first and the result manifest last, without overwrite."""

    directory = evidence_directory.resolve()
    result_path = output_path.resolve()
    if result_path.parent != directory:
        raise GuestDtbAssemblyError(
            "capture inputs and --output must share one evidence directory"
        )
    if not isinstance(serialized_result, bytes):
        raise GuestDtbAssemblyError("serialized capture result must be bytes")
    if storage is None and not directory.is_dir():
        raise GuestDtbAssemblyError("evidence directory is missing")
    capture_storage = storage or FilesystemAssemblyStorage(directory)

    final_paths: list[Path] = []
    final_data: list[bytes] = []
    seen_names = {result_path.name}
    for assembly in assemblies:
        output_name = _plain_filename(
            assembly.get("outputFileName"), field="assembled DTB output name"
        )
        if output_name in seen_names:
            raise GuestDtbAssemblyError(
                f"assembly repeats output filename {output_name}"
            )
        seen_names.add(output_name)
        data = assembly.get("data")
        if not isinstance(data, bytes):
            raise GuestDtbAssemblyError(
                f"assembled DTB {output_name} has no byte payload"
            )
        final_paths.append(directory / output_name)
        final_data.append(data)

    try:
        with capture_storage.transaction() as staging_directory:
            final_staging = [staging_directory / path.name for path in final_paths]
            manifest_staging = staging_directory / result_path.name
            published: list[Path] = []
            created: list[Path] = []
            capture_storage.ensure_absent([*final_paths, result_path])
            try:
                for staging_path, data in zip(final_staging, final_data):
                    capture_storage.write_staging(staging_path, data)
                    created.append(staging_path)
                capture_storage.write_staging(manifest_staging, serialized_result)
                created.append(manifest_staging)
                for staging_path, final_path, data in zip(
                    final_staging, final_paths, final_data
                ):
                    capture_storage.publish(
                        staging_path,
                        final_path,
                        expected_size=len(data),
                        expected_sha256=_sha256_bytes(data),
                    )
                    published.append(final_path)
                capture_storage.publish(
                    manifest_staging,
                    result_path,
                    expected_size=len(serialized_result),
                    expected_sha256=_sha256_bytes(serialized_result),
                )
                published.append(result_path)
                capture_storage.sync_directory()
            except Exception:
                for path in [*created, *reversed(published)]:
                    capture_storage.cleanup(path)
                raise
    except IoContractError as error:
        raise GuestDtbAssemblyError(str(error)) from error


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble QMP pmemsave segments and validate final Guest DTB "
            "magic, totalsize, and bounded header sections"
        )
    )
    parser.add_argument("--plan", required=True, type=Path, help="capture-plan JSON")
    parser.add_argument(
        "--execution",
        required=True,
        type=Path,
        help="capture-execution.json from the QMP executor",
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="capture-result JSON"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan_path, output_path = _prepare_cli_paths(args.plan, args.output)
        directory = plan_path.parent
        execution_path = args.execution.absolute()
        if execution_path.name != "capture-execution.json":
            raise GuestDtbAssemblyError(
                "--execution must be named capture-execution.json"
            )
        if execution_path.parent != directory:
            raise GuestDtbAssemblyError(
                "capture plan and execution must share one evidence directory"
            )
        if plan_path == execution_path:
            raise GuestDtbAssemblyError(
                "capture plan and execution must be different files"
            )
        _checked_lstat(execution_path, field="capture execution", kind="file")
        plan_bytes, _ = _read_bounded_regular_file(
            plan_path,
            byte_limit=MAX_CAPTURE_PLAN_BYTES,
            field="capture plan",
        )
        execution_bytes, _ = _read_bounded_regular_file(
            execution_path,
            byte_limit=MAX_CAPTURE_PLAN_BYTES,
            field="capture execution",
        )
        plan = decode_evidence_json(plan_bytes, field="capture plan")
        execution = decode_evidence_json(
            execution_bytes, field="capture execution"
        )
        plan_sha = _sha256_bytes(plan_bytes)
        execution_sha = _sha256_bytes(execution_bytes)
        assemblies = assemble_guest_dtbs(
            plan,
            execution,
            directory,
            capture_plan_name=plan_path.name,
            capture_plan_sha256=plan_sha,
        )
        output_names = {str(item["outputFileName"]) for item in assemblies}
        reserved_names = {plan_path.name, execution_path.name, *output_names}
        source = plan["source"]["axvisorLog"]
        reserved_names.add(str(source["path"]))
        for guest in plan["guests"]:
            for segment in guest["segments"]:
                reserved_names.add(str(segment["fileName"]))
        if args.output.name in reserved_names:
            raise GuestDtbAssemblyError(
                "capture-result output collides with an input/output"
            )
        result = build_capture_result(
            assemblies=assemblies,
            capture_plan_name=plan_path.name,
            capture_plan_sha256=plan_sha,
            capture_execution_name=execution_path.name,
            capture_execution_sha256=execution_sha,
        )
        serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        publish_capture_result(
            assemblies,
            serialized.encode("utf-8"),
            directory,
            output_path,
        )
    except (GuestDtbAssemblyError, IoContractError) as error:
        print(f"Final Guest DTB assembly failed: {error}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"Could not read/write Guest DTB capture: {error}", file=sys.stderr)
        return 2

    print(serialized, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
