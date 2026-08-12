#!/usr/bin/env python3

import sys
import tomllib
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
BASELINE_SCRIPT = WORKSPACE_ROOT / "scripts/contest/run_axvisor_baseline.sh"
HOST_CAPTURE_SCRIPT = WORKSPACE_ROOT / "scripts/contest/capture_host_environment.ps1"
BASELINE_CONFIG = WORKSPACE_ROOT / "configs/contest/qemu-aarch64-baseline.env"
LINUX_QEMU_CONFIG = (
    WORKSPACE_ROOT / "configs/contest/qemu-aarch64-linux-baseline.toml"
)
ARCEOS_QEMU_CONFIG = WORKSPACE_ROOT / "os/axvisor/.github/workflows/qemu-aarch64.toml"
ARCEOS_VM_TEMPLATE = (
    WORKSPACE_ROOT / "os/axvisor/configs/vms/qemu/aarch64/arceos-smp1.toml"
)
LINUX_VM_TEMPLATE = (
    WORKSPACE_ROOT / "os/axvisor/configs/vms/qemu/aarch64/linux-smp1.toml"
)
GITIGNORE = WORKSPACE_ROOT / ".gitignore"
ITS_NODE = ["/intc@8000000/its@8080000"]
UNSAFE_ITS_LPI_FAIL_REGEX = (
    r"(?i)ITS \[mem 0x[0-9a-f]+-0x[0-9a-f]+\]",
    r"(?i)GICv3: Expected reserved range .*not found",
    r"(?i)GICv3: CPU[0-9]+: Booted with LPIs enabled, memory probably corrupted",
)


def check_smp1_gic_template(path: Path, label: str, errors: list[str]) -> None:
    if not path.is_file():
        errors.append(f"{label} VM template is missing")
        return
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        device_config = config["devices"]
        devices = device_config["emu_devices"]
    except (KeyError, tomllib.TOMLDecodeError) as error:
        errors.append(f"{label} VM template is invalid: {error}")
        return

    expected = (
        ["gppt-gicd", 0x0800_0000, 0x1_0000, 0, 0x21, []],
        ["gppt-gicr", 0x080A_0000, 0x2_0000, 0, 0x20, [1, 0x2_0000, 0]],
    )
    for device in expected:
        if device not in devices:
            errors.append(
                f"{label} passthrough baseline lacks required GPPT GIC device {device[0]}"
            )
    for emu_type in (0x20, 0x21):
        if sum(device[4] == emu_type for device in devices) != 1:
            errors.append(
                f"{label} baseline must own exactly one emulated GIC type {emu_type:#x}"
            )
    excluded = device_config.get("excluded_devices", [])
    if excluded.count(ITS_NODE) != 1:
        errors.append(
            f"{label} passthrough root must exclude exactly one physical QEMU ITS node"
        )
    if any(device[4] == 0x22 for device in devices):
        errors.append(
            f"{label} baseline must not expose the current host-global GPPT GITS"
        )


def check_unsafe_log_fail_regex(
    config: dict[str, object], label: str, errors: list[str]
) -> None:
    fail_regex = config.get("fail_regex", [])
    for pattern in UNSAFE_ITS_LPI_FAIL_REGEX:
        if pattern not in fail_regex:
            errors.append(f"{label} QEMU smoke does not fail closed on `{pattern}`")


