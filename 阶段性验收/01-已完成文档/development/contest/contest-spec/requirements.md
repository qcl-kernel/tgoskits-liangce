# 竞赛项目规范化需求

最后更新：2026-08-20

## 1. 如何使用本文

本文把 `sources-and-decisions.md` 中的两份有效 DOCX 承诺拆成可分配、可测试、可交付的规范要求。开发者应先找到自己的 `REQ-*`，再按“工作包/实现位置”开发，按“验收标准”收集 evidence，最后更新 `traceability.md`。没有满足验收标准和证据等级，就不能把状态改成“已满足”。

规范词义：

- **必须**：基础验收要求，不得在未批准情况下删除；
- **应**：推荐实现，偏离时必须在 `sources-and-decisions.md` 登记理由；
- **可以**：增强项，不影响基础验收；
- **当前状态**：截至 2026-08-20 对仓库和已登记证据的保守判断，不是目标状态。P4 的 runtime smoke 与 TEST qualification 是不同状态。

状态只能使用：`已满足`、`部分完成`、`静态合同`、`受阻`、`未开始`、`延后-待批准`。其中静态检查、host 测试、single-Guest、dual-Guest、真实 Guest IP、AI 闭环是不同证据等级，不能互相替代。

## 2. 需求总表

| 需求 ID | 一句话目标 | 主工作包 | 当前状态 | 最关键缺口 |
|---|---|---|---|---|
| `REQ-PLAT-001` | 同一 AxVisor/QEMU 会话同时运行 Linux 与 RTOS | `P2-DUAL-01` | 已满足（Linux+Zephyr v1） | r27 双 READY/DTB/300 秒；r31 1,800 秒 coexistence。原始其他 Guest/架构候选未覆盖 |
| `REQ-PLAT-002` | Linux 至少 2 vCPU，资源/启动配置完整且互斥 | `P2-DUAL-01` | 已满足（P2 边界） | r27 绑定 Linux `[0,1]`/Zephyr `[2]`、final DTB、运行时归属与 cleanup；P4 网络设备另验 |
| `REQ-PLAT-003` | 提供申报平台的独立可复现路线 | `P2`、`P7` | 静态合同；范围变更待批准 | AArch64 v1 已冻结但 clean-clone 未验收；RISC-V/板卡延后仍需负责人批准 |
| `REQ-RT-001` | 固定 RTOS vCPU/pCPU 并控制调度干扰 | `P3-RT-01` | 静态合同 | 运行时绑定/优先级证据 |
| `REQ-RT-002` | 对 timer/IRQ/调度关键路径做可开关优化 | `P3-RT-01` | 未开始 | 基线、设计和生产补丁 |
| `REQ-RT-003` | 完成原生与虚拟化、空载与压力的实时 A/B | `P3-RT-01` | 静态合同 | host schema/statistics、六场景矩阵和 native probe 合同已有；仍缺原始 runtime 样本与 A/B |
| `REQ-NET-001` | 真实 Guest TCP/UDP/IP 作为主链路 | `P4-UPSYNC-02`、`P4-EVID-01`、`P4-SMOKE/REL` | 部分完成 | 旧 HEAD 有真实 ICMP/UDP/TCP/ICPC narrow smoke；须适配官方 21ef、补 Guest-runtime evidence schema 并重跑 qualification |
| `REQ-NET-002` | 冻结 MAC/IP/路由/端口/拓扑 | `P4-UPSYNC-02`、`P4-SMOKE-02` | 部分完成 | 固定 vnet0/MAC/IP 已在旧 HEAD runtime；新官方 HEAD 的 resolved graph/final DTB/runtime 仍须复验 |
| `REQ-NET-003` | 控制/状态/错误/ACK/心跳协议可互操作 | `P4-SMOKE-02`、`P4-REL-01` | 部分完成 | Guest CONTROL→ACK+STATUS narrow smoke 为 85/100；完整 heartbeat/error/fault/1,000 CONTROL 未验收 |
| `REQ-NET-004` | 超时、重传、去重、乱序和重启可恢复 | `P4-REL-01` | 部分完成 | host 合同与 Guest happy path 已有；真实 fault/retry/restart/exactly-once qualification 未完成 |
| `REQ-AI-001` | Linux 运行真实轻量 MLP 推理 | `P5-AI-01` | 静态合同 | host 数据/模型/训练/导出/验证工具已有；仍无冻结模型工件和 Linux Guest 推理应用/runtime |
| `REQ-AI-002` | 推理经 IP 驱动 RTOS 动作并回传状态 | `P5-AI-A/B/C/Q` | 部分完成 | 两端网络 skeleton 已运行；真实模型、动作/plant STATUS 与端到端闭环未实现 |
| `REQ-AI-003` | 与固定策略比较至少两个指标并安全回退 | `P5-AI-01` | 静态合同 | host plant/PI/watchdog/metrics 合同已有；仍缺真实 Guest 对照实验、动作与安全回退 runtime |
| `REQ-QUAL-001` | 完成空载、组合压力和长稳矩阵 | `P6-QUAL-01` | 未开始 | 双 Guest/IP/AI 前置条件 |
| `REQ-QUAL-002` | 验证隔离负例、错误输入和恢复 | `P2`、`P6` | 部分完成 | 真实综合故障注入与边界证明 |
| `REQ-DEL-001` | 交付源码、配置、脚本、数据、文档和视频 | `P7-DELIVER-01` | 部分完成 | 应用/模型/结果/视频缺失 |
| `REQ-DEL-002` | 在私有比赛仓库交付完整历史和可审查 commit series，并形成 Apache 兼容、相对 upstream dev 可复核的离线 patch/commit bundle；禁止 PR | `P7-DELIVER-01` | 未开始 | 官方历史 mirror、dirty tree 拆分、私有分支 push、离线审查包和 clean-clone 应用验证 |
| `REQ-DEL-003` | 干净环境独立复现且每项结论可追证据 | `P7-DELIVER-01` | 部分完成 | clean-clone 全流程演练 |

