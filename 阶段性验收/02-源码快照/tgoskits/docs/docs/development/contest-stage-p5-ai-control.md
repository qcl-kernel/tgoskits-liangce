# P5-AI-01 实现级开发文档：Linux MLP 与 Zephyr 安全闭环

状态：实现方案已冻结；**P5-AI-A 已完成（2026-08-20）**：canonical `3→8→1` float32 模型冻结（`apps/contest/linux-ai-controller/model/`，model.bin sha256 `2ba04889…a46d50f`）、Linux 部署 C 推理已实现（`src/model/contest_model.c`，host 编译对 256 golden 全部一致、aarch64-musl target build 通过、`CONTEST_AI_GOLDEN_C_PASS` 接入 CI）、`ai-verify` 模式就位。host 数据/模型/训练/导出/验证、plant/PI/mailbox/watchdog/metrics 工具已实现并通过合同；**B（Linux 真推理→CONTROL、Zephyr 真 action/STATUS）、C（AI-loop）、Q（对照/安全态）尚未完成**（2026-08-20）。工作包拆为 `P5-AI-A/B/C/Q`；需求：`REQ-AI-001..003`；接口：`IF-005..010`、`IF-011`；验收：`TEST-016..018`。

## 0. 2026-08-20 并行准入与任务拆分

原计划要求 TEST-011..015 全部 verified 后才进入任何 P5 Guest 开发，过度串行且会浪费已取得
的真实 happy-path 链路。现在冻结为：实现可以并行，结论不能越级。

| 子包 | 何时可开始 | 输出 | 关闭门禁 |
|---|---|---|---|
| P5-AI-A | 立即（host/target static） | canonical `3→8→1` model、metadata/dataset/golden/checksums；Linux C 推理 | host 与 Linux target 对全部 golden vectors 一致，模型 hash 固定 |
| P5-AI-B | P4-UPSYNC-02 接口冻结后 | Linux MLP→CONTROL；Zephyr mailbox/plant/watchdog/真实 STATUS；去掉占位 `0x44` | target build；网络线程不阻塞 100 ms 周期；唯一 actuator owner；500 ms safe state |
| P5-AI-C | P4-SMOKE-02 + A/B | 单一 session happy-path input→inference→IP→action→feedback | identity/model/request/sample 关联完整；只称 AI-loop smoke |
| P5-AI-Q | P4-REL-01 + AI-C | 三 seed、固定 PI 对照、fault/restart、安全态与恢复 | TEST-016..018 全 oracle、至少两指标、失败包/cleanup 完整 |

P4 当前 ICPC `85/100` 只允许开发 A/B；不能关闭 AI-C/Q。Host model、分别启动两应用、
占位 STATUS 或 console marker 都不能作为 AI-loop。

本文是 P5 的实施入口。唯一 v1 方案是 Linux Guest 内运行 `3→8→1` float32 MLP，经真实 Guest UDP/IP 发送量化 CONTROL，Zephyr 每 100 ms 应用动作、更新一阶温度对象并回传 STATUS；失去有效控制 500 ms 后，Zephyr 独立进入 `duty=0` 安全态。数值合同以 [`contest-spec/contracts.md`](contest-spec/contracts.md) 为最高实现依据。

## 1. 前置门禁

直接依赖文档：[`contest-stage-p4-network.md`](contest-stage-p4-network.md)。P4 是 P5 Guest runtime 的强制前置，不允许用 host socket 或模拟网络替代。

P5-AI-C/Q 只在以下条件满足后进入相应 Guest runtime；P5-AI-A/B 按第 0 节可提前：

1. AI-C 要求 `P4-UPSYNC-02`、`P4-EVID-01`、`P4-SMOKE-02` 已关闭；AI-Q 额外要求 `P4-REL-01`，即 `TEST-011..015` 在同一官方 axvirtio/device-graph 生产基线、当前代码和当前镜像上为 `verified`。
2. AI-C 至少要求当前新 HEAD 的 UDP `46000`/ICPC happy path、identity/DTB/capture 证据；AI-Q 还要求 TCP `46001` 回退、PCAP/metrics、fault 和 session restart 的完整 `L7 Guest-IP` qualification。
3. P4 创建的两端应用网络骨架能在目标 Guest 构建并持续运行。
4. outer-QEMU 无 NIC，主链路确实经过 AxVisor mediated `vnet0`。
5. 开发者已完整阅读仓库 `AGENTS.md`、`docs/guideline/code-quality.md` 和 `docs/guideline/feature-development.md`。
6. 禁止创建或提交 PR；完成内容按私有比赛仓库分支与离线 `git format-patch` 审查包规则交付，不直接推送 upstream。

模型/data 生成、plant/PI 的纯 host 开发可以提前并行，但不得在 P4 通过前宣称 Guest AI 或端到端闭环。

### 1.1 非结论

模型文件、host Python/C 推理、Zephyr 本地 plant、host socket、P4 的 ICPC stub 或分别运行的两端应用都不是 `L8 AI-loop`。只有同一真实双 Guest session 中可关联的 input→inference→IP→action→feedback 才能关闭 P5；P5 也不自动证明 P3 实时 A/B 或 P6 综合长稳。

出现以下情况时停止并保留失败证据：

- 需要用 host Python 推理、共享内存或 console 代替 Linux Guest 推理/IP；
- Zephyr 高优先级周期线程需要等待 socket、virtqueue、日志、文件或模型；
- payload、单位、plant、MLP shape、100 ms 周期或 500 ms watchdog 与冻结合同不一致；
- MLP 故障后静默调用 PI，却仍记录为 `mode=MLP`；
- 不同步 Guest 的时间戳被相减为单向延迟；
- 为展示“AI 更优”而事后换 seed、窗口、扰动、指标或丢弃失败样本。

### 1.2 规范与上游参照

P5 不需要开发者再选择新的模型、plant、RTOS 或传输技术路线。plant 方程、PI、数据生成、`3→8→1` MLP、float32/量化、payload、100 ms/500 ms、seed、指标和 A/B 判定均以 [`contest-spec/contracts.md`](contest-spec/contracts.md) 为唯一数值规范；框架、编译器、libm 或 Guest OS 文档只用于实现兼容，不能改变数值合同。若某项在锁定工具上无法逐字实现，应以失败证据回到 DEC/IF 复审，而不是现场换网络、模型或阈值。

## 2. P5 完成后的唯一闭环

```text
Zephyr tick i
  -> apply newest valid CONTROL at period boundary
  -> update integer temperature plant
  -> STATUS(sample_index=i, measured, target, applied_request, duty)
  -> ICPC v1 / Guest UDP / mediated vnet0
  -> Linux validates STATUS
  -> normalize 3 inputs
  -> 3x8x1 float32 MLP or explicit fixed PI baseline
  -> quantize duty_q16_16
  -> CONTROL(request_id, mode, model_version, validity=500)
  -> ICPC v1 / Guest UDP / mediated vnet0
  -> Zephyr validates and publishes into one-element mailbox
  -> next period boundary applies once
  -> later STATUS confirms applied_request_id
```

