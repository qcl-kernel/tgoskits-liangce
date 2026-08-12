// Copyright 2025 The Axvisor Team
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

use alloc::{string::String, vec::Vec};
use core::ptr::NonNull;

use ax_memory_addr::MemoryAddr;
use axvmconfig::AxVMCrateConfig;
use fdt_edit::{Fdt, Node, NodeId};

use super::tree::{FdtTree, GuestMemorySpec};
use crate::{
    AxVMRef, AxVmResult, GuestPhysAddr, VMMemoryRegion, ax_err_type,
    boot::images::load_vm_image_from_memory,
};

pub fn create_guest_fdt(
    fdt: &Fdt,
    passthrough_device_names: &[String],
    crate_config: &AxVMCrateConfig,
) -> AxVmResult<Vec<u8>> {
    let phys_cpu_ids = crate_config
        .base
        .phys_cpu_ids
        .as_deref()
        .ok_or_else(|| ax_err_type!(InvalidInput, "phys_cpu_ids is missing"))?;

    let mut guest_tree = FdtTree::clone_filtered(fdt, |node_id, path, node| {
        should_keep_generated_node(
            fdt,
            node_id,
            path,
            node,
            passthrough_device_names,
            phys_cpu_ids,
            &crate_config.devices.excluded_devices,
        )
    })?;
    super::sanitize::sanitize_guest_fdt(guest_tree.inner_mut())?;
    Ok(guest_tree.finish())
}

fn should_keep_generated_node(
    fdt: &Fdt,
    node_id: NodeId,
    node_path: &str,
    node: &Node,
    passthrough_device_names: &[String],
    phys_cpu_ids: &[usize],
    excluded_devices: &[Vec<String>],
) -> bool {
    if excluded_devices.iter().flatten().any(|excluded| {
        node_path == excluded
            || node_path
                .strip_prefix(excluded)
                .is_some_and(|suffix| suffix.starts_with('/'))
    }) {
        return false;
    }

    // Host reservations describe host ownership.  In particular, a host VM
    // carveout can exactly cover the guest's RAM, so it must not be inherited
    // by a generated guest DTB even when the root node is passed through.
    if node_path == "/reserved-memory" || node_path.starts_with("/reserved-memory/") {
        return false;
    }

    if node.name().starts_with("memory") {
        return false;
    }

    if node_path == "/cpus" || node_path.starts_with("/cpus/cpu-map") {
        return true;
    }

    if node_path.starts_with("/cpus/cpu@") {
        return need_cpu_node(phys_cpu_ids, fdt, node_id, node_path);
    }

    passthrough_device_names
        .iter()
        .any(|device_path| device_path == node_path)
        || is_descendant_of_passthrough_device(node_path, passthrough_device_names)
        || is_ancestor_of_passthrough_device(node_path, passthrough_device_names)
}

fn is_descendant_of_passthrough_device(
    node_path: &str,
    passthrough_device_names: &[String],
) -> bool {
    passthrough_device_names.iter().any(|passthrough_path| {
        node_path
            .strip_prefix(passthrough_path)
            .is_some_and(|suffix| suffix.starts_with('/'))
    })
}

fn is_ancestor_of_passthrough_device(node_path: &str, passthrough_device_names: &[String]) -> bool {
    passthrough_device_names.iter().any(|passthrough_path| {
        passthrough_path
            .strip_prefix(node_path)
            .is_some_and(|suffix| suffix.starts_with('/'))
            || node_path == "/"
    })
}

fn cpu_node_id(node_path: &str) -> Option<usize> {
    node_path
        .strip_prefix("/cpus/cpu@")
        .and_then(|rest| rest.split('/').next())
        .and_then(|id| usize::from_str_radix(id, 16).ok())
}

fn cpu_reg_address(fdt: &Fdt, node_id: NodeId) -> Option<usize> {
    fdt.view_typed(node_id)
        .and_then(|node| node.regs().first().map(|reg| reg.address as usize))
}

