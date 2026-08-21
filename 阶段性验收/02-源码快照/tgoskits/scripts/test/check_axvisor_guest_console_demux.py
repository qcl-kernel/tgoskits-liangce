#!/usr/bin/env python3
"""Behavioral contract for strict AxVisor guest-console frame demuxing."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEMUX = WORKSPACE_ROOT / "scripts/contest/demux_guest_console_frames.py"


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frame(
    *,
    vm: int,
    name: str,
    generation: int,
    sequence: int,
    payload: bytes,
    total: int,
    dropped: int = 0,
    dma: int = 0,
) -> bytes:
    return (
        "AXVISOR_GUEST_CONSOLE_FRAME "
        f"v=1 vm={vm} name={name} gen={generation} seq={sequence} "
        f"len={len(payload)} total={total} dropped={dropped} dma={dma} "
        f"hex={payload.hex()}"
    ).encode("ascii")


def valid_host_log() -> tuple[bytes, dict[int, bytes]]:
    linux_0a = b"linux partial"
    linux_0b = b"\nready\x00\xff"
    linux_1 = b"linux reset\n"
    zephyr_0a = b"zephyr "
    zephyr_0b = b"ready\n"
    lines = [
        b"[host] AxVisor startup noise is not guest evidence",
        frame(
            vm=1,
            name="linux",
            generation=0,
            sequence=0,
            payload=linux_0a,
            total=len(linux_0a),
        ),
        frame(
            vm=2,
            name="zephyr",
            generation=0,
            sequence=0,
            payload=zephyr_0a,
            total=len(zephyr_0a),
        ),
        frame(
            vm=1,
            name="linux",
            generation=0,
            sequence=1,
            payload=linux_0b,
            total=len(linux_0a) + len(linux_0b),
        ),
        frame(
            vm=2,
            name="zephyr",
            generation=0,
            sequence=1,
            payload=zephyr_0b,
            total=len(zephyr_0a) + len(zephyr_0b),
        ),
        frame(
            vm=1,
            name="linux",
            generation=1,
            sequence=0,
            payload=linux_1,
            total=len(linux_1),
        ),
        b"[host] shutdown noise",
    ]
    return b"\r\n".join(lines) + b"\r\n", {
        1: linux_0a + linux_0b + linux_1,
        2: zephyr_0a + zephyr_0b,
    }


class MemoryDemuxStorage:
    def __init__(self, error_type: type[Exception]) -> None:
        self.error_type = error_type
        self.files: dict[str, bytes] = {}
        self.publication_order: list[str] = []
        self.locked = False
        self.fail_publish_name: str | None = None

    @staticmethod
    def key(path: Path | str) -> str:
        return str(Path(path).resolve())

    @contextmanager
    def lock(self):
        if self.locked:
            raise RuntimeError("demux storage lock is already held")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def ensure_absent(self, paths: list[Path]) -> None:
        for path in paths:
            if self.key(path) in self.files:
                raise self.error_type(f"demux path {path.name} already exists")

    def write_staging(self, path: Path, data: bytes) -> None:
        key = self.key(path)
        if key in self.files:
            raise self.error_type(f"staging path {path.name} already exists")
        self.files[key] = data

    def publish(self, staging_path: Path, final_path: Path) -> None:
        if final_path.name == self.fail_publish_name:
            raise self.error_type(f"injected publication failure for {final_path.name}")
        staging_key = self.key(staging_path)
        final_key = self.key(final_path)
        if staging_key not in self.files or final_key in self.files:
            raise self.error_type("invalid in-memory publication")
        self.files[final_key] = self.files.pop(staging_key)
        self.publication_order.append(final_key)

    def cleanup(self, path: Path) -> None:
        self.files.pop(self.key(path), None)

    def contains(self, path: Path) -> bool:
        return self.key(path) in self.files


def expect_rejected(
    errors: list[str],
    demux: ModuleType,
    source: bytes,
    *,
    label: str,
    expected_message: str,
) -> None:
    try:
        demux.demux_host_log(source, {1: "linux", 2: "zephyr"})
    except demux.GuestConsoleDemuxError as error:
        if expected_message not in str(error):
            errors.append(f"demux reports the wrong {label} error: {error}")
    except Exception as error:  # noqa: BLE001 - preserve diagnosis.
        errors.append(f"demux leaks a raw {label} exception: {error}")
    else:
        errors.append(f"demux accepts {label}")


def invalid_host_logs() -> list[tuple[str, bytes, str]]:
    vm2 = frame(
        vm=2,
        name="zephyr",
        generation=0,
        sequence=0,
        payload=b"z",
        total=1,
    )
    vm1_first = frame(
        vm=1,
        name="linux",
        generation=0,
        sequence=0,
        payload=b"a",
        total=1,
    )
    return [
        (
            "sequence gap",
            b"\n".join(
                [
                    vm1_first,
                    frame(
                        vm=1,
                        name="linux",
                        generation=0,
                        sequence=2,
                        payload=b"b",
                        total=2,
                    ),
                    vm2,
                ]
            ),
            "expected sequence 1",
        ),
        (
            "payload length mismatch",
            b"\n".join(
                [
                    vm1_first.replace(b"len=1", b"len=2"),
                    vm2,
                ]
            ),
            "payload length 1 does not match declared length 2",
        ),
        (
            "dropped bytes",
            b"\n".join(
                [
                    frame(
                        vm=1,
                        name="linux",
                        generation=0,
                        sequence=0,
                        payload=b"a",
                        total=1,
                        dropped=1,
                    ),
                    vm2,
                ]
            ),
            "reports 1 dropped bytes",
        ),
        (
            "DMA attempt",
            b"\n".join(
                [
                    frame(
                        vm=1,
                        name="linux",
                        generation=0,
                        sequence=0,
                        payload=b"a",
                        total=1,
                        dma=1,
                    ),
                    vm2,
                ]
            ),
            "reports 1 DMA enable attempts",
        ),
        (
            "generation gap",
            b"\n".join(
                [
                    vm1_first,
                    frame(
                        vm=1,
                        name="linux",
                        generation=2,
                        sequence=0,
                        payload=b"b",
                        total=1,
                    ),
                    vm2,
                ]
            ),
            "expected generation 0 or 1",
        ),
        (
            "nonzero first sequence after generation switch",
            b"\n".join(
                [
                    vm1_first,
                    frame(
                        vm=1,
                        name="linux",
                        generation=1,
                        sequence=1,
                        payload=b"b",
                        total=1,
                    ),
                    vm2,
                ]
            ),
            "generation 1 must start at sequence 0",
        ),
        (
            "stale generation after switch",
            b"\n".join(
                [
                    vm1_first,
                    frame(
                        vm=1,
                        name="linux",
                        generation=1,
                        sequence=0,
                        payload=b"b",
                        total=1,
                    ),
                    frame(
                        vm=1,
                        name="linux",
                        generation=0,
                        sequence=1,
                        payload=b"c",
                        total=2,
                    ),
                    vm2,
                ]
            ),
            "stale generation 0 after generation 1",
        ),
        (
            "cumulative total mismatch",
            b"\n".join(
                [
                    vm1_first,
                    frame(
                        vm=1,
                        name="linux",
                        generation=0,
                        sequence=1,
                        payload=b"b",
                        total=3,
                    ),
                    vm2,
                ]
            ),
            "declares total 3, expected 2",
        ),
        (
            "unexpected VM",
            b"\n".join(
                [
                    vm1_first,
                    vm2,
                    frame(
                        vm=3,
                        name="other",
                        generation=0,
                        sequence=0,
                        payload=b"x",
                        total=1,
                    ),
                ]
            ),
            "unexpected VM 3",
        ),
        (
            "missing expected VM",
            vm1_first,
            "missing frames for expected VM 2",
        ),
        (
            "embedded frame marker",
            b"host-prefix " + vm1_first + b"\n" + vm2,
            "frame marker is not at the start of its line",
        ),
        (
            "ANSI-prefixed frame marker",
            b"\x1b[m" + vm1_first + b"\n" + vm2,
            "frame marker is not at the start of its line",
        ),
    ]


def check_valid_demux(errors: list[str], demux: ModuleType) -> object | None:
    source, expected_bytes = valid_host_log()
    try:
        result = demux.demux_host_log(source, {1: "linux", 2: "zephyr"})
    except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
        errors.append(f"demux rejects valid interleaved frames: {error}")
        return None

    if result.frame_count != 5:
        errors.append(f"demux reports {result.frame_count} frames instead of 5")
    if set(result.guests) != {1, 2}:
        errors.append("demux omits or invents a VM")
        return result
    for vm_id, expected in expected_bytes.items():
        if result.guests[vm_id].data != expected:
            errors.append(f"demux changes VM {vm_id} binary console bytes")
    linux_generations = result.guests[1].generations
    if [item.generation for item in linux_generations] != [0, 1]:
        errors.append("demux does not preserve the Linux generation switch")
    elif (
        linux_generations[0].first_sequence != 0
        or linux_generations[0].last_sequence != 1
        or linux_generations[1].first_sequence != 0
        or linux_generations[1].last_sequence != 0
    ):
        errors.append("demux records wrong per-generation sequence boundaries")
    return result


def check_publication(
    errors: list[str], demux: ModuleType, result: object
) -> None:
    source, expected_bytes = valid_host_log()
    directory = (WORKSPACE_ROOT / "virtual-console-evidence").resolve()
    manifest_path = directory / "console-manifest.json"
    storage = MemoryDemuxStorage(demux.GuestConsoleDemuxError)
    try:
        manifest = demux.publish_demux_evidence(
            result,
            output_directory=directory,
            host_log_name="host.log",
            host_log_data=source,
            storage=storage,
        )
    except Exception as error:  # noqa: BLE001 - preserve diagnosis.
        errors.append(f"demux cannot publish valid in-memory evidence: {error}")
        return

    for vm_id, expected in expected_bytes.items():
        output = directory / f"guest-vm-{vm_id}.console.log"
        if storage.files.get(storage.key(output)) != expected:
            errors.append(f"demux publishes wrong VM {vm_id} raw console bytes")
    if not storage.contains(manifest_path):
        errors.append("demux omits console-manifest.json")
    elif not storage.publication_order or storage.publication_order[-1] != storage.key(
        manifest_path
    ):
        errors.append("demux does not publish its manifest last")
    if any(".demux-part" in path for path in storage.files):
        errors.append("successful demux leaves staging evidence behind")
    if manifest.get("artifactStatus") != "host-log-derived-unreviewed":
        errors.append("demux overstates the artifact status")
    if manifest.get("status") != "console_frames_demuxed":
        errors.append("demux changes its narrow success status")
    if manifest.get("source", {}).get("hostLog", {}).get("sha256") != sha256(source):
        errors.append("demux manifest has the wrong host-log SHA-256")
    boundaries = set(manifest.get("doesNotProve", []))
    for boundary in (
        "the frames were produced by a real AxVisor execution",
        "either guest booted",
        "guest PL011 MMIO accesses occurred",
        "passthrough DMA is isolated",
        "Linux and Zephyr have IP connectivity",
    ):
        if boundary not in boundaries:
            errors.append(f"demux manifest omits proof boundary `{boundary}`")

    no_overwrite = MemoryDemuxStorage(demux.GuestConsoleDemuxError)
    protected = directory / "guest-vm-1.console.log"
    no_overwrite.files[no_overwrite.key(protected)] = b"preserve-me"
    try:
        demux.publish_demux_evidence(
            result,
            output_directory=directory,
            host_log_name="host.log",
            host_log_data=source,
            storage=no_overwrite,
        )
    except demux.GuestConsoleDemuxError as error:
        if "guest-vm-1.console.log already exists" not in str(error):
            errors.append(f"demux reports the wrong no-overwrite error: {error}")
    else:
        errors.append("demux overwrites an existing raw console output")
    if no_overwrite.files != {no_overwrite.key(protected): b"preserve-me"}:
        errors.append("no-overwrite failure changes or leaks evidence files")

    rollback = MemoryDemuxStorage(demux.GuestConsoleDemuxError)
    rollback.fail_publish_name = "console-manifest.json"
    try:
        demux.publish_demux_evidence(
            result,
            output_directory=directory,
            host_log_name="host.log",
            host_log_data=source,
            storage=rollback,
        )
    except demux.GuestConsoleDemuxError:
        pass
    else:
        errors.append("demux ignores an injected manifest publication failure")
    if rollback.files:
        errors.append("failed manifest-last publication leaves partial evidence")


def check_trailing_truncation(errors: list[str], demux: ModuleType) -> None:
    """A payload-short final frame is a shutdown artifact: rejected by default,
    tolerated and recorded only when explicitly allowed."""

    from check_axvisor_guest_console_demux import frame  # noqa: F401 - re-export

    good = b"complete frame payload"
    truncated = b"partial"
    source = (
        frame(
            vm=2,
            name="zephyr",
            generation=0,
            sequence=0,
            payload=good,
            total=len(good),
        )
        + b"\n"
        + b"AXVISOR_GUEST_CONSOLE_FRAME v=1 vm=2 name=zephyr gen=0 seq=1 len=%d total=%d dropped=0 dma=0 hex=%s"
        % (len(good) + len(truncated), len(good) + len(truncated), truncated.hex().encode())
        + b"\n"
    )
    try:
        demux.demux_host_log(source, {2: "zephyr"})
    except demux.GuestConsoleDemuxError:
        pass
    else:
        errors.append("trailing truncated frame is accepted without the explicit opt-in")

    try:
        result = demux.demux_host_log(
            source, {2: "zephyr"}, allow_trailing_truncation=True
        )
    except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
        errors.append(f"explicit trailing-truncation opt-in is rejected: {error}")
        return
    if result.frame_count != 1:
        errors.append("truncated trailing frame still counted as a frame")
    if not result.trailing_truncated or result.trailing_truncated.get("line") != 2:
        errors.append("trailing truncation is not recorded in the demux result")

    # An empty hex field on the final frame (cut mid-line at shutdown) is also
    # a trailing truncation, not a mid-log corruption.
    empty_hex = (
        frame(vm=2, name="zephyr", generation=0, sequence=0, payload=good, total=len(good))
        + b"\nAXVISOR_GUEST_CONSOLE_FRAME v=1 vm=2 name=zephyr gen=0 seq=1 len=15 total=30 dropped=0 dma=0 hex="
        + b"\n"
    )
    try:
        demux.demux_host_log(empty_hex, {2: "zephyr"})
    except demux.GuestConsoleDemuxError:
        pass
    else:
        errors.append("empty-hex trailing frame is accepted without the explicit opt-in")
    try:
        result = demux.demux_host_log(
            empty_hex, {2: "zephyr"}, allow_trailing_truncation=True
        )
    except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
        errors.append(f"empty-hex trailing truncation opt-in is rejected: {error}")
        return
    if result.frame_count != 1 or not result.trailing_truncated:
        errors.append("empty-hex trailing truncation is not recorded")



def check_filesystem_contract(errors: list[str], demux: ModuleType) -> None:
    source, expected_bytes = valid_host_log()
    contract_root = WORKSPACE_ROOT / "target" / "contract-tests"
    contract_root.mkdir(parents=True, exist_ok=True)
    directory = (
        contract_root
        / f".axvisor-console-demux-test-{os.getpid()}-{uuid.uuid4().hex}"
    ).resolve()
    directory.mkdir()
    try:
        host_log = directory / "host.log"
        host_log.write_bytes(source)
        lock_path = directory / ".console-demux.lock"
        lock_path.write_bytes(b"held-by-another-demux\n")
        try:
            demux.demux_and_publish(
                host_log, directory, {1: "linux", 2: "zephyr"}
            )
        except demux.GuestConsoleDemuxError as error:
            if "demux lock .console-demux.lock already exists" not in str(error):
                errors.append(f"demux reports the wrong lock contention error: {error}")
        else:
            errors.append("filesystem demux ignores an existing publication lock")
        if not lock_path.is_file() or lock_path.read_bytes() != b"held-by-another-demux\n":
            errors.append("filesystem demux removes or changes another process's lock")
        lock_path.unlink(missing_ok=True)

        try:
            manifest = demux.demux_and_publish(
                host_log, directory, {1: "linux", 2: "zephyr"}
            )
        except Exception as error:  # noqa: BLE001 - preserve diagnosis.
            errors.append(f"filesystem demux rejects valid evidence: {error}")
            return
        for vm_id, expected in expected_bytes.items():
            path = directory / f"guest-vm-{vm_id}.console.log"
            if path.read_bytes() != expected:
                errors.append(f"filesystem output changes VM {vm_id} console bytes")
        manifest_path = directory / "console-manifest.json"
        if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
            errors.append("filesystem manifest differs from the returned manifest")
        original = (directory / "guest-vm-1.console.log").read_bytes()
        try:
            demux.demux_and_publish(host_log, directory, {1: "linux", 2: "zephyr"})
        except demux.GuestConsoleDemuxError:
            pass
        else:
            errors.append("filesystem demux overwrites an existing evidence bundle")
        if (directory / "guest-vm-1.console.log").read_bytes() != original:
            errors.append("filesystem no-overwrite failure changes existing evidence")
        if list(directory.glob("*.demux-part")):
            errors.append("filesystem demux leaves staging files behind")
    finally:
        for name in (
            "host.log",
            "guest-vm-1.console.log",
            "guest-vm-2.console.log",
            "console-manifest.json",
            ".guest-vm-1.console.log.demux-part",
            ".guest-vm-2.console.log.demux-part",
            ".console-manifest.json.demux-part",
            ".console-demux.lock",
        ):
            path = directory / name
            if path.is_file() or path.is_symlink():
                path.unlink()
        directory.rmdir()


def main() -> int:
    errors: list[str] = []
    if not DEMUX.is_file():
        errors.append("guest-console frame demux is missing")
    else:
        try:
            demux = load_module("axvisor_guest_console_demux", DEMUX)
        except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
            errors.append(f"guest-console frame demux cannot be imported: {error}")
        else:
            for name in (
                "GuestConsoleDemuxError",
                "demux_host_log",
                "publish_demux_evidence",
                "demux_and_publish",
            ):
                if not hasattr(demux, name):
                    errors.append(f"guest-console demux does not expose {name}")

            if not errors:
                result = check_valid_demux(errors, demux)
                for label, source, expected_message in invalid_host_logs():
                    expect_rejected(
                        errors,
                        demux,
                        source,
                        label=label,
                        expected_message=expected_message,
                    )
                if result is not None:
                    check_publication(errors, demux, result)
                check_trailing_truncation(errors, demux)
                check_filesystem_contract(errors, demux)

    if not errors:
        return 0

    print("AxVisor guest-console demux contract failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
