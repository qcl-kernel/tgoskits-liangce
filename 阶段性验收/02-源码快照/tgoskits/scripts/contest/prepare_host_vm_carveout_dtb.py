#!/usr/bin/env python3
"""Derive one byte-bound host DTB with dedicated AxVisor VM carveouts.

The base DTB must come from a QEMU ``dumpdtb`` invocation using the same
machine topology as the intended run.  This tool never claims that QEMU later
used the derived DTB; the launch harness must bind that separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import validate_host_vm_carveouts as carveout_validator
from host_vm_carveout_io import (
    _check_directory_chain,
    _checked_path,
    publish_new_file,
    read_regular_bytes,
)


FDT_MAGIC = 0xD00DFEED
MANIFEST_NAME = "derivation.json"


class HostCarveoutPreparationError(ValueError):
    """The inputs or derived artifacts cannot form a safe preparation bundle."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strict_utf8(data: bytes, *, label: str) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise HostCarveoutPreparationError(f"{label} is not strict UTF-8") from error


def _validate_fdt(data: bytes, *, label: str) -> None:
    if len(data) < 40:
        raise HostCarveoutPreparationError(f"{label} is shorter than an FDT header")
    if int.from_bytes(data[0:4], "big") != FDT_MAGIC:
        raise HostCarveoutPreparationError(f"{label} has the wrong FDT magic")
    total_size = int.from_bytes(data[4:8], "big")
    if total_size < 40 or total_size > len(data):
        raise HostCarveoutPreparationError(f"{label} has an invalid FDT total size")


def _tool(command: str) -> str:
    if not command or "\x00" in command:
        raise HostCarveoutPreparationError("--dtc must name one executable")
    resolved = shutil.which(command)
    if resolved is None:
        raise HostCarveoutPreparationError(f"dtc executable not found: {command}")
    return resolved


