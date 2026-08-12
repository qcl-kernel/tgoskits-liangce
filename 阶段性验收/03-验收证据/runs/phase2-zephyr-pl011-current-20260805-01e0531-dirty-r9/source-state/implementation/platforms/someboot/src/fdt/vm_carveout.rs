use core::ops::Range;

use fdt_raw::{Fdt, Node, Property};
use heapless::Vec;
use spin::Once;

use crate::mem::{MemoryDescriptor, MemoryType};

#[path = "vm_carveout_support.rs"]
mod support;
use support::{raw_string_list_contains, single_string, strict_region, valid_string_list};

pub const CARVEOUT_COMPATIBLE: &str = "axvisor,vm-carveout-v1";
pub const CARVEOUT_ALIGNMENT: usize = 2 * 1024 * 1024;
const MAX_VM_CARVEOUTS: usize = 32;
const MAX_RAM_RANGES: usize = 128;

/// A boot-validated, VM-owned host-physical memory carveout.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VmCarveout {
    pub vm_id: u32,
    pub physical_start: usize,
    pub size: usize,
}

impl VmCarveout {
    pub(crate) const fn memory_descriptor(self) -> MemoryDescriptor {
        MemoryDescriptor {
            physical_start: self.physical_start,
            size_in_bytes: self.size,
            memory_type: MemoryType::Reserved,
        }
    }
}

