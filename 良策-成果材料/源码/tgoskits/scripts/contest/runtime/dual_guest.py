#!/usr/bin/env python3
"""Shared fail-closed runtime core for the f964 dual-Guest runners.

This module owns the host-side facts that must be identical for P2 and P4:
QEMU identity injection, official final-DTB evidence parsing, ``[VM n]``
console attribution, READY/drop/panic checks, bounded process cleanup and
status-last publication.  It deliberately does not define a network oracle.

The P2 entry point uses the no-data-plane validators in this file.  The P4
runner can adopt the same helpers without importing another script's private
functions.  Static callers never launch a process merely by importing this
module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:
    from ..host_vm_carveout_io import publish_new_file
except ImportError:  # pragma: no cover - direct module loading
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from host_vm_carveout_io import publish_new_file  # type: ignore[no-redef]


RUN_SCHEMA_VERSION = 1
EXPECTED_GUESTS: dict[int, str] = {1: "linux", 2: "zephyr"}
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
SOAK_SESSION_SCHEMA = "p2-f964-dual-guest-soak-v1"
SOAK_SESSION_STATUS = "dual_guest_soak_session_completed"
SOAK_MIN_DURATION_NS = 1_800 * 1_000_000_000

# The upstream axvm line is the only accepted final-DTB oracle on f964.
_DTB_EVIDENCE_RE = re.compile(
    r"contest dtb evidence:\s*"
    r"vm=(?P<vm>[0-9]+)\s+"
    r"gpa=0x(?P<gpa>[0-9a-fA-F]+)\s+"
    r"size=0x(?P<size>[0-9a-fA-F]+)\s+"
    r"hpa=0x(?P<hpa>[0-9a-fA-F]+)"
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_VM_CONSOLE_RE = re.compile(r"^\s*\[VM\s+(?P<vm>[0-9]+)\]\s?(?P<payload>.*)$")
_READY_RE = {
    1: re.compile(
        r"^AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id=(?P<boot>[A-Za-z0-9._-]{1,64})$"
    ),
    2: re.compile(
        r"^AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=(?P<boot>[A-Za-z0-9._-]{1,64})$"
    ),
}

_LEGACY_MARKERS = (
    "AXVISOR_GUEST_DTB_READY",
    "AXVISOR_GUEST_CONSOLE_FRAME",
)
_FAILURE_RE = re.compile(
    r"(?:AXVISOR_[A-Z0-9_]+_FAIL|kernel\s+panic|\bpanic(?:ked)?\b|"
    r"ESR_EL2:|ELR_EL2:|FAR_EL2:|\bunsafe\b|unclassified\s+exit)",
    re.IGNORECASE,
)

# These are the current AxVisor host/Guest-console forms plus the P4 switch
# counters.  Zero is a valid observation; any positive value is a hard fail.
_DROP_RES = (
    re.compile(
        r"\[Axvisor\s+VM\s+(?P<vm>[0-9]+)\s+console\s+dropped\s+"
        r"(?P<count>[0-9]+)\s+buffered\s+bytes\]",
        re.IGNORECASE,
    ),
    re.compile(
        r"\[Axvisor\s+host\s+console\s+dropped\s+"
        r"(?P<count>[0-9]+)\s+queued\s+bytes\]",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:capture_drops|dropped_bytes|dropped|full_drop|inactive_drop|"
        r"ingress_full_drop|inactive_target_drop|dma)\s*[=:]\s*"
        r"(?P<count>[0-9]+)\b",
        re.IGNORECASE,
    ),
)


class RuntimeContractError(ValueError):
    """An input, identity, runtime log or publication violates the contract."""


@dataclass(frozen=True)
class DtbEvidence:
    """The allocator-derived final-DTB mapping emitted by axvm."""

    vm_id: int
    gpa: int
    size: int
    hpa: int


@dataclass(frozen=True)
class GuestConsoleLine:
    """One line with an explicit AxVisor ``[VM n]`` attribution."""

    vm_id: int
    guest: str
    payload: str


@dataclass(frozen=True)
class DualGuestObservation:
    """Validated identity and console facts for one dual-Guest log."""

    dtb: Mapping[int, DtbEvidence]
    console: tuple[GuestConsoleLine, ...]
    ready: Mapping[int, str]
    drop_summaries: tuple[str, ...]


@dataclass(frozen=True)
class DualGuestSpec:
    """Inputs shared by the P2 thin entry and the future P4 adapter."""

    repository: Path
    build_config: Path
    qemu_config: Path
    linux_vmconfig: Path
    zephyr_vmconfig: Path
    run_id: str
    output_dir: Path
    duration_seconds: float = 300.0
    ready_timeout_seconds: float = 300.0
    shutdown_timeout_seconds: float = 30.0
    runtime_root: Path = Path("/tmp")


@dataclass(frozen=True)
class RuntimeIdentity:
    """Fresh process/QMP identity owned by one runner invocation."""

    nonce: str
    qemu_name: str
    runtime_dir: Path
    qmp_socket: Path
    qemu_pidfile: Path
    live_log: Path


@dataclass(frozen=True)
class DualGuestSoakEvidence:
    """Immutable facts needed to publish one current-f964 soak session."""

    run_id: str
    identity: RuntimeIdentity
    observation: DualGuestObservation
    launcher_pid: int
    launcher_start_monotonic_ns: int
    start_monotonic_ns: int
    end_monotonic_ns: int
    raw_log: Path
    resolved_configs: Mapping[str, Path]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> Path:
    if not path.is_file() or path.is_symlink():
        raise RuntimeContractError(f"{label} must be a regular non-link file: {path}")
    return path


def _strip_ansi(line: str) -> str:
    return _ANSI_RE.sub("", line)


def _require_run_id(run_id: str) -> None:
    if not RUN_ID_RE.fullmatch(run_id):
        raise RuntimeContractError("run-id must match [A-Za-z0-9._-]{1,64}")


def _require_nonce(nonce: str) -> None:
    if not NONCE_RE.fullmatch(nonce):
        raise RuntimeContractError("nonce must be exactly 32 lowercase hexadecimal characters")


def parse_dtb_evidence(
    log_text: str,
    *,
    expected_vm_ids: Iterable[int] = (1, 2),
    require_all: bool = True,
) -> dict[int, DtbEvidence]:
    """Parse unique official ``contest dtb evidence`` lines.

    ``hpa`` is intentionally taken from the log rather than guessed.  A
    duplicate, unexpected VM, malformed positive address or missing required
    VM is rejected; the old ``AXVISOR_GUEST_DTB_READY`` marker is never used.
    """

    expected = tuple(expected_vm_ids)
    found: dict[int, DtbEvidence] = {}
    for line in log_text.splitlines():
        match = _DTB_EVIDENCE_RE.search(_strip_ansi(line))
        if match is None:
            continue
        vm_id = int(match.group("vm"))
        if vm_id not in expected:
            raise RuntimeContractError(f"unexpected DTB evidence VM {vm_id}")
        if vm_id in found:
            raise RuntimeContractError(f"duplicate DTB evidence for VM {vm_id}")
        values = {
            key: int(match.group(key), 16)
            for key in ("gpa", "size", "hpa")
        }
        if any(value <= 0 for value in values.values()):
            raise RuntimeContractError(f"non-positive DTB evidence for VM {vm_id}")
        found[vm_id] = DtbEvidence(vm_id=vm_id, **values)
    if require_all:
        missing = [str(vm_id) for vm_id in expected if vm_id not in found]
        if missing:
            raise RuntimeContractError(
                "missing official contest dtb evidence for VM " + ", ".join(missing)
            )
    return found


def _parse_console_lines(log_text: str) -> tuple[GuestConsoleLine, ...]:
    lines: list[GuestConsoleLine] = []
    for raw_line in log_text.splitlines():
        line = _strip_ansi(raw_line)
        match = _VM_CONSOLE_RE.match(line)
        if match is None:
            # Host text containing a current READY marker is not Guest
            # evidence.  Requiring [VM n] here prevents log spoofing.
            if "AXVISOR_DUAL_GUEST_" in line:
                raise RuntimeContractError("READY marker is not attributed to [VM n]")
            continue
        vm_id = int(match.group("vm"))
        if vm_id not in EXPECTED_GUESTS:
            raise RuntimeContractError(f"unexpected [VM {vm_id}] console identity")
        lines.append(
            GuestConsoleLine(
                vm_id=vm_id,
                guest=EXPECTED_GUESTS[vm_id],
                payload=match.group("payload").strip(),
            )
        )
    return tuple(lines)


def _parse_ready(
    lines: Iterable[GuestConsoleLine], *, run_id: str, require_all: bool = True
) -> dict[int, str]:
    ready: dict[int, str] = {}
    for line in lines:
        if "AXVISOR_DUAL_GUEST_" not in line.payload:
            continue
        parser = _READY_RE.get(line.vm_id)
        if parser is None:
            raise RuntimeContractError(f"READY marker has no VM parser: {line.vm_id}")
        match = parser.fullmatch(line.payload)
        if match is None:
            if "READY" in line.payload:
                raise RuntimeContractError(
                    f"wrong VM/name or malformed READY on [VM {line.vm_id}]: {line.payload}"
                )
            continue
        boot_id = match.group("boot")
        if boot_id != run_id:
            raise RuntimeContractError(
                f"VM {line.vm_id} READY boot-id {boot_id!r} does not match run-id {run_id!r}"
            )
        if line.vm_id in ready:
            raise RuntimeContractError(f"duplicate READY marker for VM {line.vm_id}")
        ready[line.vm_id] = line.payload
    if require_all:
        missing = [str(vm_id) for vm_id in EXPECTED_GUESTS if vm_id not in ready]
        if missing:
            raise RuntimeContractError(
                "missing unique dual-Guest READY marker for VM " + ", ".join(missing)
            )
    return ready


def _parse_drop_summaries(log_text: str) -> tuple[str, ...]:
    summaries: list[str] = []
    for raw_line in log_text.splitlines():
        line = _strip_ansi(raw_line)
        for pattern in _DROP_RES:
            for match in pattern.finditer(line):
                count = int(match.group("count"))
                summaries.append(line.strip())
                if count:
                    raise RuntimeContractError(
                        f"console/data-plane drop or DMA count is non-zero: {line.strip()}"
                    )
    return tuple(summaries)


def _reject_forbidden_runtime_text(log_text: str) -> None:
    legacy = [marker for marker in _LEGACY_MARKERS if marker in log_text]
    if legacy:
        raise RuntimeContractError(
            "legacy f964-incompatible marker(s) observed: " + ", ".join(legacy)
        )
    match = _FAILURE_RE.search(log_text)
    if match is not None:
        raise RuntimeContractError(f"forbidden runtime failure marker: {match.group(0)!r}")


def validate_dual_guest_log(
    log_text: str,
    *,
    run_id: str,
    require_complete: bool = True,
) -> DualGuestObservation:
    """Validate DTB, console identity, READY, drop and failure contracts."""

    _require_run_id(run_id)
    _reject_forbidden_runtime_text(log_text)
    console = _parse_console_lines(log_text)
    ready = _parse_ready(console, run_id=run_id, require_all=require_complete)
    dtb = parse_dtb_evidence(log_text, require_all=require_complete)
    drops = _parse_drop_summaries(log_text)
    return DualGuestObservation(
        dtb=dtb,
        console=console,
        ready=ready,
        drop_summaries=drops,
    )


def validate_no_data_plane_qemu_config(config_text: str) -> None:
    """Reject outer-QEMU networking/placeholders for the P2 typed profile."""

    lowered = config_text.lower()
    if "${" in config_text or "-netdev" in lowered:
        raise RuntimeContractError("P2 QEMU config contains a placeholder or -netdev")
    if "-nic" not in lowered or re.search(r'"-nic"\s*,\s*"none"', lowered) is None:
        raise RuntimeContractError("P2 QEMU config must contain the exact -nic none pair")
    forbidden = ("virtio-net", "tap", "bridge", "hostfwd", "user,id=")
    observed = [token for token in forbidden if token in lowered]
    if observed:
        raise RuntimeContractError(
            "P2 QEMU config contains outer data-plane token(s): " + ", ".join(observed)
        )
    for name, value in (("-smp", "4"), ("-m", "8g")):
        if re.search(rf'"{re.escape(name)}"\s*,\s*"{re.escape(value)}"', lowered) is None:
            raise RuntimeContractError(f"P2 QEMU config must contain {name} {value}")


def validate_no_data_plane_vm_config(
    config_text: str, *, expected_vm_id: int, expected_cpu_ids: tuple[int, ...]
) -> None:
    """Validate current typed VM schema and reject all virtual data devices."""

    if "vm_type" in config_text or "interrupt_mode" in config_text or "emu_devices" in config_text:
        raise RuntimeContractError("P2 VM config uses the legacy TOML schema")
    if "[[devices.virtual]]" in config_text or "virtio-net" in config_text.lower():
        raise RuntimeContractError("P2 VM config must not declare a virtual data-plane device")
    id_match = re.search(r"(?m)^id\s*=\s*([0-9]+)\s*$", config_text)
    cpu_match = re.search(r"(?m)^cpu_num\s*=\s*([0-9]+)\s*$", config_text)
    ids_match = re.search(r"(?m)^phys_cpu_ids\s*=\s*\[([^]]*)\]", config_text)
    if id_match is None or int(id_match.group(1)) != expected_vm_id:
        raise RuntimeContractError(f"P2 VM config must declare base.id={expected_vm_id}")
    if cpu_match is None or int(cpu_match.group(1)) != len(expected_cpu_ids):
        raise RuntimeContractError(
            f"P2 VM{expected_vm_id} cpu_num must equal {len(expected_cpu_ids)}"
        )
    if ids_match is None:
        raise RuntimeContractError(f"P2 VM{expected_vm_id} has no phys_cpu_ids")
    actual_ids = tuple(int(value.strip()) for value in ids_match.group(1).split(",") if value.strip())
    if actual_ids != expected_cpu_ids:
        raise RuntimeContractError(
            f"P2 VM{expected_vm_id} phys_cpu_ids={actual_ids!r}, expected {expected_cpu_ids!r}"
        )


def inject_qemu_identity_args(
    config_text: str, *, qmp_socket: Path, qemu_pidfile: Path, qemu_name: str
) -> str:
    """Return a resolved QEMU TOML with fresh QMP/pidfile/name arguments.

    The input is never modified.  Existing identity flags are rejected to
    prevent two competing QEMU identities in one run.
    """

    if any(flag in config_text for flag in ("-qmp", "-pidfile", "-name")):
        raise RuntimeContractError("QEMU config already contains runner identity arguments")
    marker = "args = ["
    start = config_text.find(marker)
    if start < 0:
        raise RuntimeContractError("QEMU config has no args array")
    cursor = start + len(marker)
    quote = False
    escaped = False
    end = -1
    while cursor < len(config_text):
        char = config_text[cursor]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = False
        elif char == '"':
            quote = True
        elif char == "]":
            end = cursor
            break
        cursor += 1
    if end < 0:
        raise RuntimeContractError("QEMU config args array is not closed")
    injected = "".join(
        f'  "{value}",\n'
        for value in (
            "-qmp",
            f"unix:{qmp_socket},server=on,wait=off",
            "-pidfile",
            str(qemu_pidfile),
            "-name",
            qemu_name,
        )
    )
    return config_text[: end] + injected + config_text[end:]


def build_axvisor_qemu_command(
    *,
    build_config: Path,
    qemu_config: Path,
    linux_vmconfig: Path,
    zephyr_vmconfig: Path,
) -> list[str]:
    """Build the fixed repository-root AxVisor launcher argv."""

    return [
        "cargo",
        "xtask",
        "axvisor",
        "qemu",
        "--config",
        str(build_config),
        "--qemu-config",
        str(qemu_config),
        "--vmconfigs",
        str(linux_vmconfig),
        "--vmconfigs",
        str(zephyr_vmconfig),
    ]


def make_runtime_identity(*, nonce: str | None = None, runtime_root: Path = Path("/tmp")) -> RuntimeIdentity:
    """Allocate a fresh owned runtime path and QEMU identity without creating it."""

    actual_nonce = nonce or secrets.token_hex(16)
    _require_nonce(actual_nonce)
    runtime_dir = runtime_root / f"axdual-{actual_nonce}"
    return RuntimeIdentity(
        nonce=actual_nonce,
        qemu_name=f"axvisor-dual-smoke-{actual_nonce}",
        runtime_dir=runtime_dir,
        qmp_socket=runtime_dir / "qmp.sock",
        qemu_pidfile=runtime_dir / "qemu.pid",
        live_log=runtime_dir / "axvisor-live.log",
    )


def validate_status_last(output_dir: Path) -> dict[str, Any]:
    """Read a terminal status and reject files written after it."""

    candidate = Path(output_dir)
    if candidate.is_symlink() or not candidate.is_dir():
        raise RuntimeContractError(
            f"status output must be a regular directory: {output_dir}"
        )
    output = candidate.resolve()
    status_path = output / "status.json"
    if not status_path.is_file():
        raise RuntimeContractError("terminal status.json is missing")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeContractError(f"terminal status.json is invalid: {error}") from error
    if not isinstance(status, dict) or status.get("statusLast") is not True:
        raise RuntimeContractError("status.json is not marked status-last")
    status_time = status_path.stat().st_mtime_ns
    later: list[str] = []
    for path in output.rglob("*"):
        if path.is_symlink():
            raise RuntimeContractError(f"status output contains a symlink: {path}")
        if path.is_file() and path != status_path and path.stat().st_mtime_ns > status_time:
            later.append(str(path.relative_to(output)))
    if later:
        raise RuntimeContractError("files were written after status.json: " + ", ".join(later))
    return status


def _write_json_no_overwrite(path: Path, value: Mapping[str, Any]) -> None:
    publish_new_file(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        error_type=RuntimeContractError,
    )


def _publish_status_last(output_dir: Path, status: Mapping[str, Any]) -> None:
    status_path = output_dir / "status.json"
    payload = dict(status)
    payload["statusLast"] = True
    publish_new_file(
        status_path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        error_type=RuntimeContractError,
    )


def _copy_soak_artifact(source: Path, destination: Path, label: str) -> None:
    """Copy one immutable soak input without replacing an existing artifact."""

    source = _regular_file(source, label)
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise RuntimeContractError(f"{label} destination is not a regular file: {destination}")
        if destination.resolve() != source.resolve():
            raise RuntimeContractError(f"refusing to overwrite soak artifact: {destination}")
        return
    publish_new_file(
        destination,
        source.read_bytes(),
        error_type=RuntimeContractError,
    )


def _soak_file_claim(output: Path, path: Path, relative: str, label: str) -> dict[str, Any]:
    """Return a relative, byte-bound claim for one soak artifact."""

    actual = _regular_file(path, label)
    try:
        actual.relative_to(output)
    except ValueError as error:
        raise RuntimeContractError(f"{label} is outside the soak output: {actual}") from error
    if actual.relative_to(output).as_posix() != relative:
        raise RuntimeContractError(f"{label} has an unexpected relative path")
    return {
        "path": relative,
        "size": actual.stat().st_size,
        "sha256": _sha256_file(actual),
    }


def _validate_soak_file_claim(
    output: Path, relative: str, claim: Any, label: str
) -> Path:
    """Validate one relative claim and return its regular artifact path."""

    if (
        not isinstance(claim, dict)
        or set(claim) != {"path", "size", "sha256"}
        or claim.get("path") != relative
        or not isinstance(claim.get("size"), int)
        or isinstance(claim.get("size"), bool)
        or claim["size"] < 0
        or not isinstance(claim.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", claim["sha256"]) is None
    ):
        raise RuntimeContractError(f"{label} claim is malformed")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise RuntimeContractError(f"{label} path escapes the soak output")
    path = (output / relative_path).resolve()
    try:
        path.relative_to(output)
    except ValueError as error:
        raise RuntimeContractError(f"{label} path escapes the soak output") from error
    actual = _regular_file(path, label)
    if actual.stat().st_size != claim["size"] or _sha256_file(actual) != claim["sha256"]:
        raise RuntimeContractError(f"{label} size/SHA-256 drifted")
    return actual


def publish_dual_guest_soak_session(
    evidence: DualGuestSoakEvidence, *, output_dir: Path
) -> dict[str, Any]:
    """Publish and independently consume one current-f964 1,800-second session.

    The publisher is intentionally below the qualification layer.  It binds
    the current ``contest dtb evidence``/``[VM n]`` observation, resolved
    inputs and runner identity into a fresh status-neutral session file.  The
    caller must publish the terminal status only after this function returns.
    """

    candidate_output = Path(output_dir)
    if candidate_output.is_symlink() or not candidate_output.is_dir():
        raise RuntimeContractError(
            f"soak output must be an existing regular directory: {output_dir}"
        )
    output = candidate_output.resolve()
    _require_run_id(evidence.run_id)
    _require_nonce(evidence.identity.nonce)
    if evidence.identity.qemu_name != f"axvisor-dual-smoke-{evidence.identity.nonce}":
        raise RuntimeContractError("soak identity qemu_name is not nonce-bound")
    for value, label in (
        (evidence.launcher_pid, "launcher_pid"),
        (evidence.launcher_start_monotonic_ns, "launcher_start_monotonic_ns"),
        (evidence.start_monotonic_ns, "start_monotonic_ns"),
        (evidence.end_monotonic_ns, "end_monotonic_ns"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise RuntimeContractError(f"soak {label} must be a positive integer")
    duration_ns = evidence.end_monotonic_ns - evidence.start_monotonic_ns
    if duration_ns < SOAK_MIN_DURATION_NS:
        raise RuntimeContractError("soak observation is shorter than 1,800 seconds")
    if set(evidence.resolved_configs) != {"qemu", "linux", "zephyr"}:
        raise RuntimeContractError("soak resolved_configs must contain qemu, linux and zephyr")
    if output.exists() is False:
        raise RuntimeContractError(f"soak output directory does not exist: {output}")
    session_path = output / "dual-guest-soak-session.json"
    if session_path.exists():
        raise RuntimeContractError(f"refusing to overwrite soak session: {session_path}")

    config_paths = {
        "qemu": output / "configs" / "qemu.resolved.toml",
        "linux": output / "configs" / "linux.resolved.toml",
        "zephyr": output / "configs" / "zephyr.resolved.toml",
    }
    for name, source in evidence.resolved_configs.items():
        _copy_soak_artifact(Path(source), config_paths[name], f"{name} resolved config")
    raw_log = output / "logs" / "axvisor.raw.log"
    _copy_soak_artifact(evidence.raw_log, raw_log, "AxVisor raw log")

    file_claims = {
        "configs/qemu.resolved.toml": _soak_file_claim(
            output, config_paths["qemu"], "configs/qemu.resolved.toml", "QEMU resolved config"
        ),
        "configs/linux.resolved.toml": _soak_file_claim(
            output, config_paths["linux"], "configs/linux.resolved.toml", "Linux resolved config"
        ),
        "configs/zephyr.resolved.toml": _soak_file_claim(
            output, config_paths["zephyr"], "configs/zephyr.resolved.toml", "Zephyr resolved config"
        ),
        "logs/axvisor.raw.log": _soak_file_claim(
            output, raw_log, "logs/axvisor.raw.log", "AxVisor raw log"
        ),
    }
    dtb = {
        str(vm_id): {
            "vm_id": value.vm_id,
            "gpa": value.gpa,
            "size": value.size,
            "hpa": value.hpa,
        }
        for vm_id, value in sorted(evidence.observation.dtb.items())
    }
    guests = [
        {
            "vm_id": vm_id,
            "guest": EXPECTED_GUESTS[vm_id],
            "ready": evidence.observation.ready[vm_id],
            "dtb": dtb[str(vm_id)],
            "config": file_claims[f"configs/{EXPECTED_GUESTS[vm_id]}.resolved.toml"],
        }
        for vm_id in (1, 2)
    ]
    session = {
        "schema_version": SOAK_SESSION_SCHEMA,
        "run_id": evidence.run_id,
        "status": SOAK_SESSION_STATUS,
        "artifact_status": "runtime-observation-unreviewed",
        "execution": "completed",
        "qualified": False,
        "evidence_level": "L6 stability",
        "proof_scope": "one-identity-bound-f964-dual-guest-1800-second-coexistence-session",
        "identity": {
            "session_nonce": evidence.identity.nonce,
            "qemu_name": evidence.identity.qemu_name,
            "runtime_dir": str(evidence.identity.runtime_dir),
            "qmp_socket": str(evidence.identity.qmp_socket),
            "qemu_pidfile": str(evidence.identity.qemu_pidfile),
            "launcher_pid": evidence.launcher_pid,
            "launcher_start_monotonic_ns": evidence.launcher_start_monotonic_ns,
        },
        "start_monotonic_ns": evidence.start_monotonic_ns,
        "end_monotonic_ns": evidence.end_monotonic_ns,
        "duration_ns": duration_ns,
        "guests": guests,
        "observation": {
            "dtb_vms": sorted(evidence.observation.dtb),
            "ready_vms": sorted(evidence.observation.ready),
            "drop_summaries": list(evidence.observation.drop_summaries),
        },
        "files": file_claims,
        "non_claims": [
            "this session does not prove Guest-IP, AI closed loop, real-time improvement, or DMA isolation",
            "this session is not P4 network qualification or P5 AI qualification",
        ],
    }
    _write_json_no_overwrite(session_path, session)
    validate_dual_guest_soak_session(session_path, output_dir=output)
    return {
        "path": str(session_path),
        "size": session_path.stat().st_size,
        "sha256": _sha256_file(session_path),
        "duration_ns": duration_ns,
        "qualified": False,
    }


def validate_dual_guest_soak_session(
    session_path: Path, *, output_dir: Path | None = None
) -> dict[str, Any]:
    """Consume a current-f964 soak session without upgrading its evidence."""

    session_candidate = Path(session_path)
    session_path = _regular_file(session_candidate, "soak session").resolve()
    output_candidate = Path(output_dir) if output_dir is not None else session_path.parent
    if output_candidate.is_symlink() or not output_candidate.is_dir():
        raise RuntimeContractError(
            f"soak output must be a regular directory: {output_candidate}"
        )
    output = output_candidate.resolve()
    try:
        if session_path.relative_to(output).as_posix() != "dual-guest-soak-session.json":
            raise RuntimeContractError("soak session is outside the soak output")
    except ValueError as error:
        raise RuntimeContractError("soak session is outside the soak output") from error
    try:
        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise RuntimeContractError(f"duplicate soak session key: {key}")
                result[key] = value
            return result

        session = json.loads(
            session_path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeContractError(f"soak session is not valid JSON: {error}") from error
    if not isinstance(session, dict):
        raise RuntimeContractError("soak session must be a JSON object")
    expected_keys = {
        "schema_version", "run_id", "status", "artifact_status", "execution", "qualified",
        "evidence_level", "proof_scope", "identity", "start_monotonic_ns", "end_monotonic_ns",
        "duration_ns", "guests", "observation", "files", "non_claims",
    }
    if set(session) != expected_keys:
        raise RuntimeContractError("soak session key set drifted")
    if (
        session["schema_version"] != SOAK_SESSION_SCHEMA
        or session["status"] != SOAK_SESSION_STATUS
        or session["artifact_status"] != "runtime-observation-unreviewed"
        or session["execution"] != "completed"
        or session["qualified"] is not False
        or session["evidence_level"] != "L6 stability"
        or session["proof_scope"] != "one-identity-bound-f964-dual-guest-1800-second-coexistence-session"
    ):
        raise RuntimeContractError("soak session status or proof scope drifted")
    run_id = session.get("run_id")
    if not isinstance(run_id, str):
        raise RuntimeContractError("soak session run_id is missing")
    _require_run_id(run_id)
    start = session.get("start_monotonic_ns")
    end = session.get("end_monotonic_ns")
    duration = session.get("duration_ns")
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in (start, end, duration)):
        raise RuntimeContractError("soak session monotonic timestamps are invalid")
    if end <= start or end - start != duration or duration < SOAK_MIN_DURATION_NS:
        raise RuntimeContractError("soak session duration is not an exact 1,800-second minimum window")
    identity = session.get("identity")
    identity_keys = {
        "session_nonce", "qemu_name", "runtime_dir", "qmp_socket", "qemu_pidfile",
        "launcher_pid", "launcher_start_monotonic_ns",
    }
    if not isinstance(identity, dict) or set(identity) != identity_keys:
        raise RuntimeContractError("soak session identity key set drifted")
    nonce = identity.get("session_nonce")
    if not isinstance(nonce, str):
        raise RuntimeContractError("soak session nonce is missing")
    _require_nonce(nonce)
    if identity.get("qemu_name") != f"axvisor-dual-smoke-{nonce}":
        raise RuntimeContractError("soak session QEMU name is not nonce-bound")
    for field in ("runtime_dir", "qmp_socket", "qemu_pidfile"):
        if not isinstance(identity.get(field), str) or not identity[field]:
            raise RuntimeContractError(f"soak session identity {field} is missing")
    for field in ("launcher_pid", "launcher_start_monotonic_ns"):
        value = identity.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise RuntimeContractError(f"soak session identity {field} is invalid")
    if identity["launcher_start_monotonic_ns"] > start:
        raise RuntimeContractError("soak launcher start is after the observation window")

    files = session.get("files")
    expected_files = {
        "configs/qemu.resolved.toml",
        "configs/linux.resolved.toml",
        "configs/zephyr.resolved.toml",
        "logs/axvisor.raw.log",
    }
    if not isinstance(files, dict) or set(files) != expected_files:
        raise RuntimeContractError("soak session file set drifted")
    core_files = expected_files | {"dual-guest-soak-session.json"}
    runner_files = {"inputs.json", "session.json", "commands.jsonl"}
    terminal_files = {"manifest.json", "cleanup.json", "status.json"}
    actual_files = set()
    actual_dirs = set()
    for entry in output.rglob("*"):
        if entry.is_symlink():
            raise RuntimeContractError(f"soak output contains a symlink: {entry}")
        relative = entry.relative_to(output).as_posix()
        if entry.is_file():
            actual_files.add(relative)
        elif entry.is_dir():
            actual_dirs.add(relative)
        else:
            raise RuntimeContractError(f"soak output contains an unsupported entry: {relative}")
    optional_files = actual_files - core_files
    if not core_files.issubset(actual_files) or not optional_files.issubset(
        runner_files | terminal_files
    ):
        raise RuntimeContractError("soak session file set drifted")
    present_runner_files = optional_files & runner_files
    if present_runner_files and present_runner_files != runner_files:
        raise RuntimeContractError("soak runner file set is incomplete")
    present_terminal_files = optional_files & terminal_files
    if present_terminal_files and present_terminal_files != terminal_files:
        raise RuntimeContractError("soak terminal file set is incomplete")
    if actual_dirs != {"configs", "logs"}:
        raise RuntimeContractError("soak output directory set drifted")
    actual_files = {
        relative: _validate_soak_file_claim(output, relative, files[relative], relative)
        for relative in sorted(expected_files)
    }
    qemu_text = actual_files["configs/qemu.resolved.toml"].read_text(encoding="utf-8")
    for token in (identity["qemu_name"], identity["qmp_socket"], identity["qemu_pidfile"]):
        if token not in qemu_text:
            raise RuntimeContractError("resolved QEMU config is not bound to soak identity")

    raw_text = actual_files["logs/axvisor.raw.log"].read_text(encoding="utf-8", errors="replace")
    try:
        observation = validate_dual_guest_log(raw_text, run_id=run_id)
    except (UnicodeError, RuntimeContractError) as error:
        raise RuntimeContractError(f"soak raw log failed current-f964 validation: {error}") from error
    observation_claim = session.get("observation")
    if not isinstance(observation_claim, dict) or set(observation_claim) != {"dtb_vms", "ready_vms", "drop_summaries"}:
        raise RuntimeContractError("soak observation claim is malformed")
    if observation_claim["dtb_vms"] != [1, 2] or observation_claim["ready_vms"] != [1, 2]:
        raise RuntimeContractError("soak observation does not contain both Guest identities")
    if observation_claim["drop_summaries"] != list(observation.drop_summaries):
        raise RuntimeContractError("soak drop summary claim drifted")
    guests = session.get("guests")
    if not isinstance(guests, list) or len(guests) != 2:
        raise RuntimeContractError("soak session must contain exactly two guests")
    for expected_vm, expected_guest in ((1, "linux"), (2, "zephyr")):
        guest = guests[expected_vm - 1]
        if not isinstance(guest, dict) or set(guest) != {"vm_id", "guest", "ready", "dtb", "config"}:
            raise RuntimeContractError("soak guest entry key set drifted")
        if guest["vm_id"] != expected_vm or guest["guest"] != expected_guest:
            raise RuntimeContractError("soak guest identity/order drifted")
        if guest["ready"] != observation.ready[expected_vm]:
            raise RuntimeContractError(f"soak VM{expected_vm} READY claim drifted")
        dtb = guest["dtb"]
        observed_dtb = observation.dtb[expected_vm]
        if dtb != {
            "vm_id": observed_dtb.vm_id,
            "gpa": observed_dtb.gpa,
            "size": observed_dtb.size,
            "hpa": observed_dtb.hpa,
        }:
            raise RuntimeContractError(f"soak VM{expected_vm} DTB claim drifted")
        expected_config = f"configs/{expected_guest}.resolved.toml"
        if guest["config"] != files[expected_config]:
            raise RuntimeContractError(f"soak VM{expected_vm} config claim drifted")
    return {
        "schema_version": f"{SOAK_SESSION_SCHEMA}-validation-v1",
        "valid": True,
        "run_id": run_id,
        "duration_ns": duration,
        "qualified": False,
        "evidence_level": "L6 stability",
        "non_claims": [
            "validation does not prove Guest-IP, AI closed loop, real-time improvement, or qualification"
        ],
    }


def _validate_spec(spec: DualGuestSpec) -> None:
    _require_run_id(spec.run_id)
    if spec.duration_seconds not in (300.0,) and spec.duration_seconds < 1800.0:
        raise RuntimeContractError("duration must be exactly 300 seconds or at least 1800 seconds")
    if spec.duration_seconds <= 0 or spec.ready_timeout_seconds <= 0 or spec.shutdown_timeout_seconds <= 0:
        raise RuntimeContractError("runtime timeouts and duration must be positive")
    if not spec.repository.is_dir():
        raise RuntimeContractError(f"repository is not a directory: {spec.repository}")
    if not spec.runtime_root.is_absolute():
        raise RuntimeContractError("runtime_root must be an absolute path")
    if not spec.runtime_root.is_dir() or spec.runtime_root.is_symlink():
        raise RuntimeContractError(
            f"runtime_root must be an existing non-symlink directory: {spec.runtime_root}"
        )
    for path, label in (
        (spec.build_config, "build config"),
        (spec.qemu_config, "QEMU config"),
        (spec.linux_vmconfig, "Linux VM config"),
        (spec.zephyr_vmconfig, "Zephyr VM config"),
    ):
        _regular_file(path, label)
    validate_no_data_plane_qemu_config(spec.qemu_config.read_text(encoding="utf-8"))
    validate_no_data_plane_vm_config(
        spec.linux_vmconfig.read_text(encoding="utf-8"),
        expected_vm_id=1,
        expected_cpu_ids=(0, 1),
    )
    validate_no_data_plane_vm_config(
        spec.zephyr_vmconfig.read_text(encoding="utf-8"),
        expected_vm_id=2,
        expected_cpu_ids=(2,),
    )


def _wait_for_contract(
    log_path: Path,
    *,
    run_id: str,
    timeout_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> DualGuestObservation:
    deadline = monotonic() + timeout_seconds
    last_missing = "official DTB evidence and both READY markers"
    while monotonic() < deadline:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        # Check forbidden/identity errors while the stream is still partial;
        # only absence of required lines is allowed to remain pending.
        _reject_forbidden_runtime_text(text)
        console = _parse_console_lines(text)
        ready = _parse_ready(console, run_id=run_id, require_all=False)
        dtb = parse_dtb_evidence(text, require_all=False)
        missing: list[str] = []
        if len(dtb) != len(EXPECTED_GUESTS):
            missing.append("DTB=" + ",".join(str(vm) for vm in EXPECTED_GUESTS if vm not in dtb))
        if len(ready) != len(EXPECTED_GUESTS):
            missing.append("READY=" + ",".join(str(vm) for vm in EXPECTED_GUESTS if vm not in ready))
        if not missing:
            return validate_dual_guest_log(text, run_id=run_id)
        last_missing = "; ".join(missing)
        sleep(0.25)
    raise RuntimeContractError(f"timed out waiting for dual-Guest contract ({last_missing})")


def request_bounded_shutdown(
    launcher: subprocess.Popen[bytes],
    *,
    pidfile: Path,
    timeout_seconds: float,
) -> str | None:
    """Stop only the runner-owned launcher/process group."""

    deadline = time.monotonic() + timeout_seconds
    try:
        if pidfile.is_file():
            pid = int(pidfile.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGINT)
        elif launcher.poll() is None:
            launcher.send_signal(signal.SIGINT)
    except (OSError, ValueError):
        if launcher.poll() is None:
            launcher.send_signal(signal.SIGTERM)
    while launcher.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if launcher.poll() is not None:
        return None
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(launcher.pid), signal.SIGKILL)
        else:
            launcher.kill()
    except (OSError, ProcessLookupError):
        launcher.kill()
    try:
        launcher.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return "owned launcher remained alive after bounded SIGINT/TERM/KILL"
    return None


def run_dual_guest(
    spec: DualGuestSpec,
    *,
    popen_factory: Callable[..., Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run a bounded dual-Guest observation and publish a fail-closed status.

    A 300-second run publishes the short-smoke bundle.  A run of at least
    1,800 seconds additionally publishes the current-f964 soak session, but
    neither path produces Guest-IP, AI-loop, real-time, or qualification
    evidence.
    """

    _validate_spec(spec)
    if spec.output_dir.exists() or spec.output_dir.is_symlink():
        raise RuntimeContractError(f"output directory already exists: {spec.output_dir}")

    output = spec.output_dir
    output.mkdir(parents=True)
    configs = output / "configs"
    logs = output / "logs"
    configs.mkdir()
    logs.mkdir()
    identity = make_runtime_identity(runtime_root=spec.runtime_root)
    if identity.runtime_dir.exists():
        raise RuntimeContractError(f"owned runtime directory already exists: {identity.runtime_dir}")
    identity.runtime_dir.mkdir()

    resolved_qemu = configs / "qemu.resolved.toml"
    publish_new_file(
        resolved_qemu,
        inject_qemu_identity_args(
            spec.qemu_config.read_text(encoding="utf-8"),
            qmp_socket=identity.qmp_socket,
            qemu_pidfile=identity.qemu_pidfile,
            qemu_name=identity.qemu_name,
        ).encode("utf-8"),
        error_type=RuntimeContractError,
    )
    command = build_axvisor_qemu_command(
        build_config=spec.build_config.resolve(),
        qemu_config=resolved_qemu.resolve(),
        linux_vmconfig=spec.linux_vmconfig.resolve(),
        zephyr_vmconfig=spec.zephyr_vmconfig.resolve(),
    )
    input_manifest = {
        "buildConfig": {"path": str(spec.build_config), "sha256": _sha256_file(spec.build_config)},
        "qemuConfig": {"path": str(spec.qemu_config), "sha256": _sha256_file(spec.qemu_config)},
        "linuxVmconfig": {"path": str(spec.linux_vmconfig), "sha256": _sha256_file(spec.linux_vmconfig)},
        "zephyrVmconfig": {"path": str(spec.zephyr_vmconfig), "sha256": _sha256_file(spec.zephyr_vmconfig)},
    }
    _write_json_no_overwrite(output / "inputs.json", input_manifest)
    _write_json_no_overwrite(
        output / "session.json",
        {
            "schemaVersion": RUN_SCHEMA_VERSION,
            "runId": spec.run_id,
            "sessionNonce": identity.nonce,
            "qemuName": identity.qemu_name,
            "durationSeconds": spec.duration_seconds,
        },
    )
    publish_new_file(
        output / "commands.jsonl",
        (
            json.dumps({"step": "launch", "cwd": str(spec.repository.resolve()), "argv": command})
            + "\n"
        ).encode("utf-8"),
        error_type=RuntimeContractError,
    )

    launcher: subprocess.Popen[bytes] | None = None
    primary_error: str | None = None
    cleanup_error: str | None = None
    observation: DualGuestObservation | None = None
    launcher_start_monotonic_ns: int | None = None
    stability_start_monotonic_ns: int | None = None
    stability_end_monotonic_ns: int | None = None
    try:
        with identity.live_log.open("xb") as log_handle:
            launcher_start_monotonic_ns = int(monotonic() * 1_000_000_000)
            launcher = (popen_factory or subprocess.Popen)(
                command,
                cwd=spec.repository,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            observation = _wait_for_contract(
                identity.live_log,
                run_id=spec.run_id,
                timeout_seconds=spec.ready_timeout_seconds,
                monotonic=monotonic,
                sleep=sleep,
            )
            stability_start_monotonic_ns = int(monotonic() * 1_000_000_000)
            end = monotonic() + spec.duration_seconds
            while monotonic() < end:
                validate_dual_guest_log(
                    identity.live_log.read_text(encoding="utf-8", errors="replace"),
                    run_id=spec.run_id,
                )
                exit_code = launcher.poll()
                if exit_code is not None:
                    raise RuntimeContractError(
                        f"dual-Guest launcher exited before the stability window with code {exit_code}"
                    )
                sleep(min(1.0, max(0.05, end - monotonic())))
            stability_end_monotonic_ns = int(monotonic() * 1_000_000_000)
            exit_code = launcher.poll()
            if exit_code not in (None, 0):
                raise RuntimeContractError(
                    f"dual-Guest launcher exited at the stability boundary with code {exit_code}"
                )
    except (OSError, RuntimeContractError, subprocess.SubprocessError) as error:
        primary_error = str(error)
    finally:
        if launcher is not None and launcher.poll() is None:
            cleanup_error = request_bounded_shutdown(
                launcher,
                pidfile=identity.qemu_pidfile,
                timeout_seconds=spec.shutdown_timeout_seconds,
            )
        if identity.live_log.exists():
            shutil.copyfile(identity.live_log, logs / "axvisor.raw.log")
        if primary_error is None and cleanup_error is None and identity.runtime_dir.exists():
            try:
                shutil.rmtree(identity.runtime_dir)
            except OSError:
                cleanup_error = "owned runtime directory remained after cleanup"

    success = primary_error is None and cleanup_error is None and observation is not None
    soak_session: dict[str, Any] | None = None
    if success and spec.duration_seconds >= 1800.0:
        if (
            launcher is None
            or launcher_start_monotonic_ns is None
            or stability_start_monotonic_ns is None
            or stability_end_monotonic_ns is None
        ):
            primary_error = "soak session timing or launcher identity was not recorded"
            success = False
        else:
            try:
                soak_session = publish_dual_guest_soak_session(
                    DualGuestSoakEvidence(
                        run_id=spec.run_id,
                        identity=identity,
                        observation=observation,
                        launcher_pid=int(launcher.pid),
                        launcher_start_monotonic_ns=launcher_start_monotonic_ns,
                        start_monotonic_ns=stability_start_monotonic_ns,
                        end_monotonic_ns=stability_end_monotonic_ns,
                        raw_log=logs / "axvisor.raw.log",
                        resolved_configs={
                            "qemu": resolved_qemu,
                            "linux": spec.linux_vmconfig,
                            "zephyr": spec.zephyr_vmconfig,
                        },
                    ),
                    output_dir=output,
                )
            except (OSError, RuntimeContractError) as error:
                primary_error = f"soak session publication failed: {error}"
                success = False
    status_token = (
        SOAK_SESSION_STATUS
        if success and spec.duration_seconds >= 1800.0
        else "dual_guest_short_smoke_completed"
        if success
        else "dual_guest_soak_session_failed"
        if spec.duration_seconds >= 1800.0
        else "dual_guest_short_smoke_failed"
    )
    manifest = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "status": status_token,
        "success": success,
        "qualified": False,
        "proofScope": (
            "one-identity-bound-f964-dual-guest-1800-second-coexistence-session"
            if spec.duration_seconds >= 1800.0
            else "one-identity-bound-f964-dual-guest-short-smoke"
        ),
        "observation": {
            "dtbVms": sorted(observation.dtb) if observation else [],
            "readyVms": sorted(observation.ready) if observation else [],
            "dropSummaries": list(observation.drop_summaries) if observation else [],
        },
        "doesNotProve": [
            "Guest IP connectivity",
            "AI closed loop",
            "real-time improvement",
            "hardware DMA or general DMA isolation",
            "1,800-second soak qualification",
        ],
        "inputs": input_manifest,
        "soakSession": soak_session,
    }
    _write_json_no_overwrite(output / "manifest.json", manifest)
    _write_json_no_overwrite(
        output / "cleanup.json",
        {
            "ownedRuntimeDir": str(identity.runtime_dir),
            "runtimeDirRemoved": not identity.runtime_dir.exists(),
            "cleanupError": cleanup_error,
        },
    )
    _publish_status_last(
        output,
        {
            "schemaVersion": RUN_SCHEMA_VERSION,
            "runId": spec.run_id,
            "success": success,
            "status": status_token,
            "primaryError": primary_error,
            "cleanupError": cleanup_error,
            "qualified": False,
            "completedChecks": [
                "official-dtb-evidence",
                "vm-console-attribution",
                "dual-ready",
                "panic-drop-negative",
                "cleanup",
            ] + (["soak-session-published"] if soak_session is not None else [])
            if success
            else [],
            "manifestSha256": _sha256_file(output / "manifest.json"),
        },
    )
    return 0 if success else 1
