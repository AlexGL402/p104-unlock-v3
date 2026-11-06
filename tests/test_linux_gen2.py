import contextlib
import io
import unittest
from unittest.mock import patch
import p104_gen2_linux as p


class Fake:
    def __init__(self):
        self.regs = dict(p.EXPECTED_BAR0)
        self.data = {}
        self.writes = []
        self.bound = False
        self.lost = False
        self.latch = True
        self.train_success = True
        self.bad_readback = False
        self.lose_at = None
        self.t = 0
        for bdf in (p.GPU, p.ROOT):
            self.data.update({(bdf, 0x34): 0x40, (bdf, 0x40): 0x10,
                              (bdf, 0x41): 0x80, (bdf, 0x80): 1,
                              (bdf, 0x81): 0, (bdf, 0x84): 0,
                              (bdf, 0x4C): 0x42 if bdf == p.ROOT else 0x41,
                              (bdf, 0x52): 0x1041, (bdf, 0x6C): 6 if bdf == p.ROOT else 2,
                              (bdf, 0x70): 0xA1, (bdf, 0x50): 0x100,
                              (bdf, 4): 2, (bdf, 0): 0x1B8710DE if bdf == p.GPU else 0x8086})

    def cfg(self, bdf, off, size):
        assert bdf in (p.GPU, p.ROOT), 'GPU 1 touched'
        if self.lost and bdf == p.GPU:
            raise p.Stop('device lost')
        return self.data[(bdf, off)] & ((1 << (8 * size)) - 1)

    def cfg_write(self, bdf, off, size, value):
        assert size == 2
        self.writes.append(('pci', bdf, off, value))
        self.data[(bdf, off)] = value
        if off == 0x50 and bdf == p.ROOT:
            self.data[(bdf, off)] = value & ~0x20
            if self.train_success:
                for dev in (p.GPU, p.ROOT):
                    self.data[(dev, 0x52)] = 0x1042
        if self.lose_at == off:
            self.lost = True

    def topology(self):
        pass

    def driver(self):
        return self.bound

    def bar(self, off):
        assert not self.lost, 'MMIO accessed after detected loss'
        return self.regs[off]

    def bar_write(self, off, value):
        self.writes.append(('bar', off, value))
        self.regs[off] = value ^ (1 if self.bad_readback else 0)
        if self.latch and off == 0x8841C:
            self.data[(p.GPU, 0x6C)] = 6
            self.data[(p.GPU, 0x4C)] = 0x42
        if self.lose_at == off:
            self.lost = True

    def sleep(self, seconds):
        self.t += seconds


