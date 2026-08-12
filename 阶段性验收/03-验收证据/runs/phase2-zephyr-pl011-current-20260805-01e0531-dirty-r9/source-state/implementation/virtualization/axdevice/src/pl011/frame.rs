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

//! Strict, sink-independent evidence frames for the transmit-only PL011.

use core::fmt;

use super::Pl011TxDevice;

/// Marker required at the start of every Guest-console evidence record.
pub const PL011_TX_FRAME_MARKER: &str = "AXVISOR_GUEST_CONSOLE_FRAME";
/// Current Guest-console evidence protocol version.
pub const PL011_TX_FRAME_VERSION: u8 = 1;
/// Maximum payload accepted by the version-1 demultiplexer.
pub const PL011_TX_FRAME_MAX_PAYLOAD: usize = 4096;

/// A source-ordered snapshot removed from one VM-local PL011 TX ring.
///
/// Formatting this value with [`fmt::Display`] produces exactly one version-1
/// record without a trailing newline. A host backend must write that record at
/// the start of a line and append exactly one newline. The device never calls
/// a sink and therefore never waits for host output while holding its lock.
#[derive(Debug, Eq, PartialEq)]
pub struct Pl011TxFrame<'device, 'payload> {
    /// VM identity permanently bound to this device on its first frame poll.
    pub vm_id: u16,
    /// Canonical ASCII stream name taken from the device configuration.
    pub name: &'device str,
    /// Device generation, starting at zero and advancing after an emitted reset.
    pub generation: u64,
    /// Source-order sequence within this generation.
    pub sequence: u64,
    /// Payload bytes removed by this frame.
    pub payload: &'payload [u8],
    /// Cumulative emitted payload bytes within this generation.
    pub total_bytes: u64,
    /// Lifetime evidence loss, including ring overflow and reset discard.
    pub dropped_bytes: u64,
    /// Lifetime attempts to enable unsupported PL011 DMA.
    pub dma_enable_attempts: u64,
}

impl fmt::Display for Pl011TxFrame<'_, '_> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{PL011_TX_FRAME_MARKER} v={PL011_TX_FRAME_VERSION} vm={} name={} gen={} seq={} \
             len={} total={} dropped={} dma={} hex=",
            self.vm_id,
            self.name,
            self.generation,
            self.sequence,
            self.payload.len(),
            self.total_bytes,
            self.dropped_bytes,
            self.dma_enable_attempts,
        )?;
        for byte in self.payload {
            write!(formatter, "{byte:02x}")?;
        }
        Ok(())
    }
}

/// Fail-closed errors returned before a frame consumes TX bytes.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum Pl011TxFrameError {
    /// VM zero is outside the evidence protocol range.
    #[error("Guest-console frame VM id must be in 1..=65535")]
    InvalidVmId,
    /// A caller tried to relabel an already-bound device.
    #[error("Guest-console device is bound to VM {expected}, not VM {actual}")]
    VmIdentityMismatch {
        /// VM identity established by the first poll.
        expected: u16,
        /// Conflicting VM identity supplied by the caller.
        actual: u16,
    },
    /// No non-empty payload can be represented by the destination.
    #[error("Guest-console frame destination must not be empty")]
    EmptyDestination,
    /// The configured name cannot be consumed by the strict demultiplexer.
    #[error("Guest-console frame name does not match the canonical ASCII contract")]
    InvalidName,
    /// Advancing an evidence counter would lose source ordering.
    #[error("Guest-console frame {counter} counter is exhausted")]
    CounterExhausted {
        /// Counter that could not be advanced exactly.
        counter: &'static str,
    },
}

pub(super) struct Pl011TxFrameState {
    vm_id: Option<u16>,
    generation: u64,
    next_sequence: u64,
    total_bytes: u64,
    emitted_in_generation: bool,
    carried_dropped_bytes: u64,
    carried_dma_enable_attempts: u64,
}

