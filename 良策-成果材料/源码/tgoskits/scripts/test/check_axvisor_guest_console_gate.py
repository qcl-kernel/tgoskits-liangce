#!/usr/bin/env python3
"""Behavioral contract for the framed VM-1 console gate module."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/contest"))
import guest_console_gate as gate  # noqa: E402


BOOT_ID = "console-20260813T000000Z-01234567"


def _frame(vm: int, name: str, seq: int, payload: str, *, gen: int = 0, dropped: int = 0, dma: int = 0) -> bytes:
    data = payload.encode("utf-8")
    return (
        f"AXVISOR_GUEST_CONSOLE_FRAME v={gate.FRAME_VERSION if hasattr(gate, 'FRAME_VERSION') else 1} "
        f"vm={vm} name={name} gen={gen} seq={seq} len={len(data)} total={len(data)} "
        f"dropped={dropped} dma={dma} hex={data.hex()}\n".encode("ascii")
    )


FRAME_VERSION = 1


def _make_log(frames: list[bytes], *, prefix: bytes = b"") -> bytes:
    return prefix + b"".join(frames)


class RunningLauncher:
    pid = 7001

    @staticmethod
    def poll() -> None:
        return None


class ExitedLauncher:
    pid = 7002

    @staticmethod
    def poll() -> int:
        return 42


def _ready_payload(boot_id: str) -> list[str]:
    return [
        f"AXVISOR_LINUX_INIT_ENTER vm=1 boot_id={boot_id}\n",
        f"AXVISOR_LINUX_DEV_CONSOLE_READY vm=1 boot_id={boot_id} device=/dev/console cpus=2\n",
        f"AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id={boot_id}\n",
    ]


def _wait(log: Path, launcher, *, boot_id: str = BOOT_ID, prefix: bytes = b"ready-prefix\n", timeout: float = 5.0):
    prefix_hash = hashlib.sha256(prefix).hexdigest()
    return gate.wait_for_framed_guest_ready(
        log_path=log,
        launcher=launcher,
        ready_prefix_bytes=len(prefix),
        ready_prefix_sha256=prefix_hash,
        boot_id=boot_id,
        timeout_seconds=timeout,
    )


def main() -> int:
    errors: list[str] = []
    contract_root = ROOT / "target" / "contract-tests"
    contract_root.mkdir(parents=True, exist_ok=True)
    directory = contract_root / f".console-gate-contract-{os.getpid()}-{secrets.token_hex(8)}"
    directory.mkdir()
    try:
        log = directory / "host.log"
        prefix = b"AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=7536\n"

        payloads = _ready_payload(BOOT_ID)
        frames = [
            _frame(1, "linux", 0, payloads[0]),
            _frame(1, "linux", 1, "some intermediate console output"),
            _frame(1, "linux", 2, payloads[1]),
            _frame(1, "linux", 3, payloads[2]),
        ]
        log.write_bytes(_make_log(frames, prefix=prefix))
        try:
            observation = _wait(log, RunningLauncher(), prefix=prefix)
        except gate.GuestConsoleGateError as error:
            errors.append(f"valid framed markers were rejected: {error}")
        else:
            if observation.get("bootId") != BOOT_ID or observation.get("vmId") != 1 or observation.get("name") != "linux":
                errors.append("gate observation does not bind the expected VM identity")
            names = [item["name"] for item in observation.get("markers", [])]
            if names != [marker.decode("ascii") for marker in gate.GUEST_CONSOLE_MARKERS]:
                errors.append("gate observation marker order is wrong")

        linked_log = directory / "linked-host.log"
        try:
            linked_log.symlink_to(log.name)
        except (OSError, NotImplementedError):
            # Windows developer hosts may not grant symlink creation to the
            # current token; Linux CI still exercises this fail-closed case.
            pass
        else:
            try:
                _wait(linked_log, RunningLauncher(), prefix=prefix)
            except gate.GuestConsoleGateError:
                pass
            else:
                errors.append("gate follows a symlinked AxVisor log")

        per_char = directory / "per-char.log"
        payloads = _ready_payload(BOOT_ID)
        frames = []
        seq = 0
        for payload in payloads:
            for char in payload:
                frames.append(_frame(1, "linux", seq, char))
                seq += 1
        frames.append(_frame(1, "linux", seq, "trailing console line\n"))
        per_char.write_bytes(_make_log(frames, prefix=prefix))
        try:
            observation = _wait(per_char, RunningLauncher(), prefix=prefix)
        except gate.GuestConsoleGateError as error:
            errors.append(f"gate rejects a per-character frame stream: {error}")
        else:
            names = [item["name"] for item in observation.get("markers", [])]
            if names != [marker.decode("ascii") for marker in gate.GUEST_CONSOLE_MARKERS]:
                errors.append("gate does not reassemble markers across per-character frames")

        dual = directory / "dual.log"
        linux_ready = f"AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id={BOOT_ID}\n"
        zephyr_ready = f"AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id={BOOT_ID}\n"
        dual_frames = []
        l_seq = 0
        for char in linux_ready:
            dual_frames.append(_frame(1, "linux", l_seq, char))
            l_seq += 1
        z_seq = 0
        for char in zephyr_ready:
            dual_frames.append(_frame(2, "zephyr", z_seq, char))
            z_seq += 1
        dual.write_bytes(_make_log(dual_frames, prefix=prefix))
        try:
            dual_obs = gate.wait_for_dual_guest_ready(
                log_path=dual,
                launcher=RunningLauncher(),
                ready_prefix_bytes=len(prefix),
                ready_prefix_sha256=hashlib.sha256(prefix).hexdigest(),
                boot_id=BOOT_ID,
                timeout_seconds=5.0,
            )
        except gate.GuestConsoleGateError as error:
            errors.append(f"gate rejects a valid dual-guest marker stream: {error}")
        else:
            if sorted(dual_obs.get("guests", {})) != [1, 2]:
                errors.append("gate dual observation lacks both VM identities")
            if dual_obs.get("guests", {}).get(1, {}).get("name") != "linux" or dual_obs.get("guests", {}).get(2, {}).get("name") != "zephyr":
                errors.append("gate dual observation has the wrong VM names")

        # A real dual run emits the Zephyr READY once at boot, before the
        # QMP-resume prefix is frozen.  The gate must accept that: only the
        # Linux READY is required to appear after the prefix.
        dual_boot = directory / "dual-boot-ready.log"
        boot_frames = []
        z_seq = 0
        for char in zephyr_ready:
            boot_frames.append(_frame(2, "zephyr", z_seq, char))
            z_seq += 1
        l_seq = 0
        for char in linux_ready:
            boot_frames.append(_frame(1, "linux", l_seq, char))
            l_seq += 1
        dual_boot.write_bytes(_make_log(boot_frames, prefix=prefix))
        try:
            dual_obs = gate.wait_for_dual_guest_ready(
                log_path=dual_boot,
                launcher=RunningLauncher(),
                ready_prefix_bytes=len(prefix),
                ready_prefix_sha256=hashlib.sha256(prefix).hexdigest(),
                boot_id=BOOT_ID,
                timeout_seconds=5.0,
            )
        except gate.GuestConsoleGateError as error:
            errors.append(f"gate rejects a boot-time Zephyr READY before the prefix: {error}")
        else:
            if sorted(dual_obs.get("guests", {})) != [1, 2]:
                errors.append("gate boot-ready observation lacks both VM identities")

        # A boot-time Zephyr READY alone must still time out when the Linux
        # READY never appears after the prefix (platform stall case).
        dual_stall = directory / "dual-stall.log"
        stall_frames = []
        z_seq = 0
        for char in zephyr_ready:
            stall_frames.append(_frame(2, "zephyr", z_seq, char))
            z_seq += 1
        dual_stall.write_bytes(_make_log(stall_frames, prefix=prefix))
        try:
            gate.wait_for_dual_guest_ready(
                log_path=dual_stall,
                launcher=RunningLauncher(),
                ready_prefix_bytes=len(prefix),
                ready_prefix_sha256=hashlib.sha256(prefix).hexdigest(),
                boot_id=BOOT_ID,
                timeout_seconds=2.0,
            )
        except gate.GuestConsoleGateError:
            pass
        else:
            errors.append("gate accepts a dual session without a post-prefix Linux READY")

        cross = directory / "cross.log"
        cross_frames = [
            _frame(1, "linux", 0, linux_ready),
            _frame(2, "zephyr", 0, linux_ready),
        ]
        cross.write_bytes(_make_log(cross_frames, prefix=prefix))
        try:
            gate.wait_for_dual_guest_ready(
                log_path=cross,
                launcher=RunningLauncher(),
                ready_prefix_bytes=len(prefix),
                ready_prefix_sha256=hashlib.sha256(prefix).hexdigest(),
                boot_id=BOOT_ID,
                timeout_seconds=5.0,
            )
        except gate.GuestConsoleGateError:
            pass
        else:
            errors.append("gate accepts a VM2 stream carrying the VM1 READY marker")

        wrong_vm = directory / "wrong-vm.log"
        bad_frames = [
            _frame(2, "zephyr", 0, payloads[0]),
        ]
        wrong_vm.write_bytes(_make_log(bad_frames, prefix=prefix))
        try:
            _wait(wrong_vm, RunningLauncher(), prefix=prefix)
        except gate.GuestConsoleGateError:
            pass
        else:
            errors.append("gate accepts a frame from the wrong VM/name")

        dropped = directory / "dropped.log"
        bad_frames = [
            _frame(1, "linux", 0, payloads[0], dropped=16),
        ]
        dropped.write_bytes(_make_log(bad_frames, prefix=prefix))
        try:
            _wait(dropped, RunningLauncher(), prefix=prefix)
        except gate.GuestConsoleGateError:
            pass
        else:
            errors.append("gate accepts a frame with dropped bytes")

        dma = directory / "dma.log"
        bad_frames = [
            _frame(1, "linux", 0, payloads[0], dma=1),
        ]
        dma.write_bytes(_make_log(bad_frames, prefix=prefix))
        try:
            _wait(dma, RunningLauncher(), prefix=prefix)
        except gate.GuestConsoleGateError:
            pass
        else:
            errors.append("gate accepts a frame with a DMA-attempt counter")

        gap = directory / "gap.log"
        bad_frames = [
            _frame(1, "linux", 0, payloads[0]),
            _frame(1, "linux", 2, payloads[1]),
        ]
        gap.write_bytes(_make_log(bad_frames, prefix=prefix))
        try:
            _wait(gap, RunningLauncher(), prefix=prefix)
        except gate.GuestConsoleGateError:
            pass
        else:
            errors.append("gate accepts a frame sequence gap")

        wrong_boot = directory / "wrong-boot.log"
        bad_frames = [
            _frame(1, "linux", 0, f"AXVISOR_LINUX_INIT_ENTER vm=1 boot_id=console-other-00000000-00000000"),
        ]
        wrong_boot.write_bytes(_make_log(bad_frames, prefix=prefix))
        try:
            _wait(wrong_boot, RunningLauncher(), prefix=prefix)
        except gate.GuestConsoleGateError:
            pass
        else:
            errors.append("gate accepts a marker with the wrong boot-id")

        out_of_order = directory / "order.log"
        bad_frames = [
            _frame(1, "linux", 0, payloads[2]),
            _frame(1, "linux", 1, payloads[0]),
            _frame(1, "linux", 2, payloads[1]),
        ]
        out_of_order.write_bytes(_make_log(bad_frames, prefix=prefix))
        try:
            _wait(out_of_order, RunningLauncher(), prefix=prefix)
        except gate.GuestConsoleGateError:
            pass
        else:
            errors.append("gate accepts out-of-order markers")

        fail_marker = directory / "fail.log"
        bad_frames = [
            _frame(1, "linux", 0, f"AXVISOR_LINUX_CONSOLE_FAIL vm=1 boot_id={BOOT_ID} reason=proc-mount"),
        ]
        fail_marker.write_bytes(_make_log(bad_frames, prefix=prefix))
        try:
            _wait(fail_marker, RunningLauncher(), prefix=prefix)
        except gate.GuestConsoleGateError:
            pass
        else:
            errors.append("gate accepts a console FAIL marker")

        premature = directory / "premature.log"
        premature.write_bytes(_make_log([], prefix=prefix))
        try:
            _wait(premature, ExitedLauncher(), prefix=prefix)
        except gate.GuestConsoleGateError as error:
            if "exited" not in str(error):
                errors.append("gate does not fail closed when the launcher exits early")
        else:
            errors.append("gate accepts a launcher exit without markers")

        missing = directory / "missing.log"
        missing.write_bytes(_make_log([_frame(1, "linux", 0, payloads[0])], prefix=prefix))
        try:
            _wait(missing, RunningLauncher(), prefix=prefix, timeout=0.2)
        except gate.GuestConsoleGateError as error:
            if "timed out" not in str(error):
                errors.append("gate timeout has the wrong failure")
        else:
            errors.append("gate completes without all three markers")

        try:
            gate.wait_for_framed_guest_ready(
                log_path=log, launcher=RunningLauncher(),
                ready_prefix_bytes=len(prefix), ready_prefix_sha256="0" * 64,
                boot_id="bad boot id!", timeout_seconds=5.0,
            )
        except gate.GuestConsoleGateError:
            pass
        else:
            errors.append("gate accepts an invalid boot-id")
    finally:
        shutil.rmtree(directory)
    if errors:
        print("Framed console gate contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Framed console gate contract passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
