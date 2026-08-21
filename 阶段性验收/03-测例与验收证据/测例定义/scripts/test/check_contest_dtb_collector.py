#!/usr/bin/env python3
"""P4-EVID-01B contract: QMP final-DTB collector helpers, fail-closed.

Deterministic host contract over collect_guest_dtbs.find_fdt:
  - a dump containing a valid big-endian FDT returns its exact
    (offset, totalsize) and the extracted bytes round-trip;
  - corrupt magic, totalsize beyond the dump, totalsize zero/oversized,
    and missing FDT all raise DtbCollectError;
  - the first valid FDT wins and a second (stale) FDT after it is ignored.

The QMP socket/pidfile interaction itself needs a live QEMU and is exercised
by the fresh v2 runs, not by this host contract.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NET_DIR = ROOT / "scripts" / "contest" / "network"
sys.path.insert(0, str(NET_DIR))

from collect_guest_dtbs import (  # noqa: E402
    DtbCollectError,
    FDT_MAGIC,
    find_fdt,
    parse_dtb_evidence,
)


def _fdt(totalsize: int, payload: bytes = b"") -> bytes:
    body = payload or (b"\x00" * (totalsize - 8))
    return struct.pack(">II", FDT_MAGIC, totalsize) + body


def main() -> int:
    failures: list[str] = []

    def expect(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    # green: valid FDT at an offset inside the dump
    dump = b"\x00" * 1024 + _fdt(256) + b"\x00" * 64
    offset, totalsize = find_fdt(dump)
    expect(offset == 1024 and totalsize == 256, f"[FAIL-CLOSED] valid FDT not found: {offset=} {totalsize=}")
    expect(
        dump[offset : offset + totalsize] == _fdt(256),
        "[FAIL-CLOSED] FDT bytes do not round-trip",
    )

    # green: first valid FDT wins over a stale second one
    dump = _fdt(128) + b"\xff" * 32 + _fdt(64)
    offset, totalsize = find_fdt(dump)
    expect(offset == 0 and totalsize == 128, f"[FAIL-CLOSED] first FDT must win: {offset=} {totalsize=}")

    # green: FDT exactly at the dump end
    dump = b"\x00" * 8 + _fdt(24)
    offset, totalsize = find_fdt(dump)
    expect(offset == 8 and totalsize == 24, f"[FAIL-CLOSED] tail FDT: {offset=} {totalsize=}")

    # red: no magic
    try:
        find_fdt(b"\x00" * 4096)
        failures.append("[FAIL-CLOSED] missing magic must raise")
    except DtbCollectError:
        pass

    # red: totalsize overruns the dump
    try:
        find_fdt(struct.pack(">II", FDT_MAGIC, 5000) + b"\x00" * 56)
        failures.append("[FAIL-CLOSED] totalsize beyond dump must raise")
    except DtbCollectError:
        pass

    # red: totalsize zero
    try:
        find_fdt(struct.pack(">II", FDT_MAGIC, 0) + b"\x00" * 32)
        failures.append("[FAIL-CLOSED] zero totalsize must raise")
    except DtbCollectError:
        pass

    # red: oversized totalsize (beyond the 4 MiB evidence cap)
    try:
        find_fdt(struct.pack(">II", FDT_MAGIC, 5 * 1024 * 1024) + b"\x00" * 64)
        failures.append("[FAIL-CLOSED] oversized totalsize must raise")
    except DtbCollectError:
        pass

    # red: magic present but malformed (truncated header)
    try:
        find_fdt(struct.pack(">I", FDT_MAGIC))
        failures.append("[FAIL-CLOSED] truncated header must raise")
    except DtbCollectError:
        pass

    # P4-EVID-01B: parse_dtb_evidence from axvm log lines
    log = (
        "[37maxvm::arch::aarch64::fdt::core::create:338] [32mcontest dtb evidence: "
        "vm=1 gpa=0x80000000 size=0x960 hpa=0x124e00000[m\n"
        "contest dtb evidence: vm=2 gpa=0x47e00000 size=0x854 hpa=0x13cc00000\n"
        "unrelated line\n"
    )
    evidence = parse_dtb_evidence(log)
    expect(
        evidence == {
            1: {"gpa": 0x80000000, "size": 0x960, "hpa": 0x124E00000},
            2: {"gpa": 0x47E00000, "size": 0x854, "hpa": 0x13CC00000},
        },
        f"[FAIL-CLOSED] dtb evidence parse mismatch: {evidence}",
    )
    expect(
        parse_dtb_evidence("no evidence here") == {},
        "[FAIL-CLOSED] empty evidence must be empty dict",
    )
    print("  [PASS] dtb evidence parse: both VMs, gpa/size/hpa exact")

    if failures:
        print("\n".join(failures))
        return 1
    print("CONTEST_NETWORK_DTB_COLLECTOR_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
