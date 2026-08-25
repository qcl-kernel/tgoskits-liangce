#!/usr/bin/env python3
"""Extract one complete Zephyr P3 probe stream from an AxVisor console log."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:
    import p3_contract
except ImportError:  # pragma: no cover - package import path
    from . import p3_contract


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CONSOLE_PREFIX = re.compile(r"^\[VM (?P<vm>[0-9]+)\]\s*")
READY = re.compile(
    r"^P3_RT_PROBE_READY run_id=(?P<run_id>\S+) scenario=(?P<scenario>\S+) "
    r"mapping=(?P<mapping>\S+) frequency=(?P<frequency>[0-9]+) "
    r"cntvoff=(?P<cntvoff>-?[0-9]+) vm=(?P<vm>null|[0-9]+) "
    r"vcpu=(?P<vcpu>null|[0-9]+) pcpu=(?P<pcpu>null|[0-9]+)$"
)
COMPLETE = re.compile(
    r"^P3_RT_PROBE_COMPLETE run_id=(?P<run_id>\S+) scenario=(?P<scenario>\S+) "
    r"mapping=(?P<mapping>\S+) samples=(?P<samples>[0-9]+) "
    r"dropped=(?P<dropped>[0-9]+)$"
)
EVENT_NAMES = ("period_release", "period_start", "period_finish")


class ProbeExtractionError(ValueError):
    """Raised when a console log is not one complete, lossless P3 probe run."""


def _regular_file(path: Path, label: str) -> Path:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ProbeExtractionError(f"{label} must be a regular file: {candidate}")
    return candidate.resolve()


def _strip_console_prefix(line: str) -> str:
    normalized = ANSI_ESCAPE.sub("", line).strip()
    return CONSOLE_PREFIX.sub("", normalized, count=1)


def _numeric_identity(value: str) -> int | None:
    return None if value == "null" else int(value, 10)


def _single_match(
    matches: list[re.Match[str]], label: str
) -> re.Match[str]:
    if len(matches) != 1:
        raise ProbeExtractionError(f"expected exactly one {label}, found {len(matches)}")
    return matches[0]


def extract_probe_log(
    log_path: Path,
    *,
    run_id: str,
    scenario: str,
    mapping_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse and reconcile terminal markers with all event records."""

    source = _regular_file(log_path, "probe console log")
    try:
        lines = source.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ProbeExtractionError(f"cannot read probe console log: {error}") from error
    normalized = [_strip_console_prefix(line) for line in lines]
    if any("P3_RT_PROBE_FAIL" in line for line in normalized):
        raise ProbeExtractionError("probe emitted P3_RT_PROBE_FAIL")

    ready = _single_match(
        [match for line in normalized if (match := READY.fullmatch(line))],
        "P3_RT_PROBE_READY marker",
    )
    complete = _single_match(
        [match for line in normalized if (match := COMPLETE.fullmatch(line))],
        "P3_RT_PROBE_COMPLETE marker",
    )
    for field, expected in (
        ("run_id", run_id),
        ("scenario", scenario),
        ("mapping", mapping_id),
    ):
        if ready.group(field) != expected or complete.group(field) != expected:
            raise ProbeExtractionError(f"probe {field} differs from requested identity")
    sample_count = int(complete.group("samples"), 10)
    if sample_count <= 0:
        raise ProbeExtractionError("probe sample count must be positive")
    dropped = int(complete.group("dropped"), 10)
    if dropped != 0:
        raise ProbeExtractionError(f"probe dropped {dropped} events")

    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(normalized, 1):
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ProbeExtractionError(
                f"invalid probe JSON at console line {line_number}: {error}"
            ) from error
        if not isinstance(record, dict) or record.get("schema_version") != "p3-rt-event-v1":
            continue
        events.append(record)
    if len(events) != sample_count * len(EVENT_NAMES):
        raise ProbeExtractionError(
            f"expected {sample_count * len(EVENT_NAMES)} events, found {len(events)}"
        )

    expected_vm = _numeric_identity(ready.group("vm"))
    expected_vcpu = _numeric_identity(ready.group("vcpu"))
    expected_pcpu = _numeric_identity(ready.group("pcpu"))
    expected_frequency = int(ready.group("frequency"), 10)
    expected_trace = 0
    per_sample: dict[int, Counter[str]] = {}
    for index, event in enumerate(events):
        for field, expected in (
            ("run_id", run_id),
            ("scenario", scenario),
            ("clock_mapping_id", mapping_id),
            ("counter_frequency_hz", expected_frequency),
            ("vm_id", expected_vm),
            ("vcpu_id", expected_vcpu),
            ("pcpu_id", expected_pcpu),
        ):
            if event.get(field) != expected:
                raise ProbeExtractionError(
                    f"event {index} {field} differs from READY identity"
                )
        trace_sequence = event.get("trace_sequence")
        if trace_sequence != expected_trace:
            raise ProbeExtractionError(
                f"event {index} trace_sequence is {trace_sequence!r}, expected {expected_trace}"
            )
        expected_trace += 1
        sample_index = event.get("sample_index")
        if isinstance(sample_index, bool) or not isinstance(sample_index, int):
            raise ProbeExtractionError(f"event {index} has invalid sample_index")
        if not 0 <= sample_index < sample_count:
            raise ProbeExtractionError(f"event {index} sample_index is out of range")
        event_name = event.get("event")
        if event_name not in EVENT_NAMES:
            raise ProbeExtractionError(f"event {index} is not a probe period event")
        per_sample.setdefault(sample_index, Counter())[event_name] += 1
    expected_counts = Counter({name: 1 for name in EVENT_NAMES})
    for sample_index in range(sample_count):
        if per_sample.get(sample_index) != expected_counts:
            raise ProbeExtractionError(
                f"sample {sample_index} does not contain exactly one release/start/finish"
            )

    metadata = {
        "schema_version": "p3-rt-probe-extraction-v1",
        "run_id": run_id,
        "scenario": scenario,
        "clock_mapping_id": mapping_id,
        "counter_frequency_hz": expected_frequency,
        "cntvoff_ticks": int(ready.group("cntvoff"), 10),
        "vm_id": expected_vm,
        "vcpu_id": expected_vcpu,
        "pcpu_id": expected_pcpu,
        "sample_count": sample_count,
        "event_count": len(events),
        "dropped_events": dropped,
        "source": {
            "path": str(source),
            "size": source.stat().st_size,
            "sha256": p3_contract.sha256_file(source),
        },
        "qualified": False,
    }
    return metadata, events


def publish_extraction(
    output_dir: Path,
    metadata: dict[str, Any],
    events: list[dict[str, Any]],
) -> None:
    """Publish a new no-overwrite extraction directory."""

    output = Path(output_dir)
    if output.exists():
        raise ProbeExtractionError(f"output directory already exists: {output}")
    output.mkdir(parents=True)
    event_path = output / "events" / "zephyr.jsonl"
    event_path.parent.mkdir()
    p3_contract.publish_new_file(
        event_path,
        "".join(
            json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
            for event in events
        ).encode("utf-8"),
        error_type=ProbeExtractionError,
    )
    p3_contract.write_json(output / "probe.json", metadata)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--clock-mapping-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        metadata, events = extract_probe_log(
            args.log,
            run_id=args.run_id,
            scenario=args.scenario,
            mapping_id=args.clock_mapping_id,
        )
        publish_extraction(args.output_dir, metadata, events)
    except (OSError, ProbeExtractionError, p3_contract.P3ContractError) as error:
        print(f"P3 probe extraction failed: {error}")
        return 1
    print(
        f"P3_RT_PROBE_EXTRACTED run_id={args.run_id} "
        f"samples={metadata['sample_count']} output={args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