pub(crate) fn need_cpu_node(
    phys_cpu_ids: &[usize],
    fdt: &Fdt,
    node_id: NodeId,
    node_path: &str,
) -> bool {
    if !node_path.starts_with("/cpus/cpu@") {
        return true;
    }

    if let Some(cpu_id) = cpu_node_id(node_path) {
        return phys_cpu_ids.contains(&cpu_id);
    }

    cpu_reg_address(fdt, node_id).is_some_and(|cpu_address| {
        debug!("Checking CPU node {node_path} with address 0x{cpu_address:x}");
        phys_cpu_ids.contains(&cpu_address)
    })
}

fn guest_memory_specs(
    new_memory: &[VMMemoryRegion],
    crate_config: &AxVMCrateConfig,
) -> Vec<GuestMemorySpec> {
    let configured_region_count = if crate_config.kernel.configured_memory_region_count == 0 {
        crate_config.kernel.memory_regions.len()
    } else {
        crate_config
            .kernel
            .configured_memory_region_count
            .min(crate_config.kernel.memory_regions.len())
    };

    if new_memory.len() != crate_config.kernel.memory_regions.len() {
        warn!(
            "VM memory region count {} does not match config region count {}; filtering /memory \
             by zipped order",
            new_memory.len(),
            crate_config.kernel.memory_regions.len()
        );
    }

    new_memory
        .iter()
        .take(configured_region_count)
        .zip(
            crate_config
                .kernel
                .memory_regions
                .iter()
                .take(configured_region_count),
        )
        .map(|(mem, _cfg)| GuestMemorySpec::new(mem.gpa.as_usize() as u64, mem.size() as u64))
        .collect()
}

#[cfg(test)]
fn initrd_range_from_image_config(
    ramdisk: Option<&crate::config::RamdiskInfo>,
) -> Option<(u64, u64)> {
    let ramdisk = ramdisk?;
    let start = ramdisk.load_gpa.as_usize() as u64;
    let size = ramdisk.size? as u64;
    Some((start, start.saturating_add(size)))
}

pub fn update_fdt(
    fdt_src: NonNull<u8>,
    dtb_size: usize,
    vm: AxVMRef,
    crate_config: &AxVMCrateConfig,
) -> AxVmResult {
    let patch_runtime = super::selected_guest_fdt_policy().patch_runtime;
    // SAFETY: `fdt_src` originates from `GuestDtbImage::as_bytes`, and the
    // caller supplies the exact slice length while the image remains borrowed.
    let fdt_bytes = unsafe { core::slice::from_raw_parts(fdt_src.as_ptr(), dtb_size) };
    let new_fdt_bytes = patch_runtime(fdt_bytes, &vm, crate_config)?;

    load_patched_fdt(vm, new_fdt_bytes)
}

fn load_patched_fdt(vm: AxVMRef, new_fdt_bytes: Vec<u8>) -> AxVmResult {
    let dtb_size = new_fdt_bytes.len();
    let dest_addr = calculate_dtb_load_addr(vm.clone(), dtb_size)?;
    debug!(
        "New FDT will be loaded at {:x}, size: 0x{:x}",
        dest_addr, dtb_size
    );
    load_vm_image_from_memory(&new_fdt_bytes, dest_addr, vm.clone())?;
    vm.set_guest_device_tree(dest_addr, new_fdt_bytes)?;
    #[cfg(feature = "guest-fdt-evidence")]
    crate::boot::fdt::evidence::emit_ready_marker(&vm, dest_addr, dtb_size)?;
    Ok(())
}

pub fn patch_guest_fdt_for_runtime(
    fdt_bytes: &[u8],
    memory_regions: &[VMMemoryRegion],
    crate_config: &AxVMCrateConfig,
    initrd_start_size: Option<(u64, u64)>,
    create_chosen: bool,
) -> AxVmResult<Vec<u8>> {
    let mut tree = FdtTree::from_bytes(fdt_bytes)?;
    let memory_specs = guest_memory_specs(memory_regions, crate_config);
    tree.rebuild_memory_nodes(&memory_specs)?;
    if create_chosen
        || initrd_start_size.is_some()
        || tree.inner().get_by_path_id("/chosen").is_some()
    {
        tree.patch_chosen(initrd_start_size)?;
    }
    Ok(tree.finish())
}

