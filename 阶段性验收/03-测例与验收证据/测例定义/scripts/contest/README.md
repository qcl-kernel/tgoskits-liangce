# Contest runtime and validation entry

Last reviewed: 2026-08-20. This file is the repository-local command entry for the
Liangce contest work. The canonical plan and evidence meaning live in the workspace:

- `..\..\..\development\current\开发交接.md`
- `..\..\..\development\current\计划.md`
- `..\..\..\development\contest\contest-stage-p2-platform.md`
- `..\..\..\development\contest\contest-stage-p3-realtime.md`
- `..\..\..\development\contest\contest-stage-p4-network.md`
- `..\..\..\development\contest\contest-stage-p5-ai-control.md`

This checkout is the only active development tree. The old `tgoskits/` checkout is
historical input, not an alternative runtime tree.

## 1. Safety and evidence rules

- Never create or submit a PR. Never push upstream. Private-repository push also
  requires explicit user authorization.
- WSL, QEMU and Docker execution require explicit user authorization.
- Every runtime attempt uses a new output directory. Never overwrite a prior run,
  including failed attempts.
- Static/host, target build, single Guest, dual Guest, Guest IP, AI loop and real-time
  A/B are separate evidence levels.
- Historical `guest_network_completed` records remain narrow marker/cleanup smoke.
  The current v1 runner publishes `guest_network_smoke_completed` only after an exact
  scenario oracle and final-log revalidation. Neither token qualifies TEST-011..015.
- Do not add build products, cached images, `tmp/`, or raw `results/` to a source commit.

## 2. Mandatory preflight

Run from Windows PowerShell:

```powershell
Set-Location F:\project\泉城实验室\tgoskits-upstream-integration
git status --short --branch
git rev-parse HEAD
git rev-parse upstream/dev
git rev-list --left-right --count HEAD...upstream/dev
py -3 ..\development\tools\development_docs.py --repo-root . --check
py -3 scripts/test/check_contest_development_mirror.py
py -3 scripts/test/check_ci_paths.py
```

The 2026-08-20 recorded production-code checkpoint is `574d569be...`; docs-only
commits may follow it. The upstream checkpoint is `21ef4b218...`; the production-code
checkpoint is ahead 15/behind 39, while the current counts must be read from Git. If
the production ancestry or upstream SHA differs, update the handoff before coding.
Existing modified controller build artifacts, `results/`, and the unreadable
historical contract directory must not be silently removed or committed.

## 3. Deterministic gates

Run the gates relevant to the package before any target or QEMU attempt:

```powershell
# Documentation and CI layout
py -3 scripts/test/check_contest_development_mirror.py
py -3 scripts/test/check_ci_paths.py

# P4 source, network, and protocol contracts
py -3 scripts/test/check_contest_upstream_p4_baseline.py
py -3 scripts/test/check_axvisor_virtio_net_auto_resources.py
py -3 scripts/test/check_contest_network_contract.py
py -3 scripts/test/check_icpc_protocol.py
py -3 scripts/test/check_icpc_control_payload.py
py -3 scripts/test/check_contest_zephyr_control.py

# P5 host/static contracts
py -3 scripts/test/check_contest_ai_model.py
py -3 scripts/test/check_contest_ai_metrics.py

# P3 host/static contracts
py -3 scripts/test/check_contest_rt_event_schema.py
py -3 scripts/test/check_contest_rt_statistics.py
py -3 scripts/test/check_contest_rt_candidate_selection.py
py -3 scripts/test/check_contest_rt_run_plan.py
py -3 scripts/test/check_contest_rt_probe.py
```

These commands do not establish target-build or runtime evidence.

## 4. P4 current work packages

The mandatory order is:

```text
P4-UPSYNC-02 -> P4-EVID-01 -> P4-SMOKE-02 -> P4-REL-01
```

### 4.1 P4-UPSYNC-02

Port the local Auto resource, resolved FDT, level IRQ and bounded vnet0 behavior to
official `21ef4b218...` and the issuing-vCPU `DeviceContext`/separate `read`/`write`
API. Add contest checks through CI manifest v3, not by extending the old workflow
command list. Exact red/green tests are in the P4 stage manual and current handoff.

Rust/AArch64/QEMU commands are intentionally not auto-run from this document. After
the user authorizes WSL/QEMU, run the official repository `cargo xtask` gates recorded
in the handoff and save stdout/stderr/exit codes with the new source SHA.

### 4.2 P4-EVID-01

The existing files are useful inputs but not a qualification publisher:

```text
scripts/contest/network/run_guest_network.py
scripts/contest/network/validate_network_session.py
configs/contest/network/test-011-v1.json
configs/contest/network/test-012-v1.json
configs/contest/network/test-013-v1.json
configs/contest/network/test-015-v1.json
```

