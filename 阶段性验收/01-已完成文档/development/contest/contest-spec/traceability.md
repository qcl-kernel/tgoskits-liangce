# 原始承诺到交付的追踪矩阵

最后更新：2026-08-20

本文逐行贯通 `SRC → DEC → REQ → ARC → IF → WP → TEST → EVD → DEL`。表中“最高现有证据”只登记截至 2026-08-17 实际存在的最高层级，不用规划中的文件填空，也不把几个互不绑定的低等级结果拼成一次高等级成功。

## 1. 当前 evidence 登记

`EVD-*` 是本文中的证据定位符，不自动表示证据已公开或测试已通过。`results/baseline/runs/` 默认 ignored；只存在本机的包必须在 `DEL-004` 脱敏发布并校验 hash 后，才能成为 clean clone 可访问的交付证据。

| EVD-ID | 当前事实与定位 | 等级/状态 | 能证明 | 明确不能证明 |
|---|---|---|---|---|
| `EVD-001` | `phase2-linux-smp2-upstream-20260726-r2`，在 `现状.md`/`开发交接.md` 登记为 latest-upstream Linux SMP2 passed、18 项 hash OK。 | `L4 single-Guest`，历史/本地运行证据 | 该绑定版本和配置下 Linux 单 Guest 启动、2 CPU 合同。 | Linux+Zephyr 同时运行、最新未提交改动、DMA、Guest IP、AI。 |
| `EVD-002` | `phase2-zephyr-axvisor-upstream-20260726`，登记为 Zephyr AxVisor passed、10 个 110 ms 样本、66 项 hash OK。 | `L4 single-Guest`，历史/本地运行证据 | 该绑定版本下 Zephyr 单 Guest 周期 smoke 和功能路径。 | 完整实时统计、dual、网络或 AI；10 样本不是 `REQ-RT-003`。 |
| `EVD-003` | `phase2-zephyr-pl011-current-20260805-01e0531-dirty-r9`，222 帧、10 样本/PASS、521 项 SHA-256、无残留。 | `L4 single-Guest`，当前 dirty snapshot 的窄证据 | VM-local Zephyr console frame/demux 与 marker 归属。 | Linux console、dual console、实时改善、IP。 |
| `EVD-004` | `results/baseline/runs/phase2-linux-host-carveout-20260806-01e0531-dirty-r17/live-r23/`；nonce、Host report、联合报告和 marker 在 `results/baseline/README.md` 登记。 | `L4 single-Guest`，`verified` 窄观察 | `0x180000000+0x200000` guard 被 reservation 覆盖且 allocator FREE 无重叠；同会话 Host/Guest/stage-2 marker 绑定。 | 任何设备字节效果、硬件 DMA、IOMMU/SMMU、通用隔离、dual、IP。 |
| `EVD-005` | `.../phase2-virtio-dma-effect-20260810-01e0531-dirty-r24d/`。 | `failed-attempt` | cleanup-survival 失败发生过，runner 必须保留 primary/cleanup 两类错误。 | READY/DONE、GO、QMP、四份 capture、session/result、DMA effect。 |
| `EVD-006` | `.../phase2-virtio-dma-effect-20260810-01e0531-dirty-r24e/`；primary `Errno 61` 加 cleanup survivor。 | `failed-attempt` | Host early markers、Guest-DTB READY、VM boot、stage-2 reserved、Linux early boot以及两类 runner 失败。 | helper READY/DONE、GO、pmemsave、session/result、DMA effect；不能归因为 Guest panic。 |
| `EVD-007` | `scripts/test/check_icpc_protocol.py` 输出 `ICPC_PROTOCOL_PASS`；portable C99 `scripts/contest/icpc/`。 | `L2 host`，`verified` host ICPC 合同 | ICPC v1 host 编解码、CRC32C、消息组合、序号窗口、ACK/重传状态机。 | VirtIO-net、dual Guest、真实 UDP/TCP/IP、Guest 故障恢复或端到端时延。 |
| `EVD-008` | 两份 dual TOML、`configs/contest/qemu-aarch64-linux-zephyr-dual.toml` 及 topology/config Python 合同；outer 配置明确 `status=blocked_dma_console`。 | `L1 static`，历史静态合同 | 计划 CPU/RAM/设备分区以及旧 outer hub/NIC/MAC 唯一性。 | 配置被执行、两 Guest 并发、`DEC-007` mediated frontend/`vnet0`、NIC ownership 或 IPv4 可达；旧 slot2 继续禁止直通。 |
| `EVD-009` | `validate_dual_guest_soak_session.py` 及其合同，从 TOML 读取 `[0,1]`/`[2]`。 | `L1 static`，validator implemented | 给定合法/非法 session 时的离线判定规则。 | 真实 collector、真实 1,800 秒 session 或稳定性。 |
| `EVD-010` | resource-claim、Guest-FDT、Host carveout、stage-2、DMA guard 等现有静态/host 合同与 Rust gate。 | `L1..L3`，多项基础证据 | 对相应配置/解析/构建边界的失败关闭与基础实现。 | 一项聚合的 dual runtime、通用隔离、Guest IP 或 AI。 |
| `EVD-011` | `phase2-zephyr-native-ns-final-20260722T153445Z`，10 个样本均 110 ms。 | `L4 native/smoke`，历史证据 | 锁定旧环境下 Non-secure Zephyr 原生周期 smoke。 | 3×100,000/30 min 基线、tail 分位数、当前源码 A/B 或硬件上界。 |
| `EVD-012` | `contest-spec/`、开发者指南、脚本说明与当前文档合同。 | `L1 static`，进行中 | 当前需求、工作包、测试和非结论边界已被文档化。 | 实现、runtime、clean-clone、离线审查包或最终交付完成。 |
| `EVD-013` | `results/baseline/runs/phase2-virtio-dma-effect-20260813T001837Z-5328870-dirty-r24j/`；authoritative status/result、四份 capture、QMP/helper、live/review result hash 一致且 cleanup 无残留。 | `L4 single-Guest`，`verified` 窄效果 | 该 device/request/session 绑定下 payload 命中 expected 且独立 guard 不变。 | 通用 DMA isolation、IOMMU/SMMU、P4 网卡准入、dual、IP 或 AI。 |
| `EVD-014` | `results/baseline/runs/phase2-dual-smoke-20260813T225906Z-3455f62/`；同一 QEMU identity 的两份 final Guest-DTB 与 VM1/VM2 frame 已捕获。 | `failed-attempt`，未达到 `L5` 成功 | dual runner/collector 实际执行、两 VM identity/DTB/frame 绑定，以及等待 framed READY 超时这一事实。 | 两端 READY、300 秒双 Guest 成功、稳定性、Guest IP；临时 GDB 观察不是该包确认根因。 |
| `EVD-015` | `results/baseline/runs/phase2-linux-console-20260813T202225Z-c58a1de/`；status=`linux_guest_console_completed`，live/review result hash 一致。 | `L4 single-Guest`，`verified` console 路径 | 锁定 Linux VM 可通过 `/dev/kmsg`→printk→earlycon 产生受控 marker。 | 新 `/dev/console` tty fd、dual READY、regular tty、Guest IP、DMA、实时 A/B。 |
| `EVD-016` | `results/baseline/runs/phase4-net-test011-20260817T043000Z-6692559e-dirty-r41/`；status=`guest_network_failed`，双 VM boot success 后 console 首帧路径触发宿主 EL2 异常，未产生 frame/Guest READY/IP 工件。r32-r40 为同一主问题的独立 failed-attempt 输入，均 immutable。 | `failed-attempt`，未达到 `L7` | 当前 dirty tree 的正式 AxBuild 已完成且 runner 实际启动双 VM；r41 记录 console sink fault 和 primary timeout 的事实。 | 任何双 Guest NIC、ARP/ICMP、UDP/TCP/ICPC、Guest-IP 或 AI-loop 成功。 |
| `EVD-017` | 只读 fetch 的 `upstream/dev=23a07bfcf17863a5eeaae7536d7c0a55c898f50e`；官方 `axvirtio-common`/`axvirtio-net`、device graph、DmaGrant、双 Guest ArceOS demo及相关 wake/console/race 修复。 | `L0 reference`，不是本项目 evidence | 官方基线存在可复用网络/device 能力，支持 `DEC-010` 的迁移决定。 | 当前分支兼容、Linux/Zephyr build、Guest-IP、ICPC、AI 或 runtime 成功。 |
| `EVD-018` | `574d569be...` 及 `phase4-net-test011/012/013/015-*` 本地 run；ICMP 100/100、UDP echo 100/100、TCP 建连、ICPC CONTROL→ACK+STATUS 最新 85/100。 | `L7 runtime smoke`，不是 TEST qualification | 旧 HEAD 的 mediated vnet0 与两 Guest happy-path 数据面确实执行。 | TEST-011 每方向主动 ICMP、10k/fault、TCP framing/restart、1,000 CONTROL/exactly-once、完整 capture/metrics，或新官方 HEAD 兼容。 |
| `EVD-019` | 2026-08-20 只读 fetch `upstream/dev=21ef4b218...`；官方 PR #2092/#2105/#2106 的公开 Actions 结果。 | `L0 reference`，不是本项目 evidence | 当前官方 DeviceContext/read-write、CI manifest v3、host DMA coherency 基线和官方检查状态。 | 本地前移编译、Linux/Zephyr runtime、Guest-IP qualification、AI 或实时性。 |

