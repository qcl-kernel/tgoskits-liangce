#!/usr/bin/env python3

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_DIR = WORKSPACE_ROOT / "scripts" / "contest" / "icpc"
SOURCE = PROTOCOL_DIR / "icpc.c"
TEST_SOURCE = PROTOCOL_DIR / "test_icpc.c"


def main() -> int:
    compiler = find_c_compiler()
    configured_root = os.environ.get("ICPC_TEST_TMPDIR")
    temporary_root = (
        Path(configured_root)
        if configured_root
        else WORKSPACE_ROOT / "target" / "contract-tests"
    )
    temporary_root.mkdir(parents=True, exist_ok=True)
    work_directory = temporary_root / (
        f"icpc-protocol-{os.getpid()}-{secrets.token_hex(8)}"
    )
    work_directory.mkdir()
    executable = work_directory / executable_name()
    compile_protocol(compiler, executable)
    subprocess.run([str(executable)], cwd=WORKSPACE_ROOT, check=True)
    return 0


def find_c_compiler() -> str:
    configured = os.environ.get("CC")
    candidates = [configured] if configured else []
    candidates.extend(["cc", "gcc", "clang"])
    for candidate in candidates:
        if candidate and shutil.which(candidate):
            return candidate
    raise RuntimeError("ICPC protocol test requires CC, cc, gcc, or clang")


def compile_protocol(compiler: str, executable: Path) -> None:
    command = [
        compiler,
        "-std=c99",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-pedantic",
        "-I",
        str(PROTOCOL_DIR),
        str(SOURCE),
        str(TEST_SOURCE),
        "-o",
        str(executable),
    ]
    subprocess.run(command, cwd=WORKSPACE_ROOT, check=True)


def executable_name() -> str:
    return "icpc_protocol_test.exe" if os.name == "nt" else "icpc_protocol_test"


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ICPC protocol contract failed: {error}", file=sys.stderr)
        sys.exit(1)
