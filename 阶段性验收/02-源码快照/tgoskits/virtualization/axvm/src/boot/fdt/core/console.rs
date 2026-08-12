//! AArch64 Guest FDT description for a VM-owned polling PL011.

use alloc::{format, string::String, vec::Vec};

use axvm_types::{EmulatedDeviceConfig, EmulatedDeviceType};
use axvmconfig::AxVMCrateConfig;
use fdt_edit::{Node, Property, RegFixed};
use fdt_raw::RegInfo;

use super::tree::{FdtTree, prop_string};
use crate::{AxVmResult, ax_err_type};

const PL011_MMIO_SIZE: usize = 0x1000;
const TX_ONLY_IRQ_SENTINEL: usize = 0;
const POLLING_STDOUT_PATH: &str = "serial0:115200n8";
const PL011_BAUD_RATE: u32 = 115_200;

/// Installs the polling-only PL011 described by this VM's emulated devices.
///
/// An absent console leaves the DTB byte-for-byte unchanged. A configured
/// console replaces any node at the same GPA so a provided physical PL011
/// cannot retain IRQ, DMA, clock, or host-resource properties.
pub(crate) fn install_vm_owned_polling_pl011(
    fdt_bytes: &[u8],
    crate_config: &AxVMCrateConfig,
) -> AxVmResult<Vec<u8>> {
    let Some(console) = polling_console_config(crate_config)? else {
        return Ok(fdt_bytes.to_vec());
    };
    validate_polling_console(console)?;

    let mut tree = FdtTree::from_bytes(fdt_bytes)?;
    validate_root_cells(&tree)?;
    let node_path = format!("/pl011@{:x}", console.base_gpa);
    let stale_console_paths = stale_console_paths(&tree, console)?;
    if tree.inner().get_by_path_id(&node_path).is_some()
        && !stale_console_paths.iter().any(|path| path == &node_path)
    {
        return Err(ax_err_type!(
            InvalidData,
            format!("Guest FDT node {node_path} does not describe the configured PL011 range")
        ));
    }

    // Create these parents first so replacing the console remains byte-stable
    // when this final patch is applied to a developer-provided DTB twice.
    patch_console_references(&mut tree, &node_path, console.base_gpa)?;
    remove_stale_console_nodes(&mut tree, stale_console_paths);
    add_polling_console_node(&mut tree, console)?;
    super::sanitize::sanitize_guest_fdt(tree.inner_mut())?;
    Ok(tree.finish())
}

fn polling_console_config(
    crate_config: &AxVMCrateConfig,
) -> AxVmResult<Option<&EmulatedDeviceConfig>> {
    let mut consoles = crate_config
        .devices
        .emu_devices
        .iter()
        .filter(|device| device.emu_type == EmulatedDeviceType::Console);
    let console = consoles.next();
    if consoles.next().is_some() {
        return Err(ax_err_type!(
            InvalidInput,
            "AArch64 polling console requires exactly one VM-owned Console device"
        ));
    }
    Ok(console)
}

fn validate_polling_console(console: &EmulatedDeviceConfig) -> AxVmResult {
    if console.name.trim().is_empty() {
        return Err(ax_err_type!(
            InvalidInput,
            "AArch64 polling console name is empty"
        ));
    }
    if console.length != PL011_MMIO_SIZE {
        return Err(ax_err_type!(
            InvalidInput,
            format!(
                "AArch64 polling console '{}' requires length {PL011_MMIO_SIZE:#x}, got {:#x}",
                console.name, console.length
            )
        ));
    }
    if !console.base_gpa.is_multiple_of(PL011_MMIO_SIZE)
        || console.base_gpa.checked_add(console.length).is_none()
    {
        return Err(ax_err_type!(
            InvalidInput,
            format!(
                "AArch64 polling console '{}' has an invalid GPA range",
                console.name
            )
        ));
    }
    if console.irq_id != TX_ONLY_IRQ_SENTINEL {
        return Err(ax_err_type!(
            InvalidInput,
            format!(
                "AArch64 polling console '{}' must use IRQ sentinel {TX_ONLY_IRQ_SENTINEL}",
                console.name
            )
        ));
    }
    if !console.cfg_list.is_empty() {
        return Err(ax_err_type!(
            InvalidInput,
            format!(
                "AArch64 polling console '{}' cannot request backend, RX, IRQ, or DMA arguments",
                console.name
            )
        ));
    }
    Ok(())
}

fn validate_root_cells(tree: &FdtTree) -> AxVmResult {
    let root = tree
        .inner()
        .node(tree.inner().root_id())
        .ok_or_else(|| ax_err_type!(InvalidData, "Guest FDT root node is missing"))?;
    let address_cells = root
        .get_property("#address-cells")
        .and_then(|property| property.get_u32());
    let size_cells = root
        .get_property("#size-cells")
        .and_then(|property| property.get_u32());
    if address_cells != Some(2) || size_cells != Some(2) {
        return Err(ax_err_type!(
            InvalidData,
            "AArch64 polling console requires 2-cell Guest FDT addresses and sizes"
        ));
    }
    Ok(())
}

