# DeepSeek P2–P5 开发入口

最后更新：2026-08-20

本文让 Pi/OpenCode 中的 DeepSeek 从一个入口接手 P2–P5。它只组织读取顺序、工作包边界和验收门禁；数值合同仍以 `contest/contest-spec/` 为准，完整命令以 `tgoskits-upstream-integration/scripts/contest/README.md` 为准。

文档覆盖和本轮合同验证记录见 `development/P2-P5开发文档审计-2026-08-14.md`；该审计只证明开发输入完整，不替代任何 runtime evidence。

## 1. 每次会话固定读取顺序

1. `AGENTS.md` 与 `tgoskits/AGENTS.md`；
2. `development/current/开发交接.md`；
3. `development/current/阻塞.md` 顶部活跃索引；
4. `development/current/现状.md` 顶部快照；
5. `development/current/计划.md` 顶部工作包表；
6. 当前阶段手册；
7. `development/contest/contest-spec/requirements.md`、`contracts.md`、`test-matrix.md`、`traceability.md`；
8. `tgoskits-upstream-integration/scripts/contest/README.md` 中当前 runner/validator 的完整参数；
9. [`项目重新审视与执行路线（2026-08-20）`](analysis/项目重新审视与执行路线-2026-08-20.md) 的官方差异和工作包卡。

在当前开发工作树开始任何代码修改前先执行：

```powershell
Set-Location F:\project\泉城实验室\tgoskits-upstream-integration
git status --short --branch
git rev-parse HEAD
git rev-parse upstream/dev
git rev-list --left-right --count HEAD...upstream/dev
py -3 ..\development\tools\development_docs.py --repo-root . --check
py -3 scripts/test/check_contest_development_mirror.py
py -3 scripts/test/check_ci_paths.py
```

涉及 Rust 代码时必须再完整阅读 `tgoskits/docs/guideline/code-quality.md`；新增/扩大平台、设备、公共接口或用户可见能力时还须阅读 `feature-development.md`。

## 2. 当前开发顺序

```text
P2-DMA-01 / P2-DUAL-01 / P2-SOAK-01（r24j/r27/r31，均已关闭并冻结）
  -> P4-UPSYNC-02（当前主线：官方 21ef 的 DeviceContext/read-write + CI manifest v3）
  -> P4-EVID-01（guest-runtime profile、scenario validator、capture/metrics/status）
  -> P4-SMOKE-02（新 HEAD 重跑 NIC/ARP/ICMP/UDP/TCP/ICPC）
  -> 并行 P4-REL-01 + P5-AI-A/B
  -> P5-AI-C/Q（Q 依赖 P4-REL-01）
  -> P3-TRACE-FEAS-01 + P3-RT-01 production A/B
  -> P6 -> P7（私有仓库/离线包，禁止 PR）
```

P3 的只读路径、schema、统计器、native probe 可以提前维护；改变 timer/IRQ/wakeup/lock 生产语义和发布 A/B 结论必须等 P4/P5 runtime 门禁。

当前只在 `F:\project\泉城实验室\tgoskits-upstream-integration` 写入，分支为
`contest/upstream-20260817`，最近生产代码 checkpoint `574d569be...`；2026-08-20 已只读 fetch
到 `upstream/dev=21ef4b218...`（相对代码 checkpoint behind 39；docs-only 提交会增加 ahead）。
原 `tgoskits` dirty tree只读保留。
先读 [`2026-08-20 重审`](analysis/项目重新审视与执行路线-2026-08-20.md)；旧迁移矩阵只用于
解释 23a 基线的来源，不得据此跳过 #2092 DeviceContext 或 #2105 CI v3 适配。

## 3. 阶段任务地图

