#!/usr/bin/env python3
"""One-shot GPU0 SPEED-only experiment. Never prepares hardware or writes config."""
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
from design.p104_speed_build import load

OLD, NEW, OFFSET, MASK = 0x20085800, 0x20045800, 0x8C040, 0x000C0000


class SpeedAdapter(ro.Adapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, profile='cap-ready', **kwargs)
        self.write_fd = self.write_mapping = None
        self.attempted = self.returned = False
        self.readback = None

    def record(self, event, **fields):
        if event == 'cleanup':
            fields.pop('device_writes', None)
            fields.update(store_attempted=self.attempted, store_call_returned=self.returned)
        super().record(event, **fields)

    def root_guard(self, role, record=True):
        if not self.attempted:
            return super().root_guard(role, record)
        # Post-store Gen2 is an allowed result, never a reason to bypass link loss.
        raw = ro.checked_read(self.rf, 0xA2, 2)
        unsafe = bool(raw & 0x800 or not raw & 0x2000 or
                      (raw >> 4) & 63 != 4 or raw & 15 not in (1, 2))
        if record or unsafe:
            self.record('root_check', role=role, raw=raw, speed=raw & 15,
                        width=(raw >> 4) & 63, training=bool(raw & 0x800),
                        dllla=bool(raw & 0x2000), lbms=bool(raw & 0x4000))
        if unsafe:
            raise ro.Stop('Root Training/DLL/link loss: no GPU readback or recovery')
        if time.monotonic()-self.start > 5:
            raise ro.Stop('experiment admission/read deadline')
        if ro.checked_read(self.rf, 0, 4) != self.root_id:
            raise ro.Stop('Root identity changed')
        if ((self.root/'power_state').read_text().strip() != 'D0' or
            not ro.checked_read(self.rf, 4, 2) & 2 or
            ro.checked_read(self.rf, 8, 4) >> 8 != 0x060400 or
            ro.checked_read(self.rf, 0x90, 1) != 0x10 or
            (ro.checked_read(self.rf, 0x92, 2) >> 4) & 15 != 4 or
            ro.checked_read(self.rf, 0xA0, 2) & 0x30 or
            not ro.checked_read(self.rf, 0x9C, 4) & (1 << 20)):
            raise ro.Stop('Root post-store guard deviation')
        return raw

    def state(self):
        if not self.attempted:
            return super().state()
        if self.cfg(0, 4) != 0x1B8710DE:
            raise ro.Stop('GPU identity changed')
        command, pmcsr = self.cfg(4, 2), self.cfg(self.pm+4, 2)
        sta, ctl = self.cfg(self.gc+0x12, 2), self.cfg(self.gc+0x10, 2)
        if (not command & 2 or command & 4 or pmcsr & 3 or sta & 0x800 or
            (sta >> 4) & 63 != 4 or sta & 15 not in (1, 2) or ctl & 0x30):
            raise ro.Stop('GPU post-store D0/COMMAND/link deviation')
        self.resource()
        return dict(command=command, pmcsr=pmcsr, lnksta=sta)

    def root_aer_zero(self):
        if not self.ra:
            raise ro.Stop('Root AER status unavailable')
        values = [ro.checked_read(self.rf, self.ra+o, 4) for o in (4, 0x10, 0x30)]
        if any(values):
            self.record('aer', root=values)
            raise ro.Stop('nonzero Root AER; no further GPU access')
        return values

    def check_aer(self):
        self.root_guard('aer_root')
        root = self.root_aer_zero()
        if not self.ga:
            raise ro.Stop('GPU AER status unavailable')
        gpu = []
        for offset in (4, 0x10):
            value = self.cfg(self.ga+offset, 4)
            gpu.append(value)
            if value:
                self.record('aer', root=root, gpu=gpu)
                raise ro.Stop('nonzero GPU AER; no further endpoint access')
        self.record('aer', status=dict(root=root, gpu=gpu))

    def prepared_links(self):
        links = self.links()
        gpu, root = links['gpu'], links['root']
        if gpu['cap'] != 0x00453C42 or gpu['cap2'] != 6:
            raise ro.Stop('require exact prepared GPU LNKCAP=00453C42/LNKCAP2=6')
        if root['cap'] & 15 != 3 or (root['cap'] >> 4) & 63 != 16 or root['cap2'] & 14 != 14:
            raise ro.Stop('T5600 Root Gen3 x16 capability mismatch')
        if any(v['ctl2'] & 15 != 2 or v['ctl2'] & 0x810 for v in links.values()):
            raise ro.Stop('both TLS must ALREADY be 2, without compliance controls')
        if hasattr(self, 'prepared_snapshot'):
            for end in ('root', 'gpu'):
                if any(links[end][k] != self.prepared_snapshot[end][k]
                       for k in ('cap', 'cap2', 'ctl2')):
                    raise ro.Stop('prepared capability/TLS changed')
        allowed = (1, 2) if self.attempted else (1,)
        if any((v['sta'] >> 4) & 63 != 4 or v['sta'] & 0x800 or
               v['sta'] & 15 not in allowed for v in links.values()) or not root['sta'] & 0x2000:
            raise ro.Stop('prepared link snapshot not safe x4')
        speeds = {v['sta'] & 15 for v in links.values()}
        if len(speeds) != 1:
            raise ro.Stop('Root/GPU speed disagreement')
        self.record('prepared_links', links=links)
        return links

    def admit(self):
        self.open()  # Existing read-only lifecycle/guards, unchanged.
        for offset in ro.CAP_READY:
            self.read(offset)  # Exact eight-DWORD prepared baseline, no fallback.
        self.prepared_snapshot = self.prepared_links()
        if not self.ra or not self.ga or any(v for values in self.initial_aer.values() for v in values):
            raise ro.Stop('require available and zero Root/GPU AER before SPEED')
        self.check_aer()
        self.record('admitted', old=OLD, new=NEW, mask=MASK, change_speed=False)

    def map_writer(self):
        # No overlapping RO/RW alias; release RO mapping, keep config guards/fds.
        old_mapping, self.mapping = self.mapping, None
        if self.lib.ro_close(old_mapping):
            raise ro.Stop('pre-write RO munmap failed')
        fd, self.bf = self.bf, None
        os.close(fd)
        self.state()
        self.prepared_links()
        self.gate()
        self.write_fd = os.open(self.gpu/'resource0', os.O_RDWR | os.O_SYNC |
                                os.O_CLOEXEC | os.O_NOFOLLOW)
        self.state()
        self.gate()
        self.write_mapping = self.lib.speed_map(self.write_fd)
        if not self.write_mapping:
            raise OSError(ctypes.get_errno(), 'RW resource0 map failed; no store')
        self.state()
        self.prepared_links()
        self.check_aer()
        self.record('writer_mapped', flags='MAP_SHARED', protection='PROT_READ|PROT_WRITE',
                    offset=0, bytes=self.resources['map_length'], initialization_stores=0)

    def execute(self):
        if self.attempted or not self.write_mapping:
            raise ro.Stop('one-shot already consumed or not mapped')
        self.state()
        self.prepared_links()
        self.check_aer()
        self.record('store_intent', offset=OFFSET, old=OLD, new=NEW,
                    note='intent is not proof of a completed store')
        self.state()
        self.gate()
        pre = self.root_guard('speed_pre', record=False)
        # Terminal flag precedes native call, including fault or old-value refusal.
        self.attempted = True
        begin = time.monotonic_ns()
        rc = self.lib.speed_once(self.write_mapping)
        end = time.monotonic_ns()
        self.returned = rc == 0
        post = self.root_guard('speed_post', record=False)  # FIRST hardware read.
        self.record('store_return', code=rc, start_ns=begin, end_ns=end,
                    root_pre=pre, root_post=post, pci_completion_proven=False)
        if rc != 0:
            raise ro.Stop(f'native one-shot refused ({rc}); no retry')
        self.check_aer()
        # Separate readback, only after fresh state/root guards. No sleep/flush/ACK claim.
        self.state()
        self.gate()
        pre = self.root_guard('readback_pre', record=False)
        begin = time.monotonic_ns()
        value = self.lib.speed_readback(self.write_mapping)
        end = time.monotonic_ns()
        post = self.root_guard('readback_post', record=False)
        self.readback = value
        self.record('speed_readback', value=value, expected=NEW, start_ns=begin,
                    end_ns=end, root_pre=pre, root_post=post, atomic_snapshot=False)
        if value != NEW:
            raise ro.Stop('full SPEED readback mismatch; no retry/rollback')
        self.check_aer()
        self.observe_root(post)
        self.state()
        self.check_aer()
        final = self.prepared_links()
        self.record('final_links', links=final)
        return all(v['sta'] & 15 == 2 for v in final.values())

    def observe_root(self, initial):
        # Only Root config polling here; no repeated MMIO/readback/command.
        origin_ns = time.monotonic_ns()
        deadline = time.monotonic()+2
        count, last = 0, initial
        while time.monotonic() < deadline and count < 100000:
            raw = self.root_guard('observe', record=False)
            self.root_aer_zero()
            count += 1
            if raw != last:
                self.record('root_change', previous=last, raw=raw)
                last = raw
        self.record('observation', polls=count, elapsed_ns=time.monotonic_ns()-origin_ns,
                    elapsed_bound_seconds=2,
                    note='finite software samples, no hardware ACK or complete LTSSM history')

    def close(self):
        errors = []
        handle, self.write_mapping = self.write_mapping, None
        if handle is not None and self.lib.speed_close(handle):
            errors.append('RW munmap failed')
        fd, self.write_fd = self.write_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError as exc:
                errors.append(str(exc))
        try:
            super().close()
        except (OSError, ro.Stop) as exc:
            errors.append(str(exc))
        if errors:
            raise ro.Stop('; '.join(errors))


