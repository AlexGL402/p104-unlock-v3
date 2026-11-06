# T5600 dual observer: cost breakdown and safe I/O comparison

New separate command: `pcie_dual_profile.py`. Existing working sampler/unlock
files are unchanged. This is a read-only profiler, not a command experiment.
No CHANGE_SPEED/config/MMIO writes, retrain/reset/bind/unbind, driver/VBIOS
changes, GPU 1 access or automatic preparation is introduced.

## What the measured 50 ms result establishes

User-run dual sampler: root 747 samples, median root cycle 55.48 us, conservative
max gap 203.54 us; GPU 124 loads, median GPU cycle 399.88 us, conservative max gap
476.71 us; root-to-GPU skew p50 42.49 us; zero skips/errors/status changes,
124/124 safe postchecks, duration stop, kernel 6.8.0-142-generic.

The ordinary complete iteration performs:

| Item | Count per complete GPU cycle |
| --- | --- |
| Root LNKSTA reads | 6 |
| Driver-entry existence checks | 6 |
| power_state open/read/close | 1 |
| GPU config reads: identity, command, PMCSR | 3 |
| Native 8C040 load | 1 |
| Final root/driver check after load | Included in the above 6 |

Four root/driver guards occur inside `_state` (before power_state and before each
config read), one immediately before MMIO, one afterward. 747/124≈6.02 agrees
with this sequence plus a partial terminal iteration. The root stream is not a
separate free-running fast loop; its spacing includes work between guards.

399.88 us is the median **GPU start-to-start cycle**, not MMIO read latency.
42.49 us skew is the last root read end→GPU load start interval: it includes root
record bookkeeping, gate logic, final driver lstat and admission/Python overhead.
The preceding state checks occur earlier and are not all inside that skew.
The supplied aggregate numbers do not identify which syscall dominates; it is
incorrect to assign the whole 400 us to MMIO or subtract unrelated p50 values.
A quiet run confirms sampled stability, not absence of short pulses.

## What is measured

`--profile` adds a bounded in-memory nested phase recorder. All timestamps use
the capture's monotonic clock. Its phase spans are emitted only after fds close:

| Phase | Scope |
| --- | --- |
| cycle_total | read_gpu entry through successful postcheck; includes sample-store/decode between them |
| state_checks | power/identity/command/PMCSR checks, including their guards |
| final_guard | Last root/driver guard before MMIO |
| load_admission | Time/buffer-limit admission |
| mmio_load | Same start/end bracket as the GPU sample; includes Python/ctypes operations, not isolated bus latency |
| postcheck | Root/driver post-load check |
| guard | One complete root read + record/gate policy + driver probe |
| driver_binding | Fresh driver-entry probe |
| power_state | Fresh attribute open/read/parse/close |
| config_identity / config_command / config_pmcsr | Individual GPU pread/return bracket; guarding is separate |

The top-level non-overlapping partition is state_checks + final_guard +
load_admission + mmio_load + postcheck + residual. **Residual is calculated for
each successful complete cycle**, then summarized. Nested guard/driver timings
must not be added to parent totals; p50 values of components need not sum to the
p50 cycle. Residual includes wrapper/timestamp/span bookkeeping, sample packing,
decode/return/call overhead between top-level phases; it is not all kernel time.
The original start-to-start channel cycle statistics remain available separately.
Failed and incomplete spans are marked and excluded from successful-cycle totals.
Phase reports include successful timings and all finished timings separately.

Per-phase p50/p99/max, call counts per complete cycle, cycle residual, root/GPU
coverage, skips, skew and postchecks are retained. The expected six guard/driver
checks and all three config reads are tested in both I/O styles. The result is
instrumented wall time, not CPU-time attribution or a kernel/hardware trace.

A 256-pair monotonic timer calibration runs without device I/O before capture.
It is reported, **not subtracted**: it does not model all Python/profiler overhead.
Profiling adds calls and RAM stores and can enlarge the guard-to-load race window;
it is not a zero-overhead measurement. Compare modes with the same instrumentation,
then measure coverage again without `--profile`.

## Two I/O styles, identical safety-check cadence

`--io-style reference` follows the existing metadata operations and constructs
a ctypes object per load. It preserves hardware-access order, not byte-for-byte
Python execution timing of the original executable.

`--io-style fresh-fast` makes only these optimizations:

1. Prebuild path strings and call **fresh os.lstat** for driver on every guard.
   No cached negative result, TTL or every-N-cycles thinning.
2. Use fresh read-only open/pread/close for power_state on **every cycle**,
   avoiding the text-file wrapper/general decoder. Keep the fd fresh to avoid
   assuming persistent sysfs seq-file data is regenerated for each pread. Require
   full short D0 text; truncated/unexpected data is refused. PMCSR is still read.
