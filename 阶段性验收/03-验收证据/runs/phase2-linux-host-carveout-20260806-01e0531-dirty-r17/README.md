# Phase 2 Linux Host-Carveout and DMA-Guard Evidence

日期：2026-08-06  
源码：`01e053105e243d1fdd1c03f6f850b266efbe8de0` + 未提交竞赛改动  
范围：单个 Linux Guest、单个 `MapReserved` RAM 区域、QEMU AArch64

## 最终结论

`live-r21/` 在同一次、同一 QEMU 身份绑定的运行中完成了以下可观察链：

1. QEMU 实际 argv 使用本目录派生的 `prepared/host-carveout.dtb`。
2. Host FDT 中 VM 1 的 `0x80000000..0x90000000` carveout 在全局分配器初始化前进入 `Reserved`，且分配器视图为 `reserved_cover=1, free_overlap=0`。
3. Guest 最终 DTB 在 `AXVISOR_GUEST_DTB_READY` 后由同一 QMP 会话执行 `stop/pmemsave/cont` 捕获，并在恢复后重新校验同一 PID/start-time/cmdline 身份。
4. AxVM 运行时查询得到 `MapReserved` 的 `gpa=0x80000000 -> hpa=0x80000000`、`identity=1`。
5. Linux 6.18 启动，明确报告 Guest DT 中不存在 `/reserved-memory`，执行 `/bin/sh` 并出现唯一的 `~ #` 提示符。
6. harness 通过 pidfd 向同一 QEMU 发送 SIGINT，进程组退出，QMP 会话目录删除；独立复核未发现残留 QEMU。

最终状态为 `single_guest_live_capture_passed`。这关闭的是单 Guest 的 Host carveout、allocator 排除、`MapReserved` stage-2 观察和 Linux shell 启动门槛；不证明 passthrough DMA 隔离、双 Guest 共存、30 分钟稳定性或 Linux/Zephyr TCP/UDP/IP 通信。

## 输入绑定

| 输入 | 大小 | SHA-256 |
| --- | ---: | --- |
| `configs/vmconfig.map-reserved.toml` | 712 | `6bc51680cc3509369efaa669dd3f9c8065f25c74250372edcf6b366112d61fa0` |
| `prepared/host-carveout.dtb` | 8277 | `e5820a5a34d334af4c11713282cf13f5fa878386c0d5c7600207c3b758a2e63f` |
| `prepared/host-carveout.dts` | 9878 | `5e90d354ab72123cbbc5c10a500187d8a1cb87a1cf8f6b740fa3d07c5ff6286f` |
| `prepared/host-carveout.preflight.json` | 1132 | `df0e845a69195d0f40be5092a2720ead4a4994b453da0ac810a3f8a4d1483e85` |

`prepared/derivation.json` 最后发布，状态为 `prepared-unlaunched`。它证明派生关系和静态预检，不单独证明 QEMU 使用了该 DTB。`compile-dtc.stderr.log` 中保留了 base DTB 解码/重编译时继承的 numeric phandle 警告；最终 DTB 可被 `dtc` 再次解码，警告未被隐去。

## 运行迭代

| 目录 | 结果 | 分类 |
| --- | --- | --- |
| `live/`（r17） | QMP capture 成功，Host marker 可见；捕获的 Guest DTB 仍继承 Host `/reserved-memory` | 历史成功捕获，但 Guest 语义无效，不作为最终启动证据 |
| `live-r18/` | 启动前找不到 `cargo` | 失败审计，未启动 QEMU |
| `live-r19/` | READY/QMP capture 成功，等待 stage-2 marker 300 秒超时；Guest DTS 同时含 `/reserved-memory` 和同范围 `/memory@80000000` | 失败审计；定位到 root passthrough 复制 Host reservation，使 Linux 唯一 RAM 被保留 |
| `live-r20/` | Host `RESERVED` 与 `ALLOCATOR_EXCLUDED` marker 均为裸行且验证通过 | 仅关闭 byte-bound Host marker 观察，不证明 Guest boot 或 stage-2 |
| `live-r21/` | Host marker、READY、`MapReserved` stage-2、Linux boot、无 Guest reserved-memory、`/bin/sh`、`~ #` 全部按序出现 | 当前最终单 Guest 实机证据 |
| `prepared-dma-guard-r22/` | 在 r17 base DTB 上加入独立 `dma-guard@180000000`；派生与独立 preflight 字节一致 | 静态准备通过；没有启动 QEMU，不是 DMA 运行证据 |
| `live-r23/` | 同一 identity-bound QEMU 会话使用 r22 派生 DTB，guard 的 allocator marker、capture-chain/nonce/Guest-DTB、`MapReserved` 与 Linux shell 均通过聚合绑定 | Host allocator/runtime marker 观察；不证明真实 DMA effect 或 isolation |