| 阶段 | 唯一手册 | 当前可做 | 完成门禁 | 禁止越级表述 |
|---|---|---|---|---|
| P2 | `contest/contest-stage-p2-platform.md` | 只读保留 r24j/r27/r31；仅在输入或实现语义变化时做新回归 | `TEST-005..007` 已 verified；两 VM identity/READY/final DTB/log 归属、生命周期与无残留 | r24j 不是 DMA isolation；dual/soak 不是 IP |
| P3 | `contest/contest-stage-p3-realtime.md` | 维护 schema/statistics/matrix/native probe；runtime 条件满足后采 raw samples、选择最多两条路径、做可关闭 A/B | `TEST-008..010`；每场景三组、样本/时长门槛、可复算 mean/max/P99/P99.9 | 10 个样本、host benchmark、构建通过或代码变化不是实时改善 |
| P4 | `contest/contest-stage-p4-network.md` | 先做 `P4-UPSYNC-02` 与 `P4-EVID-01`；再在新 HEAD 重跑 smoke，最后完成双向大样本/fault/restart qualification | `TEST-011..015`；真实双 Guest NIC/ARP/ping/UDP/TCP/ICPC/capture/恢复；不能只靠 marker | 当前 ICMP/UDP/TCP/ICPC 是旧 HEAD 窄冒烟；`guest_network_completed`、空 metrics/capture 或官方 demo都不是完整验收 |
| P5 | `contest/contest-stage-p5-ai-control.md` | `P5-AI-A/B` 可与 P4 reliability 并行；`P5-AI-C` 做 happy path；`P5-AI-Q` 必须等待 P4-REL | `TEST-016..018`；canonical model、Guest build、input→inference→IP→action→feedback、安全态 | host 模型、占位 STATUS、分别运行应用、模拟网络或截图不是 AI-loop |

## 4. 静态门禁

优先使用 Pi 项目工具：

```text
contest_gates workpackage=p2-dual
contest_gates workpackage=p2-soak
contest_gates workpackage=p3
contest_gates workpackage=p4
contest_gates workpackage=p5
contest_gates workpackage=docs
```

任一门禁失败先修确定性合同；不得用 QEMU 反复试错替代 host regression。WSL/QEMU/Docker 仍须用户明确确认，fresh evidence 必须使用新目录。

## 5. DeepSeek 每次领取任务的任务卡

```markdown
# Task Card
- workpackage: <P4-UPSTREAM-01/P4-NET-01/P5-AI-01/P3-RT-01>
- objective: <本轮唯一可验收目标>
- base_sha: <40 hex>
- allowed_paths: <仓库相对路径列表>
- acceptance: <确定性测试 + runtime oracle>
- validation_commands: <命令；注明哪些需用户确认>
- evidence_directory: <fresh 路径或 none>
- evidence_claim: <最多能证明什么>
- non_claims: <明确不能证明什么>
- operations_needing_user: <WSL/QEMU/push/delete 等或 none>
```

同一时刻只允许一个写入者。writer 停止后，使用 Pi 的 `code-reviewer` 与 `evidence-auditor` 做只读复核；P0/P1 或 `INCONCLUSIVE` 未关闭前不得进入下一 runtime 门禁。

## 6. 每轮完成后的文档更新

1. 先更新 `development/contest/contest-spec/` 中产生事实的权威文件；
2. 再更新 `development/current/现状.md`、`阻塞.md`、`开发交接.md`，依赖变化时更新 `计划.md`；
3. 运行 `py -3 development/tools/development_docs.py --sync-to-repo` 生成仓库交付镜像；
4. 运行布局、文档合同、相关 stage gate、`git diff --check`；
5. failed-attempt 保持原样，primary/cleanup 错误分开记录。

## 7. 文档足够时与必须暂停时

阶段手册已经包含文件地图、实现顺序、负例、构建/运行命令、证据目录、完成定义和非结论，足以支持 P2–P5 分阶段开发。出现以下情况必须暂停并登记阻塞：

- 手册与当前源码入口不一致；
- 数值/状态 token/证据 schema 在 `contracts.md` 中没有唯一答案；
- 需要改变 frozen DEC、网络拓扑、模型 shape、CPU ownership 或证据等级；
- 需要覆盖旧 evidence、绕开 runner/validator 或发送未获授权的外部数据；
- runtime 所需 WSL/QEMU/Docker 尚未获用户确认。
