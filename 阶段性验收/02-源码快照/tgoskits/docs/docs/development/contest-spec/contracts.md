# 竞赛系统接口与数据合同

状态：v1 设计已冻结；P4 旧候选 host/target 门禁已有，`DEC-010` 官方基线迁移与 Guest 运行证据未完成（2026-08-17）
架构入口：[`architecture.md`](architecture.md)
需求入口：[`requirements.md`](requirements.md)
决策入口：[`sources-and-decisions.md`](sources-and-decisions.md)
验证入口：[`test-matrix.md`](test-matrix.md)

本文给出组件之间可编码、可负测、可取证的 `IF-*` 合同。`DEC-004/005/007/008` 的 v1 实现选择和数值在本文中均为 `design-frozen`：开发者不得自行另选 passthrough、地址、端口、payload、plant 或模型形状；但冻结设计不等于代码、目标构建、双 Guest、Guest IP 或 AI 闭环已经通过。决策登记的批准记录由 [`sources-and-decisions.md`](sources-and-decisions.md) 维护，若负责人推翻冻结值，必须先改 DEC、本文、双方黄金向量与测试矩阵，不能让第二套事实先进入代码。

## 1. 共同约束

- `current`：已在当前仓库源码/配置中核实；是否运行通过仍由 evidence 决定。
- `design-frozen`：唯一 v1 开发输入；仍不得与现有实现或运行事实混写。
- `planned`：路径和职责已指定但尚未实现。
- 所有多字节线上整数使用大端（network byte order）。实现必须逐字段编解码，禁止把有 padding/对齐差异的 C/Rust struct 直接发送到网络。
- 每个解码器都接收显式 buffer 长度；未知版本、未知枚举、保留位非零、长度不精确、越界值和 CRC 错误必须 fail closed。
- 所有接口变更都必须保持旧负例，新增版本或明确迁移；不得在不改变版本的情况下重解释已有字段。

## 2. IF-001 CPU、内存与设备拓扑

状态：CPU/RAM/console 与 r27/r31 双 Guest/soak 为 `verified-with-boundaries`；mediated NIC 拓扑为 `design-frozen/migration-required`，尚无 Guest-IP。
生产者：`ARC-001` 外层 QEMU、`ARC-002` AxVisor 配置加载器。
消费者：Linux/Zephyr Guest FDT、资源声明 validator、runner。

### 合同

| 资源 | Linux VM1 | Zephyr VM2 | Host/未分配 |
|---|---|---|---|
| VM ID | `1` | `2` | — |
| pCPU | `[0,1]` | `[2]` | `[3]` 用于 Host/console drain |
| vCPU 数 | `2` | `1` | — |
| RAM | GPA `0x8000_0000`, `0x1000_0000` bytes, identity map | GPA `0x4000_0000`, `0x0800_0000` bytes, `MapAlloc` | 不得与 Guest 授权 HPA 重叠 |
| block | 当前配置的 outer slot 0；P2 `MapReserved` 只允许 disposable 单 Linux窄 probe | 禁止 | P2 结果不自动准入双 Guest/生产 root block |
| network | AxVisor 合成 VirtIO-MMIO v2 frontend：GPA `0x0a00_0200`、size `0x200`、INTID 49、MAC `02:00:00:00:00:01` | AxVisor 合成独立 frontend：GPA `0x0a00_0400`、size `0x200`、INTID 50、MAC `02:00:00:00:00:02` | outer QEMU 不创建 netdev/NIC；AxVisor 内部二端口 switch |
| console | VM-local `0x0900_0000` TX-only emulated PL011 | VM-local `0x0900_0000` TX-only emulated PL011 | Host PL011 不下放 |

固定不变量：

1. 两个 `phys_cpu_ids` 集合互斥，数量分别等于 `cpu_num`，且不包含 pCPU 3。
2. P4 网络中 Linux/Zephyr 只能分别发现自己的合成 frontend；不得发现 outer slot、另一 VM frontend 或 Host uplink。当前旧 TOML 在合成节点实现前继续排除 Zephyr outer slot，不能把 passthrough 节点改名冒充 mediated frontend。
3. 两 Guest 都不得发现 ITS 或 Host `/pl011@9000000`。VM-local console 不声明物理 IRQ、clock、DMA 或 HPA。
4. 最终 Guest DTB、实际资源 claim 和启动配置必须逐运行绑定；不能只校验仓库模板。

### 失败、版本与测试

- CPU/内存/设备重叠、FDT 泄漏、缺失排除项或 slot 所有者不唯一时，validator 和启动入口都必须失败。
- 资源值变化属于架构版本变更；先更新 `DEC-*`、本合同、双 Guest TOML 和全部静态/运行测试。
- 正例：两份 dual TOML、factory 配置与两份最终 FDT 完全匹配，且外层 QEMU argv 不含 netdev/NIC。负例：重复 pCPU、交换 frontend/MAC/IRQ、恢复 Host UART/ITS、加入任一 outer NIC、同时出现合成与 passthrough 节点、篡改最终 DTB。
- 最低证据：配置 SHA-256、最终 DTB/DTS、资源 claim、两 Guest 独立日志和同一 session 身份。

## 3. IF-002 软件拷贝 mediated VirtIO-net 准入

状态：`design-frozen/migration-required`；旧候选已经有 host/target 构建，r41 无 Guest-IP。`DEC-010` 要求生产实现迁移到官方 `axvirtio-common`/`axvirtio-net`、device graph、scoped `DmaGrant` 和 VM wake/IRQ；不再等待 passthrough DMA 路线，也不保留第二套 backend。
生产者：`ARC-002/005` 的 factory、Guest-memory accessor、内部 switch、IRQ 与合成 Guest FDT。
消费者：Linux/Zephyr VirtIO 驱动、双 Guest/IP runner。

