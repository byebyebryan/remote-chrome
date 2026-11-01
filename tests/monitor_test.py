#!/usr/bin/env python3
"""Read-only monitor model and real PTY lifecycle/interaction checks."""
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import socket
import struct
import subprocess
import tempfile
import termios
import time
import types
import unittest
from unittest.mock import patch
import uuid

LAUNCHER = Path(__file__).resolve().parents[1] / 'bin/remote-chrome'
SOURCE = LAUNCHER.read_text().split("<<'REMOTE_CHROME_DIAGNOSTICS'\n", 1)[1].split('\nREMOTE_CHROME_DIAGNOSTICS\n', 1)[0]
d = types.ModuleType('diagnostics')
exec(compile(SOURCE, 'embedded-diagnostics', 'exec'), d.__dict__)
ATTEMPT = '11111111-1111-4111-8111-111111111111'
SECOND = '22222222-2222-4222-8222-222222222222'


def sample(**values):
    value = dict(schema=2, event='sample', attempt=ATTEMPT, role='viewer',
                 origin=socket.gethostname(), target='snap', session='remote-chrome-snap',
                 host=socket.gethostname(), launcher='1.9.1', **d.now(),
                 collector=dict(pid=os.getpid(), start=d.process(os.getpid())['start']),
                 owner=dict(pid=os.getpid(), start=d.process(os.getpid())['start']),
                 tcp=dict(available=True, rtt_ms=30, rttvar_ms=8, last_ack_ms=20,
                          bytes_acked=10000, send_queue=0, notsent_bytes=0),
                 delta=dict(seconds=1, bytes_sent=10, bytes_received=1000,
                            bytes_acked=10, retrans_total=0))
    value.update(values)
    return value


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='remote-chrome-monitor-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = dict(os.environ, TERM='xterm-256color', LANG='C.UTF-8', LC_ALL='C.UTF-8',
                        XDG_STATE_HOME=str(self.base / 'state'), XDG_RUNTIME_DIR=str(self.base / 'runtime'),
                        REMOTE_CHROME_CONFIG=str(self.base / 'no-config'))
        self.env.pop('NO_COLOR', None)
        (self.base / 'runtime').mkdir(mode=0o700)

    def args(self, **values):
        args = dict(target='snap', session='remote-chrome-snap', attempt=ATTEMPT,
                    recording_policy='on', days=30, cap=1073741824)
        args.update(values)
        return types.SimpleNamespace(**args)

    def snapshot(self, rows):
        with patch.dict(os.environ, self.env):
            store = d.Store()
            for row in rows:
                store.append(row)
            store.close()
            files = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in store.path.iterdir()}
            with patch.object(d.Store, 'prune_locked', side_effect=AssertionError('prune')), \
                    patch.object(d, 'bounded', side_effect=AssertionError('external command')):
                value = d.monitor_snapshot(self.args())
            self.assertEqual(files, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in store.path.iterdir()})
            return value

    def session(self, **values):
        value = dict(session='remote-chrome-snap', target='snap', attempt=ATTEMPT,
                     policy='on', transport='h264,hw,bpf=250000', pane='123')
        value.update(values)
        return value

    def text(self, value, columns=76, **kwargs):
        return '\n'.join(s for s, _ in d.monitor_lines(self.session(), value, None, columns, **kwargs))

    def test_exact_attempt_boot_role_origin_and_session_read_only(self):
        good = sample()
        rows = [good, sample(attempt=SECOND), sample(role='browser'),
                sample(boot='old'), sample(origin='other'), sample(session='other'), sample(target='other')]
        value = self.snapshot(rows)
        self.assertEqual(value['summary']['samples'], 1)
        self.assertEqual(value['state'], 'active')
        self.assertEqual(value['ack']['rtt'], 30)

    def test_idle_zero_has_bar_missing_and_idle_ack_do_not(self):
        idle = sample(delta=dict(seconds=1, bytes_sent=0, bytes_received=0, bytes_acked=0, retrans_total=0))
        buckets = d.monitor_buckets([idle], idle['wall'])
        self.assertIn(0, buckets['traffic'])
        self.assertTrue(all(v is None for v in buckets['rtt']))
        graph, peak = d.monitor_spark(buckets['traffic'], 60, False)
        self.assertEqual(peak, 0)
        self.assertEqual(graph.count('.'), 1)
        self.assertEqual(graph.count(' '), 59)

    def test_buckets_weight_traffic_and_use_eligible_max(self):
        now = time.time() // 5 * 5 + 3
        a = sample(wall=now - 1, delta=dict(seconds=1, bytes_sent=100, bytes_received=0, bytes_acked=1))
        b = sample(wall=now, tcp=dict(available=True, rtt_ms=50, rttvar_ms=12, last_ack_ms=1),
                   delta=dict(seconds=2, bytes_sent=300, bytes_received=0, bytes_acked=1))
        result = d.monitor_buckets([a, b], now)
        self.assertAlmostEqual(result['traffic'][-1], 400 / 3)
        self.assertEqual(result['rtt'][-1], 50)
        self.assertEqual(result['deviation'][-1], 12)

    def test_stale_latest_expires_but_historical_values_keep_age(self):
        row = sample(wall=time.time() - 20, mono=time.monotonic() - 20)
        value = self.snapshot([row])
        text = self.text(value)
        self.assertIn('STALE', text)
        self.assertIn('not fresh', text)
        self.assertRegex(text, r'RX latest\s+unavailable')
        self.assertRegex(text, r'RTT last ACK\s+30 ms')
        self.assertIn('30 ms / 30 ms', text)

    def test_latest_ack_age_does_not_inherit_fresh_idle_sample(self):
        ack = sample(wall=time.time() - 20, mono=time.monotonic() - 20)
        idle = sample(delta=dict(seconds=1, bytes_sent=0, bytes_received=0, bytes_acked=0, retrans_total=0))
        value = self.snapshot([ack, idle])
        text = self.text(value)
        self.assertIn('not fresh', text)
        self.assertIn('fresh /', text)
        self.assertRegex(text, r'RX latest\s+0 B/s')

    def test_missing_store_off_and_unknown_are_distinct(self):
        with patch.dict(os.environ, self.env):
            missing = d.monitor_snapshot(self.args())
            off = d.monitor_snapshot(self.args(recording_policy='off'))
            unknown = d.monitor_snapshot(self.args(attempt=''))
        self.assertEqual(missing['state'], 'degraded')
        self.assertEqual(off['state'], 'off')
        self.assertEqual(unknown['state'], 'unknown')
        self.assertFalse((self.base / 'state').exists())

    def test_partial_read_remains_visible(self):
        with patch.dict(os.environ, self.env):
            store = d.Store(); store.close()
            with patch.object(d.Store, 'records', side_effect=ValueError('query read limit reached')):
                value = d.monitor_snapshot(self.args())
        self.assertIn('PARTIAL: query read limit reached', self.text(value))

    def test_presentation_escapes_controls_and_narrow_wraps(self):
        value = self.snapshot([sample()])
        for width in (20, 40, 76, 116):
            lines = d.monitor_lines(self.session(), value, None, width, False)
            self.assertTrue(all(len(s) <= width for s, _ in lines), [(len(s), s) for s, _ in lines if len(s) > width])
            self.assertIn('RECORDING', '\n'.join(s for s, _ in lines))
        self.assertEqual(d.monitor_clean('x\x1b[2J\ny\x7f'), 'x?[2J?y?')

    def test_inventory_preserves_names_and_rejects_corrupt_data(self):
        raw = 'remote-chrome work\tsnap\t' + ATTEMPT + '\ton\th264,hw,bpf=250000\t123\n'
        result = d.monitor_inventory(raw)
        self.assertEqual(result[0]['session'], 'remote-chrome work')
        for bad in (raw.replace(ATTEMPT, 'oops'), raw.replace('snap', '-option'), 'short\n', raw.replace('work', 'w\x1bork')):
            with self.assertRaises(ValueError):
                d.monitor_inventory(bad)

    def test_observer_deadline_output_bound_and_descendant_cleanup(self):
        job = d.MonitorJob(['python3', '-c', 'import time;time.sleep(60)'], .05)
        time.sleep(.07)
        ok, _, error = job.poll()
        self.assertFalse(ok)
        self.assertIn('timed out', error)
        self.assertIsNotNone(job.process.returncode)
        job = d.MonitorJob(['python3', '-c', 'print("x"*600000)'], 2)
        time.sleep(.1)
        ok, raw, error = job.poll()
        self.assertFalse(ok)
        self.assertLessEqual(len(raw), job.LIMIT)
        self.assertIn('limit', error)

    def test_observer_cancellation_reaps_only_its_owned_group(self):
        pidfile = self.base / 'child'
        program = "import subprocess,time;from pathlib import Path;p=subprocess.Popen(['python3','-c','import time;time.sleep(60)']);Path(" + repr(str(pidfile)) + ").write_text(str(p.pid));time.sleep(60)"
        job = d.MonitorJob(['python3', '-c', program], 5)
        self.addCleanup(lambda: job.cancel() if not job.output.closed else None)
        deadline = time.monotonic() + 2
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        pid = int(pidfile.read_text())
        birth = d.process(pid)['start']
        self.assertIsNotNone(d.exact(pid, birth))
        job.cancel()
        deadline = time.monotonic() + 1
        while d.exact(pid, birth) is not None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIsNone(d.exact(pid, birth))
        self.assertIsNotNone(d.exact(os.getpid(), d.process(os.getpid())['start']))

    def test_public_help_rejects_non_tty_and_unsafe_arguments(self):
        def run(*args):
            return subprocess.run([str(LAUNCHER), *args], env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(run('monitor', '--help').returncode, 0)
        self.assertIn('interactive terminal', run('monitor').stderr)
        self.assertIn('requires HOST', run('monitor', '--session', 'custom').stderr)
        self.assertIn('Invalid monitor host', run('monitor', 'x\nspoof').stderr)
        self.assertIn('Invalid monitor session', run('monitor', 'snap', '--session', 'a:b').stderr)

    def fixture(self, slow=False, restart=False):
        fixture = self.base / 'observer'
        config = dict(slow=slow, restart=restart, trace=str(self.base / 'trace'),
                      counter=str(self.base / 'counter'), data=d.monitor_snapshot(self.args(recording_policy='off')))
        fixture.write_text('#!/usr/bin/env python3\n' + '''import json, os, sys, time
from pathlib import Path
config = CONFIG
action = sys.argv[1]
with open(config['trace'], 'a') as f: f.write(action + '\\n')
if action == '_monitor-inventory':
    path = Path(config['counter'])
    n = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(n))
    attempt = SECOND if config['restart'] and n > 1 else ATTEMPT
    print('remote-chrome-snap\\tsnap\\t' + attempt + '\\ton\\th264,hw,bpf=250000\\t123')
    print('remote-chrome-work\\tsnap\\t' + SECOND + '\\ton\\tlossless\\t124')
elif action == '_monitor-data':
    value = config['data']; value.update(attempt=sys.argv[sys.argv.index('--attempt')+1], mono=time.monotonic())
    print(json.dumps(value))
elif action == '_monitor-transport':
    if config['slow']: time.sleep(60)
    print('Decode\\thardware\\nEncode\\thardware\\nAttempt\\t'+sys.argv[4])
else: raise SystemExit('unexpected operation')
'''.replace('CONFIG', repr(config)).replace('SECOND', repr(SECOND)).replace('ATTEMPT', repr(ATTEMPT)))
        fixture.chmod(0o755)
        return fixture

    def terminal(self, command, actions, size=(40, 100), expected=0):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', *size, 0, 0))
        before = termios.tcgetattr(slave)
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=self.env, start_new_session=True)
        output = bytearray()
        start, pending = time.monotonic(), list(actions)
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - start
                self.assertLess(elapsed, 14, output.decode(errors='replace')[-1000:])
                if pending and elapsed >= pending[0][0]:
                    _, action = pending.pop(0)
                    if isinstance(action, bytes):
                        os.write(master, action)
                    elif action == 'resize':
                        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 18, 40, 0, 0))
                        process.send_signal(signal.SIGWINCH)
                    else:
                        process.send_signal(action)
                if select.select([master], [], [], .03)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError as error:
                        if error.errno != errno.EIO: raise
            self.assertEqual(process.wait(), expected, output.decode(errors='replace')[-2000:])
            self.assertEqual(termios.tcgetattr(slave), before, 'terminal attributes not restored')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
        return output.decode(errors='replace')

    def command(self, fixture, overview=False, plain=False):
        helper = self.base / 'diagnostics.py'
        helper.write_text(SOURCE)
        return ['python3', str(helper), 'monitor', '-' if overview else 'snap',
                '--session', '' if overview else 'remote-chrome-snap', '--launcher', str(fixture),
                '--terminal-fd', '0', '--monitor-plain', '1' if plain else '0']

    def test_real_pty_freeze_resume_resize_quit_while_ssh_is_slow(self):
        fixture = self.fixture(slow=True)
        output = self.terminal(self.command(fixture), [(1, b' '), (1.3, 'resize'), (1.6, b' '), (1.9, b'q')])
        self.assertIn('LIVE', output)
        self.assertIn('FROZEN', output)
        self.assertIn('recording continues', output)
        trace = (self.base / 'trace').read_text().splitlines()
        self.assertEqual(trace.count('_monitor-transport'), 1)
        self.assertGreaterEqual(trace.count('_monitor-data'), 2)
        self.assertNotIn('collect', trace)

    def test_real_pty_overview_is_local_and_selects_exact_session(self):
        fixture = self.fixture()
        output = self.terminal(self.command(fixture, overview=True), [(1, b'j\n'), (1.8, b'\t'), (2, b'q')])
        self.assertIn('LOCAL SESSIONS', output)
        self.assertIn('remote-chrome-work', output)
        self.assertIn('attempt 22222222', output)
        trace = (self.base / 'trace').read_text().splitlines()
        self.assertEqual(trace.count('_monitor-transport'), 0)  # Selected lossless session.

    def test_explicit_custom_session_is_included_without_prefix(self):
        prologue = 'source ' + str(LAUNCHER) + '''
tmux() {
  case "$1" in
    list-sessions) printf '%s\\n' work-browser unrelated ;;
    list-panes) printf '%s\\n' '%1|123|sleep 60' ;;
    show-options)
      case "${@: -1}" in
        @remote-chrome-command) printf '%s\\n' 'waypipe --no-gpu ssh snap bash' ;;
        @remote-chrome-yubikey) echo off ;;
        @remote-chrome-diagnostics) echo 'on:30:1073741824:11111111-1111-4111-8111-111111111111' ;;
      esac ;;
  esac
}
main _monitor-inventory work-browser
'''
        result = subprocess.run(['bash', '-c', prologue], env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        inventory = d.monitor_inventory(result.stdout)
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]['session'], 'work-browser')
        self.assertEqual(inventory[0]['target'], 'snap')

    def test_inventory_failure_is_not_confirmed_empty(self):
        for error, code in [('permission denied', 1), ('no server running on test', 0)]:
            script = 'source ' + str(LAUNCHER) + '\ntmux() { echo ' + repr(error) + ' >&2; return 1; }; main _monitor-inventory\n'
            value = subprocess.run(['bash', '-c', script], env=self.env, capture_output=True, text=True, timeout=5)
            self.assertEqual(value.returncode, code)
            self.assertEqual(value.stdout, '')
            if code:
                self.assertIn(error, value.stderr)

    def test_codec_check_refuses_changed_attempt_before_any_remote_work(self):
        script = 'source ' + str(LAUNCHER) + '''
tmux() { echo on:30:1073741824:22222222-2222-4222-8222-222222222222; }
chrome_transport_status() { echo 'UNEXPECTED transport check'; }
main _monitor-transport snap remote-chrome-snap 11111111-1111-4111-8111-111111111111
'''
        value = subprocess.run(['bash', '-c', script], env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(value.returncode, 1)
        self.assertNotIn('UNEXPECTED', value.stdout)

    def test_real_pty_attempt_change_discards_old_data_and_codecs(self):
        fixture = self.fixture(restart=True)
        output = self.terminal(self.command(fixture), [(6.3, b'q')])
        self.assertIn('history reset', output)
        self.assertIn('22222222', output)
        self.assertEqual((self.base / 'trace').read_text().count('_monitor-transport'), 2)

    def test_real_pty_sigint_and_sigterm_restore_terminal(self):
        fixture = self.fixture(slow=True)
        for sig, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            self.terminal(self.command(fixture), [(.6, sig)], expected=code)

    def test_real_pty_unexpected_error_restores_terminal(self):
        output = self.terminal(self.command(self.base / 'missing-observer'), [], expected=1)
        self.assertIn('No such file', output)

    def test_public_entrypoint_real_pty_and_plain_no_color(self):
        # The real Bash entrypoint must preserve tty input across its heredoc.
        commands = self.base / 'commands'; commands.mkdir()
        tmux = commands / 'tmux'
        tmux.write_text('#!/bin/sh\necho "no server running on test" >&2\nexit 1\n'); tmux.chmod(0o755)
        self.env['PATH'] = str(commands) + ':' + self.env['PATH']
        output = self.terminal([str(LAUNCHER), 'monitor', '--plain'], [(1, b'q')])
        self.assertIn('No managed sessions', output)
        self.assertNotRegex(output, r'\x1b\[(?:[0-9]+;)*3[0-7]m')
        self.terminal([str(LAUNCHER), 'monitor', '--plain'], [(.6, signal.SIGTERM)], expected=143)


if __name__ == '__main__':
    unittest.main()
