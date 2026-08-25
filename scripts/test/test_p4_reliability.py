#!/usr/bin/env python3
"""Host-only P4-REL-01 plan and fail-closed tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "contest" / "reliability"))

import run_reliability  # noqa: E402


class P4ReliabilityTests(unittest.TestCase):
    def test_full_plan_freezes_all_required_subplans(self) -> None:
        plan = run_reliability.build_plan(
            run_id="p4-rel-host", mode="full", seed=7
        )
        self.assertEqual(
            [item["mode"] for item in plan["subplans"]],
            ["core", "fault", "restart", "exactly-once"],
        )
        self.assertEqual(plan["subplans"][0]["messages_per_direction"], 10_000)
        self.assertEqual(plan["subplans"][0]["udp"]["port"], 46000)
        self.assertEqual(plan["subplans"][0]["udp"]["payload_bytes"], 256)
        self.assertEqual(plan["subplans"][0]["udp"]["packets_per_second"], 100)
        self.assertEqual(plan["subplans"][2]["tcp"]["framing"], "u16_be_frame_length_plus_icpc_packet")
        self.assertEqual(plan["subplans"][2]["tcp"]["frame_length_bytes"], [36, 1060])
        exactly_once = plan["subplans"][-1]["exactly_once"]
        self.assertEqual(exactly_once["attempt_schedule_ms"], [0, 100, 300])
        self.assertEqual(exactly_once["cancel_at_ms"], 500)
        self.assertEqual(exactly_once["forbidden_retry_at_ms"], 700)
        self.assertEqual(plan["subplans"][-1]["control"]["rate_hz"], 10)
        self.assertEqual(plan["subplans"][-1]["control"]["messages"], 1000)
        self.assertFalse(plan["qualified"])

    def test_wrong_identity_and_runtime_execution_fail_closed(self) -> None:
        with self.assertRaises(run_reliability.ReliabilityContractError):
            run_reliability.build_plan(run_id="../escape", mode="core", seed=7)
        with self.assertRaises(run_reliability.ReliabilityContractError):
            run_reliability.build_plan(run_id="p4-rel", mode="core", seed=99)
        self.assertEqual(
            run_reliability.main(["--run-id", "p4-rel", "--mode", "core"]),
            1,
        )

    def test_dry_run_package_is_status_last_and_not_qualified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p4-rel-") as name:
            output = Path(name) / "run"
            self.assertEqual(
                run_reliability.main(
                    [
                        "--run-id",
                        "p4-rel-host",
                        "--mode",
                        "exactly-once",
                        "--seed",
                        "43",
                        "--dry-run",
                        "--output-dir",
                        str(output),
                    ]
                ),
                0,
            )
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["success"])
            self.assertFalse(status["qualified"])
            self.assertTrue(status["statusLast"])
            self.assertEqual(status["manifestSha256"], run_reliability._sha256(output / "manifest.json"))
            self.assertNotIn("status.json", json.loads((output / "manifest.json").read_text())["files"])
            validation = run_reliability.validate_plan_bundle(
                output, run_id="p4-rel-host", mode="exactly-once", seed=43
            )
            self.assertTrue(validation["valid"])
            (output / "unindexed-empty-directory").mkdir()
            with self.assertRaises(run_reliability.ReliabilityContractError):
                run_reliability.validate_plan_bundle(
                    output, run_id="p4-rel-host", mode="exactly-once", seed=43
                )
            (output / "unindexed-empty-directory").rmdir()
            (output / "unindexed-artifact.bin").write_bytes(b"drift")
            with self.assertRaises(run_reliability.ReliabilityContractError):
                run_reliability.validate_plan_bundle(
                    output, run_id="p4-rel-host", mode="exactly-once", seed=43
                )

    def test_fault_subplans_publish_canonical_per_packet_manifests(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p4-rel-fault-") as name:
            output = Path(name) / "run"
            self.assertEqual(
                run_reliability.main(
                    [
                        "--run-id",
                        "p4-rel-fault",
                        "--mode",
                        "fault",
                        "--seed",
                        "7",
                        "--dry-run",
                        "--output-dir",
                        str(output),
                    ]
                ),
                0,
            )
            manifest = json.loads(
                (output / "fault-manifest-fault.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["packet_count"], 10_000)
            self.assertEqual(manifest["profile_id"], "p4-rel-fault-seed-7")
            self.assertEqual(len(manifest["entries"]), 10_000)
            self.assertEqual(
                run_reliability.validate_plan_bundle(
                    output, run_id="p4-rel-fault", mode="fault", seed=7
                )["qualified"],
                False,
            )
            manifest["entries"][9]["actions"] = []
            (output / "fault-manifest-fault.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(run_reliability.ReliabilityContractError):
                run_reliability.validate_plan_bundle(
                    output, run_id="p4-rel-fault", mode="fault", seed=7
                )

    def test_runtime_preflight_binds_prepared_inputs_without_starting_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p4-rel-preflight-") as name:
            root = Path(name)
            source = {"sourceRun": "p2-r31", "bindingSha256": "a" * 64}
            files = {
                "build": root / "build.toml",
                "qemu": root / "qemu.toml",
                "linux_vm": root / "linux.toml",
                "zephyr_vm": root / "zephyr.toml",
                "rootfs": root / "linux.ext4",
                "image": root / "zephyr.bin",
            }
            for label, path in files.items():
                path.write_bytes(label.encode("ascii"))

            def claim(path: Path) -> dict[str, object]:
                return {
                    "path": path.name,
                    "size": path.stat().st_size,
                    "sha256": run_reliability._sha256(path),
                }

            linux_manifest = root / "linux-manifest.json"
            linux_manifest.write_text(
                json.dumps(
                    {
                        "bootId": "p4-rel-preflight",
                        "mode": "udp-echo",
                        "sourceSoak": source,
                        "outputRootfs": claim(files["rootfs"]),
                    }
                ),
                encoding="utf-8",
            )
            zephyr_manifest = root / "zephyr-manifest.json"
            zephyr_manifest.write_text(
                json.dumps(
                    {
                        "bootId": "p4-rel-preflight",
                        "mode": "udp-echo",
                        "sourceSoak": source,
                        "artifacts": {"bin": claim(files["image"])},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(run_reliability, "validate_soak_binding", return_value=source), patch.object(
                run_reliability,
                "validate_current_qemu_config",
                return_value=claim(files["qemu"]),
            ):
                preflight = run_reliability.build_runtime_preflight(
                    from_soak_session=root / "soak",
                    build_config=files["build"],
                    qemu_config=files["qemu"],
                    linux_vmconfig=files["linux_vm"],
                    zephyr_vmconfig=files["zephyr_vm"],
                    linux_rootfs=files["rootfs"],
                    linux_manifest=linux_manifest,
                    zephyr_image=files["image"],
                    zephyr_build_manifest=zephyr_manifest,
                    mode="fault",
                    run_id="p4-rel-preflight",
                    seed=19,
                    output_dir=root / "runtime",
                )
                self.assertFalse(preflight["qualified"])
                self.assertEqual(preflight["fault"]["packet_count"], 10_000)
                self.assertEqual(
                    run_reliability.validate_runtime_preflight(preflight)["valid"], True
                )
                preflight["inputs"]["linux_rootfs"]["sha256"] = "0" * 64
                with self.assertRaises(run_reliability.ReliabilityContractError):
                    run_reliability.validate_runtime_preflight(preflight)


if __name__ == "__main__":
    unittest.main()
