#!/usr/bin/env python3
"""Behavioral contract for host-carveout runtime-log evidence validation."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import secrets
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/contest"
sys.path.insert(0, str(SCRIPTS))
import validate_host_carveout_runtime_log as validator  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run(args: list[str]) -> int:
    with contextlib.redirect_stderr(io.StringIO()):
        return validator.main(args)


def main() -> int:
    errors: list[str] = []
    results = ROOT / "results"
    made_results = not results.exists()
    results.mkdir(exist_ok=True)
    directory = results / f".host-carveout-runtime-contract-{os.getpid()}-{secrets.token_hex(8)}"
    directory.mkdir()
    try:
        host_dts = directory / "host.dts"
        host_dtb = directory / "host.dtb"
        vm = directory / "vm-1.toml"
        preflight = directory / "preflight.json"
        log = directory / "axvisor.log"
        capture = directory / "capture-status.json"
        output = directory / "runtime.json"
        dts_bytes = b"/dts-v1/;\n"
        dtb_bytes = b"DTB\x00host"
        vm_bytes = b"[base]\nid = 1\n"
        host_dts.write_bytes(dts_bytes)
        host_dtb.write_bytes(dtb_bytes)
        vm.write_bytes(vm_bytes)
        preflight_bytes = json.dumps({
            "schemaVersion": 1, "artifactStatus": "preflight-only", "status": "carveout_artifacts_validated", "proofScope": "host-dts-vm-carveout-static-contract",
            "sources": {"hostDts": {"path": host_dts.name, "sha256": _sha(dts_bytes)}, "vmConfigs": [{"path": vm.name, "sha256": _sha(vm_bytes)}]},
            "carveouts": [{"vmId": 1, "hpa": "0x100000000", "size": "0x8000000", "end": "0x108000000", "vmConfig": vm.name}],
        }, sort_keys=True).encode()
        preflight.write_bytes(preflight_bytes)
        log_bytes = (
            b"AXVISOR_HOST_VM_CARVEOUT_RESERVED vm=1 hpa=0x100000000 size=0x8000000 phase=before-ram-init\n"
            b"AXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED vm=1 hpa=0x100000000 size=0x8000000 reserved_cover=1 free_overlap=0 phase=before-global-allocator-init\n"
        )
        log.write_bytes(log_bytes)

        def capture_status_bytes(
            *,
            raw_log: bytes = log_bytes,
            live_log_name: str = log.name,
            host_dtb_raw: bytes = dtb_bytes,
        ) -> bytes:
            return json.dumps(
                {
                    "schemaVersion": 1,
                    "artifactStatus": validator.CAPTURE_ARTIFACT_STATUS,
                    "status": validator.CAPTURE_STATUS,
                    "proofScope": validator.CAPTURE_PROOF_SCOPE,
                    "success": True,
                    "state": {
                        "expectedVmId": 1,
                        "inputArtifacts": {
                            "hostDtb": {
                                "path": "/mnt/evidence/host.dtb",
                                "size": len(host_dtb_raw),
                                "sha256": _sha(host_dtb_raw),
                            },
                            "vmconfig": {
                                "path": "/mnt/evidence/vm-1.toml",
                                "size": len(vm_bytes),
                                "sha256": _sha(vm_bytes),
                            },
                        },
                        "liveLog": {
                            "path": live_log_name,
                            "sha256": _sha(raw_log),
                        },
                    },
                },
                sort_keys=True,
            ).encode()

        capture_bytes = capture_status_bytes()
        capture.write_bytes(capture_bytes)
        common = ["--log", str(log), "--preflight", str(preflight), "--capture-status", str(capture), "--host-dtb", str(host_dtb), "--host-dts", str(host_dts), "--vm-config", str(vm)]
        if _run([*common, "--output", str(output)]) != 0:
            errors.append("production CLI rejects valid byte-bound runtime evidence")
        else:
            manifest_bytes = output.read_bytes()
            manifest = json.loads(manifest_bytes)
            if manifest["sources"]["rawLog"] != {"path": str(log), "size": len(log_bytes), "sha256": _sha(log_bytes)}:
                errors.append("production CLI does not bind exact raw log bytes")
            if manifest.get("captureStatusBinding", {}).get("validated") is not True:
                errors.append("production CLI does not record capture-status binding")
            binding = manifest.get("captureStatusBinding", {})
            if (
                binding.get("artifactStatus") != validator.CAPTURE_ARTIFACT_STATUS
                or binding.get("status") != validator.CAPTURE_STATUS
                or binding.get("proofScope") != validator.CAPTURE_PROOF_SCOPE
            ):
                errors.append("production CLI reports a synthetic capture status")
            if _run([*common, "--output", str(output)]) == 0 or output.read_bytes() != manifest_bytes:
                errors.append("production CLI overwrites existing runtime evidence")

        guard_preflight = directory / "guard-preflight.json"
        guard_value = json.loads(preflight_bytes)
        guard_value["dmaGuards"] = [
            {
                "hpa": "0x180000000",
                "size": "0x200000",
                "end": "0x180200000",
            }
        ]
        guard_preflight.write_bytes(json.dumps(guard_value, sort_keys=True).encode())
        guard_log_bytes = (
            b"AXVISOR_HOST_VM_CARVEOUT_RESERVED vm=1 hpa=0x100000000 size=0x8000000 phase=before-ram-init\n"
            b"AXVISOR_HOST_DMA_GUARD_RESERVED hpa=0x180000000 size=0x200000 phase=before-ram-init\n"
            b"AXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED vm=1 hpa=0x100000000 size=0x8000000 reserved_cover=1 free_overlap=0 phase=before-global-allocator-init\n"
            b"AXVISOR_HOST_DMA_GUARD_ALLOCATOR_EXCLUDED hpa=0x180000000 size=0x200000 reserved_cover=1 free_overlap=0 phase=before-global-allocator-init\n"
        )
        guard_log = directory / "guard.log"
        guard_capture = directory / "guard-capture.json"
        guard_output = directory / "guard-runtime.json"
        guard_log.write_bytes(guard_log_bytes)
        guard_capture.write_bytes(
            capture_status_bytes(raw_log=guard_log_bytes, live_log_name=guard_log.name)
        )
        guard_common = [
            "--log",
            str(guard_log),
            "--preflight",
            str(guard_preflight),
            "--capture-status",
            str(guard_capture),
            "--host-dtb",
            str(host_dtb),
            "--host-dts",
            str(host_dts),
            "--vm-config",
            str(vm),
        ]
        if _run([*guard_common, "--output", str(guard_output)]) != 0:
            errors.append("production CLI rejects valid DMA-guard runtime evidence")
        else:
            guard_manifest = json.loads(guard_output.read_bytes())
            if guard_manifest.get("status") != "host_carveout_and_dma_guard_markers_match_static_preflight":
                errors.append("production CLI does not classify DMA-guard runtime evidence")
            if guard_manifest.get("expectedDmaGuards") != [
                {"hpa": "0x180000000", "size": "0x200000"}
            ]:
                errors.append("production CLI does not retain the exact DMA-guard tuple")
            markers = guard_manifest.get("dmaGuardMarkers")
            if not isinstance(markers, list) or len(markers) != 1:
                errors.append("production CLI does not publish one DMA-guard marker pair")

        old_preflight_guard_output = directory / "unexpected-guard.out.json"
        if _run(
            [
                "--log",
                str(guard_log),
                "--preflight",
                str(preflight),
                "--capture-status",
                str(guard_capture),
                "--host-dtb",
                str(host_dtb),
                "--host-dts",
                str(host_dts),
                "--vm-config",
                str(vm),
                "--output",
                str(old_preflight_guard_output),
            ]
        ) == 0 or old_preflight_guard_output.exists():
            errors.append("production CLI accepts a DMA-guard marker absent from preflight")

        guard_lines = guard_log_bytes.splitlines()
        bad_guard_logs = [
            ("missing guard allocator", b"\n".join(guard_lines[:-1]) + b"\n"),
            (
                "duplicate guard early",
                b"\n".join([guard_lines[0], guard_lines[1], guard_lines[1], *guard_lines[2:]])
                + b"\n",
            ),
            (
                "wrong guard tuple",
                guard_log_bytes.replace(b"hpa=0x180000000", b"hpa=0x180200000", 1),
            ),
            (
                "allocator before all early markers",
                b"\n".join([guard_lines[0], guard_lines[2], guard_lines[1], guard_lines[3]])
                + b"\n",
            ),
            (
                "ANSI-prefixed guard marker",
                guard_log_bytes.replace(
                    b"AXVISOR_HOST_DMA_GUARD_RESERVED",
                    b"\x1b[31mAXVISOR_HOST_DMA_GUARD_RESERVED",
                    1,
                ),
            ),
        ]
        for index, (label, invalid_log) in enumerate(bad_guard_logs):
            bad_log = directory / f"bad-guard-{index}.log"
            bad_capture = directory / f"bad-guard-{index}.capture.json"
            bad_output = directory / f"bad-guard-{index}.out.json"
            bad_log.write_bytes(invalid_log)
            bad_capture.write_bytes(
                capture_status_bytes(raw_log=invalid_log, live_log_name=bad_log.name)
            )
            if _run(
                [
                    "--log",
                    str(bad_log),
                    "--preflight",
                    str(guard_preflight),
                    "--capture-status",
                    str(bad_capture),
                    "--host-dtb",
                    str(host_dtb),
                    "--host-dts",
                    str(host_dts),
                    "--vm-config",
                    str(vm),
                    "--output",
                    str(bad_output),
                ]
            ) == 0 or bad_output.exists():
                errors.append(f"production CLI accepts {label}")

        bad_guard_preflights = [
            ("dmaGuards is not a list", {**guard_value, "dmaGuards": {}}),
            (
                "DMA guard contains vmId",
                {
                    **guard_value,
                    "dmaGuards": [{**guard_value["dmaGuards"][0], "vmId": 7}],
                },
            ),
            (
                "overlapping DMA guards",
                {
                    **guard_value,
                    "dmaGuards": [
                        guard_value["dmaGuards"][0],
                        {
                            "hpa": "0x180100000",
                            "size": "0x200000",
                            "end": "0x180300000",
                        },
                    ],
                },
            ),
        ]
        for index, (label, value) in enumerate(bad_guard_preflights):
            bad_preflight = directory / f"bad-guard-preflight-{index}.json"
            bad_output = directory / f"bad-guard-preflight-{index}.out.json"
            bad_preflight.write_bytes(json.dumps(value, sort_keys=True).encode())
            if _run(
                [
                    "--log",
                    str(guard_log),
                    "--preflight",
                    str(bad_preflight),
                    "--capture-status",
                    str(guard_capture),
                    "--host-dtb",
                    str(host_dtb),
                    "--host-dts",
                    str(host_dts),
                    "--vm-config",
                    str(vm),
                    "--output",
                    str(bad_output),
                ]
            ) == 0 or bad_output.exists():
                errors.append(f"production CLI accepts preflight where {label}")
        cases = [
            ("ansi marker", log_bytes.replace(b"AXVISOR", b"\x1b[31mAXVISOR", 1)),
            ("wrong tuple", log_bytes.replace(b"size=0x8000000", b"size=0x9000000", 1)),
            ("wrong order", log_bytes.splitlines()[1] + b"\n" + log_bytes.splitlines()[0] + b"\n"),
            ("missing allocator", log_bytes.splitlines()[0] + b"\n"),
        ]
        for index, (label, invalid_log) in enumerate(cases):
            bad_log, bad_capture, bad_output = directory / f"bad-{index}.log", directory / f"bad-{index}.json", directory / f"bad-{index}.out.json"
            bad_log.write_bytes(invalid_log)
            bad_capture.write_bytes(
                capture_status_bytes(
                    raw_log=invalid_log,
                    live_log_name=bad_log.name,
                )
            )
            if _run(["--log", str(bad_log), "--preflight", str(preflight), "--capture-status", str(bad_capture), "--host-dtb", str(host_dtb), "--host-dts", str(host_dts), "--vm-config", str(vm), "--output", str(bad_output)]) == 0 or bad_output.exists():
                errors.append(f"production CLI accepts {label}")

        status_cases: list[tuple[str, tuple[str, ...], object]] = [
            ("wrong schemaVersion", ("schemaVersion",), 2),
            ("wrong artifactStatus", ("artifactStatus",), "runtime-reviewed"),
            ("wrong status", ("status",), "single_guest_live_capture_failed"),
            ("wrong proofScope", ("proofScope",), "unbound-launch"),
            ("success false", ("success",), False),
            ("wrong expectedVmId", ("state", "expectedVmId"), 2),
            (
                "missing hostDtb",
                ("state", "inputArtifacts", "hostDtb"),
                None,
            ),
            (
                "unbound hostDtb",
                ("state", "inputArtifacts", "hostDtb", "sha256"),
                "0" * 64,
            ),
            (
                "unbound vmconfig",
                ("state", "inputArtifacts", "vmconfig", "sha256"),
                "0" * 64,
            ),
            ("unbound liveLog", ("state", "liveLog", "sha256"), "0" * 64),
        ]
        for index, (label, path, replacement) in enumerate(status_cases):
            value = json.loads(capture_bytes)
            target = value
            for component in path[:-1]:
                target = target[component]
            target[path[-1]] = replacement
            bad_capture = directory / f"bad-status-{index}.json"
            bad_output = directory / f"bad-status-{index}.out.json"
            bad_capture.write_bytes(json.dumps(value, sort_keys=True).encode())
            if _run(["--log", str(log), "--preflight", str(preflight), "--capture-status", str(bad_capture), "--host-dtb", str(host_dtb), "--host-dts", str(host_dts), "--vm-config", str(vm), "--output", str(bad_output)]) == 0 or bad_output.exists():
                errors.append(f"production CLI accepts capture status with {label}")

        empty_dtb = directory / "empty-host.dtb"
        empty_dtb.write_bytes(b"")
        empty_status = directory / "empty-host-dtb-status.json"
        empty_status.write_bytes(capture_status_bytes(host_dtb_raw=b""))
        empty_output = directory / "empty-host-dtb.out.json"
        if _run(["--log", str(log), "--preflight", str(preflight), "--capture-status", str(empty_status), "--host-dtb", str(empty_dtb), "--host-dts", str(host_dts), "--vm-config", str(vm), "--output", str(empty_output)]) == 0 or empty_output.exists():
            errors.append("production CLI accepts an empty byte-bound host DTB")
    finally:
        shutil.rmtree(directory)
        if made_results:
            try:
                results.rmdir()
            except OSError:
                pass
    if errors:
        print("host carveout runtime evidence contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
