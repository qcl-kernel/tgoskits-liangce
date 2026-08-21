# 工作包与规范对象映射

最后更新：2026-08-20

本文固定“开发哪一部分”的边界。实时状态和长命令分别由工作区 `开发交接.md` 与 `scripts/contest/README.md` 维护；本文不复制一次性 run 参数。

| 工作包 | 需求 | 主要接口 | 输入 | 修改或新建范围 | 输出 | 验收与交付 |
|---|---|---|---|---|---|---|
| `P2-DMA-01` | `REQ-QUAL-002`；`REQ-NET-001` 安全前置 | `IF-011`；P2 专属 request/session/result schema 见 `scripts/contest/README.md` | r23 reservation、prepared rootfs/QEMU/request、锁定 Guest kernel | `scripts/contest/*virtio_dma*`、helper 与对应合同；每次新建 ignored run | 一次 identity-bound block request 的四份只读 capture、session/result/status | `TEST-004/005`；支撑原始数据交付，但仅为窄字节效果观察，不实现任何 P4 网络接口 |
| `P2-DUAL-01` | `REQ-PLAT-001`、`REQ-PLAT-002` | `IF-001`、`IF-011` | dual TOML、outer QEMU、Guest-DTB/QMP、console demux | 新建 `scripts/contest/run_dual_guest_smoke.py` 及合同；在 `scripts/contest/evidence/` 实现公共 session publisher/validator 和 P2-DMA 只读 adapter；必要时修对应平台边界 | VM1/VM2 各自 READY、DTB、console、resource claim、cleanup，以及可供后续阶段复用的统一 evidence envelope | `TEST-001/006`；`DEL-001..004` |
| `P2-SOAK-01` | `REQ-PLAT-001`、`REQ-QUAL-001` | `IF-010`、`IF-011` | P2-DUAL runner 与离线 soak validator | 扩展 runner/collector，不复制第二套身份验证 | `>=1800s` monotonic session、生命周期/残留统计 | `TEST-007`；`DEL-003/004/007` |
| `P3-RT-01` | `REQ-RT-001..003` | `IF-010`、`IF-011` | 原生 Zephyr、AxVisor 相同镜像/配置、压力场景 | 新建 `scripts/contest/rt/`、`contest-realtime-path.md`；数据支持后只改 1–2 个 virtualization 关键路径 | 可关闭的生产改动、raw CSV/JSON、A/B 统计 | `TEST-008..010`；`DEL-001/003..007` |
| `P4-UPSYNC-02` | `REQ-NET-001`、`REQ-DEL-002..003` | `IF-002`、`IF-011` | 本地 `574d569be...`、官方 `21ef4b218...`、`DEC-007/010` | 前移到 #2092 issuing-vCPU `DeviceContext`/read-write；保留 Auto/resolved FDT/level IRQ/bounded vnet0；把 contest CI 迁入 manifest v3；只保留一个 backend | base/head/dirty manifest、wrong-vCPU/cross-VM/stale/queue/IRQ/CI routing 负例、host/target gate | P4-EVID-01 前置；不产生新 Guest-IP 成功结论 |
| `P4-EVID-01` | `REQ-NET-001..004`、`REQ-DEL-003` | `IF-011` | host-only v1、当前 runtime runner、TEST-011..015 oracle | 新建 Guest-runtime v2 profile/session/validator；结构化 scenario 解析；绑定 QEMU/DTB/READY/capture/PCAP/counters/metrics/fault/status-last | 分开的 `guest_network_smoke_completed`/`guest_network_qualified`；空工件、85/100、错 identity 或残留失败 | P4-SMOKE-02/REL-01 前置；不回写旧 evidence |
| `P4-SMOKE-02` | `REQ-NET-001..003` | `IF-002..007`、`IF-011` | P4-UPSYNC-02 + P4-EVID-01 | 在新 HEAD 依次运行 NIC/ARP/双向 ICMP、UDP、TCP、ICPC happy path | fresh identity-bound smoke bundles | 只关闭 smoke，不关闭 TEST-012/013/015 qualification |
| `P4-REL-01` | `REQ-NET-001..004` | `IF-002..007`、`IF-011` | P4-SMOKE-02、冻结 `DEC-005/008/010` | 10k 双向 UDP+fault、TCP framing/restart、1,000 CONTROL exactly-once/cancel | PCAP/frame/counters/metrics/fault/recovery 与完整 Guest evidence | `TEST-011..015`；`DEL-001..004/006..008` |
| `P5-AI-A/B/C/Q` | `REQ-AI-001..003` | `IF-005..010`、`IF-011` | A 可立即；B 依赖 upsync；C 依赖 P4 smoke；Q 依赖 P4 reliability | 冻结 model/golden；Linux 真推理；Zephyr mailbox/plant/watchdog/真 STATUS；happy path 与 fault qualification | input→MLP→IP→action→feedback、断网安全态、三 seed 基线对比 | `TEST-016..018`；`DEL-001/003..005/007/009/012` |
| `P6-QUAL-01` | `REQ-QUAL-001..002` | `IF-010`、`IF-011` 及所有被测运行接口 | P2–P5 verified 能力 | 新建 `scripts/contest/qualification/` 场景/故障/统计工具 | 数小时组合压力、隔离负例、可复算摘要 | `TEST-019`；`DEL-003..007` |
| `P7-DELIVER-01` | `REQ-DEL-001..003` | `IF-011` | 已验证需求、dirty-tree 变更、证据索引、空的私有比赛仓库 | 官方历史 mirror、本地提交拆分、私有分支 push、文档、复现脚本、`scripts/contest/evidence/validate_test_report.py`、`git format-patch` 离线审查包、演示；不把大镜像/raw log 入 Git，禁止创建或提交 PR | 私有仓库 refs、干净 clone、CI、机器可校验 TEST reports、patch SHA-256/应用 transcript/reviewer、视频、最终 manifest | `TEST-020..022`；全部 `DEL-001..012` |

