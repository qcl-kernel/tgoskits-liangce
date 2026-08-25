#!/usr/bin/env python3
"""Positive/negative host contract for TEST-018 preflight bundles."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "scripts" / "contest" / "ai"
FAULTS = ROOT / "configs" / "contest" / "ai" / "faults-v1.json"
QUALIFICATION = ROOT / "configs" / "contest" / "ai" / "qualification-v1.json"
sys.path.insert(0, str(AI_DIR))

import fault_profile  # noqa: E402
import run_closed_loop  # noqa: E402


def claim(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {"path": str(path), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def preflight_for_case(
    profile: dict[str, object], case_id: str, *, run_id: str
) -> dict[str, object]:
    selected = fault_profile.select_fault_case(profile, case_id)
    case_bytes = json.dumps(
        selected, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "schema_version": "p5-ai-runner-preflight-v1",
        "run_id": run_id,
        "session_id": 1,
        "scenario": "test-018",
        "controller": "mlp",
        "seed": 43,
        "qualified": False,
        "inputs": {
            "profile": claim(QUALIFICATION),
            "fault_profile": claim(FAULTS),
            "fault_case": {
                "case": case_id,
                "definition": selected,
                "sha256": hashlib.sha256(case_bytes).hexdigest(),
                "binding": "immutable_profile_case_copy",
            },
        },
    }


def main() -> int:
    profile = fault_profile.load_fault_profile(FAULTS)
    try:
        with tempfile.TemporaryDirectory(prefix="p5-test018-bundle-") as name:
            for case_id in fault_profile.CASE_IDS:
                run_id = f"test018-bundle-contract-{case_id}"
                output = Path(name) / case_id
                preflight = preflight_for_case(profile, case_id, run_id=run_id)
                run_closed_loop.write_test018_host_preflight_bundle(
                    preflight,
                    output_dir=output,
                    command=(
                        "python",
                        "run_closed_loop.py",
                        "--scenario",
                        "test-018",
                        "--fault-case",
                        case_id,
                    ),
                    cwd=ROOT,
                )
                run_closed_loop.validate_test018_host_preflight_bundle(
                    output, run_id=run_id, session_id=1, fault_case=case_id
                )
                if case_id == "linux-stop":
                    (output / "configs" / "faults-v1.json").write_bytes(
                        (output / "configs" / "faults-v1.json").read_bytes() + b"\n"
                    )
                    try:
                        run_closed_loop.validate_test018_host_preflight_bundle(
                            output, run_id=run_id, session_id=1, fault_case=case_id
                        )
                    except run_closed_loop.RunnerPlanError:
                        print("  [FAIL-CLOSED] tampered packaged fault profile rejected")
                    else:
                        raise run_closed_loop.RunnerPlanError(
                            "tampered packaged fault profile was accepted"
                        )
    except (OSError, ValueError, run_closed_loop.RunnerPlanError) as error:
        print(f"P5 TEST-018 bundle contract failed: {error}", file=sys.stderr)
        return 1
    print(
        "P5_TEST018_BUNDLE_PASS "
        f"cases={len(fault_profile.CASE_IDS)} qualified=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