pub type VmCarveoutList = Vec<VmCarveout, MAX_VM_CARVEOUTS>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum VmCarveoutError {
    MissingReservedMemory,
    InvalidReservedMemory,
    DuplicateProperty(&'static str),
    MissingProperty(&'static str),
    InvalidCompatible,
    InvalidVmId,
    InvalidReg,
    ZeroSize,
    RangeOverflow,
    Unaligned,
    OutsideRam,
    DuplicateVmId(u32),
    Overlap,
    ForbiddenProperty,
    Disabled,
    UnitAddressMismatch,
    DynamicReserved,
    InvalidReservedRange,
    ReservedOverlap,
    TooManyCarveouts,
    TooManyRamRanges,
}

static VM_CARVEOUTS: Once<VmCarveoutList> = Once::new();

pub fn vm_carveouts() -> &'static [VmCarveout] {
    VM_CARVEOUTS.get().map_or(&[], Vec::as_slice)
}

pub(crate) fn publish_vm_carveouts(carveouts: VmCarveoutList) {
    VM_CARVEOUTS.call_once(|| carveouts);
}

pub(crate) fn parse_vm_carveouts_or_panic(fdt: Fdt<'_>) -> VmCarveoutList {
    parse_vm_carveouts(fdt)
        .unwrap_or_else(|error| panic!("invalid AxVisor VM carveout manifest: {error:?}"))
}

pub(crate) fn parse_vm_carveouts(fdt: Fdt<'_>) -> Result<VmCarveoutList, VmCarveoutError> {
    let has_carveout = fdt.reserved_memory().any(|node| is_vm_carveout_node(&node));
    if !has_carveout {
        return Ok(Vec::new());
    }

    let reserved = fdt
        .find_by_path("/reserved-memory")
        .ok_or(VmCarveoutError::MissingReservedMemory)?;
    validate_reserved_memory_parent(&reserved)?;

    let ram_ranges = collect_ram_ranges(&fdt)?;
    let mut carveouts = VmCarveoutList::new();
    for node in fdt.reserved_memory() {
        if !is_vm_carveout_node(&node) {
            continue;
        }
        if node.level() != reserved.level() + 1 {
            return Err(VmCarveoutError::InvalidReservedMemory);
        }
        let carveout = parse_carveout_node(&node, &ram_ranges)?;
        if carveouts.iter().any(|item| item.vm_id == carveout.vm_id) {
            return Err(VmCarveoutError::DuplicateVmId(carveout.vm_id));
        }
        carveouts
            .push(carveout)
            .map_err(|_| VmCarveoutError::TooManyCarveouts)?;
    }

    carveouts.sort_unstable_by_key(|item| item.physical_start);
    for pair in carveouts.windows(2) {
        let previous_end = pair[0]
            .physical_start
            .checked_add(pair[0].size)
            .ok_or(VmCarveoutError::RangeOverflow)?;
        if pair[1].physical_start < previous_end {
            return Err(VmCarveoutError::Overlap);
        }
    }
    validate_ordinary_reservations(&fdt, &reserved, &carveouts)?;
    Ok(carveouts)
}

pub(crate) fn is_vm_carveout_node(node: &Node<'_>) -> bool {
    node.properties().any(|property| {
        property.name() == "compatible"
            && raw_string_list_contains(property.as_slice(), CARVEOUT_COMPATIBLE.as_bytes())
    })
}

fn validate_reserved_memory_parent(node: &Node<'_>) -> Result<(), VmCarveoutError> {
    if unique_property(node, "#address-cells")?.and_then(|prop| prop.as_u32()) != Some(2)
        || unique_property(node, "#size-cells")?.and_then(|prop| prop.as_u32()) != Some(2)
        || !unique_property(node, "ranges")?.is_some_and(|prop| prop.is_empty())
    {
        return Err(VmCarveoutError::InvalidReservedMemory);
    }
    Ok(())
}

fn parse_carveout_node(
    node: &Node<'_>,
    ram_ranges: &[Range<usize>],
) -> Result<VmCarveout, VmCarveoutError> {
    let compatible = required_property(node, "compatible")?;
    if !valid_string_list(compatible.as_slice())
        || !compatible
            .as_str_iter()
            .any(|item| item == CARVEOUT_COMPATIBLE)
    {
        return Err(VmCarveoutError::InvalidCompatible);
    }

    for name in ["size", "alloc-ranges", "reusable", "no-map"] {
        if unique_property(node, name)?.is_some() {
            return Err(VmCarveoutError::ForbiddenProperty);
        }
    }

    if let Some(status) = unique_property(node, "status")?
        && single_string(&status).is_none_or(|value| !matches!(value, "okay" | "ok"))
    {
        return Err(VmCarveoutError::Disabled);
    }

    let vm_id = required_property(node, "axvisor,vm-id")?
        .as_u32()
        .filter(|&value| value != 0)
        .ok_or(VmCarveoutError::InvalidVmId)?;
    let reg_property = required_property(node, "reg")?;
    if reg_property.len() != 16 {
        return Err(VmCarveoutError::InvalidReg);
    }
    let mut regs = node.reg().ok_or(VmCarveoutError::InvalidReg)?;
    let reg = regs.next().ok_or(VmCarveoutError::InvalidReg)?;
    if regs.next().is_some() {
        return Err(VmCarveoutError::InvalidReg);
    }
    let size = reg.size.ok_or(VmCarveoutError::InvalidReg)?;
    if size == 0 {
        return Err(VmCarveoutError::ZeroSize);
    }
    reg.address
        .checked_add(size)
        .ok_or(VmCarveoutError::RangeOverflow)?;

    let unit_address = node
        .name()
        .rsplit_once('@')
        .filter(|(prefix, suffix)| !prefix.is_empty() && !suffix.is_empty())
        .and_then(|(_, suffix)| u64::from_str_radix(suffix, 16).ok())
        .ok_or(VmCarveoutError::UnitAddressMismatch)?;
    if unit_address != reg.address {
        return Err(VmCarveoutError::UnitAddressMismatch);
    }

    let range = strict_region(reg.address, size)?;
    if range.start % CARVEOUT_ALIGNMENT != 0 || range.len() % CARVEOUT_ALIGNMENT != 0 {
        return Err(VmCarveoutError::Unaligned);
    }
    if !ram_ranges
        .iter()
        .any(|ram| ram.start <= range.start && range.end <= ram.end)
    {
        return Err(VmCarveoutError::OutsideRam);
    }

    Ok(VmCarveout {
        vm_id,
        physical_start: range.start,
        size: range.len(),
    })
}

fn collect_ram_ranges(fdt: &Fdt<'_>) -> Result<Vec<Range<usize>, MAX_RAM_RANGES>, VmCarveoutError> {
    let mut ranges = Vec::new();
    for memory in fdt.memory() {
        for region in memory.regions() {
            let Ok(range) = strict_region(region.address, region.size) else {
                continue;
            };
            ranges
                .push(range)
                .map_err(|_| VmCarveoutError::TooManyRamRanges)?;
        }
    }
    Ok(ranges)
}

fn validate_ordinary_reservations(
    fdt: &Fdt<'_>,
    reserved_parent: &Node<'_>,
    carveouts: &[VmCarveout],
) -> Result<(), VmCarveoutError> {
    for reservation in fdt.memory_reservations() {
        if reservation.size == 0 {
            continue;
        }
        let range = strict_region(reservation.address, reservation.size)
            .map_err(|_| VmCarveoutError::InvalidReservedRange)?;
        reject_reserved_overlap(&range, carveouts)?;
    }

    for node in fdt.reserved_memory() {
        if is_vm_carveout_node(&node) {
            continue;
        }
        let reg = unique_property(&node, "reg")?;
        if reg.is_none()
            && (unique_property(&node, "size")?.is_some()
                || unique_property(&node, "alloc-ranges")?.is_some())
        {
            return Err(VmCarveoutError::DynamicReserved);
        }
        let Some(reg) = reg else {
            continue;
        };
        let tuple_size =
            usize::from(reserved_parent.address_cells + reserved_parent.size_cells) * 4;
        if tuple_size == 0 || reg.is_empty() || reg.len() % tuple_size != 0 {
            return Err(VmCarveoutError::InvalidReservedRange);
        }
        let mut parsed = node.reg().ok_or(VmCarveoutError::InvalidReservedRange)?;
        for _ in 0..(reg.len() / tuple_size) {
            let entry = parsed.next().ok_or(VmCarveoutError::InvalidReservedRange)?;
            let size = entry.size.ok_or(VmCarveoutError::InvalidReservedRange)?;
            if size == 0 {
                continue;
            }
            let range = strict_region(entry.address, size)
                .map_err(|_| VmCarveoutError::InvalidReservedRange)?;
            reject_reserved_overlap(&range, carveouts)?;
        }
        if parsed.next().is_some() {
            return Err(VmCarveoutError::InvalidReservedRange);
        }
    }
    Ok(())
}

fn reject_reserved_overlap(
    reserved: &Range<usize>,
    carveouts: &[VmCarveout],
) -> Result<(), VmCarveoutError> {
    for carveout in carveouts {
        let end = carveout
            .physical_start
            .checked_add(carveout.size)
            .ok_or(VmCarveoutError::RangeOverflow)?;
        if carveout.physical_start < reserved.end && reserved.start < end {
            return Err(VmCarveoutError::ReservedOverlap);
        }
    }
    Ok(())
}

fn required_property<'a>(
    node: &Node<'a>,
    name: &'static str,
) -> Result<Property<'a>, VmCarveoutError> {
    unique_property(node, name)?.ok_or(VmCarveoutError::MissingProperty(name))
}

