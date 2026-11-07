"""Only new preparation policy tests: fixture sysfs/config/RAM, never hardware."""
import os
import struct
import tempfile
import unittest
from unittest.mock import patch

import test_bar0_readonly as fixtures
import pcie_prepare_cap_ready as prep
from design import p104_cap_ready_build as native


class CapReadyPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='p104-preparation-')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary, cls.manifest = native.build(cls.directory.name)
        cls.lib, _ = native.load(cls.binary)

    def setUp(self):
        # Fixture data only; no old read-only test cases are run.
        self.fixture = fixtures.Bar0ReadOnlyTests(methodName='runTest')
        self.fixture.lib = self.lib
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.change = self.fixture.change_config
        self.change(self.fixture.gpu/'config', 0x90, 0x21)  # TLS1, preserve bit5
        self.change(self.fixture.root/'config', 0xc0, 0x43)  # TLS3, preserve bit6
        self.rows = []
        self.adapter = prep.Preparation(self.lib, self.fixture.review, self.rows.append,
                                       sysfs=self.fixture.sysfs)
        self.addCleanup(self.adapter.close)
        self.native_step = self.lib.prep_step
        self.calls = []

    def hardware_effect(self, handle, index):
        self.calls.append(index)
        result = self.native_step(handle, index)
        if result == 0:
            if index == 2:
                self.change(self.fixture.gpu/'config', 0x8c, 6, 4)
            elif index == 3:
                self.change(self.fixture.gpu/'config', 0x6c, 0x00453c42, 4)
        return result

    def apply(self, effect=None, observe=False):
        if not observe:
            with patch.object(self.adapter, 'hold_stage'):
                return self.apply(effect, observe=True)
        with patch.object(self.lib, 'prep_step', side_effect=effect or self.hardware_effect):
            return prep.run(self.adapter, apply=True)

    def config(self, endpoint, offset):
        path = self.fixture.gpu if endpoint == 'gpu' else self.fixture.root
        return int.from_bytes((path/'config').read_bytes()[offset:offset+2], 'little')

    def test_full_preparation_order_native_readbacks_tls_only(self):
        original = self.fixture.bar.read_bytes()
        writes = []
        pwrite = os.pwrite
        def track(fd, data, offset):
            path = os.readlink(f'/proc/self/fd/{fd}')
            writes.append((path, offset, data))
            return pwrite(fd, data, offset)
        with patch.object(prep.os, 'pwrite', side_effect=track):
            result = self.apply()
        self.assertEqual(result['result'], 'PASS')
        self.assertTrue(result['cap_ready'])
        self.assertEqual(self.calls, [0, 1, 2, 3])
        self.assertEqual(result['completed_mmio'], 4)
        self.assertEqual(result['tls_verified'], ['gpu', 'root'])
        self.assertEqual([(p, o, len(d)) for p, o, d in writes],
                         [(str(self.fixture.gpu/'config'), 0x90, 2),
                          (str(self.fixture.root/'config'), 0xc0, 2)])
        self.assertEqual(self.config('gpu', 0x90), 0x22)
        self.assertEqual(self.config('root', 0xc0), 0x42)
        expected = bytearray(original)
        for offset, value in prep.STEPS:
            struct.pack_into('<I', expected, offset, value)
        self.assertEqual(self.fixture.bar.read_bytes(), expected)
        self.assertEqual(struct.unpack_from('<I', expected, 0x8c040)[0], 0x20085800)
        stages = [r for r in self.rows if r['event'] == 'snapshot' and r['role'] == 'after_mmio']
        self.assertEqual([r['links']['gpu']['cap2'] for r in stages], [2, 2, 6, 6])
        self.assertEqual([r['links']['gpu']['cap'] for r in stages],
                         [0x453c41, 0x453c41, 0x453c41, 0x453c42])
        final = next(r for r in self.rows if r['event'] == 'snapshot' and r['role'] == 'final_cap_ready')
        self.assertEqual(final['registers'], prep.ro.CAP_READY)
        self.assertFalse(result['speed_writes'])
        self.assertFalse(result['retrain'])
        self.assertLess(len(self.rows), 1000)
        self.assertGreater(result['safe_root_rows_suppressed'], 0)
        self.assertIn('DWORD PTR [rdi],esi', ' '.join(self.manifest['assembly']['ram_store32']))

    def test_dry_run_has_no_writer_or_config_write(self):
        before = self.fixture.bar.read_bytes()
        with patch.object(self.lib, 'prep_map', side_effect=AssertionError('no RW mapping')), \
             patch.object(prep.os, 'pwrite', side_effect=AssertionError('no config writes')):
            result = prep.run(self.adapter, apply=False)
        self.assertEqual(result['result'], 'PASS')
        self.assertFalse(result['cap_ready'])
        self.assertEqual(result['mmio_attempts'], [])
        self.assertEqual(self.fixture.bar.read_bytes(), before)

    def test_nonfactory_refuses_before_writer(self):
        ram = bytearray(self.fixture.bar.read_bytes())
        struct.pack_into('<I', ram, 0x8872c, 6)
        self.fixture.bar.write_bytes(ram)
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.calls, [])
        self.assertEqual(result['tls_attempts'], [])

    def test_link_loss_after_first_store_has_no_next_access_or_tls(self):
        def lose(handle, index):
            result = self.hardware_effect(handle, index)
            self.change(self.fixture.root/'config', 0xa2, 0x1041)
            return result
        read = self.lib.prep_read
        def guarded_read(handle, offset):
            if not self.config('root', 0xa2) & 0x2000:
                raise AssertionError('no unsafe readback')
            return read(handle, offset)
        with patch.object(self.lib, 'prep_read', side_effect=guarded_read):
            result = self.apply(lose)
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.calls, [0])
        self.assertEqual(result['completed_mmio'], 0)
        self.assertEqual(result['tls_attempts'], [])

    def test_unsafe_control_with_safe_sta_is_not_suppressed(self):
        def bad_control(handle, index):
            result = self.hardware_effect(handle, index)
            self.change(self.fixture.root/'config', 0xa0, 0x20)
            return result
        result = self.apply(bad_control)
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.calls, [0])
        self.assertTrue(any(r['event'] == 'root_check' and r['role'] == 'mmio_write_post'
                            and r['raw'] == 0x3041 for r in self.rows))

    def test_capability_appearing_early_stops_without_finishing_prefix(self):
        def early(handle, index):
            result = self.hardware_effect(handle, index)
            self.change(self.fixture.gpu/'config', 0x8c, 6, 4)
            return result
        result = self.apply(early)
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.calls, [0])
        self.assertIn('capability stage', result['reason'])

    def test_delayed_early_capability_stops_before_next_prefix_write(self):
        def later(_seconds):
            self.change(self.fixture.gpu/'config', 0x8c, 6, 4)
        with patch.object(prep.time, 'sleep', side_effect=later):
            result = self.apply(observe=True)
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.calls, [0])
        self.assertIn('capability stage', result['reason'])

    def test_no_latch_after_third_step_stops_before_cap_speed(self):
        def no_latch(handle, index):
            self.calls.append(index)
            return self.native_step(handle, index)
        result = self.apply(no_latch)
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.calls, [0, 1, 2])
        self.assertEqual(result['tls_attempts'], [])

    def test_wrong_readback_stops_without_rewrite(self):
        def corrupt(handle, index):
            result = self.hardware_effect(handle, index)
            ram = bytearray(self.fixture.bar.read_bytes())
            struct.pack_into('<I', ram, prep.STEPS[index][0], 7)
            self.fixture.bar.write_bytes(ram)
            return result
        result = self.apply(corrupt)
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.calls, [0])

    def test_short_tls_write_is_terminal_no_root_tls_or_rollback(self):
        original = os.pwrite
        calls = []
        def short(fd, data, offset):
            calls.append(offset)
            return original(fd, data[:1], offset)
        with patch.object(prep.os, 'pwrite', side_effect=short):
            result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['completed_mmio'], 4)
        self.assertEqual(result['tls_attempts'], ['gpu'])
        self.assertEqual(calls, [0x90])
        self.assertEqual(self.config('root', 0xc0), 0x43)

    def test_tls_already_two_is_skipped_not_rewritten(self):
        self.change(self.fixture.gpu/'config', 0x90, 0x22)
        self.change(self.fixture.root/'config', 0xc0, 0x42)
        with patch.object(prep.os, 'pwrite', side_effect=AssertionError('TLS already set')):
            result = self.apply()
        self.assertEqual(result['result'], 'PASS')
        self.assertEqual(result['tls_attempts'], [])
        self.assertEqual(result['tls_verified'], ['gpu', 'root'])

    def test_aer_failure_blocks_next_store(self):
        def aer(handle, index):
            result = self.hardware_effect(handle, index)
            self.change(self.fixture.root/'config', 0x104, 1, 4)
            return result
        result = self.apply(aer)
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.calls, [0])
        self.assertIn('AER', result['reason'])

    def test_last_moment_bme_change_prevents_next_mmio(self):
        def emit(row):
            self.rows.append(row)
            if row['event'] == 'mmio_intent' and row['step'] == 1:
                self.change(self.fixture.gpu/'config', 4, 6)
        self.adapter.emit = emit
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.calls, [0])
        self.assertEqual(result['tls_attempts'], [])

    def test_driver_binding_before_tls_prevents_config_write(self):
        def emit(row):
            self.rows.append(row)
            if row['event'] == 'tls_intent':
                (self.fixture.gpu/'driver').mkdir()
        self.adapter.emit = emit
        with patch.object(prep.os, 'pwrite', side_effect=AssertionError('must not write')):
            result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.calls, [0, 1, 2, 3])
        self.assertEqual(result['tls_attempts'], [])

    def test_transactions_pending_blocks_preparation_without_clearing(self):
        self.change(self.fixture.gpu/'config', 0x6a, 0x20)
        result = self.apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.config('gpu', 0x6a), 0x20)

    def test_native_bad_old_value_consumes_permit(self):
        fd = os.open(self.fixture.bar, os.O_RDWR)
        handle = self.lib.prep_map(fd)
        try:
            ram = bytearray(self.fixture.bar.read_bytes())
            struct.pack_into('<I', ram, 0x8872c, 1)
            self.fixture.bar.write_bytes(ram)
            self.assertEqual(self.lib.prep_step(handle, 0), -2)
            struct.pack_into('<I', ram, 0x8872c, 0)
            self.fixture.bar.write_bytes(ram)
            self.assertEqual(self.lib.prep_step(handle, 0), -1)
            self.assertEqual(self.fixture.bar.read_bytes(), ram)
        finally:
            self.lib.prep_close(handle)
            os.close(fd)

    def test_native_wrong_order_poisoned_no_arbitrary_offset_interface(self):
        fd = os.open(self.fixture.bar, os.O_RDWR)
        handle = self.lib.prep_map(fd)
        try:
            original = self.fixture.bar.read_bytes()
            self.assertEqual(self.lib.prep_step(handle, 1), -1)
            self.assertEqual(self.lib.prep_step(handle, 0), -1)
            self.assertEqual(self.fixture.bar.read_bytes(), original)
        finally:
            self.lib.prep_close(handle)
            os.close(fd)


if __name__ == '__main__':
    unittest.main()
