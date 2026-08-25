#!/usr/bin/env python3
"""Pure host contract for the f964 P2 shared dual-Guest candidate."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "contest"))

from runtime import dual_guest  # noqa: E402


def _expect_rejected(errors: list[str], action, needle: str) -> None:
    try:
        action()
    except dual_guest.RuntimeContractError as error:
        if needle not in str(error):
            errors.append(f"wrong rejection: expected {needle!r}, got {error}")
    else:
        errors.append(f"accepted forbidden input: {needle}")


def main() -> int:
    errors: list[str] = []
    qemu = (ROOT / "configs/contest/qemu-aarch64-linux-zephyr-p2-dual.toml").read_text(encoding="utf-8")
    linux = (ROOT / "os/axvisor/configs/vms/qemu/aarch64/linux-smp2-p2-dual.toml").read_text(encoding="utf-8")
    zephyr = (ROOT / "os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-p2-dual.toml").read_text(encoding="utf-8")

    try:
        dual_guest.validate_no_data_plane_qemu_config(qemu)
        dual_guest.validate_no_data_plane_vm_config(linux, expected_vm_id=1, expected_cpu_ids=(0, 1))
        dual_guest.validate_no_data_plane_vm_config(zephyr, expected_vm_id=2, expected_cpu_ids=(2,))
    except dual_guest.RuntimeContractError as error:
        errors.append(f"checked-in P2 config rejected: {error}")

    log = "\n".join(
        [
            "contest dtb evidence: vm=1 gpa=0x80000000 size=0x960 hpa=0x124e00000",
            "contest dtb evidence: vm=2 gpa=0x47e00000 size=0x854 hpa=0x13cc00000",
            "[VM 1] AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=p2-static-20260824",
            "[VM 2] AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=p2-static-20260824",
            "[VM 1] [Axvisor VM 1 console dropped 0 buffered bytes]",
            "[VM 2] dropped=0 dma=0",
        ]
    )
    try:
        observation = dual_guest.validate_dual_guest_log(log, run_id="p2-static-20260824")
        if sorted(observation.dtb) != [1, 2] or sorted(observation.ready) != [1, 2]:
            errors.append("valid log does not contain both DTB and READY identities")
    except dual_guest.RuntimeContractError as error:
        errors.append(f"valid log rejected: {error}")

    _expect_rejected(
        errors,
        lambda: dual_guest.validate_dual_guest_log(
            log.replace("[VM 2] AXVISOR_DUAL_GUEST_ZEPHYR_READY", "[VM 1] AXVISOR_DUAL_GUEST_ZEPHYR_READY"),
            run_id="p2-static-20260824",
        ),
        "READY",
    )
    _expect_rejected(
        errors,
        lambda: dual_guest.validate_dual_guest_log(
            log.replace("console dropped 0", "console dropped 1"),
            run_id="p2-static-20260824",
        ),
        "non-zero",
    )
    _expect_rejected(
        errors,
        lambda: dual_guest.validate_dual_guest_log(
            log.replace("[VM 1] AXVISOR_DUAL_GUEST_LINUX_READY", "AXVISOR_DUAL_GUEST_LINUX_READY"),
            run_id="p2-static-20260824",
        ),
        "not attributed",
    )
    _expect_rejected(
        errors,
        lambda: dual_guest.validate_dual_guest_log(
            log + "\nAXVISOR_GUEST_CONSOLE_FRAME v=1\n",
            run_id="p2-static-20260824",
        ),
        "legacy",
    )
    _expect_rejected(
        errors,
        lambda: dual_guest.validate_no_data_plane_qemu_config(qemu.replace('"none"', '"user,id=n"')),
        "exact -nic none",
    )
    _expect_rejected(
        errors,
        lambda: dual_guest.validate_no_data_plane_vm_config(
            linux + "\n[[devices.virtual]]\nmodel = \"virtio-net\"\n",
            expected_vm_id=1,
            expected_cpu_ids=(0, 1),
        ),
        "data-plane",
    )

    resolved = dual_guest.inject_qemu_identity_args(
        qemu,
        qmp_socket=Path("/tmp/axdual-test/qmp.sock"),
        qemu_pidfile=Path("/tmp/axdual-test/qemu.pid"),
        qemu_name="axvisor-dual-smoke-0123456789abcdef0123456789abcdef",
    )
    if "-qmp" not in resolved or "-pidfile" not in resolved or "-name" not in resolved:
        errors.append("resolved QEMU config does not contain all identity arguments")
    _expect_rejected(
        errors,
        lambda: dual_guest.inject_qemu_identity_args(resolved, qmp_socket=Path("/tmp/q"), qemu_pidfile=Path("/tmp/p"), qemu_name="again"),
        "already contains",
    )

    if errors:
        print("P2 shared dual-Guest contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("P2 shared dual-Guest contract passed (host/static only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
