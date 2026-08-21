"""Deterministic float32 MLP model helpers for P5 host tooling."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import numpy as np


MODEL_FLOAT_COUNT = 41
MODEL_BYTE_SIZE = MODEL_FLOAT_COUNT * 4
MODEL_SHAPE = (3, 8, 1)


def normalize_inputs(measured_mC: int, target_mC: int, previous_duty_q16_16: int) -> np.ndarray:
    """Apply the frozen three-feature normalization in float32."""

    values = np.asarray(
        [
            (measured_mC - 25_000) / 40_000.0,
            (target_mC - 25_000) / 40_000.0,
            2.0 * previous_duty_q16_16 / 65_536.0 - 1.0,
        ],
        dtype=np.float32,
    )
    return np.clip(values, np.float32(-1.0), np.float32(1.0)).astype(
        np.float32, copy=False
    )


def model_bytes(weights: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]) -> bytes:
    """Serialize W1, b1, W2, b2 as canonical little-endian float32 bytes."""

    w1, b1, w2, b2 = weights
    arrays = (
        np.asarray(w1, dtype="<f4").reshape(8, 3),
        np.asarray(b1, dtype="<f4").reshape(8),
        np.asarray(w2, dtype="<f4").reshape(1, 8),
        np.asarray(b2, dtype="<f4").reshape(1),
    )
    result = b"".join(array.tobytes(order="C") for array in arrays)
    if len(result) != MODEL_BYTE_SIZE:
        raise ValueError("model serialization does not contain 41 float32 values")
    return result


def model_arrays(model_data: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Deserialize and validate canonical model bytes."""

    if len(model_data) != MODEL_BYTE_SIZE:
        raise ValueError(f"model must be exactly {MODEL_BYTE_SIZE} bytes")
    values = np.frombuffer(model_data, dtype="<f4").astype(np.float32, copy=True)
    if not np.isfinite(values).all():
        raise ValueError("model contains a non-finite weight")
    return (
        values[:24].reshape(8, 3),
        values[24:32],
        values[32:40].reshape(1, 8),
        values[40:41],
    )


def model_sha256(model_data: bytes) -> str:
    return hashlib.sha256(model_data).hexdigest()


def model_version_from_sha256(sha256: str) -> int:
    value = int(sha256[:8], 16)
    if value != 0:
        return value
    value = int(sha256[8:16], 16)
    if value == 0:
        raise ValueError("model hash produced a zero model_version")
    return value


def infer_duty_q16_16(
    weights: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    measured_mC: int,
    target_mC: int,
    previous_duty_q16_16: int,
) -> int:
    """Run the frozen ReLU/sigmoid MLP and quantize its output."""

    w1, b1, w2, b2 = weights
    inputs = normalize_inputs(measured_mC, target_mC, previous_duty_q16_16)
    hidden = np.maximum(np.matmul(inputs, w1.T) + b1, np.float32(0.0))
    output = np.matmul(hidden, w2.T) + b2
    sigmoid = np.float32(1.0) / (np.float32(1.0) + np.exp(-output))
    scalar = float(sigmoid.reshape(-1)[0])
    if not math.isfinite(scalar):
        raise ValueError("MLP output is non-finite")
    return min(65_536, max(0, math.floor(min(max(scalar, 0.0), 1.0) * 65_536.0 + 0.5)))


def load_model(path: Path) -> tuple[bytes, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    data = path.read_bytes()
    return data, model_arrays(data)
