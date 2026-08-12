#!/usr/bin/env python3

import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
IMAGE_COMMAND_FILES = (
    WORKSPACE_ROOT / "os/axvisor/scripts/setup_qemu.sh",
    WORKSPACE_ROOT / "os/axvisor/scripts/quick-start.sh",
    WORKSPACE_ROOT / "os/axvisor/doc/qemu-quickstart.md",
    WORKSPACE_ROOT / "os/axvisor/doc/qemu-quickstart_cn.md",
)
LOCAL_XTASK = WORKSPACE_ROOT / "os/axvisor/xtask/src/main.rs"
LEGACY_IMAGE_COMMAND = "cargo axvisor image"


def main() -> int:
    errors = find_legacy_image_references()
    errors.extend(find_removed_local_image_variant())
    errors.extend(validate_setup_helper())
    errors.extend(validate_quick_start_helper())
    if not errors:
        return 0

    print("AxVisor image CLI migration is incomplete:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


def find_legacy_image_references() -> list[str]:
    errors = []
    for path in IMAGE_COMMAND_FILES:
        content = path.read_text(encoding="utf-8")
        if LEGACY_IMAGE_COMMAND in content:
            errors.append(f"{path.relative_to(WORKSPACE_ROOT)} still uses `{LEGACY_IMAGE_COMMAND}`")
    return errors


def find_removed_local_image_variant() -> list[str]:
    content = LOCAL_XTASK.read_text(encoding="utf-8")
    if "Command::Image(" not in content:
        return []
    return [
        "os/axvisor/xtask/src/main.rs still matches the removed axbuild::axvisor::Command::Image variant"
    ]


def validate_setup_helper() -> list[str]:
    path = WORKSPACE_ROOT / "os/axvisor/scripts/setup_qemu.sh"
    content = path.read_text(encoding="utf-8")
    required = (
        'WORKSPACE_ROOT="$(cd "${REPO_ROOT}/../.." && pwd)"',
        'IMAGE_STORAGE_ROOT="${AXVISOR_IMAGE_LOCAL_STORAGE:-/tmp/.axvisor-images}"',
        "pull_image() {",
        'cd "${WORKSPACE_ROOT}"',
        'cargo xtask image --local-storage "${IMAGE_STORAGE_ROOT}" pull "${image_name}"',
        "bootstrap_fallback_image_registry() {",
        "bootstrap_fallback_image_registry",
        'BUILTIN_FALLBACK_REGISTRY_URL="https://raw.githubusercontent.com/arceos-hypervisor/axvisor-guest/refs/heads/main/registry/v0.0.25.toml"',
    )
    errors = [
        f"{path.relative_to(WORKSPACE_ROOT)} is missing `{snippet}`"
        for snippet in required
        if snippet not in content
    ]
    stale_registry_guard = 'if [ -f "${storage_dir}/images.toml" ]; then'
    if stale_registry_guard in content:
        errors.append(
            f"{path.relative_to(WORKSPACE_ROOT)} skips fallback when a stale registry exists"
        )
    return errors


def validate_quick_start_helper() -> list[str]:
    path = WORKSPACE_ROOT / "os/axvisor/scripts/quick-start.sh"
    content = path.read_text(encoding="utf-8")
    required = (
        "run_image_cmd() {",
        'cd "${WORKSPACE_ROOT}"',
        'cargo xtask image "$@"',
        'AXVISOR_ROOT="$(pwd -P)"',
        'WORKSPACE_ROOT="$(cd "${AXVISOR_ROOT}/../.." && pwd -P)"',
        '--output-dir "${AXVISOR_ROOT}/tmp/images"',
        'local image_pull_args=(-S "${image_storage}")',
    )
    errors = [
        f"{path.relative_to(WORKSPACE_ROOT)} is missing `{snippet}`"
        for snippet in required
        if snippet not in content
    ]
    forbidden = (
        "run_cmd cargo xtask image",
        "--output-dir tmp/images",
    )
    errors.extend(
        f"{path.relative_to(WORKSPACE_ROOT)} uses unsafe local invocation `{snippet}`"
        for snippet in forbidden
        if snippet in content
    )
    return errors


if __name__ == "__main__":
    sys.exit(main())
