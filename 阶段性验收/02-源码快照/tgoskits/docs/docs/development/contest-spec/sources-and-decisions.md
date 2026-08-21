# 竞赛需求来源与受控决策

最后更新：2026-08-20

## 1. 目的与使用规则

本文固定“最初任务”到底来自哪里，并登记实现相对申报承诺的所有受控偏差。开发、测试、验收和对外表述必须从本文列出的有效来源追溯，不能从仓库现状反向改写原始承诺。

文档权威顺序如下：

1. `SRC-TECH-001` 与 `SRC-APP-001` 定义申报承诺；
2. 本文 `DEC-*` 只允许记录实现选择、范围延后和批准状态，不能静默删除承诺；
3. `requirements.md` 把承诺规范化为可验收的 `REQ-*`；
4. `architecture.md`、`contracts.md`、`test-matrix.md`、`deliverables.md` 和 `traceability.md` 分别规定实现、接口、验证、交付及端到端映射；
5. `../../DeepSeek-P2-P5开发入口.md`、`../contest-developer-guide.md` 与 `development/current/开发交接.md` 指示当前实际执行的工作包；
6. 代码、测试和 evidence 只能证明已经实现到哪一层，不能降低第 1 项中的要求。

若正文、代码和本文冲突，开发者应停止扩大实现，在本文新增或更新 `DEC-*`，写清批准要求后再继续。未获批准的决策不得用于“已经满足申报要求”的结论。

## 2. 有效需求源登记

哈希算法统一为 SHA-256；路径以包含 `tgoskits/` 和两份申报材料的项目工作区根目录为基准，不在可提交文件中固化个人绝对路径。

| 来源 ID | 工作区相对路径 | 从 `tgoskits/` 看的相对路径 | SHA-256 | 规范作用 |
|---|---|---|---|---|
| `SRC-TECH-001` | `development/sources/陈天昊-良策-技术方案提纲.docx` | `../development/sources/陈天昊-良策-技术方案提纲.docx` | `bc13f31afe1ad0b1e20771379da4f666e77d97ec4d67700dfd8f6952d057029b` | 技术目标、总体架构、技术路线、接口字段、指标、风险、阶段计划和验收交付 |
| `SRC-APP-001` | `development/sources/陈天昊-良策-揭榜申请书.docx` | `../development/sources/陈天昊-良策-揭榜申请书.docx` | `4ea47d1b9c5fa8051302a8518a8cd48f2118886c28eeb0273df43922b4e99c33` | 项目范围、四类工作方向、团队承诺、量化验收、计划和最终交付 |

重新提取需求前必须先校验哈希：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath `
  '..\development\sources\陈天昊-良策-技术方案提纲.docx', `
  '..\development\sources\陈天昊-良策-揭榜申请书.docx'
