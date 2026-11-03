#!/usr/bin/env python3
"""Generation-bound recovery using disposable processes and private runtime state."""
import contextlib
import io
import json
import os
from pathlib import Path
import pwd
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

LAUNCHER = Path(__file__).resolve().parents[1] / 'bin/remote-chrome'
GENERATION = 'a' * 32
SESSION = 'remote-chrome-fixture'
MODULE = LAUNCHER.read_text().split("<<'REMOTE_CHROME_SESSIONS'\n", 1)[1].split('\nREMOTE_CHROME_SESSIONS\n', 1)[0]
s = types.ModuleType('sessions')
exec(compile(MODULE, 'embedded-sessions', 'exec'), s.__dict__)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='remote-chrome-recovery-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runtime = self.base / 'runtime'
        self.runtime.mkdir(mode=0o700)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(self.runtime),
                        XDG_STATE_HOME=str(self.base / 'state'), PATH=str(self.bin) + ':' + os.environ['PATH'],
                        REMOTE_CHROME_CONFIG=str(self.base / 'no-config'), REMOTE_CHROME_NOTIFICATIONS='0')

    def shell(self, body, timeout=10):
        return subprocess.run(['bash', '-c', 'set -euo pipefail\nsource ' + shlex.quote(str(LAUNCHER)) + '\n' + body],
                              env=self.env, capture_output=True, text=True, timeout=timeout)

    def server(self, worker=False):
        socket = str(self.base / 'server.sock')
        state = self.runtime / 'remote-chrome-incoming-fixture.state'
        child = '''import os,time
from pathlib import Path
pid=os.getpid()
start=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
data={'pid':pid,'starttime':start,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
      'session':''' + repr(SESSION) + ''','generation':''' + repr(GENERATION) + '''}
path=Path(''' + repr(str(state)) + ''')
pending=path.with_suffix('.tmp')
pending.write_text(''.join(str(k)+'\\t'+str(v)+'\\n' for k,v in data.items()))
pending.chmod(0o600)
pending.replace(path)
time.sleep(60)
'''
        program = 'import subprocess,time;subprocess.Popen([' + repr(sys.executable) + ',"-c",' + repr(child) + ']);time.sleep(60)'
        if worker:
            program = 'import subprocess,time;subprocess.Popen(["waypipe","-c",' + repr(program) + ',"--socket",' + repr(socket) + ',"server"],executable=' + repr(sys.executable) + ');time.sleep(60)'
        p = subprocess.Popen(['waypipe', '-c', program, '--socket', socket, 'server'], executable=sys.executable,
                             env=self.env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def finish():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=5)
        self.addCleanup(finish)
        deadline = time.monotonic() + 5
        while not state.exists():
            if time.monotonic() >= deadline:
                self.fail('disposable remote owner did not publish')
            time.sleep(.02)
        owner = dict(line.split('\t', 1) for line in state.read_text().splitlines())
        pids = [p.pid]
        current = int(owner['pid'])
        while current != p.pid and len(pids) < 16:
            pids.append(current)
            current = int(Path(f'/proc/{current}/stat').read_text().rsplit(')', 1)[1].split()[1])
        # Only fixture processes enter the remote ps snapshot. No desktop or
        # browser process is inspected or signalled by these tests.
        ps = self.bin / 'ps'
        ps.write_text('#!' + sys.executable + '\nfrom pathlib import Path\n' +
                      'for pid in ' + repr(pids) + ':\n' +
                      ' try:\n' +
                      '  root=Path("/proc")/str(pid)\n' +
                      '  fields=(root/"stat").read_text().rsplit(")",1)[1].split()\n' +
                      '  if fields[0] in ("Z","X"):continue\n' +
                      '  cmd=(root/"cmdline").read_bytes().replace(b"\\0",b" ").decode()\n' +
                      '  print(pid,fields[1],fields[2],cmd)\n' +
                      ' except OSError:pass\n')
        ps.chmod(0o755)
        return p, state, socket

    def probe(self, socket, generation=GENERATION, session=SESSION):
        return self.shell('''chrome_reset_ssh_request() { ssh "$@"; }
ssh() { bash -c "${!#}"; }
chrome_reset_remote_identity test-host ''' + shlex.join([socket, session, '0', generation]) + '''
printf 'PID=%s SOCKET=%s\\n' "$chrome_reset_remote_pid" "$chrome_reset_remote_socket"
''')

    def test_remote_generation_resolves_socket_without_local_ssh(self):
        process, _, socket = self.server()
        p = self.probe('')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('PID=' + str(process.pid), p.stdout)
        self.assertIn('SOCKET=' + socket, p.stdout)

    def test_remote_generation_selects_server_root_above_waypipe_worker(self):
        process, _, socket = self.server(worker=True)
        p = self.probe('')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('PID=' + str(process.pid), p.stdout)
        self.assertIn('SOCKET=' + socket, p.stdout)

    def test_verified_remote_stop_ends_only_disposable_generation(self):
        process, _, _ = self.server(worker=True)
        unrelated = subprocess.Popen(['sleep', '60'], start_new_session=True)
        def stop_unrelated():
            unrelated.terminate()
            unrelated.wait(timeout=5)
        self.addCleanup(stop_unrelated)
        reaper = threading.Thread(target=process.wait, daemon=True)
        reaper.start()
        p = self.shell('''chrome_reset_ssh_request() { ssh "$@"; }
ssh() { bash -c "${!#}"; }
chrome_reset_remote_identity test-host '' ''' + shlex.join([SESSION, '0', GENERATION]) + '''
chrome_reset_remote_stop test-host "$chrome_reset_remote_socket" "$chrome_reset_remote_pgid"
code=0
chrome_reset_remote_identity test-host "$chrome_reset_remote_socket" ''' + shlex.join([SESSION, '1', GENERATION]) + ''' || code=$?
[[ "$code" = 1 ]]
''', timeout=15)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIsNotNone(process.poll())
        self.assertIsNone(unrelated.poll())
        reaper.join(timeout=2)

    def test_cached_socket_cannot_authorize_another_generation(self):
        process, _, socket = self.server()
        p = self.probe(socket, generation='b' * 32)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn('generation could not be verified', p.stderr)
        self.assertIsNone(process.poll())

    def test_reused_bootstrap_pid_start_does_not_authorize_server(self):
        process, path, socket = self.server()
        raw = path.read_text()
        start = next(line for line in raw.splitlines() if line.startswith('starttime\t'))
        path.write_text(raw.replace(start, 'starttime\t1'))
        p = self.probe(socket)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIsNone(process.poll())

    def test_other_session_cannot_use_generation_handle(self):
        process, _, socket = self.server()
        p = self.probe(socket, session='remote-chrome-other')
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIsNone(process.poll())

    def test_recorded_socket_mismatch_preserves_remote_owner(self):
        process, _, _ = self.server()
        p = self.probe(str(self.base / 'replacement.sock'))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn('socket differs', p.stderr)
        self.assertIsNone(process.poll())

    def test_timeout_policy_is_added_once_without_changing_video_or_browser_arguments(self):
        command = 'waypipe-remote-chrome --compress zstd=1 --video h264,hw,bpf=500000 --remote-bin waypipe-remote-chrome ssh snap chrome --profile-directory=Work'
        p = self.shell('first=$(chrome_recovery_command ' + shlex.quote(command) + ')\nsecond=$(chrome_recovery_command "$first")\n' +
                       '[[ "$first" = "$second" ]]\nprintf "%s\\n" "$second"\n')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.count('ServerAliveInterval=15'), 1)
        self.assertEqual(p.stdout.count('ControlPath=none'), 1)
        self.assertIn('ControlMaster=no', p.stdout)
        self.assertIn('bpf=500000', p.stdout)
        self.assertIn('snap chrome --profile-directory=Work', p.stdout)

    def test_reset_request_deadline_bounds_a_peer_that_never_returns(self):
        ssh = self.bin / 'ssh'
        ssh.write_text('#!/bin/sh\nexec sleep 60\n')
        ssh.chmod(0o755)
        timeout = self.bin / 'timeout'
        real_timeout = shutil.which('timeout')
        # Accelerate the fixture while asserting the real requested budget.
        timeout.write_text('#!/bin/sh\n[ "$1" = --kill-after=1 ] && [ "$2" = 10 ] || exit 97\nshift 2\nexec ' + shlex.quote(real_timeout) + ' --kill-after=1 .2 "$@"\n')
        timeout.chmod(0o755)
        started = time.monotonic()
        p = self.shell('chrome_reset_ssh_request test-host sleep\n')
        self.assertEqual(p.returncode, 124, p.stderr)
        self.assertLess(time.monotonic() - started, 3)

    def test_real_ssh_idle_survives_and_blackhole_ends_with_recoverable_record(self):
        for name in ('host', 'client'):
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.base / name)], check=True)
        with socket.socket() as port:
            port.bind(('127.0.0.1', 0))
            server_port = port.getsockname()[1]
        config = self.base / 'sshd_config'
        config.write_text(f'Port {server_port}\nListenAddress 127.0.0.1\nHostKey {self.base}/host\n'
                          f'PidFile {self.base}/pid\nAuthorizedKeysFile {self.base}/client.pub\n'
                          'StrictModes no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n'
                          f'UsePAM no\nLogLevel ERROR\nAllowUsers {pwd.getpwuid(os.getuid()).pw_name}\n')
        server_log = (self.base / 'sshd.log').open('wb')
        self.addCleanup(server_log.close)
        server = subprocess.Popen([shutil.which('sshd'), '-D', '-e', '-f', str(config)],
                                  stdout=server_log, stderr=server_log, start_new_session=True)
        def stop_server():
            server.terminate()
            server.wait(timeout=5)
        self.addCleanup(stop_server)
        deadline = time.monotonic() + 5
        while True:
            if server.poll() is not None:
                self.fail((self.base / 'sshd.log').read_text())
            try:
                with socket.create_connection(('127.0.0.1', server_port), timeout=.1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.03)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        proxy_port = listener.getsockname()[1]
        listener.settimeout(.2)
        dropping, done = threading.Event(), threading.Event()
        proxy_errors = []
        def proxy():
            peers = []
            try:
                while not done.is_set():
                    try:
                        client, _ = listener.accept()
                        break
                    except socket.timeout:
                        continue
                else:
                    return
                remote = socket.create_connection(('127.0.0.1', server_port), timeout=2)
                peers = [client, remote]
                with selectors.DefaultSelector() as selector:
                    selector.register(client, selectors.EVENT_READ, remote)
                    selector.register(remote, selectors.EVENT_READ, client)
                    while not done.is_set():
                        for key, _ in selector.select(.1):
                            data = key.fileobj.recv(65536)
                            if not data:
                                return
                            if not dropping.is_set():
                                key.data.sendall(data)
            except OSError as error:
                if not done.is_set():
                    proxy_errors.append(str(error))
            finally:
                for peer in peers:
                    peer.close()
        thread = threading.Thread(target=proxy, daemon=True)
        thread.start()
        def stop_proxy():
            done.set()
            thread.join(timeout=2)
            listener.close()
        self.addCleanup(stop_proxy)
        known = self.base / 'known_hosts'
        known.write_text(f'[127.0.0.1]:{proxy_port} ' + (self.base / 'host.pub').read_text())
        fifo = self.base / 'input'
        os.mkfifo(fifo, 0o600)
        writer = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, writer)
        control = self.base / 'control.sock'
        # Fixture keepalives are accelerated to one second; command-construction
        # coverage separately checks the shipped 15-second / three-check policy.
        ssh = [shutil.which('ssh'), '-F', '/dev/null', '-p', str(proxy_port), '-i', str(self.base / 'client'),
               '-M', '-S', str(control), '-o', 'IdentityAgent=none', '-o', 'IdentitiesOnly=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(known),
               '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=1', '-o', 'ServerAliveCountMax=3',
               '127.0.0.1', 'python3 -c ' + shlex.quote('import sys;print("READY",flush=True);sys.stdin.buffer.read()')]
        module = self.base / 'sessions.py'
        module.write_text(MODULE)
        fake = self.base / 'launcher'
        fake.write_text('#!/bin/sh\nexit 0\n')
        fake.chmod(0o755)
        env = dict(self.env)
        with patch.dict(os.environ, env), patch.object(s, 'ROOT', self.runtime / f'remote-chrome-sessions-{os.getuid()}'):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                s.reserve([SESSION, 'fixture', shlex.join(ssh) + ' < ' + shlex.quote(str(fifo)), 'off',
                           'off:30:1073741824:-', 'lossless', 'test', str(fake), str(os.getpid()),
                           str(control), '1050:0407', '3240', ''])
            generation = output.getvalue().strip()
            row = s.load(SESSION)
            row['phase'] = 'starting'
            s.save(row)
            with (self.base / 'stdout').open('wb') as out, (self.base / 'stderr').open('wb') as err:
                process = subprocess.Popen([sys.executable, str(module), 'run', SESSION, generation], env=env, stdout=out, stderr=err)
                def stop_process():
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)
                self.addCleanup(stop_process)
                deadline = time.monotonic() + 8
                while 'READY' not in (self.base / 'stdout').read_text():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail((self.base / 'stderr').read_text())
                    time.sleep(.03)
                time.sleep(4)
                self.assertIsNone(process.poll(), 'idle connection was treated as failed')
                dropping.set()
                started = time.monotonic()
                process.wait(timeout=8)
                elapsed = time.monotonic() - started
                self.assertGreater(elapsed, 1.5, 'proxy closed instead of keeping the failed connection open')
                self.assertLess(elapsed, 7)
                self.assertEqual(proxy_errors, [])
            row = s.load(SESSION)
            self.assertEqual(row['transport_exit'], 255)
            self.assertEqual(row['reason'], 'transport-failed')
            self.assertEqual(s.cleanup(SESSION, generation), 0)
            self.assertEqual(s.load(SESSION)['phase'], 'stopped')
            self.assertEqual(s.main(['recoverable', SESSION]), 0)
            p = self.shell('_chrome_session_usb_check ' + shlex.join([str(control), '127.0.0.1']) + '\n')
            self.assertEqual(p.returncode, 1, p.stderr)


if __name__ == '__main__':
    unittest.main()