### 3.1 固定实现边界

1. VirtIO-MMIO、split virtqueue 和网络 device model 固定使用官方 `virtualization/axvirtio-common/`、`virtualization/axvirtio-net/`；AxVisor adapter 通过 resolved device graph 注册。禁止在旧 `axdevice/src/virtio_net/`、VM manager 或 runner 中再维护另一套 queue parser/backend。
2. Guest 内存只通过具体 bundled device 的 `DmaGrant` 与一次 poll/access 生命周期内的 scoped `DeviceAccess` 读写；每页验证属于当前 VM、映射存在、方向允许、`gpa+len` 不溢出。不得保存 VM-wide accessor、裸 HPA、宿主指针、长期 Guest slice 或另一 VM capability。
3. 两个 frontend 各有独立 MMIO register bank、MAC、split virtqueue、IRQ、统计和 `u64 port_generation`。共享对象只有本次 AxVisor run 专属的二端口 switch；switch handle 绑定 run nonce/session，不能跨 QEMU/AxVisor 运行复用。
4. VirtIO 固定为 MMIO transport version 2、device ID 1、vendor ID `0x554d4551`、split ring；只协商 `VIRTIO_F_VERSION_1`、`VIRTIO_NET_F_MAC`、`VIRTIO_NET_F_STATUS`。链路 MTU 固定 1500、link status UP，但 v1 不广告 `VIRTIO_NET_F_MTU`；不支持 packed ring、indirect descriptor、event index、mergeable RX buffer、checksum/TSO/UFO、multiqueue、control queue 或 VLAN。
5. queue 0 是 RX、queue 1 是 TX；每个 queue `QueueNumMax=256` 且 driver 必须选择 256，其他值 fail closed。descriptor chain 最多 32 项且不得成环；virtio-net header 固定 10 字节。TX chain 的 header/frame 部分必须 readable，RX chain 必须 writable；frame 不含 FCS，长度固定允许 `14..1514`。
6. TX 先完整验证 chain、长度、方向、所有 GPA 和源 MAC，再一次性 copy-in 到 Host-owned `[u8;1514]` frame；任何失败都不得部分入队或更新 used ring。RX 只有确认目标 writable capacity 足够后才 copy-out，再更新 used ring。处理结束不保留任何 Guest 指针/slice。
7. switch 每端口 FIFO 容量固定 256 个 Host-owned frame。已知对端单播、广播和 IPv4 multicast 只发往对端；未知单播、任何 IPv6、VLAN、本端回环和源 MAC 不等于端口固定 MAC 的帧丢弃。满队列使用 drop-newest 并增加饱和计数；不得覆盖旧帧、阻塞 RTOS vCPU 或动态扩容。
8. VM/device reset 或销毁时先阻止新提交，再递增 `port_generation`、清空该端口 FIFO/未完成 chain/IRQ 状态并注销 port；所有 callback/frame 都携带 run session 与 generation，不匹配即拒绝。used-ring 完成后才按 owning queue 的 notification suppression 触发 owning VM IRQ，绝不触发另一 VM IRQ。
9. AxVisor 分别合成 `compatible="virtio,mmio"` 节点：Linux GPA `0x0a00_0200`、size `0x200`、SPI 49；Zephyr GPA `0x0a00_0400`、size `0x200`、SPI 50。节点可声明 `dma-coherent` 以表达 CPU-coherent 软件设备访问，但不得生成 passthrough resource claim、物理 IRQ/HPA 或 outer-QEMU slot 依赖。
10. P4 outer QEMU argv 中禁止 `-netdev` 与 `virtio-net-device`。P2 disposable `MapReserved + virtio-blk` 只保留为单 Linux 窄观察；其 success、GPA/HPA、guard 或 rootfs 都不能满足本接口任一准入条件。

### 3.2 失败、版本与测试

旧候选实现了自建 `ScopedGuestMemory`、split queue、switch、IrqSink 和 vCPU poll；这些现在只是迁移输入。P4-UPSTREAM-01 必须先证明官方 crate/device graph/DmaGrant 路径的 host/target 门禁和唯一 backend，再进入 Linux+Zephyr runtime。

- 任何跨 VM GPA、未映射/跨 region/地址溢出、权限错误、chain 成环/超过 32、queue size 非 256、frame 超过 1514、stale generation/session、MAC spoof、queue overflow 和错误 IRQ owner都必须有确定性负例；失败不得改变另一 VM 内存、used ring、IRQ 或已排队 frame。
- 正例覆盖 feature negotiation、RX/TX scatter-gather、队列 wrap、广播/组播/单播、256-frame 边界、两 VM 并发、reset 后新 generation 和 cleanup；还要对最终 Guest DTB 做逐 VM allow/deny 验证。
- 证据绑定 QEMU/AxVisor identity、VM/device/generation、frontend config、每次 copy 的 VM ID/GPA/长度/方向（不记录敏感 payload）、switch enqueue/drop/deliver、used/IRQ 计数、最终 DTB、pcap 等价镜像和 cleanup。
- 只有 lowest-level tests、AArch64 build、双 Guest 正负 runtime 和无残留全部通过，才可把接口从 `planned` 升为 `verified`。ping 只能证明其上的 IP 行为，不能替代 descriptor/跨 VM 隔离负例。

