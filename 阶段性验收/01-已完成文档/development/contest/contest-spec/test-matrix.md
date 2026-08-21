# 验收测试矩阵

最后更新：2026-08-20（历史审计标签：2026-08-14）

本文把两份申报材料中的验收口径落实为可执行的 `TEST-*`。它是判定 `REQ-*` 能否从 `implemented` 升级为 `verified` 的唯一测试清单；某个文件、静态 TOML、host 测试或历史单 Guest run 存在，都不能替代本表要求的更高层运行场景。

## 1. 状态与证据等级

状态只使用：

- `verified`：本行规定的环境、oracle、工件和复核均已满足；
- `partial`：仅有较低层前置证据，尚未达到本行 oracle；
- `failed-attempt`：真实执行失败，失败包保留且不能计为通过；
- `blocked`：依赖、决策或安全门禁尚未满足；
- `planned`：尚未实现 runner 或尚未执行。

证据等级用于防止越级表述：

| 等级 | 含义 |
|---|---|
| `L1 static` | 文档、配置、解析器或静态合同 |
| `L2 host` | host 上的单元/协议测试，不含 Guest 网络 |
| `L3 target-build` | 目标架构正式构建通过，尚无 Guest 运行 |
| `L4 single-Guest` | 一个 Guest 的身份绑定运行证据 |
| `L5 dual-Guest` | 同一 QEMU identity 中两个 Guest 同时运行 |
| `L6 stability` | 双 Guest 至少 1,800 秒或规定长稳场景 |
| `L7 Guest-IP` | 两个 Guest 之间真实 TCP/UDP/IP 收发 |
| `L8 AI-loop` | 推理、IP、RTOS 动作和反馈的端到端闭环 |
| `L9 delivery` | 私有比赛仓库、干净 clone、可复算离线交付包与演示复核；禁止 PR |

所有 runtime 测试都必须保存：UTC 起止时间和单调持续时间、Git revision 与 dirty patch、构建环境、输入镜像/配置 SHA-256、唯一 nonce/QEMU identity、原始 stdout/stderr、机器可读 `status.json`、产物清单及 SHA-256、精确复核命令、退出码和残留进程检查。失败也要 `status-last`，不得覆盖或删改为成功包。

## 2. 平台、双 Guest 与 DMA