## 2. 端到端追踪

说明：`SRC-TECH-001` 是《技术方案提纲》，`SRC-APP-001` 是《揭榜申请书》；`ARC-*`/`IF-*` 的定义分别见 `architecture.md`/`contracts.md`。本表覆盖的架构 ID 为 `ARC-001`、`ARC-002`、`ARC-003`、`ARC-004`、`ARC-005`、`ARC-006`、`ARC-007`、`ARC-008`、`ARC-009`；接口 ID 为 `IF-001`、`IF-002`、`IF-003`、`IF-004`、`IF-005`、`IF-006`、`IF-007`、`IF-008`、`IF-009`、`IF-010`、`IF-011`。`DEC-001..010` 已作为 v1 工程设计冻结；这只消除了实现分支选择，不把任何未实现/未运行项升级为完成，改变申报优先级的最终对外措辞仍需项目负责人签字。

| 来源 | 决策 | REQ | 架构 / 接口 | 工作包 | TEST | 当前 EVD | DEL | 当前状态、最高证据与下一缺口 |
|---|---|---|---|---|---|---|---|---|
| `SRC-TECH-001` 总体目标/架构/启动隔离；`SRC-APP-001` 混合关键目标 | `DEC-001..003`、`DEC-006` | `REQ-PLAT-001` | `ARC-001..004`；`IF-001`、`IF-011` | `P2-DUAL-01`、`P2-SOAK-01` | `TEST-001..003`、`TEST-006..007` | `EVD-001..003`、`EVD-008..010`、`EVD-014..015` 及 r27/r31 | `DEL-001..004`、`DEL-006..008` | **P2 已关闭**。r27 完成 300 秒双 Guest，r31 完成 1,800 秒 coexistence，unsafe=0、无残留；不证明 Guest IP、AI 或实时 A/B。 |
| 两来源的部署配置、Linux 至少 2 vCPU、资源隔离 | `DEC-002..003`、`DEC-006` | `REQ-PLAT-002` | `ARC-002`、`ARC-005`；`IF-001`、`IF-011` | `P2-DUAL-01` | `TEST-001..003`、`TEST-006` | `EVD-001..003`、`EVD-008`、`EVD-010`、r27 | `DEL-001..004`、`DEL-007` | **当前 P2 配置已验证**。r27 绑定 Linux `[0,1]`、Zephyr `[2]`、双 READY/final DTB、300 秒和 cleanup；网络设备所有权仍由 P4 单独验证。 |
| `SRC-TECH-001` QEMU AArch64/RISC-V 与板卡增强；`SRC-APP-001` 部署交付 | `DEC-003`、`DEC-006`、`DEC-009` | `REQ-PLAT-003` | `ARC-001`、`ARC-009`；`IF-011` | P2 全链、`P7-DELIVER-01` | `TEST-020..022`（并依赖 `TEST-006..019`） | `EVD-001..003`、`EVD-010`、`EVD-012` | `DEL-001..010`、`DEL-012` | **工程静态合同；范围变更待批准**。v1 工程固定 QEMU AArch64，但只有 `L3/L4` 基础工件，无 clean-clone dual→IP→AI；RISC-V/板卡为后续增强，最终对外范围变化仍待负责人签字。 |
| 两来源的 vCPU/pCPU、高优先级和干扰控制 | `DEC-002..003`、`DEC-006` | `REQ-RT-001` | `ARC-002`、`ARC-004`、`ARC-008`；`IF-001`、`IF-010..011` | `P3-RT-01` | `TEST-001`、`TEST-003`、`TEST-008..009` | `EVD-002..003`、`EVD-008`、`EVD-011` | `DEL-001..005`、`DEL-007` | **静态合同**。配置固定 Zephyr pCPU2，single-Guest smoke 可作前置；没有 dual 压力中的优先级、迁移和干扰证据。 |
| 两来源的 timer/IRQ/锁/调度优化与改前改后数据 | `DEC-006` | `REQ-RT-002` | `ARC-002`、`ARC-004`、`ARC-008`；`IF-010..011` | `P3-RT-01` | `TEST-009..010` | `EVD-002` 仅是旧功能正确性前置 | `DEL-001`、`DEL-003..007` | **未开始**。EOImode 修复解决功能错误，不是按本需求选出的实时优化或 A/B 改善。须先建测量基线，再选 1–2 个可关闭改动。 |
| 两来源的 jitter/调度/IRQ、max/长稳和 native/压力矩阵 | `DEC-003`、`DEC-006` | `REQ-RT-003` | `ARC-004`、`ARC-008`；`IF-010..011` | `P3-RT-01`、`P6-QUAL-01` | `TEST-008..010`、`TEST-019` | `EVD-002`、`EVD-011` 仅 10-sample smoke；新增 host/static 工具尚无 runtime EVD | `DEL-003..005`、`DEL-007` | **静态合同**。事件 schema、统计器、六场景矩阵、native probe 与候选选择工具已通过 host 合同；缺每场景 3 run、每 run至少 100,000 周期或 30 分钟，以及可复算 A/B。 |
| 两来源的 Linux/RTOS TCP/UDP/IP 双向主链路 | `DEC-004..008`、`DEC-010` | `REQ-NET-001` | `ARC-005..006`；`IF-002..004`、`IF-011` | `P4-UPSYNC-02`、`P4-EVID-01`、`P4-SMOKE/REL` | `TEST-011..015` | `EVD-018` smoke、`EVD-019` reference | `DEL-001..004`、`DEL-007..008` | **部分完成/需前移复验**。旧 HEAD 已有真实 ICMP/UDP/TCP/ICPC narrow smoke；官方 DeviceContext/CI v3 前移、Guest-runtime evidence 和 qualification 未完成。 |
| 两来源的拓扑/MAC/IP/route/port/bridge/NAT 文档 | `DEC-005`、`DEC-007..008` | `REQ-NET-002` | `ARC-005..006`；`IF-002..004` | `P4-UPSYNC-02`、`P4-SMOKE-02` | `TEST-001`、`TEST-011..013` | `EVD-008`、`EVD-018` | `DEL-002..004`、`DEL-006..008` | **部分完成/需前移复验**。内部 vnet0/MAC/IP/route/port 已在旧 HEAD runtime；新 HEAD resolved graph/final DTB/runtime 和资格 capture 仍缺。 |
| 两来源的 ICCP 字段、控制/状态/错误、heartbeat/ACK | `DEC-004..005`、`DEC-008` | `REQ-NET-003` | `ARC-006`；`IF-005..008` | `P4-SMOKE-02`、`P4-REL-01` | `TEST-014..015` | `EVD-007`、`EVD-018` | `DEL-001`、`DEL-003`、`DEL-006..007` | **部分完成**。Host codec 已 verified，Guest CONTROL→ACK+STATUS 有 85/100 smoke；HEARTBEAT/ERROR、1,000 CONTROL/fault/restart/动作计数未验收。 |
| 两来源的 ACK/超时/重传/去重/乱序/重启/指标 | `DEC-004..005`、`DEC-008` | `REQ-NET-004` | `ARC-006`、`ARC-008`；`IF-004..005`、`IF-007`、`IF-009..011` | `P4-REL-01`、`P6-QUAL-01` | `TEST-012..015`、`TEST-018..019` | `EVD-007`、`EVD-018` 仅 happy path | `DEL-001`、`DEL-003..005`、`DEL-007` | **部分完成**。真实 Guest happy path 已观察；drop/duplicate/reorder/corrupt、断连/重启、PCAP/metrics 和 exactly-once/cancel 仍未证明。 |
| 两来源的小型 MLP 与 Linux/StarryOS 推理 | `DEC-001`、`DEC-003`、`DEC-008` | `REQ-AI-001` | `ARC-003`、`ARC-007`；`IF-008`、`IF-010..011` | `P5-AI-01` | `TEST-016..017` | host/static 模型工具，尚无 target/runtime EVD | `DEL-001`、`DEL-003..005`、`DEL-012` | **静态合同**。host 数据/模型/训练/导出/验证工具已通过合同；仍须冻结 canonical model/golden vectors，建立部署 C/Linux Guest 应用并取得 target/runtime 证据。 |
| 两来源的 input→inference→IP→RTOS action→feedback | `DEC-001..002`、`DEC-004..005`、`DEC-007..008` | `REQ-AI-002` | `ARC-003..007`；`IF-003..009`、`IF-011` | `P5-AI-01` | `TEST-015`、`TEST-017..018` | — | `DEL-001..005`、`DEL-007`、`DEL-009`、`DEL-012` | **未开始**，最高证据为空。必须在同一 `L8 AI-loop` session 中关联输入、模型、IP CONTROL、Zephyr 动作和反馈；只发送或只打印不算闭环。 |
| 两来源的固定策略基线、至少两指标和失联恢复 | `DEC-005`、`DEC-008` | `REQ-AI-003` | `ARC-007..008`；`IF-008..011` | `P5-AI-01`、`P6-QUAL-01` | `TEST-017..019` | host/static plant/PI/watchdog/metrics，尚无 runtime EVD | `DEL-003..005`、`DEL-007`、`DEL-009`、`DEL-012` | **静态合同**。host plant、PI、watchdog、metrics 合同已通过；仍须相同 plant/初态/目标/扰动/seed 的真实 Guest 对照，并另证 watchdog safe state 和恢复。 |
| 两来源的空载/Linux/网络/AI 压力与长稳 | `DEC-006..008` | `REQ-QUAL-001` | `ARC-001..008`；`IF-010..011` | `P2-SOAK-01`、`P3-RT-01`、`P6-QUAL-01` | `TEST-007`、`TEST-009`、`TEST-019` | r31 `L6 stability`（无 IP/AI）、其余未完成 | `DEL-003..005`、`DEL-007` | **部分完成**。真实 1,800 秒双 Guest coexistence 已通过；仍无 CPU+IP+AI 组合压力、P3 A/B 或数小时综合验收。 |
| 两来源的隔离、错误输入、异常日志和恢复 | `DEC-006..008` | `REQ-QUAL-002` | `ARC-002`、`ARC-005..008`；`IF-001..002`、`IF-005`、`IF-007..011` | `P2-DMA-01`、`P4-NET-01`、`P6-QUAL-01` | `TEST-004..005`、`TEST-012..013`、`TEST-015`、`TEST-018..019` | `EVD-004..006`、`EVD-010`、`EVD-013..015` | `DEL-001..007` | **部分完成**。`EVD-013` 已关闭一次受控 block request 的窄 payload/guard 效果观察；它不证明通用 DMA isolation。dual 综合故障恢复、错误流量下 RTOS 周期和 Guest 网络/AI 恢复仍未证明。 |
| 两来源的源码/配置/脚本/数据/文档/视频成果 | 所有已批准 `DEC-*` | `REQ-DEL-001` | `ARC-009`（汇集 `ARC-001..008`）；`IF-011` | 所有工作包，`P7-DELIVER-01` 收口 | `TEST-020..021` | `EVD-001..012` 仅为部分输入 | `DEL-001..009`、`DEL-011..012` | **部分完成**，最高为分散的 `L1..L4` 基础工件；AI、综合数据/图、视频、最终 manifest 缺失。第三方报告可为经批准 `N/A`，不替代自测。 |
| 两来源的 Apache-compatible、相对 upstream `dev` 无冲突源码交付；使用私有比赛仓库且禁止 PR | `DEC-003`、`DEC-006`、`DEC-009` | `REQ-DEL-002` | `ARC-009`；`IF-011` | `P7-DELIVER-01` | `TEST-022` | — | `DEL-001`、`DEL-006..008`、`DEL-010` | **未开始**。目标仓库已创建且为空；当前 dirty tree 与本地通过不能证明正式交付完成。缺官方历史 mirror、提交拆分、私有分支 push、锁定 base、patch/hash、clean-clone `git am`、license/CI 和 reviewer。禁止创建或提交 PR。 |
| 两来源的可运行、可测量、独立复现和可追证据 | 所有适用 `DEC-*` | `REQ-DEL-003` | `ARC-008..009`；`IF-010..011` | 贯穿所有工作包，`P7-DELIVER-01` 关闭 | `TEST-020..022` | `EVD-012` 加分散 lower-level evidence | `DEL-003..008`、`DEL-010` | **部分完成**，最高为文档/合同和局部可复现。没有 clean clone 的 dual→IP→AI 全链；ignored 本地目录不是独立可访问交付。 |

