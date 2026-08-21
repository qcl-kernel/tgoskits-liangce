# 阶段 P3：AxVisor 实时路径测量、选择与 A/B 实施手册

最后更新：2026-08-14

适用工作包：`P3-RT-01`

规范状态：方案已冻结；host schema/statistics/matrix/native probe/candidate selector 已实现并通过合同，生产改动与完整 runtime evidence 未实现

目标平台：`QEMU AArch64 + AxVisor + Linux SMP2 + Zephyr SMP1`

## 1. 本文解决什么问题

本文让未接触过项目的开发者可以直接接手 P3，并明确回答：先改什么、在哪些文件改、怎样测、什么数据允许进入生产改造、怎样做 A/B、需要保存哪些工件，以及何时必须停止并回滚。

P3 不是“凭经验优化调度器”。它必须先建立可复算的原生/AxVisor 实时基线，再从数据中选择最多两条实际瓶颈路径，最后用默认关闭、可独立回滚的开关完成同输入 A/B。没有候选达到冻结门槛时，正确结果是发布“没有数据支持生产优化”的基线结论；不得降低门槛、只报均值或先写优化再挑指标。

本文是实现手册，不改写冻结规范。冲突时按以下顺序执行：

1. [`contest-spec/sources-and-decisions.md`](contest-spec/sources-and-decisions.md) 的 `DEC-002/003/006/008`；
2. [`contest-spec/requirements.md`](contest-spec/requirements.md) 的 `REQ-RT-001..003`；
3. [`contest-spec/contracts.md`](contest-spec/contracts.md) 的 `IF-010/011`；
4. [`contest-spec/test-matrix.md`](contest-spec/test-matrix.md) 的 `TEST-008..010`；
5. [`contest-spec/work-packages.md`](contest-spec/work-packages.md) 的 `P3-RT-01`；
6. 本文的文件、命令、拆分和回滚说明。

若冻结阈值、平台、pCPU 分配、时钟语义或执行顺序需要变化，先更新对应 `DEC-*` 及全部追踪文件，不能让代码先形成第二套事实。

## 2. 当前结论与执行边界

### 2.1 当前状态

- `REQ-RT-001` 最高只有静态 CPU 绑定和单 Guest smoke 前置；没有双 Guest 压力下的迁移、优先级或干扰证据。
- `REQ-RT-002` 未开始；既有 Zephyr EOImode 修复是功能正确性成果，不是按 P3 数据门禁选出的实时优化。
- `REQ-RT-003` 处于静态合同；历史原生/AxVisor 日志只有 10 个约 110 ms 样本，不满足 3 个独立 run、每 run 至少 100,000 个有效周期且持续至少 30 分钟的要求。
- `scripts/contest/rt/` 已有事件 schema、统计器、六场景 run plan、native probe 与候选选择工具并通过 host 合同；没有可关闭本需求的 runtime raw samples 或 production A/B。
- P3 的只读路径梳理、schema、统计器、native baseline 可以并行开发；完整 production A/B 必须等 P4 Guest IP 和 P5 AI 闭环通过。

### 2.2 与其他阶段的固定顺序

```text
P2-DMA-01
  -> P2-DUAL-01 / TEST-006
  -> P2-SOAK-01 / TEST-007
  -> P4-UPSTREAM-01
  -> P4-NET-01 / TEST-011..015
  -> P5-AI-01 / TEST-016..018
  -> P3-RT-01 production A/B / TEST-009..010
  -> P6-QUAL-01 / TEST-019
```

允许提前完成：P3 schema、host 工具、Zephyr probe、原生基线、静态路径报告。

不得提前完成：改变 timer/IRQ/wakeup/lock 生产语义、发布优化结论、把无网络/AI 压力的局部数据写成完整 `TEST-009`。

### 2.3 P3 的非目标

- 不重写 AxVisor 调度器，不更换 task runtime，不引入新的全局并发模型。
- 不改变 Linux `[0,1]`、Zephyr `[2]`、Host/console `[3]` 的 CPU 所有权。
- 不改变 P4 的 mediated VirtIO-net、P5 的 100 ms 控制周期、ICPC payload、500 ms 安全态或网络地址/端口。
- 不以 Host benchmark、构建通过、单次 marker、平均值或截图证明实时改善。
- 不把 WSL2/QEMU 结果外推为开发板或硬件实时上界。
- 不创建或提交 PR；代码只按项目交付规则进入私有比赛仓库和离线审查包。

## 3. 接手前的只读检查

先从仓库根目录执行：

```bash
git status --short --branch
python3 scripts/test/check_contest_developer_docs.py
python3 scripts/test/check_contest_baseline.py
python3 scripts/test/check_axvisor_dual_guest_configs.py
python3 scripts/test/check_axvisor_dual_guest_topology.py
```

Windows 纯 Python 检查可把 `python3` 换成 `py -3`；正式构建和 QEMU runtime 仍使用 Ubuntu 24.04 WSL2。不得清理或覆盖当前 dirty worktree；先把基线 revision、dirty patch SHA-256 和已有 evidence 记录到新 run 的 `session.json`。

然后确认以下依赖状态：

| 门禁 | 开始哪些工作前必须满足 | 不满足时允许做什么 |
|---|---|---|
| `TEST-006` 双 Guest 300 s | AxVisor 双 Guest 实时采集 | 只做 host 工具、schema、native probe 和静态路径梳理 |
| `TEST-007` 双 Guest 1,800 s | 任何生产 timer/IRQ/wakeup/lock 改动 | 继续测量工具开发，不改变生产语义 |
| `TEST-011..015` Guest IP/ICPC | network pressure 基线 | 只跑 native、AxVisor idle、Linux CPU pressure |
| `TEST-011..018` Guest IP + AI loop/safe state | AI、network+AI 组合压力和最终 A/B | 保留早期基线为 `partial`，不得关闭 P3 |

## 4. 已核实的当前源码路径

开始编码前必须重新执行下列检索；路径变化时更新本节和最终 as-built 报告，不能根据旧行号盲改：

