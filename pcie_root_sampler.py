#!/usr/bin/env python3
"""Read-only sampler for T5600 GPU 0's Root Port, 0000:00:02.0 + 0xA2.

Standalone: no imports from the unlock code and no hardware write API.
Run only when separately requested on the target. Tests inject synthetic reads.
"""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import struct
import time

ROOT = '0000:00:02.0'
CONFIG = Path('/sys/bus/pci/devices') / ROOT / 'config'
LNKSTA = 0xA2
SAMPLE = struct.Struct('<QQH')  # relative read-start/end ns and raw 16-bit status
MAX_SAMPLES = 1_000_000
FIELDS = ('training', 'dllla', 'speed', 'width', 'lbms')


def decode(raw):
    return dict(speed=raw & 15, width=(raw >> 4) & 63, training=bool(raw & 0x800),
                dllla=bool(raw & 0x2000), lbms=bool(raw & 0x4000),
                slot_clock=bool(raw & 0x1000), labs=bool(raw & 0x8000))


class RootConfig:
    """Only this fixed Root Port config file; O_RDONLY, never resource/BAR files."""
    def __init__(self):
        self.fd = os.open(CONFIG, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)

    def read(self, offset, size):
        data = os.pread(self.fd, size, offset)
        if len(data) != size or data == b'\xff' * size:
            raise OSError(f'config unavailable/short read at {offset:#x}')
        return int.from_bytes(data, 'little')

    def preflight(self):
        identity = self.read(0, 4)
        if identity & 0xFFFF != 0x8086:
            raise ValueError('expected Intel Root Port at fixed BDF')
        ptr, seen = self.read(0x34, 1) & 0xFC, set()
        while ptr:
            if ptr < 0x40 or ptr > 0xFC or ptr in seen:
                raise ValueError('malformed capability list')
            seen.add(ptr)
            if self.read(ptr, 1) == 0x10:
                if ptr + 0x12 != LNKSTA:
                    raise ValueError('PCIe Link Status does not match required offset 0xA2')
                flags = self.read(ptr + 2, 2)
                if (flags >> 4) & 15 != 4:
                    raise ValueError('device is not a PCIe Root Port')
                return dict(pci_identity=f'0x{identity:08X}', pcie_cap=f'0x{ptr:02X}',
                            link_status_offset=f'0x{LNKSTA:02X}')
            ptr = self.read(ptr + 1, 1) & 0xFC
        raise ValueError('PCIe capability missing')

    def read_status(self):
        return os.pread(self.fd, 2, LNKSTA)

    def close(self):
        os.close(self.fd)


@dataclass
class Capture:
    buffer: bytearray
    count: int
    origin_ns: int
    elapsed_ns: int
    requested_ns: int
    stop_reason: str
    error: str | None = None
    attempts: int = 0

    def samples(self):
        for index in range(self.count):
            yield SAMPLE.unpack_from(self.buffer, index * SAMPLE.size)


def capture(read_status, duration_ns, max_samples, clock=time.monotonic_ns):
    if not 0 < duration_ns <= 10_000_000_000 or not 1 <= max_samples <= MAX_SAMPLES:
        raise ValueError('capture bounds exceeded')
    buffer = bytearray(max_samples * SAMPLE.size)  # Allocate before timed window.
    origin = clock()
    count, attempts, reason, error = 0, 0, 'sample_limit', None
    try:
        while count < max_samples:
            start = clock() - origin
            if start >= duration_ns:
                reason = 'duration'
                break
            attempts += 1
            data = read_status()
            end = clock() - origin
            if end < start:
                raise ValueError('non-monotonic clock')
            if len(data) != 2 or data == b'\xff\xff':
                raise OSError('LNKSTA unavailable/short read')
            raw = int.from_bytes(data, 'little')
            SAMPLE.pack_into(buffer, count * SAMPLE.size, start, end, raw)
            count += 1
    except KeyboardInterrupt:
        reason, error = 'interrupted', 'KeyboardInterrupt'
    except (OSError, ValueError) as exc:
        reason, error = 'read_error', f'{type(exc).__name__}: {exc}'
    elapsed = clock() - origin
    return Capture(buffer, count, origin, elapsed, duration_ns, reason, error, attempts)


