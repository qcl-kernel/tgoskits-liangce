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

//! Guest FDT cleanup after CPU and device filtering.

use alloc::{
    collections::{BTreeMap, BTreeSet},
    string::{String, ToString},
    vec::Vec,
};

use fdt_edit::Fdt;

use super::{create::need_cpu_node, tree::FdtTree};
use crate::AxVmResult;

const CPU_MAP_PATH: &str = "/cpus/cpu-map";
const CHOSEN_PATH: &str = "/chosen";
const ALIASES_PATH: &str = "/aliases";
const CONSOLE_PROPERTIES: [&str; 2] = ["stdout-path", "linux,stdout-path"];

pub(crate) fn sanitize_guest_fdt(fdt: &mut Fdt) -> AxVmResult {
    prune_cpu_map(fdt);
    remove_missing_chosen_console_references(fdt);
    super::interrupt::validate_interrupt_references(fdt)
}

pub(crate) fn replace_cpu_nodes_from_host(
    guest_fdt: &Fdt,
    host_fdt: &Fdt,
    phys_cpu_ids: &[usize],
) -> AxVmResult<Vec<u8>> {
    let mut tree = FdtTree::from_fdt(guest_fdt.clone());
    tree.inner_mut().remove_by_path("/cpus");

    if let Some(host_cpus_id) = host_fdt.get_by_path_id("/cpus") {
        let cpus_id =
            tree.copy_subtree_from(host_fdt, host_cpus_id, tree.inner().root_id(), true)?;
        let removed_cpu_paths = tree
            .node_paths()
            .into_iter()
            .filter_map(|(node_id, path)| {
                (path.starts_with("/cpus/cpu@")
                    && !need_cpu_node(phys_cpu_ids, tree.inner(), node_id, &path))
                .then_some(path)
            })
            .collect::<Vec<_>>();
        for path in removed_cpu_paths {
            tree.inner_mut().remove_by_path(&path);
        }
        if let Some(cpus) = tree.inner_mut().node_mut(cpus_id) {
            for property in [
                "riscv,cbop-block-size",
                "riscv,cboz-block-size",
                "riscv,cbom-block-size",
            ] {
                cpus.remove_property(property);
            }
        }
    }

    sanitize_guest_fdt(tree.inner_mut())?;
    Ok(tree.finish())
}

fn prune_cpu_map(fdt: &mut Fdt) {
    if fdt.get_by_path_id(CPU_MAP_PATH).is_none() {
        return;
    }

    let cpu_phandles = retained_cpu_phandles(fdt);
    if cpu_phandles.ambiguous {
        fdt.remove_by_path(CPU_MAP_PATH);
        return;
    }
    let cpu_map_paths = fdt
        .iter_node_ids()
        .filter_map(|node_id| {
            let path = fdt.path_of(node_id);
            is_cpu_map_path(&path).then_some((node_id, path))
        })
        .collect::<Vec<_>>();
    let mut retained_paths = BTreeSet::new();
    let mut invalid_reference_paths = BTreeSet::new();

    for (node_id, path) in &cpu_map_paths {
        let Some(property) = fdt.node(*node_id).and_then(|node| node.get_property("cpu")) else {
            continue;
        };
        if valid_phandle(property).is_some_and(|cpu| cpu_phandles.values.contains(&cpu)) {
            retain_path_and_ancestors(path, &mut retained_paths);
        } else {
            invalid_reference_paths.insert(path.clone());
        }
    }

    if retained_paths.is_empty()
        || invalid_reference_paths
            .iter()
            .any(|path| retained_paths.contains(path))
    {
        fdt.remove_by_path(CPU_MAP_PATH);
        return;
    }

    let mut removed_paths = cpu_map_paths
        .into_iter()
        .map(|(_, path)| path)
        .filter(|path| path != CPU_MAP_PATH && !retained_paths.contains(path))
        .collect::<Vec<_>>();
    removed_paths.sort_by_key(|path| core::cmp::Reverse(path.matches('/').count()));
    for path in removed_paths {
        fdt.remove_by_path(&path);
    }
}

