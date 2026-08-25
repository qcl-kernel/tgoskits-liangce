#!/usr/bin/env python3
"""Static tests for the P4 adapter's shared dual-Guest contract."""

from __future__ import annotations

import sys
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "contest"))

from runtime.dual_guest import (  # noqa: E402
    RuntimeContractError,
    build_axvisor_qemu_command,
    inject_qemu_identity_args,
    request_bounded_shutdown,
    validate_dual_guest_log,
)
from network import run_guest_network  # noqa: E402


class P4SharedRuntimeAdapterTests(unittest.TestCase):
    def test_identity_injection_and_launcher_are_shared(self) -> None:
        source = (ROOT / "configs" / "contest" / "qemu-aarch64-linux-zephyr-dual.toml").read_text(
            encoding="utf-8"
        )
        resolved = inject_qemu_identity_args(
            source,
            qmp_socket=Path("/tmp/p4-qmp.sock"),
            qemu_pidfile=Path("/tmp/p4-qemu.pid"),
            qemu_name="axvisor-dual-smoke-p4-test",
        )
        self.assertIn('"-qmp"', resolved)
        self.assertIn('"-pidfile"', resolved)
        self.assertIn('"-name"', resolved)
        self.assertEqual(
            build_axvisor_qemu_command(
                build_config=Path("build.toml"),
                qemu_config=Path("qemu.toml"),
                linux_vmconfig=Path("linux.toml"),
                zephyr_vmconfig=Path("zephyr.toml"),
            )[-4:],
            ["--vmconfigs", "linux.toml", "--vmconfigs", "zephyr.toml"],
        )

    def test_v2_log_requires_official_attribution_and_dtb(self) -> None:
        log = "\n".join(
            [
                "contest dtb evidence: vm=1 gpa=0x80000000 size=0x960 hpa=0x124e00000",
                "contest dtb evidence: vm=2 gpa=0x47e00000 size=0x960 hpa=0x124e01000",
                "[VM 1] AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=p4-test",
                "[VM 2] AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=p4-test",
                "[VM 1] TGOS_LINUX_L3_SMOKE sent=100 received=100 loss=0",
                "[VM 2] TGOS_ZEPHYR_READY",
                "[Axvisor host console dropped 0 queued bytes]",
            ]
        )
        observation = validate_dual_guest_log(log, run_id="p4-test")
        self.assertEqual(sorted(observation.dtb), [1, 2])
        self.assertEqual(sorted(observation.ready), [1, 2])

    def test_legacy_or_unattributed_evidence_fails_closed(self) -> None:
        with self.assertRaises(RuntimeContractError):
            validate_dual_guest_log(
                "AXVISOR_GUEST_DTB_READY vm=1\n",
                run_id="p4-test",
            )

    def test_v2_runtime_requires_fresh_boot_identity(self) -> None:
        profile = json.loads(
            (ROOT / "configs" / "contest" / "network" / "test-011-v2.json").read_text(
                encoding="utf-8"
            )
        )
        log = "\n".join(
            [
                "AXVISOR_DUAL_GUEST_LINUX_READY",
                "AXVISOR_DUAL_GUEST_ZEPHYR_READY",
                "TGOS_LINUX_L3_SMOKE sent=100 received=100 loss=0",
            ]
        )
        with self.assertRaises(run_guest_network.GuestNetworkError):
            run_guest_network.validate_runtime_smoke(
                log, "test-011", profile, expected_boot_id=None
            )

    def test_runner_status_is_status_last(self) -> None:
        source = (ROOT / "scripts" / "contest" / "network" / "run_guest_network.py").read_text(
            encoding="utf-8"
        )
        self.assertTrue(callable(request_bounded_shutdown))
        self.assertIn("request_bounded_shutdown", source)
        self.assertNotIn("os.kill(qemu_pid, 2)", source)
        self.assertNotIn("os.killpg", source)
        self.assertIn('"statusLast": True', source)
        self.assertIn('inputs["resolvedQemuConfig"]', source)
        self.assertIn("residual_files", source)
        with self.assertRaises(RuntimeContractError):
            validate_dual_guest_log(
                "contest dtb evidence: vm=1 gpa=0x80000000 size=0x960 hpa=0x124e00000\n"
                "contest dtb evidence: vm=2 gpa=0x47e00000 size=0x960 hpa=0x124e01000\n"
                "AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=p4-test\n"
                "[VM 2] AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=p4-test\n",
                run_id="p4-test",
            )


if __name__ == "__main__":
    unittest.main()
