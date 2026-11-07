#!/usr/bin/env python3
"""Non-root, file/journal-only forensic collector. Never opens PCI/MMIO.

Run with python3 -I -S. Found source is parsed, never imported or executed.
Only structured allowlisted facts leave the in-memory input filters.
"""
import argparse
import ast
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import time
import _struct


ARCHIVES = {
    '34208c695b1a4c89d126950aa0219724f94f93ca7452aaeb09dcdd6b42372357': 'c2f4a75bc34cbe8d0e6c3a9d3eeec920596a5861',
    'f5cca64aed395e1b53b0608f7851d6e7d1557b9412d4a3a9d37a417a63f0269b': 'e1bff32c92304a3b6fc1029e4d9c99d459ea8741',
    'c5f61e73c47221ca0ccbf1c9a8e1504d9a42dbe3c8ef5f66a7c5008e10688f9d': '16f39946d95afe4c63e92404cb0e8bd89c39ac5e',
}
SOURCE_NAME = re.compile(r'p104_gen2_linux\.py(?:~|\.(?:bak|old|orig|save|\d{8,20}))?\Z')
HEX = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?\Z')
GPU, ROOT = '0000:03:00.0', '0000:00:02.0'
OFFSETS = {0x8872C, 0x2241C, 0x8841C, 0x8C2C0, 0x8C300, 0x8C040, 0x8C1C0, 0x88610}
MODES = {'--apply', '--probe', '--dry-run', '--confirm-risk', '--confirm-bdf',
         '--stage', '--mode', '--read-only'}
DENIED = {'/sys', '/proc', '/dev', '/run'}
SKIP_DIRS = {'.git', '.ssh', '.aws', '.config', '.cache', '.venv', 'venv',
             '__pycache__', 'node_modules', '.codex', '.agents'}
SAFE_PARTS = {'work', 'tools', 'evidence', 't5600', 'Downloads', 'Desktop',
              'p104-unlock-v3', 'stage1', 'stage1-override', 'override-520',
              'cap-speed-plan', 'cap-speed-result', 'cap-retrain-plan', 'cap-retrain-result'}
LIMIT = 128


def stamp(value):
    try:
        if type(value) in (int, float):
            return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
        if isinstance(value, str) and len(value) <= 40:
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.isoformat()  # naive timestamps remain explicitly naive
    except (ValueError, OverflowError, OSError):
        pass
    return None


