use core::ops::Range;

use heapless::Vec;

use crate::{
    consts::PAGE_SIZE,
    fdt::fdt_base,
    mem::{MemoryDescriptor, MemoryType, add_memory_descriptor},
};

pub fn init_memory_map() -> Option<()> {
    let fdt = super::fdt_base()?;
    let dma_guards = super::dma_guard::parse_dma_guards_or_panic(fdt.clone());
    let carveouts = super::vm_carveout::parse_vm_carveouts_or_panic(fdt.clone());

    for memory in fdt.memory() {
        for region in memory.regions() {
            let Some(region) = normalize_region(region.address, region.size) else {
                continue;
            };

            add_memory_descriptor(MemoryDescriptor {
                physical_start: region.start,
                size_in_bytes: region.end - region.start,
                memory_type: MemoryType::Free,
            })
            .unwrap();
        }
    }

    for reserved in fdt.memory_reservations() {
        let Some(region) = normalize_region(reserved.address, reserved.size) else {
            continue;
        };
        add_memory_descriptor(MemoryDescriptor::new_aligned(
            region.start,
            region.end - region.start,
            MemoryType::Reserved,
            PAGE_SIZE,
        ))
        .unwrap();
    }

    for reserved in fdt.reserved_memory() {
        if super::vm_carveout::is_vm_carveout_node(&reserved)
            || super::dma_guard::is_dma_guard_node(&reserved)
        {
            continue;
        }
        if let Some(mut itr) = reserved.reg()
            && let Some(reg) = itr.next()
            && let Some(size) = reg.size
            && let Some(region) = normalize_region(reg.address, size)
        {
            add_memory_descriptor(MemoryDescriptor {
                physical_start: region.start,
                size_in_bytes: region.end - region.start,
                memory_type: MemoryType::Reserved,
            })
            .unwrap();
        }
    }

    for &guard in &dma_guards {
        add_memory_descriptor(guard.memory_descriptor()).unwrap_or_else(|error| {
            panic!("failed to reserve AxVisor host DMA guard {guard:?}: {error:?}")
        });
        println!(
            "AXVISOR_HOST_DMA_GUARD_RESERVED hpa={:#x} size={:#x} phase=before-ram-init",
            guard.physical_start, guard.size,
        );
    }
    super::dma_guard::publish_dma_guards(dma_guards);

    for &carveout in &carveouts {
        add_memory_descriptor(carveout.memory_descriptor()).unwrap_or_else(|error| {
            panic!("failed to reserve AxVisor VM carveout {carveout:?}: {error:?}")
        });
        println!(
            "AXVISOR_HOST_VM_CARVEOUT_RESERVED vm={} hpa={:#x} size={:#x} phase=before-ram-init",
            carveout.vm_id, carveout.physical_start, carveout.size,
        );
    }
    super::vm_carveout::publish_vm_carveouts(carveouts);

    Some(())
}

pub fn memories() -> impl Iterator<Item = Range<usize>> {
    let mut res = Vec::<_, 128>::new();
    if let Some(fdt) = fdt_base() {
        for memory in fdt.memory() {
            for region in memory.regions() {
                if let Some(region) = normalize_region(region.address, region.size) {
                    res.push(region).ok();
                }
            }
        }
    }
    res.into_iter()
}

fn normalize_region(address: u64, size: u64) -> Option<Range<usize>> {
    if size == 0 {
        return None;
    }

    let start = normalize_fdt_address(address as usize);
    let size = size as usize;
    let end = start.checked_add(size)?;
    Some(start..end)
}

fn normalize_fdt_address(address: usize) -> usize {
    <crate::arch::Arch as crate::ArchTrait>::canonicalize_paddr(address)
}
