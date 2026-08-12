#!/usr/bin/env python3
"""Validate the complete AxVisor/Linux SMP2 startup proof."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


REQUIRED_EVIDENCE = (
    ("host_cpu_nodes", r"Found 4 host CPU nodes"),
    (
        "dynamic_guest_dtb",
        r"VM\[1\] DTB not found, generating from the VM configuration",
    ),
    ("axvisor_vcpu0_spawn", r"Spawning task for VM\[1\] VCpu\[0\]"),
    (
        "axvisor_vcpu0_affinity",
        r"VM\[1\]-VCpu\[0\].*created cpumask:\s*\[0,\s*\]",
    ),
    ("psci_cpu1_boot", r"try to boot target_cpu \[1\]"),
    ("axvisor_vcpu1_spawn", r"Spawning task for VM\[1\] VCpu\[1\]"),
    (
        "axvisor_vcpu1_affinity",
        r"VM\[1\]-VCpu\[1\].*created cpumask:\s*\[1,\s*\]",
    ),
    ("gic_cpu1_redistributor", r"GICv3: CPU1: found redistributor"),
    ("linux_cpu1_boot", r"CPU1: Booted secondary processor"),
    ("linux_smp_summary", r"smp: Brought up 1 node, 2 CPUs"),
    ("linux_total_processors", r"SMP: Total of 2 processors activated\."),
    ("proc_cpuinfo_proof", r"(?m)^linux-smp2-pass\r?$"),
)


MISSING_STATUSES = {
    "proc_cpuinfo_proof": "proc_cpuinfo_proof_missing",
    "host_cpu_nodes": "host_cpu_nodes_missing",
    "dynamic_guest_dtb": "dynamic_guest_dtb_missing",
    "axvisor_vcpu0_spawn": "axvisor_vcpu0_spawn_missing",
    "axvisor_vcpu0_affinity": "axvisor_vcpu0_affinity_missing",
    "psci_cpu1_boot": "psci_cpu1_boot_missing",
    "axvisor_vcpu1_spawn": "axvisor_vcpu1_spawn_missing",
    "axvisor_vcpu1_affinity": "axvisor_vcpu1_affinity_missing",
    "gic_cpu1_redistributor": "gic_cpu1_redistributor_missing",
    "linux_cpu1_boot": "linux_cpu1_boot_missing",
    "linux_smp_summary": "linux_smp_summary_missing",
    "linux_total_processors": "linux_total_processors_missing",
}


def inspect_log(text: str) -> dict[str, bool | int | None]:
    """Return explicit evidence flags without inferring success from exit code."""

    evidence: dict[str, bool | int | None] = {
        key: re.search(pattern, text) is not None
        for key, pattern in REQUIRED_EVIDENCE
    }
    mismatch = re.search(r"(?m)^linux-smp2-cpu-count=([0-9]+)\r?$", text)
    proc_mount_failure = re.search(
        r"(?m)^linux-smp2-proc-mount-failed\r?$", text
    )
    evidence["proc_mount_failed"] = proc_mount_failure is not None
    evidence["cpu_count_mismatch"] = mismatch is not None
    evidence["reported_cpu_count"] = int(mismatch.group(1)) if mismatch else None
    evidence["guest_init_failed"] = re.search(
        r"Failed to initialize guest VM:", text, flags=re.IGNORECASE
    ) is not None
    return evidence


def classify(evidence: dict[str, bool | int | None], qemu_exit: int) -> str:
    """Classify the run with the most actionable failure taking precedence."""

    if evidence["guest_init_failed"]:
        return "guest_init_failed"
    if evidence["proc_mount_failed"]:
        return "guest_proc_mount_failed"
    if evidence["cpu_count_mismatch"]:
        return "guest_cpu_count_mismatch"
    if qemu_exit in (124, 137):
        return "timed_out"
    if qemu_exit != 0:
        return "qemu_failed"

    ordered_keys = (
        "proc_cpuinfo_proof",
        "host_cpu_nodes",
        "dynamic_guest_dtb",
        "axvisor_vcpu0_spawn",
        "axvisor_vcpu0_affinity",
        "psci_cpu1_boot",
        "axvisor_vcpu1_spawn",
        "axvisor_vcpu1_affinity",
        "gic_cpu1_redistributor",
        "linux_cpu1_boot",
        "linux_smp_summary",
        "linux_total_processors",
    )
    for key in ordered_keys:
        if not evidence[key]:
            return MISSING_STATUSES[key]
    return "passed"


def build_payload(
    *, log_path: Path, qemu_exit: int, evidence: dict[str, bool | int | None]
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "guest": "linux-smp2",
        "log": str(log_path),
        "qemuExitCode": qemu_exit,
        "status": classify(evidence, qemu_exit),
        "checks": evidence,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate AxVisor/Linux SMP2 startup evidence"
    )
    parser.add_argument("log", type=Path, help="captured QEMU serial log")
    parser.add_argument("--qemu-exit", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        text = args.log.read_text(encoding="utf-8", errors="replace")
        evidence = inspect_log(text)
        payload = build_payload(
            log_path=args.log, qemu_exit=args.qemu_exit, evidence=evidence
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        print(f"Linux SMP2 log validation failed: {error}", file=sys.stderr)
        return 2

    print(payload["status"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
