# Standalone read-only Root Port sampler for Dell T5600

Implementation: `pcie_root_sampler.py`. It is independent of the unlock program,
imports no hardware experiment/backend and exposes no write API or apply option.
It opens **only** `/sys/bus/pci/devices/0000:00:02.0/config` with O_RDONLY. It
validates Intel vendor identity, PCIe Root Port type, and a PCIe capability at
0x90 so Link Status is exactly offset **0xA2**. It then reuses this fd for 2-byte
pread calls at 0xA2. No BAR0/resource mapping, GPU config read, unbind/bind, retrain,
reset, driver policy, tracefs, VBIOS or GPU 1 access occurs. The sampler accepts
both GPUs remaining bound to NVIDIA and does not even inspect their devices.
It observes any Root Port link status; it does not demand Gen1 or cause transitions.

The user supplied preliminary measurements on Ubuntu 24.04 kernel
6.8.0-142-generic: 2000 sysfs reads, p50 3.58 us, p99 4.59 us, max read 15.12 us,
max reported blind gap 82.53 us, raw 3041 throughout. These are evidence for that
previous measurement window only, not guaranteed bounds for the new program,
and the prior blind-gap definition has not been assumed identical to ours.

## Command for the next separately requested read-only test

From the repository root, with GPUs in their current state:

```sh
sudo python3 pcie_root_sampler.py \
  --duration 0.25 --max-samples 100000 --gap-threshold-us 100 \
  --output-dir work/t5600-root-sampler
```

This command has **not** been run on T5600 by the development agent. Development
and CI use injected reads, synthetic timestamps and temporary regular files only.
There is no `--apply`, alternate BDF or alternate offset option. If Root Port
identity/type/capability offset differs, the program refuses and writes an abort
journal rather than guessing or changing anything. No need to unbind either GPU.

## Bounds and capture behavior

- `--duration`: default 0.25 seconds, finite >0 and <=10 seconds. Stop starting new
  reads at the monotonic deadline. One in-flight synchronous sysfs read cannot be
  cancelled at that deadline; an overrun is explicitly reported, not hidden.
- `--max-samples`: default 100000, range 1..1000000. Preallocated packed RAM buffer,
  **18 bytes per sample** (two uint64 timestamps, one uint16 status): default
  1,800,000 bytes, maximum 18,000,000 bytes. No ring overwrites or silent drops.
  Stop on whichever comes first: duration or capacity. A full buffer can mean
  the requested time window was not covered; the uncovered tail is reported.
- No artificial sleep, printing, JSON encoding, disk write, fsync or sorting in
  the capture loop. Small temporary pread/timestamp objects and Python scheduling
  still contribute overhead. Reads are not electrically nonintrusive: the bounded
  burst adds config traffic on this root, but requests no register writes.
- Root read error, short read or FFFF ends capture and preserves prior samples.
  KeyboardInterrupt also preserves the completed prefix. A legitimate 0000 or
  changed speed/Training status is recorded, not repaired. Other status transitions
  do not trigger any action or extra device access.
- Device fd is closed before analysis/output. Exact quantiles use bounded O(N)
  temporary analysis memory in addition to the packed buffer; at the 1M-sample
  cap this can be tens of MB. The 18 MB figure is the **raw capture buffer**, not
  an RSS limit. Output is streamed, not accumulated as per-sample dictionaries.
- Metadata/preflight are fsynced before capture, all samples/summary after capture.
  A process/power crash can lose the RAM window; per-sample durability is deliberately
  not mixed into the timing loop. Output/analysis runtime is separate from the
  bounded hardware measurement window and can take longer for large buffers.

Sysfs config access can involve kernel runtime PM; the command does not change
PM policy or wake/bind a GPU deliberately. The fixed root currently serves working
bound GPUs. Kernel/firmware behavior is not controlled by the sampler.

## What the metrics mean

Every read has monotonic start s[i] immediately before the reader call and end
 e[i] immediately after it. The actual bus read lies somewhere in this bracket.
Times in sample rows are ns relative to `capture_window.clock_origin_ns`.

