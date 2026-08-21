# AxVisor 固定 HPA 与宿主预留内存设计

状态：Phase A/B、Phase C 单 Guest Host carveout/allocator/`MapReserved`/Linux shell，以及 Host DMA-guard allocator/runtime marker 观察已完成；真实 DMA effect/isolation、双 Guest 与 IP 仍阻塞
基线：`rcore-os/tgoskits` `dev`，`01e053105e243d1fdd1c03f6f850b266efbe8de0`
风险级别：高风险（宿主 allocator、固定物理内存、stage-2 页表与 passthrough DMA）

## 1. 结论

Zephyr 不能在当前 `MapAlloc` 内存上直接启用 passthrough virtio-net。Guest GPA
`0x4000_0000` 会映射到运行时分配的任意 HPA，而外层设备 DMA 地址仍按 Guest
看到的物理地址发出；没有 IOMMU 或 bounce backend 时，两者不一致会访问错误的
宿主物理内存。

本阶段选择“宿主启动前静态预留 + `MapReserved` 固定映射”，但必须分成两个
独立条件：

1. 宿主 FDT 在 allocator 初始化前把专用 HPA carveout 从普通 RAM 中排除；
2. AxVM 只把已证明属于该专用 carveout 的 HPA 映射给指定 VM。

跨 VM `PhysicalResourceLease` 只能防止两个 VM 同时声明同一 HPA，不能证明该 HPA
没有被 AxVisor 内核、per-CPU 数据或全局 allocator 使用。只改 VM TOML、Guest DTB
或 resource claim 都不能关闭 allocator ownership gate。

## 2. 当前实现审计

### 2.1 `MapReserved` 不是“预留”操作

历史实现中的 `virtualization/axvm/src/vm/mod.rs::map_reserved_memory_region()` 直接建立
GPA=HPA 的 stage-2 映射，并把 `needs_dealloc` 设为 `false`，既不调用宿主 allocator
预留页，也不验证 HPA 区间的宿主归属。当前 Phase A/B 已把这两项拆成显式合同：
someboot 在 allocator 初始化前解析专用 carveout，AxVM 在映射前只接受
`VM ID + HPA start + size` 完全相等的不可变 manifest 条目。

更具体地说，历史实现把 `hva` 也写成 `gpa`。当宿主启用重定位或 direct-map 不是
identity 时，`VMMemoryRegion::hpa()` 会再次执行 `virt_to_phys(hva)`，从而得到错误
HPA。这个地址转换缺陷可以独立修复为：

```text
hpa = HostPhysAddr(gpa)
hva = host.phys_to_virt(hpa)
stage-2: gpa -> hpa
```

当前实现已采用上述转换，并把授权放在 `phys_to_virt`、stage-2 修改和 region 登记
之前。它仍不证明某次真实启动确实使用了目标 DTB，也不证明 allocator 运行时 FREE
列表已经排除该范围。

### 2.2 allocator 已在 VM 创建前接管 FREE RAM

启动链由 `someboot` 解析宿主 FDT，`axplat-dyn` 再把 `MemoryType::Free` 暴露为
`phys_ram_ranges()`，把 `Reserved/KImage/PerCpuData` 聚合为
`reserved_phys_ram_ranges()`。`axhal` 从 RAM 中扣除这些 reserved ranges 后初始化
allocator。VM 配置被解析时，这个过程已经完成，因而 VM 创建路径不能再靠
“声明 MapReserved”把一段 FREE RAM 安全取回。

Devicetree Specification 的 `/reserved-memory` 规则要求操作系统排除所描述的
物理区间，并允许静态子节点用 `reg` 指定地址和大小：

- https://devicetree-specification.readthedocs.io/en/latest/chapter3-devicenodes.html#reserved-memory-node

当前 `someboot/src/fdt/memory.rs` 同时解析 FDT reservation block 和
`/reserved-memory` 子节点，并在 allocator 初始化前记录为 `MemoryType::Reserved`。
这正是固定 carveout 应进入的时序边界。

### 2.3 不能直接恢复 `alloc_pages_at`

