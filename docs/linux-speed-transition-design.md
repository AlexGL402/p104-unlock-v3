# T5600: speed-transition mechanisms after a negative root retrain

Research and offline code proposal only. No new executable hardware mode or write
backend is added by this change. GPU 1, VBIOS and driver installation are excluded.
Preparing this document does not authorize any future hardware operation.

## New user-provided evidence

Both TLS writes succeeded and one root retrain was issued. Afterward GPU maximum
capability is Gen2 x4 / speed vector 6, root maximum is Gen3 x16 / vector 14,
both TLS targets are 2, but both negotiated links are Gen1 x4. AER samples are
zero; the user's filtered kernel journal has no relevant events. This rejects
sufficiency of the standard-only root retrain path for the tested configuration.
It does not prove that 5 GT/s was never attempted: recovery/fallback can occur
without a captured AER error, and sampled config does not show every LTSSM state.

The following explanation assumes the latest BAR0 remains `8C040=20085800`, as
required by the preceding experiment. An old pre-trigger baseline is not proof
of its current value. A future experiment must revalidate it; no hardware reread
was performed by the development agent. Existing JSONL should be reviewed first.

## Why root 7041 differs from endpoint 1041

| Link Status field | GPU 0x1041 | Root 0x7041 |
| --- | --- | --- |
| Current speed [3:0] | 1 = 2.5 GT/s | 1 = 2.5 GT/s |
| Negotiated width [9:4] | 4 | 4 |
| Training [11] | 0 | 0 |
| Slot Clock Configuration [12] | 1 | 1 |
| DLL Link Active [13] | 0 | 1 |
| Link Bandwidth Management Status [14] | 0 | 1 |
| Autonomous Bandwidth Status [15] | 0 | 0 |

The XOR is 6000. Actual speed/width agree: both words masked by 03FF equal 0041.
GPU LNKCAP 00453C42 has DLL active reporting capability bit 20 clear, so its
DLLLA=0 cannot be interpreted as Link Down. Root supports and reports active DLL.
LBMS is a sticky management status, not an AER bit or proof of Gen2. Without a
pre-event baseline it cannot be assigned to this exact retrain. Do not clear it
just to make both status words match. `lspci`'s "downgraded" compares negotiated
and advertised limits; it is not itself an error record.

