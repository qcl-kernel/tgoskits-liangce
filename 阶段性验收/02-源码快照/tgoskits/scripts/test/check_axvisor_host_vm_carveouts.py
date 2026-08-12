#!/usr/bin/env python3
"""Behavioral contract for host-DTS VM carveout artifact validation."""

from __future__ import annotations

import contextlib
import io
import importlib.util
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = WORKSPACE_ROOT / "scripts/contest/validate_host_vm_carveouts.py"
SOMEBOOT_MEMORY = WORKSPACE_ROOT / "platforms/someboot/src/fdt/memory.rs"
SOMEBOOT_MEM_MOD = WORKSPACE_ROOT / "platforms/someboot/src/mem/mod.rs"
SOMEBOOT_ENTRY = WORKSPACE_ROOT / "platforms/someboot/src/entry/mod.rs"

RUNTIME_MARKER = (
    "AXVISOR_HOST_VM_CARVEOUT_RESERVED "
    "vm={} hpa={:#x} size={:#x} phase=before-ram-init"
)

RAM_BASE = 0x4000_0000
RAM_SIZE = 0x2_0000_0000
CARVEOUT_BASE = 0x1_0000_0000
CARVEOUT_SIZE = 0x0800_0000


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    module_directory = str(path.parent)
    sys.path.insert(0, module_directory)
    try:
        spec.loader.exec_module(module)
    finally:
        if sys.path[0] == module_directory:
            sys.path.pop(0)
    return module


def cells64(value: int) -> str:
    return f"0x{value >> 32:x} 0x{value & 0xFFFF_FFFF:x}"


def carveout_node(
    *,
    vm_id: int = 2,
    base: int = CARVEOUT_BASE,
    size: int = CARVEOUT_SIZE,
    compatible: str = "axvisor,vm-carveout-v1",
    extra_properties: str = "",
    reg: str | None = None,
) -> str:
    reg_value = reg or f"{cells64(base)} {cells64(size)}"
    return f"""
        vm{vm_id}@{base:x} {{
            compatible = "{compatible}";
            axvisor,vm-id = <0x{vm_id:x}>;
            reg = <{reg_value}>;
            {extra_properties}
        }};
"""


def dma_guard_node(
    *,
    base: int = 0x1800_0000_0,
    size: int = 0x0020_0000,
    compatible: str = "axvisor,dma-guard-v1",
    extra_properties: str = "",
) -> str:
    return f"""
        dma-guard@{base:x} {{
            compatible = "{compatible}";
            reg = <{cells64(base)} {cells64(size)}>;
            no-map;
            {extra_properties}
        }};
"""


def host_dts(*nodes: str, ram_base: int = RAM_BASE, ram_size: int = RAM_SIZE) -> str:
    body = "\n".join(nodes or (carveout_node(),))
    return f"""/dts-v1/;

/ {{
    #address-cells = <0x2>;
    #size-cells = <0x2>;

    memory@{ram_base:x} {{
        device_type = "memory";
        reg = <{cells64(ram_base)} {cells64(ram_size)}>;
    }};

    reserved-memory {{
        #address-cells = <0x2>;
        #size-cells = <0x2>;
        ranges;
        {body}
    }};
}};
"""


def with_top_level(dts: str, statement: str) -> str:
    return dts.replace("/dts-v1/;", f"/dts-v1/;\n{statement}", 1)


def vm_toml(
    *,
    vm_id: int = 2,
    base: int = CARVEOUT_BASE,
    size: int = CARVEOUT_SIZE,
    flags: int = 0x7,
    map_type: int = 2,
) -> str:
    return f"""[base]
id = {vm_id}
name = "vm-{vm_id}"
vm_type = 1
cpu_num = 1

[kernel]
entry_point = {base:#x}
kernel_path = "/guest/vm-{vm_id}.bin"
kernel_load_addr = {base:#x}
memory_regions = [
  [{base:#x}, {size:#x}, {flags}, {map_type}],
]

[devices]
emu_devices = []
passthrough_devices = []
"""


