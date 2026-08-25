#!/usr/bin/env python3
"""Deterministic, non-blocking host contract for the P5 control loop.

This module is deliberately a host/static candidate.  It uses a virtual clock
and the already-frozen Python reference/model helpers; it does not open a
socket, start a Guest, invoke QEMU, or sleep.  Its output is therefore always
``execution_kind=host_contract`` and ``evidence_level=L2 host contract``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_vm_carveout_io import publish_new_file  # noqa: E402

try:  # Support package imports and direct script/test execution.
    from . import closed_loop_contract, model
except ImportError:  # pragma: no cover - exercised by direct entry points.
    import closed_loop_contract  # type: ignore[no-redef]
    import model  # type: ignore[no-redef]


HOST_SESSION_SCHEMA = "p5-ai-session-v1"
HOST_MANIFEST_SCHEMA = "p5-ai-manifest-v1"
HOST_STATUS_SCHEMA = "p5-ai-status-v1"
HOST_CYCLE_SCHEMA = "p5-ai-host-cycle-v1"
HOST_SUMMARY_SCHEMA = "p5-ai-host-summary-v1"
HOST_CONTRACT_STATUS = "host_contract_valid"
HOST_CONTRACT_EVIDENCE = "L2 host contract"
TICK_PERIOD_MS = 100
TICKS = 1_800
RUN_DURATION_SECONDS = 180
CONTROL_VALIDITY_MS = 500
DEADLINE_BUDGET_MS = 100
SAFE_DUTY_Q16_16 = 0
MODEL_ARTIFACTS = (
    "model.bin",
    "metadata.json",
    "dataset-manifest.json",
    "golden-vectors.json",
)
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class HostContractError(ValueError):
    """Raised when a host candidate would violate a frozen P5 identity."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HostContractError(message)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_profile_contract(profile: Mapping[str, Any]) -> None:
    """Reject every profile shape except the frozen TEST-017 host contract."""

    _require(profile.get("schema_version") == closed_loop_contract.PROFILE_SCHEMA, "profile schema drift")
    _require(profile.get("profile_id") == "qualification-v1", "profile_id drift")
    _require(profile.get("test_id") == "TEST-017", "profile is not TEST-017")
    _require(profile.get("controllers") == ["fixed", "mlp"], "controller set drift")
    _require(profile.get("seeds") == [7, 19, 43], "seed set drift")

    timing = profile.get("timing")
    _require(isinstance(timing, Mapping), "profile timing is not an object")
    _require(timing.get("tick_period_ms") == TICK_PERIOD_MS, "tick period must be 100 ms")
    _require(timing.get("ticks") == TICKS, "host contract requires exactly 1800 ticks")
    _require(
        timing.get("duration_seconds") == RUN_DURATION_SECONDS,
        "host contract requires exactly 180 seconds",
    )
    _require(timing.get("control_validity_ms") == CONTROL_VALIDITY_MS, "CONTROL validity must be 500 ms")
    _require(timing.get("udp_retry_ms") == [0, 100, 300], "UDP retry schedule drift")
    _require(timing.get("udp_cancel_ms") == CONTROL_VALIDITY_MS, "UDP cancel deadline must be 500 ms")
    _require(
        int(timing["ticks"]) * int(timing["tick_period_ms"])
        == int(timing["duration_seconds"]) * 1_000,
        "tick count and duration are inconsistent",
    )

    evidence = profile.get("evidence")
    _require(isinstance(evidence, Mapping), "profile evidence is not an object")
    _require(evidence.get("level") == "L8 AI-loop", "qualification source level drifted")
    _require(evidence.get("success_status") == "ai_control_completed", "success token drift")
    _require(evidence.get("failed_status") == "ai_control_failed", "failure token drift")
    _require(evidence.get("blocked_status") == "ai_control_blocked", "blocked token drift")

    model_identity = profile.get("model")
    _require(isinstance(model_identity, Mapping), "profile model is not an object")
    _require(model_identity.get("schema_version") == "p5-mlp-model-v1", "model schema drift")
    _require(model_identity.get("model_version") == closed_loop_contract.MODEL_VERSION, "model_version drift")
    _require(model_identity.get("model_sha256") == closed_loop_contract.MODEL_SHA256, "model SHA-256 drift")
    _require(model_identity.get("metadata_sha256") == closed_loop_contract.MODEL_METADATA_SHA256, "metadata SHA-256 drift")
    _require(model_identity.get("golden_vectors_sha256") == closed_loop_contract.GOLDEN_VECTORS_SHA256, "golden vectors SHA-256 drift")
    _require(model_identity.get("dataset_manifest_sha256") == closed_loop_contract.DATASET_MANIFEST_SHA256, "dataset manifest SHA-256 drift")


