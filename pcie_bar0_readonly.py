#!/usr/bin/env python3
"""One-pass T5600 GPU0 read-only BAR0 probe. No write/recovery executor."""
import argparse
import ctypes
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import platform
import resource
import signal
import stat
import time

from design.p104_bar0_ro_build import load

GPU, ROOT = '0000:03:00.0', '0000:00:02.0'
SYSFS = Path('/sys/bus/pci/devices')
LOCK = Path('/run/lock/p104-gen2-linux.lock')
FACTORY = {0x8872C: 0, 0x2241C: 0, 0x8841C: 0xC0B46500,
           0x8C2C0: 0x068711B3, 0x8C300: 0x040A00C0,
           0x8C040: 0x20085800, 0x8C1C0: 0x00010032, 0x88610: 0}
CAP_READY = dict(FACTORY)
CAP_READY.update({0x8872C: 6, 0x2241C: 0x81, 0x8841C: 0xC0B40500,
                  0x8C1C0: 0x00020032})
PROFILES = {'factory': FACTORY, 'cap-ready': CAP_READY}


class Stop(RuntimeError):
    pass


def checked_read(fd, offset, size):
    data = os.pread(fd, size, offset)
    if len(data) != size or data == b'\xff' * size:
        raise Stop(f'config unavailable/short at {offset:#x}')
    return int.from_bytes(data, 'little')


def capabilities(read):
    result, seen = {}, set()
    p = read(0x34, 1)
    while p:
        if p & 3 or not 0x40 <= p <= 0xFC or p in seen:
            raise Stop('malformed capability chain')
        seen.add(p)
        kind = read(p, 1)
        if kind in result:
            raise Stop('duplicate capability')
        result[kind] = p
        p = read(p+1, 1)
    return result


def aer_cap(read):
    p, seen = 0x100, set()
    while p:
        if p & 3 or not 0x100 <= p <= 0xFFC or p in seen:
            raise Stop('malformed extended capability chain')
        seen.add(p)
        header = read(p, 4)
        if header == 0:
            return None
        if header & 0xFFFF == 1:
            if p > 0xFC8:
                raise Stop('AER capability out of bounds')
            return p
        p = (header >> 20) & 0xFFF
    return None