## 3. 平台与混合系统

### REQ-PLAT-001：Linux 与 RTOS 同平台并发运行

- **来源**：`SRC-TECH-001` 的总体目标、总体架构和启动/隔离验收；`SRC-APP-001` 的混合关键系统目标与考核指标。
- **规范要求**：同一个 AxVisor 实例和同一个可绑定 QEMU 会话中，必须同时启动一个 Linux Guest 和一个 Zephyr Guest。Linux-only 与 Zephyr-only 是 `DEC-001/002` 冻结的 v1 工程设计；这不删除原始承诺，也不证明两 Guest 已运行。
- **验收标准**：
  1. 同一 run 的 QEMU identity、AxVisor build、两份 VM 配置和镜像 SHA-256 可追溯；
  2. Linux 与 Zephyr 各产生唯一、可区分的启动 READY 和 console 流，日志无串线；
  3. 两 Guest 在 2–5 分钟 short smoke 内并发存活，无 panic、未分类退出或重复启动；
  4. 关闭后没有残留 QEMU、launcher、socket、pidfile 或临时 runtime 目录；
  5. 30 分钟及更长稳定性另由 `REQ-QUAL-001` 验收。
- **工作包/实现位置**：`P2-DUAL-01`；`os/axvisor/configs/vms/qemu/aarch64/*-dual.toml`；`scripts/contest/run_dual_guest_smoke.py`；r27/r31 已关闭当前 Linux+Zephyr v1 门禁。
- **测试/交付**：`scripts/test/check_axvisor_dual_guest_configs.py`、`check_axvisor_dual_guest_topology.py`、Guest-DTB/console validators，以及 identity-bound dual run evidence。
- **当前状态**：`已满足（Linux+Zephyr v1）`。r27 在同一 QEMU identity 中绑定双 READY、两份 final Guest-DTB、300 秒稳定窗口和无残留；r31 延伸至 1,800 秒。早期 READY timeout 包保持 failed-attempt。
- **未证明边界**：单 Guest 历史运行、两份独立日志、静态 TOML 或外层 QEMU 两块 NIC 都不证明双 Guest 并发。

### REQ-PLAT-002：vCPU、内存、设备、中断和启动参数完整可审计

- **来源**：`SRC-TECH-001` 的部署配置要求和启动/隔离验收；`SRC-APP-001` 的虚拟化实时工作方向。
- **规范要求**：Linux/StarryOS 必须至少获得 2 vCPU；每个 Guest 的 vCPU→pCPU、内存范围/类型、设备、IRQ、内核入口、镜像、DTB、boot args 必须显式配置且跨 VM 不冲突。当前基线固定 Linux `phys_cpu_ids=[0,1]`、Zephyr `[2]`，pCPU3 留给 Host/console drain；Linux RAM 为 `0x80000000+0x10000000`，Zephyr RAM 为 `0x40000000+0x08000000`。
- **验收标准**：
  1. 静态 validator 证明 CPU、RAM、MMIO、SPI/INTID 和设备 ownership 无重叠；
  2. final Guest-DTB 与每份 VM 配置逐字段一致，未泄漏其他 VM 或 Host 专有设备；
  3. Linux runtime 报告至少 2 个可用 CPU，Zephyr 仅使用其分配 CPU；
  4. evidence 记录内核/DTB/rootfs/配置哈希、QEMU 参数和 boot args；
  5. 实际枚举设备与声明 ownership 一致，未分配设备不可访问。
- **工作包/实现位置**：`P2-DUAL-01`；两份 dual TOML、`configs/contest/qemu-aarch64-linux-zephyr-dual.toml`、Guest-FDT 与 resource-claim 模块。
- **测试/交付**：dual config/topology、Guest-DTB、stage-2、resource-claim、console validators；双 Guest session manifest。
- **当前状态**：`已满足（P2 边界）`。r27 绑定 Linux `[0,1]`、Zephyr `[2]`、双 READY、两份 final DTB、300 秒和 cleanup；P4 合成网络设备的枚举/ownership 由 TEST-011 另行验证。
- **未证明边界**：静态地址不证明 stage-2 runtime、DMA ownership、IRQ 实际路由或 Guest 内可见 CPU。

