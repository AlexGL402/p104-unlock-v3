#!/usr/bin/env python3
"""T5600 P104 staged experiment, NOT a hardware-validated Gen2 unlock.
Default is read-only. See docs/linux-t5600.md before authorizing either stage.
"""
import argparse
import ctypes
import fcntl
import json
import mmap
import os
from pathlib import Path
import platform
import time

GPU = '0000:03:00.0'
ROOT = '0000:00:02.0'
BASE = Path('/sys/bus/pci/devices')
# Only these two BAR0 registers may be written; no speed/width/fuse triggers.
PATCHES = ((0x2241C, 0x81, 0x81), (0x8841C, 0x6000, 0))
EXPECTED_BAR0 = {0x8872C: 0, 0x2241C: 0, 0x8841C: 0xC0B46500,
                 0x8C2C0: 0x068711B3, 0x8C300: 0x040A00C0,
                 0x8C040: 0x20085800, 0x8C1C0: 0x00010032, 0x88610: 0}


class Stop(RuntimeError):
    pass


def checked(value, bits=32):
    if value == (1 << bits) - 1 or (bits == 32 and value >> 16 == 0xBADF):
        raise Stop('unavailable register / device lost; no recovery writes')
    return value


def find_cap(hw, bdf, ident):
    ptr = hw.cfg(bdf, 0x34, 1) & 0xFC
    seen = set()
    while ptr:
        if ptr < 0x40 or ptr > 0xFC or ptr in seen:
            raise Stop('malformed PCI capability list')
        seen.add(ptr)
        if hw.cfg(bdf, ptr, 1) == ident:
            return ptr
        ptr = hw.cfg(bdf, ptr + 1, 1) & 0xFC
    raise Stop(f'capability {ident:#x} missing: {bdf}')


