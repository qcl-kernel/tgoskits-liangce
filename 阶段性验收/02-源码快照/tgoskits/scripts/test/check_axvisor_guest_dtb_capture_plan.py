#!/usr/bin/env python3
"""Behavioral contract for per-VM final Guest DTB capture planning."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PLANNER = WORKSPACE_ROOT / "scripts/contest/plan_guest_dtb_capture.py"
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expect_rejected(
    errors: list[str],
    planner: ModuleType,
    text: str,
    *,
    label: str,
    expected_message: str,
) -> None:
    try:
        planner.parse_guest_dtb_markers(text, expected_vm_ids={1, 2})
    except planner.GuestDtbMarkerError as error:
        if expected_message not in str(error):
            errors.append(f"planner reports the wrong {label} error: {error}")
    else:
        errors.append(f"planner accepts {label}")


def main() -> int:
    errors: list[str] = []

    if not PLANNER.is_file():
        errors.append("final Guest DTB capture planner is missing")
    else:
        try:
            planner = load_module("guest_dtb_capture_planner", PLANNER)
        except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
            errors.append(f"final Guest DTB capture planner cannot be imported: {error}")
        else:
            for name in (
                "GuestDtbMarkerError",
                "parse_guest_dtb_markers",
                "validate_capture_ranges",
                "build_capture_plan",
            ):
                if not hasattr(planner, name):
                    errors.append(f"capture planner does not expose {name}")

            if not errors:
                valid_log = "\n".join(
                    (
                        "[  1.000] AxVisor booting",
                        "AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=8192 "
                        "hpa_segments=0x90000000:4096,0x91000000:4096",
                        "guest output that is not evidence metadata",
                        "AXVISOR_GUEST_DTB_READY vm=2 gpa=0x47e00000 size=4096 "
                        "hpa_segments=0x92000000:4096",
                    )
                )
                try:
                    markers = planner.parse_guest_dtb_markers(
                        valid_log, expected_vm_ids={1, 2}
                    )
                    planner.validate_capture_ranges(markers)
                    plan = planner.build_capture_plan(
                        markers=markers,
                        source_log_name="axvisor.log",
                        source_log_sha256="a" * 64,
                    )
                except Exception as error:  # noqa: BLE001 - preserve diagnosis.
                    errors.append(f"capture planner rejects valid markers: {error}")
                else:
                    if [entry["vmId"] for entry in plan.get("guests", [])] != [1, 2]:
                        errors.append("capture plan does not sort guests by VM id")
                    if plan.get("status") != "capture_planned":
                        errors.append("capture plan does not expose capture_planned status")
                    if plan.get("proofScope") != "final-guest-dtb-physical-capture-plan":
                        errors.append("capture plan overstates or changes its proof scope")
                    does_not_prove = set(plan.get("doesNotProve", []))
                    for boundary in (
                        "physical bytes were captured",
                        "both guests booted",
                        "passthrough DMA is isolated",
                        "Linux and Zephyr have IP connectivity",
                    ):
                        if boundary not in does_not_prove:
                            errors.append(
                                f"capture plan omits the proof boundary `{boundary}`"
                            )
                    vm1 = plan["guests"][0]
                    if vm1.get("gpa") != "0x8fe00000" or vm1.get("size") != 8192:
                        errors.append("capture plan changes VM 1 GPA or byte length")
                    segments = vm1.get("segments", [])
                    if [segment.get("fileName") for segment in segments] != [
                        "guest-vm-1.segment-000.bin",
                        "guest-vm-1.segment-001.bin",
                    ]:
                        errors.append("capture plan segment filenames are not deterministic")
                    if any("command" in segment for segment in segments):
                        errors.append("capture plan embeds shell/HMP command strings")

                duplicate = "\n".join(
                    (
                        valid_log,
                        "AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=4096 "
                        "hpa_segments=0x93000000:4096",
                    )
                )
                expect_rejected(
                    errors,
                    planner,
                    duplicate,
                    label="duplicate VM marker",
                    expected_message="duplicate marker for VM 1",
                )
                expect_rejected(
                    errors,
                    planner,
                    valid_log.splitlines()[1] + "\n",
                    label="missing expected VM marker",
                    expected_message="missing markers for VM ids: 2",
                )
                expect_rejected(
                    errors,
                    planner,
                    valid_log.replace(
                        "AXVISOR_GUEST_DTB_READY", "\x1b[mAXVISOR_GUEST_DTB_READY", 1
                    ),
                    label="ANSI-prefixed marker",
                    expected_message="malformed Guest DTB marker",
                )
                expect_rejected(
                    errors,
                    planner,
                    valid_log
                    + "\nAXVISOR_GUEST_DTB_READY vm=3 gpa=0x48000000 size=4096 "
                    "hpa_segments=0x94000000:4096\n",
                    label="unexpected VM marker",
                    expected_message="unexpected marker for VM 3",
                )
                expect_rejected(
                    errors,
                    planner,
                    valid_log
                    + "\nAXVISOR_GUEST_DTB_READY vm=x gpa=0x1 size=1 hpa_segments=x\n",
                    label="malformed marker",
                    expected_message="malformed Guest DTB marker",
                )

                invalid_cases = (
                    (
                        "segment length mismatch",
                        "AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=8192 "
                        "hpa_segments=0x90000000:4096\n"
                        "AXVISOR_GUEST_DTB_READY vm=2 gpa=0x47e00000 size=4096 "
                        "hpa_segments=0x92000000:4096\n",
                        "segment lengths total 4096, expected 8192",
                    ),
                    (
                        "overlapping HPA ranges",
                        "AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=8192 "
                        "hpa_segments=0x90000000:8192\n"
                        "AXVISOR_GUEST_DTB_READY vm=2 gpa=0x47e00000 size=4096 "
                        "hpa_segments=0x90001000:4096\n",
                        "physical capture ranges overlap",
                    ),
                    (
                        "zero-length DTB",
                        "AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=0 "
                        "hpa_segments=0x90000000:0\n"
                        "AXVISOR_GUEST_DTB_READY vm=2 gpa=0x47e00000 size=4096 "
                        "hpa_segments=0x92000000:4096\n",
                        "DTB size must be positive",
                    ),
                    (
                        "unbounded DTB capture",
                        "AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=16777217 "
                        "hpa_segments=0x90000000:16777217\n"
                        "AXVISOR_GUEST_DTB_READY vm=2 gpa=0x47e00000 size=4096 "
                        "hpa_segments=0x92000000:4096\n",
                        "exceeds capture limit",
                    ),
                    (
                        "64-bit HPA overflow",
                        "AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=4096 "
                        "hpa_segments=0xfffffffffffff800:4096\n"
                        "AXVISOR_GUEST_DTB_READY vm=2 gpa=0x47e00000 size=4096 "
                        "hpa_segments=0x92000000:4096\n",
                        "overflows 64-bit address space",
                    ),
                )
                for label, text, expected_message in invalid_cases:
                    try:
                        markers = planner.parse_guest_dtb_markers(
                            text, expected_vm_ids={1, 2}
                        )
                        planner.validate_capture_ranges(markers)
                    except planner.GuestDtbMarkerError as error:
                        if expected_message not in str(error):
                            errors.append(
                                f"planner reports the wrong {label} error: {error}"
                            )
                    else:
                        errors.append(f"planner accepts {label}")

    ci = CI.read_text(encoding="utf-8")
    expected_ci = "python3 scripts/test/check_axvisor_guest_dtb_capture_plan.py"
    if expected_ci not in ci:
        errors.append("final Guest DTB capture contract is not wired into CI")

    if not errors:
        return 0

    print("AxVisor final Guest DTB capture-plan contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
