#!/usr/bin/env python3
"""Contract tests for P3 profile and matrix planning."""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RT_DIR = ROOT / "scripts" / "contest" / "rt"
PROFILE_DIR = ROOT / "configs" / "contest"
sys.path.insert(0, str(RT_DIR))

import build_rt_matrix  # noqa: E402


def make_contract_root() -> Path:
    root = ROOT / "target" / "contract-tests" / f"p3-matrix-{secrets.token_hex(8)}"
    root.mkdir(parents=True)
    return root


def write_gate(root: Path, test_id: str) -> Path:
    directory = root / test_id
    directory.mkdir()
    (directory / "status.json").write_text(
        json.dumps({"success": True, "status": f"{test_id.lower()}_completed"}),
        encoding="utf-8",
    )
    return directory


def main() -> int:
    native = PROFILE_DIR / "p3-rt-native.toml"
    idle = PROFILE_DIR / "p3-rt-ax-idle.toml"
    matrix = build_rt_matrix.build_matrix(
        "native",
        "native-contract",
        [native],
        [7, 19, 43],
        {},
        None,
        None,
        None,
        "separate-plus-combined",
    )
    assert len(matrix["runs"]) == 3
    assert matrix["runs"][0]["scenario"] == "RT-NATIVE"

    root = make_contract_root()
    gate = write_gate(root, "TEST-007")
    discovery = build_rt_matrix.build_matrix(
        "discovery",
        "discovery-contract",
        [idle],
        [7, 19, 43],
        {"TEST-007": gate},
        None,
        None,
        None,
        "separate-plus-combined",
    )
    assert len(discovery["runs"]) == 3
    assert discovery["gates"][0]["test_id"] == "TEST-007"
    try:
        build_rt_matrix.build_matrix(
            "discovery",
            "missing-gate",
            [idle],
            [7, 19, 43],
            {},
            None,
            None,
            None,
            "separate-plus-combined",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("matrix builder accepted a missing runtime gate")
    print("P3_RT_RUN_PLAN_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"P3 run-plan contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