Linux 是模型和会话 owner；Zephyr 是 actuator、plant 和安全态 owner。Hypervisor 不解析 CONTROL/STATUS/ERROR 的业务语义。

## 3. 冻结业务合同

### 3.1 周期、对象与场景

| 项目 | 固定值 |
|---|---:|
| tick / Linux decision cadence | `100 ms` |
| qualification duration | `180 s = 1800 ticks` |
| initial temperature | `25000 mC` |
| ambient temperature | `25000 mC` |
| target | `55000 mC` |
| safe duty | `0` |
| watchdog | `500 ms` from last valid new APPLY_OUTPUT |
| heat-loss time constant | `10 s` |
| max heater rate | `4000 mC/s` |
| disturbance | ticks `600..899`: `-150 mC/tick` |
| qualification seeds | `7`、`19`、`43` |

每个 tick `i` 的顺序固定：应用本周期新命令 → 读取实际 duty → 计算 loss/heater/disturbance → 更新温度 → 发布 `STATUS.sample_index=i` → `i += 1`。正式 180 s run 必须恰有 sample index `0..1799`，不能多发或少发一个 plant sample。

### 3.2 MLP

- 输入按顺序固定为 `{measured_mC, target_mC, previous_duty_q16_16}`；`previous_duty` 必须来自同一条 STATUS 的当前实际 duty，不得使用 Linux 上一次发送但尚未确认的值。
- shape 固定 `3→8→1`；hidden=ReLU，output=sigmoid；权重、bias 和推理均为 IEEE-754 float32。
- 输出量化为 `0..65536` 的 `duty_q16_16`，最近值、恰好半值向上。
- 查表、固定公式、PI teacher 或常量不能以 `mode=MLP` 运行。

归一化固定为：

```text
x0 = clamp((measured_mC - 25000) / 40000, -1, 1)
x1 = clamp((target_mC   - 25000) / 40000, -1, 1)
x2 = clamp(2 * previous_duty_q16_16 / 65536 - 1, -1, 1)
```

### 3.3 payload

所有字段为紧凑大端字节序，禁止直接发送有 padding 的 C struct。

CONTROL 固定 24 字节：

| offset | 字段 | 规则 |
|---:|---|---|
| 0 | `schema_version:u8` | `1` |
| 1 | `command:u8` | 1 APPLY_OUTPUT / 2 ENTER_SAFE / 3 SET_TARGET |
| 2 | `control_mode:u8` | 0 FIXED / 1 MLP |
| 3 | `reserved0:u8` | `0` |
| 4 | `request_id:u32` | 非零、session 内单调递增 |
| 8 | `duty_q16_16:i32` | APPLY 时 `0..65536` |
| 12 | `target_mC:i32` | v1 qualification 为 `55000` |
| 16 | `model_version:u32` | MLP 非零；FIXED/SAFE 为 0 |
| 20 | `validity_ms:u16` | 必须 `500` |
| 22 | `reserved1:u16` | `0` |

STATUS 固定 32 字节：schema/mode/health、`applied_request_id`、`sample_index:u64`、measured、target、duty、control error。ERROR 固定 12 字节：schema/subsystem/detail、related request、diagnostic token。完整字段和错误枚举见 `IF-008`。

## 4. 目标文件清单

P4 已建立目录时在原目录扩展；不存在时按本表新建。当前 `scripts/contest/ai/` 的 dataset/model/train/export/verify/reference/metrics 与 portable ICPC payload codec 已有 host 合同；它们是可复用准备，不是 canonical model、Guest 应用或闭环 runner。下表的 Guest 路径仍须实现，已有文件必须原位维护，不能复制第二套 codec/模型格式。

```text
scripts/contest/icpc/
  icpc_control.h                         # 已有：24/32/12-byte payload API
  icpc_control.c                         # 已有：逐字段大端 codec/validator
  test_icpc_control.c                    # 已有：golden/negative vectors

apps/contest/linux-ai-controller/
  Makefile
  README.md
  include/
    controller.h
    model.h
    session.h
    event_log.h
  src/
    main.c                               # CLI、生命周期、退出码
    session.c                            # UDP/TCP/ICPC、request/feedback 关联
    controller.c                         # mode 边界、clamp、fallback
    fixed_pi.c                           # 冻结 PI
    model.c                              # 3x8x1 float32 部署推理
    event_log.c                          # IF-010 JSONL
  model/
    model.bin                            # canonical little-endian float32
    metadata.json
    dataset-manifest.json
    golden-vectors.json
    checksums.sha256
  tools/
    generate_dataset.py
    train_export.py
    verify_model.py
    compute_metrics.py
  tests/
    test_model.c
    test_fixed_pi.c
    test_session.c
  requirements.in
  requirements.lock                     # Python/NumPy 精确版本与 hashes

apps/contest/zephyr-control/
  CMakeLists.txt
  prj.conf
  app.overlay
  include/
    plant.h
    control_mailbox.h
    endpoint.h
    event_log.h
  src/
    main.c
    plant.c                              # 纯整数、可 host test
    control_mailbox.c                    # 单元素 latest-valid command
    endpoint.c                           # 低优先级 socket/ICPC/session
    event_log.c                          # 有界事件输出
  tests/
    test_plant_host.c
    test_watchdog_host.c

configs/contest/ai/
  qualification-v1.json                 # 1800 tick、band/window、seeds
  faults-v1.json                         # TEST-018 固定注入时间线

scripts/contest/ai/
  prepare_linux_ai_rootfs.py             # 新建：binary/model/config 写入副本
  prepare_zephyr_control_image.py         # 新建：锁定 Zephyr/SDK build + manifest
  run_closed_loop.py                     # 新建：TEST-017/018 唯一入口
  validate_ai_session.py                 # 新建：关联、指标、IF-011
  render_control_report.py               # 新建：只从 raw 重建表/图

scripts/test/
  check_icpc_control_payload.py           # 已有：payload host 合同
  check_contest_ai_model.py               # 已有：dataset/model/golden 合同
  check_contest_zephyr_control.py         # 已有：plant/watchdog fixture
  check_contest_ai_runner.py              # 新建：runner/status-last fixture
```

现有 host/static 准备还包括 `scripts/contest/ai/{generate_dataset.py,model.py,train_export.py,verify_model.py,reference.py,compute_metrics.py}` 及其 dataset/reference/metrics 合同。P5 开始时先把这些输出按本节 schema 冻结并迁入 `apps/contest/linux-ai-controller/model/`；不得把临时 host 输出直接称为 Guest canonical model。

完整训练数据和 runtime raw log 可以 ignored，但生成器、manifest、canonical model、metadata、golden vectors、validators 和脱敏摘要必须进入可交付集合。

