//! Configured VirtIO MMIO network devices connected by an internal L2 switch.

use core::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::{
    boxed::Box,
    collections::{BTreeMap, VecDeque},
    format,
    string::String,
    sync::{Arc, Mutex, MutexGuard},
    vec::Vec,
};

use axdevice::*;
use axdevice_base::{
    BusKind, Device, DeviceAccess, DeviceContext, DeviceError, DmaGrant, InterruptSharing,
    InterruptTrigger, IrqLine, Resource,
};
use axvirtio_common::{GuestMemory, NoGuestMemoryAccessor, VirtioError};
use axvirtio_net::{
    DeviceEvent, NetworkBackend, NetworkBackendError, RxOutcome, VirtioMmioNetDevice,
    VirtioNetConfig,
    switch::{
        EgressOutcome, IngressOutcome, SwitchPort, SwitchPortId, SwitchPortRegistration,
        VirtualSwitch,
    },
};
use axvm_types::GuestPhysAddr;
use axvmconfig::VirtualDeviceRequest;

use crate::{ConfiguredDeviceError, ConfiguredModelRegistration, DeviceInstantiationContext};

const MMIO_SLOT: &str = "mmio";
const IRQ_SLOT: &str = "irq";
const MMIO_SIZE: u64 = 0x200;
const INGRESS_CAPACITY: usize = 64;
const MAX_CONTEST_FRAME_SIZE: usize = 1514;
const FAULT_PROFILE_P4_NETWORK_V1: &str = "p4-network-fault-v1";

/// Lowercase hex dump of an Ethernet frame for the Guest-runtime v2 evidence
/// path (P4-EVID-01B). The host-side publisher parses
/// `virtio-net frame vm=.. dir=tx len=.. hex=..` lines into frames.jsonl and
/// capture.pcap; the exact bytes let the qualification validator verify
/// per-frame SHA-256 and PCAP content instead of trusting log substrings.
fn hex_dump(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    out
}

static PORT_GENERATIONS: Mutex<BTreeMap<usize, usize>> = Mutex::new(BTreeMap::new());
static INTERNAL_SWITCH: Mutex<Option<Arc<VirtualSwitch>>> = Mutex::new(None);

/// Catalog entry for `[[devices.virtual]] model = "virtio-net"`.
pub const REGISTRATION: ConfiguredModelRegistration = ConfiguredModelRegistration {
    model: "virtio-net",
    create: create_device_node,
};

pub(super) fn register(
    catalog: &mut crate::ConfiguredDeviceCatalog,
) -> Result<(), ConfiguredDeviceError> {
    catalog.register(module_path!(), REGISTRATION)
}

