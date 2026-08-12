#!/usr/bin/env python3
"""Behavioral contract for fail-closed QMP Guest DTB capture execution."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
EXECUTOR = WORKSPACE_ROOT / "scripts/contest/execute_guest_dtb_capture.py"
IO_HELPER = WORKSPACE_ROOT / "scripts/contest/guest_dtb_capture_io.py"
README = WORKSPACE_ROOT / "scripts/contest/README.md"
CI = WORKSPACE_ROOT / ".github/workflows/ci.yml"


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


PLAN_BOUNDARIES = [
    "physical bytes were captured",
    "captured bytes form valid flattened device trees",
    "both guests booted",
    "passthrough DMA is isolated",
    "Linux and Zephyr have IP connectivity",
]


def canonical_fixture(
    guests: list[tuple[int, int, list[tuple[int, int]]]] | None = None,
) -> tuple[bytes, dict[str, object]]:
    if guests is None:
        guests = [
            (1, 0x8FE00000, [(0x90000000, 20), (0x91000000, 20)])
        ]
    markers = []
    plan_guests = []
    for source_line, (vm_id, gpa, segments) in enumerate(guests, start=1):
        size = sum(length for _, length in segments)
        marker_segments = ",".join(
            f"{hpa:#x}:{length}" for hpa, length in segments
        )
        markers.append(
            f"AXVISOR_GUEST_DTB_READY vm={vm_id} gpa={gpa:#x} size={size} "
            f"hpa_segments={marker_segments}"
        )
        plan_guests.append(
            {
                "vmId": vm_id,
                "gpa": f"{gpa:#x}",
                "size": size,
                "sourceLine": source_line,
                "assembledFileName": f"guest-vm-{vm_id}.final.dtb",
                "segments": [
                    {
                        "index": index,
                        "hpa": f"{hpa:#x}",
                        "length": length,
                        "fileName": f"guest-vm-{vm_id}.segment-{index:03d}.bin",
                        "qmpOperation": "pmemsave",
                    }
                    for index, (hpa, length) in enumerate(segments)
                ],
            }
        )
    runtime = ("\n".join(markers) + "\n").encode("utf-8")
    return runtime, {
        "schemaVersion": 1,
        "artifactStatus": "marker-derived-unreviewed",
        "status": "capture_planned",
        "proofScope": "final-guest-dtb-physical-capture-plan",
        "doesNotProve": PLAN_BOUNDARIES,
        "sourcePathBase": "capture-plan.json parent directory",
        "source": {
            "axvisorLog": {"path": "axvisor.log", "sha256": sha256(runtime)}
        },
        "guests": plan_guests,
    }


class MemoryCaptureStorage:
    def __init__(
        self,
        error_type: type[Exception],
        evidence_directory: Path,
        *,
        fail_cleanup: bool = False,
        tamper_before_publish: bool = False,
    ) -> None:
        self.error_type = error_type
        self.staging_directory = evidence_directory / ".guest-dtb-qmp-private-test"
        self.files: dict[str, bytes] = {}
        self.owned: set[str] = set()
        self.publication_order: list[str] = []
        self.fail_cleanup = fail_cleanup
        self.tamper_before_publish = tamper_before_publish
        self.locked = False

    @staticmethod
    def key(path: Path | str) -> str:
        return str(Path(path).resolve())

    @contextmanager
    def transaction(self):
        if self.locked:
            raise RuntimeError("capture storage lock is already held")
        self.locked = True
        try:
            yield self.staging_directory
        finally:
            self.locked = False

    def ensure_absent(self, paths: list[Path]) -> None:
        for path in paths:
            if self.key(path) in self.files:
                raise RuntimeError(f"capture path {path.name} already exists")

    def validate_staging(self, path: Path, expected_size: int) -> str:
        data = self.files.get(self.key(path))
        if data is None:
            raise RuntimeError(f"QMP did not create {path.name}")
        self.owned.add(self.key(path))
        if len(data) != expected_size:
            raise self.error_type(
                f"evidence file {path.name} has {len(data)} bytes, "
                f"expected {expected_size}"
            )
        return sha256(data)

    def write_staging(self, path: Path, data: bytes) -> None:
        key = self.key(path)
        if key in self.files:
            raise RuntimeError(f"staging file {path.name} already exists")
        self.files[key] = data
        self.owned.add(key)
        if self.tamper_before_publish and path.name == "capture-execution.manifest":
            segment_key = self.key(
                self.staging_directory / "guest-vm-1.segment-000.bin"
            )
            segment = self.files[segment_key]
            self.files[segment_key] = bytes([segment[0] ^ 0xFF]) + segment[1:]

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
        if staging_key not in self.owned or final_key in self.files:
            raise RuntimeError("invalid in-memory publication")
        data = self.files[staging_key]
        if expected_size is not None and len(data) != expected_size:
            raise self.error_type("staging bytes changed before publication")
        if expected_sha256 is not None and sha256(data) != expected_sha256:
            raise self.error_type("staging digest changed before publication")
        self.files[final_key] = self.files.pop(staging_key)
        self.owned.remove(staging_key)
        self.owned.add(final_key)
        self.publication_order.append(final_key)

    def cleanup(self, path: Path) -> None:
        key = self.key(path)
        if key not in self.owned:
            return
        if self.fail_cleanup:
            raise self.error_type(f"injected cleanup failure for {path.name}")
        self.files.pop(key, None)
        self.owned.remove(key)

    def sync_directory(self) -> None:
        pass

    def contains(self, path: Path) -> bool:
        return self.key(path) in self.files


class FakeQmpSession:
    def __init__(
        self,
        storage: MemoryCaptureStorage,
        *,
        short_sequence: int | None = None,
    ) -> None:
        self.storage = storage
        self.negotiated = False
        self.requests: list[dict[str, object]] = []
        self.short_sequence = short_sequence

    def negotiate(self) -> None:
        self.negotiated = True

    def execute(self, request: dict[str, object]) -> dict[str, object]:
        if not self.negotiated:
            raise RuntimeError("request sent before QMP negotiation")
        self.requests.append(copy.deepcopy(request))
        arguments = request["arguments"]
        size = int(arguments["size"])
        sequence = len(self.requests) - 1
        if sequence == self.short_sequence:
            size -= 1
        self.storage.files[self.storage.key(str(arguments["filename"]))] = (
            bytes([sequence + 1]) * size
        )
        return {"return": {}, "id": request["id"]}


def expect_rejected(
    errors: list[str],
    executor: ModuleType,
    plan: dict[str, object],
    evidence_directory: Path,
    *,
    label: str,
    expected_message: str,
) -> None:
    runtime, _ = canonical_fixture()

    def read_source(filename: str) -> bytes:
        if filename != "axvisor.log":
            raise KeyError(filename)
        return runtime

    try:
        executor.build_qmp_command_plan(
            plan,
            evidence_directory,
            capture_plan_name="capture-plan.json",
            capture_plan_sha256="a" * 64,
            source_reader=read_source,
        )
    except executor.GuestDtbCaptureError as error:
        if expected_message not in str(error):
            errors.append(f"executor reports the wrong {label} error: {error}")
    else:
        errors.append(f"executor accepts {label}")


def run_execution_contract(errors: list[str], executor: ModuleType) -> None:
    runtime, plan = canonical_fixture()
    evidence = (WORKSPACE_ROOT / "virtual-evidence-success").resolve()
    plan_bytes = json.dumps(plan).encode("utf-8")
    output = evidence / "capture-execution.json"
    storage = MemoryCaptureStorage(
        executor.GuestDtbCaptureError, evidence
    )
    session = FakeQmpSession(storage)

    def read_source(filename: str) -> bytes:
        if filename != "axvisor.log":
            raise KeyError(filename)
        return runtime

    try:
        result = executor.execute_capture(
            plan,
            evidence,
            capture_plan_name="capture-plan.json",
            capture_plan_sha256=sha256(plan_bytes),
            output_path=output,
            session=session,
            source_reader=read_source,
            storage=storage,
        )
    except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
        errors.append(f"executor rejects a valid fake-QMP capture: {error}")
    else:
        if not session.negotiated or len(session.requests) != 2:
            errors.append("executor does not negotiate QMP and run every segment")
        values = [request["arguments"]["val"] for request in session.requests]
        if values != [0x90000000, 0x91000000]:
            errors.append("executor does not use marker-derived HPA values")
        if 0x8FE00000 in values:
            errors.append("executor substitutes Guest GPA for physical HPA")
        for index in range(2):
            final_path = evidence / f"guest-vm-1.segment-{index:03d}.bin"
            if not storage.contains(final_path):
                errors.append(f"executor omits exact segment {index} evidence")
        staging_parents = {
            Path(str(request["arguments"]["filename"])).parent
            for request in session.requests
        }
        if staging_parents != {storage.staging_directory.resolve()}:
            errors.append("executor does not isolate QMP files in one private directory")
        if any(
            storage.key(path).startswith(storage.key(storage.staging_directory))
            for path in storage.files
        ):
            errors.append("executor leaves private staging evidence behind")
        if not storage.contains(output):
            errors.append("executor does not publish its evidence manifest")
        elif not storage.publication_order or storage.publication_order[-1] != storage.key(output):
            errors.append("executor does not publish its evidence manifest last")
        if result.get("status") != "qmp_pmemsave_completed_exact_size":
            errors.append("executor overstates or changes its success status")
        boundaries = set(result.get("doesNotProve", []))
        for boundary in (
            "captured bytes form valid flattened device trees",
            "both guests booted",
            "passthrough DMA is isolated",
            "Linux and Zephyr have IP connectivity",
        ):
            if boundary not in boundaries:
                errors.append(f"execution result omits proof boundary `{boundary}`")

    failed_evidence = (WORKSPACE_ROOT / "virtual-evidence-short-write").resolve()
    failed_output = failed_evidence / "capture-execution.json"
    failed_storage = MemoryCaptureStorage(
        executor.GuestDtbCaptureError, failed_evidence
    )
    try:
        executor.execute_capture(
            plan,
            failed_evidence,
            capture_plan_name="capture-plan.json",
            capture_plan_sha256=sha256(plan_bytes),
            output_path=failed_output,
            session=FakeQmpSession(failed_storage, short_sequence=1),
            source_reader=read_source,
            storage=failed_storage,
        )
    except executor.GuestDtbCaptureError as error:
        if "has 19 bytes, expected 20" not in str(error):
            errors.append(f"executor reports the wrong short-write error: {error}")
    except Exception as error:  # noqa: BLE001 - preserve diagnosis.
        errors.append(f"executor leaks a raw short-write exception: {error}")
    else:
        errors.append("executor accepts a short QMP pmemsave output")
    if failed_storage.files:
        errors.append("failed capture publishes or leaks staged evidence")
    if failed_storage.contains(failed_output):
        errors.append("failed capture publishes an execution manifest")

    tampered_evidence = (WORKSPACE_ROOT / "virtual-evidence-tampered").resolve()
    tampered_storage = MemoryCaptureStorage(
        executor.GuestDtbCaptureError,
        tampered_evidence,
        tamper_before_publish=True,
    )
    try:
        executor.execute_capture(
            plan,
            tampered_evidence,
            capture_plan_name="capture-plan.json",
            capture_plan_sha256=sha256(plan_bytes),
            output_path=tampered_evidence / "capture-execution.json",
            session=FakeQmpSession(tampered_storage),
            source_reader=read_source,
            storage=tampered_storage,
        )
    except executor.GuestDtbCaptureError as error:
        if "changed before publication" not in str(error):
            errors.append(f"executor reports the wrong staging-tamper error: {error}")
    else:
        errors.append("executor publishes a segment changed after initial hashing")
    if tampered_storage.files:
        errors.append("staging-tamper rollback leaks evidence")

    cleanup_storage = MemoryCaptureStorage(
        executor.GuestDtbCaptureError,
        (WORKSPACE_ROOT / "virtual-evidence-cleanup-failure").resolve(),
        fail_cleanup=True,
    )
    try:
        executor.execute_capture(
            plan,
            cleanup_storage.staging_directory.parent,
            capture_plan_name="capture-plan.json",
            capture_plan_sha256=sha256(plan_bytes),
            output_path=cleanup_storage.staging_directory.parent
            / "capture-execution.json",
            session=FakeQmpSession(cleanup_storage, short_sequence=1),
            source_reader=read_source,
            storage=cleanup_storage,
        )
    except executor.GuestDtbCaptureError as error:
        if "cleanup" not in str(error) and "rollback" not in str(error):
            errors.append(f"executor hides cleanup failure context: {error}")
    except Exception as error:  # noqa: BLE001 - preserve diagnosis.
        errors.append(f"executor leaks a raw cleanup exception: {error}")
    else:
        errors.append("executor reports success despite an injected cleanup failure")


def main() -> int:
    errors: list[str] = []

    if not EXECUTOR.is_file():
        errors.append("final Guest DTB QMP capture executor is missing")
    else:
        try:
            executor = load_module("guest_dtb_capture_executor", EXECUTOR)
        except Exception as error:  # noqa: BLE001 - aggregate diagnostics.
            errors.append(f"final Guest DTB capture executor cannot be imported: {error}")
        else:
            for name in (
                "GuestDtbCaptureError",
                "decode_capture_plan",
                "build_qmp_command_plan",
                "execute_capture",
                "UnixQmpSession",
            ):
                if not hasattr(executor, name):
                    errors.append(f"Guest DTB capture executor does not expose {name}")

            if not errors:
                runtime, plan = canonical_fixture()

                def read_source(filename: str) -> bytes:
                    if filename != "axvisor.log":
                        raise KeyError(filename)
                    return runtime

                try:
                    command_plan = executor.build_qmp_command_plan(
                        plan,
                        Path("evidence").resolve(),
                        capture_plan_name="capture-plan.json",
                        capture_plan_sha256="a" * 64,
                        source_reader=read_source,
                    )
                except Exception as error:  # noqa: BLE001 - preserve diagnosis.
                    errors.append(f"executor rejects a valid command plan: {error}")
                else:
                    repeated = executor.build_qmp_command_plan(
                        plan,
                        Path("evidence").resolve(),
                        capture_plan_name="capture-plan.json",
                        capture_plan_sha256="a" * 64,
                        source_reader=read_source,
                    )
                    if command_plan != repeated:
                        errors.append("commands-only plan is not deterministic")
                    serialized_commands = json.dumps(command_plan)
                    if str(Path("evidence").resolve()) in serialized_commands:
                        errors.append("commands-only plan leaks an absolute host path")
                    if "${AXVISOR_QMP_STAGING}" not in serialized_commands:
                        errors.append("commands-only plan omits its logical staging placeholder")
                    commands = command_plan.get("commands", [])
                    if len(commands) != 2:
                        errors.append("executor does not plan every HPA segment")
                    else:
                        requests = [entry.get("request", {}) for entry in commands]
                        if any(request.get("execute") != "pmemsave" for request in requests):
                            errors.append("executor plans an operation other than pmemsave")
                        values = [request["arguments"]["val"] for request in requests]
                        if values != [0x90000000, 0x91000000]:
                            errors.append("command plan does not preserve segment HPAs")
                        if values[0] == int(plan["guests"][0]["gpa"], 16):
                            errors.append("command plan assumes Guest GPA equals host HPA")
                    if command_plan.get("status") != "qmp_commands_planned":
                        errors.append("command plan does not expose its narrow status")
                    if "physical bytes were captured" not in set(
                        command_plan.get("doesNotProve", [])
                    ):
                        errors.append("command plan claims that bytes were captured")

                bad_operation = copy.deepcopy(plan)
                bad_operation["guests"][0]["segments"][0]["qmpOperation"] = "dumpdtb"
                expect_rejected(
                    errors,
                    executor,
                    bad_operation,
                    Path("evidence").resolve(),
                    label="outer dumpdtb substitution",
                    expected_message="does not match runtime markers",
                )

                missing_hpa = copy.deepcopy(plan)
                del missing_hpa["guests"][0]["segments"][0]["hpa"]
                expect_rejected(
                    errors,
                    executor,
                    missing_hpa,
                    Path("evidence").resolve(),
                    label="missing HPA with an available GPA",
                    expected_message="does not match runtime markers",
                )

                escaped = copy.deepcopy(plan)
                escaped["guests"][0]["segments"][0]["fileName"] = "../outside.bin"
                expect_rejected(
                    errors,
                    executor,
                    escaped,
                    Path("evidence").resolve(),
                    label="escaping evidence path",
                    expected_message="does not match runtime markers",
                )

                changed_source = copy.deepcopy(plan)
                changed_source["source"]["axvisorLog"]["sha256"] = "0" * 64
                expect_rejected(
                    errors,
                    executor,
                    changed_source,
                    Path("evidence").resolve(),
                    label="changed marker source",
                    expected_message="AxVisor log SHA-256 does not match capture plan",
                )

                tamper_cases = []
                for label, mutation in (
                    (
                        "boolean schema version",
                        lambda item: item.__setitem__("schemaVersion", True),
                    ),
                    (
                        "marker HPA",
                        lambda item: item["guests"][0]["segments"][0].__setitem__(
                            "hpa", "0xdead0000"
                        ),
                    ),
                    (
                        "Guest GPA",
                        lambda item: item["guests"][0].__setitem__(
                            "gpa", "0x8fd00000"
                        ),
                    ),
                    (
                        "DTB size",
                        lambda item: item["guests"][0].__setitem__("size", 41),
                    ),
                    (
                        "VM id",
                        lambda item: item["guests"][0].__setitem__("vmId", 2),
                    ),
                    (
                        "marker source line",
                        lambda item: item["guests"][0].__setitem__(
                            "sourceLine", 2
                        ),
                    ),
                ):
                    changed = copy.deepcopy(plan)
                    mutation(changed)
                    tamper_cases.append((label, changed))
                for label, changed in tamper_cases:
                    expect_rejected(
                        errors,
                        executor,
                        changed,
                        Path("evidence").resolve(),
                        label=label,
                        expected_message="does not match runtime markers",
                    )

                for label, payload, expected_message in (
                    (
                        "duplicate JSON key",
                        b'{"schemaVersion":1,"schemaVersion":1}',
                        "duplicate JSON key",
                    ),
                    (
                        "oversized capture plan",
                        b" " * (executor.MAX_CAPTURE_PLAN_BYTES + 1),
                        "exceeds byte limit",
                    ),
                ):
                    try:
                        executor.decode_capture_plan(payload)
                    except executor.GuestDtbCaptureError as error:
                        if expected_message not in str(error):
                            errors.append(
                                f"executor reports the wrong {label} error: {error}"
                            )
                    else:
                        errors.append(f"executor accepts {label}")

                for timeout in (float("nan"), float("inf"), float("-inf")):
                    try:
                        executor.UnixQmpSession(Path("unused"), timeout_seconds=timeout)
                    except executor.GuestDtbCaptureError:
                        pass
                    else:
                        errors.append(f"executor accepts non-finite timeout {timeout}")

                high_runtime, high_plan = canonical_fixture(
                    [(1, 0x8FE00000, [(1 << 63, 40)])]
                )
                try:
                    executor.build_qmp_command_plan(
                        high_plan,
                        Path("evidence").resolve(),
                        capture_plan_name="capture-plan.json",
                        capture_plan_sha256="a" * 64,
                        source_reader=lambda _name: high_runtime,
                    )
                except executor.GuestDtbCaptureError as error:
                    if "QMP int64" not in str(error):
                        errors.append(f"executor reports wrong QMP int64 error: {error}")
                else:
                    errors.append("executor accepts HPA above QMP int64 range")

                large_guests = [
                    (vm_id, 0x40000000 + vm_id * 0x02000000,
                     [(0x10000000 + vm_id * 0x02000000, 16 * 1024 * 1024)])
                    for vm_id in range(1, 4)
                ]
                large_runtime, large_plan = canonical_fixture(large_guests)
                try:
                    executor.build_qmp_command_plan(
                        large_plan,
                        Path("evidence").resolve(),
                        capture_plan_name="capture-plan.json",
                        capture_plan_sha256="a" * 64,
                        source_reader=lambda _name: large_runtime,
                    )
                except executor.GuestDtbCaptureError as error:
                    if "total capture byte limit" not in str(error):
                        errors.append(f"executor reports wrong total-size error: {error}")
                else:
                    errors.append("executor accepts an excessive aggregate capture")

                many_runtime, many_plan = canonical_fixture(
                    [
                        (
                            1,
                            0x8FE00000,
                            [
                                (0x20000000 + index, 1)
                                for index in range(
                                    executor.MAX_TOTAL_QMP_COMMANDS + 1
                                )
                            ],
                        )
                    ]
                )
                try:
                    executor.build_qmp_command_plan(
                        many_plan,
                        Path("evidence").resolve(),
                        capture_plan_name="capture-plan.json",
                        capture_plan_sha256="a" * 64,
                        source_reader=lambda _name: many_runtime,
                    )
                except executor.GuestDtbCaptureError as error:
                    if "total QMP command limit" not in str(error):
                        errors.append(f"executor reports wrong command-limit error: {error}")
                else:
                    errors.append("executor accepts too many QMP commands")

                run_execution_contract(errors, executor)

    readme = README.read_text(encoding="utf-8")
    for phrase in (
        "execute_guest_dtb_capture.py",
        "--commands-only",
        "--qmp-socket",
        "outer QEMU `dumpdtb`",
    ):
        if phrase not in readme:
            errors.append(f"capture executor README guidance omits `{phrase}`")

    if EXECUTOR.is_file() and IO_HELPER.is_file():
        executor_source = EXECUTOR.read_text(encoding="utf-8")
        io_source = IO_HELPER.read_text(encoding="utf-8")
        executor_source += io_source
        for token in (
            "socket.AF_UNIX",
            '"qmp_capabilities"',
            "MAX_QMP_MESSAGE_BYTES",
            "MAX_TOTAL_QMP_COMMANDS",
            "FILE_ATTRIBUTE_REPARSE_POINT",
            "O_NOFOLLOW",
            "tempfile.mkdtemp",
            "os.link",
            "follow_symlinks=False",
        ):
            if token not in executor_source:
                errors.append(f"capture executor source omits safety token `{token}`")
        lstat_index = io_source.find(
            'checked_lstat(plan, field="capture plan", kind="file")'
        )
        resolve_index = io_source.find("plan.parent.resolve(strict=True)")
        if lstat_index < 0 or resolve_index < 0 or lstat_index > resolve_index:
            errors.append("capture plan path is resolved before lexical lstat validation")
        for fragment in (
            "data, _ = read_bounded_regular_file(",
            "return decode_capture_plan(data), sha256_bytes(data)",
        ):
            if fragment not in io_source:
                errors.append("capture plan is not decoded and hashed from one byte read")
    elif not IO_HELPER.is_file():
        errors.append("capture executor filesystem helper is missing")

    ci = CI.read_text(encoding="utf-8")
    expected_ci = "python3 scripts/test/check_axvisor_guest_dtb_capture_executor.py"
    if expected_ci not in ci:
        errors.append("final Guest DTB capture-executor contract is not wired into CI")

    if not errors:
        return 0

    print("AxVisor final Guest DTB capture-executor contract failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
