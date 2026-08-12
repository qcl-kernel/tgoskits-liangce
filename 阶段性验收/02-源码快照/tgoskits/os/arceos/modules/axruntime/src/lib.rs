// Copyright 2025 The Axvisor Team
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

//! Runtime library of [ArceOS](https://github.com/arceos-org/arceos).
//!
//! Any application uses ArceOS should link this library. It does some
//! initialization work before entering the application's `main` function.
//!
//! # Cargo Features
//!
//! - `paging`: Enable page table manipulation support.
//! - `irq`: Enable interrupt handling support.
//! - `multitask`: Enable multi-threading support.
//! - `smp`: Enable SMP (symmetric multiprocessing) support.
//! - `fs`: Enable filesystem support.
//! - `net`: Enable networking support.
//! - `display`: Enable graphics support.
//!
//! All the features are optional and disabled by default.

#![feature(extern_item_impls)]
#![cfg_attr(not(test), no_std)]
#![allow(missing_abi)]

#[macro_use]
extern crate ax_log;

extern crate ax_driver as _;

#[cfg(all(target_os = "none", not(feature = "std-compat"), not(test)))]
mod lang_items;
#[cfg(all(
    feature = "stack-protector",
    any(target_os = "none", target_env = "musl"),
    not(test)
))]
mod stack_protector;

#[cfg(feature = "smp")]
mod mp;

mod klib;

mod devices;
mod fs;
#[cfg(feature = "irq")]
pub mod irq;
mod registers;
#[cfg(feature = "serial")]
pub mod serial;

#[cfg(all(feature = "net", feature = "fs"))]
mod unix_ns;

#[cfg(feature = "aic8800-wifi")]
mod wifi_glue;

pub use ax_hal as hal;

pub(crate) mod build_info {
    include!(concat!(env!("OUT_DIR"), "/build_info.rs"));
}

#[cfg(feature = "smp")]
pub use self::mp::rust_main_secondary;

extern crate alloc;

#[cfg(feature = "fs")]
pub(crate) fn runtime_default_task_stack_size() -> usize {
    build_info::TASK_STACK_SIZE
}

#[cfg(feature = "irq")]
fn ticks_per_sec() -> u64 {
    build_info::TICKS_PER_SEC as u64
}

const LOGO: &str = r#"
       d8888                            .d88888b.   .d8888b.
      d88888                           d88P" "Y88b d88P  Y88b
     d88P888                           888     888 Y88b.
    d88P 888 888d888  .d8888b  .d88b.  888     888  "Y888b.
   d88P  888 888P"   d88P"    d8P  Y8b 888     888     "Y88b.
  d88P   888 888     888      88888888 888     888       "888
 d8888888888 888     Y88b.    Y8b.     Y88b. .d88P Y88b  d88P
d88P     888 888      "Y8888P  "Y8888   "Y88888P"   "Y8888P"
"#;

#[eii]
fn ax_app_entry() {
    #[cfg(not(test))]
    unsafe extern "C" {
        /// Legacy application's entry point.
        safe fn main();
    }
    // Default implementation
    #[cfg(not(test))]
    main();
}

struct LogIfImpl;

#[cfg(feature = "paging")]
fn runtime_page_fault_handler(
    addr: ax_memory_addr::VirtAddr,
    flags: ax_hal::trap::PageFaultFlags,
) -> bool {
    #[cfg(feature = "stack-guard-page")]
    if ax_task::diagnose_current_stack_guard_page_fault(addr) {
        return false;
    }

    ax_mm::kernel_aspace().lock().handle_page_fault(addr, flags)
}

#[ax_crate_interface::impl_interface]
impl ax_log::LogIf for LogIfImpl {
    fn console_write_str(s: &str) {
        #[cfg(feature = "serial")]
        if serial::route_console_bytes(s.as_bytes()).is_some() {
            return;
        }
        ax_hal::console::write_text_bytes(s.as_bytes());
    }

    fn current_time() -> core::time::Duration {
        ax_hal::time::monotonic_time()
    }

    fn current_cpu_id() -> Option<usize> {
        #[cfg(feature = "smp")]
        if is_init_ok() {
            Some(ax_hal::percpu::this_cpu_id())
        } else {
            None
        }
        #[cfg(not(feature = "smp"))]
        Some(0)
    }

    fn current_task_id() -> Option<u64> {
        if is_init_ok() {
            #[cfg(feature = "multitask")]
            {
                ax_task::current_may_uninit().map(|curr| curr.id().as_u64())
            }
            #[cfg(not(feature = "multitask"))]
            None
        } else {
            None
        }
    }
}

use core::sync::atomic::{AtomicUsize, Ordering};

