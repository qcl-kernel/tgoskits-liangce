#!/usr/bin/env python3
"""Static contract for the feature-gated final Guest-DTB runtime marker."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = WORKSPACE_ROOT / "virtualization/axvm/src/boot/fdt/evidence.rs"
FDT_MOD = WORKSPACE_ROOT / "virtualization/axvm/src/boot/fdt/mod.rs"
CREATE = WORKSPACE_ROOT / "virtualization/axvm/src/boot/fdt/core/create.rs"
VM = WORKSPACE_ROOT / "virtualization/axvm/src/vm/mod.rs"
AXVM_CARGO = WORKSPACE_ROOT / "virtualization/axvm/Cargo.toml"
AXVISOR_CARGO = WORKSPACE_ROOT / "os/axvisor/Cargo.toml"
README = WORKSPACE_ROOT / "scripts/contest/README.md"
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


def check_feature(
    path: Path,
    *,
    label: str,
    expected_value: list[str],
    errors: list[str],
) -> None:
    try:
        manifest = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"{label} cannot be read as TOML: {error}")
        return
    features = manifest.get("features", {})
    if features.get("guest-fdt-evidence") != expected_value:
        errors.append(f"{label} has the wrong guest-fdt-evidence feature mapping")
    if "guest-fdt-evidence" in features.get("default", []):
        errors.append(f"{label} enables Guest-DTB evidence by default")


def main() -> int:
    errors: list[str] = []

    evidence = read_text(EVIDENCE, label="Guest-DTB evidence module", errors=errors)
    if evidence:
        production = evidence.split("#[cfg(test)]", maxsplit=1)[0]
        require_fragments(
            production,
            (
                "pub(crate) fn resolve_hpa_segments",
                "while cursor < end",
                "let hpa = translate(GuestPhysAddr::from(cursor))?;",
                "PAGE_SIZE_4K - cursor % PAGE_SIZE_4K",
                "checked_add(segment.length)",
                "if covered != size",
                '"AXVISOR_GUEST_DTB_READY vm={} gpa={:#x} size={} hpa_segments={segments}"',
                "let hpa_segments = vm.guest_dtb_hpa_segments(gpa, size)?;",
                'ax_std::println!("\\n{}", descriptor.marker());',
            ),
            label="Guest-DTB evidence production path",
            errors=errors,
        )
        if "HostPhysAddr::from(gpa.as_usize())" in production:
            errors.append("Guest-DTB evidence assumes GPA equals HPA")
        require_fragments(
            evidence,
            (
                "hpa_resolver_coalesces_only_contiguous_pages",
                "descriptor_formats_strict_ready_marker",
                "descriptor_rejects_incomplete_hpa_coverage",
                "hpa_resolver_rejects_unmapped_page",
            ),
            label="Guest-DTB evidence unit-test appendix",
            errors=errors,
        )

    fdt_mod = read_text(FDT_MOD, label="Guest FDT module", errors=errors)
    require_fragments(
        fdt_mod,
        (
            '#[cfg(feature = "guest-fdt-evidence")]\n#[allow(\n    dead_code,\n    reason = "AArch64 evidence stays host-compiled for feature audits"\n)]\npub(crate) mod evidence;',
        ),
        label="Guest FDT module",
        errors=errors,
    )

    create = read_text(CREATE, label="Guest FDT load path", errors=errors)
    load = create.find("load_vm_image_from_memory(&new_fdt_bytes, dest_addr, vm.clone())?;")
    store = create.find("vm.set_guest_device_tree(dest_addr, new_fdt_bytes)?;")
    marker = create.find("crate::boot::fdt::evidence::emit_ready_marker")
    if min(load, store, marker) < 0 or not load < store < marker:
        errors.append(
            "Guest-DTB marker is not ordered after guest-memory load and VM-owned DTB state"
        )
    marker_gate = create.rfind(
        '#[cfg(feature = "guest-fdt-evidence")]', 0, marker if marker >= 0 else None
    )
    if marker >= 0 and marker_gate < store:
        errors.append("Guest-DTB marker call is not directly feature-gated")

    vm = read_text(VM, label="AxVM HPA resolver", errors=errors)
    require_fragments(
        vm,
        (
            '#[cfg(feature = "guest-fdt-evidence")]\n    #[allow(\n        dead_code,\n        reason = "AArch64 evidence stays host-compiled for feature audits"\n    )]\n    pub(crate) fn guest_dtb_hpa_segments',
            ".address_space\n                    .page_table()\n                    .query(gpa)",
            "E: Into<axaddrspace::AddrSpaceError>",
            'AxVmError::from_addrspace("resolve guest DTB HPA segment", error.into())',
            ".map_err(map_query_error)",
        ),
        label="AxVM HPA resolver",
        errors=errors,
    )

    check_feature(
        AXVM_CARGO,
        label="axvm manifest",
        expected_value=[],
        errors=errors,
    )
    check_feature(
        AXVISOR_CARGO,
        label="AxVisor manifest",
        expected_value=["axvm/guest-fdt-evidence"],
        errors=errors,
    )

    readme = read_text(README, label="contest evidence documentation", errors=errors)
    require_fragments(
        readme,
        (
            "The feature-gated `guest-fdt-evidence` path",
            "The r9 marker-line and strict demux release run passed.",
            "local r13 run then exercised the single-Guest harness against a real QMP socket",
            "completed same-session `stop`/`pmemsave`/`cont`",
            "passed an independent `dtc` decode plus the static semantic validator",
            "r16 measurement repeated that capture while also producing one byte-bound",
            "These are still single-Guest evidence, not",
            "a dual-Guest, DMA-isolation, or IP-connectivity success claim.",
        ),
        label="contest evidence documentation",
        errors=errors,
    )

    ci = read_text(CI, label="CI workflow", errors=errors)
    if "python3 scripts/test/check_axvisor_guest_dtb_evidence.py" not in ci:
        errors.append("Guest-DTB runtime evidence contract is not wired into CI")

    if not errors:
        return 0

    print("AxVisor final Guest-DTB evidence contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
