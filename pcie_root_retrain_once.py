#!/usr/bin/env python3
"""GPU0 one-shot Root RL: exactly 0040 -> 0060 at 00:02.0 config A0, word."""
import argparse
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import platform
import resource
import time

import pcie_bar0_readonly as ro
from design.p104_bar0_ro_build import load  # Read-only native exports only.

EXPECTED = dict(ro.CAP_READY)
EXPECTED[0x8C040] = 0x20045800
ROOT_CONTROL, OLD, NEW = 0xA0, 0x0040, 0x0060
OBSERVATION_NS = 2_000_000_000


class Retrain(ro.Adapter):
    def __init__(self, *args, clock=time.monotonic_ns, **kwargs):
        super().__init__(*args, profile='cap-ready', **kwargs)
        self.clock = clock
        self.attempted = self.write_returned = False
        self.writer_fd = None
        self.frozen_links = None
        self.post_speed = None
        self.root_samples = self.root_changes = 0
        self.training_seen = False

    def record(self, event, **fields):
        if event == 'cleanup':
            fields.update(mmio_writes=0, retrain_attempted=self.attempted)
        super().record(event, **fields)

    def root_guard(self, role, record=True):
        if not self.attempted:
            raw = super().root_guard(role, record)
            if ro.checked_read(self.rf, ROOT_CONTROL, 2) != OLD:
                raise ro.Stop('Root Link Control must be exactly 0040 before retrain')
            return raw
        # Endpoint admission after the observation window always requires idle.
        sample = self.poll_root(role, record)
        if sample['training'] or sample['control'] != OLD or sample['pending']:
            raise ro.Stop('Root not idle; no endpoint access')
        return sample['raw']

    def root_errors(self):
        if self.ra is None:
            raise ro.Stop('Root AER capability required')
        values = [ro.checked_read(self.rf, self.ra+o, 4) for o in (4, 0x10, 0x30)]
        if any(values):
            self.record('aer_error', endpoint='root', status=values)
            raise ro.Stop('Root AER error; no endpoint access/recovery')
        return values

    def poll_root(self, role='observe', record=False):
        """Root-only: never calls cfg(), gate(), state() or a GPU path."""
        begin = self.clock()
        raw = ro.checked_read(self.rf, 0xA2, 2)
        training, dllla = bool(raw & 0x800), bool(raw & 0x2000)
        if not dllla:
            self.record('root_unsafe', role=role, raw=raw)
            raise ro.Stop('Root DLL inactive/Link Down; no endpoint access')
        if not training and (raw & 15 not in (1, 2) or (raw >> 4) & 63 != 4):
            self.record('root_unsafe', role=role, raw=raw)
            raise ro.Stop('Root idle speed/width deviation')
        if time.monotonic()-self.start > 5:
            raise ro.Stop('experiment guard deadline')
        if (ro.checked_read(self.rf, 0, 4) != self.root_id or
            (self.root/'power_state').read_text().strip() != 'D0' or
            not ro.checked_read(self.rf, 4, 2) & 2 or
            ro.checked_read(self.rf, 8, 4) >> 8 != 0x060400 or
            ro.checked_read(self.rf, 0x90, 1) != 0x10 or
            (ro.checked_read(self.rf, 0x92, 2) >> 4) & 15 != 4 or
            not ro.checked_read(self.rf, 0x9C, 4) & (1 << 20)):
            raise ro.Stop('Root identity/D0/COMMAND/capability deviation')
        control = ro.checked_read(self.rf, ROOT_CONTROL, 2)
        if control not in (OLD, NEW):
            raise ro.Stop('Root Link Control changed outside permitted RL bit')
        errors = self.root_errors()
        status = ro.checked_read(self.rf, 0x9A, 2)
        if status & 15:
            self.record('root_device_status', value=status)
            raise ro.Stop('Root Device Status error')
        sample = dict(raw=raw, control=control, training=training, dllla=dllla,
                      speed=raw & 15, width=(raw >> 4) & 63, lbms=bool(raw & 0x4000),
                      pending=bool(status & 0x20), aer=errors,
                      start_ns=begin, end_ns=self.clock())
        if record:
            self.record('root_check', role=role, **sample)
        return sample

    def state(self):
        # cfg()/gate()/resource() retain the proven per-access project guards.
        if self.cfg(0, 4) != 0x1B8710DE:
            raise ro.Stop('GPU identity changed')
        command, pmcsr = self.cfg(4, 2), self.cfg(self.pm+4, 2)
        if command != 0x0002:
            raise ro.Stop('GPU COMMAND must be exactly 0002')
        if pmcsr & 3:
            raise ro.Stop('GPU must remain D0')
        sta = self.cfg(self.gc+18, 2)
        allowed = (1, 2) if self.attempted else (1,)
        if sta & 0x800 or (sta >> 4) & 63 != 4 or sta & 15 not in allowed:
            raise ro.Stop('GPU speed/width/Training deviation; no further GPU access')
        if self.cfg(self.gc+16, 2) & 0x30:
            raise ro.Stop('GPU Link Control deviation')
        self.resource()
        self.root_guard('device_status')
        if ro.checked_read(self.rf, 0x9A, 2) & 0x2F:
            raise ro.Stop('Root Device Status error/pending')
        if self.cfg(self.gc+10, 2) & 0x2F:
            raise ro.Stop('GPU Device Status error/pending')
        return dict(command=command, pmcsr=pmcsr, lnksta=sta)

    def aer(self):
        self.root_guard('aer_root')
        root = self.root_errors()
        if self.ga is None:
            raise ro.Stop('GPU AER capability required')
        gpu = []
        for off in (4, 0x10):
            value = self.cfg(self.ga+off, 4)
            gpu.append(value)
            if value:
                self.record('aer_error', endpoint='gpu', status=gpu)
                raise ro.Stop('GPU AER error')
        return dict(root=root, gpu=gpu)

    def check_aer(self):
        self.record('aer', status=self.aer())

    def links(self):
        raw = self.root_guard('snapshot_root')
        root = {k: ro.checked_read(self.rf, 0x90+o, n)
                for k, o, n in [('cap', 12, 4), ('cap2', 44, 4), ('ctl2', 48, 2), ('ctl', 16, 2)]}
        root['sta'] = raw
        gpu = {'sta': self.cfg(self.gc+18, 2)}
        if gpu['sta'] & 0x800 or (gpu['sta'] >> 4) & 63 != 4 or gpu['sta'] & 15 not in (1, 2):
            raise ro.Stop('GPU speed/width/Training deviation')
        gpu.update({k: self.cfg(self.gc+o, n)
                    for k, o, n in [('cap', 12, 4), ('cap2', 44, 4), ('ctl2', 48, 2), ('ctl', 16, 2)]})
        return dict(root=root, gpu=gpu)

    def verify_links(self):
        self.state()
        self.check_aer()
        links = self.links()
        self.record('links', links=links)
        gpu, root = links['gpu'], links['root']
        if gpu['cap'] != 0x00453C42 or gpu['cap2'] != 6:
            raise ro.Stop('GPU CAP/CAP2 must be 00453C42/6')
        if root['cap'] & 15 != 3 or (root['cap'] >> 4) & 63 != 16 or root['cap2'] & 14 != 14:
            raise ro.Stop('Root Gen3 x16 capability mismatch')
        if root['ctl'] != OLD or gpu['ctl'] & 0x30:
            raise ro.Stop('Link Control not idle/baseline')
        if any(v['ctl2'] & 15 != 2 or v['ctl2'] & 0x810 for v in links.values()):
            raise ro.Stop('both TLS must already be 2, compliance off')
        if not self.attempted and any(v['sta'] & 0x3FF != 0x41 for v in links.values()):
            raise ro.Stop('initial link must be Gen1 x4 on both ends')
        if len({v['sta'] & 15 for v in links.values()}) != 1:
            raise ro.Stop('Root/GPU speed disagreement')
        if self.frozen_links is not None:
            for endpoint in ('root', 'gpu'):
                if any(links[endpoint][k] != self.frozen_links[endpoint][k]
                       for k in ('cap', 'cap2', 'ctl2', 'ctl')):
                    raise ro.Stop('capability/TLS/control drift')
        return links

    def read_word(self, offset):
        if type(offset) is not int or offset not in EXPECTED or not self.mapping:
            raise ro.Stop('BAR read denied')
        self.state()
        self.gate()
        pre = self.root_guard('mmio_pre', record=False)
        begin = self.clock()
        self.loads += 1
        value = self.lib.ro_read(self.mapping, offset)
        end = self.clock()
        post = self.root_guard('mmio_post', record=False)
        self.record('dword', offset=offset, value=value, start_ns=begin, end_ns=end,
                    root_pre=pre, root_post=post, atomic_snapshot=False)
        if value == 0xFFFFFFFF or value >> 16 == 0xBADF or value > 0xFFFFFFFF:
            raise ro.Stop('MMIO unavailable/sentinel')
        return value

    def prepared_baseline(self):
        self.verify_links()
        values = {o: self.read_word(o) for o in EXPECTED}
        self.record('bar_baseline', expected=EXPECTED, actual=values)
        if values != EXPECTED:
            raise ro.Stop('exact post-SPEED prepared BAR0 baseline required')
        self.verify_links()

    def admit(self):
        self.open()  # Same proven read-only mapping/lifecycle; no write backend.
        self.prepared_baseline()
        self.frozen_links = self.verify_links()
        self.record('admitted', gpu=ro.GPU, root=ro.ROOT,
                    only_write=dict(offset=ROOT_CONTROL, size=2, old=OLD, new=NEW))

    def observe(self, deadline, initial):
        last, previous = None, None
        pending = initial
        while (pending is not None or self.clock() < deadline) and self.root_samples < 100000:
            sample = pending if pending is not None else self.poll_root()
            pending = None
            self.root_samples += 1
            self.training_seen |= sample['training']
            signature = (sample['raw'], sample['control'], sample['pending'])
            if signature != previous:
                self.root_changes += 1
                self.record('root_observation', sample=self.root_samples, **sample)
                if self.root_changes > 128:
                    raise ro.Stop('Root transition record limit')
                previous = signature
            last = sample
        if last is None:
            raise ro.Stop('no Root observation inside two-second window')
        if self.root_samples >= 100000 and self.clock() < deadline:
            raise ro.Stop('Root observation sample limit')
        self.record('observation_end', samples=self.root_samples, training_seen=self.training_seen,
                    deadline_ns=deadline, last=last)
        if last['training'] or last['control'] != OLD or last['pending']:
            raise ro.Stop('Root training/RL/pending timeout; no endpoint access')
        return last

    def trigger(self):
        if self.attempted:
            raise ro.Stop('retrain permit already consumed')
        self.prepared_baseline()
        self.gate()
        self.writer_fd = os.open(self.root/'config', os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        a, b = os.fstat(self.writer_fd), os.fstat(self.rf)
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise ro.Stop('Root config file identity changed')
        self.record('retrain_intent', bdf=ro.ROOT, offset=ROOT_CONTROL, size=2, old=OLD, new=NEW)
        self.state()
        self.verify_links()
        self.gate()
        self.root_guard('trigger_pre', record=False)
        if ro.checked_read(self.rf, ROOT_CONTROL, 2) != OLD:
            raise ro.Stop('Root Link Control changed before write')
        self.attempted = True  # Consume before any possibly short/faulting write.
        begin = self.clock()
        count = os.pwrite(self.writer_fd, b'\x60\x00', ROOT_CONTROL)  # ONLY write call.
        end = self.clock()
        self.write_returned = count == 2
        # First hardware read after pwrite is Root LNKSTA; no GPU calls in observe.
        deadline = end+OBSERVATION_NS
        initial = self.poll_root()  # Before JSONL/IPC; Root LNKSTA is the first read.
        self.record('retrain_return', count=count, start_ns=begin, end_ns=end,
                    completion_proven=False)
        if count != 2:
            raise ro.Stop('short Root word write; no retry/clear')
        return self.observe(deadline, initial)

    def close(self):
        error = None
        fd, self.writer_fd = self.writer_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError as exc:
                error = exc
        try:
            super().close()
        finally:
            if error:
                raise error


def run(adapter, apply):
    result = dict(event='result', result='FAIL', reason='not_started', apply=apply,
                  gen2_observed=False, nvidia_cuda_verified=False, mmio_writes=0)
    try:
        adapter.admit()
        if apply:
            adapter.trigger()
            links = adapter.verify_links()  # Only after idle Root admission.
            result['gen2_observed'] = all(v['sta'] & 0x3FF == 0x42 for v in links.values())
            adapter.post_speed = adapter.read_word(0x8C040)
            adapter.check_aer()
            adapter.record('final_state', links=links, speed=adapter.post_speed)
            if adapter.post_speed != EXPECTED[0x8C040]:
                raise ro.Stop('post-retrain internal SPEED drift; no rewrite')
            if not result['gen2_observed']:
                raise ro.Stop('Gen2 x4 not obtained; no second trigger')
            result.update(result='PASS', reason='gen2_x4_both_ends')
        else:
            result.update(result='PASS', reason='dry_run_ready_no_write')
    except (OSError, ValueError, KeyError, IndexError, ro.Stop) as exc:
        result['reason'] = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            adapter.close()
        except (OSError, ro.Stop) as exc:
            result.update(result='FAIL', reason=f'cleanup: {exc}')
    result.update(retrain_attempted=adapter.attempted, write_call_returned=adapter.write_returned,
                  post_speed=adapter.post_speed, root_samples=adapter.root_samples,
                  training_seen=adapter.training_seen)
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
            result = run(Retrain(lib, review, connection.send), apply)
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
    p.add_argument('--native-library', type=Path, required=True)
    p.add_argument('--mapping-review', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--dry-run', action='store_true')
    p.add_argument('--confirm-gpu0-unbound', action='store_true', required=True)
    p.add_argument('--confirm-exclusive-access', action='store_true', required=True)
    p.add_argument('--confirm-root-retrain-risk', action='store_true')
    args = p.parse_args()
    if args.apply and not args.confirm_root_retrain_risk:
        p.error('--apply requires --confirm-root-retrain-risk')
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
        emit(dict(event='start', gpu=ro.GPU, root=ro.ROOT, kernel=platform.release(), apply=args.apply,
                  only_write=dict(offset=ROOT_CONTROL, size=2, old=OLD, new=NEW),
                  observation_ns=OBSERVATION_NS, mmio_writes=False, tls_writes=False))
        context = multiprocessing.get_context('spawn')
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=worker,
            args=(send, args.native_library.resolve(), review, args.apply))
        process.start()
        send.close()
        try:
            result = ro.supervise(process, receive, emit, 10)
        except KeyboardInterrupt:
            emit(dict(event='result', result='FAIL', reason='interrupted',
                      note='write completion may be unknown; no retry/recovery'))
            result = 1
        finally:
            if process.is_alive():
                process.kill()
                process.join(1)
            receive.close()
    label = ('GEN2 PASS' if args.apply else 'DRY-RUN READY') if result == 0 else 'FAIL/STOP'
    print(f'{label}: {output}')
    return result


if __name__ == '__main__':
    raise SystemExit(main())
