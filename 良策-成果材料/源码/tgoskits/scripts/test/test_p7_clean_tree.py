#!/usr/bin/env python3
"""Host-only P7 clean-tree and manifest self-hash tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DELIVERY_DIR = ROOT / "scripts" / "contest" / "delivery"
sys.path.insert(0, str(DELIVERY_DIR))

import clean_tree  # noqa: E402
import delivery_contract  # noqa: E402
import validate_delivery  # noqa: E402


class P7CleanTreeTests(unittest.TestCase):
    def test_clean_tree_report_requires_exact_head_and_no_dirty_paths(self) -> None:
        revision = "a" * 40
        report = clean_tree.build_report(
            repository=ROOT,
            source_revision=revision,
            base_revision="b" * 40,
            head=revision,
            porcelain_status="",
        )
        self.assertTrue(report["clean"])
        self.assertEqual(report["status"], "clean_tree_verified")
        self.assertTrue(report["base_is_ancestor"])
        not_ancestor = clean_tree.build_report(
            repository=ROOT,
            source_revision=revision,
            base_revision="b" * 40,
            head=revision,
            porcelain_status="",
            base_is_ancestor=False,
        )
        self.assertFalse(not_ancestor["clean"])
        dirty = clean_tree.build_report(
            repository=ROOT,
            source_revision=revision,
            base_revision="b" * 40,
            head=revision,
            porcelain_status=" M scripts/example.py\n?? secret.key",
        )
        self.assertFalse(dirty["clean"])
        self.assertEqual(len(dirty["dirty_paths"]), 2)

    def test_invalid_revision_is_fail_closed(self) -> None:
        with self.assertRaises(clean_tree.CleanTreeError):
            clean_tree.build_report(
                repository=ROOT,
                source_revision="not-a-revision",
                base_revision="b" * 40,
                head="a" * 40,
                porcelain_status="",
            )

    def test_delivery_rejects_symlink_source_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-root-link-") as name:
            root = Path(name)
            source = root / "source"
            source.mkdir()
            (source / "README.md").write_text("delivery\n", encoding="utf-8")
            linked = root / "linked-source"
            try:
                os.symlink(source, linked, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable on this Windows host")
            with self.assertRaises(delivery_contract.DeliveryError):
                delivery_contract.collect_files(linked)

    def test_git_warnings_are_separate_and_fail_closed(self) -> None:
        revision = "a" * 40
        report = clean_tree.build_report(
            repository=ROOT,
            source_revision=revision,
            base_revision="b" * 40,
            head=revision,
            porcelain_status="",
            git_warnings="warning: could not open directory 'locked/': Permission denied",
        )
        self.assertFalse(report["clean"])
        self.assertEqual(report["dirty_paths"], [])
        self.assertEqual(len(report["git_warnings"]), 1)
        self.assertEqual(report["status"], "clean_tree_failed")

    def test_delivery_manifest_self_hash_and_status_last(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-clean-") as name:
            source = Path(name) / "source"
            source.mkdir()
            (source / "README.md").write_text("delivery\n", encoding="utf-8")
            preflight = delivery_contract.build_preflight(
                source,
                repository="qcl-kernel/tgoskits-liangce",
                source_revision="a" * 40,
                base_revision="b" * 40,
            )
            manifest = dict(preflight)
            manifest["manifestSha256"] = delivery_contract.manifest_digest(preflight)
            self.assertEqual(
                manifest["review_metadata"]["commit_series"]["status"],
                "not_generated",
            )
            self.assertFalse(manifest["review_metadata"]["final_delivery_ready"])
            self.assertEqual(
                delivery_contract.validate_manifest_digest(manifest),
                manifest["manifestSha256"],
            )
            manifest["source_revision"] = "c" * 40
            with self.assertRaises(delivery_contract.DeliveryError):
                delivery_contract.validate_manifest_digest(manifest)

            output = Path(name) / "review"
            clean_tree.write_report(
                clean_tree.build_report(
                    repository=source,
                    source_revision="a" * 40,
                    base_revision="b" * 40,
                    head="a" * 40,
                    porcelain_status="",
                ),
                output,
            )
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertTrue(status["success"])
            self.assertTrue(status["statusLast"])
            self.assertFalse(status["pushPerformed"])
            self.assertEqual(status["prPolicy"], "forbidden")

    def test_delivery_cli_requires_clean_source_binding_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-delivery-") as name:
            root = Path(name)
            source = root / "source"
            source.mkdir()
            (source / "README.md").write_text("delivery\n", encoding="utf-8")
            output = root / "review"
            source_report = {
                "clean": True,
                "status": "clean_tree_verified",
                "dirty_paths": [],
                "git_warnings": [],
                "head": "a" * 40,
                "base_is_ancestor": True,
            }
            with mock.patch.object(
                validate_delivery.clean_tree,
                "read_repository_report",
                return_value=source_report,
            ):
                self.assertEqual(
                    validate_delivery.main(
                        [
                            "--source-root",
                            str(source),
                            "--source-repository",
                            str(root),
                            "--output-dir",
                            str(output),
                            "--repository",
                            "qcl-kernel/tgoskits-liangce",
                            "--source-revision",
                            "a" * 40,
                            "--base-revision",
                            "b" * 40,
                            "--dry-run",
                        ]
                    ),
                    0,
                )
            manifest = json.loads((output / "review-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["head"], "a" * 40)
            self.assertEqual(
                delivery_contract.validate_manifest_digest(manifest),
                manifest["manifestSha256"],
            )
            self.assertTrue(delivery_contract.validate_review_bundle(output)["success"])
            (output / "unindexed.txt").write_text("drift\n", encoding="utf-8")
            with self.assertRaises(delivery_contract.DeliveryError):
                delivery_contract.validate_review_bundle(output)

    def test_delivery_cli_rejects_missing_dry_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-delivery-no-dry-") as name:
            root = Path(name)
            source = root / "source"
            source.mkdir()
            (source / "README.md").write_text("delivery\n", encoding="utf-8")
            with mock.patch.object(
                validate_delivery.clean_tree,
                "read_repository_report",
                return_value={
                    "clean": True,
                    "status": "clean_tree_verified",
                    "dirty_paths": [],
                    "git_warnings": [],
                    "head": "a" * 40,
                    "base_is_ancestor": True,
                },
            ):
                self.assertEqual(
                    validate_delivery.main(
                        [
                            "--source-root",
                            str(source),
                            "--source-repository",
                            str(root),
                            "--output-dir",
                            str(root / "review"),
                            "--repository",
                            "qcl-kernel/tgoskits-liangce",
                            "--source-revision",
                            "a" * 40,
                            "--base-revision",
                            "b" * 40,
                        ]
                    ),
                    1,
                )

    def test_review_manifest_writer_rejects_broken_symlink_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p7-delivery-output-link-") as name:
            root = Path(name)
            link = root / "review-manifest.json"
            try:
                link.symlink_to(root / "missing-target.json")
            except (OSError, NotImplementedError):
                self.skipTest("file symlinks are unavailable on this Windows host")
            with self.assertRaises(delivery_contract.DeliveryError):
                validate_delivery._write(link, {"safe": True})


if __name__ == "__main__":
    unittest.main()
