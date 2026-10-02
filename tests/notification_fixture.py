#!/usr/bin/env python3
"""Private-bus notification daemon and Secret Service ownership test fixture."""

import argparse
import json
from pathlib import Path
import signal

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

NAME = "org.freedesktop.Notifications"
OBJECT = "/org/freedesktop/Notifications"
CONTROL = "org.remotechrome.Test"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--notifications", action="store_true")
    parser.add_argument("--secrets", action="store_true")
    parser.add_argument("--caps", default="body,body-markup,actions")
    parser.add_argument("--server", default="notification-test-fixture")
    parser.add_argument("--vendor", default="remote-chrome-tests")
    args = parser.parse_args()
    if not args.address.startswith("unix:path=/tmp/remote-chrome-notify-test-"):
        parser.error("a private test bus is required")
    DBusGMainLoop(set_as_default=True)
    bus = dbus.bus.BusConnection(args.address)
    names = []
    for name, enabled in ((NAME, args.notifications), ("org.freedesktop.secrets", args.secrets)):
        if enabled:
            if int(bus.request_name(name, 4)) != 1:
                raise RuntimeError("fixture could not acquire " + name)
            names.append(name)

    def event(kind, **fields):
        with args.events.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": kind, **fields}) + "\n")

    class NotificationDaemon(dbus.service.Object):
        def __init__(self):
            super().__init__(bus, OBJECT)
            self.next_id = 100
            self.active = {}
            self.timers = {}

        @dbus.service.method(NAME, in_signature="", out_signature="as")
        def GetCapabilities(self):
            return dbus.Array(args.caps.split(","), signature="s")

        @dbus.service.method(NAME, in_signature="", out_signature="ssss")
        def GetServerInformation(self):
            return (args.server, args.vendor, "1", "1.2")

        @dbus.service.method(NAME, in_signature="susssasa{sv}i", out_signature="u")
        def Notify(self, app, replaces, icon, summary, body, actions, hints, expires):
            nid = int(replaces) if int(replaces) in self.active else self.next_id
            if nid == self.next_id:
                self.next_id += 1
            if nid in self.timers:
                GLib.source_remove(self.timers.pop(nid))
            self.active[nid] = [str(item) for item in actions]
            event("notify", id=nid, replaces=int(replaces), app=str(app), icon=str(icon),
                  summary=str(summary), body=str(body), actions=self.active[nid],
                  urgency=int(hints.get("urgency", 1)), expires=int(expires))
            if int(expires) > 0:
                self.timers[nid] = GLib.timeout_add(int(expires), self.finish, nid, 1)
            return dbus.UInt32(nid)

        def finish(self, nid, reason):
            if nid in self.active:
                self.active.pop(nid)
                timer = self.timers.pop(nid, None)
                if timer and reason != 1:
                    GLib.source_remove(timer)
                event("closed", id=nid, reason=reason)
                self.NotificationClosed(nid, reason)
            return False

        @dbus.service.method(NAME, in_signature="u", out_signature="")
        def CloseNotification(self, nid):
            self.finish(int(nid), 3)

        @dbus.service.signal(NAME, signature="uu")
        def NotificationClosed(self, nid, reason):
            pass

        @dbus.service.signal(NAME, signature="us")
        def ActionInvoked(self, nid, key):
            pass

        @dbus.service.method(CONTROL, in_signature="us", out_signature="b")
        def Action(self, nid, key):
            if str(key) not in self.active.get(int(nid), [])[::2]:
                return False
            event("action", id=int(nid), key=str(key))
            self.ActionInvoked(nid, key)
            return True

        @dbus.service.method(CONTROL, in_signature="u", out_signature="")
        def Dismiss(self, nid):
            self.finish(int(nid), 2)

    endpoint = NotificationDaemon() if args.notifications else None
    loop = GLib.MainLoop()
    signal.signal(signal.SIGTERM, lambda *_: loop.quit())
    signal.signal(signal.SIGINT, lambda *_: loop.quit())
    args.ready.write_text(bus.get_unique_name())
    try:
        loop.run()
    finally:
        if endpoint:
            endpoint.remove_from_connection()
        for name in names:
            bus.release_name(name)
        bus.close()


if __name__ == "__main__":
    main()
