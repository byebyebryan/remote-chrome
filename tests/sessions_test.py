#!/usr/bin/env python3
"""Session ownership and real disposable subprocesses; no real USB or SSH."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

LAUNCHER = Path(__file__).resolve().parents[1] / 'bin/remote-chrome'
SOURCE = LAUNCHER.read_text()
MODULE = SOURCE.split("<<'REMOTE_CHROME_SESSIONS'\n", 1)[1].split('\nREMOTE_CHROME_SESSIONS\n', 1)[0]
s = types.ModuleType('sessions')
exec(compile(MODULE, 'embedded-sessions', 'exec'), s.__dict__)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='remote-chrome-session-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runtime = self.base / 'runtime'
        self.runtime.mkdir(mode=0o700)
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(self.runtime),
                        REMOTE_CHROME_USB_SYSFS_ROOT=str(self.base / 'usb'),
                        REMOTE_CHROME_CONFIG=str(self.base / 'no-config'),
                        REMOTE_CHROME_NOTIFICATIONS='0', XDG_STATE_HOME=str(self.base / 'state'))
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env).start()
        patch.object(s, 'ROOT', self.runtime / f'remote-chrome-sessions-{os.getuid()}').start()
        self.module = self.base / 'sessions.py'
        self.module.write_text(MODULE)
        self.fake = self.base / 'fake launcher'
        self.fake.write_text('''#!/usr/bin/env python3
import json,os,subprocess,sys
from pathlib import Path
base=Path(__file__).parent
if sys.argv[1] == '_session':
    os.execv(sys.executable,[sys.executable,str(base/'sessions.py'),*sys.argv[2:]])
with (base/'calls').open('a') as f:
    f.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1] == '_session-resources' and (base/'fail-cleanup').exists():
    code=int((base/'fail-cleanup').read_text().strip() or '1')
    mask={'session':7,'usb':1,'notifications':2,'bootstrap':4}.get(sys.argv[-1],7)
    sys.exit(code & mask if code < 8 else code)
''')
        self.fake.chmod(0o755)
        self.name = 'remote-chrome-test'
        self.pid = str(os.getpid())

    def reserve(self, name=None, canonical='exec sleep 60', mode='auto', diagnostics='off:30:1073741824:-'):
        name = name or self.name
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            s.reserve([name, 'test-host', canonical, mode, diagnostics, 'lossless', 'test-origin',
                       str(self.fake), self.pid, str(self.runtime / 'usb.sock'), '1050:0407', '3240', ''])
        return output.getvalue().strip()

    def update(self, **values):
        with s.locked(self.name):
            value = s.load(self.name)
            value.update(values)
            s.save(value)
        return value

    def device(self):
        path = self.base / 'usb/3-2'
        path.mkdir(parents=True)
        (path / 'idVendor').write_text('1050\n')
        (path / 'idProduct').write_text('0407\n')
        return path

    def ready_usb(self, generation):
        self.device()
        with contextlib.redirect_stdout(io.StringIO()):
            s.usb_prepare(self.name, generation, self.pid)
        token = s.load(self.name)['usb']['token']
        s.usb_ready(self.name, generation, token, '3-2')
        return token

    def calls(self):
        path = self.base / 'calls'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def shell(self, body):
        prologue = f'''set -euo pipefail
source {shlex.quote(str(LAUNCHER))}
ssh() {{ echo 'UNEXPECTED SSH' >&2; return 97; }}
sudo() {{ echo 'UNEXPECTED SUDO' >&2; return 97; }}
tmux() {{ echo 'UNEXPECTED TMUX' >&2; return 97; }}
'''
        return subprocess.run(['bash', '-c', prologue + body], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def wait_for(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.03)
        self.fail('session transition timed out: ' + json.dumps(s.load(self.name)))

    def supervisor(self, generation):
        self.update(phase='starting')
        p = subprocess.Popen(['python3', str(self.module), 'run', self.name, generation], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def finish():
            try:
                row = s.load(self.name)
                if s.exact(row.get('transport')):
                    os.kill(row['transport']['pid'], signal.SIGTERM)
            except (OSError, ValueError):
                pass
            if p.poll() is None:
                p.terminate()
            try:
                p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate(timeout=2)
        self.addCleanup(finish)
        self.wait_for(lambda: s.load(self.name)['phase'] == 'running')
        return p

    def test_missing_inventory_and_lookup_are_read_only(self):
        self.assertEqual(s.records(), [])
        self.assertEqual(s.main(['known', self.name]), 1)
        self.assertFalse(s.ROOT.exists())

    def test_private_atomic_record_and_literal_settings(self):
        generation = self.reserve(canonical="exec sleep 60 # '$HOME' $(false)")
        row = s.load(self.name)
        self.assertEqual(row['generation'], generation)
        self.assertIn('$(false)', row['command'])
        self.assertEqual(s.ROOT.stat().st_mode & 0o777, 0o700)
        self.assertEqual(s.path_for(self.name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(s.path_for(self.name).stat().st_nlink, 1)

    def test_duplicate_launch_preserves_original_generation(self):
        generation = self.reserve()
        with self.assertRaisesRegex(ValueError, 'active or needs cleanup'):
            self.reserve()
        self.assertEqual(s.load(self.name)['generation'], generation)

    def test_generation_guard_rejects_stale_cleanup(self):
        self.reserve()
        with self.assertRaisesRegex(ValueError, 'generation changed'):
            s.cleanup(self.name, '0' * 32)
        self.assertEqual(self.calls(), [])

    def test_stopped_record_can_be_replaced(self):
        first = self.reserve()
        self.update(phase='stopped')
        second = self.reserve()
        self.assertNotEqual(first, second)
        with self.assertRaises(ValueError):
            s.stop(self.name, first)

    def test_unsafe_directory_is_rejected(self):
        s.ROOT.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, 'unsafe session directory'):
            self.reserve()

    def test_symlink_root_is_rejected(self):
        s.ROOT.symlink_to(self.runtime, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'unsafe session directory'):
            self.reserve()

    def test_symlink_record_is_preserved(self):
        s.private_root(create=True)
        victim = self.base / 'victim'
        victim.write_text('preserve')
        s.path_for(self.name).symlink_to(victim)
        with self.assertRaises(OSError):
            self.reserve()
        self.assertEqual(victim.read_text(), 'preserve')

    def test_hardlinked_record_is_rejected(self):
        self.reserve()
        os.link(s.path_for(self.name), self.base / 'alias')
        with self.assertRaisesRegex(ValueError, 'unsafe session record'):
            s.load(self.name)

    def test_atomic_replacement_between_open_and_stat_reopens_named_record(self):
        self.reserve()
        replacement = s.load(self.name)
        replacement['reason'] = 'transport-failed'
        original_stat = os.fstat
        replaced = False

        def replace_before_stat(fd):
            nonlocal replaced
            if not replaced:
                replaced = True
                s.save(replacement)
                self.assertEqual(original_stat(fd).st_nlink, 0)
            return original_stat(fd)

        with patch.object(s.os, 'fstat', side_effect=replace_before_stat):
            row = s.load(self.name)
        self.assertEqual(row['reason'], 'transport-failed')
        self.assertEqual(s.path_for(self.name).stat().st_nlink, 1)

    def test_generation_replacement_during_json_read_cannot_return_old_view(self):
        generation = self.reserve()
        self.update(phase='stopped')
        original_load = json.load
        replacement = None
        replaced = False

        def replace_during_read(stream):
            nonlocal replacement, replaced
            if not replaced:
                replaced = True
                replacement = self.reserve()
            return original_load(stream)

        with patch.object(s.json, 'load', side_effect=replace_during_read):
            with self.assertRaisesRegex(ValueError, 'generation changed'):
                s.current(self.name, generation)
        self.assertEqual(s.load(self.name)['generation'], replacement)

    def test_world_readable_record_is_rejected(self):
        self.reserve()
        s.path_for(self.name).chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'unsafe session record'):
            s.load(self.name)

    def test_unsafe_lock_is_rejected(self):
        s.private_root(create=True)
        path = s.path_for(self.name).with_suffix('.lock')
        path.symlink_to(self.base / 'victim')
        with self.assertRaises(OSError):
            self.reserve()
        self.assertFalse((self.base / 'victim').exists())

    def test_invalid_name_and_control_command_are_rejected(self):
        for name in ('../victim', '-bad:name', 'line\nfeed', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                s.path_for(name)
        with self.assertRaisesRegex(ValueError, 'invalid session launch'):
            self.reserve(canonical='sleep 1\necho injected')

    def test_pid_start_and_zombie_guard(self):
        identity = s.process(os.getpid())
        self.assertTrue(s.exact(identity))
        self.assertFalse(s.exact(dict(identity, start='wrong')))
        self.assertFalse(s.exact(None))

    def test_reset_lease_blocks_another_launch_across_teardown(self):
        identity = s.process(os.getpid())
        s.claim_lease(self.name, self.pid)
        other = dict(identity, pid=identity['pid'] + 1)
        with patch.object(s, 'process', return_value=other), patch.object(s, 'exact', return_value=True), \
                self.assertRaisesRegex(ValueError, 'another session command owns'):
            self.reserve()
        self.assertFalse(s.path_for(self.name).exists())

    def test_same_reset_owner_can_publish_replacement(self):
        first = self.reserve()
        s.claim_lease(self.name, self.pid)
        self.update(phase='stopped')
        second = self.reserve()
        self.assertNotEqual(first, second)

    def test_dead_reset_lease_does_not_block_new_launch(self):
        s.claim_lease(self.name, self.pid)
        lease = s.path_for(self.name).with_suffix('.lease')
        value = s.read_file(lease)
        value['owner']['start'] = 'old'
        lease.write_text(json.dumps(value))
        self.assertTrue(self.reserve())

    def test_generation_bound_lease_refuses_replacement_before_publishing(self):
        generation = self.reserve()
        self.update(phase='stopped')
        replacement = self.reserve()
        before = s.path_for(self.name).read_bytes()
        with self.assertRaisesRegex(ValueError, 'generation changed'):
            s.claim_lease(self.name, self.pid, generation)
        self.assertFalse(s.path_for(self.name).with_suffix('.lease').exists())
        self.assertEqual(s.path_for(self.name).read_bytes(), before)
        self.assertNotEqual(generation, replacement)

    def test_generation_bound_reads_and_service_check_refuse_replacement(self):
        generation = self.reserve()
        self.update(phase='stopped')
        self.reserve()
        before = s.path_for(self.name).read_bytes()
        for action in ('read', 'read-live', 'check-service'):
            with self.subTest(action=action), patch.object(s, 'service', side_effect=AssertionError('replacement must not be probed')):
                with self.assertRaisesRegex(ValueError, 'generation changed'):
                    s.main([action, self.name, generation])
        self.assertEqual(s.path_for(self.name).read_bytes(), before)

    def replacement_script(self):
        script = self.base / 'replacement.py'
        args = [self.name, 'test-host', 'waypipe --no-gpu ssh test-host echo new-browser',
                'off', 'off:30:1073741824:-', 'lossless', 'test-origin', str(self.fake),
                self.pid, str(self.runtime / 'usb.sock'), '1050:0407', '3240', '']
        script.write_text('import runpy,sys\n' +
                          'm=runpy.run_path(' + repr(str(self.module)) + ')\n' +
                          'args=' + repr(args) + '\nargs[8]=sys.argv[1]\n' +
                          'm["reserve"](args)\n' +
                          'row=m["load"](args[0]);row["phase"]="starting";m["save"](row)\n')
        return script

    def test_reset_replacement_before_lease_is_preserved_without_remote_probe(self):
        generation = self.reserve(canonical='waypipe --no-gpu ssh test-host echo old-browser', mode='off')
        self.update(phase='stopped', reason='transport-failed')
        script = self.replacement_script()
        p = self.shell('''
chrome_service_preflight() { :; }
eval "$(declare -f chrome_session_manager | sed '1s/chrome_session_manager/original_manager/')"
chrome_session_manager() {
  if [ "$1" = check-service ]; then
    original_manager read "$2" "${3:-}" >/dev/null || return
    python3 ''' + shlex.quote(str(script)) + ' ' + self.pid + ''' >/dev/null
    return
  fi
  if [ "$1" = stop ]; then echo WOULD-STOP-REPLACEMENT >&2; return 97; fi
  original_manager "$@"
}
chrome_reset_remote_identity() { echo WOULD-PROBE-REMOTE >&2; return 1; }
chrome_reset test-host --session remote-chrome-test --yes
''')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('generation changed', p.stderr)
        self.assertNotIn('WOULD-', p.stdout + p.stderr)
        row = s.load(self.name)
        self.assertNotEqual(row['generation'], generation)
        self.assertEqual(row['phase'], 'starting')
        self.assertIn('new-browser', row['command'])
        self.assertFalse(s.path_for(self.name).with_suffix('.lease').exists())

    def test_reset_rechecks_original_generation_before_local_stop(self):
        generation = self.reserve(canonical='waypipe --no-gpu ssh test-host echo old-browser', mode='off')
        self.update(phase='stopped', reason='transport-failed')
        script = self.replacement_script()
        p = self.shell('''
chrome_service_preflight() { :; }
eval "$(declare -f chrome_session_manager | sed '1s/chrome_session_manager/original_manager/')"
chrome_session_manager() {
  if [ "$1" = check-service ]; then original_manager read "$2" "${3:-}" >/dev/null; return; fi
  if [ "$1" = stop ]; then echo WOULD-STOP-REPLACEMENT >&2; return 97; fi
  original_manager "$@"
}
chrome_validate_launch_artifacts() { :; }
chrome_reset_remote_identity() {
  # The reset owner can legally reserve a replacement under its own lease.
  # Even then, the old reset must not switch its captured generation to it.
  python3 ''' + shlex.quote(str(script)) + ''' "$BASHPID" >/dev/null
  return 1
}
chrome_reset test-host --session remote-chrome-test --yes
''')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('generation changed', p.stderr)
        self.assertNotIn('WOULD-STOP', p.stdout + p.stderr)
        self.assertNotEqual(s.load(self.name)['generation'], generation)
        self.assertEqual(s.load(self.name)['phase'], 'starting')

    def test_reset_rechecks_original_generation_before_remote_stop(self):
        generation = self.reserve(canonical='waypipe --no-gpu ssh test-host echo old-browser', mode='off')
        self.update(phase='stopped', reason='transport-failed')
        script = self.replacement_script()
        p = self.shell('''
chrome_service_preflight() { :; }
eval "$(declare -f chrome_session_manager | sed '1s/chrome_session_manager/original_manager/')"
chrome_session_manager() {
  if [ "$1" = check-service ]; then original_manager read "$2" "${3:-}" >/dev/null; return; fi
  original_manager "$@"
}
chrome_validate_launch_artifacts() { :; }
chrome_reset_remote_identity() {
  python3 ''' + shlex.quote(str(script)) + ''' "$BASHPID" >/dev/null
  chrome_reset_remote_pid=123 chrome_reset_remote_pgid=123
  return 0
}
chrome_reset_remote_stop() { echo WOULD-STOP-REMOTE >&2; return 97; }
chrome_reset test-host --session remote-chrome-test --yes
''')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('generation changed', p.stderr)
        self.assertNotIn('WOULD-STOP', p.stdout + p.stderr)
        self.assertNotEqual(s.load(self.name)['generation'], generation)

    def test_live_supervisor_cannot_be_cleaned_by_old_hook(self):
        generation = self.reserve()
        self.update(phase='running', supervisor=s.process(os.getpid()))
        with self.assertRaisesRegex(ValueError, 'still running'):
            s.cleanup(self.name, generation)
        self.assertEqual(self.calls(), [])

    def test_failed_cleanup_keeps_retryable_record(self):
        generation = self.reserve()
        self.update(phase='running')
        (self.base / 'fail-cleanup').write_text('4')
        self.assertEqual(s.cleanup(self.name, generation), 1)
        self.assertEqual(s.load(self.name)['phase'], 'cleanup-failed')
        (self.base / 'fail-cleanup').unlink()
        self.assertEqual(s.cleanup(self.name, generation), 0)
        self.assertEqual(s.load(self.name)['phase'], 'stopped')

    def test_failed_transport_keeps_original_end_and_notifies_once(self):
        generation = self.reserve(canonical="sleep .2; exit 7")
        p = self.supervisor(generation)
        p.communicate(timeout=5)
        row = s.load(self.name)
        self.assertEqual(row['transport_exit'], 7)
        self.assertEqual(row['reason'], 'transport-failed')
        ended = row['ended']
        (self.base / 'fail-cleanup').write_text('4')
        self.assertEqual(s.cleanup(self.name, generation), 1)
        (self.base / 'fail-cleanup').unlink()
        self.assertEqual(s.cleanup(self.name, generation), 0)
        row = s.load(self.name)
        self.assertEqual(row['ended'], ended)
        self.assertEqual(row['reason'], 'transport-failed')
        self.assertTrue(s.failed(row))
        notices = [r for r in self.calls() if r[0] == '_session-failure-notice']
        self.assertEqual(len(notices), 1)
        self.assertEqual(s.main(['recoverable', self.name]), 0)

    def test_normal_exit_has_no_failure_notice(self):
        generation = self.reserve(canonical='exec sleep .2')
        p = self.supervisor(generation)
        p.communicate(timeout=5)
        self.assertEqual(s.cleanup(self.name, generation), 0)
        self.assertFalse(s.failed(s.load(self.name)))
        self.assertFalse(any(r[0] == '_session-failure-notice' for r in self.calls()))

    def test_post_stop_hook_records_supervisor_failure_and_notifies_once(self):
        generation = self.reserve()
        self.update(phase='running')
        with patch.dict(os.environ, SERVICE_RESULT='signal'):
            self.assertEqual(s.cleanup(self.name, generation), 0)
            self.assertEqual(s.cleanup(self.name, generation), 0)
        self.assertEqual(s.load(self.name)['reason'], 'supervisor-failed')
        self.assertEqual(len([row for row in self.calls() if row[0] == '_session-failure-notice']), 1)

    def test_superseded_failure_notice_does_not_mutate_replacement(self):
        old = self.reserve()
        self.update(phase='stopped', reason='transport-failed')
        replacement = self.reserve()
        s.failure_notice(self.name, old)
        row = s.load(self.name)
        self.assertEqual(row['generation'], replacement)
        self.assertFalse(row.get('failure_notice_attempted'))
        self.assertEqual(self.calls(), [])

    def test_remote_socket_reference_survives_transport_exit(self):
        self.fake.write_text(self.fake.read_text() + "\nif sys.argv[1] == '_session-socket': print('/tmp/fixture-server.sock')\n")
        generation = self.reserve(canonical='exec sleep .3')
        process = self.supervisor(generation)
        process.communicate(timeout=5)
        self.assertEqual(s.cleanup(self.name, generation), 0)
        row = s.load(self.name)
        self.assertEqual(row['remote_socket'], '/tmp/fixture-server.sock')
        self.assertFalse(s.exact(row['transport']))

    def test_removed_key_turns_off_intent_even_when_cleanup_is_pending(self):
        generation = self.reserve(mode='force')
        token = self.ready_usb(generation)
        self.update(phase='running')
        shutil.rmtree(self.base / 'usb/3-2')
        (self.base / 'fail-cleanup').write_text('1')
        self.assertEqual(s.usb_cleanup(self.name, generation, token, 'device-removed'), 1)
        row = s.load(self.name)
        self.assertEqual(row['phase'], 'running')
        self.assertEqual(row['usb']['state'], 'cleanup-failed')
        self.assertFalse(row['usb']['wanted'])
        self.device()
        self.assertFalse(s.usb_wanted(s.load(self.name)))

    def test_usb_stop_latches_off_even_when_forwarding_is_already_off(self):
        generation = self.reserve()
        row = s.load(self.name)
        row['usb']['wanted'] = True
        s.save(row)
        self.assertEqual(s.usb_cleanup(self.name, generation, '', 'requested'), 0)
        self.assertFalse(s.load(self.name)['usb']['wanted'])
        row = s.load(self.name)
        row['usb'].update(wanted=True, reason='device-removed')
        self.assertFalse(s.usb_wanted(row), 'older supervisors must not re-enable a removed key')

    def test_cleanup_deadline_preserves_usb_and_finishes_other_resources(self):
        generation = self.reserve()
        self.ready_usb(generation)
        self.update(phase='running', notify_socket='/private/notify.sock')
        raw = self.fake.read_text()
        self.fake.write_text(raw + '''
if sys.argv[1] == '_session-resources' and sys.argv[-1] == 'usb':
    child=subprocess.Popen(['sleep','60'])
    (base/'slow-child').write_text(str(child.pid))
    import time
    time.sleep(60)
''')
        started = time.monotonic()
        with patch.object(s, 'CLEANUP_SECONDS', 1):
            self.assertEqual(s.cleanup(self.name, generation), 1)
        self.assertLess(time.monotonic() - started, 3)
        row = s.load(self.name)
        self.assertEqual(row['cleanup_failures'], ['usb'])
        self.assertEqual(row['usb']['state'], 'cleanup-failed')
        scopes = [r[-1] for r in self.calls() if r[0] == '_session-resources']
        self.assertEqual(scopes, ['notifications', 'bootstrap', 'usb'])
        self.assertIsNone(s.process(int((self.base / 'slow-child').read_text())))

    def test_cleanup_deadline_contains_real_timeout_wrapped_requests(self):
        generation = self.reserve()
        token = self.ready_usb(generation)
        private_bin = self.base / 'bin'
        private_bin.mkdir()
        child_file = self.base / 'ssh-child.json'
        fake_ssh = private_bin / 'ssh'
        fake_ssh.write_text('''#!/usr/bin/env python3
import json,os,time
from pathlib import Path
pid=os.getpid()
start=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
Path(''' + repr(str(child_file)) + ''').write_text(json.dumps(dict(pid=pid,start=start,pgid=os.getpgrp())))
time.sleep(60)
''')
        fake_ssh.chmod(0o755)
        notify_state = self.base / 'notify.state'
        notify_state.write_text('private fixture\n')
        notify_state.chmod(0o600)
        self.fake.write_text('#!/bin/bash\nsource ' + shlex.quote(str(LAUNCHER)) + '\n' + '''
[ "$1" = _session-resources ] || exit 0
shift
yk_prepare_for_launch() { :; }
yk_load_state() { yk_attempt_token=''' + shlex.quote(token) + '''; }
yk_stop() { yk_ssh_request fixture.invalid true; }
notify_set_runtime_paths() { notify_state_file=''' + shlex.quote(str(notify_state)) + '''; }
notify_state_field() { printf '%s\\n' /private/notify.sock; }
notify_stop_listener() { notify_remove_remote_socket fixture.invalid /private/notify.sock; }
chrome_remove_bootstrap_script() { :; }
_chrome_session_resources "$@"
''')
        row = self.update(notify_socket='/private/notify.sock')
        row['environment'].update(PATH=str(private_bin) + ':' + os.environ['PATH'], REMOTE_CHROME_SSH_TIMEOUT='60')
        unrelated = subprocess.Popen(['sleep', '60'], start_new_session=True)
        self.addCleanup(lambda: (unrelated.terminate(), unrelated.wait(timeout=3)))
        for scope in ('usb', 'notifications'):
            with self.subTest(scope=scope):
                child_file.unlink(missing_ok=True)
                try:
                    started = time.monotonic()
                    self.assertEqual(s.resource_call(row, scope, time.monotonic() + .7), {scope})
                    self.assertLess(time.monotonic() - started, 2)
                    child = json.loads(child_file.read_text())
                    self.assertIsNone(s.process(child['pid']), 'deadline left the timeout-wrapped SSH child alive')
                    self.assertIsNone(unrelated.poll(), 'cleanup signalled a process outside its worker')
                finally:
                    if child_file.exists():
                        child = json.loads(child_file.read_text())
                        if s.process(child['pid']) == {'pid': child['pid'], 'start': child['start']}:
                            os.killpg(child['pgid'], signal.SIGKILL)

    def test_reset_ended_forced_session_without_key_replays_settings(self):
        canonical = 'waypipe-remote-chrome --compress zstd=1 --video h264,hw,bpf=500000 --remote-bin waypipe-remote-chrome ssh test-host echo custom-browser'
        generation = self.reserve(canonical=canonical, mode='force')
        token = self.ready_usb(generation)
        shutil.rmtree(self.base / 'usb/3-2')
        s.usb_cleanup(self.name, generation, token, 'device-removed')
        self.update(phase='stopped', reason='transport-failed', transport_exit=255, ended=time.time())
        p = self.shell('''
chrome_service_preflight() { :; }
eval "$(declare -f chrome_session_manager | sed '1s/chrome_session_manager/original_manager/')"
chrome_session_manager() { if [ "$1" = check-service ]; then return 0; fi; original_manager "$@"; }
chrome_video_compatibility() { :; }
chrome_validate_launch_artifacts() { :; }
chrome_reset_find_local_ssh() { echo WOULD-REQUIRE-SSH >&2; return 97; }
chrome_reset_remote_identity() { return 1; }
yk_preflight_for_forwarding() { echo WOULD-REQUIRE-KEY >&2; return 97; }
chrome_service_launch() { printf 'FORWARD=%s COMMAND=%s\\n' "$4" "$3"; }
chrome_reset test-host --session remote-chrome-test --yes
''')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('FORWARD=0', p.stdout)
        self.assertIn('h264,hw,bpf=500000', p.stdout)
        self.assertIn('custom-browser', p.stdout)
        self.assertIn('ServerAliveInterval=15', p.stdout)
        self.assertNotIn('WOULD-', p.stdout + p.stderr)

    def test_failed_ended_session_is_in_inspection_but_not_active_inventory(self):
        self.reserve()
        self.update(phase='stopped', reason='transport-failed', transport_exit=1, ended=time.time())
        active = io.StringIO()
        with contextlib.redirect_stdout(active):
            s.main(['names'])
        self.assertEqual(active.getvalue(), '')
        inspect = io.StringIO()
        with contextlib.redirect_stdout(inspect):
            s.main(['names', 'inspect'])
        self.assertIn(self.name, inspect.getvalue())

    def test_ended_reset_restores_notifications_after_listener_state_is_removed(self):
        self.reserve(canonical='waypipe --no-gpu --compress zstd=1 ssh -R /tmp/old.sock:/private/old.sock test-host echo browser')
        self.update(phase='stopped', reason='transport-failed', notify_socket='/private/old.sock',
                    notification=dict(remote_socket='/tmp/old.sock', apps='work-browser'))
        p = self.shell('''chrome_service_preflight() { :; }
eval "$(declare -f chrome_session_manager | sed '1s/chrome_session_manager/original_manager/')"
chrome_session_manager() { if [ "$1" = check-service ]; then return 0; fi; original_manager "$@"; }
chrome_validate_launch_artifacts() { :; }
chrome_reset_remote_identity() { return 1; }
chrome_service_launch() { printf 'NOTIFY=%s|%s|%s\\n' "$notify_local_socket" "$notify_remote_socket" "$notify_apps"; }
chrome_reset test-host --session remote-chrome-test --yes
''')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('NOTIFY=/private/old.sock|/tmp/old.sock|work-browser', p.stdout)

    def test_older_record_recovers_known_notification_route_and_apps(self):
        self.reserve()
        row = s.load(self.name)
        row.pop('notification')
        row['notify_socket'] = '/run/user/1000/remote-chrome-notify-1.2.3.sock'
        row['environment']['REMOTE_CHROME_NOTIFICATION_APPS'] = 'custom'
        s.save(row)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            s.read_metadata(s.load(self.name))
        fields = output.getvalue().splitlines()
        self.assertEqual(fields[-2:], ['/tmp/remote-chrome-notify-1.2.3.sock', 'custom'])

    def test_unavailable_notification_helper_does_not_mark_cleaned_usb_failed(self):
        generation = self.reserve()
        self.ready_usb(generation)
        self.update(phase='running', notify_socket='/private/notify.sock')
        self.fake.write_text(self.fake.read_text() + "\nif sys.argv[1] == '_session-resources' and sys.argv[-1] == 'notifications': sys.exit(64)\n")
        self.assertEqual(s.cleanup(self.name, generation), 1)
        row = s.load(self.name)
        self.assertEqual(row['cleanup_failures'], ['notifications'])
        self.assertEqual(row['usb']['state'], 'off')

    def test_notification_failure_keeps_unused_usb_off_and_stop_can_retry(self):
        generation = self.reserve()
        self.update(phase='running', notify_socket='/private/notify.sock')
        (self.base / 'fail-cleanup').write_text('2')
        self.assertEqual(s.cleanup(self.name, generation), 1)
        row = s.load(self.name)
        self.assertEqual(row['cleanup_failures'], ['notifications'])
        self.assertEqual(row['usb']['state'], 'off')
        self.assertEqual(row['usb']['reason'], 'not-requested')
        (self.base / 'fail-cleanup').unlink()
        with patch.object(s, 'service', return_value=False):
            self.assertEqual(s.stop(self.name, generation), 0)
        self.assertEqual(s.load(self.name)['phase'], 'stopped')
        self.assertEqual(s.load(self.name)['cleanup_failures'], [])

    def test_bootstrap_failure_does_not_hide_successful_usb_cleanup(self):
        generation = self.reserve()
        self.ready_usb(generation)
        self.update(phase='running')
        (self.base / 'fail-cleanup').write_text('4')
        self.assertEqual(s.cleanup(self.name, generation), 1)
        row = s.load(self.name)
        self.assertEqual(row['phase'], 'cleanup-failed')
        self.assertEqual(row['cleanup_failures'], ['bootstrap'])
        self.assertEqual(row['usb']['state'], 'off')

    def test_multiple_cleanup_failures_remain_specific(self):
        generation = self.reserve()
        self.ready_usb(generation)
        self.update(phase='running', notify_socket='/private/notify.sock')
        (self.base / 'fail-cleanup').write_text('3')
        self.assertEqual(s.cleanup(self.name, generation), 1)
        row = s.load(self.name)
        self.assertEqual(row['cleanup_failures'], ['notifications', 'usb'])
        self.assertEqual(row['usb']['state'], 'cleanup-failed')

    def test_unknown_or_legacy_cleanup_result_is_not_attributed_to_unused_usb(self):
        generation = self.reserve()
        self.update(phase='running')
        for code in (64, 1):
            with self.subTest(code=code):
                (self.base / 'fail-cleanup').write_text(str(code))
                if code == 1:
                    with s.locked(self.name):
                        row = s.load(self.name)
                        row.pop('resource_protocol')
                        s.save(row)
                self.assertEqual(s.cleanup(self.name, generation), 1)
                row = s.load(self.name)
                self.assertEqual(row['cleanup_failures'], ['bootstrap'] if code == 64 else ['unknown'])
                self.assertEqual(row['usb']['state'], 'off')

    def test_resource_helper_attempts_every_resource_and_reports_combined_failure(self):
        p = self.shell('''
yk_prepare_for_launch() { :; }
yk_load_state() { yk_attempt_token=token; }
runtime_path_identity() { printf identity; }
yk_stop() { echo USB-ATTEMPT; return 1; }
notify_set_runtime_paths() { notify_state_file=/private/notify; }
runtime_artifact_status() { return 0; }
notify_state_field() { printf notify.sock; }
notify_stop_listener() { echo NOTIFY-ATTEMPT; return 1; }
chrome_remove_bootstrap_script() { echo BOOTSTRAP-ATTEMPT; return 1; }
_chrome_session_resources test-host remote-chrome-test token usb.sock 1050:0407 3240 notify.sock session
''')
        self.assertEqual(p.returncode, 7, p.stderr)
        for resource in ('USB', 'NOTIFY', 'BOOTSTRAP'):
            self.assertIn(resource + '-ATTEMPT', p.stdout)
        for resource in ('USB', 'notification', 'bootstrap'):
            self.assertIn(resource + ' cleanup incomplete', p.stderr)

    def test_cleanup_is_idempotent_and_passes_exact_resources(self):
        generation = self.reserve()
        token = self.ready_usb(generation)
        self.update(phase='running')
        self.assertEqual(s.cleanup(self.name, generation), 0)
        self.assertEqual(s.cleanup(self.name, generation), 0)
        calls = [r for r in self.calls() if r[0] == '_session-resources']
        self.assertEqual(len(calls), 2)
        usb = next(r for r in calls if r[-1] == 'usb')
        self.assertEqual(usb[1:4], ['test-host', self.name, token])
        self.assertEqual(usb[-2:], ['-', 'usb'])
        self.assertEqual(calls[0][-1], 'bootstrap')

    def test_usb_start_reservation_is_exclusive(self):
        generation = self.reserve()
        with contextlib.redirect_stdout(io.StringIO()):
            s.usb_prepare(self.name, generation, self.pid)
        with self.assertRaisesRegex(ValueError, 'active or needs cleanup'):
            s.usb_prepare(self.name, generation, self.pid)

    def test_usb_ready_rejects_browser_exit_during_setup(self):
        generation = self.reserve()
        self.device()
        with contextlib.redirect_stdout(io.StringIO()):
            s.usb_prepare(self.name, generation, self.pid)
        token = s.load(self.name)['usb']['token']
        self.update(phase='stopping')
        with self.assertRaisesRegex(ValueError, 'ended or USB ownership changed'):
            s.usb_ready(self.name, generation, token, '3-2')

    def test_usb_ready_rejects_missing_device(self):
        generation = self.reserve()
        with contextlib.redirect_stdout(io.StringIO()):
            s.usb_prepare(self.name, generation, self.pid)
        with self.assertRaisesRegex(ValueError, 'disappeared or is unreadable'):
            s.usb_ready(self.name, generation, s.load(self.name)['usb']['token'], '3-2')

    def test_usb_stop_does_not_stop_session_or_notifications(self):
        generation = self.reserve()
        token = self.ready_usb(generation)
        self.assertEqual(s.usb_cleanup(self.name, generation, token, 'requested'), 0)
        row = s.load(self.name)
        self.assertEqual(row['phase'], 'preparing')
        self.assertEqual(row['usb']['state'], 'off')
        self.assertEqual(self.calls()[-1][-2:], ['-', 'usb'])

    def test_old_usb_token_cannot_stop_replacement(self):
        generation = self.reserve()
        self.ready_usb(generation)
        self.assertEqual(s.usb_cleanup(self.name, generation, 'older-token', 'requested'), 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(s.load(self.name)['usb']['state'], 'ready')

    def test_usb_cleanup_failure_leaves_browser_running(self):
        generation = self.reserve()
        token = self.ready_usb(generation)
        self.update(phase='running', supervisor=s.process(os.getpid()))
        (self.base / 'fail-cleanup').touch()
        self.assertEqual(s.usb_cleanup(self.name, generation, token, 'device-removed'), 1)
        self.assertEqual(s.load(self.name)['phase'], 'running')
        self.assertEqual(s.load(self.name)['usb']['state'], 'cleanup-failed')

    def test_unreadable_local_device_is_unknown_not_removed(self):
        generation = self.reserve()
        self.ready_usb(generation)
        with patch.object(Path, 'read_text', side_effect=PermissionError('denied')):
            self.assertIsNone(s.usb_identity(s.load(self.name)))

    def test_unit_arguments_escape_specifiers_and_expansion(self):
        self.assertEqual(s.unit_arg('path with $HOME %n "quote"'), '"path with $$HOME %%n \\"quote\\""')
        with self.assertRaises(ValueError):
            s.unit_arg('line\nfeed')

    def test_service_identity_spoof_is_rejected(self):
        self.reserve()
        response = subprocess.CompletedProcess([], 0, 'ActiveState=active\nMainPID=123\nDescription=other\nTransient=yes\n', '')
        with patch.object(s, 'command', return_value=response), self.assertRaisesRegex(ValueError, 'identity changed'):
            s.service(s.load(self.name))

    def test_service_pid_reuse_is_rejected(self):
        generation = self.reserve()
        row = self.update(supervisor=s.process(os.getpid()), phase='running')
        response = subprocess.CompletedProcess([], 0, f'ActiveState=active\nMainPID=123\nDescription=remote-chrome session {generation}\nTransient=yes\n', '')
        with patch.object(s, 'command', return_value=response), self.assertRaisesRegex(ValueError, 'process identity changed'):
            s.service(row)

    def test_start_freezes_environment_and_installs_cleanup_hook(self):
        generation = self.reserve()
        commands = []
        def execute(argv, **kwargs):
            commands.append(argv)
            self.update(phase='running', supervisor=s.process(os.getpid()))
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(s, 'command', side_effect=execute):
            s.start(self.name, generation)
        argv = commands[0]
        self.assertIn('--expand-environment=no', argv)
        self.assertIn('--collect', argv)
        self.assertIn('--property=Restart=no', argv)
        self.assertTrue(any(a.startswith('--property=ExecStopPost=') and generation in a for a in argv))
        self.assertIn('--setenv=XDG_RUNTIME_DIR=' + str(self.runtime), argv)

    def test_legacy_stop_holds_lease_through_shared_resource_cleanup(self):
        body = """
