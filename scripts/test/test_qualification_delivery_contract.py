#!/usr/bin/env python3
"""Host-only negative and status-last tests for the P6/P7 candidates."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
QUALIFICATION_DIR = ROOT / "scripts" / "contest" / "qualification"
DELIVERY_DIR = ROOT / "scripts" / "contest" / "delivery"
sys.path.insert(0, str(QUALIFICATION_DIR))
sys.path.insert(0, str(DELIVERY_DIR))

import delivery_contract  # noqa: E402
import qualification_contract  # noqa: E402
import run_core  # noqa: E402


class QualificationContractTests(unittest.TestCase):
    def test_session_json_is_not_accepted_as_a_run_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p6-contract-") as name:
            session = Path(name) / "session.json"
            session.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(qualification_contract.QualificationError):
                qualification_contract.require_run_directory(session)

    def test_run_directory_rejects_internal_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p6-contract-link-") as name:
            root = Path(name) / "run"
            root.mkdir()
            target = root / "target.txt"
            target.write_text("target\n", encoding="utf-8")
            link = root / "link.txt"
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest("file symlinks are unavailable on this Windows host")
            with self.assertRaises(qualification_contract.QualificationError):
                qualification_contract.require_run_directory(root)

    def test_write_json_rejects_broken_symlink_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p6-contract-output-link-") as name:
            root = Path(name)
            link = root / "result.json"
            try:
                link.symlink_to(root / "missing-target.json")
            except (OSError, NotImplementedError):
                self.skipTest("file symlinks are unavailable on this Windows host")
            with self.assertRaises(qualification_contract.QualificationError):
                qualification_contract.write_json(link, {"safe": True})

    def test_core_preflight_is_blocked_and_status_last(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p6-contract-") as name:
            output = Path(name) / "core"
            run_core.write_core_preflight("p6-core-negative", output)
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["success"])
            self.assertFalse(status["qualified"])
            self.assertEqual(status["status"], "qualification_smoke_completed")
            self.assertTrue(status["statusLast"])


class DeliveryContractTests(unittest.TestCase):
    def test_secret_and_nested_git_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-contract-") as name:
            root = Path(name)
            (root / "README.md").write_text("candidate\n", encoding="utf-8")
            (root / ".env").write_text("TOKEN=not-for-delivery\n", encoding="utf-8")
            nested = root / "vendor" / ".git"
            nested.mkdir(parents=True)
            (nested / "config").write_text("nested\n", encoding="utf-8")
            files, excluded, errors = delivery_contract.collect_files(root)
            self.assertEqual([entry["path"] for entry in files], ["README.md"])
            self.assertTrue(any(item["path"] == ".env" for item in excluded))
            self.assertTrue(any("nested .git" in error for error in errors))

    def test_revision_and_push_contract_are_local_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-contract-") as name:
            root = Path(name)
            (root / "README.md").write_text("candidate\n", encoding="utf-8")
            preflight = delivery_contract.build_preflight(
                root,
                repository="qcl-kernel/tgoskits-liangce",
                source_revision="a" * 40,
                base_revision="b" * 40,
            )
            self.assertEqual(preflight["status"], "delivery_preflight_ready")
            self.assertFalse(preflight["pushPerformed"])
            self.assertEqual(preflight["prPolicy"], "forbidden")
            with self.assertRaises(delivery_contract.DeliveryError):
                delivery_contract.build_preflight(
                    root,
                    repository="qcl-kernel/tgoskits-liangce",
                    source_revision="not-a-revision",
                    base_revision="b" * 40,
                )


if __name__ == "__main__":
    unittest.main()