class Tests(unittest.TestCase):
    def setUp(self):
        self.hw = Fake()
        self.events = []
        self.exp = p.Experiment(self.hw, lambda event, **kw: self.events.append((event, kw)),
                                self.hw.sleep, lambda: self.hw.t)

    def staged(self):
        self.exp.stage1()
        self.hw.writes.clear()

    def test_stage1_only_two_switches(self):
        self.exp.stage1()
        self.assertEqual(self.hw.writes, [('bar', 0x2241C, 0x81),
                                        ('bar', 0x8841C, 0xC0B40500)])
        self.assertEqual(self.hw.regs[0x8C040], 0x20085800)
        self.assertEqual(self.hw.regs[0x8872C], 0)

    def test_no_latch_no_fallback(self):
        self.hw.latch = False
        with self.assertRaisesRegex(p.Stop, 'did not latch'):
            self.exp.stage1()
        self.assertEqual(len(self.hw.writes), 2)

    def test_loss_after_each_switch_stops(self):
        for i, off in enumerate((0x2241C, 0x8841C), 1):
            self.setUp()
            self.hw.lose_at = off
            with self.assertRaises(p.Stop):
                self.exp.stage1()
            self.assertEqual(len(self.hw.writes), i)

    def test_readback_failure_stops(self):
        self.hw.bad_readback = True
        with self.assertRaisesRegex(p.Stop, 'readback'):
            self.exp.stage1()
        self.assertEqual(len(self.hw.writes), 1)

    def test_sentinels_rejected_before_writes(self):
        for value in (0xFFFFFFFF, 0xBADF5040):
            self.hw.regs[0x2241C] = value
            with self.assertRaises(p.Stop):
                self.exp.stage1()
            self.assertEqual(self.hw.writes, [])

    def test_bad_baseline_no_writes(self):
        self.hw.regs[0x8C040] = 0x20045800
        with self.assertRaisesRegex(p.Stop, 'baseline'):
            self.exp.stage1()
        self.assertEqual(self.hw.writes, [])

    def test_bound_driver_no_writes(self):
        self.hw.bound = True
        with self.assertRaisesRegex(p.Stop, 'unbound'):
            self.exp.stage1()
        self.assertFalse(self.hw.writes)

    def test_d3_busmaster_or_decode_off_refused(self):
        for off, value in ((0x84, 3), (4, 6), (4, 0)):
            self.setUp()
            self.hw.data[(p.GPU, off)] = value
            with self.assertRaises(p.Stop):
                self.exp.stage1()
            self.assertFalse(self.hw.writes)

    def test_stage2_exact_order_16bit_preserves_controls(self):
        self.staged()
        self.exp.stage2()
        self.assertEqual(self.hw.writes, [('pci', p.GPU, 0x70, 0xA2),
                                         ('pci', p.ROOT, 0x70, 0xA2),
                                         ('pci', p.ROOT, 0x50, 0x120)])
        self.assertGreaterEqual(self.hw.t, 2)

    def test_stage2_requires_current_capability(self):
        self.staged()
        self.hw.data[(p.GPU, 0x6C)] = 2
        with self.assertRaisesRegex(p.Stop, 'advertise'):
            self.exp.stage2()
        self.assertFalse(self.hw.writes)

    def test_no_stage2_without_stage1(self):
        with self.assertRaises(p.Stop):
            self.exp.stage2()
        self.assertFalse(self.hw.writes)

    def test_no_retry_on_timeout(self):
        self.staged()
        self.hw.train_success = False
        with self.assertRaisesRegex(p.Stop, 'not stable'):
            self.exp.stage2()
        self.assertEqual(len(self.hw.writes), 3)

    def test_no_retry_or_mmio_after_retrain_loss(self):
        self.staged()
        self.hw.lose_at = 0x50
        with self.assertRaises(p.Stop):
            self.exp.stage2()
        self.assertEqual(len(self.hw.writes), 3)

    def test_asymmetric_or_training_link_cannot_pass(self):
        for sta in (0x1041, 0x1082, 0x1842):
            self.setUp()
            self.hw.data[(p.GPU, 0x52)] = 0x1042
            self.hw.data[(p.ROOT, 0x52)] = sta
            with self.assertRaises(p.Stop):
                self.exp.observe_gen2(.3, .2)
            self.assertFalse(self.hw.writes)

    def test_link_down_stops_observation(self):
        self.hw.data[(p.ROOT, 0x52)] = 0
        with self.assertRaisesRegex(p.Stop, 'Link Down'):
            self.exp.observe_gen2(10, 2)
        self.assertEqual(self.hw.t, 0)

    def test_capability_cycle_rejected(self):
        self.hw.data[(p.GPU, 0x40)] = 5
        self.hw.data[(p.GPU, 0x41)] = 0x40
        with self.assertRaisesRegex(p.Stop, 'malformed'):
            p.find_cap(self.hw, p.GPU, 0x10)

    def test_cli_permissions_before_hardware_access(self):
        cases = [['--mode', 'stage2', '--apply', '--confirm-bdf', p.GPU],
                 ['--mode', 'stage1', '--apply'],
                 ['--mode', 'verify-cuda', '--confirm-bdf', p.GPU],
                 ['--apply'], ['--mode', 'stage1', '--apply', '--confirm-bdf', '0000:04:00.0']]
        for args in cases:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                p.arguments(args)
        self.assertEqual(p.arguments([]).mode, 'probe')
        self.assertFalse(p.arguments(['--mode', 'stage2']).apply)

    def test_no_mmio_write_backend_access(self):
        for off in (0x8C040, 0x8872C, 0x8C1C0):
            with self.assertRaises(p.Stop):
                p.Linux(True).bar_write(off, 0)
        with self.assertRaises(p.Stop):
            p.Linux().cfg_write(p.GPU, 0x70, 2, 2)

    def test_verify_cuda_rechecks_after_context(self):
        self.hw.bound = True
        for dev in (p.GPU, p.ROOT):
            self.hw.data[(dev, 0x52)] = 0x1042
        calls = []
        def cuda_action():
            calls.append('cuda')
            self.hw.data[(p.GPU, 0x52)] = 0x1041
        with self.assertRaisesRegex(p.Stop, 'not stable'):
            self.exp.verify_cuda(cuda_action)
        self.assertEqual(calls, ['cuda'])
        self.assertFalse(any(event == 'verification' for event, _ in self.events))
        self.assertFalse(self.hw.writes)

    def test_verify_cuda_success(self):
        self.hw.bound = True
        for dev in (p.GPU, p.ROOT):
            self.hw.data[(dev, 0x52)] = 0x1042
        self.exp.verify_cuda(lambda: None)
        self.assertGreaterEqual(self.hw.t, 12)
        self.assertEqual(self.events[-1][0], 'verification')
        self.assertFalse(self.hw.writes)

    def test_verify_requires_driver_and_gen2_before_cuda(self):
        calls = []
        with self.assertRaises(p.Stop):
            self.exp.verify_cuda(lambda: calls.append(1))
        self.hw.bound = True
        with self.assertRaises(p.Stop):
            self.exp.verify_cuda(lambda: calls.append(1))
        self.assertFalse(calls)

    def test_real_backend_uses_readonly_mapping_for_stage2(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            resource = Path(temp) / p.GPU / 'resource0'
            resource.parent.mkdir()
            resource.write_bytes(bytes(0x90000))
            with patch.object(p, 'BASE', Path(temp)):
                writer = p.Linux(writable=True, mmio_writable=True)
                try:
                    self.assertEqual(writer.bar(0x2241C), 0)
                    writer.bar_write(0x2241C, 0x81)
                    self.assertEqual(writer.bar(0x2241C), 0x81)
                finally:
                    writer.close()
                readonly = p.Linux(writable=True)
                try:
                    self.assertEqual(readonly.bar(0x2241C), 0x81)
                    with self.assertRaises(p.Stop):
                        readonly.bar_write(0x2241C, 0)
                finally:
                    readonly.close()

    def test_stage2_stops_after_first_tls_loss(self):
        self.staged()
        self.hw.lose_at = 0x70
        with self.assertRaises(p.Stop):
            self.exp.stage2()
        self.assertEqual(len(self.hw.writes), 1)

    def test_stage2_reevaluates_capability_between_writes(self):
        self.staged()
        original = self.hw.cfg_write
        def change_cap(*args):
            original(*args)
            self.hw.data[(p.GPU, 0x6C)] = 2
        self.hw.cfg_write = change_cap
        with self.assertRaises(p.Stop):
            self.exp.stage2()
        self.assertEqual(len(self.hw.writes), 1)

    def test_cuda_selects_bdf_and_transfers(self):
        class Cuda:
            def __init__(self):
                self.calls = []
                self.payload = b''
            def __getattr__(self, name):
                def invoke(*args):
                    self.calls.append(name)
                    if name == 'cuDeviceGetByPCIBusId':
                        self.bdf = args[1]
                        args[0]._obj.value = 7
                    elif name == 'cuDeviceGetPCIBusId':
                        args[0].value = p.GPU.encode()
                    elif name in ('cuCtxCreate_v2', 'cuMemAlloc_v2'):
                        args[0]._obj.value = 1
                    elif name == 'cuMemcpyHtoD_v2':
                        self.payload = bytes(args[1]._obj)
                    elif name == 'cuMemcpyDtoH_v2':
                        args[0]._obj[:] = self.payload
                    return 0
                return invoke
        cuda = Cuda()
        with patch.object(p.ctypes, 'CDLL', return_value=cuda):
            p.cuda_check()
        self.assertEqual(cuda.bdf, p.GPU.encode())
        self.assertIn('cuCtxDestroy_v2', cuda.calls)
        self.assertNotIn('cuDeviceGetCount', cuda.calls)

    def test_cuda_error_is_failure(self):
        class Broken:
            def cuInit(self, flags):
                return 999
        with patch.object(p.ctypes, 'CDLL', return_value=Broken()), self.assertRaises(p.Stop):
            p.cuda_check()


class OverrideFake(Fake):
    def __init__(self, latch_after=3):
        super().__init__()
        self.latch = False
        self.latch_after = latch_after
        self.hook = lambda hw, off: None

    def cfg_write(self, *args):
        raise AssertionError('stage1-override attempted a PCI config write')

    def bar_write(self, off, value):
        if off not in (0x8872C, 0x2241C, 0x8841C):
            raise AssertionError('forbidden BAR0 write')
        super().bar_write(off, value)
        if self.latch_after == len(self.writes):
            self.data[(p.GPU, 0x6C)] = 6
            self.data[(p.GPU, 0x4C)] = 0x42
        self.hook(self, off)

    def close(self):
        pass


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.hw = OverrideFake()
        self.events = []
        self.exp = p.Experiment(self.hw, lambda event, **kw: self.events.append((event, kw)),
                                self.hw.sleep, lambda: self.hw.t)

    def test_success_order_and_both_end_snapshots_after_each_write(self):
        self.exp.stage1_override()
        self.assertEqual(self.hw.writes, [('bar', 0x8872C, 6), ('bar', 0x2241C, 0x81),
                                         ('bar', 0x8841C, 0xC0B40500)])
        samples = [kw for event, kw in self.events
                   if event == 'override_sample' and kw['phase'] == 'after_write']
        self.assertEqual([kw['offset'] for kw in samples], [0x8872C, 0x2241C, 0x8841C])
        for sample, write in zip(samples, self.hw.writes):
            self.assertEqual(sample['readback'], write[2])
            self.assertEqual(set(sample['links']), {p.GPU, p.ROOT})
            for link in sample['links'].values():
                self.assertTrue({'cap', 'cap2', 'sta'} <= set(link))
        self.assertTrue(self.events[-1][1]['capability_gen2'])
        for off in (0x8C040, 0x8C1C0, 0x88610):
            self.assertEqual(self.hw.regs[off], p.EXPECTED_BAR0[off])

    def test_early_success_at_first_or_second_write(self):
        for step in (1, 2):
            self.setUp()
            self.hw.latch_after = step
            self.exp.stage1_override()
            self.assertEqual(len(self.hw.writes), step)
            self.assertTrue(self.events[-1][1]['early_stop'])
            self.assertEqual(self.events[-1][1]['writes'], step)

    def test_delayed_latch_prevents_next_write(self):
        self.hw.latch_after = None
        sleep = self.hw.sleep
        def delayed(seconds):
            sleep(seconds)
            if self.hw.t >= .3:
                self.hw.data[(p.GPU, 0x6C)] = 6
                self.hw.data[(p.GPU, 0x4C)] = 0x42
        self.exp.sleep = delayed
        self.exp.stage1_override()
        self.assertEqual(len(self.hw.writes), 1)
        self.assertGreaterEqual(self.hw.t, .3)

    def test_partial_capability_stops_without_more_writes(self):
        for field, value in ((0x6C, 6), (0x4C, 0x42)):
            self.setUp()
            self.hw.hook = lambda hw, off: hw.data.__setitem__((p.GPU, field), value)
            with self.assertRaisesRegex(p.Stop, 'inconsistent'):
                self.exp.stage1_override()
            self.assertEqual(len(self.hw.writes), 1)
            self.assertFalse(self.events[-1][1]['capability_gen2'])

    def test_every_baseline_register_is_mandatory(self):
        for off in p.EXPECTED_BAR0:
            self.setUp()
            self.hw.regs[off] ^= 1
            with self.assertRaisesRegex(p.Stop, 'baseline mismatch'):
                self.exp.stage1_override()
            self.assertFalse(self.hw.writes)
            self.assertEqual(self.events[-1][0], 'override_baseline')

    def test_previous_stage1_residue_is_refused(self):
        self.hw.regs[0x2241C] = 0x81
        self.hw.regs[0x8841C] = 0xC0B40500
        with self.assertRaisesRegex(p.Stop, 'baseline mismatch'):
            self.exp.stage1_override()
        self.assertFalse(self.hw.writes)

    def test_loss_after_each_write_has_no_retry_or_mmio_after_loss(self):
        for step, (off, _, _) in enumerate(p.OVERRIDE_PATCHES, 1):
            self.setUp()
            self.hw.lose_at = off
            with self.assertRaisesRegex(p.Stop, 'lost'):
                self.exp.stage1_override()
            self.assertEqual(len(self.hw.writes), step)

    def test_mismatched_readback_stops_at_each_step(self):
        for step in (1, 2, 3):
            self.setUp()
            def corrupt(hw, off):
                if len(hw.writes) == step:
                    hw.regs[off] ^= 1
            self.hw.hook = corrupt
            with self.assertRaisesRegex(p.Stop, 'readback mismatch'):
                self.exp.stage1_override()
            self.assertEqual(len(self.hw.writes), step)
            self.assertEqual(self.events[-1][0], 'override_sample')

    def test_link_down_or_training_or_dll_inactive_stops(self):
        for dev, sta, cap in ((p.GPU, 0, 0x41), (p.ROOT, 0x1841, 0x42),
                              (p.ROOT, 0x1041, 0x100042)):
            self.setUp()
            def change(hw, off):
                hw.data[(dev, 0x52)] = sta
                hw.data[(dev, 0x4C)] = cap
            self.hw.hook = change
            with self.assertRaisesRegex(p.Stop, 'link changed'):
                self.exp.stage1_override()
            self.assertEqual(len(self.hw.writes), 1)

    def test_no_latch_records_negative_result_without_fallback(self):
        self.hw.latch_after = None
        with self.assertRaisesRegex(p.Stop, 'no fallback/retrain'):
            self.exp.stage1_override()
        self.assertEqual(len(self.hw.writes), 3)
        self.assertGreaterEqual(self.hw.t, 6)
        self.assertEqual(self.events[-1][1]['reason'], 'no_latch')
        self.assertFalse(self.events[-1][1]['capability_gen2'])

    def test_unexpected_change_in_other_register_stops_before_next_write(self):
        self.hw.hook = lambda hw, off: hw.regs.__setitem__(0x8C1C0, 0x20032)
        with self.assertRaisesRegex(p.Stop, 'prefix baseline changed'):
            self.exp.stage1_override()
        self.assertEqual(len(self.hw.writes), 1)

    def test_override_result_is_not_a_stage2_baseline(self):
        self.exp.stage1_override()
        with self.assertRaisesRegex(p.Stop, 'baseline mismatch'):
            self.exp.stage2()
        self.assertEqual(len(self.hw.writes), 3)

    def test_backend_allowlist_is_mode_scoped_and_forbids_speed(self):
        import ctypes
        memory = (ctypes.c_ubyte * 0x90000)()
        for override in (False, True):
            backend = p.Linux(mmio_writable=True, override=override)
            backend.mm = ctypes.addressof(memory)  # ordinary RAM, never sysfs
            for off in (0x8C040, 0x8C1C0, 0x88610):
                with self.assertRaises(p.Stop):
                    backend.bar_write(off, 0)
            if override:
                backend.bar_write(0x8872C, 6)
                self.assertEqual(backend.bar(0x8872C), 6)
            else:
                with self.assertRaises(p.Stop):
                    backend.bar_write(0x8872C, 6)
            with self.assertRaises(p.Stop):
                backend.cfg_write(p.ROOT, 0x50, 2, 0x20)
            backend.mm = None

    def run_main(self, directory, apply=True):
        from pathlib import Path
        args = ['--mode', 'stage1-override', '--log-dir', str(directory)]
        if apply:
            args += ['--apply', '--confirm-bdf', p.GPU]
        with patch.object(p, 'Linux', return_value=self.hw) as backend, \
             patch.object(p, 'LOCK_PATH', Path(directory) / 'test.lock'), \
             patch.object(p, 'cuda_check', side_effect=AssertionError('CUDA called')), \
             contextlib.redirect_stdout(io.StringIO()):
            result = p.main(args)
        backend.assert_called_once_with(writable=False, mmio_writable=apply, override=True)
        return result

    def test_cli_plan_is_readonly_and_logs_separately(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            self.run_main(temp, apply=False)
            self.run_main(temp, apply=False)
            files = list(Path(temp).glob('gpu0-stage1-override-*.jsonl'))
            self.assertEqual(len(files), 2)
            rows = [json.loads(line) for line in files[0].read_text().splitlines()]
            self.assertEqual(rows[-1]['event'], 'plan')
            self.assertFalse(rows[-1]['hardware_writes'])
            self.assertEqual(rows[-1]['bar0_patches'], [list(patch) for patch in p.OVERRIDE_PATCHES])
            self.assertTrue(all(row['mode'] == 'stage1-override' for row in rows))
            self.assertFalse(self.hw.writes)

    def test_cli_apply_journal_records_success(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(self.run_main(temp), 0)
            rows = [json.loads(line) for line in next(Path(temp).glob('*.jsonl')).read_text().splitlines()]
            self.assertEqual(rows[-1]['event'], 'override_result')
            self.assertTrue(rows[-1]['capability_gen2'])
            self.assertEqual(sum(row['event'] == 'before_mmio' for row in rows), 3)

    def test_cli_failure_keeps_intent_and_abort_journal(self):
        import json
        import tempfile
        from pathlib import Path
        self.hw.lose_at = 0x8872C
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(p.Stop):
                self.run_main(temp)
            rows = [json.loads(line) for line in next(Path(temp).glob('*.jsonl')).read_text().splitlines()]
            self.assertEqual(rows[-1]['event'], 'abort')
            self.assertFalse(rows[-1]['recovery_writes'])
            self.assertEqual(sum(row['event'] == 'before_mmio' for row in rows), 1)
            self.assertFalse(any(row['event'] == 'override_result' for row in rows))

    def test_override_cli_still_requires_explicit_gpu0(self):
        for bdf in ('', '0000:04:00.0'):
            args = ['--mode', 'stage1-override', '--apply', '--confirm-bdf', bdf]
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                p.arguments(args)
        args = p.arguments(['--mode', 'stage1-override', '--apply', '--confirm-bdf', p.GPU])
        self.assertTrue(args.apply)


if __name__ == '__main__':
    unittest.main()
