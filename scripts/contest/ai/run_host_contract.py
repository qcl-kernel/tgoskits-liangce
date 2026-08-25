#!/usr/bin/env python3
"""CLI entry point for the P5 deterministic host contract candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from host_orchestration import HostContractConfig, HostContractError, HostIdentity, run_host_contract


def _parser() -> argparse.ArgumentParser:
    repository = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Run the non-blocking P5 1800-cycle host/static contract (not Guest runtime)."
    )
    parser.add_argument("--repository", type=Path, default=repository)
    parser.add_argument(
        "--profile",
        type=Path,
        default=repository / "configs" / "contest" / "ai" / "qualification-v1.json",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=repository / "apps" / "contest" / "linux-ai-controller" / "model" / "model.bin",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True, type=int)
    parser.add_argument("--controller", choices=("fixed", "mlp"), required=True)
    parser.add_argument("--seed", choices=(7, 19, 43), required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        identity = HostIdentity(
            run_id=args.run_id,
            session_id=args.session_id,
            controller=args.controller,
            seed=args.seed,
        )
        config = HostContractConfig.from_paths(
            identity=identity,
            profile_path=args.profile,
            model_path=args.model,
        )
        run = run_host_contract(config)
        bundle = run.to_bundle(command=(sys.executable, *sys.argv), cwd=args.repository)
        bundle.write(args.output_dir)
    except (HostContractError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"P5_HOST_CONTRACT_BLOCKED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(run.summary(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI smoke is external.
    raise SystemExit(main())