fn unique_property<'a>(
    node: &Node<'a>,
    name: &'static str,
) -> Result<Option<Property<'a>>, VmCarveoutError> {
    let mut matches = node.properties().filter(|property| property.name() == name);
    let first = matches.next();
    if matches.next().is_some() {
        return Err(VmCarveoutError::DuplicateProperty(name));
    }
    Ok(first)
}

#[cfg(test)]
mod tests {
    use alloc::{format, vec::Vec};

    use fdt_edit::{Fdt, Node, NodeId, Property};

    use super::*;

    const RAM_START: u64 = 0x4000_0000;
    const RAM_SIZE: u64 = 0x2_0000_0000;
    const CARVEOUT_START: u64 = 0x1_0000_0000;
    const CARVEOUT_SIZE: u64 = 0x0800_0000;

    #[test]
    fn parses_valid_static_carveout() {
        let fdt = test_fdt(&[CarveoutSpec::default()], None);

        let carveouts = parse_encoded(&fdt).expect("valid carveout");

        assert_eq!(
            carveouts.as_slice(),
            &[VmCarveout {
                vm_id: 2,
                physical_start: CARVEOUT_START as usize,
                size: CARVEOUT_SIZE as usize,
            }]
        );
        let descriptor = carveouts[0].memory_descriptor();
        assert_eq!(descriptor.physical_start, CARVEOUT_START as usize);
        assert_eq!(descriptor.size_in_bytes, CARVEOUT_SIZE as usize);
        assert_eq!(descriptor.memory_type, crate::mem::MemoryType::Reserved);
    }