本仓库提交 `8f0e568a2` 移除了 AxVisor/ArceOS 对 `ax-allocator` bitmap page
allocator 的依赖，切换到 TLSF/buddy-slab 后，`alloc_pages_at` 只剩
`unimplemented!()`。直接恢复固定地址分配需要重新设计 allocator 元数据、并发、
碎片与回收语义，且仍不能可靠区分 VM carveout 与宿主关键区，不是本阶段的最小
安全方案。

### 2.4 “任意 RESERVED”仍然不够

`axplat-dyn::reserved_phys_ram_ranges()` 当前还包含 `KImage` 和 `PerCpuData`。因此
简单检查目标范围是否落入 `RESERVED` 会错误地允许 VM 映射 AxVisor 内核或
per-CPU 数据。安全检查必须识别“专门授予 VM 的 carveout”，不能只检查通用
`MemRegionFlags::RESERVED`。

## 3. 目标与非目标

### 3.1 成功标准

- 固定 HPA 在宿主 allocator 初始化前从 FREE RAM 中排除；
- carveout 有稳定、可审计的 VM 归属标识，且与配置的 `MapReserved` 起点和大小完全相等；
- VM 创建时拒绝 FREE、MMIO、KImage、PerCpuData、普通 reserved 或跨边界范围；
- AxVM 记账中的 HVA 由宿主 `phys_to_virt(HPA)` 得到；
- 两个 VM 的固定 HPA、passthrough MMIO、物理 IRQ 与 pCPU claim 不重叠；
- 当前 QEMU head 上保存宿主 memory map、allocator 排除证据和 stage-2 查询证据；
- 只有 DMA smoke 和 Guest 间 IP 证据通过后，才关闭 `blocked_dma_console` 中的
  DMA 子门槛。

### 3.2 非目标

- 本设计不实现 IOMMU/SMMU、DMA remapping 或 bounce buffer；
- 不证明设备 stream ID、缓存一致性、reset/quiesce 或中断隔离；
- 不恢复通用 `alloc_pages_at`；
- 不把控制台、HyperCall、共享内存、原始 MMIO 或 `vsock` 作为 Guest 间通信；
- 不在地址尚未由 QEMU/FDT 实证前硬编码 `0x1_0000_0000` 为最终 carveout；
- 不把固定 HPA 所有权证明升级为双 Guest 启动或 IP 通信证明。

## 4. 方案比较

| 方案 | allocator 安全 | DMA 地址一致性 | 复杂度 | 结论 |
|---|---|---|---|---|
| 保持 Zephyr `MapAlloc` 并启用 passthrough | 否 | 否 | 低 | 拒绝 |
| VM 启动时调用 `alloc_pages_at` | 取决于重做 allocator | 可以 | 高 | 当前后端不支持，拒绝 |
| 宿主 FDT 静态预留 + 泛化 `RESERVED` 检查 | 部分 | 可以 | 中 | 会误接纳 KImage/PerCpu，拒绝 |
| 宿主 FDT 专用 VM carveout + 精确归属检查 | 是 | 可以 | 中 | 采用 |
| emulated/bounce virtio-net | 是 | 可转换 | 高 | 长期替代方案 |
| IOMMU/SMMU | 是 | 可重映射 | 很高 | 真机长期方案 |

## 5. 选定的所有权合同

### 5.1 宿主 FDT

QEMU runner 应先获得与最终 machine 参数完全一致的宿主 DTB，再生成一个只新增
专用 carveout 的派生 DTB。静态节点必须：

- 位于 `/reserved-memory`；
- 使用固定 `reg = <HPA SIZE>`，不用动态 `size` 分配；
- 具备项目专用 compatible，例如 `axvisor,vm-carveout-v1`；
- 具备唯一 VM ID 属性，例如 `axvisor,vm-id = <2>`；
- 不带 `reusable`；是否使用 `no-map` 必须由宿主 direct-map 访问需求决定，不能
  在未验证 `phys_to_virt` 行为时直接启用；
- 地址、大小至少 2 MiB 对齐，并完整位于 QEMU 报告的 RAM；
- 与宿主 kernel、FDT、initrd、per-CPU、其他 carveout 和设备 MMIO 不重叠。

