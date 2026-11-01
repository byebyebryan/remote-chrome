#!/usr/bin/env python3
"""Check human entry points, real PTY rendering, and presentation boundaries."""
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import subprocess
import struct
import tempfile
import termios
import time
import types
import unittest

LAUNCHER = Path(__file__).resolve().parents[1] / 'bin/remote-chrome'
SOURCE = LAUNCHER.read_text()
d = types.ModuleType('diagnostics')
exec(compile(SOURCE.split("<<'REMOTE_CHROME_DIAGNOSTICS'\n", 1)[1].split(
    '\nREMOTE_CHROME_DIAGNOSTICS\n', 1)[0], 'embedded-diagnostics', 'exec'), d.__dict__)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='remote-chrome-cli-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.env = dict(os.environ, LANG='C.UTF-8', LC_ALL='C.UTF-8', TERM='xterm-256color',
                        XDG_RUNTIME_DIR=str(base / 'runtime'), XDG_STATE_HOME=str(base / 'state'),
                        REMOTE_CHROME_CONFIG=str(base / 'no-config'), REMOTE_CHROME_NOTIFICATIONS='0')
        self.env.pop('NO_COLOR', None)
        self.env.pop('COLUMNS', None)
        for name in ('REMOTE_CHROME_TRANSPORT', 'REMOTE_CHROME_VIDEO_CODECS', 'REMOTE_CHROME_DIAGNOSTICS'):
            self.env.pop(name, None)
        (base / 'runtime').mkdir(mode=0o700)

    def run_cli(self, *args, env=None):
        return subprocess.run([str(LAUNCHER), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=10)

    def shell(self, script, terminal=False, env=None, columns=None):
        prologue = 'set -euo pipefail\nsource ' + shlex.quote(str(LAUNCHER)) + '''
PROGRAM=remote-chrome
ssh() { echo 'UNEXPECTED SSH' >&2; return 97; }
sudo() { echo 'UNEXPECTED SUDO' >&2; return 97; }
tmux() { echo 'UNEXPECTED TMUX' >&2; return 97; }
'''
        command = ['bash', '-c', prologue + script]
        if not terminal:
            return subprocess.run(command, env=env or self.env, capture_output=True, text=True, timeout=10)
        master, slave = pty.openpty()
        if columns is not None:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, columns, 0, 0))
        process = subprocess.Popen(command, env=env or self.env, stdin=subprocess.DEVNULL,
                                   stdout=slave, stderr=slave)
        os.close(slave)
        output = bytearray()
        deadline = time.monotonic() + 10
        try:
            while True:
                if time.monotonic() > deadline:
                    self.fail('PTY child exceeded deadline')
                if select.select([master], [], [], .1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    output.extend(chunk)
            return subprocess.CompletedProcess(command, process.wait(timeout=1), output.decode(), '')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

    def test_help_is_grouped_and_redirected_output_is_plain(self):
        p = self.run_cli('--help')
        self.assertEqual(p.returncode, 0, p.stderr)
        for text in ('Everyday', 'status [HOST]', 'More commands', '--verbose', '--plain', '--color MODE'):
            self.assertIn(text, p.stdout)
        self.assertNotIn('\x1b', p.stdout)
        self.assertNotIn('✓', p.stdout)
        self.assertNotIn('REMOTE_CHROME_SECRET_SCHEMA', p.stdout)

    def test_verbose_help_preserves_complete_reference(self):
        p = self.run_cli('--help', '--verbose')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('--chrome-command', p.stdout)
        self.assertIn('REMOTE_CHROME_SECRET_SCHEMA', p.stdout)

    def test_real_terminal_has_colors_and_standard_glyphs(self):
        p = self.shell('chrome_ui_title snap; chrome_ui_result ok "Session started"', terminal=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn('\x1b[1;36m', p.stdout)
        self.assertIn('· snap', p.stdout)
        self.assertIn('✓ Session started', p.stdout)
        self.assertNotIn('\x1b[2J', p.stdout)

    def test_no_color_even_empty_disables_automatic_ansi(self):
        p = self.shell('chrome_ui_title snap', terminal=True, env=dict(self.env, NO_COLOR=''))
        self.assertNotIn('\x1b', p.stdout)
        self.assertIn('snap', p.stdout)

    def test_dumb_terminal_and_c_locale_fallback(self):
        p = self.shell('chrome_ui_title snap; chrome_ui_result ok done', terminal=True,
                       env=dict(self.env, TERM='dumb', LC_ALL='C'))
        self.assertNotIn('\x1b', p.stdout)
        self.assertIn('OK done', p.stdout)
        self.assertNotIn('·', p.stdout)

    def test_plain_overrides_forced_color_and_terminal_glyphs(self):
        p = self.shell('main --color always --plain --help', terminal=True)
        self.assertEqual(p.returncode, 0)
        self.assertNotIn('\x1b', p.stdout)
        self.assertNotIn('·', p.stdout)

    def test_explicit_color_override_and_invalid_mode(self):
        p = self.run_cli('--color=always', '--help')
        self.assertIn('\x1b[', p.stdout)
        p = self.run_cli('--color', 'neon', '--help')
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout, '')
        self.assertIn('--color expects', p.stderr)

    def test_options_keep_values_and_browser_arguments_literal(self):
        p = self.shell('''chrome_ui_options launch snap --chrome-command --verbose --plain --session custom -- --color always --verbose
printf '%s\\0' "${chrome_ui_args[@]}"
''')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.split('\0')[:-1], ['launch', 'snap', '--chrome-command', '--verbose',
                         '--session', 'custom', '--', '--color', 'always', '--verbose'])

    def test_internal_machine_entrypoint_does_not_consume_flags(self):
        p = self.shell('''chrome_diagnostics_run() { printf '%s\\0' "$@"; }
main _diagnostics-query snap --session --plain
''')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('query-local\0snap\0--session\0--plain\0--version\0', p.stdout)

    def test_terminal_controls_are_removed_from_presentation_values(self):
        p = self.shell("chrome_ui_row Browser $'snap\\033[2J\\nspoof\\tvalue\\177'")
        self.assertEqual(p.returncode, 0)
        self.assertNotIn('\x1b', p.stdout)
        self.assertNotIn('\t', p.stdout)
        self.assertIn('snap?[2J?spoof?value?', p.stdout)

    def test_narrow_terminal_places_value_on_next_line(self):
        p = self.shell('chrome_ui_row Browser snap', env=dict(self.env, COLUMNS='48'))
        self.assertEqual(p.stdout, '  Browser\n    snap\n')

    def test_real_narrow_terminal_is_detected_without_exported_columns(self):
        p = self.shell('main --plain --help', terminal=True, columns=48)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn('  HOST\r\n    Open a tunnel', p.stdout)

    def test_receipt_reports_policy_and_copyable_default_commands(self):
        p = self.shell('''chrome_diagnostics=on chrome_diag_days=30 chrome_diag_cap=1073741824
chrome_ui_receipt started snap remote-chrome-snap video auto 0
''')
        self.assertEqual(p.returncode, 0, p.stderr)
        for text in ('Background session started', 'hardware preferred', '30 days / 1 GiB cap',
                     'remote-chrome status snap', 'remote-chrome reset snap', 'remote-chrome stop snap'):
            self.assertIn(text, p.stdout)
        self.assertNotIn('--session', p.stdout)
        self.assertNotIn('Connected', p.stdout)
        self.assertNotIn('NVIDIA', p.stdout)

    def test_custom_receipt_commands_round_trip_shell_quoting(self):
        p = self.shell('chrome_ui_command Restart reset "user@host.example" "work browser"')
        command = p.stdout.split('Restart', 1)[1].strip()
        self.assertEqual(shlex.split(command), ['remote-chrome', 'reset', 'user@host.example', '--session', 'work browser'])

    def test_restart_warning_is_on_stderr_and_preserved_policy_visible(self):
        p = self.shell('''chrome_diagnostics=off chrome_diag_days=17 chrome_diag_cap=536870912
chrome_ui_receipt restarted snap custom lossless auto 0
''')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('Background session restarted', p.stdout)
        self.assertIn('Lossless', p.stdout)
        self.assertIn('off for this session', p.stdout)
        self.assertIn('In-page state may have been lost', p.stderr)

    def status(self, present=True, verbose=False):
        return self.shell('''
tmux() {
  case "$1" in
    has-session) return ''' + ('0' if present else '1') + ''' ;;
    list-windows) echo chrome ;;
    show-options) echo off:30:1073741824:- ;;
    *) return 97 ;;
  esac
}
chrome_transport_status() { chrome_ui_check Video Lossless 'status: recorded transport: stock lossless'; }
chrome_ui_verbose=''' + ('1' if verbose else '0') + '''
main status snap --session custom
''')

    def test_status_compact_grouping_and_exact_details_command(self):
        p = self.status()
        self.assertEqual(p.returncode, 0, p.stderr)
        for text in ('Background session running', 'Diagnostics', 'Off for this attempt',
                     'Network / last 5 minutes / local SSH', 'Unavailable / diagnostics off',
                     'No managed forwarding', 'Disabled or untracked', 'Available session checks passed',
                     'remote-chrome status snap --session custom --verbose'):
            self.assertIn(text, p.stdout)
        self.assertFalse((Path(self.env['XDG_STATE_HOME']) / 'remote-chrome').exists())

    def test_status_failure_exit_and_explanation_survive_compact_rendering(self):
        p = self.status(present=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn('session is not running: custom', p.stderr)
        self.assertNotIn('checks passed', p.stdout)

    def test_verbose_status_keeps_detailed_checks(self):
        p = self.status(verbose=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('status: tmux session running: custom', p.stdout)
        self.assertIn('status: window: chrome', p.stdout)
        self.assertIn('status: healthy', p.stdout)

    def test_transport_observed_hardware_and_software_fallback(self):
        data = dict(schema=1, root=dict(pid=42, start='123'),
                    counts=dict(encoder_hw=0, encoder_sw=0, decoder_hw=1, decoder_sw=0),
                    render_nodes=[['/dev/dri/renderD128', '0x8086']])
        p = self.shell('chrome_transport_summary local hw <<\'DATA\'\n' + json.dumps(data) + '\nDATA\n')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('Decode', p.stdout)
        self.assertIn('hardware', p.stdout)
        self.assertNotIn('Intel', p.stdout)  # Open nodes do not identify a codec device.
        self.assertNotIn('Encode', p.stdout)
        data['counts'].update(decoder_hw=0, decoder_sw=1)
        p = self.shell('chrome_transport_summary local hw <<\'DATA\'\n' + json.dumps(data) + '\nDATA\n')
        self.assertIn('software', p.stdout)
        self.assertIn('despite hardware preference', p.stdout)

    def test_invalid_transport_observations_preserve_failure(self):
        p = self.shell('chrome_transport_summary local hw <<<\'{"schema":1,"error":"gone"}\'')
        self.assertEqual(p.returncode, 1)
        self.assertIn('transport could not be verified', p.stderr)

    def test_empty_overview_stays_local(self):
        p = self.shell('''tmux() { return 1; }
main status
''')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('Sessions', p.stdout)
        self.assertIn('None', p.stdout)
        self.assertNotIn('UNEXPECTED', p.stdout + p.stderr)

    def test_overview_keeps_each_exact_session_local(self):
        p = self.shell('''
tmux() {
 case "$1" in
  list-sessions) printf '%s\\n' remote-chrome-snap remote-chrome-work unrelated ;;
  list-windows) echo chrome ;;
  show-options) echo off:30:1073741824:- ;;
  *) return 97 ;;
 esac
}
chrome_reset_read_pane() { chrome_reset_pane_command="$1"; }
chrome_reset_parse_pane_host() {
 case "$1" in remote-chrome-snap) chrome_reset_host=snap ;; remote-chrome-work) chrome_reset_host=work ;; *) return 1 ;; esac
}
main status
''')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('remote-chrome status snap', p.stdout)
        self.assertIn('remote-chrome status work', p.stdout)
        self.assertNotIn('unrelated', p.stdout)
        self.assertNotIn('UNEXPECTED', p.stdout + p.stderr)

    def test_stop_success_waits_for_cleanup_and_failure_does_not_claim_success(self):
        script = '''tmux() { return TMUX_CODE; }
notify_stop_listener() { return 0; }
yk_prepare_for_launch() { :; }
yk_stop() { return 0; }
main stop snap
'''
        p = self.shell(script.replace('TMUX_CODE', '0'))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('Session cleanup completed', p.stdout)
        p = self.shell(script.replace('return TMUX_CODE;', 'case "$1" in has-session) return 0 ;; *) return 7 ;; esac'))
        self.assertEqual(p.returncode, 1)
        self.assertNotIn('cleanup completed', p.stdout)
        self.assertIn('Could not stop tmux session', p.stderr)

    def test_network_rows_distinguish_idle_missing_and_stale(self):
        values = dict(samples=300, unavailable=0, gaps=0, traffic_seconds=299,
                      rx_bytes=0, tx_bytes=0, traffic_peak_bytes_sec=0, retrans=0,
                      recording=dict(ack_tracking_samples=300))
        rows = dict(d.network_rows(values, 20, 'degraded'))
        self.assertIn('0.00 B/s', rows['Traffic'])
        self.assertIn('Unavailable / no recent ACK progress', rows['RTT/variation'])
        self.assertIn('STALE / 20.0s old', rows['Latest sample'])
        self.assertEqual(rows['Retransmits'], '0')
        self.assertEqual(dict(d.network_rows({}, None, 'unknown'))['Network'],
                         'Unavailable / no current-boot samples for this attempt')

    def test_network_rows_keep_coverage_delay_and_delivery_measurements(self):
        values = dict(samples=300, unavailable=2, gaps=1, traffic_seconds=297,
                      rx_bytes=297 * 250000, tx_bytes=297 * 2000, traffic_peak_bytes_sec=500000,
                      rtt_eligible=30, rtt_p50_ms=27, rtt_p95_ms=45, rttvar_p95_ms=8,
                      retrans=2, queue=1024, unsent=0)
        rows = dict(d.network_rows(values, .7, 'active'))
        self.assertIn('RX 2.00 Mbps / TX 1.95 KiB/s', rows['Traffic'])
        self.assertIn('27.00 ms p50 / 45.00 ms p95', rows['TCP RTT'])
        self.assertIn('variation proxy', rows['Variation'])
        self.assertIn('TCP 298/300 / gaps 1', rows['Coverage'])
        self.assertEqual(rows['Retransmits'], '2')


if __name__ == '__main__':
    unittest.main()
