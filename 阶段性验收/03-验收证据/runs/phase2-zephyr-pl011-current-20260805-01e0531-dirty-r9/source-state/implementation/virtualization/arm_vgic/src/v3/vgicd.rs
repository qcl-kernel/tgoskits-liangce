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

use alloc::collections::BTreeMap;

use ax_kspin::SpinNoIrq;
use axdevice_base::{AccessWidth, BaseDeviceOps, DeviceResult, EmuDeviceType};
use axvm_types::{GuestPhysAddr, GuestPhysAddrRange, HostPhysAddr};
use bitmaps::Bitmap;
use log::debug;

use super::{
    registers::*,
    utils::{perform_mmio_read, perform_mmio_write},
};
use crate::{VgicError, VgicResult, host};

/// Default size for GICD region.
pub const DEFAULT_GICD_SIZE: usize = 0x10000; // 64K
const FIRST_SPI: u32 = 32;
const SPI_END: u32 = 1020;

/// Encodes a route to one specific PE.  IROUTER.IRM is deliberately left
/// clear; setting it would request one-of-N routing and ignore the affinity.
fn encode_specific_irouter(target_cpu_affinity: (u8, u8, u8, u8)) -> u64 {
    (target_cpu_affinity.0 as u64) << 32
        | (target_cpu_affinity.1 as u64) << 16
        | (target_cpu_affinity.2 as u64) << 8
        | target_cpu_affinity.3 as u64
}

struct RouteState {
    assigned_irqs: Bitmap<{ MAX_IRQ_V3 }>,
    saved_irouters: BTreeMap<u32, u64>,
}

impl RouteState {
    fn new() -> Self {
        Self {
            assigned_irqs: Bitmap::new(),
            saved_irouters: BTreeMap::new(),
        }
    }

    fn is_assigned(&self, irq: u32) -> bool {
        irq < MAX_IRQ_V3 as u32 && self.assigned_irqs.get(irq as usize)
    }

    fn remember_route(&mut self, irq: u32, irouter: u64) -> bool {
        if self.is_assigned(irq) {
            return false;
        }
        self.saved_irouters.insert(irq, irouter);
        self.assigned_irqs.set(irq as usize, true);
        true
    }

    fn take_saved_routes(&mut self) -> BTreeMap<u32, u64> {
        let routes = core::mem::take(&mut self.saved_irouters);
        for irq in routes.keys() {
            self.assigned_irqs.set(*irq as usize, false);
        }
        routes
    }
}

/// Virtual Generic Interrupt Controller (VGIC) Distributor (D) implementation.
///
/// For GIC version 3.
pub struct VGicD {
    /// The address of the VGicD in the guest physical address space.
    pub addr: GuestPhysAddr,
    /// The size of the VGicD in bytes.
    pub size: usize,

    /// Assigned IRQ ownership and the host routes replaced by this VGicD.
    route_state: SpinNoIrq<RouteState>,

    /// The host physical address of the VGicD.
    ///
    /// TODO: move host gicd access to a separate crate, maybe arm_gic_driver.
    pub host_gicd_addr: HostPhysAddr,
}

impl VGicD {
    /// Validates that an IRQ identifier can be represented by the VGIC.
    pub fn validate_irq(irq: u32) -> VgicResult {
        if irq >= MAX_IRQ_V3 as u32 {
            return Err(VgicError::InvalidIrq {
                irq: irq as usize,
                max: MAX_IRQ_V3,
            });
        }
        Ok(())
    }

    /// Creates a new VGicD instance.
    pub fn new(addr: GuestPhysAddr, size: Option<usize>) -> Self {
        let size = size.unwrap_or(DEFAULT_GICD_SIZE);

        Self {
            addr,
            size,
            route_state: SpinNoIrq::new(RouteState::new()),
            host_gicd_addr: crate::api_reexp::get_host_gicd_base(),
        }
    }

