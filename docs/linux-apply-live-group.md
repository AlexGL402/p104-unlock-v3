# Primary Linux runner: Windows ApplyLiveGroup

`p104_apply_live_group.py` ports **the function**
[ApplyLiveGroup/RmwVerify](../src/P104Gen2x4.cs#L636), one invocation, without its
outer caller. No hardware execution has been performed for this port.

| Order | BAR0 offset | Mask | Value |
| --- | --- | --- | --- |
| 1 | 8872C | 0000000F | 00000006 |
| 2 | 2241C | 00000081 | 00000081 |
| 3 | 8841C | 00006000 | 00000000 |
| 4 | 8C2C0 | 00000004 | 00000000 |
| 5 | 8C300 | 00000010 | 00000000 |
| 6 | 8C040 | 000C0000 | 00040000 |
| 7 | 8C1C0 | 000F0000 | 00020000 |
| 8 | 88610 | 00001001 | 00000001 |

Each entry reads `old`, computes `(old & ~mask) | (value & mask)`, skips an
unchanged word with **no store, pause or readback**, otherwise performs one
aligned native DWORD store, requests a **20-ms sleep**, and separately checks
`(readback & mask) == (value & mask)`. Readback outside the mask is logged but
does not by itself fail the comparison. Native permits enforce the table order
and disallow repeated/skipped-index calls; skipped unchanged entries advance the
permit without writing. No destination initialization, pack_into or memcpy.

Like C# `ok &= RmwVerify`, a valid masked mismatch accumulates failure while
remaining entries are still visited. There are no group retries. Linux safety
failures abort immediately, even if earlier writes succeeded; no rollback.
PASS means the group completed with successful masked checks, **not Gen2**.

## Exact differences from Windows and previous Linux tools

* **Windows environment:** ApplyLiveGroup runs with NVIDIA available. Linux
  keeps GPU0 unbound, D0/runtime active, COMMAND exactly 0002; Root D0/MSE=1,
  fixed identity/topology/BAR, idle x4 at Gen1 or Gen2, LT=0/DLLLA=1,
  no RL/Link Disable, zero AER/Device Status errors/pending. Internal
  ACTIVE/directive must be idle whenever 8C040 is sampled, with the known x4
  request flags bit29=1/bit31=0. These are safety exceptions to masked-only
  acceptance. GPU1 is never accessed.
* **Linux guards:** extra reads, native old-value recheck, mapping-review,
  project lock and process isolation. Sentinel/read error, concurrent drift,
  changed controls, AER or unsafe link => STOP. Windows returns false for bad
  old reads and its non-short-circuit caller may proceed; Linux does not.
  Extra old-value reads also occur on skips; this is not a post-store readback.
  Linux scheduler/guard overhead means actual store-to-readback time can exceed
  20 ms; requested sleep remains 20 ms, recorded with monotonic timestamps.
* **Old staged preparation:** cap-speed/TLS preceded SPEED, F0000 was reduced
  to 30000, CYA/VSEC were omitted, full DWORD readbacks and 2-s per-step waits
  were enforced. This runner restores Windows order/masks, 20-ms pauses and
  masked checks, without staged capability gates or those 2-s waits.
* **Scope:** ApplyLiveGroup itself writes no PCI config, TLS, retrain,
  CHANGE_SPEED, reset, physical-width arm or watchdog. Windows LiveLatch,
  WriteTlsBoth/RetrainBoth and outer fallback loops are **not** called.
  No old experimental pipeline is run automatically before/after the group.

## Initial state and current T5600 command

Fixed GPU0 `0000:03:00.0` / Root `0000:00:02.0`, PCI ID 10de:1b87, same reviewed
ordinary resource0 range/type. Driver absence, D0/runtime, COMMAND and Root
checks are fresh before accesses. Exclude concurrent bind/PM/config writers.
RW resource0 is mapped only after RO admission; same mapping attributes, no WC,
no initialization stores. All config files remain read-only.

Choose the exact initial BAR0 profile explicitly; no detection/fallback:

| `--initial-state` | Exact eight-DWORD source profile | Initial GPU CAP / CAP2 | Allowed full GPU/Root CTL2 words |
| --- | --- | --- | --- |
| factory | existing FACTORY dictionary | 00453C41 / 2 | 0000 / 0043 |
| cap-ready | FACTORY + prefix + 8C1C0=20032 | 00453C42 / 6 | 0000 / 0043 or 0002 / 0042 |
| post-speed | cap-ready + 8C040=20045800 | 00453C42 / 6 | 0002 / 0042 |
| applied | post-speed + 88610=1 | 00453C42 / 6 | 0000 / 0043 or 0002 / 0042 |

Other invariant words: `8C2C0=068711B3`, `8C300=040A00C0`; factory values:
`8872C=0,2241C=0,8841C=C0B46500,8C040=20085800,8C1C0=10032,88610=0`.
Prefix target values: `6,81,C0B40500`. Partial/unknown states are refused.
Control/CAP/TLS words are frozen where not changed by this MMIO recipe; GPU
CAP/CAP2 declarations may evolve within the group. No initial Gen2 physical
speed is assumed solely from capability. Root must advertise Gen3 x16.

For the reported **post-speed** state, the expected action is seven skips and
one write `88610:0->1`; SPEED is not rewritten. A cold boot invalidates that
profile and is refused. From factory the runner instead makes six writes,
including SPEED before cap-speed/TLS preparation. That exact Windows ordering
and the VSEC write are not hardware-validated on T5600; unmasked differences
from the Windows card/VBIOS remain preserved by the same RMW formula. Native access correctness
does not guarantee link survival. No automatic cold recovery; host AC recovery
would interrupt GPU1.

One command from the repository root, for a separately authorized actual run:

```bash
python3 -m design.p104_live_group_build --directory ./work/live-native && sudo python3 ./p104_apply_live_group.py --native-library ./work/live-native/p104_live_group.so --mapping-review ./work/t5600-mapping-review.json --initial-state post-speed --apply --confirm-gpu0-unbound --confirm-exclusive-access --confirm-live-group-risk --output ./work/t5600-apply-live-group-first.jsonl
```

The first part only builds/audits, with no device access. Reuse the successful
mapping-review if still applicable. New output filename required. Optional
`--dry-run` instead of `--apply` previews masks/skips without writes; it is not a
mandatory repeat of prior hardware diagnostics. Preserve the entire JSONL after
any failure, do not automatically rerun. No new functional test scenarios were
added per request: validation covers recipe comparison against actual C# constants
and RMW semantics, compiler build, existing assembly gate, and Python syntax.


## Narrow post-speed SYSTEM_MAX_SPEED variant

Only `--initial-state post-speed` additionally accepts the exact eight-DWORD
profile with `8C1C0=00120032` instead of `00020032`. The other seven words,
CAP/CAP2/TLS/link/driver/power/AER guards remain identical. The admitted full
variant is frozen across admission rechecks; no bit is globally ignored and
other initial-state profiles are unchanged.

In the related-family [envytools map](https://github.com/envytools/envytools/blob/f102b82381f3f11cee113d16374c87091db039d9/rnndb/bus/pci.xml#L525-L536),
[21:20] is SYSTEM_MAX_SPEED, separate from LNK_CAP_SPEED [17:16]. Field 1
(bit20=1, bit21=0) decodes to 5 GT/s, consistent with the
[Nouveau GK104 decoder](https://github.com/torvalds/linux/blob/v6.8/drivers/gpu/drm/nouveau/nvkm/subdev/pci/gk104.c#L111-L126).
This is an interpretation supported by related-family sources, not proof of
which actor changed the field on this GP104. It does not contradict the Gen2
target. Published Windows values already include `00110036 -> 00120036`
([historical fingerprint](https://github.com/skbskhud/p104-gen2-unlock/blob/0bbfb290ce4ee1bd6da1bfc9008f8173ff560d15/docs/04-硬件事实表.md#L209-L228),
[existing project comparison](linux-gen2-root-cause-investigation.md#41-значения-не-объединять-разные-windows-снимки)).
Older T5600 evidence with `00020032` remains historical; it is not rewritten.

The actual Windows mask is F0000, bits 16–19, so bit20 is outside it:
`(00120032 & ~000F0000) | (00020000 & 000F0000) = 00120032`.
The runner therefore **skips 8C1C0**, preserving bit20 with no normalization
write, pause or readback for that entry. Native code, masks, order and pauses
are unchanged; rebuilding the existing live-native library is unnecessary.
Use a new log filename, e.g. `t5600-apply-live-group-bit20.jsonl`.
