# T5600: staged Linux Gen2 experiment

Status: **experimental, no hardware-validated Gen2 unlock**. The user ran the
original two-switch Stage 1 on T5600: both exact readbacks succeeded, but Gen2
LNKCAP2 did not appear within two seconds. Stage 2 was not run, GPU 1 was unchanged.
The user subsequently ran `stage1-override`: all three readbacks matched, GPU
LNKCAP2 changed 2→6 immediately after 8841C, but LNKCAP stayed Gen1 and both links
stayed Gen1 x4. The script stopped at `inconsistent_capability`, without Link Down.
The new `stage1-cap-speed` has only offline validation. No hardware operations were
performed by the development agent.
No VBIOS/EFI/driver installation changes, startup service, automatic rebind,
reset, width arm, recovery writes, or background holder are implemented.

## Evidence and limits

Base: `linux-gen2`, commit `16f39946d95afe4c63e92404cb0e8bd89c39ac5e`.
Target: Dell Precision T5600, Ubuntu 24.04, NVIDIA 580.196.07,
GPU 0 `0000:03:00.0` under `0000:00:02.0`, 10de:1b87,
VBIOS 86.04.7A.00.47. GPU 1 (`0000:04:00.0`, root `0000:00:03.0`)
is deliberately not selectable. PCI ID is checked; VBIOS, machine and driver
versions are operator-supplied facts, not verified by this program.

Source review (files unchanged from base):

| Source | Actual behavior | Consequence |
| --- | --- | --- |
| `src/P104Gen2x4.cs`, `ApplyLiveGroup` (around line 662) | 8872C → 2241C → 8841C → 8C2C0 → 8C300 → 8C040 → 8C1C0 → 88610, then `LiveLatch` | Linux eight-register ordering was not a transcription error. |
| `tools/apply_gen2_width.py`, `LIVE_GROUP`, `live_group`, `live_latch` | Same full group, capability polling after group | Neither implementation demonstrates success with fuse/speed writes removed. |
| `src/P104Gen2x4.cs`, `WriteTlsBoth`, `RetrainBoth` | TLS on both ends; C# can retrain both ends | Linux experiment uses only a single root trigger, like Python `root_retrain`. |
| `P104-methodology-AI-agent.md`, lines 105–114 | Calls 8841C/2241C declaration switches; explicitly says declaration and speed request must be combined | Two switches are a minimal hypothesis for capability discovery, not a proven complete unlock. |
| Same document, around line 176; C# around line 1984 | Reports driver lowering SPEED at CUDA context initialization | Gen2 before driver rebind/CUDA is insufficient evidence of usable stability. |
| `src/P104Gen2x4.cs`, `Is8C040Legal` | Actual return is `arm31 || bit29`, despite comments prohibiting both set | A bit-combination guard is not a hardware safety proof; the new path forbids every write to this register. |

Reported incident: first three **actual** BAR0 writes succeeded; Link Down occurred
on `0x8C040: 0x20085800 → 0x20045800`, before TLS/retrain. Both CYA operations
are no-ops at the supplied baseline, so SPEED is the fourth actual write, even
though sixth in the patch list. XOR is `0x000C0000`: SPEED changes 2→1;
width bits and trigger bits 0/1 do not change. Thus neither wrong width quadrant,
nor the later root retrain, explains this incident. Direct proprietary SPEED
programming while live, interaction with already-enabled FUSE_OVERRIDE, and
NVIDIA driver ownership are plausible causes. The observations do not distinguish
them. Successful unbind/rebind and unchanged unbound register reads establish
access feasibility, not safety of SPEED writes without the driver.

FUSE_OVERRIDE (`0x8872C`, low nibble 6) is described in Python as
`CHANGE_SPEED_WIDTH=3`, and is present in all successful source sequences.
There is **no source-backed proof it is optional**. The new experiment deliberately
leaves it at zero and SPEED at `0x20085800` to avoid repeating the failed
transition or adding another proprietary speed/width override. Standard TLS and
root retrain may be insufficient while proprietary limits remain. A no-latch or
Gen1 result rejects this minimal hypothesis for this baseline; the program must
stop, not append the old group or retry. Adding fuse/SPEED writes would require
a new reviewed experiment and separate hardware authorization.

