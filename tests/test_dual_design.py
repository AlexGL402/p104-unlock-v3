import unittest
from design import p104_dual_plan as p


class DualDesignTests(unittest.TestCase):
    def test_root_gate(self):
        for raw, expected in ((0x3041, 'allow'), (0x7042, 'allow'),
                              (0x1841, 'skip_training'), (0x3841, 'skip_training'),
                              (0x1041, 'stop_dll_inactive'), (0x3000, 'stop_link_down'),
                              (0x3081, 'stop_unexpected_link'), (0xFFFF, 'stop_root_unavailable')):
            self.assertEqual(p.root_action(raw), expected)

    def test_driver_pm_and_command(self):
        p.validate_gpu(0x1B8710DE, 2, 0, False)
        for args in ((0x1B8710DE, 2, 0, True), (0xFFFFFFFF, 2, 0, False),
                     (0x1B8710DE, 6, 0, False), (0x1B8710DE, 0, 0, False),
                     (0x1B8710DE, 2, 3, False)):
            with self.assertRaises(ValueError):
                p.validate_gpu(*args)

    def test_known_register_decode_and_sentinels(self):
        self.assertEqual(p.decode_gpu(0x20085811), dict(active=True, directive=1, speed_code=2))
        for value in (0xFFFFFFFF, 0xBADF5040):
            with self.assertRaises(ValueError):
                p.decode_gpu(value)

    def test_no_atomicity_or_command_acceptance_claim(self):
        report = p.interpretation(100, 0, 100)
        self.assertFalse(report['atomic_snapshot'])
        self.assertFalse(report['ignored_command_proven'])
        self.assertFalse(report['sufficient_for_definitive_change_speed_acceptance'])


if __name__ == '__main__':
    unittest.main()
