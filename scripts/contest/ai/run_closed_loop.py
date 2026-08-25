#!/usr/bin/env python3
"""Run the frozen P5 closed-loop contract and bounded Guest observation.

The ``--dry-run`` path reuses ``host_orchestration.py`` and remains an L2
host contract.  The non-dry-run TEST-017 path materializes immutable runtime
inputs, executes the guarded dual-Guest observer, and passes the observation
to the independent status-last finalizer.  ``ai_control_completed`` is only
published after the finalizer validates the complete L8 bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Callable

try:
    from .host_orchestration import (
        HostBundle,
        HostContractConfig,
        HostContractError,
        HostIdentity,
        run_host_contract,
    )
    from .prepare_linux_ai_rootfs import (
        RootfsPlanError,
        _json_file,
        _regular_directory,
        _regular_file,
        _safe_output_path,
    )
    from ..runtime.dual_guest import (
        RuntimeContractError,
        request_bounded_shutdown,
        build_axvisor_qemu_command,
        inject_qemu_identity_args,
        make_runtime_identity,
        validate_dual_guest_log,
    )
    from ..network.convert_rootfs_to_initramfs import convert as convert_initramfs
    from ..network.publish_network_artifacts import (
        write_frames_artifacts,
        write_split_logs,
    )
    from ..host_vm_carveout_io import publish_new_file
    from .fault_profile import (
        FaultProfileError,
        load_fault_profile,
        select_fault_case,
    )
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from host_orchestration import (  # type: ignore[no-redef]
        HostBundle,
        HostContractConfig,
        HostContractError,
        HostIdentity,
        run_host_contract,
    )
    from prepare_linux_ai_rootfs import (  # type: ignore[no-redef]
        RootfsPlanError,
        _json_file,
        _regular_directory,
        _regular_file,
        _safe_output_path,
    )
    from runtime.dual_guest import (  # type: ignore[no-redef]
        RuntimeContractError,
        request_bounded_shutdown,
        build_axvisor_qemu_command,
        inject_qemu_identity_args,
        make_runtime_identity,
        validate_dual_guest_log,
    )
    from network.convert_rootfs_to_initramfs import convert as convert_initramfs  # type: ignore[no-redef]
    from network.publish_network_artifacts import (  # type: ignore[no-redef]
        write_frames_artifacts,
        write_split_logs,
    )
    from host_vm_carveout_io import publish_new_file  # type: ignore[no-redef]
    # ``scripts/test`` discovery can load the P4 network ``fault_profile``
    # first.  Import this module by file identity so a same-named top-level
    # module cannot silently replace the P5 AI profile implementation.
    import importlib.util

    _fault_profile_path = Path(__file__).with_name("fault_profile.py")
    _fault_profile_spec = importlib.util.spec_from_file_location(
        "_contest_ai_fault_profile", _fault_profile_path
    )
    if _fault_profile_spec is None or _fault_profile_spec.loader is None:
        raise ImportError(f"cannot load AI fault profile: {_fault_profile_path}")
    _fault_profile_module = importlib.util.module_from_spec(_fault_profile_spec)
    sys.modules[_fault_profile_spec.name] = _fault_profile_module
    _fault_profile_spec.loader.exec_module(_fault_profile_module)
    FaultProfileError = _fault_profile_module.FaultProfileError
    load_fault_profile = _fault_profile_module.load_fault_profile
    select_fault_case = _fault_profile_module.select_fault_case


class RunnerPlanError(ValueError):
    """Raised when the documented Guest runner inputs are not bound safely."""


def _regular_bundle_directory(path: Path, label: str) -> Path:
    """Return a bundle root only when the root and every entry are non-links."""

    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_dir():
        raise RunnerPlanError(f"{label} must be a regular directory: {path}")
    for entry in candidate.rglob("*"):
        if entry.is_symlink():
            raise RunnerPlanError(f"{label} contains a symlink: {entry}")
    return candidate.absolute()


def _publisher_output_directory(path: Path, label: str) -> Path:
    """Return a publisher output path while allowing a fresh root to be created."""

    candidate = Path(path)
    if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
        raise RunnerPlanError(f"{label} must not be a symlink or non-directory: {path}")
    return candidate.absolute()


_LOCKED_LINUX_KERNEL = Path(
    "/root/.cache/tgoskits/axvisor-images/qemu_aarch64_linux/qemu-aarch64"
)


def _resolve_guest_vm_configs(
    *,
    linux_vmconfig: Path,
    zephyr_vmconfig: Path,
    linux_rootfs: Path,
    zephyr_image: Path,
    output: Path,
) -> dict[str, Any]:
    """Plan manifest-bound VM configs without materializing runtime files."""

    linux_source = _regular_file(linux_vmconfig, "Linux VM config")
    zephyr_source = _regular_file(zephyr_vmconfig, "Zephyr VM config")
    rootfs = _regular_file(linux_rootfs, "Linux rootfs")
    image = _regular_file(zephyr_image, "Zephyr image")
    linux_text = linux_source.read_text(encoding="utf-8")
    zephyr_text = zephyr_source.read_text(encoding="utf-8")
    if "ramdisk_path" in linux_text or "ramdisk_load_addr" in linux_text:
        raise RunnerPlanError("Linux VM config already contains a runner ramdisk binding")
    if "/guest/linux/linux-qemu" not in linux_text:
        raise RunnerPlanError("Linux VM config has no locked kernel placeholder")
    if "kernel_load_addr = 0x8020_0000" not in linux_text:
        raise RunnerPlanError("Linux VM config has no documented kernel load address")
    if "/path/to/zephyr.bin" not in zephyr_text:
        raise RunnerPlanError("Zephyr VM config has no manifest-bound image placeholder")

    linux_initramfs = output / "configs" / "linux-initramfs.cpio.gz"
    linux_resolved_text = linux_text.replace(
        "/guest/linux/linux-qemu", str(_LOCKED_LINUX_KERNEL)
    ).replace('image_location = "fs"', 'image_location = "memory"')
    linux_resolved_text = linux_resolved_text.replace(
        "kernel_load_addr = 0x8020_0000",
        "kernel_load_addr = 0x8020_0000\n"
        f'ramdisk_path = "{linux_initramfs.resolve()}"\n'
        "ramdisk_load_addr = 0x8800_0000",
        1,
    )
    zephyr_resolved_text = zephyr_text.replace(
        "/path/to/zephyr.bin", str(image.resolve())
    )
    linux_resolved = output / "configs" / "linux.resolved.toml"
    zephyr_resolved = output / "configs" / "zephyr.resolved.toml"
    return {
        "linux": {
            "source": _record_file(linux_source, "Linux VM config"),
            "resolved": {
                "path": str(linux_resolved),
                "sha256": hashlib.sha256(linux_resolved_text.encode("utf-8")).hexdigest(),
            },
            "kernel": {
                "path": str(_LOCKED_LINUX_KERNEL),
                "binding": "locked_axvisor_aarch64_cache",
            },
            "initramfs": {
                "path": str(linux_initramfs),
                "source_rootfs": _record_file(rootfs, "Linux rootfs"),
                "materialization": "convert_ext4_to_initramfs_cpio_gzip",
            },
        },
        "zephyr": {
            "source": _record_file(zephyr_source, "Zephyr VM config"),
            "resolved": {
                "path": str(zephyr_resolved),
                "sha256": hashlib.sha256(zephyr_resolved_text.encode("utf-8")).hexdigest(),
            },
            "image": _record_file(image, "Zephyr image"),
            "binding": "manifest_bound_placeholder",
        },
        "materialization": [
            {"path": str(linux_resolved), "operation": "write_resolved_linux_vmconfig"},
            {"path": str(zephyr_resolved), "operation": "write_resolved_zephyr_vmconfig"},
            {"path": str(linux_initramfs), "operation": "convert_rootfs_before_guest_start"},
        ],
        "non_claims": [
            "resolved VM config files and initramfs were not materialized",
            "the locked Linux kernel path was not checked on this host",
        ],
    }


def materialize_guest_vm_inputs(
    *,
    linux_vmconfig: Path,
    zephyr_vmconfig: Path,
    linux_rootfs: Path,
    zephyr_image: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Materialize only the fresh VM inputs described by the runtime plan.

    This is an explicit source-level step for the future Guest executor.  It
    does not launch Cargo/QEMU and deliberately returns no runtime evidence.
    The output directory is owned by this invocation and is removed if the
    conversion or config publication fails.
    """

    output = _safe_output_path(output_dir, "Guest VM input output directory")
    configs_dir = output / "configs"
    guest_configs = _resolve_guest_vm_configs(
        linux_vmconfig=linux_vmconfig,
        zephyr_vmconfig=zephyr_vmconfig,
        linux_rootfs=linux_rootfs,
        zephyr_image=zephyr_image,
        output=output,
    )
    linux_source = _regular_file(linux_vmconfig, "Linux VM config")
    zephyr_source = _regular_file(zephyr_vmconfig, "Zephyr VM config")
    image = _regular_file(zephyr_image, "Zephyr image")
    linux_text = linux_source.read_text(encoding="utf-8")
    linux_initramfs = Path(guest_configs["linux"]["initramfs"]["path"])
    linux_text = linux_text.replace(
        "/guest/linux/linux-qemu", str(_LOCKED_LINUX_KERNEL)
    ).replace('image_location = "fs"', 'image_location = "memory"')
    linux_text = linux_text.replace(
        "kernel_load_addr = 0x8020_0000",
        "kernel_load_addr = 0x8020_0000\n"
        f'ramdisk_path = "{linux_initramfs.resolve()}"\n'
        "ramdisk_load_addr = 0x8800_0000",
        1,
    )
    zephyr_text = zephyr_source.read_text(encoding="utf-8").replace(
        "/path/to/zephyr.bin", str(image.resolve())
    )
    created = False
    try:
        output.mkdir(parents=True)
        configs_dir.mkdir()
        initramfs_result = convert_initramfs(
            source_rootfs=_regular_file(linux_rootfs, "Linux rootfs"),
            output_initramfs=linux_initramfs,
        )
        for path, text in (
            (Path(guest_configs["linux"]["resolved"]["path"]), linux_text),
            (Path(guest_configs["zephyr"]["resolved"]["path"]), zephyr_text),
        ):
            with path.open("x", encoding="utf-8", newline="") as target:
                target.write(text)
        created = True
    except (OSError, ValueError) as error:
        raise RunnerPlanError(f"Guest VM input materialization failed: {error}") from error
    finally:
        if not created:
            shutil.rmtree(output, ignore_errors=True)
    return {
        "schema_version": "p5-ai-vm-inputs-v1",
        "execution": "completed",
        "qualified": False,
        "guest_configs": guest_configs,
        "initramfs": {
            **dict(guest_configs["linux"]["initramfs"]),
            "result": initramfs_result,
            "sha256": _sha256(linux_initramfs),
            "size": linux_initramfs.stat().st_size,
        },
        "non_claims": [
            "Cargo, WSL, QEMU, Linux Guest, Zephyr Guest, Guest-IP, and AI-loop were not executed",
            "no packet capture, event transcript, trajectory, or qualification status was produced",
        ],
    }


