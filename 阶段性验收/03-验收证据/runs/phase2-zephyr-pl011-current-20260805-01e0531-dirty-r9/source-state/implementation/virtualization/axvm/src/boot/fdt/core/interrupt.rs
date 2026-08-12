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

//! Fail-closed validation for guest interrupt references.

use alloc::{
    collections::{BTreeMap, BTreeSet},
    format,
    string::String,
    vec::Vec,
};

use fdt_edit::{Fdt, Property};

use crate::{AxVmResult, ax_err_type};

#[derive(Default)]
struct PhandleIndex {
    owners: BTreeMap<u32, String>,
    ambiguous: BTreeSet<u32>,
}

impl PhandleIndex {
    fn from_fdt(fdt: &Fdt) -> Self {
        let mut index = Self::default();

        for node_id in fdt.iter_node_ids() {
            let Some(node) = fdt.node(node_id) else {
                continue;
            };
            let path = fdt.path_of(node_id);
            let mut node_phandles = BTreeSet::new();
            let mut malformed_definition = false;
            for property_name in ["phandle", "linux,phandle"] {
                let Some(property) = node.get_property(property_name) else {
                    continue;
                };
                if let Some(phandle) = valid_phandle(property) {
                    node_phandles.insert(phandle);
                } else {
                    malformed_definition = true;
                }
            }
            if malformed_definition || node_phandles.len() > 1 {
                index.ambiguous.extend(node_phandles.iter().copied());
            }
            for phandle in node_phandles {
                if index
                    .owners
                    .get(&phandle)
                    .is_some_and(|owner| owner != &path)
                {
                    index.ambiguous.insert(phandle);
                } else {
                    index.owners.insert(phandle, path.clone());
                }
            }
        }

        index
    }

    fn controller_cells(
        &self,
        fdt: &Fdt,
        phandle: u32,
        source_path: &str,
        property_name: &str,
    ) -> AxVmResult<usize> {
        if self.ambiguous.contains(&phandle) {
            return Err(ax_err_type!(
                InvalidData,
                format!(
                    "FDT node {source_path} property {property_name} references ambiguous phandle \
                     {phandle:#x}"
                )
            ));
        }
        let controller_path = self.owners.get(&phandle).ok_or_else(|| {
            ax_err_type!(
                InvalidData,
                format!(
                    "FDT node {source_path} property {property_name} references missing phandle \
                     {phandle:#x}"
                )
            )
        })?;
        let controller = fdt
            .get_by_path(controller_path)
            .ok_or_else(|| {
                ax_err_type!(
                    InvalidData,
                    format!("FDT interrupt controller node {controller_path} is missing")
                )
            })?
            .as_node();
        if controller.get_property("interrupt-controller").is_none() {
            return Err(ax_err_type!(
                InvalidData,
                format!(
                    "FDT node {source_path} property {property_name} references non-controller \
                     node {controller_path}"
                )
            ));
        }
        let interrupt_cells = controller
            .get_property("#interrupt-cells")
            .and_then(single_u32)
            .ok_or_else(|| {
                ax_err_type!(
                    InvalidData,
                    format!(
                        "FDT interrupt controller {controller_path} has invalid #interrupt-cells"
                    )
                )
            })?;
        Ok(interrupt_cells as usize)
    }
}

pub(crate) fn validate_interrupt_references(fdt: &Fdt) -> AxVmResult {
    let phandles = PhandleIndex::from_fdt(fdt);

    for node_id in fdt.iter_node_ids() {
        let Some(node) = fdt.node(node_id) else {
            continue;
        };
        let node_path = fdt.path_of(node_id);

        if let Some(property) = node.get_property("interrupt-parent") {
            let phandle = parse_interrupt_phandle(property, &node_path, "interrupt-parent")?;
            phandles.controller_cells(fdt, phandle, &node_path, "interrupt-parent")?;
        }
        if let Some(property) = node.get_property("interrupts") {
            validate_interrupts(fdt, &phandles, &node_path, property)?;
        }
        if let Some(property) = node.get_property("interrupts-extended") {
            validate_interrupts_extended(fdt, &phandles, &node_path, property)?;
        }
    }

    Ok(())
}

