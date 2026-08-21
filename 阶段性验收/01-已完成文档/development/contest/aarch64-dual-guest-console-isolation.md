# AArch64 双 Guest Console 隔离设计

状态：Phase 1A/1B 源码接线完成；最新 Rust 门禁与运行时证据待验证

审计日期：2026-07-31

代码基线：`rcore-os/tgoskits` `dev`，`01e053105e243d1fdd1c03f6f850b266efbe8de0`

风险级别：高风险（宿主控制台、Guest MMIO/IRQ 所有权、启动证据与故障可观测性）

实现快照（2026-07-31）：

- VM-local TX-only PL011、严格 frame emitter、AArch64 Guest FDT console 注入、Linux `earlycon`、Zephyr polling-only 构建模板和 AxVisor 默认 VM 生命周期的单消费者 drain 已完成源码接线；双 Guest 配置仍保持 `blocked_dma_console`。
- 当前源码的 20 个 Python 合同与 `git diff --check` 通过；最新 Rust `fmt`、单测、Clippy 和 AArch64 构建因额度网关在执行前拒绝而未运行，不能复用此前结果。
- 尚未重编译 Zephyr，尚无真实 QEMU frame、per-VM demux、最终 Guest DTB 或双 Guest console 证据。
- 当前 drain 不覆盖动态 shell 创建 VM；没有 sink 两阶段确认/失败重试；设备销毁重建后的 generation 连续性未建立。这些边界必须在动态 gate 中失败关闭或另行实现。
- Linux synthetic 节点无 clock/IRQ；标准驱动的 probe、boot-console 交接与用户态 `/dev/console` 尚未验证，当前只承诺 earlycon 源码合同。

## 1. 决策摘要

当前 QEMU AArch64 拓扑不能把 `0x0900_0000` 的 PL011 同时交给 AxVisor、Linux 和 Zephyr。审计基线中只有一个实际 PL011，宿主 FDT 将它设为 `stdout-path`，AxVisor 启动后也据此直接访问该 MMIO。该 UART 对应 GIC SPI offset 1，即物理 INTID 33；MMIO 与中断必须作为同一个宿主资源保留。

仅从运行时 Guest DTB 中删除 `/chosen/stdout-path` 或 PL011 节点，不能解决 Zephyr 问题。当前 Zephyr 由 `qemu_cortex_a53` board 构建，console、UART 地址和中断来自构建期 Devicetree 并进入生成头文件和二进制；其镜像加载地址是 `0x4000_0000`，UART 地址仍是 `0x0900_0000`。不重新构建 Zephyr，二进制仍会访问原地址。

因此分两级处理：

1. **Phase 0，安全退化**：两 Guest 都不透传 PL011/INTID 33；Linux 关闭串口启动参数，Zephyr 重新构建为无 UART console。它只证明“没有共享物理 UART”，不满足独立 console 可观测性。
2. **Phase 1，首个可用 console 方案**：为每个 VM 创建独立的、仅发送的 emulated PL011。两个 VM 可在各自 GPA 空间继续使用 `0x0900_0000`，但该 GPA 不映射任何 HPA；每个设备写入各自有界缓冲和独立证据文件。Phase 1 不提供 RX、DMA 或可用虚拟 IRQ，只用于启动/周期 marker 输出。

审计基线中 `EmulatedDeviceType::Console` 和 `EmulatedDeviceType::VirtioConsole` 只有类型枚举，非 x86 `Console` 会打印 unsupported warning 后继续，`VirtioConsole` 没有 factory。本轮已为 AArch64 `Console` 接入 VM-local PL011 并改为 fail closed，但这只是源码状态；在最新 Rust 门禁、Zephyr 重编译和运行时证据完成前，配置仍不能关闭 console gate。

本设计不改变 `blocked_dma_console` 状态，不证明双 Guest 已启动，也不证明 Linux 与 Zephyr 已建立 IP 通信。console 仅用于宿主观测，Guest 间主通信链路仍必须是 IP 网络，不能改用 console、HyperCall、共享内存、原始 MMIO 或 `vsock`。

## 2. 范围与证明边界

### 2.1 本设计覆盖

- QEMU `virt` 外层 UART 数量与字符后端；
- AxVisor 宿主 PL011 的发现、输出和输入路径；
- Linux 运行时 FDT console 路径；
- Zephyr 构建期 Devicetree console 路径；
- 每 VM emulated PL011 的最小寄存器、缓冲、生命周期和证据合同；
- 物理 MMIO、物理 IRQ、虚拟 IRQ、FDT 与 DMA 的所有权边界；
- 失败关闭、回滚与分阶段测试矩阵。

### 2.2 本设计不覆盖

- 实现 Rust 设备模型、修改现有 VM 配置或 CI；
- 双向交互终端、shell attach、输入仲裁或终端转义处理；
- AArch64 可注入虚拟 IRQ backend 的实现；
- VirtIO console 队列、DMA/Guest 内存访问安全；
- 双 Guest 网络、AI 控制闭环或性能指标。

### 2.3 审计状态

审计时 `HEAD` 与 `upstream/dev` 相同，工作树包含其他开发中的未提交修改。本文件记录当前读取到的实现和配置；后续代码合并后必须按本文测试矩阵重新审计，不能把旧日志自动提升为最新基线成果。

## 3. 当前实现与配置证据

### 3.1 外层 QEMU：当前只有一个 PL011

