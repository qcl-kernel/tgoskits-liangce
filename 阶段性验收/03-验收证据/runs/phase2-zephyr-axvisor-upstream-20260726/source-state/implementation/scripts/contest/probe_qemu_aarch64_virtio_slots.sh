#!/usr/bin/env bash
# Capture QEMU Arm virt slot assignments for the planned Linux+Zephyr topology.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  probe_qemu_aarch64_virtio_slots.sh --output-dir <path> [options]

Options:
  --output-dir <path>  New directory for raw QMP/DTB/DTS and validated JSON.
  --qemu <command>     QEMU binary or path; defaults to qemu-system-aarch64.
  --timeout <seconds>  Per-QEMU-invocation timeout; defaults to 20.
  --dry-run            Print the commands without creating evidence.
  -h, --help           Show this help.

The probe pins one Linux block device and two guest-specific network devices to
explicit virtio-mmio buses. Both network devices join QEMU hub 0, which is the
planned Layer-2 carrier for the required guest-to-guest IP path.

Scope: a successful bundle proves only the observed outer-QEMU slot/MMIO/SPI
mapping. It does not prove dual-guest boot or IP connectivity.
EOF
}

log() {
    printf '[dual-guest-topology] %s\n' "$*" >&2
}

die() {
    log "ERROR: $*"
    exit 2
}

require_command() {
    local command_name="$1"
    command -v "$command_name" >/dev/null 2>&1 \
        || die "required command not found: $command_name"
}