fn create_device_node(
    id: DeviceNodeId,
    request: &VirtualDeviceRequest,
    context: &DeviceInstantiationContext,
) -> Result<DeviceNodeSpec, ConfiguredDeviceError> {
    let guest_mac = parse_mac(request, "guest_mac")?;
    let fault_policy = parse_fault_policy(request)?;
    let controller =
        context
            .default_wired_controller()
            .ok_or_else(|| ConfiguredDeviceError::Instantiation {
                device: request.id.clone(),
                model: request.model.clone(),
                detail: "virtio-net requires a wired interrupt controller".into(),
            })?;
    let model: Arc<dyn DeviceModel> = Arc::new(VirtioNetModel {
        guest_mac,
        fault_policy,
        controller,
        vm_id: context
            .vm_id()
            .ok_or_else(|| ConfiguredDeviceError::Instantiation {
                device: request.id.clone(),
                model: request.model.clone(),
                detail: "virtio-net requires a VM identity".into(),
            })?,
    });
    let mut node = DeviceNodeSpec::virtual_device(id, model);
    if let Some(controller_node) = context.default_wired_controller_node() {
        node = node.with_dependency(controller_node.clone());
    }
    Ok(node)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum FaultProfile {
    Disabled,
    P4NetworkV1,
}

#[derive(Clone, Copy, Debug)]
struct FaultPolicy {
    profile: FaultProfile,
    seed: u64,
}

impl FaultPolicy {
    const fn disabled() -> Self {
        Self {
            profile: FaultProfile::Disabled,
            seed: 0,
        }
    }

    const fn enabled(seed: u64) -> Self {
        Self {
            profile: FaultProfile::P4NetworkV1,
            seed,
        }
    }

    const fn enabled_for_contest(self) -> bool {
        matches!(self.profile, FaultProfile::P4NetworkV1)
    }
}

fn parse_fault_policy(request: &VirtualDeviceRequest) -> Result<FaultPolicy, ConfiguredDeviceError> {
    let enabled = request.options.get("fault_enabled");
    let seed = request.options.get("fault_seed");
    let profile = request.options.get("fault_profile");
    if enabled.is_none() && seed.is_none() && profile.is_none() {
        return Ok(FaultPolicy::disabled());
    }

    let enabled = enabled
        .and_then(toml::Value::as_bool)
        .ok_or_else(|| invalid_options(request, "`fault_enabled` must be a boolean".into()))?;
    let seed = seed
        .and_then(toml::Value::as_integer)
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(|| invalid_options(request, "`fault_seed` must be a non-negative u64".into()))?;
    let profile = profile
        .and_then(toml::Value::as_str)
        .ok_or_else(|| invalid_options(request, "`fault_profile` must be a string".into()))?;
    if profile != FAULT_PROFILE_P4_NETWORK_V1 {
        return Err(invalid_options(
            request,
            format!("unsupported `fault_profile` `{profile}`"),
        ));
    }
    if enabled {
        Ok(FaultPolicy::enabled(seed))
    } else {
        Ok(FaultPolicy::disabled())
    }
}

fn parse_mac(request: &VirtualDeviceRequest, key: &str) -> Result<[u8; 6], ConfiguredDeviceError> {
    let values = request
        .options
        .get(key)
        .and_then(toml::Value::as_array)
        .ok_or_else(|| invalid_options(request, format!("missing six-octet array `{key}`")))?;
    if values.len() != 6 {
        return Err(invalid_options(
            request,
            format!("`{key}` must contain exactly six octets"),
        ));
    }
    let mut mac = [0u8; 6];
    for (octet, value) in mac.iter_mut().zip(values) {
        *octet = value
            .as_integer()
            .and_then(|value| u8::try_from(value).ok())
            .ok_or_else(|| invalid_options(request, format!("`{key}` contains a non-u8 octet")))?;
    }
    if mac == [0; 6] || mac[0] & 1 != 0 {
        return Err(invalid_options(
            request,
            format!("`{key}` must be a nonzero unicast MAC address"),
        ));
    }
    Ok(mac)
}

fn invalid_options(request: &VirtualDeviceRequest, detail: String) -> ConfiguredDeviceError {
    ConfiguredDeviceError::InvalidOptions {
        device: request.id.clone(),
        model: request.model.clone(),
        detail,
    }
}

struct VirtioNetModel {
    guest_mac: [u8; 6],
    fault_policy: FaultPolicy,
    controller: axdevice_base::InterruptControllerId,
    vm_id: usize,
}

impl DeviceModel for VirtioNetModel {
    fn requirements(&self) -> DeviceManagerResult<DeviceRequirements> {
        DeviceRequirements::new()
            .with_mmio(
                ResourceSlot::new(MMIO_SLOT)?,
                MMIO_SIZE,
                MMIO_SIZE,
                ResourceRequest::Auto,
            )?
            .with_wired_irq(
                ResourceSlot::new(IRQ_SLOT)?,
                self.controller,
                InterruptTrigger::EdgeTriggered,
                InterruptSharing::Exclusive,
                ResourceRequest::Auto,
            )
    }

    fn firmware(&self) -> DeviceFirmwareSpec {
        let registers = ResourceSlot::new(MMIO_SLOT).expect("static slot is valid");
        let interrupt = ResourceSlot::new(IRQ_SLOT).expect("static slot is valid");
        DeviceFirmwareSpec::interfaces(
            Some(std::vec![FdtContributionSpec::Conventional(
                FdtNodeSpec::new("virtio_mmio")
                    .with_compatible("virtio,mmio")
                    .with_register(registers.clone())
                    .with_interrupt(interrupt.clone())
                    .with_empty_property("dma-coherent"),
            )]),
            Some(std::vec![AcpiContributionSpec::Conventional(
                AcpiDeviceSpec::new_indexed("VN", "LNRO0005")
                    .with_register(registers)
                    .with_interrupt(interrupt),
            )]),
        )
    }

    fn build(&self, context: &mut DeviceBuildContext<'_>) -> DeviceManagerResult<DeviceBundle> {
        let (base, size) = context.mmio(MMIO_SLOT)?;
        let irq = context.irq(IRQ_SLOT)?;
        let irq_id = irq.input().value() as u32;
        info!(
            "virtio-net vm={} resolved MMIO=0x{:x}+0x{:x} wired IRQ INTID={irq_id}",
            self.vm_id, base, size
        );
        let switch = internal_switch();
        // SwitchPortId carries the actual VM identity.  A monotonically
        // increasing per-VM generation makes a stale Arc harmless after a
        // VM/device teardown and rebuild; the contest profile has one NIC per
        // VM, hence device_index=0.
        let port_id = SwitchPortId::new(self.vm_id, next_port_generation(self.vm_id), 0);
        let endpoint = PortEndpoint::new(
            port_id,
            self.guest_mac,
            switch.clone(),
            Arc::new(AxvmWakeTarget { vm_id: self.vm_id }),
            self.fault_policy,
        );
        let registration = switch.register_owned(endpoint.clone()).map_err(|error| {
            DeviceManagerError::InvalidConfig {
                operation: "register virtio-net switch port",
                detail: format!("{error:?}"),
            }
        })?;
        endpoint.activate();

        let backend = SwitchBackend {
            endpoint: endpoint.clone(),
            switch,
        };
        let model = Arc::new(
            VirtioMmioNetDevice::new(
                GuestPhysAddr::from(base as usize),
                size as usize,
                backend,
                VirtioNetConfig::new(self.guest_mac),
                NoGuestMemoryAccessor,
            )
            .map_err(|error| DeviceManagerError::InvalidConfig {
                operation: "construct virtio-net device",
                detail: format!("{error:?}"),
            })?,
        );
        let grant = DmaGrant::new();
        let device = Arc::new(VirtioNetRuntimeDevice {
            model,
            irq,
            grant: grant.clone(),
            endpoint,
            rx_delivered: AtomicUsize::new(0),
            rx_no_guest_buffer: AtomicUsize::new(0),
            rx_errors: AtomicUsize::new(0),
            _registration: registration,
            resources: std::vec![
                Resource::MmioRange { base, size },
                Resource::IrqLine {
                    line: irq_id,
                    trigger: InterruptTrigger::EdgeTriggered,
                },
            ]
            .into_boxed_slice(),
        });
        let mut bundle = DeviceBundle::new();
        bundle.add_dma_pollable_device(device.clone(), device, grant);
        Ok(bundle)
    }
}

fn internal_switch() -> Arc<VirtualSwitch> {
    let mut slot = INTERNAL_SWITCH
        .lock()
        .expect("virtio-net switch mutex poisoned");
    slot.get_or_insert_with(VirtualSwitch::new).clone()
}

fn next_port_generation(vm_id: usize) -> usize {
    let mut generations = PORT_GENERATIONS
        .lock()
        .expect("virtio-net generation mutex poisoned");
    let next = generations.entry(vm_id).or_insert(0);
    let generation = *next;
    *next = next.wrapping_add(1);
    generation
}

fn contest_ingress_port(vm_id: usize) -> Option<u8> {
    match vm_id {
        1 => Some(0),
        2 => Some(1),
        _ => None,
    }
}

struct FaultFrame {
    sequence: u64,
    bytes: Vec<u8>,
}

struct FaultDirectionState {
    policy: FaultPolicy,
    sequence: u64,
    reorder_pending: Option<FaultFrame>,
}

impl FaultDirectionState {
    fn new(policy: FaultPolicy) -> Self {
        Self {
            policy,
            sequence: 0,
            reorder_pending: None,
        }
    }

    fn reset(&mut self) {
        self.sequence = 0;
        self.reorder_pending = None;
    }

    fn process(&mut self, frame: &[u8]) -> Vec<Vec<u8>> {
        if !self.policy.enabled_for_contest() {
            return vec![frame.into()];
        }
        let Some(_) = contest_sequence(frame) else {
            return vec![frame.into()];
        };

        self.sequence = self.sequence.wrapping_add(1);
        let sequence = self.sequence;
        let mut current = FaultFrame {
            sequence,
            bytes: frame.into(),
        };
        if sequence % 29 == 0 {
            corrupt_frame(&mut current.bytes, self.policy.seed, sequence);
        }

        let ordered = if sequence % 23 == 0 {
            if self.reorder_pending.is_none() {
                self.reorder_pending = Some(current);
                return Vec::new();
            }
            let pending = self.reorder_pending.take().expect("reorder pending exists");
            vec![current, pending]
        } else if let Some(pending) = self.reorder_pending.take() {
            vec![current, pending]
        } else {
            vec![current]
        };

        let mut output = Vec::new();
        for item in ordered {
            if item.sequence % 10 == 0 {
                continue;
            }
            output.push(item.bytes.clone());
            if item.sequence % 17 == 0 {
                output.push(item.bytes);
            }
        }
        output
    }
}

fn contest_sequence(frame: &[u8]) -> Option<u32> {
    let ethertype = frame.get(12..14)?;
    if ethertype != [0x08, 0x00] {
        return None;
    }
    let version_and_header_len = *frame.get(14)?;
    let ip_header_len = usize::from(version_and_header_len & 0x0f) * 4;
    if version_and_header_len >> 4 != 4
        || ip_header_len < 20
        || frame.get(23)? != &17
    {
        return None;
    }
    let fragment = u16::from_be_bytes(frame.get(20..22)?.try_into().ok()?);
    if fragment & 0x3fff != 0 {
        return None;
    }
    let udp_offset = 14 + ip_header_len;
    let udp_length = usize::from(u16::from_be_bytes(
        frame.get(udp_offset + 4..udp_offset + 6)?.try_into().ok()?,
    ));
    if udp_length < 8 {
        return None;
    }
    let udp_payload = frame.get(udp_offset + 8..udp_offset + udp_length)?;
    if udp_payload.len() >= 8 && udp_payload.get(..4) == Some(b"P4UD") {
        return Some(u32::from_be_bytes(udp_payload[4..8].try_into().ok()?));
    }
    const ICPC_HEADER_SIZE: usize = 36;
    const ICPC_CONTROL_PAYLOAD_SIZE: usize = 24;
    const ICPC_MESSAGE_CONTROL: u8 = 1;
    if udp_payload.len() == ICPC_HEADER_SIZE + ICPC_CONTROL_PAYLOAD_SIZE
        && udp_payload.get(..4) == Some(b"ICPC")
        && udp_payload[4] == 1
        && usize::from(udp_payload[5]) == ICPC_HEADER_SIZE
        && udp_payload[6] == ICPC_MESSAGE_CONTROL
        && usize::from(u16::from_be_bytes(
            udp_payload[28..30].try_into().ok()?,
        ))
            == ICPC_CONTROL_PAYLOAD_SIZE
        && udp_payload[30..32] == [0, 0]
    {
        return Some(u32::from_be_bytes(udp_payload[12..16].try_into().ok()?));
    }
    None
}

fn corrupt_frame(frame: &mut [u8], seed: u64, sequence: u64) {
    if frame.len() <= 14 {
        return;
    }
    let offset = 14 + ((seed ^ sequence) as usize % (frame.len() - 14));
    let bit = 1u8 << ((seed.wrapping_add(sequence) & 7) as u8);
    frame[offset] ^= bit;
}

#[derive(Clone)]
struct SwitchBackend {
    endpoint: Arc<PortEndpoint>,
    switch: Arc<VirtualSwitch>,
}

impl NetworkBackend for SwitchBackend {
    fn transmit(&self, frame: &[u8]) -> Result<(), NetworkBackendError> {
        // The official core accepts a future-facing 64 KiB maximum. This
        // contest profile fixes MTU=1500 and never negotiates jumbo-frame
        // support, so reject an oversized Guest frame before logging or
        // copying it into the internal switch.
        if frame.len() > MAX_CONTEST_FRAME_SIZE {
            warn!(
                "virtio-net TX rejected from vm={} len={} max={MAX_CONTEST_FRAME_SIZE}",
                self.endpoint.id().vm_id,
                frame.len()
            );
            return Err(NetworkBackendError::TransmitFailed);
        }
        let frames = self.endpoint.apply_fault(frame);
        if frames.is_empty() {
            info!(
                "virtio-net fault drop from vm={} generation={}",
                self.endpoint.id().vm_id,
                self.endpoint.id().generation
            );
            return Ok(());
        }
        for frame in frames {
            self.transmit_one(&frame);
        }
        Ok(())
    }

    fn transmit_one(&self, frame: &[u8]) {
        // Contest diagnostics: log the exact frame direction/length/MACs and
        // any bounded-switch egress drop. This is evidence for the internal
        // vnet0 L2 path and must be kept distinct from an outer NIC.
        if let Some(header) = frame.get(..12) {
            info!(
                "virtio-net TX from vm={} len={} dst={:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x} src={:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}",
                self.endpoint.id().vm_id,
                frame.len(),
                header[0],
                header[1],
                header[2],
                header[3],
                header[4],
                header[5],
                header[6],
                header[7],
                header[8],
                header[9],
                header[10],
                header[11]
            );
        } else {
            warn!(
                "virtio-net TX from vm={} len={} has no complete Ethernet MAC header",
                self.endpoint.id().vm_id,
                frame.len()
            );
        }
        let outcome = self.switch.switch_from_port(self.endpoint.id(), frame);
        match outcome {
            EgressOutcome::Dropped(reason) => {
                let stats = self.switch.stats();
                warn!(
                    "virtio-net switch drop from vm={} generation={} reason={:?} ingress_full_drop={} inactive_target_drop={}",
                    self.endpoint.id().vm_id,
                    self.endpoint.id().generation,
                    reason,
                    stats.ingress_full_drop.load(Ordering::Relaxed),
                    stats.inactive_target_drop.load(Ordering::Relaxed)
                );
            }
            EgressOutcome::Forwarded {
                uplink,
                local_deliveries,
            } => {
                // P4-EVID-01B evidence is emitted only after the switch has
                // accepted the frame (including length, active-generation and
                // source-MAC checks) and at least one local endpoint accepted
                // it. Uplink-only classification is not Guest-to-Guest
                // delivery evidence because this contest adapter has no outer
                // uplink. VM ids 1/2 map to the frozen evidence ports 0/1.
                if local_deliveries == 0 {
                    let stats = self.switch.stats();
                    debug!(
                        "virtio-net frame evidence skipped from vm={} generation={} local_deliveries=0 uplink_requested={uplink} ingress_full_drop={} inactive_target_drop={}",
                        self.endpoint.id().vm_id,
                        self.endpoint.id().generation,
                        stats.ingress_full_drop.load(Ordering::Relaxed),
                        stats.inactive_target_drop.load(Ordering::Relaxed)
                    );
                } else if let Some(port) = contest_ingress_port(self.endpoint.id().vm_id) {
                    let direction = if port == 0 {
                        "port0_to_port1"
                    } else {
                        "port1_to_port0"
                    };
                    let stats = self.switch.stats();
                    info!(
                        "virtio-net frame vm={} generation={} port={} dir={} len={} hex={} ingress_full_drop={} inactive_target_drop={}",
                        self.endpoint.id().vm_id,
                        self.endpoint.id().generation,
                        port,
                        direction,
                        frame.len(),
                        hex_dump(frame),
                        stats.ingress_full_drop.load(Ordering::Relaxed),
                        stats.inactive_target_drop.load(Ordering::Relaxed)
                    );
                } else {
                    warn!(
                        "virtio-net frame evidence skipped for unmapped vm={} generation={}",
                        self.endpoint.id().vm_id,
                        self.endpoint.id().generation
                    );
                }
            }
        }
    }
}

struct PortEndpoint {
    id: SwitchPortId,
    mac: [u8; 6],
    ingress: Mutex<VecDeque<Vec<u8>>>,
    fault: Mutex<FaultDirectionState>,
    active: AtomicBool,
    ingress_full_drops: AtomicUsize,
    ingress_inactive_drops: AtomicUsize,
    wake_target: Arc<dyn WakeTarget>,
    _switch: Arc<VirtualSwitch>,
}

trait WakeTarget: Send + Sync {
    fn notify(&self);
}

struct AxvmWakeTarget {
    vm_id: usize,
}

impl WakeTarget for AxvmWakeTarget {
    fn notify(&self) {
        // Wake only; vCPU0 polls DMA devices at the top of its next run-loop
        // iteration. Polling synchronously from the sender's device access
        // would let two VM device runtimes re-enter each other.
        if let Err(error) = crate::notify_vm_vcpu(self.vm_id, 0) {
            warn!(
                "failed to notify VM[{}] for virtio-net RX: {error:#}",
                self.vm_id
            );
        }
    }
}

impl PortEndpoint {
    fn new(
        id: SwitchPortId,
        mac: [u8; 6],
        switch: Arc<VirtualSwitch>,
        wake_target: Arc<dyn WakeTarget>,
        fault_policy: FaultPolicy,
    ) -> Arc<Self> {
        Arc::new(Self {
            id,
            mac,
            ingress: Mutex::new(VecDeque::new()),
            fault: Mutex::new(FaultDirectionState::new(fault_policy)),
            active: AtomicBool::new(false),
            ingress_full_drops: AtomicUsize::new(0),
            ingress_inactive_drops: AtomicUsize::new(0),
            wake_target,
            _switch: switch,
        })
    }

    fn activate(&self) {
        self.active.store(true, Ordering::Release);
    }

    fn deactivate(&self) {
        // The ingress mutex is the lifecycle linearization point. A delivery
        // that owns it first is accepted before teardown; after this store no
        // stale switch snapshot can append another frame.
        let mut ingress = self.lock_ingress();
        self.active.store(false, Ordering::Release);
        ingress.clear();
        drop(ingress);
        self.fault
            .lock()
            .expect("virtio-net fault mutex poisoned")
            .reset();
        info!(
            "virtio-net ingress counters final vm={} generation={} full_drop={} inactive_drop={}",
            self.id.vm_id,
            self.id.generation,
            self.ingress_full_drops.load(Ordering::Relaxed),
            self.ingress_inactive_drops.load(Ordering::Relaxed)
        );
    }

    fn pop_ingress(&self) -> Option<Vec<u8>> {
        self.lock_ingress().pop_front()
    }

    fn requeue_ingress(&self, frame: Vec<u8>) -> IngressOutcome {
        let mut ingress = self.lock_ingress();
        if !self.is_active() {
            let count = self.increment_ingress_inactive_drop();
            drop(ingress);
            warn!(
                "virtio-net ingress inactive-drop count={count} vm={} generation={} source=requeue",
                self.id.vm_id, self.id.generation
            );
            return IngressOutcome::Inactive;
        }
        if ingress.len() >= INGRESS_CAPACITY {
            let count = self.increment_ingress_full_drop();
            drop(ingress);
            if count == 1 || count.is_power_of_two() {
                warn!(
                    "virtio-net ingress full-drop count={count} vm={} generation={} capacity={INGRESS_CAPACITY} source=requeue",
                    self.id.vm_id, self.id.generation
                );
            }
            return IngressOutcome::Full;
        }
        ingress.push_front(frame);
        IngressOutcome::Accepted
    }

    fn increment_ingress_full_drop(&self) -> usize {
        self.ingress_full_drops
            .fetch_add(1, Ordering::Relaxed)
            + 1
    }

    fn increment_ingress_inactive_drop(&self) -> usize {
        self.ingress_inactive_drops
            .fetch_add(1, Ordering::Relaxed)
            + 1
    }

    fn lock_ingress(&self) -> MutexGuard<'_, VecDeque<Vec<u8>>> {
        self.ingress
            .lock()
            .expect("virtio-net ingress mutex poisoned")
    }

    fn apply_fault(&self, frame: &[u8]) -> Vec<Vec<u8>> {
        self.fault
            .lock()
            .expect("virtio-net fault mutex poisoned")
            .process(frame)
    }
}

