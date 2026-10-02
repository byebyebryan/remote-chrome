#!/usr/bin/env python3
"""Study-only notification endpoint; never connect this to the live user bus."""

import argparse
import json
from pathlib import Path
import signal
import sys

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

NAME = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--capabilities", default="body,actions,body-markup")
    parser.add_argument("--flags", type=int, default=5)
    args = parser.parse_args()
    # Require a study-private socket, so an accidental invocation cannot own
    # the notification name on the real user bus.
    if not args.address.startswith("unix:path=/tmp/remote-chrome-notification-study-"):
        parser.error("only a study-private bus address is accepted")
    DBusGMainLoop(set_as_default=True)
    bus = dbus.bus.BusConnection(args.address)
    loop = GLib.MainLoop()

    def event(kind, **fields):
        with args.events.open("a") as handle:
            handle.write(json.dumps({"event": kind, **fields}) + "\n")

    result = int(bus.request_name(NAME, args.flags))
    event("request_name", result=result, unique_name=bus.get_unique_name())
    if result not in (1, 4):
        return 10

    class Endpoint(dbus.service.Object):
        def __init__(self):
            super().__init__(bus, PATH)
            self.next_id = 1
            self.ids = set()

        @dbus.service.method(NAME, in_signature="", out_signature="as")
        def GetCapabilities(self):
            event("capabilities")
            return dbus.Array(args.capabilities.split(","), signature="s")

        @dbus.service.method(NAME, in_signature="", out_signature="ssss")
        def GetServerInformation(self):
            event("server_information")
            return ("remote-chrome-study", "remote-chrome", "0.0", "1.2")

        @dbus.service.method(NAME, in_signature="susssasa{sv}i", out_signature="u", sender_keyword="sender")
        def Notify(self, app, replaces, icon, summary, body, actions, hints, expires, sender=None):
            nid = int(replaces) if int(replaces) in self.ids else self.next_id
            if nid == self.next_id:
                self.next_id += 1
            self.ids.add(nid)
            event("notify", app=str(app), summary=str(summary), body=str(body),
                  actions=[str(value) for value in actions], id=nid, sender=str(sender))
            return dbus.UInt32(nid)

        @dbus.service.method(NAME, in_signature="u", out_signature="")
        def CloseNotification(self, nid):
            event("close", id=int(nid))
            self.ids.discard(int(nid))
            self.NotificationClosed(nid, 3)

        @dbus.service.signal(NAME, signature="uu")
        def NotificationClosed(self, nid, reason):
            pass

        @dbus.service.signal(NAME, signature="us")
        def ActionInvoked(self, nid, action):
            pass

        @dbus.service.method("org.remotechrome.Study", in_signature="us", out_signature="")
        def TriggerAction(self, nid, action):
            event("action", id=int(nid), action=str(action))
            self.ActionInvoked(nid, action)

    endpoint = Endpoint()

    def lost(name):
        if str(name) == NAME:
            event("name_lost")
            loop.quit()

    bus.add_signal_receiver(lost, signal_name="NameLost", dbus_interface="org.freedesktop.DBus")
    signal.signal(signal.SIGTERM, lambda *_: loop.quit())
    signal.signal(signal.SIGINT, lambda *_: loop.quit())
    args.ready.write_text(bus.get_unique_name())
    try:
        loop.run()
    finally:
        endpoint.remove_from_connection()
        bus.release_name(NAME)
        bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