def safe_file(path, limit, digest_only=False):
    """Reject links/special files and device namespaces; bound reads and report races."""
    path = Path(os.path.abspath(path))
    if any(path == Path(p) or Path(p) in path.parents for p in DENIED):
        return {'status': 'forbidden_namespace'}, None
    try:
        if path.resolve(strict=True) != path:
            return {'status': 'symlink_refused'}, None
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return {'status': 'not_regular'}, None
        if before.st_size > limit:
            return {'status': 'size_limit', 'size': before.st_size}, None
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return {'status': 'file_changed'}, None
            h, blocks, count = hashlib.sha256(), [], 0
            while True:
                block = stream.read(min(65536, limit + 1 - count))
                if not block:
                    break
                count += len(block)
                if count > limit:
                    return {'status': 'size_limit'}, None
                h.update(block)
                if not digest_only:
                    blocks.append(block)
            after = os.fstat(stream.fileno())
        stable = (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) == (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        named = path.lstat()
        stable = stable and (named.st_dev, named.st_ino) == (after.st_dev, after.st_ino)
        return dict(status='ok' if stable else 'file_changed', size=count, sha256=h.hexdigest(),
                    mtime_ns=opened.st_mtime_ns, ctime_ns=opened.st_ctime_ns,
                    timestamps_prove_execution=False), None if digest_only else b''.join(blocks)
    except (OSError, RuntimeError):
        return {'status': 'unavailable'}, None  # no raw exception/path/config data


def hint(path, repo, home):
    """Keep useful source locations, redact unrelated directory names."""
    path = Path(os.path.abspath(path))
    for base, label in ((repo, '$REPO'), (home, '$HOME')):
        try:
            parts = path.relative_to(base).parts
            break
        except ValueError:
            pass
    else:
        parts, label = path.parts[1:], '$OTHER'
    clean = []
    for part in parts:
        allowed = part in SAFE_PARTS or SOURCE_NAME.fullmatch(part) or part == 'original.py'
        allowed = allowed or bool(re.fullmatch(r'(?:gpu0|root0)(?:-(?:stage1|stage2|cap|speed|retrain|override|dual|profile|lnksta))*-\d{10,25}\.(?:jsonl|log)', part))
        clean.append(part if allowed else 'item_' + hashlib.sha256(part.encode()).hexdigest()[:10])
    return label + '/' + '/'.join(clean)


def source_facts(data):
    result = {'primitives': [], 'resource_open': [], 'mapping_lines': [], 'mapping': [],
              'observed_execution': False}
    try:
        tree = ast.parse(data.decode('utf-8-sig'))
    except (SyntaxError, UnicodeError, ValueError, MemoryError, RecursionError):
        return dict(result, parse_status='unavailable')
    aliases = {n: n for n in ('struct', 'ctypes', 'os', 'mmap')}
    nodes = []
    for node in ast.walk(tree):
        if len(nodes) >= 30000:
            break
        nodes.append(node)
    for node in nodes:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in aliases:
                    aliases[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module in ('struct', 'ctypes', 'mmap', 'os'):
            for a in node.names:
                aliases[a.asname or a.name] = node.module + '.' + a.name

    def name(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return name(node.value) + '.' + node.attr
        return ''

    mappings = set()
    for n in nodes:
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and name(n.value.func) == 'mmap.mmap':
            mappings.update(t.id for t in n.targets if isinstance(t, ast.Name))
    for n in nodes:
        if isinstance(n, ast.Call):
            called = name(n.func)
            if called == 'struct.pack_into':
                fmt = n.args[0].value if n.args and isinstance(n.args[0], ast.Constant) else None
                result['primitives'].append(dict(kind='struct.pack_into', line=n.lineno,
                    format=fmt if fmt in ('<I', '=I', '@I', 'I', '<L') else 'other_or_dynamic',
                    syntactic_mapping_argument=len(n.args) > 1 and isinstance(n.args[1], ast.Name)
                    and n.args[1].id in mappings))
            if called == 'mmap.mmap':
                result['mapping_lines'].append(n.lineno)
                flags = sorted({name(v) for v in ast.walk(n) if isinstance(v, ast.Attribute)} & {
                    'mmap.MAP_SHARED', 'mmap.MAP_PRIVATE', 'mmap.PROT_READ', 'mmap.PROT_WRITE'})
                result['mapping'].append(dict(line=n.lineno, flags_mentioned=flags,
                                               runtime_attributes_proven=False))
            if called in ('os.open', 'open', 'builtins.open'):
                strings = [v.value for v in ast.walk(n) if isinstance(v, ast.Constant) and isinstance(v.value, str)]
                if any('resource0' in s for s in strings):
                    flags = sorted({name(v) for v in ast.walk(n) if isinstance(v, ast.Attribute)} & {
                        'os.O_RDONLY', 'os.O_RDWR', 'os.O_SYNC', 'os.O_CLOEXEC', 'os.O_NOFOLLOW'})
                    result['resource_open'].append(dict(line=n.lineno, api=called,
                        resource='resource0_wc' if any('resource0_wc' in s for s in strings) else 'resource0',
                        flags_mentioned=flags, runtime_flags_proven=False))
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Attribute) and t.attr == 'value' and isinstance(t.value, ast.Call):
                    if name(t.value.func) == 'ctypes.c_uint32.from_address':
                        result['primitives'].append(dict(kind='ctypes_uint32_assignment', line=t.lineno))
    for k in ('primitives', 'resource_open', 'mapping_lines', 'mapping'):
        result[k] = result[k][:32]
    return dict(result, parse_status='ok', analysis='syntactic only; aliases/dynamic dataflow may be incomplete')


def run_bounded(argv, limit=2**20, timeout=6):
    """Trusted fixed utilities only; no shell, hooks, pager, secrets env or network."""
    if argv[0] not in ('/usr/bin/git', '/usr/bin/journalctl', '/usr/bin/dpkg-query'):
        raise ValueError('utility not allowlisted')
    env = dict(PATH='/usr/bin:/bin', LC_ALL='C', TZ='UTC', GIT_CONFIG_NOSYSTEM='1',
        GIT_CONFIG_GLOBAL='/dev/null', GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0',
        GIT_ALLOW_PROTOCOL='', GIT_NO_LAZY_FETCH='1', GIT_NO_REPLACE_OBJECTS='1')
    data, status, process = bytearray(), 'ok', None
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env=env, start_new_session=True)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while selector.get_map():
                if time.monotonic() >= deadline:
                    status = 'timeout'
                    break
                for key, _ in selector.select(min(.1, max(0, deadline - time.monotonic()))):
                    block = os.read(key.fd, min(65536, limit + 1 - len(data)))
                    if not block:
                        selector.unregister(key.fileobj)
                    else:
                        data.extend(block)
                        if len(data) > limit:
                            status = 'output_limit'
                            break
                if status != 'ok':
                    break
        if status != 'ok':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        code = process.wait(timeout=1)
        return dict(status=status if status != 'ok' or code == 0 else 'unavailable_or_failed',
                    returncode=code, stdout=bytes(data[:limit]))
    except (OSError, subprocess.SubprocessError):
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
        return dict(status='unavailable_or_failed', returncode=None, stdout=b'')
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()


def git_facts(repo, runner=run_bounded):
    prefix = ['/usr/bin/git', '--no-pager', '-c', 'core.hooksPath=/dev/null',
              '-c', 'core.fsmonitor=false', '-c', 'credential.helper=',
              '-c', 'log.showSignature=false', '-c', 'protocol.allow=never', '-C', str(repo)]
    result = {'history': [], 'reflog': [], 'objects': [], 'coverage': {}}

    def invoke(key, args):
        r = runner(prefix + args)
        result['coverage'][key] = {k: r[k] for k in ('status', 'returncode')}
        return r['stdout'].decode('utf-8', 'replace').splitlines()

    lines = invoke('history', ['log', '--no-show-signature', '--no-notes', '--all', '--max-count=48', '--format=%H%x09%ct%x09%at',
                               '--', 'p104_gen2_linux.py'])
    for line in lines[:48]:
        p = line.split('\t')
        if len(p) == 3 and HEX.fullmatch(p[0]) and all(x.isdigit() for x in p[1:]):
            result['history'].append(dict(commit=p[0], committer_epoch=int(p[1]), author_epoch=int(p[2]),
                                           execution_proven=False))
    lines = invoke('reflog', ['reflog', '--all', '--max-count=128', '--date=raw', '--format=%H%x09%gD'])
    for line in lines[:LIMIT]:
        m = re.fullmatch(r'([0-9a-f]{40,64})\t[^\t]*@\{(\d+) ([+-]\d{4})\}', line)
        if m and HEX.fullmatch(m[1]):
            result['reflog'].append(dict(commit=m[1], reflog_epoch=int(m[2]), timezone=m[3], execution_proven=False))
    commits = list(dict.fromkeys(list(ARCHIVES.values()) + [x['commit'] for x in result['history']]))[:51]
    for commit in commits:
        lines = runner(prefix + ['show', '--no-ext-diff', '--no-textconv', commit + ':p104_gen2_linux.py'],
                       limit=512*1024)
        if lines['status'] == 'ok':
            data = lines['stdout']
            digest = hashlib.sha256(data).hexdigest()
            result['objects'].append(dict(commit=commit, sha256=digest,
                archive_match=ARCHIVES.get(digest), source=source_facts(data), execution_proven=False))
        else:
            result['objects'].append(dict(commit=commit, status=lines['status']))
    result['coverage']['limits'] = dict(history=48, reflog=128, objects=51,
        note='Missing/shallow/expired objects are unknown; no fetch is permitted; timestamps are not execution evidence')
    return result


def mentions_other_gpu(text):
    return bool(re.search(r'(?:(?:0000:)?04:00\.0|0000:04:00|(?:0000:)?00:03\.0)(?![0-9a-f.])', text, re.I))


def history_fact(text):
    """Never return raw text, URLs, arbitrary arguments, environment values or hashes."""
    if mentions_other_gpu(text):
        return None
    if not re.search(r'p104|03:00|00:02\.0|8c040', text, re.I):
        return None
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=';&|<>')
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return {'kind': 'relevant_unparsed_mention', 'execution_proven': False}

    def invocation(script, interpreter, tail):
        return dict(kind='writer_invocation_syntax', interpreter=Path(interpreter).name,
            interpreter_token_sha256=hashlib.sha256(interpreter.encode()).hexdigest(),
            interpreter_token_absolute=Path(interpreter).is_absolute(),
            flags=sorted({t.split('=', 1)[0] for t in tail} & MODES),
            script_token_sha256=hashlib.sha256(script.encode()).hexdigest(),
            script_token_absolute=Path(script).is_absolute(),
            confirm_bdf_is_gpu0=any(t == '--confirm-bdf=' + GPU or
                t == '--confirm-bdf' and i+1 < len(tail) and tail[i+1] == GPU
                for i, t in enumerate(tail)),
            execution_proven=False, argv_values_redacted=True)
    segments, segment = [], []
    for token in tokens + [';']:
        if token in (';', '&&', '||', '|', '&'):
            segments.append(segment)
            segment = []
        else:
            segment.append(token)
    for segment in segments:
        pos = 0
        while pos < len(segment):
            token = segment[pos]
            if token in ('-u', '-g') and pos > 0 and segment[0] == 'sudo':
                pos += 2
                continue
            if token in ('sudo', 'env', 'command', 'time', 'nohup') or token.startswith('-') or re.match(r'^\w+=', token):
                pos += 1
            else:
                break
        if pos >= len(segment):
            continue
        command = Path(segment[pos]).name
        tail = segment[pos+1:]
        if re.fullmatch(r'python(?:3(?:\.12)?)?', command):
            # Never interpret Python -c or -m payloads as a script invocation.
            for t in tail:
                if t in ('-c', '-m'):
                    break
                if SOURCE_NAME.fullmatch(Path(t).name):
                    return invocation(t, segment[pos], tail)
                if not t.startswith('-'):
                    break
        if SOURCE_NAME.fullmatch(command):
            return invocation(segment[pos], 'shebang_unknown', tail)
        if command == 'git' and tail:
            operation = tail[0] if tail[0] in ('checkout', 'switch', 'pull', 'clone', 'reset', 'show', 'log') else 'other'
            return dict(kind='git_command_syntax', operation=operation,
                        known_commits=[c for c in ARCHIVES.values() if any(t == c or t == c[:7] for t in tail)],
                        execution_proven=False, argv_values_redacted=True)
        if command in ('lspci', 'setpci', 'journalctl', 'nvidia-smi', 'sha256sum'):
            return dict(kind='related_command_syntax', tool=command, execution_proven=False,
                        argv_values_redacted=True)
    return dict(kind='relevant_mention_only', execution_proven=False)