### REQ-PLAT-003：申报平台路线可独立复现

- **来源**：`SRC-TECH-001` 的 QEMU AArch64/RISC-V 复现路线和开发板增强路线；`SRC-APP-001` 的部署及交付计划。
- **规范要求**：v1 基础交付固定提供一条从干净环境可复现的 QEMU AArch64 路线。RISC-V 与开发板不进入 v1 工程范围，但原始承诺不删除；最终对外材料须由项目负责人签字确认其优先级变化。
- **验收标准**：
  1. AArch64 从 clean clone 按锁定工具链、依赖、镜像哈希和命令完成 build→boot→dual→IP→AI；
  2. 运行日志记录主机、WSL/OS、QEMU、Rust/编译器、Zephyr SDK 等版本；
  3. RISC-V 或开发板若在后续纳入范围，须完成同等级独立复现；v1 不得声称已完成它们；
  4. 开发板结果不得替代 QEMU 主线，也不得由 QEMU 结果外推。
- **工作包/实现位置**：P2 各运行包与 `P7-DELIVER-01`；AxBuild/xtask、QEMU 配置和复现脚本。
- **测试/交付**：clean-clone transcript、environment manifest、镜像/配置哈希、完整 evidence index。
- **当前状态**：工程证据为 `静态合同`；原承诺范围变更为 `延后-待批准`。QEMU AArch64-only v1 设计已冻结；AArch64 clean-clone 端到端验收与 RISC-V/板卡实现均未完成，最终对外范围仍需负责人签字。
- **未证明边界**：AArch64 构建/单 Guest 不能证明完整 AArch64 闭环，更不能证明 RISC-V 或板卡。

## 4. 实时性

### REQ-RT-001：RTOS 处理器绑定与干扰控制

- **来源**：`SRC-TECH-001` 的 vCPU/pCPU 绑定、RTOS 高优先级和资源隔离要求；`SRC-APP-001` 的实时性工作方向。
- **规范要求**：RTOS vCPU 必须固定到专用 pCPU，Linux vCPU 不得进入该集合；Host 管理/console 工作不得无界占用 RTOS pCPU。RTOS 周期控制任务优先级、周期、预算和后台任务必须写入配置与日志。
- **验收标准**：
  1. 静态 CPU 集互斥，运行时 vCPU enter/exit 或等价 marker 与分配一致；
  2. Zephyr 周期任务优先级高于网络解析、日志和非关键后台任务；
  3. 压力运行中没有未解释的 CPU 迁移、长期抢占或 console 阻塞；
  4. 所有绑定和优先级可通过配置开关复现，不依赖手工交互。
- **工作包/实现位置**：`P3-RT-01`，依赖 `P2-DUAL-01`；vCPU runtime、manager、Zephyr 应用配置和采集脚本。
- **测试/交付**：CPU ownership 静态合同、双 Guest runtime trace、Zephyr task manifest、压力对照日志。
- **当前状态**：`静态合同`。dual TOML 固定了 `[0,1]`/`[2]`，但调度优先级和运行时干扰尚无完整证据。
- **未证明边界**：配置中的 `phys_cpu_ids` 不自动证明实际调度、实时优先级或更低 jitter。

### REQ-RT-002：timer、IRQ 与调度关键路径优化

- **来源**：`SRC-TECH-001` 的 timer/IRQ、锁临界区和后台任务优化路线；`SRC-APP-001` 的 AxVisor 实时改造交付要求。
- **规范要求**：必须先用同钟事件分解 `Guest timer expiry → virtual IRQ enqueue → owning vCPU wake → vCPU re-entry → Guest handler`，再按固定算法选择最多 1–2 个明确瓶颈：候选须在 3 组配对基线中都贡献总 P99.9 `>=15%` 或绝对 `>=20 us`。只能对获准路径做可审查、可关闭生产改动；若无候选达标，必须保留“无数据支持优化”的未完成结论，不能凭直觉改调度器。
- **验收标准**：
  1. `contest-realtime-path.md` 说明调用路径、测点、瓶颈证据、假设与风险；
  2. 回归测试在旧实现失败、在新实现通过，且覆盖边界/失败路径；
  3. 对应 crate test、clippy、rustfmt 和正式 AArch64 AxBuild 通过；
  4. 相同镜像/配置的 A/B 数据满足 `REQ-RT-003`，功能、安全和生命周期门禁不回归；
  5. 未观察到改善时必须如实记录，不得把代码变化等同于实时提升。
- **工作包/实现位置**：`P3-RT-01`；`virtualization/axvm/src/runtime/`、`virtualization/arm_vcpu/`、`virtualization/arm_vgic/`、`os/axvisor/src/manager.rs`，只改被数据指向的最小路径。
- **测试/交付**：`scripts/contest/rt/` 的事件 schema、统计、六场景矩阵、native probe 和候选选择 host 合同已实现；仍需定向 Rust 测试、A/B patch/config、原始 trace 与分析文档。
- **当前状态**：`部分完成`（仅 host/static）。已有 schema/statistics/matrix/native probe/candidate selector 合同，但没有按本需求登记的 runtime 瓶颈基线和生产 A/B 改动。
- **未证明边界**：host benchmark、单次周期输出、代码审阅或构建成功不证明运行时改善。

