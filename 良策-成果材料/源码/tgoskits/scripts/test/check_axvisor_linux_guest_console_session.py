#!/usr/bin/env python3
"""Behavioral contract for the linux-smp2-v1 console-gate session validator."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/contest"))
import validate_linux_guest_console_session as validator  # noqa: E402


BOOT_ID = "console-20260813T000000Z-01234567"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


VM_CONFIG = """\
[base]
id = 1
name = "linux-qemu-console-gate"
vm_type = 1
cpu_num = 2
phys_cpu_ids = [0, 1]

[kernel]
entry_point = 0x8020_0000
image_location = "memory"
kernel_path = "/run/locked/guest.Image"
kernel_load_addr = 0x8020_0000
dtb_load_addr = 0x8000_0000

memory_regions = [
  [0x80000000, 0x10000000, 0x7, 1],
]

[devices]
interrupt_mode = "passthrough"

passthrough_devices = [
  ["/chosen"],
  ["/psci"],
  ["/timer"],
  ["/intc@8000000"],
  ["/virtio_mmio@a000000"],
]

passthrough_addresses = []

excluded_devices = [
  ["/intc@8000000/its@8080000"],
  ["/pl011@9000000"],
  ["/virtio_mmio@a000400"],
]

emu_devices = [
  ["gppt-gicd", 0x0800_0000, 0x1_0000, 0, 0x21, []],
  ["gppt-gicr", 0x080a_0000, 0x2_0000, 0, 0x20, [2, 0x2_0000, 0]],
  ["linux", 0x0900_0000, 0x1000, 0, 0x2, []],
]
"""

CONSOLE_LOG_TEXT = (
    f"AXVISOR_LINUX_INIT_ENTER vm=1 boot_id={BOOT_ID}\n"
    "early kernel output that is not a marker\n"
    f"AXVISOR_LINUX_DEV_CONSOLE_READY vm=1 boot_id={BOOT_ID} device=/dev/console cpus=2\n"
    f"AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id={BOOT_ID}\n"
)


def _make_fixture(directory: Path) -> dict[str, Path]:
    console_log = directory / "guest-vm-1.console.log"
    console_log.write_text(CONSOLE_LOG_TEXT, encoding="utf-8")
    log_sha = _sha(console_log.read_bytes())
    guest = {
        "vmId": 1,
        "name": "linux",
        "path": "guest-vm-1.console.log",
        "sha256": log_sha,
        "bytes": console_log.stat().st_size,
        "records": 12,
        "firstGeneration": 0,
        "lastGeneration": 0,
        "droppedBytes": 0,
        "dmaEnableAttempts": 0,
        "generations": [],
    }
    console_manifest = directory / "console-manifest.json"
    console_manifest.write_bytes(
        _json_bytes(
            {
                "schemaVersion": 1,
                "artifactStatus": "host-log-derived-unreviewed",
                "status": "console_frames_demuxed",
                "frameCount": 12,
                "guests": [guest],
            }
        )
    )
    capture_status = directory / "status.json"
    capture_status.write_bytes(
        _json_bytes(
            {
                "schemaVersion": 1,
                "status": "linux_guest_console_completed",
                "success": True,
                "state": {
                    "guestConsole": {"bootId": BOOT_ID, "vmId": 1, "name": "linux", "markers": []},
                    "guestConsoleDemux": {"manifest": {"path": "console-manifest.json", "sha256": "0" * 64}, "frameCount": 12, "guestVmIds": [1]},
                },
            }
        )
    )
    capture_dir = directory / "live-capture"
    capture_dir.mkdir()
    (capture_dir / "capture-chain.json").write_bytes(
        _json_bytes({"status": "guest_dtb_capture_completed", "guests": [{"vmId": 1}]})
    )
    (capture_dir / "guest-vm-1.final.dtb").write_bytes(b"\xd0\x0d\xfe\xed" + b"\x00" * 100)
    (capture_dir / "guest-vm-1.final.dts").write_text("/dts-v1/;\n", encoding="utf-8")
    (capture_dir / "guest-vm-1.semantic.json").write_bytes(
        _json_bytes({"status": "guest_dtb_semantic_valid"})
    )
    vm_config = directory / "linux.single-console.toml"
    vm_config.write_text(VM_CONFIG, encoding="utf-8")
    rootfs_plan = directory / "linux-rootfs-plan.json"
    rootfs_plan.write_bytes(
        _json_bytes(
            {
                "schemaVersion": 1,
                "status": "dual_guest_linux_rootfs_prepared",
                "bootId": BOOT_ID,
                "init": {"guestPath": "/init", "size": 100, "sha256": "0" * 64, "mode": "0755"},
            }
        )
    )
    return {
        "capture_status_path": capture_status,
        "capture_chain_path": capture_dir / "capture-chain.json",
        "guest_dtb_path": capture_dir / "guest-vm-1.final.dtb",
        "guest_dts_path": capture_dir / "guest-vm-1.final.dts",
        "guest_semantic_path": capture_dir / "guest-vm-1.semantic.json",
        "vm_config_path": vm_config,
        "rootfs_plan_path": rootfs_plan,
        "console_manifest_path": console_manifest,
        "console_log_path": console_log,
    }


def _run_validator(errors: list[str], fixture: dict[str, Path], *, label: str, expect_ok: bool) -> None:
    try:
        result = validator.validate_console_session(**fixture)
    except (validator.ConsoleSessionError, OSError, UnicodeError, json.JSONDecodeError) as error:
        if expect_ok:
            errors.append(f"{label} should have passed: {error}")
    else:
        if not expect_ok:
            errors.append(f"{label} should have failed")
        elif result.get("status") != "linux_guest_console_observed":
            errors.append(f"{label} has the wrong result status")


def main() -> int:
    errors: list[str] = []
    results = ROOT / "results"
    made_results = not results.exists()
    results.mkdir(exist_ok=True)
    directory = results / f".linux-console-session-contract-{os.getpid()}-{secrets.token_hex(8)}"
    directory.mkdir()
    try:
        fixture = _make_fixture(directory)
        _run_validator(errors, fixture, label="valid fixture", expect_ok=True)

        linked_log = directory / "console-log-link"
        try:
            linked_log.symlink_to(fixture["console_log_path"].name)
        except (OSError, NotImplementedError):
            # Windows developer hosts may not grant symlink creation to the
            # current token; Linux CI still exercises this fail-closed case.
            pass
        else:
            linked_fixture = dict(fixture)
            linked_fixture["console_log_path"] = linked_log
            _run_validator(
                errors,
                linked_fixture,
                label="symlinked console log",
                expect_ok=False,
            )

        wrong_boot = directory / "wrong-boot"
        wrong_boot.mkdir()
        bad = _make_fixture(wrong_boot)
        bad["console_log_path"].write_text(
            CONSOLE_LOG_TEXT.replace(BOOT_ID, "console-other-00000000-00000000"), encoding="utf-8"
        )
        bad["console_manifest_path"].write_bytes(
            _json_bytes(
                {
                    "schemaVersion": 1,
                    "status": "console_frames_demuxed",
                    "frameCount": 12,
                    "guests": [{
                        "vmId": 1, "name": "linux", "path": "guest-vm-1.console.log",
                        "sha256": _sha(bad["console_log_path"].read_bytes()),
                        "bytes": bad["console_log_path"].stat().st_size, "records": 12,
                        "droppedBytes": 0, "dmaEnableAttempts": 0,
                    }],
                }
            )
        )
        _run_validator(errors, bad, label="marker boot-id mismatch", expect_ok=False)

        dup_marker = directory / "dup-marker"
        dup_marker.mkdir()
        bad = _make_fixture(dup_marker)
        bad["console_log_path"].write_text(
            CONSOLE_LOG_TEXT + f"AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id={BOOT_ID}\n",
            encoding="utf-8",
        )
        bad["console_manifest_path"].write_bytes(
            _json_bytes(
                {
                    "schemaVersion": 1,
                    "status": "console_frames_demuxed",
                    "frameCount": 13,
                    "guests": [{
                        "vmId": 1, "name": "linux", "path": "guest-vm-1.console.log",
                        "sha256": _sha(bad["console_log_path"].read_bytes()),
                        "bytes": bad["console_log_path"].stat().st_size, "records": 13,
                        "droppedBytes": 0, "dmaEnableAttempts": 0,
                    }],
                }
            )
        )
        _run_validator(errors, bad, label="repeated READY marker", expect_ok=False)

        fail_marker = directory / "fail-marker"
        fail_marker.mkdir()
        bad = _make_fixture(fail_marker)
        bad["console_log_path"].write_text(
            f"AXVISOR_LINUX_CONSOLE_FAIL vm=1 boot_id={BOOT_ID} reason=proc-mount\n", encoding="utf-8"
        )
        bad["console_manifest_path"].write_bytes(
            _json_bytes(
                {
                    "schemaVersion": 1,
                    "status": "console_frames_demuxed",
                    "frameCount": 1,
                    "guests": [{
                        "vmId": 1, "name": "linux", "path": "guest-vm-1.console.log",
                        "sha256": _sha(bad["console_log_path"].read_bytes()),
                        "bytes": bad["console_log_path"].stat().st_size, "records": 1,
                        "droppedBytes": 0, "dmaEnableAttempts": 0,
                    }],
                }
            )
        )
        _run_validator(errors, bad, label="console FAIL marker", expect_ok=False)

        nic_vm = directory / "nic-vm"
        nic_vm.mkdir()
        bad = _make_fixture(nic_vm)
        bad["vm_config_path"].write_text(
            VM_CONFIG.replace('["/virtio_mmio@a000000"]', '["/virtio_mmio@a000000"],\n  ["/virtio_mmio@a000200"]'),
            encoding="utf-8",
        )
        _run_validator(errors, bad, label="resolved VM config keeps outer NIC slot", expect_ok=False)

        bad_plan = directory / "bad-plan"
        bad_plan.mkdir()
        bad = _make_fixture(bad_plan)
        plan = json.loads(bad["rootfs_plan_path"].read_text(encoding="utf-8"))
        plan["bootId"] = "console-other-00000000-00000000"
        bad["rootfs_plan_path"].write_bytes(_json_bytes(plan))
        _run_validator(errors, bad, label="rootfs plan boot-id mismatch", expect_ok=False)

        bad_status = directory / "bad-status"
        bad_status.mkdir()
        bad = _make_fixture(bad_status)
        status = json.loads(bad["capture_status_path"].read_text(encoding="utf-8"))
        status["status"] = "linux_guest_console_failed"
        bad["capture_status_path"].write_bytes(_json_bytes(status))
        _run_validator(errors, bad, label="failed capture status", expect_ok=False)
    finally:
        shutil.rmtree(directory)
        if made_results:
            results.rmdir()
    if errors:
        print("Linux console-gate session contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Linux console-gate session contract passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
