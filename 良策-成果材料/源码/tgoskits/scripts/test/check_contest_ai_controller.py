#!/usr/bin/env python3
"""P5-AI-B contract: Linux MLP control planner trajectory (host evidence).

Compiles the same src/model/contest_model.c + controller.h that the Linux
guest links and runs a 200-tick trajectory: MLP duty from {measured, target
55000, previous duty} drives the DEC-008 first-order plant toward the target.
Requires: every duty in [0,65536], bounded temperature, and a monotone rise
above the start (>30000 mC) yet bounded below 70000 mC. Tagged
CONTEST_AI_CONTROLLER_PASS. This is L2 host evidence (the guest-runtime AI
loop is P5-AI-C).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_SRC = ROOT / "apps" / "contest" / "linux-ai-controller" / "src" / "model"

DRIVER = r"""
#include <stdio.h>
#include "controller.h"
#include "contest_model.h"
int main(void) {
    int measured = 25000, prev = 0, target = 55000;
    int duty0 = -1, duty_final = -1, temp_final = -1;
    int ok = 1, rising = 1, prev_temp = measured;
    for (int i = 0; i < 200; i++) {
        int duty = contest_control_duty(measured, target, prev);
        if (duty < 0 || duty > 65536) ok = 0;
        if (i == 0) duty0 = duty;
        int nm = contest_plant_step(measured, duty, i);
        if (nm <= prev_temp) rising = 0;   /* a heating run must rise each tick */
        prev_temp = nm;
        measured = nm;
        prev = duty;
        duty_final = duty; temp_final = measured;
    }
    printf("TGOS_LINUX_MLP_CONTROL duty0=%d final_duty=%d final_temp_mC=%d ok_bounds=%d rising=%d\n",
           duty0, duty_final, temp_final, ok, rising);
    int sane = ok && duty0 >= 0 && rising && temp_final > 30000 && temp_final < 70000;
    return sane ? 0 : 1;
}
"""


def main() -> int:
    cc = shutil.which("gcc") or shutil.which("cc")
    if cc is None:
        print("CONTEST_AI_CONTROLLER_FAIL: no host C compiler", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="p5-controller-") as td:
        tdir = Path(td)
        driver = tdir / "main.c"
        driver.write_text(DRIVER, encoding="utf-8")
        exe = tdir / "traj"
        result = subprocess.run(
            [cc, "-O2", "-Wall", "-Wextra", "-Werror", "-std=c11",
             "-I", str(APP_SRC), str(driver),
             str(APP_SRC / "contest_model.c"), "-lm", "-o", str(exe)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("CONTEST_AI_CONTROLLER_FAIL: compile", result.stderr[-1200:], file=sys.stderr)
            return 1
        run = subprocess.run([str(exe)], capture_output=True, text=True)
        if run.returncode != 0 or "TGOS_LINUX_MLP_CONTROL" not in run.stdout:
            print(f"CONTEST_AI_CONTROLLER_FAIL: run rc={run.returncode} out={run.stdout!r} err={run.stderr!r}", file=sys.stderr)
            return 1
    print("CONTEST_AI_CONTROLLER_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