### 2.1 机器审计完整性索引

范围表达式由读者使用；合同测试用以下显式索引证明没有因范围缩写漏掉 ID：

- 测试：`TEST-001`、`TEST-002`、`TEST-003`、`TEST-004`、`TEST-005`、`TEST-006`、`TEST-007`、`TEST-008`、`TEST-009`、`TEST-010`、`TEST-011`、`TEST-012`、`TEST-013`、`TEST-014`、`TEST-015`、`TEST-016`、`TEST-017`、`TEST-018`、`TEST-019`、`TEST-020`、`TEST-021`、`TEST-022`。
- 交付：`DEL-001`、`DEL-002`、`DEL-003`、`DEL-004`、`DEL-005`、`DEL-006`、`DEL-007`、`DEL-008`、`DEL-009`、`DEL-010`、`DEL-011`、`DEL-012`。

## 3. 审计结论

截至 2026-08-20，不能依据本矩阵宣称整个“最初任务已完成”。P2 的窄 DMA、双 Guest short smoke 与 1,800 秒 coexistence 已关闭；P4 已有旧 HEAD Guest-IP narrow smoke，但无资格级 reliability/capture/metrics，且需前移到官方 21ef；P5 无 AI 闭环，P3 无 production A/B。官方实现与 PR CI 只降低迁移风险，不是本项目 runtime evidence。

证据边界继续保持：`L5 dual-Guest`/`L6 stability` 不等于 `L7 Guest-IP`；官方或 host 侧软件拷贝 mediated VirtIO-net 能力也不等于本项目 Linux+Zephyr runtime。

最短证据关键路径仍为：

```text
TEST-005/006/007（r24j/r27/r31，已通过）
  -> P4-UPSYNC-02 DeviceContext + CI v3
  -> P4-EVID-01 Guest-runtime evidence schema
  -> P4-SMOKE-02 新 HEAD happy-path
  -> [P4-REL-01 TEST-011..015 || P5-AI-A/B]
  -> P5-AI-C/Q TEST-016..018 AI 闭环与安全态
  -> TEST-009..010/019 实时 A/B 和综合压力
  -> TEST-020..022 私有比赛仓库、clean clone、离线补丁应用与交付审计（禁止 PR）
```

`TEST-005` 成功只满足 `DEC-007` 的 P2 窄观察前置，不实现 P4 mediated network，也不能宣称通用 DMA isolation。任何一行状态变化时，必须同时更新相应 `requirements.md`、`test-matrix.md`、`deliverables.md`、工作区 `现状.md`/`阻塞.md`，并保留旧 evidence 的原始状态。
