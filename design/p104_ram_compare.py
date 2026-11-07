"""Build/audit a private-RAM extension and compare three write primitives.

All destinations are newly allocated RAM. No sysfs, mmap, BAR, CUDA or external
address input is supported. Disassembly checks concern this build, not PCIe TLPs.
"""
import ctypes
import _ctypes
import _struct
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shlex
import struct
import subprocess
import sys
import sysconfig
import tempfile


def build(directory):
    if platform.machine() != 'x86_64':
        raise RuntimeError('only the audited x86-64 prototype is supported')
    source = Path(__file__).with_name('p104_ram_word.c')
    output = Path(directory) / ('_p104_ram_word' + sysconfig.get_config_var('EXT_SUFFIX'))
    # No shell. Do not inherit Python's optimization/LTO/compiler flags.
    command = shlex.split(os.environ.get('CC', 'cc')) + [
        '-std=c11', '-O2', '-fPIC', '-shared', '-fno-lto', '-fno-ipa-cp',
        '-Wall', '-Wextra', '-Werror', '-I' + sysconfig.get_path('include'),
        str(source), '-o', str(output)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    spec = importlib.util.spec_from_file_location('_p104_ram_word', output)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, output, command


def audit_assembly(binary):
    result = subprocess.run(['objdump', '-d', '-Mintel', str(binary)],
                            check=True, capture_output=True, text=True)
    instructions = {}
    for name in ('ram_store32', 'ram_load32'):
        match = re.search(r'<'+name+r'>:\n(.*?)(?=\n\s*\n|\n[0-9a-f]+ <)',
                          result.stdout, re.S)
        if not match:
            raise RuntimeError(f'{name}: missing exact symbol; audit failed closed')
        rows = []
        for line in match[1].splitlines():
            columns = line.split('\t')
            if len(columns) >= 3:
                ins = columns[-1].strip()
                if ins:
                    rows.append(ins)
                if ins.startswith('ret'):
                    break
        # This deliberately narrow gate must be reviewed on compiler changes.
        significant = [s for s in rows if s != 'endbr64']
        expected = ['mov    DWORD PTR [rdi],esi', 'ret'] if name == 'ram_store32' else [
            'mov    eax,DWORD PTR [rdi]', 'ret']
        if [' '.join(s.split()) for s in significant] != [' '.join(s.split()) for s in expected]:
            raise RuntimeError(f'{name}: unexpected generated instructions: {rows!r}')
        instructions[name] = rows
    return instructions


def compare(native, old=0x20085800, target=0x20045800):
    for value in (old, target):
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise ValueError('uint32 int required')
    packed = bytearray(struct.pack('<I', old))
    snapshots = []

    class Probe:
        def __index__(self):
            snapshots.append(struct.unpack('<I', packed)[0])
            return target

    struct.pack_into('<I', packed, 0, Probe())
    # Also compare the final result with a normal int conversion path.
    ordinary = bytearray(struct.pack('<I', old))
    struct.pack_into('<I', ordinary, 0, target)
    cell = ctypes.c_uint32(old)  # exclusively owned RAM; never a device address
    ctypes.c_uint32.from_address(ctypes.addressof(cell)).value = target
    owned = native.RamWord(old)
    owned.write(target)
    return dict(old=old, target=target, ram_only=True, hardware_allowed=False,
                pack_into=dict(conversion_snapshots=snapshots,
                               final=struct.unpack('<I', packed)[0],
                               ordinary_int_final=struct.unpack('<I', ordinary)[0]),
                ctypes=dict(final=cell.value, hidden_stores_measured=False),
                native=dict(final=owned.read(), explicit_readback=True,
                            bus_transactions_measured=False),
                limitation='Final RAM values do not certify ctypes/MMIO safety or command execution')


def main():
    with tempfile.TemporaryDirectory(prefix='p104-owned-ram-') as directory:
        native, binary, command = build(directory)
        instructions = audit_assembly(binary)
        runtime_artifacts = {}
        for name, path in (('interpreter', sys.executable),
                           ('_struct', getattr(_struct, '__file__', sys.executable)),
                           ('_ctypes', getattr(_ctypes, '__file__', sys.executable))):
            runtime_artifacts[name] = dict(path=path,
                builtin=(name != 'interpreter' and name in sys.builtin_module_names),
                sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())
        print(json.dumps(dict(python=sys.version, implementation=platform.python_implementation(),
                              runtime_artifacts=runtime_artifacts,
                              machine=platform.machine(), compiler_command=command,
                              binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                              source_sha256=hashlib.sha256(Path(__file__).with_name(
                                  'p104_ram_word.c').read_bytes()).hexdigest(),
                              audited_instructions=instructions,
                              cases=[compare(native, target=v) for v in
                                     (0x20045800, 0x20045801, 0x20085801)]), indent=2))


if __name__ == '__main__':
    main()
