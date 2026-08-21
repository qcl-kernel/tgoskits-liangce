# P4-NET-01 实现级开发文档：mediated VirtIO-net 与 Guest IP

## 2026-08-20 当前执行覆盖层

> 本轮 pi agent（2026-08-20）已在**新活动分支 `contest/upsync-20260820`**（基点官方
> `21ef4b218ebb74641134238c2050d488c7504249`）完成 **P4-UPSYNC-02 代码前移**（仅本地提交
> `38af5e71b`/`6b68c3087`/`0075e3897`）：virtio_net.rs 在官方 #2092 `read/write`+`dyn DeviceContext`
> API 上重做 Auto/level-IRQ/诊断；create.rs+capabilities 消费 resolved FDT；CI v3 以
> `.github/ci/checks/contest.toml` 接入（static 22，其中 contest 19）。Rust 宿主门禁
> （fmt/axdevice 47/axvirtio 39+18/axvm 295/clippy 10）、upsync 源码合同红绿、
> AArch64 AxBuild（qemu-aarch64-contest-network.toml+linux-smp2-dual+zephyr-smp1-dual）
> 全部通过。**这些仍是 host/static/target 门禁，不是 Linux+Zephyr Guest-IP runtime。**
> 下一张任务卡：P4-EVID-01B（Guest-runtime v2 schema）→ P4-SMOKE-02（新 HEAD fresh
> NIC/ARP/双向 ICMP/UDP/TCP/ICPC）。详细见 `development/current/开发交接.md`。

本节覆盖下文旧 `23a07bfc...`/`P4-UPSTREAM-01` 的“当前/下一步”，但不删除其中仍有效的
VirtIO、地址、队列、失败关闭和验收合同。最近生产代码 checkpoint 为 `574d569be...`，
其后可有 docs-only 提交；已 fetch 官方 `upstream/dev=21ef4b218...`（相对代码 checkpoint
ahead 15/behind 39，当前数以 Git 命令为准）。

### 当前事实

> **2026-08-20 fresh runtime（`contest/upsync-20260820`，21ef 前移代码）**：
> - TEST-011 `phase4-net-test011-fresh4-20260820T004829Z-21ef-6b68c30` =
>   `guest_network_smoke_completed`，`TGOS_LINUX_L3_SMOKE sent=100 received=100 loss=0`；
> - TEST-012 `phase4-net-test012-fresh-…` = `guest_network_smoke_completed`，
>   `TGOS_LINUX_UDP_ECHO sent=100 received=100 loss=0`；
> - TEST-013 `phase4-net-test013-keepalive-…` = `guest_network_smoke_completed`
>   （`TGOS_LINUX_TCP_CONNECTED attempt=0`，keep-alive/重连修复 `615a19d0a` 防止 init 退出）；
> - TEST-015 `phase4-net-test015-plant-*`（`a0d50fb66` Zephyr 真实 plant）=
>   `guest_network_smoke_completed`，`TGOS_LINUX_ICPC_PASS sent=100 verified=100 loss=0`
>   （连续两次；X-P4-NET-EL2-001 已解除）；
> - 均双 READY、strict oracle、final-log 复验、cleanup 通过，qualified=false；
> - 这些证明 21ef 前移的 mediated vnet0 + level-IRQ + Auto 数据面在真实 QEMU 上工作
>   （ICMP/UDP/TCP 冒烟通过），但仍是 narrow smoke，**不是 TEST-011..015 verification**。

- 旧 HEAD 已实际跑通 mediated vnet0 的 ICMP 100/100、UDP echo 100/100、TCP 建连和 ICPC
  CONTROL→ACK+STATUS（最新 `sent=100 verified=85 loss=15`）。它们是 narrow runtime smoke。
- 不得把它们写成 TEST-011..015 verified：2026-08-20 的 EVID-01A 已把 v1 runner 收紧为
  scenario/profile 绑定、双 READY、唯一完整终态、零 smoke loss/完整 ACK、forbidden 与
  final-log 复验，并只发布 `guest_network_smoke_completed/qualified=false`；测试 profile
  仍是 `host_only=true`/`L2 host`，run 的 capture/metrics 不满足本手册资格定义。
- 官方 #2092 把设备访问改为 issuing-vCPU-bound `DeviceContext` 与独立 `read`/`write`；
  #2105 改为 CI manifest v3；#2106 的 host physical-device DMA coherency与本路径的
  Guest-memory `DmaGrant` 是两个边界，均不得被表述为通用 DMA isolation。

### 现在必须按此顺序开发

1. `P4-UPSYNC-02`：固定官方 `21ef4b218...`，手工前移 Auto resource、resolved FDT、level
   IRQ、bounded vnet0 和竞赛应用；保留 issuing-vCPU context，不保存 VM-wide accessor；把
   contest CI 合同迁入 manifest v3。rebase/cherry-pick、WSL、QEMU 需用户明确确认。
2. `P4-EVID-01`：EVID-01A strict smoke 已完成；host-only v1 保留给 fixture，继续新增
   Guest-runtime v2 profile/session。
   runner/validator 必须绑定 QEMU/nonce、两 final DTB/READY、结构化 scenario 计数、frame
   JSONL/PCAP/counters/metrics/fault、hash/status-last/cleanup；终态至少拆分为
   `guest_network_smoke_completed` 与 `guest_network_qualified`。
3. `P4-SMOKE-02`：新 HEAD fresh run，按 NIC/ARP/双向 ICMP→UDP→TCP→ICPC 重建冒烟。
4. `P4-REL-01`：按 TEST-012/013/015 完成 10k 双向/fault/restart 与 1,000 CONTROL
   exactly-once/cancel。85/100、空 capture/metrics 或只命中日志子串一律失败。

下文旧示例中的 `0x0a000200/0x0a000400`、INTID 49/50 只用于解释曾经的硬编码缺陷，
不再是实现常量。当前生产值必须来自每个 VM 的 resolved device graph/Auto resource，并由
final DTB、resource claim、runtime 枚举和 evidence 逐字段绑定。

详细输入、allowed paths、red/green oracle 见工作区
`development/analysis/项目重新审视与执行路线-2026-08-20.md` 与
`development/current/开发交接.md`。完成 P4-SMOKE-02 后允许 P5 happy-path 集成，但
P5 qualification 必须等待 P4-REL-01。

状态：`DEC-010` 路线已冻结；旧官方基线上的 Rust/adapter/runtime smoke 已推进到 `574d569be...`，但当前官方为 `21ef4b218...`，且 evidence 仍不足以关闭完整 TEST-011..015（2026-08-20）。当前工作包：`P4-UPSYNC-02`、`P4-EVID-01`、`P4-SMOKE-02`、`P4-REL-01`；需求：`REQ-NET-001..004`；接口：`IF-002..007`、`IF-011`；验收：`TEST-011..015`。

本文是 P4 的实施入口。开发者不需要重新选择直通、IOMMU、自研旧栈或官方栈；唯一 v1 路线是官方 `axvirtio-common`/`axvirtio-net` + resolved device graph + scoped `DmaGrant`，其上增加二端口 `vnet0` 竞赛策略与 Linux/Zephyr adapter。数值和安全边界以 [`contest-spec/contracts.md`](contest-spec/contracts.md) 为准。

### 2026-08-17 路线重构历史 checkpoint

当前 HEAD `6692559e...` 的旧候选已通过 axdevice/axvm host tests、Clippy、AArch64 target check 和正式 AxBuild；r41 双 VM boot 后在 console 首帧路径发生 Host EL2 data abort，未取得 NIC/ARP/ICMP。2026-08-17 的官方 `upstream/dev=23a07bfc...` 已提供第一方 `axvirtio-common`、`axvirtio-net`、device graph、DmaGrant、双 Guest switch 和多项 wake/console/race 修复，且比当前分支多 198 个官方提交。因此旧候选只作为迁移输入，r32-r41 保持 immutable，不再在其上扩大生产功能。

## 0. P4-UPSTREAM-01：历史迁移入口（当前由顶部 P4-UPSYNC-02 覆盖）

