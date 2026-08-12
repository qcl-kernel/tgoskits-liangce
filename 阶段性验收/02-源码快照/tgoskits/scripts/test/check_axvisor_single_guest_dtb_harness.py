#!/usr/bin/env python3
"""Static and behavior contracts for the one-Guest QMP capture harness.

The contract intentionally never starts Cargo, QEMU, WSL, or a Unix socket.
It validates deterministic command construction and the fail-closed READY
waiting rule with synthetic log files only.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import uuid
from pathlib import Path
from types import ModuleType


sys.dont_write_bytecode = True
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
HARNESS = WORKSPACE_ROOT / "scripts/contest/run_live_guest_dtb_capture.py"
CAPTURE = WORKSPACE_ROOT / "scripts/contest/capture_live_guest_dtbs.py"
README = WORKSPACE_ROOT / "scripts/contest/README.md"
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunningLauncher:
    pid = 6001

    @staticmethod
    def poll() -> None:
        return None


class ExitedLauncher:
    pid = 6002

    @staticmethod
    def poll() -> int:
        return 23


def expect_harness_error(errors: list[str], module: ModuleType, call, label: str) -> None:
    try:
        call()
    except module.HarnessError:
        return
    except Exception as error:  # noqa: BLE001 - aggregate diagnostic.
        errors.append(f"{label} raises the wrong exception: {error}")
    else:
        errors.append(f"{label} is accepted")


def check_command_contract(errors: list[str], module: ModuleType) -> None:
    nonce = "0123456789abcdef0123456789abcdef"
    try:
        command = module.build_qemu_command(
            cargo_bin="cargo",
            build_config=Path("/work/board.toml"),
            qemu_config=Path("/work/qemu.toml"),
            vmconfig=Path("/work/vm1.toml"),
            qmp_socket=Path("/run/user/1000/qmp.sock"),
            qemu_pidfile=Path("/run/user/1000/qemu.pid"),
            qemu_name=f"axvisor-guest-dtb-{nonce}",
            rootfs=Path("/work/rootfs.img"),
        )
    except Exception as error:  # noqa: BLE001 - aggregate diagnostic.
        errors.append(f"valid one-Guest launch command is rejected: {error}")
        return
    expected_pairs = {
        "--qmp-socket": "/run/user/1000/qmp.sock",
        "--qemu-pidfile": "/run/user/1000/qemu.pid",
        "--qemu-name": f"axvisor-guest-dtb-{nonce}",
    }
    pairs_are_exact = all(
        option in command
        and command[command.index(option) + 1].replace("\\", "/") == value
        for option, value in expected_pairs.items()
    )
    if command[:3] != ["cargo", "xtask", "qemu"] or not pairs_are_exact:
        errors.append("one-Guest launch command omits identity-bound xtask arguments")
    if command[-2:] != ["--rootfs", "\\work\\rootfs.img"] and command[-2:] != [
        "--rootfs",
        "/work/rootfs.img",
    ]:
        errors.append("one-Guest launch command does not forward --rootfs exactly")
    expect_harness_error(
        errors,
        module,
        lambda: module.build_qemu_command(
            cargo_bin="/tmp/attacker-cargo",
            build_config=Path("/work/board.toml"),
            qemu_config=Path("/work/qemu.toml"),
            vmconfig=Path("/work/vm1.toml"),
            qmp_socket=Path("/run/user/1000/qmp.sock"),
            qemu_pidfile=Path("/run/user/1000/qemu.pid"),
            qemu_name=f"axvisor-guest-dtb-{nonce}",
        ),
        "cargo binary path injection",
    )


def check_runtime_and_group_contract(errors: list[str], module: ModuleType) -> None:
    expect_harness_error(
        errors,
        module,
        lambda: module._socket_path_within_limit(Path("/tmp") / ("x" * 101)),
        "overlong QMP socket path",
    )

    class GroupProbe:
        def __init__(self, gone_states: list[bool]) -> None:
            self.gone_states = list(gone_states)
            self.signals: list[int] = []

        def __call__(self, _pgid: int, signum: int) -> None:
            self.signals.append(signum)
            if signum == 0 and self.gone_states.pop(0):
                raise ProcessLookupError()

    class Clock:
        value = 0.0

        def now(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += seconds

    already_exited = GroupProbe([True])
    result = module._cleanup_owned_group(
        pgid=7001,
        timeout_seconds=1.0,
        killpg=already_exited,
        now=Clock().now,
        sleep=Clock().sleep,
    )
    if result != {
        "pgid": 7001,
        "signalSent": "none-already-exited",
        "groupExited": True,
        "escalated": False,
    }:
        errors.append("exited launcher PGID is not independently proven clean")

    term_probe = GroupProbe([False, True])
    clock = Clock()
    reap_calls: list[str] = []
    result = module._cleanup_owned_group(
        pgid=7002,
        timeout_seconds=1.0,
        killpg=term_probe,
        reap_launcher=lambda: reap_calls.append("poll"),
        now=clock.now,
        sleep=clock.sleep,
    )
    if result.get("signalSent") != "SIGTERM" or result.get("escalated") or not result.get("groupExited"):
        errors.append("SIGTERM group cleanup does not wait for complete group exit")
    if not reap_calls:
        errors.append("process-group cleanup does not reap the launcher while waiting")

    kill_probe = GroupProbe([False, False, False, True])
    clock = Clock()
    result = module._cleanup_owned_group(
        pgid=7003,
        timeout_seconds=0.05,
        killpg=kill_probe,
        now=clock.now,
        sleep=clock.sleep,
    )
    if not result.get("escalated") or result.get("signalSent") != "SIGTERM" or not result.get("groupExited"):
        errors.append("SIGTERM-to-SIGKILL cleanup does not prove final group exit")
    if module.signal.SIGTERM not in kill_probe.signals or module.SIGKILL not in kill_probe.signals:
        errors.append("group cleanup omits SIGTERM or SIGKILL escalation")


def check_identity_and_status_contract(errors: list[str], module: ModuleType) -> None:
    identity = {"pid": 4242, "startTimeTicks": 88, "cmdlineSha256": "a" * 64}
    nested = {"identity": {"processCheckpoints": {"resumed": dict(identity)}}}
    try:
        module._verify_nested_resume_continuity(
            nested=nested, current_pid=4242, current_identity=identity
        )
    except Exception as error:  # noqa: BLE001 - aggregate diagnostic.
        errors.append(f"matching nested resumed identity is rejected: {error}")
    changed = {"identity": {"processCheckpoints": {"resumed": {**identity, "pid": 17}}}}
    expect_harness_error(
        errors,
        module,
        lambda: module._verify_nested_resume_continuity(
            nested=changed, current_pid=4242, current_identity=identity
        ),
        "nested resumed PID change",
    )
    changed_full = {
        "identity": {"processCheckpoints": {"resumed": {**identity, "startTimeTicks": 89}}}
    }
    try:
        module._verify_nested_resume_continuity(
            nested=changed_full, current_pid=4242, current_identity=identity
        )
    except Exception:  # noqa: BLE001 - nested helper propagates the shared capture error.
        pass
    else:
        errors.append("nested resumed full-identity change is accepted")

    class ReadyPidfdPoll:
        def register(self, _pidfd: int, _events: int) -> None:
            pass

        def poll(self, _timeout_ms: int) -> list[tuple[int, int]]:
            return [(9, 1)]

    try:
        module._wait_for_pidfd_exit(
            pidfd=9, timeout_seconds=1.0, poll_factory=ReadyPidfdPoll
        )
    except Exception as error:  # noqa: BLE001 - aggregate diagnostic.
        errors.append(f"readable pidfd exit is rejected: {error}")

    class StuckPidfdPoll(ReadyPidfdPoll):
        def poll(self, _timeout_ms: int) -> list[tuple[int, int]]:
            return []

    expect_harness_error(
        errors,
        module,
        lambda: module._wait_for_pidfd_exit(
            pidfd=9, timeout_seconds=1.0, poll_factory=StuckPidfdPoll
        ),
        "pidfd exit timeout",
    )
    success = module._status_payload(
        started_at="a", finished_at="b", state={}, success=True, error=None
    )
    failure = module._status_payload(
        started_at="a", finished_at="b", state={}, success=False, error="x"
    )
    if "confirmed group exit" not in success.get("publication", ""):
        errors.append("success status overstates or omits confirmed cleanup publication")
    if "no nested capture-chain finality" not in failure.get("publication", ""):
        errors.append("failure status incorrectly claims nested manifest finality")


def check_host_dtb_contract(errors: list[str], module: ModuleType) -> None:
    """Exercise Host-DTB input/config/process bindings without launching QEMU."""

    # This checkout can be read-only to the Windows Python token while its
    # user-owned project parent remains writable.  Keep the isolated fixture
    # there; it is never QEMU input outside this static test.
    directory = WORKSPACE_ROOT.parent / f".host-dtb-harness-contract-{uuid.uuid4().hex}"
    # On Windows, ``mode=0o700`` can create an ACL that prevents the same
    # Python token reopening children on this mounted workspace.
    directory.mkdir()
    host_dtb = directory / "host.dtb"
    qemu = directory / "qemu.toml"
    try:
        host_dtb.write_bytes(b"host-dtb-v1")
        resolved = host_dtb.resolve(strict=True)

        def write_qemu(args: list[str]) -> None:
            qemu.write_text("args = " + json.dumps(args) + "\n", encoding="utf-8")

        expected_args = ["-machine", "virt", "-dtb", str(resolved), "-nographic"]
        write_qemu(expected_args)
        try:
            artifact = module._bound_host_dtb(host_dtb)
            actual_args = module._validated_qemu_host_dtb_args(qemu, host_dtb=resolved)
        except Exception as error:  # noqa: BLE001 - aggregate diagnostic.
            errors.append(f"valid Host DTB/config binding is rejected: {error}")
            return
        if artifact != {
            "path": str(resolved),
            "size": len(b"host-dtb-v1"),
            "sha256": module._sha256(b"host-dtb-v1"),
        }:
            errors.append("Host DTB artifact omits exact path, size, or hash")
        if actual_args != expected_args:
            errors.append("Host DTB launcher arguments are not retained exactly")

        write_qemu(["-machine", "virt"])
        expect_harness_error(
            errors,
            module,
            lambda: module._validated_qemu_host_dtb_args(qemu, host_dtb=resolved),
            "missing Host DTB option",
        )
        write_qemu(["-dtb", str(resolved), "-dtb", str(resolved)])
        expect_harness_error(
            errors,
            module,
            lambda: module._validated_qemu_host_dtb_args(qemu, host_dtb=resolved),
            "repeated Host DTB option",
        )
        write_qemu(["-dtb", str(directory / "other.dtb")])
        expect_harness_error(
            errors,
            module,
            lambda: module._validated_qemu_host_dtb_args(qemu, host_dtb=resolved),
            "mismatched Host DTB path",
        )

        try:
            module._validate_host_dtb_process_identity(
                {"argv": ["qemu-system-aarch64", "-dtb", str(resolved)]},
                host_dtb=resolved,
            )
        except Exception as error:  # noqa: BLE001 - aggregate diagnostic.
            errors.append(f"exact Host DTB QEMU argv is rejected: {error}")
        expect_harness_error(
            errors,
            module,
            lambda: module._validate_host_dtb_process_identity(
                {"argv": ["qemu-system-aarch64", "-dtb=" + str(resolved)]},
                host_dtb=resolved,
            ),
            "non-pair Host DTB QEMU argv",
        )

        host_dtb.write_bytes(b"host-dtb-v2")
        expect_harness_error(
            errors,
            module,
            lambda: module._verify_bound_host_dtb(resolved, artifact),
            "changed Host DTB bytes before status publication",
        )
        status = module._status_payload(
            started_at="a",
            finished_at="b",
            state={
                "inputArtifacts": {"hostDtb": artifact},
                "launcherQemuArgs": expected_args,
            },
            success=True,
            error=None,
        )
        if status.get("state", {}).get("inputArtifacts", {}).get("hostDtb") != artifact:
            errors.append("status does not retain Host DTB artifact binding")
        if status.get("state", {}).get("launcherQemuArgs", [])[2:4] != ["-dtb", str(resolved)]:
            errors.append("status does not retain the exact Host DTB launcher option pair")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def check_ready_contract(errors: list[str], module: ModuleType) -> None:
    marker = (
        b"AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=40 "
        b"hpa_segments=0x90000000:40\n"
    )
    # Keep synthetic writes within the checked-out workspace; sandboxed Windows
    # Python can deny the per-user Temp directory even for ordinary test files.
    log = Path("synthetic-axvisor-live.log")
    original_reader = module._read_log_snapshot
    try:
        module._read_log_snapshot = lambda _path: b"boot\n" + marker + b"still running\npartial"
        try:
            ready = module.wait_for_unique_ready(
                log_path=log,
                launcher=RunningLauncher(),
                timeout_seconds=1.0,
            )
        except Exception as error:  # noqa: BLE001 - aggregate diagnostic.
            errors.append(f"unique complete VM-1 marker is rejected: {error}")
        else:
            if ready.get("capturePlanGuestIds") != [1]:
                errors.append("ready wait does not bind the exact single Guest id")
            if not isinstance(ready.get("readyPrefixSha256"), str):
                errors.append("ready wait does not record the immutable prefix digest")

        module._read_log_snapshot = lambda _path: b"boot\n" + marker + marker
        expect_harness_error(
            errors,
            module,
            lambda: module.wait_for_unique_ready(
                log_path=log, launcher=RunningLauncher(), timeout_seconds=1.0
            ),
            "duplicate complete READY marker",
        )
        module._read_log_snapshot = lambda _path: b"boot\n"
        expect_harness_error(
            errors,
            module,
            lambda: module.wait_for_unique_ready(
                log_path=log, launcher=ExitedLauncher(), timeout_seconds=1.0
            ),
            "launcher exit before READY marker",
        )
    finally:
        module._read_log_snapshot = original_reader

    class TransientLog:
        def read_bytes(self) -> bytes:
            raise OSError(module.errno.ENODATA, "synthetic DrvFS transient")

    if module._read_log_snapshot(TransientLog()) != b"":
        errors.append("transient ENODATA log read is not deferred to the next poll")


def check_post_resume_marker_contract(errors: list[str], module: ModuleType) -> None:
    marker = b"AXVISOR_STAGE2_HPA_POSTRUN vm=1"
    ready_prefix = b"boot\nAXVISOR_GUEST_DTB_READY vm=1\n"
    ready_hash = module._sha256(ready_prefix)

    for invalid in ("", "two\nlines", "non-ascii-\u96ea", "x" * 257):
        expect_harness_error(
            errors,
            module,
            lambda invalid=invalid: module._validated_post_resume_marker(invalid),
            f"invalid post-resume marker {invalid[:12]!r}",
        )
    if module._validated_post_resume_marker(None) is not None:
        errors.append("omitted post-resume marker is not backward-compatible")

    class Clock:
        value = 0.0

        def now(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += seconds

    original_reader = module._read_log_snapshot
    try:
        snapshots = iter(
            [ready_prefix, ready_prefix + b"vcpu resumed\n" + marker + b"\n"]
        )
        last = ready_prefix

        def read_snapshot(_path: Path) -> bytes:
            nonlocal last
            last = next(snapshots, last)
            return last

        module._read_log_snapshot = read_snapshot
        clock = Clock()
        try:
            observed = module.wait_for_unique_post_resume_marker(
                log_path=Path("synthetic-axvisor-live.log"),
                launcher=RunningLauncher(),
                ready_prefix_bytes=len(ready_prefix),
                ready_prefix_sha256=ready_hash,
                marker=marker,
                timeout_seconds=1.0,
                now=clock.now,
                sleep=clock.sleep,
            )
        except Exception as error:  # noqa: BLE001 - aggregate diagnostic.
            errors.append(f"unique post-resume marker is rejected: {error}")
        else:
            expected_offset = last.index(marker)
            if observed.get("byteOffset") != expected_offset:
                errors.append("post-resume marker byte offset is not exact")
            if observed.get("observedPrefixBytes") != len(last):
                errors.append("post-resume observation omits observed prefix length")
            if observed.get("observedPrefixSha256") != module._sha256(last):
                errors.append("post-resume observation omits observed prefix hash")
            status = module._status_payload(
                started_at="a",
                finished_at="b",
                state={"postResumeMarker": observed},
                success=True,
                error=None,
            )
            if status.get("state", {}).get("postResumeMarker") != observed:
                errors.append("success status drops the post-resume marker observation")

        expect_harness_error(
            errors,
            module,
            lambda: module._post_resume_marker_observation(
                data=ready_prefix + marker + b"/" + marker,
                ready_prefix_bytes=len(ready_prefix),
                ready_prefix_sha256=ready_hash,
                marker=marker,
            ),
            "duplicate post-resume marker",
        )
        expect_harness_error(
            errors,
            module,
            lambda: module._post_resume_marker_observation(
                data=ready_prefix + b"AAAA",
                ready_prefix_bytes=len(ready_prefix),
                ready_prefix_sha256=ready_hash,
                marker=b"AAA",
            ),
            "overlapping duplicate post-resume marker",
        )
        ready_with_marker = ready_prefix + marker
        expect_harness_error(
            errors,
            module,
            lambda: module._post_resume_marker_observation(
                data=ready_with_marker,
                ready_prefix_bytes=len(ready_with_marker),
                ready_prefix_sha256=module._sha256(ready_with_marker),
                marker=marker,
            ),
            "marker present in READY prefix",
        )

        module._read_log_snapshot = lambda _path: ready_prefix
        clock = Clock()
        expect_harness_error(
            errors,
            module,
            lambda: module.wait_for_unique_post_resume_marker(
                log_path=Path("synthetic-axvisor-live.log"),
                launcher=RunningLauncher(),
                ready_prefix_bytes=len(ready_prefix),
                ready_prefix_sha256=ready_hash,
                marker=marker,
                timeout_seconds=0.1,
                process_exited=lambda: True,
                now=clock.now,
                sleep=clock.sleep,
            ),
            "QEMU exit before post-resume marker",
        )
        clock = Clock()
        expect_harness_error(
            errors,
            module,
            lambda: module.wait_for_unique_post_resume_marker(
                log_path=Path("synthetic-axvisor-live.log"),
                launcher=RunningLauncher(),
                ready_prefix_bytes=len(ready_prefix),
                ready_prefix_sha256=ready_hash,
                marker=marker,
                timeout_seconds=0.1,
                now=clock.now,
                sleep=clock.sleep,
            ),
            "post-resume marker timeout",
        )
    finally:
        module._read_log_snapshot = original_reader


def main() -> int:
    errors: list[str] = []
    if not HARNESS.is_file():
        errors.append("single-Guest live-capture harness is missing")
    else:
        source = HARNESS.read_text(encoding="utf-8")
        if len(source.splitlines()) >= 1200:
            errors.append("single-Guest harness exceeds 1200 production lines")
        for token in (
            "start_new_session=True",
            "wait_for_unique_ready",
            "expected_vm_ids={READY_VM_ID}",
            "run_live_capture(",
            "_bound_running_identity",
            "os.pidfd_open",
            "signal.pidfd_send_signal",
            "_wait_for_pidfd_exit",
            "_verify_nested_resume_continuity",
            "_stable_process(resumed, current_identity, checkpoint=\"nested capture resume\")",
            "inputArtifacts",
            "sha256",
            '"size"',
            "_cleanup_owned_group",
            "SIGKILL",
            "--runtime-dir",
            "--rootfs",
            "--host-dtb",
            "_bound_host_dtb",
            "_validated_qemu_host_dtb_args",
            "_validate_host_dtb_process_identity",
            "launcherQemuArgs",
            "--post-resume-marker",
            "--post-resume-timeout-seconds",
            "wait_for_unique_post_resume_marker",
            "exact-byte-substring-observation-only",
            '"capture-chain.json"',
            '"status.json"',
            '"published after attempted failure cleanup; no nested capture-chain finality is claimed"',
        ):
            if token not in source:
                errors.append(f"single-Guest harness omits `{token}`")
        ready_index = source.find('state["ready"] = wait_for_unique_ready(')
        capture_index = source.find("run_live_capture(")
        identity_index = source.find("pidfd, pid, post_capture_identity = _open_verified_pidfd(")
        marker_index = source.find(
            'state["postResumeMarker"] = wait_for_unique_post_resume_marker('
        )
        exit_index = source.find("signal.pidfd_send_signal(pidfd, signal.SIGINT)")
        status_index = source.find("_publish_json_new(status, status_path)")
        if not 0 <= ready_index < capture_index < identity_index < marker_index < exit_index < status_index:
            errors.append(
                "harness does not preserve READY/capture/resume/identity/marker/exit/status order"
            )
        try:
            module = load_module("single_guest_dtb_harness", HARNESS)
        except Exception as error:  # noqa: BLE001 - aggregate diagnostic.
            errors.append(f"single-Guest harness cannot be imported: {error}")
        else:
            check_command_contract(errors, module)
            check_ready_contract(errors, module)
            check_runtime_and_group_contract(errors, module)
            check_identity_and_status_contract(errors, module)
            check_host_dtb_contract(errors, module)
            check_post_resume_marker_contract(errors, module)

    capture_source = CAPTURE.read_text(encoding="utf-8")
    if "identity_bound_live_qmp_capture_completed" not in capture_source:
        errors.append("nested capture helper no longer exposes its bounded chain status")
    readme = README.read_text(encoding="utf-8")
    for token in (
        "run_live_guest_dtb_capture.py",
        "single-Guest",
        "status.json",
        "capture-chain.json",
        "setup_qemu.sh arceos",
        "qemu-aarch64-zephyr-smoke.toml",
    ):
        if token not in readme:
            errors.append(f"single-Guest harness README guidance omits `{token}`")
    if "python3 scripts/test/check_axvisor_single_guest_dtb_harness.py" not in CI.read_text(
        encoding="utf-8"
    ):
        errors.append("single-Guest harness contract is not wired into CI")

    if not errors:
        return 0
    print("AxVisor single-Guest live-capture harness contract failed:")
    for error in errors:
        print(f"  - {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
