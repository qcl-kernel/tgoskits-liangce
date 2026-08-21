# AArch64 双 Guest FDT 过滤与证据设计

状态：第一层部分实现；完整设计尚未实现<br>
基线：`rcore-os/tgoskits` `dev`，`01e053105e243d1fdd1c03f6f850b266efbe8de0`<br>
风险级别：高风险（Guest 硬件描述、物理设备所有权与启动证据）

## 1. 结论

Linux 与 Zephyr 双 Guest 不能继续沿用 `passthrough_devices = [["/"]]`。新配置必须使用显式设备路径白名单，并把最终交给每个 Guest 的 DTB 当作资源所有权结果，而不是普通启动附件。

实现必须同时满足以下四项条件：

1. `/cpus/cpu-map` 中每个 `cpu` phandle 都指向该 VM 实际保留的 CPU 节点，不允许引用已过滤 CPU；
2. `/chosen/stdout-path` 只在目标 console 属于该 VM 的设备闭包且仍存在于最终 DTB 时保留，否则删除；
3. 每个保留设备的有效 `interrupt-parent` 都必须能在同一 Guest DTB 内解析；双 Guest 配置显式列出 GIC，不依赖隐式继承碰巧生效；
4. QEMU 运行证据必须包含按 VM ID 区分的最终运行时 DTB dump、反编译结果和 SHA-256，不能只保存 QEMU 的宿主 `dumpdtb`。

完成本设计和静态拓扑检查不等于双 Guest 已经启动，也不等于 IP 通信、DMA 或独立控制台已经可用。

## 2. 当前问题

### 2.1 `/cpus/cpu-map` 会留下悬空 phandle

从宿主 FDT 生成 Guest DTB 时，`create_guest_fdt()` 的过滤规则无条件保留 `/cpus/cpu-map` 及其全部后代，却只保留 `phys_cpu_ids` 选中的 `/cpus/cpu@*` 节点。以四核 QEMU 为例，Linux 只选择 pCPU 0、1 时，映射到 pCPU 2、3 的 `core/thread` 项仍可能存在，其 `cpu` 属性将引用已经删除的 CPU phandle。

开发者提供 DTB 的分支也有同类问题。AArch64 `update_cpu_node()` 会从宿主复制完整 `/cpus` 子树，再删除未选中的 CPU 节点，但没有同步修剪复制来的 `cpu-map`。

该 DTB 可能仍能被部分 Guest 内核容忍，但不能作为 CPU 隔离已经正确生效的证据。

### 2.2 `/chosen/stdout-path` 可能指向未拥有的 console

`/chosen` 被选中时，其属性会原样复制。QEMU virt 的 `stdout-path` 通常指向 `/pl011@9000000`；若双 Guest 白名单不包含 PL011，过滤结果会删除 UART 节点，却保留指向它的字符串引用。

更危险的替代方案是为了让引用成立而把同一个 PL011 加回两个 Guest。PL011 的 MMIO `0x0900_0000` 和 SPI offset 1（GIC INTID 33）是单一物理资源，且 AxVisor 本身仍使用该宿主控制台，不能由 Linux、Zephyr 和宿主共同所有。

因此这里必须净化 `/chosen`，不能通过扩大透传范围掩盖悬空引用。

### 2.3 设备闭包不能替代显式所有权

`find_all_passthrough_devices()` 会从配置路径扩展后代节点和节点自身属性中的 phandle 依赖，再应用 `excluded_devices`。当前逻辑最后才移除字符串 `"/"`；以根节点为起点时，其全部后代已经进入闭包，因此删除根字符串并不能撤销全设备透传。

直接写在设备节点上的 `interrupt-parent` 可以进入 phandle 依赖分析，但继承自根节点或上层总线的 `interrupt-parent` 不在该设备自身属性中。祖先节点虽然会因结构需要被复制，却不会自动作为依赖分析起点。由此不能假设 virtio 节点总会自动带入 GIC。