| TEST-ID | 关联需求/决策 | 环境与操作 | Oracle / 阈值 | 必须工件 | 目标等级 | 当前状态与严格边界 |
|---|---|---|---|---|---|---|
| `TEST-001` | `REQ-PLAT-001`、`REQ-PLAT-002`、`REQ-PLAT-003`、`DEC-001..003`、`DEC-006` | Windows 可跑纯 Python；正式构建在 Ubuntu 24.04 WSL2。执行 `python3 scripts/test/check_axvisor_dual_guest_configs.py`、`check_axvisor_dual_guest_topology.py` 和 `check_ci_paths.py`。 | Linux 必须是 VM1、2 vCPU、pCPU `[0,1]`；Zephyr 必须是 VM2、1 vCPU、pCPU `[2]`；pCPU3 不分配给 Guest；内存、设备、IRQ、启动参数均能从锁定 TOML/FDT 追溯；blocked 配置不得被 runner 当成成功。 | 命令日志、解析后的拓扑 JSON、两份 TOML/outer-QEMU 配置哈希。 | `L1 static` | `partial`：已有静态 dual 配置；不证明任何双 Guest 已启动。`DEC-003` 已把 v1 验收固定为 QEMU AArch64，RISC-V/板卡属于后续增强且不得借本行声称覆盖。 |
| `TEST-002` | `REQ-PLAT-002` | QEMU AArch64 + 当前 AxVisor，使用锁定 Linux SMP2 镜像启动单 Guest，并在 Guest 内挂载 procfs、统计 CPU。 | Guest 看到恰好 2 个 processor；VM/Guest identity、配置、镜像和日志绑定；无 panic、init failure、ITS/LPI unsafe；有界退出且无残留。 | Linux raw log、Guest-DTB/DTS、resource claim、status、checksums、环境与源码快照。 | `L4 single-Guest` | `verified`（前置证据）：已有 latest-upstream Linux SMP2 single-Guest run；它不满足同时运行 RTOS。 |
| `TEST-003` | `REQ-PLAT-001`、`REQ-RT-001` | 原生 Non-secure Zephyr 与 QEMU AArch64 + AxVisor Zephyr 单 Guest，使用锁定 Zephyr v4.4.0/SDK 1.0.1 工件。 | AxVisor 下连续周期 marker 与 PASS 完整；Guest/Host `ICC_CTLR_EL1` 切换不导致第二次 PPI 停滞；无 console 串线、panic 或 unsafe；证据哈希通过且无残留。 | 原生与 AxVisor raw log、`.config`、镜像/配置 hash、frame/demux manifest、status/checksums。 | `L4 single-Guest` | `verified`（前置证据）：r9 与历史原生/AxVisor Zephyr 单 Guest可用；不证明 dual、完整实时统计或 Guest IP。 |
| `TEST-004` | `REQ-QUAL-002`、`REQ-NET-001`、`DEC-006` | 在 r23 绑定的单 Linux 会话中复核 Host DMA guard reservation、allocator exclusion 和配置区 stage-2 marker。 | 仅允许结论：精确 guard `0x180000000 + 0x200000` 被 Host reservation 覆盖、allocator FREE 无重叠，且 marker/DTB/config/QEMU identity/nonce 一致。不得从该 oracle 推导任何设备写入或隔离。 | r23 raw log、Host 报告、联合报告、Guest-DTB、config、capture-chain、checksums。 | `L4 single-Guest` | `verified`，但只是 r23 窄 Host allocator/runtime marker 观察；不是 DMA effect、硬件 DMA、IOMMU/SMMU 或通用隔离。 |
| `TEST-005` | `REQ-QUAL-002`、`REQ-NET-001`、`DEC-006..007` | fresh `r24f+`，按 `scripts/contest/README.md` 的受控 virtio-blk 流程执行唯一 nonce、QEMU/device、Guest GPA、Host HPA 绑定的只读块请求；不得复用 r24d/r24e。唯一 RAM 是 VM1 `MapReserved [0x80000000,0x90000000)`，payload 为 `0x8f000000+512`，独立 guard 为 `0x180000000+0x200000`。 | Host carveout 与 `(vm=1,hpa=0x80000000,size=0x10000000)` 精确匹配；helper `READY -> GO -> DONE` 与 QMP transcript 完整；四份 `before/after-{payload,guard}.bin` 均存在；payload 从 `0xa5` 初值变成期望 sector 字节且 guard 逐字节不变；`session.json`、`result.json` 完整，`status.json` 同时满足 `success=true` 与 `status=virtio_dma_effect_probe_completed`；无残留。`MapAlloc`/`MapIdentical`、跨区、错 device/bus、guard 改变或 stale generation 一律失败。 | request/session/result/status、四份 capture、QMP transcript、raw log、输入与输出 SHA-256、cleanup report。 | `L4 single-Guest` | `verified`：`phase2-virtio-dma-effect-20260813T001837Z-5328870-dirty-r24j` 的 status 为 `success=true`/`virtio_dma_effect_probe_completed`，helper/QMP/四份 capture 完整，payload 命中 expected、guard 不变、无残留；live/review `result.json` SHA-256 一致。只证明该设备/请求/会话的窄字节效果，不批准 P4 网卡、IOMMU/SMMU 或通用 DMA isolation。 |
| `TEST-006` | `REQ-PLAT-001..003`、`DEC-006` | `scripts/contest/run_dual_guest_smoke.py` 在同一 identity-bound QEMU 同时启动 Linux VM1 与 Zephyr VM2，持续 300 秒；本行不启用任何 Guest 数据面设备。 | 两 VM 各唯一 READY、final Guest-DTB 和独立 console；Linux CPU `[0,1]`、Zephyr `[2]`；资源/设备/IRQ 与 TOML 一致；无串线、panic、restart、unclassified exit；bounded cleanup 无残留。 | dual session/status、两 VM raw/demux logs、两份 Guest-DTB/DTS、resource claims、QMP identity、checksums。 | `L5 dual-Guest` | `verified`：`phase2-dual-smoke-20260814T043015Z-ce3713f3a-dirty-r27` 的 status 为 `success=true`/`dual_guest_short_smoke_completed`；两端各唯一 framed READY（boot-id 绑定）、identity-bound QMP capture 完成（两份 final DTB，dtc 静态语义通过）、300 秒稳定窗口 unsafe=0、demux 两 VM seq 连续且 droppedBytes=0/dma=0、哈希一致、无残留进程。前置失败包 `phase2-dual-smoke-20260813T225906Z-3455f62`（READY timeout）与 r25/r26（DrvFS ENODATA、launcher 收尾）保持 immutable failed-attempt。两个已记录边界：(1) SIGINT 后 ostool 因僵尸 QEMU 管道问题使 xtask launcher 不自然退出，runner 以 `launcherEscalated=true` 记录而非失败；(2) 双 Guest 下 Zephyr 虚拟时钟约为墙钟 1/3（Zephyr 内部 seq/uptime 自洽，health 输出墙钟 300 秒内持续无中断），单 Guest r9 无此现象，须在 TEST-007/soak 与 P3 实时测量前诊断。本行不证明 DMA/IP/实时改善。 |
| `TEST-007` | `REQ-PLAT-001`、`REQ-QUAL-001`、`DEC-006` | `TEST-006` 通过后，同一 collector 运行双 Guest 至少 1,800 秒，并覆盖一次有界停止/重启或销毁路径；用 `validate_dual_guest_soak_session.py` 离线复核。 | 真实单调持续时间 `>=1800s`；两 VM marker 连续；panic、非预期 restart、unclassified exit、resource overlap、console misattribution、cleanup residual 均为 0；validator 从 TOML 得到 `[0,1]`/`[2]`，不得硬编码假映射。 | byte-bound `dual-guest-soak-session.json`/result、raw logs、配置与镜像 hashes、生命周期事件、残留检查。 | `L6 stability` | `verified`：`phase2-dual-soak-20260814T072549Z-15df1f65d-dirty-r31` 的 `status.json` 为 `success=true`/`dual_guest_short_smoke_completed`，稳定窗口 1800.0s 且 unsafe=0；`generate_dual_guest_soak_session.py` 从 immutable evidence 重建 `dual-guest-soak-session.json`，`validate_dual_guest_soak_session.py` 输出 `dual_guest_30min_coexistence_observed`（durationNs=1800519845152，cpuSets `[0,1]`/`[2]`，双 READY/DTB marker 绑定 boot-id，QEMU identity/nonce 绑定）；demux 两 VM 连续无掉帧（dropped=0/dma=0），截断帧仅限 shutdown 末尾一帧并记录于 manifest；无残留进程。中间包 r28（前缀不符）与 r29/r30（demux 截断 failed-attempt）immutable 保留。已记录：双 Guest 下 Zephyr 虚拟 timer 中断延迟/积压导致时间基准波动（P3 分段测量前不得作为实时基线）；本行不证明 IP/DMA/实时/AI。 |