class Adapter:
    """Internal sysfs injection is for fixtures only; CLI always uses fixed SYSFS.

    No write method. Caller serializes lifecycle; native access keeps the GIL.
    CLI owns this object in an isolated single-threaded worker.
    """
    def __init__(self, lib, review, emit, profile='factory', sysfs=SYSFS):
        if profile not in PROFILES:
            raise Stop('unknown baseline profile')
        self.lib, self.review, self.emit = lib, review, emit
        self.gpu, self.root = Path(sysfs)/GPU, Path(sysfs)/ROOT
        self.profile = profile
        self.rf = self.gf = self.bf = self.mapping = None
        self.resources = self.pm = self.gc = self.ga = self.ra = None
        self.root_id = None
        self.start = time.monotonic()
        self.loads = 0

    def record(self, event, **fields):
        self.emit(dict(event=event, monotonic_ns=time.monotonic_ns(), **fields))

    def root_guard(self, role, record=True):
        if time.monotonic() - self.start > 5:
            raise Stop('five-second admission/read deadline exceeded')
        identity = checked_read(self.rf, 0, 4)
        if identity & 0xFFFF != 0x8086 or (self.root_id is not None and identity != self.root_id):
            raise Stop('Root identity lost/changed')
        if (self.root/'power_state').read_text().strip() != 'D0':
            raise Stop('Root must already be D0')
        if not checked_read(self.rf, 4, 2) & 2:
            raise Stop('Root memory forwarding disabled')
        if checked_read(self.rf, 8, 4) >> 8 != 0x060400:
            raise Stop('Root PCI class mismatch')
        if checked_read(self.rf, 0x90, 1) != 0x10:
            raise Stop('Root PCIe capability changed')
        if (checked_read(self.rf, 0x92, 2) >> 4) & 15 != 4:
            raise Stop('Root PCIe type changed')
        ctl = checked_read(self.rf, 0xA0, 2)
        raw = checked_read(self.rf, 0xA2, 2)
        unsafe = bool(raw & 0x800 or not raw & 0x2000 or raw & 0x3FF != 0x41 or ctl & 0x30)
        if record or unsafe:
            self.record('root_check', role=role, raw=raw, speed=raw & 15,
                        width=(raw >> 4) & 63, training=bool(raw & 0x800),
                        dllla=bool(raw & 0x2000), lbms=bool(raw & 0x4000))
        if unsafe:
            raise Stop('Root Training/DLL/link deviation; no further GPU access')
        if not checked_read(self.rf, 0x9C, 4) & (1 << 20):
            raise Stop('Root DLLLA reporting unsupported')
        return raw

    def gate(self):
        self.root_guard('before_endpoint')
        try:
            (self.gpu/'driver').lstat()
        except FileNotFoundError:
            pass
        else:
            raise Stop('GPU0 bound to a driver; no endpoint access')
        if (self.gpu/'power_state').read_text().strip() != 'D0':
            raise Stop('GPU0 must already be D0')
        if (self.gpu/'power/runtime_status').read_text().strip() != 'active':
            raise Stop('GPU0 runtime PM not active')
        if self.gpu.resolve(strict=True).parent != self.root.resolve(strict=True):
            raise Stop('GPU0/Root topology mismatch')

    def cfg(self, offset, size):
        self.gate()
        value = checked_read(self.gf, offset, size)
        self.root_guard('after_endpoint_config')
        return value

    def resource(self):
        self.gate()
        line = (self.gpu/'resource').read_text().splitlines()[0].split()
        if len(line) != 3:
            raise Stop('invalid BAR0 resource metadata')
        start, end, flags = (int(v, 16) for v in line)
        page = os.sysconf('SC_PAGE_SIZE')
        length = ((max(FACTORY)+4+page-1)//page)*page
        size = end-start+1
        if not start or end < start or start % page or size < length or not flags & 0x200 or flags & 0x100:
            raise Stop('BAR0 not assigned MMIO covering mapping')
        lo = self.cfg(0x10, 4)
        kind = (lo >> 1) & 3
        if lo & 1 or kind not in (0, 2):
            raise Stop('invalid PCI BAR0 type')
        address = lo & ~15
        if kind == 2:
            address |= self.cfg(0x14, 4) << 32
        if (address != start or bool(lo & 8) != bool(flags & 0x2000) or
            (kind == 2) != bool(flags & 0x100000)):
            raise Stop('BAR0 PCI/resource mismatch')
        result = dict(start=start, end=end, size=size, flags=flags, map_length=length)
        if self.resources is not None and result != self.resources:
            raise Stop('BAR0 resource changed')
        return result

    def state(self):
        if self.cfg(0, 4) != 0x1B8710DE:
            raise Stop('GPU0 PCI identity mismatch')
        command = self.cfg(4, 2)
        pmcsr = self.cfg(self.pm+4, 2)
        link = self.cfg(self.gc+0x12, 2)
        ctl = self.cfg(self.gc+0x10, 2)
        if not command & 2 or command & 4 or pmcsr & 3:
            raise Stop('require D0, MSE=1, BME=0')
        if link & 0x3FF != 0x41 or link & 0x800 or ctl & 0x30:
            raise Stop('GPU0 link must be idle Gen1 x4')
        self.resource()
        return dict(command=command, pmcsr=pmcsr, lnksta=link)

    def check_review(self):
        r = self.review
        if (r.get('gpu') != GPU or r.get('root') != ROOT or
            r.get('kernel_release') != platform.release() or
            r.get('bar0_start') != self.resources['start'] or
            r.get('bar0_size') != self.resources['size'] or
            r.get('effective_type') not in ('UC', 'UC-') or
            r.get('kernel_mapping_verified') is not True or
            r.get('conflicting_aliases_excluded') is not True or
            not isinstance(r.get('evidence'), str) or not r['evidence'].strip()):
            raise Stop('mapping review missing/mismatched: no BAR mapping')
        self.record('mapping_review', review=r, note='operator evidence, not automatic PAT certification')

    def aer(self):
        result = {}
        self.root_guard('aer_root')
        if self.ra:
            result['root'] = [checked_read(self.rf, self.ra+off, 4) for off in (4, 0x10, 0x30)]
        if self.ga:
            result['gpu'] = [self.cfg(self.ga+off, 4) for off in (4, 0x10)]
        return result

    def check_aer(self):
        now = self.aer()
        self.record('aer', status=now)
        if any(n & ~o for key, values in now.items()
               for n, o in zip(values, self.initial_aer[key])):
            raise Stop('new AER status; no clearing/retry')

    def open(self):
        if any(v is not None for v in (self.rf, self.gf, self.bf, self.mapping)):
            raise Stop('adapter already open')
        if platform.machine() != 'x86_64':
            raise Stop('x86_64 required')
        self.rf = os.open(self.root/'config', os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        self.root_id = checked_read(self.rf, 0, 4)
        if checked_read(self.rf, 0xA2, 2) & 0x800:
            raise Stop('Root Training before admission')
        rc = capabilities(lambda o, n: checked_read(self.rf, o, n))
        if rc.get(0x10) != 0x90:
            raise Stop('Root PCIe capability must be at 0x90')
        self.gate()
        self.gf = os.open(self.gpu/'config', os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        caps = capabilities(self.cfg)
        self.pm, self.gc = caps.get(1), caps.get(0x10)
        if self.pm is None or self.gc is None:
            raise Stop('GPU PM/PCIe capability missing')
        if (self.cfg(self.gc+2, 2) >> 4) & 15 != 0:
            raise Stop('GPU0 must be a PCIe endpoint')
        self.resources = self.resource()
        state = self.state()
        self.check_review()
        self.ra = aer_cap(lambda o, n: checked_read(self.rf, o, n))
        self.ga = aer_cap(self.cfg)
        self.initial_aer = self.aer()
        self.record('baseline', gpu=GPU, root=ROOT, profile=self.profile,
                    state=state, root_identity=self.root_id, resources=self.resources, aer=self.initial_aer,
                    links=self.links())
        self.gate()
        self.bf = os.open(self.gpu/'resource0', os.O_RDONLY | os.O_SYNC | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(self.bf)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < self.resources['map_length']:
            raise Stop('resource0 file type/size mismatch')
        self.state()
        self.gate()
        self.mapping = self.lib.ro_map(self.bf)
        if not self.mapping:
            raise OSError(ctypes.get_errno(), 'PROT_READ/MAP_SHARED mmap failed')
        self.state()
        self.record('mapped', protection='PROT_READ', flags='MAP_SHARED', offset=0,
                    bytes=self.resources['map_length'], device_writes=0)

    def links(self):
        result = {'root': {name: checked_read(self.rf, 0x90+off, size)
                          for name, off, size in [('cap', 12, 4), ('cap2', 44, 4),
                                                  ('sta', 18, 2), ('ctl2', 48, 2)]}}
        result['gpu'] = {name: self.cfg(self.gc+off, size)
                         for name, off, size in [('cap', 12, 4), ('cap2', 44, 4),
                                                 ('sta', 18, 2), ('ctl2', 48, 2)]}
        return result

    def read(self, offset):
        if type(offset) is not int or offset not in FACTORY or offset & 3 or not self.mapping:
            raise Stop('read offset/closed mapping denied')
        self.state()
        self.record('load_begin', offset=offset, note='pending attempt, not proof of load')
        self.gate()
        pre = self.root_guard('mmio_pre', record=False)
        begin = time.monotonic_ns()
        self.loads += 1
        value = self.lib.ro_read(self.mapping, offset)
        end = time.monotonic_ns()
        # Root is the FIRST hardware read after the load, even on a bad value.
        post = self.root_guard('mmio_post', record=False)
        self.record('dword', offset=offset, value=value, start_ns=begin, end_ns=end,
                    root_pre=pre, root_post=post, atomic_snapshot=False)
        if post != pre:
            raise Stop('Root status changed across load; no next read')
        if value == 0xFFFFFFFF or value >> 16 == 0xBADF or value > 0xFFFFFFFF:
            raise Stop('MMIO unavailable/sentinel')
        if value != PROFILES[self.profile][offset]:
            raise Stop(f'baseline mismatch at {offset:#x}; no next read')
        self.check_aer()
        return value

    def close(self):
        errors = []
        mapping, self.mapping = self.mapping, None
        if mapping is not None:
            if self.lib.ro_close(mapping):
                errors.append('munmap failed')
        for name in ('bf', 'gf', 'rf'):
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError as exc:
                    errors.append(f'{name}: {exc}')
        self.record('cleanup', errors=errors, device_writes=0)
        if errors:
            raise Stop('; '.join(errors))


def run_once(adapter):
    outcome = dict(event='result', result='FAIL', reason='not_started', loads=0)
    try:
        adapter.open()
        for offset in FACTORY:
            adapter.read(offset)
        adapter.state()
        adapter.check_aer()
        adapter.record('final_links', links=adapter.links())
        outcome.update(result='PASS', reason='one_pass_complete')
    except (OSError, ValueError, KeyError, IndexError, Stop) as exc:
        outcome.update(reason=f'{type(exc).__name__}: {exc}')
    finally:
        try:
            adapter.close()
        except (OSError, Stop) as exc:
            outcome.update(result='FAIL', reason=f'cleanup: {exc}')
    outcome['loads'] = adapter.loads
    adapter.emit(outcome)
    return outcome


def worker(connection, binary, review, profile):
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        # Shared advisory lock only; it cannot prevent another driver's bind.
        fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lib, manifest = load(binary)
            connection.send(dict(event='native', manifest=manifest))
            result = run_once(Adapter(lib, review, connection.send, profile))
        finally:
            os.close(fd)
        connection.close()
        raise SystemExit(0 if result['result'] == 'PASS' else 1)
    except Exception as exc:
        connection.send(dict(event='result', result='FAIL', reason=f'{type(exc).__name__}: {exc}'))
        connection.close()
        raise SystemExit(1)


def supervise(process, receiver, emit, timeout):
    deadline = time.monotonic()+timeout
    last = None
    count = 0
    timed_out = False
    eof = False
    while not eof:
        if time.monotonic() >= deadline:
            timed_out = True
            process.kill()
            break
        if receiver.poll(0.05):
            try:
                row = receiver.recv()
            except EOFError:
                eof = True
                continue
            count += 1
            if count > 3000:
                process.kill()
                last = dict(event='result', result='FAIL', reason='record_limit')
                emit(last)
                break
            emit(row)
            if row.get('event') == 'result':
                last = row
        elif not process.is_alive():
            # A dead worker can leave pending final records; drain on next poll.
            if not receiver.poll():
                break
    process.join(1)
    if process.is_alive():
        process.kill()
        process.join(1)
    if (timed_out or last is None or process.exitcode not in (0, 1) or
        (process.exitcode != 0 and last.get('result') == 'PASS')):
        code = process.exitcode
        reason = 'timeout' if timed_out else f'worker_exit:{code}'
        if code is not None and code < 0:
            reason += ':'+signal.Signals(-code).name
        last = dict(event='result', result='FAIL', reason=reason,
                    note='no retry/recovery; do not probe endpoint after link loss')
        emit(last)
    return 0 if last['result'] == 'PASS' else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native-library', type=Path, required=True)
    p.add_argument('--mapping-review', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--baseline', choices=PROFILES, default='factory')
    p.add_argument('--confirm-gpu0-unbound', action='store_true', required=True)
    p.add_argument('--confirm-exclusive-access', action='store_true', required=True)
    args = p.parse_args()
    try:
        review = json.loads(args.mapping_review.read_text())
    except (OSError, ValueError) as exc:
        p.error(f'mapping review: {exc}')
    if not isinstance(review, dict):
        p.error('mapping review must be an object')
    output = args.output.resolve()
    if any(output.is_relative_to(root) for root in map(Path, ('/sys', '/proc', '/dev'))):
        p.error('output must be a regular log path')
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as log:
        def emit(row):
            log.write(json.dumps(row, sort_keys=True)+'\n')
            log.flush()  # No fsync and never a device mapping operation.
        emit(dict(event='start', gpu=GPU, root=ROOT, kernel=platform.release(),
                  baseline=args.baseline, writes_allowed=False, timeout_seconds=10))
        context = multiprocessing.get_context('spawn')
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=worker, args=(send, args.native_library.resolve(), review, args.baseline))
        process.start()
        send.close()
        try:
            result = supervise(process, receive, emit, 10)
        finally:
            if process.is_alive():
                process.kill()
                process.join(1)
            receive.close()
    print(f'{"PASS" if result == 0 else "FAIL/STOP"}: {output}')
    return result


if __name__ == '__main__':
    raise SystemExit(main())