def _validate_model_artifacts(model_directory: Path, profile: Mapping[str, Any]) -> bytes:
    """Verify all canonical model inputs and return the model bytes."""

    expected = profile["model"]
    _require(isinstance(expected, Mapping), "profile model is not an object")
    expected_hashes = {
        "model.bin": expected["model_sha256"],
        "metadata.json": expected["metadata_sha256"],
        "golden-vectors.json": expected["golden_vectors_sha256"],
        "dataset-manifest.json": expected["dataset_manifest_sha256"],
    }
    data: dict[str, bytes] = {}
    for name in MODEL_ARTIFACTS:
        path = model_directory / name
        _require(path.is_file(), f"missing canonical model artifact: {name}")
        content = path.read_bytes()
        _require(_sha256(content) == expected_hashes[name], f"model artifact hash mismatch: {name}")
        data[name] = content

    metadata = json.loads(data["metadata.json"].decode("utf-8"))
    _require(isinstance(metadata, Mapping), "model metadata is not an object")
    _require(metadata.get("schema_version") == "p5-mlp-model-v1", "model metadata schema drift")
    _require(metadata.get("shape") == [3, 8, 1], "MLP shape is not 3->8->1")
    _require(metadata.get("model_version") == expected["model_version"], "model version mismatch")
    _require(metadata.get("model_sha256") == expected["model_sha256"], "metadata model hash mismatch")
    return data["model.bin"]


@dataclass(frozen=True)
class HostIdentity:
    """The identity fields that every host dry-run must bind."""

    run_id: str
    session_id: int
    controller: str
    seed: int
    profile_id: str = "qualification-v1"
    scenario: str = "test-017"

    def validate(self) -> None:
        _require(isinstance(self.run_id, str) and _RUN_ID.fullmatch(self.run_id) is not None, "invalid run_id")
        _require("/" not in self.run_id and "\\" not in self.run_id, "run_id must not contain a path separator")
        _require(type(self.session_id) is int and self.session_id > 0, "session_id must be a positive integer")
        _require(self.controller in ("fixed", "mlp"), "controller must be fixed or mlp")
        _require(type(self.seed) is int and self.seed in (7, 19, 43), "seed must be 7, 19, or 43")
        _require(self.profile_id == "qualification-v1", "profile_id must be qualification-v1")
        _require(self.scenario == "test-017", "host candidate only supports TEST-017")


@dataclass(frozen=True)
class HostContractConfig:
    """Validated inputs shared by the virtual-clock state machine."""

    identity: HostIdentity
    profile_path: Path
    model_directory: Path
    profile: dict[str, Any]
    model_bytes: bytes
    weights: Any
    fault_manifest: dict[str, Any]

    @classmethod
    def from_paths(
        cls,
        *,
        identity: HostIdentity,
        profile_path: Path,
        model_path: Path,
    ) -> "HostContractConfig":
        identity.validate()
        profile_path = Path(profile_path).resolve()
        model_path = Path(model_path).resolve()
        _require(profile_path.is_file(), "qualification profile does not exist")
        profile = closed_loop_contract.load_qualification_profile(profile_path)
        validate_profile_contract(profile)
        _require(profile["profile_id"] == identity.profile_id, "profile identity mismatch")
        model_directory = model_path.parent
        model_bytes = _validate_model_artifacts(model_directory, profile)
        _require(model_path.name == "model.bin", "model path must be canonical model.bin")
        _require(model_bytes == model_path.read_bytes(), "model bytes changed during validation")
        weights = model.model_arrays(model_bytes) if identity.controller == "mlp" else None
        fault_manifest = closed_loop_contract.generate_fault_manifest(
            profile=profile, seed=identity.seed
        )
        return cls(
            identity=identity,
            profile_path=profile_path,
            model_directory=model_directory,
            profile=profile,
            model_bytes=model_bytes,
            weights=weights,
            fault_manifest=fault_manifest,
        )


@dataclass(frozen=True)
class WatchdogState:
    """A virtual-clock watchdog observation; no wall-clock is consulted."""

    now_ms: int
    last_valid_apply_ms: int | None
    elapsed_ms: int | None
    expired: bool
    duty_q16_16: int


