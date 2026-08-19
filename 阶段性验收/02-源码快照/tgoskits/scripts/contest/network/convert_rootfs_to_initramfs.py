#!/usr/bin/env python3
"""Convert a P2/P4 disposable ext4 rootfs into a cpio.gz initramfs.

Extracts the full tree with debugfs rdump and repacks it with the classic
newc cpio format so the locked Linux kernel can mount it as initramfs.
This releases the QEMU virtio-mmio slot 0 (root block) so the mediated
vnet0 frontend at 0x0a000200 can own its 4 KiB page exclusively.
"""

from __future__ import annotations

import argparse
import gzip
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path


class InitramfsError(ValueError):
    """The conversion cannot produce a safe initramfs."""


def _run(command: list[str], *, check: bool = True) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise InitramfsError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
        )


def convert(
    *,
    source_rootfs: Path,
    output_initramfs: Path,
    debugfs: str = "debugfs",
) -> dict[str, object]:
    if output_initramfs.exists():
        raise InitramfsError(f"output initramfs already exists: {output_initramfs}")
    # Build inside the WSL-native filesystem: DrvFS (9p) rejects mknod,
    # which we need for the standard console device nodes.
    workspace = Path("/tmp") / f"contest-initramfs-{os.getpid()}-{secrets.token_hex(16)}"
    root = workspace / "root"
    root.mkdir(parents=True)
    published = False
    try:
        # Extract the full ext4 tree.
        _run([debugfs, "-R", "rdump / " + str(root), str(source_rootfs)])
        # debugfs rdump materialises device nodes as empty regular files;
        # rebuild the standard console nodes so init's stdout (opened via
        # /dev/console) can reach the kernel console/earlycon.
        dev_dir = root / "dev"
        dev_dir.mkdir(exist_ok=True)
        for name, major, minor in (
            ("console", 5, 1),
            ("null", 1, 3),
            ("tty", 5, 0),
            ("zero", 1, 5),
            ("ttyAMA0", 204, 64),
        ):
            node = dev_dir / name
            if node.exists() or node.is_symlink():
                node.unlink()
            os.mknod(node, 0o600 | stat.S_IFCHR, os.makedev(major, minor))
        # Pack as newc cpio (deterministic), then gzip.
        cpio_path = workspace / "initramfs.cpio"
        root_abs = root.resolve()
        with cpio_path.open("wb") as out:
            proc = subprocess.Popen(
                ["cpio", "--create", "--format=newc", "--owner=0:0", "--quiet"],
                stdin=subprocess.PIPE,
                stdout=out,
                cwd=root_abs,
            )
            assert proc.stdin is not None
            # Directory entries first (like the official image): the kernel
            # initramfs unpacker follows the newc directory entries; a
            # file-only archive can silently drop files whose parent
            # directory was never declared.
            proc.stdin.write(b".\n")
            for dirpath, dirnames, filenames in os.walk(root_abs):
                dirnames.sort()
                filenames.sort()
                rel_dir = Path(dirpath).relative_to(root_abs).as_posix()
                if rel_dir != ".":
                    proc.stdin.write((rel_dir + "\n").encode())
                for name in filenames:
                    full = Path(dirpath) / name
                    rel = full.relative_to(root_abs).as_posix()
                    proc.stdin.write((rel + "\n").encode())
            proc.stdin.close()
            if proc.wait() != 0:
                raise InitramfsError("cpio packing failed")
        with gzip.open(output_initramfs, "wb") as out:
            out.write(cpio_path.read_bytes())
        published = True
        return {
            "schemaVersion": 1,
            "artifactStatus": "prepared-initramfs",
            "status": "linux_network_initramfs_prepared",
            "proofScope": "one-debugfs-rdump-and-newc-cpio-gzip-repack",
            "doesNotProve": [
                "the initramfs was booted",
                "VirtIO-net frontend registration",
                "IPv4 connectivity",
                "dual-Guest execution",
            ],
            "sourceRootfs": str(source_rootfs),
            "outputInitramfs": str(output_initramfs),
            "bytes": output_initramfs.stat().st_size,
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        _ = published


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-rootfs", required=True, type=Path)
    parser.add_argument("--output-initramfs", required=True, type=Path)
    parser.add_argument("--debugfs", default="debugfs")
    args = parser.parse_args(argv)
    try:
        result = convert(
            source_rootfs=args.source_rootfs,
            output_initramfs=args.output_initramfs,
            debugfs=args.debugfs,
        )
    except InitramfsError as error:
        print(f"initramfs conversion failed: {error}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