## 5. 先实现 portable payload codec

`icpc_control.c/.h` 与现有 `icpc.c/.h` 同为 portable C99，无动态分配、无平台 socket 依赖。API 分别接受 byte buffer 与显式长度，提供 CONTROL/STATUS/ERROR encode/decode；未知 schema/enum/health bit、非零 reserved、长度不精确、范围错误和组合错误全部返回 typed status。

先写能让空实现失败的测试：

- 三种 payload 的逐字节 golden vectors；
- 最大/最小 duty、负温度、`u64 sample_index` 大端；
- 长度少 1/多 1、未知 command/mode/subsystem/detail；
- `request_id=0`、MLP `model_version=0`、FIXED model 非零；
- `validity_ms=499/501`、target 非 55000、reserved 非零；
- STATUS 未知 health bit、duty 越界、error 算术溢出；
- ERROR header error_code 为零或 payload detail 非法。

host 门禁：

```bash
python3 scripts/test/check_icpc_protocol.py
python3 scripts/test/check_icpc_control_payload.py
```

两端应用只能调用该 codec 或由同一黄金向量验证的薄适配层，禁止复制字段偏移。

## 6. Zephyr plant：纯整数、可逐 tick 复核

### 6.1 参考算法

`plant.c` 使用 `int64_t` 中间值，不能依赖实现定义的溢出。逻辑等价于：

```c
/* C99 signed division truncates toward zero. duty is already 0..65536. */
loss_mC = (25000 - temperature_mC) / 100;
heater_mC = (400 * duty_q16_16 + 32768) / 65536;
disturbance_mC = (tick_index >= 600 && tick_index <= 899) ? -150 : 0;
next_mC = temperature_mC + loss_mC + heater_mC + disturbance_mC;
```

在赋回 `int32_t` 前检查 `next_mC` 是否位于 `[-40000,125000]`。超界时：

1. 本 tick 不提交 wrap 后温度；
2. duty 归零并进入 SAFE；
3. 生成 `RUNTIME/INTERNAL_OVERFLOW`；
4. 后续只能由新 session 的有效 APPLY_OUTPUT 恢复。

host golden test 至少覆盖 tick 0、599、600、899、900，duty 0/32768/65536，负除法向零，边界温度和溢出。`TEST-017` 使用的 Python reference 必须逐 tick 与 C plant 一致。

### 6.2 sample 语义

- 初始 `temperature_mC=25000`、`sample_index=0`、`duty=0`、`SAFE=1`。
- 第一个 100 ms release 执行 tick 0，并在更新后发布 `STATUS.sample_index=0`。
- 1800 次 release 后最后一条为 `sample_index=1799`。
- STATUS 的 `control_error_mC` 用饱和的 `target-measured` 计算；溢出必须作为错误显式记录。

## 7. 固定 PI 基线

PI 只在 Linux 运行，并与 MLP 走完全相同的 STATUS→CONTROL→ACK→feedback 路径：

```text
e_C = (target_mC - measured_mC) / 1000
Kp = 0.025 duty/C
Ki = 0.005 duty/(C*s)
I_candidate = clamp(I + 0.1 * e_C, -100, 100)
u_candidate = Kp*e_C + Ki*I_candidate
```

若 `u_candidate>1 && e_C>0` 或 `u_candidate<0 && e_C<0`，拒绝本次积分并以旧 `I` 重算；否则提交 `I_candidate`。最终 `u=clamp(u_candidate,0,1)`，按最近值、恰好半值向上量化为 Q16.16。

canonical teacher 与部署 `fixed_pi.c` 都使用 IEEE-754 binary64、严格按上述表达式顺序逐项求值，不启用 fast-math/FMA contraction；量化实现为非负值 `floor(u*65536.0 + 0.5)`。generator 必须用逐 tick golden vector 证明 Python teacher 与 C teacher 的 Q16.16 输出完全相同，不能只比较浮点近似。

以下事件把积分清零：进入 SAFE、target 变化、ICPC session 变化、transport 切换、controller mode 切换。测试必须覆盖 anti-windup、上下饱和、半值量化和每一种 reset 原因。

## 8. 数据生成、训练与模型导出

### 8.1 数据生成器

`generate_dataset.py` 必须逐字实现 `IF-008` 的 PCG-XSH-RR 64/32，不调用 Python/NumPy 全局随机数。train/validation/test seed 分别是 `7/19/43`，每 split 64 个 1800-tick episode，共 115,200 样本。

每 episode 丢弃 PCG 第一个 output，再依次取 4 个 `u32`：

```text
initial_mC       = 25000 + (r0 mod 15001)
target_mC        = 45000 + (r1 mod 15001)
disturbance_tick = 300 + (r2 mod 601)
duration_ticks   = 300
disturbance_mC   = -200 + (r3 mod 301)
```

输入在 PI 计算前记录，label 在 PI 计算后记录；previous duty 和积分初值为 0。样本顺序固定为 episode-major、tick-minor。输出使用 canonical little-endian binary 加 JSON schema；每个 split 的 size/SHA-256 进入 `dataset-manifest.json`。

### 8.2 训练环境

- Python 固定为 `3.12.11`；NumPy 固定为 `2.2.6`，最终 interpreter、wheel/source hash 写入 environment manifest 与 `requirements.lock`。
- 设置 `PYTHONHASHSEED=0`、`OMP_NUM_THREADS=1`、`OPENBLAS_NUM_THREADS=1`、`MKL_NUM_THREADS=1`。
- 只在 CPU 单线程执行，所有训练张量为 float32；禁止 GPU、TF32、mixed precision 和 early stopping。
- Adam，learning rate `0.001`，batch `256`，MSE，恰好 `200` epoch。
- 每个 epoch 都按 canonical episode-major、tick-minor 顺序读取 train split，并切成连续的 256-sample batch；115,200 个 train sample 恰为每 epoch 450 batch，禁止 shuffle、drop 或 validation-driven 选择。
- requirements、Python、NumPy、BLAS 和 CPU 信息进入 model metadata。

为避免“同为 Adam”仍产生不同模型，`train_export.py` 固定为 NumPy 内显式 forward/backward/Adam，不引入 PyTorch/TensorFlow/JAX：

1. 初始化随机流复用第 8.1 节 PCG32，独立 seed 为十进制 `20260813`，初始化后丢弃第一个 output。
2. bias 全零。依次为 row-major `W1[8][3]`、`W2[1][8]` 取 32 个 `u32 r`；`u=(r+0.5)/4294967296.0`，再分别映射到 Xavier uniform `[-sqrt(6/(3+8)),+sqrt(6/(3+8))]` 与 `[-sqrt(6/(8+1)),+sqrt(6/(8+1))]`，最后转换为 float32。
3. forward 顺序固定为 `z1=x@W1.T+b1`、ReLU（恰为 0 的导数取 0）、`z2=h@W2.T+b2`、`sigmoid=1/(1+exp(-z2))`；loss 是 batch 全样本的 mean squared error，backward 使用其 `2/N` 梯度。
4. Adam 固定 `beta1=0.9`、`beta2=0.999`、`epsilon=1e-8`、无 weight decay、无 AMSGrad；参数、梯度、一二阶矩保持 float32，global step 每 batch 加 1，在每次更新时做标准 bias correction。
5. 参数遍历/更新顺序固定 `W1,b1,W2,b2`；每个 batch 后检查参数、moment、loss 全部 finite。禁止 gradient clipping、loss scaling、正则项、学习率调度或自动融合。

