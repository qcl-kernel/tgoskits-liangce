#!/usr/bin/env python3
"""Static contract for process-wide AxVM physical resource ownership.

The contract complements Rust unit tests by ensuring the claim registry is
wired into VM construction, memory preparation, device/vCPU preparation, and
CI.  A per-VM device registry or a late register_vm() check is insufficient.
"""

from __future__ import annotations

from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
LIB_RS = WORKSPACE_ROOT / "virtualization/axvm/src/lib.rs"
VM_RS = WORKSPACE_ROOT / "virtualization/axvm/src/vm/mod.rs"
PREPARE_RS = WORKSPACE_ROOT / "virtualization/axvm/src/vm/prepare.rs"
CLAIMS_MOD_RS = WORKSPACE_ROOT / "virtualization/axvm/src/resource_claim/mod.rs"
CLAIMS_TESTS_RS = WORKSPACE_ROOT / "virtualization/axvm/src/resource_claim/tests.rs"
LEGACY_CLAIMS_RS = WORKSPACE_ROOT / "virtualization/axvm/src/resource_claim.rs"
CI_YML = WORKSPACE_ROOT / ".github/workflows/ci.yml"


def require(errors: list[str], source: str, path: Path, snippets: tuple[str, ...]) -> None:
    for snippet in snippets:
        if snippet not in source:
            errors.append(f"{path.relative_to(WORKSPACE_ROOT)} is missing `{snippet}`")


def check_module(errors: list[str]) -> None:
    module_source = CLAIMS_MOD_RS.read_text(encoding="utf-8")
    if len(module_source.splitlines()) >= 800:
        errors.append(
            f"{CLAIMS_MOD_RS.relative_to(WORKSPACE_ROOT)} must remain below 800 lines"
        )
    require(
        errors,
        module_source,
        CLAIMS_MOD_RS,
        (
            "struct PhysicalResourceClaims",
            "exclusive_address_space",
            "vcpu_placements",
            "pcpu_usage_mask",
            "struct HostPhysicalRange",
            "struct HostPortRange",
            "struct PhysicalResourceRegistry",
            "pub(crate) struct PhysicalResourceLease",
            "static PHYSICAL_RESOURCE_REGISTRY",
            "AddressSpacePolicy::Passthrough",
            "VmMemMappingType::MapReserved",
            "impl Drop for PhysicalResourceLease",
            "mod tests;",
        ),
    )

    tests_source = CLAIMS_TESTS_RS.read_text(encoding="utf-8")
    require(
        errors,
        tests_source,
        CLAIMS_TESTS_RS,
        (
            "rejects_duplicate_vm_id_without_mutating_registry",
            "rejects_overlapping_passthrough_pcpu_masks",
            "allows_emulated_vms_to_share_pcpu_affinity",
            "rejects_passthrough_pcpu_overlap_with_emulated_vm",
            "allows_disjoint_passthrough_and_emulated_pcpu_masks",
            "rejects_overlapping_host_physical_ranges_across_sources",
            "allows_adjacent_host_physical_ranges",
            "rejects_duplicate_passthrough_irq",
            "released_claims_can_be_reused",
            "dynamic_memory_mappings_are_not_preclaimed",
            "passthrough_policy_rejects_dynamic_vm_in_both_orders",
            "rejects_overlapping_host_port_ranges",
            "allows_adjacent_host_port_ranges",
            "rejects_invalid_host_port_ranges",
            "same_union_vcpu_affinity_swap_invalidates_frozen_claim",
            "physical_cpu_id_change_invalidates_frozen_claim",
        ),
    )


def check_lifecycle_wiring(errors: list[str]) -> None:
    lib_source = LIB_RS.read_text(encoding="utf-8")
    vm_source = VM_RS.read_text(encoding="utf-8")
    prepare_source = PREPARE_RS.read_text(encoding="utf-8")

    require(errors, lib_source, LIB_RS, ("mod resource_claim;",))
    require(
        errors,
        vm_source,
        VM_RS,
        (
            "resource_claim::PhysicalResourceLease",
            "physical_resource_lease: PhysicalResourceLease",
            "PhysicalResourceLease::acquire(&config)?",
            "physical_resource_lease.validate(resources.config())?",
            "pub fn with_config<F, R>(&self, f: F) -> R",
            "F: FnOnce(&AxVMConfig) -> R",
            "pub(crate) fn with_config_mut<F, R>(&self, f: F) -> R",
        ),
    )
    require(
        errors,
        prepare_source,
        PREPARE_RS,
        ("vm.physical_resource_lease.validate(resources.config())?",),
    )

    acquire = vm_source.find("PhysicalResourceLease::acquire(&config)?")
    create = vm_source.find("CurrentArch::create_vm_resources(config)?")
    if acquire < 0 or create < 0 or acquire > create:
        errors.append("AxVM::new must acquire physical resources before architecture resources")

    validate = prepare_source.find("physical_resource_lease.validate(resources.config())?")
    initialize = prepare_source.find("let prepared = match initialize")
    if validate < 0 or initialize < 0 or validate > initialize:
        errors.append("VM preparation must validate the frozen claim before architecture init")


def check_ci(errors: list[str]) -> None:
    source = CI_YML.read_text(encoding="utf-8")
    require(
        errors,
        source,
        CI_YML,
        ("python3 scripts/test/check_axvm_physical_resource_claims.py",),
    )


def main() -> int:
    errors: list[str] = []
    for path in (
        LIB_RS,
        VM_RS,
        PREPARE_RS,
        CLAIMS_MOD_RS,
        CLAIMS_TESTS_RS,
        CI_YML,
    ):
        if not path.is_file():
            errors.append(f"required source file is missing: {path.relative_to(WORKSPACE_ROOT)}")
    if LEGACY_CLAIMS_RS.exists():
        errors.append(
            f"legacy module file must be removed: {LEGACY_CLAIMS_RS.relative_to(WORKSPACE_ROOT)}"
        )

    if not errors:
        check_module(errors)
        check_lifecycle_wiring(errors)
        check_ci(errors)

    if errors:
        print("AxVM physical resource claim contract failed:")
        for error in errors:
            print(f"  - {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
