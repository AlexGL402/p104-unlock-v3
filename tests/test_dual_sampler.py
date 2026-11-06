import ast
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pcie_dual_sampler as s


class Clock:
    def __init__(self):
        self.t = 0
    def __call__(self):
        self.t += 10
        return self.t


class Root:
    def __init__(self, values=None):
        self.values = iter(values or [])
        self.last = 0x3041
        self.reads = 0
    def read_status(self):
        self.reads += 1
        self.last = next(self.values, self.last)
        if isinstance(self.last, Exception):
            raise self.last
        if isinstance(self.last, bytes):
            return self.last
        return self.last.to_bytes(2, 'little')
    def read(self, off, size):
        assert (off, size) == (0x9C, 4)
        return 0x100103
    def preflight(self):
        return dict(pcie_cap='0x90')
    def close(self):
        pass


class GPU(s.ReadOnlyGPU):
    """Pure fake, retaining the production root/driver gate, never any MMIO."""
    def __init__(self, clock):
        self.clock = clock
        self.guard_reader = lambda role=0: 0x3041
        self.permit_load = lambda: None
        self.bound = False
        self.loads = self.config_reads = 0
        self.value = 0x20085800
        self.hook = lambda: None
    def _driver_bound(self):
        return self.bound
    def read_gpu(self):
        self._guard()
        self.config_reads += 1  # Synthetic endpoint config access after gate.
        self._guard()
        self.permit_load()
        start = self.clock()
        self.loads += 1
        raw = self.value
        end = self.clock()
        self.hook()
        return start, end, raw
    def close(self):
        pass