pub(crate) fn calculate_dtb_load_addr(vm: AxVMRef, fdt_size: usize) -> AxVmResult<GuestPhysAddr> {
    const MB: usize = 1024 * 1024;

    let main_memory =
        vm.memory_regions().first().cloned().ok_or_else(|| {
            ax_err_type!(InvalidInput, "VM has no memory region for DTB placement")
        })?;

    let dtb_addr = vm.with_config_mut(|config| {
        let use_configured_dtb_addr =
            config.image_config.dtb_load_gpa.is_some() && !main_memory.is_identical();

        let dtb_addr = if let Some(configured) = config
            .image_config
            .dtb_load_gpa
            .filter(|_| use_configured_dtb_addr)
        {
            configured
        } else {
            let main_memory_size = main_memory.size().min(512 * MB);
            let addr = (main_memory.gpa + main_memory_size - fdt_size).align_down(2 * MB);
            if fdt_size > main_memory_size {
                error!("DTB size is larger than available memory");
            }
            addr
        };
        config.image_config.dtb_load_gpa = Some(dtb_addr);
        dtb_addr
    });

    Ok(dtb_addr)
}

#[cfg(test)]
mod tests {
    use alloc::format;
    use core::alloc::Layout;

    use axvm_types::HostVirtAddr;
    use axvmconfig::{AxVMCrateConfig, VMDevicesConfig};
    use fdt_edit::{Fdt, Node, Property};
    use fdt_raw::RegInfo;

    use super::{
        super::tree::sanitize_bootargs, cpu_node_id, initrd_range_from_image_config, need_cpu_node,
    };
    use crate::{GuestPhysAddr, VMMemoryRegion, config::RamdiskInfo};

    fn prop_u32(name: &str, value: u32) -> Property {
        let mut prop = Property::new(name, alloc::vec![]);
        prop.set_u32_ls(&[value]);
        prop
    }

    fn prop_u32s(name: &str, values: &[u32]) -> Property {
        let mut prop = Property::new(name, alloc::vec![]);
        prop.set_u32_ls(values);
        prop
    }

    fn prop_str(name: &str, value: &str) -> Property {
        let mut prop = Property::new(name, alloc::vec![]);
        prop.set_string(value);
        prop
    }

    fn test_fdt(dts: &str) -> Fdt {
        let mut fdt = Fdt::new();
        let root = fdt.root_id();
        let cpus = fdt.add_node(root, Node::new("cpus"));
        fdt.node_mut(cpus)
            .unwrap()
            .set_property(prop_u32("#address-cells", 2));
        fdt.node_mut(cpus)
            .unwrap()
            .set_property(prop_u32("#size-cells", 0));

        for line in dts.lines().map(str::trim).filter(|line| !line.is_empty()) {
            let (name, reg) = line.split_once('=').unwrap();
            let node = fdt.add_node(cpus, Node::new(name));
            let reg = usize::from_str_radix(reg, 16).unwrap();
            fdt.view_typed_mut(node)
                .unwrap()
                .set_regs(&[RegInfo::new(reg as u64, None)]);
        }

        fdt
    }

    fn add_two_cpu_map(fdt: &mut Fdt) {
        let cpu0 = fdt.get_by_path_id("/cpus/cpu@0").unwrap();
        let cpu1 = fdt.get_by_path_id("/cpus/cpu@1").unwrap();
        fdt.node_mut(cpu0)
            .unwrap()
            .set_property(prop_u32("phandle", 1));
        fdt.node_mut(cpu1)
            .unwrap()
            .set_property(prop_u32("phandle", 2));

        let cpus = fdt.get_by_path_id("/cpus").unwrap();
        let cpu_map = fdt.add_node(cpus, Node::new("cpu-map"));
        let cluster = fdt.add_node(cpu_map, Node::new("cluster0"));
        let core0 = fdt.add_node(cluster, Node::new("core0"));
        let core1 = fdt.add_node(cluster, Node::new("core1"));
        fdt.node_mut(core0)
            .unwrap()
            .set_property(prop_u32("cpu", 1));
        fdt.node_mut(core1)
            .unwrap()
            .set_property(prop_u32("cpu", 2));
    }