print_command() {
    printf '  '
    printf '%q ' "$@"
    printf '\n'
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
VALIDATOR="$REPO_ROOT/scripts/contest/validate_dual_guest_topology.py"

OUTPUT_DIR=""
QEMU_BIN="${QEMU_SYSTEM_AARCH64:-qemu-system-aarch64}"
TIMEOUT_SECONDS=20
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output-dir)
            [[ $# -ge 2 ]] || die '--output-dir requires a value'
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --qemu)
            [[ $# -ge 2 ]] || die '--qemu requires a value'
            QEMU_BIN="$2"
            shift 2
            ;;
        --timeout)
            [[ $# -ge 2 ]] || die '--timeout requires a value'
            TIMEOUT_SECONDS="$2"
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

[[ -n "$OUTPUT_DIR" ]] || die '--output-dir is required'
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]{0,2}$ ]] \
    || die '--timeout must be an integer from 1 to 999'
[[ -f "$VALIDATOR" ]] || die "validator not found: $VALIDATOR"

MACHINE='virt,virtualization=on,gic-version=3'

if (( DRY_RUN )); then
    log 'dry-run: no directory will be created and QEMU will not run'
    log "planned output: $OUTPUT_DIR"
    print_command "$QEMU_BIN" -machine "$MACHINE" -cpu cortex-a72 -smp 4 \
        -m 1024M -accel tcg -nodefaults -display none -serial none \
        -monitor none -S \
        -drive 'if=none,id=linux-root,format=raw,file=<temporary-linux-root.img>' \
        -device \
        'virtio-blk-device,id=linux-block,drive=linux-root,bus=virtio-mmio-bus.0' \
        -netdev 'hubport,id=linux-netdev,hubid=0' \
        -device \
        'virtio-net-device,id=linux-net,netdev=linux-netdev,bus=virtio-mmio-bus.1,mac=02:00:00:00:00:01' \
        -netdev 'hubport,id=zephyr-netdev,hubid=0' \
        -device \
        'virtio-net-device,id=zephyr-net,netdev=zephyr-netdev,bus=virtio-mmio-bus.2,mac=02:00:00:00:00:02' \
        -qmp stdio
    print_command "$QEMU_BIN" \
        -machine "$MACHINE,dumpdtb=<temporary-host.dtb>" \
        '<same-device-arguments-as-above>'
    log 'scope: this plan does not prove dual-guest boot or IP connectivity'
    exit 0
fi

[[ ! -e "$OUTPUT_DIR" ]] \
    || die "output path already exists; refusing to overwrite: $OUTPUT_DIR"

require_command python3
require_command dtc
require_command mktemp
require_command truncate
require_command timeout
require_command sha256sum
if [[ "$QEMU_BIN" == */* ]]; then
    [[ -x "$QEMU_BIN" ]] || die "QEMU binary is not executable: $QEMU_BIN"
else
    require_command "$QEMU_BIN"
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tgos-dual-topology.XXXXXX")"
cleanup() {
    rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd -P)"
DISK_IMAGE="$TEMP_DIR/linux-root.img"
DTB_TEMP="$TEMP_DIR/host.dtb"
QMP_COMMANDS="$TEMP_DIR/qmp-commands.jsonl"
truncate -s 1M "$DISK_IMAGE"

QEMU_DEVICE_ARGS=(
    -cpu cortex-a72
    -smp 4
    -m 1024M
    -accel tcg
    -nodefaults
    -display none
    -serial none
    -monitor none
    -S
    -drive "if=none,id=linux-root,format=raw,file=$DISK_IMAGE"
    -device "virtio-blk-device,id=linux-block,drive=linux-root,bus=virtio-mmio-bus.0"
    -netdev "hubport,id=linux-netdev,hubid=0"
    -device "virtio-net-device,id=linux-net,netdev=linux-netdev,bus=virtio-mmio-bus.1,mac=02:00:00:00:00:01"
    -netdev "hubport,id=zephyr-netdev,hubid=0"
    -device "virtio-net-device,id=zephyr-net,netdev=zephyr-netdev,bus=virtio-mmio-bus.2,mac=02:00:00:00:00:02"
)

printf '%s\n' \
    '{"execute":"qmp_capabilities","id":"capabilities"}' \
    '{"execute":"human-monitor-command","arguments":{"command-line":"info qtree"},"id":"qtree"}' \
    '{"execute":"quit","id":"quit"}' \
    > "$QMP_COMMANDS"

"$QEMU_BIN" --version > "$OUTPUT_DIR/qemu-version.txt"
{
    printf 'qtree command:\n'
    print_command "$QEMU_BIN" -machine "$MACHINE" \
        "${QEMU_DEVICE_ARGS[@]}" -qmp stdio
    printf 'dtb command:\n'
    print_command "$QEMU_BIN" -machine "$MACHINE,dumpdtb=<temporary-host.dtb>" \
        "${QEMU_DEVICE_ARGS[@]}"
} > "$OUTPUT_DIR/qemu-argv.txt"

log 'capturing QMP info qtree from the paused machine'
set +e
timeout --signal=TERM --kill-after=5s "${TIMEOUT_SECONDS}s" \
    "$QEMU_BIN" -machine "$MACHINE" "${QEMU_DEVICE_ARGS[@]}" -qmp stdio \
    < "$QMP_COMMANDS" \
    > "$OUTPUT_DIR/qtree.qmp.jsonl" \
    2> "$OUTPUT_DIR/qtree.stderr.log"
qtree_exit=$?
set -e
if (( qtree_exit != 0 )); then
    die "QMP topology capture failed with exit code $qtree_exit; partial raw output remains in $OUTPUT_DIR"
fi

log 'dumping and decoding the matching QEMU-generated device tree'
set +e
timeout --signal=TERM --kill-after=5s "${TIMEOUT_SECONDS}s" \
    "$QEMU_BIN" -machine "$MACHINE,dumpdtb=$DTB_TEMP" \
    "${QEMU_DEVICE_ARGS[@]}" \
    > "$OUTPUT_DIR/dumpdtb.stdout.log" \
    2> "$OUTPUT_DIR/dumpdtb.stderr.log"
dumpdtb_exit=$?
set -e
if (( dumpdtb_exit != 0 )); then
    die "QEMU DTB capture failed with exit code $dumpdtb_exit; partial raw output remains in $OUTPUT_DIR"
fi
[[ -s "$DTB_TEMP" ]] || die "QEMU did not create a non-empty DTB: $DTB_TEMP"
cp "$DTB_TEMP" "$OUTPUT_DIR/host.dtb"
dtc -I dtb -O dts -o "$OUTPUT_DIR/host.dts" "$OUTPUT_DIR/host.dtb" \
    2> "$OUTPUT_DIR/dtc.stderr.log"

log 'cross-checking slot, MMIO, and SPI ownership'
python3 "$VALIDATOR" \
    --qmp "$OUTPUT_DIR/qtree.qmp.jsonl" \
    --dts "$OUTPUT_DIR/host.dts" \
    --dtb "$OUTPUT_DIR/host.dtb" \
    --qemu-version-file "$OUTPUT_DIR/qemu-version.txt" \
    --output "$OUTPUT_DIR/topology.json" \
    > "$OUTPUT_DIR/validator.stdout.json"

EVIDENCE_FILES=(
    qemu-version.txt
    qemu-argv.txt
    qtree.qmp.jsonl
    qtree.stderr.log
    dumpdtb.stdout.log
    dumpdtb.stderr.log
    host.dtb
    host.dts
    dtc.stderr.log
    topology.json
    validator.stdout.json
)
(
    cd "$OUTPUT_DIR"
    sha256sum "${EVIDENCE_FILES[@]}" > checksums.sha256
    sha256sum -c checksums.sha256
)

log "validated topology bundle: $OUTPUT_DIR"
log 'scope: this evidence does not prove dual-guest boot or IP connectivity'
