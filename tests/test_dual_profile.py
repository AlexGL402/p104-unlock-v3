import ast
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pcie_dual_profile as p
import pcie_dual_sampler as d
from test_dual_sampler import Root, Clock


@contextlib.contextmanager
def gpu_fixture(style='reference', enabled=True, limit=1000):
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / d.ROOT / d.GPU
        path.mkdir(parents=True)
        config = bytearray(256)
        config[0:4] = (0x1B8710DE).to_bytes(4, 'little')
        config[4:6] = (2).to_bytes(2, 'little')
        config[0x34] = 0x60
        config[0x60] = 1
        (path / 'config').write_bytes(config)
        (path / 'power_state').write_text('D0\n')
        (path / 'resource').write_text('40000000 40ffffff 200\n')
        resource = bytearray(0x90000)
        resource[0x8C040:0x8C044] = (0x20085800).to_bytes(4, 'little')
        (path / 'resource0').write_bytes(resource)
        clock, root = Clock(), Root()
        prof = p.Profiler(enabled, limit, clock)
        with patch.object(d, 'GPU_PATH', path):
            gpu = p.ProfileGPU(root, prof, style, clock)
            try:
                gpu.preflight()
                prof.cycle = lambda: 1
                prof.active = True
                yield path, clock, root, prof, gpu
            finally:
                gpu.close()


