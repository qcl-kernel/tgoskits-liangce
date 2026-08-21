# AArch64 passthrough SPI 路由回滚设计

状态：已实现，AArch64 target 与单 Guest QEMU 已验证；故障注入/实机回归待完成<br>
基线：`rcore-os/tgoskits` `dev`，`01e053105e243d1fdd1c03f6f850b266efbe8de0`<br>
风险级别：高风险（宿主 GICv3 物理寄存器与 VM 生命周期）

## 1. 问题

`Aarch64Arch::init_vm()` 在构建设备时调用 `VGicD::assign_irq()`。该函数会把 passthrough SPI 的宿主 `GICD_IROUTER` 改为 Guest vCPU0 对应的物理 affinity，随后 VM 初始化还可能在 DTB 校验、stage-2 映射或 vCPU setup 中失败。

实现前没有保存原寄存器值，也没有在以下路径恢复：

- 同一 VM 再次 `prepare()` 时的 transient-resource reset；
- `prepare()` 在部分 SPI 已分配后失败；
- VM destroy/drop；
- AxVM 丢弃自身 device 集合，但外部仍持有 `get_devices()` 返回的 `Arc<AxVmDevices>`。

该缺口会使未成功启动或已经销毁的 VM 继续改变宿主物理中断投递目标。当前实现已经加入 `IROUTER` 首次快照、显式生命周期释放与 `Drop` 兜底；跨 VM IRQ claim 仍只负责所有权冲突，不能替代寄存器事务回滚。

## 2. 已实现成功标准

- 第一次分配 SPI 时保存其原始 `GICD_IROUTER`；同一 VGicD 对同一 SPI 重复分配不能覆盖最初快照；
- transient reset 与 destroy 在丢弃 AxVM 自身 device 引用前显式恢复路由；
- 初始化失败时，临时 VGicD 的 `Drop` 作为兜底恢复已分配路由；
- 显式 release 与后续 `Drop` 可重复调用且只恢复一次；
- release 后清除 `assigned_irqs`，外部残留的 device `Arc` 不再被视为拥有这些 SPI；
- GICv3 affinity 路由只依赖 `IROUTER`，不再用 GICv2 风格 `ITARGETSR` 位图限制逻辑 pCPU 小于 8；
- 静态合同覆盖 assign、显式 lifecycle hook、Drop fallback 和测试接线。

上述代码路径已经实现。当前 dirty snapshot 已通过 AArch64 target check，并由最新上游 Linux SMP2 与 Zephyr 单 Guest QEMU run 覆盖正常 assign/boot 路径；这仍不等于回滚事务验收完成，因为尚未完成 AxVisor QEMU prepare-failure 故障注入或真实 GICv3 硬件寄存器回归。

## 3. 范围与非目标

本次只回滚 AxVM 在 Guest 启动前主动修改的 SPI 路由寄存器 `GICD_IROUTER`。GICv3 host 初始化已开启 affinity routing（ARE），因此 `IROUTER` 是当前路由来源，`ITARGETSR` 属于 legacy target 接口。`assign_irq()` 已不再写 `ITARGETSR`，但 VGicD 的 Guest MMIO 兼容分支仍保留该寄存器范围；本事务不会快照或恢复它。

本次不尝试快照 Guest 运行期间可能修改的全部物理 GIC 状态（enable、pending、active、group、priority、trigger configuration）。这些状态需要单独的完整 passthrough IRQ quiesce/restore 设计，不能假装由本次路由回滚解决。

## 4. 方案比较

| 方案 | 问题 | 结论 |
| --- | --- | --- |
| 只在 `VGicD::Drop` 恢复 | 外部可长期持有 `AxVmDevices` Arc，VM destroy 后 Drop 不一定发生 | 不足 |
| 只在 AxVM reset/destroy 恢复 | prepare 中临时 device 尚未提交，失败时 AxVMResources 看不到它 | 不足 |
| 显式 lifecycle release + VGicD Drop fallback | 正常路径按 VM 生命周期及时释放，未提交临时对象也能自动回滚 | 采用 |
| 在全局 IRQ claim registry 中写 GIC | 混合资源仲裁与架构硬件副作用，锁和回滚边界不清晰 | 拒绝 |

