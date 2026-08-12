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

//! VM-local, transmit-only PL011 model for AArch64 guests.

mod frame;

use alloc::{collections::VecDeque, string::String, sync::Arc};
use core::any::Any;

use ax_kspin::SpinNoIrq as Mutex;
use axdevice_base::{AccessWidth, BusAccess, BusKind, BusResponse, Device, DeviceError, Resource};
use axvm_types::{EmulatedDeviceConfig, EmulatedDeviceType};
#[cfg(target_arch = "aarch64")]
pub use frame::{
    PL011_TX_FRAME_MARKER, PL011_TX_FRAME_MAX_PAYLOAD, PL011_TX_FRAME_VERSION, Pl011TxFrame,
    Pl011TxFrameError,
};

use crate::{
    DeviceBuildContext, DeviceBundle, DeviceFactory, DeviceManagerError, DeviceManagerResult,
    DeviceRegistration,
};

const PL011_TX_MMIO_SIZE: usize = 0x1000;
const TX_RING_CAPACITY: usize = 4096;
const TX_ONLY_IRQ_SENTINEL: usize = 0;

const UARTDR: u64 = 0x000;
const UARTRSR_ECR: u64 = 0x004;
const UARTFR: u64 = 0x018;
const UARTILPR: u64 = 0x020;
const UARTIBRD: u64 = 0x024;
const UARTFBRD: u64 = 0x028;
const UARTLCR_H: u64 = 0x02c;
const UARTCR: u64 = 0x030;
const UARTIFLS: u64 = 0x034;
const UARTIMSC: u64 = 0x038;
const UARTRIS: u64 = 0x03c;
const UARTMIS: u64 = 0x040;
const UARTICR: u64 = 0x044;
const UARTDMACR: u64 = 0x048;

const UARTPERIPHID0: u64 = 0xfe0;
const UARTPERIPHID1: u64 = 0xfe4;
const UARTPERIPHID2: u64 = 0xfe8;
const UARTPERIPHID3: u64 = 0xfec;
const UARTPCELLID0: u64 = 0xff0;
const UARTPCELLID1: u64 = 0xff4;
const UARTPCELLID2: u64 = 0xff8;
const UARTPCELLID3: u64 = 0xffc;

const UARTFR_RXFE: u32 = 1 << 4;
const UARTFR_TXFE: u32 = 1 << 7;
const UARTFR_TX_READY: u32 = UARTFR_RXFE | UARTFR_TXFE;
const UARTCR_UARTEN: u32 = 1 << 0;
const UARTCR_TXE: u32 = 1 << 8;
const UARTCR_RESET: u32 = (1 << 8) | (1 << 9);
const UARTCR_WRITABLE: u32 = 0xff87;
const UARTIMSC_WRITABLE: u32 = 0x07ff;
const UARTDMACR_ENABLE_BITS: u32 = 0x7;

/// Monotonic, VM-instance-local counters for a transmit-only PL011.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct Pl011TxStatistics {
    /// Bytes written while the UART and its transmitter were enabled.
    pub total_bytes: u64,
    /// Bytes discarded because the bounded transmit ring was full.
    pub dropped_bytes: u64,
    /// Writes that requested any unsupported PL011 DMA mode.
    pub dma_enable_attempts: u64,
    /// Bytes currently waiting in this device instance's transmit ring.
    pub buffered_bytes: usize,
}

struct Pl011State {
    ilpr: u32,
    ibrd: u32,
    fbrd: u32,
    lcr_h: u32,
    cr: u32,
    ifls: u32,
    imsc: u32,
    tx: VecDeque<u8>,
    total_bytes: u64,
    dropped_bytes: u64,
    dma_enable_attempts: u64,
    frame: frame::Pl011TxFrameState,
}

impl Pl011State {
    fn new() -> Self {
        Self {
            ilpr: 0,
            ibrd: 0,
            fbrd: 0,
            lcr_h: 0,
            cr: UARTCR_RESET,
            ifls: 0,
            imsc: 0,
            tx: VecDeque::with_capacity(TX_RING_CAPACITY),
            total_bytes: 0,
            dropped_bytes: 0,
            dma_enable_attempts: 0,
            frame: frame::Pl011TxFrameState::new(),
        }
    }

