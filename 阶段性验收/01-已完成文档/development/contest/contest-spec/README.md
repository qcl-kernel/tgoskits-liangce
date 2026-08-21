# “良策”项目开发规格索引

最后更新：2026-08-14

本目录把 `development/sources/` 的两份项目申报材料转换成可开发、可测试、可交付的规范。它回答“最初承诺了什么、为什么采用当前方案、组件之间怎样交互、每项要求怎样验收”。统一接手入口是 `development/DeepSeek-P2-P5开发入口.md`，仓库级执行指南仍是 [`../contest-developer-guide.md`](../contest-developer-guide.md)；两者都不得改写或省略本目录中的原始需求。

## 1. 规范来源和优先级

发生冲突时按以下顺序处理：

1. `development/sources/陈天昊-良策-技术方案提纲.docx` 与 `development/sources/陈天昊-良策-揭榜申请书.docx` 定义项目承诺；
2. [`sources-and-decisions.md`](sources-and-decisions.md) 记录对承诺的受控取舍，未批准的决策不能伪装成原文；
3. [`requirements.md`](requirements.md)、[`architecture.md`](architecture.md) 和 [`contracts.md`](contracts.md) 是实现规范；
4. [`work-packages.md`](work-packages.md) 与开发者指南给出执行顺序；
5. [`test-matrix.md`](test-matrix.md)、[`traceability.md`](traceability.md) 和 [`deliverables.md`](deliverables.md) 决定能否宣称完成。

两份来源在 2026-08-12 进行了全文、表格、批注和修订标记的结构化审计。受项目规则限制未使用 LibreOffice，因此本规范不声称验证了 Word 的逐页视觉排版。来源字节哈希见 `sources-and-decisions.md`。

## 2. 开发者阅读路径

| 需要回答的问题 | 必读文件 |
|---|---|
| 原申请到底承诺了什么，当前为何是 Linux + Zephyr？ | `sources-and-decisions.md`、`requirements.md` |
| 系统由哪些组件组成，代码应该放在哪一层？ | `architecture.md` |
| 拓扑、DMA、网络、协议、控制和证据怎样传递？ | `contracts.md` |
| 此刻该开发哪一部分，输入输出是什么？ | `development/DeepSeek-P2-P5开发入口.md`、`work-packages.md`、`../contest-developer-guide.md`、`development/current/开发交接.md` |
| P2 平台、安全前置、双 Guest 与长稳怎样逐步实现？ | [`../contest-stage-p2-platform.md`](../contest-stage-p2-platform.md) |
| P3 实时路径怎样测量、筛选和做同输入 A/B？ | [`../contest-stage-p3-realtime.md`](../contest-stage-p3-realtime.md) |
| P4 mediated VirtIO-net、Guest IPv4 与 ICPC 怎样实现？ | [`../contest-stage-p4-network.md`](../contest-stage-p4-network.md) |
| P5 plant、MLP、控制、安全态和指标怎样闭环？ | [`../contest-stage-p5-ai-control.md`](../contest-stage-p5-ai-control.md) |
| 某项能力要跑哪些测试，什么结果才通过？ | `test-matrix.md` |
| 一条公开结论如何追到原文、代码、测试和交付物？ | `traceability.md` |
| 最终提交包缺什么？ | `deliverables.md` |

## 3. ID 与状态规则

- `SRC-*`：原始材料；`DEC-*`：范围或接口决策；`REQ-*`：规范需求；`ARC-*`：架构组件；`IF-*`：跨边界契约；`P*-*`：工作包；`TEST-*`：验收；`EVD-*`：证据；`DEL-*`：交付物。
- 实现/测试/交付生命周期使用 `planned`、`implemented`、`blocked`、`verified`、`delivered`；失败运行另记 `failed-attempt`。`requirements.md` 的中文证据状态与之并行：`未开始/静态合同/部分完成/受阻/已满足/延后-待批准`。存在文件或静态合同最多说明 `implemented/静态合同`；只有对应层级的运行证据才能写 `verified/已满足`，最终清单复核后才是 `delivered`。
- 每个 `REQ-*` 必须至少关联一个 `TEST-*` 和一个 `DEL-*`；每个 `IF-*` 必须记录生产者、消费者、版本、校验、失败行为、正例与负例。
- 每项“已完成”结论必须指向不可覆盖的 `EVD-*`、run ID、输入/输出哈希和复算命令。原始大日志可保持 ignored，但可提交索引不得指向不可访问的本地事实后仍声称公开可复现。
- 任何平台裁剪、协议字段/名称、阈值或依赖顺序变化都新增或更新 `DEC-*`；不得覆盖历史证据，也不得把未批准决策写成申报原文。

