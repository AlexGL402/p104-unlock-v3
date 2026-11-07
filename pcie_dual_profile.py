#!/usr/bin/env python3
"""Read-only phase profiling / I/O comparison for the T5600 dual sampler.
No safety-check thinning, cached state, MMIO writer, or CHANGE_SPEED executor.
"""
import argparse
import ctypes
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import struct
import time
import fcntl

import pcie_dual_sampler as dual
from pcie_root_sampler import RootConfig, distribution
from design.p104_dual_plan import validate_gpu

PHASES = ('cycle_total', 'state_checks', 'final_guard', 'load_admission', 'mmio_load',
          'postcheck', 'guard', 'driver_binding', 'power_state',
          'config_identity', 'config_command', 'config_pmcsr')
CODES = {name: index for index, name in enumerate(PHASES, 1)}
SPAN = struct.Struct('<HQQIB')  # phase, absolute start/end ns, cycle, failed
MAX_SPANS = 1_000_000
TOP_LEVEL = ('state_checks', 'final_guard', 'load_admission', 'mmio_load', 'postcheck')


class Profiler:
    def __init__(self, enabled=False, max_spans=100_000, clock=time.monotonic_ns):
        if not 1 <= max_spans <= MAX_SPANS:
            raise ValueError('profile bound exceeded')
        self.enabled = enabled
        self.active = False  # Preflight is not part of capture profiling.
        self.clock = clock
        self.cycle = lambda: 0
        self.count = 0
        self.buffer = bytearray(max_spans * SPAN.size if enabled else 0)
        self.limit = max_spans

    def begin(self, name):
        if not self.enabled or not self.active:
            return None
        if self.count >= self.limit:
            raise dual.End('profile_limit')  # Refuse the next phase, no dropped span.
        index = self.count
        self.count += 1
        start = self.clock()
        SPAN.pack_into(self.buffer, index * SPAN.size, CODES[name], start, start, self.cycle(), 2)
        return index, start

    def finish(self, token, failed=False, start=None, end=None):
        if token is None:
            return
        index, original_start = token
        code, _, _, cycle, _ = SPAN.unpack_from(self.buffer, index * SPAN.size)
        SPAN.pack_into(self.buffer, index * SPAN.size, code,
                       original_start if start is None else start,
                       self.clock() if end is None else end, cycle, int(failed))

    def call(self, name, fn, *args):
        token = self.begin(name)
        try:
            value = fn(*args)
        except BaseException:
            self.finish(token, failed=True)
            raise
        self.finish(token)
        return value

    def rows(self):
        for index in range(self.count):
            yield SPAN.unpack_from(self.buffer, index * SPAN.size)


def timer_calibration(clock=time.monotonic_ns, count=256):
    values = []
    for _ in range(count):
        start = clock()
        values.append(clock() - start)
    return distribution(values)


