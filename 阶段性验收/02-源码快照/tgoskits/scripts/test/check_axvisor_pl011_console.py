#!/usr/bin/env python3
"""Static safety contract for the AArch64 TX-only emulated PL011."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PL011 = WORKSPACE_ROOT / "virtualization/axdevice/src/pl011.rs"
PL011_FRAME = WORKSPACE_ROOT / "virtualization/axdevice/src/pl011/frame.rs"
FACTORY = WORKSPACE_ROOT / "virtualization/axdevice/src/factory.rs"
LIB = WORKSPACE_ROOT / "virtualization/axdevice/src/lib.rs"
DEVICE = WORKSPACE_ROOT / "virtualization/axdevice/src/device.rs"
AXDEVICE_CARGO = WORKSPACE_ROOT / "virtualization/axdevice/Cargo.toml"
AXVISOR_CARGO = WORKSPACE_ROOT / "os/axvisor/Cargo.toml"
AXVISOR_MAIN = WORKSPACE_ROOT / "os/axvisor/src/main.rs"
AXVISOR_MANAGER = WORKSPACE_ROOT / "os/axvisor/src/manager.rs"
HOST_DRAIN = WORKSPACE_ROOT / "os/axvisor/src/guest_console.rs"
DESIGN = (
    WORKSPACE_ROOT
    / "docs/docs/development/aarch64-dual-guest-console-isolation.md"
)
LINUX_DUAL = (
    WORKSPACE_ROOT
    / "os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml"
)
ZEPHYR_DUAL = (
    WORKSPACE_ROOT
    / "os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml"
)
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"


def require_fragments(
    text: str, fragments: tuple[str, ...], *, label: str, errors: list[str]
) -> None:
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{label} is missing `{fragment}`")


def check_dual_config(path: Path, *, label: str, errors: list[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
        config = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"{label} cannot be read as TOML: {error}")
        return

    devices = config.get("devices", {})
    if ["/pl011@9000000"] not in devices.get("excluded_devices", []):
        errors.append(f"{label} no longer excludes the host PL011")
    if any(
        isinstance(entry, list) and entry and entry[0] == "console"
        for entry in devices.get("emu_devices", [])
    ):
        errors.append(f"{label} enables emulated console before Phase 1B")
    if "# status: blocked_dma_console" not in text:
        errors.append(f"{label} overstates the dual-guest readiness status")


def main() -> int:
    errors: list[str] = []

    try:
        manifest = tomllib.loads(AXDEVICE_CARGO.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"axdevice manifest cannot be read: {error}")
    else:
        if manifest.get("features", {}).get("host-test") != ["ax-kspin/host-test"]:
            errors.append("axdevice host-test does not make SpinNoIrq safe for host unit tests")

    try:
        pl011 = PL011.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"PL011 implementation cannot be read: {error}")
        pl011 = ""

    if pl011:
        if len(pl011.splitlines()) >= 800:
            errors.append("PL011 implementation reached the mandatory split threshold")
        production = pl011.split("#[cfg(test)]", maxsplit=1)[0]
        require_fragments(
            production,
            (
                "pub struct Pl011TxDevice",
                "resources: [Resource; 1]",
                "Resource::MmioRange",
                "VecDeque::with_capacity(TX_RING_CAPACITY)",
                "if state.tx.len() == TX_RING_CAPACITY",
                "state.tx.push_back(value as u8)",
                "UARTDMACR_ENABLE_BITS",
                "dma_enable_attempts",
                "if config.irq_id != TX_ONLY_IRQ_SENTINEL",
                "if !config.cfg_list.is_empty()",
                "_context: &DeviceBuildContext<'_>",
            ),
            label="PL011 production model",
            errors=errors,
        )
        for forbidden in (
            "Resource::IrqLine",
            ".resolve_irq(",
            "context.resolve_irq",
        ):
            if forbidden in production:
                errors.append(
                    f"PL011 production model acquired forbidden capability `{forbidden}`"
                )
        full_check = production.find("if state.tx.len() == TX_RING_CAPACITY")
        push = production.find("state.tx.push_back(value as u8)")
        if full_check < 0 or push < 0 or full_check > push:
            errors.append("PL011 TX ring does not reject overflow before enqueue")

        require_fragments(
            pl011,
            (
                "data_and_flags_follow_tx_only_polling_contract",
                "primecell_identification_is_stable_and_read_only",
                "configuration_registers_mask_and_read_back_without_irq",
                "dma_enable_is_forced_off_and_observable",
                "invalid_width_bus_offset_and_boundary_are_controlled_errors",
                "reset_restores_registers_ring_and_counters",
                "instances_with_the_same_gpa_do_not_share_state_or_tx",
                "full_ring_drops_without_blocking_and_counts_loss",
                "factory_builds_one_mmio_only_device_without_resolving_irq",
                "factory_rejects_every_unsafe_or_ambiguous_configuration",
                "aarch64_builtin_registry_contains_console_factory",
            ),
            label="PL011 unit-test appendix",
            errors=errors,
        )

    try:
        frame = PL011_FRAME.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"PL011 frame implementation cannot be read: {error}")
        frame = ""
    if frame:
        if len(frame.splitlines()) >= 800:
            errors.append("PL011 frame implementation reached the mandatory split threshold")
        production = frame.split("#[cfg(test)]", maxsplit=1)[0]
        require_fragments(
            production,
            (
                'pub const PL011_TX_FRAME_MARKER: &str = "AXVISOR_GUEST_CONSOLE_FRAME";',
                "pub const PL011_TX_FRAME_VERSION: u8 = 1;",
                "pub const PL011_TX_FRAME_MAX_PAYLOAD: usize = 4096;",
                "pub struct Pl011TxFrame<'device, 'payload>",
                "pub enum Pl011TxFrameError",
                "pub fn drain_tx_frame<'device, 'payload>",
                "state.frame.bind_vm(vm_id)?;",
                ".prepare_frame(payload_len, state.dropped_bytes, state.dma_enable_attempts)?;",
                "state.tx.pop_front()",
                "state.frame.commit_frame(next_sequence, total_bytes);",
                'write!(formatter, "{byte:02x}")?;',
                "bytes.len() > 64",
                "byte.is_ascii_alphanumeric()",
            ),
            label="PL011 frame production API",
            errors=errors,
        )
        prepare = production.find(".prepare_frame(")
        dequeue = production.find("state.tx.pop_front()")
        commit = production.find("state.frame.commit_frame(")
        if min(prepare, dequeue, commit) < 0 or not prepare < dequeue < commit:
            errors.append("PL011 frame does not validate before dequeue and commit in source order")
        for forbidden in ("log::", "println!", "console_print", "Resource::IrqLine"):
            if forbidden in production:
                errors.append(f"PL011 frame API acquired forbidden sink or IRQ `{forbidden}`")
        require_fragments(
            frame,
            (
                "display_matches_the_demultiplexer_field_contract",
                "frames_preserve_ring_order_and_generation_local_totals",
                "reset_advances_only_an_observed_generation",
                "vm_binding_and_frame_counters_are_device_local",
                "invalid_inputs_do_not_consume_or_relabel_the_ring",
                "invalid_name_and_counter_exhaustion_fail_before_dequeue",
                "every_u64_frame_counter_overflow_is_fail_closed",
                "reset_discard_and_dma_attempt_remain_fail_closed_evidence",
                "stream_name_validation_matches_the_python_contract",
            ),
            label="PL011 frame unit-test appendix",
            errors=errors,
        )

    for path, label, fragments in (
        (
            HOST_DRAIN,
            "single-consumer host drain",
            (
                "pub(crate) struct GuestConsoleDrain",
                "pub(crate) fn start(vms: Vec<AxVMRef>)",
                "const FRAME_PAYLOAD_BYTES: usize = 256;",
                "const MAX_FRAMES_PER_DEVICE_PASS: usize = 16;",
                ".is::<axdevice::Pl011TxDevice>()",
                "let mut console_vms = Vec::new();",
                "console_vms.push((protocol_vm_id, vm.clone()));",
                "dedicated_drain_cpu(&vms)",
                "let affinity = affinity?;",
                "effective_affinity != affinity",
                "ax_set_current_affinity(AxCpuMask::one_shot(drain_cpu))",
                "Guest console drain task running on dedicated host CPU",
                "drain_loop(console_vms, task_stop)",
                ".downcast_ref::<axdevice::Pl011TxDevice>()",
                "console.drain_tx_frame(*protocol_vm_id, &mut payload)",
                'ax_std::println!("\\n{frame}");',
                "AxvmRuntime::stop_vm(vm_id)",
                "self.stop.store(true, Ordering::Release);",
                ".join()",
            ),
        ),
        (
            AXVISOR_MAIN,
            "AxVisor Guest-console module boundary",
            (
                '#[cfg(target_arch = "aarch64")]\nmod guest_console;',
                ".start_default_vms()",
                'panic!("failed to run default VMs: {error:#}")',
            ),
        ),
        (
            AXVISOR_MANAGER,
            "AxVisor default-VM console lifecycle",
            (
                "pub fn start_default_vms(&self) -> Result<()>",
                "GuestConsoleDrain::start(Self::vm_list())?;",
                "self.runtime.start_default_vms();",
                "console_drain.finish()?;",
            ),
        ),
    ):
        text = ""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            errors.append(f"{label} cannot be read: {error}")
        if text:
            if path == HOST_DRAIN and len(text.splitlines()) >= 800:
                errors.append("single-consumer host drain reached the split threshold")
            require_fragments(text, fragments, label=label, errors=errors)

    try:
        axvisor_manifest = tomllib.loads(AXVISOR_CARGO.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"AxVisor manifest cannot be read: {error}")
    else:
        target_dependencies = axvisor_manifest.get("target", {}).get(
            "cfg(any(not(any(windows, unix)), target_env = \"musl\"))", {}
        ).get("dependencies", {})
        if target_dependencies.get("axdevice") != {"workspace": True}:
            errors.append("AArch64 AxVisor does not directly depend on the PL011 owner crate")

    for path, label, fragments in (
        (
            FACTORY,
            "factory registry",
            (
                '#[cfg(target_arch = "aarch64")]\nuse crate::pl011::Pl011TxFactory;',
                '#[cfg(target_arch = "aarch64")]\n    registry.register(Arc::new(Pl011TxFactory))?;',
            ),
        ),
        (
            LIB,
            "axdevice module boundary",
            (
                '#[cfg(any(target_arch = "aarch64", test))]\nmod pl011;',
                "PL011_TX_FRAME_MARKER",
                "Pl011TxFrame",
                "Pl011TxFrameError",
                "Pl011TxStatistics",
            ),
        ),
        (
            DEVICE,
            "legacy device fallback",
            (
                "if device_type == EmulatedDeviceType::Console",
                'return !cfg!(target_arch = "aarch64");',
                'operation: "initialize console"',
                "requires a registered target factory",
            ),
        ),
        (
            DESIGN,
            "console isolation design",
            (
                "本设计不改变 `blocked_dma_console` 状态",
                "Phase 1A 单元",
                "console gate 与 IP gate 是两个独立门槛",
            ),
        ),
    ):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            errors.append(f"{label} cannot be read: {error}")
            continue
        require_fragments(text, fragments, label=label, errors=errors)

    check_dual_config(LINUX_DUAL, label="Linux dual-guest config", errors=errors)
    check_dual_config(ZEPHYR_DUAL, label="Zephyr dual-guest config", errors=errors)

    try:
        ci = CI.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"CI workflow cannot be read: {error}")
    else:
        if "python3 scripts/test/check_axvisor_pl011_console.py" not in ci:
            errors.append("PL011 console safety contract is not wired into CI")

    if not errors:
        return 0

    print("AxVisor TX-only PL011 console contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
