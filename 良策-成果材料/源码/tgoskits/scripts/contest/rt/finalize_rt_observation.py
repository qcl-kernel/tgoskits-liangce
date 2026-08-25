#!/usr/bin/env python3
"""Finalize one P3 probe log into a clock-bound observation package."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

try:
    import extract_probe_events
    import p3_contract
    import produce_clock_mapping
    import summarize_rt
    import validate_rt_events
except ImportError:  # pragma: no cover - package import path
    from . import (
        extract_probe_events,
        p3_contract,
        produce_clock_mapping,
        summarize_rt,
        validate_rt_events,
    )


class ObservationFinalizationError(ValueError):
    """Raised when raw P3 inputs cannot form one immutable observation."""


def finalize_observation(
    *,
    log_path: Path,
    sample_path: Path,
    schema_path: Path,
    output_dir: Path,
    run_id: str,
    scenario: str,
    mapping_id: str,
    frequency_hz: int,
    offsets: dict[str, int],
    max_residual_ns: int,
    period_ns: int,
) -> dict[str, Any]:
    """Extract events, produce their clock, and publish status last."""

    output = Path(output_dir)
    if output.exists():
        raise ObservationFinalizationError(f"output directory already exists: {output}")
    if period_ns <= 0:
        raise ObservationFinalizationError("period_ns must be positive")

    metadata, events = extract_probe_events.extract_probe_log(
        log_path,
        run_id=run_id,
        scenario=scenario,
        mapping_id=mapping_id,
    )
    if metadata["counter_frequency_hz"] != frequency_hz:
        raise ObservationFinalizationError(
            "requested counter frequency differs from the probe READY marker"
        )
    vm_id = metadata["vm_id"]
    vcpu_id = metadata["vcpu_id"]
    if vm_id is not None and vcpu_id is not None:
        identity = f"{vm_id}:{vcpu_id}"
        if offsets.get(identity) != metadata["cntvoff_ticks"]:
            raise ObservationFinalizationError(
                "requested CNTVOFF differs from the probe READY marker"
            )

    extract_probe_events.publish_extraction(output, metadata, events)
    raw_samples = produce_clock_mapping.load_samples(sample_path)
    clock = produce_clock_mapping.build_clock_document(
        run_id=run_id,
        mapping_id=mapping_id,
        frequency_hz=frequency_hz,
        offsets=offsets,
        raw_samples=raw_samples,
        max_residual_ns=max_residual_ns,
        sample_source=sample_path,
    )
    p3_contract.write_json(output / "clock.json", clock)

    validation = validate_rt_events.validate_run(
        output,
        schema_path,
        output / "clock.json",
        require_calibration=True,
    )
    summary = summarize_rt.summarize_run(
        output,
        schema_path,
        period_ns,
        output / "clock.json",
    )
    p3_contract.write_json(output / "validation.json", validation)
    p3_contract.write_json(output / "summary.json", summary)
    p3_contract.write_json(
        output / "manifest.json",
        {
            "schema_version": "p3-rt-observation-manifest-v1",
            "run_id": run_id,
            "files": p3_contract.manifest_files(
                output, excluded=("manifest.json", "status.json")
            ),
            "excluded_terminal_files": ["manifest.json", "status.json"],
        },
    )
    p3_contract.write_status_last(
        output,
        {
            "schema_version": "p3-rt-observation-status-v1",
            "run_id": run_id,
            "success": True,
            "status": "realtime_observation_completed",
            "sample_count": metadata["sample_count"],
            "event_count": metadata["event_count"],
            "dropped_events": 0,
            "clock_mapping_id": mapping_id,
            "qualified": False,
            "statusLast": True,
            "manifestSha256": p3_contract.sha256_file(output / "manifest.json"),
            "non_claims": [
                "one observation does not prove production A/B improvement",
                "qualification still requires the frozen matrix and independent runs",
            ],
        },
    )
    return {
        "run_id": run_id,
        "sample_count": metadata["sample_count"],
        "event_count": metadata["event_count"],
        "clock_mapping_id": mapping_id,
    }


def main(argv: list[str] | None = None) -> int:
    repository = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--clock-samples", required=True, type=Path)
    parser.add_argument("--schema", type=Path, default=repository / "scripts" / "contest" / "rt" / "schema" / "p3-rt-event-v1.schema.json")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--clock-mapping-id", required=True)
    parser.add_argument("--counter-frequency-hz", required=True, type=int)
    parser.add_argument("--offset", action="append", default=[])
    parser.add_argument("--max-mapping-residual-ns", required=True, type=int)
    parser.add_argument("--period-ns", type=int, default=100_000_000)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = finalize_observation(
            log_path=args.log,
            sample_path=args.clock_samples,
            schema_path=args.schema,
            output_dir=args.output_dir,
            run_id=args.run_id,
            scenario=args.scenario,
            mapping_id=args.clock_mapping_id,
            frequency_hz=args.counter_frequency_hz,
            offsets=produce_clock_mapping.parse_offsets(args.offset),
            max_residual_ns=args.max_mapping_residual_ns,
            period_ns=args.period_ns,
        )
    except (
        OSError,
        ValueError,
        extract_probe_events.ProbeExtractionError,
        p3_contract.P3ContractError,
        produce_clock_mapping.ClockProducerError,
    ) as error:
        print(f"P3 observation finalization failed: {error}")
        return 1
    print(
        f"P3_RT_OBSERVATION_FINALIZED run_id={result['run_id']} "
        f"samples={result['sample_count']} output={args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