```

以下文件是无姓名前缀的空白或早期模板，不是本项目需求源：

- `技术方案提纲.docx`
- `揭榜申请书.docx`

即使模板中的段落与成稿相似，也不得用模板覆盖、补写或解释 `SRC-TECH-001`、`SRC-APP-001`。若成稿发生任何字节变化，应登记新的来源版本和哈希，并重新审计全部 `REQ-*`，不能直接替换本表哈希。

## 3. 原始承诺摘要

两份有效来源共同承诺一套“能运行、可测量、可复现、形成闭环”的混合关键系统：

- 在同一虚拟化平台上稳定运行 Linux/StarryOS 与 RTOS；Linux/StarryOS 至少获得 2 个 vCPU，并完整记录 vCPU/pCPU、内存、设备、中断和启动参数；
- 针对 AxVisor 的实时路径实施可审查改造，用空载、Linux 压力、网络/AI 压力和长稳数据对比优化前后以及原生/裸 RTOS 基线；
- Linux/StarryOS 与 RTOS 的主通信链路必须是 TCP/UDP/IP，交换控制、状态和错误消息，并具备心跳、确认、超时、重传、去重、乱序处理和恢复；
- Linux/StarryOS 运行真实轻量神经网络推理，经 IP 下发控制结果，RTOS 执行动作并回传状态；至少用两个控制或时延指标对比固定参数/人工策略；
- 交付源码、配置、脚本、数据、协议、模型、测试、复现文档、演示视频、私有比赛仓库和可审查离线源码包。原材料中的 upstream 兼容要求保留为来源约束；开发提交进入 `qcl-kernel/tgoskits-liangce`，不创建或提交 PR，也不向 upstream push。

申报中的 StarryOS、RT-Thread、RISC-V 等选项即使在当前工程基线中延后，也仍保留在承诺账本中；只有获项目负责人批准的范围变更，才能在最终申报/答辩材料中改变其表述。

## 4. 决策状态定义

| 状态 | 含义 | 能否作为最终范围依据 |
|---|---|---|
| `adopted` | 已作为当前工程基线执行，且不改变申报承诺 | 可以，但仍需完成对应验收 |
| `adopted-for-v1-design` | 本轮用户已授权作为 v1 工程设计冻结；实现、目标构建和 runtime evidence 仍须逐项取得 | 可作为仓库 v1 开发输入；若改变申报优先级，最终对外材料仍须项目负责人签字确认 |
| `adopted-pending-final-approval` | 已用于工程推进，但改变了申报优先级或平台范围 | 不可以；最终交付前须由项目负责人批准 |
| `pending-owner-approval` | 仅是候选决策，等待项目负责人选择 | 不可以；不得据此宣称需求已满足 |
| `superseded` | 已被后续明确决策取代 | 不可以；保留审计记录 |

批准者默认是项目负责人/申报人。代码合入、测试通过或模型口头选择均不等于范围批准；批准结果须在相应 `DEC-*` 的“批准记录”字段写入日期、批准人和结论。

## 5. 受控决策登记

### DEC-001：Linux-only v1 智能计算 Guest

- **状态**：`adopted-for-v1-design`（设计冻结，未实现）
- **来源约束**：`SRC-TECH-001` 与 `SRC-APP-001` 均允许 Linux/StarryOS；StarryOS 是加分/增强路线，Linux 是功能不足时的明确回退路径。
- **当前决定**：v1 只实现 Linux Guest 的智能计算、网络会话和 MLP 推理；StarryOS 不进入 v1 代码、测试或演示关键路径。
- **理由与事实基础**：当前仓库已有 Linux 镜像、SMP2 配置和相关运行门禁，尚无可直接承载神经网络与 IP 控制应用的 StarryOS 竞赛工件。
- **影响**：所有基础验收先以 Linux 完成；未实际运行 StarryOS 时，对外只能表述为“接口预留/延后”，不能表述为 StarryOS 已完成。
- **批准要求**：本轮用户指令授权 v1 工程执行；最终对外材料若把 StarryOS 从申报优先级中移除，须由项目负责人签字确认。
- **批准记录**：2026-08-12，本轮用户要求冻结 Linux-only v1 设计；Linux 闭环尚未实现。
- **复审触发器**：最终对外材料冻结或评审明确要求 StarryOS。

### DEC-002：Zephyr-only v1 RTOS Guest

- **状态**：`adopted-for-v1-design`（设计冻结，未实现）
- **来源约束**：原方案优先使用基线支持的 RT-Thread，Zephyr/FreeRTOS 是可选路线。
- **当前决定**：v1 只实现 Zephyr Guest 的周期控制、网络适配和安全状态；RT-Thread 与 FreeRTOS 不进入 v1 代码、测试或演示关键路径。
- **理由与事实基础**：仓库已经形成 Zephyr 单 Guest、SMP1、Guest-FDT、console 与 dual 配置资产，而 RT-Thread 尚无同等级可复现工件。继续使用 Zephyr可减少平台变量并保留底层验证成果。
- **影响**：这是相对申报优先级的实质变化。所有 RTOS 测试、应用和证据必须写明 Zephyr；不得把 Zephyr 证据归为 RT-Thread 证据。
- **批准要求**：本轮用户指令授权 v1 工程执行；最终对外材料须由项目负责人签字确认“以 Zephyr 满足 v1 RTOS 路线、RT-Thread/FreeRTOS 延后”的优先级变化。
- **批准记录**：2026-08-12，本轮用户要求冻结 Zephyr-only v1 设计；端到端闭环尚未实现。
- **复审触发器**：端到端闭环完成、最终材料冻结，或评审明确要求 RT-Thread。

### DEC-003：QEMU AArch64-only v1 复现平台

- **状态**：`adopted-for-v1-design`（设计冻结，未实现）
- **来源约束**：原技术路线将 QEMU AArch64/RISC-V 作为可复现路线，开发板是增强项。
- **当前决定**：v1 只实现 `QEMU AArch64 + AxVisor + Linux SMP2 + Zephyr SMP1`；RISC-V 和开发板均不进入 v1 复现、测试或演示关键路径。
- **理由与事实基础**：当前 CPU、GIC、Guest-FDT、PL011、内存、DMA 和 dual 配置工作均围绕 QEMU AArch64，切换架构会引入另一套中断、设备树、镜像和验证变量。
- **影响**：AArch64 的成功不能证明 RISC-V 或开发板；所有证据必须记录实际架构和 QEMU 版本。
- **批准要求**：本轮用户指令授权 v1 工程执行；最终对外材料若不提供 RISC-V 路线，须由项目负责人签字确认并同步申报说明。
- **批准记录**：2026-08-12，本轮用户要求冻结 QEMU AArch64-only v1 设计；完整 AArch64 闭环尚未实现。
- **复审触发器**：AArch64 综合验收完成，或比赛验收明确要求双架构。

### DEC-004：ICCP 公共名称与 ICPC v1 wire 的受控映射

- **状态**：`adopted-for-v1-design`（设计冻结；现有 host 合同已实现，Guest 集成未实现）
- **来源约束**：两份申报材料使用 `ICCP`，并给出版本、消息类型、flags、序号、时间戳、载荷长度、错误码和 CRC 等字段；当前仓库实现与文档使用 `ICPC v1`。
- **当前决定**：公开材料首次出现写作“ICCP（仓库实现名 ICPC v1）”；代码标识、wire magic、36-byte 网络字节序固定头、C99 库和测试继续使用现有 `ICPC v1`，不重命名历史 wire。
- **差异**：该映射不只是拼写别名，v1 对字段宽度和时间单位作受控收敛，见下表；它不表示与申报 ICCP 逐字节相同。

| 申报 ICCP 字段 | 仓库 ICPC v1 | 差异处理 |
|---|---|---|
| `version: u8` | `version: u8` | 直接映射 |
| `msg_type: u8` | `message_type: u8` | 仅命名变化 |
| `flags: u16` | `flags: u8` | v1 只定义 ACK_REQUIRED/RETRANSMISSION，高位拒绝 |
| `seq: u32` | `sequence: u32` | 直接映射 |
| `timestamp_ns: u64` | `timestamp_ms: u64` | 受控降为单调毫秒；禁止据此算跨 Guest 单向延迟 |
| `payload_len: u32` | `payload_length: u16`，最大 1024 | 受控上限，v1 禁止分片 |
| `error_code: i32` | `error_code: u16` | v1 使用非负、小范围协议错误码 |
| `checksum/crc32: u32` | `crc32c: u32` | 固定 Castagnoli CRC32C |
| 未列出 | `magic`、`header_length`、`session_id`、`ack_sequence` | 为版本识别、会话和 ACK 增加 |

- **理由与事实基础**：当前 C99 实现和 host 合同已经使用 ICPC v1；直接改名会扩大无功能收益的改动，但字段差异必须显式评审。
- **影响**：设计冻结不把 host 测试升级成 Guest/IP 证据；若评审要求原始 ICCP 字节布局，必须发布新版本与迁移测试，禁止原地改写 v1。
- **批准要求**：本轮用户指令授权 v1 工程执行；最终对外材料采用该映射须由项目负责人签字确认。
- **批准记录**：2026-08-12，本轮用户要求冻结名称和现有 36-byte ICPC v1 wire；Guest 集成未实现。
- **复审触发器**：P4-NET-01 开始 Guest payload 集成，或最终协议文档冻结。

### DEC-005：UDP:46000 主传输，TCP:46001 显式新 session 回退

- **状态**：`adopted-for-v1-design`（设计冻结，未实现）
- **来源约束**：申报要求 TCP/UDP/IP 主链路，并特别要求 ACK、超时、重传、重复和乱序；没有规定必须同时使用 TCP 与 UDP。
- **当前决定**：Linux 与 Zephyr 都绑定 UDP `46000` 承载 ICPC v1；通用可靠消息使用 ACK、100/200/400 ms 退避与最多 3 次重传。CONTROL 另受 500 ms validity 约束，只能在 `t=0/100/300 ms` 发送，必须取消 `t=700 ms` 的第三次重传并进入显式回退。TCP `46001` 只可在 UDP session 已明确失败、Linux 停止旧 UDP 发送并关闭旧 session 后，以新的非零 `session_id` 显式建立回退；UDP 与 TCP 不得同时控制 actuator。
- **理由与事实基础**：UDP 能直接覆盖申报可靠性评分点；TCP 可降低工期失控时的交付风险。
- **影响**：TCP 回退也必须基于真实 Guest IP，并实现长度分帧、断连、重连、超时和会话重置；不能用 host loopback 或共享内存替代。
- **批准要求**：本轮用户指令授权 v1 工程执行；最终对外材料采用 UDP 主线与 TCP 回退须由项目负责人签字确认。
- **批准记录**：2026-08-12，本轮用户要求冻结 UDP:46000/TCP:46001 v1 设计；真实 Guest IP 未实现。
- **复审触发器**：真实 Guest UDP echo 完成，或 UDP 可靠性门禁连续失败。

### DEC-006：以安全依赖顺序替代原日历执行顺序

- **状态**：`adopted-for-v1-design`（设计冻结，未实现）
- **来源约束**：原日历先做基线和实时，再做 IP、AI、集成；最终验收范围不因执行顺序变化而减少。
- **当前决定**：P2-DMA/DUAL/SOAK 已关闭并冻结，不再扩大通用 DMA isolation。当前关键路径为 `P4-UPSYNC-02 -> P4-EVID-01 -> P4-SMOKE-02 -> [P4-REL-01 || P5-AI-A/B] -> P5-AI-C -> P5-AI-Q -> P3-RT-01 -> P6 -> P7`。P5 实现/happy-path 可利用真实 P4 smoke 并行推进，但资格结论必须等待 P4 reliability；P3 的只读测量工具可并行，生产实时语义改造须建立在冻结的 P4/P5 负载上。P3 路径选择不再依赖人工偏好：候选须在 3 组配对基线中都贡献总 P99.9 `>=15%` 或绝对 `>=20 us`，最多选择前两条；无候选达标时保持未完成并如实报告。
- **理由与事实基础**：Zephyr VirtIO-net 涉及尚未关闭的 DMA ownership；双 Guest、独立 console 和可归属 evidence 是网络、AI 与实时 A/B 的共同前置条件。
- **影响**：原阶段日期已经发生偏差，不能继续以原日期声称按期完成；安全门禁、证据等级和未完成工作必须保留。
- **批准要求**：内部执行顺序调整不需额外批准；任何为了赶期而删除 `REQ-*`、降低验收或改变最终平台范围的动作仍须负责人批准。
- **批准记录**：已在当前开发者指南和计划中采用。
- **复审触发器**：DMA/console gate 关闭、关键路径变化或最终交付范围拟缩减。

### DEC-007：P2 窄 virtio-blk 观察与 P4 软件拷贝 mediated VirtIO-net

- **状态**：`adopted-for-v1-design`（设计冻结，未实现）
- **来源约束**：申报要求使用真实 Guest TCP/UDP/IP 主链路；它没有授权绕过宿主/其他 VM 内存边界，也没有把一次设备字节观察定义为通用 DMA 隔离。
- **当前决定**：P2 仅继续验证 Linux virtio-blk `MapReserved` 的 identity-bound 单设备、单请求、四份 capture 窄字节效果观察；它不授权任何网络设备或 Zephyr DMA。P4 固定开发 AxVisor 软件拷贝 mediated VirtIO-net 双端与有界 L2 交换机：backend 只在经检查的 Guest memory context 与自身 bounce buffer 间复制，按 VM/NIC ownership、长度和队列状态失败关闭；禁止 outer-QEMU slot2 直通、固定 identity-RAM 捷径、IOMMU/SMMU 声称或把 P2 结果升级为 DMA isolation。
- **当前事实**：该段描述的是 2026-08-12 的旧基座。2026-08-17 官方 `dev` 已有第一方 axvirtio/device-graph/DmaGrant 与双 Guest网络实现；生产落点由 `DEC-010` 更新。Linux+Zephyr adapter、竞赛策略和本项目 runtime evidence 仍须完成。
- **影响**：现有 outer-QEMU `hub0`/slot2 仅为静态拓扑历史，不能作为 v1 网络 backend；Zephyr slot2 继续排除，直至 mediated 双端和 bounded L2 全部实现并验证。
- **批准要求**：本轮用户指令授权 v1 工程执行；最终对外材料须由项目负责人签字确认该路线相对申报优先级的解释。
- **批准记录**：2026-08-12，本轮用户要求冻结 P2/P4 两层边界和软件拷贝 mediated 路线；未实现、未取得 runtime evidence。
- **复审触发器**：mediated device、Guest memory context、bounded L2、定向合同或双 Guest runtime 的任一语义变化。

### DEC-008：vnet0、地址、端口与温度 MLP 控制 payload 冻结

- **状态**：`adopted-for-v1-design`（设计冻结，未实现）
- **来源约束**：申报要求明确拓扑、MAC/IP、路由、端口、协议字段和控制/状态/错误语义。
- **当前决定**：v1 使用 AxVisor 内部 isolated `vnet0`，不是 outer-QEMU `hub0` 或 slot2 直通；Linux MAC/IP 为 `02:00:00:00:00:01` / `10.77.0.1/24`，Zephyr 为 `02:00:00:00:00:02` / `10.77.0.2/24`，两端仅有 connected route，无默认网关、DNS、NAT、bridge 或宿主代理。UDP `46000` 和 TCP `46001` 遵循 `DEC-005`。
- **AI/控制决定**：被控对象固定为一阶温度对象；Zephyr 每 100 ms 执行控制周期，Linux 使用 `3→8→1` 小型 MLP，以 `{measured_mC, target_mC, previous_duty_q16_16}` 为输入并输出 `duty_q16_16`。保持现有 ICPC CONTROL 24 B、STATUS 32 B、ERROR 12 B 的固定大端 payload；从最近有效新 CONTROL 起 500 ms 未刷新时，Zephyr 将 duty 置零并上报 `SAFE|NETWORK_TIMEOUT`。
- **理由与事实基础**：内部 vnet0 与软件拷贝 mediated 双端能避免把 outer-QEMU 静态设备直通误当作 Guest network ownership；固定私网、端口、温度单位和小模型形状消除 DHCP、随机端口和控制对象分叉。
- **影响**：这些值是冻结开发输入，不是已运行事实；任何变更必须同时更新接口合同、两端配置、黄金向量、pcap validator、测试矩阵和 traceability。
- **批准要求**：本轮用户指令授权 v1 工程执行；最终对外材料采用此优先级与字段解释须由项目负责人签字确认。
- **批准记录**：2026-08-12，本轮用户要求冻结 vnet0/IP/端口/温度对象/MLP/watchdog 设计；两端应用与 runtime evidence 未实现。
- **复审触发器**：mediated vnet0 语义、payload schema、plant 参数、模型层形状或安全超时变化。

### DEC-009：内部可交付快照冻结与官方提交时刻核验

- **状态**：`adopted-for-v1-design`（交付管理设计冻结；不改变任何技术需求）
- **来源约束**：`SRC-TECH-001` 与 `SRC-APP-001` 的完整需求、验收和交付物持续有效；时间不足不能取消需求，也不能把未完成项改写为完成。
- **当前决定**：`2026-08-20 18:00 Asia/Shanghai` 是 v1 的内部可交付快照冻结点：届时冻结可审查源码、文档、manifest 草案、已取得的 evidence 与明确的未完成边界。开发分支为 `contest/axvisor-ai-control`，正式比赛仓库为私有 `qcl-kernel/tgoskits-liangce`，以锁定的 `rcore-os/tgoskits:dev` commit 作为只读兼容基线；最终产物包括职责清晰的 commit series、私有仓库分支、`git format-patch` 离线灾备包、base/head SHA、patch SHA-256、CI/复现记录与 reviewer 签字。**禁止创建或提交 PR，禁止直接推送 upstream；允许并要求向本组私有比赛仓库 push。**内部冻结不是杜撰的官方截止，也不改变申报平台 `2026-08-21` 至 `2026-08-24` 窗口。
- **外部核验**：官方平台的精确截止时刻、时区以及代码/文档/视频是否同一时限属于行政外部核验；该核验不阻塞代码和文档开发，但在最终上传、提交或对外宣称“已提交”前必须完成。
- **影响**：内部快照后发现的未完成 `REQ-*` 必须保持未完成状态并继续在 traceability、测试矩阵与交付清单中可见；不得为赶时间删除、降级或拼接低等级 evidence。
- **批准要求**：本轮用户指令授权内部冻结点作为工程执行输入；任何改变原始需求、最终范围或对外提交日期的动作仍须项目负责人签字确认。
- **批准记录**：2026-08-12，用户确认队名为“良策”、成员仅本人，并创建 `qcl-kernel/tgoskits-liangce`；本轮仍禁止创建或提交 PR，但按成员手册要求使用私有比赛仓库持续 push。官方精确截止时刻及代码以外材料的上传格式尚待核验。
- **复审触发器**：官方平台发布精确截止、项目负责人批准范围变更，或内部快照清单发生内容变化。

### DEC-010：采用官方 axvirtio/device-graph 网络基线，停止扩展旧自研栈

- **状态**：`adopted-for-v1-design`（迁移设计冻结，未取得 Linux+Zephyr Guest-IP）
- **历史工作包**：`P4-UPSTREAM-01` 在 23a 基线上完成首轮选择性迁移；它已被当前 `P4-UPSYNC-02` 覆盖，不得再作为当前官方兼容性结论。
- **来源约束**：原始材料要求基于 tgoskits/AxVisor、兼容官方 `dev`，通过 VirtIO-net 与 TCP/UDP/IP 完成 Linux/RTOS 主链路；禁止用低层替代物冒充该链路。
- **参考快照**：2026-08-20 只读 fetch 后 `rcore-os/tgoskits:dev=21ef4b218ebb74641134238c2050d488c7504249`；最近生产代码 checkpoint `574d569bea21f6a47e9adde10bea4a5a270b96d0` 相对它 ahead 15/behind 39，其后 docs-only 提交不改变 runtime 归属。新增相关变化包括 #2092 issuing-vCPU-bound `DeviceContext`/分离 `read`/`write`、#2105 CI manifest v3 和 #2106 host physical-device DMA coherency；后者与 Guest-memory `DmaGrant` 是不同边界。
- **当前决定**：P4 生产实现必须以官方 `axvirtio-common`/`axvirtio-net`、device graph、issuing-vCPU `DeviceContext`、scoped DMA grant 和 VM wake/IRQ 接口为基线。旧自建 Guest-memory/IrqSink 仅作为迁移差异输入，不得继续扩展成并存的第二套 backend。先关闭 `P4-UPSYNC-02` 与 `P4-EVID-01`，再在新 HEAD 重建 smoke/qualification；contest CI 必须接入 manifest v3，不得继续在旧 workflow 中硬编码测试列表。
- **feature 决定**：与官方第一版一致，只协商 `VIRTIO_F_VERSION_1`、`VIRTIO_NET_F_MAC`、`VIRTIO_NET_F_STATUS`。链路 MTU 固定为 1500，但 v1 不广告 `VIRTIO_NET_F_MTU`；若以后需要广告，必须作为新的兼容性决策和 Guest 正负例单独加入。
- **证据解耦**：官方两 ArceOS Guest demo 与官方 PR CI 仅是上游能力参考，不是本项目 Linux+Zephyr evidence。当前 ICMP/UDP/TCP/ICPC run 是旧 HEAD narrow smoke；因为 runner 只验证 marker+cleanup、profile 仍 host-only 且 capture/metrics 不完整，不能升级为 TEST-011..015 verified。P4-EVID-01 必须建立 Guest-runtime schema、结构化 scenario oracle 和完整工件。
- **迁移门禁**：锁定官方 SHA；生成旧路径到官方接口的逐文件迁移矩阵；通过官方示例的 host/target 门禁；生产配置只存在一个网络 backend；Linux/Zephyr adapter 可构建；descriptor、DMA grant、跨 VM、reset/stale、overflow 和错误 IRQ owner 负例通过。QEMU 运行仍须用户明确确认。
- **影响**：减少重复实现和旧框架调试，把主要开发量转回原始评分链 `NIC/ARP/ICMP -> UDP/TCP/ICPC -> MLP -> realtime A/B`。不授权盲目 rebase/cherry-pick；Git 历史操作仍按工作区规则取得明确确认。
- **批准记录**：2026-08-17，用户要求结合参考仓库和最初目标给出确定方案并更新开发文档。
- **复审触发器**：官方接口发生破坏性变化、Linux/Zephyr driver 无法在限定范围内兼容，或迁移门禁以可复现实证证明不可行。

## 6. 原计划与当前偏差

| 原阶段 | 原日期 | 原承诺 | 当前映射与事实 |
|---|---|---|---|
| 阶段 1 | 2026-07-05 至 07-12 | 环境、镜像、单 Guest/基线 | 已形成较多静态与 single-Guest 基础；latest-upstream 双 Guest 尚未通过 |
| 阶段 2 | 2026-07-13 至 07-26 | 实时路径改造与 A/B 数据 | 映射 `P3-RT-01`；生产改造和成套 A/B 数据未完成 |
| 阶段 3 | 2026-07-27 至 08-06 | TCP/UDP/IP 与可靠性 | 映射 `P4-NET-01`；host ICPC 合同已有，真实 Guest IP 未完成 |
| 阶段 4 | 2026-08-07 至 08-14 | 小型 MLP 和控制闭环 | 映射 `P5-AI-01`；应用、模型和闭环尚未完成 |
| 阶段 5 | 2026-08-15 至 08-20 | 综合测试、文档、视频 | 映射 `P6-QUAL-01`、`P7-DELIVER-01`；依赖前序运行门禁 |
| 阶段 6 | 2026-08-21 至 08-24 | 最终提交 | 尚未开始；需要私有比赛仓库、clean-clone、离线 commit/patch 审查包、交付清单和演示证据；禁止创建或提交 PR |

截至 2026-08-14，原日历已不能描述实际完成度。开发者必须按依赖和 evidence 推进，而不是补写日期。新的恢复排期可以在 `development/current/计划.md` 调整，但必须同时引用 `DEC-006`，并逐项保留 `requirements.md` 中的未完成要求。

## 7. 决策关闭规则

一个 `DEC-*` 进入 `adopted-for-v1-design` 代表本轮用户已授权其作为工程执行输入，不代表代码、目标构建或 runtime 已完成。只有在以下内容齐全时，才能把设计冻结用于最终对外范围结论：

1. 决策结论及被选择/被放弃的方案；
2. 对全部相关 `REQ-*`、接口、测试和交付物的影响；
3. 项目负责人、批准日期和可定位记录；
4. 若改动原始承诺，最终申报/答辩材料采用的准确措辞；
5. `traceability.md`、测试矩阵、交付清单和开发者指南同步更新。

未满足以上条件时，必须保持“设计冻结，未实现”或“未完成”的证据表述。实现先行不等于 runtime 通过，也不等于项目负责人已对外批准范围变更。
