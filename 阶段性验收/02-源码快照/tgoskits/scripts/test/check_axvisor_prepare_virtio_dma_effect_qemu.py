#!/usr/bin/env python3
"""Behavioral contract for disposable virtio-blk DMA-effect QEMU preparation."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/contest"))
import prepare_virtio_dma_effect_qemu as preparer  # noqa: E402


def main() -> int:
    errors: list[str] = []
    results = ROOT / "results"
    made_results = not results.exists()
    results.mkdir(exist_ok=True)
    directory = results / f".prepare-virtio-dma-qemu-{os.getpid()}-{secrets.token_hex(8)}"
    directory.mkdir()
    try:
        host_dtb = directory / "host.dtb"
        rootfs = directory / "disposable-rootfs.ext4"
        config = directory / "qemu.toml"
        probe_disk = directory / "probe.raw"
        plan_path = directory / "plan.json"
        host_bytes = b"host-dtb-for-contract"
        rootfs_bytes = b"disposable-rootfs-for-contract"
        host_dtb.write_bytes(host_bytes)
        rootfs.write_bytes(rootfs_bytes)
        nonce = "0123456789abcdef0123456789abcdef"
        try:
            plan = preparer.prepare_qemu_topology(
                host_dtb=host_dtb,
                disposable_rootfs=rootfs,
                nonce=nonce,
                config_output=config,
                probe_disk_output=probe_disk,
                plan_output=plan_path,
            )
        except (OSError, preparer.VirtioDmaQemuPreparationError) as error:
            errors.append(f"preparer rejects valid no-overwrite topology inputs: {error}")
            plan = {}
        else:
            if host_dtb.read_bytes() != host_bytes or rootfs.read_bytes() != rootfs_bytes:
                errors.append("preparer modifies a Host DTB or disposable rootfs input")
            if probe_disk.stat().st_size != 512 or probe_disk.read_bytes() == b"\xa5" * 512:
                errors.append("preparer does not create a distinguishable one-sector probe disk")
            if probe_disk.read_bytes() != preparer._probe_payload(
                nonce=nonce,
                host_dtb_hash=hashlib.sha256(host_bytes).hexdigest(),
                rootfs_hash=hashlib.sha256(rootfs_bytes).hexdigest(),
            ):
                errors.append("probe disk bytes are not deterministic from nonce and input hashes")
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            args = parsed.get("args")
            expected_pairs = [
                ("-dtb", str(host_dtb)),
                ("-drive", f"id=disk0,if=none,format=raw,file={rootfs}"),
                ("-device", "virtio-blk-device,id=linux-root,drive=disk0,bus=virtio-mmio-bus.0"),
                ("-drive", f"id=dma-probe-disk,if=none,format=raw,readonly=on,file={probe_disk}"),
                ("-device", "virtio-blk-device,id=dma-probe,drive=dma-probe-disk,bus=virtio-mmio-bus.1"),
                ("-chardev", f"socket,id=dma-go-chardev,path=/tmp/axdma-{nonce}/control.sock,server=on,wait=off"),
                ("-device", "virtio-serial-device,id=dma-go-serial,bus=virtio-mmio-bus.2"),
                ("-device", "virtconsole,id=dma-go-port,chardev=dma-go-chardev,name=dma-go"),
                ("-append", "root=/dev/vda rw init=/init"),
            ]
            if not isinstance(args, list) or any(
                not any(args[index:index + 2] == [option, value] for index in range(len(args) - 1))
                for option, value in expected_pairs
            ):
                errors.append("generated QEMU config does not bind exact root/probe/DTB arguments")
            if any(argument == "-name" or argument.startswith("-name=") for argument in args or []):
                errors.append("generated QEMU config includes a static runner identity name")
            if any(
                argument in {"-serial", "-monitor"}
                or argument.startswith("-serial=")
                or argument.startswith("-monitor=")
                or "stdio" in argument.lower()
                for argument in args or []
            ):
                errors.append("generated QEMU config exposes the GO control channel through stdio/serial/monitor")
            serial_devices = [
                argument for argument in args or []
                if argument.startswith("virtio-serial-device,")
            ]
            control_ports = [
                argument for argument in args or []
                if argument.startswith("virtconsole,")
            ]
            control_chardevs = [
                argument for argument in args or []
                if argument.startswith("socket,id=dma-go-chardev,")
            ]
            if control_chardevs != [
                f"socket,id=dma-go-chardev,path=/tmp/axdma-{nonce}/control.sock,server=on,wait=off"
            ]:
                errors.append("generated QEMU config does not contain one private server-side control socket")
            if serial_devices != ["virtio-serial-device,id=dma-go-serial,bus=virtio-mmio-bus.2"]:
                errors.append("generated QEMU config does not contain one fixed-bus virtio-serial controller")
            if control_ports != ["virtconsole,id=dma-go-port,chardev=dma-go-chardev,name=dma-go"]:
                errors.append("generated QEMU config does not contain one fixed virtio-console control port")
            if plan.get("status") != "single_guest_virtio_blk_dma_effect_qemu_prepared":
                errors.append("plan does not retain preparation-only status")
            if plan.get("sessionNonce") != nonce:
                errors.append("plan does not bind the supplied nonce")
            if plan.get("inputs", {}).get("hostDtb", {}).get("sha256") != hashlib.sha256(host_bytes).hexdigest():
                errors.append("plan does not bind Host DTB bytes")
            if plan.get("inputs", {}).get("disposableRootfs", {}).get("sha256") != hashlib.sha256(rootfs_bytes).hexdigest():
                errors.append("plan does not bind disposable rootfs bytes")
            if plan.get("expectedPayload", {}).get("sha256") != hashlib.sha256(probe_disk.read_bytes()).hexdigest():
                errors.append("plan does not bind expected sector bytes")
            expected_control = {
                "transport": "virtio-console",
                "chardevId": "dma-go-chardev",
                "socket": f"/tmp/axdma-{nonce}/control.sock",
                "server": True,
                "wait": False,
                "serialDeviceId": "dma-go-serial",
                "serialDeviceBus": "virtio-mmio-bus.2",
                "portDeviceId": "dma-go-port",
                "portName": "dma-go",
                "guestPath": "/dev/hvc0",
                "stdioControlForbidden": True,
            }
            if plan.get("qemu", {}).get("control") != expected_control:
                errors.append("plan does not exactly bind the private virtio-console control topology")
            if "DMA isolation" not in plan.get("doesNotProve", []):
                errors.append("plan omits the DMA-isolation evidence boundary")
            if plan_path.read_bytes() != (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8"):
                errors.append("published plan differs from the returned immutable plan")
            try:
                preparer.prepare_qemu_topology(
                    host_dtb=host_dtb, disposable_rootfs=rootfs, nonce=nonce,
                    config_output=config, probe_disk_output=probe_disk, plan_output=plan_path,
                )
            except preparer.VirtioDmaQemuPreparationError:
                pass
            else:
                errors.append("preparer overwrites published output artifacts")

        invalid = directory / "comma,host.dtb"
        invalid.write_bytes(b"invalid")
        try:
            preparer.prepare_qemu_topology(
                host_dtb=invalid, disposable_rootfs=rootfs, nonce=nonce,
                config_output=directory / "comma-qemu.toml", probe_disk_output=directory / "comma-probe.raw",
                plan_output=directory / "comma-plan.json",
            )
        except preparer.VirtioDmaQemuPreparationError:
            pass
        else:
            errors.append("preparer accepts a comma-bearing QEMU path")
    finally:
        shutil.rmtree(directory)
        if made_results:
            results.rmdir()
    if errors:
        print("Virtio DMA-effect QEMU preparation contract failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Virtio DMA-effect QEMU preparation contract passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
