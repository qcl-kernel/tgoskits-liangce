#!/usr/bin/env python3
"""Validate static semantics of one captured AArch64 Guest DTS and VM TOML.

This verifier intentionally consumes decoded DTS text.  It does not invoke
``dtc``, QMP, QEMU, Cargo, or a Guest.  Its JSON result binds the exact input
bytes so that the reviewed DTS and VM configuration are identifiable.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


MAX_U64 = (1 << 64) - 1
TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[{};=<>:,]|[^\s{};=<>:,]+')
PL011_ADDRESS = 0x0900_0000
PL011_SIZE = 0x1000
CONSOLE_PATH = "/pl011@9000000"
CONSOLE_REFERENCE = "serial0:115200n8"


class CapturedGuestDtbError(ValueError):
    """The supplied static DTS and VM configuration are not a valid profile."""


class Node:
    def __init__(self, name: str, parent: Node | None = None) -> None:
        self.name = name
        self.parent = parent
        self.properties: dict[str, list[list[str]]] = {}
        self.children: list[Node] = []

    @property
    def path(self) -> str:
        if self.parent is None:
            return "/"
        return f"/{self.name}" if self.parent.parent is None else f"{self.parent.path}/{self.name}"


def validate_artifacts(dts_bytes: bytes, vm_config_bytes: bytes) -> dict[str, Any]:
    """Return a narrow static-semantic result or raise ``CapturedGuestDtbError``."""

    try:
        dts_text = dts_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CapturedGuestDtbError(f"DTS is not UTF-8: {error}") from error
    try:
        vm_config = tomllib.loads(vm_config_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CapturedGuestDtbError(f"VM configuration is invalid TOML: {error}") from error
    root = _parse_dts(dts_text)
    profile = _parse_vm_config(vm_config)
    _validate_no_reserved_memory(root)
    _validate_memory(root, profile["memoryRegions"])
    _validate_interrupt_parents(root)
    _validate_cpus(root, profile)
    _validate_console(root, profile["consoleDevice"])
    return profile


def build_result(*, dts_path: Path, dts_bytes: bytes, vm_config_path: Path, vm_config_bytes: bytes, profile: dict[str, Any]) -> dict[str, Any]:
    """Build the report after validation; this does not imply runtime success."""

    return {
        "schemaVersion": 1,
        "status": "captured_guest_dtb_static_semantics_validated",
        "proofScope": "decoded-guest-dts-and-vm-config-static-semantics",
        "vm": profile,
        "sources": {
            "dts": {"path": str(dts_path), "sha256": _sha256(dts_bytes)},
            "vmConfig": {"path": str(vm_config_path), "sha256": _sha256(vm_config_bytes)},
        },
        "doesNotProve": [
            "QMP capture identity",
            "Guest boot",
            "passthrough DMA isolation",
            "Linux and Zephyr IP connectivity",
        ] + (
            ["passthrough console isolation"]
            if profile["consoleMode"] == "passthrough-or-no-vm-owned-console"
            else []
        ),
    }


def publish_result(path: Path, result: dict[str, Any]) -> None:
    """Publish a JSON report exactly once, refusing to replace evidence."""

    data = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with path.open("xb") as output:
            output.write(data)
    except FileExistsError as error:
        raise CapturedGuestDtbError(f"output already exists: {path}") from error
    except OSError as error:
        raise CapturedGuestDtbError(f"cannot publish output {path}: {error}") from error


def _parse_vm_config(config: dict[str, Any]) -> dict[str, Any]:
    base = config.get("base")
    kernel = config.get("kernel")
    if not isinstance(base, dict) or not isinstance(kernel, dict):
        raise CapturedGuestDtbError("VM configuration needs [base] and [kernel] tables")
    vm_id = _positive_int(base.get("id"), "base.id")
    cpu_num = _positive_int(base.get("cpu_num"), "base.cpu_num")
    if cpu_num != 1:
        raise CapturedGuestDtbError("captured PL011 profile requires base.cpu_num = 1")
    physical_ids = base.get("phys_cpu_ids")
    if not isinstance(physical_ids, list) or len(physical_ids) != cpu_num:
        raise CapturedGuestDtbError("base.phys_cpu_ids must contain one MPIDR per vCPU")
    mpidrs = [_u64(value, "base.phys_cpu_ids entry") for value in physical_ids]
    if len(set(mpidrs)) != len(mpidrs):
        raise CapturedGuestDtbError("base.phys_cpu_ids contains duplicate MPIDRs")
    raw_regions = kernel.get("memory_regions")
    if not isinstance(raw_regions, list) or len(raw_regions) != 1:
        raise CapturedGuestDtbError("captured PL011 profile requires one kernel.memory_regions entry")
    region = raw_regions[0]
    if not isinstance(region, list) or len(region) != 4:
        raise CapturedGuestDtbError("kernel.memory_regions entry must have four integers")
    start, size, _flags, _map_type = (_u64(value, "kernel.memory_regions entry") for value in region)
    if size == 0 or start + size > MAX_U64 + 1:
        raise CapturedGuestDtbError("kernel.memory_regions range is invalid")
    console_device = _parse_emu_devices(config)
    return {
        "id": vm_id,
        "cpuNum": cpu_num,
        "memoryRegions": [{"start": start, "size": size}],
        "mpidrs": mpidrs,
        "consoleMode": (
            "vm-owned-polling-pl011"
            if console_device is not None
            else "passthrough-or-no-vm-owned-console"
        ),
        "consoleDevice": console_device,
    }


def _parse_emu_devices(config: dict[str, Any]) -> dict[str, Any] | None:
    devices = config.get("devices")
    if not isinstance(devices, dict):
        raise CapturedGuestDtbError("VM configuration needs a [devices] table")
    raw_devices = devices.get("emu_devices")
    if not isinstance(raw_devices, list):
        raise CapturedGuestDtbError("devices.emu_devices must be a list")
    consoles: list[dict[str, Any]] = []
    for index, raw_device in enumerate(raw_devices):
        label = f"devices.emu_devices[{index}]"
        if not isinstance(raw_device, list) or len(raw_device) != 6:
            raise CapturedGuestDtbError(f"{label} must be a six-item device tuple")
        name, address, size, irq, device_type, device_config = raw_device
        if not isinstance(name, str) or not name:
            raise CapturedGuestDtbError(f"{label} name must be a non-empty string")
        address = _u64(address, f"{label} address")
        size = _u64(size, f"{label} size")
        irq = _u64(irq, f"{label} IRQ")
        device_type = _u64(device_type, f"{label} type")
        if size == 0 or address + size > MAX_U64 + 1:
            raise CapturedGuestDtbError(f"{label} range is invalid")
        if not isinstance(device_config, list):
            raise CapturedGuestDtbError(f"{label} config must be a list")
        if device_type == 0x2:
            consoles.append(
                {
                    "name": name,
                    "address": address,
                    "size": size,
                    "irq": irq,
                    "config": device_config,
                }
            )
    if len(consoles) > 1:
        raise CapturedGuestDtbError("devices.emu_devices has more than one type=0x2 console")
    return consoles[0] if consoles else None


def _validate_memory(root: Node, regions: list[dict[str, int]]) -> None:
    memories = [node for node in root.children if node.name.startswith("memory@") or _strings(node, "device_type", required=False) == ["memory"]]
    if len(memories) != 1:
        raise CapturedGuestDtbError(f"DTS must contain exactly one memory node, found {len(memories)}")
    if _one_cell(root, "#address-cells") != 2 or _one_cell(root, "#size-cells") != 2:
        raise CapturedGuestDtbError("DTS root must use two address and size cells")
    ranges = _reg(memories[0], address_cells=2, size_cells=2)
    expected = [(region["start"], region["size"]) for region in regions]
    if ranges != expected:
        raise CapturedGuestDtbError("DTS memory reg does not exactly match kernel.memory_regions")


def _validate_no_reserved_memory(root: Node) -> None:
    if any(node.name == "reserved-memory" for node in root.children):
        raise CapturedGuestDtbError(
            "DTS retains host reservation leakage at /reserved-memory"
        )


def _validate_interrupt_parents(root: Node) -> None:
    nodes = list(_walk(root))
    phandles: dict[int, Node] = {}
    for node in nodes:
        value = _cells(node, "phandle", required=False)
        if value is None:
            continue
        if len(value) != 1 or value[0] == 0:
            raise CapturedGuestDtbError(f"{node.path} has invalid phandle")
        if value[0] in phandles:
            raise CapturedGuestDtbError(f"DTS has duplicate phandle {value[0]:#x}")
        phandles[value[0]] = node

    root_parent = _interrupt_controller(root, phandles, inherited=False)
    if "arm,gic-v3" not in (_strings(root_parent, "compatible") or []):
        raise CapturedGuestDtbError("DTS root interrupt-parent must reference arm,gic-v3")
    if _one_cell(root_parent, "#interrupt-cells") != 3:
        raise CapturedGuestDtbError(
            "DTS root interrupt-parent must use three interrupt cells"
        )

    for node in nodes:
        parent = _cells(node, "interrupt-parent", required=False)
        if parent is not None:
            _interrupt_controller(node, phandles, inherited=False)

        interrupts = _cells(node, "interrupts", required=False)
        extended = _cells(node, "interrupts-extended", required=False)
        if interrupts is not None and extended is not None:
            raise CapturedGuestDtbError(
                f"{node.path} has both interrupts and interrupts-extended"
            )
        if interrupts is not None:
            controller = _interrupt_controller(node, phandles, inherited=True)
            width = _one_cell(controller, "#interrupt-cells")
            if not interrupts or len(interrupts) % width:
                raise CapturedGuestDtbError(
                    f"{node.path} interrupts does not match #interrupt-cells"
                )
        if extended is not None:
            offset = 0
            while offset < len(extended):
                controller = _controller_by_phandle(
                    extended[offset], node, phandles, "interrupts-extended"
                )
                width = _one_cell(controller, "#interrupt-cells")
                offset += 1 + width
                if width == 0 or offset > len(extended):
                    raise CapturedGuestDtbError(
                        f"{node.path} interrupts-extended has an invalid specifier"
                    )
            if not extended:
                raise CapturedGuestDtbError(
                    f"{node.path} interrupts-extended must not be empty"
                )


def _interrupt_controller(
    node: Node, phandles: dict[int, Node], *, inherited: bool
) -> Node:
    current: Node | None = node
    while current is not None:
        parent = _cells(current, "interrupt-parent", required=False)
        if parent is not None:
            if len(parent) != 1:
                raise CapturedGuestDtbError(
                    f"{current.path} interrupt-parent must contain one phandle"
                )
            return _controller_by_phandle(
                parent[0], current, phandles, "interrupt-parent"
            )
        if not inherited:
            break
        current = current.parent
    raise CapturedGuestDtbError(f"{node.path} has no resolvable interrupt-parent")


def _controller_by_phandle(
    phandle: int, node: Node, phandles: dict[int, Node], property_name: str
) -> Node:
    controller = phandles.get(phandle)
    if controller is None:
        raise CapturedGuestDtbError(
            f"{node.path} {property_name} does not reference a phandle"
        )
    if _property(controller, "interrupt-controller", required=False) is None:
        raise CapturedGuestDtbError(
            f"{node.path} {property_name} is not an interrupt-controller"
        )
    if _one_cell(controller, "#interrupt-cells") == 0:
        raise CapturedGuestDtbError(
            f"{node.path} {property_name} has zero #interrupt-cells"
        )
    return controller


def _validate_cpus(root: Node, profile: dict[str, Any]) -> None:
    cpus = _single_child(root, "cpus")
    if _one_cell(cpus, "#address-cells") != 1 or _one_cell(cpus, "#size-cells") != 0:
        raise CapturedGuestDtbError("/cpus must use one address cell and zero size cells")
    cpu_nodes = [node for node in cpus.children if node.name.startswith("cpu@") or _strings(node, "device_type", required=False) == ["cpu"]]
    if len(cpu_nodes) != profile["cpuNum"]:
        raise CapturedGuestDtbError("DTS CPU node count does not match base.cpu_num")
    mpidrs: list[int] = []
    for node in cpu_nodes:
        if _strings(node, "device_type") != ["cpu"]:
            raise CapturedGuestDtbError(f"{node.path} device_type must be cpu")
        status = _strings(node, "status", required=False)
        if status is not None and status not in (["okay"], ["ok"]):
            raise CapturedGuestDtbError(f"{node.path} CPU is not enabled")
        cells = _cells(node, "reg")
        assert cells is not None
        if len(cells) != 1:
            raise CapturedGuestDtbError(f"{node.path} CPU reg must contain one MPIDR cell")
        mpidrs.append(cells[0])
    if sorted(mpidrs) != sorted(profile["mpidrs"]):
        raise CapturedGuestDtbError("DTS CPU MPIDRs do not match base.phys_cpu_ids")

    cpu_maps = [node for node in cpus.children if node.name == "cpu-map"]
    if len(cpu_maps) > 1:
        raise CapturedGuestDtbError("/cpus has duplicate cpu-map nodes")
    if cpu_maps:
        cpu_phandles: set[int] = set()
        for node in cpu_nodes:
            value = _cells(node, "phandle")
            assert value is not None
            if len(value) != 1:
                raise CapturedGuestDtbError(
                    f"{node.path} needs one phandle when cpu-map is present"
                )
            cpu_phandles.add(value[0])
        references: list[int] = []
        for node in _walk(cpu_maps[0]):
            value = _cells(node, "cpu", required=False)
            if value is None:
                continue
            if len(value) != 1:
                raise CapturedGuestDtbError(
                    f"{node.path} cpu-map reference must contain one phandle"
                )
            references.append(value[0])
        if len(references) != len(cpu_nodes) or set(references) != cpu_phandles:
            raise CapturedGuestDtbError(
                "/cpus/cpu-map must reference every CPU phandle exactly once"
            )


def _validate_console(root: Node, console_device: dict[str, Any] | None) -> None:
    _reject_its(root)
    if console_device is None:
        _validate_passthrough_stdout(root)
        return
    pl011_nodes = [node for node in _walk(root) if node.name == "pl011@9000000"]
    if len(pl011_nodes) != 1:
        raise CapturedGuestDtbError("DTS must contain exactly one /pl011@9000000 node")
    pl011 = pl011_nodes[0]
    if pl011.path != CONSOLE_PATH:
        raise CapturedGuestDtbError("PL011 console must be a root child")
    for node in _walk(root):
        if node is not pl011 and "arm,pl011" in (_strings(node, "compatible", required=False) or []):
            raise CapturedGuestDtbError(f"DTS has another arm,pl011 node at {node.path}")
    if "arm,pl011" not in (_strings(pl011, "compatible") or []):
        raise CapturedGuestDtbError("PL011 compatible must include arm,pl011")
    if _reg(pl011, address_cells=2, size_cells=2) != [(PL011_ADDRESS, PL011_SIZE)]:
        raise CapturedGuestDtbError("PL011 reg must be 0x09000000 with size 0x1000")
    if (
        console_device["address"],
        console_device["size"],
        console_device["irq"],
        console_device["config"],
    ) != (PL011_ADDRESS, PL011_SIZE, 0, []):
        raise CapturedGuestDtbError(
            "type=0x2 console tuple must use 0x09000000, 0x1000, IRQ 0, and empty config"
        )
    if _one_cell(pl011, "reg-io-width") != 4:
        raise CapturedGuestDtbError("PL011 reg-io-width must be 4")
    if _one_cell(pl011, "current-speed") != 115200:
        raise CapturedGuestDtbError("PL011 current-speed must be 115200")
    if _strings(pl011, "status") != ["okay"]:
        raise CapturedGuestDtbError("PL011 status must be okay")
    forbidden = (
        "interrupts",
        "interrupts-extended",
        "dmas",
        "dma-names",
        "clocks",
        "clock-names",
    )
    for property_name in forbidden:
        if _property(pl011, property_name, required=False) is not None:
            raise CapturedGuestDtbError(f"PL011 must not retain {property_name}")
    aliases = _single_child(root, "aliases")
    if _strings(aliases, "serial0") != [CONSOLE_PATH]:
        raise CapturedGuestDtbError("/aliases serial0 must reference /pl011@9000000")
    chosen = _single_child(root, "chosen")
    for property_name in ("stdout-path", "linux,stdout-path"):
        if _strings(chosen, property_name) != [CONSOLE_REFERENCE]:
            raise CapturedGuestDtbError(f"/chosen {property_name} must use serial0:115200n8")
    bootargs = _strings(chosen, "bootargs")
    assert bootargs is not None
    if "earlycon=pl011,mmio32,0x9000000" not in bootargs[0].split():
        raise CapturedGuestDtbError("/chosen bootargs must retain the PL011 earlycon argument")


def _reject_its(root: Node) -> None:
    for node in _walk(root):
        if node.name.startswith("its@") or "arm,gic-v3-its" in (_strings(node, "compatible", required=False) or []):
            raise CapturedGuestDtbError(f"DTS retains an ITS node at {node.path}")


def _validate_passthrough_stdout(root: Node) -> None:
    chosen = _optional_single_child(root, "chosen")
    if chosen is None:
        return
    for property_name in ("stdout-path", "linux,stdout-path"):
        references = _strings(chosen, property_name, required=False)
        if references is not None:
            if len(references) != 1:
                raise CapturedGuestDtbError(f"/chosen {property_name} must contain one reference")
            _resolve_stdout_reference(root, references[0], property_name)


def _resolve_stdout_reference(root: Node, reference: str, property_name: str) -> None:
    target_name = reference.split(":", 1)[0]
    if not target_name:
        raise CapturedGuestDtbError(f"/chosen {property_name} has an empty reference")
    if target_name.startswith("/"):
        target_path = target_name
    else:
        aliases = _optional_single_child(root, "aliases")
        if aliases is None:
            raise CapturedGuestDtbError(f"/chosen {property_name} references missing alias {target_name}")
        targets = _strings(aliases, target_name, required=False)
        if targets is None or len(targets) != 1 or not targets[0].startswith("/"):
            raise CapturedGuestDtbError(f"/chosen {property_name} has an invalid alias {target_name}")
        target_path = targets[0]
    if not any(node.path == target_path for node in _walk(root)):
        raise CapturedGuestDtbError(f"/chosen {property_name} references missing node {target_path}")


def _parse_dts(text: str) -> Node:
    tokens = _tokenize(_strip_comments(text))
    parser = _Parser(tokens)
    return parser.parse()


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens, self.index = tokens, 0

    def parse(self) -> Node:
        self._expect("/dts-v1/")
        self._expect(";")
        self._expect("/")
        self._expect("{")
        root = Node("/")
        self._body(root)
        if self._peek() == ";":
            self._take()
        if self._peek() is not None:
            raise CapturedGuestDtbError("DTS has content after the root node")
        return root

    def _body(self, parent: Node) -> None:
        while self._peek() != "}":
            first = self._take()
            if self._peek() == ",":
                self._take()
                first = f"{first},{self._take()}"
            if self._peek() == ":":
                self._take(); first = self._take()
            if self._peek() == "{":
                self._take(); child = Node(first, parent); self._body(child); self._expect(";"); parent.children.append(child); continue
            if self._peek() == ";":
                self._take(); parent.properties.setdefault(first, []).append([]); continue
            self._expect("=")
            value: list[str] = []
            while self._peek() != ";":
                token = self._take()
                if token in ("{", "}"):
                    raise CapturedGuestDtbError(f"DTS property {first!r} has invalid delimiters")
                value.append(token)
            self._take(); parent.properties.setdefault(first, []).append(value)
        self._take()

    def _peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            raise CapturedGuestDtbError("DTS ends unexpectedly")
        self.index += 1
        return token

    def _expect(self, value: str) -> None:
        if self._take() != value:
            raise CapturedGuestDtbError(f"DTS expected {value!r}")


def _property(node: Node, name: str, *, required: bool = True) -> list[str] | None:
    values = node.properties.get(name, [])
    if len(values) > 1:
        raise CapturedGuestDtbError(f"{node.path} has duplicate {name} properties")
    if not values:
        if required:
            raise CapturedGuestDtbError(f"{node.path} has no {name} property")
        return None
    return values[0]


def _cells(node: Node, name: str, *, required: bool = True) -> list[int] | None:
    raw = _property(node, name, required=required)
    if raw is None:
        return None
    if len(raw) < 3 or raw[0] != "<" or raw[-1] != ">" or raw.count("<") != 1 or raw.count(">") != 1:
        raise CapturedGuestDtbError(f"{node.path} {name} must be one cell list")
    return [_u32(token, f"{node.path} {name}") for token in raw[1:-1]]


def _strings(node: Node, name: str, *, required: bool = True) -> list[str] | None:
    raw = _property(node, name, required=required)
    if raw is None:
        return None
    values: list[str] = []
    expect_value = True
    for token in raw:
        if expect_value:
            if not token.startswith('"'):
                raise CapturedGuestDtbError(f"{node.path} {name} must be a string list")
            try:
                decoded = ast.literal_eval(token)
            except (SyntaxError, ValueError) as error:
                raise CapturedGuestDtbError(f"{node.path} {name} has an invalid string") from error
            values.extend(item for item in decoded.split("\0") if item)
        elif token != ",":
            raise CapturedGuestDtbError(f"{node.path} {name} has invalid separators")
        expect_value = not expect_value
    if expect_value or not values:
        raise CapturedGuestDtbError(f"{node.path} {name} is empty")
    return values


def _one_cell(node: Node, name: str) -> int:
    values = _cells(node, name)
    assert values is not None
    if len(values) != 1:
        raise CapturedGuestDtbError(f"{node.path} {name} must have exactly one cell")
    return values[0]


def _reg(node: Node, *, address_cells: int, size_cells: int) -> list[tuple[int, int]]:
    cells = _cells(node, "reg")
    assert cells is not None
    stride = address_cells + size_cells
    if len(cells) % stride:
        raise CapturedGuestDtbError(f"{node.path} reg has the wrong cell count")
    result = []
    for offset in range(0, len(cells), stride):
        start = _combine(cells[offset : offset + address_cells])
        size = _combine(cells[offset + address_cells : offset + stride])
        if size == 0 or start + size > MAX_U64 + 1:
            raise CapturedGuestDtbError(f"{node.path} reg has an invalid range")
        result.append((start, size))
    return result


def _single_child(root: Node, name: str) -> Node:
    nodes = [node for node in root.children if node.name == name]
    if len(nodes) != 1:
        raise CapturedGuestDtbError(f"DTS must contain exactly one /{name} node")
    return nodes[0]


def _optional_single_child(root: Node, name: str) -> Node | None:
    nodes = [node for node in root.children if node.name == name]
    if len(nodes) > 1:
        raise CapturedGuestDtbError(f"DTS has duplicate /{name} nodes")
    return nodes[0] if nodes else None


def _walk(root: Node):
    yield root
    for child in root.children:
        yield from _walk(child)


def _strip_comments(text: str) -> str:
    result: list[str] = []
    index = 0
    quoted = escaped = False
    while index < len(text):
        char, following = text[index], text[index + 1 : index + 2]
        if quoted:
            result.append(char); escaped, quoted = (False, quoted) if escaped else (char == "\\", False if char == '"' else quoted); index += 1; continue
        if char == '"':
            quoted = True; result.append(char); index += 1; continue
        if char == "/" and following == "/":
            index = text.find("\n", index + 2)
            if index < 0: break
            result.append("\n"); index += 1; continue
        if char == "/" and following == "*":
            end = text.find("*/", index + 2)
            if end < 0: raise CapturedGuestDtbError("DTS has an unterminated block comment")
            result.append("\n" * text[index : end + 2].count("\n")); index = end + 2; continue
        result.append(char); index += 1
    if quoted: raise CapturedGuestDtbError("DTS has an unterminated string")
    return "".join(result)


def _tokenize(text: str) -> list[str]:
    tokens, cursor = [], 0
    for match in TOKEN.finditer(text):
        if text[cursor : match.start()].strip():
            raise CapturedGuestDtbError("DTS contains unsupported syntax")
        tokens.append(match.group()); cursor = match.end()
    if text[cursor:].strip(): raise CapturedGuestDtbError("DTS contains trailing unsupported syntax")
    return tokens


def _u32(token: str, label: str) -> int:
    value = _u64_literal(token, label)
    if value > 0xFFFFFFFF: raise CapturedGuestDtbError(f"{label} cell exceeds u32")
    return value


def _u64(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_U64:
        raise CapturedGuestDtbError(f"{label} must be an unsigned 64-bit integer")
    return value


def _positive_int(value: object, label: str) -> int:
    value = _u64(value, label)
    if value == 0: raise CapturedGuestDtbError(f"{label} must be positive")
    return value


def _u64_literal(token: str, label: str) -> int:
    try: value = int(token, 0)
    except ValueError as error: raise CapturedGuestDtbError(f"{label} has a non-integer cell") from error
    if value < 0 or value > MAX_U64: raise CapturedGuestDtbError(f"{label} exceeds u64")
    return value


def _combine(cells: list[int]) -> int:
    value = 0
    for cell in cells: value = (value << 32) | cell
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dts", required=True, type=Path)
    parser.add_argument("--vm-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        dts_bytes, vm_bytes = args.dts.read_bytes(), args.vm_config.read_bytes()
        profile = validate_artifacts(dts_bytes, vm_bytes)
        publish_result(args.output, build_result(dts_path=args.dts, dts_bytes=dts_bytes, vm_config_path=args.vm_config, vm_config_bytes=vm_bytes, profile=profile))
    except (CapturedGuestDtbError, OSError) as error:
        print(f"captured Guest-DTB semantic validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
