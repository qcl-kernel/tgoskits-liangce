#!/usr/bin/env python3
"""Validate static host-DTS VM carveouts against AxVM TOML mappings.

This preflight validator does not boot QEMU or inspect a live allocator.  It
accepts decoded DTS text so its parsing and fail-closed policy can be tested in
ordinary Python CI before runtime evidence is available.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from host_vm_carveout_io import (
    build_preflight_payload,
    publish_new_file,
    read_regular_bytes,
)


CARVEOUT_COMPATIBLE = "axvisor,vm-carveout-v1"
CARVEOUT_ALIGNMENT = 2 * 1024 * 1024
MAX_U32 = (1 << 32) - 1
MAX_ADDRESS_END = 1 << 64
MAX_USIZE = MAX_ADDRESS_END - 1  # Contest target is 64-bit AArch64.
MAP_RESERVED = 2
VALID_MAP_TYPES = {0, 1, MAP_RESERVED}

TOKEN_PATTERN = re.compile(r'"(?:\\.|[^"\\])*"|[{};=<>:]|[^\s{};=<>:]+')
INTEGER_LITERAL_PATTERN = re.compile(r"(?:0[xX][0-9a-fA-F]+|0[0-7]+|0|[1-9][0-9]*)\Z")


class CarveoutArtifactError(ValueError):
    """The DTS/TOML artifacts cannot prove a static VM carveout contract."""


def _parse_u64_literal(token: str, *, label: str) -> int:
    if not INTEGER_LITERAL_PATTERN.fullmatch(token):
        raise CarveoutArtifactError(f"unsupported {label} syntax")
    if token.lower().startswith("0x"):
        value = int(token, 16)
    elif len(token) > 1 and token.startswith("0"):
        value = int(token, 8)
    else:
        value = int(token, 10)
    if value > MAX_USIZE:
        raise CarveoutArtifactError(f"{label} integer exceeds u64")
    return value


class _DtsNode:
    __slots__ = ("name", "properties", "children")

    def __init__(self, name: str) -> None:
        self.name = name
        self.properties: dict[str, list[list[str]]] = {}
        self.children: list[_DtsNode] = []

    def add_property(self, name: str, value: list[str]) -> None:
        self.properties.setdefault(name, []).append(value)


class _DtsParser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.index = 0

    def _peek(self) -> str | None:
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            raise CarveoutArtifactError("DTS ends unexpectedly")
        self.index += 1
        return token

    def _expect(self, expected: str) -> None:
        observed = self._take()
        if observed != expected:
            raise CarveoutArtifactError(
                f"DTS expected {expected!r}, found {observed!r}"
            )

    def parse(self) -> tuple[_DtsNode, list[tuple[int, int, str]]]:
        reservations: list[tuple[int, int, str]] = []
        saw_header = False
        while self._peek() is not None and self._peek() != "/":
            directive = self._take()
            if directive == "/dts-v1/":
                if saw_header:
                    raise CarveoutArtifactError("DTS has duplicate /dts-v1/ header")
                self._expect(";")
                saw_header = True
                continue
            if directive != "/memreserve/":
                raise CarveoutArtifactError(
                    f"unsupported top-level DTS statement {directive!r}"
                )
            values: list[str] = []
            while self._peek() not in (None, ";"):
                values.append(self._take())
            self._expect(";")
            if len(values) != 2:
                raise CarveoutArtifactError("unsupported /memreserve/ syntax")
            index = len(reservations)
            base = _parse_u64_literal(values[0], label="/memreserve/")
            size = _parse_u64_literal(values[1], label="/memreserve/")
            start, end = _checked_range(base, size, label=f"/memreserve/ entry {index}")
            reservations.append((start, end, f"/memreserve/ entry {index}"))
        if not saw_header:
            raise CarveoutArtifactError("DTS has no /dts-v1/ header")
        if self._peek() != "/":
            raise CarveoutArtifactError("DTS has no root node")
        self._take()
        self._expect("{")
        root = _DtsNode("/")
        self._parse_body(root)
        if self._peek() == ";":
            self._take()
        if self._peek() is not None:
            raise CarveoutArtifactError("DTS has a statement after the root node")
        return root, reservations

    def _parse_body(self, parent: _DtsNode) -> None:
        while True:
            token = self._peek()
            if token is None:
                raise CarveoutArtifactError(
                    f"DTS node {parent.name!r} has no closing brace"
                )
            if token == "}":
                self._take()
                return

            first = self._take()
            if self._peek() == ":":
                self._take()
                first = self._take()

            next_token = self._peek()
            if next_token == "{":
                self._take()
                child = _DtsNode(first)
                self._parse_body(child)
                self._expect(";")
                parent.children.append(child)
                continue
            if next_token == ";":
                self._take()
                parent.add_property(first, [])
                continue
            if next_token != "=":
                raise CarveoutArtifactError(
                    f"DTS statement {first!r} is neither a node nor a property"
                )

            self._take()
            value: list[str] = []
            while self._peek() not in (None, ";"):
                if self._peek() in ("{", "}"):
                    raise CarveoutArtifactError(
                        f"DTS property {first!r} has malformed delimiters"
                    )
                value.append(self._take())
            self._expect(";")
            parent.add_property(first, value)


def _strip_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
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
            output.append(char)
            index += 1
            continue
        if char == "/" and following == "/":
            newline = text.find("\n", index + 2)
            if newline < 0:
                break
            output.append("\n")
            index = newline + 1
            continue
        if char == "/" and following == "*":
            end = text.find("*/", index + 2)
            if end < 0:
                raise CarveoutArtifactError("DTS has an unterminated block comment")
            output.append("\n" * text[index : end + 2].count("\n"))
            index = end + 2
            continue
        output.append(char)
        index += 1
    if in_string:
        raise CarveoutArtifactError("DTS has an unterminated string literal")
    return "".join(output)


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    cursor = 0
    for match in TOKEN_PATTERN.finditer(text):
        gap = text[cursor : match.start()]
        if gap.strip():
            raise CarveoutArtifactError(
                f"DTS contains unsupported syntax near {gap[:20]!r}"
            )
        tokens.append(match.group(0))
        cursor = match.end()
    if text[cursor:].strip():
        raise CarveoutArtifactError("DTS contains trailing unsupported syntax")
    return tokens


def _parse_dts(text: str) -> tuple[_DtsNode, list[tuple[int, int, str]]]:
    stripped = _strip_comments(text)
    return _DtsParser(_tokenize(stripped)).parse()


def _property(
    node: _DtsNode,
    name: str,
    *,
    required: bool = False,
) -> list[str] | None:
    values = node.properties.get(name, [])
    if len(values) > 1:
        raise CarveoutArtifactError(
            f"DTS node {node.name!r} has duplicate {name} properties"
        )
    if not values:
        if required:
            raise CarveoutArtifactError(
                f"DTS node {node.name!r} has no {name} property"
            )
        return None
    return values[0]


def _cells(node: _DtsNode, name: str, *, required: bool = True) -> list[int] | None:
    raw = _property(node, name, required=required)
    if raw is None:
        return None
    if len(raw) < 3 or raw[0] != "<" or raw[-1] != ">":
        raise CarveoutArtifactError(
            f"DTS node {node.name!r} {name} must be one cell list"
        )
    if raw.count("<") != 1 or raw.count(">") != 1:
        raise CarveoutArtifactError(
            f"DTS node {node.name!r} {name} has multiple cell lists"
        )
    parsed: list[int] = []
    for token in raw[1:-1]:
        try:
            value = int(token, 0)
        except ValueError as error:
            raise CarveoutArtifactError(
                f"DTS node {node.name!r} {name} has non-integer cell {token!r}"
            ) from error
        if value < 0 or value > MAX_U32:
            raise CarveoutArtifactError(
                f"DTS node {node.name!r} {name} cell is outside u32"
            )
        parsed.append(value)
    if not parsed:
        raise CarveoutArtifactError(f"DTS node {node.name!r} {name} has no cells")
    return parsed


def _strings(node: _DtsNode, name: str, *, required: bool = True) -> list[str] | None:
    raw = _property(node, name, required=required)
    if raw is None:
        return None
    strings: list[str] = []
    expect_string = True
    for token in raw:
        if expect_string:
            if not token.startswith('"'):
                raise CarveoutArtifactError(
                    f"DTS node {node.name!r} {name} is not a string list"
                )
            try:
                value = ast.literal_eval(token)
            except (SyntaxError, ValueError) as error:
                raise CarveoutArtifactError(
                    f"DTS node {node.name!r} {name} has an invalid string"
                ) from error
            if not isinstance(value, str):
                raise CarveoutArtifactError(
                    f"DTS node {node.name!r} {name} is not a string list"
                )
            strings.extend(part for part in value.split("\0") if part)
            expect_string = False
        else:
            if token != ",":
                raise CarveoutArtifactError(
                    f"DTS node {node.name!r} {name} has invalid separators"
                )
            expect_string = True
    if expect_string or not strings:
        raise CarveoutArtifactError(
            f"DTS node {node.name!r} {name} is an empty string list"
        )
    return strings


def _one_u32(node: _DtsNode, name: str) -> int:
    cells = _cells(node, name)
    assert cells is not None
    if len(cells) != 1:
        raise CarveoutArtifactError(
            f"DTS node {node.name!r} {name} must contain exactly one cell"
        )
    return cells[0]


def _combine_cells(cells: list[int]) -> int:
    value = 0
    for cell in cells:
        value = (value << 32) | cell
    return value


def _checked_range(base: int, size: int, *, label: str) -> tuple[int, int]:
    if size <= 0:
        raise CarveoutArtifactError(f"{label} has a zero-sized range")
    end = base + size
    if base < 0 or base >= MAX_ADDRESS_END or end > MAX_ADDRESS_END:
        raise CarveoutArtifactError(f"{label} exceeds the 64-bit address space")
    return base, end


def _reg_ranges(
    node: _DtsNode,
    *,
    address_cells: int,
    size_cells: int,
) -> list[tuple[int, int]]:
    cells = _cells(node, "reg")
    assert cells is not None
    stride = address_cells + size_cells
    if stride <= 0 or len(cells) % stride != 0:
        raise CarveoutArtifactError(
            f"DTS node {node.name!r} reg does not match address/size cells"
        )
    ranges: list[tuple[int, int]] = []
    for offset in range(0, len(cells), stride):
        base = _combine_cells(cells[offset : offset + address_cells])
        size = _combine_cells(cells[offset + address_cells : offset + stride])
        ranges.append(_checked_range(base, size, label=f"DTS node {node.name!r}"))
    return ranges


def _unit_address(node: _DtsNode) -> int:
    prefix, separator, unit = node.name.rpartition("@")
    if not separator or not prefix or not unit:
        raise CarveoutArtifactError(
            f"DTS carveout node {node.name!r} has no unit address"
        )
    try:
        return int(unit, 16)
    except ValueError as error:
        raise CarveoutArtifactError(
            f"DTS carveout node {node.name!r} has an invalid unit address"
        ) from error


def _single_child(node: _DtsNode, name: str) -> _DtsNode:
    matches = [child for child in node.children if child.name == name]
    if len(matches) != 1:
        raise CarveoutArtifactError(
            f"DTS must contain exactly one /{name} node, found {len(matches)}"
        )
    return matches[0]


def _ram_ranges(root: _DtsNode) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for node in root.children:
        device_type = _strings(node, "device_type", required=False)
        if not node.name.startswith("memory@") and device_type != ["memory"]:
            continue
        ranges.extend(_reg_ranges(node, address_cells=2, size_cells=2))
    if not ranges:
        raise CarveoutArtifactError("DTS contains no RAM ranges")
    ranges.sort()
    for previous, current in zip(ranges, ranges[1:]):
        if current[0] < previous[1]:
            raise CarveoutArtifactError("DTS RAM ranges overlap")
    return ranges


def _reserved_descendants(node: _DtsNode) -> list[tuple[_DtsNode, int]]:
    result: list[tuple[_DtsNode, int]] = []
    pending = [(child, 1) for child in reversed(node.children)]
    while pending:
        child, depth = pending.pop()
        result.append((child, depth))
        pending.extend((nested, depth + 1) for nested in reversed(child.children))
    return result


def _parse_carveouts(dts_text: str) -> list[dict[str, int]]:
    root, ordinary_reserved = _parse_dts(dts_text)
    if _one_u32(root, "#address-cells") != 2:
        raise CarveoutArtifactError("DTS root #address-cells must be 2")
    if _one_u32(root, "#size-cells") != 2:
        raise CarveoutArtifactError("DTS root #size-cells must be 2")

    ram_ranges = _ram_ranges(root)
    reserved = _single_child(root, "reserved-memory")
    if _one_u32(reserved, "#address-cells") != 2:
        raise CarveoutArtifactError("/reserved-memory #address-cells must be 2")
    if _one_u32(reserved, "#size-cells") != 2:
        raise CarveoutArtifactError("/reserved-memory #size-cells must be 2")
    ranges_property = _property(reserved, "ranges", required=True)
    if ranges_property:
        raise CarveoutArtifactError("/reserved-memory ranges must be empty")

    carveouts: list[dict[str, int]] = []
    dynamic_reserved: list[str] = []
    for node, depth in _reserved_descendants(reserved):
        for cell_property in ("#address-cells", "#size-cells"):
            if cell_property in node.properties and _one_u32(node, cell_property) != 2:
                raise CarveoutArtifactError(
                    f"DTS reserved-memory descendant {node.name!r} changes cell width"
                )
        compatible = _strings(node, "compatible", required=False) or []
        is_carveout = CARVEOUT_COMPATIBLE in compatible
        has_reg = "reg" in node.properties
        dynamic = "size" in node.properties or "alloc-ranges" in node.properties
        if has_reg and dynamic:
            raise CarveoutArtifactError(
                f"DTS reserved-memory node {node.name!r} mixes reg with "
                "dynamic allocation properties"
            )
        if not is_carveout:
            if dynamic:
                dynamic_reserved.append(node.name)
            if has_reg:
                regions = _reg_ranges(node, address_cells=2, size_cells=2)
                if len(regions) != 1:
                    raise CarveoutArtifactError(
                        f"DTS reserved-memory node {node.name!r} must have "
                        "exactly one reg tuple"
                    )
                start, end = regions[0]
                ordinary_reserved.append((start, end, node.name))
            continue

        if depth != 1:
            raise CarveoutArtifactError(
                f"DTS carveout node {node.name!r} must be a direct reserved-memory child"
            )
        if dynamic:
            raise CarveoutArtifactError(
                f"DTS carveout node {node.name!r} must not have dynamic size"
            )
        if "reusable" in node.properties:
            raise CarveoutArtifactError(
                f"DTS carveout node {node.name!r} must not have reusable"
            )
        if "no-map" in node.properties:
            raise CarveoutArtifactError(
                f"DTS carveout node {node.name!r} must not have no-map"
            )
        status = _strings(node, "status", required=False)
        if status is not None and status not in (["okay"], ["ok"]):
            raise CarveoutArtifactError(
                f"DTS carveout node {node.name!r} is not enabled"
            )

        vm_id = _one_u32(node, "axvisor,vm-id")
        if vm_id == 0:
            raise CarveoutArtifactError(
                f"DTS carveout node {node.name!r} has invalid VM id 0"
            )
        regions = _reg_ranges(node, address_cells=2, size_cells=2)
        if len(regions) != 1:
            raise CarveoutArtifactError(
                f"DTS carveout node {node.name!r} must have exactly one reg tuple"
            )
        base, end = regions[0]
        size = end - base
        if _unit_address(node) != base:
            raise CarveoutArtifactError(
                f"DTS carveout node {node.name!r} unit address does not match reg"
            )
        if base % CARVEOUT_ALIGNMENT != 0 or size % CARVEOUT_ALIGNMENT != 0:
            raise CarveoutArtifactError(
                f"DTS carveout node {node.name!r} base and size must be 2 MiB aligned"
            )
        contained_in_ram = any(
            ram_start <= base and end <= ram_end for ram_start, ram_end in ram_ranges
        )
        if not contained_in_ram:
            raise CarveoutArtifactError(
                f"DTS carveout node {node.name!r} is not contained in one RAM range"
            )
        carveouts.append({"vmId": vm_id, "hpa": base, "size": size, "end": end})

    if not carveouts:
        raise CarveoutArtifactError(
            f"DTS contains no dedicated VM carveouts with {CARVEOUT_COMPATIBLE!r}"
        )
    if dynamic_reserved:
        raise CarveoutArtifactError(
            f"DTS dynamic reserved-memory node {dynamic_reserved[0]!r} has no "
            "statically provable range"
        )

    by_vm: dict[int, dict[str, int]] = {}
    for carveout in carveouts:
        vm_id = carveout["vmId"]
        if vm_id in by_vm:
            raise CarveoutArtifactError(f"DTS has duplicate VM id {vm_id} carveouts")
        by_vm[vm_id] = carveout

    carveouts.sort(key=lambda item: item["hpa"])
    for previous, current in zip(carveouts, carveouts[1:]):
        if current["hpa"] < previous["end"]:
            raise CarveoutArtifactError(
                f"DTS carveout for VM {current['vmId']} overlaps VM {previous['vmId']}"
            )
    for carveout in carveouts:
        for start, end, source in ordinary_reserved:
            if carveout["hpa"] < end and start < carveout["end"]:
                raise CarveoutArtifactError(
                    f"DTS carveout for VM {carveout['vmId']} overlaps "
                    f"reserved {source!r}"
                )
    return carveouts


def _parse_vm_configs(
    vm_config_texts: dict[str, str],
) -> dict[int, tuple[str, list[tuple[int, int]]]]:
    configs: dict[int, tuple[str, list[tuple[int, int]]]] = {}
    for name, text in vm_config_texts.items():
        try:
            config = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise CarveoutArtifactError(
                f"VM config {name!r} is invalid TOML: {error}"
            ) from error
        tables: dict[str, dict[str, Any]] = {}
        for table_name in ("base", "kernel", "devices"):
            table = config.get(table_name)
            if not isinstance(table, dict):
                raise CarveoutArtifactError(
                    f"VM config {name!r} has no [{table_name}] table"
                )
            tables[table_name] = table
        base_table, kernel, devices = (
            tables["base"],
            tables["kernel"],
            tables["devices"],
        )

        def usize(value: Any, field: str, *, positive: bool = False) -> int:
            valid = isinstance(value, int) and not isinstance(value, bool)
            if not valid or value < 0 or value > MAX_USIZE or (positive and value == 0):
                raise CarveoutArtifactError(
                    f"VM config {name!r} {field} must be a usize"
                )
            return value

        vm_id = usize(base_table.get("id"), "base.id", positive=True)
        for field in ("vm_type", "cpu_num"):
            usize(base_table.get(field), f"base.{field}", positive=field == "cpu_num")
        if not isinstance(base_table.get("name"), str) or not base_table["name"]:
            raise CarveoutArtifactError(
                f"VM config {name!r} base.name must be a string"
            )
        if vm_id in configs:
            raise CarveoutArtifactError(
                f"VM config artifacts contain duplicate VM id {vm_id}"
            )
        for field in ("entry_point", "kernel_load_addr"):
            usize(kernel.get(field), f"kernel.{field}")
        if not isinstance(kernel.get("kernel_path"), str):
            raise CarveoutArtifactError(
                f"VM config {name!r} kernel.kernel_path must be a string"
            )
        for field in ("emu_devices", "passthrough_devices"):
            if not isinstance(devices.get(field), list):
                raise CarveoutArtifactError(
                    f"VM config {name!r} devices.{field} must be an array"
                )
        memory_regions = kernel.get("memory_regions")
        if not isinstance(memory_regions, list):
            raise CarveoutArtifactError(
                f"VM config {name!r} has no kernel.memory_regions array"
            )
        reserved_mappings: list[tuple[int, int]] = []
        for index, region in enumerate(memory_regions):
            if not isinstance(region, list) or len(region) != 4:
                raise CarveoutArtifactError(
                    f"VM config {name!r} memory region {index} must have four integers"
                )
            has_non_integer = any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in region
            )
            if has_non_integer:
                raise CarveoutArtifactError(
                    f"VM config {name!r} memory region {index} must have four integers"
                )
            gpa, size, flags, map_type = region
            gpa = usize(gpa, f"memory region {index} GPA")
            size = usize(size, f"memory region {index} size", positive=True)
            flags = usize(flags, f"memory region {index} flags")
            map_type = usize(map_type, f"memory region {index} map type")
            if flags & ~0x3F:
                raise CarveoutArtifactError(
                    f"VM config {name!r} memory region {index} flags contain unknown bits"
                )
            if map_type not in VALID_MAP_TYPES:
                raise CarveoutArtifactError(
                    f"VM config {name!r} memory region {index} map type must be 0, 1, or 2"
                )
            _checked_range(gpa, size, label=f"VM config {name!r} memory region {index}")
            if map_type == MAP_RESERVED:
                reserved_mappings.append((gpa, size))
        configs[vm_id] = (name, reserved_mappings)
    return configs


def validate_artifacts(
    dts_text: str,
    vm_config_texts: dict[str, str],
) -> list[dict[str, int | str]]:
    """Validate and cross-reference carveouts without executing QEMU."""

    if not vm_config_texts:
        raise CarveoutArtifactError("at least one VM config is required")
    carveouts = _parse_carveouts(dts_text)
    configs = _parse_vm_configs(vm_config_texts)
    matched_mappings: set[tuple[int, int, int]] = set()
    result: list[dict[str, int | str]] = []

    for carveout in carveouts:
        vm_id = carveout["vmId"]
        config = configs.get(vm_id)
        if config is None:
            raise CarveoutArtifactError(
                f"VM {vm_id} carveout has no matching VM config artifact"
            )
        config_name, mappings = config
        expected = (carveout["hpa"], carveout["size"])
        if mappings.count(expected) != 1:
            raise CarveoutArtifactError(
                f"VM {vm_id} carveout has no exact MapReserved mapping"
            )
        matched_mappings.add((vm_id, expected[0], expected[1]))
        result.append({**carveout, "vmConfig": config_name})

    for vm_id, (_name, mappings) in configs.items():
        for base, size in mappings:
            if (vm_id, base, size) not in matched_mappings:
                raise CarveoutArtifactError(
                    f"VM {vm_id} MapReserved mapping [{base:#x}, {base + size:#x}) "
                    "has no exact host carveout"
                )
    return sorted(result, key=lambda item: int(item["vmId"]))


def build_payload(
    *,
    carveouts: list[dict[str, int | str]],
    dts_name: str,
    dts_bytes: bytes,
    vm_config_bytes: dict[str, bytes],
) -> dict[str, Any]:
    """Build a narrowly-scoped JSON result for already validated artifacts."""
    return build_preflight_payload(
        carveouts=carveouts,
        dts_name=dts_name,
        dts_bytes=dts_bytes,
        vm_config_bytes=vm_config_bytes,
        compatible=CARVEOUT_COMPATIBLE,
        alignment=CARVEOUT_ALIGNMENT,
    )


def _read_source(path: Path, *, label: str) -> bytes:
    return read_regular_bytes(path, label=label, error_type=CarveoutArtifactError)


def _decode_source(data: bytes, *, label: str) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise CarveoutArtifactError(f"{label} is not UTF-8: {error}") from error


def _atomic_write(path: Path, data: bytes) -> None:
    publish_new_file(path, data, error_type=CarveoutArtifactError)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate dedicated host-DTS VM carveouts against exact AxVM "
            "MapReserved TOML mappings without starting QEMU"
        )
    )
    parser.add_argument("--dts", required=True, type=Path, help="decoded host DTS")
    parser.add_argument(
        "--vm-config",
        required=True,
        action="append",
        type=Path,
        help="AxVM TOML config; repeat for each VM artifact",
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="validated preflight JSON"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sources = [args.dts, *args.vm_config]
        output = args.output.resolve()
        if any(path.resolve() == output for path in sources):
            raise CarveoutArtifactError("--output must not overwrite an input artifact")

        dts_bytes = _read_source(args.dts, label="host DTS")
        dts_text = _decode_source(dts_bytes, label="host DTS")
        vm_config_texts: dict[str, str] = {}
        vm_config_bytes: dict[str, bytes] = {}
        for path in args.vm_config:
            if path.name in vm_config_texts:
                raise CarveoutArtifactError(
                    f"VM config artifact name is duplicated: {path.name}"
                )
            data = _read_source(path, label=f"VM config {path.name!r}")
            vm_config_bytes[path.name] = data
            vm_config_texts[path.name] = _decode_source(
                data, label=f"VM config {path.name!r}"
            )

        carveouts = validate_artifacts(dts_text, vm_config_texts)
        payload = build_payload(
            carveouts=carveouts,
            dts_name=args.dts.name,
            dts_bytes=dts_bytes,
            vm_config_bytes=vm_config_bytes,
        )
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        _atomic_write(args.output, serialized.encode("utf-8"))
    except CarveoutArtifactError as error:
        print(f"Host VM carveout validation failed: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(
            f"Host VM carveout validation could not read/write artifacts: {error}",
            file=sys.stderr,
        )
        return 2

    print(serialized, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
