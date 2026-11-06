# T5600 dual-channel read-only observer

New standalone command: `pcie_dual_sampler.py`. Existing working tools, including
`pcie_root_sampler.py` and `p104_gen2_linux.py`, are **not modified**. There is no
CHANGE_SPEED executor, config/MMIO write function, unbind/bind/reset/retrain,
driver installation, VBIOS operation or GPU 1/root access in the new sampler.

The user's root-only result on kernel 6.8.0-142-generic is retained as context:
63,650 samples in 250 ms, read p50/p99 2.870/3.756 us, cycle p50 4.097 us,
conservative gap p99/max 11.105/36.966 us, status 3041 throughout. These are
root-only measurements with NVIDIA bound. They are **not** performance bounds
for the dual-channel observer with GPU 0 unbound and additional guards.

## A. Mechanism and access ordering reviewed

Existing project code reads BAR0 through PCI sysfs `resource0`, maps it with
PROT_READ, and performs an aligned native 32-bit ctypes load. Earlier user
experiments established access feasibility when GPU 0 is unbound; this does not
prove safety during link transitions. The new sampler uses that mechanism but
maps only the 4 KiB page at resource offset 8C000, and dereferences only dword
8C040. It does not instantiate the old Linux backend (which also has write APIs).
Root-only helpers `RootConfig`, status decode and quantiles are reused unchanged.

Read-only resource opens use O_RDONLY/O_SYNC/O_CLOEXEC/O_NOFOLLOW and the mapping
uses **PROT_READ**, never PROT_WRITE. The GPU config fd is O_RDONLY. Only fixed
GPU `0000:03:00.0` and root `0000:00:02.0` exist as targets; no BDF/offset override
or fallback is exposed. Native dword access is restricted to x86_64.

Preflight order:

1. Open/validate only the root: Intel identity, Root Port type, PCIe cap 90,
   LNKSTA A2, DLL active reporting supported. Read root status first.
2. If Training, refuse initial preflight; if DLL inactive, lost/invalid link or
   unexpected idle width/rate, STOP. No GPU metadata/config/BAR operation follows
   an observed unsafe root state.
3. Check GPU driver entry absent (any driver, not only NVIDIA). If bound, STOP
   **before opening GPU config or BAR0**. Both GPUs can remain bound to run the
   existing root-only sampler, but not this BAR observer.
4. Confirm GPU 0's resolved parent is the fixed root and `power_state` already D0.
   Open GPU config read-only; verify 10de:1b87, find a valid PM capability, read
   MSE=1/BME=0 and PMCSR=D0. The code refuses rather than enables decode, clears
   bus mastering or wakes the GPU. Missing power_state/PM information is a STOP.
5. Check assigned memory resource 0 covers the known page; open/map read-only.
   No other BAR is opened. Recheck root/driver before any native dereference.

Every GPU config read and GPU metadata phase is guarded by a **fresh root read
and driver-entry check**. Each hot GPU load repeats power_state, identity,
command and PMCSR validation, then a final root/driver gate. The aligned dword
read is timestamped separately from those guards. A post-load root/driver check
records whether the sequential bracket became unsafe. All hot root guard reads
are included in the root stream; they are not hidden measurement traffic.

Absolute safety limitation: userspace cannot atomically lock the hardware link,
the driver's binding state and a following MMIO instruction. A link may fail or
another actor bind after the last check. Pre/post checks report detected races,
not prevent all races. A physical loss can cause SIGBUS/native MMIO failure that
Python cannot reliably recover from; a fatal process/power failure can lose the
RAM sample window. The existing cooperating-process hardware lock excludes the
project's other lock-respecting experiments, not kernel/firmware or arbitrary
external actors. No driver override, kernel module or policy write is used to
claim stronger serialization.

## B. Offline policy and synthetic verification

`design/p104_dual_plan.py` has no I/O. It defines the root gate, GPU preconditions,
known register decode and explicitly non-atomic/inconclusive interpretation.
Its four policy tests were run before adding the hardware reader. Reader/capture
coverage is in `tests/test_dual_sampler.py`, including a real PROT_READ map of a
**temporary regular file**, never a GPU. No development test opens actual sysfs
PCI devices. Dedicated Ubuntu 24.04 CI runs both new suites; full CI discovers them.

## C. Capture and first read-only test

**GPU 0 is currently reported bound to NVIDIA, so this command must refuse in
that state.** It does not unbind it. Preparing GPU 0 unbound/D0/MSE=1/BME=0 is a
separate explicitly authorized operation using the already-known per-device
procedure, outside this tool; GPU 1 must remain as-is. Do not unload the shared
NVIDIA module, and do not repair failed preconditions with ad hoc register writes.
No instruction here authorizes development-time hardware access.

Once that separate preparation has been authorized and completed, the exact
first short read-only test command, from the repository root, is:

```sh
sudo python3 pcie_dual_sampler.py \
  --duration 0.05 --max-records 100000 \
  --output-dir work/t5600-dual-first
```

The development agent has **not** run this command on hardware. There is no
apply/write/CHANGE_SPEED/unbind option, even experimental or hidden.

Capture uses one monotonic clock, not two independently running sampler threads.
Root guards and GPU loads are sequential; their stamps permit correlation, not
an atomic snapshot. Root rows are more frequent than GPU rows because safety
checks require several root reads per GPU load. There is no artificial sleep,
printing, JSON encoding, disk I/O, fsync or sorting in the hot capture loop.
Preflight itself involves filesystem reads; those are not measurement samples.

