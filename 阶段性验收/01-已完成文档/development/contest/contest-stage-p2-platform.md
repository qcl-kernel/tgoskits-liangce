# 阶段 P2：平台安全前置、双 Guest 与稳定性实现手册

状态：`P2-DMA-01`、`P2-DUAL-01`、`P2-SOAK-01` 已分别由 r24j、r27、r31 以冻结边界 verified；P2 现为只读基础，当前主线转入 P4-UPSTREAM-01（2026-08-17）

适用仓库：`tgoskits`，QEMU AArch64 v1 主线

关联需求：`REQ-PLAT-001..003`、`REQ-QUAL-001..002`

关联测试：`TEST-001..007`

本手册回答“阶段 P2 具体开发什么、先改哪些文件、怎样运行、什么才算完成”。需求、架构和证据定义仍以 [`contest-spec/README.md`](contest-spec/README.md) 的优先级为准；本手册不得被用来降低其中的验收条件。实时工作树事实以 `development/current/现状.md`、`阻塞.md` 和 `开发交接.md` 为准。

## 1. P2 到底包含什么

这里的 `P2` 是项目第二阶段，不是缺陷优先级。它包含三个按顺序关闭的工作包：

```text
P2-DMA-01：单 Linux、单设备、单请求的受控 virtio-blk 字节效果观察
  -> P2-DUAL-01：同一 QEMU/AxVisor 会话中的 Linux VM1 + Zephyr VM2 300 秒 short smoke
     -> P2-SOAK-01：同一 collector 的 >= 1800 秒共存与有界生命周期退出
```

P2 的最终产出是一个可复现的 `L5 dual-Guest` short smoke 和一个 `L6 stability` 运行包。P2 不实现网络、AI 或生产实时优化：

- P2-DMA 成功不等于硬件 DMA、IOMMU/SMMU 或通用 DMA isolation；
- P2-DUAL 成功不等于 30 分钟稳定、Guest IP 或实时改善；
- P2-SOAK 成功不等于 TCP/UDP/IP、ICPC、AI 闭环或数小时综合压力；
- Guest console 只用于归属运行证据，不得替代 P4 的 IP 主链路；
- 禁止创建或提交 PR。阶段成果进入私有比赛仓库前仍需按交付手册取得明确上传授权。

## 2. 当前状态与下一动作

| 工作包/门禁 | 当前事实 | 当前允许动作 | 完成后进入 |
|---|---|---|---|
| `P2-DMA-01` / `X-DMA-001` | r24j 已满足 `success=true`/`virtio_dma_effect_probe_completed`、helper/QMP/四份 capture、payload 命中、guard 不变和无残留 | 只读保留 r23/r24d/r24e/r24j；除非输入/实现发生实质变化，不重跑或扩大结论 | `P2-DUAL-01` |
| `X-CONSOLE-001` | Linux 单 Guest console 包已通过，但 marker 走 `/dev/kmsg`→printk→earlycon；不证明 regular `/dev/console` tty | 只为 dual READY 需要修改/验证 marker 路径；偏差必须显式记录 | `P2-DUAL-01` |
| `P2-DUAL-01` / `X-DUAL-001` | r27 `success=true`/`dual_guest_short_smoke_completed`，双 READY/DTB、300 秒、unsafe=0、无残留 | 保留 r25/r26 等失败包；仅在输入或平台语义变化时新目录回归 | `P2-SOAK-01`（已关闭） |
| `P2-SOAK-01` | r31 达到 1,800 秒，validator 输出 `dual_guest_30min_coexistence_observed`，unsafe=0、无残留 | 只读保留；Zephyr timer 速率偏差进入 P3 分段测量，不重开 P2 完成状态 | P4-UPSTREAM-01 |

任何人接手时先执行：

```powershell
Set-Location 'F:\project\泉城实验室\tgoskits-upstream-integration'
git status --short --branch
Get-Content ..\development\current\开发交接.md
Get-Content ..\development\current\阻塞.md -TotalCount 220
```

不要清理 dirty worktree、不要删除 stash、不要改写 `results/baseline/runs/` 中的历史包。新的运行只能写入新的 ignored 目录。

## 3. P2 文件地图

### 3.1 已存在且应复用

| 文件 | 作用 | 证据边界 |
|---|---|---|
| `os/axvisor/configs/board/qemu-aarch64-linux-dma-guard-evidence.toml` | P2-DMA 的 AxBuild profile | 只启用单 Linux probe 所需 feature |
| `scripts/contest/guest_virtio_blk_odirect_probe.c` | Guest 内静态 helper，READY 后等待 `GO <nonce>` 并完成一次 512-byte `pread` | 用户态/O_DIRECT 路径，不证明 descriptor 直接指向该 GPA |
| `scripts/contest/verify_guest_kernel_virtio_console.py` | 验证锁定 kernel 内建 VirtIO console 前置 | 不证明 `/dev/hvc0` 已枚举 |
| `scripts/contest/prepare_disposable_virtio_dma_rootfs.py` | 从只读 cache 复制 fresh ext4，注入 helper 与 `/init` | preparation-only |
| `scripts/contest/prepare_virtio_dma_effect_qemu.py` | 生成 fresh root/probe/control QEMU TOML、sector 和 plan | preparation-only |
| `scripts/contest/prepare_virtio_dma_effect_request.py` | 已知实际 GPA 时的离线 request 审计工具 | live runner 不得预建 `live/request.json` |
| `scripts/contest/run_virtio_dma_effect_probe.py` | identity-bound QEMU/QMP/GO/四 capture/cleanup runner | 当前只覆盖 P2-DMA |
| `scripts/contest/validate_virtio_dma_effect_probe.py` | 独立复核 request/session/log/capture 的字节关系 | 只给窄字节观察 |
| `os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml` | Linux VM1 静态 dual 模板 | 当前仍是 blocker，不可直接运行 |
| `os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml` | Zephyr VM2 静态 dual 模板 | kernel path 是占位值，当前不可直接运行 |
| `configs/contest/qemu-aarch64-linux-zephyr-dual.toml` | 历史 outer slot/topology 合同 | 含 outer NIC 且 `blocked_dma_console`，不得用于 `TEST-006` |
| `scripts/contest/plan_guest_dtb_capture.py`、`execute_guest_dtb_capture.py`、`assemble_guest_dtb_capture.py` | marker -> QMP pmemsave -> final DTB 的现有链 | 必须绑定同一 QEMU identity |
| `scripts/contest/run_live_guest_dtb_capture.py`、`guest_dtb_capture_io.py` | 已有单 Guest identity、no-overwrite、pidfd 和 cleanup 实现 | 可抽取公共组件，不得复制宽松版本 |
| `scripts/contest/demux_guest_console_frames.py` | 从严格 frame 重建每 VM console | 输入本身必须来自同一次运行 |
| `scripts/contest/validate_dual_guest_soak_session.py` | 对 supplied 1,800 秒 session 做 fail-closed 离线复核 | 不是 launcher/collector |
| `scripts/test/check_axvisor_*dma*.py`、`check_axvisor_dual_guest_*.py`、Guest-DTB/console 合同 | 当前静态和 host 回归 | 不产生 runtime 结论 |

### 3.2 已实现组件与仍待补的公共层

以下是 P2 的文件级开发清单。`已实现/维护` 表示必须在原文件继续修复，不得重建同义 runner；公共 evidence 层仍待按 `IF-011` 补齐。

