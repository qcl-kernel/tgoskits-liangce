#!/usr/bin/env python3
"""Generate the deterministic P5 PI-teacher dataset.

The default command emits the frozen 64-episode, 1,800-tick train,
validation, and test splits.  The optional function arguments are only for
small host-side contract fixtures; the CLI keeps the qualification sizes
fixed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_vm_carveout_io import publish_new_file  # noqa: E402

from reference import (
    Pcg32,
    episode_parameters_from_generator,
    generate_teacher_samples,
)


SPLITS = {"train": 7, "validation": 19, "test": 43}
EPISODES_PER_SPLIT = 64
TICKS_PER_EPISODE = 1_800


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_split(
    output_directory: Path,
    split_name: str,
    seed: int,
    episodes: int = EPISODES_PER_SPLIT,
    ticks: int = TICKS_PER_EPISODE,
) -> dict[str, Any]:
    if episodes <= 0 or ticks <= 0:
        raise ValueError("episodes and ticks must be positive")
    output_path = output_directory / f"{split_name}.jsonl"
    generator = Pcg32(seed)
    generator.next_u32()
    sample_count = 0
    with output_path.open("x", encoding="utf-8", newline="\n") as output:
        for episode_index in range(episodes):
            parameters = episode_parameters_from_generator(generator)
            for sample in generate_teacher_samples(parameters, ticks):
                record = {
                    "episode": episode_index,
                    "input": sample["input"],
                    "label": sample["label"],
                    "tick": sample["tick"],
                }
                output.write(canonical_json(record) + "\n")
                sample_count += 1
    return {
        "name": split_name,
        "seed": seed,
        "episodes": episodes,
        "ticks_per_episode": ticks,
        "sample_count": sample_count,
        "path": output_path.name,
        "size_bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
    }


def generate_dataset(
    output_directory: Path,
    episodes: int = EPISODES_PER_SPLIT,
    ticks: int = TICKS_PER_EPISODE,
) -> dict[str, Any]:
    """Write all canonical splits and return the manifest value."""

    output_directory.mkdir(parents=True, exist_ok=True)
    if any(output_directory.iterdir()):
        raise ValueError(f"dataset output directory is not empty: {output_directory}")
    splits = [
        write_split(output_directory, name, seed, episodes, ticks)
        for name, seed in SPLITS.items()
    ]
    manifest = {
        "schema_version": "p5-dataset-v1",
        "generator": "scripts/contest/ai/generate_dataset.py",
        "split_seed_map": SPLITS,
        "episodes_per_split": episodes,
        "ticks_per_episode": ticks,
        "sample_order": "episode-major-tick-minor",
        "splits": splits,
    }
    manifest_path = output_directory / "dataset-manifest.json"
    publish_new_file(
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        error_type=ValueError,
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        generate_dataset(args.output)
    except (OSError, ValueError) as error:
        print(f"P5 dataset generation failed: {error}")
        return 1
    print("P5_DATASET_GENERATION_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