双 Guest 配置应显式列出 `/intc@8000000`，并显式排除 `/intc@8000000/its@8080000`。最终 DTB 还必须验证每个中断设备的有效 `interrupt-parent`；缺失或指向已删除节点时，VM 准备应失败，不能只打印警告后继续启动。

### 2.4 当前没有最终 Guest DTB 证据

现有 `probe_qemu_aarch64_virtio_slots.sh` 的 `-machine ...,dumpdtb=...` 只证明外层 QEMU virt 机器的静态槽位。Guest DTB 此后还经历：

- 按 VM 的 CPU 与设备过滤；
- passthrough 地址和 SPI 提取；
- VM 内存实际分配；
- `/memory`、`/chosen` 与 initrd 属性的运行时改写；
- DTB 加载地址选择。

因此宿主 DTB、准备阶段 Guest DTB 和最终加载到 Guest 内存的 DTB 是三个不同对象。只有最后一个对象能证明 Guest 实际看到的 CPU、内存和设备集合。

## 3. 安全不变量

实现后的准备与证据链必须满足以下不变量：

- `phys_cpu_ids` 对应的 CPU 节点集合与最终 DTB 中 `/cpus/cpu@*` 集合一致；
- `/cpus/cpu-map` 不存在，或其中每个 `cpu` 属性都唯一解析到保留 CPU 节点；
- `/chosen/stdout-path` 和兼容属性 `linux,stdout-path` 不存在，或解析到设备所有权闭包中的现存 console 节点；
- `passthrough_devices` 不使用 `"/"`，保留的带 `reg`/`ranges` 设备来自显式白名单、其后代或经审核的 phandle 依赖；
- 每个具有 `interrupts` 或 `interrupts-extended` 的保留设备都能解析有效中断控制器；
- Linux 与 Zephyr 的设备节点、MMIO 区间、SPI 和 CPU 集合互不交叉；
- 最终 DTB 证据带 VM ID、最终 Guest GPA、外层物理内存段、字节长度和 SHA-256；缺失、重复或无法验证的证据不得标为通过。

## 4. 当前调用路径

AxVisor 的调用顺序如下：

```text
os/axvisor/src/config.rs::init_guest_vm
  -> axvm::boot::prepare_guest_boot
  -> arch::prepare_guest_boot
  -> Aarch64Arch::prepare_guest_boot
  -> boot/fdt/core::prepare_dtb_guest
       -> build_guest_dtb
          -> host DTB + no provided DTB
             -> set_phys_cpu_sets
             -> setup_guest_fdt_from_vmm
                -> find_all_passthrough_devices
                -> create_guest_fdt
          -> provided DTB
             -> set_phys_cpu_sets（有 host DTB 时）
             -> update_provided_fdt
                -> AArch64 update_cpu_node
       -> enrich_guest_config
          -> parse_reserved_memory_regions
          -> parse_passthrough_devices_address
          -> parse_vm_interrupt
  -> AxVM::new / prepare_memory_layout
  -> PreparedGuestBoot::load_images
  -> ImageLoaderCore::load
  -> Aarch64Arch::load_guest_dtb
  -> update_fdt
     -> patch_guest_fdt_for_runtime
        -> rebuild_memory_nodes
        -> patch_chosen
     -> load_patched_fdt
        -> load_vm_image_from_memory
        -> AxVM::set_guest_device_tree
```

设计必须覆盖两个净化时点：

1. `build_guest_dtb` 返回后、`enrich_guest_config` 解析前：修剪 CPU 拓扑、清理 console 引用并验证设备/中断闭包，确保据此生成的 MMIO 和 SPI 配置可信；
2. `patch_guest_fdt_for_runtime` 完成后、`load_vm_image_from_memory` 前：对最终字节再次运行结构验证，并生成证据描述，确保运行时改写没有破坏准备阶段不变量。

## 5. 设计

