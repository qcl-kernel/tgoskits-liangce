//! Feature-gated, post-vCPU-run stage-2 HPA observations.
//!
//! This is deliberately a narrow host-side observation.  It records one
//! configured guest-memory page only after the architecture backend returns a
//! successful VM exit and before AxVM dispatches that exit; it is not a claim
//! that the exit was handled successfully, nor a guest-boot, DMA, allocator,
//! or inter-VM connectivity assertion.

use alloc::{format, string::String};

use ax_memory_addr::PAGE_SIZE_4K;
#[allow(
    unused_imports,
    reason = "some architecture page tables expose query as an inherent method"
)]
use axaddrspace::NestedPageTableOps;

use super::{AxVM, AxVMResources, VMMemoryRegion};
use crate::{
    AxVmError, AxVmResult, GuestPhysAddr, HostPhysAddr, ax_err_type,
    config::{VmMemConfig, VmMemMappingType},
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum EvidenceMemoryKind {
    Alloc,
    Identical,
    Reserved,
}

impl EvidenceMemoryKind {
    const fn from_mapping_type(mapping_type: &VmMemMappingType) -> Self {
        match mapping_type {
            VmMemMappingType::MapAlloc => Self::Alloc,
            VmMemMappingType::MapIdentical => Self::Identical,
            VmMemMappingType::MapReserved => Self::Reserved,
        }
    }

    const fn marker_value(self) -> &'static str {
        match self {
            Self::Alloc => "alloc",
            Self::Identical => "identical",
            Self::Reserved => "reserved",
        }
    }
}

/// One post-run observation of a configured memory page in the stage-2 table.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Stage2HpaPostRunDescriptor {
    vm_id: usize,
    vcpu_id: usize,
    kind: EvidenceMemoryKind,
    gpa: GuestPhysAddr,
    hpa: HostPhysAddr,
    identity: bool,
}

impl Stage2HpaPostRunDescriptor {
    fn marker(self) -> String {
        format!(
            "AXVISOR_STAGE2_HPA_POSTRUN vm={} vcpu={} kind={} gpa={:#x} hpa={:#x} page_size={} \
             identity={}",
            self.vm_id,
            self.vcpu_id,
            self.kind.marker_value(),
            self.gpa.as_usize(),
            self.hpa.as_usize(),
            PAGE_SIZE_4K,
            if self.identity { 1 } else { 0 },
        )
    }
}

fn selected_configured_memory_region(
    resources: &AxVMResources,
) -> AxVmResult<(&VmMemConfig, &VMMemoryRegion)> {
    let mut first = None;
    for (index, config) in resources.config.memory_regions().iter().enumerate() {
        let region = resources.memory_regions.get(index).ok_or_else(|| {
            ax_err_type!(
                BadState,
                format!("configured memory region {index} is not prepared")
            )
        })?;
        if region.size() < PAGE_SIZE_4K {
            return Err(ax_err_type!(
                InvalidData,
                format!("configured memory region {index} is smaller than one page")
            ));
        }
        if region.size() != config.size {
            return Err(ax_err_type!(
                InvalidData,
                format!(
                    "configured memory region {index} size mismatch: config={:#x}, prepared={:#x}",
                    config.size,
                    region.size()
                )
            ));
        }
        if !matches!(&config.map_type, VmMemMappingType::MapIdentical)
            && region.gpa.as_usize() != config.gpa
        {
            return Err(ax_err_type!(
                InvalidData,
                format!(
                    "configured memory region {index} GPA mismatch: config={:#x}, prepared={:#x}",
                    config.gpa,
                    region.gpa.as_usize()
                )
            ));
        }
        if matches!(&config.map_type, VmMemMappingType::MapReserved) {
            return Ok((config, region));
        }
        if first.is_none() {
            first = Some((config, region));
        }
    }
    first.ok_or_else(|| ax_err_type!(BadState, "VM has no configured memory region to observe"))
}

/// Builds a marker after a successful backend VM exit and before its dispatch.
pub(crate) fn postrun_descriptor(
    vm: &AxVM,
    vcpu_id: usize,
) -> AxVmResult<Stage2HpaPostRunDescriptor> {
    if !vm.running() {
        return Err(ax_err_type!(
            BadState,
            format!(
                "VM[{}] is not running for a post-run stage-2 observation",
                vm.id()
            )
        ));
    }

    fn map_query_error<E>(error: E) -> AxVmError
    where
        E: Into<axaddrspace::AddrSpaceError>,
    {
        AxVmError::from_addrspace("query post-run stage-2 HPA", error.into())
    }

    vm.with_resources(|resources| {
        let (config, region) = selected_configured_memory_region(resources)?;
        let hpa = resources
            .address_space
            .page_table()
            .query(region.gpa)
            .map(|(hpa, ..)| hpa)
            .map_err(map_query_error)?;
        let expected_hpa = region.host_paddr();
        if hpa != expected_hpa {
            return Err(ax_err_type!(
                InvalidData,
                format!(
                    "stage-2 HPA differs from configured memory backing: query={:#x}, \
                     expected={:#x}",
                    hpa.as_usize(),
                    expected_hpa.as_usize()
                )
            ));
        }

        let kind = EvidenceMemoryKind::from_mapping_type(&config.map_type);
        let identity = region.gpa.as_usize() == hpa.as_usize();
        if matches!(
            kind,
            EvidenceMemoryKind::Identical | EvidenceMemoryKind::Reserved
        ) && !identity
        {
            return Err(ax_err_type!(
                InvalidData,
                format!(
                    "{kind:?} memory is not identity mapped: GPA={:#x}, HPA={:#x}",
                    region.gpa.as_usize(),
                    hpa.as_usize()
                )
            ));
        }

        Ok(Stage2HpaPostRunDescriptor {
            vm_id: vm.id(),
            vcpu_id,
            kind,
            gpa: region.gpa,
            hpa,
            identity,
        })
    })
}

/// Builds a strict, no-payload marker for one observed stage-2 mapping.
pub(crate) fn postrun_marker(vm: &AxVM, vcpu_id: usize) -> AxVmResult<String> {
    Ok(postrun_descriptor(vm, vcpu_id)?.marker())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn marker_format_is_strict_and_contains_only_the_narrow_observation() {
        let descriptor = Stage2HpaPostRunDescriptor {
            vm_id: 2,
            vcpu_id: 0,
            kind: EvidenceMemoryKind::Reserved,
            gpa: GuestPhysAddr::from(0x1_0000_0000),
            hpa: HostPhysAddr::from(0x1_0000_0000),
            identity: true,
        };
        assert_eq!(
            descriptor.marker(),
            "AXVISOR_STAGE2_HPA_POSTRUN vm=2 vcpu=0 kind=reserved gpa=0x100000000 hpa=0x100000000 \
             page_size=4096 identity=1"
        );
    }

    #[test]
    fn mapping_kind_names_do_not_upgrade_alloc_or_identical_to_reserved() {
        assert_eq!(
            EvidenceMemoryKind::from_mapping_type(&VmMemMappingType::MapAlloc).marker_value(),
            "alloc"
        );
        assert_eq!(
            EvidenceMemoryKind::from_mapping_type(&VmMemMappingType::MapIdentical).marker_value(),
            "identical"
        );
        assert_eq!(
            EvidenceMemoryKind::from_mapping_type(&VmMemMappingType::MapReserved).marker_value(),
            "reserved"
        );
    }
}