    /// Assigns an IRQ to a specific CPU.
    pub fn assign_irq(&self, irq: u32, target_cpu_affinity: (u8, u8, u8, u8)) -> VgicResult {
        debug!("Physically assigning IRQ {irq} with affinity {target_cpu_affinity:?}");

        Self::validate_irq(irq)?;
        if !Self::is_spi(irq) {
            return Err(VgicError::InvalidSpi {
                irq: irq as usize,
                first: FIRST_SPI as usize,
                end: SPI_END as usize,
            });
        }
        let mut route_state = self.route_state.lock();
        if route_state.is_assigned(irq) {
            return Err(VgicError::IrqAlreadyAssigned { irq: irq as usize });
        }
        let _gicd_guard = GICD_LOCK.lock();
        let original_irouter = self.read_irouter(irq);
        assert!(route_state.remember_route(irq, original_irouter));
        self.write_irouter(irq, encode_specific_irouter(target_cpu_affinity));
        Ok(())
    }

    /// Restores every host SPI route owned by this VGicD.
    ///
    /// This operation is idempotent so AxVM lifecycle cleanup and the Drop
    /// fallback can both call it safely.
    pub fn release_assigned_irqs(&self) {
        let mut route_state = self.route_state.lock();
        let saved_irouters = route_state.take_saved_routes();
        if saved_irouters.is_empty() {
            return;
        }

        let _gicd_guard = GICD_LOCK.lock();
        for (irq, irouter) in saved_irouters.into_iter().rev() {
            self.write_irouter(irq, irouter);
        }
    }

    fn irouter_paddr(&self, irq: u32) -> HostPhysAddr {
        self.host_gicd_addr + GICD_IROUTER + (irq as usize) * 8
    }

    fn read_irouter(&self, irq: u32) -> u64 {
        let addr = host::phys_to_virt(self.irouter_paddr(irq));
        unsafe { core::ptr::read_volatile(addr.as_ptr() as *const u64) }
    }

    fn write_irouter(&self, irq: u32, value: u64) {
        let addr = host::phys_to_virt(self.irouter_paddr(irq));
        unsafe { core::ptr::write_volatile(addr.as_mut_ptr() as *mut u64, value) }
    }

    const fn is_spi(irq: u32) -> bool {
        irq >= FIRST_SPI && irq < SPI_END
    }
}

impl Drop for VGicD {
    fn drop(&mut self) {
        self.release_assigned_irqs();
    }
}

impl BaseDeviceOps<GuestPhysAddrRange> for VGicD {
    fn emu_type(&self) -> axdevice_base::EmuDeviceType {
        EmuDeviceType::GPPTDistributor
    }

    fn address_range(&self) -> GuestPhysAddrRange {
        GuestPhysAddrRange::from_start_size(self.addr, self.size)
    }

