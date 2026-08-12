//! AArch64 VM resource creation and initialization.

use alloc::sync::Arc;

use arm_vcpu::{ArmVcpuCreateConfig, ArmVcpuSetupConfig};
use axdevice_base::DeviceRegistry as _;
use axvm_types::{NestedPagingConfig, VMInterruptMode, VmArchVcpuOps};

use super::{Aarch64Arch, npt};
use crate::{
    AxVmError, AxVmResult, ax_err,
    config::AxVMConfig,
    vm::{
        AxVM, AxVMResources,
        prepare::{
            PreparedVm, VmInitRequest,
            address_space::{guest_owned_regions, map_guest_address_space},
            complete_vm_init, default_device_factories,
            devices::PreparedDevices,
            validate_guest_dtb,
            vcpus::{PreparedVcpus, VcpuPlacement, vcpu_placements},
        },
    },
};

impl Aarch64Arch {
    pub(crate) fn create_vm_resources(config: AxVMConfig) -> AxVmResult<AxVMResources> {
        let placements = config.phys_cpu_ls.get_vcpu_affinities_pcpu_ids();
        let levels = guest_page_table_levels(&placements)?;
        let page_table = npt::NestedPageTable::new(levels)?;
        AxVMResources::from_page_table(config, page_table, |root_paddr| {
            nested_paging_config(root_paddr, levels, &placements)
        })
    }

    pub(crate) fn init_vm(vm: &AxVM, request: VmInitRequest<'_>) -> AxVmResult {
        match request {
            VmInitRequest::Default => {
                let factories = default_device_factories()?;
                let interrupt_fabric = crate::InterruptFabric::new(vm.interrupt_mode());
                init_vm_with(vm, &factories, interrupt_fabric)
            }
            VmInitRequest::Provided {
                factories,
                interrupt_fabric,
            } => init_vm_with(vm, factories, interrupt_fabric),
        }
    }
}

fn init_vm_with(
    vm: &AxVM,
    factories: &axdevice::DeviceFactoryRegistry,
    interrupt_fabric: crate::InterruptFabric,
) -> AxVmResult {
    complete_vm_init(vm, interrupt_fabric, |resources, interrupt_fabric| {
        let placements = vcpu_placements(resources);
        let dtb_addr = resources
            .config()
            .image_config()
            .dtb_load_gpa
            .unwrap_or_default();
        let vcpus = PreparedVcpus::create(vm.id(), &placements, |placement| {
            Ok(ArmVcpuCreateConfig {
                mpidr_el1: placement.phys_cpu_id as _,
                dtb_addr: dtb_addr.as_usize(),
            })
        })?;
        let mut devices = PreparedDevices::build_common(resources, factories, interrupt_fabric)?;
        register_arch_devices(resources.config(), &placements, &mut devices.devices)?;
        devices.register_special_devices(vm)?;
        validate_guest_dtb(resources)?;

        let owned_regions = guest_owned_regions(resources);
        map_guest_address_space(vm, resources, devices.devices(), &owned_regions)?;
        vcpus.setup(resources, build_vcpu_setup_config)?;

        Ok(PreparedVm::new(vcpus, devices))
    })
}

fn build_vcpu_setup_config(
    config: &AxVMConfig,
    _memory_regions: &[crate::vm::VMMemoryRegion],
) -> AxVmResult<<super::AxvmArmVcpu as VmArchVcpuOps>::SetupConfig> {
    let passthrough = config.interrupt_mode() == VMInterruptMode::Passthrough;
    Ok(ArmVcpuSetupConfig {
        passthrough_interrupt: passthrough,
        passthrough_timer: passthrough,
    })
}

fn register_arch_devices(
    config: &AxVMConfig,
    placements: &[VcpuPlacement],
    devices: &mut axdevice::AxVmDevices,
) -> AxVmResult {
    if config.interrupt_mode() == VMInterruptMode::Passthrough {
        assign_passthrough_spis(config, placements, devices)?;
    } else {
        register_virtual_timers(devices)?;
    }
    Ok(())
}

