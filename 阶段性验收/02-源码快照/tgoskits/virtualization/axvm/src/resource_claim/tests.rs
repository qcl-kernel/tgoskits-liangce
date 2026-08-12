use alloc::{format, string::String, vec, vec::Vec};

use super::*;
use crate::config::{
    AxVMConfigParams, PassThroughAddressConfig, PassThroughDeviceConfig, PassThroughPortConfig,
    PhysCpuList, VmMemConfig,
};

struct TestConfigBuilder {
    id: VMId,
    mode: VMInterruptMode,
    cpu_masks: Vec<usize>,
    devices: Vec<PassThroughDeviceConfig>,
    addresses: Vec<PassThroughAddressConfig>,
    ports: Vec<PassThroughPortConfig>,
    memory: Vec<VmMemConfig>,
    policy: AddressSpacePolicy,
    irqs: Vec<u32>,
}

impl TestConfigBuilder {
    fn new(id: VMId) -> Self {
        Self {
            id,
            mode: VMInterruptMode::NoIrq,
            cpu_masks: Vec::new(),
            devices: Vec::new(),
            addresses: Vec::new(),
            ports: Vec::new(),
            memory: Vec::new(),
            policy: AddressSpacePolicy::Virtualized,
            irqs: Vec::new(),
        }
    }

    fn passthrough_cpu(mut self, mask: usize) -> Self {
        self.mode = VMInterruptMode::Passthrough;
        self.cpu_masks.push(mask);
        self
    }

    fn emulated_cpu(mut self, mask: usize) -> Self {
        self.mode = VMInterruptMode::Emulated;
        self.cpu_masks.push(mask);
        self
    }

    fn device(mut self, base_gpa: usize, base_hpa: usize, length: usize) -> Self {
        self.devices.push(PassThroughDeviceConfig {
            name: String::from("test-device"),
            base_gpa,
            base_hpa,
            length,
            irq_id: 0,
        });
        self
    }

    fn address(mut self, base_gpa: usize, length: usize) -> Self {
        self.addresses
            .push(PassThroughAddressConfig { base_gpa, length });
        self
    }

    fn port(mut self, base: u16, length: u16) -> Self {
        self.ports.push(PassThroughPortConfig { base, length });
        self
    }

    fn memory(mut self, gpa: usize, size: usize, map_type: VmMemMappingType) -> Self {
        self.memory.push(VmMemConfig {
            gpa,
            size,
            flags: 0x7,
            map_type,
        });
        self
    }

    fn passthrough_policy(mut self) -> Self {
        self.policy = AddressSpacePolicy::Passthrough;
        self
    }

    fn irq(mut self, irq: u32) -> Self {
        self.irqs.push(irq);
        self
    }

    fn build(self) -> AxVMConfig {
        let cpu_num = self.cpu_masks.len().max(1);
        let cpu_sets = (!self.cpu_masks.is_empty()).then_some(self.cpu_masks);
        let mut config = AxVMConfig::new(AxVMConfigParams {
            id: self.id,
            name: format!("vm-{}", self.id),
            phys_cpu_ls: PhysCpuList::new(cpu_num, None, cpu_sets),
            pass_through_devices: self.devices,
            pass_through_addresses: self.addresses,
            pass_through_ports: self.ports,
            address_space_policy: self.policy,
            memory_regions: self.memory,
            interrupt_mode: self.mode,
            ..Default::default()
        });
        for irq in self.irqs {
            config.add_pass_through_irq(irq);
        }
        config
    }
}

#[test]
fn rejects_duplicate_vm_id_without_mutating_registry() {
    let mut registry = PhysicalResourceRegistry::new();
    let first = PhysicalResourceClaims::from_config(&TestConfigBuilder::new(1).build()).unwrap();
    let duplicate =
        PhysicalResourceClaims::from_config(&TestConfigBuilder::new(1).build()).unwrap();

    registry.insert(first).unwrap();
    let error = registry.insert(duplicate).unwrap_err();

    assert!(matches!(
        error,
        AxVmError::ResourceConflict {
            resource: "VM ID",
            ..
        }
    ));
    assert_eq!(registry.len(), 1);
}

#[test]
fn rejects_overlapping_passthrough_pcpu_masks() {
    let mut registry = PhysicalResourceRegistry::new();
    let first = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(1).passthrough_cpu(0b0011).build(),
    )
    .unwrap();
    let second = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(2).passthrough_cpu(0b0010).build(),
    )
    .unwrap();

    registry.insert(first).unwrap();
    let error = registry.insert(second).unwrap_err();

    assert!(matches!(
        error,
        AxVmError::ResourceConflict {
            resource: "physical CPU",
            ..
        }
    ));
}