/// Number of CPUs that have completed initialization.
static INITED_CPUS: AtomicUsize = AtomicUsize::new(0);

fn is_init_ok() -> bool {
    INITED_CPUS.load(Ordering::Acquire) == ax_hal::cpu_num()
}

/// The main entry point of the ArceOS runtime.
///
/// It is called from the bootstrapping code in the specific platform crate (see
/// [`ax_plat::main`]).
///
/// `cpu_id` is the logic ID of the current CPU, and `arg` is passed from the
/// bootloader (typically the device tree blob address).
///
/// In multi-core environment, this function is called on the primary core, and
/// secondary cores call [`rust_main_secondary`].
#[cfg_attr(not(test), ax_plat::main)]
pub fn rust_main(cpu_id: usize, arg: usize) -> ! {
    ax_hal::percpu::init_primary(cpu_id);
    // After per-CPU init, before scheduler/IPI/IRQ paths can allocate.
    // This is a no-op for allocator backends that do not need per-CPU state.
    ax_alloc::init_percpu_slab(cpu_id);
    ax_hal::init_early(cpu_id, arg);
    let log_level = option_env!("AX_LOG").unwrap_or("info");

    ax_println!("{}", LOGO);
    ax_println!(
        indoc::indoc! {"
            arch = {}
            platform = {}
            target = {}
            build_mode = {}
            log_level = {}
            backtrace = {}
            smp = {}
        "},
        build_info::ARCH,
        hal::platform_name(),
        build_info::TARGET,
        build_info::MODE,
        log_level,
        axbacktrace::is_enabled(),
        ax_hal::cpu_num()
    );

    ax_log::init();
    ax_log::set_max_level(log_level); // no effect if set `log-level-*` features
    info!("Logging is enabled.");
    info!("Primary CPU {cpu_id} started, arg = {arg:#x}.");

    info!("Found physcial memory regions:");
    for r in ax_hal::mem::memory_regions() {
        info!(
            "  [{:x?}, {:x?}) {} ({:?})",
            r.paddr,
            r.paddr + r.size,
            r.name,
            r.flags
        );
    }

    init_allocator();

    let (kernel_space_start, kernel_space_size) = ax_hal::mem::kernel_aspace();

    {
        use core::ops::Range;

        unsafe extern "C" {
            safe static _stext: [u8; 0];
            safe static _etext: [u8; 0];
        }

        let fp_range_start = kernel_space_start.as_usize();
        let fp_range_end = fp_range_start.saturating_add(kernel_space_size);
        axbacktrace::init(
            Range {
                start: _stext.as_ptr() as usize,
                end: _etext.as_ptr() as usize,
            },
            Range {
                start: fp_range_start,
                end: fp_range_end,
            },
        );
    }

    info!(
        "kernel aspace: [{:#x?}, {:#x?})",
        kernel_space_start,
        kernel_space_start + kernel_space_size,
    );

    #[cfg(feature = "paging")]
    {
        ax_mm::init_memory_management();
        ax_hal::trap::set_page_fault_handler(runtime_page_fault_handler);
    }

    info!("Initialize platform devices...");
    ax_hal::init_later(cpu_id, arg);
    if rdrive::is_initialized() {
        registers::append_linker_registers();
        #[cfg(feature = "irq")]
        ax_hal::irq::init_boot_irqs(cpu_id)
            .unwrap_or_else(|err| panic!("failed to initialize boot IRQs: {err:?}"));
        #[cfg(not(feature = "irq"))]
        rdrive::probe_pre_kernel()
            .unwrap_or_else(|err| panic!("failed to run pre-kernel driver probes: {err:?}"));
    } else {
        warn!("rdrive is not initialized; skip pre-kernel driver probe");
    }

    #[cfg(feature = "multitask")]
    ax_task::init_scheduler();

    #[cfg(feature = "ipi")]
    {
        ax_ipi::init();
        #[cfg(feature = "irq")]
        ax_hal::irq::set_run_on_cpu_sync(ax_ipi_run_on_cpu_sync);
    }

    #[cfg(feature = "irq")]
    {
        info!("Initialize interrupt handlers...");
        init_interrupt();
    }

    // Install the ArceOS runtime glue into the OS-independent Wi-Fi driver
    // cores (aic8800 / sdhci-cv1800) *before* probing, since the FDT probe
    // brings the chip up and that needs timing/task capabilities. The cores
    // declare no ArceOS dependency themselves; this is the adapter layer (see
    // `wifi_glue`).
    #[cfg(feature = "aic8800-wifi")]
    wifi_glue::install_runtime();

    devices::probe_all_devices();

    #[cfg(feature = "serial")]
    serial::init(cpu_id);

    #[cfg(feature = "rtc")]
    ax_println!(
        "Boot at {}\n",
        chrono::DateTime::from_timestamp_nanos(ax_hal::time::wall_time_nanos() as _),
    );

    fs::init(ax_hal::boot::bootargs());

    #[cfg(feature = "display")]
    devices::init_display();

    #[cfg(feature = "input")]
    devices::init_input();

    #[cfg(feature = "net")]
    devices::init_net();

    #[cfg(feature = "vsock")]
    devices::init_vsock();

    #[cfg(feature = "smp")]
    self::mp::start_secondary_cpus(cpu_id);

    #[cfg(all(feature = "tls", not(feature = "multitask")))]
    {
        info!("Initialize thread local storage...");
        init_tls();
    }

    ax_ctor_bare::call_ctors();

    info!("Primary CPU {cpu_id} init OK.");
    INITED_CPUS.fetch_add(1, Ordering::Release);

    while !is_init_ok() {
        core::hint::spin_loop();
    }

    #[cfg(all(feature = "irq", feature = "ipi"))]
    ax_ipi::wait_for_all_cpus_ready();

    ax_app_entry();

    #[cfg(feature = "multitask")]
    ax_task::exit(0);
    #[cfg(not(feature = "multitask"))]
    {
        debug!("main task exited: exit_code={}", 0);
        ax_hal::power::system_off();
    }
}

fn init_allocator() {
    use ax_hal::mem::{MemRegionFlags, memory_regions, phys_to_virt};

    info!("Initialize global memory allocator...");
    info!("  use {} allocator.", ax_alloc::global_allocator().name());

    // The page allocator (which backs user-space page population via
    // `alloc_pages`) is initialized from a single contiguous region by
    // `global_init`; every other free region is handed to the byte/heap
    // allocator by `global_add_memory` (the bitmap page allocator does not
    // support `add_memory`). So the region chosen for `global_init` *is* the
    // entire pool available for user memory.
    //
    // Pick the LARGEST free region for the page allocator. Platforms with a
    // single contiguous RAM region (x86/aarch64/riscv64 qemu-virt) are
    // unaffected (largest == the only region). Platforms with disjoint regions
    // (loongarch64 qemu-virt: a small ~248 MB low region below the MMIO hole
    // plus the multi-GB high region at 0x8000_0000) previously picked the small
    // low region — the "first free region after .bss" heuristic — which capped
    // all user allocations at ~248 MB regardless of total RAM, OOM'ing large
    // workloads (e.g. the gradle build JVM) even with gigabytes free.
    let mut max_region_size = 0;
    let mut max_region_paddr = 0.into();

    for r in memory_regions() {
        if r.flags.contains(MemRegionFlags::FREE) && r.size > max_region_size {
            max_region_size = r.size;
            max_region_paddr = r.paddr;
        }
    }

    #[cfg(feature = "host-carveout-allocator-evidence")]
    emit_host_vm_carveout_allocator_evidence();

    #[cfg(feature = "host-dma-guard-allocator-evidence")]
    emit_host_dma_guard_allocator_evidence();

    for r in memory_regions() {
        if r.flags.contains(MemRegionFlags::FREE) && r.paddr == max_region_paddr {
            ax_alloc::global_init(phys_to_virt(r.paddr).as_usize(), r.size)
                .expect("initialize global allocator failed");
            break;
        }
    }

    for r in memory_regions() {
        if r.flags.contains(MemRegionFlags::FREE) && r.paddr != max_region_paddr {
            ax_alloc::global_add_memory(phys_to_virt(r.paddr).as_usize(), r.size)
                .expect("add heap memory region failed");
        }
    }
}

#[cfg(feature = "host-carveout-allocator-evidence")]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CarveoutAllocatorEvidenceError {
    CarveoutRangeOverflow { vm_id: u32 },
    RegionRangeOverflow,
    MissingReservedCover { vm_id: u32 },
    FreeRegionOverlap { vm_id: u32 },
}