当前实现边界：共享净化 helper 已接入 generated FDT 与 AArch64 provided-DTB CPU replacement，能够修剪悬空、重复或畸形 CPU phandle 对应的 CPU-map，并在 `stdout-path`/`linux,stdout-path` 的目标节点最终不存在时删除属性且保留 bootargs。两条生产路径也已接入中断引用 fail-closed 校验：直接或继承的 `interrupt-parent`、`interrupts` 以及 `interrupts-extended` 必须唯一解析到仍存在的 interrupt-controller，且 `#interrupt-cells` 与 specifier 长度必须有效。feature-gated 的 Rust 侧最终 DTB 描述符、stage-2 HPA 分段解析和 ready marker 已接入，默认关闭。当前尚未接入设备所有权闭包；“目标节点存在”不代表该 console 已通过显式所有权审核。QMP capture runner、DTB/DTS 文件、hash manifest 和 QEMU 回归仍未实现。

### 5.1 统一的准备阶段净化

新增内部的 Guest FDT finalization 步骤，概念输入为：

- VM ID；
- `phys_cpu_ids`；
- 原始显式设备路径；
- `find_all_passthrough_devices()` 计算出的设备所有权闭包；
- `excluded_devices`；
- 待处理 DTB 字节。

该步骤不公开新的 Guest ABI，也不改变 DTB 文件格式。它按固定顺序执行：

1. 修剪 `/cpus/cpu-map`；
2. 清理 `/chosen` console 属性；
3. 验证设备路径、phandle 和有效 `interrupt-parent`；
4. 编码并重新解析一次结果，拒绝不可解析或违反不变量的 DTB。

从宿主生成的路径必须传入显式白名单和完整依赖闭包。开发者提供 DTB 当前被视为完整虚拟平台描述，并未按 `passthrough_devices` 过滤；本次不能暗中改变所有旧配置的该语义。对于新的双 Guest runner，应先禁止 `dtb_path`/嵌入 DTB，强制使用宿主 FDT 加显式白名单。提供 DTB 的路径本次仍必须执行 CPU-map 与悬空 console 净化；将其改造成同一显式所有权模型属于后续独立迁移。

### 5.2 CPU map 修剪

先从最终保留的 `/cpus/cpu@*` 节点收集 `phandle` 与 `linux,phandle`。随后遍历 `/cpus/cpu-map`：

- 带 `cpu` 属性的映射叶节点只在 phandle 唯一指向保留 CPU 时保留；
- 引用不存在、已删除或重复 CPU 的映射叶节点删除；
- 从最深层向上删除失去全部映射后代的空 `thread`、`core`、`cluster` 容器；
- 未识别或结构损坏的 `cpu-map` 不原样保留，而是整体删除并记录确定性告警；CPU 节点本身仍保留；
- 修剪后重新扫描全部 `cpu` 属性，任何悬空引用都返回 `InvalidData`。

`cpu-map` 在 Devicetree 中是可选拓扑描述。无法安全修复时删除整棵 map，比把错误拓扑交给 Guest 更可兼容、更容易回滚。

该逻辑应由生成 DTB 的 `create_guest_fdt()` 与 AArch64 `update_cpu_node()` 共用，避免只修复其中一条路径。

### 5.3 `/chosen` console 净化

对 `stdout-path` 与 `linux,stdout-path` 使用相同规则：

1. 取冒号前的节点路径或 alias 名；保留冒号后的串口参数只用于原值恢复，不参与查找；
2. alias 形式通过 `/aliases` 解析为绝对节点路径；
3. 目标必须存在于净化后的 DTB，且属于设备所有权闭包，而非仅作为结构祖先被复制；
4. 目标还必须未命中 `excluded_devices`；
5. 任一条件不满足时，只删除对应 stdout 属性，保留 `/chosen/bootargs`、initrd 字段和其他属性。

本设计不会自动选择替代 console，也不会把 PL011 加回白名单。Linux 在没有 Guest console 时可能缺少可见输出，这是当前独立 console 尚未实现的真实限制，不能由 FDT 过滤层伪装解决。

### 5.4 显式设备白名单与中断控制器

双 Guest 配置的最小路径合同为：

