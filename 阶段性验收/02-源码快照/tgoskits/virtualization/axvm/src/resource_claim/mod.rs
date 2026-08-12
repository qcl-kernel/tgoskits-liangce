//! Process-wide ownership of physical resources assigned to VMs.

use alloc::{collections::BTreeMap, format, vec::Vec};
use core::cmp::max;

use ax_kspin::SpinNoIrq as Mutex;

use crate::{
    AxVmError, AxVmResult, VMId,
    config::{AddressSpacePolicy, AxVMConfig, VMInterruptMode, VmMemMappingType},
    vm::{VM_ASPACE_BASE, VM_ASPACE_SIZE},
};

const PAGE_SIZE_4K: usize = 0x1000;
const PAGE_MASK_4K: usize = PAGE_SIZE_4K - 1;

static PHYSICAL_RESOURCE_REGISTRY: Mutex<PhysicalResourceRegistry> =
    Mutex::new(PhysicalResourceRegistry::new());

/// Keeps one VM's process-wide physical resource claims alive.
///
/// The lease is intentionally crate-private and non-cloneable. It is owned by
/// the VM object so construction failures and the final VM drop both release
/// the registry entry without a separate rollback path.
pub(crate) struct PhysicalResourceLease {
    claims: PhysicalResourceClaims,
}

impl PhysicalResourceLease {
    pub(crate) fn acquire(config: &AxVMConfig) -> AxVmResult<Self> {
        let claims = PhysicalResourceClaims::from_config(config)?;
        PHYSICAL_RESOURCE_REGISTRY.lock().insert(claims.clone())?;
        Ok(Self { claims })
    }

    pub(crate) fn validate(&self, config: &AxVMConfig) -> AxVmResult {
        let current = PhysicalResourceClaims::from_config(config)?;
        if current != self.claims {
            return Err(AxVmError::invalid_config(format!(
                "VM[{}] physical resources changed after AxVM::new",
                self.claims.vm_id
            )));
        }
        Ok(())
    }
}