## 5. 设计

### 5.1 VGicD 路由快照

`VGicD` 已使用一个私有 `SpinNoIrq<RouteState>` 同时持有 assigned bitmap 与 `BTreeMap<u32, u64>` 路由快照。map 的键是完整 GIC INTID，值是第一次分配前读取的原始 `GICD_IROUTER`。原先公开的 `assigned_irqs` 字段被收进私有状态，对外继续通过 `is_irq_assigned()` 查询。单锁保证 Guest MMIO 的“检查 assigned 后访问物理寄存器”不会与 release 发生 check-then-use 竞态。

`assign_irq()` 顺序：

1. 校验 INTID；
2. 获取 route-state 锁，若 assigned 已存在则返回重复分配错误；
3. 按固定锁序获取宿主 `GICD_LOCK` 并读取当前 `IROUTER`；
4. 保存第一次快照并在 `assigned_irqs` 中标记所有权；
5. 写入 IRM 清零的目标 affinity。

寄存器读写不返回错误，因此一旦通过 INTID 校验，后续操作没有需要撤销的可失败分支。重复 assign 保留最初宿主值，避免 release 只恢复到 VM 自己上一次写入的路由。

### 5.2 幂等释放

`release_assigned_irqs(&self)` 已在 route-state 锁内用 `mem::take` 原子取走全部待恢复项，同时清除这些 `assigned_irqs` 位；随后在仍持有 route-state 锁时获取宿主 `GICD_LOCK`，逐项写回原始 `IROUTER`。

第一次调用后 map 为空，因此显式 reset、destroy 和 `Drop` 的任意组合都不会二次恢复或覆盖后来合法的新 owner。该函数只在 vCPU 尚未运行或已经 join 后调用，不进入中断热路径。

`Drop for VGicD` 调用该函数，覆盖 `PreparedDevices` 尚未提交便因初始化错误而析构的路径。

### 5.3 AxVM 架构生命周期 hook

在 `ArchOps` 已增加默认空实现：

```rust
fn release_vm_devices(_devices: &axdevice::AxVmDevices) {}
```

AArch64 实现查找 device 集合中的 `VGicD` 并调用 `release_assigned_irqs()`；其他架构行为不变。

`AxVMResources::reset_transient_resources()` 在任何可失败的 address-space 恢复之前调用 hook，随后才清空 device 引用。`AxVM::cleanup_resource_set()` 在 unmap/deallocate 和 `devices.take()` 前调用同一 hook。即使外部持有设备 Arc，物理路由也已经及时归还。

### 5.4 GICv3 target 语义

实现已删除 `assign_irq()` 对 `cpu_phys_id < 8` 的限制和 `ITARGETSR` 写入。GICv3 host 初始化明确启用 ARE；特定 PE 由完整四级 affinity 编码到 `IROUTER`，逻辑 CPU 编号不应被压缩为 GICv2 的 8 位 target list。

`assign_irq()` 删除不再具备硬件语义的 `cpu_phys_id` 参数，只接收完整 INTID 与 `target_cpu_affinity`。

### 5.5 API 与错误合同变化

- `VGicD::assign_irq(irq, cpu_phys_id, affinity)` 收紧为 `VGicD::assign_irq(irq, affinity)`；调用方必须传完整 GIC INTID 与 MPIDR affinity；
- 新增 `InvalidSpi` 与 `IrqAlreadyAssigned` 错误，分别拒绝非 `32..1020` INTID 和覆盖首次快照的重复分配；
- AxVM 将配置中的 SPI offset 用 `checked_add(32)` 转成 INTID；配置了 passthrough SPI 却找不到 VGicD 时，prepare 返回资源错误，不再仅告警后假成功；
- `ArchOps::release_vm_devices()` 是 crate 内生命周期扩展点，其他架构使用默认空实现，不改变 Guest ABI、配置格式或镜像格式。