impl SwitchPort for PortEndpoint {
    fn id(&self) -> SwitchPortId {
        self.id
    }

    fn guest_mac(&self) -> [u8; 6] {
        self.mac
    }

    fn is_active(&self) -> bool {
        self.active.load(Ordering::Acquire)
    }

    fn deliver_ingress(&self, frame: &[u8]) -> IngressOutcome {
        let mut ingress = self.lock_ingress();
        if !self.is_active() {
            let count = self.increment_ingress_inactive_drop();
            drop(ingress);
            // Teardown-race rejections should be rare. Publish every
            // cumulative value so a stale worker that runs after the final
            // snapshot remains reconstructable from the merged log.
            warn!(
                "virtio-net ingress inactive-drop count={count} vm={} generation={}",
                self.id.vm_id, self.id.generation
            );
            return IngressOutcome::Inactive;
        }
        if ingress.len() >= INGRESS_CAPACITY {
            let count = self.increment_ingress_full_drop();
            drop(ingress);
            if count == 1 || count.is_power_of_two() {
                warn!(
                    "virtio-net ingress full-drop count={count} vm={} generation={} capacity={INGRESS_CAPACITY}",
                    self.id.vm_id, self.id.generation
                );
            }
            return IngressOutcome::Full;
        }
        ingress.push_back(frame.into());
        // Keep enqueue + wake on the same lifecycle side of `deactivate()`.
        // The wake target only queues a vCPU notification; it never polls the
        // device synchronously, so holding this mutex cannot re-enter ingress.
        self.wake_target.notify();
        IngressOutcome::Accepted
    }
}