def evaluate_watchdog(
    *, last_valid_apply_ms: int | None, now_ms: int, timeout_ms: int = CONTROL_VALIDITY_MS
) -> WatchdogState:
    """Apply the exact 499/500/501 ms fail-closed boundary."""

    _require(type(now_ms) is int and now_ms >= 0, "watchdog now_ms must be non-negative")
    _require(type(timeout_ms) is int and timeout_ms == CONTROL_VALIDITY_MS, "watchdog timeout must be 500 ms")
    if last_valid_apply_ms is None:
        return WatchdogState(now_ms, None, None, True, SAFE_DUTY_Q16_16)
    _require(type(last_valid_apply_ms) is int and last_valid_apply_ms >= 0, "invalid last valid apply time")
    _require(now_ms >= last_valid_apply_ms, "virtual clock moved backwards")
    elapsed_ms = now_ms - last_valid_apply_ms
    expired = elapsed_ms >= timeout_ms
    return WatchdogState(
        now_ms,
        last_valid_apply_ms,
        elapsed_ms,
        expired,
        SAFE_DUTY_Q16_16 if expired else -1,
    )


@dataclass(frozen=True)
class HostCycle:
    """One deterministic virtual period, with no Guest/IP claims."""

    sample_index: int
    request_id: int
    release_ms: int
    period_start_ms: int
    inference_start_ms: int | None
    inference_finish_ms: int | None
    control_apply_ms: int
    period_finish_ms: int
    input_temperature_mC: int
    measured_mC: int
    target_mC: int
    previous_duty_q16_16: int
    duty_q16_16: int
    model_version: int
    fault_drop_scheduled: bool

    @property
    def cycle_duration_ms(self) -> int:
        return self.period_finish_ms - self.release_ms

    @property
    def deadline_met(self) -> bool:
        return self.cycle_duration_ms <= DEADLINE_BUDGET_MS

    def as_dict(self, *, identity: HostIdentity) -> dict[str, Any]:
        return {
            "schema_version": HOST_CYCLE_SCHEMA,
            "run_id": identity.run_id,
            "session_id": identity.session_id,
            "scenario": identity.scenario,
            "controller": identity.controller,
            "seed": identity.seed,
            "execution_kind": "host_contract",
            "evidence_level": HOST_CONTRACT_EVIDENCE,
            "sample_index": self.sample_index,
            "request_id": self.request_id,
            "release_ms": self.release_ms,
            "period_start_ms": self.period_start_ms,
            "inference_start_ms": self.inference_start_ms,
            "inference_finish_ms": self.inference_finish_ms,
            "control_apply_ms": self.control_apply_ms,
            "period_finish_ms": self.period_finish_ms,
            "cycle_duration_ms": self.cycle_duration_ms,
            "deadline_budget_ms": DEADLINE_BUDGET_MS,
            "deadline_met": self.deadline_met,
            "input_temperature_mC": self.input_temperature_mC,
            "measured_mC": self.measured_mC,
            "target_mC": self.target_mC,
            "previous_duty_q16_16": self.previous_duty_q16_16,
            "duty_q16_16": self.duty_q16_16,
            "model_version": self.model_version,
            "health_flags": 0,
            "applied_request_id": self.request_id,
            "feedback_confirmed": True,
            "control_validity_ms": CONTROL_VALIDITY_MS,
            "fault_drop_scheduled": self.fault_drop_scheduled,
            "transport": "none",
            "timing_domain": "virtual_monotonic_ms",
        }


