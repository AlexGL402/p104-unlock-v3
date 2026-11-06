import unittest
from design.p104_observability import assess_channel, assess_trace


class ObservabilityTests(unittest.TestCase):
    def test_50ms_sampling_cannot_distinguish_absent_and_short_pulse(self):
        times = (0, 50_000_000, 100_000_000)
        never_active = [dict(start_ns=t, end_ns=t, active=False) for t in times]
        # A hypothetical 100us pulse at 5ms falls entirely between the reads.
        short_pulse = [dict(start_ns=t, end_ns=t, active=5_000_000 <= t < 5_100_000)
                       for t in times]
        self.assertEqual(never_active, short_pulse)
        report = assess_channel(short_pulse, 0, 100_000_000)
        self.assertEqual(report['worst_case_gap_ns'], 50_000_000)
        self.assertEqual(report['interpretation'], 'inconclusive_no_activity')
        self.assertFalse(report['ignored_command_proven'])

    def test_read_duration_and_boundary_blind_intervals_count(self):
        rows = [dict(start_ns=20, end_ns=25, active=False),
                dict(start_ns=60, end_ns=70, active=False)]
        report = assess_channel(rows, 0, 120)
        self.assertEqual(report['worst_case_gap_ns'], 60)

    def test_active_blind_during_training_cannot_borrow_root_samples(self):
        root = [dict(start_ns=t, end_ns=t, active=False) for t in (0, 10, 20, 30)]
        active = [root[0], root[-1]]
        self.assertEqual(assess_channel(root, 0, 30)['worst_case_gap_ns'], 10)
        self.assertEqual(assess_channel(active, 0, 30)['worst_case_gap_ns'], 30)

    def test_unmeasured_pulse_minimum_is_not_inferred_from_poll_interval(self):
        row = [dict(start_ns=0, end_ns=0, active=False), dict(start_ns=50, end_ns=50, active=False)]
        self.assertFalse(assess_channel(row, 0, 50)['covers_documented_pulse'])
        self.assertFalse(assess_channel(row, 0, 50, 50)['covers_documented_pulse'])
        self.assertTrue(assess_channel(row, 0, 50, 51)['covers_documented_pulse'])
        self.assertFalse(assess_channel(row, 0, 50, 51)['ignored_command_proven'])

    def test_activity_is_observation_not_proof_of_command_causality(self):
        report = assess_channel([dict(start_ns=10, end_ns=11, active=True)], 0, 20)
        self.assertEqual(report['interpretation'], 'activity_observed_not_causal_proof')

    def test_trace_event_name_alone_is_not_coverage(self):
        report = assess_trace(True, False, False, 0)
        self.assertEqual(report['interpretation'], 'inconclusive_trace_silence')
        self.assertFalse(report['usable_complete_history'])
        self.assertFalse(assess_trace(True, True, True, 1)['usable_complete_history'])
        self.assertTrue(assess_trace(True, True, True, 0)['usable_complete_history'])

    def test_empty_or_invalid_timings(self):
        self.assertEqual(assess_channel([], 0, 100)['interpretation'], 'inconclusive_no_activity')
        with self.assertRaises(ValueError):
            assess_channel([dict(start_ns=10, end_ns=5, active=False)], 0, 100)


if __name__ == '__main__':
    unittest.main()
