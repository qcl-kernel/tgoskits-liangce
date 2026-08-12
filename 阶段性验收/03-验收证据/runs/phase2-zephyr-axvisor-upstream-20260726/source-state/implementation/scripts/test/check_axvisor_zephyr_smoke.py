#!/usr/bin/env python3
"""Static and behavioral contract for the contest Zephyr smoke path."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import struct
import sys
import tomllib
import uuid
from pathlib import Path
from types import ModuleType
from typing import Callable


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_VM_CONFIG = (
    WORKSPACE_ROOT / "os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1.toml"
)
ENV_CONFIG = WORKSPACE_ROOT / "configs/contest/qemu-aarch64-zephyr-smoke.env"
QEMU_CONFIG = WORKSPACE_ROOT / "configs/contest/qemu-aarch64-zephyr-smoke.toml"
AXVISOR_BUILD_CONFIG = (
    WORKSPACE_ROOT / "os/axvisor/configs/board/qemu-aarch64-zephyr-smoke.toml"
)
APP_ROOT = WORKSPACE_ROOT / "scripts/contest/zephyr-periodic-smoke"
GENERATOR = WORKSPACE_ROOT / "scripts/contest/generate_axvisor_zephyr_vmconfig.py"
VALIDATOR = WORKSPACE_ROOT / "scripts/contest/validate_zephyr_smoke_log.py"
RUNNER = WORKSPACE_ROOT / "scripts/contest/run_axvisor_zephyr_smoke.sh"
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"
CONTEST_README = WORKSPACE_ROOT / "scripts/contest/README.md"
CONFIG_README = WORKSPACE_ROOT / "configs/contest/README.md"
ARM_VCPU_SOURCE = WORKSPACE_ROOT / "virtualization/arm_vcpu/src/vcpu.rs"

ZEPHYR_REVISION = "684c9e8f32e4373a21098559f748f06915f950c9"
MANIFEST_SHA256 = "9c3661dd82e5ab7f487e3c0a4eee8726978736eadb470a3a09a76c77a8f10f92"
SDK_VERSION = "1.0.1"
BOARD = "qemu_cortex_a53"
SUCCESS_MARKER = "TGOS_ZEPHYR_SMOKE_PASS samples=10"


class WritableTempDirectory:
    """Workspace-local temporary directory without Windows' restrictive 0o700 ACL."""

    def __init__(self, parent: Path) -> None:
        self.path = parent / f"tgos-zephyr-generator-{uuid.uuid4().hex}"

    def __enter__(self) -> str:
        self.path.mkdir(parents=True)
        return str(self.path)

    def __exit__(self, *_args: object) -> None:
        shutil.rmtree(self.path)


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_elf(
    *,
    entry: int = 0x4000_1034,
    machine: int = 183,
    elf_class: int = 2,
    data_encoding: int = 1,
    ph_flags: int = 5,
    segment_vaddr: int = 0x4000_1000,
    segment_paddr: int = 0x4000_1000,
    segment_size: int = 0x100,
) -> bytes:
    ident = bytearray(b"\x7fELF" + bytes(12))
    ident[4] = elf_class
    ident[5] = data_encoding
    ident[6] = 1
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        bytes(ident),
        2,
        machine,
        1,
        entry,
        64,
        0,
        0,
        64,
        56,
        1,
        0,
        0,
        0,
    )
    phdr = struct.pack(
        "<IIQQQQQQ",
        1,
        ph_flags,
        0x100,
        segment_vaddr,
        segment_paddr,
        segment_size,
        segment_size,
        0x1000,
    )
    return header + phdr + bytes(0x100 - len(header) - len(phdr)) + bytes(segment_size)


def expect_rejected(
    errors: list[str],
    generate: Callable[..., object],
    root: Path,
    label: str,
    elf_data: bytes,
    *,
    binary_size: int = 0x3000,
) -> None:
    elf_path = root / f"{label}.elf"
    binary_path = root / f"{label}.bin"
    output_path = root / f"{label}.toml"
    provenance_path = root / f"{label}.json"
    elf_path.write_bytes(elf_data)
    binary_path.write_bytes(bytes(binary_size))
    try:
        generate(
            CANONICAL_VM_CONFIG,
            elf_path,
            binary_path,
            output_path,
            provenance_path,
        )
    except (OSError, ValueError):
        return
    errors.append(f"ELF generator accepted invalid case: {label}")


