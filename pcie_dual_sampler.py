#!/usr/bin/env python3
"""Read-only T5600 Root LNKSTA + GPU 0 8C040 sampler. No write executor.
GPU 0 must ALREADY be unbound and D0/MSE=1/BME=0. Never unbinds it.
"""
import argparse
import ctypes
from datetime import datetime, timezone
import fcntl
import json
import math
import mmap
import os
from pathlib import Path
import platform
import struct
import time

from design.p104_dual_plan import GPU, ROOT, root_action, validate_gpu, decode_gpu, interpretation
from pcie_root_sampler import RootConfig, decode as decode_root, distribution

GPU_PATH = Path('/sys/bus/pci/devices') / GPU
LOCK_PATH = Path('/run/lock/p104-gen2-linux.lock')
BAR_OFFSET = 0x8C040
PAGE_OFFSET = 0x8C000
PAGE_SIZE = 4096
ROW = struct.Struct('<BQQIIH')  # kind, read start/end ns, raw/reason, cycle, role
ROOT_ROW, GPU_ROW, SKIP_ROW = 1, 2, 3
MAX_RECORDS = 1_000_000
SKIPS = {'skip_training': 1, 'stop_dll_inactive': 2}


class Stop(RuntimeError):
    pass


class Skip(RuntimeError):
    pass


class End(RuntimeError):
    pass


class ReadOnlyGPU:
    """Known page only, PROT_READ mapping and aligned native dword LOAD.

    Every endpoint config/metadata/load entry is root-gated. The final check
    cannot eliminate asynchronous physical loss or an external driver-bind race.
    """
    def __init__(self, root, clock=time.monotonic_ns):
        self.root = root
        self.clock = clock
        self.guard_reader = self._plain_root
        self.permit_load = lambda: None
        self.gpu_fd = self.bar_fd = self.address = self.pm = None
        self.loads = 0
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.mmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_long)
        self.libc.mmap.restype = ctypes.c_void_p
        self.libc.munmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t)

    def _plain_root(self, role=0):
        data = self.root.read_status()
        if len(data) != 2:
            raise Stop('short Root LNKSTA read')
        return int.from_bytes(data, 'little')

    def _driver_bound(self):
        try:
            (GPU_PATH / 'driver').lstat()
            return True  # Any driver entry, not just nvidia, refuses BAR access.
        except FileNotFoundError:
            return False

    def _guard(self, role=0):
        raw = self.guard_reader(role)
        action = root_action(raw)
        if action == 'skip_training':
            raise Skip(action)
        if action != 'allow':
            raise Stop(action)
        if self._driver_bound():
            raise Stop('GPU 0 driver is bound; no endpoint/BAR access')

    def _cfg(self, off, size):
        self._guard()
        data = os.pread(self.gpu_fd, size, off)
        if len(data) != size or data == b'\xff' * size:
            raise Stop(f'GPU config unavailable at {off:#x}')
        return int.from_bytes(data, 'little')

    def _state(self):
        self._guard()
        if (GPU_PATH / 'power_state').read_text().strip() != 'D0':
            raise Stop('GPU 0 power_state must already be D0')
        identity, command, pmcsr = self._cfg(0, 4), self._cfg(4, 2), self._cfg(self.pm + 4, 2)
        try:
            validate_gpu(identity, command, pmcsr, False)
        except ValueError as exc:
            raise Stop(str(exc)) from exc
        return dict(identity=f'0x{identity:08X}', command=command, pmcsr=pmcsr, power_state='D0')

    def preflight(self):
        if platform.machine() != 'x86_64':
            raise Stop('native MMIO load supported only on x86_64')
        root_info = self.root.preflight()
        if not self.root.read(0x9C, 4) & 0x100000:
            raise Stop('root DLL active reporting required')
        self._guard()
        if GPU_PATH.resolve(strict=True).parent.name != ROOT:
            raise Stop('unexpected GPU 0 parent; fixed topology required')
        self._guard()
        if (GPU_PATH / 'power_state').read_text().strip() != 'D0':
            raise Stop('GPU 0 must already be D0')
        self._guard()
        self.gpu_fd = os.open(GPU_PATH / 'config', os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        if self._cfg(0, 4) != 0x1B8710DE:
            raise Stop('unexpected GPU 0 PCI ID')
        ptr, seen = self._cfg(0x34, 1) & 0xFC, set()
        while ptr:
            if ptr < 0x40 or ptr > 0xFC or ptr in seen:
                raise Stop('malformed GPU capability list')
            seen.add(ptr)
            if self._cfg(ptr, 1) == 1:
                self.pm = ptr
                break
            ptr = self._cfg(ptr + 1, 1) & 0xFC
        if self.pm is None:
            raise Stop('GPU power-management capability missing')
        state = self._state()
        self._guard()
        with (GPU_PATH / 'resource').open('r') as resources:
            parts = resources.readline().split()
        if len(parts) != 3:
            raise Stop('BAR0 resource descriptor unavailable')
        start, end, flags = (int(part, 16) for part in parts)
        if not start or end < start or end - start + 1 < PAGE_OFFSET + PAGE_SIZE or not flags & 0x200:
            raise Stop('BAR0 must be assigned memory covering the known page')
        self._guard()
        self.bar_fd = os.open(GPU_PATH / 'resource0', os.O_RDONLY | os.O_SYNC | os.O_CLOEXEC | os.O_NOFOLLOW)
        self._guard()
        address = self.libc.mmap(None, PAGE_SIZE, mmap.PROT_READ, mmap.MAP_SHARED, self.bar_fd, PAGE_OFFSET)
        if address in (None, 0, ctypes.c_void_p(-1).value):
            raise OSError(ctypes.get_errno(), 'read-only BAR0 page map failed')
        self.address = address
        self._guard()  # No dereference until all conditions have been rechecked.
        return dict(root=root_info, gpu=GPU, gpu_state=state, known_register='0x8C040',
                    mapped_offset='0x8C000', mapped_bytes=PAGE_SIZE, mapping='PROT_READ')

    def read_gpu(self):
        self._state()
        self._guard()
        self.permit_load()  # Bound check before native load, not after allocation.
        start = self.clock()
        self.loads += 1
        raw = ctypes.c_uint32.from_address(self.address + BAR_OFFSET - PAGE_OFFSET).value
        end = self.clock()
        return start, end, raw

    def postcheck(self):
        self._guard(role=1)

    def close(self):
        if self.address is not None:
            self.libc.munmap(self.address, PAGE_SIZE)
            self.address = None
        for name in ('bar_fd', 'gpu_fd'):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)