def expect_rejected(
    errors: list[str],
    validator: ModuleType,
    dts: str,
    configs: dict[str, str],
    *,
    label: str,
    expected_message: str,
    expected_dma_guards: list[dict[str, int]] | None = None,
) -> None:
    try:
        validator.validate_artifacts(
            dts, configs, expected_dma_guards=expected_dma_guards
        )
    except validator.CarveoutArtifactError as error:
        if expected_message not in str(error):
            errors.append(f"validator reports the wrong {label} error: {error}")
    else:
        errors.append(f"validator accepts {label}")


def check_runtime_marker_contract(errors: list[str]) -> None:
    """Check the narrow boot-time marker and its source-level ordering contract."""
    try:
        memory_source = SOMEBOOT_MEMORY.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"cannot read someboot FDT memory source: {error}")
        return

    try:
        mem_mod_source = SOMEBOOT_MEM_MOD.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"cannot read someboot memory initialization source: {error}")
        return

    try:
        entry_source = SOMEBOOT_ENTRY.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"cannot read someboot primary-entry source: {error}")
        return

    parse_match = re.search(
        r"let carveouts\s*=\s*super::vm_carveout::parse_vm_carveouts_or_panic\("
        r"fdt\.clone\(\)\);",
        memory_source,
    )
    loop_match = re.search(r"for &carveout in &carveouts \{", memory_source)
    reservation_match = re.search(
        r"add_memory_descriptor\(carveout\.memory_descriptor\(\)\)"
        r"\.unwrap_or_else\(\|error\| \{.*?\}\);",
        memory_source,
        flags=re.DOTALL,
    )
    marker_match = re.search(
        r"println!\(\s*\""
        + re.escape(RUNTIME_MARKER)
        + r"\"\s*,\s*carveout\.vm_id\s*,\s*carveout\.physical_start\s*,"
        r"\s*carveout\.size\s*,\s*\);",
        memory_source,
        flags=re.DOTALL,
    )
    loop_contract = None
    if reservation_match is not None and marker_match is not None:
        loop_contract = re.search(
            r"for &carveout in &carveouts \{\s*"
            + reservation_match.re.pattern
            + r"\s*"
            + marker_match.re.pattern
            + r"\s*\}",
            memory_source,
            flags=re.DOTALL,
        )

    if parse_match is None:
        errors.append("someboot does not strictly parse VM carveouts before reservation")
    if loop_match is None:
        errors.append("someboot does not iterate the strictly parsed VM carveouts")
    if reservation_match is None:
        errors.append("someboot does not reserve each VM carveout descriptor")
    if marker_match is None:
        errors.append("someboot omits the exact host VM carveout runtime marker")
    elif memory_source.count(RUNTIME_MARKER) != 1:
        errors.append("someboot runtime marker format is not emitted exactly once")
    if loop_contract is None:
        errors.append(
            "someboot must emit the carveout marker in each reservation loop after success"
        )

    if all(
        match is not None
        for match in (parse_match, loop_match, reservation_match, marker_match, loop_contract)
    ):
        assert parse_match is not None
        assert loop_match is not None
        assert reservation_match is not None
        assert marker_match is not None
        assert loop_contract is not None
        if not (
            parse_match.start()
            < loop_match.start()
            == loop_contract.start()
        ):
            errors.append(
                "someboot must parse VM carveouts before the reservation-and-marker loop"
            )

    early_init_match = re.search(
        r"pub\(crate\) fn early_init\(\) \{(?P<body>.*?)\n\}",
        mem_mod_source,
        flags=re.DOTALL,
    )
    if early_init_match is None:
        errors.append("someboot early memory initialization function is missing")
        return

    early_init_body = early_init_match.group("body")
    init_memory_map_at = early_init_body.find("crate::fdt::init_memory_map();")
    ram_init_at = early_init_body.find("ram::init(")
    if init_memory_map_at == -1 or ram_init_at == -1:
        errors.append("someboot does not expose the expected memory initialization sequence")
    elif init_memory_map_at > ram_init_at:
        errors.append("someboot initializes RAM before completing the FDT memory map")

    primary_init_match = re.search(
        r"pub fn primary_init_early\([^)]*\) \{(?P<body>.*?)\n\}",
        entry_source,
        flags=re.DOTALL,
    )
    if primary_init_match is None:
        errors.append("someboot primary early initialization function is missing")
        return

    primary_init_body = primary_init_match.group("body")
    setup_earlycon_at = primary_init_body.find("crate::fdt::setup_earlycon();")
    memory_early_init_at = primary_init_body.find("crate::mem::early_init();")
    if setup_earlycon_at == -1 or memory_early_init_at == -1:
        errors.append("someboot does not expose the expected early-console sequence")
    elif setup_earlycon_at > memory_early_init_at:
        errors.append(
            "someboot initializes the FDT memory map before setting up early console"
        )