impl Pl011TxFrameState {
    pub(super) const fn new() -> Self {
        Self {
            vm_id: None,
            generation: 0,
            next_sequence: 0,
            total_bytes: 0,
            emitted_in_generation: false,
            carried_dropped_bytes: 0,
            carried_dma_enable_attempts: 0,
        }
    }

    fn bind_vm(&mut self, vm_id: u16) -> Result<(), Pl011TxFrameError> {
        if vm_id == 0 {
            return Err(Pl011TxFrameError::InvalidVmId);
        }
        match self.vm_id {
            Some(expected) if expected != vm_id => Err(Pl011TxFrameError::VmIdentityMismatch {
                expected,
                actual: vm_id,
            }),
            Some(_) => Ok(()),
            None => {
                self.vm_id = Some(vm_id);
                Ok(())
            }
        }
    }

    fn prepare_frame(
        &self,
        payload_len: usize,
        dropped_bytes: u64,
        dma_enable_attempts: u64,
    ) -> Result<(u64, u64, u64, u64), Pl011TxFrameError> {
        let payload_len = u64::try_from(payload_len)
            .map_err(|_| Pl011TxFrameError::CounterExhausted { counter: "total" })?;
        let next_sequence =
            self.next_sequence
                .checked_add(1)
                .ok_or(Pl011TxFrameError::CounterExhausted {
                    counter: "sequence",
                })?;
        let total_bytes = self
            .total_bytes
            .checked_add(payload_len)
            .ok_or(Pl011TxFrameError::CounterExhausted { counter: "total" })?;
        let reported_dropped = self
            .carried_dropped_bytes
            .checked_add(dropped_bytes)
            .ok_or(Pl011TxFrameError::CounterExhausted { counter: "dropped" })?;
        let reported_dma = self
            .carried_dma_enable_attempts
            .checked_add(dma_enable_attempts)
            .ok_or(Pl011TxFrameError::CounterExhausted { counter: "dma" })?;
        Ok((next_sequence, total_bytes, reported_dropped, reported_dma))
    }

    fn commit_frame(&mut self, next_sequence: u64, total_bytes: u64) {
        self.next_sequence = next_sequence;
        self.total_bytes = total_bytes;
        self.emitted_in_generation = true;
    }

    pub(super) fn reset(
        &mut self,
        pending_bytes: usize,
        dropped_bytes: u64,
        dma_enable_attempts: u64,
    ) -> Result<(), Pl011TxFrameError> {
        let generation = if self.emitted_in_generation {
            self.generation
                .checked_add(1)
                .ok_or(Pl011TxFrameError::CounterExhausted {
                    counter: "generation",
                })?
        } else {
            self.generation
        };
        let pending_bytes = u64::try_from(pending_bytes)
            .map_err(|_| Pl011TxFrameError::CounterExhausted { counter: "dropped" })?;
        let carried_dropped_bytes = self
            .carried_dropped_bytes
            .checked_add(dropped_bytes)
            .and_then(|count| count.checked_add(pending_bytes))
            .ok_or(Pl011TxFrameError::CounterExhausted { counter: "dropped" })?;
        let carried_dma_enable_attempts = self
            .carried_dma_enable_attempts
            .checked_add(dma_enable_attempts)
            .ok_or(Pl011TxFrameError::CounterExhausted { counter: "dma" })?;

        self.generation = generation;
        self.next_sequence = 0;
        self.total_bytes = 0;
        self.emitted_in_generation = false;
        self.carried_dropped_bytes = carried_dropped_bytes;
        self.carried_dma_enable_attempts = carried_dma_enable_attempts;
        Ok(())
    }
}

pub(super) fn is_valid_frame_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    if bytes.is_empty() || bytes.len() > 64 || !bytes[0].is_ascii_alphanumeric() {
        return false;
    }
    bytes[1..]
        .iter()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