| VM | 必选结构/设备路径 | 当前禁止路径 |
| --- | --- | --- |
| Linux VM 1 | `/chosen`、`/psci`、`/timer`、`/intc@8000000`、`/virtio_mmio@a000000`、`/virtio_mmio@a000200` | `/`、`/pl011@9000000`、`/intc@8000000/its@8080000`、Zephyr slot |
| Zephyr VM 2 | `/psci`、`/timer`、`/intc@8000000`；slot 2 仅在 DMA 安全方案完成后加入 | `/`、`/pl011@9000000`、`/intc@8000000/its@8080000`、Linux block/net slot |

节点过滤完成后，验证器应从每个中断设备向上解析有效 `interrupt-parent`：先读节点自身属性，再按 Devicetree 继承规则检查祖先。解析结果必须指向 Guest DTB 中的 interrupt-controller 节点；不允许为了修复缺失依赖而在验证阶段自动扩展白名单。

当前最小实现还会验证 `interrupts` 的总字节数能被有效父控制器的 `#interrupt-cells` 整除，并逐项解析非空 `interrupts-extended` 中的 phandle 与控制器专属 specifier。被引用的 phandle 必须非保留值、全局唯一；同一节点的 `phandle` 与 `linux,phandle` 不一致也视为歧义。目标节点必须带 `interrupt-controller`，且其 `#interrupt-cells` 必须是单个 u32。任一条件不满足时，generated FDT 与 provided-DTB CPU replacement 都在编码前失败。此阶段不解释 `interrupt-map`/interrupt nexus，不校验 MSI、IOMMU、clock 等其他 phandle 合同；这些能力不得从当前实现状态中推断。

对 QEMU virt，配置作者必须显式写入 `/intc@8000000`。这种重复是有意的：中断控制器所有权应该在 TOML 和评审 diff 中可见，而不是依赖宿主 FDT 的继承布局。

现有 path-only 设备条目应保持 `length = 0`，由过滤后的 DTB 解析实际地址。当前显式五元组分支与 path-only 分支行为不同，本设计不修改该解析语义；新的双 Guest 静态合同应拒绝混用两种形式。

### 5.5 最终运行时 DTB dump 与 hash

最终证据采集必须位于 `patch_guest_fdt_for_runtime()` 之后。建议增加仅用于 QEMU 验证构建的 `guest-fdt-evidence` feature，默认关闭，避免生产构建持续输出证据元数据。

`load_patched_fdt()` 成功写入并登记 DTB 后，证据模式输出一条机器可解析、包含 VM ID 的 marker：

```text
AXVISOR_GUEST_DTB_READY vm=<id> gpa=<hex> size=<decimal> hpa_segments=<hpa:length,...>
```

当前 Rust 侧实现由 `guest-fdt-evidence` feature 控制，AxVM 与 AxVisor 的默认 feature 均不启用它。启用后，只有在最终 DTB 字节写入 Guest address space 成功并且 `set_guest_device_tree()` 成功之后，才从同一 stage-2 page table 按 4 KiB 边界查询实际 HPA、合并连续段并输出不带日志级别前缀的严格 marker。任一页查询失败、地址溢出或分段长度不能完整覆盖 DTB 时返回错误且不输出 marker；实现不使用 GPA=HPA 假设。

该 marker 只提供安全的 capture 描述符，不代表文件已抓取或语义验证通过。当前实现不负责 QMP/HMP、`pmemsave`、文件写入、`dtc`、SHA-256 或成功状态判定。

这里必须给出覆盖完整 DTB 的外层物理段，而不能假设所有 Guest 都满足 GPA=HPA。runner 收到每个预期 VM 恰好一条 marker 后，通过 QMP/HMP 暂停 QEMU，并对每个物理段执行 `pmemsave`，按顺序拼接为精确长度的 DTB。随后：

