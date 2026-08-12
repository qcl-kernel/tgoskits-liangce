#!/usr/bin/env bash
set -euo pipefail

repo=/mnt/f/project/泉城实验室/tgoskits
evidence_root="$repo/results/baseline/runs/phase2-linux-host-carveout-20260806-01e0531-dirty-r17"
live="$evidence_root/live-r23"
prepared="$evidence_root/prepared-dma-guard-r22"
vm_config="$evidence_root/configs/vmconfig.map-reserved.toml"
guest_dtb="$live/live-capture/guest-vm-1.final.dtb"
capture_chain="$live/live-capture/capture-chain.json"
guest_dts="$live/guest-vm-1.final.dts"
review_dts="$live/guest-vm-1.review.dts"
semantic_report="$live/guest-vm-1.semantic.json"
stage2_report="$live/stage2-hpa-runtime.json"
host_report="$live/host-carveout-and-dma-guard-runtime.json"
aggregate_report="$live/linux-map-reserved-dma-guard-boot-runtime.json"

cd "$repo"

inputs=(
  "$live/axvisor-live.log"
  "$live/status.json"
  "$guest_dtb"
  "$capture_chain"
  "$prepared/host-carveout.preflight.json"
  "$prepared/host-carveout.dtb"
  "$prepared/host-carveout.dts"
  "$vm_config"
)
for input in "${inputs[@]}"; do
  test -f "$input" || {
    printf 'missing r23 input: %s\n' "$input" >&2
    exit 1
  }
done

outputs=(
  "$guest_dts"
  "$review_dts"
  "$live/guest-vm-1.final.dtc.stderr.log"
  "$live/guest-vm-1.review.dtc.stderr.log"
  "$semantic_report"
  "$stage2_report"
  "$host_report"
  "$aggregate_report"
)
for output in "${outputs[@]}"; do
  test ! -e "$output" || {
    printf 'refusing to overwrite r23 evidence: %s\n' "$output" >&2
    exit 1
  }
done

dtc -I dtb -O dts -o "$guest_dts" "$guest_dtb" \
  2>"$live/guest-vm-1.final.dtc.stderr.log"
dtc -I dtb -O dts -o "$review_dts" "$guest_dtb" \
  2>"$live/guest-vm-1.review.dtc.stderr.log"
cmp -- "$guest_dts" "$review_dts"

python3 scripts/contest/validate_captured_guest_dtb.py \
  --dts "$guest_dts" \
  --vm-config "$vm_config" \
  --output "$semantic_report"

python3 scripts/contest/validate_stage2_hpa_runtime_log.py \
  --log "$live/axvisor-live.log" \
  --vm-config "$vm_config" \
  --capture-status "$live/status.json" \
  --output "$stage2_report"

python3 scripts/contest/validate_host_carveout_runtime_log.py \
  --log "$live/axvisor-live.log" \
  --preflight "$prepared/host-carveout.preflight.json" \
  --capture-status "$live/status.json" \
  --host-dtb "$prepared/host-carveout.dtb" \
  --host-dts "$prepared/host-carveout.dts" \
  --vm-config "$vm_config" \
  --output "$host_report"

python3 scripts/contest/validate_linux_map_reserved_boot_runtime.py \
  --log "$live/axvisor-live.log" \
  --capture-status "$live/status.json" \
  --guest-dts "$guest_dts" \
  --guest-semantic-report "$semantic_report" \
  --stage2-report "$stage2_report" \
  --host-carveout-report "$host_report" \
  --vm-config "$vm_config" \
  --capture-chain "$capture_chain" \
  --guest-dtb "$guest_dtb" \
  --output "$aggregate_report"

sha256sum -- \
  "$live/status.json" \
  "$live/axvisor-live.log" \
  "$guest_dtb" \
  "$guest_dts" \
  "$review_dts" \
  "$semantic_report" \
  "$stage2_report" \
  "$host_report" \
  "$aggregate_report"

printf '%s\n' 'r23 validation completed; inspect status scopes and QEMU residuals before promotion'
