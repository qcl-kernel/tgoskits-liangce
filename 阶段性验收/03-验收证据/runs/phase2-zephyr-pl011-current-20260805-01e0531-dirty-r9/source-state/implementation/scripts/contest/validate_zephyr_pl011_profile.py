#!/usr/bin/env python3
"""Validate the polling-only Zephyr PL011 template and optional build output."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


MAX_TEXT_BYTES = 4 * 1024 * 1024
REQUIRED_KCONFIG = {
    "CONFIG_SERIAL": "y",
    "CONFIG_UART_PL011": "y",
    "CONFIG_UART_CONSOLE": "y",
    "CONFIG_UART_INTERRUPT_DRIVEN": "n",
    "CONFIG_UART_ASYNC_API": "n",
    "CONFIG_DMA": "n",
    "CONFIG_SHELL_BACKEND_SERIAL": "n",
}
FORBIDDEN_UART_PROPERTIES = (
    "interrupts",
    "interrupts-extended",
    "interrupt-names",
    "dmas",
    "dma-names",
)
BINDING_UNSUPPORTED_UART_PROPERTIES = ("reg-io-width",)


class ValidationError(ValueError):
    """A profile or build artifact violates the polling-only contract."""


def read_bounded_text(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ValidationError(f"cannot stat {path}: {error}") from error
    if size > MAX_TEXT_BYTES:
        raise ValidationError(f"{path} exceeds the {MAX_TEXT_BYTES}-byte limit")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValidationError(f"cannot read UTF-8 text from {path}: {error}") from error


def parse_kconfig(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        match = re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=(.+)", line)
        if match is None:
            match = re.fullmatch(r"# (CONFIG_[A-Z0-9_]+) is not set", line)
            if match is None:
                continue
            key, value = match.group(1), "n"
        else:
            key, value = match.groups()
        if key in values:
            raise ValidationError(f"duplicate {key} at line {line_number}")
        values[key] = value
    return values


def validate_kconfig(
    text: str, *, label: str, allow_missing_disabled: bool = False
) -> None:
    values = parse_kconfig(text)
    errors = []
    for key, expected in REQUIRED_KCONFIG.items():
        actual = values.get(key)
        if actual == expected:
            continue
        # Kconfig omits symbols whose dependencies are unsatisfied.  For a
        # final generated .config that is equivalent to disabled, but the
        # source profile must still spell out every fail-closed setting.
        if allow_missing_disabled and expected == "n" and actual is None:
            continue
        errors.append(f"{key} must be {expected}, got {actual or 'missing'}")
    if errors:
        raise ValidationError(f"{label}: " + "; ".join(errors))


def extract_braced_block(text: str, marker: re.Pattern[str], *, label: str) -> str:
    match = marker.search(text)
    if match is None:
        raise ValidationError(f"{label} block is missing")
    opening = text.find("{", match.start())
    if opening < 0:
        raise ValidationError(f"{label} has no opening brace")

    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index]
    raise ValidationError(f"{label} has no closing brace")


def normalized_cells(text: str) -> str:
    return " ".join(text.replace("\n", " ").split())


def reject_assigned_capabilities(block: str, *, label: str) -> None:
    for property_name in FORBIDDEN_UART_PROPERTIES:
        if re.search(rf"(?m)^\s*{re.escape(property_name)}\s*=", block):
            raise ValidationError(f"{label} assigns forbidden {property_name}")


def reject_binding_unsupported_properties(block: str, *, label: str) -> None:
    for property_name in BINDING_UNSUPPORTED_UART_PROPERTIES:
        if re.search(rf"(?m)^\s*{re.escape(property_name)}\s*=", block):
            raise ValidationError(
                f"{label} assigns {property_name}, which Zephyr arm,pl011 does not bind"
            )


def validate_overlay(text: str) -> None:
    uart = extract_braced_block(
        text, re.compile(r"(?m)^\s*&uart0\s*\{"), label="overlay &uart0"
    )
    chosen = extract_braced_block(
        text, re.compile(r"(?m)^\s*chosen\s*\{"), label="overlay /chosen"
    )
    normalized = normalized_cells(uart)
    required = (
        'compatible = "arm,pl011";',
        "reg = <0x0 0x09000000 0x0 0x1000>;",
        "current-speed = <115200>;",
        'status = "okay";',
    )
    for fragment in required:
        if fragment not in normalized:
            raise ValidationError(f"overlay &uart0 is missing {fragment}")
    reject_assigned_capabilities(uart, label="overlay &uart0")
    reject_binding_unsupported_properties(uart, label="overlay &uart0")
    for property_name in FORBIDDEN_UART_PROPERTIES:
        if f"/delete-property/ {property_name};" not in normalized:
            raise ValidationError(
                f"overlay &uart0 does not delete inherited {property_name}"
            )
    if "zephyr,console = &uart0;" not in normalized_cells(chosen):
        raise ValidationError("overlay /chosen does not select &uart0")


def validate_final_dts(text: str) -> None:
    uart = extract_braced_block(
        text,
        re.compile(r"(?m)^\s*(?:[A-Za-z0-9_]+:\s*)?uart@9000000\s*\{"),
        label="final uart@9000000",
    )
    normalized = normalized_cells(uart)
    required_patterns = (
        re.compile(r'compatible\s*=\s*"arm,pl011(?:\\0[^\"]*)?"\s*;'),
        re.compile(
            r"reg\s*=\s*<\s*0x0+\s+0x0?9000000\s+0x0+\s+0x1000\s*>\s*;"
        ),
    )
    for pattern in required_patterns:
        if pattern.search(normalized) is None:
            raise ValidationError(
                f"final uart@9000000 does not match {pattern.pattern}"
            )
    reject_assigned_capabilities(uart, label="final uart@9000000")
    reject_binding_unsupported_properties(uart, label="final uart@9000000")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overlay", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--final-dts", type=Path)
    parser.add_argument("--final-config", type=Path)
    args = parser.parse_args()
    if (args.final_dts is None) != (args.final_config is None):
        parser.error("--final-dts and --final-config must be supplied together")
    return args


def main() -> int:
    args = parse_args()
    try:
        validate_overlay(read_bounded_text(args.overlay))
        validate_kconfig(read_bounded_text(args.config), label="profile config")
        final_artifacts = args.final_dts is not None
        if final_artifacts:
            validate_final_dts(read_bounded_text(args.final_dts))
            validate_kconfig(
                read_bounded_text(args.final_config),
                label="final .config",
                allow_missing_disabled=True,
            )
    except ValidationError as error:
        print(f"Zephyr polling PL011 profile validation failed: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "schemaVersion": 1,
                "status": (
                    "final_artifacts_static_validated"
                    if final_artifacts
                    else "template_validated"
                ),
                "pollingOnly": True,
                "guestPhysicalAddress": "0x09000000",
                "mmioLength": "0x1000",
                "registerAccessWidth": "Zephyr arm,pl011 binding default",
                "irqDelivery": "none",
                "dma": "disabled",
                "doesNotProve": [
                    "Zephyr was rebuilt from a locked source revision",
                    "the resulting image booted under AxVisor",
                    "console bytes were drained into a VM-owned sink",
                    "dual-guest DMA, IP networking, or soak gates passed",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