```bash
rg -n "interrupt_mode|/timer|phys_cpu_ids" \
  os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml \
  os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml
rg -n "passthrough_timer|passthrough_interrupt|CNTVOFF_EL2" \
  virtualization/axvm/src/arch/aarch64 \
  virtualization/arm_vcpu/src
rg -n "register_timer|check_events|queue_interrupt|inject_pending_interrupts|send_ipi|before_vcpu_run" \
  virtualization/axvm/src virtualization/arm_vgic/src/vtimer
```

截至本文更新时，代码事实如下：

| 逻辑阶段 | 当前实现锚点 | 必须先确认的事实 |
|---|---|---|
| Zephyr CPU 绑定 | `os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml` | VM2/vCPU0 固定 pCPU2；配置为 `interrupt_mode="passthrough"`，并透传 `/timer` |
| Linux CPU 绑定 | `os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml` | VM1 两个 vCPU 固定 pCPU0/1 |
| vCPU task 创建/绑定 | `virtualization/axvm/src/runtime/vcpus.rs::build_vcpu_task` | `phys_cpu_ids` 经 `set_cpumask` 落到 task；fallback/mask warning 必须计为异常 |
| vCPU run loop | `virtualization/axvm/src/runtime/vcpus.rs::vcpu_run` | `before_vcpu_run` 后进入 `CurrentArch::run_vcpu`；WFE/暂停走 wait queue |
| queued IRQ 路径 | `runtime/vcpus.rs::{queue_interrupt,inject_pending_interrupts}`、`runtime/{dispatcher,queue}.rs` | enqueue、notify、IPI、drain、inject 已有分层，但当前 Zephyr passthrough timer 不得未经验证就宣称走过该路径 |
| AArch64 enter/exit | `virtualization/axvm/src/architecture/ops.rs::run_vcpu`、`arm_vcpu/src/vcpu.rs::{run,run_guest}` | pending IRQ 在 guest entry 前 drain；底层保存/恢复 GIC 与 timer context |
| virtual timer 路径 | `virtualization/axvm/src/timer.rs`、`virtualization/arm_vgic/src/vtimer/` | 非 passthrough 模式可注册 host timer 并注入 virtual IRQ；不是当前 production Zephyr 配置的当然事实 |
| 时钟映射 | `virtualization/arm_vcpu/src/vcpu.rs::init_vm_context`、`context_frame.rs` | Guest `CNTVCT_EL0 = CNTPCT_EL0 - CNTVOFF_EL2`；当前初始化让 Guest virtual counter 从接近零开始，跨 EL1/EL2 合并前必须记录 offset/frequency |
| 历史 probe | `scripts/contest/zephyr-periodic-smoke/` | 只打印 10 个毫秒级样本；只能复用构建骨架，不能直接作为 P3 采集器 |

### 4.1 passthrough 与冻结逻辑链的处理

冻结需求使用逻辑链：

```text
Guest timer expiry
  -> virtual IRQ enqueue
  -> owning vCPU wake
  -> vCPU re-entry
  -> Guest handler
```

当前 Zephyr 配置实际是 timer/interrupt passthrough，因此其中某些软件阶段可能根本不执行。P3 必须先生成 `path.json`，逐阶段写 `present=true|false`、实现锚点和观测方法：

- 真实执行的阶段才产生事件；
- 不存在的阶段在 `path.json` 标成 `not_applicable_passthrough`，不得补造零时延事件；
- 不可观测但可能存在的阶段标成 `unobservable`，该段不得进入候选排名；
- `runtime/dispatcher.rs` 存在不等于 Zephyr timer 经过它；
- 若要把 production 配置从 passthrough 改为 virtualized timer/IRQ，这属于测量对象和中断语义变化，必须先增加受控决策、重跑 P2 双 Guest/soak 和功能门禁，不能在 P3 内静默切换。

因此，P3 的第一项产物不是优化 patch，而是与当前源码和配置一致的 as-built `contest-realtime-path.md`。路径不成立时允许阻塞，不允许把目标架构图当作运行事实。

## 5. 目录和文件级实施清单

以下是目标布局，不是“全部待新建”清单。事件 schema、`validate_rt_events.py`、`summarize_rt.py`、`select_rt_candidates.py`、`build_rt_matrix.py`、native probe、六份 profile 和五项 host 合同已存在；缺失的 run/summary schema、fixture、统一 runtime runner 和生产改动只在对应子包创建。继续开发必须维护现有文件，不得复制第二套统计器或矩阵格式。

```text
scripts/contest/rt/
├── README.md
├── schema/
│   ├── p3-rt-event-v1.schema.json
│   ├── p3-rt-run-v1.schema.json
│   └── p3-rt-summary-v1.schema.json
├── fixtures/
│   ├── valid/
│   └── invalid/
├── zephyr-rt-probe/
│   ├── CMakeLists.txt
│   ├── prj.conf
│   └── src/main.c
├── validate_rt_events.py
├── summarize_rt.py
├── select_rt_candidates.py
├── build_rt_matrix.py
└── run_rt_matrix.py

scripts/test/
├── check_contest_rt_event_schema.py
├── check_contest_rt_statistics.py
├── check_contest_rt_candidate_selection.py
└── check_contest_rt_run_plan.py

configs/contest/
├── p3-rt-native.toml
├── p3-rt-ax-idle.toml
├── p3-rt-ax-cpu.toml
├── p3-rt-ax-net.toml
├── p3-rt-ax-ai.toml
└── p3-rt-ax-combined.toml

development/contest/
├── contest-stage-p3-realtime.md        # 权威实现手册（仓库内有受控镜像）
└── contest-realtime-path.md            # P3-A 持续更新的 as-built 路径/选择报告
```

上述六个 profile 文件名是冻结接口，不得由开发者临时换名或合并。其场景映射为：

