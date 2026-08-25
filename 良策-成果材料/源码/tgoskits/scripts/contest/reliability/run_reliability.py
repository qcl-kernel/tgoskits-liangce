#!/usr/bin/env python3
"""Plan or execute one fail-closed P4 reliability workload.

The dry-run path freezes the P4-REL-01 workload and fault identity without
launching a runtime.  Supplying the complete byte-bound runtime inputs without
``--dry-run`` invokes the shared dual-Guest executor and publishes an
unqualified status-last runtime bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping


NETWORK_CONTEST_DIR = Path(__file__).resolve().parents[1] / "network"
if str(NETWORK_CONTEST_DIR) not in sys.path:
    sys.path.insert(0, str(NETWORK_CONTEST_DIR))

from fault_profile import (  # noqa: E402
    FaultProfileError,
    generate_fault_plan,
    validate_fault_manifest,
)
from input_binding import (  # noqa: E402
    SoakBindingError,
    require_matching_binding,
    validate_current_qemu_config,
    validate_soak_binding,
)

CONTEST_DIR = Path(__file__).resolve().parents[1]
if str(CONTEST_DIR) not in sys.path:
    sys.path.insert(0, str(CONTEST_DIR))
from host_vm_carveout_io import publish_new_file  # noqa: E402


SCHEMA = "p4-reliability-plan-v1"
MANIFEST_SCHEMA = "p4-reliability-manifest-v1"
STATUS_SCHEMA = "p4-reliability-status-v1"
SUMMARY_SCHEMA = "p4-reliability-summary-v1"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SEEDS = (7, 19, 43)
SCENARIOS = {"test-012", "test-013", "test-015"}
MODE_SCENARIO = {
    "core": "test-012",
    "fault": "test-012",
    "restart": "test-013",
    "exactly-once": "test-015",
}
MODES = (*MODE_SCENARIO, "full")
RELIABILITY_SCENARIO_MODES = {
    "core": ("test-012", "udp-reliability", "udp-reliability"),
    "fault": ("test-012", "udp-reliability", "udp-reliability"),
    "restart": ("test-013", "tcp-reliability", "tcp-reliability"),
    "exactly-once": ("test-015", "icpc-reliability", "icpc-reliability"),
}
UDP_CONTRACT = {
    "port": 46000,
    "payload_bytes": 256,
    "packets_per_second": 100,
    "duration_seconds": 100,
}
TCP_CONTRACT = {
    "port": 46001,
    "framing": "u16_be_frame_length_plus_icpc_packet",
    "frame_length_bytes": [36, 1060],
    "partial_read_bytes": 1,
    "merge_every_frames": 31,
    "disconnects": 1,
    "endpoint_restarts": 1,
    "half_frame_eof": True,
    "connect_deadline_ms": 500,
    "retry_wait_ms": 500,
    "max_connect_attempts": 3,
    "new_session_on_transport_switch": True,
    "auto_return_to_udp": False,
}
CONTROL_CONTRACT = {
    "udp_port": 46000,
    "duration_seconds": 100,
    "rate_hz": 10,
    "messages": 1000,
    "attempt_schedule_ms": [0, 100, 300],
    "cancel_at_ms": 500,
    "forbidden_retry_at_ms": 700,
    "wire_sizes_bytes": {"control": [24], "status": [32], "error": [12]},
}


class ReliabilityContractError(ValueError):
    """Raised when a P4 reliability plan is not frozen or safe."""


def _regular_file(path: Path, label: str) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ReliabilityContractError(f"{label} must be a regular non-link file: {path}")
    return path.resolve()


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_regular_file(path, label).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReliabilityContractError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReliabilityContractError(f"{label} must be a JSON object")
    return value


def _file_claim(path: Path, label: str) -> dict[str, Any]:
    path = _regular_file(path, label)
    return {"path": str(path), "size": path.stat().st_size, "sha256": _sha256(path)}


def _verify_manifest_claim(
    document: Mapping[str, Any], key: str, actual: Path, label: str
) -> None:
    claim = document.get(key)
    if not isinstance(claim, Mapping):
        raise ReliabilityContractError(f"{label} has no {key} claim")
    if (
        not isinstance(claim.get("path"), str)
        or Path(claim["path"]).name != actual.name
        or claim.get("size") != actual.stat().st_size
        or claim.get("sha256") != _sha256(actual)
    ):
        raise ReliabilityContractError(f"{label} {key} is not byte-bound")


def build_runtime_preflight(
    *,
    from_soak_session: Path,
    build_config: Path,
    qemu_config: Path,
    linux_vmconfig: Path,
    zephyr_vmconfig: Path,
    linux_rootfs: Path,
    linux_manifest: Path,
    zephyr_image: Path,
    zephyr_build_manifest: Path,
    mode: str,
    run_id: str,
    seed: int,
    output_dir: Path,
) -> dict[str, Any]:
    """Bind the future P4-REL runner inputs without starting runtime."""

    if mode not in RELIABILITY_SCENARIO_MODES:
        raise ReliabilityContractError(
            "runtime preflight mode must be one concrete reliability subplan"
        )
    run_id = _safe_id(run_id, "run_id")
    if seed not in SEEDS:
        raise ReliabilityContractError(f"seed must be one of {SEEDS}")
    scenario, expected_linux_mode, expected_zephyr_mode = RELIABILITY_SCENARIO_MODES[mode]
    try:
        source_binding = validate_soak_binding(Path(from_soak_session))
        qemu_claim = validate_current_qemu_config(Path(qemu_config))
    except (OSError, SoakBindingError) as error:
        raise ReliabilityContractError(f"P2/P4 input binding failed: {error}") from error
    qemu_claim = {
        **qemu_claim,
        "path": str(_regular_file(qemu_config, "QEMU config")),
    }
    linux_manifest_data = _json_object(linux_manifest, "Linux rootfs manifest")
    zephyr_manifest_data = _json_object(zephyr_build_manifest, "Zephyr build manifest")
    try:
        require_matching_binding(
            linux_manifest_data, source_binding, label="Linux rootfs manifest"
        )
        require_matching_binding(
            zephyr_manifest_data, source_binding, label="Zephyr build manifest"
        )
    except SoakBindingError as error:
        raise ReliabilityContractError(str(error)) from error
    for label, document in (
        ("Linux rootfs manifest", linux_manifest_data),
        ("Zephyr build manifest", zephyr_manifest_data),
    ):
        if document.get("bootId") != run_id:
            raise ReliabilityContractError(f"{label} bootId does not match run_id")
    if linux_manifest_data.get("mode") != expected_linux_mode:
        raise ReliabilityContractError("Linux preparation mode does not match reliability subplan")
    if zephyr_manifest_data.get("mode") != expected_zephyr_mode:
        raise ReliabilityContractError("Zephyr preparation mode does not match reliability subplan")
    linux_rootfs = _regular_file(linux_rootfs, "Linux rootfs")
    zephyr_image = _regular_file(zephyr_image, "Zephyr image")
    _verify_manifest_claim(linux_manifest_data, "outputRootfs", linux_rootfs, "Linux rootfs manifest")
    zephyr_artifacts = zephyr_manifest_data.get("artifacts")
    if not isinstance(zephyr_artifacts, Mapping) or not isinstance(zephyr_artifacts.get("bin"), Mapping):
        raise ReliabilityContractError("Zephyr build manifest has no binary artifact claim")
    _verify_manifest_claim(
        {"bin": zephyr_artifacts["bin"]}, "bin", zephyr_image, "Zephyr build manifest"
    )
    inputs = {
        "from_soak_session": {
            "path": str(Path(from_soak_session).resolve()),
            "source_run": source_binding["sourceRun"],
            "binding_sha256": source_binding["bindingSha256"],
        },
        "build_config": _file_claim(build_config, "AxVisor build config"),
        "qemu_config": {**qemu_claim, "nic_none": True},
        "linux_vmconfig": _file_claim(linux_vmconfig, "Linux VM config"),
        "zephyr_vmconfig": _file_claim(zephyr_vmconfig, "Zephyr VM config"),
        "linux_rootfs": _file_claim(linux_rootfs, "Linux rootfs"),
        "linux_manifest": _file_claim(linux_manifest, "Linux rootfs manifest"),
        "zephyr_image": _file_claim(zephyr_image, "Zephyr image"),
        "zephyr_build_manifest": _file_claim(
            zephyr_build_manifest, "Zephyr build manifest"
        ),
    }
    preflight = {
        "schema_version": "p4-reliability-runtime-preflight-v1",
        "run_id": run_id,
        "mode": mode,
        "scenario": scenario,
        "seed": seed,
        "execution_kind": "guest_runtime_plan",
        "execution": "planned",
        "runtime_backend": "shared-dual-guest",
        "qualified": False,
        "output_dir": str(Path(output_dir).resolve()),
        "inputs": inputs,
        "fault": (
            {
                "profile_id": _fault_profile(mode, seed)["profile_id"],
                "packet_count": _fault_packet_count(mode),
                "manifest_file": _fault_artifact_name(mode),
            }
            if mode in {"fault", "exactly-once"}
            else None
        ),
        "non_claims": [
            "P4-REL runtime, Guest packets, PCAP, recovery, and exactly-once were not started",
            "the preflight does not prove Guest-IP, target build, or P4-REL-01 qualification",
        ],
    }
    validate_runtime_preflight(preflight, validate_source=False)
    return preflight


def validate_runtime_preflight(
    preflight: Mapping[str, Any], *, validate_source: bool = True
) -> dict[str, Any]:
    """Consume a serialized reliability input preflight independently."""

    expected_keys = {
        "schema_version", "run_id", "mode", "scenario", "seed", "execution_kind",
        "execution", "runtime_backend", "qualified", "output_dir", "inputs", "fault",
        "non_claims",
    }
    if set(preflight) != expected_keys:
        raise ReliabilityContractError("runtime preflight keys drifted")
    run_id = _safe_id(preflight.get("run_id"), "run_id")
    mode = preflight.get("mode")
    if mode not in RELIABILITY_SCENARIO_MODES or preflight.get("scenario") != RELIABILITY_SCENARIO_MODES[mode][0]:
        raise ReliabilityContractError("runtime preflight mode/scenario identity drifted")
    if (
        preflight.get("schema_version") != "p4-reliability-runtime-preflight-v1"
        or preflight.get("seed") not in SEEDS
        or preflight.get("execution_kind") != "guest_runtime_plan"
        or preflight.get("execution") != "planned"
        or preflight.get("runtime_backend") != "shared-dual-guest"
        or preflight.get("qualified") is not False
        or not isinstance(preflight.get("non_claims"), list)
    ):
        raise ReliabilityContractError("runtime preflight execution contract drifted")
    inputs = preflight.get("inputs")
    if not isinstance(inputs, Mapping) or set(inputs) != {
        "from_soak_session", "build_config", "qemu_config", "linux_vmconfig",
        "zephyr_vmconfig", "linux_rootfs", "linux_manifest", "zephyr_image",
        "zephyr_build_manifest",
    }:
        raise ReliabilityContractError("runtime preflight input set drifted")
    source = inputs["from_soak_session"]
    if not isinstance(source, Mapping) or not isinstance(source.get("path"), str):
        raise ReliabilityContractError("runtime preflight source claim is incomplete")
    source_binding: Mapping[str, Any] | None = None
    if validate_source:
        try:
            source_binding = validate_soak_binding(Path(source["path"]))
        except (OSError, SoakBindingError) as error:
            raise ReliabilityContractError(f"runtime preflight source revalidation failed: {error}") from error
        if (
            source.get("source_run") != source_binding["sourceRun"]
            or source.get("binding_sha256") != source_binding["bindingSha256"]
        ):
            raise ReliabilityContractError("runtime preflight source binding drifted")
    for name, claim in inputs.items():
        if name == "from_soak_session":
            continue
        if not isinstance(claim, Mapping):
            raise ReliabilityContractError(f"runtime preflight claim is incomplete: {name}")
        path = _regular_file(Path(claim.get("path", "")), f"runtime preflight {name}")
        if claim.get("size") != path.stat().st_size or claim.get("sha256") != _sha256(path):
            raise ReliabilityContractError(f"runtime preflight hash drifted: {name}")
    qemu = inputs["qemu_config"]
    if not isinstance(qemu, Mapping) or qemu.get("nic_none") is not True:
        raise ReliabilityContractError("runtime preflight does not prove -nic none")
    if validate_source:
        try:
            validate_current_qemu_config(Path(qemu["path"]))
        except (OSError, SoakBindingError) as error:
            raise ReliabilityContractError(
                f"runtime preflight QEMU revalidation failed: {error}"
            ) from error
        linux_manifest = _json_object(
            Path(inputs["linux_manifest"]["path"]), "Linux rootfs manifest"
        )
        zephyr_manifest = _json_object(
            Path(inputs["zephyr_build_manifest"]["path"]),
            "Zephyr build manifest",
        )
        try:
            require_matching_binding(
                linux_manifest, source_binding, label="Linux rootfs manifest"
            )
            require_matching_binding(
                zephyr_manifest, source_binding, label="Zephyr build manifest"
            )
        except SoakBindingError as error:
            raise ReliabilityContractError(str(error)) from error
        scenario, linux_mode, zephyr_mode = RELIABILITY_SCENARIO_MODES[mode]
        if (
            linux_manifest.get("bootId") != run_id
            or zephyr_manifest.get("bootId") != run_id
            or linux_manifest.get("mode") != linux_mode
            or zephyr_manifest.get("mode") != zephyr_mode
        ):
            raise ReliabilityContractError("runtime preflight preparation identity drifted")
        _verify_manifest_claim(
            linux_manifest,
            "outputRootfs",
            Path(inputs["linux_rootfs"]["path"]),
            "Linux rootfs manifest",
        )
        zephyr_artifacts = zephyr_manifest.get("artifacts")
        if not isinstance(zephyr_artifacts, Mapping) or not isinstance(zephyr_artifacts.get("bin"), Mapping):
            raise ReliabilityContractError("Zephyr build manifest has no binary artifact claim")
        _verify_manifest_claim(
            {"bin": zephyr_artifacts["bin"]},
            "bin",
            Path(inputs["zephyr_image"]["path"]),
            "Zephyr build manifest",
        )
    if mode in {"fault", "exactly-once"}:
        fault = preflight.get("fault")
        if fault != {
            "profile_id": _fault_profile(mode, int(preflight["seed"]))["profile_id"],
            "packet_count": _fault_packet_count(mode),
            "manifest_file": _fault_artifact_name(mode),
        }:
            raise ReliabilityContractError("runtime preflight fault identity drifted")
    elif preflight.get("fault") is not None:
        raise ReliabilityContractError("runtime preflight has unexpected fault metadata")
    return {
        "schema_version": "p4-reliability-runtime-preflight-validation-v1",
        "valid": True,
        "run_id": run_id,
        "mode": mode,
        "scenario": preflight["scenario"],
        "seed": preflight["seed"],
        "qualified": False,
        "non_claims": list(preflight["non_claims"]),
    }


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise ReliabilityContractError(f"{field} must be a safe identifier")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    publish_new_file(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        error_type=ReliabilityContractError,
    )


def _fault_manifest(seed: int) -> dict[str, Any]:
    if seed not in SEEDS:
        raise ReliabilityContractError(f"seed must be one of {SEEDS}")
    return {
        "schema_version": "p4-reliability-fault-v1",
        "seed": seed,
        "random_source": "forbidden; rules are deterministic",
        "packet_numbering": "one-based",
        "rules": {
            "drop_every": 10,
            "duplicate_every": 17,
            "reorder_every": 23,
            "corrupt_every": 29,
        },
        "operation_order": ["corrupt", "reorder", "duplicate", "drop"],
        "non_claims": ["planned inputs only; no packet was transmitted"],
    }


def _fault_profile(mode: str, seed: int) -> dict[str, Any]:
    """Build the exact profile consumed by the shared P4 fault producer."""

    if mode not in {"fault", "exactly-once"}:
        raise ReliabilityContractError(
            f"fault profile is not defined for subplan mode: {mode}"
        )
    return {
        "schema_version": "p4-network-fault-v1",
        "profile_id": f"p4-rel-{mode}-seed-{seed}",
        "seed": seed,
        "enabled": True,
        "packet_numbering": "one-based",
        "rules": {
            "drop_every": 10,
            "duplicate_every": 17,
            "reorder_every": 23,
            "corrupt_every": 29,
        },
        "operation_order": ["corrupt", "reorder", "duplicate", "drop"],
    }


def _fault_packet_count(mode: str) -> int:
    if mode == "fault":
        return 10_000
    if mode == "exactly-once":
        return 1_000
    raise ReliabilityContractError(
        f"fault packet count is not defined for subplan mode: {mode}"
    )


def _fault_artifact_name(mode: str) -> str:
    if mode not in {"fault", "exactly-once"}:
        raise ReliabilityContractError(
            f"fault artifact is not defined for subplan mode: {mode}"
        )
    return f"fault-manifest-{mode}.json"


def _build_fault_artifact(mode: str, seed: int) -> dict[str, Any]:
    profile = _fault_profile(mode, seed)
    return generate_fault_plan(profile, _fault_packet_count(mode)).as_dict()


def _subplan(mode: str, seed: int) -> dict[str, Any]:
    scenario = MODE_SCENARIO[mode]
    plan: dict[str, Any] = {
        "mode": mode,
        "scenario": scenario,
        "seed": seed,
        "direction": ["linux_to_zephyr", "zephyr_to_linux"],
        "execution": "not_started",
        "runtime_backend": "disabled",
    }
    if mode == "core":
        plan.update(
            {
                "udp": dict(UDP_CONTRACT),
                "messages_per_direction": 10_000,
                "fault_enabled": False,
                "restart": False,
                "exactly_once": False,
            }
        )
    elif mode == "fault":
        plan.update(
            {
                "udp": dict(UDP_CONTRACT),
                "messages_per_direction": 10_000,
                "fault_enabled": True,
                "fault_manifest": _fault_manifest(seed),
                "fault_profile_id": _fault_profile(mode, seed)["profile_id"],
                "fault_packet_count": _fault_packet_count(mode),
                "fault_manifest_file": _fault_artifact_name(mode),
                "restart": False,
                "exactly_once": False,
            }
        )
    elif mode == "restart":
        plan.update(
            {
                "tcp": dict(TCP_CONTRACT),
                "scenario": "test-013",
                "messages": 10_000,
                "fault_enabled": False,
                "restart": {
                    "partial_read_bytes": 1,
                    "merge_every_frames": 31,
                    "disconnects": 1,
                    "endpoint_restarts": 1,
                    "half_frame_eof": True,
                    "new_session_required": True,
                },
                "exactly_once": False,
            }
        )
    elif mode == "exactly-once":
        plan.update(
            {
                "control": dict(CONTROL_CONTRACT),
                "scenario": "test-015",
                "messages": 1_000,
                "fault_enabled": True,
                "fault_manifest": _fault_manifest(seed),
                "fault_profile_id": _fault_profile(mode, seed)["profile_id"],
                "fault_packet_count": _fault_packet_count(mode),
                "fault_manifest_file": _fault_artifact_name(mode),
                "restart": {"session_restart": True, "old_window_cleared": True},
                "exactly_once": {
                    "attempt_schedule_ms": CONTROL_CONTRACT["attempt_schedule_ms"],
                    "cancel_at_ms": CONTROL_CONTRACT["cancel_at_ms"],
                    "forbidden_retry_at_ms": CONTROL_CONTRACT["forbidden_retry_at_ms"],
                    "ack_required": True,
                    "status_correlation_required": True,
                },
            }
        )
    else:  # pragma: no cover - guarded by build_plan
        raise ReliabilityContractError(f"unsupported subplan mode: {mode}")
    return plan


def build_plan(*, run_id: str, mode: str, seed: int) -> dict[str, Any]:
    run_id = _safe_id(run_id, "run_id")
    if mode not in MODES:
        raise ReliabilityContractError(f"mode must be one of {MODES}")
    if seed not in SEEDS:
        raise ReliabilityContractError(f"seed must be one of {SEEDS}")
    modes = ("core", "fault", "restart", "exactly-once") if mode == "full" else (mode,)
    subplans = [_subplan(item, seed) for item in modes]
    return {
        "schema_version": SCHEMA,
        "run_id": run_id,
        "requested_mode": mode,
        "seed": seed,
        "subplans": subplans,
        "execution_kind": "host_contract",
        "evidence_level": "L2 host contract",
        "qualified": False,
        "runtime_evidence": "not_produced",
        "non_claims": [
            "no Guest, QEMU, WSL, network, PCAP, or runtime packets were produced",
            "this plan cannot close TEST-012, TEST-013, TEST-015, or P4-REL-01",
        ],
    }


def _file_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"manifest.json", "status.json"}:
            continue
        if path.is_symlink():
            raise ReliabilityContractError(f"refusing symlinked plan artifact: {path}")
        relative = path.relative_to(root).as_posix()
        records.append(
            {"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)}
        )
    return records


def write_plan(plan: Mapping[str, Any], output_dir: Path) -> None:
    if output_dir.exists():
        raise ReliabilityContractError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    _write_json(output_dir / "plan.json", plan)
    for subplan in plan["subplans"]:
        if subplan.get("fault_enabled") is True:
            artifact_name = subplan.get("fault_manifest_file")
            if not isinstance(artifact_name, str):
                raise ReliabilityContractError(
                    f"fault subplan {subplan.get('mode')!r} has no artifact name"
                )
            _write_json(
                output_dir / artifact_name,
                _build_fault_artifact(subplan["mode"], int(plan["seed"])),
            )
    commands = [
        {
            "schema_version": "p4-reliability-command-v1",
            "run_id": plan["run_id"],
            "mode": subplan["mode"],
            "execution": "not_started",
            "argv": [],
            "reason": "host-only plan; runtime backend is disabled",
        }
        for subplan in plan["subplans"]
    ]
    publish_new_file(
        output_dir / "commands.jsonl",
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in commands).encode(
            "utf-8"
        ),
        error_type=ReliabilityContractError,
    )
    _write_json(
        output_dir / "summary.json",
        {
            "schema_version": SUMMARY_SCHEMA,
            "run_id": plan["run_id"],
            "requested_mode": plan["requested_mode"],
            "planned_subplans": len(plan["subplans"]),
            "completed_subplans": 0,
            "status": "blocked",
            "qualified": False,
            "missing_runtime_events": ["guest_packets", "pcap", "recovery", "exactly_once"],
        },
    )
    _write_json(
        output_dir / "cleanup.json",
        {"residualProcesses": [], "residualSockets": [], "residualFiles": []},
    )
    _write_json(
        output_dir / "manifest.json",
        {
            "schema_version": MANIFEST_SCHEMA,
            "run_id": plan["run_id"],
            "files": _file_records(output_dir),
            "excluded_terminal_files": ["manifest.json", "status.json"],
        },
    )
    _write_json(
        output_dir / "status.json",
        {
            "schema_version": STATUS_SCHEMA,
            "run_id": plan["run_id"],
            "success": False,
            "qualified": False,
            "status": "p4_reliability_plan_blocked",
            "execution_kind": "host_contract",
            "evidence_level": "L2 host contract",
            "blockedReason": "runtime backend disabled; plan only",
            "manifestSha256": _sha256(output_dir / "manifest.json"),
            "pushPerformed": False,
            "statusLast": True,
        },
    )


def validate_plan_bundle(
    output_dir: Path, *, run_id: str, mode: str, seed: int
) -> dict[str, Any]:
    """Independently consume a host-only plan/status bundle.

    This validator proves only that the immutable plan package is internally
    consistent.  It cannot turn the package into P4 runtime evidence.
    """

    candidate = Path(output_dir)
    if candidate.is_symlink() or not candidate.is_dir():
        raise ReliabilityContractError(
            f"plan output directory must be a regular directory: {output_dir}"
        )
    root = candidate.resolve()
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise ReliabilityContractError(f"plan package contains symlink: {entry}")

    def read_json(name: str) -> dict[str, Any]:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ReliabilityContractError(f"plan artifact is missing or unsafe: {name}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReliabilityContractError(f"plan artifact is invalid JSON: {name}") from error
        if not isinstance(value, dict):
            raise ReliabilityContractError(f"plan artifact must be an object: {name}")
        return value

    plan = read_json("plan.json")
    summary = read_json("summary.json")
    cleanup = read_json("cleanup.json")
    manifest = read_json("manifest.json")
    status = read_json("status.json")
    run_id = _safe_id(run_id, "run_id")
    if mode not in MODES or seed not in SEEDS:
        raise ReliabilityContractError("validator mode/seed is outside the frozen contract")
    if (
        plan.get("schema_version") != SCHEMA
        or plan.get("run_id") != run_id
        or plan.get("requested_mode") != mode
        or plan.get("seed") != seed
        or plan.get("execution_kind") != "host_contract"
        or plan.get("evidence_level") != "L2 host contract"
        or plan.get("qualified") is not False
        or plan.get("runtime_evidence") != "not_produced"
    ):
        raise ReliabilityContractError("plan identity or non-qualification claim drifted")
    subplans = plan.get("subplans")
    expected_modes = ["core", "fault", "restart", "exactly-once"] if mode == "full" else [mode]
    if (
        not isinstance(subplans, list)
        or [item.get("mode") for item in subplans if isinstance(item, dict)] != expected_modes
        or any(
            not isinstance(item, dict)
            or item.get("run_id") not in (None, run_id)
            or item.get("execution") != "not_started"
            or item.get("runtime_backend") != "disabled"
            for item in subplans
        )
    ):
        raise ReliabilityContractError("plan subplans are not frozen and disabled")
    expected_fault_files: set[str] = set()
    for subplan in subplans:
        if subplan.get("fault_enabled") is True:
            mode_name = subplan.get("mode")
            if mode_name not in {"fault", "exactly-once"}:
                raise ReliabilityContractError("unknown fault-enabled reliability subplan")
            expected_profile = _fault_profile(mode_name, seed)
            expected_count = _fault_packet_count(mode_name)
            if (
                subplan.get("fault_profile_id") != expected_profile["profile_id"]
                or subplan.get("fault_packet_count") != expected_count
                or subplan.get("fault_manifest_file") != _fault_artifact_name(mode_name)
            ):
                raise ReliabilityContractError("fault subplan identity drifted")
            fault_path = root / _fault_artifact_name(mode_name)
            if not fault_path.is_file() or fault_path.is_symlink():
                raise ReliabilityContractError("fault manifest artifact is missing or unsafe")
            try:
                fault_document = json.loads(fault_path.read_text(encoding="utf-8"))
                validate_fault_manifest(
                    fault_document,
                    expected_packet_count=expected_count,
                    expected_profile_id=expected_profile["profile_id"],
                )
            except (OSError, json.JSONDecodeError, FaultProfileError) as error:
                raise ReliabilityContractError(
                    f"fault manifest artifact is not canonical: {fault_path.name}"
                ) from error
            expected_fault_files.add(fault_path.name)
        elif any(
            key in subplan
            for key in ("fault_profile_id", "fault_packet_count", "fault_manifest_file")
        ):
            raise ReliabilityContractError("fault metadata present on disabled subplan")
    expected_files = {
        "plan.json",
        "commands.jsonl",
        "summary.json",
        "cleanup.json",
        *expected_fault_files,
    }
    if (
        summary.get("schema_version") != SUMMARY_SCHEMA
        or summary.get("run_id") != run_id
        or summary.get("requested_mode") != mode
        or summary.get("planned_subplans") != len(expected_modes)
        or summary.get("completed_subplans") != 0
        or summary.get("status") != "blocked"
        or summary.get("qualified") is not False
    ):
        raise ReliabilityContractError("plan summary is not fail-closed")
    if cleanup != {"residualProcesses": [], "residualSockets": [], "residualFiles": []}:
        raise ReliabilityContractError("plan cleanup contains residual claims")
    if manifest.get("schema_version") != MANIFEST_SCHEMA or manifest.get("run_id") != run_id:
        raise ReliabilityContractError("plan manifest identity/schema drifted")
    if manifest.get("excluded_terminal_files") != ["manifest.json", "status.json"]:
        raise ReliabilityContractError("plan manifest terminal-file exclusion drifted")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ReliabilityContractError("plan manifest files must be a list")
    actual_records = _file_records(root)
    if files != actual_records:
        raise ReliabilityContractError("plan manifest file records are not byte-exact")
    if {record["path"] for record in actual_records} != expected_files:
        raise ReliabilityContractError("plan bundle contains an unexpected artifact")
    expected_directories: set[str] = set()
    for record in actual_records:
        parts = record["path"].split("/")[:-1]
        for index in range(1, len(parts) + 1):
            expected_directories.add("/".join(parts[:index]))
    actual_directories = {
        entry.relative_to(root).as_posix()
        for entry in root.rglob("*")
        if entry.is_dir()
    }
    if actual_directories != expected_directories:
        raise ReliabilityContractError("plan package directory set drifted")
    if (
        set(status) != {
            "schema_version", "run_id", "success", "qualified", "status",
            "execution_kind", "evidence_level", "blockedReason", "manifestSha256",
            "pushPerformed", "statusLast",
        }
        or status.get("schema_version") != STATUS_SCHEMA
        or status.get("run_id") != run_id
        or status.get("success") is not False
        or status.get("qualified") is not False
        or status.get("status") != "p4_reliability_plan_blocked"
        or status.get("execution_kind") != "host_contract"
        or status.get("evidence_level") != "L2 host contract"
        or not isinstance(status.get("blockedReason"), str)
        or not status["blockedReason"]
        or status.get("manifestSha256") != _sha256(root / "manifest.json")
        or status.get("pushPerformed") is not False
        or status.get("statusLast") is not True
    ):
        raise ReliabilityContractError("plan status is not a status-last blocked result")
    commands = root / "commands.jsonl"
    if not commands.is_file() or commands.is_symlink():
        raise ReliabilityContractError("plan commands artifact is missing or unsafe")
    rows = []
    for line_number, line in enumerate(commands.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ReliabilityContractError(f"commands.jsonl line {line_number} is invalid") from error
        if not isinstance(row, dict):
            raise ReliabilityContractError(f"commands.jsonl line {line_number} is not an object")
        rows.append(row)
    if len(rows) != len(expected_modes):
        raise ReliabilityContractError("plan command count does not match subplans")
    for row, expected_mode in zip(rows, expected_modes):
        if (
            row.get("schema_version") != "p4-reliability-command-v1"
            or row.get("run_id") != run_id
            or row.get("mode") != expected_mode
            or row.get("execution") != "not_started"
            or row.get("argv") != []
        ):
            raise ReliabilityContractError("plan command identity/execution claim drifted")
    return {
        "schema_version": "p4-reliability-plan-validation-v1",
        "valid": True,
        "run_id": run_id,
        "mode": mode,
        "seed": seed,
        "qualified": False,
        "non_claims": [
            "plan validation does not prove Guest packets, PCAP, recovery, exactly-once, or P4-REL-01"
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--seed", choices=SEEDS, type=int, default=7)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--from-soak-session", type=Path)
    parser.add_argument("--build-config", type=Path)
    parser.add_argument("--qemu-config", type=Path)
    parser.add_argument("--linux-vmconfig", type=Path)
    parser.add_argument("--zephyr-vmconfig", type=Path)
    parser.add_argument("--linux-rootfs", type=Path)
    parser.add_argument("--linux-rootfs-manifest", type=Path)
    parser.add_argument("--zephyr-image", type=Path)
    parser.add_argument("--zephyr-build-manifest", type=Path)
    parser.add_argument("--preflight-output", type=Path)
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[3],
    )
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--runtime-root", type=Path, default=Path("/tmp"))
    args = parser.parse_args(argv)
    try:
        preflight_values = {
            "from_soak_session": args.from_soak_session,
            "build_config": args.build_config,
            "qemu_config": args.qemu_config,
            "linux_vmconfig": args.linux_vmconfig,
            "zephyr_vmconfig": args.zephyr_vmconfig,
            "linux_rootfs": args.linux_rootfs,
            "linux_manifest": args.linux_rootfs_manifest,
            "zephyr_image": args.zephyr_image,
            "zephyr_build_manifest": args.zephyr_build_manifest,
        }
        if args.preflight_output is not None or any(
            value is not None for value in preflight_values.values()
        ):
            if args.preflight_output is None:
                raise ReliabilityContractError(
                    "runtime input arguments require --preflight-output"
                )
            missing = [name for name, value in preflight_values.items() if value is None]
            if missing:
                raise ReliabilityContractError(
                    "runtime preflight inputs are incomplete: " + ", ".join(missing)
                )
            preflight = build_runtime_preflight(
                from_soak_session=args.from_soak_session,
                build_config=args.build_config,
                qemu_config=args.qemu_config,
                linux_vmconfig=args.linux_vmconfig,
                zephyr_vmconfig=args.zephyr_vmconfig,
                linux_rootfs=args.linux_rootfs,
                linux_manifest=args.linux_rootfs_manifest,
                zephyr_image=args.zephyr_image,
                zephyr_build_manifest=args.zephyr_build_manifest,
                mode=args.mode,
                run_id=args.run_id,
                seed=args.seed,
                output_dir=args.output_dir or args.preflight_output.parent,
            )
            _write_json(args.preflight_output, preflight)
            validate_runtime_preflight(
                json.loads(args.preflight_output.read_text(encoding="utf-8"))
            )
            if args.dry_run:
                print(
                    f"P4_RELIABILITY_PREFLIGHT_PASS mode={args.mode} "
                    f"scenario={preflight['scenario']} qualified=false"
                )
                return 0
            if args.output_dir is None:
                raise ReliabilityContractError(
                    "runtime execution requires a fresh --output-dir"
                )
            try:
                from runtime_executor import (
                    ReliabilityRuntimeError,
                    execute_reliability_runtime,
                )
            except ImportError:  # pragma: no cover - package import path
                from .runtime_executor import (  # type: ignore[no-redef]
                    ReliabilityRuntimeError,
                    execute_reliability_runtime,
                )
            try:
                status = execute_reliability_runtime(
                    preflight,
                    repository=args.repository,
                    output_dir=args.output_dir,
                    timeout_seconds=args.timeout_seconds,
                    runtime_root=args.runtime_root,
                )
            except ReliabilityRuntimeError as error:
                raise ReliabilityContractError(str(error)) from error
            print(
                f"P4_RELIABILITY_RUNTIME_{'PASS' if status['success'] else 'FAILED'} "
                f"mode={args.mode} qualified=false"
            )
            return 0 if status["success"] else 1
        plan = build_plan(run_id=args.run_id, mode=args.mode, seed=args.seed)
        if not args.dry_run:
            raise ReliabilityContractError(
                "runtime execution is disabled; pass --dry-run for a host plan"
            )
        if args.output_dir is not None:
            write_plan(plan, args.output_dir)
            validate_plan_bundle(
                args.output_dir, run_id=args.run_id, mode=args.mode, seed=args.seed
            )
    except (OSError, ReliabilityContractError) as error:
        print(f"P4 reliability plan failed closed: {error}")
        return 1
    print(
        f"P4_RELIABILITY_DRY_RUN_PASS mode={args.mode} "
        f"subplans={len(plan['subplans'])} qualified=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
