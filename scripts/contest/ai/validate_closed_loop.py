#!/usr/bin/env python3
"""Validate one immutable TEST-017 evidence bundle without trusting summaries."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any, Mapping

try:  # Reusable as scripts.contest.ai.validate_closed_loop and as a CLI.
    from . import closed_loop_contract, compute_metrics, model, verify_model
    from . import host_orchestration, produce_icpc_records
except ImportError:  # pragma: no cover - direct CLI execution.
    import closed_loop_contract
    import compute_metrics
    import model
    import verify_model
    import host_orchestration
    import produce_icpc_records


SESSION_SCHEMA = "p5-ai-session-v1"
MANIFEST_SCHEMA = "p5-ai-manifest-v1"
STATUS_SCHEMA = "p5-ai-status-v1"
SUCCESS_CHECKS = {"oracle", "hashes", "validator", "cleanup"}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
HOST_CONTRACT_LEVEL = "L2 host contract"
HOST_CONTRACT_STATUS = "host_contract_valid"
LINUX_MAC = bytes.fromhex("020000000001")
ZEPHYR_MAC = bytes.fromhex("020000000002")
LINUX_IPV4 = bytes((10, 77, 0, 1))
ZEPHYR_IPV4 = bytes((10, 77, 0, 2))
ICPC_UDP_PORT = 46000


class ClosedLoopValidationError(ValueError):
    """One fail-closed TEST-017 bundle validation error."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ClosedLoopValidationError(f"cannot read JSON {path}: {error}") from error


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ClosedLoopValidationError(
                    f"{path}:{line_number} must contain a JSON object"
                )
            rows.append(value)
    except (OSError, json.JSONDecodeError) as error:
        raise ClosedLoopValidationError(f"cannot read JSONL {path}: {error}") from error
    return rows


def _require_file(path: Path, *, nonempty: bool = True) -> None:
    if not path.is_file():
        raise ClosedLoopValidationError(f"required artifact is missing: {path}")
    if nonempty and path.stat().st_size == 0:
        raise ClosedLoopValidationError(f"required artifact is empty: {path}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ClosedLoopValidationError(f"{name} must be an object")
    return value


def _validate_commands(path: Path) -> None:
    commands = _load_jsonl(path)
    if not commands:
        raise ClosedLoopValidationError("commands.jsonl must contain a command record")
    for index, command in enumerate(commands):
        argv = command.get("argv")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(item, str) and item for item in argv
        ):
            raise ClosedLoopValidationError(f"commands[{index}].argv is invalid")
        if not isinstance(command.get("cwd"), str) or not command["cwd"]:
            raise ClosedLoopValidationError(f"commands[{index}].cwd is invalid")
        for field in ("started_monotonic_ns", "finished_monotonic_ns", "exit_code"):
            if not isinstance(command.get(field), int) or isinstance(command[field], bool):
                raise ClosedLoopValidationError(f"commands[{index}].{field} is invalid")
        if command["finished_monotonic_ns"] < command["started_monotonic_ns"]:
            raise ClosedLoopValidationError(f"commands[{index}] time moved backwards")
        if command["exit_code"] != 0:
            raise ClosedLoopValidationError(f"commands[{index}] did not exit successfully")


def _validate_cleanup(path: Path) -> None:
    cleanup = _require_mapping(_load_json(path), "cleanup.json")
    if cleanup.get("residualProcesses") != []:
        raise ClosedLoopValidationError("cleanup reports residual processes")
    if cleanup.get("residualSockets") != []:
        raise ClosedLoopValidationError("cleanup reports residual sockets")
    if cleanup.get("residualFiles") != []:
        raise ClosedLoopValidationError("cleanup reports residual files")


def _validate_pcap(path: Path) -> list[bytes]:
    data = path.read_bytes()
    endian_by_magic = {
        b"\xd4\xc3\xb2\xa1": "<",
        b"\xa1\xb2\xc3\xd4": ">",
        b"\x4d\x3c\xb2\xa1": "<",
        b"\xa1\xb2\x3c\x4d": ">",
    }
    endian = endian_by_magic.get(data[:4])
    if len(data) <= 24 or endian is None:
        raise ClosedLoopValidationError("capture.pcap has no valid packet record")
    _, major, minor, _, _, snaplen, linktype = struct.unpack(
        f"{endian}IHHIIII", data[:24]
    )
    if (major, minor) != (2, 4) or linktype != 1 or snaplen < 14:
        raise ClosedLoopValidationError("capture.pcap global header is invalid")
    packets: list[bytes] = []
    offset = 24
    while offset < len(data):
        if len(data) - offset < 16:
            raise ClosedLoopValidationError("capture.pcap has a truncated record header")
        _, _, included, original = struct.unpack(
            f"{endian}IIII", data[offset : offset + 16]
        )
        offset += 16
        if included < 14 or included > snaplen or original < included:
            raise ClosedLoopValidationError("capture.pcap record length is invalid")
        if len(data) - offset < included:
            raise ClosedLoopValidationError("capture.pcap has a truncated packet")
        packets.append(data[offset : offset + included])
        offset += included
    if not packets:
        raise ClosedLoopValidationError("capture.pcap contains no packets")
    return packets


