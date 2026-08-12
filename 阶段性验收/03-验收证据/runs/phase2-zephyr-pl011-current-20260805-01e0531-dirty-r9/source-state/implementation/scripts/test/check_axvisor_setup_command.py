#!/usr/bin/env python3

import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = WORKSPACE_ROOT / "os/axvisor/scripts/setup_qemu.sh"
LOCAL_XTASK_COMMAND = "  cd ${REPO_ROOT}\n  cargo xtask qemu \\\\\n"
TOP_LEVEL_XTASK_COMMAND = "cargo xtask axvisor qemu"


def main() -> int:
    setup_script = SETUP_SCRIPT.read_text(encoding="utf-8")
    errors = validate_setup_command(setup_script)
    if not errors:
        return 0

    print("AxVisor QEMU setup command contract is invalid:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


def validate_setup_command(setup_script: str) -> list[str]:
    errors = []
    if LOCAL_XTASK_COMMAND not in setup_script:
        errors.append("the command printed after cd ${REPO_ROOT} must use the local `cargo xtask qemu` interface")
    if TOP_LEVEL_XTASK_COMMAND in setup_script:
        errors.append("the local AxVisor directory must not use the top-level `cargo xtask axvisor qemu` interface")
    return errors


if __name__ == "__main__":
    sys.exit(main())
