#!/usr/bin/env python3
"""Static contract for AArch64 passthrough SPI routing.

The contract deliberately checks the architecture boundary instead of merely
looking for a successful QEMU marker: a VM identifier is not a physical CPU,
and a specific GICv3 affinity route must leave IROUTER.IRM clear.
"""

from __future__ import annotations

from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
AXVM_AARCH64_VM = WORKSPACE_ROOT / "virtualization/axvm/src/arch/aarch64/vm.rs"
VCPU_PLACEMENTS = WORKSPACE_ROOT / "virtualization/axvm/src/vm/prepare/vcpus.rs"
VGICD = WORKSPACE_ROOT / "virtualization/arm_vgic/src/v3/vgicd.rs"
VGICR = WORKSPACE_ROOT / "virtualization/arm_vgic/src/v3/vgicr.rs"
AXDEVICE = WORKSPACE_ROOT / "virtualization/axdevice/src/device.rs"
ARCH_OPS = WORKSPACE_ROOT / "virtualization/axvm/src/architecture/ops.rs"
AXVM_VM = WORKSPACE_ROOT / "virtualization/axvm/src/vm/mod.rs"


def section(source: str, start: str, end: str) -> str:
    start_index = source.find(start)
    if start_index < 0:
        return ""
    end_index = source.find(end, start_index + len(start))
    return source[start_index:] if end_index < 0 else source[start_index:end_index]


def require(errors: list[str], source: str, path: Path, snippets: tuple[str, ...]) -> None:
    for snippet in snippets:
        if snippet not in source:
            errors.append(f"{path.relative_to(WORKSPACE_ROOT)} is missing `{snippet}`")


def check_axvm_route(errors: list[str]) -> None:
    source = AXVM_AARCH64_VM.read_text(encoding="utf-8")
    if "vm.id() - 1" in source:
        errors.append("AArch64 passthrough SPI routing still derives a pCPU from the VM id")

    require(
        errors,
        source,
        AXVM_AARCH64_VM,
        (
            "register_arch_devices(resources.config(), &placements, &mut devices.devices)",
            "placements: &[VcpuPlacement]",
            ".single_pcpu_id(crate::percpu::enabled_cpu_mask())",
            "mpidr_to_affinity(placement.phys_cpu_id)",
            "fn mpidr_to_affinity(mpidr: usize) -> (u8, u8, u8, u8)",
            "((mpidr >> 32) & 0xff) as u8",
            "((mpidr >> 16) & 0xff) as u8",
            "((mpidr >> 8) & 0xff) as u8",
            "(mpidr & 0xff) as u8",
            "mpidr_to_affinity_extracts_all_levels",
            "fn release_vm_devices(devices: &axdevice::AxVmDevices)",
            "gicd.release_assigned_irqs()",
            ".checked_add(32)",
            "AxVmError::resource_unavailable",
        ),
    )

    assign = section(source, "fn assign_passthrough_spis", "fn register_virtual_timers")
    for snippet in ("placements: &[VcpuPlacement]", "gicd.assign_irq", "cpu_id", "affinity"):
        if snippet not in assign:
            errors.append(f"assign_passthrough_spis is missing `{snippet}`")


def check_placement(errors: list[str]) -> None:
    source = VCPU_PLACEMENTS.read_text(encoding="utf-8")
    require(
        errors,
        source,
        VCPU_PLACEMENTS,
        (
            "impl VcpuPlacement",
            "fn single_pcpu_id(self, enabled_cpu_mask: usize) -> Option<usize>",
            "mask.is_power_of_two()",
            "mask.trailing_zeros() as usize",
            "enabled_cpu_mask != 0",
            "single_pcpu_id_accepts_one_hot_mask",
            "single_pcpu_id_rejects_missing_zero_or_multi_bit_masks",
            "single_pcpu_id_rejects_disabled_cpu",
        ),
    )