def _internet_checksum(data: bytes) -> int:
    """Return the RFC 1071 checksum over an even-padded byte string."""

    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _extract_icpc_udp_payload(
    packet: bytes, *, ingress_port: int, location: str
) -> bytes | None:
    """Parse the frozen Ethernet/IPv4/UDP path and return its ICPC payload.

    Non-ICPC traffic (for example ARP) may coexist in a capture.  Any frame
    containing ICPC bytes, or any datagram on the frozen business flow, must
    nevertheless be a complete, checksum-valid fixed-endpoint UDP packet.
    """

    if len(packet) < 14:
        raise ClosedLoopValidationError(f"{location} Ethernet frame is truncated")
    contains_magic = b"ICPC" in packet[14:]
    destination_mac, source_mac = packet[:6], packet[6:12]
    ether_type = int.from_bytes(packet[12:14], "big")
    if ether_type != 0x0800:
        if contains_magic:
            raise ClosedLoopValidationError(
                f"{location} contains ICPC bytes outside IPv4"
            )
        return None

    ipv4 = packet[14:]
    if len(ipv4) < 20:
        raise ClosedLoopValidationError(f"{location} IPv4 header is truncated")
    version, ihl_words = ipv4[0] >> 4, ipv4[0] & 0x0F
    if version != 4 or ihl_words != 5:
        raise ClosedLoopValidationError(f"{location} must use IPv4 without options")
    total_length = int.from_bytes(ipv4[2:4], "big")
    if total_length != len(ipv4) or total_length < 28:
        raise ClosedLoopValidationError(f"{location} IPv4 total length mismatch")
    if _internet_checksum(ipv4[:20]) != 0:
        raise ClosedLoopValidationError(f"{location} IPv4 checksum mismatch")
    fragment = int.from_bytes(ipv4[6:8], "big")
    if fragment & 0x3FFF:
        raise ClosedLoopValidationError(f"{location} fragmented IPv4 is forbidden")
    if ipv4[8] == 0:
        raise ClosedLoopValidationError(f"{location} IPv4 TTL is zero")
    if ipv4[9] != 17:
        if contains_magic:
            raise ClosedLoopValidationError(
                f"{location} contains ICPC bytes outside UDP"
            )
        return None

    udp = ipv4[20:]
    source_port = int.from_bytes(udp[:2], "big")
    destination_port = int.from_bytes(udp[2:4], "big")
    udp_length = int.from_bytes(udp[4:6], "big")
    source_ipv4, destination_ipv4 = ipv4[12:16], ipv4[16:20]
    if ingress_port == 0:
        expected = (LINUX_MAC, ZEPHYR_MAC, LINUX_IPV4, ZEPHYR_IPV4)
    elif ingress_port == 1:
        expected = (ZEPHYR_MAC, LINUX_MAC, ZEPHYR_IPV4, LINUX_IPV4)
    else:
        raise ClosedLoopValidationError(f"{location} ingress_port is invalid")
    expected_source_mac, expected_destination_mac, expected_source_ip, expected_destination_ip = expected
    frozen_flow = (
        source_ipv4 in (LINUX_IPV4, ZEPHYR_IPV4)
        and destination_ipv4 in (LINUX_IPV4, ZEPHYR_IPV4)
        and (source_port == ICPC_UDP_PORT or destination_port == ICPC_UDP_PORT)
    )
    if not contains_magic and not frozen_flow:
        return None
    if (
        source_mac != expected_source_mac
        or destination_mac != expected_destination_mac
        or source_ipv4 != expected_source_ip
        or destination_ipv4 != expected_destination_ip
        or source_port != ICPC_UDP_PORT
        or destination_port != ICPC_UDP_PORT
    ):
        raise ClosedLoopValidationError(f"{location} ICPC endpoint identity mismatch")
    if udp_length != len(udp) or udp_length < 8:
        raise ClosedLoopValidationError(f"{location} UDP length mismatch")
    udp_checksum = int.from_bytes(udp[6:8], "big")
    if udp_checksum == 0:
        raise ClosedLoopValidationError(f"{location} UDP checksum is disabled")
    pseudo_header = (
        source_ipv4
        + destination_ipv4
        + b"\0\x11"
        + udp_length.to_bytes(2, "big")
    )
    if _internet_checksum(pseudo_header + udp) != 0:
        raise ClosedLoopValidationError(f"{location} UDP checksum mismatch")
    payload = udp[8:]
    if not payload.startswith(b"ICPC"):
        raise ClosedLoopValidationError(
            f"{location} UDP/46000 payload is not one ICPC packet"
        )
    return payload