## 6. 并发与所有权

- route snapshot map 与 assigned bitmap 共用一把 `SpinNoIrq`；assign、release 和所有基于 assigned 的 Guest 物理 GICD 访问都先持有该锁；
- 宿主 `GICD_LOCK` 始终在 route-state 锁之后获取，assign/release/Guest MMIO 采用同一顺序；
- 正常 lifecycle release 发生在 VM prepare/reset 或 vCPU join 后；Drop fallback 发生在对象已不可再被访问时；
- 跨 VM physical IRQ claim 保证两个 AxVM 不会合法地同时操作同一 passthrough IRQ；本模块仍保持重复 release 幂等；
- 外部残留 Arc 可以继续读取 device 对象，但 release 后不再拥有 assigned bit，也不会在最终 Drop 再写宿主路由。

## 7. 已完成验证与待回归项

当前 host 侧证据：

- `cargo test -p arm_vgic --features vgicv3`：6 个 unit tests 与 3 个 error-contract tests 通过，0 failed；覆盖 IRM 清零编码、首次快照不被覆盖、幂等 drain 和 release 后 stale access mask 归零；这些是纯状态/合同测试，不会访问真实 GICD；
- `cargo test -p axvm --features host-test --test arch_boundary_contract`：18/18 通过；
- `cargo test -p axvm --features host-test`：132 个 unit tests、18 个 architecture-boundary tests、4 个 error-contract tests 全部通过，0 failed；doc tests 为 0；
- `cargo clippy -p arm_vgic --features vgicv3 --all-targets -- -D warnings` 通过；
- `cargo clippy -p axvm --features host-test --all-targets -- -D warnings` 通过；
- `cargo check -p arm_vgic --features vgicv3 --target aarch64-unknown-none-softfloat` 通过；
- `CARGO_TARGET_DIR=/tmp/tgoskits-axvm-aarch64-check CARGO_INCREMENTAL=0 cargo check -p axvm --lib --target aarch64-unknown-none-softfloat --no-default-features --frozen` 连续两次通过；
- Python 静态合同全部通过，其中 `check_axvisor_aarch64_irq_routing.py` 检查 pinned pCPU、MPIDR affinity、IRM、SPI 边界、route snapshot/Drop、ArchOps hook、reset/destroy 接线和 GICR LAST；其余检查覆盖 CI 路径、setup/image CLI、contest baseline、Linux SMP2、Zephyr smoke、physical claims 与双 Guest outer topology；
- 最新上游 QEMU run `phase2-linux-smp2-upstream-20260726-r2` 和 `phase2-zephyr-axvisor-upstream-20260726` 均通过，证明配置了 VGicD 的正常 SPI assign/Guest 启动路径可运行；它们没有触发 reset/destroy 后的物理 route 读回。

仍必须补充：

1. AxVisor QEMU 故障注入：先成功分配一个 SPI，再让后续 prepare 失败，验证清理路径执行；普通启动成功标记不能替代该测试；
2. 真实 GICv3 硬件读取分配前、prepare 失败/reset/destroy 后的 `IROUTER`，证明原始 64 位值被精确恢复；
3. 外部保留 `Arc<AxVmDevices>` 的生命周期集成回归，确认显式 release 后 stale device 无法再操作已释放 SPI；现有纯状态单测只覆盖内部 route-state 行为。

## 8. 回滚与后续

回滚只需移除 snapshot 字段、release 方法、ArchOps hook 和 lifecycle 调用，不改变 Guest ABI、TOML 或持久化格式。

后续必须独立审计 Guest 运行期间可写的 GICD enable、pending、active、group、group modifier、priority 和 trigger configuration 状态，并设计 IRQ quiesce 后的完整快照恢复。当前实现只回滚 `GICD_IROUTER`，不得表述为“完整 GIC SPI 状态已恢复”。此外，私有 `GICD_LOCK` 不能与 host `somehal`/`arm-gic-driver` 的独立 affinity 写天然串行化；长期应把 claim/swap/restore 下沉到宿主 GIC/IRQ 所有权接口。
