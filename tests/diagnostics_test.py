#!/usr/bin/env python3
"""Exercise the shipped embedded helper with private stores and real lifetimes."""
import contextlib
import errno
import fcntl
import io
import json
import multiprocessing
import os
from pathlib import Path
import signal
import socket
import subprocess
import struct
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import uuid

LAUNCHER = Path(__file__).resolve().parents[1] / "bin/remote-chrome"
SOURCE = LAUNCHER.read_text().split("<<'REMOTE_CHROME_DIAGNOSTICS'\n", 1)[1].split("\nREMOTE_CHROME_DIAGNOSTICS\n", 1)[0]
d = types.ModuleType("diagnostics")
exec(compile(SOURCE, "embedded-diagnostics", "exec"), d.__dict__)


def record(attempt=None, role="viewer", **values):
    return dict(schema=1, attempt=attempt or str(uuid.uuid4()), role=role,
                origin=socket.gethostname(), target="snap", session="remote-chrome-snap",
                host=socket.gethostname(), **d.now(), event="sample", **values)


def concurrent_writer(base, barrier, queue):
    d.CHUNK = 2048
    s = d.Store(cap=8192, base=base)
    barrier.wait(timeout=5)
    success = False
    for _ in range(30):
        if s.append(record(payload="x" * 700)):
            success = True
            break
        time.sleep(0.01)
    queue.put(success)
    barrier.wait(timeout=5)
    s.close()


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="remote-chrome-diagnostics-test-")
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, XDG_STATE_HOME=str(self.base))
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def store(self, **kwargs):
        store = d.Store(**kwargs)
        self.addCleanup(store.close)
        return store

    def test_private_store_and_file_modes(self):
        s = self.store()
        self.assertTrue(s.append(record()))
        self.assertEqual(s.path.stat().st_mode & 0o777, 0o700)
        for path in s.path.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_existing_readable_parent_is_preserved(self):
        parent = self.base / "remote-chrome"
        parent.mkdir(mode=0o755)
        s = self.store()
        self.assertTrue(s.append(record()))
        self.assertEqual(parent.stat().st_mode & 0o777, 0o755)

    def test_symlink_parent_and_diagnostic_directory_rejected(self):
        parent = self.base / "remote-chrome"
        target = self.base / "target"
        target.mkdir()
        parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            d.Store()
        parent.unlink()
        parent.mkdir(mode=0o700)
        (parent / "diagnostics").symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            d.Store()
        self.assertEqual(list(target.iterdir()), [])

    def test_public_directory_and_unsafe_lock_rejected(self):
        parent = self.base / "remote-chrome"
        parent.mkdir(mode=0o700)
        path = parent / "diagnostics"
        path.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            d.Store()
        path.chmod(0o700)
        (path / ".lock").symlink_to(self.base / "missing")
        with self.assertRaises(OSError):
            d.Store()

    def test_foreign_and_hardlinked_file_rejected(self):
        s = self.store()
        self.assertTrue(s.append(record()))
        s.rotate()
        name = s.chunks()[0][1]
        os.link(s.path / name, self.base / "copy")
        with self.assertRaises(ValueError):
            s.prune()
        with patch.object(d.os, "getuid", return_value=123456):
            # The safety predicate uses the captured real uid, not a pathname.
            info = types.SimpleNamespace(st_mode=0o100600, st_uid=123456, st_nlink=1)
            self.assertFalse(d.private(info))

    def test_age_prunes_closed_but_preserves_open_chunk(self):
        s = self.store()
        s.append(record())
        first = s.chunks()[0][1]
        os.utime(s.path / first, (time.time() - 31 * 86400,) * 2)
        self.assertTrue(s.prune())
        self.assertTrue((s.path / first).exists())
        s.rotate()
        self.assertTrue(s.prune())
        self.assertFalse((s.path / first).exists())

    def test_quota_is_shared_across_roles_and_attempts(self):
        with patch.object(d, "CHUNK", 2048):
            a, b = self.store(cap=4096), self.store(cap=4096)
            self.assertTrue(a.append(record(role="viewer")))
            first = a.chunks()[0][1]
            self.assertTrue(b.append(record(role="browser")))
            a.rotate()
            self.assertTrue(a.append(record()))
            self.assertFalse((a.path / first).exists())
            self.assertLessEqual(sum(size for _, _, size in a.chunks()), 4096)

    def test_full_quota_skips_without_deleting_open_writers(self):
        with patch.object(d, "CHUNK", 2048):
            a, b = self.store(cap=2048), self.store(cap=2048)
            self.assertTrue(a.append(record()))
            first = a.chunks()[0][1]
            self.assertFalse(b.append(record()))
            self.assertTrue((a.path / first).exists())
            self.assertTrue(a.append(record()))

    def test_quota_rechecks_size_after_writer_closes(self):
        with patch.object(d, "CHUNK", 2048):
            s = self.store(cap=2048)
            self.assertTrue(s.append(record()))
            s.rotate()
            name = s.chunks()[0][1]
            stale_scan = [(stamp, filename, 0) for stamp, filename, _ in s.chunks()]
            with patch.object(s, "chunks", return_value=stale_scan):
                self.assertTrue(s.append(record()))
            self.assertFalse((s.path / name).exists())

    def test_busy_lock_never_waits(self):
        a, b = self.store(), self.store()
        fcntl.flock(a.lock, fcntl.LOCK_EX)
        start = time.monotonic()
        self.assertFalse(b.append(record()))
        self.assertFalse(b.prune())
        self.assertLess(time.monotonic() - start, 0.1)
        fcntl.flock(a.lock, fcntl.LOCK_UN)

    def test_concurrent_writers_reserve_before_writing(self):
        barrier, queue = multiprocessing.Barrier(5), multiprocessing.Queue()
        children = [multiprocessing.Process(target=concurrent_writer,
                    args=(str(self.base), barrier, queue)) for _ in range(4)]
        for child in children:
            child.start()
        try:
            barrier.wait(timeout=5)
            self.assertTrue(all(queue.get(timeout=5) for _ in children))
            total = sum(p.stat().st_size for p in (self.base / "remote-chrome/diagnostics").glob("*.jsonl"))
            self.assertLessEqual(total, 8192)
            barrier.wait(timeout=5)
        finally:
            for child in children:
                child.join(timeout=5)
                if child.is_alive():
                    child.kill()
                    child.join()
                self.assertEqual(child.exitcode, 0)

    def test_disk_full_is_reported_and_readable_history_survives(self):
        s = self.store()
        row = record()
        self.assertTrue(s.append(row))
        with patch.object(d.os, "write", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                s.append(record())
        rows = list(s.records(0, time.time()))
        self.assertEqual([r["attempt"] for r in rows], [row["attempt"]])

    def test_query_filters_time_origin_session_and_incomplete_line(self):
        s = self.store()
        first = record()
        s.append(first)
        other = record()
        other.update(origin="elsewhere", session="other")
        s.append(other)
        os.write(s.writer, b'{"partial":')
        rows = list(s.records(first["wall"] - 1, time.time(), "snap", socket.gethostname(), "remote-chrome-snap"))
        self.assertEqual(rows, [first])
        self.assertEqual(list(s.records(time.time() + 5, time.time() + 10)), [])

    def test_query_read_limit_is_explicit(self):
        s = self.store()
        s.append(record())
        with patch.object(d, "READ_LIMIT", 10):
            with self.assertRaisesRegex(ValueError, "shorter time window"):
                list(s.records(0, time.time()))

    def test_socket_parse_and_unavailable_fields(self):
        raw = 'ESTAB 12 34 100.1.2.3:456 [fd7a::123]:22 uid:1000 ino:4321 sk:abc rtt:18.2/2.1 bytes_sent:2048 bytes_received:1024 bytes_retrans:80 retrans:0/3 cwnd:12'
        row = d.parse_ss(raw)[0]
        self.assertEqual(row["rtt_ms"], 18.2)
        self.assertEqual(row["rttvar_ms"], 2.1)
        self.assertEqual(row["retrans_total"], 3)
        self.assertIsNone(row["segs_in"])
        self.assertEqual((row["recv_queue"], row["send_queue"]), (12, 34))
        self.assertEqual(d.parse_ss("garbage"), [])

    def test_counter_differences_require_same_socket_and_monotonic_counters(self):
        a = dict(available=True, inode="1", cookie="a", local=["a", 1], peer=["b", 2],
                 bytes_sent=100, bytes_received=50, retrans_total=2, bytes_retrans=None)
        b = dict(a, bytes_sent=120, bytes_received=60, retrans_total=3)
        delta, reason = d.differences(b, a, 2)
        self.assertIsNone(reason)
        self.assertEqual(delta["bytes_sent"], 20)
        self.assertIsNone(delta["bytes_retrans"])
        self.assertEqual(d.differences(dict(b, cookie="new"), a, 2)[1], "socket identity changed")
        self.assertEqual(d.differences(dict(b, bytes_sent=1), a, 2)[1], "counter discontinuity")
        self.assertIsNone(d.differences(dict(available=False), a, 2)[0])

    def test_browser_tcp_requires_exact_four_tuple(self):
        raw = 'ESTAB 0 0 100.2.2.2:22 100.1.1.1:42 ino:123 sk:abc rtt:9/1'
        with patch.object(d, "bounded", return_value=raw):
            self.assertTrue(d.tcp_sample("browser", {}, "100.1.1.1 42 100.2.2.2 22", None)["available"])
            self.assertFalse(d.tcp_sample("browser", {}, "100.1.1.1 43 100.2.2.2 22", None)["available"])
            self.assertFalse(d.tcp_sample("browser", {}, "", None)["available"])

    def test_viewer_refuses_ambiguous_ssh_processes(self):
        rows = {1: dict(exe="/usr/bin/ssh"), 2: dict(exe="/usr/bin/ssh")}
        with patch.object(d, "bounded") as tool:
            self.assertFalse(d.tcp_sample("viewer", rows, "", None)["available"])
            tool.assert_not_called()

    def test_passive_path_never_infers_derp_from_home_region(self):
        status = {"Peer": {"privatekey": dict(TailscaleIPs=["100.1.1.1"], CurAddr="", Relay="sea", Active=True)}}
        def answer(command, **_):
            self.assertNotIn("ping", command)
            return json.dumps(status) if command[0] == "tailscale" else "[]"
        with patch.object(d, "bounded", side_effect=answer):
            row = d.path_sample("100.1.1.1")
        self.assertEqual(row["tailscale"]["mode"], "unknown")
        self.assertIsNone(row['underlay_route'])
        self.assertNotIn("privatekey", json.dumps(row))

    def test_bounded_tools_timeout_and_output_limit(self):
        start = time.monotonic()
        self.assertIsNone(d.bounded(["python3", "-c", "import time;time.sleep(30)"], timeout=0.1))
        self.assertLess(time.monotonic() - start, 1)
        self.assertIsNone(d.bounded(["python3", "-c", "print('x'*10000)"], limit=100))

    def test_exact_process_identity_rejects_reused_start_and_zombie(self):
        child = subprocess.Popen(["sleep", "30"])
        try:
            identity = d.process(child.pid)
            self.assertIsNotNone(d.exact(child.pid, identity["start"]))
            self.assertIsNone(d.exact(child.pid, "0"))
        finally:
            child.terminate()
            child.wait()
        self.assertIsNone(d.exact(child.pid, identity["start"]))

    def test_collector_stops_when_exact_owner_exits_and_does_not_signal_it(self):
        helper = self.base / "helper.py"
        helper.write_text(SOURCE)
        owner = subprocess.Popen(["sleep", "30"])
        start = d.process(owner.pid)["start"]
        attempt = str(uuid.uuid4())
        collector = subprocess.Popen(["python3", str(helper), "collect", "snap", "--owner", str(owner.pid),
                                      "--start", start, "--attempt", attempt, "--origin", socket.gethostname()])
        try:
            deadline = time.monotonic() + 5
            while not list((self.base / "remote-chrome/diagnostics").glob("*.jsonl")) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNone(owner.poll())
            owner.terminate()
            owner.wait()
            self.assertEqual(collector.wait(timeout=5), 0)
            rows = list(self.store().records(0, time.time()))
            self.assertEqual({r["attempt"] for r in rows}, {attempt})
            self.assertIn("start", [r["event"] for r in rows])
            self.assertIn("end", [r["event"] for r in rows])
        finally:
            if owner.poll() is None:
                owner.terminate()
                owner.wait()
            if collector.poll() is None:
                collector.terminate()
                collector.wait(timeout=5)

    def test_summary_does_not_turn_missing_data_into_zero(self):
        rows = [record(tcp=dict(available=False), delta=None, processes=[])]
        summary = d.summarize(rows)[0]
        self.assertIsNone(summary["rtt_p95_ms"])
        self.assertIsNone(summary["traffic_peak_bytes_sec"])
        self.assertIsNone(summary["retrans"])
        self.assertEqual(summary["unavailable"], 1)

    def test_mark_is_timestamp_only_and_links_attempt(self):
        s = self.store()
        original = record()
        s.append(original)
        with contextlib.redirect_stdout(io.StringIO()):
            d.main(["mark", "snap"])
        marks = [r for r in s.records(0, time.time()) if r["event"] == "mark"]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["attempt"], original["attempt"])
        self.assertNotIn("argv", marks[0])

    def test_status_is_read_only_without_or_with_old_history(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.main(["status", "snap"]), 0)
        self.assertFalse((self.base / "remote-chrome").exists())
        s = self.store()
        s.append(record())
        s.rotate()
        name = s.chunks()[0][1]
        os.utime(s.path / name, (time.time() - 31 * 86400,) * 2)
        with contextlib.redirect_stdout(io.StringIO()):
            d.main(["status", "snap"])
        self.assertTrue((s.path / name).exists())

    def test_helper_observation_keeps_error_text_out_and_checks_owner(self):
        runtime = self.base / "runtime"
        runtime.mkdir(mode=0o700)
        path = runtime / "remote-chrome-incoming-fixture.state"
        path.write_text(f"pid\t123\nstarttime\t456\nboot_id\t{d.BOOT}\nnotify_backend\tunavailable\n"
                        "notify_error\tprivate notification error text\n")
        path.chmod(0o600)
        args = types.SimpleNamespace(role="browser", owner=123, start="456")
        with patch.dict(os.environ, XDG_RUNTIME_DIR=str(runtime)):
            result = d.helper_observation(args)
            self.assertTrue(result["startup_error"])
            self.assertNotIn("private", json.dumps(result))
            args.start = "reused"
            self.assertFalse(d.helper_observation(args)["available"])

    def test_partial_remote_results_and_private_bounded_export(self):
        s = self.store()
        old = record()
        old["wall"] -= 3600
        s.append(old)
        row = record()
        s.append(row)
        output = self.base / "incident.jsonl"
        with patch.object(d, "bounded", return_value=None), contextlib.redirect_stdout(io.StringIO()) as text:
            self.assertEqual(d.main(["query", "snap", "--since", "20m", "--export", str(output)]), 0)
        self.assertIn("Browser endpoint: unavailable", text.getvalue())
        exported = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual(exported[1:], [row])
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with patch.object(d, "bounded", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(FileExistsError):
                d.main(["query", "snap", "--export", str(output)])

    def test_managed_export_and_unbounded_windows_rejected(self):
        s = self.store()
        with patch.object(d, "bounded", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "outside"):
                d.main(["query", "snap", "--export", str(s.path / "export")])
        for value in ("0m", "31d", "all", "-1h"):
            with self.assertRaises(ValueError):
                d.duration(value)

    def test_bootstrap_freezes_attempt_and_has_no_external_module_dependency(self):
        script = 'source "$1"; chrome_transport_defaults; chrome_diagnostics=on; chrome_diag_policy=video; '
        script += 'chrome_diagnostics_prepare snap remote-chrome-snap; chrome_secure_bootstrap_script'
        env = dict(os.environ, REMOTE_CHROME_CONFIG=str(self.base / "missing"))
        first = subprocess.check_output(["bash", "-c", script, "test", str(LAUNCHER)], env=env, text=True)
        second = subprocess.check_output(["bash", "-c", script, "test", str(LAUNCHER)], env=env, text=True)
        self.assertIn(SOURCE, first)
        ids = [line for line in first.splitlines() if line.startswith("chrome_diag_attempt=")]
        self.assertEqual(len(ids), 1)
        self.assertNotIn(ids[0], second)
        subprocess.run(["bash", "-n"], input=first, text=True, check=True)

    def test_configuration_is_data_and_validates_limits(self):
        config = self.base / "config"
        config.write_text("diagnostics=on\ndiagnostics_retention_days=30\ndiagnostics_max_bytes=1073741824\n")
        env = dict(os.environ, REMOTE_CHROME_CONFIG=str(config))
        command = ["bash", "-c", 'source "$1"; chrome_transport_defaults', "test", str(LAUNCHER)]
        self.assertEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
        for text in ("diagnostics=maybe\n", "diagnostics_retention_days=31\n", "diagnostics_max_bytes=1073741825\n",
                     "diagnostics=$(touch should-not-exist)\n"):
            config.write_text(text)
            self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
        self.assertFalse((LAUNCHER.parent.parent / "should-not-exist").exists())

    def measured(self, wall, **changes):
        tcp = dict(available=True, bytes_sent=100, bytes_received=100, bytes_acked=100,
                   send_queue=0, notsent_bytes=0, unacked=0, last_ack_ms=10,
                   rtt_ms=10, rttvar_ms=1, retrans_total=0)
        tcp.update(changes.pop('tcp', {}))
        delta = dict(seconds=1, bytes_sent=100, bytes_received=100, bytes_acked=100,
                     retrans_total=0, bytes_retrans=0)
        delta.update(changes.pop('delta', {}))
        row = record(attempt='11111111-1111-4111-8111-111111111111', tcp=tcp, delta=delta, processes=[])
        row.update(schema=2, wall=wall, mono=wall,
                   utc=d.dt.datetime.fromtimestamp(wall, d.dt.timezone.utc).isoformat())
        row.update(changes)
        return row

    def query_args(self, **changes):
        values = dict(days=30, cap=1073741824, target='snap', origin=socket.gethostname(),
                      session='', since_wall=time.time() - 60, until_wall=time.time() + 1,
                      include_records=False, resolution='auto')
        values.update(changes)
        return types.SimpleNamespace(**values)

    def test_tcp_info_known_zero_and_short_abi_are_distinct(self):
        full = d.tcp_info(bytes(280))
        self.assertEqual(full['retrans_total'], 0)
        self.assertEqual(full['bytes_retrans'], 0)
        self.assertEqual(full['notsent_bytes'], 0)
        short = d.tcp_info(bytes(104))
        self.assertEqual(short['retrans_total'], 0)
        self.assertIsNone(short['bytes_retrans'])
        self.assertIsNone(short['notsent_bytes'])

    def test_kernel_socket_matches_own_loopback_tcp_info(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with socket.create_connection(listener.getsockname()) as client:
                accepted, _ = listener.accept()
                with accepted:
                    client.sendall(b'diagnostics-fixture')
                    accepted.recv(1024)
                    actual = d.kernel_socket(client.getsockname(), client.getpeername())
                    if actual is None:
                        self.skipTest('SOCK_DIAG not available in this environment')
                    expected = d.tcp_info(client.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 512))
                    for key in ('bytes_sent', 'bytes_acked', 'retrans_total', 'bytes_retrans', 'notsent_bytes'):
                        self.assertEqual(actual[key], expected[key])

    def test_ss_suppressed_retrans_zero_does_not_lose_first_retransmission(self):
        zero = d.parse_ss('ESTAB 0 0 100.1.1.1:1 100.2.2.2:2 ino:1 sk:a rtt:10/1 cwnd:10')[0]
        one = d.parse_ss('ESTAB 0 0 100.1.1.1:1 100.2.2.2:2 ino:1 sk:a rtt:10/1 cwnd:10 retrans:0/1')[0]
        self.assertEqual(zero['retrans_total'], 0)
        delta, _ = d.differences(dict(zero, available=True), dict(zero, available=True), 1)
        self.assertEqual(delta['retrans_total'], 0)
        delta, _ = d.differences(dict(one, available=True), dict(zero, available=True), 1)
        self.assertEqual(delta['retrans_total'], 1)

    def test_root_accepted_socket_requires_explicit_browser_exception(self):
        local, peer = ['127.0.0.1', 22], ['127.0.0.2', 42000]
        body = bytearray(72)
        body[0] = socket.AF_INET
        struct.pack_into('!HH', body, 4, local[1], peer[1])
        body[8:12] = socket.inet_aton(local[0])
        body[24:28] = socket.inet_aton(peer[0])
        struct.pack_into('=IIII', body, 56, 0, 0, 0, 123)
        body += struct.pack('=HH', 108, 2) + bytes(104)
        message = struct.pack('=IHHII', 16 + len(body), 20, 0, os.getpid(), 0) + body
        channel = unittest.mock.MagicMock()
        channel.__enter__.return_value = channel
        channel.recvmsg.return_value = (message, [], 0, (0, 0))
        with patch.object(d, 'UID', 1000), patch.object(d.socket, 'socket', return_value=channel):
            self.assertIsNone(d.kernel_socket(local, peer))
            self.assertEqual(d.kernel_socket(local, peer, allow_server=True)['socket_uid'], 0)
            self.assertIsNone(d.kernel_socket(local, ['127.0.0.3', 42000], allow_server=True))

    def test_idle_rtt_estimate_is_excluded_from_eligible_distribution(self):
        active = self.measured(100)
        idle = self.measured(101, tcp={'rtt_ms': 500, 'last_ack_ms': 60000},
                             delta={'bytes_sent': 0, 'bytes_received': 0, 'bytes_acked': 0})
        g = d.summarize([active] + [dict(idle, wall=102 + i) for i in range(100)])[0]
        self.assertEqual(g['rtt_p95_ms'], 10)
        self.assertEqual(g['rtt_eligible'], 1)
        self.assertEqual(g['activity']['idle'], 100)
        self.assertIsNone(d.summarize([idle])[0]['rtt_p95_ms'])

    def test_legacy_rtt_has_explicit_freshness_limit(self):
        row = self.measured(100, schema=1)
        del row['delta']['bytes_acked']
        del row['tcp']['last_ack_ms']
        g = d.summarize([row])[0]
        self.assertIsNone(g['rtt_p95_ms'])
        self.assertEqual(g['legacy_rtt_p95_ms'], 10)

    def test_rtt_spike_requires_baseline_and_ack_progress(self):
        rows = [self.measured(i) for i in range(25)]
        rows.append(self.measured(25, tcp={'rtt_ms': 120, 'rttvar_ms': 40}))
        flags = d.summarize(rows)[0]['incidents']
        self.assertIn('rtt_inflation', {f['kind'] for f in flags})
        self.assertIn('rtt_variation', {f['kind'] for f in flags})
        self.assertNotIn('rtt_inflation', {f['kind'] for f in d.summarize(rows[-1:])[0]['incidents']})

    def test_ack_stall_requires_outstanding_data_and_known_progress_counter(self):
        rows = [self.measured(i, tcp={'send_queue': 1000, 'notsent_bytes': 500},
                              delta={'bytes_acked': 0, 'bytes_sent': 0, 'bytes_received': 0}) for i in range(4)]
        flags = d.summarize(rows)[0]['incidents']
        self.assertIn('ack_stall', {f['kind'] for f in flags})
        for row in rows:
            row['delta']['bytes_acked'] = None
        self.assertNotIn('ack_stall', {f['kind'] for f in d.summarize(rows)[0]['incidents']})

    def test_path_change_resets_rtt_baseline(self):
        analysis = d.Analysis()
        for i in range(25):
            analysis.feed(self.measured(i))
        base = self.measured(25)
        for i, mode in enumerate(('direct', 'peer-relay')):
            analysis.feed(dict(base, wall=25 + i, event='network', tailscale={'mode': mode}))
        analysis.feed(self.measured(27, tcp={'rtt_ms': 120}))
        flags = {f['kind'] for f in analysis.summaries()[0]['incidents']}
        self.assertIn('path_change', flags)
        self.assertNotIn('rtt_inflation', flags)

    def test_compact_rotation_preserves_identity_and_prior_context(self):
        store = self.store()
        row = self.measured(time.time() - 10)
        identity = {k: row[k] for k in ('schema', 'attempt', 'role', 'origin', 'target', 'session', 'host', 'boot')}
        codec = dict(identity, **d.now(), event='codec', available=True, contexts=[])
        store.context = dict(identity=identity, state={'codec': codec})
        store.append(row)
        store.rotate()
        store.append(dict(row, wall=row['wall'] + 2))
        store.rotate()
        loaded = list(store.records(row['wall'] + 1, row['wall'] + 3))
        sample = next(r for r in loaded if r['event'] == 'sample')
        self.assertEqual(sample['attempt'], row['attempt'])
        self.assertEqual(sample['tcp'], row['tcp'])
        self.assertTrue(any(r.get('carried') and r['event'] == 'codec' for r in loaded))
        for path in store.path.glob('*.jsonl'):
            raw = [json.loads(s) for s in path.read_text().splitlines()]
            self.assertEqual(raw[0]['event'], 'context')
            self.assertNotIn('attempt', raw[-1])

    def test_minute_history_does_not_double_count_raw_samples(self):
        wall = time.time() - 120
        row = self.measured(wall)
        raw = self.store()
        raw.append(row)
        minute = d.Store(base=self.base, stream='minute')
        self.addCleanup(minute.close)
        identity = {k: row[k] for k in ('schema', 'attempt', 'role', 'origin', 'target', 'session', 'host', 'boot')}
        minute.context = dict(identity=identity, state={})
        analysis = d.Analysis()
        analysis.feed(row)
        minute.append(dict(row, event='minute', wall=wall + 59, aggregate=analysis.drain()[0]))
        raw.append(self.measured(wall + 70))
        result = d.query(self.query_args(since_wall=wall - 10800, until_wall=time.time()))
        self.assertEqual(result['summaries'][0]['samples'], 2)
        self.assertIn('minute', result['resolution'])
        self.assertEqual(result['summaries'][0]['tx_bytes'], 200)

    def test_minute_context_before_window_is_carried_forward(self):
        wall = time.time() - 120
        row = self.measured(wall)
        minute = d.Store(base=self.base, stream='minute')
        self.addCleanup(minute.close)
        identity = {k: row[k] for k in ('schema', 'attempt', 'role', 'origin', 'target', 'session', 'host', 'boot')}
        minute.context = dict(identity=identity, state={})
        path = dict(row, event='network', tailscale={'mode': 'direct'})
        analysis = d.Analysis()
        analysis.feed(row)
        minute.append(dict(row, event='minute', aggregate=analysis.drain()[0], state={'network': path}))
        later = self.measured(wall + 70)
        analysis.feed(later)
        minute.append(dict(later, event='minute', aggregate=analysis.drain()[0]))
        rows = list(minute.records(wall + 30, time.time(), stream='minute'))
        self.assertTrue(any(r.get('carried') and r['event'] == 'network' for r in rows))

    def test_normal_query_streams_beyond_export_read_limit(self):
        store = self.store()
        store.append(self.measured(time.time()))
        with patch.object(d, 'READ_LIMIT', 10):
            result = d.query(self.query_args())
            self.assertEqual(result['summaries'][0]['samples'], 1)
            with self.assertRaisesRegex(ValueError, 'shorter time window'):
                d.query(self.query_args(include_records=True))

    def test_rollup_and_raw_histograms_have_same_distribution(self):
        rows = [self.measured(i, tcp={'rtt_ms': i + .2}) for i in range(60)]
        rows[1]['processes'] = [dict(kind='waypipe', cpu_percent=12, rss_bytes=4096)]
        rows.append(dict(rows[-1], event='host', pressure={'cpu': {'avg10': 3}}))
        expected = d.summarize(rows)[0]
        analysis = d.Analysis()
        for row in rows:
            analysis.feed(row)
        minute = dict(rows[-1], event='minute', aggregate=analysis.drain()[0])
        actual = d.summarize([minute])[0]
        for key in ('samples', 'rtt_p95_ms', 'rttvar_p95_ms', 'tx_bytes', 'rx_bytes', 'retrans', 'timeline'):
            self.assertEqual(actual[key], expected[key])

    def test_wifi_persists_allowlist_without_ssid_or_mac(self):
        # Test a real-shaped response through the allowlist without touching Wi-Fi.
        fake = self.base / 'wlan-fixture'
        (fake / 'wireless').mkdir(parents=True)
        original = d.Path.iterdir
        def listing(path):
            return iter([fake]) if str(path) == '/sys/class/net' else original(path)
        raw = 'Connected to aa:bb:cc:dd:ee:ff\n SSID: PRIVATE\n signal: -49 dBm\n tx bitrate: 72.2 MBit/s\n tx retries: 12\n tx failed: 1\n'
        with patch.object(d.Path, 'iterdir', listing), patch.object(d, 'bounded', return_value=raw):
            result = d.wifi_sample()
        encoded = json.dumps(result)
        self.assertNotIn('PRIVATE', encoded)
        self.assertNotIn('aa:bb:cc:dd:ee:ff', encoded)
        self.assertEqual(result['interfaces']['wlan-fixture']['tx_retries'], 12)

    def test_incident_window_requires_timezone(self):
        args = types.SimpleNamespace(at='2026-10-31T14:00:00-07:00', window='5m', since='20m')
        with patch.object(d.time, 'time', return_value=1793484000):
            left, right = d.incident_window(args)
        self.assertLessEqual(right - left, 300)
        args.at = '2026-10-31T14:00:00'
        with self.assertRaisesRegex(ValueError, 'timezone'):
            d.incident_window(args)
        args.at = '2099-10-31T14:00:00-07:00'
        with self.assertRaisesRegex(ValueError, 'future'):
            d.incident_window(args)

    def test_watch_reads_local_only_and_is_bounded(self):
        args = self.query_args()
        args.watch, args.export, args.probe, args.at, args.timeline = '1s', None, None, None, False
        with patch.object(d, 'query', return_value={'status': 'available'}), \
                patch.object(d, 'bounded') as tools, patch.object(d.time, 'sleep'), \
                patch.object(d.time, 'monotonic', side_effect=[1, 3]), contextlib.redirect_stdout(io.StringIO()):
            d.watch_cli(args)
        tools.assert_not_called()

    def test_active_probe_requires_flag_and_records_distinct_icmp_results(self):
        store = self.store()
        row = self.measured(time.time(), tcp={'peer': ['100.1.1.1', 22]})
        store.append(row)
        args = self.query_args()
        args.probe = '2s'
        raw = '2 packets transmitted, 1 received, 50% packet loss\nrtt min/avg/max/mdev = 10/10/10/0 ms\n'
        with patch.object(d, 'bounded', return_value=raw) as tool, contextlib.redirect_stdout(io.StringIO()):
            d.probe_cli(args)
        self.assertEqual(tool.call_args.args[0][0], 'ping')
        probe = next(r for r in store.records(0, time.time()) if r['event'] == 'probe')
        self.assertEqual((probe['sent'], probe['received']), (2, 1))
        self.assertEqual(probe['method'], 'ICMP active probe')

    def test_collector_generates_readable_compact_samples_and_minute_tail(self):
        helper = self.base / 'helper.py'
        helper.write_text(SOURCE)
        owner = subprocess.Popen(['sleep', '30'])
        collector = subprocess.Popen(['python3', str(helper), 'collect', 'snap', '--owner', str(owner.pid),
                                      '--start', d.process(owner.pid)['start'], '--attempt', str(uuid.uuid4()),
                                      '--origin', socket.gethostname(), '--days', '30', '--cap', '1073741824'])
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                chunks = list((self.base / 'remote-chrome/diagnostics').glob('*.jsonl'))
                if chunks and any('"event":"sample"' in p.read_text() for p in chunks):
                    break
                time.sleep(.02)
            owner.terminate()
            owner.wait()
            self.assertEqual(collector.wait(timeout=5), 0)
            store = self.store()
            rows = list(store.records(0, time.time(), stream='all'))
            self.assertTrue(any(r['event'] == 'sample' for r in rows))
            self.assertTrue(any(r['event'] == 'minute' for r in rows))
            self.assertFalse(any(r['event'] == 'collector_failure' for r in rows))
        finally:
            if owner.poll() is None:
                owner.terminate()
                owner.wait()
            if collector.poll() is None:
                collector.terminate()
                collector.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