QEMU `virt` 的 `highmem` 与 `compact-highmem` 会影响 32 位以上区域布局，因此候选
`0x1_0000_0000` 只能通过当前固定 QEMU 版本和实际 `dumpdtb` 验证，不能按经验
直接使用：

- https://qemu.readthedocs.io/en/v8.2.10/system/arm/virt.html

### 5.2 AxVM 运行时

建议增加只读的宿主能力边界，而不是让 `axvm` 任意访问平台内部 memory map：

```text
HostVmCarveouts::find_vm_carveout(vm_id, hpa, size)
    -> exact owned carveout | typed rejection
```

该能力应从保存的宿主 FDT 解析项目专用属性，或由启动层在 allocator 初始化后生成
不可变 manifest。它必须区分：专用 VM carveout、普通 reserved、KImage、
PerCpuData、MMIO 与 FREE RAM。

`MapReserved` 准备顺序为：

1. 校验非零、2 MiB 对齐、地址加法不溢出；
2. 用 VM ID、HPA 起点和大小查找一个三元组完全相等的专用 carveout；
3. 确认 `PhysicalResourceLease` 仍持有同一 HPA claim；
4. 计算 `hpa = gpa` 与 `hva = host.phys_to_virt(hpa)`；
5. 建立 stage-2 `gpa -> hpa`；
6. 记录不可释放的 `VMMemoryRegion`；
7. VM 销毁时只 unmap，不把 carveout 交回全局 allocator。

任一步失败都不得留下 stage-2 映射或 `memory_regions` 条目。

## 6. Zephyr 迁移

当前 `zephyr-smp1-dual.toml` 保持 `MapAlloc` 和 `blocked_dma_console`，不能仅把
map type 改为 `MapReserved`。迁移前必须：

1. 从当前 QEMU `dumpdtb` 和启动 memory map 选择实际空闲且可预留的 128 MiB HPA；
2. 生成并校验宿主派生 DTB；
3. 用 overlay 重链接 Zephyr `zephyr,sram`、链接地址和镜像 load/entry；
4. 把 VM 的 GPA/HPA 合同同步为该 carveout；
5. 保存 `zephyr.dts`、`.config`、ELF/BIN 与哈希；
6. 先运行无 passthrough 的单 Guest memory smoke；
7. 再运行 virtio-net DMA smoke，最后才进入双 Guest IP 测试。

## 7. 分阶段实现与验证

### Phase A：独立地址转换修复

- [x] 为 `MapReserved` 增加会在 identity-HVA 旧实现上失败的静态/单元契约；
- [x] `hva` 改为 `host.phys_to_virt(hpa)`；
- 不改变任何双 Guest 配置，不声称 allocator ownership 已完成。

### Phase B：专用 carveout 解析与 fail-closed 校验

- [x] 先为错误 VM ID、部分覆盖、普通 reserved、FREE、KImage、PerCpu、MMIO、重叠和
  溢出写失败测试；
- [x] 增加 someboot 专用 parser/预留注入和最小只读 host capability；普通 merged
  reserved ranges 不进入授权 manifest；
- [x] 缺少专用标识、VM ID/HPA/size 任一不精确匹配时 `MapReserved` 必须失败；拒绝
  发生在 `phys_to_virt`、stage-2 修改和 `memory_regions.push` 之前。

### Phase C：QEMU/Zephyr 实证

- 固定 QEMU 版本、完整参数与派生 DTB；使用
  `prepare_host_vm_carveout_dtb.py` 从同一 machine 参数的原始 `dumpdtb`
  生成专用节点，并保存 `host-carveout.dtb`、解码 DTS、preflight 与
  `derivation.json`。该 manifest 的 `prepared-unlaunched` 仅证明字节绑定的
  静态派生，不能证明 QEMU 实际使用它；
- 以 `run_live_guest_dtb_capture.py --host-dtb <derived>` 做单 Guest 启动时，
  QEMU 配置和已启动进程 argv 都必须含唯一且完全相同的独立
  `-dtb <derived>` 参数。它把 DTB 的路径、大小和 hash 绑定到该一次
  launcher/READY/QMP-capture/退出流程，却不证明 reservation 到达早期启动或
  allocator 排除。该 harness 的成功 `single_guest_live_capture_passed` 是
  runtime-marker validator 实际消费的 capture status，而不是另造一个虚构的
  专用成功状态；
