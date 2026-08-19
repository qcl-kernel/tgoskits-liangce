#!/usr/bin/env python3
"""Prepare a fresh Zephyr network image for P4 TEST-011..015.

Builds the locked zephyr-control app (Zephyr v4.4.0, SDK 1.0.1, west 1.5.0,
board qemu_cortex_a53) with the vnet0 overlay and a run-scoped boot id and
mode, then publishes a byte-bound manifest.  The build directory is never
reused; existing outputs are rejected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ZEPHYR_REVISION = "684c9e8f32e4373a21098559f748f06915f950c9"
WEST_MANIFEST_SHA256 = "9c3661dd82e5ab7f487e3c0a4eee8726978736eadb470a3a09a76c77a8f10f92"
ZEPHYR_SDK_VERSION = "1.0.1"
WEST_VERSION = "1.5.0"
BOARD = "qemu_cortex_a53"


class ZephyrImageError(ValueError):
    """The supplied environment cannot produce a safe Zephyr image."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(command: list[str], *, env: dict[str, str], check: bool = True) -> None:
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise ZephyrImageError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zephyr-base", required=True, type=Path)
    parser.add_argument("--zephyr-sdk-dir", required=True, type=Path)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--overlay", required=True, type=Path)
    parser.add_argument("--boot-id", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--output-manifest", required=True, type=Path)
    parser.add_argument("--west-venv-bin", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.build_dir.exists() or args.output_dir.exists():
        print("refusing to reuse an existing build or output directory", file=sys.stderr)
        return 1
    if args.output_manifest.exists():
        print(f"refusing to overwrite manifest: {args.output_manifest}", file=sys.stderr)
        return 1
    if len(args.boot_id) > 64 or not all(c.isalnum() or c in "._-" for c in args.boot_id):
        print("--boot-id must match [A-Za-z0-9._-]{1,64}", file=sys.stderr)
        return 1
    if args.mode not in ("l3-smoke", "udp-echo", "tcp-listen"):
        print(f"unsupported mode {args.mode!r}", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env["PATH"] = f"{args.west_venv_bin}:{env.get('PATH', '')}"
    env["ZEPHYR_BASE"] = str(args.zephyr_base)
    env["ZEPHYR_SDK_INSTALL_DIR"] = str(args.zephyr_sdk_dir)

    try:
        args.build_dir.mkdir(parents=True)
        args.output_dir.mkdir(parents=True)
        _run(
            [
                "west", "build", "-b", BOARD, "-d", str(args.build_dir), str(args.app), "--",
                "-DDTC_OVERLAY_FILE=" + str(args.overlay.resolve()),
                f'-DCONFIG_DUAL_BOOT_ID="{args.boot_id}"',
                f'-DCONFIG_CONTEST_NET_MODE="{args.mode}"',
            ],
            env=env,
        )
        zephyr_dir = args.build_dir / "zephyr"
        bin_path = zephyr_dir / "zephyr.bin"
        elf_path = zephyr_dir / "zephyr.elf"
        config_path = zephyr_dir / ".config"
        dts_path = zephyr_dir / "zephyr.dts"
        for required in (bin_path, elf_path, config_path, dts_path):
            if not required.is_file():
                raise ZephyrImageError(f"missing build artifact: {required}")
        _run(["e2fsck", "--version"], env=env, check=False)  # no-op guard

        artifacts = {
            "bin": {"path": "zephyr.bin", "size": bin_path.stat().st_size, "sha256": _sha256_file(bin_path)},
            "elf": {"path": "zephyr.elf", "size": elf_path.stat().st_size, "sha256": _sha256_file(elf_path)},
            "config": {"path": ".config", "size": config_path.stat().st_size, "sha256": _sha256_file(config_path)},
            "dts": {"path": "zephyr.dts", "size": dts_path.stat().st_size, "sha256": _sha256_file(dts_path)},
        }
        for name in ("zephyr.bin", "zephyr.elf", ".config", "zephyr.dts"):
            # `os.link` fails across filesystems (ext4 build dir -> DrvFS
            # output dir); fall back to an ordinary copy.
            try:
                os.link(zephyr_dir / name, args.output_dir / name, follow_symlinks=False)
            except OSError:
                shutil.copy2(zephyr_dir / name, args.output_dir / name)
        manifest = {
            "schemaVersion": 1,
            "artifactStatus": "prepared-zephyr-network-image",
            "status": "zephyr_network_image_prepared",
            "proofScope": "one-locked-zephyr-build-with-vnet0-overlay",
            "doesNotProve": [
                "the image was booted",
                "VirtIO-net frontend registration",
                "IPv4 connectivity",
                "dual-Guest execution",
            ],
            "zephyrRevision": ZEPHYR_REVISION,
            "westManifestSha256": WEST_MANIFEST_SHA256,
            "sdkVersion": ZEPHYR_SDK_VERSION,
            "westVersion": WEST_VERSION,
            "board": BOARD,
            "bootId": args.boot_id,
            "mode": args.mode,
            "overlay": {"path": args.overlay.name, "sha256": _sha256_file(args.overlay)},
            "artifacts": artifacts,
        }
        args.output_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": manifest["status"], "bin": str(bin_path)}, indent=2))
        return 0
    except ZephyrImageError as error:
        print(f"Zephyr network image preparation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
