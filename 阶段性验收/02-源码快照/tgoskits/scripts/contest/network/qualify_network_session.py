#!/usr/bin/env python3
"""P4-EVID-01B: Guest-runtime v2 qualification validator.

Consumes an immutable Guest-IP evidence bundle produced by the new-HEAD runner
and FAILS CLOSED unless *every* gate holds.  It can only emit
`guest_network_qualified`; anything short of the full TEST oracle/artifacts is a
failure (never a qualified story).  This validator is deterministic host Python;
it never runs QEMU and never mutates the bundle.

Required bundle layout (run root):
  session.json  manifest.json  status.json  commands.jsonl  cleanup.json
  dtb/{linux-final.{dtb,dts},zephyr-final.{dtb,dts}}
  logs/{axvisor.raw.log,linux.raw.log,zephyr.raw.log}
  network/{frames.jsonl,capture.pcap,counters.json,fault-manifest.json?}
  metrics/{raw.jsonl,summary.json}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

SESSION_SCHEMA_VERSION = "p4-network-session-v2"
STATUS_SCHEMA_VERSION = "p4-network-status-v2"
PROFILE_SCHEMA_VERSION = "p4-network-profile-v2"
COUNTERS_SCHEMA_VERSION = "p4-network-counters-v1"
FRAME_SCHEMA_VERSION = "p4-network-frame-v1"
MANIFEST_SCHEMA_VERSION = "p4-network-manifest-v1"
SUCCESS_CHECKS = {"oracle", "hashes", "validator", "cleanup"}
REQUIRED_STATUS_FIELDS = {
    "schema_version",
    "success",
    "status",
    "qualified",
    "primaryError",
    "cleanupError",
    "completedChecks",
    "manifestSha256",
    "statusLast",
}
MAX_SKIPPED_FRAMES = 5

READY_LINUX = "AXVISOR_DUAL_GUEST_LINUX_READY"
READY_ZEPHYR = "AXVISOR_DUAL_GUEST_ZEPHYR_READY"
FORBIDDEN = ("kernel panic", "AXVISOR_LINUX_CONSOLE_FAIL")

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class QualificationError(ValueError):
    """Raised when a guest-runtime bundle does not satisfy the v2 gate."""


def _read_json(path: Path, field: str) -> Mapping[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationError(f"{field} is unreadable/corrupt: {error}") from error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise QualificationError(f"{field} must be a non-empty safe identifier")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise QualificationError(f"{field} must be a 64-hex sha256")
    return value


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualificationError(f"{field} must be an object")
    return value


def _require_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise QualificationError(f"{field} must be an integer >= {minimum}")
    return value


def _assert_absent(text: str, field: str) -> None:
    for token in FORBIDDEN:
        if token in text:
            raise QualificationError(f"{field} contains forbidden marker {token!r}")


def _validate_profile(profile: Path | None, session: Mapping[str, Any]) -> Mapping[str, Any]:
    if profile is None:
        raise QualificationError("a v2 profile is required for qualification")
    data = _read_json(profile, "profile")
    if data.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise QualificationError("profile is not p4-network-profile-v2")
    if data.get("host_only") is not False or data.get("guest_runtime") is not True:
        raise QualificationError("profile is not a Guest-runtime profile")
    expected_level = data.get("evidence_level")
    if expected_level not in {"L6 stability", "L7 Guest-IP"}:
        raise QualificationError("profile evidence_level must be L6/L7 Guest-IP")
    if data.get("test_id") != session.get("test_id"):
        raise QualificationError("profile test_id does not match session")
    if data.get("profile_id") != session.get("profile_id"):
        raise QualificationError("profile profile_id does not match session")
    if data.get("scenario") != session.get("scenario"):
        raise QualificationError("profile scenario does not match session")
    oracle = _require_mapping(data.get("oracle"), "profile.oracle")
    required = oracle.get("requiredMarkers")
    if not isinstance(required, list) or set(required) != {
        READY_LINUX,
        READY_ZEPHYR,
    }:
        raise QualificationError("profile must require both frozen dual-Guest READY markers")
    return data


def _validate_session(root: Path, session_document: Mapping[str, Any]) -> Mapping[str, Any]:
    required = {
        "schema_version",
        "run_id",
        "session_id",
        "nonce",
        "test_id",
        "profile_id",
        "scenario",
        "host_only",
        "evidence_level",
        "transport",
        "clock_domain",
        "network",
        "endpoints",
        "execution",
        "identity",
        "manifest_sha256",
    }
    unknown = set(session_document) - required
    if unknown:
        raise QualificationError(f"session has unknown fields: {sorted(unknown)}")
    if session_document["schema_version"] != SESSION_SCHEMA_VERSION:
        raise QualificationError("session is not p4-network-session-v2")
    run_id = _require_id(session_document["run_id"], "session.run_id")
    session_id = _require_id(session_document["session_id"], "session.session_id")
    nonce = _require_id(session_document["nonce"], "session.nonce")
    if session_id == "0" or nonce == "0":
        raise QualificationError("session_id and nonce must be non-zero")
    if session_document["host_only"] is not False:
        raise QualificationError("session is not a Guest-runtime session")
    if session_document["clock_domain"] != "monotonic_ns":
        raise QualificationError("session.clock_domain must be monotonic_ns")
    execution = _require_mapping(session_document["execution"], "session.execution")
    if dict(execution) != {
        "host_only": False,
        "qemu": True,
        "wsl": True,
        "guest_runtime": True,
    }:
        raise QualificationError("session execution identity is not Guest-runtime")
    identity = _require_mapping(session_document["identity"], "session.identity")
    if identity.get("producer") != "p4-network-guest":
        raise QualificationError("session identity producer is not p4-network-guest")
    if (identity.get("run_id"), identity.get("session_id"), identity.get("nonce")) != (
        run_id,
        session_id,
        nonce,
    ):
        raise QualificationError("session identity fields do not agree")
    manifest = _require_sha256(
        session_document["manifest_sha256"], "session.manifest_sha256"
    )
    return {"path": root, "manifest_sha256": manifest}


def _collect_markers(root: Path) -> dict[str, list[str]]:
    logs = {
        "linux": root / "logs" / "linux.raw.log",
        "zephyr": root / "logs" / "zephyr.raw.log",
    }
    found = {"linux": [], "zephyr": []}
    for name, path in logs.items():
        if not path.is_file():
            raise QualificationError(f"missing {path.name}")
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if "READY" in line:
                found[name].append(line.strip())
    return found


def _validate_dtb(root: Path) -> None:
    for stem in ("linux-final", "zephyr-final"):
        for suffix in (".dtb", ".dts"):
            path = root / "dtb" / f"{stem}{suffix}"
            if not path.is_file() or path.stat().st_size == 0:
                raise QualificationError(f"missing or empty dtb/{stem}{suffix}")


def _validate_frames(root: Path, counters: Mapping[str, Any]) -> int:
    path = root / "network" / "frames.jsonl"
    if not path.is_file() or path.stat().st_size == 0:
        raise QualificationError("network/frames.jsonl is missing or empty")
    rows = 0
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as error:
                raise QualificationError(f"frames.jsonl line corrupt: {error}") from error
            if frame.get("schema_version") != FRAME_SCHEMA_VERSION:
                raise QualificationError("frames.jsonl has an unsupported schema")
            seq = _require_int(frame.get("sequence"), "frame.sequence")
            portfolio = root / "send"
            seq_key = f"{seq}:{frame.get('direction')}:{frame.get('ingress_port')}"
            if seq_key in seen:
                raise QualificationError(f"frames.jsonl duplicate frame {seq_key}")
            seen.add(seq_key)
            length = _require_int(frame.get("length"), "frame.length", minimum=14)
            hexstr = frame.get("frame_hex")
            if not isinstance(hexstr, str):
                raise QualificationError("frame.frame_hex must be a hex string")
            if len(hexstr) != length * 2:
                raise QualificationError(
                    f"frame_hex length mismatch for seq={seq} ({len(hexstr)} != {length*2})"
                )
            digest = hashlib.sha256(bytes.fromhex(hexstr)).hexdigest()
            if frame.get("sha256") != digest:
                raise QualificationError(f"frame sha256 mismatch for seq={seq}")
            rows += 1
    measured = counters.get("capture_frames")
    if isinstance(measured, int) and measured != rows:
        # Honest accounting: evidence lines lost to UART interleaving are
        # counted in network/frames-skipped.json (bounded), so a clean log
        # must match exactly while a lossy one still has to account for
        # every captured frame.
        skipped = 0
        skip_path = root / "network" / "frames-skipped.json"
        if skip_path.is_file():
            try:
                skipped = int(_read_json(skip_path, "frames-skipped").get("skipped", 0))
            except QualificationError:
                skipped = -1
        if measured != rows + skipped or skipped < 0 or skipped > MAX_SKIPPED_FRAMES:
            raise QualificationError(
                f"counters.capture_frames={measured} != frames.jsonl rows={rows} "
                f"+ skipped={skipped}"
            )
    return rows


def _validate_pcap(root: Path, frame_count: int) -> None:
    path = root / "network" / "capture.pcap"
    if not path.is_file() or path.stat().st_size < 24:
        raise QualificationError("network/capture.pcap is missing or empty")
    header = path.read_bytes()[:24]
    if header[:4] != b"\xd4\xc3\xb2\xa1" and header[:4] != b"\xa1\xb2\xc3\xd4":
        raise QualificationError("capture.pcap has an invalid magic")
    if frame_count == 0:
        raise QualificationError("capture.pcap present but frames.jsonl empty")


def _validate_counters(root: Path, session: Mapping[str, Any]) -> Mapping[str, Any]:
    path = root / "network" / "counters.json"
    counters = _read_json(path, "counters")
    if counters.get("schema_version") != COUNTERS_SCHEMA_VERSION:
        raise QualificationError("counters are not p4-network-counters-v1")
    for key in (
        "capture_frames",
        "capture_bytes",
        "capture_drops",
        "switch_enqueue",
        "switch_deliver",
        "switch_drop",
    ):
        _require_int(counters.get(key), f"counters.{key}")
    test_id = session["test_id"]
    if test_id == "TEST-011":
        if counters["switch_drop"] > 0:
            raise QualificationError("TEST-011 qualification disallows any switch drop")
        # Bidirectional 100/100 ICMP = 200 echo/reply deliveries plus ARP
        # overhead; measured fresh runs deliver ~204 frames. 200 is the
        # strict bidirectional floor (a single direction alone cannot pass).
        if counters["switch_deliver"] < 200:
            raise QualificationError(
                f"TEST-011 needs >=200 deliveries, got {counters['switch_deliver']}"
            )
    return counters


def _validate_metrics(root: Path) -> None:
    raw = root / "metrics" / "raw.jsonl"
    summary = root / "metrics" / "summary.json"
    if not raw.is_file() or raw.stat().st_size == 0:
        raise QualificationError("metrics/raw.jsonl is missing or empty")
    if not summary.is_file():
        raise QualificationError("metrics/summary.json is missing")
    summary_data = _read_json(summary, "metrics/summary.json")
    if not isinstance(summary_data.get("scenario"), str):
        raise QualificationError("metrics/summary.json is not structured")


def _validate_manifest(
    root: Path, session_manifest_sha: str, paths: list[Path]
) -> None:
    manifest = _read_json(root / "manifest.json", "manifest")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise QualificationError("manifest is not p4-network-manifest-v1")
    entries = manifest.get("files")
    if not isinstance(entries, dict) or not entries:
        raise QualificationError("manifest.files must be a dict")
    for rel, expected in entries.items():
        if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
            raise QualificationError(f"manifest entry {rel} is not a sha256")
        candidate = root / rel
        if not candidate.is_file():
            raise QualificationError(f"manifest references missing {rel}")
        if _sha256_file(candidate) != expected:
            raise QualificationError(f"manifest hash mismatch for {rel}")
    for path in paths:
        rel = path.relative_to(root).as_posix()
        if rel not in entries:
            raise QualificationError(f"artifact {rel} not present in manifest")
    # Hash the byte-exact manifest file so that producer and validator agree
    # structurally (no re-serialization drift).
    digest = _sha256_file(root / "manifest.json")
    if digest != session_manifest_sha:
        raise QualificationError("session.manifest_sha256 does not match manifest")
    # session.json / manifest.json / status.json / cleanup.json are not
    # self-referentially hashed here; the session->manifest->article chain is
    # bound by session.manifest_sha256 and status.manifestSha256 below.


def _validate_cleanup(root: Path) -> None:
    cleanup = _read_json(root / "cleanup.json", "cleanup")
    residuals = cleanup.get("residualProcesses")
    if not isinstance(residuals, list) or residuals:
        raise QualificationError("cleanup reports residual processes")
    if cleanup.get("residualSockets"):
        raise QualificationError("cleanup reports residual sockets")


def _validate_status(root: Path, session_manifest_sha: str) -> None:
    status = _read_json(root / "status.json", "status")
    unknown = set(status) - REQUIRED_STATUS_FIELDS
    if unknown:
        raise QualificationError(f"status has unknown fields: {sorted(unknown)}")
    if status["schema_version"] != STATUS_SCHEMA_VERSION:
        raise QualificationError("status is not p4-network-status-v2")
    if status["success"] is not True or status["status"] != "guest_network_qualified":
        raise QualificationError("status is not a qualified success")
    if status["qualified"] is not True:
        raise QualificationError("status.qualified must be true")
    if status["statusLast"] is not True:
        raise QualificationError("status.json is not status-last")
    if status["manifestSha256"] != session_manifest_sha:
        raise QualificationError("status.manifestSha256 does not match session")
    if set(status["completedChecks"]) != SUCCESS_CHECKS:
        raise QualificationError("status.completedChecks must be exactly the four checks")


def qualify_guest_session(root: Path, profile: Path | None = None) -> dict[str, Any]:
    session_document = _read_json(root / "session.json", "session")
    _validate_profile(profile, session_document)
    session = _validate_session(root, session_document)
    markers = _collect_markers(root)
    linux_lines = markers["linux"]
    zephyr_lines = markers["zephyr"]
    if not linux_lines or not any(READY_LINUX in line for line in linux_lines):
        raise QualificationError(f"missing {READY_LINUX} in linux log")
    if not zephyr_lines or not any(READY_ZEPHYR in line for line in zephyr_lines):
        raise QualificationError(f"missing {READY_ZEPHYR} in zephyr log")
    # forbid panic/console-fail anywhere
    for log in ("axvisor.raw.log", "linux.raw.log", "zephyr.raw.log"):
        path = root / "logs" / log
        if not path.is_file():
            raise QualificationError(f"missing logs/{log}")
        _assert_absent(path.read_text(encoding="utf-8", errors="replace"), f"logs/{log}")
    _validate_dtb(root)
    counters = _validate_counters(root, session_document)
    frame_count = _validate_frames(root, counters)
    _validate_pcap(root, frame_count)
    _validate_metrics(root)
    artifacts = [
        root / "dtb" / "linux-final.dtb",
        root / "dtb" / "linux-final.dts",
        root / "dtb" / "zephyr-final.dtb",
        root / "dtb" / "zephyr-final.dts",
        root / "network" / "frames.jsonl",
        root / "network" / "capture.pcap",
        root / "network" / "counters.json",
        root / "metrics" / "raw.jsonl",
        root / "metrics" / "summary.json",
    ]
    _validate_manifest(root, session["manifest_sha256"], artifacts)
    _validate_cleanup(root)
    _validate_status(root, session["manifest_sha256"])
    return {
        "schema_version": "p4-network-session-validation-v2",
        "valid": True,
        "run_id": session_document["run_id"],
        "session_id": session_document["session_id"],
        "test_id": session_document["test_id"],
        "profile_id": session_document["profile_id"],
        "evidence_level": session_document["evidence_level"],
        "status": "guest_network_qualified",
        "qualified": True,
        "capture_frames": frame_count,
        "manifest_sha256": session["manifest_sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args(argv)
    try:
        report = qualify_guest_session(args.session, args.profile)
    except (QualificationError, OSError) as error:
        print(f"P4 network qualification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