class DualProfileTests(unittest.TestCase):
    def test_six_root_and_driver_checks_both_styles(self):
        for style in ('reference', 'fresh-fast'):
            with gpu_fixture(style) as (_, _, root, prof, gpu):
                before = root.reads
                start, end, raw = gpu.read_gpu()
                gpu.postcheck()
                report = p.profile_report(prof)
                self.assertEqual(root.reads - before, 6)
                self.assertEqual(report['calls_per_complete_cycle']['guard']['min'], 6)
                self.assertEqual(report['calls_per_complete_cycle']['driver_binding']['min'], 6)
                for phase in ('power_state', 'config_identity', 'config_command', 'config_pmcsr'):
                    self.assertEqual(report['calls_per_complete_cycle'][phase]['min'], 1)
                self.assertEqual(raw, 0x20085800)
                mmio = next(row for row in prof.rows() if row[0] == p.CODES['mmio_load'])
                self.assertEqual(mmio[1:3], (start, end))
                self.assertEqual(report['complete_cycle_count'], 1)
                self.assertGreaterEqual(report['cycle_residual']['max_ns'], 0)

    def test_additive_partition_uses_same_cycle_not_summed_medians(self):
        prof = p.Profiler(True, 20, Clock())
        prof.active = True
        prof.cycle = lambda: 1
        cycle = prof.begin('cycle_total')
        for phase in p.TOP_LEVEL:
            token = prof.begin(phase)
            prof.finish(token)
        prof.finish(cycle)
        report = p.profile_report(prof)
        spans = {row[0]: row[2] - row[1] for row in prof.rows()}
        residual = spans[p.CODES['cycle_total']] - sum(spans[p.CODES[name]] for name in p.TOP_LEVEL)
        self.assertEqual(report['cycle_residual']['p50_ns'], residual)

    def test_binding_and_power_changes_not_cached(self):
        for style in ('reference', 'fresh-fast'):
            with gpu_fixture(style) as (path, _, _, _, gpu):
                gpu.read_gpu()
                gpu.postcheck()
                (path / 'driver').symlink_to(path / 'nvidia')
                with self.assertRaisesRegex(d.Stop, 'driver is bound'):
                    gpu.read_gpu()
                self.assertEqual(gpu.loads, 1)
                (path / 'driver').unlink()
                (path / 'power_state').write_text('D3hot\n')
                with self.assertRaisesRegex(d.Stop, 'D0'):
                    gpu.read_gpu()
                self.assertEqual(gpu.loads, 1)

    def test_identity_command_pmcsr_remain_fresh(self):
        for offset, value in ((0, 0xFFFFFFFF), (4, 6), (0x64, 3)):
            with gpu_fixture('fresh-fast') as (path, _, _, _, gpu):
                with (path / 'config').open('r+b') as config:
                    config.seek(offset)
                    config.write(value.to_bytes(4 if offset == 0 else 2, 'little'))
                with self.assertRaises(d.Stop):
                    gpu.read_gpu()
                self.assertEqual(gpu.loads, 0)

    def test_cached_view_does_not_cache_register_value(self):
        with gpu_fixture('fresh-fast') as (path, _, _, _, gpu):
            self.assertEqual(gpu.read_gpu()[2], 0x20085800)
            gpu.postcheck()
            with (path / 'resource0').open('r+b') as resource:
                resource.seek(0x8C040)
                resource.write((0x20085810).to_bytes(4, 'little'))
                resource.flush()
            self.assertEqual(gpu.read_gpu()[2], 0x20085810)
            gpu.postcheck()

    def test_training_or_inactive_dll_stops_mmio_both_styles(self):
        for style in ('reference', 'fresh-fast'):
            for status, exc in ((0x1841, d.Skip), (0x1041, d.Stop)):
                with gpu_fixture(style) as (_, _, root, prof, gpu):
                    root.last = status
                    with self.assertRaises(exc):
                        gpu.read_gpu()
                    self.assertEqual(gpu.loads, 0)
                    self.assertEqual(p.profile_report(prof)['complete_cycle_count'], 0)

    def test_midcycle_root_and_driver_changes_prevent_load(self):
        for style in ('reference', 'fresh-fast'):
            with gpu_fixture(style) as (path, _, root, _, gpu):
                root.values = iter((0x3041, 0x1841))
                with self.assertRaises(d.Skip):
                    gpu.read_gpu()
                self.assertEqual(gpu.loads, 0)
            with gpu_fixture(style) as (path, _, _, _, gpu):
                probe = gpu._driver_probe
                count = 0
                def rebinding():
                    nonlocal count
                    count += 1
                    if count == 5:
                        (path / 'driver').symlink_to(path / 'nvidia')
                    return probe()
                gpu._driver_probe = rebinding
                with self.assertRaises(d.Stop):
                    gpu.read_gpu()
                self.assertEqual(gpu.loads, 0)

    def test_fresh_power_file_open_each_cycle_readonly(self):
        with gpu_fixture('fresh-fast') as (_, _, _, _, gpu):
            real_open, real_close = os.open, os.close
            opens, closes = [], []
            def opening(path, flags):
                self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
                self.assertEqual(path, gpu.power_path)
                fd = real_open(path, flags)
                opens.append(fd)
                return fd
            def closing(fd):
                closes.append(fd)
                return real_close(fd)
            with patch.object(p.os, 'open', side_effect=opening), patch.object(p.os, 'close', side_effect=closing):
                for _ in range(2):
                    gpu.read_gpu()
                    gpu.postcheck()
            self.assertEqual(len(opens), 2)
            self.assertEqual(opens, closes)

    def test_profile_budget_consumed_before_operation(self):
        with gpu_fixture('fresh-fast', limit=1) as (_, _, root, prof, gpu):
            before = root.reads
            with self.assertRaisesRegex(d.End, 'profile_limit'):
                gpu.read_gpu()
            self.assertEqual(gpu.loads, 0)
            self.assertEqual(root.reads, before)
            self.assertEqual(prof.count, 1)
            self.assertEqual(len(prof.buffer), p.SPAN.size)

    def test_collection_profile_failure_and_no_fsync(self):
        with gpu_fixture('fresh-fast') as (_, clock, root, prof, gpu):
            result = d.Collection(root.read_status, gpu, 5000, 100, clock)
            prof.cycle = lambda: result.cycle
            with patch.object(p.os, 'fsync', side_effect=AssertionError('hot fsync')):
                result.run()
            self.assertGreater(gpu.loads, 0)
            self.assertLessEqual(prof.count, prof.limit)
            self.assertGreater(p.profile_report(prof)['complete_cycle_count'], 0)

    def test_no_profile_uses_zero_profile_ram(self):
        with gpu_fixture('fresh-fast', enabled=False) as (_, _, root, prof, gpu):
            before = root.reads
            gpu.read_gpu()
            gpu.postcheck()
            self.assertEqual(root.reads - before, 6)
            self.assertEqual(prof.count, 0)
            self.assertEqual(len(prof.buffer), 0)

    def test_calibration_and_cli(self):
        self.assertEqual(p.timer_calibration(Clock(), 3)['p50_ns'], 10)
        for args in (['--apply'], ['--duration', 'nan'], ['--io-style', 'unsafe'],
                     ['--max-profile-spans', '1000001'], ['--max-records', '0']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                p.arguments(args)
        self.assertFalse(p.arguments([]).profile)

    def test_main_profile_jsonl_with_regular_files_only(self):
        with gpu_fixture('fresh-fast') as (path, _, _, _, _):
            output = path / 'profile-results'
            with patch.object(p, 'RootConfig', return_value=Root()), \
                 patch.object(d, 'LOCK_PATH', path / 'test.lock'), \
                 contextlib.redirect_stdout(io.StringIO()):
                rc = p.main(['--duration', '.01', '--max-records', '50', '--profile',
                             '--io-style', 'fresh-fast', '--output-dir', str(output)])
            self.assertEqual(rc, 0)
            rows = [json.loads(line) for line in next(output.glob('*.jsonl')).read_text().splitlines()]
            self.assertEqual(rows[-1]['event'], 'summary')
            self.assertTrue(rows[-1]['profile']['enabled'])
            self.assertTrue(any(row['event'] == 'profile_span' for row in rows))
            self.assertFalse(rows[0]['hardware_writes'])
            self.assertTrue(rows[0]['unchanged_check_frequency'])

    def test_profile_report_handles_multiple_cycles_without_nested_double_count(self):
        prof = p.Profiler(True, 100, Clock())
        prof.active = True
        for cycle in (1, 2):
            prof.cycle = lambda value=cycle: value
            total = prof.begin('cycle_total')
            for phase in p.TOP_LEVEL:
                parent = prof.begin(phase)
                if phase == 'state_checks':
                    child = prof.begin('driver_binding')
                    prof.finish(child)
                prof.finish(parent)
            prof.finish(total)
        report = p.profile_report(prof)
        self.assertEqual(report['complete_cycle_count'], 2)
        self.assertEqual(report['calls_per_complete_cycle']['driver_binding']['max'], 1)
        self.assertGreaterEqual(report['cycle_residual']['p50_ns'], 0)

    def test_standalone_has_no_writer_or_state_check_decimation(self):
        text = Path(p.__file__).read_text()
        tree = ast.parse(text)
        attrs = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertFalse({'pwrite', 'ioctl', 'system'} & attrs)
        self.assertNotIn('O_RDWR', text)
        self.assertFalse(any(isinstance(target, ast.Attribute) and target.attr == 'value'
                             for node in ast.walk(tree) if isinstance(node, ast.Assign)
                             for target in node.targets))


if __name__ == '__main__':
    unittest.main()
