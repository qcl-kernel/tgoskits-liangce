//! Architecture-neutral vCPU collection construction and setup.

use alloc::{boxed::Box, sync::Arc, vec::Vec};

use axvm_types::VmArchVcpuOps;

use super::super::{AxVCpuRef, AxVMResources, VCpu};
use crate::AxVmResult;

#[derive(Clone, Copy, Debug)]
pub(crate) struct VcpuPlacement {
    pub(crate) id: usize,
    pub(crate) phys_cpu_set: Option<usize>,
    pub(crate) phys_cpu_id: usize,
}

impl VcpuPlacement {
    /// Returns the single logical pCPU selected by this placement.
    ///
    /// Passthrough interrupt routing must target the same fixed pCPU as the
    /// vCPU task.  A missing or multi-bit affinity is therefore not a valid
    /// source for a GICD ITARGETSR/IROUTER route.  When AxVM has already
    /// recorded its enabled CPUs, reject a configured CPU outside that set so
    /// the runtime cannot silently fall back to a different target later.
    #[allow(dead_code, reason = "used by the AArch64 passthrough IRQ backend")]
    pub(crate) fn single_pcpu_id(self, enabled_cpu_mask: usize) -> Option<usize> {
        let mask = self.phys_cpu_set?;
        if !mask.is_power_of_two() || (enabled_cpu_mask != 0 && mask & enabled_cpu_mask != mask) {
            return None;
        }
        Some(mask.trailing_zeros() as usize)
    }
}

pub(crate) struct PreparedVcpus {
    vcpus: Vec<AxVCpuRef>,
}

impl PreparedVcpus {
    pub(crate) fn create(
        vm_id: usize,
        placements: &[VcpuPlacement],
        mut build_config: impl FnMut(
            VcpuPlacement,
        ) -> AxVmResult<
            <crate::arch::ArchVCpu as VmArchVcpuOps>::CreateConfig,
        >,
    ) -> AxVmResult<Self> {
        debug!("id: {vm_id}, vCPU placements: {placements:#x?}");

        let mut vcpus = Vec::with_capacity(placements.len());
        for placement in placements.iter().copied() {
            trace!(
                "Creating VM[{vm_id}] vCPU[{}] for physical CPU {}",
                placement.id, placement.phys_cpu_id
            );
            let arch_config = build_config(placement)?;

            // FIXME: VCpu is neither `Send` nor `Sync` by design, check whether
            // 1. we should make it `Send` and `Sync`, or
            // 2. we can guarantee that no cross-thread access is performed
            #[allow(clippy::arc_with_non_send_sync)]
            vcpus.push(Arc::new(VCpu::new(
                vm_id,
                placement.id,
                placement.phys_cpu_set,
                arch_config,
            )?));
        }

        Ok(Self { vcpus })
    }

    pub(crate) fn setup(
        &self,
        resources: &AxVMResources,
        mut build_config: impl FnMut(
            &crate::config::AxVMConfig,
            &[crate::vm::VMMemoryRegion],
        ) -> AxVmResult<
            <crate::arch::ArchVCpu as VmArchVcpuOps>::SetupConfig,
        >,
    ) -> AxVmResult {
        for vcpu in &self.vcpus {
            let setup_config = build_config(&resources.config, &resources.memory_regions)?;
            let entry = if vcpu.id() == 0 {
                resources.config.bsp_entry()
            } else {
                resources.config.ap_entry()
            };

            debug!("Setting up vCPU[{}] entry at {:#x}", vcpu.id(), entry);
            vcpu.setup(entry, resources.nested_paging, setup_config)?;
        }
        Ok(())
    }

    pub(crate) fn into_boxed_slice(self) -> Box<[AxVCpuRef]> {
        self.vcpus.into_boxed_slice()
    }
}

pub(crate) fn vcpu_placements(resources: &AxVMResources) -> Vec<VcpuPlacement> {
    resources
        .config
        .phys_cpu_ls
        .get_vcpu_affinities_pcpu_ids()
        .into_iter()
        .map(|(id, phys_cpu_set, phys_cpu_id)| VcpuPlacement {
            id,
            phys_cpu_set,
            phys_cpu_id,
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::VcpuPlacement;

    fn placement(phys_cpu_set: Option<usize>) -> VcpuPlacement {
        VcpuPlacement {
            id: 0,
            phys_cpu_set,
            phys_cpu_id: 0,
        }
    }

    #[test]
    fn single_pcpu_id_accepts_one_hot_mask() {
        assert_eq!(placement(Some(0b1000)).single_pcpu_id(0), Some(3));
        assert_eq!(placement(Some(0b1000)).single_pcpu_id(0b1111), Some(3));
    }

    #[test]
    fn single_pcpu_id_rejects_missing_zero_or_multi_bit_masks() {
        assert_eq!(placement(None).single_pcpu_id(0), None);
        assert_eq!(placement(Some(0)).single_pcpu_id(0), None);
        assert_eq!(placement(Some(0b1010)).single_pcpu_id(0), None);
    }

    #[test]
    fn single_pcpu_id_rejects_disabled_cpu() {
        assert_eq!(placement(Some(0b1000)).single_pcpu_id(0b0011), None);
    }
}
