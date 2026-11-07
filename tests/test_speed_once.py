"""New one-shot policy/native integration tests, never devices or old test suites."""
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import test_bar0_readonly as fixtures
import pcie_speed_once as speed
from design import p104_speed_build as native


class SpeedOnceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='p104-speed-policy-')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary, cls.manifest = native.build(cls.directory.name)
        cls.lib, _ = native.load(cls.binary)

    def setUp(self):
        # Reuse fixture DATA construction only; do not rerun old read-only tests.
        self.fixture = fixtures.Bar0ReadOnlyTests(methodName='runTest')
        self.fixture.lib = self.lib
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.rows = []
        self.adapter = speed.SpeedAdapter(self.lib, self.fixture.review, self.rows.append,
                                           sysfs=self.fixture.sysfs)
        self.addCleanup(self.adapter.close)

    def prepare(self):
        ram = bytearray(self.fixture.bar.read_bytes())
        for offset, value in speed.ro.CAP_READY.items():
            struct.pack_into('<I', ram, offset, value)  # plain fixture RAM
        self.fixture.bar.write_bytes(ram)
        change = self.fixture.change_config
        change(self.fixture.gpu/'config', 0x6c, 0x00453c42, 4)
        change(self.fixture.gpu/'config', 0x8c, 6, 4)
        change(self.fixture.gpu/'config', 0x90, 2)
        change(self.fixture.root/'config', 0xc0, 2)
        return bytes(ram)

    def run_apply(self):
        # No real-time wait in policy tests; observe_root is separately exercised.
        with patch.object(self.adapter, 'observe_root'):
            return speed.run(self.adapter, apply=True)

    def test_factory_refuses_before_rw_map_and_store(self):
        with patch.object(self.lib, 'speed_map', side_effect=AssertionError('must not map RW')):
            result = self.run_apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertFalse(result['store_attempted'])
        self.assertIn('baseline mismatch', result['reason'])

    def test_dry_run_requires_preparation_but_never_opens_writer(self):
        original = self.prepare()
        with patch.object(self.lib, 'speed_map', side_effect=AssertionError('no writer')):
            result = speed.run(self.adapter, apply=False)
        self.assertEqual(result['reason'], 'dry_run_ready_no_write')
        self.assertFalse(result['store_attempted'])
        self.assertEqual(self.fixture.bar.read_bytes(), original)

    def test_prepared_one_store_readback_no_other_dword_changes(self):
        original = self.prepare()
        calls = []
        store, readback = self.lib.speed_once, self.lib.speed_readback
        def apply(handle):
            calls.append('store')
            return store(handle)
        def read(handle):
            calls.append('readback')
            return readback(handle)
        with patch.object(self.lib, 'speed_once', side_effect=apply), \
             patch.object(self.lib, 'speed_readback', side_effect=read):
            result = self.run_apply()
        self.assertEqual(result['result'], 'PASS')
        self.assertEqual(calls, ['store', 'readback'])
        self.assertEqual(result['readback'], speed.NEW)
        self.assertFalse(result['gen2_observed'])
        self.assertFalse(result['gen2_unlocked_proven'])
        expected = bytearray(original)
        struct.pack_into('<I', expected, speed.OFFSET, speed.NEW)
        self.assertEqual(self.fixture.bar.read_bytes(), expected)
        self.assertEqual(speed.OLD ^ speed.NEW, speed.MASK)
        self.assertEqual(speed.NEW & 1, 0)
        self.assertEqual(self.manifest['assembly']['store'], ['mov DWORD PTR [rdi],esi', 'ret'])
        self.assertEqual(self.rows[-1]['event'], 'result')

    def test_missing_tls_or_capability_refuses_without_store(self):
        self.prepare()
        self.fixture.change_config(self.fixture.root/'config', 0xc0, 1)
        result = self.run_apply()
        self.assertFalse(result['store_attempted'])
        self.assertIn('TLS', result['reason'])

    def test_changed_extra_register_refuses_no_fallback(self):
        self.prepare()
        ram = bytearray(self.fixture.bar.read_bytes())
        struct.pack_into('<I', ram, 0x88610, 1)
        self.fixture.bar.write_bytes(ram)
        result = self.run_apply()
        self.assertFalse(result['store_attempted'])
        self.assertIn('baseline mismatch', result['reason'])

    def test_link_loss_after_store_has_no_readback_or_retry(self):
        self.prepare()
        store = self.lib.speed_once
        def lose_link(handle):
            rc = store(handle)
            self.fixture.change_config(self.fixture.root/'config', 0xa2, 0x1041)
            return rc
        with patch.object(self.lib, 'speed_once', side_effect=lose_link) as calls, \
             patch.object(self.lib, 'speed_readback', side_effect=AssertionError('unsafe endpoint read')):
            result = self.run_apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertTrue(result['store_call_returned'])
        self.assertIsNone(result['readback'])
        self.assertEqual(calls.call_count, 1)

    def test_wrong_readback_fails_without_rollback(self):
        self.prepare()
        with patch.object(self.lib, 'speed_readback', return_value=speed.OLD), \
             patch.object(self.lib, 'speed_once', wraps=self.lib.speed_once) as calls:
            result = self.run_apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertIn('readback mismatch', result['reason'])
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(struct.unpack_from('<I', self.fixture.bar.read_bytes(), speed.OFFSET)[0], speed.NEW)

    def test_native_old_value_guard_consumes_attempt_without_store(self):
        self.prepare()
        self.adapter.admit()
        self.adapter.map_writer()
        ram = bytearray(self.fixture.bar.read_bytes())
        struct.pack_into('<I', ram, speed.OFFSET, speed.OLD | 0x10)
        self.fixture.bar.write_bytes(ram)
        handle = self.adapter.write_mapping
        self.assertEqual(self.lib.speed_once(handle), -2)
        self.assertEqual(self.lib.speed_once(handle), -1)
        self.assertEqual(self.lib.speed_readback(handle), 2**64-1)
        self.assertEqual(self.fixture.bar.read_bytes(), ram)

    def test_native_writer_rejects_readonly_fd(self):
        fd = os.open(self.fixture.bar, os.O_RDONLY)
        try:
            self.assertFalse(self.lib.speed_map(fd))
        finally:
            os.close(fd)

    def test_native_success_cannot_repeat_even_when_value_restored(self):
        self.prepare()
        self.adapter.admit()
        self.adapter.map_writer()
        handle = self.adapter.write_mapping
        self.assertEqual(self.lib.speed_once(handle), 0)
        ram = bytearray(self.fixture.bar.read_bytes())
        struct.pack_into('<I', ram, speed.OFFSET, speed.OLD)
        self.fixture.bar.write_bytes(ram)
        self.assertEqual(self.lib.speed_once(handle), -1)
        self.assertEqual(self.fixture.bar.read_bytes(), ram)

    def test_post_store_gen2_is_valid_but_never_claims_cuda_stability(self):
        self.prepare()
        store = self.lib.speed_once
        def transition(handle):
            rc = store(handle)
            self.fixture.change_config(self.fixture.root/'config', 0xa2, 0x7042)
            self.fixture.change_config(self.fixture.gpu/'config', 0x72, 0x1042)
            return rc
        with patch.object(self.lib, 'speed_once', side_effect=transition):
            result = self.run_apply()
        self.assertEqual(result['result'], 'PASS')
        self.assertTrue(result['gen2_observed'])
        self.assertFalse(result['gen2_unlocked_proven'])

    def test_root_observation_is_bounded_and_stops_on_training(self):
        self.prepare()
        self.adapter.admit()
        self.adapter.attempted = True
        self.adapter.start = 0
        ticks = iter(i/10 for i in range(60))
        with patch.object(speed.time, 'monotonic', side_effect=lambda: next(ticks)):
            self.adapter.observe_root(0x3041)
        observation = next(row for row in self.rows if row['event'] == 'observation')
        self.assertGreater(observation['polls'], 0)
        self.fixture.change_config(self.fixture.root/'config', 0xa2, 0x3841)
        with self.assertRaises(speed.ro.Stop):
            self.adapter.observe_root(0x3041)

    def test_bound_driver_and_bme_never_issue_store(self):
        self.prepare()
        (self.fixture.gpu/'driver').mkdir()
        result = self.run_apply()
        self.assertFalse(result['store_attempted'])
        (self.fixture.gpu/'driver').rmdir()
        self.fixture.change_config(self.fixture.gpu/'config', 4, 6)
        result = self.run_apply()
        self.assertFalse(result['store_attempted'])

    def test_last_moment_link_or_command_change_prevents_store(self):
        self.prepare()
        def emit(row):
            self.rows.append(row)
            if row['event'] == 'store_intent':
                self.fixture.change_config(self.fixture.root/'config', 0xa2, 0x1041)
        self.adapter.emit = emit
        with patch.object(self.lib, 'speed_once', side_effect=AssertionError('must not store')):
            result = self.run_apply()
        self.assertFalse(result['store_attempted'])
        self.assertEqual(result['result'], 'FAIL')

    def test_new_aer_after_store_stops_without_second_store(self):
        self.prepare()
        original = self.lib.speed_once
        def aer(handle):
            rc = original(handle)
            self.fixture.change_config(self.fixture.root/'config', 0x104, 1, 4)
            return rc
        with patch.object(self.lib, 'speed_once', side_effect=aer) as calls:
            result = self.run_apply()
        self.assertEqual(result['result'], 'FAIL')
        self.assertIn('AER', result['reason'])
        self.assertEqual(calls.call_count, 1)


if __name__ == '__main__':
    unittest.main()