| profile | 唯一 `scenario` | 固定内容 |
|---|---|---|
| `p3-rt-native.toml` | `RT-NATIVE` | Non-secure Zephyr、100 ms probe、无 AxVisor/Linux |
| `p3-rt-ax-idle.toml` | `RT-AX-IDLE` | P2 已验证 dual 输入、Linux idle |
| `p3-rt-ax-cpu.toml` | `RT-AX-CPU` | 同一 dual 输入、Linux pCPU0/1 固定 CPU stress |
| `p3-rt-ax-net.toml` | `RT-AX-NET` | P4 已验证 vnet0/Guest-IP 输入与固定 network pressure |
| `p3-rt-ax-ai.toml` | `RT-AX-AI` | P5 已验证 AI loop，不额外叠加 CPU stress |
| `p3-rt-ax-combined.toml` | `RT-AX-COMBINED` | CPU + network + AI 的固定组合压力 |

profile 只存放仓库相对配置、场景参数、负载参数与预期门禁；不存放个人绝对路径、历史 evidence 目录或一个候选的开/关结果。具体输入路径和 hash 由 matrix builder 从当前 verified gate 及命令行锁入 `matrix.json`；候选 feature off/on 由 production matrix 展开，不用两份手写 profile 制造隐藏差异。

生产源码只允许在数据指向时修改下列范围：

| 路径 | 允许内容 | 禁止内容 |
|---|---|---|
| `virtualization/axvm/src/runtime/` | 有界 trace、实际经过的 IRQ queue/wakeup/entry 最小改动 | 重写调度器、在锁内打印/分配/唤醒、改变无关架构 |
| `virtualization/axvm/src/timer.rs` | 实际 virtual timer 路径的测点或获选改动 | 在 passthrough 未经过该文件时据此宣称收益 |
| `virtualization/axvm/src/architecture/ops.rs`、`arch/aarch64/` | vCPU entry/exit、AArch64 clock mapping、实际路径的最小适配 | 改 pCPU 所有权、改 P4/P5 语义、扩大为公共跨架构 API |
| `virtualization/arm_vcpu/` | 只有数据证明底层 enter/exit 或 timer context 是候选时才改 | 未经 AArch64 build/runtime 的寄存器顺序改动、新增无依据 `unsafe` |
| `virtualization/arm_vgic/` | 只有实际走 virtual IRQ/vtimer 且获选时才改 | 把 passthrough 路径误归为 VGIC，或改变未被测 IRQ 语义 |
| `os/axvisor/Cargo.toml`、`src/manager.rs` | feature 透传、启动时打印最终 compiled feature 和 CPU ownership | 放入 timer/IRQ 算法或旁路 runtime |
| `configs/contest/` | 新建 P3 run profile，继承已验证 P2/P4/P5 输入并只改变被测开关 | 原地改写历史 evidence 使用的配置或改变网络/AI 负载 |

Zephyr probe 先放在 `scripts/contest/rt/zephyr-rt-probe/`，以便 P4/P5 未完成时获取 native/AxVisor 基线。P5 的 `apps/contest/zephyr-control/` 建成后，把同一测量模块作为明确源码依赖或机械同步生成物接入；禁止维护两套事件语义。

## 6. P3-RT-01 子包、实现顺序与提交顺序

### P3-RT-A：冻结输入并形成 as-built 路径

输入：当前 Git revision/dirty patch、双 Guest TOML、Zephyr `.config`/最终 DTS/ELF/bin、Linux image/rootfs、QEMU/AxBuild/Zephyr SDK 版本。

实施：

1. 新建 `contest-realtime-path.md`，记录第 4 节所有函数锚点和当前真实 `path_variant`。
2. 从 VM 配置解析 CPU 集，不在脚本中另写 `[0,1]`/`[2]` 常量。
3. 记录 Zephyr task priority、period、budget、日志线程优先级；任一为空则拒绝正式采集。
4. 将 production config 与 instrumentation config 分开；二者除 trace 开关外逐字段比较。
5. 生成 `path.json` 和 `inputs.json`，逐文件保存 SHA-256。

完成条件：路径报告能让 reviewer 从每个逻辑阶段跳到真实函数；passthrough 中不存在的阶段被明确标注，没有借计划代码补空白。

### P3-RT-B：事件 schema、validator 和统计器

先写 invalid fixtures，再实现 parser。至少覆盖：缺字段、未知 schema、时间倒退、重复 sample、跨 run 拼接、错误 clock mapping、drop 非零、单位错误、样本不足、status 过早、hash 篡改。

固定 CLI：

```bash
python3 scripts/contest/rt/validate_rt_events.py \
  --run results/baseline/runs/<run-id> \
  --schema scripts/contest/rt/schema/p3-rt-event-v1.schema.json

python3 scripts/contest/rt/summarize_rt.py \
  --run results/baseline/runs/<run-id> \
  --output results/baseline/runs/<run-id>/summary.json

python3 scripts/contest/rt/select_rt_candidates.py \
  --matrix results/baseline/runs/<matrix-id>/matrix.json \
  --output results/baseline/runs/<matrix-id>/selection.json
```

约定：数据/schema 错误非零退出；合法但没有候选时选择器退出 0，并写 `decision="no_candidate"`。该结果不能把 P3 标为完成，但也不是工具失败。

#### P3-RT-B.1：turnkey matrix CLI 合同

`build_rt_matrix.py`、`run_rt_matrix.py` 和六个 profile 当前均是本阶段待实现文件。实现时必须支持下列精确 CLI；不得让执行者再手工拼 QEMU argv、临时发明 profile 名或修改 `matrix.json`。先在 Ubuntu 24.04 / WSL2 中设定公共参数：

```bash
set -euo pipefail
repo="$(pwd -P)"
test -f "$repo/configs/contest/qemu-aarch64-zephyr-smoke.env"
test -n "${ZEPHYR_BASE:?set ZEPHYR_BASE to the locked Zephyr 4.4.0 checkout}"
test -n "${ZEPHYR_SDK_INSTALL_DIR:?set ZEPHYR_SDK_INSTALL_DIR to Zephyr SDK 1.0.1}"

runs="$repo/results/baseline/runs"
profiles="$repo/configs/contest"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
revision="$(git rev-parse --short=12 HEAD)"
west_bin="${WEST_BIN:-west}"
cargo_bin="${CARGO_BIN:-cargo}"
```

`ZEPHYR_BASE` 必须指向 `configs/contest/qemu-aarch64-zephyr-smoke.env` 锁定 SHA 的 clean checkout，`ZEPHYR_SDK_INSTALL_DIR` 必须指向该文件锁定的 SDK。builder/runner 自己重新验证 revision、manifest hash、SDK/west 版本和工具路径；不能因为环境变量存在就信任它。