训练脚本还要提交 initialization hash、epoch 0/1/200 loss、首末 batch gradient hash 和最终 41 参数 hash 的黄金摘要；这些值在第一次锁定平台成功生成后写入 metadata/测试 fixture，后续漂移直接失败。

如果锁定平台重跑不能产生相同 canonical model bytes，`TEST-016` 失败；不能只放宽 hash 为数值近似。

### 8.3 导出

`model.bin` 顺序固定：

```text
W1[8][3], b1[8], W2[1][8], b2[1]
```

共 `24 + 8 + 8 + 1 = 41` 个 little-endian float32，即 164 bytes。完整 SHA-256 是模型身份；`model_version` 取 SHA-256 前 4 bytes 按大端解释，若为 0 则取后 4 bytes，仍为 0 时导出失败。

`metadata.json` 至少包含 schema、shape、activation、normalization、quantization、dataset hashes、training parameters、tool versions、model size/hash/version 和生成命令。JSON 使用 UTF-8、sorted keys、LF 和末尾换行，保证 canonical bytes。

golden vectors 至少包含：

- 归一化上下边界与 clamp 外输入；
- duty 0、32768、65536；
- 初始、目标、扰动和安全态代表点；
- seed 43 test split 中固定抽取的 256 个样本。

Python reference 与部署 C 的每个输出相差不得超过 2 个 Q16.16 LSB；任何 NaN/Inf 或输出越界都失败。

### 8.4 TEST-016 命令

```bash
python3 -m pip install --require-hashes -r apps/contest/linux-ai-controller/requirements.lock
python3 apps/contest/linux-ai-controller/tools/generate_dataset.py --output tmp/contest/ai-data
python3 apps/contest/linux-ai-controller/tools/train_export.py --data tmp/contest/ai-data --output apps/contest/linux-ai-controller/model
python3 apps/contest/linux-ai-controller/tools/verify_model.py --model apps/contest/linux-ai-controller/model/model.bin --metadata apps/contest/linux-ai-controller/model/metadata.json --golden apps/contest/linux-ai-controller/model/golden-vectors.json
python3 scripts/test/check_contest_ai_model.py
```

同一 clean directory 重跑一次并比较 model bytes/hash，之后才可把 model 注入 Linux rootfs。

## 9. Linux controller 实现

### 9.1 固定 CLI

```text
linux-ai-controller
  --bind 10.77.0.1
  --peer 10.77.0.2
  --udp-port 46000
  --tcp-port 46001
  --mode fixed|mlp
  --model /opt/tgos/model.bin
  --metadata /opt/tgos/metadata.json
  --run-id <runner-issued-id>
  --event-log /run/tgos/linux-events.jsonl
```

所有 v1 值仍由编译期合同验证；CLI 只允许显式传入相同值，不提供隐藏第二套默认。MLP 模式启动时先验证 size/hash/model_version/全部 finite；失败则退出或显式进入 FIXED/SAFE，并输出 mode boundary，不能继续标记 MLP。

### 9.2 状态机

1. 创建新的非零 `session_id`，绑定固定 IP/peer/UDP port。
2. 收到 STATUS 后验证 ICPC、payload、session、sequence、sample monotonic、target 和 duty。
3. 对第一次出现的 `sample_index` 记录 `input_receive`，使用该 STATUS 的 measured/target/duty 计算一次 controller。
4. 记录 `inference_start/finish`，clamp/量化，分配新的非零 `request_id`。
5. 发送 ACK_REQUIRED CONTROL，并记录 first/last send；重传保持相同 session/sequence/request。
6. 收到匹配 ACK 只关闭 transport wait；收到后续 `applied_request_id` 匹配的 STATUS 才记录 `feedback_receive` 和闭环成功。
7. 重复 STATUS 不重复推理；乱序/旧 sample 记录并拒绝。
8. CONTROL 在 `t=0/100/300 ms` 发送；500 ms 未 ACK 时取消旧消息、关闭旧 UDP session并进入显式 TCP fallback。
9. 进程退出前尽力发送 ENTER_SAFE，但 Zephyr watchdog 不依赖该包。

进程启动时一次性分配 buffer 和加载模型；每次推理不进行 heap allocation，不调用 shell，不写同步磁盘。event log 经有界队列交给低优先级 logger；队列丢失必须计数，发生关键事件丢失的 run 不能通过。

### 9.3 构建

```bash
make -C apps/contest/linux-ai-controller test CC=gcc
make -C apps/contest/linux-ai-controller clean all \
  CC=/opt/aarch64-linux-musl-cross/bin/aarch64-linux-musl-gcc
```

目标 binary 必须静态链接或把全部动态依赖及 hash 写入 rootfs manifest。`prepare_linux_ai_rootfs.py` 只修改新的 disposable rootfs 副本，写入 binary、model、metadata 和启动配置；禁止修改缓存源镜像。

### 9.4 Linux AI rootfs 制备入口

planned CLI 的参数名固定如下。每个 qualification/fault session 都从 P4 manifest 绑定的同一基准 ext4 创建新副本；即使 binary/model 相同，也不能让两个 run 共用可写 rootfs：

```bash
repo="$(git rev-parse --show-toplevel)"
p4="$repo/results/baseline/runs/<P4-TEST-015-success-run>"
mode="mlp"                         # fixed 或 mlp
seed="43"                          # 7、19 或 43
run_id="phase5-ai-${mode}-s${seed}-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 HEAD)"
prepared="$repo/tmp/contest/p5-prepared/$run_id"
p4_source_rootfs="<absolute-linux-rootfs-bound-by-$p4/manifest.json>"

python3 "$repo/scripts/contest/ai/prepare_linux_ai_rootfs.py" \
  --repository "$repo" \
  --from-network-session "$p4" \
  --source-rootfs "$p4_source_rootfs" \
  --source-manifest "$p4/manifest.json" \
  --controller-binary "$repo/apps/contest/linux-ai-controller/build/linux-ai-controller" \
  --model "$repo/apps/contest/linux-ai-controller/model/model.bin" \
  --metadata "$repo/apps/contest/linux-ai-controller/model/metadata.json" \
  --model-checksums "$repo/apps/contest/linux-ai-controller/model/checksums.sha256" \
  --qualification-profile "$repo/configs/contest/ai/qualification-v1.json" \
  --controller-mode "$mode" \
  --seed "$seed" \
  --bind-ip 10.77.0.1 \
  --peer-ip 10.77.0.2 \
  --udp-port 46000 \
  --tcp-port 46001 \
  --run-id "$run_id" \
  --output-rootfs "$prepared/linux-ai.ext4" \
  --output-controller-config "$prepared/linux-controller.json" \
  --output-manifest "$prepared/linux-ai-rootfs.json"
```