fn stale_console_paths(tree: &FdtTree, console: &EmulatedDeviceConfig) -> AxVmResult<Vec<String>> {
    let target_start = console.base_gpa as u64;
    let target_end = target_start + console.length as u64;
    let mut paths = Vec::new();

    for node_id in tree.inner().iter_node_ids() {
        let Some(view) = tree.inner().view_typed(node_id) else {
            continue;
        };
        if !view
            .regs()
            .iter()
            .any(|reg| reg_overlaps(reg, target_start, target_end))
        {
            continue;
        }

        let Some(node) = tree.inner().node(node_id) else {
            continue;
        };
        let path = tree.inner().path_of(node_id);
        if !node
            .compatibles()
            .any(|compatible| compatible == "arm,pl011")
        {
            return Err(ax_err_type!(
                InvalidData,
                format!(
                    "Guest FDT node {path} overlaps VM-owned PL011 GPA {target_start:#x} but is \
                     not arm,pl011"
                )
            ));
        }
        paths.push(path);
    }
    Ok(paths)
}

fn reg_overlaps(reg: &RegFixed, target_start: u64, target_end: u64) -> bool {
    if target_start <= reg.address && reg.address < target_end {
        return true;
    }
    let Some(size) = reg.size.filter(|size| *size != 0) else {
        return false;
    };
    let reg_end = reg.address.saturating_add(size);
    reg.address < target_end && target_start < reg_end
}

fn remove_stale_console_nodes(tree: &mut FdtTree, mut paths: Vec<String>) {
    paths.sort_by_key(|path| core::cmp::Reverse(path.matches('/').count()));
    for path in paths {
        tree.inner_mut().remove_by_path(&path);
    }
}

fn patch_console_references(tree: &mut FdtTree, node_path: &str, base_gpa: usize) -> AxVmResult {
    let aliases = tree.ensure_path("/aliases")?;
    tree.set_property(aliases, prop_string("serial0", node_path))?;

    let chosen = tree.ensure_path("/chosen")?;
    let bootargs = tree
        .inner()
        .node(chosen)
        .and_then(|node| node.get_property("bootargs"))
        .and_then(|property| property.as_str())
        .unwrap_or_default();
    let bootargs = polling_bootargs(bootargs, base_gpa);
    tree.set_property(chosen, prop_string("bootargs", &bootargs))?;
    tree.set_property(chosen, prop_string("stdout-path", POLLING_STDOUT_PATH))?;
    tree.set_property(
        chosen,
        prop_string("linux,stdout-path", POLLING_STDOUT_PATH),
    )
}

fn polling_bootargs(existing: &str, base_gpa: usize) -> String {
    let mut tokens = existing
        .split_whitespace()
        .filter(|token| !token.starts_with("earlycon="))
        .filter(|token| !token.starts_with("console=ttyAMA"))
        .map(String::from)
        .collect::<Vec<_>>();
    tokens.push(format!("earlycon=pl011,mmio32,{base_gpa:#x}"));
    tokens.join(" ")
}

fn add_polling_console_node(tree: &mut FdtTree, console: &EmulatedDeviceConfig) -> AxVmResult {
    // Deliberately omit clocks, interrupts, and DMA. This node is an earlycon
    // contract, not proof that Linux's regular amba-pl011 tty can probe. The
    // locked Guest kernel must pass the boot-console handoff runtime gate.
    let root = tree.inner().root_id();
    let node_name = format!("pl011@{:x}", console.base_gpa);
    let node = tree.add_node(root, Node::new(&node_name));
    tree.set_property(
        node,
        prop_strings("compatible", &["arm,pl011", "arm,primecell"]),
    )?;
    tree.set_property(node, prop_u32("reg-io-width", 4))?;
    tree.set_property(node, prop_u32("current-speed", PL011_BAUD_RATE))?;
    tree.set_property(node, prop_string("status", "okay"))?;
    tree.inner_mut()
        .view_typed_mut(node)
        .ok_or_else(|| ax_err_type!(InvalidData, "polling PL011 node is missing"))?
        .set_regs(&[RegInfo::new(
            console.base_gpa as u64,
            Some(console.length as u64),
        )]);
    Ok(())
}

fn prop_u32(name: &str, value: u32) -> Property {
    let mut property = Property::new(name, Vec::new());
    property.set_u32_ls(&[value]);
    property
}

fn prop_strings(name: &str, values: &[&str]) -> Property {
    let mut property = Property::new(name, Vec::new());
    property.set_string_ls(values);
    property
}

