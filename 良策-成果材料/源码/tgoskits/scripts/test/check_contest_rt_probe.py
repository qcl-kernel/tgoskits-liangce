#!/usr/bin/env python3
"""Static contract for the P3 native Zephyr probe template."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "scripts" / "contest" / "rt" / "zephyr-rt-probe"


def main() -> int:
    cmake = (PROBE / "CMakeLists.txt").read_text(encoding="utf-8")
    config = (PROBE / "prj.conf").read_text(encoding="utf-8")
    source = (PROBE / "src" / "main.c").read_text(encoding="utf-8")
    assert "find_package(Zephyr REQUIRED" in cmake
    for required in (
        "CONFIG_TIMESLICING=n",
        "CONFIG_NUM_PREEMPT_PRIORITIES=8",
        "CONFIG_HEAP_MEM_POOL_SIZE=0",
    ):
        assert required in config
    for required in (
        "K_TIMER_DEFINE",
        "k_cycle_get_64",
        "k_sem_take",
        "period_release",
        "period_start",
        "period_finish",
        "event_drop_count",
        "p3-rt-event-v1",
    ):
        assert required in source
    timer_body = source.split("static void timer_expiry", 1)[1].split(
        "static void period_loop", 1
    )[0]
    assert "printk" not in timer_body
    assert "k_sleep" not in timer_body
    print("P3_RT_PROBE_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError) as error:
        print(f"P3 probe contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
