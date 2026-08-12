#!/usr/bin/env python3
"""Static contract for the blocked Linux+Zephyr dual-guest configuration."""

from __future__ import annotations

import copy
import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
LINUX_CANONICAL = (
    WORKSPACE_ROOT / "os/axvisor/configs/vms/qemu/aarch64/linux-smp2.toml"
)
ZEPHYR_CANONICAL = (
    WORKSPACE_ROOT / "os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1.toml"
)
LINUX_DUAL = (
    WORKSPACE_ROOT
    / "os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml"
)
ZEPHYR_DUAL = (
    WORKSPACE_ROOT
    / "os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml"
)
QEMU_DUAL = (
    WORKSPACE_ROOT / "configs/contest/qemu-aarch64-linux-zephyr-dual.toml"
)
TOPOLOGY_VALIDATOR = (
    WORKSPACE_ROOT / "scripts/contest/validate_dual_guest_topology.py"
)
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"

BLOCKED_STATUS = "blocked_dma_console"
GICD = ["gppt-gicd", 0x0800_0000, 0x1_0000, 0, 0x21, []]
LINUX_GICR = [
    "gppt-gicr",
    0x080A_0000,
    0x2_0000,
    0,
    0x20,
    [2, 0x2_0000, 0],
]
ZEPHYR_GICR = [
    "gppt-gicr",
    0x080A_0000,
    0x2_0000,
    0,
    0x20,
    [1, 0x2_0000, 2],
]
LINUX_POLLING_PL011 = ["linux", 0x0900_0000, 0x1000, 0, 0x2, []]
ZEPHYR_POLLING_PL011 = ["zephyr", 0x0900_0000, 0x1000, 0, 0x2, []]

LINUX_PATHS = [
    ["/chosen"],
    ["/psci"],
    ["/timer"],
    ["/intc@8000000"],
    ["/virtio_mmio@a000000"],
    ["/virtio_mmio@a000200"],
]
LINUX_EXCLUDED = [
    ["/intc@8000000/its@8080000"],
    ["/pl011@9000000"],
    ["/virtio_mmio@a000400"],
]
ZEPHYR_PATHS = [
    ["/psci"],
    ["/timer"],
    ["/intc@8000000"],
]
ZEPHYR_EXCLUDED = [
    ["/intc@8000000/its@8080000"],
    ["/pl011@9000000"],
    ["/virtio_mmio@a000000"],
    ["/virtio_mmio@a000200"],
    ["/virtio_mmio@a000400"],
]

EXPECTED_QEMU_ARGS = [
    "-nographic",
    "-cpu",
    "cortex-a72",
    "-machine",
    "virt,virtualization=on,gic-version=3",
    "-smp",
    "4",
    "-m",
    "8g",
    "-drive",
    "id=linux-root,if=none,format=raw,file=${workspaceFolder}/tmp/rootfs.img",
    "-device",
    "virtio-blk-device,id=linux-block,drive=linux-root,bus=virtio-mmio-bus.0",
    "-append",
    "root=/dev/vda rw init=/bin/sh",
    "-netdev",
    "hubport,id=linux-netdev,hubid=0",
    "-device",
    (
        "virtio-net-device,id=linux-net,netdev=linux-netdev,"
        "bus=virtio-mmio-bus.1,mac=02:00:00:00:00:01"
    ),
    "-netdev",
    "hubport,id=zephyr-netdev,hubid=0",
    "-device",
    (
        "virtio-net-device,id=zephyr-net,netdev=zephyr-netdev,"
        "bus=virtio-mmio-bus.2,mac=02:00:00:00:00:02"
    ),
]


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_toml(
    path: Path, label: str, errors: list[str]
) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        errors.append(f"{label} is missing")
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
        return tomllib.loads(text), text
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"{label} cannot be parsed: {error}")
        return None, None


def check_path_only(
    entries: object, *, label: str, errors: list[str]
) -> None:
    if not isinstance(entries, list):
        errors.append(f"{label} is not an array")
        return
    for entry in entries:
        if (
            not isinstance(entry, list)
            or len(entry) != 1
            or not isinstance(entry[0], str)
            or not entry[0].startswith("/")
        ):
            errors.append(f"{label} contains a non path-only entry: {entry!r}")
    if ["/"] in entries:
        errors.append(f"{label} contains forbidden root passthrough")


def expected_linux(canonical: dict[str, Any]) -> dict[str, Any]:
    expected = copy.deepcopy(canonical)
    expected["base"]["name"] = "linux-qemu-smp2-dual"
    expected["devices"] = {
        "interrupt_mode": "passthrough",
        "passthrough_devices": LINUX_PATHS,
        "passthrough_addresses": [],
        "excluded_devices": LINUX_EXCLUDED,
        "emu_devices": [GICD, LINUX_GICR, LINUX_POLLING_PL011],
    }
    return expected


def expected_zephyr(canonical: dict[str, Any]) -> dict[str, Any]:
    expected = copy.deepcopy(canonical)
    expected["base"].update(
        {
            "id": 2,
            "name": "zephyr-qemu-dual",
            "phys_cpu_ids": [2],
        }
    )
    expected["devices"] = {
        "interrupt_mode": "passthrough",
        "emu_devices": [GICD, ZEPHYR_GICR, ZEPHYR_POLLING_PL011],
        "passthrough_devices": ZEPHYR_PATHS,
        "passthrough_addresses": [],
        "excluded_devices": ZEPHYR_EXCLUDED,
    }
    return expected