### REQ-RT-003：实时指标、场景和原生基线

- **来源**：`SRC-TECH-001` 的 jitter、调度延迟、中断响应、最大延迟、长稳与空载/压力对比；`SRC-APP-001` 的量化数据要求。
- **规范要求**：必须测量周期 jitter、调度/唤醒延迟和中断响应中的可实现指标，并报告平均、最大、P99、P99.9、missed deadline 与异常样本；与原生/裸 Zephyr 及优化前 AxVisor 比较。
- **验收标准**：
  1. 场景至少包含原生 Zephyr、AxVisor 空载、Linux CPU 压力、网络压力、AI+网络组合压力；
  2. 每场景至少 3 个独立 run，每 run 至少 100,000 个周期样本或持续 30 分钟（取更严格者）；
  3. 原始 CSV/JSON 含单调时间、样本定义、周期、miss 标记、环境和镜像/配置哈希，统计表可由脚本重算；
  4. 优化声明要求预先指定的 P99.9 或最大值在 3 组配对 run 中方向一致改善，其他主指标不得出现未解释的显著回归；
  5. WSL2/QEMU 宿主噪声必须明确标注，不能冒充硬件实时上界。
- **工作包/实现位置**：`P3-RT-01`；`scripts/contest/rt/`、Zephyr 测量任务、`contest-realtime-path.md`。
- **测试/交付**：采集器单元测试、统计器黄金样本、原始数据、机器可读 summary、A/B 报告。
- **当前状态**：`静态合同`。host 侧事件 schema、统计器、六场景 run matrix、native probe 和候选选择合同已实现并通过对应 host 检查；尚无规定样本量的原生/虚拟化 runtime 数据，也没有可据此宣称的生产 A/B 改善。
- **未证明边界**：平均值不能替代 tail/max；单 run 不能证明稳定改善；虚拟环境结果不能外推到开发板。

## 5. Guest IP 与应用协议

### REQ-NET-001：TCP/UDP/IP 为 Guest 主通信链路

- **来源**：`SRC-TECH-001` 的 IP 双向通信与错误恢复；`SRC-APP-001` 的网络协议工作方向和验收表。
- **规范要求**：Linux 与 Zephyr 的控制主链路必须通过各自 Guest 内网络栈和 AxVisor 软件拷贝 mediated VirtIO-net 使用 IPv4 UDP/TCP；内部 bounded L2 仅连接 vnet0 双端。共享内存、HyperCall、裸 MMIO、console、vsock、outer-QEMU `hub0` 或 slot2 直通均不能替代主链路。
- **验收标准**：
  1. 同一 dual-Guest run 中两 Guest 枚举各自独立 NIC；
  2. 固定地址 ping/等价 L3 reachability、UDP echo 和 ICPC 业务流依次通过；
  3. evidence 同时绑定 QEMU/VM identity、mediated device config、最终 Guest-DTB、Guest 网卡、MAC/IP/端口、内部 frame capture/PCAP、收发日志和 session；
  4. Linux→Zephyr 控制和 Zephyr→Linux 状态/错误均有实际 IP 数据报；
  5. 若启用 TCP 回退，仍须证明真实 Guest IP、分帧和重连。
- **工作包/实现位置**：`P4-UPSTREAM-01`、`P4-NET-01`；官方 axvirtio/device graph/DmaGrant 双端、bounded vnet0 L2、两端应用和网络 runner。
- **测试/交付**：NIC enumeration、L3、UDP echo、TCP fallback、ICPC Guest integration 和 pcap evidence。
- **当前状态**：`部分完成/需前移复验`。本地 `574d569be...` 已在 mediated vnet0 上观察 ICMP/UDP/TCP/ICPC narrow smoke；但官方已前进到 `21ef4b218...` 并引入 issuing-vCPU DeviceContext 与 CI v3。当前 runner/profile 也不足以关闭 TEST qualification。先完成 P4-UPSYNC-02/P4-EVID-01，再在新 HEAD fresh run；outer slot2 继续禁止直通。
- **未证明边界**：host loopback ICPC、QEMU 参数中的两块 NIC、single-Guest NIC 或 ping host 都不证明 Linux↔Zephyr IP。

### REQ-NET-002：拓扑、MAC、IP、路由与端口冻结

- **来源**：`SRC-TECH-001` 的拓扑、MAC/IP、路由、端口、bridge/NAT 文档要求；`SRC-APP-001` 的网络配置和日志交付要求。
- **规范要求**：v1 网络合同固定为 AxVisor 内部 isolated `vnet0`（不是 outer-QEMU hub0/TAP/bridge），Linux `02:00:00:00:00:01` / `10.77.0.1/24`，Zephyr `02:00:00:00:00:02` / `10.77.0.2/24`，两端仅 connected route、无默认网关/DNS/NAT/bridge/宿主代理；UDP `46000` 主线与 TCP `46001` 显式新 session 回退；配置必须机读并由两端共同使用。
- **验收标准**：
  1. 不需要开发者猜测或手改源码即可生成两端网络配置；
  2. validator 证明 MAC、IP、端口唯一且与 AxVisor mediated config、Guest、final DTB 和内部 capture/PCAP 一致；
  3. 首版同网段直连时明确写“无默认网关、无 NAT”；若使用 bridge/NAT，则记录 host 地址、路由和防火墙；
  4. 端点重启后配置可重复，证据中没有 DHCP 或随机端口漂移；
  5. 拓扑图、配置、runner 和抓包四者一致。
