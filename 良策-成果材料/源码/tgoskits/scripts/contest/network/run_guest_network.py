#!/usr/bin/env python3
"""P4 guest-network runner for TEST-011..015.

Launches one identity-bound dual-Guest QEMU with the mediated VirtIO-net
profile and the prepared Linux/Zephyr network images, waits for the
scenario's completion markers in the demuxed guest consoles, then publishes
an IF-011 status-last evidence bundle.  Never reuses an output directory;
every input is byte-bound to its manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from runtime.dual_guest import (
        RuntimeContractError,
        build_axvisor_qemu_command,
        inject_qemu_identity_args,
        request_bounded_shutdown,
        validate_dual_guest_log,
    )
    from host_vm_carveout_io import publish_new_file
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from runtime.dual_guest import (  # type: ignore[no-redef]
        RuntimeContractError,
        build_axvisor_qemu_command,
        inject_qemu_identity_args,
        request_bounded_shutdown,
        validate_dual_guest_log,
    )
    from host_vm_carveout_io import publish_new_file  # type: ignore[no-redef]

try:
    from input_binding import (
        SoakBindingError,
        require_matching_binding,
        validate_current_qemu_config,
        validate_soak_binding,
    )
except ImportError:  # pragma: no cover - package import path
    from .input_binding import (
        SoakBindingError,
        require_matching_binding,
        validate_current_qemu_config,
        validate_soak_binding,
    )

RUN_SCHEMA_VERSION = 1
SCENARIOS = ("test-011", "test-012", "test-013", "test-015", "test-018-safe")
SCENARIO_IMAGE_MODES = {
    "test-011": ("l3-smoke", "l3-smoke"),
    "test-012": ("udp-echo", "udp-echo"),
    "test-013": ("tcp-client", "tcp-listen"),
    # The Zephyr UDP server decodes ICPC CONTROL and emits ACK + STATUS.
    "test-015": ("icpc", "udp-echo"),
    "test-018-safe": ("icpc-safe", "udp-echo"),
}
TOKENS = {
    "ok": "guest_network_smoke_completed",
    "failed": "guest_network_failed",
    "blocked": "guest_network_blocked",
}
SMOKE_ORACLES = {
    "test-011": {
        "testId": "TEST-011",
        "profileScenario": "l3-smoke",
        "marker": "TGOS_LINUX_L3_SMOKE",
        "pattern": re.compile(
            r"^TGOS_LINUX_L3_SMOKE sent=(?P<sent>\d+) "
            r"received=(?P<received>\d+) loss=(?P<loss>\d+)$"
        ),
    },
    "test-012": {
        "testId": "TEST-012",
        "profileScenario": "udp-echo",
        "marker": "TGOS_LINUX_UDP_ECHO",
        "pattern": re.compile(
            r"^TGOS_LINUX_UDP_ECHO sent=(?P<sent>\d+) "
            r"received=(?P<received>\d+) loss=(?P<loss>\d+)$"
        ),
    },
    "test-013": {
        "testId": "TEST-013",
        "profileScenario": "tcp-fallback",
        "marker": "TGOS_LINUX_TCP_CONNECTED",
        "pattern": re.compile(
            r"^TGOS_LINUX_TCP_CONNECTED peer=(?P<peer>[^ ]+) "
            r"attempt=(?P<attempt>\d+)$"
        ),
    },
    "test-015": {
        "testId": "TEST-015",
        "profileScenario": "icpc",
        "marker": "TGOS_LINUX_ICPC_PASS",
        "pattern": re.compile(
            r"^TGOS_LINUX_ICPC_PASS sent=(?P<sent>\d+) "
            r"verified=(?P<verified>\d+) loss=(?P<loss>\d+)$"
        ),
    },
    "test-018-safe": {
        "testId": "TEST-018",
        "profileScenario": "icpc-safe",
        "marker": "TGOS_LINUX_ICPC_SAFE_PASS",
        "pattern": re.compile(
            r"^TGOS_LINUX_ICPC_SAFE_PASS phase1_verified=(?P<p1>\d+) "
            r"phase2_verified=(?P<p2>\d+)$"
        ),
    },
}
RUNTIME_FAILURE_MARKERS = (
    "AXVISOR_LINUX_CONSOLE_FAIL",
    "TGOS_ZEPHYR_NET_FAIL",
    "kernel panic",
    "ESR_EL2:",
    "ELR_EL2:",
    "FAR_EL2:",
)
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
BOOT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class GuestNetworkError(ValueError):
    """The supplied inputs or the run cannot produce valid evidence."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _process_exists(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _checked_file(path: Path, field: str) -> Path:
    if not path.is_file() or path.is_symlink():
        raise GuestNetworkError(f"{field} must be a regular non-link file: {path}")
    return path