    fn add_interrupt_controller(fdt: &mut Fdt, phandle: u32) {
        let controller = fdt.add_node(fdt.root_id(), Node::new("intc@8000000"));
        fdt.node_mut(controller)
            .unwrap()
            .set_property(prop_u32("phandle", phandle));
        fdt.node_mut(controller)
            .unwrap()
            .set_property(Property::new("interrupt-controller", alloc::vec![]));
        fdt.node_mut(controller)
            .unwrap()
            .set_property(prop_u32("#interrupt-cells", 3));
    }

    fn add_interrupt_device(fdt: &mut Fdt) {
        let device = fdt.add_node(fdt.root_id(), Node::new("device@1000"));
        fdt.node_mut(device)
            .unwrap()
            .set_property(prop_u32s("interrupts", &[0, 16, 4]));
    }

    fn add_vm_carveout(fdt: &mut Fdt, base: u64, size: u64) {
        let root = fdt.root_id();
        fdt.node_mut(root)
            .unwrap()
            .set_property(prop_u32("#address-cells", 2));
        fdt.node_mut(root)
            .unwrap()
            .set_property(prop_u32("#size-cells", 2));
        let reserved = fdt.add_node(root, Node::new("reserved-memory"));
        fdt.node_mut(reserved)
            .unwrap()
            .set_property(prop_u32("#address-cells", 2));
        fdt.node_mut(reserved)
            .unwrap()
            .set_property(prop_u32("#size-cells", 2));
        let carveout = fdt.add_node(reserved, Node::new(&format!("vm-carveout@{base:x}")));
        fdt.node_mut(carveout)
            .unwrap()
            .set_property(prop_str("compatible", "axvisor,vm-carveout-v1"));
        fdt.node_mut(carveout)
            .unwrap()
            .set_property(prop_u32("axvisor,vm-id", 1));
        fdt.view_typed_mut(carveout)
            .unwrap()
            .set_regs(&[RegInfo::new(base, Some(size))]);
    }

    fn assert_no_reserved_memory(fdt: &Fdt) {
        assert!(fdt.get_by_path_id("/reserved-memory").is_none());
        assert!(
            !fdt.iter_node_ids()
                .map(|id| fdt.path_of(id))
                .any(|path| path.starts_with("/reserved-memory/"))
        );
    }

    #[test]
    fn cpu_node_selection_uses_node_id_when_reg_differs() {
        let fdt = test_fdt("cpu@0=200\ncpu@100=0\ncpu@101=100");
        let selected: alloc::vec::Vec<_> = fdt
            .iter_node_ids()
            .map(|id| (id, fdt.path_of(id)))
            .filter(|(_, path)| path.starts_with("/cpus/cpu@"))
            .filter_map(|(id, path)| need_cpu_node(&[0x100], &fdt, id, &path).then_some(path))
            .collect();

        assert_eq!(selected, ["/cpus/cpu@100"]);
    }

    #[test]
    fn cpu_node_id_parses_hex_unit_address() {
        assert_eq!(cpu_node_id("/cpus/cpu@100"), Some(0x100));
    }

    #[test]
    fn initrd_range_requires_both_address_and_size() {
        assert_eq!(
            initrd_range_from_image_config(Some(&RamdiskInfo {
                load_gpa: GuestPhysAddr::from(0xa000_0000usize),
                size: None,
            })),
            None
        );
        assert_eq!(
            initrd_range_from_image_config(Some(&RamdiskInfo {
                load_gpa: GuestPhysAddr::from(0xa000_0000usize),
                size: Some(0x1234),
            })),
            Some((0xa000_0000, 0xa000_1234))
        );
    }

    #[test]
    fn sanitize_bootargs_enables_auto_repair_for_block_roots() {
        let bootargs = "root=/dev/mmcblk0p2 rw console=ttyS2,1500000 rootwait rootfstype=ext4";

        assert_eq!(
            sanitize_bootargs(bootargs),
            "root=/dev/mmcblk0p2 rw console=ttyS2,1500000 rootwait rootfstype=ext4 fsck.repair=yes"
        );
    }

    #[test]
    fn sanitize_bootargs_preserves_existing_fsck_policy() {
        let bootargs =
            "root=/dev/mmcblk0p2 ro rootwait rootfstype=ext4 fsckfix rdinit=/init root=/dev/ram0";

        assert_eq!(
            sanitize_bootargs(bootargs),
            "root=/dev/mmcblk0p2 rw rootwait rootfstype=ext4 fsckfix"
        );
    }

