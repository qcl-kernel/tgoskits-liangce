# 最终交付物清单

最后更新：2026-08-17

本文定义两份申报材料所承诺交付物的文件级清单。表中的“计划路径”不是“已经存在”；只有路径可访问、SHA-256 已填写、生成/复核命令成功、reviewer 留下结论且状态为 `delivered`，该项才可计入最终完成度。

`DEC-009` 固定的 `2026-08-20 18:00 Asia/Shanghai` 只是内部可交付快照冻结点。届时允许清单仍含 `partial/planned/blocked`，但必须如实保留；该快照不是最终验收完成，也不是官方平台截止。官方精确提交时刻未核验前，不得执行或宣称最终提交。

## 1. 状态与清单规则

状态只使用：

- `planned`：尚未产生；
- `partial`：已有一部分源码、文档或本地证据，但尚不构成完整可交付项；
- `ready-for-review`：内容、hash 和复现命令齐全，等待指定 reviewer；
- `delivered`：review 通过且最终 manifest 已冻结；
- `N/A`：仅用于来源明确写作“如有”的可选项，并需项目负责人签字说明。

最终新增 `results/contest/manifest.json` 作为机器可读总清单。每个文件或外部 evidence archive 至少包含以下字段；目录名、URL 或 run ID 不能代替文件 hash：

```json
{
  "deliverable_id": "DEL-001",
  "path_or_uri": "repo-relative path or immutable URI",
  "sha256": "64 lowercase hex characters",
  "size_bytes": 0,
  "source_revision": "40-hex git commit",
  "generated_by": "exact command or manual-source identifier",
  "verified_by": "exact command",
  "reviewer": "real person or upstream review URL",
  "reviewed_at_utc": "YYYY-MM-DDTHH:MM:SSZ",
  "status": "delivered",
  "requirements": ["REQ-..."],
  "evidence_scope": "what this artifact proves",
  "non_claims": ["what this artifact does not prove"]
}
```

大型镜像、pcap 和 raw logs 不进入 Git。它们可以位于 ignored `results/baseline/runs/` 或外部不可变归档，但最终 manifest 必须给出可访问位置、大小、SHA-256、生成命令和脱敏审查记录。只有本机路径而无可交付归档时，状态最多为 `partial`。

## 2. 交付项

