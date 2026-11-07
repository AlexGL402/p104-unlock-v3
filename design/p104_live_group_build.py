"""Build/compare the Windows recipe and audit native DWORD accesses. No device I/O."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from design.p104_ram_compare import audit_assembly

GROUP = ((0x8872C, 0xF, 6), (0x2241C, 0x81, 0x81), (0x8841C, 0x6000, 0),
         (0x8C2C0, 4, 0), (0x8C300, 0x10, 0), (0x8C040, 0xC0000, 0x40000),
         (0x8C1C0, 0xF0000, 0x20000), (0x88610, 0x1001, 1))
ROOT = Path(__file__).resolve().parents[1]


def verify_windows():
    source = (ROOT/'src/P104Gen2x4.cs').read_text()
    constants = {k: int(v, 16) for k, v in re.findall(r'const uint (OFF_\w+) = (0x[0-9A-Fa-f]+);', source)}
    body = source.split('static bool ApplyLiveGroup(string why)', 1)[1].split('return ok;', 1)[0]
    calls = re.findall(r'ok &= RmwVerify\(\w+, (OFF_\w+), (0x[0-9A-Fa-f]+|\d+)u, (0x[0-9A-Fa-f]+|\d+)u, "0x([0-9A-Fa-f]+)', body)
    recipe = tuple((constants[name], int(mask, 0), int(value, 0)) for name, mask, value, _ in calls)
    if recipe != GROUP or any(constants[name] != int(label, 16) for name, _, _, label in calls):
        raise RuntimeError('Windows recipe changed; manual source review required')
    rmw = source.split('static bool RmwVerify(', 1)[1].split('static bool ApplyLiveGroup', 1)[0]
    for fragment in ('uint nv = (old & ~mask) | (value & mask);', 'if (nv == old) return true;',
                     'Thread.Sleep(20);', 'bool ok = (rb & mask) == (value & mask);'):
        if fragment not in rmw:
            raise RuntimeError('Windows RMW semantics changed; manual review required')
    return recipe


def hashes():
    paths = ('design/p104_live_group.c', 'design/p104_bar0_ro.c',
             'design/p104_live_group_build.py', 'src/P104Gen2x4.cs')
    return {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}


def audit(binary):
    rows = audit_assembly(binary)
    output = subprocess.run(['nm', '-D', '--defined-only', str(binary)], check=True,
                            capture_output=True, text=True).stdout
    if {l.split()[-1] for l in output.splitlines() if l.split()} != {
            'ro_map', 'ro_read', 'ro_close', 'live_map', 'live_step', 'live_read', 'live_close'}:
        raise RuntimeError('unexpected native exports')
    return rows


def build(directory):
    recipe = verify_windows()
    directory = Path(directory)
    header = directory/'p104_live_recipe.h'
    header.write_text(''.join('{0x%08xu, 0x%08xu, 0x%08xu},\n' % r for r in recipe))
    binary = directory/'p104_live_group.so'
    command = shlex.split(os.environ.get('CC', 'cc')) + [
        '-std=c11', '-O2', '-fPIC', '-shared', '-fno-lto', '-fno-ipa-cp',
        '-Wall', '-Wextra', '-Werror',
        '-DP104_LIVE_RECIPE_HEADER='+json.dumps(str(header.resolve())),
        str(ROOT/'design/p104_live_group.c'), '-o', str(binary)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    manifest = dict(sources=hashes(), recipe=recipe, assembly=audit(binary), command=command,
                    binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), device_io=False)
    binary.with_suffix('.json').write_text(json.dumps(manifest, indent=2)+'\n')
    return binary, manifest


def load(binary):
    recipe = verify_windows()
    binary = Path(binary).resolve(strict=True)
    manifest = json.loads(binary.with_suffix('.json').read_text())
    if (manifest['sources'] != hashes() or tuple(map(tuple, manifest['recipe'])) != recipe or
        manifest['binary_sha256'] != hashlib.sha256(binary.read_bytes()).hexdigest()):
        raise ValueError('native/source/recipe mismatch; rebuild required')
    audit(binary)
    lib = ctypes.PyDLL(str(binary), use_errno=True)
    for name in ('ro_map', 'live_map'):
        getattr(lib, name).argtypes = [ctypes.c_int]
        getattr(lib, name).restype = ctypes.c_void_p
    for name in ('ro_read', 'live_read'):
        getattr(lib, name).argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        getattr(lib, name).restype = ctypes.c_uint64
    for name in ('ro_close', 'live_close'):
        getattr(lib, name).argtypes = [ctypes.c_void_p]
        getattr(lib, name).restype = ctypes.c_int
    lib.live_step.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint32]
    lib.live_step.restype = ctypes.c_int
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
