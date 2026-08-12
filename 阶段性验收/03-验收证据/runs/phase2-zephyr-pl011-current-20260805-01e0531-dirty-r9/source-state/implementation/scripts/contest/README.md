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

## 6. Plan final per-VM Guest DTB capture

The outer QEMU `dumpdtb` records the host machine description only.  It does
not contain the filtered and runtime-patched DTB that AxVisor loads for each
Guest.  The feature-gated `guest-fdt-evidence` path emits one strict marker per
VM only after the final bytes have been written and registered:

```text
AXVISOR_GUEST_DTB_READY vm=1 gpa=0x8fe00000 size=8192 hpa_segments=0x90000000:4096,0x91000000:4096
```

Before a QMP-aware runner issues any `pmemsave`, validate those markers and
create a bounded, command-free plan:

```bash
python3 scripts/contest/plan_guest_dtb_capture.py \
  --log results/baseline/runs/<run-id>/axvisor.log \
  --expected-vm 1 \
  --expected-vm 2 \
  --output results/baseline/runs/<run-id>/capture-plan.json
```

The planner rejects malformed, missing, duplicate, unexpected, oversized,
overflowing, truncated, and physically overlapping ranges.  It does not send
QMP commands and its `capture_planned` status does not prove that any bytes
were captured, that either Guest booted, that DMA is isolated, or that the
Guests exchanged IP traffic.

Validate the exact `pmemsave` requests without opening a QMP socket:

```bash
python3 scripts/contest/execute_guest_dtb_capture.py \
  --plan results/baseline/runs/<run-id>/capture-plan.json \
  --commands-only \
  --output results/baseline/runs/<run-id>/capture-commands.json
```

The command artifact uses each marker-derived segment HPA as QMP `val`; the
Guest GPA remains provenance only and is never used as a fallback address.
When QEMU was started with an existing Unix QMP socket, execute the same plan:

```bash
python3 scripts/contest/execute_guest_dtb_capture.py \
  --plan results/baseline/runs/<run-id>/capture-plan.json \
  --qmp-socket /run/user/1000/axvisor-qmp.sock \
  --output results/baseline/runs/<run-id>/capture-execution.json
```

The executor negotiates QMP capabilities, accepts only `pmemsave`, refuses to
overwrite evidence, and checks the source-log hash and exact segment sizes.
QEMU writes into a private, randomly named staging directory; each segment is
revalidated immediately before no-replacement publication, and the execution
manifest is published last.  A failure removes only files owned by this
attempt.  The narrow status
`qmp_pmemsave_completed_exact_size` proves neither an FDT header nor semantic
validity, Guest boot, DMA isolation, or Guest IP traffic.  The outer QEMU
`dumpdtb` remains host-machine evidence and cannot replace this capture.

For a live run, prefer the identity-bound wrapper instead of invoking the
executor directly.  Start AxVisor through xtask with all three narrow launch
arguments; the name must contain a fresh 128-bit lowercase hexadecimal nonce:

```bash
nonce="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
cargo xtask qemu ... \
  --qmp-socket /run/user/1000/axvisor-qmp.sock \
  --qemu-pidfile /run/user/1000/axvisor-qemu.pid \
  --qemu-name "axvisor-guest-dtb-${nonce}"
```

Once every `AXVISOR_GUEST_DTB_READY` line is visible, capture through that
same running process and QMP session:

```bash
python3 scripts/contest/capture_live_guest_dtbs.py \
  --live-log results/baseline/runs/<run-id>/axvisor-qemu.log \
  --evidence-dir results/baseline/runs/<run-id>/guest-dtb-live \
  --expected-vm 1 --expected-vm 2 \
  --qmp-socket /run/user/1000/axvisor-qmp.sock \
  --qemu-pidfile /run/user/1000/axvisor-qemu.pid \
  --qemu-name "axvisor-guest-dtb-${nonce}"
```

The wrapper binds the pidfile PID, `/proc` start time and boot id, UID,
executable, complete command-line hash, Unix-socket inode, Linux `SO_PEERCRED`,
QMP `query-name`, and nonce.  It requires an initially running VM, issues
`stop`, freezes newline-complete log bytes exactly through the final expected
ready marker, runs every `pmemsave` on that same negotiated session, and always
attempts `cont`.  It publishes `capture-chain.json` last, hashing the frozen
prefix, identity record, plan, segment files, execution record, assembled DTBs,
and header-validation result.  Partial evidence has no final chain manifest.

When stdout is relayed through `tee`, QEMU owns the pipe rather than the log
file descriptor.  The chain therefore records, but cannot cryptographically
prove, that the supplied log writer belongs to the bound QEMU process.  It also
does not prove DTB semantic validity, Guest boot, DMA isolation, or Guest IP
traffic.

After the executor has produced every segment in the evidence directory,
assemble the segments and validate the bounded FDT header:

```bash
python3 scripts/contest/assemble_guest_dtb_capture.py \
  --plan results/baseline/runs/<run-id>/capture-plan.json \
  --execution results/baseline/runs/<run-id>/capture-execution.json \
  --output results/baseline/runs/<run-id>/capture-result.json
```