- 启用默认关闭的 `host-carveout-allocator-evidence` 后，保存严格的
  `AXVISOR_HOST_VM_CARVEOUT_RESERVED ... phase=before-ram-init` 与
  `AXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED ... reserved_cover=1
  free_overlap=0 phase=before-global-allocator-init` 日志。前者是早期 Reserved
  注入观察，后者是全局 allocator 初始化前的 Reserved-cover/FREE-overlap
  观察；二者须由 `validate_host_carveout_runtime_log.py` 与同字节的 preflight、
  Host DTB/DTS、唯一 harness VM TOML、raw log 及真实成功的
  `single_guest_live_capture_passed` status 联合 fail-closed 校验。校验结果只
  证明这些已绑定的文字观察，不能自行推断 Guest 或 DMA 行为；
- 独立的 `stage2-hpa-evidence` 只在后端 VM exit 已返回 AxVM 后查询一页
  stage-2；其中 `kind=reserved`、`identity=1`、`gpa=hpa` 才是一个
  `MapReserved` 的后期 stage-2 观察。它不由上述 allocator-marker validator
  产生或替代；
- DMA 前后对目标缓冲做 nonce/hash 校验；
- 保存失败注入：移除 reservation、改 VM ID、缩短范围时 VM 必须拒绝准备；
- 最后单独验证 Linux 与 Zephyr 的 TCP/UDP/IP 通信。

## 8. 回滚

- Phase A 地址转换修复可独立回滚，但回滚后不得使用 relocated host 的
  `MapReserved`；
- Phase B 失败时移除 contest carveout 配置并继续 `MapAlloc` headless smoke；
- Phase C 失败时保留证据，恢复 `blocked_dma_console`，不得用根节点透传、共享 UART
  或非 IP 通道绕过；
- 宿主 FDT 派生文件必须按 run 生成，不覆盖仓库中的原始 QEMU DTB。

## 9. 当前完成边界

Phase A/B 已完成源码、host 单测、严格 Clippy 和 AArch64 target check：专用
`axvisor,vm-carveout-v1` parser 会在 allocator 初始化前注入 Reserved，独立只读
manifest 经 axplat/axhal 暴露，AxVM 只授权 VM ID/HPA/size 完全相等的 `MapReserved`。
静态 DTS/TOML validator 也已落地，但它不证明 QEMU 实际使用该 DTB。

Phase C 的 r20 已完成 host marker runtime 验证：`host-carveout-runtime.json`
给出 `host_carveout_markers_match_static_preflight`，并把 Host DTB、唯一 VM
TOML、raw log 和 preflight 字节绑定到真实
`single_guest_live_capture_passed` capture status。这个结果只覆盖已绑定的
host marker 文字观察，不证明 Guest boot、`MapReserved` stage-2、passthrough
DMA、dual-Guest 或 Guest IP。

Guest DTB 同样有独立边界：不得继承 Host `/reserved-memory`，即使 root
passthrough 被选择。`virtualization/axvm/src/boot/fdt/core/create.rs` 会先过滤
该路径；generated-DTB、runtime-patch 和 near-path 回归分别覆盖过滤、后续修补
和不会误删相似名称节点的行为。

r21 已在同一 identity-bound QEMU 运行中进一步关闭单 Guest 底层 gate：Host
`RESERVED`、allocator `reserved_cover=1/free_overlap=0`、Guest-DTB READY、
`MapReserved` 的 `gpa=hpa/identity=1`、Linux 启动、Guest DT 无
`/reserved-memory`、`/bin/sh` 与唯一 `~ #` 均按序出现。捕获 DTB 的两次独立
`dtc` 解码逐字节一致，Guest-DTB、stage-2 和 Host-carveout 三份 validator 均通过，
结束后无 QEMU 或会话目录残留。