Native `TEST-008` 三次正式 run：

```bash
native_id="p3-native-${stamp}-${revision}"
native_matrix="$runs/${native_id}-matrix"

python3 scripts/contest/rt/build_rt_matrix.py \
  --mode native \
  --matrix-id "$native_id" \
  --profile "$profiles/p3-rt-native.toml" \
  --seed 7 \
  --seed 19 \
  --seed 43 \
  --output-dir "$native_matrix"

python3 scripts/contest/rt/run_rt_matrix.py \
  --repository "$repo" \
  --matrix "$native_matrix/matrix.json" \
  --zephyr-base "$ZEPHYR_BASE" \
  --zephyr-sdk-dir "$ZEPHYR_SDK_INSTALL_DIR" \
  --west-bin "$west_bin" \
  --cargo-bin "$cargo_bin" \
  --evidence-root "$runs" \
  --jobs 1
```

Discovery 矩阵只在当前源码/镜像世代的 `TEST-007` 已通过后运行。`test007_run` 是那个 immutable evidence run 的绝对根目录，目录下必须有 terminal `status.json`、`session.json`、`manifest.json` 和 cleanup 工件：

```bash
test007_run="<absolute-current-generation-TEST-007-run-directory>"
discovery_id="p3-discovery-${stamp}-${revision}"
discovery_matrix="$runs/${discovery_id}-matrix"

python3 scripts/contest/rt/build_rt_matrix.py \
  --mode discovery \
  --matrix-id "$discovery_id" \
  --profile "$profiles/p3-rt-ax-idle.toml" \
  --profile "$profiles/p3-rt-ax-cpu.toml" \
  --seed 7 \
  --seed 19 \
  --seed 43 \
  --native-matrix "$native_matrix/matrix.json" \
  --gate "TEST-007=$test007_run" \
  --output-dir "$discovery_matrix"

python3 scripts/contest/rt/run_rt_matrix.py \
  --repository "$repo" \
  --matrix "$discovery_matrix/matrix.json" \
  --zephyr-base "$ZEPHYR_BASE" \
  --zephyr-sdk-dir "$ZEPHYR_SDK_INSTALL_DIR" \
  --west-bin "$west_bin" \
  --cargo-bin "$cargo_bin" \
  --evidence-root "$runs" \
  --jobs 1

python3 scripts/contest/rt/select_rt_candidates.py \
  --matrix "$discovery_matrix/matrix.json" \
  --output "$discovery_matrix/selection.json"
```

Production A/B 仅在 P4/P5 门禁全部通过且 `selection.json` 真正选出 1–2 条路径时运行。下列每个 `testNNN_run` 都从当前 evidence index/交接表中取得该 TEST 已通过的 immutable run 绝对根目录；同一综合 run 若确实分别关闭多个 oracle，允许多个变量指向同一目录，但 builder 仍须逐 TEST 复核 scope：

```bash
test011_run="<absolute-current-generation-TEST-011-run-directory>"
test012_run="<absolute-current-generation-TEST-012-run-directory>"
test013_run="<absolute-current-generation-TEST-013-run-directory>"
test014_run="<absolute-current-generation-TEST-014-run-directory>"
test015_run="<absolute-current-generation-TEST-015-run-directory>"
test016_run="<absolute-current-generation-TEST-016-run-directory>"
test017_run="<absolute-current-generation-TEST-017-run-directory>"
test018_run="<absolute-current-generation-TEST-018-run-directory>"
production_id="p3-production-ab-${stamp}-${revision}"
production_matrix="$runs/${production_id}-matrix"

python3 scripts/contest/rt/build_rt_matrix.py \
  --mode production-ab \
  --matrix-id "$production_id" \
  --profile "$profiles/p3-rt-ax-idle.toml" \
  --profile "$profiles/p3-rt-ax-cpu.toml" \
  --profile "$profiles/p3-rt-ax-net.toml" \
  --profile "$profiles/p3-rt-ax-ai.toml" \
  --profile "$profiles/p3-rt-ax-combined.toml" \
  --seed 7 \
  --seed 19 \
  --seed 43 \
  --native-matrix "$native_matrix/matrix.json" \
  --discovery-matrix "$discovery_matrix/matrix.json" \
  --selection "$discovery_matrix/selection.json" \
  --candidate-layout separate-plus-combined \
  --gate "TEST-007=$test007_run" \
  --gate "TEST-011=$test011_run" \
  --gate "TEST-012=$test012_run" \
  --gate "TEST-013=$test013_run" \
  --gate "TEST-014=$test014_run" \
  --gate "TEST-015=$test015_run" \
  --gate "TEST-016=$test016_run" \
  --gate "TEST-017=$test017_run" \
  --gate "TEST-018=$test018_run" \
  --output-dir "$production_matrix"

python3 scripts/contest/rt/run_rt_matrix.py \
  --repository "$repo" \
  --matrix "$production_matrix/matrix.json" \
  --zephyr-base "$ZEPHYR_BASE" \
  --zephyr-sdk-dir "$ZEPHYR_SDK_INSTALL_DIR" \
  --west-bin "$west_bin" \
  --cargo-bin "$cargo_bin" \
  --evidence-root "$runs" \
  --jobs 1
```

`--gate TEST-ID=<run-dir>` 不是人工声明。builder 必须调用公共 `IF-011` validator，验证 `success=true`、producer-specific completed token、manifest/hash、cleanup 和该 TEST oracle/scope，并把 gate 根目录、status/manifest hash 写入 matrix。任一 gate 为 `partial`、`failed-attempt`、`blocked`、历史不同输入世代或只有文档状态时，builder 非零退出。`--output-dir` 和每个预生成 run 目录必须原先不存在；`--jobs` 正式值固定为 1，避免并行运行争用同一 live QEMU/配置。