#[cfg(any(
    feature = "host-carveout-allocator-evidence",
    feature = "host-dma-guard-allocator-evidence"
))]
fn range_end(start: usize, size: usize) -> Option<usize> {
    start.checked_add(size)
}

#[cfg(any(
    feature = "host-carveout-allocator-evidence",
    feature = "host-dma-guard-allocator-evidence"
))]
fn ranges_overlap(
    left_start: usize,
    left_end: usize,
    right_start: usize,
    right_end: usize,
) -> bool {
    left_start < right_end && right_start < left_end
}

#[cfg(feature = "host-carveout-allocator-evidence")]
fn validate_host_vm_carveout_allocator_evidence<
    C,
    R,
    Regions,
    RegionIter,
    CarveoutRange,
    RegionRange,
>(
    carveouts: impl IntoIterator<Item = C>,
    regions: Regions,
    carveout_range: CarveoutRange,
    region_range: RegionRange,
) -> Result<(), CarveoutAllocatorEvidenceError>
where
    C: Copy,
    R: Copy,
    Regions: Fn() -> RegionIter,
    RegionIter: IntoIterator<Item = R>,
    CarveoutRange: Fn(C) -> (u32, usize, usize),
    RegionRange: Fn(R) -> (usize, usize, bool, bool),
{
    for carveout in carveouts {
        let (vm_id, carveout_start, carveout_size) = carveout_range(carveout);
        let carveout_end = range_end(carveout_start, carveout_size)
            .ok_or(CarveoutAllocatorEvidenceError::CarveoutRangeOverflow { vm_id })?;
        let mut reserved_cover = false;

        for region in regions() {
            let (region_start, region_size, is_reserved, is_free) = region_range(region);
            let region_end = range_end(region_start, region_size)
                .ok_or(CarveoutAllocatorEvidenceError::RegionRangeOverflow)?;
            if is_reserved && region_start <= carveout_start && carveout_end <= region_end {
                reserved_cover = true;
            }
            if is_free && ranges_overlap(carveout_start, carveout_end, region_start, region_end) {
                return Err(CarveoutAllocatorEvidenceError::FreeRegionOverlap { vm_id });
            }
        }

        if !reserved_cover {
            return Err(CarveoutAllocatorEvidenceError::MissingReservedCover { vm_id });
        }
    }
    Ok(())
}