该结果允许把“单 Guest Host carveout/allocator/MapReserved/Linux shell”标为运行时
完成，但没有 Zephyr 重链接、DMA nonce/hash、双 Guest 共存、30 分钟稳定性或
TCP/UDP/IP 实证。因此 `blocked_dma_console` 的 DMA/双 Guest gate 仍保持阻塞，
不能把 r21 扩大解释为 DMA-safe 或网络闭环。

### r22：Host DMA guard 静态底座

r22 在 VM 1 carveout 之外准备了独立的
`dma-guard@180000000`（2 MiB、2 MiB 对齐、`no-map`、无 VM ID）。解析器将
guard 与 VM-owned manifest 分离，拒绝动态 reservation、损坏 RAM、范围溢出、
不对齐、跨 RAM、与 memreserve/普通 reserved/VM carveout/其他 guard 重叠、
重复或非精确属性；在 early memory setup 发布前读取 manifest 会 fail-closed。
平台 API 只传播不可变 HPA/size，axruntime 则在首次 global allocator 初始化前
验证 Reserved 完整覆盖和 FREE 零重叠，成功时才输出独立 guard marker。

`prepared-dma-guard-r22` 的派生 DTB SHA-256 为
`ffea82e11c318d02243a14c5deea9434fe89ff313878fad336ce0cd2147084ab`，两份
preflight 均为
`c5f7a5a36f756e6289009a027840c9881918fe8299e4184402859a8297ad072d` 且字节
一致。r22 本身保持为静态派生；2026-08-10 当前工作树的 30/30 Python 合同、
DMA-guard Rust fmt/test/production-Clippy、正式 AArch64 AxBuild 和随后 r23 真实
QEMU 运行均已完成。

这个 guard 只建立受控 DMA-effect 探针所需的 Host allocator 保留区。它没有
IOMMU/SMMU domain、DMA remapping、mediated device 或 bounce backend，不能被
描述为“已隔离 DMA”。后续即使单次 nonce/hash 效果探针成功，也只能证明该次
请求/会话的期望效果；通用 DMA 隔离仍需独立的硬件或中介后端。

### r23：Host DMA guard allocator/runtime 观察

r23 使用 r22 派生 DTB 启动一个 Linux Guest，状态为
`single_guest_live_capture_passed`，128-bit session nonce 为
`7a0d4d836a6eb047403dd2ce2da0c934`。同一次 identity-bound QEMU/QMP capture
chain 中，guard `0x180000000:0x200000` 的
`AXVISOR_HOST_DMA_GUARD_RESERVED` 出现在 raw log line 118，
`AXVISOR_HOST_DMA_GUARD_ALLOCATOR_EXCLUDED reserved_cover=1 free_overlap=0`
出现在 line 195，均早于 READY（line 284）。Host report 为
`host_carveout_and_dma_guard_markers_match_static_preflight`，SHA-256 为
`e3e0969ed8bb64bbb5b7fa491d70bcb2cce409e7ed1c6f32e116d3f0c4d10162`；联合
single-Guest Linux boot report 为
`linux_map_reserved_single_guest_boot_chain_observed`，SHA-256 为
`882bd47385713d1515fd5fb6beb8c24711ba2ffebe0651dc332cbc3d49b3d88b`。

联合报告还把 `capture-chain.json`（SHA-256
`21c61f35d8987966a4a1936a38592d921412d1f66673aee4f31971c32a0eadf9`）与
status nonce、QEMU name 和 chain 内唯一 VM 1 final Guest-DTB 逐字节绑定；它同样
绑定 r22 Host DTB、VM TOML、stage-2 reserved identity、Linux init shell 与唯一
`~ #`。QEMU 和 runtime session directory 均已确认无残留。

该结果的严格边界是“一个已配置 guard 的 Host allocator/runtime marker observation”。
它没有提交或观测任何设备 DMA 请求，不能证明 payload 改写、设备总线访问范围、
IOMMU/SMMU domain、通用 DMA isolation、双 Guest 或 Guest IP。

### Controlled DMA-effect probe：实现与证据边界

