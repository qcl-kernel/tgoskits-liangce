// Copyright 2026 The Axvisor Team
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

//! Single-consumer host drain for VM-owned, transmit-only PL011 devices.

use alloc::{sync::Arc, vec::Vec};
use core::{
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

use anyhow::{Result, anyhow};
use ax_std::os::arceos::api::task::{AxCpuMask, ax_set_current_affinity};
use ax_std::thread::{self, JoinHandle};
use axvm::{AxVMRef, AxvmRuntime};

const FRAME_PAYLOAD_BYTES: usize = 256;
const MAX_FRAMES_PER_DEVICE_PASS: usize = 16;
const IDLE_POLL_INTERVAL: Duration = Duration::from_millis(1);

/// Lifecycle handle for the default-VM Guest console drain task.
pub(crate) struct GuestConsoleDrain {
    stop: Arc<AtomicBool>,
    task: JoinHandle<Result<()>>,
}

impl GuestConsoleDrain {
    /// Starts one ordered consumer when at least one prepared VM owns a PL011.
    pub(crate) fn start(vms: Vec<AxVMRef>) -> Result<Option<Self>> {
        let mut console_vms = Vec::new();
        for vm in &vms {
            let devices = vm
                .get_devices()
                .map_err(|error| anyhow!("inspect VM[{}] console devices: {error:?}", vm.id()))?;
            if devices
                .devices()
                .any(|device| device.as_any().is::<axdevice::Pl011TxDevice>())
            {
                let protocol_vm_id = u16::try_from(vm.id())
                    .ok()
                    .filter(|vm_id| *vm_id != 0)
                    .ok_or_else(|| {
                        anyhow!(
                            "VM[{}] owns a Guest console but its id is outside 1..=65535",
                            vm.id()
                        )
                    })?;
                console_vms.push((protocol_vm_id, vm.clone()));
            }
        }
        if console_vms.is_empty() {
            return Ok(None);
        }
        let drain_cpu = dedicated_drain_cpu(&vms).ok_or_else(|| {
            anyhow!(
                "Guest console evidence requires explicit valid vCPU masks and one dedicated host CPU"
            )
        })?;

        let stop = Arc::new(AtomicBool::new(false));
        let task_stop = stop.clone();
        let task = thread::Builder::new()
            .name("guest-console-drain".into())
            .spawn(move || {
                ax_set_current_affinity(AxCpuMask::one_shot(drain_cpu)).map_err(|error| {
                    anyhow!("pin Guest console drain task to host CPU {drain_cpu}: {error:?}")
                })?;
                info!("Guest console drain task running on dedicated host CPU {drain_cpu}");
                drain_loop(console_vms, task_stop);
                Ok(())
            })
            .map_err(|error| anyhow!("spawn Guest console drain task: {error}"))?;
        Ok(Some(Self { stop, task }))
    }

    /// Requests a final drain and waits until every remaining frame is emitted.
    pub(crate) fn finish(self) -> Result<()> {
        self.stop.store(true, Ordering::Release);
        self.task
            .join()
            .map_err(|error| anyhow!("join Guest console drain task: {error}"))?
    }
}

fn dedicated_drain_cpu(vms: &[AxVMRef]) -> Option<usize> {
    let cpu_count = thread::available_parallelism().ok()?.get();
    let cpu_capacity = AxCpuMask::full().len();
    if cpu_count > cpu_capacity || cpu_count > usize::BITS as usize {
        return None;
    }
    let host_cpu_mask = if cpu_count == usize::BITS as usize {
        usize::MAX
    } else {
        (1_usize << cpu_count) - 1
    };
    let mut guest_cpu_mask = 0_usize;
    for vm in vms {
        for (_, affinity, _) in vm.get_vcpu_affinities_pcpu_ids() {
            let affinity = affinity?;
            let effective_affinity = affinity & host_cpu_mask;
            if effective_affinity == 0 || effective_affinity != affinity {
                return None;
            }
            guest_cpu_mask |= effective_affinity;
        }
    }
    (0..cpu_count)
        .rev()
        .find(|cpu_id| guest_cpu_mask & (1_usize << cpu_id) == 0)
}

fn drain_loop(vms: Vec<(u16, AxVMRef)>, stop: Arc<AtomicBool>) {
    let mut failed_vms = Vec::new();
    loop {
        let emitted = drain_pass(&vms, &mut failed_vms);
        if stop.load(Ordering::Acquire) && !emitted {
            break;
        }
        if emitted {
            thread::yield_now();
        } else {
            thread::sleep(IDLE_POLL_INTERVAL);
        }
    }
}

fn drain_pass(vms: &[(u16, AxVMRef)], failed_vms: &mut Vec<usize>) -> bool {
    let mut emitted = false;
    for (protocol_vm_id, vm) in vms {
        let vm_id = vm.id();
        if failed_vms.contains(&vm_id) {
            continue;
        }
        let devices = match vm.get_devices() {
            Ok(devices) => devices,
            Err(error) => {
                fail_console_vm(vm_id, format_args!("device lookup failed: {error:?}"));
                failed_vms.push(vm_id);
                continue;
            }
        };

        for device in devices.devices() {
            let Some(console) = device.as_any().downcast_ref::<axdevice::Pl011TxDevice>() else {
                continue;
            };
            let mut payload = [0_u8; FRAME_PAYLOAD_BYTES];
            for _ in 0..MAX_FRAMES_PER_DEVICE_PASS {
                match console.drain_tx_frame(*protocol_vm_id, &mut payload) {
                    Ok(Some(frame)) => {
                        // This direct print holds the host console's record lock. Establish
                        // the line boundary in the same transaction: colored host logs leave
                        // their ANSI reset after the newline, and it must never prefix a frame.
                        // The only consumer task emits frames in device-assigned sequence.
                        ax_std::println!("\n{frame}");
                        emitted = true;
                    }
                    Ok(None) => break,
                    Err(error) => {
                        fail_console_vm(vm_id, format_args!("frame drain failed: {error}"));
                        failed_vms.push(vm_id);
                        break;
                    }
                }
            }
            if failed_vms.contains(&vm_id) {
                break;
            }
        }
    }
    emitted
}

fn fail_console_vm(vm_id: usize, detail: core::fmt::Arguments<'_>) {
    error!("VM[{vm_id}] Guest console evidence failed closed: {detail}");
    if let Err(error) = AxvmRuntime::stop_vm(vm_id) {
        error!("VM[{vm_id}] could not stop after Guest console failure: {error:?}");
    }
}
