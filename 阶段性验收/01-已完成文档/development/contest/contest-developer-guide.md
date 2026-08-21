# AxVisor Linux/Zephyr 竞赛项目开发者指南

最后更新：2026-08-20

> 这是竞赛开发执行入口。两份原始申报材料的规范化需求、架构、接口、测试和最终交付以 [`contest-spec/README.md`](contest-spec/README.md) 为准；当前一次执行和本地证据位置以 `development/current/开发交接.md` 为准，长命令与证据格式以 `tgoskits-upstream-integration/scripts/contest/README.md` 为准。仓库内同名文件是本权威版本的交付镜像。

## 2026-08-20 当前入口

- 唯一开发工作树：`F:\project\泉城实验室\tgoskits-upstream-integration`；旧 `tgoskits/` 只作历史/evidence/迁移来源。
- 最近生产代码 checkpoint `574d569be...`；其后可有 docs-only 提交。已 fetch 官方
  `upstream/dev=21ef4b218...`；相对代码 checkpoint ahead 15/behind 39，当前数以 Git 命令为准。
- 当前工作包：`P4-UPSYNC-02`，随后是 `P4-EVID-01` 与 `P4-SMOKE-02`。精确任务卡见
  `development/current/开发交接.md`，依据见
  `development/analysis/项目重新审视与执行路线-2026-08-20.md`。
- 当前 P4 ICMP/UDP/TCP/ICPC 是真实双 Guest **窄冒烟**，不是完整 TEST-011..015：runner 只验证
  marker+cleanup，profile 仍是 host-only，capture/metrics 未达到资格证据。
- P5 模型/Guest 应用可与 P4 reliability 并行开发，但 P5 qualification 依赖 P4-REL-01；P3
  生产 A/B 在最终网络/AI 路径冻结后重采。
- 官方 #2092 的 issuing-vCPU `DeviceContext`、#2105 CI manifest v3 和 #2106 host DMA
  coherency必须纳入适配；后者不替代 Guest-memory `DmaGrant`，也不证明 DMA isolation。

## 1. 先判断自己该看哪份文档

| 问题 | 唯一权威文档 | 更新要求 |
|---|---|---|
| 最初两份 Word 承诺了什么，怎样追到设计、测试和交付？ | `development/contest/contest-spec/README.md` 与 `traceability.md` | 来源、需求、范围决策或验收变化时更新 |
| 我现在应该接哪个任务？ | `development/current/开发交接.md` | 每次中断、失败或交接前更新 |
| 项目为什么这样分阶段、依赖顺序是什么？ | `development/current/计划.md` | 工作包状态或依赖变化时更新 |
| 哪些事实已经有证据，哪些仍未完成？ | `development/current/现状.md` | 新证据包或失败审计产生后更新 |
| 当前被什么条件阻塞、由谁解除？ | `development/current/阻塞.md` | 阻塞出现、缓解或解除时更新 |
| 代码在哪、一个工作包怎样开发？ | 本文 | 模块边界、入口或完成定义变化时更新 |
| P2、P3、P4、P5 的逐文件实现步骤是什么？ | `contest-stage-p2-platform.md`、`contest-stage-p3-realtime.md`、`contest-stage-p4-network.md`、`contest-stage-p5-ai-control.md` | 阶段接口、实现顺序、命令或 DoD 变化时更新 |
| 脚本的完整参数和证据格式是什么？ | `scripts/contest/README.md` | runner/validator CLI 变化时更新 |
| 本地证据怎样登记？ | `results/baseline/README.md` | 证据等级或索引格式变化时更新 |

不要在多份文档复制完整运行命令。本文给出入口和最小门禁，`scripts/contest/README.md` 保存完整命令，`开发交接.md` 只保存当前一次执行所需的实参和输出目录。

## 2. 目标、主线和非目标

主线平台固定为 `QEMU AArch64 + AxVisor + Linux（至少 2 vCPU）+ Zephyr（1 vCPU）`。最终交付必须同时具备：

1. Linux 与 Zephyr 双 Guest 稳定共存；
2. 以 VirtIO-net 和 TCP/UDP/IP 为主数据通道；
3. Linux 中执行轻量 MLP 推理，Zephyr 执行控制动作并回传状态；
4. 有改造前后的实时性、压力与隔离数据；
5. 从干净环境可复现，并形成可审查的提交、文档和演示。