- **工作包/实现位置**：`P4-NET-01`；`configs/contest/`、两端应用配置、network runner/validator。
- **测试/交付**：静态 topology validator、Guest `ip addr/route` 或 Zephyr 等价输出、socket bind 日志、pcap endpoint 审计。
- **当前状态**：`部分完成/需前移复验`。vnet0、MAC/IP、路由和端口已冻结并在旧 HEAD Guest runtime 使用；resolved Auto MMIO/IRQ 也有运行观察。新官方 HEAD 的 final DTB/resource/runtime 绑定以及资格级 capture/PCAP 仍未完成。
- **未证明边界**：配置文本不证明 Guest 使用该地址，也不证明端口可达或双向传输。

### REQ-NET-003：版本化控制/状态/错误协议

- **来源**：`SRC-TECH-001` 的 ICCP 字段表、三类业务消息及 heartbeat/ACK；`SRC-APP-001` 的协议验收项。
- **规范要求**：公开材料首次写作“ICCP（仓库实现名 ICPC v1）”；代码、wire magic 和测试继续采用 `ICPC v1` 固定 36 字节头、网络字节序和最大 1024 字节 payload。它是 v1 受控字段映射，不等于申报 ICCP 的逐字节布局。
- **验收标准**：
  1. 线格式、应用 payload、取值范围、单位和错误码在架构合同中完整定义；
  2. Linux 与 Zephyr 使用同一 C99 协议库或同一黄金向量，编解码字节完全一致；
  3. CONTROL 触发一次且仅一次动作，STATUS 关联请求，ERROR 携带可解释错误，ACK/HEARTBEAT 遵守组合规则；
  4. 截断、尾随字节、未知版本/类型/flags、超长 payload、错误 CRC 全部拒绝；
  5. host 合同、两 Guest 单元测试和真实 Guest 互操作全部通过。
- **工作包/实现位置**：`P4-NET-01`；`scripts/contest/icpc/`、`contest-icpc-protocol.md`、两端应用协议 adapter。
- **测试/交付**：`scripts/test/check_icpc_protocol.py`、黄金报文、Guest build/test、真实 UDP/可选 TCP evidence。
- **当前状态**：`部分完成`。ICPC v1 host 合同和 Guest codec 已存在；旧 HEAD 已观察 CONTROL→ACK+STATUS `sent=100 verified=85 loss=15`。仍缺 HEARTBEAT/ERROR、1,000 CONTROL、fault/restart 与 exactly-once/cancel 的资格证据。
- **未证明边界**：host 测试只证明协议库，不证明 VirtIO-net、Guest IP、业务动作或申报字段偏差已获接受。

### REQ-NET-004：可靠性、错误恢复与网络指标

- **来源**：`SRC-TECH-001` 的 ACK、超时重传、乱序/重复、断连恢复、成功率/延迟/吞吐；`SRC-APP-001` 的恢复与量化验收。
- **规范要求**：UDP `46000` 主线使用 ACK、100/200/400 ms 退避、最多 3 次重传、序号比较、去重和 session 重置；不得无界重试或重复执行控制动作。TCP `46001` 仅可在 UDP 明确失败后以新非零 session 显式回退，并实现 framing、超时、断连和重连。
- **验收标准**：
  1. 无故障 CONTROL/STATUS/ERROR/heartbeat 流成功；
  2. 确定性注入丢包、重复、乱序、CRC 损坏、延迟、端点重启和网络断开；
  3. 重复 CONTROL 只产生一次动作，旧/歧义序号被拒绝，新 session 清除旧去重状态；
  4. 达到重试上限后进入显式超时/安全状态，网络恢复后在有界时间内重新建立业务流；
  5. 报告总消息、成功、超时、应用错误、重复、重传、恢复时间、RTT 和吞吐，原始数据可复算。
- **工作包/实现位置**：`P4-NET-01`；ICPC reliability、network fault injector、Guest applications。
- **测试/交付**：确定性 host 状态机测试、双 Guest netem/代理或等价故障注入、pcap、machine-readable counters。
- **当前状态**：`部分完成`。协议/host 合同与无故障 Guest happy path 已存在；真实 drop/duplicate/reorder/corrupt、timeout/retry、session restart 和逐包/动作指标仍未完成。
- **未证明边界**：CRC 不是安全认证；RTT 不能直接转成未同步 Guest 间单向延迟；host 丢包模拟不证明 Guest 恢复。

## 6. AI 控制闭环

### REQ-AI-001：真实轻量神经网络推理

