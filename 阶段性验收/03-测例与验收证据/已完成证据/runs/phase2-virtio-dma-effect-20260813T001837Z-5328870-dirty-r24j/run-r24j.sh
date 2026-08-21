#!/usr/bin/env bash
# P2-DMA-01 r24j 正式运行命令（记录于 run 目录，SHA-256 可复核）
set -euo pipefail
repo=/mnt/f/project/泉城实验室/tgoskits
cd "$repo"

run=/mnt/f/project/泉城实验室/tgoskits/results/baseline/runs/phase2-virtio-dma-effect-20260813T001837Z-5328870-dirty-r24j
nonce=$(cat "$run/inputs/session-nonce.txt")

python3 scripts/contest/run_virtio_dma_effect_probe.py \
  --repository "$repo" \
  --axvisor-dir "$repo/os/axvisor" \
  --build-config "$repo/os/axvisor/configs/board/qemu-aarch64-linux-dma-guard-evidence.toml" \
  --qemu-config "$run/topology/qemu.toml" \
  --vmconfig "$run/inputs/vmconfig.map-reserved.toml" \
  --rootfs "$run/prepared/linux-dma-effect-rootfs.ext4" \
  --rootfs-plan "$run/prepared/linux-dma-effect-rootfs.json" \
  --qemu-plan "$run/topology/plan.json" \
  --expected-sector "$run/topology/probe.raw" \
  --host-dtb "$run/host-carveout/host-carveout.dtb" \
  --kernel-prereq-manifest "$run/prepared/guest-kernel-virtio-console.json" \
  --guard-hpa 0x180000000 \
  --guard-size 0x200000 \
  --nonce "$nonce" \
  --timeout 1800 \
  --cargo-bin /root/.cargo/bin/cargo \
  --evidence-dir "$run/live"
