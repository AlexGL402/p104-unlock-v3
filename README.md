<h2><strong>2026-11-01: Due to current technical limitations, physical x16 on the P104 is not achievable at this time; this project will be on an extended hiatus for further exploration.</strong></h2>

# P104-100: PCIe Gen2 x4 unlock + x16 width declaration (Windows, full open source)

[中文](README.zh-CN.md) | **English**

> Windows tool for the NVIDIA **P104-100** (GP104 mining card, `10DE:1B87`):
> factory **Gen1 x4 (≈0.78 GB/s)** → **Gen2 x4 (≈1.55/1.69 GB/s measured)**,
> plus the **x16 width declaration** (`LnkCap maxwidth = 16`), kept alive by a resident watchdog.
> Measured drop recovery: **Gen2 in 0.23–0.25 s, x16 declaration in 0.73–0.84 s**.

## Current version: v3.3 (tool v1.2)

> ⚠️ **The v3.1 "2.0x16" Manual/Auto packages (v1.0) are deprecated — do not use them.**
> They only worked via "full card reset, then wait for the driver to rebind" (very low success rate).
> Use `P104-2.0x16-v1.2.zip` from v3.3 instead — live writes, normally **no reset**. Old packages remain
> as history only.

Changes in v1.2 vs v1.1:

| Item | v1.1 (v3.2) | v1.2 (v3.3) |
|---|---|---|
| Watchdog | 150 ms | **20 ms** |
| Drop → Gen2 recovery | ~4 s | **0.23–0.25 s** |
| Drop → x16 declaration recovery | 12–16 s | **0.73–0.84 s** |
| Width retrain | none | **after every recovery/re-arm, until `LNKSTA width=16`** (2 consecutive misses → stop for this process) |
| Status panel | English RESULT | **Chinese verdict + evidence line** (data freshness/process/task; works non-admin) |
| `带宽测试.cmd` | printed "recovered" then dropped again on exit | **destroys CUDA context first, then waits, then prints** |
| Script name | `一键解锁.cmd` | **`自动解锁.cmd`** |

> ⚠️ **Physical x16 test result (2026-10-31): not yet achieved on this P104-100 as of 2026-10-31.**
> After soldering AC coupling capacitors, we verified: runtime writes, driver-disabled writes, full device
> resets, and an exhaustive 117-bit register sweep — the trained link width stays x4.
> Width registers such as `0x88140` (LINK_WIDTH+COMMIT) are protected by a hardware privilege gate (`0xBADF5040`).
> This tool delivers **Gen2 x4 + x16 width declaration**; physical x16 depends on board batch/routing.
> Details: [`docs/09-物理层实测记录-2026-10-31.md`](docs/09-物理层实测记录-2026-10-31.md).

## Measured results (2026-10-30/31, single card, CPU-attached x16 slot)

| Item | Result |
|---|---|
| Unlock path | **live write group, no full-card reset** (0 resets; ~5 s from factory state after restart) |
| PCIe link | **Gen2 x4** (`LNKSTA = 0x00001042`) |
| `LnkCap` declaration | maxspeed = Gen2, **maxwidth = x16** (`0x00454D02`) |
| CUDA bandwidth (pinned) | **H2D 1.55 / D2H 1.67 GB/s** (Gen1 x4 reference 0.78 / 0.85) |
| Drop recovery (v1.2) | Gen2 **0.23 / 0.23 / 0.25 s**; x16 declaration **0.73 / 0.82 / 0.84 s** |
| Gen1 dwell per drop | median **0 s**, mean 0.9 s |
| Resident hold | 20 ms watchdog, writes only on deviation |
| Autostart | scheduled task: boot +90 s / logon +120 s / retry every 5 min (cold-boot verified on v1.1) |
| Reset fallback | only after repeated live failures: max 1 per run, max 2 per boot, ≥10 min apart |

Register fingerprints (same card/slot, verify with `状态.cmd`):

```
               Gen1 factory     Gen2 unlocked
LNKSTA         0x00001041       0x00001042      <- current link speed
LNKCAP2        0x00000002       0x00000006      <- declared speeds
LNKCAP         0x00463C41       0x00454D02      <- maxspd=Gen2, maxwidth=16
LINK_CONFIG_0  0x20585800       0x20545800      <- 0x8C040 (SPEED=1)
```

> Note: GPU-Z / nvidia-smi "supported speed/width" are **driver-reported values** and are unreliable on this
> card (often shows 1.1 x4). Trust the raw `LNKSTA` / `LNKCAP` values from `状态.cmd`.

## Physical layer test (2026-10-31)