    #[test]
    fn runtime_patch_can_leave_missing_chosen_for_host_copy() {
        let fdt = Fdt::new();
        let dtb = fdt.encode().as_ref().to_vec();
        let cfg = AxVMCrateConfig::default();

        let patched = super::patch_guest_fdt_for_runtime(&dtb, &[], &cfg, None, false).unwrap();
        let reparsed = Fdt::from_bytes(&patched).unwrap();

        assert!(reparsed.get_by_path_id("/chosen").is_none());

        let patched = super::patch_guest_fdt_for_runtime(&dtb, &[], &cfg, None, true).unwrap();
        let reparsed = Fdt::from_bytes(&patched).unwrap();

        assert!(reparsed.get_by_path_id("/chosen").is_some());
    }

    #[test]
    fn generated_fdt_filters_cpu_nodes_by_unit_address() {
        let fdt = test_fdt("cpu@0=200\ncpu@100=0\ncpu@101=100");
        let cfg = AxVMCrateConfig {
            base: axvmconfig::VMBaseConfig {
                phys_cpu_ids: Some(alloc::vec![0x100]),
                ..Default::default()
            },
            ..Default::default()
        };
        let dtb = super::create_guest_fdt(&fdt, &[], &cfg).unwrap();
        let reparsed = Fdt::from_bytes(&dtb).unwrap();

        assert!(reparsed.get_by_path_id("/cpus/cpu@100").is_some());
        assert!(reparsed.get_by_path_id("/cpus/cpu@0").is_none());
        assert!(reparsed.get_by_path_id("/cpus/cpu@101").is_none());
    }

    #[test]
    fn generated_fdt_drops_host_vm_carveout_before_root_passthrough() {
        let mut fdt = test_fdt("cpu@0=0");
        add_vm_carveout(&mut fdt, 0x8000_0000, 0x1000_0000);
        let cfg = AxVMCrateConfig {
            base: axvmconfig::VMBaseConfig {
                phys_cpu_ids: Some(alloc::vec![0]),
                ..Default::default()
            },
            kernel: axvmconfig::VMKernelConfig {
                memory_regions: alloc::vec![axvmconfig::VmMemConfig {
                    gpa: 0x8000_0000,
                    size: 0x1000_0000,
                    flags: 0x7,
                    map_type: axvmconfig::VmMemMappingType::MapReserved,
                }],
                ..Default::default()
            },
            ..Default::default()
        };
        let passthrough = alloc::vec!["/".into()];

        let generated = super::create_guest_fdt(&fdt, &passthrough, &cfg).unwrap();
        let generated_fdt = Fdt::from_bytes(&generated).unwrap();
        assert_no_reserved_memory(&generated_fdt);

        let memory = VMMemoryRegion {
            gpa: GuestPhysAddr::from(0x8000_0000usize),
            hva: HostVirtAddr::from(0x8000_0000usize),
            layout: Layout::from_size_align(0x1000_0000, 0x20_0000).unwrap(),
            needs_dealloc: false,
        };
        let patched =
            super::patch_guest_fdt_for_runtime(&generated, &[memory], &cfg, None, false).unwrap();
        let patched_fdt = Fdt::from_bytes(&patched).unwrap();
        assert_no_reserved_memory(&patched_fdt);
        let memory_id = patched_fdt.get_by_path_id("/memory@80000000").unwrap();
        let memory_node = patched_fdt.view_typed(memory_id).unwrap();
        let regs = memory_node.regs();
        let reg = regs.first().unwrap();
        assert_eq!(reg.address, 0x8000_0000);
        assert_eq!(reg.size, Some(0x1000_0000));
    }