## 领取与关闭规则

1. 只领取依赖已经满足的最前置工作包；并行工作不得修改同一生产边界或运行同一 live 配置。
2. 动代码前在 `开发交接.md` 写明工作包、输入、预期输出与证据目录；确定 bug 先补能让旧实现失败的回归。
3. 高风险的 DMA、IRQ、虚拟化、协议和公共接口工作必须先更新对应 `DEC/ARC/IF/TEST`，再实现。
4. 工作包关闭需要：最低层合同、适用 Rust 门禁、正式 AArch64 构建、对应层级 runtime、status-last、输入输出哈希、无残留检查和明确未证明边界。
5. `P2-DMA-01` 的一次窄观察不能直接批准 Zephyr VirtIO-net；`P2-DUAL-01` short smoke 不能直接关闭 soak；host ICPC 不能关闭任何真实 Guest IP 项。

## P4-NET-01 固定拆分与门禁

本节是 `DEC-007/008/010` 的实现合同。2026-08-20 起先执行 P4-UPSYNC-02/P4-EVID-01；下方 P4-NET-A..F 保留为网络语义拆分，不再作为当前工作包名称。

| 子门禁 | 必做内容 | 通过条件 | 明确禁止 |
|---|---|---|---|
| `P4-NET-A` | 采用官方 `axvirtio-common`/`axvirtio-net` 和 resolved device graph；以 `DmaGrant` + scoped `DeviceAccess` 完成受控 Guest-memory copy | host 模型/最低层 Rust 测试覆盖配置、grant、注册、生命周期和失败回滚；正式 AArch64 AxBuild 通过 | 不保存 VM-wide accessor/裸指针；不复制 queue parser；不保留旧自研 backend 作为运行时选项 |
| `P4-NET-B` | 复用官方 modern VirtIO-MMIO net、每 Guest 一对 TX/RX queue、固定 MAC、IRQ 注入、bounded polling/notify | 只协商固定三项 `VIRTIO_F_VERSION_1`、`VIRTIO_NET_F_MAC`、`VIRTIO_NET_F_STATUS`；MTU=1500 但不广告 MTU feature；descriptor、queue size、链长、方向和 GPA span 全部 fail closed | v1 禁止 indirect descriptor、mergeable RX buffer、checksum/GSO/TSO/UFO、control VQ 和任何未列出的 feature |
| `P4-NET-C` | 实现两端口软件拷贝 `vnet0` switch | 单帧最多 1514 B（不含 FCS）；每目的端最多 256 帧；仅固定单播、广播及必要 ARP/IPv4；队满采用 drop-newest（tail drop）并计数；重启按 generation 清空旧帧 | 不连接 outer-QEMU NIC/TAP/bridge/NAT；不开放 slot2 passthrough；不让一个 Guest 的 descriptor 直接引用另一 Guest 内存 |
| `P4-NET-D` | 为两 VM 从各自 resolved device graph/Auto resource 合成独立 `virtio,mmio` Guest-FDT 节点；禁止在规范中预写固定 MMIO/INTID | 最终 Guest-DTB、resource claim、resolved graph 和 runtime 枚举四者逐字段一致；原 outer 设备节点不得同时 passthrough | 不硬编码旧 `0x0a000200/400`、INTID 49/50；不复用 shared Host PL011；不从 outer FDT 节点存在推导 frontend 已实现 |
| `P4-NET-E` | Linux/Zephyr 固定地址配置、内部 capture stream 与 runner | `TEST-011` 的 NIC→ARP→ICMP 顺序通过；capture 含 frame bytes、方向、单调时间、长度/hash、generation，可无损导出 PCAP | Host ping、host loopback、静态 MAC、只打印 marker 均不能升级为 Guest IP |
| `P4-NET-F` | UDP/TCP/ICPC 与故障注入 | 依次通过 `TEST-012..015`，且所有状态、动作和 PCAP/frame transcript 同 session 绑定 | TCP 不能与 UDP 同时控制 actuator；不得用共享内存、console、HyperCall 或 vsock 代替主链路 |

