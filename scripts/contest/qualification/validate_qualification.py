#!/usr/bin/env python3
"""Fail-closed P6 validator.

The positional object is deliberately named ``--run`` and must be a directory;
passing ``session.json`` is rejected before any other validation occurs.
"""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from qualification_contract import QualificationError, validate_run_directory, write_json
except ImportError:  # pragma: no cover - package import path
    from .qualification_contract import QualificationError, validate_run_directory, write_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path, help="immutable qualification run directory")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate_run_directory(args.run)
        if args.output is not None:
            write_json(args.output, report)
    except (OSError, QualificationError) as error:
        print(f"P6 qualification validation failed: {error}")
        return 1
    print(f"P6_QUALIFICATION_PASS run={report['run_id']} qualified=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
