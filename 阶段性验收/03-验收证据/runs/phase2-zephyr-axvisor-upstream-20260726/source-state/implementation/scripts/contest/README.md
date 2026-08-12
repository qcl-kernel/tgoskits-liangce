# Contest baseline helpers

This directory contains the small, reviewable wrappers used to collect the
QEMU AArch64 AxVisor baseline.  The wrappers do not replace AxVisor's own image
or VM setup logic: guest preparation is always delegated to
`os/axvisor/scripts/setup_qemu.sh`.

## 1. Preview the commands

From the repository root, validate the configuration and print every command
without creating files or starting QEMU:

```bash
bash scripts/contest/run_axvisor_baseline.sh --guest all --dry-run
```

The accepted guest values are `arceos`, `linux`, `linux-smp2`, and `all`.
`all` intentionally preserves the phase-1 meaning of ArceOS plus single-vCPU
Linux; run `linux-smp2` explicitly for the phase-2 proof.  A custom run ID may
contain only letters, digits, dots, underscores, and hyphens:

```bash
bash scripts/contest/run_axvisor_baseline.sh \
  --guest arceos \
  --run-id 20260718T120000Z-arceos \
  --dry-run
```

## 2. Capture the Windows host

Run this step in PowerShell on the Windows host.  Docker is optional and is
queried only when `-IncludeDocker` is supplied.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\contest\capture_host_environment.ps1 `
  -RunId 20260718T120000Z-baseline `
  -DryRun

powershell -ExecutionPolicy Bypass -File scripts\contest\capture_host_environment.ps1 `
  -RunId 20260718T120000Z-baseline `
  -IncludeDocker
```

The script records only reproducibility fields.  It deliberately omits serial
numbers, full environment-variable dumps, Docker credentials, and full
`docker info` output.

## 3. Prepare Ubuntu 24.04 / WSL2

The direct WSL path was verified with the following packages.  `libclang` is
required by bindgen and `ipxe-qemu` supplies `efi-virtio.rom`; having only the
`clang` and QEMU executables is not sufficient.

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential curl git libssl-dev libudev-dev pkg-config python3 \
  qemu-system-arm wget xz-utils clang libclang-19-dev cmake zlib1g-dev \
  ipxe-qemu

test -f /usr/lib/ipxe/qemu/efi-virtio.rom
```

Install the same AArch64 musl toolchain source used by the repository's
container image:

```bash
wget -q \
  https://github.com/arceos-org/setup-musl/releases/download/prebuilt/aarch64-linux-musl-cross.tgz \
  -O /tmp/aarch64-linux-musl-cross.tgz
sudo tar -xzf /tmp/aarch64-linux-musl-cross.tgz -C /opt
/opt/aarch64-linux-musl-cross/bin/aarch64-linux-musl-cc --version
```

Entering the repository installs the toolchain declared in
`rust-toolchain.toml`.  The ELF-to-binary step also needs cargo-binutils:

```bash
rustup show
cargo install cargo-binutils --version 0.4.0 --locked
cargo-objcopy --version
```

## 4. Run the guest baselines

Run this step in a Linux environment that already provides the repository
toolchain, Cargo, QEMU, and the standard `timeout` utility.  The script itself
does not launch or require Docker.

```bash
bash scripts/contest/run_axvisor_baseline.sh \
  --guest all \
  --run-id 20260718T120000Z-baseline

bash scripts/contest/run_axvisor_baseline.sh \
  --guest linux-smp2 \
  --run-id 20260722T120000Z-linux-smp2
```

Raw run bundles are written below `results/baseline/runs/<run-id>/`.  A real
run also appends one row per guest to `results/baseline/index.csv`.  Review and
sanitize a bundle before publishing it; do not commit the `runs/` directory.
The wrapper defaults image downloads to
`${XDG_CACHE_HOME:-$HOME/.cache}/tgoskits/axvisor-images`; set
`AXVISOR_IMAGE_LOCAL_STORAGE` to use a different persistent cache.