| 文件 | 动作 | 最小职责 |
|---|---|---|
| `scripts/contest/run_dual_guest_smoke.py` | 已实现/维护 | 唯一 P2 dual/soak launcher；多 `--vmconfig` 启动、QMP identity、双 DTB、双 console、持续时间、status-last、cleanup |
| `scripts/test/check_axvisor_run_dual_guest_smoke.py` | 已实现/维护 | 正例 fixture 和身份错配、单 READY、串线、drop、panic、超时、残留、blocked config、outer NIC 等负例 |
| `scripts/contest/prepare_dual_guest_linux_rootfs.py` | 已实现/维护 | 复制 cache rootfs，安装 run/boot-id 绑定的 `/init`；检查 2 CPU 并从 VM-local console 发 Linux READY；不改共享 cache |
| `scripts/test/check_axvisor_prepare_dual_guest_linux_rootfs.py` | 已实现/维护 | no-overwrite、ext4/helper/init/boot-id/CPU marker、source hash 不变负例 |
| `scripts/contest/prepare_linux_guest_console_inputs.py` | 已实现/维护 | X-CONSOLE 唯一输入 preparer；调用上述 rootfs preparer并生成 no-data-plane QEMU、单 Linux resolved VM config 和输入 manifest |
| `scripts/test/check_axvisor_prepare_linux_guest_console_inputs.py` | 已实现/维护 | 锁定输出名、boot-id、rootfs、2-vCPU、slot0-only、synthetic PL011、无 outer NIC/占位符与 no-overwrite 合同 |
| `scripts/contest/run_live_guest_dtb_capture.py` | opt-in 扩展 | 保留默认单 Guest capture 合同；增加 `linux-smp2-v1` framed-console profile，在同一 identity 中等待 demux 后的 Linux READY、验证 DTB/rootfs/console、cleanup 后 status-last |
| `scripts/test/check_axvisor_single_guest_dtb_harness.py` | 已扩展/维护 | 默认 CLI/token 不回归；console profile 覆盖 raw-marker 伪造、frame gap/drop/DMA、错误 VM/name/boot-id、缺 `/dev/console` marker 和 status 顺序 |
| `scripts/contest/validate_linux_guest_console_session.py` | 已实现/维护 | 独立复核 capture chain、final DTB/DTS、resolved VM config、rootfs plan、strict demux、三枚 marker、identity、manifest 与 terminal status |
| `scripts/test/check_axvisor_linux_guest_console_session.py` | 已实现/维护 | valid fixture 与跨 run、重复 marker、错误 CPU、物理 PL011/IRQ/clock/DMA、hash/status 篡改负例 |
| `configs/contest/qemu-aarch64-linux-console-gate.toml` | 已实现/维护 | X-CONSOLE 专用 outer QEMU；4 pCPU/8 GiB、slot0 root block、`init=/init`、`keep_bootcon`，无 NIC/slot1/slot2/占位符 |
| `scripts/contest/dual-guest-smoke/zephyr/` | 已实现/维护 | 不改历史 periodic-smoke；构建 run/boot-id 绑定的 Zephyr READY 和周期 health marker |
| `os/axvisor/configs/board/qemu-aarch64-dual-guest-evidence.toml` | 已实现/维护 | 仅 dual boot、root block、Guest-DTB/stage-2 evidence 所需 feature；不得启用 P4 网络 |
| `configs/contest/qemu-aarch64-linux-zephyr-dual.toml` | 已改造/维护 | 保持 run-ready no-data-plane outer 配置，不得恢复两个 `-netdev`/`virtio-net-device` 或占位路径 |
| 两份 `*-dual.toml` | 已改造/维护 | Linux 只保留 root slot 0；两 VM 继续排除 Host PL011/ITS 和全部未分配 outer slot；run 时使用解析后的副本 |
| `scripts/contest/evidence/` | 按 `IF-011` 新建公共层 | session publisher/validator 和只读 P2-DMA adapter；后续阶段复用，不让每个 runner 自创证据外壳 |

若抽取 QMP identity/cleanup 公共模块，必须先给现有单 Guest 和 DMA runner 增加等价回归，再移动实现；禁止直接 import 另一脚本的私有下划线函数后形成隐式 API。

## 4. 环境和阶段总门禁

正式构建和运行只在 Ubuntu 24.04 / WSL2 POSIX 环境执行。Windows 只承担 Git 状态和纯 Python 合同。

进入 WSL 仓库根后：

```bash
set -euo pipefail
export PATH=/root/.cargo/bin:/opt/aarch64-linux-musl-cross/bin:$PATH

for tool in python3 cargo rustc qemu-system-aarch64 dtc debugfs sha256sum timeout; do
  command -v "$tool" >/dev/null
done
test -x /opt/aarch64-linux-musl-cross/bin/aarch64-linux-musl-gcc
rustup show active-toolchain
qemu-system-aarch64 --version
python3 scripts/test/check_ci_paths.py
python3 scripts/test/check_contest_baseline.py
python3 scripts/test/check_contest_developer_docs.py
```

缺少 `dtc` 或 `debugfs` 时安装对应发行版包 `device-tree-compiler`、`e2fsprogs`。安装日志和实际版本进入 environment manifest。不要把“命令存在”写成“目标构建或 QEMU 已通过”。

修改 Rust 前完整阅读 `AGENTS.md`、`docs/guideline/code-quality.md`；若扩大平台、设备、公共接口或硬件能力，再完整阅读 `docs/guideline/feature-development.md`。Rust 改动按 crate 跑 test/strict Clippy、`cargo fmt --all -- --check` 和正式 AArch64 AxBuild；不要用裸 `cargo check` 代替 AxBuild。

## 5. P2 实现顺序与总状态机

```text
BLOCKED
  -> STATIC_GATES_PASSED
  -> FRESH_INPUTS_PREPARED
  -> LAUNCHED
  -> QEMU_IDENTITY_BOUND
  -> REQUIRED_GUEST_MARKERS_OBSERVED
  -> CAPTURES_OR_STABILITY_WINDOW_COMPLETED
  -> VALIDATED
  -> OWNED_PROCESS_GROUP_EXITED
  -> RUNTIME_DIRECTORY_REMOVED
  -> STATUS_PUBLISHED_LAST
```

任一步失败都进入：

```text
PRIMARY_FAILURE_RECORDED
  -> BOUNDED_CLEANUP_ATTEMPTED
  -> CLEANUP_RESULT_RECORDED_SEPARATELY
  -> FAILED_STATUS_PUBLISHED_LAST
```

失败后的目录仍是证据，不能删掉、补文件或改成成功。后续尝试使用新 run ID、新 nonce、新输出目录和新的 mutable rootfs。

## 6. P2-DMA-01：已完成窄观察与未来变更时的复验手册

### 6.1 固定输入与每次新建输入

| 参数 | 来源 | 是否可复用 | 运行前检查 |
|---|---|---|---|
| repository | 当前 WSL 中 `pwd -P` | 路径可复用 | 记录 HEAD、branch、dirty status 和 patch hash |
| Linux kernel | 显式 cache `qemu_aarch64_linux/qemu-aarch64` | 只读 cache 可复用 | 当前锁定 SHA-256 `d8127a4ce952ae9bfa539d3535260b8d67973b766e51a3de0b97b526bfd6722c` |
| source rootfs | 同 cache 的 `rootfs.img` | 只读 source 可复用 | 当前锁定 size `33554432`、SHA-256 `178f81bf40c1a0723a75e0b0f18b70e09b09ceae2b91ce1a990e5f7a792a224c` |
| VM TOML | 本节模板写入本次 `inputs/` | 必须 fresh | kernel path、VM1、`MapReserved` 和地址精确匹配 |
| base/derived Host DTB | 本次 QEMU `dumpdtb` 后由 preparer派生 | 必须 fresh | 8 GiB、VM1 carveout、独立 guard、derivation/preflight 均通过 |
| helper | 本次用锁定 compiler 从 C 源重新构建 | 必须 fresh | ELF64/AArch64 `ET_EXEC`、static、无 `PT_INTERP`/`DT_NEEDED` |
| disposable rootfs/plan | 本次 preparer 输出 | 必须 fresh | source 不变；输出与 plan 原先不存在 |
| QEMU TOML/probe sector/plan | 本次 preparer 输出 | 必须 fresh | root bus 0、probe bus 1、control bus 2；绝对路径 |
| `live/` request/session/result/captures | runner 输出 | 必须 fresh | 运行前整个目录不存在；request 只能由 READY 后的 runner 创建 |

