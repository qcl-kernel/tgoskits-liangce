#!/usr/bin/env python3
"""Compile and run the portable P5 Zephyr-control host contracts."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "apps" / "contest" / "zephyr-control"
INCLUDE_DIR = APP_DIR / "include"
CONTRACT_TEST_ROOT = ROOT / "target" / "contract-tests"
SOURCE_FILES = [
    APP_DIR / "src" / "plant.c",
    APP_DIR / "src" / "control_mailbox.c",
    APP_DIR / "src" / "watchdog.c",
]
TEST_FILES = [
    APP_DIR / "tests" / "test_plant_host.c",
    APP_DIR / "tests" / "test_watchdog_host.c",
]


def find_c_compiler() -> str:
    configured = os.environ.get("CC")
    candidates = [configured] if configured else []
    candidates.extend(["cc", "gcc", "clang"])
    for candidate in candidates:
        if candidate and shutil.which(candidate):
            return candidate
    raise RuntimeError("P5 host contract requires CC, cc, gcc, or clang")


def compile_and_run(compiler: str, test_file: Path, executable: Path) -> None:
    command = [
        compiler,
        "-std=c99",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-pedantic",
        "-ffp-contract=off",
        "-I",
        str(INCLUDE_DIR),
        *(str(source) for source in SOURCE_FILES),
        str(test_file),
        "-o",
        str(executable),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    subprocess.run([str(executable)], cwd=ROOT, check=True)


def executable_name(test_file: Path) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    return f"{test_file.stem}{suffix}"


def make_contract_directory(prefix: str) -> Path:
    CONTRACT_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    directory = CONTRACT_TEST_ROOT / f"{prefix}-{os.getpid()}-{secrets.token_hex(8)}"
    directory.mkdir()
    return directory


def main() -> int:
    compiler = find_c_compiler()
    temporary_root = make_contract_directory("p5-zephyr-control")
    for test_file in TEST_FILES:
        compile_and_run(
            compiler,
            test_file,
            temporary_root / executable_name(test_file),
        )
    print(f"C compiler: {compiler}")
    print("CONTEST_ZEPHYR_CONTROL_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"P5 Zephyr-control contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
