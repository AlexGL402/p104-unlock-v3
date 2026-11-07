"""Offline build of the preparation backend; reuses the audited assembly gate."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from design.p104_ram_compare import audit_assembly


def hashes():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('p104_cap_ready.c', 'p104_bar0_ro.c')}


def audit(binary):
    rows = audit_assembly(binary)
    output = subprocess.run(['nm', '-D', '--defined-only', str(binary)],
                            check=True, capture_output=True, text=True).stdout
    symbols = {line.split()[-1] for line in output.splitlines() if line.split()}
    if symbols != {'ro_map', 'ro_read', 'ro_close', 'prep_map', 'prep_step', 'prep_read', 'prep_close'}:
        raise RuntimeError(f'unexpected native exports: {symbols}')
    return rows


def build(directory):
    binary = Path(directory)/'p104_cap_ready.so'
    source = Path(__file__).with_name('p104_cap_ready.c')
    command = shlex.split(os.environ.get('CC', 'cc')) + [
        '-std=c11', '-O2', '-fPIC', '-shared', '-fno-lto', '-fno-ipa-cp',
        '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    manifest = dict(binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                    sources=hashes(), assembly=audit(binary), command=command,
                    build_device_operations=False)
    binary.with_suffix('.json').write_text(json.dumps(manifest, indent=2)+'\n')
    return binary, manifest


def load(binary):
    binary = Path(binary).resolve(strict=True)
    manifest = json.loads(binary.with_suffix('.json').read_text())
    if manifest['binary_sha256'] != hashlib.sha256(binary.read_bytes()).hexdigest() or manifest['sources'] != hashes():
        raise ValueError('native/source hash mismatch; rebuild required')
    audit(binary)
    lib = ctypes.PyDLL(str(binary), use_errno=True)
    for name in ('ro_map', 'prep_map'):
        getattr(lib, name).argtypes = [ctypes.c_int]
        getattr(lib, name).restype = ctypes.c_void_p
    for name in ('ro_read', 'prep_read'):
        getattr(lib, name).argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        getattr(lib, name).restype = ctypes.c_uint64
    for name in ('ro_close', 'prep_close'):
        getattr(lib, name).argtypes = [ctypes.c_void_p]
        getattr(lib, name).restype = ctypes.c_int
    lib.prep_step.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    lib.prep_step.restype = ctypes.c_int
    return lib, manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', required=True, type=Path)
    args = p.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    _, manifest = build(args.directory)
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