1. 记录 `current_head`、`upstream_dev`、分叉计数和 dirty patch hash；禁止先 rebase/cherry-pick 再补记录。
2. 生成逐文件迁移矩阵：旧 `axdevice/src/virtio_net`、Guest-memory、IrqSink、FDT、poll/wake 分别映射到官方 `axvirtio-common`、`axvirtio-net`、device graph、`DmaGrant`、wired IRQ/VM wake；标明“复用、竞赛适配、删除候选、仍缺”。
3. 在隔离分支/worktree 中通过官方双 ArceOS network demo 的 host/target 门禁；其 QEMU run 需用户明确确认，且只作为上游能力检查。
4. 生产配置必须只实例化官方 backend。竞赛代码只保留固定 MAC/IP、两端口有界策略、capture/validator、Linux/Zephyr adapter 和 ICPC；禁止以 feature 同时保留旧/新两套 backend。
5. 先构建 Linux/Zephyr adapter，再运行 TEST-011。新的 runner 将 console 归属门禁与 NIC/ARP/ICMP oracle 分开；任一仍须 fail closed，但 console sink 故障不得遮蔽已绑定的网络事件。
6. P4-UPSTREAM-01 完成不等于 Guest-IP。只有本节完成后，下面 P4-NET-A..F 才成为可执行任务。

### 0.1 固定迁移矩阵

| 当前候选/关注点 | 官方生产落点 | 处理决定 |
|---|---|---|
| `virtualization/axdevice/src/virtio_net/{mmio,queue}.rs` | `virtualization/axvirtio-common/`、`virtualization/axvirtio-net/` | 不再并行维护；只迁移竞赛专属负例或策略 |
| `virtualization/axdevice/src/guest_memory.rs`、`virtualization/axvm/src/vm/guest_memory.rs` | `virtualization/axdevice_base/src/device.rs` 的 `DmaGrant`/`DeviceAccess`，以及 `axvirtio-common` 的 `GuestMemory` 边界 | 采用 scoped access；禁止保存 VM-wide accessor |
| 自建 factory/prepare/FDT glue | `virtualization/axdevice/src/graph/`、`book/design/axvisor-resolved-device-graph.md`、`os/axvisor/src/virtio_net.rs` | 迁移到 resolved graph；保留固定 MAC/MMIO/IRQ 配置 adapter |
| 自建 AArch64 `IrqSink`/peer wake | 官方 wired IRQ、`VmInterruptSender`/`notify_vm_vcpu` 与 DMA-pollable device | 使用官方生命周期；竞赛只补所需断言和观察计数 |
| 自建 `switch.rs` | `axvirtio-net/src/switch.rs` + AxVisor bounded ingress glue | 复用 switch primitive；固定二端口、256-frame、MAC/IPv4 policy 由竞赛 adapter 实现 |
| `scripts/contest/network/`、Linux/Zephyr应用、ICPC | 无等价竞赛实现 | 保留并适配；这是本项目真正需要新增的部分 |

### 0.2 接手者的只读确认命令

```powershell
git status --short --branch
git rev-parse HEAD
git rev-parse upstream/dev
git rev-list --left-right --count upstream/dev...HEAD
git show upstream/dev:virtualization/axvirtio-net/README.md
git show upstream/dev:docs/design/axvisor-virtio-net.md
git show upstream/dev:os/axvisor/src/virtio_net.rs
```

预期参考 SHA 为 `23a07bfcf17863a5eeaae7536d7c0a55c898f50e`。若已变化，先更新 `DEC-010` 的观察快照和迁移风险，不能静默改基线。任何 rebase/cherry-pick、WSL/QEMU 或远端写入仍按工作区规则取得用户明确确认。

### 0.3 2026-08-17 迁移执行状态与阅读优先级

独立工作树 `F:\project\泉城实验室\tgoskits-upstream-integration` 已建立，分支
`contest/upstream-20260817`。官方 bytes/SHA preflight、逐文件迁移矩阵和首批
network profile/fault/capture/session host 合同已通过。迁移矩阵的权威路径为
`development/analysis/P4-UPSTREAM-01迁移矩阵-2026-08-17.md`；发布镜像不复制
`development/analysis/`，因此此处保留权威路径而不建立会断裂的相对链接。

当前新工作树已经形成选择性迁移提交：`c8a6b86de` 绑定官方 baseline/P4 host
network 合同，`84f10cfcc` 迁入 P2/P3/P5 host/static 包，`fe78e63b2` 发布受控
开发文档镜像，`2dbb785d6` 迁入 portable ICPC v1 core，`7296381cc`（2026-08-18）
关闭 X-P4-RES-001：`os/axvisor/src/virtio_net.rs` 的 MMIO/IRQ requirement 改为
`ResourceRequest::Auto`，axdevice 新增回归 `fixed_linux_root_block_and_auto_virtio_net_coexist`，
Python 源码合同 `check_axvisor_virtio_net_auto_resources.py` 接入 CI；fmt、axdevice 47、
axvirtio-common 39、axvirtio-net 18、xtask clippy 4/4、正式 AArch64 AxBuild（含官方双
ArceOS demo guest 镜像构建）全部通过。注：官方结构不存在宿主 `cargo test -p axvisor`
路径（依赖 cfg 门禁 + clippy 流程 unsupported），原补丁内两项 unit test 已等价迁移到
axdevice 宿主测试与源码合同。

2026-08-18 追加：官方 demo 首次 QEMU 运行暴露官方 FDT 硬编码缺口——
`install_configured_virtio_net/blk` 固定写 `0x0a000000/0x0a000200`/INTID 48/49，不消费
resolved graph；Auto 解析（`0x0b000000+`）后 Guest 探测旧地址报 `no device was found`。
本地提交 `cf9f7e0bd` 修复：FDT 安装消费 `ResolvedVirtioDevice`（来自 VM 不可变 device
graph，与运行时注册同源），节点路径随解析基址，配置设备无解析结果 fail closed。修复后
官方 demo 双 Guest 探测 `virtio_mmio@b000000` 并交换 64 KiB（checksum 匹配），
`VM1/VM2_VIRTIO_NET_PASS`；axvm 宿主测试 279+1、clippy 8/8、AArch64 AxBuild 全绿。
这仍是上游能力检查，不是竞赛 Linux+Zephyr Guest-IP evidence。上述提交和 host 合同均
不构成 Guest NIC/IP/runtime 证据。

本手册第 6–9 节保留旧候选的风险、负例和语义 oracle，便于确认迁移没有丢失安全边界；
其中的 `ScopedGuestMemory`、run-scoped 自建 switch、手写 MMIO/queue、AArch64
`IrqSink`、`emu_devices` 和手工 FDT installer **不再是生产实现指令**。与第 0 节或
官方 resolved graph 冲突时，一律以第 0 节和迁移矩阵为准。实现者不得因为旧段落使用
“必须”措辞而复制第二套 backend。

## 1. 开始条件与停止条件

### 1.1 开始编码前必须满足

1. `P2-DUAL-01` 的 `TEST-006` 已在当前代码、当前镜像上得到 `L5 dual-Guest` 成功包。
2. `P2-SOAK-01` 的 `TEST-007` 已得到同一 collector 的 `>=1800 s` 成功包。
3. Linux VM1、Zephyr VM2 的 final DTB、镜像、VM TOML、QEMU identity 和独立 console 均已绑定。
4. 工作树的当前改动与所有者已登记；不得覆盖其他开发者的 dirty-tree 修改。
5. 已完整阅读仓库 `AGENTS.md`、`docs/guideline/code-quality.md` 和 `docs/guideline/feature-development.md`。本工作包修改虚拟设备、Guest memory、IRQ 和公共接口，属于高风险功能开发。
6. `P4-UPSYNC-02` 已关闭，生产图中只有当前官方 axvirtio/device-graph backend。