r23 和 r24e 只能用于比较设计、错误和哈希，不能作为本次 mutable 输入。特别禁止复制 r24e 的 prepared rootfs、QEMU plan、probe sector、nonce、`live/` 或 `run-r24e.sh` 后改名。

### 6.2 静态门禁

```bash
python3 scripts/test/check_axvisor_guest_kernel_virtio_console.py
python3 scripts/test/check_axvisor_guest_virtio_blk_odirect_probe.py
python3 scripts/test/check_axvisor_disposable_virtio_dma_rootfs.py
python3 scripts/test/check_axvisor_prepare_virtio_dma_effect_qemu.py
python3 scripts/test/check_axvisor_prepare_virtio_dma_effect_request.py
python3 scripts/test/check_axvisor_run_virtio_dma_effect_probe.py
python3 scripts/test/check_axvisor_virtio_dma_effect_probe.py
python3 scripts/test/check_ci_paths.py
```

任一非零退出：停止实机步骤，保存命令、stderr 和 exit code；先写能在旧实现失败的确定性回归，再修复。

### 6.3 参数准备

下面命令约定仓库位于 WSL 的 `/mnt/f/project/泉城实验室/tgoskits`。若实际位置不同，只改 `repo`；其余参数从 `repo` 派生。

```bash
set -euo pipefail
repo=/mnt/f/project/泉城实验室/tgoskits
cd "$repo"

export AXVISOR_IMAGE_LOCAL_STORAGE=/root/.cache/tgoskits/axvisor-images
cache="$AXVISOR_IMAGE_LOCAL_STORAGE/qemu_aarch64_linux"
linux_kernel="$cache/qemu-aarch64"
source_rootfs="$cache/rootfs.img"
build_config="$repo/os/axvisor/configs/board/qemu-aarch64-linux-dma-guard-evidence.toml"

test -f "$linux_kernel"
test -f "$source_rootfs"
printf '%s  %s\n' \
  d8127a4ce952ae9bfa539d3535260b8d67973b766e51a3de0b97b526bfd6722c \
  "$linux_kernel" | sha256sum -c -
printf '%s  %s\n' \
  178f81bf40c1a0723a75e0b0f18b70e09b09ceae2b91ce1a990e5f7a792a224c \
  "$source_rootfs" | sha256sum -c -

nonce="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
short_rev="$(git rev-parse --short=7 HEAD)"
run_id="phase2-virtio-dma-effect-$(date -u +%Y%m%dT%H%M%SZ)-${short_rev}-dirty-r24f"
run="$repo/results/baseline/runs/$run_id"
test ! -e "$run"
mkdir -p "$run/inputs" "$run/prepared" "$run/topology" "$run/review"
printf '%s\n' "$nonce" > "$run/inputs/session-nonce.txt"
git status --short --branch > "$run/inputs/git-status.txt"
git diff --binary HEAD > "$run/inputs/dirty.patch"
git ls-files --others --exclude-standard > "$run/inputs/untracked-files.txt"
```

若 r24f 已存在，不得覆盖；把 suffix 递增为 r24g、r24h。只有 `git status --porcelain` 为空时才把 run ID 中的 `dirty` 改为 `clean`。`dirty.patch` 覆盖 staged/unstaged tracked delta；公共 input manifest 还必须逐文件绑定 `untracked-files.txt` 中与本工作包有关的源码，不能只保存文件名。若 cache 不存在，可先显式使用同一 cache 运行 `(cd os/axvisor && bash scripts/setup_qemu.sh linux)`；下载或 hash 与上表不一致时停止并登记输入漂移，不得静默沿用同名新镜像。用于与 r23/r24e 连续比较的 QEMU 为 8.2.2；版本变化时先重跑并复核 topology/Host-DTB 基线，再决定新的 run 世代。

把以下内容保存为本次 `$run/inputs/vmconfig.map-reserved.toml`。由于上面固定了 cache 路径，`kernel_path` 不再是占位符：

```toml
[base]
id = 1
name = "linux-qemu-map-reserved-evidence"
vm_type = 1
cpu_num = 1
phys_cpu_ids = [0]

[kernel]
entry_point = 0x8020_0000
image_location = "memory"
kernel_path = "/root/.cache/tgoskits/axvisor-images/qemu_aarch64_linux/qemu-aarch64"
kernel_load_addr = 0x8020_0000
dtb_load_addr = 0x8000_0000

memory_regions = [
  [0x8000_0000, 0x1000_0000, 0x7, 2],
]

[devices]
interrupt_mode = "passthrough"
passthrough_devices = [["/"]]
passthrough_addresses = []
excluded_devices = [["/intc@8000000/its@8080000"]]
emu_devices = [
  ["gppt-gicd", 0x0800_0000, 0x1_0000, 0, 0x21, []],
  ["gppt-gicr", 0x080a_0000, 0x2_0000, 0, 0x20, [1, 0x2_0000, 0]],
]
```

然后生成本次 Host DTB、helper、rootfs 和 QEMU plan：

```bash
vm_config="$run/inputs/vmconfig.map-reserved.toml"

bash scripts/contest/probe_qemu_aarch64_virtio_slots.sh \
  --output-dir "$run/outer-topology" \
  --memory-mib 8192

python3 scripts/contest/prepare_host_vm_carveout_dtb.py \
  --base-dtb "$run/outer-topology/host.dtb" \
  --vm-config "$vm_config" \
  --dma-guard 0x180000000:0x200000 \
  --output-dir "$run/host-carveout"

host_dtb="$run/host-carveout/host-carveout.dtb"
helper="$run/prepared/guest-virtio-blk-odirect-probe"
prepared_rootfs="$run/prepared/linux-dma-effect-rootfs.ext4"

/opt/aarch64-linux-musl-cross/bin/aarch64-linux-musl-gcc \
  -std=c11 -O2 -Wall -Wextra -Werror -pedantic -static -no-pie \
  scripts/contest/guest_virtio_blk_odirect_probe.c \
  -o "$helper"

python3 scripts/contest/verify_guest_kernel_virtio_console.py \
  --kernel "$linux_kernel" \
  --output "$run/prepared/guest-kernel-virtio-console.json"

python3 scripts/contest/prepare_disposable_virtio_dma_rootfs.py \
  --source-rootfs "$source_rootfs" \
  --helper "$helper" \
  --session-nonce "$nonce" \
  --output-rootfs "$prepared_rootfs" \
  --plan-output "$run/prepared/linux-dma-effect-rootfs.json"

python3 scripts/contest/prepare_virtio_dma_effect_qemu.py \
  --host-dtb "$host_dtb" \
  --disposable-rootfs "$prepared_rootfs" \
  --session-nonce "$nonce" \
  --qemu-config-output "$run/topology/qemu.toml" \
  --probe-disk-output "$run/topology/probe.raw" \
  --plan-output "$run/topology/plan.json"
```

`probe_qemu_aarch64_virtio_slots.sh` 在这里用于取得同 machine/8 GiB 的 fresh base DTB；其历史 outer NIC 拓扑结论不进入 P2-DMA 或后续 P2-DUAL 的数据面结论。

