#!/usr/bin/env python3
"""Bind one Linux Image to the VirtIO-console prerequisites for the DMA probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import zlib
from pathlib import Path

from host_vm_carveout_io import publish_new_file, read_regular_bytes


IKCONFIG_START = b"IKCFG_ST"
IKCONFIG_END = b"IKCFG_ED"
MAX_CONFIG_BYTES = 4 * 1024 * 1024
REQUIRED = (
    "CONFIG_VIRTIO",
    "CONFIG_VIRTIO_MMIO",
    "CONFIG_VIRTIO_CONSOLE",
    "CONFIG_HVC_DRIVER",
    "CONFIG_DEVTMPFS",
    "CONFIG_DEVTMPFS_MOUNT",
)


class GuestKernelConfigError(ValueError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _embedded_config(image: bytes) -> tuple[int, bytes]:
    if image.count(IKCONFIG_START) != 1:
        raise GuestKernelConfigError(
            "kernel Image must contain exactly one IKCONFIG start marker"
        )
    offset = image.index(IKCONFIG_START)
    compressed = image[offset + len(IKCONFIG_START) :]
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        config = decoder.decompress(compressed, MAX_CONFIG_BYTES + 1)
    except zlib.error as error:
        raise GuestKernelConfigError(
            f"embedded IKCONFIG gzip is invalid: {error}"
        ) from error
    if len(config) > MAX_CONFIG_BYTES or not decoder.eof:
        raise GuestKernelConfigError(
            "embedded IKCONFIG is truncated or exceeds the size limit"
        )
    if not decoder.unused_data.startswith(IKCONFIG_END):
        raise GuestKernelConfigError("embedded IKCONFIG has no adjacent end marker")
    return offset, config


def _symbols(config: bytes) -> dict[str, str]:
    try:
        text = config.decode("ascii", "strict")
    except UnicodeDecodeError as error:
        raise GuestKernelConfigError("embedded IKCONFIG is not ASCII") from error
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("CONFIG_") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name in values:
            raise GuestKernelConfigError(f"embedded IKCONFIG repeats {name}")
        values[name] = value
    missing = [name for name in REQUIRED if values.get(name) != "y"]
    if missing:
        raise GuestKernelConfigError(
            "kernel lacks built-in DMA-effect control prerequisites: "
            + ", ".join(missing)
        )
    return {name: values[name] for name in REQUIRED}


def verify(*, kernel: Path, output: Path) -> dict[str, object]:
    image = read_regular_bytes(
        kernel, label="Guest Linux kernel", error_type=GuestKernelConfigError
    )
    offset, config = _embedded_config(image)
    required = _symbols(config)
    result: dict[str, object] = {
        "schemaVersion": 1,
        "artifactStatus": "preflight-only",
        "status": "guest_kernel_virtio_console_prerequisites_verified",
        "proofScope": "embedded-linux-ikconfig-built-in-virtio-console-prerequisites",
        "doesNotProve": [
            "the kernel Image was launched",
            "the VirtIO console device was enumerated",
            "/dev/hvc0 appeared",
            "the GO nonce reached the Guest helper",
            "a virtio-blk request completed",
            "DMA isolation",
        ],
        "kernel": {"path": kernel.name, "size": len(image), "sha256": _sha(image)},
        "embeddedConfig": {
            "markerOffset": offset,
            "size": len(config),
            "sha256": _sha(config),
            "requiredBuiltIns": required,
        },
    }
    publish_new_file(
        output,
        (json.dumps(result, indent=2) + "\n").encode("utf-8"),
        error_type=GuestKernelConfigError,
    )
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify built-in Guest Linux VirtIO-console prerequisites"
    )
    parser.add_argument("--kernel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        verify(kernel=args.kernel, output=args.output)
    except (OSError, GuestKernelConfigError) as error:
        print(
            f"Guest kernel VirtIO-console preflight failed: {error}",
            file=__import__("sys").stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
