//! Small, allocation-free parsing helpers for VM carveout validation.

use core::ops::Range;

use fdt_raw::Property;

use super::VmCarveoutError;
use crate::ArchTrait;

pub(super) fn strict_region(address: u64, size: u64) -> Result<Range<usize>, VmCarveoutError> {
    if size == 0 {
        return Err(VmCarveoutError::ZeroSize);
    }
    address
        .checked_add(size)
        .ok_or(VmCarveoutError::RangeOverflow)?;
    let address = usize::try_from(address).map_err(|_| VmCarveoutError::RangeOverflow)?;
    let size = usize::try_from(size).map_err(|_| VmCarveoutError::RangeOverflow)?;
    let start = <crate::arch::Arch as ArchTrait>::canonicalize_paddr(address);
    let end = start
        .checked_add(size)
        .ok_or(VmCarveoutError::RangeOverflow)?;
    Ok(start..end)
}

pub(super) fn raw_string_list_contains(bytes: &[u8], expected: &[u8]) -> bool {
    bytes.split(|byte| *byte == 0).any(|item| item == expected)
}

pub(super) fn valid_string_list(bytes: &[u8]) -> bool {
    !bytes.is_empty()
        && bytes.last() == Some(&0)
        && bytes[..bytes.len() - 1]
            .split(|byte| *byte == 0)
            .all(|item| !item.is_empty() && core::str::from_utf8(item).is_ok())
}

pub(super) fn single_string<'a>(property: &Property<'a>) -> Option<&'a str> {
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