#[cfg(feature = "host-carveout-allocator-evidence")]
fn emit_host_vm_carveout_allocator_evidence() {
    use ax_hal::mem::{MemRegionFlags, memory_regions, vm_carveouts};

    let carveouts = vm_carveouts();
    validate_host_vm_carveout_allocator_evidence(
        carveouts.iter().copied(),
        memory_regions,
        |carveout| (carveout.vm_id, carveout.physical_start, carveout.size),
        |region| {
            (
                region.paddr.as_usize(),
                region.size,
                region.flags.contains(MemRegionFlags::RESERVED),
                region.flags.contains(MemRegionFlags::FREE),
            )
        },
    )
    .unwrap_or_else(|error| {
        panic!("AxVisor host VM carveout allocator evidence validation failed: {error:?}")
    });

    // Do not emit a success marker until every carveout has passed the complete
    // reserved-cover and free-overlap scan above.
    for carveout in carveouts {
        ax_println!(
            "\nAXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED vm={} hpa={:#x} size={:#x} \
             reserved_cover=1 free_overlap=0 phase=before-global-allocator-init",
            carveout.vm_id,
            carveout.physical_start,
            carveout.size,
        );
    }
}

#[cfg(feature = "host-dma-guard-allocator-evidence")]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DmaGuardAllocatorEvidenceError {
    EmptyGuardRange {
        guard_index: usize,
    },
    GuardRangeOverflow {
        guard_index: usize,
    },
    RegionRangeOverflow,
    MissingReservedCover {
        guard_index: usize,
    },
    FreeRegionOverlap {
        guard_index: usize,
    },
    GuardOverlap {
        first_guard_index: usize,
        second_guard_index: usize,
    },
}

#[cfg(feature = "host-dma-guard-allocator-evidence")]
fn checked_dma_guard_end(
    guard_index: usize,
    guard_start: usize,
    guard_size: usize,
) -> Result<usize, DmaGuardAllocatorEvidenceError> {
    if guard_size == 0 {
        return Err(DmaGuardAllocatorEvidenceError::EmptyGuardRange { guard_index });
    }
    range_end(guard_start, guard_size)
        .ok_or(DmaGuardAllocatorEvidenceError::GuardRangeOverflow { guard_index })
}

/// Validates allocator exclusion evidence without allocating memory.
///
/// The guard and region suppliers are restartable so every guard can be
/// compared with every later guard and every memory region before a caller
/// emits any success marker.
#[cfg(feature = "host-dma-guard-allocator-evidence")]
fn validate_host_dma_guard_allocator_evidence<
    G,
    R,
    Guards,
    GuardIter,
    Regions,
    RegionIter,
    GuardRange,
    RegionRange,