builder 固定展开 seed 7 `off→on`、seed 19 `on→off`、seed 43 `off→on`；`off/on` 指候选生产 feature，discovery 所需的 `contest-rt-trace` 在配对两侧保持相同。有两个候选时，`separate-plus-combined` 先为每个候选独立展开完整矩阵，再增加 combined 辅助结果；combined 不替代任一单项结果。runner 必须从 `matrix.json` 执行预登记 run ID/顺序并只追加新 evidence，不得回写或重解释 matrix plan。

### P3-RT-C：Zephyr 高优先级 probe 与 native baseline

从现有 `zephyr-periodic-smoke` 复制最小构建骨架，不能继续使用 10-sample/millisecond 日志格式。实现要求：

- period 固定 100 ms；正式样本使用 64-bit counter/ns，不使用 wall clock；
- timer callback/ISR 只写预分配的有界 record ring；不 `printk`、不分配、不等待；
- 高优先级周期线程写 `period_release/start/finish` 和 `guest_handler_enter`；
- 低优先级 drain 线程批量输出 JSONL 或严格二进制 frame；buffer 满时递增 drop 并使 run 失败；
- 网络、console、日志和后台线程优先级低于周期线程；最终 priority/budget 进入 `inputs.json`；
- 每条样本含连续 `sample_index`；溢出、重复、时间倒退或缺口不能静默修复。

先做三次短校准，比较 `contest-rt-trace` 关闭与开启时 Guest 自身 `abs_jitter_ns.p99_9`。trace 开启不得新增 deadline miss，P99.9 开销必须不超过关闭态的 5%；超过即先改成更低开销的预分配/批量方案，不能进入正式矩阵。这个 5% 是采集质量门禁，不是对外实时优化结论。

完成 `TEST-008` 时，原生 Zephyr 必须有 3 个独立 run。每 run 同时满足：

- warm-up 1,000 个周期，原始记录保留但不进正式统计；
- warm-up 后有效周期 `>=100,000`；
- 从首个有效 `period_release` 到最后一个有效 `period_finish` 的单调持续时间 `>=1,800 s`；
- drop、重复、时间倒退、unclassified sample 均为 0。

100 ms 周期下 100,000 个有效样本约需 10,000 秒，因此实际由样本数门禁主导，不能只跑 30 分钟。

### P3-RT-D：AxVisor path trace 与 CPU ownership

实现默认关闭的 `contest-rt-trace` 构建 feature。off/on 使用同一源码 revision 和 build/QEMU/VM 输入，只允许 feature 集合存在这一项差异；feature 缺失时不得保留热路径分支以外的副作用。

测点必须遵守：

- 读取 counter、写入预分配 per-pCPU ring；IRQ/调度敏感上下文不打印、不分配、不获取非 IRQ-safe 锁；
- 每条 host event 记录 `vm_id/vcpu_id/pcpu_id/intid/path_variant`；
- `vCPU wake` 必须在真正执行 notify/IPI 的位置记录，不能在 enqueue 前预记成功；
- `vCPU re-entry` 在 drain/inject 完成、执行 guest entry 之前记录；
- `timer_expiry` 只能来自真实 deadline 到期点；passthrough timer 不经过 host timer wheel 时不得伪造；
- console collector 只负责搬运证据，不参与被测周期路径。

CPU ownership validator 必须证明整个 run 中 VM2/vCPU0 只在 pCPU2 entry/exit，Linux vCPU 只在 pCPU0/1，pCPU3 不运行 Guest。任何 fallback cpumask warning、迁移或未知 pCPU 都使 run 失败。

### P3-RT-E：早期基线矩阵

P2 dual/soak 通过后，先执行以下 discovery 场景：

| 场景 ID | 系统 | 负载 | 进入候选选择 |
|---|---|---|---|
| `RT-NATIVE` | 原生 Non-secure Zephyr | 仅 probe | 是，作为原生基准 |
| `RT-AX-IDLE` | 双 Guest AxVisor，Linux idle | 无额外压力 | 是 |
| `RT-AX-CPU` | 双 Guest AxVisor | Linux pCPU0/1 固定 CPU stress | 是 |

每个场景使用 run seeds `7/19/43`，每 seed 独立启动、独立目录、独立 nonce；不得在同一 QEMU 生命周期中把三段日志拆成三个 run。镜像、TOML、trace schema、周期和采样参数固定；CPU stress 命令、worker 数和 affinity 必须进入 `commands.jsonl`。

P4/P5 未完成时，这一矩阵最高只能支持候选发现和 `partial` 报告，不能关闭 `TEST-009`。

### P3-RT-F：候选选择门禁

候选限定为实际路径中的 `timer`、`IRQ`、`vCPU wakeup/re-entry` 或明确锁临界区。选择器按以下顺序执行，不能人工跳过：

1. 校验 3 个 seed 的 native、AxVisor idle 和 Linux CPU stress 输入身份与样本门禁。
2. 对每个 seed 计算 AxVisor-off 相对 native 的预登记总指标差；默认总指标是 `abs_jitter_ns.p99_9`，调度路径同时报告 `wakeup_latency_ns.p99_9`。
3. 只有三个配对 seed 中总 P99.9 恶化均 `>=15%`，或绝对增加均 `>=20 us`（`20,000 ns`），才允许继续归因。
4. 对真实可观测段计算其 P99.9 和占总 P99.9 的比例；三个 seed 中该段均贡献 `>=15%` 或绝对 `>=20 us`（`20,000 ns`）才成为候选。
5. 方向不一致、任一 seed 不达标、事件缺失、passthrough 中阶段不存在或噪声未分类时淘汰。
6. 按三个 seed 的 P99.9 相对贡献中位数降序排列；同值时按绝对贡献中位数降序，再按稳定 path ID 字典序，最多选择前两条。

`selection.json` 至少包含：输入 run/hash、预登记指标、每 seed 数值、是否越过两种门槛、淘汰原因、最终 `selected_paths` 和生成命令。不得手工编辑选择器输出；需调整算法时先版本化 schema/脚本并重跑全部 baseline。

### P3-RT-G：最多两个生产改动

只有 `selection.json` 选中的路径才能进入生产代码。每个候选独立完成：