## 4. IF-003 隔离 IPv4 拓扑

状态：`design-frozen/not-implemented`；旧 outer hub/MAC 配置不是本接口实现。
生产者：AxVisor mediated `vnet0` frontend/switch、Linux/Zephyr 网络配置。
消费者：`ARC-006` ICPC 端点和网络验证器。

| 项目 | Linux | Zephyr | 状态 |
|---|---|---|---|
| AxVisor network | `vnet0` port 0 | `vnet0` port 1 | `design-frozen`；Host 无 port |
| MAC | `02:00:00:00:00:01` | `02:00:00:00:00:02` | `design-frozen` frontend config |
| IPv4 | `10.77.0.1/24` | `10.77.0.2/24` | `design-frozen` |
| connected route | `10.77.0.0/24` | `10.77.0.0/24` | `design-frozen` |
| default route / gateway | 无 | 无 | `design-frozen` |
| DNS | 无 | 无 | `design-frozen` |
| Host bridge / NAT / proxy | 无 | 无 | `design-frozen` |
| MTU | `1500` | `1500` | `design-frozen` |

网络只允许该 `/24` 内的 ARP、ICMP 和比赛端口流量。抓包由内部 switch 在完成 copy-in、通过 L2 校验后复制到有界 evidence ring，记录 ingress port、generation、方向、单调时间和 frame bytes；该镜像不得成为第三个转发端口，也不得反向注入。不得为抓包增加 QEMU netdev、宿主可路由网卡、NAT 或代理端点。

### 失败、版本与测试

- MAC/IP 重复、前缀不一致、意外默认路由、非零 NAT/bridge/proxy、outer NIC 参数或出现第三个控制端点时 fail closed。
- 正例顺序：两 NIC 独立枚举 → ARP 邻居 → 双向 ICMP → UDP echo → TCP stream。每一级不得代替后一级。
- 负例：重复地址、错误 MAC 绑定、错误 `/24`、外网目的地址、拆除一侧 NIC、重启一侧 Guest。
- 证据：两端 `ip addr/route` 或等价 Zephyr 配置、邻居表、最终 DTB、内部 frame capture、switch/端点日志、配置哈希和证明 outer argv 无 NIC 的审计；只有 Host ping 不算 Guest↔Guest。

## 5. IF-004 UDP 主传输与 TCP 回退

状态：`design-frozen/not-implemented`；`DEC-005/008` 已固定传输与端口。
生产者/消费者：Linux 与 Zephyr 的 `ARC-006` 适配层。

### UDP 主传输

- 两端都绑定各自固定 IP 的 UDP `46000`，只接受 `IF-003` 的对端；业务数据报必须恰好包含一个 `IF-005` ICPC packet。socket 不绑定 wildcard 地址，不接受 broadcast/multicast 业务报文。
- packet 上限 1060 字节（36 字节头 + 1024 字节 payload），低于 1500 MTU；v1 禁止 IP 分片和一个 ICPC packet 跨多个 UDP 数据报。
- CONTROL 固定设置 `ACK_REQUIRED`；ERROR 固定设置 `ACK_REQUIRED`；STATUS 与 HEARTBEAT 固定 best-effort 且 flags 为零。每方向只有一个可靠消息槽，因此 Linux 的 CONTROL 与 Zephyr 的 ERROR 不互相阻塞。STATUS 每 100 ms 一条；若任一方向 250 ms 没有其他出站 packet，则发送一个 HEARTBEAT，1,000 ms 没有任何有效入站 packet 时只把 transport 标为 down，不刷新/替代 500 ms actuator watchdog。
- 通用 ICPC 保持 `IF-007` 的 100/200/400 ms、首次加三次重传。CONTROL 另受 500 ms application validity 约束：发送发生在 `t=0/100/300 ms`；到 `t=500 ms` 仍无匹配 ACK 时取消计划中的 `t=700 ms` 重传、将旧 UDP session 置为 failed 并进入 TCP 回退。这样不会在安全态后重发过期 actuator 命令。

### TCP 回退

- Zephyr 只在回退状态监听其固定 IP 的 TCP `46001`，Linux 主动连接。每帧是 `frame_length:u16_be` 加一个完整 ICPC packet；`frame_length` 范围 `36..1060`，且必须等于随后 packet 的实际长度。解析器必须正确处理任意 partial read 和多个完整 frame 被同一次 read 返回的情况。
- 回退不能让 UDP 与 TCP 同时拥有 actuator。UDP session 明确失败后，Linux 停止发送并关闭旧 session，再以新的非零 `session_id` 建立 TCP；Zephyr 在切换间隙保持或进入 `IF-009` 安全状态。
- 单次 nonblocking connect deadline 固定 500 ms；失败后等待 500 ms，最多执行 3 次 connect attempt，三次均失败则保持安全态并返回 terminal transport failure，由 runner 决定是否以全新 evidence session 重启。TCP 建立后不自动切回 UDP；切回只能停止 TCP、新建 ICPC session 并作为独立测试阶段执行。
- v1 不允许静默降级：runner/应用必须记录 `transport=udp|tcp`、切换原因、每次 connect deadline 和新 session。半开 TCP、短读、超长 frame 或连接重置都按失败处理，不拼接旧 session 数据。

### 失败、版本与测试