def check_generator(errors: list[str]) -> None:
    if not GENERATOR.is_file():
        errors.append("Zephyr VM config generator is missing")
        return
    try:
        generator = load_module("zephyr_vmconfig_generator", GENERATOR)
    except Exception as error:  # noqa: BLE001 - contract should report import failures.
        errors.append(f"Zephyr VM config generator cannot be imported: {error}")
        return

    generate = getattr(generator, "generate", None)
    if not callable(generate):
        errors.append("Zephyr VM config generator does not expose generate()")
        return

    canonical_before = hashlib.sha256(CANONICAL_VM_CONFIG.read_bytes()).hexdigest()
    temporary_root = WORKSPACE_ROOT / "tmp/contest/test"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with WritableTempDirectory(temporary_root) as temporary:
        root = Path(temporary)
        elf_path = root / "zephyr.elf"
        binary_path = root / "zephyr.bin"
        output_path = root / "zephyr.generated.toml"
        provenance_path = root / "zephyr.provenance.json"
        elf_path.write_bytes(make_elf())
        binary_path.write_bytes(bytes(0x3000))
        try:
            result = generate(
                CANONICAL_VM_CONFIG,
                elf_path,
                binary_path,
                output_path,
                provenance_path,
            )
        except Exception as error:  # noqa: BLE001 - preserve actionable contract output.
            errors.append(f"Zephyr VM config generator rejects valid ELF: {error}")
        else:
            generated = tomllib.loads(output_path.read_text(encoding="utf-8"))
            kernel = generated.get("kernel", {})
            if kernel.get("entry_point") != 0x4000_1034:
                errors.append("generated Zephyr VM config has the wrong physical entry")
            if kernel.get("kernel_path") != str(binary_path.resolve()):
                errors.append("generated Zephyr VM config does not use the absolute BIN path")
            if kernel.get("image_location") != "memory":
                errors.append("generated Zephyr VM config changed image_location")
            payload = json.loads(provenance_path.read_text(encoding="utf-8"))
            if payload.get("entryVirtual") != "0x0000000040001034":
                errors.append("Zephyr provenance omits the virtual ELF entry")
            if payload.get("entryPhysical") != "0x0000000040001034":
                errors.append("Zephyr provenance omits the converted physical entry")
            for key in ("templateSha256", "elfSha256", "binarySha256"):
                if not isinstance(payload.get(key), str) or len(payload[key]) != 64:
                    errors.append(f"Zephyr provenance has no valid {key}")
            if not isinstance(result, dict) or result.get("entry_physical") != 0x4000_1034:
                errors.append("generate() does not return the parsed physical entry")

        if hashlib.sha256(CANONICAL_VM_CONFIG.read_bytes()).hexdigest() != canonical_before:
            errors.append("Zephyr generator modified the canonical upstream template")
        if list(root.glob(".*.tmp-*")):
            errors.append("Zephyr generator left atomic-write temporary files behind")

        wrong_magic = bytearray(make_elf())
        wrong_magic[0] = 0
        expect_rejected(errors, generate, root, "wrong-magic", bytes(wrong_magic))

        expect_rejected(errors, generate, root, "wrong-class", make_elf(elf_class=1))
        expect_rejected(errors, generate, root, "wrong-endian", make_elf(data_encoding=2))
        expect_rejected(errors, generate, root, "wrong-machine", make_elf(machine=62))
        expect_rejected(errors, generate, root, "truncated-phdr", make_elf()[:80])
        expect_rejected(errors, generate, root, "no-exec-segment", make_elf(ph_flags=4))
        expect_rejected(
            errors,
            generate,
            root,
            "entry-outside-segment",
            make_elf(entry=0x4000_2000),
        )
        expect_rejected(
            errors,
            generate,
            root,
            "binary-window-overflow",
            make_elf(),
            binary_size=0x1000,
        )


def complete_log() -> str:
    lines = [
        "VM[1] created successfully",
        "Loading VM[1] kernel from /tmp/zephyr.bin",
        "Spawning task for VM[1] VCpu[0]",
        'VCpu task (8, "VM[1]-VCpu[0]") created cpumask: [0, ]',
        "VM[1] boot success!",
        "TGOS_ZEPHYR_SMOKE_START period_ms=100 samples=10",
    ]
    uptime = 1000
    for sequence in range(1, 11):
        uptime += 100
        lines.append(
            "TGOS_ZEPHYR_SMOKE_SAMPLE "
            f"seq={sequence} uptime_ms={uptime} delta_ms=100"
        )
    lines.append(SUCCESS_MARKER)
    return "\n".join(lines)


