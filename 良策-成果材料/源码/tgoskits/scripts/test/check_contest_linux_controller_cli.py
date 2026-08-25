#!/usr/bin/env python3
"""Compile and run the portable Linux controller CLI contract."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "apps" / "contest" / "linux-ai-controller"
SOURCE = APP_DIR / "src" / "runtime_config.c"
HEADER = APP_DIR / "src" / "runtime_config.h"
TEST = APP_DIR / "test" / "runtime_config_test.c"
CONTRACT_ROOT = ROOT / "target" / "contract-tests"


def find_compiler() -> str:
    candidates = []
    configured = os.environ.get("CC")
    if configured:
        candidates.append(configured)
    candidates.extend(("cc", "gcc", "clang"))
    for candidate in candidates:
        if shutil.which(candidate):
            return candidate
    raise RuntimeError("Linux controller CLI contract requires CC, cc, gcc, or clang")


def main() -> int:
    compiler = find_compiler()
    if not SOURCE.is_file() or not HEADER.is_file() or not TEST.is_file():
        raise RuntimeError("Linux controller CLI contract sources are incomplete")
    run_dir = CONTRACT_ROOT / f"p5-linux-controller-cli-{os.getpid()}-{secrets.token_hex(8)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    executable = run_dir / ("runtime_config_test.exe" if os.name == "nt" else "runtime_config_test")
    command = [
        compiler,
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-std=c11",
        "-I",
        str(APP_DIR / "src"),
        "-o",
        str(executable),
        str(TEST),
        str(SOURCE),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    subprocess.run(
        [
            str(executable),
            "apps/contest/linux-ai-controller/model/model.bin",
            "apps/contest/linux-ai-controller/model/metadata.json",
        ],
        cwd=ROOT,
        check=True,
    )
    print(f"LINUX_CONTROLLER_CLI_HOST_PASS compiler={compiler}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Linux controller CLI contract failed: {error}")
        raise SystemExit(1)
