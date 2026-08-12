#!/usr/bin/env python3
"""Fail-closed validator for one 30-minute identity-bound dual-Guest session.

This validates a captured session manifest.  It intentionally does not launch
QEMU, inspect network traffic, or infer a DMA operation from a console marker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from host_vm_carveout_io import publish_new_file, read_regular_bytes


MIN_DURATION_NS = 1_800 * 1_000_000_000
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
BOOT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DTB_RE = re.compile(
    r"^AXVISOR_GUEST_DTB_READY vm=(?P<vm>[12]) "
    r"gpa=0x[0-9a-f]+ size=[1-9][0-9]* "
    r"hpa_segments=0x[0-9a-f]+:[1-9][0-9]*(?:,0x[0-9a-f]+:[1-9][0-9]*)*$"
)
FORBIDDEN_RE = re.compile(r"(?:restart|exit|panic|unsafe)", re.IGNORECASE)


class SoakError(ValueError):
    """The supplied session does not meet the narrow coexistence contract."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SoakError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _json(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, SoakError) as error:
        raise SoakError(f"{label} is not duplicate-free UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise SoakError(f"{label} must be a JSON object")
    return value


def _keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise SoakError(f"{label} keys must be exactly {sorted(expected)!r}")


def _positive(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SoakError(f"{label} must be a positive integer")
    return value


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SoakError("identity must be an object")
    expected = {"bootId", "qemuPid", "qemuStartMonotonicNs", "qemuName", "sessionNonce"}
    _keys(value, expected, label="identity")
    boot_id = value["bootId"]
    nonce = value["sessionNonce"]
    if not isinstance(boot_id, str) or not BOOT_ID_RE.fullmatch(boot_id):
        raise SoakError("identity.bootId has an unsafe format")
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        raise SoakError("identity.sessionNonce must be 32 lowercase hex characters")
    if not isinstance(value["qemuName"], str) or value["qemuName"] != f"axvisor-dual-soak-{nonce}":
        raise SoakError("identity.qemuName is not nonce-bound")
    _positive(value["qemuPid"], label="identity.qemuPid")
    _positive(value["qemuStartMonotonicNs"], label="identity.qemuStartMonotonicNs")
    return value


def _validate_config(value: Any, *, source: Path, contents: bytes, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SoakError(f"{label}.vmConfig must be an object")
    _keys(value, {"path", "sha256", "size"}, label=f"{label}.vmConfig")
    if value["path"] != source.name or value["sha256"] != _sha(contents) or value["size"] != len(contents):
        raise SoakError(f"{label}.vmConfig is not byte-bound to its supplied config")
    return value


def _config_cpu_ids(contents: bytes, *, label: str) -> list[int]:
    try:
        config = tomllib.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SoakError(f"{label} VM config is not valid UTF-8 TOML: {error}") from error
    base = config.get("base")
    if not isinstance(base, dict):
        raise SoakError(f"{label} VM config must contain a [base] table")
    cpu_ids = base.get("phys_cpu_ids")
    if not isinstance(cpu_ids, list) or not cpu_ids:
        raise SoakError(f"{label} VM config base.phys_cpu_ids must be a non-empty integer list")
    if any(not isinstance(cpu_id, int) or isinstance(cpu_id, bool) or cpu_id < 0 for cpu_id in cpu_ids):
        raise SoakError(f"{label} VM config base.phys_cpu_ids must be a non-negative integer list")
    if len(set(cpu_ids)) != len(cpu_ids):
        raise SoakError(f"{label} VM config base.phys_cpu_ids must not contain duplicates")
    return cpu_ids


def _validate_guest(value: Any, *, expected_vm: int, expected_guest: str, source: Path, contents: bytes, boot_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SoakError("guest entry must be an object")
    _keys(value, {"vmId", "guest", "vmConfig", "guestDtbMarker", "readyMarker"}, label="guest")
    if value["vmId"] != expected_vm or value["guest"] != expected_guest:
        raise SoakError(f"expected unique VM{expected_vm} {expected_guest} guest entry")
    marker = value["guestDtbMarker"]
    if not isinstance(marker, str) or (match := DTB_RE.fullmatch(marker)) is None or int(match.group("vm")) != expected_vm:
        raise SoakError(f"VM{expected_vm} is missing its exact Guest-DTB marker")
    expected_ready = (
        f"AXVISOR_DUAL_GUEST_{expected_guest.upper()}_READY vm={expected_vm} boot_id={boot_id}"
    )
    if value["readyMarker"] != expected_ready:
        raise SoakError(f"VM{expected_vm} is missing its exact {expected_guest} READY marker")
    _validate_config(value["vmConfig"], source=source, contents=contents, label=f"VM{expected_vm}")
    return value


def _health_marker(phase: str, monotonic_ns: int, identity: dict[str, Any]) -> str:
    return (
        f"AXVISOR_DUAL_GUEST_HEALTH phase={phase} monotonic_ns={monotonic_ns} "
        f"boot_id={identity['bootId']} qemu_pid={identity['qemuPid']} "
        f"qemu_start_ns={identity['qemuStartMonotonicNs']} qemu_name={identity['qemuName']} "
        f"nonce={identity['sessionNonce']} linux_vm=1 zephyr_vm=2"
    )


def _validate_health(value: Any, *, phase: str, monotonic_ns: int, identity: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SoakError("health marker entry must be an object")
    expected = {"phase", "monotonicNs", "bootId", "qemuPid", "qemuStartMonotonicNs", "qemuName", "sessionNonce", "marker"}
    _keys(value, expected, label="health marker")
    if value["phase"] != phase or value["monotonicNs"] != monotonic_ns:
        raise SoakError("health markers must be start then end at session boundaries")
    for key in ("bootId", "qemuPid", "qemuStartMonotonicNs", "qemuName", "sessionNonce"):
        if value[key] != identity[key]:
            raise SoakError(f"health marker {phase} does not bind the QEMU identity")
    if value["marker"] != _health_marker(phase, monotonic_ns, identity):
        raise SoakError(f"health marker {phase} text is not exact")
    return value


def validate_session(session: dict[str, Any], *, linux_config: Path, linux_bytes: bytes, zephyr_config: Path, zephyr_bytes: bytes) -> dict[str, Any]:
    expected = {"schemaVersion", "artifactStatus", "status", "proofScope", "identity", "cpuSets", "startMonotonicNs", "endMonotonicNs", "guests", "healthMarkers", "events"}
    _keys(session, expected, label="session")
    if session["schemaVersion"] != 1 or session["artifactStatus"] != "capture-generated-unreviewed" or session["status"] != "dual_guest_soak_session_completed" or session["proofScope"] != "one-identity-bound-qemu-dual-guest-1800-second-coexistence-session":
        raise SoakError("session has an unsupported status or proof scope")
    identity = _identity(session["identity"])
    linux_cpu_ids = _config_cpu_ids(linux_bytes, label="Linux")
    zephyr_cpu_ids = _config_cpu_ids(zephyr_bytes, label="Zephyr")
    if set(linux_cpu_ids).intersection(zephyr_cpu_ids):
        raise SoakError("supplied VM configs must have mutually exclusive base.phys_cpu_ids")
    expected_cpu_sets = {"linux": linux_cpu_ids, "zephyr": zephyr_cpu_ids}
    if session["cpuSets"] != expected_cpu_sets:
        raise SoakError("cpuSets must exactly match the supplied VM configs' base.phys_cpu_ids")
    start = _positive(session["startMonotonicNs"], label="startMonotonicNs")
    end = _positive(session["endMonotonicNs"], label="endMonotonicNs")
    if end <= start or end - start < MIN_DURATION_NS:
        raise SoakError("monotonic session duration must be at least 1800 seconds")
    guests = session["guests"]
    if not isinstance(guests, list) or len(guests) != 2:
        raise SoakError("guests must contain exactly VM1 Linux and VM2 Zephyr")
    linux = _validate_guest(guests[0], expected_vm=1, expected_guest="linux", source=linux_config, contents=linux_bytes, boot_id=identity["bootId"])
    zephyr = _validate_guest(guests[1], expected_vm=2, expected_guest="zephyr", source=zephyr_config, contents=zephyr_bytes, boot_id=identity["bootId"])
    health = session["healthMarkers"]
    if not isinstance(health, list) or len(health) != 2:
        raise SoakError("exactly one start and one end health marker are required")
    _validate_health(health[0], phase="start", monotonic_ns=start, identity=identity)
    _validate_health(health[1], phase="end", monotonic_ns=end, identity=identity)
    if session["events"] != []:
        raise SoakError("restart, exit, panic, unsafe, or unclassified events are forbidden")
    marker_text = "\n".join([linux["guestDtbMarker"], linux["readyMarker"], zephyr["guestDtbMarker"], zephyr["readyMarker"], health[0]["marker"], health[1]["marker"]])
    if FORBIDDEN_RE.search(marker_text):
        raise SoakError("restart/exit/panic/unsafe text is forbidden in claimed markers")
    return {"identity": identity, "durationNs": end - start, "cpuSets": session["cpuSets"], "guests": [linux, zephyr]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--linux-vm-config", required=True, type=Path)
    parser.add_argument("--zephyr-vm-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        session_bytes = read_regular_bytes(args.session, label="session", error_type=SoakError)
        linux_bytes = read_regular_bytes(args.linux_vm_config, label="Linux VM config", error_type=SoakError)
        zephyr_bytes = read_regular_bytes(args.zephyr_vm_config, label="Zephyr VM config", error_type=SoakError)
        validated = validate_session(_json(session_bytes, label="session"), linux_config=args.linux_vm_config, linux_bytes=linux_bytes, zephyr_config=args.zephyr_vm_config, zephyr_bytes=zephyr_bytes)
        report = {
            "schemaVersion": 1,
            "artifactStatus": "observation-derived",
            "status": "dual_guest_30min_coexistence_observed",
            "proofScope": "one-identity-bound-qemu-dual-guest-1800-second-coexistence-session",
            "doesNotProve": ["Linux and Zephyr IP connectivity", "DMA was executed", "DMA isolation", "end-to-end latency", "AI closed-loop control"],
            "sessionIdentity": validated["identity"], "durationNs": validated["durationNs"], "cpuSets": validated["cpuSets"], "guests": validated["guests"],
            "sources": {"session": {"path": args.session.name, "sha256": _sha(session_bytes), "size": len(session_bytes)}, "linuxVmConfig": {"path": args.linux_vm_config.name, "sha256": _sha(linux_bytes), "size": len(linux_bytes)}, "zephyrVmConfig": {"path": args.zephyr_vm_config.name, "sha256": _sha(zephyr_bytes), "size": len(zephyr_bytes)}},
        }
        publish_new_file(args.output, (json.dumps(report, indent=2, sort_keys=True) + "\n").encode(), error_type=SoakError)
    except (OSError, SoakError) as error:
        print(f"dual-guest soak validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