- **来源**：`SRC-TECH-001` 的小型 MLP、输入/输出和 Linux/StarryOS 推理路线；`SRC-APP-001` 的 AI 工作方向。
- **规范要求**：Linux 必须执行真实小型神经网络前向推理，不能用查表、随机数或固定常量冒充。v1 固定使用 `3→8→1` 可审计 MLP，输入为 `{measured_mC, target_mC, previous_duty_q16_16}`，输出为 `duty_q16_16`；归一化、激活、训练/生成方法和模型 SHA-256 必须固定。
- **验收标准**：
  1. 数据生成、训练/权重生成和模型导出命令可重复；固定 seed 下模型哈希或数值容差稳定；
  2. 模型元数据包含数据 schema、输入/输出、归一化、层、版本、工具版本和 SHA-256；
  3. Linux 单元测试用黄金输入验证输出及边界值；
  4. runtime 日志区分输入采样、推理开始/结束、输出和模型版本；
  5. 模型大小和周期适合持续闭环运行，不因分配/日志造成无界延迟。
- **工作包/实现位置**：`P5-AI-01`；计划路径 `apps/contest/linux-ai-controller/` 及其 model/data tools。
- **测试/交付**：数据/模型 manifest、训练或权重生成脚本、Linux 推理程序、黄金向量与运行日志。
- **当前状态**：`静态合同/实现可开始`。host 侧数据生成、模型、训练/导出、golden-vector/metrics 验证工具已实现；Linux 程序仍是 network skeleton。P5-AI-A 可立即冻结 canonical model 并接 target C 推理，但尚无 Guest runtime。
- **未证明边界**：普通控制公式、固定 PID、host Python 推理或模型文件存在均不证明 Guest 内真实推理。

### REQ-AI-002：input→inference→IP→RTOS action→feedback

- **来源**：`SRC-TECH-001` 的完整闭环链路；`SRC-APP-001` 的 RTOS 控制动作和反馈验收。
- **规范要求**：必须按 v1 一阶温度对象和固定 payload 完成全链：Zephyr 温度状态→Linux 输入→MLP 推理→真实 Guest IP CONTROL→Zephyr heater duty→STATUS/ERROR 反馈。Zephyr 控制周期固定 100 ms，并可从日志观察。
- **验收标准**：
  1. 单一 run/session 中五个阶段都有带 request/sequence 的可关联 marker；
  2. pcap 中的 CONTROL/STATUS 与两端应用日志、动作状态和模型版本一致；
  3. 每条被确认 CONTROL 只应用一次，过期、重复或无效消息不改变 actuator；
  4. 至少完成一个目标阶跃和一个负载扰动场景，状态回传能驱动下一次推理；
  5. 连续闭环运行满足测试矩阵的时长和消息完整性门禁。
- **工作包/实现位置**：`P5-AI-01`；`apps/contest/linux-ai-controller/`、`apps/contest/zephyr-control/`、ICPC payload adapter。
- **测试/交付**：两端单元测试、Guest builds、端到端 runner、pcap、闭环 time-series 和演示脚本。
- **当前状态**：`部分实现/无 AI-loop`。两端应用目录与 P4 网络 skeleton 已建立，Zephyr 能回占位 STATUS；模型推理、真实 plant/action/feedback、identity-bound AI session 尚未实现。
- **未证明边界**：host 进程互发、只发送推理值、RTOS 只打印消息或没有反馈回路都不是闭环。

### REQ-AI-003：固定策略对照、至少两个指标和安全回退

- **来源**：`SRC-TECH-001` 的人工固定参数基线、控制误差/稳定时间/准确率/延迟等对比；`SRC-APP-001` 的至少两项量化指标和可观察控制效果。
- **规范要求**：MLP 控制必须与冻结的固定参数/人工策略在相同初态、目标和扰动下比较；至少报告两个预先定义指标。网络失联、协议错误或推理无效时，Zephyr 必须进入确定性安全状态；自最近有效新 CONTROL 起 500 ms 未刷新时，duty 必须归零并上报 `SAFE|NETWORK_TIMEOUT`。
- **验收标准**：
  1. 对照组和 MLP 组使用相同 plant、初态、目标序列、扰动、采样周期和 run seed；
  2. 至少报告控制 RMSE/IAE、settling time、overshoot、端到端 RTT 中两个，并给出定义、单位、样本数和原始时序；
  3. 不要求伪造“MLP 必然更优”；若没有改善，如实报告结果和原因，但闭环必须正确可复现；
  4. 超过架构合同规定的 CONTROL watchdog、连续协议错误或失联时 actuator 回到 safe value，并回报 health/error；
  5. 恢复只能在新 session/有效 CONTROL 后发生，不能重放旧动作。
- **工作包/实现位置**：`P5-AI-01`；两端应用、plant、experiment runner 和指标脚本。
- **测试/交付**：固定策略配置、MLP 配置、配对实验 manifest、统计结果、安全状态故障注入、演示片段。
- **当前状态**：`静态合同`。host 侧一阶 plant、固定 PI、mailbox/watchdog 和指标计算合同已实现；真实 Guest 对照实验、ICPC 动作链、safe-state runtime 与恢复仍未开始。
- **未证明边界**：只比较推理耗时、只展示曲线截图、不同扰动的两次运行或未定义安全值都不满足本需求。

