#!/usr/bin/env python3
"""P4-UPSTREAM-01 / X-P4-RES-001 source contract.

The official virtio-net model in `os/axvisor/src/virtio_net.rs` must request
`ResourceRequest::Auto` for both its MMIO window and its wired IRQ, so the
resolved AArch64 resource graph avoids the Linux passthrough root block
(Guest MMIO 0x0a000000 / INTID 48). It must not pin the old fixed values and
must not register a second network backend.

This is a deterministic host-side contract. The behavioral regression lives in
`virtualization/axdevice/tests/resource_planning.rs`
(`fixed_linux_root_block_and_auto_virtio_net_coexist`); the AArch64 AxBuild
proves the production code compiles. None of these artifacts alone is a
Guest-IP or runtime network claim.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
NET_RS = ROOT / "os" / "axvisor" / "src" / "virtio_net.rs"
REGRESSION_RS = ROOT / "virtualization" / "axdevice" / "tests" / "resource_planning.rs"
REGRESSION_TEST = "fixed_linux_root_block_and_auto_virtio_net_coexist"

PASS_TOKEN = "AXVISOR_VIRTIO_NET_AUTO_RESOURCES_PASS"
MMIO_SIZE = 0x200
OLD_FIXED_MMIO = "0x0a00_0000"
OLD_FIXED_IRQ = "ControllerInputId::new(48)"


def main() -> int:
    problems: list[str] = []
    src = NET_RS.read_text(encoding="utf-8")

    # 1) The model requirements must declare Auto for both resource slots.
    req_body = re.search(r"fn requirements\(&self\).*?\{(.*?)\n    \}", src, re.S)
    if req_body is None:
        problems.append("virtio_net.rs: `requirements()` body not found")
    else:
        body = req_body.group(1)
        auto_count = body.count("ResourceRequest::Auto")
        if auto_count < 2:
            problems.append(
                f"virtio_net.rs: `requirements()` must declare Auto for MMIO and wired IRQ (found {auto_count})"
            )
        if OLD_FIXED_MMIO in body:
            problems.append(f"virtio_net.rs: stale fixed MMIO {OLD_FIXED_MMIO} still pinned")
        if OLD_FIXED_IRQ in body:
            problems.append(f"virtio_net.rs: stale fixed IRQ {OLD_FIXED_IRQ} still pinned")

    # 2) The file must not pin the old fixed values anywhere else.
    for needle in (OLD_FIXED_MMIO, OLD_FIXED_IRQ, "Fixed(48)"):
        if needle in src:
            problems.append(f"virtio_net.rs: forbidden fixed resource `{needle}` present")

    # 3) Exactly one network model registration, with the official model name.
    registrations = re.findall(r"pub const REGISTRATION: ConfiguredModelRegistration", src)
    if len(registrations) != 1:
        problems.append(
            f"virtio_net.rs: expected exactly one ConfiguredModelRegistration, found {len(registrations)}"
        )
    if "model: \"virtio-net\"" not in src:
        problems.append("virtio_net.rs: model name must be `virtio-net`")
    for forbidden in ("contest-virtio-net", "virtio-net-contest"):
        if forbidden in src:
            problems.append(f"virtio_net.rs: second/renamed backend `{forbidden}` forbidden")

    # 4) MMIO window size must stay 0x200.
    mmio_size = re.search(r"const MMIO_SIZE:\s*u64\s*=\s*0x([0-9a-fA-F]+)", src)
    if mmio_size is None or int(mmio_size.group(1), 16) != MMIO_SIZE:
        problems.append(f"virtio_net.rs: MMIO_SIZE must be 0x{MMIO_SIZE:x}")

    # 5) The behavioral regression must exist in the axdevice host test.
    regression = REGRESSION_RS.read_text(encoding="utf-8")
    if REGRESSION_TEST not in regression:
        problems.append(f"resource_planning.rs: regression test `{REGRESSION_TEST}` missing")
    if "ResourceRequest::Auto" not in regression or "ResourceRequest::Fixed" not in regression:
        problems.append("resource_planning.rs: regression must cover both Auto and Fixed paths")

    if problems:
        print("AXVISOR_VIRTIO_NET_AUTO_RESOURCES_FAIL")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(PASS_TOKEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