    fn handle_read(
        &self,
        addr: <GuestPhysAddrRange as axdevice_base::DeviceAddrRange>::Addr,
        width: AccessWidth,
    ) -> DeviceResult<usize> {
        let gicd_base = self.host_gicd_addr;
        let reg = addr - self.addr;
        let route_state = self.route_state.lock();

        debug!("vGICD read reg {reg:#x} width {width:?}");

        let result = match reg {
            reg if GICD_IROUTER_RANGE.contains(&reg) => {
                let irq = (reg - GICD_IROUTER) as u32 / 8;

                if route_state.is_assigned(irq) && Self::is_spi(irq) {
                    let _gicd_guard = GICD_LOCK.lock();
                    perform_mmio_read(gicd_base + reg, width)
                } else {
                    // If the IRQ is not assigned, return 0
                    Ok(0)
                }
            }
            reg if GICD_ITARGETSR_RANGE.contains(&reg) => {
                let irq = (reg - GICD_ITARGETSR) as u32;

                if route_state.is_assigned(irq) && Self::is_spi(irq) {
                    let _gicd_guard = GICD_LOCK.lock();
                    perform_mmio_read(gicd_base + reg, width)
                } else {
                    // If the IRQ is not assigned, return 0
                    Ok(0)
                }
            }
            reg if GICD_ICENABLER_RANGE.contains(&reg)
                || GICD_ISENABLER_RANGE.contains(&reg)
                || GICD_ICPENDR_RANGE.contains(&reg)
                || GICD_ISPENDR_RANGE.contains(&reg)
                || GICD_ICACTIVER_RANGE.contains(&reg)
                || GICD_ISACTIVER_RANGE.contains(&reg) =>
            {
                self.irq_masked_read_with_state(&route_state, reg, reg & 0x7f, 0, width)
            }
            reg if GICD_IGROUPR_RANGE.contains(&reg) => {
                self.irq_masked_read_with_state(&route_state, reg, reg & 0x7f, 0, width)
            }
            reg if GICD_IGRPMODR_RANGE.contains(&reg) => {
                self.irq_masked_read_with_state(&route_state, reg, reg & 0x7f, 0, width)
            }
            reg if GICD_ICFGR_RANGE.contains(&reg) => {
                self.irq_masked_read_with_state(&route_state, reg, reg & 0xff, 1, width)
            }
            reg if GICD_IPRIORITYR_RANGE.contains(&reg) => {
                self.irq_masked_read_with_state(&route_state, reg, reg & 0x3ff, 3, width)
            }
            reg if GICDV3_PIDR0_RANGE.contains(&reg)
                || GICDV3_PIDR4_RANGE.contains(&reg)
                || GICDV3_CIDR0_RANGE.contains(&reg)
                || reg == GICD_CTLR
                || reg == GICD_TYPER
                || reg == GICD_IIDR
                || reg == GICD_TYPER2 =>
            {
                // read-only
                // ignore write
                let _gicd_guard = GICD_LOCK.lock();
                perform_mmio_read(gicd_base + reg, width)
            }
            _ => {
                todo!("vgicdv3 read unimplemented for reg {:#x}", reg);
            }
        };
        Ok(result?)
    }

    fn handle_write(
        &self,
        addr: <GuestPhysAddrRange as axdevice_base::DeviceAddrRange>::Addr,
        width: AccessWidth,
        val: usize,
    ) -> DeviceResult {
        let gicd_base = self.host_gicd_addr;
        let reg = addr - self.addr;
        let route_state = self.route_state.lock();

        debug!("vGICD write reg {reg:#x} width {width:?} val {val:#x}");

        let result = match reg {
            reg if GICD_IROUTER_RANGE.contains(&reg) => {
                let irq = (reg - GICD_IROUTER) as u32 / 8;

                if route_state.is_assigned(irq) && Self::is_spi(irq) {
                    let _gicd_guard = GICD_LOCK.lock();
                    perform_mmio_write(gicd_base + reg, width, val)
                } else {
                    // If the IRQ is not assigned, ignore the write
                    Ok(())
                }
            }
            reg if GICD_ITARGETSR_RANGE.contains(&reg) => {
                let irq = (reg - GICD_ITARGETSR) as u32; // it was wrong in hVisor

                if route_state.is_assigned(irq) && Self::is_spi(irq) {
                    let _gicd_guard = GICD_LOCK.lock();
                    perform_mmio_write(gicd_base + reg, width, val)
                } else {
                    // If the IRQ is not assigned, ignore the write
                    Ok(())
                }
            }
            reg if GICD_ICENABLER_RANGE.contains(&reg)
                || GICD_ISENABLER_RANGE.contains(&reg)
                || GICD_ICPENDR_RANGE.contains(&reg)
                || GICD_ISPENDR_RANGE.contains(&reg)
                || GICD_ICACTIVER_RANGE.contains(&reg)
                || GICD_ISACTIVER_RANGE.contains(&reg) =>
            {
                self.irq_masked_write_with_state(&route_state, reg, reg & 0x7f, 0, width, true, val)
            }
            reg if GICD_IGROUPR_RANGE.contains(&reg) => self.irq_masked_write_with_state(
                &route_state,
                reg,
                reg & 0x7f,
                0,
                width,
                false,
                val,
            ),
            reg if GICD_IGRPMODR_RANGE.contains(&reg) => self.irq_masked_write_with_state(
                &route_state,
                reg,
                reg & 0x7f,
                0,
                width,
                false,
                val,
            ),
            reg if GICD_ICFGR_RANGE.contains(&reg) => self.irq_masked_write_with_state(
                &route_state,
                reg,
                reg & 0xff,
                1,
                width,
                false,
                val,
            ),
            reg if GICD_IPRIORITYR_RANGE.contains(&reg) => self.irq_masked_write_with_state(
                &route_state,
                reg,
                reg & 0x3ff,
                3,
                width,
                false,
                val,
            ),
            reg if GICDV3_PIDR0_RANGE.contains(&reg)
                || GICDV3_PIDR4_RANGE.contains(&reg)
                || GICDV3_CIDR0_RANGE.contains(&reg)
                || reg == GICD_CTLR
                || reg == GICD_TYPER
                || reg == GICD_IIDR
                || reg == GICD_TYPER2 =>
            {
                // read-only
                // ignore write
                Ok(())
            }
            _ => {
                todo!("vgicdv3 write unimplemented for reg {:#x}", reg);
            }
        };
        Ok(result?)
    }
}

