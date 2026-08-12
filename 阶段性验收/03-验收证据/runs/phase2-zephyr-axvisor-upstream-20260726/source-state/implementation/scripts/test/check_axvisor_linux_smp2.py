#!/usr/bin/env python3

import copy
import importlib.util
import sys
import tomllib
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SMP1_CONFIG = (
    WORKSPACE_ROOT / "os/axvisor/configs/vms/qemu/aarch64/linux-smp1.toml"
)
SMP2_CONFIG = (
    WORKSPACE_ROOT / "os/axvisor/configs/vms/qemu/aarch64/linux-smp2.toml"
)
SETUP_SCRIPT = WORKSPACE_ROOT / "os/axvisor/scripts/setup_qemu.sh"
BASELINE_ENV = WORKSPACE_ROOT / "configs/contest/qemu-aarch64-baseline.env"
BASELINE_RUNNER = WORKSPACE_ROOT / "scripts/contest/run_axvisor_baseline.sh"
LINUX_QEMU_CONFIG = (
    WORKSPACE_ROOT / "configs/contest/qemu-aarch64-linux-baseline.toml"
)
LINUX_SMP2_QEMU_CONFIG = (
    WORKSPACE_ROOT / "configs/contest/qemu-aarch64-linux-smp2.toml"
)
LOG_VALIDATOR = WORKSPACE_ROOT / "scripts/contest/validate_linux_smp2_log.py"


EXPECTED_SHELL_COMMAND = (
    "marker=linux-smp2; if [ ! -r /proc/cpuinfo ]; then "
    "/bin/busybox mount -t proc proc /proc || { "
    "printf '%s-proc-mount-failed\\n' \"$marker\"; exit 1; }; fi; "
    "cpu_count=0; "
    'while IFS=: read -r key value; do case "$key" in processor*) '
    "cpu_count=$((cpu_count + 1));; esac; done < /proc/cpuinfo; "
    'if [ "$cpu_count" -eq 2 ]; then printf \'%s-pass\\n\' "$marker"; '
    "else printf '%s-cpu-count=%s\\n' \"$marker\" \"$cpu_count\"; fi"
)


