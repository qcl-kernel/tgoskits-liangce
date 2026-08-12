#!/usr/bin/env python3
"""Static and behavioral contract for the dual-guest virtio-mmio probe."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROBE = WORKSPACE_ROOT / "scripts/contest/probe_qemu_aarch64_virtio_slots.sh"
VALIDATOR = WORKSPACE_ROOT / "scripts/contest/validate_dual_guest_topology.py"
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"

EXPECTED_DEVICES = (
    ("linux-block", "virtio-blk-device", 0, 0x0A00_0000, 16),
    ("linux-net", "virtio-net-device", 1, 0x0A00_0200, 17),
    ("zephyr-net", "virtio-net-device", 2, 0x0A00_0400, 18),
)


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def qtree_fixture() -> str:
    lines = ["bus: main-system-bus", "  type System"]
    for device_id, device_type, slot, base, _spi_offset in EXPECTED_DEVICES:
        lines.extend(
            (
                '  dev: virtio-mmio, id ""',
                f"    mmio {base:016x}/{0x200:016x}",
                f"    bus: virtio-mmio-bus.{slot}",
                "      type virtio-mmio-bus",
                f'      dev: {device_type}, id "{device_id}"',
            )
        )
    return "\n".join(
        (
            json.dumps({"QMP": {"version": {"qemu": {"major": 8, "minor": 2}}}}),
            json.dumps({"return": {}, "id": "capabilities"}),
            json.dumps({"return": "\n".join(lines) + "\n", "id": "qtree"}),
        )
    )


def dts_fixture() -> str:
    nodes = []
    for _device_id, _device_type, _slot, base, spi_offset in EXPECTED_DEVICES:
        nodes.append(
            f"""
    virtio_mmio@{base:x} {{
        dma-coherent;
        interrupts = <0x00 0x{spi_offset:x} 0x01>;
        reg = <0x00 0x{base:x} 0x00 0x200>;
        compatible = \"virtio,mmio\";
    }};