P2 的 virtio-blk 字节效果观察不是 P4 的实现依赖。即使 `TEST-005` 通过，也不能放开 outer-QEMU 网卡直通；P4 只依赖稳定双 Guest、资源归属和独立 console。

### 1.2 任一条件出现时立即停止扩大实现

- final DTB 与 VM TOML 的 MMIO/IRQ/MAC 不一致；
- 官方 `DmaGrant`/scoped `DeviceAccess` 无法在不保存 VM-wide accessor 的前提下完成当前操作；
- 需要向设备暴露 HPA、宿主裸指针或长期 Guest slice 才能继续；
- 需要恢复 outer `-netdev`、TAP、bridge、NAT、host proxy 或 slot2 passthrough 才能 ping；
- 一个 frontend 能访问另一 VM 的 GPA、used ring 或 IRQ；
- reset/destroy 后旧 callback、旧 frame 或旧 accessor 仍可生效；
- 为通过测试需要删减 descriptor、跨 VM、queue overflow 或 stale-generation 负例。

停止后保留 `failed-attempt`，更新 `development/current/阻塞.md`，不要切换技术路线。

## 2. 冻结结果与非目标

P4 完成时必须得到以下链路：

```text
Linux virtio_net driver
  -> VM1 VirtIO-MMIO frontend
  -> checked VM1 guest-memory copy-in
  -> Host-owned bounded vnet0 port 0
  -> Host-owned bounded vnet0 port 1
  -> checked VM2 guest-memory copy-out
  -> VM2 VirtIO-MMIO frontend
  -> Zephyr virtio Ethernet driver
```

反方向完全对称。`vnet0` 没有第三个端口，也没有 Host uplink。

明确非目标：

- 不实现 PCI transport、packed ring、indirect descriptor、event index、mergeable RX、multiqueue、control VQ、VLAN、checksum/GSO/TSO/UFO；
- 不实现 MAC 学习、DHCP、DNS、默认网关、IPv6、NAT、TAP 或宿主路由；
- 不把 ICPC、AI 或控制策略放入 hypervisor；
- 不声称 IOMMU/SMMU、硬件 DMA 或通用 DMA isolation；
- 不用 console、共享内存、HyperCall、裸 MMIO 或 vsock 替代 Guest IP；
- 不创建或提交 PR；本阶段只形成可审查提交和证据输入。

### 2.1 非结论

设计、配置、host test、目标构建、单 Guest 或双 Guest 存活都不是 P4 完成证据。只有 `TEST-011..015` 的真实双 Guest IP 包才能形成 `L7 Guest-IP`；P4 仍不能证明 AI 闭环、实时改善、硬件 DMA、IOMMU/SMMU 或通用隔离。

### 2.2 规范与上游参照