struct ScopedDeviceMemory<'a> {
    context: &'a mut dyn DeviceContext,
    grant: &'a DmaGrant,
}

impl GuestMemory for ScopedDeviceMemory<'_> {
    fn read(&mut self, guest_addr: GuestPhysAddr, data: &mut [u8]) -> Result<(), VirtioError> {
        self.context
            .read_guest_memory(self.grant, guest_addr, data)
            .map_err(|_| VirtioError::InvalidAddress)
    }

    fn write(&mut self, guest_addr: GuestPhysAddr, data: &[u8]) -> Result<(), VirtioError> {
        self.context
            .write_guest_memory(self.grant, guest_addr, data)
            .map_err(|_| VirtioError::InvalidAddress)
    }
}

struct VirtioNetRuntimeDevice {
    model: Arc<VirtioMmioNetDevice<SwitchBackend, NoGuestMemoryAccessor>>,
    irq: IrqLine,
    grant: DmaGrant,
    endpoint: Arc<PortEndpoint>,
    rx_delivered: AtomicUsize,
    rx_no_guest_buffer: AtomicUsize,
    rx_errors: AtomicUsize,
    _registration: SwitchPortRegistration,
    resources: Box<[Resource]>,
}

impl Drop for VirtioNetRuntimeDevice {
    fn drop(&mut self) {
        // Deactivate before SwitchPortRegistration drops and unregisters the
        // table entry.  Switch workers holding a stale Arc then fail closed.
        self.endpoint.deactivate();
    }
}

