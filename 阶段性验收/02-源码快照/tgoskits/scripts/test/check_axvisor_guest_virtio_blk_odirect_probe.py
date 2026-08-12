#!/usr/bin/env python3
"""Compile and constrain the disposable Linux Guest O_DIRECT blk-read helper."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/contest/guest_virtio_blk_odirect_probe.c"
READY = "AXVISOR_VIRTIO_BLK_ODIRECT_READY nonce=%s gpa=0x%"
DONE = "AXVISOR_VIRTIO_BLK_ODIRECT_DONE nonce=%s bytes=%d gpa=0x%"


def main() -> int:
    errors: list[str] = []
    try:
        source = SOURCE.read_text(encoding="utf-8")
    except OSError as error:
        print(f"Guest virtio-blk O_DIRECT probe contract failed: {error}", file=sys.stderr)
        return 1

    required_fragments = (
        "#define _GNU_SOURCE",
        "O_RDONLY | O_DIRECT | O_CLOEXEC",
        "posix_memalign(&allocated, page_size, page_size)",
        "memset(allocated, 0xa5, page_size)",
        "mlock(allocated, page_size)",
        'open("/proc/self/pagemap", O_RDONLY | O_CLOEXEC)',
        "PAGEMAP_PRESENT",
        "PAGEMAP_PFN_MASK",
        "pfn == 0",
        "pread(device_fd, buffer, BLOCK_BYTES, offset)",
        "bytes_read != BLOCK_BYTES",
        "--control-device",
        'strcmp(device, "/dev/hvc0") == 0',
        "O_RDONLY | O_NONBLOCK | O_NOCTTY | O_CLOEXEC",
        "S_ISCHR(device_status.st_mode)",
        "open_read_only_control_device(arguments->control_device)",
        "wait_for_go_authorization(control_fd, arguments->nonce, arguments->hold_milliseconds)",
        "poll(&input, 1, remaining_milliseconds)",
        "read(control_fd, received + received_bytes, sizeof(received) - received_bytes)",
        '"GO %s\\n"',
        "GO authorization has trailing bytes or EOF",
        READY,
        DONE,
        "for (;;) {\n        pause();\n    }",
        "do not prove descriptor addressing, a stage-2 mapping",
    )
    for fragment in required_fragments:
        if fragment not in source:
            errors.append(f"helper source is missing `{fragment}`")

    forbidden_fragments = (
        "O_WRONLY",
        "O_RDWR",
        "pwrite(",
        "process_vm_writev",
        "/dev/mem",
        "MAP_FIXED",
        "memory-write",
        "human-monitor-command",
        "STDIN_FILENO",
        "scanf(",
        "gets(",
    )
    for fragment in forbidden_fragments:
        if fragment in source:
            errors.append(f"helper source permits a forbidden write/control surface: `{fragment}`")

    ready_index = source.find(READY)
    go_index = source.find("wait_for_go_authorization(control_fd, arguments->nonce, arguments->hold_milliseconds)")
    read_index = source.find("read_exact_sector(device_fd, buffer, arguments->sector)")
    done_index = source.find(DONE)
    pause_index = source.find("for (;;) {\n        pause();\n    }")
    if min(ready_index, go_index, read_index, done_index, pause_index) < 0 or not (
        ready_index < go_index < read_index < done_index < pause_index
    ):
        errors.append("helper does not preserve READY, GO, read, DONE, retain-buffer order")

    if platform.system() == "Linux":
        errors.extend(compile_and_exercise_helper())
    else:
        print("Guest virtio-blk O_DIRECT helper host compile skipped outside Linux")

    if errors:
        print("Guest virtio-blk O_DIRECT probe contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Guest virtio-blk O_DIRECT probe contract passed")
    return 0


def compile_and_exercise_helper() -> list[str]:
    compiler = find_c_compiler()
    if compiler is None:
        return ["Linux host has no C compiler (CC, cc, gcc, or clang)"]
    temporary_root = ROOT / "target" / "contract-tests"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="guest-virtio-blk-odirect-", dir=temporary_root) as directory:
        executable = Path(directory) / "guest-virtio-blk-odirect-probe"
        command = [
            compiler,
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-static",
            str(SOURCE),
            "-o",
            str(executable),
        ]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            return [f"static GCC-compatible compile failed: {detail}"]
        rejected = subprocess.run(
            [
                str(executable),
                "--nonce",
                "NOT-A-LOWER-HEX-NONCE",
                "--device",
                "/dev/vda",
                "--control-device",
                "/dev/hvc0",
                "--sector",
                "0",
                "--hold-ms",
                "0",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if rejected.returncode == 0:
            return ["helper accepts a malformed nonce"]
        if "AXVISOR_VIRTIO_BLK_ODIRECT_" in rejected.stdout:
            return ["helper emits a READY/DONE marker before rejecting malformed input"]
    return []


def find_c_compiler() -> str | None:
    configured = os.environ.get("CC")
    candidates = [configured] if configured else []
    candidates.extend(["cc", "gcc", "clang"])
    return next((candidate for candidate in candidates if candidate and shutil.which(candidate)), None)


if __name__ == "__main__":
    sys.exit(main())