impl Pl011TxDevice {
    /// Removes one strict evidence frame from this device's TX ring.
    ///
    /// The caller owns `destination`; at most 4096 oldest bytes are copied into
    /// it. Validation and counter-overflow failures leave the ring and frame
    /// counters unchanged. An empty ring returns `Ok(None)`, but still binds
    /// the device to `vm_id` so later callers cannot relabel its source.
    ///
    /// This method does not allocate, call a sink, assert an IRQ, or touch Guest
    /// memory. Runtime code should invoke it outside the vCPU exit path and emit
    /// the returned display record unchanged at the start of a host-log line.
    pub fn drain_tx_frame<'device, 'payload>(
        &'device self,
        vm_id: u16,
        destination: &'payload mut [u8],
    ) -> Result<Option<Pl011TxFrame<'device, 'payload>>, Pl011TxFrameError> {
        if destination.is_empty() {
            return Err(Pl011TxFrameError::EmptyDestination);
        }
        if !is_valid_frame_name(&self.name) {
            return Err(Pl011TxFrameError::InvalidName);
        }

        let mut state = self.state.lock();
        state.frame.bind_vm(vm_id)?;
        if state.tx.is_empty() {
            return Ok(None);
        }

        let payload_len = destination
            .len()
            .min(state.tx.len())
            .min(PL011_TX_FRAME_MAX_PAYLOAD);
        let (next_sequence, total_bytes, dropped_bytes, dma_enable_attempts) = state
            .frame
            .prepare_frame(payload_len, state.dropped_bytes, state.dma_enable_attempts)?;
        for (slot, byte) in destination[..payload_len].iter_mut().zip(state.tx.iter()) {
            *slot = *byte;
        }
        for _ in 0..payload_len {
            let _ = state.tx.pop_front();
        }
        let sequence = state.frame.next_sequence;
        let generation = state.frame.generation;
        state.frame.commit_frame(next_sequence, total_bytes);
        drop(state);

        Ok(Some(Pl011TxFrame {
            vm_id,
            name: &self.name,
            generation,
            sequence,
            payload: &destination[..payload_len],
            total_bytes,
            dropped_bytes,
            dma_enable_attempts,
        }))
    }
}

#[cfg(test)]
mod tests {
    use alloc::{format, string::ToString};

    use axdevice_base::Device;

    use super::*;
    use crate::pl011::{PL011_TX_MMIO_SIZE, UARTCR, UARTCR_TXE, UARTCR_UARTEN, UARTDMACR, UARTDR};

    const BASE: u64 = 0x0900_0000;

    fn enabled_device(name: &str) -> Pl011TxDevice {
        let device = Pl011TxDevice::new(name.to_string(), BASE);
        device
            .write_register(UARTCR, UARTCR_UARTEN | UARTCR_TXE)
            .unwrap();
        device
    }

    fn write_bytes(device: &Pl011TxDevice, bytes: &[u8]) {
        for byte in bytes {
            device.write_register(UARTDR, u32::from(*byte)).unwrap();
        }
    }

    #[test]
    fn display_matches_the_demultiplexer_field_contract() {
        let device = enabled_device("linux");
        write_bytes(&device, &[b'A', b'\n', 0xff]);
        let mut payload = [0; 8];
        let frame = device.drain_tx_frame(1, &mut payload).unwrap().unwrap();

        assert_eq!(
            format!("{frame}"),
            "AXVISOR_GUEST_CONSOLE_FRAME v=1 vm=1 name=linux gen=0 seq=0 len=3 total=3 dropped=0 \
             dma=0 hex=410aff"
        );
    }

