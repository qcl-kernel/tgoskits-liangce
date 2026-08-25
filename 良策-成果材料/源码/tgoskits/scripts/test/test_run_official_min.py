#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "contest" / "ci" / "run_official_min.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("run_official_min", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def row(check_id: str, group: str, *, phase: str = "test") -> dict:
    return {
        "id": check_id,
        "group": group,
        "phase": phase,
        "name": check_id,
        "command": "true",
        "source": "fixture.toml",
        "timeout_minutes": 1,
    }


class OfficialMinSelectionTests(unittest.TestCase):
    def test_selection_derives_all_contest_rows_from_static_matrix(self) -> None:
        plan = {
            "static_matrix": {
                "include": [
                    row("check-formatting", "Static", phase="static"),
                    row("run-sync-lint", "Static", phase="static"),
                    row("contest-one", "Contest", phase="static"),
                    row("contest-two", "Contest", phase="static"),
                ]
            },
            "workspace_matrix": {
                "include": [row("run-clippy", "Workspace"), row("test-with-std", "Workspace")]
            },
            "arceos_matrix": {"include": []},
            "starry_matrix": {"include": []},
            "axvisor_matrix": {
                "include": [row(runner.OFFICIAL_MIN_CORE_ID, "AxVisor")]
            },
        }

        selected, missing = runner.select_official_min_rows(plan)

        self.assertEqual(missing, [])
        self.assertEqual(
            [item["id"] for item in selected],
            [
                "check-formatting",
                "run-sync-lint",
                "run-clippy",
                "test-with-std",
                "contest-one",
                "contest-two",
                runner.OFFICIAL_MIN_CORE_ID,
            ],
        )

    def test_missing_core_row_is_reported_instead_of_skipped(self) -> None:
        plan = {
            "static_matrix": {
                "include": [
                    row("check-formatting", "Static", phase="static"),
                    row("run-sync-lint", "Static", phase="static"),
                ]
            },
            "workspace_matrix": {
                "include": [row("run-clippy", "Workspace"), row("test-with-std", "Workspace")]
            },
            "arceos_matrix": {"include": []},
            "starry_matrix": {"include": []},
            "axvisor_matrix": {"include": []},
        }

        _selected, missing = runner.select_official_min_rows(plan)

        self.assertEqual(missing, [runner.OFFICIAL_MIN_CORE_ID])


