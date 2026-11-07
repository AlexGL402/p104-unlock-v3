# T5600 / P104-100 Gen2 x4 — Operator and ChatGPT recovery playbook

**Evidence date:** 2026-11-08. **Scope:** Dell Precision T5600; GPU0 `0000:03:00.0` NVIDIA P104-100 (10de:1b87); upstream Root `0000:00:02.0`; GPU1 `0000:04:00.0` must remain untouched.

**Status:** Physical **5 GT/s x4 (PCIe Gen2 x4)** was verified once in a running Linux session by independent `lspci` reads at **both** GPU0 and Root Port. Not yet verified: repeat from cold boot, NVIDIA driver rebind, CUDA/Ollama performance, persistence after reboot. **Do not present this as a safe one-command cold-boot unlock.**

Evidence: [2026-11-08 successful live test](evidence/t5600-p104-gen2-x4-confirmed-2026-11-08.md). The retrain script's own result was **FAIL** due to its strict post-transition GPU CAP/CAP2 equality check, **despite** confirmed Gen2 x4 in later `lspci`. Do not alter or erase that historical failure.

## A. For the operator: what to save NOW, before reboot or driver bind

All commands in this section are **read-only with respect to PCIe**, except for writing log/backup files on disk:

```bash
mkdir -p ~/ai6-backups/p104-gen2x4-2026-11-08
cp ~/p104-gen2-live-01b4cce/work/t5600-apply-live-group-bit20.jsonl \
  ~/ai6-backups/p104-gen2x4-2026-11-08/
cp ~/p104-gen2-live-01b4cce/work/t5600-root-retrain-post-group-second.jsonl \
  ~/ai6-backups/p104-gen2x4-2026-11-08/
{
  date -Is
  echo '=== GPU0 ==='
  sudo lspci -vv -s 03:00.0
  echo '=== ROOT ==='
  sudo lspci -vv -s 00:02.0
  echo '=== DRIVERS ==='
  lspci -nnk -s 03:00.0
  lspci -nnk -s 04:00.0
} > ~/ai6-backups/p104-gen2x4-2026-11-08/lspci-before-bind.txt
sha256sum ~/ai6-backups/p104-gen2x4-2026-11-08/* \
  > ~/ai6-backups/p104-gen2x4-2026-11-08/SHA256SUMS
sync
```

Check `LnkSta: Speed 5GT/s, Width x4` on **both** GPU0 and Root. `LnkCap` and `LnkCtl2` are not substitutes for `LnkSta`. GPU1 should remain attached to `nvidia`. Do **not** restart NVIDIA, rebind GPU0, reboot, or run another retrain as a way of “checking” this snapshot.

## B. Exact, observed successful *live-session* sequence (not a cold-boot recipe)

The successful sequence was achieved after several prior experiments and **is not yet reproduced end-to-end from a cold machine**:

1. GPU0 remained **unbound** from NVIDIA, in D0/runtime active, `COMMAND=0002`; GPU1 stayed on NVIDIA. Root Port was D0, Link healthy, AER zero; GPU0 negotiated **Gen1 x4**.
2. Earlier staged BAR0 preparation, capability/TLS writes and a **native single-DWORD** SPEED store placed `0x8C040=0x20045800`; GPU Link Control 2 `0002`, Root Link Control 2 `0042`. The old Root retrain performed **before VSEC=1** did not reach Gen2. Do not automatically repeat it.
3. `p104_apply_live_group.py` from PR #1 after bit20 fix `5a8c1ca` ran in **post-speed** mode. It skipped 7 unchanged RMW entries and made exactly one native DWORD store to `0x88610`, `0→1`. `GROUP PASS` and masked readback `1`. It retained `0x8C1C0=0x00120032`.
4. `pcie_root_retrain_once.py` from commit `f470362` ran **after** GROUP PASS: precisely one 16-bit Root Port config write `0xA0: 0x0040→0x0060`. During two seconds of Root-only observation: Training appeared (`LNKSTA=0x7842`) and cleared (`0x7042`), with physical **Gen2 x4** and zero Root AER.
5. Script stopped with `FAIL: GPU CAP/CAP2 must be 00453C42/6` after training. Subsequent read-only `lspci` proved **both Root and GPU0 at Gen2 x4**. The actual GPU CAP/CAP2 post-transition values were not captured in the abridged JSONL excerpt; **do not guess** them.

### Exact eight BAR0 DWORDs immediately before successful Root retrain

| BAR0 offset | Exact value |
|---|---|
| `0x8872C` | `0x00000006` |
| `0x2241C` | `0x00000081` |
| `0x8841C` | `0xC0B40500` |
| `0x8C2C0` | `0x068711B3` |
| `0x8C300` | `0x040A00C0` |
| `0x8C040` | `0x20045800` |
| `0x8C1C0` | `0x00120032` |
| `0x88610` | `0x00000001` |

**This is the *post-GROUP, pre-retrain* profile, not an invitation to write those values manually.**

## C. For the operator: next boot / lost Gen2 / future repeat

1. First **identify the new state without modifying hardware**:
   ```bash
   echo '=== GPU0 ==='
   sudo lspci -vv -s 03:00.0 | grep -E 'LnkCap:|LnkSta:|LnkCap2:|LnkCtl2:'
   echo '=== ROOT ==='
   sudo lspci -vv -s 00:02.0 | grep -E 'LnkCap:|LnkSta:|LnkCap2:|LnkCtl2:'
   echo '=== DRIVERS ==='
   lspci -nnk -s 03:00.0
   lspci -nnk -s 04:00.0
   ```
