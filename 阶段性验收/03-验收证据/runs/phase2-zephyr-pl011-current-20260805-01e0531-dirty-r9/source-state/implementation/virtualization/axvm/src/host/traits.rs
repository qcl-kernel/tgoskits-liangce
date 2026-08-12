//! Internal host capability traits used by the AxVM runtime.

use core::time::Duration;

use axvm_types::{HostPhysAddr, HostVirtAddr};

use crate::AxVmResult;

/// Host memory allocation and address translation.
pub trait HostMemory {
    /// Allocate one 4 KiB host frame.
    fn alloc_frame(&self) -> Option<HostPhysAddr>;

    /// Free one frame returned by [`HostMemory::alloc_frame`].
    fn dealloc_frame(&self, paddr: HostPhysAddr);

    /// Allocate contiguous host frames.
    fn alloc_contiguous_frames(
        &self,
        num_frames: usize,
        frame_align: usize,
    ) -> Option<HostPhysAddr>;

    /// Free contiguous host frames.
    fn dealloc_contiguous_frames(&self, paddr: HostPhysAddr, num_frames: usize);

    /// Returns whether the immutable platform manifest assigns this exact
    /// host-physical carveout to `vm_id`.
    ///
    /// Implementations must not infer ownership from generic reserved-memory
    /// ranges. The VM identifier, start address, and size must all match one
    /// boot-validated manifest entry.
    fn owns_vm_carveout(&self, vm_id: usize, paddr: HostPhysAddr, size: usize) -> bool;

    /// Convert a host physical address to a host virtual address.
    fn phys_to_virt(&self, paddr: HostPhysAddr) -> HostVirtAddr;

    /// Convert a host virtual address to a host physical address.
    fn virt_to_phys(&self, vaddr: HostVirtAddr) -> HostPhysAddr;
}

/// Host time and timer operations.
pub trait HostTime {
    /// Read monotonic host time.
    fn monotonic_time(&self) -> Duration;

    /// Program the host one-shot timer.
    fn set_oneshot_timer(&self, deadline_ns: u64);
}

/// Host CPU topology and affinity operations.
pub trait HostCpu {
    /// CPU affinity mask type.
    type CpuMask: Send + Sync + 'static;

    /// Number of usable host CPUs.
    fn cpu_count(&self) -> usize;

    /// Current host CPU ID.
    fn this_cpu_id(&self) -> usize;
}

/// Host platform lifecycle and virtualization controls.
pub trait HostPlatform {
    /// Check whether hardware virtualization is available.
    fn has_hardware_support(&self) -> bool;

    /// Enable virtualization on the current host CPU.
    fn enable_virtualization_on_current_cpu(&self) -> AxVmResult;

    /// Enable virtualization on every usable host CPU.
    fn enable_virtualization_on_all_cpus(&self) -> AxVmResult;
}

pub(crate) fn exact_vm_carveout_match(
    requested_vm_id: usize,
    requested_start: HostPhysAddr,
    requested_size: usize,
    manifest: impl IntoIterator<Item = (u32, usize, usize)>,
) -> bool {
    let Ok(requested_vm_id) = u32::try_from(requested_vm_id) else {
        return false;
    };
    if requested_size == 0 {
        return false;
    }

    manifest.into_iter().any(|(vm_id, start, size)| {
        vm_id == requested_vm_id && start == requested_start.as_usize() && size == requested_size
    })
}

#[cfg(test)]
mod tests {
    use super::exact_vm_carveout_match;

    const OWNER_VM: usize = 2;
    const START: usize = 0x1_0000_0000;
    const SIZE: usize = 0x0800_0000;

    fn manifest() -> impl Iterator<Item = (u32, usize, usize)> {
        [(OWNER_VM as u32, START, SIZE)].into_iter()
    }

    #[test]
    fn exact_vm_carveout_match_accepts_only_the_full_owner_tuple() {
        assert!(exact_vm_carveout_match(
            OWNER_VM,
            START.into(),
            SIZE,
            manifest()
        ));
        assert!(!exact_vm_carveout_match(
            OWNER_VM + 1,
            START.into(),
            SIZE,
            manifest()
        ));
        assert!(!exact_vm_carveout_match(
            OWNER_VM,
            (START + 0x20_0000).into(),
            SIZE,
            manifest()
        ));
        assert!(!exact_vm_carveout_match(
            OWNER_VM,
            (START + 0x20_0000).into(),
            SIZE - 0x20_0000,
            manifest()
        ));
        assert!(!exact_vm_carveout_match(
            OWNER_VM,
            START.into(),
            SIZE - 0x20_0000,
            manifest()
        ));
        assert!(!exact_vm_carveout_match(
            OWNER_VM,
            START.into(),
            SIZE + 0x20_0000,
            manifest()
        ));
    }

    #[test]
    fn exact_vm_carveout_match_fails_closed_for_missing_or_unrepresentable_owner() {
        assert!(!exact_vm_carveout_match(
            OWNER_VM,
            START.into(),
            SIZE,
            core::iter::empty()
        ));
        if usize::BITS > u32::BITS {
            let unrepresentable_owner = (u64::from(u32::MAX) + 1) as usize;
            assert!(!exact_vm_carveout_match(
                unrepresentable_owner,
                START.into(),
                SIZE,
                manifest()
            ));
        }
        assert!(!exact_vm_carveout_match(
            OWNER_VM,
            START.into(),
            0,
            [(OWNER_VM as u32, START, 0)]
        ));
    }
}