class ProfileGPU(dual.ReadOnlyGPU):
    def __init__(self, root, profiler, io_style='reference', clock=time.monotonic_ns):
        if io_style not in ('reference', 'fresh-fast'):
            raise ValueError('invalid I/O style')
        super().__init__(root, clock)
        self.profiler = profiler
        self.io_style = io_style
        self.driver_path = os.fspath(dual.GPU_PATH / 'driver')
        self.power_path = os.fspath(dual.GPU_PATH / 'power_state')
        self.mmio_view = None
        self.cycle_token = None

    def _driver_probe(self):
        if self.io_style == 'reference':
            return super()._driver_bound()
        try:
            os.lstat(self.driver_path)  # Fresh stat EVERY guard; never cache absence.
            return True
        except FileNotFoundError:
            return False

    def _driver_bound(self):
        return self.profiler.call('driver_binding', self._driver_probe)

    def _guard(self, role=0):
        return self.profiler.call('guard', super()._guard, role)

    def _power_text(self):
        if self.io_style == 'reference':
            return (dual.GPU_PATH / 'power_state').read_text().strip()
        # Fresh fd every time: do not assume an open sysfs text fd regenerates
        # its seq-file content on every pread. No cached power-state values.
        fd = os.open(self.power_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            data = os.pread(fd, 64, 0)
            if len(data) == 64:
                raise dual.Stop('unexpected/truncated power_state text')
            return data.decode('ascii').strip()
        finally:
            os.close(fd)

    def _cfg(self, off, size):
        self._guard()
        name = {0: 'config_identity', 4: 'config_command'}.get(off, 'config_pmcsr')
        data = self.profiler.call(name, os.pread, self.gpu_fd, size, off)
        if len(data) != size or data == b'\xff' * size:
            raise dual.Stop(f'GPU config unavailable at {off:#x}')
        return int.from_bytes(data, 'little')

    def _state_impl(self):
        self._guard()
        if self.profiler.call('power_state', self._power_text) != 'D0':
            raise dual.Stop('GPU 0 power_state must already be D0')
        identity = self._cfg(0, 4)
        command = self._cfg(4, 2)
        pmcsr = self._cfg(self.pm + 4, 2)
        try:
            validate_gpu(identity, command, pmcsr, False)
        except ValueError as exc:
            raise dual.Stop(str(exc)) from exc
        return dict(identity=f'0x{identity:08X}', command=command, pmcsr=pmcsr, power_state='D0')

    def _state(self):
        return self.profiler.call('state_checks', self._state_impl)

    def preflight(self):
        info = super().preflight()  # Identical capability/topology/resource checks.
        if self.io_style == 'fresh-fast':
            # Construct a view only; .value is not accessed here.
            self.mmio_view = ctypes.c_uint32.from_address(self.address + dual.BAR_OFFSET - dual.PAGE_OFFSET)
        return dict(info, io_style=self.io_style, state_cached=False)

    def read_gpu(self):
        self.cycle_token = self.profiler.begin('cycle_total')
        try:
            self._state()
            self.profiler.call('final_guard', self._guard)
            self.profiler.call('load_admission', self.permit_load)
            mmio = self.profiler.begin('mmio_load')  # Reserve before the load.
            start = self.clock()
            self.loads += 1
            raw = (self.mmio_view.value if self.io_style == 'fresh-fast' else
                   ctypes.c_uint32.from_address(self.address + dual.BAR_OFFSET - dual.PAGE_OFFSET).value)
            end = self.clock()
            self.profiler.finish(mmio, start=start, end=end)
            return start, end, raw
        except BaseException:
            self.profiler.finish(self.cycle_token, failed=True)
            self.cycle_token = None
            raise

    def postcheck(self):
        try:
            self.profiler.call('postcheck', super().postcheck)
        except BaseException:
            self.profiler.finish(self.cycle_token, failed=True)
            self.cycle_token = None
            raise
        self.profiler.finish(self.cycle_token)
        self.cycle_token = None

    def close(self):
        self.mmio_view = None  # Do not keep a native view after unmap.
        super().close()


def profile_report(profiler):
    phases = {}
    for name, code in CODES.items():
        phases[name] = dict(inclusive=distribution(end - start for phase, start, end, _, failed in profiler.rows()
                                                 if phase == code and not failed),
                            inclusive_all_finished=distribution(end - start for phase, start, end, _, failed in profiler.rows()
                                                                if phase == code and failed < 2),
                            failed_or_incomplete=sum(phase == code and bool(failed)
                                                     for phase, _, _, _, failed in profiler.rows()),
                            incomplete=sum(phase == code and failed == 2
                                           for phase, _, _, _, failed in profiler.rows()))
    cycles = {}
    top_codes = {CODES[name] for name in TOP_LEVEL} | {CODES['cycle_total']}
    for code, start, end, cycle, failed in profiler.rows():
        group = cycles.setdefault(cycle, dict(spans={}, counts={}))
        if code in top_codes:
            group['spans'][code] = (start, end, failed)
        group['counts'][code] = group['counts'].get(code, 0) + 1
    residuals = []
    count_stats = {name: [] for name in PHASES}
    # Non-overlapping top-level partitions, computed per successful complete
    # cycle. Never subtract unrelated medians or add nested inclusive timings.
    for group in cycles.values():
        spans = group['spans']
        total = spans.get(CODES['cycle_total'])
        parts = [spans.get(CODES[name]) for name in TOP_LEVEL]
        if not total or total[2] or any(part is None or part[2] for part in parts):
            continue
        residuals.append((total[1] - total[0]) - sum(end - start for start, end, _ in parts))
        for name, code in CODES.items():
            count_stats[name].append(group['counts'].get(code, 0))
    return dict(enabled=profiler.enabled, recorded_spans=profiler.count,
                allocated_profile_bytes=len(profiler.buffer), span_bytes=SPAN.size,
                phases=phases, complete_cycle_count=len(residuals),
                nonoverlap_top_level=list(TOP_LEVEL),
                cycle_residual=distribution(residuals),
                calls_per_complete_cycle={name: dict(count=len(values),
                                                    min=min(values) if values else None,
                                                    max=max(values) if values else None)
                                         for name, values in count_stats.items()},
                observer_overhead_included=True,
                note='Inclusive guard/state timings nest; do not sum them. MMIO bracket includes Python/ctypes overhead.')


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=float, default=.05)
    parser.add_argument('--max-records', type=int, default=100_000)
    parser.add_argument('--max-profile-spans', type=int, default=100_000)
    parser.add_argument('--profile', action='store_true', help='record nested phase timings; adds observer overhead')
    parser.add_argument('--io-style', choices=('reference', 'fresh-fast'), default='reference')
    parser.add_argument('--output-dir', type=Path, default=Path('work/pcie-dual-profile'))
    a = parser.parse_args(argv)
    if not math.isfinite(a.duration) or not 0 < a.duration <= 10 or int(a.duration * 1e9) == 0:
        parser.error('duration must be finite, >0 and <=10 seconds')
    if not 1 <= a.max_records <= dual.MAX_RECORDS or not 1 <= a.max_profile_spans <= MAX_SPANS:
        parser.error('record/span bounds must be 1..1000000')
    return a