def _run(command: list[str], *, input_bytes: bytes | None = None) -> tuple[bytes, bytes]:
    try:
        completed = subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise HostCarveoutPreparationError(
            f"could not execute {Path(command[0]).name}: {error}"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise HostCarveoutPreparationError(
            f"{Path(command[0]).name} exited {completed.returncode}: {detail}"
        )
    return completed.stdout, completed.stderr


def _dtc_decode(dtc: str, dtb: bytes) -> tuple[bytes, bytes]:
    return _run([dtc, "-I", "dtb", "-O", "dts"], input_bytes=dtb)


def _dtc_compile(dtc: str, dts: bytes) -> tuple[bytes, bytes]:
    return _run([dtc, "-I", "dts", "-O", "dtb"], input_bytes=dts)


def _mapping_specs(
    vm_config_texts: dict[str, str],
) -> list[dict[str, int | str]]:
    parsed = carveout_validator._parse_vm_configs(vm_config_texts)
    specs: list[dict[str, int | str]] = []
    for vm_id, (name, mappings) in sorted(parsed.items()):
        if len(mappings) != 1:
            raise HostCarveoutPreparationError(
                f"VM {vm_id} config {name!r} must contain exactly one MapReserved region"
            )
        hpa, size = mappings[0]
        if (
            hpa % carveout_validator.CARVEOUT_ALIGNMENT
            or size % carveout_validator.CARVEOUT_ALIGNMENT
        ):
            raise HostCarveoutPreparationError(
                f"VM {vm_id} MapReserved region must be 2 MiB aligned"
            )
        end = hpa + size
        if end > carveout_validator.MAX_ADDRESS_END:
            raise HostCarveoutPreparationError(f"VM {vm_id} MapReserved region overflows")
        specs.append({"vmId": vm_id, "hpa": hpa, "size": size, "end": end, "vmConfig": name})

    specs_by_hpa = sorted(specs, key=lambda item: (int(item["hpa"]), int(item["end"])))
    for previous, current in zip(specs_by_hpa, specs_by_hpa[1:]):
        if int(current["hpa"]) < int(previous["end"]):
            raise HostCarveoutPreparationError(
                f"VM {current['vmId']} MapReserved region overlaps VM {previous['vmId']}"
            )
    return specs


def _cells64(value: int) -> str:
    return f"0x{value >> 32:x} 0x{value & 0xffff_ffff:x}"


def _root_child_insertion_offset(base_dts: str, root_body_start: int) -> int:
    """Return the start of the first root child (or the root closing line).

    ``dtc -O dts`` emits all properties before child nodes.  A newly injected
    child therefore has to follow those properties.  Scan outside quoted
    strings and comments so a brace in a bootarg/comment cannot select an
    invalid insertion point.
    """
    in_string = False
    escaped = False
    line_comment = False
    block_comment = False
    index = root_body_start
    while index < len(base_dts):
        char = base_dts[index]
        following = base_dts[index + 1] if index + 1 < len(base_dts) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and following == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == "/" and following == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and following == "*":
            block_comment = True
            index += 2
            continue
        if char == "{" or char == "}":
            line_start = base_dts.rfind("\n", root_body_start, index) + 1
            return line_start
        index += 1
    raise HostCarveoutPreparationError("base DTS root node is not closed")


def inject_reserved_memory(
    base_dts: str,
    specs: list[dict[str, int | str]],
    dma_guards: list[dict[str, int]] | None = None,
) -> str:
    """Insert one direct reserved-memory node into canonical ``dtc`` text."""
    try:
        root, _reservations = carveout_validator._parse_dts(base_dts)
    except carveout_validator.CarveoutArtifactError as error:
        raise HostCarveoutPreparationError(f"base DTS is invalid: {error}") from error
    nodes: list[str] = []
    for spec in specs:
        vm_id = int(spec["vmId"])
        hpa = int(spec["hpa"])
        size = int(spec["size"])
        nodes.extend(
            [
                f"\t\tvm-carveout@{hpa:x} {{",
                f'\t\t\tcompatible = "{carveout_validator.CARVEOUT_COMPATIBLE}";',
                f"\t\t\taxvisor,vm-id = <0x{vm_id:x}>;",
                f"\t\t\treg = <{_cells64(hpa)} {_cells64(size)}>;",
                "\t\t};",
                "",
            ]
        )
    for guard in dma_guards or []:
        hpa = int(guard["hpa"])
        size = int(guard["size"])
        nodes.extend(
            [
                f"\t\tdma-guard@{hpa:x} {{",
                f'\t\t\tcompatible = "{carveout_validator.DMA_GUARD_COMPATIBLE}";',
                f"\t\t\treg = <{_cells64(hpa)} {_cells64(size)}>;",
                "\t\t\tno-map;",
                "\t\t};",
                "",
            ]
        )

    reserved_nodes = [child for child in root.children if child.name == "reserved-memory"]
    if len(reserved_nodes) > 1:
        raise HostCarveoutPreparationError("base DTS has duplicate reserved-memory nodes")
    if reserved_nodes:
        reserved = reserved_nodes[0]
        try:
            valid_reserved_container = (
                carveout_validator._one_u32(reserved, "#address-cells") == 2
                and carveout_validator._one_u32(reserved, "#size-cells") == 2
                and not carveout_validator._property(reserved, "ranges", required=True)
            )
        except carveout_validator.CarveoutArtifactError as error:
            raise HostCarveoutPreparationError(
                "base DTS reserved-memory node is not a 64-bit empty-ranges container"
            ) from error
        if not valid_reserved_container:
            raise HostCarveoutPreparationError(
                "base DTS reserved-memory node is not a 64-bit empty-ranges container"
            )
        reserved_line = re.search(r"(?m)^[ \t]*reserved-memory\s*\{[ \t]*$", base_dts)
        if reserved_line is None:
            raise HostCarveoutPreparationError(
                "base DTS has no canonical reserved-memory opening line"
            )
        insertion_offset = _root_child_insertion_offset(base_dts, reserved_line.end())
        return base_dts[:insertion_offset] + "\n".join(nodes) + "\n" + base_dts[insertion_offset:]

    root_line = re.search(r"(?m)^/\s*\{[ \t]*$", base_dts)
    if root_line is None:
        raise HostCarveoutPreparationError("base DTS has no canonical root opening line")
    insertion_offset = _root_child_insertion_offset(base_dts, root_line.end())
    block = "\n".join(
        [
            "",
            "\treserved-memory {",
            "\t\t#address-cells = <0x02>;",
            "\t\t#size-cells = <0x02>;",
            "\t\tranges;",
            "",
            *nodes,
            "\t};",
        ]
    )
    return base_dts[:insertion_offset] + block + "\n\n" + base_dts[insertion_offset:]


def _artifact(path: str, data: bytes) -> dict[str, Any]:
    return {"path": path, "size": len(data), "sha256": _sha256(data)}


def build_derivation_payload(
    *,
    base_dtb_name: str,
    base_dtb: bytes,
    vm_config_bytes: dict[str, bytes],
    artifacts: dict[str, bytes],
    specs: list[dict[str, int | str]],
    dtc_version: str,
    dma_guards: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "artifactStatus": "prepared-unlaunched",
        "status": "host_vm_carveout_dtb_prepared",
        "proofScope": "qemu-base-dtb-to-static-carveout-dtb-derivation",
        "doesNotProve": [
            "the derived DTB was supplied to QEMU",
            "host allocator exclusion",
            "AxVM MapReserved stage-2 mapping",
            "passthrough DMA isolation",
            "QEMU used a DMA guard reservation",
            "DMA guard changed host allocation or DMA behavior",
            "DMA isolation",
            "dual-Guest execution",
            "Guest IP connectivity",
        ],
        "publication": "published last after all named artifacts",
        "tool": {"dtcVersion": dtc_version},
        "sources": {
            "baseHostDtb": _artifact(base_dtb_name, base_dtb),
            "vmConfigs": [
                _artifact(name, data) for name, data in sorted(vm_config_bytes.items())
            ],
        },
        "carveouts": [
            {
                "vmId": int(spec["vmId"]),
                "hpa": f"{int(spec['hpa']):#x}",
                "size": f"{int(spec['size']):#x}",
                "end": f"{int(spec['end']):#x}",
                "vmConfig": str(spec["vmConfig"]),
            }
            for spec in specs
        ],
        "dmaGuards": [
            {
                "hpa": f"{int(guard['hpa']):#x}",
                "size": f"{int(guard['size']):#x}",
                "end": f"{int(guard['end']):#x}",
            }
            for guard in (dma_guards or [])
        ],
        "artifacts": [
            _artifact(name, data) for name, data in sorted(artifacts.items())
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dtb", required=True, type=Path)
    parser.add_argument("--vm-config", required=True, action="append", type=Path)
    parser.add_argument(
        "--dma-guard",
        action="append",
        default=[],
        metavar="HPA:SIZE",
        help="static non-VM DMA guard; repeat for each 2 MiB-aligned range",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dtc", default="dtc", help="dtc executable name or path")
    args = parser.parse_args(argv)

    try:
        output_dir = args.output_dir.resolve()
        _check_directory_chain(output_dir.parent, HostCarveoutPreparationError)
        try:
            output_dir.lstat()
        except FileNotFoundError:
            pass
        else:
            raise HostCarveoutPreparationError("output directory already exists")

        base_dtb = read_regular_bytes(
            args.base_dtb,
            label="base host DTB",
            error_type=HostCarveoutPreparationError,
        )
        _validate_fdt(base_dtb, label="base host DTB")
        vm_config_bytes: dict[str, bytes] = {}
        vm_config_texts: dict[str, str] = {}
        for path in args.vm_config:
            if path.name in vm_config_bytes:
                raise HostCarveoutPreparationError(
                    f"duplicate VM config artifact name: {path.name}"
                )
            data = read_regular_bytes(
                path,
                label=f"VM config {path.name!r}",
                error_type=HostCarveoutPreparationError,
            )
            vm_config_bytes[path.name] = data
            vm_config_texts[path.name] = _strict_utf8(
                data, label=f"VM config {path.name!r}"
            )
        specs = _mapping_specs(vm_config_texts)
        dma_guards = carveout_validator.parse_dma_guard_specs(args.dma_guard)

        dtc = _tool(args.dtc)
        version_raw, version_stderr = _run([dtc, "--version"])
        dtc_version = _strict_utf8(version_raw or version_stderr, label="dtc version").strip()
        base_dts, base_decode_stderr = _dtc_decode(dtc, base_dtb)
        injected = inject_reserved_memory(
            _strict_utf8(base_dts, label="decoded base DTS"), specs, dma_guards
        ).encode("utf-8")
        derived_dtb, compile_stderr = _dtc_compile(dtc, injected)
        _validate_fdt(derived_dtb, label="derived host DTB")
        derived_dts, final_decode_stderr = _dtc_decode(dtc, derived_dtb)
        derived_text = _strict_utf8(derived_dts, label="decoded derived host DTS")
        validated = carveout_validator.validate_artifacts(
            derived_text, vm_config_texts, expected_dma_guards=dma_guards
        )
        if [int(item["vmId"]) for item in validated] != [
            int(item["vmId"]) for item in specs
        ]:
            raise HostCarveoutPreparationError("derived DTS changed the expected VM set")

        preflight_payload = carveout_validator.build_payload(
            carveouts=validated,
            dts_name="host-carveout.dts",
            dts_bytes=derived_dts,
            vm_config_bytes=vm_config_bytes,
            dma_guards=dma_guards,
        )
        preflight = (
            json.dumps(preflight_payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        artifacts = {
            "base-host.dts": base_dts,
            "base-dtc.stderr.log": base_decode_stderr,
            "host-carveout.dtb": derived_dtb,
            "host-carveout.dts": derived_dts,
            "host-carveout.preflight.json": preflight,
            "compile-dtc.stderr.log": compile_stderr,
            "final-decode-dtc.stderr.log": final_decode_stderr,
        }
        manifest_payload = build_derivation_payload(
            base_dtb_name=args.base_dtb.name,
            base_dtb=base_dtb,
            vm_config_bytes=vm_config_bytes,
            artifacts=artifacts,
            specs=specs,
            dma_guards=dma_guards,
            dtc_version=dtc_version,
        )
        manifest = (
            json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

        os.mkdir(output_dir, 0o700)
        _checked_path(
            output_dir,
            directory=True,
            label="output directory",
            error_type=HostCarveoutPreparationError,
        )
        for name, data in artifacts.items():
            publish_new_file(
                output_dir / name,
                data,
                error_type=HostCarveoutPreparationError,
            )
        publish_new_file(
            output_dir / MANIFEST_NAME,
            manifest,
            error_type=HostCarveoutPreparationError,
        )
    except (HostCarveoutPreparationError, carveout_validator.CarveoutArtifactError) as error:
        print(f"Host VM carveout DTB preparation failed: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Host VM carveout DTB preparation I/O failed: {error}", file=sys.stderr)
        return 2

    print(manifest.decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
