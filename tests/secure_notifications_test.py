#!/usr/bin/env python3
"""Exercise the generated secure bootstrap against private source/destination buses."""

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
NAME = "org.freedesktop.Notifications"


def wait_for(predicate, label, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(label)


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


FAKE_CHROME = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
record = {"argv": sys.argv[1:], "bus": os.environ["DBUS_SESSION_BUS_ADDRESS"]}
if os.environ.get("CHROME_MODE") == "notify":
    import dbus
    bus = dbus.bus.BusConnection(record["bus"])
    service = dbus.Interface(bus.get_object("org.freedesktop.Notifications",
        "/org/freedesktop/Notifications"), "org.freedesktop.Notifications")
    record["server"] = list(map(str, service.GetServerInformation()))
    record["caps"] = list(map(str, service.GetCapabilities()))
    hints = dbus.Dictionary({"urgency": dbus.Byte(2, variant_level=1)}, signature="sv")
    actions = dbus.Array(["default", "Open"], signature="s")
    nid = service.Notify("Google Chrome", dbus.UInt32(0), "", "<b>bootstrap notification</b>",
                         "<b>strong</b> &amp; safe", actions, hints, dbus.Int32(0))
    record["id"] = int(nid)
    replacement = service.Notify("Google Chrome", dbus.UInt32(nid), "", "replacement",
        "<i>updated</i>", actions, hints, dbus.Int32(0))
    record["replacement"] = int(replacement)
    service.CloseNotification(replacement)
if os.environ.get("CHROME_CLEANUP_DELAY"):
    import signal, time
    def delayed_term(*args):
        Path(os.environ["CHROME_TRACE"] + ".term").write_text("TERM")
        time.sleep(0.3)
        sys.exit(0)
    signal.signal(signal.SIGTERM, delayed_term)
Path(os.environ["CHROME_TRACE"]).write_text(json.dumps(record))
if os.environ.get("CHROME_MODE") == "hold":
    import time
    while True:
        time.sleep(0.1)
sys.exit(int(os.environ.get("CHROME_STATUS", "0")))
'''


class SecureNotifications(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="remote-chrome-notify-test-")
        self.root = Path(self.tmp.name)
        self.processes = []
        self.handles = []
        self.source_runtime = self.root / "source-runtime"
        self.destination_runtime = self.root / "destination-runtime"
        self.source_runtime.mkdir(mode=0o700)
        self.destination_runtime.mkdir(mode=0o700)
        self.source = self.private_bus("source")
        self.destination = self.private_bus("destination")
        self.fixture("wallet", self.source, "--secrets")
        self.fakebin = self.root / "bin"
        self.fakebin.mkdir()
        scripts = {
            "fake-chrome": FAKE_CHROME,
            "secret-tool": '#!/usr/bin/env bash\nexit "${SECRET_TOOL_STATUS:-0}"\n',
            "ksecretd": '#!/usr/bin/env bash\necho unexpected-wallet-start >&2\nexit 97\n',
            "ssh": '#!/usr/bin/env bash\nexit 1\n',
            # Existing-daemon mode still uses notify-send; capture it harmlessly.
            "notify-send": '#!/usr/bin/env python3\nimport json, os, sys\n'
                'with open(os.environ["LEGACY_CAPTURE"], "a") as f:\n'
                '    f.write(json.dumps(sys.argv[1:])+"\\n")\n',
        }
        for name, content in scripts.items():
            path = self.fakebin / name
            path.write_text(content)
            path.chmod(0o700)
        self.script = self.root / "bootstrap.sh"
        result = subprocess.run(["bash", "-c", 'source "$1"; chrome_secure_bootstrap_script',
                                 "fixture", str(REPO / "bin/remote-chrome")],
                                capture_output=True, text=True, check=True, timeout=5)
        self.script.write_text(result.stdout)
        self.trace = self.root / "chrome.json"

    def spawn(self, label, argv, env=None):
        log = (self.root / (label + ".log")).open("w")
        self.handles.append(log)
        process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
        self.processes.append(process)
        return process

    def private_bus(self, label):
        address = "unix:path=" + str(self.root / (label + ".sock"))
        config = self.root / (label + ".conf")
        config.write_text('<busconfig><type>session</type><listen>' + address + '</listen>'
            '<auth>EXTERNAL</auth><policy context="default"><allow own="*"/>'
            '<allow send_destination="*"/><allow receive_sender="*"/>'
            '<allow eavesdrop="true"/></policy></busconfig>')
        process = self.spawn(label, ["dbus-daemon", "--nofork", "--config-file=" + str(config)])
        wait_for(lambda: (self.root / (label + ".sock")).is_socket(), label + " bus not ready")
        self.assertIsNone(process.poll())
        return address

    def fixture(self, label, address, *flags):
        ready = self.root / (label + ".ready")
        process = self.spawn(label, ["python3", str(REPO / "tests/notification_fixture.py"),
            "--address", address, "--events", str(self.root / (label + ".jsonl")),
            "--ready", str(ready), *flags])
        wait_for(ready.exists, label + " fixture not ready")
        self.assertIsNone(process.poll())
        return process

    def listener(self, caps="body,actions,body-markup"):
        self.fixture("desktop", self.destination, "--notifications", "--caps", caps)
        env = os.environ | {
            "DBUS_SESSION_BUS_ADDRESS": self.destination,
            "XDG_RUNTIME_DIR": str(self.destination_runtime),
            "PATH": str(self.fakebin) + ":" + os.environ["PATH"],
            "LEGACY_CAPTURE": str(self.root / "legacy.jsonl"),
        }
        listener = self.spawn("listener", ["bash", str(REPO / "bin/remote-chrome"),
            "_notify-listener", str(self.root / "relay.sock"), str(self.root / "listener.events")], env)
        wait_for(lambda: (self.root / "relay.sock").is_socket(), "listener not ready")
        self.assertIsNone(listener.poll())
        return listener

    def run_bootstrap(self, *, notifications=True, mode="idle", extra_env=None):
        env = os.environ | {
            "DBUS_SESSION_BUS_ADDRESS": self.source,
            "XDG_RUNTIME_DIR": str(self.source_runtime),
            "PATH": str(self.fakebin) + ":" + os.environ["PATH"],
            "CHROME_TRACE": str(self.trace), "CHROME_MODE": mode,
            "REMOTE_CHROME_ORIGIN_NAME": "test-display", "REMOTE_CHROME_ORIGIN_TARGET": "test-source",
            "REMOTE_CHROME_ORIGIN_SESSION": "remote-chrome-notification-test",
            "REMOTE_CHROME_ORIGIN_USER": "test-user",
            "LEGACY_CAPTURE": str(self.root / "legacy.jsonl"),
        }
        env.update(extra_env or {})
        return subprocess.run(["bash", str(self.script), "fake-chrome", "chrome",
            "chrome_libsecret_os_crypt_password_v2",
            str(self.root / "relay.sock") if notifications else "", "chrome", "--new-window"],
            env=env, capture_output=True, text=True, timeout=12)

    def owned(self, address, name=NAME):
        result = subprocess.run(["busctl", "--address=" + address, "--json=short", "call",
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", "s", name], capture_output=True, text=True, check=True, timeout=2)
        return json.loads(result.stdout)["data"][0]

    def clean_source(self):
        self.assertFalse(self.owned(self.source))
        self.assertTrue(self.owned(self.source, "org.freedesktop.secrets"))
        self.assertEqual(list(self.source_runtime.glob("remote-chrome-*")), [])

    def test_generated_bootstrap_native_delivery_replacement_close_and_cleanup(self):
        self.listener()
        result = self.run_bootstrap(mode="notify")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.trace.read_text())
        self.assertIn("--password-store=gnome-libsecret", data["argv"])
        self.assertIn("remote-chrome-dbus-proxy-", data["bus"])
        self.assertEqual(data["id"], data["replacement"])
        self.assertTrue({"body", "actions"}.issubset(data["caps"]))
        rows = wait_for(lambda: events(self.root / "desktop.jsonl"), "desktop did not receive notification")
        notifications = [row for row in rows if row["event"] == "notify"]
        self.assertEqual(len(notifications), 2, rows)
        self.assertEqual(notifications[0]["summary"], "bootstrap notification")
        self.assertEqual(notifications[0]["body"], "<b></b><b>strong</b> &amp; safe")
        self.assertEqual(notifications[0]["urgency"], 2)
        self.assertEqual(notifications[0]["actions"], ["default", "Open"])
        self.assertEqual(notifications[1]["replaces"], notifications[0]["id"])
        self.assertNotEqual(data["id"], notifications[0]["id"])
        wait_for(lambda: any(row["event"] == "closed" and row["reason"] == 3
            for row in events(self.root / "desktop.jsonl")), "source close did not close destination")
        self.clean_source()

    def test_lookup_failure_rolls_back_notification_resources(self):
        self.listener()
        result = self.run_bootstrap(extra_env={"SECRET_TOOL_STATUS": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Safe Storage lookup failed", result.stderr)
        self.assertFalse(self.trace.exists())
        self.clean_source()

    def test_chrome_failure_status_survives_owned_cleanup(self):
        self.listener()
        result = self.run_bootstrap(extra_env={"CHROME_STATUS": "7"})
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertTrue(self.trace.exists())
        self.clean_source()

    def test_disabled_notifications_do_not_claim_the_service(self):
        result = self.run_bootstrap(notifications=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.clean_source()

    def test_destination_without_actions_is_not_advertised_as_capable(self):
        self.listener(caps="body,body-markup")
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.trace.exists())
        self.assertRegex(result.stderr.lower(), "(notification|notify).*(unavailable|failed|actions|not forwarded)")
        self.clean_source()

    def test_existing_daemon_is_not_owned_or_stopped(self):
        daemon = self.fixture("source-desktop", self.source, "--notifications")
        self.listener()
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(daemon.poll())
        self.assertTrue(self.owned(self.source))
        self.assertEqual(list(self.source_runtime.glob("remote-chrome-*")), [])

    def test_missing_bindings_warn_without_weakening_secure_launch(self):
        self.listener()
        missing = self.root / "missing-bindings"
        missing.mkdir()
        (missing / "dbus.py").write_text('raise ImportError("fixture missing dbus bindings")\n')
        result = self.run_bootstrap(extra_env={"PYTHONPATH": str(missing)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--password-store=gnome-libsecret", json.loads(self.trace.read_text())["argv"])
        self.assertRegex(result.stderr.lower(), "(notification|notify).*(unavailable|failed|not forwarded)")
        self.clean_source()

    def test_missing_socket_warns_and_releases_partial_helper(self):
        started = time.monotonic()
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(self.trace.exists())
        self.assertRegex(result.stderr.lower(), "(notification|notify).*(unavailable|failed|not forwarded)")
        self.clean_source()

    def test_preexisting_managed_fallback_is_busy_and_preserved(self):
        daemon = self.fixture("busy-helper", self.source, "--notifications",
                              "--server", "remote-chrome-notification-endpoint", "--vendor", "remote-chrome")
        self.listener()
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.trace.exists())
        self.assertIsNone(daemon.poll())
        self.assertTrue(self.owned(self.source))
        self.assertEqual(list(self.source_runtime.glob("remote-chrome-*")), [])

    def holding_bootstrap(self, *, tracked=False, delayed_cleanup=False):
        env = os.environ | {
            "DBUS_SESSION_BUS_ADDRESS": self.source,
            "XDG_RUNTIME_DIR": str(self.source_runtime),
            "PATH": str(self.fakebin) + ":" + os.environ["PATH"],
            "CHROME_TRACE": str(self.trace), "CHROME_MODE": "hold",
        }
        if delayed_cleanup:
            env["CHROME_CLEANUP_DELAY"] = "1"
        if tracked:
            env.update(REMOTE_CHROME_ORIGIN_NAME="test-display",
                       REMOTE_CHROME_ORIGIN_TARGET="test-source",
                       REMOTE_CHROME_ORIGIN_SESSION="remote-chrome-notification-test",
                       REMOTE_CHROME_ORIGIN_USER="test-user")
        bootstrap = self.spawn("holding-bootstrap", ["bash", str(self.script), "fake-chrome",
            "chrome", "chrome_libsecret_os_crypt_password_v2", str(self.root / "relay.sock"),
            "chrome"], env)
        wait_for(self.trace.exists, "holding Chrome did not start")
        self.assertTrue(self.owned(self.source))
        return bootstrap, env

    def test_term_runs_exact_owned_helper_and_chrome_cleanup(self):
        self.listener()
        bootstrap, _ = self.holding_bootstrap()
        bootstrap.send_signal(signal.SIGTERM)
        self.assertEqual(bootstrap.wait(timeout=8), 143)
        self.clean_source()

    def test_hup_runs_owned_cleanup(self):
        self.listener()
        bootstrap, _ = self.holding_bootstrap()
        bootstrap.send_signal(signal.SIGHUP)
        self.assertEqual(bootstrap.wait(timeout=8), 129)
        self.clean_source()

    def test_second_signal_cannot_interrupt_owned_cleanup(self):
        self.listener()
        bootstrap, _ = self.holding_bootstrap(tracked=True, delayed_cleanup=True)
        bootstrap.send_signal(signal.SIGTERM)
        wait_for(lambda: Path(str(self.trace) + ".term").exists(), "cleanup did not reach Chrome TERM")
        bootstrap.send_signal(signal.SIGHUP)
        self.assertEqual(bootstrap.wait(timeout=8), 143)
        self.clean_source()

    def stop_incoming(self, env):
        return subprocess.run(["bash", "-c", 'source "$1"; chrome_stop_incoming',
            "fixture", str(REPO / "bin/remote-chrome")], env=env,
            capture_output=True, text=True, timeout=10)

    def test_incoming_stop_releases_helper_when_source_is_unreachable(self):
        self.listener()
        bootstrap, env = self.holding_bootstrap(tracked=True)
        self.assertEqual(len(list(self.source_runtime.glob("remote-chrome-incoming-*.state"))), 1)
        result = self.stop_incoming(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Display host cleanup unavailable", result.stderr)
        self.assertEqual(bootstrap.wait(timeout=8), 143)
        self.clean_source()

    def test_incoming_stop_preserves_changed_process_identity(self):
        self.listener()
        bootstrap, env = self.holding_bootstrap(tracked=True)
        state, = self.source_runtime.glob("remote-chrome-incoming-*.state")
        lines = state.read_text().splitlines()
        state.write_text("\n".join("starttime\t0" if line.startswith("starttime\t")
                                  else line for line in lines) + "\n")
        result = self.stop_incoming(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(bootstrap.poll())
        self.assertTrue(self.owned(self.source))
        self.assertFalse(state.exists())
        bootstrap.send_signal(signal.SIGTERM)
        self.assertEqual(bootstrap.wait(timeout=8), 143)
        self.clean_source()

    def source_status(self, env):
        return subprocess.run(["bash", str(REPO / "bin/remote-chrome"), "_notify-status",
            "remote-chrome-notification-test"], env=env,
            capture_output=True, text=True, timeout=3)

    def test_status_checks_actual_helper_ownership_without_sending_notifications(self):
        self.listener()
        bootstrap, env = self.holding_bootstrap(tracked=True)
        result = self.source_status(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("backend=helper", result.stdout)
        self.assertIn("transport=ready", result.stdout)
        self.assertIn("ownership=verified", result.stdout)
        self.assertEqual(events(self.root / "desktop.jsonl"), [])
        bootstrap.send_signal(signal.SIGTERM)
        self.assertEqual(bootstrap.wait(timeout=8), 143)
        self.clean_source()

    def test_status_reports_lost_transport_while_browser_stays_alive(self):
        listener = self.listener()
        bootstrap, env = self.holding_bootstrap(tracked=True)
        listener.send_signal(signal.SIGTERM)
        listener.wait(timeout=4)
        wait_for(lambda: not self.owned(self.source), "disconnected helper retained its bus name")
        result = self.source_status(env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("transport=degraded", result.stdout)
        self.assertIsNone(bootstrap.poll())
        bootstrap.send_signal(signal.SIGTERM)
        self.assertEqual(bootstrap.wait(timeout=8), 143)
        self.clean_source()

    def tearDown(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
        for handle in self.handles:
            handle.close()
        self.tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