以下内容不能替代主线：共享内存、HyperCall、裸 MMIO 或 vsock 不能替代 IP；host 侧 ICPC 测试不能替代 Guest 网络；单 Guest 不能替代双 Guest；一次模拟设备字节观察不能替代 IOMMU/SMMU 或通用 DMA 隔离。

### 2.1 原申报与当前工程基线

| 申报允许或优先的路线 | 当前工程基线 | 决策状态 | 开发者边界 |
|---|---|---|---|
| Linux 或 StarryOS 智能 Guest | Linux-only v1 | `DEC-001`，`adopted-for-v1-design`；未实现 | Linux 证据不等于 StarryOS 证据；对外范围变化仍需负责人签字 |
| RT-Thread 优先，Zephyr/FreeRTOS 可选 | Zephyr-only v1 | `DEC-002`，`adopted-for-v1-design`；未实现 | 不得声称已经覆盖 RT-Thread/FreeRTOS；对外范围变化仍需负责人签字 |
| QEMU AArch64/RISC-V，开发板增强 | QEMU AArch64-only v1 | `DEC-003`，`adopted-for-v1-design`；未实现 | RISC-V/开发板不进入 v1，且不得声称已完成 |
| 申报名称 `ICCP` | 公开首次写 `ICCP（仓库实现名 ICPC v1）`；wire 保持 36-byte `ICPC v1` | `DEC-004`，`adopted-for-v1-design`；Guest 集成未实现 | host 合同不等于 Guest IP，也不等于 ICCP 逐字节布局 |
| TCP 或 UDP/IP | UDP:46000 + ACK/100/200/400 ms/通用消息最多 3 次重传；500 ms CONTROL 只在 t=0/100/300 ms 发送；TCP:46001 仅新 session 显式回退 | `DEC-005`，`adopted-for-v1-design`；未实现 | UDP/TCP 不得同时控制 actuator，过期 CONTROL 不得在 t=700 ms 重发 |

完整理由、替代方案、影响和批准字段见 `contest-spec/sources-and-decisions.md`。本轮用户已授权以上 v1 设计作为工程执行输入；它们不是已实现或已验证结论，且改变申报优先级时最终对外材料仍需项目负责人签字确认。

`DEC-009` 将 `2026-08-20 18:00 Asia/Shanghai` 定义为内部可交付快照冻结点，不是官方截止。官方平台的精确时刻仍须行政核验；这不阻塞代码开发，但阻塞最终上传、提交或“已提交”对外表述。完整原始需求不因时间不足而删除，未完成项必须如实保留。

## 3. 五分钟接手

### 3.1 固定工作区和读取顺序

在 Windows PowerShell 中：

```powershell
Set-Location '<workspace>\tgoskits-upstream-integration'
git status --short --branch
git rev-parse HEAD
git rev-parse upstream/dev
Get-Content ..\development\current\开发交接.md
Get-Content ..\development\current\阻塞.md -TotalCount 180
```

随后阅读本文件中当前工作包，再打开相应源码。工作树长期为 dirty，现有修改属于项目现场；不要执行 `git reset --hard`、不要覆盖 ignored `results/baseline/runs/` 证据、不要删除安全 stash。

进入 Rust 源码修改前，完整阅读 `AGENTS.md` 与 `docs/guideline/code-quality.md`。新增或扩大平台、硬件、公共接口或用户可见能力时，还要完整阅读 `docs/guideline/feature-development.md`。使用仓库规定的 Rust nightly 和 `cargo xtask` 入口。

### 3.2 环境和最小静态门禁

正式 AArch64 构建与 QEMU 在 Ubuntu 24.04 WSL2 中运行：

```bash
cd '<workspace>/tgoskits-upstream-integration'
export PATH=/root/.cargo/bin:/opt/aarch64-linux-musl-cross/bin:$PATH
python3 scripts/test/check_ci_paths.py
```

Windows 可以运行纯 Python 合同；Rust/AArch64/QEMU 结论必须记录实际环境。依赖与缓存路径见 `scripts/contest/README.md`。QEMU 或长稳等待时间不等于模型 token；运行日志应保留原件，给模型时只提供必要片段和哈希。

### 3.3 如何选择工作包

只领取状态为“可开始”的最前置工作包。当前顺序是：

```text
P2-DMA-01 / P2-DUAL-01 / P2-SOAK-01（已关闭并冻结）
  -> P4-UPSYNC-02 -> P4-EVID-01 -> P4-SMOKE-02
  -> [P4-REL-01 || P5-AI-A/B] -> P5-AI-C -> P5-AI-Q
  -> P3-TRACE-FEAS-01 -> P3-RT-01 -> P6-QUAL-01 -> P7-DELIVER-01
```

