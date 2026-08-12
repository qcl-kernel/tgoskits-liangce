#!/usr/bin/env python3
"""Fail-closed filesystem I/O for host VM carveout evidence."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path
from typing import Any


REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
ErrorType = type[Exception]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_preflight_payload(
    *,
    carveouts: list[dict[str, int | str]],
    dts_name: str,
    dts_bytes: bytes,
    vm_config_bytes: dict[str, bytes],
    compatible: str,
    alignment: int,
) -> dict[str, Any]:
    """Build the byte-bound, narrowly scoped preflight manifest."""

    return {
        "schemaVersion": 1,
        "artifactStatus": "preflight-only",
        "status": "carveout_artifacts_validated",
        "proofScope": "host-dts-vm-carveout-static-contract",
        "doesNotProve": [
            "host allocator excluded the carveout",
            "AxVM stage-2 maps the expected HPA",
            "passthrough DMA is isolated",
            "Linux and Zephyr have IP connectivity",
            "decoded DTS is the DTB supplied to AxVisor",
            "VM TOML was deserialized by the current AxVM build",
        ],
        "fixedContract": {
            "compatible": compatible,
            "alignment": f"{alignment:#x}",
            "mapping": "exact MapReserved GPA=HPA and size match",
        },
        "carveouts": [
            {
                "vmId": int(item["vmId"]),
                "hpa": f"{int(item['hpa']):#x}",
                "size": f"{int(item['size']):#x}",
                "end": f"{int(item['end']):#x}",
                "vmConfig": str(item["vmConfig"]),
            }
            for item in carveouts
        ],
        "sources": {
            "hostDts": {"path": dts_name, "sha256": _sha256(dts_bytes)},
            "vmConfigs": [
                {"path": name, "sha256": _sha256(data)}
                for name, data in sorted(vm_config_bytes.items())
            ],
        },
    }


def _is_reparse(info: os.stat_result) -> bool:
    return bool(int(getattr(info, "st_file_attributes", 0)) & REPARSE_FLAG)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return int(info.st_dev), int(info.st_ino)


def _checked_path(
    path: Path, *, directory: bool, label: str, error_type: ErrorType
) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise error_type(f"{label} must not be a symlink or reparse point")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise error_type(f"{label} has the wrong filesystem type")
    if _identity(info)[1] == 0:
        raise error_type(f"{label} has no stable filesystem identity")
    return info


def _check_directory_chain(directory: Path, error_type: ErrorType) -> None:
    for component in reversed([directory, *directory.parents]):
        _checked_path(
            component,
            directory=True,
            label=f"path component {component}",
            error_type=error_type,
        )


def _fsync_directory(directory: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not directory_flag:
        return
    descriptor = os.open(directory, os.O_RDONLY | directory_flag)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_regular_bytes(path: Path, *, label: str, error_type: ErrorType) -> bytes:
    """Read one stable regular file without following symlinks or reparse points."""

    _check_directory_chain(path.parent, error_type)
    initial = _checked_path(path, directory=False, label=label, error_type=error_type)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(initial) or _is_reparse(opened):
            raise error_type(f"{label} changed while it was opened")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            data = source.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    current = _checked_path(path, directory=False, label=label, error_type=error_type)
    if _identity(current) != _identity(initial):
        raise error_type(f"{label} changed while it was read")
    return data


def _unlink_owned(path: Path, identity: tuple[int, int], error_type: ErrorType) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    replaced = (
        stat.S_ISLNK(info.st_mode) or _is_reparse(info) or _identity(info) != identity
    )
    if replaced:
        raise error_type(f"refused to unlink unowned path {path.name}")
    path.unlink()


def publish_new_file(path: Path, data: bytes, *, error_type: ErrorType) -> None:
    """Stage fully, then atomically hard-link a single no-overwrite manifest."""

    _check_directory_chain(path.parent, error_type)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(existing.st_mode) or _is_reparse(existing):
            raise error_type("output must not be a symlink or reparse point")
        raise error_type(f"output {path.name!r} already exists")

    staging = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(16)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(staging, flags, 0o600)
    except FileExistsError as error:
        raise error_type(f"staging output {staging.name!r} already exists") from error
    identity = _identity(os.fstat(descriptor))
    published = False
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as destination:
            descriptor = -1
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
            if _identity(os.fstat(destination.fileno())) != identity:
                raise error_type("staging output identity changed while writing")
        staged = _checked_path(
            staging,
            directory=False,
            label="staging output",
            error_type=error_type,
        )
        if _identity(staged) != identity:
            raise error_type("staging output identity changed")
        _check_directory_chain(path.parent, error_type)
        try:
            os.link(staging, path, follow_symlinks=False)
        except FileExistsError as error:
            raise error_type(f"output {path.name!r} already exists") from error
        published = True
        final = _checked_path(
            path,
            directory=False,
            label="published output",
            error_type=error_type,
        )
        if _identity(final) != identity:
            raise error_type("published output identity changed")
    except Exception:
        if published:
            _unlink_owned(path, identity, error_type)
        _unlink_owned(staging, identity, error_type)
        raise
    else:
        try:
            _unlink_owned(staging, identity, error_type)
            _fsync_directory(path.parent)
        except Exception:
            _unlink_owned(path, identity, error_type)
            raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
