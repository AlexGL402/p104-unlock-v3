import contextlib
import ctypes
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import p104_gen2_linux as p
from test_linux_gen2 import Fake


class CapFake(Fake):
    def __init__(self):
        super().__init__()
        self.regs = dict(p.EXPECTED_OVERRIDE_BAR0)
        self.data[(p.GPU, 0x4C)] = 0x00453C41
        self.data[(p.GPU, 0x6C)] = 6
        self.hook = lambda hw: hw.data.__setitem__((p.GPU, 0x4C), 0x00453C42)

    def cfg_write(self, *args):
        raise AssertionError('PCI config write attempted')

    def bar_write(self, off, value):
        assert not self.writes, 'second write attempted'
        assert (off, value) == (0x8C1C0, 0x00020032), 'forbidden BAR0 write'
        super().bar_write(off, value)
        self.hook(self)

    def close(self):
        pass


class CapSpeedTests(unittest.TestCase):
    def setUp(self):
        self.hw = CapFake()
        self.events = []
        self.exp = p.Experiment(self.hw, lambda event, **kw: self.events.append((event, kw)),
                                self.hw.sleep, lambda: self.hw.t)

    def test_one_write_success_and_two_second_both_end_observation(self):
        self.exp.stage1_cap_speed()
        self.assertEqual(self.hw.writes, [('bar', 0x8C1C0, 0x20032)])
        self.assertEqual(self.hw.t, 2)
        result = self.events[-1][1]
        self.assertTrue(result['capability_gen2'])
        self.assertFalse(result['link_gen2_x4'])
        samples = [row for name, row in self.events if name == 'cap_speed_sample']
        self.assertGreater(len(samples), 1)
        for row in samples:
            self.assertEqual(row['readback'], 0x20032)
            self.assertEqual(set(row['links']), {p.GPU, p.ROOT})
            for bdf in (p.GPU, p.ROOT):
                self.assertTrue({'cap', 'cap2', 'sta'} <= set(row['links'][bdf]))
            self.assertEqual(row['links_hex'][p.GPU]['cap'], '0x00453C42')
        for off, expected in p.EXPECTED_OVERRIDE_BAR0.items():
            if off != 0x8C1C0:
                self.assertEqual(self.hw.regs[off], expected)

    def test_no_cap_change_is_negative_with_one_write_no_retry(self):
        self.hw.hook = lambda hw: None
        with self.assertRaisesRegex(p.Stop, 'without fallback'):
            self.exp.stage1_cap_speed()
        self.assertEqual(self.hw.t, 2)
        self.assertEqual(len(self.hw.writes), 1)
        self.assertEqual(self.events[-1][1]['reason'], 'lnkcap_unchanged')

    def test_delayed_capability_change_is_observed(self):
        self.hw.hook = lambda hw: None
        sleep = self.hw.sleep
        def delayed(seconds):
            sleep(seconds)
            if self.hw.t >= .5:
                self.hw.data[(p.GPU, 0x4C)] = 0x453C42
        self.exp.sleep = delayed
        self.exp.stage1_cap_speed()
        self.assertTrue(self.events[-1][1]['capability_gen2'])
        self.assertEqual(len(self.hw.writes), 1)

    def test_every_bar0_baseline_value_is_strict(self):
        for off in p.EXPECTED_OVERRIDE_BAR0:
            self.setUp()
            self.hw.regs[off] ^= 1
            with self.assertRaisesRegex(p.Stop, 'exact override baseline'):
                self.exp.stage1_cap_speed()
            self.assertFalse(self.hw.writes)

    def test_factory_baseline_refused_without_prefix_preparation(self):
        self.hw.regs = dict(p.EXPECTED_BAR0)
        with self.assertRaisesRegex(p.Stop, 'no prefix preparation'):
            self.exp.stage1_cap_speed()
        self.assertFalse(self.hw.writes)

    def test_incomplete_prefix_refused(self):
        for off in (0x8872C, 0x2241C, 0x8841C):
            self.setUp()
            self.hw.regs[off] = p.EXPECTED_BAR0[off]
            with self.assertRaises(p.Stop):
                self.exp.stage1_cap_speed()
            self.assertFalse(self.hw.writes)

    def test_exact_gpu_capabilities_required(self):
        for off, value in ((0x4C, 0x4537409), (0x4C, 0x453C42), (0x6C, 2), (0x6C, 14)):
            self.setUp()
            self.hw.data[(p.GPU, off)] = value
            with self.assertRaisesRegex(p.Stop, 'LNKCAP='):
                self.exp.stage1_cap_speed()
            self.assertFalse(self.hw.writes)

    def test_driver_d0_decode_busmaster_and_link_guards(self):
        mutations = [lambda hw: setattr(hw, 'bound', True),
                     lambda hw: hw.data.__setitem__((p.GPU, 0x84), 3),
                     lambda hw: hw.data.__setitem__((p.GPU, 4), 0),
                     lambda hw: hw.data.__setitem__((p.GPU, 4), 6),
                     lambda hw: hw.data.__setitem__((p.GPU, 0x52), 0x1042),
                     lambda hw: hw.data.__setitem__((p.ROOT, 0x52), 0x1081)]
        for mutate in mutations:
            self.setUp()
            mutate(self.hw)
            with self.assertRaises(p.Stop):
                self.exp.stage1_cap_speed()
            self.assertFalse(self.hw.writes)

    def test_identity_and_topology_guards(self):
        for device in (p.GPU, p.ROOT):
            self.setUp()
            self.hw.data[(device, 0)] = 0x1234
            with self.assertRaises(p.Stop):
                self.exp.stage1_cap_speed()
            self.assertFalse(self.hw.writes)
        self.setUp()
        self.hw.topology = lambda: (_ for _ in ()).throw(p.Stop('wrong parent'))
        with self.assertRaises(p.Stop):
            self.exp.stage1_cap_speed()
        self.assertFalse(self.hw.writes)

    def test_lost_device_after_single_write_never_reads_bar_again(self):
        self.hw.lose_at = 0x8C1C0
        with self.assertRaisesRegex(p.Stop, 'lost'):
            self.exp.stage1_cap_speed()
        self.assertEqual(len(self.hw.writes), 1)

    def test_full_readback_including_bits_outside_mask(self):
        self.hw.hook = lambda hw: hw.regs.__setitem__(0x8C1C0, 0x20033)
        with self.assertRaisesRegex(p.Stop, 'full readback mismatch'):
            self.exp.stage1_cap_speed()
        self.assertEqual(len(self.hw.writes), 1)
        self.assertEqual(self.events[-1][0], 'cap_speed_sample')

    def test_changed_link_capabilities_or_other_register_aborts(self):
        mutations = [lambda hw: hw.data.__setitem__((p.GPU, 0x52), 0),
                     lambda hw: hw.data.__setitem__((p.ROOT, 0x52), 0x1841),
                     lambda hw: hw.data.__setitem__((p.GPU, 0x52), 0x1042),
                     lambda hw: hw.data.__setitem__((p.GPU, 0x6C), 2),
                     lambda hw: hw.data.__setitem__((p.ROOT, 0x70), 2),
                     lambda hw: hw.data.__setitem__((p.ROOT, 0x4C), 0x43),
                     lambda hw: hw.regs.__setitem__(0x88610, 1)]
        for mutate in mutations:
            self.setUp()
            self.hw.hook = mutate
            with self.assertRaises(p.Stop):
                self.exp.stage1_cap_speed()
            self.assertEqual(len(self.hw.writes), 1)
            self.assertFalse(any(name == 'cap_speed_result' for name, _ in self.events))

    def test_backend_single_write_exact_transition_and_allowlist(self):
        memory = (ctypes.c_ubyte * 0x90000)()
        addr = ctypes.addressof(memory)
        backend = p.Linux(mmio_writable=True, cap_speed=True)
        backend.mm = addr  # ordinary RAM, not a device mapping
        for off in (0x8872C, 0x2241C, 0x8841C, 0x8C040, 0x88610):
            with self.assertRaises(p.Stop):
                backend.bar_write(off, 0)
        ctypes.c_uint32.from_address(addr + 0x8C1C0).value = 0x10032
        with self.assertRaises(p.Stop):
            backend.bar_write(0x8C1C0, 0x20033)
        backend.bar_write(0x8C1C0, 0x20032)
        ctypes.c_uint32.from_address(addr + 0x8C1C0).value = 0x10032
        with self.assertRaises(p.Stop):
            backend.bar_write(0x8C1C0, 0x20032)
        backend.mm = None
        with self.assertRaises(p.Stop):
            backend.cfg_write(p.ROOT, 0x50, 2, 0x20)
        with self.assertRaises(p.Stop):
            p.Linux(writable=True, cap_speed=True)
        with self.assertRaises(p.Stop):
            p.Linux(override=True, cap_speed=True)

    def run_main(self, directory, apply):
        args = ['--mode', 'stage1-cap-speed', '--log-dir', str(directory)]
        args += ['--apply', '--confirm-bdf', p.GPU] if apply else ['--dry-run']
        with patch.object(p, 'Linux', return_value=self.hw) as backend, \
             patch.object(p, 'LOCK_PATH', Path(directory) / 'test.lock'), \
             patch.object(p, 'cuda_check', side_effect=AssertionError('CUDA called')), \
             contextlib.redirect_stdout(io.StringIO()):
            result = p.main(args)
        backend.assert_called_once_with(writable=False, mmio_writable=apply,
                                        override=False, cap_speed=True)
        return result

    def test_dry_run_strict_preflight_and_separate_jsonl(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(self.run_main(temp, False), 0)
            self.assertEqual(self.run_main(temp, False), 0)
            files = list(Path(temp).glob('gpu0-stage1-cap-speed-*.jsonl'))
            self.assertEqual(len(files), 2)
            rows = [json.loads(line) for line in files[0].read_text().splitlines()]
            self.assertEqual(rows[-1]['mask'], 0x30000)
            self.assertEqual(rows[-1]['old'], 0x10032)
            self.assertEqual(rows[-1]['new'], 0x20032)
            self.assertFalse(rows[-1]['hardware_writes'])
            self.assertFalse(self.hw.writes)
            self.hw.regs = dict(p.EXPECTED_BAR0)
            with self.assertRaisesRegex(p.Stop, 'override baseline'):
                self.run_main(temp, False)

    def test_apply_jsonl_success_and_failure(self):
        for lost in (False, True):
            self.setUp()
            with tempfile.TemporaryDirectory() as temp:
                if lost:
                    self.hw.lose_at = 0x8C1C0
                    with self.assertRaises(p.Stop):
                        self.run_main(temp, True)
                else:
                    self.run_main(temp, True)
                rows = [json.loads(line) for line in next(Path(temp).glob('*.jsonl')).read_text().splitlines()]
                self.assertTrue(all(row['mode'] == 'stage1-cap-speed' for row in rows))
                self.assertEqual(sum(row['event'] == 'before_mmio' for row in rows), 1)
                self.assertEqual(rows[-1]['event'], 'abort' if lost else 'cap_speed_result')

    def test_cli_permissions_and_exclusive_actions(self):
        for extra in ([], ['--confirm-bdf', '0000:04:00.0'],
                      ['--confirm-bdf', p.GPU, '--dry-run']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                p.arguments(['--mode', 'stage1-cap-speed', '--apply'] + extra)
        self.assertFalse(p.arguments(['--mode', 'stage1-cap-speed']).apply)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            p.arguments(['--mode', 'verify-cuda', '--dry-run', '--authorize-cuda', '--confirm-bdf', p.GPU])


if __name__ == '__main__':
    unittest.main()