`os/axvisor/configs/qemu/qemu-aarch64.toml:1-17` 和 `configs/contest/qemu-aarch64-linux-zephyr-dual.toml:5-29` 都使用：

- `-machine virt,virtualization=on,gic-version=3`；
- `-nographic`；
- 没有第二个 `-serial` 或独立 `-chardev`。

QEMU 官方文档说明，`-nographic` 会把模拟串口重定向到当前终端，并默认与 monitor 复用。QEMU `virt` 最多提供一到两个 NonSecure PL011；第二个只会在显式提供第二个 `-serial` backend 且未启用 TrustZone 时出现。当前命令行没有第二个 backend。

已有只读拓扑证据 `results/baseline/runs/phase2-dual-topology-probe-20260726/` 进一步给出：

- `qemu-version.txt`：QEMU 8.2.2；
- `qtree.qmp.jsonl`：恰好一个 `dev: pl011`；
- `host.dts:307-313`：PL011 的 MMIO 为 `0x0900_0000..0x0900_1000`，中断为 `<0 1 4>`；
- `host.dts:424`：宿主 `/chosen/stdout-path = "/pl011@9000000"`。

按 GIC Devicetree binding，三单元中断描述符第一单元 `0` 表示 SPI，第二单元是 SPI 内偏移。因此 `<0 1 4>` 表示 SPI offset 1，对应全局物理 INTID `32 + 1 = 33`。

本地 QEMU 8.2.2 的辅助探测也显示 PL011 不是可用 `-device pl011` 任意追加的 pluggable device；QEMU `virt` 的第二 UART 必须通过 machine 的第二个串口 backend 创建。即使显式创建第二个 NonSecure UART，宿主加两个 Guest 仍需三个互不共享的 UART，所以“每方透传一只物理 UART”在当前 QEMU `virt` 拓扑上不可成立。

### 3.2 AxVisor 宿主正在使用该 PL011

宿主 console 初始化链是确定的：

1. `platforms/someboot/src/lib.rs:201-206` 在 MMU 启用后的 `prime_entry()` 调用 `fdt::setup_earlycon()`；
2. `platforms/someboot/src/fdt/earlycon.rs:10-27` 在架构 console 和命令行 earlycon 均未接管时回退到 `set_by_stdout()`；
3. 同文件 `29-83` 解析宿主 `/chosen/stdout-path`、`reg` 和 `compatible`，对 `arm,pl011` 创建并打开 PL011；
4. `platforms/someboot/src/console/mod.rs:306-317` 将它安装为 early output，同时把输入读取也落到该 early console；
5. `os/arceos/modules/axruntime/src/lib.rs:128-136` 的日志接口最终调用 `ax_hal::console::write_text_bytes()`。

AArch64 的 `ArchConsoleOps` 当前没有提供另一套架构专用 console，因此宿主没有可替代的隐藏输出通道。AxVisor 当前主要以轮询方式使用 early PL011；即使没有为它安装 UART IRQ handler，物理 INTID 33 也必须随物理 UART 一并保留，不能因为“宿主暂时不收中断”就分给 Guest。

现有单 Guest QEMU 日志也只是共用输出的历史证据：

- `results/baseline/runs/phase2-linux-smp2-final-20260722T100646Z-330b9ce2d52b-dirty/linux-smp2/qemu.log:241-287` 先出现 AxVisor，再在同一文件出现 Linux；
- `results/baseline/runs/phase2-zephyr-axvisor-eoimode-final-20260722T160950Z/axvisor-qemu.log:247-298` 先出现 AxVisor，再在同一文件出现 Zephyr 和 smoke marker。

这些日志证明“历史单 Guest 输出能在宿主 stdio 中被看到”，不证明输出属于独立 backend，更不证明两个 Guest 可以安全并发使用同一 UART。

### 3.3 Linux：stdout 来自运行时 Guest DTB

`os/axvisor/configs/vms/qemu/aarch64/linux-smp1.dts` 当前描述：

- `308-313`：`/pl011@9000000`，MMIO `0x0900_0000`、SPI offset 1；
- `388`：`serial0 = "/pl011@9000000"`；
- `393`：`stdout-path = "/pl011@9000000"`。

对于 Linux，最终交付的 Guest DTB 和 bootargs 可以控制 early console 与常规 PL011 driver 是否发现该设备。本轮源码会删除同 GPA 的旧物理 PL011，生成不含 IRQ/DMA/HPA 的 VM-local 节点，并把 `/chosen` 与 `serial0` 指向它；这只是 Phase 1 的 polling/earlycon 描述，不等价于常规 PL011 tty 已可用。

标准 Linux `amba-pl011` 驱动的完整 probe/startup 通常仍依赖 clock 与 IRQ。当前 synthetic 节点故意不提供物理 IRQ，也不把 VM-local sentinel 写入 DT，因此必须以锁定 kernel 实测 earlycon 的持续范围、boot marker 和 `/dev/console` 行为。若输出在 earlycon 交接时中断，应保持 gate 阻断并选择显式保留 boot console、增加真正的 VM-local IRQ backend，或回退 Phase 0；不得恢复物理 INTID 33。

### 3.4 Zephyr：UART 来自构建期 board Devicetree

`os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1.dts` 当前描述：

- `9-14`：`zephyr,console = &uart0`、`zephyr,shell-uart = &uart0`，stdout 指向 `/soc/uart@9000000`；
- `77-85`：`uart0` 为 `arm,pl011`，MMIO `0x0900_0000`，中断描述符包含 SPI offset 1，且状态为 enabled。