`P3-RT-01` 的同钟只读路径梳理和采集工具可以在 P4/P5 期间并行，但完整 production A/B 必须覆盖冻结的网络和 AI 压力矩阵，因此排在 P5 后；改变调度/中断语义前还必须先有可复现的稳定双 Guest 基线和满足准入阈值的路径数据。任何后置工作包都不能用静态配置假装其运行依赖已满足。

## 4. 模块地图

| 开发内容 | 生产代码/配置 | 主测试或运行入口 |
|---|---|---|
| AxVisor 启动、VM 生命周期、默认 VM | `os/axvisor/src/main.rs`、`manager.rs`、`config.rs` | `cargo xtask build/qemu`、`scripts/contest/run_axvisor_baseline.sh` |
| vCPU 创建、运行和退出分派 | `virtualization/axvm/src/runtime/`、`virtualization/axvm/src/vm/`、`virtualization/arm_vcpu/src/` | 对应 crate test/clippy、AArch64 AxBuild、QEMU runtime |
| GIC、虚拟中断、timer | `virtualization/arm_vgic/src/`、`virtualization/arm_vcpu/src/context_frame.rs` | `arm_vgic`/`arm_vcpu` 定向测试与 Guest 周期测量 |
| VM 内存、固定 HPA、stage-2 | `virtualization/axvm/src/vm/memory.rs`、`layout.rs`、`resource_claim/` | host carveout、stage-2、DMA effect 合同 |
| Host FDT 与 allocator reservation | `platforms/someboot/src/fdt/`、`platforms/*/src/mem.rs`、`os/arceos/modules/axruntime/` | DMA guard Rust 门禁、Host runtime evidence |
| Guest FDT 生成与过滤 | `virtualization/axvm/src/boot/fdt/`、`arch/aarch64/fdt.rs` | Guest-DTB 计划、QMP capture、`dtc` validator |
| VM-local console/PL011 | `virtualization/axdevice/src/pl011*`、`os/axvisor/src/guest_console.rs` | console/demux/Guest-FDT 合同和动态 Guest 日志 |
| Linux/Zephyr 双 Guest 配置 | `os/axvisor/configs/vms/qemu/aarch64/*-dual.toml`、`configs/contest/qemu-aarch64-linux-zephyr-dual.toml` | r27 short smoke + r31 1,800 秒 soak verified；不证明网络 |
| P4 官方网络基线 | `virtualization/axvirtio-common/`、`virtualization/axvirtio-net/`、`os/axvisor/src/virtio_net.rs`（来自锁定 upstream） | P4-UPSYNC-02；#2092 DeviceContext/read-write；官方 demo 仅为 reference |
| 受控 DMA effect | `scripts/contest/*virtio_dma*`、`guest_virtio_blk_odirect_probe.c` | `check_axvisor_*virtio_dma*.py`、fresh runtime evidence |
| IP 应用协议 | `scripts/contest/icpc/`、`development/contest/contest-icpc-protocol.md` | `scripts/test/check_icpc_protocol.py`；当前仅 host 合同 |
| 竞赛 Linux/Zephyr 应用 | `apps/contest/linux-ai-controller/` 与 `apps/contest/zephyr-control/` | P4 network skeleton 已运行；MLP/真实 STATUS/安全闭环待实现 |
| 证据与 CI | `scripts/test/`、`.github/ci/checks/*.toml`、`scripts/test/ci_plan.py`、`results/baseline/` | P4-EVID-01、CI manifest v3、各工作包 validator |

### 4.1 阶段实现手册

本文件只负责跨阶段导航和当前工作包摘要。真正动手前，必须打开对应阶段手册；手册中的“计划路径”仍需由开发者创建，不表示实现已经存在：

| 阶段 | 实现级手册 | 覆盖范围 |
|---|---|---|
| P2 | [`contest-stage-p2-platform.md`](contest-stage-p2-platform.md) | P2-DMA、双 Guest short smoke、1,800 秒 soak、公共 evidence envelope |
| P3 | [`contest-stage-p3-realtime.md`](contest-stage-p3-realtime.md) | 同钟采集、路径筛选、可关闭改造和 production A/B |
| P4 | [`contest-stage-p4-network.md`](contest-stage-p4-network.md) | mediated VirtIO-net、`vnet0`、Guest IPv4、UDP/TCP/ICPC |
| P5 | [`contest-stage-p5-ai-control.md`](contest-stage-p5-ai-control.md) | 一阶 plant、`3→8→1` MLP、动作/回传、安全态和指标 |

