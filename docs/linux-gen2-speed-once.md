# First one-shot SPEED-only test (not run during development)

`pcie_speed_once.py`: fixed GPU0 `0000:03:00.0`, Root `0000:00:02.0`.
One permitted store: `8C040: 20085800 -> 20045800`, mask `000C0000`.
CHANGE_SPEED bit0 stays zero. This is **the same final value as the old crash**;
the new single-DWORD access path removes the identified software hazard but does
not prove that SPEED itself is safe. GPU0 loss/cold recovery remains possible.

## Current factory baseline: NO-GO

The successful factory read-only PASS admits the read path, not this write.
This tool refuses factory state. Required BEFORE invocation, as separate,
explicitly authorized operations (never performed by this tool):

1. Override prefix in order `8872C=6 -> 2241C=81 -> 8841C=C0B40500`.
2. `8C1C0=00020032`; GPU LNKCAP must be `00453C42`, LNKCAP2 `6`.
3. GPU and Root TLS must already be 2; physical link remains idle Gen1 x4.
   **Do not use stage2-cap-retrain merely to set TLS**: it also retrains.
   A TLS-only preparation requires its own explicitly authorized operation.

These change override/capability/config state, with independent risks of
inconsistency, power/binding races and link failure. They are not implied by a
SPEED-only invocation. No automatic chain or guard bypass exists.
Other DWORDs must remain exactly: `8C2C0=068711B3`, `8C300=040A00C0`,
`88610=0`, `8C040=20085800`. Root must advertise Gen3 x16 / speeds 2.5–8 GT/s;
both AER capabilities must be available with zero status. Full CAP/CAP2/CTL2
snapshots are retained and checked for drift; TLS=2, compliance controls off.

GPU0 already unbound from any driver, D0/runtime active, MSE=1/BME=0; Root
D0/MSE=1, Training=0/DLLLA=1. Operator excludes simultaneous binder/PM/config
writers. Reuse the valid mapping-review JSON from the successful read-only test
if kernel and BAR0 range still match; there is no new broad audit requirement.

## Commands, from repository root

Build without hardware access:

```bash
python3 -m design.p104_speed_build --directory ./work/speed-native
```

**Only after separate preparation and permission for the actual SPEED write**, run once:

```bash
sudo python3 ./pcie_speed_once.py --native-library ./work/speed-native/p104_speed_once.so --mapping-review ./work/t5600-mapping-review.json --apply --confirm-gpu0-unbound --confirm-exclusive-access --confirm-speed-only-risk --output ./work/t5600-speed-once-first.jsonl
```

Use a new log name. Optional `--dry-run` instead of `--apply` (risk flag can be
omitted) performs the same admission reads with no RW mapping or store; a new
hardware dry-run is not a prerequisite if using the guarded apply invocation.
No flags select another GPU, another register/value, force, retries or rollback.

## PASS / FAIL

Mapping open/close never initializes device words. RO preflight is unmapped
before ordinary resource0 is reopened O_RDWR / MAP_SHARED / PROT_READ|PROT_WRITE.
The native operation consumes its one-shot permit before checking the exact old
DWORD and performing the audited scalar store. It cannot be rearmed, including
a failed old-value check. Compiler audit accepts only `mov DWORD PTR [rdi],esi;
ret` for the store helper. Readback is a separate explicit guarded DWORD load.
No config writes, binding, reset, retrain, width or other MMIO stores exist.

First Root check follows the native call before endpoint access. Training/DLL
loss => STOP without GPU readback. Root/GPU D0/COMMAND/resource guards remain
fresh. Nonzero AER stops before further endpoint access once observed. One exact
readback is followed by at most 2 seconds / 100000 Root-only polls, final guarded
link/config checks and cleanup. Gen1 or Gen2 is allowed after the store, x4 only.
Sequential observations cannot exclude transient events between reads.

PASS means one returned store call, full `20045800` readback, safe observed link
and unchanged capability/TLS, zero observed AER, successful cleanup. Remaining
Gen1 is a PASS of this limited write step, **not a Gen2 success**. The separate
`gen2_observed` flag means both ends finally reported 5 GT/s x4; it does not
establish NVIDIA/CUDA stability (`gen2_unlocked_proven` stays false).
FAIL means STOP: preserve the entire JSONL; no second command, rollback or probing
GPU after link loss. Store intent/CPU return alone does not prove PCIe completion.
SIGBUS/SIGSEGV/timeout is logged by the isolated parent; completion may be unknown.
No automatic recovery. Full AC recovery, if needed, requires an operator window
and interrupts GPU1; the tool never accesses GPU1. No CUDA/bind is performed.