impl VGicD {
    /// Checks if an IRQ is assigned to this VGicD.
    pub fn is_irq_assigned(&self, irq: u32) -> bool {
        self.route_state.lock().is_assigned(irq)
    }

    /// Checks if an IRQ is a Software Generated Interrupt (SGI).
    pub fn is_irq_sgi(&self, irq: u32) -> bool {
        // Check if the IRQ is a Software Generated Interrupt (SGI)
        irq < 16
    }

    /// Checks if an IRQ is a Shared Peripheral Interrupt (SPI).
    pub fn is_irq_spi(&self, irq: u32) -> bool {
        Self::is_spi(irq)
    }

    /// Returns the mask of bits for the irqs assigned to this VGicD, in a bit-field reg.
    pub fn irq_access_mask(
        &self,
        reg_offset: usize,
        bits_per_irq_shift: usize,
        width: AccessWidth,
    ) -> usize {
        let route_state = self.route_state.lock();
        Self::irq_access_mask_with_state(&route_state, reg_offset, bits_per_irq_shift, width)
    }

    fn irq_access_mask_with_state(
        route_state: &RouteState,
        reg_offset: usize,
        bits_per_irq_shift: usize,
        width: AccessWidth,
    ) -> usize {
        if bits_per_irq_shift > 3 {
            panic!(
                "bits_per_irq_shift must be <= 3, got {}",
                bits_per_irq_shift
            );
        }

        // How many IRQs there are in the mmio region the access width covers?
        let irqs_in_access_width = width.size() << (3 - bits_per_irq_shift);
        // The first IRQ at the given register offset.
        let first_irq = reg_offset << (3 - bits_per_irq_shift);
        // The mask of a single IRQ in the bit-field register.
        let single_irq_mask = (1 << (bits_per_irq_shift + 1)) - 1;

        let mut mask = 0;
        for irq in 0..irqs_in_access_width {
            if route_state.is_assigned((first_irq + irq) as _) {
                // If the IRQ is assigned, set the corresponding bits in the mask.
                mask |= single_irq_mask << (irq << bits_per_irq_shift);
            }
        }

        mask
    }

    /// Performs masked read access to GICD registers.
    pub fn irq_masked_read(
        &self,
        offset: usize,
        reg_offset: usize,
        bits_per_irq_shift: usize,
        width: AccessWidth,
        _is_poke: bool,
    ) -> VgicResult<usize> {
        let route_state = self.route_state.lock();
        self.irq_masked_read_with_state(&route_state, offset, reg_offset, bits_per_irq_shift, width)
    }