def _new_directory(path: Path, label: str) -> Path:
    try:
        path.mkdir()
    except FileExistsError as error:
        raise GuestNetworkError(f"{label} already exists: {path}") from error
    return path


def _read_json(path: Path, label: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GuestNetworkError(f"cannot parse {label}: {error}") from error


def _bind_manifest_artifact(manifest: dict, key: str, actual: Path, label: str) -> None:
    entry = manifest.get(key)
    if not isinstance(entry, dict) or "sha256" not in entry:
        raise GuestNetworkError(f"{label} manifest has no {key} entry")
    actual_hash = _sha256_file(actual)
    if actual_hash != entry["sha256"]:
        raise GuestNetworkError(
            f"{label} {key} hash {actual_hash} does not match manifest {entry['sha256']}"
        )


def _wait_for_marker(
    log_path: Path,
    marker: str,
    timeout: float,
    failure_markers: tuple[str, ...] = (),
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    tail = b""
    while time.monotonic() < deadline:
        try:
            data = log_path.read_bytes()
        except OSError:
            data = b""
        if data != tail:
            tail = data
        for failure_marker in failure_markers:
            if failure_marker.encode() in tail:
                raise GuestNetworkError(
                    f"runtime failure marker {failure_marker!r} observed before {marker!r}"
                )
        if marker.encode() in tail:
            return True, tail.decode("utf-8", errors="replace")
        time.sleep(1)
    return False, tail.decode("utf-8", errors="replace")


def _publish_structured_artifacts(
    output: Path, scenario: str, fields: dict[str, object], log_text: str, *, run_id: str, session_id: str
) -> None:
    """P4-EVID-01B: write structured counters/metrics/frames into a bundle.

    Supplementary: the scenario smoke oracle has already passed; a failure to
    derive artifacts must never downgrade a valid smoke (recorded, not fatal).
    """
    try:
        from publish_network_artifacts import write_frames_artifacts, write_network_artifacts
    except ImportError:  # pragma: no cover
        from .publish_network_artifacts import write_frames_artifacts, write_network_artifacts
    sent = int(fields.get("sent", 0) or 0)
    received = int(fields.get("received", fields.get("verified", 0)) or 0)
    loss = int(fields.get("loss", 0) or 0)
    transport = "udp" if scenario != "test-011" else "icmp"
    if scenario == "test-013":
        transport = "tcp"
        sent, received, loss = 1, 1, 0
    write_network_artifacts(
        output,
        scenario=scenario,
        transport=transport,
        sent=sent,
        received=received,
        loss=loss,
        log_text=log_text,
    )
    try:
        write_frames_artifacts(
            output, log_text, run_id=run_id, session_id=session_id
        )
    except Exception as error:  # noqa: BLE001 - frames are supplementary for smoke
        print(f"warning: frame artifacts not published: {error}", file=sys.stderr)


def validate_runtime_smoke(
    log_text: str,
    scenario: str,
    profile_data: dict,
    *,
    expected_boot_id: str | None = None,
) -> dict:
    """Validate one narrow Guest-network smoke without qualifying a TEST gate."""

    oracle = _validate_smoke_profile(scenario, profile_data)
    _validate_runtime_markers(
        log_text, profile_data, expected_boot_id=expected_boot_id
    )
    if profile_data.get("schema_version") == "p4-network-profile-v2":
        if not expected_boot_id:
            raise GuestNetworkError(
                "p4-network-profile-v2 requires the fresh expected_boot_id"
            )
        try:
            validate_dual_guest_log(log_text, run_id=expected_boot_id)
        except RuntimeContractError as error:
            raise GuestNetworkError(
                f"shared dual-Guest runtime contract failed: {error}"
            ) from error
    completion_line, fields = _parse_completion_line(log_text, oracle)
    _validate_smoke_fields(scenario, fields)
    return {
        "scenario": scenario,
        "testId": oracle["testId"],
        "completionLine": completion_line,
        "fields": fields,
        "evidenceClass": "runtime-smoke",
        "qualified": False,
        "qualificationStatus": "qualification-pending",
    }


def _validate_prepared_modes(
    scenario: str, linux_manifest: dict, zephyr_manifest: dict
) -> None:
    try:
        expected_linux, expected_zephyr = SCENARIO_IMAGE_MODES[scenario]
    except KeyError as error:
        raise GuestNetworkError(f"unknown scenario {scenario!r}") from error
    actual_linux = linux_manifest.get("mode")
    actual_zephyr = zephyr_manifest.get("mode")
    if actual_linux != expected_linux or actual_zephyr != expected_zephyr:
        raise GuestNetworkError(
            f"{scenario} requires prepared modes "
            f"linux={expected_linux!r}, zephyr={expected_zephyr!r}; got "
            f"linux={actual_linux!r}, zephyr={actual_zephyr!r}"
        )


def _validate_profile_timeout(profile_data: dict, timeout_seconds: float) -> None:
    configured = profile_data.get("timeoutSeconds")
    if (
        not isinstance(configured, (int, float))
        or isinstance(configured, bool)
        or configured <= 0
    ):
        raise GuestNetworkError("scenario profile timeoutSeconds must be positive")
    if float(configured) != timeout_seconds:
        raise GuestNetworkError(
            f"--timeout-seconds={timeout_seconds:g} does not match the "
            f"scenario profile timeoutSeconds={configured}"
        )


def _validate_smoke_profile(scenario: str, profile_data: dict) -> dict:
    if scenario not in SMOKE_ORACLES:
        raise GuestNetworkError(f"unknown scenario {scenario!r}")
    expected = SMOKE_ORACLES[scenario]
    version = profile_data.get("schema_version")
    if version == "p4-network-profile-v1":
        required = {
            "schema_version": "p4-network-profile-v1",
            "profile_id": f"{scenario}-v1",
            "test_id": expected["testId"],
            "scenario": expected["profileScenario"],
            "host_only": True,
            "evidence_level": "L2 host",
            "completionMarker": expected["marker"],
        }
        label = "frozen v1 smoke input"
    elif version == "p4-network-profile-v2":
        required = {
            "schema_version": "p4-network-profile-v2",
            "profile_id": f"{scenario}-v2",
            "test_id": expected["testId"],
            "scenario": f"{expected['profileScenario']}-qualification",
            "host_only": False,
            "guest_runtime": True,
            "evidence_level": "L7 Guest-IP",
            "completionMarker": expected["marker"],
        }
        label = "Guest-runtime v2 input"
    else:
        raise GuestNetworkError(
            f"{scenario} profile schema_version is not v1 or v2: {version!r}"
        )
    mismatches = [
        f"{field}={profile_data.get(field)!r}"
        for field, value in required.items()
        if profile_data.get(field) != value
    ]
    if mismatches:
        raise GuestNetworkError(
            f"{scenario} profile is not the {label}: " + ", ".join(mismatches)
        )
    if version == "p4-network-profile-v2":
        expected_fault = {
            "schema_version": "p4-network-fault-v1",
            "profile_id": f"p4-{scenario}-disabled",
            "seed": 0,
            "enabled": False,
            "packet_numbering": "one-based",
            "rules": {
                "drop_every": 0,
                "duplicate_every": 0,
                "reorder_every": 0,
                "corrupt_every": 0,
            },
            "operation_order": ["corrupt", "reorder", "duplicate", "drop"],
        }
        if profile_data.get("fault") != expected_fault:
            raise GuestNetworkError(
                f"{label} requires the scenario-bound disabled fault profile "
                f"p4-{scenario}-disabled; full fault execution belongs to P4-REL-01"
            )
    profile_oracle = profile_data.get("oracle")
    if not isinstance(profile_oracle, dict):
        raise GuestNetworkError("scenario profile has no runtime oracle")
    if profile_oracle.get("requiredMarkers") != [
        "AXVISOR_DUAL_GUEST_LINUX_READY",
        "AXVISOR_DUAL_GUEST_ZEPHYR_READY",
    ]:
        raise GuestNetworkError(
            "scenario profile must require both frozen dual-Guest READY markers"
        )
    return expected


def _validate_runtime_markers(
    log_text: str,
    profile_data: dict,
    *,
    expected_boot_id: str | None = None,
) -> None:
    oracle = profile_data.get("oracle")
    if not isinstance(oracle, dict):
        raise GuestNetworkError("scenario profile has no runtime oracle")
    required = oracle.get("requiredMarkers", [])
    if not isinstance(required, list) or any(
        not isinstance(marker, str) for marker in required
    ):
        raise GuestNetworkError(
            "scenario profile requiredMarkers must be a string list"
        )
    missing = [marker for marker in required if log_text.count(marker) != 1]
    if missing:
        raise GuestNetworkError(
            "required Guest markers were not observed exactly once: "
            + ", ".join(missing)
        )
    if expected_boot_id is not None:
        ready_lines = (
            f"AXVISOR_DUAL_GUEST_LINUX_READY vm=1 boot_id={expected_boot_id}",
            f"AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id={expected_boot_id}",
        )
        wrong_identity = [
            line for line in ready_lines if log_text.count(line) != 1
        ]
        if wrong_identity:
            raise GuestNetworkError(
                "dual-Guest READY markers are not bound exactly once to the fresh run id: "
                + ", ".join(wrong_identity)
            )
    forbidden = oracle.get("forbidden", [])
    if not isinstance(forbidden, list) or any(
        not isinstance(marker, str) for marker in forbidden
    ):
        raise GuestNetworkError(
            "scenario profile forbidden markers must be a string list"
        )
    all_forbidden = tuple(dict.fromkeys((*forbidden, *RUNTIME_FAILURE_MARKERS)))
    observed = [
        marker for marker in all_forbidden if marker.lower() in log_text.lower()
    ]
    if observed:
        raise GuestNetworkError(
            "forbidden runtime markers were observed: " + ", ".join(observed)
        )


def _strip_console_attribution(line: str) -> str:
    """Normalize a merged-console line before frozen-format matching.

    21ef's AxVisor prepends each VM's console output with an ANSI reset and a
    `[VM N] ` attribution prefix (e.g. "\x1b[m[VM 1] TGOS_...").  The frozen
    smoke format must be matched against the bare guest line, so strip the
    escape sequence and the attribution prefix (when present).  Lines without
    either pass through unchanged.
    """
    import re as _re

    line = _re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line)
    line = _re.sub(r"^\[VM \d+\]\s*", "", line)
    # The merged console may interleave an in-flight kernel printk continuation
    # (e.g. "[    3.111706] sdhci-pltfm: ...") onto the app's completion line.
    # Truncate at the first kernel-timestamp bracket so the frozen smoke format
    # match is stable.  Bare app lines (no bracket) pass through unchanged.
    continuation = _re.search(r"\[\s*\d+\.\d+\]", line)
    if continuation is not None:
        line = line[: continuation.start()]
    return line.strip()


def _parse_completion_line(
    log_text: str, oracle: dict
) -> tuple[str, dict[str, object]]:
    pattern = oracle["pattern"]
    marker = oracle["marker"]
    candidates = [
        _strip_console_attribution(line)
        for line in log_text.splitlines()
        if marker in line
    ]
    if len(candidates) != 1:
        raise GuestNetworkError(
            f"completion marker {marker!r} must occur on exactly one log line"
        )
    match = pattern.fullmatch(candidates[0])
    if match is None:
        raise GuestNetworkError(
            f"completion line does not match the frozen {oracle['testId']} smoke format"
        )
    fields: dict[str, object] = {
        key: int(value) if value.isdecimal() else value
        for key, value in match.groupdict().items()
    }
    return candidates[0], fields


def _validate_smoke_fields(scenario: str, fields: dict[str, object]) -> None:
    if scenario == "test-018-safe":
        p1 = int(fields.get("p1", 0) or 0)
        p2 = int(fields.get("p2", 0) or 0)
        if p1 < 1 or p2 < 1:
            raise GuestNetworkError(
                f"test-018-safe requires verified CONTROLS in both phases, got p1={p1} p2={p2}"
            )
        return
    if scenario in {"test-011", "test-012"}:
        if fields != {"sent": 100, "received": 100, "loss": 0}:
            raise GuestNetworkError(f"{scenario} smoke requires 100/100 and zero loss")
        return
    if scenario == "test-013":
        if fields["peer"] != "10.77.0.2:46001" or not 0 <= fields["attempt"] <= 2:
            raise GuestNetworkError(
                "test-013 smoke requires the frozen peer and bounded connect attempt"
            )
        return
    if fields != {"sent": 100, "verified": 100, "loss": 0}:
        raise GuestNetworkError(
            "test-015 smoke requires all 100 ICPC ACKs and zero loss"
        )


def run_guest_network(
    *,
    repository: Path,
    build_config: Path,
    qemu_config: Path,
    linux_vmconfig: Path,
    zephyr_vmconfig: Path,
    linux_rootfs: Path,
    linux_manifest: Path,
    zephyr_image: Path,
    zephyr_build_manifest: Path,
    from_soak_session: Path,
    scenario: str,
    profile: Path,
    timeout_seconds: float,
    run_id: str,
    output_dir: Path,
) -> int:
    if scenario not in SCENARIOS:
        raise GuestNetworkError(f"unknown scenario {scenario!r}")
    if not re.match(r"^[A-Za-z0-9._-]+$", run_id):
        raise GuestNetworkError("run-id must match [A-Za-z0-9._-]+")
    if len(run_id) > 64:
        raise GuestNetworkError("run-id must be at most 64 characters")
    try:
        source_soak = validate_soak_binding(from_soak_session)
        current_qemu_claim = validate_current_qemu_config(qemu_config)
    except (SoakBindingError, OSError) as error:
        raise GuestNetworkError(f"P2/P4 input binding failed: {error}") from error

    output = _new_directory(output_dir, "output directory")
    _new_directory(output / "configs", "configs directory")
    logs_dir = _new_directory(output / "logs", "logs directory")
    _new_directory(output / "network", "network directory")
    _new_directory(output / "metrics", "metrics directory")
    _new_directory(output / "dtb", "dtb directory")

    for path, field in [
        (build_config, "build config"), (qemu_config, "QEMU config"),
        (linux_vmconfig, "Linux VM config"), (zephyr_vmconfig, "Zephyr VM config"),
        (linux_rootfs, "Linux rootfs"), (linux_manifest, "Linux rootfs manifest"),
        (zephyr_image, "Zephyr image"), (zephyr_build_manifest, "Zephyr build manifest"),
        (profile, "scenario profile"),
    ]:
        _checked_file(path, field)

    linux_meta = _read_json(linux_manifest, "Linux rootfs manifest")
    zephyr_meta = _read_json(zephyr_build_manifest, "Zephyr build manifest")
    try:
        require_matching_binding(linux_meta, source_soak, label="Linux rootfs manifest")
        require_matching_binding(zephyr_meta, source_soak, label="Zephyr build manifest")
    except SoakBindingError as error:
        raise GuestNetworkError(str(error)) from error
    if linux_meta.get("bootId") != run_id or zephyr_meta.get("bootId") != run_id:
        raise GuestNetworkError(
            "prepared Linux and Zephyr bootId values must equal --run-id"
        )
    _validate_prepared_modes(scenario, linux_meta, zephyr_meta)
    _bind_manifest_artifact(linux_meta, "outputRootfs", linux_rootfs, "Linux")
    # Zephyr manifests nest the artifacts under "artifacts.bin".
    zephyr_bin_entry = zephyr_meta.get("artifacts", {}).get("bin", zephyr_meta.get("bin"))
    if not isinstance(zephyr_bin_entry, dict) or zephyr_bin_entry.get("sha256") != _sha256_file(zephyr_image):
        raise GuestNetworkError("Zephyr image hash does not match its build manifest")
    profile_data = _read_json(profile, "scenario profile")
    smoke_oracle = _validate_smoke_profile(scenario, profile_data)
    _validate_profile_timeout(profile_data, timeout_seconds)
    soak_provenance = output / "configs" / "soak-provenance.json"
    publish_new_file(
        soak_provenance,
        (json.dumps(source_soak, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        error_type=GuestNetworkError,
    )
    bound_profile = output / "configs" / "profile.json"
    publish_new_file(
        bound_profile,
        (json.dumps(profile_data, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        error_type=GuestNetworkError,
    )

    # Resolve the Zephyr VM config against the manifest-bound image path.
    zephyr_vm_text = zephyr_vmconfig.read_text(encoding="utf-8")
    zephyr_image_abs = zephyr_image.resolve()
    if "/path/to/zephyr.bin" in zephyr_vm_text:
        zephyr_vm_text = zephyr_vm_text.replace("/path/to/zephyr.bin", str(zephyr_image_abs))
    resolved_zephyr_vmconfig = output / "configs" / "zephyr.resolved.toml"
    publish_new_file(
        resolved_zephyr_vmconfig,
        zephyr_vm_text.encode("utf-8"),
        error_type=GuestNetworkError,
    )
    zephyr_vmconfig = resolved_zephyr_vmconfig

    # Resolve the Linux kernel path (locked image cache) in the Linux VM config.
    linux_vm_text = linux_vmconfig.read_text(encoding="utf-8")
    locked_kernel = Path("/root/.cache/tgoskits/axvisor-images/qemu_aarch64_linux/qemu-aarch64")
    if not locked_kernel.is_file():
        raise GuestNetworkError(f"locked Linux kernel image missing: {locked_kernel}")
    if "/guest/linux/linux-qemu" in linux_vm_text:
        linux_vm_text = linux_vm_text.replace("/guest/linux/linux-qemu", str(locked_kernel))
        # The locked image is read directly into memory, not from a guest fs.
        linux_vm_text = linux_vm_text.replace('image_location = "fs"', 'image_location = "memory"')
        # The rootfs travels as an initramfs (releases the virtio-mmio slot 0
        # page so the mediated vnet0 frontend can own 0x0a000200 exclusively).
        initramfs = output / "configs" / "linux-initramfs.cpio.gz"
        from convert_rootfs_to_initramfs import convert as convert_initramfs  # type: ignore
        convert_initramfs(source_rootfs=linux_rootfs, output_initramfs=initramfs)
        # The Linux RAM is 0x8000_0000..0x9000_0000 (MAP_IDENTICAL); place the
        # initramfs above the kernel (kernel ~0x8020_0000, ~32 MiB headroom
        # at 0x8800_0000) instead of the legacy 0x0c000000 which is outside
        # the VM region on the official baseline.
        ramdisk_gpa = "0x8800_0000"
        linux_vm_text = linux_vm_text.replace(
            'kernel_load_addr = 0x8020_0000',
            'kernel_load_addr = 0x8020_0000\nramdisk_path = "' + str(initramfs.resolve()) + '"\nramdisk_load_addr = ' + ramdisk_gpa,
        )
    resolved_linux_vmconfig = output / "configs" / "linux.resolved.toml"
    publish_new_file(
        resolved_linux_vmconfig,
        linux_vm_text.encode("utf-8"),
        error_type=GuestNetworkError,
    )
    linux_vmconfig = resolved_linux_vmconfig

    # Freeze inputs.
    inputs = {
        name: {"path": str(path), "sha256": _sha256_file(path)}
        for name, path in [
            ("buildConfig", build_config), ("qemuConfig", qemu_config),
            ("linuxVmconfig", linux_vmconfig), ("zephyrVmconfig", zephyr_vmconfig),
            ("linuxRootfs", linux_rootfs), ("zephyrImage", zephyr_image),
            ("profile", bound_profile), ("soakProvenance", soak_provenance),
        ]
    }
    inputs["currentQemuIsolation"] = current_qemu_claim

    nonce = secrets.token_hex(16)
    # Guest images are prepared with this same identifier. The fresh random
    # nonce still binds the QEMU process identity and is never inherited from
    # the historical P2 source run.
    boot_id = run_id
    qemu_name = f"axvisor-dual-smoke-{nonce}"
    runtime_dir = Path("/tmp") / f"axp4-{nonce}"
    runtime_dir.mkdir()
    qmp_socket = runtime_dir / "qmp.sock"
    qemu_pidfile = runtime_dir / "qemu.pid"
    live_log = runtime_dir / "axvisor-live.log"

    session = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "runId": run_id,
        "scenario": scenario,
        "sessionNonce": nonce,
        "bootId": boot_id,
        "qemuName": qemu_name,
        "startedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    publish_new_file(
        output / "session.json",
        (json.dumps(session, indent=2) + "\n").encode("utf-8"),
        error_type=GuestNetworkError,
    )

    # Official-baseline xtask `qemu` has no QMP/pidfile/name flags; inject
    # the identity-binding arguments into the QEMU config `args` instead.
    qemu_text = qemu_config.read_text(encoding="utf-8")
    resolved_qemu_config = output / "configs" / "qemu.resolved.toml"
    publish_new_file(
        resolved_qemu_config,
        inject_qemu_identity_args(
            qemu_text,
            qmp_socket=qmp_socket,
            qemu_pidfile=qemu_pidfile,
            qemu_name=qemu_name,
        ).encode("utf-8"),
        error_type=GuestNetworkError,
    )
    inputs["resolvedQemuConfig"] = {
        "path": str(resolved_qemu_config),
        "sha256": _sha256_file(resolved_qemu_config),
    }

    commands = [{
        "step": "launch",
        "argv": build_axvisor_qemu_command(
            build_config=build_config,
            qemu_config=resolved_qemu_config,
            linux_vmconfig=linux_vmconfig,
            zephyr_vmconfig=zephyr_vmconfig,
        ),
    }]
    publish_new_file(
        output / "commands.jsonl",
        "".join(json.dumps(c) + "\n" for c in commands).encode("utf-8"),
        error_type=GuestNetworkError,
    )

    launcher = None
    log_handle = None
    success = False
    primary_error = None
    cleanup_error = None
    smoke_report = None
    qemu_pid: int | None = None
    residual_processes: list[str] = []
    residual_sockets: list[str] = []
    residual_files: list[str] = []
    try:
        log_handle = live_log.open("xb", buffering=0)
        launcher = subprocess.Popen(
            commands[0]["argv"],
            # The current command is the repository-level tg-xtask route.
            # Running it from os/axvisor would select that directory's legacy
            # local alias and reinterpret `axvisor` as an invalid subcommand.
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # Wait for the scenario completion marker in the raw log.
        scenario_timeout = timeout_seconds
        marker = smoke_oracle["marker"]
        found, _ = _wait_for_marker(
            live_log,
            marker,
            scenario_timeout,
            RUNTIME_FAILURE_MARKERS,
        )
        if not found:
            raise GuestNetworkError(
                f"completion marker {marker!r} not seen within {scenario_timeout}s"
            )
        # Let the tail lines (and any keep-alive frames) settle, then re-read
        # the full live log: reading exactly at the marker can race with the
        # console writer between the TX line and its frame-hex line, which
        # would make counters and frames.jsonl disagree.
        time.sleep(2)
        log_text = live_log.read_text(encoding="utf-8", errors="replace")
        smoke_report = validate_runtime_smoke(
            log_text,
            scenario,
            profile_data,
            expected_boot_id=boot_id,
        )
        # P4-EVID-01B: derive structured scenario counters/metrics/frames into
        # the bundle (supplementary; never fails an already-valid smoke).
        try:
            _publish_structured_artifacts(
                output, scenario, smoke_report["fields"], log_text,
                run_id=run_id, session_id=boot_id,
            )
        except Exception as error:  # noqa: BLE001 - supplementary artifacts only
            print(f"warning: structured artifacts not published: {error}", file=sys.stderr)
        # P4-EVID-01B: v2 runs also capture both final Guest DTBs over the
        # runner-injected QMP socket while QEMU is still alive. A capture
        # failure is fail-closed for qualification (missing dtb/ artifacts)
        # but never downgrades the already-valid smoke.
        if profile_data.get("schema_version") == "p4-network-profile-v2":
            try:
                from collect_guest_dtbs import DtbCollectError, collect_dtbs

                collect_dtbs(
                    qmp_socket,
                    output,
                    run_id=run_id,
                    session_id=boot_id,
                    nonce=nonce,
                    qemu_name=qemu_name,
                    qemu_pidfile=qemu_pidfile,
                    log_text=live_log.read_text(encoding="utf-8", errors="replace"),
                )
            except Exception as error:  # noqa: BLE001 - fail-closed at qualification
                print(f"warning: guest DTB capture failed (qualification will fail closed): {error}", file=sys.stderr)
        # Let the QEMU settle and use the shared runner-owned shutdown path.
        time.sleep(2)
        try:
            qemu_pid = int(qemu_pidfile.read_text().strip())
        except (OSError, ValueError):
            qemu_pid = None
        if launcher is not None:
            cleanup_error = request_bounded_shutdown(
                launcher,
                pidfile=qemu_pidfile,
                timeout_seconds=60.0,
            )
        if _process_exists(qemu_pid):
            residual_processes.append(f"qemu pid {qemu_pid}")
        if qmp_socket.exists():
            residual_sockets.append(str(qmp_socket))
        if residual_processes or residual_sockets:
            cleanup_error = cleanup_error or "runtime residual process/socket detected"
        success = cleanup_error is None
    except GuestNetworkError as error:
        primary_error = str(error)
    except Exception as error:  # noqa: BLE001 - package all failure evidence.
        primary_error = str(error)
    finally:
        if launcher is not None and launcher.poll() is None:
            try:
                emergency_cleanup = request_bounded_shutdown(
                    launcher,
                    pidfile=qemu_pidfile,
                    timeout_seconds=5.0,
                )
                cleanup_error = cleanup_error or emergency_cleanup
            except Exception as error:  # noqa: BLE001 - preserve failed-attempt evidence
                cleanup_error = cleanup_error or f"shared emergency cleanup failed: {error}"
        if log_handle is not None:
            log_handle.close()
        if live_log.exists():
            shutil.copyfile(live_log, logs_dir / "axvisor.raw.log")
        try:
            shutil.rmtree(runtime_dir)
        except OSError as error:
            cleanup_error = cleanup_error or f"runtime directory cleanup failed: {error}"
        if runtime_dir.exists():
            residual_files.append(str(runtime_dir))
            cleanup_error = cleanup_error or "runtime directory remains after cleanup"

    if cleanup_error is not None:
        success = False

    final_log = logs_dir / "axvisor.raw.log"
    if success:
        try:
            smoke_report = validate_runtime_smoke(
                final_log.read_text(encoding="utf-8", errors="replace"),
                scenario,
                profile_data,
                expected_boot_id=boot_id,
            )
        except (GuestNetworkError, OSError) as error:
            success = False
            primary_error = f"final log smoke validation failed: {error}"
        # P4-EVID-01B: split the merged console into per-Guest raw logs
        # (linux.raw.log / zephyr.raw.log) for the v2 qualification gate.
        try:
            from publish_network_artifacts import write_split_logs

            write_split_logs(
                output, final_log.read_text(encoding="utf-8", errors="replace")
            )
        except Exception as error:  # noqa: BLE001 - fail-closed at qualification
            print(f"warning: per-Guest log split failed (qualification will fail closed): {error}", file=sys.stderr)

    # Publish artifacts.
    manifest = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "artifactStatus": "runtime-smoke-complete" if success else "failed-attempt",
        "status": TOKENS["ok"] if success else TOKENS["failed"],
        "proofScope": "one-dual-guest-mediated-network-runtime-smoke",
        "smokeOracle": smoke_report if success else None,
        "qualification": {
            "qualified": False,
            "status": "qualification-pending",
            "requiredPublisher": "p4-guest-runtime-v2",
        },
        "doesNotProve": [
            "DMA isolation",
            "hardware DMA",
            "AI closed loop",
            "real-time improvement",
            "outer-QEMU networking",
            "TEST-011..015 qualification",
            "QMP peer identity",
        ],
        "inputs": inputs,
        "session": session,
    }
    publish_new_file(
        output / "manifest.json",
        (json.dumps(manifest, indent=2) + "\n").encode("utf-8"),
        error_type=GuestNetworkError,
    )
    status = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "runId": run_id,
        "success": success,
        "status": TOKENS["ok"] if success else TOKENS["failed"],
        "primaryError": primary_error,
        "cleanupError": cleanup_error,
        "evidenceClass": "runtime-smoke" if success else "failed-attempt",
        "qualified": False,
        "statusLast": True,
        "completionLine": smoke_report["completionLine"] if success else None,
        "completedChecks": [
            "runtime-markers",
            "scenario-smoke-oracle",
            "final-log-revalidation",
            "cleanup",
        ]
        if success
        else [],
        "manifestSha256": _sha256_file(output / "manifest.json"),
    }
    publish_new_file(
        output / "status.json",
        (json.dumps(status, indent=2) + "\n").encode("utf-8"),
        error_type=GuestNetworkError,
    )

    # P4-EVID-01B: Guest-runtime v2 qualification path.  When the caller
    # selects a p4-network-profile-v2 profile, publish the v2 bundle
    # (session/manifest/cleanup/status) and run the fail-closed qualification
    # validator.  Only the validator may emit `guest_network_qualified`; any
    # deficiency leaves the bundle as a fail-closed smoke (qualified=false)
    # and the run exits non-zero so scripts cannot mistake it for a pass.
    v2_result = None
    if success and profile_data.get("schema_version") == "p4-network-profile-v2":
        try:
            from v2_publisher import publish_v2
        except ImportError:  # pragma: no cover
            from .v2_publisher import publish_v2
        try:
            v2_result = publish_v2(
                output,
                profile,
                source_soak_session=from_soak_session,
                run_id=run_id,
                session_id=boot_id,
                nonce=nonce,
                test_id=str(profile_data["test_id"]),
                profile_id=str(profile_data["profile_id"]),
                scenario=str(profile_data["scenario"]),
                evidence_level=str(profile_data["evidence_level"]),
                transport=str(profile_data["transport"]),
                network=profile_data["network"],
                endpoints=profile_data["endpoints"],
                residual_processes=residual_processes,
                residual_sockets=residual_sockets,
                residual_files=residual_files,
                cleanup_error=cleanup_error,
                extra_files=[soak_provenance, bound_profile],
            )
        except Exception as error:  # noqa: BLE001 - v2 publish must never crash the runner
            v2_result = {
                "valid": False,
                "status": "guest_network_smoke_completed",
                "qualified": False,
                "error": str(error),
            }
        print(json.dumps({"v2Qualification": v2_result}, ensure_ascii=False, indent=2))
    print(json.dumps(status, ensure_ascii=False, indent=2))
    if v2_result is not None and not v2_result.get("valid"):
        # Smoke succeeded but qualification did not: fail-closed exit.
        return 2
    return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--build-config", required=True, type=Path)
    parser.add_argument("--qemu-config", required=True, type=Path)
    parser.add_argument("--linux-vmconfig", required=True, type=Path)
    parser.add_argument("--zephyr-vmconfig", required=True, type=Path)
    parser.add_argument("--linux-rootfs", required=True, type=Path)
    parser.add_argument("--linux-rootfs-manifest", required=True, type=Path)
    parser.add_argument("--zephyr-image", required=True, type=Path)
    parser.add_argument("--zephyr-build-manifest", required=True, type=Path)
    parser.add_argument("--from-soak-session", required=True, type=Path)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--timeout-seconds", required=True, type=float)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return run_guest_network(
            repository=args.repository,
            build_config=args.build_config,
            qemu_config=args.qemu_config,
            linux_vmconfig=args.linux_vmconfig,
            zephyr_vmconfig=args.zephyr_vmconfig,
            linux_rootfs=args.linux_rootfs,
            linux_manifest=args.linux_rootfs_manifest,
            zephyr_image=args.zephyr_image,
            zephyr_build_manifest=args.zephyr_build_manifest,
            from_soak_session=args.from_soak_session,
            scenario=args.scenario,
            profile=args.profile,
            timeout_seconds=args.timeout_seconds,
            run_id=args.run_id,
            output_dir=args.output_dir,
        )
    except GuestNetworkError as error:
        print(f"guest network run failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