class Collection:
    def __init__(self, root_reader, gpu, duration_ns, max_records, clock=time.monotonic_ns):
        if not 0 < duration_ns <= 10_000_000_000 or not 1 <= max_records <= MAX_RECORDS:
            raise ValueError('capture bounds exceeded')
        self.root_reader, self.gpu, self.clock = root_reader, gpu, clock
        self.duration_ns = duration_ns
        self.buffer = bytearray(max_records * ROW.size)
        self.limit = max_records
        self.count = self.cycle = self.origin = self.elapsed = 0
        self.reason = self.error = None
        self.gpu_started = False
        self.root_attempts = 0
        self.skip_counts = {(reason, role): 0 for reason in SKIPS for role in (0, 1)}
        self.skip_markers_not_stored = 0

    def rows(self):
        for index in range(self.count):
            yield ROW.unpack_from(self.buffer, index * ROW.size)

    def _room(self):
        if self.count >= self.limit:
            raise End('record_limit')
        if self.clock() - self.origin >= self.duration_ns:
            raise End('duration')

    def _store(self, kind, start, end, raw, role=0):
        if self.count >= self.limit:
            raise End('record_limit')
        if end < start:
            raise Stop('non-monotonic read bracket')
        ROW.pack_into(self.buffer, self.count * ROW.size, kind, start, end, raw, self.cycle, role)
        self.count += 1

    def _root(self, role=0):
        self._room()
        start = self.clock() - self.origin
        self.root_attempts += 1
        data = self.root_reader()
        end = self.clock() - self.origin
        if len(data) != 2:
            raise Stop('short Root LNKSTA read')
        raw = int.from_bytes(data, 'little')
        self._store(ROOT_ROW, start, end, raw, role)
        return raw

    def _skip(self, reason, role=0):
        # Skip is a software marker, not an extra hardware read or a timed MMIO.
        self.skip_counts[(reason, role)] += 1
        if self.count < self.limit:
            now = self.clock() - self.origin
            self._store(SKIP_ROW, now, now, SKIPS[reason], role)
        else:
            self.skip_markers_not_stored += 1

    def run(self):
        self.origin = self.clock()
        self.gpu.guard_reader = self._root
        self.gpu.permit_load = self._room
        try:
            while True:
                self.cycle += 1
                self.gpu_started = False
                try:
                    start, end, raw = self.gpu.read_gpu()
                    self._store(GPU_ROW, start - self.origin, end - self.origin, raw)
                    self.gpu_started = True
                    decode_gpu(raw)  # Invalid raw value is retained, then STOP.
                    self.gpu.postcheck()
                except Skip as exc:
                    self._skip(str(exc), role=int(self.gpu_started))
                    # Retry only passive root observation; never any command.
                    continue
                except Stop as exc:
                    if str(exc) == 'stop_dll_inactive':
                        self._skip(str(exc), role=int(self.gpu_started))
                    raise
        except End as exc:
            self.reason = str(exc)
        except KeyboardInterrupt:
            self.reason, self.error = 'interrupted', 'KeyboardInterrupt'
        except (OSError, ValueError, Stop) as exc:
            self.reason, self.error = 'stop', f'{type(exc).__name__}: {exc}'
        self.elapsed = self.clock() - self.origin
        # A window ending in Training is not a successful complete observation.
        last_root = None
        for row in self.rows():
            if row[0] == ROOT_ROW:
                last_root = row[3]
        if self.error is None and last_root is not None and root_action(last_root) != 'allow':
            self.reason, self.error = 'stop', 'window ended with unsafe root; no GPU access resumed'
        return self