def check_irouter(errors: list[str]) -> None:
    source = VGICD.read_text(encoding="utf-8")
    require(
        errors,
        source,
        VGICD,
        (
            "fn encode_specific_irouter(target_cpu_affinity: (u8, u8, u8, u8)) -> u64",
            "encode_specific_irouter(target_cpu_affinity)",
            "specific_irouter_encodes_affinity_with_irm_clear",
            "assert_eq!(value & (1u64 << 31), 0);",
            "struct RouteState",
            "saved_irouters",
            "pub fn release_assigned_irqs(&self)",
            "impl Drop for VGicD",
            "route_state_preserves_the_first_snapshot",
            "taking_saved_routes_is_idempotent",
            "released_route_state_blocks_stale_irq_access",
        ),
    )
    assign = section(source, "pub fn assign_irq", "impl BaseDeviceOps")
    if "| 1 << 31" in assign or "| 1u64 << 31" in assign:
        errors.append("VGicD::assign_irq still sets IROUTER.IRM for a specific affinity")
    if "GICD_ITARGETSR" in assign:
        errors.append("GICv3 VGicD::assign_irq still writes the legacy ITARGETSR")
    if "cpu_phys_id >= u8::BITS" in assign:
        errors.append("GICv3 VGicD::assign_irq still imposes the legacy 8-pCPU limit")


def check_route_release_lifecycle(errors: list[str]) -> None:
    arch_ops_source = ARCH_OPS.read_text(encoding="utf-8")
    vm_source = AXVM_VM.read_text(encoding="utf-8")
    require(
        errors,
        arch_ops_source,
        ARCH_OPS,
        ("fn release_vm_devices(_devices: &axdevice::AxVmDevices) {}",),
    )
    require(
        errors,
        vm_source,
        AXVM_VM,
        (
            "fn release_devices(&mut self)",
            "crate::arch::release_vm_devices(&devices)",
            "resources.release_devices();",
        ),
    )
    if vm_source.count("release_devices();") < 2:
        errors.append("AxVM must release architecture device state during reset and destroy")


def check_gicr_last(errors: list[str]) -> None:
    source = VGICR.read_text(encoding="utf-8")
    require(
        errors,
        source,
        VGICR,
        (
            "is_last: bool",
            "pub fn new(addr: GuestPhysAddr, size: Option<usize>, cpu_id: usize, is_last: bool)",
            "fn virtualize_gicr_typer(value: usize, is_last: bool) -> usize",
            "value | GICR_TYPER_LAST",
            "value & !GICR_TYPER_LAST",
            "virtualize_gicr_typer(value, self.is_last)",
            "non_last_gicr_clears_physical_last_bit",
            "last_gicr_sets_last_without_changing_other_bits",
        ),
    )
    typer_read = section(source, "GICR_TYPER =>", "GICR_IIDR")
    if "if true" in typer_read:
        errors.append("VGicR still marks every redistributor as LAST")

    axdevice_source = AXDEVICE.read_text(encoding="utf-8")
    gppt_init = section(
        axdevice_source,
        "EmulatedDeviceType::GPPTRedistributor =>",
        "EmulatedDeviceType::GPPTDistributor =>",
    )
    if "i + 1 == cpu_num" not in gppt_init:
        errors.append("AxVmDevices does not identify only the final guest redistributor")


def main() -> int:
    errors: list[str] = []
    for path in (
        AXVM_AARCH64_VM,
        VCPU_PLACEMENTS,
        VGICD,
        VGICR,
        AXDEVICE,
        ARCH_OPS,
        AXVM_VM,
    ):
        if not path.is_file():
            errors.append(f"required source file is missing: {path.relative_to(WORKSPACE_ROOT)}")
    if errors:
        print("AArch64 passthrough SPI routing contract failed:")
        for error in errors:
            print(f"  - {error}")
        return 1

    check_axvm_route(errors)
    check_placement(errors)
    check_irouter(errors)
    check_route_release_lifecycle(errors)
    check_gicr_last(errors)
    if errors:
        print("AArch64 passthrough SPI routing contract failed:")
        for error in errors:
            print(f"  - {error}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