2. If both report **5GT/s x4**, do not run the unlock again. If they report **Gen1**, do **not** run the post-GROUP retrain or ApplyLiveGroup by assumption: after a reboot the BAR0 profile and bind state may differ. The `post-speed` and post-GROUP exact guards intentionally reject unknown/cold profiles.
3. Inspect the original evidence and [ApplyLiveGroup documentation](linux-apply-live-group.md), [Root retrain documentation](linux-gen2-root-retrain-once.md). Recover previous state and determine supported transitions **with read-only inspections first**. A cold-boot restart procedure remains **UNVALIDATED** and needs separate, explicit review before any hardware writes.
4. If an experiment stops or reports an error, save JSONL and **never rerun automatically**. Do not blindly toggle `0x8C040`, use Python `struct.pack_into` against MMIO, write endpoint Link Control bit5, trigger `CHANGE_SPEED`, attempt width-arm, reset, or force PCIe retrain. Earlier unsafe write caused Link Down and required full AC recovery. This could interrupt GPU1/AI6.
5. Keep logs out of version control unless intentionally reviewed/sanitized for credentials, host paths and privacy.

## D. Sources/checkout for a *future reviewed* experiment (not executable recovery)

The relevant code is in PR #1, branch `fix/linux-t5600-staged-gen2`:

- `p104_apply_live_group.py` (group; committed at `5a8c1ca`).
- `pcie_root_retrain_once.py` (post-GROUP Root-only one-shot; `f470362`).
- `design/p104_live_group_build.py` (native group loader/build; different from RO backend).
- `design/p104_bar0_ro_build.py` (read-only native backend used for retrain).
- `docs/linux-apply-live-group.md`, `docs/linux-gen2-root-retrain-once.md`.
- `docs/evidence/t5600-p104-gen2-x4-confirmed-2026-11-08.md`.

Safely fetch refs and prepare an **isolated** source checkout without touching the existing working tree or hardware:

```bash
cd ~/p104-unlock-v3
git fetch mine fix/linux-t5600-staged-gen2 --tags
mkdir -p ~/p104-gen2-recovery-review
git archive mine/fix/linux-t5600-staged-gen2 | tar -x -C ~/p104-gen2-recovery-review
```

**Do not `git reset`, `git checkout`, or `git pull` into the old working directory.** Avoid copying a native `.so` without its matching JSON build manifest: `p104_bar0_ro.so` requires `p104_bar0_ro.json` in the same build directory; the group library uses its own different manifest. Do not substitute one library for the other. Review path and hashes on the actual machine.

The successful live test used source in `~/p104-gen2-live-01b4cce`, with the post-group Root runner updated from `f470362`. The native RO library and its manifest were copied from `~/p104-gen2-live-8e5bfaf/work/bar0-ro-native/`.

## E. For ChatGPT / Codex in a future conversation

1. **Read this playbook and evidence first.** The user has already obtained **physical Gen2 x4 once on both ends on Linux**; do not restart research from the assumption that Linux cannot unlock this GPU.
2. Fixed topology: T5600 `GPU0=03:00.0`, Root `00:02.0`, GPU1 `04:00.0`. Always check that device identity/topology is unchanged before referring to those BDFs.
3. Distinguish state: capability Gen2 vs **physical negotiated** Gen2; and driver unbound vs NVIDIA/CUDA working. No CUDA proof yet.
4. Chronology matters: pre-GROUP VSEC=0 Root-only retrain **failed**; after GROUP PASS/VSEC=1 Root-only retrain **reached Gen2 x4**. Endpoint Retrain Link bit5 is reserved; don't recommend it.
5. Do not hide the post-GROUP Root runner's `FAIL` after it transitioned. Root observation showed Gen2, but final CAP/CAP2 equality check failed. Independent `lspci` on both ends is the physical success evidence. Determine the actual post-transition GPU CAP/CAP2 from fresh **read-only** data before changing guard logic; do not relax the guard without evidence.
6. Before proposing any future write, first demand/obtain the current read-only PCI state, inspect all known source and log baselines, and state risks to AI6/GPU1. No automatic retries, rollback, rebind, BIOS/kernel/EFI edits, driver installs, or host power cycle. Only one narrowly-scoped, explicitly authorized change if genuinely needed.
7. **Do not infer a reboot-persistent one-command procedure** from the successful live session. If asked for repeat, explain precisely which staged preparation was previously observed but not revalidated after a cold boot.
8. If user wants the result preserved, prioritize repository evidence, sanitized logs, Git tag, and read-only hardware snapshot. For NVIDIA/CUDA testing later, treat binding as an independent change with its own guards/backup/approval.

## F. Human-readable status to paste into a new chat

> Dell T5600, GPU0 P104-100 03:00.0, Root 00:02.0, GPU1 04:00.0. On 2026-11-08 we achieved physical PCIe Gen2 x4 on Linux, independently verified by lspci GPU0 and Root at 5GT/s x4. Sequence in this session: staged BAR0 + native SPEED and TLS=2, then ApplyLiveGroup GROUP PASS (VSEC 88610 0→1), then one 16-bit Root-only retrain A0 0040→0060. Retrain script reported FAIL due to GPU CAP/CAP2 post-check even though Root and GPU lspci verified Gen2. All steps and caveats in docs/T5600-P104-GEN2X4-RECOVERY.md. Reboot/rebind/CUDA not yet validated. Do not rerun hardware writes without reading current state and the evidence.