    fn reset(&mut self) -> Result<(), frame::Pl011TxFrameError> {
        self.frame
            .reset(self.tx.len(), self.dropped_bytes, self.dma_enable_attempts)?;
        self.ilpr = 0;
        self.ibrd = 0;
        self.fbrd = 0;
        self.lcr_h = 0;
        self.cr = UARTCR_RESET;
        self.ifls = 0;
        self.imsc = 0;
        self.tx.clear();
        self.total_bytes = 0;
        self.dropped_bytes = 0;
        self.dma_enable_attempts = 0;
        Ok(())
    }
}

/// A VM-local PL011 subset that supports bounded, polling-only transmission.
///
/// The model owns no host physical address, IRQ line, DMA engine, input path,
/// or external sink. Each factory build creates a fresh register bank and TX
/// ring. A later VM-owned backend may drain strict, source-ordered records with
/// [`Self::drain_tx_frame`] without coupling a sink to the vCPU exit path.
pub struct Pl011TxDevice {
    name: String,
    base: u64,
    resources: [Resource; 1],
    state: Mutex<Pl011State>,
}

impl Pl011TxDevice {
    fn new(name: String, base: u64) -> Self {
        Self {
            name,
            base,
            resources: [Resource::MmioRange {
                base,
                size: PL011_TX_MMIO_SIZE as u64,
            }],
            state: Mutex::new(Pl011State::new()),
        }
    }

    /// Returns a consistent snapshot of the instance-local counters.
    pub fn statistics(&self) -> Pl011TxStatistics {
        let state = self.state.lock();
        Pl011TxStatistics {
            total_bytes: state.total_bytes,
            dropped_bytes: state.dropped_bytes,
            dma_enable_attempts: state.dma_enable_attempts,
            buffered_bytes: state.tx.len(),
        }
    }

    fn checked_offset(&self, access: &BusAccess) -> Result<u64, DeviceError> {
        if access.kind != BusKind::Mmio {
            return Err(DeviceError::Unsupported {
                operation: "access TX-only PL011",
                detail: alloc::format!("PL011 does not support the {:?} bus", access.kind),
            });
        }
        if access.width != AccessWidth::Dword {
            return Err(DeviceError::InvalidWidth {
                expected: AccessWidth::Dword,
                actual: access.width,
            });
        }

        let access_end = access
            .addr
            .checked_add(AccessWidth::Dword.size() as u64)
            .ok_or(DeviceError::OutOfRange { addr: access.addr })?;
        let region_end = self.base + PL011_TX_MMIO_SIZE as u64;
        if access.addr < self.base || access_end > region_end {
            return Err(DeviceError::OutOfRange { addr: access.addr });
        }
        Ok(access.addr - self.base)
    }

    fn read_register(&self, offset: u64) -> Result<u32, DeviceError> {
        let constant = match offset {
            UARTDR | UARTRSR_ECR | UARTRIS | UARTMIS | UARTDMACR => Some(0),
            UARTFR => Some(UARTFR_TX_READY),
            UARTPERIPHID0 => Some(0x11),
            UARTPERIPHID1 => Some(0x10),
            UARTPERIPHID2 => Some(0x14),
            UARTPERIPHID3 => Some(0x00),
            UARTPCELLID0 => Some(0x0d),
            UARTPCELLID1 => Some(0xf0),
            UARTPCELLID2 => Some(0x05),
            UARTPCELLID3 => Some(0xb1),
            UARTICR => return Err(DeviceError::WriteOnly),
            _ => None,
        };
        if let Some(value) = constant {
            return Ok(value);
        }

        let state = self.state.lock();
        match offset {
            UARTILPR => Ok(state.ilpr),
            UARTIBRD => Ok(state.ibrd),
            UARTFBRD => Ok(state.fbrd),
            UARTLCR_H => Ok(state.lcr_h),
            UARTCR => Ok(state.cr),
            UARTIFLS => Ok(state.ifls),
            UARTIMSC => Ok(state.imsc),
            _ => Err(DeviceError::Unsupported {
                operation: "read TX-only PL011 register",
                detail: alloc::format!("register offset {offset:#x} is not modeled"),
            }),
        }
    }