>(
    guards: Guards,
    regions: Regions,
    guard_range: GuardRange,
    region_range: RegionRange,
) -> Result<(), DmaGuardAllocatorEvidenceError>
where
    G: Copy,
    R: Copy,
    Guards: Fn() -> GuardIter,
    GuardIter: IntoIterator<Item = G>,
    Regions: Fn() -> RegionIter,
    RegionIter: IntoIterator<Item = R>,
    GuardRange: Fn(G) -> (usize, usize),
    RegionRange: Fn(R) -> (usize, usize, bool, bool),
{
    for (guard_index, guard) in guards().into_iter().enumerate() {
        let (guard_start, guard_size) = guard_range(guard);
        let guard_end = checked_dma_guard_end(guard_index, guard_start, guard_size)?;

        for (other_guard_index, other_guard) in guards().into_iter().enumerate() {
            if other_guard_index <= guard_index {
                continue;
            }
            let (other_start, other_size) = guard_range(other_guard);
            let other_end = checked_dma_guard_end(other_guard_index, other_start, other_size)?;
            if ranges_overlap(guard_start, guard_end, other_start, other_end) {
                return Err(DmaGuardAllocatorEvidenceError::GuardOverlap {
                    first_guard_index: guard_index,
                    second_guard_index: other_guard_index,
                });
            }
        }

        let mut reserved_cover = false;
        for region in regions() {
            let (region_start, region_size, is_reserved, is_free) = region_range(region);
            let region_end = range_end(region_start, region_size)
                .ok_or(DmaGuardAllocatorEvidenceError::RegionRangeOverflow)?;
            if is_reserved && region_start <= guard_start && guard_end <= region_end {
                reserved_cover = true;
            }
            if is_free && ranges_overlap(guard_start, guard_end, region_start, region_end) {
                return Err(DmaGuardAllocatorEvidenceError::FreeRegionOverlap { guard_index });
            }
        }

        if !reserved_cover {
            return Err(DmaGuardAllocatorEvidenceError::MissingReservedCover { guard_index });
        }
    }
    Ok(())
}

#[cfg(feature = "host-dma-guard-allocator-evidence")]
fn emit_host_dma_guard_allocator_evidence() {
    use ax_hal::mem::{DmaGuard, MemRegionFlags, dma_guards, memory_regions};

    let guards = dma_guards();
    // Complete validation deliberately precedes the marker loop: an invalid
    // manifest panics without printing a partial success record.
    validate_host_dma_guard_allocator_evidence(
        || guards.iter().copied(),
        memory_regions,
        |guard: DmaGuard| (guard.physical_start, guard.size),
        |region| {
            (
                region.paddr.as_usize(),
                region.size,
                region.flags.contains(MemRegionFlags::RESERVED),
                region.flags.contains(MemRegionFlags::FREE),
            )
        },
    )
    .unwrap_or_else(|error| {
        panic!("AxVisor host DMA guard allocator evidence validation failed: {error:?}")
    });

    for guard in guards {
        ax_println!(
            "\nAXVISOR_HOST_DMA_GUARD_ALLOCATOR_EXCLUDED hpa={:#x} size={:#x} reserved_cover=1 \
             free_overlap=0 phase=before-global-allocator-init",
            guard.physical_start,
            guard.size,
        );
    }
}

#[cfg(feature = "irq")]
fn init_interrupt() {
    init_percpu_irq(ax_hal::percpu::this_cpu_id());

    // Enable IRQs before starting app
    ax_hal::asm::enable_irqs();

    #[cfg(feature = "ipi")]
    ax_ipi::mark_current_cpu_ready();
}

#[cfg(feature = "irq")]
pub(crate) fn init_percpu_irq(cpu_id: usize) {
    ax_hal::irq::cpu_online(cpu_id).expect("failed to mark CPU online for IRQ framework");
    ax_hal::irq::init_common_irq_handler();

    if ax_hal::percpu::this_cpu_is_bsp() {
        let cpus = ax_hal::irq::CpuMask::first_n(ax_hal::cpu_num());
        ax_hal::irq::request_percpu_irq(ax_hal::time::irq_num(), cpus, timer_irq_handler)
            .expect("failed to register timer IRQ handler");

        #[cfg(any(feature = "ipi", feature = "wake-ipi"))]
        ax_hal::irq::request_percpu_irq(ax_hal::irq::ipi_irq(), cpus, ipi_irq_handler)
            .expect("failed to register IPI IRQ handler");
    }

    init_timer();
}

#[cfg(all(feature = "irq", feature = "ipi"))]
unsafe fn ax_ipi_run_on_cpu_sync(
    cpu: usize,
    f: unsafe fn(*mut ()),
    arg: *mut (),
) -> Result<(), ax_hal::irq::IrqError> {
    unsafe { ax_ipi::run_on_cpu_sync_raw(cpu, f, arg) }
}

