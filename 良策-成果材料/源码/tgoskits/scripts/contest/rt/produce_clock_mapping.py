#!/usr/bin/env python3
"""Produce one session-owned P3 clock mapping from explicit counter samples."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    import clock_contract
    import p3_contract
except ImportError:  # pragma: no cover - package import path
    from . import clock_contract, p3_contract


SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SAMPLE_FIELDS = {
    "vm_id",
    "vcpu_id",
    "host_counter_ticks",
    "guest_counter_ticks",
}


class ClockProducerError(ValueError):
    """Raised when raw clock observations cannot form a safe mapping."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_id(value: str, field: str) -> str:
    if SAFE_ID.fullmatch(value) is None:
        raise ClockProducerError(f"{field} must be a safe identifier")
    return value


def _integer(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClockProducerError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ClockProducerError(f"{field} must be >= {minimum}")
    return value


def _parse_offset(value: str) -> tuple[str, int]:
    identity, separator, offset_text = value.partition("=")
    if not separator or identity.count(":") != 1:
        raise ClockProducerError("--offset must use VM:VCPU=SIGNED_TICKS")
    vm_text, vcpu_text = identity.split(":")
    if not vm_text.isdecimal() or not vcpu_text.isdecimal():
        raise ClockProducerError("--offset VM and VCPU must be non-negative integers")
    try:
        offset = int(offset_text, 10)
    except ValueError as error:
        raise ClockProducerError("--offset ticks must be a signed integer") from error
    return f"{int(vm_text)}:{int(vcpu_text)}", offset


def parse_offsets(values: list[str]) -> dict[str, int]:
    """Parse unique VM/VCPU offsets from command-line bindings."""

    offsets: dict[str, int] = {}
    for value in values:
        identity, offset = _parse_offset(value)
        if identity in offsets:
            raise ClockProducerError(f"duplicate --offset identity: {identity}")
        offsets[identity] = offset
    if not offsets:
        raise ClockProducerError("at least one --offset is required")
    return offsets


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ClockProducerError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_samples(path: Path) -> list[dict[str, int]]:
    """Load strict raw host/guest counter pairs from a regular JSONL file."""

    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ClockProducerError(f"sample input must be a regular file: {source}")
    samples: list[dict[str, int]] = []
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ClockProducerError(f"cannot read sample input: {error}") from error
    if not lines:
        raise ClockProducerError("sample input must not be empty")
    for line_number, line in enumerate(lines, 1):
        try:
            record = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        except (json.JSONDecodeError, ClockProducerError) as error:
            raise ClockProducerError(
                f"invalid calibration JSONL line {line_number}: {error}"
            ) from error
        if not isinstance(record, Mapping) or set(record) != SAMPLE_FIELDS:
            raise ClockProducerError(
                f"calibration line {line_number} must contain exactly {sorted(SAMPLE_FIELDS)}"
            )
        samples.append(
            {
                "vm_id": _integer(record["vm_id"], f"line {line_number}.vm_id", minimum=0),
                "vcpu_id": _integer(
                    record["vcpu_id"], f"line {line_number}.vcpu_id", minimum=0
                ),
                "host_counter_ticks": _integer(
                    record["host_counter_ticks"],
                    f"line {line_number}.host_counter_ticks",
                    minimum=0,
                ),
                "guest_counter_ticks": _integer(
                    record["guest_counter_ticks"],
                    f"line {line_number}.guest_counter_ticks",
                    minimum=0,
                ),
            }
        )
    return samples


def build_clock_document(
    *,
    run_id: str,
    mapping_id: str,
    frequency_hz: int,
    offsets: Mapping[str, int],
    raw_samples: list[Mapping[str, int]],
    max_residual_ns: int,
    sample_source: Path,
) -> dict[str, Any]:
    """Compute residuals and build a fully bound ``p3-clock-v1`` document."""

    run_id = _safe_id(run_id, "run_id")
    mapping_id = _safe_id(mapping_id, "clock_mapping_id")
    frequency_hz = _integer(frequency_hz, "counter_frequency_hz", minimum=1)
    max_residual_ns = _integer(
        max_residual_ns, "max_mapping_residual_ns", minimum=0
    )
    normalized_offsets = dict(offsets)
    calibrated: list[dict[str, int]] = []
    sampled_offsets: set[str] = set()
    seen: set[tuple[int, int, int, int]] = set()
    for index, sample in enumerate(raw_samples):
        vm_id = _integer(sample.get("vm_id"), f"samples[{index}].vm_id", minimum=0)
        vcpu_id = _integer(
            sample.get("vcpu_id"), f"samples[{index}].vcpu_id", minimum=0
        )
        host_ticks = _integer(
            sample.get("host_counter_ticks"),
            f"samples[{index}].host_counter_ticks",
            minimum=0,
        )
        guest_ticks = _integer(
            sample.get("guest_counter_ticks"),
            f"samples[{index}].guest_counter_ticks",
            minimum=0,
        )
        identity = f"{vm_id}:{vcpu_id}"
        if identity not in normalized_offsets:
            raise ClockProducerError(f"sample has no registered --offset: {identity}")
        sample_identity = (vm_id, vcpu_id, host_ticks, guest_ticks)
        if sample_identity in seen:
            raise ClockProducerError(f"duplicate calibration sample: {sample_identity}")
        seen.add(sample_identity)
        sampled_offsets.add(identity)
        mapped_ticks = guest_ticks + normalized_offsets[identity]
        if mapped_ticks < 0:
            raise ClockProducerError(f"sample maps before counter epoch: {identity}")
        residual_ns = (
            abs(host_ticks - mapped_ticks) * 1_000_000_000
        ) // frequency_hz
        if residual_ns > max_residual_ns:
            raise ClockProducerError(
                f"sample residual {residual_ns} ns exceeds threshold for {identity}"
            )
        calibrated.append(
            {
                "vm_id": vm_id,
                "vcpu_id": vcpu_id,
                "host_counter_ticks": host_ticks,
                "guest_counter_ticks": guest_ticks,
                "residual_ns": residual_ns,
            }
        )
    missing = sorted(set(normalized_offsets) - sampled_offsets)
    if missing:
        raise ClockProducerError(
            "every registered VM/VCPU requires a calibration sample; missing "
            + ", ".join(missing)
        )
    source = Path(sample_source).resolve()
    document: dict[str, Any] = {
        "schema_version": clock_contract.CLOCK_SCHEMA,
        "run_id": run_id,
        "clock_mapping_id": mapping_id,
        "counter_frequency_hz": frequency_hz,
        "host_counter": clock_contract.EXPECTED_HOST_COUNTER,
        "guest_counter": clock_contract.EXPECTED_GUEST_COUNTER,
        "cntvoff_ticks_by_vcpu": normalized_offsets,
        "mapping": clock_contract.EXPECTED_MAPPING,
        "calibration_samples": calibrated,
        "max_mapping_residual_ns": max_residual_ns,
        "source": {
            "path": str(source),
            "size": source.stat().st_size,
            "sha256": _sha256(source),
        },
        "producer": "scripts/contest/rt/produce_clock_mapping.py",
    }
    clock_contract.validate_clock_document(
        document, require_calibration=True, location="generated clock.json"
    )
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--clock-mapping-id")
    parser.add_argument("--counter-frequency-hz", required=True, type=int)
    parser.add_argument("--offset", action="append", default=[])
    parser.add_argument("--samples", required=True, type=Path)
    parser.add_argument("--max-mapping-residual-ns", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        mapping_id = args.clock_mapping_id or f"{args.run_id}-clock"
        offsets = parse_offsets(args.offset)
        samples = load_samples(args.samples)
        document = build_clock_document(
            run_id=args.run_id,
            mapping_id=mapping_id,
            frequency_hz=args.counter_frequency_hz,
            offsets=offsets,
            raw_samples=samples,
            max_residual_ns=args.max_mapping_residual_ns,
            sample_source=args.samples,
        )
        p3_contract.write_json(args.output, document)
    except (ClockProducerError, clock_contract.ClockContractError, p3_contract.P3ContractError, OSError) as error:
        print(f"P3 clock mapping production failed: {error}")
        return 1
    print(
        f"P3_CLOCK_MAPPING_PRODUCED run_id={args.run_id} "
        f"samples={len(samples)} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