def main(argv=None):
    a = arguments(argv)
    a.output_dir.mkdir(parents=True, exist_ok=True)
    path = a.output_dir / f'gpu0-dual-profile-{time.time_ns()}.jsonl'
    print('JSONL:', path, flush=True)
    with path.open('x', encoding='utf-8') as log, dual.LOCK_PATH.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def durable(event, **row):
            log.write(json.dumps(dict(event=event, **row)) + '\n')
            log.flush()
            os.fsync(log.fileno())
        profiler = Profiler(a.profile, a.max_profile_spans)
        durable('manifest', started_utc=datetime.now(timezone.utc).isoformat(), kernel=os.uname().release,
                root=dual.ROOT, gpu=dual.GPU, io_style=a.io_style, profile=a.profile,
                hardware_writes=False, automatic_unbind=False, gpu1_access=False,
                unchanged_check_frequency=True, driver_state_cached=False,
                power_state_cached=False, mmio_load_only=True,
                raw_sample_bytes=a.max_records * dual.ROW.size,
                raw_profile_bytes=len(profiler.buffer), requested_duration_s=a.duration,
                max_profile_spans=a.max_profile_spans,
                timer_pair_calibration=timer_calibration(),
                calibration_is_not_subtracted=True)
        root = gpu = result = None
        try:
            root = RootConfig()
            gpu = ProfileGPU(root, profiler, a.io_style)
            durable('preflight', **gpu.preflight())
            result = dual.Collection(root.read_status, gpu, int(a.duration * 1e9), a.max_records)
            profiler.cycle = lambda: result.cycle
            profiler.active = True
            result.run()
        except (OSError, ValueError, dual.Stop, dual.Skip, KeyboardInterrupt) as exc:
            durable('abort', reason=f'{type(exc).__name__}: {exc}')
            print('STOP:', exc)
            return 1
        finally:
            profiler.active = False
            # Finish a cycle if decode/store ended collection before postcheck.
            if gpu is not None:
                profiler.finish(gpu.cycle_token, failed=True)
                gpu.cycle_token = None
                gpu.close()
            if root is not None:
                root.close()
        durable('capture_window', clock_origin_ns=result.origin, elapsed_ns=result.elapsed)
        for code, start, end, cycle, failed in profiler.rows():
            log.write(json.dumps(dict(event='profile_span', phase=PHASES[code - 1],
                                      cycle=cycle, start_ns=start - result.origin,
                                      end_ns=end - result.origin, failed=bool(failed),
                                      status=('complete', 'failed', 'incomplete')[failed])) + '\n')
        report = dual.summary(result)
        report['profile'] = profile_report(profiler)
        report['io_style'] = a.io_style
        dual.emit_capture(log, result, report)
    print(json.dumps(report))
    return 130 if result.reason == 'interrupted' else 1 if result.error else 0


if __name__ == '__main__':
    raise SystemExit(main())
