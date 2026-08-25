#!/usr/bin/env python3
"""P4-UPSYNC-03 source contract for the c82 AxVM configured virtio-net.

This is a deterministic host check (L1 static). It verifies that the contest
deltas were re-applied on top of official c82 without carrying the former
AxVisor-owned backend or its unverified IRQ assumptions back into production:

  - MMIO + wired IRQ are planned through the resolved graph (`ResourceRequest::Auto`),
    not the fixed `0x0a00_0000` / `ControllerInputId::new(48)` of the official base.
  - The Device trait is implemented with the split `read`/`write` + `DeviceContext`
    API (`dyn DeviceContext`), not the removed `access(&self, &BusAccess, &mut dyn DeviceAccess)`
    returning `BusResponse`.
  - IRQs retain the official c82 `EdgeTriggered` + `IrqLine::pulse` semantics.
  - firmware stays on typed `FdtContributionSpec` / `FdtNodeSpec` resolution.
  - The optional `EgressOutcome` from the bounded internal vnet0 switch is
    observed and logged (drop diagnostics).
  - The file must be importable/free of ambiguity (each forbidden token absent).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET = (
    REPO_ROOT
    / "virtualization"
    / "axvm"
    / "src"
    / "configured"
    / "devices"
    / "virtio_net.rs"
)
LEGACY_TARGET = REPO_ROOT / "os" / "axvisor" / "src" / "virtio_net.rs"
SWITCH_TARGET = REPO_ROOT / "virtualization" / "axvirtio-net" / "src" / "switch.rs"
RUNNER_TARGET = REPO_ROOT / "scripts" / "contest" / "network" / "run_guest_network.py"

TOKEN = "AXVISOR_VIRTIO_NET_UPSYNC_PASS"

# (description, regex) must match exactly once.
REQUIRED = [
    ("MMIO planned via resolved graph (Auto)", r"ResourceRequest::Auto"),
    ("wired IRQ retains official edge trigger", r"InterruptTrigger::EdgeTriggered"),
    (
        "wired IRQ planned via resolved graph (Auto)",
        r"""\.with_wired_irq\(\s*ResourceSlot::new\(IRQ_SLOT\)\?,.*?InterruptTrigger::EdgeTriggered,.*?ResourceRequest::Auto""",
    ),
    ("DeviceContext-typed ScopedDeviceMemory", r"context:\s*&\'a mut dyn DeviceContext"),
    ("split read() method", r"fn read\(\s*&\s*self\s*,.*&DeviceAccess,.*&mut dyn DeviceContext,?\s*\)\s*->\s*Result<u64, DeviceError>"),
    ("split write() method", r"fn write\(\s*&\s*self\s*,.*&DeviceAccess,.*context:\s*&\s*mut dyn DeviceContext,?\s*\)\s*->\s*Result<\(\), DeviceError>"),
    ("edge pulse during RX delivery", r"self\.irq\s*\n?\s*\.pulse\(\)"),
    ("typed FDT contribution", r"FdtContributionSpec::Conventional"),
    ("typed FDT node", r"FdtNodeSpec::new\(\"virtio_mmio\"\)"),
    ("EgressOutcome observed", r"EgressOutcome::Dropped"),
    ("switch identity uses actual VM id", r"SwitchPortId::new\(self\.vm_id,\s*next_port_generation\(self\.vm_id\),\s*0\)"),
    ("runtime teardown deactivates stale endpoint", r"impl Drop for VirtioNetRuntimeDevice.*?self\.endpoint\.deactivate\(\)"),
    (
        "deactivation linearizes lifecycle and clears bounded ingress",
        r"fn deactivate\(&self\).*?let mut ingress = self\.lock_ingress\(\);"
        r".*?self\.active\.store\(false, Ordering::Release\);.*?ingress\.clear\(\)",
    ),
    ("accepted frame evidence carries generation and port", r"virtio-net frame vm=\{\} generation=\{\} port=\{\} dir=\{\}"),
    ("short TX frame is guarded before MAC indexing", r"frame\.get\(\.\.12\)"),
    ("contest MTU bounds the Ethernet frame", r"const MAX_CONTEST_FRAME_SIZE:\s*usize\s*=\s*1514"),
    ("oversized TX is rejected before switch copy", r"frame\.len\(\)\s*>\s*MAX_CONTEST_FRAME_SIZE"),
    ("persistent RX delivered counter", r"rx_delivered\.fetch_add\(1,\s*Ordering::Relaxed\)"),
    ("bounded no-buffer diagnostics", r"rx_no_guest_buffer\s*\.fetch_add\(1,\s*Ordering::Relaxed\)"),
    ("bounded RX error diagnostics", r"rx_errors\.fetch_add\(1,\s*Ordering::Relaxed\)"),
    (
        "requeue lifecycle check shares ingress lock",
        r"fn requeue_ingress\(&self,\s*frame:\s*Vec<u8>\)\s*->\s*IngressOutcome\s*\{"
        r".*?let mut ingress = self\.lock_ingress\(\);"
        r".*?if !self\.is_active\(\)",
    ),
    (
        "inactive requeue is counted and rejected",
        r"fn requeue_ingress\(&self,\s*frame:\s*Vec<u8>\)\s*->\s*IngressOutcome\s*\{"
        r".*?increment_ingress_inactive_drop\(\).*?IngressOutcome::Inactive",
    ),
    (
        "inactive requeue diagnostics use canonical ingress prefix",
        r"virtio-net ingress inactive-drop count=\{count\} vm=\{\} generation=\{\} "
        r"source=requeue",
    ),
    (
        "full requeue diagnostics use canonical ingress prefix",
        r"virtio-net ingress full-drop count=\{count\} vm=\{\} generation=\{\} "
        r"capacity=\{INGRESS_CAPACITY\} source=requeue",
    ),
    (
        "poll path observes requeue rejection",
        r"let requeue = self\.endpoint\.requeue_ingress\(frame\);"
        r".*?requeue != IngressOutcome::Accepted",
    ),
    ("separate full-ingress runtime counter", r"ingress_full_drops\s*\.fetch_add\(1,\s*Ordering::Relaxed\)"),
    ("separate inactive-ingress runtime counter", r"ingress_inactive_drops\s*\.fetch_add\(1,\s*Ordering::Relaxed\)"),
    ("frame evidence checks accepted local delivery", r"if local_deliveries == 0"),
    (
        "accepted frame evidence publishes ingress saturation totals",
        r"virtio-net frame vm=\{\} generation=\{\} port=\{\} dir=\{\} len=\{\} hex=\{\} "
        r"ingress_full_drop=\{\} inactive_target_drop=\{\}",
    ),
]

SWITCH_REQUIRED = [
    ("typed ingress outcome", r"pub enum IngressOutcome"),
    ("full ingress outcome", r"\bFull\b"),
    ("inactive ingress outcome", r"\bInactive\b"),
    ("aggregate full-ingress counter", r"pub ingress_full_drop:\s*AtomicU64"),
    ("aggregate inactive-target counter", r"pub inactive_target_drop:\s*AtomicU64"),
    ("forwarding reports accepted local deliveries", r"Forwarded\s*\{\s*uplink:\s*bool,\s*local_deliveries:\s*usize,?\s*\}"),
]

# (description, regex) must NOT appear anywhere.
FORBIDDEN = [
    ("removed access() device entry point", r"fn access\(\s*&\s*self\s*,.*BusAccess"),
    ("removed BusResponse", r"\bBusResponse\b"),
    ("removed BusAccess type", r"\bBusAccess\b"),
    ("fixed MMIO 0x0a00_0000", r"ResourceRequest::Fixed\(0x0a00_0000\)"),
    ("fixed INTID 48", r"ControllerInputId::new\(48\)"),
    ("unverified level-triggered rollback", r"InterruptTrigger::LevelTriggered"),
    ("unverified level assert", r"self\.irq\s*\n?\s*\.assert\(\)"),
    ("unverified level deassert", r"self\.irq\s*\n?\s*\.deassert\(\)"),
    ("VM-wide DeviceAccess guest-memory entry", r"ScopedDeviceMemory\s*\{\s*access,"),
]


def main() -> int:
    if not TARGET.is_file():
        print(f"FAIL: missing {TARGET.relative_to(REPO_ROOT)}")
        return 1
    text = TARGET.read_text(encoding="utf-8")
    errors: list[str] = []

    if not SWITCH_TARGET.is_file():
        errors.append(f"missing {SWITCH_TARGET.relative_to(REPO_ROOT)}")
        switch_text = ""
    else:
        switch_text = SWITCH_TARGET.read_text(encoding="utf-8")

    if not RUNNER_TARGET.is_file():
        errors.append(f"missing {RUNNER_TARGET.relative_to(REPO_ROOT)}")
        runner_text = ""
    else:
        runner_text = RUNNER_TARGET.read_text(encoding="utf-8")

    if LEGACY_TARGET.exists():
        errors.append("legacy AxVisor-owned virtio_net.rs still exists as a second backend")

    for description, pattern in REQUIRED:
        if not re.search(pattern, text, flags=re.DOTALL):
            errors.append(f"missing: {description}")

    for description, pattern in SWITCH_REQUIRED:
        if not re.search(pattern, switch_text, flags=re.DOTALL):
            errors.append(f"missing: {description}")

    if not re.search(
        r'"cargo",\s*"xtask",\s*"axvisor",\s*"qemu"', runner_text
    ):
        errors.append("missing: runner uses current `cargo xtask axvisor qemu` route")
    if re.search(r'"cargo",\s*"xtask",\s*"qemu"', runner_text):
        errors.append("forbidden: removed top-level `cargo xtask qemu` route")
    if not re.search(r"cwd\s*=\s*repository\s*,", runner_text):
        errors.append("missing: repository-level xtask runs from repository root")
    if re.search(r"cwd\s*=\s*repository\s*/\s*[\"']os/axvisor[\"']", runner_text):
        errors.append("forbidden: repository-level xtask launched from os/axvisor")

    for description, pattern in FORBIDDEN:
        if re.search(pattern, text, flags=re.DOTALL):
            errors.append(f"forbidden: {description}")

    if errors:
        print("AXVISOR_VIRTIO_NET_UPSYNC_FAIL")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"{TOKEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
