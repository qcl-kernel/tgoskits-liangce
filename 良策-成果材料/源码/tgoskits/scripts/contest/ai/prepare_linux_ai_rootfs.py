#!/usr/bin/env python3
"""Prepare a disposable P5 Linux AI rootfs.

The default ``--dry-run`` path only validates inputs and writes a host plan.
The materializer is deliberately separate from planning: it requires an
immutable source-rootfs claim, an available ``debugfs`` executable, fresh
output paths, and verifies every injected file by dumping it back from the
ext4 image before publishing the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from . import closed_loop_contract
    from ..host_vm_carveout_io import publish_new_file
    from .host_orchestration import MODEL_ARTIFACTS, validate_profile_contract
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import closed_loop_contract  # type: ignore[no-redef]
    from host_vm_carveout_io import publish_new_file  # type: ignore[no-redef]
    from host_orchestration import MODEL_ARTIFACTS, validate_profile_contract  # type: ignore[no-redef]


SCHEMA = "p5-ai-rootfs-plan-v1"
STATUS_SCHEMA = "p5-ai-rootfs-status-v1"
MATERIALIZED_SCHEMA = "p5-ai-rootfs-v1"
DEPENDENCY_SCHEMA = "p5-linux-controller-dependencies-v1"
CONTROLLERS = ("fixed", "mlp")
SEEDS = (7, 19, 43)
NETWORK_DEFAULTS = {
    "bind_ip": "10.77.0.1",
    "peer_ip": "10.77.0.2",
    "udp_port": 46000,
    "tcp_port": 46001,
}
_CHECKSUM_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+(?:\*?)([^\s]+)$")


class RootfsPlanError(ValueError):
    """Raised when a P5 rootfs plan cannot be frozen safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _regular_file(path: Path, label: str) -> Path:
    path = Path(path)
    if path.is_symlink():
        raise RootfsPlanError(f"{label} must not be a symlink/reparse point: {path}")
    try:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
    except OSError as error:
        raise RootfsPlanError(f"{label} cannot be inspected: {path}: {error}") from error
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise RootfsPlanError(f"{label} must not be a symlink/reparse point: {path}")
    path = path.resolve()
    if not path.is_file():
        raise RootfsPlanError(f"{label} must be a regular file: {path}")
    return path


def _regular_directory(path: Path, label: str) -> Path:
    path = Path(path)
    if path.is_symlink():
        raise RootfsPlanError(f"{label} must not be a symlink/reparse point: {path}")
    try:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
    except OSError as error:
        raise RootfsPlanError(f"{label} cannot be inspected: {path}: {error}") from error
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise RootfsPlanError(f"{label} must not be a symlink/reparse point: {path}")
    path = path.resolve()
    if not path.is_dir():
        raise RootfsPlanError(f"{label} must be a directory: {path}")
    return path


def _json_file(path: Path, label: str) -> tuple[Path, dict[str, Any]]:
    path = _regular_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RootfsPlanError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise RootfsPlanError(f"{label} root must be an object")
    return path, value


def _safe_output_path(path: Path, label: str) -> Path:
    """Return an absolute output path while rejecting existing reparse paths."""

    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise RootfsPlanError(f"{label} must not already exist: {path}")
    for ancestor in path.parents:
        if not ancestor.exists() and not ancestor.is_symlink():
            continue
        if ancestor.is_symlink():
            raise RootfsPlanError(
                f"{label} parent must not be a symlink/reparse point: {ancestor}"
            )
        try:
            attributes = getattr(ancestor.stat(), "st_file_attributes", 0)
        except OSError as error:
            raise RootfsPlanError(f"{label} parent cannot be inspected: {ancestor}: {error}") from error
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise RootfsPlanError(
                f"{label} parent must not be a symlink/reparse point: {ancestor}"
            )
    return path


def _record_file(path: Path, label: str) -> dict[str, Any]:
    path = _regular_file(path, label)
    return {"path": str(path), "size": path.stat().st_size, "sha256": _sha256(path)}


