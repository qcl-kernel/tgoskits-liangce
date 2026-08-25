#!/usr/bin/env python3
"""Host/source contract for the P3 as-built path report."""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RT_DIR = ROOT / "scripts" / "contest" / "rt"
sys.path.insert(0, str(RT_DIR))

import build_rt_path  # noqa: E402


def main() -> int:
    path, inputs = build_rt_path.build_path(ROOT)
    assert path["schema_version"] == "p3-rt-path-v1"
    assert path["path_variant"] == "aarch64-virtualized-vtimer-to-vcpu-reentry"
    assert path["runtime_observed"] is False
    stages = {stage["stage"]: stage for stage in path["stages"]}
    assert stages["timer_wait_schedule"]["present"] is True
    assert stages["generic_pending_interrupt_queue_for_arch_timer"]["present"] is False
    assert stages["guest_handler"]["observability"] == "guest_runtime_required"
    assert isinstance(inputs["dirty"], bool)
    assert isinstance(inputs["dirty_status"], list)
    assert len(inputs["vm_configs"]) == 2
    assert {tuple(record["phys_cpu_ids"]) for record in inputs["vm_configs"]} == {(0, 1), (2,)}
    assert all(record["passthrough"] == [] for record in inputs["vm_configs"])
    assert all(len(record["sha256"]) == 64 for record in inputs["source_files"])

    root = ROOT / "target" / "contract-tests" / f"p3-path-{secrets.token_hex(8)}"
    root.mkdir(parents=True)
    fixture = root / "vm.toml"
    fixture.write_text(
        "[base]\nguest_type='virtualized'\nphys_cpu_ids=[0]\n[devices]\npassthrough=['/timer']\ndisabled=[]\n",
        encoding="utf-8",
    )
    try:
        build_rt_path._vm_record(root, "vm.toml")
    except build_rt_path.PathContractError:
        pass
    else:
        raise AssertionError("non-empty physical passthrough must fail closed")

    report = root / "report"
    build_rt_path.write_report(report, path, inputs)
    if sorted(item.name for item in report.iterdir()) != [
        "inputs.json",
        "manifest.json",
        "path.json",
        "status.json",
    ]:
        raise AssertionError("P3 path report has an unexpected file set")
    status = json.loads((report / "status.json").read_text(encoding="utf-8"))
    if status.get("status") != "p3_path_static_completed" or status.get("statusLast") is not True:
        raise AssertionError("P3 path report status is not status-last")
    try:
        build_rt_path.write_report(report, path, inputs)
    except build_rt_path.PathContractError:
        pass
    else:
        raise AssertionError("P3 path report can be overwritten")

    print("P3_RT_PATH_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, json.JSONDecodeError) as error:
        print(f"P3 path contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