def materialize_guest_runtime_inputs(
    *,
    qemu_config: Path,
    linux_vmconfig: Path,
    zephyr_vmconfig: Path,
    linux_rootfs: Path,
    zephyr_image: Path,
    output_dir: Path,
    qmp_socket: Path,
    qemu_pidfile: Path,
    qemu_name: str,
) -> dict[str, Any]:
    """Materialize the complete resolved input set for a future Guest run.

    This is deliberately a pre-launch boundary: it writes only fresh QEMU/
    VM configs and the converted initramfs, and never invokes Cargo, QEMU, or
    a Guest.  The output is removed on any failure so a later executor cannot
    accidentally consume a partial identity/config set.
    """

    output = _safe_output_path(output_dir, "Guest runtime input output directory")
    qemu_source = _regular_file(qemu_config, "QEMU config")
    qemu_text = qemu_source.read_text(encoding="utf-8")
    guest_configs = _resolve_guest_vm_configs(
        linux_vmconfig=linux_vmconfig,
        zephyr_vmconfig=zephyr_vmconfig,
        linux_rootfs=linux_rootfs,
        zephyr_image=zephyr_image,
        output=output,
    )
    try:
        resolved_qemu_text = inject_qemu_identity_args(
            qemu_text,
            qmp_socket=Path(qmp_socket).absolute(),
            qemu_pidfile=Path(qemu_pidfile).absolute(),
            qemu_name=qemu_name,
        )
    except (OSError, ValueError) as error:
        raise RunnerPlanError(f"QEMU config resolution failed: {error}") from error

    linux_source = _regular_file(linux_vmconfig, "Linux VM config")
    zephyr_source = _regular_file(zephyr_vmconfig, "Zephyr VM config")
    image = _regular_file(zephyr_image, "Zephyr image")
    linux_text = linux_source.read_text(encoding="utf-8")
    linux_initramfs = Path(guest_configs["linux"]["initramfs"]["path"])
    linux_text = linux_text.replace(
        "/guest/linux/linux-qemu", str(_LOCKED_LINUX_KERNEL)
    ).replace('image_location = "fs"', 'image_location = "memory"')
    linux_text = linux_text.replace(
        "kernel_load_addr = 0x8020_0000",
        "kernel_load_addr = 0x8020_0000\n"
        f'ramdisk_path = "{linux_initramfs.resolve()}"\n'
        "ramdisk_load_addr = 0x8800_0000",
        1,
    )
    zephyr_text = zephyr_source.read_text(encoding="utf-8").replace(
        "/path/to/zephyr.bin", str(image.resolve())
    )
    configs_dir = output / "configs"
    resolved_qemu = configs_dir / "qemu.resolved.toml"
    created = False
    try:
        output.mkdir(parents=True)
        configs_dir.mkdir()
        initramfs_result = convert_initramfs(
            source_rootfs=_regular_file(linux_rootfs, "Linux rootfs"),
            output_initramfs=linux_initramfs,
        )
        for path, text in (
            (resolved_qemu, resolved_qemu_text),
            (Path(guest_configs["linux"]["resolved"]["path"]), linux_text),
            (Path(guest_configs["zephyr"]["resolved"]["path"]), zephyr_text),
        ):
            with path.open("x", encoding="utf-8", newline="") as target:
                target.write(text)
        created = True
    except (OSError, ValueError) as error:
        raise RunnerPlanError(f"Guest runtime input materialization failed: {error}") from error
    finally:
        if not created:
            shutil.rmtree(output, ignore_errors=True)
    return {
        "schema_version": "p5-ai-runtime-inputs-v1",
        "execution": "completed",
        "qualified": False,
        "resolved_qemu": {
            "path": str(resolved_qemu),
            "sha256": _sha256(resolved_qemu),
            "source": _record_file(qemu_source, "QEMU config"),
        },
        "guest_configs": guest_configs,
        "initramfs": {
            **dict(guest_configs["linux"]["initramfs"]),
            "result": initramfs_result,
            "sha256": _sha256(linux_initramfs),
            "size": linux_initramfs.stat().st_size,
        },
        "runtime_identity": {
            "qmp_socket": str(Path(qmp_socket).absolute()),
            "qemu_pidfile": str(Path(qemu_pidfile).absolute()),
            "qemu_name": qemu_name,
        },
        "non_claims": [
            "Cargo, WSL, QEMU, Linux Guest, Zephyr Guest, Guest-IP, and AI-loop were not executed",
            "no packet capture, event transcript, trajectory, or qualification status was produced",
        ],
    }


def validate_materialized_guest_runtime_inputs(
    plan: dict[str, Any], materialized: dict[str, Any]
) -> None:
    """Verify that materialized files still match one immutable runtime plan."""

    validate_guest_runtime_plan(plan)
    if materialized.get("schema_version") != "p5-ai-runtime-inputs-v1":
        raise RunnerPlanError("materialized runtime inputs have the wrong schema")
    if materialized.get("execution") != "completed" or materialized.get("qualified") is not False:
        raise RunnerPlanError("materialized runtime inputs must be completed and unqualified")

    def check_claim(
        actual: Any, expected: Any, label: str, *, require_source: bool = False
    ) -> None:
        if not isinstance(actual, dict) or not isinstance(expected, dict):
            raise RunnerPlanError(f"{label} claim is missing")
        actual_path = actual.get("path")
        expected_path = expected.get("path")
        actual_sha = actual.get("sha256")
        expected_sha = expected.get("sha256")
        if not all(isinstance(value, str) and value for value in (actual_path, expected_path, actual_sha, expected_sha)):
            raise RunnerPlanError(f"{label} claim is incomplete")
        if Path(actual_path).absolute() != Path(expected_path).absolute():
            raise RunnerPlanError(f"{label} path drifted from runtime plan")
        if actual_sha != expected_sha:
            raise RunnerPlanError(f"{label} SHA-256 drifted from runtime plan")
        try:
            materialized_path = _regular_file(Path(actual_path), label)
            if _sha256(materialized_path) != actual_sha:
                raise RunnerPlanError(f"{label} on-disk SHA-256 does not match claim")
        except (OSError, RootfsPlanError) as error:
            raise RunnerPlanError(f"{label} cannot be verified: {error}") from error
        if require_source and not isinstance(actual.get("source"), dict):
            raise RunnerPlanError(f"{label} source claim is missing")

    check_claim(
        materialized.get("resolved_qemu"),
        plan.get("resolved_qemu"),
        "resolved QEMU config",
        require_source=True,
    )
    actual_guests = materialized.get("guest_configs")
    planned_guests = plan.get("guest_configs")
    if not isinstance(actual_guests, dict) or not isinstance(planned_guests, dict):
        raise RunnerPlanError("materialized guest config claims are missing")
    for guest in ("linux", "zephyr"):
        actual = actual_guests.get(guest)
        planned = planned_guests.get(guest)
        if not isinstance(actual, dict) or not isinstance(planned, dict):
            raise RunnerPlanError(f"materialized {guest} config claim is missing")
        check_claim(actual.get("resolved"), planned.get("resolved"), f"{guest} resolved config")

    actual_initramfs = materialized.get("initramfs")
    planned_initramfs = planned_guests.get("linux", {}).get("initramfs")
    if not isinstance(actual_initramfs, dict) or not isinstance(planned_initramfs, dict):
        raise RunnerPlanError("materialized initramfs claim is missing")
    check_claim(actual_initramfs, planned_initramfs, "Linux initramfs")

    actual_identity = materialized.get("runtime_identity")
    planned_identity = plan.get("runtime_identity")
    if not isinstance(actual_identity, dict) or not isinstance(planned_identity, dict):
        raise RunnerPlanError("materialized runtime identity is missing")
    for field in ("qmp_socket", "qemu_pidfile", "qemu_name"):
        if actual_identity.get(field) != planned_identity.get(field):
            raise RunnerPlanError(f"materialized runtime identity {field} drifted")

    argv = plan["command"]["argv"]
    expected_paths = (
        plan["resolved_qemu"]["path"],
        plan["guest_configs"]["linux"]["resolved"]["path"],
        plan["guest_configs"]["zephyr"]["resolved"]["path"],
    )
    if any(path not in argv for path in expected_paths):
        raise RunnerPlanError("fixed AxVisor argv does not reference every materialized config")


def publish_guest_raw_capture(
    *, output_dir: Path, live_log: Path, run_id: str, session_id: int
) -> dict[str, Any]:
    """Publish only artifacts directly observable in one AxVisor live log.

    The merged log is copied byte-for-byte, then the existing VM-attributed
    splitter and frame parser consume that same text.  No completion marker,
    trajectory, or summary is accepted as a substitute for raw capture.
    """

    output = _publisher_output_directory(output_dir, "observation output")
    source = _regular_file(live_log, "AxVisor live log")
    try:
        log_bytes = source.read_bytes()
        log_text = log_bytes.decode("utf-8", errors="replace")
        logs_dir = output / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        axvisor_log = logs_dir / "axvisor.raw.log"
        with axvisor_log.open("xb") as target:
            target.write(log_bytes)
        split = write_split_logs(output, log_text)
        if set(split) != {"linux", "zephyr"}:
            raise RunnerPlanError(
                "AxVisor live log has no complete Linux/Zephyr VM attribution"
            )
        frames = write_frames_artifacts(
            output,
            log_text,
            run_id=run_id,
            session_id=str(session_id),
        )
        if set(frames) != {"frames", "pcap"}:
            raise RunnerPlanError(
                "AxVisor live log has no accepted virtio-net frame evidence"
            )
    except (OSError, ValueError) as error:
        raise RunnerPlanError(f"raw Guest capture publication failed: {error}") from error
    return {
        "schema_version": "p5-ai-raw-capture-v1",
        "execution": "completed",
        "qualified": False,
        "source": {"path": str(source), "size": len(log_bytes), "sha256": _sha256(source)},
        "files": {
            "axvisor": {"path": str(axvisor_log), "size": axvisor_log.stat().st_size, "sha256": _sha256(axvisor_log)},
            "linux": {"path": str(split["linux"]), "size": split["linux"].stat().st_size, "sha256": _sha256(split["linux"])},
            "zephyr": {"path": str(split["zephyr"]), "size": split["zephyr"].stat().st_size, "sha256": _sha256(split["zephyr"])},
            "frames": {"path": str(frames["frames"]), "size": frames["frames"].stat().st_size, "sha256": _sha256(frames["frames"])},
            "pcap": {"path": str(frames["pcap"]), "size": frames["pcap"].stat().st_size, "sha256": _sha256(frames["pcap"])},
        },
        "non_claims": [
            "ICPC attempt records, Linux/Zephyr event transcripts, trajectory, metrics, and qualification were not produced by this publisher",
            "capture publication does not prove Guest-IP, AI-loop, or ai_control_completed",
        ],
    }


_RUNTIME_EVENT_FIELDS = (
    "schema_version",
    "run_id",
    "endpoint",
    "scenario",
    "transport",
    "session_id",
    "sequence",
    "request_id",
    "sample_index",
    "event",
    "monotonic_ns",
    "value",
    "unit",
    "outcome",
)