On this P104-100 (VBIOS `86.04.7a.00.19`, subsys `1237:10DE`) with soldered AC coupling capacitors
(0402 100nF X7R), the link still trains to x4 only. Verified paths:

| Path | Result |
|---|---|
| Runtime `0x8C040` arm + retrain | declaration 16 OK, physical x4 |
| Driver disabled + writes + retrain | x4 |
| Full resets (restart / disable-enable / remove-scan) | x4 (resets did take effect) |
| Exhaustive XVE bit sweep (117 bits, 12.24M samples) | **no bit changed width**; `0x8C080` always `0x0404` |
| Driver disabled + `0x88140` (LINK_WIDTH16\|COMMIT)/`0x88084`/`0x8C080`/`0x88460` | privilege gate `0xBADF5040`, writes dropped |
| VBIOS read-only analysis | DevInit never writes width fields; boot x4 is not set by VBIOS |

**Conclusion**: no software/firmware entry for width on this card; x4 comes from PHY/strap/board.
**Some batches may differ** (online success reports exist) — compare PCB routing and real `LNKSTA` width/bandwidth.
Raw data: [`docs/09-物理层实测记录-2026-10-31.md`](docs/09-物理层实测记录-2026-10-31.md) and [`docs/evidence/`](docs/evidence/).

**Where we are stuck (help welcome)**: (1) the boot-time strap declares maxwidth=4, so the first link training is always x4; (2) after we change the declaration at runtime, the link never re-runs Detect (Retrain only goes to Recovery, and a Link-Disable cycle drops the card). If you know where the strap lives / how to change it, or how to force a re-detect without losing the card, please open an Issue/Discussion. Details: [`docs/09`](docs/09-物理层实测记录-2026-10-31.md) section 7.
**Note**: this package already implements "declaration arm + width retrain". If a given P104-100 board natively supports 16 lanes (strap/routing/caps), this tool **may** unlock physical x16 directly via retrain; however this has **not yet been verified on our board** (still x4 after cap mod) — more samples are needed.

## Download & usage
Download `P104-2.0x16-v1.2.zip` from **Releases** (or the same-named folder in this repo), extract fully, then:

1. Right-click **`自动解锁.cmd`** → **Run as administrator**;
2. After `SUCCESS` you can **close the window** — the hold process lives in a SYSTEM scheduled task and
   re-unlocks after reboot;
3. Verify with `状态.cmd`:
   ```
   RESULT: 已解锁：Gen2 x4（5.0 GT/s）   [声明宽度 x16]
   ```
4. Bandwidth: `带宽测试.cmd` (≈1.55/1.69 GB/s).

| Script | Purpose |
|---|---|
| `自动解锁.cmd` | install + register task + unlock + wait (admin; **recommended**, window can be closed) |
| `状态.cmd` | read-only link/declaration/hold status with evidence line (no admin needed) |
| `带宽测试.cmd` | CUDA bandwidth test (briefly drops the link, waits for recovery) |
| `停止按住.cmd` | stop holding (link falls back to Gen1 — normal) |
| `卸载.cmd` | uninstall task/drivers (policy keys kept by default) |
| `手动解锁-仅实验用-窗口不能关.cmd` | experimental only: foreground hold, **window must stay open** |

**Can I close the window?** One-click **yes**; manual **no** (that window *is* the hold process).
Re-running is safe: if already unlocked it skips writes/resets and only restarts the hold process.

**Wait times:** autostart fires at boot **+90 s**, unlocks **3–10 s** later (wait 2–3 min after boot before
checking); if status says "recovering", wait 20 s; `带宽测试.cmd` briefly drops the link 10–20 s and waits
up to 30 s for recovery; task retries at boot +90 s / logon +120 s / every 5 min.

## Why drops still happen

The NVIDIA driver rewrites these volatile registers when it initializes/wakes the GPU or creates a CUDA
context → the link briefly falls back to Gen1 and the width declaration is wiped. The v1.2 20 ms watchdog
restores **Gen2 in 0.23 s** and the **x16 declaration in 0.8 s**. With no program touching the GPU the link is
stable (measured: 6 min 40 s with zero drops).

- GPU-Z / MSI Afterburner polling, browser hardware acceleration, game launchers and chat apps can each
  trigger one drop (self-heals in 2–5 s);
- During gaming the link stays Gen2 under load; close GPU-Z/Afterburner for a cleaner graph;
- Physical x16 needs hardware support (PHY/board); not achievable on this card. The tool retrains width after each recovery and reports the real result.
**About the GPU-Z "flapping" display:** GPU-Z / Afterburner refresh is **not synchronized** with our recovery — the tool re-applies within 0.2–0.5 s, but the monitor may sample exactly during the brief dip, so it looks like flapping. For the real picture, check the `LNKSTA` values from `状态.cmd` or the recovery lines in `status.txt`.

