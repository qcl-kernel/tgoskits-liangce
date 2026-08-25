#!/usr/bin/env python3
"""P4-UPSYNC-03 / X-P4-RES-001 source contract.

The c82 official virtio-net model lives in
`virtualization/axvm/src/configured/devices/virtio_net.rs`. It must request
`ResourceRequest::Auto` for both its MMIO window and its wired IRQ, so the
resolved AArch64 resource graph avoids the historical Linux root-block
collision (Guest MMIO 0x0a000000 / INTID 48). It must not pin the old fixed
values or leave the pre-c82 AxVisor-owned copy as a second backend.

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
NET_RS = ROOT / "virtualization" / "axvm" / "src" / "configured" / "devices" / "virtio_net.rs"
LEGACY_NET_RS = ROOT / "os" / "axvisor" / "src" / "virtio_net.rs"
LEGACY_BACKEND = ROOT / "virtualization" / "axdevice" / "src" / "virtio_net"
REGRESSION_RS = ROOT / "virtualization" / "axdevice" / "tests" / "resource_planning.rs"
REGRESSION_TEST = "fixed_linux_root_block_and_auto_virtio_net_coexist"

PASS_TOKEN = "AXVISOR_VIRTIO_NET_AUTO_RESOURCES_PASS"
MMIO_SIZE = 0x200
OLD_FIXED_MMIO = "0x0a00_0000"
OLD_FIXED_IRQ = "ControllerInputId::new(48)"


def main() -> int:
    problems: list[str] = []
    if not NET_RS.is_file():
        print("AXVISOR_VIRTIO_NET_AUTO_RESOURCES_FAIL")
        print(f"  - missing c82 production backend: {NET_RS.relative_to(ROOT)}")
        return 1
    src = NET_RS.read_text(encoding="utf-8")

    # 1) The model requirements must declare Auto for both resource slots.
    req_body = re.search(r"fn requirements\(&self\).*?\{(.*?)\n    \}", src, re.S)
    if req_body is None:
        problems.append("configured virtio_net.rs: `requirements()` body not found")
    else:
        body = req_body.group(1)
        auto_count = body.count("ResourceRequest::Auto")
        if auto_count < 2:
            problems.append(
                "configured virtio_net.rs: `requirements()` must declare Auto "
                f"for MMIO and wired IRQ (found {auto_count})"
            )
        if OLD_FIXED_MMIO in body:
            problems.append(f"configured virtio_net.rs: stale fixed MMIO {OLD_FIXED_MMIO} still pinned")
        if OLD_FIXED_IRQ in body:
            problems.append(f"configured virtio_net.rs: stale fixed IRQ {OLD_FIXED_IRQ} still pinned")

    # 2) The file must not pin the old fixed values anywhere else.
    for needle in (OLD_FIXED_MMIO, OLD_FIXED_IRQ, "Fixed(48)"):
        if needle in src:
            problems.append(f"configured virtio_net.rs: forbidden fixed resource `{needle}` present")

    # 3) Exactly one network model registration, with the official model name.
    registrations = re.findall(r"pub const REGISTRATION: ConfiguredModelRegistration", src)
    if len(registrations) != 1:
        problems.append(
            "configured virtio_net.rs: expected exactly one "
            f"ConfiguredModelRegistration, found {len(registrations)}"
        )
    if "model: \"virtio-net\"" not in src:
        problems.append("configured virtio_net.rs: model name must be `virtio-net`")
    for forbidden in ("contest-virtio-net", "virtio-net-contest"):
        if forbidden in src:
            problems.append(f"configured virtio_net.rs: second/renamed backend `{forbidden}` forbidden")

    # 4) MMIO window size must stay 0x200.
    mmio_size = re.search(r"const MMIO_SIZE:\s*u64\s*=\s*0x([0-9a-fA-F]+)", src)
    if mmio_size is None or int(mmio_size.group(1), 16) != MMIO_SIZE:
        problems.append(f"configured virtio_net.rs: MMIO_SIZE must be 0x{MMIO_SIZE:x}")

    # 5) c82 owns the only production backend. The former AxVisor glue and
    # the older contest axdevice implementation must not coexist with it.
    if LEGACY_NET_RS.exists():
        problems.append(
            "legacy os/axvisor/src/virtio_net.rs still exists beside the c82 backend"
        )
    if LEGACY_BACKEND.exists():
        problems.append(
            "legacy virtualization/axdevice/src/virtio_net backend still exists"
        )

    # 6) The behavioral regression must exist in the axdevice host test.
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