这一探针解决的具体缺口是：r23 只证明 guard 在 Host allocator 初始化前被排除，
没有提交设备请求，也没有观察请求前后的 payload/guard 字节。成功标准限定为一个
disposable QEMU 会话中的一次 Linux Guest `O_DIRECT` 只读：READY 指出的 512-byte
buffer 在请求前必须精确为 `0xa5 * 512`，GO 以后必须等于绑定的只读 probe sector，
而独立 guard 在前后两次 capture 中逐字节相同。guard 改变只能发布
out-of-bounds observation，不能发布成功。

采用用户态 helper，而不是新增内核模块或直接构造 virtqueue descriptor，原因是它
能复用当前 Linux Guest 和 virtio-blk 驱动，并把改动限制在 disposable rootfs 与
host-side evidence tooling。代价是 Linux block layer 可能使用 bounce/copy 路径，
所以即使字节观察成功，也只能称为“一次 Guest-issued `O_DIRECT` read 的受控字节
效应”，不能证明 descriptor 直接指向该页或由硬件 DMA engine 写入。若比赛最终
要求 descriptor/HPA 级因果或通用隔离，仍需专用内核 probe、mediated/bounce backend
或 IOMMU/SMMU 方案。

语义依据与本地约束如下：

- OASIS VirtIO 1.3 §5.2 规定 `VIRTIO_BLK_T_IN` 用读取的 sector 内容填充 data，
  且 read data 长度必须是 512 bytes 的整数倍；本探针只使用 sector 0 的一个
  512-byte request：<https://docs.oasis-open.org/virtio/virtio/v1.3/virtio-v1.3.html>；
- Linux `O_DIRECT` 的地址、长度与 offset 对齐规则随设备/内核而变；helper 使用
  page-aligned buffer、512-byte length/offset，并把 `open`/`pread` 失败作为失败，
  但不把 `O_DIRECT` 本身解释为 DMA 证明：
  <https://man7.org/linux/man-pages/man2/open.2.html>；
- Linux pagemap 的 PFN 位为 0--54，present 为 bit 63、swapped 为 bit 62；没有
  `CAP_SYS_ADMIN` 时 PFN 可能被清零，因此 helper 在 PFN 为零时 fail-closed：
  <https://www.kernel.org/doc/html/v5.5/admin-guide/mm/pagemap.html>；
- QEMU QMP `pmemsave` 保存的是外层 QEMU Guest physical memory；这里的外层 Guest
  正是 AxVisor Host，因此 manifest 将该地址空间标为 Host physical。runner 只在
  `query-name` 身份绑定且 `query-status=paused` 的两个独立窗口调用 `pmemsave`：
  <https://www.qemu.org/docs/master/interop/qemu-qmp-ref.html>。

实现分成五个 no-overwrite 工件边界：

1. `guest_virtio_blk_odirect_probe.c` pin/touch buffer，从 `/proc/self/pagemap` 得到
   GPA，输出 nonce-bound READY，只接受 launcher stdin 的精确 `GO <nonce>\n`，
   完成一次 `pread` 后输出 DONE 并保持 buffer 存活；
2. `prepare_disposable_virtio_dma_rootfs.py` 只复制 cache rootfs，在新 copy 注入
   helper 与前台 `/init`，不修改共享 cache；
3. `prepare_virtio_dma_effect_qemu.py` 生成 deterministic 512-byte probe disk，
   root device 固定在 `virtio-mmio-bus.0`，只读 probe device 固定在 bus 1；
4. `prepare_virtio_dma_effect_request.py` 将 nonce、QEMU/build/VM 配置、只读
   device/drive/bus/Guest path、identity `MapReserved` payload GPA/HPA 与 guard
   绑定，并只允许两组 stop/pmemsave/cont capture；
5. `validate_virtio_dma_effect_probe.py` 独立重验 raw log 中唯一 READY/DONE、精确
   GO、两个 paused window、四份 capture 的大小/hash/顺序和最终字节关系。

截至本文档更新时，helper、disposable rootfs、QEMU topology、request 与 validator
合同及其对抗性负例已经进入 CI；identity-bound live runner 与真实四份物理 capture
仍在推进，尚无 probe result。因此不得以 r23 或这些 prepared artifact 解除
`blocked_dma_console`，也不得据此声称 DMA isolation、双 Guest 或 Guest IP 完成。