class OfficialMinExecutionTests(unittest.TestCase):
    def test_dry_run_uses_actual_repository_planner(self) -> None:
        args = runner.parse_args(
            [
                "--since-ref",
                "f96452ce892e2916a0c5bfe5aa9e7908b8085c06",
                "--dry-run",
            ]
        )
        plan = runner.build_official_plan(
            repository=args.repository,
            repository_owner=args.repository_owner,
            event_name=args.event_name,
            base_ref=args.base_ref,
            since_ref=args.since_ref,
        )
        all_rows = runner.flatten_plan(plan)
        selected, missing = runner.select_official_min_rows(plan)

        self.assertEqual(missing, [])
        all_ids = [item["id"] for item in all_rows]
        selected_ids = [item["id"] for item in selected]
        contest_ids = [
            item["id"]
            for item in plan["static_matrix"]["include"]
            if item.get("group") == "Contest"
        ]
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertGreaterEqual(len(all_rows), len(selected))
        self.assertTrue(set(contest_ids).issubset(selected_ids))
        self.assertIn(runner.OFFICIAL_MIN_CORE_ID, selected_ids)

    def test_existing_run_directory_is_rejected(self) -> None:
        with self.assertRaises(runner.OfficialMinError):
            runner._resolve_run_dir(ROOT / "scripts", "contest")

    def test_runner_does_not_follow_log_or_commands_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="official-min-links-") as name:
            root = Path(name)
            (root / "logs").mkdir()
            target = root / "target.txt"
            target.write_text("sentinel\n", encoding="utf-8")
            log_path = root / "logs" / "001-check.log"
            commands_path = root / "commands.jsonl"
            try:
                log_path.symlink_to(target)
                commands_path.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")

            with self.assertRaises(OSError):
                runner._run_one(
                    row("check", "Static"),
                    run_dir=root,
                    environment={},
                    shell="cmd.exe",
                    index=1,
                )
            with self.assertRaises(runner.OfficialMinError):
                runner._write_command_record(commands_path, {"id": "check"})
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")

    def test_completed_run_manifest_is_consumed_and_tamper_is_rejected(self) -> None:
        plan = {
            "static_matrix": {
                "include": [
                    row("check-formatting", "Static", phase="static"),
                    row("run-sync-lint", "Static", phase="static"),
                    row("contest-one", "Contest", phase="static"),
                ]
            },
            "workspace_matrix": {
                "include": [
                    row("run-clippy", "Workspace"),
                    row("test-with-std", "Workspace"),
                ]
            },
            "arceos_matrix": {"include": []},
            "starry_matrix": {"include": []},
            "axvisor_matrix": {
                "include": [row(runner.OFFICIAL_MIN_CORE_ID, "AxVisor")]
            },
        }

        def fake_run_one(
            check: dict,
            *,
            run_dir: Path,
            index: int,
            **_: object,
        ) -> dict:
            relative_log = f"logs/{index:03d}-{check['id']}.log"
            (run_dir / relative_log).write_text(
                f"check_id={check['id']}\n", encoding="utf-8"
            )
            return {
                "id": check["id"],
                "name": check["name"],
                "source": check["source"],
                "command": check["command"],
                "timeout_minutes": check["timeout_minutes"],
                "log": relative_log,
                "started_at": "fixture-start",
                "finished_at": "fixture-finish",
                "duration_seconds": 0.001,
                "status": "passed",
                "exit_code": 0,
            }

        with tempfile.TemporaryDirectory(prefix="official-min-manifest-") as name:
            output_root = Path(name)
            args = runner.parse_args(
                [
                    "--since-ref",
                    "fixture-since",
                    "--output-root",
                    str(output_root),
                    "--run-id",
                    "fixture-run",
                ]
            )
            with patch.object(runner, "build_official_plan", return_value=plan), patch.object(
                runner, "_run_one", side_effect=fake_run_one
            ):
                self.assertEqual(runner.run(args), 0)
            run_dir = output_root / "fixture-run"
            validation = runner.validate_official_min_run(run_dir)
            self.assertTrue(validation["valid"])
            self.assertEqual(validation["status"], "official_min_passed")
            (run_dir / "unindexed-empty-directory").mkdir()
            with self.assertRaises(runner.OfficialMinError):
                runner.validate_official_min_run(run_dir)
            (run_dir / "unindexed-empty-directory").rmdir()
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn(
                "status.json", [entry["path"] for entry in manifest["files"]]
            )
            log_path = run_dir / "logs" / "001-check-formatting.log"
            log_path.write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(runner.OfficialMinError):
                runner.validate_official_min_run(run_dir)
            log_path.write_text("check_id=check-formatting\n", encoding="utf-8")
            status_path = run_dir / "status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["run_id"] = "tampered-run"
            status_path.write_text(
                json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaises(runner.OfficialMinError):
                runner.validate_official_min_run(run_dir)

    def test_failed_run_accounts_for_remaining_checks_as_skipped(self) -> None:
        plan = {
            "static_matrix": {
                "include": [
                    row("check-formatting", "Static", phase="static"),
                    row("run-sync-lint", "Static", phase="static"),
                    row("contest-one", "Contest", phase="static"),
                ]
            },
            "workspace_matrix": {
                "include": [
                    row("run-clippy", "Workspace"),
                    row("test-with-std", "Workspace"),
                ]
            },
            "arceos_matrix": {"include": []},
            "starry_matrix": {"include": []},
            "axvisor_matrix": {
                "include": [row(runner.OFFICIAL_MIN_CORE_ID, "AxVisor")]
            },
        }

        def fake_run_one(
            check: dict,
            *,
            run_dir: Path,
            index: int,
            **_: object,
        ) -> dict:
            relative_log = f"logs/{index:03d}-{check['id']}.log"
            (run_dir / relative_log).write_text(
                f"check_id={check['id']}\n", encoding="utf-8"
            )
            failed = index == 2
            return {
                "id": check["id"],
                "name": check["name"],
                "source": check["source"],
                "command": check["command"],
                "timeout_minutes": check["timeout_minutes"],
                "log": relative_log,
                "started_at": "fixture-start",
                "finished_at": "fixture-finish",
                "duration_seconds": 0.001,
                "status": "failed" if failed else "passed",
                "exit_code": 9 if failed else 0,
            }

        with tempfile.TemporaryDirectory(prefix="official-min-failed-") as name:
            output_root = Path(name)
            args = runner.parse_args(
                [
                    "--since-ref",
                    "fixture-since",
                    "--output-root",
                    str(output_root),
                    "--run-id",
                    "fixture-failed-run",
                ]
            )
            with patch.object(runner, "build_official_plan", return_value=plan), patch.object(
                runner, "_run_one", side_effect=fake_run_one
            ):
                self.assertEqual(runner.run(args), 1)
            run_dir = output_root / "fixture-failed-run"
            validation = runner.validate_official_min_run(run_dir)
            self.assertEqual(validation["status"], "official_min_failed")
            self.assertFalse(validation["success"])
            self.assertEqual(validation["completed_ids"], ["check-formatting"])
            self.assertEqual(
                validation["skipped_ids"],
                [
                    "run-sync-lint",
                    "run-clippy",
                    "test-with-std",
                    "contest-one",
                    runner.OFFICIAL_MIN_CORE_ID,
                ],
            )


if __name__ == "__main__":
    unittest.main()
