#!/usr/bin/env python3
"""Bounded USB SSH control-socket liveness checks."""
import contextlib
import os
from pathlib import Path
import pwd
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

LAUNCHER = Path(__file__).resolve().parents[1] / 'bin/remote-chrome'


class UsbLivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='remote-chrome-usb-liveness-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, HOME=str(self.base), PATH=str(self.bin) + ':' + os.environ['PATH'])

    def shell(self, body, timeout=5):
        return subprocess.run(
            ['bash', '-c', 'set -euo pipefail\nsource ' + shlex.quote(str(LAUNCHER)) + '\n' + body],
            env=self.env, capture_output=True, text=True, timeout=timeout)

    def check_socket(self, path, remote='fixture', timeout=5):
        command = '_chrome_session_usb_check ' + shlex.join([str(path), remote])
        return self.shell('if ' + command + '; then exit 0; else code=$?; exit "$code"; fi\n', timeout=timeout)

    def create_socket(self, path):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen()

        def cleanup():
            listener.close()
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

        self.addCleanup(cleanup)
        return listener

    def fake_ssh(self, source):
        path = self.bin / 'ssh'
        path.write_text('#!' + sys.executable + '\n' + source)
        path.chmod(0o755)
        return path

    def test_absent_socket_is_dead_without_starting_ssh(self):
        marker = self.base / 'ssh-called'
        self.fake_ssh('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
        missing = self.base / 'missing.sock'

        result = self.check_socket(missing)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(result.stdout + result.stderr, '')

    def test_private_real_ssh_master_is_alive(self):
        ssh, control, remote = self.start_private_master()

        result = self.check_socket(control, remote)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(control.is_socket())
        self.assertIsNone(ssh.poll())
        self.assertEqual(result.stdout + result.stderr, '')

    def test_killed_real_ssh_master_leaves_dead_socket_without_cleanup(self):
        ssh, control, remote = self.start_private_master()
        self.assertEqual(self.check_socket(control, remote).returncode, 0)
        socket_identity = control.stat().st_ino

        ssh.kill()
        ssh.wait(timeout=5)
        self.assertTrue(control.is_socket(), 'the killed master should leave its control socket behind')

        result = self.check_socket(control, remote)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(control.is_socket(), 'the checker must preserve the stale control socket')
        self.assertEqual(control.stat().st_ino, socket_identity)
        self.assertEqual(result.stdout + result.stderr, '')

    def test_ambiguous_ssh_error_preserves_socket(self):
        path = self.base / 'control.sock'
        self.create_socket(path)
        self.fake_ssh(
            'import os, sys\n'
            'assert os.environ.get("LC_ALL") == "C"\n'
            'print(' + repr(f'Control socket connect({path}): Permission denied') + ', file=sys.stderr)\n'
            'raise SystemExit(255)\n')

        result = self.check_socket(path)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(path.is_socket())
        self.assertEqual(result.stdout + result.stderr, '')

    def test_unsafe_socket_permissions_are_unknown_without_ssh(self):
        path = self.base / 'control.sock'
        self.create_socket(path)
        path.chmod(0)
        marker = self.base / 'ssh-called'
        self.fake_ssh('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')

        result = self.check_socket(path)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(path.is_socket())
        self.assertEqual(path.stat().st_mode & 0o777, 0)
        self.assertFalse(marker.exists())
        self.assertEqual(result.stdout + result.stderr, '')

    def test_unreadable_parent_is_unknown_not_absent(self):
        if os.geteuid() == 0:
            self.skipTest('root can bypass directory search permissions')
        parent = self.base / 'private'
        parent.mkdir(mode=0o700)
        path = parent / 'control.sock'
        self.create_socket(path)
        parent.chmod(0)
        marker = self.base / 'ssh-called'
        self.fake_ssh('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')

        try:
            result = self.check_socket(path)
        finally:
            parent.chmod(0o700)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(path.is_socket())
        self.assertFalse(marker.exists())

    def test_symlink_socket_path_is_unknown_and_preserved(self):
        target = self.base / 'target.sock'
        self.create_socket(target)
        path = self.base / 'control.sock'
        path.symlink_to(target)
        marker = self.base / 'ssh-called'
        self.fake_ssh('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')

        result = self.check_socket(path)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(path.is_symlink())
        self.assertTrue(target.is_socket())
        self.assertFalse(marker.exists())

    def test_hardlinked_socket_is_unknown_without_starting_ssh(self):
        path = self.base / 'control.sock'
        self.create_socket(path)
        alias = self.base / 'alias.sock'
        os.link(path, alias)
        marker = self.base / 'ssh-called'
        self.fake_ssh('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')

        result = self.check_socket(path)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(path.stat().st_ino, alias.stat().st_ino)
        self.assertEqual(path.stat().st_nlink, 2)

    def test_replaced_socket_during_refusal_is_unknown(self):
        path = self.base / 'control.sock'
        self.create_socket(path)
        original_inode = path.stat().st_ino
        replacement = path.with_suffix('.replacement')
        self.fake_ssh(
            'import os, socket, sys\n'
            'from pathlib import Path\n'
            'path = Path(' + repr(str(path)) + ')\n'
            'path.rename(' + repr(str(replacement)) + ')\n'
            'listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n'
            'listener.bind(str(path))\n'
            'os.chmod(path, 0o600)\n'
            'print(' + repr(f'Control socket connect({path}): Connection refused') + ', file=sys.stderr)\n'
            'raise SystemExit(255)\n')

        result = self.check_socket(path)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(path.is_socket())
        self.assertNotEqual(path.stat().st_ino, original_inode)
        self.assertTrue(replacement.is_socket())
        self.assertEqual(result.stdout + result.stderr, '')

    def test_socket_change_time_detects_mutation_when_mode_is_restored(self):
        path = self.base / 'control.sock'
        self.create_socket(path)
        self.fake_ssh(
            'import os, sys, time\n'
            'path = ' + repr(str(path)) + '\n'
            'os.chmod(path, 0o640)\n'
            'time.sleep(0.003)\n'
            'os.chmod(path, 0o600)\n'
            'print(' + repr(f'Control socket connect({path}): Connection refused') + ', file=sys.stderr)\n'
            'raise SystemExit(255)\n')

        result = self.check_socket(path)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(path.is_socket())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result.stdout + result.stderr, '')

    def test_timeout_is_unknown_and_bounded(self):
        path = self.base / 'control.sock'
        self.create_socket(path)
        self.fake_ssh('import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n')
        started = time.monotonic()

        result = self.check_socket(path, timeout=4)

        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertLess(elapsed, 3, f'USB liveness check took {elapsed:.2f}s')
        self.assertTrue(path.is_socket())
        self.assertEqual(result.stdout + result.stderr, '')

    def start_private_master(self):
        ssh = shutil.which('ssh')
        sshd = shutil.which('sshd')
        ssh_keygen = shutil.which('ssh-keygen')
        if not ssh or not sshd or not ssh_keygen:
            self.skipTest('OpenSSH client, server, and keygen are required for the real master fixture')

        host_key = self.base / 'host'
        client_key = self.base / 'client'
        for key in (host_key, client_key):
            subprocess.run([ssh_keygen, '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                           env=self.env, check=True, capture_output=True)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]

        username = pwd.getpwuid(os.getuid()).pw_name
        config = self.base / 'sshd_config'
        config.write_text(
            f'Port {port}\nListenAddress 127.0.0.1\nHostKey {host_key}\n'
            f'PidFile {self.base / "sshd.pid"}\nAuthorizedKeysFile {client_key}.pub\n'
            'StrictModes no\nPubkeyAuthentication yes\nPasswordAuthentication no\n'
            'KbdInteractiveAuthentication no\nUsePAM no\nLogLevel ERROR\n'
            f'AllowUsers {username}\n')
        server_log_path = self.base / 'sshd.log'
        server_log = server_log_path.open('wb')
        self.addCleanup(server_log.close)
        server = subprocess.Popen([sshd, '-D', '-e', '-f', str(config)], env=self.env,
                                  stdout=server_log, stderr=server_log, start_new_session=True)

        def stop_server():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(server.pid, signal.SIGKILL)
                server.wait(timeout=5)

        self.addCleanup(stop_server)
        deadline = time.monotonic() + 5
        while True:
            if server.poll() is not None:
                self.fail(server_log_path.read_text())
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    self.fail(server_log_path.read_text())
                time.sleep(0.03)

        known_hosts = self.base / 'known_hosts'
        known_hosts.write_text(f'[127.0.0.1]:{port} ' + (self.base / 'host.pub').read_text())
        control = self.base / 'control.sock'
        master_log_path = self.base / 'ssh-master.log'
        master_log = master_log_path.open('wb')
        self.addCleanup(master_log.close)
        master = subprocess.Popen(
            [ssh, '-F', '/dev/null', '-p', str(port), '-i', str(client_key), '-M', '-S', str(control),
             '-o', 'IdentityAgent=none', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
             '-o', 'UserKnownHostsFile=' + str(known_hosts), '-o', 'GlobalKnownHostsFile=/dev/null',
             '-o', 'BatchMode=yes', '-N', f'{username}@127.0.0.1'],
            env=self.env, stdout=master_log, stderr=master_log, start_new_session=True)

        def stop_master():
            if master.poll() is None:
                master.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                master.wait(timeout=5)

        self.addCleanup(stop_master)
        deadline = time.monotonic() + 5
        while not control.exists():
            if master.poll() is not None:
                self.fail(master_log_path.read_text())
            if time.monotonic() > deadline:
                self.fail('private OpenSSH master did not create its control socket')
            time.sleep(0.02)
        return master, control, f'{username}@127.0.0.1'


if __name__ == '__main__':
    unittest.main()