class HostClosedLoopStateMachine:
    """A poll/step state machine with no blocking operation in its hot path."""

    def __init__(self, config: HostContractConfig):
        self.config = config
        self.identity = config.identity
        self._temperature_mC = int(config.profile["plant"]["initial_temperature_mC"])
        self._previous_duty_q16_16 = SAFE_DUTY_Q16_16
        self._fixed_controller = closed_loop_contract.FixedPiController()
        self._next_sample_index = 0
        self._next_release_ms = 0
        self._last_valid_apply_ms: int | None = None
        self._cycles: list[HostCycle] = []
        self._phase = "SAFE"
        self._failure: str | None = None

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def next_release_ms(self) -> int:
        return self._next_release_ms

    @property
    def cycles(self) -> tuple[HostCycle, ...]:
        return tuple(self._cycles)

    @property
    def failure(self) -> str | None:
        return self._failure

    def watchdog(self, now_ms: int) -> WatchdogState:
        """Observe the watchdog and enter SAFE without waiting for a timer."""

        state = evaluate_watchdog(
            last_valid_apply_ms=self._last_valid_apply_ms,
            now_ms=now_ms,
        )
        if state.expired:
            self._phase = "SAFE"
        return state

    def step(self, release_ms: int) -> HostCycle:
        """Process exactly one 100 ms release; callers own scheduling."""

        if self._phase == "COMPLETE":
            raise HostContractError("host state machine already completed")
        if self._phase == "FAILED":
            raise HostContractError(self._failure or "host state machine failed")
        _require(type(release_ms) is int, "release_ms must be an integer")
        _require(
            release_ms == self._next_release_ms,
            f"non-blocking release mismatch: expected {self._next_release_ms}, got {release_ms}",
        )
        sample_index = self._next_sample_index
        _require(sample_index < TICKS, "host state machine has no remaining sample")

        watchdog = self.watchdog(release_ms)
        initial_safe = sample_index == 0 and watchdog.expired
        input_temperature_mC = self._temperature_mC
        target_mC = int(self.config.profile["plant"]["target_mC"])
        previous_duty_q16_16 = self._previous_duty_q16_16
        try:
            if self.identity.controller == "fixed":
                duty_q16_16 = self._fixed_controller.update(input_temperature_mC, target_mC)
                model_version = 0
            else:
                duty_q16_16 = model.infer_duty_q16_16(
                    self.config.weights,
                    input_temperature_mC,
                    target_mC,
                    previous_duty_q16_16,
                )
                model_version = int(self.config.profile["model"]["model_version"])
        except (TypeError, ValueError, OverflowError) as error:
            self._phase = "FAILED"
            self._failure = f"controller failed closed at sample {sample_index}: {error}"
            raise HostContractError(self._failure) from error

        _require(0 <= int(duty_q16_16) <= 65_536, "controller duty is outside Q16.16 range")
        request_id = sample_index + 1
        _require(request_id <= TICKS, "request_id exceeded 1800")
        _require(model_version == (0 if self.identity.controller == "fixed" else closed_loop_contract.MODEL_VERSION), "model identity drift")

        # The virtual transport has no sleep or socket wait.  These offsets are
        # deterministic budget points, not Guest timestamps or runtime evidence.
        period_start_ms = release_ms + 1
        inference_start_ms = release_ms + 2 if self.identity.controller == "mlp" else None
        inference_finish_ms = release_ms + 3 if self.identity.controller == "mlp" else None
        control_apply_ms = release_ms + 5
        period_finish_ms = release_ms + 6
        _require(period_finish_ms - release_ms <= DEADLINE_BUDGET_MS, "host cycle missed 100 ms deadline")

        measured_mC = closed_loop_contract.plant_step(
            input_temperature_mC,
            int(duty_q16_16),
            sample_index,
        )
        cycle = HostCycle(
            sample_index=sample_index,
            request_id=request_id,
            release_ms=release_ms,
            period_start_ms=period_start_ms,
            inference_start_ms=inference_start_ms,
            inference_finish_ms=inference_finish_ms,
            control_apply_ms=control_apply_ms,
            period_finish_ms=period_finish_ms,
            input_temperature_mC=input_temperature_mC,
            measured_mC=measured_mC,
            target_mC=target_mC,
            previous_duty_q16_16=previous_duty_q16_16,
            duty_q16_16=int(duty_q16_16),
            model_version=model_version,
            fault_drop_scheduled=sample_index in set(
                self.config.fault_manifest["drop_first_control_request_indices"]
            ),
        )
        _require(not initial_safe or cycle.request_id == 1, "initial SAFE identity drift")
        self._last_valid_apply_ms = control_apply_ms
        self._temperature_mC = measured_mC
        self._previous_duty_q16_16 = int(duty_q16_16)
        self._cycles.append(cycle)
        self._next_sample_index += 1
        self._next_release_ms += TICK_PERIOD_MS
        self._phase = "COMPLETE" if self._next_sample_index == TICKS else "RUNNING"
        return cycle

    def run(self) -> "HostRun":
        """Drive all 1,800 releases synchronously in virtual time."""

        while self._next_sample_index < TICKS:
            self.step(self._next_release_ms)
        result = HostRun(config=self.config, cycles=tuple(self._cycles))
        result.validate()
        return result