## 7. 综合质量与隔离

### REQ-QUAL-001：空载、组合压力和长稳矩阵

- **来源**：`SRC-TECH-001` 的空载/Linux 压力/网络/AI、最大延迟和长时间稳定性；`SRC-APP-001` 的稳定可复现要求。
- **规范要求**：必须在相同可追溯构建上完成双 Guest 空载、CPU 压力、网络压力、AI 闭环和组合压力；先验收 30 分钟 dual coexistence，再进行数小时综合长稳。
- **验收标准**：
  1. 30 分钟 run 中两 Guest 持续存活、marker/identity/config/raw log byte-bound，且 cleanup 无残留；
  2. 综合 run 至少覆盖 Linux CPU 压力 + ICPC 网络 + AI 控制，并记录精确时长；
  3. 报告 panic/restart/unclassified exit、实时 tail、网络成功/超时/吞吐、控制指标和资源异常；
  4. 允许通过测试矩阵定义的多个 run 累积证据，但不得把互不绑定的旧日志拼成一次成功会话；
  5. 失败 run 原样保留，并分类 primary/cleanup 错误。
- **工作包/实现位置**：`P2-SOAK-01`、`P6-QUAL-01`；dual runner、`scripts/contest/qualification/`。
- **测试/交付**：soak session validator、场景 runner、原始日志/time-series、machine-readable summary 和 evidence index。
- **当前状态**：`部分完成`。r31 已提供真实 1,800 秒双 Guest coexistence、unsafe=0 和无残留；数小时 CPU+IP+AI 组合压力尚不存在。
- **未证明边界**：validator 的合成 fixture、短 smoke 或不含 IP/AI 的单 Guest 长跑不证明综合稳定性。

### REQ-QUAL-002：隔离负例、错误输入和恢复

- **来源**：`SRC-TECH-001` 的隔离、错误恢复和异常日志要求；`SRC-APP-001` 的资源边界与恢复验收。
- **规范要求**：系统必须对 CPU/内存/设备/IRQ ownership 冲突失败关闭；对网络协议错误、重复/乱序、Guest 重启和应用失联有界恢复；错误流量不得破坏 RTOS 周期任务。
- **验收标准**：
  1. 静态负例覆盖跨 VM CPU、RAM、MMIO、SPI/INTID 和设备重复声明；
  2. runtime 负例覆盖非法 FDT/资源、坏协议包、网络中断、Linux 应用崩溃和 Zephyr/端点重启；
  3. 每种故障具有预期错误码、拒绝点、恢复条件和残留检查；
  4. RTOS watchdog/safe state 在故障时生效，周期 miss 和最大延迟仍被记录；
  5. DMA 结论严格限定：P2 的 `MapReserved` Linux virtio-blk 单设备/单请求字节观察不能称 IOMMU/SMMU、通用 DMA isolation 或网络准入；P4 必须实现软件拷贝 mediated VirtIO-net、Guest memory context 与 bounded vnet0 L2。
- **工作包/实现位置**：P2 安全基础、`P4-NET-01`、`P6-QUAL-01`；resource claims、FDT、DMA probe、mediated device、Guest memory context、bounded L2 和 fault injector。
- **测试/交付**：静态负例合同、runtime failed-attempt 包、恢复 evidence、边界声明。
- **当前状态**：`部分完成`。除既有资源/Guest-FDT/DMA 静态合同和失败审计外，`phase2-virtio-dma-effect-20260813T001837Z-5328870-dirty-r24j` 已通过一次受控 virtio-blk 请求的 payload/guard 窄效果门禁；该结果不证明通用 DMA isolation，真实双 Guest 综合故障与恢复仍未完成。
- **未证明边界**：nonce/hash 绑定的单设备 probe 不证明硬件 DMA 隔离；错误被日志打印也不证明系统恢复或实时任务不受影响。

## 8. 交付与复现

### REQ-DEL-001：完整工程工件与演示

- **来源**：`SRC-TECH-001` 的实时、IP、AI、设计/测试/复现/视频交付清单；`SRC-APP-001` 的成果形式。
- **规范要求**：必须交付可审查源码、配置、runner/validator、协议、数据 schema、模型与元数据、可复算结果、设计/测试/复现文档和演示视频。大镜像/raw logs 可以不入 Git，但必须登记生成方法、大小和 SHA-256。
- **验收标准**：
  1. `deliverables.md` 每一项都有 owner、仓库路径、生成命令、验证命令、evidence 和状态；
  2. 不存在文档引用但缺失的脚本/配置，或只有截图没有原始数据的指标；
  3. 演示在 5 分钟左右依次展示双 Guest、真实 IP、AI 动作/反馈、实时 A/B 和故障安全状态；
  4. 所有第三方代码/模型/数据许可兼容，NOTICE/来源信息齐全；
  5. 最终包不含密钥、个人绝对路径、不可再生成的临时文件或超大 raw 工件。
