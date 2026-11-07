import ast
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pcie_root_sampler as s


def synthetic(rows, elapsed=100, requested=100):
    buf = bytearray(len(rows) * s.SAMPLE.size)
    for index, row in enumerate(rows):
        s.SAMPLE.pack_into(buf, index * s.SAMPLE.size, *row)
    return s.Capture(buf, len(rows), 1000, elapsed, requested, 'duration', attempts=len(rows))


def config_file(directory, cap=0x90, vendor=0x8086, port_type=4):
    path = Path(directory) / 'root-config'
    data = bytearray(256)
    data[0:4] = (0x12340000 | vendor).to_bytes(4, 'little')
    data[0x34] = cap
    data[cap] = 0x10
    data[cap + 2:cap + 4] = ((port_type << 4) | 2).to_bytes(2, 'little')
    data[cap + 0x12:cap + 0x14] = (0x3041).to_bytes(2, 'little')
    path.write_bytes(data)
    return path


class RootSamplerTests(unittest.TestCase):
    def test_exact_metrics_and_quantiles(self):
        result = synthetic([(1, 4, 0x3041), (10, 15, 0x3041), (20, 30, 0x3041)], 35)
        report = s.report(result, 5)
        self.assertEqual(report['timing']['read'], dict(count=3, p50_ns=5, p99_ns=10, max_ns=10))
        self.assertEqual(report['timing']['cycle']['p50_ns'], 9.5)
        self.assertEqual(report['timing']['no_read_gap']['max_ns'], 6)
        self.assertEqual(report['timing']['conservative_gap']['max_ns'], 20)
        self.assertEqual(report['conservative_max_gap_including_boundaries_ns'], 20)
        self.assertEqual(report['no_read_gaps_above_threshold'], 1)
        self.assertEqual(report['uncovered_requested_tail_ns'], 65)
        self.assertFalse(report['observability']['ignored_command_proven'])

    def test_all_required_transitions(self):
        values = (0x3041, 0x3841, 0x3042, 0x7042, 0x5042, 0x5082)
        report = s.report(synthetic([(i * 10, i * 10 + 1, raw) for i, raw in enumerate(values)]), 5)
        self.assertEqual(report['field_changes'], dict(training=2, dllla=1, speed=1, width=1, lbms=1))
        self.assertEqual(report['raw_status_changes'], 5)

    def test_2000_stable_samples_do_not_prove_no_pulses(self):
        rows = [(i * 4000, i * 4000 + 3500, 0x3041) for i in range(2000)]
        report = s.report(synthetic(rows, 8_000_000, 8_000_000), 100_000)
        self.assertEqual(report['sample_count'], 2000)
        self.assertEqual(report['raw_status_changes'], 0)
        self.assertEqual(report['observability']['interpretation'], 'no_sampled_status_change')
        self.assertIsNone(report['observability']['minimum_device_pulse_duration_ns'])

    def test_bounded_buffer_stops_without_overwrite(self):
        ticks = iter((0, 1, 4, 10, 15, 20, 30, 35))
        reads = []
        result = s.capture(lambda: reads.append(1) or b'\x41\x30', 100, 3, ticks.__next__)
        self.assertEqual(result.stop_reason, 'sample_limit')
        self.assertEqual(len(result.buffer), 3 * 18)
        self.assertEqual(len(reads), 3)
        self.assertEqual(list(result.samples()), [(1, 4, 0x3041), (10, 15, 0x3041), (20, 30, 0x3041)])

    def test_duration_stops_new_reads(self):
        ticks = iter((0, 1, 4, 101, 102))
        result = s.capture(lambda: b'\x41\x30', 100, 3, ticks.__next__)
        self.assertEqual(result.stop_reason, 'duration')
        self.assertEqual(result.count, 1)
        self.assertFalse(s.report(result, 10)['read_completed_after_deadline'])

    def test_inflight_read_overrun_is_reported(self):
        ticks = iter((0, 1, 150, 151))
        result = s.capture(lambda: b'\x41\x30', 100, 1, ticks.__next__)
        self.assertTrue(s.report(result, 10)['read_completed_after_deadline'])

    def test_short_allones_or_exception_preserve_prior_samples(self):
        for failure in (b'\x41', b'\xff\xff', OSError('gone')):
            calls = iter((b'\x41\x30', failure))
            def reader():
                item = next(calls)
                if isinstance(item, Exception):
                    raise item
                return item
            ticks = iter(range(100))
            result = s.capture(reader, 100, 3, ticks.__next__)
            self.assertEqual(result.stop_reason, 'read_error')
            self.assertEqual(result.count, 1)
            self.assertEqual(s.report(result, 10)['failed_or_invalid_reads'], 1)

    def test_interrupt_retains_samples(self):
        calls = 0
        def reader():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt()
            return b'\x41\x30'
        result = s.capture(reader, 100, 3, iter(range(100)).__next__)
        self.assertEqual(result.stop_reason, 'interrupted')
        self.assertEqual(result.count, 1)

    def test_empty_and_one_sample(self):
        empty = s.report(synthetic([], 10), 1)
        self.assertIsNone(empty['timing']['read']['p50_ns'])
        self.assertEqual(empty['conservative_max_gap_including_boundaries_ns'], 10)
        one = s.report(synthetic([(2, 5, 0x3041)], 20), 1)
        self.assertEqual(one['timing']['cycle']['count'], 0)
        self.assertEqual(one['conservative_max_gap_including_boundaries_ns'], 18)

    def test_fixed_root_readonly_fd_and_exact_offset(self):
        with tempfile.TemporaryDirectory() as temp:
            path = config_file(temp)
            real_open, real_pread = os.open, os.pread
            accesses = []
            def opening(target, flags):
                self.assertEqual(target, path)
                self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
                return real_open(target, flags)
            def reading(fd, size, offset):
                accesses.append((size, offset))
                return real_pread(fd, size, offset)
            with patch.object(s, 'CONFIG', path), patch.object(s.os, 'open', side_effect=opening), \
                 patch.object(s.os, 'pread', side_effect=reading):
                device = s.RootConfig()
                try:
                    self.assertEqual(device.preflight()['link_status_offset'], '0xA2')
                    self.assertEqual(device.read_status(), b'\x41\x30')
                finally:
                    device.close()
            self.assertEqual(accesses[-1], (2, 0xA2))

    def test_wrong_vendor_type_or_cap_offset_refused(self):
        for kw in (dict(vendor=0x1234), dict(port_type=0), dict(cap=0x80)):
            with tempfile.TemporaryDirectory() as temp:
                path = config_file(temp, **kw)
                with patch.object(s, 'CONFIG', path):
                    device = s.RootConfig()
                    try:
                        with self.assertRaises(ValueError):
                            device.preflight()
                    finally:
                        device.close()

    def test_jsonl_samples_transitions_and_summary(self):
        result = synthetic([(0, 1, 0x3041), (10, 11, 0x7042)])
        with tempfile.TemporaryFile('w+', encoding='utf-8') as log:
            s.write_results(log, result, s.report(result, 100))
            log.seek(0)
            rows = [json.loads(line) for line in log]
        self.assertEqual([row['event'] for row in rows], ['sample', 'sample', 'status_change', 'summary'])
        self.assertEqual(set(rows[2]['changed_fields']), {'speed', 'lbms'})
        self.assertEqual(rows[0]['raw_hex'], '0x3041')

    def test_main_uses_injected_regular_file_not_hardware(self):
        with tempfile.TemporaryDirectory() as temp:
            path = config_file(temp)
            output = Path(temp) / 'results'
            with patch.object(s, 'CONFIG', path), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(s.main(['--duration', '.01', '--max-samples', '3', '--output-dir', str(output)]), 0)
                self.assertEqual(s.main(['--duration', '.01', '--max-samples', '3', '--output-dir', str(output)]), 0)
            files = list(output.glob('*.jsonl'))
            self.assertEqual(len(files), 2)
            rows = [json.loads(line) for line in files[0].read_text().splitlines()]
            self.assertFalse(rows[0]['hardware_writes'])
            self.assertFalse(rows[0]['bar0_access'])
            self.assertFalse(rows[0]['gpu1_access'])
            self.assertEqual(rows[-1]['stop_reason'], 'sample_limit')
            self.assertEqual(rows[-1]['sample_count'], 3)

    def test_preflight_failure_still_leaves_jsonl(self):
        with tempfile.TemporaryDirectory() as temp:
            path = config_file(temp, port_type=0)
            output = Path(temp) / 'results'
            with patch.object(s, 'CONFIG', path), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(s.main(['--output-dir', str(output)]), 1)
            rows = [json.loads(line) for line in next(output.glob('*.jsonl')).read_text().splitlines()]
            self.assertEqual(rows[-1]['event'], 'abort')

    def test_cli_bounds_and_no_write_options(self):
        for args in (['--duration', 'nan'], ['--duration', 'inf'], ['--duration', '11'],
                     ['--duration', '0'], ['--max-samples', '1000001'], ['--max-samples', '0'],
                     ['--gap-threshold-us', 'nan'], ['--apply'], ['--bdf', '0000:04:00.0']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                s.arguments(args)

    def test_standalone_source_has_no_hardware_write_or_mmio_api(self):
        tree = ast.parse(Path(s.__file__).read_text())
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertTrue({'pread', 'open'} <= calls)
        self.assertFalse({'pwrite', 'mmap', 'ioctl', 'system', 'run'} & calls)
        self.assertNotIn('p104_gen2_linux', Path(s.__file__).read_text())


if __name__ == '__main__':
    unittest.main()
