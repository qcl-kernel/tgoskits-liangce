#!/usr/bin/env python3
"""P4-UPSYNC-02 source contract for the forward-ported os/axvisor/src/virtio_net.rs.

This is a deterministic host check (L1 static). It verifies that the contest
deltas were re-applied on top of the official #2092 device API and that no
pre-#2092 / pre-upsync pattern was accidentally carried back:

  - MMIO + wired IRQ are planned through the resolved graph (`ResourceRequest::Auto`),
    not the fixed `0x0a00_0000` / `ControllerInputId::new(48)` of the official base.
  - The Device trait is implemented with the split `read`/`write` + `DeviceContext`
    API (`dyn DeviceContext`), not the removed `access(&self, &BusAccess, &mut dyn DeviceAccess)`
    returning `BusResponse`.
  - IRQs are level-triggered (`InterruptTrigger::LevelTriggered`) and driven with
    `IrqLine::assert`/`deassert` (never an edge-only `pulse` for RX delivery).
  - The optional `EgressOutcome` from the bounded internal vnet0 switch is
    observed and logged (drop diagnostics).
  - The file must be importable/free of ambiguity (each forbidden token absent).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET = REPO_ROOT / "os" / "axvisor" / "src" / "virtio_net.rs"

TOKEN = "AXVISOR_VIRTIO_NET_UPSYNC_PASS"

# (description, regex) must match exactly once.
REQUIRED = [
    ("MMIO planned via resolved graph (Auto)", r"ResourceRequest::Auto"),
    ("wired IRQ is level-triggered", r"InterruptTrigger::LevelTriggered"),
    (
        "wired IRQ planned via resolved graph (Auto)",
        r"""\.with_wired_irq\(\s*ResourceSlot::new\(IRQ_SLOT\)\?,.*?InterruptTrigger::LevelTriggered,.*?ResourceRequest::Auto""",
    ),
    ("DeviceContext-typed ScopedDeviceMemory", r"context:\s*&\'a mut dyn DeviceContext"),
    ("split read() method", r"fn read\(\s*&\s*self\s*,.*&DeviceAccess,.*&mut dyn DeviceContext,?\s*\)\s*->\s*Result<u64, DeviceError>"),
    ("split write() method", r"fn write\(\s*&\s*self\s*,.*&DeviceAccess,.*context:\s*&\s*mut dyn DeviceContext,?\s*\)\s*->\s*Result<\(\), DeviceError>"),
    ("level assert() during RX delivery", r"self\.irq\s*\n?\s*\.assert\(\)"),
    ("deassert() on non-pending path", r"self\.irq\s*\n?\s*\.deassert\(\)"),
    ("EgressOutcome observed", r"EgressOutcome::Dropped"),
    ("RX delivered counter", r"delivered\s*\+= *1"),
]

# (description, regex) must NOT appear anywhere.
FORBIDDEN = [
    ("removed access() device entry point", r"fn access\(\s*&\s*self\s*,.*BusAccess"),
    ("removed BusResponse", r"\bBusResponse\b"),
    ("removed BusAccess type", r"\bBusAccess\b"),
    ("fixed MMIO 0x0a00_0000", r"ResourceRequest::Fixed\(0x0a00_0000\)"),
    ("fixed INTID 48", r"ControllerInputId::new\(48\)"),
    ("edge-only RX pulse", r"self\.irq\s*\n?\s*\.pulse\(\)"),
    ("VM-wide DeviceAccess guest-memory entry", r"ScopedDeviceMemory\s*\{\s*access,"),
]


def main() -> int:
    if not TARGET.is_file():
        print(f"FAIL: missing {TARGET.relative_to(REPO_ROOT)}")
        return 1
    text = TARGET.read_text(encoding="utf-8")
    errors: list[str] = []

    for description, pattern in REQUIRED:
        if not re.search(pattern, text, flags=re.DOTALL):
            errors.append(f"missing: {description}")

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
