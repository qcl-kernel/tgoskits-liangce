#!/usr/bin/env bash
# Collect reproducible, unmodified AxVisor QEMU AArch64 guest baselines.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  run_axvisor_baseline.sh --guest <arceos|linux|linux-smp2|all> [options]

Options:
  --guest <name>  Guest baseline to run. Required.
  --run-id <id>   Evidence directory name. Defaults to UTC time + Git SHA.
  --dry-run       Validate inputs and print commands without writing or running.
  -h, --help      Show this help.
EOF
}

log() {
    printf '[contest-baseline] %s\n' "$*" >&2
}

die() {
    log "ERROR: $*"
    exit 2
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

validate_relative_path() {
    local name="$1"
    local value="$2"

    [[ -n "$value" ]] || die "$name must not be empty"
    [[ "$value" != /* ]] || die "$name must be repository-relative: $value"
    [[ "/$value/" != *'/../'* ]] || die "$name must not contain '..': $value"
}

print_command() {
    printf '  '
    printf '%q ' "$@"
    printf '\n'
}

csv_field() {
    local value="${1//\"/\"\"}"
    printf '"%s"' "$value"
}

json_escape() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//$'\n'/\\n}"
    value="${value//$'\r'/\\r}"
    value="${value//$'\t'/\\t}"
    printf '%s' "$value"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
CONFIG_PATH="$REPO_ROOT/configs/contest/qemu-aarch64-baseline.env"

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

GUEST=""
RUN_ID=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --guest)
            [[ $# -ge 2 ]] || die '--guest requires a value'
            GUEST="$2"
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

case "$GUEST" in
    arceos|linux|linux-smp2|all) ;;
    '') die '--guest is required' ;;
    *) die "unsupported guest: $GUEST" ;;
esac

[[ -f "$CONFIG_PATH" ]] || die "configuration not found: $CONFIG_PATH"
# This file is checked into the repository and is therefore trusted shell input.
# shellcheck source=../../configs/contest/qemu-aarch64-baseline.env
source "$CONFIG_PATH"

required_variables=(
    AXVISOR_DIR_REL
    BUILD_CONFIG_REL
    QEMU_CONFIG_REL
    LINUX_QEMU_CONFIG_REL
    LINUX_SMP2_QEMU_CONFIG_REL
    RESULTS_ROOT_REL
    TIMEOUT_SECONDS
    ARCEOS_SETUP_GUEST
    ARCEOS_VMCONFIG_REL
    ARCEOS_SUCCESS_MARKER
    LINUX_SETUP_GUEST
    LINUX_VMCONFIG_REL
    LINUX_SUCCESS_MARKER
    LINUX_SMP2_SETUP_GUEST
    LINUX_SMP2_VMCONFIG_REL
    LINUX_SMP2_SUCCESS_MARKER
)
for variable in "${required_variables[@]}"; do
    [[ -n "${!variable:-}" ]] || die "missing configuration value: $variable"
done

validate_relative_path AXVISOR_DIR_REL "$AXVISOR_DIR_REL"
validate_relative_path BUILD_CONFIG_REL "$BUILD_CONFIG_REL"
validate_relative_path QEMU_CONFIG_REL "$QEMU_CONFIG_REL"
validate_relative_path LINUX_QEMU_CONFIG_REL "$LINUX_QEMU_CONFIG_REL"
validate_relative_path LINUX_SMP2_QEMU_CONFIG_REL "$LINUX_SMP2_QEMU_CONFIG_REL"
validate_relative_path RESULTS_ROOT_REL "$RESULTS_ROOT_REL"
validate_relative_path ARCEOS_VMCONFIG_REL "$ARCEOS_VMCONFIG_REL"
validate_relative_path LINUX_VMCONFIG_REL "$LINUX_VMCONFIG_REL"
validate_relative_path LINUX_SMP2_VMCONFIG_REL "$LINUX_SMP2_VMCONFIG_REL"
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]{0,4}$ ]] \
    || die "TIMEOUT_SECONDS must be an integer from 1 to 99999"

AXVISOR_DIR="$(cd "$REPO_ROOT/$AXVISOR_DIR_REL" && pwd -P)"
EXPECTED_AXVISOR_DIR="$(cd "$REPO_ROOT/os/axvisor" && pwd -P)"
[[ "$AXVISOR_DIR" == "$EXPECTED_AXVISOR_DIR" ]] \
    || die "AXVISOR_DIR_REL must resolve to os/axvisor"

RESULTS_ROOT="$REPO_ROOT/$RESULTS_ROOT_REL"
EXPECTED_RESULTS_ROOT="$REPO_ROOT/results/baseline/runs"
[[ "$RESULTS_ROOT" == "$EXPECTED_RESULTS_ROOT" ]] \
    || die "RESULTS_ROOT_REL must be results/baseline/runs"

[[ -f "$AXVISOR_DIR/scripts/setup_qemu.sh" ]] \
    || die 'AxVisor setup script is missing'
[[ -f "$AXVISOR_DIR/$BUILD_CONFIG_REL" ]] \
    || die "build config is missing: $BUILD_CONFIG_REL"
[[ -f "$AXVISOR_DIR/$QEMU_CONFIG_REL" ]] \
    || die "QEMU config is missing: $QEMU_CONFIG_REL"
[[ -f "$REPO_ROOT/$LINUX_QEMU_CONFIG_REL" ]] \
    || die "Linux QEMU smoke config is missing: $LINUX_QEMU_CONFIG_REL"
[[ -f "$REPO_ROOT/$LINUX_SMP2_QEMU_CONFIG_REL" ]] \
    || die "Linux SMP2 QEMU smoke config is missing: $LINUX_SMP2_QEMU_CONFIG_REL"

if [[ -z "$RUN_ID" ]]; then
    SHORT_SHA="$(git_repo rev-parse --short=12 HEAD 2>/dev/null || printf 'unknown')"
    RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$SHORT_SHA"
fi
[[ "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] \
    || die "invalid run ID: $RUN_ID"

RUN_DIR="$RESULTS_ROOT/$RUN_ID"

guest_values() {
    local guest="$1"
    case "$guest" in
        arceos)
            SETUP_GUEST="$ARCEOS_SETUP_GUEST"
            VMCONFIG_REL="$ARCEOS_VMCONFIG_REL"
            SUCCESS_MARKER="$ARCEOS_SUCCESS_MARKER"
            QEMU_CONFIG_PATH="$AXVISOR_DIR/$QEMU_CONFIG_REL"
            ;;
        linux)
            SETUP_GUEST="$LINUX_SETUP_GUEST"
            VMCONFIG_REL="$LINUX_VMCONFIG_REL"
            SUCCESS_MARKER="$LINUX_SUCCESS_MARKER"
            QEMU_CONFIG_PATH="$REPO_ROOT/$LINUX_QEMU_CONFIG_REL"
            ;;
        linux-smp2)
            SETUP_GUEST="$LINUX_SMP2_SETUP_GUEST"
            VMCONFIG_REL="$LINUX_SMP2_VMCONFIG_REL"
            SUCCESS_MARKER="$LINUX_SMP2_SUCCESS_MARKER"
            QEMU_CONFIG_PATH="$REPO_ROOT/$LINUX_SMP2_QEMU_CONFIG_REL"
            ;;
        *)
            die "internal unsupported guest: $guest"
            ;;
    esac
}

print_guest_plan() {
    local guest="$1"
    guest_values "$guest"

    log "guest: $guest"
    printf '  cd %q\n' "$AXVISOR_DIR"
    print_command bash scripts/setup_qemu.sh "$SETUP_GUEST"
    print_command timeout --signal=INT --kill-after=30s --foreground "$TIMEOUT_SECONDS" \
        cargo xtask qemu \
        --config "$BUILD_CONFIG_REL" \
        --qemu-config "$QEMU_CONFIG_PATH" \
        --vmconfigs "$VMCONFIG_REL"
    printf '  success marker: %q\n' "$SUCCESS_MARKER"
}

if (( DRY_RUN )); then
    log 'dry-run: no directories will be created and no commands will run'
    log "repository: $REPO_ROOT"
    log "configuration: $CONFIG_PATH"
    log "output: $RUN_DIR"
    if [[ "$GUEST" == all ]]; then
        print_guest_plan arceos
        print_guest_plan linux
    else
        print_guest_plan "$GUEST"
    fi
    exit 0
fi

export AXVISOR_IMAGE_LOCAL_STORAGE="${AXVISOR_IMAGE_LOCAL_STORAGE:-${XDG_CACHE_HOME:-$HOME/.cache}/tgoskits/axvisor-images}"

AARCH64_MUSL_TOOLCHAIN_BIN="${AXVISOR_AARCH64_MUSL_TOOLCHAIN_BIN:-/opt/aarch64-linux-musl-cross/bin}"
if ! command -v aarch64-linux-musl-cc >/dev/null 2>&1 \
    && [[ -x "$AARCH64_MUSL_TOOLCHAIN_BIN/aarch64-linux-musl-cc" ]]; then
    export PATH="$AARCH64_MUSL_TOOLCHAIN_BIN:$PATH"
fi

for command_name in \
    aarch64-linux-musl-cc awk cargo cargo-objcopy clang git ldconfig \
    python3 qemu-system-aarch64 sha256sum tar tee timeout; do
    require_command "$command_name"
done

LIBCLANG_LISTING="$(ldconfig -p 2>/dev/null)"
LIBCLANG_PATH_RESOLVED="$(awk '/libclang.*\.so/{ print $NF; exit }' <<< "$LIBCLANG_LISTING")"
[[ -n "$LIBCLANG_PATH_RESOLVED" && -f "$LIBCLANG_PATH_RESOLVED" ]] \
    || die 'libclang shared library not found; install the matching libclang development package'

EFI_VIRTIO_ROM_PATH=""
for candidate in \
    /usr/share/qemu/efi-virtio.rom \
    /usr/lib/ipxe/qemu/efi-virtio.rom; do
    if [[ -f "$candidate" ]]; then
        EFI_VIRTIO_ROM_PATH="$candidate"
        break
    fi
done
[[ -n "$EFI_VIRTIO_ROM_PATH" ]] \
    || die 'efi-virtio.rom not found; install the ipxe-qemu package'

RUN_ARTIFACTS=(
    configs
    runtime-environment.txt
    git-status.txt
    source-state
    arceos
    linux
    linux-smp2
    checksums.sha256
)
if [[ -e "$RUN_DIR" && ! -d "$RUN_DIR" ]]; then
    die "run path exists but is not a directory: $RUN_DIR"
fi
for artifact in "${RUN_ARTIFACTS[@]}"; do
    [[ ! -e "$RUN_DIR/$artifact" ]] \
        || die "run already contains baseline artifact: $RUN_DIR/$artifact"
done
mkdir -p "$RUN_DIR/configs" "$RUN_DIR/source-state"

cp "$CONFIG_PATH" "$RUN_DIR/configs/qemu-aarch64-baseline.env"
cp "$AXVISOR_DIR/$BUILD_CONFIG_REL" "$RUN_DIR/configs/board.toml"
cp "$AXVISOR_DIR/$QEMU_CONFIG_REL" "$RUN_DIR/configs/qemu-arceos.toml"
cp "$REPO_ROOT/$LINUX_QEMU_CONFIG_REL" "$RUN_DIR/configs/qemu-linux.toml"
cp "$REPO_ROOT/$LINUX_SMP2_QEMU_CONFIG_REL" "$RUN_DIR/configs/qemu-linux-smp2.toml"

GIT_COMMIT="$(git_repo rev-parse HEAD)"
GIT_BRANCH="$(git_repo branch --show-current)"
GIT_PORCELAIN="$(git_repo status --porcelain)"
GIT_STATUS_SHORT="$(git_repo status --short --branch)"
if [[ -n "$GIT_PORCELAIN" ]]; then
    GIT_DIRTY=true
else
    GIT_DIRTY=false
fi

git_repo diff --binary --no-ext-diff HEAD > "$RUN_DIR/source-state/worktree.patch"
git_repo ls-files --others --exclude-standard -z \
    > "$RUN_DIR/source-state/untracked-files.zlist"
(
    cd "$REPO_ROOT"
    tar --null \
        --files-from="$RUN_DIR/source-state/untracked-files.zlist" \
        --create \
        --file="$RUN_DIR/source-state/untracked-files.tar"
)
while IFS= read -r -d '' relative_path; do
    hash_line="$(sha256sum "$REPO_ROOT/$relative_path")"
    printf '%s  %s\n' "${hash_line%% *}" "$relative_path"
done < "$RUN_DIR/source-state/untracked-files.zlist" \
    > "$RUN_DIR/source-state/untracked-files.sha256"

{
    printf 'schema_version=1\n'
    printf 'captured_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'run_id=%s\n' "$RUN_ID"
    printf 'git_commit=%s\n' "$GIT_COMMIT"
    printf 'git_branch=%s\n' "$GIT_BRANCH"
    printf 'git_dirty=%s\n' "$GIT_DIRTY"
    printf 'uname=%s\n' "$(uname -a)"
    printf 'rustc=%s\n' "$(rustc --version 2>&1 || printf unavailable)"
    printf 'cargo=%s\n' "$(cargo --version 2>&1 || printf unavailable)"
    printf 'clang=%s\n' "$(clang --version 2>&1 | sed -n '1p')"
    printf 'libclang=%s\n' "$LIBCLANG_PATH_RESOLVED"
    printf 'cargo_objcopy=%s\n' "$(cargo-objcopy --version 2>&1 | sed -n '1p')"
    printf 'aarch64_musl_cc=%s\n' "$(aarch64-linux-musl-cc --version 2>&1 | sed -n '1p')"
    printf 'qemu=%s\n' "$(qemu-system-aarch64 --version 2>&1 | sed -n '1p')"
    printf 'efi_virtio_rom=%s\n' "$EFI_VIRTIO_ROM_PATH"
} > "$RUN_DIR/runtime-environment.txt"

printf '%s\n' "$GIT_STATUS_SHORT" > "$RUN_DIR/git-status.txt"

INDEX_PATH="$REPO_ROOT/results/baseline/index.csv"
EXPECTED_INDEX_HEADER='run_id,started_utc,git_commit,guest,status,duration_seconds,success_marker,summary_path'
[[ -f "$INDEX_PATH" ]] || die "baseline index is missing: $INDEX_PATH"
INDEX_HEADER="$(sed -n '1p' "$INDEX_PATH" | tr -d '\r')"
[[ "$INDEX_HEADER" == "$EXPECTED_INDEX_HEADER" ]] \
    || die 'baseline index header does not match the expected schema'

append_index_row() {
    local guest="$1"
    local started_at="$2"
    local status="$3"
    local duration="$4"
    local marker="$5"
    local summary_rel="$RESULTS_ROOT_REL/$RUN_ID/$guest/status.json"
    {
        csv_field "$RUN_ID"; printf ','
        csv_field "$started_at"; printf ','
        csv_field "$GIT_COMMIT"; printf ','
        csv_field "$guest"; printf ','
        csv_field "$status"; printf ','
        csv_field "$duration"; printf ','
        csv_field "$marker"; printf ','
        csv_field "$summary_rel"; printf '\n'
    } >> "$INDEX_PATH"
}

write_status() {
    local path="$1"
    local guest="$2"
    local started_at="$3"
    local finished_at="$4"
    local duration="$5"
    local status="$6"
    local setup_exit="$7"
    local qemu_exit="$8"
    local marker="$9"
    local marker_seen="${10}"
    local marker_json
    marker_json="$(json_escape "$marker")"

    cat > "$path" <<EOF
{
  "schemaVersion": 1,
  "runId": "$RUN_ID",
  "guest": "$guest",
  "startedAtUtc": "$started_at",
  "finishedAtUtc": "$finished_at",
  "durationSeconds": $duration,
  "status": "$status",
  "setupExitCode": $setup_exit,
  "qemuExitCode": $qemu_exit,
  "successMarker": "$marker_json",
  "successMarkerSeen": $marker_seen
}
EOF
}

run_guest() {
    local guest="$1"
    local guest_dir="$RUN_DIR/$guest"
    local started_at
    local finished_at
    local started_epoch
    local finished_epoch
    local duration
    local setup_exit
    local qemu_exit
    local marker_seen=false
    local status

    guest_values "$guest"
    mkdir -p "$guest_dir"
    started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    started_epoch="$(date +%s)"

    {
        printf 'cd %q\n' "$AXVISOR_DIR"
        print_command bash scripts/setup_qemu.sh "$SETUP_GUEST"
    } > "$guest_dir/setup-command.txt"

    set +e
    (
        cd "$AXVISOR_DIR"
        bash scripts/setup_qemu.sh "$SETUP_GUEST"
    ) 2>&1 | tee "$guest_dir/setup.log"
    setup_exit=${PIPESTATUS[0]}
    set -e

    if (( setup_exit != 0 )); then
        finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        finished_epoch="$(date +%s)"
        duration=$((finished_epoch - started_epoch))
        status='setup_failed'
        write_status "$guest_dir/status.json" "$guest" "$started_at" "$finished_at" \
            "$duration" "$status" "$setup_exit" null "$SUCCESS_MARKER" false
        append_index_row "$guest" "$started_at" "$status" "$duration" "$SUCCESS_MARKER"
        return 1
    fi

    [[ -f "$AXVISOR_DIR/$VMCONFIG_REL" ]] \
        || die "setup succeeded but generated VM config is missing: $VMCONFIG_REL"
    cp "$AXVISOR_DIR/$VMCONFIG_REL" "$guest_dir/vmconfig.generated.toml"

    {
        printf 'cd %q\n' "$AXVISOR_DIR"
        print_command timeout --signal=INT --kill-after=30s --foreground "$TIMEOUT_SECONDS" \
            cargo xtask qemu \
            --config "$BUILD_CONFIG_REL" \
            --qemu-config "$QEMU_CONFIG_PATH" \
            --vmconfigs "$VMCONFIG_REL"
    } > "$guest_dir/command.txt"

    set +e
    (
        cd "$AXVISOR_DIR"
        timeout --signal=INT --kill-after=30s --foreground "$TIMEOUT_SECONDS" \
            cargo xtask qemu \
            --config "$BUILD_CONFIG_REL" \
            --qemu-config "$QEMU_CONFIG_PATH" \
            --vmconfigs "$VMCONFIG_REL"
    ) 2>&1 | tee "$guest_dir/qemu.log"
    qemu_exit=${PIPESTATUS[0]}
    set -e

    if [[ "$guest" == linux-smp2 ]]; then
        if grep -Eq '^linux-smp2-pass[[:space:]]*$' "$guest_dir/qemu.log"; then
            marker_seen=true
        fi

        set +e
        status="$(python3 "$REPO_ROOT/scripts/contest/validate_linux_smp2_log.py" \
            "$guest_dir/qemu.log" \
            --qemu-exit "$qemu_exit" \
            --output "$guest_dir/startup-evidence.json")"
        validator_exit=$?
        set -e
        if (( validator_exit != 0 )); then
            status='evidence_validator_failed'
        fi
    else
        if grep -Fq -- "$SUCCESS_MARKER" "$guest_dir/qemu.log"; then
            marker_seen=true
        fi

        if (( qemu_exit == 124 || qemu_exit == 137 )); then
            status='timed_out'
        elif (( qemu_exit != 0 )); then
            status='qemu_failed'
        elif [[ "$marker_seen" != true ]]; then
            status='marker_missing'
        else
            status='passed'
        fi
    fi

    finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    finished_epoch="$(date +%s)"
    duration=$((finished_epoch - started_epoch))
    write_status "$guest_dir/status.json" "$guest" "$started_at" "$finished_at" \
        "$duration" "$status" "$setup_exit" "$qemu_exit" "$SUCCESS_MARKER" "$marker_seen"
    append_index_row "$guest" "$started_at" "$status" "$duration" "$SUCCESS_MARKER"

    [[ "$status" == passed ]]
}

overall_status=0
if [[ "$GUEST" == all ]]; then
    run_guest arceos || overall_status=1
    run_guest linux || overall_status=1
else
    run_guest "$GUEST" || overall_status=1
fi

(
    cd "$RUN_DIR"
    find . -type f ! -name checksums.sha256 -print0 \
        | sort -z \
        | xargs -0 sha256sum
) > "$RUN_DIR/checksums.sha256"

if (( overall_status == 0 )); then
    log "baseline passed; evidence: $RUN_DIR"
else
    log "one or more baselines failed; inspect evidence: $RUN_DIR"
fi
exit "$overall_status"
