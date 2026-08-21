# ICPC v1 工控客户机通信协议

## 0. 申报术语与版本追溯

两份原始申报材料把应用协议称为 `ICCP`；仓库当前源码、线格式 magic、测试和本文使用 `ICPC v1`。`DEC-004` 已把 v1 工程口径冻结为：公共材料首次写作 `ICCP（仓库实现名 ICPC v1）`，源码、wire magic、36-byte header 和测试继续使用 `ICPC v1`。这是受控映射，不表示两种字段布局逐字节相同；改变申报口径的最终对外材料仍需项目负责人签字。执行规则如下：

- 源码符号、wire magic 和现有测试继续使用 `ICPC`，避免无证据的破坏性重命名；
- 面向申报材料的矩阵首次写作 `ICCP（仓库实现名 ICPC v1）`，不得把两个名称无说明混写；
- 若未来要求把 `ICCP` 作为新的 wire 名称，必须发布新协议版本、迁移/兼容方案和正负例测试，不能原地改写 v1 历史证据。

原提纲给的是字段提议，不是已经发布的 ABI。当前 v1 对它做了以下已冻结、可审计映射；缩窄字段和时间单位变化必须在对外说明中保留：

| 原提纲字段 | 原提纲类型 | ICPC v1 字段 | 变化与理由 |
| --- | --- | --- | --- |
| `version` | `u8` | `version: u8`，另加 `magic[4]`、`header_length: u8` | 保留版本并增加快速拒绝错误协议/错误头长的能力 |
| `msg_type` | `u8` | `message_type: u8` | 语义保持，固定为五类消息 |
| `flags` | `u16` | `flags: u8` | v1 只有两个已定义 bit；未知 bit 失败关闭；v1 接受该受控缩窄 |
| `seq` | `u32` | `sequence: u32`，另加 `session_id: u32`、`ack_sequence: u32` | 保留序号并显式支持重启隔离和 ACK 关联 |
| `timestamp_ns` | `u64` | `timestamp_ms: u64` | 当前端点只承诺单调毫秒时钟；不据此计算跨 Guest 单向时延 |
| `payload_len` | `u32` | `payload_length: u16` | v1 单包上限 1024 字节，缩窄用于固定小报文并拒绝分片 |
| `error_code` | `i32` | `error_code: u16` | v1 使用非负枚举；负值语义没有实现；v1 接受该受控缩窄 |
| `checksum/crc32` | `u32` | `crc32c: u32` | 固定为 Castagnoli CRC32C，避免“CRC32”多项式歧义 |

需求来源、决策状态和最终验收映射见 `contest-spec/sources-and-decisions.md`、`contest-spec/requirements.md` 与 `contest-spec/traceability.md`。

## 1. 问题与成功标准

比赛要求 Linux/StarryOS 与 RTOS Guest 通过 TCP/UDP/IP 交换控制、状态和错误消息，并覆盖确认、超时、重传、重复包及乱序处理。v1 已固定 Linux + Zephyr 和 AxVisor 软件拷贝 mediated VirtIO-net/内部 `vnet0` 路线；该设备与双 Guest 运行链仍未实现。协议层可以独立验证，但 host 合同不能替代真实 Guest IP。

直接使用者是 Linux Guest 用户态控制程序、Zephyr Guest 控制任务和主机侧一致性测试工具。第一阶段完成标准如下：

- 固定且可版本化的网络字节序线格式；
- 支持 `CONTROL`、`STATUS`、`ERROR`、`ACK`、`HEARTBEAT` 五类消息；
- 包含会话、序号、确认序号、时间戳、载荷长度、错误码和 CRC32C；
- C99 实现不使用堆分配，可同时进入 Linux 和 Zephyr 构建；
- 对截断、尾随字节、未知版本/类型/标志、字段组合错误和校验错误全部失败关闭；
- 在 Windows 与 Linux 主机上可编译运行同一组协议测试。

## 2. 非目标

- 本协议不替代 VirtIO-net、UDP/IP、路由、MAC、ACL 或防火墙配置。
- 第一阶段不证明双 Guest 已启动、IP 已联通或 DMA 已隔离。
- CRC32C 只检测意外损坏，不提供认证、保密或抗重放安全性。
- v1 不支持 UDP 分片、跨包大消息、动态字段扩展或单向时钟同步。
- v1 时间戳只用于关联和发送端往返/闭环测量，不直接计算跨 Guest 单向延迟。

## 3. 项目内检索与方案比较

