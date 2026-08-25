#!/usr/bin/env python3
"""Host-only tests for the P5 C1/C2 entry points."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "scripts" / "contest" / "ai"
sys.path.insert(0, str(AI_DIR))

import prepare_linux_ai_rootfs  # noqa: E402
import run_closed_loop  # noqa: E402
import validate_closed_loop  # noqa: E402


PROFILE = ROOT / "configs" / "contest" / "ai" / "qualification-v1.json"
MODEL_DIR = ROOT / "apps" / "contest" / "linux-ai-controller" / "model"


class P5GuestOrchestrationTests(unittest.TestCase):
    def test_rootfs_plan_binds_profile_and_model_without_mutation(self) -> None:
        plan = prepare_linux_ai_rootfs.build_plan(
            run_id="p5-prep-host",
            source_rootfs=None,
            profile=PROFILE,
            model_directory=MODEL_DIR,
        )
        self.assertEqual(plan["schema_version"], "p5-ai-rootfs-plan-v1")
        self.assertEqual(plan["test_id"], "TEST-017")
        self.assertIsNone(plan["source_rootfs"])
        self.assertEqual(plan["execution"], "not_started")
        self.assertFalse(plan["qualified"])

    def test_guest_paths_fail_closed_without_dry_run(self) -> None:
        self.assertEqual(
            prepare_linux_ai_rootfs.main(
                ["--run-id", "p5-prep-host", "--output-dir", "unused"]
            ),
            1,
        )

    def test_rootfs_plan_rejects_symlink_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-link-") as name:
            link = Path(name) / "profile.json"
            try:
                link.symlink_to(PROFILE)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlink fixture unavailable: {error}")
            with self.assertRaises(prepare_linux_ai_rootfs.RootfsPlanError):
                prepare_linux_ai_rootfs.build_plan(
                    run_id="p5-link-negative",
                    source_rootfs=None,
                    profile=link,
                    model_directory=MODEL_DIR,
                )

    def test_rootfs_plan_rejects_symlink_model_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-model-link-") as name:
            link = Path(name) / "model"
            try:
                link.symlink_to(MODEL_DIR, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"directory symlink fixture unavailable: {error}")
            with self.assertRaises(prepare_linux_ai_rootfs.RootfsPlanError):
                prepare_linux_ai_rootfs.build_plan(
                    run_id="p5-model-link-negative",
                    source_rootfs=None,
                    profile=PROFILE,
                    model_directory=link,
                )

    def test_rootfs_plan_binds_optional_source_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-manifest-") as name:
            root = Path(name)
            source = root / "linux.ext4"
            source.write_bytes(b"immutable-rootfs")
            source_hash = prepare_linux_ai_rootfs._sha256(source)
            manifest = root / "p4-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "inputs": {
                            "linuxRootfs": {
                                "size": source.stat().st_size,
                                "sha256": source_hash,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            plan = prepare_linux_ai_rootfs.build_plan(
                run_id="p5-source-bound",
                source_rootfs=source,
                source_manifest=manifest,
                profile=PROFILE,
                model_directory=MODEL_DIR,
                controller_mode="fixed",
                seed=7,
            )
            self.assertEqual(plan["controller_mode"], "fixed")
            self.assertEqual(plan["seed"], 7)
            self.assertEqual(plan["source_rootfs"]["sha256"], source_hash)
            with self.assertRaises(prepare_linux_ai_rootfs.RootfsPlanError):
                prepare_linux_ai_rootfs.build_plan(
                    run_id="p5-source-unbound",
                    source_rootfs=source,
                    source_manifest=None,
                    profile=PROFILE,
                    model_directory=MODEL_DIR,
                )

    def test_rootfs_materializer_copies_and_dump_verifies_guest_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-materialize-") as name:
            root = Path(name)
            source = root / "linux.ext4"
            source.write_bytes(b"immutable-rootfs")
            source_hash = prepare_linux_ai_rootfs._sha256(source)
            source_manifest = root / "p4-manifest.json"
            source_manifest.write_text(
                json.dumps(
                    {
                        "inputs": {
                            "linuxRootfs": {
                                "size": source.stat().st_size,
                                "sha256": source_hash,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            binary = root / "linux-ai-controller"
            binary.write_bytes(b"controller")
            prepared = root / "prepared"
            plan = prepare_linux_ai_rootfs.build_plan(
                run_id="p5-materialize-host",
                source_rootfs=source,
                source_manifest=source_manifest,
                profile=PROFILE,
                model_directory=MODEL_DIR,
                repository=ROOT,
                controller_binary=binary,
                controller_mode="mlp",
                seed=43,
                bind_ip="10.77.0.1",
                peer_ip="10.77.0.2",
                udp_port=46000,
                tcp_port=46001,
                output_rootfs=prepared / "linux-ai.ext4",
                output_controller_config=prepared / "linux-controller.json",
                output_manifest=prepared / "linux-ai-rootfs.json",
            )
            config_bytes = (
                json.dumps(plan["controller_config"], ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            guest_bytes = {
                "/linux-ai-controller": binary.read_bytes(),
                "/opt/tgos/model.bin": (MODEL_DIR / "model.bin").read_bytes(),
                "/opt/tgos/metadata.json": (MODEL_DIR / "metadata.json").read_bytes(),
                "/opt/tgos/dataset-manifest.json": (MODEL_DIR / "dataset-manifest.json").read_bytes(),
                "/opt/tgos/golden-vectors.json": (MODEL_DIR / "golden-vectors.json").read_bytes(),
                "/etc/tgos/linux-controller.json": config_bytes,
            }

            def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if "-R" not in argv:
                    return subprocess.CompletedProcess(argv, 0, "", "")
                request = argv[argv.index("-R") + 1]
                if request.startswith("dump "):
                    guest_path, destination = request[len("dump ") :].rsplit(" ", 1)
                    Path(destination).write_bytes(guest_bytes[guest_path])
                return subprocess.CompletedProcess(argv, 0, "", "")

            with mock.patch.object(prepare_linux_ai_rootfs.shutil, "which", side_effect=lambda tool: tool), mock.patch.object(
                prepare_linux_ai_rootfs.subprocess, "run", side_effect=fake_run
            ):
                result = prepare_linux_ai_rootfs.materialize_rootfs(plan)

            self.assertEqual(result["schema_version"], "p5-ai-rootfs-v1")
            self.assertEqual(result["execution"], "completed")
            self.assertFalse(result["qualified"])
            self.assertEqual(
                result["controller_binary"]["dependency_validation"]["linkage"],
                "static",
            )
            self.assertEqual(result["output_rootfs"]["sha256"], source_hash)
            self.assertEqual(
                result["guest_files"]["/opt/tgos/model.bin"]["sha256"],
                prepare_linux_ai_rootfs._sha256(MODEL_DIR / "model.bin"),
            )
            self.assertTrue((prepared / "linux-ai.ext4").is_file())
            self.assertTrue((prepared / "linux-controller.json").is_file())
            self.assertTrue((prepared / "linux-ai-rootfs.json").is_file())
            self.assertTrue(
                any(
                    command["argv"][command["argv"].index("-R") + 1].startswith("dump ")
                    for command in result["debugfs_commands"]
                )
            )
            with mock.patch.object(prepare_linux_ai_rootfs.shutil, "which", side_effect=lambda tool: tool):
                with self.assertRaises(prepare_linux_ai_rootfs.RootfsPlanError):
                    prepare_linux_ai_rootfs.materialize_rootfs(plan)

    def test_rootfs_materializer_requires_debugfs_before_publishing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-materialize-missing-tool-") as name:
            root = Path(name)
            source = root / "linux.ext4"
            source.write_bytes(b"immutable-rootfs")
            source_hash = prepare_linux_ai_rootfs._sha256(source)
            source_manifest = root / "p4-manifest.json"
            source_manifest.write_text(
                json.dumps(
                    {"inputs": {"linuxRootfs": {"size": source.stat().st_size, "sha256": source_hash}}}
                ),
                encoding="utf-8",
            )
            binary = root / "linux-ai-controller"
            binary.write_bytes(b"controller")
            prepared = root / "prepared"
            plan = prepare_linux_ai_rootfs.build_plan(
                run_id="p5-materialize-no-debugfs",
                source_rootfs=source,
                source_manifest=source_manifest,
                profile=PROFILE,
                model_directory=MODEL_DIR,
                controller_binary=binary,
                bind_ip="10.77.0.1",
                peer_ip="10.77.0.2",
                udp_port=46000,
                tcp_port=46001,
                output_rootfs=prepared / "linux-ai.ext4",
                output_controller_config=prepared / "linux-controller.json",
                output_manifest=prepared / "linux-ai-rootfs.json",
            )
            with mock.patch.object(prepare_linux_ai_rootfs.shutil, "which", return_value=None):
                with self.assertRaises(prepare_linux_ai_rootfs.RootfsPlanError):
                    prepare_linux_ai_rootfs.materialize_rootfs(plan)
            self.assertFalse((prepared / "linux-ai.ext4").exists())
            self.assertFalse((prepared / "linux-controller.json").exists())
            self.assertFalse((prepared / "linux-ai-rootfs.json").exists())

    def test_controller_dependency_manifest_binds_dynamic_readelf_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-dependency-") as name:
            root = Path(name)
            binary = root / "linux-ai-controller"
            binary.write_bytes(b"dynamic-controller")
            library = root / "libc.musl-aarch64.so.1"
            library.write_bytes(b"runtime-library")

            def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if "-lW" in argv:
                    output = "Requesting program interpreter: /lib/ld-musl-aarch64.so.1]"
                else:
                    output = "(NEEDED) Shared library: [libc.musl-aarch64.so.1]"
                return subprocess.CompletedProcess(argv, 0, output, "")

            with mock.patch.object(prepare_linux_ai_rootfs.shutil, "which", side_effect=lambda tool: tool), mock.patch.object(
                prepare_linux_ai_rootfs.subprocess, "run", side_effect=fake_run
            ):
                with self.assertRaises(prepare_linux_ai_rootfs.RootfsPlanError):
                    prepare_linux_ai_rootfs._validate_controller_dependencies(
                        binary,
                        readelf="readelf",
                        dependency_manifest=None,
                    )
                manifest = root / "dependencies.json"
                manifest.write_text(
                    json.dumps(
                        {
                            "schema_version": "p5-linux-controller-dependencies-v1",
                            "binary_sha256": prepare_linux_ai_rootfs._sha256(binary),
                            "interpreter": "/lib/ld-musl-aarch64.so.1",
                            "libraries": [
                                {
                                    "soname": "libc.musl-aarch64.so.1",
                                    "path": str(library),
                                    "size": library.stat().st_size,
                                    "sha256": prepare_linux_ai_rootfs._sha256(library),
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                claim = prepare_linux_ai_rootfs._validate_controller_dependencies(
                    binary,
                    readelf="readelf",
                    dependency_manifest=manifest,
                )
            self.assertEqual(claim["linkage"], "dynamic")
            self.assertEqual(claim["status"], "verified_host_readelf_and_manifest")
            self.assertEqual(claim["needed"], ["libc.musl-aarch64.so.1"])

    def test_planned_documented_preparer_cli_binds_all_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-preparer-cli-") as name:
            root = Path(name)
            session = root / "p4-session"
            session.mkdir()
            source = root / "linux.ext4"
            source.write_bytes(b"immutable-rootfs")
            source_hash = prepare_linux_ai_rootfs._sha256(source)
            (session / "manifest.json").write_text(
                json.dumps(
                    {
                        "inputs": {
                            "linuxRootfs": {
                                "size": source.stat().st_size,
                                "sha256": source_hash,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            binary = root / "linux-ai-controller"
            binary.write_bytes(b"target-binary-placeholder")
            prepared = root / "prepared"
            output_manifest = prepared / "linux-ai-rootfs.json"
            self.assertEqual(
                prepare_linux_ai_rootfs.main(
                    [
                        "--repository",
                        str(ROOT),
                        "--from-network-session",
                        str(session),
                        "--source-rootfs",
                        str(source),
                        "--source-manifest",
                        str(session / "manifest.json"),
                        "--controller-binary",
                        str(binary),
                        "--model",
                        str(MODEL_DIR / "model.bin"),
                        "--metadata",
                        str(MODEL_DIR / "metadata.json"),
                        "--dataset-manifest",
                        str(MODEL_DIR / "dataset-manifest.json"),
                        "--golden",
                        str(MODEL_DIR / "golden-vectors.json"),
                        "--model-checksums",
                        str(MODEL_DIR / "checksums.sha256"),
                        "--qualification-profile",
                        str(PROFILE),
                        "--controller-mode",
                        "mlp",
                        "--seed",
                        "43",
                        "--bind-ip",
                        "10.77.0.1",
                        "--peer-ip",
                        "10.77.0.2",
                        "--udp-port",
                        "46000",
                        "--tcp-port",
                        "46001",
                        "--run-id",
                        "phase5-ai-mlp-s43-host",
                        "--output-rootfs",
                        str(prepared / "linux-ai.ext4"),
                        "--output-controller-config",
                        str(prepared / "linux-controller.json"),
                        "--output-manifest",
                        str(output_manifest),
                        "--dry-run",
                    ]
                ),
                0,
            )
            plan = json.loads((prepared / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["from_network_session"]["manifest_sha256"], prepare_linux_ai_rootfs._sha256(session / "manifest.json"))
            self.assertEqual(plan["network"]["udp_port"], 46000)
            self.assertEqual(plan["startup_argv"][2], "mlp")
            self.assertEqual(plan["planned_outputs"]["manifest"], str(output_manifest.absolute()))
            self.assertEqual(plan["controller_binary"]["dependency_validation"], "not_started_host_only")
            self.assertEqual(plan["fault_manifest"]["seed"], 43)
            self.assertEqual(plan["fault_manifest"]["drop_first_control_count"], 15)
            self.assertEqual(plan["controller_config"]["model_version"], 731924617)
            self.assertEqual(
                plan["controller_config"]["fault_manifest_sha256"],
                prepare_linux_ai_rootfs._json_sha256(plan["fault_manifest"]),
            )

    def test_planned_preparer_rejects_network_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-preparer-network-") as name:
            with self.assertRaises(prepare_linux_ai_rootfs.RootfsPlanError):
                prepare_linux_ai_rootfs.build_plan(
                    run_id="p5-network-drift",
                    source_rootfs=None,
                    profile=PROFILE,
                    model_directory=MODEL_DIR,
                    bind_ip="10.77.0.1",
                    peer_ip="10.77.0.2",
                    udp_port=46002,
                    tcp_port=46001,
                )

    def test_guest_vm_input_materializer_publishes_fresh_resolved_configs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-vm-inputs-") as name:
            root = Path(name)
            linux_vm = root / "linux-vm.toml"
            zephyr_vm = root / "zephyr-vm.toml"
            linux_rootfs = root / "linux.ext4"
            zephyr_image = root / "zephyr.bin"
            linux_vm.write_text(
                'kernel_path = "/guest/linux/linux-qemu"\n'
                'kernel_load_addr = 0x8020_0000\n'
                'image_location = "fs"\n',
                encoding="utf-8",
            )
            zephyr_vm.write_text(
                'kernel_path = "/path/to/zephyr.bin"\n', encoding="utf-8"
            )
            linux_rootfs.write_bytes(b"rootfs")
            zephyr_image.write_bytes(b"zephyr")
            output = root / "inputs"

            def fake_convert(*, source_rootfs: Path, output_initramfs: Path) -> dict[str, object]:
                self.assertEqual(source_rootfs, linux_rootfs.resolve())
                output_initramfs.write_bytes(b"initramfs")
                return {"status": "prepared-initramfs"}

            with mock.patch.object(
                run_closed_loop, "convert_initramfs", side_effect=fake_convert
            ):
                result = run_closed_loop.materialize_guest_vm_inputs(
                    linux_vmconfig=linux_vm,
                    zephyr_vmconfig=zephyr_vm,
                    linux_rootfs=linux_rootfs,
                    zephyr_image=zephyr_image,
                    output_dir=output,
                )
            self.assertEqual(result["schema_version"], "p5-ai-vm-inputs-v1")
            self.assertEqual(result["execution"], "completed")
            self.assertFalse(result["qualified"])
            self.assertTrue((output / "configs" / "linux.resolved.toml").is_file())
            self.assertTrue((output / "configs" / "zephyr.resolved.toml").is_file())
            self.assertTrue((output / "configs" / "linux-initramfs.cpio.gz").is_file())
            self.assertIn("qemu-aarch64", (output / "configs" / "linux.resolved.toml").read_text())
            self.assertIn(str(zephyr_image.resolve()), (output / "configs" / "zephyr.resolved.toml").read_text())

            blocked = root / "blocked-inputs"
            with mock.patch.object(
                run_closed_loop, "convert_initramfs", side_effect=ValueError("blocked")
            ):
                with self.assertRaises(run_closed_loop.RunnerPlanError):
                    run_closed_loop.materialize_guest_vm_inputs(
                        linux_vmconfig=linux_vm,
                        zephyr_vmconfig=zephyr_vm,
                        linux_rootfs=linux_rootfs,
                        zephyr_image=zephyr_image,
                        output_dir=blocked,
                    )
            self.assertFalse(blocked.exists())

    def test_guest_runtime_input_materializer_binds_qemu_identity_atomically(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-runtime-inputs-") as name:
            root = Path(name)
            qemu = root / "qemu.toml"
            linux_vm = root / "linux-vm.toml"
            zephyr_vm = root / "zephyr-vm.toml"
            linux_rootfs = root / "linux.ext4"
            zephyr_image = root / "zephyr.bin"
            qemu.write_text('args = ["-machine", "virt"]\n', encoding="utf-8")
            linux_vm.write_text(
                'kernel_path = "/guest/linux/linux-qemu"\n'
                'kernel_load_addr = 0x8020_0000\n'
                'image_location = "fs"\n',
                encoding="utf-8",
            )
            zephyr_vm.write_text('kernel_path = "/path/to/zephyr.bin"\n', encoding="utf-8")
            linux_rootfs.write_bytes(b"rootfs")
            zephyr_image.write_bytes(b"zephyr")
            output = root / "runtime-inputs"

            def fake_convert(*, source_rootfs: Path, output_initramfs: Path) -> dict[str, object]:
                output_initramfs.write_bytes(source_rootfs.read_bytes() + b"-cpio")
                return {"schema_version": "p5-initramfs-v1", "size": output_initramfs.stat().st_size}

            with mock.patch.object(run_closed_loop, "convert_initramfs", side_effect=fake_convert):
                result = run_closed_loop.materialize_guest_runtime_inputs(
                    qemu_config=qemu,
                    linux_vmconfig=linux_vm,
                    zephyr_vmconfig=zephyr_vm,
                    linux_rootfs=linux_rootfs,
                    zephyr_image=zephyr_image,
                    output_dir=output,
                    qmp_socket=root / "runtime" / "qmp.sock",
                    qemu_pidfile=root / "runtime" / "qemu.pid",
                    qemu_name="p5-qemu-test",
                )

            self.assertEqual(result["schema_version"], "p5-ai-runtime-inputs-v1")
            self.assertFalse(result["qualified"])
            resolved_qemu = output / "configs" / "qemu.resolved.toml"
            qemu_text = resolved_qemu.read_text(encoding="utf-8")
            self.assertIn("unix:", qemu_text)
            self.assertIn(str(root / "runtime" / "qemu.pid"), qemu_text)
            self.assertIn('"p5-qemu-test"', qemu_text)
            self.assertEqual(result["resolved_qemu"]["sha256"], run_closed_loop._sha256(resolved_qemu))

            plan = {
                "schema_version": "p5-ai-runtime-plan-v1",
                "session_id": 2,
                "execution": "not_started",
                "qualified": False,
                "command": {
                    "cwd": str(ROOT),
                    "argv": [
                        "cargo", "xtask", "axvisor", "qemu",
                        "--qemu-config", str(resolved_qemu),
                        "--vmconfigs", str(output / "configs" / "linux.resolved.toml"),
                        "--vmconfigs", str(output / "configs" / "zephyr.resolved.toml"),
                    ],
                },
                "runtime_identity": {
                    "qmp_socket": str((root / "runtime" / "qmp.sock").absolute()),
                    "qemu_pidfile": str((root / "runtime" / "qemu.pid").absolute()),
                    "qemu_name": "p5-qemu-test",
                },
                "resolved_qemu": result["resolved_qemu"],
                "guest_configs": result["guest_configs"],
            }
            # The materialized initramfs is a complete file claim; the plan
            # carries the same path plus its source/rootfs binding.
            plan["guest_configs"]["linux"]["initramfs"] = result["initramfs"]
            run_closed_loop.validate_materialized_guest_runtime_inputs(plan, result)
            drifted = dict(result)
            drifted["runtime_identity"] = dict(result["runtime_identity"])
            drifted["runtime_identity"]["qemu_name"] = "other-name"
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.validate_materialized_guest_runtime_inputs(plan, drifted)

            blocked = root / "runtime-inputs-blocked"
            qemu.write_text('args = ["-qmp", "unix:old.sock"]\n', encoding="utf-8")
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.materialize_guest_runtime_inputs(
                    qemu_config=qemu,
                    linux_vmconfig=linux_vm,
                    zephyr_vmconfig=zephyr_vm,
                    linux_rootfs=linux_rootfs,
                    zephyr_image=zephyr_image,
                    output_dir=blocked,
                    qmp_socket=root / "runtime" / "qmp.sock",
                    qemu_pidfile=root / "runtime" / "qemu.pid",
                    qemu_name="p5-qemu-test",
                )
            self.assertFalse(blocked.exists())

    def test_guest_raw_capture_publisher_requires_attributed_guests_and_frames(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-raw-capture-") as name:
            root = Path(name)
            live_log = root / "axvisor-live.log"
            frame_hex = (b"\x02\x00\x00\x00\x00\x01" + b"\x02\x00\x00\x00\x00\x02" + b"\x08\x00" + b"\x00" * 46).hex()
            live_log.write_text(
                "[VM 1] AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=p5-capture\n"
                "[VM 2] AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=p5-capture\n"
                f"virtio-net frame vm=0 generation=1 port=0 dir=port0_to_port1 len=60 hex={frame_hex}\n",
                encoding="utf-8",
            )
            output = root / "bundle"
            result = run_closed_loop.publish_guest_raw_capture(
                output_dir=output, live_log=live_log, run_id="p5-capture", session_id=2
            )
            self.assertEqual(result["schema_version"], "p5-ai-raw-capture-v1")
            self.assertFalse(result["qualified"])
            self.assertTrue((output / "logs" / "axvisor.raw.log").is_file())
            self.assertTrue((output / "logs" / "linux.raw.log").is_file())
            self.assertTrue((output / "logs" / "zephyr.raw.log").is_file())
            self.assertTrue((output / "network" / "frames.jsonl").is_file())
            self.assertTrue((output / "network" / "capture.pcap").is_file())

            blocked = root / "blocked"
            live_log.write_text("[VM 1] only-linux\n", encoding="utf-8")
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.publish_guest_raw_capture(
                    output_dir=blocked, live_log=live_log, run_id="p5-capture", session_id=2
                )
            self.assertTrue((blocked / "logs" / "axvisor.raw.log").is_file())

    def test_guest_event_transcript_publisher_extracts_only_vm_attributed_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-event-transcripts-") as name:
            root = Path(name)
            logs = root / "logs"
            logs.mkdir()
            common = {
                "schema_version": "p5-ai-event-v1",
                "run_id": "p5-events",
                "scenario": "test-017",
                "transport": "udp",
                "session_id": 2,
                "sequence": None,
                "request_id": 1,
                "sample_index": 0,
                "event": "period_release",
                "monotonic_ns": 10,
                "value": None,
                "unit": None,
                "outcome": "released",
            }
            linux = dict(common, endpoint="linux")
            zephyr = dict(common, endpoint="zephyr")
            (logs / "linux.raw.log").write_text(
                "[VM 1] " + json.dumps(linux, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            (logs / "zephyr.raw.log").write_text(
                "[VM 2] " + json.dumps(zephyr, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            result = run_closed_loop.publish_guest_event_transcripts(
                output_dir=root, run_id="p5-events"
            )
            self.assertEqual(result["schema_version"], "p5-ai-event-transcripts-v1")
            self.assertFalse(result["qualified"])
            self.assertIn('"endpoint":"linux"', (root / "metrics" / "linux-events.jsonl").read_text())
            self.assertIn('"endpoint":"zephyr"', (root / "metrics" / "zephyr-events.jsonl").read_text())

            drifted = root / "drifted"
            (drifted / "logs").mkdir(parents=True)
            drifted_record = dict(linux, run_id="other-run")
            (drifted / "logs" / "linux.raw.log").write_text(
                "[VM 1] " + json.dumps(drifted_record) + "\n", encoding="utf-8"
            )
            (drifted / "logs" / "zephyr.raw.log").write_text(
                "[VM 2] " + json.dumps(zephyr) + "\n", encoding="utf-8"
            )
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.publish_guest_event_transcripts(
                    output_dir=drifted, run_id="p5-events"
                )

    def test_guest_observation_bundle_validates_identity_before_event_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-observation-") as name:
            root = Path(name)
            live_log = root / "axvisor-live.log"
            common = {
                "schema_version": "p5-ai-event-v1",
                "run_id": "p5-observation",
                "scenario": "test-017",
                "transport": "udp",
                "session_id": 3,
                "sequence": None,
                "request_id": 1,
                "sample_index": 0,
                "event": "period_release",
                "monotonic_ns": 10,
                "value": None,
                "unit": None,
                "outcome": "released",
            }
            frame_hex = (
                b"\x02\x00\x00\x00\x00\x01"
                + b"\x02\x00\x00\x00\x00\x02"
                + b"\x08\x00"
                + b"\x00" * 46
            ).hex()
            live_log.write_text(
                "contest dtb evidence: vm=1 gpa=0x1000 size=0x100 hpa=0x2000\n"
                "contest dtb evidence: vm=2 gpa=0x3000 size=0x100 hpa=0x4000\n"
                "[VM 1] AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=p5-observation\n"
                "[VM 2] AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=p5-observation\n"
                "[VM 1] " + json.dumps(dict(common, endpoint="linux"), separators=(",", ":")) + "\n"
                "[VM 2] " + json.dumps(dict(common, endpoint="zephyr"), separators=(",", ":")) + "\n"
                f"virtio-net frame vm=0 generation=1 port=0 dir=port0_to_port1 len=60 hex={frame_hex}\n",
                encoding="utf-8",
            )
            output = root / "observation"
            result = run_closed_loop.publish_guest_observation_bundle(
                output_dir=output,
                live_log=live_log,
                run_id="p5-observation",
                session_id=3,
            )
            self.assertEqual(result["schema_version"], "p5-ai-observation-v1")
            self.assertFalse(result["qualified"])
            self.assertEqual(result["runtime_contract"]["ready_vms"], [1, 2])
            self.assertEqual(result["runtime_contract"]["dtb_vms"], [1, 2])
            self.assertTrue((output / "observation.json").is_file())
            self.assertTrue((output / "metrics" / "linux-events.jsonl").is_file())
            self.assertFalse((output / "status.json").exists())
            validation = run_closed_loop.validate_guest_observation_bundle(
                output, run_id="p5-observation", session_id=3
            )
            self.assertTrue(validation["valid"])
            (output / "metrics" / "unindexed.jsonl").write_text("drift\n", encoding="utf-8")
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.validate_guest_observation_bundle(
                    output, run_id="p5-observation", session_id=3
                )
            (output / "metrics" / "unindexed.jsonl").unlink()
            empty = output / "unindexed-empty"
            empty.mkdir()
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.validate_guest_observation_bundle(
                    output, run_id="p5-observation", session_id=3
                )
            empty.rmdir()

            tampered = output / "metrics" / "linux-events.jsonl"
            original = tampered.read_text(encoding="utf-8")
            tampered.write_text(original + "{}\n", encoding="utf-8")
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.validate_guest_observation_bundle(
                    output, run_id="p5-observation", session_id=3
                )
            tampered.write_text(original, encoding="utf-8")
            (output / "status.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.validate_guest_observation_bundle(
                    output, run_id="p5-observation", session_id=3
                )

            blocked = root / "blocked"
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.publish_guest_observation_bundle(
                    output_dir=blocked,
                    live_log=live_log,
                    run_id="p5-observation",
                    session_id=4,
                )

    def test_planned_runner_cli_binds_guest_inputs_without_starting_guest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-runner-cli-") as name:
            root = Path(name)
            session = root / "p4-session"
            session.mkdir()
            (session / "manifest.json").write_text(
                json.dumps({"schema_version": "p4-network-manifest-v1"}),
                encoding="utf-8",
            )
            run_id = "phase5-ai-mlp-s43-runner-host"
            rootfs = root / "linux-ai.ext4"
            rootfs.write_bytes(b"prepared-linux-rootfs")
            rootfs_manifest = root / "linux-ai-rootfs.json"
            rootfs_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "p5-ai-rootfs-v1",
                        "run_id": run_id,
                        "controller_mode": "mlp",
                        "seed": 43,
                    }
                ),
                encoding="utf-8",
            )
            controller_config = root / "linux-controller.json"
            controller_config.write_text(
                json.dumps(
                    {
                        "schema_version": "p5-linux-controller-config-v1",
                        "run_id": run_id,
                        "controller_mode": "mlp",
                        "seed": 43,
                        "model_version": 731924617,
                    }
                ),
                encoding="utf-8",
            )
            zephyr_manifest = root / "zephyr-build-manifest.json"
            zephyr_manifest.write_text(
                json.dumps({"schema_version": "p5-zephyr-control-build-v1", "run_id": run_id}),
                encoding="utf-8",
            )
            build_config = root / "build.toml"
            qemu_config = root / "qemu.toml"
            linux_vm = root / "linux-vm.toml"
            zephyr_vm = root / "zephyr-vm.toml"
            zephyr_image = root / "zephyr.bin"
            zephyr_elf = root / "zephyr.elf"
            for path, content in (
                (build_config, b"build"),
                (qemu_config, b'args = ["-machine", "virt"]\n'),
                (
                    linux_vm,
                    b'kernel_path = "/guest/linux/linux-qemu"\n'
                    b'kernel_load_addr = 0x8020_0000\n'
                    b'image_location = "fs"\n',
                ),
                (zephyr_vm, b'kernel_path = "/path/to/zephyr.bin"\n'),
                (zephyr_image, b"image"),
                (zephyr_elf, b"elf"),
            ):
                path.write_bytes(content)
            output = root / "result"
            common = [
                "--repository", str(ROOT),
                "--from-network-session", str(session),
                "--build-config", str(build_config),
                "--qemu-config", str(qemu_config),
                "--linux-vmconfig", str(linux_vm),
                "--zephyr-vmconfig", str(zephyr_vm),
                "--linux-rootfs", str(rootfs),
                "--linux-rootfs-manifest", str(rootfs_manifest),
                "--linux-controller-config", str(controller_config),
                "--zephyr-image", str(zephyr_image),
                "--zephyr-elf", str(zephyr_elf),
                "--zephyr-build-manifest", str(zephyr_manifest),
                "--scenario", "test-017",
                "--profile", str(PROFILE),
                "--controller", "mlp",
                "--seed", "43",
                "--timeout-seconds", "600",
                "--run-id", run_id,
                "--session-id", "2",
                "--output-dir", str(output),
            ]
            self.assertEqual(run_closed_loop.main(common + ["--dry-run"]), 0)
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "host_contract_valid")
            preflight = json.loads(
                (output / "configs" / "runner-preflight.json").read_text(encoding="utf-8")
            )
            self.assertEqual(preflight["schema_version"], "p5-ai-runner-preflight-v1")
            self.assertEqual(preflight["execution"], "not_started")
            self.assertFalse(preflight["qualified"])
            runtime = preflight["runtime"]
            self.assertEqual(runtime["schema_version"], "p5-ai-runtime-plan-v1")
            self.assertEqual(runtime["session_id"], 2)
            self.assertEqual(runtime["execution"], "not_started")
            run_closed_loop.validate_guest_runtime_plan(runtime)
            invalid_runtime = dict(runtime)
            invalid_runtime["execution"] = "completed"
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.validate_guest_runtime_plan(invalid_runtime)
            invalid_session = dict(runtime, session_id=0)
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.validate_guest_runtime_plan(invalid_session)
            self.assertEqual(
                runtime["command"]["argv"][:4],
                ["cargo", "xtask", "axvisor", "qemu"],
            )
            self.assertTrue(runtime["runtime_identity"]["qmp_socket"].endswith("qmp.sock"))
            self.assertTrue(
                runtime["guest_configs"]["linux"]["resolved"]["path"].endswith(
                    "linux.resolved.toml"
                )
            )
            self.assertEqual(
                runtime["guest_configs"]["zephyr"]["binding"],
                "manifest_bound_placeholder",
            )
            self.assertFalse(Path(runtime["runtime_identity"]["runtime_dir"]).exists())
            self.assertNotIn(
                "linux-vm.toml",
                runtime["command"]["argv"],
            )
            self.assertEqual(
                validate_closed_loop.validate_host_bundle(
                    output, PROFILE, controller="mlp", seed=43
                )["evidence_level"],
                "L2 host contract",
            )
            self.assertEqual(
                run_closed_loop.main(common[:-1] + [str(root / "blocked")]),
                1,
            )
        self.assertEqual(
            run_closed_loop.main(
                [
                    "--run-id",
                    "p5-closed-host",
                    "--session-id",
                    "1",
                    "--controller",
                    "fixed",
                    "--seed",
                    "7",
                    "--output-dir",
                    "unused",
                ]
            ),
            1,
        )

    def test_guest_runtime_from_preflight_separates_inputs_from_evidence_bundle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-preflight-executor-") as name:
            root = Path(name)
            input_dir = root / ".p5-preflight.inputs"
            input_dir.mkdir()
            output = root / "bundle"
            run_id = "p5-preflight-executor"
            plan = {
                "schema_version": "p5-ai-runtime-plan-v1",
                "run_id": run_id,
                "session_id": 5,
                "execution": "not_started",
                "qualified": False,
                "resolved_qemu": {"path": str(input_dir / "configs" / "qemu.resolved.toml")},
                "runtime_identity": {
                    "qmp_socket": str(root / "runtime" / "qmp.sock"),
                    "qemu_pidfile": str(root / "runtime" / "qemu.pid"),
                    "qemu_name": "p5-preflight-qemu",
                },
            }
            preflight = {
                "scenario": "test-017",
                "run_id": run_id,
                "session_id": 5,
                "execution": "not_started",
                "qualified": False,
                "runtime": plan,
                "inputs": {
                    key: {"path": str(root / f"{key}.input")}
                    for key in (
                        "qemu_config",
                        "linux_vmconfig",
                        "zephyr_vmconfig",
                        "linux_rootfs",
                        "zephyr_image",
                    )
                },
            }
            materialized = {"schema_version": "p5-ai-runtime-inputs-v1"}
            execution = {"schema_version": "p5-ai-execution-v1", "session_id": 5}
            with mock.patch.object(
                run_closed_loop,
                "validate_guest_runtime_plan",
            ) as validate_plan, mock.patch.object(
                run_closed_loop,
                "materialize_guest_runtime_inputs",
                return_value=materialized,
            ) as materialize, mock.patch.object(
                run_closed_loop,
                "validate_materialized_guest_runtime_inputs",
            ) as validate, mock.patch.object(
                run_closed_loop,
                "execute_guest_runtime",
                return_value=execution,
            ) as execute:
                result = run_closed_loop.execute_guest_runtime_from_preflight(
                    preflight,
                    output_dir=output,
                )
            self.assertIs(result, execution)
            validate_plan.assert_called_once_with(plan)
            materialize.assert_called_once()
            self.assertEqual(materialize.call_args.kwargs["output_dir"], input_dir)
            validate.assert_called_once_with(plan, materialized)
            execute.assert_called_once_with(
                plan,
                materialized,
                output_dir=output,
                run_id=run_id,
                session_id=5,
                popen_factory=None,
            )
            self.assertFalse(input_dir.exists())

    def test_guest_runtime_from_preflight_rejects_plan_before_materialization(self) -> None:
        preflight = {
            "scenario": "test-017",
            "run_id": "p5-preflight-invalid",
            "session_id": 1,
            "execution": "not_started",
            "qualified": False,
            "runtime": {},
            "inputs": {},
        }
        with mock.patch.object(run_closed_loop, "materialize_guest_runtime_inputs") as materialize:
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.execute_guest_runtime_from_preflight(
                    preflight,
                    output_dir=Path("unused"),
                )
            materialize.assert_not_called()

    def test_guest_executor_rejects_unvalidated_plan_before_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-executor-gate-") as name:
            launcher = mock.Mock()
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.execute_guest_runtime(
                    {"schema_version": "p5-ai-runtime-plan-v1"},
                    {},
                    output_dir=Path(name) / "run",
                    run_id="p5-executor-gate",
                    session_id=1,
                    popen_factory=launcher,
                )
            launcher.assert_not_called()

    def test_guest_executor_uses_resolved_command_and_publishes_unqualified_observation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-executor-positive-") as name:
            root = Path(name)
            runtime_dir = root / "runtime"
            live_log = runtime_dir / "axvisor-live.log"
            output = root / "output"
            run_id = "p5-executor-positive"
            common = {
                "schema_version": "p5-ai-event-v1",
                "run_id": run_id,
                "scenario": "test-017",
                "transport": "udp",
                "session_id": 4,
                "sequence": None,
                "request_id": 1,
                "sample_index": 0,
                "event": "period_release",
                "monotonic_ns": 10,
                "value": None,
                "unit": None,
                "outcome": "released",
            }
            frame_hex = (
                b"\x02\x00\x00\x00\x00\x01"
                + b"\x02\x00\x00\x00\x00\x02"
                + b"\x08\x00"
                + b"\x00" * 46
            ).hex()
            log_bytes = (
                "contest dtb evidence: vm=1 gpa=0x1000 size=0x100 hpa=0x2000\n"
                "contest dtb evidence: vm=2 gpa=0x3000 size=0x100 hpa=0x4000\n"
                f"[VM 1] AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id={run_id}\n"
                f"[VM 2] AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id={run_id}\n"
                "[VM 1] " + json.dumps(dict(common, endpoint="linux"), separators=(",", ":")) + "\n"
                "[VM 2] " + json.dumps(dict(common, endpoint="zephyr"), separators=(",", ":")) + "\n"
                f"virtio-net frame vm=0 generation=1 port=0 dir=port0_to_port1 len=60 hex={frame_hex}\n"
            ).encode("utf-8")

            class FakeProcess:
                def __init__(self, stream: object) -> None:
                    stream.write(log_bytes)  # type: ignore[union-attr]
                    stream.flush()  # type: ignore[union-attr]

                def poll(self) -> int:
                    return 0

            plan = {
                "schema_version": "p5-ai-runtime-plan-v1",
                "run_id": run_id,
                "session_id": 4,
                "execution": "not_started",
                "qualified": False,
                "runtime_identity": {
                    "runtime_dir": str(runtime_dir),
                    "live_log": str(live_log),
                    "qmp_socket": str(runtime_dir / "qmp.sock"),
                    "qemu_pidfile": str(runtime_dir / "qemu.pid"),
                    "qemu_name": "p5-executor-qemu",
                },
                "resolved_qemu": {
                    "path": str(root / "qemu.toml"),
                    "sha256": "qemu-hash",
                    "source": {},
                },
                "guest_configs": {
                    "linux": {"resolved": {"path": str(root / "linux.toml"), "sha256": "linux-hash"}},
                    "zephyr": {"resolved": {"path": str(root / "zephyr.toml"), "sha256": "zephyr-hash"}},
                },
                "command": {
                    "cwd": str(root),
                    "argv": [
                        "cargo", "xtask", "axvisor", "qemu",
                        "--qemu-config", str(root / "qemu.toml"),
                        "--vmconfigs", str(root / "linux.toml"),
                        "--vmconfigs", str(root / "zephyr.toml"),
                    ],
                },
            }
            with mock.patch.object(
                run_closed_loop, "validate_materialized_guest_runtime_inputs"
            ) as validate_inputs:
                result = run_closed_loop.execute_guest_runtime(
                    plan,
                    {"schema_version": "p5-ai-runtime-inputs-v1"},
                    output_dir=output,
                    run_id=run_id,
                    session_id=4,
                    popen_factory=lambda _argv, **kwargs: FakeProcess(kwargs["stdout"]),
                )
            validate_inputs.assert_called_once()
            self.assertEqual(result["schema_version"], "p5-ai-execution-v1")
            self.assertFalse(result["qualified"])
            self.assertTrue((output / "observation.json").is_file())
            self.assertTrue((output / "execution.json").is_file())
            self.assertFalse((output / "status.json").exists())
            execution_validation = run_closed_loop.validate_guest_execution_manifest(
                output, run_id=run_id, session_id=4
            )
            self.assertTrue(execution_validation["valid"])
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.publish_guest_runtime_status(
                    output,
                    run_id=run_id,
                    session_id=4,
                    controller="mlp",
                    seed=43,
                    profile_id="qualification-v1",
                    status_token="ai_control_completed",
                    primary_error="not allowed",
                )
            execution = json.loads(
                (output / "execution.json").read_text(encoding="utf-8")
            )
            execution["qualified"] = True
            (output / "execution.json").write_text(
                json.dumps(execution) + "\n", encoding="utf-8"
            )
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.validate_guest_execution_manifest(
                    output, run_id=run_id, session_id=4
                )
            execution["qualified"] = False
            (output / "execution.json").write_text(
                json.dumps(execution) + "\n", encoding="utf-8"
            )
            status_result = run_closed_loop.publish_guest_runtime_status(
                output,
                run_id=run_id,
                session_id=4,
                controller="mlp",
                seed=43,
                profile_id="qualification-v1",
                status_token="ai_control_blocked",
                primary_error="full TEST-017 evidence is not yet available",
                blocked_reason="the current host has no approved Cargo/QEMU runtime backend",
            )
            self.assertEqual(status_result["status"], "ai_control_blocked")
            status_validation = run_closed_loop.validate_guest_runtime_status(
                output, run_id=run_id, session_id=4
            )
            self.assertTrue(status_validation["valid"])
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["success"])
            self.assertTrue(status["statusLast"])
            self.assertEqual(status["status"], "ai_control_blocked")
            self.assertNotIn("status.json", json.loads((output / "manifest.json").read_text())["files"])
            self.assertFalse(runtime_dir.exists())
            unindexed_empty = output / "unindexed-empty"
            unindexed_empty.mkdir()
            with self.assertRaises(run_closed_loop.RunnerPlanError):
                run_closed_loop.validate_guest_runtime_status(
                    output, run_id=run_id, session_id=4
                )
            unindexed_empty.rmdir()

            class EarlyProcess:
                def __init__(self, stream: object) -> None:
                    stream.write(b"[VM 1] early-exit\n")  # type: ignore[union-attr]
                    stream.flush()  # type: ignore[union-attr]

                def poll(self) -> int:
                    return 0

            failure_output = root / "failure-output"
            with mock.patch.object(
                run_closed_loop, "validate_materialized_guest_runtime_inputs"
            ):
                with self.assertRaises(run_closed_loop.RunnerPlanError):
                    run_closed_loop.execute_guest_runtime(
                        plan,
                        {"schema_version": "p5-ai-runtime-inputs-v1"},
                        output_dir=failure_output,
                        run_id=run_id,
                        session_id=4,
                        popen_factory=lambda _argv, **kwargs: EarlyProcess(kwargs["stdout"]),
                    )
            self.assertTrue(runtime_dir.exists())
            self.assertTrue(live_log.is_file())

    def test_closed_loop_dry_run_is_l2_status_last(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p5-c1c2-") as name:
            output = Path(name) / "run"
            self.assertEqual(
                run_closed_loop.main(
                    [
                        "--run-id",
                        "p5-closed-host",
                        "--session-id",
                        "2",
                        "--controller",
                        "mlp",
                        "--seed",
                        "43",
                        "--output-dir",
                        str(output),
                        "--dry-run",
                    ]
                ),
                0,
            )
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "host_contract_valid")
            self.assertTrue(status["statusLast"])
            self.assertNotIn("ai_control_completed", json.dumps(status))
            result = validate_closed_loop.validate_host_bundle(
                output,
                PROFILE,
                controller="mlp",
                seed=43,
            )
            self.assertTrue(result["valid"])
            self.assertEqual(result["evidence_level"], "L2 host contract")
            (output / "unindexed.txt").write_text("drift\n", encoding="utf-8")
            with self.assertRaises(validate_closed_loop.ClosedLoopValidationError):
                validate_closed_loop.validate_host_bundle(
                    output,
                    PROFILE,
                    controller="mlp",
                    seed=43,
                )


if __name__ == "__main__":
    unittest.main()
