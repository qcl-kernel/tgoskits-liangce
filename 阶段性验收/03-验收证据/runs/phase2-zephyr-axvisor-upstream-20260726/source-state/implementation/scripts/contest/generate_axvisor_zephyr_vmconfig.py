#!/usr/bin/env python3
"""Generate an AxVisor Zephyr VM config from a verified AArch64 ELF image."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Sequence


ELF_HEADER = struct.Struct("<16sHHIQQQIHHHHHH")
ELF_PROGRAM_HEADER = struct.Struct("<IIQQQQQQ")
ELF_MAGIC = b"\x7fELF"
ELF_CLASS_64 = 2
ELF_DATA_LITTLE_ENDIAN = 1
ELF_CURRENT_VERSION = 1
ELF_TYPE_EXECUTABLE = 2
ELF_MACHINE_AARCH64 = 183
ELF_PROGRAM_TYPE_LOAD = 1
ELF_PROGRAM_FLAG_EXECUTE = 1
ADDRESS_SPACE_SIZE = 1 << 64
KERNEL_KEYS = ("entry_point", "kernel_path", "image_location")


def generate(
    template_path: str | Path,
    elf_path: str | Path,
    binary_path: str | Path,
    output_path: str | Path,
    provenance_path: str | Path,
) -> dict[str, object]:
    """Generate an AxVisor VM config and its provenance record.

    The ELF must be a little-endian, executable AArch64 ELF64 image. Its entry
    must be backed by exactly one executable ``PT_LOAD`` segment. The converted
    physical entry and the raw BIN image must fit the template's load window and
    RAM layout.

    Raises:
        OSError: If an input cannot be read or an output cannot be written.
        ValueError: If an input violates the ELF, template, or path contract.
    """

    paths = _resolve_paths(
        template_path,
        elf_path,
        binary_path,
        output_path,
        provenance_path,
    )
    _reject_path_collisions(paths)

    template_bytes = paths["template"].read_bytes()
    elf_bytes = paths["elf"].read_bytes()
    binary_bytes = paths["binary"].read_bytes()
    template_text = template_bytes.decode("utf-8")
    template = tomllib.loads(template_text)

    elf_entry = _parse_elf_entry(elf_bytes)
    load_layout = _validate_load_layout(
        template,
        elf_entry["entry_physical"],
        len(binary_bytes),
    )
    generated_text = _render_generated_template(
        template_text,
        template,
        elf_entry["entry_physical"],
        paths["binary"],
    )
    generated_bytes = generated_text.encode("utf-8")
    provenance = _build_provenance(
        paths,
        template_bytes,
        elf_bytes,
        binary_bytes,
        generated_bytes,
        elf_entry,
        load_layout,
    )

    _atomic_write(paths["output"], generated_bytes)
    _atomic_write(
        paths["provenance"],
        (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )

    return {
        "entry_virtual": elf_entry["entry_virtual"],
        "entry_physical": elf_entry["entry_physical"],
        "template_sha256": provenance["templateSha256"],
        "elf_sha256": provenance["elfSha256"],
        "binary_sha256": provenance["binarySha256"],
        "output_path": str(paths["output"]),
        "provenance_path": str(paths["provenance"]),
    }


def _resolve_paths(
    template_path: str | Path,
    elf_path: str | Path,
    binary_path: str | Path,
    output_path: str | Path,
    provenance_path: str | Path,
) -> dict[str, Path]:
    return {
        "template": Path(template_path).resolve(),
        "elf": Path(elf_path).resolve(),
        "binary": Path(binary_path).resolve(),
        "output": Path(output_path).resolve(),
        "provenance": Path(provenance_path).resolve(),
    }


def _reject_path_collisions(paths: dict[str, Path]) -> None:
    input_paths = {paths["template"], paths["elf"], paths["binary"]}
    for label in ("output", "provenance"):
        if paths[label] in input_paths:
            raise ValueError(f"{label} path would overwrite an input: {paths[label]}")
    if paths["output"] == paths["provenance"]:
        raise ValueError("generated config and provenance paths must be different")


def _parse_elf_entry(elf: bytes) -> dict[str, int]:
    if len(elf) < ELF_HEADER.size:
        raise ValueError("ELF header is truncated")

    (
        identifier,
        elf_type,
        machine,
        elf_version,
        entry_virtual,
        program_header_offset,
        _section_header_offset,
        _flags,
        elf_header_size,
        program_header_size,
        program_header_count,
        _section_header_size,
        _section_header_count,
        _section_name_index,
    ) = ELF_HEADER.unpack_from(elf)
    _validate_elf_header(
        identifier,
        elf_type,
        machine,
        elf_version,
        elf_header_size,
        program_header_size,
        program_header_count,
    )
    _validate_program_header_table(
        elf,
        program_header_offset,
        program_header_size,
        program_header_count,
    )

    executable_entries: list[dict[str, int]] = []
    for index in range(program_header_count):
        offset = program_header_offset + index * program_header_size
        program_header = ELF_PROGRAM_HEADER.unpack_from(elf, offset)
        candidate = _entry_from_program_header(elf, entry_virtual, program_header)
        if candidate is not None:
            executable_entries.append(candidate)

    if not executable_entries:
        raise ValueError("ELF entry is not file-backed by an executable PT_LOAD segment")
    if len(executable_entries) != 1:
        raise ValueError("ELF entry is ambiguously mapped by executable PT_LOAD segments")

    entry = executable_entries[0]
    entry["entry_virtual"] = entry_virtual
    return entry


def _validate_elf_header(
    identifier: bytes,
    elf_type: int,
    machine: int,
    elf_version: int,
    elf_header_size: int,
    program_header_size: int,
    program_header_count: int,
) -> None:
    if identifier[:4] != ELF_MAGIC:
        raise ValueError("input does not have ELF magic")
    if identifier[4] != ELF_CLASS_64:
        raise ValueError("Zephyr ELF must use the ELF64 class")
    if identifier[5] != ELF_DATA_LITTLE_ENDIAN:
        raise ValueError("Zephyr ELF must use little-endian encoding")
    if identifier[6] != ELF_CURRENT_VERSION or elf_version != ELF_CURRENT_VERSION:
        raise ValueError("Zephyr ELF uses an unsupported ELF version")
    if elf_type != ELF_TYPE_EXECUTABLE:
        raise ValueError("Zephyr ELF must be an ET_EXEC image")
    if machine != ELF_MACHINE_AARCH64:
        raise ValueError("Zephyr ELF must target AArch64")
    if elf_header_size != ELF_HEADER.size:
        raise ValueError("Zephyr ELF has an invalid ELF64 header size")
    if program_header_size < ELF_PROGRAM_HEADER.size:
        raise ValueError("Zephyr ELF program headers are too small")
    if program_header_count == 0:
        raise ValueError("Zephyr ELF has no program headers")


def _validate_program_header_table(
    elf: bytes,
    table_offset: int,
    entry_size: int,
    entry_count: int,
) -> None:
    table_size = entry_size * entry_count
    table_end = table_offset + table_size
    if table_offset > len(elf) or table_end > len(elf):
        raise ValueError("ELF program header table is truncated")


def _entry_from_program_header(
    elf: bytes,
    entry_virtual: int,
    program_header: tuple[int, ...],
) -> dict[str, int] | None:
    (
        segment_type,
        segment_flags,
        segment_offset,
        segment_virtual,
        segment_physical,
        segment_file_size,
        segment_memory_size,
        _segment_alignment,
    ) = program_header
    if segment_type != ELF_PROGRAM_TYPE_LOAD:
        return None

    if segment_file_size > segment_memory_size:
        raise ValueError("ELF PT_LOAD file size exceeds its memory size")
    segment_file_end = _checked_range_end(
        segment_offset,
        segment_file_size,
        "ELF PT_LOAD file range",
    )
    if segment_file_end > len(elf):
        raise ValueError("ELF PT_LOAD file range is truncated")
    virtual_file_end = _checked_range_end(
        segment_virtual,
        segment_file_size,
        "ELF PT_LOAD virtual file range",
    )
    _checked_range_end(
        segment_virtual,
        segment_memory_size,
        "ELF PT_LOAD virtual memory range",
    )

    if not (segment_flags & ELF_PROGRAM_FLAG_EXECUTE):
        return None
    if not (segment_virtual <= entry_virtual < virtual_file_end):
        return None

    entry_offset = entry_virtual - segment_virtual
    entry_physical = segment_physical + entry_offset
    if entry_physical >= ADDRESS_SPACE_SIZE:
        raise ValueError("ELF physical entry overflows the 64-bit address space")
    return {
        "entry_physical": entry_physical,
        "segment_virtual": segment_virtual,
        "segment_physical": segment_physical,
        "segment_file_size": segment_file_size,
    }


def _checked_range_end(start: int, size: int, label: str) -> int:
    if start < 0 or start >= ADDRESS_SPACE_SIZE:
        raise ValueError(f"{label} starts outside the 64-bit address space")
    if size < 0:
        raise ValueError(f"{label} has a negative size")
    end = start + size
    if end > ADDRESS_SPACE_SIZE:
        raise ValueError(f"{label} overflows the 64-bit address space")
    return end


def _validate_load_layout(
    template: dict[str, object],
    entry_physical: int,
    binary_size: int,
) -> dict[str, int]:
    kernel = template.get("kernel")
    if not isinstance(kernel, dict):
        raise ValueError("template has no [kernel] table")

    load_address = _require_integer(kernel, "kernel_load_addr")
    if binary_size <= 0:
        raise ValueError("Zephyr BIN image is empty")
    load_end = _checked_range_end(load_address, binary_size, "Zephyr BIN load window")
    if not (load_address <= entry_physical < load_end):
        raise ValueError("physical ELF entry is outside the Zephyr BIN load window")

    memory_regions = kernel.get("memory_regions")
    if not isinstance(memory_regions, list) or not memory_regions:
        raise ValueError("template kernel has no memory_regions")

    containing_region: tuple[int, int] | None = None
    for index, region in enumerate(memory_regions):
        if not isinstance(region, list) or len(region) < 2:
            raise ValueError(f"template memory region {index} is malformed")
        region_start = _require_integer_value(region[0], f"memory region {index} base")
        region_size = _require_integer_value(region[1], f"memory region {index} size")
        if region_size <= 0:
            raise ValueError(f"template memory region {index} is empty")
        region_end = _checked_range_end(
            region_start,
            region_size,
            f"template memory region {index}",
        )
        if region_start <= load_address and load_end <= region_end:
            containing_region = (region_start, region_end)
            break

    if containing_region is None:
        raise ValueError("Zephyr BIN load window does not fit in template RAM")
    if not (containing_region[0] <= entry_physical < containing_region[1]):
        raise ValueError("physical ELF entry is outside template RAM")

    return {
        "kernel_load_address": load_address,
        "binary_size": binary_size,
        "memory_region_start": containing_region[0],
        "memory_region_end": containing_region[1],
    }


def _require_integer(mapping: dict[str, object], key: str) -> int:
    if key not in mapping:
        raise ValueError(f"template kernel is missing {key}")
    return _require_integer_value(mapping[key], f"template kernel {key}")


def _require_integer_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _render_generated_template(
    template_text: str,
    template: dict[str, object],
    entry_physical: int,
    binary_path: Path,
) -> str:
    replacements = {
        "entry_point": f"0x{entry_physical:016x}",
        "kernel_path": json.dumps(str(binary_path), ensure_ascii=False),
        "image_location": '"memory"',
    }
    lines = template_text.splitlines(keepends=True)
    in_kernel = False
    found: set[str] = set()

    for index, line in enumerate(lines):
        section = _toml_table_name(line)
        if section is not None:
            in_kernel = section == "kernel"
            continue
        if not in_kernel:
            continue

        for key, replacement in replacements.items():
            updated = _replace_assignment_value(line, key, replacement)
            if updated is None:
                continue
            if key in found:
                raise ValueError(f"template kernel contains duplicate {key}")
            lines[index] = updated
            found.add(key)
            break

    missing = set(KERNEL_KEYS) - found
    if missing:
        raise ValueError(
            "template kernel is missing replaceable keys: " + ", ".join(sorted(missing))
        )

    generated_text = "".join(lines)
    generated = tomllib.loads(generated_text)
    _validate_generated_template(
        template,
        generated,
        entry_physical,
        str(binary_path),
    )
    return generated_text


def _toml_table_name(line: str) -> str | None:
    body = line.rstrip("\r\n")
    match = re.fullmatch(r"[ \t]*\[([A-Za-z0-9_.-]+)\][ \t]*(?:#.*)?", body)
    return match.group(1) if match else None


def _replace_assignment_value(line: str, key: str, replacement: str) -> str | None:
    newline = ""
    body = line
    if body.endswith("\r\n"):
        body, newline = body[:-2], "\r\n"
    elif body.endswith("\n"):
        body, newline = body[:-1], "\n"

    match = re.match(rf"^([ \t]*{re.escape(key)}[ \t]*=[ \t]*)(.*)$", body)
    if match is None:
        return None
    value_and_suffix = match.group(2)
    value_end = _toml_scalar_value_end(value_and_suffix)
    suffix = value_and_suffix[value_end:]
    if suffix.strip() and not suffix.lstrip().startswith("#"):
        raise ValueError(f"template kernel {key} is not a single-line scalar")
    return match.group(1) + replacement + suffix + newline


def _toml_scalar_value_end(value: str) -> int:
    if not value:
        raise ValueError("template assignment has no value")
    if value.startswith(('"""', "'''")):
        raise ValueError("multi-line TOML values are not supported in the VM template")
    if value[0] in ('"', "'"):
        quote = value[0]
        escaped = False
        for index in range(1, len(value)):
            character = value[index]
            if quote == '"' and escaped:
                escaped = False
                continue
            if quote == '"' and character == "\\":
                escaped = True
                continue
            if character == quote:
                return index + 1
        raise ValueError("template assignment has an unterminated string")

    match = re.match(r"[^ \t#]+", value)
    if match is None:
        raise ValueError("template assignment has no scalar value")
    return match.end()


