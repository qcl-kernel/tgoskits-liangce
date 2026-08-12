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
WEST_BIN="${WEST_BIN:-west}"

[[ "$RESULTS_ROOT" == "$REPO_ROOT/results/baseline/runs" ]] \
    || die 'RESULTS_ROOT_REL must remain results/baseline/runs'
for required_file in \
    "$APP_DIR/CMakeLists.txt" "$APP_DIR/prj.conf" "$APP_DIR/src/main.c" \
    "$VM_TEMPLATE" "$QEMU_CONFIG_PATH" "$GENERATOR" "$VALIDATOR" \
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
    log 'dry-run: no directories will be created and no command will run'
    log "repository: $REPO_ROOT"
    log "mode: $MODE"
    log "output: $RUN_DIR"
    print_command "$WEST_BIN" manifest --freeze --active-only
    print_command "$WEST_BIN" build -p always -b "$ZEPHYR_BOARD" -d "$BUILD_DIR" "$APP_DIR"
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
    sed sha256sum sort tee timeout truncate xargs; do
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
    configs/contest
    os/axvisor/configs/board/qemu-aarch64-zephyr-smoke.toml
    os/axvisor/configs/vms/qemu/aarch64/linux-smp2.toml
    os/axvisor/doc/qemu-quickstart.md
    os/axvisor/doc/qemu-quickstart_cn.md
    os/axvisor/scripts/quick-start.sh
    os/axvisor/scripts/setup_qemu.sh
    os/axvisor/xtask/src/main.rs
    scripts/contest
    scripts/test/check_axvisor_image_cli.py
    scripts/test/check_axvisor_linux_smp2.py
    scripts/test/check_axvisor_setup_command.py
    scripts/test/check_axvisor_zephyr_smoke.py
    scripts/test/check_ci_paths.py
    scripts/test/check_contest_baseline.py
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
NATIVE_EXIT=-1
AXVISOR_EXIT=-1

run_build() {
    local build_exit

    log "building Zephyr $ZEPHYR_BOARD at revision $ZEPHYR_REVISION"
    set +e
    (
        cd "$ZEPHYR_BASE"
        "$WEST_BIN" build -p always -b "$ZEPHYR_BOARD" -d "$BUILD_DIR" "$APP_DIR"
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
    cp "$BUILD_DIR/zephyr/.config" "$RUN_DIR/configs/zephyr-dotconfig"
    if ! grep -Fxq "CONFIG_ARMV8_A_NS=y" "$BUILD_DIR/zephyr/.config"; then
        BUILD_STATUS=nonsecure_el1_config_missing
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
    local generation_exit
    local normalized_exit
    local validator_exit
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

    # This file only suppresses xtask's default rootfs download.  The dedicated
    # build has no fs/block features and the QEMU config intentionally has no disk.
    truncate -s 4096 "$rootfs_placeholder"
    {
        printf 'cd %q\n' "$AXVISOR_DIR"
        print_command timeout --signal=INT --kill-after=30s \
            "$AXVISOR_TIMEOUT_SECONDS" cargo xtask qemu \
            --config "$AXVISOR_BUILD_CONFIG_REL" \
            --qemu-config "$QEMU_CONFIG_PATH" \
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
            --qemu-config "$QEMU_CONFIG_PATH" \
            --vmconfigs "$VMCONFIG_PATH" \
            --rootfs "$rootfs_placeholder"
    ) 2>&1 | tee "$RUN_DIR/axvisor-qemu.log"
    AXVISOR_EXIT=${PIPESTATUS[0]}
    set -e
    printf '%s\n' "$AXVISOR_EXIT" > "$RUN_DIR/axvisor-exit-code.txt"

    normalized_exit=$AXVISOR_EXIT
    if grep -Eq '^TGOS_ZEPHYR_SMOKE_PASS samples=10[[:space:]]*$' \
        "$RUN_DIR/axvisor-qemu.log"; then
        normalized_exit=0
    fi
    set +e
    python3 "$VALIDATOR" "$RUN_DIR/axvisor-qemu.log" \
        --scope axvisor --qemu-exit "$normalized_exit" \
        --output "$RUN_DIR/axvisor-evidence.json" \
        > "$RUN_DIR/axvisor-validator.json"
    validator_exit=$?
    set -e
    if (( validator_exit != 0 )); then
        AXVISOR_STATUS=evidence_validator_failed
    else
        AXVISOR_STATUS="$(json_value "$RUN_DIR/axvisor-evidence.json" status)"
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
