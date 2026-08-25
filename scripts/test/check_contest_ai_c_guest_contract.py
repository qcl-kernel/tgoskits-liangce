#!/usr/bin/env python3
"""Host contract for the portable C pieces used by both P5 Guest apps.

This is deliberately narrower than a Guest run: it proves the fixed PI
sequence and the shared big-endian payload codec with the same source files
that are linked into the Linux application.  It does not claim Zephyr or
Guest-IP/runtime evidence.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "apps" / "contest" / "linux-ai-controller" / "src" / "model"
COMMON_DIR = ROOT / "apps" / "contest" / "common"


def main() -> int:
    cc = shutil.which("gcc") or shutil.which("cc")
    if cc is None:
        print("P5_C_GUEST_CONTRACT_FAIL: no host C compiler", file=sys.stderr)
        return 1

    samples = [(25_000, 55_000), (30_000, 55_000), (54_000, 55_000),
               (60_000, 55_000), (25_000, 45_000), (45_000, 45_000)]
    driver_lines = [
        '#include <stdint.h>',
        '#include <stdio.h>',
        '#include "controller.h"',
        '#include "icpc_payload.h"',
        'int main(void) {',
        '  struct contest_fixed_pi_state state = {0};',
        '  contest_fixed_pi_reset(&state);',
    ]
    for measured, target in samples:
        driver_lines.append(
            f'  printf("%d ", contest_fixed_pi_update(&state, {measured}, {target}));'
        )
    driver_lines.extend([
        '  uint8_t bytes[8] = {0};',
        '  contest_icpc_write_u32_be(bytes, UINT32_C(0x12345678));',
        '  contest_icpc_write_i32_be(bytes + 4, INT32_C(-2));',
        '  printf("%02x%02x%02x%02x %u %d\\n", bytes[0], bytes[1], bytes[2], bytes[3],',
        '         contest_icpc_read_u32_be(bytes), contest_icpc_read_i32_be(bytes + 4));',
        '  return 0;',
        '}',
    ])

    with tempfile.TemporaryDirectory(prefix="p5-c-guest-contract-") as td:
        tdir = Path(td)
        driver = tdir / "contract.c"
        exe = tdir / "contract"
        driver.write_text("\n".join(driver_lines) + "\n", encoding="utf-8")
        result = subprocess.run(
            [cc, "-O2", "-Wall", "-Wextra", "-Werror", "-std=c11",
             "-I", str(MODEL_DIR), "-I", str(COMMON_DIR), str(driver),
             str(MODEL_DIR / "fixed_pi.c"), "-lm", "-o", str(exe)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"P5_C_GUEST_CONTRACT_FAIL: compile {result.stderr[-2000:]}",
                  file=sys.stderr)
            return 1
        run = subprocess.run([str(exe)], capture_output=True, text=True)
        if run.returncode != 0:
            print(f"P5_C_GUEST_CONTRACT_FAIL: run {run.stderr[-1000:]}",
                  file=sys.stderr)
            return 1

    sys.path.insert(0, str(ROOT / "scripts" / "contest" / "ai"))
    from reference import FixedPiController  # noqa: WPS433

    reference = FixedPiController()
    expected = [reference.update(measured, target) for measured, target in samples]
    fields = run.stdout.split()
    actual = [int(value) for value in fields[: len(samples)]]
    if actual != expected:
        print(f"P5_C_GUEST_CONTRACT_FAIL: fixed PI {actual!r} != {expected!r}",
              file=sys.stderr)
        return 1
    if fields[len(samples):] != ["12345678", "305419896", "-2"]:
        print(f"P5_C_GUEST_CONTRACT_FAIL: payload codec {fields!r}",
              file=sys.stderr)
        return 1
    print("P5_C_GUEST_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