def main() -> int:
    errors: list[str] = []

    check_runtime_marker_contract(errors)

    if not VALIDATOR.is_file():
        errors.append("host VM carveout artifact validator is missing")
    else:
        try:
            validator = load_module("host_vm_carveout_validator", VALIDATOR)
        except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
            errors.append(f"host VM carveout validator cannot be imported: {error}")
        else:
            for name in (
                "CarveoutArtifactError",
                "validate_artifacts",
                "build_payload",
                "parse_args",
                "main",
            ):
                if not hasattr(validator, name):
                    errors.append(f"host VM carveout validator does not expose {name}")

            if not errors:
                configs = {"vm-2.toml": vm_toml()}
                try:
                    dts = host_dts()
                    dts_bytes = dts.replace("\n", "\r\n").encode("utf-8")
                    config_bytes = {
                        name: text.encode("utf-8") for name, text in configs.items()
                    }
                    carveouts = validator.validate_artifacts(dts, configs)
                    payload = validator.build_payload(
                        carveouts=carveouts,
                        dts_name="host-with-carveout.dts",
                        dts_bytes=dts_bytes,
                        vm_config_bytes=config_bytes,
                    )
                except Exception as error:  # noqa: BLE001 - preserve diagnosis.
                    errors.append(
                        f"validator rejects valid carveout artifacts: {error}"
                    )
                else:
                    if len(carveouts) != 1:
                        errors.append("validator does not return exactly one carveout")
                    else:
                        observed = carveouts[0]
                        expected = {
                            "vmId": 2,
                            "hpa": CARVEOUT_BASE,
                            "size": CARVEOUT_SIZE,
                            "end": CARVEOUT_BASE + CARVEOUT_SIZE,
                            "vmConfig": "vm-2.toml",
                        }
                        if observed != expected:
                            errors.append(
                                f"validator returns the wrong carveout: {observed!r}"
                            )
                    if payload.get("status") != "carveout_artifacts_validated":
                        errors.append(
                            "payload does not expose its narrow static status"
                        )
                    if (
                        payload.get("proofScope")
                        != "host-dts-vm-carveout-static-contract"
                    ):
                        errors.append("payload has the wrong proof scope")
                    boundaries = set(payload.get("doesNotProve", []))
                    for boundary in (
                        "host allocator excluded the carveout",
                        "AxVM stage-2 maps the expected HPA",
                        "passthrough DMA is isolated",
                        "Linux and Zephyr have IP connectivity",
                        "decoded DTS is the DTB supplied to AxVisor",
                        "VM TOML was deserialized by the current AxVM build",
                    ):
                        if boundary not in boundaries:
                            errors.append(f"payload omits proof boundary `{boundary}`")
                    observed_hash = (
                        payload.get("sources", {}).get("hostDts", {}).get("sha256")
                    )
                    if observed_hash != hashlib.sha256(dts_bytes).hexdigest():
                        errors.append(
                            "payload does not hash the exact DTS source bytes"
                        )
                    if payload.get("dmaGuards") != []:
                        errors.append("unrequested DMA guards leak into the preflight payload")

                try:
                    guards = validator.parse_dma_guard_specs(["0x180000000:0x200000"])
                    address_only_nodes = """    cpus {
        #address-cells = <0x1>;
        #size-cells = <0x0>;
        cpu@0 {
            device_type = "cpu";
            reg = <0x0>;
        };
    };

    identifiers {
        #address-cells = <0x1>;
        #size-cells = <0x0>;
        token@7 { reg = <0x7>; };
    };

"""
                    guarded_dts = host_dts(carveout_node(), dma_guard_node()).replace(
                        "    reserved-memory {", address_only_nodes + "    reserved-memory {", 1
                    )
                    guarded_carveouts = validator.validate_artifacts(
                        guarded_dts, configs, expected_dma_guards=guards
                    )
                    guarded_payload = validator.build_payload(
                        carveouts=guarded_carveouts,
                        dts_name="guarded.dts",
                        dts_bytes=guarded_dts.encode(),
                        vm_config_bytes=config_bytes,
                        dma_guards=guards,
                    )
                except Exception as error:  # noqa: BLE001 - aggregate contract failures.
                    errors.append(f"validator rejects valid non-VM DMA guard: {error}")
                else:
                    if guarded_payload.get("dmaGuards") != [
                        {"hpa": "0x180000000", "size": "0x200000", "end": "0x180200000"}
                    ]:
                        errors.append("DMA guard is not separately recorded in the payload")
                    if any("vmId" in guard for guard in guarded_payload["dmaGuards"]):
                        errors.append("DMA guard is incorrectly recorded as a VM carveout")
                    for boundary in (
                        "QEMU used a DMA guard reservation",
                        "DMA guard changed host allocation or DMA behavior",
                        "DMA isolation",
                    ):
                        if boundary not in guarded_payload.get("doesNotProve", []):
                            errors.append(f"DMA guard payload omits boundary `{boundary}`")

                for value, label in (
                    ("0:0x200000", "zero DMA guard HPA"),
                    ("0x18001000:0x200000", "unaligned DMA guard"),
                    ("0xffffffffffffffff:0x200000", "overflowing DMA guard"),
                ):
                    try:
                        validator.parse_dma_guard_specs([value])
                    except validator.CarveoutArtifactError:
                        pass
                    else:
                        errors.append(f"validator accepts {label}")
                try:
                    validator.parse_dma_guard_specs(
                        ["0x1a200000:0x200000", "0x1a000000:0x400000"]
                    )
                except validator.CarveoutArtifactError:
                    pass
                else:
                    errors.append("validator misses non-HPA-ordered DMA guard overlap")

                guard_expectation = validator.parse_dma_guard_specs(["0x180000000:0x200000"])
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node()),
                    configs,
                    label="missing expected DMA guard",
                    expected_message="do not exactly match --dma-guard",
                    expected_dma_guards=guard_expectation,
                )
                malformed_mmio_dts = host_dts(carveout_node(), dma_guard_node()).replace(
                    "    reserved-memory {",
                    f"""    serial@1a0000000 {{
        reg = <{cells64(0x1A00_0000_0)} {cells64(0)}>;
    }};

    reserved-memory {{""",
                    1,
                )
                expect_rejected(
                    errors,
                    validator,
                    malformed_mmio_dts,
                    configs,
                    label="zero-sized root MMIO",
                    expected_message="DTS node 'serial@1a0000000' has a zero-sized range",
                    expected_dma_guards=guard_expectation,
                )
                guard_mmio_dts = host_dts(carveout_node(), dma_guard_node()).replace(
                    "    reserved-memory {",
                    f"""    serial@180000000 {{
        reg = <{cells64(0x1800_0000_0)} {cells64(0x0020_0000)}>;
    }};

    reserved-memory {{""",
                    1,
                )
                expect_rejected(
                    errors,
                    validator,
                    guard_mmio_dts,
                    configs,
                    label="DMA guard overlapping root MMIO",
                    expected_message="overlaps reserved 'MMIO serial@180000000'",
                    expected_dma_guards=guard_expectation,
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(), dma_guard_node()),
                    configs,
                    label="extra DMA guard",
                    expected_message="do not exactly match --dma-guard",
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(), dma_guard_node(extra_properties="no-map = <0x1>;")),
                    configs,
                    label="nonempty DMA guard no-map",
                    expected_message="duplicate no-map properties",
                    expected_dma_guards=guard_expectation,
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(), dma_guard_node().replace("no-map;", "")),
                    configs,
                    label="DMA guard without no-map",
                    expected_message="has no no-map property",
                    expected_dma_guards=guard_expectation,
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(
                        carveout_node(),
                        dma_guard_node(extra_properties="axvisor,vm-id = <0x2>;"),
                    ),
                    configs,
                    label="VM-id polluted DMA guard",
                    expected_message="must not have axvisor,vm-id",
                    expected_dma_guards=guard_expectation,
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(
                        carveout_node(),
                        dma_guard_node(compatible="axvisor,vm-carveout-v1"),
                    ),
                    configs,
                    label="DMA guard posing as VM carveout",
                    expected_message="confused compatible/name",
                    expected_dma_guards=guard_expectation,
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(
                        carveout_node(),
                        dma_guard_node(extra_properties="size = <0x0 0x200000>;"),
                    ),
                    configs,
                    label="dynamic DMA guard",
                    expected_message="mixes reg with dynamic allocation properties",
                    expected_dma_guards=guard_expectation,
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(), dma_guard_node(base=CARVEOUT_BASE)),
                    configs,
                    label="DMA guard overlapping VM carveout",
                    expected_message="overlaps VM 2 carveout",
                    expected_dma_guards=validator.parse_dma_guard_specs(
                        [f"{CARVEOUT_BASE:#x}:0x200000"]
                    ),
                )
                expect_rejected(
                    errors,
                    validator,
                    with_top_level(
                        host_dts(carveout_node(), dma_guard_node()),
                        "/memreserve/ 0x180000000 0x200000;",
                    ),
                    configs,
                    label="DMA guard overlapping memreserve",
                    expected_message="overlaps reserved '/memreserve/ entry 0'",
                    expected_dma_guards=guard_expectation,
                )

                try:
                    validator.validate_artifacts(
                        with_top_level(host_dts(), "/memreserve/ 0X300000000 0X1000;"),
                        configs,
                    )
                except Exception as error:  # noqa: BLE001 - preserve diagnosis.
                    errors.append(
                        f"validator rejects uppercase-hex /memreserve/: {error}"
                    )

                expect_rejected(
                    errors,
                    validator,
                    with_top_level(
                        host_dts(),
                        "/memreserve/ 0X100000000 0X08000000;",
                    ),
                    configs,
                    label="uppercase-hex overlapping /memreserve/",
                    expected_message="overlaps reserved '/memreserve/ entry 0'",
                )
                expect_rejected(
                    errors,
                    validator,
                    with_top_level(host_dts(), "/memreserve/ garbage;"),
                    configs,
                    label="malformed /memreserve/",
                    expected_message="unsupported /memreserve/ syntax",
                )
                expect_rejected(
                    errors,
                    validator,
                    with_top_level(host_dts(), "/memreserve/ 0X300000000ULL 0X1000;"),
                    configs,
                    label="unsupported /memreserve/ integer suffix",
                    expected_message="unsupported /memreserve/ syntax",
                )
                expect_rejected(
                    errors,
                    validator,
                    with_top_level(host_dts(), "/plugin/;"),
                    configs,
                    label="unsupported top-level DTS directive",
                    expected_message="unsupported top-level DTS statement",
                )

                ordinary_mixed = f"""
        ordinary@{CARVEOUT_BASE:x} {{
            compatible = "example,ordinary";
            reg = <{cells64(CARVEOUT_BASE)} {cells64(CARVEOUT_SIZE)}>;
            size = <{cells64(CARVEOUT_SIZE)}>;
        }};
"""
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(), ordinary_mixed),
                    configs,
                    label="ordinary reg plus dynamic size",
                    expected_message="mixes reg with dynamic allocation properties",
                )
                duplicate_carveout_reg = carveout_node(
                    extra_properties=(
                        f"reg = <{cells64(CARVEOUT_BASE)} {cells64(CARVEOUT_SIZE)}>;"
                    )
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(duplicate_carveout_reg),
                    configs,
                    label="duplicate carveout reg property",
                    expected_message="duplicate reg properties",
                )

                ordinary_dynamic = f"""
        pool {{
            compatible = "shared-dma-pool";
            size = <{cells64(CARVEOUT_SIZE)}>;
        }};
"""
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(), ordinary_dynamic),
                    configs,
                    label="unknown dynamic reserved-memory node",
                    expected_message="dynamic reserved-memory node",
                )
                ordinary_alloc_ranges = f"""
        pool {{
            compatible = "shared-dma-pool";
            size = <{cells64(CARVEOUT_SIZE)}>;
            alloc-ranges = <{cells64(RAM_BASE)} {cells64(RAM_SIZE)}>;
        }};
"""
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(), ordinary_alloc_ranges),
                    configs,
                    label="alloc-ranges dynamic reserved-memory node",
                    expected_message="dynamic reserved-memory node",
                )

                nested_overlap = f"""
        container {{
            #address-cells = <0x2>;
            #size-cells = <0x2>;
            nested@{CARVEOUT_BASE:x} {{
                reg = <{cells64(CARVEOUT_BASE)} {cells64(CARVEOUT_SIZE)}>;
            }};
        }};
"""
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(), nested_overlap),
                    configs,
                    label="nested reserved-memory overlap",
                    expected_message="overlaps reserved 'nested@100000000'",
                )
                nested_safe = f"""
        container {{
            #address-cells = <0x2>;
            #size-cells = <0x2>;
            nested@50000000 {{
                reg = <{cells64(0x5000_0000)} {cells64(0x20_0000)}>;
            }};
        }};
"""
                try:
                    validator.validate_artifacts(
                        host_dts(carveout_node(), nested_safe), configs
                    )
                except Exception as error:  # noqa: BLE001 - preserve diagnosis.
                    errors.append(f"validator rejects safe nested reservation: {error}")
                wrong_nested_cells = """
        container {
            #address-cells = <0x1>;
            #size-cells = <0x2>;
        };
"""
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(), wrong_nested_cells),
                    configs,
                    label="nested reserved-memory cell-width change",
                    expected_message="changes cell width",
                )

                expect_rejected(
                    errors,
                    validator,
                    host_dts(),
                    {"vm-2.toml": vm_toml(flags=-1)},
                    label="negative TOML mapping flags",
                    expected_message="flags must be a usize",
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(),
                    {"vm-2.toml": vm_toml(flags=0x40)},
                    label="unknown TOML mapping flag bits",
                    expected_message="flags contain unknown bits",
                )
                invalid_map_type = vm_toml().replace(
                    "memory_regions = [",
                    "memory_regions = [\n  [0x50000000, 0x200000, 0x7, 3],",
                    1,
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(),
                    {"vm-2.toml": invalid_map_type},
                    label="unknown TOML map type",
                    expected_message="map type must be 0, 1, or 2",
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(),
                    {"vm-2.toml": vm_toml().replace("[devices]", "[ignored]")},
                    label="missing AxVM devices table",
                    expected_message="has no [devices] table",
                )

                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(compatible="example,ordinary-reserved")),
                    configs,
                    label="non-dedicated compatible",
                    expected_message="no dedicated VM carveouts",
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(
                        carveout_node(
                            reg=(
                                f"{cells64(CARVEOUT_BASE)} {cells64(CARVEOUT_SIZE)} "
                                f"{cells64(0x1_1000_0000)} {cells64(CARVEOUT_SIZE)}"
                            )
                        )
                    ),
                    configs,
                    label="multiple reg tuples",
                    expected_message="exactly one reg tuple",
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(base=CARVEOUT_BASE + 0x1000)),
                    configs,
                    label="unaligned base",
                    expected_message="2 MiB aligned",
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(size=CARVEOUT_SIZE + 0x1000)),
                    configs,
                    label="unaligned size",
                    expected_message="2 MiB aligned",
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(base=0x2_4000_0000)),
                    configs,
                    label="range outside RAM",
                    expected_message="not contained in one RAM range",
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(extra_properties="reusable;")),
                    configs,
                    label="reusable carveout",
                    expected_message="must not have reusable",
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(carveout_node(extra_properties="no-map;")),
                    configs,
                    label="no-map carveout",
                    expected_message="must not have no-map",
                )
                dynamic = carveout_node(extra_properties="size = <0x0 0x08000000>;")
                expect_rejected(
                    errors,
                    validator,
                    host_dts(dynamic),
                    configs,
                    label="dynamic-size carveout",
                    expected_message="mixes reg with dynamic allocation properties",
                )

                duplicate_vm_dts = host_dts(
                    carveout_node(vm_id=2),
                    carveout_node(vm_id=2, base=0x1_1000_0000),
                )
                expect_rejected(
                    errors,
                    validator,
                    duplicate_vm_dts,
                    configs,
                    label="duplicate VM id",
                    expected_message="duplicate VM id 2",
                )

                overlapping_dts = host_dts(
                    carveout_node(vm_id=2, size=0x1000_0000),
                    carveout_node(vm_id=3, base=0x1_0800_0000),
                )
                expect_rejected(
                    errors,
                    validator,
                    overlapping_dts,
                    {
                        "vm-2.toml": vm_toml(size=0x1000_0000),
                        "vm-3.toml": vm_toml(
                            vm_id=3,
                            base=0x1_0800_0000,
                        ),
                    },
                    label="overlapping carveouts",
                    expected_message="overlaps VM",
                )

                expect_rejected(
                    errors,
                    validator,
                    host_dts(),
                    {"vm-2.toml": vm_toml(base=CARVEOUT_BASE + 0x200000)},
                    label="TOML base mismatch",
                    expected_message="has no exact MapReserved mapping",
                )
                expect_rejected(
                    errors,
                    validator,
                    host_dts(),
                    {"vm-2.toml": vm_toml(map_type=0)},
                    label="TOML MapAlloc substitution",
                    expected_message="has no exact MapReserved mapping",
                )

                directory = WORKSPACE_ROOT / "results"
                unique = f"{os.getpid()}-{secrets.token_hex(8)}"
                output = directory / f".carveout-contract-{unique}.json"
                protected = directory / f".carveout-protected-{unique}.txt"
                symlink = directory / f".carveout-symlink-{unique}.json"
                collision_output = directory / f".carveout-collision-{unique}.json"
                collision_stage = collision_output.with_name(
                    f".{collision_output.name}.tmp-{os.getpid()}-fixed-token"
                )
                dts_source = directory / f".carveout-source-{unique}.dts"
                vm_source = directory / f".carveout-vm-{unique}.toml"
                main_output = directory / f".carveout-main-{unique}.json"
                try:
                    try:
                        validator._atomic_write(output, b"first\n")
                    except Exception as error:  # noqa: BLE001 - aggregate.
                        errors.append(f"validator cannot publish new evidence: {error}")
                    else:
                        if output.read_bytes() != b"first\n":
                            errors.append(
                                "validator publishes the wrong evidence bytes"
                            )
                        try:
                            validator._atomic_write(output, b"second\n")
                        except validator.CarveoutArtifactError as error:
                            if "already exists" not in str(error):
                                errors.append(
                                    f"validator reports wrong no-overwrite error: {error}"
                                )
                        except Exception as error:  # noqa: BLE001 - aggregate.
                            errors.append(
                                f"validator raises wrong no-overwrite exception: {error}"
                            )
                        else:
                            errors.append("validator overwrites published evidence")
                        if output.read_bytes() != b"first\n":
                            errors.append(
                                "no-overwrite failure changes existing evidence"
                            )

                    io_module = sys.modules.get("host_vm_carveout_io")
                    if io_module is None:
                        errors.append("validator filesystem helper is not importable")
                    else:
                        fake_entries = (
                            (
                                "symlink",
                                SimpleNamespace(
                                    st_mode=stat.S_IFLNK,
                                    st_file_attributes=0,
                                    st_dev=1,
                                    st_ino=1,
                                ),
                            ),
                            (
                                "reparse",
                                SimpleNamespace(
                                    st_mode=stat.S_IFREG,
                                    st_file_attributes=0x400,
                                    st_dev=1,
                                    st_ino=1,
                                ),
                            ),
                        )
                        original_reparse_flag = io_module.REPARSE_FLAG
                        io_module.REPARSE_FLAG = 0x400
                        try:
                            for entry_label, info in fake_entries:
                                fake_path = SimpleNamespace(
                                    lstat=lambda info=info: info
                                )
                                try:
                                    io_module._checked_path(
                                        fake_path,
                                        directory=False,
                                        label=entry_label,
                                        error_type=validator.CarveoutArtifactError,
                                    )
                                except validator.CarveoutArtifactError as error:
                                    if "symlink or reparse point" not in str(error):
                                        errors.append(
                                            f"validator reports wrong {entry_label} error: {error}"
                                        )
                                else:
                                    errors.append(
                                        f"validator accepts a synthetic {entry_label}"
                                    )
                        finally:
                            io_module.REPARSE_FLAG = original_reparse_flag
                        original_token_hex = io_module.secrets.token_hex
                        collision_stage.write_bytes(b"preserve-stage")
                        io_module.secrets.token_hex = lambda _count: "fixed-token"
                        try:
                            validator._atomic_write(collision_output, b"new\n")
                        except validator.CarveoutArtifactError as error:
                            if "staging output" not in str(error):
                                errors.append(
                                    f"validator reports wrong staging collision: {error}"
                                )
                        except Exception as error:  # noqa: BLE001 - aggregate.
                            errors.append(
                                f"validator raises wrong staging collision error: {error}"
                            )
                        else:
                            errors.append("validator overwrites a staging collision")
                        finally:
                            io_module.secrets.token_hex = original_token_hex
                        if collision_stage.read_bytes() != b"preserve-stage":
                            errors.append("staging collision changes the existing file")
                        if collision_output.exists():
                            errors.append(
                                "staging collision publishes partial evidence"
                            )

                    exact_dts_bytes = host_dts().replace("\n", "\r\n").encode()
                    exact_vm_bytes = vm_toml().encode()
                    dts_source.write_bytes(exact_dts_bytes)
                    vm_source.write_bytes(exact_vm_bytes)
                    main_stdout, main_stderr = io.StringIO(), io.StringIO()
                    with (
                        contextlib.redirect_stdout(main_stdout),
                        contextlib.redirect_stderr(main_stderr),
                    ):
                        main_status = validator.main(
                            [
                                "--dts",
                                str(dts_source),
                                "--vm-config",
                                str(vm_source),
                                "--output",
                                str(main_output),
                            ]
                        )
                    if main_status != 0:
                        errors.append(
                            "validator main rejects valid byte sources: "
                            + main_stderr.getvalue()
                        )
                    else:
                        manifest = json.loads(main_output.read_bytes())
                        source_hash = manifest["sources"]["hostDts"]["sha256"]
                        if source_hash != hashlib.sha256(exact_dts_bytes).hexdigest():
                            errors.append(
                                "validator main normalizes source bytes before hashing"
                            )
                        preserved_manifest = main_output.read_bytes()
                        with (
                            contextlib.redirect_stdout(io.StringIO()),
                            contextlib.redirect_stderr(io.StringIO()),
                        ):
                            second_status = validator.main(
                                [
                                    "--dts",
                                    str(dts_source),
                                    "--vm-config",
                                    str(vm_source),
                                    "--output",
                                    str(main_output),
                                ]
                            )
                        if second_status == 0:
                            errors.append("validator main overwrites existing evidence")
                        if main_output.read_bytes() != preserved_manifest:
                            errors.append(
                                "validator main no-overwrite failure changes evidence"
                            )

                    protected.write_bytes(b"preserve")
                    try:
                        symlink.symlink_to(protected)
                    except OSError:
                        pass  # Windows may not grant symlink creation to this process.
                    else:
                        try:
                            validator._atomic_write(symlink, b"replace\n")
                        except validator.CarveoutArtifactError as error:
                            if "symlink or reparse point" not in str(error):
                                errors.append(
                                    f"validator reports wrong symlink error: {error}"
                                )
                        except Exception as error:  # noqa: BLE001 - aggregate.
                            errors.append(
                                f"validator raises wrong symlink exception: {error}"
                            )
                        else:
                            errors.append("validator follows an output symlink")
                        if protected.read_bytes() != b"preserve":
                            errors.append("output symlink changes its protected target")
                finally:
                    for path in (
                        symlink,
                        collision_output,
                        collision_stage,
                        main_output,
                        vm_source,
                        dts_source,
                        output,
                        protected,
                    ):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError as error:
                            errors.append(
                                f"could not clean contract probe {path}: {error}"
                            )

    if not errors:
        return 0

    print("AxVisor host VM carveout artifact contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
