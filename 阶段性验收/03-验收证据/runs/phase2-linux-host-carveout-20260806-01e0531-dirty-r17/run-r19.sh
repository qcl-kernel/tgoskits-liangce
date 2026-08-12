#!/usr/bin/env bash
set -eu

export PATH="/root/.cargo/bin:${PATH}"
cd /mnt/f/project/泉城实验室/tgoskits
exec python3 scripts/contest/run_live_guest_dtb_capture.py \
  --axvisor-dir /mnt/f/project/泉城实验室/tgoskits/os/axvisor \
  --build-config /mnt/f/project/泉城实验室/tgoskits/os/axvisor/configs/board/qemu-aarch64-linux-carveout-evidence.toml \
  --qemu-config /mnt/f/project/泉城实验室/tgoskits/results/baseline/runs/phase2-linux-host-carveout-20260806-01e0531-dirty-r17/configs/qemu-aarch64-linux-host-dtb.toml \
  --vmconfig /mnt/f/project/泉城实验室/tgoskits/results/baseline/runs/phase2-linux-host-carveout-20260806-01e0531-dirty-r17/configs/vmconfig.map-reserved.toml \
  --host-dtb /mnt/f/project/泉城实验室/tgoskits/results/baseline/runs/phase2-linux-host-carveout-20260806-01e0531-dirty-r17/prepared/host-carveout.dtb \
  --rootfs /root/.cache/tgoskits/axvisor-images/qemu_aarch64_linux/rootfs.img \
  --evidence-dir /mnt/f/project/泉城实验室/tgoskits/results/baseline/runs/phase2-linux-host-carveout-20260806-01e0531-dirty-r17/live-r19 \
  --ready-timeout-seconds 1800 \
  --capture-timeout-seconds 20 \
  --post-resume-marker 'AXVISOR_STAGE2_HPA_POSTRUN vm=1 vcpu=0 kind=reserved gpa=0x80000000 hpa=0x80000000 page_size=4096 identity=1' \
  --post-resume-timeout-seconds 180 \
  --shutdown-timeout-seconds 30