def check_validator(errors: list[str]) -> None:
    if not VALIDATOR.is_file():
        errors.append("Zephyr log validator is missing")
        return
    try:
        validator = load_module("zephyr_smoke_validator", VALIDATOR)
    except Exception as error:  # noqa: BLE001
        errors.append(f"Zephyr log validator cannot be imported: {error}")
        return

    evidence = validator.inspect_log(complete_log())
    if validator.classify(evidence, 0) != "passed":
        errors.append("Zephyr validator rejects a complete AxVisor periodic proof")

    missing_sample = validator.inspect_log(
        complete_log().replace(
            "TGOS_ZEPHYR_SMOKE_SAMPLE seq=5 uptime_ms=1500 delta_ms=100\n", ""
        )
    )
    if validator.classify(missing_sample, 0) != "sample_sequence_invalid":
        errors.append("Zephyr validator does not reject missing sample sequence 5")

    bad_delta = validator.inspect_log(
        complete_log().replace("seq=7 uptime_ms=1700 delta_ms=100", "seq=7 uptime_ms=1700 delta_ms=900")
    )
    if validator.classify(bad_delta, 0) != "sample_delta_out_of_range":
        errors.append("Zephyr validator does not reject an out-of-range sample delta")

    missing_affinity = validator.inspect_log(
        complete_log().replace(
            'VCpu task (8, "VM[1]-VCpu[0]") created cpumask: [0, ]', ""
        )
    )
    if validator.classify(missing_affinity, 0) != "axvisor_vcpu0_affinity_missing":
        errors.append("Zephyr validator does not identify missing vCPU0 affinity")

    if validator.classify(evidence, 124) != "timed_out":
        errors.append("Zephyr validator does not prioritize an AxVisor timeout")
    if validator.classify(evidence, 7) != "qemu_failed":
        errors.append("Zephyr validator does not report a nonzero QEMU exit")
    init_failure = validator.inspect_log(
        "Failed to initialize guest VM: prepare devices and vCPUs\n"
    )
    if validator.classify(init_failure, 124) != "guest_init_failed":
        errors.append("Zephyr guest-init failure does not outrank a later timeout")


def check_passthrough_gic_cpu_interface(errors: list[str]) -> None:
    """Guard the host/guest EOImode switch that keeps PPIs repeatable."""

    if not ARM_VCPU_SOURCE.is_file():
        errors.append("AArch64 vCPU source is missing")
        return

    source = ARM_VCPU_SOURCE.read_text(encoding="utf-8")
    required = (
        "const ICC_CTLR_EL1_EOIMODE: u64 = 1 << 1;",
        "struct GicCpuInterfaceState",
        "ctlr_el1: u64",
        "pmr_el1: u64",
        "igrpen1_el1: u64",
        "guest_gic_cpu_interface: Option<GicCpuInterfaceState>",
        "passthrough_interrupt: bool",
        "fn passthrough_guest_icc_ctlr(host: u64) -> u64",
        "host & !ICC_CTLR_EL1_EOIMODE",
        "fn gic_system_register_interface_available() -> bool",
        "ID_AA64PFR0_EL1.get()",
        "ICC_SRE_EL2",
        "fn enter_guest_interrupt_context(&mut self) -> Option<GicCpuInterfaceState>",
        "host_gic_cpu_interface: Option<GicCpuInterfaceState>",
        "self.guest_gic_cpu_interface = Some(guest_gic_cpu_interface);",
    )
    for snippet in required:
        if snippet not in source:
            errors.append(f"passthrough GIC CPU-interface fix is missing `{snippet}`")

    run_start = source.find("pub fn run(&mut self)")
    run_end = source.find("/// Binds this vCPU", run_start)
    run_body = source[run_start:run_end] if run_start >= 0 and run_end >= 0 else ""
    expected_order = (
        "msr daifset",
        "self.restore_vm_system_regs()",
        "self.enter_guest_interrupt_context()",
        "self.run_guest()",
        "self.leave_guest_interrupt_context(host_gic_cpu_interface)",
        "self.vmexit_handler(trap_kind)",
        "msr daifclr",
    )
    positions = [run_body.find(snippet) for snippet in expected_order]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        errors.append(
            "ArmVcpu::run must switch the passthrough guest ICC context around "
            "run_guest and restore the host before VM-exit handling/IRQ unmask"
        )

    leave_start = source.find("fn leave_guest_interrupt_context")
    leave_end = source.find("fn init_hv", leave_start)
    leave_body = source[leave_start:leave_end]
    if "passthrough_guest_icc_ctlr(guest" in leave_body:
        errors.append(
            "passthrough exit must preserve an explicit guest EOImode after the "
            "one-step default was established on first entry"
        )