1. 先写能在旧实现失败的最低层确定性回归或行为合同；
2. 写独立设计小节：问题、数据、替代方案、并发/IRQ/寄存器风险、回滚；
3. 只改最小实际路径，不顺手重构相邻模块；
4. 增加语义化编译 feature，例如 `contest-rt-opt-vcpu-wake`，默认关闭；
5. off build 不编入该 feature，on build 只增加该 feature；两份 build manifest 必须证明源码 revision、target、其余 features 和配置完全相同；
6. 启动日志和 `session.json` 同时记录 compiled features、AxVisor binary SHA-256 和选中 path ID；
7. 验证 feature 未编入时与旧行为一致，并验证重新关闭 feature 可以完整恢复 baseline；
8. 分别验证 candidate 1、candidate 2；两项都存在时最后才增加 combined build，不得只测组合后声称每项有效。

feature 名必须表达获选路径，不得永久保留 `candidate1` 这类无语义名称。未被选择的试验代码不得留在生产树中。

### P3-RT-H：完整 production A/B

P4/P5 verified 后扩展为完整 `TEST-009` 矩阵：

| 场景 ID | 固定负载 | 前置 |
|---|---|---|
| `RT-AX-IDLE` | Linux idle | `TEST-007` |
| `RT-AX-CPU` | Linux 固定 CPU stress | `TEST-007` |
| `RT-AX-NET` | P4 固定 network pressure | `TEST-007`、`TEST-011..015` |
| `RT-AX-AI` | P5 固定 AI loop，不额外叠加 CPU stress | `TEST-007`、`TEST-011..018` |
| `RT-AX-COMBINED` | CPU + network + AI | `TEST-007`、`TEST-011..018` |

每个场景、每个 seed 都跑 off/on 配对。为降低时间漂移，顺序固定为：seed 7 `off→on`、seed 19 `on→off`、seed 43 `off→on`。off/on 的 AxVisor binary 因候选 feature 而具有不同 hash；除此之外，源码 revision、target、其余 features、Guest images、TOML、负载 seed、QEMU 参数和采样窗口必须逐项相同并记录 hash。

若有两个候选，每个候选单独运行完整矩阵；combined 只作额外结果。正式优化结论要求：预登记的 candidate primary P99.9 或 max 在三个配对 run 中方向一致改善，其他主指标没有未解释的显著回退，所有功能、安全、生命周期门禁保持通过。

### P3-RT-I：证据发布与状态关闭

每个 run 遵守 `IF-011`：创建新目录，失败也保留；`status.json` 最后写。矩阵聚合目录只引用 immutable run，不复制或改写 raw 数据。

完成后同步：

- `contest-realtime-path.md` 的 as-built 路径、选择与最终 diff；
- `contest-spec/requirements.md` 的 `REQ-RT-001..003`；
- `contest-spec/test-matrix.md` 的 `TEST-008..010`；
- `contest-spec/traceability.md`、`deliverables.md`；
- `development/current/现状.md`、`阻塞.md`、`计划.md`、`开发交接.md`。

不得覆盖历史 EVD，也不得把 `partial` 改成 `verified`，除非本手册第 12 节全部满足。

## 7. 统一时钟和事件 schema

### 7.1 时钟域

P3 禁止使用 wall clock 计算延迟。文件名可以使用 UTC，指标只使用单调时钟。

原生 Zephyr：使用同一 Guest monotonic/counter 域。

AxVisor Guest 内：使用 `CNTVCT_EL0` 或 Zephyr 对它的 64-bit cycle API。

AxVisor Host/EL2：使用 `CNTPCT_EL0` 对应的单调 counter。

跨 EL1/EL2 合并时，`clock.json` 必须记录：

```json
{
  "schema_version": "p3-clock-v1",
  "counter_frequency_hz": 0,
  "host_counter": "CNTPCT_EL0",
  "guest_counter": "CNTVCT_EL0",
  "cntvoff_ticks_by_vcpu": {"2:0": 0},
  "mapping": "host_ticks = guest_ticks + cntvoff_ticks",
  "calibration_samples": [],
  "max_mapping_residual_ns": 0
}
```

所有数值必须来自该 session；示例中的零不是默认值。validator 使用整数运算：先映射 ticks，再以 `floor(ticks * 1_000_000_000 / frequency)` 转换为 `monotonic_ns`。frequency 为零、offset 未绑定 VM/vCPU、映射后时间倒退或校准残差超过预登记阈值时，跨域分段无效；只能保留各端同钟指标。

当前 `CNTVOFF_EL2` 在 Guest 初始化时设置为物理 counter，因此不能假设 Host/Guest 数值天然相等。若无法可靠导出 offset，不得用 console 接收时间减 Host 时间，也不得发布完整 Host→Guest 单向路径。

### 7.2 原始事件

原始记录首选 JSONL。每条必须包含 `IF-010` 的字段：

```text
schema_version, run_id, endpoint, scenario, transport, session_id,
sequence, request_id, sample_index, event, monotonic_ns, value, unit, outcome
```

P3 再增加：

```text
clock_domain, raw_counter_ticks, counter_frequency_hz, clock_mapping_id,
vm_id, vcpu_id, pcpu_id, interrupt_id, path_variant, path_id,
feature_state, trace_sequence
```

不可用字段写 JSON `null`，真实零写 `0`。`monotonic_ns` 必须与 raw counter/clock mapping 可复算；只给格式化字符串或 percentile 不合格。

P3 事件至少覆盖：

- Guest：`period_release`、`period_start`、`period_finish`、`guest_handler_enter`；
- 实际经过的 Host 路径：`timer_expiry`、`virq_enqueue`、`vcpu_wake_request`、`vcpu_wake_complete`、`pending_irq_drain`、`vcpu_reentry`；
- 运行控制：`warmup_end`、`load_start`、`load_stop`、`trace_drop`、`deadline_miss`；
- P4/P5 场景继续保留 `packet_send/receive`、`inference_start/finish`、`control_apply`、`safe_enter/exit`、`feedback_receive`。

没有发生的阶段不生成假事件，而是在 `path.json` 解释 `not_applicable`。事件 join 必须同时匹配 `run_id + vm_id + vcpu_id + sample_index/trace_sequence + path_variant`；只按时间接近或文本顺序猜关联一律失败。

