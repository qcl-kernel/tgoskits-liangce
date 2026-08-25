#!/usr/bin/env python3
"""P5-AI-A/TEST-016 contract: the deployment C inference matches the golden vectors.

Compiles src/model/contest_model.c with a host C compiler (same code the Linux
guest runs) and executes the frozen 256 golden vectors.  REQUIRES the C model to
match every golden vector within 2 Q16.16 duty LSBs -- proving the Linux target
C inference is bit-consistent with the canonical model (host L2 evidence; the
aarch64-musl cross-build is validated separately by the app Makefile).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_SRC = ROOT / "apps" / "contest" / "linux-ai-controller" / "src" / "model"
GOLDEN_COUNT = 256


def main() -> int:
    cc = shutil.which("gcc") or shutil.which("cc")
    if cc is None:
        print("CONTEST_AI_GOLDEN_C_FAIL: no host C compiler", file=sys.stderr)
        return 1
    for required in ("contest_model.c", "contest_model.h",
                     "contest_model_bin.h", "contest_golden_vectors.h"):
        if not (APP_SRC / required).is_file():
            print(f"CONTEST_AI_GOLDEN_C_FAIL: missing {required}", file=sys.stderr)
            return 1

    with tempfile.TemporaryDirectory(prefix="p5-golden-c-") as td:
        tdir = Path(td)
        driver = (
            "#include <stdio.h>\n"
            '#include "contest_model.h"\n'
            "int main(void){int m=contest_mlp_verify_golden();"
            f"printf(\"TGOS_LINUX_MLP_VERIFY passed=%d expected={GOLDEN_COUNT}\\n\",m);"
            "return m==256?0:1;}"
        )
        (tdir / "verify_main.c").write_text(driver, encoding="utf-8")
        exe = tdir / "verify"
        result = subprocess.run(
            [cc, "-O2", "-Wall", "-Wextra", "-Werror", "-std=c11",
             "-I", str(APP_SRC), str(tdir / "verify_main.c"),
             str(APP_SRC / "contest_model.c"), "-lm", "-o", str(exe)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("CONTEST_AI_GOLDEN_C_FAIL: compile", result.stderr[-1500:], file=sys.stderr)
            return 1
        run = subprocess.run([str(exe)], capture_output=True, text=True)
        if run.returncode != 0 or "TGOS_LINUX_MLP_VERIFY" not in run.stdout:
            print(f"CONTEST_AI_GOLDEN_C_FAIL: run rc={run.returncode} out={run.stdout!r} err={run.stderr!r}", file=sys.stderr)
            return 1
    print("CONTEST_AI_GOLDEN_C_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
