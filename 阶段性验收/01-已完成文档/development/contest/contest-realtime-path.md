# P3 As-Built Realtime Path

状态：host-only 路径报告已建立；运行时事件、双 Guest discovery 和 production A/B 尚未完成（2026-08-14）。

本文是 P3 的 as-built 静态入口，不把计划中的调用链写成已经观测到的运行事实。真实事件必须由 `scripts/contest/rt/` 产生并通过 `p3-rt-event-v1` validator。

## 1. 固定逻辑链

```text
Guest timer expiry
  -> virtual IRQ enqueue
  -> owning vCPU wake
  -> vCPU re-entry
  -> Guest handler
```

当前 Zephyr dual 配置使用 timer/interrupt passthrough。若某个阶段没有经过当前配置的实际软件路径，事件必须在运行 manifest 中标记为 `not_applicable_passthrough`，不得生成假零延迟。

## 2. 当前源码锚点

| 逻辑阶段 | 当前源码锚点 | 当前静态结论 |
|---|---|---|
| vCPU task 创建和 pCPU 绑定 | `virtualization/axvm/src/runtime/vcpus.rs::build_vcpu_task` | `phys_cpu_ids` 通过 cpumask 绑定；fallback warning 必须被 runtime validator 视为异常 |
| vCPU 主循环 | `virtualization/axvm/src/runtime/vcpus.rs::vcpu_run` | 先经过 `before_vcpu_run`，再进入架构运行入口 |
| queued IRQ | `virtualization/axvm/src/runtime/vcpus.rs::{queue_interrupt,inject_pending_interrupts}` | 代码存在分层，但不能据此证明 Zephyr passthrough timer 经过该路径 |
| AArch64 entry/exit | `virtualization/axvm/src/architecture/ops.rs::run_vcpu`、`virtualization/arm_vcpu/src/vcpu.rs::{run,run_guest}` | 进入 Guest 前处理 pending IRQ；真实事件仍待采集 |
| virtual timer | `virtualization/axvm/src/timer.rs`、`virtualization/arm_vgic/src/vtimer/` | 仅适用于实际 virtualized timer 路径；当前 passthrough 配置不能自动归因到此处 |
| Guest 周期 marker | `scripts/contest/rt/zephyr-rt-probe/` | 新增 probe 模板；尚无 target build 或 runtime evidence |

## 3. 测量边界

- 时间只使用 Guest/Host monotonic counter；wall clock 只用于目录命名。
- 事件必须包含 `run_id`、endpoint、scenario、sample_index、monotonic_ns、raw_counter_ticks、clock_mapping_id、VM/vCPU/pCPU 和 `trace_sequence`。
- 生产改动前必须完成 native、AxVisor idle 和 Linux CPU discovery；候选在三个 seed 中均达到 `15%` P99.9 或 `20 us` 才可进入 production feature。
- P4 network、P5 AI 和 combined profile 只能在对应 gate evidence 通过后运行。

## 4. 当前不证明

- 本文不证明 timer/IRQ/wakeup 运行时链路已经被观测。
- 本文不证明双 Guest、Guest IP、AI-loop 或实时改善。
- 当前 host schema、统计器和 selector 通过，只证明工具合同，不证明任何 runtime 指标。
