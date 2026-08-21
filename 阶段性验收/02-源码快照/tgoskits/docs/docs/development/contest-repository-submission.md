# 良策队比赛仓库提交指南

状态：本地准备中，尚未上传<br>
队伍：良策<br>
成员：1 人<br>
正式仓库：`https://github.com/qcl-kernel/tgoskits-liangce`<br>
可见性要求：Private<br>
官方 upstream：`https://github.com/rcore-os/tgoskits.git`

## 1. 提交边界

正式提交面是 `qcl-kernel/tgoskits-liangce` 私有比赛仓库，不是网页上传压缩包，也不是个人 GitHub Fork。首次导入必须从官方仓库的 bare clone 向仍为空的比赛仓库执行一次 `git push --mirror`，以保留完整 commit、branch、tag、作者和时间历史。比赛仓库开始开发后禁止再次执行 mirror push。

开发分支固定为 `contest/axvisor-ai-control`。开发提交直接 push 到本组私有仓库，不创建 PR；官方 `rcore-os/tgoskits` 只允许 fetch，不得 push。离线 `git format-patch`、base/head SHA、patch SHA-256 和 clean-clone 应用记录继续作为审查与灾备工件，但不替代私有仓库中的完整 Git 历史。

## 2. 首次导入流程

目标仓库必须保持为空，且创建时不得初始化 README、`.gitignore` 或 License：

```bash
git clone --bare https://github.com/rcore-os/tgoskits.git tgoskits-initial.git
git -C tgoskits-initial.git push --mirror \
  git@github.com:qcl-kernel/tgoskits-liangce.git
```

镜像完成后，在 GitHub 中把默认分支设为 `dev`，再进行普通 clone：

```bash
git clone git@github.com:qcl-kernel/tgoskits-liangce.git tgoskits-liangce
cd tgoskits-liangce
git remote add upstream https://github.com/rcore-os/tgoskits.git
```

普通 clone 的最终远程结构应为：

```text
origin    git@github.com:qcl-kernel/tgoskits-liangce.git
upstream  https://github.com/rcore-os/tgoskits.git
```

## 3. 当前开发成果迁移

当前工作树必须先完成提交拆分、敏感信息检查和验证，再把开发分支推送到比赛仓库。禁止把 dirty worktree、外层“阶段性验收”目录或无 `.git` 的源码快照当作正式上传对象。

职责拆分和逐 commit 关闭条件见 [`contest-commit-plan.md`](contest-commit-plan.md)。

首次镜像和默认分支设置完成后，可在当前开发仓库中把比赛仓库登记为单独远程，再推送已提交的开发分支：

```bash
git remote add competition git@github.com:qcl-kernel/tgoskits-liangce.git
git push -u competition contest/axvisor-ai-control
```

本文件只记录流程；在用户明确授权上传前，不执行上述 push 命令。

## 4. 入库内容

应提交：

- AxVisor、AxVM、AxDevice、someboot 等源码改动；
- `configs/contest/` 和相关 VM/board 配置；
- `scripts/contest/`、合同测试和 CI 改动；
- 设计、协议、开发、复现和阶段性验收文档；
- 小型、已脱敏、可审查的 evidence 索引与摘要。

不应提交：

- Guest 镜像、rootfs、`target/`、临时 EXE 和缓存；
- `results/baseline/runs/` 等机器相关 raw log 全量包；
- `.codex-temp`、contract-test scratch、一次性状态目录；
- 密码、Token、SSH 私钥、评测凭据、主机序列号或不必要的个人绝对路径。

## 5. 上传前检查

```bash
git status --short
git diff --check
git lfs ls-files
git remote -v
git fetch upstream
git rev-parse upstream/dev
python scripts/test/check_contest_developer_docs.py
```

还必须完成：

1. 目标仓库为 Private，Owner 为 `qcl-kernel`。
2. 目标仓库名为 `tgoskits-liangce`。
3. 唯一成员为仓库创建者本人；未添加其他参赛组。
4. 首次导入前目标仓库为空。
5. 工作树变更已拆成职责清晰的 commit，且无敏感信息和大体积临时工件。
6. 必要合同、格式、构建和运行证据按其真实等级通过。
7. 上传前再次确认不会 push 到 `upstream`。

## 6. 当前状态（2026-08-12）

- 仓库 URL 和命名：已确认。
- 成员范围：用户确认仅本人。
- 空仓库：`git ls-remote` 无 refs，已只读确认。
- Private 可见性：需登录 GitHub 后在仓库 Settings/General 人工确认。
- 官方完整历史 mirror：未执行。
- 默认分支 `dev`：待 mirror 后设置。
- 当前开发工作树提交拆分：未完成。
- 远端上传：未执行。