### 6.4 运行与独立复核

把实际命令保存为本次 `run-r24f.sh` 并记录它的 SHA-256。执行入口只能是现有 runner：

```bash
python3 scripts/contest/run_virtio_dma_effect_probe.py \
  --repository "$repo" \
  --axvisor-dir "$repo/os/axvisor" \
  --build-config "$build_config" \
  --qemu-config "$run/topology/qemu.toml" \
  --vmconfig "$vm_config" \
  --rootfs "$prepared_rootfs" \
  --rootfs-plan "$run/prepared/linux-dma-effect-rootfs.json" \
  --qemu-plan "$run/topology/plan.json" \
  --expected-sector "$run/topology/probe.raw" \
  --host-dtb "$host_dtb" \
  --kernel-prereq-manifest "$run/prepared/guest-kernel-virtio-console.json" \
  --guard-hpa 0x180000000 \
  --guard-size 0x200000 \
  --nonce "$nonce" \
  --timeout 1800 \
  --cargo-bin /root/.cargo/bin/cargo \
  --evidence-dir "$run/live"
```

runner 已自行调用 validator 并写 `live/result.json`。再生成一个不同路径的 no-overwrite 审计结果：

```bash
python3 scripts/contest/validate_virtio_dma_effect_probe.py \
  --request "$run/live/request.json" \
  --session "$run/live/session.json" \
  --raw-log "$run/live/observed-log-prefix.bin" \
  --expected-payload "$run/topology/probe.raw" \
  --before-guard "$run/live/before-guard.bin" \
  --after-guard "$run/live/after-guard.bin" \
  --before-payload "$run/live/before-payload.bin" \
  --after-payload "$run/live/after-payload.bin" \
  --output "$run/review/result.json"

cmp "$run/live/result.json" "$run/review/result.json"
test ! -e "/tmp/axdma-$nonce"
if pgrep -af "axvisor-dma-effect-$nonce"; then exit 1; fi
```

### 6.5 P2-DMA 完成定义

必须同时满足：

1. `live/status.json` 最后写入，`success=true` 且当前 runner-specific `status=virtio_dma_effect_probe_completed`；
2. `request.json`、`session.json`、`result.json` 和 `observed-log-prefix.bin` 全部存在；
3. READY、GO、DONE、QEMU name、PID/peer、nonce、device `dma-probe`、bus 1、Guest `/dev/vdb` 和 control `/dev/hvc0` 一致；
4. `before-payload.bin` 精确为 512 个 `0xa5`，`after-payload.bin` 精确等于 fresh `probe.raw`；
5. `before-guard.bin` 与 `after-guard.bin` 逐字节相同；
6. `result.status=controlled_virtio_blk_dma_effect_observed`，不得接受 `controlled_virtio_blk_out_of_bounds_effect_observed`；
7. immutable launch inputs 运行前后不变，mutable rootfs 的前后 hash 分开记录；
8. owned process group 已退出，QMP/control socket、pidfile 和 `/tmp/axdma-<nonce>` 均不存在；
9. 独立 validator exit 0，结果与 runner 结果一致；
10. 结论文本只能写“该设备、该请求、该会话的受控字节效果观察”。

`IF-011` 统一的是 `success: true|false`、producer-specific `status` token、primary/cleanup error、完成检查与 manifest hash，不存在通用的 `status=success|failed|blocked` token。关闭 P2-DMA 按 `TEST-005` 的上述双字段判定；不得改写 r24d/r24e。公共 evidence adapter 必须保留旧 runner 的 `success` 与 `status=virtio_dma_effect_probe_{completed,failed}`，再无损补齐统一 envelope；不得把旧 token 改写成另一套字符串。

### 6.6 失败与清理

- READY 前失败：不存在 request/capture 不等于 DMA 失败，只能记录具体启动/设备/console 错误；
- READY 后、DONE 前失败：保留 request、raw log、已完成 QMP transcript 与 capture，不能补造 after 文件；
- guard 改变：这是明确非成功的 out-of-bounds observation，立即阻止后续 gate；
- cleanup 同时失败：`primary error` 与 `cleanup error` 分开保存，cleanup 不得覆盖 primary；
- runner 返回后仍有 owned PID/socket/runtime：该 run 必须失败；仅在核对 PID、start time、PGID、QEMU name 和 nonce 后清理本 runner 拥有的对象；
- 失败目录写 README，更新工作区 `现状.md`/`阻塞.md`，下次用新 suffix。

## 7. X-CONSOLE-001：P2-DUAL 前必须关闭的 Linux 动态门禁

P2-DUAL 不能依赖 Host 向 `/bin/sh` 注入 `shell_init_cmd`，因为 VM-local PL011 v1 是 TX-only、无 RX。Linux READY 必须由 Guest 自己输出。当前 `run_live_guest_dtb_capture.py` 已有单 Guest process-group、QMP identity、final-DTB capture、pidfd shutdown 和 cleanup，但它的 `--post-resume-marker` 只搜索 Host combined log 中的明文；Guest frame 的 payload 位于 `hex=` 字段，不能用该选项证明 Guest marker。因此旧 CLI 或单独运行 demux 都不能关闭本门禁。

### 7.1 冻结的复用决策

唯一实现方案是**向现有** `scripts/contest/run_live_guest_dtb_capture.py` 增加 opt-in `linux-smp2-v1` framed-console profile；不得复制一个较宽松的单 Guest launcher，也不得把 `run_dual_guest_smoke.py` 提前改成兼容单 Guest 的多模式脚本。

扩展必须满足：

1. 未传 `--guest-console-profile` 时，现有 CLI、schema、`single_guest_live_capture_{passed,failed}` token 和测试逐字节兼容；不得改写历史单 Guest evidence。
2. profile 模式复用原 harness 的 launch、QMP `SO_PEERCRED`/name/PID/start-time identity、final-DTB capture、pidfd 与 process-group cleanup。
3. profile 模式复用 `demux_guest_console_frames.py` 的公开 parser/publisher；若需要抽公共 API，先给 demux 和旧 harness 增加等价回归，禁止 import 私有下划线函数。
4. profile 模式禁止同时传 `--post-resume-marker`。运行中 READY 等待必须增量解析完整 `AXVISOR_GUEST_CONSOLE_FRAME`，只接受 `vm=1 name=linux` 的连续 frame payload；Host 明文、其他 VM/name、坏序号、drop 或 DMA attempt 都不能解锁 shutdown。
5. QMP capture/resume 后观察到三枚 Guest marker，且重新绑定同一 QEMU identity，才允许受控退出。log 关闭后执行完整 demux、DTB 解码/语义检查、session validator、manifest 和 cleanup 检查，最后才发布 terminal status。

### 7.2 固定 profile、Guest `/init` 和输入

| 项目 | 固定值/来源 | 拒绝条件 |
|---|---|---|
| profile | `linux-smp2-v1` | 未知 profile 或与 legacy marker 选项混用 |
| VM/device identity | VM `1`、framed name `linux` | 其他 VM/name、重复 identity |
| CPU | `cpu_num=2`、`phys_cpu_ids=[0,1]` | CPU 数/集合漂移或 pCPU2/3 泄漏 |
| kernel/rootfs source | 第 6.1 节锁定 cache 与 SHA-256 | source hash 漂移或 source 被修改 |
| build profile | `os/axvisor/configs/board/qemu-aarch64-dual-guest-evidence.toml` | 启用 P4 网络或缺 Guest-DTB/console 所需 feature |
| outer QEMU | `configs/contest/qemu-aarch64-linux-console-gate.toml` | 非 4 pCPU/8 GiB、outer NIC、slot1/2、TAP/bridge/NAT、占位路径 |
| data plane | 只允许 slot0 Linux root block | 任一 netdev、virtio-net、probe/control disk |
| VM-local console | `linux`，GPA `0x09000000`、size `0x1000`、IRQ `0`、cfg `[]` | Host PL011、INTID 33、clock、DMA、HPA 或 RX |
| boot args | `root=/dev/vda rw init=/init console=ttyAMA0,115200 earlycon=pl011,mmio32,0x9000000 keep_bootcon` | `init=/bin/sh`、缺 earlycon/keep_bootcon、交互注入 |
| boot-id | `[A-Za-z0-9._-]{1,64}`，prepare 前生成 | 运行后从日志生成或复用旧 run |