    fn write_register(&self, offset: u64, value: u32) -> Result<(), DeviceError> {
        match offset {
            UARTFR | UARTRIS | UARTMIS | UARTPERIPHID0 | UARTPERIPHID1 | UARTPERIPHID2
            | UARTPERIPHID3 | UARTPCELLID0 | UARTPCELLID1 | UARTPCELLID2 | UARTPCELLID3 => {
                return Err(DeviceError::ReadOnly);
            }
            _ => {}
        }

        let mut state = self.state.lock();
        match offset {
            UARTDR => {
                if state.cr & (UARTCR_UARTEN | UARTCR_TXE) == (UARTCR_UARTEN | UARTCR_TXE) {
                    let total_bytes = state.total_bytes.checked_add(1).ok_or_else(|| {
                        DeviceError::InvalidState {
                            operation: "write TX-only PL011",
                            detail: "total byte counter is exhausted".into(),
                        }
                    })?;
                    if state.tx.len() == TX_RING_CAPACITY {
                        let dropped_bytes =
                            state.dropped_bytes.checked_add(1).ok_or_else(|| {
                                DeviceError::InvalidState {
                                    operation: "write TX-only PL011",
                                    detail: "dropped byte counter is exhausted".into(),
                                }
                            })?;
                        state.total_bytes = total_bytes;
                        state.dropped_bytes = dropped_bytes;
                    } else {
                        state.total_bytes = total_bytes;
                        state.tx.push_back(value as u8);
                    }
                }
            }
            UARTRSR_ECR | UARTICR => {}
            UARTILPR => state.ilpr = value & 0xff,
            UARTIBRD => state.ibrd = value & 0xffff,
            UARTFBRD => state.fbrd = value & 0x3f,
            UARTLCR_H => state.lcr_h = value & 0xff,
            UARTCR => state.cr = value & UARTCR_WRITABLE,
            UARTIFLS => state.ifls = value & 0x3f,
            UARTIMSC => state.imsc = value & UARTIMSC_WRITABLE,
            UARTDMACR => {
                if value & UARTDMACR_ENABLE_BITS != 0 {
                    state.dma_enable_attempts = state
                        .dma_enable_attempts
                        .checked_add(1)
                        .ok_or_else(|| DeviceError::InvalidState {
                            operation: "write TX-only PL011",
                            detail: "DMA-attempt counter is exhausted".into(),
                        })?;
                }
            }
            _ => {
                return Err(DeviceError::Unsupported {
                    operation: "write TX-only PL011 register",
                    detail: alloc::format!("register offset {offset:#x} is not modeled"),
                });
            }
        }
        Ok(())
    }
}

impl Device for Pl011TxDevice {
    fn name(&self) -> &str {
        &self.name
    }

    fn resources(&self) -> &[Resource] {
        &self.resources
    }

    fn handle(&self, access: &BusAccess) -> Result<BusResponse, DeviceError> {
        let offset = self.checked_offset(access)?;
        if access.is_read {
            Ok(BusResponse::Read {
                value: self.read_register(offset)? as u64,
            })
        } else {
            self.write_register(offset, access.data as u32)?;
            Ok(BusResponse::Write)
        }
    }

    fn as_any(&self) -> &dyn Any {
        self
    }

    fn reset(&mut self) -> Result<(), DeviceError> {
        self.state
            .lock()
            .reset()
            .map_err(|error| DeviceError::InvalidState {
                operation: "reset TX-only PL011",
                detail: alloc::format!("{error}"),
            })
    }
}

pub(crate) struct Pl011TxFactory;

impl DeviceFactory for Pl011TxFactory {
    fn device_type(&self) -> EmulatedDeviceType {
        EmulatedDeviceType::Console
    }

    fn build(
        &self,
        config: &EmulatedDeviceConfig,
        _context: &DeviceBuildContext<'_>,
    ) -> DeviceManagerResult<DeviceBundle> {
        let invalid = |detail| DeviceManagerError::InvalidConfig {
            operation: "build TX-only PL011",
            detail,
        };
        if config.emu_type != EmulatedDeviceType::Console {
            return Err(invalid(alloc::format!(
                "device '{}' has type {}, expected {}",
                config.name,
                config.emu_type,
                EmulatedDeviceType::Console
            )));
        }
        if !frame::is_valid_frame_name(&config.name) {
            return Err(invalid(
                "device name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}".into(),
            ));
        }
        if config.length != PL011_TX_MMIO_SIZE {
            return Err(invalid(alloc::format!(
                "device '{}' requires an exact {PL011_TX_MMIO_SIZE:#x}-byte MMIO window, got {:#x}",
                config.name,
                config.length
            )));
        }
        if !config.base_gpa.is_multiple_of(PL011_TX_MMIO_SIZE) {
            return Err(invalid(alloc::format!(
                "device '{}' base GPA {:#x} is not {PL011_TX_MMIO_SIZE:#x}-byte aligned",
                config.name,
                config.base_gpa
            )));
        }
        if config.base_gpa.checked_add(config.length).is_none() {
            return Err(invalid(alloc::format!(
                "device '{}' MMIO range overflows the guest address space",
                config.name
            )));
        }
        if config.irq_id != TX_ONLY_IRQ_SENTINEL {
            return Err(invalid(alloc::format!(
                "device '{}' is polling-only: irq_id must use the TX-only sentinel {}, got {}",
                config.name,
                TX_ONLY_IRQ_SENTINEL,
                config.irq_id
            )));
        }
        if !config.cfg_list.is_empty() {
            return Err(invalid(alloc::format!(
                "device '{}' does not accept backend, DMA, RX, or passthrough arguments",
                config.name
            )));
        }

        let device = Arc::new(Pl011TxDevice::new(
            config.name.clone(),
            config.base_gpa as u64,
        ));
        Ok(DeviceRegistration::Device(device).into())
    }
}

