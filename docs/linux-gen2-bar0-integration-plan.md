# BAR0 integration: bounded admission plan

**GO for implementing a separate read-only resource0 adapter; NO-GO for hardware
writes now.** No executor, device access or working-tool changes in this update.
The RAM lifecycle at `21b56e0` passed; it does not certify SPEED semantics.

## Mapping contract

* Pin GPU `0000:03:00.0` (10de:1b87), parent `0000:00:02.0`; validate BAR0
  as the expected MMIO memory resource (not I/O), flags, nonzero start and length.
  Compare resource metadata with PCI BAR identity; do not write enable/config.
* Open only its normal `resource0`: `O_RDONLY | O_CLOEXEC | O_SYNC`; use
  `MAP_SHARED`, `PROT_READ`, BAR-relative file offset 0. No `_wc`, anonymous,
  private/COW fallback, `/dev/mem`, initialization, prefault scan, memcpy,
  struct packing, msync/flush or automatic preparation. Mapping creation and
  cleanup must not load/store a device word. Close: invalidate owner, munmap,
  close fd, retaining/reporting cleanup errors; never rollback a register.
* Length: round up the highest allowlisted offset plus 4 to the OS page size;
  require the entire mapped interval within BAR0. For known offsets through
  `0x8C300`, this is `0x8D000` on 4-KiB pages. Validate overflow, alignment and
  `offset <= length-4` before every access; restrict reads to known safe DWORDs.
  No public arbitrary pointer/fd/write interface. Do not reuse RamWord init.
* A future separately reviewed write-enabled variant would use O_RDWR and
  PROT_READ|PROT_WRITE, retaining the exact audited volatile DWORD functions,
  integer validation and owner/lifetime serialization. One explicit store;
  separate guarded same-device readback. CPU/compiler barriers and O_SYNC are
  not posted-write completion; a successful safe same-device read orders the
  preceding write but does not acknowledge an internal speed command.
* Linux v6.8 [pci-sysfs.c](https://github.com/torvalds/linux/blob/v6.8/drivers/pci/pci-sysfs.c)
  routes resource0 through write_combine=0;
  [mmap.c](https://github.com/torvalds/linux/blob/v6.8/drivers/pci/mmap.c)
  uses pgprot_device, rather than pgprot_writecombine. x86 noncached protection
  uses UC- ([pgtable.h](https://github.com/torvalds/linux/blob/v6.8/arch/x86/include/asm/pgtable.h)).
  Do not label the effective mapping strict UC from its filename or O_SYNC.
  At admission, verify the target Ubuntu kernel path and available read-only
  PAT/MTRR evidence against the BAR range; reject effective WB/WC or unresolved
  conflicting aliases. No cache/PAT/MTRR changes or debugfs mounting by the tool.

## Guards and ordering

Fresh guards before each endpoint access: GPU0 unbound from **any** driver,
D0 and runtime active, MSE=1/BME=0; exact identity/topology/resource unchanged;
Root Training=0, DLLLA=1 and valid expected speed/width. Check Root before GPU
config/BAR reads; immediately recheck Root after MMIO. No GPU readback after an
unsafe Root observation. Serialize lifecycle/access and use the project lock;
that lock does not exclude NVIDIA or a separate binder. Operator must exclude
concurrent bind/runtime-PM/config writers; a userspace guard has a TOCTOU window.
No guard may fix power state, command bits or binding automatically.

Windows ApplyLiveGroup order is
`8872C→2241C→8841C→8C2C0→8C300→8C040→8C1C0→88610`
([source](../src/P104Gen2x4.cs)); changed-only masked RMW, 20-ms delay, masked
readback, SPEED without CHANGE_SPEED. Linux's verified preparation is
`8872C=6→2241C=81→8841C=C0B40500`, then `8C1C0=20032`, then separately
established consistent CAP/CAP2 and TLS=2. This is not the Windows sequence or
proof of equivalent behavior. Cold-boot factory baseline is acceptable for a
read-only test, but must refuse a future prepared-baseline write test. No automatic
prefix/TLS/retrain or extra CYA/VSEC/width writes.

## First limited test, only after separate authorization

1. Review separate read-only adapter, exact kernel mapping path and final-binary
   disassembly; run its RAM lifecycle/failure tests. Arrange maintenance access
   and a possible cold-power recovery window (whole-host recovery affects GPU1).
2. Capture JSONL identity, guards, BAR range/type, kernel/cache evidence and
   Root/GPU link baseline. Open/map without touching destination, then repeat
   guards. This test makes **zero MMIO/config writes**, including SPEED.
3. One guarded pass over the known baseline DWORDs, including 8C040; record
   monotonic brackets and Root pre/postchecks. Unmap/close, report errors and exit.
   No loop, bind, CUDA, retrain, reset or automatic recovery.
4. STOP on identity/binding/D-state/command/resource deviation, Training/DLL loss,
   short/error config read, SIGBUS/SIGSEGV, sentinel/unexpected register value,
   readback/status inconsistency or new relevant AER. Signals terminate the
   isolated process; do not probe GPU after link loss. Preserve existing logs,
   never clear W1C AER/status. Missing AER does not prove safety.
5. On loss: stop all endpoint access, retain Root/host evidence only if safe;
   no repeat/rollback/reset. Operator schedules full AC recovery if necessary;
   tool never performs it. Verify GPU0 recovery before discussing another test.

Passing this closes read-only mapping integration admission, not Gen2 admission.
Remaining write blockers: unreviewed real write-enabled lifecycle/memory type,
uncontrolled concurrent owner, and register-transition risk (the old crash is
not proven solely attributable to pack_into). Any future one-store SPEED test
needs its own explicit decision/authorization; no CHANGE_SPEED executor here.