Source: [Linux PCI register definitions](https://github.com/torvalds/linux/blob/v6.8/include/uapi/linux/pci_regs.h).

## Separate mechanisms

| Mechanism | What it changes/requests | T5600 evidence / limitation |
| --- | --- | --- |
| XVE override + declaration switches | Enables supported-speed advertisement | Prefix produced LNKCAP2=6, not LNKCAP Gen2 by itself |
| 8C1C0[17:16], LNK_CAP_SPEED | Maximum capability field | 1→2 made GPU LNKCAP advertise Gen2 |
| LNKCTL2.TLS | Standard target for speed negotiation | TLS=2 at both ends did not force actual Gen2 |
| Root LNKCTL.RL | Downstream-port retraining request | One request completed/returned to Gen1; no observed AER |
| 8C040.SPEED[19:18] | Proprietary requested speed: Pascal 2=Gen1, 1=Gen2 | Still Gen1 in the known baseline; strongest remaining rate-policy candidate |
| 8C040.LTSSM_DIRECTIVE[3:0]=1 | Internal CHANGE_SPEED request | Different entry point from root RL; not exercised in the standard-only path |
| Physical Recovery/speed negotiation | Exchanges training sequences and changes physical rate | Capabilities and targets alone do not prove it succeeds |

[Nouveau gk104.c](https://github.com/torvalds/linux/blob/v6.8/drivers/gpu/drm/nouveau/nvkm/subdev/pci/gk104.c)
separates cap-speed, LNKCTL speed and actual link speed. `gk104_pcie_set_link()`
clamps the request against cap-speed and LNKCTL speed before calling the internal
speed setter. That setter programs SPEED with mask C0000, then separately sets
bit 0. It does **not** rely solely on root RL. This is source evidence for distinct
controls, not a proven RTL equation or guarantee on P104. The Pascal
[gp100.c](https://github.com/torvalds/linux/blob/v6.8/drivers/gpu/drm/nouveau/nvkm/subdev/pci/gp100.c)
PCI function table does not itself provide the GK104 speed-setting callbacks;
"Nouveau does this" must not be presented as a validated GP104 unlock path.

[envytools pci.xml](https://github.com/envytools/envytools/blob/master/rnndb/bus/pci.xml)
defines directive 0=normal, 1=CHANGE_SPEED; bit 4=ACTIVE; Pascal SPEED encoding
above. Its register map is reverse engineered and has explicit uncertainty notes.

Original P104 C# `ApplyLiveGroup` changes SPEED before LNK_CAP_SPEED, then VSEC,
and only checks capability afterward. `WriteSpeedGen2` deliberately omits bit 0.
Therefore Windows success does not isolate whether direct SPEED programming,
internal directive, VSEC or driver participation is necessary. Original `RmwVerify`
waits 20 ms; it does not describe the intermediate LTSSM transition.

Repo evidence `docs/evidence/P104-2.0x16-XVE位穷举-2026-10-31.md` reports self-clearing
bit 0/1 and an internal CHANGE_SPEED sequence found in VBIOS **86.04.7A.00.19**.
That is not the user's .47 VBIOS, so it is supporting evidence only, not an exact
firmware match; no firmware modification or fresh VBIOS dump is proposed.

## Remaining policy candidates, ranked

1. **Internal SPEED still requests Gen1.** Consistent capability and TLS removed
   two earlier limits; they did not change this third control. This is the strongest
   working hypothesis, but not experimentally proven necessary/sufficient on T5600.
2. **Internal CHANGE_SPEED entry point is required.** Nouveau explicitly triggers
   it after selecting speed. Root RL and this directive cannot be assumed equivalent.
3. **Another gate, driver ownership or physical training constraint.** Still open.
   Zero AER and kernel-log silence do not eliminate benign fallback or an internal
   clamp. Raising capability does not alter board signal integrity.

Useful exclusions/limits from the existing baseline:

- 8C040 bit 11 is already 1. A different-card bit sweep reports loss of Gen2 on
  clearing it, but there is no missing bit 11 here to "repair".
- CYA_0 bit 2 is already clear; CYA_1 bit 4 is already clear. The
  [published Pascal GP102 header](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/src/common/inc/swref/published/pascal/gp102/dev_nv_xp.h)
  identifies CYA_1 bit 4 as BLOCK_HOST2XP_HOLD_LTSSM. Do not rewrite these no-ops.
- 8C1C0 SYSTEM_MAX_SPEED[21:20]=0 is not the Gen1 value in the related-family map;
  it does not establish genuine Gen3 support either. Do not modify it or the
  separate PCIE_V2_SUPPORTED bit 2 based only on an inconsistent historical label.
- 88610=0 did not prevent **capability** becoming Gen2. Its necessity for physical
  speed remains unresolved; no verified independent gate semantics on this P104
  justify mixing a VSEC write into the next test.
- Record full LNKCTL2, including HASD bit 5. It governs hardware-autonomous speed
  behavior; it is not proof that a software RL command was blocked. No blind HASD
  clearing is proposed. The offline planner reports it without modifying it.

## Why neither obvious Gen2 write is approved

| Candidate | Difference from prior failure | Why not the next authorized operation |
| --- | --- | --- |
| Two writes: SPEED then bit 0, like GK104 | Capability/TLS now prepared first | First write still reproduces 20045800, which already caused Link Down |
| Combined 20045801 in one aligned dword | Programs Gen2 and directive together; avoids a separately issued 20045800 transaction | Not the Nouveau two-write order; internal sampling order unknown. Self-clear may immediately leave 20045800. No evidence it is safer |
| Change width arm/bit29 or add VSEC | Different control state | Adds untested variables and previously documented dangerous states |

Capability/TLS-first is a technically distinct precondition from the first crash,
but it is not enough to declare another direct SPEED write safe. A combined write
is a research candidate, **not** a workaround certified by source code. It needs
its own future review and permission; the offline proposal does not produce it.

## Minimal next experiment proposal: internal same-speed control

First test the unexercised internal request path **without changing SPEED**:

```text
one prospective aligned dword transaction, GPU 0 only:
8C040: 20085800 → 20085801
changed bits: bit 0 only
SPEED stays 2 (Gen1); width bits, bit 11 and all policy bits preserved
```

This is a control experiment, not a Gen2 unlock attempt. It may initiate internal
training even though requested speed stays Gen1; it is not a harmless no-op and
still carries Link Down risk. It is distinct from repeating root RL. No root RL,
TLS, VSEC, width, reset, bind, fallback, second command or automatic restore.

Required future preflight: exact post-cap-speed BAR0 baseline, both TLS=2,
consistent GPU/root capability, driver absent, D0/MSE/BME as before, both links
idle Gen1 x4, command nibble zero, internal ACTIVE clear, no AER/counter delta.
Full root/GPU control words and status baselines are retained, not rewritten.
If power removal lost the prefix, refuse and do not auto-prepare it.

Future observation design: durable intent before the single attempt; root status
and AER first, no endpoint/BAR access while root Training or inactive DLL; after
idle, sample directive/ACTIVE and both-end status. Ignore only bit 0 self-clear
and read-only ACTIVE for register comparison; every other bit must remain the
expected Gen1 baseline. Bound observation to two seconds, stop on any error or
unexpected negotiated width/rate or non-idle timeout, no recovery writes. Polling
can miss short ACTIVE/Training intervals; do not require catching a pulse, or
interpret lack of a pulse as rejection. Do not clear LBMS/AER to create a marker.

Interpretation:

- Observed transient ACTIVE/Training followed by idle Gen1 x4: evidence that an
  internal same-speed request reaches some control logic; not proof of Gen2 safety.
- Bit 0 self-clears with no observed transition: **inconclusive**. Self-clear alone
  does not distinguish executed command, ignored request or missed short event.
- Link Down, AER, width/speed change, other register change or stuck activity:
  stop. The control itself is not validated; do not advance to a Gen2 request.

Only after reviewing that independent result could a higher-risk Gen2 transaction
be discussed. Do not implement it as an automatic second stage.

## Offline code delivered

`design/p104_speed_plan.py` is a pure decoder/proposal builder over supplied data.
It has no sysfs/mmap/config/CUDA imports, no device handle and no executor.
Its result explicitly has `design_only=true`, `execution_allowed=false`, live
revalidation and separate permission requirements. It proposes only the same-speed
control and reports HASD. It rejects 20045800/20045801 as control readbacks. No new
`--apply` mode is added to `p104_gen2_linux.py` and its existing backends still deny
these 8C040 writes.

`tests/test_speed_design.py` uses synthetic dictionaries, tests 7041/1041 decoding,
command-only proposal, self-clear uncertainty, dangerous/mismatched readbacks,
strict baseline, HASD preservation and GPU/error/TLS gates. Passing tests establish
only software logic, not device behavior. The entire suite has 84 tests.
