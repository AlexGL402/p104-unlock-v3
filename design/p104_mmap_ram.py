"""RAM-only mapping adapter. No file descriptor, path or external address API.

The native library creates anonymous ordinary RAM, never a device mapping.
Readback is explicit; no hardware executor imports this module.
"""
import ctypes
import os
from pathlib import Path
import shlex
import subprocess
from design.p104_ram_compare import audit_assembly


def build(directory):
    source = Path(__file__).with_suffix('.c')
    binary = Path(directory) / 'p104_mmap_ram.so'
    command = shlex.split(os.environ.get('CC', 'cc')) + [
        '-std=c11', '-O2', '-fPIC', '-shared', '-fno-lto', '-fno-ipa-cp',
        '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    audit_assembly(binary)
    # Keep the GIL across short native accesses/close: no concurrent free.
    lib = ctypes.PyDLL(str(binary))
    pointer = ctypes.c_void_p
    lib.mapping_open.argtypes = [ctypes.c_size_t]
    lib.mapping_open.restype = pointer
    lib.mapping_close.argtypes = [pointer]
    lib.mapping_close.restype = ctypes.c_int
    lib.mapping_destroy.argtypes = [pointer]
    lib.mapping_destroy.restype = None
    lib.mapping_write.argtypes = [pointer, ctypes.c_uint64, ctypes.c_uint32]
    lib.mapping_write.restype = ctypes.c_int
    lib.mapping_read.argtypes = [pointer, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint32)]
    lib.mapping_read.restype = ctypes.c_int
    return lib, binary, command


def integer(value, maximum):
    if type(value) is not int:
        raise TypeError('exact int required')
    if not 0 <= value <= maximum:
        raise ValueError('integer outside allowed range')
    return value


class RamMapping:
    def __init__(self, library, length):
        if getattr(self, '_handle', None):
            raise ValueError('mapping already initialized')
        self._handle = None
        self._library = library
        integer(length, 16 * 1024 * 1024)
        if length < 4:
            raise ValueError('mapping too small')
        self._handle = library.mapping_open(length)
        if not self._handle:
            raise OSError('anonymous RAM mmap failed')

    def _offset(self, offset):
        integer(offset, 2**64 - 1)
        if not self._handle:
            raise ValueError('mapping closed')
        return offset

    def write(self, offset, value):
        offset = self._offset(offset)
        integer(value, 2**32 - 1)
        if self._library.mapping_write(self._handle, offset, value):
            raise ValueError('unaligned or out-of-bounds offset')
        # No implicit readback.

    def read(self, offset):
        offset = self._offset(offset)
        value = ctypes.c_uint32()
        if self._library.mapping_read(self._handle, offset, ctypes.byref(value)):
            raise ValueError('unaligned or out-of-bounds offset')
        return value.value

    def close(self):
        if self._handle:
            if self._library.mapping_close(self._handle):
                raise OSError('munmap failed')
            self._library.mapping_destroy(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def __del__(self):
        if getattr(self, '_handle', None):
            self._library.mapping_destroy(self._handle)
            self._handle = None


def main():
    import hashlib
    import json
    import tempfile
    with tempfile.TemporaryDirectory(prefix='p104-mmap-audit-') as directory:
        _, binary, command = build(directory)
        print(json.dumps(dict(ram_only=True, hardware_allowed=False,
            command=command, binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            source_sha256=hashlib.sha256(Path(__file__).with_suffix('.c').read_bytes()).hexdigest(),
            assembly=audit_assembly(binary)), indent=2))


if __name__ == '__main__':
    main()
