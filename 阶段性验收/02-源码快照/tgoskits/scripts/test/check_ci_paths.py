#!/usr/bin/env python3

import re
import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_MANIFEST = WORKSPACE_ROOT / "Cargo.toml"
CI_WORKFLOW = WORKSPACE_ROOT / ".github/workflows/ci.yml"
REQUIRED_CI_PATHS = {"configs/**"}
REQUIRED_CI_CHECKS = {
    "scripts/test/check_contest_developer_docs.py",
    "scripts/test/check_icpc_protocol.py",
    "scripts/test/check_axvisor_prepare_host_vm_carveout_dtb.py",
    "scripts/test/check_axvisor_host_carveout_runtime_evidence.py",
    "scripts/test/check_axvisor_host_carveout_allocator_runtime_evidence.py",
    "scripts/test/check_axvisor_linux_map_reserved_boot_runtime_evidence.py",
    "scripts/test/check_axvisor_virtio_dma_effect_probe.py",
    "scripts/test/check_axvisor_prepare_virtio_dma_effect_request.py",
    "scripts/test/check_axvisor_guest_virtio_blk_odirect_probe.py",
    "scripts/test/check_axvisor_disposable_virtio_dma_rootfs.py",
    "scripts/test/check_axvisor_prepare_virtio_dma_effect_qemu.py",
    "scripts/test/check_axvisor_guest_kernel_virtio_console.py",
    "scripts/test/check_axvisor_run_virtio_dma_effect_probe.py",
    "scripts/test/check_axvisor_single_guest_dtb_harness.py",
    "scripts/test/check_axvisor_dual_guest_soak_session.py",
}


def main() -> int:
    source_roots = workspace_source_roots()
    ci_paths = ci_check_paths()
    expected_paths = {f"{root}/**" for root in source_roots} | REQUIRED_CI_PATHS
    missing_paths = sorted(path for path in expected_paths if path not in ci_paths)
    ci_checks = ci_python_checks()
    missing_checks = sorted(REQUIRED_CI_CHECKS - ci_checks)

    if not missing_paths and not missing_checks:
        return 0

    if missing_paths:
        print("CI path filter is missing workspace source directories:", file=sys.stderr)
        for path in missing_paths:
            print(f"  - {path}", file=sys.stderr)
    if missing_checks:
        print("CI workflow is missing required contract checks:", file=sys.stderr)
        for check in missing_checks:
            print(f"  - python3 {check}", file=sys.stderr)
    return 1


def workspace_source_roots() -> set[str]:
    manifest = WORKSPACE_MANIFEST.read_text(encoding="utf-8")
    members = manifest.split("members = [", maxsplit=1)[1].split("]", maxsplit=1)[0]
    package_paths = re.findall(r'^\s+"([^"]+)",?$', members, flags=re.MULTILINE)
    package_paths.extend(re.findall(r'\bpath\s*=\s*"([^"]+)"', manifest))
    return {Path(package_path).parts[0] for package_path in package_paths}


def ci_check_paths() -> set[str]:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    ci_checks = workflow.split("            ci_checks:\n", maxsplit=1)[1].split(
        "            base_container_publish:\n", maxsplit=1
    )[0]
    return set(re.findall(r'^\s+- "([^"]+)"$', ci_checks, flags=re.MULTILINE))


def ci_python_checks() -> set[str]:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    return set(
        re.findall(
            r"^\s*run:\s+python3\s+(scripts/test/check_[^\s]+\.py)\s*$",
            workflow,
            flags=re.MULTILINE,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