当前 smoke 流程又明确：

- `scripts/test/check_axvisor_zephyr_smoke.py:37-41` 固定 Zephyr revision、board `qemu_cortex_a53` 和成功 marker；
- `scripts/contest/run_axvisor_zephyr_smoke.sh:349-354` 以该 board 执行 `west build`；
- `scripts/contest/zephyr-periodic-smoke/prj.conf:1-4` 启用 `CONFIG_PRINTK`、`CONFIG_CONSOLE`、`CONFIG_UART_CONSOLE` 和 boot banner；
- `os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1.toml:27,33-35` 将镜像加载/链接在 `0x4000_0000` RAM 区域。

Zephyr 官方构建说明指出，board DTS、SoC DTSI 和 overlay 会在配置阶段合并为 `zephyr.dts`，再生成 `devicetree_generated.h` 供驱动和应用编译使用。因此运行时从 AxVisor 生成的 Guest DTB 中删除 PL011，不能改写已经编进 Zephyr 的 `uart0` 地址；如果仍运行原镜像，它仍可能访问 `0x0900_0000`。

Phase 0 必须重新构建无 UART console 的 Zephyr 镜像。Phase 1 也必须通过 board overlay/Kconfig 重新构建并归档最终 `zephyr.dts`、`.config` 和镜像哈希，证明其 UART 指向 VM-local emulated PL011，且不启用 interrupt-driven、async 或 DMA 路径。不能只检查运行时 Guest DTB。

### 3.5 双 Guest 配置已有 emulated console 源码，但仍 fail closed

`linux-smp2-dual.toml` 和 `zephyr-smp1-dual.toml` 都标记为 `blocked_dma_console`，显式排除宿主 `/pl011@9000000`，并分别配置名为 `linux`、`zephyr` 的 VM-local TX-only PL011。这避免了继续共享宿主 UART；由于最新 Rust、Zephyr 重编译和 QEMU frame/demux 尚未通过，它仍不代表已有可用 Guest console。

不能用旧模板 `virtualization/axvmconfig/templates/aarch64.toml` 中 GPA=HPA 的 PL011 passthrough 恢复输出。该模板把同一 `0x0900_0000` 和 IRQ 交给 Guest，只适用于历史单 Guest 假设，与宿主加双 Guest 所有权冲突。

### 3.6 AArch64 emulated console 已接线，交互 attach 仍未实现

历史审计发现 `Console` 只有枚举且非 x86 路径 warning 后继续。本轮已完成以下源码修正：

- `virtualization/axvm-types/src/lib.rs:681-726` 定义了 `Console = 0x2` 和 `VirtioConsole = 0xE3`；
- AArch64 已注册 TX-only PL011 factory；非法 name/GPA/length/IRQ sentinel/backend 参数失败关闭，不能再 warning 后继续；
- `VirtioConsole` 仍没有 factory 或设备实现；
- `virtualization/axvm/src/arch/x86_64/mod.rs:443-453` 的 `Console` 是 x86 16550 特例；其 host ops 在同文件 `264-269` 直接读写宿主 console，不能直接复制为多 VM AArch64 隔离方案；
- `os/axvisor/src/shell/command/vm.rs:1138-1140` 虽声明 `vm start --console`，但 `vm_start()` 的 `180-227` 只读取 `detach`，目前 `--console` 没有 attach 行为。

因此当前只能表述为“默认 VM 的 TX 证据通路完成源码接线”。任何只看到 factory、命令行 flag、静态合同或 warning 消失的检查都不能作为 console 运行完成证据。

### 3.7 FDT 与 IRQ 解析还有一个隐蔽风险

`virtualization/axvm/src/boot/fdt/core/parser.rs:421-470` 已能在 FDT MMIO 与 emulated device 重叠时跳过 passthrough 映射，这是可复用的正确基础。

同文件 `583-616` 会遍历设备节点的 `interrupts` 并加入 `pass_through_irq`。本轮选择更小的 fail-closed 合同：synthetic PL011 完全不生成 `interrupts`，配置中的 `irq_id = 0` 只作为 Console factory 的 TX-only sentinel，绝不写入 DT 或解析为 IRQ。静态回归同时证明物理 INTID 33 不进入 passthrough claim。

因此 Phase 1B 不需要伪造 VM-local IRQ 描述；若未来节点出现任何 `interrupts`，合同必须立即失败。真正的 VM-local IRQ 留到 Phase 2，并要求 parser 按 emulated ownership 区分虚拟能力与物理 claim。

此外，`virtualization/axvm/src/arch/aarch64/vm.rs:36-46` 当前用 `InterruptFabric::new()` 创建无 sink 的 fabric；`virtualization/axvm/src/irq/mod.rs:66-109` 会在设备请求 IRQ line 时返回 unsupported。Phase 1 的 PL011 因而只能是永不解析/断言 IRQ 的 TX polling 设备。RX 或 interrupt-driven TX 必须等 AArch64 虚拟 IRQ backend 单独完成，且不得把物理 INTID 33 当作替代品。

## 4. 不可违反的所有权合同