## 8. 指标和统计规则

### 8.1 样本定义

| 指标 | 固定计算 |
|---|---|
| signed jitter | `period_start[i] - period_start[i-1] - period_ns` |
| absolute jitter | `abs(signed_jitter)`；P3 headline 使用 `abs_jitter_ns.p99_9` |
| wakeup latency | 同一 Zephyr 时钟的 `period_start - period_release` |
| execution time | 同一 Zephyr 时钟的 `period_finish - period_start` |
| path segment | 同一映射后 counter 域中相邻真实路径事件之差；缺阶段不计算 |
| deadline miss | `period_finish > next_period_release`，或超过 scenario 中预登记 budget；两种计数分别报告 |

### 8.2 分位数

- 每个 run 独立排序，不把三个 run 拼成一个超大样本。
- P99/P99.9 使用 nearest-rank：排序后索引为 `ceil(p*n)-1`，0-based。
- 同时报 `n`、mean、min、max、P99、P99.9、miss count、drop count 和异常分类。
- mean 使用完整纳秒整数累加后输出十进制；分位数保持整数 ns。
- warm-up、timeout 和失败样本不得静默删除；排除规则与计数写入 summary。
- paired change 同时报绝对 ns 和百分比；baseline 为 0 时百分比写 `null`，不能写无穷或 0%。
- 判定以三个 run 的逐 run 方向为准；aggregate/图表只辅助说明，不能用 pooled mean 掩盖一个失败 run。

### 8.3 预登记主指标

在任何 on-run 开始前，`matrix.json` 必须给每个候选登记唯一 primary metric，取值只能是该候选真实可观测的 `segment.p99_9`、`wakeup_latency_ns.p99_9`、`abs_jitter_ns.p99_9` 或对应 max。结果生成后不得改 primary metric；需要改变时创建新 matrix ID 并重跑 off/on。

## 9. feature、构建配置与回滚合同

### 9.1 测量开关

- 编译 feature：`contest-rt-trace`，default off。
- `virtualization/axvm/Cargo.toml` 定义底层 feature；`os/axvisor/Cargo.toml` 只做 `axvm/contest-rt-trace` 透传。
- 若真实测点位于 `arm_vcpu` 或 `arm_vgic`，由 `axvm` feature 继续显式透传对应 crate 的同名 feature；不得让它们默认开启。
- feature 未编入：不得产生 trace event，不得改变 IRQ、wake、entry 或 timer 行为。
- runner 为 trace-off/trace-on 生成两份 build manifest，并拒绝除该 feature 外的 feature/target/source 差异。

### 9.2 生产优化开关

每个获选候选使用不同的语义化编译 feature；例如获选的 vCPU wake 路径可使用：

```toml
[features]
contest-rt-opt-vcpu-wake = []
```

示例名称不是预先选择 vCPU wake。最终名字必须来自 `selection.json`。所有开关默认关闭；默认构建行为与 P2/P4/P5 已验证基线一致。除非后续决策明确需要 live toggle，否则不要为了 A/B 扩大 `axvmconfig` 的公共 TOML schema；编译 feature 已提供可审计、可删除的关闭路径。

### 9.3 立即回滚条件

任一条件出现时，停止 on-run、在 terminal `status.json` 写 `success=false` 与 `status=realtime_measurement_failed`、用不含对应 feature 的 baseline build 在新目录重跑 off 基线：

- panic、unclassified VM exit/restart、cleanup residual；
- CPU migration、wrong-pCPU entry、错误 IRQ owner；
- deadline miss 增加但无预登记解释；
- Guest memory、网络、AI safe-state 或生命周期合同回归；
- trace drop、时钟映射失效、事件关联不唯一；
- primary metric 三组方向不一致，或其他主指标出现未解释显著回退。

若 feature-off build 仍不能恢复基线，说明改动未被开关完全隔离：回滚整个候选 commit，不允许继续收集“关闭态”数据。

## 10. 验证命令

工具实现后，从仓库根目录执行：

```bash
python3 scripts/test/check_contest_rt_event_schema.py
python3 scripts/test/check_contest_rt_statistics.py
python3 scripts/test/check_contest_rt_candidate_selection.py
python3 scripts/test/check_contest_rt_run_plan.py
cargo fmt --all -- --check
cargo xtask clippy --package axvm
cargo xtask clippy --package arm_vcpu
cargo xtask clippy --package arm_vgic
cargo xtask clippy --package axvisor
```

只执行实际改动 crate 的 clippy，但 `axvm` 与 `axvisor` 的跨层改动必须一起验证。host-testable queue/统计核心优先加入现有 `cargo xtask test` 白名单；若该命令尚未发现新 opt-in test，先证明原因并运行精确 `cargo test -p <crate> --features <feature>`，同时补上统一 runner 覆盖，不能让测试永久隐藏。

正式 AArch64 build 和 QEMU 命令不得在本文硬编码个人缓存路径。P3 runner 必须复用通过 `TEST-006/007` 的 build/QEMU/VM 输入 manifest，并在 `commands.jsonl` 保存解析后的 argv、cwd、exit code 和输入 hash。开发者不得手工拼一个不同拓扑来取得更好数据。

## 11. 每个 run 的工件

```text
results/baseline/runs/<unique-run-id>/
├── session.json
├── inputs.json
├── path.json
├── clock.json
├── commands.jsonl
├── events/
│   ├── zephyr.jsonl
│   ├── axvisor.jsonl
│   └── linux-load.jsonl
├── raw/
│   ├── axvisor.log
│   ├── linux.log
│   └── zephyr.log
├── network/                         # 仅 network/AI/combined 场景
│   ├── traffic.pcap
│   └── capture.json                 # filter、逻辑接口/采集点、方向与 drop
├── metrics.csv
├── summary.json
├── manifest.json
├── cleanup.json
└── status.json
```

`RT-AX-NET`、`RT-AX-AI` 和 `RT-AX-COMBINED` 必须保存与同一 session 绑定的受控 network capture；内部 `vnet0` 没有 Host OS 网卡名时，`capture.json` 显式写逻辑接口、capture point 和 filter，不得用 Host loopback/outer-QEMU 抓包替代。raw log 的 byte 数、首末 sequence、drop 和 DMA-attempt 计数同样进入 `manifest.json`/`summary.json`。