    #[test]
    fn generated_fdt_keeps_similarly_named_selected_device() {
        let mut fdt = test_fdt("cpu@0=0");
        fdt.add_node(fdt.root_id(), Node::new("reserved-memory-device"));
        let cfg = AxVMCrateConfig {
            base: axvmconfig::VMBaseConfig {
                phys_cpu_ids: Some(alloc::vec![0]),
                ..Default::default()
            },
            ..Default::default()
        };
        let selected = alloc::vec!["/reserved-memory-device".into()];

        let dtb = super::create_guest_fdt(&fdt, &selected, &cfg).unwrap();
        let reparsed = Fdt::from_bytes(&dtb).unwrap();

        assert!(reparsed.get_by_path_id("/reserved-memory-device").is_some());
    }

    #[test]
    fn generated_fdt_prunes_cpu_map_entries_for_removed_cpus() {
        let mut fdt = test_fdt("cpu@0=0\ncpu@1=1");
        add_two_cpu_map(&mut fdt);

        let cfg = AxVMCrateConfig {
            base: axvmconfig::VMBaseConfig {
                phys_cpu_ids: Some(alloc::vec![0]),
                ..Default::default()
            },
            ..Default::default()
        };
        let dtb = super::create_guest_fdt(&fdt, &[], &cfg).unwrap();
        let reparsed = Fdt::from_bytes(&dtb).unwrap();

        assert!(reparsed.get_by_path_id("/cpus/cpu@0").is_some());
        assert!(reparsed.get_by_path_id("/cpus/cpu@1").is_none());
        assert!(
            reparsed
                .get_by_path_id("/cpus/cpu-map/cluster0/core0")
                .is_some()
        );
        assert!(
            reparsed
                .get_by_path_id("/cpus/cpu-map/cluster0/core1")
                .is_none()
        );
    }

    #[test]
    fn generated_fdt_drops_cpu_map_for_duplicate_cpu_phandles() {
        let mut fdt = test_fdt("cpu@0=0\ncpu@1=1");
        add_two_cpu_map(&mut fdt);
        let cpu1 = fdt.get_by_path_id("/cpus/cpu@1").unwrap();
        fdt.node_mut(cpu1)
            .unwrap()
            .set_property(prop_u32("phandle", 1));

        let cfg = AxVMCrateConfig {
            base: axvmconfig::VMBaseConfig {
                phys_cpu_ids: Some(alloc::vec![0, 1]),
                ..Default::default()
            },
            ..Default::default()
        };
        let dtb = super::create_guest_fdt(&fdt, &[], &cfg).unwrap();
        let reparsed = Fdt::from_bytes(&dtb).unwrap();

        assert!(reparsed.get_by_path_id("/cpus/cpu-map").is_none());
    }

    #[test]
    fn generated_fdt_drops_cpu_map_for_malformed_cpu_phandle() {
        let mut fdt = test_fdt("cpu@0=0\ncpu@1=1");
        add_two_cpu_map(&mut fdt);
        let cpu1 = fdt.get_by_path_id("/cpus/cpu@1").unwrap();
        fdt.node_mut(cpu1).unwrap().set_property(Property::new(
            "phandle",
            alloc::vec![0, 0, 0, 2, 0, 0, 0, 3],
        ));

        let cfg = AxVMCrateConfig {
            base: axvmconfig::VMBaseConfig {
                phys_cpu_ids: Some(alloc::vec![0, 1]),
                ..Default::default()
            },
            ..Default::default()
        };
        let dtb = super::create_guest_fdt(&fdt, &[], &cfg).unwrap();
        let reparsed = Fdt::from_bytes(&dtb).unwrap();

        assert!(reparsed.get_by_path_id("/cpus/cpu-map").is_none());
    }

    #[test]
    fn generated_fdt_rejects_missing_inherited_interrupt_parent() {
        let mut fdt = test_fdt("cpu@0=0");
        let root = fdt.root_id();
        fdt.node_mut(root)
            .unwrap()
            .set_property(prop_u32("interrupt-parent", 0x42));
        add_interrupt_device(&mut fdt);

        let cfg = AxVMCrateConfig {
            base: axvmconfig::VMBaseConfig {
                phys_cpu_ids: Some(alloc::vec![0]),
                ..Default::default()
            },
            ..Default::default()
        };
        let selected = alloc::vec!["/device@1000".into()];

        assert!(super::create_guest_fdt(&fdt, &selected, &cfg).is_err());
    }

