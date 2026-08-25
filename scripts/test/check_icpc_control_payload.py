#!/usr/bin/env python3
"""Compile and run the portable ICPC control-payload contract."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ICPC_DIR = ROOT / "scripts" / "contest" / "icpc"


def find_compiler() -> str:
    candidates = [os.environ.get("CC"), "cc", "gcc", "clang"]
    for candidate in candidates:
        if candidate and shutil.which(candidate):
            return candidate
    raise RuntimeError("control payload test requires CC, cc, gcc, or clang")


def main() -> int:
    compiler = find_compiler()
    temporary_root = ROOT / "target" / "contract-tests"
    temporary_root.mkdir(parents=True, exist_ok=True)
    work_directory = temporary_root / (
        f"icpc-control-payload-{os.getpid()}-{secrets.token_hex(8)}"
    )
    work_directory.mkdir()
    executable = work_directory / (
        "icpc_control_payload_test.exe"
        if os.name == "nt"
        else "icpc_control_payload_test"
    )
    subprocess.run(
        [
            compiler,
            "-std=c99",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-I",
            str(ICPC_DIR),
            str(ICPC_DIR / "icpc_control.c"),
            str(ICPC_DIR / "test_icpc_control.c"),
            "-o",
            str(executable),
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run([str(executable)], cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ICPC control payload contract failed: {error}", file=sys.stderr)
        sys.exit(1)