def history_facts(data):
    matches, pending_time, pending, start = [], None, '', 0
    for line_no, line in enumerate(data.decode('utf-8', 'replace').splitlines()[:50000], 1):
        bash = re.fullmatch(r'#(\d{9,12})', line)
        if bash:
            pending_time = stamp(int(bash[1]))
            continue
        zsh = re.match(r'^: (\d{9,12}):\d+;(.*)$', line)
        if zsh:
            pending_time, line = stamp(int(zsh[1])), zsh[2]
        if not pending:
            start = line_no
        pending += line[:8192]
        if line.endswith('\\') and len(pending) < 8192:
            pending = pending[:-1] + ' '
            continue
        fact = history_fact(pending[:8192])
        if fact:
            matches.append(dict(fact, line=start, saved_shell_timestamp=pending_time,
                                timestamp_is_execution_proof=False))
        pending, pending_time = '', None
        if len(matches) >= LIMIT:
            break
    return matches


def event_class(text):
    lowered = text.lower()
    if 'fallen off' in lowered or 'gpu disappeared' in lowered or 'gpu is lost' in lowered:
        return 'endpoint_lost'
    if 'link down' in lowered or 'linkdown' in lowered:
        return 'link_down'
    if 'xid' in lowered:
        return 'xid'
    if 'aer' in lowered or 'pcie bus error' in lowered:
        return 'aer'
    if 'baseline mismatch' in lowered:
        return 'baseline_mismatch'
    if 'no retry' in lowered or 'no fallback' in lowered or 'no further writes' in lowered:
        return 'stop_no_retry'
    return None