impl Device for VirtioNetRuntimeDevice {
    fn name(&self) -> &str {
        "virtio-net"
    }

    fn resources(&self) -> &[Resource] {
        &self.resources
    }

    fn read(
        &self,
        access: &DeviceAccess,
        _context: &mut dyn DeviceContext,
    ) -> Result<u64, DeviceError> {
        if access.bus() != BusKind::Mmio {
            return Err(DeviceError::OutOfRange {
                addr: access.address(),
            });
        }
        self.model
            .mmio_read(
                GuestPhysAddr::from(access.address() as usize),
                access.width(),
            )
            .map(|value| value as u64)
            .map_err(map_virtio_error)
    }

    fn write(
        &self,
        access: &DeviceAccess,
        value: u64,
        context: &mut dyn DeviceContext,
    ) -> Result<(), DeviceError> {
        if access.bus() != BusKind::Mmio {
            return Err(DeviceError::OutOfRange {
                addr: access.address(),
            });
        }
        let mut memory = ScopedDeviceMemory {
            context,
            grant: &self.grant,
        };
        let event = self
            .model
            .mmio_write_with_memory(
                GuestPhysAddr::from(access.address() as usize),
                access.width(),
                value as usize,
                &mut memory,
            )
            .map_err(map_virtio_error)?;
        self.pulse_if_pending(event)?;
        Ok(())
    }
}