3. Construct the ctypes view for the already-read-only-mapped known dword once.
   Access `.value` for a **fresh native load each cycle**, never cache register data.
   Drop the view before unmapping.

Neither style removes, batches, delays or reduces root/driver, power_state,
identity, MSE/BME or PMCSR checks. The last root/driver guard remains before every
MMIO read, and the postcheck remains after it. Existing fixed BDF/topology,
Root DLL reporting, resource/PM checks and all STOP/Training-skip semantics are
inherited from the reviewed reader. Endpoint config fds were already persistent
and remain so. Driver, identity, power and command **values are not cached**.

No speedup on T5600 is claimed before hardware profiling. Optimizations may have
small effect if actual config/metadata kernel work or scheduling dominates.
Further possible work, contingent on measurements, is reducing Python overhead
in a small audited read-only native loop with the same guards. Not included:
checking safety every N cycles, cached driver/D0/MSE/BME, persistent power_state
text snapshots without freshness proof, speculative BAR reads, extra threads
reading GPU through an unsafe root state, arbitrary register/ECAM mappings.

## Commands for separately authorized read-only profiling

GPU 0 must already be unbound/D0/MSE=1/BME=0. Bound driver or unsafe root still
causes refusal; this tool never prepares the device. GPU 1 remains unchanged.
The following commands have **not** been executed on hardware by the developer.
Run each separately, retaining its complete journal:

```sh
# First obtain an instrumented baseline with reference I/O:
sudo python3 pcie_dual_profile.py --profile --io-style reference \
  --duration 0.05 --max-records 100000 --max-profile-spans 100000 \
  --output-dir work/t5600-profile-reference
```

```sh
# Compare fresh-fast with the same instrumentation and bounds:
sudo python3 pcie_dual_profile.py --profile --io-style fresh-fast \
  --duration 0.05 --max-records 100000 --max-profile-spans 100000 \
  --output-dir work/t5600-profile-fresh-fast
```

```sh
# Then measure fresh-fast coverage without nested phase recording:
sudo python3 pcie_dual_profile.py --io-style fresh-fast \
  --duration 0.05 --max-records 100000 \
  --output-dir work/t5600-fresh-fast-coverage
```

For a symmetric non-profiled A/B, run the last command separately with reference
style and another output directory. There is no automatic chain, command stimulus
or automatic rerun after a STOP. If state or external workload changes between
runs, record it and do not call the difference a causal speedup.

## Storage and interpretation

Existing raw sample buffer: default 100000×27 bytes=2.7 MB. Optional profile buffer:
default 100000×23 bytes=2.3 MB, capped at 1M spans=23 MB. With profiling disabled,
profile allocation is zero. Reserve a slot **before** entering an instrumented
phase, including the native load; when exhausted, stop as `profile_limit`, without
silently dropping spans. Parent slots are reserved before children and completed
in place, so JSONL span order is nesting/allocation order, not completion order.

Exact post-capture statistics use additional bounded O(N) analysis memory.
No profile sorting, printing, JSON serialization, disk write or fsync is done in
the capture loop. Preflight/manifest are fsynced before it; all results afterward.
As before, in-flight reads can overrun the requested deadline, and native SIGBUS,
power loss or process death can lose the RAM window. No recovery writes follow.

Each separate `gpu0-dual-profile-<time_ns>.jsonl` contains manifest/I/O style,
calibration, preflight, common capture window, `profile_span` phase/cycle/bracket/
complete-failed-incomplete status, original dual samples/skips and summary with
`profile`. Non-profiled runs keep the original dual metrics but no nested spans.
Check duration/record/profile limit, complete cycles, safe/unsafe/unpaired postchecks
and both-channel conservative gaps before comparing latency. A faster median
with a worse max blind gap is not necessarily a better observer.

Even genuinely faster polling supplies no documented command ACK, full LTSSM
history or minimum ACTIVE pulse duration. Training skips, sequential skew and
asynchronous bind/link/PM races remain. Do not infer ignored/executed CHANGE_SPEED
from no observed transition, and do not treat profiling success as permission to
implement or issue that command.

## Tests and CI

```sh
python3 -m unittest discover -s tests -p 'test_dual_profile.py' -v
python3 -m py_compile pcie_dual_profile.py tests/test_dual_profile.py
```

Dedicated Ubuntu 24.04 workflow plus existing full/dual discovery. Synthetic tests
use ordinary temporary files and read-only mappings, never actual sysfs hardware.
They verify six root/driver checks in each style, fresh power/identity/command/PM,
late binding/Training/DLL STOP, cached-view fresh loads, additive same-cycle
partitions, bounded profile reservation, no hot fsync, disabled profile storage,
JSONL and absence of writer APIs. Existing working tool files remain unchanged.