截至 2026-08-20，四篇阶段手册已经完成并纳入文档合同。这里的“完成”仅指实现级开发文档完整；P2 runtime 已按窄边界关闭，P4 只有旧 HEAD narrow smoke，P5/P3 的生产结果仍按各手册的完成定义保持未完成。

## 5. 证据等级与发布规则

从低到高依次是：设计/静态合同、host 单元测试、目标架构构建、single-Guest runtime、dual-Guest runtime、30 分钟/长稳、真实 Guest IP、AI 闭环。只能报告实际达到的等级。

- `r23 ≠ DMA isolation`：它只证明 Host DMA guard 被 allocator 排除并出现绑定的 runtime marker。
- `r24d/r24e failed`：它们保持失败审计；r24j 已关闭一次窄效果观察，但不能外推为 DMA isolation、dual 或网络准入。
- `static/single Guest ≠ dual/IP`：外层 VirtIO 拓扑、双 Guest TOML、host ICPC、单 Guest FDT 或 console 均不能证明双 Guest 或 IP。
- 受控 virtio-blk 成功最多可称“该设备、该请求、该会话的字节效果观察”，不能称硬件 DMA 或通用隔离，也不能自动解除 Zephyr VirtIO-net gate。
- `results/baseline/runs/` 是本地 ignored 原始证据；每次运行必须使用新目录、写 status-last、保留失败包并检查残留进程。公开结论还必须有可提交的索引/摘要。

## 6. 工作包执行规范

每个开发者在动代码前，把工作包 ID 写入交接文件，并按以下顺序工作：

1. 读取“先读”和输入工件，复现当前最小合同；
2. 为确定 bug 先写能够在旧实现失败的确定性回归；
3. 只改“修改/新建”范围，跨边界时更新工作包设计；
4. 跑最低层测试、目标架构门禁、必要 runtime；
5. 发布不可覆盖的 evidence，更新 `现状.md`；失败则更新 `阻塞.md`；
6. 在交接中记录命令、exit code、证据路径、哈希、未证明边界和下一步。

### P2-DMA-01：已关闭的一次受控 virtio-blk 字节效果观察

关联需求：`REQ-QUAL-002`，并作为 `REQ-NET-001` 的安全前置；接口编号以 `contest-spec/contracts.md` 为准。

状态：r24j 已 verified；本节保留为结论边界和输入/实现变化后的复验合同，不是当前优先级。

- 已有事实：r24j 完成 nonce/QEMU/device/Guest/GPA/HPA 绑定的 Linux virtio-blk `MapReserved` 请求，四份 host-physical capture 完整，payload 命中 expected、guard 不变、无残留。这只是单设备、单请求、单会话的窄字节效果观察。
- 先读：`scripts/contest/README.md` 第 8 节、`run_virtio_dma_effect_probe.py`、`validate_virtio_dma_effect_probe.py`、r24d/r24e 本地 README。
- 修改/新建：仅在失败由确定性 bug 引起时修改 `scripts/contest/*virtio_dma*`、helper、对应 `scripts/test/check_axvisor_*dma*.py`；每次执行新建 `results/baseline/runs/phase2-virtio-dma-effect-<UTC>-<rev>-dirty-r24f/`，不得复用 r24d/r24e rootfs、plan 或 `live/`。
- 运行前门禁：

```bash
python3 scripts/test/check_axvisor_guest_kernel_virtio_console.py
python3 scripts/test/check_axvisor_guest_virtio_blk_odirect_probe.py
python3 scripts/test/check_axvisor_disposable_virtio_dma_rootfs.py
python3 scripts/test/check_axvisor_prepare_virtio_dma_effect_qemu.py
python3 scripts/test/check_axvisor_prepare_virtio_dma_effect_request.py
python3 scripts/test/check_axvisor_run_virtio_dma_effect_probe.py
python3 scripts/test/check_axvisor_virtio_dma_effect_probe.py
```

