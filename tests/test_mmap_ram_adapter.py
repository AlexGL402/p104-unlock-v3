"""No devices: anonymous RAM, page-write traps, and final-binary disassembly."""
import ctypes
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from design import p104_mmap_ram as adapter


class MmapAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='p104-mmap-ram-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.lib, cls.binary, cls.command = adapter.build(cls.temp.name)

    def test_final_binary_scalar_assembly(self):
        result = adapter.audit_assembly(self.binary)
        self.assertIn('ram_store32', result)
        # Also audit the watched executable: same source, different final binary.
        harness = Path(self.temp.name) / 'watch'
        command = self.command[:]
        command.remove('-shared')
        command[command.index(str(Path(adapter.__file__).with_suffix('.c')))] = str(
            Path(adapter.__file__).with_name('p104_mmap_watch.c'))
        command[-1] = str(harness)
        subprocess.run(command, check=True, capture_output=True, text=True)
        adapter.audit_assembly(harness)
        result = subprocess.run([str(harness)], check=True, capture_output=True,
                                text=True, timeout=20)
        self.assertIn('open/close=0 stores; write=1', result.stdout)
        self.assertIn('negative control=2', result.stdout)

    def test_full_mapping_lifecycle_and_boundaries(self):
        with adapter.RamMapping(self.lib, 0x90000) as mapping:
            with self.assertRaises(ValueError):
                mapping.__init__(self.lib, 8)
            for offset in (0, 0x8c040, 0x8fffc):
                for value in (0, 0xffffffff, 0x20045800, 0x20085801):
                    self.assertIsNone(mapping.write(offset, value))
                    self.assertEqual(mapping.read(offset), value)
            for offset in (1, 0x8c041, 0x8fffd, 0x90000, -1, 2**64):
                with self.assertRaises(ValueError):
                    mapping.write(offset, 1)
                with self.assertRaises(ValueError):
                    mapping.read(offset)
            for value in (-1, 2**32, True, None, 1.0):
                previous = mapping.read(0)
                with self.assertRaises((ValueError, TypeError)):
                    mapping.write(0, value)
                self.assertEqual(mapping.read(0), previous)
        mapping.close()
        with self.assertRaises(ValueError):
            mapping.read(0)
        with self.assertRaises(ValueError):
            mapping.write(0, 1)

    def test_no_conversion_callbacks(self):
        class Index:
            def __index__(self):
                raise AssertionError('must not be invoked')
        for length in (0, 3, 16*1024*1024+1, -1, True, Index()):
            with self.assertRaises((ValueError, TypeError)):
                adapter.RamMapping(self.lib, length)
        with adapter.RamMapping(self.lib, 8) as m:
            for offset in (True, Index(), 1.0):
                with self.assertRaises(TypeError):
                    m.read(offset)
            with self.assertRaises(TypeError):
                m.write(0, Index())

    def test_mapping_allocation_failure(self):
        with patch.object(self.lib, 'mapping_open', return_value=None):
            with self.assertRaises(OSError):
                adapter.RamMapping(self.lib, 8)

    def test_native_rejects_null_handles_and_bounds(self):
        output = ctypes.c_uint32()
        self.assertEqual(self.lib.mapping_write(None, 0, 1), -1)
        self.assertEqual(self.lib.mapping_read(None, 0, ctypes.byref(output)), -1)
        self.assertEqual(self.lib.mapping_close(None), -1)
        self.lib.mapping_destroy(None)


if __name__ == '__main__':
    unittest.main()
