#!/usr/bin/env python3
"""Fake-launcher/QMP contract for the bounded DMA-effect runner core."""

from __future__ import annotations
import ast
import errno
import hashlib
import inspect
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/contest"))
import run_virtio_dma_effect_probe as runner  # noqa: E402


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Stdin:
    def __init__(self, log: Path, done: bytes):
        self.log, self.done, self.writes = log, done, []

    def write(self, b: bytes):
        self.writes.append(b)
        self.log.write_bytes(self.log.read_bytes() + self.done)
        return len(b)

    def flush(self):
        pass


class Launcher:
    def __init__(self, log: Path, done: bytes):
        self.stdin = Stdin(log, done)

    def poll(self):
        return None


class Qmp:
    def __init__(self, name: str, payload: bytes, guard: bytes):
        self.name, self.payload, self.guard, self.states = (
            name,
            payload,
            guard,
            ["running", "paused", "running", "paused", "running"],
        )
        self.index = 0
        self.commands = []

    def negotiate(self):
        pass

    def peer_credentials(self):
        return (7, 1000, 0)

    def execute_control(self, c, request_id, arguments=None):
        if c == "query-name":
            return {"return": {"name": self.name}, "id": request_id}
        if c in {"stop", "cont"}:
            self.commands.append(
                {"execute": c, "arguments": arguments or {}, "id": request_id}
            )
            self.index += 1
            return {"return": {}, "id": request_id}
        value = self.states[self.index]
        return {"return": {"status": value}, "id": request_id}

    def execute(self, v):
        self.commands.append(v)
        c = v["execute"]
        if c == "stop":
            self.index += 1
        elif c == "cont":
            self.index += 1
        elif c == "pmemsave":
            a = v["arguments"]
            Path(a["filename"]).write_bytes(
                self.guard if a["val"] == 0x180000000 else self.payload
            )
        return {"return": {}, "id": v["id"]}


class CleanupLauncher:
    returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout):
        return 0


class ActiveLauncher:
    def poll(self):
        return None


class EnodataThenLog:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def exists(self):
        return True

    def read_bytes(self):
        self.calls += 1
        if self.calls == 1:
            raise OSError(errno.ENODATA, "active DrvFS append")
        return self.data


