#!/usr/bin/env python3
"""Build a hash-bound, host-only P3 measurement matrix plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_vm_carveout_io import publish_new_file  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - pinned project Python is 3.11+
    import tomli as tomllib


SEEDS = (7, 19, 43)
PROFILE_SCHEMA = "p3-rt-profile-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_gate(value: str) -> tuple[str, Path]:
    test_id, separator, directory = value.partition("=")
    if not separator or not test_id.startswith("TEST-") or not directory:
        raise ValueError(f"invalid --gate value: {value!r}")
    return test_id, Path(directory).resolve()


def read_profile(path: Path) -> dict[str, Any]:
    try:
        profile = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"cannot read profile {path}: {error}") from error
    if profile.get("schema_version") != PROFILE_SCHEMA:
        raise ValueError(f"{path}: unsupported profile schema")
    for field in ("scenario", "mode"):
        if not isinstance(profile.get(field), str) or not profile[field]:
            raise ValueError(f"{path}: {field} must be non-empty")
    if profile["mode"] not in {"native", "discovery", "production-ab"}:
        raise ValueError(f"{path}: invalid mode")
    required_gates = profile.get("required_gates", [])
    if not isinstance(required_gates, list) or any(
        not isinstance(gate, str) or not gate.startswith("TEST-")
        for gate in required_gates
    ):
        raise ValueError(f"{path}: required_gates must contain TEST-* strings")
    if profile.get("period_ms") != 100:
        raise ValueError(f"{path}: period_ms must be 100")
    if profile.get("minimum_samples") != 100000:
        raise ValueError(f"{path}: minimum_samples must be 100000")
    if profile.get("minimum_duration_ns") != 1_800_000_000_000:
        raise ValueError(f"{path}: minimum_duration_ns is not frozen")
    profile["profile_path"] = str(path)
    profile["profile_sha256"] = sha256_file(path)
    return profile


def read_gate(test_id: str, directory: Path) -> dict[str, Any]:
    status_path = directory / "status.json"
    if not status_path.is_file():
        status_path = directory / "live" / "status.json"
    if not status_path.is_file():
        raise ValueError(f"{test_id}: missing status.json under {directory}")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{test_id}: invalid status.json: {error}") from error
    if status.get("success") is not True:
        raise ValueError(f"{test_id}: gate status is not successful")
    return {
        "test_id": test_id,
        "directory": str(directory),
        "status_path": str(status_path),
        "status_sha256": sha256_file(status_path),
    }


def build_matrix(
    mode: str,
    matrix_id: str,
    profiles: list[Path],
    seeds: list[int],
    gates: dict[str, Path],
    native_matrix: Path | None,
    discovery_matrix: Path | None,
    selection: Path | None,
    candidate_layout: str,
) -> dict[str, Any]:
    if not matrix_id or any(character in matrix_id for character in "/\\"):
        raise ValueError("matrix_id must be a safe non-empty identifier")
    if mode not in {"native", "discovery", "production-ab"}:
        raise ValueError("mode must be native, discovery, or production-ab")
    if seeds != list(SEEDS):
        raise ValueError(f"seeds must be exactly {list(SEEDS)}")
    if not profiles:
        raise ValueError("at least one profile is required")
    parsed_profiles = [read_profile(profile) for profile in profiles]
    if any(profile["mode"] != mode for profile in parsed_profiles):
        raise ValueError("profile mode does not match matrix mode")

    gate_records: dict[str, dict[str, Any]] = {}
    for profile in parsed_profiles:
        for test_id in profile["required_gates"]:
            if test_id not in gates:
                raise ValueError(f"{profile['scenario']}: missing gate {test_id}")
            if test_id not in gate_records:
                gate_records[test_id] = read_gate(test_id, gates[test_id])

    references: dict[str, Any] = {}
    if native_matrix is not None:
        references["native_matrix"] = str(native_matrix.resolve())
    if discovery_matrix is not None:
        references["discovery_matrix"] = str(discovery_matrix.resolve())

    selected_paths: list[str] = []
    if selection is not None:
        try:
            selection_value = json.loads(selection.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid selection.json: {error}") from error
        if selection_value.get("schema_version") != "p3-rt-selection-v1":
            raise ValueError("selection schema is not p3-rt-selection-v1")
        selected_paths = selection_value.get("selected_paths", [])
        if not isinstance(selected_paths, list) or any(
            not isinstance(path_id, str) or not path_id for path_id in selected_paths
        ):
            raise ValueError("selection selected_paths is invalid")
        references["selection"] = {
            "path": str(selection.resolve()),
            "sha256": sha256_file(selection),
        }

    runs: list[dict[str, Any]] = []
    for profile in parsed_profiles:
        for seed in seeds:
            base = {
                "profile": profile["scenario"],
                "profile_sha256": profile["profile_sha256"],
                "scenario": profile["scenario"],
                "seed": seed,
                "period_ms": profile["period_ms"],
                "required_gates": profile["required_gates"],
            }
            if mode == "production-ab":
                if not selected_paths:
                    raise ValueError("production-ab requires at least one selected path")
                for path_id in selected_paths:
                    for feature_state in ("off", "on"):
                        runs.append(
                            {
                                **base,
                                "path_id": path_id,
                                "feature_state": feature_state,
                            }
                        )
                if candidate_layout == "separate-plus-combined" and len(selected_paths) > 1:
                    runs.append({**base, "path_id": "combined", "feature_state": "on"})
            else:
                runs.append({**base, "path_id": None, "feature_state": "off"})

    return {
        "schema_version": "p3-rt-matrix-v1",
        "matrix_id": matrix_id,
        "mode": mode,
        "seeds": seeds,
        "profiles": [
            {
                "scenario": profile["scenario"],
                "path": profile["profile_path"],
                "sha256": profile["profile_sha256"],
            }
            for profile in parsed_profiles
        ],
        "gates": list(gate_records.values()),
        "references": references,
        "runs": runs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("native", "discovery", "production-ab"))
    parser.add_argument("--matrix-id", required=True)
    parser.add_argument("--profile", action="append", required=True, type=Path)
    parser.add_argument("--seed", action="append", required=True, type=int)
    parser.add_argument("--gate", action="append", default=[])
    parser.add_argument("--native-matrix", type=Path)
    parser.add_argument("--discovery-matrix", type=Path)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--candidate-layout", default="separate-plus-combined")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output_dir.exists():
            raise ValueError(f"output directory already exists: {args.output_dir}")
        gates = dict(parse_gate(value) for value in args.gate)
        matrix = build_matrix(
            args.mode,
            args.matrix_id,
            args.profile,
            args.seed,
            gates,
            args.native_matrix,
            args.discovery_matrix,
            args.selection,
            args.candidate_layout,
        )
        args.output_dir.mkdir(parents=True)
        publish_new_file(
            args.output_dir / "matrix.json",
            (json.dumps(matrix, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            error_type=ValueError,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"P3 matrix build failed: {error}")
        return 1
    print(f"P3_RT_MATRIX_BUILD_PASS runs={len(matrix['runs'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
