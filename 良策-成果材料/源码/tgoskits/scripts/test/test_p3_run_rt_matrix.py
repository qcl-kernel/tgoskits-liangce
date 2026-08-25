#!/usr/bin/env python3
"""Minimal host-only tests for the CODE-C P3 candidate."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RT_DIR = ROOT / "scripts" / "contest" / "rt"
sys.path.insert(0, str(RT_DIR))

import p3_contract  # noqa: E402
import run_rt_matrix  # noqa: E402


def _matrix(mode: str = "production-ab") -> dict[str, object]:
    runs: list[dict[str, object]] = []
    for seed in p3_contract.SEEDS:
        if mode == "production-ab":
            for state in ("off", "on"):
                runs.append(
                    {
                        "scenario": "RT-AX-IDLE",
                        "seed": seed,
                        "path_id": "vcpu-wake",
                        "feature_state": state,
                        "required_gates": ["TEST-007"],
                    }
                )
        else:
            runs.append(
                {
                    "scenario": "RT-NATIVE",
                    "seed": seed,
                    "path_id": None,
                    "feature_state": "off",
                    "required_gates": [],
                }
            )
    return {
        "schema_version": "p3-rt-matrix-v1",
        "matrix_id": "p3-contract",
        "mode": mode,
        "seeds": list(p3_contract.SEEDS),
        "profiles": [{"scenario": "RT-AX-IDLE" if mode == "production-ab" else "RT-NATIVE", "sha256": "a" * 64}],
        "runs": runs,
    }


class P3RunMatrixContractTests(unittest.TestCase):
    def test_production_requires_exact_off_on_pairs(self) -> None:
        plan = p3_contract.validate_matrix(_matrix())
        self.assertEqual(plan["run_count"], 6)
        self.assertEqual(len({run["run_id"] for run in plan["runs"]}), 6)

        broken = copy.deepcopy(_matrix())
        broken["runs"] = broken["runs"][:-1]
        with self.assertRaises(p3_contract.P3ContractError):
            p3_contract.validate_matrix(broken)

    def test_dry_run_writes_status_last_and_explicit_na(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p3-code-c-", dir=ROOT) as name:
            root = Path(name)
            matrix_path = root / "matrix.json"
            output = root / "dry-run"
            matrix_path.write_text(json.dumps(_matrix()), encoding="utf-8")
            self.assertEqual(
                run_rt_matrix.main(
                    ["--matrix", str(matrix_path), "--dry-run", "--output-dir", str(output)]
                ),
                0,
            )
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["success"])
            self.assertEqual(status["status"], "realtime_measurement_blocked")
            self.assertTrue(status["statusLast"])
            package = run_rt_matrix.validate_matrix_package(output)
            self.assertTrue(package["valid"])
            self.assertFalse(package["qualified"])
            summary = json.loads(
                next((output / "summaries").glob("*.json")).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "blocked")
            self.assertFalse(summary["exactly_once"])
            self.assertTrue(summary["missing_events"])
            self.assertNotIn("status.json", json.loads((output / "manifest.json").read_text())["files"])

    def test_package_consumer_rejects_unindexed_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p3-code-c-", dir=ROOT) as name:
            root = Path(name)
            matrix_path = root / "matrix.json"
            output = root / "dry-run"
            matrix_path.write_text(json.dumps(_matrix("native")), encoding="utf-8")
            self.assertEqual(
                run_rt_matrix.main(
                    ["--matrix", str(matrix_path), "--dry-run", "--output-dir", str(output)]
                ),
                0,
            )
            (output / "unindexed-empty-directory").mkdir()
            with self.assertRaises(p3_contract.P3ContractError):
                run_rt_matrix.validate_matrix_package(output)
            (output / "unindexed-empty-directory").rmdir()
            (output / "unindexed.txt").write_text("tamper\n", encoding="utf-8")
            with self.assertRaises(p3_contract.P3ContractError):
                run_rt_matrix.validate_matrix_package(output)

    def test_package_consumer_rejects_hash_tamper_and_early_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p3-code-c-", dir=ROOT) as name:
            root = Path(name)
            matrix_path = root / "matrix.json"
            output = root / "dry-run"
            matrix_path.write_text(json.dumps(_matrix("native")), encoding="utf-8")
            self.assertEqual(
                run_rt_matrix.main(
                    ["--matrix", str(matrix_path), "--dry-run", "--output-dir", str(output)]
                ),
                0,
            )
            manifest_path = output / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            first_file = next(iter(manifest["files"].values()))
            first_file["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(p3_contract.P3ContractError):
                run_rt_matrix.validate_matrix_package(output)

        with tempfile.TemporaryDirectory(prefix="p3-code-c-", dir=ROOT) as name:
            root = Path(name)
            matrix_path = root / "matrix.json"
            output = root / "dry-run"
            matrix_path.write_text(json.dumps(_matrix("native")), encoding="utf-8")
            self.assertEqual(
                run_rt_matrix.main(
                    ["--matrix", str(matrix_path), "--dry-run", "--output-dir", str(output)]
                ),
                0,
            )
            status_path = output / "status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["statusLast"] = False
            status_path.write_text(json.dumps(status), encoding="utf-8")
            with self.assertRaises(p3_contract.P3ContractError):
                run_rt_matrix.validate_matrix_package(output)

    def test_manifest_enumerator_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p3-code-c-", dir=ROOT) as name:
            root = Path(name)
            target = root / "target.txt"
            target.write_text("bound\n", encoding="utf-8")
            link = root / "link.txt"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as error:
                if os.name == "nt":
                    self.skipTest(f"file symlink fixture unavailable: {error}")
                raise
            with self.assertRaises(p3_contract.P3ContractError):
                p3_contract.manifest_files(root)

    def test_non_dry_run_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p3-code-c-", dir=ROOT) as name:
            matrix_path = Path(name) / "matrix.json"
            matrix_path.write_text(json.dumps(_matrix("native")), encoding="utf-8")
            self.assertEqual(run_rt_matrix.main(["--matrix", str(matrix_path)]), 1)


if __name__ == "__main__":
    unittest.main()