跨文件状态按下表映射；各列描述不同对象，不能把一个文件中的单词直接复制到另一文件：

| 生命周期事实 | 工作包/实现 | `requirements.md` | `test-matrix.md` | `deliverables.md` | 是否允许宣称完成 |
|---|---|---|---|---|---|
| 只有设计或静态合同 | `planned` 或 `implemented` | `静态合同`/`未开始` | `planned`/`blocked`/`partial` | `planned`/`partial` | 否 |
| 生产代码存在但目标运行未通过 | `implemented` | `部分完成` 或 `受阻` | `partial` | `partial` | 否 |
| 一次真实执行失败 | 工作包保持活动或受阻 | 不升级 | `failed-attempt`，另建不可覆盖 `EVD-*` | 不升级 | 否 |
| 本行 TEST 的环境、oracle 和工件全部通过 | 对应工作包可关闭 | 只有全部关联强制 TEST 通过才写 `已满足` | `verified` | `partial` 或 `ready-for-review` | 只允许该 TEST/REQ 的窄结论 |
| 文件、hash、复现、review 和最终 manifest 均冻结 | `delivered` | 已满足 | verified | `delivered` | 仅允许 manifest 覆盖的结论 |

`requirements.md` 是需求事实权威，`test-matrix.md` 是验收状态权威，`deliverables.md` 是交付状态权威，`development/current/现状.md` 只汇总而不能覆盖它们。一次状态变化必须先更新产生事实的权威文件，再同步 `traceability.md` 和集中账本。

## 4. 完整完成条件

只有 `traceability.md` 中所有强制 `REQ-*` 达到 `verified`，所有目标 `DEL-*` 达到 `delivered`，并且私有比赛仓库、干净 clone 演练、离线 patch/commit 审查包和演示均完成时，才能说“最初任务已完成”。开发提交直接进入 `qcl-kernel/tgoskits-liangce` 私有仓库，不创建或提交 PR；`rcore-os/tgoskits` 只读。当前事实仍是阶段 2：single-Guest 和安全前置较多，但受控 DMA effect、双 Guest、实时化 A/B、真实 Guest IP、AI 闭环和最终交付均未完成。

## 5. 2026-08-12 冻结的 v1 技术路线

开发者不再自行选择以下分支；完整理由和未实现边界见 `sources-and-decisions.md` 的 `DEC-001..010`：

- 智能 Guest = Linux；RTOS Guest = Zephyr；复现平台 = QEMU AArch64。StarryOS、RT-Thread/FreeRTOS、RISC-V 和开发板为后续增强，不能用当前证据声称已覆盖。
- 公共材料首次写 `ICCP（仓库实现名 ICPC v1）`；wire、magic、36-byte header 与 host C99 实现保持 ICPC v1。
- 主传输 = UDP `46000`，应用层 ACK/RTO 为 100/200/400 ms、最多 3 次重传；TCP `46001` 仅在旧 UDP session 明确关闭后以新 session 显式回退。
- P2 的 Linux `MapReserved`/virtio-blk 只做 identity-bound 窄字节观察；P4 必须实现 AxVisor 软件拷贝 mediated VirtIO-net 双 frontend 和 bounded internal `vnet0`。禁止 outer-QEMU slot2 passthrough，也禁止把 r23/未来 P2 成功称作 DMA isolation。
- 网络固定 Linux `02:00:00:00:00:01` / `10.77.0.1/24`、Zephyr `02:00:00:00:00:02` / `10.77.0.2/24`，无默认网关、DNS、TAP/bridge/NAT 或 Host proxy。
- 控制对象 = 100 ms 一阶温度 plant；Linux 模型 = `3→8→1` MLP；CONTROL/STATUS/ERROR payload = 24/32/12 B；500 ms 无有效新 CONTROL 时 Zephyr duty 归零并上报安全态。

这些是“设计冻结”，不是“实现完成”。当前执行链以 `work-packages.md` 为准：P2 窄观察 → dual smoke → 1,800 s soak → mediated network → Guest IP/ICPC → AI → 实时 A/B → 综合验证 → 私有比赛仓库/clean-clone/离线审查包。`2026-08-20 18:00 Asia/Shanghai` 仅为内部可交付快照冻结点；官方平台精确截止和代码以外材料的上传格式仍须外部核验。