## Configuration

The checked-in, trusted configuration is
`configs/contest/qemu-aarch64-baseline.env`.  It points at AxVisor's canonical
board and generated VM configuration paths.  ArceOS uses the canonical QEMU
runner configuration.  Linux uses the contest-only QEMU smoke configuration,
which waits for the `~ #` prompt and runs a shell-built-in command that emits
an anchored `test pass!` marker.  Generated VM configuration files remain
under `os/axvisor/tmp/` and are copied into the run bundle only as evidence.

The Linux SMP2 runner uses the sibling `linux-smp2.toml` VM definition and a
separate QEMU harness.  The minimal rootfs starts directly in `/bin/sh`, so the
probe first mounts procfs with the bundled `/bin/busybox`, then counts
`processor` records in `/proc/cpuinfo` using shell built-ins.  It emits
`linux-smp2-pass` only when the count is exactly two.
`validate_linux_smp2_log.py` additionally requires the
dynamic DTB, vCPU 0/1 affinity, PSCI CPU1 startup, GIC redistributor, and Linux
SMP summary lines.  The per-check result is archived as
`linux-smp2/startup-evidence.json`; a marker alone is not accepted as proof.

## 5. Run the locked Zephyr periodic smoke

The phase-2 RTOS path uses Zephyr v4.4.0 at commit
`684c9e8f32e4373a21098559f748f06915f950c9`, Zephyr SDK 1.0.1, west 1.5.0,
and the maintained `qemu_cortex_a53` board.  The complete active west manifest
is also locked by SHA-256, so a module checkout cannot drift independently of
the Zephyr repository.  Toolchain installation is an explicit environment
preparation step; the runner performs no provisioning or network access.

Point the runner at an already prepared workspace and SDK, then preview or run
the native-QEMU and AxVisor checks in order:

```bash
export PATH=/root/.cache/tgoskits-contest/zephyr-4.4.0-venv/bin:$PATH
export ZEPHYR_BASE=/root/.cache/tgoskits-contest/zephyrproject-4.4.0/zephyr
export ZEPHYR_SDK_INSTALL_DIR=/root/.cache/tgoskits-contest/zephyr-sdk-1.0.1

bash scripts/contest/run_axvisor_zephyr_smoke.sh \
  --mode all \
  --run-id phase2-zephyr-smoke-review \
  --dry-run

bash scripts/contest/run_axvisor_zephyr_smoke.sh \
  --mode all \
  --run-id phase2-zephyr-smoke-review
```

`zephyr-periodic-smoke` emits a start line, ten numbered samples at a nominal
100 ms period, and `TGOS_ZEPHYR_SMOKE_PASS samples=10` only after the loop
completes.  The validator additionally checks ordered sequence numbers,
strictly increasing uptime, a 50–500 ms delta envelope, and—under AxVisor—VM
creation, kernel loading, vCPU 0 creation, pCPU 0 affinity, and boot success.

The VM generator reads the actual ELF64/AArch64 executable segment and
converts its virtual entry to a physical load address.  It writes a new config
under `os/axvisor/tmp/vmconfigs/`; it never edits the canonical Zephyr template.
The run bundle records the ELF, BIN, generated config, provenance hashes,
frozen manifest, logs, evidence JSON, a bounded implementation snapshot with
per-file SHA-256 hashes, the bounded tracked diff, and `checksums.sha256`.
The AxVisor run uses a dedicated build config with no filesystem or block
driver; its 4 KiB `--rootfs` placeholder only prevents xtask from downloading
a default image and is never attached to QEMU.  Set `ZEPHYR_BUILD_DIR` to
an ext4 path such as `/root/.cache/tgoskits-contest/builds/<run-id>` when a
repository on `/mnt/*` makes CMake/Ninja file operations too slow.
