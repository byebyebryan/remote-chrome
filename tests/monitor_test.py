#!/usr/bin/env python3
"""Read-only monitor model and real PTY lifecycle/interaction checks."""
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
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

    def test_managed_inventory_exposes_usb_state_without_changing_attempt_identity(self):
        raw = f'remote-chrome-snap\tsnap\t{ATTEMPT}\ton\th264,hw,bpf=250000\t123\trunning\toff\tdevice-removed'
        session = d.monitor_inventory(raw)[0]
        self.assertEqual(session['usb'], 'off')
        self.assertEqual(session['usb_reason'], 'device-removed')
        self.assertEqual(d.monitor_identity(session), d.monitor_identity(dict(session, usb='ready', usb_reason='none')))
        lines = '\n'.join(text for text, _ in d.monitor_lines(session, None, None, 100))
        self.assertIn('USB off / device removed', lines)

    def test_normal_usb_stop_is_not_an_attention_warning(self):
        value = dict(state='active', age=.1, mono=10, latest=dict(tcp=dict(available=True)))
        session = self.session(phase='running', usb='off', usb_reason='device-removed')
        self.assertEqual(d.monitor_attention(session, value, None, now_mono=10), [])
        session['usb'] = 'cleanup-failed'
        self.assertIn('USB cleanup incomplete', d.monitor_attention(session, value, None, now_mono=10)[0])

    def test_idle_zero_has_bar_missing_and_idle_ack_do_not(self):
        idle = sample(delta=dict(seconds=1, bytes_sent=0, bytes_received=0, bytes_acked=0, retrans_total=0))
        buckets = d.monitor_buckets([idle], idle['wall'])
        self.assertIn(0, buckets['traffic'])
        self.assertTrue(all(v is None for v in buckets['rtt']))
        graph = d.monitor_chart(buckets['traffic'], 67, 3, False, traffic=True)
        self.assertEqual(graph[-2].split('|')[1].count('.'), 1)
        self.assertEqual(graph[-2].split('|')[1].count(' '), 59)
        self.assertTrue(all(not row.split('|')[1].strip() for row in graph[:-2]))

    def test_area_chart_fills_multiple_rows_and_keeps_gaps_empty(self):
        graph = d.monitor_chart([0, None, 100, 50], 12, 4, False)
        columns = [row.split('|')[1] for row in graph[:-1]]
        self.assertEqual(len(columns), 4)
        self.assertTrue(all(row[2] == '#' for row in columns))
        self.assertTrue(all(row[1] == ' ' for row in columns))
        self.assertEqual(columns[-1][0], '.')
        self.assertTrue(all(row[0] == ' ' for row in columns[:-1]))
        self.assertEqual(sum(row[3] == '#' for row in columns), 2)

    def test_chart_missing_values_do_not_acquire_a_zero_scale(self):
        graph = d.monitor_chart([None, float('nan'), -1], 40, 5)
        self.assertIn('unavailable', graph[0])
        self.assertTrue(all(not row.split('│')[1].strip() for row in graph[:-1]))
        self.assertNotIn('0 ms', '\n'.join(graph))

    def test_traffic_chart_uses_one_unit_for_peak_midpoint_and_zero(self):
        for peak, unit in ((0, 'B/s'), (1000, 'B/s'), (12000, 'KiB/s'), (15900, 'Mbps'), (125000, 'Mbps')):
            for unicode, separator in ((True, '│'), (False, '|')):
                graph = d.monitor_chart([0, peak / 2, None, peak], 76, 8, unicode, traffic=True)
                labels = [row.split(separator)[0].strip() for row in graph[:-1] if row.split(separator)[0].strip()]
                self.assertEqual(len(labels), 3)
                self.assertTrue(all(label.endswith(' ' + unit) for label in labels), labels)
                self.assertEqual(labels[-1], '0 ' + unit)
                self.assertTrue(any(c in graph[-2] for c in '▁.#'))

    def test_transport_wording_distinguishes_recorded_policy_from_observed_use(self):
        self.assertEqual(d.monitor_transport('h264,hw,bpf=250000'), 'H.264 / hardware preferred / 250k bits/frame')
        self.assertEqual(d.monitor_transport('h264,sw,bpf=500000'), 'H.264 / software codecs / 500k bits/frame')
        self.assertIn('250,001 bits/frame', d.monitor_transport('h264,hw,bpf=250001'))
        self.assertEqual(d.monitor_transport('lossless'), 'Lossless')
        self.assertIn('Unknown recorded transport', d.monitor_transport('future\x1bcodec'))
        self.assertNotIn('\x1b', d.monitor_transport('future\x1bcodec'))

    def test_chart_fills_wide_terminals_without_interpolating_gaps(self):
        bins = d.monitor_graph_columns([0, None, 10], 12)
        self.assertEqual(bins, [0] * 4 + [None] * 4 + [10] * 4)
        self.assertEqual(d.monitor_graph_columns([0, 10, None, None], 2), [10, None])
        for width in (12, 20, 40, 116, 180):
            graph = d.monitor_chart([0, None, 10], width, 6)
            self.assertTrue(all(len(row) <= width for row in graph))
            self.assertTrue(all(len(row) == width for row in graph[:-1]))

    def test_graphs_grow_with_terminal_height_and_short_terminals_scroll(self):
        value = self.snapshot([sample()])
        sizes = []
        for available in (20, 40, 60, 90):
            lines = d.monitor_lines(self.session(), value, None, 96, rows=available)
            plots = [text for text, _ in lines if '│' in text]
            self.assertGreaterEqual(len(plots), 9)
            self.assertEqual(len(plots) % 3, 0)
            sizes.append(len(plots))
            if available >= 60:
                self.assertLessEqual(len(lines), available)
                self.assertGreaterEqual(len(lines), available - 2)
        self.assertGreater(sizes[-1], sizes[-2])
        self.assertGreater(sizes[-2], sizes[0])

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

    def test_attention_precedes_metrics_and_can_stay_pinned_in_short_viewports(self):
        value = self.snapshot([sample()])
        value['partial'] = ['query read limit reached']
        for rows in (20, 36, 56):
            lines = d.monitor_lines(self.session(), value, None, 76, rows=rows)
            self.assertEqual(lines[0], ('PARTIAL: query read limit reached', 'warn'))
            alerts = d.monitor_attention(self.session(), value, None)
            self.assertEqual(alerts, ['PARTIAL: query read limit reached'])
            self.assertIn('PARTIAL:', d.monitor_banner(alerts, 36))
            self.assertGreater(len(lines), 36 if rows == 20 else 0)

    def test_attention_keeps_stale_failure_counts_and_escaped_details(self):
        value = self.snapshot([sample()])
        value.update(age=20, state='degraded', partial=['query\x1b limit'])
        remote = dict(mono=time.monotonic(), ok=False, text='SSH\n unavailable')
        alerts = d.monitor_attention(self.session(), value, remote)
        self.assertTrue(any('STALE sample' in text for text in alerts), alerts)
        self.assertTrue(any('Recorder degraded' in text for text in alerts), alerts)
        self.assertTrue(any('Codec check unavailable' in text for text in alerts), alerts)
        for width in (12, 36, 96):
            banner = d.monitor_banner(alerts, width)
            self.assertLessEqual(len(banner), width)
            self.assertIn('Attention', banner)
        self.assertNotRegex(' '.join(alerts), r'[\x00-\x1f\x7f]')
        remote.update(ok=True, warning='software fallback observed')
        self.assertIn('Codec warning: software fallback observed', d.monitor_attention(self.session(), value, remote))
        idle = sample(delta=dict(seconds=1, bytes_sent=0, bytes_received=0, bytes_acked=0, retrans_total=0))
        self.assertEqual(d.monitor_attention(self.session(), self.snapshot([idle]), None), [])

    def test_help_explains_scope_and_keeps_exact_custom_status_command(self):
        session = self.session(session="remote-chrome work browser's $(literal)")
        lines = d.monitor_help(session, 200, ['PARTIAL: read limit'])
        text = '\n'.join(part for part, _ in lines)
        self.assertIn('not a full session health check', text)
        self.assertIn('CURRENT ATTENTION', text)
        self.assertIn('PARTIAL: read limit', text)
        self.assertIn('not one-way jitter or frame/input latency', text)
        self.assertIn('Page Up/Down', text)
        command = next(part for part, _ in lines if part.startswith('remote-chrome status'))
        self.assertEqual(shlex.split(command), ['remote-chrome', 'status', 'snap', '--session', session['session']])
        for width in (20, 40, 76):
            self.assertTrue(all(len(part) <= width for part, _ in d.monitor_help(session, width)))
        generic = '\n'.join(part for part, _ in d.monitor_help(None, 76))
        unknown = '\n'.join(part for part, _ in d.monitor_help(self.session(target='-'), 76))
        self.assertIn('\nremote-chrome status\n', generic)
        self.assertIn('\nremote-chrome status\n', unknown)

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

    def fixture(self, slow=False, restart=False, data=None):
        fixture = self.base / 'observer'
        config = dict(slow=slow, restart=restart, trace=str(self.base / 'trace'),
                      counter=str(self.base / 'counter'), data=data if data is not None else d.monitor_snapshot(self.args(recording_policy='off')))
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

    def test_real_pty_help_scroll_resize_escape_and_live_observation(self):
        value = self.snapshot([sample()]); value['partial'] = ['query read limit reached']
        fixture = self.fixture(slow=True, data=value)
        output = self.terminal(self.command(fixture), [(.6, b'j' * 60), (.9, b'?'),
                               (1.2, b' '), (1.4, b'\x1b[6~\x1b[6~'), (1.7, 'resize'),
                               (2, b'\x1b[6~' * 6), (2.2, b'\x1b'), (3.4, b' '), (3.8, b'q')], size=(24, 80))
        self.assertIn('MONITOR HELP', output)
        self.assertIn('FULL SESSION CHECKS', output)
        self.assertIn('PARTIAL:', output)
        self.assertIn('FROZEN', output)  # Escape returned to the dashboard; Space now freezes it.
        trace = (self.base / 'trace').read_text().splitlines()
        self.assertGreaterEqual(trace.count('_monitor-data'), 3)
        self.assertEqual(trace.count('_monitor-transport'), 1)
        self.assertTrue(set(trace) <= {'_monitor-inventory', '_monitor-data', '_monitor-transport'})

    def test_real_pty_help_from_overview_stays_local_and_escape_still_quits(self):
        fixture = self.fixture()
        output = self.terminal(self.command(fixture, overview=True), [(.5, b'?'), (.9, b'?'), (1.2, b'\x1b')], size=(24, 80))
        self.assertIn('MONITOR HELP', output)
        self.assertNotIn('_monitor-data', (self.base / 'trace').read_text())
        self.assertNotIn('_monitor-transport', (self.base / 'trace').read_text())

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