`P2-DMA-01` 的 `MapReserved`/virtio-blk 受控字节观察仍按既定顺序先做，用于验证 Host reservation、Guest/HPA 绑定和 evidence runner；它不是 `P4-NET-A..F` 的实现，也不会解除 outer-QEMU `blocked_dma_console`。P4 采用软件拷贝 backend 后，Guest frame 只经验证后的 copy-in/copy-out 跨边界，不依赖 passthrough device 对 Guest GPA 发起 DMA。

## 固定执行链

```text
P2-DMA-01
  -> P2-DUAL-01 short smoke
  -> P2-SOAK-01 >= 1800 s
  -> P4-UPSYNC-02 official DeviceContext + CI v3 migration
  -> P4-EVID-01 -> P4-SMOKE-02
  -> [P4-REL-01 || P5-AI-A/B]
  -> P5-AI-C -> P5-AI-Q
  -> P3-TRACE-FEAS-01 -> P3-RT-01 A/B
  -> P6-QUAL-01
  -> P7-DELIVER-01
```

前一门禁失败时保留 `failed-attempt` 并修复该层；不得通过删除测试、改低证据等级或启用 passthrough 绕过。

## P3-RT-01 固定选择门禁

P3 不预先凭经验挑一个“大概率有效”的优化，也不开放重写调度器。开发者先实现同钟分段采集：Guest timer expiry → virtual IRQ enqueue → owning vCPU wake → vCPU re-entry → Guest handler marker，代码边界固定在 `virtualization/axvm/`、`arm_vcpu/`、`arm_vgic/` 及 `scripts/contest/rt/`。

在原生 Zephyr、AxVisor 空载与 Linux CPU 压力下各完成 3 组配对基线后，统计器按冻结规则自动排名：只有在三组中均稳定贡献总 P99.9 的 `>=15%`，或绝对贡献 `>=20 us` 的路径可进入生产修改；最多选前两条。每项修改必须可由独立 feature/config 开关关闭，先过最低层功能/安全测试与 AArch64 build，再用完全相同输入做 A/B。若没有路径达到门槛，工作包输出“没有数据支持生产优化”及基线报告，不得通过更换指标、只报均值或事后挑场景制造改善。