`configs/contest/qemu-aarch64-linux-console-gate.toml` 是新的 checked-in 专用输入：以通用 QEMU AArch64 配置为基础，只保留 root `virtio-blk-device`；rootfs 文件由 harness 的 `--rootfs` 注入，因此 TOML 内不得含 `${workspaceFolder}` 或个人绝对路径。它不是 dual、网络或性能配置。

`prepare_dual_guest_linux_rootfs.py` 生成的 `/init` 必须自行完成以下顺序，不能读取 Host stdin：

1. 通过继承的 stdout 写一次 `AXVISOR_LINUX_INIT_ENTER vm=1 boot_id=<boot-id>`；
2. 挂载 `/proc`、`/sys` 和 devtmpfs，确认 `/proc/cpuinfo` 恰有 2 个 processor；
3. 要求 `/dev/console` 是字符设备，并以 write-only 方式成功打开独立 fd；把 `/sys/class/tty/console/active` 的 bounded 原文记录到 Guest log 和 session；
4. 只通过新打开的 `/dev/console` fd 依次写一次：

   ```text
   AXVISOR_LINUX_DEV_CONSOLE_READY vm=1 boot_id=<boot-id> device=/dev/console cpus=2
   AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=<boot-id>
   ```

5. READY 后进入有界、无 stdin 的 health loop，直到 runner 受控关闭 QEMU。

任何 mount、CPU 计数、字符设备或 open/write 失败只能写 `AXVISOR_LINUX_CONSOLE_FAIL ...` 后停在安全 loop；不得仍打印 READY。`INIT_ENTER` 与 `/dev/console` marker 的先后连续出现，是 early output 到显式 `/dev/console` 输出的动态门禁；仅看到 kernel earlycon 或 Host 文本不合格。

### 7.3 fresh 输入准备命令

先实现 `prepare_linux_guest_console_inputs.py`，让它调用 `prepare_dual_guest_linux_rootfs.py` 的公开 API并生成固定文件名；不要用临时 `sed` 改模板。每次从仓库根执行：

```bash
set -euo pipefail
repo="$(pwd -P)"
cache=/root/.cache/tgoskits/axvisor-images/qemu_aarch64_linux
source_rootfs="$cache/rootfs.img"
linux_kernel="$cache/qemu-aarch64"
boot_id="console-$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
short_rev="$(git rev-parse --short=7 HEAD)"
run="$repo/results/baseline/runs/phase2-linux-console-$(date -u +%Y%m%dT%H%M%SZ)-${short_rev}"

test ! -e "$run"
mkdir -p "$run/inputs" "$run/review"
git status --short --branch > "$run/inputs/git-status.txt"
git diff --binary HEAD > "$run/inputs/dirty.patch"
git ls-files --others --exclude-standard > "$run/inputs/untracked-files.txt"

python3 scripts/contest/prepare_linux_guest_console_inputs.py \
  --repository "$repo" \
  --source-rootfs "$source_rootfs" \
  --linux-kernel "$linux_kernel" \
  --source-vm-config "$repo/os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml" \
  --source-qemu-config "$repo/configs/contest/qemu-aarch64-linux-console-gate.toml" \
  --boot-id "$boot_id" \
  --output-dir "$run/prepared"
```

preparer 的输出目录原先必须不存在，输出名固定为：

```text
prepared/
  qemu.single-linux.toml
  linux.single-console.toml
  linux-console-rootfs.ext4
  linux-rootfs-plan.json
  console-inputs.json
```

`console-inputs.json` 逐字节绑定 source/output hash、boot-id、三枚 marker、init bytes/hash、kernel、rootfs、两份 resolved TOML 和固定 profile。resolved VM config 将 kernel path 解析为本次锁定文件，只保留 root slot0 和 synthetic console；resolved QEMU config不得含 placeholder、NIC 或 slot1/2。preparer 完成后重新验证 source kernel/rootfs hash未变。

### 7.4 实现完成后先跑的 host 门禁

```bash
python3 scripts/test/check_axvisor_linux_smp2.py
python3 scripts/test/check_axvisor_captured_guest_dtb.py
python3 scripts/test/check_axvisor_guest_console_demux.py
python3 scripts/test/check_axvisor_prepare_dual_guest_linux_rootfs.py
python3 scripts/test/check_axvisor_prepare_linux_guest_console_inputs.py
python3 scripts/test/check_axvisor_single_guest_dtb_harness.py
python3 scripts/test/check_axvisor_linux_guest_console_session.py
python3 scripts/test/check_ci_paths.py
```

新增 fixture 必须至少让以下情况失败：source/output 覆盖或 symlink、raw Host marker 伪造、payload marker 跨 frame、错误 VM/name/boot-id、sequence gap、generation 回退、drop/DMA 非零、marker 重复/乱序、CPU 不是 2、`/dev/console` marker 缺失、final DTB 带物理 PL011/INTID 33/clock/DMA、QEMU identity 漂移、manifest 篡改、cleanup 残留和 status 过早。

### 7.5 唯一 runtime CLI 与 runner 顺序

当前 harness 尚无以下三个 `--guest-console-*` 参数；实现后 X-CONSOLE 的唯一入口固定为：

```bash
test ! -e "$run/live"

python3 scripts/contest/run_live_guest_dtb_capture.py \
  --axvisor-dir "$repo/os/axvisor" \
  --build-config "$repo/os/axvisor/configs/board/qemu-aarch64-dual-guest-evidence.toml" \
  --qemu-config "$run/prepared/qemu.single-linux.toml" \
  --vmconfig "$run/prepared/linux.single-console.toml" \
  --rootfs "$run/prepared/linux-console-rootfs.ext4" \
  --guest-console-profile linux-smp2-v1 \
  --guest-console-boot-id "$boot_id" \
  --guest-console-rootfs-plan "$run/prepared/linux-rootfs-plan.json" \
  --guest-console-ready-timeout-seconds 300 \
  --ready-timeout-seconds 300 \
  --capture-timeout-seconds 30 \
  --shutdown-timeout-seconds 30 \
  --cargo-bin /root/.cargo/bin/cargo \
  --evidence-dir "$run/live"
```

profile 模式的确定顺序是：

1. 创建新 evidence/runtime，冻结所有输入、Git state、environment 和实际 argv；依赖未满足时发布 blocked 包而不是启动；
2. 用现有 harness 启动单 VM1，绑定 launcher process group、QEMU pid/name/QMP peer和 input hash；
3. 等唯一 `AXVISOR_GUEST_DTB_READY vm=1`，在同一暂停窗口捕获 final DTB，再验证同一 identity 恢复；
4. 从 Host log 的完整 frame 增量重建 VM1 bytes，要求三枚 marker 各一次且顺序正确；READY 必须位于 capture/resume 后新增的 VM1 payload，不能来自 frozen READY prefix；
5. READY 后再次核对 pid/QMP/name/start time，再用 pidfd `SIGINT -> TERM -> KILL` 有界关闭 owned group；
6. log 关闭后完整运行 strict demux，生成 final DTS 与静态语义报告；复核 rootfs plan、VM config、CPU、console、marker、frame、capture chain 和 immutable input；
7. 生成 cleanup、`linux-console-result.json`、session 和 manifest；全部 hash/validator/cleanup 通过后最后发布 terminal `status.json`。

