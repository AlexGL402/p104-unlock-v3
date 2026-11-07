# T5600: observability review before any internal CHANGE_SPEED experiment

Research / offline analysis only. No new write executor, hardware sampler, kernel
module, trace attachment, tracefs setting, driver change or VBIOS operation is
implemented or executed here. GPU 1 is excluded.

## Root-only passive sampler now available

The independent [read-only sampler](linux-root-sampler.md) implements the next
Root Port timing test, fixed to 0000:00:02.0 LNKSTA offset A2. It reads no GPU or
BAR0 and works with both GPUs bound to NVIDIA. The user's 2000-read run on
6.8.0-142-generic observed 3041 throughout, read p50/p99/max 3.58/4.59/15.12 us
and reported max blind gap 82.53 us. That finite-window observation is not a
GP104 pulse-duration or command-acceptance guarantee. New reports separate the
gap definitions and actual capture coverage. CHANGE_SPEED execution remains blocked.

## Decision

**Do not implement the proposed 20085800→20085801 executor yet.** With the existing
50 ms polling loop a negative result cannot distinguish an ignored request from
an executed same-speed request whose brief activity was missed, or a request
accepted without entering an externally distinguishable state. Faster polling
alone cannot repair the missing command-completion semantics.

The next useful step is a separately reviewed passive observation inventory and
timing calibration, without any test write or retrain. A future command experiment
must name its observable hypothesis and admit an inconclusive outcome. We cannot
promise to make every negative hardware result uniquely interpretable.

## Existing sampling is not 20 Hz hardware telemetry

`CapRetrain.run()` sleeps 50 ms after a cycle. The cycle also performs config
reads, optional sysfs counter reads and per-port JSONL flush/fsync. Actual sample
gaps are at least the sleep plus work/scheduling overhead, not a fixed 50 ms.
The older stage loops sleep 100 ms. No measured worst-case gaps from the real
T5600 were supplied, so no actual sampling rate is claimed.

There is no documented minimum duration for GP104's proprietary ACTIVE pulse or
the particular root's Training indication for this command. A 100 us pulse at
5 ms and no pulse at all give identical samples at 0, 50 and 100 ms. Under a
simplified uniform-phase instantaneous-sampling model, capture probability for
one pulse of duration d shorter than spacing T is d/T, not certainty; at T=50 ms,
100 us gives 0.2%, 1 ms gives 2%. These are illustrations, not measured device
pulse lengths/probabilities. Extending the post-window does not recapture a pulse
which has already ended, and repeating a command is not passive observation.

Most importantly: the safety rule skips endpoint/BAR0 access while root Training
or inactive DLL is observed. That creates a deliberate blind interval in ACTIVE
samples, potentially covering the whole interesting episode. Do not bypass this
rule for a denser trace. Root Training, endpoint status and proprietary ACTIVE
need **separate** timestamp/coverage reports; root samples cannot fill BAR gaps.

## What can be observed without register writes

| Channel | What a positive observation supports | What silence cannot prove |
| --- | --- | --- |
| Root LNKSTA Training/DLLLA/current speed | Root-visible link activity or actual sampled rate | No intervening Recovery/training, request ignored |
| GPU 8C040 ACTIVE [4] / directive [3:0] | Internal status changed; command bit pending/cleared | Command executed, ACK/retirement, complete LTSSM path |
| Both-end LNKSTA speed/width | Actual sampled negotiated outcome | No brief higher-rate interval between samples |
| Existing LBMS/AER status and kernel AER counters | Management/error occurrence; error delta | Every training attempt generates a latch/error |
| A genuine controller-specific hardware history | Recorded transitions, if driver/hardware coverage is established | Nothing about this Intel root if the producer belongs to another controller |

