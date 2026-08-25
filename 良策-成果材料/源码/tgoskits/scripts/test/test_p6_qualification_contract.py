#!/usr/bin/env python3
"""Host-only P6 combined-profile and preflight contracts."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
QUALIFICATION_DIR = ROOT / "scripts" / "contest" / "qualification"
sys.path.insert(0, str(QUALIFICATION_DIR))

import qualification_contract as contract  # noqa: E402
import compute_qualification_metrics  # noqa: E402
import publish_qualification  # noqa: E402
import run_qualification  # noqa: E402
import run_full  # noqa: E402


PROFILE = ROOT / "configs" / "contest" / "qualification" / "combined-v1.json"


class P6QualificationContractTests(unittest.TestCase):
    def test_combined_profile_freezes_phases_and_exact_fault_targets(self) -> None:
        profile = contract.read_json(PROFILE, "combined-v1")
        self.assertEqual(profile["duration_s"], contract.DURATION_S)
        self.assertEqual(profile["seeds"], list(contract.SEEDS))
        self.assertEqual(
            [phase["name"] for phase in profile["phases"]],
            [phase[0] for phase in contract.PHASES],
        )
        contract._validate_fault_manifest(profile["fault_manifest"], "fault_manifest")

    def test_full_manifest_consumer_rejects_unindexed_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p6-full-manifest-") as name:
            root = Path(name) / "run"
            root.mkdir()
            records = []
            for relative in contract.REQUIRED_FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("evidence\n", encoding="utf-8")
                records.append(
                    {
                        "path": relative,
                        "size": path.stat().st_size,
                        "sha256": contract.sha256_file(path),
                        "producer": "fixture",
                    }
                )
            (root / "manifest.json").write_text("manifest\n", encoding="utf-8")
            (root / "status.json").write_text("status\n", encoding="utf-8")
            manifest = {
                "schema_version": contract.MANIFEST_SCHEMA,
                "run_id": "p6-full-manifest-s7",
                "files": records,
            }
            self.assertEqual(
                contract.validate_manifest(root, manifest, "p6-full-manifest-s7"),
                contract.sha256_file(root / "manifest.json"),
            )
            (root / "unindexed.txt").write_text("drift\n", encoding="utf-8")
            with self.assertRaises(contract.QualificationError):
                contract.validate_manifest(root, manifest, "p6-full-manifest-s7")

    def test_preflight_is_fresh_manifest_bound_and_not_qualified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p6-preflight-") as name:
            output = Path(name) / "run"
            plan = run_qualification.build_preflight(
                run_id="p6-host-s7",
                seed=7,
                profile_path=PROFILE,
                source_revision="unbound",
                qemu_identity="unbound",
                nonce="unbound",
            )
            run_qualification.write_preflight(plan, PROFILE, output)
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["success"])
            self.assertFalse(status["qualified"])
            self.assertEqual(status["status"], "qualification_smoke_completed")
            self.assertTrue(status["statusLast"])
            self.assertEqual(
                status["manifest_sha256"],
                contract.sha256_file(output / "manifest.json"),
            )
            report = contract.validate_preflight_package(output, expected_run_id="p6-host-s7")
            self.assertFalse(report["qualified"])
            self.assertEqual(report["status"], "qualification_smoke_completed")

    def test_preflight_consumer_rejects_unindexed_or_tampered_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p6-preflight-consumer-") as name:
            output = Path(name) / "run"
            plan = run_qualification.build_preflight(
                run_id="p6-host-s19",
                seed=19,
                profile_path=PROFILE,
            )
            run_qualification.write_preflight(plan, PROFILE, output)
            (output / "extra.txt").write_text("unindexed\n", encoding="utf-8")
            with self.assertRaises(contract.QualificationError):
                contract.validate_preflight_package(output)

            (output / "extra.txt").unlink()
            commands = output / "commands.jsonl"
            commands.write_text(
                commands.read_text(encoding="utf-8").replace("not_started", "started"),
                encoding="utf-8",
            )
            with self.assertRaises(contract.QualificationError):
                contract.validate_preflight_package(output)

    def test_fault_manifest_without_exact_target_is_rejected(self) -> None:
        profile = contract.read_json(PROFILE, "combined-v1")
        broken = json.loads(json.dumps(profile))
        broken["fault_manifest"]["targets"] = broken["fault_manifest"]["targets"][:-1]
        with self.assertRaises(contract.QualificationError):
            contract._validate_fault_manifest(broken["fault_manifest"], "fault_manifest")

    def test_cleanup_requires_all_residual_fields(self) -> None:
        with self.assertRaises(contract.QualificationError):
            contract._validate_cleanup({})

    def test_full_dry_run_cannot_mint_qualification_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p6-full-") as name:
            output = Path(name) / "aggregate"
            fake_qualified = {
                "schema_version": contract.AGGREGATE_SCHEMA,
                "profile": contract.PROFILE,
                "qualified": True,
                "sessions": [
                    {"run_id": "historical-7", "seed": 7, "manifest_sha256": "0" * 64},
                    {"run_id": "historical-19", "seed": 19, "manifest_sha256": "1" * 64},
                    {"run_id": "historical-43", "seed": 43, "manifest_sha256": "2" * 64},
                ],
                "non_claims": list(contract.AGGREGATE_NON_CLAIMS),
            }
            with patch.object(run_full, "aggregate_runs", return_value=fake_qualified):
                self.assertEqual(
                    run_full.main(
                        [
                            "--run",
                            "historical-1",
                            "--run",
                            "historical-2",
                            "--run",
                            "historical-3",
                            "--output-dir",
                            str(output),
                            "--dry-run",
                        ]
                    ),
                    0,
                )
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["success"])
            self.assertFalse(status["qualified"])
            self.assertEqual(status["status"], "qualification_smoke_completed")
            self.assertTrue(status["statusLast"])
            report = contract.validate_aggregate_preflight_package(output)
            self.assertEqual(report["seeds"], [7, 19, 43])

    def test_aggregate_preflight_consumer_rejects_tampering_or_unindexed_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p6-aggregate-consumer-") as name:
            output = Path(name) / "aggregate"
            fake_qualified = {
                "schema_version": contract.AGGREGATE_SCHEMA,
                "profile": contract.PROFILE,
                "qualified": True,
                "sessions": [
                    {"run_id": "historical-7", "seed": 7, "manifest_sha256": "0" * 64},
                    {"run_id": "historical-19", "seed": 19, "manifest_sha256": "1" * 64},
                    {"run_id": "historical-43", "seed": 43, "manifest_sha256": "2" * 64},
                ],
                "non_claims": list(contract.AGGREGATE_NON_CLAIMS),
            }
            with patch.object(run_full, "aggregate_runs", return_value=fake_qualified):
                self.assertEqual(
                    run_full.main(
                        [
                            "--run", "historical-1", "--run", "historical-2", "--run", "historical-3",
                            "--output-dir", str(output), "--dry-run",
                        ]
                    ),
                    0,
                )
            (output / "extra.txt").write_text("unindexed\n", encoding="utf-8")
            with self.assertRaises(contract.QualificationError):
                contract.validate_aggregate_preflight_package(output)

    def test_runtime_cli_is_fail_closed_without_dry_run(self) -> None:
        self.assertEqual(
            run_qualification.main(
                [
                    "--run-id",
                    "p6-blocked",
                    "--seed",
                    "7",
                    "--output-dir",
                    "unused",
                ]
            ),
            1,
        )

    def test_metrics_and_publisher_do_not_create_runtime_success(self) -> None:
        self.assertEqual(
            compute_qualification_metrics.main(["--run", "missing-p6-run"]),
            1,
        )
        self.assertEqual(
            publish_qualification.main(
                [
                    "--run",
                    "missing-p6-run",
                    "--output-dir",
                    "unused",
                ]
            ),
            1,
        )

    def test_publication_preflight_consumer_rejects_unindexed_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p6-publication-consumer-") as name:
            source = Path(name) / "source"
            source.mkdir()
            (source / "input").mkdir()
            (source / "input" / "scenario.json").write_text(
                json.dumps(
                    {
                        "run_id": "p6-publish-s7",
                        "source_revision": "unbound",
                        "qemu_identity": "unbound",
                        "nonce": "unbound",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            output = Path(name) / "publication"
            plan = publish_qualification.build_preflight(source)
            publish_qualification.write_preflight(plan, output)
            report = contract.validate_publication_preflight_package(output)
            self.assertFalse(report["qualified"])
            (output / "extra.txt").write_text("unindexed\n", encoding="utf-8")
            with self.assertRaises(contract.QualificationError):
                contract.validate_publication_preflight_package(output)


if __name__ == "__main__":
    unittest.main()