修复位于 `virtualization/axvm/src/boot/fdt/core/create.rs`：在任何 root passthrough 命中前，无条件排除 `/reserved-memory` 及其后代。回归同时覆盖 generated DTB、runtime-patched DTB，以及不应被误删的 `/reserved-memory-device` 近似路径；`cargo test -p axvm generated_fdt_` 为 11/11 通过。

## r21 精确运行证据

以下标记在 `live-r21/axvisor-live.log` 中各出现一次，顺序固定：

| 行 | 观察 |
| ---: | --- |
| 119 | `AXVISOR_HOST_VM_CARVEOUT_RESERVED vm=1 hpa=0x80000000 size=0x10000000` |
| 187 | `AXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED ... reserved_cover=1 free_overlap=0 ...` |
| 275 | `AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=7540 ...` |
| 290 | `AXVISOR_STAGE2_HPA_POSTRUN vm=1 vcpu=0 kind=reserved gpa=0x80000000 hpa=0x80000000 page_size=4096 identity=1` |
| 292 | Linux 6.18 version 行 |
| 297 | `No reserved-memory node in the DT` |
| 548 | `Run /bin/sh as init process` |
| 550 | 唯一的 `~ #`，随后 QEMU 收到 SIGINT |

`--post-resume-marker '~ #'` 仅表示在恢复后的 launcher-owned 日志字节中观察到该精确子串；Linux 启动结论还依赖上表的有序、唯一日志观察。它本身不是网络、DMA 或双 Guest 证明。

## r21 派生报告

| 文件 | 状态/内容 | SHA-256 |
| --- | --- | --- |
| `live-r21/status.json` | `single_guest_live_capture_passed` | `ca6688a4ec55547b9fb509d9d55256c9c1ca2c9c24108b721bb7926222ea2071` |
| `live-r21/axvisor-live.log` | 38257 字节原始运行日志 | `77291a3917a6755e5bf74cf6716c6efc431614ffcee05c163a0869923fab16f3` |
| `live-r21/live-capture/guest-vm-1.final.dtb` | 7540 字节 QMP 捕获结果 | `1c2f933da644f4396dddd8ede2a24d499e083c7eca97aee49da5286cd1e207d5` |
| `live-r21/guest-vm-1.final.dts` | 第一次独立 `dtc` 解码 | `399e9c57d0c6e142b716743da1aaa9d4e612fc89ed2b5ef91b2a7f8126585989` |
| `live-r21/guest-vm-1.review.dts` | 第二次独立 `dtc` 解码，与第一次逐字节一致 | `399e9c57d0c6e142b716743da1aaa9d4e612fc89ed2b5ef91b2a7f8126585989` |
| `live-r21/guest-vm-1.semantic.json` | `captured_guest_dtb_static_semantics_validated`；根 `/reserved-memory` 为 0 | `6c46648d62cef7d98686aea70853b93c501ec8f8684b05f9001ae2a46dbfc364` |
| `live-r21/stage2-hpa-runtime.json` | `stage2_hpa_markers_match_configured_regions` | `6d2c8b342a6027caaaca2e2d319fd9763c397ef9c027c20ec84cfd9ed5d702ff` |
| `live-r21/host-carveout-runtime.json` | `host_carveout_markers_match_static_preflight` | `9fdce0cc62716b479e7af1df1d6f2670788dc378ae25e8d41aafc6f1f8adb103` |
| `live-r21/linux-map-reserved-boot-runtime.json` | `linux_map_reserved_single_guest_boot_chain_observed`；聚合 8 个有序文字观察 | `125dfae617a929af40fa108f4760b737446b3a8c4046469c3bac2a85afb9a0cd` |
| `live-r21/linux-map-reserved-boot-runtime.review-r24.json` | 加强版聚合器独立复核；与原报告逐字节一致 | `125dfae617a929af40fa108f4760b737446b3a8c4046469c3bac2a85afb9a0cd` |

Guest-DTB 静态报告把 console 分类为 `passthrough-or-no-vm-owned-console`，因此不声称 VM-local console 隔离。四类派生报告分别说明各自的窄证明范围，不能互相替代；它们共同绑定同一 r21 原始日志和配置，但仍不产生 DMA、双 Guest 或 IP 结论。

## r22 DMA guard 静态准备

`prepared-dma-guard-r22/host-carveout.dts` 在 VM 1 carveout 之外加入了 2 MiB、
2 MiB 对齐的 `dma-guard@180000000`，带精确
`compatible = "axvisor,dma-guard-v1"`、`reg` 与空 `no-map`，且没有 VM ID。
两次 preflight 字节一致，明确把它与 VM-owned carveout 分开：