Read only the already-understood status registers. No GP104 full-state LTSSM
history/FIFO/command completion counter has been identified in the examined
sources. `8872C=6` is the override value, not a state trace.
[envytools](https://github.com/envytools/envytools/blob/master/rnndb/bus/pci.xml)
defines ACTIVE and directive fields, not a guaranteed pulse length or an
execution-retirement acknowledgement. Self-clear alone is not such an ACK.
Root Training is a proxy, not a complete enumeration of LTSSM states.

AER and Link Status reads do not require W1C clearing. Existing root LBMS=1 is
already saturated for a new-event test; without clearing it (not proposed), it
cannot mark a new command. AER absence is not a positive command witness.
Known-register reads are electrically/software intrusive transactions even if
no writes are requested; they alter timing/load and still have a link-loss race.
Sysfs config accesses can involve runtime PM, so the existing unbound/D0 conditions
must be maintained; do not wake a D3 GPU under the label "passive".

## Kernel tracing: useful scope and hard limitations

Ubuntu 24.04 does not uniquely identify the running kernel (GA/HWE/custom). No
claim about trace availability on this T5600 is made without uname/config and the
actual tracefs event formats. Reading available event/function listings is the
first step; no driver install or kernel replacement is warranted.

### AER

Linux v6.8 [ras_event.h](https://github.com/torvalds/linux/blob/v6.8/include/ras/ras_event.h)
and [aer.c](https://github.com/torvalds/linux/blob/v6.8/drivers/pci/pcie/aer.c)
provide `ras:aer_event` when the kernel reports AER, with BDF, status and severity.
This may preserve an error report after the kernel clears the hardware status,
but is neither every physical error nor every LTSSM transition. A clean/quiet
trace is compatible with ignored, successful same-speed and benign-fallback cases.

### PCIe link / LTSSM events in newer kernels

The examined v6.8 trace-event directory has no generic pci.h link/LTSSM event.
Newer [pci.h](https://github.com/torvalds/linux/blob/master/include/trace/events/pci.h)
contains `pci:pcie_link_event` / hotplug events. The link event is emitted when
kernel software refreshes link information; it is not an interrupt on every
hardware LTSSM transition. A userspace RL or BAR store need not call that producer.

Newer [pci_controller.h](https://github.com/torvalds/linux/blob/master/include/trace/events/pci_controller.h)
contains `pcie_ltssm_state_transition`. The located producer is
[Rockchip DesignWare](https://github.com/torvalds/linux/blob/master/drivers/pci/controller/dwc/pcie-dw-rockchip.c),
which reads controller-specific history. This is not a producer for the Intel
root in T5600 or the NVIDIA endpoint. Event-name presence alone does not establish
coverage. Hardware debug FIFO reads may advance a read cursor; do not assume an
unknown debug block is side-effect-free just because it is read rather than written.

### ftrace/kprobes/BPF and MMIO traces

Function tracing may identify kernel calls such as `pcie_retrain_link` or the
actual sysfs config accessors present in `available_filter_functions`. This can
help identify competing kernel software activity, not autonomous hardware states.
Our sysfs RL write does not invoke `pcie_retrain_link()`; a kprobe on that helper
can remain silent even when userspace triggers RL successfully. On v6.8 the
[config access implementation](https://github.com/torvalds/linux/blob/v6.8/drivers/pci/access.c)
does not emit a generic pci_cfg_read/write tracepoint. Do not invent event names;
attach only after checking the exact kernel functions, formats and BDF filters.

`rwmmio:*` hooks cover instrumented **kernel** MMIO accessors, not plain userspace
CPU loads/stores through Python mmap/ctypes. They cannot see the GPU internally
changing a register without a CPU read either. The v6.8 x86
[io.h](https://github.com/torvalds/linux/blob/v6.8/arch/x86/include/asm/io.h)
uses inline accessors; mere existence of the trace-event definition is insufficient.

The kernel [mmiotrace documentation](https://github.com/torvalds/linux/blob/v6.8/Documentation/trace/mmiotrace.rst)
explicitly says userspace MMIO access tracing is not supported, describes
page-fault/single-step instrumentation and SMP limitations. It is not a passive
solution for the current Python path. Global mmiotrace/CPU-offline tracing could
disturb GPU 1 workloads and is excluded from this proposal.

Trace enable/filter/clock/buffer setup is a software configuration change, not a
PCI/MMIO register write, but is not performed automatically. Use an isolated
trace instance, narrowly scoped events/BDF filters and a supported monotonic
clock if a future setup is reviewed. Record actual configuration and lost/overrun
counts. No event producer means no valid negative inference. Kernel AER handlers
may perform recovery independently; tracing does not prevent or serialize them.

## Does SPEED=Gen1 make actual Gen2 impossible on GP104?

**Not established by the sources reviewed.** Correct the stronger interpretation
of the previous hypothesis: the register is an internal speed-request selector,
not a proven immutable upper-bound/fuse or always-enforced maximum-rate limit.

[Nouveau GK104](https://github.com/torvalds/linux/blob/v6.8/drivers/gpu/drm/nouveau/nvkm/subdev/pci/gk104.c)
selects code 1 for a requested 5 GT/s and code 2 for 2.5 GT/s, then triggers
CHANGE_SPEED. That supports the driver's intended request sequence. It does not
prove that an externally initiated transition can never produce Gen2 with code 2,
nor that a snapshot value is necessarily the last latched request. Capability
and TLS are separate. The Pascal gp100 table does not itself provide the GK104
speed callbacks; there is no source-level GP104 behavioral theorem here.

Original C# `WidthArm()` even comments that writing factory `20085800` back
"does not affect Gen2", without logging a sufficient isolated before/after speed
trace to verify it. Do not treat that comment as a counterexample or dismiss it
as proof of impossibility. The repository's single-bit sweep from SPEED=1 tests
codes 0 or 3 by one-bit flips, not code 2 (which requires flipping both bits), so
that sweep is not a controlled Gen1-selector-vs-Gen2 test either.

T5600 supplies one well-defined negative case: prepared capability/TLS plus one
root RL did not produce observed Gen2. It supports an internal-policy hypothesis
but does not isolate SPEED as the cause or rule out other gates/benign fallback.
The same-speed control cannot answer whether Gen2 is physically achievable while
SPEED remains 2. To answer that requires either exact-chip documentation or an
independently observed actual 5 GT/s transition with evidence that the selector
was unchanged throughout; no unsafe write or repeat retrain is proposed to obtain it.

## Criteria for an informative next experiment

### First, passive preparation without a command

1. Inventory actual kernel release/config, available events, event formats,
   matching Intel-root/NVIDIA producers and existing AER reporting. Unknown or
   inapplicable channels are recorded as unavailable, not "quiet".
2. Design a bounded two-window read-only sampler: fast root status and safe ACTIVE
   reads (only outside Training/inactive-DLL intervals), slower AER/counter reads.
   Do not scan unknown registers. Measure the real bracketed read intervals;
   publish p50/p99/max and boundary/skip gaps for each channel separately.
3. In the fast window use a bounded in-memory buffer, not fsync per sample;
   keep durable baseline/intent before a future command and flush samples on
   completion/error. This trades some crash-survival detail for observation
   quality and must be explicit. No synthetic pulse duration is device evidence.
4. Use a quiet baseline window of comparable length before any future stimulus.
   Record software interference and trace losses. Do not create a calibration
   event by retraining or resetting the card.

### Then, classify results without forcing a binary answer

| Outcome | Permitted conclusion |
| --- | --- |
| Verified command issue plus temporally correlated ACTIVE/Training | Some activity observed after the request; execution causality still depends on excluding competing activity |
| Documented matching per-command ACK / complete relevant state history | Stronger execution evidence; no such channel is identified here |
| Self-clear, unchanged Gen1, no captured pulse | Inconclusive accepted/ignored/missed/no-state-change cases remain |
| All samples idle with certified maximum gap G | No observed activity; can exclude only continuous observable pulses longer than G fully inside the covered window, and only for that channel's justified semantics |
| Errors/Link Down/stuck state | Informative adverse outcome: safety precondition failed; stop with no further command or recovery writes |
| Missing producer, trace loss, excessive read/skip gap, competing change | Measurement-invalid/inconclusive, not a device rejection |

A useful negative result can bound **observed activity or safety**, rather than
prove "ignored". Pre-register these bounds before a future write. If the purpose
requires a conclusive accepted-vs-ignored distinction, the hardware experiment
must stay blocked until an applicable ACK/history mechanism is found. Faster
polling and a longer timeout do not supply one.

## Offline deliverable

`design/p104_observability.py` assesses supplied bracketed samples for one channel,
including boundary and read-duration uncertainty, and assesses trace producer/
coverage/loss metadata. It never declares an ignored command proven. Tests show
an explicit pulse/no-pulse observational equivalence and the danger of borrowing
root samples to fill ACTIVE blind intervals. No hardware sampler or write
executor is added. The speed proposal now reports
`readiness=blocked_pending_observability_review`.