def check_vm_config(
    *,
    config: dict[str, Any],
    text: str,
    expected: dict[str, Any],
    label: str,
    expected_paths: list[list[str]],
    expected_excluded: list[list[str]],
    errors: list[str],
) -> None:
    if f"# status: {BLOCKED_STATUS}" not in text:
        errors.append(f"{label} does not declare the static blocker status")
    if "dtb_path" in config.get("kernel", {}):
        errors.append(f"{label} must use AxVisor-generated guest DTB")
    if config != expected:
        errors.append(f"{label} drifted from its canonical kernel/memory contract")

    devices = config.get("devices", {})
    paths = devices.get("passthrough_devices")
    excluded = devices.get("excluded_devices")
    check_path_only(paths, label=f"{label} passthrough_devices", errors=errors)
    check_path_only(excluded, label=f"{label} excluded_devices", errors=errors)
    if paths != expected_paths:
        errors.append(f"{label} has the wrong explicit FDT ownership paths")
    if excluded != expected_excluded:
        errors.append(f"{label} has the wrong fail-closed FDT exclusions")
    if devices.get("passthrough_addresses") != []:
        errors.append(f"{label} must not mix explicit address passthrough")


def check_topology_lock(errors: list[str]) -> None:
    if not TOPOLOGY_VALIDATOR.is_file():
        errors.append("dual-guest topology validator is missing")
        return
    try:
        topology = load_module("dual_guest_topology_lock", TOPOLOGY_VALIDATOR)
    except Exception as error:  # noqa: BLE001 - aggregate contract diagnostics.
        errors.append(f"dual-guest topology validator cannot be imported: {error}")
        return

    expected = {
        "linux-block": (0, 0x0A00_0000, 16, 48),
        "linux-net": (1, 0x0A00_0200, 17, 49),
        "zephyr-net": (2, 0x0A00_0400, 18, 50),
    }
    for device_id, locked in expected.items():
        layout = topology.EXPECTED_LAYOUT.get(device_id, {})
        slot = layout.get("slot")
        if not isinstance(slot, int):
            errors.append(f"topology lock has no integer slot for {device_id}")
            continue
        observed = (
            slot,
            topology.MMIO_BASE + slot * topology.MMIO_STRIDE,
            topology.GIC_SPI_OFFSET_BASE + slot,
            topology.GIC_SPI_INTID_BASE
            + topology.GIC_SPI_OFFSET_BASE
            + slot,
        )
        if observed != locked:
            errors.append(
                f"dual-guest config disagrees with live topology lock for {device_id}"
            )


def main() -> int:
    errors: list[str] = []

    linux, linux_text = read_toml(LINUX_DUAL, "Linux dual-guest VM config", errors)
    zephyr, zephyr_text = read_toml(
        ZEPHYR_DUAL, "Zephyr dual-guest VM config", errors
    )
    qemu, qemu_text = read_toml(QEMU_DUAL, "dual-guest outer QEMU config", errors)

    linux_canonical = tomllib.loads(LINUX_CANONICAL.read_text(encoding="utf-8"))
    zephyr_canonical = tomllib.loads(
        ZEPHYR_CANONICAL.read_text(encoding="utf-8")
    )

    if linux is not None and linux_text is not None:
        check_vm_config(
            config=linux,
            text=linux_text,
            expected=expected_linux(linux_canonical),
            label="Linux dual-guest VM config",
            expected_paths=LINUX_PATHS,
            expected_excluded=LINUX_EXCLUDED,
            errors=errors,
        )
    if zephyr is not None and zephyr_text is not None:
        check_vm_config(
            config=zephyr,
            text=zephyr_text,
            expected=expected_zephyr(zephyr_canonical),
            label="Zephyr dual-guest VM config",
            expected_paths=ZEPHYR_PATHS,
            expected_excluded=ZEPHYR_EXCLUDED,
            errors=errors,
        )

    if linux is not None and zephyr is not None:
        linux_cpus = set(linux["base"]["phys_cpu_ids"])
        zephyr_cpus = set(zephyr["base"]["phys_cpu_ids"])
        if linux["base"]["id"] != 1 or zephyr["base"]["id"] != 2:
            errors.append("dual guests must use distinct VM ids 1 and 2")
        if linux_cpus != {0, 1} or zephyr_cpus != {2}:
            errors.append("dual guests have the wrong pinned physical CPU sets")
        if linux_cpus & zephyr_cpus:
            errors.append("dual-guest physical CPU sets overlap")

    if qemu is not None and qemu_text is not None:
        if qemu.get("status") != BLOCKED_STATUS:
            errors.append("outer QEMU config does not expose blocked_dma_console")
        if qemu.get("args") != EXPECTED_QEMU_ARGS:
            errors.append("outer QEMU config drifted from the fixed slot/hub/MAC plan")
        if qemu.get("success_regex") != []:
            errors.append("blocked dual-guest QEMU config must have no success regex")
        if "shell_prefix" in qemu or "shell_init_cmd" in qemu:
            errors.append("blocked dual-guest QEMU config must not drive a guest shell")
        if "passed" in qemu_text.lower():
            errors.append("static dual-guest QEMU config claims a passed state")
        if "virtio-net-pci" in qemu_text:
            errors.append("dual-guest IP topology must use virtio-mmio, not PCI")

    check_topology_lock(errors)

    ci = CI.read_text(encoding="utf-8")
    if "python3 scripts/test/check_axvisor_dual_guest_configs.py" not in ci:
        errors.append("dual-guest static configuration contract is not wired into CI")

    if not errors:
        return 0

    print("AxVisor dual-guest static config contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