def _validate_checksum_file(path: Path, expected: dict[str, str]) -> dict[str, Any]:
    path = _regular_file(path, "model checksums")
    entries: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RootfsPlanError(f"model checksums cannot be read: {error}") from error
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        match = _CHECKSUM_LINE.fullmatch(line.strip())
        if match is None:
            raise RootfsPlanError(f"model checksums has invalid line {line_number}")
        digest, name = match.groups()
        if name in entries:
            raise RootfsPlanError(f"model checksums has duplicate entry: {name}")
        entries[name] = digest.lower()
    if entries != expected:
        raise RootfsPlanError("model checksums do not exactly match the qualification profile")
    return _record_file(path, "model checksums")


def _validate_network(
    *,
    bind_ip: str | None,
    peer_ip: str | None,
    udp_port: int | None,
    tcp_port: int | None,
) -> dict[str, Any] | None:
    values = (bind_ip, peer_ip, udp_port, tcp_port)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise RootfsPlanError("bind/peer IP and UDP/TCP ports must be supplied together")
    try:
        ipaddress.ip_address(bind_ip)
        ipaddress.ip_address(peer_ip)
    except ValueError as error:
        raise RootfsPlanError(f"network address is invalid: {error}") from error
    if bind_ip != NETWORK_DEFAULTS["bind_ip"] or peer_ip != NETWORK_DEFAULTS["peer_ip"]:
        raise RootfsPlanError("network IPs drift from the frozen 10.77.0.0/24 contract")
    if udp_port != NETWORK_DEFAULTS["udp_port"] or tcp_port != NETWORK_DEFAULTS["tcp_port"]:
        raise RootfsPlanError("network ports drift from the frozen 46000/46001 contract")
    return {
        "bind_ip": bind_ip,
        "peer_ip": peer_ip,
        "udp_port": udp_port,
        "tcp_port": tcp_port,
    }


def _manifest_rootfs_claim(manifest: dict[str, Any]) -> dict[str, Any]:
    candidates: list[Any] = [manifest.get("linuxRootfs"), manifest.get("sourceRootfs")]
    inputs = manifest.get("inputs")
    if isinstance(inputs, dict):
        candidates.extend([inputs.get("linuxRootfs"), inputs.get("sourceRootfs")])
    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance(candidate.get("sha256"), str):
            return candidate
    raise RootfsPlanError("source manifest has no Linux/source rootfs hash claim")