def main() -> int:
    errors = []
    main_tree = ast.parse(inspect.getsource(runner.main))
    primary_initializers = [
        node
        for node in ast.walk(main_tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "primary_error"
        and isinstance(node.value, ast.Constant)
        and node.value.value is None
    ]
    if len(primary_initializers) != 1:
        errors.append(
            "main does not initialize primary_error exactly once before cleanup"
        )
    if "primary_error" in inspect.getsource(runner._marker):
        errors.append("marker parser unexpectedly owns runner cleanup error state")
    main_source = inspect.getsource(runner.main)
    for input_name in ("kernelPrerequisiteManifest", "guestKernel"):
        if input_name not in main_source:
            errors.append(
                f"runner does not retain {input_name} as an immutable launch input"
            )
    d = ROOT / "results" / f".run-dma-{os.getpid()}-{secrets.token_hex(4)}"
    d.mkdir(parents=True)
    try:
        kernel = d / "guest.Image"
        kernel_bytes = b"guest-kernel-image"
        kernel.write_bytes(kernel_bytes)
        vmconfig = d / "linux.toml"
        vmconfig.write_text(
            "[kernel]\n"
            'image_location = "memory"\n'
            f"kernel_path = {json.dumps(str(kernel))}\n",
            encoding="utf-8",
        )
        manifest = d / "guest-kernel-virtio-console.json"
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "artifactStatus": "preflight-only",
                    "status": runner.KERNEL_PREREQ_STATUS,
                    "kernel": {
                        "path": kernel.name,
                        "size": len(kernel_bytes),
                        "sha256": sha(kernel_bytes),
                    },
                    "embeddedConfig": {
                        "requiredBuiltIns": {
                            item: "y" for item in runner.KERNEL_REQUIRED_BUILT_INS
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        try:
            bound_manifest, bound_vmconfig, bound_path, bound_kernel = (
                runner._require_kernel_prerequisites(
                    manifest_path=manifest, vmconfig_path=vmconfig
                )
            )
        except runner.EffectRunError as error:
            errors.append(f"valid kernel prerequisite contract was rejected: {error}")
        else:
            if (
                bound_manifest != manifest.read_bytes()
                or bound_vmconfig != vmconfig.read_bytes()
                or bound_path != kernel
                or bound_kernel != kernel_bytes
            ):
                errors.append(
                    "kernel prerequisite binding does not preserve exact inputs"
                )
        manifest.write_bytes(b'{"schemaVersion":1,"schemaVersion":1}')
        try:
            runner._require_kernel_prerequisites(
                manifest_path=manifest, vmconfig_path=vmconfig
            )
        except runner.EffectRunError as error:
            if "duplicate JSON member" not in str(error):
                errors.append("duplicate kernel manifest member has the wrong failure")
        else:
            errors.append("runner accepts a duplicate-member kernel manifest")
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "artifactStatus": "preflight-only",
                    "status": runner.KERNEL_PREREQ_STATUS,
                    "kernel": {
                        "path": kernel.name,
                        "size": len(kernel_bytes),
                        "sha256": "0" * 64,
                    },
                    "embeddedConfig": {
                        "requiredBuiltIns": {
                            item: "y" for item in runner.KERNEL_REQUIRED_BUILT_INS
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        try:
            runner._require_kernel_prerequisites(
                manifest_path=manifest, vmconfig_path=vmconfig
            )
        except runner.EffectRunError as error:
            if "does not bind" not in str(error):
                errors.append("kernel hash mismatch has the wrong failure")
        else:
            errors.append("runner accepts a kernel manifest with a mismatched hash")
        argv = [
            "--repository",
            "/repo",
            "--axvisor-dir",
            "/repo/os/axvisor",
            "--build-config",
            "/build.toml",
            "--qemu-config",
            "/qemu.toml",
            "--vmconfig",
            "/vm.toml",
            "--rootfs",
            "/rootfs.ext4",
            "--rootfs-plan",
            "/rootfs.json",
            "--qemu-plan",
            "/qemu.json",
            "--expected-sector",
            "/probe.raw",
            "--host-dtb",
            "/host.dtb",
            "--kernel-prereq-manifest",
            "/kernel.json",
            "--evidence-dir",
            "/out",
            "--guard-hpa",
            "0x180000000",
            "--guard-size",
            "4096",
            "--nonce",
            "0123456789abcdef0123456789abcdef",
        ]
        if runner.parse_args(argv).kernel_prereq_manifest != Path("/kernel.json"):
            errors.append("runner CLI does not require kernel prerequisite manifest")
        nonce = "0123456789abcdef0123456789abcdef"
        payload = b"P" * 512
        guard = b"G" * 4096
        name = "axvisor-dma-effect-" + nonce
        ready = f"AXVISOR_VIRTIO_BLK_ODIRECT_READY nonce={nonce} gpa=0x80002000 size=512 sector=0 device=/dev/vdb control_device=/dev/hvc0".encode()
        done = f"AXVISOR_VIRTIO_BLK_ODIRECT_DONE nonce={nonce} bytes=512 gpa=0x80002000".encode()
        log = d / "axvisor.log"
        log.write_bytes(
            b"AXVISOR_GUEST_DTB_READY vm=1 gpa=0x80000000 size=64 hpa_segments=0x80000000:64\n"
            + ready
            + b"\n"
        )
        device = {
            "kind": "virtio-blk-device",
            "id": "dma-probe",
            "bus": "virtio-mmio-bus.1",
            "driveId": "dma-probe-disk",
            "guestPath": "/dev/vdb",
        }
        region = {
            "gpa": 0x80000000,
            "hpa": 0x80000000,
            "size": 0x4000000,
            "flags": 7,
            "mapType": 2,
            "identity": True,
        }
        ranges = {"gpa": 0x80002000, "hpa": 0x80002000, "size": 512}
        guardr = {"hpa": 0x180000000, "size": 4096}
        temp = [
            ("before", "guard", guardr, "before-guard.bin"),
            ("before", "payload", ranges, "before-payload.bin"),
            ("after", "guard", guardr, "after-guard.bin"),
            ("after", "payload", ranges, "after-payload.bin"),
        ]
        req = {
            "sessionNonce": nonce,
            "qemuName": name,
            "device": device,
            "control": {
                "kind": "virtio-console",
                "chardevId": "dma-go-chardev",
                "serialDeviceId": "dma-go-serial",
                "serialBus": "virtio-mmio-bus.2",
                "portId": "dma-go-port",
                "portName": "dma-go",
                "guestPath": "/dev/hvc0",
                "socketPath": f"/tmp/axdma-{nonce}/control.sock",
            },
            "request": {
                "sector": 0,
                "payload": ranges,
                "boundMapReservedRegion": region,
            },
            "guestMarkers": {"ready": ready.decode(), "done": done.decode()},
            "qmpCaptureTemplate": {
                "measurements": [
                    {
                        "phase": p,
                        "target": t,
                        "hpa": r["hpa"],
                        "size": r["size"],
                        "outputFileName": n,
                    }
                    for p, t, r, n in temp
                ]
            },
        }
        (d / "request.json").write_bytes(json.dumps(req).encode())
        q = Qmp(name, payload, guard)
        launch = Launcher(log, b"later\n" + done + b"\n")
        sent = []

        class ControlConnection:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        control_connection = ControlConnection()

        def control_sender(**kwargs):
            sent.append(kwargs)
            launch.stdin.write(kwargs["command"])
            return {"pid": 7, "uid": 1000}, control_connection

        value = runner.execute_effect_session(
            session=q,
            launcher=launch,
            log_path=log,
            request=req,
            output=d,
            timeout=1,
            control_sender=control_sender,
        )
        frozen = (d / "observed-log-prefix.bin").read_bytes()
        if frozen != log.read_bytes() or not frozen.endswith(done + b"\n"):
            errors.append("runner does not seal exactly the DONE-complete log prefix")
        log.write_bytes(log.read_bytes() + b"shutdown tail\n")
        if (d / "observed-log-prefix.bin").read_bytes() != frozen:
            errors.append("frozen observed log changes after simulated shutdown append")
        if [item["command"] for item in sent] != [f"GO {nonce}\n".encode()]:
            errors.append(
                "runner did not send exactly one virtio-console GO after before capture"
            )
        if not control_connection.closed:
            errors.append(
                "runner does not keep and then close the control socket through DONE"
            )
        if [x["execute"] for x in q.commands] != [
            "stop",
            "pmemsave",
            "pmemsave",
            "cont",
            "stop",
            "pmemsave",
            "pmemsave",
            "cont",
        ]:
            errors.append("runner QMP state machine has forbidden/reordered command")
        if value["qmp"]["states"] != [
            "running",
            "paused",
            "running",
            "paused",
            "running",
        ]:
            errors.append("session does not bind two pause windows")
        # Zombie-only PGID is no longer a live process group, but remains
        # explicit cleanup evidence; a non-zombie peer would fail closed.
        proc = d / "proc"
        (proc / "42").mkdir(parents=True)
        (proc / "42" / "stat").write_text("42 (qemu) Z 1 99 99 0\n", encoding="ascii")
        killed = []
        cleanup = runner._cleanup_group(
            pgid=99,
            launcher=CleanupLauncher(),
            pidfd=None,
            proc_root=proc,
            killpg=lambda pgid, signo: killed.append((pgid, signo)),
        )
        if (
            cleanup.get("zombiePids") != [42]
            or cleanup.get("noLiveMembers") is not True
            or cleanup.get("groupGone") is not False
        ):
            errors.append("cleanup does not classify zombie-only PGID evidence")
        active_log = EnodataThenLog(log.read_bytes())
        tick_values = iter([0.0, 0.0, 0.1])
        try:
            runner._wait_log(
                active_log,
                ActiveLauncher(),
                want_ready=True,
                timeout=1,
                now=lambda: next(tick_values),
                sleep=lambda _: None,
            )
        except Exception as error:
            errors.append(f"active ENODATA retry regressed: {error}")
        live_proc = d / "proc-live"
        (live_proc / "43").mkdir(parents=True)
        stat_path = live_proc / "43" / "stat"
        stat_path.write_text("43 (qemu) R 1 100 100 0\n", encoding="ascii")
        poll_ticks = iter([0.0, 0.0, 0.0, 0.0, 0.1, 0.2])
        try:
            delayed = runner._cleanup_group(
                pgid=100,
                launcher=CleanupLauncher(),
                pidfd=None,
                proc_root=live_proc,
                killpg=lambda *_: None,
                now=lambda: next(poll_ticks),
                sleep=lambda _: stat_path.write_text(
                    "43 (qemu) Z 1 100 100 0\n", encoding="ascii"
                ),
            )
            if (
                delayed.get("reapPolls", 0) < 1
                or delayed.get("noLiveMembers") is not True
            ):
                errors.append("SIGKILL reap polling did not wait for no-live-members")
        except Exception as error:
            errors.append(f"SIGKILL reap polling regressed: {error}")
        # A malformed READY must fail before any QMP command/GO.
        log.write_bytes(b"bad\n")
        q2 = Qmp(name, payload, guard)
        l2 = Launcher(log, b"")
        try:
            runner.execute_effect_session(
                session=q2,
                launcher=l2,
                log_path=log,
                request=req,
                output=d,
                timeout=0.01,
                wait_log=lambda *a, **k: (_ for _ in ()).throw(
                    runner.EffectRunError("bad marker")
                ),
            )
        except runner.EffectRunError:
            pass
        else:
            errors.append("runner accepts missing READY")
        if q2.commands or l2.stdin.writes:
            errors.append("runner issues QMP/GO before READY")
        # Duplicate READY must fail immediately instead of turning into a timeout.
        log.write_bytes(ready + b"\n" + ready + b"\n")
        try:
            runner._wait_log(log, l2, want_ready=True, timeout=0.01)
        except runner.EffectRunError as error:
            if "duplicate" not in str(error):
                errors.append("duplicate READY is not fail-fast")
        else:
            errors.append("runner accepts duplicate READY")
        # Duplicate Guest-DTB READY and a mismatched QMP response id are both
        # immediate identity failures, matching real QMP response envelopes.
        log.write_bytes(
            b"AXVISOR_GUEST_DTB_READY vm=1 gpa=0x80000000 size=64 hpa_segments=0x80000000:64\n"
            b"AXVISOR_GUEST_DTB_READY vm=1 gpa=0x80000000 size=64 hpa_segments=0x80000000:64\n"
            + ready
            + b"\n"
        )
        try:
            runner._wait_log(log, l2, want_ready=True, timeout=0.01)
        except runner.EffectRunError as error:
            if "duplicate Guest-DTB READY" not in str(error):
                errors.append("duplicate Guest-DTB READY is not fail-fast")
        else:
            errors.append("runner accepts duplicate Guest-DTB READY")

        class WrongIdQmp:
            def execute_control(self, _command, *, request_id):
                return {"return": {}, "id": request_id + "-wrong"}

        try:
            runner._qmp(WrongIdQmp(), "stop", {}, "expected-id")
        except runner.EffectRunError:
            pass
        else:
            errors.append("runner accepts a QMP control response with the wrong id")
    finally:
        shutil.rmtree(d)
    if errors:
        print("DMA runner contract failed:", *errors, sep="\n  - ", file=sys.stderr)
        return 1
    print("DMA runner contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