def main() -> int:
    script = BASELINE_SCRIPT.read_text(encoding="utf-8")
    errors = []

    if "cargo xtask axvisor qemu" in script:
        errors.append(
            "baseline script invokes the workspace CLI after changing to os/axvisor; "
            "use the local `cargo xtask qemu` alias"
        )
    if "cargo xtask qemu" not in script:
        errors.append("baseline script does not invoke the AxVisor-local QEMU command")
    if '[[ ! -e "$RUN_DIR" ]]' in script:
        errors.append(
            "baseline script rejects a run directory created by the Windows host capture"
        )
    if "RUN_ARTIFACTS=(" not in script:
        errors.append("baseline script does not guard its own evidence artifacts")
    if "git_repo() {" not in script or "git.exe" not in script:
        errors.append(
            "baseline script does not use Windows Git for a repository on a WSL mount"
        )
    direct_linux_git = [
        line
        for line in script.splitlines()
        if 'git -C "$REPO_ROOT"' in line and "command git -C" not in line
    ]
    if direct_linux_git:
        errors.append("baseline script still performs direct Linux Git scans on the WSL mount")
    expected_musl_bin = (
        'AARCH64_MUSL_TOOLCHAIN_BIN="${AXVISOR_AARCH64_MUSL_TOOLCHAIN_BIN:-'
        '/opt/aarch64-linux-musl-cross/bin}"'
    )
    if expected_musl_bin not in script:
        errors.append(
            "baseline script does not configure the repository-standard AArch64 musl toolchain"
        )
    if "aarch64-linux-musl-cc" not in script:
        errors.append(
            "baseline script does not fail fast when the AArch64 musl compiler is missing"
        )
    for dependency_probe in (
        "cargo-objcopy",
        "LIBCLANG_PATH_RESOLVED",
        "EFI_VIRTIO_ROM_PATH",
        "/usr/lib/ipxe/qemu/efi-virtio.rom",
    ):
        if dependency_probe not in script:
            errors.append(f"baseline preflight is missing `{dependency_probe}`")
    hard_timeout = "timeout --signal=INT --kill-after=30s --foreground"
    if script.count(hard_timeout) < 2:
        errors.append(
            "baseline QEMU commands are not protected by an INT plus hard-kill timeout"
        )
    unsafe_status = (
        'if grep -Eq -- "$UNSAFE_ITS_LPI_REGEX" "$guest_dir/qemu.log"; then'
    )
    guest_init_status = (
        "elif grep -Fq -- 'Failed to initialize guest VM' \"$guest_dir/qemu.log\"; then"
    )
    timeout_status = "elif (( qemu_exit == 124 || qemu_exit == 137 )); then"
    qemu_failure_status = "elif (( qemu_exit != 0 )); then"
    marker_status = "elif [[ \"$marker_seen\" != true ]]; then"
    if not all(
        value in script
        for value in (
            unsafe_status,
            guest_init_status,
            timeout_status,
            qemu_failure_status,
            marker_status,
        )
    ):
        errors.append(
            "baseline status classification does not distinguish unsafe ITS/LPI, "
            "timeout, QEMU, and marker failures"
        )
    elif not (
        script.index(unsafe_status)
        < script.index(guest_init_status)
        < script.index(timeout_status)
        < script.index(qemu_failure_status)
        < script.index(marker_status)
    ):
        errors.append("baseline status classification uses the wrong priority order")
    if "source-state" not in script or "worktree.patch" not in script:
        errors.append("baseline evidence does not capture the dirty tracked worktree diff")
    if "untracked-files.tar" not in script or "untracked-files.sha256" not in script:
        errors.append("baseline evidence does not archive and hash untracked source files")
    crlf_safe_index_header = (
        "INDEX_HEADER=\"$(sed -n '1p' \"$INDEX_PATH\" | tr -d '\\r')\""
    )
    if crlf_safe_index_header not in script:
        errors.append(
            "baseline index schema check is not safe for a CRLF index on a Windows mount"
        )
    persistent_cache = (
        'export AXVISOR_IMAGE_LOCAL_STORAGE="${AXVISOR_IMAGE_LOCAL_STORAGE:-'
        '${XDG_CACHE_HOME:-$HOME/.cache}/tgoskits/axvisor-images}"'
    )
    if persistent_cache not in script:
        errors.append("baseline script does not default to a persistent per-user image cache")

    baseline_config = BASELINE_CONFIG.read_text(encoding="utf-8")
    if 'LINUX_QEMU_CONFIG_REL="configs/contest/qemu-aarch64-linux-baseline.toml"' not in baseline_config:
        errors.append("baseline config does not select a Linux-specific QEMU smoke config")
    if 'QEMU_CONFIG_PATH="$REPO_ROOT/$LINUX_QEMU_CONFIG_REL"' not in script:
        errors.append("Linux baseline does not resolve its dedicated QEMU smoke config")

    check_smp1_gic_template(ARCEOS_VM_TEMPLATE, "ArceOS", errors)
    check_smp1_gic_template(LINUX_VM_TEMPLATE, "Linux", errors)

    if not LINUX_QEMU_CONFIG.is_file():
        errors.append("Linux baseline QEMU smoke config is missing")
    else:
        linux_qemu_config = LINUX_QEMU_CONFIG.read_text(encoding="utf-8")
        linux_qemu = tomllib.loads(linux_qemu_config)
        required_linux_smoke_settings = (
            'success_regex = ["(?m)^test pass!\\\\s*$"]',
            'shell_prefix = "~ #"',
            'shell_init_cmd = "pwd && echo \'test pass!\'"',
        )
        for setting in required_linux_smoke_settings:
            if setting not in linux_qemu_config:
                errors.append(f"Linux QEMU smoke config is missing `{setting}`")
        if "(?i)Failed to initialize guest VM" not in linux_qemu_config:
            errors.append("Linux QEMU smoke config does not fail fast on Guest init failure")
        check_unsafe_log_fail_regex(linux_qemu, "Linux", errors)

    if not ARCEOS_QEMU_CONFIG.is_file():
        errors.append("ArceOS QEMU smoke config is missing")
    else:
        arceos_qemu_text = ARCEOS_QEMU_CONFIG.read_text(encoding="utf-8")
        if "(?i)Failed to initialize guest VM" not in arceos_qemu_text:
            errors.append("ArceOS QEMU smoke config does not fail fast on Guest init failure")
        check_unsafe_log_fail_regex(
            tomllib.loads(arceos_qemu_text), "ArceOS", errors
        )

    host_capture = HOST_CAPTURE_SCRIPT.read_text(encoding="utf-8")
    for variable, class_name in (
        ("operatingSystems", "Win32_OperatingSystem"),
        ("processors", "Win32_Processor"),
        ("computerSystems", "Win32_ComputerSystem"),
    ):
        expected = f"${variable} = @(Get-CimValue -ClassName '{class_name}')"
        if expected not in host_capture:
            errors.append(
                f"host capture does not preserve `{class_name}` results as an array"
            )

    gitignore = GITIGNORE.read_text(encoding="utf-8")
    if "/results/baseline/runs/" not in gitignore:
        errors.append("raw baseline run bundles are not ignored by Git")

    if not errors:
        return 0

    print("Contest baseline contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
