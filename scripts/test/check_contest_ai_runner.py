#!/usr/bin/env python3
"""Static contract gate for the P5 Guest runner source boundary."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "contest" / "ai" / "run_closed_loop.py"


REQUIRED_FUNCTIONS = {
    "build_guest_runtime_plan",
    "materialize_guest_vm_inputs",
    "materialize_guest_runtime_inputs",
    "build_runtime_preflight",
    "write_test018_host_preflight_bundle",
    "validate_test018_host_preflight_bundle",
    "validate_guest_runtime_plan",
    "validate_materialized_guest_runtime_inputs",
    "execute_guest_runtime_from_preflight",
    "publish_guest_raw_capture",
    "publish_guest_event_transcripts",
    "publish_guest_observation_bundle",
    "validate_guest_observation_bundle",
    "execute_guest_runtime",
    "validate_guest_execution_manifest",
    "publish_guest_runtime_status",
    "validate_guest_runtime_status",
}
REQUIRED_TOKENS = (
    "p5-ai-runtime-plan-v1",
    "p5-ai-vm-inputs-v1",
    "_LOCKED_LINUX_KERNEL",
    "/guest/linux/linux-qemu",
    "/path/to/zephyr.bin",
    "linux-initramfs.cpio.gz",
    "ramdisk_load_addr = 0x8800_0000",
    "convert_initramfs",
    "build_axvisor_qemu_command",
    "inject_qemu_identity_args",
    'path.open("x"',
    "shutil.rmtree(output",
    "runtime_input_dir",
    "P5_GUEST_OBSERVATION_PASS",
    "p5-ai-observation-v1",
    "observation package already has a terminal status",
    "p5-ai-execution-v1",
    "subprocess.Popen",
    "execution.json",
    "validate_guest_execution_manifest",
    "p5-ai-execution-validation-v1",
    "publish_guest_runtime_status",
    "validate_guest_runtime_status",
    "ai_control_blocked",
    "ai_control_failed",
    "blockedReason",
    "p5-ai-status-publication-v1",
    "--fault-profile",
    "--fault-case",
    "load_fault_profile",
    "select_fault_case",
    "spec_from_file_location",
    "_contest_ai_fault_profile",
    "immutable_profile_case_copy",
    "TEST-018 Guest fault injection and recovery are not implemented",
    "test018_host_preflight",
    "P5_TEST018_HOST_PREFLIGHT_PASS",
    "TEST-018 host preflight file set drifted",
    "TEST-018 case binding hash drifted",
)


def main() -> int:
    try:
        source = RUNNER.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(RUNNER))
    except (OSError, SyntaxError) as error:
        print(f"P5_AI_RUNNER_SOURCE_BLOCKED: {error}")
        return 1
    functions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing_functions = sorted(REQUIRED_FUNCTIONS - functions)
    missing_tokens = [token for token in REQUIRED_TOKENS if token not in source]
    if missing_functions or missing_tokens:
        print(
            "P5_AI_RUNNER_SOURCE_BLOCKED: "
            f"missing_functions={missing_functions} missing_tokens={missing_tokens}"
        )
        return 1
    print(
        "P5_AI_RUNNER_SOURCE_PASS "
        f"functions={len(REQUIRED_FUNCTIONS)} tokens={len(REQUIRED_TOKENS)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