## 3. 实时性与压力对比

| TEST-ID | 关联需求 | 环境与操作 | Oracle / 阈值 | 必须工件 | 目标等级 | 当前状态与严格边界 |
|---|---|---|---|---|---|---|
| `TEST-008` | `REQ-RT-001`、`REQ-RT-003` | 原生 Non-secure Zephyr；固定同一镜像、周期、CPU、样本格式，采集周期抖动、调度/唤醒延迟和可测中断响应。 | 至少 3 个独立 run；每 run 达到 100,000 个周期样本或持续 30 分钟两者中更严格者。原始样本无静默丢弃；分别给出 `n`、mean、max、P99、P99.9、miss count 和异常样本。历史 10 个 110 ms 周期样本只算 smoke。 | raw CSV/JSON、环境/镜像 hash、采集器版本、统计 JSON/CSV、重算命令。 | `L4 single-Guest` | `partial`：有 10 样本原生 smoke，无规范要求的样本量、3-run 重复或完整统计数据集。 |
| `TEST-009` | `REQ-RT-001..003` | AxVisor + Zephyr，依次跑空载、Linux CPU 压力、网络压力、AI 压力和组合压力；每组与 `TEST-008` 使用同一测量定义，并运行优化关闭/开启 A/B。 | 每个场景至少 3 个独立 run，每 run 达到 100,000 个周期或 30 分钟两者中更严格者；报告 mean/max/P99/P99.9、miss count。A/B 输入、镜像和负载除被测开关外相同；预先选定的 P99.9 或 max 在 3 组配对 run 中方向一致改善，其他主指标无未解释显著回退。 | 每场景 raw samples、压力生成日志、A/B config、统计与置信/重复性说明、源码 diff、checksums。 | `L6 stability` | `planned`：host 事件 schema、统计器、六场景 profile/matrix 和 native probe 合同已实现并通过检查；尚无 runtime raw samples、三组配对 run 或生产 A/B 改造。 |
| `TEST-010` | `REQ-RT-002`、`REQ-RT-003` | 先按同钟 timestamp 把 timer expiry→IRQ enqueue→vCPU wake→re-entry→Guest handler 分段；仅对在 3 组配对基线中均贡献总 P99.9 `>=15%` 或绝对 `>=20 us` 的前 1–2 条路径做可关闭改动，再运行最低层 Rust test、strict Clippy、rustfmt、正式 AArch64 AxBuild 和 `TEST-009`。 | 选择报告可从 raw timestamps 重算且没有事后换指标；每个改动有独立开关或清晰对照提交；功能/安全合同全绿；构建成功本身不算实时改善；只有 `TEST-009` 数据满足冻结阈值才通过。若无路径达到准入门槛，正确结果是发布基线/无优化结论，而不是改低门槛。 | 分段 raw timestamps、选择报告、测试/构建日志、工具链版本、源码 hash/diff、A/B 配置和证据链接。 | `L6 stability` | `planned`：候选选择工具/合同已实现；没有真实 raw timestamps 或三组基线可供选择。旧 Zephyr EOImode 修复是功能正确性证据，不等于实时优化 A/B。 |