- 实机入口：严格按 `scripts/contest/README.md` 第 8 节依次生成 kernel prerequisite、disposable rootfs、QEMU plan 和新 evidence-dir，再运行 `run_virtio_dma_effect_probe.py`；不要手写简化 QEMU 命令。
- 完成定义：`status.json` 同时满足 `success=true` 与 `status=virtio_dma_effect_probe_completed`；`session.json` 与 `result.json` 均存在；四份 `before/after-{payload,guard}.bin`、冻结日志前缀、输入哈希和 cleanup 绑定完整；payload 从 `0xa5` 初值变为 expected sector；guard 不变；无活 QEMU/launcher/runtime 目录残留。
- 失败分类：保留 primary 与 cleanup 两类错误；没有完整成功工件就登记 failed-attempt，绝不从早期 Linux log 推导 DMA 结论。
- 非结论：不证明硬件 DMA、IOMMU/SMMU、通用 DMA isolation、AxVisor mediated VirtIO-net、双 Guest 或 IP；不得据此允许 outer-QEMU slot2 直通。

### P2-DUAL-01：已关闭的双 Guest short-smoke launcher/collector

关联需求：`REQ-PLAT-001`、`REQ-PLAT-002`；接口：`IF-001`、`IF-011`。

状态：r27 verified。runner/collector 已在同一会话取得双 READY/final DTB、300 秒稳定窗口、unsafe=0 和无残留；早期 READY timeout 包保持 immutable。

- 目标：同一 identity-bound QEMU 会话内启动 Linux VM1 与 Zephyr VM2，并把两者的 READY、final Guest-DTB、console 和退出/残留分别归属。
- 固定资源：Linux `phys_cpu_ids=[0,1]`，Zephyr `[2]`，pCPU3 留给 Host/console drain；Linux 与 Zephyr 配置分别为 `linux-smp2-dual.toml`、`zephyr-smp1-dual.toml`。
- 先读：`validate_dual_guest_topology.py`、Guest-DTB capture 脚本、`demux_guest_console_frames.py`、两份 dual TOML 和 `aarch64-dual-guest-*.md` 设计。
- 修改范围：维护 `scripts/contest/run_dual_guest_smoke.py`、对应合同及 READY 生成/解析路径；复用现有 QMP identity/capture 组件，不复制宽松实现。任何新 runtime 使用新目录。
- 最小门禁：

```bash
python3 scripts/test/check_axvisor_dual_guest_configs.py
python3 scripts/test/check_axvisor_dual_guest_topology.py
python3 scripts/test/check_axvisor_guest_dtb_live_capture.py
python3 scripts/test/check_axvisor_guest_console_demux.py
```

- 完成定义：唯一 nonce/QEMU identity；VM1/VM2 各唯一 READY 和 final DTB；Linux/Zephyr 日志无串线；CPU/内存/设备/IRQ 与配置一致；unsafe/panic/init failure 为零；有界 cleanup 且无残留。先以 2–5 分钟 short smoke 成功，再进入 soak。
- 非结论：short smoke 不证明 30 分钟、IP、DMA isolation 或实时改善。

### P2-SOAK-01：已关闭的双 Guest 30 分钟共存与生命周期压力

关联需求：`REQ-PLAT-001`、`REQ-QUAL-001`；接口：`IF-010`、`IF-011`。

状态：r31 verified。真实持续时间 1,800 秒，validator 输出 `dual_guest_30min_coexistence_observed`，unsafe=0、无残留；Zephyr timer 速率偏差转入 P3，不重开 P2。

- 目标：把 P2-DUAL-01 的 collector 扩展为至少 1,800 秒单调时钟会话，并覆盖一次有界停止/重启或销毁路径。
- 先读：`validate_dual_guest_soak_session.py` 及其合同。validator 从 TOML 读取真实 CPU 集 `[0,1]`/`[2]`；不得硬编码另一套映射。
- 修改/新建：扩展 dual runner 生成 byte-bound session；补 raw log、QMP peer、PID/start-time/boot-id、两 VM markers 与资源 claim 的来源绑定。现有 validator 只验证 supplied session，不是 runtime collector。
- 验证：

```bash
python3 scripts/test/check_axvisor_dual_guest_soak_session.py
python3 scripts/contest/validate_dual_guest_soak_session.py \
  --session <run>/dual-guest-soak-session.json \
  --linux-vm-config os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml \
  --zephyr-vm-config os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml \
  --output <run>/dual-guest-soak-result.json
```