    #[test]
    fn ordinary_reserved_node_never_enters_manifest() {
        let mut fdt = test_fdt(&[CarveoutSpec::default()], None);
        add_ordinary_static(&mut fdt, 0x9000_0000, 0x20_0000);

        let carveouts = parse_encoded(&fdt).expect("ordinary reservation is separate");

        assert_eq!(carveouts.len(), 1);
        assert_eq!(carveouts[0].vm_id, 2);
    }

    #[test]
    fn tree_without_dedicated_carveout_returns_empty_manifest() {
        let mut fdt = test_fdt(&[], None);
        add_ordinary_dynamic(&mut fdt);

        assert!(
            parse_encoded(&fdt)
                .expect("legacy tree remains accepted")
                .is_empty()
        );
    }

    #[test]
    fn rejects_zero_vm_id() {
        let spec = CarveoutSpec {
            vm_id: 0,
            ..Default::default()
        };
        assert_eq!(parse_test(&[spec]), Err(VmCarveoutError::InvalidVmId));
    }

    #[test]
    fn rejects_duplicate_vm_id() {
        let second = CarveoutSpec {
            start: CARVEOUT_START + 2 * CARVEOUT_SIZE,
            ..Default::default()
        };
        assert_eq!(
            parse_test(&[CarveoutSpec::default(), second]),
            Err(VmCarveoutError::DuplicateVmId(2))
        );
    }

    #[test]
    fn rejects_multiple_reg_tuples() {
        let spec = CarveoutSpec {
            extra_reg: true,
            ..Default::default()
        };
        assert_eq!(parse_test(&[spec]), Err(VmCarveoutError::InvalidReg));
    }

    #[test]
    fn rejects_reg_without_complete_size() {
        let mut fdt = test_fdt(&[CarveoutSpec::default()], None);
        let node = find_node(&fdt, "vm-carveout@100000000");
        fdt.node_mut(node)
            .unwrap()
            .set_property(prop_u32s("reg", &[1, 0]));

        assert_eq!(parse_encoded(&fdt), Err(VmCarveoutError::InvalidReg));
    }

    #[test]
    #[should_panic(expected = "invalid AxVisor VM carveout manifest")]
    fn runtime_wrapper_panics_for_recognized_malformed_carveout() {
        let mut fdt = test_fdt(&[CarveoutSpec::default()], None);
        let node = find_node(&fdt, "vm-carveout@100000000");
        fdt.node_mut(node)
            .unwrap()
            .set_property(prop_u32s("reg", &[1, 0]));
        let encoded = fdt.encode();
        let raw = fdt_raw::Fdt::from_bytes(encoded.as_ref()).expect("parse generated fdt");

        parse_vm_carveouts_or_panic(raw);
    }

    #[test]
    fn rejects_zero_size_and_range_overflow() {
        let zero = CarveoutSpec {
            size: 0,
            ..Default::default()
        };
        assert_eq!(parse_test(&[zero]), Err(VmCarveoutError::ZeroSize));

        let overflow = CarveoutSpec {
            start: 0xffff_ffff_ffe0_0000,
            size: 0x40_0000,
            ..Default::default()
        };
        assert_eq!(parse_test(&[overflow]), Err(VmCarveoutError::RangeOverflow));
    }

    #[test]
    fn rejects_unaligned_base_or_size() {
        let base = CarveoutSpec {
            start: CARVEOUT_START + 0x1000,
            ..Default::default()
        };
        assert_eq!(parse_test(&[base]), Err(VmCarveoutError::Unaligned));

        let size = CarveoutSpec {
            size: CARVEOUT_SIZE + 0x1000,
            ..Default::default()
        };
        assert_eq!(parse_test(&[size]), Err(VmCarveoutError::Unaligned));
    }