def _validate_structured_network(
    root: Path, run_id: str, session_id: int
) -> list[dict[str, Any]]:
    packets = _validate_pcap(root / "network" / "capture.pcap")
    frames = _load_jsonl(root / "network" / "frames.jsonl")
    if len(frames) != len(packets):
        raise ClosedLoopValidationError("frames.jsonl count does not match capture.pcap")
    for index, (row, packet) in enumerate(zip(frames, packets, strict=True)):
        location = f"frames[{index}]"
        if row.get("schema_version") != "p4-network-frame-v1":
            raise ClosedLoopValidationError(f"{location} schema mismatch")
        if row.get("run_id") != run_id or row.get("session_id") != str(session_id):
            raise ClosedLoopValidationError(f"{location} identity mismatch")
        if row.get("sequence") != index or row.get("length") != len(packet):
            raise ClosedLoopValidationError(f"{location} sequence/length mismatch")
        frame_hex = row.get("frame_hex")
        if not isinstance(frame_hex, str):
            raise ClosedLoopValidationError(f"{location} has no frame_hex")
        try:
            decoded = bytes.fromhex(frame_hex)
        except ValueError as error:
            raise ClosedLoopValidationError(f"{location} frame_hex is invalid") from error
        if decoded != packet or row.get("sha256") != hashlib.sha256(packet).hexdigest():
            raise ClosedLoopValidationError(f"{location} bytes/hash mismatch")
        for field in ("ingress_port", "generation", "monotonic_ns"):
            if not isinstance(row.get(field), int) or isinstance(row[field], bool):
                raise ClosedLoopValidationError(f"{location}.{field} is invalid")
        ingress_port = row["ingress_port"]
        if row.get("direction") != f"port{ingress_port}_to_port{1 - ingress_port}":
            raise ClosedLoopValidationError(f"{location}.direction is invalid")
        payload = _extract_icpc_udp_payload(
            packet, ingress_port=ingress_port, location=location
        )
        if payload is not None:
            row["_icpc_wire_hex"] = payload.hex()

    return frames


def _validate_runtime_logs(root: Path) -> str:
    texts = {
        name: (root / "logs" / name).read_text(
            encoding="utf-8", errors="replace"
        )
        for name in ("axvisor.raw.log", "linux.raw.log", "zephyr.raw.log")
    }
    forbidden = (
        "kernel panic",
        "TGOS_ZEPHYR_NET_FAIL",
        "TGOS_ZEPHYR_ACK_SEND_FAIL",
        "AXVISOR_LINUX_CONSOLE_FAIL",
    )
    for name, content in texts.items():
        lowered = content.lower()
        for token in forbidden:
            if token.lower() in lowered:
                raise ClosedLoopValidationError(f"logs/{name} contains {token}")
    if "AXVISOR_DUAL_GUEST_LINUX_APP_READY" not in texts["linux.raw.log"]:
        raise ClosedLoopValidationError("Linux runtime READY marker is missing")
    if "AXVISOR_DUAL_GUEST_ZEPHYR_READY" not in texts["zephyr.raw.log"]:
        raise ClosedLoopValidationError("Zephyr runtime READY marker is missing")
    if "virtio-net TX from vm=" not in texts["axvisor.raw.log"]:
        raise ClosedLoopValidationError("AxVisor log contains no Guest TX evidence")
    if "virtio-net RX delivered=" not in texts["axvisor.raw.log"]:
        raise ClosedLoopValidationError("AxVisor log contains no Guest RX evidence")
    return texts["linux.raw.log"]


