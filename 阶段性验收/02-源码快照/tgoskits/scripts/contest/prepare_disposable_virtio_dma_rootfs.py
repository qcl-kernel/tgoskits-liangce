#!/usr/bin/env python3
"""Create a fresh ext4 Guest rootfs for one bounded virtio-blk experiment.

The cache image is an input only.  This tool copies it to a never-before-used
path, injects an ELF helper and a purpose-built ``/init`` through ``debugfs``,
then writes a no-overwrite JSON plan.  It does not start QEMU or submit a
virtio request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
from pathlib import Path
from typing import Callable

from host_vm_carveout_io import publish_new_file


NONCE = re.compile(r"[0-9a-f]{32}\Z")
EXT4_MAGIC_OFFSET = 1024 + 56
EXT4_MAGIC = b"\x53\xef"
HELPER_GUEST_PATH = "/tgos-dma-effect-helper"
INIT_GUEST_PATH = "/init"
BLOCK_DEVICE_PATH = "/dev/vdb"
CONTROL_DEVICE_PATH = "/dev/hvc0"
HELPER_HOLD_MILLISECONDS = 300000
DEVICE_WAIT_SECONDS = 60
MAX_HELPER_BYTES = 16 * 1024 * 1024
REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


class DisposableRootfsError(ValueError):
    """The supplied inputs cannot safely produce a disposable rootfs."""


def _sha256_file(path: Path, *, label: str) -> tuple[int, str]:
    info = _regular(path, label=label)
    initial_state = _file_state(info)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if _file_state(opened) != initial_state:
            raise DisposableRootfsError(f"{label} changed while it was opened")
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            while block := stream.read(1024 * 1024):
                digest.update(block)
                size += len(block)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if _file_state(_regular(path, label=label)) != initial_state:
        raise DisposableRootfsError(f"{label} changed while it was read")
    return size, digest.hexdigest()


def _identity(info: os.stat_result) -> tuple[int, int]:
    return int(info.st_dev), int(info.st_ino)


def _file_state(info: os.stat_result) -> tuple[int, int, int, int]:
    """Fingerprint a regular input without treating a path re-open as stable.

    Deliberately omit ``ctime``: Windows reports a different ctime value for
    lstat versus an equivalent open handle on some mounted worktrees.
    """

    return (
        *_identity(info),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", 0)),
    )


def _is_reparse(info: os.stat_result) -> bool:
    return bool(int(getattr(info, "st_file_attributes", 0)) & REPARSE_FLAG)


def _regular(path: Path, *, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise DisposableRootfsError(f"{label} does not exist") from error
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise DisposableRootfsError(f"{label} must be a regular non-link file")
    return info


def _directory_chain(path: Path) -> None:
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise DisposableRootfsError(f"output directory component {item} is unsafe")


def _ensure_absent(path: Path, *, label: str) -> None:
    _directory_chain(path.parent)
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise DisposableRootfsError(f"{label} {path.name!r} already exists")


def _unlink_owned(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(current.st_mode) or _is_reparse(current) or _identity(current) != identity:
        raise DisposableRootfsError(f"refused to remove replaced temporary file {path.name}")
    path.unlink()


def _copy_new(
    source: Path, destination: Path, *, label: str, require_ext4: bool = False,
    max_bytes: int | None = None,
) -> tuple[int, str]:
    """Copy a stable regular source to an absent destination and hash its bytes."""

    source_info = _regular(source, label=label)
    source_state = _file_state(source_info)
    if max_bytes is not None and source_info.st_size > max_bytes:
        raise DisposableRootfsError(f"{label} exceeds {max_bytes} byte limit")
    _ensure_absent(destination, label="destination")
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    destination_fd = -1
    destination_identity: tuple[int, int] | None = None
    try:
        if _file_state(os.fstat(source_fd)) != source_state:
            raise DisposableRootfsError(f"{label} changed while it was opened")
        if require_ext4:
            probe = os.read(source_fd, EXT4_MAGIC_OFFSET + len(EXT4_MAGIC))
            if len(probe) < EXT4_MAGIC_OFFSET + len(EXT4_MAGIC) or probe[EXT4_MAGIC_OFFSET:EXT4_MAGIC_OFFSET + 2] != EXT4_MAGIC:
                raise DisposableRootfsError(f"{label} is not an ext4 image (superblock magic missing)")
            os.lseek(source_fd, 0, os.SEEK_SET)
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        destination_identity = _identity(os.fstat(destination_fd))
        digest = hashlib.sha256()
        size = 0
        while block := os.read(source_fd, 1024 * 1024):
            digest.update(block)
            size += len(block)
            if max_bytes is not None and size > max_bytes:
                raise DisposableRootfsError(f"{label} exceeds {max_bytes} byte limit")
            offset = 0
            while offset < len(block):
                offset += os.write(destination_fd, block[offset:])
        os.fsync(destination_fd)
        if _file_state(os.fstat(source_fd)) != source_state or _file_state(_regular(source, label=label)) != source_state:
            raise DisposableRootfsError(f"{label} changed while it was copied")
        if _identity(os.fstat(destination_fd)) != destination_identity:
            raise DisposableRootfsError("temporary output identity changed while copying")
        return size, digest.hexdigest()
    except Exception:
        if destination_identity is not None:
            _unlink_owned(destination, destination_identity)
        raise
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)


def _write_new(path: Path, data: bytes) -> None:
    _ensure_absent(path, label="temporary input")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _debugfs_quote(path: Path | str) -> str:
    value = str(path)
    if "\x00" in value or "\n" in value or "\r" in value:
        raise DisposableRootfsError("debugfs path must not contain line controls")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _init_bytes(nonce: str) -> bytes:
    return f"""#!/bin/sh