def _validate_generated_template(
    template: dict[str, object],
    generated: dict[str, object],
    entry_physical: int,
    binary_path: str,
) -> None:
    template_without_replacements = copy.deepcopy(template)
    generated_without_replacements = copy.deepcopy(generated)
    template_kernel = template_without_replacements.get("kernel")
    generated_kernel = generated_without_replacements.get("kernel")
    if not isinstance(template_kernel, dict) or not isinstance(generated_kernel, dict):
        raise ValueError("generated config lost the [kernel] table")

    for key in KERNEL_KEYS:
        template_kernel.pop(key, None)
        generated_kernel.pop(key, None)
    if generated_without_replacements != template_without_replacements:
        raise ValueError("generated config changed fields outside the allowed kernel keys")

    actual_kernel = generated.get("kernel")
    if not isinstance(actual_kernel, dict):
        raise ValueError("generated config has no [kernel] table")
    if actual_kernel.get("entry_point") != entry_physical:
        raise ValueError("generated config has the wrong physical entry")
    if actual_kernel.get("kernel_path") != binary_path:
        raise ValueError("generated config has the wrong Zephyr BIN path")
    if actual_kernel.get("image_location") != "memory":
        raise ValueError("generated config must load the Zephyr BIN from memory")


def _build_provenance(
    paths: dict[str, Path],
    template: bytes,
    elf: bytes,
    binary: bytes,
    generated: bytes,
    elf_entry: dict[str, int],
    load_layout: dict[str, int],
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "templatePath": str(paths["template"]),
        "elfPath": str(paths["elf"]),
        "binaryPath": str(paths["binary"]),
        "generatedConfigPath": str(paths["output"]),
        "entryVirtual": _format_address(elf_entry["entry_virtual"]),
        "entryPhysical": _format_address(elf_entry["entry_physical"]),
        "executableSegmentVirtual": _format_address(elf_entry["segment_virtual"]),
        "executableSegmentPhysical": _format_address(elf_entry["segment_physical"]),
        "kernelLoadAddress": _format_address(load_layout["kernel_load_address"]),
        "binarySize": load_layout["binary_size"],
        "memoryRegionStart": _format_address(load_layout["memory_region_start"]),
        "memoryRegionEndExclusive": _format_address(load_layout["memory_region_end"]),
        "templateSha256": hashlib.sha256(template).hexdigest(),
        "elfSha256": hashlib.sha256(elf).hexdigest(),
        "binarySha256": hashlib.sha256(binary).hexdigest(),
        "generatedConfigSha256": hashlib.sha256(generated).hexdigest(),
    }


def _format_address(address: int) -> str:
    return f"0x{address:016x}"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an AxVisor Zephyr VM config from an AArch64 ELF",
    )
    parser.add_argument("template", type=Path)
    parser.add_argument("elf", type=Path)
    parser.add_argument("binary", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("provenance", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_argument_parser().parse_args(argv)
    try:
        result = generate(
            arguments.template,
            arguments.elf,
            arguments.binary,
            arguments.output,
            arguments.provenance,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"physical-entry={_format_address(result['entry_physical'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
