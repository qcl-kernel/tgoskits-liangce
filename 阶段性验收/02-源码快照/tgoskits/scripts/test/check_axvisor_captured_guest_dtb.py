#!/usr/bin/env python3
"""Behavioral checks for captured Guest-DTB static semantic validation."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts/contest/validate_captured_guest_dtb.py"
README = ROOT / "scripts/contest/README.md"
CI = ROOT / ".github/workflows/ci.yml"

DTS = """/dts-v1/;
/ {
  #address-cells = <2>;
  #size-cells = <2>;
  interrupt-parent = <5>;
  intc@8000000 { phandle = <5>; compatible = "arm,gic-v3"; interrupt-controller; #interrupt-cells = <3>; };
  cpus { #address-cells = <1>; #size-cells = <0>;
    cpu-map { cluster0 { core0 { cpu = <6>; }; }; };
    cpu@0 { phandle = <6>; device_type = "cpu"; reg = <0>; status = "okay"; };
  };
  memory@40000000 { device_type = "memory"; reg = <0 0x40000000 0 0x08000000>; };
  aliases { serial0 = "/pl011@9000000"; };
  chosen {
    bootargs = "earlycon=pl011,mmio32,0x9000000";
    stdout-path = "serial0:115200n8";
    linux,stdout-path = "serial0:115200n8";
  };
  pl011@9000000 {
    compatible = "arm,pl011", "arm,primecell";
    reg = <0 0x09000000 0 0x1000>;
    reg-io-width = <4>;
    current-speed = <115200>;
    status = "okay";
  };
};
"""
TOML = """[base]
id = 1
cpu_num = 1
phys_cpu_ids = [0]
[kernel]
memory_regions = [[0x40000000, 0x08000000, 7, 1]]
[devices]
emu_devices = [
  ["gppt-gicd", 0x08000000, 0x10000, 0, 0x21, []],
  ["gppt-gicr", 0x080a0000, 0x20000, 0, 0x20, [1, 0x20000, 0]],
  ["zephyr", 0x09000000, 0x1000, 0, 0x2, []],
]
"""
PASSTHROUGH_TOML = """[base]
id = 1
cpu_num = 1
phys_cpu_ids = [0]
[kernel]
memory_regions = [[0x40000000, 0x08000000, 7, 1]]
[devices]
emu_devices = [
  ["gppt-gicd", 0x08000000, 0x10000, 0, 0x21, []],
  ["gppt-gicr", 0x080a0000, 0x20000, 0, 0x20, [1, 0x20000, 0]],
]
"""
PASSTHROUGH_DTS = DTS.replace(
    '    reg-io-width = <4>;\n    current-speed = <115200>;\n',
    '    interrupts = <0 1 4>;\n    clocks = <7>;\n',
)


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("captured_guest_dtb_validator", VALIDATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {VALIDATOR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rejected(errors: list[str], module: ModuleType, dts: str, label: str) -> None:
    try:
        module.validate_artifacts(dts.encode(), TOML.encode())
    except module.CapturedGuestDtbError:
        return
    errors.append(f"validator accepts {label}")


def rejected_config(
    errors: list[str], module: ModuleType, toml: str, label: str
) -> None:
    try:
        module.validate_artifacts(DTS.encode(), toml.encode())
    except module.CapturedGuestDtbError:
        return
    errors.append(f"validator accepts {label}")


def rejected_passthrough(
    errors: list[str], module: ModuleType, dts: str, label: str
) -> None:
    try:
        module.validate_artifacts(dts.encode(), PASSTHROUGH_TOML.encode())
    except module.CapturedGuestDtbError:
        return
    errors.append(f"validator accepts passthrough profile with {label}")


class MemoryOutput:
    """Minimal exclusive-create output double without a sandbox-dependent temp dir."""

    def __init__(self) -> None:
        self.data: bytes | None = None

    def open(self, mode: str):
        if mode != "xb" or self.data is not None:
            raise FileExistsError("already published")
        owner = self

        class Writer(io.BytesIO):
            def __exit__(self, *unused: object) -> None:
                owner.data = self.getvalue()
                self.close()

        return Writer()

    def __str__(self) -> str:
        return "memory-output.json"


def main() -> int:
    errors: list[str] = []
    if not VALIDATOR.is_file():
        errors.append("captured Guest-DTB semantic validator is missing")
    elif len(VALIDATOR.read_text(encoding="utf-8").splitlines()) >= 800:
        errors.append("captured Guest-DTB semantic validator must remain below 800 lines")
    else:
        module = load_module()
        try:
            profile = module.validate_artifacts(DTS.encode(), TOML.encode())
        except Exception as error:  # noqa: BLE001 - aggregate all diagnostics.
            errors.append(f"validator rejects the synthetic valid DTS: {error}")
        else:
            if (
                profile.get("id") != 1
                or profile.get("cpuNum") != 1
                or profile.get("consoleMode") != "vm-owned-polling-pl011"
            ):
                errors.append("validator does not return the matched VM identity")

        try:
            module.validate_artifacts(
                DTS.replace(
                    '  pl011@9000000 {',
                    '  reserved-memory-device { compatible = "example,reserved-device"; };\n'
                    '  pl011@9000000 {',
                ).encode(),
                TOML.encode(),
            )
        except Exception as error:  # noqa: BLE001 - aggregate all diagnostics.
            errors.append(f"validator rejects non-reserved-memory root node: {error}")

        try:
            passthrough_profile = module.validate_artifacts(
                PASSTHROUGH_DTS.encode(), PASSTHROUGH_TOML.encode()
            )
        except Exception as error:  # noqa: BLE001 - aggregate all diagnostics.
            errors.append(f"validator rejects physical passthrough PL011: {error}")
        else:
            if passthrough_profile.get("consoleMode") != "passthrough-or-no-vm-owned-console":
                errors.append("validator does not report passthrough console mode")

        cases = {
            "an invalid serial0 alias": DTS.replace('serial0 = "/pl011@9000000"', 'serial0 = "/wrong"'),
            "an ITS node": DTS.replace('  pl011@9000000 {', '  its@8080000 { compatible = "arm,gic-v3-its"; };\n  pl011@9000000 {'),
            "a reserved-memory axvisor carveout": DTS.replace(
                '  pl011@9000000 {',
                '  reserved-memory { #address-cells = <2>; #size-cells = <2>;\n'
                '    vm-carveout@80000000 { compatible = "axvisor,vm-carveout-v1"; reg = <0 0x80000000 0 0x1000>; };\n'
                '  };\n  pl011@9000000 {',
            ),
            "an ordinary reserved-memory child": DTS.replace(
                '  pl011@9000000 {',
                '  reserved-memory { #address-cells = <2>; #size-cells = <2>;\n'
                '    framebuffer@80000000 { reg = <0 0x80000000 0 0x1000>; };\n'
                '  };\n  pl011@9000000 {',
            ),
            "PL011 interrupts": DTS.replace('    status = "okay";', '    status = "okay"; interrupts = <0 1 4>;'),
            "PL011 extended interrupts": DTS.replace(
                '    status = "okay";',
                '    status = "okay"; interrupts-extended = <5 0 1 4>;'
            ),
            "PL011 DMA properties": DTS.replace('    status = "okay";', '    status = "okay"; dmas = <5 1>; dma-names = "rx";'),
            "a mismatched CPU MPIDR": DTS.replace('reg = <0>; status = "okay";', 'reg = <1>; status = "okay";'),
            "a CPU node without device_type": DTS.replace(
                'device_type = "cpu"; ', ""
            ),
            "a dangling cpu-map reference": DTS.replace(
                "cpu = <6>;", "cpu = <7>;"
            ),
            "a duplicate cpu-map reference": DTS.replace(
                "core0 { cpu = <6>; };",
                "core0 { cpu = <6>; }; core1 { cpu = <6>; };",
            ),
            "a missing root interrupt-parent": DTS.replace(
                "  interrupt-parent = <5>;\n", ""
            ),
            "a non-GICv3 root interrupt-parent": DTS.replace(
                'compatible = "arm,gic-v3";', 'compatible = "example,intc";'
            ),
            "a wrong GIC interrupt cell width": DTS.replace(
                "#interrupt-cells = <3>;", "#interrupt-cells = <2>;"
            ),
            "a malformed inherited interrupt specifier": DTS.replace(
                "  memory@40000000 {",
                "  timer { interrupts = <1 2>; };\n  memory@40000000 {",
            ),
            "an unresolved interrupt-parent phandle": DTS.replace('interrupt-parent = <5>;', 'interrupt-parent = <6>;'),
            "a non-controller interrupt-parent": DTS.replace('interrupt-controller; #interrupt-cells = <3>;', '#interrupt-cells = <3>;'),
        }
        for label, changed in cases.items():
            rejected(errors, module, changed, label)
        for label, changed in {
            "a dangling stdout-path": PASSTHROUGH_DTS.replace(
                'stdout-path = "serial0:115200n8";',
                'stdout-path = "/missing-console";',
            ),
            "an ITS node": PASSTHROUGH_DTS.replace(
                '  pl011@9000000 {',
                '  its@8080000 { compatible = "arm,gic-v3-its"; };\n  pl011@9000000 {',
            ),
        }.items():
            rejected_passthrough(errors, module, changed, label)
        for label, changed in {
            "a zero VM id": TOML.replace("id = 1", "id = 0"),
            "a multi-vCPU configuration": TOML.replace("cpu_num = 1", "cpu_num = 2"),
            "a mismatched VM memory region": TOML.replace("0x08000000", "0x04000000"),
            "a malformed emulated-device tuple": TOML.replace(
                '["zephyr", 0x09000000, 0x1000, 0, 0x2, []]',
                '["zephyr", 0x09000000, 0x1000, 0, 0x2]',
            ),
            "two VM-owned consoles": TOML.replace(
                '  ["zephyr", 0x09000000, 0x1000, 0, 0x2, []],',
                '  ["zephyr", 0x09000000, 0x1000, 0, 0x2, []],\n'
                '  ["other", 0x09000000, 0x1000, 0, 0x2, []],',
            ),
            "a VM-owned console with a delivered IRQ": TOML.replace(
                '["zephyr", 0x09000000, 0x1000, 0, 0x2, []]',
                '["zephyr", 0x09000000, 0x1000, 1, 0x2, []]',
            ),
            "a VM-owned console at the wrong address": TOML.replace(
                '["zephyr", 0x09000000, 0x1000, 0, 0x2, []]',
                '["zephyr", 0x09001000, 0x1000, 0, 0x2, []]',
            ),
        }.items():
            rejected_config(errors, module, changed, label)

        output = MemoryOutput()
        try:
            profile = module.validate_artifacts(DTS.encode(), TOML.encode())
            report = module.build_result(dts_path=Path("guest.dts"), dts_bytes=DTS.encode(), vm_config_path=Path("vm.toml"), vm_config_bytes=TOML.encode(), profile=profile)
            module.publish_result(output, report)
            written = json.loads((output.data or b"").decode("utf-8"))
            if written["sources"]["dts"]["sha256"] != module._sha256(DTS.encode()):
                errors.append("result does not bind the raw DTS SHA-256")
            if "Guest boot" not in written["doesNotProve"]:
                errors.append("result overstates its static proof scope")
            passthrough_report = module.build_result(
                dts_path=Path("linux.dts"),
                dts_bytes=PASSTHROUGH_DTS.encode(),
                vm_config_path=Path("linux.toml"),
                vm_config_bytes=PASSTHROUGH_TOML.encode(),
                profile=passthrough_profile,
            )
            if "passthrough console isolation" not in passthrough_report["doesNotProve"]:
                errors.append("passthrough report overstates console isolation")
            original = output.data
            try:
                module.publish_result(output, copy.deepcopy(report))
            except module.CapturedGuestDtbError:
                pass
            else:
                errors.append("validator overwrites existing JSON evidence")
            if output.data != original:
                errors.append("failed no-overwrite publication changes existing evidence")
        except Exception as error:  # noqa: BLE001 - aggregate all diagnostics.
            errors.append(f"validator cannot publish a valid static report: {error}")

    if README.is_file():
        text = README.read_text(encoding="utf-8")
        for token in ("validate_captured_guest_dtb.py", "does not prove QMP capture identity"):
            if token not in text:
                errors.append(f"README omits captured-DTB semantic validation guidance `{token}`")
    else:
        errors.append("contest README is missing")
    if "python3 scripts/test/check_axvisor_captured_guest_dtb.py" not in CI.read_text(encoding="utf-8"):
        errors.append("captured Guest-DTB semantic validator is not wired into CI")

    if not errors:
        return 0
    print("AxVisor captured Guest-DTB semantic validation check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