- 完成定义：真实持续时间 `>=1800s`；两 VM 无 panic/restart/unclassified exit；marker、身份、配置和原始日志逐字节绑定；cleanup 无残留。之后再加空载、CPU 压力和网络压力样本。
- 非结论：仅共存不证明 IP、实时改善或 AI 闭环。

### P3-RT-01：实时路径基线与 1–2 个可开关改造

关联需求：`REQ-RT-001..003`；接口：`IF-010`、`IF-011`。

状态：路径梳理/测量工具可并行开始；语义改造依赖稳定双 Guest 基线，完整 production A/B 依 `DEC-006` 在 P4 网络和 P5 AI 闭环通过后执行。

- 目标：用同钟事件量化 `Guest timer expiry → virtual IRQ enqueue → owning vCPU wake → vCPU re-entry → Guest handler` 各段，依据冻结准入算法最多选择 1–2 个改造点。
- 先读：`virtualization/axvm/src/runtime/vcpus.rs`、`virtualization/arm_vcpu/src/vcpu.rs`、`context_frame.rs`、`virtualization/arm_vgic/src/vtimer/`、`os/axvisor/src/manager.rs`。
- 修改/新建：维护 `scripts/contest/rt/` 的采集/统计脚本和 `development/contest/contest-realtime-path.md`；生产改动放回对应 virtualization crate，并提供配置开关或可对照提交。
- 测量矩阵：原生 Zephyr、AxVisor 空载、Linux CPU 压力、网络压力、组合压力；每组记录样本数、最大值、P99/P99.9、环境和原始数据。前三个可先用于路径选择；候选路径只有在 3 组配对基线中都贡献总 P99.9 `>=15%` 或绝对 `>=20 us` 才准入，最多取排名前两条。WSL2 结果必须标注宿主调度噪声。
- 申报验收记录必须逐场景保存：原生/AxVisor/AxVisor+改动开关、镜像与配置 SHA-256、运行环境、样本数、单位、mean/max/P99/P99.9、miss 数、压力参数、原始 CSV/JSON 和复算命令。优化结论必须预先指定主指标与改善方向；没有同镜像同配置 A/B 数据只能写“设计预期”。
- 验证：最低层 crate test/clippy/rustfmt、正式 AArch64 AxBuild、相同镜像/配置的 A/B runtime；统计脚本必须能从原始 CSV/JSON 重算表格。
- 完成定义：至少一个由上述门槛选出的可审查生产改动，在重复样本中改善预先指定指标且无功能/安全回归，并能关闭得到基线。若没有路径达到门槛，必须发布“无数据支持改动”的基线结论，P3 保持未完成并复审场景/工期，禁止凭直觉改调度器或降低阈值。

### P4-UPSTREAM-01 / P4-NET-01：Linux/Zephyr mediated vnet0 TCP/UDP/IP 主链路与 ICPC

关联需求：`REQ-NET-001..004`；接口：`IF-002..007`、`IF-011`。

状态：host ICPC v1 完成；Guest VirtIO-net/DMA ownership 与双 Guest runtime 未完成。