def number(value):
    if type(value) is int and 0 <= value <= 0xFFFFFFFF:
        return value
    if isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]{1,8}', value):
        return int(value, 16)
    return None


def log_facts(data):
    output = []
    for line_no, line in enumerate(data.decode('utf-8', 'replace').splitlines()[:10000], 1):
        if len(line) > 65536 or mentions_other_gpu(line):
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            # Plain text is reduced to event class only, never copied.
            kind = event_class(line)
            if kind and re.search(r'p104|03:00|8c040', line, re.I):
                output.append(dict(line=line_no, kind=kind, source='text_classification'))
            continue
        if not isinstance(row, dict):
            continue
        event = row.get('event')
        if event not in ('baseline', 'before_mmio', 'after_mmio', 'before_tls', 'after_tls',
                         'before_retrain', 'abort', 'result', 'cap_retrain_result', 'cap_speed_result',
                         'override_baseline', 'cap_speed_baseline', 'cap_retrain_baseline'):
            continue
        entry = dict(line=line_no, event=event, timestamp=stamp(row.get('ts')))
        for k in ('offset', 'old', 'new', 'readback', 'mask'):
            n = number(row.get(k))
            if n is not None:
                entry[k] = n
        for k in ('driver_bound', 'apply', 'recovery_writes'):
            if type(row.get(k)) is bool:
                entry[k] = row[k]
        if row.get('mode') in ('stage1', 'stage1-override', 'stage1-cap-speed',
                               'stage2', 'stage2-cap-retrain', 'probe', 'dry-run', 'apply'):
            entry['mode'] = row['mode']
        for k in ('script_sha256', 'source_sha256', 'python_sha256', 'executable_sha256', '_struct_sha256'):
            if isinstance(row.get(k), str) and re.fullmatch(r'[0-9a-f]{64}', row[k]):
                entry[k] = row[k]
        for k in ('registers', 'actual'):
            if isinstance(row.get(k), dict):
                regs = {str(n): number(v) for a, v in row[k].items()
                        if (n := number(a) if isinstance(a, str) and a.startswith('0x') else
                            int(a) if isinstance(a, str) and a.isdigit() and len(a) < 10 else None) in OFFSETS}
                entry[k] = {a: v for a, v in regs.items() if v is not None}
        for k in ('links', 'gpu_link', 'root_link'):
            link = row.get(k)
            if isinstance(link, dict):
                ports = {b: link[b] for b in (GPU, ROOT) if isinstance(link.get(b), dict)} if k == 'links' else {k: link}
                entry[k] = {b: {f: number(v) for f, v in values.items()
                               if f in ('cap', 'sta', 'cap2', 'ctl', 'ctl2') and number(v) is not None}
                            for b, values in ports.items()}
        entry['reason_class'] = event_class(str(row.get('reason', '')))
        entry['semantics'] = 'intent_not_ack' if event.startswith('before_') else 'script_report_not_bus_trace'
        output.append(entry)
        if len(output) >= LIMIT:
            break
    return output


