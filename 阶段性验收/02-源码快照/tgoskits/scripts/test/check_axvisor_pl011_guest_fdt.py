#!/usr/bin/env python3
"""Static contract for VM-owned polling PL011 Guest FDT wiring."""

from __future__ import annotations

import runpy
import subprocess
import sys
import tomllib
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_FDT = (
    WORKSPACE_ROOT / "virtualization/axvm/src/boot/fdt/core/console.rs"
)
CORE_MOD = WORKSPACE_ROOT / "virtualization/axvm/src/boot/fdt/core/mod.rs"
AARCH64_CAPABILITIES = (
    WORKSPACE_ROOT / "virtualization/axvm/src/arch/aarch64/capabilities.rs"
)
LINUX_DUAL = (
    WORKSPACE_ROOT
    / "os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml"
)
ZEPHYR_DUAL = (
    WORKSPACE_ROOT
    / "os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml"
)
ZEPHYR_OVERLAY = (
    WORKSPACE_ROOT / "configs/contest/zephyr/qemu-cortex-a53-pl011-polling.overlay"
)
ZEPHYR_CONFIG = (
    WORKSPACE_ROOT / "configs/contest/zephyr/qemu-cortex-a53-pl011-polling.conf"
)
ZEPHYR_VALIDATOR = (
    WORKSPACE_ROOT / "scripts/contest/validate_zephyr_pl011_profile.py"
)
DESIGN = (
    WORKSPACE_ROOT
    / "docs/docs/development/aarch64-dual-guest-console-isolation.md"
)
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"

def require_fragments(
    text: str, fragments: tuple[str, ...], *, label: str, errors: list[str]
) -> None:
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{label} is missing `{fragment}`")


def read_text(path: Path, *, label: str, errors: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"{label} cannot be read: {error}")
        return ""


def check_vm_config(
    path: Path, *, label: str, expected_name: str, errors: list[str]
) -> None:
    text = read_text(path, label=label, errors=errors)
    if not text:
        return
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        errors.append(f"{label} is invalid TOML: {error}")
        return

    devices = config.get("devices", {})
    consoles = [
        entry
        for entry in devices.get("emu_devices", [])
        if isinstance(entry, list) and len(entry) >= 5 and entry[4] == 0x2
    ]
    polling_console = [expected_name, 0x0900_0000, 0x1000, 0, 0x2, []]
    if consoles != [polling_console]:
        errors.append(f"{label} must own exactly one TX-only polling PL011")
    if ["/pl011@9000000"] not in devices.get("excluded_devices", []):
        errors.append(f"{label} no longer excludes the host PL011")
    if any(
        isinstance(entry, list)
        and len(entry) >= 3
        and entry[0] == "/pl011@9000000"
        for entry in devices.get("passthrough_devices", [])
    ):
        errors.append(f"{label} maps the host PL011 as a passthrough device")
    if "# status: blocked_dma_console" not in text:
        errors.append(f"{label} overstates dual-guest readiness")