Current v1 profiles explicitly say `host_only=true` and `L2 host`. The runtime runner
now fail-closes on profile/scenario drift, missing or duplicate READY/completion lines,
forbidden markers, non-zero smoke loss, and changes found by final-log revalidation.
This closes the false-success smoke path but not P4-EVID-01. The remaining work must
retain v1 for host fixtures and add a Guest-runtime v2 schema/validator with QEMU
identity, nonce, two final DTBs/READY,
structured scenario counters, frame JSONL, PCAP, counters, metrics, fault manifest,
hashes, status-last, and cleanup. Use `guest_network_smoke_completed` for smoke and
`guest_network_qualified` only after the scenario-specific TEST oracle passes.

### 4.3 Current runner CLI (strict, unqualified smoke only)

The old evidence and v1 profile class remain **historical smoke only**; the stricter
oracle prevents new false success but does not retroactively upgrade those packages.

Inspect the exact installed interface:

```powershell
py -3 scripts/contest/network/run_guest_network.py --help
py -3 scripts/contest/network/prepare_linux_network_rootfs.py --help
py -3 scripts/contest/network/prepare_zephyr_network_image.py --help
py -3 scripts/contest/network/validate_network_session.py --help
```

The existing smoke runner takes all inputs explicitly:

```text
run_guest_network.py
  --repository <absolute current repository>
  --build-config os/axvisor/configs/board/qemu-aarch64-contest-network.toml
  --qemu-config configs/contest/qemu-aarch64-linux-zephyr-dual.toml
  --linux-vmconfig <manifest-bound Linux VM TOML>
  --zephyr-vmconfig <manifest-bound Zephyr VM TOML>
  --linux-rootfs <prepared disposable rootfs>
  --linux-rootfs-manifest <matching manifest>
  --zephyr-image <prepared zephyr.bin>
  --zephyr-build-manifest <matching manifest>
  --scenario <test-011|test-012|test-013|test-015>
  --profile <matching profile JSON>
  --timeout-seconds <bounded timeout>
  --run-id <new unique ID>
  --output-dir <new nonexistent evidence directory>
```

Do not use this v1 command to publish qualification. After Guest-runtime v2 is complete, replace this
section with the v2 command from `--help` and add its exact current run parameters to
the handoff. Never reuse an old resolved VM TOML, rootfs/image manifest, or output dir.

### 4.4 Qualification sequence

- TEST-011: each direction actively sends 100 ICMP echo; loss 0; identity/DTB/capture.
- TEST-012: 10,000 UDP echo per direction at 100 packets/s plus fixed fault profiles.
- TEST-013: 10,000 framed TCP messages plus partial/merged reads, disconnect, restart,
  and half-frame EOF.
- TEST-015: 1,000 CONTROL at 10 Hz with ACK/STATUS/ERROR/HEARTBEAT, faults, restart,
  exactly-once application or explicit expiry/cancel.

The previous ICPC `sent=100 verified=85 loss=15` is a valuable smoke and a mandatory
regression input; it is not a passing TEST-015 qualification.

## 5. P5 entry

P5-AI-A can begin now; P5-AI-B begins after the upsync interface freezes; P5-AI-C
requires new-HEAD P4 smoke; P5-AI-Q requires P4 reliability qualification.

```powershell
py -3 scripts/contest/ai/generate_dataset.py --help
py -3 scripts/contest/ai/train_export.py --help
py -3 scripts/contest/ai/verify_model.py --help
py -3 scripts/contest/ai/compute_metrics.py --help
```

The Linux controller must run the canonical `3->8->1` float32 model and match golden
vectors. The Zephyr application must route CONTROL through the one-element mailbox,
apply action only at the 100 ms boundary, publish STATUS from the real plant/action,
and enter duty=0 after 500 ms without a valid command. Placeholder STATUS bytes do not
qualify. Exact numerical contracts are in the P5 stage manual and `contracts.md`.

## 6. P3 entry

The host tools can be developed before runtime, but production samples are collected
only after the official upsync and P4/P5 production path freeze:

```powershell
py -3 scripts/contest/rt/validate_rt_events.py --help
py -3 scripts/contest/rt/summarize_rt.py --help
py -3 scripts/contest/rt/select_rt_candidates.py --help
py -3 scripts/contest/rt/build_rt_matrix.py --help
```

First close P3-TRACE-FEAS-01: identify the real passthrough/virtual timer path, one clock
domain, and instrumentation overhead. The official no_std `ax-tracepoint` may be used
only if this feasibility gate proves it fits AxVisor+Zephyr without changing production
semantics; otherwise keep the dedicated probe and IF-010 event schema. Missing path
events are `N/A`, never synthesized.

## 7. Handoff after every package

Update, in order:

1. affected `development/contest/contest-spec/` facts;
2. `development/current/{现状,阻塞,计划,开发交接}.md`;
3. canonical-to-repository mirror using
   `py -3 ..\development\tools\development_docs.py --repo-root . --sync-to-repo --check`;
4. deterministic package tests, `check_contest_development_mirror.py`,
   `check_ci_paths.py`, and `git diff --check`;
5. local source commit only, excluding evidence/build/cache files. No PR and no push
   without authorization.
