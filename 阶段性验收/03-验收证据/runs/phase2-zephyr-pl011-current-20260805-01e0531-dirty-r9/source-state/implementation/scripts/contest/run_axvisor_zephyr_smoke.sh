#!/usr/bin/env bash
# Build and validate the locked Zephyr periodic task natively and under AxVisor.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  run_axvisor_zephyr_smoke.sh --mode <native|axvisor|all> [options]

Options:
  --mode <name>  Run native QEMU, AxVisor, or both in that order. Required.
  --run-id <id>  Evidence directory name. Defaults to UTC time + Git SHA.
  --dry-run       Validate static inputs and print the planned commands only.
  -h, --help      Show this help.

Required environment:
  ZEPHYR_BASE                Zephyr source directory at the locked revision.
  ZEPHYR_SDK_INSTALL_DIR     Zephyr SDK directory at the locked version.

Optional environment:
  WEST_BIN                   west executable; defaults to `west` on PATH.
  ZEPHYR_BUILD_DIR           external build directory; defaults below repo tmp/.

The polling PL011 overlay/config are always applied.  AxVisor runs detect a
configured emulated console automatically: framed consoles are demultiplexed
and validated from the reconstructed guest log; configurations without one
retain the legacy raw-log validation path.
EOF
}

log() {
    printf '[contest-zephyr] %s\n' "$*" >&2
}

