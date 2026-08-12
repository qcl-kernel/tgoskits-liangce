#!/usr/bin/env python3
"""Static regression contract for AxVM MapReserved HPA/HVA accounting."""

from __future__ import annotations

import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
VM = WORKSPACE_ROOT / "virtualization/axvm/src/vm/mod.rs"
MEMORY = WORKSPACE_ROOT / "virtualization/axvm/src/vm/memory.rs"
PAGING = WORKSPACE_ROOT / "virtualization/axvm/src/host/paging.rs"
HOST_TRAITS = WORKSPACE_ROOT / "virtualization/axvm/src/host/traits.rs"
ARCEOS_HOST = WORKSPACE_ROOT / "virtualization/axvm/src/host/arceos.rs"
DESIGN = (
    WORKSPACE_ROOT
    / "docs/docs/development/axvisor-fixed-hpa-allocator-reservation.md"
)
ZEPHYR_DUAL = (
    WORKSPACE_ROOT
    / "os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml"
)
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"


def read_text(path: Path, *, label: str, errors: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"{label} cannot be read: {error}")
        return ""


def require_fragments(
    text: str, fragments: tuple[str, ...], *, label: str, errors: list[str]
) -> None:
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{label} is missing `{fragment}`")


def function_body(text: str, signature: str, next_marker: str) -> str:
    start = text.find(signature)
    if start < 0:
        return ""
    end = text.find(next_marker, start)
    return text[start:] if end < 0 else text[start:end]


def main() -> int:
    errors: list[str] = []

    vm = read_text(VM, label="AxVM reserved mapping", errors=errors)
    reserved_body = function_body(
        vm,
        "pub fn map_reserved_memory_region(",
        "/// Destroys the VM",
    )
    require_fragments(
        reserved_body,
        (
            "memory::authorized_reserved_host_addresses(",
            "host::default_host().owns_vm_carveout(vm_id, hpa, size)",
            "host::paging::phys_to_virt",
            ".map_linear(\n                    gpa,\n                    hpa,",
            "VMMemoryRegion {\n                gpa,\n                hva,",
            "needs_dealloc: false",
        ),
        label="AxVM reserved mapping",
        errors=errors,
    )
    if "let hva = gpa.as_usize().into();" in reserved_body:
        errors.append("MapReserved still treats a guest physical address as a host virtual one")
    authorization = reserved_body.find("memory::authorized_reserved_host_addresses(")
    mutable_resources = reserved_body.find("self.with_resources_mut")
    if authorization < 0 or mutable_resources < 0 or authorization > mutable_resources:
        errors.append(
            "MapReserved does not complete carveout authorization before mutable VM access"
        )

    memory = read_text(MEMORY, label="reserved address helper", errors=errors)
    require_fragments(
        memory,
        (
            "pub(crate) fn reserved_host_addresses(",
            "let hpa = HostPhysAddr::from(gpa.as_usize());",
            "let hva = translate(hpa);",
            "pub(crate) fn authorized_reserved_host_addresses(",
            "if !authorize(vm_id, hpa, size)",
            '"VM host-memory carveout"',
            "reserved_mapping_translates_hpa_through_host_direct_map",
            "reserved_mapping_denial_precedes_host_address_translation",
            "assert_eq!(translation_calls.get(), 0);",
            "assert_ne!(hva.as_usize(), gpa.as_usize());",
        ),
        label="reserved address helper and regression test",
        errors=errors,
    )
    denial = memory.find("if !authorize(vm_id, hpa, size)")
    translation = memory.find("Ok(reserved_host_addresses(gpa, translate))")
    if denial < 0 or translation < 0 or denial > translation:
        errors.append("reserved mapping translates HPA before carveout authorization")

    host_traits = read_text(HOST_TRAITS, label="host memory capability", errors=errors)
    require_fragments(
        host_traits,
        (
            "fn owns_vm_carveout(&self, vm_id: usize, paddr: HostPhysAddr, size: usize) -> bool;",
            "pub(crate) fn exact_vm_carveout_match(",
            "let Ok(requested_vm_id) = u32::try_from(requested_vm_id)",
            "vm_id == requested_vm_id",
            "start == requested_start.as_usize()",
            "size == requested_size",
            "exact_vm_carveout_match_accepts_only_the_full_owner_tuple",
            "exact_vm_carveout_match_fails_closed_for_missing_or_unrepresentable_owner",
        ),
        label="host memory carveout ownership capability",
        errors=errors,
    )

    arceos_host = read_text(ARCEOS_HOST, label="ArceOS host adapter", errors=errors)
    ownership_body = function_body(
        arceos_host,
        "fn owns_vm_carveout(",
        "fn phys_to_virt(",
    )
    require_fragments(
        ownership_body,
        (
            "exact_vm_carveout_match(",
            "modules::ax_hal::mem::vm_carveouts()",
            "carveout.vm_id",
            "carveout.physical_start",
            "carveout.size",
        ),
        label="ArceOS immutable carveout manifest adapter",
        errors=errors,
    )
    if "reserved_phys_ram_ranges" in ownership_body:
        errors.append("ArceOS carveout ownership is inferred from generic reserved RAM")

    paging = read_text(PAGING, label="host paging adapter", errors=errors)
    require_fragments(
        paging,
        (
            "pub(crate) fn phys_to_virt(paddr: PhysAddr) -> VirtAddr",
            "default_host().phys_to_virt(paddr)",
        ),
        label="host paging adapter",
        errors=errors,
    )

    design = read_text(DESIGN, label="fixed-HPA design", errors=errors)
    require_fragments(
        design,
        (
            "状态：Phase A/B 源码与编译门禁完成；Phase C 实机 allocator/DMA/IP 仍阻塞",
            "简单检查目标范围是否落入 `RESERVED`",
            "fixed-HPA/DMA 运行 gate 继续保持阻塞",
        ),
        label="fixed-HPA design",
        errors=errors,
    )

    zephyr = read_text(ZEPHYR_DUAL, label="Zephyr dual-guest config", errors=errors)
    if "[0x4000_0000, 0x0800_0000, 0x7, 0]" not in zephyr:
        errors.append("Zephyr dual config changed MapAlloc before carveout validation exists")
    if "# status: blocked_dma_console" not in zephyr:
        errors.append("Zephyr dual config overstates fixed-HPA/DMA readiness")

    ci = read_text(CI, label="CI workflow", errors=errors)
    if "python3 scripts/test/check_axvm_map_reserved_hpa.py" not in ci:
        errors.append("MapReserved HPA/HVA regression contract is not wired into CI")

    if not errors:
        return 0

    print("AxVM MapReserved HPA/HVA contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
