#!/usr/bin/env python3
"""Unit and static checks for the P5 host-only orchestration candidate."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "scripts" / "contest" / "ai"
sys.path.insert(0, str(AI_DIR))

import host_orchestration  # noqa: E402


PROFILE = ROOT / "configs" / "contest" / "ai" / "qualification-v1.json"
MODEL = ROOT / "apps" / "contest" / "linux-ai-controller" / "model" / "model.bin"


def config(controller: str = "fixed", seed: int = 7) -> host_orchestration.HostContractConfig:
    return host_orchestration.HostContractConfig.from_paths(
        identity=host_orchestration.HostIdentity(
            run_id=f"p5-host-{controller}-s{seed}",
            session_id=17 if controller == "fixed" else 18,
            controller=controller,
            seed=seed,
        ),
        profile_path=PROFILE,
        model_path=MODEL,
    )


class P5HostOrchestrationTest(unittest.TestCase):
    def test_fixed_and_mlp_complete_exact_virtual_contract(self) -> None:
        for controller in ("fixed", "mlp"):
            with self.subTest(controller=controller):
                run = host_orchestration.run_host_contract(config(controller))
                self.assertEqual(len(run.cycles), 1_800)
                self.assertEqual(run.cycles[0].sample_index, 0)
                self.assertEqual(run.cycles[-1].sample_index, 1_799)
                self.assertEqual(run.cycles[0].request_id, 1)
                self.assertEqual(run.cycles[-1].request_id, 1_800)
                self.assertEqual(run.cycles[-1].release_ms + 100, 180_000)
                self.assertTrue(all(cycle.deadline_met for cycle in run.cycles))
                self.assertEqual(run.summary()["evidence_level"], "L2 host contract")
                self.assertEqual(run.summary()["runtime_evidence"], "not_produced")

    def test_watchdog_is_exactly_500_ms_and_safe_zero(self) -> None:
        self.assertFalse(
            host_orchestration.evaluate_watchdog(last_valid_apply_ms=0, now_ms=499).expired
        )
        for now_ms in (500, 501):
            state = host_orchestration.evaluate_watchdog(
                last_valid_apply_ms=0, now_ms=now_ms
            )
            self.assertTrue(state.expired)
            self.assertEqual(state.duty_q16_16, 0)
        self.assertTrue(
            host_orchestration.evaluate_watchdog(last_valid_apply_ms=None, now_ms=0).expired
        )

    def test_old_600_tick_or_8_second_profile_is_rejected(self) -> None:
        profile = json.loads(PROFILE.read_text(encoding="utf-8"))
        old_profile = deepcopy(profile)
        old_profile["timing"]["ticks"] = 600
        old_profile["timing"]["duration_seconds"] = 8
        with self.assertRaises(host_orchestration.HostContractError):
            host_orchestration.validate_profile_contract(old_profile)

    def test_identity_and_model_hash_are_fail_closed(self) -> None:
        with self.assertRaises(host_orchestration.HostContractError):
            host_orchestration.HostIdentity(
                run_id="../escape",
                session_id=1,
                controller="mlp",
                seed=7,
            ).validate()
        bad_model = MODEL.read_bytes()[:-1] + bytes([MODEL.read_bytes()[-1] ^ 1])
        profile = json.loads(PROFILE.read_text(encoding="utf-8"))
        self.assertNotEqual(
            hashlib.sha256(bad_model).hexdigest(), profile["model"]["model_sha256"]
        )
        bad_profile = deepcopy(profile)
        bad_profile["model"]["model_sha256"] = hashlib.sha256(bad_model).hexdigest()
        with self.assertRaises(host_orchestration.HostContractError):
            host_orchestration.validate_profile_contract(bad_profile)

    def test_bundle_is_status_last_and_host_only(self) -> None:
        run = host_orchestration.run_host_contract(config("mlp"))
        bundle = run.to_bundle(command=("test", "dry-run"), cwd=ROOT)
        self.assertEqual(next(reversed(bundle.files)), "status.json")
        status = json.loads(bundle.files["status.json"])
        self.assertEqual(status["status"], "host_contract_valid")
        self.assertTrue(status["statusLast"])
        self.assertEqual(status["manifestSha256"], bundle.manifest_sha256)
        session = json.loads(bundle.files["session.json"])
        self.assertEqual(session["execution_kind"], "host_contract")
        self.assertEqual(session["evidence_level"], "L2 host contract")
        self.assertNotIn("ai_control_completed", bundle.files["status.json"].decode("utf-8"))
        manifest = json.loads(bundle.files["manifest.json"])
        self.assertNotIn("session.json", manifest["files"])
        self.assertNotIn("status.json", manifest["files"])

    def test_candidate_has_no_blocking_runtime_primitives(self) -> None:
        source = (AI_DIR / "host_orchestration.py").read_text(encoding="utf-8")
        self.assertNotIn("time.sleep", source)
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("import socket", source)


if __name__ == "__main__":
    unittest.main()
