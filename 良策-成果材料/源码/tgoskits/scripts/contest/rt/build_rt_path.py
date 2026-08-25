#!/usr/bin/env python3
"""Build the host/source P3 as-built path and input inventory.

The report deliberately describes the current AArch64 virtual-timer control
flow without pretending that a Guest event was observed.  It is a static
precondition for a later P3 runtime matrix, not a runtime qualification tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - pinned project Python is 3.11+
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_vm_carveout_io import publish_new_file, read_regular_bytes  # noqa: E402


PATH_SCHEMA = "p3-rt-path-v1"
INPUT_SCHEMA = "p3-rt-inputs-v1"
MANIFEST_SCHEMA = "p3-rt-path-manifest-v1"
STATUS_SCHEMA = "p3-rt-path-status-v1"

VM_CONFIGS = (
    "os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml",
    "os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml",
)

SOURCE_FILES = (
    "virtualization/axvm/src/runtime/vcpus.rs",
    "virtualization/axvm/src/architecture/ops.rs",
    "virtualization/axvm/src/arch/aarch64/mod.rs",
    "virtualization/axvm/src/arch/aarch64/vtimer/state.rs",
    "virtualization/axvm/src/timer.rs",
    "virtualization/arm_vcpu/src/architecture/vcpu.rs",
    "virtualization/arm_vcpu/src/timer.rs",
)


class PathContractError(ValueError):
    """Raised when the as-built static path cannot be trusted."""


def sha256_file(path: Path) -> str:
    try:
        data = read_regular_bytes(
            path,
            label=f"P3 source artifact {path.name}",
            error_type=PathContractError,
        )
    except PathContractError:
        raise
    except OSError as error:
        raise PathContractError(f"cannot read P3 source artifact {path}: {error}") from error
    return hashlib.sha256(data).hexdigest()


def _required_file(repository: Path, relative: str) -> Path:
    path = repository / relative
    if not path.is_file() or path.is_symlink():
        raise PathContractError(f"required regular file is missing or symlinked: {relative}")
    return path


def _vm_record(repository: Path, relative: str) -> dict[str, Any]:
    path = _required_file(repository, relative)
    try:
        config = tomllib.loads(
            read_regular_bytes(
                path,
                label=f"VM config {relative}",
                error_type=PathContractError,
            ).decode("utf-8")
        )
    except PathContractError:
        raise
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PathContractError(f"cannot parse VM config {relative}: {error}") from error
    base = config.get("base")
    devices = config.get("devices")
    if not isinstance(base, dict) or not isinstance(devices, dict):
        raise PathContractError(f"{relative}: missing [base] or [devices]")
    guest_type = base.get("guest_type")
    phys_cpu_ids = base.get("phys_cpu_ids")
    passthrough = devices.get("passthrough")
    disabled = devices.get("disabled")
    if guest_type != "virtualized":
        raise PathContractError(f"{relative}: guest_type must be virtualized")
    if (
        not isinstance(phys_cpu_ids, list)
        or not phys_cpu_ids
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in phys_cpu_ids)
    ):
        raise PathContractError(f"{relative}: phys_cpu_ids must be non-empty CPU ids")
    if passthrough != []:
        raise PathContractError(
            f"{relative}: physical passthrough is not empty; static virtual-timer path is unsafe"
        )
    if not isinstance(disabled, list):
        raise PathContractError(f"{relative}: devices.disabled must be a list")
    virtual_devices = config.get("devices", {}).get("virtual", [])
    if not isinstance(virtual_devices, list):
        raise PathContractError(f"{relative}: devices.virtual must be a list")
    return {
        "path": relative,
        "sha256": sha256_file(path),
        "vm_id": base.get("id"),
        "name": base.get("name"),
        "guest_type": guest_type,
        "cpu_num": base.get("cpu_num"),
        "phys_cpu_ids": phys_cpu_ids,
        "passthrough": passthrough,
        "disabled": disabled,
        "virtual_device_models": [
            item.get("model")
            for item in virtual_devices
            if isinstance(item, dict) and isinstance(item.get("model"), str)
        ],
    }


def _source_records(repository: Path) -> list[dict[str, Any]]:
    records = []
    for relative in SOURCE_FILES:
        path = _required_file(repository, relative)
        try:
            data = read_regular_bytes(
                path,
                label=f"P3 source artifact {relative}",
                error_type=PathContractError,
            )
        except PathContractError:
            raise
        except OSError as error:
            raise PathContractError(f"cannot read P3 source artifact {relative}: {error}") from error
        records.append(
            {"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    return records


def _git(repository: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise PathContractError(f"cannot read Git identity: {error}") from error
    return completed.stdout.strip()


def build_path(repository: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    repository = repository.resolve()
    if not repository.is_dir():
        raise PathContractError(f"repository is not a directory: {repository}")
    vm_records = [_vm_record(repository, relative) for relative in VM_CONFIGS]
    source_records = _source_records(repository)
    revision = _git(repository, "rev-parse", "HEAD")
    status = _git(repository, "status", "--short")
    dirty = bool(status)

    path = {
        "schema_version": PATH_SCHEMA,
        "path_id": "aarch64-virtual-timer-v1",
        "revision": revision,
        "evidence_level": "host_source",
        "runtime_observed": False,
        "path_variant": "aarch64-virtualized-vtimer-to-vcpu-reentry",
        "stages": [
            {
                "stage": "guest_timer_expiry",
                "present": True,
                "observability": "guest_runtime_required",
                "anchor": "Guest probe event; no host source anchor can prove expiry",
                "non_claim": "not observed by this report",
            },
            {
                "stage": "timer_level_synchronization",
                "present": True,
                "observability": "host_source",
                "anchor": "virtualization/axvm/src/arch/aarch64/mod.rs::synchronize_timer",
                "source": "virtualization/axvm/src/arch/aarch64/vtimer/state.rs::synchronize/publish_levels",
            },
            {
                "stage": "timer_wait_schedule",
                "present": True,
                "observability": "host_source",
                "anchor": "virtualization/axvm/src/arch/aarch64/vtimer/state.rs::arm_wait",
                "source": "virtualization/axvm/src/timer.rs::register_timer_handle",
            },
            {
                "stage": "host_timer_expiry",
                "present": True,
                "observability": "host_source",
                "anchor": "virtualization/axvm/src/timer.rs::check_events",
                "non_claim": "host source does not prove a Guest deadline was reached",
            },
            {
                "stage": "publish_and_wake_vcpu",
                "present": True,
                "observability": "host_source",
                "anchor": "virtualization/axvm/src/timer.rs::publish_before_wake + runtime/vcpus.rs::notify_vcpu",
            },
            {
                "stage": "vcpu_reentry",
                "present": True,
                "observability": "host_source",
                "anchor": "virtualization/axvm/src/architecture/ops.rs::run_vcpu",
                "source": "runtime/vcpus.rs::inject_pending_interrupts -> ArchOps::before_vcpu_run",
            },
            {
                "stage": "guest_handler",
                "present": True,
                "observability": "guest_runtime_required",
                "anchor": "Guest probe event; no production sample is present",
                "non_claim": "not observed by this report",
            },
            {
                "stage": "generic_pending_interrupt_queue_for_arch_timer",
                "present": False,
                "observability": "not_applicable_arch_timer_backend",
                "anchor": "architectural timer callback wakes through VGIC timer state and notify_vcpu",
                "non_claim": "do not attribute this timer path to queue_interrupt without runtime evidence",
            },
        ],
        "clock": {
            "clock_domain": "AArch64 CNTPCT_EL0 with host monotonic deadline conversion",
            "mapping_id": "p3-clock-source-static-v1",
            "counter_frequency_hz": None,
            "offset_exported": False,
            "runtime_status": "required_before_event_join",
        },
        "non_claims": [
            "source presence is not runtime execution evidence",
            "no Guest raw samples, clock.json, production A/B, or TEST-008..010 result",
            "no timer/IRQ production semantics were changed by this artifact",
        ],
    }
    inputs = {
        "schema_version": INPUT_SCHEMA,
        "revision": revision,
        "dirty": dirty,
        "dirty_status": status.splitlines(),
        "vm_configs": vm_records,
        "source_files": source_records,
        "runtime_inputs": {
            "status": "not_bound",
            "required_before_runtime": [
                "TEST-007 immutable source-bound run",
                "Zephyr SDK and clean checkout",
                "Guest image/build manifests",
                "fresh output directory",
            ],
        },
    }
    return path, inputs


def _file_records(root: Path) -> dict[str, dict[str, Any]]:
    records = {}
    for path in sorted(root.iterdir()):
        if path.name == "status.json" or not path.is_file():
            continue
        if path.is_symlink():
            raise PathContractError(f"P3 report enumerator refuses symlink: {path.name}")
        records[path.name] = {"size": path.stat().st_size, "sha256": sha256_file(path)}
    return records


def _write_json_new(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    publish_new_file(path, payload, error_type=PathContractError)


def write_report(output_dir: Path, path: dict[str, Any], inputs: dict[str, Any]) -> None:
    if output_dir.exists():
        raise PathContractError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    _write_json_new(output_dir / "path.json", path)
    _write_json_new(output_dir / "inputs.json", inputs)
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "revision": path["revision"],
        "files": _file_records(output_dir),
        "excluded": ["status.json"],
    }
    _write_json_new(output_dir / "manifest.json", manifest)
    status = {
        "schema_version": STATUS_SCHEMA,
        "status": "p3_path_static_completed",
        "success": True,
        "qualified": False,
        "revision": path["revision"],
        "runtime_observed": False,
        "blockedReason": "Guest runtime and clock mapping are still required",
        "statusLast": True,
    }
    _write_json_new(output_dir / "status.json", status)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        path, inputs = build_path(args.repository)
        write_report(args.output_dir, path, inputs)
    except (OSError, PathContractError, TypeError, ValueError) as error:
        print(f"P3 realtime path build failed: {error}")
        return 1
    print(f"P3_RT_PATH_BUILD_PASS revision={path['revision']} files={len(inputs['source_files']) + len(inputs['vm_configs'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