def run(adapter, apply):
    result = dict(event='result', result='FAIL', reason='not_started', apply=apply,
                  gen2_observed=False, gen2_unlocked_proven=False)
    try:
        adapter.admit()
        if apply:
            adapter.map_writer()
            result['gen2_observed'] = adapter.execute()
            result.update(result='PASS', reason='one_store_exact_readback_link_x4')
        else:
            result.update(result='PASS', reason='dry_run_ready_no_write')
    except (OSError, ValueError, KeyError, IndexError, ro.Stop) as exc:
        result['reason'] = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            adapter.close()
        except (OSError, ro.Stop) as exc:
            result.update(result='FAIL', reason=f'cleanup: {exc}')
    result.update(store_attempted=adapter.attempted, store_call_returned=adapter.returned,
                  readback=adapter.readback)
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
            result = run(SpeedAdapter(lib, review, connection.send), apply)
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
    p.add_argument('--confirm-speed-only-risk', action='store_true')
    args = p.parse_args()
    if args.apply and not args.confirm_speed_only_risk:
        p.error('--apply requires --confirm-speed-only-risk')
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
            log.flush()
        emit(dict(event='start', gpu=ro.GPU, root=ro.ROOT, kernel=platform.release(),
                  apply=args.apply, only_write=dict(offset=OFFSET, old=OLD, new=NEW),
                  change_speed=False, config_writes=False))
        context = multiprocessing.get_context('spawn')
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=worker,
            args=(send, args.native_library.resolve(), review, args.apply))
        process.start()
        send.close()
        try:
            result = ro.supervise(process, receive, emit, 10)
        finally:
            if process.is_alive():
                process.kill()
                process.join(1)
            receive.close()
    print(f'{"PASS" if result == 0 else "FAIL/STOP"}: {output}')
    return result


if __name__ == '__main__':
    raise SystemExit(main())