impl Drop for PhysicalResourceLease {
    fn drop(&mut self) {
        PHYSICAL_RESOURCE_REGISTRY.lock().remove(self.claims.vm_id);
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PhysicalResourceClaims {
    vm_id: VMId,
    exclusive_address_space: bool,
    vcpu_placements: Vec<(usize, Option<usize>, usize)>,
    pcpu_usage_mask: usize,
    exclusive_pcpu_mask: usize,
    host_ranges: Vec<HostPhysicalRange>,
    host_ports: Vec<HostPortRange>,
    passthrough_irqs: Vec<u32>,
}

impl PhysicalResourceClaims {
    fn from_config(config: &AxVMConfig) -> AxVmResult<Self> {
        let exclusive_address_space =
            config.address_space_policy() == AddressSpacePolicy::Passthrough;
        let vcpu_placements = config.phys_cpu_ls.get_vcpu_affinities_pcpu_ids();
        validate_pcpu_placements(config, &vcpu_placements)?;
        let pcpu_usage_mask = pcpu_usage_mask(&vcpu_placements);
        let exclusive_pcpu_mask = exclusive_pcpu_mask(config, &vcpu_placements)?;
        let mut host_ranges = host_physical_ranges(config)?;
        merge_host_ranges(&mut host_ranges);
        let mut host_ports = host_port_ranges(config)?;
        merge_host_ports(&mut host_ports);

        let mut passthrough_irqs = if config.interrupt_mode() == VMInterruptMode::Passthrough {
            config.pass_through_irqs().to_vec()
        } else {
            Vec::new()
        };
        passthrough_irqs.sort_unstable();
        passthrough_irqs.dedup();

        Ok(Self {
            vm_id: config.id(),
            exclusive_address_space,
            vcpu_placements,
            pcpu_usage_mask,
            exclusive_pcpu_mask,
            host_ranges,
            host_ports,
            passthrough_irqs,
        })
    }
}

fn pcpu_usage_mask(vcpu_placements: &[(usize, Option<usize>, usize)]) -> usize {
    vcpu_placements
        .iter()
        .fold(0, |usage_mask, (_, requested_mask, _)| {
            usage_mask | requested_mask.unwrap_or(usize::MAX)
        })
}

fn validate_pcpu_placements(
    config: &AxVMConfig,
    vcpu_placements: &[(usize, Option<usize>, usize)],
) -> AxVmResult {
    let enabled_cpu_mask = crate::percpu::enabled_cpu_mask();
    for &(vcpu_id, phys_cpu_set, _) in vcpu_placements {
        let Some(mask) = phys_cpu_set else {
            continue;
        };
        if mask == 0 {
            return Err(AxVmError::invalid_config(format!(
                "VM[{}] vCPU[{vcpu_id}] has an empty physical CPU affinity",
                config.id()
            )));
        }
        if enabled_cpu_mask != 0 && mask & enabled_cpu_mask != mask {
            return Err(AxVmError::invalid_config(format!(
                "VM[{}] vCPU[{vcpu_id}] mask {mask:#x} includes a disabled CPU; enabled mask is \
                 {enabled_cpu_mask:#x}",
                config.id()
            )));
        }
    }
    Ok(())
}

fn exclusive_pcpu_mask(
    config: &AxVMConfig,
    vcpu_placements: &[(usize, Option<usize>, usize)],
) -> AxVmResult<usize> {
    if config.interrupt_mode() != VMInterruptMode::Passthrough {
        return Ok(0);
    }

    let mut exclusive_mask = 0;
    for &(vcpu_id, phys_cpu_set, _) in vcpu_placements {
        let mask = phys_cpu_set.ok_or_else(|| {
            AxVmError::invalid_config(format!(
                "VM[{}] passthrough vCPU[{vcpu_id}] has no physical CPU affinity",
                config.id()
            ))
        })?;
        exclusive_mask |= mask;
    }
    Ok(exclusive_mask)
}

fn host_physical_ranges(config: &AxVMConfig) -> AxVmResult<Vec<HostPhysicalRange>> {
    let mut ranges = Vec::new();

    if config.address_space_policy() == AddressSpacePolicy::Passthrough {
        ranges.push(HostPhysicalRange::page_expanded(
            "passthrough address-space policy",
            VM_ASPACE_BASE,
            VM_ASPACE_SIZE,
        )?);
    }

    for device in config.pass_through_devices() {
        ranges.push(HostPhysicalRange::from_linear_mapping(
            "passthrough device",
            device.base_gpa,
            device.base_hpa,
            device.length,
        )?);
    }

    for address in config.pass_through_addresses() {
        ranges.push(HostPhysicalRange::page_expanded(
            "passthrough address",
            address.base_gpa,
            address.length,
        )?);
    }

    for memory in config
        .memory_regions()
        .iter()
        .filter(|memory| memory.map_type == VmMemMappingType::MapReserved)
    {
        ranges.push(HostPhysicalRange::page_aligned(
            "reserved memory",
            memory.gpa,
            memory.size,
        )?);
    }

    Ok(ranges)
}

fn merge_host_ranges(ranges: &mut Vec<HostPhysicalRange>) {
    ranges.sort_unstable();
    let mut merged = Vec::<HostPhysicalRange>::with_capacity(ranges.len());
    for range in ranges.drain(..) {
        if let Some(previous) = merged.last_mut()
            && range.base <= previous.end
        {
            previous.end = max(previous.end, range.end);
            continue;
        }
        merged.push(range);
    }
    *ranges = merged;
}

fn host_port_ranges(config: &AxVMConfig) -> AxVmResult<Vec<HostPortRange>> {
    config
        .pass_through_ports()
        .iter()
        .map(|port| HostPortRange::new(port.base, port.length))
        .collect()
}

fn merge_host_ports(ranges: &mut Vec<HostPortRange>) {
    ranges.sort_unstable();
    let mut merged = Vec::<HostPortRange>::with_capacity(ranges.len());
    for range in ranges.drain(..) {
        if let Some(previous) = merged.last_mut()
            && range.base <= previous.end
        {
            previous.end = max(previous.end, range.end);
            continue;
        }
        merged.push(range);
    }
    *ranges = merged;
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct HostPhysicalRange {
    base: usize,
    end: usize,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct HostPortRange {
    base: u32,
    end: u32,
}

impl HostPortRange {
    fn new(base: u16, length: u16) -> AxVmResult<Self> {
        if length == 0 {
            return Err(AxVmError::invalid_config(format!(
                "host I/O port range at {base:#x} has zero length"
            )));
        }
        let end = u32::from(base) + u32::from(length);
        if end > u32::from(u16::MAX) + 1 {
            return Err(AxVmError::invalid_config(format!(
                "host I/O port range overflows: base={base:#x}, length={length:#x}"
            )));
        }
        Ok(Self {
            base: u32::from(base),
            end,
        })
    }

    const fn overlaps(self, other: Self) -> bool {
        self.base < other.end && other.base < self.end
    }
}

impl HostPhysicalRange {
    fn from_linear_mapping(
        name: &'static str,
        base_gpa: usize,
        base_hpa: usize,
        length: usize,
    ) -> AxVmResult<Self> {
        checked_end(name, "GPA", base_gpa, length)?;
        if base_gpa & PAGE_MASK_4K != base_hpa & PAGE_MASK_4K {
            return Err(AxVmError::invalid_config(format!(
                "{name} has different GPA/HPA page offsets: gpa={base_gpa:#x}, hpa={base_hpa:#x}"
            )));
        }
        Self::page_expanded(name, base_hpa, length)
    }

    fn page_expanded(name: &'static str, base: usize, length: usize) -> AxVmResult<Self> {
        let raw_end = checked_end(name, "HPA", base, length)?;
        let aligned_base = base & !PAGE_MASK_4K;
        let aligned_end = raw_end
            .checked_add(PAGE_MASK_4K)
            .map(|end| end & !PAGE_MASK_4K)
            .ok_or_else(|| {
                AxVmError::invalid_config(format!(
                    "{name} HPA range [{base:#x}, {raw_end:#x}) overflows during page alignment"
                ))
            })?;
        Ok(Self {
            base: aligned_base,
            end: aligned_end,
        })
    }

    fn page_aligned(name: &'static str, base: usize, length: usize) -> AxVmResult<Self> {
        let end = checked_end(name, "HPA", base, length)?;
        if base & PAGE_MASK_4K != 0 || length & PAGE_MASK_4K != 0 {
            return Err(AxVmError::invalid_config(format!(
                "{name} HPA range must be 4 KiB aligned: base={base:#x}, length={length:#x}"
            )));
        }
        Ok(Self { base, end })
    }

    const fn overlaps(self, other: Self) -> bool {
        self.base < other.end && other.base < self.end
    }
}

fn checked_end(
    name: &'static str,
    address_kind: &'static str,
    base: usize,
    length: usize,
) -> AxVmResult<usize> {
    if length == 0 {
        return Err(AxVmError::invalid_config(format!(
            "{name} {address_kind} range has zero length"
        )));
    }
    base.checked_add(length).ok_or_else(|| {
        AxVmError::invalid_config(format!(
            "{name} {address_kind} range overflows: base={base:#x}, length={length:#x}"
        ))
    })
}

struct PhysicalResourceRegistry {
    claims: BTreeMap<VMId, PhysicalResourceClaims>,
}

impl PhysicalResourceRegistry {
    const fn new() -> Self {
        Self {
            claims: BTreeMap::new(),
        }
    }

    fn insert(&mut self, requested: PhysicalResourceClaims) -> AxVmResult {
        if self.claims.contains_key(&requested.vm_id) {
            return Err(AxVmError::resource_conflict(
                "VM ID",
                format!(
                    "VM[{}] already owns a physical resource claim",
                    requested.vm_id
                ),
            ));
        }

        for existing in self.claims.values() {
            validate_no_conflict(&requested, existing)?;
        }
        self.claims.insert(requested.vm_id, requested);
        Ok(())
    }

    fn remove(&mut self, vm_id: VMId) -> Option<PhysicalResourceClaims> {
        self.claims.remove(&vm_id)
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.claims.len()
    }
}

fn validate_no_conflict(
    requested: &PhysicalResourceClaims,
    existing: &PhysicalResourceClaims,
) -> AxVmResult {
    if requested.exclusive_address_space || existing.exclusive_address_space {
        return Err(AxVmError::resource_conflict(
            "host address space",
            format!(
                "VM[{}] and VM[{}] cannot coexist because VM[{}] owns the passthrough identity \
                 address space",
                requested.vm_id,
                existing.vm_id,
                if requested.exclusive_address_space {
                    requested.vm_id
                } else {
                    existing.vm_id
                }
            ),
        ));
    }

    let overlapping_cpus = (requested.exclusive_pcpu_mask & existing.pcpu_usage_mask)
        | (existing.exclusive_pcpu_mask & requested.pcpu_usage_mask);
    if overlapping_cpus != 0 {
        return Err(AxVmError::resource_conflict(
            "physical CPU",
            format!(
                "VM[{}] exclusive/usage masks {:#x}/{:#x} overlap VM[{}] exclusive/usage masks \
                 {:#x}/{:#x} at {overlapping_cpus:#x}",
                requested.vm_id,
                requested.exclusive_pcpu_mask,
                requested.pcpu_usage_mask,
                existing.vm_id,
                existing.exclusive_pcpu_mask,
                existing.pcpu_usage_mask
            ),
        ));
    }

    for requested_range in &requested.host_ranges {
        if let Some(existing_range) = existing
            .host_ranges
            .iter()
            .find(|existing_range| requested_range.overlaps(**existing_range))
        {
            return Err(AxVmError::resource_conflict(
                "host physical address",
                format!(
                    "VM[{}] range [{:#x}, {:#x}) overlaps VM[{}] range [{:#x}, {:#x})",
                    requested.vm_id,
                    requested_range.base,
                    requested_range.end,
                    existing.vm_id,
                    existing_range.base,
                    existing_range.end
                ),
            ));
        }
    }

    for requested_range in &requested.host_ports {
        if let Some(existing_range) = existing
            .host_ports
            .iter()
            .find(|existing_range| requested_range.overlaps(**existing_range))
        {
            return Err(AxVmError::resource_conflict(
                "host I/O port",
                format!(
                    "VM[{}] range [{:#x}, {:#x}) overlaps VM[{}] range [{:#x}, {:#x})",
                    requested.vm_id,
                    requested_range.base,
                    requested_range.end,
                    existing.vm_id,
                    existing_range.base,
                    existing_range.end
                ),
            ));
        }
    }

    if let Some(irq) = requested
        .passthrough_irqs
        .iter()
        .find(|irq| existing.passthrough_irqs.contains(irq))
    {
        return Err(AxVmError::resource_conflict(
            "passthrough IRQ",
            format!(
                "VM[{}] and VM[{}] both claim physical IRQ {irq}",
                requested.vm_id, existing.vm_id
            ),
        ));
    }

    Ok(())
}

#[cfg(test)]
mod tests;