#[cfg(feature = "irq")]
fn periodic_interval_nanos() -> u64 {
    ax_hal::time::NANOS_PER_SEC / ticks_per_sec()
}

#[cfg(feature = "irq")]
#[ax_percpu::def_percpu]
static NEXT_PERIODIC_DEADLINE_NANOS: u64 = 0;

#[cfg(feature = "irq")]
fn with_periodic_deadline<R>(
    operation: impl for<'scope> FnOnce(&ax_percpu::CpuPin<'scope>) -> R,
) -> R {
    // SAFETY: every caller runs either during offline CPU initialization or in
    // the local timer IRQ path. Both contexts prevent migration for the whole
    // callback, and the CPU-local area was installed before runtime entry.
    unsafe { ax_percpu::with_cpu_pin(operation) }
        .unwrap_or_else(|error| panic!("timer CPU-local state is invalid: {error}"))
}

#[cfg(feature = "irq")]
fn init_timer() {
    ax_hal::time::enable_timer_irq();
    let now_ns = ax_hal::time::monotonic_time_nanos();
    with_periodic_deadline(|pin| {
        NEXT_PERIODIC_DEADLINE_NANOS
            .write_current(pin, now_ns.saturating_add(periodic_interval_nanos()));
    });
    program_next_timer();
}

#[cfg(feature = "irq")]
fn advance_periodic_timer(now_ns: u64) -> bool {
    let mut deadline = with_periodic_deadline(|pin| NEXT_PERIODIC_DEADLINE_NANOS.read_current(pin));
    if deadline == 0 {
        with_periodic_deadline(|pin| {
            NEXT_PERIODIC_DEADLINE_NANOS
                .write_current(pin, now_ns.saturating_add(periodic_interval_nanos()));
        });
        return false;
    }
    if now_ns < deadline {
        return false;
    }

    while deadline <= now_ns {
        deadline = deadline.saturating_add(periodic_interval_nanos());
        if deadline == u64::MAX {
            break;
        }
    }
    with_periodic_deadline(|pin| NEXT_PERIODIC_DEADLINE_NANOS.write_current(pin, deadline));
    true
}

#[cfg(feature = "irq")]
fn program_next_timer() {
    let mut deadline = with_periodic_deadline(|pin| NEXT_PERIODIC_DEADLINE_NANOS.read_current(pin));
    if deadline == 0 {
        let now_ns = ax_hal::time::monotonic_time_nanos();
        deadline = now_ns.saturating_add(periodic_interval_nanos());
        with_periodic_deadline(|pin| NEXT_PERIODIC_DEADLINE_NANOS.write_current(pin, deadline));
    }
    #[cfg(feature = "multitask")]
    if let Some(task_deadline) = ax_task::next_timer_deadline_nanos() {
        deadline = core::cmp::min(deadline, task_deadline);
    }

    ax_hal::time::set_oneshot_timer(deadline);
    #[cfg(feature = "multitask")]
    ax_task::note_programmed_timer_deadline_nanos(deadline);
}

#[cfg(feature = "irq")]
fn timer_irq_handler(ctx: ax_hal::irq::IrqContext) -> ax_hal::irq::IrqReturn {
    let _ = ctx;
    #[cfg(feature = "multitask")]
    let scheduler_tick = advance_periodic_timer(ax_hal::time::monotonic_time_nanos());
    #[cfg(not(feature = "multitask"))]
    let _ = advance_periodic_timer(ax_hal::time::monotonic_time_nanos());
    #[cfg(feature = "multitask")]
    ax_task::on_timer_irq(scheduler_tick);
    program_next_timer();
    ax_hal::irq::IrqReturn::Handled
}

#[cfg(all(feature = "irq", feature = "ipi"))]
fn ipi_irq_handler(_ctx: ax_hal::irq::IrqContext) -> ax_hal::irq::IrqReturn {
    ax_ipi::ipi_handler();
    ax_hal::irq::IrqReturn::Handled
}

#[cfg(all(feature = "irq", feature = "wake-ipi", not(feature = "ipi")))]
fn ipi_irq_handler(_ctx: ax_hal::irq::IrqContext) -> ax_hal::irq::IrqReturn {
    ax_hal::irq::IrqReturn::Handled
}

#[cfg(all(feature = "tls", not(feature = "multitask")))]
fn init_tls() {
    let main_tls = ax_hal::tls::TlsArea::alloc();
    let kernel_tls = ax_hal::context::KernelTlsBase::new(main_tls.tls_ptr() as usize);
    unsafe { ax_hal::asm::write_thread_pointer(kernel_tls) };
    core::mem::forget(main_tls);
}

