#!/usr/bin/env python3
"""Behavioral contract for prepared virtio-blk DMA-effect requests."""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/contest"))
import prepare_virtio_dma_effect_request as preparer  # noqa: E402
import validate_virtio_dma_effect_probe as validator  # noqa: E402


def _run(args: list[str]) -> int:
    with contextlib.redirect_stderr(io.StringIO()):
        return preparer.main(args)


def main() -> int:
    errors: list[str] = []
    results = ROOT / "results"
    made_results = not results.exists()
    results.mkdir(exist_ok=True)
    directory = (
        results / f".prepare-virtio-dma-contract-{os.getpid()}-{secrets.token_hex(8)}"
    )
    directory.mkdir()
    try:
        qemu = directory / "qemu.toml"
        build = directory / "build.toml"
        vm = directory / "vm.toml"
        expected = directory / "expected.bin"
        output = directory / "request.json"
        nonce = "0123456789abcdef0123456789abcdef"
        qemu.write_text(
            "args = ["
            + ", ".join(
                json.dumps(item)
                for item in (
                    "-drive",
                    f"id=dma-probe-disk,if=none,format=raw,readonly=on,file={expected.resolve()}",
                    "-device",
                    "virtio-blk-device,id=linux-block,drive=dma-probe-disk,bus=virtio-mmio-bus.0",
                    "-chardev",
                    "socket,id=dma-go-chardev,path=/tmp/axdma-0123456789abcdef0123456789abcdef/control.sock,server=on,wait=off",
                    "-device",
                    "virtio-serial-device,id=dma-go-serial,bus=virtio-mmio-bus.2",
                    "-device",
                    "virtconsole,id=dma-go-port,chardev=dma-go-chardev,name=dma-go",
                )
            )
            + "]\n",
            encoding="utf-8",
        )
        build.write_text('features = ["ax-driver/virtio-blk"]\n', encoding="utf-8")
        vm.write_text(
            "[base]\nid = 1\n\n[kernel]\nmemory_regions = [[0x180000000, 0x4000000, 0x7, 2]]\n",
            encoding="utf-8",
        )
        expected.write_bytes(bytes(range(256)) * 2)
        common = [
            "--repository",
            str(ROOT),
            "--qemu-config",
            str(qemu),
            "--build-config",
            str(build),
            "--vm-config",
            str(vm),
            "--expected-payload",
            str(expected),
            "--device-id",
            "linux-block",
            "--bus",
            "virtio-mmio-bus.0",
            "--guest-device",
            "/dev/vdb",
            "--drive-id",
            "dma-probe-disk",
            "--sector",
            "8",
            "--payload-hpa",
            "0x180200000",
            "--control-socket",
            "/tmp/axdma-0123456789abcdef0123456789abcdef/control.sock",
            "--guard-hpa",
            "0x180000000",
            "--guard-size",
            "0x200000",
            "--session-nonce",
            nonce,
        ]
        if _run([*common, "--output", str(output)]) != 0:
            errors.append("preparer rejects a matching single virtio-blk request")
        else:
            result = json.loads(output.read_bytes())
            if (
                result.get("status")
                != "single_virtio_blk_read_dma_effect_request_prepared"
            ):
                errors.append("preparer does not emit request-prepared status")
            if (
                result.get("sessionNonce") != nonce
                or result.get("qemuName") != f"axvisor-dma-effect-{nonce}"
            ):
                errors.append(
                    "preparer does not bind the supplied nonce to the QEMU name"
                )
            if result.get("request", {}).get("vmId") != 1:
                errors.append("preparer does not bind VM config identity")
            template = result.get("qmpCaptureTemplate", {})
            if template.get("allowedOperations") != [
                "query-name",
                "query-status",
                "stop",
                "cont",
                "pmemsave",
            ]:
                errors.append("preparer permits a QMP write-capable operation")
            measurements = template.get("measurements")
            if not isinstance(measurements, list) or [
                item.get("outputFileName") for item in measurements
            ] != [
                "before-guard.bin",
                "before-payload.bin",
                "after-guard.bin",
                "after-payload.bin",
            ]:
                errors.append(
                    "preparer does not produce the four fixed capture templates"
                )
            repository = result.get("repository", {})
            if repository.get("worktreeState") not in {"clean", "dirty"} or len(
                repository.get("gitHead", "")
            ) not in {40, 64}:
                errors.append("preparer does not bind repository revision/state")
            try:
                validator._request(
                    result, expected_payload=expected, expected=expected.read_bytes()
                )
            except validator.VirtioDmaEffectError as error:
                errors.append(
                    f"preparer output is rejected by effect validator: {error}"
                )
            if _run([*common, "--output", str(output)]) == 0:
                errors.append("preparer overwrites an existing request output")

        overlap_output = directory / "overlap.json"
        if (
            _run(
                [
                    *common,
                    "--payload-hpa",
                    "0x180000000",
                    "--output",
                    str(overlap_output),
                ]
            )
            == 0
        ):
            errors.append("preparer accepts payload/guard overlap")

        wrong_device_output = directory / "wrong-device.json"
        if (
            _run(
                [
                    *common,
                    "--device-id",
                    "other-block",
                    "--output",
                    str(wrong_device_output),
                ]
            )
            == 0
        ):
            errors.append("preparer accepts a device absent from QEMU config")

        missing_feature_build = directory / "missing-feature.toml"
        missing_feature_build.write_text("features = []\n", encoding="utf-8")
        missing_feature_output = directory / "missing-feature.json"
        if (
            _run(
                [
                    "--repository",
                    str(ROOT),
                    "--qemu-config",
                    str(qemu),
                    "--build-config",
                    str(missing_feature_build),
                    "--vm-config",
                    str(vm),
                    "--expected-payload",
                    str(expected),
                    "--device-id",
                    "linux-block",
                    "--bus",
                    "virtio-mmio-bus.0",
                    "--guest-device",
                    "/dev/vdb",
                    "--drive-id",
                    "dma-probe-disk",
                    "--control-socket",
                    "/tmp/axdma-0123456789abcdef0123456789abcdef/control.sock",
                    "--sector",
                    "8",
                    "--payload-hpa",
                    "0x180200000",
                    "--guard-hpa",
                    "0x180000000",
                    "--guard-size",
                    "0x200000",
                    "--output",
                    str(missing_feature_output),
                ]
            )
            == 0
        ):
            errors.append("preparer accepts a build config without virtio-blk")

        alloc_vm = directory / "alloc-vm.toml"
        alloc_vm.write_text(
            "[base]\nid = 1\n\n[kernel]\nmemory_regions = [[0x180000000, 0x4000000, 0x7, 0]]\n",
            encoding="utf-8",
        )
        alloc_output = directory / "alloc.json"
        if (
            _run(
                [
                    "--repository",
                    str(ROOT),
                    "--qemu-config",
                    str(qemu),
                    "--build-config",
                    str(build),
                    "--vm-config",
                    str(alloc_vm),
                    "--expected-payload",
                    str(expected),
                    "--device-id",
                    "linux-block",
                    "--bus",
                    "virtio-mmio-bus.0",
                    "--guest-device",
                    "/dev/vdb",
                    "--drive-id",
                    "dma-probe-disk",
                    "--control-socket",
                    "/tmp/axdma-0123456789abcdef0123456789abcdef/control.sock",
                    "--sector",
                    "8",
                    "--payload-hpa",
                    "0x180200000",
                    "--guard-hpa",
                    "0x180000000",
                    "--guard-size",
                    "0x200000",
                    "--output",
                    str(alloc_output),
                ]
            )
            == 0
        ):
            errors.append("preparer accepts a MapAlloc payload region")

        short_vm = directory / "short-vm.toml"
        short_vm.write_text(
            "[base]\nid = 1\n\n[kernel]\nmemory_regions = [[0x180200000, 0x200, 0x7, 2]]\n",
            encoding="utf-8",
        )
        cross_output = directory / "cross.json"
        if (
            _run(
                [
                    "--repository",
                    str(ROOT),
                    "--qemu-config",
                    str(qemu),
                    "--build-config",
                    str(build),
                    "--vm-config",
                    str(short_vm),
                    "--expected-payload",
                    str(expected),
                    "--device-id",
                    "linux-block",
                    "--bus",
                    "virtio-mmio-bus.0",
                    "--guest-device",
                    "/dev/vdb",
                    "--drive-id",
                    "dma-probe-disk",
                    "--control-socket",
                    "/tmp/axdma-0123456789abcdef0123456789abcdef/control.sock",
                    "--sector",
                    "8",
                    "--payload-hpa",
                    "0x180201000",
                    "--guard-hpa",
                    "0x180000000",
                    "--guard-size",
                    "0x200000",
                    "--output",
                    str(cross_output),
                ]
            )
            == 0
        ):
            errors.append(
                "preparer accepts a payload extending beyond its MapReserved region"
            )

        ambiguous_vm = directory / "ambiguous-vm.toml"
        ambiguous_vm.write_text(
            "[base]\nid = 1\n\n[kernel]\nmemory_regions = [[0x180000000, 0x4000000, 0x7, 2], [0x180100000, 0x4000000, 0x7, 2]]\n",
            encoding="utf-8",
        )
        ambiguous_output = directory / "ambiguous.json"
        if (
            _run(
                [
                    "--repository",
                    str(ROOT),
                    "--qemu-config",
                    str(qemu),
                    "--build-config",
                    str(build),
                    "--vm-config",
                    str(ambiguous_vm),
                    "--expected-payload",
                    str(expected),
                    "--device-id",
                    "linux-block",
                    "--bus",
                    "virtio-mmio-bus.0",
                    "--guest-device",
                    "/dev/vdb",
                    "--drive-id",
                    "dma-probe-disk",
                    "--control-socket",
                    "/tmp/axdma-0123456789abcdef0123456789abcdef/control.sock",
                    "--sector",
                    "8",
                    "--payload-hpa",
                    "0x180200000",
                    "--guard-hpa",
                    "0x180000000",
                    "--guard-size",
                    "0x200000",
                    "--output",
                    str(ambiguous_output),
                ]
            )
            == 0
        ):
            errors.append(
                "preparer accepts a payload covered by multiple MapReserved regions"
            )
    finally:
        shutil.rmtree(directory)
        if made_results:
            results.rmdir()
    if errors:
        print("Prepared virtio DMA-effect request contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Prepared virtio DMA-effect request contract passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
