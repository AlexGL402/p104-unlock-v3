"""Root-only new retrain policy, synthetic sysfs/regular RAM file, no devices."""
import ast
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_bar0_readonly as fixtures
import pcie_root_retrain_once as retrain
from design import p104_bar0_ro_build as native


class Clock:
    def __init__(self):
        self.ns = 0
    def __call__(self):
        self.ns += 20_000_000
        return self.ns


class RootRetrainOnceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='p104-root-rl-')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary, cls.manifest = native.build(cls.directory.name)
        cls.lib, _ = native.load(cls.binary)

    def setUp(self):
        self.fixture = fixtures.Bar0ReadOnlyTests(methodName='runTest')
        self.fixture.lib = self.lib
        self.fixture.setUp()  # Fixture construction only; no old tests run.
        self.addCleanup(self.fixture.doCleanups)
        self.change = self.fixture.change_config
        ram = bytearray(self.fixture.bar.read_bytes())
        for offset, value in retrain.EXPECTED.items():
            ram[offset:offset+4] = value.to_bytes(4, 'little')
        self.fixture.bar.write_bytes(ram)
        self.original_bar = bytes(ram)
        self.change(self.fixture.gpu/'config', 0x6c, 0x00453c42, 4)
        self.change(self.fixture.gpu/'config', 0x8c, 6, 4)
        self.change(self.fixture.gpu/'config', 0x90, 2)
        self.change(self.fixture.root/'config', 0xc0, 0x42)
        self.change(self.fixture.root/'config', 0xa0, 0x40)
        self.rows, self.writes = [], []
        self.adapter = retrain.Retrain(self.lib, self.fixture.review, self.rows.append,
                                       sysfs=self.fixture.sysfs, clock=Clock())
        self.addCleanup(self.adapter.close)
        self.sequence = [(0x3841, 0x60), (0x7042, 0x40)]
        self.last = self.sequence[-1]
        self.position = 0

    def root_word(self, offset):
        return int.from_bytes((self.fixture.root/'config').read_bytes()[offset:offset+2], 'little')

    def apply(self, *, short=False, hook=None):
        pwrite, poll = os.pwrite, self.adapter.poll_root
        pread, mmio = os.pread, self.lib.ro_read
        def write(fd, data, offset):
            self.writes.append((os.readlink(f'/proc/self/fd/{fd}'), offset, data))
            result = pwrite(fd, data[:1] if short else data, offset)
            if hook:
                hook()
            return result
        def advance(*args, **kwargs):
            if self.position < len(self.sequence):
                self.last = self.sequence[self.position]
                self.position += 1
            sta, ctl = self.last
            # Simulated hardware transitions/self-clear, not protocol writes.
            self.change(self.fixture.root/'config', 0xa2, sta)
            self.change(self.fixture.root/'config', 0xa0, ctl)
            self.change(self.fixture.gpu/'config', 0x72, 0x1040 | (sta & 15))
            return poll(*args, **kwargs)
        def guarded_pread(fd, size, offset):
            path = os.readlink(f'/proc/self/fd/{fd}')
            if self.adapter.attempted and str(self.fixture.gpu) in path and self.root_word(0xa2) & 0x800:
                raise AssertionError('GPU config read during Training')
            return pread(fd, size, offset)
        def guarded_mmio(handle, offset):
            if self.adapter.attempted and self.root_word(0xa2) & 0x800:
                raise AssertionError('BAR read during Training')
            return mmio(handle, offset)
        with patch.object(retrain.os, 'pwrite', side_effect=write), \
             patch.object(self.adapter, 'poll_root', side_effect=advance), \
             patch.object(retrain.os, 'pread', side_effect=guarded_pread), \
             patch.object(self.lib, 'ro_read', side_effect=guarded_mmio):
            return retrain.run(self.adapter, apply=True)

    def test_success_one_exact_root_word_and_no_mmio_changes(self):
        result = self.apply()
        self.assertEqual(result['result'], 'PASS')
        self.assertTrue(result['gen2_observed'])
        self.assertTrue(result['training_seen'])
        self.assertEqual(self.writes, [(str(self.fixture.root/'config'), 0xa0, b'\x60\x00')])
        self.assertEqual(self.fixture.bar.read_bytes(), self.original_bar)
        self.assertEqual(self.root_word(0xc0), 0x42)  # TLS untouched
        self.assertEqual(result['post_speed'], 0x20045800)
        self.assertEqual(result['mmio_writes'], 0)
        self.assertFalse(result['nvidia_cuda_verified'])
        self.assertLess(len(self.rows), 3000)

    def test_dry_run_does_not_open_root_writer_or_trigger(self):
        with patch.object(retrain.os, 'pwrite', side_effect=AssertionError('must not write')):
            result = retrain.run(self.adapter, apply=False)
        self.assertEqual(result['reason'], 'dry_run_ready_no_write')
        self.assertFalse(result['retrain_attempted'])
        self.assertEqual(self.root_word(0xa0), 0x40)
        self.assertIsNone(self.adapter.writer_fd)

    def test_exact_root_old_word_required(self):
        self.change(self.fixture.root/'config', 0xa0, 0)
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.writes, [])
        self.assertFalse(result['retrain_attempted'])

    def test_old_internal_speed_refused_before_any_write(self):
        ram = bytearray(self.original_bar)
        ram[0x8c040:0x8c044] = (0x20085800).to_bytes(4, 'little')
        self.fixture.bar.write_bytes(ram)
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.writes, [])
        self.assertIn('post-GROUP prepared', result['reason'])

    def test_pre_group_baselines_rejected_without_write(self):
        for offset, value in ((0x88610, 0), (0x8C1C0, 0x00020032)):
            with self.subTest(offset=hex(offset)):
                ram = bytearray(self.original_bar)
                ram[offset:offset+4] = value.to_bytes(4, 'little')
                self.fixture.bar.write_bytes(ram)
                result = self.apply()
                self.assertEqual(result['result'], 'FAIL')
                self.assertFalse(result['retrain_attempted'])
                self.assertEqual(self.writes, [])

    def test_gen2_only_at_end_does_not_establish_stability(self):
        sample = dict(raw=0x7042, control=0x40, pending=False, training=False,
                      speed=2, width=4, start_ns=0, end_ns=20_000_000)
        with self.assertRaisesRegex(retrain.ro.Stop, 'stability window too short'):
            self.adapter.observe(0, sample)
        self.assertEqual(self.writes, [])

    def test_gpu_command_must_be_exactly_0002(self):
        self.change(self.fixture.gpu/'config', 4, 0x402)
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.writes, [])
        self.assertIn('exactly 0002', result['reason'])

    def test_bound_driver_refuses(self):
        (self.fixture.gpu/'driver').mkdir()
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.writes, [])

    def test_missing_tls_refuses(self):
        self.change(self.fixture.gpu/'config', 0x90, 0)
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.writes, [])

    def test_preexisting_training_or_dll_loss_refuses(self):
        self.change(self.fixture.root/'config', 0xa2, 0x3841)
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.writes, [])

    def test_link_down_after_trigger_no_gpu_readback_no_second_trigger(self):
        self.sequence = [(0x1041, 0x40)]
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertIsNone(result['post_speed'])
        self.assertEqual(len(self.writes), 1)
        self.assertIn('Link Down', result['reason'])

    def test_training_timeout_has_no_endpoint_reads(self):
        self.sequence = [(0x3841, 0x60)]
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertTrue(result['training_seen'])
        self.assertIsNone(result['post_speed'])
        self.assertEqual(len(self.writes), 1)
        self.assertIn('timeout', result['reason'])

    def test_self_clear_without_gen2_is_fail(self):
        self.sequence = [(0x3041, 0x40)]
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertFalse(result['gen2_observed'])
        self.assertEqual(result['post_speed'], 0x20045800)
        self.assertEqual(len(self.writes), 1)
        self.assertIn('not obtained', result['reason'])

    def test_gen2_without_observed_training_can_pass(self):
        self.sequence = [(0x7042, 0x40)]
        result = self.apply()
        self.assertEqual(result['result'], 'PASS')
        self.assertFalse(result['training_seen'])

    def test_retrain_bit_stuck_is_not_manually_cleared(self):
        self.sequence = [(0x3041, 0x60)]
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.root_word(0xa0), 0x60)

    def test_aer_after_write_stops_before_endpoint_access(self):
        result = self.apply(hook=lambda: self.change(self.fixture.root/'config', 0x104, 1, 4))
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(len(self.writes), 1)
        self.assertIsNone(result['post_speed'])
        self.assertIn('AER', result['reason'])

    def test_short_write_is_terminal(self):
        result = self.apply(short=True)
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(len(self.writes), 1)
        self.assertFalse(result['write_call_returned'])
        self.assertIsNone(result['post_speed'])
        self.assertIn('short', result['reason'])

    def test_post_speed_drift_is_recorded_never_rewritten(self):
        def drift():
            ram = bytearray(self.fixture.bar.read_bytes())
            ram[0x8c040:0x8c044] = (0x20085800).to_bytes(4, 'little')
            self.fixture.bar.write_bytes(ram)
        result = self.apply(hook=drift)
        self.assertEqual(result['result'], 'FAIL')
        self.assertTrue(result['gen2_observed'])
        self.assertEqual(result['post_speed'], 0x20085800)
        self.assertEqual(len(self.writes), 1)

    def test_permit_consumed_even_on_failure(self):
        self.sequence = [(0x1041, 0x40)]
        self.apply()
        with self.assertRaisesRegex(retrain.ro.Stop, 'already consumed'):
            self.adapter.trigger()
        self.assertEqual(len(self.writes), 1)

    def test_width_loss_is_stop_before_endpoint_postchecks(self):
        self.sequence = [(0x7022, 0x40)]
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(len(self.writes), 1)
        self.assertIsNone(result['post_speed'])

    def test_only_root_gen2_never_passes(self):
        original = self.adapter.cfg
        def cfg(offset, size):
            value = original(offset, size)
            if self.adapter.attempted and offset == 0x72:
                return 0x1041
            return value
        with patch.object(self.adapter, 'cfg', side_effect=cfg):
            result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertIn('disagreement', result['reason'])
        self.assertEqual(len(self.writes), 1)

    def test_new_training_after_observation_blocks_final_gpu_read(self):
        def emit(row):
            self.rows.append(row)
            if row['event'] == 'observation_end':
                self.last = (0x3841, 0x60)
        self.adapter.emit = emit
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertIsNone(result['post_speed'])
        self.assertEqual(len(self.writes), 1)

    def test_risk_confirmation_required_before_any_open(self):
        argv = ['pcie_root_retrain_once.py', '--apply', '--native-library', 'missing',
                '--mapping-review', 'missing', '--output', 'missing',
                '--confirm-gpu0-unbound', '--confirm-exclusive-access']
        with patch('sys.argv', argv), contextlib.redirect_stderr(io.StringIO()), \
             patch.object(retrain.os, 'open', side_effect=AssertionError('must not open')):
            with self.assertRaises(SystemExit) as caught:
                retrain.main()
        self.assertEqual(caught.exception.code, 2)

    def test_gpu_training_observed_at_final_state_stops_further_gpu_reads(self):
        original = self.adapter.cfg
        seen = False
        def cfg(offset, size):
            nonlocal seen
            if seen:
                raise AssertionError('extra GPU read after GPU Training observed')
            value = original(offset, size)
            if self.adapter.attempted and offset == 0x72:
                seen = True
                return 0x1841
            return value
        with patch.object(self.adapter, 'cfg', side_effect=cfg):
            result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertIsNone(result['post_speed'])
        self.assertEqual(len(self.writes), 1)

    def test_source_has_exactly_one_pwrite_and_readonly_native_loader(self):
        source = Path(retrain.__file__).read_text()
        tree = ast.parse(source)
        writes = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and
                  isinstance(n.func, ast.Attribute) and n.func.attr == 'pwrite']
        self.assertEqual(len(writes), 1)
        self.assertEqual(ast.literal_eval(writes[0].args[1]), b'\x60\x00')
        self.assertEqual(source.count('O_RDWR'), 2)  # Root writer and advisory lock only.
        for forbidden in ('speed_once(', 'prep_step(', 'cfg_write(', 'O_WRONLY | os.O_SYNC'):
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()
