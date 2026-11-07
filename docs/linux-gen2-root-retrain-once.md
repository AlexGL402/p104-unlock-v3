# One Root retrain after full ApplyLiveGroup PASS

Tool `pcie_root_retrain_once.py` implements the proposal in
[transition decision](linux-gen2-transition-decision.md#current-decision-after-successful-native-speed-only-test).
No real hardware operation was performed during development.

The **only write** is one `os.pwrite` of two bytes `60 00` to Root
`0000:00:02.0/config` offset `0xA0`, and only if current word is exactly
`0040`. The permit is consumed before the syscall, including short/error writes.
No GPU config writes, TLS writes, MMIO writes, CHANGE_SPEED, reset, binding,
manual RL clear, rollback or second retrain. Old instruments are unchanged.

## Admission and observation

Reuses existing read-only native backend, identity/topology/resource/D0/runtime,
MSE/BME, link guards, project lock and isolated JSONL supervisor. Only GPU0
`0000:03:00.0` and Root `0000:00:02.0`; no GPU1 paths or driver changes.
GPU COMMAND must be **exactly 0002**, D0 and already unbound. Root D0/MSE=1.

Before trigger, all eight DWORDs must exactly equal the full post-GROUP state:
`8872C=6, 2241C=81, 8841C=C0B40500, 8C2C0=068711B3,
8C300=040A00C0, 8C040=20045800, 8C1C0=00120032, 88610=1`.
The former VSEC=0 / cap-speed=00020032 profile is rejected, never normalized.
No ApplyLiveGroup, TLS or MMIO preparation is called.

GPU CAP=`00453C42`, CAP2=6, Root capability Gen3 x16, full GPU/Root Link Control 2 words exactly 0002/0042 (both TLS=2),
physical Gen1 x4, Root LT=0/DLLLA=1/CTL=0040. AER and Device Status errors
and pending transactions must be zero. Controls/CAP/CAP2/TLS are captured and
checked for drift. No preparation or repair of a failed condition is attempted.
BAR0 remains O_RDONLY / PROT_READ / MAP_SHARED throughout.

After the one word write, the first hardware read is Root LNKSTA. Observe Root
only for a two-second loop budget. Training is expected and never admits GPU
config/MMIO access. While Training, speed/width fields are not interpreted as
settled values. DLLLA=0, Root error/read failure or invalid idle speed/width =>
STOP. Root RL/Training/pending still set at the deadline => timeout/STOP.
Root transitions are retained, with finite sample/event limits and no fsync.

The final Root Gen2 x4 interval must cover at least 100 ms of the bounded
observation window, without observed Training/RL/pending interruptions.
This is sampled stability, not proof of continuous physical operation.

Once idle, every endpoint access again has fresh Root pre/postchecks and
GPU binding/power/COMMAND guards. Verify both link endpoints, CAP/CAP2/TLS/AER;
one safe read-only SPEED post-sample is recorded. Internal SPEED drift is FAIL,
never corrected; `gen2_observed` is retained if physical Gen2 was already verified.
Finite sequential samples do not provide atomic snapshots or an execution ACK.
The loop budget cannot preempt a blocking config syscall; the parent watchdog
is 10 seconds. An uninterruptible hardware/kernel access cannot be safely
recovered by this process. Worker core dumps are disabled.

## Current T5600 command (one separately authorized hardware invocation)

Use the **read-only** native library and mapping-review from the successful
BAR0 read test; neither requires rebuilding if unchanged. Do not supply the
SPEED/preparation library: the loader accepts only the read-only exports.

```bash
sudo python3 ./pcie_root_retrain_once.py --native-library ./work/bar0-ro-native/p104_bar0_ro.so --mapping-review ./work/t5600-mapping-review.json --apply --confirm-gpu0-unbound --confirm-exclusive-access --confirm-root-retrain-risk --output ./work/t5600-root-retrain-post-group.jsonl
```

Fresh filename required. Optional `--dry-run` instead of `--apply` checks
admission without opening the Root writer or writing; no extra hardware dry-run
is required by this implementation. Missing native binary can be built offline
with the existing `python3 -m design.p104_bar0_ro_build --directory ./work/bar0-ro-native`.

## Gen2 PASS / FAIL

Apply PASS (exit 0): final `result=PASS`, `reason=gen2_x4_both_ends`,
`gen2_observed=true`; both LNKSTA speed=2/width=4, no Training/DLL loss at
admitted checks, zero observed AER, unchanged controls/CAP/TLS and post-SPEED
`20045800`, successful cleanup. This is unbound physical-link validation,
**not NVIDIA/CUDA stability** (`nvidia_cuda_verified=false`).

Gen1 after the window, only one end at Gen2, width loss, readback/control drift,
any error or timeout => FAIL/STOP. RL self-clear/LBMS or an observed Training
pulse alone cannot pass. Missing Training/AER does not prove no transition or
that the request was ignored. A pending intent/returned syscall is not PCIe ACK.

Preserve the whole JSONL on FAIL; do not rerun, invoke another trigger, rebind
or reset. Partial/uncertain outcomes are left untouched. Real Link Down/cold
recovery remains possible; operator AC recovery also interrupts GPU1. No
recovery action is performed by this tool. SPEED is never rewritten.