The assembler re-parses the source markers, rebuilds the canonical plan, and
binds the source log, plan, execution manifest, segment hashes, deterministic
filenames, exact sizes and order.  It then checks FDT magic, `totalsize`,
version fields, and bounded structure/string/reservation offsets.  It writes one
`guest-vm-<id>.final.dtb` per VM and a `capture-result.json`.  The status
`dtb_bytes_assembled_header_validated` still does not prove Devicetree semantic
validity, VM ownership, Guest boot, DMA isolation, or Linux/Zephyr IP traffic.

The identity-bound launch arguments and capture wrapper have Rust/Python
contract coverage.  A current AArch64 AxVisor release containing the Guest-DTB
marker path has also built and booted in the single-Zephyr r8 run.  The marker
producer now establishes a fresh line under the shared console lock so the
strict planner can continue rejecting ANSI-prefixed records.  That final line
boundary change still needs a new Rust/AArch64/QEMU run, and the wrapper has not
been exercised against a real identity-bound QMP socket.  The assembled DTBs
have not yet passed `dtc` semantic review.  These tools therefore remain an
evidence chain, not a runnable dual-Guest success claim.

### Capture the final outer-QEMU memory topology

Generate the host DTB with the same RAM size that the final AxVisor run will
use.  The probe defaults to 8192 MiB (8 GiB); pass the value explicitly when
assembling a reproducibility command:

```bash
bash scripts/contest/probe_qemu_aarch64_virtio_slots.sh \
  --output-dir results/baseline/runs/<run-id>/outer-topology \
  --memory-mib 8192
```

The selected value is applied to both the QMP topology capture and the matching
`dumpdtb` invocation.  Use `--dry-run` to review both commands without creating
an evidence directory or starting QEMU.  The resulting `host.dtb` is QEMU's
unmodified tree; this step does not select or reserve a VM carveout.

## 7. Validate host VM carveout artifacts

Before enabling any `MapReserved` VM RAM, validate a decoded host DTS against
the exact VM TOML artifacts used by that run:

```bash
python3 scripts/contest/validate_host_vm_carveouts.py \
  --dts results/baseline/runs/<run-id>/host-final.dts \
  --vm-config os/axvisor/configs/vms/qemu/aarch64/linux-smp2-dual.toml \
  --vm-config os/axvisor/configs/vms/qemu/aarch64/zephyr-smp1-dual.toml \
  --output results/baseline/runs/<run-id>/host-vm-carveouts.json
```

The validator accepts only dedicated `axvisor,vm-carveout-v1` nodes and exact
VM-id/HPA/size matches.  It rejects malformed or dynamic reservations,
overlaps, invalid VM configs, and evidence overwrite.  Its result is static
artifact evidence only: it does not prove that QEMU used this DTB, that the
boot allocator excluded the ranges, that AxVM authorized them, or that DMA was
isolated.  Those runtime identities and allocator/stage-2 observations remain
separate gates.

The current dedicated-carveout contract deliberately rejects `no-map`.
`MapReserved` still obtains a host virtual address through `phys_to_virt`, so a
generator must not add `no-map` until that direct-map dependency has been
removed or independently validated and the someboot/runtime contracts are
updated together.

## 8. Demultiplex framed Guest console evidence

After a host backend has emitted strict console frame records into one bounded
AxVisor log, reconstruct one byte stream per expected VM:

```bash
python3 scripts/contest/demux_guest_console_frames.py \
  --host-log results/baseline/runs/<run-id>/axvisor.log \
  --output-dir results/baseline/runs/<run-id> \
  --expect-vm 1:linux \
  --expect-vm 2:zephyr
```

The demultiplexer requires fixed-order frame fields, contiguous sequence and
generation state, complete byte counts, and zero dropped/DMA-attempt counters.
It publishes `guest-vm-<id>.console.log` files before `console-manifest.json`
without replacement.  The VM-local PL011 frame emitter, synthesized Guest FDT,
dedicated-host-pCPU drain, and Zephyr polling-only profile have now built and
run under AArch64 AxVisor.  Run r8 strictly reconstructed 279 contiguous frames
and 719 Guest bytes with zero dropped bytes and DMA attempts.  That run still
finished `timed_out` because its CRLF success line was not normalized; the
runner fix needs a new r9 before the package can be called passed.  The
demultiplexer proves only the supplied frame stream and reconstruction rules;
Guest boot and console isolation still require the separately bound lifecycle,
configuration, and runtime evidence.

## 9. Validate the Zephyr polling-only PL011 profile

Validate the checked-in template before building:

```bash
python3 scripts/contest/validate_zephyr_pl011_profile.py \
  --overlay configs/contest/zephyr/qemu-cortex-a53-pl011-polling.overlay \
  --config configs/contest/zephyr/qemu-cortex-a53-pl011-polling.conf
```

For a real Zephyr build, also pass `--final-dts` and `--final-config`.  A
template-only pass is static evidence and must not be reported as a rebuilt or
booted Zephyr image.