    fn irq_masked_read_with_state(
        &self,
        route_state: &RouteState,
        offset: usize,
        reg_offset: usize,
        bits_per_irq_shift: usize,
        width: AccessWidth,
    ) -> VgicResult<usize> {
        let mask =
            Self::irq_access_mask_with_state(route_state, reg_offset, bits_per_irq_shift, width);
        if mask == 0 {
            return Ok(0);
        }
        let _gicd_guard = GICD_LOCK.lock();

        Ok(perform_mmio_read(self.host_gicd_addr + offset, width)? & mask)
    }

    /// Performs masked write access to GICD registers.
    pub fn irq_masked_write(
        &self,
        offset: usize,
        reg_offset: usize,
        bits_per_irq_shift: usize,
        width: AccessWidth,
        is_poke: bool,
        val: usize,
    ) -> VgicResult<()> {
        let route_state = self.route_state.lock();
        self.irq_masked_write_with_state(
            &route_state,
            offset,
            reg_offset,
            bits_per_irq_shift,
            width,
            is_poke,
            val,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn irq_masked_write_with_state(
        &self,
        route_state: &RouteState,
        offset: usize,
        reg_offset: usize,
        bits_per_irq_shift: usize,
        width: AccessWidth,
        is_poke: bool,
        val: usize,
    ) -> VgicResult<()> {
        let mask =
            Self::irq_access_mask_with_state(route_state, reg_offset, bits_per_irq_shift, width);
        if mask == 0 {
            return Ok(());
        }
        let _gicd_guard = GICD_LOCK.lock();

        if is_poke {
            perform_mmio_write(self.host_gicd_addr + offset, width, val & mask)
        } else {
            let current_value = perform_mmio_read(self.host_gicd_addr + offset, width)?;
            let new_value = (current_value & !mask) | (val & mask);
            perform_mmio_write(self.host_gicd_addr + offset, width, new_value)
        }
    }
}

// Todo: move this lock to arceos or axvisor
static GICD_LOCK: ax_kspin::SpinNoIrq<()> = ax_kspin::SpinNoIrq::new(());

#[cfg(test)]
mod tests {
    use axdevice_base::AccessWidth;

    use super::{RouteState, encode_specific_irouter};

    #[test]
    fn specific_irouter_encodes_affinity_with_irm_clear() {
        let value = encode_specific_irouter((0x12, 0x34, 0x56, 0x78));
        assert_eq!(value, 0x12_0034_5678);
        assert_eq!(value & (1u64 << 31), 0);
    }

    #[test]
    fn route_state_preserves_the_first_snapshot() {
        let mut state = RouteState::new();

        assert!(state.remember_route(32, 0x8000_0000_0000_1234));
        assert!(!state.remember_route(32, 0x5678));

        assert!(state.is_assigned(32));
        assert_eq!(state.saved_irouters.get(&32), Some(&0x8000_0000_0000_1234));
    }

    #[test]
    fn taking_saved_routes_is_idempotent() {
        let mut state = RouteState::new();
        assert!(state.remember_route(32, 0x1234));
        assert!(state.remember_route(48, 0x5678));

        let routes = state.take_saved_routes();
        assert_eq!(routes.len(), 2);
        assert!(!state.is_assigned(32));
        assert!(!state.is_assigned(48));
        assert!(state.take_saved_routes().is_empty());
    }

    #[test]
    fn released_route_state_blocks_stale_irq_access() {
        let mut state = RouteState::new();
        assert!(state.remember_route(32, 0x1234));
        assert_ne!(
            super::VGicD::irq_access_mask_with_state(&state, 4, 0, AccessWidth::Dword),
            0
        );

        state.take_saved_routes();

        assert_eq!(
            super::VGicD::irq_access_mask_with_state(&state, 4, 0, AccessWidth::Dword),
            0
        );
    }
}