| DEL-ID | 内容与关联需求 | 计划/当前路径 | Hash 与生成/复核命令 | Reviewer 门禁 | 当前状态 |
|---|---|---|---|---|---|
| `DEL-001` | **源码**：AxVisor 实时/安全改动、mediated VirtIO-net 双 frontend/Guest-memory copy/`vnet0`、Linux 推理应用、Zephyr 控制应用、portable ICPC；覆盖 `REQ-PLAT-*`、`REQ-RT-*`、`REQ-NET-*`、`REQ-AI-*`。 | 当前/计划：`platforms/`、`virtualization/axdevice/`、`virtualization/axvm/`、`os/axvisor/`、`scripts/contest/icpc/`；当前 dirty tree 已有 P4 mediated net/queue/switch/IRQ/poll 源码 checkpoint；待补齐 P4 Guest runtime、P5 模型/闭环应用。最终逐文件进入 `results/contest/manifest.json`。 | 当前没有最终集合 hash。生成：正常代码开发/构建命令；复核：`git diff --check`、适用 test/strict Clippy/rustfmt、正式 AArch64 AxBuild、每文件 `sha256sum`。 | 实现负责人自审 + 离线源码审查者；高风险 Guest-memory、VirtIO descriptor、IRQ、协议改动须单独审查。 | `partial`：dual runner/collector、P3 host RT 工具、P4 host fault/capture 工具、P4 mediated frontend 源码和 P5 host model/control 工具已有；当前代码 Rust/target/runtime 门禁、成功 Guest-IP、生产实时 A/B、Guest AI 应用仍未关闭。 |
| `DEL-002` | **配置**：vCPU/pCPU、内存、设备、IRQ、启动参数、双 Guest、内部 `vnet0`、网络 MAC/IP/route/port、压力与安全策略；覆盖 `REQ-PLAT-002..003`、`REQ-NET-002`、`REQ-QUAL-001`。 | 当前：`configs/contest/`、`os/axvisor/configs/vms/qemu/aarch64/*dual.toml`；计划补 run-ready mediated network/场景配置并由 manifest 枚举。 | 每份 TOML/env/overlay/DTS hash 必填；复核 dual 配置合同及计划中的 mediated-network/qualification 合同。 | 平台负责人 + evidence reviewer。 | `partial`：`DEC-008` 已冻结内部 `vnet0`、MAC/IP/port/payload；现有 dual/outer topology 仍是 `blocked_dma_console` 静态历史，不能作为 run-ready mediated 配置或 Guest-IP 证据。 |
| `DEL-003` | **构建、运行、故障注入和统计脚本**；覆盖所有 `REQ-*` 的可复现入口。 | 当前：`scripts/contest/`、`scripts/test/`；P4 network prepare/runner/fault/capture/validator 源码已存在但 runtime publisher 仍需完善；计划补 `scripts/contest/qualification/` 与最终交付 runner。 | 文件 SHA-256；Python `py_compile`/合同，Bash `bash -n`，PowerShell parser；runtime 脚本还须保存 `--help`、exact invocation 和 exit code。 | 对应工作包 owner + 未参与该次运行的证据复核者；若团队只有一人，以离线 review manifest/答辩复核记录补足。 | `partial`：dual runner/collector 已实跑并形成失败包，RT schema/statistics/matrix/native probe、network fault/capture/validator、P4 mediated source boundary 和 AI host model/train/verify/plant/watchdog/metrics 等 host 工具合同通过；缺当前代码目标门禁、成功 dual Guest-IP、综合 qualification 和 final delivery runner。 |
| `DEL-004` | **原始数据与证据索引**：raw log、CSV/JSON、pcap、Guest-DTB、QMP、状态、环境、源码快照和 checksum；覆盖 `REQ-RT-003`、`REQ-NET-004`、`REQ-AI-003`、`REQ-QUAL-*`。 | 本地原始包：ignored `results/baseline/runs/<run-id>/`；计划提交 `results/contest/evidence-index.csv` 和脱敏 `results/contest/evidence/<EVD-ID>/summary.json`，大文件转不可变外部归档。 | 每个 raw/summary/archive 单独 SHA-256；执行 `sha256sum -c checksums.sha256` 或 PowerShell `Get-FileHash`；索引校验器须验证 run ID、时间、revision、路径、hash、status 和 evidence scope。 | evidence reviewer 逐项签署；不得由 CSV 行自动提升证据等级。 | `partial`：新增 r24j verified 窄 DMA effect、Linux console verified 包和最新 dual failed-attempt，均仍是本地 ignored 事实；尚无完整可公开索引，失败包状态不得改写。 |
| `DEL-005` | **统计表与图**：实时 mean/max/P99/P99.9、网络成功/超时/吞吐/RTT、AI 控制误差/settling/overshoot、长稳异常；覆盖 `REQ-RT-003`、`REQ-NET-004`、`REQ-AI-003`、`REQ-QUAL-001`。 | 计划：`results/contest/statistics/*.json|csv`、`results/contest/figures/*.{svg,png}`，生成源码放 `scripts/contest/rt/` 或 `scripts/contest/qualification/`。 | 图、表和生成脚本均入 manifest；`generated_by` 必须是一条从 `DEL-004` raw 数据重建全部数值的命令；复核结果须逐字段匹配已提交统计表。 | 数据复核者检查输入集合、单位、分位数算法、异常/缺失样本和图表标签。 | `planned`：RT statistics 与 AI metrics 计算合同已有，但尚无完整 RT/IP/AI/长稳 runtime 数据，因此不能先制作“结果图”或声称指标成立。 |
| `DEL-006` | **设计文档**：原始来源、受控决策、需求、架构、接口、协议、实时路径和安全边界；覆盖 `REQ-DEL-001`。 | 权威版：`development/contest/contest-spec/`、`development/contest/contest-icpc-protocol.md`、`contest-realtime-path.md`；Git/Docusaurus 交付版由 `development/tools/development_docs.py` 单向同步到 `docs/docs/development/`。 | 文档逐文件 SHA-256；复核 `development_docs.py --check`、`check_contest_developer_docs.py`、链接/ID/traceability 合同与 `git diff --check`。 | 项目负责人确认改变申报优先级的最终对外措辞；技术 reviewer 确认 `DEC-001..010` 的 as-built 代码/接口一致。 | `partial`：v1 设计已冻结，但官方网络基线迁移、Guest IP、AI、runtime 反馈和最终 as-built 内容尚未关闭；设计冻结不等于交付。 |
| `DEL-007` | **测试规范与测试报告**：本矩阵、单元/合同/构建/runtime/故障/长稳报告；覆盖 `REQ-QUAL-*`、`REQ-DEL-001`。 | 规范：`contest-spec/test-matrix.md`；计划报告：`results/contest/tests/<TEST-ID>.json` 及精简 Markdown 摘要；validator 固定为 `scripts/contest/evidence/validate_test_report.py`，合同固定为 `scripts/test/check_contest_test_report.py`。 | 每份报告含 exact command、exit code、环境、输入/输出 hash 和 oracle 判定；validator 必须对 TEST ID、状态、证据等级、必需工件、hash 和本矩阵 oracle 做 fail-closed 复核。 | 对应组件 owner + evidence reviewer；失败报告同样保留，不能改写状态。 | `partial`：`TEST-005` 有 r24j verified 窄证据，`TEST-006` 有 authoritative failed-attempt；2026-08-14 复跑的 12 项 P3/P4/P5 host 合同均通过。大多数强制 Guest/runtime/长稳测试仍未通过。 |
| `DEL-008` | **独立复现材料**：环境锁定、依赖/镜像下载 hash、build/run 命令、clean-clone transcript、CI；覆盖 `REQ-DEL-001`、`REQ-DEL-003`。 | 当前：`contest-developer-guide.md`、`scripts/contest/README.md`、`results/baseline/README.md`；计划 `docs/docs/development/contest-reproduction.md`、`results/contest/reproduction/`。 | `TEST-020` 在新 clone 执行；记录 base/head SHA、工具版本、命令/exit code和所有输出 hash。仅引用本机 ignored 路径则失败。 | 非原执行者优先；无法独立人员复核时由指定离线 reviewer 签署并明确限制，不以 PR reviewer 替代。 | `planned`：现有指南是接手材料，dirty worktree 尚未做最终 clean-clone 演练。 |
| `DEL-009` | **演示视频与脚本**：展示双 Guest、真实 IP、AI 动作/反馈、实时对比和故障恢复；覆盖 `REQ-AI-002..003`、`REQ-DEL-003`。 | 计划：`results/contest/demo/demo-script.md`；视频保存到申报平台或不可变归档，URI 记录于 manifest，不直接提交大文件。 | 视频文件 SHA-256、时长、分辨率、录制日期、对应 run IDs；手工逐段核对视频时间码与原始证据。 | 项目负责人确认无剪辑误导、无秘密/个人信息、每个公开结论均可追溯。 | `planned`：端到端能力未完成，不能用模拟/host 画面冒充双 Guest 演示。 |
| `DEL-010` | **私有比赛仓库与离线源码审查包（禁止 PR）**：`qcl-kernel/tgoskits-liangce` 保留官方完整历史；以锁定 `rcore-os/tgoskits:dev` SHA 为只读 base，`contest/axvisor-ai-control` commit series Apache-compatible、可无冲突应用且 CI 通过；覆盖 `REQ-DEL-002`。 | 比赛仓库 refs；计划 `results/contest/delivery/patches/*.patch`、`review-manifest.json`、`apply-transcript.txt`；禁止创建或提交 PR，禁止直接 push upstream，不记录伪造 PR URL。 | repository/default branch/refs、base/head 40-hex SHA、commit list、每份 patch SHA-256、`git diff --check`、clean-clone `git am`、license scan、required CI URLs 与状态；单人队伍由答辩或指定外部复核记录补足独立 review。 | 项目负责人确认比赛仓库权限、manifest 的 evidence 边界及 `prPolicy=forbidden`；外部复核者如有则登记。 | `planned`：目标仓库已创建且为空；官方历史 mirror、dirty tree 拆分、开发分支 push、clean-clone 和 patch bundle 均未完成；PR 被明确禁止。 |
| `DEL-011` | **第三方检测报告（如有）**。原申请把它列为可选，不得用不存在的报告替代自测。 | `N/A`；若未来取得，登记不可变报告文件/URI、机构、日期和范围。 | 当前 `N/A`，无 hash/命令；最终 manifest 必须明确 `reason=not_required_by_source`。若提供则计算报告 SHA-256 并核验签章/范围。 | 项目负责人对 `N/A` 签字；如有报告，由发布机构和项目负责人共同确认。 | `N/A`：不会阻塞基础交付，也不能提升任何技术证据等级。 |
| `DEL-012` | **模型、模型卡与可复现数据生成**：固定一阶温度对象、PI 基线、`3→8→1` MLP、训练/导出和 golden vectors；覆盖 `REQ-AI-001..003`。 | 计划：`apps/contest/linux-ai-controller/model/`（canonical weights、metadata、golden vectors）；当前已有 host 数据生成、模型、训练/导出、验证、plant/PI/watchdog/metrics 工具。 | 模型/metadata/golden vector/data schema 每文件 SHA-256；`TEST-016` 必须重建相同 canonical model bytes，并证明部署输出与参考不超过 2 个 Q16.16 duty LSB；记录 seed、框架/编译器版本。 | AI 实现负责人 + 控制指标 reviewer。 | `partial`：host 工具与合同已实现；仍没有冻结 canonical 模型、模型卡、归档训练数据、部署 C/Linux Guest 推理或 target/runtime 证据。 |

## 3. 最终交付审计顺序

1. 由 `traceability.md` 导出所有强制 `REQ-*`，确认对应 `TEST-*` 已为 `verified`。
2. 生成 `results/contest/manifest.json`；拒绝空 hash、通配符路径、当前机器临时路径、可变 URL 和不存在的 summary。
3. 从 manifest 在干净 clone 逐项执行 `verified_by`；大文件从登记 URI 下载后先验 hash 再使用。
4. 从 `DEL-004` 原始数据重算 `DEL-005`，再核对文档、视频和离线 review manifest 中的每个数字及结论。
5. 完成 `TEST-020..022` 后，由项目负责人冻结 manifest；之后任何文件变化都需要新版本和新 hash。

只有 `DEL-001..010` 与 `DEL-012` 全部为 `delivered`，`DEL-011` 为经签字的 `N/A` 或真实 `delivered`，才能说申报材料承诺的最终交付已完成。
