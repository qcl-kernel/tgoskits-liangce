"""Deterministic host regression for the current-f964 P2 soak publisher."""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "contest"))

from runtime import dual_guest  # noqa: E402


ENTRY_PATH = ROOT / "scripts" / "contest" / "run_dual_guest_smoke.py"
ENTRY_SPEC = importlib.util.spec_from_file_location("p2_dual_guest_entry", ENTRY_PATH)
if ENTRY_SPEC is None or ENTRY_SPEC.loader is None:
    raise RuntimeError(f"cannot load P2 entry point: {ENTRY_PATH}")
ENTRY_MODULE = importlib.util.module_from_spec(ENTRY_SPEC)
ENTRY_SPEC.loader.exec_module(ENTRY_MODULE)


QEMU_CONFIG = ROOT / "configs" / "contest" / "qemu-aarch64-linux-zephyr-p2-dual.toml"
LINUX_CONFIG = ROOT / "os" / "axvisor" / "configs" / "vms" / "qemu" / "aarch64" / "linux-smp2-p2-dual.toml"
ZEPHYR_CONFIG = ROOT / "os" / "axvisor" / "configs" / "vms" / "qemu" / "aarch64" / "zephyr-smp1-p2-dual.toml"

RUN_ID = "p2-soak-host-test"
NONCE = "0123456789abcdef0123456789abcdef"
RAW_LOG = "\n".join(
    [
        "contest dtb evidence: vm=1 gpa=0x80000000 size=0x960 hpa=0x124e00000",
        "contest dtb evidence: vm=2 gpa=0x47e00000 size=0x854 hpa=0x13cc00000",
        f"[VM 1] AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id={RUN_ID}",
        f"[VM 2] AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id={RUN_ID}",
        "[VM 1] [Axvisor VM 1 console dropped 0 buffered bytes]",
        "[VM 2] dropped=0 dma=0",
        "",
    ]
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += max(seconds, 1_800.0)


class CompletedLauncher:
    pid = 4242

    def __init__(self, stdout) -> None:
        self.poll_count = 0
        stdout.write(RAW_LOG.encode("utf-8"))
        stdout.flush()

    def poll(self) -> int | None:
        self.poll_count += 1
        return None if self.poll_count == 1 else 0


class EarlyExitLauncher(CompletedLauncher):
    def poll(self) -> int:
        return 9


class P2DualGuestSoakTests(unittest.TestCase):
    def _spec(self, root: Path, output: Path) -> dual_guest.DualGuestSpec:
        (root / "runtime").mkdir()
        return dual_guest.DualGuestSpec(
            repository=ROOT,
            build_config=QEMU_CONFIG,
            qemu_config=QEMU_CONFIG,
            linux_vmconfig=LINUX_CONFIG,
            zephyr_vmconfig=ZEPHYR_CONFIG,
            run_id=RUN_ID,
            output_dir=output,
            duration_seconds=1_800.0,
            ready_timeout_seconds=5.0,
            shutdown_timeout_seconds=1.0,
            runtime_root=root / "runtime",
        )

    def test_fake_launcher_publishes_and_consumes_current_f964_soak(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2-soak-runner-") as name:
            root = Path(name)
            output = root / "bundle"
            clock = FakeClock()

            result = dual_guest.run_dual_guest(
                self._spec(root, output),
                popen_factory=lambda *args, **kwargs: CompletedLauncher(kwargs["stdout"]),
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )

            self.assertEqual(
                result,
                0,
                json.loads((output / "status.json").read_text(encoding="utf-8")),
            )
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], dual_guest.SOAK_SESSION_STATUS)
            self.assertFalse(status["qualified"])
            self.assertTrue(status["statusLast"])
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], dual_guest.SOAK_SESSION_STATUS)
            self.assertFalse(manifest["qualified"])
            self.assertIsNotNone(manifest["soakSession"])
            session = output / "dual-guest-soak-session.json"
            validation = dual_guest.validate_dual_guest_soak_session(session)
            self.assertTrue(validation["valid"])
            self.assertFalse(validation["qualified"])
            dual_guest.validate_status_last(output)
            (output / "unindexed.txt").write_text("drift\n", encoding="utf-8")
            with self.assertRaises(dual_guest.RuntimeContractError):
                dual_guest.validate_dual_guest_soak_session(session)
            self.assertEqual(list((root / "runtime").iterdir()), [])

    def test_cli_dry_run_selects_soak_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2-soak-cli-") as name:
            root = Path(name)
            output = root / "bundle"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = ENTRY_MODULE.main(
                    [
                        "--repository", str(ROOT),
                        "--build-config", str(QEMU_CONFIG),
                        "--qemu-config", str(QEMU_CONFIG),
                        "--linux-vmconfig", str(LINUX_CONFIG),
                        "--zephyr-vmconfig", str(ZEPHYR_CONFIG),
                        "--run-id", "p2-soak-cli-test",
                        "--duration-seconds", "1800",
                        "--output-dir", str(output),
                        "--dry-run",
                    ]
                )
            self.assertEqual(result, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["mode"], "soak")
            self.assertFalse(payload["qualified"])
            self.assertFalse(output.exists())
            self.assertFalse((root / "runtime").exists())

    def test_soak_consumer_rejects_tampered_log_and_short_window(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2-soak-contract-") as name:
            root = Path(name)
            output = root / "bundle"
            output.mkdir()
            (root / "runtime").mkdir()
            configs = output / "configs"
            logs = output / "logs"
            configs.mkdir()
            logs.mkdir()
            identity = dual_guest.make_runtime_identity(
                nonce=NONCE, runtime_root=root / "runtime"
            )
            resolved_qemu = configs / "qemu.resolved.toml"
            resolved_qemu.write_text(
                dual_guest.inject_qemu_identity_args(
                    QEMU_CONFIG.read_text(encoding="utf-8"),
                    qmp_socket=identity.qmp_socket,
                    qemu_pidfile=identity.qemu_pidfile,
                    qemu_name=identity.qemu_name,
                ),
                encoding="utf-8",
            )
            raw = logs / "axvisor.raw.log"
            raw.write_text(RAW_LOG, encoding="utf-8")
            observation = dual_guest.validate_dual_guest_log(RAW_LOG, run_id=RUN_ID)
            evidence = dual_guest.DualGuestSoakEvidence(
                run_id=RUN_ID,
                identity=identity,
                observation=observation,
                launcher_pid=4242,
                launcher_start_monotonic_ns=1_000_000_000,
                start_monotonic_ns=2_000_000_000,
                end_monotonic_ns=2_000_000_000 + dual_guest.SOAK_MIN_DURATION_NS,
                raw_log=raw,
                resolved_configs={
                    "qemu": resolved_qemu,
                    "linux": LINUX_CONFIG,
                    "zephyr": ZEPHYR_CONFIG,
                },
            )
            dual_guest.publish_dual_guest_soak_session(evidence, output_dir=output)
            session = output / "dual-guest-soak-session.json"

            raw.write_text(RAW_LOG.replace("dropped=0", "dropped=1"), encoding="utf-8")
            with self.assertRaises(dual_guest.RuntimeContractError):
                dual_guest.validate_dual_guest_soak_session(session)

            raw.write_text(RAW_LOG, encoding="utf-8")
            value = json.loads(session.read_text(encoding="utf-8"))
            value["duration_ns"] = dual_guest.SOAK_MIN_DURATION_NS - 1
            session.write_text(json.dumps(value) + "\n", encoding="utf-8")
            with self.assertRaises(dual_guest.RuntimeContractError):
                dual_guest.validate_dual_guest_soak_session(session)

    def test_early_launcher_exit_preserves_runtime_evidence_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2-soak-early-exit-") as name:
            root = Path(name)
            output = root / "bundle"
            runtime_root = root / "runtime"
            result = dual_guest.run_dual_guest(
                self._spec(root, output),
                popen_factory=lambda *args, **kwargs: EarlyExitLauncher(kwargs["stdout"]),
                monotonic=FakeClock().monotonic,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(result, 1)
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["success"])
            self.assertEqual(status["status"], "dual_guest_soak_session_failed")
            cleanup = json.loads((output / "cleanup.json").read_text(encoding="utf-8"))
            self.assertFalse(cleanup["runtimeDirRemoved"])
            self.assertTrue(any(runtime_root.iterdir()))

    def test_missing_runtime_root_is_rejected_before_output_creation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2-soak-runtime-root-") as name:
            root = Path(name)
            output = root / "bundle"
            spec = dataclasses.replace(self._spec(root, output), runtime_root=root / "missing")
            with self.assertRaises(dual_guest.RuntimeContractError):
                dual_guest.run_dual_guest(spec)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