"""
        )
    return "/dts-v1/;\n/ {\n" + "".join(nodes) + "};\n"


def expect_overlap_rejected(
    errors: list[str],
    validator: ModuleType,
    devices: list[dict[str, object]],
    *,
    label: str,
    expected_message: str,
) -> None:
    try:
        validator.validate_topology(devices)
    except validator.TopologyError as error:
        if expected_message not in str(error):
            errors.append(
                f"dual-guest validator reports the wrong {label} error: {error}"
            )
    else:
        errors.append(f"dual-guest validator accepts {label}")


def check_validator(errors: list[str]) -> None:
    if not VALIDATOR.is_file():
        errors.append("dual-guest topology validator is missing")
        return

    try:
        validator = load_module("dual_guest_topology_validator", VALIDATOR)
    except Exception as error:  # noqa: BLE001 - report import failures together.
        errors.append(f"dual-guest topology validator cannot be imported: {error}")
        return

    required_api = ("TopologyError", "inspect_topology", "validate_topology")
    for name in required_api:
        if not hasattr(validator, name):
            errors.append(f"dual-guest topology validator does not expose {name}")
    if any(not hasattr(validator, name) for name in required_api):
        return

    try:
        devices = validator.inspect_topology(qtree_fixture(), dts_fixture())
        validator.validate_topology(devices)
    except Exception as error:  # noqa: BLE001 - preserve actionable contract output.
        errors.append(f"dual-guest validator rejects a valid probe fixture: {error}")
        return

    observed = {
        str(device.get("qemuId")): (
            device.get("qemuType"),
            device.get("slot"),
            device.get("mmioBase"),
            device.get("mmioSize"),
            device.get("spiOffset"),
            device.get("gicIntid"),
        )
        for device in devices
    }
    for device_id, device_type, slot, base, spi_offset in EXPECTED_DEVICES:
        expected = (device_type, slot, base, 0x200, spi_offset, spi_offset + 32)
        if observed.get(device_id) != expected:
            errors.append(
                f"dual-guest validator produced the wrong mapping for {device_id}: "
                f"{observed.get(device_id)!r}"
            )

    duplicate_slot = copy.deepcopy(devices)
    duplicate_slot[1]["slot"] = duplicate_slot[0]["slot"]
    duplicate_slot[1]["bus"] = duplicate_slot[0]["bus"]
    expect_overlap_rejected(
        errors,
        validator,
        duplicate_slot,
        label="slot overlap",
        expected_message="slot overlap",
    )

    overlapping_mmio = copy.deepcopy(devices)
    overlapping_mmio[1]["mmioBase"] = int(overlapping_mmio[0]["mmioBase"]) + 0x100
    expect_overlap_rejected(
        errors,
        validator,
        overlapping_mmio,
        label="MMIO overlap",
        expected_message="MMIO overlap",
    )

    duplicate_spi = copy.deepcopy(devices)
    duplicate_spi[1]["spiOffset"] = duplicate_spi[0]["spiOffset"]
    duplicate_spi[1]["gicIntid"] = duplicate_spi[0]["gicIntid"]
    expect_overlap_rejected(
        errors,
        validator,
        duplicate_spi,
        label="SPI overlap",
        expected_message="SPI overlap",
    )

    wrong_fixed_mapping = copy.deepcopy(devices)
    wrong_fixed_mapping[0]["mmioBase"] = 0x0A00_1000
    expect_overlap_rejected(
        errors,
        validator,
        wrong_fixed_mapping,
        label="non-canonical slot mapping",
        expected_message="fixed mapping mismatch",
    )


def check_probe_cli(errors: list[str]) -> None:
    """Exercise argument handling where a native Bash is available."""

    if sys.platform == "win32":
        return
    bash = shutil.which("bash")
    if bash is None:
        errors.append("native Bash is required to exercise the topology probe CLI")
        return

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [bash, str(PROBE), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )

    default = run("--output-dir", "unused-default", "--dry-run")
    if default.returncode != 0 or default.stdout.count("-m 8192M") != 2:
        errors.append(
            "topology probe dry-run does not apply the default 8 GiB host RAM"
        )

    explicit = run(
        "--output-dir",
        "unused-explicit",
        "--memory-mib",
        "12288",
        "--dry-run",
    )
    if explicit.returncode != 0 or explicit.stdout.count("-m 12288M") != 2:
        errors.append("topology probe dry-run ignores explicit host RAM")

    for invalid in ("0", "08", "8G", "10000000"):
        rejected = run(
            "--output-dir",
            "unused-invalid",
            "--memory-mib",
            invalid,
            "--dry-run",
        )
        if rejected.returncode != 2:
            errors.append(f"topology probe accepts invalid --memory-mib {invalid!r}")
        if "--memory-mib must be an integer" not in rejected.stderr:
            errors.append(
                f"topology probe reports the wrong --memory-mib {invalid!r} error"
            )


def main() -> int:
    errors: list[str] = []

    if not PROBE.is_file():
        errors.append("QEMU AArch64 virtio-mmio slot probe is missing")
    else:
        probe = PROBE.read_text(encoding="utf-8")
        required_probe_snippets = (
            "--output-dir <path>",
            "--memory-mib <MiB>",
            "MEMORY_MIB=8192",
            "--memory-mib must be an integer from 1 to 9999999",
            'MEMORY_ARG="${MEMORY_MIB}M"',
            "QEMU_MACHINE_ARGS=(",
            "human-monitor-command",
            "info qtree",
            "dumpdtb=",
            "id=linux-block,drive=linux-root,bus=virtio-mmio-bus.0",
            "id=linux-net,netdev=linux-netdev,bus=virtio-mmio-bus.1",
            "id=zephyr-net,netdev=zephyr-netdev,bus=virtio-mmio-bus.2",
            "hubport,id=linux-netdev,hubid=0",
            "hubport,id=zephyr-netdev,hubid=0",
            "mac=02:00:00:00:00:01",
            "mac=02:00:00:00:00:02",
            "validate_dual_guest_topology.py",
            "does not prove dual-guest boot or IP connectivity",
        )
        for snippet in required_probe_snippets:
            if snippet not in probe:
                errors.append(f"QEMU virtio-mmio probe is missing `{snippet}`")
        checksum_files = (
            "qemu-version.txt",
            "qemu-argv.txt",
            "qtree.qmp.jsonl",
            "qtree.stderr.log",
            "dumpdtb.stdout.log",
            "dumpdtb.stderr.log",
            "host.dtb",
            "host.dts",
            "dtc.stderr.log",
            "topology.json",
            "validator.stdout.json",
        )
        if "EVIDENCE_FILES=(" not in probe:
            errors.append("QEMU virtio-mmio probe has no evidence checksum inventory")
        else:
            inventory = probe.split("EVIDENCE_FILES=(", maxsplit=1)[1].split(
                ")", maxsplit=1
            )[0]
            for filename in checksum_files:
                if filename not in inventory:
                    errors.append(f"topology checksum inventory omits `{filename}`")
            if "checksums.sha256" in inventory:
                errors.append("topology checksum manifest must not hash itself")
        for snippet in (
            "require_command sha256sum",
            'sha256sum "${EVIDENCE_FILES[@]}" > checksums.sha256',
            "sha256sum -c checksums.sha256",
        ):
            if snippet not in probe:
                errors.append(f"QEMU virtio-mmio probe is missing `{snippet}`")
        if "virtio-net-pci" in probe:
            errors.append("dual-guest probe must use MMIO, not PCI, VirtIO-net")
        if "-m 1024M" in probe:
            errors.append("dual-guest probe must not retain the old 1 GiB host RAM")
        if probe.count('-m "$MEMORY_ARG"') != 1:
            errors.append(
                "dual-guest probe must define the selected RAM in one shared argument set"
            )
        if probe.count('"${QEMU_MACHINE_ARGS[@]}"') != 3:
            errors.append(
                "dual-guest probe must share machine arguments across both dry-run "
                "commands and real captures"
            )
        if "topology_validated" in probe:
            errors.append(
                "probe script must not contain a hand-written validated result"
            )

    check_validator(errors)
    check_probe_cli(errors)

    ci = CI.read_text(encoding="utf-8")
    expected_ci_command = "python3 scripts/test/check_axvisor_dual_guest_topology.py"
    if expected_ci_command not in ci:
        errors.append("dual-guest topology contract is not wired into CI")

    if not errors:
        return 0

    print("AxVisor dual-guest topology contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