| 文件 | 大小 | SHA-256 |
| --- | ---: | --- |
| `prepared-dma-guard-r22/derivation.json` | 2415 | `cc45e03d320f258f6f35af74f4dd378c932d5eac7a89d12d51fdfdb0ef36c4d6` |
| `prepared-dma-guard-r22/host-carveout.dtb` | 8388 | `ffea82e11c318d02243a14c5deea9434fe89ff313878fad336ce0cd2147084ab` |
| `prepared-dma-guard-r22/host-carveout.dts` | 10001 | `4503e84b88ef56188035a70b00f08125df0d637abd7290cb2f24869248b1aab5` |
| `prepared-dma-guard-r22/host-carveout.preflight.json` | 1366 | `c5f7a5a36f756e6289009a027840c9881918fe8299e4184402859a8297ad072d` |
| `prepared-dma-guard-r22/host-carveout.preflight.review.json` | 1366 | `c5f7a5a36f756e6289009a027840c9881918fe8299e4184402859a8297ad072d` |

源码已增加 guard 的 fail-closed FDT 解析、独立平台 manifest、Host allocator
初始化前 `Reserved`/FREE 排除校验和联合 runtime validator。动态 reservation、损坏
RAM、重复/错位属性、越界/重叠以及 pre-init 空清单缓存均有拒绝路径。2026-08-10
已完成该 delta 的 Rust 门禁、正式 AxBuild 与 r23；但 r22 本身仍是“静态准备”，
r23 只把这一个 guard 范围升级为 Host allocator/runtime marker 观察，绝不升级为
真实 DMA effect 或 DMA isolation。

## r23 DMA guard 运行观察

`live-r23/` 于 2026-08-09 完成，`status=single_guest_live_capture_passed`，session
nonce 为 `7a0d4d836a6eb047403dd2ce2da0c934`。同一 launcher/QMP identity chain 的原始
日志中，guard `0x180000000:0x200000` 的 `RESERVED` 位于 line 118，
`ALLOCATOR_EXCLUDED reserved_cover=1 free_overlap=0` 位于 line 195，二者均早于
READY（line 284）。随后 stage-2 reserved identity、Linux version、无 Guest
`/reserved-memory`、`/bin/sh` 与唯一 `~ #` 依序出现。

`validate-r23.sh` 读取 r22 的 DTB/DTS/preflight 和 r23 的 capture-chain、Guest
DTB、状态、原始日志及派生报告。聚合器验证 capture-chain SHA-256、32 个十六进制字符的 nonce
与 QEMU name 一致，并将 chain 中唯一的 VM 1 Guest-DTB 与解码输入逐字节绑定。
其结果是窄的单 Guest literal/log evidence，不能替代 DMA 请求或隔离证明。

| 文件 | 大小 | SHA-256 |
| --- | ---: | --- |
| `live-r23/status.json` | 6949 | `2b2829415eca4a387ab36d226b3d3e475f9af8dadba473a65e752afca08ca588` |
| `live-r23/axvisor-live.log` | 38971 | `fd170d561459e09041e851c5f3c09914ab19b3e28abf38c80684f633fcf6fae5` |
| `live-r23/live-capture/capture-chain.json` | 1815 | `21c61f35d8987966a4a1936a38592d921412d1f66673aee4f31971c32a0eadf9` |
| `live-r23/host-carveout-and-dma-guard-runtime.json` | 3437 | `e3e0969ed8bb64bbb5b7fa491d70bcb2cce409e7ed1c6f32e116d3f0c4d10162` |
| `live-r23/stage2-hpa-runtime.json` | 2218 | `693ad64c3b8c01b4ccad5b58df880587cd12a8468e8f63dd064db01b7335722e` |
| `live-r23/linux-map-reserved-dma-guard-boot-runtime.json` | 5867 | `882bd47385713d1515fd5fb6beb8c24711ba2ffebe0651dc332cbc3d49b3d88b` |

## 已执行验证

- `cargo test -p axvm generated_fdt_`：11 passed。
- 所有 `scripts/test/check_*.py`：30/30 passed。
- 两次 `dtc -I dtb -O dts`：输出 SHA-256 一致。
- 三个严格 validator：均 exit 0，状态见上表。
- 精确 marker 计数：8 个关键观察各 1 次，行号严格递增。
- 结束后复核：无 `qemu-system-aarch64` 残留；`/tmp/axgdtb-a969ddb934f29bc8041a02937ba2ade5` 不存在。

## 下一步

1. 用独立 nonce/pattern 与前后 hash 做一个受控 DMA 效果探针；该单次探针不得表述为通用 DMA 隔离，真正的强隔离仍需要 IOMMU/SMMU 或 mediated/bounce backend。
3. 将 Linux 与 Zephyr 配置从 `blocked_dma_console` 提升到可运行双 Guest 拓扑，先做短 smoke，再做 30 分钟稳定性。
4. 在独立 VirtIO 网卡、固定 MAC/IPv4 和同一 QEMU hub 上依次完成 ping、UDP echo、TCP；TCP/UDP/IP 仍是必需的 Guest 主数据链路。