def build_plan(
    *,
    run_id: str,
    session_id: int,
    source_rootfs: Path | None,
    source_manifest: Path | None = None,
    profile: Path,
    model_directory: Path,
    controller_mode: str = "mlp",
    seed: int = 43,
    repository: Path | None = None,
    from_network_session: Path | None = None,
    controller_binary: Path | None = None,
    model: Path | None = None,
    metadata: Path | None = None,
    dataset_manifest: Path | None = None,
    golden: Path | None = None,
    model_checksums: Path | None = None,
    bind_ip: str | None = None,
    peer_ip: str | None = None,
    udp_port: int | None = None,
    tcp_port: int | None = None,
    output_rootfs: Path | None = None,
    output_controller_config: Path | None = None,
    output_manifest: Path | None = None,
) -> dict[str, Any]:
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        raise RootfsPlanError("run-id must be a non-empty path-safe identifier")
    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id <= 0:
        raise RootfsPlanError("session-id must be a positive integer")
    if controller_mode not in CONTROLLERS:
        raise RootfsPlanError(f"controller-mode must be one of {CONTROLLERS}")
    if seed not in SEEDS:
        raise RootfsPlanError(f"seed must be one of {SEEDS}")
    profile = _regular_file(profile, "profile")
    try:
        profile_data = json.loads(profile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RootfsPlanError(f"profile is not valid JSON: {error}") from error
    if not isinstance(profile_data, dict):
        raise RootfsPlanError("profile root must be an object")
    try:
        validate_profile_contract(profile_data)
    except (KeyError, TypeError, ValueError) as error:
        raise RootfsPlanError(f"profile contract failed: {error}") from error
    try:
        fault_manifest = closed_loop_contract.generate_fault_manifest(
            profile=profile_data, seed=seed
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RootfsPlanError(f"fault manifest contract failed: {error}") from error

    model_directory = _regular_directory(model_directory, "model directory")
    expected = profile_data["model"]
    expected_hashes = {
        "model.bin": expected["model_sha256"],
        "metadata.json": expected["metadata_sha256"],
        "golden-vectors.json": expected["golden_vectors_sha256"],
        "dataset-manifest.json": expected["dataset_manifest_sha256"],
    }
    model_files: dict[str, dict[str, Any]] = {}
    for name in MODEL_ARTIFACTS:
        path = _regular_file(model_directory / name, f"model artifact {name}")
        actual_hash = _sha256(path)
        if actual_hash != expected_hashes[name]:
            raise RootfsPlanError(f"model artifact hash mismatch: {name}")
        model_files[name] = {
            "path": str(path),
            "size": path.stat().st_size,
            "sha256": actual_hash,
        }

    explicit_artifacts = {
        "model.bin": model,
        "metadata.json": metadata,
        "dataset-manifest.json": dataset_manifest,
        "golden-vectors.json": golden,
    }
    for name, explicit_path in explicit_artifacts.items():
        if explicit_path is None:
            continue
        actual = _regular_file(explicit_path, f"explicit model artifact {name}")
        if actual != Path(model_files[name]["path"]):
            raise RootfsPlanError(f"explicit model artifact is not canonical {name}")
    checksum_claim = None
    if model_checksums is not None:
        checksum_claim = _validate_checksum_file(model_checksums, expected_hashes)

    repository_claim = None
    if repository is not None:
        repository_path = _regular_directory(repository, "repository")
        repository_claim = str(repository_path)

    session_claim = None
    if from_network_session is not None:
        session_path = _regular_directory(from_network_session, "from-network-session")
        session_manifest = session_path / "manifest.json"
        session_manifest_path, _ = _json_file(session_manifest, "from-network-session manifest")
        session_claim = {
            "path": str(session_path),
            "manifest": str(session_manifest_path),
            "manifest_sha256": _sha256(session_manifest_path),
        }

    binary_claim = None
    if controller_binary is not None:
        binary_claim = _record_file(controller_binary, "controller binary")
        binary_claim["dependency_validation"] = "not_started_host_only"

    network = _validate_network(
        bind_ip=bind_ip,
        peer_ip=peer_ip,
        udp_port=udp_port,
        tcp_port=tcp_port,
    )

    planned_outputs = None
    supplied_outputs = {
        "rootfs": output_rootfs,
        "controller_config": output_controller_config,
        "manifest": output_manifest,
    }
    if any(value is not None for value in supplied_outputs.values()):
        if any(value is None for value in supplied_outputs.values()):
            raise RootfsPlanError("all three output paths are required together")
        checked_outputs = {
            key: _safe_output_path(value, f"output {key}")
            for key, value in supplied_outputs.items()
        }
        if len(set(checked_outputs.values())) != len(checked_outputs):
            raise RootfsPlanError("output paths must be distinct")
        planned_outputs = {key: str(value) for key, value in checked_outputs.items()}

    startup_argv = None
    controller_config = None
    if network is not None:
        startup_argv = [
            "/linux-ai-controller",
            "--mode",
            controller_mode,
            "--seed",
            str(seed),
            "--bind",
            network["bind_ip"],
            "--peer",
            network["peer_ip"],
            "--udp-port",
            str(network["udp_port"]),
            "--tcp-port",
            str(network["tcp_port"]),
            "--model",
            "/opt/tgos/model.bin",
            "--metadata",
            "/opt/tgos/metadata.json",
            "--run-id",
            run_id,
            "--session-id",
            str(session_id),
            "--event-log",
            "/run/tgos/linux-events.jsonl",
        ]
        controller_config = {
            "schema_version": "p5-linux-controller-config-v1",
            "run_id": run_id,
            "session_id": session_id,
            "controller_mode": controller_mode,
            "seed": seed,
            "model_version": 0 if controller_mode == "fixed" else profile_data["model"]["model_version"],
            "network": network,
            "fault_manifest_sha256": _json_sha256(fault_manifest),
            "execution": "not_started",
        }

    source_claim = None
    source_manifest_claim = None
    if source_rootfs is not None:
        if source_manifest is None:
            raise RootfsPlanError("source-manifest is required when source-rootfs is provided")
        source = _regular_file(source_rootfs, "source rootfs")
        manifest_path, manifest_data = _json_file(source_manifest, "source manifest")
        source_manifest_claim = {
            "path": str(manifest_path),
            "sha256": _sha256(manifest_path),
        }
        rootfs_claim = _manifest_rootfs_claim(manifest_data)
        source_size = source.stat().st_size
        source_hash = _sha256(source)
        if rootfs_claim.get("sha256") != source_hash or rootfs_claim.get("size") != source_size:
            raise RootfsPlanError(
                "source rootfs does not match the size/hash claim in source manifest"
            )
        source_claim = {
            "path": str(source),
            "name": source.name,
            "size": source_size,
            "sha256": source_hash,
            "source_manifest_sha256": source_manifest_claim["sha256"],
            "mutation": "disabled",
        }
    return {
        "schema_version": SCHEMA,
        "run_id": run_id,
        "session_id": session_id,
        "controller_mode": controller_mode,
        "seed": seed,
        "profile_id": profile_data["profile_id"],
        "test_id": profile_data["test_id"],
        "model_version": profile_data["model"]["model_version"],
        "fault_manifest": fault_manifest,
        "repository": repository_claim,
        "from_network_session": session_claim,
        "controller_binary": binary_claim,
        "source_rootfs": source_claim,
        "source_manifest": source_manifest_claim,
        "model": {
            "directory": str(model_directory),
            "files": model_files,
            "checksums": checksum_claim,
        },
        "network": network,
        "planned_outputs": planned_outputs,
        "startup_argv": startup_argv,
        "controller_config": controller_config,
        "guest_destination": "/linux-ai-controller",
        "execution_kind": "host_contract",
        "execution": "not_started",
        "qualified": False,
        "runtime_evidence": "not_produced",
        "non_claims": [
            "no debugfs/rootfs mutation was performed",
            "no Linux Guest, UDP/IP, mediated NIC, or AI-loop evidence was produced",
        ],
    }


def write_plan(plan: dict[str, Any], output_dir: Path) -> None:
    if output_dir.exists():
        raise RootfsPlanError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    publish_new_file(
        output_dir / "plan.json",
        (json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        error_type=RootfsPlanError,
    )
    publish_new_file(
        output_dir / "commands.jsonl",
        (
            json.dumps(
                {
                    "execution": "not_started",
                    "argv": [],
                    "reason": "dry-run plan only; rootfs materialization was not requested",
                },
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
        error_type=RootfsPlanError,
    )
    publish_new_file(
        output_dir / "status.json",
        (
            json.dumps(
                {
                    "schema_version": STATUS_SCHEMA,
                    "run_id": plan["run_id"],
                    "success": False,
                    "qualified": False,
                    "status": "p5_rootfs_plan_only",
                    "blockedReason": "dry-run plan only; rootfs materialization was not requested",
                    "execution_kind": "host_contract",
                    "statusLast": True,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
        error_type=RootfsPlanError,
    )


def _tool_path(tool: str, label: str) -> str:
    resolved = shutil.which(tool)
    if resolved is None:
        candidate = Path(tool)
        if candidate.is_file() and not candidate.is_symlink():
            resolved = str(candidate.resolve())
    if resolved is None:
        raise RootfsPlanError(f"{label} executable is unavailable: {tool}")
    return resolved


def _run_debugfs(
    executable: str,
    image: Path,
    request: str,
    commands: list[dict[str, Any]],
    *,
    write: bool,
    allow_existing: bool = False,
) -> subprocess.CompletedProcess[str]:
    argv = [executable]
    if write:
        argv.append("-w")
    argv.extend(["-R", request, str(image)])
    result = subprocess.run(argv, capture_output=True, text=True)
    stdout = result.stdout[-1000:]
    stderr = result.stderr[-1000:]
    commands.append(
        {
            "argv": argv,
            "exit_code": result.returncode,
            "stdout_tail": stdout,
            "stderr_tail": stderr,
        }
    )
    if result.returncode != 0:
        detail = f"stdout={stdout!r} stderr={stderr!r}"
        if not (allow_existing and "exist" in f"{stdout} {stderr}".lower()):
            raise RootfsPlanError(
                f"debugfs command failed ({result.returncode}): {request}; {detail}"
            )
    return result


def _validate_controller_dependencies(
    binary: Path,
    *,
    readelf: str,
    dependency_manifest: Path | None,
) -> dict[str, Any]:
    """Prove static linkage or bind every dynamic dependency to a hash claim."""

    executable = _tool_path(readelf, "readelf")
    commands = []
    outputs: dict[str, str] = {}
    for mode, flags in (("program", "-lW"), ("dynamic", "-dW")):
        argv = [executable, flags, str(binary)]
        result = subprocess.run(argv, capture_output=True, text=True)
        commands.append(
            {
                "argv": argv,
                "exit_code": result.returncode,
                "stdout_tail": result.stdout[-2000:],
                "stderr_tail": result.stderr[-1000:],
            }
        )
        if result.returncode != 0:
            raise RootfsPlanError(
                f"readelf {mode} inspection failed ({result.returncode}): {binary}"
            )
        outputs[mode] = result.stdout

    program = outputs["program"]
    dynamic = outputs["dynamic"]
    interpreter_match = re.search(r"Requesting program interpreter:\s*([^]]+)\]", program)
    needed = sorted(set(re.findall(r"\(NEEDED\).*?\[([^]]+)\]", dynamic)))
    dynamic_binary = interpreter_match is not None or bool(needed) or "DYNAMIC" in program
    claim: dict[str, Any] = {
        "schema_version": DEPENDENCY_SCHEMA,
        "binary_sha256": _sha256(binary),
        "linkage": "dynamic" if dynamic_binary else "static",
        "interpreter": interpreter_match.group(1).strip() if interpreter_match else None,
        "needed": needed,
        "commands": commands,
    }
    if not dynamic_binary:
        claim["status"] = "verified_host_readelf"
        return claim
    if dependency_manifest is None:
        raise RootfsPlanError(
            "controller binary is dynamically linked; dependency-manifest is required"
        )
    manifest_path, manifest = _json_file(dependency_manifest, "controller dependency manifest")
    if manifest.get("schema_version") != DEPENDENCY_SCHEMA:
        raise RootfsPlanError("controller dependency manifest schema mismatch")
    if manifest.get("binary_sha256") != claim["binary_sha256"]:
        raise RootfsPlanError("controller dependency manifest binary hash mismatch")
    if manifest.get("interpreter") != claim["interpreter"]:
        raise RootfsPlanError("controller dependency manifest interpreter mismatch")
    libraries = manifest.get("libraries")
    if not isinstance(libraries, list):
        raise RootfsPlanError("controller dependency manifest libraries must be a list")
    by_name: dict[str, dict[str, Any]] = {}
    for index, library in enumerate(libraries):
        if not isinstance(library, dict) or not isinstance(library.get("soname"), str):
            raise RootfsPlanError(f"controller dependency entry {index} is invalid")
        soname = library["soname"]
        if soname in by_name:
            raise RootfsPlanError(f"controller dependency is duplicated: {soname}")
        path_value = library.get("path")
        if not isinstance(path_value, str):
            raise RootfsPlanError(f"controller dependency path is missing: {soname}")
        dependency_path = _regular_file(Path(path_value), f"controller dependency {soname}")
        if library.get("size") != dependency_path.stat().st_size or library.get("sha256") != _sha256(dependency_path):
            raise RootfsPlanError(f"controller dependency hash mismatch: {soname}")
        by_name[soname] = {
            "soname": soname,
            "path": str(dependency_path),
            "size": dependency_path.stat().st_size,
            "sha256": _sha256(dependency_path),
        }
    if sorted(by_name) != needed:
        raise RootfsPlanError("controller dependency manifest does not exactly match DT_NEEDED")
    claim.update(
        {
            "status": "verified_host_readelf_and_manifest",
            "manifest": {"path": str(manifest_path), "sha256": _sha256(manifest_path)},
            "libraries": [by_name[name] for name in needed],
        }
    )
    return claim


def _materialized_guest_files(plan: dict[str, Any]) -> dict[str, Path]:
    controller_claim = plan.get("controller_binary")
    model = plan.get("model")
    if not isinstance(controller_claim, dict) or not isinstance(model, dict):
        raise RootfsPlanError("rootfs plan is missing controller/model claims")
    controller_path = controller_claim.get("path")
    model_files = model.get("files")
    if not isinstance(controller_path, str) or not isinstance(model_files, dict):
        raise RootfsPlanError("rootfs plan has invalid controller/model claims")
    controller = _regular_file(Path(controller_path), "controller binary")
    controller_claim_size = controller_claim.get("size")
    controller_claim_hash = controller_claim.get("sha256")
    if controller_claim_size != controller.stat().st_size or controller_claim_hash != _sha256(controller):
        raise RootfsPlanError("controller binary changed after the plan was built")
    paths: dict[str, Path] = {
        "/linux-ai-controller": controller,
    }
    for name in MODEL_ARTIFACTS:
        claim = model_files.get(name)
        if not isinstance(claim, dict) or not isinstance(claim.get("path"), str):
            raise RootfsPlanError(f"rootfs plan is missing model artifact claim: {name}")
        artifact = _regular_file(Path(claim["path"]), f"model artifact {name}")
        if claim.get("size") != artifact.stat().st_size or claim.get("sha256") != _sha256(artifact):
            raise RootfsPlanError(f"model artifact changed after the plan was built: {name}")
        paths[f"/opt/tgos/{name}"] = artifact
    return paths


def materialize_rootfs(
    plan: dict[str, Any],
    *,
    debugfs: str = "debugfs",
    readelf: str = "readelf",
    dependency_manifest: Path | None = None,
) -> dict[str, Any]:
    """Write a fresh ext4 copy and verify all guest files by dump-back.

    This function performs only the disposable image mutation.  It never
    starts a Guest, WSL, QEMU, or controller process.  The caller must pass a
    plan produced by :func:`build_plan`; all output paths are still rechecked
    immediately before publication to retain the no-overwrite contract.
    """

    if plan.get("schema_version") != SCHEMA:
        raise RootfsPlanError("materializer requires a p5-ai-rootfs-plan-v1 plan")
    source_claim = plan.get("source_rootfs")
    source_manifest_claim = plan.get("source_manifest")
    planned_outputs = plan.get("planned_outputs")
    if not isinstance(source_claim, dict) or not isinstance(source_manifest_claim, dict):
        raise RootfsPlanError("source-rootfs and source-manifest are required for materialization")
    if not isinstance(planned_outputs, dict) or set(planned_outputs) != {
        "rootfs",
        "controller_config",
        "manifest",
    }:
        raise RootfsPlanError("materialization requires all three planned output paths")

    source = _regular_file(Path(str(source_claim.get("path"))), "source rootfs")
    source_size = source.stat().st_size
    source_hash = _sha256(source)
    if source_claim.get("size") != source_size or source_claim.get("sha256") != source_hash:
        raise RootfsPlanError("source rootfs changed after the plan was built")
    source_manifest = _regular_file(
        Path(str(source_manifest_claim.get("path"))), "source manifest"
    )
    if source_manifest_claim.get("sha256") != _sha256(source_manifest):
        raise RootfsPlanError("source manifest changed after the plan was built")

    output_rootfs = _safe_output_path(Path(str(planned_outputs["rootfs"])), "output rootfs")
    output_config = _safe_output_path(
        Path(str(planned_outputs["controller_config"])), "output controller config"
    )
    output_manifest = _safe_output_path(
        Path(str(planned_outputs["manifest"])), "output manifest"
    )
    if len({output_rootfs, output_config, output_manifest}) != 3:
        raise RootfsPlanError("materialized output paths must be distinct")
    output_rootfs.parent.mkdir(parents=True, exist_ok=True)
    output_config.parent.mkdir(parents=True, exist_ok=True)
    output_manifest.parent.mkdir(parents=True, exist_ok=True)

    executable = _tool_path(debugfs, "debugfs")
    guest_files = _materialized_guest_files(plan)
    dependency_claim = _validate_controller_dependencies(
        guest_files["/linux-ai-controller"],
        readelf=readelf,
        dependency_manifest=dependency_manifest,
    )
    controller_config = plan.get("controller_config")
    if not isinstance(controller_config, dict):
        raise RootfsPlanError("network-bound controller config is required")
    config_bytes = (
        json.dumps(controller_config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    commands: list[dict[str, Any]] = []
    created_outputs: list[Path] = []
    published_ok = False
    workspace = Path(tempfile.mkdtemp(prefix=".p5-rootfs-materialize-", dir=str(output_rootfs.parent)))
    staged_rootfs = workspace / "linux-ai.ext4"
    staged_config = workspace / "linux-controller.json"
    staged_init = workspace / "init"
    try:
        shutil.copyfile(source, staged_rootfs)
        if _sha256(staged_rootfs) != source_hash:
            raise RootfsPlanError("source rootfs changed while being copied")
        staged_config.write_bytes(config_bytes)
        startup_argv = plan.get("startup_argv")
        if not isinstance(startup_argv, list) or not startup_argv:
            raise RootfsPlanError("materialization requires the qualification startup argv")
        init_script = (
            "#!/bin/sh\n"
            "set -e\n"
            "/bin/busybox mount -t proc proc /proc 2>/dev/null || true\n"
            "/bin/busybox mount -t sysfs sysfs /sys 2>/dev/null || true\n"
            "/bin/busybox mkdir -p /run/tgos\n"
            f"echo 'AXVISOR_LINUX_INIT_ENTER vm=1 boot_id={controller_config['run_id']}'\n"
            f"echo 'AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id={controller_config['run_id']}'\n"
            "exec " + " ".join(shlex.quote(str(item)) for item in startup_argv) + "\n"
        )
        staged_init.write_text(init_script, encoding="utf-8", newline="\n")

        # Directories are created independently so an old directory in the
        # source image is harmless, while all other debugfs failures remain
        # fatal.
        for guest_dir in ("/opt", "/opt/tgos", "/etc", "/etc/tgos"):
            _run_debugfs(
                executable,
                staged_rootfs,
                f"mkdir {guest_dir}",
                commands,
                write=True,
                allow_existing=True,
            )

        local_guest = {
            **guest_files,
            "/etc/tgos/linux-controller.json": staged_config,
            "/init": staged_init,
        }
        guest_source_paths = {
            **{guest_path: str(path) for guest_path, path in guest_files.items()},
            "/etc/tgos/linux-controller.json": str(output_config),
            "/init": "generated:qualification-startup-argv",
        }
        for guest_path, local_path in local_guest.items():
            _run_debugfs(
                executable,
                staged_rootfs,
                f"rm {guest_path}",
                commands,
                write=True,
                allow_existing=True,
            )
            _run_debugfs(
                executable,
                staged_rootfs,
                f"write {local_path} {guest_path}",
                commands,
                write=True,
            )
            if guest_path in ("/linux-ai-controller", "/init"):
                _run_debugfs(
                    executable,
                    staged_rootfs,
                    f"sif {guest_path} mode 0100755",
                    commands,
                    write=True,
                )

        expected_guest = {
            guest_path: _sha256(path)
            for guest_path, path in local_guest.items()
        }
        for index, (guest_path, expected_hash) in enumerate(expected_guest.items()):
            dumped = workspace / f"dump-{index}"
            _run_debugfs(
                executable,
                staged_rootfs,
                f"dump {guest_path} {dumped}",
                commands,
                write=False,
            )
            dumped = _regular_file(dumped, f"debugfs dump {guest_path}")
            if _sha256(dumped) != expected_hash:
                raise RootfsPlanError(f"dump-back hash mismatch for guest file {guest_path}")
            # The dump is retained only inside the disposable workspace; its
            # hash is recorded in the manifest below.

        output_rootfs.parent.mkdir(parents=True, exist_ok=True)
        os.link(staged_rootfs, output_rootfs, follow_symlinks=False)
        created_outputs.append(output_rootfs)
        os.link(staged_config, output_config, follow_symlinks=False)
        created_outputs.append(output_config)

        output_claim = {
            "path": str(output_rootfs),
            "name": output_rootfs.name,
            "size": output_rootfs.stat().st_size,
            "sha256": _sha256(output_rootfs),
        }
        materialized = {
            "schema_version": MATERIALIZED_SCHEMA,
            "run_id": plan["run_id"],
            "session_id": plan["session_id"],
            "controller_mode": plan["controller_mode"],
            "seed": plan["seed"],
            "profile_id": plan["profile_id"],
            "test_id": plan["test_id"],
            "model_version": plan["model_version"],
            "source_rootfs": {
                **source_claim,
                "mutation": "fresh_copy_only",
            },
            "source_manifest": dict(source_manifest_claim),
            "output_rootfs": output_claim,
            "controller_binary": {
                **dict(plan["controller_binary"]),
                "dependency_validation": dependency_claim,
            },
            "model": dict(plan["model"]),
            "network": plan["network"],
            "fault_manifest": plan["fault_manifest"],
            "controller_config": {
                **controller_config,
                "path": str(output_config),
                "sha256": _sha256(output_config),
            },
            "guest_files": {
                guest_path: {
                    "source": guest_source_paths[guest_path],
                    "size": local_path.stat().st_size,
                    "sha256": expected_hash,
                }
                for guest_path, local_path in local_guest.items()
                for expected_hash in (expected_guest[guest_path],)
            },
            "startup_argv": plan["startup_argv"],
            "debugfs_commands": commands,
            "execution_kind": "host_materialization",
            "execution": "completed",
            "qualified": False,
            "runtime_evidence": "not_produced",
            "non_claims": [
                "no Linux Guest, UDP/IP, mediated NIC, or AI-loop evidence was produced",
                "no target build, WSL, QEMU, or controller process was executed",
            ],
        }
        publish_new_file(
            output_manifest,
            (json.dumps(materialized, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
            error_type=RootfsPlanError,
        )
        created_outputs.append(output_manifest)
        published_ok = True
        return materialized
    except OSError as error:
        raise RootfsPlanError(f"rootfs materialization failed: {error}") from error
    finally:
        if not published_ok:
            for path in created_outputs:
                if path.exists():
                    path.unlink()
        shutil.rmtree(workspace, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    repository = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True, type=int)
    parser.add_argument("--source-rootfs", type=Path)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--repository", type=Path, default=repository)
    parser.add_argument("--from-network-session", type=Path)
    parser.add_argument("--controller-binary", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--dataset-manifest", type=Path)
    parser.add_argument("--golden", type=Path)
    parser.add_argument("--model-checksums", type=Path)
    parser.add_argument("--controller-mode", choices=CONTROLLERS, default="mlp")
    parser.add_argument("--seed", choices=SEEDS, type=int, default=43)
    parser.add_argument(
        "--profile", "--qualification-profile",
        dest="profile",
        type=Path,
        default=repository / "configs" / "contest" / "ai" / "qualification-v1.json",
    )
    parser.add_argument(
        "--model-directory",
        type=Path,
        default=repository / "apps" / "contest" / "linux-ai-controller" / "model",
    )
    parser.add_argument("--bind-ip")
    parser.add_argument("--peer-ip")
    parser.add_argument("--udp-port", type=int)
    parser.add_argument("--tcp-port", type=int)
    parser.add_argument("--output-rootfs", type=Path)
    parser.add_argument("--output-controller-config", type=Path)
    parser.add_argument("--output-manifest", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--debugfs", default="debugfs")
    parser.add_argument("--readelf", default="readelf")
    parser.add_argument("--dependency-manifest", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(
            run_id=args.run_id,
            session_id=args.session_id,
            source_rootfs=args.source_rootfs,
            source_manifest=args.source_manifest,
            profile=args.profile,
            model_directory=args.model_directory,
            controller_mode=args.controller_mode,
            seed=args.seed,
            repository=args.repository,
            from_network_session=args.from_network_session,
            controller_binary=args.controller_binary,
            model=args.model,
            metadata=args.metadata,
            dataset_manifest=args.dataset_manifest,
            golden=args.golden,
            model_checksums=args.model_checksums,
            bind_ip=args.bind_ip,
            peer_ip=args.peer_ip,
            udp_port=args.udp_port,
            tcp_port=args.tcp_port,
            output_rootfs=args.output_rootfs,
            output_controller_config=args.output_controller_config,
            output_manifest=args.output_manifest,
        )
        if args.dry_run:
            output_dir = args.output_dir
            if output_dir is None and args.output_manifest is not None:
                output_dir = Path(args.output_manifest).parent
            if output_dir is None:
                raise RootfsPlanError("output-dir or output-manifest is required")
            write_plan(plan, output_dir)
        else:
            materialized = materialize_rootfs(
                plan,
                debugfs=args.debugfs,
                readelf=args.readelf,
                dependency_manifest=args.dependency_manifest,
            )
    except (OSError, RootfsPlanError) as error:
        print(f"P5_ROOTFS_PREPARATION_BLOCKED: {error}")
        return 1
    if args.dry_run:
        print(f"P5_ROOTFS_PLAN_PASS run={args.run_id} qualified=false")
    else:
        print(
            f"P5_ROOTFS_PREPARED_PASS run={args.run_id} "
            f"rootfs_sha256={materialized['output_rootfs']['sha256']} qualified=false"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
