//! Strict, fail-closed parsing for host DMA guard reservations.

use core::ops::Range;

use fdt_raw::{Fdt, Node, Property};
use heapless::Vec;
use spin::Once;

use crate::{
    ArchTrait,
    mem::{MemoryDescriptor, MemoryType},
};

pub const DMA_GUARD_COMPATIBLE: &str = "axvisor,dma-guard-v1";
pub const DMA_GUARD_ALIGNMENT: usize = 2 * 1024 * 1024;
const MAX_DMA_GUARDS: usize = 32;
const MAX_RAM_RANGES: usize = 128;

/// A boot-validated host-physical range reserved from host allocation.
///
/// This reservation supports controlled DMA-effect observations; it does not
/// by itself constrain bus-master DMA or prove DMA isolation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DmaGuard {
    pub physical_start: usize,
    pub size: usize,
}

impl DmaGuard {
    pub(crate) const fn memory_descriptor(self) -> MemoryDescriptor {
        MemoryDescriptor {
            physical_start: self.physical_start,
            size_in_bytes: self.size,
            memory_type: MemoryType::Reserved,
        }
    }
}

pub type DmaGuardList = Vec<DmaGuard, MAX_DMA_GUARDS>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DmaGuardError {
    MissingReservedMemory,
    InvalidReservedMemory,
    InvalidName,
    DuplicateProperty(&'static str),
    MissingProperty(&'static str),
    InvalidCompatible,
    InvalidReg,
    ZeroSize,
    RangeOverflow,
    Unaligned,
    OutsideRam,
    Overlap,
    ForbiddenProperty,
    MissingNoMap,
    InvalidNoMap,
    Disabled,
    UnitAddressMismatch,
    DynamicReserved,
    InvalidReservedRange,
    ReservedOverlap,
    TooManyGuards,
    TooManyRamRanges,
    InvalidRamRange,
}

static DMA_GUARDS: Once<DmaGuardList> = Once::new();

/// The immutable DMA guard manifest published during early memory setup.
pub fn dma_guards() -> &'static [DmaGuard] {
    DMA_GUARDS
        .get()
        .expect("DMA guard manifest requested before early memory setup")
        .as_slice()
}

pub(crate) fn publish_dma_guards(guards: DmaGuardList) {
    DMA_GUARDS.call_once(|| guards);
}

pub(crate) fn parse_dma_guards_or_panic(fdt: Fdt<'_>) -> DmaGuardList {
    parse_dma_guards(fdt)
        .unwrap_or_else(|error| panic!("invalid AxVisor host DMA guard manifest: {error:?}"))
}

pub(crate) fn parse_dma_guards(fdt: Fdt<'_>) -> Result<DmaGuardList, DmaGuardError> {
    let has_guard = fdt.reserved_memory().any(|node| is_dma_guard_node(&node));
    if !has_guard {
        return Ok(Vec::new());
    }

    let reserved = fdt
        .find_by_path("/reserved-memory")
        .ok_or(DmaGuardError::MissingReservedMemory)?;
    validate_reserved_memory_parent(&reserved)?;
    let ram_ranges = collect_ram_ranges(&fdt)?;
    let mut guards = DmaGuardList::new();
    for node in fdt.reserved_memory() {
        if !is_dma_guard_node(&node) {
            continue;
        }
        if node.level() != reserved.level() + 1 {
            return Err(DmaGuardError::InvalidReservedMemory);
        }
        guards
            .push(parse_guard_node(&node, &ram_ranges)?)
            .map_err(|_| DmaGuardError::TooManyGuards)?;
    }
    guards.sort_unstable_by_key(|guard| guard.physical_start);
    for pair in guards.windows(2) {
        let previous_end = pair[0]
            .physical_start
            .checked_add(pair[0].size)
            .ok_or(DmaGuardError::RangeOverflow)?;
        if pair[1].physical_start < previous_end {
            return Err(DmaGuardError::Overlap);
        }
    }
    validate_other_reservations(&fdt, &reserved, &guards)?;
    Ok(guards)
}

/// Identify every node that claims the DMA guard namespace, even if malformed.
pub(crate) fn is_dma_guard_node(node: &Node<'_>) -> bool {
    node.name() == "dma-guard"
        || node.name().starts_with("dma-guard@")
        || node.properties().any(|property| {
            property.name() == "compatible"
                && raw_string_list_contains(property.as_slice(), DMA_GUARD_COMPATIBLE.as_bytes())
        })
}

fn validate_reserved_memory_parent(node: &Node<'_>) -> Result<(), DmaGuardError> {
    if unique_property(node, "#address-cells")?.and_then(|prop| prop.as_u32()) != Some(2)
        || unique_property(node, "#size-cells")?.and_then(|prop| prop.as_u32()) != Some(2)
        || !unique_property(node, "ranges")?.is_some_and(|prop| prop.is_empty())
    {
        return Err(DmaGuardError::InvalidReservedMemory);
    }
    Ok(())
}

fn parse_guard_node(
    node: &Node<'_>,
    ram_ranges: &[Range<usize>],
) -> Result<DmaGuard, DmaGuardError> {
    if !node.name().starts_with("dma-guard@") {
        return Err(DmaGuardError::InvalidName);
    }
    let compatible = required_property(node, "compatible")?;
    if single_string(&compatible) != Some(DMA_GUARD_COMPATIBLE) {
        return Err(DmaGuardError::InvalidCompatible);
    }
    let no_map = unique_property(node, "no-map")?.ok_or(DmaGuardError::MissingNoMap)?;
    if !no_map.is_empty() {
        return Err(DmaGuardError::InvalidNoMap);
    }
    for name in ["axvisor,vm-id", "reusable", "size", "alloc-ranges"] {
        if unique_property(node, name)?.is_some() {
            return Err(DmaGuardError::ForbiddenProperty);
        }
    }
    if let Some(status) = unique_property(node, "status")?
        && single_string(&status).is_none_or(|value| !matches!(value, "okay" | "ok"))
    {
        return Err(DmaGuardError::Disabled);
    }

    let reg_property = required_property(node, "reg")?;
    if reg_property.len() != 16 {
        return Err(DmaGuardError::InvalidReg);
    }
    let mut regs = node.reg().ok_or(DmaGuardError::InvalidReg)?;
    let reg = regs.next().ok_or(DmaGuardError::InvalidReg)?;
    if regs.next().is_some() {
        return Err(DmaGuardError::InvalidReg);
    }
    let size = reg.size.ok_or(DmaGuardError::InvalidReg)?;
    if size == 0 {
        return Err(DmaGuardError::ZeroSize);
    }
    reg.address
        .checked_add(size)
        .ok_or(DmaGuardError::RangeOverflow)?;
    let unit_address = node
        .name()
        .strip_prefix("dma-guard@")
        .filter(|suffix| !suffix.is_empty())
        .and_then(|suffix| u64::from_str_radix(suffix, 16).ok())
        .ok_or(DmaGuardError::UnitAddressMismatch)?;
    if unit_address != reg.address {
        return Err(DmaGuardError::UnitAddressMismatch);
    }
    let range = strict_region(reg.address, size)?;
    if range.start % DMA_GUARD_ALIGNMENT != 0 || range.len() % DMA_GUARD_ALIGNMENT != 0 {
        return Err(DmaGuardError::Unaligned);
    }
    if !ram_ranges
        .iter()
        .any(|ram| ram.start <= range.start && range.end <= ram.end)
    {
        return Err(DmaGuardError::OutsideRam);
    }
    Ok(DmaGuard {
        physical_start: range.start,
        size: range.len(),
    })
}

fn collect_ram_ranges(fdt: &Fdt<'_>) -> Result<Vec<Range<usize>, MAX_RAM_RANGES>, DmaGuardError> {
    let mut ranges = Vec::new();
    for memory in fdt.memory() {
        for region in memory.regions() {
            let range = strict_region(region.address, region.size)
                .map_err(|_| DmaGuardError::InvalidRamRange)?;
            ranges
                .push(range)
                .map_err(|_| DmaGuardError::TooManyRamRanges)?;
        }
    }
    Ok(ranges)
}

fn validate_other_reservations(
    fdt: &Fdt<'_>,
    reserved_parent: &Node<'_>,
    guards: &[DmaGuard],
) -> Result<(), DmaGuardError> {
    for reservation in fdt.memory_reservations() {
        if reservation.size != 0 {
            reject_reserved_overlap(
                &strict_region(reservation.address, reservation.size)?,
                guards,
            )?;
        }
    }
    for node in fdt.reserved_memory() {
        if is_dma_guard_node(&node) {
            continue;
        }
        let reg = unique_property(&node, "reg")?;
        if reg.is_none()
            && (unique_property(&node, "size")?.is_some()
                || unique_property(&node, "alloc-ranges")?.is_some())
        {
            return Err(DmaGuardError::DynamicReserved);
        }
        let Some(reg) = reg else {
            continue;
        };
        let tuple_size =
            usize::from(reserved_parent.address_cells + reserved_parent.size_cells) * 4;
        if tuple_size == 0 || reg.is_empty() || reg.len() % tuple_size != 0 {
            return Err(DmaGuardError::InvalidReservedRange);
        }
        let mut parsed = node.reg().ok_or(DmaGuardError::InvalidReservedRange)?;
        for _ in 0..(reg.len() / tuple_size) {
            let entry = parsed.next().ok_or(DmaGuardError::InvalidReservedRange)?;
            let size = entry.size.ok_or(DmaGuardError::InvalidReservedRange)?;
            if size != 0 {
                reject_reserved_overlap(&strict_region(entry.address, size)?, guards)?;
            }
        }
        if parsed.next().is_some() {
            return Err(DmaGuardError::InvalidReservedRange);
        }
    }
    Ok(())
}

fn reject_reserved_overlap(
    reserved: &Range<usize>,
    guards: &[DmaGuard],
) -> Result<(), DmaGuardError> {
    for guard in guards {
        let end = guard
            .physical_start
            .checked_add(guard.size)
            .ok_or(DmaGuardError::RangeOverflow)?;
        if guard.physical_start < reserved.end && reserved.start < end {
            return Err(DmaGuardError::ReservedOverlap);
        }
    }
    Ok(())
}

fn strict_region(address: u64, size: u64) -> Result<Range<usize>, DmaGuardError> {
    if size == 0 {
        return Err(DmaGuardError::ZeroSize);
    }
    address
        .checked_add(size)
        .ok_or(DmaGuardError::RangeOverflow)?;
    let address = usize::try_from(address).map_err(|_| DmaGuardError::RangeOverflow)?;
    let size = usize::try_from(size).map_err(|_| DmaGuardError::RangeOverflow)?;
    let start = <crate::arch::Arch as ArchTrait>::canonicalize_paddr(address);
    let end = start
        .checked_add(size)
        .ok_or(DmaGuardError::RangeOverflow)?;
    Ok(start..end)
}

fn raw_string_list_contains(bytes: &[u8], expected: &[u8]) -> bool {
    bytes.split(|byte| *byte == 0).any(|item| item == expected)
}

fn valid_string_list(bytes: &[u8]) -> bool {
    !bytes.is_empty()
        && bytes.last() == Some(&0)
        && bytes[..bytes.len() - 1]
            .split(|byte| *byte == 0)
            .all(|item| !item.is_empty() && core::str::from_utf8(item).is_ok())
}

fn single_string<'a>(property: &Property<'a>) -> Option<&'a str> {
    if !valid_string_list(property.as_slice()) {
        return None;
    }
    let mut strings = property.as_str_iter();
    let value = strings.next()?;
    if strings.next().is_some() {
        return None;
    }
    Some(value)
}