| 资源 | 所有者 | Phase 1 映射/行为 | 禁止事项 |
| --- | --- | --- | --- |
| 外层 PL011 HPA `0x0900_0000..0x0900_1000` | AxVisor 宿主 | 保持宿主 early console | 不映射到任何 Guest，不由 vCPU exit handler 直接写 |
| 外层 PL011 物理 INTID 33 | 宿主保留 | 即使宿主轮询也保留在设备租约内 | 不加入 Guest passthrough IRQ，不改其 route/enable 状态 |
| Linux emulated PL011 GPA `0x0900_0000` | VM 1 | stage-2 无 HPA mapping，由 VM 1 设备实例截获 | 不与 VM 2 共用寄存器状态或缓冲 |
| Zephyr emulated PL011 GPA `0x0900_0000` | VM 2 | stage-2 无 HPA mapping，由 VM 2 设备实例截获 | 不因 GPA 数字相同而建立共享 backing |
| console IRQ | 无 | Phase 1 配置只用 `irq_id = 0` sentinel，最终 DT 不含 `interrupts` | 不解析成物理 SPI，不调用当前无 sink 的 fabric |
| VM 1 TX ring | VM 1 设备生命周期 | 有界、非阻塞、独立序号/丢弃计数 | 不让 VM 2、宿主日志写入原始 ring |
| VM 2 TX ring | VM 2 设备生命周期 | 同上 | 不复用 VM 1 ring 或 generation |
| 宿主物理输出 | AxVisor console drain task | 可把已分帧记录转写到单一外层串口 | vCPU exit 路径不得等待 UART；原始证据文件不能靠人眼前缀分流 |
| Guest FDT/Zephyr build DT | 对应 VM 配置生成器 | 只描述 VM 所拥有的 console | 不把 synthetic node 扩展为 passthrough closure |
| DMA | 无 | PL011 Phase 1 不提供 DMA，DT 不声明 `dmas` | `DMACR` 不得触发 Guest 内存或 HPA 访问 |

同一个 GPA 可以存在于不同 VM 的 stage-2 地址空间，因为所有权键必须是 `(vm_id, gpa_range)`；物理资源冲突检查则必须使用 `(hpa_range)` 和物理 IRQ。实现不能用全局 GPA 表把两个 emulated PL011 合并。

## 5. 候选方案对比

| 方案 | 隔离性 | Guest 改动 | IRQ/DMA 风险 | 启动可观测性 | 当前成熟度 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| A. 关闭两个 Guest UART | 高 | Linux bootargs/FDT；Zephyr 重编译 | 无 | 无 Guest 串口输出 | 可立即实施 | Phase 0 安全退化，不关闭独立 console gate |
| B. 每 VM TX-only emulated PL011 | 高，前提是实例/ring 分离且无 HPA | Linux synthetic FDT/bootargs；Zephyr overlay/Kconfig 重编译 | Phase 1 无 DMA、无 IRQ delivery | 目标是独立启动与周期 marker | factory/device/default-VM drain 已源码接线，动态 gate 待完成 | **推荐的最小可用方案** |
| C. VirtIO console | 高，可支持多端口 | 两 Guest driver/Kconfig/FDT | virtqueue 会访问 Guest 内存并需虚拟 IRQ | 完整度高 | 只有枚举，无 factory | 长期方案；DMA/IRQ gate 完成前不采用 |
| D. 每方独立物理 UART passthrough | 取决于硬件 | 较小 | 物理 MMIO/IRQ/clock/pin 租约复杂 | 可提供物理独立终端 | 当前 QEMU 最多两个 NonSecure UART | 当前 host + 2 Guest 需要三个，QEMU `virt` 不满足；真机后续评估 |
| E. IP syslog/telnet console | 网络隔离后可行 | 网络栈与服务 | 继承当前 virtio DMA/IRQ 风险 | 无法覆盖网络起来前的早期启动 | 当前 IP gate 尚未关闭 | 只能作为后期辅助，不能替代早期 console，也不能把 console 当 Guest 间主通道 |
| F. 共享物理 PL011，再按行加 VM 前缀 | 低 | 看似最小 | 三方竞争同一 MMIO/IRQ | 输出来源不可证明，输入无法仲裁 | 历史单 Guest 日志容易造成误判 | 明确拒绝 |

## 6. 推荐最小实现：每 VM TX-only emulated PL011

### 6.1 为什么先做 TX-only

当前目标是给双 Guest 启动和周期 marker 提供可归属证据，而不是立即交付交互 shell。TX polling 的边界最小：

- 不需要把键盘输入分配给某个 VM；
- 不需要 AArch64 虚拟 IRQ sink；
- 不访问 Guest RAM，不引入 DMA；
- Linux early console 与 Zephyr polling UART 都可以用最小寄存器集合验证；
- 失败时可回退到 Phase 0，不影响宿主 PL011。

Phase 1 的“独立”定义是：每 VM 有独立设备状态、独立有界缓冲和可校验的 VM 身份。当前单一宿主 drain 把严格 frame 写入同一 host log，再由 demux 原子生成 per-VM 原始文件与 manifest；显示复用不是资源共享，物理串口仍只由宿主写，vCPU 不直接接触它。

### 6.2 设备实例合同

每个 emulated PL011 必须在 VM 准备阶段一次性创建，并绑定不可变的：

- 首次 drain 时绑定且不可重标的 `vm_id`，以及设备内 generation；
- GPA base `0x0900_0000`、长度 `0x1000`；
- 独立寄存器状态；
- 独立 TX ring、generation-local sequence/总字节数与 lifetime dropped-byte/DMA-attempt 计数；
- 符合严格 schema 的设备名；per-VM 文件由 demux 发布，不由设备直接持有文件 sink。

