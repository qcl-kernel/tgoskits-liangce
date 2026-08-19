#!/usr/bin/env python3
"""Prepare a fresh disposable Linux network rootfs for P4 TEST-011..015.

Starts from a byte-verified copy of the P2 soak-bound ext4 and injects the
contest linux-ai-controller binary plus an /init that configures the fixed
IPv4/route and launches the requested mode.  The output is no-overwrite,
bound to the source hashes, and verified by dump-back comparison.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

INIT_GUEST_PATH = "/init"
APP_GUEST_PATH = "/linux-ai-controller"
BOOT_ID_PATTERN = "[A-Za-z0-9._-]{1,64}"


class NetworkRootfsError(ValueError):
    """The supplied inputs cannot produce a safe network rootfs."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(command: list[str], *, check: bool, accept_one: bool = False) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    ok = result.returncode == 0
    if accept_one:
        ok = ok or result.returncode == 1  # e2fsck: 1 = errors corrected
    if check and not ok:
        raise NetworkRootfsError(
            f"command failed ({result.returncode}): {' '.join(str(c) for c in command)}\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
        )


def _init_script(boot_id: str, mode: str) -> bytes:
    # The P2-derived rootfs exposes only `sh`/`init` symlinks to busybox, so
    # every non-builtin command must be invoked as `/bin/busybox <applet>`
    # and must tolerate failure (a missing applet would otherwise exit 127
    # and panic the kernel as "Attempted to kill init").
    return (
        "#!/bin/sh\n"
        "set -e\n"
        "/bin/busybox mount -t proc proc /proc 2>/dev/null || true\n"
        "/bin/busybox mount -t sysfs sysfs /sys 2>/dev/null || true\n"
        "echo 'AXVISOR_LINUX_INIT_ENTER vm=1 boot_id=" + boot_id + "'\n"
        "echo 'AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=" + boot_id + "'\n"
        "exec /linux-ai-controller --mode=" + mode + "\n"
    ).encode()


def prepare_network_rootfs(
    *,
    source_rootfs: Path,
    app_binary: Path,
    boot_id: str,
    mode: str,
    output_rootfs: Path,
    debugfs: str = "debugfs",
    e2fsck: str = "e2fsck",
) -> dict[str, object]:
    if len(boot_id) > 64 or not all(c.isalnum() or c in "._-" for c in boot_id):
        raise NetworkRootfsError("--boot-id must match [A-Za-z0-9._-]{1,64}")
    if mode not in ("l3-smoke", "udp-echo", "tcp-client", "icpc"):
        raise NetworkRootfsError(f"unsupported network mode {mode!r}")
    if source_rootfs.resolve() == output_rootfs.resolve():
        raise NetworkRootfsError("source and output rootfs must be different paths")
    if output_rootfs.exists():
        raise NetworkRootfsError(f"output rootfs already exists: {output_rootfs}")

    workspace = output_rootfs.parent / f".p4-rootfs-{os.getpid()}-{secrets.token_hex(16)}"
    workspace.mkdir(mode=0o755)
    staged = workspace / "rootfs.ext4"
    staged_init = workspace / "init"
    staged_app = workspace / "app"
    published = False
    try:
        # 1. Byte-verified fresh copy of the P2 soak-bound rootfs.
        source_hash = _sha256_file(source_rootfs)
        shutil.copyfile(source_rootfs, staged)
        staged_hash = _sha256_file(staged)
        if staged_hash != source_hash:
            raise NetworkRootfsError("source rootfs changed while being copied")
        source_size = source_rootfs.stat().st_size

        # 2. Inject the app binary and the /init script.
        app_hash = _sha256_file(app_binary)
        shutil.copyfile(app_binary, staged_app)
        init_bytes = _init_script(boot_id, mode)
        staged_init.write_bytes(init_bytes)
        init_hash = hashlib.sha256(init_bytes).hexdigest()

        _run([e2fsck, staged, "-fy"], check=True, accept_one=True)
        _run([debugfs, "-w", "-R", f"rm {INIT_GUEST_PATH}", staged], check=False)
        _run([debugfs, "-w", "-R", f"write {staged_init} {INIT_GUEST_PATH}", staged], check=True)
        _run([debugfs, "-w", "-R", f"sif {INIT_GUEST_PATH} mode 0100755", staged], check=True)
        _run([debugfs, "-w", "-R", f"write {staged_app} {APP_GUEST_PATH}", staged], check=True)
        _run([debugfs, "-w", "-R", f"sif {APP_GUEST_PATH} mode 0100755", staged], check=True)
        _run([e2fsck, staged, "-fn"], check=True, accept_one=True)

        # 3. Verify the injected files by dump-back comparison.
        dumped_init = workspace / "dump-init"
        dumped_app = workspace / "dump-app"
        _run([debugfs, "-w", "-R", f"dump {INIT_GUEST_PATH} {dumped_init}", staged], check=True)
        _run([debugfs, "-w", "-R", f"dump {APP_GUEST_PATH} {dumped_app}", staged], check=True)
        if not dumped_init.is_file() or not dumped_app.is_file():
            raise NetworkRootfsError("debugfs dump produced no file")
        if _sha256_file(dumped_init) != init_hash:
            raise NetworkRootfsError("injected /init dump does not match the generated bytes")
        if _sha256_file(dumped_app) != app_hash:
            raise NetworkRootfsError("injected app dump does not match the source binary")

        output_hash = _sha256_file(staged)
        os.link(staged, output_rootfs, follow_symlinks=False)
        published = True
        return {
            "schemaVersion": 1,
            "artifactStatus": "prepared-network-rootfs",
            "status": "linux_network_rootfs_prepared",
            "proofScope": "one-fresh-ext4-copy-with-app-and-init-injection",
            "doesNotProve": [
                "the rootfs was booted",
                "VirtIO-net frontend registration",
                "IPv4 connectivity",
                "DMA isolation",
                "dual-Guest execution",
            ],
            "bootId": boot_id,
            "mode": mode,
            "sourceRootfs": {"path": source_rootfs.name, "size": source_size, "sha256": source_hash},
            "appBinary": {"path": app_binary.name, "size": app_binary.stat().st_size, "sha256": app_hash},
            "outputRootfs": {"path": output_rootfs.name, "size": staged.stat().st_size, "sha256": output_hash},
            "init": {"path": INIT_GUEST_PATH, "size": len(init_bytes), "sha256": init_hash},
        }
    finally:
        if not published:
            shutil.rmtree(workspace, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-rootfs", required=True, type=Path)
    parser.add_argument("--app-binary", required=True, type=Path)
    parser.add_argument("--boot-id", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--output-rootfs", required=True, type=Path)
    parser.add_argument("--output-manifest", required=True, type=Path)
    parser.add_argument("--debugfs", default="debugfs")
    parser.add_argument("--e2fsck", default="e2fsck")
    args = parser.parse_args(argv)

    try:
        result = prepare_network_rootfs(
            source_rootfs=args.source_rootfs,
            app_binary=args.app_binary,
            boot_id=args.boot_id,
            mode=args.mode,
            output_rootfs=args.output_rootfs,
            debugfs=args.debugfs,
            e2fsck=args.e2fsck,
        )
    except NetworkRootfsError as error:
        print(f"Linux network rootfs preparation failed: {error}", file=sys.stderr)
        return 1
    if args.output_manifest.exists():
        print(f"refusing to overwrite manifest: {args.output_manifest}", file=sys.stderr)
        return 1
    args.output_manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "outputRootfs": str(args.output_rootfs)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
