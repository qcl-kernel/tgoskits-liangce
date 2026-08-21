# AxVM 跨虚拟机物理资源 Claim 设计

状态：已实现，AArch64 target 与单 Guest QEMU 已验证；双 Guest/实机隔离回归待完成<br>
基线：`rcore-os/tgoskits` `dev`，`01e053105e243d1fdd1c03f6f850b266efbe8de0`<br>
风险级别：高风险（虚拟化、IRQ、物理地址与资源所有权）

## 1. 问题与必要性

实现前，AxVM 分别在不同层验证资源：

- `GuestRegionPlanner` 验证一个 VM 内部的 GPA 布局和 stage-2 mapping；
- `AxVmDevices` 验证一个 VM 内部 emulated device 的 MMIO/PIO/IRQ 资源；
- `VM_REGISTRY` 在 VM 完成内存准备、镜像加载和设备/vCPU 初始化后，才检查 VM ID 是否重复。

这些旧检查都不能阻止两个 VM 同时拥有同一个宿主物理资源。两个配置可能在到达 `register_vm()` 前已经：

- 将同一 passthrough MMIO/HPA 映射到不同 VM；
- 将同一 `MapReserved` identity memory 当作各自 RAM；
- 将同一物理 SPI 路由给不同 VM；
- 将 passthrough-interrupt vCPU 固定到重叠的 pCPU；
- 使用相同 VM ID 完成有副作用的准备流程。

这会让启动结果依赖初始化顺序，并可能造成跨 VM 内存写入、物理中断误投递或实时任务相互干扰。当前实现已在 `AxVM::new()` 的架构资源创建前加入进程级 claim，并在内存和 VM prepare 前复核冻结快照；运行时 AArch64/QEMU/实机隔离证据仍待补充。

## 2. 调用方、场景与成功标准

直接调用方是 `AxVM::new(AxVMConfig)`，间接调用方是 AxVisor 的静态配置、文件系统配置和 shell 创建流程。

典型场景：

1. VM 1 声明 HPA `[0x0a00_0000, 0x0a01_0000)` 和 SPI 33；
2. VM 2 声明与其重叠的 HPA 或同一 SPI；
3. VM 2 必须在写入 Guest 镜像、建立最终设备路由或注册运行时 VM 之前失败；
4. 错误必须指出冲突资源类型、当前 VM、已有 VM 和冲突值；
5. VM 1 销毁且最后一个 `Arc<AxVM>` 释放后，相同资源可以被新 VM 重新申请。

已实现完成标准：

- 重复 VM ID、重叠的固定 HPA、重叠 host I/O port、重复 passthrough IRQ，以及 passthrough 独占 pCPU 与任意其他 VM 的 pCPU usage 重叠均被拒绝；
- 相邻但不重叠的地址区间可同时申请；
- emulated-interrupt VM 仍可按原语义共享调度 CPU；
- 构造失败和 VM drop 都自动释放 claim，不需要调用方手工回滚；
- VM 创建后如果配置中的物理资源字段被修改，内存或设备准备必须在产生新副作用前拒绝该配置；
- 单元测试覆盖冲突、非冲突、释放和配置冻结，静态合同保证功能已接入创建与准备主路径。

## 3. 范围与非目标

本次 claim 覆盖：

- VM ID；
- 所有 VM 的 pCPU usage mask，以及 passthrough interrupt 模式下的独占 affinity mask；
- `PassThroughDeviceConfig.base_hpa/length`；
- identity `PassThroughAddressConfig.base_gpa/length`；
- `MapReserved` 的 identity HPA；
- `AddressSpacePolicy::Passthrough` 的隐式 identity HPA 窗口，采用 fail-closed 的整窗独占；
- x86 passthrough host I/O port 半开区间；
- FDT/runtime config 解析得到的 passthrough physical IRQ。

非目标：

