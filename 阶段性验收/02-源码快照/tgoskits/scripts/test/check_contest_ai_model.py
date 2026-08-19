#!/usr/bin/env python3
"""Contract tests for deterministic P5 model export and verification."""

from __future__ import annotations

import hashlib
import os
import secrets
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "scripts" / "contest" / "ai"
CONTRACT_TEST_ROOT = ROOT / "target" / "contract-tests"
sys.path.insert(0, str(AI_DIR))

import generate_dataset  # noqa: E402
import train_export  # noqa: E402
import verify_model  # noqa: E402


def make_contract_directory(prefix: str) -> Path:
    CONTRACT_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    directory = CONTRACT_TEST_ROOT / f"{prefix}-{os.getpid()}-{secrets.token_hex(8)}"
    directory.mkdir()
    return directory


def main() -> int:
    root = make_contract_directory("p5-model-contract")
    data = root / "data"
    first = root / "first"
    second = root / "second"
    generate_dataset.generate_dataset(data, episodes=2, ticks=5)
    train_export.export_model(data, first, epochs=2, batch_size=2, allow_small=True)
    train_export.export_model(data, second, epochs=2, batch_size=2, allow_small=True)
    assert hashlib.sha256((first / "model.bin").read_bytes()).digest() == hashlib.sha256(
        (second / "model.bin").read_bytes()
    ).digest()
    result = verify_model.verify_model(
        first / "model.bin", first / "metadata.json", first / "golden-vectors.json"
    )
    assert result["valid"] is True
    assert result["vectors_checked"] > 0

    tampered = root / "tampered.bin"
    tampered.write_bytes(bytes([first.joinpath("model.bin").read_bytes()[0] ^ 1]) + first.joinpath("model.bin").read_bytes()[1:])
    try:
        verify_model.verify_model(
            tampered, first / "metadata.json", first / "golden-vectors.json"
        )
    except ValueError:
        pass
    else:
        raise AssertionError("model verifier accepted a tampered model")
    print("P5_MODEL_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"P5 model contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
