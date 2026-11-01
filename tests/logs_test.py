#!/usr/bin/env python3
"""Historical journal selection without a live record or real service changes."""
import contextlib
import datetime
import io
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

LAUNCHER = Path(__file__).resolve().parents[1] / 'bin/remote-chrome'
MODULE = LAUNCHER.read_text().split("<<'REMOTE_CHROME_SESSIONS'\n", 1)[1].split('\nREMOTE_CHROME_SESSIONS\n', 1)[0]
s = types.ModuleType('sessions')
exec(compile(MODULE, 'embedded-sessions', 'exec'), s.__dict__)
SESSION = 'remote-chrome-snap'
FIRST = '1' * 32
SECOND = '2' * 32


class LogsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='remote-chrome-logs-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(s, 'ROOT', self.base / 'missing-runtime').start()

    def journal(self, argv, **kwargs):
        self.assertEqual(argv[0], 'journalctl')
        field = next(item for item in argv if item.startswith('--field='))
        if field == '--field=_SYSTEMD_USER_UNIT':
            output = '\n'.join([s.unit_name(SESSION, SECOND), s.unit_name('other-session', FIRST),
                                s.unit_name(SESSION, 'bad*'), 'app-chrome.scope'])
        else:
            # A launch with only manager messages must remain discoverable.
            output = '\n'.join([s.unit_name(SESSION, FIRST), s.unit_name(SESSION, SECOND)])
        return subprocess.CompletedProcess(argv, 0, output, '')

    def select(self, args):
        with patch.object(s, 'command', side_effect=self.journal), patch.object(s.os, 'execvp') as execute:
            self.assertEqual(s.logs(SESSION, args), 0)
        self.assertFalse(s.ROOT.exists())
        return execute.call_args.args[1]

    def test_history_survives_absent_runtime_and_includes_old_launches(self):
        argv = self.select([])
        units = {item.removeprefix('--unit=') for item in argv if item.startswith('--unit=')}
        self.assertEqual(units, {s.unit_name(SESSION, FIRST), s.unit_name(SESSION, SECOND)})
        self.assertIn('--lines=100', argv)
        self.assertNotIn('--follow', argv)

    def test_explicit_generation_does_not_require_current_record_or_discovery(self):
        with patch.object(s, 'command', side_effect=AssertionError('journal discovery')), \
                patch.object(s.os, 'execvp') as execute:
            self.assertEqual(s.logs(SESSION, ['--generation', FIRST, '--follow']), 0)
        argv = execute.call_args.args[1]
        self.assertIn('--unit=' + s.unit_name(SESSION, FIRST), argv)
        self.assertIn('--follow', argv)
        self.assertFalse(s.ROOT.exists())

    def test_follow_stays_with_current_generation(self):
        with patch.object(s, 'load', return_value={'generation': SECOND}), \
                patch.object(s, 'command', side_effect=AssertionError('journal discovery')), \
                patch.object(s.os, 'execvp') as execute:
            s.logs(SESSION, ['--follow', '--since', '2h'])
        argv = execute.call_args.args[1]
        self.assertIn('--unit=' + s.unit_name(SESSION, SECOND), argv)
        self.assertNotIn('--unit=' + s.unit_name(SESSION, FIRST), argv)

    def test_follow_without_current_record_requires_selection(self):
        with self.assertRaisesRegex(ValueError, 'no current launch record'):
            s.logs(SESSION, ['--follow'])

    def test_list_discovers_manager_only_launches_and_filters_other_sessions(self):
        output = io.StringIO()
        with patch.object(s, 'command', side_effect=self.journal), contextlib.redirect_stdout(output):
            s.logs(SESSION, ['--list'])
        text = output.getvalue()
        self.assertIn(FIRST, text)
        self.assertIn(SECOND, text)
        self.assertNotIn('bad*', text)
        self.assertNotIn('other-session', text)
        self.assertNotIn('.service', text)
        self.assertFalse(s.ROOT.exists())

    def test_time_window_uses_one_clock_and_explicit_timezone(self):
        now = datetime.datetime(2026, 11, 1, 23, tzinfo=datetime.timezone.utc).timestamp()
        with patch.object(s.time, 'time', return_value=now):
            argv = self.select(['--since', '2h', '--until', '2026-11-01T14:00:00-08:00', '--lines', '500'])
        self.assertIn('--since=@' + format(now - 7200, '.6f'), argv)
        self.assertIn('--until=@' + format(now - 3600, '.6f'), argv)
        self.assertIn('--lines=500', argv)

    def test_invalid_filters_fail_before_journal_access(self):
        cases = [(['--generation', '*'], '32-character'),
                 (['--generation='], '32-character'), (['--since='], 'log time'),
                 (['--generation', '$(touch /tmp/unsafe)'], '32-character'),
                 (['--lines', '0'], '--lines'), (['--lines', '10001'], '--lines'),
                 (['--since', '2026-01-01T12:00:00'], 'timezone'),
                 (['--since', '0h'], 'log time'),
                 (['--since', '1m', '--until', '2m'], 'earlier'),
                 (['--since', '2999-01-01T00:00:00Z'], 'past'),
                 (['--follow', '--until', '2m'], '--follow'),
                 (['--list', '--since', '2h'], '--list')]
        with patch.object(s, 'command', side_effect=AssertionError('journal access')):
            for args, message in cases:
                with self.subTest(args=args), self.assertRaisesRegex(ValueError, message):
                    s.logs(SESSION, args)

    def test_journal_failure_is_not_reported_as_empty_history(self):
        p = subprocess.CompletedProcess([], 1, '', 'permission denied')
        with patch.object(s, 'command', return_value=p), self.assertRaisesRegex(ValueError, 'permission denied'):
            s.logs(SESSION, [])

    def test_empty_history_explains_journal_retention(self):
        output = io.StringIO()
        with patch.object(s, 'command', return_value=subprocess.CompletedProcess([], 0, '', '')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(s.logs(SESSION, []), 0)
        self.assertIn('No retained journal launches', output.getvalue())
        self.assertIn('host policy', output.getvalue())

    def shell(self, body):
        env = dict(os.environ, XDG_RUNTIME_DIR=str(self.base / 'runtime'),
                   REMOTE_CHROME_CONFIG=str(self.base / 'no-config'))
        return subprocess.run(['bash', '-c', 'set -euo pipefail\nsource ' + shlex.quote(str(LAUNCHER)) + '\n' + body],
                              env=env, text=True, capture_output=True, timeout=10)

    def test_public_history_dispatch_does_not_require_live_tmux_or_record(self):
        p = self.shell('''tmux() { return 1; }
chrome_service_known() { return 1; }
chrome_session_manager() { printf '%s\\n' "$@"; }
chrome_logs snap --since=2h --generation ''' + FIRST + '\n')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.splitlines(), ['logs', SESSION, '--since=2h', '--generation', FIRST])

    def test_legacy_capture_is_preserved_and_historical_flags_are_explicit(self):
        fixture = '''tmux() { case "$1" in has-session) return 0;; capture-pane) echo LEGACY-OUTPUT;; *) return 97;; esac; }
'''
        p = self.shell(fixture + 'chrome_logs snap\n')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('LEGACY-OUTPUT', p.stdout)
        p = self.shell(fixture + 'chrome_logs snap --since 2h\n')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('uses tmux', p.stderr)


if __name__ == '__main__':
    unittest.main()