`status.json` 最后写入，并遵守 `IF-011` 的双字段语义：规范化 `success` 只能为布尔值；producer-specific `status` 只能为 `realtime_measurement_completed`、`realtime_measurement_failed` 或 `realtime_measurement_blocked`。只有所有文件在 `manifest.json` 中有 size/SHA-256/producer、validator 通过且 cleanup 无残留时，才允许写 `success=true` 与 completed token；failed/blocked 均写 `success=false`，blocked preflight 另写非空 `blockedReason`。失败或 blocked run 同样保留，后续运行用新 run ID；不得修改失败包凑成功。

矩阵聚合目录另含：

```text
matrix.json
selection.json
paired-results.csv
report.md
recompute-command.txt
checksums.sha256
```

## 12. P3 完成定义

只有同时满足以下条件，才能把 `P3-RT-01` 和 `REQ-RT-001..003` 标为完成：

1. `TEST-008`：原生 Zephyr 三次正式 run 全部满足样本、时长、schema、drop 和统计门禁。
2. `TEST-009`：AxVisor idle、Linux CPU、network、AI、combined 五个场景均完成三组 off/on 配对；network/AI 场景来自已通过的 P4/P5 真实能力。
3. `TEST-010`：候选由机器可复算门禁选择，最多两条；每条有独立 feature/config、最低层回归、strict Clippy/rustfmt、正式 AArch64 build 和完整 A/B。
4. Zephyr 始终固定 pCPU2，Linux 只在 pCPU0/1，Host/console 工作不无界占用 pCPU2；任务优先级、period、budget 和负载都可从输入复现。
5. 预登记 primary metric 在三个配对 run 中方向一致改善，且功能、安全、生命周期和其他主指标没有未解释回归。
6. 所有 raw 数据、统计、源码/config hash、命令、失败记录、cleanup 和 status 遵守 `IF-010/011`，可由一条命令重算。
7. `contest-realtime-path.md` 已从计划更新为 as-built，相关 REQ/TEST/traceability/deliverables 和工作区交接文件同步。

如果没有候选达到选择门槛，则必须完成 `TEST-008` 和 discovery baseline、发布 `decision=no_candidate` 及限制说明，但 `REQ-RT-002`/`TEST-010` 保持未完成；不得将“没有证据支持修改”改写成“实时优化完成”。

## 13. 风险、诊断和责任人

| 风险 | 可观察信号 | 处理/回滚 | owner |
|---|---|---|---|
| passthrough 路径与计划链不一致 | `path.json` 缺 enqueue/wake；源码不经过 dispatcher | 标 `not_applicable`，不伪造；如要换 timer mode，先走 DEC 和 P2 回归 | Hypervisor |
| trace 自身扰动 | trace-on P99.9 超过 off 5%或新增 miss | 减少测点、预分配/批量 drain；校准未过不得正式采集 | Validation |
| 时钟映射错误 | residual 超限、映射后倒退、offset 缺失 | 禁止跨域差值，只保留端点同钟指标，新 run 修复 | Hypervisor + Validation |
| console/log 背压 | ring drop、周期线程阻塞、pCPU2 被 drain 占用 | fail run；drain 移到低优先级/Host pCPU3，不扩大 buffer 掩盖问题 | RTOS |
| WSL2/QEMU 宿主噪声 | 三组方向不一致、Host steal/负载变化 | 保留环境数据并判噪声未分类；不得筛掉坏 run | Validation |
| 候选改动破坏 IRQ/生命周期 | wrong owner、panic、restart、cleanup residual | runtime toggle off；若 off 不恢复则回滚 commit | Hypervisor |
| P4/P5 输入变化 | image/config/load hash 不同 | 新 matrix ID 重跑，旧数据保留但不跨版本拼接 | Network/AI + Validation |
| 无候选达标 | `selection.json decision=no_candidate` | 发布基线/限制，P3 保持未完成并复审场景和工期 | Project owner |

## 14. 允许结论与非结论

可以按证据说：

- “完成 P3 schema/validator 的 host 合同”；
- “完成原生 Zephyr 规范基线”；
- “在指定 revision、QEMU、镜像和场景下观察到某段 P99.9”；
- “候选在三个配对 run 中达到冻结选择门槛”；
- “在完整五场景 A/B 中，预登记指标方向一致改善且门禁无回归”。

不能说：

- 静态 CPU TOML 证明无迁移或 jitter 改善；
- 10 个历史样本、单 run、mean 或 build success 证明实时优化；
- `runtime/dispatcher.rs` 存在就证明 passthrough timer 经历 enqueue/wake；
- WSL2/QEMU 数据是开发板最坏时延上界；
- 早期 idle/CPU baseline 已完成 network/AI production A/B；
- feature 名、代码 diff 或优化意图本身等于性能收益。

这组边界必须原样进入最终报告的 `non_claims`，防止 P3 局部成果被升级为整个项目完成。

## 15. 架构语义参照（非项目验收规范）

- [Arm Generic Timer guide](https://developer.arm.com/-/media/Arm%20Developer%20Community/PDF/Learn%20the%20Architecture/Generic%20Timer.pdf?revision=c710e7a7-9f52-4901-8c9d-91b19f44f9c7)：仅用于核对 Generic Timer counter、offset、timer 寄存器和虚拟计时语义。
- [Armv8-A virtualization guide](https://developer.arm.com/-/media/Arm%20Developer%20Community/PDF/Learn%20the%20Architecture/Armv8-A%20virtualization.pdf?revision=a765a7df-1a00-434d-b241-357bfda2dd31)：仅用于核对 EL2、Guest/Host 上下文与虚拟化计时的架构语义。

两份 Arm 资料都不能替代本项目的 `DEC/REQ/ARC/IF/TEST`、当前源码路径发现和真实 runtime evidence。若通用架构示例与当前 `path.json` 不同，必须以项目冻结合同和 as-built path discovery 的更严格边界为准；不得用通用手册中存在某条路径，推导当前 passthrough 配置已经过该路径。