def main() -> int:
    errors = []

    smp1 = tomllib.loads(SMP1_CONFIG.read_text(encoding="utf-8"))
    if smp1["base"]["cpu_num"] != 1 or smp1["base"]["phys_cpu_ids"] != [0]:
        errors.append("canonical linux-smp1 CPU mapping was modified")

    if not SMP2_CONFIG.is_file():
        errors.append("QEMU AArch64 linux-smp2 VM configuration is missing")
    else:
        smp2_text = SMP2_CONFIG.read_text(encoding="utf-8")
        smp2 = tomllib.loads(smp2_text)
        if smp2["base"]["id"] != 1:
            errors.append("linux-smp2 must remain VM id 1 for the single-guest baseline")
        if smp2["base"]["name"] != "linux-qemu-smp2":
            errors.append("linux-smp2 does not use a distinct reviewable VM name")
        if smp2["base"]["cpu_num"] != 2:
            errors.append("linux-smp2 does not declare two virtual CPUs")
        if smp2["base"]["phys_cpu_ids"] != [0, 1]:
            errors.append("linux-smp2 is not pinned to physical CPUs 0 and 1")
        expected_smp2 = copy.deepcopy(smp1)
        expected_smp2["base"].update(
            {
                "name": "linux-qemu-smp2",
                "cpu_num": 2,
                "phys_cpu_ids": [0, 1],
            }
        )
        expected_smp2["devices"]["emu_devices"] = [
            ["gppt-gicd", 0x0800_0000, 0x1_0000, 0, 0x21, []],
            ["gppt-gicr", 0x080A_0000, 0x2_0000, 0, 0x20, [2, 0x2_0000, 0]],
        ]
        if smp2 != expected_smp2:
            errors.append(
                "linux-smp2 must differ from linux-smp1 only in name, CPU mapping, "
                "and its required two-vCPU GPPT GIC"
            )
        if "dtb_path" in smp2["kernel"]:
            errors.append("linux-smp2 must keep AxVisor dynamic guest DTB generation")
        memory_regions = smp2["kernel"]["memory_regions"]
        if memory_regions != [[0x8000_0000, 0x1000_0000, 0x7, 1]]:
            errors.append("linux-smp2 changed the validated single-guest RAM mapping")
        if "System RAM 256 MiB" not in smp2_text:
            errors.append("linux-smp2 RAM comment does not match its 256 MiB size")

    setup = SETUP_SCRIPT.read_text(encoding="utf-8")
    required_setup_snippets = (
        "arceos|arceos-riscv64|linux|linux-smp2|linux-x86_64",
        "linux-smp2     - aarch64 Linux guest with two pinned vCPUs",
        'linux-smp2)     CFG="qemu_aarch64_linux|qemu/aarch64/linux-smp2.toml|linux-aarch64-qemu-smp2.toml|qemu-aarch64.toml|.github/workflows/qemu-aarch64.toml|qemu-aarch64|linux-smp2-pass"',
    )
    for snippet in required_setup_snippets:
        if snippet not in setup:
            errors.append(f"setup_qemu.sh is missing linux-smp2 contract `{snippet}`")

    if not LINUX_SMP2_QEMU_CONFIG.is_file():
        errors.append("Linux SMP2 QEMU smoke configuration is missing")
    else:
        baseline_qemu = tomllib.loads(LINUX_QEMU_CONFIG.read_text(encoding="utf-8"))
        smp2_qemu = tomllib.loads(
            LINUX_SMP2_QEMU_CONFIG.read_text(encoding="utf-8")
        )
        for field in ("args", "shell_prefix", "to_bin", "uefi"):
            if smp2_qemu.get(field) != baseline_qemu.get(field):
                errors.append(f"Linux SMP2 QEMU smoke changed baseline field `{field}`")
        if smp2_qemu.get("success_regex") != [r"(?m)^linux-smp2-pass\s*$"]:
            errors.append("Linux SMP2 success regex is not line-anchored")
        if "(?i)Failed to initialize guest VM" not in smp2_qemu.get(
            "fail_regex", []
        ):
            errors.append(
                "Linux SMP2 QEMU smoke does not stop on an explicit guest-init failure"
            )
        if smp2_qemu.get("shell_init_cmd") != EXPECTED_SHELL_COMMAND:
            errors.append("Linux SMP2 shell command does not prove exactly two CPUs")
        else:
            forbidden_commands = ("grep", "wc", "awk", "sed", "cat", "nproc", "getconf")
            command = smp2_qemu["shell_init_cmd"]
            for forbidden in forbidden_commands:
                if forbidden in command.split():
                    errors.append(
                        f"Linux SMP2 shell proof depends on external command `{forbidden}`"
                    )
            if "linux-smp2-pass" in command:
                errors.append("Linux SMP2 command echo contains the complete success marker")
            if "/bin/busybox mount -t proc proc /proc" not in command:
                errors.append("Linux SMP2 proof does not mount procfs in the minimal rootfs")

    baseline_env = BASELINE_ENV.read_text(encoding="utf-8")
    required_env_snippets = (
        'LINUX_SMP2_QEMU_CONFIG_REL="configs/contest/qemu-aarch64-linux-smp2.toml"',
        'LINUX_SMP2_SETUP_GUEST="linux-smp2"',
        'LINUX_SMP2_VMCONFIG_REL="tmp/vmconfigs/linux-aarch64-qemu-smp2.generated.toml"',
        'LINUX_SMP2_SUCCESS_MARKER="linux-smp2-pass"',
    )
    for snippet in required_env_snippets:
        if snippet not in baseline_env:
            errors.append(f"baseline env is missing Linux SMP2 contract `{snippet}`")

    runner = BASELINE_RUNNER.read_text(encoding="utf-8")
    required_runner_snippets = (
        "<arceos|linux|linux-smp2|all>",
        "arceos|linux|linux-smp2|all)",
        "linux-smp2)",
        "validate_linux_smp2_log.py",
        "startup-evidence.json",
    )
    for snippet in required_runner_snippets:
        if snippet not in runner:
            errors.append(f"baseline runner is missing Linux SMP2 contract `{snippet}`")
    if "grep -Eq '^linux-smp2-pass[[:space:]]*$'" not in runner:
        errors.append("baseline runner does not detect the SMP2 marker with POSIX whitespace")

    if not LOG_VALIDATOR.is_file():
        errors.append("Linux SMP2 log validator is missing")
    else:
        spec = importlib.util.spec_from_file_location("linux_smp2_log", LOG_VALIDATOR)
        if spec is None or spec.loader is None:
            errors.append("Linux SMP2 log validator cannot be imported")
        else:
            validator = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(validator)
            complete_log = "\n".join(
                (
                    "Found 4 host CPU nodes",
                    "VM[1] DTB not found, generating from the VM configuration",
                    "Spawning task for VM[1] VCpu[0]",
                    'VCpu task (9, "VM[1]-VCpu[0]") created cpumask: [0, ]',
                    "VM[1]'s VCpu[0] try to boot target_cpu [1] entry_point=0 arg=0x0",
                    "Spawning task for VM[1] VCpu[1]",
                    'VCpu task (10, "VM[1]-VCpu[1]") created cpumask: [1, ]',
                    "GICv3: CPU1: found redistributor 1 region 0:0x00000000080c0000",
                    "CPU1: Booted secondary processor 0x0000000001 [0x410fd083]",
                    "smp: Brought up 1 node, 2 CPUs",
                    "SMP: Total of 2 processors activated.",
                    "linux-smp2-pass",
                )
            )
            complete = validator.inspect_log(complete_log)
            if validator.classify(complete, 0) != "passed":
                errors.append("Linux SMP2 log validator rejects a complete boot proof")
            missing_affinity = validator.inspect_log(
                complete_log.replace(
                    'VCpu task (10, "VM[1]-VCpu[1]") created cpumask: [1, ]', ""
                )
            )
            if validator.classify(missing_affinity, 0) != "axvisor_vcpu1_affinity_missing":
                errors.append("Linux SMP2 validator does not identify missing vCPU1 affinity")
            mismatch = validator.inspect_log("linux-smp2-cpu-count=1\n")
            if validator.classify(mismatch, 1) != "guest_cpu_count_mismatch":
                errors.append("Linux SMP2 CPU-count mismatch does not outrank QEMU failure")
            mount_failure = validator.inspect_log("linux-smp2-proc-mount-failed\n")
            if validator.classify(mount_failure, 1) != "guest_proc_mount_failed":
                errors.append("Linux SMP2 procfs mount failure is not classified explicitly")
            init_failure = validator.inspect_log(
                "Failed to initialize guest VM: prepare devices and vCPUs\n"
            )
            if validator.classify(init_failure, 124) != "guest_init_failed":
                errors.append(
                    "Linux SMP2 guest-init failure does not outrank a later timeout"
                )

    if not errors:
        return 0

    print("AxVisor Linux SMP2 contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