配置中不能存在对应 HPA。设备注册成功后，该 GPA 必须由 MMIO emulation dispatch 捕获，stage-2 不创建 identity mapping。设备名、GPA 或 backend allocation 任一冲突都应使 VM 准备失败。

`EmulatedDeviceConfig.irq_id` 是必填 `usize`。当前实现已在 Console 专用 factory/FDT 层把 `0` 明确限定为 TX-only sentinel，拒绝任何其他值，并且不调用 `InterruptFabric`、不生成 DT `interrupts`。证据 manifest 仍应记录 `irq_delivery = none`，避免把 sentinel 误读为真实 IRQ 0。

### 6.3 最小 PL011 寄存器模型

寄存器行为以 Arm PL011 TRM 和仓库现有 `drivers/serial/some-serial/src/pl011.rs:13-137` 为参照，由针对当前 Linux/Zephyr 镜像的 red-first trace 确定最终子集，不能凭 marker 偶然出现就宣称兼容完整 PL011。

| 寄存器组 | Phase 1 行为 | 验收重点 |
| --- | --- | --- |
| `UARTDR` `0x000` | 启用且 TX 可用时，取低 8 位作为一个输出字节入 ring | 多 vCPU 并发顺序可定义、不可跨 VM |
| `UARTFR` `0x018` | 报告 RX empty、TX not full、TX empty、not busy | Guest polling 不能永久等待 |
| `UARTIBRD/FBRD/LCR_H/CR/IFLS` | 在掩码后保存并可读回；不改变物理 UART | 满足驱动初始化，不伪造物理副作用 |
| `IMSC/RIS/MIS/ICR` | 可保存 mask；raw/masked status 恒无待处理中断；clear 为无副作用 | 设备永不请求当前不存在的 IRQ sink |
| `DMACR` | DMA enable 位强制为 0；发现 enable 请求时计数并使测试失败 | 不能读写 Guest RAM/HPA |
| PrimeCell/Peripheral ID | 按 TRM 返回稳定只读 ID | Linux AMBA/PL011 probe 能识别 |
| RX `UARTDR` 读取 | 返回空状态定义值，`RXFE` 保持置位 | 不伪装交互输入 |
| 未实现/保留 offset 与非法宽度 | 按经验证策略 RAZ/WI 或返回受控 emulation error | 不能 panic、越界或落到物理 MMIO |

TX 入 ring 必须发生在 vCPU exit 路径中的有界时间内。不得在该路径等待文件系统、物理 UART、锁的无限期竞争或用户 attach。ring 满时采用明确的 fail-observable 策略：非阻塞丢弃本次记录、递增计数，并由 drain 写入一次可解析 overflow marker；测试运行要求 dropped count 为 0。

### 6.4 Linux FDT 与启动合同

推荐为 Linux 生成 VM-local synthetic PL011 节点，而不是从宿主 FDT 复制物理节点：

- 节点 GPA 为 `0x0900_0000`，compatible 与寄存器长度符合 PL011 binding；
- 不包含 `dmas`/`dma-names`；
- `/chosen/stdout-path` 和 `serial0` 只指向该 synthetic node；
- 不包含 `interrupts`；Phase 1 没有 VM-local IRQ 线路或 delivery；
- FDT resource parser 根据 emulated ownership 跳过该节点的 passthrough MMIO，且因无 IRQ 属性不能收集 console IRQ；
- 最终 DTB 验证同时检查“节点存在”和“没有 HPA/物理 IRQ claim”。

Linux Phase 1 首先只承诺 early/polled console。Linux 官方参数文档支持 `earlycon=pl011,...` 的轮询 console；具体 bootargs、clock 缺失时的 probe 行为、boot-console 交接和 marker 必须由当前 kernel 的单 VM red-first 测试固定并归档。若常规 `amba-pl011` 驱动依赖 clock/IRQ，Phase 1 只能显式保留 boot console 用于 smoke，或回退 Phase 0；不得在没有 AArch64 IRQ backend 时假装交互 tty 已完成。

### 6.5 Zephyr 构建合同

Zephyr 不能只靠运行时 FDT 修补。Phase 1 必须用 overlay/Kconfig 重新构建：

- `zephyr,console` 和需要时的 `zephyr,shell-uart` 指向 emulated PL011；
- UART reg 仍可使用 VM-local `0x0900_0000`，因为它与 Linux 位于不同 stage-2 空间；
- 删除中断属性；Phase 1 polling profile 不描述 VM-local 或物理 IRQ；
- 显式关闭 UART interrupt-driven、async 与 DMA 功能，只保留 polling output；
- 构建后归档最终 `zephyr.dts`、`.config`、`zephyr.bin`/ELF 哈希和 board/revision。

如果该 board/driver 在禁用 IRQ 后仍会访问中断寄存器或依赖 IRQ 完成 TX，Phase 1 测试必须失败，并转为 Phase 0 无 UART 镜像；不能通过重新放开物理 PL011 passthrough 绕过失败。

仓库已有锁定的 Zephyr v4.4.0 revision、SDK 与历史 smoke 证据，但新增 polling profile 尚未针对该 revision 重编译。因此本阶段新增的是显式构建模板，而不是伪造的构建结果：