preparer 必须验证 `mode/seed/profile/model_version` 组合、静态链接/动态依赖、source 前后 hash、ext4 中实际文件 hash 和启动 argv；输出存在、source manifest 不含该 rootfs、或任何目标路径是 symlink/reparse point 时失败。FIXED run 仍装入同一 model bytes 以保持 rootfs 内容可比，但启动 config 必须明确 `mode=fixed`，运行时不得加载或调用模型。

## 10. Zephyr controller 实现

### 10.1 线程与数据边界

固定三个职责：

| 线程 | Zephyr preempt priority | 允许工作 | 禁止工作 |
|---|---:|---|---|
| plant/control | `1` | 100 ms release、mailbox snapshot、watchdog、整数 plant、STATUS snapshot | socket、ICPC parse、阻塞日志、heap |
| network | `5` | RX/TX、ICPC/payload 校验、ACK/ERROR、发布 latest command | plant 更新、无界 retry |
| logger | `7` | 从有界 ring 输出事件 | 反压周期线程 |

`CONFIG_NUM_PREEMPT_PRIORITIES` 必须至少为 8。周期 release 使用单调 timer；事件时间由 `k_cycle_get_64()` 经锁定频率转换为 ns，转换方法和频率进入日志。wall clock 不参与控制或指标。

`control_mailbox` 是容量 1 的 latest-valid mailbox。网络线程完成 CRC/session/sequence/schema/request/range/validity 全部验证后，在短 `k_spinlock` 临界区发布不可变 command snapshot；周期线程 nonblocking 读取。新 command 只覆盖尚未应用且更新的 request；重复包只重发 ACK，不重发动作。

### 10.2 100 ms tick

周期线程每次 release：

1. 记录 `period_release/start`；
2. 若 mailbox 有当前 session 的新有效 APPLY_OUTPUT，在周期边界应用一次并记录 session/sequence/request/sample；
3. 若收到 ENTER_SAFE 或 watchdog pending，则在 plant 计算前置 duty=0；
4. 运行第 6 节整数 plant；
5. 生成本 tick STATUS snapshot，投递到有界 network TX mailbox；
6. 记录 `period_finish`；若 finish 越过下一 release，设置 DEADLINE_MISS、增加 counter，不能静默丢 tick。

network TX 取不到 buffer时不得阻塞周期线程；它保留最新 STATUS，增加 overwritten/drop counter。资格 run 如缺少任一 1800 个 plant sample 失败。

### 10.3 500 ms 安全态

- boot、network-ready、session change 和 reconnect 默认 SAFE/duty=0。
- 只有新的、完全有效的 APPLY_OUTPUT 刷新 watchdog；ACK、HEARTBEAT、STATUS、ERROR、SET_TARGET、ENTER_SAFE、重复/旧/坏 CONTROL 均不刷新。
- watchdog 使用接收端本地 monotonic time，从最近有效新 APPLY_OUTPUT 接收时刻计龄。
- 到 `500 ms` 时设置 pending-safe；最迟在紧随其后的 100 ms tick 开始前 duty=0，并置 `SAFE|NETWORK_TIMEOUT`。
- 恢复只接受当前 transport 的新非零 session 中有效新 APPLY_OUTPUT；旧 session 即使 CRC 正确也不能退出 SAFE。

host 虚拟时钟测试必须覆盖 499/500/501 ms、恰逢 tick、两个 tick 之间、重复包、700 ms 旧重传和 session restart。

### 10.4 Zephyr build

最终 `prj.conf` / `.config` 必须启用固定地址所需的 VirtIO Ethernet、IPv4、ARP、ICMP、UDP、TCP 和线程/计时能力，并禁用 DHCP/IPv6。唯一 planned 制备入口固定为：

```bash
repo="$(git rev-parse --show-toplevel)"
export PATH=/root/.cache/tgoskits-contest/zephyr-4.4.0-venv/bin:$PATH
export ZEPHYR_BASE=/root/.cache/tgoskits-contest/zephyrproject-4.4.0/zephyr
export ZEPHYR_SDK_INSTALL_DIR=/root/.cache/tgoskits-contest/zephyr-sdk-1.0.1

python3 "$repo/scripts/contest/ai/prepare_zephyr_control_image.py" \
  --repository "$repo" \
  --zephyr-base "$ZEPHYR_BASE" \
  --zephyr-revision 684c9e8f32e4373a21098559f748f06915f950c9 \
  --west-manifest-sha256 9c3661dd82e5ab7f487e3c0a4eee8726978736eadb470a3a09a76c77a8f10f92 \
  --zephyr-sdk-dir "$ZEPHYR_SDK_INSTALL_DIR" \
  --zephyr-sdk-version 1.0.1 \
  --west-version 1.5.0 \
  --board qemu_cortex_a53 \
  --app "$repo/apps/contest/zephyr-control" \
  --network-config "$repo/configs/contest/zephyr/qemu-cortex-a53-vnet0.conf" \
  --network-overlay "$repo/configs/contest/zephyr/qemu-cortex-a53-vnet0.overlay" \
  --qualification-profile "$repo/configs/contest/ai/qualification-v1.json" \
  --build-dir "$repo/tmp/contest/p5-zephyr/build" \
  --output-dir "$repo/tmp/contest/p5-zephyr/image" \
  --output-manifest "$repo/tmp/contest/p5-zephyr/zephyr-control-build.json"
```

wrapper 必须等价执行 `west build -p always -b qemu_cortex_a53`。保存 `zephyr.bin`、`zephyr.elf`、`.config`、`zephyr.dts` 和全部 hash。validator 必须从最终 `.config`、DTS 与 P4 resolved graph 证明驱动、静态 IP、实际 Auto MMIO/INTID 逐字段一致且无 outer NIC；不得预写旧 `0x0a000400`/INTID 50，只检查 source `.conf` 也不足。正式 run 只能引用上述 manifest，不能直接拿 build 目录中一个未绑定的 `zephyr.bin`。

## 11. 模式、协议与故障行为

### 11.1 正常模式

- FIXED 和 MLP 分别作为独立 run 启动，不能在一条资格曲线中拼接两种 controller。
- MLP CONTROL 使用非零且匹配 manifest 的 model_version；FIXED 使用 0。
- Zephyr 每 100 ms 发布 STATUS；Linux每个新 STATUS 最多发起一个新 CONTROL。
- CONTROL 的 ACK 与 STATUS feedback 是两个门禁；只有 feedback 确认动作实际应用。

