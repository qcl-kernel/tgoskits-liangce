# 泉城实验室开发文档中心

最后更新：2026-08-20

这里是本项目所有**活动开发文档的唯一入口**。新开发者或 DeepSeek 不再从工作区根目录、`tgoskits/docs/docs/development/` 和零散周报中猜入口。

## 立即开始

1. 先读 [DeepSeek P2–P5 开发入口](DeepSeek-P2-P5开发入口.md)。
2. 再按顺序读 [开发交接](current/开发交接.md) → [阻塞](current/阻塞.md) → [现状](current/现状.md) → [计划](current/计划.md)。
3. 打开当前工作包的阶段手册：
   - [P2 平台、双 Guest 与长稳](contest/contest-stage-p2-platform.md)
   - [P3 实时测量与 A/B](contest/contest-stage-p3-realtime.md)
   - [P4 mediated VirtIO-net 与 Guest IP](contest/contest-stage-p4-network.md)
   - [P5 Linux MLP 与 Zephyr 安全闭环](contest/contest-stage-p5-ai-control.md)
4. 需求、接口、测试和证据等级以 [contest-spec](contest/contest-spec/README.md) 为权威。
5. 本轮覆盖检查与合同结果见 [P2–P5 开发文档审计](P2-P5开发文档审计-2026-08-14.md)。
6. **当前权威重审结论与修订执行链**见 [项目重新审视与执行路线（2026-08-20）](analysis/项目重新审视与执行路线-2026-08-20.md)。旧的 [项目最大困难与路线重构](analysis/项目最大困难与路线重构-2026-08-17.md) 只保留历史判断。
7. 旧官方基线上的逐文件去留、资源冲突和首批迁移见 [P4-UPSTREAM-01 迁移矩阵](analysis/P4-UPSTREAM-01迁移矩阵-2026-08-17.md)；继续开发前必须再执行 `P4-UPSYNC-02`，不得把其中的 `23a07bfc...` 当作当前官方 HEAD。
8. 两份原始目标材料位于 `sources/陈天昊-良策-技术方案提纲.docx` 与 `sources/陈天昊-良策-揭榜申请书.docx`；原件只读，规范化需求见 `contest/contest-spec/requirements.md`。

## 目录

| 目录 | 内容 | 更新规则 |
|---|---|---|
| `current/` | 计划、现状、阻塞、开发交接 | 每次状态、证据、阻塞或交接变化时更新 |
| `contest/` | 竞赛执行指南、P2–P5 手册、协议、架构、安全设计、规范矩阵 | 实现接口、合同、测试或完成定义变化时更新 |
| `analysis/` | 故障分析、仓库影响分析、阶段比较 | 只记录分析，不覆盖权威状态 |
| `reports/` | 当前周报与历史周报 | 用于汇报，不作为 runtime 证据 |
| `process/` | Pi/DeepSeek 配置与协作流程 | agent 组件或审查流程变化时更新 |
| `sources/` | 原始申请书、技术方案与 PDF | 原件只读；hash 由规范文档核对 |
| `reference/` | 仓库通用开发指南的集中权威副本，以及必须原位保留的运行时文档索引 | 通用指南单向同步；运行时配置只导航 |
| `tools/` | 文档镜像同步与布局检查 | 机械同步，不提升证据等级 |

## 单一事实来源与仓库镜像

- `development/contest/` 是 23 份竞赛开发文档的工作区权威版本；`development/reference/repository/` 是 5 份仓库通用开发指南的集中权威版本。
- `tgoskits-upstream-integration/docs/docs/development/` 中这 28 份同名文件是当前 Git/Docusaurus/私有比赛仓库需要的**交付镜像**，不得单独编辑；旧 `tgoskits/` 镜像只作历史参考。
- 修改权威版本后运行：

```powershell
py -3 development/tools/development_docs.py --repo-root tgoskits-upstream-integration --sync-to-repo
py -3 development/tools/development_docs.py --repo-root tgoskits-upstream-integration --check
```

- ArceOS、StarryOS、AxVisor 和组件官网指南的集中副本见 [仓库参考文档](reference/README.md)，仓库内原路径作为发布镜像保留。
- `AGENTS.md`、`.pi/skills/**/SKILL.md`、`.pi/prompts/*.md` 属于工具自动发现的运行时配置，不迁移；本目录只提供其索引。

## 证据纪律

设计/静态合同不等于 runtime；host 测试不等于 Guest；single-Guest 不等于 dual-Guest；r23/r24j 不等于通用 DMA isolation；双 Guest 不等于 Guest IP；Guest IP 不等于 AI-loop；构建或少量样本不等于实时 A/B。任何文档冲突时，以 `contest-spec/requirements.md`、`test-matrix.md`、`traceability.md`、`deliverables.md` 和 immutable evidence 为准。
