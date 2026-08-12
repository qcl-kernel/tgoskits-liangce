#!/usr/bin/env python3
"""Prepare a no-overwrite QEMU topology and read-only virtio-blk probe disk.

The generated topology is deliberately not a runner.  It prepares one
single-Guest outer-QEMU TOML, a deterministic sector-zero raw disk, and a
nonce-bound plan, but never starts QEMU or submits a virtio request.  The
runner supplies QMP/pidfile/name identity arguments separately.
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
import sys
from pathlib import Path

from host_vm_carveout_io import publish_new_file


NONCE = re.compile(r"[0-9a-f]{32}\Z")
SECTOR_BYTES = 512
REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
FORBIDDEN_QEMU_PATH_BYTES = {",", "\0", "\r", "\n"}
CONTROL_CHARDEV_ID = "dma-go-chardev"
CONTROL_SERIAL_ID = "dma-go-serial"
CONTROL_PORT_ID = "dma-go-port"
CONTROL_PORT_NAME = "dma-go"
CONTROL_BUS = "virtio-mmio-bus.2"
CONTROL_GUEST_PATH = "/dev/hvc0"


class VirtioDmaQemuPreparationError(ValueError):
    """Inputs cannot safely form a disposable virtio-blk QEMU topology."""


def prepare_qemu_topology(
    *, host_dtb: Path, disposable_rootfs: Path, nonce: str, config_output: Path,
    probe_disk_output: Path, plan_output: Path,
) -> dict[str, object]:
    """Publish a QEMU config, one sector disk, and their immutable plan."""

    _validate_nonce(nonce)
    paths = {
        "host DTB": host_dtb,
        "disposable rootfs": disposable_rootfs,
        "QEMU config output": config_output,
        "probe disk output": probe_disk_output,
        "plan output": plan_output,
    }
    for label, path in paths.items():
        _validate_absolute_qemu_path(path, label=label)
    if len({str(path) for path in paths.values()}) != len(paths):
        raise VirtioDmaQemuPreparationError("all input and output paths must be distinct")
    host_dtb_size, host_dtb_hash = _sha256_file(host_dtb, label="host DTB")
    rootfs_size, rootfs_hash = _sha256_file(disposable_rootfs, label="disposable rootfs")
    if host_dtb_size == 0 or rootfs_size == 0:
        raise VirtioDmaQemuPreparationError("host DTB and disposable rootfs must not be empty")
    for label, path in (
        ("QEMU config output", config_output),
        ("probe disk output", probe_disk_output),
        ("plan output", plan_output),
    ):
        _ensure_absent(path, label=label)

    probe_payload = _probe_payload(nonce=nonce, host_dtb_hash=host_dtb_hash, rootfs_hash=rootfs_hash)
    if probe_payload == b"\xa5" * SECTOR_BYTES:
        raise VirtioDmaQemuPreparationError("probe payload must differ from the helper's 0xa5 buffer")
    config_bytes = _qemu_config_bytes(
        host_dtb=host_dtb,
        disposable_rootfs=disposable_rootfs,
        probe_disk=probe_disk_output,
        control_socket=_control_socket(nonce),
    )

    workspace = config_output.parent / f".virtio-dma-qemu-{os.getpid()}-{secrets.token_hex(16)}"
    workspace.mkdir(mode=0o755)
    staged_config = workspace / "qemu.toml"
    staged_probe_disk = workspace / "probe.raw"
    published: list[tuple[Path, tuple[int, int]]] = []
    try:
        _write_new(staged_config, config_bytes)
        _write_new(staged_probe_disk, probe_payload)
        config_size, config_hash = _sha256_file(staged_config, label="generated QEMU config")
        probe_size, probe_hash = _sha256_file(staged_probe_disk, label="generated probe disk")
        if probe_size != SECTOR_BYTES:
            raise VirtioDmaQemuPreparationError("probe disk must contain exactly one 512-byte sector")

        _publish_hard_link(staged_config, config_output, label="QEMU config output", published=published)
        _publish_hard_link(staged_probe_disk, probe_disk_output, label="probe disk output", published=published)
        plan = {
            "schemaVersion": 1,
            "artifactStatus": "prepared-qemu-topology-only",
            "status": "single_guest_virtio_blk_dma_effect_qemu_prepared",
            "proofScope": "one-configured-single-guest-root-and-read-only-probe-virtio-blk-topology",
            "doesNotProve": [
                "the QEMU configuration was launched",
                "the disposable rootfs booted",
                "the Guest helper emitted READY, received GO, or emitted DONE",
                "a virtio-blk request was submitted or completed",
                "DMA was executed",
                "the observed bytes were written by a hardware DMA engine",
                "DMA isolation",
                "dual-Guest execution",
                "Linux and Zephyr IP connectivity",
            ],
            "sessionNonce": nonce,
            "inputs": {
                "hostDtb": _artifact(host_dtb, host_dtb_size, host_dtb_hash),
                "disposableRootfs": _artifact(disposable_rootfs, rootfs_size, rootfs_hash),
            },
            "outputs": {
                "qemuConfig": _artifact(config_output, config_size, config_hash),
                "probeDisk": _artifact(probe_disk_output, probe_size, probe_hash),
            },
            "expectedPayload": {
                "path": str(probe_disk_output),
                "sector": 0,
                "size": SECTOR_BYTES,
                "sha256": probe_hash,
                "mustDifferFrom": "a5-repeated-512-byte-helper-buffer",
            },
            "qemu": {
                "hostDtbOption": str(host_dtb),
                "root": {
                    "driveId": "disk0",
                    "deviceId": "linux-root",
                    "bus": "virtio-mmio-bus.0",
                    "guestDevice": "/dev/vda",
                    "rootfsPatchedBy": "cargo xtask qemu --rootfs replaces only disk0 file=",
                },
                "probe": {
                    "driveId": "dma-probe-disk",
                    "deviceId": "dma-probe",
                    "bus": "virtio-mmio-bus.1",
                    "guestDevice": "/dev/vdb",
                    "readonly": True,
                    "sector": 0,
                },
                "control": {
                    "transport": "virtio-console",
                    "chardevId": CONTROL_CHARDEV_ID,
                    "socket": str(_control_socket(nonce)),
                    "server": True,
                    "wait": False,
                    "serialDeviceId": CONTROL_SERIAL_ID,
                    "serialDeviceBus": CONTROL_BUS,
                    "portDeviceId": CONTROL_PORT_ID,
                    "portName": CONTROL_PORT_NAME,
                    "guestPath": CONTROL_GUEST_PATH,
                    "stdioControlForbidden": True,
                },
                "kernelAppend": "root=/dev/vda rw init=/init",
                "runnerInjectedOptions": ["-qmp", "-pidfile", "-name"],
                "staticNameForbidden": True,
            },
        }
        publish_new_file(
            plan_output,
            (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            error_type=VirtioDmaQemuPreparationError,
        )
        return plan
    except Exception:
        for path, identity in reversed(published):
            _unlink_owned(path, identity)
        raise
    finally:
        shutil.rmtree(workspace, ignore_errors=False)


def _qemu_config_bytes(
    *, host_dtb: Path, disposable_rootfs: Path, probe_disk: Path, control_socket: str
) -> bytes:
    args = [
        "-nographic",
        "-cpu",
        "cortex-a72",
        "-machine",
        "virt,virtualization=on,gic-version=3",
        "-smp",
        "4",
        "-m",
        "8g",
        "-dtb",
        str(host_dtb),
        "-drive",
        f"id=disk0,if=none,format=raw,file={disposable_rootfs}",
        "-device",
        "virtio-blk-device,id=linux-root,drive=disk0,bus=virtio-mmio-bus.0",
        "-drive",
        f"id=dma-probe-disk,if=none,format=raw,readonly=on,file={probe_disk}",
        "-device",
        "virtio-blk-device,id=dma-probe,drive=dma-probe-disk,bus=virtio-mmio-bus.1",
        "-chardev",
        f"socket,id={CONTROL_CHARDEV_ID},path={control_socket},server=on,wait=off",
        "-device",
        f"virtio-serial-device,id={CONTROL_SERIAL_ID},bus={CONTROL_BUS}",
        "-device",
        f"virtconsole,id={CONTROL_PORT_ID},chardev={CONTROL_CHARDEV_ID},name={CONTROL_PORT_NAME}",
        "-append",
        "root=/dev/vda rw init=/init",
    ]
    forbidden = {"-name", "-serial", "-monitor"}
    if any(
        argument in forbidden
        or any(argument.startswith(option + "=") for option in forbidden)
        or "stdio" in argument.lower()
        for argument in args
    ):
        raise VirtioDmaQemuPreparationError(
            "static QEMU configuration must not contain name, stdio control, serial, or monitor arguments"
        )
    lines = ["# Generated by prepare_virtio_dma_effect_qemu.py; runner injects QMP/pidfile/name.", "args = ["]
    lines.extend(f"  {json.dumps(argument, ensure_ascii=True)}," for argument in args)
    lines.extend([
        "]",
        "",
        "fail_regex = [",
        '  "(?i)Failed to initialize guest VM",',
        '  "(?i)\\\\bpanic(?:ked)?\\\\b",',
        '  "(?i)kernel panic",',
        "]",
        "success_regex = []",
        "to_bin = true",
        "uefi = false",
        "",
    ])
    return "\n".join(lines).encode("utf-8")


def _probe_payload(*, nonce: str, host_dtb_hash: str, rootfs_hash: str) -> bytes:
    prefix = (
        b"TGOS-VIRTIO-BLK-DMA-EFFECT-V1\n"
        + f"nonce={nonce}\n".encode("ascii")
        + f"host_dtb_sha256={host_dtb_hash}\n".encode("ascii")
        + f"rootfs_sha256={rootfs_hash}\n".encode("ascii")
    )
    payload = bytearray(prefix)
    counter = 0
    while len(payload) < SECTOR_BYTES:
        payload.extend(hashlib.sha256(prefix + counter.to_bytes(8, "big")).digest())
        counter += 1
    return bytes(payload[:SECTOR_BYTES])


def _control_socket(nonce: str) -> str:
    """Return the bounded, nonce-private socket which the live runner owns."""

    _validate_nonce(nonce)
    socket = f"/tmp/axdma-{nonce}/control.sock"
    if len(socket.encode("utf-8")) > 100:
        raise VirtioDmaQemuPreparationError("control socket exceeds the bounded Unix-socket length")
    return socket


def _validate_nonce(nonce: str) -> None:
    if NONCE.fullmatch(nonce) is None:
        raise VirtioDmaQemuPreparationError("--session-nonce must be 128-bit lower-case hexadecimal")


def _validate_absolute_qemu_path(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise VirtioDmaQemuPreparationError(f"{label} path must be absolute")
    try:
        value = str(path)
        value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise VirtioDmaQemuPreparationError(f"{label} path must be UTF-8") from error
    if any(character in value for character in FORBIDDEN_QEMU_PATH_BYTES):
        raise VirtioDmaQemuPreparationError(f"{label} path contains a forbidden QEMU delimiter")


def _artifact(path: Path, size: int, digest: str) -> dict[str, object]:
    return {"path": str(path), "size": size, "sha256": digest}


def _sha256_file(path: Path, *, label: str) -> tuple[int, str]:
    before = _regular(path, label=label)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if _identity(os.fstat(descriptor)) != _identity(before):
            raise VirtioDmaQemuPreparationError(f"{label} changed while it was opened")
        digest = hashlib.sha256()
        size = 0
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
            size += len(block)
    finally:
        os.close(descriptor)
    if _identity(_regular(path, label=label)) != _identity(before):
        raise VirtioDmaQemuPreparationError(f"{label} changed while it was read")
    return size, digest.hexdigest()


def _regular(path: Path, *, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise VirtioDmaQemuPreparationError(f"{label} does not exist") from error
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise VirtioDmaQemuPreparationError(f"{label} must be a regular non-link file")
    return info


def _directory_chain(path: Path) -> None:
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise VirtioDmaQemuPreparationError(f"output directory component {item} is unsafe")


def _ensure_absent(path: Path, *, label: str) -> None:
    _directory_chain(path.parent)
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise VirtioDmaQemuPreparationError(f"{label} {path.name!r} already exists")


def _write_new(path: Path, content: bytes) -> None:
    _ensure_absent(path, label="staging output")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_hard_link(
    source: Path, destination: Path, *, label: str, published: list[tuple[Path, tuple[int, int]]],
) -> None:
    _ensure_absent(destination, label=label)
    source_identity = _identity(_regular(source, label="staging output"))
    os.link(source, destination, follow_symlinks=False)
    destination_identity = _identity(_regular(destination, label=label))
    if destination_identity != source_identity:
        raise VirtioDmaQemuPreparationError(f"{label} identity differs from staged bytes")
    published.append((destination, destination_identity))


def _unlink_owned(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(current.st_mode) or _is_reparse(current) or _identity(current) != identity:
        raise VirtioDmaQemuPreparationError(f"refused to remove replaced output {path.name}")
    path.unlink()


def _identity(info: os.stat_result) -> tuple[int, int]:
    return int(info.st_dev), int(info.st_ino)


def _is_reparse(info: os.stat_result) -> bool:
    return bool(int(getattr(info, "st_file_attributes", 0)) & REPARSE_FLAG)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare no-overwrite QEMU virtio DMA-effect topology inputs")
    parser.add_argument("--host-dtb", required=True, type=Path)
    parser.add_argument("--disposable-rootfs", required=True, type=Path)
    parser.add_argument("--session-nonce", required=True)
    parser.add_argument("--qemu-config-output", required=True, type=Path)
    parser.add_argument("--probe-disk-output", required=True, type=Path)
    parser.add_argument("--plan-output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        prepare_qemu_topology(
            host_dtb=args.host_dtb,
            disposable_rootfs=args.disposable_rootfs,
            nonce=args.session_nonce,
            config_output=args.qemu_config_output,
            probe_disk_output=args.probe_disk_output,
            plan_output=args.plan_output,
        )
        return 0
    except (OSError, VirtioDmaQemuPreparationError) as error:
        print(f"virtio DMA-effect QEMU preparation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