## Implementation contract

- Default/probe and plans perform reads only; importing the module has no I/O.
- Write preflight: exact GPU/root, GPU already unbound, D0, memory decode on,
  bus mastering off, both links idle Gen1 x4, root advertises Gen2, exact supplied
  BAR0 baseline. These checks abort rather than repairing PCI command/PM state.
- Stage 1: only `2241C |= 0x81`, then `8841C &= ~0x6000`; preserve every other
  bit, flush via aligned dword readback, observe capability for at most 2 seconds.
  At most two writes; no PCI writes or retrain. Both LNKCAP2 vector bits 1/2
  and LNKCAP maximum speed must advertise Gen2 before reporting success.
- Stage 1 override: separate `stage1-override` mode with its own BAR0 allowlist
  `8872C → 2241C → 8841C`, factory baseline only. No config write permission,
  TLS, retrain, reset, driver bind or CUDA. See the next section for early-stop
  behavior and its independent journal. Original Stage 1 cannot write 8872C.
- Stage 2: separate invocation and explicit `--authorize-stage2`; recheck current
  capability, both link states, and baseline with exactly the two Stage 1 changes.
  Set both TLS fields to 2 with 16-bit writes preserving other bits; verify both;
  trigger root LNKCTL Retrain once. Read-only observation requires both LNKSTA
  speed=2, negotiated width=4, training clear, Link Disable clear for 2 seconds.
- Any error stops. No retry, reset, automatic rollback, or automatic driver rebind.
  PCI all-ones/short reads and BAR all-ones/BADF sentinels fail closed. Before every
  BAR access after a write, config identity is checked. Native aligned 32-bit MMIO
  loads/stores are restricted to x86_64; config writes are 16-bit, avoiding adjacent
  status W1C fields. Config checks cannot eliminate a physical-loss race/SIGBUS.
- JSONL records intent durably (`fsync`) before every write and readback afterward.
  Failed operations may leave the two switches or TLS modified: power-cycle recovery
  remains manual. Logs are not permission tokens; Stage 2 validates live state.

## Override-prefix experiment: `stage1-override` on GPU 0

The negative Stage 1 result establishes that the two switches alone did not latch
Gen2 within the measured window on this machine. It does not establish that SPEED
must be written. This experiment tests the source-ordered **three-register prefix**
of C# `ApplyLiveGroup` / Python `LIVE_GROUP`, with observation between writes,
instead of repeating the entire group. FUSE_OVERRIDE was present in those source
paths and absent from the unsuccessful minimal experiment. Its sufficiency and
safety without the rest of the group are not established by the sources.

The entire eight-register BAR0 baseline must match exactly before the first write:

| Offset | Required factory value |
| --- | --- |
| `0x8872C` | `0x00000000` |
| `0x2241C` | `0x00000000` |
| `0x8841C` | `0xC0B46500` |
| `0x8C2C0` | `0x068711B3` |
| `0x8C300` | `0x040A00C0` |
| `0x8C040` | `0x20085800` |
| `0x8C1C0` | `0x00010032` |
| `0x88610` | `0x00000000` |

**The retained values from the user's Stage 1 (`2241C=81`, `8841C=C0B40500`)
are not accepted.** Do not run the apply command against that state. Establishing
the factory baseline is a separate hardware action requiring its own permission;
this mode does not restore registers, rebind, reset or power-cycle anything.
No repeat of the already-established unbind/rebind diagnostics is required.

With factory baseline established and GPU 0 already unbound using the known
per-device method, inspect the read-only plan first:

```sh
sudo python3 p104_gen2_linux.py --mode stage1-override \
  --log-dir work/t5600-override-plan
```

This only prints the actual registers and proposed three-write sequence. Apply
performs the strict baseline and ownership/D0/MSE/BME/link preflight. Do not run
the following command until a separate authorization for actual hardware writes:

```sh
sudo python3 p104_gen2_linux.py --mode stage1-override --apply \
  --confirm-bdf 0000:03:00.0 --log-dir work/t5600-override
```