@dataclass(frozen=True)
class HostRun:
    """Completed host-only result; it intentionally carries no Guest events."""

    config: HostContractConfig
    cycles: tuple[HostCycle, ...]

    @property
    def identity(self) -> HostIdentity:
        return self.config.identity

    def _validator_rows(self) -> list[dict[str, int | str]]:
        return [
            {
                "controller": self.identity.controller,
                "seed": self.identity.seed,
                "sample_index": cycle.sample_index,
                "measured_mC": cycle.measured_mC,
                "target_mC": cycle.target_mC,
                "duty_q16_16": cycle.duty_q16_16,
                "model_version": cycle.model_version,
                "health_flags": 0,
                "applied_request_id": cycle.request_id,
                "feedback_confirmed": 1,
                # These are only validator placeholders for the already
                # separate host trajectory oracle; no runtime latency is
                # emitted by this candidate.
                "closed_loop_rtt_ns": 0,
                "action_latency_ns": 0,
            }
            for cycle in self.cycles
        ]

    def validate(self) -> None:
        """Recompute identity, cycle, model/plant and sequence invariants."""

        self.identity.validate()
        validate_profile_contract(self.config.profile)
        _require(len(self.cycles) == TICKS, "host run must contain exactly 1800 cycles")
        _require(
            [cycle.sample_index for cycle in self.cycles] == list(range(TICKS)),
            "sample_index is not continuous 0..1799",
        )
        _require(
            [cycle.request_id for cycle in self.cycles] == list(range(1, TICKS + 1)),
            "request_id is not continuous 1..1800",
        )
        _require(
            [cycle.release_ms for cycle in self.cycles]
            == [index * TICK_PERIOD_MS for index in range(TICKS)],
            "virtual release schedule is not 100 ms periodic",
        )
        _require(all(cycle.deadline_met for cycle in self.cycles), "a host cycle missed the 100 ms deadline")
        if self.identity.controller == "fixed":
            _require(
                all(
                    cycle.inference_start_ms is None
                    and cycle.inference_finish_ms is None
                    for cycle in self.cycles
                ),
                "FIXED host run must not emit inference timing",
            )
        else:
            _require(
                all(
                    cycle.inference_start_ms is not None
                    and cycle.inference_finish_ms is not None
                    and cycle.inference_start_ms < cycle.inference_finish_ms
                    for cycle in self.cycles
                ),
                "MLP host run is missing inference timing",
            )
        _require(
            self.cycles[-1].release_ms + TICK_PERIOD_MS
            == RUN_DURATION_SECONDS * 1_000,
            "virtual run duration is not 180 seconds",
        )
        expected_fault_manifest = closed_loop_contract.generate_fault_manifest(
            profile=self.config.profile, seed=self.identity.seed
        )
        _require(self.config.fault_manifest == expected_fault_manifest, "fault manifest identity drift")
        expected_drop = set(expected_fault_manifest["drop_first_control_request_indices"])
        _require(
            {cycle.sample_index for cycle in self.cycles if cycle.fault_drop_scheduled}
            == expected_drop,
            "fault schedule is not bound to the profile seed",
        )
        mlp_infer = None
        if self.identity.controller == "mlp":
            mlp_infer = lambda measured, target, previous: model.infer_duty_q16_16(
                self.config.weights, measured, target, previous
            )
        closed_loop_contract.validate_trajectory(
            self._validator_rows(),
            profile=self.config.profile,
            controller=self.identity.controller,
            seed=self.identity.seed,
            mlp_infer=mlp_infer,
        )

    def summary(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": HOST_SUMMARY_SCHEMA,
            "run_id": self.identity.run_id,
            "session_id": self.identity.session_id,
            "scenario": self.identity.scenario,
            "controller": self.identity.controller,
            "seed": self.identity.seed,
            "profile_id": self.identity.profile_id,
            "execution_kind": "host_contract",
            "evidence_level": HOST_CONTRACT_EVIDENCE,
            "ticks": TICKS,
            "duration_seconds": RUN_DURATION_SECONDS,
            "tick_period_ms": TICK_PERIOD_MS,
            "request_id_range": [1, TICKS],
            "sample_index_range": [0, TICKS - 1],
            "deadline_budget_ms": DEADLINE_BUDGET_MS,
            "deadline_misses": 0,
            "watchdog_timeout_ms": CONTROL_VALIDITY_MS,
            "watchdog_safe_duty_q16_16": SAFE_DUTY_Q16_16,
            "fault_manifest_sha256": _sha256(_json_bytes(self.config.fault_manifest)),
            "transport": "none",
            "timing_domain": "virtual_monotonic_ms",
            "runtime_evidence": "not_produced",
            "status": HOST_CONTRACT_STATUS,
        }

    def cycle_jsonl(self) -> bytes:
        return b"".join(
            _json_bytes(cycle.as_dict(identity=self.identity)) for cycle in self.cycles
        )

    def to_bundle(
        self,
        *,
        command: Sequence[str] = ("host_orchestration.py", "deterministic-dry-run"),
        cwd: Path | None = None,
        extra_files: Mapping[str, bytes] | None = None,
    ) -> "HostBundle":
        """Build a status-last host contract bundle without writing it."""

        self.validate()
        cwd_text = str(Path.cwd() if cwd is None else Path(cwd).resolve())
        payloads: dict[str, bytes] = {
            "commands.jsonl": _json_bytes(
                {
                    "argv": [str(item) for item in command],
                    "cwd": cwd_text,
                    "started_monotonic_ns": 0,
                    "finished_monotonic_ns": 0,
                    "exit_code": 0,
                }
            ),
            "cleanup.json": _json_bytes(
                {
                    "residualProcesses": [],
                    "residualSockets": [],
                    "residualFiles": [],
                }
            ),
            "configs/qualification-v1.json": self.config.profile_path.read_bytes(),
            "configs/fault-manifest.json": _json_bytes(self.config.fault_manifest),
            "metrics/host-cycles.jsonl": self.cycle_jsonl(),
            "metrics/summary.json": _json_bytes(self.summary()),
        }
        for name in MODEL_ARTIFACTS:
            payloads[f"model/{name}"] = (self.config.model_directory / name).read_bytes()
        if extra_files:
            for relative, content in extra_files.items():
                relative_path = Path(relative)
                _require(
                    not relative_path.is_absolute() and ".." not in relative_path.parts,
                    "extra bundle path escapes output directory",
                )
                _require(relative not in payloads, f"extra bundle path collides: {relative}")
                payloads[relative] = bytes(content)

        manifest_files = {
            relative: {
                "size": len(content),
                "sha256": _sha256(content),
                "producer": "scripts/contest/ai/host_orchestration.py",
            }
            for relative, content in sorted(payloads.items())
        }
        manifest_bytes = _json_bytes(
            {
                "schema_version": HOST_MANIFEST_SCHEMA,
                "run_id": self.identity.run_id,
                "files": manifest_files,
            }
        )
        manifest_sha256 = _sha256(manifest_bytes)
        session_bytes = _json_bytes(
            {
                "schema_version": HOST_SESSION_SCHEMA,
                "run_id": self.identity.run_id,
                "session_id": self.identity.session_id,
                "scenario": self.identity.scenario,
                "controller": self.identity.controller,
                "seed": self.identity.seed,
                "profile_id": self.identity.profile_id,
                "execution_kind": "host_contract",
                "evidence_level": HOST_CONTRACT_EVIDENCE,
                "manifest_sha256": manifest_sha256,
            }
        )
        status_bytes = _json_bytes(
            {
                "schema_version": HOST_STATUS_SCHEMA,
                "success": True,
                "status": HOST_CONTRACT_STATUS,
                "primaryError": None,
                "cleanupError": None,
                "completedChecks": ["oracle", "hashes", "validator", "cleanup"],
                "manifestSha256": manifest_sha256,
                "statusLast": True,
            }
        )
        ordered_files = {
            **payloads,
            "manifest.json": manifest_bytes,
            "session.json": session_bytes,
            "status.json": status_bytes,
        }
        _require(next(reversed(ordered_files)) == "status.json", "status.json must be written last")
        return HostBundle(files=ordered_files, manifest_sha256=manifest_sha256)


@dataclass(frozen=True)
class HostBundle:
    """In-memory status-last bundle with a non-overwriting writer."""

    files: Mapping[str, bytes]
    manifest_sha256: str

    def write(self, output_dir: Path) -> None:
        output_dir = Path(output_dir).resolve()
        _require(not output_dir.exists(), "output directory already exists; use a fresh run directory")
        output_dir.mkdir(parents=True)
        for relative, content in self.files.items():
            relative_path = Path(relative)
            _require(not relative_path.is_absolute() and ".." not in relative_path.parts, "bundle path escapes output directory")
            target = output_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            publish_new_file(target, content, error_type=HostContractError)


def run_host_contract(config: HostContractConfig) -> HostRun:
    """Run the complete 1,800-cycle host contract in virtual time."""

    state_machine = HostClosedLoopStateMachine(config)
    return state_machine.run()