def decode_channel(kind, raw):
    if kind == ROOT_ROW:
        if raw == 0xFFFF:
            raise ValueError('Root status unavailable')
        return decode_root(raw)
    return decode_gpu(raw)


def channel_report(result, kind):
    def samples():
        return ((start, end, raw) for tag, start, end, raw, _, _ in result.rows() if tag == kind)
    def intervals(which):
        previous = None
        for start, end, _ in samples():
            if which == 'read':
                yield end - start
            elif previous is not None:
                ps, pe = previous
                yield start - pe if which == 'no_read_gap' else start - ps if which == 'cycle' else end - ps
            previous = (start, end)
    timing = {k: distribution(intervals(k)) for k in ('read', 'no_read_gap', 'cycle', 'conservative_gap')}
    first = last = None
    changes = {}
    valid = invalid = 0
    for start, end, raw in samples():
        try:
            fields = decode_channel(kind, raw)
        except ValueError:
            invalid += 1
            continue
        valid += 1
        if first is None:
            first = (start, end)
        if last is not None:
            for key, value in fields.items():
                changes[key] = changes.get(key, 0) + (value != last[2][key])
        last = (start, end, fields)
    worst = result.elapsed if first is None else max(first[1], result.elapsed - last[0],
                                                    timing['conservative_gap']['max_ns'] or 0)
    return dict(timing=timing, changes=changes, valid_samples=valid, invalid_samples=invalid,
                conservative_max_gap_including_boundaries_ns=worst,
                leading_no_read_ns=result.elapsed if first is None else first[0],
                trailing_no_read_ns=result.elapsed if last is None else result.elapsed - last[1])


def summary(result):
    root = channel_report(result, ROOT_ROW)
    gpu = channel_report(result, GPU_ROW)
    skips = {'training_before_gpu': 0, 'dll_inactive_before_gpu': 0,
             'training_after_gpu_uncertain_bracket': 0, 'dll_inactive_after_gpu_uncertain_bracket': 0}
    skews = []
    root_end = None
    pending_gpu = False
    post_safe = post_unsafe = 0
    for kind, start, end, raw, _, role in result.rows():
        if kind == ROOT_ROW:
            root_end = end
            if pending_gpu:
                if root_action(raw) == 'allow':
                    post_safe += 1
                else:
                    post_unsafe += 1
                pending_gpu = False
        elif kind == GPU_ROW and root_end is not None:
            skews.append(start - root_end)
            pending_gpu = True
    for (reason, role), count in result.skip_counts.items():
        key = ('training' if reason == 'skip_training' else 'dll_inactive') + (
            '_after_gpu_uncertain_bracket' if role else '_before_gpu')
        skips[key] += count
    return dict(records=result.count, raw_buffer_bytes=len(result.buffer), record_bytes=ROW.size,
                capture_elapsed_ns=result.elapsed, requested_duration_ns=result.duration_ns,
                root_read_attempts=result.root_attempts, gpu_load_attempts=result.gpu.loads,
                uncovered_requested_tail_ns=max(0, result.duration_ns - result.elapsed),
                stop_reason=result.reason, error=result.error, overwrites=0,
                root=root, gpu=gpu, skips=skips,
                skip_markers_not_stored=result.skip_markers_not_stored,
                gpu_with_safe_post_root_sample=post_safe,
                gpu_with_unsafe_post_root_sample=post_unsafe,
                gpu_without_post_root_sample=int(pending_gpu),
                root_end_to_gpu_start_skew=distribution(skews),
                observability=interpretation(root['timing']['read']['count'],
                                            gpu['timing']['read']['count'], sum(skips.values())))


