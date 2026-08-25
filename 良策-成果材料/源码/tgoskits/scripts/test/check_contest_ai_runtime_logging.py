#!/usr/bin/env python3
"""Static gate for raw P5 Linux/Zephyr event evidence producers."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LINUX = ROOT / "apps" / "contest" / "linux-ai-controller" / "src" / "main.c"
ZEPHYR = ROOT / "apps" / "contest" / "zephyr-control" / "src" / "main.c"

REQUIRED_LINUX = (
    "linux_event_write_one",
    "stream != stdout",
    "p5-ai-event-v1",
    "raw event evidence",
)
REQUIRED_ZEPHYR = (
    "zephyr_event_print",
    "CONFIG_DUAL_BOOT_ID",
    "p5-ai-event-v1",
    '"safe_enter"',
    '"period_release"',
    '"packet_receive"',
    '"ack_send"',
    '"control_apply"',
    '"packet_send"',
    '"period_finish"',
)


def main() -> int:
    try:
        linux_source = LINUX.read_text(encoding="utf-8")
        zephyr_source = ZEPHYR.read_text(encoding="utf-8")
    except OSError as error:
        print(f"P5_AI_RUNTIME_LOGGING_BLOCKED: {error}")
        return 1
    missing_linux = [token for token in REQUIRED_LINUX if token not in linux_source]
    missing_zephyr = [token for token in REQUIRED_ZEPHYR if token not in zephyr_source]
    if missing_linux or missing_zephyr:
        print(
            "P5_AI_RUNTIME_LOGGING_BLOCKED: "
            f"linux={missing_linux} zephyr={missing_zephyr}"
        )
        return 1
    print(
        "P5_AI_RUNTIME_LOGGING_SOURCE_PASS "
        f"linux_tokens={len(REQUIRED_LINUX)} zephyr_tokens={len(REQUIRED_ZEPHYR)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
