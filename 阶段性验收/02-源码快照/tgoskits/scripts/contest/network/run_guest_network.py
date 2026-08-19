#!/usr/bin/env python3
"""P4 guest-network runner for TEST-011..015.

Launches one identity-bound dual-Guest QEMU with the mediated VirtIO-net
profile and the prepared Linux/Zephyr network images, waits for the
scenario's completion markers in the demuxed guest consoles, then publishes
an IF-011 status-last evidence bundle.  Never reuses an output directory;
every input is byte-bound to its manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

RUN_SCHEMA_VERSION = 1
SCENARIOS = ("test-011", "test-012", "test-013", "test-015")
TOKENS = {
    "ok": "guest_network_completed",
    "failed": "guest_network_failed",
    "blocked": "guest_network_blocked",
}
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
BOOT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class GuestNetworkError(ValueError):
    """The supplied inputs or the run cannot produce valid evidence."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _checked_file(path: Path, field: str) -> Path:
    if not path.is_file() or path.is_symlink():
        raise GuestNetworkError(f"{field} must be a regular non-link file: {path}")
    return path


def _new_directory(path: Path, label: str) -> Path:
    try:
        path.mkdir()
    except FileExistsError as error:
        raise GuestNetworkError(f"{label} already exists: {path}") from error
    return path


