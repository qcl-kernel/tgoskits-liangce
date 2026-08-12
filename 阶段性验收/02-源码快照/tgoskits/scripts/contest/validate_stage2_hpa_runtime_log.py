#!/usr/bin/env python3
"""Validate byte-bound post-run AxVM stage-2 HPA observations.

This is intentionally a host-log validator, not a runtime launcher.  It only
accepts the narrow observation emitted by the feature-gated AxVM marker path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from host_vm_carveout_io import publish_new_file, read_regular_bytes


MARKER_PREFIX = "AXVISOR_STAGE2_HPA_POSTRUN"
PAGE_SIZE = 4096
MAX_U64 = (1 << 64) - 1
MAP_KINDS = {0: "alloc", 1: "identical", 2: "reserved"}
MARKER_PATTERN = re.compile(
    r"^AXVISOR_STAGE2_HPA_POSTRUN "
    r"vm=(?P<vm>[1-9][0-9]*) "
    r"vcpu=(?P<vcpu>[0-9]+) "
    r"kind=(?P<kind>alloc|identical|reserved) "
    r"gpa=(?P<gpa>0x[0-9a-f]+) "
    r"hpa=(?P<hpa>0x[0-9a-f]+) "
    r"page_size=(?P<page_size>4096) "
    r"identity=(?P<identity>[01])$"
)


class Stage2RuntimeEvidenceError(ValueError):
    """The supplied artifacts cannot prove the narrow stage-2 observation."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _uint(value: object, *, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_U64
    ):
        raise Stage2RuntimeEvidenceError(f"{label} must be an unsigned 64-bit integer")
    return value


def _selected_region(config: dict[str, Any], *, label: str) -> dict[str, int | str]:
    base = config.get("base")
    kernel = config.get("kernel")
    if not isinstance(base, dict) or not isinstance(kernel, dict):
        raise Stage2RuntimeEvidenceError(f"{label} must contain [base] and [kernel] tables")
    vm_id = _uint(base.get("id"), label=f"{label} base.id")
    if vm_id == 0:
        raise Stage2RuntimeEvidenceError(f"{label} base.id must be positive")
    cpu_num = _uint(base.get("cpu_num"), label=f"{label} base.cpu_num")
    if cpu_num == 0:
        raise Stage2RuntimeEvidenceError(f"{label} base.cpu_num must be positive")
    regions = kernel.get("memory_regions")
    if not isinstance(regions, list) or not regions:
        raise Stage2RuntimeEvidenceError(
            f"{label} kernel.memory_regions must be a non-empty array"
        )

    first: dict[str, int | str] | None = None
    for index, region in enumerate(regions):
        if not isinstance(region, list) or len(region) != 4:
            raise Stage2RuntimeEvidenceError(
                f"{label} kernel.memory_regions[{index}] must contain four integers"
            )
        gpa = _uint(region[0], label=f"{label} memory region {index} GPA")
        size = _uint(region[1], label=f"{label} memory region {index} size")
        _uint(region[2], label=f"{label} memory region {index} flags")
        map_type = _uint(region[3], label=f"{label} memory region {index} map type")
        if map_type not in MAP_KINDS:
            raise Stage2RuntimeEvidenceError(
                f"{label} memory region {index} map type must be 0, 1, or 2"
            )
        if (
            size < PAGE_SIZE
            or gpa % PAGE_SIZE
            or size % PAGE_SIZE
            or gpa + size > MAX_U64 + 1
        ):
            raise Stage2RuntimeEvidenceError(
                f"{label} memory region {index} is not a page-aligned non-overflowing range"
            )
        parsed = {
            "vmId": vm_id,
            "cpuNum": cpu_num,
            "gpa": gpa,
            "size": size,
            "mapType": map_type,
            "kind": MAP_KINDS[map_type],
        }
        if map_type == 2:
            return parsed
        if first is None:
            first = parsed
    assert first is not None
    return first


def parse_vm_config_bytes(data: bytes, *, label: str) -> dict[str, int | str]:
    """Parse one raw TOML configuration and choose the production marker region."""
    try:
        config = tomllib.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise Stage2RuntimeEvidenceError(f"{label} is not strict UTF-8 TOML: {error}") from error
    return _selected_region(config, label=label)


