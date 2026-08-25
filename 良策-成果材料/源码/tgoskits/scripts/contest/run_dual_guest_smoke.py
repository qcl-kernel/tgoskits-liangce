#!/usr/bin/env python3
"""Thin f964 P2 dual-Guest entry point.

All process, identity, console and publication behavior belongs to
``runtime.dual_guest``.  This file only parses P2's typed/no-data-plane inputs
and selects a short-smoke or current-f964 soak plan.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from runtime.dual_guest import (
    DualGuestSpec,
    RuntimeContractError,
    build_axvisor_qemu_command,
    validate_no_data_plane_qemu_config,
    validate_no_data_plane_vm_config,
    run_dual_guest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--build-config", required=True, type=Path)
    parser.add_argument("--qemu-config", required=True, type=Path)
    parser.add_argument("--linux-vmconfig", required=True, type=Path)
    parser.add_argument("--zephyr-vmconfig", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--ready-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--shutdown-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate typed inputs and print the fixed launcher argv without launching",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        spec = DualGuestSpec(
            repository=args.repository,
            build_config=args.build_config,
            qemu_config=args.qemu_config,
            linux_vmconfig=args.linux_vmconfig,
            zephyr_vmconfig=args.zephyr_vmconfig,
            run_id=args.run_id,
            output_dir=args.output_dir,
            duration_seconds=args.duration_seconds,
            ready_timeout_seconds=args.ready_timeout_seconds,
            shutdown_timeout_seconds=args.shutdown_timeout_seconds,
        )
        # Let the shared runner perform the complete validation for a live
        # run.  Dry-run repeats only the file/config checks because it must not
        # create an output or /tmp runtime directory.
        if args.dry_run:
            if not spec.repository.is_dir():
                raise RuntimeContractError(f"repository is not a directory: {spec.repository}")
            validate_no_data_plane_qemu_config(args.qemu_config.read_text(encoding="utf-8"))
            validate_no_data_plane_vm_config(
                args.linux_vmconfig.read_text(encoding="utf-8"),
                expected_vm_id=1,
                expected_cpu_ids=(0, 1),
            )
            validate_no_data_plane_vm_config(
                args.zephyr_vmconfig.read_text(encoding="utf-8"),
                expected_vm_id=2,
                expected_cpu_ids=(2,),
            )
            print(json.dumps({
                "mode": "soak" if args.duration_seconds >= 1800 else "short-smoke",
                "argv": build_axvisor_qemu_command(
                    build_config=args.build_config.resolve(),
                    qemu_config=args.qemu_config.resolve(),
                    linux_vmconfig=args.linux_vmconfig.resolve(),
                    zephyr_vmconfig=args.zephyr_vmconfig.resolve(),
                ),
                "qualified": False,
            }, indent=2))
            return 0
        return run_dual_guest(spec)
    except (OSError, RuntimeContractError) as error:
        print(f"dual guest smoke blocked/failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
