"""Fake sysfs and ordinary RAM-backed regular files only; never real PCI paths."""
import ctypes
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import platform
import signal
import struct
import tempfile
import unittest
from unittest.mock import patch

import pcie_bar0_readonly as probe
from design import p104_bar0_ro_build as native


def signal_worker(signal_number):
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.kill(os.getpid(), signal_number)


def stalled_worker():
    import time
    time.sleep(5)


def fixture_worker(connection, binary, review, sysfs):
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    lib, _ = native.load(binary)
    result = probe.run_once(probe.Adapter(lib, review, connection.send, sysfs=sysfs))
    connection.close()
    raise SystemExit(0 if result['result'] == 'PASS' else 1)


class Bar0ReadOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_dir = tempfile.TemporaryDirectory(prefix='p104-ro-build-')
        cls.addClassCleanup(cls.build_dir.cleanup)
        cls.binary, cls.manifest = native.build(cls.build_dir.name)
        cls.lib, _ = native.load(cls.binary)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='p104-fake-sysfs-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.sysfs = base/'sys/bus/pci/devices'
        self.sysfs.mkdir(parents=True)
        self.root = base/'devices'/probe.ROOT
        self.gpu = self.root/probe.GPU
        self.gpu.mkdir(parents=True)
        (self.sysfs/probe.ROOT).symlink_to(self.root, target_is_directory=True)
        (self.sysfs/probe.GPU).symlink_to(self.gpu, target_is_directory=True)
        root = bytearray(4096)
        gpu = bytearray(4096)
        def put(data, off, value, size):
            data[off:off+size] = value.to_bytes(size, 'little')
        for data, values in ((root, [(0, 0x3c028086, 4), (4, 6, 2), (8, 0x06040000, 4),
                (0x34, 0x90, 1), (0x90, 0x10, 1), (0x92, 0x42, 2),
                (0x9c, 0x100103, 4), (0xa2, 0x3041, 2), (0xbc, 0xe, 4),
                (0x100, 0x10001, 4)]),
                (gpu, [(0, 0x1b8710de, 4), (4, 2, 2), (0x10, 0x90000000, 4),
                (0x34, 0x40, 1), (0x40, 1, 1), (0x41, 0x60, 1),
                (0x60, 0x10, 1), (0x62, 2, 2), (0x6c, 0x00453c41, 4),
                (0x72, 0x1041, 2), (0x8c, 2, 4), (0x100, 0x10001, 4)])):
            for off, val, size in values:
                put(data, off, val, size)
        (self.root/'config').write_bytes(root)
        (self.gpu/'config').write_bytes(gpu)
        (self.root/'power_state').write_text('D0\n')
        (self.gpu/'power_state').write_text('D0\n')
        (self.gpu/'power').mkdir()
        (self.gpu/'power/runtime_status').write_text('active\n')
        (self.gpu/'resource').write_text('0000000090000000 0000000090ffffff 0000000000000200\n')
        ram = bytearray(b'\xa5'*0x90000)
        for off, value in probe.FACTORY.items():
            struct.pack_into('<I', ram, off, value)  # ordinary fixture RAM only
        self.bar = self.gpu/'resource0'
        self.bar.write_bytes(ram)
        self.initial = hashlib.sha256(ram).hexdigest()
        self.review = dict(gpu=probe.GPU, root=probe.ROOT,
            kernel_release=platform.release(), bar0_start=0x90000000,
            bar0_size=0x1000000, effective_type='UC', kernel_mapping_verified=True,
            conflicting_aliases_excluded=True, evidence='synthetic fixture, not hardware evidence')
        self.rows = []
        self.adapter = probe.Adapter(self.lib, self.review, self.rows.append, sysfs=self.sysfs)
        self.addCleanup(self.adapter.close)

    def change_config(self, path, offset, value, size=2):
        data = bytearray(path.read_bytes())
        data[offset:offset+size] = value.to_bytes(size, 'little')
        path.write_bytes(data)

    def result(self):
        return probe.run_once(self.adapter)

    def test_complete_native_readonly_path_no_destination_changes(self):
        operations = []
        original = os.open
        def opener(path, flags, *args, **kw):
            operations.append((Path(path), flags))
            return original(path, flags, *args, **kw)
        with patch.object(probe.os, 'open', side_effect=opener):
            result = self.result()
        self.assertEqual(result['result'], 'PASS')
        self.assertEqual(result['loads'], 8)
        self.assertEqual(hashlib.sha256(self.bar.read_bytes()).hexdigest(), self.initial)
        for _, flags in operations:
            self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
        self.assertEqual([p.name for p, _ in operations].count('resource0'), 1)
        self.assertFalse(any('04:00.0' in str(p) or '00:03.0' in str(p) for p, _ in operations))
        loads = [r for r in self.rows if r['event'] == 'dword']
        self.assertEqual(len(loads), 8)
        for r in loads:
            self.assertEqual(r['root_pre'], 0x3041)
            self.assertEqual(r['root_post'], 0x3041)
            self.assertFalse(r['atomic_snapshot'])
        self.assertFalse(hasattr(self.adapter, 'write'))
        self.assertEqual(self.manifest['assembly'], ['mov eax,DWORD PTR [rdi]', 'ret'])

    def test_isolated_native_worker_and_jsonl_pass(self):
        context = multiprocessing.get_context('spawn')
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=fixture_worker,
            args=(send, self.binary, self.review, self.sysfs))
        process.start()
        send.close()
        lines = []
        try:
            result = probe.supervise(process, receive,
                lambda row: lines.append(json.dumps(row)), 5)
            self.assertEqual(result, 0)
            rows = [json.loads(line) for line in lines]
            self.assertEqual(rows[-1]['result'], 'PASS')
            self.assertEqual(rows[-1]['loads'], 8)
        finally:
            if process.is_alive():
                process.kill()
                process.join()
            receive.close()

    def test_cap_ready_is_read_only_and_factory_never_auto_prepares(self):
        ram = bytearray(self.bar.read_bytes())
        for offset, value in probe.CAP_READY.items():
            struct.pack_into('<I', ram, offset, value)
        self.bar.write_bytes(ram)
        result = self.result()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['loads'], 1)
        self.adapter.profile = 'cap-ready'
        self.change_config(self.gpu/'config', 0x6c, 0x00453c42, 4)
        self.change_config(self.gpu/'config', 0x8c, 6, 4)
        previous = self.adapter.loads
        result = self.result()
        self.assertEqual(result['result'], 'PASS')
        self.assertEqual(result['loads']-previous, 8)
        self.assertEqual(self.bar.read_bytes(), ram)

    def test_bound_driver_refuses_before_endpoint_open(self):
        (self.gpu/'driver').symlink_to('/nonexistent/nvidia')
        result = self.result()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['loads'], 0)
        self.assertIn('bound', result['reason'])
        self.assertIsNone(self.adapter.gf)
        self.assertFalse(any(r['event'] == 'mapped' for r in self.rows))

    def test_training_dll_loss_speed_width_disable_stop_before_gpu(self):
        for raw in (0x3841, 0x1041, 0x3042, 0x3021, 0, 0xffff):
            with self.subTest(raw=raw):
                self.change_config(self.root/'config', 0xa2, raw)
                result = self.result()
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['loads'], 0)
        self.change_config(self.root/'config', 0xa2, 0x3041)
        self.change_config(self.root/'config', 0xa0, 0x10)
        self.assertEqual(self.result()['loads'], 0)

    def test_d0_runtime_command_identity_and_gpu_link_fail_closed(self):
        self.adapter.open()
        for name, value in [('power_state', 'D3hot'), ('power/runtime_status', 'suspended')]:
            path = self.gpu/name
            old = path.read_text()
            path.write_text(value)
            with self.assertRaises(probe.Stop):
                self.adapter.read(0x8c040)
            path.write_text(old)
        for off, bad, size in [(4, 0, 2), (4, 6, 2), (0x44, 3, 2),
                                (0, 0x123410de, 4), (0x72, 0x1081, 2)]:
            data = (self.gpu/'config').read_bytes()
            self.change_config(self.gpu/'config', off, bad, size)
            with self.assertRaises(probe.Stop):
                self.adapter.read(0x8c040)
            (self.gpu/'config').write_bytes(data)
        self.assertEqual(self.adapter.loads, 0)

    def test_resource_review_and_pci_bar_mismatches(self):
        for key, bad in [('effective_type', 'WC'), ('effective_type', 'WB'),
                         ('kernel_release', 'wrong'), ('bar0_start', 0),
                         ('conflicting_aliases_excluded', False), ('evidence', '')]:
            old = self.review[key]
            self.review[key] = bad
            self.assertEqual(self.result()['result'], 'FAIL')
            self.review[key] = old
        self.change_config(self.gpu/'config', 0x10, 0x91000000, 4)
        self.assertEqual(self.result()['loads'], 0)

    def test_short_io_capability_cycle_topology_and_small_bar(self):
        original = (self.gpu/'config').read_bytes()
        (self.gpu/'config').write_bytes(original[:1])
        self.assertEqual(self.result()['loads'], 0)
        (self.gpu/'config').write_bytes(original)
        self.change_config(self.gpu/'config', 0x41, 0x40, 1)
        self.assertEqual(self.result()['loads'], 0)
        (self.gpu/'config').write_bytes(original)
        (self.gpu/'resource').write_text('90000000 90000fff 200\n')
        self.assertEqual(self.result()['loads'], 0)
        (self.sysfs/probe.ROOT).unlink()
        (self.sysfs/probe.ROOT).symlink_to(self.gpu)
        self.assertEqual(self.result()['loads'], 0)

    def test_root_power_and_memory_forwarding_fail_closed(self):
        (self.root/'power_state').write_text('D3hot')
        self.assertEqual(self.result()['loads'], 0)
        (self.root/'power_state').write_text('D0')
        self.change_config(self.root/'config', 4, 4)
        self.assertEqual(self.result()['loads'], 0)

    def test_allowlist_validation_closed_and_no_load_on_denied(self):
        self.adapter.open()
        for offset in (0, 0x8c041, -1, 2**64, True, 1.0, '0x8c040'):
            with self.assertRaises(probe.Stop):
                self.adapter.read(offset)
        self.assertEqual(self.adapter.loads, 0)
        self.adapter.close()
        with self.assertRaises(probe.Stop):
            self.adapter.read(0x8c040)

    def test_driver_or_resource_change_after_mapping_stops(self):
        self.adapter.open()
        (self.gpu/'driver').mkdir()
        with self.assertRaises(probe.Stop):
            self.adapter.read(0x8c040)
        (self.gpu/'driver').rmdir()
        (self.gpu/'resource').write_text('90000000 91ffffff 200\n')
        with self.assertRaises(probe.Stop):
            self.adapter.read(0x8c040)
        self.assertEqual(self.adapter.loads, 0)

    def test_postcheck_link_loss_stops_without_more_endpoint_access(self):
        original = self.lib.ro_read
        def load_then_loss(handle, off):
            result = original(handle, off)
            self.change_config(self.root/'config', 0xa2, 0x1041)
            return result
        with patch.object(self.lib, 'ro_read', side_effect=load_then_loss):
            result = self.result()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['loads'], 1)
        self.assertFalse(any(r['event'] == 'dword' for r in self.rows))
        self.assertEqual(self.rows[-2]['event'], 'cleanup')

    def test_lbms_change_during_load_is_logged_and_stops(self):
        original = self.lib.ro_read
        def load_then_lbms(handle, off):
            result = original(handle, off)
            self.change_config(self.root/'config', 0xa2, 0x7041)
            return result
        with patch.object(self.lib, 'ro_read', side_effect=load_then_lbms):
            result = self.result()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['loads'], 1)
        self.assertIn('Root status changed', result['reason'])
        row = next(r for r in self.rows if r['event'] == 'dword')
        self.assertEqual(row['root_post'], 0x7041)

    def test_sentinel_and_unexpected_baseline_stop_without_next_dword(self):
        for value in (0xffffffff, 0xbadf5040, 7):
            ram = bytearray(self.bar.read_bytes())
            struct.pack_into('<I', ram, next(iter(probe.FACTORY)), value)
            self.bar.write_bytes(ram)
            previous = self.adapter.loads
            result = self.result()
            self.assertEqual(result['result'], 'FAIL')
            self.assertEqual(result['loads']-previous, 1)

    def test_new_aer_stops_existing_status_is_retained(self):
        original = self.lib.ro_read
        self.change_config(self.root/'config', 0x104, 2, 4)
        def read_and_set_aer(handle, off):
            value = original(handle, off)
            self.change_config(self.root/'config', 0x104, 3, 4)
            return value
        with patch.object(self.lib, 'ro_read', side_effect=read_and_set_aer):
            result = self.result()
        self.assertEqual(result['loads'], 1)
        self.assertIn('AER', result['reason'])
        self.assertEqual(int.from_bytes((self.root/'config').read_bytes()[0x104:0x108], 'little'), 3)

    def test_mapping_error_and_cleanup_error_fail(self):
        with patch.object(self.lib, 'ro_map', return_value=None):
            result = self.result()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['loads'], 0)
        self.adapter.open()
        original = self.lib.ro_close
        def close_and_report_error(handle):
            original(handle)
            return -1
        with patch.object(self.lib, 'ro_close', side_effect=close_and_report_error):
            with self.assertRaisesRegex(probe.Stop, 'munmap'):
                self.adapter.close()
        self.assertIsNone(self.adapter.mapping)

    def test_os_mapping_is_shared_readonly_and_short_file_refused(self):
        self.adapter.open()
        rows = [row for row in Path('/proc/self/maps').read_text().splitlines()
                if str(self.bar) in row]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].split()[1], 'r--s')
        self.adapter.close()
        self.bar.write_bytes(b'x')
        fd = os.open(self.bar, os.O_RDONLY)
        try:
            self.assertFalse(self.lib.ro_map(fd))
        finally:
            os.close(fd)

    def test_native_refuses_writable_fd_and_unlisted_offsets(self):
        fd = os.open(self.bar, os.O_RDWR)
        try:
            self.assertFalse(self.lib.ro_map(fd))
        finally:
            os.close(fd)
        fd = os.open(self.bar, os.O_RDONLY)
        try:
            handle = self.lib.ro_map(fd)
            self.assertTrue(handle)
            try:
                self.assertEqual(self.lib.ro_read(handle, 0), 2**64-1)
                self.assertEqual(self.lib.ro_read(handle, 0x8c041), 2**64-1)
                self.assertEqual(self.lib.ro_read(handle, 2**64-1), 2**64-1)
            finally:
                self.assertEqual(self.lib.ro_close(handle), 0)
        finally:
            os.close(fd)

    def test_binary_hash_and_assembly_fail_closed(self):
        with patch.object(native, 'audit', side_effect=RuntimeError('bad assembly')):
            with self.assertRaises(RuntimeError):
                native.load(self.binary)
        manifest = self.binary.with_suffix('.json')
        text = manifest.read_text()
        try:
            data = json.loads(text)
            data['binary_sha256'] = 'wrong'
            manifest.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                native.load(self.binary)
        finally:
            manifest.write_text(text)

    def test_worker_signal_and_timeout_have_jsonl_stop_no_retry(self):
        context = multiprocessing.get_context('spawn')
        for target, args, timeout in ((signal_worker, (signal.SIGBUS,), 3),
                                      (signal_worker, (signal.SIGSEGV,), 3),
                                      (stalled_worker, (), 0.1)):
            receive, send = context.Pipe(duplex=False)
            process = context.Process(target=target, args=args)
            process.start()
            send.close()
            rows = []
            try:
                self.assertEqual(probe.supervise(process, receive, rows.append, timeout), 1)
                self.assertEqual(rows[-1]['result'], 'FAIL')
                self.assertFalse(process.is_alive())
            finally:
                if process.is_alive():
                    process.kill()
                    process.join()
                receive.close()


if __name__ == '__main__':
    unittest.main()
