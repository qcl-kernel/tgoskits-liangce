#!/usr/bin/env python3
"""Fail-closed P2 soak provenance binding for fresh P4 network inputs.

The P2 run remains historical evidence: its QEMU identity is never reused as
the identity of a P4 run.  This module proves only that a fresh P4 preparation
started from the byte-bound rootfs/config bundle of one validated 1,800-second
dual-Guest run and records that provenance in a manifest-hashed document.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import sys
import tomllib
from pathlib import Path
from typing import Any, Mapping


CONTEST_DIR = Path(__file__).resolve().parents[1]
if str(CONTEST_DIR) not in sys.path:
    sys.path.insert(0, str(CONTEST_DIR))

from validate_dual_guest_soak_session import (  # noqa: E402
    MIN_DURATION_NS,
    validate_session,
)


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
BOOT_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
PROVENANCE_SCHEMA_VERSION = 1
REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)

REQUIRED_FILES = {
    "session": "dual-guest-soak-session.json",
    "result": "dual-guest-soak-result.json",
    "inputs": "prepared/dual-inputs.json",
    "linuxVmConfig": "prepared/linux.resolved.toml",
    "zephyrVmConfig": "prepared/zephyr.resolved.toml",
    "qemuConfig": "prepared/qemu.no-dataplane.toml",
    "linuxRootfs": "prepared/linux-console-rootfs.ext4",
    "linuxRootfsPlan": "prepared/linux-rootfs-plan.json",
    "zephyrBuildManifest": "prepared/zephyr-build-manifest.json",
    "liveStatus": "live/status.json",
}

PROVENANCE_FIELDS = {
    "schemaVersion",
    "artifactStatus",
    "status",
    "proofScope",
    "doesNotProve",
    "sourceRun",
    "bindingSha256",
    "sourceIdentity",
    "durationNs",
    "cpuSets",
    "historicalOuterNicExplicitlyDisabled",
    "reusableLinuxSourceRootfs",
    "historicalRuntimeRootfs",
    "artifacts",
}

PROVENANCE_DOES_NOT_PROVE = [
    "the fresh P4 inputs were built",
    "the current c82 AxVisor build succeeded",
    "fresh Linux or Zephyr Guest enumeration",
    "Guest IP connectivity",
    "DMA isolation",
]


class SoakBindingError(ValueError):
    """The selected P2 run is not a complete, byte-bound soak source."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SoakBindingError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _json(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, SoakBindingError) as error:
        raise SoakBindingError(
            f"{label} is not duplicate-free UTF-8 JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise SoakBindingError(f"{label} must be a JSON object")
    return value


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SoakBindingError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise SoakBindingError(f"{label} keys must be exactly {sorted(expected)!r}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_reparse(path: Path) -> bool:
    return bool(int(getattr(path.lstat(), "st_file_attributes", 0)) & REPARSE_FLAG)


def _read_file(root: Path, relative: str, *, label: str) -> tuple[Path, bytes]:
    candidate = root.joinpath(*relative.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise SoakBindingError(f"{label} escapes or is missing from the soak run") from error
    current = candidate
    while True:
        if current.is_symlink() or _is_reparse(current):
            raise SoakBindingError(f"{label} must not use a symlink or reparse point")
        if current == root:
            break
        current = current.parent
    if not candidate.is_file():
        raise SoakBindingError(f"{label} must be a regular file")
    return candidate, candidate.read_bytes()


def _claim(
    value: Any,
    *,
    data: bytes,
    expected_name: str,
    label: str,
) -> None:
    claim = _mapping(value, label=label)
    _exact_keys(claim, {"path", "size", "sha256"}, label=label)
    path = claim["path"]
    if not isinstance(path, str) or Path(path).name != expected_name:
        raise SoakBindingError(f"{label}.path must end in {expected_name!r}")
    if (
        not isinstance(claim["size"], int)
        or isinstance(claim["size"], bool)
        or claim["size"] <= 0
        or not isinstance(claim["sha256"], str)
        or SHA256_RE.fullmatch(claim["sha256"]) is None
    ):
        raise SoakBindingError(f"{label} is not a valid byte claim")
    if claim["size"] != len(data) or claim["sha256"] != _sha256(data):
        raise SoakBindingError(f"{label} is not byte-bound to {expected_name}")


def _source_claim(value: Any, *, label: str) -> None:
    claim = _mapping(value, label=label)
    _exact_keys(claim, {"path", "sha256"}, label=label)
    if not isinstance(claim["path"], str) or not claim["path"]:
        raise SoakBindingError(f"{label}.path must be non-empty")
    if not isinstance(claim["sha256"], str) or SHA256_RE.fullmatch(claim["sha256"]) is None:
        raise SoakBindingError(f"{label}.sha256 must be lowercase SHA-256")


def _claim_value(value: Any, *, expected_name: str, label: str) -> dict[str, Any]:
    claim = _mapping(value, label=label)
    _exact_keys(claim, {"path", "size", "sha256"}, label=label)
    if not isinstance(claim["path"], str) or Path(claim["path"]).name != expected_name:
        raise SoakBindingError(f"{label}.path must end in {expected_name!r}")
    if (
        not isinstance(claim["size"], int)
        or isinstance(claim["size"], bool)
        or claim["size"] <= 0
        or not isinstance(claim["sha256"], str)
        or SHA256_RE.fullmatch(claim["sha256"]) is None
    ):
        raise SoakBindingError(f"{label} is not a valid byte claim")
    return dict(claim)


def _validate_provenance_claim(
    value: Any,
    *,
    expected_name: str,
    expected_path: str | None = None,
    label: str,
    require_size: bool = True,
) -> dict[str, Any]:
    claim = _mapping(value, label=label)
    _exact_keys(claim, {"path", "size", "sha256"}, label=label)
    if (
        not isinstance(claim["path"], str)
        or Path(claim["path"]).name != expected_name
        or (expected_path is not None and claim["path"] != expected_path)
    ):
        raise SoakBindingError(f"{label}.path must end in {expected_name!r}")
    if (
        not isinstance(claim["size"], int)
        or isinstance(claim["size"], bool)
        or (require_size and claim["size"] <= 0)
        or not isinstance(claim["sha256"], str)
        or SHA256_RE.fullmatch(claim["sha256"]) is None
    ):
        raise SoakBindingError(f"{label} is not a valid byte claim")
    return dict(claim)


def _validate_provenance_document(provenance: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the generated P2 provenance shape and all derived claims.

    The validator is deliberately independent of a source directory.  It is
    used by the offline P4 qualifier to prove that the binding digest is
    recomputed from the embedded claims, while :func:`validate_soak_binding`
    remains the stronger check that reads the original P2 bytes.
    """

    document = _mapping(provenance, label="soak provenance")
    _exact_keys(document, PROVENANCE_FIELDS, label="soak provenance")
    if (
        document["schemaVersion"] != PROVENANCE_SCHEMA_VERSION
        or document["artifactStatus"] != "validated-source-provenance"
        or document["status"] != "p2_soak_input_bound"
        or document["proofScope"] != "p2-soak-provenance-for-fresh-p4-inputs"
    ):
        raise SoakBindingError("soak provenance has an unsupported status or proof scope")
    if document["doesNotProve"] != PROVENANCE_DOES_NOT_PROVE:
        raise SoakBindingError("soak provenance negative proof scope is not canonical")
    source_run = document["sourceRun"]
    if (
        not isinstance(source_run, str)
        or Path(source_run).name != source_run
        or BOOT_ID_RE.fullmatch(source_run) is None
    ):
        raise SoakBindingError("soak provenance sourceRun is not a safe run identity")
    binding = document["bindingSha256"]
    if not isinstance(binding, str) or SHA256_RE.fullmatch(binding) is None:
        raise SoakBindingError("soak provenance bindingSha256 is not lowercase SHA-256")

    identity = _mapping(document["sourceIdentity"], label="soak provenance sourceIdentity")
    _exact_keys(
        identity,
        {"bootId", "qemuPid", "qemuStartMonotonicNs", "qemuName", "sessionNonce"},
        label="soak provenance sourceIdentity",
    )
    if not isinstance(identity["bootId"], str) or BOOT_ID_RE.fullmatch(identity["bootId"]) is None:
        raise SoakBindingError("soak provenance sourceIdentity.bootId is unsafe")
    if not isinstance(identity["sessionNonce"], str) or NONCE_RE.fullmatch(identity["sessionNonce"]) is None:
        raise SoakBindingError("soak provenance sourceIdentity.sessionNonce is invalid")
    if identity["qemuName"] != f"axvisor-dual-soak-{identity['sessionNonce']}":
        raise SoakBindingError("soak provenance sourceIdentity.qemuName is not nonce-bound")
    for key in ("qemuPid", "qemuStartMonotonicNs"):
        if (
            not isinstance(identity[key], int)
            or isinstance(identity[key], bool)
            or identity[key] <= 0
        ):
            raise SoakBindingError(f"soak provenance sourceIdentity.{key} is invalid")

    duration = document["durationNs"]
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < MIN_DURATION_NS:
        raise SoakBindingError("soak provenance duration is shorter than 1,800 seconds")
    cpu_sets = _mapping(document["cpuSets"], label="soak provenance cpuSets")
    _exact_keys(cpu_sets, {"linux", "zephyr"}, label="soak provenance cpuSets")
    cpu_values: dict[str, list[int]] = {}
    for guest in ("linux", "zephyr"):
        values = cpu_sets[guest]
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(cpu, int) or isinstance(cpu, bool) or cpu < 0 for cpu in values)
            or len(set(values)) != len(values)
        ):
            raise SoakBindingError(f"soak provenance cpuSets.{guest} is invalid")
        cpu_values[guest] = list(values)
    if set(cpu_values["linux"]).intersection(cpu_values["zephyr"]):
        raise SoakBindingError("soak provenance cpuSets overlap")
    if not isinstance(document["historicalOuterNicExplicitlyDisabled"], bool):
        raise SoakBindingError("soak provenance outer-NIC observation must be boolean")

    reusable_source = _validate_provenance_claim(
        document["reusableLinuxSourceRootfs"],
        expected_name="rootfs.img",
        expected_path="rootfs.img",
        label="soak provenance reusableLinuxSourceRootfs",
    )
    historical = _mapping(
        document["historicalRuntimeRootfs"],
        label="soak provenance historicalRuntimeRootfs",
    )
    _exact_keys(
        historical,
        {"launchClaim", "currentFile", "currentMatchesLaunchClaim", "reusable"},
        label="soak provenance historicalRuntimeRootfs",
    )
    launch = _validate_provenance_claim(
        historical["launchClaim"],
        expected_name="linux-console-rootfs.ext4",
        expected_path="linux-console-rootfs.ext4",
        label="soak provenance historicalRuntimeRootfs.launchClaim",
    )
    current = _validate_provenance_claim(
        historical["currentFile"],
        expected_name="linux-console-rootfs.ext4",
        expected_path="prepared/linux-console-rootfs.ext4",
        label="soak provenance historicalRuntimeRootfs.currentFile",
    )
    matches = (
        launch["size"] == current["size"]
        and launch["sha256"] == current["sha256"]
    )
    if historical["currentMatchesLaunchClaim"] is not matches:
        raise SoakBindingError(
            "soak provenance historical rootfs match flag disagrees with byte claims"
        )
    if historical["reusable"] is not False:
        raise SoakBindingError("historical launched rootfs must remain non-reusable")

    artifacts = _mapping(document["artifacts"], label="soak provenance artifacts")
    _exact_keys(artifacts, set(REQUIRED_FILES), label="soak provenance artifacts")
    claims = {
        name: _validate_provenance_claim(
            artifacts[name],
            expected_name=Path(relative).name,
            expected_path=relative,
            label=f"soak provenance artifacts.{name}",
        )
        for name, relative in REQUIRED_FILES.items()
    }
    return {
        "document": dict(document),
        "sourceRun": source_run,
        "sourceIdentity": dict(identity),
        "durationNs": duration,
        "cpuSets": cpu_values,
        "reusableLinuxSourceRootfs": reusable_source,
        "historicalRuntimeRootfs": {
            "launchClaim": launch,
            "currentFile": current,
            "currentMatchesLaunchClaim": matches,
            "reusable": False,
        },
        "artifacts": claims,
    }


def _binding_digest_from_validated(validated: Mapping[str, Any]) -> str:
    claims = validated["artifacts"]
    payload = {
        "sourceRun": validated["sourceRun"],
        "sourceSessionSha256": claims["session"]["sha256"],
        "sourceResultSha256": claims["result"]["sha256"],
        "sourceInputsSha256": claims["inputs"]["sha256"],
        "sourceRootfsSha256": validated["reusableLinuxSourceRootfs"]["sha256"],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def recompute_binding_sha256(provenance: Mapping[str, Any]) -> str:
    """Recompute the canonical P2 binding digest from provenance claims.

    This function intentionally does not trust ``bindingSha256``.  It parses
    the complete claim set first and then hashes the same canonical payload
    used by :func:`validate_soak_binding`.
    """

    return _binding_digest_from_validated(_validate_provenance_document(provenance))


def validate_provenance_document(provenance: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a manifest-bound provenance document and its derived digest."""

    validated = _validate_provenance_document(provenance)
    expected = _binding_digest_from_validated(validated)
    if validated["document"]["bindingSha256"] != expected:
        raise SoakBindingError(
            "soak provenance bindingSha256 does not match its canonical claims"
        )
    return validated["document"]


def validate_provenance_against_source(
    provenance: Mapping[str, Any], source_root: Path
) -> dict[str, Any]:
    """Revalidate embedded provenance against the immutable P2 source bytes.

    The source is validated afresh with :func:`validate_soak_binding`; the
    embedded document must then equal that freshly generated canonical result.
    This closes the gap between a self-consistent forged claim set and the
    actual P2 evidence directory.
    """

    embedded = validate_provenance_document(provenance)
    expected = validate_soak_binding(source_root)
    if embedded != expected:
        raise SoakBindingError(
            "embedded soak provenance differs from freshly validated P2 source"
        )
    return expected


def _validate_result(
    result: Mapping[str, Any],
    *,
    validated: Mapping[str, Any],
    session_data: bytes,
    linux_data: bytes,
    zephyr_data: bytes,
) -> None:
    _exact_keys(
        result,
        {
            "schemaVersion",
            "artifactStatus",
            "status",
            "proofScope",
            "doesNotProve",
            "durationNs",
            "guests",
            "cpuSets",
            "sessionIdentity",
            "sources",
        },
        label="dual-guest-soak-result.json",
    )
    if (
        result["schemaVersion"] != 1
        or result["artifactStatus"] != "observation-derived"
        or result["status"] != "dual_guest_30min_coexistence_observed"
        or result["proofScope"]
        != "one-identity-bound-qemu-dual-guest-1800-second-coexistence-session"
    ):
        raise SoakBindingError("soak result has an unsupported status or proof scope")
    for key, validated_key in (
        ("durationNs", "durationNs"),
        ("cpuSets", "cpuSets"),
        ("guests", "guests"),
        ("sessionIdentity", "identity"),
    ):
        if result[key] != validated[validated_key]:
            raise SoakBindingError(f"soak result {key} disagrees with the validated session")
    sources = _mapping(result["sources"], label="soak result sources")
    _exact_keys(
        sources,
        {"session", "linuxVmConfig", "zephyrVmConfig"},
        label="soak result sources",
    )
    _claim(
        sources["session"],
        data=session_data,
        expected_name="dual-guest-soak-session.json",
        label="soak result session source",
    )
    _claim(
        sources["linuxVmConfig"],
        data=linux_data,
        expected_name="linux.resolved.toml",
        label="soak result Linux config source",
    )
    _claim(
        sources["zephyrVmConfig"],
        data=zephyr_data,
        expected_name="zephyr.resolved.toml",
        label="soak result Zephyr config source",
    )


def _validate_prepared_inputs(
    inputs: Mapping[str, Any],
    *,
    boot_id: str,
    artifacts: Mapping[str, bytes],
) -> dict[str, dict[str, Any]]:
    _exact_keys(
        inputs,
        {
            "schemaVersion",
            "artifactStatus",
            "status",
            "proofScope",
            "doesNotProve",
            "bootId",
            "sources",
            "outputs",
            "resolvedZephyr",
            "init",
        },
        label="prepared/dual-inputs.json",
    )
    if (
        inputs["schemaVersion"] != 1
        or inputs["artifactStatus"] != "prepared-inputs-only"
        or inputs["status"] != "dual_guest_smoke_inputs_prepared"
        or inputs["bootId"] != boot_id
    ):
        raise SoakBindingError("prepared inputs do not match the soak boot identity")
    sources = _mapping(inputs["sources"], label="prepared input sources")
    _exact_keys(
        sources,
        {"linuxKernel", "sourceRootfs", "zephyrBinary"},
        label="prepared input sources",
    )
    for name, claim in sources.items():
        _source_claim(claim, label=f"prepared input source {name}")

    outputs = _mapping(inputs["outputs"], label="prepared outputs")
    expected_outputs = {
        "linux.resolved.toml": "linuxVmConfig",
        "zephyr.resolved.toml": "zephyrVmConfig",
        "qemu.no-dataplane.toml": "qemuConfig",
        "linux-console-rootfs.ext4": "linuxRootfs",
        "linux-rootfs-plan.json": "linuxRootfsPlan",
        "zephyr-build-manifest.json": "zephyrBuildManifest",
    }
    _exact_keys(outputs, set(expected_outputs), label="prepared outputs")
    for filename, artifact_name in expected_outputs.items():
        if artifact_name == "linuxRootfs":
            continue
        _claim(
            outputs[filename],
            data=artifacts[artifact_name],
            expected_name=filename,
            label=f"prepared output {filename}",
        )
    launched_rootfs = _claim_value(
        outputs["linux-console-rootfs.ext4"],
        expected_name="linux-console-rootfs.ext4",
        label="prepared output linux-console-rootfs.ext4",
    )
    rootfs_plan = _json(
        artifacts["linuxRootfsPlan"], label="prepared/linux-rootfs-plan.json"
    )
    if (
        rootfs_plan.get("schemaVersion") != 1
        or rootfs_plan.get("artifactStatus") != "prepared-rootfs-only"
        or rootfs_plan.get("status") != "dual_guest_linux_rootfs_prepared"
        or rootfs_plan.get("bootId") != boot_id
    ):
        raise SoakBindingError("Linux rootfs plan does not match the soak boot identity")
    planned_output = _mapping(
        rootfs_plan.get("outputRootfs"), label="Linux rootfs plan output"
    )
    planned_output_core = {
        key: planned_output.get(key) for key in ("path", "size", "sha256")
    }
    if planned_output_core != launched_rootfs:
        raise SoakBindingError("Linux launch rootfs claim disagrees with its plan")
    source_rootfs = _claim_value(
        rootfs_plan.get("sourceRootfs"),
        expected_name="rootfs.img",
        label="Linux rootfs plan source",
    )
    prepared_source = _mapping(
        sources["sourceRootfs"], label="prepared input source sourceRootfs"
    )
    if (
        Path(str(prepared_source["path"])).name != source_rootfs["path"]
        or prepared_source["sha256"] != source_rootfs["sha256"]
    ):
        raise SoakBindingError("Linux reusable source rootfs disagrees across manifests")
    if inputs["resolvedZephyr"] != {
        "vmId": 2,
        "cpuNum": 1,
        "physCpuIds": [2],
    }:
        raise SoakBindingError("prepared inputs do not preserve VM2/[2] ownership")
    init = _mapping(inputs["init"], label="prepared init")
    _exact_keys(init, {"size", "sha256"}, label="prepared init")
    if (
        not isinstance(init["size"], int)
        or isinstance(init["size"], bool)
        or init["size"] <= 0
        or not isinstance(init["sha256"], str)
        or SHA256_RE.fullmatch(init["sha256"]) is None
    ):
        raise SoakBindingError("prepared init claim is invalid")
    return {
        "launchedRootfs": launched_rootfs,
        "reusableSourceRootfs": source_rootfs,
    }


def _validate_qemu_args(data: bytes, *, require_nic_none: bool, label: str) -> bool:
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SoakBindingError(f"{label} is not UTF-8 TOML: {error}") from error
    args = document.get("args")
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise SoakBindingError(f"{label}.args must be a string array")
    nic_none = False
    for index, arg in enumerate(args):
        lowered = arg.lower()
        if lowered in {"-net", "-netdev"} or lowered.startswith("-netdev="):
            raise SoakBindingError(f"{label} enables a forbidden outer network backend")
        if lowered == "-nic":
            value = args[index + 1].lower() if index + 1 < len(args) else ""
            if value != "none":
                raise SoakBindingError(f"{label} must use only '-nic none'")
            nic_none = True
        elif lowered.startswith("-nic="):
            if lowered != "-nic=none":
                raise SoakBindingError(f"{label} must use only '-nic none'")
            nic_none = True
    if require_nic_none and not nic_none:
        raise SoakBindingError(f"{label} must explicitly disable the outer NIC")
    return nic_none


def validate_current_qemu_config(path: Path) -> dict[str, Any]:
    """Validate the current P4 QEMU file, which must explicitly use -nic none."""
    path = Path(path)
    if path.is_symlink() or _is_reparse(path) or not path.is_file():
        raise SoakBindingError("current QEMU config must be a regular non-link file")
    data = path.read_bytes()
    _validate_qemu_args(data, require_nic_none=True, label="current QEMU config")
    return {"path": path.name, "size": len(data), "sha256": _sha256(data)}


def _validate_live_status(
    status: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    artifacts: Mapping[str, bytes],
    launched_rootfs: Mapping[str, Any],
) -> None:
    if (
        status.get("schemaVersion") != 1
        or status.get("artifactStatus") != "run-complete"
        or status.get("status") != "dual_guest_short_smoke_completed"
        or status.get("success") is not True
        or status.get("error") is not None
    ):
        raise SoakBindingError("live status is not a completed successful P2 run")
    state = _mapping(status.get("state"), label="live status state")
    for key in ("bootId", "qemuName", "sessionNonce", "qemuStartMonotonicNs"):
        if state.get(key) != identity[key]:
            raise SoakBindingError(f"live status {key} disagrees with soak identity")
    ready_identity = _mapping(state.get("readyIdentity"), label="live ready identity")
    if ready_identity.get("pid") != identity["qemuPid"]:
        raise SoakBindingError("live QEMU PID disagrees with soak identity")

    inputs = _mapping(state.get("inputArtifacts"), label="live input artifacts")
    live_names = {
        "qemuConfig": "qemuConfig",
        "linuxVmconfig": "linuxVmConfig",
        "zephyrVmconfig": "zephyrVmConfig",
    }
    for claim_name, artifact_name in live_names.items():
        _claim(
            inputs.get(claim_name),
            data=artifacts[artifact_name],
            expected_name=REQUIRED_FILES[artifact_name].split("/")[-1],
            label=f"live input {claim_name}",
        )
    live_rootfs = _claim_value(
        inputs.get("linuxRootfs"),
        expected_name="linux-console-rootfs.ext4",
        label="live input linuxRootfs",
    )
    if (
        Path(str(live_rootfs["path"])).name
        != Path(str(launched_rootfs["path"])).name
        or live_rootfs["size"] != launched_rootfs["size"]
        or live_rootfs["sha256"] != launched_rootfs["sha256"]
    ):
        raise SoakBindingError("live Linux rootfs claim disagrees with prepared inputs")
    resolved = _mapping(state.get("resolvedVmConfigs"), label="resolved VM configs")
    if resolved.get("linux") != {"vmId": 1, "cpuNum": 2, "physCpuIds": [0, 1]}:
        raise SoakBindingError("live Linux VM ownership is not VM1/[0,1]")
    if resolved.get("zephyr") != {"vmId": 2, "cpuNum": 1, "physCpuIds": [2]}:
        raise SoakBindingError("live Zephyr VM ownership is not VM2/[2]")
    window = _mapping(state.get("stabilityWindow"), label="stability window")
    start = window.get("startMonotonicNs")
    end = window.get("endMonotonicNs")
    duration = window.get("durationSeconds")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or end - start < MIN_DURATION_NS
        or not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration < 1800
    ):
        raise SoakBindingError("live stability window is shorter than 1,800 seconds")
    ready = _mapping(state.get("dualGuestReady"), label="live dual-Guest readiness")
    if ready.get("bootId") != identity["bootId"]:
        raise SoakBindingError("live dual-Guest readiness has a different boot id")


def validate_soak_binding(run_root: Path) -> dict[str, Any]:
    """Validate one immutable P2 run and return canonical P4 provenance."""
    supplied_root = Path(run_root)
    try:
        root = supplied_root.resolve(strict=True)
    except OSError as error:
        raise SoakBindingError(f"soak run does not exist: {supplied_root}") from error
    if supplied_root.is_symlink() or _is_reparse(supplied_root) or not root.is_dir():
        raise SoakBindingError("soak run must be a regular non-link directory")

    paths: dict[str, Path] = {}
    artifacts: dict[str, bytes] = {}
    for name, relative in REQUIRED_FILES.items():
        paths[name], artifacts[name] = _read_file(root, relative, label=name)

    session = _json(artifacts["session"], label="dual-guest-soak-session.json")
    validated = validate_session(
        session,
        linux_config=paths["linuxVmConfig"],
        linux_bytes=artifacts["linuxVmConfig"],
        zephyr_config=paths["zephyrVmConfig"],
        zephyr_bytes=artifacts["zephyrVmConfig"],
    )
    result = _json(artifacts["result"], label="dual-guest-soak-result.json")
    _validate_result(
        result,
        validated=validated,
        session_data=artifacts["session"],
        linux_data=artifacts["linuxVmConfig"],
        zephyr_data=artifacts["zephyrVmConfig"],
    )
    prepared_inputs = _json(artifacts["inputs"], label="prepared/dual-inputs.json")
    rootfs_binding = _validate_prepared_inputs(
        prepared_inputs,
        boot_id=str(validated["identity"]["bootId"]),
        artifacts=artifacts,
    )
    live_status = _json(artifacts["liveStatus"], label="live/status.json")
    _validate_live_status(
        live_status,
        identity=validated["identity"],
        artifacts=artifacts,
        launched_rootfs=rootfs_binding["launchedRootfs"],
    )
    historical_nic_none = _validate_qemu_args(
        artifacts["qemuConfig"],
        require_nic_none=False,
        label="historical QEMU config",
    )

    file_claims = {
        name: {
            "path": REQUIRED_FILES[name],
            "size": len(data),
            "sha256": _sha256(data),
        }
        for name, data in artifacts.items()
    }
    binding_payload = {
        "sourceRun": root.name,
        "sourceSessionSha256": file_claims["session"]["sha256"],
        "sourceResultSha256": file_claims["result"]["sha256"],
        "sourceInputsSha256": file_claims["inputs"]["sha256"],
        "sourceRootfsSha256": rootfs_binding["reusableSourceRootfs"]["sha256"],
    }
    binding_sha256 = hashlib.sha256(
        json.dumps(binding_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schemaVersion": PROVENANCE_SCHEMA_VERSION,
        "artifactStatus": "validated-source-provenance",
        "status": "p2_soak_input_bound",
        "proofScope": "p2-soak-provenance-for-fresh-p4-inputs",
        "doesNotProve": list(PROVENANCE_DOES_NOT_PROVE),
        "sourceRun": root.name,
        "bindingSha256": binding_sha256,
        "sourceIdentity": validated["identity"],
        "durationNs": validated["durationNs"],
        "cpuSets": validated["cpuSets"],
        "historicalOuterNicExplicitlyDisabled": historical_nic_none,
        "reusableLinuxSourceRootfs": rootfs_binding["reusableSourceRootfs"],
        "historicalRuntimeRootfs": {
            "launchClaim": rootfs_binding["launchedRootfs"],
            "currentFile": file_claims["linuxRootfs"],
            "currentMatchesLaunchClaim": (
                file_claims["linuxRootfs"]["size"]
                == rootfs_binding["launchedRootfs"]["size"]
                and file_claims["linuxRootfs"]["sha256"]
                == rootfs_binding["launchedRootfs"]["sha256"]
            ),
            "reusable": False,
        },
        "artifacts": file_claims,
    }


def require_matching_binding(manifest: Mapping[str, Any], binding: Mapping[str, Any], *, label: str) -> None:
    """Require a prepared-image manifest to carry this exact source binding."""
    source = _mapping(manifest.get("sourceSoak"), label=f"{label}.sourceSoak")
    if source != binding:
        raise SoakBindingError(f"{label} is not bound to the selected P2 soak run")