#[cfg(test)]
mod tests {
    use alloc::{string::ToString, vec};

    use axdevice_base::{InterruptTriggerMode, IrqLine};

    use super::*;
    use crate::IrqResolver;
    #[cfg(target_arch = "aarch64")]
    use crate::register_builtin_factories;

    const BASE: u64 = 0x0900_0000;

    struct RejectingIrqResolver;

    impl IrqResolver for RejectingIrqResolver {
        fn resolve_irq(
            &self,
            _line: usize,
            _trigger: InterruptTriggerMode,
        ) -> DeviceManagerResult<IrqLine> {
            panic!("TX-only PL011 factory must not resolve an IRQ")
        }
    }

    fn access(is_read: bool, offset: u64, width: AccessWidth, data: u64) -> BusAccess {
        BusAccess {
            kind: BusKind::Mmio,
            is_read,
            addr: BASE + offset,
            width,
            data,
        }
    }

    fn read(device: &Pl011TxDevice, offset: u64) -> Result<u64, DeviceError> {
        match device.handle(&access(true, offset, AccessWidth::Dword, 0))? {
            BusResponse::Read { value } => Ok(value),
            BusResponse::Write => panic!("read returned a write acknowledgement"),
        }
    }

    fn write(device: &Pl011TxDevice, offset: u64, value: u64) -> Result<(), DeviceError> {
        match device.handle(&access(false, offset, AccessWidth::Dword, value))? {
            BusResponse::Write => Ok(()),
            BusResponse::Read { .. } => panic!("write returned read data"),
        }
    }

    fn drain_tx_for_register_test(device: &Pl011TxDevice, destination: &mut [u8]) -> usize {
        let mut state = device.state.lock();
        let count = destination.len().min(state.tx.len());
        for (slot, value) in destination[..count].iter_mut().zip(state.tx.drain(..count)) {
            *slot = value;
        }
        count
    }

    fn enabled_device(name: &str) -> Pl011TxDevice {
        let device = Pl011TxDevice::new(name.to_string(), BASE);
        write(&device, UARTCR, (UARTCR_UARTEN | UARTCR_TXE) as u64).unwrap();
        device
    }

    fn valid_config() -> EmulatedDeviceConfig {
        EmulatedDeviceConfig {
            name: "guest-uart".into(),
            base_gpa: BASE as usize,
            length: PL011_TX_MMIO_SIZE,
            irq_id: 0,
            emu_type: EmulatedDeviceType::Console,
            cfg_list: vec![],
        }
    }

    fn assert_invalid_config(config: EmulatedDeviceConfig) {
        let resolver = RejectingIrqResolver;
        let context = DeviceBuildContext::new(&resolver);
        assert!(matches!(
            Pl011TxFactory.build(&config, &context),
            Err(DeviceManagerError::InvalidConfig { .. })
        ));
    }

    #[test]
    fn data_and_flags_follow_tx_only_polling_contract() {
        let device = Pl011TxDevice::new("uart0".into(), BASE);
        assert_eq!(read(&device, UARTFR), Ok(UARTFR_TX_READY as u64));
        assert_eq!(read(&device, UARTDR), Ok(0));

        write(&device, UARTDR, b'x' as u64).unwrap();
        assert_eq!(device.statistics().total_bytes, 0);

        write(&device, UARTCR, (UARTCR_UARTEN | UARTCR_TXE) as u64).unwrap();
        write(&device, UARTDR, b'a' as u64).unwrap();
        write(&device, UARTDR, 0x1_62).unwrap();

        let mut output = [0; 4];
        assert_eq!(drain_tx_for_register_test(&device, &mut output), 2);
        assert_eq!(&output[..2], b"ab");
        assert_eq!(read(&device, UARTFR), Ok(UARTFR_TX_READY as u64));
    }