仓库中没有满足比赛字段和可靠性要求的 ICPC/ivcproto 实现。`ax-net` 提供 TCP/UDP socket 能力，但不负责比赛应用协议。现有 console frame 格式只用于宿主日志分流，不能充当 Guest 主通信链路。

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 直接使用 TCP | 可靠、有序、实现快 | 难以展示应用层 ACK、去重和重传评分点 | 工期失控时的回退方案 |
| UDP + 文本 JSON | 易调试 | 解析和长度边界复杂、RTOS 开销较大、线格式不稳定 | 不采用 |
| UDP + 固定二进制头 | 边界明确、开销小、便于 C 实现和故障注入 | 需要自行实现可靠性状态机 | 采用 |
| HyperCall/共享内存/vsock | 可能更容易先打通 | 不满足主链路必须基于 IP 的赛题边界 | 禁止作为主链路 |

## 4. 规范依据

- RFC 768：UDP 保留消息边界，但不保证交付、顺序或去重，因此可靠性必须由 ICPC 补充。
- RFC 1982：32 位序号使用有限序号空间比较；相差恰好 `2^31` 时顺序未定义，接收方必须拒绝该歧义。
- RFC 3309：采用 Castagnoli 多项式的 CRC32C，并以标准测试向量 `123456789 -> 0xe3069283` 固定实现。
- RFC 8085：UDP 应用必须控制重传速率、限制消息大小，并避免无界重试。v1 默认采用有限次数指数退避。

## 5. v1 线格式

所有整数使用网络字节序。固定头长 36 字节，最大载荷 1024 字节，单个 UDP 数据报最大 1060 字节。

| 偏移 | 长度 | 字段 | 约束 |
| ---: | ---: | --- | --- |
| 0 | 4 | magic | ASCII `ICPC` |
| 4 | 1 | version | 固定为 1 |
| 5 | 1 | header_length | 固定为 36 |
| 6 | 1 | message_type | 1..5 |
| 7 | 1 | flags | bit0 `ACK_REQUIRED`，bit1 `RETRANSMISSION` |
| 8 | 4 | session_id | 非零；每次端点启动重新生成 |
| 12 | 4 | sequence | 非零；每个发送数据报递增 |
| 16 | 4 | ack_sequence | 仅 `ACK` 非零 |
| 20 | 8 | timestamp_ms | 发送端单调时钟毫秒值 |
| 28 | 2 | payload_length | 0..1024，且数据报长度必须精确匹配 |
| 30 | 2 | error_code | 仅 `ERROR` 可非零 |
| 32 | 4 | crc32c | 校验时本字段按零处理，覆盖头和载荷 |

字段组合规则：

- `ACK` 必须无载荷、无 flags、`ack_sequence != 0`、`error_code == 0`。
- 非 `ACK` 的 `ack_sequence` 必须为零。
- `ERROR` 的 `error_code` 必须非零；其他类型必须为零。
- `HEARTBEAT` 必须无载荷。
- `RETRANSMISSION` 只能与 `ACK_REQUIRED` 同时出现。
- 未知 flags、版本、类型或额外尾随字节一律拒绝。

## 6. 可靠性状态机

协议库第二阶段在固定线格式上实现有限状态机：

1. 需要确认的业务消息设置 `ACK_REQUIRED`，发送后进入等待表。
2. 初始 RTO 为 100 ms，超时后设置 `RETRANSMISSION` 并指数退避到 200/400 ms。
3. 最多发送 4 次（首次加 3 次重传）；仍未确认则返回显式超时。
4. 接收方按 `(session_id, sequence)` 去重；重复包重新发送 ACK，但不得重复执行控制动作。
5. 控制指令只应用比最近已应用序号更新的消息；RFC 1982 歧义序号返回错误。
6. 会话变化时清空旧会话去重状态，避免端点重启后的序号冲突。

## 7. 验证与交付

第一阶段测试覆盖 CRC32C 标准向量、控制/状态/错误/ACK/心跳往返、网络字节序、序号回绕，以及全部失败关闭规则。测试用 GCC/Clang 编译 C99 实现并执行，随后接入 CI。

第二阶段增加丢包、重复、乱序、损坏、超时和会话重启的确定性状态机测试。真实网络阶段必须另外提供 Linux↔Zephyr 的 ping、UDP echo、ICPC 三类消息、故障注入、成功率、重传率、RTT、恢复时间和吞吐证据。

## 8. 回滚

协议库位于比赛专用目录，不改变 AxVisor、Linux、Zephyr 或网络栈默认行为。未接入 Guest 前可直接移除；接入后如 UDP 可靠性无法按期完成，可保留同一消息头并切换到 TCP 分帧传输，但必须更新版本/传输文档和测试证据。