- 非固定 peer、零长度/超长/截断/尾随数据、一个数据报多帧或 UDP/TCP 并发控制时拒绝消息，不刷新 watchdog。
- 正例：UDP 正常流、通用消息完整重传、CONTROL 在 500 ms 截止、明确切换到新 TCP session、TCP partial-read 重组和多个相邻 frame。负例：700 ms 后重发旧 CONTROL、非法/超长第二个 frame、连接关闭时 frame 仍截断、半开连接、三次 connect failure、旧 UDP session 在 TCP 生效后发控制。
- 证据同时记录五元组、transport、session、sequence、收发字节/包、切换时间线和抓包；TCP 成功不能冒充 UDP 主链路成功。

## 6. IF-005 ICPC v1 线格式

状态：线格式 `design-frozen/current-host-implemented`，当前证据等级仅为 C99 host 合同；Guest codec/socket 未实现。
规范实现：`scripts/contest/icpc/icpc.h`、`icpc.c`；权威说明：`development/contest/contest-icpc-protocol.md`，仓库交付镜像为 `docs/docs/development/contest-icpc-protocol.md`。

所有字段使用大端。固定头 36 字节，payload 最大 1024 字节：

| 偏移 | 大小 | 字段 | v1 约束 |
|---:|---:|---|---|
| 0 | 4 | `magic` | ASCII `ICPC` |
| 4 | 1 | `version` | `1` |
| 5 | 1 | `header_length` | `36` |
| 6 | 1 | `message_type` | `1 CONTROL`、`2 STATUS`、`3 ERROR`、`4 ACK`、`5 HEARTBEAT` |
| 7 | 1 | `flags` | bit0 `ACK_REQUIRED`、bit1 `RETRANSMISSION`；其余为零 |
| 8 | 4 | `session_id` | 非零；每次端点启动或 transport 切换重新生成 |
| 12 | 4 | `sequence` | 非零；每个发送 packet 递增 |
| 16 | 4 | `ack_sequence` | 只有 ACK 非零 |
| 20 | 8 | `timestamp_ms` | 发送端 monotonic 毫秒；不代表 UTC |
| 28 | 2 | `payload_length` | `0..1024`，packet 长度必须精确等于 `36 + payload_length` |
| 30 | 2 | `error_code` | 只有 ERROR 非零；当前值域 `1..4` |
| 32 | 4 | `crc32c` | Castagnoli；计算时本字段置零，覆盖头和 payload |

组合约束：ACK/HEARTBEAT 无 payload；ACK 无 flags 且 `ack_sequence != 0`；非 ACK 的 `ack_sequence=0`；ERROR 的 `error_code != 0`；非 ERROR 的 `error_code=0`；`RETRANSMISSION` 必须和 `ACK_REQUIRED` 同时出现。任何额外尾随字节均非法。

线格式只能通过新的 `version` 改变。正负测试至少覆盖 CRC32C 标准向量、各消息往返、大小端、最大 payload、截断、尾随、未知字段、错误组合和损坏；Guest 适配后必须运行相同向量，而不是另写不兼容 codec。

## 7. IF-006 申报 ICCP 与仓库 ICPC 的映射