fn validate_interrupts(
    fdt: &Fdt,
    phandles: &PhandleIndex,
    node_path: &str,
    property: &Property,
) -> AxVmResult {
    let phandle = effective_interrupt_parent(fdt, node_path)?.ok_or_else(|| {
        ax_err_type!(
            InvalidData,
            format!("FDT node {node_path} has interrupts but no interrupt-parent")
        )
    })?;
    let cells = phandles.controller_cells(fdt, phandle, node_path, "interrupts")?;
    let byte_width = cells
        .checked_mul(core::mem::size_of::<u32>())
        .ok_or_else(|| {
            ax_err_type!(
                InvalidData,
                format!("FDT node {node_path} interrupt cell width overflows")
            )
        })?;

    if byte_width == 0 {
        if property.data.is_empty() {
            return Ok(());
        }
    } else if property.data.len().is_multiple_of(byte_width) {
        return Ok(());
    }

    Err(ax_err_type!(
        InvalidData,
        format!(
            "FDT node {node_path} interrupts length {} does not match #interrupt-cells {cells}",
            property.data.len()
        )
    ))
}

fn validate_interrupts_extended(
    fdt: &Fdt,
    phandles: &PhandleIndex,
    node_path: &str,
    property: &Property,
) -> AxVmResult {
    if property.data.is_empty() {
        return Err(ax_err_type!(
            InvalidData,
            format!("FDT node {node_path} has empty interrupts-extended")
        ));
    }
    if !property
        .data
        .len()
        .is_multiple_of(core::mem::size_of::<u32>())
    {
        return Err(ax_err_type!(
            InvalidData,
            format!("FDT node {node_path} has malformed interrupts-extended bytes")
        ));
    }

    let words = property
        .data
        .as_chunks::<{ core::mem::size_of::<u32>() }>()
        .0
        .iter()
        .map(|chunk| u32::from_be_bytes(*chunk))
        .collect::<Vec<_>>();
    let mut cursor = 0;
    while cursor < words.len() {
        let phandle = words[cursor];
        cursor += 1;
        let cells = phandles.controller_cells(fdt, phandle, node_path, "interrupts-extended")?;
        let end = cursor
            .checked_add(cells)
            .filter(|end| *end <= words.len())
            .ok_or_else(|| {
                ax_err_type!(
                    InvalidData,
                    format!(
                        "FDT node {node_path} interrupts-extended entry for {phandle:#x} is \
                         truncated"
                    )
                )
            })?;
        cursor = end;
    }

    Ok(())
}

fn effective_interrupt_parent(fdt: &Fdt, node_path: &str) -> AxVmResult<Option<u32>> {
    let mut current_path = Some(node_path);
    while let Some(path) = current_path {
        let node_id = fdt.get_by_path_id(path).ok_or_else(|| {
            ax_err_type!(
                InvalidData,
                format!("FDT node path disappeared during interrupt validation: {path}")
            )
        })?;
        if let Some(property) = fdt
            .node(node_id)
            .and_then(|node| node.get_property("interrupt-parent"))
        {
            return parse_interrupt_phandle(property, path, "interrupt-parent").map(Some);
        }
        current_path = parent_path(path);
    }
    Ok(None)
}

fn parent_path(path: &str) -> Option<&str> {
    if path == "/" {
        return None;
    }
    path.rfind('/').map(|separator| {
        if separator == 0 {
            "/"
        } else {
            &path[..separator]
        }
    })
}

fn parse_interrupt_phandle(
    property: &Property,
    node_path: &str,
    property_name: &str,
) -> AxVmResult<u32> {
    valid_phandle(property).ok_or_else(|| {
        ax_err_type!(
            InvalidData,
            format!("FDT node {node_path} has malformed {property_name}")
        )
    })
}

fn valid_phandle(property: &Property) -> Option<u32> {
    single_u32(property).filter(|phandle| *phandle != 0 && *phandle != u32::MAX)
}

fn single_u32(property: &Property) -> Option<u32> {
    (property.data.len() == core::mem::size_of::<u32>())
        .then(|| property.get_u32())
        .flatten()
}