- 目标：开发 AxVisor 软件拷贝 mediated VirtIO-net 双端、受检查的 Guest memory context 和有界 L2 `vnet0`，而非复用 outer-QEMU hub0 或 slot2 直通；固定 MAC/IPv4/端口，依次完成 ping、UDP echo、TCP stream，再接入 `ICCP（仓库实现名 ICPC v1）` 控制/状态/错误消息。
- 先读：`configs/contest/qemu-aarch64-linux-zephyr-dual.toml`、dual VM TOML、`scripts/contest/icpc/`、`contest-icpc-protocol.md`、`virtualization/axdevice/` 的设备边界。
- 当前前置：先关闭 `P4-UPSTREAM-01`。生产实现固定采用官方 `axvirtio-common`/`axvirtio-net`、resolved device graph、scoped `DmaGrant` 和 VM wake/IRQ；旧自研 queue/Guest-memory/IrqSink 只作迁移输入，不允许形成第二套 backend。bounded vnet0 L2 只交换两个 mediated 端口；outer-QEMU slot2 直通、fixed identity-RAM 捷径和把 P2 观察描述为 DMA-safe 均禁止。
- 当前事实：r41 是 console sink EL2 fault 的 failed-attempt，没有 Guest-IP；官方两 ArceOS Guest demo 也不是 Linux+Zephyr evidence。迁移后按 NIC→ARP→双向 ICMP→UDP→TCP→ICPC 的顺序验收。
- 修改/新建：设备、memory context 与 bounded L2 放在 AxVisor/virtualization 正确边界；Guest 应用放入 `apps/contest/linux-ai-controller/` 与 `apps/contest/zephyr-control/`；网络 runner/validator 放 `scripts/contest/`，配置继续放 `configs/contest/`，不得把协议业务逻辑塞入 hypervisor。
- 冻结网络值：Linux `02:00:00:00:00:01` / `10.77.0.1/24`，Zephyr `02:00:00:00:00:02` / `10.77.0.2/24`，内部 isolated vnet0、无默认网关/DNS/NAT/bridge/host proxy；两端 UDP `46000`，通用可靠消息 ACK 退避 100/200/400 ms、最多 3 次重传；500 ms CONTROL 只允许 `t=0/100/300 ms` 三次总发送；TCP `46001` 只能在 UDP 明确失败后以新非零 session 显式回退。
- 验证顺序：静态 topology → 双 Guest 枚举独立 NIC → 固定地址 ping → UDP echo → TCP → ICPC 正常流 → 丢包/延迟/重复/乱序/断连恢复。
- 完成定义：同一双 Guest evidence 中具备真实收发、端点身份、包/序号统计、错误恢复和延迟/吞吐；host `check_icpc_protocol.py` 仍只是最低层合同。
- 验收记录字段：run ID 与 QEMU/VM identity、NIC slot/device、MAC、IPv4/掩码、路由、端口、传输和协议版本；发送/接收/ACK/重传/重复/乱序/超时计数；成功率的分母/分子、RTT、有效吞吐、故障注入点与恢复时间；原始日志/pcap/配置/状态哈希及 evidence level。任一字段缺失都不得宣称申报中的 IP 可靠性完成。

### P5-AI-01：轻量 MLP 控制闭环

关联需求：`REQ-AI-001..003`；接口：`IF-005..010`、`IF-011`。

状态：依赖 P4-NET-01。

- 目标：Linux 运行固定 `3→8→1` 小型 MLP，根据一阶温度对象的 `{measured_mC, target_mC, previous_duty_q16_16}` 输出 `duty_q16_16`；Zephyr 每 100 ms 更新 virtual heater duty 并回传状态。自最近有效新 CONTROL 起 500 ms 未刷新时，Zephyr 必须将 duty 归零并上报 `SAFE|NETWORK_TIMEOUT`。
- 修改/新建：Linux 推理与模型工件放 `apps/contest/linux-ai-controller/`；Zephyr 周期控制放 `apps/contest/zephyr-control/`；模型元数据记录输入、输出、归一化、权重 SHA-256 和训练/导出版本。
- 实现顺序：冻结仿真对象与固定参数基线 → 生成可复现数据 → 训练/导出小 MLP → Linux 单元推理 → ICPC 传输 → Zephyr 动作 → 状态回传 → 故障回退。
- 完成定义：一次完整 input→inference→IP→action→feedback 链；至少报告控制误差、稳定时间、端到端延迟中的两项，并与固定参数基线对比；网络断开时 Zephyr 进入明确安全状态。
- AI 闭环验收卡必须固定：plant 方程/参数与扰动、输入特征和归一化、MLP 层形/权重 SHA-256/训练与导出版本、控制动作量化、手动固定参数基线、断网/坏包安全回退、端到端时间戳边界、控制误差与稳定时间定义、至少两项 A/B 结论，以及视频时间戳到 run ID 的绑定。

### P6-QUAL-01：综合压力、隔离负例与可复算数据

关联需求：`REQ-QUAL-001..002`；接口：`IF-010`、`IF-011` 及所有被测运行接口。

状态：依赖 P2-SOAK-01、P3-RT-01、P4-NET-01、P5-AI-01。

- 目标：数小时运行 AI、网络和 CPU 组合压力，验证错误输入、资源边界和恢复。
- 修改/新建：新增 `scripts/contest/qualification/` 场景 runner、故障注入清单和统计器；原始 CSV/JSON 进入 ignored run，提交 schema、摘要和绘图脚本。
- 完成定义：长稳时长、样本数、最大/P99/P99.9、网络成功率/超时/吞吐、控制指标、panic/restart/残留统计完整；非法长度/版本/连接不能破坏 Zephyr 周期任务；所有表格可由脚本重算。

### P7-DELIVER-01：提交、复现和演示

关联需求：`REQ-DEL-001..003`；最终交付索引见 `contest-spec/deliverables.md`。