状态：`design-frozen`；[`DEC-004`](sources-and-decisions.md#dec-004) 固定公共名为 ICCP、仓库 wire/代码名为 ICPC v1。
目标：保留申报验收语义，同时诚实记录现有 ICPC v1 并非申报表格的逐字节复制。

| 申报 `ICCP` 概念字段 | ICPC v1 字段 | 映射/偏差 |
|---|---|---|
| `version:u8` | `version:u8` | 直接映射 |
| `msg_type:u8` | `message_type:u8` | 直接映射；五类消息固定 |
| `flags:u16` | `flags:u8` | v1 只需要 ACK_REQUIRED/RETRANSMISSION；高位不传输且未知位拒绝 |
| `seq:u32` | `sequence:u32` | 直接映射 |
| `timestamp_ns:u64` | `timestamp_ms:u64` | 分辨率受控降低；高精度指标由 `IF-010` 本地事件记录承担 |
| `payload_len:u32` | `payload_length:u16` | v1 上限 1024，因此有界窄化 |
| `error_code:i32` | `error_code:u16` | v1 只允许非负、小范围协议错误码；详细业务错误进 `IF-008` payload |
| `checksum/crc32:u32` | `crc32c:u32` | 固定 Castagnoli CRC32C |
| 未在申报表格列出 | `magic`、`header_length`、`session_id`、`ack_sequence` | 为定界、重启隔离和显式 ACK 增加 |

对外文档中的首次出现写作“ICCP（仓库线协议实现名 ICPC v1）”，随后概念需求可写 ICCP，代码、magic 和 wire 统一写 `ICPC`。验收通过后可以称“ICPC v1 实现了 ICCP 的功能字段与可靠性要求”，不能称“与申报 ICCP 字节布局完全相同”。若评审要求原始位宽或纳秒时间戳，必须新增协议版本和迁移测试，禁止原地改变 v1。

## 8. IF-007 会话、ACK、重传与去重

状态：语义 `design-frozen`；核心状态机 `current/host-implemented`，Guest socket 与 CONTROL validity 取消 API `not-implemented`。

- 每次进程启动、Guest 重启或 UDP/TCP 切换生成新的非零 `session_id`；接收端观察到新 session 时清空旧窗口和应用层 request 状态，并先保持安全状态。
- v1 同一发送端最多有一个待确认可靠消息。通用可靠消息初始 RTO 100 ms，随后 200 ms、400 ms；发送时刻为 `t=0/100/300/700 ms`，在 `t=1100 ms` 无 ACK 时显式 timeout。
- CONTROL 固定 `validity_ms=500`，优先于通用 retry：只允许 `t=0/100/300 ms` 三次发送；到 `t=500 ms` 仍未 ACK 时，应用调用明确的 cancel/expire 路径，绝不执行 `t=700 ms` 重传。该 API、状态和测试必须加入 reference library，不能靠调用者遗忘 poll 来模拟取消。
- 重传保持相同 `(session_id, sequence)` 并设置 `ACK_REQUIRED|RETRANSMISSION`。只有 session 与 `ack_sequence` 同时匹配的 ACK 才完成等待。
- 接收端维护 64 packet 窗口，按 RFC 1982 比较 32 位 sequence；重复包重发 ACK 但不重复执行，陈旧包拒绝，相差正好 `2^31` 的歧义值拒绝。
- HEARTBEAT 不替代新 CONTROL，也不刷新 actuator watchdog。网络活性和控制新鲜度是两个独立状态。

失败时不得无限重试、回退为“收到即执行”或让旧 session 恢复控制。测试覆盖 ACK 丢失、重复、乱序、回绕、歧义值、错误 session/ack、端点重启、retry deadline 溢出，以及 CONTROL 在 500 ms 取消且 700 ms 无发送。证据须能按 sequence 重建每个逻辑消息的发送次数、ACK、执行次数、expire/fallback 和最终 outcome。

## 9. IF-008 CONTROL、STATUS 与 ERROR payload

状态：`design-frozen/not-implemented`；当前 ICPC 库仍只把 payload 当不透明字节。
v1 对象固定为一阶温度仿真；温度使用毫摄氏度（`mC`），动作使用 `[0,1]` 的 Q16.16 heater duty。CONTROL/STATUS/ERROR 大小固定为 24/32/12 字节。

所有 payload 都是紧凑大端字节序，保留字段必须为零。

### CONTROL，固定 24 字节

| 偏移 | 大小 | 字段 | 约束 |
|---:|---:|---|---|
| 0 | 1 | `schema_version` | `1` |
| 1 | 1 | `command` | `1 APPLY_OUTPUT`、`2 ENTER_SAFE`、`3 SET_TARGET` |
| 2 | 1 | `control_mode` | `0 FIXED_BASELINE`、`1 MLP`；ENTER_SAFE 时也必须声明来源模式 |
| 3 | 1 | `reserved0` | `0` |
| 4 | 4 | `request_id` | 非零；同一 session 单调递增 |
| 8 | 4 | `duty_q16_16:i32` | APPLY_OUTPUT 时 `0..65536`；其他命令为 `0` |
| 12 | 4 | `target_mC:i32` | SET_TARGET 时使用；其余命令为当前目标的显式回显 |
| 16 | 4 | `model_version:u32` | MLP 时非零并映射到模型 manifest；FIXED 时为 `0` |
| 20 | 2 | `validity_ms:u16` | v1 必须恰为 `500`；从接收端首次接受 APPLY_OUTPUT 起允许保持动作的最长时间 |
| 22 | 2 | `reserved1` | `0` |

命令组合固定如下：

- `APPLY_OUTPUT`：`duty=0..65536`、`target_mC` 必须等于 Zephyr 当前目标、`validity_ms=500`；MLP 模式要求非零且匹配的 `model_version`，FIXED 模式要求 `model_version=0`。只有它能刷新 actuator watchdog。
- `ENTER_SAFE`：`duty=0`、`model_version=0`、`validity_ms=500`；在下一个 100 ms tick 前把 duty 清零，且不刷新 watchdog。
- `SET_TARGET`：v1 资格场景只接受 `target_mC=55000`，`duty=0`、`model_version=0`、`validity_ms=500`；更新目标但不刷新 watchdog。扩展 target 范围必须成为新 profile/决策，不能让验收场景漂移。

`request_id` 负责动作/反馈关联；ICPC `sequence` 负责传输层排序，二者都必须验证。发送端从首次发送计龄，到 500 ms 必须取消尚未发生的重传；接收端从首次接受 APPLY_OUTPUT 启动本地 watchdog。两 Guest 无同步时钟，接收端不能用对端 `timestamp_ms` 判断在途绝对年龄。

### STATUS，固定 32 字节

| 偏移 | 大小 | 字段 | 约束 |
|---:|---:|---|---|
| 0 | 1 | `schema_version` | `1` |
| 1 | 1 | `control_mode` | 当前实际模式，`0 FIXED` 或 `1 MLP` |
| 2 | 2 | `health_flags` | bit0 SAFE、bit1 NETWORK_TIMEOUT、bit2 SENSOR_INVALID、bit3 ACTUATOR_CLAMPED、bit4 DEADLINE_MISS；其余为零 |
| 4 | 4 | `applied_request_id` | 最近一次实际应用的 request；从未应用时 `0` |
| 8 | 8 | `sample_index` | Zephyr 启动后单调递增的控制样本序号 |
| 16 | 4 | `measured_mC:i32` | 当前对象输出 |
| 20 | 4 | `target_mC:i32` | 当前目标 |
| 24 | 4 | `duty_q16_16:i32` | 当前实际动作，合法范围 `0..65536` |
| 28 | 4 | `control_error_mC:i32` | `target_mC - measured_mC`，饱和时须显式记录 |

### ERROR，固定 12 字节

| 偏移 | 大小 | 字段 | 约束 |
|---:|---:|---|---|
| 0 | 1 | `schema_version` | `1` |
| 1 | 1 | `subsystem` | `1 PROTOCOL`、`2 NETWORK`、`3 CONTROL`、`4 MODEL`、`5 RUNTIME` |
| 2 | 2 | `detail_code` | 子系统内稳定枚举，`0` 非法 |
| 4 | 4 | `related_request_id` | 无相关请求时 `0` |
| 8 | 4 | `diagnostic_token` | 对应本地日志事件的非敏感 ID；不是地址或指针 |

ICPC 头的 `error_code` 必须同时非零；payload 给出更细分类。ACK/HEARTBEAT 继续使用空 payload。未知 schema、命令、模式、health 位、subsystem、长度或保留位一律拒绝。负例还应覆盖 NaN 不适用但 Q16.16 越界、request 回退、model/version 不匹配、过期 validity、错误单位和饱和溢出。

`detail_code` 的 v1 值固定为：PROTOCOL `{1 BAD_SCHEMA,2 BAD_LENGTH,3 BAD_RESERVED,4 BAD_RANGE,5 STALE_REQUEST}`；NETWORK `{1 PEER_MISMATCH,2 CONTROL_EXPIRED,3 SWITCH_OVERFLOW,4 TRANSPORT_DOWN}`；CONTROL `{1 TARGET_MISMATCH,2 SAFE_TIMEOUT,3 ACTUATOR_CLAMPED}`；MODEL `{1 VERSION_MISMATCH,2 NONFINITE_OUTPUT,3 INFERENCE_FAILURE}`；RUNTIME `{1 DEADLINE_MISS,2 INTERNAL_OVERFLOW}`。其他值解码为未知错误并保留安全态，不能被当成成功。

### 固定一阶温度 plant

- 周期与离散步长固定 `dt=100 ms`；一次 180 s 资格运行恰为 1,800 tick。状态初值和环境温度均为 `25000 mC`，目标固定 `55000 mC`，安全 duty 固定 `0`。
- 热损失时间常数固定 10 s，每 tick 使用有符号整数除法向零取整：`loss_mC=(25000-T_mC)/100`。
- heater 最大升温率固定 `4000 mC/s`；每 tick 为 `heater_mC=round_nearest(400*duty_q16_16/65536)`，正数恰好半数时向上取整。
- disturbance 在 `t∈[60 s,90 s)`（tick 600..899）固定为 `-150 mC/tick`，其余为 0。更新顺序固定为先读取本 tick 已应用 duty，再计算 `T_next=T+loss+heater+disturbance`，最后发布该 tick STATUS；中间值使用有符号 64 位，结果超出 `[-40000,125000] mC` 时进入安全态并报告 `RUNTIME/INTERNAL_OVERFLOW`，不得静默 wrap。
- seeds `7/19/43` 分别定义三次配对资格运行的网络 fault/调度随机流；无故障 plant 方程本身不含随机项。固定 PI 与 MLP 在同一 seed 下必须复用逐事件 fault manifest。

### 固定 PI 基线

PI 在 Linux 每 100 ms、收到新 STATUS 后计算一次：`e_C=(target_mC-measured_mC)/1000`，`Kp=0.025 duty/C`，`Ki=0.005 duty/(C*s)`，积分初值 0、范围 `[-100,100] C*s`。先求 `I_candidate=clamp(I+0.1*e_C,-100,100)` 和 `u_candidate=Kp*e_C+Ki*I_candidate`；若 `u_candidate>1 && e_C>0` 或 `u_candidate<0 && e_C<0`，拒绝本次积分并用旧 `I` 重算，否则提交 `I_candidate`。最终 `u=clamp(u_candidate,0,1)`，按最近值（恰好半值向上）量化为 `duty_q16_16`。进入 SAFE、目标改变或 ICPC session 改变时积分清零。该实现位于 Linux 端并走与 MLP 完全相同的 IP/payload 路径。

### 固定 MLP 与训练/导出合同

- 网络只允许 `3→8→1`：输入 `{measured_mC,target_mC,previous_duty_q16_16}`，8 个 hidden 使用 ReLU，单输出使用 sigmoid；权重、bias 和计算全部为 IEEE-754 float32，Linux 部署实现不得调用规则/查表旁路。
- 输入归一化固定为 `x0=clamp((measured_mC-25000)/40000,-1,1)`、`x1=clamp((target_mC-25000)/40000,-1,1)`、`x2=clamp(2*previous_duty_q16_16/65536-1,-1,1)`。sigmoid 输出 clamp 到 `[0,1]`，再以最近值、恰好半值向上量化到 `0..65536`。
- canonical 数据生成器用本节 plant 与固定 PI teacher 产生监督样本：seed 7 为 train、19 为 validation、43 为 test；每个 split 运行 64 个 1,800-tick episode，并在每次 PI 计算前记录输入、计算后记录 duty label。随机流固定为 PCG-XSH-RR 64/32：`old=state; state=old*6364136223846793005+1442695040888963407 (mod 2^64); x=((old>>18)^old)>>27; rot=old>>59; output=rotr32(u32(x),rot)`；state 先置 seed 并丢弃第一次 output。每 episode 依次取四个 `u32`，令初温 `25000+(r0 mod 15001) mC`、目标 `45000+(r1 mod 15001) mC`、disturbance 起点 `300+(r2 mod 601)` tick、持续 300 tick、幅值 `-200+(r3 mod 301) mC/tick`，其余时刻 disturbance 为 0，previous duty/PI integral 初值为 0。样本顺序固定为 episode 再 tick；每个 split 的 SHA-256 写入 metadata，之后改变任一生成值即产生新 `model_version`。不得在取得结果后移动 split 或 seed。
- canonical 训练固定 CPU 单线程 float32、Adam、learning rate `0.001`、batch `256`、MSE、200 epoch、无 early stopping；框架和版本进入 lock/manifest并启用 deterministic algorithms。导出顺序固定 `W1[8][3],b1[8],W2[1][8],b2[1]` 的 little-endian float32 bytes；完整 SHA-256 是模型身份，`model_version` 取 SHA-256 前 4 bytes 的大端非零整数（若为零则取后 4 bytes）。
- Python 参考与 Linux 部署 C 的 golden-vector duty 必须相差不超过 2 个 Q16.16 LSB。模型不存在、hash/version 不符、任一非有限中间值或输出越界时不发送 MLP APPLY_OUTPUT，明确切换 FIXED 或 ENTER_SAFE 并记录模式边界。

## 10. IF-009 控制应用与安全状态

状态：`design-frozen/not-implemented`；100 ms tick、500 ms watchdog 与 safe duty 0 已固定。

1. Zephyr 启动、session 改变和网络重连时默认 `SAFE`，`duty=0`。
2. 只有 CRC、ICPC 组合、session、sequence、业务 schema、request、范围和 `validity_ms==500` 全部有效且为新的 APPLY_OUTPUT，才能在下一个 100 ms 周期边界应用并刷新 watchdog。SET_TARGET 更新目标但不刷新，ENTER_SAFE 立即安排安全化且不刷新。
3. 重复包只重发 ACK；HEARTBEAT、STATUS、ERROR、SET_TARGET、ENTER_SAFE、非法/陈旧 CONTROL 均不刷新 actuator watchdog。
4. 从最近一次有效新 APPLY_OUTPUT 的接收单调时间起满 500 ms 时设置 pending-safe；最迟在紧随其后的 100 ms tick 开始前把 duty 置零，并设置 `SAFE|NETWORK_TIMEOUT`。没有任何 APPLY_OUTPUT 的启动态从网络线程就绪时开始同一计时。恢复必须由当前 transport 的新非零 session 中有效新 APPLY_OUTPUT 显式发生。
5. Linux 推理结果先检查有限值、模型版本并 clamp；Zephyr 再独立检查线上范围。两端任何一侧校验不能代替另一侧。
6. MLP 故障可以由 Linux 明确切换到 `FIXED_BASELINE`，但必须发送新 request 并在 evidence 中形成模式边界；不得悄悄把固定算法记录成 MLP。

实现必须用单调时钟，watchdog 不受墙钟跳变影响。固定 PI/MLP 都以 10 Hz 发送 APPLY_OUTPUT，因此正常情况下每个安全窗口有 5 个新 request。正例覆盖启动安全、连续控制、显式 ENTER_SAFE、MLP↔FIXED 切换和新 session 恢复；负例覆盖 Linux crash、恰在 499/500/501 ms 的边界、重复旧控制、`validity_ms!=500`、发送端 700 ms 重传、非法 duty、错误模型版本和 UDP/TCP 双源。每次动作变化必须能由 `session_id + sequence + request_id + sample_index` 追溯。

## 11. IF-010 时间、实时与闭环指标

状态：指标语义 `frozen`；具体门槛和场景见 `requirements.md` 与 `test-matrix.md`。

### 时钟规则

- Guest 内原始事件使用该 Guest 的 monotonic 纳秒时钟；wall clock 只用于文件命名和人类时间，不参与延迟计算。
- `IF-005.timestamp_ms` 是发送端 monotonic 毫秒截断值，只用于日志关联；RTT 必须由发送端本地的 send/ACK-receive 事件计算。没有时钟同步证据时，禁止用 Linux 时间减 Zephyr 时间计算单向延迟，也禁止接收端据此推断 packet 的绝对年龄。
- Linux 闭环延迟用同一 Linux 时钟计算：收到作为推理输入的 STATUS 到收到带匹配 `applied_request_id` 的后续 STATUS。Zephyr应用延迟用同一 Zephyr 时钟计算：有效 CONTROL 解码完成到动作实际生效。

### 原始事件最小 schema

端点必须输出 CSV 或 JSONL；每条至少包含：

`schema_version, run_id, endpoint, scenario, transport, session_id, sequence, request_id, sample_index, event, monotonic_ns, value, unit, outcome`

不可用字段写显式 `null`，不得用 `0` 混淆“未知”和真实零。`event` 至少覆盖 `period_release/start/finish`、`packet_send/receive`、`inference_start/finish`、`control_apply`、`safe_enter/exit` 和 `feedback_receive`。原始采集不能只输出汇总 percentile。

### 派生指标

| 指标 | 计算边界 |
|---|---|
| 周期抖动 | Zephyr 相邻 `period_start` 间隔减配置周期；报告样本数、mean、max、P99、P99.9 和 miss 数 |
| 调度/唤醒延迟 | 同一 Zephyr 时钟的 `period_start - period_release` |
| 执行时间 | 同一 Zephyr 时钟的 `period_finish - period_start` |
| 网络 RTT | 同一发送端 `ACK receive - first/last send` 两种口径都记录，不能混用 |
| 闭环延迟 | Linux 的 input STATUS receive 到匹配 feedback STATUS receive；超时样本进入失败分母 |
| 控制误差 | 从 Zephyr 原始等间隔样本计算 MAE/RMSE/IAE；报告单位和窗口 |
| 稳定时间 | 目标阶跃后首次进入并持续留在已声明误差带的时间；误差带和保持窗口必须写入 scenario |
| 超调量 | 目标阶跃方向上的最大越界，绝对值和目标幅度百分比都报告 |
| 成功率 | 成功逻辑 request / 所有发起 request；重传不增加分母 |
| 吞吐 | 明确 payload bytes 与 wire bytes 两种口径、方向和测量时长 |

percentile 算法、warm-up、丢弃规则、超时值和异常分类必须由版本化统计脚本固定，并能从原始记录复算。对照组与 MLP 组必须复用相同场景输入；若宿主为 WSL2，要在环境清单标注宿主调度噪声。

P3 生产改造的选择门槛固定为：同一候选指标在 3 个配对 run 中，AxVisor 关闭态相对原生基线的 P99.9 恶化均至少 15%，或额外延迟均至少 20 us。只按预先登记的 P99.9 相对差从高到低选择最多 2 条 timer/IRQ/vCPU wakeup/lock 路径；每条开启后仍用 3 组配对 run 验证。样本未过门槛、方向不一致或噪声无法分类时不改生产语义，只交付基线与限制说明。

## 12. IF-011 证据发布合同

状态：schema 与发布语义 `design-frozen`；部分底层惯例已实现，端到端 publisher/validator `not-implemented`。
生产者：所有 runner、构建器和统计器。
消费者：validator、`ARC-009` 交付者和评审者。

实现落点已经固定，不再由各阶段自行另造一套格式：`P2-DUAL-01` 首先新建 `scripts/contest/evidence/schema.py`、`publish_session.py`、`validate_session.py` 与 `scripts/test/check_contest_evidence_session.py`，并为现有 P2-DMA schema v1 提供只读 adapter；P2-SOAK、P3、P4、P5 和 P6 复用该 publisher。`P7-DELIVER-01` 再新建 `scripts/contest/evidence/validate_test_report.py` 与对应合同，把 `results/contest/tests/<TEST-ID>.json` 机械映射到本合同和 `test-matrix.md`，不得复制第二套状态机。

每次运行使用不可覆盖的新目录，并至少包含：

| 工件 | 最小内容 |
|---|---|
| `session.json` | schema、run ID、nonce、commit、dirty 状态/补丁哈希、宿主/QEMU/工具版本、PID/start-time/boot-id、开始/结束 monotonic 时间 |
| `manifest.json` | 所有输入和输出的 repo-relative 名称、大小、SHA-256、producer；不得把自身哈希递归写入自身 |
| `commands.jsonl` | argv、cwd、开始/结束、exit code；敏感值脱敏但参数结构可审计 |
| Guest/raw logs | Linux、Zephyr、AxVisor 分流原件；记录 byte 数、首末 sequence、drop 和 DMA-attempt 计数 |
| network capture | Guest↔Guest IP 场景的受控 pcap 及 filter/接口说明 |
| metrics raw | 符合 `IF-010` 的 CSV/JSONL，不被摘要覆盖 |
| `summary.json` | 场景、样本/失败分母、派生指标、异常、统计脚本版本 |
| `status.json` | 最后写入；规范化 `success: true|false`、稳定的 producer-specific `status` token、primary error、cleanup error、完成检查和 manifest hash。blocked preflight 还须给出 `blockedReason`；消费者不得只按 token 名猜测结果。 |

发布规则：

1. 只有所有必需工件存在、哈希复核、validator 成功且 cleanup 无残留后，才写 `success=true`。P2-DMA schema v1 的成功 token 固定为 `status=virtio_dma_effect_probe_completed`，失败 token 固定为 `status=virtio_dma_effect_probe_failed`；后续 publisher 必须显式适配这些 token，不能要求旧 evidence 被改写成通用 `status=success`。
2. 失败或 blocked 运行也保留原始工件；后续运行必须用新目录，不能修改失败包伪造成成功。
3. `results/baseline/runs/` 可保留本地大文件，但仓库必须提交 schema、validator、可公开摘要和索引；公开摘要逐条链接到真实证据身份。
4. 静态、host、build、single-Guest、dual-Guest、30 分钟/长稳、Guest IP、AI 闭环是不同等级。manifest 必须声明唯一最高等级，低级证据不得提升结论。
5. video、截图和人工描述是辅助材料，不能替代机器可读 status、原始日志、pcap 和可复算数据。

正例覆盖成功包和预期失败包的完整发布；负例覆盖缺文件、篡改 hash、复用 nonce、PID 身份错配、status 过早写入、cleanup 残留、跨 session 拼接和 host ICPC 冒充 Guest IP。validator 对任何不完整证据都必须非零退出。

## 13. 接口落地顺序

1. `IF-001` 静态拓扑持续通过；实现 `IF-002/DEC-007` 后在两份 VM 配置中加入合成 frontend，并继续排除所有 outer VirtIO-net slot。不得把 Zephyr outer slot 2 移出排除表。
2. 取得同一 session 的双 Guest/设备证据后，实现已经由 `DEC-008` 冻结的 `IF-003/004` 地址与端口，按 ICMP → UDP → TCP 验证；不得在实现期另选第二套拓扑。
3. 保持 `IF-005/007` host 合同全绿，把同一 codec 接到两 Guest；按已经由 `DEC-004/IF-006` 冻结的 ICCP↔ICPC 映射发布双方黄金向量。
4. 先为 `IF-008/009` 写双方 codec、范围和 watchdog 负例，再接入固定策略，最后接入 MLP。
5. 从第一条 runtime 起遵守 `IF-010/011`，禁止完成后补造时间线或从摘要反推原始数据。

任一步骤被阻塞时，保留失败证据并回到对应 `ARC/IF/DEC`；不得跳过安全门直接把后置功能标成完成。
