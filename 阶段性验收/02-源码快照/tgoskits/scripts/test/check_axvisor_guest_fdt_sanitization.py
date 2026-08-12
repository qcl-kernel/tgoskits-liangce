#!/usr/bin/env python3
"""Static contract for fail-closed Guest-FDT preparation safeguards."""

from __future__ import annotations

import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SANITIZE = WORKSPACE_ROOT / "virtualization/axvm/src/boot/fdt/core/sanitize.rs"
INTERRUPT = WORKSPACE_ROOT / "virtualization/axvm/src/boot/fdt/core/interrupt.rs"
CREATE = WORKSPACE_ROOT / "virtualization/axvm/src/boot/fdt/core/create.rs"
AARCH64_FDT = WORKSPACE_ROOT / "virtualization/axvm/src/arch/aarch64/fdt.rs"
DESIGN = (
    WORKSPACE_ROOT
    / "docs/docs/development/aarch64-dual-guest-fdt-filtering.md"
)
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"


def read_text(path: Path, *, label: str, errors: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"{label} cannot be read: {error}")
        return ""


def require_fragments(
    text: str, fragments: tuple[str, ...], *, label: str, errors: list[str]
) -> None:
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{label} is missing `{fragment}`")


def main() -> int:
    errors: list[str] = []

    sanitize = read_text(SANITIZE, label="Guest-FDT sanitizer", errors=errors)
    require_fragments(
        sanitize,
        (
            "prune_cpu_map(fdt);",
            "remove_missing_chosen_console_references(fdt);",
            "super::interrupt::validate_interrupt_references(fdt)",
            "if cpu_phandles.ambiguous",
            "fdt.remove_by_path(CPU_MAP_PATH);",
            '["stdout-path", "linux,stdout-path"]',
            "chosen.remove_property(property_name);",
            "sanitize_guest_fdt(tree.inner_mut())?;",
        ),
        label="Guest-FDT sanitizer",
        errors=errors,
    )

    interrupt = read_text(
        INTERRUPT, label="Guest-FDT interrupt validator", errors=errors
    )
    require_fragments(
        interrupt,
        (
            "struct PhandleIndex",
            "ambiguous: BTreeSet<u32>",
            'controller.get_property("interrupt-controller").is_none()',
            'get_property("#interrupt-cells")',
            "effective_interrupt_parent(fdt, node_path)?",
            "property.data.len().is_multiple_of(byte_width)",
            'if property.data.is_empty()',
            'format!("FDT node {node_path} has empty interrupts-extended")',
            "while cursor < words.len()",
        ),
        label="Guest-FDT interrupt validator",
        errors=errors,
    )
    production_interrupt = interrupt.split("#[cfg(test)]", maxsplit=1)[0]
    if "warn!(" in production_interrupt or "return Ok(())" not in production_interrupt:
        errors.append("interrupt validation no longer has an auditable fail-closed shape")

    create = read_text(CREATE, label="Guest-FDT creation path", errors=errors)
    require_fragments(
        create,
        (
            "super::sanitize::sanitize_guest_fdt(guest_tree.inner_mut())?;",
            "generated_fdt_prunes_cpu_map_entries_for_removed_cpus",
            "generated_fdt_drops_cpu_map_for_duplicate_cpu_phandles",
            "generated_fdt_drops_cpu_map_for_malformed_cpu_phandle",
            "generated_fdt_rejects_missing_inherited_interrupt_parent",
            "generated_fdt_accepts_retained_interrupt_parent",
            "generated_fdt_rejects_conflicting_controller_phandles",
            "provided_fdt_cpu_replacement_prunes_cpu_map_and_missing_console",
            "provided_fdt_rejects_missing_interrupt_references",
            "provided_fdt_rejects_empty_interrupts_extended",
            "generated_fdt_drops_missing_chosen_console_but_keeps_bootargs",
        ),
        label="Guest-FDT creation path and tests",
        errors=errors,
    )

    aarch64_fdt = read_text(
        AARCH64_FDT, label="AArch64 provided-DTB path", errors=errors
    )
    require_fragments(
        aarch64_fdt,
        (
            "core::sanitize::sanitize_guest_fdt(&mut provided_fdt)?;",
            "core::sanitize::replace_cpu_nodes_from_host",
        ),
        label="AArch64 provided-DTB path",
        errors=errors,
    )

    design = read_text(DESIGN, label="Guest-FDT design", errors=errors)
    require_fragments(
        design,
        (
            "状态：第一层部分实现；完整设计尚未实现",
            "运行时二次语义验证",
            "实际 capture/hash evidence 与 QEMU 回归均未完成",
            "不等于双 Guest 已经启动",
        ),
        label="Guest-FDT design",
        errors=errors,
    )

    ci = read_text(CI, label="CI workflow", errors=errors)
    if "python3 scripts/test/check_axvisor_guest_fdt_sanitization.py" not in ci:
        errors.append("Guest-FDT sanitizer contract is not wired into CI")

    if not errors:
        return 0

    print("AxVisor Guest-FDT sanitizer contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