    #[test]
    fn generated_fdt_accepts_retained_interrupt_parent() {
        let mut fdt = test_fdt("cpu@0=0");
        let root = fdt.root_id();
        fdt.node_mut(root)
            .unwrap()
            .set_property(prop_u32("interrupt-parent", 0x42));
        add_interrupt_device(&mut fdt);
        add_interrupt_controller(&mut fdt, 0x42);

        let cfg = AxVMCrateConfig {
            base: axvmconfig::VMBaseConfig {
                phys_cpu_ids: Some(alloc::vec![0]),
                ..Default::default()
            },
            ..Default::default()
        };
        let selected = alloc::vec!["/device@1000".into(), "/intc@8000000".into()];

        assert!(super::create_guest_fdt(&fdt, &selected, &cfg).is_ok());
    }

    #[test]
    fn generated_fdt_exclusion_overrides_retained_parent_subtree() {
        let mut fdt = test_fdt("cpu@0=0");
        add_interrupt_controller(&mut fdt, 0x42);
        let controller = fdt.get_by_path_id("/intc@8000000").unwrap();
        let its = fdt.add_node(controller, Node::new("its@8080000"));
        fdt.node_mut(its)
            .unwrap()
            .set_property(prop_str("compatible", "arm,gic-v3-its"));

        let cfg = AxVMCrateConfig {
            base: axvmconfig::VMBaseConfig {
                phys_cpu_ids: Some(alloc::vec![0]),
                ..Default::default()
            },
            devices: VMDevicesConfig {
                excluded_devices: alloc::vec![alloc::vec!["/intc@8000000/its@8080000".into(),]],
                ..Default::default()
            },
            ..Default::default()
        };
        let selected = alloc::vec!["/intc@8000000".into()];
        let dtb = super::create_guest_fdt(&fdt, &selected, &cfg).unwrap();
        let reparsed = Fdt::from_bytes(&dtb).unwrap();

        assert!(reparsed.get_by_path_id("/intc@8000000").is_some());
        assert!(
            reparsed
                .get_by_path_id("/intc@8000000/its@8080000")
                .is_none()
        );
    }

    #[test]
    fn generated_fdt_rejects_conflicting_controller_phandles() {
        let mut fdt = test_fdt("cpu@0=0");
        let root = fdt.root_id();
        fdt.node_mut(root)
            .unwrap()
            .set_property(prop_u32("interrupt-parent", 0x42));
        add_interrupt_device(&mut fdt);
        add_interrupt_controller(&mut fdt, 0x42);
        let controller = fdt.get_by_path_id("/intc@8000000").unwrap();
        fdt.node_mut(controller)
            .unwrap()
            .set_property(prop_u32("linux,phandle", 0x43));

        let cfg = AxVMCrateConfig {
            base: axvmconfig::VMBaseConfig {
                phys_cpu_ids: Some(alloc::vec![0]),
                ..Default::default()
            },
            ..Default::default()
        };
        let selected = alloc::vec!["/device@1000".into(), "/intc@8000000".into()];

        assert!(super::create_guest_fdt(&fdt, &selected, &cfg).is_err());
    }

    #[test]
    fn provided_fdt_cpu_replacement_prunes_cpu_map_and_missing_console() {
        let mut host_fdt = test_fdt("cpu@0=0\ncpu@1=1");
        add_two_cpu_map(&mut host_fdt);

        let mut provided_fdt = Fdt::new();
        let root = provided_fdt.root_id();
        let chosen = provided_fdt.add_node(root, Node::new("chosen"));
        provided_fdt
            .node_mut(chosen)
            .unwrap()
            .set_property(prop_str("bootargs", "root=/dev/vda rw"));
        provided_fdt
            .node_mut(chosen)
            .unwrap()
            .set_property(prop_str("stdout-path", "/pl011@9000000:115200n8"));

        let dtb =
            super::super::sanitize::replace_cpu_nodes_from_host(&provided_fdt, &host_fdt, &[0])
                .unwrap();
        let reparsed = Fdt::from_bytes(&dtb).unwrap();
        let chosen = reparsed.get_by_path("/chosen").unwrap().as_node();

        assert!(reparsed.get_by_path_id("/cpus/cpu@0").is_some());
        assert!(reparsed.get_by_path_id("/cpus/cpu@1").is_none());
        assert!(
            reparsed
                .get_by_path_id("/cpus/cpu-map/cluster0/core0")
                .is_some()
        );
        assert!(
            reparsed
                .get_by_path_id("/cpus/cpu-map/cluster0/core1")
                .is_none()
        );
        assert_eq!(
            chosen.get_property("bootargs").unwrap().as_str(),
            Some("root=/dev/vda rw")
        );
        assert!(chosen.get_property("stdout-path").is_none());
    }

