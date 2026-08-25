#!/usr/bin/env python3
"""Host-only validation for the P3 cross-EL clock mapping contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CLOCK_SCHEMA = "p3-clock-v1"
CLOCK_REQUIRED_FIELDS = (
    "schema_version",
    "counter_frequency_hz",
    "host_counter",
    "guest_counter",
    "cntvoff_ticks_by_vcpu",
    "mapping",
    "calibration_samples",
    "max_mapping_residual_ns",
)
CALIBRATION_FIELDS = (
    "vm_id",
    "vcpu_id",
    "host_counter_ticks",
    "guest_counter_ticks",
    "residual_ns",
)
EXPECTED_HOST_COUNTER = "CNTPCT_EL0"
EXPECTED_GUEST_COUNTER = "CNTVCT_EL0"
EXPECTED_MAPPING = "host_ticks = guest_ticks + cntvoff_ticks"


class ClockContractError(ValueError):
    """Raised when a P3 clock document or mapped event is unsafe to use."""


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_integer(value: Any, field: str, *, minimum: int | None = None) -> int:
    if not _is_integer(value) or (minimum is not None and value < minimum):
        suffix = f" >= {minimum}" if minimum is not None else ""
        raise ClockContractError(f"{field} must be an integer{suffix}")
    return value


def _offset_key(vm_id: int, vcpu_id: int) -> str:
    return f"{vm_id}:{vcpu_id}"


def _parse_offset_key(key: Any, location: str) -> tuple[int, int]:
    if not isinstance(key, str) or key.count(":") != 1:
        raise ClockContractError(f"{location} must be a VM:VCPU key")
    vm_text, vcpu_text = key.split(":")
    if not vm_text.isdecimal() or not vcpu_text.isdecimal():
        raise ClockContractError(f"{location} must be a non-negative VM:VCPU key")
    return int(vm_text), int(vcpu_text)


def map_counter_to_monotonic_ns(
    raw_counter_ticks: int,
    counter_frequency_hz: int,
    cntvoff_ticks: int = 0,
) -> int:
    """Map ticks first, then apply the documented floor conversion."""

    raw_counter_ticks = _require_integer(
        raw_counter_ticks, "raw_counter_ticks", minimum=0
    )
    counter_frequency_hz = _require_integer(
        counter_frequency_hz, "counter_frequency_hz", minimum=1
    )
    cntvoff_ticks = _require_integer(cntvoff_ticks, "cntvoff_ticks")
    mapped_ticks = raw_counter_ticks + cntvoff_ticks
    if mapped_ticks < 0:
        raise ClockContractError("mapped counter ticks must be non-negative")
    return (mapped_ticks * 1_000_000_000) // counter_frequency_hz


def validate_clock_document(
    document: Mapping[str, Any],
    *,
    require_calibration: bool = False,
    location: str = "clock.json",
) -> dict[str, Any]:
    """Validate and summarize a session-owned ``p3-clock-v1`` document."""

    if not isinstance(document, Mapping):
        raise ClockContractError(f"{location}: root must be a JSON object")
    missing = [field for field in CLOCK_REQUIRED_FIELDS if field not in document]
    if missing:
        raise ClockContractError(f"{location}: missing fields {', '.join(missing)}")
    if document["schema_version"] != CLOCK_SCHEMA:
        raise ClockContractError(f"{location}: unsupported schema_version")
    frequency = _require_integer(
        document["counter_frequency_hz"],
        f"{location}.counter_frequency_hz",
        minimum=1,
    )
    if document["host_counter"] != EXPECTED_HOST_COUNTER:
        raise ClockContractError(
            f"{location}.host_counter must be {EXPECTED_HOST_COUNTER}"
        )
    if document["guest_counter"] != EXPECTED_GUEST_COUNTER:
        raise ClockContractError(
            f"{location}.guest_counter must be {EXPECTED_GUEST_COUNTER}"
        )
    if document["mapping"] != EXPECTED_MAPPING:
        raise ClockContractError(f"{location}.mapping is not the P3 mapping")

    offsets = document["cntvoff_ticks_by_vcpu"]
    if not isinstance(offsets, Mapping) or not offsets:
        raise ClockContractError(
            f"{location}.cntvoff_ticks_by_vcpu must be a non-empty object"
        )
    normalized_offsets: dict[str, int] = {}
    for key, value in offsets.items():
        vm_id, vcpu_id = _parse_offset_key(
            key, f"{location}.cntvoff_ticks_by_vcpu key"
        )
        normalized_key = _offset_key(vm_id, vcpu_id)
        if normalized_key in normalized_offsets:
            raise ClockContractError(
                f"{location}.cntvoff_ticks_by_vcpu contains duplicate VM/VCPU mapping"
            )
        normalized_offsets[normalized_key] = _require_integer(
            value,
            f"{location}.cntvoff_ticks_by_vcpu[{key!r}]",
        )

    max_residual = _require_integer(
        document["max_mapping_residual_ns"],
        f"{location}.max_mapping_residual_ns",
        minimum=0,
    )
    samples = document["calibration_samples"]
    if not isinstance(samples, list):
        raise ClockContractError(f"{location}.calibration_samples must be a list")
    if require_calibration and not samples:
        raise ClockContractError(f"{location}: calibration_samples must not be empty")

    seen_samples: set[tuple[int, int, int, int]] = set()
    observed_max_residual = 0
    for index, sample in enumerate(samples):
        sample_location = f"{location}.calibration_samples[{index}]"
        if not isinstance(sample, Mapping):
            raise ClockContractError(f"{sample_location} must be an object")
        missing_sample_fields = [
            field for field in CALIBRATION_FIELDS if field not in sample
        ]
        if missing_sample_fields:
            raise ClockContractError(
                f"{sample_location}: missing fields {', '.join(missing_sample_fields)}"
            )
        vm_id = _require_integer(sample["vm_id"], f"{sample_location}.vm_id", minimum=0)
        vcpu_id = _require_integer(
            sample["vcpu_id"], f"{sample_location}.vcpu_id", minimum=0
        )
        host_ticks = _require_integer(
            sample["host_counter_ticks"],
            f"{sample_location}.host_counter_ticks",
            minimum=0,
        )
        guest_ticks = _require_integer(
            sample["guest_counter_ticks"],
            f"{sample_location}.guest_counter_ticks",
            minimum=0,
        )
        residual_ns = _require_integer(
            sample["residual_ns"], f"{sample_location}.residual_ns", minimum=0
        )
        sample_identity = (vm_id, vcpu_id, host_ticks, guest_ticks)
        if sample_identity in seen_samples:
            raise ClockContractError(f"{sample_location}: duplicate calibration sample")
        seen_samples.add(sample_identity)
        offset_key = _offset_key(vm_id, vcpu_id)
        if offset_key not in normalized_offsets:
            raise ClockContractError(
                f"{sample_location}: VM/VCPU has no registered CNTVOFF mapping"
            )
        mapped_ticks = guest_ticks + normalized_offsets[offset_key]
        actual_residual_ns = (
            abs(host_ticks - mapped_ticks) * 1_000_000_000
        ) // frequency
        if residual_ns != actual_residual_ns:
            raise ClockContractError(
                f"{sample_location}: residual_ns does not match integer mapping"
            )
        observed_max_residual = max(observed_max_residual, residual_ns)
        if residual_ns > max_residual:
            raise ClockContractError(
                f"{sample_location}: mapping residual exceeds registered threshold"
            )

    mapping_id = document.get("clock_mapping_id")
    if mapping_id is not None and (
        not isinstance(mapping_id, str) or not mapping_id
    ):
        raise ClockContractError(f"{location}.clock_mapping_id must be a non-empty string")
    return {
        "schema_version": CLOCK_SCHEMA,
        "counter_frequency_hz": frequency,
        "clock_mapping_id": mapping_id,
        "calibration_sample_count": len(samples),
        "max_mapping_residual_ns": max_residual,
        "observed_max_mapping_residual_ns": observed_max_residual,
        "offsets": normalized_offsets,
    }


def validate_events_against_clock(
    events: list[Mapping[str, Any]],
    document: Mapping[str, Any],
    *,
    location: str = "events",
) -> dict[str, Any]:
    """Recompute every event timestamp from its raw counter and clock mapping."""

    summary = validate_clock_document(document, location="clock.json")
    frequency = summary["counter_frequency_hz"]
    offsets = summary["offsets"]
    mapping_id = summary["clock_mapping_id"]
    for index, event in enumerate(events):
        event_location = f"{location}[{index}]"
        if event["counter_frequency_hz"] != frequency:
            raise ClockContractError(
                f"{event_location}: counter_frequency_hz differs from clock.json"
            )
        if mapping_id is not None and event["clock_mapping_id"] != mapping_id:
            raise ClockContractError(
                f"{event_location}: clock_mapping_id differs from clock.json"
            )
        vm_id = event["vm_id"]
        vcpu_id = event["vcpu_id"]
        if (vm_id is None) != (vcpu_id is None):
            raise ClockContractError(
                f"{event_location}: vm_id and vcpu_id must be both present or null"
            )
        if vm_id is None:
            offset = 0
        else:
            key = _offset_key(vm_id, vcpu_id)
            if key not in offsets:
                raise ClockContractError(
                    f"{event_location}: VM/VCPU has no registered CNTVOFF mapping"
                )
            offset = offsets[key]
        expected = map_counter_to_monotonic_ns(
            event["raw_counter_ticks"], frequency, offset
        )
        if event["monotonic_ns"] != expected:
            raise ClockContractError(
                f"{event_location}: monotonic_ns is not reproducible from clock mapping"
            )
    return {
        "schema_version": CLOCK_SCHEMA,
        "event_count": len(events),
        "clock_mapping_id": mapping_id,
        "calibration_sample_count": summary["calibration_sample_count"],
    }