### 11.2 MLP 故障

model 缺失、hash/version 错、任一 weight/intermediate/output 非 finite 或推理失败时，Linux必须：

1. 不发送标记为 MLP 的 APPLY_OUTPUT；
2. 发送 MODEL ERROR 或 ENTER_SAFE；
3. 若显式配置允许 fallback，建立清晰 mode boundary、清零 PI integral、用新的 request 进入 FIXED；
4. raw event 和 summary 把后续样本归入 FIXED，不把整段写成 MLP。

### 11.3 网络/协议故障

- CRC/schema/range/model/target/validity 错误：不应用、不刷新 watchdog，返回 ERROR 或带 health 的 STATUS；
- duplicate CONTROL：重发 ACK，动作执行次数不变；
- stale/ambiguous sequence 或 request 回退：拒绝；
- UDP CONTROL 到 500 ms：取消旧重传，Zephyr safe，随后才用新 session 回退 TCP；
- Linux crash/网络停止：Zephyr 周期任务继续，最迟下一 tick 安全化；
- Zephyr endpoint restart：新 session、SAFE 起步，Linux 旧窗口全部作废。

## 12. A/B qualification profile

`configs/contest/ai/qualification-v1.json` 在首次正式运行前提交并冻结以下值：

- controller：`fixed`、`mlp`；
- seeds：`7`、`19`、`43`；每个 seed 两种 controller 共用同一 fault/scheduling manifest；
- 1800 ticks，无 warm-up 丢弃，无事后样本删除；
- target error band：`±1000 mC`；settling hold：连续 `50 ticks`（5 s）；
- initial settling 从 tick 0 计算；若 60 s disturbance 前不能保持 50 ticks则为 `null/not-settled-before-disturbance`；
- disturbance recovery 从 tick 900 计算，使用同一 band/hold；
- timeout、丢包、缺 sample 和 safe tick 均保留在失败分母。

每个 controller/seed 都是独立新 evidence session，不能把两种模式或多个 seed 合并成一个虚构 run。

### 12.1 指标定义

| 指标 | 固定计算 |
|---|---|
| RMSE | `sqrt(sum(error_mC^2)/N)`，使用全部 1800 个 Zephyr sample |
| IAE | `sum(abs(error_mC))*0.1`，单位 `mC*s` |
| MAE | `sum(abs(error_mC))/N`，单位 `mC` |
| overshoot | `max(0, max(measured_mC)-55000)`，同时报告 mC 与相对 30000 mC step 的百分比 |
| initial settling | 首个满足 band 且连续 50 tick 的窗口起点；未满足为 null |
| recovery settling | 从 tick 900 起首个连续 50 tick 的窗口起点减 90 s；未满足为 null |
| closed-loop RTT | Linux 同钟：input STATUS receive 到匹配 feedback STATUS receive；timeout 进入失败分母 |
| action latency | Zephyr 同钟：CONTROL decode 完成到实际 apply |
| success rate | 有匹配 feedback 的逻辑 request / 所有发起 request；重传不加分母 |

percentile 使用提交的统计脚本固定算法并在 summary 写明；原始事件不能只留下汇总。

只有三组配对结果都满足以下条件，才可额外声称“智能优化有效”：

1. MLP 的 RMSE `<=` 同 seed FIXED；
2. MLP 的 IAE `<=` 同 seed FIXED；
3. 每个 seed 至少 RMSE 或 IAE 一项严格 `<`；
4. safety violation、unclassified failure、silent corruption 均为 0；
5. 实时、网络、settling/overshoot 没有未解释回退。

不满足时仍可在闭环正确、可复现的前提下关闭功能需求，但报告必须写“不变/回退”，不得修改条件。

## 13. TEST-017：端到端闭环

planned runner 的完整 CLI 固定如下。先完成第 9.4、10.4 节制备；然后对 `fixed|mlp × 7|19|43` 逐项创建 fresh Linux rootfs 并运行，共 6 个新 session：

```bash
repo="$(git rev-parse --show-toplevel)"
p4="$repo/results/baseline/runs/<P4-TEST-015-success-run>"
build="$repo/os/axvisor/configs/board/qemu-aarch64-contest-network.toml"
qemu="$repo/configs/contest/qemu-aarch64-linux-zephyr-dual.toml"
linux_vm="$repo/os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml"
zephyr_vm="$repo/os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml"
p4_source_rootfs="<absolute-linux-rootfs-bound-by-$p4/manifest.json>"
zephyr_dir="$repo/tmp/contest/p5-zephyr/image"
zephyr_manifest="$repo/tmp/contest/p5-zephyr/zephyr-control-build.json"

for mode in fixed mlp; do
  for seed in 7 19 43; do
    run_id="phase5-ai-${mode}-s${seed}-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 HEAD)"
    prepared="$repo/tmp/contest/p5-prepared/$run_id"

    python3 "$repo/scripts/contest/ai/prepare_linux_ai_rootfs.py" \
      --repository "$repo" \
      --from-network-session "$p4" \
      --source-rootfs "$p4_source_rootfs" \
      --source-manifest "$p4/manifest.json" \
      --controller-binary "$repo/apps/contest/linux-ai-controller/build/linux-ai-controller" \
      --model "$repo/apps/contest/linux-ai-controller/model/model.bin" \
      --metadata "$repo/apps/contest/linux-ai-controller/model/metadata.json" \
      --model-checksums "$repo/apps/contest/linux-ai-controller/model/checksums.sha256" \
      --qualification-profile "$repo/configs/contest/ai/qualification-v1.json" \
      --controller-mode "$mode" \
      --seed "$seed" \
      --bind-ip 10.77.0.1 \
      --peer-ip 10.77.0.2 \
      --udp-port 46000 \
      --tcp-port 46001 \
      --run-id "$run_id" \
      --output-rootfs "$prepared/linux-ai.ext4" \
      --output-controller-config "$prepared/linux-controller.json" \
      --output-manifest "$prepared/linux-ai-rootfs.json"

    python3 "$repo/scripts/contest/ai/run_closed_loop.py" \
      --repository "$repo" \
      --from-network-session "$p4" \
      --build-config "$build" \
      --qemu-config "$qemu" \
      --linux-vmconfig "$linux_vm" \
      --zephyr-vmconfig "$zephyr_vm" \
      --linux-rootfs "$prepared/linux-ai.ext4" \
      --linux-rootfs-manifest "$prepared/linux-ai-rootfs.json" \
      --linux-controller-config "$prepared/linux-controller.json" \
      --zephyr-image "$zephyr_dir/zephyr.bin" \
      --zephyr-elf "$zephyr_dir/zephyr.elf" \
      --zephyr-build-manifest "$zephyr_manifest" \
      --scenario test-017 \
      --profile "$repo/configs/contest/ai/qualification-v1.json" \
      --controller "$mode" \
      --seed "$seed" \
      --timeout-seconds 600 \
      --run-id "$run_id" \
      --output-dir "$repo/results/baseline/runs/$run_id"
  done
done
```