## 4. 真实 Guest IP 与 ICPC

| TEST-ID | 关联需求/决策 | 环境与操作 | Oracle / 阈值 | 必须工件 | 目标等级 | 当前状态与严格边界 |
|---|---|---|---|---|---|---|
| `TEST-011` | `REQ-NET-001`、`REQ-NET-002`、`DEC-007`、`DEC-008`、`DEC-010` | P4-UPSYNC-02/P4-EVID-01 关闭后，双 Guest 使用官方 axvirtio/device-graph/DeviceContext/DmaGrant 路径中的两个 mediated frontend 和有界 `vnet0`；outer-QEMU 网卡、slot2 passthrough、TAP/bridge/NAT 均关闭；加载冻结 MAC/IPv4。 | 先通过唯一 backend、issuing-vCPU/descriptor/DmaGrant/跨 VM/stale-generation/queue-overflow 负例和 AArch64 build；两 Guest 各只枚举自己的 NIC/MAC；Linux `10.77.0.1/24`、Zephyr `10.77.0.2/24`，无默认路由；ARP 成功；每方向 100 个 ICMP echo，丢包 0、地址/MAC/路由偏差 0。 | upstream/base/head 与迁移 manifest、两端 `ip addr/route` 或 Zephyr 等价输出、ping 原始输出、最终 Guest-DTB、resource claims、内部 frame capture/导出 PCAP、metrics、hash/status；console 归属单独报告。 | `L7 Guest-IP` | `runtime-smoke-observed / qualification pending`：旧 HEAD 已观察 Linux 主动 100/100 ICMP 及双向 frame；但不是“每方向主动 100”，且 runner/profile/capture/metrics 未满足本行工件。须在 21ef 前移后的新 HEAD 以 Guest-runtime v2 fresh run。 |
| `TEST-012` | `REQ-NET-001..004`、`DEC-005`、`DEC-008` | `TEST-011` 后每方向发送 10,000 个 256-byte UDP echo，固定 100 packets/s；再分别用固定 seed 注入每 10 包丢 1 包、每 17 包复制 1 包、每 23 包交换相邻顺序、每 29 包损坏 1 包。 | 无故障档发送/接收一一对应、丢包 0、silent corruption 0；故障档的 injected/observed drop、duplicate、reorder、CRC/内容错误逐包对账且非注入异常为 0；成功率、超时、重传率、同端 RTT 和两种吞吐口径全部可复算。 | 两端 raw logs、内部 PCAP/frame capture、fault seed/profile、metrics JSON/CSV、status/checksums。 | `L7 Guest-IP` | `runtime-smoke-observed / qualification pending`：旧 HEAD 有 Linux→Zephyr→Linux UDP echo 100/100；尚无每方向 10,000、固定 fault、PCAP/counters/metrics 和新 HEAD 复验。100 包 smoke 不能关闭本行。 |
| `TEST-013` | `REQ-NET-001`、`REQ-NET-004`、`DEC-005`、`DEC-008` | 在同一双 Guest拓扑以 TCP `46001` 运行 10,000 个 ICPC-length framed message；固定注入 1-byte partial reads、每 31 帧合并 read、连接中断、端点重启和一次半帧 EOF。 | 正常帧一一对应且无错位；半帧 EOF 必须拒绝；断开在 500 ms 内显式暴露；重连必须使用新非零 session，旧控制执行数为 0；UDP session 已停止且不存在双 actuator owner。 | 两端 logs、内部 PCAP/frame capture、fault timeline、metrics、session/status/checksums。 | `L7 Guest-IP` | `runtime-smoke-observed / qualification pending`：旧 HEAD 只观察 TCP 连接建立 marker；未传 10,000 framed messages，也未覆盖 partial/merged read、EOF、disconnect/restart、唯一 owner 或完整工件。 |
| `TEST-014` | `REQ-NET-003`、`REQ-NET-004`、`DEC-004`、`DEC-005` | host 上执行 `python3 scripts/test/check_icpc_protocol.py`，编译运行 portable C99 ICPC v1 测试。 | 输出 `ICPC_PROTOCOL_PASS`；36-byte 网络序头、CRC32C、5 类消息、失败关闭、RFC 1982 序号、64 包窗口、ACK 与 100/200/400 ms、最多 3 次重传合同全绿。 | 编译命令/版本、测试 stdout/stderr/exit code、`icpc.c/.h` 与测试向量 SHA-256。 | `L2 host` | `verified`，但仅 host 协议编解码与状态机；`DEC-004/005` 已冻结 v1 设计，仍不证明 Guest、VirtIO-net、UDP/IP、丢包恢复或端到端时延。 |
| `TEST-015` | `REQ-NET-003..004`、`DEC-004`、`DEC-005`、`DEC-008` | 在 `TEST-012` 的真实 Guest UDP `46000` 上运行固定 24/32/12-byte CONTROL/STATUS/ERROR 以及 ACK/HEARTBEAT；100 秒内以 10 Hz 发起 1,000 个 CONTROL，并覆盖丢包、重复、乱序、CRC 错误、版本/长度错误、session 重启和超时。 | 合法消息两端字段逐项一致；每个逻辑 CONTROL 要么恰应用一次并收到关联 STATUS，要么按 `t=0/100/300 ms` 共 3 次发送、在 500 ms validity 到期时显式取消并进入回退；不得执行通用可靠消息的 `t=700 ms` 第三次重传。重复执行、非法执行、未知状态和 silent loss 都为 0；session 变化清空旧窗口。 | 两端 ICPC logs、PCAP/frame capture、输入向量/fault seed、应用动作计数、metrics/status/checksums。 | `L7 Guest-IP` | `runtime-smoke-observed / qualification pending`：旧 HEAD 已观察 Guest CONTROL→ACK+STATUS，但最新为 `sent=100 verified=85 loss=15`，无 1,000 CONTROL/fault/restart/exactly-once/cancel/PCAP/metrics。当前 `guest_network_completed` 不能关闭本行。 |

