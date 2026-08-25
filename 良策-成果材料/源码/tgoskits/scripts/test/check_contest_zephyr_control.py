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
MAIN_SOURCE = APP_DIR / "src" / "main.c"
CMAKE_FILE = APP_DIR / "CMakeLists.txt"
PRJ_CONF = APP_DIR / "prj.conf"
CONTRACT_TEST_ROOT = ROOT / "target" / "contract-tests"
SOURCE_FILES = [
    APP_DIR / "src" / "plant.c",
    APP_DIR / "src" / "control_mailbox.c",
    APP_DIR / "src" / "control_loop.c",
    APP_DIR / "src" / "watchdog.c",
]
TEST_FILES = [
    APP_DIR / "tests" / "test_control_loop_host.c",
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


def check_c2_integration_source() -> None:
    main_source = MAIN_SOURCE.read_text(encoding="utf-8")
    cmake = CMAKE_FILE.read_text(encoding="utf-8")
    prj_conf = PRJ_CONF.read_text(encoding="utf-8")
    required = (
        "control_periodic_thread",
        "control_loop_release",
        "control_loop_publish",
        "K_MSGQ_DEFINE(control_status_queue",
        "k_timer_start(&control_timer",
        "k_timer_status_sync(&control_timer)",
        "CONTROL_PRIORITY 1",
        "NET_PRIORITY 5",
        "LOGGER_PRIORITY 7",
        "K_MSGQ_DEFINE(control_event_queue",
        "control_logger_thread",
        "k_msgq_get(&control_event_queue",
        "g_control_status_overwritten",
        "status_queue_overwrite",
        "control_loop.c",
        "CONFIG_NUM_PREEMPT_PRIORITIES=8",
    )
    combined = main_source + cmake + prj_conf
    missing = [token for token in required if token not in combined]
    if missing:
        raise RuntimeError(f"C2 integration source is missing: {missing}")
    forbidden = ("plant_step(&", "plant_apply_duty(&")
    leaked = [token for token in forbidden if token in main_source]
    if leaked:
        raise RuntimeError(
            f"network/main source owns plant work instead of the C2 thread: {leaked}"
        )
    event_start = main_source.index("static void zephyr_event_print")
    logger_start = main_source.index("static void control_logger_thread")
    periodic_start = main_source.index("static void control_periodic_thread")
    event_source = main_source[event_start:logger_start]
    periodic_source = main_source[periodic_start:]
    if "printk(\"" in event_source:
        raise RuntimeError("C2 event producer performs console I/O")
    if "printk(\"" in periodic_source.split("static void send_pending_status", 1)[0]:
        raise RuntimeError("C2 periodic thread performs console I/O")
    print("CONTROL_LOOP_INTEGRATION_SOURCE_PASS")


def main() -> int:
    compiler = find_c_compiler()
    check_c2_integration_source()
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
