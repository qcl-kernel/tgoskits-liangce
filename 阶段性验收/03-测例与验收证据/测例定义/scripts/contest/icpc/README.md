# ICPC v1 reference library

This directory contains the allocation-free C99 reference implementation of
the contest Industrial Control Packet Contract (ICPC). It defines only the
application protocol. Linux and Zephyr must still exchange these datagrams
through the verified VirtIO-net UDP/IP path.

The v1 library provides:

- a fixed 36-byte network-order header and payloads up to 1024 bytes;
- `CONTROL`, `STATUS`, `ERROR`, `ACK`, and `HEARTBEAT` messages;
- strict encode/decode validation and CRC32C integrity checks;
- RFC 1982-style sequence comparison and a 64-packet receive window;
- one outstanding reliable message with 100/200/400 ms bounded retry timing.

Run the host contract test from the repository root:

```bash
python3 scripts/test/check_icpc_protocol.py
```

Set `CC` to select another C99 compiler. `ICPC_TEST_TMPDIR` may select an
alternate writable directory for the temporary executable.

The protocol design, non-goals, field table, and runtime evidence boundary are
documented in `docs/docs/development/contest-icpc-protocol.md`.
