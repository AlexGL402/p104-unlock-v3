import unittest
from design import p104_speed_plan as d


class SpeedDesignTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = dict(gpu=d.GPU, root=d.ROOT, driver_bound=False, d_state=0, command=2,
                             registers=dict(d.BASELINE), links={
                                 d.GPU: dict(cap=0x453C42, cap2=6, sta=0x1041, ctl=0x140, ctl2=2),
                                 d.ROOT: dict(cap=0x100103, cap2=14, sta=0x7041, ctl=0x40, ctl2=2)},
                             errors={d.GPU: dict(uncor=0, cor=0, device_status=0),
                                     d.ROOT: dict(uncor=0, cor=0, device_status=0, root_status=0)})

    def test_status_difference_is_reporting_not_speed_or_width(self):
        gpu, root = d.decode_status(0x1041), d.decode_status(0x7041)
        for key in ('speed', 'width', 'training', 'slot_clock'):
            self.assertEqual(gpu[key], root[key])
        self.assertTrue(root['dll_active'])
        self.assertTrue(root['bandwidth_management'])
        self.assertFalse(gpu['dll_active'])

    def test_proposal_changes_only_directive_and_cannot_execute(self):
        plan = d.proposal(self.snapshot)
        self.assertFalse(plan['execution_allowed'])
        self.assertTrue(plan['design_only'])
        self.assertEqual(plan['transaction']['new'], 0x20085801)
        self.assertEqual(plan['transaction']['changed_mask'], 1)
        self.assertEqual(plan['root_retrains'], 0)
        self.assertEqual(plan['max_write_attempts'], 1)

    def test_self_clear_does_not_prove_execution(self):
        for value in (0x20085800, 0x20085801, 0x20085810, 0x20085811):
            observation = d.classify_control_readback(value)
            self.assertFalse(observation['proves_execution'])
            self.assertFalse(observation['proves_gen2'])

    def test_both_gen2_candidates_and_width_directive_rejected_as_control_readback(self):
        for value in (0x20045800, 0x20045801, 0x20085802, 0x20085000):
            with self.assertRaises(ValueError):
                d.classify_control_readback(value)

    def test_register_baseline_cannot_be_repaired_by_planner(self):
        for off in d.BASELINE:
            self.setUp()
            self.snapshot['registers'][off] ^= 1
            with self.assertRaises(ValueError):
                d.proposal(self.snapshot)

    def test_hasd_is_reported_never_modified(self):
        self.snapshot['links'][d.GPU]['ctl2'] |= 0x20
        before = self.snapshot['links'][d.GPU]['ctl2']
        self.assertTrue(d.proposal(self.snapshot)['tls_hasd'][d.GPU])
        self.assertEqual(self.snapshot['links'][d.GPU]['ctl2'], before)

    def test_refuse_wrong_gpu_errors_and_wrong_tls(self):
        mutations = [lambda s: s.update(gpu='0000:04:00.0'),
                     lambda s: s['errors'][d.ROOT].update(cor=1),
                     lambda s: s['links'][d.ROOT].update(ctl2=1),
                     lambda s: s.update(driver_bound=True),
                     lambda s: s['links'][d.ROOT].update(sta=0x7841)]
        for mutate in mutations:
            self.setUp()
            mutate(self.snapshot)
            with self.assertRaises(ValueError):
                d.proposal(self.snapshot)


if __name__ == '__main__':
    unittest.main()
