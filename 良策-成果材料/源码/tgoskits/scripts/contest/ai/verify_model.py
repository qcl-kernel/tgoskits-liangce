#!/usr/bin/env python3
"""Verify a P5 model, metadata, golden vectors, and checksums."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

try:  # Support package imports from validators and direct CLI execution.
    from .model import (
        MODEL_BYTE_SIZE,
        infer_duty_q16_16,
        load_model,
        model_sha256,
        model_version_from_sha256,
    )
except ImportError:  # pragma: no cover - direct CLI entry point.
    from model import (
        MODEL_BYTE_SIZE,
        infer_duty_q16_16,
        load_model,
        model_sha256,
        model_version_from_sha256,
    )


def verify_model(
    model_path: Path,
    metadata_path: Path,
    golden_path: Path,
    *,
    expected_vector_count: int = 256,
) -> dict[str, object]:
    data, weights = load_model(model_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    sha256 = model_sha256(data)
    if len(data) != MODEL_BYTE_SIZE:
        raise ValueError("model byte size is not canonical")
    if metadata.get("schema_version") != "p5-mlp-model-v1":
        raise ValueError("metadata schema is not p5-mlp-model-v1")
    if metadata.get("shape") != [3, 8, 1]:
        raise ValueError("model shape is not 3x8x1")
    if metadata.get("model_size_bytes") != MODEL_BYTE_SIZE:
        raise ValueError("metadata model size is incorrect")
    if metadata.get("model_sha256") != sha256:
        raise ValueError("metadata model hash does not match model.bin")
    if metadata.get("model_version") != model_version_from_sha256(sha256):
        raise ValueError("metadata model version does not match model hash")
    if golden.get("schema_version") != "p5-golden-v1":
        raise ValueError("golden vector schema is invalid")
    vectors = golden.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != expected_vector_count:
        raise ValueError(
            f"golden vectors must contain exactly {expected_vector_count} records"
        )
    if metadata.get("golden_vector_count") != expected_vector_count:
        raise ValueError("metadata golden vector count is not canonical")
    checked = 0
    for vector in vectors:
        raw_input = vector.get("input")
        expected = vector.get("expected_duty_q16_16")
        if (
            not isinstance(raw_input, list)
            or len(raw_input) != 3
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in raw_input)
            or not isinstance(expected, int)
        ):
            raise ValueError("invalid golden vector")
        actual = infer_duty_q16_16(weights, *raw_input)
        if abs(actual - expected) > 2:
            raise ValueError(
                f"golden vector differs by more than 2 LSB: {actual} != {expected}"
            )
        checked += 1
    return {"valid": True, "model_sha256": sha256, "vectors_checked": checked}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--golden", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_model(args.model, args.metadata, args.golden)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"P5 model verification failed: {error}")
        return 1
    print(f"P5_MODEL_VERIFY_PASS vectors={result['vectors_checked']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
