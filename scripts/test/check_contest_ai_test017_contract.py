#!/usr/bin/env python3
"""Fail-closed host contract for the frozen TEST-017 qualification profile."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "scripts" / "contest" / "ai"
PROFILE = ROOT / "configs" / "contest" / "ai" / "qualification-v1.json"
sys.path.insert(0, str(AI_DIR))

import closed_loop_contract  # noqa: E402
import compute_metrics  # noqa: E402
import model  # noqa: E402


def trajectory_rows(*, controller: str = "fixed", seed: int = 7, ticks: int = 1800):
    temperature_mC = closed_loop_contract.load_qualification_profile(PROFILE)["plant"][
        "initial_temperature_mC"
    ]
    rows = []
    fixed_controller = closed_loop_contract.FixedPiController()
    mlp_weights = (
        model.load_model(
            ROOT / "apps" / "contest" / "linux-ai-controller" / "model" / "model.bin"
        )[1]
        if controller == "mlp"
        else None
    )
    previous_duty_q16_16 = 0
    for index in range(ticks):
        duty_q16_16 = (
            fixed_controller.update(temperature_mC, 55_000)
            if controller == "fixed"
            else model.infer_duty_q16_16(
                mlp_weights, temperature_mC, 55_000, previous_duty_q16_16
            )
        )
        temperature_mC = closed_loop_contract.plant_step(
            temperature_mC, duty_q16_16, index
        )
        rows.append(
            {
            "controller": controller,
            "seed": str(seed),
            "sample_index": str(index),
            "measured_mC": str(temperature_mC),
            "target_mC": "55000",
            "duty_q16_16": str(duty_q16_16),
            "model_version": str(
                0 if controller == "fixed" else closed_loop_contract.MODEL_VERSION
            ),
            "health_flags": "0",
            "applied_request_id": str(index + 1),
            "feedback_confirmed": "1",
            "closed_loop_rtt_ns": "200000",
            "action_latency_ns": "100000",
            }
        )
        previous_duty_q16_16 = duty_q16_16
    return rows


def must_reject(profile, rows, controller: str, seed: int, contains: str) -> None:
    try:
        closed_loop_contract.validate_trajectory(
            rows, profile=profile, controller=controller, seed=seed
        )
    except ValueError as error:
        assert contains in str(error), str(error)
    else:
        raise AssertionError(f"TEST-017 accepted invalid trajectory: {contains}")


def event(
    endpoint: str,
    name: str,
    request_id: int,
    sample_index: int,
    *,
    value=None,
    unit=None,
    outcome: str = "ok",
):
    offsets = {
        "linux": {
            "period_release": 0,
            "period_start": 10_000,
            "input_receive": 20_000,
            "inference_start": 30_000,
            "inference_finish": 40_000,
            "packet_send": 50_000,
            "ack_receive": 150_000,
            "feedback_receive": 220_000,
            "period_finish": 230_000,
        },
        "zephyr": {
            "period_release": 0,
            "period_start": 10_000,
            "packet_receive": 20_000,
            "ack_send": 30_000,
            "control_apply": 120_000,
            "packet_send": 130_000,
            "period_finish": 140_000,
            "safe_enter": 0,
            "safe_exit": 0,
        },
    }
    sequences = {
        ("linux", "input_receive"): request_id * 10 + 1,
        ("linux", "packet_send"): request_id * 10 + 2,
        ("linux", "ack_receive"): request_id * 10 + 3,
        ("linux", "feedback_receive"): request_id * 10 + 4,
        ("zephyr", "packet_receive"): request_id * 10 + 2,
        ("zephyr", "ack_send"): request_id * 10 + 3,
        ("zephyr", "control_apply"): request_id * 10 + 2,
        ("zephyr", "packet_send"): request_id * 10 + 4,
    }
    return {
        "schema_version": "p5-ai-event-v1",
        "run_id": "phase5-ai-fixed-s7-contract",
        "endpoint": endpoint,
        "scenario": "test-017",
        "transport": "udp",
        "session_id": 17,
        "sequence": sequences.get((endpoint, name)),
        "request_id": request_id,
        "sample_index": sample_index,
        "event": name,
        "monotonic_ns": sample_index * 100_000_000 + 10 + offsets[endpoint][name],
        "value": value,
        "unit": unit,
        "outcome": outcome,
    }


def safe_event(name: str, monotonic_ns: int):
    value = event("zephyr", name, 1, 0)
    value.update(
        session_id=17,
        sequence=None,
        request_id=None,
        sample_index=None,
        monotonic_ns=monotonic_ns,
    )
    return value


def request_event(endpoint: str, name: str, row, input_mC: int):
    request_id = int(row["applied_request_id"])
    sample_index = int(row["sample_index"])
    measured = int(row["measured_mC"])
    duty = int(row["duty_q16_16"])
    semantic = {
        ("linux", "input_receive"): (input_mC, "mC", "status"),
        ("linux", "packet_send"): (duty, "q16_16", "sent"),
        ("linux", "ack_receive"): (request_id * 10 + 2, "sequence", "ack"),
        ("linux", "feedback_receive"): (measured, "mC", "status_applied"),
        ("linux", "inference_finish"): (duty, "q16_16", "ok"),
        ("zephyr", "packet_receive"): (duty, "q16_16", "control_valid"),
        ("zephyr", "ack_send"): (request_id * 10 + 2, "sequence", "ack"),
        ("zephyr", "control_apply"): (duty, "q16_16", "applied"),
        ("zephyr", "packet_send"): (measured, "mC", "status"),
    }
    value, unit, outcome = semantic.get((endpoint, name), (None, None, "ok"))
    return event(
        endpoint,
        name,
        request_id,
        sample_index,
        value=value,
        unit=unit,
        outcome=outcome,
    )


def main() -> int:
    profile = closed_loop_contract.load_qualification_profile(PROFILE)
    assert profile["schema_version"] == "p5-ai-qualification-v1"
    assert profile["controllers"] == ["fixed", "mlp"]
    assert profile["seeds"] == [7, 19, 43]
    assert profile["timing"]["tick_period_ms"] == 100
    assert profile["timing"]["ticks"] == 1800
    assert profile["plant"]["disturbance_start_tick"] == 600
    assert profile["plant"]["disturbance_end_tick"] == 899
    assert profile["fault_stream"]["algorithm"] == "pcg-xsh-rr-64-32"
    assert profile["fault_stream"]["first_control_drop_modulus"] == 100
    assert profile["fault_stream"]["first_control_drop_residue"] == 0
    manifest7 = closed_loop_contract.generate_fault_manifest(profile=profile, seed=7)
    manifest19 = closed_loop_contract.generate_fault_manifest(profile=profile, seed=19)
    manifest43 = closed_loop_contract.generate_fault_manifest(profile=profile, seed=43)
    assert manifest7["drop_first_control_request_indices"][:3] == [128, 307, 414]
    assert manifest19["drop_first_control_request_indices"][:3] == [3, 10, 408]
    assert manifest7["drop_first_control_count"] == 21
    assert manifest19["drop_first_control_count"] == 22
    assert manifest43["drop_first_control_count"] == 15
    assert manifest7["outputs_sha256"] == "e261bdf362a9eb06ba87b64fe833d30d1f539326c7e96defb7e2527e3cdf704b"
    assert manifest19["outputs_sha256"] == "094027e39c5c18e7b0673ecf39381ebaec410e8cfc2d6b8e7d0d86734434c343"
    assert manifest43["outputs_sha256"] == "20ebf1e11a411b96bfc5ec076656b14ae96048535635317d645ffb95ac53a3b2"
    assert manifest7["drop_first_control_request_indices"] == profile["fault_golden"]["7"]["drop_first_control_request_indices"]
    assert manifest19["drop_first_control_request_indices"] == profile["fault_golden"]["19"]["drop_first_control_request_indices"]
    assert manifest43["drop_first_control_request_indices"] == profile["fault_golden"]["43"]["drop_first_control_request_indices"]
    assert manifest7 != manifest19
    metadata = json.loads(
        (ROOT / "apps" / "contest" / "linux-ai-controller" / "model" / "metadata.json")
        .read_text(encoding="utf-8")
    )
    model_bytes = (
        ROOT / "apps" / "contest" / "linux-ai-controller" / "model" / "model.bin"
    ).read_bytes()
    assert profile["model"]["model_version"] == metadata["model_version"]
    assert profile["model"]["model_sha256"] == metadata["model_sha256"]
    assert profile["model"]["model_sha256"] == hashlib.sha256(model_bytes).hexdigest()
    model_directory = ROOT / "apps" / "contest" / "linux-ai-controller" / "model"
    dataset_manifest = json.loads(
        (model_directory / "dataset-manifest.json").read_text(encoding="utf-8")
    )
    assert dataset_manifest["schema_version"] == "p5-dataset-v1"
    assert dataset_manifest["episodes_per_split"] == 64
    assert dataset_manifest["ticks_per_episode"] == 1800
    for split in dataset_manifest["splits"]:
        assert split["sample_count"] == 115_200
        assert split["sha256"] == metadata["dataset_sha256"][split["name"]]
    checksums = dict(
        reversed(line.split("  ", 1))
        for line in (model_directory / "checksums.sha256")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    )
    for name in ("model.bin", "metadata.json", "golden-vectors.json", "dataset-manifest.json"):
        assert checksums[name] == hashlib.sha256((model_directory / name).read_bytes()).hexdigest()

    valid = trajectory_rows()
    checked = closed_loop_contract.validate_trajectory(
        valid, profile=profile, controller="fixed", seed=7
    )
    assert len(checked) == 1800
    metrics = compute_metrics.compute_metrics(
        checked,
        target_mC=55000,
        band_mC=1000,
        hold_ticks=50,
        expected_ticks=1800,
        recovery_start_tick=900,
    )["groups"][0]
    assert metrics["initial_settling_tick"] is None
    assert metrics["recovery_settling_tick"] is None
    assert metrics["overshoot_percent"] == 0.0
    assert metrics["success_rate"] == 1.0

    must_reject(profile, valid[:600], "fixed", 7, "exactly 1800")

    gap = trajectory_rows()
    gap[777] = dict(gap[777], sample_index="778")
    must_reject(profile, gap, "fixed", 7, "continuous sample_index")

    must_reject(profile, trajectory_rows(controller="mlp"), "fixed", 7, "controller")
    must_reject(profile, trajectory_rows(seed=9), "fixed", 7, "seed")

    bad_feedback = trajectory_rows()
    bad_feedback[100] = dict(bad_feedback[100], feedback_confirmed="0")
    must_reject(profile, bad_feedback, "fixed", 7, "feedback")

    impossible = trajectory_rows()
    impossible[50] = dict(impossible[50], measured_mC="55000")
    must_reject(profile, impossible, "fixed", 7, "plant recurrence")

    marker = "TGOS_LINUX_TRAJ_DONE ticks=1800 mode=fixed seed=7 acked=1800 feedback=1800"
    fields = closed_loop_contract.parse_completion_marker(marker)
    closed_loop_contract.validate_completion_fields(
        fields, profile=profile, controller="fixed", seed=7
    )
    try:
        closed_loop_contract.validate_completion_fields(
            dict(fields, feedback=1799),
            profile=profile,
            controller="fixed",
            seed=7,
        )
    except ValueError as error:
        assert "feedback" in str(error)
    else:
        raise AssertionError("TEST-017 accepted an incomplete feedback marker")

    linux_events = []
    zephyr_events = [safe_event("safe_enter", 0), safe_event("safe_exit", 1)]
    for index in range(1800):
        row = checked[index]
        input_mC = 25_000 if index == 0 else int(checked[index - 1]["measured_mC"])
        linux_events.extend(
            request_event("linux", name, row, input_mC)
            for name in (
                "period_release",
                "period_start",
                "input_receive",
                "packet_send",
                "ack_receive",
                "feedback_receive",
                "period_finish",
            )
        )
        zephyr_events.extend(
            request_event("zephyr", name, row, input_mC)
            for name in (
                "period_release",
                "period_start",
                "packet_receive",
                "ack_send",
                "control_apply",
                "packet_send",
                "period_finish",
            )
        )
    closed_loop_contract.validate_event_associations(
        linux_events,
        zephyr_events,
        trajectory=checked,
        run_id="phase5-ai-fixed-s7-contract",
        controller="fixed",
    )
    wrong_safe_session = [dict(item) for item in zephyr_events]
    wrong_safe_session[0]["session_id"] = 18
    try:
        closed_loop_contract.validate_event_associations(
            linux_events,
            wrong_safe_session,
            trajectory=checked,
            run_id="phase5-ai-fixed-s7-contract",
            controller="fixed",
        )
    except ValueError as error:
        assert "safe_enter session mismatch" in str(error), str(error)
    else:
        raise AssertionError("TEST-017 accepted safe events from another session")

    missing_feedback = [
        item
        for item in linux_events
        if not (
            item["request_id"] == 1800 and item["event"] == "feedback_receive"
        )
    ]
    try:
        closed_loop_contract.validate_event_associations(
            missing_feedback,
            zephyr_events,
            trajectory=checked,
            run_id="phase5-ai-fixed-s7-contract",
            controller="fixed",
        )
    except ValueError as error:
        assert "feedback_receive" in str(error)
    else:
        raise AssertionError("TEST-017 accepted a missing feedback event")

    missing_ack = [
        item
        for item in linux_events
        if not (item["request_id"] == 1800 and item["event"] == "ack_receive")
    ]
    try:
        closed_loop_contract.validate_event_associations(
            missing_ack,
            zephyr_events,
            trajectory=checked,
            run_id="phase5-ai-fixed-s7-contract",
            controller="fixed",
        )
    except ValueError as error:
        assert "ack_receive" in str(error)
    else:
        raise AssertionError("TEST-017 accepted a missing ACK event")

    bad_rtt = list(checked)
    bad_rtt[0] = dict(bad_rtt[0], closed_loop_rtt_ns=199_999)
    try:
        closed_loop_contract.validate_event_associations(
            linux_events,
            zephyr_events,
            trajectory=bad_rtt,
            run_id="phase5-ai-fixed-s7-contract",
            controller="fixed",
        )
    except ValueError as error:
        assert "RTT" in str(error), str(error)
    else:
        raise AssertionError("TEST-017 accepted a trajectory/raw-event RTT mismatch")

    unknown_apply = list(zephyr_events)
    extra_apply = event("zephyr", "control_apply", 9999, 1799)
    extra_apply["monotonic_ns"] = int(zephyr_events[-1]["monotonic_ns"]) + 1
    unknown_apply.append(extra_apply)
    try:
        closed_loop_contract.validate_event_associations(
            linux_events,
            unknown_apply,
            trajectory=checked,
            run_id="phase5-ai-fixed-s7-contract",
            controller="fixed",
        )
    except ValueError as error:
        assert "unknown request" in str(error)
    else:
        raise AssertionError("TEST-017 accepted an unassociated control_apply")

    canonical_mlp_weights = model.load_model(
        ROOT / "apps" / "contest" / "linux-ai-controller" / "model" / "model.bin"
    )[1]
    checked_mlp = closed_loop_contract.validate_trajectory(
        trajectory_rows(controller="mlp"),
        profile=profile,
        controller="mlp",
        seed=7,
        mlp_infer=(
            lambda measured_mC, target_mC, previous_duty_q16_16: model.infer_duty_q16_16(
                canonical_mlp_weights,
                measured_mC,
                target_mC,
                previous_duty_q16_16,
            )
        ),
    )
    try:
        closed_loop_contract.validate_event_associations(
            linux_events,
            zephyr_events,
            trajectory=checked_mlp,
            run_id="phase5-ai-fixed-s7-contract",
            controller="mlp",
        )
    except ValueError as error:
        assert "inference" in str(error), str(error)
    else:
        raise AssertionError("TEST-017 accepted MLP events without inference")

    print("P5_TEST017_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, ValueError) as error:
        print(f"P5 TEST-017 contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
