"""Offline native tests. Destinations are extension-owned or freshly allocated RAM."""
import ast
import ctypes
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from design import p104_ram_compare as comparison


class RamPrimitiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='p104-native-tests-')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.native, cls.binary, cls.command = comparison.build(cls.directory.name)

    def test_exact_native_load_store_assembly(self):
        asm = comparison.audit_assembly(self.binary)
        self.assertEqual(asm['ram_store32'][-1], 'ret')
        self.assertEqual(len([s for s in asm['ram_store32'] if s.startswith('mov ')]), 1)
        self.assertEqual(len([s for s in asm['ram_load32'] if s.startswith('mov ')]), 1)

    def test_read_write_all_bits_and_explicit_readback(self):
        word = self.native.RamWord()
        for v in (0, 0xFFFFFFFF, 0x20085800, 0x20045800, 0x20045801,
                  0x20085801, 0xAAAAAAAA, 0x55555555):
            self.assertIsNone(word.write(v))
            self.assertEqual(word.read(), v)
            self.assertEqual(word.read(), v)

    def test_words_are_owned_and_independent(self):
        a, b = self.native.RamWord(0x11223344), self.native.RamWord(0x55667788)
        a.write(0x20045800)
        self.assertEqual(b.read(), 0x55667788)
        b.write(0)
        self.assertEqual(a.read(), 0x20045800)

    def test_ctypes_wrapping_is_not_the_native_validation_contract(self):
        for value, wrapped in ((-1, 0xFFFFFFFF), (2**32, 0)):
            cell = ctypes.c_uint32(0x20085800)
            cell.value = value
            self.assertEqual(cell.value, wrapped)
            native = self.native.RamWord(0x20085800)
            with self.assertRaises(OverflowError):
                native.write(value)
            self.assertEqual(native.read(), 0x20085800)

    def test_conversion_rejected_before_destination_changes(self):
        word = self.native.RamWord(0x20085800)

        class Probe:
            called = False

            def __index__(self):
                self.called = True
                return 0x20045800

        probe = Probe()
        for value in (-1, 2**32, 2**100, True, 1.5, b'1234', None, probe):
            with self.assertRaises((TypeError, OverflowError)):
                word.write(value)
            self.assertEqual(word.read(), 0x20085800)
        self.assertFalse(probe.called)

    def test_no_external_address_buffer_or_mapping_interface(self):
        word = self.native.RamWord()
        for name in ('address', 'buffer', 'map', 'from_address', 'fileno', '__dict__'):
            self.assertFalse(hasattr(word, name), name)
        with self.assertRaises(TypeError):
            memoryview(word)
        with self.assertRaises(TypeError):
            self.native.RamWord(address=ctypes.addressof(ctypes.c_uint32()))
        with self.assertRaises(TypeError):
            self.native.RamWord(bytearray(4))

    def test_three_ram_methods_final_values_not_hardware_approval(self):
        for target in (0x20045800, 0x20045801, 0x20085801):
            r = comparison.compare(self.native, target=target)
            self.assertEqual(r['pack_into']['conversion_snapshots'], [0])
            self.assertEqual(r['pack_into']['ordinary_int_final'], target)
            self.assertEqual(r['ctypes']['final'], target)
            self.assertEqual(r['native']['final'], target)
            self.assertFalse(r['ctypes']['hidden_stores_measured'])
            self.assertFalse(r['native']['bus_transactions_measured'])
            self.assertFalse(r['hardware_allowed'])

    def test_normal_ram_sentinel_control(self):
        # Serialization into RAM remains legitimate; the error was its MMIO use.
        ram = bytearray(b'PRE!' + struct.pack('<I', 0x20085800) + b'POST')
        struct.pack_into('<I', ram, 4, 0x20045800)
        self.assertEqual(ram[:4], b'PRE!')
        self.assertEqual(ram[8:], b'POST')

    def test_assembly_gate_rejects_missing_symbol(self):
        result = type('Result', (), {'stdout': ''})()
        with patch.object(comparison.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'failed closed'):
                comparison.audit_assembly(self.binary)

    def test_assembly_gate_rejects_extra_destination_store(self):
        text = ('0001 <ram_store32>:\n  1:\t00\tmov    DWORD PTR [rdi],0x0\n'
                '  2:\t00\tmov    DWORD PTR [rdi],esi\n  3:\t00\tret\n\n')
        result = type('Result', (), {'stdout': text})()
        with patch.object(comparison.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'unexpected generated'):
                comparison.audit_assembly(self.binary)

    def test_assembly_gate_rejects_wrong_access_width(self):
        text = ('0001 <ram_store32>:\n  1:\t00\tmov    BYTE PTR [rdi],sil\n'
                '  2:\t00\tret\n\n')
        result = type('Result', (), {'stdout': text})()
        with patch.object(comparison.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'unexpected generated'):
                comparison.audit_assembly(self.binary)

    def test_source_has_no_device_api_or_writable_buffer_export(self):
        source = Path(comparison.__file__).with_name('p104_ram_word.c').read_text()
        for forbidden in ('mmap(', 'open(', 'Py_bf_getbuffer', '/sys/', '/dev/',
                          'PyLong_AsVoidPtr', 'PyObject_GetBuffer'):
            self.assertNotIn(forbidden, source)
        self.assertIn('_Static_assert(offsetof(RamWord, value) % 4 == 0', source)
        self.assertIn('"memory"', source)
        tree = ast.parse(Path(comparison.__file__).read_text())
        imports = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
        self.assertNotIn('mmap', imports)
        self.assertNotIn('p104_gen2_linux', imports)


if __name__ == '__main__':
    unittest.main()
