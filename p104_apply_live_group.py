#!/usr/bin/env python3
"""One guarded Linux invocation of Windows ApplyLiveGroup. No caller stages."""
import argparse
import ctypes
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import platform
import resource
import time

import pcie_bar0_readonly as ro
from design.p104_live_group_build import GROUP, load

POST_SPEED = dict(ro.CAP_READY)
POST_SPEED[0x8C040] = 0x20045800
APPLIED = dict(POST_SPEED)
APPLIED[0x88610] = 1
BASELINES = {'factory': ro.FACTORY, 'cap-ready': ro.CAP_READY,
             'post-speed': POST_SPEED, 'applied': APPLIED}


class LiveGroup(ro.Adapter):
    def __init__(self, *args, initial_state, **kwargs):
        if initial_state not in BASELINES:
            raise ro.Stop('unknown initial state')
        super().__init__(*args, profile='factory', **kwargs)
        self.initial_state = initial_state
        self.rw = False
        self.attempts, self.returned_stores, self.skipped, self.mask_failures = [], [], [], []
        self.frozen = None

    def record(self, event, **fields):
        if event == 'cleanup':
            fields.pop('device_writes', None)
            fields.update(store_calls_returned=self.returned_stores)
        super().record(event, **fields)

    def root_guard(self, role, record=True):
        # Same project checks, permitting an already-idle Gen2 x4 as Windows does.
        raw = ro.checked_read(self.rf, 0xA2, 2)
        if raw & 0x800 or not raw & 0x2000 or raw & 15 not in (1, 2) or (raw >> 4) & 63 != 4:
            self.record('unsafe_root', role=role, raw=raw)
            raise ro.Stop('Root Training/DLL/speed/width: no further GPU access')
        if time.monotonic()-self.start > 5:
            raise ro.Stop('five-second group/admission deadline')
        if (self.root_id & 0xFFFF != 0x8086 or
            ro.checked_read(self.rf, 0, 4) != self.root_id or
            (self.root/'power_state').read_text().strip() != 'D0' or
            not ro.checked_read(self.rf, 4, 2) & 2 or
            ro.checked_read(self.rf, 8, 4) >> 8 != 0x060400 or
            ro.checked_read(self.rf, 0x90, 1) != 0x10 or
            (ro.checked_read(self.rf, 0x92, 2) >> 4) & 15 != 4 or
            ro.checked_read(self.rf, 0xA0, 2) & 0x30 or
            not ro.checked_read(self.rf, 0x9C, 4) & (1 << 20)):
            raise ro.Stop('Root identity/D0/COMMAND/control deviation')
        return raw

    def state(self):
        if self.cfg(0, 4) != 0x1B8710DE:
            raise ro.Stop('GPU0 identity changed')
        command, pmcsr = self.cfg(4, 2), self.cfg(self.pm+4, 2)
        if command != 2 or pmcsr & 3:
            raise ro.Stop('GPU0 requires D0 and COMMAND=0002')
        sta = self.cfg(self.gc+18, 2)
        if sta & 0x800 or sta & 15 not in (1, 2) or (sta >> 4) & 63 != 4:
            raise ro.Stop('GPU Training/speed/width deviation')
        if self.cfg(self.gc+16, 2) & 0x30:
            raise ro.Stop('GPU Link Control deviation')
        self.resource()
        self.root_guard('device_status')
        if ro.checked_read(self.rf, 0x9A, 2) & 0x2F or self.cfg(self.gc+10, 2) & 0x2F:
            raise ro.Stop('PCIe Device Status error/pending')
        return dict(command=command, pmcsr=pmcsr, lnksta=sta)

    def aer(self):
        self.root_guard('aer')
        if self.ra is None or self.ga is None:
            raise ro.Stop('both AER capabilities required')
        result = dict(root=[], gpu=[])
        for endpoint, offsets in (('root', (4, 0x10, 0x30)), ('gpu', (4, 0x10))):
            for off in offsets:
                value = (ro.checked_read(self.rf, self.ra+off, 4) if endpoint == 'root'
                         else self.cfg(self.ga+off, 4))
                result[endpoint].append(value)
                if value:
                    self.record('aer_error', status=result)
                    raise ro.Stop(f'{endpoint} AER error')
        return result

    def links(self):
        sta = self.root_guard('snapshot')
        root = {name: ro.checked_read(self.rf, 0x90+off, size)
                for name, off, size in [('cap', 12, 4), ('cap2', 44, 4), ('ctl', 16, 2), ('ctl2', 48, 2)]}
        root['sta'] = sta
        gpu = {'sta': self.cfg(self.gc+18, 2)}
        if gpu['sta'] & 0x800 or gpu['sta'] & 15 not in (1, 2) or (gpu['sta'] >> 4) & 63 != 4:
            raise ro.Stop('GPU link snapshot not idle x4')
        gpu.update({name: self.cfg(self.gc+off, size)
                    for name, off, size in [('cap', 12, 4), ('cap2', 44, 4), ('ctl', 16, 2), ('ctl2', 48, 2)]})
        if root['sta'] & 15 != gpu['sta'] & 15:
            self.record('unsafe_links', root=root, gpu=gpu)
            raise ro.Stop('Root/GPU physical speed disagreement')
        return dict(root=root, gpu=gpu)

    def guard(self):
        self.state()
        errors = self.aer()
        links = self.links()
        if self.frozen is not None:
            for endpoint in ('root', 'gpu'):
                if any(links[endpoint][k] != self.frozen[endpoint][k] for k in ('ctl', 'ctl2')):
                    raise ro.Stop('PCIe controls changed outside MMIO-only group')
            if any(links['root'][k] != self.frozen['root'][k] for k in ('cap', 'cap2')):
                raise ro.Stop('Root capabilities changed')
        return dict(links=links, aer=errors)

    def read_word(self, offset):
        if offset not in ro.FACTORY or not self.mapping:
            raise ro.Stop('BAR read offset/owner denied')
        self.state()
        self.gate()
        pre = self.root_guard('bar_pre', record=False)
        begin = time.monotonic_ns()
        self.loads += 1
        value = (self.lib.live_read(self.mapping, offset) if self.rw
                 else self.lib.ro_read(self.mapping, offset))
        end = time.monotonic_ns()
        post = self.root_guard('bar_post', record=False)
        self.record('dword', offset=offset, value=value, start_ns=begin, end_ns=end,
                    root_pre=pre, root_post=post)
        if value == 0xFFFFFFFF or value >> 16 == 0xBADF or value > 0xFFFFFFFF:
            raise ro.Stop('BAR unavailable/sentinel; no following register access')
        if offset == 0x8C040 and value & 0xA000001F != 0x20000000:
            raise ro.Stop('internal ACTIVE/directive or known x4 SPEED safety flags changed')
        return value

    def exact_initial(self):
        frame = self.guard()
        actual = {offset: self.read_word(offset) for offset in ro.FACTORY}
        expected = BASELINES[self.initial_state]
        self.record('initial_bar', state=self.initial_state, expected=expected, actual=actual)
        if actual != expected:
            raise ro.Stop('exact selected initial state mismatch; no automatic preparation')
        gpu, root = frame['links']['gpu'], frame['links']['root']
        cap, cap2 = (0x00453C41, 2) if self.initial_state == 'factory' else (0x00453C42, 6)
        if gpu['cap'] != cap or gpu['cap2'] != cap2:
            raise ro.Stop('initial GPU declaration mismatch')
        if root['cap'] & 15 != 3 or (root['cap'] >> 4) & 63 != 16 or root['cap2'] & 14 != 14:
            raise ro.Stop('Root declaration mismatch')
        tls = (gpu['ctl2'], root['ctl2'])
        allowed = {(0, 0x43)} if self.initial_state == 'factory' else {(0, 0x43), (2, 0x42)}
        if self.initial_state == 'post-speed':
            allowed = {(2, 0x42)}
        if tls not in allowed:
            raise ro.Stop('initial full Link Control 2 words outside selected profile')
        if any(v['sta'] & 15 > (v['cap'] & 15) for v in frame['links'].values()):
            raise ro.Stop('initial physical speed contradicts declared maximum')
        self.frozen = frame['links']
        return frame

    def admit(self):
        self.open()  # Proven read-only lifecycle and mapping-review admission.
        frame = self.exact_initial()
        self.record('group_plan', recipe=GROUP, delay_ms=20, initial=frame,
                    scope='ApplyLiveGroup only; no LiveLatch/TLS/retrain/reset/width/holder')

    def map_writer(self):
        self.exact_initial()
        mapping, self.mapping = self.mapping, None
        if self.lib.ro_close(mapping):
            raise ro.Stop('RO munmap failed')
        fd, self.bf = self.bf, None
        os.close(fd)
        self.guard()
        self.gate()
        self.bf = os.open(self.gpu/'resource0', os.O_RDWR | os.O_SYNC | os.O_CLOEXEC | os.O_NOFOLLOW)
        self.guard()
        self.gate()
        self.mapping = self.lib.live_map(self.bf)
        if not self.mapping:
            raise OSError(ctypes.get_errno(), 'RW resource0 mapping failed')
        self.rw = True
        self.exact_initial()  # Mapping creation must not initialize device words.

    def group(self):
        ok = True
        for index, (offset, mask, value) in enumerate(GROUP):
            self.guard()
            old = self.read_word(offset)
            new = (old & ~mask) | (value & mask)
            self.record('rmw_plan', index=index, offset=offset, old=old, new=new, mask=mask)
            self.guard()
            self.gate()
            pre = self.root_guard('step_pre', record=False)
            self.attempts.append(index)
            begin = time.monotonic_ns()
            rc = self.lib.live_step(self.mapping, index, old)
            end = time.monotonic_ns()
            if rc == 0:
                self.returned_stores.append(index)
            post = self.root_guard('step_post', record=False)
            self.record('native_return', index=index, code=rc, start_ns=begin, end_ns=end,
                        root_pre=pre, root_post=post, pci_completion_proven=False)
            if rc not in (0, 1) or (rc == 1) != (new == old):
                raise ro.Stop(f'native step/order/old-value refusal: {rc}; no retry')
            if rc == 1:
                self.skipped.append(index)
                self.record('skip_unchanged', index=index, offset=offset, value=old,
                            sleep=False, readback=False)
                continue
            sleep_start = time.monotonic_ns()
            time.sleep(0.020)  # Windows Thread.Sleep(20), only after a changed store.
            sleep_end = time.monotonic_ns()
            self.guard()
            rb = self.read_word(offset)
            masked_ok = (rb & mask) == (value & mask)
            ok &= masked_ok  # Windows ok &= RmwVerify: no short circuit on valid mismatch.
            if not masked_ok:
                self.mask_failures.append(index)
            self.record('masked_readback', index=index, offset=offset, readback=rb, mask=mask,
                        wanted=value & mask, masked_ok=masked_ok, sleep_start_ns=sleep_start,
                        sleep_end_ns=sleep_end, outside_mask_changed=bool((rb ^ new) & ~mask))
        self.record('group_complete', masked_ok=ok, final=self.guard())
        return ok

    def close(self):
        error = None
        if self.rw and self.mapping is not None:
            mapping, self.mapping = self.mapping, None
            if self.lib.live_close(mapping):
                error = 'RW munmap failed'
        try:
            super().close()
        finally:
            if error:
                raise ro.Stop(error)