- 不替代单 VM 的 `AxVmDevices` resource registry；
- 不改变 Guest GPA 内部布局规则；
- 不为 `MapAlloc` 或 `MapIdentical` 预先申请 HPA，它们由宿主 allocator 动态分配并保证唯一；
- 不在本次引入 VM 间共享设备、共享 IRQ 或可分割设备的授权模型；
- 不把共享内存、HyperCall、裸 MMIO 或 `vsock` 变成 Guest 主数据通道；
- 不在本次实现运行中热添加 passthrough 物理资源。动态 `map_reserved_memory_region()` 的外部调用需要后续独立的事务式 claim API，当前配置驱动流程不依赖该能力；
- 不负责把固定 passthrough/`MapReserved` RAM 从宿主 allocator 的可分配区移除。registry 只仲裁 AxVM 与 AxVM 之间的所有权；平台内存保留表或 allocator exclusion 仍必须独立配置和验证。

## 4. 当前仓库能力与 prior art

| 能力 | 位置 | 可复用结论 |
| --- | --- | --- |
| VM 运行时注册表 | `virtualization/axvm/src/manager.rs` | 可参考 `SpinNoIrq<BTreeMap<...>>` 的全局注册方式，但它检查太晚，不能直接承载 pending VM claim |
| 单 VM device 资源注册 | `virtualization/axdevice/src/device.rs`，提交 `e36ff51c5` | 复用“先验证全部、再原子提交、错误后不留部分状态”的语义；其 registry 是 per-VM，不能直接复用为跨 VM 所有者 |
| VM 内地址规划 | `virtualization/axvm/src/layout.rs` | 复用 4 KiB 对齐、半开区间、zero-length/overflow 拒绝语义；它只看一个 VM 的 GPA，不能回答 HPA 所有者 |
| 动态内存分配 | `AxVM::alloc_memory_region()` | `MapAlloc`/`MapIdentical` 的 HPA 来自宿主 allocator，无需建立配置期固定 HPA claim |
| identity reserved memory | `AxVM::map_reserved_memory_region()` | 直接执行 `GPA == HPA` mapping，必须纳入固定 HPA claim |
| VM 生命周期 drop | `impl Drop for AxVM` | 适合用 RAII lease 在最后一个 VM 引用释放时归还全局 claim |

没有发现当前 base 中具备相同生命周期和跨 VM HPA/pCPU/IRQ 语义的现成组件。该能力与 `AxVmDevices` 互补，不重复其单 VM虚拟设备检查。

## 5. 方案比较

| 方案 | 优点 | 主要问题 | 结论 |
| --- | --- | --- | --- |
| 保持现状 | 无代码变化 | 双 VM 可静默拥有同一物理资源，无法证明隔离 | 拒绝 |
| 仅在 AxVisor `init_guest_vm()` 预检 | 实现局部、容易接入当前程序 | 绕过 AxVM 库边界，其他调用方不受保护；释放与失败回滚易遗漏 | 拒绝 |
| 在 `register_vm()` 时检查 | 可与现有 VM registry 合并 | 检查发生在内存映射、镜像加载和设备/GIC 初始化之后 | 拒绝 |
| 扩展 `AxVmDevices` registry | 已有资源类型和事务注册 | registry 属于单 VM，创建时还没有完整 devices；pCPU、reserved RAM 和 VM ID 不属于设备资源 | 拒绝 |
| AxVM crate 内全局 registry + VM 持有 RAII lease | 在首次副作用前检查；库调用方统一；失败/drop 自动回滚；无需公共 API | 增加一个进程级锁和一份 claim 快照；必须处理创建后配置变更 | 采用 |

## 6. 选定设计

### 6.1 模块边界

已新增 crate-private `resource_claim` 模块，生产代码与单测分别位于 `resource_claim/mod.rs` 和 `resource_claim/tests.rs`。它只依赖 `AxVMConfig`、`AxVmError`、`VMId` 和仓库已有的非睡眠锁，不对外暴露 registry API。

模块包含：

- `PhysicalResourceClaims`：一个 VM 的规范化不可变 claim 快照；
- `HostPhysicalRange`：4 KiB 对齐的半开 HPA 区间；
- `PhysicalResourceRegistry`：可单测的 `BTreeMap<VMId, PhysicalResourceClaims>`；
- `PhysicalResourceLease`：插入全局 registry 后返回的 RAII 所有者。