`--from-network-session` 只复用并复核 P4 已验证的配置/网络输入和 SHA-256；P5 使用新的 Linux rootfs、Zephyr control image、QEMU identity 和 evidence，不得修改 P4 包。runner 必须拒绝已有 output、rootfs run ID/config 不匹配、任一 model/image/profile hash 漂移以及 FIXED run 产生 inference 事件。

每个 run 的 oracle：

- 恰好 1800 个连续 plant sample；
- 每个已应用 request 可关联 Linux input/inference/send、PCAP CONTROL、Zephyr receive/apply 和后续 STATUS feedback；
- 重复执行、非法执行、未知 mode、silent sample loss 为 0；
- model/hash/version 与 rootfs/日志/CONTROL 完全一致；
- RMSE、IAE、settling、overshoot、RTT、action latency 和 success rate 可从 raw 重算；
- panic、unclassified restart 和 cleanup residual 为 0。

## 14. TEST-018：安全态与恢复

`configs/contest/ai/faults-v1.json` 固定至少包含以下独立 case；每个 case 使用新 session，不在一条成功曲线上隐藏多个 primary failure：

| case | 注入 | 必须结果 |
|---|---|---|
| `duty-out-of-range` | `-1`、`65537` | 不应用、不刷新 watchdog、CONTROL/BAD_RANGE |
| `bad-model-version` | MLP version 不匹配 | 不应用、MODEL/VERSION_MISMATCH |
| `duplicate-control` | 相同 session/sequence/request 重放 | ACK 可重发，动作只执行一次 |
| `stale-request` | request 回退 | 拒绝、PROTOCOL/STALE_REQUEST |
| `bad-crc-schema` | CRC、schema、reserved 损坏 | 拒绝、不刷新 watchdog |
| `linux-stop` | 在有效控制后停止进程 `>=700 ms` | 500 ms pending-safe，最迟下一 tick duty=0 |
| `network-stop` | 阻断端口 `>=700 ms` | 周期继续、SAFE|NETWORK_TIMEOUT、无任务饿死 |
| `endpoint-restart` | 重启任一端点 | 新非零 session，旧控制执行 0 次 |
| `udp-expiry-tcp` | 丢全部 CONTROL ACK | t=0/100/300，500 ms取消，旧 UDP 关闭，新 TCP session恢复 |

每个 case 保存 fault manifest、两端 raw events、PCAP、duty/temperature 轨迹、ERROR/STATUS、safe enter/exit 和恢复时间。499/500/501 ms host 虚拟时钟测试通过不能替代真实 Guest 的 500 ms 故障 run。

每个 case 的 planned CLI 必须显式给出同一冻结 fault profile 和唯一 case；不允许 runner 默认选择或一次运行偷偷串联多个 primary fault：

```bash
repo="$(git rev-parse --show-toplevel)"
p4="$repo/results/baseline/runs/<P4-TEST-015-success-run>"
build="$repo/os/axvisor/configs/board/qemu-aarch64-contest-network.toml"
qemu="$repo/configs/contest/qemu-aarch64-linux-zephyr-dual.toml"
linux_vm="$repo/os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml"
zephyr_vm="$repo/os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml"
p4_source_rootfs="<absolute-linux-rootfs-bound-by-$p4/manifest.json>"
zephyr_dir="$repo/tmp/contest/p5-zephyr/image"
zephyr_manifest="$repo/tmp/contest/p5-zephyr/zephyr-control-build.json"
fault_profile="$repo/configs/contest/ai/faults-v1.json"

fault_cases=(
  duty-out-of-range
  bad-model-version
  duplicate-control
  stale-request
  bad-crc-schema
  linux-stop
  network-stop
  endpoint-restart
  udp-expiry-tcp
)

for fault_case in "${fault_cases[@]}"; do
  run_id="phase5-ai-test018-${fault_case}-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 HEAD)"
  prepared="$repo/tmp/contest/p5-prepared/$run_id"

  python3 "$repo/scripts/contest/ai/prepare_linux_ai_rootfs.py" \
    --repository "$repo" \
    --from-network-session "$p4" \
    --source-rootfs "$p4_source_rootfs" \
    --source-manifest "$p4/manifest.json" \
    --controller-binary "$repo/apps/contest/linux-ai-controller/build/linux-ai-controller" \
    --model "$repo/apps/contest/linux-ai-controller/model/model.bin" \
    --metadata "$repo/apps/contest/linux-ai-controller/model/metadata.json" \
    --model-checksums "$repo/apps/contest/linux-ai-controller/model/checksums.sha256" \
    --qualification-profile "$repo/configs/contest/ai/qualification-v1.json" \
    --controller-mode mlp \
    --seed 43 \
    --bind-ip 10.77.0.1 \
    --peer-ip 10.77.0.2 \
    --udp-port 46000 \
    --tcp-port 46001 \
    --run-id "$run_id" \
    --output-rootfs "$prepared/linux-ai.ext4" \
    --output-controller-config "$prepared/linux-controller.json" \
    --output-manifest "$prepared/linux-ai-rootfs.json"

  python3 "$repo/scripts/contest/ai/run_closed_loop.py" \
    --repository "$repo" \
    --from-network-session "$p4" \
    --build-config "$build" \
    --qemu-config "$qemu" \
    --linux-vmconfig "$linux_vm" \
    --zephyr-vmconfig "$zephyr_vm" \
    --linux-rootfs "$prepared/linux-ai.ext4" \
    --linux-rootfs-manifest "$prepared/linux-ai-rootfs.json" \
    --linux-controller-config "$prepared/linux-controller.json" \
    --zephyr-image "$zephyr_dir/zephyr.bin" \
    --zephyr-elf "$zephyr_dir/zephyr.elf" \
    --zephyr-build-manifest "$zephyr_manifest" \
    --scenario test-018 \
    --profile "$repo/configs/contest/ai/qualification-v1.json" \
    --fault-profile "$fault_profile" \
    --fault-case "$fault_case" \
    --controller mlp \
    --seed 43 \
    --timeout-seconds 600 \
    --run-id "$run_id" \
    --output-dir "$repo/results/baseline/runs/$run_id"
done
```

runner 必须验证 `fault_case` 恰好是 profile 中一个对象并把解析后的不可变子配置复制到证据包。Host/switch 注入只能影响该 case 声明的点；发生第二个 primary fault、注入未命中、预期 ERROR/safe/recovery 任一缺失都写 `ai_control_failed`，不能自动换 case 重试。

## 15. IF-010/011 证据目录

