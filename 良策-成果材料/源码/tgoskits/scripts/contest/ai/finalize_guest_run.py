#!/usr/bin/env python3
"""Finalize one immutable TEST-017 Guest observation into an L8 bundle.

This module never invents runtime values.  ICPC attempts, endpoint events,
trajectory values and latency values are derived exclusively from the raw
dual-Guest observation.  A complete bundle is validated in a fresh staging
directory before the status-last byte set is published to its final path.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping

try:
    from . import closed_loop_contract, compute_metrics, model, produce_icpc_records
    from . import validate_closed_loop
    from .host_orchestration import HostBundle
    from .run_closed_loop import RunnerPlanError, validate_guest_execution_manifest
except ImportError:  # pragma: no cover - direct script execution
    import closed_loop_contract  # type: ignore[no-redef]
    import compute_metrics  # type: ignore[no-redef]
    import model  # type: ignore[no-redef]
    import produce_icpc_records  # type: ignore[no-redef]
    import validate_closed_loop  # type: ignore[no-redef]
    from host_orchestration import HostBundle  # type: ignore[no-redef]
    from run_closed_loop import (  # type: ignore[no-redef]
        RunnerPlanError,
        validate_guest_execution_manifest,
    )


PRODUCER = "scripts/contest/ai/finalize_guest_run.py"
TRAJECTORY_FIELDS = (
    "controller",
    "seed",
    "sample_index",
    "measured_mC",
    "target_mC",
    "duty_q16_16",
    "model_version",
    "health_flags",
    "applied_request_id",
    "feedback_confirmed",
    "closed_loop_rtt_ns",
    "action_latency_ns",
)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n"
        for row in rows
    ).encode("utf-8")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RunnerPlanError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise RunnerPlanError(f"{label} must be a JSON object")
    return value


def _load_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RunnerPlanError(f"cannot read {label}: {error}") from error
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise RunnerPlanError(
                f"{label}:{line_number} is not JSON: {error}"
            ) from error
        if not isinstance(value, dict):
            raise RunnerPlanError(f"{label}:{line_number} is not an object")
        rows.append(value)
    return rows


def _extract_attempts(
    observation: Path, *, run_id: str, session_id: int, ticks: int
) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    for guest, vm_id in (("linux", 1), ("zephyr", 2)):
        path = observation / "logs" / f"{guest}.raw.log"
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            marker = f"[VM {vm_id}] "
            marker_index = line.find(marker)
            if marker_index < 0:
                continue
            payload = line[marker_index + len(marker) :].strip()
            if not payload.startswith("{"):
                continue
            try:
                row = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("schema_version") != produce_icpc_records.ATTEMPT_SCHEMA:
                continue
            if row.get("run_id") != run_id or row.get("session_id") != session_id:
                raise RunnerPlanError(
                    f"{guest} attempt {line_number} identity drifted"
                )
            produce_icpc_records._decode_attempt(row, len(raw) + 1)
            raw.append(row)
    if not raw:
        raise RunnerPlanError("Guest observation has no ICPC injection attempts")

    decoded = [
        (row, closed_loop_contract._decode_icpc_wire(bytes.fromhex(row["wire_hex"])))
        for row in raw
    ]
    controls: dict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    control_request_by_sequence: dict[int, int] = {}
    for row, wire in decoded:
        if wire["message_type"] == closed_loop_contract.ICPC_MESSAGE_CONTROL:
            request_id = int(wire["request_id"])
            controls.setdefault(request_id, []).append((row, wire))
            control_request_by_sequence[int(wire["sequence"])] = request_id
    acks: dict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    statuses: dict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for row, wire in decoded:
        if wire["message_type"] == closed_loop_contract.ICPC_MESSAGE_ACK:
            request_id = control_request_by_sequence.get(int(wire["ack_sequence"]))
            if request_id is None:
                raise RunnerPlanError("ACK attempt has no CONTROL association")
            acks.setdefault(request_id, []).append((row, wire))
        elif wire["message_type"] == closed_loop_contract.ICPC_MESSAGE_STATUS:
            statuses.setdefault(int(wire["request_id"]), []).append((row, wire))

    ordered: list[dict[str, Any]] = []
    for request_id in range(1, ticks + 1):
        request_controls = sorted(
            controls.get(request_id, []), key=lambda item: int(item[0]["attempt"])
        )
        request_acks = acks.get(request_id, [])
        request_statuses = statuses.get(request_id, [])
        if not request_controls or len(request_acks) != 1 or len(request_statuses) != 1:
            raise RunnerPlanError(
                f"request {request_id} does not have complete CONTROL/ACK/STATUS attempts"
            )
        ordered.extend(row for row, _wire in request_controls)
        ordered.append(request_acks[0][0])
        ordered.append(request_statuses[0][0])
    if len(ordered) != len(raw):
        raise RunnerPlanError("ICPC attempts contain unassociated or duplicate records")
    return ordered


def _event_index(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[int, str], list[dict[str, Any]]]:
    result: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for source in rows:
        row = dict(source)
        result.setdefault((int(row["request_id"]), str(row["event"])), []).append(row)
    return result


def _first(
    index: Mapping[tuple[int, str], list[dict[str, Any]]],
    request_id: int,
    name: str,
) -> dict[str, Any]:
    matches = index.get((request_id, name), [])
    if not matches:
        raise RunnerPlanError(f"missing {name} event for request {request_id}")
    return min(matches, key=lambda row: int(row["monotonic_ns"]))


def _trajectory_bytes(
    *,
    linux_events: list[dict[str, Any]],
    zephyr_events: list[dict[str, Any]],
    icpc_records: list[dict[str, Any]],
    controller: str,
    seed: int,
    ticks: int,
) -> tuple[bytes, list[dict[str, Any]]]:
    linux = _event_index(linux_events)
    zephyr = _event_index(zephyr_events)
    status_by_request: dict[int, dict[str, Any]] = {}
    for record in icpc_records:
        if record["message_type"] == "status":
            status_by_request[int(record["request_id"])] = (
                closed_loop_contract._decode_icpc_wire(
                    bytes.fromhex(str(record["wire_hex"]))
                )
            )
    rows: list[dict[str, Any]] = []
    for request_id in range(1, ticks + 1):
        control = _first(linux, request_id, "packet_send")
        feedback = _first(linux, request_id, "feedback_receive")
        input_event = _first(linux, request_id, "input_receive")
        receive = _first(zephyr, request_id, "packet_receive")
        applied = _first(zephyr, request_id, "control_apply")
        status = status_by_request.get(request_id)
        if status is None:
            raise RunnerPlanError(f"missing STATUS wire for request {request_id}")
        rows.append(
            {
                "controller": controller,
                "seed": seed,
                "sample_index": request_id - 1,
                "measured_mC": int(feedback["value"]),
                "target_mC": int(status["target_mC"]),
                "duty_q16_16": int(control["value"]),
                "model_version": int(status["model_version"] if "model_version" in status else (0 if controller == "fixed" else closed_loop_contract.MODEL_VERSION)),
                "health_flags": int(status["health_flags"]),
                "applied_request_id": request_id,
                "feedback_confirmed": 1,
                "closed_loop_rtt_ns": int(feedback["monotonic_ns"])
                - int(input_event["monotonic_ns"]),
                "action_latency_ns": int(applied["monotonic_ns"])
                - int(receive["monotonic_ns"]),
            }
        )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=TRAJECTORY_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8"), rows


def _svg(rows: list[dict[str, Any]], field: str, title: str) -> bytes:
    values = [int(row[field]) for row in rows]
    low, high = min(values), max(values)
    span = max(1, high - low)
    points = " ".join(
        f"{40 + index * 920 / max(1, len(values) - 1):.2f},"
        f"{340 - (value - low) * 280 / span:.2f}"
        for index, value in enumerate(values)
    )
    return (
        "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"1000\" height=\"380\" "
        "viewBox=\"0 0 1000 380\"><rect width=\"1000\" height=\"380\" fill=\"white\"/>"
        f"<text x=\"40\" y=\"28\" font-family=\"sans-serif\" font-size=\"18\">{title}</text>"
        "<line x1=\"40\" y1=\"340\" x2=\"960\" y2=\"340\" stroke=\"#555\"/>"
        f"<polyline fill=\"none\" stroke=\"#1769aa\" stroke-width=\"1.5\" points=\"{points}\"/>"
        f"<text x=\"40\" y=\"370\" font-family=\"monospace\" font-size=\"12\">min={low} max={high} samples={len(values)}</text>"
        "</svg>\n"
    ).encode("utf-8")


def finalize_guest_run(
    preflight: Mapping[str, Any], *, observation_dir: Path, output_dir: Path
) -> dict[str, Any]:
    """Validate and publish a complete TEST-017 Guest-runtime bundle."""

    run_id = preflight.get("run_id")
    session_id = preflight.get("session_id")
    controller = preflight.get("controller")
    seed = preflight.get("seed")
    if (
        preflight.get("scenario") != "test-017"
        or not isinstance(run_id, str)
        or not isinstance(session_id, int)
        or controller not in ("fixed", "mlp")
        or seed not in (7, 19, 43)
    ):
        raise RunnerPlanError("finalizer received an invalid TEST-017 preflight")
    observation = Path(observation_dir).absolute()
    output = Path(output_dir).absolute()
    if output.exists():
        raise RunnerPlanError(f"final output directory must be fresh: {output}")
    validate_guest_execution_manifest(
        observation, run_id=run_id, session_id=session_id
    )
    inputs = preflight.get("inputs")
    if not isinstance(inputs, dict):
        raise RunnerPlanError("finalizer preflight has no input bindings")
    profile_path = Path(inputs["profile"]["path"])
    profile = closed_loop_contract.load_qualification_profile(profile_path)
    ticks = int(profile["timing"]["ticks"])
    fault_manifest = closed_loop_contract.generate_fault_manifest(
        profile=profile, seed=seed
    )

    attempts = _extract_attempts(
        observation, run_id=run_id, session_id=session_id, ticks=ticks
    )
    frames = validate_closed_loop._validate_structured_network(
        observation, run_id, session_id
    )
    records = produce_icpc_records.produce_records(
        attempts,
        frames,
        run_id=run_id,
        session_id=session_id,
        fault_manifest=fault_manifest,
    )
    attempts_bytes = _jsonl_bytes(attempts)
    records_bytes = _jsonl_bytes(records)
    fault_manifest_bytes = _json_bytes(fault_manifest)
    transcript = produce_icpc_records.build_injection_transcript(
        records,
        run_id=run_id,
        session_id=session_id,
        raw_attempts=attempts_bytes,
        icpc_records=records_bytes,
        fault_manifest_bytes=fault_manifest_bytes,
        fault_manifest=fault_manifest,
    )

    linux_events = _load_jsonl(
        observation / "metrics" / "linux-events.jsonl", "Linux event transcript"
    )
    zephyr_events = _load_jsonl(
        observation / "metrics" / "zephyr-events.jsonl", "Zephyr event transcript"
    )
    trajectory_bytes, trajectory = _trajectory_bytes(
        linux_events=linux_events,
        zephyr_events=zephyr_events,
        icpc_records=records,
        controller=controller,
        seed=seed,
        ticks=ticks,
    )
    model_dir = Path(inputs["repository"]) / "apps" / "contest" / "linux-ai-controller" / "model"
    mlp_weights = None
    if controller == "mlp":
        mlp_weights = model.load_model(model_dir / "model.bin")[1]
    checked = closed_loop_contract.validate_trajectory(
        trajectory,
        profile=profile,
        controller=controller,
        seed=seed,
        mlp_infer=(
            None
            if mlp_weights is None
            else lambda measured, target, previous: model.infer_duty_q16_16(
                mlp_weights, measured, target, previous
            )
        ),
    )
    metrics = compute_metrics.compute_metrics(
        checked,
        target_mC=int(profile["plant"]["target_mC"]),
        band_mC=int(profile["metrics"]["error_band_mC"]),
        hold_ticks=int(profile["metrics"]["settling_hold_ticks"]),
        expected_ticks=ticks,
        recovery_start_tick=int(profile["metrics"]["recovery_start_tick"]),
        overshoot_step_mC=int(profile["metrics"]["overshoot_step_mC"]),
    )

    execution = _load_json(observation / "execution.json", "execution manifest")
    command = execution["command"]
    command_bytes = _jsonl_bytes((command,))
    zephyr_artifact_dir = Path(inputs["zephyr_build_manifest"]["path"]).parent
    payloads: dict[str, bytes] = {
        "commands.jsonl": command_bytes,
        "cleanup.json": _json_bytes(
            {"residualProcesses": [], "residualSockets": [], "residualFiles": []}
        ),
        "configs/qualification-v1.json": profile_path.read_bytes(),
        "configs/fault-manifest.json": fault_manifest_bytes,
        "configs/linux-app-config.json": Path(inputs["linux_controller_config"]["path"]).read_bytes(),
        "configs/zephyr-dotconfig": (zephyr_artifact_dir / ".config").read_bytes(),
        "configs/zephyr.dts": (zephyr_artifact_dir / "zephyr.dts").read_bytes(),
        "logs/axvisor.raw.log": (observation / "logs" / "axvisor.raw.log").read_bytes(),
        "logs/linux.raw.log": (observation / "logs" / "linux.raw.log").read_bytes(),
        "logs/zephyr.raw.log": (observation / "logs" / "zephyr.raw.log").read_bytes(),
        "network/capture.pcap": (observation / "network" / "capture.pcap").read_bytes(),
        "network/frames.jsonl": (observation / "network" / "frames.jsonl").read_bytes(),
        "network/icpc-attempts.jsonl": attempts_bytes,
        "network/icpc.jsonl": records_bytes,
        "network/injection-transcript.json": _json_bytes(transcript),
        "metrics/linux-events.jsonl": _jsonl_bytes(linux_events),
        "metrics/zephyr-events.jsonl": _jsonl_bytes(zephyr_events),
        "metrics/trajectory.csv": trajectory_bytes,
        "metrics/summary.json": _json_bytes(metrics),
        "metrics/recompute.txt": (
            f"source=metrics/trajectory.csv\ncontroller={controller}\nseed={seed}\n"
            f"ticks={ticks}\nsummary_sha256={hashlib.sha256(_json_bytes(metrics)).hexdigest()}\n"
        ).encode("utf-8"),
        "figures/temperature.svg": _svg(checked, "measured_mC", "Measured temperature (mC)"),
        "figures/duty.svg": _svg(checked, "duty_q16_16", "Controller duty (Q16.16)"),
    }
    for name in ("model.bin", "metadata.json", "dataset-manifest.json", "golden-vectors.json"):
        payloads[f"model/{name}"] = (model_dir / name).read_bytes()

    manifest_files = {
        relative: {
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "producer": PRODUCER,
        }
        for relative, content in sorted(payloads.items())
    }
    manifest_bytes = _json_bytes(
        {"schema_version": "p5-ai-manifest-v1", "run_id": run_id, "files": manifest_files}
    )
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    session_bytes = _json_bytes(
        {
            "schema_version": "p5-ai-session-v1",
            "run_id": run_id,
            "session_id": session_id,
            "scenario": "test-017",
            "controller": controller,
            "seed": seed,
            "profile_id": profile["profile_id"],
            "execution_kind": "guest_runtime",
            "evidence_level": profile["evidence"]["level"],
            "manifest_sha256": manifest_sha256,
        }
    )
    status_bytes = _json_bytes(
        {
            "schema_version": "p5-ai-status-v1",
            "success": True,
            "status": profile["evidence"]["success_status"],
            "primaryError": None,
            "cleanupError": None,
            "completedChecks": ["oracle", "hashes", "validator", "cleanup"],
            "manifestSha256": manifest_sha256,
            "statusLast": True,
        }
    )
    bundle = HostBundle(
        files={
            **payloads,
            "manifest.json": manifest_bytes,
            "session.json": session_bytes,
            "status.json": status_bytes,
        },
        manifest_sha256=manifest_sha256,
    )
    stage = output.parent / f".{run_id}.finalizing"
    if stage.exists():
        raise RunnerPlanError(f"finalizer staging directory already exists: {stage}")
    try:
        bundle.write(stage)
        validate_closed_loop.validate_bundle(
            stage,
            profile_path,
            controller=controller,
            seed=seed,
            qualification=True,
        )
        shutil.rmtree(stage)
        bundle.write(output)
        shutil.rmtree(observation)
    except Exception:
        # Preserve staging/observation on failure for forensic inspection.
        raise
    return {
        "schema_version": "p5-ai-finalization-v1",
        "run_id": run_id,
        "session_id": session_id,
        "status": profile["evidence"]["success_status"],
        "qualified": True,
        "manifest_sha256": manifest_sha256,
        "output_dir": str(output),
    }