`AxVM` 已新增一个私有、不可克隆的 lease 字段。AxVM 不暴露 registry，也不允许调用方手工 remove claim。

### 6.2 Claim 提取规则

| 配置来源 | 规范化 claim |
| --- | --- |
| `config.id()` | VM ID 唯一键 |
| `get_vcpu_affinities_pcpu_ids()` | 保存每个 `(vcpu_id, affinity_mask, phys_cpu_id)` 作为冻结快照；所有 VM 计算 usage mask，passthrough VM 另计算 exclusive mask |
| `pass_through_devices` | `base_hpa..base_hpa+length`，向外对齐到 4 KiB |
| `pass_through_addresses` | identity HPA=`base_gpa`，向外对齐到 4 KiB |
| `MapReserved` | identity HPA=`gpa`；base 和 size 必须已经 4 KiB 对齐，不静默改变实际 mapping 语义 |
| `MapAlloc` | 不进入配置 claim；实际 HPA 由 allocator 管理 |
| `MapIdentical` | 不进入配置 claim；当前实现先动态分配 HPA，再把 GPA 设为该 HPA |
| `AddressSpacePolicy::Passthrough` | 申请 AxVM stage-2 的完整 identity window；本阶段不尝试在全局层复制单 VM punch-hole 规划 |
| `pass_through_ports` | 规范化为 `[base, base+length)`；零长度或越过 `0x10000` 拒绝，相邻范围允许、重叠范围冲突 |
| `pass_through_irqs` | 排序、去重后的物理 IRQ ID；仅 passthrough interrupt 模式生效 |

所有地址使用半开区间 `[base, end)`。零长度、加法溢出或向上对齐溢出在获取全局锁前返回 `InvalidConfig`。相邻区间不冲突；只要满足 `a.base < b.end && b.base < a.end` 即冲突。

pCPU 规则按以下顺序执行：

1. 对所有 VM 的显式 affinity mask 做输入校验：零 mask 一律拒绝；当 `enabled_cpu_mask()` 已知且非零时，显式 mask 必须是 enabled mask 的子集；host-test 中 enabled mask 为零时只能验证非零规则，目标机仍需回归 disabled-CPU 拒绝路径；
2. 所有 VM 都生成 `pcpu_usage_mask`。每个显式 mask 按位合并；任一 vCPU 未 pin 时，usage 保守扩大为 `usize::MAX`，表示该 VM 可能使用任意 pCPU；
3. passthrough interrupt VM 的每个 vCPU 都必须有显式非零 mask，所有 mask 合并为 `exclusive_pcpu_mask`。claim 层允许多 bit mask；AArch64 passthrough SPI 路由还会独立要求 vCPU0 one-hot pin；
4. 冲突判断是对称的 `requested.exclusive & existing.usage` 与 `existing.exclusive & requested.usage`。因此 passthrough VM 不仅与另一个 passthrough VM 冲突，也会与任何占用同一 pCPU 的 emulated/no-IRQ VM 冲突；两个都没有 exclusive mask 的普通 VM 仍可共享 CPU；
5. 未 pin 的普通 VM usage 为全 CPU，所以它会与任意 passthrough exclusive mask 冲突，且不受 VM 插入顺序影响。这是无法证明调度范围时的 fail-closed 行为。

### 6.3 生命周期与状态转换

```text
enriched AxVMConfig
        |
        v
PhysicalResourceClaims::from_config
        |
        v
global registry validate + insert ---- conflict ---> return error, no state change
        |
        v
PhysicalResourceLease
        |
        v
CurrentArch::create_vm_resources ---- error -------> local lease Drop removes claim
        |
        v
Arc<AxVM> owns lease
        |
        +---- prepare-memory / prepare: validate config still matches frozen claim
        |
        v
last Arc<AxVM> Drop -> destroy resources -> lease Drop -> remove claim
```

Claim 已在 `AxVM::new()` 中、`CurrentArch::create_vm_resources()` 之前获取。这样重复 VM ID 或物理资源冲突在 stage-2 memory、Guest image 和设备/GIC 初始化前失败。