def journal_facts(since, until, runner=run_bounded):
    # _TRANSPORT=kernel spans accessible boots; journalctl -k would imply current boot.
    r = runner(['/usr/bin/journalctl', '--no-pager', '--quiet', '--output=json',
                '--since=' + since, '--until=' + until, '_TRANSPORT=kernel'], limit=4*2**20)
    output = []
    for i, line in enumerate(r['stdout'].splitlines(), 1):
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(row, dict) or not isinstance(row.get('MESSAGE'), str):
            continue
        text = row['MESSAGE']
        if mentions_other_gpu(text) or not re.search(r'(?:(?:0000:)?03:00\.0|0000:03:00|PCI:(?:0000:)?03:00|(?:0000:)?00:02\.0)(?![0-9a-f.])', text, re.I):
            continue
        kind = event_class(text)
        if not kind:
            continue
        event = dict(record=i, kind=kind, target='gpu0_or_root0')
        event['bdfs'] = []
        if re.search(r'(?:0000:)?03:00(?:\.0)?', text, re.I):
            event['bdfs'].append(GPU)
        if re.search(r'(?:0000:)?00:02\.0', text, re.I):
            event['bdfs'].append(ROOT)
        for k in ('__REALTIME_TIMESTAMP', '__MONOTONIC_TIMESTAMP'):
            if isinstance(row.get(k), str) and re.fullmatch(r'\d{1,20}', row[k]):
                event[k] = int(row[k])
        if isinstance(row.get('_BOOT_ID'), str) and re.fullmatch(r'[0-9a-f]{32}', row['_BOOT_ID']):
            event['boot_id'] = row['_BOOT_ID']
        m = re.search(r'Xid\s*\([^)]*\):\s*(\d{1,4})', text, re.I)
        if m:
            event['xid'] = int(m[1])
        if kind == 'aer':
            lowered = text.lower()
            event['severity'] = 'nonfatal' if re.search(r'\bnon-?fatal\b', lowered) else 'fatal' if re.search(r'\bfatal\b', lowered) else 'corrected' if re.search(r'\bcorrected\b', lowered) else 'unknown'
        output.append(event)
        if len(output) >= LIMIT:
            break
    boots = runner(['/usr/bin/journalctl', '--list-boots', '--no-pager', '--quiet'], limit=256*1024)
    boot_rows = []
    for line in boots['stdout'].decode('utf-8', 'replace').splitlines()[-64:]:
        m = re.match(r'^\s*(-?\d+)\s+([0-9a-f]{32})\s+', line)
        if m:
            dates = re.findall(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', line)
            boot_rows.append(dict(index=int(m[1]), boot_id=m[2], displayed_times=dates[:2],
                                  displayed_timezone='UTC (collector subprocess environment)'))
    return dict(events=output, boots=boot_rows, query_status=r['status'], boot_status=boots['status'],
                scope='accessible journal only; missing permissions/rotation/volatile boots may hide events',
                complete_visibility_proven=False, event_limit=LIMIT, boot_limit=64)


def runtime_facts(runner=run_bounded):
    artifacts = {}
    for label, path in (('interpreter', sys.executable), ('_struct', getattr(_struct, '__file__', sys.executable))):
        resolved = Path(path).resolve()
        meta, _ = safe_file(resolved, 256*2**20, digest_only=True)
        artifacts[label] = dict(meta, path=str(resolved).replace(str(Path.home()), '$HOME'),
                                builtin=label in sys.builtin_module_names)
    r = runner(['/usr/bin/dpkg-query', '-W', '-f=${binary:Package}\t${Version}\n',
                'python3.12-minimal', 'libpython3.12-minimal', 'libpython3.12-stdlib'])
    packages = []
    for line in r['stdout'].decode('ascii', 'replace').splitlines()[:10]:
        m = re.fullmatch(r'((?:lib)?python3\.12-(?:minimal|stdlib)(?::amd64)?)\t([0-9][0-9A-Za-z.+:~\-]{0,80})', line)
        if m:
            packages.append(dict(package=m[1], version=m[2]))
    return dict(version='.'.join(map(str, sys.version_info[:3])), implementation=sys.implementation.name,
                artifacts=artifacts, packages=packages, package_status=r['status'],
                semantics='current on-disk artifacts, not crash runtime or proof of loaded binary identity')


def discover(roots):
    sources, logs, gaps, visited = [], [], [], 0
    for root in roots:
        root = Path(os.path.abspath(root))
        if any(part in SKIP_DIRS for part in root.parts):
            gaps.append('private_search_root_refused')
            continue
        if any(root == Path(p) or Path(p) in root.parents for p in DENIED):
            gaps.append('unsafe_search_root_refused')
            continue
        try:
            exists, resolved = root.exists(), root.resolve()
        except (OSError, RuntimeError):
            gaps.append('search_root_unavailable')
            continue
        if not exists:
            continue
        if resolved != root or any(root == Path(p) or Path(p) in root.parents for p in DENIED):
            gaps.append('unsafe_search_root_refused')
            continue
        for directory, dirs, files in os.walk(root, followlinks=False,
                                             onerror=lambda exc: gaps.append('directory_unavailable')):
            depth = len(Path(directory).relative_to(root).parts)
            dirs[:] = [d for d in sorted(dirs) if depth < 5 and d not in SKIP_DIRS and not d.startswith('.')
                       and not (Path(directory)/d).is_symlink()][:64]
            for name in sorted(files)[:2048]:
                visited += 1
                p = Path(directory)/name
                if SOURCE_NAME.fullmatch(name) or p == root/'work'/'original.py':
                    if len(sources) < 48:
                        sources.append(p)
                if name.endswith(('.jsonl', '.log')) and re.search(r'gpu0|root0|p104|gen2', name, re.I):
                    if len(logs) < 48:
                        logs.append(p)
                if visited >= 8192:
                    return list(dict.fromkeys(sources)), list(dict.fromkeys(logs)), gaps + ['scan_limit']
    return list(dict.fromkeys(sources)), list(dict.fromkeys(logs)), gaps


def collect(repo, roots, home, since, until, runner=run_bounded, runtime=None):
    repo, home = Path(os.path.abspath(repo)), Path(os.path.abspath(home))
    if any(repo == Path(p) or Path(p) in repo.parents for p in DENIED) or repo.resolve() != repo:
        raise ValueError('unsafe repository path refused before utility invocation')
    sources, logs, gaps = discover(roots)
    # A single known top-level home filename; never recursively scan all HOME.
    top = home/'p104_gen2_linux.py'
    if top.exists() and top not in sources:
        sources.append(top)
    report = dict(schema=1, collected_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        hardware_access=False, gpu1_access=False, hardware_execution_allowed=False,
        root_escalation=False, window=dict(since=since, until=until),
        runtime=runtime if runtime is not None else runtime_facts(runner),
        sources=[], history=[], logs=[], gaps=gaps, git=git_facts(repo, runner),
        archive_registry=ARCHIVES, journal=journal_facts(since, until, runner))
    report['collector_file'], _ = safe_file(Path(__file__).resolve(), 512*1024, digest_only=True)
    report['search_scope'] = [hint(root, repo, home) for root in roots]
    commit_meta, commit_data = safe_file(repo/'work'/'base.commit', 128*1024)
    report['saved_commit_object'] = dict(commit_meta, execution_proven=False)
    if commit_data is not None:
        commit_id = hashlib.sha1(b'commit ' + str(len(commit_data)).encode() + b'\0' + commit_data).hexdigest()
        report['saved_commit_object']['git_sha1'] = commit_id
        report['saved_commit_object']['known_archive_commit'] = commit_id if commit_id in ARCHIVES.values() else None
    for path in sources[:48]:
        meta, data = safe_file(path, 512*1024)
        entry = dict(meta, location=hint(path, repo, home),
                     absolute_path_sha256=hashlib.sha256(str(Path(os.path.abspath(path))).encode()).hexdigest(),
                     archive_match=ARCHIVES.get(meta.get('sha256')))
        if data is not None:
            entry['source'] = source_facts(data)
        report['sources'].append(entry)
    for name in ('.bash_history', '.zsh_history'):
        meta, data = safe_file(home/name, 8*2**20)
        # Do not persist whole-history hash/stat/content or arbitrary command text.
        report['history'].append(dict(kind=name, status=meta['status'], matches=history_facts(data) if data is not None else [],
                                      coverage='saved history only; current session/disabled history may be absent'))
    for path in logs:
        meta, data = safe_file(path, 2*2**20)
        report['logs'].append(dict(meta, location=hint(path, repo, home),
                                  facts=log_facts(data) if data is not None else []))
    report['assessment'] = dict(executed_crash_writer='Unknown', actual_pcie_store_sequence='Unknown',
        decision='NO-GO', next_hardware_test_authorized=False,
        note='A matching current file or command-history entry does not prove the bytes executed at the crash',
        grades=['Confirmed', 'Strongly supported', 'Possible', 'Unknown'])
    report['findings'] = [
        dict(claim='Current readable copies match an archived source',
             grade='Confirmed' if any(x.get('archive_match') and x['status'] == 'ok' for x in report['sources']) else 'Unknown',
             scope='current snapshot only; not execution'),
        dict(claim='A saved writer-invocation command syntax is present',
             grade='Confirmed' if any(x['kind'] == 'writer_invocation_syntax' for h in report['history'] for x in h['matches']) else 'Unknown',
             scope='saved history record only; not proof that the command ran'),
        dict(claim='The original crash used struct.pack_into on BAR0', grade='Unknown',
             scope='manual correlation of contemporaneous provenance is still required'),
        dict(claim='A transient clear caused the actual GPU loss', grade='Possible',
             scope='source/RAM mechanism; PCIe delivery and crash runtime are not established'),
    ]
    report['coverage'] = dict(depth=5, entries=8192, sources=48, logs=48, matches_per_input=LIMIT,
        history_lines=50000, log_lines=10000, source_bytes=512*1024, log_bytes=2*2**20,
        note='All finite searches are incomplete; absence of matches is not absence of execution')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--search-root', action='append', type=Path, default=[])
    parser.add_argument('--since', default='2026-11-05T19:00:00Z')
    parser.add_argument('--until', default='2026-11-06T03:00:00Z')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error('run as the ordinary user; no sudo/root collection or automatic escalation')
    try:
        start, end = (dt.datetime.fromisoformat(s.replace('Z', '+00:00')) for s in (args.since, args.until))
        if start.tzinfo is None or end.tzinfo is None or not dt.timedelta(0) < end-start <= dt.timedelta(days=7):
            raise ValueError()
    except ValueError:
        parser.error('timezone-aware since/until required, positive window <=7 days')
    # Canonical dates, no journalctl expression or additional match injection.
    since, until = (x.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') for x in (start, end))
    home = Path.home()
    roots = [args.repo, home/'Downloads', home/'Desktop', home/'p104-unlock-v3'] + args.search_root
    try:
        report = collect(args.repo, roots, home, since, until)
    except (ValueError, OSError, RuntimeError):
        parser.error('collection precondition failed; no report written')
    payload = (json.dumps(report, indent=2, ensure_ascii=True) + '\n').encode()
    if len(payload) > 4*2**20:
        parser.error('report size limit exceeded; no report written')
    output = args.output or Path('t5600-linkdown-forensics-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
    output = Path(os.path.abspath(output))
    if output.suffix != '.json' or output.parent.resolve() != output.parent or any(
            output == Path(p) or Path(p) in output.parents for p in DENIED):
        parser.error('ordinary non-symlink output directory and .json filename required')
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
    except OSError:
        parser.error('cannot create output exclusively; existing files are never overwritten')
    print('Created ' + output.name + ' (0600). Review and share this JSON only; do not share full histories.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
