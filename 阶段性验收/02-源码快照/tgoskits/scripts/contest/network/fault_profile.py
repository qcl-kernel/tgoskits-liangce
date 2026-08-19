#!/usr/bin/env python3
"""Deterministic host-side fault plans for the frozen P4 network contract.

The module deliberately operates on host-owned byte strings.  It does not know
about VirtIO rings, Guest memory, QEMU, or sockets.  A profile is parsed once,
then expanded into an immutable per-packet manifest.  No random source is used
when a plan is generated or applied.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "p4-network-fault-v1"
OPERATION_ORDER = ("corrupt", "reorder", "duplicate", "drop")
RULE_NAMES = ("drop_every", "duplicate_every", "reorder_every", "corrupt_every")
MAX_PACKET_COUNT = 1_000_000
VALID_SEEDS = (7, 19, 43)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_U64_MAX = (1 << 64) - 1


class FaultProfileError(ValueError):
    """Raised when a profile or a generated manifest violates the P4 contract."""


def _require_int(value: Any, field: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FaultProfileError(f"{field} must be an integer")
    if value < minimum:
        raise FaultProfileError(f"{field} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise FaultProfileError(f"{field} must be <= {maximum}")
    return value


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FaultProfileError(f"{field} must be an object")
    return value


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise FaultProfileError(f"{field} must be a safe non-empty identifier")
    return value


@dataclass(frozen=True)
class FaultRules:
    """The four one-based periodic rules frozen by TEST-012/015."""

    drop_every: int
    duplicate_every: int
    reorder_every: int
    corrupt_every: int

    def as_dict(self) -> dict[str, int]:
        return {
            "drop_every": self.drop_every,
            "duplicate_every": self.duplicate_every,
            "reorder_every": self.reorder_every,
            "corrupt_every": self.corrupt_every,
        }


@dataclass(frozen=True)
class FaultProfile:
    """Validated immutable fault configuration."""

    profile_id: str
    seed: int
    enabled: bool
    rules: FaultRules
    operation_order: tuple[str, ...] = OPERATION_ORDER

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "seed": self.seed,
            "enabled": self.enabled,
            "packet_numbering": "one-based",
            "rules": self.rules.as_dict(),
            "operation_order": list(self.operation_order),
        }


@dataclass(frozen=True)
class FaultEntry:
    """Actions selected for one original packet number."""

    packet_index: int
    actions: tuple[str, ...]
    reorder_with: int | None
    corrupt_mask: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "packet_index": self.packet_index,
            "actions": list(self.actions),
            "reorder_with": self.reorder_with,
            "corrupt_mask": self.corrupt_mask,
        }


@dataclass(frozen=True)
class FaultPlan:
    """A deterministic, per-packet fault manifest."""

    profile: FaultProfile
    packet_count: int
    entries: tuple[FaultEntry, ...]

    def expected_counts(self) -> dict[str, int]:
        counts = {"drop": 0, "duplicate": 0, "reorder": 0, "corrupt": 0}
        for entry in self.entries:
            for action in entry.actions:
                counts[action] += 1
        counts["output_frames"] = self.packet_count + counts["duplicate"] - counts["drop"]
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.profile.as_dict(),
            "packet_count": self.packet_count,
            "expected_counts": self.expected_counts(),
            "entries": [entry.as_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class AppliedFrame:
    """One output frame and its original packet identity after fault injection."""

    source_packet_index: int
    copy_index: int
    frame: bytes
    actions: tuple[str, ...]


def _read_source(source: str | Path | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(source, Mapping):
        return source
    if isinstance(source, Path):
        path = source
    elif isinstance(source, str):
        candidate = Path(source)
        if candidate.is_file():
            path = candidate
        else:
            try:
                document = json.loads(source)
            except json.JSONDecodeError as error:
                raise FaultProfileError(f"profile path does not exist: {source}") from error
            return _require_mapping(document, "profile document")
    else:
        raise FaultProfileError("profile source must be a path, JSON object, or JSON string")

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FaultProfileError(f"cannot read fault profile {path}: {error}") from error
    return _require_mapping(document, "profile document")


def _select_fault_document(
    document: Mapping[str, Any], requested_profile_id: str | None
) -> Mapping[str, Any]:
    if "fault_profiles" in document:
        profiles = _require_mapping(document["fault_profiles"], "fault_profiles")
        if requested_profile_id is None:
            if len(profiles) != 1:
                raise FaultProfileError(
                    "fault_profiles requires --fault-profile-id when it has multiple entries"
                )
            selected = next(iter(profiles.values()))
        else:
            try:
                selected = profiles[requested_profile_id]
            except KeyError as error:
                raise FaultProfileError(
                    f"fault profile {requested_profile_id!r} is not present"
                ) from error
        return _require_mapping(selected, "selected fault profile")

    for field in ("fault", "fault_profile"):
        if field in document:
            selected = _require_mapping(document[field], field)
            if requested_profile_id is not None and selected.get("profile_id") != requested_profile_id:
                raise FaultProfileError(
                    f"{field}.profile_id does not match {requested_profile_id!r}"
                )
            return selected
    return document


def parse_fault_profile(
    source: str | Path | Mapping[str, Any], requested_profile_id: str | None = None
) -> FaultProfile:
    """Parse and validate one frozen P4 fault profile.

    Enabled profiles must use exactly the four periodic intervals from the
    contest contract.  A disabled profile must contain four zero intervals.
    """

    document = _select_fault_document(_read_source(source), requested_profile_id)
    if document.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise FaultProfileError("unsupported fault profile schema_version")
    profile_id = _require_id(document.get("profile_id"), "profile_id")
    seed = _require_int(document.get("seed", 0), "seed", maximum=_U64_MAX)
    enabled = document.get("enabled", True)
    if not isinstance(enabled, bool):
        raise FaultProfileError("enabled must be boolean")
    if document.get("packet_numbering", "one-based") != "one-based":
        raise FaultProfileError("packet_numbering must be one-based")

    raw_rules = _require_mapping(document.get("rules", {}), "rules")
    values: dict[str, int] = {}
    for name in RULE_NAMES:
        values[name] = _require_int(raw_rules.get(name, 0), f"rules.{name}", maximum=MAX_PACKET_COUNT)
    rules = FaultRules(**values)
    expected_rules = FaultRules(drop_every=10, duplicate_every=17, reorder_every=23, corrupt_every=29)
    if enabled and rules != expected_rules:
        raise FaultProfileError("enabled profile rules do not match the frozen P4 intervals")
    if not enabled and any(values.values()):
        raise FaultProfileError("disabled profile must have zero fault intervals")
    if enabled and seed not in VALID_SEEDS:
        raise FaultProfileError("enabled profile seed must be one of 7, 19, or 43")
    if not enabled and seed != 0:
        raise FaultProfileError("disabled profile seed must be zero")

    raw_order = document.get("operation_order", list(OPERATION_ORDER))
    if not isinstance(raw_order, list) or tuple(raw_order) != OPERATION_ORDER:
        raise FaultProfileError("operation_order must be corrupt,reorder,duplicate,drop")
    return FaultProfile(
        profile_id=profile_id,
        seed=seed,
        enabled=enabled,
        rules=rules,
    )


def _corrupt_mask(seed: int, packet_index: int) -> int:
    mixed = (seed ^ (packet_index * 0x9E3779B97F4A7C15)) & _U64_MAX
    mask = ((mixed >> 24) ^ mixed ^ (packet_index * 0xA5)) & 0xFF
    return mask or 0x01


def generate_fault_plan(profile: FaultProfile | str | Path | Mapping[str, Any], packet_count: int) -> FaultPlan:
    """Expand a profile into a deterministic one-based packet plan."""

    if not isinstance(profile, FaultProfile):
        profile = parse_fault_profile(profile)
    packet_count = _require_int(packet_count, "packet_count", maximum=MAX_PACKET_COUNT)
    entries: list[FaultEntry] = []
    for packet_index in range(1, packet_count + 1):
        actions: list[str] = []
        reorder_with: int | None = None
        corrupt_mask: int | None = None
        rules = profile.rules
        if profile.enabled:
            if rules.corrupt_every and packet_index % rules.corrupt_every == 0:
                actions.append("corrupt")
                corrupt_mask = _corrupt_mask(profile.seed, packet_index)
            if rules.reorder_every and packet_index % rules.reorder_every == 0:
                candidate = packet_index + 1
                if candidate <= packet_count:
                    actions.append("reorder")
                    reorder_with = candidate
            if rules.duplicate_every and packet_index % rules.duplicate_every == 0:
                actions.append("duplicate")
            if rules.drop_every and packet_index % rules.drop_every == 0:
                actions.append("drop")
        entries.append(
            FaultEntry(
                packet_index=packet_index,
                actions=tuple(actions),
                reorder_with=reorder_with,
                corrupt_mask=corrupt_mask,
            )
        )
    return FaultPlan(profile=profile, packet_count=packet_count, entries=tuple(entries))


def _corrupt_frame(frame: bytes, seed: int, packet_index: int, mask: int) -> bytes:
    if not frame:
        raise FaultProfileError(f"packet {packet_index} cannot be corrupted because it is empty")
    offset = (seed ^ (packet_index * 0x45D9F3B)) % len(frame)
    mutable = bytearray(frame)
    mutable[offset] ^= mask
    return bytes(mutable)


def apply_fault_plan(frames: Sequence[bytes], plan: FaultPlan) -> tuple[AppliedFrame, ...]:
    """Apply a plan to host-owned frames in the frozen operation order.

    A duplicate is inserted before a later drop removes the original copy.  If
    two rules hit the same packet, this makes the order observable and leaves
    one duplicate as the injected delivery, rather than silently dropping both.
    """

    if len(frames) != plan.packet_count:
        raise FaultProfileError(
            f"frame count {len(frames)} does not match plan packet_count {plan.packet_count}"
        )
    working: list[AppliedFrame] = []
    for entry, frame in zip(plan.entries, frames, strict=True):
        transformed = bytes(frame)
        if "corrupt" in entry.actions:
            if entry.corrupt_mask is None:
                raise FaultProfileError(f"packet {entry.packet_index} has no corruption mask")
            transformed = _corrupt_frame(
                transformed,
                plan.profile.seed,
                entry.packet_index,
                entry.corrupt_mask,
            )
        working.append(
            AppliedFrame(
                source_packet_index=entry.packet_index,
                copy_index=0,
                frame=transformed,
                actions=entry.actions,
            )
        )

    reordered: list[AppliedFrame] = []
    cursor = 0
    while cursor < len(working):
        current = working[cursor]
        entry = plan.entries[current.source_packet_index - 1]
        if entry.reorder_with is not None:
            if cursor + 1 >= len(working):
                raise FaultProfileError(f"packet {entry.packet_index} reorder partner is missing")
            partner = working[cursor + 1]
            if partner.source_packet_index != entry.reorder_with:
                raise FaultProfileError(
                    f"packet {entry.packet_index} reorder partner is not adjacent"
                )
            reordered.extend((partner, current))
            cursor += 2
        else:
            reordered.append(current)
            cursor += 1

    duplicated: list[AppliedFrame] = []
    for frame in reordered:
        duplicated.append(frame)
        entry = plan.entries[frame.source_packet_index - 1]
        if "duplicate" in entry.actions:
            duplicated.append(
                AppliedFrame(
                    source_packet_index=frame.source_packet_index,
                    copy_index=1,
                    frame=frame.frame,
                    actions=frame.actions,
                )
            )

    output = [
        frame
        for frame in duplicated
        if not (
            frame.copy_index == 0
            and "drop" in plan.entries[frame.source_packet_index - 1].actions
        )
    ]
    return tuple(output)


def validate_fault_manifest(
    manifest: Mapping[str, Any],
    *,
    expected_packet_count: int | None = None,
    expected_profile_id: str | None = None,
) -> FaultPlan:
    """Re-generate and byte-compare a supplied fault manifest."""

    document = _require_mapping(manifest, "fault manifest")
    profile = parse_fault_profile(document)
    if expected_profile_id is not None and profile.profile_id != expected_profile_id:
        raise FaultProfileError("fault manifest profile_id does not match the session")
    packet_count = _require_int(document.get("packet_count"), "packet_count", maximum=MAX_PACKET_COUNT)
    if expected_packet_count is not None and packet_count != expected_packet_count:
        raise FaultProfileError("fault manifest packet_count does not match the session")
    generated = generate_fault_plan(profile, packet_count)
    if document != generated.as_dict():
        raise FaultProfileError("fault manifest is not the canonical deterministic plan")
    return generated


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    if path.exists():
        raise FaultProfileError(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--packet-count", required=True, type=int)
    parser.add_argument("--fault-profile-id")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        profile = parse_fault_profile(args.profile, args.fault_profile_id)
        plan = generate_fault_plan(profile, args.packet_count)
        _write_json(args.output, plan.as_dict())
    except (FaultProfileError, OSError) as error:
        print(f"P4 fault profile failed: {error}", file=sys.stderr)
        return 1
    print(
        "P4_FAULT_PROFILE_PASS "
        f"profile={plan.profile.profile_id} packets={plan.packet_count} "
        f"expected={json.dumps(plan.expected_counts(), sort_keys=True)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