`AddressSpacePolicy::Passthrough` 还设置显式的全局 address-space 独占标记。只比较固定 HPA ranges 不足以覆盖另一个 VM 的 `MapAlloc`/`MapIdentical` 动态 HPA，因此任一侧使用整窗 identity policy 时拒绝任何第二个 VM，且与插入顺序无关。该全局拒绝同样不能替代宿主 allocator 对保留物理页的排除。

构造期间任一步骤返回错误时，Rust 局部变量析构自动释放 lease。正常销毁时，`AxVM::drop()` 先执行现有 `destroy()`，随后字段按 Rust 规则析构并释放 lease；物理资源不会在 VM 自身资源清理前提前变为可用。

### 6.4 创建后配置冻结

实现前，`AxVM::with_config()` 向 closure 公开 `&mut AxVMConfig`，内部启动代码用它调整 kernel/DTB/initrd 地址，外部调用者也能借此修改物理资源字段。当前实现为避免 claim 与真实配置成为两个状态源：

- 公开 `with_config()` 改为只提供 `&AxVMConfig`；
- 新增 crate-private `with_config_mut()`，只供仓库内启动准备代码调整非物理 boot/image 字段；
- lease 保存创建时的规范化 `PhysicalResourceClaims`，其中包含逐 vCPU placement，而不仅是 OR 后的 CPU mask；这样 affinity 对调或 `phys_cpu_id` 改变也会使冻结校验失败；
- `prepare_memory_layout()` 在读取 memory config 的同一 machine lock 临界区内重新提取并比较 claim；
- `complete_vm_init()` 在持有 machine lock、调用 architecture initialize 之前执行同样检查；
- 只改变 image/boot 地址不会改变 claim，因此保持现有流程；
- 改变 pCPU、passthrough HPA、reserved memory、address-space policy 或 IRQ 会返回配置错误，不自动替换全局 claim。

不采用“自动更新 claim”，因为 VM 可能已经建立旧 mapping，自动替换 registry 状态会掩盖未拆除的旧资源。运行时重配置需要未来独立的 stop/unmap/reclaim/claim/rebuild 事务。

### 6.5 冲突顺序与错误

registry 在同一个 `SpinNoIrq` 临界区内：

1. 检查 VM ID；
2. 检查 pCPU mask；
3. 检查固定 HPA ranges；
4. 检查 host I/O port ranges；
5. 检查 passthrough IRQ；
6. 所有检查通过后一次性插入 claim。

任何失败都不修改 registry。错误使用现有 `AxVmError::ResourceConflict`，`resource` 分别为 `VM ID`、`physical CPU`、`host physical address` 或 `passthrough IRQ`，detail 包含两个 VM ID 和具体 mask/range/IRQ。

pCPU 检查比较 `exclusive_pcpu_mask` 与对端 `pcpu_usage_mask`，不是只比较两个 exclusive mask。host I/O port 冲突使用 `resource = "host I/O port"`；`AddressSpacePolicy::Passthrough` 的全局排他使用 `resource = "host address space"`。

## 7. 并发、锁与性能

- 全局 registry 使用 AxVM 已采用的 `SpinNoIrq`，只在 VM 创建和最终 drop 时访问；不进入 vCPU run、IRQ dispatch 或网络数据热路径。
- claim 提取、排序、地址规范化和错误输入检查在锁外完成。
- 锁内复杂度为 VM 数和固定资源数的线性/二次小集合比较；当前目标是少量 VM，避免为推测规模增加 interval tree。
- 本模块不持有 VM registry lock、machine lock 或 device registry lock，因此没有新增跨锁顺序。

## 8. 兼容、启用与回滚

该检查默认启用，不增加 TOML 字段。原先资源不重叠的配置行为不变；原先依赖重复 VM ID、重叠固定 HPA/port/IRQ 或 passthrough pCPU 的配置会更早失败并给出原因。公开 `with_config()` 的 closure 参数已从 `&mut AxVMConfig` 收紧为 `&AxVMConfig`，这是有意的 API 变化：workspace 内合法的 boot/image 写调用已迁移到 crate-private `with_config_mut()`；下游若曾在 VM 创建后修改物理资源，需要改为在构造 `AxVMConfig` 时完成。新增的 `PhysicalResourceLease`、registry 与 prepare 校验均为 crate-private，不改变 Guest ABI、TOML 或镜像格式。