| Metric | Definition |
| --- | --- |
| `read` | e[i] - s[i]; includes the Python reader call/syscall/return bracket |
| `no_read_gap` | s[i] - e[i-1]; time between completed and next initiated read |
| `cycle` | s[i] - s[i-1]; start-to-start cycle including processing overhead |
| `conservative_gap` | e[i] - s[i-1]; conservative maximum separation of two unknown actual read instants |
| `leading_no_read_ns` / `trailing_no_read_ns` | Capture boundaries before first start / after last end |
| `conservative_max_gap_including_boundaries_ns` | Maximum of conservative inter-read gaps, first end from capture start, and capture end from last start |
| `capture_elapsed_ns` | Whole timed loop including terminal check/overrun |
| `uncovered_requested_tail_ns` | Requested time remaining when capture stopped early |

Each read/gap/cycle distribution includes count, p50 (conventional median, mean
of middle pair for even N), p99 (nearest rank), max. Empty distributions are null,
not fabricated zeros. Threshold counts distinguish ordinary no-read gaps and
conservative gaps. `--gap-threshold-us` is only a reporting budget; it does not
assert a GP104 minimum pulse duration or certify command acceptance observability.
`read_completed_after_deadline` and `capture_overran_requested_window` distinguish
an in-flight late read from mere loop/end-check overhead.

## JSONL and changes

Each invocation creates an exclusive `root0-lnksta-<time_ns>.jsonl` under the output
directory. The path is printed before preflight. Events:

- `manifest`: fixed root/path/offset, kernel release, clock, requested bounds,
  quantile definitions, and explicit no-write/no-BAR/no-GPU-access declarations.
- `preflight`: validated Root Port identity/capability offset; or `abort` on failure.
- `capture_window`: absolute clock origin and actual elapsed capture time.
- `sample`: raw numeric/hex LNKSTA, bracket timestamps and interval values,
  decoded Training, DLLLA, Speed, Width, LBMS plus Slot Clock/LABS.
- `status_change`: old/new values for each changed **Training/DLLLA/Speed/Width/LBMS**,
  sample index and a conservative change window between adjacent read brackets.
  This records when a change was detected, not the exact hardware transition time;
  multiple transitions between samples can cancel and remain invisible.
- `capture_error`: error or interruption, retaining the valid sample prefix.
- `summary`: bounds/stop reason, timing distributions, field-change counts, gap
  threshold counts, first/last status and observability limitations. Valid samples
  are never discarded/overwritten; failed/invalid read attempts are counted apart.

Exit code 0: normal duration or capacity stop; 1: preflight/read failure;
130: capture interrupted (partial result retained). Summary prints to stdout after
capture, not in its hot loop. A capacity stop is normal but **not** proof the full
requested duration was sampled.

## Interpreting observability

3041 means Gen1 x4, Training=0, DLLLA=1, LBMS=0. 7041 differs only in sticky LBMS.
The sampler never clears LBMS or AER and does not manufacture a calibration event.
It does not read or claim coverage of GPU ACTIVE/command acceptance/full LTSSM.

Even unchanged status at microsecond read latencies cannot exclude a pulse shorter
than an actual inter-observation gap. The observed max gap is a bound for this
finite capture, not a future scheduling guarantee. Without an independently
established pulse lower bound or matching complete hardware history/ACK, no
sample count or quiet report proves an ignored CHANGE_SPEED command. There is
no CHANGE_SPEED executor here.

For the next test, return the complete JSONL, particularly summary stop reason,
coverage/tail, all four timing distributions and field transitions. Compare with
the previous 2000-read run only after matching units and gap definitions. A changed
link status is evidence to review, not permission to unbind or retrain.

## Synthetic verification and CI

```sh
python3 -m unittest discover -s tests -p 'test_root_sampler.py' -v
python3 -m py_compile pcie_root_sampler.py tests/test_root_sampler.py
```

Dedicated Ubuntu 24.04 CI workflow `root-sampler-tests.yml` runs this independent
suite without GPU/root privileges; the existing full suite also discovers it.
Tests validate fixed-root read-only open/offset, identity guards, exact statistics,
all requested field transitions, 2000 stable synthetic samples, bounded storage,
deadline and in-flight overrun, short/all-ones/error/interruption handling,
JSONL, separate runs and absence of MMIO/config-write/command APIs.
