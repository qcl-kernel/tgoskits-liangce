# P3 realtime host tools

This directory contains the P3 event contract, clock mapping validator,
statistics tools, candidate selector, matrix planner, and the host-only matrix
package consumer. These tools do not launch WSL, QEMU, a Guest, or a target
build.

## Event validation

Validate a native/same-clock event run with:

```powershell
py -3 scripts/contest/rt/validate_rt_events.py `
  --run <run> `
  --schema scripts/contest/rt/schema/p3-rt-event-v1.schema.json
```

For a cross-EL run, pass the session-owned `clock.json`. The validator maps raw
ticks first and then computes `floor(mapped_ticks * 1_000_000_000 / frequency)`:

```powershell
py -3 scripts/contest/rt/validate_rt_events.py `
  --run <run> `
  --schema scripts/contest/rt/schema/p3-rt-event-v1.schema.json `
  --clock <run>/clock.json `
  --require-calibration
```

`clock.json` must be `p3-clock-v1`; its `cntvoff_ticks_by_vcpu` keys bind every
mapped VM/vCPU, calibration residuals must be within the registered threshold,
and event `monotonic_ns` must be exactly reproducible from raw counter ticks.
P3 event units are `ns` or `null`.

## Statistics and matrix

```powershell
py -3 scripts/contest/rt/summarize_rt.py --help
py -3 scripts/contest/rt/build_rt_matrix.py --help
py -3 scripts/contest/rt/select_rt_candidates.py --help
py -3 scripts/contest/rt/run_rt_matrix.py --help
```

The matrix runner is intentionally host-only and emits
`realtime_measurement_blocked`. A passing host contract is not P3-CLOCK-REG-01,
TEST-008..010, Guest-IP, or production A/B evidence.
