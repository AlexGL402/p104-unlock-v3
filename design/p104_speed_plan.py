"""Offline proposal only: no sysfs, mmap, CUDA, driver or write backend.

The returned transaction describes a future separately authorized same-speed
control. It is NOT an executable plan or permission to access hardware.
"""

BASELINE = {0x8872C: 6, 0x2241C: 0x81, 0x8841C: 0xC0B40500,
            0x8C2C0: 0x068711B3, 0x8C300: 0x040A00C0,
            0x8C040: 0x20085800, 0x8C1C0: 0x00020032, 0x88610: 0}
GPU = '0000:03:00.0'
ROOT = '0000:00:02.0'


def decode_status(raw):
    if not isinstance(raw, int) or not 0 <= raw < 0xFFFF:
        raise ValueError('invalid/unavailable 16-bit Link Status')
    return dict(raw=raw, speed=raw & 15, width=(raw >> 4) & 63,
                training=bool(raw & 0x800), slot_clock=bool(raw & 0x1000),
                dll_active=bool(raw & 0x2000), bandwidth_management=bool(raw & 0x4000),
                autonomous_bandwidth=bool(raw & 0x8000))


def decode_config_link(raw):
    if not isinstance(raw, int) or not 0 <= raw < 0xFFFFFFFF or raw >> 16 == 0xBADF:
        raise ValueError('invalid/unavailable BAR0 register')
    return dict(raw=raw, directive=raw & 15, active=bool(raw & 16),
                pascal_speed_code=(raw >> 18) & 3, bit11=bool(raw & 0x800),
                bit29=bool(raw & 0x20000000), bit31=bool(raw & 0x80000000))


def proposal(snapshot):
    """Validate supplied offline data and describe one FUTURE Gen1 control.

    Input is a structured snapshot, not a live-device handle. Its provenance and
    freshness cannot be established here; future hardware code must revalidate.
    """
    if snapshot['gpu'] != GPU or snapshot['root'] != ROOT:
        raise ValueError('GPU 0 / exact root only')
    if snapshot['driver_bound'] or snapshot['d_state'] != 0 or snapshot['command'] & 6 != 2:
        raise ValueError('require unbound D0, MSE on/BME off')
    if snapshot['registers'] != BASELINE:
        raise ValueError('require exact post-cap-speed register state')
    links = snapshot['links']
    if set(links) != {GPU, ROOT}:
        raise ValueError('exactly the two ends of GPU 0 link required')
    for dev, link in links.items():
        sta = decode_status(link['sta'])
        if sta['speed'] != 1 or sta['width'] != 4 or sta['training']:
            raise ValueError('require idle Gen1 x4')
        if link['ctl'] & 0x30 or link['ctl2'] & 15 != 2:
            raise ValueError('require no RL/Link Disable, both TLS=2')
        if link['cap'] & 0x100000 and not sta['dll_active']:
            raise ValueError('reported DLL must be active')
    if links[GPU]['cap'] != 0x00453C42 or links[GPU]['cap2'] != 6:
        raise ValueError('GPU capability must be consistent Gen2 x4')
    root = links[ROOT]
    if root['cap'] & 15 != 3 or root['cap'] >> 4 & 63 != 16 or root['cap2'] & 14 != 14:
        raise ValueError('root capability must match confirmed Gen3 x16')
    errors = snapshot['errors']
    if set(errors) != {GPU, ROOT}:
        raise ValueError('both-end error data required')
    for dev, status in errors.items():
        if status['uncor'] or status['cor'] or status['device_status'] & 0x2F:
            raise ValueError('error/pending transactions')
        if dev == ROOT and status['root_status'] & 0x7F:
            raise ValueError('root error status')
    old = BASELINE[0x8C040]
    command = (old & ~15) | 1
    assert command ^ old == 1
    assert (command & 0xC0000) == (old & 0xC0000)
    return dict(design_only=True, execution_allowed=False,
                requires_separate_hardware_authorization=True,
                requires_live_revalidation=True,
                readiness='blocked_pending_observability_review',
                negative_result_can_prove_ignored=False,
                current_50ms_polling_is_sufficient=False,
                experiment='same-speed internal CHANGE_SPEED control; not Gen2 attempt',
                transaction=dict(offset=0x8C040, old=old, new=command, changed_mask=1),
                max_write_attempts=1, root_retrains=0, recovery_writes=0,
                tls_hasd={dev: bool(link['ctl2'] & 0x20) for dev, link in links.items()},
                limitations=['self-clear alone does not prove LTSSM execution',
                             'control does not establish Gen2 safety or support'])


def classify_control_readback(raw):
    """Self-clearing command / read-only ACTIVE may vary; SPEED must not vary."""
    decoded = decode_config_link(raw)
    if (raw & ~0x11) != BASELINE[0x8C040]:
        raise ValueError('unexpected change outside command/ACTIVE; stop without rollback')
    return dict(command_pending=bool(raw & 1), active=decoded['active'],
                proves_execution=False, proves_gen2=False)
