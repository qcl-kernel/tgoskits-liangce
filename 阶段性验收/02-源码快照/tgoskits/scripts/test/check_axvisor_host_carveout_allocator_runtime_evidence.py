#!/usr/bin/env python3
"""Static contract for default-off host carveout and DMA-guard allocator evidence."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "os/arceos/modules/axruntime/src/lib.rs"
RUNTIME_CARGO = ROOT / "os/arceos/modules/axruntime/Cargo.toml"
AXSTD_CARGO = ROOT / "os/arceos/ulib/axstd/Cargo.toml"
AXVISOR_CARGO = ROOT / "os/axvisor/Cargo.toml"
PLATFORM_MEM = ROOT / "platforms/ax-plat/src/mem.rs"
DMA_GUARD_PROFILE = (
    ROOT / "os/axvisor/configs/board/qemu-aarch64-linux-dma-guard-evidence.toml"
)
DEFAULT_QEMU_AARCH64_PROFILE = ROOT / "os/axvisor/configs/board/qemu-aarch64.toml"
FEATURE = "host-carveout-allocator-evidence"
DMA_GUARD_FEATURE = "host-dma-guard-allocator-evidence"
MARKER = (
    "AXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED vm={} hpa={:#x} size={:#x} "
    "reserved_cover=1 free_overlap=0 phase=before-global-allocator-init"
)
MARKER_PREFIX = "AXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED vm={} hpa={:#x} size={:#x}"
MARKER_SUFFIX = "reserved_cover=1 free_overlap=0 phase=before-global-allocator-init"
DMA_GUARD_MARKER = (
    "AXVISOR_HOST_DMA_GUARD_ALLOCATOR_EXCLUDED hpa={:#x} size={:#x} "
    "reserved_cover=1 free_overlap=0 phase=before-global-allocator-init"
)
DMA_GUARD_MARKER_PREFIX = "AXVISOR_HOST_DMA_GUARD_ALLOCATOR_EXCLUDED hpa={:#x} size={:#x}"


def load_features(path: Path, errors: list[str]) -> dict[str, object]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")).get("features", {})
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"cannot read {path}: {error}")
        return {}


def main() -> int:
    errors: list[str] = []
    carveout_feature_chain = {
        RUNTIME_CARGO: [],
        AXSTD_CARGO: ["ax-runtime/host-carveout-allocator-evidence"],
        AXVISOR_CARGO: ["ax-std/host-carveout-allocator-evidence"],
    }
    dma_guard_feature_chain = {
        RUNTIME_CARGO: [],
        AXSTD_CARGO: ["ax-runtime/host-dma-guard-allocator-evidence"],
        AXVISOR_CARGO: ["ax-std/host-dma-guard-allocator-evidence"],
    }
    for feature, expected in (
        (FEATURE, carveout_feature_chain),
        (DMA_GUARD_FEATURE, dma_guard_feature_chain),
    ):
        for path, mapping in expected.items():
            features = load_features(path, errors)
            if features.get(feature) != mapping:
                errors.append(f"{path} has the wrong {feature} feature mapping")
            if feature in features.get("default", []):
                errors.append(f"{path} enables {feature} by default")

    try:
        dma_guard_profile = tomllib.loads(DMA_GUARD_PROFILE.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"cannot read {DMA_GUARD_PROFILE}: {error}")
    else:
        if DMA_GUARD_FEATURE not in dma_guard_profile.get("features", []):
            errors.append("DMA guard evidence profile does not enable its evidence feature")
        if dma_guard_profile.get("vm_configs") != []:
            errors.append("DMA guard evidence profile is not disposable (vm_configs must be empty)")

    try:
        default_qemu_profile = tomllib.loads(
            DEFAULT_QEMU_AARCH64_PROFILE.read_text(encoding="utf-8")
        )
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"cannot read {DEFAULT_QEMU_AARCH64_PROFILE}: {error}")
    else:
        if DMA_GUARD_FEATURE in default_qemu_profile.get("features", []):
            errors.append("default qemu-aarch64 profile enables DMA guard evidence")

    try:
        runtime = RUNTIME.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"cannot read runtime evidence source: {error}")
        runtime = ""

    required = (
        '#[cfg(feature = "host-carveout-allocator-evidence")]\n    emit_host_vm_carveout_allocator_evidence();',
        "fn validate_host_vm_carveout_allocator_evidence",
        "CarveoutRangeOverflow",
        "MissingReservedCover",
        "FreeRegionOverlap",
        "start.checked_add(size)",
        "region_start <= carveout_start && carveout_end <= region_end",
        "ranges_overlap(carveout_start, carveout_end, region_start, region_end)",
        "region.flags.contains(MemRegionFlags::RESERVED)",
        "region.flags.contains(MemRegionFlags::FREE)",
        "ax_println!(",
        MARKER_PREFIX,
        MARKER_SUFFIX,
        "accepts_complete_reserved_cover_without_free_overlap",
        "rejects_missing_reserved_cover",
        "rejects_one_byte_free_overlap",
        "accepts_adjacent_free_region",
        "rejects_overflowing_carveout",
        "checks_each_carveout_in_input_order",
    )
    for fragment in required:
        if fragment not in runtime:
            errors.append(f"runtime evidence source is missing `{fragment}`")

    marker = runtime.find(MARKER_PREFIX)
    call = runtime.find("emit_host_vm_carveout_allocator_evidence();")
    first_global_init = runtime.find("ax_alloc::global_init(")
    if min(marker, call, first_global_init) < 0 or not call < first_global_init:
        errors.append("allocator evidence is not called before the first global_init")
    if "phys_to_virt(carveout" in runtime or "VirtAddr" in runtime[marker : marker + len(MARKER) + 200]:
        errors.append("allocator evidence marker leaks a virtual address")

    dma_guard_required = (
        '#[cfg(feature = "host-dma-guard-allocator-evidence")]\n    emit_host_dma_guard_allocator_evidence();',
        "fn validate_host_dma_guard_allocator_evidence",
        "EmptyGuardRange",
        "GuardRangeOverflow",
        "GuardOverlap",
        "MissingReservedCover",
        "FreeRegionOverlap",
        "Complete validation deliberately precedes the marker loop",
        DMA_GUARD_MARKER_PREFIX,
        "rejects_duplicate_guard_range",
        "validation_before_marker_loop_is_all_or_none",
        "accepts_empty_manifest_without_marker_candidates",
    )
    for fragment in dma_guard_required:
        if fragment not in runtime:
            errors.append(f"DMA guard runtime evidence source is missing `{fragment}`")

    dma_guard_marker = runtime.find(DMA_GUARD_MARKER_PREFIX)
    dma_guard_call = runtime.find("emit_host_dma_guard_allocator_evidence();")
    if (
        min(dma_guard_marker, dma_guard_call, first_global_init) < 0
        or not dma_guard_call < first_global_init
    ):
        errors.append("DMA guard evidence is not called before the first global_init")
    if (
        "phys_to_virt(guard" in runtime
        or "VirtAddr" in runtime[dma_guard_marker : dma_guard_marker + len(DMA_GUARD_MARKER) + 200]
    ):
        errors.append("DMA guard allocator evidence marker leaks a virtual address")

    try:
        platform_mem = PLATFORM_MEM.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"cannot read platform DMA guard API: {error}")
    else:
        if "its presence is not proof of DMA isolation" not in platform_mem:
            errors.append("platform DMA guard API overstates the reservation as DMA isolation")

    if errors:
        print("AxVisor host carveout allocator runtime evidence check failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