Maximum write sequence, once each:

1. `8872C: (old & ~0xF) | 6` → `0x00000006`.
2. `2241C: old | 0x81` → `0x00000081`.
3. `8841C: old & ~0x6000` → `0xC0B40500`.

After each write: identity check, exact dword readback (posted-write flush), and
raw GPU **and Root Port** LNKCAP, LNKCAP2, LNKSTA, LNKCTL and LNKCTL2 snapshot.
Observe for up to two seconds per step using reads only (at most about six seconds
total observation plus I/O). Each observation also rechecks the last readback.
Before the next write, verify the entire expected BAR0 prefix state again.
No masked-write verification hides a change outside the intended bits.

If GPU LNKCAP2's Gen2 support bit appears after the first or second write, stop
without issuing the next write. Complete capability success additionally requires
both ends' LNKCAP maximum speed >=2 and LNKCAP2 vector bits 1/2 set. A partial or
inconsistent announcement also stops, with a negative result, rather than finishing
the group. If GPU LNKCAP changes ahead of LNKCAP2, the same conservative stop applies.
Throughout this capability-only experiment both negotiated links must remain idle
Gen1 x4; training, changed speed/width, Link Disable, or inactive DLL when reporting
is supported cause an abort. A change to Gen2 link state itself is an unexpected
transition to review, not permission to continue.

Every run creates a separate exclusive JSONL file:
`gpu0-stage1-override-<time_ns>.jsonl` in the chosen directory. The path is printed
before hardware access, including runs which later fail. Every row includes mode
and apply/plan status, and is flushed/fsynced. Important events:

- `override_baseline`: exact expected and actual BAR0 maps.
- `before_mmio`, `after_mmio`: step, offset, intent and actual readback.
- `override_sample`: step, observation phase, readback and raw links on both ends.
- `override_result`: positive capability or negative `no_latch` /
  `inconsistent_capability`, number of writes, last offset, early-stop flag.
- `abort`: failure reason; no automatic recovery writes. If device access is lost,
  a complete after-write snapshot may be unavailable; the durable intent and abort
  remain, and BAR0 is not accessed after detected config loss.

Return the complete JSONL for review, whether positive or negative. Capability
success is not Gen2 x4 link success. **Do not launch Stage 2 after this experiment**:
its existing baseline deliberately rejects FUSE_OVERRIDE=6 and any early prefix.
There is no chained TLS/retrain/CUDA step, no retries and no automatic rollback.
Writes to `8C040`, `8C1C0` and `88610` are denied by the backend allowlist.
GPU 1 and its root remain outside this experiment.

## New experiment: `stage1-cap-speed` on GPU 0

