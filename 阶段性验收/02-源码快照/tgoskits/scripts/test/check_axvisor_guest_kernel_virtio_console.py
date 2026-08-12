#!/usr/bin/env python3
"""Contract tests for the Guest kernel VirtIO-console preflight."""

from __future__ import annotations

import gzip
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/contest/verify_guest_kernel_virtio_console.py"
REQUIRED = (
    "CONFIG_VIRTIO",
    "CONFIG_VIRTIO_MMIO",
    "CONFIG_VIRTIO_CONSOLE",
    "CONFIG_HVC_DRIVER",
    "CONFIG_DEVTMPFS",
    "CONFIG_DEVTMPFS_MOUNT",
)


def _image(lines: list[str]) -> bytes:
    config = ("\n".join(lines) + "\n").encode("ascii")
    return (
        b"arm64-prefix" + b"IKCFG_ST" + gzip.compress(config, mtime=0) + b"IKCFG_EDtail"
    )


def _run(kernel: Path, output: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--kernel", str(kernel), "--output", str(output)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def main() -> int:
    errors: list[str] = []
    directory = (
        ROOT
        / "results"
        / f".kernel-console-contract-{os.getpid()}-{secrets.token_hex(4)}"
    )
    directory.mkdir(parents=True)
    try:
        kernel = directory / "Image"
        output = directory / "verified.json"
        kernel.write_bytes(_image([f"{name}=y" for name in REQUIRED]))
        if _run(kernel, output).returncode != 0:
            errors.append("valid built-in VirtIO-console prerequisites are rejected")
        else:
            result = json.loads(output.read_text(encoding="utf-8"))
            required = result.get("embeddedConfig", {}).get("requiredBuiltIns")
            if required != {name: "y" for name in REQUIRED}:
                errors.append("manifest does not bind every required built-in symbol")
            if (
                result.get("status")
                != "guest_kernel_virtio_console_prerequisites_verified"
            ):
                errors.append("manifest status is not fail-closed and canonical")

        if _run(kernel, output).returncode == 0:
            errors.append("preflight overwrites an existing manifest")

        missing = directory / "missing.Image"
        missing.write_bytes(
            _image(
                [f"{name}=y" for name in REQUIRED if name != "CONFIG_VIRTIO_CONSOLE"]
            )
        )
        if _run(missing, directory / "missing.json").returncode == 0:
            errors.append("preflight accepts a kernel without CONFIG_VIRTIO_CONSOLE=y")

        module = directory / "module.Image"
        module.write_bytes(
            _image(
                [
                    f"{name}={'m' if name == 'CONFIG_VIRTIO_CONSOLE' else 'y'}"
                    for name in REQUIRED
                ]
            )
        )
        if _run(module, directory / "module.json").returncode == 0:
            errors.append(
                "preflight accepts a modular control driver for early /dev/hvc0"
            )

        duplicate = directory / "duplicate.Image"
        duplicate.write_bytes(
            _image([f"{name}=y" for name in REQUIRED] + ["CONFIG_VIRTIO=y"])
        )
        if _run(duplicate, directory / "duplicate.json").returncode == 0:
            errors.append("preflight accepts duplicate required symbols")

        markers = directory / "markers.Image"
        markers.write_bytes(kernel.read_bytes() + kernel.read_bytes())
        if _run(markers, directory / "markers.json").returncode == 0:
            errors.append("preflight accepts ambiguous IKCONFIG start markers")
    finally:
        shutil.rmtree(directory)
    if errors:
        print(
            "Guest kernel VirtIO-console contract failed:",
            *errors,
            sep="\n  - ",
            file=sys.stderr,
        )
        return 1
    print("Guest kernel VirtIO-console contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
