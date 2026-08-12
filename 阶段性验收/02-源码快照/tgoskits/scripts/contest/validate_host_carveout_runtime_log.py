#!/usr/bin/env python3
"""Fail-closed validation for observed host VM-carveout runtime markers.

This validator joins four independently supplied, immutable evidence inputs:
the static carveout preflight, decoded host DTS, AxVM configuration(s), and a
successful host-runtime capture status.  It validates only literal log
observations; it does not attempt to infer allocator or guest behaviour.
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


EARLY_PREFIX = "AXVISOR_HOST_VM_CARVEOUT_RESERVED "
ALLOCATOR_PREFIX = "AXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED "
DMA_GUARD_EARLY_PREFIX = "AXVISOR_HOST_DMA_GUARD_RESERVED "
DMA_GUARD_ALLOCATOR_PREFIX = "AXVISOR_HOST_DMA_GUARD_ALLOCATOR_EXCLUDED "
EARLY_PATTERN = re.compile(
    r"AXVISOR_HOST_VM_CARVEOUT_RESERVED vm=(?P<vm>[1-9][0-9]*) "
    r"hpa=(?P<hpa>0x[0-9a-f]+) size=(?P<size>0x[0-9a-f]+) "
    r"phase=before-ram-init\Z"
)
ALLOCATOR_PATTERN = re.compile(
    r"AXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED vm=(?P<vm>[1-9][0-9]*) "
    r"hpa=(?P<hpa>0x[0-9a-f]+) size=(?P<size>0x[0-9a-f]+) "
    r"reserved_cover=1 free_overlap=0 phase=before-global-allocator-init\Z"
)
DMA_GUARD_EARLY_PATTERN = re.compile(
    r"AXVISOR_HOST_DMA_GUARD_RESERVED "
    r"hpa=(?P<hpa>0x[0-9a-f]+) size=(?P<size>0x[0-9a-f]+) "
    r"phase=before-ram-init\Z"
)
DMA_GUARD_ALLOCATOR_PATTERN = re.compile(
    r"AXVISOR_HOST_DMA_GUARD_ALLOCATOR_EXCLUDED "
    r"hpa=(?P<hpa>0x[0-9a-f]+) size=(?P<size>0x[0-9a-f]+) "
    r"reserved_cover=1 free_overlap=0 phase=before-global-allocator-init\Z"
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
HEX_PATTERN = re.compile(r"0x[0-9a-f]+\Z")
CAPTURE_ARTIFACT_STATUS = "capture-generated-unreviewed"
CAPTURE_STATUS = "single_guest_live_capture_passed"
CAPTURE_PROOF_SCOPE = (
    "one-guest-launch-ready-same-session-qmp-capture-resume-identity-exit"
)


class HostCarveoutRuntimeEvidenceError(ValueError):
    """The supplied files do not prove the narrow runtime-log observation."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HostCarveoutRuntimeEvidenceError(
                f"JSON has duplicate member {key!r}"
            )
        result[key] = value
    return result