- 校验 FDT magic 和 header `totalsize`；
- 用 `dtc -I dtb -O dts` 生成可审阅文本；
- 在宿主脚本中计算 SHA-256，不把哈希实现引入 no_std 热路径；
- 生成 `guest-vm-<id>.dtb`、`guest-vm-<id>.dts`、`guest-vm-<id>.sha256` 和包含 GPA/HPA 段的 manifest；
- 对全部文件生成并回验总 `checksums.sha256`。

marker 必须在最终 DTB 字节成功写入且 `AxVM::set_guest_device_tree()` 成功后发出。marker 缺失、VM ID 重复、物理段长度总和不匹配、QMP dump 失败或 SHA-256 回验失败都必须使 evidence 阶段失败。

该证据只证明“最终 Guest DTB 内容与资源合同一致”。没有 Guest 启动标记、双独立终端、ping/UDP 和稳定性窗口时，不得把它升级为双 Guest 运行通过。

## 6. 测试先行

实现顺序遵循先红后绿；测试失败应先证明当前缺口，再进入代码修改。

### 6.1 纯 FDT 单元测试

在 `virtualization/axvm/src/boot/fdt/core/` 的现有测试模块中先加入最小构造树测试：

- `generated_fdt_prunes_unselected_cpu_map_entries`：选择 CPU 0、1 后不存在指向 CPU 2、3 的 map 叶节点；
- `provided_fdt_cpu_replacement_prunes_cpu_map`：AArch64 提供 DTB 分支得到同一结果；
- `malformed_cpu_map_is_removed_not_preserved`：未知/悬空结构整体删除；
- `chosen_drops_stdout_path_when_console_is_absent`：保留 bootargs，但删除绝对路径 stdout；
- `chosen_drops_unowned_console_alias`：alias 能解析但目标不属于所有权闭包时删除；
- `chosen_preserves_owned_console_and_suffix`：目标明确拥有时保留完整波特率后缀；
- `inherited_interrupt_parent_requires_present_controller`：根节点继承的 GIC 被删除时返回错误；
- `explicit_interrupt_controller_satisfies_reference`：显式保留 GIC 后通过；
- `excluded_interrupt_controller_fails_closed`：白名单与排除项冲突时拒绝准备；
- `runtime_patch_revalidates_final_fdt`：运行时重建 memory/chosen 后仍满足 CPU、console 和中断不变量。
- `hpa_resolver_coalesces_only_contiguous_pages`：只合并实际 HPA 连续的 DTB 分段；
- `descriptor_formats_strict_ready_marker`：marker 字段名、进制和顺序严格固定；
- `descriptor_rejects_incomplete_hpa_coverage`：分段不能完整覆盖 DTB 时拒绝描述符；
- `hpa_resolver_rejects_unmapped_page`：任一 stage-2 查询缺页时不得生成 marker。

测试必须直接重新解析编码后的 DTB 并检查 phandle 解析结果，不能只断言内部路径集合。

### 6.2 静态合同测试

扩展 `scripts/test/check_axvisor_dual_guest_topology.py`，或增加职责单一的 `check_axvisor_dual_guest_fdt_contract.py`，对待提交双 Guest 配置检查：

- VM ID 分别为 1、2，pCPU 集合分别为 `{0,1}`、`{2}` 且不重叠；
- 不存在 `passthrough_devices = [["/"]]`；
- 全部条目为 path-only，未混入显式地址五元组；
- 两 VM 都显式包含 GIC 并排除 ITS；
- 两 VM 都不拥有 PL011/INTID 33；
- Linux 只拥有 virtio slot 0、1，Zephyr slot 2 在 DMA gate 关闭时不能出现在可运行配置；
- QEMU slot、MMIO、SPI offset/INTID 和 MAC 与拓扑探针输出一致；
- 新双 Guest 配置不使用 `dtb_path` 或嵌入开发者 DTB。

静态合同只允许输出 `topology_validated` 或明确的阻塞状态，不能输出 `passed`。

### 6.3 QEMU evidence 测试

QEMU 阶段按以下顺序执行：

