#!/usr/bin/env python3
"""Behavioral contract for assembling captured final Guest DTB segments."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import struct
import sys
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from types import ModuleType


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
ASSEMBLER = WORKSPACE_ROOT / "scripts/contest/assemble_guest_dtb_capture.py"
EXECUTOR = WORKSPACE_ROOT / "scripts/contest/execute_guest_dtb_capture.py"
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def minimal_fdt() -> bytes:
    return struct.pack(
        ">10I", 0xD00DFEED, 40, 40, 40, 40, 17, 16, 0, 0, 0
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def valid_plan(runtime_sha: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "artifactStatus": "marker-derived-unreviewed",
        "status": "capture_planned",
        "proofScope": "final-guest-dtb-physical-capture-plan",
        "doesNotProve": [
            "physical bytes were captured",
            "captured bytes form valid flattened device trees",
            "both guests booted",
            "passthrough DMA is isolated",
            "Linux and Zephyr have IP connectivity",
        ],
        "sourcePathBase": "capture-plan.json parent directory",
        "source": {
            "axvisorLog": {"path": "axvisor.log", "sha256": runtime_sha}
        },
        "guests": [
            {
                "vmId": 1,
                "gpa": "0x8fe00000",
                "size": 40,
                "sourceLine": 1,
                "assembledFileName": "guest-vm-1.final.dtb",
                "segments": [
                    {
                        "index": 0,
                        "hpa": "0x90000000",
                        "length": 20,
                        "fileName": "guest-vm-1.segment-000.bin",
                        "qmpOperation": "pmemsave",
                    },
                    {
                        "index": 1,
                        "hpa": "0x91000000",
                        "length": 20,
                        "fileName": "guest-vm-1.segment-001.bin",
                        "qmpOperation": "pmemsave",
                    },
                ],
            }
        ],
    }


def valid_execution(
    *,
    plan_sha: str,
    runtime_sha: str,
    segment_bytes: list[bytes],
) -> dict[str, object]:
    segments = []
    for index, data in enumerate(segment_bytes):
        segments.append(
            {
                "vmId": 1,
                "index": index,
                "hpa": f"{0x9000_0000 + index * 0x0100_0000:#x}",
                "length": len(data),
                "path": f"guest-vm-1.segment-{index:03d}.bin",
                "sha256": sha256(data),
                "qmpOperation": "pmemsave",
            }
        )
    return {
        "schemaVersion": 1,
        "artifactStatus": "capture-generated-unreviewed",
        "status": "qmp_pmemsave_completed_exact_size",
        "proofScope": "final-guest-dtb-qmp-pmemsave-byte-capture",
        "sourcePathBase": "artifact parent directory",
        "source": {
            "capturePlan": {
                "path": "capture-plan.json",
                "sha256": plan_sha,
            },
            "axvisorLog": {
                "path": "axvisor.log",
                "sha256": runtime_sha,
            },
        },
        "qmp": {
            "capabilitiesNegotiated": True,
            "operation": "pmemsave",
            "addressSpace": "host-physical",
        },
        "guests": [
            {
                "vmId": 1,
                "gpa": "0x8fe00000",
                "size": sum(len(data) for data in segment_bytes),
                "segments": segments,
            }
        ],
    }


class MemoryAssemblyStorage:
    def __init__(self, error_type: type[Exception]) -> None:
        self.error_type = error_type
        self.files: dict[str, bytes] = {}
        self.publication_order: list[str] = []

    @contextmanager
    def transaction(self):
        yield Path("memory-assembly-staging").resolve()

    @staticmethod
    def key(path: Path) -> str:
        return str(path.resolve())

    def contains(self, path: Path) -> bool:
        return self.key(path) in self.files

    def ensure_absent(self, paths: list[Path]) -> None:
        for path in paths:
            if self.contains(path):
                raise self.error_type(f"assembly path {path.name} already exists")

    def write_staging(self, path: Path, data: bytes) -> None:
        key = self.key(path)
        if key in self.files:
            raise self.error_type(f"assembly path {path.name} already exists")
        self.files[key] = data

    def publish(
        self,
        staging_path: Path,
        final_path: Path,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> None:
        staging_key = self.key(staging_path)
        final_key = self.key(final_path)
        if final_key in self.files:
            raise self.error_type(f"assembly path {final_path.name} already exists")
        data = self.files[staging_key]
        if expected_size is not None and len(data) != expected_size:
            raise self.error_type("assembly staging size changed")
        if expected_sha256 is not None and sha256(data) != expected_sha256:
            raise self.error_type("assembly staging digest changed")
        self.files[final_key] = self.files.pop(staging_key)
        self.publication_order.append(final_key)

    def cleanup(self, path: Path) -> None:
        self.files.pop(self.key(path), None)

    def sync_directory(self) -> None:
        pass


def expect_rejected(
    errors: list[str],
    assembler: ModuleType,
    plan: dict[str, object],
    execution: dict[str, object],
    reader: object,
    *,
    capture_plan_sha256: str,
    label: str,
    expected_message: str,
) -> None:
    try:
        assembler.assemble_guest_dtbs(
            plan,
            execution,
            Path("."),
            capture_plan_name="capture-plan.json",
            capture_plan_sha256=capture_plan_sha256,
            evidence_reader=reader,
        )
    except assembler.GuestDtbAssemblyError as error:
        if expected_message not in str(error):
            errors.append(f"assembler reports the wrong {label} error: {error}")
    else:
        errors.append(f"assembler accepts {label}")


def main() -> int:
    errors: list[str] = []

    if not ASSEMBLER.is_file():
        errors.append("final Guest DTB assembler is missing")
    else:
        try:
            assembler = load_module("guest_dtb_assembler", ASSEMBLER)
        except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
            errors.append(f"final Guest DTB assembler cannot be imported: {error}")
        else:
            try:
                executor = load_module("guest_dtb_capture_executor", EXECUTOR)
            except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
                errors.append(f"Guest DTB executor cannot be imported: {error}")
            for name in (
                "GuestDtbAssemblyError",
                "assemble_guest_dtbs",
                "build_capture_result",
                "publish_capture_result",
                "parse_args",
            ):
                if not hasattr(assembler, name):
                    errors.append(f"Guest DTB assembler does not expose {name}")

            if not errors:
                with redirect_stderr(io.StringIO()):
                    try:
                        assembler.parse_args(
                            [
                                "--plan",
                                "capture-plan.json",
                                "--output",
                                "capture-result.json",
                            ]
                        )
                    except SystemExit as error:
                        if error.code != 2:
                            errors.append(
                                "CLI returns the wrong code without --execution"
                            )
                    else:
                        errors.append("CLI does not require --execution")
                try:
                    parsed = assembler.parse_args(
                        [
                            "--plan",
                            "capture-plan.json",
                            "--execution",
                            "capture-execution.json",
                            "--output",
                            "capture-result.json",
                        ]
                    )
                except SystemExit as error:
                    errors.append(f"CLI rejects --execution: {error}")
                else:
                    if parsed.execution != Path("capture-execution.json"):
                        errors.append("CLI does not retain --execution")

                runtime = (
                    b"AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=40 "
                    b"hpa_segments=0x90000000:20,0x91000000:20\n"
                )
                dtb = minimal_fdt()
                files = {
                    "axvisor.log": runtime,
                    "guest-vm-1.segment-000.bin": dtb[:20],
                    "guest-vm-1.segment-001.bin": dtb[20:],
                }

                def read_evidence(filename: str, _expected_size: int | None) -> bytes:
                    return files[filename]

                plan = valid_plan(sha256(runtime))
                plan_bytes = json.dumps(plan, sort_keys=True).encode("utf-8")
                plan_sha = sha256(plan_bytes)
                fixture_execution = valid_execution(
                    plan_sha=plan_sha,
                    runtime_sha=sha256(runtime),
                    segment_bytes=[dtb[:20], dtb[20:]],
                )
                execution = executor._execution_result(
                    {
                        "source": fixture_execution["source"],
                        "guests": [
                            {
                                "vmId": 1,
                                "gpa": "0x8fe00000",
                                "size": len(dtb),
                            }
                        ],
                    },
                    fixture_execution["guests"][0]["segments"],
                )

                try:
                    assembler.decode_evidence_json(
                        b'{"schemaVersion":1,"schemaVersion":1}',
                        field="capture plan",
                    )
                except assembler.GuestDtbAssemblyError as error:
                    if "duplicate JSON key" not in str(error):
                        errors.append(
                            "assembler reports the wrong duplicate-key error: "
                            f"{error}"
                        )
                else:
                    errors.append("assembler accepts duplicate JSON keys")

                markerless = b"NO MARKERS HERE\n"
                forged_plan = valid_plan(sha256(markerless))
                forged_plan_bytes = json.dumps(forged_plan, sort_keys=True).encode(
                    "utf-8"
                )
                forged_execution = valid_execution(
                    plan_sha=sha256(forged_plan_bytes),
                    runtime_sha=sha256(markerless),
                    segment_bytes=[dtb[:20], dtb[20:]],
                )
                forged_files = dict(files)
                forged_files["axvisor.log"] = markerless
                expect_rejected(
                    errors,
                    assembler,
                    forged_plan,
                    forged_execution,
                    lambda filename, _size: forged_files[filename],
                    capture_plan_sha256=sha256(forged_plan_bytes),
                    label="marker-free self-consistent evidence chain",
                    expected_message="runtime markers",
                )

                boolean_schema = copy.deepcopy(plan)
                boolean_schema["schemaVersion"] = True
                expect_rejected(
                    errors,
                    assembler,
                    boolean_schema,
                    execution,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="boolean schema version",
                    expected_message="runtime markers",
                )
                try:
                    assemblies = assembler.assemble_guest_dtbs(
                        plan,
                        execution,
                        Path("."),
                        capture_plan_name="capture-plan.json",
                        capture_plan_sha256=plan_sha,
                        evidence_reader=read_evidence,
                    )
                    result = assembler.build_capture_result(
                        assemblies=assemblies,
                        capture_plan_name="capture-plan.json",
                        capture_plan_sha256=plan_sha,
                        capture_execution_name="capture-execution.json",
                        capture_execution_sha256="b" * 64,
                    )
                except Exception as error:  # noqa: BLE001 - preserve diagnosis.
                    errors.append(f"assembler rejects valid segment files: {error}")
                else:
                    if len(assemblies) != 1 or assemblies[0].get("data") != dtb:
                        errors.append("assembler changes or omits captured DTB bytes")
                    guest = result.get("guests", [{}])[0]
                    if guest.get("vmId") != 1 or guest.get("sha256") != sha256(dtb):
                        errors.append("capture result has wrong VM id or DTB hash")
                    if result.get("status") != "dtb_bytes_assembled_header_validated":
                        errors.append(
                            "capture result does not expose its narrow status"
                        )
                    execution_source = result.get("source", {}).get(
                        "captureExecution", {}
                    )
                    if execution_source != {
                        "path": "capture-execution.json",
                        "sha256": "b" * 64,
                    }:
                        errors.append(
                            "capture result omits its execution-manifest hash"
                        )
                    boundaries = set(result.get("doesNotProve", []))
                    for boundary in (
                        "device-tree semantic validity",
                        "both guests booted",
                        "passthrough DMA is isolated",
                        "Linux and Zephyr have IP connectivity",
                    ):
                        if boundary not in boundaries:
                            errors.append(
                                f"capture result omits proof boundary `{boundary}`"
                            )

                    storage = MemoryAssemblyStorage(
                        assembler.GuestDtbAssemblyError
                    )
                    evidence = (WORKSPACE_ROOT / "virtual-assembly").resolve()
                    output = evidence / "capture-result.json"
                    serialized = json.dumps(result).encode("utf-8")
                    try:
                        assembler.publish_capture_result(
                            assemblies,
                            serialized,
                            evidence,
                            output,
                            storage=storage,
                        )
                    except Exception as error:  # noqa: BLE001 - preserve diagnosis.
                        errors.append(f"assembler cannot publish valid output: {error}")
                    else:
                        assembled_path = evidence / "guest-vm-1.final.dtb"
                        if not storage.contains(assembled_path):
                            errors.append("assembler does not publish the final DTB")
                        if not storage.contains(output):
                            errors.append("assembler does not publish its manifest")
                        elif storage.publication_order[-1] != storage.key(output):
                            errors.append(
                                "assembler does not publish its manifest last"
                            )
                        original_files = dict(storage.files)
                        try:
                            assembler.publish_capture_result(
                                assemblies,
                                serialized,
                                evidence,
                                output,
                                storage=storage,
                            )
                        except assembler.GuestDtbAssemblyError as error:
                            if "already exists" not in str(error):
                                errors.append(
                                    "assembler reports the wrong no-overwrite "
                                    f"error: {error}"
                                )
                        else:
                            errors.append("assembler overwrites published evidence")
                        if storage.files != original_files:
                            errors.append("failed overwrite changes published evidence")

                files["guest-vm-1.segment-001.bin"] = dtb[20:-1]
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    execution,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="short QMP segment",
                    expected_message="has 19 bytes, expected 20",
                )
                files["guest-vm-1.segment-001.bin"] = dtb[20:]

                escaped = copy.deepcopy(plan)
                escaped["guests"][0]["segments"][0]["fileName"] = "../outside.bin"
                expect_rejected(
                    errors,
                    assembler,
                    escaped,
                    execution,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="escaping segment path",
                    expected_message="runtime markers",
                )

                bad_magic = bytearray(dtb)
                bad_magic[:4] = b"BAD!"
                files["guest-vm-1.segment-000.bin"] = bytes(bad_magic[:20])
                bad_magic_execution = valid_execution(
                    plan_sha=plan_sha,
                    runtime_sha=sha256(runtime),
                    segment_bytes=[bytes(bad_magic[:20]), dtb[20:]],
                )
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    bad_magic_execution,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="bad FDT magic",
                    expected_message="does not start with flattened-DT magic",
                )

                bad_total = bytearray(dtb)
                bad_total[4:8] = struct.pack(">I", 39)
                files["guest-vm-1.segment-000.bin"] = bytes(bad_total[:20])
                bad_total_execution = valid_execution(
                    plan_sha=plan_sha,
                    runtime_sha=sha256(runtime),
                    segment_bytes=[bytes(bad_total[:20]), dtb[20:]],
                )
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    bad_total_execution,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="FDT totalsize mismatch",
                    expected_message=(
                        "header totalsize 39 does not match captured size 40"
                    ),
                )

                bad_offset = bytearray(dtb)
                bad_offset[8:12] = struct.pack(">I", 41)
                files["guest-vm-1.segment-000.bin"] = bytes(bad_offset[:20])
                bad_offset_execution = valid_execution(
                    plan_sha=plan_sha,
                    runtime_sha=sha256(runtime),
                    segment_bytes=[bytes(bad_offset[:20]), dtb[20:]],
                )
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    bad_offset_execution,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="out-of-range FDT section",
                    expected_message="structure block exceeds totalsize",
                )

                files["guest-vm-1.segment-000.bin"] = dtb[:20]
                wrong_source = copy.deepcopy(plan)
                wrong_source["source"]["axvisorLog"]["sha256"] = "0" * 64
                expect_rejected(
                    errors,
                    assembler,
                    wrong_source,
                    execution,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="changed marker source",
                    expected_message="AxVisor log SHA-256 does not match capture plan",
                )

                wrong_status = copy.deepcopy(plan)
                wrong_status["status"] = "passed"
                expect_rejected(
                    errors,
                    assembler,
                    wrong_status,
                    execution,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="overstated input plan",
                    expected_message="capture plan status must be capture_planned",
                )

                missing_execution: dict[str, object] = {}
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    missing_execution,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="missing execution manifest",
                    expected_message="capture execution schemaVersion must be 1",
                )

                wrong_execution_status = copy.deepcopy(execution)
                wrong_execution_status["status"] = "capture_started"
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    wrong_execution_status,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="incomplete execution manifest",
                    expected_message=(
                        "capture execution status must be "
                        "qmp_pmemsave_completed_exact_size"
                    ),
                )

                boolean_execution_schema = copy.deepcopy(execution)
                boolean_execution_schema["schemaVersion"] = True
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    boolean_execution_schema,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="boolean execution schema version",
                    expected_message="capture execution schemaVersion must be 1",
                )

                stale_execution = copy.deepcopy(execution)
                stale_execution["source"]["capturePlan"]["sha256"] = "0" * 64
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    stale_execution,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="stale execution manifest",
                    expected_message=(
                        "capture execution plan SHA-256 does not match current "
                        "capture plan"
                    ),
                )

                wrong_execution_log = copy.deepcopy(execution)
                wrong_execution_log["source"]["axvisorLog"]["sha256"] = "0" * 64
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    wrong_execution_log,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="execution with another source log",
                    expected_message=(
                        "capture execution AxVisor log does not match capture plan"
                    ),
                )

                missing_segment = copy.deepcopy(execution)
                missing_segment["guests"][0]["segments"].pop()
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    missing_segment,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="execution with a missing segment",
                    expected_message=(
                        "execution segment count does not match capture plan"
                    ),
                )

                segment_field_cases = (
                    ("vmId", 2, "VM id"),
                    ("vmId", True, "VM id"),
                    ("index", 1, "index"),
                    ("index", False, "index"),
                    ("hpa", "0x92000000", "HPA"),
                    ("length", 19, "length"),
                    ("path", "guest-vm-1.segment-001.bin", "filename"),
                )
                for field, value, diagnostic in segment_field_cases:
                    changed_execution = copy.deepcopy(execution)
                    changed_execution["guests"][0]["segments"][0][field] = value
                    expect_rejected(
                        errors,
                        assembler,
                        plan,
                        changed_execution,
                        read_evidence,
                        capture_plan_sha256=plan_sha,
                        label=f"execution with changed segment {field}",
                        expected_message=(
                            f"execution segment {diagnostic} does not match "
                            "capture plan"
                        ),
                    )

                tampered_sha = copy.deepcopy(execution)
                tampered_sha["guests"][0]["segments"][0]["sha256"] = "0" * 64
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    tampered_sha,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="tampered execution segment hash",
                    expected_message="SHA-256 does not match capture execution",
                )

                swapped_records = copy.deepcopy(execution)
                swapped_records["guests"][0]["segments"].reverse()
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    swapped_records,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="swapped execution segment records",
                    expected_message=(
                        "execution segment index does not match capture plan"
                    ),
                )

                files["guest-vm-1.segment-000.bin"] = dtb[20:]
                files["guest-vm-1.segment-001.bin"] = dtb[:20]
                expect_rejected(
                    errors,
                    assembler,
                    plan,
                    execution,
                    read_evidence,
                    capture_plan_sha256=plan_sha,
                    label="swapped captured segment bytes",
                    expected_message="SHA-256 does not match capture execution",
                )
                files["guest-vm-1.segment-000.bin"] = dtb[:20]
                files["guest-vm-1.segment-001.bin"] = dtb[20:]

    ci = CI.read_text(encoding="utf-8")
    expected_ci = "python3 scripts/test/check_axvisor_guest_dtb_assembler.py"
    if expected_ci not in ci:
        errors.append("final Guest DTB assembly contract is not wired into CI")
    if ASSEMBLER.is_file():
        assembler_source = ASSEMBLER.read_text(encoding="utf-8")
        if len(assembler_source.splitlines()) >= 800:
            errors.append("final Guest DTB assembler must remain below 800 lines")

    if not errors:
        return 0

    print("AxVisor final Guest DTB assembly contract check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
