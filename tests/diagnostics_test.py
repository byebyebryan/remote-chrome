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


if __name__ == "__main__":
    unittest.main(verbosity=2)