fn required_property<'a>(
    node: &Node<'a>,
    name: &'static str,
) -> Result<Property<'a>, DmaGuardError> {
    unique_property(node, name)?.ok_or(DmaGuardError::MissingProperty(name))
}

fn unique_property<'a>(
    node: &Node<'a>,
    name: &'static str,
) -> Result<Option<Property<'a>>, DmaGuardError> {
    let mut matches = node.properties().filter(|property| property.name() == name);
    let first = matches.next();
    if matches.next().is_some() {
        return Err(DmaGuardError::DuplicateProperty(name));
    }
    Ok(first)
}

#[cfg(test)]
mod tests {
    use alloc::{format, vec, vec::Vec};

    use fdt_edit::{Fdt, Node, NodeId, Property};

    use super::*;

    const RAM_START: u64 = 0x4000_0000;
    const RAM_SIZE: u64 = 0x2_0000_0000;
    const GUARD_START: u64 = 0x1_0000_0000;
    const GUARD_SIZE: u64 = 0x20_0000;

    #[test]
    fn parses_happy_path_and_none_for_legacy_tree() {
        let guards = parse_encoded(&test_fdt(&[GuardSpec::default()], None)).unwrap();
        assert_eq!(
            guards.as_slice(),
            &[DmaGuard {
                physical_start: GUARD_START as usize,
                size: GUARD_SIZE as usize,
            }]
        );
        assert!(parse_encoded(&test_fdt(&[], None)).unwrap().is_empty());
    }