def _validate_model(root: Path, profile: Mapping[str, Any]) -> None:
    model_path = root / "model" / "model.bin"
    metadata_path = root / "model" / "metadata.json"
    golden_path = root / "model" / "golden-vectors.json"
    dataset_path = root / "model" / "dataset-manifest.json"
    expected_hashes = {
        model_path: profile["model"]["model_sha256"],
        metadata_path: profile["model"]["metadata_sha256"],
        golden_path: profile["model"]["golden_vectors_sha256"],
        dataset_path: profile["model"]["dataset_manifest_sha256"],
    }
    for path, expected in expected_hashes.items():
        if _sha256_file(path) != expected:
            raise ClosedLoopValidationError(
                f"canonical model artifact hash mismatch: {path.name}"
            )
    result = verify_model.verify_model(model_path, metadata_path, golden_path)
    if result.get("model_sha256") != profile["model"]["model_sha256"]:
        raise ClosedLoopValidationError("bundled model hash does not match profile")
    metadata = _require_mapping(_load_json(metadata_path), "model/metadata.json")
    if metadata.get("model_version") != profile["model"]["model_version"]:
        raise ClosedLoopValidationError("bundled model version does not match profile")
    dataset = _require_mapping(
        _load_json(dataset_path),
        "model/dataset-manifest.json",
    )
    if dataset.get("schema_version") != "p5-dataset-v1":
        raise ClosedLoopValidationError("dataset manifest schema is invalid")
    if dataset.get("split_seed_map") != {"train": 7, "validation": 19, "test": 43}:
        raise ClosedLoopValidationError("dataset split seeds are not canonical")
    if dataset.get("episodes_per_split") != 64 or dataset.get("ticks_per_episode") != 1800:
        raise ClosedLoopValidationError("dataset dimensions are not canonical")
    if dataset.get("sample_order") != "episode-major-tick-minor":
        raise ClosedLoopValidationError("dataset sample order is not canonical")
    splits = dataset.get("splits")
    if not isinstance(splits, list) or len(splits) != 3:
        raise ClosedLoopValidationError("dataset manifest needs three splits")
    by_name = {
        item.get("name"): item
        for item in splits
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    if set(by_name) != {"train", "validation", "test"}:
        raise ClosedLoopValidationError("dataset split names are invalid")
    metadata_hashes = _require_mapping(metadata.get("dataset_sha256"), "metadata.dataset_sha256")
    split_sizes = {"train": 7_859_500, "validation": 7_859_504, "test": 7_859_500}
    for name, seed in (("train", 7), ("validation", 19), ("test", 43)):
        split = by_name[name]
        if (
            split.get("seed") != seed
            or split.get("episodes") != 64
            or split.get("ticks_per_episode") != 1800
            or split.get("sample_count") != 115_200
            or split.get("path") != f"{name}.jsonl"
            or split.get("size_bytes") != split_sizes[name]
            or split.get("sha256") != metadata_hashes.get(name)
        ):
            raise ClosedLoopValidationError(f"dataset {name} split does not match metadata")


def _validate_manifest(
    root: Path,
    *,
    run_id: str,
    session_manifest_sha256: str,
    required_artifacts: tuple[str, ...],
) -> None:
    manifest_path = root / "manifest.json"
    manifest = _require_mapping(_load_json(manifest_path), "manifest.json")
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise ClosedLoopValidationError("manifest schema is not p5-ai-manifest-v1")
    if manifest.get("run_id") != run_id:
        raise ClosedLoopValidationError("manifest run_id mismatch")
    files = _require_mapping(manifest.get("files"), "manifest.files")
    expected_paths = set(required_artifacts) - {
        "session.json",
        "manifest.json",
        "status.json",
    }
    if set(files) != expected_paths:
        missing = sorted(expected_paths - set(files))
        extra = sorted(set(files) - expected_paths)
        raise ClosedLoopValidationError(
            f"manifest file set mismatch missing={missing} extra={extra}"
        )
    for relative, raw_entry in files.items():
        entry = _require_mapping(raw_entry, f"manifest.files[{relative}]")
        if set(entry) != {"size", "sha256", "producer"}:
            raise ClosedLoopValidationError(f"manifest entry fields invalid for {relative}")
        candidate = root / relative
        if not candidate.is_file():
            raise ClosedLoopValidationError(f"manifest references missing {relative}")
        if entry.get("size") != candidate.stat().st_size:
            raise ClosedLoopValidationError(f"manifest size mismatch for {relative}")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise ClosedLoopValidationError(f"manifest sha256 invalid for {relative}")
        if _sha256_file(candidate) != digest:
            raise ClosedLoopValidationError(f"manifest hash mismatch for {relative}")
        if not isinstance(entry.get("producer"), str) or not entry["producer"]:
            raise ClosedLoopValidationError(f"manifest producer missing for {relative}")
    actual_manifest_sha256 = _sha256_file(manifest_path)
    if actual_manifest_sha256 != session_manifest_sha256:
        raise ClosedLoopValidationError("session manifest_sha256 does not match manifest")


def _validate_status(
    path: Path, manifest_sha256: str, *, expected_status: str
) -> None:
    status = _require_mapping(_load_json(path), "status.json")
    required = {
        "schema_version",
        "success",
        "status",
        "primaryError",
        "cleanupError",
        "completedChecks",
        "manifestSha256",
        "statusLast",
    }
    if set(status) != required:
        raise ClosedLoopValidationError("status.json fields are not the frozen success schema")
    if status.get("schema_version") != STATUS_SCHEMA:
        raise ClosedLoopValidationError("status schema is not p5-ai-status-v1")
    if status.get("success") is not True or status.get("status") != expected_status:
        raise ClosedLoopValidationError(
            f"bundle does not publish expected status {expected_status}"
        )
    if status.get("primaryError") is not None or status.get("cleanupError") is not None:
        raise ClosedLoopValidationError("successful status contains an error")
    checks = status.get("completedChecks")
    if not isinstance(checks, list) or len(checks) != len(set(checks)):
        raise ClosedLoopValidationError("status completedChecks is not a unique list")
    if set(checks) != SUCCESS_CHECKS:
        raise ClosedLoopValidationError("status completedChecks must be exactly four checks")
    if status.get("manifestSha256") != manifest_sha256:
        raise ClosedLoopValidationError("status manifestSha256 does not match session")
    if status.get("statusLast") is not True:
        raise ClosedLoopValidationError("status.json is not marked status-last")


def validate_bundle(
    run_directory: Path,
    profile_path: Path,
    *,
    controller: str,
    seed: int,
    qualification: bool = True,
) -> dict[str, Any]:
    candidate_root = Path(run_directory)
    if candidate_root.is_symlink() or not candidate_root.is_dir():
        raise ClosedLoopValidationError(
            f"bundle must be a regular directory: {run_directory}"
        )
    run_directory = candidate_root.resolve()
    profile = closed_loop_contract.load_qualification_profile(profile_path.resolve())
    closed_loop_contract.validate_run_identity(
        profile=profile, controller=controller, seed=seed
    )

    required = (
        "session.json",
        "manifest.json",
        "commands.jsonl",
        "status.json",
        "cleanup.json",
        "configs/qualification-v1.json",
        "configs/fault-manifest.json",
        "configs/linux-app-config.json",
        "configs/zephyr-dotconfig",
        "configs/zephyr.dts",
        "model/model.bin",
        "model/metadata.json",
        "model/dataset-manifest.json",
        "model/golden-vectors.json",
        "logs/axvisor.raw.log",
        "logs/linux.raw.log",
        "logs/zephyr.raw.log",
        "network/capture.pcap",
        "network/frames.jsonl",
        "network/icpc.jsonl",
        "metrics/linux-events.jsonl",
        "metrics/zephyr-events.jsonl",
        "metrics/trajectory.csv",
        "metrics/summary.json",
        "metrics/recompute.txt",
        "figures/temperature.svg",
        "figures/duty.svg",
    )
    injection_artifacts = (
        "network/icpc-attempts.jsonl",
        "network/injection-transcript.json",
    )
    if all((run_directory / relative).is_file() for relative in injection_artifacts):
        required += injection_artifacts

    expected_files = set(required)
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for entry in run_directory.rglob("*"):
        if entry.is_symlink():
            raise ClosedLoopValidationError(f"bundle contains symlink: {entry}")
        relative = entry.relative_to(run_directory).as_posix()
        if entry.is_file():
            actual_files.add(relative)
        elif entry.is_dir():
            actual_dirs.add(relative)
        else:
            raise ClosedLoopValidationError(
                f"bundle contains unsupported filesystem entry: {relative}"
            )
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise ClosedLoopValidationError(
            f"bundle file set mismatch missing={missing} extra={extra}"
        )
    expected_dirs = {
        Path(relative).parent.as_posix()
        for relative in expected_files
        if Path(relative).parent != Path(".")
    }
    if actual_dirs != expected_dirs:
        raise ClosedLoopValidationError("bundle directory set drifted")
    for relative in required:
        _require_file(run_directory / relative)

    copied_profile = closed_loop_contract.load_qualification_profile(
        run_directory / "configs" / "qualification-v1.json"
    )
    if copied_profile != profile:
        raise ClosedLoopValidationError("bundled qualification profile does not match input")
    expected_fault_manifest = closed_loop_contract.generate_fault_manifest(
        profile=profile, seed=seed
    )
    if _load_json(run_directory / "configs" / "fault-manifest.json") != expected_fault_manifest:
        raise ClosedLoopValidationError("bundled paired fault manifest is invalid")

    session = _load_json(run_directory / "session.json")
    if not isinstance(session, dict):
        raise ClosedLoopValidationError("session.json must be an object")
    if session.get("schema_version") != SESSION_SCHEMA:
        raise ClosedLoopValidationError("session schema is not p5-ai-session-v1")
    expected_session_fields = {
        "schema_version",
        "run_id",
        "session_id",
        "scenario",
        "controller",
        "seed",
        "profile_id",
        "execution_kind",
        "evidence_level",
        "manifest_sha256",
    }
    if set(session) != expected_session_fields:
        raise ClosedLoopValidationError("session fields are not the frozen schema")
    run_id = session.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ClosedLoopValidationError("session.json has no nonempty run_id")
    session_id = session.get("session_id")
    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id <= 0:
        raise ClosedLoopValidationError("session.json has no positive session_id")
    if session.get("controller") != controller or session.get("seed") != seed:
        raise ClosedLoopValidationError("session controller/seed does not match invocation")
    if session.get("scenario") != "test-017":
        raise ClosedLoopValidationError("session scenario is not test-017")
    if session.get("profile_id") != profile["profile_id"]:
        raise ClosedLoopValidationError("session profile_id mismatch")
    expected_execution_kind = "guest_runtime" if qualification else "host_contract"
    expected_evidence_level = (
        profile["evidence"]["level"] if qualification else HOST_CONTRACT_LEVEL
    )
    expected_status = (
        profile["evidence"]["success_status"]
        if qualification
        else HOST_CONTRACT_STATUS
    )
    if session.get("execution_kind") != expected_execution_kind:
        raise ClosedLoopValidationError("session execution_kind mismatch")
    if session.get("evidence_level") != expected_evidence_level:
        raise ClosedLoopValidationError("session evidence_level mismatch")
    if qualification:
        required += tuple(relative for relative in injection_artifacts if relative not in required)
        for relative in injection_artifacts:
            _require_file(run_directory / relative)
    manifest_sha256 = session.get("manifest_sha256")
    if not isinstance(manifest_sha256, str) or SHA256.fullmatch(manifest_sha256) is None:
        raise ClosedLoopValidationError("session manifest_sha256 is invalid")

    _validate_commands(run_directory / "commands.jsonl")
    _validate_cleanup(run_directory / "cleanup.json")
    _validate_model(run_directory, profile)
    network_frames = _validate_structured_network(run_directory, run_id, session_id)
    linux_log = _validate_runtime_logs(run_directory)

    trajectory_path = run_directory / "metrics" / "trajectory.csv"
    try:
        with trajectory_path.open(newline="", encoding="utf-8") as source:
            trajectory = list(csv.DictReader(source))
    except (OSError, csv.Error) as error:
        raise ClosedLoopValidationError(f"cannot read trajectory: {error}") from error
    mlp_weights = (
        model.load_model(run_directory / "model" / "model.bin")[1]
        if controller == "mlp"
        else None
    )
    checked = closed_loop_contract.validate_trajectory(
        trajectory,
        profile=profile,
        controller=controller,
        seed=seed,
        mlp_infer=(
            None
            if controller == "fixed"
            else (
                lambda measured_mC, target_mC, previous_duty_q16_16: model.infer_duty_q16_16(
                    mlp_weights,
                    measured_mC,
                    target_mC,
                    previous_duty_q16_16,
                )
            )
        ),
    )

    linux_events = _load_jsonl(run_directory / "metrics" / "linux-events.jsonl")
    zephyr_events = _load_jsonl(run_directory / "metrics" / "zephyr-events.jsonl")
    closed_loop_contract.validate_event_associations(
        linux_events,
        zephyr_events,
        trajectory=checked,
        run_id=run_id,
        controller=controller,
    )
    icpc_records = _load_jsonl(run_directory / "network" / "icpc.jsonl")
    closed_loop_contract.validate_icpc_records(
        icpc_records,
        network_frames,
        trajectory=checked,
        linux_events=linux_events,
        zephyr_events=zephyr_events,
        profile=profile,
        fault_manifest=expected_fault_manifest,
        run_id=run_id,
        session_id=session_id,
        controller=controller,
    )
    if qualification:
        try:
            expected_transcript = produce_icpc_records.build_injection_transcript(
                icpc_records,
                run_id=run_id,
                session_id=session_id,
                raw_attempts=(run_directory / "network" / "icpc-attempts.jsonl").read_bytes(),
                icpc_records=(run_directory / "network" / "icpc.jsonl").read_bytes(),
                fault_manifest_bytes=(run_directory / "configs" / "fault-manifest.json").read_bytes(),
                fault_manifest=expected_fault_manifest,
            )
        except (OSError, ValueError) as error:
            raise ClosedLoopValidationError(f"injection transcript contract failed: {error}") from error
        if _load_json(run_directory / "network" / "injection-transcript.json") != expected_transcript:
            raise ClosedLoopValidationError("injection-transcript.json does not equal independent recomputation")

    marker_lines = []
    for line in linux_log.splitlines():
        stripped = line.strip()
        marker = "[VM 1] "
        marker_index = stripped.find(marker)
        payload = (
            stripped[marker_index + len(marker) :]
            if marker_index >= 0
            else stripped
        )
        if payload.startswith("TGOS_LINUX_TRAJ_DONE"):
            marker_lines.append(payload)
    if len(marker_lines) != 1:
        raise ClosedLoopValidationError(
            f"expected exactly one completion marker, got {len(marker_lines)}"
        )
    completion = closed_loop_contract.parse_completion_marker(marker_lines[0])
    closed_loop_contract.validate_completion_fields(
        completion, profile=profile, controller=controller, seed=seed
    )

    metrics = compute_metrics.compute_metrics(
        checked,
        target_mC=int(profile["plant"]["target_mC"]),
        band_mC=int(profile["metrics"]["error_band_mC"]),
        hold_ticks=int(profile["metrics"]["settling_hold_ticks"]),
        expected_ticks=int(profile["timing"]["ticks"]),
        recovery_start_tick=int(profile["metrics"]["recovery_start_tick"]),
        overshoot_step_mC=int(profile["metrics"]["overshoot_step_mC"]),
    )
    summary = _load_json(run_directory / "metrics" / "summary.json")
    if summary != metrics:
        raise ClosedLoopValidationError("summary.json does not equal raw metric recomputation")

    _validate_manifest(
        run_directory,
        run_id=run_id,
        session_manifest_sha256=manifest_sha256,
        required_artifacts=required,
    )
    _validate_status(
        run_directory / "status.json",
        manifest_sha256,
        expected_status=expected_status,
    )

    return {
        "schema_version": "p5-ai-validation-v1",
        "valid": True,
        "run_id": run_id,
        "controller": controller,
        "seed": seed,
        "ticks": len(checked),
        "requests": len({int(row["applied_request_id"]) for row in checked}),
        "evidence_level": expected_evidence_level,
        "status": expected_status,
    }


def _validate_host_manifest(
    root: Path, *, run_id: str, session_manifest_sha256: str
) -> None:
    """Validate the smaller C0 host bundle without expecting runtime artifacts."""

    manifest = _require_mapping(_load_json(root / "manifest.json"), "manifest.json")
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise ClosedLoopValidationError("host manifest schema is not p5-ai-manifest-v1")
    if manifest.get("run_id") != run_id:
        raise ClosedLoopValidationError("host manifest run_id mismatch")
    expected = {
        "commands.jsonl",
        "cleanup.json",
        "configs/qualification-v1.json",
        "configs/fault-manifest.json",
        "metrics/host-cycles.jsonl",
        "metrics/summary.json",
        "model/model.bin",
        "model/metadata.json",
        "model/dataset-manifest.json",
        "model/golden-vectors.json",
    }
    files = _require_mapping(manifest.get("files"), "manifest.files")
    optional = {"configs/runner-preflight.json"}
    actual = set(files)
    if not expected.issubset(actual) or actual - expected - optional:
        raise ClosedLoopValidationError(
            f"host manifest file set mismatch missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )
    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    expected_files = actual | {"session.json", "manifest.json", "status.json"}
    if actual_files != expected_files:
        raise ClosedLoopValidationError(
            f"host bundle file set mismatch missing={sorted(expected_files - actual_files)} "
            f"extra={sorted(actual_files - expected_files)}"
        )
    expected_directories: set[str] = set()
    for relative in actual_files:
        parts = relative.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            expected_directories.add("/".join(parts[:index]))
    actual_directories = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir()
    }
    if actual_directories != expected_directories:
        raise ClosedLoopValidationError("host bundle directory set drifted")
    for relative, raw_entry in files.items():
        entry = _require_mapping(raw_entry, f"manifest.files[{relative}]")
        if set(entry) != {"size", "sha256", "producer"}:
            raise ClosedLoopValidationError(f"host manifest entry fields invalid for {relative}")
        candidate = root / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise ClosedLoopValidationError(f"host manifest references missing {relative}")
        if entry.get("size") != candidate.stat().st_size:
            raise ClosedLoopValidationError(f"host manifest size mismatch for {relative}")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise ClosedLoopValidationError(f"host manifest sha256 invalid for {relative}")
        if _sha256_file(candidate) != digest:
            raise ClosedLoopValidationError(f"host manifest hash mismatch for {relative}")
        if not isinstance(entry.get("producer"), str) or not entry["producer"]:
            raise ClosedLoopValidationError(f"host manifest producer missing for {relative}")
    preflight_path = root / "configs" / "runner-preflight.json"
    if "configs/runner-preflight.json" in actual:
        preflight = _require_mapping(_load_json(preflight_path), "runner-preflight.json")
        if preflight.get("schema_version") != "p5-ai-runner-preflight-v1":
            raise ClosedLoopValidationError("runner preflight schema is invalid")
        if preflight.get("run_id") != run_id:
            raise ClosedLoopValidationError("runner preflight run_id mismatch")
        if preflight.get("scenario") != "test-017":
            raise ClosedLoopValidationError("runner preflight scenario mismatch")
        if preflight.get("execution_kind") != "host_contract":
            raise ClosedLoopValidationError("runner preflight execution kind is invalid")
        if preflight.get("execution") != "not_started" or preflight.get("qualified") is not False:
            raise ClosedLoopValidationError("runner preflight must remain not_started and unqualified")
        if preflight.get("timeout_seconds") != 600:
            raise ClosedLoopValidationError("runner preflight timeout mismatch")
        if not isinstance(preflight.get("inputs"), dict) or not preflight["inputs"]:
            raise ClosedLoopValidationError("runner preflight inputs are missing")
    if _sha256_file(root / "manifest.json") != session_manifest_sha256:
        raise ClosedLoopValidationError("host session manifest_sha256 does not match manifest")


def validate_host_bundle(
    run_directory: Path,
    profile_path: Path,
    *,
    controller: str,
    seed: int,
) -> dict[str, Any]:
    """Independently validate the deterministic L2 host-only C0 bundle."""

    candidate_root = Path(run_directory)
    if candidate_root.is_symlink() or not candidate_root.is_dir():
        raise ClosedLoopValidationError(
            f"host bundle must be a regular directory: {run_directory}"
        )
    root = candidate_root.resolve()
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise ClosedLoopValidationError(f"host bundle contains symlink: {entry}")
    profile = closed_loop_contract.load_qualification_profile(profile_path.resolve())
    closed_loop_contract.validate_run_identity(profile=profile, controller=controller, seed=seed)
    required = (
        "session.json",
        "manifest.json",
        "commands.jsonl",
        "status.json",
        "cleanup.json",
        "configs/qualification-v1.json",
        "configs/fault-manifest.json",
        "metrics/host-cycles.jsonl",
        "metrics/summary.json",
        "model/model.bin",
        "model/metadata.json",
        "model/dataset-manifest.json",
        "model/golden-vectors.json",
    )
    for relative in required:
        _require_file(root / relative)

    copied_profile = closed_loop_contract.load_qualification_profile(
        root / "configs" / "qualification-v1.json"
    )
    if copied_profile != profile:
        raise ClosedLoopValidationError("host bundled qualification profile does not match input")
    expected_fault_manifest = closed_loop_contract.generate_fault_manifest(
        profile=profile, seed=seed
    )
    if _load_json(root / "configs" / "fault-manifest.json") != expected_fault_manifest:
        raise ClosedLoopValidationError("host bundled fault manifest is invalid")

    session = _require_mapping(_load_json(root / "session.json"), "session.json")
    if session.get("schema_version") != SESSION_SCHEMA:
        raise ClosedLoopValidationError("host session schema is not p5-ai-session-v1")
    expected_session_fields = {
        "schema_version",
        "run_id",
        "session_id",
        "scenario",
        "controller",
        "seed",
        "profile_id",
        "execution_kind",
        "evidence_level",
        "manifest_sha256",
    }
    if set(session) != expected_session_fields:
        raise ClosedLoopValidationError("host session fields are not frozen")
    run_id = session.get("run_id")
    session_id = session.get("session_id")
    if not isinstance(run_id, str) or not run_id:
        raise ClosedLoopValidationError("host session run_id is invalid")
    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id <= 0:
        raise ClosedLoopValidationError("host session_id is invalid")
    if session.get("scenario") != "test-017":
        raise ClosedLoopValidationError("host scenario is not test-017")
    if session.get("controller") != controller or session.get("seed") != seed:
        raise ClosedLoopValidationError("host session controller/seed mismatch")
    if session.get("profile_id") != profile["profile_id"]:
        raise ClosedLoopValidationError("host session profile mismatch")
    if session.get("execution_kind") != "host_contract":
        raise ClosedLoopValidationError("host bundle cannot claim guest_runtime")
    if session.get("evidence_level") != HOST_CONTRACT_LEVEL:
        raise ClosedLoopValidationError("host bundle evidence level mismatch")
    manifest_sha256 = session.get("manifest_sha256")
    if not isinstance(manifest_sha256, str) or SHA256.fullmatch(manifest_sha256) is None:
        raise ClosedLoopValidationError("host session manifest hash is invalid")

    _validate_commands(root / "commands.jsonl")
    _validate_cleanup(root / "cleanup.json")
    _validate_model(root, profile)
    cycles = _load_jsonl(root / "metrics" / "host-cycles.jsonl")
    trajectory: list[dict[str, Any]] = []
    for index, cycle in enumerate(cycles):
        if cycle.get("schema_version") != host_orchestration.HOST_CYCLE_SCHEMA:
            raise ClosedLoopValidationError(f"host cycle {index} schema mismatch")
        for field, expected in (
            ("run_id", run_id),
            ("session_id", session_id),
            ("scenario", "test-017"),
            ("controller", controller),
            ("seed", seed),
        ):
            if cycle.get(field) != expected:
                raise ClosedLoopValidationError(f"host cycle {index} {field} mismatch")
        trajectory.append(
            {
                "controller": controller,
                "seed": seed,
                "sample_index": cycle.get("sample_index"),
                "measured_mC": cycle.get("measured_mC"),
                "target_mC": cycle.get("target_mC"),
                "duty_q16_16": cycle.get("duty_q16_16"),
                "model_version": cycle.get("model_version"),
                "health_flags": cycle.get("health_flags"),
                "applied_request_id": cycle.get("request_id"),
                "feedback_confirmed": 1 if cycle.get("feedback_confirmed") else 0,
                "closed_loop_rtt_ns": 0,
                "action_latency_ns": 0,
            }
        )
    mlp_weights = (
        model.load_model(root / "model" / "model.bin")[1]
        if controller == "mlp"
        else None
    )
    checked = closed_loop_contract.validate_trajectory(
        trajectory,
        profile=profile,
        controller=controller,
        seed=seed,
        mlp_infer=(
            None
            if controller == "fixed"
            else (
                lambda measured_mC, target_mC, previous_duty_q16_16: model.infer_duty_q16_16(
                    mlp_weights,
                    measured_mC,
                    target_mC,
                    previous_duty_q16_16,
                )
            )
        ),
    )
    summary = _require_mapping(_load_json(root / "metrics" / "summary.json"), "metrics/summary.json")
    expected_summary = host_orchestration.HostRun(
        config=host_orchestration.HostContractConfig.from_paths(
            identity=host_orchestration.HostIdentity(
                run_id=run_id,
                session_id=session_id,
                controller=controller,
                seed=seed,
            ),
            profile_path=profile_path,
            model_path=root / "model" / "model.bin",
        ),
        cycles=tuple(
            host_orchestration.HostCycle(
                sample_index=int(row["sample_index"]),
                request_id=int(row["applied_request_id"]),
                release_ms=int(cycle["release_ms"]),
                period_start_ms=int(cycle["period_start_ms"]),
                inference_start_ms=cycle.get("inference_start_ms"),
                inference_finish_ms=cycle.get("inference_finish_ms"),
                control_apply_ms=int(cycle["control_apply_ms"]),
                period_finish_ms=int(cycle["period_finish_ms"]),
                input_temperature_mC=int(cycle["input_temperature_mC"]),
                measured_mC=int(cycle["measured_mC"]),
                target_mC=int(cycle["target_mC"]),
                previous_duty_q16_16=int(cycle["previous_duty_q16_16"]),
                duty_q16_16=int(cycle["duty_q16_16"]),
                model_version=int(cycle["model_version"]),
                fault_drop_scheduled=bool(cycle["fault_drop_scheduled"]),
            )
            for row, cycle in zip(trajectory, cycles, strict=True)
        ),
    ).summary()
    if summary != expected_summary:
        raise ClosedLoopValidationError("host summary does not equal deterministic recomputation")

    _validate_host_manifest(root, run_id=run_id, session_manifest_sha256=manifest_sha256)
    _validate_status(root / "status.json", manifest_sha256, expected_status=HOST_CONTRACT_STATUS)
    return {
        "schema_version": "p5-ai-host-validation-v1",
        "valid": True,
        "run_id": run_id,
        "controller": controller,
        "seed": seed,
        "ticks": len(checked),
        "evidence_level": HOST_CONTRACT_LEVEL,
        "status": HOST_CONTRACT_STATUS,
        "runtime_evidence": "not_produced",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--controller", required=True, choices=("fixed", "mlp"))
    parser.add_argument("--seed", required=True, type=int, choices=(7, 19, 43))
    parser.add_argument(
        "--host-contract",
        action="store_true",
        help="validate the L2 host-only C0 bundle instead of a Guest runtime bundle",
    )
    args = parser.parse_args(argv)
    try:
        result = (
            validate_host_bundle(
                args.run_dir,
                args.profile,
                controller=args.controller,
                seed=args.seed,
            )
            if args.host_contract
            else validate_bundle(
                args.run_dir,
                args.profile,
                controller=args.controller,
                seed=args.seed,
            )
        )
    except (ClosedLoopValidationError, OSError, ValueError) as error:
        print(f"P5 closed-loop validation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    print("P5_CLOSED_LOOP_VALIDATION_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
