"""Offline timing/evidence assessment. No hardware or tracefs access.

Samples are bracketed read intervals in monotonic nanoseconds for ONE channel.
Do not mix root Training timestamps with BAR ACTIVE timestamps.
"""


def assess_channel(samples, window_start_ns, window_end_ns, pulse_min_ns=None):
    if window_end_ns <= window_start_ns:
        raise ValueError('invalid observation window')
    rows = list(samples)
    previous = window_start_ns
    gaps = []
    activity = False
    for row in rows:
        start, end = row['start_ns'], row['end_ns']
        if start < previous or end < start or end > window_end_ns:
            raise ValueError('overlapping/unordered/out-of-window read intervals')
        # The actual hardware read can fall anywhere inside its bracket.
        activity |= bool(row['active'])
        previous = end
    if rows:
        gaps.append(rows[0]['end_ns'] - window_start_ns)
        gaps.extend(b['end_ns'] - a['start_ns'] for a, b in zip(rows, rows[1:]))
        gaps.append(window_end_ns - rows[-1]['start_ns'])
    else:
        gaps.append(window_end_ns - window_start_ns)
    worst = max(gaps)
    bounded = pulse_min_ns is not None and pulse_min_ns > worst
    return dict(sample_count=len(rows), worst_case_gap_ns=worst,
                activity_seen=activity,
                documented_pulse_min_ns=pulse_min_ns,
                covers_documented_pulse=bool(rows) and bounded,
                ignored_command_proven=False,
                interpretation='activity_observed_not_causal_proof' if activity else
                'no_activity_with_declared_duration_bound' if bounded and rows else
                'inconclusive_no_activity',
                requires_side_effect_free_reads=True,
                lower_bound_must_be_independently_justified=True)


def assess_trace(event_available, matching_producer, complete_hardware_history,
                 losses, correlated_activity=False):
    if losses < 0:
        raise ValueError('invalid lost-event count')
    reliable_history = bool(event_available and matching_producer and
                            complete_hardware_history and losses == 0)
    return dict(usable_complete_history=reliable_history,
                activity_seen=bool(correlated_activity),
                ignored_command_proven=False,
                interpretation='activity_observed_not_causal_proof' if correlated_activity else
                'no_history_activity' if reliable_history else 'inconclusive_trace_silence')
