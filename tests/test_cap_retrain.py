import contextlib
import ctypes
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import p104_gen2_linux as p
from test_cap_speed import CapFake


class RetrainFake(CapFake):
    def __init__(self):
        super().__init__()
        self.regs = dict(p.EXPECTED_CAP_BAR0)
        self.data[(p.GPU, 0x4C)] = 0x453C42
        self.data[(p.ROOT, 0x4C)] = 0x100103
        self.data[(p.ROOT, 0x6C)] = 14
        self.data[(p.ROOT, 0x52)] = 0x3041
        self.data[(p.ROOT, 0x42)] = 0x42
        for dev in (p.GPU, p.ROOT):
            self.data.update({(dev, 0x100): 0x20001, (dev, 0x104): 0,
                              (dev, 0x110): 0, (dev, 0x130): 0, (dev, 0x4A): 0})
        self.stats = {dev: {} for dev in (p.GPU, p.ROOT)}
        self.hook = lambda hw, bdf, off: None
        self.training = False
        self.train_success = True
        self.retrain_writes = 0

    def cfg_write(self, bdf, off, size, value):
        assert size == 2
        assert (bdf, off) in ((p.GPU, 0x70), (p.ROOT, 0x70), (p.ROOT, 0x50))
        self.writes.append(('pci', bdf, off, value))
        self.data[(bdf, off)] = value
        if off == 0x50:
            self.retrain_writes += 1
            assert self.retrain_writes == 1
            self.data[(bdf, off)] &= ~0x20
            self.training = True
            self.data[(p.ROOT, 0x52)] = 0x1841  # Training, DLL temporarily inactive.
        self.hook(self, bdf, off)

    def bar_write(self, *args):
        raise AssertionError('MMIO write attempted')

    def bar(self, off):
        assert not self.training, 'BAR accessed while root training'
        return super().bar(off)

    def cfg(self, bdf, off, size):
        if self.training and bdf == p.GPU:
            raise AssertionError('endpoint accessed while root training')
        return super().cfg(bdf, off, size)

    def aer_stats(self, bdf):
        return dict(self.stats[bdf])

    def sleep(self, seconds):
        super().sleep(seconds)
        if self.training and self.t >= .15:
            self.training = False
            for dev in (p.GPU, p.ROOT):
                self.data[(dev, 0x52)] = (0x3042 if dev == p.ROOT else 0x1042) if self.train_success else (0x3041 if dev == p.ROOT else 0x1041)