#[cfg(test)]
mod tests {
    use alloc::{string::ToString, vec, vec::Vec};

    use axvm_types::{EmulatedDeviceConfig, EmulatedDeviceType};
    use axvmconfig::{AxVMCrateConfig, VMDevicesConfig};
    use fdt_edit::{Fdt, Node, Property};
    use fdt_raw::RegInfo;

    use super::install_vm_owned_polling_pl011;

    fn prop_u32(name: &str, value: u32) -> Property {
        let mut property = Property::new(name, vec![]);
        property.set_u32_ls(&[value]);
        property
    }

    fn base_fdt() -> Fdt {
        let mut fdt = Fdt::new();
        let root = fdt.root_id();
        fdt.node_mut(root)
            .unwrap()
            .set_property(prop_u32("#address-cells", 2));
        fdt.node_mut(root)
            .unwrap()
            .set_property(prop_u32("#size-cells", 2));
        fdt
    }

    fn console() -> EmulatedDeviceConfig {
        EmulatedDeviceConfig {
            name: "guest-pl011".to_string(),
            base_gpa: 0x0900_0000,
            length: 0x1000,
            irq_id: 0,
            emu_type: EmulatedDeviceType::Console,
            cfg_list: vec![],
        }
    }

    fn config(devices: Vec<EmulatedDeviceConfig>) -> AxVMCrateConfig {
        AxVMCrateConfig {
            devices: VMDevicesConfig {
                emu_devices: devices,
                ..Default::default()
            },
            ..Default::default()
        }
    }

    fn assert_polling_console(dtb: &[u8]) {
        let fdt = Fdt::from_bytes(dtb).unwrap();
        let node = fdt.get_by_path("/pl011@9000000").unwrap().as_node();
        assert_eq!(
            node.compatibles().collect::<Vec<_>>(),
            ["arm,pl011", "arm,primecell"]
        );
        assert_eq!(
            node.get_property("reg-io-width")
                .and_then(|prop| prop.get_u32()),
            Some(4)
        );
        assert!(node.get_property("interrupts").is_none());
        assert!(node.get_property("interrupts-extended").is_none());
        assert!(node.get_property("dmas").is_none());
        assert!(node.get_property("dma-names").is_none());

        let regs = fdt
            .view_typed(fdt.get_by_path_id("/pl011@9000000").unwrap())
            .unwrap()
            .regs();
        assert_eq!(regs.len(), 1);
        assert_eq!(regs[0].address, 0x0900_0000);
        assert_eq!(regs[0].size, Some(0x1000));
        let aliases = fdt.get_by_path("/aliases").unwrap().as_node();
        assert_eq!(
            aliases.get_property("serial0").unwrap().as_str(),
            Some("/pl011@9000000")
        );
        let chosen = fdt.get_by_path("/chosen").unwrap().as_node();
        assert_eq!(
            chosen.get_property("stdout-path").unwrap().as_str(),
            Some("serial0:115200n8")
        );
        assert!(
            chosen
                .get_property("bootargs")
                .unwrap()
                .as_str()
                .unwrap()
                .contains("earlycon=pl011,mmio32,0x9000000")
        );
    }

    #[test]
    fn no_console_config_preserves_the_tree() {
        let original = base_fdt().encode().as_ref().to_vec();
        assert_eq!(
            install_vm_owned_polling_pl011(&original, &config(vec![])).unwrap(),
            original
        );
    }

    #[test]
    fn rejects_duplicate_or_non_polling_console_configs() {
        let original = base_fdt().encode().as_ref().to_vec();
        assert!(
            install_vm_owned_polling_pl011(&original, &config(vec![console(), console()])).is_err()
        );

        let mut invalid = console();
        invalid.irq_id = 33;
        assert!(install_vm_owned_polling_pl011(&original, &config(vec![invalid])).is_err());
    }

    #[test]
    fn replaces_provided_physical_pl011_with_polling_node() {
        let mut fdt = base_fdt();
        let soc = fdt.add_node(fdt.root_id(), Node::new("soc"));
        fdt.node_mut(soc)
            .unwrap()
            .set_property(prop_u32("#address-cells", 2));
        fdt.node_mut(soc)
            .unwrap()
            .set_property(prop_u32("#size-cells", 2));
        let uart = fdt.add_node(soc, Node::new("uart@9000000"));
        let mut compatible = Property::new("compatible", vec![]);
        compatible.set_string_ls(&["arm,pl011", "arm,primecell"]);
        fdt.node_mut(uart).unwrap().set_property(compatible);
        fdt.view_typed_mut(uart)
            .unwrap()
            .set_regs(&[RegInfo::new(0x0900_0000, Some(0x1000))]);
        fdt.node_mut(uart)
            .unwrap()
            .set_property(prop_u32("interrupts", 33));
        fdt.node_mut(uart)
            .unwrap()
            .set_property(prop_u32("dmas", 1));

        let dtb = install_vm_owned_polling_pl011(fdt.encode().as_ref(), &config(vec![console()]))
            .unwrap();
        assert_polling_console(&dtb);
        let reparsed = Fdt::from_bytes(&dtb).unwrap();
        assert!(reparsed.get_by_path_id("/soc/uart@9000000").is_none());
    }

