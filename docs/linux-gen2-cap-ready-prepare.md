# Controlled factory -> cap-ready preparation (GPU0 only)

Tool: `pcie_prepare_cap_ready.py`. Development/testing is offline only.
Reuses the read-only adapter's identity/topology/BAR/D0/COMMAND/Root guards,
lifecycle and isolated JSONL supervisor. Native backend reuses its volatile load
and the audited single aligned DWORD store/assembly gate. No pack_into, mapped
word initialization or arbitrary MMIO write API. Existing tools are unchanged.

The source sequence is stage1-override followed by stage1-cap-speed in
`p104_gen2_linux.py`. We do **not** call its stage2/CapRetrain runner: TLS there
leads to a retrain. Only its TLS formula/order is retained: GPU then Root,
`new = (old & ~0xF) | 2`, preserving every other Link Control 2 bit.

| Step | Fixed BAR0 write | Required GPU LNKCAP / LNKCAP2 afterwards |
| --- | --- | --- |
| 1 | `8872C: 0 -> 6` | `00453C41 / 2` |
| 2 | `2241C: 0 -> 81` | `00453C41 / 2` |
| 3 | `8841C: C0B46500 -> C0B40500` | `00453C41 / 6` |
| 4 | `8C1C0: 00010032 -> 00020032` | `00453C42 / 6` |

The known third-step intermediate declaration is intentional, **not cap-ready**.
Any other capability timing/value stops; no blind completion or repeated write.
After each MMIO step retain a two-second read-only stability window from the
staged implementation; an unexpected delayed capability stops before the next
write. Each fixed stage has a five-second budget; the parent deadline is 25 s. Then set GPU TLS=2 and Root TLS=2, with full 16-bit readback.
Already-2 TLS is verified/skipped. No writes to Link Control, COMMAND, SPEED
`8C040`, width, CYA/VSEC, AER/W1C, reset/bind or VBIOS. No automatic SPEED launch.

## Admission

Only `0000:03:00.0`, parent `0000:00:02.0`. Exact eight-DWORD **factory BAR0**
baseline required; partial preparation and already-cap-ready state are refused.
GPU0 unbound from any driver, D0/runtime active, MSE=1/BME=0; Root D0/MSE=1,
Training=0/DLLLA=1. Both ends stay Gen1 x4 throughout. AER and Device Status
errors/Transactions Pending must be zero; no status is cleared. Root capability
must match Gen3 x16; initial GPU CAP/CAP2 `00453C41/2`. Initial TLS may be GPU
1/2, Root 1/2/3, compliance controls off. One additional observed T5600 profile
is admitted: **full GPU CTL2=0000 / full Root CTL2=0043**, only after all the same
factory BAR0/CAP/link/COMMAND guards. No other TLS=0 profile is allowed.
Exact CTL/CTL2 snapshots are then checked for drift. Every operation has fresh guards, one separate readback and
post-step full register/CAP/CAP2/STA checks. Safe routine Root rows are counted
rather than streamed; unsafe Root rows and per-access brackets are retained.

Use the mapping-review JSON from the successful read-only test, provided kernel
and BAR range still match. Exclude other bind/PM/config writers; the project lock
only excludes cooperating tools. Ordinary resource0, no WC/fallback. RO mapping
is released before RW mapping; neither open/close initializes device words.

## Commands (repository root; only after permission for actual preparation)

Build as the ordinary user, no hardware access:

```bash
python3 -m design.p104_cap_ready_build --directory ./work/prep-native
```

One actual preparation invocation, **not run by the developer**:

```bash
sudo python3 ./pcie_prepare_cap_ready.py --native-library ./work/prep-native/p104_cap_ready.so --mapping-review ./work/t5600-mapping-review.json --apply --confirm-gpu0-unbound --confirm-exclusive-access --confirm-preparation-risk --output ./work/t5600-cap-ready-first.jsonl
```

Optional `--dry-run` instead of `--apply` admits factory state without writes;
it is not a required repeat of the completed hardware read-only test.

PASS: exit 0; last JSONL row `result=PASS`, `cap_ready=true`, `completed_mmio=4`,
`tls_verified=["gpu","root"]`. The `snapshot` row with
`role=final_cap_ready` contains all eight final DWORDs and both link snapshots:
GPU CAP `00453C42`, CAP2 `6`, both TLS=2, physical Gen1 x4. This is the prepared
baseline for `pcie_speed_once.py`, not physical Gen2 or CUDA validation. Check
these JSONL rows offline; a separate hardware read-only rerun is unnecessary:

```bash
python3 -c 'import json,sys; [print(json.dumps(r)) for line in open(sys.argv[1]) if (r:=json.loads(line)).get("event")=="result" or r.get("role")=="final_cap_ready"]' ./work/t5600-cap-ready-first.jsonl
```

FAIL: immediate STOP, **partial state is left in place**. No retry/rollback or
resume option; do not rerun or continue to SPEED. Keep the entire JSONL, including
uncertain write attempts. Native per-step permits are consumed before access;
TLS permits before pwrite, including short writes. Training/DLL loss prevents
further endpoint access; fatal signals/timeouts are recorded by the parent.
Read-only guards cannot eliminate asynchronous link-loss/binding races. Override,
capability and TLS writes still carry link-loss risk. Operator cold recovery,
if needed, interrupts GPU1 as well; no recovery is performed by this tool.

## Narrow TLS=0 correction

TLS=0 is not a speed encoding equivalent to TLS=1. PCIe Base 4.0 draft v0.3,
Table 7-28, p.654 ([specification mirror](https://raw.githubusercontent.com/osdev-jp/spec/refs/heads/main/PCI_Express_Base_4.0_Rev0.3_February19-2014.pdf)),
permits a 2.5-GT/s-only component to hardwire this field to zero; the same rule
and upstream-port distinction appear in the vendor
[AMD register reference, Target Link Speed field](https://docs.amd.com/r/en-US/am052-versal-aie-prime-pcm-gen2-register-reference/VF4_PF5_PCIE_CAP_VF_LINK_CONTROL2_LINK_STATUS2_REG-MMI_PCIE_CTRL_USP-Register?contentId=EPTA~LkElQGm2YDfY2R5lQ).
This supports admitting a **read** of zero in our Gen1-declaring baseline; it
does not prove GP104 hardwiring or authorize writing zero/unsupported speeds.
The current hardware report identifies the exact factory pair. Older T5600
[evidence lines 1,8–13](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-retrain-result/gpu0-stage2-cap-retrain-1793930796885371367.jsonl#L1-L13)
already records GPU 0000 / Root 0043, then successful 0000->0002 and
0043->0042 readbacks after capability preparation. Those older TLS observations
precede that experiment's retrain; this tool does not reproduce its retrain.
Final TLS=2 requirements, native store and preparation order are unchanged.