def _json_object(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostCarveoutRuntimeEvidenceError(
            f"{label} is not strict UTF-8 JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise HostCarveoutRuntimeEvidenceError(f"{label} must contain a JSON object")
    return value


def _uint(value: Any, *, label: str, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HostCarveoutRuntimeEvidenceError(f"{label} must be an unsigned integer")
    if positive and value == 0:
        raise HostCarveoutRuntimeEvidenceError(f"{label} must be non-zero")
    return value


def _sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise HostCarveoutRuntimeEvidenceError(f"{label} must be a lower-case SHA-256")
    return value


def _hex(value: Any, *, label: str, positive: bool = False) -> int:
    if not isinstance(value, str) or HEX_PATTERN.fullmatch(value) is None:
        raise HostCarveoutRuntimeEvidenceError(f"{label} must be lower-case hexadecimal")
    parsed = int(value, 16)
    if positive and parsed == 0:
        raise HostCarveoutRuntimeEvidenceError(f"{label} must be non-zero")
    return parsed


def _bound_source(
    value: Any,
    *,
    label: str,
    expected: bytes,
    path_name: str | None = None,
    require_size: bool = False,
    require_nonempty_path: bool = False,
) -> None:
    if not isinstance(value, dict):
        raise HostCarveoutRuntimeEvidenceError(f"{label} must be an object")
    if require_nonempty_path and (
        not isinstance(value.get("path"), str) or not value["path"]
    ):
        raise HostCarveoutRuntimeEvidenceError(f"{label} path must be non-empty")
    if path_name is not None and value.get("path") != path_name:
        raise HostCarveoutRuntimeEvidenceError(f"{label} path does not match supplied input")
    if require_size and "size" not in value:
        raise HostCarveoutRuntimeEvidenceError(f"{label} has no size")
    if "size" in value and _uint(value["size"], label=f"{label}.size") != len(expected):
        raise HostCarveoutRuntimeEvidenceError(f"{label} size does not match supplied input")
    if _sha(value.get("sha256"), label=f"{label}.sha256") != _sha256(expected):
        raise HostCarveoutRuntimeEvidenceError(f"{label} hash does not match supplied input")


def validate_preflight(
    data: bytes,
    *,
    host_dts: Path,
    host_dts_bytes: bytes,
    vm_configs: dict[str, bytes],
) -> tuple[list[dict[str, int]], list[dict[str, int]]]:
    """Accept only an exact successful static-preflight result for these bytes."""
    preflight = _json_object(data, label="preflight")
    if preflight.get("schemaVersion") != 1:
        raise HostCarveoutRuntimeEvidenceError("preflight schemaVersion must be 1")
    if preflight.get("artifactStatus") != "preflight-only":
        raise HostCarveoutRuntimeEvidenceError("preflight is not a preflight-only artifact")
    if preflight.get("status") != "carveout_artifacts_validated":
        raise HostCarveoutRuntimeEvidenceError("preflight is not a successful carveout validation")
    if preflight.get("proofScope") != "host-dts-vm-carveout-static-contract":
        raise HostCarveoutRuntimeEvidenceError("preflight has an unexpected proof scope")
    sources = preflight.get("sources")
    if not isinstance(sources, dict):
        raise HostCarveoutRuntimeEvidenceError("preflight has no sources object")
    _bound_source(
        sources.get("hostDts"),
        label="preflight.sources.hostDts",
        expected=host_dts_bytes,
        path_name=host_dts.name,
    )
    raw_configs = sources.get("vmConfigs")
    if not isinstance(raw_configs, list) or len(raw_configs) != len(vm_configs):
        raise HostCarveoutRuntimeEvidenceError("preflight VM-config list does not match supplied inputs")
    seen_names: set[str] = set()
    for item in raw_configs:
        if not isinstance(item, dict):
            raise HostCarveoutRuntimeEvidenceError("preflight VM-config source is not an object")
        name = item.get("path")
        if not isinstance(name, str) or name not in vm_configs or name in seen_names:
            raise HostCarveoutRuntimeEvidenceError("preflight has an unexpected VM-config source")
        seen_names.add(name)
        _bound_source(
            item,
            label=f"preflight.sources.vmConfigs[{name!r}]",
            expected=vm_configs[name],
            path_name=name,
        )
    if seen_names != set(vm_configs):
        raise HostCarveoutRuntimeEvidenceError("preflight omits a supplied VM configuration")

    raw_carveouts = preflight.get("carveouts")
    if not isinstance(raw_carveouts, list) or not raw_carveouts:
        raise HostCarveoutRuntimeEvidenceError("preflight has no carveout tuples")
    expected: list[dict[str, int]] = []
    previous_vm = 0
    seen_vm_ids: set[int] = set()
    for index, item in enumerate(raw_carveouts):
        if not isinstance(item, dict):
            raise HostCarveoutRuntimeEvidenceError(f"preflight carveout {index} is not an object")
        vm_id = _uint(item.get("vmId"), label=f"preflight carveout {index}.vmId", positive=True)
        hpa = _hex(item.get("hpa"), label=f"preflight carveout {index}.hpa")
        size = _hex(item.get("size"), label=f"preflight carveout {index}.size", positive=True)
        end = _hex(item.get("end"), label=f"preflight carveout {index}.end", positive=True)
        config = item.get("vmConfig")
        if not isinstance(config, str) or config not in vm_configs:
            raise HostCarveoutRuntimeEvidenceError(f"preflight carveout {index} VM config is not supplied")
        if end != hpa + size:
            raise HostCarveoutRuntimeEvidenceError(f"preflight carveout {index} end disagrees with HPA/size")
        if vm_id in seen_vm_ids or vm_id <= previous_vm:
            raise HostCarveoutRuntimeEvidenceError("preflight carveout VM ids must be unique ascending")
        seen_vm_ids.add(vm_id)
        previous_vm = vm_id
        expected.append({"vmId": vm_id, "hpa": hpa, "size": size})
    raw_guards = preflight.get("dmaGuards", [])
    if not isinstance(raw_guards, list):
        raise HostCarveoutRuntimeEvidenceError("preflight dmaGuards must be a list")
    guards: list[dict[str, int]] = []
    previous_end = 0
    for index, item in enumerate(raw_guards):
        if not isinstance(item, dict) or set(item) != {"hpa", "size", "end"}:
            raise HostCarveoutRuntimeEvidenceError(
                f"preflight DMA guard {index} must contain only hpa/size/end"
            )
        hpa = _hex(item.get("hpa"), label=f"preflight DMA guard {index}.hpa", positive=True)
        size = _hex(item.get("size"), label=f"preflight DMA guard {index}.size", positive=True)
        end = _hex(item.get("end"), label=f"preflight DMA guard {index}.end", positive=True)
        if end != hpa + size:
            raise HostCarveoutRuntimeEvidenceError(
                f"preflight DMA guard {index} end disagrees with HPA/size"
            )
        if guards and hpa < previous_end:
            raise HostCarveoutRuntimeEvidenceError(
                "preflight DMA guards must be ascending and non-overlapping"
            )
        previous_end = end
        guards.append({"hpa": hpa, "size": size})
    return expected, guards


def validate_capture_status(
    data: bytes,
    *,
    host_dtb_bytes: bytes,
    vm_configs: dict[str, bytes],
    log_bytes: bytes,
    log_name: str,
    expected_vm_ids: list[int],
) -> dict[str, Any]:
    """Bind a successful host runtime capture to all raw operational inputs."""
    status = _json_object(data, label="capture status")
    if status.get("schemaVersion") != 1:
        raise HostCarveoutRuntimeEvidenceError("capture status schemaVersion must be 1")
    if status.get("success") is not True:
        raise HostCarveoutRuntimeEvidenceError("capture status success must be true")
    if status.get("artifactStatus") != CAPTURE_ARTIFACT_STATUS:
        raise HostCarveoutRuntimeEvidenceError(
            f"capture status artifactStatus must be {CAPTURE_ARTIFACT_STATUS!r}"
        )
    if status.get("status") != CAPTURE_STATUS:
        raise HostCarveoutRuntimeEvidenceError(
            f"capture status status must be {CAPTURE_STATUS!r}"
        )
    if status.get("proofScope") != CAPTURE_PROOF_SCOPE:
        raise HostCarveoutRuntimeEvidenceError(
            f"capture status proofScope must be {CAPTURE_PROOF_SCOPE!r}"
        )
    if len(vm_configs) != 1 or len(expected_vm_ids) != 1:
        raise HostCarveoutRuntimeEvidenceError(
            "single-Guest capture status requires exactly one VM configuration and carveout"
        )
    if not host_dtb_bytes:
        raise HostCarveoutRuntimeEvidenceError("supplied host DTB must be non-empty")
    state = status.get("state")
    if not isinstance(state, dict):
        raise HostCarveoutRuntimeEvidenceError("capture status has no state object")
    observed_vm_id = _uint(
        state.get("expectedVmId"),
        label="capture status state.expectedVmId",
        positive=True,
    )
    if observed_vm_id != expected_vm_ids[0]:
        raise HostCarveoutRuntimeEvidenceError(
            "capture status expectedVmId does not match the preflight carveout"
        )
    inputs = state.get("inputArtifacts")
    if not isinstance(inputs, dict):
        raise HostCarveoutRuntimeEvidenceError("capture status has no inputArtifacts object")
    _bound_source(
        inputs.get("hostDtb"),
        label="capture status state.inputArtifacts.hostDtb",
        expected=host_dtb_bytes,
        require_size=True,
        require_nonempty_path=True,
    )
    if "vmConfigs" in inputs or "vmconfig" not in inputs:
        raise HostCarveoutRuntimeEvidenceError(
            "capture status must bind the real harness inputArtifacts.vmconfig field"
        )
    _bound_source(
        inputs["vmconfig"],
        label="capture status state.inputArtifacts.vmconfig",
        expected=next(iter(vm_configs.values())),
        require_size=True,
        require_nonempty_path=True,
    )
    _bound_source(
        state.get("liveLog"),
        label="capture status state.liveLog",
        expected=log_bytes,
        path_name=log_name,
    )
    return {
        "validated": True,
        "schemaVersion": 1,
        "artifactStatus": CAPTURE_ARTIFACT_STATUS,
        "status": CAPTURE_STATUS,
        "proofScope": CAPTURE_PROOF_SCOPE,
        "expectedVmId": observed_vm_id,
        "hostDtb": {"nonEmpty": True, "sizeMatch": True, "sha256Match": True},
        "vmconfig": {"sizeMatch": True, "sha256Match": True},
        "liveLog": {"pathMatch": True, "sha256Match": True},
    }


def parse_runtime_log(
    log_bytes: bytes,
    *,
    expected: list[dict[str, int]],
    expected_dma_guards: list[dict[str, int]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        text = log_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise HostCarveoutRuntimeEvidenceError("raw log is not strict UTF-8") from error
    expected_by_vm = {item["vmId"]: item for item in expected}
    early: list[tuple[int, int, dict[str, int]]] = []
    allocator: list[tuple[int, int, dict[str, int]]] = []
    seen_early: set[int] = set()
    seen_allocator: set[int] = set()
    expected_guards = {(item["hpa"], item["size"]): item for item in expected_dma_guards}
    guard_early: list[tuple[int, dict[str, int]]] = []
    guard_allocator: list[tuple[int, dict[str, int]]] = []
    seen_guard_early: set[tuple[int, int]] = set()
    seen_guard_allocator: set[tuple[int, int]] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        pattern: re.Pattern[str] | None = None
        phase = ""
        guard_phase = ""
        if DMA_GUARD_EARLY_PREFIX in line:
            pattern, guard_phase = DMA_GUARD_EARLY_PATTERN, "guard-reserved-before-ram-init"
        elif DMA_GUARD_ALLOCATOR_PREFIX in line:
            pattern, guard_phase = (
                DMA_GUARD_ALLOCATOR_PATTERN,
                "guard-allocator-excluded-before-global-allocator-init",
            )
        elif EARLY_PREFIX in line:
            pattern, phase = EARLY_PATTERN, "reserved-before-ram-init"
        elif ALLOCATOR_PREFIX in line:
            pattern, phase = ALLOCATOR_PATTERN, "allocator-excluded-before-global-allocator-init"
        if pattern is None:
            continue
        match = pattern.fullmatch(line)
        if match is None:
            raise HostCarveoutRuntimeEvidenceError(
                f"malformed {guard_phase or phase} marker on line {line_number}"
            )
        if guard_phase:
            hpa, size = int(match.group("hpa"), 16), int(match.group("size"), 16)
            key = (hpa, size)
            tuple_ = expected_guards.get(key)
            if tuple_ is None:
                raise HostCarveoutRuntimeEvidenceError(
                    f"unexpected DMA guard marker on line {line_number}"
                )
            entry = (line_number, tuple_)
            if guard_phase.startswith("guard-reserved"):
                if key in seen_guard_early:
                    raise HostCarveoutRuntimeEvidenceError(
                        f"duplicate early DMA guard marker for {hpa:#x}/{size:#x}"
                    )
                seen_guard_early.add(key)
                guard_early.append(entry)
            else:
                if key in seen_guard_allocator:
                    raise HostCarveoutRuntimeEvidenceError(
                        f"duplicate allocator DMA guard marker for {hpa:#x}/{size:#x}"
                    )
                seen_guard_allocator.add(key)
                guard_allocator.append(entry)
            continue
        vm_id = int(match.group("vm"), 10)
        tuple_ = expected_by_vm.get(vm_id)
        if tuple_ is None:
            raise HostCarveoutRuntimeEvidenceError(f"unexpected VM {vm_id} marker on line {line_number}")
        hpa, size = int(match.group("hpa"), 16), int(match.group("size"), 16)
        if (hpa, size) != (tuple_["hpa"], tuple_["size"]):
            raise HostCarveoutRuntimeEvidenceError(f"VM {vm_id} marker tuple does not match preflight")
        entry = (line_number, vm_id, tuple_)
        if phase.startswith("reserved"):
            if vm_id in seen_early:
                raise HostCarveoutRuntimeEvidenceError(f"duplicate early reservation marker for VM {vm_id}")
            seen_early.add(vm_id)
            early.append(entry)
        else:
            if vm_id in seen_allocator:
                raise HostCarveoutRuntimeEvidenceError(f"duplicate allocator-excluded marker for VM {vm_id}")
            seen_allocator.add(vm_id)
            allocator.append(entry)
    expected_ids = [item["vmId"] for item in expected]
    if [item[1] for item in early] != expected_ids:
        raise HostCarveoutRuntimeEvidenceError("early reservation markers are missing or out of expected VM order")
    if [item[1] for item in allocator] != expected_ids:
        raise HostCarveoutRuntimeEvidenceError("allocator-excluded markers are missing or out of expected VM order")
    expected_guard_keys = [(item["hpa"], item["size"]) for item in expected_dma_guards]
    if [(item[1]["hpa"], item[1]["size"]) for item in guard_early] != expected_guard_keys:
        raise HostCarveoutRuntimeEvidenceError(
            "early DMA guard markers are missing or out of expected HPA order"
        )
    if [(item[1]["hpa"], item[1]["size"]) for item in guard_allocator] != expected_guard_keys:
        raise HostCarveoutRuntimeEvidenceError(
            "allocator DMA guard markers are missing or out of expected HPA order"
        )
    all_early_lines = [item[0] for item in early] + [item[0] for item in guard_early]
    all_allocator_lines = [item[0] for item in allocator] + [item[0] for item in guard_allocator]
    if max(all_early_lines) >= min(all_allocator_lines):
        raise HostCarveoutRuntimeEvidenceError("allocator-excluded markers must follow all early reservation markers")
    carveout_markers = [
        {
            "vmId": item["vmId"],
            "hpa": f"{item['hpa']:#x}",
            "size": f"{item['size']:#x}",
            "earlyReservedLine": early[index][0],
            "allocatorExcludedLine": allocator[index][0],
        }
        for index, item in enumerate(expected)
    ]
    guard_markers = [
        {
            "hpa": f"{item['hpa']:#x}",
            "size": f"{item['size']:#x}",
            "earlyReservedLine": guard_early[index][0],
            "allocatorExcludedLine": guard_allocator[index][0],
        }
        for index, item in enumerate(expected_dma_guards)
    ]
    return carveout_markers, guard_markers


def _source(path: Path, data: bytes) -> dict[str, Any]:
    return {"path": str(path), "size": len(data), "sha256": _sha256(data)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--preflight", required=True, type=Path)
    parser.add_argument("--capture-status", required=True, type=Path)
    parser.add_argument("--host-dtb", required=True, type=Path)
    parser.add_argument("--host-dts", required=True, type=Path)
    parser.add_argument("--vm-config", required=True, action="append", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        all_inputs = [args.log, args.preflight, args.capture_status, args.host_dtb, args.host_dts, *args.vm_config]
        if any(path.resolve() == args.output.resolve() for path in all_inputs):
            raise HostCarveoutRuntimeEvidenceError("--output must not overwrite an input artifact")
        log_bytes = read_regular_bytes(args.log, label="raw log", error_type=HostCarveoutRuntimeEvidenceError)
        preflight_bytes = read_regular_bytes(args.preflight, label="preflight", error_type=HostCarveoutRuntimeEvidenceError)
        capture_status_bytes = read_regular_bytes(args.capture_status, label="capture status", error_type=HostCarveoutRuntimeEvidenceError)
        host_dtb_bytes = read_regular_bytes(args.host_dtb, label="host DTB", error_type=HostCarveoutRuntimeEvidenceError)
        host_dts_bytes = read_regular_bytes(args.host_dts, label="host DTS", error_type=HostCarveoutRuntimeEvidenceError)
        vm_configs: dict[str, bytes] = {}
        for path in args.vm_config:
            if path.name in vm_configs:
                raise HostCarveoutRuntimeEvidenceError(f"duplicate VM config basename {path.name!r}")
            vm_configs[path.name] = read_regular_bytes(path, label=f"VM config {path.name!r}", error_type=HostCarveoutRuntimeEvidenceError)
        expected, expected_dma_guards = validate_preflight(
            preflight_bytes,
            host_dts=args.host_dts,
            host_dts_bytes=host_dts_bytes,
            vm_configs=vm_configs,
        )
        capture_binding = validate_capture_status(
            capture_status_bytes,
            host_dtb_bytes=host_dtb_bytes,
            vm_configs=vm_configs,
            log_bytes=log_bytes,
            log_name=args.log.name,
            expected_vm_ids=[item["vmId"] for item in expected],
        )
        markers, dma_guard_markers = parse_runtime_log(
            log_bytes,
            expected=expected,
            expected_dma_guards=expected_dma_guards,
        )
        has_dma_guards = bool(expected_dma_guards)
        payload = {
            "schemaVersion": 1,
            "artifactStatus": "runtime-log-observation-validated",
            "status": (
                "host_carveout_and_dma_guard_markers_match_static_preflight"
                if has_dma_guards
                else "host_carveout_markers_match_static_preflight"
            ),
            "proofScope": (
                "byte-bound-host-log-carveout-and-dma-guard-marker-observation"
                if has_dma_guards
                else "byte-bound-host-log-carveout-marker-observation"
            ),
            "doesNotProve": ["Guest boot", "MapReserved stage-2 mapping", "passthrough DMA effect", "passthrough DMA isolation", "dual-Guest execution", "Guest IP connectivity"],
            "sources": {"rawLog": _source(args.log, log_bytes), "preflight": _source(args.preflight, preflight_bytes), "captureStatus": _source(args.capture_status, capture_status_bytes), "hostDtb": _source(args.host_dtb, host_dtb_bytes), "hostDts": _source(args.host_dts, host_dts_bytes), "vmConfigs": [_source(path, vm_configs[path.name]) for path in args.vm_config]},
            "captureStatusBinding": capture_binding,
            "expectedCarveouts": [{"vmId": item["vmId"], "hpa": f"{item['hpa']:#x}", "size": f"{item['size']:#x}"} for item in expected],
            "expectedDmaGuards": [
                {"hpa": f"{item['hpa']:#x}", "size": f"{item['size']:#x}"}
                for item in expected_dma_guards
            ],
            "markers": markers,
            "dmaGuardMarkers": dma_guard_markers,
        }
        publish_new_file(args.output, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"), error_type=HostCarveoutRuntimeEvidenceError)
    except (OSError, HostCarveoutRuntimeEvidenceError) as error:
        print(f"host carveout runtime log validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