def emit_capture(log, result, report):
    prior = {}
    latest_root = None
    for kind, start, end, raw, cycle, role in result.rows():
        if kind == SKIP_ROW:
            event = dict(event='gpu_skipped' if not role else 'post_gpu_gate_unsafe',
                         reason='training' if raw == 1 else 'dll_inactive',
                         cycle=cycle, start_ns=start, end_ns=end,
                         latest_root_raw=latest_root,
                         training=None if latest_root is None else bool(latest_root & 0x800),
                         dllla=None if latest_root is None else bool(latest_root & 0x2000))
        else:
            channel = 'root' if kind == ROOT_ROW else 'gpu'
            try:
                fields = decode_channel(kind, raw)
            except ValueError:
                fields = dict(invalid=True)
            previous = prior.get(channel)
            changed = {} if previous is None else {key: dict(old=previous.get(key), new=value)
                                                   for key, value in fields.items() if previous.get(key) != value}
            event = dict(event='sample', channel=channel, cycle=cycle, role='postcheck' if role else 'guard_or_load',
                         start_ns=start, end_ns=end, read_duration_ns=end - start,
                         raw=raw, raw_hex=f'0x{raw:08X}', fields=fields, changed=changed)
            prior[channel] = fields
            if kind == ROOT_ROW:
                latest_root = raw
        log.write(json.dumps(event) + '\n')
    log.write(json.dumps(dict(event='summary', **report)) + '\n')
    log.flush()
    os.fsync(log.fileno())


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=float, default=.05, help='seconds, >0..10; first test default 50ms')
    parser.add_argument('--max-records', type=int, default=100_000, help='shared bounded root/GPU/skip buffer, <=1000000')
    parser.add_argument('--output-dir', type=Path, default=Path('work/pcie-dual-sampler'))
    a = parser.parse_args(argv)
    if not math.isfinite(a.duration) or not 0 < a.duration <= 10 or int(a.duration * 1e9) == 0:
        parser.error('duration must be finite, >0 and <=10 seconds')
    if not 1 <= a.max_records <= MAX_RECORDS:
        parser.error('max-records must be 1..1000000')
    return a


def main(argv=None):
    a = arguments(argv)
    a.output_dir.mkdir(parents=True, exist_ok=True)
    path = a.output_dir / f'gpu0-dual-{time.time_ns()}.jsonl'
    print('JSONL:', path, flush=True)
    with path.open('x', encoding='utf-8') as log, LOCK_PATH.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def durable(event, **row):
            log.write(json.dumps(dict(event=event, **row)) + '\n')
            log.flush()
            os.fsync(log.fileno())
        durable('manifest', started_utc=datetime.now(timezone.utc).isoformat(), kernel=os.uname().release,
                root=ROOT, gpu=GPU, root_offset='0xA2', gpu_offset='0x8C040',
                hardware_writes=False, automatic_unbind=False, gpu1_access=False,
                sequential_not_atomic=True, max_records=a.max_records,
                raw_buffer_bytes=a.max_records * ROW.size, requested_duration_s=a.duration,
                clock='monotonic_ns', no_fsync_in_hot_loop=True)
        root = gpu = None
        try:
            root = RootConfig()
            gpu = ReadOnlyGPU(root)
            durable('preflight', **gpu.preflight())
            result = Collection(root.read_status, gpu, int(a.duration * 1e9), a.max_records).run()
        except (OSError, ValueError, Stop, Skip, KeyboardInterrupt) as exc:
            durable('abort', reason=f'{type(exc).__name__}: {exc}')
            print('STOP:', exc)
            return 1
        finally:
            if gpu is not None:
                gpu.close()
            if root is not None:
                root.close()
        durable('capture_window', clock_origin_ns=result.origin, elapsed_ns=result.elapsed)
        report = summary(result)
        emit_capture(log, result, report)
    print(json.dumps(report))
    return 130 if result.reason == 'interrupted' else 1 if result.error else 0


if __name__ == '__main__':
    raise SystemExit(main())