    #[test]
    fn primecell_identification_is_stable_and_read_only() {
        let device = Pl011TxDevice::new("uart0".into(), BASE);
        let expected = [
            (UARTPERIPHID0, 0x11),
            (UARTPERIPHID1, 0x10),
            (UARTPERIPHID2, 0x14),
            (UARTPERIPHID3, 0x00),
            (UARTPCELLID0, 0x0d),
            (UARTPCELLID1, 0xf0),
            (UARTPCELLID2, 0x05),
            (UARTPCELLID3, 0xb1),
        ];
        for (offset, value) in expected {
            assert_eq!(read(&device, offset), Ok(value));
            assert_eq!(write(&device, offset, 0), Err(DeviceError::ReadOnly));
            assert_eq!(read(&device, offset), Ok(value));
        }
    }

    #[test]
    fn configuration_registers_mask_and_read_back_without_irq() {
        let device = Pl011TxDevice::new("uart0".into(), BASE);
        let registers: [(u64, u32, u32); 7] = [
            (UARTILPR, 0xffff_ffff, 0xff),
            (UARTIBRD, 0xffff_ffff, 0xffff),
            (UARTFBRD, 0xffff_ffff, 0x3f),
            (UARTLCR_H, 0xffff_ffff, 0xff),
            (UARTCR, 0xffff_ffff, UARTCR_WRITABLE),
            (UARTIFLS, 0xffff_ffff, 0x3f),
            (UARTIMSC, 0xffff_ffff, UARTIMSC_WRITABLE),
        ];
        for (offset, value, expected) in registers {
            write(&device, offset, value as u64).unwrap();
            assert_eq!(read(&device, offset), Ok(expected as u64));
        }

        assert_eq!(read(&device, UARTRIS), Ok(0));
        assert_eq!(read(&device, UARTMIS), Ok(0));
        write(&device, UARTICR, UARTIMSC_WRITABLE as u64).unwrap();
        assert_eq!(read(&device, UARTRIS), Ok(0));
        assert_eq!(read(&device, UARTMIS), Ok(0));
        assert!(
            device
                .resources()
                .iter()
                .all(|resource| !matches!(resource, Resource::IrqLine { .. }))
        );
    }

    #[test]
    fn dma_enable_is_forced_off_and_observable() {
        let device = Pl011TxDevice::new("uart0".into(), BASE);
        assert_eq!(read(&device, UARTDMACR), Ok(0));
        write(&device, UARTDMACR, UARTDMACR_ENABLE_BITS as u64).unwrap();
        write(&device, UARTDMACR, 0).unwrap();
        assert_eq!(read(&device, UARTDMACR), Ok(0));
        assert_eq!(device.statistics().dma_enable_attempts, 1);
    }

    #[test]
    fn invalid_width_bus_offset_and_boundary_are_controlled_errors() {
        let device = Pl011TxDevice::new("uart0".into(), BASE);
        for width in [AccessWidth::Byte, AccessWidth::Word, AccessWidth::Qword] {
            assert!(matches!(
                device.handle(&access(true, UARTFR, width, 0)),
                Err(DeviceError::InvalidWidth {
                    expected: AccessWidth::Dword,
                    actual,
                }) if actual == width
            ));
        }

        assert!(matches!(
            read(&device, 0x08),
            Err(DeviceError::Unsupported { .. })
        ));
        assert!(matches!(
            device.handle(&BusAccess {
                kind: BusKind::Mmio,
                is_read: true,
                addr: BASE - 4,
                width: AccessWidth::Dword,
                data: 0,
            }),
            Err(DeviceError::OutOfRange { addr }) if addr == BASE - 4
        ));
        assert!(matches!(
            device.handle(&access(
                true,
                PL011_TX_MMIO_SIZE as u64 - 1,
                AccessWidth::Dword,
                0,
            )),
            Err(DeviceError::OutOfRange { addr })
                if addr == BASE + PL011_TX_MMIO_SIZE as u64 - 1
        ));
        assert!(matches!(
            device.handle(&BusAccess {
                kind: BusKind::Port,
                is_read: true,
                addr: BASE,
                width: AccessWidth::Dword,
                data: 0,
            }),
            Err(DeviceError::Unsupported { .. })
        ));
    }

