#!/usr/bin/env python3
"""Host/source tests for the explicit TEST-018 runner binding boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "scripts" / "contest" / "ai"
sys.path.insert(0, str(AI_DIR))

_FAULT_PROFILE_SPEC = importlib.util.spec_from_file_location(
    "_test_ai_fault_profile", AI_DIR / "fault_profile.py"
)
assert _FAULT_PROFILE_SPEC is not None and _FAULT_PROFILE_SPEC.loader is not None
fault_profile = importlib.util.module_from_spec(_FAULT_PROFILE_SPEC)
sys.modules[_FAULT_PROFILE_SPEC.name] = fault_profile
_FAULT_PROFILE_SPEC.loader.exec_module(fault_profile)
import run_closed_loop  # noqa: E402


FAULTS = ROOT / "configs" / "contest" / "ai" / "faults-v1.json"
QUALIFICATION = ROOT / "configs" / "contest" / "ai" / "qualification-v1.json"


class Test018RunnerBinding(unittest.TestCase):
    def test_profile_loads_and_selected_case_is_detached(self) -> None:
        document = fault_profile.load_fault_profile(FAULTS)
        selected = fault_profile.select_fault_case(document, "linux-stop")
        self.assertEqual(tuple(item["case"] for item in document["cases"]), fault_profile.CASE_IDS)
        self.assertEqual(selected["case"], "linux-stop")
        selected["expected"]["safe_deadline_ms"] = 501
        self.assertEqual(document["cases"][5]["expected"]["safe_deadline_ms"], 500)

    def test_test017_cannot_carry_fault_case(self) -> None:
        with self.assertRaises(run_closed_loop.RunnerPlanError):
            run_closed_loop.build_runtime_preflight(
                repository=ROOT,
                from_network_session=ROOT,
                build_config=ROOT / "missing-build",
                qemu_config=ROOT / "missing-qemu",
                linux_vmconfig=ROOT / "missing-linux-vm",
                zephyr_vmconfig=ROOT / "missing-zephyr-vm",
                linux_rootfs=ROOT / "missing-rootfs",
                linux_rootfs_manifest=ROOT / "missing-rootfs-manifest",
                linux_controller_config=ROOT / "missing-controller",
                zephyr_image=ROOT / "missing-image",
                zephyr_elf=ROOT / "missing-elf",
                zephyr_build_manifest=ROOT / "missing-zephyr-manifest",
                scenario="test-017",
                profile=ROOT / "configs" / "contest" / "ai" / "qualification-v1.json",
                fault_profile=FAULTS,
                fault_case="linux-stop",
                controller="mlp",
                seed=43,
                timeout_seconds=600,
                run_id="test018-binding-negative",
                session_id=1,
                output_dir=ROOT / "unused-test018-output",
            )

    def test_cli_forwards_one_explicit_case_then_stops_before_guest(self) -> None:
        common = [
            "--repository", str(ROOT),
            "--from-network-session", str(ROOT / "p4"),
            "--build-config", str(ROOT / "build"),
            "--qemu-config", str(ROOT / "qemu"),
            "--linux-vmconfig", str(ROOT / "linux-vm"),
            "--zephyr-vmconfig", str(ROOT / "zephyr-vm"),
            "--linux-rootfs", str(ROOT / "rootfs"),
            "--linux-rootfs-manifest", str(ROOT / "rootfs-manifest"),
            "--linux-controller-config", str(ROOT / "controller"),
            "--zephyr-image", str(ROOT / "zephyr-image"),
            "--zephyr-elf", str(ROOT / "zephyr-elf"),
            "--zephyr-build-manifest", str(ROOT / "zephyr-manifest"),
            "--scenario", "test-018",
            "--profile", str(ROOT / "configs" / "contest" / "ai" / "qualification-v1.json"),
            "--fault-profile", str(FAULTS),
            "--fault-case", "linux-stop",
            "--controller", "mlp",
            "--seed", "43",
            "--run-id", "test018-cli-binding",
            "--session-id", "1",
            "--output-dir", str(ROOT / "unused-test018-output"),
        ]
        with mock.patch.object(run_closed_loop, "build_runtime_preflight", return_value={} ) as preflight:
            self.assertEqual(run_closed_loop.main(common), 1)
        kwargs = preflight.call_args.kwargs
        self.assertEqual(kwargs["scenario"], "test-018")
        self.assertEqual(kwargs["fault_case"], "linux-stop")
        self.assertEqual(kwargs["fault_profile"], FAULTS)

    def test_dry_preflight_publishes_case_copy_without_runtime_claim(self) -> None:
        document = fault_profile.load_fault_profile(FAULTS)
        selected = fault_profile.select_fault_case(document, "linux-stop")
        preflight = {
            "schema_version": "p5-ai-runner-preflight-v1",
            "run_id": "test018-host-plan",
            "session_id": 1,
            "scenario": "test-018",
            "controller": "mlp",
            "seed": 43,
            "qualified": False,
            "inputs": {
                "profile": {
                    "path": str(QUALIFICATION),
                    "size": QUALIFICATION.stat().st_size,
                    "sha256": hashlib.sha256(QUALIFICATION.read_bytes()).hexdigest(),
                },
                "fault_profile": {
                    "path": str(FAULTS),
                    "size": FAULTS.stat().st_size,
                    "sha256": hashlib.sha256(FAULTS.read_bytes()).hexdigest(),
                },
                "fault_case": {
                    "case": "linux-stop",
                    "definition": selected,
                    "sha256": hashlib.sha256(
                        json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest(),
                    "binding": "immutable_profile_case_copy",
                },
            },
        }
        with tempfile.TemporaryDirectory(prefix="p5-test018-host-") as name:
            output = Path(name) / "bundle"
            publication = run_closed_loop.write_test018_host_preflight_bundle(
                preflight,
                output_dir=output,
                command=("python", "run_closed_loop.py", "--scenario", "test-018"),
                cwd=ROOT,
            )
            validation = run_closed_loop.validate_test018_host_preflight_bundle(
                output,
                run_id="test018-host-plan",
                session_id=1,
                fault_case="linux-stop",
            )
            self.assertEqual(publication["fault_case"], "linux-stop")
            self.assertFalse(publication["qualified"])
            self.assertEqual(validation["evidence_level"], "L2 host contract")
            self.assertFalse(validation["qualified"])
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "host_contract_valid")
            self.assertFalse(status["qualified"])
            case_copy = json.loads((output / "configs" / "fault-case.json").read_text(encoding="utf-8"))
            self.assertEqual(case_copy, selected)
            self.assertEqual(json.loads((output / "session.json").read_text(encoding="utf-8"))["scenario"], "test-018")


if __name__ == "__main__":
    unittest.main()
