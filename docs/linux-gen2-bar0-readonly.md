# First read-only BAR0 pass on T5600

Tool: `pcie_bar0_readonly.py`; native backend `design/p104_bar0_ro.c`.
Only GPU0 `0000:03:00.0` / Root `0000:00:02.0`. One pass, eight allowlisted
DWORDs; no device write function, SPEED executor, fallback or retry. Existing
hardware tools are unchanged. No actual device test has been run during development.

## Before running

GPU0 must **already** be unbound, D0, runtime active, MSE=1/BME=0, Gen1 x4;
Root must be D0 with memory forwarding enabled and idle Gen1 x4 with DLLLA
reporting active (Root BME need not be zero). No simultaneous bind,
runtime-PM/config writer or other experiment. The advisory project lock excludes
cooperating tools only. Do not unbind GPU1. Factory state after cold boot is a
valid read-only baseline; there is no automatic prefix preparation.

The approved integration plan requires a mapping review. Supply
`work/t5600-mapping-review.json` with these fields, using the actual BAR range
from existing T5600 records (`resource` first line: start, end, flags in hex;
size = end-start+1). Addresses/sizes below must be JSON integers:

```json
{
  "gpu": "0000:03:00.0",
  "root": "0000:00:02.0",
  "kernel_release": "6.8.0-142-generic",
  "bar0_start": 0,
  "bar0_size": 0,
  "effective_type": "UC",
  "kernel_mapping_verified": false,
  "conflicting_aliases_excluded": false,
  "evidence": ""
}
```

This is an intentionally **invalid template**, not evidence. Replace range,
review booleans, type (`UC` or `UC-`) and evidence with the actual reviewed
kernel device-mapping path/PAT-MTRR findings. Normal resource0 is selected,
never resource0_wc; neither its filename nor O_SYNC proves effective UC.
The file is an operator review assertion, not automatic cache certification.
Absent/mismatched review => FAIL before mmap. Do not invent evidence to pass.
No new broad forensic investigation is required.

## Exact commands (from repository root)

Build as the ordinary user, without device access. Requires cc and binutils:

```bash
python3 -m design.p104_bar0_ro_build --directory ./work/bar0-ro-native
```

After the review file is ready, run **once** (requires permission to read config
and resource0 and acquire the existing lock):

```bash
sudo python3 ./pcie_bar0_readonly.py --native-library ./work/bar0-ro-native/p104_bar0_ro.so --mapping-review ./work/t5600-mapping-review.json --baseline factory --confirm-gpu0-unbound --confirm-exclusive-access --output ./work/t5600-bar0-readonly-first.jsonl
```

Use a fresh output filename; existing files are never overwritten. If separately
prepared cap-speed state is actually present, explicitly choose `--baseline
cap-ready` instead. That selects expected values only, never prepares hardware.
Do not change profiles and rerun merely to bypass a failed baseline.

## PASS / STOP and next decision

PASS (exit 0): all eight exact expected DWORDs read with safe Root pre/postchecks,
no guard deviation/new AER status, and successful unmap/close. JSONL ends with
`result=PASS`, `loads=8`, `reason=one_pass_complete`; retains initial/final
CAP/CAP2/STA/CTL2. Root/GPU reads are sequential, not an atomic snapshot.

FAIL/STOP (nonzero): any failed guard, mapping/IO error, sentinel or baseline
mismatch, new AER status, deadline or fatal signal. No next DWORD, retry,
rollback, recovery, reset, retrain or bind. Parent logs SIGBUS/SIGSEGV/timeout;
worker core dumps are disabled. A pending load_begin is not proof of a load.
After link loss do not probe GPU; retain the JSONL and arrange operator recovery
if necessary. Full AC recovery interrupts GPU1 too. Missing AER is not proof
that a physical transition/failure did not occur.

Return the **whole JSONL**, including any FAIL. Review actual mapping evidence,
all eight DWORDs and link/guard results. A factory PASS admits the read path,
not a SPEED write: prepared prefix/capability/TLS remain separate authorized
steps. A cap-ready PASS still needs consistent Gen2 CAP/CAP2 and TLS=2 before
a separate SPEED-only decision. The previous crash is not proven exclusively
caused by pack_into. SPEED/CHANGE_SPEED remain NO-GO in this tool.

Remaining risks: userspace TOCTOU with link/power/binding changes, effective
mapping evidence accuracy, and a native MMIO read that faults/hangs after a
safe precheck. Process isolation and a 10-second parent deadline bound ordinary
failure handling, but cannot terminate an uninterruptible kernel access or
prevent a platform-level PCIe fault. Guards cannot make asynchronous link loss
atomic. No write-path safety or command execution ACK is inferred.