#[cfg(test)]
mod tests {
    #[test]
    fn fs_init_accepts_bootargs_without_fs_feature() {
        crate::fs::init(Some("root=/dev/vda"));
    }

    #[cfg(feature = "host-carveout-allocator-evidence")]
    mod host_carveout_allocator_evidence {
        use super::super::{
            CarveoutAllocatorEvidenceError, validate_host_vm_carveout_allocator_evidence,
        };

        #[derive(Clone, Copy)]
        struct Carveout {
            vm_id: u32,
            start: usize,
            size: usize,
        }

        #[derive(Clone, Copy)]
        struct Region {
            start: usize,
            size: usize,
            reserved: bool,
            free: bool,
        }

        fn validate(
            carveouts: &[Carveout],
            regions: &[Region],
        ) -> Result<(), CarveoutAllocatorEvidenceError> {
            validate_host_vm_carveout_allocator_evidence(
                carveouts.iter().copied(),
                || regions.iter().copied(),
                |carveout| (carveout.vm_id, carveout.start, carveout.size),
                |region| (region.start, region.size, region.reserved, region.free),
            )
        }

        #[test]
        fn accepts_complete_reserved_cover_without_free_overlap() {
            assert_eq!(
                validate(
                    &[Carveout {
                        vm_id: 7,
                        start: 0x1000,
                        size: 0x1000,
                    }],
                    &[
                        Region {
                            start: 0x0,
                            size: 0x4000,
                            reserved: true,
                            free: false,
                        },
                        Region {
                            start: 0x4000,
                            size: 0x1000,
                            reserved: false,
                            free: true,
                        },
                    ],
                ),
                Ok(())
            );
        }

        #[test]
        fn rejects_missing_reserved_cover() {
            assert_eq!(
                validate(
                    &[Carveout {
                        vm_id: 7,
                        start: 0x1000,
                        size: 0x1000,
                    }],
                    &[Region {
                        start: 0x1000,
                        size: 0x0800,
                        reserved: true,
                        free: false,
                    }],
                ),
                Err(CarveoutAllocatorEvidenceError::MissingReservedCover { vm_id: 7 })
            );
        }

        #[test]
        fn rejects_one_byte_free_overlap() {
            assert_eq!(
                validate(
                    &[Carveout {
                        vm_id: 7,
                        start: 0x1000,
                        size: 0x1000,
                    }],
                    &[
                        Region {
                            start: 0x1000,
                            size: 0x1000,
                            reserved: true,
                            free: false,
                        },
                        Region {
                            start: 0x1fff,
                            size: 1,
                            reserved: false,
                            free: true,
                        },
                    ],
                ),
                Err(CarveoutAllocatorEvidenceError::FreeRegionOverlap { vm_id: 7 })
            );
        }

        #[test]
        fn accepts_adjacent_free_region() {
            assert_eq!(
                validate(
                    &[Carveout {
                        vm_id: 7,
                        start: 0x1000,
                        size: 0x1000,
                    }],
                    &[
                        Region {
                            start: 0x1000,
                            size: 0x1000,
                            reserved: true,
                            free: false,
                        },
                        Region {
                            start: 0x2000,
                            size: 0x1000,
                            reserved: false,
                            free: true,
                        },
                    ],
                ),
                Ok(())
            );
        }

        #[test]
        fn rejects_overflowing_carveout() {
            assert_eq!(
                validate(
                    &[Carveout {
                        vm_id: 7,
                        start: usize::MAX,
                        size: 1,
                    }],
                    &[],
                ),
                Err(CarveoutAllocatorEvidenceError::CarveoutRangeOverflow { vm_id: 7 })
            );
        }

        #[test]
        fn checks_each_carveout_in_input_order() {
            assert_eq!(
                validate(
                    &[
                        Carveout {
                            vm_id: 9,
                            start: 0x4000,
                            size: 0x1000,
                        },
                        Carveout {
                            vm_id: 3,
                            start: 0x1000,
                            size: 0x1000,
                        },
                    ],
                    &[
                        Region {
                            start: 0x4000,
                            size: 0x1000,
                            reserved: true,
                            free: false,
                        },
                        Region {
                            start: 0x1000,
                            size: 0x1000,
                            reserved: true,
                            free: false,
                        },
                    ],
                ),
                Ok(())
            );
        }
    }

    #[cfg(feature = "host-dma-guard-allocator-evidence")]
    mod host_dma_guard_allocator_evidence {
        use super::super::{
            DmaGuardAllocatorEvidenceError, validate_host_dma_guard_allocator_evidence,
        };

        #[derive(Clone, Copy)]
        struct Guard {
            start: usize,
            size: usize,
        }