fn assign_passthrough_spis(
    config: &AxVMConfig,
    placements: &[VcpuPlacement],
    devices: &axdevice::AxVmDevices,
) -> AxVmResult {
    if config.pass_through_spis().is_empty() {
        return Ok(());
    }
    let placement = placements
        .iter()
        .copied()
        .find(|placement| placement.id == 0)
        .ok_or_else(|| AxVmError::invalid_config("passthrough VM has no vCPU0 placement"))?;
    let cpu_id = placement
        .single_pcpu_id(crate::percpu::enabled_cpu_mask())
        .ok_or_else(|| {
            AxVmError::invalid_config(format_args!(
                "passthrough VM vCPU0 must be pinned to one enabled pCPU, got {:?}",
                placement.phys_cpu_set
            ))
        })?;
    let affinity = mpidr_to_affinity(placement.phys_cpu_id);
    let gicd = devices
        .devices()
        .find_map(|device| device.as_any().downcast_ref::<arm_vgic::v3::vgicd::VGicD>())
        .ok_or_else(|| {
            AxVmError::resource_unavailable(
                "AArch64 GIC distributor",
                "passthrough SPIs were configured but no VGicD device was prepared",
            )
        })?;

    for spi in config.pass_through_spis() {
        let intid = spi.checked_add(32).ok_or_else(|| {
            AxVmError::invalid_config(format_args!(
                "passthrough SPI offset {spi} overflows its GIC INTID"
            ))
        })?;
        debug!("Assigning passthrough SPI INTID {intid} to pCPU {cpu_id}");
        gicd.assign_irq(intid, affinity)
            .map_err(|error| AxVmError::interrupt("assign passthrough SPI", error))?;
    }
    Ok(())
}

pub(super) fn release_vm_devices(devices: &axdevice::AxVmDevices) {
    for device in devices.devices() {
        if let Some(gicd) = device.as_any().downcast_ref::<arm_vgic::v3::vgicd::VGicD>() {
            gicd.release_assigned_irqs();
        }
    }
}

fn mpidr_to_affinity(mpidr: usize) -> (u8, u8, u8, u8) {
    (
        ((mpidr >> 32) & 0xff) as u8,
        ((mpidr >> 16) & 0xff) as u8,
        ((mpidr >> 8) & 0xff) as u8,
        (mpidr & 0xff) as u8,
    )
}

fn register_virtual_timers(devices: &mut axdevice::AxVmDevices) -> AxVmResult {
    for device in axdevice::create_vtimer_devices() {
        devices.register(Arc::from(device) as Arc<dyn axdevice_base::Device>)?;
    }
    Ok(())
}

fn guest_page_table_levels(vcpu_mappings: &[(usize, Option<usize>, usize)]) -> AxVmResult<usize> {
    let mut selected = usize::MAX;
    for cpu_id in crate::architecture::ops::target_phys_cpu_ids(vcpu_mappings) {
        let levels = crate::percpu::cpu_max_guest_page_table_levels(cpu_id)
            .unwrap_or_else(arm_vcpu::max_guest_page_table_levels);
        if levels == 0 {
            return ax_err!(
                Unsupported,
                "AArch64 nested paging is not enabled on target CPU"
            );
        }
        selected = selected.min(levels);
    }
    if selected == usize::MAX {
        selected = arm_vcpu::max_guest_page_table_levels();
    }
    match selected {
        3 | 4 => Ok(selected),
        _ => ax_err!(Unsupported, "unsupported AArch64 stage-2 page-table levels"),
    }
}

fn nested_paging_config(
    root_paddr: ax_memory_addr::PhysAddr,
    levels: usize,
    vcpu_mappings: &[(usize, Option<usize>, usize)],
) -> AxVmResult<NestedPagingConfig> {
    let mut pa_bits = usize::MAX;
    for cpu_id in crate::architecture::ops::target_phys_cpu_ids(vcpu_mappings) {
        let bits =
            crate::percpu::cpu_guest_phys_addr_bits(cpu_id).unwrap_or_else(arm_vcpu::pa_bits);
        pa_bits = pa_bits.min(bits);
    }
    if pa_bits == usize::MAX {
        pa_bits = arm_vcpu::pa_bits();
    }

    let gpa_bits = match levels {
        3 => 39,
        4 => 48,
        _ => return ax_err!(InvalidInput, "unsupported AArch64 stage-2 levels"),
    };
    Ok(NestedPagingConfig::new(
        root_paddr, levels, gpa_bits, pa_bits,
    ))
}

#[cfg(test)]
mod tests {
    use super::mpidr_to_affinity;

    #[test]
    fn mpidr_to_affinity_extracts_all_levels() {
        assert_eq!(mpidr_to_affinity(0x12_0034_5678), (0x12, 0x34, 0x56, 0x78));
    }
}