impl DmaPollableDeviceOps for VirtioNetRuntimeDevice {
    fn poll_dma(
        &self,
        _now_ns: u64,
        context: &mut dyn DeviceContext,
        grant: &DmaGrant,
    ) -> DeviceManagerResult {
        let mut memory = ScopedDeviceMemory { context, grant };
        while let Some(frame) = self.endpoint.pop_ingress() {
            match self.model.receive_frame_with_memory(&frame, &mut memory) {
                Ok(RxOutcome::Delivered { notify, .. }) => {
                    let delivered = self.rx_delivered.fetch_add(1, Ordering::Relaxed) + 1;
                    if notify {
                        info!(
                            "virtio-net RX delivered={delivered} to mac={:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}, pulsing IRQ",
                            self.endpoint.mac[0],
                            self.endpoint.mac[1],
                            self.endpoint.mac[2],
                            self.endpoint.mac[3],
                            self.endpoint.mac[4],
                            self.endpoint.mac[5]
                        );
                        self.irq
                            .pulse()
                            .map_err(|error| DeviceManagerError::InvalidState {
                                operation: "pulse virtio-net RX interrupt",
                                detail: format!("{error}"),
                            })?;
                    } else {
                        info!(
                            "virtio-net RX delivered={delivered} to mac={:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}, notify=false",
                            self.endpoint.mac[0],
                            self.endpoint.mac[1],
                            self.endpoint.mac[2],
                            self.endpoint.mac[3],
                            self.endpoint.mac[4],
                            self.endpoint.mac[5]
                        );
                    }
                }
                Ok(RxOutcome::NoGuestBuffer) => {
                    let requeue = self.endpoint.requeue_ingress(frame);
                    if requeue != IngressOutcome::Accepted {
                        warn!(
                            "virtio-net RX no-guest-buffer requeue rejected vm={} generation={} outcome={requeue:?}",
                            self.endpoint.id.vm_id,
                            self.endpoint.id.generation
                        );
                    }
                    let count = self
                        .rx_no_guest_buffer
                        .fetch_add(1, Ordering::Relaxed)
                        + 1;
                    if count == 1 || count.is_power_of_two() {
                        warn!(
                            "virtio-net RX no-guest-buffer count={count} vm={} delivered={} interrupt_status=0x{:x}",
                            self.endpoint.id.vm_id,
                            self.rx_delivered.load(Ordering::Relaxed),
                            self.model.interrupt_status()
                        );
                    }
                    break;
                }
                Err(error) => {
                    let count = self.rx_errors.fetch_add(1, Ordering::Relaxed) + 1;
                    if count == 1 || count.is_power_of_two() {
                        warn!(
                            "virtio-net drops an ingress frame count={count} vm={} rx_queue_faulted={} delivered={}: {error:?}",
                            self.endpoint.id.vm_id,
                            self.model.is_queue_faulted(0),
                            self.rx_delivered.load(Ordering::Relaxed)
                        );
                    }
                }
            }
        }
        Ok(())
    }
}