    #[test]
    fn same_gpa_non_pl011_node_fails_closed() {
        let mut fdt = base_fdt();
        let device = fdt.add_node(fdt.root_id(), Node::new("device@9000000"));
        let mut compatible = Property::new("compatible", vec![]);
        compatible.set_string("vendor,other-device");
        fdt.node_mut(device).unwrap().set_property(compatible);
        fdt.view_typed_mut(device)
            .unwrap()
            .set_regs(&[RegInfo::new(0x0900_0000, Some(0x1000))]);

        assert!(
            install_vm_owned_polling_pl011(fdt.encode().as_ref(), &config(vec![console()]),)
                .is_err()
        );
    }

    #[test]
    fn interior_gpa_non_pl011_node_without_size_fails_closed() {
        let mut fdt = base_fdt();
        let device = fdt.add_node(fdt.root_id(), Node::new("device@9000100"));
        let mut compatible = Property::new("compatible", vec![]);
        compatible.set_string("vendor,other-device");
        fdt.node_mut(device).unwrap().set_property(compatible);
        fdt.view_typed_mut(device)
            .unwrap()
            .set_regs(&[RegInfo::new(0x0900_0100, None)]);

        assert!(
            install_vm_owned_polling_pl011(fdt.encode().as_ref(), &config(vec![console()]),)
                .is_err()
        );
    }

    #[test]
    fn provided_console_does_not_become_a_passthrough_claim() {
        let mut fdt = base_fdt();
        let uart = fdt.add_node(fdt.root_id(), Node::new("uart@9000000"));
        let mut compatible = Property::new("compatible", vec![]);
        compatible.set_string("arm,pl011");
        fdt.node_mut(uart).unwrap().set_property(compatible);
        fdt.view_typed_mut(uart)
            .unwrap()
            .set_regs(&[RegInfo::new(0x0900_0000, Some(0x1000))]);
        let mut interrupts = Property::new("interrupts", vec![]);
        interrupts.set_u32_ls(&[0, 1, 4]);
        fdt.node_mut(uart).unwrap().set_property(interrupts);

        let config = config(vec![console()]);
        let dtb = install_vm_owned_polling_pl011(fdt.encode().as_ref(), &config).unwrap();
        let mut vm_config = crate::config::AxVMConfig::new(crate::config::AxVMConfigParams {
            id: 1,
            name: "linux".to_string(),
            emu_devices: config.devices.emu_devices.clone(),
            ..Default::default()
        });
        super::super::parser::parse_passthrough_devices_address(&mut vm_config, &config, &dtb)
            .unwrap();
        super::super::parser::parse_vm_interrupt(&mut vm_config, &dtb).unwrap();

        assert!(vm_config.pass_through_devices().is_empty());
        assert!(vm_config.pass_through_irqs().is_empty());
    }

    #[test]
    fn generated_tree_gets_polling_console_and_mmio32_earlycon() {
        let mut fdt = base_fdt();
        let chosen = fdt.add_node(fdt.root_id(), Node::new("chosen"));
        let mut bootargs = Property::new("bootargs", vec![]);
        bootargs
            .set_string("root=/dev/vda console=ttyAMA0 earlycon=pl011,0x9000000 fsck.repair=yes");
        fdt.node_mut(chosen).unwrap().set_property(bootargs);

        let dtb = install_vm_owned_polling_pl011(fdt.encode().as_ref(), &config(vec![console()]))
            .unwrap();
        assert_polling_console(&dtb);
        let reparsed = Fdt::from_bytes(&dtb).unwrap();
        let bootargs = reparsed
            .get_by_path("/chosen")
            .unwrap()
            .as_node()
            .get_property("bootargs")
            .unwrap()
            .as_str()
            .unwrap();
        assert!(!bootargs.contains("console=ttyAMA0"));
        assert_eq!(bootargs.matches("earlycon=").count(), 1);
    }

    #[test]
    fn reapplying_the_profile_is_idempotent() {
        let original = base_fdt().encode().as_ref().to_vec();
        let config = config(vec![console()]);
        let once = install_vm_owned_polling_pl011(&original, &config).unwrap();
        let twice = install_vm_owned_polling_pl011(&once, &config).unwrap();
        assert_eq!(twice, once);
    }
}