    #[test]
    fn rejects_outside_or_crossing_ram() {
        let outside = CarveoutSpec {
            start: 0x3_0000_0000,
            ..Default::default()
        };
        assert_eq!(parse_test(&[outside]), Err(VmCarveoutError::OutsideRam));

        let crossing = CarveoutSpec {
            start: 0x7e00_0000,
            size: 0x0400_0000,
            ..Default::default()
        };
        let fdt = test_fdt(
            &[crossing],
            Some(&[(0x4000_0000, 0x4000_0000), (0x8000_0000, 0x4000_0000)]),
        );
        assert_eq!(parse_encoded(&fdt), Err(VmCarveoutError::OutsideRam));
    }

    #[test]
    fn rejects_overlapping_carveouts() {
        let first = CarveoutSpec {
            size: 0x1000_0000,
            ..Default::default()
        };
        let second = CarveoutSpec {
            vm_id: 3,
            start: CARVEOUT_START + CARVEOUT_SIZE,
            ..Default::default()
        };
        assert_eq!(parse_test(&[first, second]), Err(VmCarveoutError::Overlap));
    }

    #[test]
    fn rejects_reusable_no_map_and_dynamic_dedicated_nodes() {
        for forbidden in [
            Forbidden::Reusable,
            Forbidden::NoMap,
            Forbidden::Size,
            Forbidden::AllocRanges,
        ] {
            let spec = CarveoutSpec {
                forbidden: Some(forbidden),
                ..Default::default()
            };
            assert_eq!(parse_test(&[spec]), Err(VmCarveoutError::ForbiddenProperty));
        }
    }

    #[test]
    fn rejects_duplicate_critical_property() {
        let mut fdt = test_fdt(&[CarveoutSpec::default()], None);
        let node = find_node(&fdt, "vm-carveout@100000000");
        fdt.node_mut(node)
            .unwrap()
            .add_property(prop_u32s("axvisor,vm-id", &[3]));

        assert_eq!(
            parse_encoded(&fdt),
            Err(VmCarveoutError::DuplicateProperty("axvisor,vm-id"))
        );
    }

    #[test]
    fn rejects_duplicate_compatible_or_reg_property() {
        let mut duplicate_compatible = test_fdt(&[CarveoutSpec::default()], None);
        let node = find_node(&duplicate_compatible, "vm-carveout@100000000");
        duplicate_compatible
            .node_mut(node)
            .unwrap()
            .add_property(prop_strs("compatible", &[CARVEOUT_COMPATIBLE]));
        assert_eq!(
            parse_encoded(&duplicate_compatible),
            Err(VmCarveoutError::DuplicateProperty("compatible"))
        );

        let mut duplicate_reg = test_fdt(&[CarveoutSpec::default()], None);
        let node = find_node(&duplicate_reg, "vm-carveout@100000000");
        duplicate_reg
            .node_mut(node)
            .unwrap()
            .add_property(prop_reg(&[(CARVEOUT_START, CARVEOUT_SIZE)]));
        assert_eq!(
            parse_encoded(&duplicate_reg),
            Err(VmCarveoutError::DuplicateProperty("reg"))
        );
    }

    #[test]
    fn rejects_malformed_recognized_compatible() {
        let mut fdt = test_fdt(&[CarveoutSpec::default()], None);
        let node = find_node(&fdt, "vm-carveout@100000000");
        fdt.node_mut(node).unwrap().set_property(Property::new(
            "compatible",
            CARVEOUT_COMPATIBLE.as_bytes().to_vec(),
        ));

        assert_eq!(parse_encoded(&fdt), Err(VmCarveoutError::InvalidCompatible));
    }