## How it works (v1.2)

1. Map GPU BAR0 through BYOVD drivers (WinRing0 / ThrottleStop);
2. Apply the **live declaration write group**:
   `0x8872C=6` → `0x2241C|=0x81` → `0x8841C&=~0x6000` → `0x8C2C0&=~4`
   → `0x8C300&=~0x10` → `0x8C040 SPEED=1` → `0x8C1C0[19:16]=2` → `0x88610|=1`;
3. Set `LNKCTL2=2` on both ends + retrain the root port → link trains to **Gen2 x4**;
4. Write the **x16 width declaration**: `0x8C040 = (cur & ~0xF8000000) | 0x80000000`
   (safe recipe; rollback always writes the whole word `0x20085800` — never clear bit31 with a mask);
5. **Width retrain**: after a successful arm, retrain once and read `LNKSTA` width; only `width=16` counts as
   success; 2 consecutive misses → assume no capacitors, stop retraining for this process;
6. **20 ms watchdog**: only writes on deviation; after a drop it retrains every 20 ms, restores Gen2 and re-arms;
7. If live writes keep failing (rare), one full-card reset is allowed (max 1/run, 2/boot, ≥10 min apart).

## Open source: tool + methodology + toolchain

| Path | Content |
|---|---|
| `P104-2.0x16-v1.2/` | Windows one-click package (pure ASCII cmd) |
| [`src/P104Gen2x4.cs`](src/P104Gen2x4.cs) | C# source (single file, with notes and field lessons) |
| [`tools/`](tools/README.md) | Python experiment toolchain: read-only probe, live write group, width recipe, HW lock |
| [`docs/07-破解方法论与AI-Agent指南.md`](docs/07-破解方法论与AI-Agent指南.md) | Full methodology (Chinese): four layers, standard workflow, safety rules, adapting to other cards |
| [`docs/08-v1.2-更新说明.md`](docs/08-v1.2-更新说明.md) | v1.1 → v1.2 changes and measurements |
| [`docs/05-功能边界总表.md`](docs/05-功能边界总表.md) | What can and cannot be unlocked |

> **Hand it to an AI agent.** If you own a different mining card (P106 / CMP series, etc.), give this repo's
> README, methodology and `tools/` to an AI agent (Claude Code / Codex / DSH, etc.) and let it follow the same
> workflow — **read-only probe → write-channel control → single-variable search for the declaration switch →
> speed/width → resident hold → bandwidth verification** — to analyze and adapt the approach to your own
> hardware. The tool is open source, and so is the methodology.

## Versions

| Version | Content | Note |
|---|---|---|
| **v3.3 (tool v1.2)** | 20 ms watchdog + width retrain + Chinese status + `-bw` order fix; includes physical x16 test result (not achievable on this card) | **current, recommended** |
| v3.2 (tool v1.1) | 150 ms watchdog, live write group, status panel | usable fallback |
| v3.1 (v1.0 packages) | reset-and-rebind path | **deprecated, do not use** (kept as history) |
| v1.6 (old repo) | first Gen2 x4 one-click tool | Gen2-only users: [old repo `p104-gen2-unlock`](https://github.com/skbskhud/p104-gen2-unlock) |

> The old repo publishes the same v3.3 release (dual-repo distribution); both repos use the same version number.

## Compatibility & safety

- Windows 10 / 11 x64 + **administrator**; P104-100 (`10DE:1B87`) only;
- Verified on CPU-attached PCIe x16 slot (PCH slot untested);
- BYOVD drivers (WinRing0 / ThrottleStop) may be flagged by AV/anti-cheat — expected;
- **Volatile registers only**: no VBIOS/EEPROM/fuse writes; power cycle reverts everything;
- If link training fails and the card drops off the bus: **full AC power-off for 30 s** restores it;
- Physical x16 needs **AC coupling capacitor modification** — not guaranteed by software; the tool logs whether
  physical x16 was achieved.

## Credits

- Users of the old v1.6 (Gen2 x4) release for feedback;
- Authors of the open drivers/toolchain (WinRing0, ThrottleStop);
- Everyone in the community who explored P104/P106 PCIe unlocking first.

## Disclaimer

For learning, research and your own hardware only. Modifying PCIe links can cause device loss/system
instability; use at your own risk. No VBIOS or fuse-level persistent changes are made.