chrome_remove_bootstrap_script() { :; }
notify_set_runtime_paths() { :; }
notify_stop_listener() { :; }
yk_prepare_for_launch() { :; }
yk_stop() { :; }
tmux() {
  case "$1" in
    has-session) return 0 ;;
    kill-session)
      if chrome_session_manager lease remote-chrome-test "$OTHER_OWNER"; then
        echo 'competing launch was admitted' >&2
        return 97
      fi
      return 0 ;;
  esac
}
chrome_stop test-host --session remote-chrome-test
"""
        with patch.dict(os.environ, OTHER_OWNER=self.pid):
            self.env['OTHER_OWNER'] = self.pid
            p = self.shell(body)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('another session command owns this session', p.stderr)
        self.assertNotIn('competing launch was admitted', p.stderr)

    def test_requested_supervisor_stop_is_successful(self):
        generation = self.reserve()
        p = self.supervisor(generation)
        p.terminate()
        out, err = p.communicate(timeout=5)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(s.load(self.name)['reason'], 'requested')

    def test_normal_transport_exit_records_reason_and_allows_cleanup(self):
        generation = self.reserve(canonical='exec sleep .2')
        p = self.supervisor(generation)
        out, err = p.communicate(timeout=5)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(s.load(self.name)['reason'], 'transport-exited')
        self.assertEqual(s.cleanup(self.name, generation), 0)
        self.assertEqual(s.load(self.name)['phase'], 'stopped')

    def test_unplug_stops_only_usb_and_replug_stays_off(self):
        generation = self.reserve()
        self.ready_usb(generation)
        p = self.supervisor(generation)
        child = s.load(self.name)['transport']
        shutil.rmtree(self.base / 'usb/3-2')
        self.wait_for(lambda: s.load(self.name)['usb']['state'] == 'off')
        self.assertIsNone(p.poll())
        self.assertTrue(s.exact(child))
        self.assertEqual(s.load(self.name)['usb']['reason'], 'device-removed')
        self.device()
        time.sleep(.7)
        self.assertEqual(s.load(self.name)['usb']['state'], 'off')
        self.assertEqual(len([r for r in self.calls() if r[0] == '_session-resources']), 1)

    def test_usb_setup_owner_exit_is_cleaned_without_stopping_browser(self):
        generation = self.reserve()
        with contextlib.redirect_stdout(io.StringIO()):
            s.usb_prepare(self.name, generation, self.pid)
        with s.locked(self.name):
            row = s.load(self.name)
            row['usb']['setup_owner']['start'] = 'old'
            s.save(row)
        p = self.supervisor(generation)
        self.wait_for(lambda: s.load(self.name)['usb']['state'] == 'off')
        self.assertIsNone(p.poll())
        self.assertEqual(s.load(self.name)['usb']['reason'], 'setup-owner-exited')

    def test_lifecycle_events_are_saved_without_marking(self):
        attempt = '22222222-2222-4222-8222-222222222222'
        p = subprocess.run([str(LAUNCHER), '_session-event', 'test-host', self.name, attempt,
                            'usb_stopped', 'device-removed', self.pid, s.process(self.pid)['start'], '30', '1073741824',
                            '', 'a' * 32],
                           env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0, p.stderr)
        store = self.base / 'state/remote-chrome/diagnostics'
        records = '\n'.join(path.read_text() for path in store.rglob('*.jsonl'))
        self.assertIn('usb_stopped', records)
        self.assertIn('device-removed', records)
        self.assertIn('"session_generation":"' + 'a' * 32 + '"', records)

    def test_background_resource_cleanup_never_prompts(self):
        script = f'''source {shlex.quote(str(LAUNCHER))}
yk_prepare_for_launch() {{ :; }}
yk_load_state() {{ yk_attempt_token=token; return 0; }}
yk_attempt_lock={shlex.quote(str(self.runtime))}
yk_stop() {{ sudo usbip unbind -b 3-2; }}
_chrome_session_resources test-host remote-chrome-test token /unused 1050:0407 3240 -
'''
        fake_bin = self.base / 'bin'
        fake_bin.mkdir()
        sudo = fake_bin / 'sudo'
        sudo.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        sudo.chmod(0o755)
        env = dict(self.env, PATH=str(fake_bin) + ':' + os.environ['PATH'])
        p = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.splitlines(), ['-n', '-k', 'usbip', 'unbind', '-b', '3-2'])

    def test_duplicate_cannot_publish_bootstrap_stop_browser_or_start_listener(self):
        self.reserve()
        p = self.shell('''chrome_transport_defaults
chrome_diag_policy=lossless
chrome_prepare_bootstrap_script() { echo 'WOULD PUBLISH'; return 1; }
chrome_handle_existing() { echo 'WOULD STOP BROWSER'; }
notify_start_listener() { echo 'WOULD START LISTENER'; }
chrome_service_launch test-host remote-chrome-test 'exec sleep 60' 0 0 0 google-chrome
''')
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn('WOULD', p.stdout)
        self.assertIn('active or needs cleanup', p.stderr)

    def test_service_status_works_without_tmux_or_remote_probes(self):
        self.reserve()
        self.update(phase='running', supervisor=s.process(os.getpid()))
        p = self.shell('''chrome_transport_status() { chrome_ui_check Video Lossless recorded; }
chrome_status_diagnostics() { :; }
yk_load_state() { return 1; }
notify_status_current() { return 0; }
chrome_status_current test-host remote-chrome-test
''')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('Background session running', p.stdout)
        self.assertIn('No managed forwarding', p.stdout)
        self.assertNotIn('UNEXPECTED', p.stderr)

    def test_monitor_inventory_includes_normal_usb_removal(self):
        generation = self.reserve()
        token = self.ready_usb(generation)
        s.usb_cleanup(self.name, generation, token, 'device-removed')
        self.update(phase='running', supervisor=s.process(os.getpid()))
        p = self.shell('''tmux() { echo 'no server running on fixture' >&2; return 1; }
chrome_monitor_inventory
''')
        self.assertEqual(p.returncode, 0, p.stderr)
        fields = p.stdout.strip().split('\t')
        self.assertEqual(len(fields), 12)
        self.assertEqual(fields[6:10], ['running', 'off', 'device-removed', '-'])

    def test_status_reports_notification_cleanup_without_usb_failure(self):
        self.reserve()
        self.update(phase='cleanup-failed', cleanup_failures=['notifications'])
        p = self.shell('''chrome_transport_status() { :; }
chrome_status_diagnostics() { :; }
yk_load_state() { return 1; }
notify_status_current() { return 0; }
chrome_status_current test-host remote-chrome-test
''')
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn('notifications / retry stop', p.stdout)
        self.assertIn('off / not-requested', p.stdout)
        self.assertNotIn('USB cleanup', p.stdout + p.stderr)

    def test_explicit_service_stop_cannot_fall_through_to_unqualified_usb_stop(self):
        self.reserve()
        p = self.shell('''eval "$(declare -f chrome_session_manager | sed '1s/chrome_session_manager/original_manager/')"
chrome_session_manager() { if [ "$1" = stop ]; then return 0; fi; original_manager "$@"; }
yk_stop() { echo 'UNQUALIFIED USB STOP'; return 97; }
chrome_stop test-host --session remote-chrome-test
''')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('UNQUALIFIED', p.stdout)

    def test_usb_cli_stop_uses_recorded_target_and_keeps_session(self):
        generation = self.reserve()
        self.ready_usb(generation)
        self.update(phase='running', supervisor=s.process(os.getpid()))
        p = self.shell('chrome_usb stop test-host --session remote-chrome-test\n')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('Chrome kept running', p.stdout)
        self.assertEqual(s.load(self.name)['phase'], 'running')
        self.assertEqual(s.load(self.name)['usb']['state'], 'off')

    def test_usb_cli_rejects_wrong_host_before_cleanup(self):
        self.reserve()
        p = self.shell('chrome_usb stop other-host --session remote-chrome-test\n')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('different host', p.stderr)
        self.assertEqual(self.calls(), [])

    def test_new_session_attach_explains_logs_without_tmux(self):
        self.reserve()
        p = self.shell('chrome_attach test-host --session remote-chrome-test\n')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('logs test-host', p.stdout)
        self.assertNotIn('UNEXPECTED', p.stderr)

    def test_stopping_waits_for_inflight_usb_setup_before_resources(self):
        generation = self.reserve()
        with contextlib.redirect_stdout(io.StringIO()):
            s.usb_prepare(self.name, generation, self.pid)
        self.update(phase='running')
        released = threading.Event()
        original = s.exact
        def settle():
            time.sleep(.15)
            with s.locked(self.name):
                row = s.load(self.name)
                self.assertEqual(row['phase'], 'stopping')
                self.assertFalse(any(r[-1] == 'usb' for r in self.calls() if r[0] == '_session-resources'))
                row['usb']['state'] = 'off'
                s.save(row)
            released.set()
        thread = threading.Thread(target=settle)
        thread.start()
        with patch.object(s, 'exact', side_effect=lambda identity: original(identity)):
            self.assertEqual(s.cleanup(self.name, generation), 0)
        thread.join(timeout=2)
        self.assertTrue(released.is_set())

    def test_cleanup_rejects_terminal_control_in_usb_reason(self):
        generation = self.reserve()
        with self.assertRaisesRegex(ValueError, 'invalid USB stop reason'):
            s.usb_cleanup(self.name, generation, 'token', '\x1b[2J')
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