# Generated by prepare_disposable_virtio_dma_rootfs.py; nonce={nonce}
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
echo "TGOS_DMA_EFFECT_HELPER_BEGIN nonce={nonce} device={BLOCK_DEVICE_PATH} control_device={CONTROL_DEVICE_PATH} sector=0"
device_wait=0
while {{ [ ! -b {BLOCK_DEVICE_PATH} ] || [ ! -c {CONTROL_DEVICE_PATH} ]; }} && [ "$device_wait" -lt {DEVICE_WAIT_SECONDS} ]; do
    sleep 1
    device_wait=$((device_wait + 1))
done
if [ ! -b {BLOCK_DEVICE_PATH} ] || [ ! -c {CONTROL_DEVICE_PATH} ]; then
    echo "TGOS_DMA_EFFECT_HELPER_DEVICE_TIMEOUT nonce={nonce} device={BLOCK_DEVICE_PATH} control_device={CONTROL_DEVICE_PATH} waited_seconds=$device_wait"
    exit 1
fi
exec {HELPER_GUEST_PATH} --nonce {nonce} --device {BLOCK_DEVICE_PATH} --control-device {CONTROL_DEVICE_PATH} --sector 0 --hold-ms {HELPER_HOLD_MILLISECONDS}
""".encode("utf-8")


def _read_stable_helper_bytes(path: Path) -> bytes:
    """Read the staged helper once through a nofollow FD for ELF validation."""

    info = _regular(path, label="staged compiled helper")
    state = _file_state(info)
    if info.st_size > MAX_HELPER_BYTES:
        raise DisposableRootfsError(f"staged compiled helper exceeds {MAX_HELPER_BYTES} byte limit")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if _file_state(opened) != state:
            raise DisposableRootfsError("staged compiled helper changed while it was opened")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            data = stream.read(MAX_HELPER_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > MAX_HELPER_BYTES:
        raise DisposableRootfsError(f"staged compiled helper exceeds {MAX_HELPER_BYTES} byte limit")
    if _file_state(_regular(path, label="staged compiled helper")) != state:
        raise DisposableRootfsError("staged compiled helper changed while it was read")
    return data


def _validate_static_aarch64_helper(data: bytes) -> None:
    """Require an ELF64/AArch64 ET_EXEC helper with no loader or dependency."""

    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4:7] != b"\x02\x01\x01":
        raise DisposableRootfsError("compiled helper must be a little-endian ELF64 binary")
    e_type, machine = struct.unpack_from("<HH", data, 16)
    if e_type != 2 or machine != 183:
        raise DisposableRootfsError("compiled helper must be an AArch64 ET_EXEC binary (use -static -no-pie)")
    program_offset = struct.unpack_from("<Q", data, 32)[0]
    program_size, program_count = struct.unpack_from("<HH", data, 54)
    if program_size != 56 or program_offset > len(data) or program_count > (len(data) - program_offset) // program_size:
        raise DisposableRootfsError("compiled helper has malformed ELF program headers")
    for index in range(program_count):
        offset = program_offset + index * program_size
        program_type = struct.unpack_from("<I", data, offset)[0]
        if program_type == 3:  # PT_INTERP
            raise DisposableRootfsError("compiled helper must not contain a PT_INTERP loader")
        if program_type != 2:  # PT_DYNAMIC
            continue
        dynamic_offset, dynamic_size = struct.unpack_from("<QQ", data, offset + 8)
        if dynamic_offset > len(data) or dynamic_size > len(data) - dynamic_offset or dynamic_size % 16:
            raise DisposableRootfsError("compiled helper has malformed PT_DYNAMIC data")
        terminated = False
        for dynamic_index in range(dynamic_size // 16):
            tag, _value = struct.unpack_from("<qQ", data, dynamic_offset + dynamic_index * 16)
            if tag == 1:  # DT_NEEDED
                raise DisposableRootfsError("compiled helper must not contain DT_NEEDED dependencies")
            if tag == 0:  # DT_NULL
                terminated = True
                break
        if not terminated:
            raise DisposableRootfsError("compiled helper PT_DYNAMIC data has no DT_NULL terminator")


def _run_debugfs(tool: str, image: Path, command: str, *, required: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([tool, "-w", "-R", command, str(image)], text=True, encoding="utf-8", errors="strict", capture_output=True, check=False)
    if required and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown debugfs failure"
        raise DisposableRootfsError(f"debugfs command failed ({command!r}): {detail}")
    return result


DebugfsRunner = Callable[[str, Path, str, bool], None]


def _verified_dump(tool: str, image: Path, guest_path: str, destination: Path) -> tuple[int, str]:
    _run_debugfs(tool, image, f"dump -p {guest_path} {_debugfs_quote(destination)}")
    return _sha256_file(destination, label=f"debugfs dump {guest_path}")


def _default_runner(tool: str, image: Path, command: str, required: bool) -> None:
    _run_debugfs(tool, image, command, required=required)


def prepare_rootfs(
    *, source_rootfs: Path, helper: Path, output_rootfs: Path, nonce: str, debugfs: str,
    runner: DebugfsRunner = _default_runner, verify_injection: bool = True,
) -> dict[str, object]:
    if NONCE.fullmatch(nonce) is None:
        raise DisposableRootfsError("--session-nonce must be 128-bit lower-case hexadecimal")
    if source_rootfs.resolve() == output_rootfs.resolve():
        raise DisposableRootfsError("source rootfs and output rootfs must be different paths")
    _ensure_absent(output_rootfs, label="output rootfs")

    # Keep staging beside the requested output so the final hard-link cannot
    # cross filesystems.  Avoid tempfile's Windows-only restrictive ACL mode:
    # this workspace is still private by its random, no-overwrite name.
    workspace = output_rootfs.parent / f".dma-rootfs-{os.getpid()}-{secrets.token_hex(16)}"
    workspace.mkdir(mode=0o755)
    staged_rootfs = workspace / "rootfs.ext4"
    staged_helper = workspace / "helper.elf"
    staged_init = workspace / "init"
    published = False
    try:
        source_size, source_hash = _copy_new(source_rootfs, staged_rootfs, label="source rootfs", require_ext4=True)
        helper_size, helper_hash = _copy_new(
            helper, staged_helper, label="compiled helper", max_bytes=MAX_HELPER_BYTES,
        )
        helper_bytes = _read_stable_helper_bytes(staged_helper)
        if len(helper_bytes) != helper_size or hashlib.sha256(helper_bytes).hexdigest() != helper_hash:
            raise DisposableRootfsError("staged compiled helper bytes do not match the nofollow copied hash")
        _validate_static_aarch64_helper(helper_bytes)
        init_bytes = _init_bytes(nonce)
        _write_new(staged_init, init_bytes)
        init_size, init_hash = _sha256_file(staged_init, label="generated init")

        # Both removals target only the unpublished disposable copy.  They allow
        # a cache image that already has /init without mutating the cache itself.
        for guest_path in (HELPER_GUEST_PATH, INIT_GUEST_PATH):
            runner(debugfs, staged_rootfs, f"rm {guest_path}", False)
        runner(debugfs, staged_rootfs, f"write {_debugfs_quote(staged_helper)} {HELPER_GUEST_PATH}", True)
        runner(debugfs, staged_rootfs, f"sif {HELPER_GUEST_PATH} mode 0100755", True)
        runner(debugfs, staged_rootfs, f"write {_debugfs_quote(staged_init)} {INIT_GUEST_PATH}", True)
        runner(debugfs, staged_rootfs, f"sif {INIT_GUEST_PATH} mode 0100755", True)

        if verify_injection:
            dumped_helper = workspace / "dump-helper"
            dumped_init = workspace / "dump-init"
            if _verified_dump(debugfs, staged_rootfs, HELPER_GUEST_PATH, dumped_helper) != (helper_size, helper_hash):
                raise DisposableRootfsError("debugfs helper dump does not match the staged helper")
            if _verified_dump(debugfs, staged_rootfs, INIT_GUEST_PATH, dumped_init) != (init_size, init_hash):
                raise DisposableRootfsError("debugfs init dump does not match generated /init")

        output_size, output_hash = _sha256_file(staged_rootfs, label="prepared rootfs")
        _ensure_absent(output_rootfs, label="output rootfs")
        os.link(staged_rootfs, output_rootfs, follow_symlinks=False)
        published = True
        return {
            "schemaVersion": 1,
            "artifactStatus": "prepared-rootfs-only",
            "status": "disposable_virtio_dma_effect_guest_rootfs_prepared",
            "proofScope": "one-new-ext4-copy-with-helper-and-init-injection",
            "doesNotProve": [
                "the disposable rootfs was booted",
                "the helper ran or completed",
                "a virtio-blk request was submitted or completed",
                "DMA was executed",
                "DMA isolation",
                "dual-Guest execution",
                "Linux and Zephyr IP connectivity",
            ],
            "sessionNonce": nonce,
            "sourceRootfs": {"path": source_rootfs.name, "size": source_size, "sha256": source_hash},
            "outputRootfs": {"path": output_rootfs.name, "size": output_size, "sha256": output_hash, "copyOnly": True},
            "helper": {"guestPath": HELPER_GUEST_PATH, "size": helper_size, "sha256": helper_hash, "format": "ELF64/AArch64 ET_EXEC", "requires": ["no-PT_INTERP", "PT_DYNAMIC-permitted-only-with-DT_NULL-and-no-DT_NEEDED"], "sourceRead": "nofollow-single-fd-copy-to-staged-helper"},
            "init": {"guestPath": INIT_GUEST_PATH, "size": init_size, "sha256": init_hash, "mode": "0755"},
            "bootContract": {"kernelAppend": "init=/init", "device": BLOCK_DEVICE_PATH, "controlDevice": CONTROL_DEVICE_PATH, "sector": 0, "deviceWait": {"blockDevice": BLOCK_DEVICE_PATH, "controlDevice": CONTROL_DEVICE_PATH, "maxSeconds": DEVICE_WAIT_SECONDS, "pollSeconds": 1, "timeoutAction": "exit-1"}, "helperInvocation": f"{HELPER_GUEST_PATH} --nonce {nonce} --device {BLOCK_DEVICE_PATH} --control-device {CONTROL_DEVICE_PATH} --sector 0 --hold-ms {HELPER_HOLD_MILLISECONDS}", "helperForeground": True, "guestConsoleOwnsControl": True, "competingShell": False},
            "controlledTool": {"name": "debugfs", "operations": ["rm-disposable-path", "write", "set-inode-mode", "dump-verify"]},
        }
    except Exception:
        if published:
            _unlink_owned(output_rootfs, _identity(staged_rootfs.lstat()))
        raise
    finally:
        shutil.rmtree(workspace, ignore_errors=False)


def _tool_path(value: str) -> str:
    resolved = shutil.which(value)
    if resolved is None:
        raise DisposableRootfsError(f"debugfs tool {value!r} was not found")
    return resolved


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create one no-overwrite disposable virtio DMA-effect Guest rootfs")
    parser.add_argument("--source-rootfs", required=True, type=Path)
    parser.add_argument("--helper", required=True, type=Path)
    parser.add_argument("--session-nonce", required=True)
    parser.add_argument("--output-rootfs", required=True, type=Path)
    parser.add_argument("--plan-output", required=True, type=Path)
    parser.add_argument("--debugfs", default="debugfs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.plan_output in {args.source_rootfs, args.helper, args.output_rootfs}:
            raise DisposableRootfsError("--plan-output must be distinct from all binary inputs and output rootfs")
        # Reject an already-published plan before any disposable image is made;
        # otherwise a no-overwrite manifest collision could leave an unindexed
        # image behind.
        _ensure_absent(args.plan_output, label="plan output")
        tool = _tool_path(args.debugfs)
        plan = prepare_rootfs(source_rootfs=args.source_rootfs, helper=args.helper, output_rootfs=args.output_rootfs, nonce=args.session_nonce, debugfs=tool)
        publish_new_file(args.plan_output, (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), error_type=DisposableRootfsError)
        return 0
    except (OSError, DisposableRootfsError) as error:
        print(f"disposable virtio DMA-effect rootfs preparation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
