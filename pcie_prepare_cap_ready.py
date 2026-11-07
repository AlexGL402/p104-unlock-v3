#!/usr/bin/env python3
"""Factory -> cap-ready GPU0 only. Fixed four MMIO steps, TLS-only, no SPEED."""
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
from design.p104_cap_ready_build import load

# Same validated order as stage1-override followed by stage1-cap-speed.
STEPS = ((0x8872C, 6), (0x2241C, 0x81), (0x8841C, 0xC0B40500),
         (0x8C1C0, 0x00020032))


class Preparation(ro.Adapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, profile='factory', **kwargs)
        self.expected = dict(ro.FACTORY)
        self.phase = self.completed_mmio = 0
        self.rw = False
        self.mmio_attempts, self.mmio_returns, self.tls_attempts = [], [], []
        self.tls_expected = self.initial_links = None
        self.tls_done = []
        self.safe_root_rows_suppressed = 0

    def root_guard(self, role, record=True):
        # Base still emits every unsafe observation, including bad Link Control
        # with an otherwise normal STA. Suppress only successfully checked rows.
        raw = super().root_guard(role, record=False)
        if record:
            self.safe_root_rows_suppressed += 1
        return raw

    def record(self, event, **fields):
        if event == 'cleanup':
            fields.pop('device_writes', None)
            fields.update(mmio_attempts=self.mmio_attempts, tls_attempts=self.tls_attempts)
        super().record(event, **fields)

    def links(self):
        # Validate Root status before ANY endpoint snapshot read, not afterwards.
        raw = self.root_guard('snapshot_root')
        root = {name: ro.checked_read(self.rf, 0x90+off, size)
                for name, off, size in [('cap', 12, 4), ('cap2', 44, 4),
                                        ('ctl2', 48, 2), ('ctl', 16, 2)]}
        root['sta'] = raw
        gpu = {'sta': self.cfg(self.gc+18, 2)}
        if gpu['sta'] & 0x3FF != 0x41 or gpu['sta'] & 0x800:
            raise ro.Stop('GPU link not idle Gen1 x4')
        gpu.update({name: self.cfg(self.gc+off, size)
                    for name, off, size in [('cap', 12, 4), ('cap2', 44, 4),
                                            ('ctl2', 48, 2), ('ctl', 16, 2)]})
        return dict(root=root, gpu=gpu)

    def check_aer(self):
        self.root_guard('aer_root')
        if not self.ra or not self.ga:
            raise ro.Stop('both AER capabilities required')
        status = dict(root=[], gpu=[])
        for name, offsets in (('root', (4, 0x10, 0x30)), ('gpu', (4, 0x10))):
            for off in offsets:
                value = (ro.checked_read(self.rf, self.ra+off, 4) if name == 'root'
                         else self.cfg(self.ga+off, 4))
                status[name].append(value)
                if value:
                    self.record('aer', status=status)
                    raise ro.Stop(f'nonzero {name} AER; no further endpoint access')
        self.record('aer', status=status)
        return status

    def aer(self):
        return self.check_aer()  # Fail on initial AER too, before further GPU access.

    def state(self):
        state = super().state()
        self.root_guard('device_status')
        root_status = ro.checked_read(self.rf, 0x9A, 2)
        if root_status & 0x2F:
            raise ro.Stop(f'Root Device Status {root_status:#x}: error/transactions pending')
        gpu_status = self.cfg(self.gc+0xA, 2)
        if gpu_status & 0x2F:
            raise ro.Stop(f'GPU Device Status {gpu_status:#x}: error/transactions pending')
        state.update(root_device_status=root_status, gpu_device_status=gpu_status)
        return state

    def read_word(self, offset, expected):
        if type(offset) is not int or offset not in ro.FACTORY or not self.mapping:
            raise ro.Stop('BAR read denied')
        self.state()
        self.gate()
        pre = self.root_guard('mmio_read_pre', record=False)
        begin = time.monotonic_ns()
        self.loads += 1
        value = (self.lib.prep_read(self.mapping, offset) if self.rw
                 else self.lib.ro_read(self.mapping, offset))
        end = time.monotonic_ns()
        post = self.root_guard('mmio_read_post', record=False)
        self.record('dword', phase=self.phase, offset=offset, expected=expected, value=value,
                    start_ns=begin, end_ns=end, root_pre=pre, root_post=post)
        if value != expected:
            raise ro.Stop(f'exact BAR baseline/readback mismatch at {offset:#x}')
        return value

    def snapshot(self, role):
        state = self.state()
        self.check_aer()
        links = self.links()
        self.validate_links(links)
        values = {off: self.read_word(off, value) for off, value in self.expected.items()}
        self.record('snapshot', role=role, phase=self.phase, state=state,
                    registers=values, links=links)
        return links

    def validate_links(self, links):
        gpu, root = links['gpu'], links['root']
        cap, cap2 = (0x00453C42 if self.phase == 4 else 0x00453C41), (6 if self.phase >= 3 else 2)
        if gpu['cap'] != cap or gpu['cap2'] != cap2:
            self.record('capability_mismatch', phase=self.phase, expected_cap=cap,
                        expected_cap2=cap2, links=links)
            raise ro.Stop('unexpected capability stage; no next operation')
        if root['cap'] & 15 != 3 or (root['cap'] >> 4) & 63 != 16 or root['cap2'] & 14 != 14:
            raise ro.Stop('T5600 Root capability mismatch')
        if self.initial_links is not None:
            if any(root[k] != self.initial_links['root'][k] for k in ('cap', 'cap2')):
                raise ro.Stop('Root capability changed')
            for endpoint in ('gpu', 'root'):
                if links[endpoint]['ctl'] != self.initial_links[endpoint]['ctl']:
                    raise ro.Stop('Link Control changed; no reset/retrain writes allowed')
                if links[endpoint]['ctl2'] != self.tls_expected[endpoint]:
                    raise ro.Stop('Link Control 2 changed outside planned TLS operation')

    def admit(self):
        self.open()  # Reuse read-only identity/topology/BAR/D0/COMMAND/mapping guards.
        links = self.snapshot('factory_admission')
        if links['gpu']['ctl2'] & 15 not in (1, 2) or links['root']['ctl2'] & 15 not in (1, 2, 3):
            raise ro.Stop('unexpected initial TLS')
        if any(v['ctl2'] & 0x810 for v in links.values()):
            raise ro.Stop('compliance controls must be off')
        self.initial_links = links
        self.tls_expected = {name: v['ctl2'] for name, v in links.items()}
        self.record('plan', mmio_steps=STEPS, tls_order=[ro.GPU, ro.ROOT], retrain=False,
                    speed_writes=False, rollback=False)

    def map_writer(self):
        self.snapshot('before_rw_map')
        mapping, self.mapping = self.mapping, None
        if self.lib.ro_close(mapping):
            raise ro.Stop('RO munmap failed')
        fd, self.bf = self.bf, None
        os.close(fd)
        self.state()
        self.gate()
        self.bf = os.open(self.gpu/'resource0', os.O_RDWR | os.O_SYNC | os.O_CLOEXEC | os.O_NOFOLLOW)
        self.state()
        self.gate()
        self.mapping = self.lib.prep_map(self.bf)
        if not self.mapping:
            raise OSError(ctypes.get_errno(), 'preparation mmap failed')
        self.rw = True
        self.snapshot('after_rw_map')

    def mmio_step(self, index):
        self.start = time.monotonic()  # Same five-second guard budget, per fixed stage.
        if type(index) is not int or not 0 <= index < 4 or index != self.phase or index in self.mmio_attempts or not self.rw:
            raise ro.Stop('MMIO step out of order/already consumed')
        self.snapshot('before_mmio')
        offset, target = STEPS[index]
        self.record('mmio_intent', step=index, offset=offset, old=self.expected[offset], new=target)
        self.state()
        self.check_aer()
        self.gate()
        pre = self.root_guard('mmio_write_pre', record=False)
        self.mmio_attempts.append(index)
        begin = time.monotonic_ns()
        rc = self.lib.prep_step(self.mapping, index)
        end = time.monotonic_ns()
        if rc == 0:
            self.mmio_returns.append(index)
        post = self.root_guard('mmio_write_post', record=False)  # FIRST hardware read.
        self.record('mmio_return', step=index, code=rc, start_ns=begin, end_ns=end,
                    root_pre=pre, root_post=post, completion_proven=False)
        if rc:
            raise ro.Stop(f'native MMIO step refused: {rc}; no retry')
        self.check_aer()
        self.read_word(offset, target)  # Separate full DWORD readback.
        self.expected[offset] = target
        self.phase += 1
        self.snapshot('after_mmio')  # Includes BOTH CAP/CAP2/STA after every write.
        self.hold_stage()  # Preserve the staged tool's two-second observation window.
        self.snapshot('settled_mmio')
        self.completed_mmio = self.phase

    def hold_stage(self):
        deadline = time.monotonic()+2
        samples = 0
        while time.monotonic() < deadline and samples < 20:
            self.state()
            self.check_aer()
            links = self.links()
            self.validate_links(links)
            samples += 1
            self.record('stage_observation', phase=self.phase, sample=samples, links=links)
            remaining = deadline-time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
        self.record('stage_settled', phase=self.phase, samples=samples,
                    observation_seconds=2, write_retries=0)

    def tls_step(self, endpoint):
        self.start = time.monotonic()
        if (endpoint not in ('gpu', 'root') or self.phase != 4 or endpoint in self.tls_attempts or
            len(self.tls_done) >= 2 or endpoint != ('gpu', 'root')[len(self.tls_done)]):
            raise ro.Stop('TLS endpoint/order/permit denied')
        if endpoint == 'root' and self.tls_expected['gpu'] & 15 != 2:
            raise ro.Stop('GPU TLS must precede Root TLS')
        self.snapshot('before_tls')
        old = self.tls_expected[endpoint]
        new = (old & ~15) | 2
        if old == new:
            self.record('tls_already_set', endpoint=endpoint, value=old)
            self.tls_done.append(endpoint)
            return
        path = self.gpu if endpoint == 'gpu' else self.root
        source_fd = self.gf if endpoint == 'gpu' else self.rf
        offset = self.gc+0x30 if endpoint == 'gpu' else 0xC0
        self.record('tls_intent', endpoint=endpoint, offset=offset, old=old, new=new)
        self.state()
        self.check_aer()
        self.gate()
        fd = os.open(path/'config', os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            a, b = os.fstat(fd), os.fstat(source_fd)
            if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                raise ro.Stop('config file identity changed')
            self.state()
            self.check_aer()
            current = (self.cfg(offset, 2) if endpoint == 'gpu'
                       else ro.checked_read(self.rf, offset, 2))
            if current != old:
                raise ro.Stop('TLS changed immediately before write')
            self.gate()
            pre = self.root_guard('tls_pre', record=False)
            self.tls_attempts.append(endpoint)  # Consume even on short/uncertain write.
            count = os.pwrite(fd, new.to_bytes(2, 'little'), offset)
            post = self.root_guard('tls_post', record=False)  # No endpoint read first.
            self.record('tls_return', endpoint=endpoint, offset=offset, count=count,
                        root_pre=pre, root_post=post)
            if count != 2:
                raise ro.Stop('short TLS write; no retry')
        finally:
            os.close(fd)
        self.state()
        self.check_aer()
        readback = (self.cfg(offset, 2) if endpoint == 'gpu'
                    else ro.checked_read(self.rf, offset, 2))
        self.record('tls_readback', endpoint=endpoint, value=readback, expected=new)
        if readback != new:
            raise ro.Stop('full TLS readback mismatch; no retry')
        self.tls_expected[endpoint] = new
        self.snapshot('after_tls')
        self.tls_done.append(endpoint)

    def close(self):
        error = None
        if self.rw and self.mapping is not None:
            mapping, self.mapping = self.mapping, None
            if self.lib.prep_close(mapping):
                error = 'RW munmap failed'
        try:
            super().close()
        finally:
            if error:
                raise ro.Stop(error)


def run(adapter, apply):
    result = dict(event='result', result='FAIL', reason='not_started', cap_ready=False,
                  speed_writes=False, retrain=False, apply=apply)
    try:
        adapter.admit()
        if apply:
            adapter.map_writer()
            for index in range(4):
                adapter.mmio_step(index)
            for endpoint in ('gpu', 'root'):
                adapter.tls_step(endpoint)
            final = adapter.snapshot('final_cap_ready')
            if any(v['ctl2'] & 15 != 2 for v in final.values()):
                raise ro.Stop('both TLS targets must be 2')
            result.update(result='PASS', reason='cap_ready_gen1_x4_tls2', cap_ready=True)
        else:
            result.update(result='PASS', reason='dry_run_factory_admitted_no_write')
    except (OSError, ValueError, KeyError, IndexError, ro.Stop) as exc:
        result['reason'] = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            adapter.close()
        except (OSError, ro.Stop) as exc:
            result.update(result='FAIL', reason=f'cleanup: {exc}', cap_ready=False)
    result.update(mmio_attempts=adapter.mmio_attempts, mmio_store_calls_returned=adapter.mmio_returns,
                  completed_mmio=adapter.completed_mmio, tls_attempts=adapter.tls_attempts,
                  tls_verified=adapter.tls_done,
                  safe_root_rows_suppressed=adapter.safe_root_rows_suppressed,
                  bar_read_attempts=adapter.loads)
    adapter.emit(result)
    return result


def worker(connection, binary, review, apply):
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        fd = os.open(ro.LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lib, manifest = load(binary)
            connection.send(dict(event='native', manifest=manifest))
            result = run(Preparation(lib, review, connection.send), apply)
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
    p.add_argument('--output', required=True, type=Path)
    modes = p.add_mutually_exclusive_group(required=True)
    modes.add_argument('--apply', action='store_true')
    modes.add_argument('--dry-run', action='store_true')
    p.add_argument('--confirm-gpu0-unbound', action='store_true', required=True)
    p.add_argument('--confirm-exclusive-access', action='store_true', required=True)
    p.add_argument('--confirm-preparation-risk', action='store_true')
    args = p.parse_args()
    if args.apply and not args.confirm_preparation_risk:
        p.error('--apply requires --confirm-preparation-risk')
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
        emit(dict(event='start', gpu=ro.GPU, root=ro.ROOT, kernel=platform.release(),
                  apply=args.apply, speed_writes=False, retrain=False,
                  parent_timeout_seconds=25, stage_budget_seconds=5,
                  observation_seconds_per_mmio=2))
        context = multiprocessing.get_context('spawn')
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=worker,
            args=(send, args.native_library.resolve(), review, args.apply))
        process.start()
        send.close()
        try:
            result = ro.supervise(process, receive, emit, 25)
        finally:
            if process.is_alive():
                process.kill()
                process.join(1)
            receive.close()
    print(f'{"PASS" if result == 0 else "FAIL/STOP"}: {output}')
    return result


if __name__ == '__main__':
    raise SystemExit(main())