impl VirtioNetRuntimeDevice {
    fn pulse_if_pending(&self, event: DeviceEvent) -> Result<(), DeviceError> {
        if event == DeviceEvent::InterruptPending {
            self.irq.pulse().map_err(|error| DeviceError::Backend {
                operation: "pulse virtio-net interrupt",
                detail: format!("{error}"),
            })?;
        }
        Ok(())
    }
}

fn map_virtio_error(error: VirtioError) -> DeviceError {
    DeviceError::InvalidInput {
        operation: "access virtio-net MMIO transport",
        detail: format!("{error:?}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct CountingWakeTarget(AtomicUsize);

    impl WakeTarget for CountingWakeTarget {
        fn notify(&self) {
            self.0.fetch_add(1, Ordering::Relaxed);
        }
    }

    #[test]
    fn endpoint_enforces_drop_newest_capacity_and_teardown_gate() {
        let switch = VirtualSwitch::new();
        let wake_target = Arc::new(CountingWakeTarget(AtomicUsize::new(0)));
        let endpoint = PortEndpoint::new(
            SwitchPortId::new(2, 9, 0),
            [0x02, 0, 0, 0, 0, 2],
            switch,
            wake_target.clone(),
        );
        endpoint.activate();

        for marker in 0u8..INGRESS_CAPACITY as u8 {
            assert_eq!(
                endpoint.deliver_ingress(&[marker]),
                IngressOutcome::Accepted
            );
        }
        assert_eq!(endpoint.deliver_ingress(&[64]), IngressOutcome::Full);
        {
            let ingress = endpoint.lock_ingress();
            assert_eq!(ingress.len(), INGRESS_CAPACITY);
            assert_eq!(ingress.front().map(Vec::as_slice), Some(&[0][..]));
            assert_eq!(ingress.back().map(Vec::as_slice), Some(&[63][..]));
            assert!(!ingress.iter().any(|frame| frame.as_slice() == [64]));
        }
        assert_eq!(endpoint.ingress_full_drops.load(Ordering::Relaxed), 1);
        assert_eq!(wake_target.0.load(Ordering::Relaxed), INGRESS_CAPACITY);

        endpoint.deactivate();
        assert!(endpoint.lock_ingress().is_empty());
        assert_eq!(endpoint.deliver_ingress(&[65]), IngressOutcome::Inactive);
        assert!(endpoint.lock_ingress().is_empty());
        assert_eq!(
            endpoint.requeue_ingress(vec![66]),
            IngressOutcome::Inactive
        );
        assert!(endpoint.lock_ingress().is_empty());
        assert_eq!(wake_target.0.load(Ordering::Relaxed), INGRESS_CAPACITY);
        assert_eq!(
            endpoint.ingress_inactive_drops.load(Ordering::Relaxed),
            2
        );
    }

    #[test]
    fn requeue_does_not_restore_frame_after_teardown() {
        let switch = VirtualSwitch::new();
        let wake_target = Arc::new(CountingWakeTarget(AtomicUsize::new(0)));
        let endpoint = PortEndpoint::new(
            SwitchPortId::new(3, 4, 0),
            [0x02, 0, 0, 0, 0, 3],
            switch,
            wake_target,
        );
        endpoint.activate();
        assert_eq!(endpoint.deliver_ingress(&[7]), IngressOutcome::Accepted);
        let frame = endpoint.pop_ingress().expect("test frame must be queued");

        endpoint.deactivate();
        assert_eq!(
            endpoint.requeue_ingress(frame),
            IngressOutcome::Inactive
        );
        assert!(endpoint.lock_ingress().is_empty());
        assert_eq!(
            endpoint.ingress_inactive_drops.load(Ordering::Relaxed),
            1
        );
    }
}