_P5_STATUS_SCHEMA = "p5-ai-status-v1"
_P5_SESSION_SCHEMA = "p5-ai-session-v1"
_P5_MANIFEST_SCHEMA = "p5-ai-manifest-v1"
_P5_STATUS_TOKENS = {"ai_control_failed", "ai_control_blocked"}
_P5_STATUS_CHECKS = {"oracle", "hashes", "validator", "cleanup"}


def publish_guest_event_transcripts(
    *, output_dir: Path, run_id: str, session_id: int | None = None
) -> dict[str, Any]:
    """Extract VM-attributed JSON events without rewriting or synthesizing them."""

    output = _publisher_output_directory(output_dir, "runtime output")
    metrics_dir = output / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}
    for guest, vm_id in (("linux", 1), ("zephyr", 2)):
        source = _regular_file(output / "logs" / f"{guest}.raw.log", f"{guest} raw log")
        records: list[str] = []
        for line_number, raw_line in enumerate(
            source.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            marker = f"[VM {vm_id}] "
            marker_index = raw_line.find(marker)
            if marker_index < 0:
                continue
            payload = raw_line[marker_index + len(marker) :].strip()
            if not payload.startswith("{"):
                continue
            try:
                record = json.loads(payload)
            except json.JSONDecodeError as error:
                raise RunnerPlanError(
                    f"{guest} event line {line_number} is not valid JSON: {error}"
                ) from error
            if not isinstance(record, dict):
                raise RunnerPlanError(f"{guest} event line {line_number} is not an object")
            if record.get("schema_version") == "p5-ai-icpc-attempt-v1":
                if guest == "zephyr" and record.get("direction") != "zephyr_to_linux":
                    raise RunnerPlanError(
                        f"{guest} attempt line {line_number} direction drifted"
                    )
                if guest == "linux" and record.get("direction") != "linux_to_zephyr":
                    raise RunnerPlanError(
                        f"{guest} attempt line {line_number} direction drifted"
                    )
                continue
            missing = [field for field in _RUNTIME_EVENT_FIELDS if field not in record]
            if missing:
                raise RunnerPlanError(
                    f"{guest} event line {line_number} is missing fields: {missing}"
                )
            if (
                record["schema_version"] != "p5-ai-event-v1"
                or record["run_id"] != run_id
                or record["endpoint"] != guest
                or record["scenario"] != "test-017"
            ):
                raise RunnerPlanError(
                    f"{guest} event line {line_number} identity/schema drifted"
                )
            if session_id is not None and record["session_id"] != session_id:
                raise RunnerPlanError(
                    f"{guest} event line {line_number} session identity drifted"
                )
            records.append(payload)
        if not records:
            raise RunnerPlanError(f"{guest} raw log has no p5-ai-event-v1 records")
        target = metrics_dir / f"{guest}-events.jsonl"
        publish_new_file(
            target,
            ("\n".join(records) + "\n").encode("utf-8"),
            error_type=RunnerPlanError,
        )
        results[guest] = {
            "path": str(target),
            "size": target.stat().st_size,
            "sha256": _sha256(target),
            "records": len(records),
        }
    return {
        "schema_version": "p5-ai-event-transcripts-v1",
        "execution": "completed",
        "qualified": False,
        "files": results,
        "non_claims": [
            "event transcript extraction does not produce trajectory, ICPC records, metrics, or qualification",
            "records are accepted only when emitted by VM-attributed raw logs",
        ],
    }


def publish_guest_observation_bundle(
    *, output_dir: Path, live_log: Path, run_id: str, session_id: int
) -> dict[str, Any]:
    """Close one raw Guest observation without deriving qualification evidence.

    The caller must provide a fresh output directory and one immutable live
    log. This coordinator validates the official dual-Guest identity before
    publishing event transcripts, then records only raw-capture and
    event-transcript claims. It never runs a command, reconstructs ICPC
    records/trajectory, or writes ``status.json``.
    """

    output = _publisher_output_directory(output_dir, "execution output")
    if output.exists():
        raise RunnerPlanError(f"observation output directory must be fresh: {output}")
    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id <= 0:
        raise RunnerPlanError("observation session_id must be a positive integer")
    raw_capture = publish_guest_raw_capture(
        output_dir=output,
        live_log=live_log,
        run_id=run_id,
        session_id=session_id,
    )
    try:
        observation = validate_dual_guest_log(
            (output / "logs" / "axvisor.raw.log").read_text(
                encoding="utf-8", errors="replace"
            ),
            run_id=run_id,
            require_complete=True,
        )
    except (OSError, RuntimeContractError) as error:
        raise RunnerPlanError(f"dual-Guest observation contract failed: {error}") from error
    transcripts = publish_guest_event_transcripts(
        output_dir=output, run_id=run_id, session_id=session_id
    )
    result = {
        "schema_version": "p5-ai-observation-v1",
        "run_id": run_id,
        "session_id": session_id,
        "execution": "completed",
        "qualified": False,
        "runtime_contract": {
            "dtb_vms": sorted(observation.dtb),
            "ready_vms": sorted(observation.ready),
            "drop_summaries": list(observation.drop_summaries),
        },
        "raw_capture": raw_capture,
        "event_transcripts": transcripts,
        "non_claims": [
            "observation publication does not prove Guest-IP or AI-loop completion",
            "ICPC attempts, trajectory, derived metrics, cleanup, and qualification were not produced",
            "status.json was deliberately not written; a later validator must publish status-last",
        ],
    }
    observation_path = output / "observation.json"
    publish_new_file(
        observation_path,
        (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        error_type=RunnerPlanError,
    )
    result["file"] = {
        "path": str(observation_path),
        "size": observation_path.stat().st_size,
        "sha256": _sha256(observation_path),
    }
    return result


def validate_guest_observation_bundle(
    output_dir: Path, *, run_id: str, session_id: int, allow_execution: bool = False
) -> dict[str, Any]:
    """Validate a raw observation package before any status-last finalizer.

    This consumer verifies only immutable observation facts. It rejects a
    terminal status, path/hash/size drift and event identity drift, while
    deliberately refusing to infer ICPC, trajectory, cleanup or qualification
    from the observation package.
    """

    output = _regular_bundle_directory(output_dir, "observation output")
    observation_path = _regular_file(output / "observation.json", "observation manifest")
    if (output / "status.json").exists():
        raise RunnerPlanError("observation package already has a terminal status")
    try:
        value = json.loads(observation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RunnerPlanError(f"observation manifest cannot be read: {error}") from error
    if not isinstance(value, dict):
        raise RunnerPlanError("observation manifest must be an object")
    if (
        value.get("schema_version") != "p5-ai-observation-v1"
        or value.get("run_id") != run_id
        or value.get("session_id") != session_id
        or value.get("execution") != "completed"
        or value.get("qualified") is not False
    ):
        raise RunnerPlanError("observation manifest identity/schema drifted")

    def verify_claim(claim: Any, label: str) -> Path:
        if not isinstance(claim, dict):
            raise RunnerPlanError(f"{label} claim is missing")
        path_value = claim.get("path")
        size = claim.get("size")
        sha256 = claim.get("sha256")
        if (
            not isinstance(path_value, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(sha256, str)
        ):
            raise RunnerPlanError(f"{label} claim is malformed")
        path = Path(path_value).absolute()
        try:
            path.relative_to(output)
        except ValueError as error:
            raise RunnerPlanError(f"{label} escapes observation output") from error
        actual = _regular_file(path, label)
        if actual.stat().st_size != size or _sha256(actual) != sha256:
            raise RunnerPlanError(f"{label} size/SHA-256 drifted")
        return actual

    raw_capture = value.get("raw_capture")
    if not isinstance(raw_capture, dict) or raw_capture.get("schema_version") != "p5-ai-raw-capture-v1":
        raise RunnerPlanError("raw capture claim is missing or has the wrong schema")
    if raw_capture.get("execution") != "completed" or raw_capture.get("qualified") is not False:
        raise RunnerPlanError("raw capture must remain completed and unqualified")
    raw_files = raw_capture.get("files")
    if not isinstance(raw_files, dict) or set(raw_files) != {"axvisor", "linux", "zephyr", "frames", "pcap"}:
        raise RunnerPlanError("raw capture file set is incomplete")
    claimed_paths: set[str] = set()
    for key, claim in raw_files.items():
        path = verify_claim(claim, f"raw capture {key}")
        claimed_paths.add(path.relative_to(output).as_posix())

    transcripts = value.get("event_transcripts")
    if not isinstance(transcripts, dict) or transcripts.get("schema_version") != "p5-ai-event-transcripts-v1":
        raise RunnerPlanError("event transcript claim is missing or has the wrong schema")
    transcript_files = transcripts.get("files")
    if not isinstance(transcript_files, dict) or set(transcript_files) != {"linux", "zephyr"}:
        raise RunnerPlanError("event transcript file set is incomplete")
    for guest, claim in transcript_files.items():
        path = verify_claim(claim, f"{guest} event transcript")
        claimed_paths.add(path.relative_to(output).as_posix())
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            raise RunnerPlanError(f"{guest} event transcript is empty")
        for line_number, line in enumerate(lines, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise RunnerPlanError(
                    f"{guest} event transcript line {line_number} is invalid JSON"
                ) from error
            if not isinstance(record, dict):
                raise RunnerPlanError(f"{guest} event transcript line {line_number} is not an object")
            missing = [field for field in _RUNTIME_EVENT_FIELDS if field not in record]
            if missing:
                raise RunnerPlanError(
                    f"{guest} event transcript line {line_number} is missing fields: {missing}"
                )
            if (
                record["schema_version"] != "p5-ai-event-v1"
                or record["run_id"] != run_id
                or record["endpoint"] != guest
                or record["scenario"] != "test-017"
                or record["session_id"] != session_id
            ):
                raise RunnerPlanError(
                    f"{guest} event transcript line {line_number} identity drifted"
                )
    runtime_contract = value.get("runtime_contract")
    if not isinstance(runtime_contract, dict):
        raise RunnerPlanError("runtime contract claim is missing")
    if runtime_contract.get("dtb_vms") != [1, 2] or runtime_contract.get("ready_vms") != [1, 2]:
        raise RunnerPlanError("observation does not contain both Guest identity claims")
    expected_files = claimed_paths | {"observation.json"}
    if allow_execution:
        expected_files.add("execution.json")
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for entry in output.rglob("*"):
        relative = entry.relative_to(output).as_posix()
        if entry.is_file():
            actual_files.add(relative)
        elif entry.is_dir():
            actual_dirs.add(relative)
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise RunnerPlanError(
            f"observation bundle file set mismatch missing={missing} extra={extra}"
        )
    expected_dirs = {
        Path(relative).parent.as_posix()
        for relative in expected_files
        if Path(relative).parent != Path(".")
    }
    if actual_dirs != expected_dirs:
        raise RunnerPlanError("observation bundle directory set drifted")
    return {
        "schema_version": "p5-ai-observation-validation-v1",
        "valid": True,
        "run_id": run_id,
        "session_id": session_id,
        "qualified": False,
        "non_claims": [
            "observation validation does not produce status, ICPC, trajectory, cleanup, or qualification"
        ],
    }


def _runtime_bundle_file_records(output: Path) -> dict[str, dict[str, Any]]:
    """Return byte-bound records for a partial Guest runtime bundle."""

    records: dict[str, dict[str, Any]] = {}
    for path in sorted(output.rglob("*")):
        if path.is_symlink():
            raise RunnerPlanError(f"runtime bundle refuses symlinked entry: {path}")
        if not path.is_file() or path.name in {"manifest.json", "session.json", "status.json"}:
            continue
        try:
            relative = path.relative_to(output).as_posix()
        except ValueError as error:  # pragma: no cover - rglob is output-bound
            raise RunnerPlanError(f"runtime bundle artifact escapes output: {path}") from error
        records[relative] = {
            "size": path.stat().st_size,
            "sha256": _sha256(path),
            "producer": "scripts/contest/ai/run_closed_loop.py",
        }
    if not records:
        raise RunnerPlanError("runtime bundle has no immutable artifacts")
    return records


def publish_guest_runtime_status(
    output_dir: Path,
    *,
    run_id: str,
    session_id: int,
    controller: str,
    seed: int,
    profile_id: str,
    status_token: str,
    primary_error: str,
    blocked_reason: str | None = None,
    cleanup_error: str | None = None,
) -> dict[str, Any]:
    """Publish a status-last partial Guest bundle without claiming AI success.

    The current executor only produces raw Guest observation.  Therefore this
    finalizer permits the documented failed/blocked tokens and deliberately
    rejects ``ai_control_completed`` until an independent TEST-017 validator
    has produced the complete ICPC, trajectory, model and cleanup bundle.
    """

    output = _regular_bundle_directory(output_dir, "runtime output")
    if (output / "status.json").exists():
        raise RunnerPlanError("runtime bundle already has a terminal status")
    if not isinstance(run_id, str) or not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        raise RunnerPlanError("status run_id is not path-safe")
    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id <= 0:
        raise RunnerPlanError("status session_id must be a positive integer")
    if controller not in ("fixed", "mlp") or seed not in (7, 19, 43):
        raise RunnerPlanError("status controller/seed is outside the frozen TEST-017 identity")
    if not isinstance(profile_id, str) or not profile_id or "/" in profile_id or "\\" in profile_id:
        raise RunnerPlanError("status profile_id is not safe")
    if not isinstance(primary_error, str) or not primary_error.strip():
        raise RunnerPlanError("failed/blocked status requires primary_error")
    if status_token not in _P5_STATUS_TOKENS:
        raise RunnerPlanError("unsupported runtime status token")
    if status_token == "ai_control_blocked":
        if blocked_reason is None or not isinstance(blocked_reason, str) or not blocked_reason.strip():
            raise RunnerPlanError("blocked status requires a non-empty blocked_reason")
    elif blocked_reason is not None:
        raise RunnerPlanError("failed status cannot contain blocked_reason")
    if cleanup_error is not None and (not isinstance(cleanup_error, str) or not cleanup_error.strip()):
        raise RunnerPlanError("cleanup_error must be a non-empty string when present")

    validate_guest_execution_manifest(output, run_id=run_id, session_id=session_id)
    try:
        execution = json.loads((output / "execution.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RunnerPlanError(f"execution manifest cannot be read for status publication: {error}") from error
    command = execution.get("command") if isinstance(execution, dict) else None
    if not isinstance(command, dict):
        raise RunnerPlanError("execution manifest has no command provenance")
    command_fields = {"argv", "cwd", "started_monotonic_ns", "finished_monotonic_ns", "exit_code"}
    if set(command) != command_fields or not isinstance(command["argv"], list) or not command["argv"]:
        raise RunnerPlanError("execution command provenance is incomplete")
    if not isinstance(command["cwd"], str) or not isinstance(command["exit_code"], int):
        raise RunnerPlanError("execution command provenance has invalid types")
    if not isinstance(command["started_monotonic_ns"], int) or not isinstance(command["finished_monotonic_ns"], int):
        raise RunnerPlanError("execution command timestamps are missing")
    runtime_identity = execution.get("runtime_identity")
    if not isinstance(runtime_identity, dict) or not isinstance(runtime_identity.get("runtime_dir"), str):
        raise RunnerPlanError("execution runtime identity is incomplete")

    commands_path = output / "commands.jsonl"
    cleanup_path = output / "cleanup.json"
    manifest_path = output / "manifest.json"
    session_path = output / "session.json"
    for path in (commands_path, cleanup_path, manifest_path, session_path):
        if path.exists():
            raise RunnerPlanError(f"refusing to overwrite {path}")
    publish_new_file(
        commands_path,
        (json.dumps(command, sort_keys=True) + "\n").encode("utf-8"),
        error_type=RunnerPlanError,
    )
    cleanup = {
        "schema_version": "p5-ai-cleanup-v1",
        "residualProcesses": [],
        "residualSockets": [],
        "residualFiles": [],
        "runtimeDirRemoved": not Path(runtime_identity["runtime_dir"]).exists(),
        "cleanupError": cleanup_error,
    }
    publish_new_file(
        cleanup_path,
        (json.dumps(cleanup, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        error_type=RunnerPlanError,
    )
    files = _runtime_bundle_file_records(output)
    manifest = {
        "schema_version": _P5_MANIFEST_SCHEMA,
        "run_id": run_id,
        "files": files,
        "bundle_kind": "partial_guest_observation",
        "non_claims": [
            "this partial bundle does not contain the complete TEST-017 model/ICPC/trajectory evidence",
            "status is failed or blocked and cannot qualify P5",
        ],
    }
    publish_new_file(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        error_type=RunnerPlanError,
    )
    manifest_sha256 = _sha256(manifest_path)
    session = {
        "schema_version": _P5_SESSION_SCHEMA,
        "run_id": run_id,
        "session_id": session_id,
        "scenario": "test-017",
        "controller": controller,
        "seed": seed,
        "profile_id": profile_id,
        "execution_kind": "guest_runtime",
        "evidence_level": "L3 guest observation",
        "manifest_sha256": manifest_sha256,
    }
    publish_new_file(
        session_path,
        (json.dumps(session, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        error_type=RunnerPlanError,
    )
    status: dict[str, Any] = {
        "schema_version": _P5_STATUS_SCHEMA,
        "success": False,
        "status": status_token,
        "primaryError": primary_error.strip(),
        "cleanupError": cleanup_error,
        "completedChecks": ["cleanup"] if cleanup_error is None else [],
        "manifestSha256": manifest_sha256,
        "statusLast": True,
    }
    if status_token == "ai_control_blocked":
        status["blockedReason"] = blocked_reason.strip()
    status_path = output / "status.json"
    with status_path.open("x", encoding="utf-8") as status_handle:
        status_handle.write(
            json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
    validate_guest_runtime_status(output, run_id=run_id, session_id=session_id)
    return {
        "schema_version": "p5-ai-status-publication-v1",
        "run_id": run_id,
        "session_id": session_id,
        "status": status_token,
        "success": False,
        "qualified": False,
        "file": {"path": str(status_path), "size": status_path.stat().st_size, "sha256": _sha256(status_path)},
        "non_claims": ["status publication does not produce ai_control_completed or L8 qualification"],
    }


def validate_guest_runtime_status(
    output_dir: Path, *, run_id: str, session_id: int
) -> dict[str, Any]:
    """Consume the status-last partial runtime bundle and reject false success."""

    output = _regular_bundle_directory(output_dir, "runtime output")
    status_path = _regular_file(output / "status.json", "runtime status")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(status, dict):
        raise RunnerPlanError("runtime status must be an object")
    allowed = {
        "schema_version", "success", "status", "primaryError", "cleanupError",
        "completedChecks", "manifestSha256", "statusLast", "blockedReason",
    }
    if set(status) - allowed:
        raise RunnerPlanError("runtime status contains unreviewed fields")
    if status.get("schema_version") != _P5_STATUS_SCHEMA or status.get("status") not in _P5_STATUS_TOKENS:
        raise RunnerPlanError("runtime status schema/token is invalid")
    if status.get("success") is not False or status.get("statusLast") is not True:
        raise RunnerPlanError("runtime status is not fail-closed and status-last")
    if not isinstance(status.get("primaryError"), str) or not status["primaryError"]:
        raise RunnerPlanError("failed runtime status requires primaryError")
    checks = status.get("completedChecks")
    if not isinstance(checks, list) or len(checks) != len(set(checks)) or not set(checks) <= _P5_STATUS_CHECKS:
        raise RunnerPlanError("runtime status completedChecks is invalid")
    if status["status"] == "ai_control_blocked":
        if not isinstance(status.get("blockedReason"), str) or not status["blockedReason"]:
            raise RunnerPlanError("blocked runtime status requires blockedReason")
    elif "blockedReason" in status:
        raise RunnerPlanError("failed runtime status cannot contain blockedReason")
    session = json.loads(_regular_file(output / "session.json", "runtime session").read_text(encoding="utf-8"))
    manifest_path = _regular_file(output / "manifest.json", "runtime manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(session, dict)
        or session.get("schema_version") != _P5_SESSION_SCHEMA
        or session.get("run_id") != run_id
        or session.get("session_id") != session_id
        or session.get("manifest_sha256") != _sha256(manifest_path)
        or status.get("manifestSha256") != _sha256(manifest_path)
    ):
        raise RunnerPlanError("runtime session/status manifest binding drifted")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != _P5_MANIFEST_SCHEMA or manifest.get("run_id") != run_id:
        raise RunnerPlanError("runtime manifest identity/schema drifted")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise RunnerPlanError("runtime manifest has no files")
    actual_files = _runtime_bundle_file_records(output)
    if set(files) != set(actual_files):
        raise RunnerPlanError("runtime manifest file set is not exact")
    actual_dirs = {
        entry.relative_to(output).as_posix()
        for entry in output.rglob("*")
        if entry.is_dir()
    }
    expected_dirs = {
        Path(relative).parent.as_posix()
        for relative in files
        if Path(relative).parent != Path(".")
    }
    if actual_dirs != expected_dirs:
        raise RunnerPlanError("runtime bundle directory set is not exact")
    for relative, claim in files.items():
        path = (output / relative).absolute()
        try:
            path.relative_to(output)
        except ValueError as error:
            raise RunnerPlanError(f"runtime manifest path escapes output: {relative}") from error
        actual = _regular_file(path, f"runtime artifact {relative}")
        if (
            not isinstance(claim, dict)
            or not isinstance(claim.get("producer"), str)
            or not claim["producer"]
            or claim.get("size") != actual.stat().st_size
            or claim.get("sha256") != _sha256(actual)
        ):
            raise RunnerPlanError(f"runtime manifest artifact drifted: {relative}")
    return {
        "schema_version": "p5-ai-status-validation-v1",
        "valid": True,
        "run_id": run_id,
        "session_id": session_id,
        "status": status["status"],
        "success": False,
        "qualified": False,
    }


def execute_guest_runtime(
    plan: dict[str, Any],
    materialized: dict[str, Any],
    *,
    output_dir: Path,
    run_id: str,
    session_id: int,
    ready_timeout_seconds: float = 300.0,
    runtime_timeout_seconds: float = 600.0,
    shutdown_timeout_seconds: float = 30.0,
    popen_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Execute one resolved Guest command and publish only raw observation.

    The command is intentionally injectable for host tests, while the normal
    path uses ``subprocess.Popen`` with the immutable plan argv. No host clock,
    host socket or host trajectory is used. A failed attempt leaves the
    runner-owned runtime directory intact for evidence inspection; a success
    copies raw observation first and removes only that owned runtime directory.
    This function never writes ``status.json`` or returns qualification.
    """

    validate_guest_runtime_plan(plan)
    validate_materialized_guest_runtime_inputs(plan, materialized)
    if plan.get("run_id") != run_id or plan.get("session_id") != session_id:
        raise RunnerPlanError("execution run_id does not match runtime plan")
    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id <= 0:
        raise RunnerPlanError("execution session_id must be a positive integer")
    if ready_timeout_seconds <= 0 or runtime_timeout_seconds <= 0 or shutdown_timeout_seconds <= 0:
        raise RunnerPlanError("execution timeouts must be positive")
    if runtime_timeout_seconds != 600.0:
        raise RunnerPlanError("TEST-017 runtime timeout must be exactly 600 seconds")

    output = Path(output_dir).absolute()
    if output.exists():
        raise RunnerPlanError(f"execution output directory must be fresh: {output}")
    identity = plan.get("runtime_identity")
    command = plan.get("command")
    if not isinstance(identity, dict) or not isinstance(command, dict):
        raise RunnerPlanError("runtime execution identity/command is missing")
    runtime_dir = Path(identity.get("runtime_dir", "")).absolute()
    live_log = Path(identity.get("live_log", "")).absolute()
    if not str(identity.get("runtime_dir", "")) or not str(identity.get("live_log", "")):
        raise RunnerPlanError("runtime execution directory/live log is missing")
    if live_log.parent != runtime_dir:
        raise RunnerPlanError("live log is outside runner-owned runtime directory")
    if runtime_dir.exists():
        raise RunnerPlanError(f"runner-owned runtime directory already exists: {runtime_dir}")
    argv = command.get("argv")
    cwd = command.get("cwd")
    if not isinstance(argv, list) or not argv or not isinstance(cwd, str) or not cwd:
        raise RunnerPlanError("runtime execution command is incomplete")

    launcher: Any = None
    saw_complete_contract = False
    exit_code: int | None = None
    controlled_shutdown = False
    started_monotonic_ns: int | None = None
    finished_monotonic_ns: int | None = None
    runtime_dir.mkdir(parents=True)
    try:
        with live_log.open("xb") as log_handle:
            started_monotonic_ns = time.monotonic_ns()
            launcher = (popen_factory or subprocess.Popen)(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            ready_deadline = time.monotonic() + ready_timeout_seconds
            runtime_deadline = time.monotonic() + runtime_timeout_seconds
            while True:
                text = live_log.read_text(encoding="utf-8", errors="replace")
                try:
                    partial = validate_dual_guest_log(
                        text, run_id=run_id, require_complete=False
                    )
                except RuntimeContractError as error:
                    raise RunnerPlanError(
                        f"Guest runtime contract failed while running: {error}"
                    ) from error
                if len(partial.ready) == 2 and len(partial.dtb) == 2:
                    saw_complete_contract = True
                linux_complete = "TGOS_LINUX_TRAJ_DONE ticks=1800" in text
                zephyr_complete = False
                if linux_complete:
                    for raw_line in text.splitlines():
                        marker_index = raw_line.find("[VM 2] ")
                        if marker_index < 0:
                            continue
                        payload = raw_line[marker_index + len("[VM 2] ") :].strip()
                        if not payload.startswith("{"):
                            continue
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if (
                            isinstance(event, dict)
                            and event.get("schema_version") == "p5-ai-event-v1"
                            and event.get("endpoint") == "zephyr"
                            and event.get("request_id") == 1800
                            and event.get("event") == "period_finish"
                        ):
                            zephyr_complete = True
                            break
                if linux_complete and zephyr_complete:
                    cleanup_error = request_bounded_shutdown(
                        launcher,
                        pidfile=Path(identity["qemu_pidfile"]),
                        timeout_seconds=shutdown_timeout_seconds,
                    )
                    if cleanup_error:
                        raise RunnerPlanError(
                            f"completed Guest runtime cleanup failed: {cleanup_error}"
                        )
                    controlled_shutdown = True
                    exit_code = 0
                    break
                if not saw_complete_contract and time.monotonic() >= ready_deadline:
                    raise RunnerPlanError(
                        "Guest runtime did not reach complete dual-Guest READY/DTB contract"
                    )
                exit_code = launcher.poll()
                if exit_code is not None:
                    break
                if time.monotonic() >= runtime_deadline:
                    raise RunnerPlanError("Guest runtime exceeded the 600-second bounded timeout")
                time.sleep(0.1)
            finished_monotonic_ns = time.monotonic_ns()
            if exit_code != 0 and not controlled_shutdown:
                raise RunnerPlanError(f"Guest runtime exited with code {exit_code}")
        if not saw_complete_contract:
            raise RunnerPlanError("Guest runtime exited before complete dual-Guest contract")
        observation = publish_guest_observation_bundle(
            output_dir=output,
            live_log=live_log,
            run_id=run_id,
            session_id=session_id,
        )
        validate_guest_observation_bundle(
            output, run_id=run_id, session_id=session_id
        )
        shutil.rmtree(runtime_dir)
        if runtime_dir.exists():
            raise RunnerPlanError("runner-owned runtime directory remained after success cleanup")
    except (OSError, subprocess.SubprocessError) as error:
        if launcher is not None and launcher.poll() is None:
            cleanup_error = request_bounded_shutdown(
                launcher,
                pidfile=Path(identity["qemu_pidfile"]),
                timeout_seconds=shutdown_timeout_seconds,
            )
            if cleanup_error:
                raise RunnerPlanError(f"Guest runtime failed and cleanup failed: {cleanup_error}") from error
        raise RunnerPlanError(f"Guest runtime execution failed: {error}") from error
    except RunnerPlanError:
        if launcher is not None and launcher.poll() is None:
            request_bounded_shutdown(
                launcher,
                pidfile=Path(identity["qemu_pidfile"]),
                timeout_seconds=shutdown_timeout_seconds,
            )
        raise
    result = {
        "schema_version": "p5-ai-execution-v1",
        "run_id": run_id,
        "session_id": session_id,
        "execution": "completed",
        "qualified": False,
        "exit_code": exit_code,
        "command": {
            "argv": list(argv),
            "cwd": cwd,
            "started_monotonic_ns": started_monotonic_ns,
            "finished_monotonic_ns": finished_monotonic_ns,
            "exit_code": exit_code,
        },
        "runtime_identity": identity,
        "observation": observation,
        "non_claims": [
            "execution does not produce status.json, ICPC records, trajectory, cleanup, or qualification",
            "the caller must run the independent TEST-017 validator before any status-last publication",
        ],
    }
    execution_path = output / "execution.json"
    if execution_path.exists():
        raise RunnerPlanError(f"refusing to overwrite {execution_path}")
    publish_new_file(
        execution_path,
        (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        error_type=RunnerPlanError,
    )
    result["file"] = {
        "path": str(execution_path),
        "size": execution_path.stat().st_size,
        "sha256": _sha256(execution_path),
    }
    validate_guest_execution_manifest(
        output, run_id=run_id, session_id=session_id
    )
    return result


def validate_guest_execution_manifest(
    output_dir: Path, *, run_id: str, session_id: int
) -> dict[str, Any]:
    """Validate the durable executor result before status-last publication."""

    output = _regular_bundle_directory(output_dir, "execution output")
    if (output / "status.json").exists():
        raise RunnerPlanError("execution package already has a terminal status")
    execution_path = _regular_file(output / "execution.json", "execution manifest")
    try:
        value = json.loads(execution_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RunnerPlanError(f"execution manifest cannot be read: {error}") from error
    if not isinstance(value, dict):
        raise RunnerPlanError("execution manifest must be an object")
    if (
        value.get("schema_version") != "p5-ai-execution-v1"
        or value.get("run_id") != run_id
        or value.get("session_id") != session_id
        or value.get("execution") != "completed"
        or value.get("qualified") is not False
        or value.get("exit_code") != 0
    ):
        raise RunnerPlanError("execution manifest identity/completion claim drifted")
    command = value.get("command")
    if (
        not isinstance(command, dict)
        or set(command) != {"argv", "cwd", "started_monotonic_ns", "finished_monotonic_ns", "exit_code"}
        or not isinstance(command.get("argv"), list)
        or not command.get("argv")
        or not isinstance(command.get("cwd"), str)
        or not isinstance(command.get("started_monotonic_ns"), int)
        or not isinstance(command.get("finished_monotonic_ns"), int)
        or not isinstance(command.get("exit_code"), int)
        or command["exit_code"] != 0
    ):
        raise RunnerPlanError("execution command provenance is incomplete or drifted")
    observation = value.get("observation")
    if not isinstance(observation, dict):
        raise RunnerPlanError("execution manifest has no observation claim")
    if (
        observation.get("schema_version") != "p5-ai-observation-v1"
        or observation.get("run_id") != run_id
        or observation.get("session_id") != session_id
        or observation.get("qualified") is not False
    ):
        raise RunnerPlanError("execution observation claim drifted")
    observation_result = validate_guest_observation_bundle(
        output, run_id=run_id, session_id=session_id, allow_execution=True
    )
    return {
        "schema_version": "p5-ai-execution-validation-v1",
        "valid": True,
        "run_id": run_id,
        "session_id": session_id,
        "qualified": False,
        "observation": observation_result,
        "non_claims": [
            "execution validation does not publish status or qualification"
        ],
    }


def build_guest_runtime_plan(
    *,
    repository: Path,
    build_config: Path,
    qemu_config: Path,
    linux_vmconfig: Path,
    zephyr_vmconfig: Path,
    linux_rootfs: Path,
    zephyr_image: Path,
    run_id: str,
    session_id: int,
    output_dir: Path,
    runtime_root: Path,
) -> dict[str, Any]:
    """Bind the shared AxVisor/QEMU lifecycle without starting it.

    The plan is intentionally pure: it creates no runtime directory and does
    not invoke Cargo, WSL, QEMU, or a Guest.  The later runtime executor must
    materialize exactly this resolved config and argv in a fresh input
    directory, separate from the evidence bundle.
    """

    try:
        if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id <= 0:
            raise RunnerPlanError("runtime plan session_id must be a positive integer")
        identity_nonce = hashlib.sha256(
            f"p5-runtime-plan:{run_id}:{session_id}".encode("utf-8")
        ).hexdigest()[:32]
        identity = make_runtime_identity(
            nonce=identity_nonce, runtime_root=Path(runtime_root).absolute()
        )
        output = _safe_output_path(output_dir, "runner output directory")
        if identity.runtime_dir.exists():
            raise RunnerPlanError(
                f"planned runtime directory already exists: {identity.runtime_dir}"
            )
        qemu_source = _regular_file(qemu_config, "QEMU config")
        resolved_qemu = output / "configs" / "qemu.resolved.toml"
        resolved_text = inject_qemu_identity_args(
            qemu_source.read_text(encoding="utf-8"),
            qmp_socket=identity.qmp_socket,
            qemu_pidfile=identity.qemu_pidfile,
            qemu_name=identity.qemu_name,
        )
        guest_configs = _resolve_guest_vm_configs(
            linux_vmconfig=linux_vmconfig,
            zephyr_vmconfig=zephyr_vmconfig,
            linux_rootfs=linux_rootfs,
            zephyr_image=zephyr_image,
            output=output,
        )
        command = build_axvisor_qemu_command(
            build_config=_regular_file(build_config, "AxVisor build config").resolve(),
            qemu_config=resolved_qemu.resolve(),
            linux_vmconfig=Path(guest_configs["linux"]["resolved"]["path"]),
            zephyr_vmconfig=Path(guest_configs["zephyr"]["resolved"]["path"]),
        )
    except (OSError, ValueError) as error:
        raise RunnerPlanError(f"Guest runtime plan failed: {error}") from error
    return {
        "schema_version": "p5-ai-runtime-plan-v1",
        "run_id": run_id,
        "session_id": session_id,
        "execution_kind": "guest_runtime",
        "execution": "not_started",
        "qualified": False,
        "runtime_identity": {
            "nonce": identity.nonce,
            "qemu_name": identity.qemu_name,
            "runtime_dir": str(identity.runtime_dir),
            "qmp_socket": str(identity.qmp_socket),
            "qemu_pidfile": str(identity.qemu_pidfile),
            "live_log": str(identity.live_log),
        },
        "resolved_qemu": {
            "path": str(resolved_qemu),
            "sha256": hashlib.sha256(resolved_text.encode("utf-8")).hexdigest(),
            "source": _record_file(qemu_source, "QEMU config"),
        },
        "guest_configs": guest_configs,
        "command": {
            "cwd": str(Path(repository).resolve()),
            "argv": command,
        },
        "non_claims": [
            "no runtime directory was created",
            "Cargo, WSL, QEMU, Linux Guest, Zephyr Guest, and Guest-IP were not executed",
        ],
    }


def validate_guest_runtime_plan(plan: dict[str, Any]) -> None:
    """Fail closed if a generated runtime plan is not execution-safe."""

    if plan.get("schema_version") != "p5-ai-runtime-plan-v1":
        raise RunnerPlanError("runtime plan schema is not p5-ai-runtime-plan-v1")
    if plan.get("execution") != "not_started" or plan.get("qualified") is not False:
        raise RunnerPlanError("runtime plan must remain not_started and unqualified")
    session_id = plan.get("session_id")
    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id <= 0:
        raise RunnerPlanError("runtime plan session_id must be a positive integer")
    command = plan.get("command")
    if not isinstance(command, dict) or command.get("cwd") in (None, ""):
        raise RunnerPlanError("runtime plan command cwd is missing")
    argv = command.get("argv")
    if not isinstance(argv, list) or argv[:4] != ["cargo", "xtask", "axvisor", "qemu"]:
        raise RunnerPlanError("runtime plan does not use the fixed AxVisor qemu argv")
    guest_configs = plan.get("guest_configs")
    if not isinstance(guest_configs, dict):
        raise RunnerPlanError("runtime plan guest_configs is missing")
    for guest in ("linux", "zephyr"):
        details = guest_configs.get(guest)
        if not isinstance(details, dict):
            raise RunnerPlanError(f"runtime plan {guest} config is missing")
        resolved = details.get("resolved")
        if (
            not isinstance(resolved, dict)
            or not resolved.get("path")
            or not resolved.get("sha256")
        ):
            raise RunnerPlanError(f"runtime plan {guest} resolved config claim is incomplete")
    runtime_identity = plan.get("runtime_identity")
    if not isinstance(runtime_identity, dict) or not runtime_identity.get("qmp_socket"):
        raise RunnerPlanError("runtime plan QMP identity is missing")
    resolved_qemu = plan.get("resolved_qemu")
    if (
        not isinstance(resolved_qemu, dict)
        or not resolved_qemu.get("path")
        or not resolved_qemu.get("sha256")
        or not isinstance(resolved_qemu.get("source"), dict)
    ):
        raise RunnerPlanError("runtime plan resolved QEMU config claim is incomplete")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _record_file(path: Path, label: str) -> dict[str, Any]:
    path = _regular_file(path, label)
    return {"path": str(path), "size": path.stat().st_size, "sha256": _sha256(path)}


def _record_json(path: Path, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    json_path, value = _json_file(path, label)
    return value, {
        "path": str(json_path),
        "size": json_path.stat().st_size,
        "sha256": _sha256(json_path),
        "schema_version": value.get("schema_version"),
    }


def build_runtime_preflight(
    *,
    repository: Path,
    from_network_session: Path,
    build_config: Path,
    qemu_config: Path,
    linux_vmconfig: Path,
    zephyr_vmconfig: Path,
    linux_rootfs: Path,
    linux_rootfs_manifest: Path,
    linux_controller_config: Path,
    zephyr_image: Path,
    zephyr_elf: Path,
    zephyr_build_manifest: Path,
    scenario: str,
    profile: Path,
    fault_profile: Path | None = None,
    fault_case: str | None = None,
    controller: str,
    seed: int,
    timeout_seconds: int,
    run_id: str,
    session_id: int,
    output_dir: Path,
) -> dict[str, Any]:
    """Validate the exact P5 runner boundary without starting a Guest."""

    if scenario not in ("test-017", "test-018"):
        raise RunnerPlanError("scenario must be test-017 or test-018")
    if timeout_seconds != 600:
        raise RunnerPlanError("P5 runner timeout must be exactly 600 seconds")
    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id <= 0:
        raise RunnerPlanError("TEST-017 session_id must be a positive integer")
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        raise RunnerPlanError("run-id must be a non-empty path-safe identifier")
    try:
        profile_data = json.loads(Path(profile).read_text(encoding="utf-8"))
        from .host_orchestration import validate_profile_contract
    except ImportError:  # pragma: no cover - direct script execution
        from host_orchestration import validate_profile_contract  # type: ignore[no-redef]
    except (OSError, json.JSONDecodeError) as error:
        raise RunnerPlanError(f"profile cannot be read: {error}") from error
    if not isinstance(profile_data, dict):
        raise RunnerPlanError("profile must be a JSON object")
    try:
        validate_profile_contract(profile_data)
    except (KeyError, TypeError, ValueError) as error:
        raise RunnerPlanError(f"profile contract failed: {error}") from error
    if controller not in ("fixed", "mlp") or seed not in (7, 19, 43):
        raise RunnerPlanError("controller/seed is outside the frozen P5 identity")
    if scenario == "test-017" and (fault_profile is not None or fault_case is not None):
        raise RunnerPlanError("TEST-017 cannot carry a TEST-018 fault profile or case")
    fault_case_binding: dict[str, Any] | None = None
    fault_profile_claim: dict[str, Any] | None = None
    if scenario == "test-018":
        if fault_profile is None or fault_case is None:
            raise RunnerPlanError("TEST-018 requires --fault-profile and --fault-case")
        try:
            fault_document = load_fault_profile(Path(fault_profile))
            selected_case = select_fault_case(fault_document, fault_case)
        except FaultProfileError as error:
            raise RunnerPlanError(f"TEST-018 fault binding failed: {error}") from error
        fault_profile_claim = _record_file(Path(fault_profile), "TEST-018 fault profile")
        case_bytes = json.dumps(
            selected_case, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        fault_case_binding = {
            "case": selected_case["case"],
            "definition": selected_case,
            "sha256": hashlib.sha256(case_bytes).hexdigest(),
            "binding": "immutable_profile_case_copy",
        }

    repository = _regular_directory(repository, "repository")
    session = _regular_directory(from_network_session, "from-network-session")
    session_manifest = _record_json(session / "manifest.json", "from-network-session manifest")[1]
    profile_claim = _record_file(profile, "qualification profile")
    rootfs_manifest_data, rootfs_manifest_claim = _record_json(
        linux_rootfs_manifest, "Linux rootfs manifest"
    )
    controller_config_data, controller_config_claim = _record_json(
        linux_controller_config, "Linux controller config"
    )
    for label, value in (
        ("Linux rootfs manifest", rootfs_manifest_data),
        ("Linux controller config", controller_config_data),
    ):
        if value.get("run_id") != run_id:
            raise RunnerPlanError(f"{label} run_id does not match runner")
        if value.get("session_id") != session_id:
            raise RunnerPlanError(f"{label} session_id does not match runner")
        if value.get("controller_mode") not in (None, controller):
            raise RunnerPlanError(f"{label} controller mode does not match runner")
        if value.get("seed") not in (None, seed):
            raise RunnerPlanError(f"{label} seed does not match runner")
    expected_model_version = 0 if controller == "fixed" else profile_data["model"]["model_version"]
    if controller_config_data.get("model_version") not in (None, expected_model_version):
        raise RunnerPlanError("Linux controller config model_version does not match runner")

    zephyr_manifest_data, zephyr_manifest_claim = _record_json(
        zephyr_build_manifest, "Zephyr build manifest"
    )
    if zephyr_manifest_data.get("run_id") not in (None, run_id):
        raise RunnerPlanError("Zephyr build manifest run_id does not match runner")

    inputs = {
        "repository": str(repository),
        "from_network_session": {
            "path": str(session),
            "manifest": session_manifest,
        },
        "build_config": _record_file(build_config, "AxVisor build config"),
        "qemu_config": _record_file(qemu_config, "QEMU config"),
        "linux_vmconfig": _record_file(linux_vmconfig, "Linux VM config"),
        "zephyr_vmconfig": _record_file(zephyr_vmconfig, "Zephyr VM config"),
        "linux_rootfs": _record_file(linux_rootfs, "Linux rootfs"),
        "linux_rootfs_manifest": rootfs_manifest_claim,
        "linux_controller_config": controller_config_claim,
        "zephyr_image": _record_file(zephyr_image, "Zephyr image"),
        "zephyr_elf": _record_file(zephyr_elf, "Zephyr ELF"),
        "zephyr_build_manifest": zephyr_manifest_claim,
        "profile": profile_claim,
    }
    if fault_profile_claim is not None and fault_case_binding is not None:
        inputs["fault_profile"] = fault_profile_claim
        inputs["fault_case"] = fault_case_binding
    output = _safe_output_path(output_dir, "runner output directory")
    # Resolved VM/QEMU inputs are disposable launch material, not evidence
    # artifacts.  Keeping them in a sibling directory lets the executor
    # enforce a fresh output bundle and retain the materialized inputs on a
    # failed attempt for inspection.
    runtime_input_dir = output.parent / f".{run_id}.inputs"
    runtime_plan = build_guest_runtime_plan(
        repository=repository,
        build_config=build_config,
        qemu_config=qemu_config,
        linux_vmconfig=linux_vmconfig,
        zephyr_vmconfig=zephyr_vmconfig,
        linux_rootfs=linux_rootfs,
        zephyr_image=zephyr_image,
        run_id=run_id,
        session_id=session_id,
        output_dir=runtime_input_dir,
        runtime_root=output.parent / f".{run_id}.runtime",
    )
    validate_guest_runtime_plan(runtime_plan)
    return {
        "schema_version": "p5-ai-runner-preflight-v1",
        "run_id": run_id,
        "session_id": session_id,
        "scenario": scenario,
        "controller": controller,
        "seed": seed,
        "timeout_seconds": timeout_seconds,
        "execution_kind": "host_contract",
        "execution": "not_started",
        "qualified": False,
        "inputs": inputs,
        "output_dir": str(output),
        "runtime": runtime_plan,
        "non_claims": [
            "QEMU and Guest execution were not started",
            "no Guest-IP, AI-loop, L8, or ai_control_completed evidence was produced",
        ],
    }


def execute_guest_runtime_from_preflight(
    preflight: dict[str, Any],
    *,
    output_dir: Path,
    popen_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Materialize and execute one preflight-bound TEST-017 Guest run.

    The preflight's sibling ``runtime_input_dir`` owns only resolved launch
    files.  The final ``output_dir`` starts fresh and receives the immutable
    raw observation/execution manifests.  On executor failure, both the
    runtime directory and input directory remain available to the caller for
    failed-attempt preservation; after a successful unqualified observation,
    only the disposable input directory is removed.
    """

    if preflight.get("scenario") != "test-017":
        raise RunnerPlanError("Guest runtime execution is currently limited to TEST-017")
    if preflight.get("execution") != "not_started" or preflight.get("qualified") is not False:
        raise RunnerPlanError("Guest runtime preflight must remain not_started and unqualified")
    plan = preflight.get("runtime")
    inputs = preflight.get("inputs")
    if not isinstance(plan, dict) or not isinstance(inputs, dict):
        raise RunnerPlanError("Guest runtime preflight is missing plan or input bindings")
    validate_guest_runtime_plan(plan)
    run_id = preflight.get("run_id")
    session_id = preflight.get("session_id")
    if not isinstance(run_id, str) or not isinstance(session_id, int) or isinstance(session_id, bool):
        raise RunnerPlanError("Guest runtime preflight identity is malformed")
    identity = plan.get("runtime_identity")
    if not isinstance(identity, dict) or any(
        not isinstance(identity.get(field), str) or not identity.get(field)
        for field in ("qmp_socket", "qemu_pidfile", "qemu_name")
    ):
        raise RunnerPlanError("Guest runtime preflight identity is missing")
    resolved_qemu = plan.get("resolved_qemu")
    if not isinstance(resolved_qemu, dict) or not isinstance(resolved_qemu.get("path"), str):
        raise RunnerPlanError("Guest runtime preflight resolved QEMU claim is missing")

    def input_path(name: str) -> Path:
        claim = inputs.get(name)
        if not isinstance(claim, dict) or not isinstance(claim.get("path"), str):
            raise RunnerPlanError(f"Guest runtime preflight input is missing: {name}")
        return Path(claim["path"])

    input_dir = Path(resolved_qemu["path"]).absolute().parent.parent
    materialized = materialize_guest_runtime_inputs(
        qemu_config=input_path("qemu_config"),
        linux_vmconfig=input_path("linux_vmconfig"),
        zephyr_vmconfig=input_path("zephyr_vmconfig"),
        linux_rootfs=input_path("linux_rootfs"),
        zephyr_image=input_path("zephyr_image"),
        output_dir=input_dir,
        qmp_socket=Path(identity["qmp_socket"]),
        qemu_pidfile=Path(identity["qemu_pidfile"]),
        qemu_name=str(identity["qemu_name"]),
    )
    validate_materialized_guest_runtime_inputs(plan, materialized)
    result = execute_guest_runtime(
        plan,
        materialized,
        output_dir=output_dir,
        run_id=run_id,
        session_id=session_id,
        popen_factory=popen_factory,
    )
    try:
        shutil.rmtree(input_dir)
    except OSError as error:
        raise RunnerPlanError(f"successful Guest run left runtime inputs behind: {error}") from error
    if input_dir.exists():
        raise RunnerPlanError("successful Guest run left runtime input directory behind")
    result["runtime_inputs"] = {
        "path": str(input_dir),
        "removed": True,
    }
    return result


def write_test018_host_preflight_bundle(
    preflight: dict[str, Any],
    *,
    output_dir: Path,
    command: tuple[str, ...],
    cwd: Path,
) -> dict[str, Any]:
    """Publish only the immutable TEST-018 case-binding host plan.

    This status-last package is deliberately not a fault run: it contains no
    Guest events, action, recovery, PCAP, or qualification result.  The
    non-dry-run path remains blocked until a real injector/executor exists.
    """

    if preflight.get("scenario") != "test-018" or preflight.get("qualified") is not False:
        raise RunnerPlanError("TEST-018 host package requires an unqualified test-018 preflight")
    inputs = preflight.get("inputs")
    if not isinstance(inputs, dict):
        raise RunnerPlanError("TEST-018 preflight has no input binding")
    profile_claim = inputs.get("profile")
    fault_profile_claim = inputs.get("fault_profile")
    fault_case_binding = inputs.get("fault_case")
    if not all(isinstance(item, dict) for item in (profile_claim, fault_profile_claim, fault_case_binding)):
        raise RunnerPlanError("TEST-018 host package input binding is incomplete")
    case_definition = fault_case_binding.get("definition")
    case_id = fault_case_binding.get("case")
    if not isinstance(case_definition, dict) or case_definition.get("case") != case_id:
        raise RunnerPlanError("TEST-018 host package case binding is inconsistent")
    case_bytes = json.dumps(
        case_definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if (
        fault_case_binding.get("binding") != "immutable_profile_case_copy"
        or fault_case_binding.get("sha256") != hashlib.sha256(case_bytes).hexdigest()
    ):
        raise RunnerPlanError("TEST-018 host package case hash binding is invalid")
    try:
        qualification_bytes = Path(profile_claim["path"]).read_bytes()
        fault_profile_bytes = Path(fault_profile_claim["path"]).read_bytes()
    except (KeyError, OSError, TypeError) as error:
        raise RunnerPlanError(f"TEST-018 host package profile input cannot be read: {error}") from error

    def payload(value: Any) -> bytes:
        return (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    payloads: dict[str, bytes] = {
        "commands.jsonl": payload(
            {
                "argv": [str(item) for item in command],
                "cwd": str(Path(cwd).resolve()),
                "started_monotonic_ns": 0,
                "finished_monotonic_ns": 0,
                "exit_code": 0,
            }
        ),
        "cleanup.json": payload(
            {
                "residualProcesses": [],
                "residualSockets": [],
                "residualFiles": [],
                "runtimeDirRemoved": True,
            }
        ),
        "configs/qualification-v1.json": qualification_bytes,
        "configs/faults-v1.json": fault_profile_bytes,
        "configs/fault-case.json": payload(case_definition),
        "configs/runner-preflight.json": payload(preflight),
        "metrics/summary.json": payload(
            {
                "schema_version": "p5-ai-test018-host-summary-v1",
                "run_id": preflight.get("run_id"),
                "session_id": preflight.get("session_id"),
                "scenario": "test-018",
                "controller": preflight.get("controller"),
                "seed": preflight.get("seed"),
                "fault_case": case_id,
                "execution_kind": "host_contract",
                "evidence_level": "L2 host contract",
                "runtime_evidence": "not_produced",
                "qualified": False,
                "non_claims": [
                    "no Guest fault injection or recovery was executed",
                    "no raw event, action, trajectory, PCAP, or qualification evidence was produced",
                ],
            }
        ),
    }
    files = {
        name: {
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "producer": "scripts/contest/ai/run_closed_loop.py",
        }
        for name, content in sorted(payloads.items())
    }
    manifest = payload(
        {
            "schema_version": _P5_MANIFEST_SCHEMA,
            "run_id": preflight.get("run_id"),
            "scenario": "test-018",
            "bundle_kind": "test018_host_preflight",
            "files": files,
            "qualified": False,
        }
    )
    manifest_sha256 = hashlib.sha256(manifest).hexdigest()
    session = payload(
        {
            "schema_version": _P5_SESSION_SCHEMA,
            "run_id": preflight.get("run_id"),
            "session_id": preflight.get("session_id"),
            "scenario": "test-018",
            "controller": preflight.get("controller"),
            "seed": preflight.get("seed"),
            "profile_id": "qualification-v1",
            "fault_profile_id": "faults-v1",
            "fault_case": case_id,
            "execution_kind": "host_contract",
            "evidence_level": "L2 host contract",
            "qualified": False,
            "manifest_sha256": manifest_sha256,
        }
    )
    status = payload(
        {
            "schema_version": _P5_STATUS_SCHEMA,
            "success": True,
            "status": "host_contract_valid",
            "primaryError": None,
            "cleanupError": None,
            "completedChecks": ["fault_profile", "fault_case_binding", "runtime_not_started", "cleanup"],
            "manifestSha256": manifest_sha256,
            "qualified": False,
            "statusLast": True,
        }
    )
    ordered = {**payloads, "manifest.json": manifest, "session.json": session, "status.json": status}
    HostBundle(files=ordered, manifest_sha256=manifest_sha256).write(output_dir)
    status_path = Path(output_dir).resolve() / "status.json"
    return {
        "schema_version": "p5-ai-test018-host-publication-v1",
        "run_id": preflight.get("run_id"),
        "session_id": preflight.get("session_id"),
        "fault_case": case_id,
        "status": "host_contract_valid",
        "qualified": False,
        "file": {"path": str(status_path), "size": status_path.stat().st_size, "sha256": _sha256(status_path)},
        "non_claims": ["this host preflight does not produce TEST-018 runtime qualification"],
    }


def validate_test018_host_preflight_bundle(
    output_dir: Path,
    *,
    run_id: str,
    session_id: int,
    fault_case: str,
) -> dict[str, Any]:
    """Consume a TEST-018 host preflight package without upgrading evidence."""

    output = _regular_directory(Path(output_dir), "TEST-018 host preflight output")
    expected_payloads = {
        "commands.jsonl",
        "cleanup.json",
        "configs/qualification-v1.json",
        "configs/faults-v1.json",
        "configs/fault-case.json",
        "configs/runner-preflight.json",
        "metrics/summary.json",
    }
    expected_files = expected_payloads | {"manifest.json", "session.json", "status.json"}
    actual_files = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    }
    if actual_files != expected_files:
        raise RunnerPlanError(
            f"TEST-018 host preflight file set drifted: expected {sorted(expected_files)}, got {sorted(actual_files)}"
        )

    def read_json(relative: str) -> dict[str, Any]:
        path = output / relative
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RunnerPlanError(f"TEST-018 host preflight JSON invalid: {relative}: {error}") from error
        if not isinstance(value, dict):
            raise RunnerPlanError(f"TEST-018 host preflight JSON must be an object: {relative}")
        return value

    manifest = read_json("manifest.json")
    if (
        manifest.get("schema_version") != _P5_MANIFEST_SCHEMA
        or manifest.get("run_id") != run_id
        or manifest.get("scenario") != "test-018"
        or manifest.get("bundle_kind") != "test018_host_preflight"
        or manifest.get("qualified") is not False
    ):
        raise RunnerPlanError("TEST-018 host preflight manifest identity drifted")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, dict) or set(manifest_files) != expected_payloads:
        raise RunnerPlanError("TEST-018 host preflight manifest file set drifted")
    for relative, claim in manifest_files.items():
        if not isinstance(claim, dict):
            raise RunnerPlanError(f"TEST-018 host preflight file claim is invalid: {relative}")
        path = output / relative
        if claim.get("size") != path.stat().st_size or claim.get("sha256") != _sha256(path):
            raise RunnerPlanError(f"TEST-018 host preflight file hash drifted: {relative}")
        if claim.get("producer") != "scripts/contest/ai/run_closed_loop.py":
            raise RunnerPlanError(f"TEST-018 host preflight producer drifted: {relative}")
    manifest_sha256 = _sha256(output / "manifest.json")

    session = read_json("session.json")
    if (
        session.get("schema_version") != _P5_SESSION_SCHEMA
        or session.get("run_id") != run_id
        or session.get("session_id") != session_id
        or session.get("scenario") != "test-018"
        or session.get("execution_kind") != "host_contract"
        or session.get("evidence_level") != "L2 host contract"
        or session.get("fault_case") != fault_case
        or session.get("qualified") is not False
        or session.get("manifest_sha256") != manifest_sha256
    ):
        raise RunnerPlanError("TEST-018 host preflight session identity drifted")

    status = read_json("status.json")
    if (
        status.get("schema_version") != _P5_STATUS_SCHEMA
        or status.get("status") != "host_contract_valid"
        or status.get("success") is not True
        or status.get("qualified") is not False
        or status.get("statusLast") is not True
        or status.get("manifestSha256") != manifest_sha256
        or status.get("completedChecks")
        != ["fault_profile", "fault_case_binding", "runtime_not_started", "cleanup"]
    ):
        raise RunnerPlanError("TEST-018 host preflight status drifted")

    preflight = read_json("configs/runner-preflight.json")
    if preflight.get("run_id") != run_id or preflight.get("session_id") != session_id:
        raise RunnerPlanError("TEST-018 host preflight runner identity drifted")
    if preflight.get("scenario") != "test-018" or preflight.get("qualified") is not False:
        raise RunnerPlanError("TEST-018 host preflight runner claim drifted")
    inputs = preflight.get("inputs")
    case_binding = inputs.get("fault_case") if isinstance(inputs, dict) else None
    profile_claim = inputs.get("profile") if isinstance(inputs, dict) else None
    fault_profile_claim = inputs.get("fault_profile") if isinstance(inputs, dict) else None
    if (
        not isinstance(case_binding, dict)
        or case_binding.get("case") != fault_case
        or not isinstance(profile_claim, dict)
        or not isinstance(fault_profile_claim, dict)
    ):
        raise RunnerPlanError("TEST-018 host preflight case binding is missing")

    def verify_input_claim(relative: str, claim: dict[str, Any], label: str) -> None:
        path = output / relative
        if (
            claim.get("size") != path.stat().st_size
            or claim.get("sha256") != _sha256(path)
        ):
            raise RunnerPlanError(f"TEST-018 {label} input claim drifted")

    verify_input_claim("configs/qualification-v1.json", profile_claim, "qualification profile")
    verify_input_claim("configs/faults-v1.json", fault_profile_claim, "fault profile")
    try:
        case_definition = json.loads(
            (output / "configs" / "fault-case.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise RunnerPlanError(f"TEST-018 case copy cannot be read: {error}") from error
    try:
        fault_document = load_fault_profile(output / "configs" / "faults-v1.json")
        selected_case = select_fault_case(fault_document, fault_case)
    except FaultProfileError as error:
        raise RunnerPlanError(f"TEST-018 packaged fault profile failed validation: {error}") from error
    if selected_case != case_definition:
        raise RunnerPlanError("TEST-018 case copy differs from packaged fault profile")
    case_bytes = json.dumps(
        case_definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    case_sha256 = hashlib.sha256(case_bytes).hexdigest()
    if case_binding.get("sha256") != case_sha256:
        raise RunnerPlanError("TEST-018 case binding hash drifted")
    if case_binding.get("definition") != case_definition:
        raise RunnerPlanError("TEST-018 case copy differs from preflight binding")

    summary = read_json("metrics/summary.json")
    if (
        summary.get("schema_version") != "p5-ai-test018-host-summary-v1"
        or summary.get("run_id") != run_id
        or summary.get("fault_case") != fault_case
        or summary.get("runtime_evidence") != "not_produced"
        or summary.get("qualified") is not False
    ):
        raise RunnerPlanError("TEST-018 host preflight summary drifted")
    return {
        "schema_version": "p5-ai-test018-host-validation-v1",
        "run_id": run_id,
        "session_id": session_id,
        "fault_case": fault_case,
        "evidence_level": "L2 host contract",
        "qualified": False,
        "manifest_sha256": manifest_sha256,
        "non_claims": [
            "host preflight validation does not prove Guest fault injection, recovery, or qualification"
        ],
    }


def main(argv: list[str] | None = None) -> int:
    repository = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", type=int, default=1)
    parser.add_argument("--controller", choices=("fixed", "mlp"), required=True)
    parser.add_argument("--seed", choices=(7, 19, 43), type=int, required=True)
    parser.add_argument(
        "--profile",
        type=Path,
        default=repository / "configs" / "contest" / "ai" / "qualification-v1.json",
    )
    parser.add_argument("--repository", type=Path, default=repository)
    parser.add_argument("--from-network-session", type=Path)
    parser.add_argument("--build-config", type=Path)
    parser.add_argument("--qemu-config", type=Path)
    parser.add_argument("--linux-vmconfig", type=Path)
    parser.add_argument("--zephyr-vmconfig", type=Path)
    parser.add_argument("--linux-rootfs", type=Path)
    parser.add_argument("--linux-rootfs-manifest", type=Path)
    parser.add_argument("--linux-controller-config", type=Path)
    parser.add_argument("--zephyr-image", type=Path)
    parser.add_argument("--zephyr-elf", type=Path)
    parser.add_argument("--zephyr-build-manifest", type=Path)
    parser.add_argument("--scenario", default="test-017")
    parser.add_argument("--fault-profile", type=Path)
    parser.add_argument("--fault-case")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument(
        "--model",
        type=Path,
        default=repository / "apps" / "contest" / "linux-ai-controller" / "model" / "model.bin",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        extended = any(
            value is not None
            for value in (
                args.from_network_session,
                args.build_config,
                args.qemu_config,
                args.linux_vmconfig,
                args.zephyr_vmconfig,
                args.linux_rootfs,
                args.linux_rootfs_manifest,
                args.linux_controller_config,
                args.zephyr_image,
                args.zephyr_elf,
                args.zephyr_build_manifest,
                args.fault_profile,
                args.fault_case,
            )
        )
        if extended:
            planned = (
                args.from_network_session,
                args.build_config,
                args.qemu_config,
                args.linux_vmconfig,
                args.zephyr_vmconfig,
                args.linux_rootfs,
                args.linux_rootfs_manifest,
                args.linux_controller_config,
                args.zephyr_image,
                args.zephyr_elf,
                args.zephyr_build_manifest,
            )
            if any(value is None for value in planned):
                raise RunnerPlanError("documented Guest runner inputs must be supplied together")
            preflight = build_runtime_preflight(
                repository=args.repository,
                from_network_session=args.from_network_session,
                build_config=args.build_config,
                qemu_config=args.qemu_config,
                linux_vmconfig=args.linux_vmconfig,
                zephyr_vmconfig=args.zephyr_vmconfig,
                linux_rootfs=args.linux_rootfs,
                linux_rootfs_manifest=args.linux_rootfs_manifest,
                linux_controller_config=args.linux_controller_config,
                zephyr_image=args.zephyr_image,
                zephyr_elf=args.zephyr_elf,
                zephyr_build_manifest=args.zephyr_build_manifest,
                scenario=args.scenario,
                profile=args.profile,
                fault_profile=args.fault_profile,
                fault_case=args.fault_case,
                controller=args.controller,
                seed=args.seed,
                timeout_seconds=args.timeout_seconds,
                run_id=args.run_id,
                session_id=args.session_id,
                output_dir=args.output_dir,
            )
            if args.scenario == "test-018":
                if not args.dry_run:
                    raise RunnerPlanError(
                        "TEST-018 Guest fault injection and recovery are not implemented; pass --dry-run for host preflight"
                    )
                publication = write_test018_host_preflight_bundle(
                    preflight,
                    output_dir=args.output_dir,
                    command=tuple(str(item) for item in sys.argv),
                    cwd=args.repository,
                )
                validate_test018_host_preflight_bundle(
                    args.output_dir,
                    run_id=args.run_id,
                    session_id=args.session_id,
                    fault_case=publication["fault_case"],
                )
                print(
                    f"P5_TEST018_HOST_PREFLIGHT_PASS case={publication['fault_case']} qualified=false"
                )
                return 0
            if not args.dry_run:
                observation_dir = (
                    args.output_dir.parent / f".{args.run_id}.observation"
                )
                result = execute_guest_runtime_from_preflight(
                    preflight,
                    output_dir=observation_dir,
                )
                try:
                    from .finalize_guest_run import finalize_guest_run
                except ImportError:  # pragma: no cover - direct script execution
                    from finalize_guest_run import finalize_guest_run  # type: ignore[no-redef]
                final = finalize_guest_run(
                    preflight,
                    observation_dir=observation_dir,
                    output_dir=args.output_dir,
                )
                print(
                    f"P5_GUEST_QUALIFICATION_PASS controller={args.controller} "
                    f"seed={args.seed} qualified=true "
                    f"session={result['session_id']} status={final['status']}"
                )
                return 0
        else:
            preflight = None
            if not args.dry_run:
                raise HostContractError(
                    "Guest closed-loop execution is disabled; pass --dry-run for host virtual time"
                )
        identity = HostIdentity(
            run_id=args.run_id,
            session_id=args.session_id,
            controller=args.controller,
            seed=args.seed,
        )
        config = HostContractConfig.from_paths(
            identity=identity,
            profile_path=args.profile,
            model_path=args.model,
        )
        result = run_host_contract(config)
        extra_files = None
        if preflight is not None:
            extra_files = {
                "configs/runner-preflight.json": (
                    json.dumps(preflight, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                ).encode("utf-8")
            }
        result.to_bundle(
            command=(sys.executable, *sys.argv),
            cwd=args.repository,
            extra_files=extra_files,
        ).write(args.output_dir)
    except (HostContractError, RunnerPlanError, RootfsPlanError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"P5_CLOSED_LOOP_BLOCKED: {error}")
        return 1
    print(
        f"P5_CLOSED_LOOP_HOST_PASS controller={args.controller} "
        f"seed={args.seed} ticks=1800 qualified=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