def main() -> int:
    errors: list[str] = []

    console = read_text(CONSOLE_FDT, label="Guest FDT console module", errors=errors)
    if console:
        if len(console.splitlines()) >= 800:
            errors.append("Guest FDT console module reached the split threshold")
        production = console.split("#[cfg(test)]", maxsplit=1)[0]
        require_fragments(
            production,
            (
                "pub(crate) fn install_vm_owned_polling_pl011",
                "EmulatedDeviceType::Console",
                "PL011_MMIO_SIZE",
                "TX_ONLY_IRQ_SENTINEL",
                "fn stale_console_paths(",
                "reg_overlaps(reg, target_start, target_end)",
                'compatible == "arm,pl011"',
                "fn remove_stale_console_nodes(",
                'prop_strings("compatible", &["arm,pl011", "arm,primecell"])',
                'prop_u32("reg-io-width", 4)',
                'prop_string("serial0", node_path)',
                'prop_string("stdout-path", POLLING_STDOUT_PATH)',
                "earlycon=pl011,mmio32,",
                "super::sanitize::sanitize_guest_fdt(tree.inner_mut())?;",
            ),
            label="Guest FDT console production path",
            errors=errors,
        )
        for forbidden in (
            'prop_u32s("interrupts"',
            'Property::new("interrupts"',
            'Property::new("dmas"',
            'Property::new("dma-names"',
            "base_hpa",
        ):
            if forbidden in production:
                errors.append(
                    f"Guest FDT console path acquired forbidden capability `{forbidden}`"
                )
        require_fragments(
            console,
            (
                "no_console_config_preserves_the_tree",
                "rejects_duplicate_or_non_polling_console_configs",
                "replaces_provided_physical_pl011_with_polling_node",
                "same_gpa_non_pl011_node_fails_closed",
                "interior_gpa_non_pl011_node_without_size_fails_closed",
                "provided_console_does_not_become_a_passthrough_claim",
                "generated_tree_gets_polling_console_and_mmio32_earlycon",
                "reapplying_the_profile_is_idempotent",
            ),
            label="Guest FDT console test appendix",
            errors=errors,
        )

    core_mod = read_text(CORE_MOD, label="Guest FDT module boundary", errors=errors)
    require_fragments(
        core_mod,
        ("pub(crate) mod console;",),
        label="Guest FDT module boundary",
        errors=errors,
    )

    capabilities = read_text(
        AARCH64_CAPABILITIES, label="AArch64 FDT capability path", errors=errors
    )
    require_fragments(
        capabilities,
        (
            "core::console::install_vm_owned_polling_pl011",
            "let runtime_fdt = super::fdt::core::patch_guest_fdt_for_runtime(",
            "let provided_fdt =",
            "super::fdt::update_cpu_node(&provided_fdt, host_fdt.as_ref(), crate_config)?;",
        ),
        label="AArch64 FDT capability path",
        errors=errors,
    )
    if capabilities.count("core::console::install_vm_owned_polling_pl011") != 2:
        errors.append("AArch64 must wire polling PL011 into provided and runtime FDT paths")

    check_vm_config(
        LINUX_DUAL, label="Linux dual config", expected_name="linux", errors=errors
    )
    check_vm_config(
        ZEPHYR_DUAL,
        label="Zephyr dual config",
        expected_name="zephyr",
        errors=errors,
    )

    validator = read_text(
        ZEPHYR_VALIDATOR, label="Zephyr polling profile validator", errors=errors
    )
    if validator:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ZEPHYR_VALIDATOR),
                    "--overlay",
                    str(ZEPHYR_OVERLAY),
                    "--config",
                    str(ZEPHYR_CONFIG),
                ],
                cwd=WORKSPACE_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"Zephyr polling profile validator could not run: {error}")
        else:
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip()
                errors.append(f"Zephyr polling template validation failed: {detail}")
            validator_namespace = runpy.run_path(str(ZEPHYR_VALIDATOR))
            invalid_overlay = ZEPHYR_OVERLAY.read_text(encoding="utf-8").replace(
                "    current-speed = <115200>;",
                "    reg-io-width = <4>;\n    current-speed = <115200>;",
            )
            try:
                validator_namespace["validate_overlay"](invalid_overlay)
            except validator_namespace["ValidationError"] as error:
                if "does not bind" not in str(error):
                    errors.append(
                        "Zephyr polling validator reports the wrong reg-io-width failure"
                    )
            else:
                errors.append(
                    "Zephyr polling validator accepts unsupported reg-io-width"
                )
            dtc_formatted_uart = """
            /dts-v1/;
            / {
                soc {
                    uart0: uart@9000000 {
                        compatible = "arm,pl011";
                        reg = < 0x0 0x9000000 0x0 0x1000 >;
                        current-speed = < 0x1c200 >;
                        status = "okay";
                    };
                };
            };
            """
            try:
                validator_namespace["validate_final_dts"](dtc_formatted_uart)
            except validator_namespace["ValidationError"] as error:
                errors.append(
                    "Zephyr polling validator rejects dtc-formatted reg cells: "
                    f"{error}"
                )
            inactive_symbols_omitted = ZEPHYR_CONFIG.read_text(
                encoding="utf-8"
            ).replace("CONFIG_UART_ASYNC_API=n\n", "").replace(
                "CONFIG_SHELL_BACKEND_SERIAL=n\n", ""
            )
            try:
                validator_namespace["validate_kconfig"](
                    inactive_symbols_omitted,
                    label="synthetic final .config",
                    allow_missing_disabled=True,
                )
            except validator_namespace["ValidationError"] as error:
                errors.append(
                    "Zephyr polling validator rejects dependency-hidden disabled "
                    f"symbols in final .config: {error}"
                )
            try:
                validator_namespace["validate_kconfig"](
                    inactive_symbols_omitted, label="synthetic source profile"
                )
            except validator_namespace["ValidationError"]:
                pass
            else:
                errors.append(
                    "Zephyr polling validator lets the source profile omit "
                    "fail-closed disabled settings"
                )

    design = read_text(DESIGN, label="console isolation design", errors=errors)
    require_fragments(
        design,
        (
            "qemu-cortex-a53-pl011-polling.overlay",
            "validate_zephyr_pl011_profile.py",
            "不代表 Zephyr 已重编译",
            "boot-console 交接",
        ),
        label="console isolation design",
        errors=errors,
    )

    ci = read_text(CI, label="CI workflow", errors=errors)
    if "python3 scripts/test/check_axvisor_pl011_guest_fdt.py" not in ci:
        errors.append("Guest FDT polling PL011 contract is not wired into CI")

    if not errors:
        return 0

    print("AxVisor polling PL011 Guest FDT contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