class RetrainTests(unittest.TestCase):
    def setUp(self):
        self.hw = RetrainFake()
        self.events = []
        self.exp = p.Experiment(self.hw, lambda event, **row: self.events.append((event, row)),
                                self.hw.sleep, lambda: self.hw.t)
        self.run = p.CapRetrain(self.exp)

    def test_success_tls_order_preservation_one_retrain_no_mmio_during_training(self):
        self.run.run(apply=True)
        self.assertEqual(self.hw.writes, [('pci', p.GPU, 0x70, 0xA2),
                                         ('pci', p.ROOT, 0x70, 0xA2),
                                         ('pci', p.ROOT, 0x50, 0x120)])
        self.assertEqual(self.events[-1][0], 'cap_retrain_result')
        self.assertTrue(self.events[-1][1]['gen2_x4'])
        self.assertEqual(self.hw.regs, p.EXPECTED_CAP_BAR0)

    def test_dry_run_strict_no_writes(self):
        self.run.run()
        self.assertFalse(self.hw.writes)
        self.assertEqual(self.events[-1][0], 'cap_retrain_plan')

    def test_already_tls2_skips_tls_writes(self):
        for dev in (p.GPU, p.ROOT):
            self.hw.data[(dev, 0x70)] = 0xA2
        self.run.run(True)
        self.assertEqual(len(self.hw.writes), 1)

    def test_each_bar0_baseline_required(self):
        for off in p.EXPECTED_CAP_BAR0:
            self.setUp()
            self.hw.regs[off] ^= 1
            with self.assertRaises(p.Stop):
                self.run.run(True)
            self.assertFalse(self.hw.writes)

    def test_missing_aer_refused(self):
        self.hw.data[(p.GPU, 0x100)] = 0
        with self.assertRaisesRegex(p.Stop, 'AER capability missing'):
            p.CapRetrain(self.exp)
        self.assertFalse(self.hw.writes)

    def test_malformed_ext_cap_list(self):
        self.hw.data[(p.GPU, 0x100)] = (0x100 << 20) | 2
        with self.assertRaisesRegex(p.Stop, 'malformed'):
            p.find_ext_cap(self.hw, p.GPU, 1)

    def test_aer_baseline_or_pending_transactions_refused(self):
        for off, value in ((0x104, 1), (0x110, 1), (0x130, 1), (0x4A, 0x20)):
            self.setUp()
            self.hw.data[(p.ROOT, off)] = value
            with self.assertRaises(p.Stop):
                self.run.run(True)
            self.assertFalse(self.hw.writes)

    def test_aer_interrupt_message_number_is_not_an_error(self):
        self.hw.data[(p.ROOT, 0x130)] = 5 << 27
        self.run.run(False)
        self.assertFalse(self.hw.writes)

    def test_new_aer_after_first_tls_prevents_remaining_writes(self):
        self.hw.hook = lambda hw, dev, off: hw.data.__setitem__((p.ROOT, 0x110), 1)
        with self.assertRaisesRegex(p.Stop, 'AER'):
            self.run.run(True)
        self.assertEqual(len(self.hw.writes), 1)

    def test_kernel_counters_detect_error_even_if_status_was_cleared(self):
        self.hw.hook = lambda hw, dev, off: hw.stats[p.ROOT].__setitem__('correctable', '1')
        with self.assertRaisesRegex(p.Stop, 'counters changed'):
            self.run.run(True)
        self.assertEqual(len(self.hw.writes), 1)

    def test_tls_full_readback_failure_no_retrain(self):
        self.hw.hook = lambda hw, dev, off: hw.data.__setitem__((dev, off), hw.data[(dev, off)] ^ 0x100)
        with self.assertRaisesRegex(p.Stop, 'readback mismatch'):
            self.run.run(True)
        self.assertEqual(len(self.hw.writes), 1)

    def test_training_before_trigger_is_not_retried(self):
        self.hw.data[(p.ROOT, 0x52)] |= 0x800
        with self.assertRaises(p.Stop):
            self.run.run(True)
        self.assertFalse(self.hw.writes)

    def test_no_success_when_stays_gen1_no_second_retrain(self):
        self.hw.train_success = False
        with self.assertRaisesRegex(p.Stop, 'no retry'):
            self.run.run(True)
        self.assertEqual(self.hw.retrain_writes, 1)
        self.assertEqual(self.hw.t, 5)

    def test_stuck_training_times_out_without_endpoint_or_bar_access(self):
        self.hw.sleep = lambda seconds: setattr(self.hw, 't', self.hw.t + seconds)
        self.exp.sleep = self.hw.sleep
        with self.assertRaisesRegex(p.Stop, 'no retry'):
            self.run.run(True)
        self.assertEqual(self.hw.retrain_writes, 1)

    def test_root_link_down_after_trigger_stops_immediately(self):
        def down(hw, dev, off):
            if off == 0x50:
                hw.training = False
                hw.data[(p.ROOT, 0x52)] = 0
        self.hw.hook = down
        with self.assertRaisesRegex(p.Stop, 'Link Down'):
            self.run.run(True)
        self.assertEqual(self.hw.retrain_writes, 1)

    def test_post_retrain_mmio_change_is_not_corrected(self):
        def change(hw, dev, off):
            if off == 0x50:
                hw.regs[0x8C040] = 0x20045800
        self.hw.hook = change
        with self.assertRaisesRegex(p.Stop, 'BAR0 changed'):
            self.run.run(True)
        self.assertEqual(len(self.hw.writes), 3)

    def test_backend_forbids_all_mmio_and_unlisted_pci_writes(self):
        backend = p.CapRetrainLinux(writable=True)
        for off in p.EXPECTED_CAP_BAR0:
            with self.assertRaises(p.Stop):
                backend.bar_write(off, 0)
        with patch.object(backend, 'cfg', side_effect=self.hw.cfg):
            for dev, off, size, value in ((p.GPU, 0x50, 2, 0x120),
                                          (p.ROOT, 0x104, 2, 1), (p.ROOT, 0x50, 4, 0x120),
                                          (p.GPU, 0x70, 2, 0xB2)):
                with self.assertRaises(p.Stop):
                    backend.cfg_write(dev, off, size, value)
            with patch.object(p.Linux, 'cfg_write') as raw:
                backend.cfg_write(p.GPU, 0x70, 2, 0xA2)
                with self.assertRaises(p.Stop):
                    backend.cfg_write(p.GPU, 0x70, 2, 0xA2)
                self.hw.data[(p.GPU, 0x70)] = 0xA2
                self.hw.data[(p.ROOT, 0x70)] = 0xA2
                backend.cfg_write(p.ROOT, 0x50, 2, 0x120)
                with self.assertRaises(p.Stop):
                    backend.cfg_write(p.ROOT, 0x50, 2, 0x120)
                self.assertEqual(raw.call_count, 2)

    def exp_for_main(self, hw, record):
        return self.exp.__class__(hw, record, hw.sleep, lambda: hw.t)

    def test_cli_permission_and_journal(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            p.arguments(['--mode', 'stage2-cap-retrain', '--apply', '--confirm-bdf', p.GPU])
        with tempfile.TemporaryDirectory() as temp:
            args = ['--mode', 'stage2-cap-retrain', '--apply', '--authorize-cap-retrain',
                    '--confirm-bdf', p.GPU, '--log-dir', temp]
            with patch.object(p, 'CapRetrainLinux', return_value=self.hw) as backend, \
                 patch.object(p, 'Experiment', side_effect=lambda hw, record: self.exp_for_main(hw, record)), \
                 patch.object(p, 'LOCK_PATH', Path(temp) / 'test.lock'), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(p.main(args), 0)
            backend.assert_called_once_with(writable=True)
            rows = [json.loads(line) for line in next(Path(temp).glob('*.jsonl')).read_text().splitlines()]
            self.assertEqual(rows[-1]['event'], 'cap_retrain_result')
            self.assertEqual(sum(row['event'] == 'before_retrain' for row in rows), 1)


if __name__ == '__main__':
    unittest.main()