## 5. AI 闭环、综合验证与交付

| TEST-ID | 关联需求 | 环境与操作 | Oracle / 阈值 | 必须工件 | 目标等级 | 当前状态与严格边界 |
|---|---|---|---|---|---|---|
| `TEST-016` | `REQ-AI-001` | Linux host/Guest 用固定 seed、规范化和脚本训练/导出 `3→8→1` float32 MLP；输入为 `{measured_mC,target_mC,previous_duty_q16_16}`，hidden=ReLU，output=sigmoid，并在同一组 golden vectors 上交叉校验 Python 与部署 C 推理。 | 单线程 CPU 固定 seed 重跑导出得到相同 canonical model bytes/SHA-256；模型 metadata 固定缩放、层、激活和版本；所有 golden vector 的部署结果与参考结果相差不超过 2 个 Q16.16 duty LSB；模型包含实际两层矩阵乘法且不是规则/查表。 | 数据生成脚本、训练/导出命令、原始数据 hash、模型、metadata、golden vectors、测试报告。 | `L3 target-build` | `partial`：host 数据/模型/训练/导出/验证工具及其合同已实现；尚无冻结 canonical model、模型卡、部署 C/Linux Guest 应用、target build 或 Guest golden-vector 结果，因此本测试未通过。 |
| `TEST-017` | `REQ-AI-001`、`REQ-AI-002`、`REQ-AI-003` | 按 `IF-008..010` 固定一阶温度 plant，初值/环境 25,000 mC、目标 55,000 mC、100 ms tick、180 s；60–90 s 注入 `-150 mC/tick` 负载。固定 PI 与 MLP 分别使用 seeds `7/19/43` 运行完整 Linux inference → Guest IP → Zephyr action → STATUS feedback。 | 每个 command 能关联 inference/input/request/RTOS action/feedback；必报 RMSE、IAE、settling time、overshoot 和同端闭环 RTT。结果改善、不变或回退均须如实报告；只有至少 RMSE/IAE 均不劣且其中一项改善、其他安全/实时指标无未解释回退时，才能额外宣称“智能优化有效”。跨 Guest 单向延迟无时钟同步时禁算。 | 两组 raw trajectories、模型 hash、ICPC logs/PCAP、指标 JSON/CSV、重算脚本、对比图、status/checksums。 | `L8 AI-loop` | `blocked`：host plant、PI、mailbox/watchdog 与 metrics 合同已实现；仍依赖真实 Guest IP、冻结模型和两端 Guest 应用，没有闭环 runtime。 |
| `TEST-018` | `REQ-AI-002..003`、`REQ-QUAL-002` | 在闭环中注入控制超范围、过期/重复/损坏消息、Linux/网络停止和端点重启。 | Zephyr 对非法控制不执行；自最近一次有效新 CONTROL 起 500 ms 内无更新时，最迟下一个 100 ms tick 将 duty 归零并上报 `SAFE|NETWORK_TIMEOUT`；恢复只接受新 session/更新 sequence；故障不得导致 panic、任务饿死或失控输出。 | fault manifest/seed、两端 raw logs、动作轨迹、error/status 消息、恢复时间、checksums。 | `L8 AI-loop` | `blocked`：host mailbox/watchdog/CONTROL-plant 合同已实现；Zephyr Guest 集成、真实网络故障与 safe-state runtime 尚未开始。 |
| `TEST-019` | `REQ-QUAL-001..002` | P2–P5 均 verified 后，运行数小时 CPU + network + AI 组合压力，并重复资源隔离/协议负例。 | 预先登记持续时间和负载；panic、unclassified restart、resource overlap、silent corruption、cleanup residual 为 0；完整报告 max/P99/P99.9、miss、网络成功/超时/吞吐、控制指标；所有摘要可从 raw 数据重算。 | qualification scenario manifest、raw logs/CSV/JSON/pcap、统计/图、environment/status/checksums。 | `L8 AI-loop` / `L6 stability` | `planned`：30 分钟 dual soak、Guest IP、实时 A/B 与 AI 均未完成，不能开始正式综合验收。 |
| `TEST-020` | `REQ-DEL-001`、`REQ-DEL-003` | 在无工作区缓存/ignored evidence 的干净 clone 按复现文档从依赖校验开始，构建并运行可公开的最低完整演示；另运行 CI 与全部静态合同。 | 文档中每条命令可直接执行；依赖版本和下载 hash 锁定；CI 全绿；结果只引用随交付可访问的摘要/工件；第二位复核者或 maintainer 记录命令、exit code 和差异。 | clean-clone transcript、环境 manifest、CI URL/log、输出 checksums、review record。 | `L9 delivery` | `blocked`：当前 worktree dirty，ignored 本地 evidence 不能供 clean clone 访问，尚无最终复现演练。 |
| `TEST-021` | `REQ-DEL-001..003` | 按 `deliverables.md` 清单做逐项 hash/命令/reviewer 审计，播放演示并从图表反查 raw 数据。 | 所有必需 `DEL-*` 状态为 `delivered`；不存在悬空本地路径、缺 hash、不可重算数字或越级文案；视频依次展示双 Guest、Guest IP、AI 动作/反馈、实时对比与故障恢复。 | 最终 manifest、review checklist、视频及 hash、公开证据索引。 | `L9 delivery` | `planned`：当前只能审计规范，不能审计最终交付包。 |
| `TEST-022` | `REQ-DEL-002` | 确认 `qcl-kernel/tgoskits-liangce` 保留官方完整历史和开发分支，锁定只读 upstream `dev` base SHA，把可审查提交导出为 `git format-patch` 离线包；在 clean clone 上执行 `git am`、CI、license 和人工 review。全程禁止创建或提交 PR，也禁止直接推送 upstream。 | 比赛仓库 refs、commit/patch 顺序、base/head SHA 与每份 patch SHA-256 完整；clean clone 应用无冲突；Apache-compatible 许可与第三方声明齐全；required CI 通过；review 结论与未完成限制写入离线 manifest；远端 PR/MR 记录必须为 `not-created-by-policy`。 | repository URL/refs、commit list、base/head SHA、patch 文件与 checksums、`git am` transcript、CI/review/license 记录、禁止 PR 声明。 | `L9 delivery` | `planned`：目标仓库已创建且为空；尚未 mirror 官方历史、拆分 dirty tree、push 开发分支、生成离线补丁包或完成 clean-clone 应用验证；PR 明确禁止。 |

## 6. 执行与判定规则

1. `partial`、`failed-attempt`、`blocked` 和 `planned` 都不得被写成“通过”。
2. 后置测试不能替前置测试补结论：例如 `TEST-014` 通过不能关闭 `TEST-011..013/015`，`TEST-004` 或未来 `TEST-005` 通过不能自动批准 Zephyr VirtIO-net。
3. 原材料未给出数值阈值的项目，必须在首次正式运行前通过相应 `DEC-*` 或新决策冻结；先看结果再挑阈值的测试无效。
4. WSL2/QEMU 结果必须标注宿主调度噪声；无跨 Guest 时钟同步时只计算同端 RTT/闭环时延，不发布单向延迟。
5. 每次通过或失败后同步 `traceability.md`、工作区 `现状.md`/`阻塞.md` 和 `deliverables.md`，但永不改写历史 evidence。
