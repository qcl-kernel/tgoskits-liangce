#!/usr/bin/env python3
"""Behavioral contract for run-scoped host carveout DTB derivation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE_ROOT / "scripts/contest/prepare_host_vm_carveout_dtb.py"
CONTEST = WORKSPACE_ROOT / "scripts/contest"
sys.path.insert(0, str(CONTEST))
import validate_host_vm_carveouts as validator  # noqa: E402


def load_script():
    spec = importlib.util.spec_from_file_location("prepare_host_vm_carveout_dtb", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def base_dts(*, reserved: bool = False) -> str:
    reserved_node = "\n\treserved-memory { ranges; };\n" if reserved else ""
    return f"""/dts-v1/;

/ {{{reserved_node}
\t#address-cells = <0x2>;
\t#size-cells = <0x2>;
\tmemory@40000000 {{
\t\tdevice_type = "memory";
\t\treg = <0x0 0x40000000 0x2 0x0>;
\t}};
}};
"""


def vm_toml(*, mappings: str = "[0x8000_0000, 0x1000_0000, 0x7, 2]") -> str:
    return f"""[base]
id = 1
name = "linux-carveout"
vm_type = 1
cpu_num = 1
[kernel]
entry_point = 0x80200000
kernel_path = "/guest/linux"
kernel_load_addr = 0x80200000
memory_regions = [{mappings}]
[devices]
emu_devices = []
passthrough_devices = []
"""


def vm_toml_with_id(vm_id: int, *, hpa: int, size: int = 0x1000_0000) -> str:
    return vm_toml(mappings=f"[{hpa:#x}, {size:#x}, 0x7, 2]").replace(
        "id = 1", f"id = {vm_id}", 1
    )


def main() -> int:
    errors: list[str] = []
    try:
        module = load_script()
    except Exception as error:  # noqa: BLE001 - aggregate contract failures.
        print(f"could not import host carveout preparer: {error}", file=sys.stderr)
        return 1

    try:
        specs = module._mapping_specs({"vm.toml": vm_toml()})
        derived = module.inject_reserved_memory(base_dts(), specs)
        validated = validator.validate_artifacts(derived, {"vm.toml": vm_toml()})
        if len(validated) != 1 or validated[0]["vmId"] != 1:
            errors.append("derived DTS does not validate as one VM carveout")
        if 'compatible = "axvisor,vm-carveout-v1";' not in derived:
            errors.append("derived DTS has no dedicated compatible")
        if "reg = <0x0 0x80000000 0x0 0x10000000>;" not in derived:
            errors.append("derived DTS has the wrong 64-bit carveout tuple")
        if "no-map" in derived or "reusable" in derived:
            errors.append("derived DTS adds a forbidden carveout property")
        root_property = derived.find("#size-cells = <0x2>;")
        reserved_child = derived.find("\treserved-memory {")
        original_child = derived.find("\tmemory@40000000 {")
        if not 0 <= root_property < reserved_child < original_child:
            errors.append("reserved-memory is not inserted after root properties and before children")
    except Exception as error:  # noqa: BLE001 - aggregate contract failures.
        errors.append(f"valid derivation failed: {error}")

    try:
        guards = validator.parse_dma_guard_specs(["0x180000000:0x200000"])
        guarded = module.inject_reserved_memory(base_dts(), specs, guards)
        validator.validate_artifacts(
            guarded, {"vm.toml": vm_toml()}, expected_dma_guards=guards
        )
        if 'dma-guard@180000000 {' not in guarded:
            errors.append("preparer does not derive the DMA guard node name")
        for fragment in (
            'compatible = "axvisor,dma-guard-v1";',
            "reg = <0x1 0x80000000 0x0 0x200000>;",
            "no-map;",
        ):
            if fragment not in guarded:
                errors.append(f"preparer omits DMA guard fragment {fragment!r}")
        guard_start = guarded.find("dma-guard@180000000")
        guard_end = guarded.find("\t\t};", guard_start)
        if "axvisor,vm-id" in guarded[guard_start:guard_end]:
            errors.append("preparer assigns a VM id to the DMA guard")
    except Exception as error:  # noqa: BLE001 - aggregate contract failures.
        errors.append(f"DMA guard derivation failed: {error}")

    invalid_cases = (
        ({"vm.toml": vm_toml(mappings="[0x80000000, 0x10000000, 0x7, 0]")}, "no MapReserved"),
        (
            {
                "vm.toml": vm_toml(
                    mappings=(
                        "[0x80000000, 0x10000000, 0x7, 2], "
                        "[0xa0000000, 0x10000000, 0x7, 2]"
                    )
                )
            },
            "multiple MapReserved",
        ),
    )
    for configs, label in invalid_cases:
        try:
            module._mapping_specs(configs)
        except module.HostCarveoutPreparationError:
            pass
        else:
            errors.append(f"preparer accepts {label}")

    non_hpa_ordered_overlap = {
        "vm1.toml": vm_toml_with_id(1, hpa=0xA000_0000),
        "vm2.toml": vm_toml_with_id(2, hpa=0x7000_0000),
        "vm3.toml": vm_toml_with_id(3, hpa=0xA800_0000),
    }
    try:
        module._mapping_specs(non_hpa_ordered_overlap)
    except module.HostCarveoutPreparationError:
        pass
    else:
        errors.append("preparer misses overlap when VM-id order differs from HPA order")

    try:
        module.inject_reserved_memory(base_dts(reserved=True), specs)
    except module.HostCarveoutPreparationError:
        pass
    else:
        errors.append("preparer merges an ambiguous existing reserved-memory node")

    fake_base = b"base-dtb"
    fake_configs = {"vm.toml": vm_toml().encode()}
    fake_artifacts = {"host-carveout.dtb": b"derived", "preflight.json": b"{}\n"}
    payload = module.build_derivation_payload(
        base_dtb_name="base.dtb",
        base_dtb=fake_base,
        vm_config_bytes=fake_configs,
        artifacts=fake_artifacts,
        specs=specs,
        dma_guards=validator.parse_dma_guard_specs(["0x180000000:0x200000"]),
        dtc_version="Version: DTC test",
    )
    if payload.get("status") != "host_vm_carveout_dtb_prepared":
        errors.append("derivation manifest has the wrong status")
    if payload.get("artifactStatus") != "prepared-unlaunched":
        errors.append("derivation manifest overstates runtime evidence")
    source = payload.get("sources", {}).get("baseHostDtb", {})
    if source.get("sha256") != hashlib.sha256(fake_base).hexdigest():
        errors.append("derivation manifest does not hash the base DTB bytes")
    if payload.get("dmaGuards") != [
        {"hpa": "0x180000000", "size": "0x200000", "end": "0x180200000"}
    ]:
        errors.append("derivation manifest does not separately record DMA guards")
    serialized = json.dumps(payload)
    for boundary in (
        "the derived DTB was supplied to QEMU",
        "host allocator exclusion",
        "passthrough DMA isolation",
        "Guest IP connectivity",
        "QEMU used a DMA guard reservation",
        "DMA guard changed host allocation or DMA behavior",
        "DMA isolation",
    ):
        if boundary not in serialized:
            errors.append(f"derivation manifest omits boundary {boundary!r}")

    source_text = SCRIPT.read_text(encoding="utf-8")
    for fragment in (
        "read_regular_bytes(",
        "publish_new_file(",
        "subprocess.run(",
        'publication": "published last after all named artifacts',
        "host-carveout.preflight.json",
        "_validate_fdt(derived_dtb",
        "validate_artifacts(",
        "--dma-guard",
        "dmaGuards",
    ):
        if fragment not in source_text:
            errors.append(f"production preparer is missing {fragment!r}")
    manifest_publish = source_text.rfind("output_dir / MANIFEST_NAME")
    artifact_publish = source_text.find("for name, data in artifacts.items()")
    if artifact_publish < 0 or manifest_publish < artifact_publish:
        errors.append("derivation manifest is not published after artifacts")

    directory = WORKSPACE_ROOT / "results"
    unique = f"{os.getpid()}-{secrets.token_hex(8)}"
    base_source = directory / f".prepare-base-{unique}.dtb"
    vm_source = directory / f".prepare-vm-{unique}.toml"
    output_dir = directory / f".prepare-output-{unique}"
    base_bytes = module.FDT_MAGIC.to_bytes(4, "big") + (40).to_bytes(4, "big") + b"\0" * 32
    derived_bytes = base_bytes[:-1] + b"\1"
    original_tool, original_run, original_publish, original_mkdir = (
        module._tool,
        module._run,
        module.publish_new_file,
        module.os.mkdir,
    )
    try:
        base_source.write_bytes(base_bytes)
        vm_source.write_text(vm_toml(), encoding="utf-8")
        derived_text = module.inject_reserved_memory(base_dts(), specs).encode()

        def fake_run(command, *, input_bytes=None):
            if command[1:] == ["--version"]:
                return b"fake dtc\n", b""
            if command[1:] == ["-I", "dtb", "-O", "dts"]:
                return (base_dts().encode() if input_bytes == base_bytes else derived_text), b""
            if command[1:] == ["-I", "dts", "-O", "dtb"]:
                return derived_bytes, b""
            raise AssertionError(f"unexpected fake dtc command: {command!r}")

        def fail_manifest(path, data, *, error_type):
            if path.name == module.MANIFEST_NAME:
                raise module.HostCarveoutPreparationError("injected manifest failure")
            return original_publish(path, data, error_type=error_type)

        # The production mode is intentionally strict; use normal temporary
        # directory permissions here so Windows can exercise publication order.
        module.os.mkdir = lambda path, mode=0o777: original_mkdir(path, 0o777)
        module._tool = lambda _command: "fake-dtc"
        module._run = fake_run
        module.publish_new_file = fail_manifest
        status = module.main(
            [
                "--base-dtb", str(base_source), "--vm-config", str(vm_source),
                "--output-dir", str(output_dir),
            ]
        )
        if status == 0:
            errors.append("preparer accepts an injected manifest publication failure")
        if (output_dir / module.MANIFEST_NAME).exists():
            errors.append("failed preparation leaves a successful derivation manifest")
    except Exception as error:  # noqa: BLE001 - aggregate contract failures.
        errors.append(f"cannot probe late manifest publication failure: {error}")
    finally:
        module._tool, module._run, module.publish_new_file, module.os.mkdir = (
            original_tool, original_run, original_publish, original_mkdir
        )
        for path in (base_source, vm_source):
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                errors.append(f"cannot remove preparation probe source: {error}")
        if output_dir.exists():
            for path in output_dir.iterdir():
                try:
                    path.unlink()
                except OSError as error:
                    errors.append(f"cannot remove preparation probe artifact: {error}")
            try:
                output_dir.rmdir()
            except OSError as error:
                errors.append(f"cannot remove preparation probe directory: {error}")

    if not errors:
        return 0
    print("AxVisor host carveout DTB preparation contract failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
