# 良策队阶段性提交拆分清单

状态：计划已建立，尚未执行 `git add`、`git commit` 或 `git push`<br>
目标分支：`contest/axvisor-ai-control`<br>
兼容基线：`rcore-os/tgoskits:dev` / `01e053105e243d1fdd1c03f6f850b266efbe8de0`

## 拆分原则

- 每个 commit 只承载一个可审查责任，避免把文档、平台内存、Guest FDT、console 和 CI 混在同一提交中。
- 已经同时包含暂存与未暂存改动的文件必须使用 `git diff`、`git diff --cached` 和 `git add -p` 复核，不能直接依赖当前 index。
- 不使用无审查的 `git add -A`；不提交 raw evidence、镜像、缓存、临时 EXE 或 contract scratch。
- 每个代码 commit 在提交前运行对应测试；涉及 Rust 的 commit 还必须按 crate 运行 rustfmt、target check/test 和 strict Clippy。

## 建议 commit series

1. `docs(contest): define requirements and private repository delivery`
   - README、开发指南、比赛规格、提交指南、阶段摘要。
   - 只改变需求、证据边界和交付流程，不夹带生产代码。
2. `test(contest): add baseline and evidence contracts`
   - `scripts/test/check_contest_*.py`、证据 validator、CI 路径与 workflow 接线。
   - 记录 host/static 合同的适用边界，不声明双 Guest 或 IP runtime。
3. `feat(axvm): enforce cross-vm resource ownership`
   - `virtualization/axvm/src/resource_claim/`、AArch64 IRQ route/rollback、相关 AxVM/arm_vgic 变更和测试。
4. `feat(axvm): sanitize guest fdt and capture stage2 evidence`
   - Guest FDT create/sanitize/console/interrupt/evidence、DTB capture 及其合同。
5. `feat(axdevice): add vm-local pl011 console framing`
   - AxDevice PL011、Guest console drain/demux、相关配置与验证。
6. `feat(axvisor): reserve fixed hpa and dma guard ranges`
   - someboot/axplat/axhal/axruntime carveout 与 DMA guard、AxVM `MapReserved` 授权、配置和 runtime evidence contracts。
7. `feat(contest): add linux smp2 and zephyr smoke workflows`
   - Linux SMP2、Zephyr 单 Guest/双 Guest静态配置、runner、环境锁定和 QEMU baseline 工具。
8. `feat(contest): add portable icpc v1 host protocol`
   - ICPC C99 库、协议文档、host 合同与 CI 接线。
9. `chore(repo): finalize contest delivery metadata`
   - `.gitignore`、`.gitattributes`、Cargo/README 最终同步、小型脱敏 evidence index。

## 每个 commit 的关闭条件

```text
git diff --check
相关 Python 合同通过
相关 Rust fmt/test/check/clippy 通过
无 high-confidence secret 命中
无个人绝对路径
无 >10 MiB 新文件，除非有明确来源、许可和必要性说明
```

全部 commit 完成后，再在干净 clone 上验证 series，并生成 `git format-patch` 与 SHA-256。只有用户明确授权后，才 push 到 `qcl-kernel/tgoskits-liangce`。
