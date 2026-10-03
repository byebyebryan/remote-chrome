#!/usr/bin/env python3
"""Exercise the private helper-backed notification protocol without a desktop bus."""

import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

REPO = Path(__file__).resolve().parents[1]
NAME = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
DBusGMainLoop(set_as_default=True)


def wait_for(predicate, label, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump_context()
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    pump_context()
    raise AssertionError(label)


def pump_context():
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def pump_for(duration):
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        pump_context()
        time.sleep(0.005)


def rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def helper_source():
    source = (REPO / "bin/remote-chrome").read_text()
    match = re.search(
        r"cat >\"\$notify_forwarder_script\" <<'REMOTE_CHROME_NOTIFY_ENDPOINT'\n(.*?)\nREMOTE_CHROME_NOTIFY_ENDPOINT",
        source,
        re.S,
    )
    if match is None:
        raise AssertionError("embedded REMOTE_CHROME_NOTIFY_ENDPOINT helper is missing")
    compile(match.group(1), "REMOTE_CHROME_NOTIFY_ENDPOINT", "exec")
    return match.group(1)


DELAYED_DESTINATION = r'''#!/usr/bin/env python3
import json, signal, sys
from pathlib import Path
import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

NAME = "org.freedesktop.Notifications"
CONTROL = "org.remotechrome.Test"
DBusGMainLoop(set_as_default=True)
address, events_path, ready_path = sys.argv[1:]
bus = dbus.bus.BusConnection(address)
if int(bus.request_name(NAME, 4)) != 1:
    raise RuntimeError("delayed fixture could not own destination name")
next_id = 700
active = set()
pending_replies = {}

def event(kind, **fields):
    with open(events_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": kind, **fields}) + "\n")

class Endpoint(dbus.service.Object):
    def __init__(self):
        super().__init__(bus, "/org/freedesktop/Notifications")

    @dbus.service.method(NAME, in_signature="", out_signature="as")
    def GetCapabilities(self):
        return dbus.Array(["body", "actions", "body-markup"], signature="s")

    @dbus.service.method(NAME, in_signature="", out_signature="ssss")
    def GetServerInformation(self):
        return ("delayed-test", "remote-chrome-tests", "1", "1.2")

    @dbus.service.method(NAME, in_signature="susssasa{sv}i", out_signature="u",
                         async_callbacks=("reply_cb", "error_cb"))
    def Notify(self, app, replaces, icon, summary, body, actions, hints, expires,
               reply_cb, error_cb):
        global next_id
        nid = int(replaces) if int(replaces) in active else next_id
        if nid == next_id:
            next_id += 1
        active.add(nid)
        event("notify-received", id=nid, replaces=int(replaces), summary=str(summary))
        pending_replies[nid] = reply_cb

    @dbus.service.method(NAME, in_signature="u", out_signature="")
    def CloseNotification(self, nid):
        nid = int(nid)
        if nid in active:
            active.remove(nid)
            event("closed", id=nid, reason=3)
            self.NotificationClosed(dbus.UInt32(nid), dbus.UInt32(3))

    @dbus.service.signal(NAME, signature="us")
    def ActionInvoked(self, nid, key):
        pass

    @dbus.service.signal(NAME, signature="uu")
    def NotificationClosed(self, nid, reason):
        pass

    @dbus.service.method(CONTROL, in_signature="us", out_signature="b")
    def Action(self, nid, key):
        return False

    @dbus.service.method(CONTROL, in_signature="us", out_signature="")
    def RawAction(self, nid, key):
        event("raw-action", id=int(nid), key=str(key))
        self.ActionInvoked(dbus.UInt32(nid), dbus.String(key))

    @dbus.service.method(CONTROL, in_signature="u", out_signature="b")
    def ReleaseReply(self, nid):
        reply_cb = pending_replies.pop(int(nid), None)
        if reply_cb is None:
            return False
        reply_cb(dbus.UInt32(nid))
        event("notify-replied", id=int(nid))
        return True

    @dbus.service.method(CONTROL, in_signature="u", out_signature="")
    def Dismiss(self, nid):
        nid = int(nid)
        if nid in active:
            active.remove(nid)
            event("closed", id=nid, reason=2)
            self.NotificationClosed(dbus.UInt32(nid), dbus.UInt32(2))

    @dbus.service.method(CONTROL, in_signature="u", out_signature="")
    def Expire(self, nid):
        nid = int(nid)
        if nid in active:
            active.remove(nid)
            event("closed", id=nid, reason=1)
            self.NotificationClosed(dbus.UInt32(nid), dbus.UInt32(1))

endpoint = Endpoint()
loop = GLib.MainLoop()
signal.signal(signal.SIGTERM, lambda *_: loop.quit())
signal.signal(signal.SIGINT, lambda *_: loop.quit())
Path(ready_path).write_text(bus.get_unique_name())
try:
    loop.run()
finally:
    endpoint.remove_from_connection()
    bus.release_name(NAME)
    bus.close()
'''


class HeadlessNotificationProtocol(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="remote-chrome-notify-test-")
        self.root = Path(self.temp.name)
        self.processes = []
        self.logs = []
        self.handles = []
        self.raw_connections = []
        self.source_address = self.start_bus("source")
        self.destination_address = self.start_bus("destination")
        self.destination_runtime = self.root / "destination-runtime"
        self.destination_runtime.mkdir(mode=0o700)
        self.events_path = self.root / "destination.jsonl"
        self.fixture_ready = self.root / "destination.ready"
        self.destination_fixture = self.start_destination_fixture(
            "destination-fixture", self.destination_address, self.events_path, self.fixture_ready,
        )
        wait_for(self.fixture_ready.exists, "destination fixture did not acquire its name")
        self.relay = self.root / "relay.sock"
        self.listener_log = self.root / "listener.events"
        self.listener = self.start_listener(
            "listener", self.destination_address, self.destination_runtime, self.relay, self.listener_log,
        )
        wait_for(self.relay.is_socket, "production listener did not create its socket")
        self.destination_bus = dbus.bus.BusConnection(self.destination_address)
        self.control = dbus.Interface(
            self.destination_bus.get_object(NAME, PATH),
            "org.remotechrome.Test",
        )
        self.source_runtime = self.root / "source-runtime"
        self.source_runtime.mkdir(mode=0o700)
        self.status_path = self.source_runtime / "endpoint.state"
        self.status_path.touch(mode=0o600)
        self.endpoint_script = self.root / "endpoint.py"
        self.endpoint_script.write_text(helper_source())
        self.endpoint_script.chmod(0o700)
        self.source_bus = None
        self.source = None
        self.endpoint = None
        self.signal_events = []
        self.signal_matches = []

    def start_destination_fixture(self, label, address, events_path, ready_path):
        return self.start_process(label, [
            "python3", str(REPO / "tests/notification_fixture.py"),
            "--address", address, "--events", str(events_path),
            "--ready", str(ready_path), "--notifications",
        ])

    def start_delayed_destination(self):
        self.destination_fixture.send_signal(signal.SIGTERM)
        self.destination_fixture.wait(timeout=3)
        delayed_script = self.root / "delayed-destination.py"
        delayed_script.write_text(DELAYED_DESTINATION)
        delayed_script.chmod(0o700)
        delayed_ready = self.root / "delayed.ready"
        self.start_process("delayed-destination", [
            "python3", str(delayed_script), self.destination_address,
            str(self.events_path), str(delayed_ready),
        ])
        wait_for(delayed_ready.exists, "delayed destination did not acquire its name")
        self.control = dbus.Interface(
            self.destination_bus.get_object(NAME, PATH), "org.remotechrome.Test"
        )

    def start_listener(self, label, address, runtime, relay, log_path):
        listener_env = os.environ | {
            "DBUS_SESSION_BUS_ADDRESS": address,
            "XDG_RUNTIME_DIR": str(runtime),
        }
        return self.start_process(label, [
            "bash", str(REPO / "bin/remote-chrome"),
            "_notify-listener", str(relay), str(log_path),
        ], listener_env)

    def start_process(self, label, argv, env=None):
        log_path = self.root / (label + ".log")
        handle = log_path.open("w")
        self.logs.append(log_path)
        self.handles.append(handle)
        process = subprocess.Popen(
            argv, env=env or os.environ, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=handle, start_new_session=True,
        )
        self.processes.append(process)
        return process

    def start_bus(self, label):
        address = "unix:path=" + str(self.root / (label + ".sock"))
        config = self.root / (label + ".conf")
        config.write_text(
            '<busconfig><type>session</type><listen>' + address + '</listen>'
            '<auth>EXTERNAL</auth><policy context="default"><allow own="*"/>'
            '<allow send_destination="*"/><allow receive_sender="*"/>'
            '<allow eavesdrop="true"/></policy></busconfig>'
        )
        process = self.start_process(
            label + "-bus", ["dbus-daemon", "--nofork", "--config-file=" + str(config)]
        )
        wait_for(lambda: Path(address.removeprefix("unix:path=")).is_socket(), label + " bus did not start")
        self.assertIsNone(process.poll())
        return address

    def start_endpoint(self):
        endpoint_env = os.environ | {
            "DBUS_SESSION_BUS_ADDRESS": self.source_address,
            "XDG_RUNTIME_DIR": str(self.source_runtime),
            "REMOTE_CHROME_ORIGIN_SESSION": "headless-protocol-test",
        }
        self.endpoint = self.start_process("endpoint", [
            "python3", str(self.endpoint_script), str(self.relay), "chrome", str(self.status_path),
        ], endpoint_env)
        wait_for(lambda: self.status_path.read_text().startswith("ready"), "source endpoint did not become ready")
        self.source_bus = dbus.bus.BusConnection(self.source_address)
        self.source = dbus.Interface(
            self.source_bus.get_object(NAME, PATH), NAME
        )
        self.signal_matches.append(self.source_bus.add_signal_receiver(
            self.record_action, signal_name="ActionInvoked", dbus_interface=NAME, path=PATH,
        ))
        self.signal_matches.append(self.source_bus.add_signal_receiver(
            self.record_closed, signal_name="NotificationClosed", dbus_interface=NAME, path=PATH,
        ))
        return self.source

    def record_action(self, source_id, key):
        self.signal_events.append(("action", int(source_id), str(key)))

    def record_closed(self, source_id, reason):
        self.signal_events.append(("closed", int(source_id), int(reason)))

    def wait_signal(self, expected, label):
        return wait_for(
            lambda: next((row for row in self.signal_events if row == expected), None),
            label,
        )

    def notify(self, summary, *, replaces=0, actions=("default", "Open"), expires=0):
        hints = dbus.Dictionary({"urgency": dbus.Byte(2, variant_level=1)}, signature="sv")
        return int(self.source.Notify(
            "Google Chrome", dbus.UInt32(replaces), "", summary, "<b>body</b>",
            dbus.Array(list(actions), signature="s"), hints, dbus.Int32(expires),
        ))

    def destination_notifications(self):
        return [row for row in rows(self.events_path) if row.get("event") == "notify"]

    def test_action_mapping_replacement_close_dismiss_expiry_and_disconnect_cleanup(self):
        source = self.start_endpoint()
        self.assertTrue({"body", "actions"}.issubset(set(map(str, source.GetCapabilities()))))

        source_id = self.notify("first notification")
        wait_for(lambda: len(self.destination_notifications()) >= 1, "first destination Notify was not delivered")
        first = self.destination_notifications()[0]
        self.assertEqual(first["summary"], "first notification")
        self.assertEqual(first["body"], "<b></b><b>body</b>")
        self.assertEqual(first["actions"], ["default", "Open"])

        replacement_id = self.notify("replaced notification", replaces=source_id)
        self.assertEqual(replacement_id, source_id)
        wait_for(lambda: len(self.destination_notifications()) >= 2, "replacement Notify was not delivered")
        second = self.destination_notifications()[1]
        self.assertEqual(second["replaces"], first["id"])
        self.assertEqual(second["id"], first["id"])

        self.assertTrue(self.control.Action(dbus.UInt32(first["id"]), "default"))
        self.wait_signal(("action", source_id, "default"), "destination action did not return to source")
        source.CloseNotification(dbus.UInt32(source_id))
        wait_for(
            lambda: any(row.get("event") == "closed" and row.get("id") == first["id"] and row.get("reason") == 3
                        for row in rows(self.events_path)),
            "source CloseNotification was not delivered",
        )
        self.wait_signal(("closed", source_id, 3), "source close callback was not returned")
        with self.assertRaises(dbus.DBusException):
            source.CloseNotification(dbus.UInt32(source_id))

        dismissed_source = self.notify("dismiss me")
        wait_for(lambda: len(self.destination_notifications()) >= 3, "dismiss notification was not delivered")
        dismissed = self.destination_notifications()[2]
        self.control.Dismiss(dbus.UInt32(dismissed["id"]))
        self.wait_signal(("closed", dismissed_source, 2), "dismiss callback was not returned")

        expiring_source = self.notify("expire me", expires=500)
        wait_for(lambda: len(self.destination_notifications()) >= 4, "expiring notification was not delivered")
        expiring = self.destination_notifications()[3]
        self.assertEqual(expiring["expires"], 500)
        self.wait_signal(("closed", expiring_source, 1), "expiry callback was not returned")

        disconnect_source = self.notify("close on transport disconnect")
        wait_for(lambda: len(self.destination_notifications()) >= 5, "disconnect notification was not delivered")
        disconnect_local = self.destination_notifications()[4]["id"]
        self.endpoint.send_signal(signal.SIGTERM)
        self.assertEqual(self.endpoint.wait(timeout=4), 0)
        wait_for(
            lambda: any(row.get("event") == "closed" and row.get("id") == disconnect_local
                        and row.get("reason") == 3 for row in rows(self.events_path)),
            "listener did not close its own mapped destination notification after source disconnect",
        )
        self.assertFalse(self.has_source_owner())

    def has_source_owner(self):
        bus = dbus.bus.BusConnection(self.source_address)
        daemon = dbus.Interface(bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus"),
                                "org.freedesktop.DBus")
        try:
            return bool(daemon.NameHasOwner(NAME))
        finally:
            bus.close()

    def recv_frame(self, connection, timeout=2):
        deadline = time.monotonic() + timeout
        buffer = bytearray()
        while time.monotonic() < deadline:
            ready, _, _ = select.select([connection], [], [], deadline - time.monotonic())
            if not ready:
                break
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer.extend(chunk)
            if b"\n" in buffer:
                line, _, _ = buffer.partition(b"\n")
                return json.loads(line)
        raise AssertionError("listener protocol reply timed out")

    def test_listener_rejects_stale_frames_bool_ids_and_unknown_close(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.raw_connections.append(connection)
        connection.connect(str(self.relay))
        session = "malformed-protocol-test"
        generation = "generation-a"
        send = lambda value: connection.sendall(
            (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")
        )
        send({"v": 1, "type": "hello", "session_id": session, "generation": generation})
        ready = self.recv_frame(connection)
        self.assertEqual(ready["type"], "ready")
        self.assertEqual(ready["v"], 1)
        self.assertTrue(ready["destination_owner"].startswith(":"))
        self.assertIn("actions", ready["capabilities"])
        send({"v": 1, "type": "active", "session_id": session, "generation": generation})

        send({"v": 1, "type": "notify", "session_id": session, "generation": generation,
              "source_id": True, "replaces_id": 0, "app_name": "Chrome", "summary": "bad",
              "body": "body", "actions": [], "expires": 0, "desktop_entry": "chrome"})
        invalid_id = self.recv_frame(connection)
        self.assertEqual(invalid_id.get("code"), "invalid-id")

        send({"v": 1, "type": "close", "session_id": session, "generation": generation,
              "source_id": 77, "local_id": 0})
        unknown = self.recv_frame(connection)
        self.assertEqual(unknown.get("code"), "unknown-source-id")

        send({"v": 1, "type": "action", "session_id": session, "generation": "stale-generation",
              "source_id": 1, "local_id": 100, "key": "default"})
        stale = self.recv_frame(connection)
        self.assertEqual(stale.get("code"), "stale-session")
        connection.close()

    def test_disconnect_drains_delayed_destination_notify_reply(self):
        self.start_delayed_destination()

        source = self.start_endpoint()
        self.notify("reply after disconnect")
        requested = wait_for(
            lambda: next((row for row in rows(self.events_path)
                          if row.get("event") == "notify-received"), None),
            "delayed destination did not receive Notify",
        )
        self.assertEqual(requested["summary"], "reply after disconnect")
        self.assertFalse(any(row.get("event") == "notify-replied" for row in rows(self.events_path)))
        self.endpoint.send_signal(signal.SIGTERM)
        self.assertEqual(self.endpoint.wait(timeout=4), 0)
        self.assertFalse(any(row.get("event") == "notify-replied" for row in rows(self.events_path)))
        self.assertTrue(self.control.ReleaseReply(dbus.UInt32(requested["id"])))
        wait_for(
            lambda: any(row.get("event") == "notify-replied" for row in rows(self.events_path)),
            "destination did not release the delayed Notify reply after source disconnect",
        )
        wait_for(
            lambda: any(row.get("event") == "closed" and row.get("id") == requested["id"]
                        and row.get("reason") == 3 for row in rows(self.events_path)),
            "listener did not drain the delayed Notify reply and close its returned local ID",
            timeout=4,
        )
        self.assertFalse(self.has_source_owner())

    def test_unoffered_destination_action_is_not_forwarded(self):
        self.start_delayed_destination()
        self.start_endpoint()
        source_id = self.notify("reject an action that was never offered")
        requested = wait_for(
            lambda: next((row for row in rows(self.events_path)
                          if row.get("event") == "notify-received"), None),
            "delayed destination did not receive Notify",
        )
        self.assertTrue(self.control.ReleaseReply(dbus.UInt32(requested["id"])))
        wait_for(
            lambda: self.listener_log.exists()
            and any(f"\tdelivered\t{source_id}\t{requested['id']}\t" in line
                    for line in self.listener_log.read_text().splitlines()),
            "destination notification was not mapped before injecting the action",
        )
        self.control.RawAction(dbus.UInt32(requested["id"]), "not-offered")
        wait_for(
            lambda: any(row.get("event") == "raw-action" for row in rows(self.events_path)),
            "unknown action signal was not injected by the fixture",
        )
        pump_for(0.1)
        self.assertNotIn(("action", source_id, "not-offered"), self.signal_events)

    def test_queued_replacement_survives_old_destination_expiry(self):
        self.start_delayed_destination()
        source = self.start_endpoint()
        source_id = self.notify("original")
        wait_for(lambda: len(rows(self.events_path)) == 1, "original was not received")
        original_id = rows(self.events_path)[0]["id"]
        self.assertTrue(self.control.ReleaseReply(dbus.UInt32(original_id)))
        wait_for(lambda: f"delivered\t{source_id}\t{original_id}" in self.listener_log.read_text(),
                 "original was not mapped")

        self.notify("hold unrelated delivery")
        held = wait_for(lambda: next((row for row in rows(self.events_path)
                        if row.get("summary") == "hold unrelated delivery"), None), "held request missing")
        self.assertEqual(self.notify("queued replacement", replaces=source_id), source_id)
        self.control.Expire(dbus.UInt32(original_id))
        pump_for(0.1)
        self.assertNotIn(("closed", source_id, 1), self.signal_events)
        self.assertTrue(self.control.ReleaseReply(dbus.UInt32(held["id"])))
        replacement = wait_for(lambda: next((row for row in rows(self.events_path)
                               if row.get("summary") == "queued replacement"), None),
                               "accepted replacement was discarded after the old popup expired")
        self.assertEqual(replacement["replaces"], 0)
        self.assertNotEqual(replacement["id"], original_id)
        self.assertTrue(self.control.ReleaseReply(dbus.UInt32(replacement["id"])))
        wait_for(lambda: f"delivered\t{source_id}\t{replacement['id']}" in self.listener_log.read_text(),
                 "replacement was not mapped")
        self.control.RawAction(dbus.UInt32(replacement["id"]), "default")
        self.wait_signal(("action", source_id, "default"), "replacement action did not return")
        source.CloseNotification(dbus.UInt32(source_id))
        self.wait_signal(("closed", source_id, 3), "replacement did not close")

    def test_click_and_dismiss_before_initial_notify_reply_are_replayed(self):
        self.start_delayed_destination()
        source = self.start_endpoint()
        source_id = self.notify("early callbacks")
        requested = wait_for(lambda: next((row for row in rows(self.events_path)
                            if row.get("event") == "notify-received"), None), "request missing")
        local_id = requested["id"]
        self.control.RawAction(dbus.UInt32(local_id + 1), "default")
        self.control.RawAction(dbus.UInt32(local_id), "not-offered")
        self.control.RawAction(dbus.UInt32(local_id), "default")
        self.control.Dismiss(dbus.UInt32(local_id))
        pump_for(0.1)
        self.assertEqual(self.signal_events, [])
        self.assertTrue(self.control.ReleaseReply(dbus.UInt32(local_id)))
        self.wait_signal(("closed", source_id, 2), "early dismissal was lost")
        self.assertEqual(self.signal_events, [("action", source_id, "default"), ("closed", source_id, 2)])
        with self.assertRaises(dbus.DBusException):
            source.CloseNotification(dbus.UInt32(source_id))

    def test_early_replacement_callbacks_use_the_new_offered_actions(self):
        self.start_delayed_destination()
        self.start_endpoint()
        source_id = self.notify("original actions")
        requested = wait_for(lambda: next((row for row in rows(self.events_path)
                            if row.get("event") == "notify-received"), None), "request missing")
        local_id = requested["id"]
        self.assertTrue(self.control.ReleaseReply(dbus.UInt32(local_id)))
        wait_for(lambda: f"delivered\t{source_id}\t{local_id}" in self.listener_log.read_text(),
                 "original was not mapped")
        self.assertEqual(self.notify("new actions", replaces=source_id, actions=("reply", "Reply")), source_id)
        wait_for(lambda: any(row.get("summary") == "new actions" for row in rows(self.events_path)),
                 "replacement missing")
        self.control.RawAction(dbus.UInt32(local_id), "default")
        self.control.RawAction(dbus.UInt32(local_id), "reply")
        self.control.Dismiss(dbus.UInt32(local_id))
        pump_for(0.1)
        self.assertEqual(self.signal_events, [])
        self.assertTrue(self.control.ReleaseReply(dbus.UInt32(local_id)))
        self.wait_signal(("closed", source_id, 2), "replacement dismissal was lost")
        self.assertEqual(self.signal_events, [("action", source_id, "reply"), ("closed", source_id, 2)])

    def test_legacy_all_apps_accepts_blank_application_names(self):
        source = (REPO / "bin/remote-chrome").read_text()
        match = re.search(r"cat >\"\$notify_forwarder_script\" <<'REMOTE_CHROME_NOTIFY_FORWARDER'\n"
                          r"(.*?)\nREMOTE_CHROME_NOTIFY_FORWARDER", source, re.S)
        self.assertIsNotNone(match)
        namespace = {"__name__": "notification_forwarder_test"}
        exec(compile(match.group(1), "notify-forwarder", "exec"), namespace)
        record = namespace["extract"]({
            "type": "method_call", "interface": NAME, "member": "Notify",
            "payload": {"data": ["", 0, "", "summary", "body", [], {}, 0]},
        })
        self.assertIsNotNone(record)
        self.assertEqual((record["app_name"], record["summary"], record["body"]), ("", "summary", "body"))
        self.assertTrue(namespace["matches"](record["app_name"], namespace["parse_apps"]("*")))
        self.assertFalse(namespace["matches"](record["app_name"], namespace["parse_apps"]("chrome")))

    def test_source_ignores_callbacks_for_an_update_superseded_in_transit(self):
        namespace = {"__name__": "notification_endpoint_test"}
        exec(compile(helper_source(), "notify-endpoint", "exec"), namespace)
        endpoint = object.__new__(namespace["NotificationEndpoint"])
        endpoint.dbus = dbus
        endpoint.session_id, endpoint.generation = "revision-test", "generation"
        emitted = []
        endpoint.object = SimpleNamespace(
            ActionInvoked=lambda nid, key: emitted.append(("action", int(nid), str(key))),
            NotificationClosed=lambda nid, reason: emitted.append(("closed", int(nid), int(reason))),
        )
        # Revision two was accepted locally, but only revision one is mapped.
        endpoint.active = {1: {"revision": 2, "mapped_revision": 1, "local_id": 700,
                               "actions": ["default"], "close_requested": False}}
        base = {"v": 1, "session_id": endpoint.session_id, "generation": endpoint.generation,
                "source_id": 1, "revision": 1, "local_id": 700}
        for kind in ("mapped", "action", "closed", "delivery-error"):
            endpoint.handle(base | {"type": kind, "key": "default", "reason": 1})
        self.assertEqual(emitted, [])
        self.assertEqual(endpoint.active[1]["mapped_revision"], 1)
        current = base | {"revision": 2, "local_id": 701}
        endpoint.handle(current | {"type": "mapped"})
        self.assertEqual(endpoint.active[1]["mapped_revision"], 2)
        endpoint.handle(current | {"type": "action", "key": "default"})
        endpoint.handle(current | {"type": "closed", "reason": 2})
        self.assertEqual(emitted, [("action", 1, "default"), ("closed", 1, 2)])
        self.assertEqual(endpoint.active, {})

    def test_listener_rejects_stale_notification_revisions_without_closing_current(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.raw_connections.append(connection)
        connection.connect(str(self.relay))
        identity = {"v": 1, "session_id": "revision-protocol-test", "generation": "generation"}
        def send(kind, **fields):
            connection.sendall((json.dumps(identity | {"type": kind} | fields) + "\n").encode())
        send("hello", notification_revisions=True)
        self.assertTrue(self.recv_frame(connection)["notification_revisions"])
        send("active")
        notification = {"source_id": 1, "revision": 2, "replaces_id": 0, "app_name": "Chrome",
                        "summary": "current", "body": "body", "actions": [], "expires": 0}
        send("notify", **notification)
        mapped = self.recv_frame(connection)
        self.assertEqual((mapped["type"], mapped["revision"]), ("mapped", 2))
        send("notify", **(notification | {"revision": 1, "replaces_id": 1, "summary": "stale"}))
        self.assertEqual(self.recv_frame(connection)["code"], "stale-notification-revision")
        send("close", source_id=1, revision=1, local_id=mapped["local_id"])
        self.assertEqual(self.recv_frame(connection)["code"], "stale-notification-revision")
        send("close", source_id=1, revision=True, local_id=mapped["local_id"])
        self.assertEqual(self.recv_frame(connection)["code"], "invalid-notification-revision")
        self.assertEqual(len(self.destination_notifications()), 1)
        send("close", source_id=1, revision=2, local_id=mapped["local_id"])
        closed = self.recv_frame(connection)
        self.assertEqual((closed["type"], closed["revision"], closed["reason"]), ("closed", 2, 3))

    def test_excess_early_callbacks_disconnect_and_drain_the_owned_delivery(self):
        self.start_delayed_destination()
        self.start_endpoint()
        self.notify("bounded early callbacks")
        requested = wait_for(lambda: next((row for row in rows(self.events_path)
                            if row.get("event") == "notify-received"), None), "request missing")
        for offset in range(1, 66):
            self.control.RawAction(dbus.UInt32(requested["id"] + offset), "default")
        wait_for(lambda: self.endpoint.poll() is not None, "early callback queue did not enforce its limit")
        self.assertFalse(self.has_source_owner())
        self.assertTrue(self.control.ReleaseReply(dbus.UInt32(requested["id"])))
        wait_for(lambda: any(row.get("event") == "closed" and row.get("id") == requested["id"]
                            for row in rows(self.events_path)), "overflow did not drain the owned destination ID")
        self.assertEqual(self.signal_events, [])
        self.assertIsNone(self.listener.poll())

    def test_destination_owner_replacement_invalidates_old_ids_and_actions(self):
        source = self.start_endpoint()
        source_id = self.notify("old daemon notification")
        wait_for(lambda: len(self.destination_notifications()) == 1,
                 "old destination did not receive notification")
        old_local_id = self.destination_notifications()[0]["id"]
        wait_for(
            lambda: self.listener_log.exists()
            and any(f"\tdelivered\t{source_id}\t{old_local_id}\t" in line
                    for line in self.listener_log.read_text().splitlines()),
            "old destination ID was not mapped before owner replacement",
        )

        self.destination_fixture.send_signal(signal.SIGTERM)
        self.destination_fixture.wait(timeout=3)
        wait_for(lambda: self.endpoint.poll() is not None,
                 "source helper retained its name after destination owner loss")
        self.assertFalse(self.has_source_owner())

        replacement_ready = self.root / "replacement-destination.ready"
        self.destination_fixture = self.start_destination_fixture(
            "replacement-destination", self.destination_address, self.events_path, replacement_ready,
        )
        wait_for(replacement_ready.exists, "replacement destination did not acquire its name")
        self.control = dbus.Interface(
            self.destination_bus.get_object(NAME, PATH), "org.remotechrome.Test"
        )
        destination = dbus.Interface(self.destination_bus.get_object(NAME, PATH), NAME)
        new_local_id = int(destination.Notify(
            "replacement daemon", dbus.UInt32(0), "", "new daemon notification", "body",
            dbus.Array(["default", "Open"], signature="s"),
            dbus.Dictionary({}, signature="sv"), dbus.Int32(0),
        ))
        self.assertEqual(new_local_id, old_local_id)
        self.assertTrue(self.control.Action(dbus.UInt32(new_local_id), "default"))
        time.sleep(0.1)
        self.assertNotIn(("action", source_id, "default"), self.signal_events)
        self.assertFalse(self.has_source_owner())

    def test_competing_helper_loses_do_not_queue_without_disturbing_owner(self):
        self.start_endpoint()
        self.assertTrue(self.has_source_owner())
        original_pid = self.endpoint.pid

        second_runtime = self.root / "destination-2-runtime"
        second_runtime.mkdir(mode=0o700)
        second_address = self.start_bus("destination-2")
        second_events = self.root / "destination-2.jsonl"
        second_ready = self.root / "destination-2.ready"
        self.start_destination_fixture("destination-2-fixture", second_address, second_events, second_ready)
        wait_for(second_ready.exists, "second destination did not acquire its name")
        second_relay = self.root / "relay-2.sock"
        second_listener_log = self.root / "listener-2.events"
        self.start_listener(
            "listener-2", second_address, second_runtime, second_relay, second_listener_log,
        )
        wait_for(second_relay.is_socket, "second listener did not create its socket")

        second_status = self.root / "second-endpoint.state"
        second_status.touch(mode=0o600)
        contender_env = os.environ | {
            "DBUS_SESSION_BUS_ADDRESS": self.source_address,
            "XDG_RUNTIME_DIR": str(self.source_runtime),
            "REMOTE_CHROME_ORIGIN_SESSION": "competing-helper-test",
        }
        contender = self.start_process("contending-endpoint", [
            "python3", str(self.endpoint_script), str(second_relay), "chrome", str(second_status),
        ], contender_env)
        self.assertEqual(contender.wait(timeout=4), 20)
        self.assertEqual(second_status.read_text().splitlines()[0], "busy-managed")
        self.assertEqual(self.endpoint.pid, original_pid)
        self.assertIsNone(self.endpoint.poll())
        self.assertTrue(self.has_source_owner())
        owner = dbus.Interface(
            self.source_bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus"),
            "org.freedesktop.DBus",
        ).GetNameOwner(NAME)
        information = dbus.Interface(
            self.source_bus.get_object(str(owner), PATH), NAME
        ).GetServerInformation()
        self.assertEqual(str(information[0]), "remote-chrome-notification-endpoint")
        self.assertEqual(str(information[1]), "remote-chrome")
        self.assertEqual(rows(second_events), [])

    def test_oversized_frame_disconnects_and_listener_accepts_next_client(self):
        first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.raw_connections.append(first)
        first.connect(str(self.relay))
        session = "oversized-frame-test"
        generation = "generation-oversized"
        first.sendall((json.dumps({
            "v": 1, "type": "hello", "session_id": session, "generation": generation,
        }) + "\n").encode("utf-8"))
        self.assertEqual(self.recv_frame(first)["type"], "ready")
        first.sendall(b"x" * (64 * 1024 + 1))
        first.setblocking(False)

        def is_disconnected():
            ready, _, _ = select.select([first], [], [], 0)
            if not ready:
                return False
            try:
                return first.recv(1) == b""
            except BlockingIOError:
                return False

        wait_for(is_disconnected, "listener did not disconnect oversized protocol frame")

        following = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.raw_connections.append(following)
        following.connect(str(self.relay))
        following.sendall((json.dumps({
            "v": 1, "type": "hello", "session_id": "after-oversized-frame",
            "generation": "generation-following",
        }) + "\n").encode("utf-8"))
        ready = self.recv_frame(following)
        self.assertEqual(ready["type"], "ready")
        self.assertEqual(ready["v"], 1)
        self.assertTrue(ready["destination_owner"].startswith(":"))

    def tearDown(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
        for match in self.signal_matches:
            try:
                match.remove()
            except Exception:
                pass
        for bus in (self.source_bus, self.destination_bus):
            if bus is not None:
                try:
                    bus.close()
                except Exception:
                    pass
        for connection in self.raw_connections:
            try:
                connection.close()
            except OSError:
                pass
        for handle in self.handles:
            handle.close()
        self.temp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
