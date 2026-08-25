#!/usr/bin/env python3
"""Load and bind one immutable TEST-018 fault case.

This is a host/source contract only.  It validates the frozen profile before a
runner can associate a case with a fresh session; it does not inject faults,
start a Guest, or publish qualification evidence.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


SCHEMA = "p5-ai-faults-v1"
PROFILE_ID = "faults-v1"
TEST_ID = "TEST-018"
CASE_IDS = (
    "duty-out-of-range",
    "bad-model-version",
    "duplicate-control",
    "stale-request",
    "bad-crc-schema",
    "linux-stop",
    "network-stop",
    "endpoint-restart",
    "udp-expiry-tcp",
)


class FaultProfileError(ValueError):
    """Raised when a TEST-018 profile or case is unsafe to bind."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FaultProfileError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FaultProfileError(f"{label} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise FaultProfileError(
            f"{label} keys drifted: expected {sorted(expected)}, got {sorted(value)}"
        )


def validate_case(value: Any, index: int = 0) -> dict[str, Any]:
    """Validate one TEST-018 case against the frozen semantics."""

    record = _object(value, f"cases[{index}]")
    _exact_keys(record, {"case", "inject", "expected", "runtime"}, f"cases[{index}]")
    case_id = record.get("case")
    if case_id not in CASE_IDS:
        raise FaultProfileError(f"unknown TEST-018 case: {case_id!r}")
    inject = _object(record["inject"], f"{case_id}.inject")
    expected = _object(record["expected"], f"{case_id}.expected")
    runtime = _object(record["runtime"], f"{case_id}.runtime")
    _exact_keys(runtime, {"requires_new_session", "single_primary_fault"}, f"{case_id}.runtime")
    if runtime != {"requires_new_session": True, "single_primary_fault": True}:
        raise FaultProfileError(f"{case_id}: runtime identity is not fail-closed")

    if case_id == "duty-out-of-range":
        expected_inject = {"kind": "control_value", "values": [-1, 65537]}
        expected_result = {"apply": False, "watchdog_refresh": False, "error": "BAD_RANGE"}
    elif case_id == "bad-model-version":
        expected_inject = {"kind": "model_version", "value": "not-current"}
        expected_result = {"apply": False, "watchdog_refresh": False, "error": "VERSION_MISMATCH"}
    elif case_id == "duplicate-control":
        expected_inject = {"kind": "replay", "same": ["session_id", "sequence", "request_id"]}
        expected_result = {"ack_retransmit": True, "action_count": 1, "error": None}
    elif case_id == "stale-request":
        expected_inject = {"kind": "request_id", "direction": "backward"}
        expected_result = {"apply": False, "watchdog_refresh": False, "error": "STALE_REQUEST"}
    elif case_id == "bad-crc-schema":
        expected_inject = {"kind": "wire_mutation", "mutations": ["crc", "schema", "reserved"]}
        expected_result = {"apply": False, "watchdog_refresh": False, "error": "INVALID_PAYLOAD"}
    elif case_id in {"linux-stop", "network-stop"}:
        expected_inject = (
            {"kind": "endpoint_stop", "endpoint": "linux", "duration_ms": 700}
            if case_id == "linux-stop"
            else {"kind": "network_block", "duration_ms": 700}
        )
        expected_result = {
            "safe_deadline_ms": 500,
            "safe_tick_ms": 100,
            "duty_q16_16": 0,
            "error": "NETWORK_TIMEOUT",
        }
    elif case_id == "endpoint-restart":
        expected_inject = {"kind": "endpoint_restart", "endpoint": "either"}
        expected_result = {
            "new_session_nonzero": True,
            "old_session_action_count": 0,
            "error": "SESSION_RESET",
        }
    else:
        expected_inject = {"kind": "udp_ack_expiry", "retry_ms": [0, 100, 300], "cancel_ms": 500}
        expected_result = {
            "udp_closed_at_ms": 500,
            "tcp_recovery": True,
            "new_session_nonzero": True,
            "error": "TRANSPORT_FALLBACK",
        }
    if inject != expected_inject or expected != expected_result:
        raise FaultProfileError(f"{case_id} contract drifted")
    return record


def load_fault_profile(path: Path) -> dict[str, Any]:
    """Load and fully validate a TEST-018 profile, rejecting duplicate keys."""

    try:
        document = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, json.JSONDecodeError, FaultProfileError) as error:
        raise FaultProfileError(f"cannot read TEST-018 fault profile: {error}") from error
    document = _object(document, "fault profile")
    _exact_keys(
        document,
        {"schema_version", "profile_id", "test_id", "host_only", "evidence_level", "cases", "non_claims"},
        "fault profile",
    )
    if document["schema_version"] != SCHEMA or document["profile_id"] != PROFILE_ID:
        raise FaultProfileError("fault profile schema or identity is invalid")
    if document["test_id"] != TEST_ID or document["host_only"] is not True:
        raise FaultProfileError("fault profile is not host-only TEST-018")
    if document["evidence_level"] != "L2 host contract":
        raise FaultProfileError("fault profile evidence level drifted")
    cases = document["cases"]
    if not isinstance(cases, list) or tuple(
        record.get("case") for record in cases if isinstance(record, dict)
    ) != CASE_IDS:
        raise FaultProfileError("TEST-018 cases must be complete, ordered, and unique")
    for index, record in enumerate(cases):
        validate_case(record, index)
    non_claims = document["non_claims"]
    if not isinstance(non_claims, list) or len(non_claims) < 3 or any(
        not isinstance(item, str) or not item for item in non_claims
    ):
        raise FaultProfileError("fault profile non_claims are incomplete")
    return document


def select_fault_case(document: dict[str, Any], case_id: str) -> dict[str, Any]:
    """Return a detached case definition for immutable runner binding."""

    if not isinstance(case_id, str) or case_id not in CASE_IDS:
        raise FaultProfileError(f"unknown TEST-018 fault case: {case_id!r}")
    for index, record in enumerate(document.get("cases", [])):
        if isinstance(record, dict) and record.get("case") == case_id:
            return copy.deepcopy(validate_case(record, index))
    raise FaultProfileError(f"TEST-018 fault case is not present: {case_id}")