1. 运行现有 virtio 槽位探针并回验宿主 `dumpdtb`、QMP `qtree` 和 checksums；
2. 使用 evidence feature 启动 AxVisor，等待 VM 1、VM 2 的最终 DTB marker；
3. 暂停 QEMU，按 marker 的外层 HPA 段分别 dump 两份 DTB并计算 SHA-256；
4. 对反编译结果执行语义校验：CPU、cpu-map、memory、chosen、GIC、ITS、PL011 和 virtio 节点都必须符合各 VM 合同；
5. 验证 VM 1 的 DTB 不含 VM 2 slot，VM 2 的 DTB不含 VM 1 block/net slot；
6. 保存命令、QEMU/AxVisor 版本、源提交与 dirty-state、配置哈希、原始日志、两份 DTB/DTS 和总 checksum manifest。

如果当前 DMA 或 console gate 阻止 VM 2 使用网卡，证据应如实记录 `blocked_dma_console`，而不是通过放宽 FDT 校验取得绿色结果。

## 7. 兼容性与回滚

### 7.1 兼容策略

- 不新增 Guest ABI，不改变既有 TOML 字段编码；
- CPU-map 修剪仅删除不能解析到现存 CPU 的可选拓扑项；
- `/chosen` 只删除无效或未拥有的 stdout 属性，保留 bootargs 和 initrd；
- 缺失 `interrupt-parent` 从可能带病启动改为准备阶段 `InvalidData`，属于有意的 fail-closed 行为；旧显式配置可通过补充中断控制器路径修复；
- 本次不全局禁止历史单 Guest 的根透传，但新的双 Guest 静态合同必须拒绝它；全局废止根透传需要另立迁移计划；
- developer-provided DTB 的完整平台语义暂不重写，双 Guest runner 在迁移完成前禁止使用该路径；
- evidence 功能默认关闭，仅 QEMU 验证 board/runner 开启。

### 7.2 回滚边界

实现应拆成可独立回滚的三层：

1. 纯 FDT 净化与验证；
2. 双 Guest 静态合同；
3. feature-gated DTB evidence 与 QMP dump。

若 evidence 采集影响 QEMU 启动，可单独关闭 feature 和 runner 捕获，不需要撤销 FDT 正确性修复。若某平台的 CPU map 结构不兼容，可临时回滚为“删除整个 cpu-map”，不能回滚为保留悬空 phandle。若新的中断引用校验暴露旧配置，应修复白名单或按平台增加有界兼容规则，不允许恢复根节点透传作为快捷回滚。

## 8. 非目标

本设计不负责：

- 解决 Zephyr `MapAlloc` 下 passthrough virtio-net 的非 identity DMA；
- 实现 bounce/emulated virtio-net、IOMMU 或固定保留内存；
- 实现 AArch64 emulated PL011、virtio-console 或多 Guest 独立终端；
- 修改 Zephyr 编译期 devicetree overlay、Kconfig 或网络驱动；
- 证明 Linux/Zephyr 双启动、IP ping/UDP、AI 控制闭环或 30 分钟稳定性；
- 对任意 Devicetree 属性执行全局 phandle 垃圾回收；本次只处理 CPU map、console 和中断引用合同；
- 改变 passthrough 五元组地址解析、GIC SPI 路由回滚或跨 VM resource claim；
- 把 `quick-start.sh --multi` 的 ArceOS+Linux 结果当作本项目 Linux+Zephyr 证据。

## 9. 完成门槛

只有在纯 FDT 单测、静态合同和 QEMU 最终 DTB evidence 三层全部通过后，才能把本项状态改为“已实现，待双 Guest 运行回归”。当前完成了第一层中的 CPU-map、缺失 console 目标净化和中断引用 fail-closed 校验，并实现了默认关闭的 Rust 侧 HPA 描述符与 marker；console/设备所有权闭包、运行时二次语义验证、静态合同、实际 capture/hash evidence 与 QEMU 回归均未完成，因此本文件状态保持：**第一层部分实现；完整设计尚未实现**。