#[test]
fn allows_emulated_vms_to_share_pcpu_affinity() {
    let mut registry = PhysicalResourceRegistry::new();
    let first =
        PhysicalResourceClaims::from_config(&TestConfigBuilder::new(1).emulated_cpu(1).build())
            .unwrap();
    let second =
        PhysicalResourceClaims::from_config(&TestConfigBuilder::new(2).emulated_cpu(1).build())
            .unwrap();

    registry.insert(first).unwrap();
    registry.insert(second).unwrap();
    assert_eq!(registry.len(), 2);
}

#[test]
fn rejects_passthrough_pcpu_overlap_with_emulated_vm() {
    let passthrough = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(1).passthrough_cpu(0b0010).build(),
    )
    .unwrap();
    let emulated = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(2).emulated_cpu(0b0010).build(),
    )
    .unwrap();

    let mut passthrough_first = PhysicalResourceRegistry::new();
    passthrough_first.insert(passthrough.clone()).unwrap();
    assert!(passthrough_first.insert(emulated.clone()).is_err());

    let mut emulated_first = PhysicalResourceRegistry::new();
    emulated_first.insert(emulated).unwrap();
    assert!(emulated_first.insert(passthrough).is_err());
}

#[test]
fn allows_disjoint_passthrough_and_emulated_pcpu_masks() {
    let mut registry = PhysicalResourceRegistry::new();
    let passthrough = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(1).passthrough_cpu(0b0010).build(),
    )
    .unwrap();
    let emulated = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(2).emulated_cpu(0b0100).build(),
    )
    .unwrap();

    registry.insert(passthrough).unwrap();
    registry.insert(emulated).unwrap();
}

#[test]
fn rejects_overlapping_host_physical_ranges_across_sources() {
    let mut registry = PhysicalResourceRegistry::new();
    let device = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(1).device(0x2003, 0x9003, 4).build(),
    )
    .unwrap();
    let reserved = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(2)
            .memory(0x9000, 0x1000, VmMemMappingType::MapReserved)
            .build(),
    )
    .unwrap();

    registry.insert(device).unwrap();
    let error = registry.insert(reserved).unwrap_err();

    assert!(matches!(
        error,
        AxVmError::ResourceConflict {
            resource: "host physical address",
            ..
        }
    ));
}

#[test]
fn allows_adjacent_host_physical_ranges() {
    let mut registry = PhysicalResourceRegistry::new();
    let first = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(1)
            .device(0x2000, 0x9000, 0x1000)
            .build(),
    )
    .unwrap();
    let second = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(2).address(0xa000, 0x1000).build(),
    )
    .unwrap();

    registry.insert(first).unwrap();
    registry.insert(second).unwrap();
    assert_eq!(registry.len(), 2);
}

#[test]
fn rejects_duplicate_passthrough_irq() {
    let mut registry = PhysicalResourceRegistry::new();
    let first = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(1)
            .passthrough_cpu(1)
            .irq(33)
            .irq(33)
            .build(),
    )
    .unwrap();
    let second = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(2).passthrough_cpu(2).irq(33).build(),
    )
    .unwrap();

    registry.insert(first).unwrap();
    let error = registry.insert(second).unwrap_err();

    assert!(matches!(
        error,
        AxVmError::ResourceConflict {
            resource: "passthrough IRQ",
            ..
        }
    ));
}

#[test]
fn released_claims_can_be_reused() {
    let mut registry = PhysicalResourceRegistry::new();
    let original = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(1).address(0x9000, 0x1000).build(),
    )
    .unwrap();
    registry.insert(original).unwrap();

    assert!(registry.remove(1).is_some());

    let replacement = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(2).address(0x9000, 0x1000).build(),
    )
    .unwrap();
    registry.insert(replacement).unwrap();
    assert_eq!(registry.len(), 1);
}

#[test]
fn dynamic_memory_mappings_are_not_preclaimed() {
    let claims = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(1)
            .memory(0x8000_0000, 0x20_0000, VmMemMappingType::MapAlloc)
            .memory(0x9000_0000, 0x20_0000, VmMemMappingType::MapIdentical)
            .build(),
    )
    .unwrap();

    assert!(claims.host_ranges.is_empty());
}

#[test]
fn rejects_overlapping_host_port_ranges() {
    let mut registry = PhysicalResourceRegistry::new();
    let first =
        PhysicalResourceClaims::from_config(&TestConfigBuilder::new(1).port(0x6000, 0x80).build())
            .unwrap();
    let second =
        PhysicalResourceClaims::from_config(&TestConfigBuilder::new(2).port(0x607f, 2).build())
            .unwrap();

    registry.insert(first).unwrap();
    assert!(registry.insert(second).is_err());
}