class Linux:
    def __init__(self, writable=False, mmio_writable=False):
        self.writable = writable
        self.mmio_writable = mmio_writable
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.mmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_long)
        self.libc.mmap.restype = ctypes.c_void_p
        self.libc.munmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
        self.mm = None
        self.fd = None

    def cfg(self, bdf, off, size):
        with open(BASE / bdf / 'config', 'rb', buffering=0) as f:
            data = os.pread(f.fileno(), size, off)
        if len(data) != size:
            raise Stop('short PCI config read')
        return checked(int.from_bytes(data, 'little'), size * 8)

    def cfg_write(self, bdf, off, size, value):
        if not self.writable or size != 2:
            raise Stop('PCI write access denied')
        with open(BASE / bdf / 'config', 'r+b', buffering=0) as f:
            if os.pwrite(f.fileno(), value.to_bytes(size, 'little'), off) != size:
                raise Stop('short PCI write')

    def driver(self):
        return (BASE / GPU / 'driver').is_symlink()

    def topology(self):
        if (BASE / GPU).resolve(strict=True).parent.name != ROOT:
            raise Stop('unexpected GPU 0 parent')

    def bar(self, off):
        if off not in EXPECTED_BAR0 or off % 4:
            raise Stop('BAR0 read offset denied')
        if self.mm is None:
            self.fd = os.open(BASE / GPU / 'resource0',
                              (os.O_RDWR if self.mmio_writable else os.O_RDONLY) | os.O_SYNC)
            self.mm = self.libc.mmap(None, 0x90000,
                                     mmap.PROT_READ | (mmap.PROT_WRITE if self.mmio_writable else 0),
                                     mmap.MAP_SHARED, self.fd, 0)
            if self.mm == ctypes.c_void_p(-1).value:
                self.mm = None
                os.close(self.fd)
                self.fd = None
                raise OSError(ctypes.get_errno(), 'BAR0 mmap failed')
        # Native aligned 32-bit load, including for PROT_READ mappings.
        return checked(ctypes.c_uint32.from_address(self.mm + off).value)

    def bar_write(self, off, value):
        if not self.mmio_writable or self.mm is None or off not in {p[0] for p in PATCHES}:
            raise Stop('BAR0 write access denied')
        ctypes.c_uint32.from_address(self.mm + off).value = value

    def close(self):
        if self.mm is not None:
            self.libc.munmap(self.mm, 0x90000)
            self.mm = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Experiment:
    def __init__(self, hw, record, sleep=time.sleep, clock=time.monotonic):
        self.hw, self.record, self.sleep, self.clock = hw, record, sleep, clock
        self.gc = find_cap(hw, GPU, 0x10)
        self.rc = find_cap(hw, ROOT, 0x10)

    def alive(self):
        if self.hw.cfg(GPU, 0, 4) != 0x1B8710DE:
            raise Stop('GPU identity lost; stop all writes')
        if self.hw.cfg(ROOT, 0, 2) != 0x8086:
            raise Stop('root identity lost')

    def links(self):
        self.alive()
        result = {}
        for bdf, cap in ((GPU, self.gc), (ROOT, self.rc)):
            result[bdf] = {name: self.hw.cfg(bdf, cap + off, size)
                           for name, off, size in (('cap', 0xC, 4), ('sta', 0x12, 2),
                                                  ('cap2', 0x2C, 4), ('ctl', 0x10, 2),
                                                  ('ctl2', 0x30, 2))}
        return result

    @staticmethod
    def link_ok(state, speed):
        return all((v['sta'] & 0x3FF) == (0x40 | speed)
                   and not (v['sta'] & (1 << 11)) and not (v['ctl'] & ((1 << 4) | (1 << 5)))
                   for v in state.values())

    def guard(self):
        self.alive()
        self.hw.topology()
        if self.hw.driver():
            raise Stop('GPU 0 must already be unbound; tool never unbinds drivers')
        command = self.hw.cfg(GPU, 4, 2)
        if not command & 2 or command & 4:
            raise Stop('require memory decoding enabled and bus mastering disabled')
        pm = find_cap(self.hw, GPU, 1)
        if self.hw.cfg(GPU, pm + 4, 2) & 3:
            raise Stop('GPU must be in D0')
        if not self.link_ok(self.links(), 1):
            raise Stop('require idle Gen1 x4 on both ends before each stage')

    def registers(self):
        values = {}
        for off in EXPECTED_BAR0:
            self.alive()  # Never touch BAR0 after a detected config loss.
            values[off] = checked(self.hw.bar(off))
        return values

    def preflight(self, stage):
        self.guard()
        expected = dict(EXPECTED_BAR0)
        if stage == 'stage2':
            for off, mask, value in PATCHES:
                expected[off] = (expected[off] & ~mask) | value
        if self.registers() != expected:
            raise Stop('BAR0 baseline mismatch; no writes performed')
        root = self.links()[ROOT]
        if not (root['cap2'] & 4) or (root['cap'] & 15) < 2:
            raise Stop('root does not advertise Gen2')
        if stage == 'stage2':
            self.require_gen2_cap()

    def require_gen2_cap(self):
        state = self.links()
        if any((v['cap2'] & 6) != 6 or (v['cap'] & 15) < 2 for v in state.values()):
            raise Stop('both ends must currently advertise Gen2; no TLS/retrain')

    def stage1(self):
        self.preflight('stage1')
        for off, mask, value in PATCHES:
            self.guard()
            old = checked(self.hw.bar(off))
            if old != EXPECTED_BAR0[off]:
                raise Stop('baseline changed before BAR0 write')
            new = (old & ~mask) | value
            if new != old:
                self.record('before_mmio', offset=off, old=old, new=new)
                self.hw.bar_write(off, new)
                self.alive()
                rb = checked(self.hw.bar(off))  # Same-device read flushes posted write.
                self.record('after_mmio', offset=off, readback=rb)
                if rb != new:
                    raise Stop('BAR0 readback mismatch')
        deadline = self.clock() + 2
        while True:
            state = self.links()
            if not self.link_ok(state, 1):
                raise Stop('link changed during stage1; no further writes')
            if all((v['cap2'] & 6) == 6 and (v['cap'] & 15) >= 2 for v in state.values()):
                self.record('stage1_result', capability_gen2=True, links=state)
                return
            if self.clock() >= deadline:
                raise Stop('minimal switches did not latch Gen2; no fallback/retrain')
            self.sleep(.1)

    def stage2(self):
        self.preflight('stage2')
        for bdf, cap in ((GPU, self.gc), (ROOT, self.rc)):
            self.guard()
            self.require_gen2_cap()
            old = self.hw.cfg(bdf, cap + 0x30, 2)
            new = (old & ~15) | 2
            if old != new:
                self.record('before_tls', bdf=bdf, old=old, new=new)
                self.hw.cfg_write(bdf, cap + 0x30, 2, new)
                self.alive()
                if self.hw.cfg(bdf, cap + 0x30, 2) != new:
                    raise Stop('TLS readback mismatch')
        self.guard()
        self.require_gen2_cap()
        if any((v['ctl2'] & 15) != 2 for v in self.links().values()):
            raise Stop('TLS changed before retrain')
        old = self.hw.cfg(ROOT, self.rc + 0x10, 2)
        self.record('before_retrain', bdf=ROOT, old=old)
        self.hw.cfg_write(ROOT, self.rc + 0x10, 2, old | 0x20)
        # One trigger only. Read-only bounded observation, never retry or roll back.
        self.observe_gen2(10, 2)
        self.record('stage2_result', link_gen2_x4=True, cuda_verified=False)

    def verify_cuda(self, action):
        if not self.hw.driver():
            raise Stop('manually rebind GPU 0 before CUDA verification')
        self.observe_gen2(4, 2)
        action()
        self.observe_gen2(12, 10)
        self.record('verification', gen2_x4=True, cuda_transfer=True,
                    note='short validation only; sustained workload test still required')

    def observe_gen2(self, timeout, stable):
        deadline, since = self.clock() + timeout, None
        while True:
            state = self.links()
            self.record('link_sample', links=state)
            now = self.clock()
            if self.link_ok(state, 2):
                since = now if since is None else since
                if now - since >= stable:
                    return
            else:
                since = None
                if any((v['sta'] & 15) == 0 or ((v['sta'] >> 4) & 63) == 0
                       or v['ctl'] & 0x10 for v in state.values()):
                    raise Stop('Link Down; no retry or recovery writes')
            if now >= deadline:
                raise Stop('Gen2 x4 not stable on both ends')
            self.sleep(.1)


