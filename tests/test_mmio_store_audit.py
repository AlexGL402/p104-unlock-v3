"""Synthetic RAM tests only: no existing hardware tool is imported."""
import ast
import platform
from pathlib import Path
import unittest

from design import p104_mmio_store_audit as audit


class StoreAuditTests(unittest.TestCase):
    def test_bit_differences(self):
        for target, xor, speed, directive in (
            (0x20045800, 0xC0000, 1, 0),
            (0x20045801, 0xC0001, 1, 1),
            (0x20085801, 1, 2, 1),
        ):
            self.assertEqual(0x20085800 ^ target, xor)
            f = audit.fields(target)
            self.assertEqual((f['speed_code'], f['directive']), (speed, directive))
            self.assertTrue(f['bit29'])
            self.assertFalse(f['bit31'])

    @unittest.skipUnless(platform.python_implementation() == 'CPython', 'CPython-specific probe')
    def test_hidden_clear_precedes_each_candidate(self):
        for target in (0x20045800, 0x20045801, 0x20085801):
            r = audit.observe_pack_into(target=target)
            self.assertEqual(r['conversion_snapshots'], [0])
            self.assertEqual(r['final'], target)
            self.assertTrue(r['sentinels_preserved'])
            self.assertIsNone(r['conversion_error'])

    @unittest.skipUnless(platform.python_implementation() == 'CPython', 'CPython-specific probe')
    def test_conversion_failure_leaves_private_ram_cleared(self):
        r = audit.observe_pack_into(conversion_failure=True)
        self.assertEqual(r['conversion_snapshots'], [0])
        self.assertEqual(r['final'], 0)
        self.assertEqual(r['conversion_error'], 'synthetic conversion failure')
        self.assertTrue(r['sentinels_preserved'])

    def test_zero_has_neither_reported_width_bit(self):
        f = audit.fields(0)
        self.assertFalse(f['bit29'])
        self.assertFalse(f['bit31'])
        # Field decoding cannot itself prove that a GPU will fail.

    def test_invalid_words_and_external_buffers_rejected(self):
        for v in (-1, 2**32, True, '0x20045800', bytearray(4)):
            with self.assertRaises(ValueError):
                audit.observe_pack_into(target=v)
        with self.assertRaises(ValueError):
            audit.observe_pack_into(conversion_failure='yes')

    def test_report_is_design_only(self):
        r = audit.report()
        self.assertFalse(r['hardware_execution_allowed'])
        self.assertEqual(len(r['cases']), 3)
        for case in r['cases']:
            self.assertTrue(case['ram_only'])
            self.assertFalse(case['hardware_execution_allowed'])

    def test_imports_cannot_open_devices_or_load_hardware_backend(self):
        tree = ast.parse(Path(audit.__file__).read_text())
        imports = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
        self.assertEqual(imports, {'json', 'platform', 'struct', 'sys'})
        self.assertFalse(any(isinstance(n, ast.ImportFrom) for n in ast.walk(tree)))


if __name__ == '__main__':
    unittest.main()