```text
results/baseline/runs/<run-id>/
  session.json
  manifest.json
  commands.jsonl
  status.json                         # 最后写：IF-011 布尔结果 + P5 token
  configs/
    qualification-v1.json
    fault-manifest.json
    linux-app-config.json
    zephyr-dotconfig
    zephyr.dts
  model/
    model.bin
    metadata.json
    dataset-manifest.json
    golden-vectors.json
  logs/
    axvisor.raw.log
    linux.raw.log
    zephyr.raw.log
  network/
    capture.pcap
    frames.jsonl
    icpc.jsonl
  metrics/
    linux-events.jsonl
    zephyr-events.jsonl
    trajectory.csv
    summary.json
    recompute.txt
  figures/
    temperature.svg
    duty.svg
  cleanup.json
```

每条原始事件至少包含：

```text
schema_version, run_id, endpoint, scenario, transport,
session_id, sequence, request_id, sample_index, event,
monotonic_ns, value, unit, outcome
```

未知值写 `null`，不能用 0 代替。至少记录 `period_release/start/finish`、`packet_send/receive`、`inference_start/finish`、`control_apply`、`safe_enter/exit`、`feedback_receive`。

Linux RTT 只用 Linux monotonic clock；Zephyr action latency 只用 Zephyr monotonic clock。ICPC `timestamp_ms` 仅用于关联。没有同步证据时禁止发布跨 Guest 单向延迟。

`status.json` 复用 `IF-011`，本 producer 的 token 固定为：成功 `ai_control_completed`、运行失败 `ai_control_failed`、前置阻塞 `ai_control_blocked`。成功示例为：

```json
{"success":true,"status":"ai_control_completed","primaryError":null,"cleanupError":null,"completedChecks":["oracle","hashes","validator","cleanup"],"manifestSha256":"<64-hex>"}
```

失败和 blocked 均写 `success:false`，保留 primary/cleanup error；blocked 还必须写非空 `blockedReason`。只有一个独立 qualification run 或一个明确 fault case 的自身 oracle、hash、validator 和 cleanup 全部通过后，才能原子发布成功状态；六个 qualification session 的汇总必须引用六个不可变子包，不能用一个汇总成功覆盖失败子包。失败包原样保留，后续运行必须新建 run ID；consumer 必须同时检查布尔值、token 和必需字段，不能只猜 token 名。

## 16. 验证顺序

```bash
# 1. 协议与纯逻辑
python3 scripts/test/check_icpc_protocol.py
python3 scripts/test/check_icpc_control_payload.py
python3 scripts/test/check_contest_zephyr_control.py

# 2. 模型与 Linux host tests
python3 scripts/test/check_contest_ai_model.py
make -C apps/contest/linux-ai-controller test CC=gcc

# 3. 目标构建
make -C apps/contest/linux-ai-controller clean all \
  CC=/opt/aarch64-linux-musl-cross/bin/aarch64-linux-musl-gcc
west build -p always -b qemu_cortex_a53 \
  -d tmp/contest/zephyr-control apps/contest/zephyr-control

# 4. runner fixtures / 文档路径
python3 scripts/test/check_contest_ai_runner.py
python3 scripts/test/check_ci_paths.py

# 5. 正式 runtime
# run_closed_loop.py: six TEST-017 sessions, then TEST-018 cases
```

修改共享 C codec 后先跑 host 合同；修改 Zephyr plant/watchdog 后先跑虚拟时钟和整数 golden；修改模型后重跑 dataset/model hash；修改 network/session 后重跑 P4 的 `TEST-014/015`。不能只运行最高层闭环。

## 17. 实现顺序与提交边界

| 次序 | 提交内容 | 关闭条件 |
|---:|---|---|
| 1 | ICPC payload codec + golden negative tests | 24/32/12-byte host 合同全绿 |
| 2 | portable plant + watchdog/mailbox host tests | tick、overflow、499/500/501 ms 全绿 |
| 3 | PI + dataset generator | PCG、split hash、teacher golden 全绿 |
| 4 | deterministic training/export | clean rerun model bytes/hash 相同 |
| 5 | Linux C inference | Python/C <=2 Q16.16 LSB |
| 6 | Linux session/controller app | request/ACK/feedback/mode tests全绿 |
| 7 | Zephyr threads/endpoint/plant | target build、final config/DTS 全绿 |
| 8 | rootfs/image/runner/validator | fixture 成功/失败/status-last 全绿 |
| 9 | 6 个配对 runtime | `TEST-017` 全部证据完整 |
| 10 | 故障 runtime | `TEST-018` 全部安全态/恢复通过 |

若模型训练和应用开发并行，双方只能通过已提交的 model metadata、payload header 和 golden vectors协作，不能口头约定第二套字段。

## 18. P5 完成定义

只有以下条件全部满足，`P5-AI-01` 才能关闭：

- `TEST-016`：dataset、训练、导出、canonical model、metadata、golden vectors 可复现；
- Linux 目标 binary 确实执行 3×8 和 8×1 两层矩阵计算，Guest runtime 日志含真实 inference marker；
- Zephyr plant、mailbox、100 ms cadence、500 ms watchdog 和安全态 host/target/runtime 均通过；
- `TEST-017` 的 6 个 session 全部能关联 input→inference→IP→action→feedback，并报告冻结指标；
- `TEST-018` 的错误输入、Linux/网络停止、restart 和 UDP→TCP 回退均 fail closed、有界恢复；
- 所有 runtime 是当前代码/镜像、真实 Guest IP、同一 session evidence，最高等级达到 `L8 AI-loop`；
- raw 数据、PCAP、模型、配置、命令、hash、status-last、cleanup 和重算命令完整；
- 结果无论改善、不变或回退都如实报告，只有满足第 12 节条件才使用“智能优化有效”；
- `requirements.md`、`test-matrix.md`、`traceability.md`、`deliverables.md`、工作区 `现状.md`/`阻塞.md` 已同步；
- 未声称 StarryOS、RT-Thread、硬件 DMA、开发板、实时 A/B 或综合长稳已由 P5 自动完成。

P5 关闭后把同一网络+AI workload 交给 `P3-RT-01` 完整 production A/B，再进入 `P6-QUAL-01`。P5 的 100 ms 应用周期数据可以作为 P3 压力输入，但不能替代 P3 的 timer/IRQ/vCPU 分段测量和选点门禁。

## 19. 交接模板

```text
工作包：P5-AI-01 / 当前提交边界
base/head：<40-hex>/<40-hex or dirty patch hash>
模型身份：<model sha256 + model_version>
dataset：<train/validation/test sha256>
当前最高证据：L2/L3/L7/L8
已通过：<TEST-016/017/018 subset>
本次命令与 exit code：<commands>
最新 evidence：<run-id + status + manifest sha256>
模式/seed：<fixed|mlp + 7|19|43>
失败/受阻：<primary + cleanup error>
未证明：<explicit non-claims>
下一步唯一入口：<one exact command>
```

下一位开发者先核对 model/data/config hash，再从最早未关闭的提交边界继续；不得从图表或视频反推 raw evidence 已经完整。