- `configs/contest/zephyr/qemu-cortex-a53-pl011-polling.overlay` 删除 UART 的
  `interrupts`/`dmas` 能力；锁定的 Zephyr 4.4 `arm,pl011` binding 不接受
  `reg-io-width`，因此构建 overlay 使用驱动默认宽度，而 AxVM 合成的运行时 Guest
  FDT 仍显式写入 `reg-io-width = <4>`；
- `configs/contest/zephyr/qemu-cortex-a53-pl011-polling.conf` 关闭 interrupt-driven、
  async、DMA 和串口 shell backend；
- `scripts/contest/validate_zephyr_pl011_profile.py` 校验模板，并可在提供
  `--final-dts` 与 `--final-config` 时静态核对真实构建产物。

下面的模板检查只证明 profile 约束成立，不代表 Zephyr 已重编译、镜像已启动，
也不代表 per-VM console sink、双 Guest 或 IP/DMA gate 已通过：

```bash
python3 scripts/contest/validate_zephyr_pl011_profile.py \
  --overlay configs/contest/zephyr/qemu-cortex-a53-pl011-polling.overlay \
  --config configs/contest/zephyr/qemu-cortex-a53-pl011-polling.conf
```

### 6.6 日志与可观测性合同

当前严格 frame 固定包含且只包含以下有序字段：

- marker 与 schema version：`AXVISOR_GUEST_CONSOLE_FRAME v=1`；
- `vm`、`name`、`gen`、`seq`；
- `len`、generation-local `total`；
- lifetime `dropped` 与 `dma`；
- lowercase `hex` payload。

run ID、QEMU PID/start-time/launch nonce、host timestamp 和源码身份属于外层 evidence manifest，不伪装成 frame 字段。当前协议没有 record-drop 计数；ring overflow/reset discard 统一计入 `dropped` bytes。

host log 中每条 frame 必须从行首开始且不带日志前缀；demux 生成的 per-VM 文件只含解码后的原始 payload。验证器必须从严格 frame、期望 VM/name 和 manifest 判断来源，不能仅用文本内容猜测“这行像 Linux”或“这行像 Zephyr”。

一次有效的 console 证据目录至少包含：

```text
run-manifest.json
source-state.txt
qemu-version.txt
qemu-argv.txt
host.log
guest-vm-1.console.log
guest-vm-2.console.log
console-manifest.json
resource-claims.json
guest-vm-1.final.dtb
guest-vm-1.final.dts
zephyr.dts
zephyr.config
checksums.sha256
```

`console-manifest.json` 必须给出两个输出流的字节数、frame 数、drop/DMA 计数、SHA-256、首末 sequence 和 generation。测试成功要求两个 drop/DMA 计数均为 0，两个 marker 各自只出现在所属文件，并且宿主日志没有 raw Guest byte 注入导致的 shell/日志破坏。

`vm start --console` 在真正连接到指定 VM ring、能处理退出和生命周期前仍应视为未实现；Phase 1 可以只生成证据文件和只读聚合输出，不得把现有 no-op flag 当成交互能力。

## 7. 分阶段实施计划

### Phase 0：先消除物理 UART 共享

1. 保持双 Guest 配置显式排除 `/pl011@9000000`；
2. 静态拒绝 HPA `0x0900_0000..0x0900_1000` 和物理 INTID 33 出现在任一 Guest claim；
3. Linux 删除/关闭指向物理 PL011 的 console bootargs 与 stdout；
4. Zephyr 重新构建无 console/UART console 镜像，并保存 `.config` 与最终 `zephyr.dts`；
5. 记录状态为 `headless_safe` 或继续 `blocked_dma_console`，不能记为 independent console complete。

### Phase 1A：设备单元与 fail-closed factory

1. [x] 为 AArch64 `Console` 注册明确的 PL011 factory；
2. [x] 非支持组合从 warning 改为 VM prepare error；
3. [x] 完成 TX-only 寄存器模型、独立 ring、overflow 和 reset 单元测试源码；
4. [x] 静态证明设备实例不持有 HPA、物理 IRQ 或 DMA capability；
5. [ ] 在当前 Rust 门禁后，以单 VM QEMU 跑通 Linux/Zephyr 的实际访问 trace。

### Phase 1B：FDT、Zephyr build 与证据 runner

1. [x] Linux synthetic FDT 节点加入 emulated ownership；
2. [x] synthetic 节点完全不生成 IRQ/DMA 属性，并新增 INTID 33 不进入 claim 的回归；
3. [x] Zephyr overlay/Kconfig polling-only 模板已加入；
4. [x] 默认 VM 单消费者 drain 与严格 frame/demux 已完成源码接线；
5. [ ] 重编译 Zephyr并完成 Linux/Zephyr 单 Guest 回归后，再进入双 Guest 短测与 soak。

### Phase 2：可选 RX 与虚拟 IRQ

只有在 AArch64 安装真正的 VM-local IRQ sink、GPPT/虚拟状态语义和生命周期测试后，才允许：

- RX ring 和输入焦点/attach；
- RX/TX interrupt-driven 模式；
- `vm start --console` 交互 attach；
- 终端断开、重连和 backpressure 策略。

虚拟 IRQ 必须由每 VM backend 注入，不能路由物理 INTID 33。若 Phase 2 需要更完整的 console 协议，应重新评估 VirtIO console，而不是继续扩大不完整 PL011 模型。

### Phase 3：独立宿主传输或真实硬件