    #[test]
    fn rejects_disabled_or_unit_address_mismatch() {
        let disabled = CarveoutSpec {
            disabled: true,
            ..Default::default()
        };
        assert_eq!(parse_test(&[disabled]), Err(VmCarveoutError::Disabled));

        let mut fdt = test_fdt(&[CarveoutSpec::default()], None);
        let node = find_node(&fdt, "vm-carveout@100000000");
        fdt.node_mut(node).unwrap().name = "vm-carveout@100200000".into();
        assert_eq!(
            parse_encoded(&fdt),
            Err(VmCarveoutError::UnitAddressMismatch)
        );
    }

    #[test]
    fn rejects_ordinary_dynamic_reservation_when_carveout_exists() {
        let mut fdt = test_fdt(&[CarveoutSpec::default()], None);
        add_ordinary_dynamic(&mut fdt);

        assert_eq!(parse_encoded(&fdt), Err(VmCarveoutError::DynamicReserved));
    }

    #[test]
    fn rejects_overlap_with_ordinary_reserved_or_memreserve() {
        let mut ordinary = test_fdt(&[CarveoutSpec::default()], None);
        add_ordinary_static(&mut ordinary, CARVEOUT_START, 0x20_0000);
        assert_eq!(
            parse_encoded(&ordinary),
            Err(VmCarveoutError::ReservedOverlap)
        );

        let mut memreserve = test_fdt(&[CarveoutSpec::default()], None);
        memreserve
            .memory_reservations
            .push(fdt_edit::MemoryReservation {
                address: CARVEOUT_START,
                size: 0x20_0000,
            });
        assert_eq!(
            parse_encoded(&memreserve),
            Err(VmCarveoutError::ReservedOverlap)
        );
    }

    #[test]
    fn checks_every_ordinary_reserved_reg_tuple_for_overlap() {
        let mut fdt = test_fdt(&[CarveoutSpec::default()], None);
        let reserved = find_node(&fdt, "reserved-memory");
        let ordinary = fdt.add_node(reserved, Node::new("ordinary@90000000"));
        fdt.node_mut(ordinary).unwrap().set_property(prop_reg(&[
            (0x9000_0000, 0x20_0000),
            (CARVEOUT_START, 0x20_0000),
        ]));

        assert_eq!(parse_encoded(&fdt), Err(VmCarveoutError::ReservedOverlap));
    }

    #[derive(Clone, Copy)]
    enum Forbidden {
        Reusable,
        NoMap,
        Size,
        AllocRanges,
    }

    #[derive(Clone, Copy)]
    struct CarveoutSpec {
        vm_id: u32,
        start: u64,
        size: u64,
        extra_reg: bool,
        forbidden: Option<Forbidden>,
        disabled: bool,
    }

    impl Default for CarveoutSpec {
        fn default() -> Self {
            Self {
                vm_id: 2,
                start: CARVEOUT_START,
                size: CARVEOUT_SIZE,
                extra_reg: false,
                forbidden: None,
                disabled: false,
            }
        }
    }

    fn parse_test(specs: &[CarveoutSpec]) -> Result<VmCarveoutList, VmCarveoutError> {
        parse_encoded(&test_fdt(specs, None))
    }

    fn parse_encoded(fdt: &Fdt) -> Result<VmCarveoutList, VmCarveoutError> {
        parse_bytes(fdt.encode().as_ref())
    }

    fn parse_bytes(bytes: &[u8]) -> Result<VmCarveoutList, VmCarveoutError> {
        let raw = fdt_raw::Fdt::from_bytes(bytes).expect("parse generated fdt");
        parse_vm_carveouts(raw)
    }

    fn test_fdt(specs: &[CarveoutSpec], rams: Option<&[(u64, u64)]>) -> Fdt {
        let mut fdt = Fdt::new();
        let root = fdt.root_id();
        fdt.node_mut(root)
            .unwrap()
            .set_property(prop_u32s("#address-cells", &[2]));
        fdt.node_mut(root)
            .unwrap()
            .set_property(prop_u32s("#size-cells", &[2]));

        for &(start, size) in rams.unwrap_or(&[(RAM_START, RAM_SIZE)]) {
            let memory = fdt.add_node(root, Node::new(&format!("memory@{start:x}")));
            fdt.node_mut(memory)
                .unwrap()
                .set_property(prop_strs("device_type", &["memory"]));
            fdt.node_mut(memory)
                .unwrap()
                .set_property(prop_reg(&[(start, size)]));
        }

        let reserved = fdt.add_node(root, Node::new("reserved-memory"));
        fdt.node_mut(reserved)
            .unwrap()
            .set_property(prop_u32s("#address-cells", &[2]));
        fdt.node_mut(reserved)
            .unwrap()
            .set_property(prop_u32s("#size-cells", &[2]));
        fdt.node_mut(reserved)
            .unwrap()
            .set_property(Property::new("ranges", Vec::new()));

        for spec in specs {
            add_carveout(&mut fdt, reserved, *spec);
        }
        fdt
    }