### 7.6 证据目录和状态 token

```text
<run>/
  inputs/
    git-status.txt
    dirty.patch
    untracked-files.txt
  prepared/
    qemu.single-linux.toml
    linux.single-console.toml
    linux-console-rootfs.ext4
    linux-rootfs-plan.json
    console-inputs.json
  live/
    axvisor-live.log
    launch-command.json
    commands.jsonl
    environment.json
    live-capture/
      axvisor-ready-prefix.log
      capture-plan.json
      capture-execution.json
      capture-identity.json
      capture-result.json
      capture-chain.json
      guest-vm-1.final.dtb
      guest-vm-1.final.dts
      guest-vm-1.semantic.json
    guest-vm-1.console.log
    console-manifest.json
    linux-console-result.json
    linux-console-session.json
    cleanup.json
    manifest.json
    status.json                     # 最后写
  review/
    linux-console-result.json
```

X-CONSOLE terminal token 固定为：成功 `success=true`、`status=linux_guest_console_completed`；运行失败 `success=false`、`status=linux_guest_console_failed`；依赖未满足 `success=false`、`status=linux_guest_console_blocked` 并提供 `blockedReason`。默认 legacy harness token 不变。`linux-console-result.json.status` 固定为 `linux_guest_console_observed`，不能拿 result token冒充 terminal success。

### 7.7 独立 validator 与完成定义

runner 完成后从原件重算到新路径：

```bash
python3 scripts/contest/validate_linux_guest_console_session.py \
  --capture-status "$run/live/status.json" \
  --capture-chain "$run/live/live-capture/capture-chain.json" \
  --guest-dtb "$run/live/live-capture/guest-vm-1.final.dtb" \
  --guest-dts "$run/live/live-capture/guest-vm-1.final.dts" \
  --guest-semantic "$run/live/live-capture/guest-vm-1.semantic.json" \
  --vm-config "$run/prepared/linux.single-console.toml" \
  --rootfs-plan "$run/prepared/linux-rootfs-plan.json" \
  --console-manifest "$run/live/console-manifest.json" \
  --console-log "$run/live/guest-vm-1.console.log" \
  --session "$run/live/linux-console-session.json" \
  --manifest "$run/live/manifest.json" \
  --output "$run/review/linux-console-result.json"

cmp "$run/live/linux-console-result.json" "$run/review/linux-console-result.json"
```

只有以下全部成立，`X-CONSOLE-001` 才能关闭：

1. terminal status 为 `success=true`、`linux_guest_console_completed`，且 status-last/manifest/cleanup 通过；
2. 同一 QEMU identity 中 VM1 final DTB、resolved TOML、CPU `[0,1]`、root slot0 和 synthetic PL011 逐字段一致；
3. final DTB 只有 `0x09000000` VM-local console，`stdout-path`/earlycon 正确且无 Host PL011、INTID 33、IRQ、clock、DMA、HPA、ITS 或 outer network；
4. strict demux 仅得到 VM1/name `linux`，frame sequence连续，drop/DMA attempt 为 0；三枚 boot-id marker 各一次、顺序正确，READY 在 capture/resume 后；
5. `/dev/console` marker 确由 fresh `/init` 在检查字符设备、成功 open/write 和 2 CPU 后产生；stdin始终 disabled；
6. 独立 validator exit 0、result 为 `linux_guest_console_observed`，且与 runner result逐字节一致；
7. QEMU/process group/QMP socket/pidfile/runtime dir 无残留，输入 hash 运行前后不变。

若只看到 `INIT_ENTER` 或 kernel earlycon而缺 `/dev/console` marker，结果必须失败。下一步按单一新 run 修 polling-only console、VM-local clock描述或 boot-console 保留/交接；若判断必须新增 virtual IRQ，先停止 X-CONSOLE，实现前更新对应 `DEC/ARC/IF/TEST` 并重新评审，不能把 IRQ 当作普通修复塞入当前 TX-only/no-IRQ v1。若仍不能保持该安全边界，则记录 DEC 后回退 headless Phase 0。禁止恢复物理 PL011/INTID 33、共享 Host UART、stdin 注入或把 Host marker 写入 Guest stream。允许结论仅为“该 revision、锁定 Linux 和单 VM1 会话中，framed VM-local console可从 early output 连续到显式 `/dev/console` READY”。它不证明双 Guest、IP、DMA isolation 或实时性。

### 7.8 Zephyr dual 输入交接

X-CONSOLE 关闭后，Zephyr dual image仍必须从独立的 `dual-guest-smoke/zephyr` 应用 fresh build，并输出：

```text
AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=<boot-id>
```

dual run 的 `boot-id` 仍在 build/prepare 前生成，同时进入 Linux init、Zephyr build artifact、QEMU identity和 session；不得复用 X-CONSOLE 的 boot-id 或运行后从 Host 日志拼接 READY。

## 8. P2-DUAL-01：runner/collector 实现合同

### 8.1 输入从哪里来

| 输入 | 参数来源 | 必须检查 |
|---|---|---|
| `repository`/`axvisor-dir` | 当前 cleanly resolved WSL path | regular directory；记录 Git state |
| build config | 新建 `qemu-aarch64-dual-guest-evidence.toml` | 只含 block/fs、Guest-DTB/stage-2 evidence；无网络 backend |
| outer QEMU TOML | gate 关闭后的 no-data-plane 配置或本次其解析副本 | `-smp 4`、`-m 8g`；无 `${...}`、`-netdev`、`virtio-net-device`；只有 Linux root block |
| Linux VM config | `linux-smp2-dual.toml` 的本次解析副本 | VM1、2 vCPU、`[0,1]`、256 MiB；只允许 root slot 0；synthetic console |
| Zephyr VM config | `zephyr-smp1-dual.toml` 的本次解析副本 | VM2、1 vCPU、`[2]`、128 MiB；fresh absolute bin path；所有 outer VirtIO slot 排除 |
| Linux rootfs | `prepare_dual_guest_linux_rootfs.py` fresh 输出 | exact boot-id、CPU check、source hash 不变 |
| Zephyr image/ELF/config/DTS | 锁定 Zephyr 4.4.0/SDK 1.0.1 fresh build | boot-id、polling PL011、无 IRQ/DMA、hash |
| duration | runner 固定 short smoke 为 `300` 秒 | 不接受小于 120 秒；正式 `TEST-006` 固定 300 秒 |
| nonce/name/runtime | 32 lowercase hex；short 为 `axvisor-dual-smoke-<nonce>`，soak 为 validator 已冻结的 `axvisor-dual-soak-<nonce>`；runtime 为 `/tmp/axdual-<nonce>` | output/runtime 原先不存在 |

当前 checked-in outer TOML 含两个 QEMU hub NIC，且三份配置都明确为 blocker。因此在完成 P2-DMA 与 X-CONSOLE 前，runner 必须拒绝它们；不得靠 `--force` 跳过。gate 关闭后，配置与 `check_axvisor_dual_guest_configs.py` 必须在同一变更中迁移到 no-data-plane run-ready 合同。P4 后续网卡由 AxVisor mediated frontend 提供，不再恢复 outer NIC。

### 8.2 目标 CLI

`run_dual_guest_smoke.py` 尚未实现。实现完成后，CLI 固定为：

