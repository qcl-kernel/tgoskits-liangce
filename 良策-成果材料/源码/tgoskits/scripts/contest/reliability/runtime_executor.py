#!/usr/bin/env python3
"""Execute one identity-bound P4 reliability subplan.

The executor consumes a byte-bound preflight, resolves the prepared Guest
images into disposable VM configs, runs the shared AxVisor/QEMU boundary, and
publishes a status-last runtime bundle.  It never marks the bundle qualified;
qualification remains the responsibility of an independent consumer.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping


CONTEST_DIR = Path(__file__).resolve().parents[1]
NETWORK_DIR = CONTEST_DIR / "network"
for import_dir in (CONTEST_DIR, NETWORK_DIR):
    if str(import_dir) not in sys.path:
        sys.path.insert(0, str(import_dir))

from host_vm_carveout_io import publish_new_file  # noqa: E402
from convert_rootfs_to_initramfs import convert as convert_initramfs  # noqa: E402
from publish_network_artifacts import write_split_logs  # noqa: E402
from runtime.dual_guest import (  # noqa: E402
    RuntimeContractError,
    build_axvisor_qemu_command,
    inject_qemu_identity_args,
    make_runtime_identity,
    request_bounded_shutdown,
    validate_dual_guest_log,
)


RUNTIME_SCHEMA = "p4-reliability-runtime-v1"
RUNTIME_FAILURE_MARKERS = (
    "TGOS_LINUX_NET_FAIL",
    "TGOS_ZEPHYR_NET_FAIL",
    "panicked at",
    "kernel panic",
    "UnhandledException",
)
COMPLETION_MARKERS = {
    "core": (
        "TGOS_LINUX_UDP_RELIABILITY_COMPLETE",
        "TGOS_ZEPHYR_UDP_RELIABILITY_COMPLETE",
    ),
    "fault": (
        "TGOS_LINUX_UDP_RELIABILITY_COMPLETE",
        "TGOS_ZEPHYR_UDP_RELIABILITY_COMPLETE",
    ),
    "restart": (
        "TGOS_LINUX_TCP_RELIABILITY_COMPLETE",
        "TGOS_ZEPHYR_TCP_RELIABILITY_COMPLETE",
    ),
    "exactly-once": (
        "TGOS_LINUX_ICPC_RELIABILITY_COMPLETE",
        "TGOS_ZEPHYR_ICPC_RELIABILITY_COMPLETE",
    ),
}


class ReliabilityRuntimeError(RuntimeError):
    """Raised when a P4 reliability runtime cannot be completed safely."""


def execute_reliability_runtime(
    preflight: Mapping[str, Any],
    *,
    repository: Path,
    output_dir: Path,
    timeout_seconds: float,
    runtime_root: Path = Path("/tmp"),
) -> dict[str, Any]:
    """Run one concrete reliability mode and publish its unqualified result."""

    mode = str(preflight.get("mode", ""))
    if mode not in COMPLETION_MARKERS:
        raise ReliabilityRuntimeError(f"unsupported concrete reliability mode: {mode}")
    if timeout_seconds <= 0:
        raise ReliabilityRuntimeError("timeout_seconds must be positive")
    repository = Path(repository).resolve()
    output = Path(output_dir)
    if not repository.is_dir():
        raise ReliabilityRuntimeError(f"repository is not a directory: {repository}")
    if output.exists() or output.is_symlink():
        raise ReliabilityRuntimeError(f"output directory already exists: {output}")
    if not runtime_root.is_absolute() or not runtime_root.is_dir() or runtime_root.is_symlink():
        raise ReliabilityRuntimeError(
            f"runtime root must be an existing absolute non-link directory: {runtime_root}"
        )

    inputs = _runtime_inputs(preflight)
    output.mkdir(parents=True)
    configs = output / "configs"
    logs = output / "logs"
    configs.mkdir()
    logs.mkdir()
    _write_json(output / "preflight.json", dict(preflight))

    linux_vmconfig, zephyr_vmconfig = _resolve_guest_configs(
        inputs,
        configs=configs,
        mode=mode,
        seed=int(preflight["seed"]),
    )
    identity = make_runtime_identity(runtime_root=runtime_root)
    if identity.runtime_dir.exists():
        raise ReliabilityRuntimeError(
            f"runner-owned runtime directory already exists: {identity.runtime_dir}"
        )
    identity.runtime_dir.mkdir()
    qemu_config = configs / "qemu.resolved.toml"
    publish_new_file(
        qemu_config,
        inject_qemu_identity_args(
            inputs["qemu_config"].read_text(encoding="utf-8"),
            qmp_socket=identity.qmp_socket,
            qemu_pidfile=identity.qemu_pidfile,
            qemu_name=identity.qemu_name,
        ).encode("utf-8"),
        error_type=ReliabilityRuntimeError,
    )
    command = build_axvisor_qemu_command(
        build_config=inputs["build_config"],
        qemu_config=qemu_config,
        linux_vmconfig=linux_vmconfig,
        zephyr_vmconfig=zephyr_vmconfig,
    )
    publish_new_file(
        output / "commands.jsonl",
        (json.dumps({"step": "launch", "cwd": str(repository), "argv": command}) + "\n").encode(
            "utf-8"
        ),
        error_type=ReliabilityRuntimeError,
    )

    launcher: subprocess.Popen[bytes] | None = None
    primary_error: str | None = None
    cleanup_error: str | None = None
    summaries: dict[str, dict[str, int]] = {}
    try:
        with identity.live_log.open("xb") as log_handle:
            launcher = subprocess.Popen(
                command,
                cwd=repository,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            text = _wait_for_completion(
                identity.live_log,
                markers=COMPLETION_MARKERS[mode],
                run_id=str(preflight["run_id"]),
                launcher=launcher,
                timeout_seconds=timeout_seconds,
            )
            summaries = _parse_completion_summaries(text, COMPLETION_MARKERS[mode])
    except (
        OSError,
        ReliabilityRuntimeError,
        RuntimeContractError,
        subprocess.SubprocessError,
    ) as error:
        primary_error = str(error)
    finally:
        if launcher is not None and launcher.poll() is None:
            cleanup_error = request_bounded_shutdown(
                launcher,
                pidfile=identity.qemu_pidfile,
                timeout_seconds=60.0,
            )
        if identity.live_log.is_file():
            shutil.copyfile(identity.live_log, logs / "axvisor.raw.log")
        if identity.runtime_dir.exists():
            try:
                shutil.rmtree(identity.runtime_dir)
            except OSError as error:
                cleanup_error = cleanup_error or f"owned runtime cleanup failed: {error}"

    final_log = logs / "axvisor.raw.log"
    if primary_error is None and cleanup_error is None:
        try:
            text = final_log.read_text(encoding="utf-8", errors="replace")
            validate_dual_guest_log(text, run_id=str(preflight["run_id"]))
            summaries = _parse_completion_summaries(text, COMPLETION_MARKERS[mode])
            write_split_logs(output, text)
        except Exception as error:  # noqa: BLE001 - preserve the failed runtime package
            primary_error = f"final runtime consumption failed: {error}"

    success = primary_error is None and cleanup_error is None
    _write_json(
        output / "summary.json",
        {
            "schema_version": "p4-reliability-runtime-summary-v1",
            "run_id": preflight["run_id"],
            "mode": mode,
            "scenario": preflight["scenario"],
            "seed": preflight["seed"],
            "completion": summaries,
            "success": success,
            "qualified": False,
        },
    )
    _write_json(
        output / "cleanup.json",
        {
            "ownedRuntimeDir": str(identity.runtime_dir),
            "runtimeDirRemoved": not identity.runtime_dir.exists(),
            "cleanupError": cleanup_error,
        },
    )
    manifest = {
        "schema_version": RUNTIME_SCHEMA,
        "run_id": preflight["run_id"],
        "mode": mode,
        "scenario": preflight["scenario"],
        "seed": preflight["seed"],
        "success": success,
        "qualified": False,
        "proof_scope": "one identity-bound dual-Guest reliability runtime",
        "files": _artifact_records(output),
        "non_claims": [
            "runtime publication alone does not qualify TEST-012/013/015",
            "no P4-REL qualification is claimed without an independent consumer",
        ],
    }
    _write_json(output / "manifest.json", manifest)
    status = {
        "schema_version": "p4-reliability-runtime-status-v1",
        "run_id": preflight["run_id"],
        "mode": mode,
        "success": success,
        "status": "p4_reliability_runtime_completed" if success else "p4_reliability_runtime_failed",
        "primaryError": primary_error,
        "cleanupError": cleanup_error,
        "qualified": False,
        "manifestSha256": _sha256(output / "manifest.json"),
        "statusLast": True,
    }
    _write_json(output / "status.json", status)
    return status


def _runtime_inputs(preflight: Mapping[str, Any]) -> dict[str, Path]:
    claims = preflight.get("inputs")
    if not isinstance(claims, Mapping):
        raise ReliabilityRuntimeError("runtime preflight has no input claims")
    names = (
        "build_config",
        "qemu_config",
        "linux_vmconfig",
        "zephyr_vmconfig",
        "linux_rootfs",
        "zephyr_image",
    )
    resolved: dict[str, Path] = {}
    for name in names:
        claim = claims.get(name)
        if not isinstance(claim, Mapping) or not isinstance(claim.get("path"), str):
            raise ReliabilityRuntimeError(f"runtime input claim is incomplete: {name}")
        path = Path(claim["path"])
        if not path.is_file() or path.is_symlink():
            raise ReliabilityRuntimeError(f"runtime input is not a regular file: {path}")
        if claim.get("size") != path.stat().st_size or claim.get("sha256") != _sha256(path):
            raise ReliabilityRuntimeError(f"runtime input hash drifted: {name}")
        resolved[name] = path.resolve()
    return resolved


def _resolve_guest_configs(
    inputs: Mapping[str, Path], *, configs: Path, mode: str, seed: int
) -> tuple[Path, Path]:
    linux_text = inputs["linux_vmconfig"].read_text(encoding="utf-8")
    kernel = Path("/root/.cache/tgoskits/axvisor-images/qemu_aarch64_linux/qemu-aarch64")
    if not kernel.is_file() or kernel.is_symlink():
        raise ReliabilityRuntimeError(f"locked Linux kernel image is missing: {kernel}")
    linux_text = linux_text.replace("/guest/linux/linux-qemu", str(kernel.resolve()))
    initramfs = configs / "linux-initramfs.cpio.gz"
    convert_initramfs(source_rootfs=inputs["linux_rootfs"], output_initramfs=initramfs)
    if "ramdisk_path" in linux_text:
        raise ReliabilityRuntimeError("Linux VM config already contains a ramdisk binding")
    linux_text = linux_text.replace(
        "kernel_load_addr = 0x8020_0000",
        "kernel_load_addr = 0x8020_0000\n"
        f'ramdisk_path = "{initramfs.resolve()}"\n'
        "ramdisk_load_addr = 0x8800_0000",
    )
    zephyr_text = inputs["zephyr_vmconfig"].read_text(encoding="utf-8").replace(
        "/path/to/zephyr.bin", str(inputs["zephyr_image"])
    )
    fault_enabled = mode in {"fault", "exactly-once"}
    linux_text = _bind_fault_policy(linux_text, enabled=fault_enabled, seed=seed)
    zephyr_text = _bind_fault_policy(zephyr_text, enabled=fault_enabled, seed=seed)
    linux_path = configs / "linux.resolved.toml"
    zephyr_path = configs / "zephyr.resolved.toml"
    publish_new_file(linux_path, linux_text.encode("utf-8"), error_type=ReliabilityRuntimeError)
    publish_new_file(zephyr_path, zephyr_text.encode("utf-8"), error_type=ReliabilityRuntimeError)
    return linux_path, zephyr_path


def _bind_fault_policy(text: str, *, enabled: bool, seed: int) -> str:
    enabled_text, enabled_count = re.subn(
        r"(?m)^fault_enabled\s*=\s*(?:true|false)\s*$",
        f"fault_enabled = {'true' if enabled else 'false'}",
        text,
    )
    seeded_text, seed_count = re.subn(
        r"(?m)^fault_seed\s*=\s*[^\r\n]+$", f"fault_seed = {seed}", enabled_text
    )
    if enabled_count != 1 or seed_count != 1:
        raise ReliabilityRuntimeError("VM config has no unique typed fault policy")
    return seeded_text


def _wait_for_completion(
    log_path: Path,
    *,
    markers: tuple[str, str],
    run_id: str,
    launcher: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        failures = [marker for marker in RUNTIME_FAILURE_MARKERS if marker.lower() in text.lower()]
        if failures:
            raise ReliabilityRuntimeError("runtime failure marker observed: " + ", ".join(failures))
        if all(text.count(marker) == 1 for marker in markers):
            validate_dual_guest_log(text, run_id=run_id)
            return text
        exit_code = launcher.poll()
        if exit_code is not None:
            raise ReliabilityRuntimeError(
                f"dual-Guest launcher exited before completion with code {exit_code}"
            )
        time.sleep(0.25)
    raise ReliabilityRuntimeError("timed out waiting for reliability completion markers")


def _parse_completion_summaries(
    text: str, markers: tuple[str, str]
) -> dict[str, dict[str, int]]:
    summaries: dict[str, dict[str, int]] = {}
    for marker in markers:
        matches = [line for line in text.splitlines() if marker in line]
        if len(matches) != 1:
            raise ReliabilityRuntimeError(f"completion marker must occur exactly once: {marker}")
        bare = re.sub(r"^(?:\x1b\[[0-9;?]*[A-Za-z])?\[VM \d+\]\s*", "", matches[0])
        fields = {
            name: int(value)
            for name, value in re.findall(r"([A-Za-z_]+)=([0-9]+)", bare)
        }
        if not fields:
            raise ReliabilityRuntimeError(f"completion marker has no counters: {marker}")
        summaries[marker] = fields
    return summaries


def _artifact_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"manifest.json", "status.json"}:
            continue
        if path.is_symlink():
            raise ReliabilityRuntimeError(f"runtime bundle contains a symlink: {path}")
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return records


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    publish_new_file(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        error_type=ReliabilityRuntimeError,
    )
