#!/usr/bin/env python3
"""Fail-closed host contract for the canonical TEST-018 fault cases."""

from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "configs" / "contest" / "ai" / "faults-v1.json"
sys.path.insert(0, str(ROOT / "scripts" / "contest" / "ai"))
from fault_profile import (  # noqa: E402
    FaultProfileError,
    load_fault_profile,
    validate_case,
)


def main() -> int:
    try:
        profile = load_fault_profile(PROFILE)
        tampered = copy.deepcopy(profile)
        tampered["cases"][5]["expected"]["safe_deadline_ms"] = 501
        try:
            validate_case(tampered["cases"][5], 5)
        except FaultProfileError:
            print("  [FAIL-CLOSED] tampered safe deadline rejected")
        else:
            raise FaultProfileError("tampered safe deadline was accepted")
    except FaultProfileError as error:
        print(f"P5 TEST-018 contract failed: {error}", file=sys.stderr)
        return 1
    print(f"P5_TEST018_CONTRACT_PASS cases={len(profile['cases'])} qualified=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
