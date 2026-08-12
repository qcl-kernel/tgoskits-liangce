#!/usr/bin/env python3
"""Aggregate one byte-bound, literal Linux MapReserved boot-log chain.

This verifier deliberately joins existing, independently produced evidence.  It
does not launch a VM or infer any behaviour not written in the supplied log.
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


class LinuxMapReservedBootEvidenceError(ValueError):
    """The supplied evidence cannot establish the narrow literal observation."""


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
LOWER_HEX = re.compile(r"0x[0-9a-f]+\Z")
NONCE = re.compile(r"[0-9a-f]{32}\Z")
READY = re.compile(r"AXVISOR_GUEST_DTB_READY vm=1 gpa=0x[0-9a-f]+ size=[1-9][0-9]* hpa_segments=[^\s]+\Z")
STAGE2 = re.compile(
    r"AXVISOR_STAGE2_HPA_POSTRUN vm=1 vcpu=[0-9]+ kind=reserved "
    r"gpa=0x[0-9a-f]+ hpa=0x[0-9a-f]+ page_size=4096 identity=1\Z"
)
HOST_RESERVED = re.compile(
    r"AXVISOR_HOST_VM_CARVEOUT_RESERVED vm=1 hpa=0x[0-9a-f]+ size=0x[0-9a-f]+ "
    r"phase=before-ram-init\Z"
)
HOST_ALLOCATOR = re.compile(
    r"AXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED vm=1 hpa=0x[0-9a-f]+ size=0x[0-9a-f]+ "
    r"reserved_cover=1 free_overlap=0 phase=before-global-allocator-init\Z"
)
DMA_GUARD_RESERVED = re.compile(
    r"AXVISOR_HOST_DMA_GUARD_RESERVED hpa=0x[0-9a-f]+ size=0x[0-9a-f]+ "
    r"phase=before-ram-init\Z"
)
DMA_GUARD_ALLOCATOR = re.compile(
    r"AXVISOR_HOST_DMA_GUARD_ALLOCATOR_EXCLUDED hpa=0x[0-9a-f]+ size=0x[0-9a-f]+ "
    r"reserved_cover=1 free_overlap=0 phase=before-global-allocator-init\Z"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LinuxMapReservedBootEvidenceError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _json(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LinuxMapReservedBootEvidenceError(f"{label} is not strict duplicate-free JSON: {error}") from error
    if not isinstance(value, dict):
        raise LinuxMapReservedBootEvidenceError(f"{label} must be a JSON object")
    return value


def _uint(value: object, *, label: str, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (positive and value == 0):
        raise LinuxMapReservedBootEvidenceError(f"{label} must be a {'positive ' if positive else ''}integer")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise LinuxMapReservedBootEvidenceError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _hex(value: object, *, label: str, positive: bool = False) -> int:
    if not isinstance(value, str) or LOWER_HEX.fullmatch(value) is None:
        raise LinuxMapReservedBootEvidenceError(f"{label} must be lowercase hexadecimal")
    parsed = int(value, 16)
    if positive and parsed == 0:
        raise LinuxMapReservedBootEvidenceError(f"{label} must be non-zero")
    return parsed


def _relative_evidence_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LinuxMapReservedBootEvidenceError(f"{label} must be a non-empty relative path")
    candidate = Path(value)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
        raise LinuxMapReservedBootEvidenceError(f"{label} must be a normalized relative path")
    return value


def _source(value: object, *, label: str, data: bytes, require_size: bool = True) -> None:
    if not isinstance(value, dict):
        raise LinuxMapReservedBootEvidenceError(f"{label} must be an object")
    if not isinstance(value.get("path"), str) or not value["path"]:
        raise LinuxMapReservedBootEvidenceError(f"{label}.path must be non-empty")
    if _digest(value.get("sha256"), label=f"{label}.sha256") != _sha(data):
        raise LinuxMapReservedBootEvidenceError(f"{label} hash does not bind supplied bytes")
    if require_size:
        if _uint(value.get("size"), label=f"{label}.size") != len(data):
            raise LinuxMapReservedBootEvidenceError(f"{label} size does not bind supplied bytes")


def _line(lines: list[str], source_line: object, *, label: str) -> tuple[int, str]:
    number = _uint(source_line, label=f"{label}.sourceLine", positive=True)
    if number > len(lines):
        raise LinuxMapReservedBootEvidenceError(f"{label}.sourceLine is outside raw log")
    return number, lines[number - 1]


def _one(items: list[int], *, label: str) -> int:
    if len(items) != 1:
        raise LinuxMapReservedBootEvidenceError(f"{label} must occur exactly once")
    return items[0]


def _status(
    status: dict[str, Any],
    *,
    log: bytes,
    vm: bytes,
    capture_chain: bytes | None = None,
    guest_dtb: bytes | None = None,
) -> dict[str, Any]:
    if status.get("schemaVersion") != 1 or status.get("success") is not True:
        raise LinuxMapReservedBootEvidenceError("capture status is not a successful schemaVersion 1 artifact")
    if status.get("artifactStatus") != "capture-generated-unreviewed":
        raise LinuxMapReservedBootEvidenceError("capture status artifactStatus is not capture-generated-unreviewed")
    if status.get("status") != "single_guest_live_capture_passed":
        raise LinuxMapReservedBootEvidenceError("capture status is not single_guest_live_capture_passed")
    if status.get("proofScope") != "one-guest-launch-ready-same-session-qmp-capture-resume-identity-exit":
        raise LinuxMapReservedBootEvidenceError("capture status proofScope is not the expected one-Guest scope")
    state = status.get("state")
    if not isinstance(state, dict) or _uint(state.get("expectedVmId"), label="capture status expectedVmId", positive=True) != 1:
        raise LinuxMapReservedBootEvidenceError("capture status must identify exactly VM 1")
    inputs = state.get("inputArtifacts")
    if not isinstance(inputs, dict):
        raise LinuxMapReservedBootEvidenceError("capture status has no inputArtifacts")
    _source(inputs.get("vmconfig"), label="capture status vmconfig", data=vm)
    # The produced capture status intentionally binds liveLog by path+hash only.
    _source(state.get("liveLog"), label="capture status liveLog", data=log, require_size=False)
    marker = state.get("postResumeMarker")
    if not isinstance(marker, dict) or marker.get("marker") != "~ #":
        raise LinuxMapReservedBootEvidenceError("capture status must contain the literal '~ #' post-resume marker")
    if _uint(marker.get("markerBytes"), label="postResumeMarker.markerBytes") != 3:
        raise LinuxMapReservedBootEvidenceError("postResumeMarker.markerBytes must be 3")
    ready = state.get("ready")
    if not isinstance(ready, dict):
        raise LinuxMapReservedBootEvidenceError("capture status has no READY-prefix observation")
    if ready.get("capturePlanGuestIds") != [1]:
        raise LinuxMapReservedBootEvidenceError("READY capture plan must contain exactly VM 1")
    ready_size = _uint(ready.get("readyPrefixBytes"), label="readyPrefixBytes", positive=True)
    if ready_size > len(log) or _digest(ready.get("readyPrefixSha256"), label="readyPrefixSha256") != _sha(log[:ready_size]):
        raise LinuxMapReservedBootEvidenceError("READY-prefix observation does not match raw log bytes")
    if _uint(marker.get("readyPrefixBytes"), label="postResumeMarker.readyPrefixBytes") != ready_size:
        raise LinuxMapReservedBootEvidenceError("post-resume marker has a different READY-prefix size")
    offset = _uint(marker.get("byteOffset"), label="postResumeMarker.byteOffset")
    observed = _uint(marker.get("observedPrefixBytes"), label="postResumeMarker.observedPrefixBytes", positive=True)
    sealed = _uint(marker.get("sealedBeforeSigintPrefixBytes"), label="postResumeMarker.sealedBeforeSigintPrefixBytes", positive=True)
    if not ready_size <= offset <= observed - 3 or observed > len(log) or sealed > len(log):
        raise LinuxMapReservedBootEvidenceError("post-resume marker offsets are inconsistent with raw log bytes")
    if log[offset : offset + 3] != b"~ #" or log.count(b"~ #") != 1:
        raise LinuxMapReservedBootEvidenceError("raw log does not contain exactly one literal '~ #' marker")
    for key, size in (("observedPrefixSha256", observed), ("sealedBeforeSigintPrefixSha256", sealed)):
        if _digest(marker.get(key), label=f"postResumeMarker.{key}") != _sha(log[:size]):
            raise LinuxMapReservedBootEvidenceError(f"post-resume {key} does not match raw log prefix bytes")
    if observed != sealed or marker.get("observedPrefixSha256") != marker.get("sealedBeforeSigintPrefixSha256"):
        raise LinuxMapReservedBootEvidenceError("post-resume observation was not sealed at the same prefix")
    if _uint(marker.get("occurrencesInPostResumeRegion"), label="postResumeMarker.occurrencesInPostResumeRegion") != 1:
        raise LinuxMapReservedBootEvidenceError("post-resume marker must be observed once")
    nested = state.get("nestedCaptureChain")
    if not isinstance(nested, dict) or nested.get("status") != "identity_bound_live_qmp_capture_completed":
        raise LinuxMapReservedBootEvidenceError("nested capture chain did not complete with identity binding")
    nested_path = _relative_evidence_path(
        nested.get("path"), label="nestedCaptureChain.path"
    )
    nested_hash = _digest(nested.get("sha256"), label="nestedCaptureChain.sha256")
    resumed = state.get("postResumeIdentity")
    if not isinstance(resumed, dict) or resumed.get("pidfdBound") is not True:
        raise LinuxMapReservedBootEvidenceError("post-resume identity is not bound to a pidfd")
    if state.get("qemuExit") != "SIGINT sent through pidfd after nested resumed identity revalidation":
        raise LinuxMapReservedBootEvidenceError("capture status qemuExit is not the exact pidfd SIGINT result")
    if state.get("launcherExitCode") != 0:
        raise LinuxMapReservedBootEvidenceError("capture status launcher did not exit with code zero")
    cleanup = state.get("groupCleanup")
    if not isinstance(cleanup, dict) or cleanup.get("groupExited") is not True or cleanup.get("escalated") is not False:
        raise LinuxMapReservedBootEvidenceError("capture status group cleanup is not a non-escalated confirmed exit")
    runtime_cleanup = state.get("runtimeCleanup")
    if not isinstance(runtime_cleanup, dict) or runtime_cleanup.get("directoryRemoved") is not True:
        raise LinuxMapReservedBootEvidenceError("capture status runtime directory was not removed")
    result = {
        "expectedVmId": 1,
        "readyPrefixBytes": ready_size,
        "postResumeMarkerByteOffset": offset,
    }
    if (capture_chain is None) != (guest_dtb is None):
        raise LinuxMapReservedBootEvidenceError(
            "capture-chain and Guest-DTB inputs must be supplied together"
        )
    if capture_chain is None:
        return result
    if nested_path != "live-capture/capture-chain.json":
        raise LinuxMapReservedBootEvidenceError(
            "nested capture-chain path is not the canonical live-capture manifest"
        )
    if nested_hash != _sha(capture_chain):
        raise LinuxMapReservedBootEvidenceError(
            "nested capture-chain hash does not bind the supplied manifest"
        )
    chain = _json(capture_chain, label="capture chain")
    if (
        chain.get("schemaVersion") != 1
        or chain.get("artifactStatus") != "capture-generated-unreviewed"
        or chain.get("status") != "identity_bound_live_qmp_capture_completed"
        or chain.get("proofScope")
        != "same-session-paused-qmp-final-guest-dtb-capture-chain"
        or chain.get("sourcePathBase") != "capture-chain.json parent directory"
    ):
        raise LinuxMapReservedBootEvidenceError(
            "capture chain is not the expected identity-bound QMP manifest"
        )
    nonce = state.get("sessionNonce")
    if not isinstance(nonce, str) or NONCE.fullmatch(nonce) is None:
        raise LinuxMapReservedBootEvidenceError("capture status has no valid session nonce")
    if state.get("qemuName") != f"axvisor-guest-dtb-{nonce}":
        raise LinuxMapReservedBootEvidenceError(
            "capture status QEMU name is not bound to its session nonce"
        )
    if chain.get("sessionNonce") != nonce:
        raise LinuxMapReservedBootEvidenceError(
            "capture-chain nonce does not match capture status"
        )
    artifacts = chain.get("artifacts")
    if not isinstance(artifacts, list):
        raise LinuxMapReservedBootEvidenceError("capture chain has no artifacts list")
    matching_dtb = [
        item
        for item in artifacts
        if isinstance(item, dict) and item.get("path") == "guest-vm-1.final.dtb"
    ]
    if len(matching_dtb) != 1:
        raise LinuxMapReservedBootEvidenceError(
            "capture chain must bind exactly one VM-1 final Guest DTB"
        )
    _source(
        matching_dtb[0],
        label="capture chain VM-1 final Guest DTB",
        data=guest_dtb,
    )
    result["captureChain"] = {
        "path": nested_path,
        "sha256Match": True,
        "sessionNonceMatch": True,
        "guestDtbMatch": True,
    }
    return result


def _semantic(report: dict[str, Any], *, dts: bytes, vm: bytes) -> str:
    if report.get("schemaVersion") != 1 or report.get("status") != "captured_guest_dtb_static_semantics_validated":
        raise LinuxMapReservedBootEvidenceError("guest semantic report has the wrong successful status")
    sources = report.get("sources")
    if not isinstance(sources, dict):
        raise LinuxMapReservedBootEvidenceError("guest semantic report has no sources")
    _source(sources.get("dts"), label="guest semantic dts", data=dts, require_size=False)
    _source(sources.get("vmConfig"), label="guest semantic vmConfig", data=vm, require_size=False)
    profile = report.get("vm")
    if not isinstance(profile, dict) or _uint(profile.get("id"), label="guest semantic vm.id", positive=True) != 1:
        raise LinuxMapReservedBootEvidenceError("guest semantic report must describe exactly VM 1")
    mode = profile.get("consoleMode")
    if mode not in ("vm-owned-polling-pl011", "passthrough-or-no-vm-owned-console"):
        raise LinuxMapReservedBootEvidenceError("guest semantic report has an unknown consoleMode")
    return mode


def _runtime_report(report: dict[str, Any], *, kind: str, log: bytes, status: bytes, vm: bytes) -> dict[str, Any]:
    if kind == "stage2":
        wanted = {"stage2_hpa_markers_match_configured_regions"}
    else:
        wanted = {
            "host_carveout_markers_match_static_preflight",
            "host_carveout_and_dma_guard_markers_match_static_preflight",
        }
    if report.get("schemaVersion") != 1 or report.get("artifactStatus") != "runtime-log-observation-validated" or report.get("status") not in wanted:
        raise LinuxMapReservedBootEvidenceError(f"{kind} report has the wrong successful status")
    sources = report.get("sources")
    if not isinstance(sources, dict):
        raise LinuxMapReservedBootEvidenceError(f"{kind} report has no sources")
    _source(sources.get("rawLog"), label=f"{kind} rawLog", data=log)
    _source(sources.get("captureStatus"), label=f"{kind} captureStatus", data=status)
    configs = sources.get("vmConfigs")
    if not isinstance(configs, list) or len(configs) != 1:
        raise LinuxMapReservedBootEvidenceError(f"{kind} report must bind exactly one VM config")
    _source(configs[0], label=f"{kind} vmConfig", data=vm)
    binding = report.get("captureStatusBinding")
    if not isinstance(binding, dict) or binding.get("validated") is not True or binding.get("status") != "single_guest_live_capture_passed" or binding.get("expectedVmId") != 1:
        raise LinuxMapReservedBootEvidenceError(f"{kind} report capture-status binding is invalid")
    return report


def _validate_vm(vm: bytes) -> None:
    try:
        parsed = tomllib.loads(vm.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise LinuxMapReservedBootEvidenceError(f"VM config is not strict TOML: {error}") from error
    base = parsed.get("base")
    if not isinstance(base, dict) or _uint(base.get("id"), label="VM config base.id", positive=True) != 1:
        raise LinuxMapReservedBootEvidenceError("VM config must identify exactly VM 1")


def _dma_guard_binding(host: dict[str, Any], *, lines: list[str], host_early: int, host_allocator: int, ready: int) -> dict[str, Any] | None:
    if host.get("status") == "host_carveout_markers_match_static_preflight":
        return None
    expected = host.get("expectedDmaGuards")
    markers = host.get("dmaGuardMarkers")
    if not isinstance(expected, list) or not expected or not isinstance(markers, list) or len(markers) != len(expected):
        raise LinuxMapReservedBootEvidenceError("DMA-guard host report must bind non-empty expected guards to marker pairs")
    pairs: list[tuple[int, int]] = []
    early_lines: list[int] = []
    allocator_lines: list[int] = []
    bound_markers: list[dict[str, Any]] = []
    for index, (item, marker) in enumerate(zip(expected, markers, strict=True)):
        if not isinstance(item, dict) or set(item) != {"hpa", "size"}:
            raise LinuxMapReservedBootEvidenceError("DMA-guard expected tuple must contain exactly hpa and size")
        hpa = _hex(item.get("hpa"), label=f"DMA-guard expected[{index}].hpa", positive=True)
        size = _hex(item.get("size"), label=f"DMA-guard expected[{index}].size", positive=True)
        if not isinstance(marker, dict) or set(marker) != {"hpa", "size", "earlyReservedLine", "allocatorExcludedLine"}:
            raise LinuxMapReservedBootEvidenceError("DMA-guard marker pair has unexpected fields")
        if marker.get("hpa") != item["hpa"] or marker.get("size") != item["size"]:
            raise LinuxMapReservedBootEvidenceError("DMA-guard marker pair does not bind its expected tuple")
        early_no, early_line = _line(lines, marker.get("earlyReservedLine"), label=f"DMA-guard early marker {index}")
        allocator_no, allocator_line = _line(lines, marker.get("allocatorExcludedLine"), label=f"DMA-guard allocator marker {index}")
        if DMA_GUARD_RESERVED.fullmatch(early_line) is None or early_line != (
            f"AXVISOR_HOST_DMA_GUARD_RESERVED hpa={item['hpa']} size={item['size']} phase=before-ram-init"
        ):
            raise LinuxMapReservedBootEvidenceError("DMA-guard early source line does not extract its exact raw-log marker")
        if DMA_GUARD_ALLOCATOR.fullmatch(allocator_line) is None or allocator_line != (
            f"AXVISOR_HOST_DMA_GUARD_ALLOCATOR_EXCLUDED hpa={item['hpa']} size={item['size']} "
            "reserved_cover=1 free_overlap=0 phase=before-global-allocator-init"
        ):
            raise LinuxMapReservedBootEvidenceError("DMA-guard allocator source line does not extract its exact raw-log marker")
        pairs.append((hpa, size))
        early_lines.append(early_no)
        allocator_lines.append(allocator_no)
        bound_markers.append({"hpa": item["hpa"], "size": item["size"], "earlyReservedLine": early_no, "allocatorExcludedLine": allocator_no})
    if pairs != sorted(pairs) or len(set(pairs)) != len(pairs):
        raise LinuxMapReservedBootEvidenceError("DMA-guard expected tuples must be unique and in ascending HPA order")
    if any(left_hpa + left_size > right_hpa for (left_hpa, left_size), (right_hpa, _) in zip(pairs, pairs[1:], strict=False)):
        raise LinuxMapReservedBootEvidenceError("DMA-guard expected tuples overlap")
    if early_lines != sorted(early_lines) or allocator_lines != sorted(allocator_lines):
        raise LinuxMapReservedBootEvidenceError("DMA-guard marker pairs are not in expected HPA order")
    if max([host_early, *early_lines]) >= min([host_allocator, *allocator_lines]):
        raise LinuxMapReservedBootEvidenceError("DMA-guard allocator markers must follow all early reservations")
    if max(allocator_lines) >= ready:
        raise LinuxMapReservedBootEvidenceError("DMA-guard allocator markers must precede the READY marker")
    if sum(value.startswith("AXVISOR_HOST_DMA_GUARD_RESERVED ") for value in lines) != len(expected):
        raise LinuxMapReservedBootEvidenceError("DMA-guard early markers must occur exactly once per expected guard")
    if sum(value.startswith("AXVISOR_HOST_DMA_GUARD_ALLOCATOR_EXCLUDED ") for value in lines) != len(expected):
        raise LinuxMapReservedBootEvidenceError("DMA-guard allocator markers must occur exactly once per expected guard")
    return {
        "hostRuntimeStatus": host["status"],
        "expectedDmaGuards": [{"hpa": item["hpa"], "size": item["size"]} for item in expected],
        "markerPairs": bound_markers,
        "evidenceBoundary": "allocator/runtime marker observation only; does not prove DMA isolation",
    }


def _observations(lines: list[str], *, stage: dict[str, Any], host: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    stage_markers = stage.get("markers")
    if not isinstance(stage_markers, list) or len(stage_markers) != 1 or not isinstance(stage_markers[0], dict):
        raise LinuxMapReservedBootEvidenceError("stage2 report must contain exactly one marker")
    stage_marker = stage_markers[0]
    if (stage_marker.get("vmId"), stage_marker.get("kind"), stage_marker.get("identity")) != (1, "reserved", 1):
        raise LinuxMapReservedBootEvidenceError("stage2 report marker is not unique reserved GPA=HPA identity=1")
    vcpu = _uint(stage_marker.get("vcpuId"), label="stage2 marker.vcpuId")
    page_size = _uint(stage_marker.get("pageSize"), label="stage2 marker.pageSize")
    gpa = _hex(stage_marker.get("gpa"), label="stage2 marker.gpa")
    hpa = _hex(stage_marker.get("hpa"), label="stage2 marker.hpa")
    if page_size != 4096 or gpa != hpa:
        raise LinuxMapReservedBootEvidenceError("stage2 report marker must be 4096-byte reserved GPA=HPA")
    stage_number, stage_line = _line(lines, stage_marker.get("sourceLine"), label="stage2 marker")
    if STAGE2.fullmatch(stage_line) is None or stage_line != (
        f"AXVISOR_STAGE2_HPA_POSTRUN vm=1 vcpu={vcpu} kind=reserved "
        f"gpa={stage_marker.get('gpa')} hpa={stage_marker.get('hpa')} page_size={stage_marker.get('pageSize')} identity=1"
    ):
        raise LinuxMapReservedBootEvidenceError("stage2 sourceLine does not extract its exact raw-log marker")
    host_markers = host.get("markers")
    if not isinstance(host_markers, list) or len(host_markers) != 1 or not isinstance(host_markers[0], dict):
        raise LinuxMapReservedBootEvidenceError("host report must contain exactly one marker")
    host_marker = host_markers[0]
    if host_marker.get("vmId") != 1:
        raise LinuxMapReservedBootEvidenceError("host report marker must identify VM 1")
    early_no, early_line = _line(lines, host_marker.get("earlyReservedLine"), label="host early marker")
    alloc_no, alloc_line = _line(lines, host_marker.get("allocatorExcludedLine"), label="host allocator marker")
    hpa, size = host_marker.get("hpa"), host_marker.get("size")
    _hex(hpa, label="host marker.hpa")
    _hex(size, label="host marker.size", positive=True)
    if HOST_RESERVED.fullmatch(early_line) is None or early_line != f"AXVISOR_HOST_VM_CARVEOUT_RESERVED vm=1 hpa={hpa} size={size} phase=before-ram-init":
        raise LinuxMapReservedBootEvidenceError("host early source line does not extract its exact raw-log marker")
    if HOST_ALLOCATOR.fullmatch(alloc_line) is None or alloc_line != f"AXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED vm=1 hpa={hpa} size={size} reserved_cover=1 free_overlap=0 phase=before-global-allocator-init":
        raise LinuxMapReservedBootEvidenceError("host allocator source line does not extract its exact raw-log marker")
    for label, prefix in (
        ("host reservation marker", "AXVISOR_HOST_VM_CARVEOUT_RESERVED "),
        ("host allocator marker", "AXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED "),
        ("stage2 marker", "AXVISOR_STAGE2_HPA_POSTRUN "),
    ):
        if sum(value.startswith(prefix) for value in lines) != 1:
            raise LinuxMapReservedBootEvidenceError(f"{label} must occur exactly once")
    ready = _one([index + 1 for index, value in enumerate(lines) if READY.fullmatch(value)], label="READY marker")
    dma_guard = _dma_guard_binding(host, lines=lines, host_early=early_no, host_allocator=alloc_no, ready=ready)
    linux = _one([index + 1 for index, value in enumerate(lines) if "Linux version " in value], label="Linux version")
    no_reserved = _one([index + 1 for index, value in enumerate(lines) if "No reserved-memory node in the DT" in value], label="No reserved-memory DT message")
    shell = _one([index + 1 for index, value in enumerate(lines) if "Run /bin/sh as init process" in value], label="init shell message")
    prompt = _one([index + 1 for index, value in enumerate(lines) if "~ #" in value], label="shell prompt")
    chain = [early_no, alloc_no, ready, stage_number, linux, no_reserved, shell, prompt]
    if chain != sorted(chain) or len(set(chain)) != len(chain):
        raise LinuxMapReservedBootEvidenceError("literal boot observations are not in the required strict order")
    observations = [
        {"name": name, "sourceLine": number, "literalLine": lines[number - 1]}
        for name, number in zip(("hostReserved", "allocatorExcluded", "ready", "stage2ReservedIdentity", "linuxVersion", "noReservedMemory", "initShell", "shellPrompt"), chain, strict=True)
    ]
    return observations, dma_guard


def _input(path: Path, data: bytes) -> dict[str, Any]:
    return {"path": str(path), "size": len(data), "sha256": _sha(data)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--capture-status", required=True, type=Path)
    parser.add_argument("--guest-dts", required=True, type=Path)
    parser.add_argument("--guest-semantic-report", required=True, type=Path)
    parser.add_argument("--stage2-report", required=True, type=Path)
    parser.add_argument("--host-carveout-report", required=True, type=Path)
    parser.add_argument("--vm-config", required=True, type=Path)
    parser.add_argument("--capture-chain", type=Path)
    parser.add_argument("--guest-dtb", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if (args.capture_chain is None) != (args.guest_dtb is None):
            raise LinuxMapReservedBootEvidenceError(
                "--capture-chain and --guest-dtb must be supplied together"
            )
        inputs = [args.log, args.capture_status, args.guest_dts, args.guest_semantic_report, args.stage2_report, args.host_carveout_report, args.vm_config]
        if args.capture_chain is not None and args.guest_dtb is not None:
            inputs.extend((args.capture_chain, args.guest_dtb))
        if any(path.resolve() == args.output.resolve() for path in inputs):
            raise LinuxMapReservedBootEvidenceError("--output must not overwrite an input artifact")
        raw = {str(path): read_regular_bytes(path, label=str(path), error_type=LinuxMapReservedBootEvidenceError) for path in inputs}
        if any(not data for data in raw.values()):
            raise LinuxMapReservedBootEvidenceError("every evidence input must be non-empty")
        log, status_bytes, dts, semantic_bytes, stage_bytes, host_bytes, vm = (
            raw[str(path)] for path in inputs[:7]
        )
        capture_chain = (
            None
            if args.capture_chain is None
            else raw[str(args.capture_chain)]
        )
        guest_dtb = None if args.guest_dtb is None else raw[str(args.guest_dtb)]
        _validate_vm(vm)
        status = _json(status_bytes, label="capture status")
        semantic = _json(semantic_bytes, label="guest semantic report")
        capture = _status(
            status,
            log=log,
            vm=vm,
            capture_chain=capture_chain,
            guest_dtb=guest_dtb,
        )
        stage = _runtime_report(_json(stage_bytes, label="stage2 report"), kind="stage2", log=log, status=status_bytes, vm=vm)
        host = _runtime_report(_json(host_bytes, label="host carveout report"), kind="host", log=log, status=status_bytes, vm=vm)
        console_mode = _semantic(semantic, dts=dts, vm=vm)
        try:
            lines = log.decode("utf-8", errors="strict").splitlines()
        except UnicodeDecodeError as error:
            raise LinuxMapReservedBootEvidenceError("raw log is not strict UTF-8") from error
        observations, dma_guard = _observations(lines, stage=stage, host=host)
        payload = {
            "schemaVersion": 1,
            "artifactStatus": "runtime-log-observation-validated",
            "status": "linux_map_reserved_single_guest_boot_chain_observed",
            "proofScope": "byte-bound-literal-single-guest-linux-mapreserved-boot-log-chain",
            "doesNotProve": ["DMA isolation", "dual Guest execution", "30min stability", "Linux/Zephyr IP connectivity", "passthrough console isolation", "actual semantics beyond literal bytes"],
            "sources": {"rawLog": _input(args.log, log), "captureStatus": _input(args.capture_status, status_bytes), "guestDts": _input(args.guest_dts, dts), "guestSemanticReport": _input(args.guest_semantic_report, semantic_bytes), "stage2Report": _input(args.stage2_report, stage_bytes), "hostCarveoutReport": _input(args.host_carveout_report, host_bytes), "vmConfig": _input(args.vm_config, vm)},
            "captureStatusBinding": capture,
            "guestSemanticBinding": {"status": "captured_guest_dtb_static_semantics_validated", "consoleModeRecordedOnly": console_mode},
            "literalObservations": observations,
        }
        if args.capture_chain is not None and args.guest_dtb is not None:
            payload["sources"]["captureChain"] = _input(
                args.capture_chain, capture_chain
            )
            payload["sources"]["guestDtb"] = _input(args.guest_dtb, guest_dtb)
        if dma_guard is not None:
            payload["hostDmaGuardBinding"] = dma_guard
        publish_new_file(args.output, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"), error_type=LinuxMapReservedBootEvidenceError)
    except (OSError, LinuxMapReservedBootEvidenceError) as error:
        print(f"Linux MapReserved boot runtime validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