- **工作包/实现位置**：各工作包产生工件，`P7-DELIVER-01` 收口；详见 `deliverables.md`。
- **测试/交付**：文档合同、license/size/secret scan、演示 rehearsal、交付 manifest。
- **当前状态**：`部分完成`。安全基础、配置、协议和开发文档已有；AI 应用/模型、综合数据、视频和最终 manifest 未完成。
- **未证明边界**：文件存在不代表可构建、可运行或满足相应证据等级。

### REQ-DEL-002：私有比赛仓库与 Apache 兼容的 upstream-dev 离线审查包（禁止 PR）

- **来源**：`SRC-TECH-001` 的 Apache 兼容、提交 tgoskits `dev`、无冲突要求；`SRC-APP-001` 的开源成果承诺。
- **规范要求**：正式仓库为私有 `qcl-kernel/tgoskits-liangce`，首次从官方 bare clone mirror 完整历史，之后以普通 push 维护 `contest/axvisor-ai-control`。最终实现必须拆成职责清晰、可审查的提交，以锁定 upstream `dev` SHA 为 base 生成离线 patch/commit bundle，并在干净 clone 中证明可应用且无未解决冲突；新增代码、依赖和资产必须许可证兼容。禁止创建或提交 PR，禁止直接推送 upstream。
- **验收标准**：
  1. 明确 upstream URL、只读目标 `dev` base SHA、本地 topic 分支和每个提交范围；
  2. clean worktree 上 rebase/merge 检查无冲突，CI 和目标架构门禁通过；
  3. `review-manifest.md/json` 按 `REQ-*` 列出改动、测试、evidence、风险、回滚和未证明边界；
  4. 不把本地 ignored evidence、镜像、缓存或无关用户改动提交；
  5. 许可证扫描和人工复核确认兼容 Apache-2.0 项目；
  6. 私有比赛仓库包含开发分支与提交历史；GitHub/GitLab 等远端不存在由本项目创建的 PR/MR，交付记录不得包含伪造的 PR URL。
- **工作包/实现位置**：`P7-DELIVER-01`；比赛仓库提交指南、Git 提交计划、CI、`git format-patch`、离线 review manifest。
- **测试/交付**：clean status、锁定 base 记录、CI URL/日志、license report、patch series、patch SHA-256、clean-clone `git am` transcript 和 reviewer 签字。
- **当前状态**：`未开始`。私有目标仓库已创建且保持为空，但官方历史 mirror、当前 dirty tree 提交拆分、开发分支 push、最终离线审查包和 clean-clone 应用证据均未完成；PR 被明确禁止。
- **未证明边界**：本地 commit、旧提交实机结果或当前分支能构建都不证明 upstream dev 无冲突。

### REQ-DEL-003：独立复现与结论追溯

- **来源**：`SRC-TECH-001` 的独立复现、脚本/日志/抓包/数据要求；`SRC-APP-001` 的“可运行、可测量、可复现”。
- **规范要求**：未参与开发的人必须能只按文档在干净环境复现基础链路，并从每条公开结论追到原始 evidence、验证器和输入哈希。
- **验收标准**：
  1. fresh clone/fresh evidence directory 按单一入口完成依赖检查、构建、运行、验证和 cleanup；
  2. README 不依赖隐含 shell 状态、个人绝对缓存或手工编辑；所有可变路径均为参数；
  3. 每个 evidence 包包含 status-last、session/result、原始日志、环境、输入哈希、命令、exit code 和残留检查；
  4. `traceability.md` 对每个 `REQ-*` 给出测试和交付物，未完成项明确为空而不是用低等级证据填充；
  5. 第二名开发者或全新环境完成一次 rehearsal，并记录偏差和修复。
- **工作包/实现位置**：贯穿全部工作包，`P7-DELIVER-01` 最终关闭；`scripts/contest/README.md`、results schema、CI 和复现指南。
- **测试/交付**：`scripts/test/check_ci_paths.py`、开发文档合同、clean-clone transcript、evidence manifest、traceability audit。
- **当前状态**：`部分完成`。已有多类可移植 Python 合同和 evidence 约束；完整 clean-clone dual/IP/AI 路线尚未存在。
- **未证明边界**：CI 中的静态/host 测试不证明 QEMU runtime；开发者本人重复运行不等于独立复现。

## 9. 需求状态变更规则

把任何需求改为 `已满足` 前，必须同时满足：

1. 本文验收标准全部通过，或存在已批准 `DEC-*` 精确修改该标准；
2. `test-matrix.md` 中对应测试行通过，且 evidence 是当前代码/配置/镜像生成；
3. `deliverables.md` 中对应工件存在并通过完整性检查；
4. `traceability.md` 能从来源→需求→决策→实现→测试→证据→交付双向追溯；
5. `现状.md` 只使用实际达到的证据等级，并明确仍未证明内容。

失败、超时和受阻不能用“基本完成”替代。旧提交或旧上游上的实机成功可登记为“历史实机证据”，但在当前代码上回归前不能关闭 latest-upstream 要求。
