#!/usr/bin/env python3
"""Validate the self-contained contest development-document mirror."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIRROR = ROOT / "docs" / "docs" / "development"
TOP_LEVEL = (
    "aarch64-dual-guest-console-isolation.md",
    "aarch64-dual-guest-fdt-filtering.md",
    "aarch64-passthrough-spi-route-rollback.md",
    "axvisor-cross-vm-resource-claims.md",
    "axvisor-fixed-hpa-allocator-reservation.md",
    "contest-commit-plan.md",
    "contest-developer-guide.md",
    "contest-icpc-protocol.md",
    "contest-realtime-path.md",
    "contest-repository-submission.md",
    "contest-stage-p2-platform.md",
    "contest-stage-p3-realtime.md",
    "contest-stage-p4-network.md",
    "contest-stage-p5-ai-control.md",
)
SPEC = (
    "architecture.md",
    "contracts.md",
    "deliverables.md",
    "README.md",
    "requirements.md",
    "sources-and-decisions.md",
    "test-matrix.md",
    "traceability.md",
    "work-packages.md",
)
REFERENCE = (
    "arceos.md",
    "axvisor.md",
    "components.md",
    "starryos.md",
    "starryos-signal-extension-syscalls.md",
)


def require_fragments(path: Path, fragments: tuple[str, ...], errors: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{path.relative_to(ROOT)} is missing {fragment!r}")


def main() -> int:
    expected = [MIRROR / name for name in TOP_LEVEL]
    expected.extend(MIRROR / "contest-spec" / name for name in SPEC)
    expected.extend(MIRROR / name for name in REFERENCE)
    errors: list[str] = []
    for path in expected:
        if not path.is_file() or path.is_symlink():
            errors.append(f"missing regular document: {path.relative_to(ROOT)}")

    if not errors:
        require_fragments(
            MIRROR / "contest-developer-guide.md",
            (
                "P2-DMA-01",
                "P3-RT-01",
                "P4-UPSTREAM-01",
                "P5-AI-01",
                "P6-QUAL-01",
                "P7-DELIVER-01",
                "禁止创建或提交 PR",
            ),
            errors,
        )
        require_fragments(
            MIRROR / "contest-stage-p4-network.md",
            (
                "23a07bfcf17863a5eeaae7536d7c0a55c898f50e",
                "contest/upstream-20260817",
                "c8a6b86de",
                "84f10cfcc",
                "ResourceRequest::Auto",
                "不构成 Guest NIC/IP/runtime",
            ),
            errors,
        )
        require_fragments(
            MIRROR / "contest-spec" / "sources-and-decisions.md",
            ("DEC-010", "P4-UPSTREAM-01", "axvirtio-net"),
            errors,
        )

    if errors:
        print("Contest development mirror contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"CONTEST_DEVELOPMENT_MIRROR_PASS documents={len(expected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