def main() -> int:
    errors: list[str] = []

    canonical = tomllib.loads(CANONICAL_VM_CONFIG.read_text(encoding="utf-8"))
    if canonical["base"]["id"] != 1 or canonical["base"]["phys_cpu_ids"] != [0]:
        errors.append("canonical Zephyr single-guest VM identity/CPU mapping changed")
    if canonical["kernel"]["kernel_path"] != "/path/to/zephyr.bin":
        errors.append("canonical Zephyr template no longer exposes its placeholder path")
    if canonical["devices"].get("interrupt_mode") != "passthrough":
        errors.append("canonical Zephyr guest must retain passthrough interrupt mode")
    expected_gppt_gic = [
        ["gppt-gicd", 0x0800_0000, 0x1_0000, 0, 0x21, []],
        ["gppt-gicr", 0x080A_0000, 0x2_0000, 0, 0x20, [1, 0x2_0000, 0]],
    ]
    if canonical["devices"].get("emu_devices") != expected_gppt_gic:
        errors.append(
            "canonical Zephyr guest must provide the VGicD/VGicR required for "
            "passthrough SPI ownership"
        )
    if canonical["devices"].get("passthrough_devices") != [["/"]]:
        errors.append("canonical Zephyr guest root passthrough contract changed")

    if not ENV_CONFIG.is_file():
        errors.append("Zephyr smoke environment contract is missing")
    else:
        env = ENV_CONFIG.read_text(encoding="utf-8")
        expected = (
            f'ZEPHYR_REVISION="{ZEPHYR_REVISION}"',
            f'ZEPHYR_MANIFEST_SHA256="{MANIFEST_SHA256}"',
            f'ZEPHYR_SDK_VERSION="{SDK_VERSION}"',
            'ZEPHYR_TOOLCHAIN_NAME="aarch64-zephyr-elf"',
            f'ZEPHYR_BOARD="{BOARD}"',
            'ZEPHYR_APP_REL="scripts/contest/zephyr-periodic-smoke"',
            'ZEPHYR_VM_TEMPLATE_REL="os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1.toml"',
            'ZEPHYR_QEMU_CONFIG_REL="configs/contest/qemu-aarch64-zephyr-smoke.toml"',
            f'ZEPHYR_SUCCESS_MARKER="{SUCCESS_MARKER}"',
            'AXVISOR_BUILD_CONFIG_REL="configs/board/qemu-aarch64-zephyr-smoke.toml"',
        )
        for snippet in expected:
            if snippet not in env:
                errors.append(f"Zephyr env is missing `{snippet}`")
        if "PLACEHOLDER" in env or "TODO" in env:
            errors.append("Zephyr env still contains an unresolved placeholder")

    if not AXVISOR_BUILD_CONFIG.is_file():
        errors.append("Zephyr-specific AxVisor build config is missing")
    else:
        axvisor_build = tomllib.loads(
            AXVISOR_BUILD_CONFIG.read_text(encoding="utf-8")
        )
        if axvisor_build.get("features") != []:
            errors.append(
                "Zephyr-specific AxVisor build must not enable fs or block drivers"
            )
        if axvisor_build.get("target") != "aarch64-unknown-none-softfloat":
            errors.append("Zephyr-specific AxVisor build target is not AArch64")
        if axvisor_build.get("vm_configs") != []:
            errors.append("Zephyr-specific AxVisor build must receive VM configs at runtime")

    required_app_files = (APP_ROOT / "CMakeLists.txt", APP_ROOT / "prj.conf", APP_ROOT / "src/main.c")
    for path in required_app_files:
        if not path.is_file():
            errors.append(f"Zephyr periodic app file is missing: {path.relative_to(WORKSPACE_ROOT)}")
    if (APP_ROOT / "src/main.c").is_file():
        app = (APP_ROOT / "src/main.c").read_text(encoding="utf-8")
        for snippet in (
            "#define PERIOD_MS 100",
            "#define SAMPLE_COUNT 10",
            "k_msleep(PERIOD_MS)",
            "TGOS_ZEPHYR_SMOKE_START",
            "TGOS_ZEPHYR_SMOKE_SAMPLE",
            SUCCESS_MARKER,
        ):
            if snippet not in app:
                errors.append(f"Zephyr periodic app is missing `{snippet}`")
    if (APP_ROOT / "prj.conf").is_file():
        prj_conf = (APP_ROOT / "prj.conf").read_text(encoding="utf-8")
        if "CONFIG_ARMV8_A_NS=y" not in prj_conf.splitlines():
            errors.append("Zephyr periodic app is not locked to Non-secure EL1")

    if not QEMU_CONFIG.is_file():
        errors.append("Zephyr AxVisor QEMU config is missing")
    else:
        qemu = tomllib.loads(QEMU_CONFIG.read_text(encoding="utf-8"))
        args = qemu.get("args", [])
        joined_args = " ".join(args)
        if args.count("-smp") != 1 or args[args.index("-smp") + 1] != "4":
            errors.append("Zephyr QEMU outer machine does not expose four pCPUs")
        if "virtio-blk" in joined_args or "-append" in args or "-drive" in args:
            errors.append("Zephyr QEMU config must not attach the baseline Linux disk")
        if qemu.get("success_regex") != [
            r"(?m)^TGOS_ZEPHYR_SMOKE_PASS samples=10\r?$"
        ]:
            errors.append("Zephyr QEMU success regex is not a strict complete line")
        if "(?i)Failed to initialize guest VM" not in qemu.get("fail_regex", []):
            errors.append(
                "Zephyr QEMU smoke does not stop on an explicit guest-init failure"
            )

    if not RUNNER.is_file():
        errors.append("Zephyr AxVisor smoke runner is missing")
    else:
        runner = RUNNER.read_text(encoding="utf-8")
        for snippet in (
            "--mode <native|axvisor|all>",
            "west manifest --freeze --active-only",
            "west build -p always",
            "generate_axvisor_zephyr_vmconfig.py",
            "validate_zephyr_smoke_log.py",
            "--rootfs",
            "checksums.sha256",
            "manifest-frozen.yml",
            "snapshot_paths=(",
            "implementation-files.sha256",
            "relevant-tracked.patch",
            'git_repo status --short --branch -- "${snapshot_paths[@]}"',
            "configs/axvisor-build.toml",
            "configs/zephyr-dotconfig",
            'grep -Fxq "CONFIG_ARMV8_A_NS=y" "$BUILD_DIR/zephyr/.config"',
            "secure_native_machine_detected",
        ):
            if snippet not in runner:
                errors.append(f"Zephyr runner is missing `{snippet}`")
        for forbidden in ("git clone", "west init", "west update", "pip install", "curl ", "wget "):
            if forbidden in runner:
                errors.append(f"Zephyr runner performs forbidden implicit network setup: {forbidden}")
        if "git_repo status --short --branch >" in runner:
            errors.append("Zephyr runner captures an unbounded full-worktree status")
        if "git_repo diff --binary --no-ext-diff HEAD >" in runner:
            errors.append("Zephyr runner captures an unbounded full-worktree diff")
        if (
            'timeout --signal=TERM --kill-after=5s "$NATIVE_TIMEOUT_SECONDS"'
            not in runner
        ):
            errors.append("Zephyr native timeout command is missing")
        native_timeout_block = runner.split("run_native() {", maxsplit=1)[-1].split(
            "verify_provenance() {", maxsplit=1
        )[0]
        if "--foreground" in native_timeout_block:
            errors.append("Zephyr native timeout must kill the complete QEMU process group")
        axvisor_block = runner.split("run_axvisor() {", maxsplit=1)[-1].split(
            "overall_exit=", maxsplit=1
        )[0]
        if "--foreground" in axvisor_block:
            errors.append("Zephyr AxVisor timeout must kill the complete QEMU process group")
        if (
            'timeout --signal=INT --kill-after=30s "$AXVISOR_TIMEOUT_SECONDS"'
            not in axvisor_block
        ):
            errors.append("Zephyr AxVisor timeout command is missing")
        for snippet in (
            "normalized_exit=$AXVISOR_EXIT",
            "AXVISOR_STATUS=vmconfig_generation_failed",
            "AXVISOR_STATUS=provenance_verification_failed",
        ):
            if snippet not in axvisor_block:
                errors.append(f"Zephyr AxVisor runner is missing `{snippet}`")

    check_generator(errors)
    check_validator(errors)
    check_passthrough_gic_cpu_interface(errors)

    ci = CI.read_text(encoding="utf-8")
    if "Validate AxVisor Zephyr smoke configuration" not in ci:
        errors.append("CI does not run the Zephyr smoke contract")
    if "python3 scripts/test/check_axvisor_zephyr_smoke.py" not in ci:
        errors.append("CI Zephyr smoke step does not call the contract checker")

    for readme in (CONTEST_README, CONFIG_README):
        if "run_axvisor_zephyr_smoke.sh" not in readme.read_text(encoding="utf-8"):
            errors.append(f"{readme.relative_to(WORKSPACE_ROOT)} omits Zephyr smoke usage")

    if not errors:
        return 0

    print("AxVisor Zephyr smoke contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