def cuda_check():
    """Explicit, separate verification after manual driver rebind; never called by stages.
    Select by PCI address, not CUDA ordinal. No enumeration or context on GPU 1.
    """
    cuda = ctypes.CDLL('libcuda.so.1')

    def call(name, *args):
        rc = getattr(cuda, name)(*args)
        if rc:
            raise Stop(f'{name} failed: CUDA error {rc}')

    call('cuInit', 0)
    dev = ctypes.c_int()
    call('cuDeviceGetByPCIBusId', ctypes.byref(dev), GPU.encode())
    bus = ctypes.create_string_buffer(32)
    call('cuDeviceGetPCIBusId', bus, len(bus), dev)
    if bus.value.decode().lower() != GPU:
        raise Stop('CUDA selected unexpected PCI address')
    ctx, memory = ctypes.c_void_p(), ctypes.c_uint64()
    try:
        call('cuCtxCreate_v2', ctypes.byref(ctx), 0, dev)
        call('cuMemAlloc_v2', ctypes.byref(memory), ctypes.c_size_t(4096))
        src = (ctypes.c_ubyte * 4096)(*(i % 251 for i in range(4096)))
        dst = (ctypes.c_ubyte * 4096)()
        call('cuMemcpyHtoD_v2', memory, ctypes.byref(src), ctypes.c_size_t(4096))
        call('cuMemcpyDtoH_v2', ctypes.byref(dst), memory, ctypes.c_size_t(4096))
        call('cuCtxSynchronize')
        if bytes(src) != bytes(dst):
            raise Stop('CUDA transfer verification mismatch')
    finally:
        if memory.value:
            call('cuMemFree_v2', memory)
        if ctx.value:
            call('cuCtxDestroy_v2', ctx)


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=('probe', 'stage1', 'stage2', 'verify-cuda'), default='probe')
    p.add_argument('--apply', action='store_true', help='execute the selected stage; otherwise plan only')
    p.add_argument('--confirm-bdf', default='')
    p.add_argument('--authorize-stage2', action='store_true', help='separate permission for TLS and one root retrain')
    p.add_argument('--authorize-cuda', action='store_true', help='allow a CUDA context/transfer on GPU 0')
    p.add_argument('--log-dir', type=Path, default=Path('work/linux-backup'))
    a = p.parse_args(argv)
    if a.apply and a.mode not in ('stage1', 'stage2'):
        p.error('--apply requires stage1 or stage2')
    if (a.apply or a.mode == 'verify-cuda') and a.confirm_bdf != GPU:
        p.error(f'explicit --confirm-bdf {GPU} required')
    if a.apply and a.mode == 'stage2' and not a.authorize_stage2:
        p.error('stage2 requires --authorize-stage2 after review of stage1')
    if a.mode == 'verify-cuda' and not a.authorize_cuda:
        p.error('verify-cuda requires --authorize-cuda')
    return a


def main(argv=None):
    a = arguments(argv)
    if platform.machine() != 'x86_64':
        raise Stop('only x86_64 MMIO access is supported')
    a.log_dir.mkdir(parents=True, exist_ok=True)
    with open('/run/lock/p104-gen2-linux.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = a.log_dir / f'gpu0-{time.time_ns()}.jsonl'
        with path.open('x') as log:
            def record(event, **values):
                row = json.dumps(dict(ts=time.time(), event=event, **values))
                log.write(row + '\n')
                log.flush()
                os.fsync(log.fileno())
                print(row, flush=True)
            hw = Linux(writable=a.apply, mmio_writable=a.apply and a.mode == 'stage1')
            try:
                hw.topology()
                exp = Experiment(hw, record)
                exp.alive()
                record('baseline', gpu=GPU, root=ROOT, links=exp.links(), driver_bound=hw.driver())
                if a.mode == 'verify-cuda':
                    exp.verify_cuda(cuda_check)
                elif a.apply:
                    getattr(exp, a.mode)()
                else:
                    record('plan', mode=a.mode, registers=exp.registers(),
                           stage1_bar0=PATCHES, stage2='TLS=2 both ends; root retrain once',
                           hardware_writes=False)
            except BaseException as exc:
                record('abort', reason=f'{type(exc).__name__}: {exc}', recovery_writes=False)
                raise
            finally:
                hw.close()
        print('Log:', path)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (Stop, OSError) as exc:
        print('STOP:', exc, file=__import__('sys').stderr)
        raise SystemExit(1)