回滚只需移除 AxVM 的 lease 字段、创建/准备校验和私有模块，不涉及持久化数据、Guest ABI 或镜像格式。VM stop/reset 不释放 claim，因为同一 VM 仍拥有其物理资源；只有最后一个 `Arc<AxVM>` drop 才释放。

## 9. 已完成验证与待回归项

已确认的 host 侧证据：

- `cargo test -p axvm --features host-test resource_claim`：21 个 claim tests 通过，覆盖重复 VM ID、passthrough/passthrough 与 passthrough/emulated pCPU 冲突、普通 VM CPU 共享、跨来源 HPA、host port、IRQ、释放复用、整窗 policy、对齐和配置冻结；
- `cargo test -p axvm --features host-test --test arch_boundary_contract`：18/18 通过；
- `cargo test -p axvm --features host-test`：132 个 unit tests、18 个 architecture-boundary tests、4 个 error-contract tests 全部通过，0 failed；doc tests 为 0；
- `cargo clippy -p axvm --features host-test --all-targets -- -D warnings` 通过；
- Python 静态合同全部通过。`check_axvm_physical_resource_claims.py` 验证模块拆分、claim 类型、pCPU usage 字段、lease/Drop、创建前 acquire、两处 prepare 冻结校验、只读/可变 config API 分离和 CI 接线；其余合同覆盖 CI 路径、setup/image CLI、contest baseline、Linux SMP2、Zephyr smoke、AArch64 IRQ routing 与双 Guest outer topology；
- `cargo test -p arm_vgic --features vgicv3` 的 6 个 unit tests 与 3 个 error-contract tests，以及 arm_vgic/axvm 两条 targeted Clippy 也已通过，为 claim 后续触发的 AArch64 SPI route 生命周期提供 host 侧回归保护。
- `arm_vgic` 与 `axvm --lib --no-default-features` 的 `aarch64-unknown-none-softfloat` target check 均通过；
- 最新上游 Linux SMP2 与 Zephyr AxVisor 单 Guest QEMU run 均通过。它们在目标机 `enabled_cpu_mask != 0` 时成功校验合法 one-hot affinity，并证明单 VM claim 不阻断正常创建；它们不覆盖第二 VM 冲突或 allocator exclusion。

host-test 尚不能证明以下目标行为：

1. `enabled_cpu_mask != 0` 时显式 mask 包含 disabled CPU 的目标环境拒绝路径；当前 QEMU 只覆盖合法 mask；
2. QEMU 双 Guest 中，无冲突配置可共同启动，故意冲突配置在第二个 `AxVM::new()` 时失败且不会留下 mapping/IRQ 副作用；
3. 实机上 passthrough pCPU、HPA、IRQ 与 DMA 所有权确实和配置一致；
4. 平台启动前已把固定 passthrough/`MapReserved` RAM 从宿主 allocator 排除。claim 单测通过不能证明 allocator exclusion 已完成。

真实 AArch64/QEMU/实机证据必须来自实现后的当前 HEAD/dirty snapshot，不能复用旧提交上的运行结果代替。

## 10. 后续独立工作

- 为运行中热添加/移除 passthrough 资源设计事务式 API；
- 固定 passthrough/`MapReserved` RAM 必须先从宿主 allocator 可分配区排除；当前 claim registry 只仲裁 VM 之间的所有权，既不会修改 allocator free list，也不能替代平台内存保留表；这是进入实机 passthrough 前的独立阻断项；
- 为“普通 VM 未 pin 时 usage=`usize::MAX`”和显式 mask 包含 disabled CPU 增加目标环境或可注入 enabled-mask 的专门回归；当前实现逻辑已 fail-closed，但 host 单测环境不能覆盖非零 enabled mask；
- 若出现大量 VM/资源，再以基准数据决定是否引入 interval tree；
- 双 Guest 配置使用显式设备节点，禁止两个 VM 同时请求根节点 passthrough；
- 在资源隔离成立后继续 VirtIO-net + TCP/UDP/IP 主链路，网络共享策略不由本模块替代。