def run(adapter, apply):
    result = dict(event='result', result='FAIL', reason='not_started', apply=apply,
                  group_completed=False, gen2_verified=False, config_writes=0)
    try:
        adapter.admit()
        if apply:
            adapter.map_writer()
            ok = adapter.group()
            result.update(group_completed=True, result='PASS' if ok else 'FAIL',
                          reason='windows_group_masked_ok' if ok else 'windows_group_masked_failure')
        else:
            for index, (offset, mask, value) in enumerate(GROUP):
                old = adapter.read_word(offset)
                new = (old & ~mask) | (value & mask)
                adapter.record('dry_run_step', index=index, offset=offset, old=old, new=new,
                               skip=old == new, delay_ms=0 if old == new else 20)
            result.update(result='PASS', reason='dry_run_no_write')
    except (OSError, ValueError, KeyError, IndexError, ro.Stop) as exc:
        result['reason'] = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            adapter.close()
        except (OSError, ro.Stop) as exc:
            result.update(result='FAIL', reason=f'cleanup: {exc}')
    result.update(steps_attempted=adapter.attempts, store_calls_returned=adapter.returned_stores,
                  skipped=adapter.skipped, mask_failures=adapter.mask_failures)
    adapter.emit(result)
    return result


def worker(connection, binary, review, initial_state, apply):
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        fd = os.open(ro.LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lib, manifest = load(binary)
            connection.send(dict(event='native', manifest=manifest))
            result = run(LiveGroup(lib, review, connection.send, initial_state=initial_state), apply)
        finally:
            os.close(fd)
        connection.close()
        raise SystemExit(0 if result['result'] == 'PASS' else 1)
    except Exception as exc:
        connection.send(dict(event='result', result='FAIL', reason=f'{type(exc).__name__}: {exc}'))
        connection.close()
        raise SystemExit(1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native-library', required=True, type=Path)
    p.add_argument('--mapping-review', required=True, type=Path)
    p.add_argument('--initial-state', choices=BASELINES, required=True)
    p.add_argument('--output', required=True, type=Path)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--dry-run', action='store_true')
    p.add_argument('--confirm-gpu0-unbound', action='store_true', required=True)
    p.add_argument('--confirm-exclusive-access', action='store_true', required=True)
    p.add_argument('--confirm-live-group-risk', action='store_true')
    args = p.parse_args()
    if args.apply and not args.confirm_live_group_risk:
        p.error('--apply requires --confirm-live-group-risk')
    try:
        review = json.loads(args.mapping_review.read_text())
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    if not isinstance(review, dict):
        p.error('mapping review must be an object')
    output = args.output.resolve()
    if any(output.is_relative_to(root) for root in map(Path, ('/sys', '/proc', '/dev'))):
        p.error('output must be a regular log path')
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as log:
        def emit(row):
            log.write(json.dumps(row, sort_keys=True)+'\n')
            log.flush()
        emit(dict(event='start', scope='Windows ApplyLiveGroup', gpu=ro.GPU, root=ro.ROOT,
                  initial_state=args.initial_state, apply=args.apply, config_writes=False))
        context = multiprocessing.get_context('spawn')
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=worker,
            args=(send, args.native_library.resolve(), review, args.initial_state, args.apply))
        process.start()
        send.close()
        try:
            result = ro.supervise(process, receive, emit, 10)
        except KeyboardInterrupt:
            emit(dict(event='result', result='FAIL', reason='interrupted; no rollback/retry'))
            result = 1
        finally:
            if process.is_alive():
                process.kill()
                process.join(1)
            receive.close()
    print(f'{"GROUP PASS" if result == 0 else "FAIL/STOP"}: {output}')
    return result


if __name__ == '__main__':
    raise SystemExit(main())
