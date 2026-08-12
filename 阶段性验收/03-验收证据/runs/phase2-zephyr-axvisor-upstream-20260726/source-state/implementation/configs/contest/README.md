# Contest configuration

`qemu-aarch64-baseline.env` is the trusted manifest consumed by
`scripts/contest/run_axvisor_baseline.sh`.  It intentionally contains only
stable identifiers, canonical repository-relative paths, success markers, and
the run timeout.

This directory is not a second source of AxVisor VM definitions.  Board and
guest VM configuration remain owned by `os/axvisor/configs/`.  The
`qemu-aarch64-linux-baseline.toml` file is only a deterministic runner harness:
it preserves the canonical AArch64 QEMU arguments, waits for the minimal Linux
shell prompt, and emits an anchored success marker.  The existing
`os/axvisor/scripts/setup_qemu.sh` creates machine-specific VM configurations
under `os/axvisor/tmp/vmconfigs/`; the baseline wrapper records a copy after a
successful setup.

`qemu-aarch64-linux-smp2.toml` is the phase-2 runner harness.  It keeps the
same outer QEMU machine and four host CPUs, but its interactive shell command
prints the unique success marker only after `/proc/cpuinfo` proves two Linux
CPUs.  The actual two-vCPU ownership remains in
`os/axvisor/configs/vms/qemu/aarch64/linux-smp2.toml`; this directory must not
duplicate or override that VM definition.

Because the manifest is sourced by Bash, treat changes to it as executable
code.  Values must remain simple quoted strings, paths must be relative to the
repository or AxVisor directory as documented in the file, and no commands or
shell substitutions may be added.

`qemu-aarch64-zephyr-smoke.env` is the locked phase-2 Zephyr source and
toolchain contract consumed by
`scripts/contest/run_axvisor_zephyr_smoke.sh`.  It pins the full Zephyr commit,
active west-manifest hash, SDK/west versions, board, repository paths, markers,
and timeouts.  Runtime-specific installation paths stay outside this file and
must be supplied through `ZEPHYR_BASE` and `ZEPHYR_SDK_INSTALL_DIR`.

`qemu-aarch64-zephyr-smoke.toml` is the outer AxVisor QEMU harness.  It keeps
the four-CPU AArch64 `virt` machine used by the Linux evidence, but deliberately
has no Linux block device, root command line, or drive.  AxBuild receives a
small explicit placeholder through `--rootfs` only to bypass managed-rootfs
resolution; because the QEMU harness has no block device, that placeholder is
not exposed to the Zephyr guest.  The success regex accepts only the complete
periodic-task marker line.