- QEMU 可为每个 per-VM sink 提供独立 socket/file/PTY，但它只是 backend 传输，不改变 Guest 设备所有权；
- 真机只有在确认至少三个独立 UART 及 pin/clock/reset/IRQ 均可分配时，才评估物理 passthrough；
- VirtIO console 需要先关闭 Guest 内存访问、virtqueue 和 IRQ gate。

## 8. 失败关闭与回滚

### 8.1 必须使 VM 准备失败的条件

- AArch64 配置了 `Console`，但 factory 未注册或返回空设备；
- 同一 VM 内 GPA 重叠、设备长度不为预期，或 backend/ring 分配失败；
- emulated PL011 同时存在 HPA passthrough mapping；
- synthetic FDT 的 stdout 不能解析到该 VM 设备；
- emulated node 的 IRQ 出现在物理 passthrough claim；
- 任一 Guest 获得物理 INTID 33；
- Zephyr 产物缺少最终 `zephyr.dts`/`.config`，或仍启用未获批准的 UART IRQ/DMA；
- VM ID/generation 与 sink manifest 不一致。

### 8.2 运行时失败条件

- 任一字节出现在错误 VM 的原始日志；
- 任一 ring 的 dropped count 非 0；
- vCPU exit 因 console backend 阻塞或超时；
- Guest UART 访问落到宿主 HPA；
- 物理 INTID 33 的 route/enable/pending 状态被 Guest 改变；
- 宿主 shell 输入或日志被 Guest raw byte 污染；
- reset/destroy 后旧 generation 仍能写入新 sink；
- 非法 MMIO 访问触发宿主 panic、越界或未捕获异常。

### 8.3 回滚步骤

1. 停止启用 emulated console 的双 Guest runner，保留失败证据目录；
2. 从 Guest 配置移除 emulated console 和 synthetic FDT 入口；
3. Linux 回到无 serial console，Zephyr 回到 Phase 0 的无 UART 构建；
4. 确认双 Guest 配置仍排除 PL011/INTID 33，并恢复 `blocked_dma_console`；
5. 单独修复设备模型后从 Phase 1A 重跑。

回滚不得通过恢复 `passthrough_devices = [["/"]]`、重新加入 `/pl011@9000000` 或共享宿主 UART 完成。宿主 PL011 配置在整个回滚过程中不应变化。

## 9. 分阶段测试矩阵

| 阶段 | 测试 | 通过条件 | 必留证据 | 允许的结论 |
| --- | --- | --- | --- | --- |
| Phase 0 静态 | 扫描两 VM 配置、最终 Linux DTB、Zephyr `.config`/`zephyr.dts` | 无 PL011 HPA、无物理 INTID 33 claim；Zephyr UART console 关闭 | 配置副本、DT、哈希、静态报告 | 物理 UART 未共享 |
| Phase 1A 单元 | DR/FR/配置/ID/IRQ/DMA/非法访问 | polling 可前进；IRQ 恒不请求；DMA 恒关闭；错误受控 | 单测日志、coverage/测试列表 | 寄存器子集满足单测 |
| Phase 1A 隔离 | 两设备同 GPA、并发写、reset/destroy/recreate | 状态/ring/generation 完全独立，无 stale write | 每实例计数和哈希 | 设备实例隔离 |
| Phase 1A backpressure | ring 满、drain 停止、后端关闭 | vCPU 不阻塞；drop 精确计数；运行判失败 | fault log、overflow marker | 失败可观测，不是成功 |
| Phase 1B FDT | 生成/反编译 Linux 最终 DTB | stdout 解析到 synthetic node；无 HPA/IRQ/DMA；INTID 33 不进 physical claim | DTB/DTS、resource claims | Linux 描述闭包正确 |
| Phase 1B Zephyr build | pristine build + overlay/Kconfig 审计 | `zephyr.dts` 指向 emulated GPA且无 IRQ/DMA；polling-only；镜像哈希固定 | build log、`.config`、`zephyr.dts`、ELF/bin | Zephyr 二进制配置正确 |
| 单 Linux QEMU | 当前目标 kernel 输出唯一 Linux marker | marker 只在 VM 1 sink；drop=0；宿主正常 | host/VM1 log、manifest | Linux 单 VM console 可用 |
| 单 Zephyr QEMU | 当前固定 revision 输出 smoke marker | marker 只在 VM 2 sink；drop=0；无物理 UART/GIC 访问 | host/VM2 log、manifest | Zephyr 单 VM console 可用 |
| 双 Guest 短测 | 同时启动并持续输出不同 nonce marker | 两个 vCPU 都有独立进度；marker 无交叉；drop=0；INTID33 不变 | 两个 raw log、claims、FDT、checksum | 双 Guest console 短测通过 |
| 双 Guest soak | 至少 30 分钟周期 marker + reset/recreate 场景 | sequence 连续或有解释；无 cross-byte、drop、panic、stale generation | soak manifest、资源快照、失败注入记录 | console 隔离稳定性达到本阶段门槛 |
| Phase 2 RX/IRQ | 输入焦点、虚拟 IRQ、attach/断开 | 仅目标 VM 收到输入；无物理 IRQ；生命周期安全 | IRQ trace、attach transcript | 交互 console 可用 |
| 后续 IP | ping/UDP/TCP 或项目规定 IP 协议 | 两 Guest 使用独立网卡资源完成 IP 交换 | 抓包、地址表、应用日志、时延数据 | 才能声明 Guest 间 IP 通信 |

