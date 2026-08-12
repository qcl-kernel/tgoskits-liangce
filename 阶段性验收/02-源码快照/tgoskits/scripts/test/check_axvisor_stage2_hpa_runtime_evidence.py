#!/usr/bin/env python3
"""Static and parser contracts for the narrow post-run stage-2 HPA marker."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import secrets
import stat
import sys
import tomllib
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = WORKSPACE_ROOT / "virtualization/axvm/src/vm/stage2_evidence.rs"
VM = WORKSPACE_ROOT / "virtualization/axvm/src/vm/mod.rs"
VCPUS = WORKSPACE_ROOT / "virtualization/axvm/src/runtime/vcpus.rs"
ARCH_OPS = WORKSPACE_ROOT / "virtualization/axvm/src/architecture/ops.rs"
AXVM_CARGO = WORKSPACE_ROOT / "virtualization/axvm/Cargo.toml"
AXVISOR_CARGO = WORKSPACE_ROOT / "os/axvisor/Cargo.toml"
ZEPHYR_MEASUREMENT = (
    WORKSPACE_ROOT
    / "os/axvisor/configs/board/qemu-aarch64-zephyr-stage2-evidence.toml"
)
README = WORKSPACE_ROOT / "scripts/contest/README.md"
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"
CONTEST_SCRIPTS = WORKSPACE_ROOT / "scripts/contest"
sys.path.insert(0, str(CONTEST_SCRIPTS))
import validate_stage2_hpa_runtime_log as runtime_validator  # noqa: E402


def read_text(path: Path, *, label: str, errors: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"{label} cannot be read: {error}")
        return ""


def require_fragments(
    text: str, fragments: tuple[str, ...], *, label: str, errors: list[str]
) -> None:
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{label} is missing `{fragment}`")


def check_feature(
    path: Path, *, label: str, expected_value: list[str], errors: list[str]
) -> None:
    try:
        manifest = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"{label} cannot be read as TOML: {error}")
        return
    features = manifest.get("features", {})
    if features.get("stage2-hpa-evidence") != expected_value:
        errors.append(f"{label} has the wrong stage2-hpa-evidence feature mapping")
    if "stage2-hpa-evidence" in features.get("default", []):
        errors.append(f"{label} enables stage2-hpa-evidence by default")


def check_parser(errors: list[str]) -> None:
    def vm_toml(
        vm_id: int, gpa: int, map_type: int, reserved_gpa: int | None = None
    ) -> bytes:
        regions = f"[[{gpa:#x}, 0x2000, 0x7, {map_type}]]"
        if reserved_gpa is not None:
            regions = (
                f"[[{gpa:#x}, 0x2000, 0x7, {map_type}], "
                f"[{reserved_gpa:#x}, 0x2000, 0x7, 2]]"
            )
        return (
            f"[base]\nid = {vm_id}\ncpu_num = 2\n"
            f"[kernel]\nmemory_regions = {regions}\n"
        ).encode("utf-8")

    results_root = WORKSPACE_ROOT / "results"
    created_results_root = False
    try:
        results_root.mkdir()
    except FileExistsError:
        pass
    except OSError as error:
        errors.append(f"could not create stage-2 runtime contract results root: {error}")
        return
    else:
        created_results_root = True
    try:
        root_info = results_root.lstat()
    except OSError as error:
        errors.append(f"could not inspect stage-2 runtime contract results root: {error}")
        return
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        errors.append("stage-2 runtime contract results root is not a real directory")
        return

    unique = f"{os.getpid()}-{secrets.token_hex(8)}"
    directory = results_root / f".stage2-runtime-contract-{unique}"
    try:
        directory.mkdir()
    except OSError as error:
        errors.append(f"could not create stage-2 runtime contract directory: {error}")
        if created_results_root:
            try:
                results_root.rmdir()
            except OSError as cleanup_error:
                errors.append(
                    "could not clean empty stage-2 runtime contract results root: "
                    f"{cleanup_error}"
                )
        return
    try:
        vm1 = directory / "vm-1.toml"
        vm2 = directory / "vm-2.toml"
        log = directory / "axvisor.log"
        output = directory / "stage2.json"
        vm1_bytes = vm_toml(1, 0x4000_0000, 0)
        vm2_bytes = vm_toml(2, 0x4000_0000, 0, reserved_gpa=0x1_0000_0000)
        raw_log = (
            b"\x1b[32m[INFO]\x1b[0m VM[1] VCpu[0] running...\r\n"
            b"AXVISOR_STAGE2_HPA_POSTRUN vm=1 vcpu=0 kind=alloc gpa=0x40000000 hpa=0x80000000 page_size=4096 identity=0\r\n"
            b"[INFO] VM[2] VCpu[1] running...\r\n"
            b"AXVISOR_STAGE2_HPA_POSTRUN vm=2 vcpu=1 kind=reserved gpa=0x100000000 hpa=0x100000000 page_size=4096 identity=1\r\n"
        )
        vm1.write_bytes(vm1_bytes)
        vm2.write_bytes(vm2_bytes)
        log.write_bytes(raw_log)
        with contextlib.redirect_stderr(io.StringIO()):
            status = runtime_validator.main(
                [
                    "--log",
                    str(log),
                    "--vm-config",
                    str(vm1),
                    "--vm-config",
                    str(vm2),
                    "--output",
                    str(output),
                ]
            )
        if status != 0:
            errors.append("production CLI rejects valid byte-bound runtime evidence")
            return
        manifest_bytes = output.read_bytes()
        manifest = json.loads(manifest_bytes)
        if manifest["sources"]["rawLog"] != {
            "path": str(log),
            "size": len(raw_log),
            "sha256": hashlib.sha256(raw_log).hexdigest(),
        }:
            errors.append("production CLI does not bind the original raw log bytes")
        if [marker["vmId"] for marker in manifest["markers"]] != [1, 2]:
            errors.append("production parser does not sort markers by VM id")
        with contextlib.redirect_stderr(io.StringIO()):
            if runtime_validator.main(
                [
                    "--log",
                    str(log),
                    "--vm-config",
                    str(vm1),
                    "--vm-config",
                    str(vm2),
                    "--output",
                    str(output),
                ]
            ) == 0:
                errors.append("production CLI overwrites existing evidence")
        if output.read_bytes() != manifest_bytes:
            errors.append("production CLI changes evidence after no-overwrite failure")

        capture_log = directory / "axvisor-live.log"
        capture_log_bytes = raw_log.splitlines()[0] + b"\r\n" + raw_log.splitlines()[1] + b"\r\n"
        capture_log.write_bytes(capture_log_bytes)

        def capture_status_bytes(
            *,
            vm_id: int = 1,
            success: bool = True,
            status: str = "single_guest_live_capture_passed",
            vmconfig_sha: str | None = None,
            live_log_sha: str | None = None,
            live_log_size: int | None = None,
        ) -> bytes:
            return json.dumps(
                {
                    "success": success,
                    "status": status,
                    "state": {
                        "expectedVmId": vm_id,
                        "inputArtifacts": {
                            "vmconfig": {
                                "size": len(vm1_bytes),
                                "sha256": (
                                    hashlib.sha256(vm1_bytes).hexdigest()
                                    if vmconfig_sha is None
                                    else vmconfig_sha
                                ),
                            }
                        },
                        "liveLog": {
                            "size": (
                                len(capture_log_bytes)
                                if live_log_size is None
                                else live_log_size
                            ),
                            "sha256": (
                                hashlib.sha256(capture_log_bytes).hexdigest()
                                if live_log_sha is None
                                else live_log_sha
                            ),
                        },
                    },
                },
                sort_keys=True,
            ).encode("utf-8")

        capture_status = directory / "capture-status.json"
        capture_output = directory / "capture-stage2.json"
        capture_status_raw = capture_status_bytes()
        capture_status.write_bytes(capture_status_raw)
        with contextlib.redirect_stderr(io.StringIO()):
            status = runtime_validator.main(
                [
                    "--log",
                    str(capture_log),
                    "--vm-config",
                    str(vm1),
                    "--capture-status",
                    str(capture_status),
                    "--output",
                    str(capture_output),
                ]
            )
        if status != 0:
            errors.append("production CLI rejects a bound successful capture status")
        else:
            capture_manifest_bytes = capture_output.read_bytes()
            capture_manifest = json.loads(capture_manifest_bytes)
            if capture_manifest["sources"].get("captureStatus") != {
                "path": str(capture_status),
                "size": len(capture_status_raw),
                "sha256": hashlib.sha256(capture_status_raw).hexdigest(),
            }:
                errors.append("production CLI does not bind raw capture-status bytes")
            if capture_manifest.get("captureStatusBinding", {}).get("validated") is not True:
                errors.append("production CLI does not record capture-status validation")
            with contextlib.redirect_stderr(io.StringIO()):
                if runtime_validator.main(
                    [
                        "--log",
                        str(capture_log),
                        "--vm-config",
                        str(vm1),
                        "--capture-status",
                        str(capture_status),
                        "--output",
                        str(capture_output),
                    ]
                ) == 0:
                    errors.append("capture-status CLI overwrites existing evidence")
            if capture_output.read_bytes() != capture_manifest_bytes:
                errors.append("capture-status no-overwrite failure changes evidence")

        invalid_status_cases = (
            (capture_status_bytes(vmconfig_sha="0" * 64), "VM config hash mismatch"),
            (capture_status_bytes(live_log_sha="0" * 64), "live-log hash mismatch"),
            (
                capture_status_bytes(live_log_size=len(capture_log_bytes) + 1),
                "live-log size mismatch",
            ),
            (capture_status_bytes(vm_id=2), "expected VM mismatch"),
            (
                capture_status_bytes(status="single_guest_live_capture_failed"),
                "failed capture status",
            ),
        )
        for index, (status_bytes, label) in enumerate(invalid_status_cases):
            status_path = directory / f"invalid-status-{index}.json"
            invalid_output = directory / f"invalid-status-{index}.out.json"
            status_path.write_bytes(status_bytes)
            with contextlib.redirect_stderr(io.StringIO()):
                status = runtime_validator.main(
                    [
                        "--log",
                        str(capture_log),
                        "--vm-config",
                        str(vm1),
                        "--capture-status",
                        str(status_path),
                        "--output",
                        str(invalid_output),
                    ]
                )
            if status == 0 or invalid_output.exists():
                errors.append(f"production CLI accepts capture status with {label}")

        multiple_config_output = directory / "multiple-config-capture.json"
        with contextlib.redirect_stderr(io.StringIO()):
            status = runtime_validator.main(
                [
                    "--log",
                    str(log),
                    "--vm-config",
                    str(vm1),
                    "--vm-config",
                    str(vm2),
                    "--capture-status",
                    str(capture_status),
                    "--output",
                    str(multiple_config_output),
                ]
            )
        if status == 0 or multiple_config_output.exists():
            errors.append("capture-status CLI accepts more than one VM config")

        invalid_cases = (
            (raw_log.replace(b"kind=alloc", b"kind=identical", 1), "wrong mapping kind"),
            (raw_log.replace(b"AXVISOR", b" AXVISOR", 1), "whitespace-prefixed marker"),
            (raw_log.replace(b"AXVISOR", b"\x1b[mAXVISOR", 1), "ANSI marker"),
            (raw_log.replace(b"AXVISOR", "\x9bAXVISOR".encode("utf-8"), 1), "C1 ANSI marker"),
            (raw_log.replace(b"vm=2", b"vm=3", 1), "unknown VM"),
            (raw_log.replace(b"vcpu=1", b"vcpu=2", 1), "out-of-range vCPU"),
            (raw_log + raw_log.splitlines()[1] + b"\r\n", "duplicate marker"),
            (raw_log.splitlines()[0] + b"\r\n" + raw_log.splitlines()[2] + b"\r\n", "missing marker"),
            (raw_log.replace(b"VM[2] VCpu[1] running...\r\n", b"", 1), "missing prior running line"),
        )
        for index, (invalid_log, label) in enumerate(invalid_cases):
            invalid_path = directory / f"invalid-{index}.log"
            invalid_output = directory / f"invalid-{index}.json"
            invalid_path.write_bytes(invalid_log)
            with contextlib.redirect_stderr(io.StringIO()):
                status = runtime_validator.main(
                    [
                        "--log",
                        str(invalid_path),
                        "--vm-config",
                        str(vm1),
                        "--vm-config",
                        str(vm2),
                        "--output",
                        str(invalid_output),
                    ]
                )
            if status == 0 or invalid_output.exists():
                errors.append(f"production CLI accepts {label}")
    finally:
        try:
            for path in directory.iterdir():
                path.unlink()
            directory.rmdir()
            if created_results_root:
                results_root.rmdir()
        except OSError as error:
            errors.append(f"could not clean stage-2 runtime contract files: {error}")


def main() -> int:
    errors: list[str] = []
    check_parser(errors)

    evidence = read_text(EVIDENCE, label="stage-2 HPA evidence module", errors=errors)
    require_fragments(
        evidence,
        (
            "fn selected_configured_memory_region(",
            "VmMemMappingType::MapReserved",
            "resources.memory_regions.get(index)",
            "region.size() != config.size",
            ".page_table()\n            .query(region.gpa)",
            "let expected_hpa = region.host_paddr();",
            "if hpa != expected_hpa",
            "EvidenceMemoryKind::Identical | EvidenceMemoryKind::Reserved",
            '"AXVISOR_STAGE2_HPA_POSTRUN vm={} vcpu={} kind={} gpa={:#x} hpa={:#x} page_size={} \\',
            "identity={}",
            "pub(crate) fn postrun_marker",
        ),
        label="stage-2 HPA evidence production path",
        errors=errors,
    )
    if "HostPhysAddr::from(region.gpa.as_usize())" in evidence:
        errors.append("stage-2 evidence derives HPA from GPA instead of querying the page table")

    vm = read_text(VM, label="AxVM runtime marker state", errors=errors)
    require_fragments(
        vm,
        (
            '#[cfg(feature = "stage2-hpa-evidence")]\npub(crate) mod stage2_evidence;',
            "stage2_hpa_marker_emitted: core::sync::atomic::AtomicBool",
            "stage2_hpa_failure_reported: core::sync::atomic::AtomicBool",
            "AtomicBool::new(false)",
            "compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)",
        ),
        label="AxVM runtime marker state",
        errors=errors,
    )

    vcpus = read_text(VCPUS, label="vCPU post-run marker hook", errors=errors)
    require_fragments(
        vcpus,
        (
            "pub(crate) fn emit_stage2_hpa_postrun_marker",
            "crate::vm::stage2_evidence::postrun_marker(vm, vcpu_id)",
            "post-run stage-2 HPA observation unavailable",
            "if vm.mark_stage2_hpa_marker_emitted() {",
            "if vm.mark_stage2_hpa_failure_reported() {",
            "successful backend VM exits will retry silently",
            'ax_std::println!("\\n{marker}");',
        ),
        label="vCPU post-run marker hook",
        errors=errors,
    )

    arch_ops = read_text(ARCH_OPS, label="architecture vCPU-exit hook", errors=errors)
    require_fragments(
        arch_ops,
        (
            "let exit = vcpu.run()?;",
            '#[cfg(feature = "stage2-hpa-evidence")]\n                crate::runtime::vcpus::emit_stage2_hpa_postrun_marker(vm, vcpu_id);',
            'trace!("{exit:#x?}");',
            "Self::handle_vcpu_exit_bound(vm, vcpu, exit)?",
        ),
        label="architecture vCPU-exit hook",
        errors=errors,
    )
    backend_return = arch_ops.find("let exit = vcpu.run()?;")
    emit = arch_ops.find(
        "crate::runtime::vcpus::emit_stage2_hpa_postrun_marker(vm, vcpu_id);",
        backend_return,
    )
    dispatch = arch_ops.find("Self::handle_vcpu_exit_bound(vm, vcpu, exit)?", emit)
    if min(backend_return, emit, dispatch) < 0 or not backend_return < emit < dispatch:
        errors.append(
            "post-run stage-2 marker is not gated on a successful backend vCPU exit"
        )
    hook_call = "crate::runtime::vcpus::emit_stage2_hpa_postrun_marker(vm, vcpu_id);"
    if arch_ops.count(hook_call) != 1:
        errors.append("architecture run loop does not contain exactly one stage-2 hook")
    if "emit_stage2_hpa_postrun_marker(&vm, vcpu_id);" in vcpus:
        errors.append("outer vCPU task loop still emits stage-2 evidence")

    check_feature(AXVM_CARGO, label="axvm manifest", expected_value=[], errors=errors)
    check_feature(
        AXVISOR_CARGO,
        label="AxVisor manifest",
        expected_value=["axvm/stage2-hpa-evidence"],
        errors=errors,
    )
    try:
        measurement = tomllib.loads(ZEPHYR_MEASUREMENT.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"Zephyr measurement build config cannot be read: {error}")
    else:
        features = measurement.get("features")
        if features != ["guest-fdt-evidence", "stage2-hpa-evidence"]:
            errors.append(
                "Zephyr measurement build config has the wrong evidence features"
            )

    readme = read_text(README, label="contest evidence documentation", errors=errors)
    require_fragments(
        readme,
        (
            "stage2-hpa-evidence",
            "AXVISOR_STAGE2_HPA_POSTRUN",
            "not prove guest boot",
            "allocator exclusion",
            "DMA isolation",
            "dual-Guest execution",
            "IP connectivity",
            "handled the\nobserved backend exit successfully",
            "validate_stage2_hpa_runtime_log.py",
            "raw log",
            "--capture-status",
            "runtime-enriched VM configuration",
            "only literal log order",
        ),
        label="contest evidence documentation",
        errors=errors,
    )

    ci = read_text(CI, label="CI workflow", errors=errors)
    if "python3 scripts/test/check_axvisor_stage2_hpa_runtime_evidence.py" not in ci:
        errors.append("post-run stage-2 HPA evidence contract is not wired into CI")

    if errors:
        print("AxVisor post-run stage-2 HPA evidence contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