die() {
    log "ERROR: $*"
    exit 2
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

print_command() {
    printf '  '
    printf '%q ' "$@"
    printf '\n'
}

validate_relative_path() {
    local name="$1"
    local value="$2"

    [[ -n "$value" ]] || die "$name must not be empty"
    [[ "$value" != /* ]] || die "$name must be repository-relative: $value"
    [[ "/$value/" != *'/../'* ]] || die "$name must not contain '..': $value"
}

sha256_file() {
    sha256sum "$1" | awk '{print $1}'
}

json_value() {
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]])' "$1" "$2"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
CONFIG_PATH="$REPO_ROOT/configs/contest/qemu-aarch64-zephyr-smoke.env"

git_repo() {
    if [[ -n "${WSL_DISTRO_NAME:-}" ]] \
        && command -v git.exe >/dev/null 2>&1 \
        && command -v wslpath >/dev/null 2>&1; then
        local windows_repo
        windows_repo="$(wslpath -w "$REPO_ROOT")"
        git.exe -C "$windows_repo" "$@" | tr -d '\r'
    else
        command git -C "$REPO_ROOT" "$@"
    fi
}

MODE=""
RUN_ID=""
DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            [[ $# -ge 2 ]] || die '--mode requires a value'
            MODE="$2"
            shift 2
            ;;
        --run-id)
            [[ $# -ge 2 ]] || die '--run-id requires a value'
            RUN_ID="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

case "$MODE" in
    native|axvisor|all) ;;
    '') die '--mode is required' ;;
    *) die "unsupported mode: $MODE" ;;
esac

[[ -f "$CONFIG_PATH" ]] || die "configuration not found: $CONFIG_PATH"
# shellcheck source=../../configs/contest/qemu-aarch64-zephyr-smoke.env
source "$CONFIG_PATH"

required_variables=(
    ZEPHYR_REVISION
    ZEPHYR_MANIFEST_SHA256
    ZEPHYR_SDK_VERSION
    ZEPHYR_TOOLCHAIN_NAME
    ZEPHYR_WEST_VERSION
    ZEPHYR_BOARD
    ZEPHYR_APP_REL
    ZEPHYR_BUILD_ROOT_REL
    ZEPHYR_VM_TEMPLATE_REL
    ZEPHYR_VMCONFIG_REL
    ZEPHYR_PROVENANCE_REL
    ZEPHYR_QEMU_CONFIG_REL
    ZEPHYR_SUCCESS_MARKER
    AXVISOR_DIR_REL
    AXVISOR_BUILD_CONFIG_REL
    RESULTS_ROOT_REL
    NATIVE_TIMEOUT_SECONDS
    AXVISOR_TIMEOUT_SECONDS
)
for variable in "${required_variables[@]}"; do
    [[ -n "${!variable:-}" ]] || die "missing configuration value: $variable"
done

for path_variable in \
    ZEPHYR_APP_REL ZEPHYR_BUILD_ROOT_REL ZEPHYR_VM_TEMPLATE_REL \
    ZEPHYR_VMCONFIG_REL ZEPHYR_PROVENANCE_REL ZEPHYR_QEMU_CONFIG_REL \
    AXVISOR_DIR_REL AXVISOR_BUILD_CONFIG_REL RESULTS_ROOT_REL; do
    validate_relative_path "$path_variable" "${!path_variable}"
done
[[ "$ZEPHYR_REVISION" =~ ^[0-9a-f]{40}$ ]] \
    || die 'ZEPHYR_REVISION must be a full lowercase Git SHA'
[[ "$ZEPHYR_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]] \
    || die 'ZEPHYR_MANIFEST_SHA256 must be a lowercase SHA-256'
[[ "$NATIVE_TIMEOUT_SECONDS" =~ ^[1-9][0-9]{0,3}$ ]] \
    || die 'NATIVE_TIMEOUT_SECONDS must be an integer from 1 to 9999'
[[ "$AXVISOR_TIMEOUT_SECONDS" =~ ^[1-9][0-9]{0,3}$ ]] \
    || die 'AXVISOR_TIMEOUT_SECONDS must be an integer from 1 to 9999'

APP_DIR="$REPO_ROOT/$ZEPHYR_APP_REL"
BUILD_ROOT="$REPO_ROOT/$ZEPHYR_BUILD_ROOT_REL"
BUILD_DIR="${ZEPHYR_BUILD_DIR:-$BUILD_ROOT/build}"
VM_TEMPLATE="$REPO_ROOT/$ZEPHYR_VM_TEMPLATE_REL"
VMCONFIG_PATH="$REPO_ROOT/$ZEPHYR_VMCONFIG_REL"
PROVENANCE_PATH="$REPO_ROOT/$ZEPHYR_PROVENANCE_REL"
QEMU_CONFIG_PATH="$REPO_ROOT/$ZEPHYR_QEMU_CONFIG_REL"
AXVISOR_DIR="$REPO_ROOT/$AXVISOR_DIR_REL"
RESULTS_ROOT="$REPO_ROOT/$RESULTS_ROOT_REL"
GENERATOR="$REPO_ROOT/scripts/contest/generate_axvisor_zephyr_vmconfig.py"
VALIDATOR="$REPO_ROOT/scripts/contest/validate_zephyr_smoke_log.py"
PROFILE_VALIDATOR="$REPO_ROOT/scripts/contest/validate_zephyr_pl011_profile.py"
CONSOLE_DEMUXER="$REPO_ROOT/scripts/contest/demux_guest_console_frames.py"
PL011_OVERLAY="$REPO_ROOT/configs/contest/zephyr/qemu-cortex-a53-pl011-polling.overlay"
PL011_CONF="$REPO_ROOT/configs/contest/zephyr/qemu-cortex-a53-pl011-polling.conf"
CONSOLE_FAIL_REGEX='Guest console evidence failed closed'
UNSAFE_ITS_LPI_REGEX='ITS \[mem 0x[0-9a-fA-F]+-0x[0-9a-fA-F]+\]|GICv3: Expected reserved range .*not found|GICv3: CPU[0-9]+: Booted with LPIs enabled, memory probably corrupted'
WEST_BIN="${WEST_BIN:-west}"

console_identity_from_vmconfig() {
    python3 -c '
import sys
import tomllib
import re

with open(sys.argv[1], "rb") as source:
    config = tomllib.load(source)
base = config.get("base", {})
devices = config.get("devices", {}).get("emu_devices", [])
consoles = [
    device for device in devices
    if isinstance(device, list) and len(device) >= 5 and device[4] == 2
]
if not consoles:
    raise SystemExit(0)
if len(consoles) != 1:
    raise SystemExit("generated VM config must contain at most one console")
vm_id = base.get("id")
name = consoles[0][0]
if (
    isinstance(vm_id, bool)
    or not isinstance(vm_id, int)
    or not 1 <= vm_id <= 65535
):
    raise SystemExit("generated VM config has an invalid VM id")
if not isinstance(name, str) or re.fullmatch(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name
) is None:
    raise SystemExit("generated VM config has an invalid console name")
print(f"{vm_id}:{name}")
' "$1"
}

[[ "$RESULTS_ROOT" == "$REPO_ROOT/results/baseline/runs" ]] \
    || die 'RESULTS_ROOT_REL must remain results/baseline/runs'
for required_file in \
    "$APP_DIR/CMakeLists.txt" "$APP_DIR/prj.conf" "$APP_DIR/src/main.c" \
    "$VM_TEMPLATE" "$QEMU_CONFIG_PATH" "$GENERATOR" "$VALIDATOR" \
    "$PROFILE_VALIDATOR" "$CONSOLE_DEMUXER" "$PL011_OVERLAY" "$PL011_CONF" \
    "$AXVISOR_DIR/$AXVISOR_BUILD_CONFIG_REL"; do
    [[ -f "$required_file" ]] || die "required file not found: $required_file"
done

if [[ -z "$RUN_ID" ]]; then
    SHORT_SHA="$(git_repo rev-parse --short=12 HEAD 2>/dev/null || printf unknown)"
    RUN_ID="phase2-zephyr-smoke-$(date -u +%Y%m%dT%H%M%SZ)-$SHORT_SHA"
fi
[[ "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] \
    || die "invalid run ID: $RUN_ID"
RUN_DIR="$RESULTS_ROOT/$RUN_ID"

# These literal command contracts are checked without provisioning a toolchain in CI:
#   west manifest --freeze --active-only
#   west build -p always
if (( DRY_RUN )); then
    command -v python3 >/dev/null 2>&1 || die 'required command not found: python3'
    dry_console_identity="$(console_identity_from_vmconfig "$VM_TEMPLATE")" \
        || die 'VM template has an invalid console identity'
    log 'dry-run: no directories will be created and no build/QEMU command will run'
    log "repository: $REPO_ROOT"
    log "mode: $MODE"
    log "output: $RUN_DIR"
    print_command "$WEST_BIN" manifest --freeze --active-only
    print_command python3 "$PROFILE_VALIDATOR" \
        --overlay "$PL011_OVERLAY" --config "$PL011_CONF"
    print_command "$WEST_BIN" build -p always -b "$ZEPHYR_BOARD" \
        -d "$BUILD_DIR" "$APP_DIR" -- \
        "-DDTC_OVERLAY_FILE=$PL011_OVERLAY" "-DEXTRA_CONF_FILE=$PL011_CONF"
    print_command python3 "$PROFILE_VALIDATOR" \
        --overlay "$PL011_OVERLAY" --config "$PL011_CONF" \
        --final-dts "$BUILD_DIR/zephyr/zephyr.dts" \
        --final-config "$BUILD_DIR/zephyr/.config"
    if [[ "$MODE" == native || "$MODE" == all ]]; then
        print_command timeout --signal=TERM --kill-after=5s \
            "$NATIVE_TIMEOUT_SECONDS" "$WEST_BIN" build -d "$BUILD_DIR" -t run
    fi
    if [[ "$MODE" == axvisor || "$MODE" == all ]]; then
        print_command python3 "$GENERATOR" "$VM_TEMPLATE" \
            "$BUILD_DIR/zephyr/zephyr.elf" "$BUILD_DIR/zephyr/zephyr.bin" \
            "$VMCONFIG_PATH" "$PROVENANCE_PATH"
        print_command cargo xtask qemu --config "$AXVISOR_BUILD_CONFIG_REL" \
            --qemu-config "$QEMU_CONFIG_PATH" --vmconfigs "$VMCONFIG_PATH" \
            --rootfs "$BUILD_ROOT/rootfs-placeholder.img"
        if [[ -n "$dry_console_identity" ]]; then
            log "console mode: framed ($dry_console_identity)"
            print_command python3 "$CONSOLE_DEMUXER" \
                --host-log "$RUN_DIR/axvisor-qemu.log" \
                --output-dir "$RUN_DIR" --expect-vm "$dry_console_identity"
        else
            log 'console mode: legacy_raw'
        fi
    fi
    exit 0
fi

[[ -n "${ZEPHYR_BASE:-}" ]] || die 'ZEPHYR_BASE is required'
[[ -n "${ZEPHYR_SDK_INSTALL_DIR:-}" ]] || die 'ZEPHYR_SDK_INSTALL_DIR is required'
ZEPHYR_BASE="$(cd "$ZEPHYR_BASE" && pwd -P)"
ZEPHYR_SDK_INSTALL_DIR="$(cd "$ZEPHYR_SDK_INSTALL_DIR" && pwd -P)"
export ZEPHYR_BASE ZEPHYR_SDK_INSTALL_DIR

AARCH64_MUSL_TOOLCHAIN_BIN="${AXVISOR_AARCH64_MUSL_TOOLCHAIN_BIN:-/opt/aarch64-linux-musl-cross/bin}"
if ! command -v aarch64-linux-musl-cc >/dev/null 2>&1 \
    && [[ -x "$AARCH64_MUSL_TOOLCHAIN_BIN/aarch64-linux-musl-cc" ]]; then
    export PATH="$AARCH64_MUSL_TOOLCHAIN_BIN:$PATH"
fi

for command_name in \
    awk cargo cargo-objcopy clang find git python3 qemu-system-aarch64 \
    sed sha256sum sort tar tee timeout truncate xargs; do
    require_command "$command_name"
done
if [[ "$MODE" == axvisor || "$MODE" == all ]]; then
    require_command aarch64-linux-musl-cc
    require_command ldconfig
    LIBCLANG_PATH_RESOLVED="$(ldconfig -p 2>/dev/null | awk '/libclang.*\.so/{ print $NF; exit }')"
    [[ -n "$LIBCLANG_PATH_RESOLVED" && -f "$LIBCLANG_PATH_RESOLVED" ]] \
        || die 'libclang shared library not found'
else
    LIBCLANG_PATH_RESOLVED=not_required
fi
if [[ "$WEST_BIN" == */* ]]; then
    [[ -x "$WEST_BIN" ]] || die "WEST_BIN is not executable: $WEST_BIN"
else
    require_command "$WEST_BIN"
fi

ZEPHYR_HEAD="$(git -C "$ZEPHYR_BASE" rev-parse HEAD)"
[[ "$ZEPHYR_HEAD" == "$ZEPHYR_REVISION" ]] \
    || die "Zephyr revision mismatch: expected $ZEPHYR_REVISION, got $ZEPHYR_HEAD"
[[ -z "$(git -C "$ZEPHYR_BASE" status --porcelain)" ]] \
    || die "Zephyr worktree must be clean: $ZEPHYR_BASE"
[[ -f "$ZEPHYR_SDK_INSTALL_DIR/sdk_version" ]] \
    || die "Zephyr SDK version file is missing: $ZEPHYR_SDK_INSTALL_DIR/sdk_version"
ACTUAL_SDK_VERSION="$(tr -d '[:space:]' < "$ZEPHYR_SDK_INSTALL_DIR/sdk_version")"
[[ "$ACTUAL_SDK_VERSION" == "$ZEPHYR_SDK_VERSION" ]] \
    || die "Zephyr SDK mismatch: expected $ZEPHYR_SDK_VERSION, got $ACTUAL_SDK_VERSION"
ZEPHYR_TOOLCHAIN_CC="$ZEPHYR_SDK_INSTALL_DIR/gnu/$ZEPHYR_TOOLCHAIN_NAME/bin/$ZEPHYR_TOOLCHAIN_NAME-gcc"
[[ -x "$ZEPHYR_TOOLCHAIN_CC" ]] \
    || die "Zephyr target toolchain is missing: $ZEPHYR_TOOLCHAIN_CC"
ACTUAL_WEST_VERSION="$("$WEST_BIN" --version | awk '{print $NF}' | sed 's/^v//')"
[[ "$ACTUAL_WEST_VERSION" == "$ZEPHYR_WEST_VERSION" ]] \
    || die "west mismatch: expected $ZEPHYR_WEST_VERSION, got $ACTUAL_WEST_VERSION"

[[ ! -e "$RUN_DIR" ]] || die "run directory already exists: $RUN_DIR"
mkdir -p "$RUN_DIR/configs" "$RUN_DIR/source-state" "$BUILD_ROOT"
cp "$CONFIG_PATH" "$RUN_DIR/configs/qemu-aarch64-zephyr-smoke.env"
cp "$QEMU_CONFIG_PATH" "$RUN_DIR/configs/qemu-aarch64-zephyr-smoke.toml"
cp "$VM_TEMPLATE" "$RUN_DIR/configs/zephyr-smp1.template.toml"
cp "$AXVISOR_DIR/$AXVISOR_BUILD_CONFIG_REL" "$RUN_DIR/configs/axvisor-build.toml"
cp "$APP_DIR/CMakeLists.txt" "$RUN_DIR/configs/app-CMakeLists.txt"
cp "$APP_DIR/prj.conf" "$RUN_DIR/configs/app-prj.conf"
cp "$APP_DIR/src/main.c" "$RUN_DIR/configs/app-main.c"
cp "$PL011_OVERLAY" "$RUN_DIR/configs/qemu-cortex-a53-pl011-polling.overlay"
cp "$PL011_CONF" "$RUN_DIR/configs/qemu-cortex-a53-pl011-polling.conf"

(
    cd "$ZEPHYR_BASE"
    "$WEST_BIN" manifest --freeze --active-only
) > "$RUN_DIR/manifest-frozen.yml"
ACTUAL_MANIFEST_SHA256="$(sha256_file "$RUN_DIR/manifest-frozen.yml")"
[[ "$ACTUAL_MANIFEST_SHA256" == "$ZEPHYR_MANIFEST_SHA256" ]] \
    || die "Zephyr manifest mismatch: expected $ZEPHYR_MANIFEST_SHA256, got $ACTUAL_MANIFEST_SHA256"

GIT_COMMIT="$(git_repo rev-parse HEAD)"
GIT_BRANCH="$(git_repo branch --show-current)"
snapshot_paths=(
    .gitattributes
    .github/workflows/ci.yml
    .gitignore
    Cargo.lock
    Cargo.toml
    configs/contest
    os/arceos/modules/axhal
    os/axvisor/Cargo.toml
    os/axvisor/configs/board/qemu-aarch64-zephyr-smoke.toml
    os/axvisor/configs/vms/qemu/aarch64/linux-smp2.toml
    os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml
    os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1.toml
    os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml
    os/axvisor/doc/qemu-quickstart.md
    os/axvisor/doc/qemu-quickstart_cn.md
    os/axvisor/scripts/quick-start.sh
    os/axvisor/scripts/setup_qemu.sh
    os/axvisor/src
    os/axvisor/xtask/src/main.rs
    platforms/ax-plat
    platforms/axplat-dyn
    platforms/someboot
    scripts/contest
    scripts/test/check_axvisor_guest_console_demux.py
    scripts/test/check_axvisor_image_cli.py
    scripts/test/check_axvisor_linux_smp2.py
    scripts/test/check_axvisor_pl011_console.py
    scripts/test/check_axvisor_pl011_guest_fdt.py
    scripts/test/check_axvisor_setup_command.py
    scripts/test/check_axvisor_zephyr_smoke.py
    scripts/test/check_axvm_map_reserved_hpa.py
    scripts/test/check_axvm_physical_resource_claims.py
    scripts/test/check_ci_paths.py
    scripts/test/check_contest_baseline.py
    virtualization/arm_vcpu
    virtualization/arm_vgic
    virtualization/axdevice
    virtualization/axdevice_base
    virtualization/axvm
)
git_repo status --short --branch -- "${snapshot_paths[@]}" \
    > "$RUN_DIR/source-state/git-status.txt"
mkdir -p "$RUN_DIR/source-state/implementation"
(
    cd "$REPO_ROOT"
    cp --parents -R "${snapshot_paths[@]}" "$RUN_DIR/source-state/implementation"
)
printf '%s\n' "${snapshot_paths[@]}" > "$RUN_DIR/source-state/snapshot-paths.txt"
(
    cd "$RUN_DIR/source-state/implementation"
    find . -type f -print0 | sort -z | xargs -0 sha256sum \
        > "$RUN_DIR/source-state/implementation-files.sha256"
)
git_repo diff --binary --no-ext-diff HEAD -- \
    .gitattributes .github/workflows/ci.yml .gitignore \
    os/axvisor/doc/qemu-quickstart.md \
    os/axvisor/doc/qemu-quickstart_cn.md \
    os/axvisor/scripts/quick-start.sh \
    os/axvisor/scripts/setup_qemu.sh \
    os/axvisor/xtask/src/main.rs \
    scripts/test/check_ci_paths.py \
    > "$RUN_DIR/source-state/relevant-tracked.patch"
git_repo diff --binary --no-ext-diff HEAD \
    > "$RUN_DIR/source-state/tracked-worktree.patch"
git_repo ls-files --others --exclude-standard -z \
    > "$RUN_DIR/source-state/untracked-source-files.zlist"
(
    cd "$REPO_ROOT"
    tar --create --file="$RUN_DIR/source-state/untracked-source.tar" \
        --null --verbatim-files-from \
        --files-from="$RUN_DIR/source-state/untracked-source-files.zlist"
)
{
    printf 'schema_version=1\n'
    printf 'base_commit=%s\n' "$GIT_COMMIT"
    printf 'tracked_changes=tracked-worktree.patch\n'
    printf 'untracked_paths=untracked-source-files.zlist (NUL-delimited)\n'
    printf 'untracked_payload=untracked-source.tar\n'
    printf 'reconstruct_in_clean_checkout=git apply tracked-worktree.patch, then extract untracked-source.tar\n'
} > "$RUN_DIR/source-state/reconstruction.txt"
{
    printf 'schema_version=1\n'
    printf 'captured_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'run_id=%s\n' "$RUN_ID"
    printf 'git_commit=%s\n' "$GIT_COMMIT"
    printf 'git_branch=%s\n' "$GIT_BRANCH"
    printf 'zephyr_revision=%s\n' "$ZEPHYR_HEAD"
    printf 'zephyr_manifest_sha256=%s\n' "$ACTUAL_MANIFEST_SHA256"
    printf 'zephyr_sdk_version=%s\n' "$ACTUAL_SDK_VERSION"
    printf 'zephyr_toolchain=%s\n' "$ZEPHYR_TOOLCHAIN_NAME"
    printf 'west_version=%s\n' "$ACTUAL_WEST_VERSION"
    printf 'zephyr_board=%s\n' "$ZEPHYR_BOARD"
    printf 'uname=%s\n' "$(uname -a)"
    printf 'cargo=%s\n' "$(cargo --version 2>&1)"
    printf 'qemu=%s\n' "$(qemu-system-aarch64 --version 2>&1 | sed -n '1p')"
    printf 'libclang=%s\n' "$LIBCLANG_PATH_RESOLVED"
} > "$RUN_DIR/runtime-environment.txt"

BUILD_STATUS=not_run
NATIVE_STATUS=not_requested
AXVISOR_STATUS=not_requested
AXVISOR_CONSOLE_MODE=not_requested
AXVISOR_CONSOLE_VM=not_requested
NATIVE_EXIT=-1
AXVISOR_EXIT=-1

run_build() {
    local build_exit
    local profile_exit

    log 'validating the polling-only PL011 build inputs'
    set +e
    python3 "$PROFILE_VALIDATOR" \
        --overlay "$PL011_OVERLAY" --config "$PL011_CONF" \
        > "$RUN_DIR/pl011-profile-template.json" \
        2> "$RUN_DIR/pl011-profile-template.stderr"
    profile_exit=$?
    set -e
    if (( profile_exit != 0 )); then
        BUILD_STATUS=pl011_template_validation_failed
        return 1
    fi

    log "building Zephyr $ZEPHYR_BOARD at revision $ZEPHYR_REVISION"
    set +e
    (
        cd "$ZEPHYR_BASE"
        "$WEST_BIN" build -p always -b "$ZEPHYR_BOARD" -d "$BUILD_DIR" \
            "$APP_DIR" -- \
            "-DDTC_OVERLAY_FILE=$PL011_OVERLAY" \
            "-DEXTRA_CONF_FILE=$PL011_CONF"
    ) 2>&1 | tee "$RUN_DIR/build.log"
    build_exit=${PIPESTATUS[0]}
    set -e
    printf '%s\n' "$build_exit" > "$RUN_DIR/build-exit-code.txt"
    if (( build_exit != 0 )); then
        BUILD_STATUS=build_failed
        return 1
    fi
    if ! grep -Eq "Found toolchain: zephyr ${ZEPHYR_SDK_VERSION}([ )]|$)" "$RUN_DIR/build.log"; then
        BUILD_STATUS=toolchain_evidence_missing
        return 1
    fi
    if [[ ! -s "$BUILD_DIR/zephyr/.config" ]]; then
        BUILD_STATUS=zephyr_dotconfig_missing
        return 1
    fi
    if [[ ! -s "$BUILD_DIR/zephyr/zephyr.dts" ]]; then
        BUILD_STATUS=zephyr_dts_missing
        return 1
    fi
    cp "$BUILD_DIR/zephyr/.config" "$RUN_DIR/configs/zephyr-dotconfig"
    cp "$BUILD_DIR/zephyr/zephyr.dts" "$RUN_DIR/configs/zephyr.dts"
    if ! grep -Fxq "CONFIG_ARMV8_A_NS=y" "$BUILD_DIR/zephyr/.config"; then
        BUILD_STATUS=nonsecure_el1_config_missing
        return 1
    fi
    set +e
    python3 "$PROFILE_VALIDATOR" \
        --overlay "$PL011_OVERLAY" --config "$PL011_CONF" \
        --final-dts "$BUILD_DIR/zephyr/zephyr.dts" \
        --final-config "$BUILD_DIR/zephyr/.config" \
        > "$RUN_DIR/pl011-profile-final.json" \
        2> "$RUN_DIR/pl011-profile-final.stderr"
    profile_exit=$?
    set -e
    if (( profile_exit != 0 )); then
        BUILD_STATUS=pl011_final_validation_failed
        return 1
    fi
    grep -F "qemu-system-aarch64" "$BUILD_DIR/build.ninja" \
        > "$RUN_DIR/configs/native-qemu-command-lines.txt" || true
    if grep -Fq "virt,secure=on,gic-version=3" "$BUILD_DIR/build.ninja"; then
        BUILD_STATUS=secure_native_machine_detected
        return 1
    fi
    for artifact in zephyr.elf zephyr.bin; do
        [[ -s "$BUILD_DIR/zephyr/$artifact" ]] || {
            BUILD_STATUS="${artifact//./_}_missing"
            return 1
        }
        cp "$BUILD_DIR/zephyr/$artifact" "$RUN_DIR/$artifact"
    done
    BUILD_STATUS=passed
}

run_native() {
    local normalized_exit
    local validator_exit

    log 'running the periodic task in native Zephyr QEMU'
    set +e
    (
        cd "$ZEPHYR_BASE"
        timeout --signal=TERM --kill-after=5s "$NATIVE_TIMEOUT_SECONDS" \
            "$WEST_BIN" build -d "$BUILD_DIR" -t run
    ) 2>&1 | tee "$RUN_DIR/native-qemu.log"
    NATIVE_EXIT=${PIPESTATUS[0]}
    set -e
    printf '%s\n' "$NATIVE_EXIT" > "$RUN_DIR/native-exit-code.txt"

    normalized_exit=$NATIVE_EXIT
    if grep -Eq '^TGOS_ZEPHYR_SMOKE_PASS samples=10[[:space:]]*$' \
        "$RUN_DIR/native-qemu.log"; then
        normalized_exit=0
    fi
    set +e
    python3 "$VALIDATOR" "$RUN_DIR/native-qemu.log" \
        --scope native --qemu-exit "$normalized_exit" \
        --output "$RUN_DIR/native-evidence.json" \
        > "$RUN_DIR/native-validator.json"
    validator_exit=$?
    set -e
    if (( validator_exit != 0 )); then
        NATIVE_STATUS=evidence_validator_failed
    else
        NATIVE_STATUS="$(json_value "$RUN_DIR/native-evidence.json" status)"
    fi
    [[ "$NATIVE_STATUS" == passed ]]
}

verify_provenance() {
    local expected_binary
    local expected_elf
    local actual_binary
    local actual_elf

    expected_binary="$(json_value "$PROVENANCE_PATH" binarySha256)" || return 1
    expected_elf="$(json_value "$PROVENANCE_PATH" elfSha256)" || return 1
    actual_binary="$(sha256_file "$BUILD_DIR/zephyr/zephyr.bin")" || return 1
    actual_elf="$(sha256_file "$BUILD_DIR/zephyr/zephyr.elf")" || return 1
    if [[ "$actual_binary" != "$expected_binary" ]]; then
        log 'Zephyr BIN changed after VM config generation'
        return 1
    fi
    if [[ "$actual_elf" != "$expected_elf" ]]; then
        log 'Zephyr ELF changed after VM config generation'
        return 1
    fi
}

run_axvisor() {
    local console_identity
    local console_identity_exit
    local console_vm_id
    local demux_exit
    local generation_exit
    local guest_console_log
    local normalized_exit
    local runtime_qemu_config="$QEMU_CONFIG_PATH"
    local validator_exit
    local validation_log
    local validation_scope=axvisor
    local rootfs_placeholder="$BUILD_ROOT/rootfs-placeholder.img"

    log 'generating a VM config from the verified Zephyr ELF/BIN pair'
    mkdir -p "$(dirname "$VMCONFIG_PATH")"
    set +e
    python3 "$GENERATOR" "$VM_TEMPLATE" "$BUILD_DIR/zephyr/zephyr.elf" \
        "$BUILD_DIR/zephyr/zephyr.bin" "$VMCONFIG_PATH" "$PROVENANCE_PATH" \
        2>&1 | tee "$RUN_DIR/vmconfig-generation.log"
    generation_exit=${PIPESTATUS[0]}
    set -e
    printf '%s\n' "$generation_exit" > "$RUN_DIR/vmconfig-generation-exit-code.txt"
    if (( generation_exit != 0 )); then
        AXVISOR_STATUS=vmconfig_generation_failed
        return 1
    fi
    if ! verify_provenance; then
        AXVISOR_STATUS=provenance_verification_failed
        return 1
    fi
    cp "$VMCONFIG_PATH" "$RUN_DIR/vmconfig.generated.toml"
    cp "$PROVENANCE_PATH" "$RUN_DIR/vmconfig.provenance.json"

    set +e
    console_identity="$(console_identity_from_vmconfig "$VMCONFIG_PATH")"
    console_identity_exit=$?
    set -e
    if (( console_identity_exit != 0 )); then
        AXVISOR_STATUS=console_identity_invalid
        return 1
    fi
    if [[ -n "$console_identity" ]]; then
        AXVISOR_CONSOLE_MODE=framed
        AXVISOR_CONSOLE_VM="$console_identity"
        runtime_qemu_config="$RUN_DIR/configs/qemu-aarch64-zephyr-framed-console.toml"
        cp "$QEMU_CONFIG_PATH" "$runtime_qemu_config"
        sed -i '/^fail_regex = \[$/a\  "Guest console evidence failed closed",' \
            "$runtime_qemu_config"
        if [[ "$(grep -Fc "$CONSOLE_FAIL_REGEX" "$runtime_qemu_config")" != 1 ]]; then
            AXVISOR_STATUS=console_fail_regex_generation_failed
            return 1
        fi
    else
        AXVISOR_CONSOLE_MODE=legacy_raw
        AXVISOR_CONSOLE_VM=none
    fi

    # This file only suppresses xtask's default rootfs download.  The dedicated
    # build has no fs/block features and the QEMU config intentionally has no disk.
    truncate -s 4096 "$rootfs_placeholder"
    {
        printf 'cd %q\n' "$AXVISOR_DIR"
        print_command timeout --signal=INT --kill-after=30s \
            "$AXVISOR_TIMEOUT_SECONDS" cargo xtask qemu \
            --config "$AXVISOR_BUILD_CONFIG_REL" \
            --qemu-config "$runtime_qemu_config" \
            --vmconfigs "$VMCONFIG_PATH" \
            --rootfs "$rootfs_placeholder"
    } > "$RUN_DIR/axvisor-command.txt"

    log 'running the Zephyr periodic task as an AxVisor guest'
    set +e
    (
        cd "$AXVISOR_DIR"
        timeout --signal=INT --kill-after=30s "$AXVISOR_TIMEOUT_SECONDS" \
            cargo xtask qemu \
            --config "$AXVISOR_BUILD_CONFIG_REL" \
            --qemu-config "$runtime_qemu_config" \
            --vmconfigs "$VMCONFIG_PATH" \
            --rootfs "$rootfs_placeholder"
    ) 2>&1 | tee "$RUN_DIR/axvisor-qemu.log"
    AXVISOR_EXIT=${PIPESTATUS[0]}
    set -e
    printf '%s\n' "$AXVISOR_EXIT" > "$RUN_DIR/axvisor-exit-code.txt"

    normalized_exit=$AXVISOR_EXIT
    validation_log="$RUN_DIR/axvisor-qemu.log"
    if grep -Eq "$CONSOLE_FAIL_REGEX" "$RUN_DIR/axvisor-qemu.log"; then
        AXVISOR_STATUS=console_evidence_failed_closed
        log "$CONSOLE_FAIL_REGEX: host drain reported an unrecoverable evidence error"
        return 1
    fi
    if grep -Eq "$UNSAFE_ITS_LPI_REGEX" "$RUN_DIR/axvisor-qemu.log"; then
        AXVISOR_STATUS=unsafe_its_lpi_detected
        log 'unsafe ITS/LPI runtime evidence was detected before console demux'
        return 1
    fi
    if grep -Eiq 'Failed to initialize guest VM' "$RUN_DIR/axvisor-qemu.log"; then
        AXVISOR_STATUS=guest_init_failed
        log 'AxVisor rejected the Guest before console demux'
        return 1
    fi
    if [[ -n "$console_identity" ]]; then
        console_vm_id="${console_identity%%:*}"
        set +e
        python3 "$CONSOLE_DEMUXER" \
            --host-log "$RUN_DIR/axvisor-qemu.log" \
            --output-dir "$RUN_DIR" \
            --expect-vm "$console_identity" \
            > "$RUN_DIR/console-demux.json" \
            2> "$RUN_DIR/console-demux.stderr"
        demux_exit=$?
        set -e
        if (( demux_exit != 0 )); then
            AXVISOR_STATUS=console_demux_failed_closed
            log "$CONSOLE_FAIL_REGEX: strict frame demux rejected the host log"
            return 1
        fi
        guest_console_log="$RUN_DIR/guest-vm-${console_vm_id}.console.log"
        [[ -s "$guest_console_log" ]] || {
            AXVISOR_STATUS=console_guest_log_missing
            log "$CONSOLE_FAIL_REGEX: demux did not publish the expected guest log"
            return 1
        }
        validation_log="$guest_console_log"
        validation_scope=native
        if ! grep -Eq "VM\\[$console_vm_id\\][[:space:]]+created success" \
            "$RUN_DIR/axvisor-qemu.log" \
            || ! grep -Eq "Spawning task for VM\\[$console_vm_id\\] VCpu\\[0\\]" \
                "$RUN_DIR/axvisor-qemu.log" \
            || ! grep -Eq "VM\\[$console_vm_id\\] boot success" \
                "$RUN_DIR/axvisor-qemu.log"; then
            AXVISOR_STATUS=axvisor_console_lifecycle_missing
            log "$CONSOLE_FAIL_REGEX: AxVisor lifecycle evidence is incomplete"
            return 1
        fi
        # PL011 preserves the Guest's CRLF bytes exactly.  Match the locked
        # marker as a complete line while permitting its trailing CR so a
        # timeout after a successful smoke run can be normalized safely.
        if grep -Eq '^TGOS_ZEPHYR_SMOKE_PASS samples=10[[:space:]]*$' \
            "$guest_console_log"; then
            case "$AXVISOR_EXIT" in
                0|124|137) normalized_exit=0 ;;
            esac
        fi
    elif grep -Eq '^TGOS_ZEPHYR_SMOKE_PASS samples=10[[:space:]]*$' \
        "$RUN_DIR/axvisor-qemu.log"; then
        normalized_exit=0
    fi
    set +e
    python3 "$VALIDATOR" "$validation_log" \
        --scope "$validation_scope" --qemu-exit "$normalized_exit" \
        --output "$RUN_DIR/axvisor-evidence.json" \
        > "$RUN_DIR/axvisor-validator.json"
    validator_exit=$?
    set -e
    if (( validator_exit != 0 )); then
        AXVISOR_STATUS=evidence_validator_failed
    else
        AXVISOR_STATUS="$(json_value "$RUN_DIR/axvisor-evidence.json" status)"
    fi
    if [[ -n "$console_identity" && "$AXVISOR_STATUS" != passed ]]; then
        log "$CONSOLE_FAIL_REGEX: reconstructed guest log did not pass validation"
    fi
    if ! verify_provenance; then
        AXVISOR_STATUS=provenance_verification_failed
        return 1
    fi
    [[ "$AXVISOR_STATUS" == passed ]]
}

overall_exit=0
if ! run_build; then
    overall_exit=1
else
    if [[ "$MODE" == native || "$MODE" == all ]]; then
        run_native || overall_exit=1
    fi
    if [[ "$MODE" == axvisor || "$MODE" == all ]]; then
        if [[ "$MODE" != all || "$NATIVE_STATUS" == passed ]]; then
            run_axvisor || overall_exit=1
        else
            AXVISOR_STATUS=skipped_after_native_failure
        fi
    fi
fi

if (( overall_exit == 0 )); then
    OVERALL_STATUS=passed
else
    OVERALL_STATUS=failed
fi
cat > "$RUN_DIR/status.json" <<EOF
{
  "schemaVersion": 1,
  "runId": "$RUN_ID",
  "mode": "$MODE",
  "status": "$OVERALL_STATUS",
  "buildStatus": "$BUILD_STATUS",
  "nativeStatus": "$NATIVE_STATUS",
  "nativeExitCode": $NATIVE_EXIT,
  "axvisorStatus": "$AXVISOR_STATUS",
  "axvisorExitCode": $AXVISOR_EXIT,
  "axvisorConsoleMode": "$AXVISOR_CONSOLE_MODE",
  "axvisorConsoleVm": "$AXVISOR_CONSOLE_VM",
  "successMarker": "$ZEPHYR_SUCCESS_MARKER",
  "zephyrRevision": "$ZEPHYR_REVISION",
  "zephyrManifestSha256": "$ZEPHYR_MANIFEST_SHA256",
  "zephyrSdkVersion": "$ZEPHYR_SDK_VERSION"
}
EOF

(
    cd "$RUN_DIR"
    find . -type f ! -name checksums.sha256 -print0 \
        | sort -z \
        | xargs -0 sha256sum > checksums.sha256
)
log "evidence: $RUN_DIR"
log "result: $OVERALL_STATUS"
exit "$overall_exit"