struct RetainedCpuPhandles {
    values: BTreeSet<u32>,
    ambiguous: bool,
}

fn retained_cpu_phandles(fdt: &Fdt) -> RetainedCpuPhandles {
    let mut owners = BTreeMap::new();
    let mut values = BTreeSet::new();
    let mut ambiguous = false;

    for node_id in fdt.iter_node_ids() {
        let path = fdt.path_of(node_id);
        if !is_direct_cpu_node_path(&path) {
            continue;
        }
        let Some(node) = fdt.node(node_id) else {
            continue;
        };
        let mut node_phandles = BTreeSet::new();
        for property_name in ["phandle", "linux,phandle"] {
            let Some(property) = node.get_property(property_name) else {
                continue;
            };
            if let Some(phandle) = valid_phandle(property) {
                node_phandles.insert(phandle);
            } else {
                ambiguous = true;
            }
        }
        if node_phandles.len() > 1 {
            ambiguous = true;
        }
        for phandle in node_phandles {
            if owners
                .get(&phandle)
                .is_some_and(|owner: &String| owner != &path)
            {
                ambiguous = true;
            } else {
                owners.insert(phandle, path.clone());
            }
            values.insert(phandle);
        }
    }

    RetainedCpuPhandles { values, ambiguous }
}

fn valid_phandle(property: &fdt_edit::Property) -> Option<u32> {
    (property.data.len() == core::mem::size_of::<u32>())
        .then(|| property.get_u32())
        .flatten()
        .filter(|phandle| *phandle != 0 && *phandle != u32::MAX)
}

fn is_direct_cpu_node_path(path: &str) -> bool {
    path.strip_prefix("/cpus/cpu@")
        .is_some_and(|suffix| !suffix.is_empty() && !suffix.contains('/'))
}

fn is_cpu_map_path(path: &str) -> bool {
    path == CPU_MAP_PATH
        || path
            .strip_prefix(CPU_MAP_PATH)
            .is_some_and(|suffix| suffix.starts_with('/'))
}

fn retain_path_and_ancestors(path: &str, retained_paths: &mut BTreeSet<String>) {
    let mut current = path;
    loop {
        retained_paths.insert(current.to_string());
        if current == CPU_MAP_PATH {
            break;
        }
        let Some(separator) = current.rfind('/') else {
            break;
        };
        current = &current[..separator];
        if !is_cpu_map_path(current) {
            break;
        }
    }
}

fn remove_missing_chosen_console_references(fdt: &mut Fdt) {
    let Some(chosen_id) = fdt.get_by_path_id(CHOSEN_PATH) else {
        return;
    };
    let removed_properties = CONSOLE_PROPERTIES
        .into_iter()
        .filter(|property_name| {
            let Some(property) = fdt
                .node(chosen_id)
                .and_then(|chosen| chosen.get_property(property_name))
            else {
                return false;
            };
            property
                .as_str()
                .is_none_or(|value| !console_target_exists(fdt, value))
        })
        .collect::<Vec<_>>();

    if let Some(chosen) = fdt.node_mut(chosen_id) {
        for property_name in removed_properties {
            chosen.remove_property(property_name);
        }
    }
}

fn console_target_exists(fdt: &Fdt, stdout_path: &str) -> bool {
    let target = strip_console_options(stdout_path.trim());
    if target.is_empty() {
        return false;
    }

    if target.starts_with('/') {
        return fdt.get_by_path_id(target).is_some();
    }

    let Some(alias_target) = fdt
        .get_by_path(ALIASES_PATH)
        .and_then(|aliases| aliases.as_node().get_property(target))
        .and_then(|property| property.as_str())
    else {
        return false;
    };
    let alias_target = strip_console_options(alias_target.trim());
    alias_target.starts_with('/') && fdt.get_by_path_id(alias_target).is_some()
}

fn strip_console_options(path: &str) -> &str {
    path.split_once(':').map_or(path, |(target, _)| target)
}
