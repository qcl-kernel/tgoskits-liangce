#!/usr/bin/env python3
"""Fail-closed filesystem primitives for Guest DTB capture evidence."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any


MAX_CAPTURE_PLAN_BYTES = 4 * 1024 * 1024
MAX_SOURCE_LOG_BYTES = 64 * 1024 * 1024
CAPTURE_LOCK_NAME = ".guest-dtb-qmp-capture.lock"
REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


class GuestDtbCaptureError(ValueError):
    """A capture input or filesystem transition violates the evidence contract."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity(info: os.stat_result) -> tuple[int, int]:
    identity = (int(info.st_dev), int(info.st_ino))
    if identity[1] == 0:
        raise GuestDtbCaptureError("filesystem does not expose stable file identities")
    return identity


def _is_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0))
    return bool(REPARSE_FLAG and attributes & REPARSE_FLAG)


def checked_lstat(path: Path, *, field: str, kind: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise GuestDtbCaptureError(f"could not inspect {field}: {error}") from error
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise GuestDtbCaptureError(f"{field} must not be a symlink or reparse point")
    expected = {
        "file": stat.S_ISREG,
        "directory": stat.S_ISDIR,
        "socket": stat.S_ISSOCK,
    }[kind]
    if not expected(info.st_mode):
        raise GuestDtbCaptureError(f"{field} is not a regular {kind}")
    _identity(info)
    return info


def _reject_reparse_chain(directory: Path) -> None:
    for entry in reversed([directory, *directory.parents]):
        checked_lstat(entry, field=f"path component {entry}", kind="directory")


def _open_flags(*, writable: bool = False) -> int:
    flags = os.O_RDWR if writable else os.O_RDONLY
    return flags | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


def read_bounded_regular_file(
    path: Path, *, byte_limit: int, field: str
) -> tuple[bytes, tuple[int, int]]:
    initial = checked_lstat(path, field=field, kind="file")
    initial_id = _identity(initial)
    try:
        descriptor = os.open(path, _open_flags())
    except OSError as error:
        raise GuestDtbCaptureError(f"could not open {field}: {error}") from error
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != initial_id or _is_reparse(opened):
            raise GuestDtbCaptureError(f"{field} changed while it was opened")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            data = source.read(byte_limit + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > byte_limit:
        raise GuestDtbCaptureError(f"{field} exceeds byte limit {byte_limit}")
    current = checked_lstat(path, field=field, kind="file")
    if _identity(current) != initial_id:
        raise GuestDtbCaptureError(f"{field} changed while it was read")
    return data, initial_id


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GuestDtbCaptureError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_json_constant(value: str) -> Any:
    raise GuestDtbCaptureError(f"non-finite JSON constant is forbidden: {value}")


def decode_capture_plan(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_CAPTURE_PLAN_BYTES:
        raise GuestDtbCaptureError(
            f"capture plan exceeds byte limit {MAX_CAPTURE_PLAN_BYTES}"
        )
    try:
        plan = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_json_object,
            parse_constant=reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise GuestDtbCaptureError(f"capture plan JSON is malformed: {error}") from error
    if not isinstance(plan, dict):
        raise GuestDtbCaptureError("capture plan root is not an object")
    return plan


def read_capture_plan(path: Path) -> tuple[dict[str, Any], str]:
    data, _ = read_bounded_regular_file(
        path, byte_limit=MAX_CAPTURE_PLAN_BYTES, field="capture plan"
    )
    return decode_capture_plan(data), sha256_bytes(data)


def path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise GuestDtbCaptureError(f"could not inspect path {path}: {error}") from error
    return True


def _write_new_file(path: Path, data: bytes) -> tuple[int, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as destination:
            descriptor = -1
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
            identity = _identity(os.fstat(destination.fileno()))
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return identity


def _unlink_owned(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(current.st_mode) or _is_reparse(current):
        raise GuestDtbCaptureError(f"refused to unlink replaced path {path.name}")
    if _identity(current) != identity:
        raise GuestDtbCaptureError(f"refused to unlink unowned path {path.name}")
    path.unlink()


def _publish_owned(
    staging: Path, final: Path, identity: tuple[int, int]
) -> tuple[int, int]:
    current = checked_lstat(staging, field="owned staging file", kind="file")
    if _identity(current) != identity:
        raise GuestDtbCaptureError(f"staging ownership changed: {staging.name}")
    if path_exists(final):
        raise GuestDtbCaptureError(f"evidence file {final.name} already exists")
    try:
        os.link(staging, final, follow_symlinks=False)
    except (NotImplementedError, OSError) as error:
        raise GuestDtbCaptureError(
            f"could not atomically publish evidence {final.name}: {error}"
        ) from error
    try:
        published = checked_lstat(final, field="published file", kind="file")
        if _identity(published) != identity:
            raise GuestDtbCaptureError(f"published ownership changed: {final.name}")
        _unlink_owned(staging, identity)
    except Exception:
        _unlink_owned(final, identity)
        raise
    return identity


@contextmanager
def _capture_lock(directory: Path) -> Any:
    lock_path = directory / CAPTURE_LOCK_NAME
    try:
        identity = _write_new_file(lock_path, f"pid={os.getpid()}\n".encode("ascii"))
    except FileExistsError as error:
        raise GuestDtbCaptureError(f"capture lock {CAPTURE_LOCK_NAME} exists") from error
    try:
        yield
    finally:
        try:
            _unlink_owned(lock_path, identity)
        except OSError as error:
            raise GuestDtbCaptureError(f"capture lock cleanup failed: {error}") from error


def _fsync_directory(directory: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FilesystemCaptureStorage:
    """Own private staging files by stable filesystem identity."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.owned: dict[Path, tuple[int, int]] = {}
        self.staging_directory: Path | None = None
        self.staging_identity: tuple[int, int] | None = None

    @contextmanager
    def transaction(self) -> Any:
        with _capture_lock(self.directory):
            raw = tempfile.mkdtemp(prefix=".guest-dtb-qmp-", dir=self.directory)
            stage = Path(raw)
            os.chmod(stage, 0o700)
            info = checked_lstat(stage, field="private staging directory", kind="directory")
            self.staging_directory = stage
            self.staging_identity = _identity(info)
            try:
                yield stage
            finally:
                current = checked_lstat(
                    stage, field="private staging directory", kind="directory"
                )
                if _identity(current) != self.staging_identity:
                    raise GuestDtbCaptureError("private staging ownership changed")
                try:
                    os.rmdir(stage)
                    _fsync_directory(self.directory)
                except OSError as error:
                    raise GuestDtbCaptureError(
                        f"private staging cleanup failed: {error}"
                    ) from error

    def ensure_absent(self, paths: list[Path]) -> None:
        for path in paths:
            if path_exists(path):
                raise GuestDtbCaptureError(f"capture path {path.name} already exists")

    def validate_staging(self, path: Path, expected_size: int) -> str:
        if self.staging_directory is None or path.parent != self.staging_directory:
            raise GuestDtbCaptureError("QMP staging path escapes its private directory")
        initial = checked_lstat(path, field="QMP staging file", kind="file")
        identity = _identity(initial)
        descriptor = os.open(path, _open_flags(writable=True))
        try:
            if _identity(os.fstat(descriptor)) != identity:
                raise GuestDtbCaptureError("QMP staging file changed while opened")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                descriptor = -1
                data = source.read(expected_size + 1)
                os.fsync(source.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self.owned[path] = identity
        current = checked_lstat(path, field="QMP staging file", kind="file")
        if _identity(current) != identity:
            raise GuestDtbCaptureError("QMP staging file changed while hashed")
        if len(data) != expected_size:
            raise GuestDtbCaptureError(
                f"evidence file {path.name} has {len(data)} bytes, expected {expected_size}"
            )
        return sha256_bytes(data)

    def write_staging(self, path: Path, data: bytes) -> None:
        self.owned[path] = _write_new_file(path, data)

    def publish(
        self,
        staging: Path,
        final: Path,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> None:
        identity = self.owned.get(staging)
        if identity is None:
            raise GuestDtbCaptureError(f"staging file is not owned: {staging.name}")
        if expected_size is not None:
            digest = self.validate_staging(staging, expected_size)
            if expected_sha256 is None or digest != expected_sha256:
                raise GuestDtbCaptureError(
                    f"staging digest changed before publication: {staging.name}"
                )
        self.owned[final] = _publish_owned(staging, final, identity)
        del self.owned[staging]

    def cleanup(self, path: Path) -> None:
        identity = self.owned.get(path)
        if identity is None:
            return
        _unlink_owned(path, identity)
        del self.owned[path]

    def sync_directory(self) -> None:
        _fsync_directory(self.directory)


def publish_json_new(payload: dict[str, Any], output_path: Path) -> None:
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    publish_bytes_new(data, output_path)


def publish_bytes_new(data: bytes, output_path: Path) -> None:
    """Publish immutable evidence bytes without replacing an existing path."""

    if not isinstance(data, bytes):
        raise GuestDtbCaptureError("capture output payload must be bytes")
    if path_exists(output_path):
        raise GuestDtbCaptureError(f"capture output {output_path.name} already exists")
    staging = output_path.parent / f".{output_path.name}.{secrets.token_hex(16)}.tmp"
    identity: tuple[int, int] | None = None
    try:
        identity = _write_new_file(staging, data)
        _publish_owned(staging, output_path, identity)
        identity = None
        _fsync_directory(output_path.parent)
    finally:
        if identity is not None:
            _unlink_owned(staging, identity)


def prepare_cli_paths(plan_arg: Path, output_arg: Path) -> tuple[Path, Path]:
    plan = Path(os.path.abspath(os.fspath(plan_arg)))
    output = Path(os.path.abspath(os.fspath(output_arg)))
    if plan.parent != output.parent:
        raise GuestDtbCaptureError("capture plan and --output must share a directory")
    _reject_reparse_chain(plan.parent)
    checked_lstat(plan, field="capture plan", kind="file")
    if path_exists(output):
        raise GuestDtbCaptureError("capture output already exists or is a link")
    directory = plan.parent.resolve(strict=True)
    return directory / plan.name, directory / output.name
