#!/usr/bin/env python3
"""Validate an observed QEMU Arm virt dual-guest virtio-mmio layout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


# Locked after a live QEMU 8.2.2 qtree+DTB probe. A future QEMU layout drift is
# an explicit validation failure until a new live capture updates this contract.
MMIO_BASE = 0x0A00_0000
MMIO_STRIDE = 0x200
GIC_SPI_OFFSET_BASE = 16
GIC_SPI_INTID_BASE = 32

EXPECTED_LAYOUT: dict[str, dict[str, object]] = {
    "linux-block": {
        "guest": "linux",
        "purpose": "root-block",
        "qemuType": "virtio-blk-device",
        "slot": 0,
    },
    "linux-net": {
        "guest": "linux",
        "purpose": "ip-network",
        "qemuType": "virtio-net-device",
        "slot": 1,
    },
    "zephyr-net": {
        "guest": "zephyr",
        "purpose": "ip-network",
        "qemuType": "virtio-net-device",
        "slot": 2,
    },
}

DEVICE_PATTERN = re.compile(
    r'^(?P<indent>[ \t]*)dev:\s+(?P<type>[A-Za-z0-9_.+-]+),\s+'
    r'id\s+"(?P<id>[^"]*)"\s*$'
)
MMIO_PATTERN = re.compile(
    r"^(?P<indent>[ \t]*)mmio\s+"
    r"(?P<base>(?:0x)?[0-9a-fA-F]+)/(?P<size>(?:0x)?[0-9a-fA-F]+)\s*$"
)
BUS_PATTERN = re.compile(
    r"^(?P<indent>[ \t]*)bus:\s+virtio-mmio-bus\.(?P<slot>[0-9]+)\s*$"
)
NODE_PATTERN = re.compile(
    r"(?ms)^[ \t]*(?P<name>[A-Za-z0-9,._+-]+)@(?P<unit>[0-9a-fA-F]+)"
    r"[ \t]*\{(?P<body>.*?)^[ \t]*\};"
)


class TopologyError(ValueError):
    """The captured QEMU topology does not satisfy the fixed resource plan."""


def _parse_hex(value: str, *, field: str) -> int:
    try:
        return int(value.removeprefix("0x"), 16)
    except ValueError as error:
        raise TopologyError(f"invalid hexadecimal {field}: {value}") from error


def _extract_qtree_response(qmp_text: str) -> str:
    qtree_responses: list[str] = []
    for line_number, line in enumerate(qmp_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise TopologyError(
                f"QMP line {line_number} is not valid JSON: {error.msg}"
            ) from error
        if not isinstance(message, dict):
            raise TopologyError(f"QMP line {line_number} is not a JSON object")
        if message.get("id") != "qtree":
            continue
        if "error" in message:
            raise TopologyError(f"QEMU rejected info qtree: {message['error']!r}")
        response = message.get("return")
        if not isinstance(response, str):
            raise TopologyError("QMP qtree response is not text")
        qtree_responses.append(response)

    if len(qtree_responses) != 1:
        raise TopologyError(
            "expected exactly one QMP response with id 'qtree', "
            f"found {len(qtree_responses)}"
        )
    return qtree_responses[0]


def _parse_occupied_transports(qtree: str) -> list[dict[str, object]]:
    lines = qtree.splitlines()
    occupied: list[dict[str, object]] = []

    for index, line in enumerate(lines):
        transport = DEVICE_PATTERN.match(line)
        if transport is None or transport.group("type") != "virtio-mmio":
            continue

        transport_indent = len(transport.group("indent"))
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            if candidate.strip():
                candidate_indent = len(candidate) - len(candidate.lstrip(" \t"))
                if candidate_indent <= transport_indent:
                    break
            end += 1
        block = lines[index + 1 : end]

        mmio_matches = [
            match
            for block_line in block
            if (match := MMIO_PATTERN.match(block_line)) is not None
        ]
        bus_matches = [
            (position, match)
            for position, block_line in enumerate(block)
            if (match := BUS_PATTERN.match(block_line)) is not None
        ]
        if len(mmio_matches) != 1 or len(bus_matches) != 1:
            raise TopologyError(
                "each virtio-mmio transport must expose exactly one MMIO range "
                "and one numbered child bus"
            )

        bus_position, bus = bus_matches[0]
        bus_indent = len(bus.group("indent"))
        children = []
        for block_line in block[bus_position + 1 :]:
            child = DEVICE_PATTERN.match(block_line)
            if child is not None and len(child.group("indent")) == bus_indent + 2:
                children.append(child)
        if not children:
            continue
        if len(children) != 1:
            raise TopologyError(
                f"virtio-mmio-bus.{bus.group('slot')} has multiple child devices"
            )

        child = children[0]
        occupied.append(
            {
                "qemuId": child.group("id"),
                "qemuType": child.group("type"),
                "slot": int(bus.group("slot")),
                "bus": f"virtio-mmio-bus.{bus.group('slot')}",
                "mmioBase": _parse_hex(
                    mmio_matches[0].group("base"), field="MMIO base"
                ),
                "mmioSize": _parse_hex(
                    mmio_matches[0].group("size"), field="MMIO size"
                ),
            }
        )

    if not occupied:
        raise TopologyError("QEMU qtree contains no occupied virtio-mmio transports")
    return occupied


def _property_cells(body: str, name: str) -> list[int]:
    pattern = re.compile(
        rf"(?ms)^[ \t]*{re.escape(name)}[ \t]*=[ \t]*<(?P<cells>.*?)>[ \t]*;"
    )
    match = pattern.search(body)
    if match is None:
        raise TopologyError(f"virtio-mmio DT node has no {name} property")
    tokens = re.findall(r"0x[0-9a-fA-F]+|[0-9]+", match.group("cells"))
    if not tokens:
        raise TopologyError(f"virtio-mmio DT node has an empty {name} property")
    return [int(token, 0) for token in tokens]


def _parse_dts_nodes(dts_text: str) -> dict[int, dict[str, int]]:
    nodes: dict[int, dict[str, int]] = {}
    for match in NODE_PATTERN.finditer(dts_text):
        body = match.group("body")
        if re.search(
            r'(?m)^[ \t]*compatible[ \t]*=[ \t]*"virtio,mmio"[ \t]*;',
            body,
        ) is None:
            continue

        reg = _property_cells(body, "reg")
        interrupts = _property_cells(body, "interrupts")
        if len(reg) != 4:
            raise TopologyError(
                "QEMU Arm virt virtio-mmio reg must contain two address and two size cells"
            )
        if len(interrupts) != 3:
            raise TopologyError(
                "QEMU Arm virt virtio-mmio interrupts must contain type, SPI offset, and flags"
            )

        base = (reg[0] << 32) | reg[1]
        size = (reg[2] << 32) | reg[3]
        unit_address = int(match.group("unit"), 16)
        if unit_address != base:
            raise TopologyError(
                f"DT unit address {unit_address:#x} does not match reg base {base:#x}"
            )
        if interrupts[0] != 0:
            raise TopologyError(
                f"DT interrupt for virtio-mmio@{base:x} is not a GIC SPI"
            )
        if base in nodes:
            raise TopologyError(f"duplicate virtio-mmio DT base {base:#x}")
        nodes[base] = {
            "mmioSize": size,
            "spiOffset": interrupts[1],
            "gicIntid": interrupts[1] + GIC_SPI_INTID_BASE,
            "irqFlags": interrupts[2],
        }

    if not nodes:
        raise TopologyError("DTS contains no compatible = \"virtio,mmio\" nodes")
    return nodes


def inspect_topology(qmp_text: str, dts_text: str) -> list[dict[str, object]]:
    """Cross-reference occupied qtree buses with MMIO and SPI data from DT."""

    qtree = _extract_qtree_response(qmp_text)
    transports = _parse_occupied_transports(qtree)
    dt_nodes = _parse_dts_nodes(dts_text)
    devices: list[dict[str, object]] = []

    for transport in transports:
        base = int(transport["mmioBase"])
        node = dt_nodes.get(base)
        if node is None:
            raise TopologyError(
                f"occupied qtree transport at {base:#x} has no matching DT node"
            )
        if int(transport["mmioSize"]) != node["mmioSize"]:
            raise TopologyError(
                f"qtree/DT MMIO size mismatch at {base:#x}: "
                f"{int(transport['mmioSize']):#x} != {node['mmioSize']:#x}"
            )

        device_id = str(transport["qemuId"])
        expected = EXPECTED_LAYOUT.get(device_id, {})
        devices.append(
            {
                **transport,
                **node,
                "guest": expected.get("guest", "unexpected"),
                "purpose": expected.get("purpose", "unexpected"),
            }
        )

    return devices


def _require_integer(device: dict[str, object], field: str) -> int:
    value = device.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TopologyError(
            f"device {device.get('qemuId', '<unknown>')} has non-integer {field}"
        )
    return value


def validate_topology(devices: list[dict[str, object]]) -> None:
    """Reject overlaps and any drift from the fixed three-device layout."""

    by_id: dict[str, dict[str, object]] = {}
    slots: dict[int, str] = {}
    spis: dict[int, str] = {}
    ranges: list[tuple[int, int, str]] = []

    for device in devices:
        device_id = device.get("qemuId")
        if not isinstance(device_id, str) or not device_id:
            raise TopologyError("occupied virtio-mmio device has no QEMU id")
        if device_id in by_id:
            raise TopologyError(f"duplicate QEMU device id: {device_id}")
        by_id[device_id] = device

        slot = _require_integer(device, "slot")
        if slot in slots:
            raise TopologyError(
                f"slot overlap: {device_id} and {slots[slot]} both use slot {slot}"
            )
        slots[slot] = device_id

        spi = _require_integer(device, "gicIntid")
        if spi in spis:
            raise TopologyError(
                f"SPI overlap: {device_id} and {spis[spi]} both use INTID {spi}"
            )
        spis[spi] = device_id

        base = _require_integer(device, "mmioBase")
        size = _require_integer(device, "mmioSize")
        if size <= 0:
            raise TopologyError(f"device {device_id} has a non-positive MMIO size")
        ranges.append((base, base + size, device_id))

    ranges.sort()
    for previous, current in zip(ranges, ranges[1:]):
        if current[0] < previous[1]:
            raise TopologyError(
                "MMIO overlap: "
                f"{previous[2]} [{previous[0]:#x}, {previous[1]:#x}) and "
                f"{current[2]} [{current[0]:#x}, {current[1]:#x})"
            )

    expected_ids = set(EXPECTED_LAYOUT)
    actual_ids = set(by_id)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        unexpected = sorted(actual_ids - expected_ids)
        raise TopologyError(
            f"device inventory mismatch: missing={missing}, unexpected={unexpected}"
        )

    for device_id, expected in EXPECTED_LAYOUT.items():
        device = by_id[device_id]
        slot = int(expected["slot"])
        fixed = {
            "qemuType": expected["qemuType"],
            "slot": slot,
            "bus": f"virtio-mmio-bus.{slot}",
            "mmioBase": MMIO_BASE + slot * MMIO_STRIDE,
            "mmioSize": MMIO_STRIDE,
            "spiOffset": GIC_SPI_OFFSET_BASE + slot,
            "gicIntid": GIC_SPI_INTID_BASE + GIC_SPI_OFFSET_BASE + slot,
            "irqFlags": 1,
        }
        mismatches = [
            f"{field}={device.get(field)!r} (expected {value!r})"
            for field, value in fixed.items()
            if device.get(field) != value
        ]
        if mismatches:
            raise TopologyError(
                f"fixed mapping mismatch for {device_id}: " + ", ".join(mismatches)
            )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _serialized_device(device: dict[str, object]) -> dict[str, object]:
    return {
        "guest": device["guest"],
        "purpose": device["purpose"],
        "qemuId": device["qemuId"],
        "qemuType": device["qemuType"],
        "slot": device["slot"],
        "bus": device["bus"],
        "mmioBase": f"{int(device['mmioBase']):#010x}",
        "mmioSize": f"{int(device['mmioSize']):#x}",
        "spiOffset": device["spiOffset"],
        "gicIntid": device["gicIntid"],
        "irqFlags": device["irqFlags"],
    }


def build_payload(
    *,
    devices: list[dict[str, object]],
    qmp_path: Path,
    dts_path: Path,
    dtb_path: Path,
    qemu_version_path: Path,
    qemu_version: str,
) -> dict[str, Any]:
    """Build a scoped evidence document from files produced by the probe."""

    sources = {}
    for name, path in (
        ("qmpQtree", qmp_path),
        ("hostDts", dts_path),
        ("hostDtb", dtb_path),
        ("qemuVersion", qemu_version_path),
    ):
        sources[name] = {
            "path": path.name,
            "sha256": _sha256(path),
        }

    return {
        "schemaVersion": 1,
        "artifactStatus": "probe-generated-unreviewed",
        "status": "topology_validated",
        "proofScope": "qemu-aarch64-virtio-mmio-resource-map",
        "doesNotProve": [
            "AxVisor booted both guests",
            "guest DTBs expose only their owned devices",
            "passthrough DMA is isolated",
            "Linux and Zephyr have IP connectivity",
            "dual-guest stability",
        ],
        "sourcePathBase": "topology.json parent directory",
        "qemuVersion": qemu_version,
        "fixedContract": {
            "machine": "virt,virtualization=on,gic-version=3",
            "mmioBase": f"{MMIO_BASE:#010x}",
            "mmioStride": f"{MMIO_STRIDE:#x}",
            "spiOffsetBase": GIC_SPI_OFFSET_BASE,
            "networkBackend": "qemu-hubport",
            "networkHubId": 0,
        },
        "devices": [
            _serialized_device(device)
            for device in sorted(devices, key=lambda item: int(item["slot"]))
        ],
        "sources": sources,
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cross-check a live QEMU Arm virt qtree/DTB capture against the "
            "fixed Linux+Zephyr virtio-mmio resource plan"
        )
    )
    parser.add_argument("--qmp", required=True, type=Path, help="raw QMP JSONL")
    parser.add_argument("--dts", required=True, type=Path, help="decoded host DTS")
    parser.add_argument("--dtb", required=True, type=Path, help="raw host DTB")
    parser.add_argument(
        "--qemu-version-file",
        required=True,
        type=Path,
        help="QEMU --version output captured by the probe",
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="validated topology JSON"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        bundle_directory = args.output.resolve().parent
        for source in (
            args.qmp,
            args.dts,
            args.dtb,
            args.qemu_version_file,
        ):
            if source.resolve().parent != bundle_directory:
                raise TopologyError(
                    "all raw inputs and --output must share one evidence directory"
                )
        qmp_text = args.qmp.read_text(encoding="utf-8")
        dts_text = args.dts.read_text(encoding="utf-8")
        dtb = args.dtb.read_bytes()
        qemu_version = args.qemu_version_file.read_text(
            encoding="utf-8", errors="replace"
        ).strip()
        if len(dtb) < 4 or dtb[:4] != bytes.fromhex("d00dfeed"):
            raise TopologyError("host DTB does not start with the flattened-DT magic")
        if not qemu_version:
            raise TopologyError("QEMU version capture is empty")

        devices = inspect_topology(qmp_text, dts_text)
        validate_topology(devices)
        payload = build_payload(
            devices=devices,
            qmp_path=args.qmp,
            dts_path=args.dts,
            dtb_path=args.dtb,
            qemu_version_path=args.qemu_version_file,
            qemu_version=qemu_version.splitlines()[0],
        )
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        _atomic_write(args.output, serialized)
    except TopologyError as error:
        print(f"Dual-guest topology validation failed: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(
            "Dual-guest topology validation could not read/write evidence: "
            f"{error}",
            file=sys.stderr,
        )
        return 2

    print(serialized, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
