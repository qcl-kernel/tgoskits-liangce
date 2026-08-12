#!/usr/bin/env python3
"""Regression tests for the literal Linux MapReserved boot-chain aggregator."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONTEST = ROOT / "scripts/contest"
VALIDATOR = CONTEST / "validate_linux_map_reserved_boot_runtime.py"
CI = ROOT / ".github/workflows/ci.yml"
R23_ROOT = (
    ROOT
    / "results/baseline/runs/phase2-linux-host-carveout-20260806-01e0531-dirty-r17"
)
R23_RUNNER = R23_ROOT / "run-r23.sh"
R23_VALIDATOR = R23_ROOT / "validate-r23.sh"
sys.path.insert(0, str(CONTEST))


def load_validator():
    spec = importlib.util.spec_from_file_location("linux_map_reserved_validator", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source(path: Path, data: bytes, *, size: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "sha256": sha(data)}
    if size:
        result["size"] = len(data)
    return result


def invoke(
    module, paths: dict[str, Path], output: Path, *, bind_capture_chain: bool = True
) -> int:
    arguments = [
        "--log", str(paths["log"]),
        "--capture-status", str(paths["status"]),
        "--guest-dts", str(paths["dts"]),
        "--guest-semantic-report", str(paths["semantic"]),
        "--stage2-report", str(paths["stage"]),
        "--host-carveout-report", str(paths["host"]),
        "--vm-config", str(paths["vm"]),
    ]
    if bind_capture_chain:
        arguments.extend(
            ["--capture-chain", str(paths["chain"]), "--guest-dtb", str(paths["dtb"])]
        )
    arguments.extend(["--output", str(output)])
    with contextlib.redirect_stderr(io.StringIO()):
        return module.main(arguments)


def write_happy(directory: Path, *, dma_guard: bool = False, guard_allocator_after_ready: bool = False) -> dict[str, Path]:
    paths = {name: directory / filename for name, filename in (
        ("log", "axvisor.log"), ("status", "status.json"), ("dts", "guest.dts"),
        ("semantic", "semantic.json"), ("stage", "stage.json"), ("host", "host.json"),
        ("vm", "vm.toml"), ("dtb", "guest-vm-1.final.dtb"),
        ("chain", "capture-chain.json"),
    )}
    vm = b"[base]\nid = 1\ncpu_num = 1\n[kernel]\nmemory_regions = [[0x80000000, 0x10000000, 7, 2]]\n"
    dts = b"/dts-v1/; / { };\n"
    dtb = b"guest-dtb-bytes"
    guard_allocator = "AXVISOR_HOST_DMA_GUARD_ALLOCATOR_EXCLUDED hpa=0x180000000 size=0x200000 reserved_cover=1 free_overlap=0 phase=before-global-allocator-init"
    log_lines = [
        "AXVISOR_HOST_VM_CARVEOUT_RESERVED vm=1 hpa=0x80000000 size=0x10000000 phase=before-ram-init",
        *(["AXVISOR_HOST_DMA_GUARD_RESERVED hpa=0x180000000 size=0x200000 phase=before-ram-init"] if dma_guard else []),
        "AXVISOR_HOST_VM_CARVEOUT_ALLOCATOR_EXCLUDED vm=1 hpa=0x80000000 size=0x10000000 reserved_cover=1 free_overlap=0 phase=before-global-allocator-init",
        *([guard_allocator] if dma_guard and not guard_allocator_after_ready else []),
        "AXVISOR_GUEST_DTB_READY vm=1 gpa=0x80000000 size=64 hpa_segments=0x80000000:64",
        *([guard_allocator] if dma_guard and guard_allocator_after_ready else []),
        "AXVISOR_STAGE2_HPA_POSTRUN vm=1 vcpu=0 kind=reserved gpa=0x80000000 hpa=0x80000000 page_size=4096 identity=1",
        "Linux version 6.1.0-test",
        "No reserved-memory node in the DT",
        "Run /bin/sh as init process",
        "~ #",
    ]
    log = ("\n".join(log_lines) + "\n").encode()
    paths["log"].write_bytes(log)
    paths["vm"].write_bytes(vm)
    paths["dts"].write_bytes(dts)
    paths["dtb"].write_bytes(dtb)
    ready_end = log.index(log_lines[2].encode()) + len(log_lines[2]) + 1
    prompt_offset = log.index(b"~ #")
    nonce = "a" * 32
    chain = {
        "schemaVersion": 1,
        "artifactStatus": "capture-generated-unreviewed",
        "status": "identity_bound_live_qmp_capture_completed",
        "proofScope": "same-session-paused-qmp-final-guest-dtb-capture-chain",
        "sourcePathBase": "capture-chain.json parent directory",
        "sessionNonce": nonce,
        "artifacts": [source(Path("guest-vm-1.final.dtb"), dtb)],
    }
    chain_bytes = json.dumps(chain, sort_keys=True).encode()
    paths["chain"].write_bytes(chain_bytes)
    status = {
        "schemaVersion": 1, "artifactStatus": "capture-generated-unreviewed", "success": True,
        "status": "single_guest_live_capture_passed",
        "proofScope": "one-guest-launch-ready-same-session-qmp-capture-resume-identity-exit",
        "state": {
            "expectedVmId": 1,
            "sessionNonce": nonce,
            "qemuName": f"axvisor-guest-dtb-{nonce}",
            "inputArtifacts": {"vmconfig": source(paths["vm"], vm)},
            "liveLog": source(paths["log"], log, size=False),
            "ready": {"readyPrefixBytes": ready_end, "readyPrefixSha256": sha(log[:ready_end]), "capturePlanGuestIds": [1]},
            "postResumeMarker": {
                "marker": "~ #", "markerBytes": 3, "byteOffset": prompt_offset,
                "readyPrefixBytes": ready_end, "observedPrefixBytes": len(log),
                "observedPrefixSha256": sha(log), "sealedBeforeSigintPrefixBytes": len(log),
                "sealedBeforeSigintPrefixSha256": sha(log), "occurrencesInPostResumeRegion": 1,
            },
            "nestedCaptureChain": {"path": "live-capture/capture-chain.json", "sha256": sha(chain_bytes), "status": "identity_bound_live_qmp_capture_completed"},
            "postResumeIdentity": {"pidfdBound": True},
            "qemuExit": "SIGINT sent through pidfd after nested resumed identity revalidation",
            "launcherExitCode": 0,
            "groupCleanup": {"groupExited": True, "escalated": False},
            "runtimeCleanup": {"directoryRemoved": True},
        },
    }
    status_bytes = json.dumps(status, sort_keys=True).encode()
    paths["status"].write_bytes(status_bytes)
    semantic = {"schemaVersion": 1, "status": "captured_guest_dtb_static_semantics_validated", "vm": {"id": 1, "consoleMode": "passthrough-or-no-vm-owned-console"}, "sources": {"dts": source(paths["dts"], dts, size=False), "vmConfig": source(paths["vm"], vm, size=False)}}
    paths["semantic"].write_bytes(json.dumps(semantic, sort_keys=True).encode())
    stage = {
        "schemaVersion": 1, "artifactStatus": "runtime-log-observation-validated", "status": "stage2_hpa_markers_match_configured_regions",
        "sources": {"rawLog": source(paths["log"], log), "captureStatus": source(paths["status"], status_bytes), "vmConfigs": [source(paths["vm"], vm)]},
        "captureStatusBinding": {"validated": True, "status": "single_guest_live_capture_passed", "expectedVmId": 1},
        "markers": [{"vmId": 1, "vcpuId": 0, "kind": "reserved", "gpa": "0x80000000", "hpa": "0x80000000", "pageSize": 4096, "identity": 1, "sourceLine": 6 if dma_guard else 4}],
    }
    paths["stage"].write_bytes(json.dumps(stage, sort_keys=True).encode())
    host = {
        "schemaVersion": 1, "artifactStatus": "runtime-log-observation-validated", "status": "host_carveout_and_dma_guard_markers_match_static_preflight" if dma_guard else "host_carveout_markers_match_static_preflight",
        "sources": {"rawLog": source(paths["log"], log), "captureStatus": source(paths["status"], status_bytes), "vmConfigs": [source(paths["vm"], vm)]},
        "captureStatusBinding": {"validated": True, "status": "single_guest_live_capture_passed", "expectedVmId": 1},
        "markers": [{"vmId": 1, "hpa": "0x80000000", "size": "0x10000000", "earlyReservedLine": 1, "allocatorExcludedLine": 3 if dma_guard else 2}],
        "expectedDmaGuards": [{"hpa": "0x180000000", "size": "0x200000"}] if dma_guard else [],
        "dmaGuardMarkers": [{"hpa": "0x180000000", "size": "0x200000", "earlyReservedLine": 2, "allocatorExcludedLine": 5 if guard_allocator_after_ready else 4}] if dma_guard else [],
    }
    paths["host"].write_bytes(json.dumps(host, sort_keys=True).encode())
    return paths


def reject(module, directory: Path, mutate, label: str, errors: list[str], *, dma_guard: bool = False, guard_allocator_after_ready: bool = False) -> None:
    case = directory / label.replace(" ", "-")
    case.mkdir()
    paths = write_happy(case, dma_guard=dma_guard, guard_allocator_after_ready=guard_allocator_after_ready)
    mutate(paths)
    output = case / "out.json"
    if invoke(module, paths, output) == 0 or output.exists():
        errors.append(f"validator accepts {label}")


def set_json(path: Path, components: tuple[str, ...], replacement: object) -> None:
    payload = json.loads(path.read_bytes())
    target = payload
    for component in components[:-1]:
        target = target[component]
    target[components[-1]] = replacement
    path.write_bytes(json.dumps(payload, sort_keys=True).encode())


def main() -> int:
    errors: list[str] = []
    if not VALIDATOR.is_file():
        errors.append("literal Linux MapReserved boot runtime validator is missing")
        return 1
    try:
        module = load_validator()
    except Exception as error:  # noqa: BLE001
        print(f"could not import validator: {error}", file=sys.stderr)
        return 1
    results = ROOT / "results"
    created_results = not results.exists()
    results.mkdir(exist_ok=True)
    directory = results / f".linux-mapreserved-contract-{os.getpid()}-{secrets.token_hex(8)}"
    directory.mkdir()
    try:
        paths = write_happy(directory)
        output = directory / "manifest.json"
        if invoke(module, paths, output) != 0:
            errors.append("validator rejects a complete byte-bound literal chain")
        else:
            manifest = json.loads(output.read_bytes())
            if manifest.get("status") != "linux_map_reserved_single_guest_boot_chain_observed":
                errors.append("manifest has the wrong success status")
            if [item["name"] for item in manifest.get("literalObservations", [])] != ["hostReserved", "allocatorExcluded", "ready", "stage2ReservedIdentity", "linuxVersion", "noReservedMemory", "initShell", "shellPrompt"]:
                errors.append("manifest does not preserve the literal observation chain")
            if "actual semantics beyond literal bytes" not in manifest.get("doesNotProve", []):
                errors.append("manifest overstates evidence beyond literal bytes")
            original = output.read_bytes()
            if invoke(module, paths, output) == 0 or output.read_bytes() != original:
                errors.append("validator overwrites an existing manifest")
        legacy_output = directory / "legacy-manifest.json"
        if invoke(module, paths, legacy_output, bind_capture_chain=False) != 0:
            errors.append("validator no longer accepts the pre-r23 evidence interface")

        guard_case = directory / "dma-guard"
        guard_case.mkdir()
        guard_paths = write_happy(guard_case, dma_guard=True)
        guard_output = guard_case / "manifest.json"
        if invoke(module, guard_paths, guard_output) != 0:
            errors.append("validator rejects the complete DMA-guard boot chain")
        else:
            guard_manifest = json.loads(guard_output.read_bytes())
            binding = guard_manifest.get("hostDmaGuardBinding")
            if not isinstance(binding, dict) or binding.get("expectedDmaGuards") != [{"hpa": "0x180000000", "size": "0x200000"}]:
                errors.append("manifest does not bind the exact DMA-guard tuple")
            elif binding.get("evidenceBoundary") != "allocator/runtime marker observation only; does not prove DMA isolation":
                errors.append("manifest overstates DMA-guard marker evidence")
            if "DMA isolation" not in guard_manifest.get("doesNotProve", []):
                errors.append("DMA-guard manifest does not retain the DMA-isolation boundary")
            capture_binding = guard_manifest.get("captureStatusBinding", {}).get(
                "captureChain"
            )
            if capture_binding != {
                "path": "live-capture/capture-chain.json",
                "sha256Match": True,
                "sessionNonceMatch": True,
                "guestDtbMatch": True,
            }:
                errors.append("manifest does not bind the capture-chain nonce and Guest DTB")

        reject(module, directory, lambda p: set_json(p["host"], ("expectedDmaGuards",), []), "empty dma guards", errors, dma_guard=True)
        reject(module, directory, lambda p: p["chain"].write_bytes(b"{}"), "capture chain mutation", errors)
        reject(module, directory, lambda p: p["dtb"].write_bytes(b"different guest dtb"), "captured guest dtb mutation", errors)
        reject(module, directory, lambda p: set_json(p["status"], ("state", "sessionNonce"), "b" * 32), "capture nonce mismatch", errors)
        reject(module, directory, lambda p: set_json(p["host"], ("dmaGuardMarkers", 0, "hpa"), "0x180200000"), "unbound dma marker", errors, dma_guard=True)
        reject(module, directory, lambda p: set_json(p["host"], ("dmaGuardMarkers", 0, "allocatorExcludedLine"), 3), "dma marker wrong line", errors, dma_guard=True)
        reject(module, directory, lambda p: set_json(p["host"], ("expectedDmaGuards", 0, "end"), "0x180200000"), "dma expected extra field", errors, dma_guard=True)
        reject(module, directory, lambda p: None, "dma allocator after ready", errors, dma_guard=True, guard_allocator_after_ready=True)

        reject(module, directory, lambda p: p["log"].write_bytes(p["log"].read_bytes().replace(b"Linux version", b"Linux versionx")), "log mutation", errors)
        reject(module, directory, lambda p: p["log"].write_bytes(p["log"].read_bytes() + b"~ #\n"), "duplicate marker", errors)
        reject(module, directory, lambda p: p["log"].write_bytes(p["log"].read_bytes().replace(b"Linux version 6.1.0-test\nNo reserved-memory", b"No reserved-memory\nLinux version 6.1.0-test", 1)), "wrong order", errors)
        reject(module, directory, lambda p: p["status"].write_bytes(p["status"].read_bytes().replace(b"single_guest_live_capture_passed", b"single_guest_live_capture_failed")), "bad status", errors)
        reject(module, directory, lambda p: p["stage"].write_bytes(p["stage"].read_bytes().replace(b'"sha256": "', b'"sha256": "0', 1)), "report hash", errors)
        reject(module, directory, lambda p: p["semantic"].write_bytes(b'{"schemaVersion":1,"schemaVersion":1}'), "duplicate json key", errors)
        reject(module, directory, lambda p: p["vm"].write_bytes(b""), "empty input", errors)
        status_cases = (
            ("artifact status", ("artifactStatus",), "reviewed"),
            ("proof scope", ("proofScope",), "broader-scope"),
            ("capture plan guest ids", ("state", "ready", "capturePlanGuestIds"), [1, 2]),
            ("nested capture status", ("state", "nestedCaptureChain", "status"), "failed"),
            ("nested capture path", ("state", "nestedCaptureChain", "path"), "../capture-chain.json"),
            ("nested capture hash", ("state", "nestedCaptureChain", "sha256"), "0" * 63),
            ("pidfd binding", ("state", "postResumeIdentity", "pidfdBound"), False),
            ("qemu exit", ("state", "qemuExit"), "SIGTERM"),
            ("launcher exit", ("state", "launcherExitCode"), 1),
            ("group cleanup exit", ("state", "groupCleanup", "groupExited"), False),
            ("group cleanup escalation", ("state", "groupCleanup", "escalated"), True),
            ("runtime directory cleanup", ("state", "runtimeCleanup", "directoryRemoved"), False),
        )
        for label, components, replacement in status_cases:
            reject(
                module,
                directory,
                lambda p, c=components, v=replacement: set_json(p["status"], c, v),
                f"status {label}",
                errors,
            )
        # The shared secure reader must refuse symlink inputs.  Windows may deny link creation.
        link_case = directory / "symlink"
        link_case.mkdir()
        link_paths = write_happy(link_case)
        try:
            linked = link_case / "linked.log"
            os.symlink(link_paths["log"], linked)
            link_paths["log"] = linked
            if invoke(module, link_paths, link_case / "out.json") == 0:
                errors.append("validator accepts symlink input")
        except (OSError, NotImplementedError):
            pass
        # Directly select the raw log as output: it must fail before any write.
        if invoke(module, paths, paths["log"]) == 0:
            errors.append("validator accepts an output that overwrites an input")

        output_link = directory / "output-link.json"
        try:
            os.symlink(paths["log"], output_link)
            if invoke(module, paths, output_link) == 0:
                errors.append("validator accepts a symlink output")
        except (OSError, NotImplementedError):
            pass
    finally:
        try:
            for path in sorted(directory.rglob("*"), reverse=True):
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            directory.rmdir()
            if created_results:
                results.rmdir()
        except OSError as error:
            errors.append(f"could not clean literal boot-chain test files: {error}")

    ci = CI.read_text(encoding="utf-8")
    if "python3 scripts/test/check_axvisor_linux_map_reserved_boot_runtime_evidence.py" not in ci:
        errors.append("literal Linux MapReserved boot runtime test is not wired into CI")
    try:
        r23_runner = R23_RUNNER.read_text(encoding="utf-8")
        r23_validator = R23_VALIDATOR.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"could not read r23 wrapper contract: {error}")
    else:
        for fragment in (
            "qemu-aarch64-linux-dma-guard-evidence.toml",
            "qemu-aarch64-linux-dma-guard-hold.toml",
            "prepared-dma-guard-r22/host-carveout.dtb",
            "results/baseline/runs/phase2-linux-host-carveout-20260806-01e0531-dirty-r17/live-r23",
            "--post-resume-marker '~ #'",
        ):
            if fragment not in r23_runner:
                errors.append(f"r23 runner is missing identity-bound input `{fragment}`")
        for fragment in (
            'capture_chain="$live/live-capture/capture-chain.json"',
            '"$capture_chain"',
            "--capture-chain \"$capture_chain\"",
            "--guest-dtb \"$guest_dtb\"",
            "host-carveout-and-dma-guard-runtime.json",
            "linux-map-reserved-dma-guard-boot-runtime.json",
        ):
            if fragment not in r23_validator:
                errors.append(f"r23 validator is missing bound artifact `{fragment}`")
    if errors:
        print("Linux MapReserved boot runtime evidence contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
