"""RAM-only investigation of the legacy struct.pack_into MMIO write primitive.

No device, mapping, address, path or caller-owned buffer can be supplied.
The __index__ probe observes one conversion-time snapshot, not a bus trace.
See docs/linux-gen2-root-cause-investigation.md for the CPython source proof
that the clearing step also precedes conversion of ordinary integer arguments.
"""
import json
import platform
import struct
import sys


def word(value):
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError('expected an unsigned 32-bit integer')
    return value


def fields(value):
    value = word(value)
    return dict(speed_code=(value >> 18) & 3, directive=value & 15,
                active=bool(value & 16), bit29=bool(value & (1 << 29)),
                bit31=bool(value & (1 << 31)))


def observe_pack_into(old=0x20085800, target=0x20045800,
                      conversion_failure=False):
    """Reproduce the buffer mutation on a private bytearray only.

    A conversion callback makes the intermediate RAM contents visible without
    polling or hardware. It does not establish the actual T5600 instruction
    sequence, PCIe byte enables, duration or GPU latch behavior.
    """
    old, target = word(old), word(target)
    if type(conversion_failure) is not bool:
        raise ValueError('conversion_failure must be bool')
    prefix, suffix = b'PRE!', b'POST'
    buffer = bytearray(prefix + struct.pack('<I', old) + suffix)
    observed = []

    class ConversionProbe:
        def __index__(self):
            observed.append(struct.unpack_from('<I', buffer, 4)[0])
            if conversion_failure:
                raise ValueError('synthetic conversion failure')
            return target

    error = None
    try:
        struct.pack_into('<I', buffer, 4, ConversionProbe())
    except ValueError as exc:
        error = str(exc)
    return dict(ram_only=True, hardware_execution_allowed=False,
                old=old, target=target, xor=old ^ target,
                conversion_snapshots=observed,
                final=struct.unpack_from('<I', buffer, 4)[0],
                sentinels_preserved=(buffer[:4] == prefix and buffer[8:] == suffix),
                conversion_error=error,
                interpretation='RAM snapshots; not a hardware trace or command ACK')


def report():
    return dict(implementation=platform.python_implementation(),
                python=sys.version.split()[0], hardware_execution_allowed=False,
                cases=[observe_pack_into(target=v) for v in
                       (0x20045800, 0x20045801, 0x20085801)],
                fields={f'{v:08X}': fields(v) for v in
                        (0x20085800, 0, 0x20045800, 0x20045801, 0x20085801)},
                limitation='Source and RAM reproduction support a hidden clear; '
                           'the actual crash runtime and MMIO trace are unavailable')


if __name__ == '__main__':
    print(json.dumps(report(), indent=2))