class DualSamplerTests(unittest.TestCase):
    def collection(self, statuses=None, duration=2000, limit=100):
        self.clock, self.root = Clock(), Root(statuses)
        self.gpu = GPU(self.clock)
        return s.Collection(self.root.read_status, self.gpu, duration, limit, self.clock)

    def test_two_independent_channels_and_nonatomic_skew(self):
        result = self.collection().run()
        report = s.summary(result)
        self.assertGreater(report['root']['timing']['read']['count'], report['gpu']['timing']['read']['count'])
        self.assertGreater(report['gpu']['timing']['read']['count'], 0)
        self.assertEqual(report['root']['timing']['read']['p50_ns'], 10)
        self.assertEqual(report['gpu']['timing']['read']['p50_ns'], 10)
        self.assertGreater(report['root_end_to_gpu_start_skew']['max_ns'], 0)
        self.assertFalse(report['observability']['atomic_snapshot'])
        self.assertFalse(report['observability']['ignored_command_proven'])

    def test_training_skips_all_gpu_access_then_resumes(self):
        result = self.collection([0x1841, 0x1841, 0x3041]).run()
        rows = list(result.rows())
        self.assertEqual([row[0] for row in rows[:4]], [s.ROOT_ROW, s.SKIP_ROW, s.ROOT_ROW, s.SKIP_ROW])
        self.assertEqual(s.summary(result)['skips']['training_before_gpu'], 2)
        self.assertGreater(self.gpu.loads, 0)

    def test_dll_inactive_stops_without_endpoint_access(self):
        result = self.collection([0x1041]).run()
        self.assertEqual(self.gpu.config_reads, 0)
        self.assertEqual(self.gpu.loads, 0)
        self.assertIn('stop_dll_inactive', result.error)
        self.assertEqual(s.summary(result)['skips']['dll_inactive_before_gpu'], 1)

    def test_link_down_or_allones_stops(self):
        for raw in (0, 0x3000, 0xFFFF):
            result = self.collection([raw]).run()
            self.assertEqual(result.reason, 'stop')
            self.assertEqual(self.gpu.loads, 0)

    def test_driver_bound_has_no_gpu_cfg_or_load(self):
        result = self.collection()
        self.gpu.bound = True
        result.run()
        self.assertEqual(self.gpu.config_reads, 0)
        self.assertEqual(self.gpu.loads, 0)
        self.assertIn('driver is bound', result.error)

    def test_midcycle_training_prevents_load(self):
        result = self.collection([0x3041, 0x1841, 0x1041]).run()
        self.assertEqual(self.gpu.config_reads, 1)
        self.assertEqual(self.gpu.loads, 0)
        self.assertEqual(s.summary(result)['skips']['training_before_gpu'], 1)

    def test_unsafe_postcheck_marks_sequential_race_not_atomic_snapshot(self):
        result = self.collection([0x3041, 0x3041, 0x1841, 0x1041]).run()
        report = s.summary(result)
        self.assertEqual(self.gpu.loads, 1)
        self.assertEqual(report['gpu_with_unsafe_post_root_sample'], 1)
        self.assertEqual(report['skips']['training_after_gpu_uncertain_bracket'], 1)

    def test_bounded_storage_no_unrecorded_load(self):
        for limit in (1, 2, 3, 4, 5):
            result = self.collection(limit=limit).run()
            self.assertLessEqual(result.count, limit)
            self.assertEqual(len(result.buffer), s.ROW.size * limit)
            self.assertEqual(sum(row[0] == s.GPU_ROW for row in result.rows()), self.gpu.loads)
            self.assertEqual(s.summary(result)['overwrites'], 0)

    def test_full_buffer_training_marker_accounted_even_if_not_stored(self):
        result = self.collection([0x1841], limit=1).run()
        self.assertEqual(result.count, 1)
        self.assertEqual(result.skip_markers_not_stored, 1)
        self.assertEqual(s.summary(result)['skips']['training_before_gpu'], 1)
        self.assertEqual(result.reason, 'stop')

    def test_errors_and_mmio_sentinels_stop_with_partial_buffer(self):
        for raw in (b'\x41', OSError('root unavailable')):
            result = self.collection([raw]).run()
            self.assertEqual(result.reason, 'stop')
            self.assertEqual(self.gpu.loads, 0)
        for raw in (0xFFFFFFFF, 0xBADF5040):
            result = self.collection()
            self.gpu.value = raw
            result.run()
            self.assertEqual(result.reason, 'stop')
            self.assertEqual(self.gpu.loads, 1)

    def test_register_changes_are_recorded(self):
        result = self.collection(duration=500)
        self.gpu.hook = lambda: setattr(self.gpu, 'value', 0x20045811)
        result.run()
        report = s.summary(result)
        self.assertGreater(report['gpu']['changes']['active'], 0)
        self.assertGreater(report['gpu']['changes']['directive'], 0)
        self.assertGreater(report['gpu']['changes']['speed_code'], 0)

    def test_jsonl_and_no_fsync_in_capture(self):
        result = self.collection()
        with patch.object(s.os, 'fsync', side_effect=AssertionError('hot fsync')):
            result.run()
        with tempfile.TemporaryFile('w+', encoding='utf-8') as log:
            s.emit_capture(log, result, s.summary(result))
            log.seek(0)
            rows = [json.loads(line) for line in log]
        self.assertEqual(rows[-1]['event'], 'summary')
        self.assertEqual({row['channel'] for row in rows if row['event'] == 'sample'}, {'root', 'gpu'})

    def test_actual_readonly_page_mechanism_uses_regular_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / s.ROOT / s.GPU
            path.mkdir(parents=True)
            config = bytearray(256)
            config[0:4] = (0x1B8710DE).to_bytes(4, 'little')
            config[4:6] = (2).to_bytes(2, 'little')
            config[0x34] = 0x60
            config[0x60] = 1
            (path / 'config').write_bytes(config)
            (path / 'power_state').write_text('D0\n')
            (path / 'resource').write_text('0000000040000000 0000000040ffffff 0000000000000200\n')
            resource = bytearray(0x90000)
            resource[0x8C040:0x8C044] = (0x20085800).to_bytes(4, 'little')
            (path / 'resource0').write_bytes(resource)
            root = Root()
            real_open = s.os.open
            def ro_open(target, flags):
                self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
                self.assertIn(Path(target), (path / 'config', path / 'resource0'))
                return real_open(target, flags)
            with patch.object(s, 'GPU_PATH', path), patch.object(s.os, 'open', side_effect=ro_open):
                gpu = s.ReadOnlyGPU(root)
                try:
                    info = gpu.preflight()
                    self.assertEqual(info['mapping'], 'PROT_READ')
                    self.assertEqual(gpu.read_gpu()[2], 0x20085800)
                    self.assertEqual(gpu.loads, 1)
                    (path / 'driver').symlink_to(path / 'nvidia')
                    with self.assertRaisesRegex(s.Stop, 'driver is bound'):
                        gpu.read_gpu()
                    self.assertEqual(gpu.loads, 1)
                    (path / 'driver').unlink()
                    (path / 'power_state').write_text('D3hot\n')
                    with self.assertRaisesRegex(s.Stop, 'D0'):
                        gpu.read_gpu()
                    self.assertEqual(gpu.loads, 1)
                finally:
                    gpu.close()

    def test_bound_preflight_never_opens_gpu_config_or_bar(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / s.ROOT / s.GPU
            path.mkdir(parents=True)
            (path / 'driver').symlink_to(path / 'nvidia')
            with patch.object(s, 'GPU_PATH', path), patch.object(s.os, 'open', side_effect=AssertionError('GPU opened')):
                gpu = s.ReadOnlyGPU(Root())
                try:
                    with self.assertRaisesRegex(s.Stop, 'driver is bound'):
                        gpu.preflight()
                finally:
                    gpu.close()

    def test_main_jsonl_is_independent_and_fake_only(self):
        import time
        root, gpu = Root(), GPU(time.monotonic_ns)
        gpu.preflight = lambda: dict(mapping='synthetic, no device')
        with tempfile.TemporaryDirectory() as temp:
            args = ['--duration', '.01', '--max-records', '10', '--output-dir', temp]
            with patch.object(s, 'RootConfig', return_value=root), \
                 patch.object(s, 'ReadOnlyGPU', return_value=gpu), \
                 patch.object(s, 'LOCK_PATH', Path(temp) / 'test.lock'), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(s.main(args), 0)
            rows = [json.loads(line) for line in next(Path(temp).glob('*.jsonl')).read_text().splitlines()]
            self.assertFalse(rows[0]['hardware_writes'])
            self.assertFalse(rows[0]['gpu1_access'])
            self.assertEqual(rows[-1]['event'], 'summary')

    def test_main_refusal_keeps_abort_jsonl_without_mapping(self):
        root, gpu = Root(), GPU(Clock())
        gpu.preflight = lambda: (_ for _ in ()).throw(s.Stop('GPU 0 driver is bound'))
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(s, 'RootConfig', return_value=root), \
                 patch.object(s, 'ReadOnlyGPU', return_value=gpu), \
                 patch.object(s, 'LOCK_PATH', Path(temp) / 'test.lock'), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(s.main(['--output-dir', temp]), 1)
            rows = [json.loads(line) for line in next(Path(temp).glob('*.jsonl')).read_text().splitlines()]
            self.assertEqual(rows[-1]['event'], 'abort')
            self.assertEqual(gpu.loads, 0)

    def test_cli_bounds_and_absence_of_command_or_unbind_flags(self):
        for args in (['--duration', 'nan'], ['--duration', '0'], ['--duration', '11'],
                     ['--max-records', '1000001'], ['--apply'], ['--unbind'], ['--bdf', '0000:04:00.0']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                s.arguments(args)

    def test_source_has_no_config_write_or_executor_api(self):
        tree = ast.parse(Path(s.__file__).read_text())
        attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse({'pwrite', 'ioctl', 'system', 'run_command'} & attrs)
        self.assertNotIn('PROT_WRITE', Path(s.__file__).read_text())
        self.assertNotIn('O_RDWR', Path(s.__file__).read_text())
        assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
        self.assertFalse(any(isinstance(target, ast.Attribute) and target.attr == 'value'
                             for node in assignments for target in node.targets))


if __name__ == '__main__':
    unittest.main()