双 Guest console 短测至少需要两个 Guest 各自输出包含 run nonce、VM ID 和单调计数的 marker。仅看到 AxVisor 打印“VM started”，或只看到两个 VM 配置被解析，不等于两个 Guest vCPU 已执行。

## 10. 完成定义与禁止声明

### 10.1 Phase 1 console gate 完成条件

必须同时满足：

1. AArch64 `Console` 要么构造真实设备，要么 fail closed，不再 warning-and-continue；
2. 两 VM 的 `0x0900_0000` 都没有 HPA backing；
3. 物理 INTID 33 只属于宿主，resource claim 和 GIC trace 均无 Guest 修改；
4. Linux 最终 DTB 与 Zephyr 构建产物分别证明 console 指向 emulated device；
5. 两个设备实例、ring、日志、generation 和哈希独立；
6. 当前 Linux 与固定 Zephyr revision 的单 VM 测试通过；
7. 双 Guest 短测和 soak 的 marker 无交叉，drop 为 0；
8. 失败注入不会阻塞 vCPU、破坏宿主 console 或泄漏旧实例；
9. 证据目录包含源码状态、QEMU 版本/argv、最终 DT、资源 claims 和 checksums。

### 10.2 即使 Phase 1 通过也不能声明

- 已提供双向交互终端；
- `vm start --console` 已可用；
- VirtIO console 已实现；
- 双 Guest 网络/DMA 已安全；
- Linux 与 Zephyr 已通过 IP 通信；
- AI 到 RTOS 的网络闭环已完成；
- 真机独立 UART 已验证。

console gate 与 IP gate 是两个独立门槛。console 证据只能证明输出归属和资源隔离；Guest 间通信必须另行以 IP 地址、网卡所有权、报文/连接证据和可复现命令证明。

## 11. 建议的实现工作包

为降低审查和回滚成本，后续实现应拆成互不混杂的提交：

1. **PL011 device tests**：先加入寄存器、隔离、overflow、reset 的失败测试；
2. **AArch64 factory**：注册 Console，unsupported 改为 fail closed，不接 IRQ；
3. **per-VM backend**：有界 ring、generation、manifest 和非阻塞 drain；
4. **FDT ownership**：synthetic Linux node，emulated IRQ 不进入 passthrough claim；
5. **Zephyr build profile**：overlay/Kconfig 与构建产物校验；
6. **evidence runner**：两个 raw sink、resource claims、hash 与 marker validator；
7. **dual QEMU tests**：短测、soak、fault injection；
8. **IRQ/RX follow-up**：作为独立高风险变更，不与 TX-only 合并。

每个工作包都应保持 Phase 0 可回滚。不要在 console 实现提交中顺带放开 virtio DMA、网络 slot 或根节点 passthrough。

## 12. 权威参考与仓库证据索引

外部规范：

- [QEMU `virt` machine documentation](https://www.qemu.org/docs/master/system/arm/virt)：一到两个 NonSecure PL011，第二个需要第二个 `-serial` backend；
- [QEMU user manual, `-nographic`](https://www.qemu.org/docs/master/system/qemu-manpage.html#hxtool-6)：串口重定向并与 monitor 复用；
- [Arm PL011 Technical Reference Manual, DDI0183](https://developer.arm.com/documentation/ddi0183/latest/)：寄存器和设备语义；
- [Linux PL011 Devicetree binding](https://www.kernel.org/doc/Documentation/devicetree/bindings/serial/pl011.yaml)：`reg`、`interrupts`、clock 与可选 DMA 属性；
- [Linux GICv3 Devicetree binding](https://www.kernel.org/doc/Documentation/devicetree/bindings/interrupt-controller/arm,gic-v3.yaml)：SPI/PPI 类型和中断编号编码；
- [Linux kernel parameters, PL011 early console](https://docs.kernel.org/admin-guide/kernel-parameters.html)：PL011 early polling console；
- [Zephyr Devicetree input/output](https://docs.zephyrproject.org/latest/build/dts/intro-input-output.html)：构建期 `zephyr.dts` 与生成头文件；
- [VirtIO 1.3, Console Device](https://docs.oasis-open.org/virtio/virtio/v1.3/virtio-v1.3.html#x1-6640003)：Device ID 3、输入/输出及控制 virtqueues。

关键仓库路径：

- `drivers/data/qemu.dts:307-313,383-385`；
- `platforms/someboot/src/fdt/earlycon.rs:10-83`；
- `platforms/someboot/src/console/mod.rs:65-83,306-317`；
- `os/axvisor/configs/vms/qemu/aarch64/linux-smp1.dts:308-313,388-393`；
- `os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1.dts:9-14,77-85`；
- `virtualization/axvm-types/src/lib.rs:583-598,681-726`；
- `virtualization/axdevice/src/device.rs:348-359`；
- `virtualization/axvm/src/boot/fdt/core/parser.rs:421-470,583-616`；
- `virtualization/axvm/src/arch/aarch64/vm.rs:36-46`；
- `virtualization/axvm/src/irq/mod.rs:66-109`；
- `docs/docs/development/aarch64-dual-guest-fdt-filtering.md`；
- `results/baseline/runs/phase2-dual-topology-probe-20260726/`。

最终提醒：本文件是实施设计和验收合同。文件存在、枚举存在、静态拓扑通过或历史单 Guest 日志存在，都不能替代 Phase 1 的实现与双 Guest 运行证据，更不能替代后续 IP 通信证据。