Bounds:

- Duration default 50 ms, finite >0..10 s. New reads stop at the deadline;
  synchronous in-flight calls can overrun and are included in actual elapsed time.
- Shared packed root/GPU/skip buffer, 27 bytes/record, default 100,000 records =
  2.7 MB; hard cap 1,000,000 records = 27 MB. Stop rather than overwrite. Reserve
  a record before a load; no native load is deliberately performed without room
  for its sample. Record-limit stop can truncate the requested time window.
- Analysis/output after closing the fds uses additional bounded O(N) memory for
  exact quantiles and skew statistics. The raw-buffer size is not a process RSS
  limit. JSONL is streamed after capture, so output can take longer than sampling.
- Baseline/manifest are fsynced before capture. RAM samples flush on completion or
  a handled STOP/error. This cannot promise partial sample survival after SIGBUS,
  power loss or an unhandled native failure.

## D. STOP and deliberate skips

During capture:

| Observed condition | Behavior |
| --- | --- |
| Root Training=1, including DLLLA=0 during Training | Record root + skip marker; no GPU config/metadata/MMIO access; continue bounded root observation |
| DLLLA=0 outside Training | Record explicit DLL-inactive skip, then STOP |
| Link Down, idle width not x4 or speed outside Gen1/Gen2 | STOP, no further GPU access |
| Root/GPU config short/all-ones/error, wrong GPU identity | STOP |
| GPU driver entry appears, power_state/PMCSR not D0, MSE/BME mismatch | STOP, no repair |
| BAR0 FFFFFFFF/BADF sentinel | Retain invalid sample, then STOP |
| Window ends while root remains unsafe/Training | Negative STOP/incomplete observation, not success |
| Buffer/time limit with safe root | Normal bounded end; report coverage and unpaired last load if any |

The runtime never tries to re-establish conditions. Repeated root reads after a
Training skip are passive observations, not command retries. If the post-load
root check is unsafe, the prior GPU sample stays recorded with an uncertain
sequential bracket; it is not claimed safe/atomic retrospectively. This cannot
identify exactly when an asynchronous transition started between reads.

## JSONL and report

Unique `gpu0-dual-<time_ns>.jsonl` per run:

- `manifest`: fixed targets/offsets, kernel, clock, configured bounds and no-write/
  no-GPU1/nonatomic declarations.
- `preflight` or `abort`: validated conditions or explicit refusal before capture.
- `capture_window`: common absolute monotonic clock origin and actual elapsed ns.
- `sample`: channel, cycle ID, role, read start/end/duration, raw numeric/hex data,
  decoded fields and old/new differences. Root: speed/width/Training/DLLLA/LBMS
  plus Slot Clock/LABS. GPU: **only 8C040** ACTIVE, directive, Pascal SPEED code.
- `gpu_skipped`: reason, cycle, latest root raw/Training/DLLLA. A post-GPU unsafe
  gate is distinguished as `post_gpu_gate_unsafe`.
- `summary`: STOP/error, independent per-channel timing, skip counts, guard-to-load
  skew, post-root bracket coverage, invalid samples, elapsed/requested/uncovered
  duration, buffer bounds and observability limits. An omitted skip marker when
  the buffer fills is explicitly counted; its triggering root row is retained.

Per channel p50 (median), p99 (nearest rank), max are computed independently for
read brackets, no-read gaps, start-to-start cycles and conservative read-instant
gaps. Boundary intervals are included in the conservative maximum. GPU gaps
include Training skips and state-check overhead: root timing cannot substitute
for missing ACTIVE samples. GPU **read latency** measures the native load bracket,
not the entire safety-check cycle. Guard-to-load skew reports elapsed time from
the last root read end to the GPU read start; nonzero skew and post-check status
must not be presented as simultaneous state.

Exit code 0: bounded complete stop with safe last root; 1: refusal/STOP/error;
130: handled interrupt. Manifest/refusal journals may exist even without samples.
Return the complete file for review, not only percentile summaries.

## E. Is this sufficient for a CHANGE_SPEED experiment?

It is sufficient to **measure actual dual-channel coverage and detect some
correlated activity**, not to decide command acceptance conclusively. Remaining
limitations:

1. No documented minimum GP104 ACTIVE/Training duration or per-command ACK.
2. No complete LTSSM history or guaranteed applicable Intel/NVIDIA trace producer.
3. Root-gated sampling intentionally suppresses GPU reads during the most
   interesting transitions, producing selection-dependent ACTIVE blind windows.
4. Sequential non-atomic access, root-to-GPU skew and physical/bind/PM races.
5. Native MMIO failure may be uncatchable; no automatic reset/bind recovery.
6. Extra guards change latency/load; prior root-only timing is not transferable.
7. This observer does not collect AER or enable kernel traces; prior clean AER is
   not evidence that a future internal command was accepted or ignored.
8. Unknown pulses can occur entirely between samples or during skipped windows;
   self-clear, silence or an idle end state cannot distinguish ignored, executed,
   accepted/no-state-change and missed activity. No stimulus or acceptance marker
   is manufactured to calibrate the observer.

The CHANGE_SPEED executor remains absent. Review measured channel gaps, safe/
unsafe/unpaired brackets, error outcomes and skip-window extent before another
proposal. No result of this passive sampler automatically authorizes a command.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_dual*.py' -v
python3 -m py_compile pcie_dual_sampler.py design/p104_dual_plan.py tests/test_dual_design.py tests/test_dual_sampler.py
```
