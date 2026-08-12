#!/usr/bin/env python3
"""Behavioral contract for disposable virtio DMA-effect Guest rootfs preparation."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import struct
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/contest"))
import prepare_disposable_virtio_dma_rootfs as preparer  # noqa: E402


def _ext4_fixture() -> bytes:
    image = bytearray(4096)
    image[1024 + 56:1024 + 58] = b"\x53\xef"
    image[2048:2064] = b"cached-rootfs-v1"
    return bytes(image)


def _static_aarch64_elf_fixture() -> bytes:
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHIQQQIHHHHHH", header, 16, 2, 183, 1, 0x400000, 64, 0, 0, 64, 56, 0, 64, 0, 0)
    return bytes(header)


def main() -> int:
    errors: list[str] = []
    preparer_source = Path(preparer.__file__).read_text(encoding="utf-8")
    for required_fragment in (
        'getattr(os, "O_NOFOLLOW"',
        "_read_stable_helper_bytes",
        "nofollow-single-fd-copy-to-staged-helper",
    ):
        if required_fragment not in preparer_source:
            errors.append(f"preparer omits stable helper input guard `{required_fragment}`")
    results = ROOT / "results"
    made_results = not results.exists()
    results.mkdir(exist_ok=True)
    directory = results / f".disposable-rootfs-contract-{os.getpid()}-{secrets.token_hex(8)}"
    directory.mkdir()
    try:
        source = directory / "cached.ext4"
        helper = directory / "helper.elf"
        output = directory / "fresh.ext4"
        source_bytes = _ext4_fixture()
        helper_bytes = _static_aarch64_elf_fixture()
        source.write_bytes(source_bytes)
        helper.write_bytes(helper_bytes)
        calls: list[tuple[str, bool]] = []

        def fake_runner(_tool: str, _image: Path, command: str, required: bool) -> None:
            calls.append((command, required))

        nonce = "0123456789abcdef0123456789abcdef"
        try:
            plan = preparer.prepare_rootfs(
                source_rootfs=source,
                helper=helper,
                output_rootfs=output,
                nonce=nonce,
                debugfs="debugfs-for-contract-only",
                runner=fake_runner,
                verify_injection=False,
            )
        except (OSError, preparer.DisposableRootfsError) as error:
            errors.append(f"preparer rejects an ext4 source and ELF helper without root/QEMU: {error}")
            plan = {}
        else:
            if source.read_bytes() != source_bytes:
                errors.append("preparer modifies the cached source rootfs")
            if not output.is_file() or output.read_bytes() != source_bytes:
                errors.append("preparer does not publish a byte-copy at a new output path")
            if plan.get("status") != "disposable_virtio_dma_effect_guest_rootfs_prepared":
                errors.append("preparer does not label the artifact as rootfs-only preparation")
            if plan.get("sessionNonce") != nonce:
                errors.append("preparer does not bind the supplied nonce")
            if plan.get("sourceRootfs", {}).get("sha256") != hashlib.sha256(source_bytes).hexdigest():
                errors.append("preparer does not bind the cached source hash")
            if plan.get("outputRootfs", {}).get("copyOnly") is not True:
                errors.append("preparer does not declare a copied disposable output")
            if plan.get("helper", {}).get("sha256") != hashlib.sha256(helper_bytes).hexdigest():
                errors.append("preparer does not bind the helper hash")
            helper_manifest = plan.get("helper", {})
            if helper_manifest.get("format") != "ELF64/AArch64 ET_EXEC" or helper_manifest.get("sourceRead") != "nofollow-single-fd-copy-to-staged-helper":
                errors.append("preparer does not record the stable AArch64 helper input contract")
            if helper_manifest.get("requires") != ["no-PT_INTERP", "PT_DYNAMIC-permitted-only-with-DT_NULL-and-no-DT_NEEDED"]:
                errors.append("preparer does not state its exact PT_DYNAMIC/DT_NEEDED contract")
            if plan.get("init", {}).get("sha256") != hashlib.sha256(preparer._init_bytes(nonce)).hexdigest():
                errors.append("preparer does not bind the generated init hash")
            boot = plan.get("bootContract", {})
            expected_invocation = f"/tgos-dma-effect-helper --nonce {nonce} --device /dev/vdb --control-device /dev/hvc0 --sector 0 --hold-ms 300000"
            expected_wait = {"blockDevice": "/dev/vdb", "controlDevice": "/dev/hvc0", "maxSeconds": 60, "pollSeconds": 1, "timeoutAction": "exit-1"}
            if boot.get("kernelAppend") != "init=/init" or boot.get("device") != "/dev/vdb" or boot.get("controlDevice") != "/dev/hvc0" or boot.get("sector") != 0 or boot.get("deviceWait") != expected_wait or boot.get("helperInvocation") != expected_invocation or boot.get("helperForeground") is not True or boot.get("guestConsoleOwnsControl") is not True or boot.get("competingShell") is not False:
                errors.append("preparer does not state the independent /dev/hvc0 control-device boot contract")
            init_text = preparer._init_bytes(nonce).decode("utf-8")
            for required_fragment in (
                'while { [ ! -b /dev/vdb ] || [ ! -c /dev/hvc0 ]; } && [ "$device_wait" -lt 60 ]; do',
                'TGOS_DMA_EFFECT_HELPER_DEVICE_TIMEOUT',
                'exec /tgos-dma-effect-helper --nonce',
                '--control-device /dev/hvc0',
                '--hold-ms 300000',
            ):
                if required_fragment not in init_text:
                    errors.append(f"generated init is missing `{required_fragment}`")
            if "&\n" in init_text or any(line.strip() == "/bin/sh" for line in init_text.splitlines()):
                errors.append("generated init starts a competing background helper or shell")
            required_operations = [
                ("rm /tgos-dma-effect-helper", False),
                ("rm /init", False),
                ("write ", True),
                ("sif /tgos-dma-effect-helper mode 0100755", True),
                ("write ", True),
                ("sif /init mode 0100755", True),
            ]
            if len(calls) != len(required_operations) or any(not command.startswith(prefix) or required != expected for (command, required), (prefix, expected) in zip(calls, required_operations)):
                errors.append("preparer does not limit debugfs to remove/write/set-inode-mode of the two disposable guest paths")
            if "DMA isolation" not in plan.get("doesNotProve", []):
                errors.append("preparer omits its DMA-isolation evidence boundary")
            try:
                preparer.prepare_rootfs(
                    source_rootfs=source, helper=helper, output_rootfs=output, nonce=nonce,
                    debugfs="debugfs-for-contract-only", runner=fake_runner, verify_injection=False,
                )
            except preparer.DisposableRootfsError:
                pass
            else:
                errors.append("preparer overwrites an existing output rootfs")

        bad_source = directory / "not-ext4.img"
        bad_source.write_bytes(b"not an ext4 image")
        try:
            preparer.prepare_rootfs(
                source_rootfs=bad_source, helper=helper, output_rootfs=directory / "bad-output.ext4", nonce=nonce,
                debugfs="debugfs-for-contract-only", runner=fake_runner, verify_injection=False,
            )
        except preparer.DisposableRootfsError:
            pass
        else:
            errors.append("preparer accepts a non-ext4 source")

        script_helper = directory / "helper.sh"
        script_helper.write_text("#!/bin/sh\n", encoding="utf-8")
        try:
            preparer.prepare_rootfs(
                source_rootfs=source, helper=script_helper, output_rootfs=directory / "script-output.ext4", nonce=nonce,
                debugfs="debugfs-for-contract-only", runner=fake_runner, verify_injection=False,
            )
        except preparer.DisposableRootfsError:
            pass
        else:
            errors.append("preparer accepts a non-ELF helper")

        pie_helper = directory / "helper-pie.elf"
        pie_bytes = bytearray(_static_aarch64_elf_fixture())
        struct.pack_into("<H", pie_bytes, 16, 3)
        pie_helper.write_bytes(pie_bytes)
        try:
            preparer.prepare_rootfs(
                source_rootfs=source, helper=pie_helper, output_rootfs=directory / "pie-output.ext4", nonce=nonce,
                debugfs="debugfs-for-contract-only", runner=fake_runner, verify_injection=False,
            )
        except preparer.DisposableRootfsError:
            pass
        else:
            errors.append("preparer accepts an ET_DYN static-PIE helper")

        interp_helper = directory / "helper-interp.elf"
        interp_bytes = bytearray(_static_aarch64_elf_fixture() + bytes(56))
        struct.pack_into("<H", interp_bytes, 56, 1)
        struct.pack_into("<I", interp_bytes, 64, 3)
        interp_helper.write_bytes(interp_bytes)
        try:
            preparer.prepare_rootfs(
                source_rootfs=source, helper=interp_helper, output_rootfs=directory / "interp-output.ext4", nonce=nonce,
                debugfs="debugfs-for-contract-only", runner=fake_runner, verify_injection=False,
            )
        except preparer.DisposableRootfsError:
            pass
        else:
            errors.append("preparer accepts a helper with PT_INTERP")

        needed_helper = directory / "helper-needed.elf"
        needed_bytes = bytearray(_static_aarch64_elf_fixture() + bytes(56 + 16))
        struct.pack_into("<H", needed_bytes, 56, 1)
        struct.pack_into("<I", needed_bytes, 64, 2)
        struct.pack_into("<QQ", needed_bytes, 72, 120, 16)
        struct.pack_into("<qQ", needed_bytes, 120, 1, 0)
        needed_helper.write_bytes(needed_bytes)
        try:
            preparer.prepare_rootfs(
                source_rootfs=source, helper=needed_helper, output_rootfs=directory / "needed-output.ext4", nonce=nonce,
                debugfs="debugfs-for-contract-only", runner=fake_runner, verify_injection=False,
            )
        except preparer.DisposableRootfsError:
            pass
        else:
            errors.append("preparer accepts a helper with DT_NEEDED")

        unterminated_dynamic_helper = directory / "helper-unterminated-dynamic.elf"
        unterminated_dynamic = bytearray(_static_aarch64_elf_fixture() + bytes(56 + 16))
        struct.pack_into("<H", unterminated_dynamic, 56, 1)
        struct.pack_into("<I", unterminated_dynamic, 64, 2)
        struct.pack_into("<QQ", unterminated_dynamic, 72, 120, 16)
        struct.pack_into("<qQ", unterminated_dynamic, 120, 5, 0)
        unterminated_dynamic_helper.write_bytes(unterminated_dynamic)
        try:
            preparer.prepare_rootfs(
                source_rootfs=source, helper=unterminated_dynamic_helper,
                output_rootfs=directory / "unterminated-dynamic-output.ext4", nonce=nonce,
                debugfs="debugfs-for-contract-only", runner=fake_runner, verify_injection=False,
            )
        except preparer.DisposableRootfsError:
            pass
        else:
            errors.append("preparer accepts PT_DYNAMIC without DT_NULL")

        mutable_helper = directory / "mutable-helper.elf"
        mutable_helper.write_bytes(helper_bytes)
        mutable_output = directory / "mutable-output.ext4"
        mutated = False

        def mutate_source_after_stage(_tool: str, _image: Path, _command: str, _required: bool) -> None:
            nonlocal mutated
            if not mutated:
                mutable_helper.write_bytes(b"mutated-after-staging")
                mutated = True

        try:
            stable_plan = preparer.prepare_rootfs(
                source_rootfs=source, helper=mutable_helper, output_rootfs=mutable_output, nonce=nonce,
                debugfs="debugfs-for-contract-only", runner=mutate_source_after_stage, verify_injection=False,
            )
        except preparer.DisposableRootfsError as error:
            errors.append(f"preparer does not retain its staged helper after source mutation: {error}")
        else:
            if not mutated or stable_plan.get("helper", {}).get("sha256") != hashlib.sha256(helper_bytes).hexdigest():
                errors.append("preparer manifest is not bound to the stable staged helper bytes")
    finally:
        shutil.rmtree(directory)
        if made_results:
            results.rmdir()
    if errors:
        print("Disposable virtio DMA rootfs contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Disposable virtio DMA rootfs contract passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