```bash
python3 scripts/contest/run_dual_guest_smoke.py \
  --repository <absolute-repo> \
  --axvisor-dir <absolute-repo>/os/axvisor \
  --build-config <absolute-dual-build.toml> \
  --qemu-config <absolute-no-dataplane-qemu.toml> \
  --linux-vmconfig <absolute-resolved-linux.toml> \
  --zephyr-vmconfig <absolute-resolved-zephyr.toml> \
  --linux-rootfs <absolute-fresh-linux-rootfs.ext4> \
  --zephyr-image <absolute-fresh-zephyr.bin> \
  --boot-id <safe-run-boot-id> \
  --nonce <32-lowercase-hex> \
  --duration-seconds 300 \
  --ready-timeout-seconds 300 \
  --shutdown-timeout-seconds 30 \
  --cargo-bin /root/.cargo/bin/cargo \
  --evidence-dir <absolute-new-run>/live
```

`--duration-seconds 1800` 及以上进入 soak 模式；不得另写第二套 launcher。runner 对所有输入使用解析后的绝对路径、regular/non-symlink 检查、size/SHA-256 绑定和运行后稳定性复核。

### 8.3 runner 的确定顺序

1. 拒绝已存在的 evidence/runtime 目录、非法 nonce/boot-id、blocked status、占位路径和 outer NIC；
2. 解析两份 TOML并验证 VM ID、CPU、RAM、设备、GPA、IRQ 不变量；不在代码中另硬编码一套 CPU 集作为真相；
3. 冻结 input manifest、environment、实际 argv 与 dirty patch hash；
4. 用 `cargo xtask qemu` 和两个重复 `--vmconfigs` 启动同一 AxVisor；stdin 为 disabled；创建独立 process group；
5. 通过 pidfile、`/proc/<pid>/stat` start time、boot ID、exe/cmdline、QMP `SO_PEERCRED`、`query-name` 和 nonce 绑定唯一 QEMU；
6. 等待且只接受 VM1/VM2 各一个 `AXVISOR_GUEST_DTB_READY`；复用现有 plan/executor/assembler 捕获两份 final DTB；
7. 从 Host frame log运行 strict demux；两 Guest 各只允许一个本 run boot-id 的 READY；Linux 还必须报告 2 CPU，Zephyr 周期 marker 单调；
8. 将 final DTB/DTS、TOML 与 runtime resource claims 逐字段交叉校验；Host PL011/ITS、另一 VM设备、未分配 outer slot 均不得泄漏；
9. 两 READY 完整后记 `startMonotonicNs`；连续观察 300 秒，期间禁止 panic、unsafe、init failure、duplicate READY、restart、unclassified exit、console cross-byte、drop 或 DMA attempt；
10. 观察窗口结束后写 end health marker，重新核对 QEMU identity；通过 pidfd SIGINT -> TERM -> KILL 的有界策略只清理 owned group；
11. 确认 PID/PGID、QMP socket、pidfile、runtime dir 全部消失，复核 immutable inputs；
12. 先发布 raw/derived files 和 manifest，最后发布 terminal `status.json`。

### 8.4 short-smoke 证据目录

```text
<run>/
  inputs/
    environment.json
    git-status.txt
    dirty.patch
    build.toml
    qemu.no-dataplane.toml
    linux.resolved.toml
    zephyr.resolved.toml
    linux-rootfs-plan.json
    zephyr-build-manifest.json
  live/
    axvisor.log
    qmp.jsonl
    launch-command.json
    identity.json
    guest-dtb-capture-plan.json
    guest-dtb-capture-execution.json
    guest-vm-1.final.dtb
    guest-vm-1.final.dts
    guest-vm-2.final.dtb
    guest-vm-2.final.dts
    guest-vm-1.console.log
    guest-vm-2.console.log
    console-manifest.json
    resource-claims.json
    dual-guest-session.json
    manifest.json
    commands.jsonl
    status.json
```

`status.json` 使用 `IF-011` 的布尔结果加 producer-specific token：成功为 `success=true`、`status=dual_guest_short_smoke_completed`；运行失败为 `success=false`、`status=dual_guest_short_smoke_failed`；前置门禁未满足为 `success=false`、`status=dual_guest_short_smoke_blocked` 并提供 `blockedReason`。不得另设 `resultStatus`，消费者同时校验布尔值、精确 token、manifest hash、完成检查和错误字段，不能只凭 token 名猜测结果。

### 8.5 必须先写的负例

`check_axvisor_run_dual_guest_smoke.py` 至少覆盖：

- existing evidence dir、symlink/reparse input、重复 JSON key、运行中 input hash 变化；
- checked-in `blocked_dma_console` 配置、`${workspaceFolder}`、任一 `-netdev`/outer NIC；
- VM ID 重复、CPU overlap、pCPU3 分给 Guest、RAM/MMIO/physical IRQ overlap；
- 只有一个 DTB/READY、重复 READY、错误 boot-id、VM2 marker 出现在 VM1 stream；
- final DTB 被替换、QMP peer/PID/start-time/name 不一致、禁止 QMP command；
- frame sequence gap、generation 改写、drop/DMA attempt 非零；
- panic、unsafe、init failure、unexpected exit、300 秒前停止；
- primary failure 加 cleanup failure、SIGKILL 后仍有 live group member、unknown runtime file；
- status 在 cleanup/manifest 前写入、跨 run 拼接 raw/derived artifact。

### 8.6 P2-DUAL 完成定义

`TEST-006` 只有在以下全部满足时关闭：

1. 同一 identity-bound QEMU/AxVisor 同时运行 VM1 Linux 和 VM2 Zephyr整整 300 秒；
2. 两 VM 各唯一 Guest-DTB、READY 和独立 console；
3. Linux 实际看到 2 CPU并绑定 pCPU `[0,1]`，Zephyr 绑定 `[2]`，pCPU3 不属于 Guest；
4. final DTB、resource claim、配置和实际设备/IRQ 一致；无 Host/跨 VM 泄漏；
5. outer QEMU 无数据面网卡，slot2 不直通；
6. panic/unsafe/restart/unclassified exit/cross-byte/drop/DMA-attempt 均为 0；
7. cleanup 和 residual检查通过，最后发布 `success=true`、`status=dual_guest_short_smoke_completed`，所有输入输出有 hash；
8. 对抗性合同、适用 Rust gate 和正式 AArch64 AxBuild通过。

允许结论仅为“该 revision、输入和一次会话下的 300 秒 Linux+Zephyr 双 Guest 共存及可归属 console/资源观察”。

## 9. P2-SOAK-01：复用 collector 完成 30 分钟

### 9.1 实现方式

不得复制 `run_dual_guest_soak.py`。在同一 `run_dual_guest_smoke.py` 中把观察窗口参数化：

- `300` 秒：short smoke，产生 `dual-guest-session.json`；
- `>=1800` 秒：soak，另产生现有 validator 所需的 `dual-guest-soak-session.json`；
- 两种模式共享输入解析、QMP identity、DTB capture、console demux、错误分类、cleanup 和 status publisher；
- soak 每 10 秒采集一次 Host/Guest health 索引，但不靠采样行替代原始连续 log；
- start/end 使用同一 Host monotonic clock。wall clock 只用于 run ID。

### 9.2 现有 soak schema

输入 `dual-guest-soak-session.json` 必须保持现有 validator 的 exact-key v1 schema：

