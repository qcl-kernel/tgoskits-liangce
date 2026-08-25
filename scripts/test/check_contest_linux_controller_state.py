#!/usr/bin/env python3
"""Compile and run the allocation-free Linux controller state machine."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "apps" / "contest" / "linux-ai-controller"
SOURCES = (
    APP_DIR / "src" / "linux_controller_state.c",
    APP_DIR / "src" / "model" / "contest_model.c",
    APP_DIR / "src" / "model" / "fixed_pi.c",
    APP_DIR / "test" / "linux_controller_state_test.c",
)
CONTRACT_ROOT = ROOT / "target" / "contract-tests"
MAIN_SOURCE = APP_DIR / "src" / "main.c"


def find_compiler() -> str:
    configured = os.environ.get("CC")
    candidates = ([configured] if configured else []) + ["cc", "gcc", "clang"]
    for candidate in candidates:
        if candidate and shutil.which(candidate):
            return candidate
    raise RuntimeError("Linux controller state contract requires CC, cc, gcc, or clang")


def check_nonblocking_source() -> None:
    source = MAIN_SOURCE.read_text(encoding="utf-8")
    start = source.index("static int icpc_nonblocking_loop(void)\n{")
    end = source.index("static int icpc_safe_loop(void) {", start)
    loop = source[start:end]
    required = (
        "linux_controller_start",
        "linux_controller_observe_status",
        "linux_controller_observe_ack",
        "linux_controller_observe_feedback",
        "O_NONBLOCK",
        "poll(&descriptor, 1, 10)",
        "monotonic_ns_now",
        "100000000",
        "500000000",
    )
    missing = [token for token in required if token not in loop]
    if missing:
        raise RuntimeError(f"nonblocking Linux loop is missing: {missing}")
    if "ICPC_FLAG_RETRANSMISSION" not in source:
        raise RuntimeError("nonblocking Linux send helper lost retransmission flags")
    forbidden = ("SO_RCVTIMEO", "usleep(", "sleep(")
    leaked = [token for token in forbidden if token in loop]
    if leaked:
        raise RuntimeError(f"nonblocking Linux loop contains blocking primitive: {leaked}")
    logger_contract = (
        "linux_event_logger_main",
        "pthread_create",
        "linux_event_logger_stop",
        "p5-ai-event-v1",
        "event_dropped",
        "retry_sent",
        'fopen(g_event_log_path, "wx")',
        "TGOS_LINUX_TRAJ_DONE ticks=%d mode=%s seed=%u acked=%u feedback=%u\\n",
    )
    missing_logger = [token for token in logger_contract if token not in source]
    if missing_logger:
        raise RuntimeError(f"Linux event logger contract is incomplete: {missing_logger}")


def main() -> int:
    compiler = find_compiler()
    check_nonblocking_source()
    missing = [str(path) for path in SOURCES if not path.is_file()]
    if missing:
        raise RuntimeError(f"Linux controller state sources are incomplete: {missing}")
    run_dir = CONTRACT_ROOT / f"p5-linux-controller-state-{os.getpid()}-{secrets.token_hex(8)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    executable = run_dir / ("linux_controller_state_test.exe" if os.name == "nt" else "linux_controller_state_test")
    subprocess.run(
        [
            compiler,
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-std=c11",
            "-pthread",
            "-I",
            str(APP_DIR / "src"),
            "-o",
            str(executable),
            *(str(path) for path in SOURCES),
            "-lm",
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run([str(executable)], cwd=ROOT, check=True)
    print(f"LINUX_CONTROLLER_STATE_HOST_PASS compiler={compiler}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Linux controller state contract failed: {error}")
        raise SystemExit(1)
