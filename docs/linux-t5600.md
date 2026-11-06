# T5600: staged Linux Gen2 experiment

Status: **experimental, not hardware validated**. This change removes the write
that caused the reported Link Down; it does not prove that the remaining path
can unlock or retain Gen2. No hardware operations were performed during development.
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

## T5600 validation procedure (not executed)

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
python3 -m py_compile p104_gen2_linux.py tests/test_linux_gen2.py
```

Tests use injected config/BAR state, simulated time, and a fake CUDA library.
They exercise both success and injected failures without sysfs access, CUDA
initialization, hardware writes, resets or retraining. They validate control flow,
not NVIDIA register semantics or T5600 electrical behavior.