def distribution(values):
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return dict(count=0, p50_ns=None, p99_ns=None, max_ns=None)
    middle = n // 2
    median = ordered[middle] if n % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return dict(count=n, p50_ns=median,
                p99_ns=ordered[(99 * n + 99) // 100 - 1], max_ns=ordered[-1])


def intervals(result, kind):
    previous = None
    for start, end, _ in result.samples():
        if kind == 'read':
            yield end - start
        elif previous is not None:
            pstart, pend = previous
            if kind == 'no_read_gap':
                yield start - pend
            elif kind == 'cycle':
                yield start - pstart
            elif kind == 'conservative_gap':
                # Actual read instants can fall anywhere inside their brackets.
                yield end - pstart
        previous = (start, end)


def report(result, threshold_ns):
    timing = {kind: distribution(intervals(result, kind))
              for kind in ('read', 'no_read_gap', 'cycle', 'conservative_gap')}
    first = last = None
    transitions = {key: 0 for key in FIELDS}
    raw_changes = 0
    for start, end, raw in result.samples():
        decoded = decode(raw)
        if first is None:
            first = (start, end, raw)
        if last is not None:
            old = decode(last[2])
            raw_changes += raw != last[2]
            for key in FIELDS:
                transitions[key] += old[key] != decoded[key]
        last = (start, end, raw)
    if first is None:
        leading = trailing = result.elapsed_ns
        worst = result.elapsed_ns
    else:
        leading, trailing = first[0], result.elapsed_ns - last[1]
        worst = max(first[1], result.elapsed_ns - last[0],
                    timing['conservative_gap']['max_ns'] or 0)
    return dict(sample_count=result.count, stop_reason=result.stop_reason, error=result.error,
                requested_duration_ns=result.requested_ns, capture_elapsed_ns=result.elapsed_ns,
                read_completed_after_deadline=bool(last and last[1] > result.requested_ns),
                capture_overran_requested_window=result.elapsed_ns > result.requested_ns,
                uncovered_requested_tail_ns=max(0, result.requested_ns - result.elapsed_ns),
                raw_buffer_bytes=len(result.buffer), sample_record_bytes=SAMPLE.size,
                buffer_overwrites=0, discarded_samples=0,
                read_attempts=result.attempts, failed_or_invalid_reads=result.attempts - result.count,
                sample_limit_reached=result.stop_reason == 'sample_limit',
                timing=timing, leading_no_read_ns=leading, trailing_no_read_ns=trailing,
                conservative_max_gap_including_boundaries_ns=worst,
                gap_threshold_ns=threshold_ns,
                no_read_gaps_above_threshold=sum(v > threshold_ns for v in intervals(result, 'no_read_gap')),
                conservative_gaps_above_threshold=sum(v > threshold_ns for v in intervals(result, 'conservative_gap')),
                raw_status_changes=raw_changes, field_changes=transitions,
                first_status_hex=None if first is None else f'0x{first[2]:04X}',
                last_status_hex=None if last is None else f'0x{last[2]:04X}',
                observability=dict(channel='root LNKSTA only; not GPU ACTIVE or complete LTSSM',
                                   minimum_device_pulse_duration_ns=None,
                                   ignored_command_proven=False,
                                   interpretation='sampled_activity' if raw_changes else 'no_sampled_status_change',
                                   note='Silence does not exclude short pulses; threshold is not a device guarantee.'))


def write_results(log, result, summary):
    def emit(event, **row):
        log.write(json.dumps(dict(event=event, **row)) + '\n')
    previous = None
    previous_start = previous_end = None
    for index, (start, end, raw) in enumerate(result.samples()):
        state = decode(raw)
        emit('sample', index=index, read_start_ns=start, read_end_ns=end,
             read_duration_ns=end - start,
             cycle_start_interval_ns=None if previous_start is None else start - previous_start,
             no_read_gap_before_ns=None if previous_end is None else start - previous_end,
             conservative_gap_before_ns=None if previous_start is None else end - previous_start,
             raw=raw, raw_hex=f'0x{raw:04X}', **state)
        if previous is not None:
            changed = {k: dict(old=previous[k], new=state[k]) for k in FIELDS
                       if previous[k] != state[k]}
            if changed:
                emit('status_change', sample_index=index, read_start_ns=start,
                     read_end_ns=end, change_window_start_ns=previous_start,
                     change_window_end_ns=end, changed_fields=changed)
        previous = state
        previous_start, previous_end = start, end
    if result.error:
        emit('capture_error', reason=result.stop_reason, error=result.error)
    emit('summary', **summary)
    log.flush()
    os.fsync(log.fileno())


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=float, default=.25, help='capture seconds, >0 and <=10 (default .25)')
    parser.add_argument('--max-samples', type=int, default=100_000, help='stop without overwrite when buffer full; <=1000000')
    parser.add_argument('--gap-threshold-us', type=float, default=100, help='reporting threshold only, not a guaranteed pulse length')
    parser.add_argument('--output-dir', type=Path, default=Path('work/pcie-root-sampler'))
    a = parser.parse_args(argv)
    if not math.isfinite(a.duration) or not 0 < a.duration <= 10 or int(a.duration * 1e9) == 0:
        parser.error('--duration must be finite, >0 and <=10 seconds')
    if not 1 <= a.max_samples <= MAX_SAMPLES:
        parser.error('--max-samples must be 1..1000000')
    if not math.isfinite(a.gap_threshold_us) or a.gap_threshold_us <= 0:
        parser.error('--gap-threshold-us must be finite and positive')
    return a


def main(argv=None):
    a = arguments(argv)
    a.output_dir.mkdir(parents=True, exist_ok=True)
    path = a.output_dir / f'root0-lnksta-{time.time_ns()}.jsonl'
    print('JSONL:', path, flush=True)
    with path.open('x', encoding='utf-8') as log:
        manifest = dict(event='manifest', root=ROOT, config_path=str(CONFIG), offset='0xA2',
                        started_utc=datetime.now(timezone.utc).isoformat(), kernel=os.uname().release,
                        clock='monotonic_ns', clock_resolution_s=time.get_clock_info('monotonic').resolution,
                        duration_s=a.duration, max_samples=a.max_samples,
                        allocated_sample_buffer_bytes=a.max_samples * SAMPLE.size,
                        hardware_writes=False, bar0_access=False, gpu_config_access=False,
                        gpu1_access=False, artificial_poll_sleep=False,
                        quantiles='p50=median; p99=nearest rank',
                        note='All sample times relative to capture clock_origin_ns. No fsync/printing in capture.')
        log.write(json.dumps(manifest) + '\n')
        log.flush()
        os.fsync(log.fileno())
        device = None
        try:
            device = RootConfig()
            preflight = device.preflight()
            log.write(json.dumps(dict(event='preflight', **preflight)) + '\n')
            log.flush()
            os.fsync(log.fileno())
            result = capture(device.read_status, int(a.duration * 1e9), a.max_samples)
        except (OSError, ValueError, KeyboardInterrupt) as exc:
            log.write(json.dumps(dict(event='abort', reason=f'{type(exc).__name__}: {exc}', hardware_writes=False)) + '\n')
            log.flush()
            os.fsync(log.fileno())
            print('STOP:', exc)
            return 1
        finally:
            if device is not None:
                device.close()  # No device reads while sorting or writing JSONL.
        log.write(json.dumps(dict(event='capture_window', clock_origin_ns=result.origin_ns,
                                  elapsed_ns=result.elapsed_ns)) + '\n')
        summary = report(result, round(a.gap_threshold_us * 1000))
        write_results(log, result, summary)
    print(json.dumps(dict(event='summary', **summary), ensure_ascii=False))
    return 130 if result.stop_reason == 'interrupted' else 1 if result.error else 0


if __name__ == '__main__':
    raise SystemExit(main())
