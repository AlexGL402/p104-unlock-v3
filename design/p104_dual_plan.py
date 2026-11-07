"""Offline policy for the read-only dual-channel observer; no device I/O."""
GPU = '0000:03:00.0'
ROOT = '0000:00:02.0'


def root_action(raw):
    if not isinstance(raw, int) or not 0 <= raw < 0xFFFF:
        return 'stop_root_unavailable'
    if raw & 0x800:
        return 'skip_training'  # Negotiated fields/DLL may be transient here.
    if not raw & 0x2000:
        return 'stop_dll_inactive'
    if raw & 15 == 0 or (raw >> 4) & 63 == 0:
        return 'stop_link_down'
    if raw & 15 not in (1, 2) or (raw >> 4) & 63 != 4:
        return 'stop_unexpected_link'
    return 'allow'


def validate_gpu(identity, command, pmcsr, driver_bound):
    if driver_bound:
        raise ValueError('GPU 0 has a driver; BAR0 access forbidden')
    if identity != 0x1B8710DE:
        raise ValueError('GPU 0 identity unavailable or unexpected')
    if command & 6 != 2:
        raise ValueError('GPU 0 requires MSE=1 and BME=0; no repair')
    if pmcsr & 3:
        raise ValueError('GPU 0 must already be D0; no wake/reset')


def decode_gpu(raw):
    if not isinstance(raw, int) or not 0 <= raw < 0xFFFFFFFF or raw >> 16 == 0xBADF:
        raise ValueError('GPU MMIO unavailable/sentinel')
    return dict(active=bool(raw & 0x10), directive=raw & 15, speed_code=(raw >> 18) & 3)


def interpretation(root_samples, gpu_samples, skipped):
    return dict(root_samples=root_samples, gpu_samples=gpu_samples, skipped_gpu_reads=skipped,
                atomic_snapshot=False, ignored_command_proven=False,
                complete_ltssm_history=False,
                sufficient_for_definitive_change_speed_acceptance=False,
                note='Per-channel gaps include safety skips; status silence is inconclusive.')
