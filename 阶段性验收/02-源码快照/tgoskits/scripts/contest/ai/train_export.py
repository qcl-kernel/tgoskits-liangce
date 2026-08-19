#!/usr/bin/env python3
"""Train and export the deterministic P5 float32 MLP on host data."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
from typing import Any

import numpy as np

from model import (
    infer_duty_q16_16,
    model_bytes,
    model_sha256,
    model_version_from_sha256,
)
from reference import Pcg32


EPOCHS = 200
BATCH_SIZE = 256
LEARNING_RATE = np.float32(0.001)
BETA_1 = np.float32(0.9)
BETA_2 = np.float32(0.999)
EPSILON = np.float32(1e-8)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_split(path: Path) -> tuple[np.ndarray, np.ndarray]:
    raw_inputs: list[list[int]] = []
    raw_labels: list[int] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                raise ValueError(f"{path}:{line_number}: blank line")
            record = json.loads(line)
            values = record.get("input")
            label = record.get("label")
            if (
                not isinstance(values, list)
                or len(values) != 3
                or not all(isinstance(value, int) and not isinstance(value, bool) for value in values)
                or not isinstance(label, int)
                or isinstance(label, bool)
                or not 0 <= label <= 65_536
            ):
                raise ValueError(f"{path}:{line_number}: invalid dataset record")
            raw_inputs.append(values)
            raw_labels.append(label)
    if not raw_inputs:
        raise ValueError(f"empty dataset split: {path}")
    inputs = np.asarray(raw_inputs, dtype=np.float32)
    inputs[:, 0] = np.clip((inputs[:, 0] - 25_000.0) / 40_000.0, -1.0, 1.0)
    inputs[:, 1] = np.clip((inputs[:, 1] - 25_000.0) / 40_000.0, -1.0, 1.0)
    inputs[:, 2] = np.clip(2.0 * inputs[:, 2] / 65_536.0 - 1.0, -1.0, 1.0)
    labels = np.asarray(raw_labels, dtype=np.float32).reshape(-1, 1) / np.float32(65_536.0)
    return inputs, labels


def initialize_weights() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    generator = Pcg32(20_260_813)
    generator.next_u32()
    values = [(generator.next_u32() + 0.5) / 4_294_967_296.0 for _ in range(32)]
    w1_limit = float(np.sqrt(6.0 / (3.0 + 8.0)))
    w2_limit = float(np.sqrt(6.0 / (8.0 + 1.0)))
    w1 = np.asarray(
        [value * 2.0 * w1_limit - w1_limit for value in values[:24]],
        dtype=np.float32,
    ).reshape(8, 3)
    b1 = np.zeros(8, dtype=np.float32)
    w2 = np.asarray(
        [value * 2.0 * w2_limit - w2_limit for value in values[24:]],
        dtype=np.float32,
    ).reshape(1, 8)
    b2 = np.zeros(1, dtype=np.float32)
    return w1, b1, w2, b2


def forward(
    weights: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    inputs: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    w1, b1, w2, b2 = weights
    hidden_pre = np.matmul(inputs, w1.T) + b1
    hidden = np.maximum(hidden_pre, np.float32(0.0))
    output_pre = np.matmul(hidden, w2.T) + b2
    output = np.float32(1.0) / (np.float32(1.0) + np.exp(-output_pre))
    return hidden_pre, hidden, output


def mse(weights: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray], inputs: np.ndarray, labels: np.ndarray) -> float:
    output = forward(weights, inputs)[2]
    return float(np.mean((output - labels) ** 2, dtype=np.float32))


def train(
    inputs: np.ndarray,
    labels: np.ndarray,
    epochs: int = EPOCHS,
    batch_size: int = BATCH_SIZE,
) -> tuple[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray], dict[str, Any]]:
    if epochs <= 0 or batch_size <= 0 or len(inputs) != len(labels):
        raise ValueError("invalid training dimensions")
    if len(inputs) % batch_size != 0:
        raise ValueError("training samples must divide evenly into batches")
    weights = initialize_weights()
    moments = tuple(np.zeros_like(value) for value in weights)
    velocities = tuple(np.zeros_like(value) for value in weights)
    loss_trace: list[float] = []
    step = 0
    for _epoch in range(epochs):
        for offset in range(0, len(inputs), batch_size):
            batch_inputs = inputs[offset : offset + batch_size]
            batch_labels = labels[offset : offset + batch_size]
            hidden_pre, hidden, output = forward(weights, batch_inputs)
            sample_count = np.float32(len(batch_inputs))
            output_gradient = np.float32(2.0) * (output - batch_labels) / sample_count
            w1, b1, w2, b2 = weights
            gradient_w2 = np.matmul(output_gradient.T, hidden)
            gradient_b2 = np.sum(output_gradient, axis=0, dtype=np.float32)
            hidden_gradient = np.matmul(output_gradient, w2) * (hidden_pre > 0.0)
            gradient_w1 = np.matmul(hidden_gradient.T, batch_inputs)
            gradient_b1 = np.sum(hidden_gradient, axis=0, dtype=np.float32)
            gradients = (gradient_w1, gradient_b1, gradient_w2, gradient_b2)
            updated_moments = []
            updated_velocities = []
            updated_weights = []
            step += 1
            correction_1 = np.float32(1.0 - float(BETA_1) ** step)
            correction_2 = np.float32(1.0 - float(BETA_2) ** step)
            for weight, gradient, moment, velocity in zip(
                weights, gradients, moments, velocities
            ):
                next_moment = BETA_1 * moment + (np.float32(1.0) - BETA_1) * gradient
                next_velocity = BETA_2 * velocity + (np.float32(1.0) - BETA_2) * (gradient * gradient)
                moment_hat = next_moment / correction_1
                velocity_hat = next_velocity / correction_2
                next_weight = weight - LEARNING_RATE * moment_hat / (np.sqrt(velocity_hat) + EPSILON)
                if not np.isfinite(next_weight).all():
                    raise ValueError("training produced a non-finite parameter")
                updated_moments.append(next_moment.astype(np.float32, copy=False))
                updated_velocities.append(next_velocity.astype(np.float32, copy=False))
                updated_weights.append(next_weight.astype(np.float32, copy=False))
            weights = tuple(updated_weights)  # type: ignore[assignment]
            moments = tuple(updated_moments)  # type: ignore[assignment]
            velocities = tuple(updated_velocities)  # type: ignore[assignment]
            loss_trace.append(float(np.mean((output - batch_labels) ** 2, dtype=np.float32)))
    return weights, {
        "epochs": epochs,
        "batch_size": batch_size,
        "learning_rate": float(LEARNING_RATE),
        "loss_first_batch": loss_trace[0],
        "loss_last_batch": loss_trace[-1],
        "optimizer": "explicit-float32-adam",
    }


def export_model(
    data_directory: Path,
    output_directory: Path,
    epochs: int = EPOCHS,
    batch_size: int = BATCH_SIZE,
    allow_small: bool = False,
) -> dict[str, Any]:
    if output_directory.exists() and any(output_directory.iterdir()):
        raise ValueError(f"model output directory is not empty: {output_directory}")
    if not allow_small and (epochs != EPOCHS or batch_size != BATCH_SIZE):
        raise ValueError("canonical export requires 200 epochs and batch size 256")
    split_paths = {name: data_directory / f"{name}.jsonl" for name in ("train", "validation", "test")}
    if any(not path.is_file() for path in split_paths.values()):
        raise ValueError("train, validation, and test splits are required")
    train_inputs, train_labels = load_split(split_paths["train"])
    validation_inputs, validation_labels = load_split(split_paths["validation"])
    test_inputs, test_labels = load_split(split_paths["test"])
    if not allow_small and len(train_inputs) != 115_200:
        raise ValueError("canonical train split must contain 115200 samples")
    weights, training = train(train_inputs, train_labels, epochs, batch_size)
    data = model_bytes(weights)
    sha256 = model_sha256(data)
    model_version = model_version_from_sha256(sha256)
    output_directory.mkdir(parents=True, exist_ok=True)
    model_path = output_directory / "model.bin"
    metadata_path = output_directory / "metadata.json"
    golden_path = output_directory / "golden-vectors.json"
    model_path.write_bytes(data)
    golden_records = []
    raw_test_records = []
    with split_paths["test"].open(encoding="utf-8") as source:
        for line in source:
            if len(golden_records) >= 256:
                break
            record = json.loads(line)
            raw_test_records.append(record)
            golden_records.append(
                {
                    "input": record["input"],
                    "expected_duty_q16_16": infer_duty_q16_16(
                        weights, *record["input"]
                    ),
                }
            )
    metadata = {
        "schema_version": "p5-mlp-model-v1",
        "shape": [3, 8, 1],
        "activation": {"hidden": "relu", "output": "sigmoid"},
        "normalization": "IF-008-v1",
        "quantization": "nearest-half-up-q16.16",
        "model_size_bytes": len(data),
        "model_sha256": sha256,
        "model_version": model_version,
        "numpy_version": np.__version__,
        "python_version": platform.python_version(),
        "training": training,
        "dataset_sha256": {name: sha256_file(path) for name, path in split_paths.items()},
        "validation_mse": mse(weights, validation_inputs, validation_labels),
        "test_mse": mse(weights, test_inputs, test_labels),
        "golden_vector_count": len(golden_records),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    golden_path.write_text(json.dumps({"schema_version": "p5-golden-v1", "vectors": golden_records}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksum_lines = []
    for path in (model_path, metadata_path, golden_path):
        checksum_lines.append(f"{sha256_file(path)}  {path.name}")
    (output_directory / "checksums.sha256").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        export_model(args.data, args.output)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"P5 model export failed: {error}")
        return 1
    print("P5_MODEL_EXPORT_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
