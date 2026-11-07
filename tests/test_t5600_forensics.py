"""Collector tests use ordinary temporary fixtures and fixed utility mocks only."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('forensics', Path(__file__).parents[1]/'tools'/'collect_t5600_forensics.py')
f = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(f)
SHA = '16f39946d95afe4c63e92404cb0e8bd89c39ac5e'
BOOT = '1234567890abcdef1234567890abcdef'
SECRET = 'ghp_DO_NOT_COLLECT_PRIVATE_CANARY_12345'


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='p104-forensic-fixtures-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo, self.home = self.base/'repo', self.base/'home'
        self.repo.mkdir()
        self.home.mkdir()

    @staticmethod
    def runner(argv, **kwargs):
        if 'log' in argv:
            data = (SHA+'\t1793925103\t1793925103\n').encode()
        elif 'reflog' in argv:
            data = (SHA+'\trefs/heads/SECRET_BRANCH@{1793925104 +0500}\n').encode()
        elif 'show' in argv:
            data = b'import struct\nstruct.pack_into("<I", mm, 0x8c040, new)\n'
        elif '--list-boots' in argv:
            data = ('-1 '+BOOT+' Fri 2026-11-06 00:00:00 UTC Fri 2026-11-06 01:00:00 UTC\n').encode()
        elif argv[0] == '/usr/bin/journalctl':
            assert '_TRANSPORT=kernel' in argv and '-k' not in argv
            rows = [dict(MESSAGE='NVRM: Xid (PCI:0000:03:00): 79 '+SECRET,
                         _BOOT_ID=BOOT, __REALTIME_TIMESTAMP='1793926000000000',
                         __MONOTONIC_TIMESTAMP='123456'),
                    dict(MESSAGE='NVRM: Xid (PCI:0000:04:00): 79 '+SECRET),
                    dict(MESSAGE='pcieport 0000:00:02.0: AER: Uncorrectable (Fatal) error '+SECRET),
                    dict(MESSAGE='unrelated auth '+SECRET)]
            data = b'\n'.join(json.dumps(x).encode() for x in rows)
        elif argv[0] == '/usr/bin/dpkg-query':
            data = b'python3.12-minimal\t3.12.3-1ubuntu0.17\n'
        else:
            data = b''
        return dict(status='ok', returncode=0, stdout=data)

    def test_source_is_parsed_not_executed_and_mapping_is_syntactic(self):
        marker = self.base/'MUST_NOT_EXIST'
        source = (f'from pathlib import Path\nPath({str(marker)!r}).write_text("bad")\n'
                  'import struct as S, mmap, os\n'
                  'fd=os.open("/sys/bus/pci/devices/0000:03:00.0/resource0", os.O_RDWR|os.O_SYNC)\n'
                  'mm=mmap.mmap(fd,4096)\nS.pack_into("<I",mm,0x8c040,0x20045800)\n')
        facts = f.source_facts(source.encode())
        self.assertFalse(marker.exists())
        self.assertEqual(facts['primitives'][0]['kind'], 'struct.pack_into')
        self.assertTrue(facts['primitives'][0]['syntactic_mapping_argument'])
        self.assertEqual(facts['resource_open'][0]['flags_mentioned'], ['os.O_RDWR', 'os.O_SYNC'])
        self.assertFalse(facts['observed_execution'])

    def test_ctypes_source_and_parse_failure(self):
        result = f.source_facts(b'import ctypes as C\nC.c_uint32.from_address(address).value = value\n')
        self.assertEqual(result['primitives'][0]['kind'], 'ctypes_uint32_assignment')
        self.assertEqual(f.source_facts(b'not Python !!!')['parse_status'], 'unavailable')

    def test_regular_file_hash_is_not_execution_timestamp(self):
        p = self.repo/'p104_gen2_linux.py'
        p.write_bytes(b'print("not executed")\n')
        meta, data = f.safe_file(p, 1000)
        self.assertEqual(meta['sha256'], hashlib.sha256(data).hexdigest())
        self.assertFalse(meta['timestamps_prove_execution'])

    def test_special_files_symlinks_and_namespaces_refused(self):
        p = self.base/'fifo'
        os.mkfifo(p)
        self.assertEqual(f.safe_file(p, 1000)[0]['status'], 'not_regular')
        link = self.base/'p104_gen2_linux.py'
        link.symlink_to('/sys/bus/pci/devices/0000:03:00.0/resource0')
        self.assertIn(f.safe_file(link, 1000)[0]['status'], ('symlink_refused', 'unavailable'))
        self.assertEqual(f.safe_file('/sys/fake', 1000)[0]['status'], 'forbidden_namespace')
        self.assertEqual(f.safe_file('/dev/mem', 1000)[0]['status'], 'forbidden_namespace')

    def test_size_limit(self):
        p = self.base/'large'
        p.write_bytes(b'x'*1024)
        self.assertEqual(f.safe_file(p, 8)[0]['status'], 'size_limit')

    def test_history_keeps_flags_not_secrets_or_full_command(self):
        raw = ('#1793926000\nsudo env GH_TOKEN='+SECRET+' python3 ./p104_gen2_linux.py '
               '--apply --confirm-risk --confirm-bdf 0000:03:00.0 --password '+SECRET+'\n'
               'git clone https://user:'+SECRET+'@github.com/AlexGL402/p104-unlock-v3\n')
        rows = f.history_facts(raw.encode())
        self.assertEqual(rows[0]['kind'], 'writer_invocation_syntax')
        self.assertIn('--apply', rows[0]['flags'])
        self.assertFalse(rows[0]['execution_proven'])
        self.assertIsNotNone(rows[0]['saved_shell_timestamp'])
        saved = json.dumps(rows)
        for secret in (SECRET, 'GH_TOKEN', '--password', 'https://', 'user:'):
            self.assertNotIn(secret, saved)

    def test_mentions_and_python_payloads_are_not_script_launches(self):
        for command in ('echo python3 p104_gen2_linux.py --apply',
                        'cat p104_gen2_linux.py',
                        'python3 -c "print(\'p104_gen2_linux.py\')"'):
            self.assertNotEqual(f.history_fact(command)['kind'], 'writer_invocation_syntax')
        self.assertIsNone(f.history_fact('python3 p104_gen2_linux.py --apply #0000:04:00.0'))
        self.assertFalse(f.mentions_other_gpu('P104 command at 04:00:00'))

    def test_absolute_script_tokens_can_be_correlated_without_plain_paths(self):
        path = str(self.repo/'p104_gen2_linux.py')
        fact = f.history_fact('/usr/bin/python3.12 '+path+' --apply')
        self.assertEqual(fact['script_token_sha256'], hashlib.sha256(path.encode()).hexdigest())
        self.assertTrue(fact['script_token_absolute'])
        self.assertTrue(fact['interpreter_token_absolute'])
        self.assertNotIn(str(self.repo), json.dumps(fact))

    def test_zsh_multiline_sudo_user_and_unknown_time(self):
        raw = (': 1793926000:0;sudo -u user python3 p104_gen2_linux.py \\\n'
               '--apply\npython3 p104_gen2_linux.py --dry-run\n')
        rows = f.history_facts(raw.encode())
        self.assertIn('--apply', rows[0]['flags'])
        self.assertIsNone(rows[1]['saved_shell_timestamp'])

    def test_json_logs_are_allowlisted_and_intent_is_not_ack(self):
        raw = json.dumps(dict(event='before_mmio', offset='0x8c040', old='0x20085800',
                              new='0x20045800', reason='GPU disappeared '+SECRET,
                              token=SECRET, argv=SECRET, ts='2026-11-06T05:40:00')).encode()
        rows = f.log_facts(raw)
        self.assertEqual(rows[0]['new'], 0x20045800)
        self.assertEqual(rows[0]['semantics'], 'intent_not_ack')
        self.assertEqual(rows[0]['reason_class'], 'endpoint_lost')
        self.assertNotIn(SECRET, json.dumps(rows))

    def test_log_baseline_decimal_and_hex_keys(self):
        raw = json.dumps(dict(event='cap_retrain_baseline', actual={
            '573504': 0x20085800, '0x8c1c0': 0x20032, 'bad': SECRET})).encode()
        result = f.log_facts(raw)[0]['actual']
        self.assertEqual(result, {'573504': 0x20085800, '573888': 0x20032})

    def test_journal_cross_boot_filter_and_redaction(self):
        result = f.journal_facts('2026-11-05 19:00:00 UTC', '2026-11-06 03:00:00 UTC', self.runner)
        self.assertEqual(len(result['events']), 2)
        self.assertEqual(result['events'][0]['xid'], 79)
        self.assertEqual(result['events'][0]['boot_id'], BOOT)
        self.assertEqual(result['events'][1]['severity'], 'fatal')
        self.assertFalse(result['complete_visibility_proven'])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_inaccessible_journal_is_unknown_not_no_crash(self):
        runner = lambda *a, **k: dict(status='unavailable_or_failed', returncode=1, stdout=b'')
        result = f.journal_facts('a', 'b', runner)
        self.assertEqual(result['query_status'], 'unavailable_or_failed')
        self.assertFalse(result['complete_visibility_proven'])

    def test_git_metadata_does_not_prove_executed_content(self):
        result = f.git_facts(self.repo, self.runner)
        self.assertEqual(result['history'][0]['commit'], SHA)
        self.assertEqual(result['reflog'][0]['reflog_epoch'], 1793925104)
        self.assertNotIn('SECRET_BRANCH', json.dumps(result))
        # The commit label is known but this fixture's content is not that archive.
        self.assertIsNone(result['objects'][0]['archive_match'])
        self.assertFalse(result['objects'][0]['execution_proven'])

    def test_archive_registry_matches_published_manifest(self):
        path = Path(__file__).parents[1]/'docs/evidence/linux-mmio-primitive-offline.json'
        entries = json.loads(path.read_text())['legacy_sources']
        self.assertEqual(f.ARCHIVES, {x['sha256']: x['commit'] for x in entries})

    def test_discovery_is_narrow_and_does_not_follow_links(self):
        (self.repo/'.config').mkdir()
        (self.repo/'.config'/'p104_gen2_linux.py').write_text(SECRET)
        (self.repo/'p104_gen2_linux.py').write_text('pass')
        (self.repo/'link').symlink_to(self.home, target_is_directory=True)
        (self.home/'p104_gen2_linux.py').write_text('pass')
        sources, logs, gaps = f.discover([self.repo])
        self.assertEqual(sources, [self.repo/'p104_gen2_linux.py'])
        self.assertEqual(logs, [])

    def test_paths_do_not_disclose_secret_components(self):
        p = self.repo/SECRET/('gpu0-'+SECRET+'.log')
        self.assertNotIn(SECRET, f.hint(p, self.repo, self.home))

    def test_collect_only_fixture_data_never_authorizes_hardware(self):
        (self.repo/'p104_gen2_linux.py').write_text('raise SystemExit("never execute")')
        (self.home/'.bash_history').write_text('python3 p104_gen2_linux.py --apply --secret '+SECRET)
        result = f.collect(self.repo, [self.repo], self.home, 'a', 'b', self.runner, runtime={'fixture': True})
        self.assertEqual(result['assessment']['executed_crash_writer'], 'Unknown')
        self.assertEqual(result['assessment']['decision'], 'NO-GO')
        self.assertFalse(result['hardware_access'])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_unsafe_repo_is_rejected_before_utilities(self):
        with patch.object(f, 'git_facts') as git:
            with self.assertRaises(ValueError):
                f.collect(Path('/sys'), [], self.home, 'a', 'b', self.runner, runtime={})
            git.assert_not_called()

    def test_runner_does_not_allow_python_or_hardware_utilities(self):
        for utility in ('/usr/bin/python3', '/usr/bin/lspci', '/usr/bin/setpci'):
            with self.assertRaises(ValueError):
                f.run_bounded([utility])

    def test_runner_output_is_bounded(self):
        r = f.run_bounded(['/usr/bin/git', '--version'], limit=4)
        self.assertEqual(r['status'], 'output_limit')
        self.assertEqual(len(r['stdout']), 4)

    def test_cli_refuses_root_and_invalid_dates(self):
        with patch.object(f.os, 'geteuid', return_value=0), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                f.main([])
        with patch.object(f.os, 'geteuid', return_value=1000), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                f.main(['--since', '2026-11-06', '--until', '2026-11-07'])

    def test_cli_creates_private_json_and_never_overwrites(self):
        output = self.base/'report.json'
        with patch.object(f.os, 'geteuid', return_value=1000), patch.object(f, 'collect', return_value={'safe': True}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(f.main(['--output', str(output)]), 0)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                f.main(['--output', str(output)])
        self.assertEqual(json.loads(output.read_text()), {'safe': True})

    def test_real_git_fixture_disables_textconv_and_external_diff(self):
        def git(*args):
            return subprocess.run(['/usr/bin/git', '-C', str(self.repo), *args],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(PATH='/usr/bin:/bin',
                GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', LC_ALL='C')).stdout
        git('init', '-q')
        source = self.repo/'p104_gen2_linux.py'
        source.write_text('raise RuntimeError("must not execute source")\n')
        (self.repo/'.gitattributes').write_text('*.py diff=trap\n')
        git('add', '.')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        marker = self.base/'BAD_HELPER_EXECUTED'
        git('config', 'diff.trap.textconv', 'touch '+str(marker))
        git('config', 'diff.external', 'touch '+str(marker))
        result = f.git_facts(self.repo)
        self.assertFalse(marker.exists())
        self.assertEqual(len(result['history']), 1)
        current = [x for x in result['objects'] if x.get('sha256') == hashlib.sha256(source.read_bytes()).hexdigest()]
        self.assertEqual(len(current), 1)


class AdditionalRamControlTests(unittest.TestCase):
    @unittest.skipUnless(sys.implementation.name == 'cpython', 'CPython-specific source path')
    def test_plain_int_overflow_leaves_cleared_ram_without_index_probe(self):
        ram = bytearray(b'PRE!' + struct.pack('<I', 0x20085800) + b'POST')
        with self.assertRaises(struct.error):
            struct.pack_into('<I', ram, 4, 2**32)
        self.assertEqual(ram[4:8], bytes(4))
        self.assertEqual(ram[:4] + ram[8:], b'PRE!POST')
        # Ordinary int, but deliberately invalid: not a trace of the crash target.


if __name__ == '__main__':
    unittest.main()