- 项目数值、feature 子集、queue/descriptor 上限、MAC/IP/端口、失败关闭和证据语义以 [`contest-spec/contracts.md`](contest-spec/contracts.md) 为唯一验收合同；它们比通用实现允许范围更窄。
- [OASIS VirtIO 1.2](https://docs.oasis-open.org/virtio/virtio/v1.2/virtio-v1.2.html) 只用于核对 modern VirtIO-MMIO、split virtqueue、net header、状态机和 notification 的兼容语义；不得据此打开本文明确禁用的 packed/indirect/event-index/offload/multiqueue 等能力。
- [Zephyr v4.4.0 Ethernet driver tree](https://github.com/zephyrproject-rtos/zephyr/tree/v4.4.0/drivers/ethernet) 及同 tag 的 Kconfig/Devicetree binding 只用于确认锁定 Guest 驱动需要的接口和符号；不能升级 tag、换 board，或让上游默认地址取代本文固定配置。

## 3. 冻结常量

| 项目 | Linux / VM1 | Zephyr / VM2 |
|---|---:|---:|
| VM ID | `1` | `2` |
| `vnet0` port | `0` | `1` |
| frontend GPA | resolved graph 从 AArch64 auto MMIO pool 分配并写入 final DTB | 同左；可与 VM1 相同，因为属于独立 Guest 地址空间 |
| MMIO size | `0x200` | `0x200` |
| GIC INTID | resolved graph 从该 VM wired IRQ domain 分配 | 同左；最终值由 final DTB/runtime report 绑定 |
| FDT SPI cell | `resolved INTID - 32` | `resolved INTID - 32` |
| MAC | `02:00:00:00:00:01` | `02:00:00:00:00:02` |
| IPv4 | `10.77.0.1/24` | `10.77.0.2/24` |
| connected route | `10.77.0.0/24` | `10.77.0.0/24` |
| UDP | `46000` | `46000` |
| TCP fallback | active connect | listen `10.77.0.2:46001` |

共同常量：

- VirtIO-MMIO transport version `2`，network device ID `1`，vendor ID `0x554d4551`；
- queue 0 = RX，queue 1 = TX；`QueueNumMax=256` 且 driver 必须选择 `256`；
- descriptor chain 最多 `32` 项；virtio-net header 固定 `10` 字节；
- Ethernet frame 不含 FCS，长度 `14..1514`，MTU `1500`；
- 每目的端 FIFO 容量 `256` 帧，满时 `drop-newest`；
- 只协商 `VIRTIO_F_VERSION_1`、`VIRTIO_NET_F_MAC`、`VIRTIO_NET_F_STATUS`；MTU 固定 1500，但不广告 `VIRTIO_NET_F_MTU`；
- UDP packet 最大 `1060` 字节，即 36-byte ICPC header + 1024-byte payload；禁止 IP 分片；
- 无默认路由、网关、DNS、Host bridge、NAT 或 proxy。

VM TOML 不再携带 MMIO、IRQ、port、MTU 或 queue 的裸数字；使用官方开放设备配置：

```toml
[[devices.virtual]]
id = "virtnet0"
model = "virtio-net"
guest_mac = [0x02, 0x00, 0x00, 0x00, 0x00, 0x01] # VM2 末字节为 0x02
```

`VirtualDeviceRequest` 只保存 ID、model 与设备语义选项；资源由 graph planner 签发。
当前官方 glue 固定 `0x0a000000/INTID 48`，与 Linux root block 冲突；X-P4-RES-001
确定采用同一 model 的 `ResourceRequest::Auto`，禁止以用户选项重新暴露 MMIO/IRQ。

## 4. 当前源码事实、上游能力与迁移缺口

生产事实以官方 `23a07bfc...` 为准：portable MMIO/queue/net model 位于
`axvirtio-common`/`axvirtio-net`；AxVisor glue 位于 `os/axvisor/src/virtio_net.rs`；
Guest memory 通过 scoped `DeviceAccess` + `DmaGrant`；RX 由 DMA-pollable device
推进；FDT 与 runtime 共用 resolved resource graph。当前唯一先修缺口是 Linux root
block 与官方 net 固定 MMIO 冲突，方案为 Auto resource planning。

以下表格只描述旧 `6692559e...` 候选为什么不再继续，不代表新基线的待实现 API：

| 现有路径 | 可复用内容 | P4 缺口 |
|---|---|---|
| `virtualization/axvm-types/src/lib.rs` | `EmulatedDeviceType::VirtioNet`、`EmulatedDeviceConfig` | 无 factory/backend |
| `virtualization/axdevice/src/factory.rs` | `DeviceFactoryRegistry`、`DeviceBuildContext`、`IrqResolver` | context 目前只有 IRQ |
| `virtualization/axdevice/src/registration.rs` | 原子 `DeviceBundle`、`PollableDeviceOps` | 生产路径尚未调用 pollable device |
| `virtualization/axdevice/src/device.rs` | MMIO/IRQ 冲突检测、pollable registry | 无 VirtIO-net 类型 |
| `virtualization/axvm/src/vm/mod.rs` | `read_from_guest` / `write_to_guest` | 无 VM-scoped、generation-bound 能力 |
| `virtualization/axvm/src/vm/prepare/devices.rs` | factory 构建设备 | 未注入 memory/switch，且默认 factory 按 VM 创建 |
| `virtualization/axvm/src/irq/mod.rs` | VM-local `InterruptFabric` / `IrqLine` | frontend 尚未解析并持有独立 IRQ |
| `virtualization/axvm/src/arch/aarch64/vm.rs` | vCPU run 前会排空 VM runtime IRQ queue；`VmArchVcpuOps::inject_interrupt` 可注入虚拟 INTID | 默认只创建无 backend 的 `InterruptFabric`；AArch64 尚无 `IrqSink`，当前只有 RISC-V 实现可参考 |
| `virtualization/axvm/src/boot/fdt/core/console.rs` | 合成 VM-local FDT 节点的可复用模式 | 无 `virtio,mmio` network 节点 |
| `os/axvisor/src/manager.rs` | 默认 VM 生命周期、专用 host CPU 选择模式 | 无 run-scoped switch 和 device poller |
| `scripts/contest/icpc/` | 36-byte ICPC v1 host 合同 | 无 Guest socket/payload 适配 |

必须特别处理两个事实：

1. 当前 `vm.prepare()` 会为每个 VM建立默认 factory registry；若把 switch 放在 factory 内，会错误地产生两个彼此不通的 switch。
2. `iter_pollable_dev()` 目前只有测试消费者；只注册 `PollableDeviceOps` 不会让队列运行。

## 5. 旧候选文件清单与迁移边界

本节原文件清单描述 2026-08-16 旧候选，现改为**迁移清单**：queue/MMIO/device model 不再在这些旧路径新增实现，目标是删除或机械适配到官方 crate；`scripts/contest/network/{fault_profile.py,capture.py,validate_network_session.py}` 与 Guest 应用仍是竞赛专属内容，继续维护，不能创建同义工具。

```text
virtualization/axdevice/src/
  guest_memory.rs                         # 新建：ScopedGuestMemory capability
  virtio_net/
    mod.rs                                # 新建：frontend、factory、统计、reset
    mmio.rs                               # 新建：VirtIO-MMIO v2 寄存器/状态机
    queue.rs                              # 新建：split-ring/descriptor 校验与 used 更新
    switch.rs                             # 新建：二端口 bounded vnet0
    tests.rs                              # 新建：最低层正负测试
  factory.rs                              # 修改：DeviceBuildContext 注入 memory/switch
  lib.rs                                  # 修改：模块与有意 public surface

virtualization/axvm/src/vm/
  guest_memory.rs                         # 新建：Weak<AxVM> adapter、region/generation 校验
  mod.rs                                  # 修改：VM generation 与 accessor 构造
  prepare/devices.rs                      # 修改：把 capability 传给 DeviceBuildContext
virtualization/axvm/src/arch/aarch64/
  irq.rs                                  # 新建：VM-local runtime-queue IrqSink
  mod.rs                                  # 修改：声明 irq 模块
  vm.rs                                   # 修改：默认 prepare 安装 AArch64 IrqSink
virtualization/axvm/src/boot/fdt/core/
  network.rs                              # 新建：逐 VM 合成 virtio,mmio 节点
  mod.rs                                  # 修改：调用 network installer

os/axvisor/src/
  manager.rs                              # 修改：每次 AxVisor 启动唯一 switch/poller 生命周期
  config.rs                               # 修改：两个 VM 使用同一 run-scoped services
  mediated_net.rs                         # 新建：poller/capture drain 与失败关闭策略

configs/contest/qemu-aarch64-linux-zephyr-dual.toml
os/axvisor/configs/board/qemu-aarch64-contest-network.toml # 新建：P4/P5 正式 AxBuild profile
os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml
os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml
configs/contest/zephyr/qemu-cortex-a53-vnet0.conf       # 新建
configs/contest/zephyr/qemu-cortex-a53-vnet0.overlay    # 新建
configs/contest/network/
  test-011-v1.json                      # 新建：ARP/ICMP profile
  test-012-v1.json                      # 新建：UDP 正常+故障 profile
  test-013-v1.json                      # 新建：TCP fallback profile
  test-015-v1.json                      # 新建：Guest ICPC profile

apps/contest/linux-ai-controller/          # P4 建网络/ICPC骨架，P5 加模型
apps/contest/zephyr-control/                # P4 建网络/ICPC骨架，P5 加 plant/control

scripts/contest/network/
  prepare_linux_network_rootfs.py        # 新建：fresh ext4 + P4 app/config
  prepare_zephyr_network_image.py        # 新建：锁定 Zephyr/SDK build + manifest
  run_guest_network.py                     # 新建：TEST-011..015 单一运行入口
  fault_profile.py                         # 已有 host 合同：扩展确定性 frame/packet fault plan
  capture.py                               # 已有 host 合同：扩展内部 capture -> PCAP
  validate_network_session.py              # 已有 host 合同：扩展真实 IF-011/status-last session

scripts/test/
  check_contest_network_contract.py         # 已有：host fault/capture/validator 合同
  check_axvisor_mediated_virtio_net.py      # 新建：host/fixture 合同入口
  check_contest_guest_network_apps.py       # 新建：两端构建/黄金报文合同
  check_contest_network_runner.py           # 新建：runner 成功/失败 fixture
```

如实际实现必须增加文件，应放在上述模块内部，并在交接记录写明原因；不得把 queue parser 放到 `os/axvisor`，也不得复制第二份 ICPC wire 实现。

## 6. P4-NET-A：旧候选能力/factory oracle（禁止复制实现）

### 6.1 先写失败测试

最低层测试首先覆盖：

- `VirtioNet` 无 memory capability 时 factory 失败；
- 无 shared switch、错误 port、重复 port 或重复 factory 时失败；
- accessor 访问另一 VM GPA、未映射 GPA、跨 region、`gpa+len` 溢出、空/过长 buffer 和错误方向时失败；
- reset 后旧 generation accessor 失败；
- factory 构建中任一资源注册失败时，MMIO、IRQ 和 pollable registration 全部回滚。

### 6.2 固定 capability surface

`axdevice` 定义能力接口，`axvm` 实现；依赖方向保持 `axvm -> axdevice`，禁止 `axdevice -> axvm`：

```rust,ignore
pub trait ScopedGuestMemory: Send + Sync {
    fn vm_id(&self) -> usize;
    fn generation(&self) -> u64;
    fn read(&self, gpa: GuestPhysAddr, dst: &mut [u8]) -> DeviceManagerResult;
    fn write(&self, gpa: GuestPhysAddr, src: &[u8]) -> DeviceManagerResult;
}
```

实现规则：

1. `axvm` adapter 只持有 `Weak<AxVM>`、捕获的 `vm_id`、`generation` 和只读授权 region 表。
2. 每次访问先用 `checked_add`，再按 4 KiB 页证明全部字节位于当前 VM 已准备 region 且操作方向允许，最后调用 `AxVM::read_from_guest` / `write_to_guest`。
3. 不返回 HPA、HVA、裸指针、`&[u8]` 或 `&mut [u8]`；一次调用结束后不保留 Guest 地址映射。
4. `AxVM` 每次 prepare/reset/destroy 都推进非零 `u64 generation`；旧 adapter 即使 `Weak` 仍能升级，也必须因 generation 不匹配失败。
5. generation 溢出不得回绕为有效值，必须拒绝新的 prepare。

`DeviceBuildContext` 在保留 `IrqResolver` 的同时增加：

```rust,ignore
guest_memory: Option<Arc<dyn ScopedGuestMemory>>,
mediated_net: Option<Arc<MediatedNetSwitch>>,
```

Console 等不需要这些能力的 factory 不受影响；`VirtioNetFactory` 必须要求两者存在。

### 6.3 run-scoped services

- `AxvmManager::new()` 创建一次 `MediatedNetSwitch` 和唯一 `run_epoch`，两个 VM prepare/reset 均复用同一 `Arc`。
- 不能在每个 `default_device_factories()` 中各建 switch。
- `AxVM::reset()` 路径必须复用已登记的 device services，不能退回只有 IRQ 的默认 prepare。
- VM stop/reset/destroy 先阻止新提交，再 unregister port、清空 FIFO/未完成 chain/IRQ，最后推进 generation。
- switch 或 poller 的 terminal failure 必须停止受影响 VM，并在 Host log 中输出可由 evidence parser 唯一识别的错误；不得静默停转。

### 6.4 P4-A 门禁

正式 profile 固定新建为 `os/axvisor/configs/board/qemu-aarch64-contest-network.toml`：

```toml
features = [
    "ax-driver/virtio-blk",
    "fs",
    "guest-fdt-evidence",
    "stage2-hpa-evidence",
]
log = "Info"
target = "aarch64-unknown-none-softfloat"
vm_configs = []
```

mediated frontend/switch 是本 profile 的生产代码，不允许依赖开发机默认 feature 才被链接。若实现确需新增 Cargo feature，名称固定为 `mediated-virtio-net`，必须沿 `axvisor -> axvm -> axdevice` 转发并加入上表，同时补一条 feature 关闭时拒绝 `VirtioNet` 配置的测试。

```bash
cargo test -p axdevice
cargo xtask clippy --package axdevice --package axvm
cargo fmt --all -- --check

cd os/axvisor
cargo xtask build \
  --config configs/board/qemu-aarch64-contest-network.toml \
  --vmconfigs configs/vms/qemu/aarch64/linux-smp2-dual.toml \
  --vmconfigs configs/vms/qemu/aarch64/zephyr-smp1-dual.toml
```

必须另外运行正式 AArch64 AxVisor build；host test 不能替代目标构建。只有能力测试、生命周期测试、回滚测试和目标构建全部通过，才进入 P4-B。

## 7. P4-NET-B：旧候选 MMIO/queue oracle（迁移为官方回归）

### 7.1 MMIO 寄存器

`mmio.rs` 只实现以下 transport 寄存器，未列出的读返回规范允许值，未列出的写失败或忽略必须由测试固定，不能随调用路径变化：

| offset | register | 行为 |
|---:|---|---|
| `0x000` | MagicValue | `0x74726976` |
| `0x004` | Version | `2` |
| `0x008` | DeviceID | `1` |
| `0x00c` | VendorID | `0x554d4551` |
| `0x010/014` | DeviceFeatures/Sel | 只暴露四个冻结 feature |
| `0x020/024` | DriverFeatures/Sel | 未提供 feature 导致 `FEATURES_OK` 被清除 |
| `0x030` | QueueSel | 仅 `0`、`1` |
| `0x034` | QueueNumMax | `256` |
| `0x038` | QueueNum | 只接受 `256` |
| `0x044` | QueueReady | `0/1`，重复 ready 或不完整地址失败 |
| `0x050` | QueueNotify | 调度对应 queue 的有界处理 |
| `0x060/064` | InterruptStatus/Ack | used=bit0；ack 后无 pending 则 deassert |
| `0x070` | Status | 实现标准 reset/ACK/DRIVER/FEATURES_OK/DRIVER_OK/FAILED |
| `0x080..0x0a4` | desc/avail/used low/high | 组合 64-bit GPA，ready 后不可改 |
| `0x0fc` | ConfigGeneration | reset/config 变化时推进 |
| `0x100..` | net config | 6-byte MAC、status UP、MTU 1500 |

所有寄存器访问必须校验宽度、对齐和状态顺序。`status=0` 是完整 device reset；非法状态转换将设备置为 `DEVICE_NEEDS_RESET`，不能 panic。

### 7.2 split queue 处理顺序

TX：

1. 快照当前 session/generation、queue 地址、`avail.idx` 和 last index；
2. 逐项读取并校验 descriptor：索引范围、NEXT 环、最多 32 项、方向为 readable、全部 GPA 可读；
3. 前 10 字节必须是 v1 不 offload 的 virtio-net header；拒绝非零 GSO/checksum 字段；
4. 验证 frame 总长 `14..1514`、源 MAC 等于本端固定 MAC、EtherType/目标符合 switch policy；
5. 一次性复制到 Host-owned frame，不保留 Guest slice；
6. 尝试 enqueue；队满时 drop-newest 并计数，但该 TX descriptor 仍正常完成；
7. 在 generation 再次匹配后写 used element/index；
8. 若 guest 未设置 `VIRTQ_AVAIL_F_NO_INTERRUPT`，设置 interrupt status used bit，再 assert 本 frontend 的 level IRQ。

RX：

1. 只有 switch 中存在当前 generation frame 且 RX avail 非空才处理；
2. 先验证完整 writable chain、无环、<=32 项、capacity >= `10 + frame_len`；
3. 写入全零 10-byte header 和 frame；全部 copy 成功后才写 used；
4. capacity 不足或 GPA 非法时不得部分写，不得弹出 frame；设备进入 `DEVICE_NEEDS_RESET` 并记录具体 counter；
5. 成功后弹出该 frame，更新 used，再按本 queue suppression 状态触发本 VM IRQ。

不得在持有 switch lock 时访问 Guest memory 或注入 IRQ。frontend lock 与 switch lock不得嵌套；代码中记录锁顺序和中断上下文假设。

### 7.3 必须的 queue 负例

- descriptor index 越界、NEXT 成环、链长 33；
- queue size 128/255/257；
- TX descriptor 带 WRITE、RX descriptor 不带 WRITE；
- 10-byte header 截断、非法 offload 字段；
- frame 13、1515 字节；
- GPA 未映射、跨 region、末地址溢出；
- driver 修改 ready queue 地址；
- stale generation 在 copy 前、copy 后、used 前分别到达；
- used 更新成功但错误 VM IRQ 被触发；
- reset 时 queue 中有未完成 TX/RX。

这些测试必须检查另一 VM 的内存、used index、IRQ count 和已排队 frame 均未变化。

## 8. P4-NET-C：竞赛二端口策略（扩展官方 switch，不重写 backend）

`switch.rs` 使用固定两个端口和预分配有界 FIFO：

```rust,ignore
struct HostFrame {
    len: u16,
    bytes: [u8; 1514],
    source_port: u8,
    source_generation: u64,
    monotonic_ns: u64,
}
```

转发规则按以下顺序执行：

1. 拒绝 source port/generation/session 不匹配；
2. 拒绝源 MAC spoof；
3. 拒绝 VLAN EtherType、IPv6 和长度不合法；
4. 对端固定单播、广播以及 IPv4 multicast只发往对端；
5. 未知单播和本端回环丢弃；
6. 对端 FIFO 已有 256 帧时 drop-newest，旧帧不被覆盖；
7. 每一种拒绝/丢弃都有独立 `u64` counter，counter 溢出饱和并报告，不回绕。

capture ring 是只读镜像，不是第三端口：只在完整 copy-in 和 L2 校验通过后记录 frame bytes、ingress port、generation、方向、单调时间、长度和 SHA-256；capture 满时可以丢 capture 并单独计数，但不能改变转发结果。导出器必须把丢失数量写入 evidence；发生 capture loss 的运行不能关闭需要完整逐包对账的 `TEST-012/015`。

reset/unregister 的顺序固定为：阻止新提交 → 推进 port generation → 清空该端 FIFO 和旧 capture callback → 注销 port → 释放 frontend。重注册相同 VM/port 使用新 generation，旧帧不得送达。

## 9. P4-NET-D：旧候选配置/FDT/IRQ说明（以本节 9.0 取代）

### 9.0 官方基线目标

- 两份 VM TOML 都只增加 `[[devices.virtual]] model="virtio-net"` 和固定 Guest MAC；
- `VirtioNetModel` 对 MMIO 与 wired IRQ 声明 `ResourceRequest::Auto`，资源池避开
  passthrough root block、Guest RAM、控制器和保留区；
- final FDT 由 `DeviceFirmwareSpec` 与 resolved resources 自动生成，不再维护
  `boot/fdt/core/network.rs`；
- IRQ 使用官方 controller/`IrqLine`/DMA-poll/`notify_vm_vcpu` 生命周期，不再增加
  AArch64 `IrqSink`；
- final DTB、runtime resource report 和 capture/session 必须记录相同 MMIO/INTID/MAC。

下面 9.1–9.2 的固定 `emu_devices`、手工 FDT 节点和自建 sink 是旧候选审计材料，
不得照搬到新工作树；仅保留“不能使用 Host IRQ、不能跨 VM、错误必须 fail closed”的
安全 oracle。

### 9.1 VM TOML 目标条目

两份 v1 TOML 保持现有 `interrupt_mode = "passthrough"`，只因为 Guest GIC 仍由既有 GPPT controller 路径提供；这不把 VirtIO-net 变成直通设备。INTID 49/50 只进入第 9.2 节 VM-local software sink，不能出现在 `pass_through_spis`、Host GIC route 或 outer resource claim 中。若未来要把整个 GIC 改为 `interrupt_mode="emu"`，必须作为独立架构工作包验证两 Guest timer/IPI/设备 IRQ，不能在 P4 实现中顺手切换。

Linux 删除 `/virtio_mmio@a000200` passthrough，并在 `emu_devices` 加入：

```toml
["linux-vnet0", 0x0a00_0200, 0x200, 49, 0xe2, [1, 0, 0x0200, 0x00000001, 1500, 256]]
```

Zephyr 继续排除全部 outer VirtIO slot，并在 `emu_devices` 加入：

```toml
["zephyr-vnet0", 0x0a00_0400, 0x200, 50, 0xe2, [1, 1, 0x0200, 0x00000002, 1500, 256]]
```

outer `configs/contest/qemu-aarch64-linux-zephyr-dual.toml` 必须删除两个 `-netdev` 与两个 `virtio-net-device` 参数。P4 runner 启动前再次审计最终 argv；只改模板而最终 argv 仍含网卡时失败。

### 9.2 合成 FDT

`network.rs` 仿照 `console.rs` 的 fail-closed、幂等安装模式，为当前 VM唯一 `VirtioNet` 配置创建：

```dts
virtio_mmio@a000200 {
    compatible = "virtio,mmio";
    reg = <0x0 0x0a000200 0x0 0x200>;
    interrupts = <0 17 4>;
    interrupt-parent = <...guest GIC phandle...>;
    dma-coherent;
};
```

Zephyr 节点地址改为 `0x0a000400`，SPI cell 改为 `18`。INTID 49/50 与 SPI cell 17/18 的换算必须在测试中显式断言，禁止混用。

FDT installer 必须：

- 拒绝同 GPA 的非 `virtio,mmio` 节点；
- 移除/拒绝 outer passthrough 节点，不能同时存在两种 ownership；
- 不生成 HPA、物理 DMA、Host IRQ 或 passthrough resource claim；
- 保留并验证 Guest GIC interrupt-parent；
- 重复应用字节稳定；
- 最终 Linux DTB 只有 VM1 frontend，Zephyr DTB 只有 VM2 frontend。

IRQ 使用 `InterruptTriggerMode::LevelTriggered`。used interrupt 只连接 `context.resolve_irq(config.irq_id, LevelTriggered)` 返回的 VM-local `IrqLine`；不调用 Host 物理 IRQ API。

当前 AArch64 默认 prepare 使用 `InterruptFabric::new(...)`，没有 `IrqSink`，因此仅新增 VirtIO-net factory 会在 `resolve_irq` 时失败。必须先在 `arch/aarch64/irq.rs` 增加 `Aarch64VmIrqSink`，并按以下边界接线：

1. sink 只保存 `vm_id`、目标 `vcpu_id=0`、捕获的 VM generation 和每条线的 asserted 状态；不能强持有 `AxVM`，不能把 GPPT `VGicD` 当软件设备 IRQ backend。
2. `set_level(false)` 只撤销该线 asserted 状态；`false→true` 和 `pulse` 通过现有 VM runtime IRQ queue 唤醒 vCPU0，最终由 `VmArchVcpuOps::inject_interrupt(INTID)` 注入 Guest。重复 `true` 不重复排队。
3. 只接受当前 VM 配置中由 emulated device 声明且互不重复的虚拟 SPI；49 只能属于 VM1、50 只能属于 VM2。不得调用 `assign_irq`、写 Host GIC route、消费 Host IRQ 或把 INTID 加入 `pass_through_spis`。
4. VM 尚未 Running、generation 已变化、vCPU0 不存在、runtime queue 不可用或注入失败时返回 typed error；frontend 必须进入可观察的 terminal failure，不能把 IRQ 丢失当成功。
5. 在 default prepare 中先构造该 sink，再建立 `InterruptFabric::with_sink(...)`；Provided 路径仍验证 mode/backend，不得静默覆盖测试或平台提供的 sink。

最低层测试必须证明 49/50 分别只进入所属 VM 的 vCPU0、assert 去重、ack/deassert 后可再次 assert、pulse 行为、stale generation/Stopped VM 失败，以及构建失败不会留下 asserted line。目标 AArch64 runtime 还要从 Guest 侧证明 used-ring 更新后能收到 IRQ，ack 清空 `InterruptStatus` 后线路撤销；只看到 Host queue 入队不算完成。

## 10. P4-NET-E：Guest IPv4 与 TEST-011

### 10.1 Linux

P4 在 `apps/contest/linux-ai-controller/` 先建立不含模型的网络骨架：固定绑定 `10.77.0.1`，提供 `--mode l3-smoke|udp-echo|tcp-client|icpc`。程序启动时输出唯一 READY，记录实际 interface、MAC、IPv4、prefix、route、MTU 和 socket bind；检测到默认路由或地址漂移直接退出非零。

Linux 镜像构建必须包含 VirtIO-net/IPv4/ICMP/UDP/TCP 驱动。runner 不依赖交互式 `ip addr add`；网络配置由 rootfs 构建步骤或应用启动脚本生成，并进入 manifest。

### 10.2 Zephyr

`apps/contest/zephyr-control/` 在 P4 先实现低优先级网络线程和 smoke 模式，不实现 plant。锁定 Zephyr `v4.4.0`、SDK `1.0.1`、board `qemu_cortex_a53`。最终 `.config` 必须证明网络、IPv4、ARP、ICMP、UDP、TCP、VirtIO 与 VirtIO Ethernet driver 已启用；配置符号若与锁定 Zephyr Kconfig 不一致，应修 `.conf`，不能换驱动或换 RTOS。

静态地址由 overlay/app config 生成；不得用 DHCP。启动日志必须输出实际 MAC、IPv4、prefix、connected route、MTU 和 READY。

### 10.3 两端制备入口

下列两个 planned CLI 是 P4 唯一制备入口，参数名也是实现合同。它们必须拒绝已存在输出、相对/符号链接输入、manifest/hash 不匹配和 source 在运行中变化；禁止原地修改 P2 rootfs、Zephyr checkout 或共享 cache。

先构建 P4 Linux 网络应用，再从已验证 P2 soak 所绑定的 ext4 制作 fresh copy：

```bash
repo="$(git rev-parse --show-toplevel)"
soak="$repo/results/baseline/runs/<P2-SOAK-success-run>"
prep_id="phase4-net-input-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 HEAD)"
prep="$repo/tmp/contest/p4-prepared/$prep_id"
source_rootfs="<absolute-linux-rootfs-bound-by-$soak/live/manifest.json>"

make -C "$repo/apps/contest/linux-ai-controller" clean p4-network \
  CC=/opt/aarch64-linux-musl-cross/bin/aarch64-linux-musl-gcc

python3 "$repo/scripts/contest/network/prepare_linux_network_rootfs.py" \
  --repository "$repo" \
  --from-soak-session "$soak" \
  --source-rootfs "$source_rootfs" \
  --source-manifest "$soak/live/manifest.json" \
  --app-binary "$repo/apps/contest/linux-ai-controller/build/linux-ai-controller" \
  --bind-cidr 10.77.0.1/24 \
  --peer-ip 10.77.0.2 \
  --mac 02:00:00:00:00:01 \
  --udp-port 46000 \
  --tcp-port 46001 \
  --run-id "$prep_id" \
  --output-rootfs "$prep/linux-network.ext4" \
  --output-manifest "$prep/linux-network-rootfs.json"
```

再用锁定 checkout、manifest、SDK、west 和 board 构建一个包含全部 P4 smoke/UDP/TCP/ICPC 模式的 Zephyr image：

```bash
export PATH=/root/.cache/tgoskits-contest/zephyr-4.4.0-venv/bin:$PATH
export ZEPHYR_BASE=/root/.cache/tgoskits-contest/zephyrproject-4.4.0/zephyr
export ZEPHYR_SDK_INSTALL_DIR=/root/.cache/tgoskits-contest/zephyr-sdk-1.0.1

python3 "$repo/scripts/contest/network/prepare_zephyr_network_image.py" \
  --repository "$repo" \
  --zephyr-base "$ZEPHYR_BASE" \
  --zephyr-revision 684c9e8f32e4373a21098559f748f06915f950c9 \
  --west-manifest-sha256 9c3661dd82e5ab7f487e3c0a4eee8726978736eadb470a3a09a76c77a8f10f92 \
  --zephyr-sdk-dir "$ZEPHYR_SDK_INSTALL_DIR" \
  --zephyr-sdk-version 1.0.1 \
  --west-version 1.5.0 \
  --board qemu_cortex_a53 \
  --app "$repo/apps/contest/zephyr-control" \
  --config "$repo/configs/contest/zephyr/qemu-cortex-a53-vnet0.conf" \
  --overlay "$repo/configs/contest/zephyr/qemu-cortex-a53-vnet0.overlay" \
  --application-mode p4-network \
  --run-id "$prep_id" \
  --build-dir "$prep/zephyr-build" \
  --output-dir "$prep/zephyr-network" \
  --output-manifest "$prep/zephyr-network-build.json"
```

成功输出必须包含 Linux ext4、两份 manifest、`zephyr.bin`、`zephyr.elf`、`.config`、`zephyr.dts` 及 SHA-256。runner 只接受这两个 manifest 中的精确路径/hash；不能手工替换 image。

### 10.4 TEST-011 固定顺序

1. descriptor/跨 VM/stale generation/overflow 负例全绿；
2. `cargo test -p axdevice`、相关 axvm tests、Clippy、rustfmt；
3. 正式 AArch64 AxVisor、Linux app、Zephyr app 构建；
4. 最终 QEMU argv 无任何 outer NIC；
5. 两 Guest 各只枚举自己的 NIC/MAC；
6. ARP 邻居建立；
7. Linux→Zephyr 100 个 ICMP echo，loss=0；
8. Zephyr→Linux 100 个 ICMP echo，loss=0；
9. final DTB、resource claim、switch counter、capture/PCAP 与端点日志逐字段一致；
10. 有界退出，残留进程/socket/runtime dir 为 0。

Host ping、静态 MAC、只打印 READY 或 outer hub 上的 ping 均失败。

## 11. P4-NET-F：UDP、TCP 与 ICPC

### 11.1 UDP echo / TEST-012

- 两端只绑定自己的固定地址和 UDP `46000`，只接受固定 peer；
- 每方向 10,000 个 256-byte payload，100 packets/s；
- 无故障档 loss、silent corruption、非注入 duplicate/reorder 均为 0；
- 故障档使用固定 manifest：每 10 包 drop 1、每 17 包 duplicate 1、每 23 包交换相邻顺序、每 29 包损坏 1；同一 packet 命中多条规则时按 `corrupt -> reorder -> duplicate -> drop` 的 manifest 顺序执行并逐包记录；
- 报告 request 成功率、timeout、重传率、同端 RTT、payload/wire throughput；重传不增加逻辑 request 分母。

故障注入作用于 switch 的测试 hook，正式 profile 默认关闭。hook 接收预生成不可变 fault manifest，不调用随机源，不允许改变 descriptor/GPA 校验。

### 11.2 TCP fallback / TEST-013

- Zephyr 只在 fallback 状态监听 `10.77.0.2:46001`，Linux 主动连接；
- framing 为 `u16_be frame_length + 完整 ICPC packet`，长度 `36..1060`；
- 运行 10,000 个 frame，覆盖 1-byte partial read、每 31 帧合并 read、连接中断、端点重启和 half-frame EOF；
- connect deadline 500 ms，失败后等待 500 ms，最多 3 次；
- 每次 transport 切换必须关闭旧 UDP session，生成新非零 session；UDP/TCP 不得同时拥有 actuator；
- TCP 建立后不自动切回 UDP。

### 11.3 ICPC / TEST-014、TEST-015

先保持 host 合同：

```bash
python3 scripts/test/check_icpc_protocol.py
```

输出必须包含 `ICPC_PROTOCOL_PASS`。对外首次写作“ICCP（仓库线协议实现名 ICPC v1）”，代码、magic、抓包和 wire 一律写 ICPC；字段差异按 `IF-006`，不得宣称逐字节等同申报表格。然后两端直接复用 `scripts/contest/icpc/icpc.c/.h` 或由同一源码生成的字节兼容适配层，禁止各写一份 wire codec。

真实 Guest 测试在 UDP `46000` 上运行 100 秒、10 Hz、1,000 个 CONTROL，并覆盖 24/32/12-byte CONTROL/STATUS/ERROR、ACK、HEARTBEAT、丢包、重复、乱序、CRC、版本、长度、session restart 和 timeout。CONTROL 发送只允许 `t=0/100/300 ms`；`t=500 ms` 到期取消，禁止 `t=700 ms` 重发旧控制。

P4 只验证 payload 字段互操作和“每个合法 CONTROL 恰处理一次”的应用 stub；真实 plant、heater duty 和安全闭环在 P5 关闭。P4 不能因此标为 `L8 AI-loop`。

## 12. Runner 与证据合同

planned runner 的 CLI 到以下参数为止即为冻结合同；实现者不得再提供一个参数更少、会从个人 cache 猜输入的“快捷入口”。先定义本次已经制备并有 hash 的输入：

```bash
repo="$(git rev-parse --show-toplevel)"
soak="$repo/results/baseline/runs/<P2-SOAK-success-run>"
prep="$repo/tmp/contest/p4-prepared/<phase4-net-input-id>"
build="$repo/os/axvisor/configs/board/qemu-aarch64-contest-network.toml"
qemu="$repo/configs/contest/qemu-aarch64-linux-zephyr-dual.toml"
linux_vm="$repo/os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml"
zephyr_vm="$repo/os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml"
linux_rootfs="$prep/linux-network.ext4"
linux_manifest="$prep/linux-network-rootfs.json"
zephyr_image="$prep/zephyr-network/zephyr.bin"
zephyr_elf="$prep/zephyr-network/zephyr.elf"
zephyr_manifest="$prep/zephyr-network-build.json"
```

`TEST-011`：

```bash
run_id="phase4-net-test011-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 HEAD)"
python3 "$repo/scripts/contest/network/run_guest_network.py" \
  --repository "$repo" \
  --from-soak-session "$soak" \
  --build-config "$build" \
  --qemu-config "$qemu" \
  --linux-vmconfig "$linux_vm" \
  --zephyr-vmconfig "$zephyr_vm" \
  --linux-rootfs "$linux_rootfs" \
  --linux-rootfs-manifest "$linux_manifest" \
  --zephyr-image "$zephyr_image" \
  --zephyr-elf "$zephyr_elf" \
  --zephyr-build-manifest "$zephyr_manifest" \
  --scenario test-011 \
  --profile "$repo/configs/contest/network/test-011-v1.json" \
  --timeout-seconds 600 \
  --run-id "$run_id" \
  --output-dir "$repo/results/baseline/runs/$run_id"
```

`TEST-012` 的一个 invocation 必须按 profile 依次执行双向无故障和双向确定性故障四个子阶段，不能只跑其中一个：

```bash
run_id="phase4-net-test012-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 HEAD)"
python3 "$repo/scripts/contest/network/run_guest_network.py" \
  --repository "$repo" \
  --from-soak-session "$soak" \
  --build-config "$build" \
  --qemu-config "$qemu" \
  --linux-vmconfig "$linux_vm" \
  --zephyr-vmconfig "$zephyr_vm" \
  --linux-rootfs "$linux_rootfs" \
  --linux-rootfs-manifest "$linux_manifest" \
  --zephyr-image "$zephyr_image" \
  --zephyr-elf "$zephyr_elf" \
  --zephyr-build-manifest "$zephyr_manifest" \
  --scenario test-012 \
  --profile "$repo/configs/contest/network/test-012-v1.json" \
  --timeout-seconds 1200 \
  --run-id "$run_id" \
  --output-dir "$repo/results/baseline/runs/$run_id"
```

`TEST-013`：

```bash
run_id="phase4-net-test013-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 HEAD)"
python3 "$repo/scripts/contest/network/run_guest_network.py" \
  --repository "$repo" \
  --from-soak-session "$soak" \
  --build-config "$build" \
  --qemu-config "$qemu" \
  --linux-vmconfig "$linux_vm" \
  --zephyr-vmconfig "$zephyr_vm" \
  --linux-rootfs "$linux_rootfs" \
  --linux-rootfs-manifest "$linux_manifest" \
  --zephyr-image "$zephyr_image" \
  --zephyr-elf "$zephyr_elf" \
  --zephyr-build-manifest "$zephyr_manifest" \
  --scenario test-013 \
  --profile "$repo/configs/contest/network/test-013-v1.json" \
  --timeout-seconds 900 \
  --run-id "$run_id" \
  --output-dir "$repo/results/baseline/runs/$run_id"
```

`TEST-015`（先单独跑过 `TEST-014` host 合同）：

```bash
run_id="phase4-net-test015-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 HEAD)"
python3 "$repo/scripts/contest/network/run_guest_network.py" \
  --repository "$repo" \
  --from-soak-session "$soak" \
  --build-config "$build" \
  --qemu-config "$qemu" \
  --linux-vmconfig "$linux_vm" \
  --zephyr-vmconfig "$zephyr_vm" \
  --linux-rootfs "$linux_rootfs" \
  --linux-rootfs-manifest "$linux_manifest" \
  --zephyr-image "$zephyr_image" \
  --zephyr-elf "$zephyr_elf" \
  --zephyr-build-manifest "$zephyr_manifest" \
  --scenario test-015 \
  --profile "$repo/configs/contest/network/test-015-v1.json" \
  --timeout-seconds 600 \
  --run-id "$run_id" \
  --output-dir "$repo/results/baseline/runs/$run_id"
```

`--from-soak-session` 必须读取并复核 P2 manifest 中的镜像、VM 配置、QEMU identity 输入和 SHA-256；其余显式输入再绑定 P4 当前产物。每个 TEST 使用全新目录；runner 必须拒绝已有输出、任一 hash 漂移、outer NIC、占位路径和不匹配的 run ID。

```text
results/baseline/runs/<run-id>/
  session.json
  manifest.json
  commands.jsonl
  status.json                         # 最后写
  configs/
  logs/
    axvisor.raw.log
    linux.raw.log
    zephyr.raw.log
  dtb/
    linux-final.dtb
    linux-final.dts
    zephyr-final.dtb
    zephyr-final.dts
  network/
    frames.jsonl
    capture.pcap
    counters.json
    fault-manifest.json
  metrics/
    raw.jsonl
    summary.json
  cleanup.json
```

`status.json` 复用 `IF-011`；结果放在布尔 `success`，`status` 不使用通用结果枚举。v1 smoke
producer 的成功 token 固定为 `guest_network_smoke_completed`，且必须同时写
`qualified:false`；Guest-runtime v2 只有在本节全部工件和 oracle 通过后才允许写
`guest_network_qualified`。运行失败为 `guest_network_failed`，前置阻塞为
`guest_network_blocked`：

```json
{"success":true,"status":"guest_network_qualified","qualified":true,"primaryError":null,"cleanupError":null,"completedChecks":["oracle","hashes","validator","cleanup"],"manifestSha256":"<64-hex>"}
```

失败和 blocked 均写 `success:false`，保留 primary/cleanup error；blocked 还必须写非空 `blockedReason`。只有全部 hash、oracle、validator 和 cleanup 通过后才能原子写成功状态；consumer 必须同时检查布尔值、token 和必需字段，不能只猜 token 名。

每个网络包至少绑定：run ID、QEMU identity、VM/device/port generation、最终 argv、两份 final DTB、NIC/MAC/IP/route/port、transport/session/sequence、switch enqueue/drop/deliver、used/IRQ counters、raw frame/PCAP、故障计划、时钟语义和 cleanup。

## 13. 实现顺序与逐步关闭

| 次序 | 提交边界 | 必须先转绿 | 可开始下一步的条件 |
|---:|---|---|---|
| 1 | `ScopedGuestMemory` + VM generation | 跨 VM/GPA/overflow/stale 负例 | 不暴露裸地址，reset 失效 |
| 2 | run-scoped switch services + factory | duplicate/missing service/rollback | 两 VM共享一个 switch |
| 3 | MMIO/feature/status | register/status 负例 | Linux/Zephyr 能识别 device ID |
| 4 | split queue + IRQ | chain/direction/wrap/owner 负例 | synthetic RX/TX fixture 全绿 |
| 5 | bounded switch + capture | MAC/overflow/reset/concurrency | 双 frontend host model 互通 |
| 6 | FDT/TOML/outer argv | allow/deny、INTID、幂等 | AArch64 build 全绿 |
| 7 | Guest driver/config | 两端 build、最终 `.config`/DTB | 独立 NIC 枚举 |
| 8 | ARP/ICMP | `TEST-011` | `L7 Guest-IP` 基础成立 |
| 9 | UDP/TCP | `TEST-012/013` | 双向 socket、故障恢复成立 |
| 10 | ICPC Guest adapter | `TEST-014/015` | P4 可关闭并移交 P5 |

每个提交只覆盖一行或紧耦合的两行。任何失败先修最低层；不得用后续 ping 掩盖 descriptor 或隔离负例。

## 14. P4 完成定义

只有同时满足以下条件，`P4-NET-01` 才能标为完成：

- `P4-NET-A..F` 全部关闭；
- `TEST-011..015` 当前代码和当前镜像均为 `verified`；
- `cargo test -p axdevice`、相关 axvm tests、`cargo xtask clippy --package axdevice --package axvm`、rustfmt、正式 AArch64 AxVisor build 全绿；
- outer QEMU 最终 argv 不含任何 netdev/NIC；
- 两 Guest final DTB 只含各自 frontend，MMIO/IRQ/MAC 与 runtime 一致；
- descriptor、跨 VM、stale generation、queue overflow、reset、错误 IRQ owner 负例全部保留；
- 双向 ARP/ICMP、UDP、TCP fallback 和 ICPC 的 raw log/PCAP/counter 可逐包复核；
- 每个 success run 满足 `IF-011`、status-last、hash 和零残留；
- `requirements.md`、`test-matrix.md`、`traceability.md`、`deliverables.md`、工作区 `现状.md`/`阻塞.md` 同步为准确状态；
- 交接记录写明最高证据只能是 `L7 Guest-IP`，不宣称 AI、实时改善、硬件 DMA 或通用隔离。

## 15. 交接模板

```text
工作包：P4-NET-01 / 子门禁 P4-NET-?
base/head：<40-hex>/<40-hex or dirty patch hash>
当前最高证据：L1/L2/L3/L5/L6/L7
已完成文件：<repo-relative paths>
本次命令与 exit code：<commands>
最新 evidence：<run-id + status + manifest sha256>
已通过 TEST：<TEST-011..015 subset>
失败/受阻：<primary error + cleanup error>
未证明：<explicit non-claims>
下一步唯一入口：<one exact command>
```

下一位开发者必须从最早未关闭的子门禁继续，不能从最终 ping 或 ICPC 演示倒推底层安全门已经完成。