def _sha256_field(value: object, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise Stage2RuntimeEvidenceError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Stage2RuntimeEvidenceError(
                f"capture status has duplicate JSON member {key!r}"
            )
        result[key] = value
    return result


def validate_capture_status_bytes(
    data: bytes,
    *,
    label: str,
    expected_vm_id: int,
    vm_config_bytes: bytes,
    log_bytes: bytes,
) -> dict[str, Any]:
    """Bind a successful one-Guest capture status to these exact input bytes."""
    try:
        status = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_json_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Stage2RuntimeEvidenceError(
            f"{label} is not strict UTF-8 JSON: {error}"
        ) from error
    if not isinstance(status, dict):
        raise Stage2RuntimeEvidenceError(f"{label} must contain a JSON object")
    if status.get("success") is not True:
        raise Stage2RuntimeEvidenceError(f"{label} success must be true")
    if status.get("status") != "single_guest_live_capture_passed":
        raise Stage2RuntimeEvidenceError(
            f"{label} status must be 'single_guest_live_capture_passed'"
        )
    state = status.get("state")
    if not isinstance(state, dict):
        raise Stage2RuntimeEvidenceError(f"{label} has no state object")
    status_vm_id = _uint(state.get("expectedVmId"), label=f"{label} state.expectedVmId")
    if status_vm_id != expected_vm_id:
        raise Stage2RuntimeEvidenceError(
            f"{label} state.expectedVmId does not match the VM configuration"
        )
    input_artifacts = state.get("inputArtifacts")
    if not isinstance(input_artifacts, dict):
        raise Stage2RuntimeEvidenceError(f"{label} has no state.inputArtifacts object")
    vmconfig = input_artifacts.get("vmconfig")
    if not isinstance(vmconfig, dict):
        raise Stage2RuntimeEvidenceError(
            f"{label} has no state.inputArtifacts.vmconfig object"
        )
    vmconfig_size = _uint(
        vmconfig.get("size"), label=f"{label} state.inputArtifacts.vmconfig.size"
    )
    vmconfig_sha = _sha256_field(
        vmconfig.get("sha256"), label=f"{label} state.inputArtifacts.vmconfig.sha256"
    )
    if vmconfig_size != len(vm_config_bytes) or vmconfig_sha != _sha256(vm_config_bytes):
        raise Stage2RuntimeEvidenceError(
            f"{label} state.inputArtifacts.vmconfig does not bind the supplied VM config"
        )
    live_log = state.get("liveLog")
    if not isinstance(live_log, dict):
        raise Stage2RuntimeEvidenceError(f"{label} has no state.liveLog object")
    live_log_sha = _sha256_field(
        live_log.get("sha256"), label=f"{label} state.liveLog.sha256"
    )
    if live_log_sha != _sha256(log_bytes):
        raise Stage2RuntimeEvidenceError(
            f"{label} state.liveLog does not bind the supplied raw log"
        )
    if "size" in live_log:
        live_log_size = _uint(live_log["size"], label=f"{label} state.liveLog.size")
        if live_log_size != len(log_bytes):
            raise Stage2RuntimeEvidenceError(
                f"{label} state.liveLog size does not match the supplied raw log"
            )
    return {
        "validated": True,
        "status": "single_guest_live_capture_passed",
        "expectedVmId": expected_vm_id,
        "vmConfig": {"sizeMatch": True, "sha256Match": True},
        "liveLog": {
            "sizeChecked": "size" in live_log,
            "sha256Match": True,
        },
    }


def parse_stage2_runtime_log(
    log_bytes: bytes, *, expected_vms: dict[int, dict[str, int | str]]
) -> list[dict[str, Any]]:
    """Fail closed on one exact marker and an earlier matching vCPU-run line per VM."""
    if not expected_vms:
        raise Stage2RuntimeEvidenceError("at least one VM configuration is required")
    try:
        log_text = log_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise Stage2RuntimeEvidenceError(f"raw log is not strict UTF-8: {error}") from error

    markers: dict[int, dict[str, Any]] = {}
    running_lines: set[tuple[int, int]] = set()
    for line_number, raw_line in enumerate(log_text.splitlines(), start=1):
        for vm_id in expected_vms:
            # The marker needs a run announcement for the same vCPU before it.
            # This literal is intentionally a substring because AxVisor's log level
            # prefix is outside the vCPU production message.
            running_prefix = f"VM[{vm_id}] VCpu["
            if running_prefix in raw_line:
                match = re.search(
                    rf"VM\[{vm_id}\] VCpu\[([0-9]+)\] running\.\.\.", raw_line
                )
                if match is not None:
                    running_lines.add((vm_id, int(match.group(1), 10)))
        if MARKER_PREFIX not in raw_line:
            continue
        match = MARKER_PATTERN.fullmatch(raw_line)
        if match is None:
            raise Stage2RuntimeEvidenceError(
                f"malformed stage-2 marker on line {line_number}: {raw_line!r}"
            )
        vm_id = int(match.group("vm"), 10)
        expected = expected_vms.get(vm_id)
        if expected is None:
            raise Stage2RuntimeEvidenceError(f"unexpected marker for VM {vm_id}")
        if vm_id in markers:
            raise Stage2RuntimeEvidenceError(f"duplicate marker for VM {vm_id}")
        vcpu_id = int(match.group("vcpu"), 10)
        if vcpu_id >= int(expected["cpuNum"]):
            raise Stage2RuntimeEvidenceError(
                f"VM {vm_id} marker vCPU {vcpu_id} is outside configured cpu_num"
            )
        if (vm_id, vcpu_id) not in running_lines:
            raise Stage2RuntimeEvidenceError(
                f"marker for VM {vm_id} vCPU {vcpu_id} has no earlier running log line"
            )
        gpa = int(match.group("gpa"), 16)
        hpa = int(match.group("hpa"), 16)
        if gpa > MAX_U64 or hpa > MAX_U64 or gpa % PAGE_SIZE or hpa % PAGE_SIZE:
            raise Stage2RuntimeEvidenceError(
                "marker addresses must be page-aligned u64 values"
            )
        identity = int(match.group("identity"), 10)
        if bool(identity) != (gpa == hpa):
            raise Stage2RuntimeEvidenceError("marker identity flag disagrees with GPA/HPA")
        kind = match.group("kind")
        if kind != expected["kind"]:
            raise Stage2RuntimeEvidenceError(
                f"VM {vm_id} marker kind {kind!r} does not match configured {expected['kind']!r}"
            )
        if int(expected["mapType"]) in (0, 2) and gpa != expected["gpa"]:
            raise Stage2RuntimeEvidenceError(
                f"VM {vm_id} marker GPA does not match configured GPA"
            )
        if kind in ("identical", "reserved") and identity != 1:
            raise Stage2RuntimeEvidenceError(f"{kind} marker must be an identity mapping")
        markers[vm_id] = {
            "vmId": vm_id,
            "vcpuId": vcpu_id,
            "kind": kind,
            "gpa": f"{gpa:#x}",
            "hpa": f"{hpa:#x}",
            "pageSize": PAGE_SIZE,
            "identity": identity,
            "sourceLine": line_number,
        }

    missing = sorted(set(expected_vms) - set(markers))
    if missing:
        raise Stage2RuntimeEvidenceError("missing markers for VM ids: " + ", ".join(map(str, missing)))
    return [markers[vm_id] for vm_id in sorted(markers)]


def build_payload(
    *,
    log_name: str,
    log_bytes: bytes,
    vm_config_bytes: dict[str, bytes],
    expected_vms: dict[int, dict[str, int | str]],
    markers: list[dict[str, Any]],
    capture_status_name: str | None = None,
    capture_status_bytes: bytes | None = None,
    capture_status_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sources: dict[str, Any] = {
        "rawLog": {
            "path": log_name,
            "size": len(log_bytes),
            "sha256": _sha256(log_bytes),
        },
        "vmConfigs": [
            {"path": name, "size": len(data), "sha256": _sha256(data)}
            for name, data in sorted(vm_config_bytes.items())
        ],
    }
    if capture_status_name is not None and capture_status_bytes is not None:
        sources["captureStatus"] = {
            "path": capture_status_name,
            "size": len(capture_status_bytes),
            "sha256": _sha256(capture_status_bytes),
        }
    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "artifactStatus": "runtime-log-observation-validated",
        "status": "stage2_hpa_markers_match_configured_regions",
        "proofScope": "byte-bound-host-log-postrun-stage2-hpa-observation",
        "doesNotProve": [
            "Guest boot",
            "host allocator exclusion",
            "passthrough DMA isolation",
            "dual-Guest execution",
            "Guest IP connectivity",
            "successful handling of the observed backend VM exit",
            "the original TOML equals any runtime-enriched VM configuration",
            "the running log substring proves anything beyond literal log order",
        ],
        "sources": sources,
        "expectedRegions": [
            {
                "vmId": vm_id,
                "cpuNum": int(value["cpuNum"]),
                "kind": value["kind"],
                "configuredGpa": f"{int(value['gpa']):#x}",
                "size": f"{int(value['size']):#x}",
                "mapType": int(value["mapType"]),
            }
            for vm_id, value in sorted(expected_vms.items())
        ],
        "markers": markers,
    }
    if capture_status_binding is not None:
        payload["captureStatusBinding"] = capture_status_binding
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path, help="raw AxVisor host log")
    parser.add_argument(
        "--vm-config",
        required=True,
        action="append",
        type=Path,
        help="AxVM TOML (repeat once per VM)",
    )
    parser.add_argument(
        "--capture-status",
        type=Path,
        help="successful run_live_guest_dtb_capture.py status.json for one VM",
    )
    parser.add_argument("--output", required=True, type=Path, help="new JSON evidence path")
    args = parser.parse_args(argv)
    try:
        log_bytes = read_regular_bytes(
            args.log, label="raw log", error_type=Stage2RuntimeEvidenceError
        )
        vm_config_bytes: dict[str, bytes] = {}
        expected_vms: dict[int, dict[str, int | str]] = {}
        for path in args.vm_config:
            name = str(path)
            if name in vm_config_bytes:
                raise Stage2RuntimeEvidenceError(f"duplicate VM configuration argument {name!r}")
            data = read_regular_bytes(
                path,
                label=f"VM configuration {name}",
                error_type=Stage2RuntimeEvidenceError,
            )
            selected = parse_vm_config_bytes(data, label=f"VM configuration {name}")
            vm_id = int(selected["vmId"])
            if vm_id in expected_vms:
                raise Stage2RuntimeEvidenceError(f"duplicate VM id {vm_id} across configurations")
            vm_config_bytes[name] = data
            expected_vms[vm_id] = selected
        markers = parse_stage2_runtime_log(log_bytes, expected_vms=expected_vms)
        capture_status_bytes: bytes | None = None
        capture_status_binding: dict[str, Any] | None = None
        if args.capture_status is not None:
            if len(vm_config_bytes) != 1:
                raise Stage2RuntimeEvidenceError(
                    "--capture-status requires exactly one --vm-config"
                )
            capture_status_bytes = read_regular_bytes(
                args.capture_status,
                label="capture status",
                error_type=Stage2RuntimeEvidenceError,
            )
            vm_id = next(iter(expected_vms))
            config_bytes = next(iter(vm_config_bytes.values()))
            capture_status_binding = validate_capture_status_bytes(
                capture_status_bytes,
                label="capture status",
                expected_vm_id=vm_id,
                vm_config_bytes=config_bytes,
                log_bytes=log_bytes,
            )
        payload = build_payload(
            log_name=str(args.log),
            log_bytes=log_bytes,
            vm_config_bytes=vm_config_bytes,
            expected_vms=expected_vms,
            markers=markers,
            capture_status_name=(
                None if args.capture_status is None else str(args.capture_status)
            ),
            capture_status_bytes=capture_status_bytes,
            capture_status_binding=capture_status_binding,
        )
        publish_new_file(
            args.output,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            error_type=Stage2RuntimeEvidenceError,
        )
    except (OSError, Stage2RuntimeEvidenceError) as error:
        print(f"stage-2 runtime log validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