Purpose: test whether `8C1C0[17:16]` controls the still-Gen1 LNKCAP maximum-speed
field, independently of LNKCAP2 and a request to change the physical link rate.
[Nouveau gk104.c](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/nouveau/nvkm/subdev/pci/gk104.c)
uses mask `0x30000` in `gk104_pcie_set_cap_speed()` / `gk104_pcie_cap_speed()`;
[envytools pci.xml](https://github.com/envytools/envytools/blob/master/rnndb/bus/pci.xml)
names bits 17:16 `LNK_CAP_SPEED`. These are related-family implementation and
register descriptions, not a guarantee of P104 behavior or write safety. The
repository's older label "target speed [19:16]" should not be used for this test.
`8C040` requests a link-rate change separately; `88610` is not modified to keep
this a single-variable capability experiment.

**Authorization to prepare code does not authorize running either hardware step.**
The commands below are instructions only; none were run by the development agent.

Strict preconditions (also checked by dry-run):

- GPU 0 `0000:03:00.0`, ID 10de:1b87, parent `0000:00:02.0` with Intel identity.
- GPU already unbound, D0, memory decoding on and bus mastering off; both links
  idle Gen1 x4. Link Disable/Retrain/Training or inactive reported DLL aborts.
- GPU LNKCAP exactly `0x00453C41`, LNKCAP2 exactly `0x00000006`; root advertises
  Gen2 and remains Gen1 x4. JSON integer **4537409 decimal** is `0x00453C41`;
  the literal `0x04537409` is different and will be rejected.
- Every known BAR0 register must match the following map:

| Offset | Required value before cap-speed |
| --- | --- |
| `0x8872C` | `0x00000006` |
| `0x2241C` | `0x00000081` |
| `0x8841C` | `0xC0B40500` |
| `0x8C2C0` | `0x068711B3` |
| `0x8C300` | `0x040A00C0` |
| `0x8C040` | `0x20085800` |
| `0x8C1C0` | `0x00010032` |
| `0x88610` | `0x00000000` |

**After full power removal the override prefix is lost.** This mode refuses the
factory baseline, missing/partial prefix, and already-modified cap-speed state.
It never calls `stage1-override`, restores registers, or asks the driver to prepare
anything. If a fresh prefix is needed, its preparation is a **separate operation**:
review the existing override procedure, obtain explicit permission for its three
writes, then run that mode alone from its required factory baseline:

```sh
# ONLY after separate authorization for preparing the prefix:
sudo python3 p104_gen2_linux.py --mode stage1-override --apply \
  --confirm-bdf 0000:03:00.0 --log-dir work/t5600-prefix-preparation
```

Do not chain this command to cap-speed. Review the prefix journal first. The
previously observed `inconsistent_capability` is a negative result of that mode,
not an automatic go-ahead; cap-speed is designed specifically to require the
verified LNKCAP=Gen1 / LNKCAP2=6 state after that result. A prefix which stops
earlier, loses the device, or changes any required value is not an accepted baseline.
Restoring the factory baseline when needed is outside these automatic modes.

Read-only cap-speed validation/plan (fails if any prerequisite does not match):

```sh
sudo python3 p104_gen2_linux.py --mode stage1-cap-speed --dry-run \
  --log-dir work/t5600-cap-speed-plan
```

Omitting both `--apply` and `--dry-run` also selects the same read-only plan.
`--dry-run` and `--apply` are mutually exclusive. Only after review and **separate
permission for the single cap-speed hardware write**:

```sh
sudo python3 p104_gen2_linux.py --mode stage1-cap-speed --apply \
  --confirm-bdf 0000:03:00.0 --log-dir work/t5600-cap-speed
```

The only permitted write is:

```text
offset = 0x8C1C0
mask   = 0x00030000
old    = 0x00010032
new    = (old & ~mask) | 0x00020000 = 0x00020032
```

Backend permissions allow only that address/value/old-value transition, once per
backend instance; all override, SPEED and VSEC writes are denied. PCI config writes
are disabled. Full preflight is repeated before durable write intent. After the
single write, record full readback and both-end LNKCAP/LNKCAP2/LNKSTA plus controls;
observe in a two-second monotonic window, with no retry writes. Polling sleeps are
bounded by the remaining window; individual filesystem/MMIO/logging calls are
synchronous and cannot have a guaranteed wall-clock deadline. Exact readback and
all other known BAR0 values must remain unchanged except the intended transition.
Control fields and root capabilities must not change either.

The experiment then ends in every case:

- `capability_consistent`: GPU LNKCAP is exactly `0x00453C42`, LNKCAP2 remains 6,
  both actual links remain Gen1 x4. This is **capability success only**.
- `lnkcap_unchanged`: full readback succeeded but GPU LNKCAP remains `0x00453C41`.
  Record the negative result and stop with failure, no additional writes.
- Loss, full-readback mismatch, unexpected capability/control/BAR0 change, training,
  or any negotiated-link change: abort immediately. No automatic recovery.

Each invocation creates an exclusive `gpu0-stage1-cap-speed-<time_ns>.jsonl`.
`cap_speed_baseline` stores expected/actual BAR0 and links; `before_mmio` includes
the mask/old/new; `after_mmio` stores full readback; `cap_speed_sample` stores raw
both-end values in numeric **and explicit hex** form. `cap_speed_result` records
the outcome, `writes=1`, `link_gen2_x4=false`, `retrain_performed=false`; failures
retain intent and `abort`. Mode/apply status is recorded in every fsynced row.
Dry-run has its own journal but no write intent. Supply the complete journal for
review before proposing another experiment. Do not run Stage 2 after this mode:
its baseline intentionally rejects the retained override/cap-speed values.

No TLS, retrain, reset, bind, CUDA, background hold, startup task or automatic
prefix preparation is part of this mode. GPU 1, VBIOS and driver installation
remain outside its scope.

## Original two-switch procedure (user reported no latch; do not repeat)

The following documents the original Stage 1/Stage 2 interface. It is not a
fallback or continuation of `stage1-override`; use the new procedure above for
the next proposed experiment.

Existing unbind/rebind and BAR0-read diagnostics need not be repeated. Before any
hardware experiment, review the Stage 1 plan and obtain separate authorization
for Stage 1. Leave GPU 1 and its root untouched. Close CUDA clients for GPU 0 and
use the already-tested per-device unbind method; do not unload the shared NVIDIA
module. The tool never performs this step. If D0/decode/bus-master preconditions
fail, stop and review instead of modifying them ad hoc.

Read-only plan (when ready to access target hardware):

```sh
sudo python3 p104_gen2_linux.py --mode stage1 --log-dir work/t5600-stage1-plan
```

Only after explicit Stage 1 authorization:

```sh
sudo python3 p104_gen2_linux.py --mode stage1 --apply \
  --confirm-bdf 0000:03:00.0 --log-dir work/t5600-stage1
```

Review `stage1_result` and raw links. It is capability success, **not Gen2 link
success**. If no latch, readback mismatch, Link Down, or any error: stop, retain
JSONL and relevant kernel log, do not run Stage 2, do not retry. For a lost card
use the previously successful full power-off recovery; this program does not do it.
Review GPU 0 results before considering any future GPU 1 experiment.

Only after reviewing Stage 1 output and receiving **new explicit Stage 2 permission**:

```sh
sudo python3 p104_gen2_linux.py --mode stage2 --apply --authorize-stage2 \
  --confirm-bdf 0000:03:00.0 --log-dir work/t5600-stage2
```

A Stage 2 failure ends this experiment. No SPEED/fuse fallback is available.
If both ends sustain Gen2 x4, manually rebind only GPU 0 using the known working
method, with authorization for this verification step. Do not change GPU 1.
Then, with explicit permission to initialize CUDA on GPU 0:

```sh
sudo python3 p104_gen2_linux.py --mode verify-cuda --authorize-cuda \
  --confirm-bdf 0000:03:00.0 --log-dir work/t5600-cuda
```

This opens a read-only hardware backend, checks both LNKSTA for 2 seconds,
selects the CUDA device by PCI bus ID, creates a context, allocates 4 KiB, verifies
a patterned host→device→host transfer and synchronization, and checks both link
states again for 10 seconds. It requires installed `libcuda.so.1`, not a CUDA
Toolkit install. CUDA initialization is an explicit driver action, not part of
probe/stages, and may itself change link state through the NVIDIA driver.
No context or workload is created on GPU 1. Failure, CUDA error, or a return to
Gen1 is not success; no automatic register correction follows.

To claim **stable** Gen2 x4, additionally run an authorized sustained CUDA workload
pinned to GPU 0 by UUID/BDF, with before/during/after endpoint **and root** LNKSTA
samples, CUDA correctness and transfer results, and kernel AER/Xid review. Record
at least 30 minutes including idle/load transitions and new CUDA contexts. Check
GPU 1 continues its existing workload without reconfiguring it. A 4 KiB smoke test
and a 10-second observation do not establish throughput or long-term stability.
Attach JSONL and workload results to the PR; none are currently available.

## Offline tests

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile p104_gen2_linux.py tests/test_linux_gen2.py tests/test_cap_speed.py
```

Tests use injected config/BAR state, simulated time, and a fake CUDA library.
They exercise both success and injected failures without sysfs access, CUDA
initialization, hardware writes, resets or retraining. They validate control flow,
not NVIDIA register semantics or T5600 electrical behavior.