状态：最后收口，阶段 1 的 dirty-tree 拆分可以提前规划。

- 目标：把实现拆成可审查提交，唯一开发分支使用 `qcl-kernel/tgoskits-liangce:contest/axvisor-ai-control`，兼容基线使用 `rcore-os/tgoskits:dev`（本地 `upstream/dev`）；完成干净环境演练、设计/测试文档和 5 分钟演示。禁止直接推 upstream `dev` 或另建未登记的最终提交分支。
- 修改/新建：更新仓库文档、可提交 evidence 索引、私有比赛仓库提交记录、离线 review manifest、`git format-patch` 灾备包和演示脚本；大镜像与 raw logs 不入 Git，只登记来源、大小、SHA-256 和生成命令。禁止创建或提交 PR，但允许并要求把已审查 commit push 到本组私有比赛仓库。
- 完成定义：CI 通过；私有比赛仓库保留官方完整历史和本组开发分支；离线补丁包在锁定 upstream-dev base 的干净 clone 中无冲突应用并复现；repository/base/head/patch SHA 和 reviewer 记录齐全；远端未创建本项目 PR；演示依次展示双 Guest、IP、AI 控制、实时对比和故障恢复；每项公开说法都能指向对应等级证据。

## 7. 完成一个工作包时必须留下什么

在 `开发交接.md` 记录：工作包 ID、改动文件、精确命令和 exit code、目标架构/运行环境、证据目录、关键 SHA-256、成功/失败状态、尚未证明内容、下一工作包。然后在 `现状.md` 登记事实；若未完成，在 `阻塞.md` 登记 owner、影响、解除条件。不要修改失败包来“修成成功”。

最小提交前检查：

```bash
python3 scripts/test/check_contest_developer_docs.py
python3 scripts/test/check_ci_paths.py
git diff --check
```

Rust 或生产代码工作包还必须按 `AGENTS.md` 完成对应 test、clippy、rustfmt 与正式目标构建；runtime 工作包必须检查进程、socket、pidfile 和临时目录无残留。

## 8. 剩余工作量与模型 token 预算

以下是 2026-08-12 的研发规划区间，不是 API 报价或模型能力承诺。统计口径包括模型读取源码/文档/测试输出与生成分析/补丁/命令/报告，不包括 QEMU、编译、30 分钟和数小时长稳测试的纯等待时间。

| 工作包 | 工程工作量 | GPT 高能力编码代理 token | 主要不确定性 |
|---|---:|---:|---|
| P2-DMA/dual/soak/console 前置 | 16–34 人日 | 0.19–0.39M | QEMU 失败轮数、双 console 与 runner cleanup |
| P3 实时化 | 10–14 人日 | 0.18–0.38M | 测量噪声、是否要改中断/唤醒路径 |
| P4 IP + ICPC Guest 集成 | 7–10 人日 | 0.21–0.45M | Zephyr NIC 路径、丢包/恢复 |
| P5 AI 闭环 | 7–10 人日 | 0.15–0.32M | Guest 构建、模型/协议迭代 |
| P6 综合验证 | 5–7 人日 | 0.12–0.27M | 长稳和组合压力失败诊断 |
| P7 交付及阶段 1 dirty-tree 收口 | 7–13 人日 | 0.07–0.16M | 上游冲突、干净复现 |
| 合计 | 52–88 人日；架构返工时预留 70–110 人日 | 0.92–1.97M；保守中心约 1.45M |

若 QEMU/DMA/网络连续失败，可把 GPT 风险上限预留到约 3.0M token。把长日志先压缩为时间线、错误上下文和哈希，通常可减少 20%–40% 的模型输入。

[DeepSeek 官方模型目录](https://api-docs.deepseek.com/api/list-models)已列出模型 ID `deepseek-v4-flash`，[官方模型页](https://api-docs.deepseek.com/quick_start/pricing/)说明其支持 1M context、thinking/non-thinking 与 tool calls；但没有本项目或同等 AxVisor/Rust/QEMU 工作流的官方等效 benchmark。因此这里只做跨多轮研发的情景估算：按更多澄清、审查和返工回合，建议预留输入 0.95–2.60M、输出 0.32–0.95M，合计约 1.27–3.55M token。这个总量可以跨越多个 1M-context 会话，并不是一次请求的上下文长度。高风险 Rust、虚拟化、DMA 与 evidence 发布仍应由高能力模型或人工复核；不要把这个倍数解释为价格、速度或质量的官方结论。