        #[derive(Clone, Copy)]
        struct Region {
            start: usize,
            size: usize,
            reserved: bool,
            free: bool,
        }

        fn validate(
            guards: &[Guard],
            regions: &[Region],
        ) -> Result<(), DmaGuardAllocatorEvidenceError> {
            validate_host_dma_guard_allocator_evidence(
                || guards.iter().copied(),
                || regions.iter().copied(),
                |guard| (guard.start, guard.size),
                |region| (region.start, region.size, region.reserved, region.free),
            )
        }

        #[test]
        fn accepts_complete_reserved_cover_without_free_overlap() {
            assert_eq!(
                validate(
                    &[Guard {
                        start: 0x1000,
                        size: 0x1000,
                    }],
                    &[
                        Region {
                            start: 0x0,
                            size: 0x4000,
                            reserved: true,
                            free: false,
                        },
                        Region {
                            start: 0x4000,
                            size: 0x1000,
                            reserved: false,
                            free: true,
                        },
                    ],
                ),
                Ok(())
            );
        }

        #[test]
        fn rejects_missing_reserved_cover() {
            assert_eq!(
                validate(
                    &[Guard {
                        start: 0x1000,
                        size: 0x1000,
                    }],
                    &[Region {
                        start: 0x1000,
                        size: 0x0800,
                        reserved: true,
                        free: false,
                    }],
                ),
                Err(DmaGuardAllocatorEvidenceError::MissingReservedCover { guard_index: 0 })
            );
        }

        #[test]
        fn rejects_one_byte_free_overlap() {
            assert_eq!(
                validate(
                    &[Guard {
                        start: 0x1000,
                        size: 0x1000,
                    }],
                    &[
                        Region {
                            start: 0x1000,
                            size: 0x1000,
                            reserved: true,
                            free: false,
                        },
                        Region {
                            start: 0x1fff,
                            size: 1,
                            reserved: false,
                            free: true,
                        },
                    ],
                ),
                Err(DmaGuardAllocatorEvidenceError::FreeRegionOverlap { guard_index: 0 })
            );
        }

        #[test]
        fn accepts_adjacent_free_region() {
            assert_eq!(
                validate(
                    &[Guard {
                        start: 0x1000,
                        size: 0x1000,
                    }],
                    &[
                        Region {
                            start: 0x1000,
                            size: 0x1000,
                            reserved: true,
                            free: false,
                        },
                        Region {
                            start: 0x2000,
                            size: 0x1000,
                            reserved: false,
                            free: true,
                        },
                    ],
                ),
                Ok(())
            );
        }

        #[test]
        fn rejects_overflowing_guard_range() {
            assert_eq!(
                validate(
                    &[Guard {
                        start: usize::MAX,
                        size: 1,
                    }],
                    &[],
                ),
                Err(DmaGuardAllocatorEvidenceError::GuardRangeOverflow { guard_index: 0 })
            );
        }

        #[test]
        fn rejects_duplicate_guard_range() {
            assert_eq!(
                validate(
                    &[
                        Guard {
                            start: 0x1000,
                            size: 0x1000,
                        },
                        Guard {
                            start: 0x1000,
                            size: 0x1000,
                        },
                    ],
                    &[Region {
                        start: 0x0,
                        size: 0x4000,
                        reserved: true,
                        free: false,
                    }],
                ),
                Err(DmaGuardAllocatorEvidenceError::GuardOverlap {
                    first_guard_index: 0,
                    second_guard_index: 1,
                })
            );
        }

        #[test]
        fn validation_before_marker_loop_is_all_or_none() {
            let guards = [
                Guard {
                    start: 0x1000,
                    size: 0x1000,
                },
                Guard {
                    start: 0x3000,
                    size: 0x1000,
                },
            ];
            let regions = [
                Region {
                    start: 0x1000,
                    size: 0x1000,
                    reserved: true,
                    free: false,
                },
                Region {
                    start: 0x5000,
                    size: 0x1000,
                    reserved: false,
                    free: true,
                },
            ];

            // This mirrors the production validation-before-loop boundary:
            // the second guard invalidates the manifest, so the marker phase
            // cannot receive even the first guard.
            let marker_count = validate(&guards, &regions).map(|()| guards.len());
            assert_eq!(
                marker_count,
                Err(DmaGuardAllocatorEvidenceError::MissingReservedCover { guard_index: 1 })
            );
        }

        #[test]
        fn accepts_empty_manifest_without_marker_candidates() {
            let guards = [];
            assert_eq!(validate(&guards, &[]), Ok(()));
            assert_eq!(guards.len(), 0);
        }
    }
}