    #[test]
    fn frames_preserve_ring_order_and_generation_local_totals() {
        let device = enabled_device("linux");
        write_bytes(&device, b"abcde");

        let mut first_payload = [0; 2];
        let first = device
            .drain_tx_frame(1, &mut first_payload)
            .unwrap()
            .unwrap();
        assert_eq!(
            (first.sequence, first.total_bytes, first.payload),
            (0, 2, &b"ab"[..])
        );

        let mut second_payload = [0; 2];
        let second = device
            .drain_tx_frame(1, &mut second_payload)
            .unwrap()
            .unwrap();
        assert_eq!(
            (second.sequence, second.total_bytes, second.payload),
            (1, 4, &b"cd"[..])
        );

        let mut third_payload = [0; PL011_TX_MMIO_SIZE];
        let third = device
            .drain_tx_frame(1, &mut third_payload)
            .unwrap()
            .unwrap();
        assert_eq!(
            (third.sequence, third.total_bytes, third.payload),
            (2, 5, &b"e"[..])
        );
        assert!(
            device
                .drain_tx_frame(1, &mut third_payload)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn reset_advances_only_an_observed_generation() {
        let mut device = enabled_device("zephyr");
        write_bytes(&device, b"a");
        let mut first_payload = [0; 1];
        let first = device
            .drain_tx_frame(2, &mut first_payload)
            .unwrap()
            .unwrap();
        assert_eq!(
            (first.generation, first.sequence, first.total_bytes),
            (0, 0, 1)
        );

        Device::reset(&mut device).unwrap();
        Device::reset(&mut device).unwrap();
        device
            .write_register(UARTCR, UARTCR_UARTEN | UARTCR_TXE)
            .unwrap();
        write_bytes(&device, b"bc");
        let mut second_payload = [0; 2];
        let second = device
            .drain_tx_frame(2, &mut second_payload)
            .unwrap()
            .unwrap();
        assert_eq!(
            (second.generation, second.sequence, second.total_bytes),
            (1, 0, 2)
        );
    }

    #[test]
    fn vm_binding_and_frame_counters_are_device_local() {
        let first = enabled_device("linux");
        let second = enabled_device("zephyr");
        let mut poll_buffer = [0; 1];
        assert!(first.drain_tx_frame(1, &mut poll_buffer).unwrap().is_none());
        assert!(
            second
                .drain_tx_frame(2, &mut poll_buffer)
                .unwrap()
                .is_none()
        );
        write_bytes(&first, b"1");
        write_bytes(&second, b"2");

        let mut first_payload = [0; 1];
        let first_frame = first
            .drain_tx_frame(1, &mut first_payload)
            .unwrap()
            .unwrap();
        let mut second_payload = [0; 1];
        let second_frame = second
            .drain_tx_frame(2, &mut second_payload)
            .unwrap()
            .unwrap();
        assert_eq!(
            (first_frame.vm_id, first_frame.sequence, first_frame.payload),
            (1, 0, &b"1"[..])
        );
        assert_eq!(
            (
                second_frame.vm_id,
                second_frame.sequence,
                second_frame.payload
            ),
            (2, 0, &b"2"[..])
        );
    }

    #[test]
    fn invalid_inputs_do_not_consume_or_relabel_the_ring() {
        let device = enabled_device("linux");
        write_bytes(&device, b"x");

        assert_eq!(
            device.drain_tx_frame(1, &mut []),
            Err(Pl011TxFrameError::EmptyDestination)
        );
        let mut payload = [0; 1];
        assert_eq!(
            device.drain_tx_frame(0, &mut payload),
            Err(Pl011TxFrameError::InvalidVmId)
        );
        assert!(device.drain_tx_frame(1, &mut payload).unwrap().is_some());

        write_bytes(&device, b"y");
        assert_eq!(
            device.drain_tx_frame(2, &mut payload),
            Err(Pl011TxFrameError::VmIdentityMismatch {
                expected: 1,
                actual: 2,
            })
        );
        let frame = device.drain_tx_frame(1, &mut payload).unwrap().unwrap();
        assert_eq!(
            (frame.sequence, frame.total_bytes, frame.payload),
            (1, 2, &b"y"[..])
        );
    }

    #[test]
    fn invalid_name_and_counter_exhaustion_fail_before_dequeue() {
        let invalid = enabled_device("not a stream");
        write_bytes(&invalid, b"x");
        let mut payload = [0; 1];
        assert_eq!(
            invalid.drain_tx_frame(1, &mut payload),
            Err(Pl011TxFrameError::InvalidName)
        );
        assert_eq!(invalid.statistics().buffered_bytes, 1);

        let exhausted = enabled_device("linux");
        assert!(exhausted.drain_tx_frame(1, &mut payload).unwrap().is_none());
        write_bytes(&exhausted, b"z");
        exhausted.state.lock().frame.next_sequence = u64::MAX;
        assert_eq!(
            exhausted.drain_tx_frame(1, &mut payload),
            Err(Pl011TxFrameError::CounterExhausted {
                counter: "sequence"
            })
        );
        assert_eq!(exhausted.statistics().buffered_bytes, 1);
    }

    #[test]
    fn every_u64_frame_counter_overflow_is_fail_closed() {
        let mut payload = [0; 1];

        let total = enabled_device("linux");
        assert!(total.drain_tx_frame(1, &mut payload).unwrap().is_none());
        write_bytes(&total, b"t");
        total.state.lock().frame.total_bytes = u64::MAX;
        assert_eq!(
            total.drain_tx_frame(1, &mut payload),
            Err(Pl011TxFrameError::CounterExhausted { counter: "total" })
        );
        assert_eq!(total.statistics().buffered_bytes, 1);

        let dropped = enabled_device("linux");
        assert!(dropped.drain_tx_frame(1, &mut payload).unwrap().is_none());
        write_bytes(&dropped, b"d");
        {
            let mut state = dropped.state.lock();
            state.frame.carried_dropped_bytes = u64::MAX;
            state.dropped_bytes = 1;
        }
        assert_eq!(
            dropped.drain_tx_frame(1, &mut payload),
            Err(Pl011TxFrameError::CounterExhausted { counter: "dropped" })
        );
        assert_eq!(dropped.statistics().buffered_bytes, 1);

        let dma = enabled_device("linux");
        assert!(dma.drain_tx_frame(1, &mut payload).unwrap().is_none());
        write_bytes(&dma, b"m");
        {
            let mut state = dma.state.lock();
            state.frame.carried_dma_enable_attempts = u64::MAX;
            state.dma_enable_attempts = 1;
        }
        assert_eq!(
            dma.drain_tx_frame(1, &mut payload),
            Err(Pl011TxFrameError::CounterExhausted { counter: "dma" })
        );
        assert_eq!(dma.statistics().buffered_bytes, 1);

        let mut generation = enabled_device("zephyr");
        write_bytes(&generation, b"g");
        let emitted = generation.drain_tx_frame(2, &mut payload).unwrap().unwrap();
        assert_eq!(emitted.sequence, 0);
        generation.state.lock().frame.generation = u64::MAX;
        write_bytes(&generation, b"p");
        assert!(matches!(
            Device::reset(&mut generation),
            Err(axdevice_base::DeviceError::InvalidState { .. })
        ));
        assert_eq!(generation.statistics().buffered_bytes, 1);
    }

    #[test]
    fn reset_discard_and_dma_attempt_remain_fail_closed_evidence() {
        let mut device = enabled_device("zephyr");
        write_bytes(&device, b"a");
        let mut first_payload = [0; 1];
        let first = device
            .drain_tx_frame(2, &mut first_payload)
            .unwrap()
            .unwrap();
        assert_eq!(first.generation, 0);

        write_bytes(&device, b"lost");
        device.write_register(UARTDMACR, 1).unwrap();
        Device::reset(&mut device).unwrap();
        device
            .write_register(UARTCR, UARTCR_UARTEN | UARTCR_TXE)
            .unwrap();
        write_bytes(&device, b"n");

        let mut payload = [0; 1];
        let frame = device.drain_tx_frame(2, &mut payload).unwrap().unwrap();
        assert_eq!(frame.generation, 1);
        assert_eq!(frame.dropped_bytes, 4);
        assert_eq!(frame.dma_enable_attempts, 1);
    }

    #[test]
    fn stream_name_validation_matches_the_python_contract() {
        for valid in ["a", "linux", "vm-1.console_log", &"a".repeat(64)] {
            assert!(is_valid_frame_name(valid));
        }
        for invalid in [
            "",
            "-linux",
            "a/b",
            "guest name",
            "guest\n",
            &"a".repeat(65),
        ] {
            assert!(!is_valid_frame_name(invalid));
        }
    }
}
