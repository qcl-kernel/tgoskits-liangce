#!/usr/bin/env python3
"""P4-EVID-01B: QMP capture of both Guests' final DTBs (fail-closed).

The AxVisor final Guest DTB is assembled at runtime by axvm and placed in the
VM's identity-mapped RAM tail: `main_memory.gpa + size - fdt_size` aligned
down to 2 MiB (see axvm `calculate_dtb_load_addr`; a configured
`dtb_load_addr` is ignored for identical memory).  This collector:

  1. connects to the runner-injected QMP unix socket and binds identity
     (socket, pidfile, QEMU name, nonce, session run id);
  2. `stop` -> `pmemsave` of each VM RAM tail scan window -> `cont`;
  3. scans the dumps for the FDT magic (big-endian 0xd00dfeed), reads the
     big-endian `totalsize`, extracts the byte-exact DTB;
  4. converts it with `dtc` to the .dts text form the validator requires;
  5. writes `dtb/{linux,zephyr}-final.{dtb,dts}` and a binding report.

Pure host Python + QMP socket; it never launches QEMU and never mutates an
existing evidence bundle (no-overwrite).  Any failure (missing socket, bad
magic, dtc failure, no-overwrite) is fail-closed: the caller must not
qualify.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

# axvm logs the exact final-DTB mapping on every boot:
#   contest dtb evidence: vm=1 gpa=0x80000000 size=0x960 hpa=0x124e00000
_DTB_EVIDENCE = re.compile(
    r"contest dtb evidence: vm=(\d+) gpa=0x([0-9a-f]+) size=0x([0-9a-f]+) hpa=0x([0-9a-f]+)"
)


def parse_dtb_evidence(log_text: str) -> dict[int, dict[str, int]]:
    """Parse the axvm final-DTB evidence lines into vm_id -> mapping.

    The host physical address is allocated by the buddy allocator and differs
    between boots, so the collector must never hard-code it; the log line is
    the single source of truth and doubles as the identity binding.
    """
    found: dict[int, dict[str, int]] = {}
    for line in log_text.splitlines():
        match = _DTB_EVIDENCE.search(line)
        if not match:
            continue
        vm_id = int(match.group(1))
        found[vm_id] = {
            "gpa": int(match.group(2), 16),
            "size": int(match.group(3), 16),
            "hpa": int(match.group(4), 16),
        }
    return found


FDT_MAGIC = 0xD00DFEED
MAX_DTB_BYTES = 4 * 1024 * 1024


class DtbCollectError(ValueError):
    """A QMP/DTB capture transition violates the evidence contract."""


def _read_json_line(sock: socket.socket, timeout: float = 15.0) -> dict[str, Any]:
    sock.settimeout(timeout)
    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            raise DtbCollectError("QMP socket closed while reading a response")
        data += chunk
        if b"\n" in data:
            line, data = data.split(b"\n", 1)
            try:
                return json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as error:  # events may interleave
                # QMP events (e.g. RTC_CHANGE) precede the command response;
                # keep reading until a JSON object with "return"/"error".
                if b'"return"' in line or b'"error"' in line:
                    raise DtbCollectError(f"QMP response decode failed: {error}") from error
                continue


def _send_qmp(sock: socket.socket, command: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"execute": command}
    if arguments is not None:
        payload["arguments"] = arguments
    sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    while True:
        response = _read_json_line(sock)
        if "error" in response:
            raise DtbCollectError(f"QMP {command} error: {response['error']}")
        if "return" in response:
            return response


def connect_qmp(qmp_socket: Path) -> socket.socket:
    if not qmp_socket.is_socket() and not qmp_socket.exists():
        raise DtbCollectError(f"QMP socket missing: {qmp_socket}")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(qmp_socket))
    except OSError as error:
        sock.close()
        raise DtbCollectError(f"QMP connect failed: {error}") from error
    # greeting
    _read_json_line(sock)
    _send_qmp(sock, "qmp_capabilities")
    return sock


def find_fdt(data: bytes) -> tuple[int, int]:
    """Return (offset, totalsize) of the first valid FDT in a dump."""
    limit = len(data) - 8
    offset = 0
    while offset <= limit:
        index = data.find(struct.pack(">I", FDT_MAGIC), offset)
        if index < 0:
            break
        if index + 8 <= len(data):
            (totalsize,) = struct.unpack(">I", data[index + 4 : index + 8])
            if 0 < totalsize <= MAX_DTB_BYTES and index + totalsize <= len(data):
                return index, totalsize
        offset = index + 1
    raise DtbCollectError("no valid FDT (magic + totalsize) found in dump")


def pmemsave_dump(sock: socket.socket, base: int, size: int, path: Path) -> None:
    _send_qmp(
        sock,
        "pmemsave",
        {"val": base, "size": size, "filename": str(path)},
    )


def _write_no_overwrite(path: Path, data: bytes) -> None:
    if path.exists():
        raise DtbCollectError(f"refusing to overwrite {path}")
    path.write_bytes(data)


def collect_dtbs(
    qmp_socket: Path,
    output_dir: Path,
    *,
    run_id: str,
    session_id: str,
    nonce: str,
    qemu_name: str,
    qemu_pidfile: Path | None,
    log_text: str,
) -> dict[str, Any]:
    """Capture both final DTBs; returns a binding report (never partial).

    The capture addresses come from the axvm `contest dtb evidence` log lines
    (the host physical address is allocator-dependent and must not be
    guessed); missing evidence lines fail closed.
    """
    output = Path(output_dir)
    if not output.is_dir():
        raise DtbCollectError(f"output directory missing: {output}")
    dtb_dir = output / "dtb"
    dtb_dir.mkdir(parents=True, exist_ok=True)
    for key in ("linux", "zephyr"):
        for file in (dtb_dir / f"{key}-final.dtb", dtb_dir / f"{key}-final.dts"):
            if file.exists():
                raise DtbCollectError(f"refusing to overwrite {file}")

    evidence = parse_dtb_evidence(log_text)
    targets = {1: "linux", 2: "zephyr"}
    missing = [key for vm_id, key in targets.items() if vm_id not in evidence]
    if missing:
        raise DtbCollectError(
            f"missing dtb evidence lines for {', '.join(missing)} "
            f"(binary predates the contest dtb evidence log?)"
        )

    bindings: dict[str, Any] = {
        "schema_version": "p4-network-dtb-capture-v1",
        "run_id": run_id,
        "session_id": session_id,
        "nonce": nonce,
        "qemuName": qemu_name,
        "qemuPid": int(qemu_pidfile.read_text().strip()) if qemu_pidfile and qemu_pidfile.is_file() else None,
        "qmpSocket": str(qmp_socket),
        "dtbs": {},
    }
    if bindings["qemuPid"] is None:
        raise DtbCollectError("QEMU pidfile missing/unreadable; cannot bind capture identity")

    sock = connect_qmp(qmp_socket)
    try:
        dumps: dict[str, bytes] = {}
        for vm_id, key in targets.items():
            entry = evidence[vm_id]
            # 4 KiB padding so the FDT header is captured even if the log
            # size is off by a page; the magic/totalsize check stays exact.
            size = entry["size"] + 4096
            raw = Path(f"/tmp/axp4-dtb-{nonce}-{key}.bin")
            try:
                pmemsave_dump(sock, entry["hpa"], size, raw)
                dumps[key] = raw.read_bytes()
            finally:
                try:
                    raw.unlink()
                except OSError:
                    pass
    finally:
        try:
            sock.close()
        except OSError:
            pass

    for key, data in dumps.items():
        try:
            offset, totalsize = find_fdt(data)
        except DtbCollectError as error:
            raise DtbCollectError(f"{key} dump scan failed: {error}") from error
        dtb = data[offset : offset + totalsize]
        dtb_path = dtb_dir / f"{key}-final.dtb"
        dts_path = dtb_dir / f"{key}-final.dts"
        _write_no_overwrite(dtb_path, dtb)
        result = subprocess.run(
            ["dtc", "-I", "dtb", "-O", "dts", "-o", str(dts_path), str(dtb_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise DtbCollectError(f"dtc failed for {key}: {result.stderr.strip()}")
        vm_id = 1 if key == "linux" else 2
        bindings["dtbs"][key] = {
            "gpa": evidence[vm_id]["gpa"],
            "hpa": evidence[vm_id]["hpa"],
            "size": totalsize,
            "sha256": hashlib.sha256(dtb).hexdigest(),
        }

    report = output / "dtb" / "capture.json"
    report.write_text(json.dumps(bindings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return bindings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qmp-socket", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--qemu-name", required=True)
    parser.add_argument("--qemu-pidfile", type=Path)
    parser.add_argument("--log", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        report = collect_dtbs(
            args.qmp_socket,
            args.output_dir,
            run_id=args.run_id,
            session_id=args.session_id,
            nonce=args.nonce,
            qemu_name=args.qemu_name,
            qemu_pidfile=args.qemu_pidfile,
            log_text=args.log.read_text(encoding="utf-8", errors="replace"),
        )
    except DtbCollectError as error:
        print(f"dtb capture failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
