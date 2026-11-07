"""Offline build/disassembly gate for the fixed one-shot SPEED backend. No device I/O."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


def audit(binary):
    assembly = subprocess.run(['objdump', '-d', '-Mintel', str(binary)], check=True,
                              capture_output=True, text=True).stdout
    match = re.search(r'<ro_load32>:\n(.*?)(?=\n\s*\n|\n[0-9a-f]+ <)', assembly, re.S)
    if not match:
        raise RuntimeError('missing ro_load32; audit failed closed')
    rows = []
    for line in match[1].splitlines():
        parts = line.split('\t')
        if len(parts) >= 3:
            ins = ' '.join(parts[-1].split())
            if ins != 'endbr64':
                rows.append(ins)
            if ins == 'ret':
                break
    if rows != ['mov eax,DWORD PTR [rdi]', 'ret']:
        raise RuntimeError(f'unexpected native load: {rows}')
    symbols = subprocess.run(['nm', '-D', '--defined-only', str(binary)], check=True,
                             capture_output=True, text=True).stdout
    exports = {line.split()[-1] for line in symbols.splitlines() if line.split()}
    expected = {'ro_map', 'ro_read', 'ro_close', 'speed_map', 'speed_once',
                'speed_readback', 'speed_close'}
    if exports != expected:
        raise RuntimeError(f'unexpected exports: {exports}')
    match = re.search(r'<ram_store32>:\n(.*?)(?=\n\s*\n|\n[0-9a-f]+ <)', assembly, re.S)
    if not match:
        raise RuntimeError('missing store; audit failed closed')
    stores = []
    for line in match[1].splitlines():
        parts = line.split('\t')
        if len(parts) >= 3:
            ins = ' '.join(parts[-1].split())
            if ins != 'endbr64':
                stores.append(ins)
            if ins == 'ret':
                break
    if stores != ['mov DWORD PTR [rdi],esi', 'ret']:
        raise RuntimeError(f'unexpected native store: {stores}')
    return dict(load=rows, store=stores)


def build(directory):
    binary = Path(directory) / 'p104_speed_once.so'
    source = Path(__file__).with_name('p104_speed_once.c')
    command = shlex.split(os.environ.get('CC', 'cc')) + [
        '-std=c11', '-O2', '-fPIC', '-shared', '-fno-lto', '-fno-ipa-cp',
        '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    rows = audit(binary)
    manifest = dict(binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                    source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    readonly_source_sha256=hashlib.sha256(source.with_name('p104_bar0_ro.c').read_bytes()).hexdigest(),
                    assembly=rows, command=command, build_device_operations=False)
    binary.with_suffix('.json').write_text(json.dumps(manifest, indent=2)+'\n')
    return binary, manifest


def load(binary):
    binary = Path(binary).resolve(strict=True)
    manifest = json.loads(binary.with_suffix('.json').read_text())
    if manifest['binary_sha256'] != hashlib.sha256(binary.read_bytes()).hexdigest():
        raise ValueError('native binary hash mismatch')
    source = Path(__file__).with_name('p104_speed_once.c')
    if manifest['source_sha256'] != hashlib.sha256(source.read_bytes()).hexdigest():
        raise ValueError('native source hash mismatch; rebuild required')
    if manifest['readonly_source_sha256'] != hashlib.sha256(source.with_name('p104_bar0_ro.c').read_bytes()).hexdigest():
        raise ValueError('read-only source changed; rebuild required')
    audit(binary)
    lib = ctypes.PyDLL(str(binary), use_errno=True)  # Keep GIL across access/lifetime calls.
    lib.ro_map.argtypes = [ctypes.c_int]
    lib.ro_map.restype = ctypes.c_void_p
    lib.ro_read.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    lib.ro_read.restype = ctypes.c_uint64
    lib.ro_close.argtypes = [ctypes.c_void_p]
    lib.ro_close.restype = ctypes.c_int
    lib.speed_map.argtypes = [ctypes.c_int]
    lib.speed_map.restype = ctypes.c_void_p
    lib.speed_once.argtypes = [ctypes.c_void_p]
    lib.speed_once.restype = ctypes.c_int
    lib.speed_readback.argtypes = [ctypes.c_void_p]
    lib.speed_readback.restype = ctypes.c_uint64
    lib.speed_close.argtypes = [ctypes.c_void_p]
    lib.speed_close.restype = ctypes.c_int
    return lib, manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', type=Path, required=True)
    args = p.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    _, manifest = build(args.directory)
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