    #[test]
    fn provided_fdt_rejects_missing_interrupt_references() {
        let host_fdt = test_fdt("cpu@0=0");
        let mut provided_fdt = Fdt::new();
        let root = provided_fdt.root_id();
        provided_fdt
            .node_mut(root)
            .unwrap()
            .set_property(prop_u32("interrupt-parent", 0x42));
        add_interrupt_device(&mut provided_fdt);

        assert!(
            super::super::sanitize::replace_cpu_nodes_from_host(&provided_fdt, &host_fdt, &[0])
                .is_err()
        );
        add_interrupt_controller(&mut provided_fdt, 0x42);
        assert!(
            super::super::sanitize::replace_cpu_nodes_from_host(&provided_fdt, &host_fdt, &[0])
                .is_ok()
        );

        let mut extended_fdt = Fdt::new();
        let device = extended_fdt.add_node(extended_fdt.root_id(), Node::new("device@2000"));
        extended_fdt
            .node_mut(device)
            .unwrap()
            .set_property(prop_u32s("interrupts-extended", &[0x43, 0, 17, 4]));
        assert!(
            super::super::sanitize::replace_cpu_nodes_from_host(&extended_fdt, &host_fdt, &[0])
                .is_err()
        );
        add_interrupt_controller(&mut extended_fdt, 0x43);
        assert!(
            super::super::sanitize::replace_cpu_nodes_from_host(&extended_fdt, &host_fdt, &[0])
                .is_ok()
        );
    }

    #[test]
    fn provided_fdt_rejects_empty_interrupts_extended() {
        let host_fdt = test_fdt("cpu@0=0");
        let mut provided_fdt = Fdt::new();
        let device = provided_fdt.add_node(provided_fdt.root_id(), Node::new("device@2000"));
        provided_fdt
            .node_mut(device)
            .unwrap()
            .set_property(Property::new("interrupts-extended", alloc::vec![]));

        assert!(
            super::super::sanitize::replace_cpu_nodes_from_host(&provided_fdt, &host_fdt, &[0])
                .is_err()
        );
    }

    #[test]
    fn generated_fdt_drops_missing_chosen_console_but_keeps_bootargs() {
        let mut fdt = test_fdt("cpu@0=0");
        let root = fdt.root_id();
        let chosen = fdt.add_node(root, Node::new("chosen"));
        fdt.node_mut(chosen)
            .unwrap()
            .set_property(prop_str("bootargs", "root=/dev/vda rw"));
        fdt.node_mut(chosen)
            .unwrap()
            .set_property(prop_str("stdout-path", "/pl011@9000000:115200n8"));
        fdt.node_mut(chosen)
            .unwrap()
            .set_property(prop_str("linux,stdout-path", "/pl011@9000000:115200n8"));
        fdt.add_node(root, Node::new("pl011@9000000"));

        let cfg = AxVMCrateConfig {
            base: axvmconfig::VMBaseConfig {
                phys_cpu_ids: Some(alloc::vec![0]),
                ..Default::default()
            },
            ..Default::default()
        };
        let selected = alloc::vec!["/chosen".into()];
        let dtb = super::create_guest_fdt(&fdt, &selected, &cfg).unwrap();
        let reparsed = Fdt::from_bytes(&dtb).unwrap();
        let chosen = reparsed.get_by_path("/chosen").unwrap().as_node();

        assert_eq!(
            chosen.get_property("bootargs").unwrap().as_str(),
            Some("root=/dev/vda rw")
        );
        assert!(chosen.get_property("stdout-path").is_none());
        assert!(chosen.get_property("linux,stdout-path").is_none());
        assert!(reparsed.get_by_path_id("/pl011@9000000").is_none());
    }
}