#[test]
fn allows_adjacent_host_port_ranges() {
    let mut registry = PhysicalResourceRegistry::new();
    let first =
        PhysicalResourceClaims::from_config(&TestConfigBuilder::new(1).port(0x6000, 0x80).build())
            .unwrap();
    let second =
        PhysicalResourceClaims::from_config(&TestConfigBuilder::new(2).port(0x6080, 0x20).build())
            .unwrap();

    registry.insert(first).unwrap();
    registry.insert(second).unwrap();
}

#[test]
fn rejects_invalid_host_port_ranges() {
    let empty =
        PhysicalResourceClaims::from_config(&TestConfigBuilder::new(1).port(0x6000, 0).build());
    let overflowing =
        PhysicalResourceClaims::from_config(&TestConfigBuilder::new(2).port(0xfff0, 0x20).build());

    assert!(empty.is_err());
    assert!(overflowing.is_err());
}

#[test]
fn rejects_passthrough_device_with_mismatched_page_offsets() {
    let error = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(1)
            .device(0x2001, 0x9002, 0x10)
            .build(),
    )
    .unwrap_err();

    assert!(matches!(error, AxVmError::InvalidConfig { .. }));
}

#[test]
fn rejects_unaligned_reserved_memory() {
    let error = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(1)
            .memory(0x9001, 0x1000, VmMemMappingType::MapReserved)
            .build(),
    )
    .unwrap_err();

    assert!(matches!(error, AxVmError::InvalidConfig { .. }));
}

#[test]
fn passthrough_address_space_policy_is_globally_exclusive() {
    let mut registry = PhysicalResourceRegistry::new();
    let whole_space = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(1).passthrough_policy().build(),
    )
    .unwrap();
    let one_page = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(2).address(0x4000, 0x1000).build(),
    )
    .unwrap();

    registry.insert(whole_space).unwrap();
    assert!(registry.insert(one_page).is_err());
}

#[test]
fn passthrough_policy_rejects_dynamic_vm_in_both_orders() {
    let passthrough = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(1).passthrough_policy().build(),
    )
    .unwrap();
    let dynamic = PhysicalResourceClaims::from_config(
        &TestConfigBuilder::new(2)
            .memory(0x8000_0000, 0x20_0000, VmMemMappingType::MapAlloc)
            .build(),
    )
    .unwrap();

    let mut passthrough_first = PhysicalResourceRegistry::new();
    passthrough_first.insert(passthrough.clone()).unwrap();
    assert!(passthrough_first.insert(dynamic.clone()).is_err());

    let mut dynamic_first = PhysicalResourceRegistry::new();
    dynamic_first.insert(dynamic).unwrap();
    assert!(dynamic_first.insert(passthrough).is_err());
}

#[test]
fn boot_only_config_changes_preserve_the_frozen_claim() {
    let mut config = TestConfigBuilder::new(1).address(0x9000, 0x1000).build();
    let frozen = PhysicalResourceClaims::from_config(&config).unwrap();

    config.image_config.kernel_load_gpa = 0x8020_0000usize.into();
    config.cpu_config.bsp_entry = 0x8020_0000usize.into();

    assert_eq!(
        PhysicalResourceClaims::from_config(&config).unwrap(),
        frozen
    );
}

#[test]
fn physical_config_changes_invalidate_the_frozen_claim() {
    let mut config = TestConfigBuilder::new(1).address(0x9000, 0x1000).build();
    let frozen = PhysicalResourceClaims::from_config(&config).unwrap();

    config.add_pass_through_device(PassThroughDeviceConfig {
        name: String::from("late-device"),
        base_gpa: 0x20_000,
        base_hpa: 0xa0_000,
        length: 0x1000,
        irq_id: 0,
    });

    assert_ne!(
        PhysicalResourceClaims::from_config(&config).unwrap(),
        frozen
    );
}

#[test]
fn same_union_vcpu_affinity_swap_invalidates_frozen_claim() {
    let mut config = TestConfigBuilder::new(1)
        .passthrough_cpu(0b0001)
        .passthrough_cpu(0b0010)
        .build();
    let frozen = PhysicalResourceClaims::from_config(&config).unwrap();

    config
        .phys_cpu_ls_mut()
        .set_guest_cpu_sets(vec![0b0010, 0b0001]);

    assert_ne!(
        PhysicalResourceClaims::from_config(&config).unwrap(),
        frozen
    );
}

#[test]
fn physical_cpu_id_change_invalidates_frozen_claim() {
    let mut config = TestConfigBuilder::new(1).emulated_cpu(1).build();
    let frozen = PhysicalResourceClaims::from_config(&config).unwrap();

    config.phys_cpu_ls_mut().set_guest_phys_cpu_ids(vec![7]);

    assert_ne!(
        PhysicalResourceClaims::from_config(&config).unwrap(),
        frozen
    );
}
