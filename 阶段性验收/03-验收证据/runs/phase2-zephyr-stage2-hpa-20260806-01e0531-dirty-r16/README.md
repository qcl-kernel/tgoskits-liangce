# r16 single-Guest stage-2 HPA review

This directory is a local, ignored evidence package for the dirty worktree at
`01e053105`. The launcher published `status.json` last with
`success=true` and `status=single_guest_live_capture_passed` after the QEMU
process group exited and `/tmp/axgdtb-a410614810017ed9c8c478118c75ca87`
was removed.

## Validated observations

- `axvisor-live.log` SHA-256:
  `7c5f85e8038c71c540bbb185f655cd7b819eca4b5203731771f7151d6fd1cca4`.
- Exactly one strict stage-2 marker follows the matching VM/vCPU running line:
  `AXVISOR_STAGE2_HPA_POSTRUN vm=1 vcpu=0 kind=alloc gpa=0x40000000 hpa=0x232200000 page_size=4096 identity=0`.
- `stage2-hpa-runtime.json` SHA-256:
  `f52b6b3dd0953aa2d7933d05ae772754f40288ce0da673316b6e1a208e70f63e`.
  It binds the exact host log, `status.json`, and input VM TOML. Its status is
  `stage2_hpa_markers_match_configured_regions`.
- Captured `guest-vm-1.final.dtb` is 7640 bytes with SHA-256
  `a90a5336b2883284f18a60a5c2a928e32031f689c2b2befb54602e68d9dd8cbf`.
- Two independent `dtc -I dtb -O dts` decodes produced DTS SHA-256
  `0c871a90a677d4d697fe639242eb0a5748d93489aef63cfb94f4f05b941c39ef`.
- `dtc-review/guest-vm-1.static-semantics.json` SHA-256:
  `a581a6cb47a2b1816299a669f388ad854f9fb48558aa62b716728c5832a6f13b`.
  Its status is `captured_guest_dtb_static_semantics_validated` and it binds
  VM TOML SHA-256
  `e9c1b2ee7ea2b355ca028cf82d5c3e02ad6752f370a92234fe6ec8ac8bb3d8ca`.
- The decoded r16 DTS differs from the reviewed r13 DTS only in the QEMU-provided
  `rng-seed` and `kaslr-seed` values.

## Evidence boundary

This proves one single-VM `MapAlloc` software stage-2 page-table query after a
backend VM exit returned to AxVM, plus the identity-bound QMP capture and static
semantics of that captured Guest DTS. It does not prove that the observed exit
was subsequently handled successfully, Guest boot, host allocator exclusion,
`MapReserved` carveout use, passthrough DMA isolation, dual-Guest execution,
30-minute stability, or Guest TCP/UDP/IP connectivity.
