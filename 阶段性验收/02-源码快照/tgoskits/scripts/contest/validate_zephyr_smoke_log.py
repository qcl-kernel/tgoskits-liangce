#!/usr/bin/env python3
"""Validate native or AxVisor Zephyr periodic-smoke evidence."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


EXPECTED_SEQUENCES = list(range(1, 11))
MIN_DELTA_MS = 50
MAX_DELTA_MS = 500

AXVISOR_EVIDENCE = (
    (
        "axvisor_vm_created",
        r"VM\[1\]\s+created success(?:fully)?(?:[!,]|\b)",
    ),
    (
        "axvisor_kernel_loaded",
        r"(?:Loading VM\[1\] kernel from|VM\[1\]\s+created success, loading images)",
    ),
    ("axvisor_vcpu0_spawn", r"Spawning task for VM\[1\] VCpu\[0\]"),
    (
        "axvisor_vcpu0_affinity",
        r"VM\[1\]-VCpu\[0\].*created cpumask:\s*\[0,\s*\]",
    ),
    ("axvisor_vm_boot_success", r"VM\[1\] boot success(?:!|\b)"),
)

AXVISOR_MISSING_STATUSES = {
    "axvisor_vm_created": "axvisor_vm_create_missing",
    "axvisor_kernel_loaded": "axvisor_kernel_load_missing",
    "axvisor_vcpu0_spawn": "axvisor_vcpu0_spawn_missing",
    "axvisor_vcpu0_affinity": "axvisor_vcpu0_affinity_missing",
    "axvisor_vm_boot_success": "axvisor_vm_boot_success_missing",
}

UNSAFE_ITS_LPI_EVIDENCE = (
    (
        "unsafe_its_probe",
        r"ITS \[mem 0x[0-9a-f]+-0x[0-9a-f]+\]",
    ),
    (
        "unsafe_lpi_reserved_range_missing",
        r"GICv3: Expected reserved range .*not found",
    ),
    (
        "unsafe_lpis_enabled",
        r"GICv3: CPU[0-9]+: Booted with LPIs enabled, memory probably corrupted",
    ),
)

UNSAFE_ITS_LPI_STATUSES = {
    "unsafe_its_probe": "unsafe_its_probe_detected",
    "unsafe_lpi_reserved_range_missing": "unsafe_lpi_reserved_range_missing",
    "unsafe_lpis_enabled": "unsafe_lpis_enabled",
}

START_PATTERN = re.compile(
    r"(?m)^TGOS_ZEPHYR_SMOKE_START period_ms=100 samples=10\r?$"
)
SAMPLE_PATTERN = re.compile(
    r"(?m)^TGOS_ZEPHYR_SMOKE_SAMPLE "
    r"seq=([0-9]+) uptime_ms=([0-9]+) delta_ms=([0-9]+)\r?$"
)
PASS_PATTERN = re.compile(
    r"(?m)^TGOS_ZEPHYR_SMOKE_PASS samples=10\r?$"
)


def inspect_log(text: str) -> dict[str, Any]:
    """Extract explicit startup and periodic-sampling evidence from ``text``."""

    evidence: dict[str, Any] = {
        key: re.search(pattern, text) is not None
        for key, pattern in AXVISOR_EVIDENCE
    }
    evidence.update(
        {
            key: re.search(pattern, text, flags=re.IGNORECASE) is not None
            for key, pattern in UNSAFE_ITS_LPI_EVIDENCE
        }
    )

    samples = [
        (int(sequence), int(uptime_ms), int(delta_ms))
        for sequence, uptime_ms, delta_ms in SAMPLE_PATTERN.findall(text)
    ]
    sequences = [sample[0] for sample in samples]
    uptimes = [sample[1] for sample in samples]
    deltas = [sample[2] for sample in samples]

    evidence.update(
        {
            "smoke_start": START_PATTERN.search(text) is not None,
            "sample_count": len(samples),
            "sample_sequences": sequences,
            "sample_uptimes_ms": uptimes,
            "sample_deltas_ms": deltas,
            "sample_sequence_valid": sequences == EXPECTED_SEQUENCES,
            "sample_uptime_monotonic": len(uptimes) == len(EXPECTED_SEQUENCES)
            and all(
                current > previous
                for previous, current in zip(uptimes, uptimes[1:])
            ),
            "sample_delta_in_range": len(deltas) == len(EXPECTED_SEQUENCES)
            and all(MIN_DELTA_MS <= delta <= MAX_DELTA_MS for delta in deltas),
            "success_marker": PASS_PATTERN.search(text) is not None,
            "guest_init_failed": re.search(
                r"Failed to initialize guest VM:", text, flags=re.IGNORECASE
            )
            is not None,
        }
    )
    return evidence


def classify(
    evidence: dict[str, Any], qemu_exit: int, scope: str = "axvisor"
) -> str:
    """Return the most actionable status, prioritizing process failures."""

    if scope not in ("native", "axvisor"):
        raise ValueError(f"unsupported validation scope: {scope}")

    if scope == "axvisor":
        for key, _pattern in UNSAFE_ITS_LPI_EVIDENCE:
            if evidence.get(key, False):
                return UNSAFE_ITS_LPI_STATUSES[key]
        if evidence.get("guest_init_failed", False):
            return "guest_init_failed"
    if qemu_exit in (124, 137):
        return "timed_out"
    if qemu_exit != 0:
        return "qemu_failed"

    if scope == "axvisor":
        for key, _pattern in AXVISOR_EVIDENCE:
            if not evidence.get(key, False):
                return AXVISOR_MISSING_STATUSES[key]

    if not evidence.get("smoke_start", False):
        return "smoke_start_missing"
    if not evidence.get("sample_sequence_valid", False):
        return "sample_sequence_invalid"
    if not evidence.get("sample_uptime_monotonic", False):
        return "sample_uptime_not_monotonic"
    if not evidence.get("sample_delta_in_range", False):
        return "sample_delta_out_of_range"
    if not evidence.get("success_marker", False):
        return "success_marker_missing"
    return "passed"


def build_payload(
    *,
    log_path: Path,
    qemu_exit: int,
    scope: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Build the machine-readable evidence document emitted by the CLI."""

    return {
        "schemaVersion": 1,
        "guest": "zephyr-smoke",
        "scope": scope,
        "log": str(log_path),
        "qemuExitCode": qemu_exit,
        "status": classify(evidence, qemu_exit, scope),
        "checks": evidence,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Zephyr periodic-smoke serial evidence"
    )
    parser.add_argument("log", type=Path, help="captured QEMU serial log")
    parser.add_argument("--qemu-exit", type=int, required=True)
    parser.add_argument(
        "--scope",
        choices=("native", "axvisor"),
        default="axvisor",
        help="native checks guest markers; axvisor also checks VM startup evidence",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional path for the same JSON document written to stdout",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        text = args.log.read_text(encoding="utf-8", errors="replace")
        evidence = inspect_log(text)
        payload = build_payload(
            log_path=args.log,
            qemu_exit=args.qemu_exit,
            scope=args.scope,
            evidence=evidence,
        )
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
    except OSError as error:
        print(f"Zephyr smoke log validation failed: {error}", file=sys.stderr)
        return 2

    print(serialized, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