    #[test]
    #[should_panic(expected = "DMA guard manifest requested before early memory setup")]
    fn manifest_access_before_early_memory_setup_fails_closed() {
        dma_guards();
    }

    #[test]
    fn name_or_compatible_confusion_fails_closed() {
        let mut name_only = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&name_only, "dma-guard@100000000");
        name_only
            .node_mut(node)
            .unwrap()
            .set_property(prop_strs("compatible", &["example,other"]));
        assert_eq!(
            parse_encoded(&name_only),
            Err(DmaGuardError::InvalidCompatible)
        );

        let mut compatible_only = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&compatible_only, "dma-guard@100000000");
        compatible_only.node_mut(node).unwrap().name = "ordinary@100000000".into();
        assert_eq!(
            parse_encoded(&compatible_only),
            Err(DmaGuardError::InvalidName)
        );

        let mut bare_name = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&bare_name, "dma-guard@100000000");
        bare_name.node_mut(node).unwrap().name = "dma-guard".into();
        assert_eq!(parse_encoded(&bare_name), Err(DmaGuardError::InvalidName));

        let mut malformed_compatible_only = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&malformed_compatible_only, "dma-guard@100000000");
        let node = malformed_compatible_only.node_mut(node).unwrap();
        node.name = "ordinary@100000000".into();
        node.set_property(Property::new(
            "compatible",
            DMA_GUARD_COMPATIBLE.as_bytes().to_vec(),
        ));
        assert_eq!(
            parse_encoded(&malformed_compatible_only),
            Err(DmaGuardError::InvalidName)
        );
    }

    #[test]
    fn requires_empty_no_map_and_rejects_forbidden_properties() {
        let missing = test_fdt(
            &[GuardSpec {
                no_map: false,
                ..Default::default()
            }],
            None,
        );
        assert_eq!(parse_encoded(&missing), Err(DmaGuardError::MissingNoMap));

        let mut nonempty = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&nonempty, "dma-guard@100000000");
        nonempty
            .node_mut(node)
            .unwrap()
            .set_property(Property::new("no-map", vec![0]));
        assert_eq!(parse_encoded(&nonempty), Err(DmaGuardError::InvalidNoMap));

        let mut duplicate = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&duplicate, "dma-guard@100000000");
        duplicate
            .node_mut(node)
            .unwrap()
            .add_property(Property::new("no-map", Vec::new()));
        assert_eq!(
            parse_encoded(&duplicate),
            Err(DmaGuardError::DuplicateProperty("no-map"))
        );

        for name in ["axvisor,vm-id", "reusable", "size", "alloc-ranges"] {
            let mut fdt = test_fdt(&[GuardSpec::default()], None);
            let node = find_node(&fdt, "dma-guard@100000000");
            fdt.node_mut(node)
                .unwrap()
                .set_property(Property::new(name, vec![0]));
            assert_eq!(parse_encoded(&fdt), Err(DmaGuardError::ForbiddenProperty));
        }
    }

    #[test]
    fn rejects_disabled_or_malformed_status() {
        let mut disabled = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&disabled, "dma-guard@100000000");
        disabled
            .node_mut(node)
            .unwrap()
            .set_property(prop_strs("status", &["disabled"]));
        assert_eq!(parse_encoded(&disabled), Err(DmaGuardError::Disabled));

        let mut malformed = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&malformed, "dma-guard@100000000");
        malformed
            .node_mut(node)
            .unwrap()
            .set_property(Property::new("status", b"okay".to_vec()));
        assert_eq!(parse_encoded(&malformed), Err(DmaGuardError::Disabled));
    }

    #[test]
    fn rejects_non_exact_or_duplicate_compatible() {
        let mut multiple = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&multiple, "dma-guard@100000000");
        multiple.node_mut(node).unwrap().set_property(prop_strs(
            "compatible",
            &[DMA_GUARD_COMPATIBLE, "example,other"],
        ));
        assert_eq!(
            parse_encoded(&multiple),
            Err(DmaGuardError::InvalidCompatible)
        );

        let mut malformed = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&malformed, "dma-guard@100000000");
        malformed
            .node_mut(node)
            .unwrap()
            .set_property(Property::new(
                "compatible",
                DMA_GUARD_COMPATIBLE.as_bytes().to_vec(),
            ));
        assert_eq!(
            parse_encoded(&malformed),
            Err(DmaGuardError::InvalidCompatible)
        );

        let mut duplicate = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&duplicate, "dma-guard@100000000");
        duplicate
            .node_mut(node)
            .unwrap()
            .add_property(prop_strs("compatible", &[DMA_GUARD_COMPATIBLE]));
        assert_eq!(
            parse_encoded(&duplicate),
            Err(DmaGuardError::DuplicateProperty("compatible"))
        );
    }

    #[test]
    fn rejects_non_direct_alignment_ram_and_overflow_failures() {
        let mut nested = test_fdt(&[], None);
        let reserved = find_node(&nested, "reserved-memory");
        let parent = nested.add_node(reserved, Node::new("container"));
        add_guard(&mut nested, parent, GuardSpec::default());
        assert_eq!(
            parse_encoded(&nested),
            Err(DmaGuardError::InvalidReservedMemory)
        );

        assert_eq!(
            parse_encoded(&test_fdt(
                &[GuardSpec {
                    start: GUARD_START + 0x1000,
                    ..Default::default()
                }],
                None,
            )),
            Err(DmaGuardError::Unaligned)
        );
        assert_eq!(
            parse_encoded(&test_fdt(
                &[GuardSpec {
                    start: 0x3_0000_0000,
                    ..Default::default()
                }],
                None,
            )),
            Err(DmaGuardError::OutsideRam)
        );
        assert_eq!(
            parse_encoded(&test_fdt(
                &[GuardSpec {
                    start: 0xffff_ffff_ffe0_0000,
                    size: 0x20_0000,
                    ..Default::default()
                }],
                None,
            )),
            Err(DmaGuardError::RangeOverflow)
        );
    }

    #[test]
    fn rejects_unit_address_zero_size_and_unaligned_size() {
        let mut unit_mismatch = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&unit_mismatch, "dma-guard@100000000");
        unit_mismatch.node_mut(node).unwrap().name = "dma-guard@100200000".into();
        assert_eq!(
            parse_encoded(&unit_mismatch),
            Err(DmaGuardError::UnitAddressMismatch)
        );

        assert_eq!(
            parse_encoded(&test_fdt(
                &[GuardSpec {
                    size: 0,
                    ..Default::default()
                }],
                None,
            )),
            Err(DmaGuardError::ZeroSize)
        );
        assert_eq!(
            parse_encoded(&test_fdt(
                &[GuardSpec {
                    size: GUARD_SIZE + 0x1000,
                    ..Default::default()
                }],
                None,
            )),
            Err(DmaGuardError::Unaligned)
        );
    }

    #[test]
    fn rejects_guard_crossing_adjacent_ram_ranges() {
        let guard = GuardSpec {
            start: 0x7fe0_0000,
            size: 0x40_0000,
            ..Default::default()
        };
        let fdt = test_fdt(
            &[guard],
            Some(&[(0x4000_0000, 0x4000_0000), (0x8000_0000, 0x4000_0000)]),
        );

        assert_eq!(parse_encoded(&fdt), Err(DmaGuardError::OutsideRam));
    }

    #[test]
    fn rejects_guard_duplicate_overlap_memreserve_and_vm_carveout_overlap() {
        let duplicate = [
            GuardSpec::default(),
            GuardSpec {
                start: GUARD_START,
                ..Default::default()
            },
        ];
        assert_eq!(
            parse_encoded(&test_fdt(&duplicate, None)),
            Err(DmaGuardError::Overlap)
        );

        let mut memreserve = test_fdt(&[GuardSpec::default()], None);
        memreserve
            .memory_reservations
            .push(fdt_edit::MemoryReservation {
                address: GUARD_START,
                size: GUARD_SIZE,
            });
        assert_eq!(
            parse_encoded(&memreserve),
            Err(DmaGuardError::ReservedOverlap)
        );

        let mut vm_carveout = test_fdt(&[GuardSpec::default()], None);
        add_other_reserved(
            &mut vm_carveout,
            "vm-carveout@100000000",
            GUARD_START,
            GUARD_SIZE,
        );
        assert_eq!(
            parse_encoded(&vm_carveout),
            Err(DmaGuardError::ReservedOverlap)
        );
    }

    #[test]
    fn rejects_dynamic_ordinary_reservation_when_guard_exists() {
        let mut fdt = test_fdt(&[GuardSpec::default()], None);
        let reserved = find_node(&fdt, "reserved-memory");
        let dynamic = fdt.add_node(reserved, Node::new("dynamic-pool"));
        fdt.node_mut(dynamic)
            .unwrap()
            .set_property(prop_u32s("size", &[0, 0x20_0000]));

        assert_eq!(parse_encoded(&fdt), Err(DmaGuardError::DynamicReserved));
    }

    #[test]
    fn rejects_malformed_secondary_ram_range() {
        let mut fdt = test_fdt(&[GuardSpec::default()], None);
        let root = fdt.root_id();
        let memory = fdt.add_node(root, Node::new("memory@ffffffffffe00000"));
        fdt.node_mut(memory)
            .unwrap()
            .set_property(prop_strs("device_type", &["memory"]));
        fdt.node_mut(memory)
            .unwrap()
            .set_property(prop_reg(&[(0xffff_ffff_ffe0_0000, 0x40_0000)]));

        assert_eq!(parse_encoded(&fdt), Err(DmaGuardError::InvalidRamRange));
    }

    #[test]
    fn capacity_and_property_duplicates_are_rejected() {
        let specs: Vec<_> = (0..33)
            .map(|index| GuardSpec {
                start: GUARD_START + index * GUARD_SIZE,
                ..Default::default()
            })
            .collect();
        assert_eq!(
            parse_encoded(&test_fdt(&specs, None)),
            Err(DmaGuardError::TooManyGuards)
        );

        let mut fdt = test_fdt(&[GuardSpec::default()], None);
        let node = find_node(&fdt, "dma-guard@100000000");
        fdt.node_mut(node)
            .unwrap()
            .add_property(prop_reg(&[(GUARD_START, GUARD_SIZE)]));
        assert_eq!(
            parse_encoded(&fdt),
            Err(DmaGuardError::DuplicateProperty("reg"))
        );
    }

    #[derive(Clone, Copy)]
    struct GuardSpec {
        start: u64,
        size: u64,
        no_map: bool,
    }
    impl Default for GuardSpec {
        fn default() -> Self {
            Self {
                start: GUARD_START,
                size: GUARD_SIZE,
                no_map: true,
            }
        }
    }

    fn parse_encoded(fdt: &Fdt) -> Result<DmaGuardList, DmaGuardError> {
        parse_dma_guards(fdt_raw::Fdt::from_bytes(fdt.encode().as_ref()).unwrap())
    }

    fn test_fdt(specs: &[GuardSpec], rams: Option<&[(u64, u64)]>) -> Fdt {
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
        for &spec in specs {
            add_guard(&mut fdt, reserved, spec);
        }
        fdt
    }

    fn add_guard(fdt: &mut Fdt, reserved: NodeId, spec: GuardSpec) {
        let node = fdt.add_node(reserved, Node::new(&format!("dma-guard@{:x}", spec.start)));
        fdt.node_mut(node)
            .unwrap()
            .set_property(prop_strs("compatible", &[DMA_GUARD_COMPATIBLE]));
        fdt.node_mut(node)
            .unwrap()
            .set_property(prop_reg(&[(spec.start, spec.size)]));
        if spec.no_map {
            fdt.node_mut(node)
                .unwrap()
                .set_property(Property::new("no-map", Vec::new()));
        }
    }

    fn add_other_reserved(fdt: &mut Fdt, name: &str, start: u64, size: u64) {
        let reserved = find_node(fdt, "reserved-memory");
        let node = fdt.add_node(reserved, Node::new(name));
        fdt.node_mut(node)
            .unwrap()
            .set_property(prop_reg(&[(start, size)]));
    }

    fn find_node(fdt: &Fdt, name: &str) -> NodeId {
        fdt.iter_node_ids()
            .find(|&id| fdt.node(id).is_some_and(|node| node.name() == name))
            .unwrap()
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