- `schemaVersion=1`；
- `artifactStatus=capture-generated-unreviewed`；
- `status=dual_guest_soak_session_completed`；
- `proofScope=one-identity-bound-qemu-dual-guest-1800-second-coexistence-session`；
- identity 含 `bootId/qemuPid/qemuStartMonotonicNs/qemuName/sessionNonce`，name 为 `axvisor-dual-soak-<nonce>`；
- `cpuSets` 必须等于从传入 TOML 解析的 Linux `[0,1]` 和 Zephyr `[2]`；
- `endMonotonicNs-startMonotonicNs >= 1_800_000_000_000`；
- guests 严格按 VM1 Linux、VM2 Zephyr 顺序，每项绑定 TOML basename/size/hash、DTB marker 和 READY；
- healthMarkers 只有同 identity 的 start/end；
- `events=[]` 表示稳定窗口内没有 restart、exit、panic、unsafe 或未分类事件。

`TEST-007` 要求的有界停止/销毁发生在 end health marker 之后，写入外层 `status.json.lifecycle` 和 `cleanup`，不能塞进当前 schema 的 `events` 后再声称 validator 可通过。外层 terminal status 成功为 `success=true`、`status=dual_guest_soak_completed`，运行失败为 `success=false`、`status=dual_guest_soak_failed`，前置阻塞为 `success=false`、`status=dual_guest_soak_blocked` 并提供 `blockedReason`；内层 `dual-guest-soak-session.json.status=dual_guest_soak_session_completed` 保持不变。若未来要求“窗口内重启并继续计时”，必须先升级 schema/validator/test，不得重解释 v1。

### 9.3 执行和复核

目标运行命令与 short smoke相同，只改：

```bash
--duration-seconds 1800
```

runner 成功并完成 cleanup 后，使用不同输出路径独立复核：

```bash
python3 scripts/test/check_axvisor_dual_guest_soak_session.py

python3 scripts/contest/validate_dual_guest_soak_session.py \
  --session <run>/live/dual-guest-soak-session.json \
  --linux-vm-config <run>/inputs/linux.resolved.toml \
  --zephyr-vm-config <run>/inputs/zephyr.resolved.toml \
  --output <run>/review/dual-guest-soak-result.json
```

validator 成功的唯一结果 status 是 `dual_guest_30min_coexistence_observed`。它仍须与 runner status、raw logs、manifest、lifecycle/cleanup 记录一起审查；单独构造一份可通过的 JSON 仍只有静态 validator 证据。

### 9.4 P2-SOAK 完成定义

1. `P2-DUAL-01` 已在同一代码/输入世代关闭；
2. 同一真实 QEMU identity 的 monotonic 窗口不少于 1,800 秒；
3. 两 Guest marker/console 连续且可归属，panic、unsafe、unexpected restart/exit、resource overlap、cross-byte、drop、DMA attempt、unclassified event 均为 0；
4. 两 VM config、镜像、final DTB/DTS、raw log 和 session 逐字节绑定；
5. end marker 后完成至少一次有界整体停止/销毁，所有 owned 资源无残留；
6. 离线 validator exit 0，输出为 `dual_guest_30min_coexistence_observed`；
7. `success=true`、`status=dual_guest_soak_completed` 与 manifest/checksums 最后闭合，运行命令和 exit code 可复核。

允许结论仅为“该 revision 和会话下至少 1,800 秒的 Linux+Zephyr 双 Guest 共存与有界生命周期观察”。

## 10. 每个 P2 运行统一保留什么

即使公共 `IF-011` publisher 尚未实现，runner 也不得少于以下信息：

| 类别 | 必填内容 |
|---|---|
| source | full commit、branch、dirty status、bounded binary patch hash |
| environment | Windows/WSL/Ubuntu、kernel、QEMU、Rust/Cargo、compiler、dtc/debugfs 版本 |
| inputs | 每个 TOML、DTB、kernel、image、rootfs、plan 的路径、size、SHA-256 |
| identity | run ID、nonce、boot-id、QEMU name、PID、start time、boot ID、UID、exe/cmdline、QMP peer |
| commands | argv 数组、cwd、开始/结束 UTC 与 monotonic、exit code；敏感值脱敏 |
| raw | AxVisor log、QMP JSONL、每 VM console、Guest DTB/DTS、必要 capture |
| derived | session、semantic reports、result、cleanup/lifecycle、summary |
| publication | manifest/checksums；terminal status 最后写入 |
| boundary | `proofScope` 与逐条 `doesNotProve` |

若目录包含大文件并保持 ignored，至少要在可提交的 evidence index 中登记不可变归档位置和 hash；不能只在 Markdown 声称“已运行”。

## 11. 失败分类和安全清理

统一错误分类：

| 类别 | 示例 | 是否可重试 |
|---|---|---|
| input | hash 漂移、占位路径、blocked config、重复 nonce | 修输入后用新 run |
| build | AxBuild/Zephyr/helper 编译失败 | 修确定性问题后用新 run |
| launch | QEMU 未启动、pidfile/QMP 未出现 | 查 primary；新 run |
| identity | SO_PEERCRED/name/PID/start-time 不一致 | 安全失败，不连接未知 QEMU |
| guest | 缺 READY、panic、init failure、CPU 数错误 | 保留 raw；不能归为 Host/DMA 结论 |
| evidence | DTB/capture/hash/schema/marker 不一致 | 该 run 失败，禁止补造 |
| cleanup | owned process/socket/runtime 残留 | 与 primary 分开记录；先验证所有权再有界清理 |

清理只能作用于由本 runner 创建并由 nonce、QEMU name、PID/start time、process group 与 runtime path 同时绑定的对象。禁止用模糊 `pkill qemu`、全局递归删除 `/tmp` 或删除整个 results 目录。

## 12. 阶段 P2 关闭清单

开发者只有在以下各项都有当前源码证据时，才能把阶段 P2 标为完成：

- [x] `TEST-001` dual 静态配置在 run-ready no-data-plane 合同下通过；只达到 `L1 static`；
- [x] `TEST-002/003` 的 Linux SMP2、Zephyr 单 Guest前置已有 `L4 single-Guest` 证据；
- [x] `TEST-004` r23 只读前置仍按原 scope 登记；
- [x] `TEST-005` 已由 fresh r24j 完整通过，r24d/r24e 状态未改；
- [x] `X-CONSOLE-001` 的 Linux 单 Guest动态 gate 已通过，但 `/dev/kmsg`→earlycon 偏差仍须在 dual 口径中显式处理；
- [ ] `TEST-006` 同一 identity 的 300 秒双 Guest short smoke 通过；
- [ ] `TEST-007` 同一 collector 的 >=1800 秒 soak、离线复核和 cleanup 通过；
- [ ] 所有适用 Python/Rust/AxBuild 门禁通过，命令与 exit code 已保存；
- [ ] 每次失败包不可覆盖，成功包 status-last、hash 完整、无残留；
- [ ] `requirements.md`、`test-matrix.md`、`traceability.md`、`deliverables.md` 以及工作区 `现状.md`/`阻塞.md`/`开发交接.md` 同步更新；
- [ ] 对外措辞没有把 P2 扩大为 DMA isolation、IP、AI、实时改善或原始任务全部完成。

关闭后唯一下一主线是 P4 mediated VirtIO-net/IP。P3 只读测量工具可以并行，但 production 实时改造和 A/B 结论仍须遵守全局固定顺序。

## 13. 非结论

阶段 P2 全部关闭后仍然不能声称：硬件 DMA 或通用 DMA isolation 已证明、IOMMU/SMMU 已实现、Linux 与 Zephyr 已建立 IP 通信、ICPC 已在 Guest 间运行、AI 控制闭环已完成、生产实时性已经改善，或最初两份申报材料中的所有任务已经完成。上述结论必须分别由 P4、P5、P3、P6 和 P7 的对应测试与证据关闭。