    fn add_carveout(fdt: &mut Fdt, reserved: NodeId, spec: CarveoutSpec) {
        let node = fdt.add_node(
            reserved,
            Node::new(&format!("vm-carveout@{:x}", spec.start)),
        );
        let compatible = prop_strs("compatible", &[CARVEOUT_COMPATIBLE]);
        fdt.node_mut(node).unwrap().set_property(compatible);
        fdt.node_mut(node)
            .unwrap()
            .set_property(prop_u32s("axvisor,vm-id", &[spec.vm_id]));
        let mut regs = vec![(spec.start, spec.size)];
        if spec.extra_reg {
            regs.push((spec.start + 2 * spec.size, spec.size));
        }
        fdt.node_mut(node).unwrap().set_property(prop_reg(&regs));
        if spec.disabled {
            fdt.node_mut(node)
                .unwrap()
                .set_property(prop_strs("status", &["disabled"]));
        }
        if let Some(forbidden) = spec.forbidden {
            let (name, data) = match forbidden {
                Forbidden::Reusable => ("reusable", Vec::new()),
                Forbidden::NoMap => ("no-map", Vec::new()),
                Forbidden::Size => ("size", vec![0, 0, 0x20, 0]),
                Forbidden::AllocRanges => ("alloc-ranges", vec![0; 16]),
            };
            fdt.node_mut(node)
                .unwrap()
                .set_property(Property::new(name, data));
        }
    }

    fn add_ordinary_static(fdt: &mut Fdt, start: u64, size: u64) {
        let reserved = find_node(fdt, "reserved-memory");
        let node = fdt.add_node(reserved, Node::new(&format!("ordinary@{start:x}")));
        fdt.node_mut(node)
            .unwrap()
            .set_property(prop_strs("compatible", &["example,ordinary"]));
        fdt.node_mut(node)
            .unwrap()
            .set_property(prop_reg(&[(start, size)]));
    }

    fn add_ordinary_dynamic(fdt: &mut Fdt) {
        let reserved = find_node(fdt, "reserved-memory");
        let node = fdt.add_node(reserved, Node::new("dynamic-pool"));
        fdt.node_mut(node)
            .unwrap()
            .set_property(prop_u32s("size", &[0, 0x20_0000]));
    }

    fn find_node(fdt: &Fdt, name: &str) -> NodeId {
        fdt.iter_node_ids()
            .find(|&id| fdt.node(id).is_some_and(|node| node.name() == name))
            .expect("test node")
    }

    fn prop_reg(ranges: &[(u64, u64)]) -> Property {
        let mut cells = Vec::new();
        for &(start, size) in ranges {
            cells.extend_from_slice(&(start >> 32).to_be_bytes()[4..]);
            cells.extend_from_slice(&(start as u32).to_be_bytes());
            cells.extend_from_slice(&(size >> 32).to_be_bytes()[4..]);
            cells.extend_from_slice(&(size as u32).to_be_bytes());
        }
        Property::new("reg", cells)
    }

    fn prop_u32s(name: &str, values: &[u32]) -> Property {
        let mut data = Vec::new();
        for value in values {
            data.extend_from_slice(&value.to_be_bytes());
        }
        Property::new(name, data)
    }

    fn prop_strs(name: &str, values: &[&str]) -> Property {
        let mut data = Vec::new();
        for value in values {
            data.extend_from_slice(value.as_bytes());
            data.push(0);
        }
        Property::new(name, data)
    }
}