    #[test]
    fn reset_restores_registers_ring_and_counters() {
        let mut device = enabled_device("uart0");
        write(&device, UARTIBRD, 26).unwrap();
        write(&device, UARTIMSC, 0x7ff).unwrap();
        write(&device, UARTDR, b'r' as u64).unwrap();
        write(&device, UARTDMACR, 1).unwrap();

        device.reset().unwrap();

        assert_eq!(read(&device, UARTCR), Ok(UARTCR_RESET as u64));
        assert_eq!(read(&device, UARTIBRD), Ok(0));
        assert_eq!(read(&device, UARTIMSC), Ok(0));
        assert_eq!(device.statistics(), Pl011TxStatistics::default());
        let mut output = [0; 1];
        assert_eq!(drain_tx_for_register_test(&device, &mut output), 0);
        assert_eq!(device.name(), "uart0");
    }

    #[test]
    fn instances_with_the_same_gpa_do_not_share_state_or_tx() {
        let first = enabled_device("vm1-uart");
        let second = enabled_device("vm2-uart");
        write(&first, UARTIBRD, 1).unwrap();
        write(&second, UARTIBRD, 2).unwrap();
        write(&first, UARTDR, b'1' as u64).unwrap();
        write(&second, UARTDR, b'2' as u64).unwrap();

        assert_eq!(read(&first, UARTIBRD), Ok(1));
        assert_eq!(read(&second, UARTIBRD), Ok(2));
        let mut first_output = [0; 1];
        let mut second_output = [0; 1];
        assert_eq!(drain_tx_for_register_test(&first, &mut first_output), 1);
        assert_eq!(drain_tx_for_register_test(&second, &mut second_output), 1);
        assert_eq!(first_output, [b'1']);
        assert_eq!(second_output, [b'2']);
    }

    #[test]
    fn full_ring_drops_without_blocking_and_counts_loss() {
        let device = enabled_device("uart0");
        for value in 0..TX_RING_CAPACITY + 3 {
            write(&device, UARTDR, value as u64).unwrap();
        }

        let stats = device.statistics();
        assert_eq!(stats.total_bytes, (TX_RING_CAPACITY + 3) as u64);
        assert_eq!(stats.buffered_bytes, TX_RING_CAPACITY);
        assert_eq!(stats.dropped_bytes, 3);
    }

    #[test]
    fn factory_builds_one_mmio_only_device_without_resolving_irq() {
        let resolver = RejectingIrqResolver;
        let context = DeviceBuildContext::new(&resolver);
        let bundle = Pl011TxFactory.build(&valid_config(), &context).unwrap();
        assert_eq!(bundle.devices.len(), 1);
        assert!(bundle.pollable.is_empty());
        let device = bundle.devices[0]
            .as_any()
            .downcast_ref::<Pl011TxDevice>()
            .expect("factory must build a PL011 device");
        assert_eq!(device.name(), "guest-uart");
        assert_eq!(
            device.resources(),
            &[Resource::MmioRange {
                base: BASE,
                size: PL011_TX_MMIO_SIZE as u64,
            }]
        );
    }

    #[test]
    fn factory_rejects_every_unsafe_or_ambiguous_configuration() {
        let mut config = valid_config();
        config.name.clear();
        assert_invalid_config(config);

        for name in ["-guest".into(), "guest console".into(), "a".repeat(65)] {
            let mut config = valid_config();
            config.name = name;
            assert_invalid_config(config);
        }

        let mut config = valid_config();
        config.base_gpa += 1;
        assert_invalid_config(config);

        let mut config = valid_config();
        config.length -= 1;
        assert_invalid_config(config);

        let mut config = valid_config();
        config.irq_id = 1;
        assert_invalid_config(config);

        let mut config = valid_config();
        config.cfg_list.push(1);
        assert_invalid_config(config);

        let mut config = valid_config();
        config.emu_type = EmulatedDeviceType::Dummy;
        assert_invalid_config(config);

        let mut config = valid_config();
        config.base_gpa = usize::MAX & !(PL011_TX_MMIO_SIZE - 1);
        assert_invalid_config(config);
    }

    #[cfg(target_arch = "aarch64")]
    #[test]
    fn aarch64_builtin_registry_contains_console_factory() {
        let mut registry = crate::DeviceFactoryRegistry::new();
        register_builtin_factories(&mut registry).unwrap();
        assert!(registry.get(EmulatedDeviceType::Console).is_some());
    }
}