def _read_json(path: Path, label: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GuestNetworkError(f"cannot parse {label}: {error}") from error


def _bind_manifest_artifact(manifest: dict, key: str, actual: Path, label: str) -> None:
    entry = manifest.get(key)
    if not isinstance(entry, dict) or "sha256" not in entry:
        raise GuestNetworkError(f"{label} manifest has no {key} entry")
    actual_hash = _sha256_file(actual)
    if actual_hash != entry["sha256"]:
        raise GuestNetworkError(
            f"{label} {key} hash {actual_hash} does not match manifest {entry['sha256']}"
        )


def _wait_for_marker(
    log_path: Path,
    marker: str,
    timeout: float,
    failure_markers: tuple[str, ...] = (),
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    tail = b""
    while time.monotonic() < deadline:
        try:
            data = log_path.read_bytes()
        except OSError:
            data = b""
        if data != tail:
            tail = data
        for failure_marker in failure_markers:
            if failure_marker.encode() in tail:
                raise GuestNetworkError(
                    f"runtime failure marker {failure_marker!r} observed before {marker!r}"
                )
        if marker.encode() in tail:
            return True, tail.decode("utf-8", errors="replace")
        time.sleep(1)
    return False, tail.decode("utf-8", errors="replace")


def _validate_runtime_markers(log_text: str, profile_data: dict) -> None:
    oracle = profile_data.get("oracle")
    if not isinstance(oracle, dict):
        raise GuestNetworkError("scenario profile has no runtime oracle")
    required = oracle.get("requiredMarkers", [])
    if not isinstance(required, list) or any(not isinstance(marker, str) for marker in required):
        raise GuestNetworkError("scenario profile requiredMarkers must be a string list")
    missing = [marker for marker in required if marker not in log_text]
    if missing:
        raise GuestNetworkError(
            "required Guest markers were not observed: " + ", ".join(missing)
        )
    forbidden = oracle.get("forbidden", [])
    if not isinstance(forbidden, list) or any(not isinstance(marker, str) for marker in forbidden):
        raise GuestNetworkError("scenario profile forbidden markers must be a string list")
    observed = [marker for marker in forbidden if marker.lower() in log_text.lower()]
    if observed:
        raise GuestNetworkError(
            "forbidden runtime markers were observed: " + ", ".join(observed)
        )


def run_guest_network(
    *,
    repository: Path,
    build_config: Path,
    qemu_config: Path,
    linux_vmconfig: Path,
    zephyr_vmconfig: Path,
    linux_rootfs: Path,
    linux_manifest: Path,
    zephyr_image: Path,
    zephyr_build_manifest: Path,
    scenario: str,
    profile: Path,
    timeout_seconds: float,
    run_id: str,
    output_dir: Path,
) -> int:
    if scenario not in SCENARIOS:
        raise GuestNetworkError(f"unknown scenario {scenario!r}")
    if not re.match(r"^[A-Za-z0-9._-]+$", run_id):
        raise GuestNetworkError("run-id must match [A-Za-z0-9._-]+")

    output = _new_directory(output_dir, "output directory")
    configs_dir = _new_directory(output / "configs", "configs directory")
    logs_dir = _new_directory(output / "logs", "logs directory")
    network_dir = _new_directory(output / "network", "network directory")
    metrics_dir = _new_directory(output / "metrics", "metrics directory")
    dtb_dir = _new_directory(output / "dtb", "dtb directory")

    for path, field in [
        (build_config, "build config"), (qemu_config, "QEMU config"),
        (linux_vmconfig, "Linux VM config"), (zephyr_vmconfig, "Zephyr VM config"),
        (linux_rootfs, "Linux rootfs"), (linux_manifest, "Linux rootfs manifest"),
        (zephyr_image, "Zephyr image"), (zephyr_build_manifest, "Zephyr build manifest"),
        (profile, "scenario profile"),
    ]:
        _checked_file(path, field)

    linux_meta = _read_json(linux_manifest, "Linux rootfs manifest")
    zephyr_meta = _read_json(zephyr_build_manifest, "Zephyr build manifest")
    _bind_manifest_artifact(linux_meta, "outputRootfs", linux_rootfs, "Linux")
    # Zephyr manifests nest the artifacts under "artifacts.bin".
    zephyr_bin_entry = zephyr_meta.get("artifacts", {}).get("bin", zephyr_meta.get("bin"))
    if not isinstance(zephyr_bin_entry, dict) or zephyr_bin_entry.get("sha256") != _sha256_file(zephyr_image):
        raise GuestNetworkError(
            f"Zephyr image hash does not match its build manifest"
        )
    profile_data = _read_json(profile, "scenario profile")

    # Resolve the Zephyr VM config against the manifest-bound image path.
    zephyr_vm_text = zephyr_vmconfig.read_text(encoding="utf-8")
    zephyr_image_abs = zephyr_image.resolve()
    if "/path/to/zephyr.bin" in zephyr_vm_text:
        zephyr_vm_text = zephyr_vm_text.replace("/path/to/zephyr.bin", str(zephyr_image_abs))
    resolved_zephyr_vmconfig = output / "configs" / "zephyr.resolved.toml"
    resolved_zephyr_vmconfig.write_text(zephyr_vm_text, encoding="utf-8")
    zephyr_vmconfig = resolved_zephyr_vmconfig

    # Resolve the Linux kernel path (locked image cache) in the Linux VM config.
    linux_vm_text = linux_vmconfig.read_text(encoding="utf-8")
    locked_kernel = Path("/root/.cache/tgoskits/axvisor-images/qemu_aarch64_linux/qemu-aarch64")
    if not locked_kernel.is_file():
        raise GuestNetworkError(f"locked Linux kernel image missing: {locked_kernel}")
    if "/guest/linux/linux-qemu" in linux_vm_text:
        linux_vm_text = linux_vm_text.replace("/guest/linux/linux-qemu", str(locked_kernel))
        # The locked image is read directly into memory, not from a guest fs.
        linux_vm_text = linux_vm_text.replace('image_location = "fs"', 'image_location = "memory"')
        # The rootfs travels as an initramfs (releases the virtio-mmio slot 0
        # page so the mediated vnet0 frontend can own 0x0a000200 exclusively).
        initramfs = output / "configs" / "linux-initramfs.cpio.gz"
        from convert_rootfs_to_initramfs import convert as convert_initramfs  # type: ignore
        convert_initramfs(source_rootfs=linux_rootfs, output_initramfs=initramfs)
        # The Linux RAM is 0x8000_0000..0x9000_0000 (MAP_IDENTICAL); place the
        # initramfs above the kernel (kernel ~0x8020_0000, ~32 MiB headroom
        # at 0x8800_0000) instead of the legacy 0x0c000000 which is outside
        # the VM region on the official baseline.
        ramdisk_gpa = "0x8800_0000"
        linux_vm_text = linux_vm_text.replace(
            'kernel_load_addr = 0x8020_0000',
            'kernel_load_addr = 0x8020_0000\nramdisk_path = "' + str(initramfs.resolve()) + '"\nramdisk_load_addr = ' + ramdisk_gpa,
        )
    resolved_linux_vmconfig = output / "configs" / "linux.resolved.toml"
    resolved_linux_vmconfig.write_text(linux_vm_text, encoding="utf-8")
    linux_vmconfig = resolved_linux_vmconfig

    # Freeze inputs.
    inputs = {
        name: {"path": str(path), "sha256": _sha256_file(path)}
        for name, path in [
            ("buildConfig", build_config), ("qemuConfig", qemu_config),
            ("linuxVmconfig", linux_vmconfig), ("zephyrVmconfig", zephyr_vmconfig),
            ("linuxRootfs", linux_rootfs), ("zephyrImage", zephyr_image),
            ("profile", profile),
        ]
    }

    nonce = secrets.token_hex(16)
    boot_id = f"p4net-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{nonce[:8]}"
    qemu_name = f"axvisor-dual-smoke-{nonce}"
    runtime_dir = Path("/tmp") / f"axp4-{nonce}"
    runtime_dir.mkdir()
    qmp_socket = runtime_dir / "qmp.sock"
    qemu_pidfile = runtime_dir / "qemu.pid"
    live_log = runtime_dir / "axvisor-live.log"

    session = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "runId": run_id,
        "scenario": scenario,
        "sessionNonce": nonce,
        "bootId": boot_id,
        "qemuName": qemu_name,
        "startedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (output / "session.json").write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")

    # Official-baseline xtask `qemu` has no QMP/pidfile/name flags; inject
    # the identity-binding arguments into the QEMU config `args` instead.
    qemu_text = qemu_config.read_text(encoding="utf-8")
    qemu_marker = "args = ["
    qemu_start = qemu_text.find(qemu_marker)
    qemu_end = qemu_text.find("]", qemu_start) if qemu_start >= 0 else -1
    if qemu_start < 0 or qemu_end < 0:
        raise GuestNetworkError("QEMU config has no closed `args = [...]` array")
    qemu_inject = "".join(
        f'  "{arg}",\n' for arg in ("-qmp", f"unix:{qmp_socket},server=on,wait=off",
                                      "-pidfile", str(qemu_pidfile),
                                      "-name", qemu_name)
    )
    resolved_qemu_config = output / "configs" / "qemu.resolved.toml"
    resolved_qemu_config.write_text(
        qemu_text[:qemu_end] + qemu_inject + qemu_text[qemu_end:], encoding="utf-8"
    )

    commands = [{
        "step": "launch",
        "argv": [
            "cargo", "xtask", "qemu",
            "--config", str(build_config),
            "--qemu-config", str(resolved_qemu_config),
            "--vmconfigs", str(linux_vmconfig),
            "--vmconfigs", str(zephyr_vmconfig),
        ],
    }]
    (output / "commands.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in commands), encoding="utf-8")

    launcher = None
    log_handle = None
    success = False
    primary_error = None
    cleanup_error = None
    try:
        log_handle = live_log.open("xb", buffering=0)
        launcher = subprocess.Popen(
            commands[0]["argv"],
            cwd=repository / "os/axvisor",
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # Wait for the scenario completion marker in the raw log.
        scenario_timeout = timeout_seconds
        marker = profile_data.get("completionMarker", "TGOS_LINUX_L3_SMOKE")
        found, log_text = _wait_for_marker(
            live_log,
            marker,
            scenario_timeout,
            ("ESR_EL2:", "ELR_EL2:", "FAR_EL2:"),
        )
        if not found:
            raise GuestNetworkError(f"completion marker {marker!r} not seen within {scenario_timeout}s")
        _validate_runtime_markers(log_text, profile_data)
        # Let the QEMU settle and the launcher exit via SIGINT.
        time.sleep(2)
        try:
            qemu_pid = int(qemu_pidfile.read_text().strip())
            os.kill(qemu_pid, 2)  # SIGINT
        except (OSError, ValueError):
            pass
        try:
            launcher.wait(timeout=30)
        except subprocess.TimeoutExpired:
            cleanup_error = "launcher did not exit after bounded SIGINT"
        success = cleanup_error is None
    except GuestNetworkError as error:
        primary_error = str(error)
    except Exception as error:  # noqa: BLE001 - package all failure evidence.
        primary_error = str(error)
    finally:
        if launcher is not None and launcher.poll() is None:
            import signal as _signal
            try:
                os.killpg(os.getpgid(launcher.pid), _signal.SIGKILL)
            except (OSError, ProcessLookupError):
                launcher.kill()
        if log_handle is not None:
            log_handle.close()
        if live_log.exists():
            shutil.copyfile(live_log, logs_dir / "axvisor.raw.log")
        shutil.rmtree(runtime_dir, ignore_errors=True)

    # Publish artifacts.
    manifest = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "artifactStatus": "run-complete" if success else "failed-attempt",
        "status": TOKENS["ok"] if success else TOKENS["failed"],
        "proofScope": "one-identity-bound-dual-guest-mediated-network-session",
        "doesNotProve": [
            "DMA isolation", "hardware DMA", "AI closed loop",
            "real-time improvement", "outer-QEMU networking",
        ],
        "inputs": inputs,
        "session": session,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    status = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "runId": run_id,
        "success": success,
        "status": TOKENS["ok"] if success else TOKENS["failed"],
        "primaryError": primary_error,
        "cleanupError": cleanup_error,
        "completedChecks": ["runtime-markers", "cleanup"] if success else [],
        "manifestSha256": _sha256_file(output / "manifest.json"),
    }
    (output / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--build-config", required=True, type=Path)
    parser.add_argument("--qemu-config", required=True, type=Path)
    parser.add_argument("--linux-vmconfig", required=True, type=Path)
    parser.add_argument("--zephyr-vmconfig", required=True, type=Path)
    parser.add_argument("--linux-rootfs", required=True, type=Path)
    parser.add_argument("--linux-rootfs-manifest", required=True, type=Path)
    parser.add_argument("--zephyr-image", required=True, type=Path)
    parser.add_argument("--zephyr-build-manifest", required=True, type=Path)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--timeout-seconds", required=True, type=float)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return run_guest_network(
            repository=args.repository,
            build_config=args.build_config,
            qemu_config=args.qemu_config,
            linux_vmconfig=args.linux_vmconfig,
            zephyr_vmconfig=args.zephyr_vmconfig,
            linux_rootfs=args.linux_rootfs,
            linux_manifest=args.linux_rootfs_manifest,
            zephyr_image=args.zephyr_image,
            zephyr_build_manifest=args.zephyr_build_manifest,
            scenario=args.scenario,
            profile=args.profile,
            timeout_seconds=args.timeout_seconds,
            run_id=args.run_id,
            output_dir=args.output_dir,
        )
    except GuestNetworkError as error:
        print(f"guest network run failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
