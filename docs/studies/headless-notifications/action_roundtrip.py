#!/usr/bin/env python3
"""Isolated bidirectional notification-action prototype.

The controller starts two private D-Bus daemons, a source-side notification
endpoint, a direct destination D-Bus client, a destination notification-daemon
fixture, a private Unix-socket relay, and disposable Chrome/KWin instances.
Nothing connects to the live user bus or browser profile.
"""

import argparse
import ast
from collections import deque
import html
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from html.parser import HTMLParser

import dbus
import dbus.bus
import dbus.exceptions
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from study import HERE, NAME, OBJECT, Page, REPO, Study, WebSocket, records, until


VERSION = 1
MAX_FRAME = 64 * 1024
MAX_QUEUED_MESSAGES = 64
MAX_QUEUED_BYTES = 1 << 20
MAX_ACTIVE = 64
MAX_ACTIONS = 8
IMPLEMENTED_CAPABILITIES = {"body", "body-markup", "actions"}
PRIVATE_PREFIX = "/tmp/remote-chrome-notification-study-"


def write_event(path, event, **fields):
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")


def load_current_formatter():
    """Load the existing inline listener formatter without copying its code."""
    source = (REPO / "bin/remote-chrome").read_text(encoding="utf-8")
    marker = "<<'REMOTE_CHROME_NOTIFY_LISTENER'\n"
    start = source.index(marker) + len(marker)
    end = source.index("\nREMOTE_CHROME_NOTIFY_LISTENER", start)
    listener = ast.parse(source[start:end])
    wanted = {
        "ENTITIES", "TAGS", "SAFE_NAME", "MAX_SUMMARY", "MAX_BODY", "MAX_APP",
        "MAX_DESKTOP_ENTRY", "NotificationBody", "clean_body", "clean_text", "clean_name",
    }
    nodes = [
        node for node in listener.body
        if (isinstance(node, ast.ClassDef) and node.name in wanted)
        or (isinstance(node, ast.FunctionDef) and node.name in wanted)
        or (isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in wanted
                    for target in node.targets))
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    namespace = {"html": html, "re": re, "HTMLParser": HTMLParser}
    exec(compile(module, "bin/remote-chrome:_notify-listener-formatter", "exec"), namespace)
    return namespace


def format_notification(formatter, record, body_markup):
    summary = formatter["clean_text"](record.get("summary"), formatter["MAX_SUMMARY"])
    plain_body = formatter["clean_body"](record.get("body"), False)
    body = ""
    if plain_body.strip():
        body = (formatter["clean_body"](record.get("body"), True)
                if body_markup else plain_body)
    app_name = formatter["clean_text"](record.get("app"), formatter["MAX_APP"]) or "remote-chrome"
    desktop_entry = formatter["clean_name"](record.get("desktop_entry"),
                                              formatter["MAX_DESKTOP_ENTRY"])
    return {
        "app_name": app_name,
        "summary": summary or plain_body[:formatter["MAX_SUMMARY"]],
        "body": body,
        "urgency": record.get("urgency", 1),
        "desktop_entry": desktop_entry,
    }


def envelope(session_id, generation, kind, **fields):
    return {
        "v": VERSION,
        "type": kind,
        "session_id": session_id,
        "generation": generation,
        **fields,
    }


def read_line(sock, max_bytes=MAX_FRAME):
    result = bytearray()
    while len(result) <= max_bytes:
        chunk = sock.recv(1)
        if not chunk:
            raise EOFError("peer closed before handshake completed")
        if chunk == b"\n":
            return json.loads(result.decode("utf-8"))
        result.extend(chunk)
    raise ValueError("oversized handshake")


class SocketChannel:
    """One bounded, nonblocking, newline-delimited JSON stream on GLib."""

    def __init__(self, sock, handler, on_close, log_path):
        self.sock = sock
        self.sock.setblocking(False)
        self.handler = handler
        self.on_close = on_close
        self.log_path = Path(log_path)
        self.input = bytearray()
        self.outgoing = deque()
        self.outgoing_bytes = 0
        self.read_watch = GLib.io_add_watch(
            self.sock.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
            self._readable)
        self.write_watch = None
        self.closed = False

    def send(self, message):
        if self.closed:
            raise ConnectionError("notification relay is closed")
        wire = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(wire) > MAX_FRAME:
            raise ValueError("notification relay frame is too large")
        if (len(self.outgoing) >= MAX_QUEUED_MESSAGES
                or self.outgoing_bytes + len(wire) > MAX_QUEUED_BYTES):
            raise BufferError("notification relay queue is full")
        self.outgoing.append(memoryview(wire))
        self.outgoing_bytes += len(wire)
        if self.write_watch is None:
            self.write_watch = GLib.io_add_watch(
                self.sock.fileno(), GLib.IO_OUT | GLib.IO_HUP | GLib.IO_ERR,
                self._writable)

    def _readable(self, _fd, condition):
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            self.close("peer-closed")
            return False
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    self.close("peer-closed")
                    return False
                self.input.extend(chunk)
                if len(self.input) > MAX_FRAME:
                    raise ValueError("notification relay frame is too large")
                while True:
                    index = self.input.find(b"\n")
                    if index < 0:
                        break
                    line = bytes(self.input[:index])
                    del self.input[:index + 1]
                    if len(line) > MAX_FRAME:
                        raise ValueError("notification relay frame is too large")
                    message = json.loads(line.decode("utf-8"))
                    if not isinstance(message, dict):
                        raise ValueError("notification relay frame is not an object")
                    self.handler(message)
        except BlockingIOError:
            pass
        except (ConnectionError, OSError, UnicodeError, ValueError, RecursionError) as error:
            write_event(self.log_path, "protocol-error", message=str(error))
            self.close("protocol-error")
            return False
        return True

    def _writable(self, _fd, condition):
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            self.close("peer-closed")
            return False
        try:
            while self.outgoing:
                current = self.outgoing[0]
                sent = self.sock.send(current)
                if sent <= 0:
                    break
                self.outgoing_bytes -= sent
                if sent == len(current):
                    self.outgoing.popleft()
                else:
                    self.outgoing[0] = current[sent:]
                    break
        except BlockingIOError:
            pass
        except OSError as error:
            write_event(self.log_path, "socket-write-failed", message=str(error))
            self.close("socket-write-failed")
            return False
        if not self.outgoing:
            self.write_watch = None
            return False
        return True

    def close(self, reason):
        if self.closed:
            return
        self.closed = True
        for watch in (self.read_watch, self.write_watch):
            if watch is not None:
                try:
                    GLib.source_remove(watch)
                except GLib.Error:
                    pass
        self.read_watch = self.write_watch = None
        try:
            self.sock.close()
        finally:
            self.on_close(reason)


class EndpointUnavailable(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.LimitsExceeded"


def run_source(args):
    if not args.address.startswith("unix:path=" + PRIVATE_PREFIX):
        raise RuntimeError("source endpoint only accepts a private study bus")
    if not args.socket.startswith(PRIVATE_PREFIX):
        raise RuntimeError("source endpoint only accepts a private study socket")
    DBusGMainLoop(set_as_default=True)
    bus = dbus.bus.BusConnection(args.address)
    loop = GLib.MainLoop()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect(args.socket)
    sock.sendall((json.dumps(envelope(args.session_id, args.generation, "hello")) + "\n").encode())
    ready = read_line(sock)
    if (ready.get("v") != VERSION or ready.get("type") != "ready"
            or ready.get("session_id") != args.session_id
            or ready.get("generation") != args.generation):
        raise RuntimeError("destination handshake identity did not match")
    remote_caps = ready.get("capabilities")
    if not isinstance(remote_caps, list) or any(not isinstance(item, str) for item in remote_caps):
        raise RuntimeError("destination handshake capabilities are malformed")
    capabilities = sorted(set(remote_caps) & IMPLEMENTED_CAPABILITIES)
    sock.settimeout(None)
    event_path = Path(args.events)
    source_to_local = {}
    local_to_source = {}
    active = {}
    next_id = 1
    channel = None

    class Endpoint(dbus.service.Object):
        def __init__(self):
            super().__init__(bus, OBJECT)

        @dbus.service.method(NAME, in_signature="", out_signature="as")
        def GetCapabilities(self):
            write_event(event_path, "capabilities", capabilities=capabilities)
            return dbus.Array(capabilities, signature="s")

        @dbus.service.method(NAME, in_signature="", out_signature="ssss")
        def GetServerInformation(self):
            return ("remote-chrome-action-study", "remote-chrome", "0.0", "1.2")

        @dbus.service.method(NAME, in_signature="susssasa{sv}i", out_signature="u")
        def Notify(self, app, replaces_id, icon, summary, body, actions, hints, expire_timeout):
            nonlocal next_id
            action_values = [str(value) for value in actions]
            if len(action_values) % 2 or len(action_values) > MAX_ACTIONS * 2:
                raise EndpointUnavailable("notification action list is malformed or too large")
            if len(str(summary)) > 512 or len(str(body)) > 4096:
                raise EndpointUnavailable("notification exceeds the study size limit")
            replacement = int(replaces_id)
            source_id = replacement if replacement in active else next_id
            if source_id == next_id:
                next_id += 1
            if source_id not in active and len(active) >= MAX_ACTIVE:
                raise EndpointUnavailable("study endpoint has reached its active limit")
            hint_values = {}
            try:
                urgency = hints.get("urgency")
                if urgency is not None:
                    hint_values["urgency"] = int(urgency)
                desktop_entry = hints.get("desktop-entry")
                if desktop_entry is not None:
                    hint_values["desktop_entry"] = str(desktop_entry)
            except (AttributeError, TypeError, ValueError):
                hint_values = {}
            notification = {
                "source_id": source_id,
                "replaces_source_id": replacement if replacement in active else 0,
                "app": str(app), "summary": str(summary), "body": str(body),
                "actions": action_values, "hints": hint_values,
                "expires": int(expire_timeout),
            }
            try:
                channel.send(envelope(args.session_id, args.generation, "notify", **notification))
            except (BufferError, ConnectionError, OSError, ValueError) as error:
                raise EndpointUnavailable(str(error))
            active[source_id] = {"action_keys": set(action_values[::2])}
            write_event(event_path, "notify", **notification)
            return dbus.UInt32(source_id)

        @dbus.service.method(NAME, in_signature="u", out_signature="")
        def CloseNotification(self, notification_id):
            source_id = int(notification_id)
            if source_id not in active:
                return
            try:
                channel.send(envelope(args.session_id, args.generation, "close",
                                      source_id=source_id))
            except (BufferError, ConnectionError, OSError, ValueError) as error:
                raise EndpointUnavailable(str(error))
            write_event(event_path, "close-request", source_id=source_id)

        @dbus.service.signal(NAME, signature="us")
        def ActionInvoked(self, notification_id, action):
            pass

        @dbus.service.signal(NAME, signature="uu")
        def NotificationClosed(self, notification_id, reason):
            pass

    def on_message(message):
        if (message.get("v") != VERSION or message.get("session_id") != args.session_id
                or message.get("generation") != args.generation):
            write_event(event_path, "stale-message-rejected", message_type=message.get("type"))
            return
        kind = message.get("type")
        if kind == "mapped":
            source_id, local_id = message.get("source_id"), message.get("local_id")
            if not isinstance(source_id, int) or not isinstance(local_id, int):
                write_event(event_path, "malformed-map-rejected")
                return
            if source_id not in active:
                write_event(event_path, "unknown-map-rejected", source_id=source_id,
                            local_id=local_id)
                return
            old_local = source_to_local.get(source_id)
            if old_local is not None and old_local != local_id:
                local_to_source.pop(old_local, None)
            source_to_local[source_id] = local_id
            local_to_source[local_id] = source_id
            write_event(event_path, "mapped", source_id=source_id, local_id=local_id)
            return
        if kind == "action":
            source_id, local_id, key = (message.get("source_id"), message.get("local_id"),
                                        message.get("key"))
            if (not isinstance(source_id, int) or not isinstance(local_id, int)
                    or local_to_source.get(local_id) != source_id
                    or source_to_local.get(source_id) != local_id
                    or source_id not in active or not isinstance(key, str)
                    or key not in active[source_id]["action_keys"]):
                write_event(event_path, "action-rejected", source_id=source_id,
                            local_id=local_id, key=key)
                return
            endpoint.ActionInvoked(dbus.UInt32(source_id), dbus.String(key))
            write_event(event_path, "action", source_id=source_id, local_id=local_id, key=key)
            return
        if kind == "closed":
            source_id, local_id, reason = (message.get("source_id"), message.get("local_id"),
                                           message.get("reason"))
            if (not isinstance(source_id, int) or not isinstance(local_id, int)
                    or local_to_source.get(local_id) != source_id
                    or source_to_local.get(source_id) != local_id
                    or source_id not in active or reason not in (1, 2, 3, 4)):
                write_event(event_path, "closed-rejected", source_id=source_id,
                            local_id=local_id, reason=reason)
                return
            endpoint.NotificationClosed(dbus.UInt32(source_id), dbus.UInt32(reason))
            active.pop(source_id, None)
            source_to_local.pop(source_id, None)
            local_to_source.pop(local_id, None)
            write_event(event_path, "closed", source_id=source_id, local_id=local_id,
                        reason=reason)
            return
        write_event(event_path, "unknown-message-rejected", message_type=kind)

    endpoint = Endpoint()
    channel = SocketChannel(sock, on_message,
                            lambda reason: write_event(event_path, "socket-closed", reason=reason),
                            event_path)
    ownership = int(bus.request_name(NAME, dbus.bus.NAME_FLAG_DO_NOT_QUEUE))
    write_event(event_path, "ready", name_result=ownership, capabilities=capabilities,
                unique_name=bus.get_unique_name(), session_id=args.session_id,
                generation=args.generation)
    if ownership != dbus.bus.REQUEST_NAME_REPLY_PRIMARY_OWNER:
        return 10
    Path(args.ready).write_text(json.dumps({"capabilities": capabilities,
                                            "unique_name": bus.get_unique_name()}))

    def stop(_signum, _frame):
        loop.quit()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        loop.run()
    finally:
        channel.close("shutdown")
        endpoint.remove_from_connection()
        bus.release_name(NAME)
        bus.close()
    return 0


class DestinationClient:
    def __init__(self, args):
        self.args = args
        self.events = Path(args.events)
        self.loop = GLib.MainLoop()
        self.bus = dbus.bus.BusConnection(args.address)
        self.proxy = self.bus.get_object(NAME, OBJECT)
        self.interface = dbus.Interface(self.proxy, NAME)
        raw_caps = self.interface.GetCapabilities(timeout=2)
        local_caps = {str(value) for value in raw_caps}
        self.capabilities = sorted(local_caps & IMPLEMENTED_CAPABILITIES)
        self.session_id = args.session_id
        self.generation = args.generation
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.setblocking(False)
        self.server.bind(args.socket)
        os.chmod(args.socket, 0o600)
        self.server.listen(1)
        self.accept_watch = GLib.io_add_watch(
            self.server.fileno(), GLib.IO_IN | GLib.IO_ERR,
            self._accept)
        self.channel = None
        self.pending = deque()
        self.pending_inflight = False
        self.source_to_local = {}
        self.local_to_source = {}
        self.actions_by_source = {}
        self.formatter = load_current_formatter()
        self.bus.add_signal_receiver(self._action_invoked,
                                     signal_name="ActionInvoked",
                                     dbus_interface=NAME, bus_name=NAME, path=OBJECT)
        self.bus.add_signal_receiver(self._notification_closed,
                                     signal_name="NotificationClosed",
                                     dbus_interface=NAME, bus_name=NAME, path=OBJECT)
        write_event(self.events, "client-ready", capabilities=self.capabilities,
                    formatter_source="bin/remote-chrome:_notify-listener",
                    socket=args.socket, session_id=self.session_id,
                    generation=self.generation)
        Path(args.ready).write_text(json.dumps({"capabilities": self.capabilities}))

    def _accept(self, _fd, condition):
        if condition & GLib.IO_ERR:
            self.loop.quit()
            return False
        try:
            client, _ = self.server.accept()
        except BlockingIOError:
            return True
        if self.channel is not None:
            client.close()
            write_event(self.events, "second-peer-rejected")
            return True
        client.settimeout(2)
        try:
            hello = read_line(client)
            if (hello.get("v") != VERSION or hello.get("type") != "hello"
                    or hello.get("session_id") != self.session_id
                    or hello.get("generation") != self.generation):
                raise ValueError("socket handshake session/generation mismatch")
            response = envelope(self.session_id, self.generation, "ready",
                                capabilities=self.capabilities)
            client.sendall((json.dumps(response) + "\n").encode("utf-8"))
            client.settimeout(None)
        except (OSError, ValueError, EOFError) as error:
            write_event(self.events, "handshake-rejected", message=str(error))
            client.close()
            return True
        self.channel = SocketChannel(client, self._message,
                                     lambda reason: write_event(self.events, "socket-closed",
                                                                reason=reason),
                                     self.events)
        write_event(self.events, "peer-connected", session_id=self.session_id,
                    generation=self.generation)
        return True

    def _valid_envelope(self, message):
        return (message.get("v") == VERSION
                and message.get("session_id") == self.session_id
                and message.get("generation") == self.generation)

    def _message(self, message):
        if not self._valid_envelope(message):
            write_event(self.events, "stale-message-rejected", message_type=message.get("type"))
            return
        if message.get("type") not in ("notify", "close"):
            write_event(self.events, "unknown-message-rejected", message_type=message.get("type"))
            return
        if len(self.pending) >= MAX_QUEUED_MESSAGES:
            write_event(self.events, "queue-full", queue="operations")
            self.loop.quit()
            return
        write_event(self.events, "queue-enqueued", message_type=message["type"],
                    source_id=message.get("source_id"),
                    replaces_source_id=message.get("replaces_source_id", 0),
                    summary=message.get("summary"))
        self.pending.append(message)
        self._pump()

    def _pump(self):
        if self.pending_inflight or not self.pending or self.channel is None:
            return
        message = self.pending.popleft()
        self.pending_inflight = True
        if message["type"] == "notify":
            delay = max(0, int(self.args.delay_ms))
            if delay:
                GLib.timeout_add(delay, self._notify, message)
            else:
                self._notify(message)
        else:
            self._close(message)

    def _notify(self, message):
        source_id = message.get("source_id")
        replaces_source_id = message.get("replaces_source_id", 0)
        if (not isinstance(source_id, int) or source_id <= 0
                or not isinstance(replaces_source_id, int) or replaces_source_id < 0):
            self._operation_error(message, "invalid source notification ID")
            return GLib.SOURCE_REMOVE
        actions = message.get("actions")
        if (not isinstance(actions, list) or len(actions) % 2
                or len(actions) > MAX_ACTIONS * 2
                or any(not isinstance(value, str) or len(value) > 128 for value in actions)):
            self._operation_error(message, "invalid action list")
            return GLib.SOURCE_REMOVE
        if "actions" in self.capabilities and not actions:
            # Actions are optional per notification; this is safe because the
            # session handshake established the actual callback path.
            pass
        replaced_local = self.source_to_local.get(replaces_source_id, 0)
        formatted = format_notification(self.formatter, message, "body-markup" in self.capabilities)
        hints = {}
        if formatted["urgency"] in (0, 1, 2):
            hints["urgency"] = dbus.Byte(formatted["urgency"], variant_level=1)
        if formatted["desktop_entry"]:
            hints["desktop-entry"] = dbus.String(formatted["desktop_entry"], variant_level=1)
        wire_hints = dbus.Dictionary(hints, signature="sv")
        write_event(self.events, "notify-submit", source_id=source_id,
                    replaces_source_id=replaces_source_id, replaces_local_id=replaced_local,
                    app=formatted["app_name"], summary=formatted["summary"],
                    body=formatted["body"], actions=actions)

        def completed(local_id):
            local_id = int(local_id)
            old_local = self.source_to_local.get(source_id)
            if old_local is not None and old_local != local_id:
                self.local_to_source.pop(old_local, None)
            old_source = self.local_to_source.get(local_id)
            if old_source is not None and old_source != source_id:
                self.source_to_local.pop(old_source, None)
            self.source_to_local[source_id] = local_id
            self.local_to_source[local_id] = source_id
            offered = set(actions[::2])
            self.actions_by_source[source_id] = offered
            try:
                self.channel.send(envelope(self.session_id, self.generation, "mapped",
                                           source_id=source_id, local_id=local_id))
            except (BufferError, ConnectionError, OSError, ValueError) as error:
                self._operation_error(message, str(error))
                return
            write_event(self.events, "mapped", source_id=source_id, local_id=local_id,
                        replaces_local_id=replaced_local)
            self.pending_inflight = False
            self._pump()

        self.interface.Notify(
            dbus.String(formatted["app_name"]), dbus.UInt32(replaced_local), dbus.String(""),
            dbus.String(formatted["summary"]), dbus.String(formatted["body"]),
            dbus.Array(actions, signature="s"), wire_hints, dbus.Int32(int(message.get("expires", -1))),
            reply_handler=completed,
            error_handler=lambda error: self._operation_error(message, str(error)),
            timeout=2,
        )
        return GLib.SOURCE_REMOVE

    def _close(self, message):
        source_id = message.get("source_id")
        if not isinstance(source_id, int):
            self._operation_error(message, "invalid source notification ID")
            return
        local_id = self.source_to_local.get(source_id)
        if local_id is None:
            self._operation_error(message, "close arrived before notification mapping")
            return
        write_event(self.events, "close-submit", source_id=source_id, local_id=local_id)

        def completed(*_):
            self.pending_inflight = False
            self._pump()

        self.interface.CloseNotification(
            dbus.UInt32(local_id), reply_handler=completed,
            error_handler=lambda error: self._operation_error(message, str(error)), timeout=2)

    def _operation_error(self, message, reason):
        write_event(self.events, "operation-error", message_type=message.get("type"),
                    source_id=message.get("source_id"), reason=reason)
        self.pending_inflight = False
        self._pump()

    def _action_invoked(self, local_id, key):
        local_id, key = int(local_id), str(key)
        source_id = self.local_to_source.get(local_id)
        if (source_id is None or self.source_to_local.get(source_id) != local_id
                or key not in self.actions_by_source.get(source_id, set())):
            write_event(self.events, "action-rejected", local_id=local_id, key=key)
            return
        try:
            self.channel.send(envelope(self.session_id, self.generation, "action",
                                       source_id=source_id, local_id=local_id, key=key))
        except (BufferError, ConnectionError, OSError, ValueError) as error:
            write_event(self.events, "action-send-failed", source_id=source_id,
                        local_id=local_id, reason=str(error))
            return
        write_event(self.events, "action", source_id=source_id, local_id=local_id, key=key)

    def _notification_closed(self, local_id, reason):
        local_id, reason = int(local_id), int(reason)
        source_id = self.local_to_source.get(local_id)
        if source_id is None or self.source_to_local.get(source_id) != local_id:
            write_event(self.events, "closed-rejected", local_id=local_id, reason=reason)
            return
        try:
            self.channel.send(envelope(self.session_id, self.generation, "closed",
                                       source_id=source_id, local_id=local_id, reason=reason))
        except (BufferError, ConnectionError, OSError, ValueError) as error:
            write_event(self.events, "closed-send-failed", source_id=source_id,
                        local_id=local_id, reason=str(error))
            return
        self.source_to_local.pop(source_id, None)
        self.local_to_source.pop(local_id, None)
        self.actions_by_source.pop(source_id, None)
        write_event(self.events, "closed", source_id=source_id, local_id=local_id,
                    reason=reason)

    def run(self):
        def stop(_signum, _frame):
            self.loop.quit()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.loop.run()
        self.server.close()
        self.bus.close()
        try:
            os.unlink(self.args.socket)
        except FileNotFoundError:
            pass
        return 0


def run_client(args):
    if not args.address.startswith("unix:path=" + PRIVATE_PREFIX):
        raise RuntimeError("destination client only accepts a private study bus")
    if not args.socket.startswith(PRIVATE_PREFIX):
        raise RuntimeError("destination client only accepts a private study socket")
    DBusGMainLoop(set_as_default=True)
    return DestinationClient(args).run()


def run_destination_fixture(args):
    if not args.address.startswith("unix:path=" + PRIVATE_PREFIX):
        raise RuntimeError("destination fixture only accepts a private study bus")
    DBusGMainLoop(set_as_default=True)
    bus = dbus.bus.BusConnection(args.address)
    loop = GLib.MainLoop()
    event_path = Path(args.events)
    try:
        caps = [part for part in args.capabilities.split(",") if part]
    except AttributeError:
        caps = ["body", "actions", "body-markup"]

    class DestinationDaemon(dbus.service.Object):
        def __init__(self):
            super().__init__(bus, OBJECT)
            self.next_id = 100
            self.active = {}

        @dbus.service.method(NAME, in_signature="", out_signature="as")
        def GetCapabilities(self):
            write_event(event_path, "capabilities", capabilities=caps)
            return dbus.Array(caps, signature="s")

        @dbus.service.method(NAME, in_signature="", out_signature="ssss")
        def GetServerInformation(self):
            return ("remote-chrome-destination-fixture", "remote-chrome-study", "0.0", "1.2")

        @dbus.service.method(NAME, in_signature="susssasa{sv}i", out_signature="u")
        def Notify(self, app, replaces_id, icon, summary, body, actions, hints, expires):
            replacement = int(replaces_id)
            local_id = replacement if replacement in self.active else self.next_id
            if local_id == self.next_id:
                self.next_id += 1
            values = [str(value) for value in actions]
            self.active[local_id] = {"actions": values[::2], "summary": str(summary)}
            write_event(event_path, "notify", app=str(app), replaces_id=replacement,
                        local_id=local_id, summary=str(summary), body=str(body),
                        actions=values, expires=int(expires))
            return dbus.UInt32(local_id)

        @dbus.service.method(NAME, in_signature="u", out_signature="")
        def CloseNotification(self, local_id):
            local_id = int(local_id)
            write_event(event_path, "close", local_id=local_id)
            if local_id in self.active:
                self.active.pop(local_id, None)
                self.NotificationClosed(dbus.UInt32(local_id), dbus.UInt32(3))

        @dbus.service.signal(NAME, signature="uu")
        def NotificationClosed(self, local_id, reason):
            pass

        @dbus.service.signal(NAME, signature="us")
        def ActionInvoked(self, local_id, key):
            pass

        @dbus.service.method("org.remotechrome.Study", in_signature="us", out_signature="")
        def TriggerAction(self, local_id, key):
            local_id, key = int(local_id), str(key)
            item = self.active.get(local_id)
            if item is None or key not in item["actions"]:
                write_event(event_path, "fixture-action-rejected", local_id=local_id, key=key)
                return
            write_event(event_path, "fixture-action", local_id=local_id, key=key)
            self.ActionInvoked(dbus.UInt32(local_id), dbus.String(key))

        @dbus.service.method("org.remotechrome.Study", in_signature="u", out_signature="")
        def Dismiss(self, local_id):
            self._close_with_reason(int(local_id), 2, "fixture-dismiss")

        @dbus.service.method("org.remotechrome.Study", in_signature="u", out_signature="")
        def Expire(self, local_id):
            self._close_with_reason(int(local_id), 1, "fixture-expire")

        def _close_with_reason(self, local_id, reason, event):
            if local_id not in self.active:
                return
            self.active.pop(local_id, None)
            write_event(event_path, event, local_id=local_id, reason=reason)
            self.NotificationClosed(dbus.UInt32(local_id), dbus.UInt32(reason))

    daemon = DestinationDaemon()
    ownership = int(bus.request_name(NAME, dbus.bus.NAME_FLAG_DO_NOT_QUEUE))
    if ownership != dbus.bus.REQUEST_NAME_REPLY_PRIMARY_OWNER:
        return 10
    Path(args.ready).write_text(json.dumps({"capabilities": caps,
                                            "unique_name": bus.get_unique_name()}))

    def stop(_signum, _frame):
        loop.quit()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        loop.run()
    finally:
        daemon.remove_from_connection()
        bus.release_name(NAME)
        bus.close()
    return 0


def run_child(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True, choices=("source", "client", "destination"))
    parser.add_argument("--address", required=True)
    parser.add_argument("--socket")
    parser.add_argument("--events", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--session-id", default="study-session")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--capabilities", default="body,actions,body-markup")
    args = parser.parse_args(argv)
    if args.role == "source":
        return run_source(args)
    if args.role == "client":
        return run_client(args)
    return run_destination_fixture(args)


class RoundTripStudy(Study):
    def __init__(self, output_path, delay_ms):
        super().__init__()
        self.output_path = Path(output_path) if output_path else self.root / "results.json"
        self.delay_ms = delay_ms
        self.results.update({
            "prototype": "milestone-0-bidirectional-notification-actions",
            "protocol": {
                "transport": "persistent newline-delimited JSON over private AF_UNIX SOCK_STREAM",
                "version": VERSION,
                "identity": ["session_id", "generation"],
                "source_to_destination": ["notify", "close"],
                "destination_to_source": ["mapped", "action", "closed"],
                "callbacks_include": ["source_id", "local_id", "generation"],
                "queue_limit_messages": MAX_QUEUED_MESSAGES,
                "queue_limit_bytes": MAX_QUEUED_BYTES,
                "active_limit": MAX_ACTIVE,
            },
            "checks": {},
            "manual_visual_acceptance": "not performed; destination action was injected by the private fixture daemon",
        })
        self.destination_address = "unix:path=" + str(self.root / "destination-bus.sock")
        self.destination_socket = str(self.root / "destination-bridge.sock")
        self.source_ready = self.root / "source.ready"
        self.client_ready = self.root / "client.ready"
        self.destination_ready = self.root / "destination.ready"
        self.source_events = self.root / "source-events.jsonl"
        self.client_events = self.root / "client-events.jsonl"
        self.destination_events = self.root / "destination-events.jsonl"
        self.source_env = dict(self.env, DBUS_SESSION_BUS_ADDRESS=self.address)
        self.destination_env = dict(self.env, DBUS_SESSION_BUS_ADDRESS=self.destination_address)
        self.page_server = None
        self.page_thread = None
        self.client = None
        self.destination = None
        self.source = None
        self.chrome_process = None
        self.chrome_browser = None
        self.chrome_page = None
        self.session_id = "study-" + os.urandom(8).hex()
        self.generation = 17

    def _start_bus(self, label, address, env):
        config_path = self.root / (label + "-bus.conf")
        config_path.write_text(
            '<busconfig><type>session</type><listen>' + address + '</listen>'
            '<auth>EXTERNAL</auth><policy context="default">'
            '<allow send_destination="*"/><allow receive_sender="*"/>'
            '<allow own="*"/></policy></busconfig>', encoding="utf-8")
        process = self.spawn(label + "-bus", ["dbus-daemon", "--nofork",
                                                "--config-file=" + str(config_path)], env)
        socket_path = Path(address.split("unix:path=", 1)[1])
        until(lambda: socket_path.exists() or process.poll() is not None)
        if process.poll() is not None:
            raise RuntimeError(label + " private D-Bus daemon exited")

    def _dbus_call(self, address, interface, member, signature="", *values):
        command = ["busctl", "--address=" + address, "--", "call", NAME, OBJECT,
                   interface, member]
        if signature:
            command += [signature, *map(str, values)]
        result = subprocess.run(command, env=self.env, check=False, capture_output=True,
                                text=True, timeout=4)
        if result.returncode != 0:
            raise RuntimeError("private bus call failed: " + " ".join(command)
                               + "\n" + result.stderr.strip())
        return result.stdout.strip()

    def _source_notify(self, summary, body, replaces_id=0, actions=("default", "Open")):
        args = ["Google Chrome", str(replaces_id), "", summary, body,
                str(len(actions)), *actions, "0", "-1"]
        output = self._dbus_call(self.address, NAME, "Notify", "susssasa{sv}i", *args)
        return int(output.split()[-1])

    def _wait_event(self, path, predicate, timeout=8):
        return until(lambda: next((row for row in reversed(records(path)) if predicate(row)), None),
                     timeout=timeout)

    def _check(self, key, value, passed=None):
        self.results["checks"][key] = value
        if passed is not None:
            self.results["checks"][key + "_passed"] = bool(passed)
            if not passed:
                raise AssertionError(key + " did not pass: " + repr(value))
        print(key + "=" + json.dumps(value, ensure_ascii=False), flush=True)

    def _open_chrome(self):
        profile = self.root / "profile-action-roundtrip"
        browser_env = dict(self.env, WAYLAND_DISPLAY="wayland-study",
                           DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(self.root / "proxy.sock"))
        self.chrome_process = self.spawn("chrome-action-roundtrip", [
            "google-chrome-stable", "--ozone-platform=wayland", "--disable-gpu",
            "--disable-features=Vulkan", "--password-store=basic",
            "--user-data-dir=" + str(profile), "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1", "--no-first-run",
            "--no-default-browser-check", "--disable-background-networking",
            "--disable-sync", "--disable-component-update", "--disable-breakpad",
            self.origin,
        ], browser_env)
        until(lambda: (profile / "DevToolsActivePort").exists(), timeout=12)
        port, browser_path = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
        self.chrome_browser = WebSocket(f"ws://127.0.0.1:{port}{browser_path}")
        self.chrome_browser.call("Browser.setPermission", {
            "permission": {"name": "notifications"}, "setting": "granted",
            "origin": self.origin,
        })
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as response:
            targets = json.load(response)
        target = next(item for item in targets if item["type"] == "page")
        self.chrome_page = WebSocket(target["webSocketDebuggerUrl"])
        until(lambda: self.chrome_page.evaluate("document.readyState === 'complete'"))
        return profile

    def run(self):
        print("scratch=" + str(self.root), flush=True)
        self.results["chrome"] = subprocess.check_output(
            ["google-chrome-stable", "--version"], text=True).strip()
        self.results["source_bus"] = self.address
        self.results["destination_bus"] = self.destination_address
        self.results["source_generation"] = self.generation
        self.results["session_id"] = self.session_id
        self.results["formatter"] = "loaded from current bin/remote-chrome _notify-listener definitions"
        self.results["installed_launcher_sha256"] = __import__("hashlib").sha256(
            (REPO / "bin/remote-chrome").read_bytes()).hexdigest()

        self._start_bus("source", self.address, self.source_env)
        self._start_bus("destination", self.destination_address, self.destination_env)
        self.destination = self.spawn("destination-daemon", [sys.executable, str(HERE / "action_roundtrip.py"),
            "--role", "destination", "--address", self.destination_address,
            "--events", str(self.destination_events), "--ready", str(self.destination_ready),
            "--session-id", self.session_id, "--generation", str(self.generation)],
            self.destination_env)
        until(lambda: self.destination_ready.exists() or self.destination.poll() is not None)
        if self.destination.poll() is not None:
            raise RuntimeError("destination notification fixture exited")
        self.client = self.spawn("destination-client", [sys.executable, str(HERE / "action_roundtrip.py"),
            "--role", "client", "--address", self.destination_address,
            "--socket", self.destination_socket, "--events", str(self.client_events),
            "--ready", str(self.client_ready), "--session-id", self.session_id,
            "--generation", str(self.generation), "--delay-ms", str(self.delay_ms)],
            self.destination_env)
        until(lambda: self.client_ready.exists() or self.client.poll() is not None)
        if self.client.poll() is not None:
            raise RuntimeError("destination D-Bus client exited before readiness")
        self.source = self.spawn("source-endpoint", [sys.executable, str(HERE / "action_roundtrip.py"),
            "--role", "source", "--address", self.address, "--socket", self.destination_socket,
            "--events", str(self.source_events), "--ready", str(self.source_ready),
            "--session-id", self.session_id, "--generation", str(self.generation)], self.source_env)
        until(lambda: self.source_ready.exists() or self.source.poll() is not None)
        if self.source.poll() is not None:
            raise RuntimeError("source notification endpoint exited before readiness")
        source_caps = json.loads(self.source_ready.read_text())["capabilities"]
        destination_caps = json.loads(self.client_ready.read_text())["capabilities"]
        self.results["capabilities"] = {
            "destination_probe": destination_caps,
            "source_advertised": source_caps,
            "claims_actions_only_after_direct_client_callback_setup": "actions" in source_caps,
            "not_claimed": ["icon-static", "persistence", "inline-reply", "activation-token"],
        }
        self._check("capabilities_negotiated", self.results["capabilities"],
                    source_caps == ["actions", "body", "body-markup"])
        source_probe = self._dbus_call(self.address, NAME, "GetCapabilities")
        self._check("source_dbus_capabilities", source_probe,
                    all(capability in source_probe for capability in source_caps))

        # The private KWin compositor and xdg-dbus-proxy are isolated inside
        # the temporary XDG runtime/config/data directories from Study.
        self.spawn("kwin", ["kwin_wayland", "--virtual", "--no-lockscreen",
                            "--no-global-shortcuts", "--no-kactivities", "--socket",
                            "wayland-study", "--width", "800", "--height", "600"])
        until(lambda: (self.root / "runtime/wayland-study").exists(), timeout=12)
        self.spawn("proxy", ["xdg-dbus-proxy", self.address, str(self.root / "proxy.sock"),
                             "--filter", "--talk=org.freedesktop.secrets",
                             "--talk=org.freedesktop.Notifications"], self.source_env)
        until(lambda: (self.root / "proxy.sock").exists())
        server = __import__("http.server").server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
        self.page_server = server
        self.page_thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.page_thread.start()
        self.origin = f"http://127.0.0.1:{server.server_port}"
        self._open_chrome()

        self.chrome_page.evaluate(
            "window.studyEvents = [];"
            "window.studyNotification = new Notification('study action roundtrip', "
            "{body: 'Native body & <format> test', requireInteraction: true});"
            "window.studyNotification.onclick = () => window.studyEvents.push('onclick');"
            "window.studyNotification.onclose = () => window.studyEvents.push('onclose');"
            "true")
        actual_source_notify = self._wait_event(
            self.source_events, lambda row: row.get("event") == "notify"
            and row.get("summary") == "study action roundtrip")
        actual_mapped = self._wait_event(
            self.source_events, lambda row: row.get("event") == "mapped"
            and row.get("source_id") == actual_source_notify["source_id"])
        destination_notify = next(row for row in records(self.destination_events)
                                  if row.get("event") == "notify"
                                  and row.get("summary") == "study action roundtrip")
        formatting = {
            "summary": destination_notify["summary"],
            "body": destination_notify["body"],
            "actions": destination_notify["actions"],
            "source_id": actual_mapped["source_id"],
            "local_id": actual_mapped["local_id"],
        }
        self._check("formatter_and_id_mapping", formatting,
                    formatting["source_id"] == actual_source_notify["source_id"]
                    and formatting["local_id"] == destination_notify["local_id"]
                    and formatting["summary"] == "study action roundtrip"
                    and "actions" in source_caps
                    and "default" in formatting["actions"])

        # Action enters through the destination fixture's user-facing signal.
        # The prototype has no source-side TriggerAction hook.
        injected = self._dbus_call(self.destination_address, "org.remotechrome.Study",
                                   "TriggerAction", "us", actual_mapped["local_id"], "default")
        until(lambda: "onclick" in self.chrome_page.evaluate("window.studyEvents"))
        source_action = self._wait_event(
            self.source_events, lambda row: row.get("event") == "action"
            and row.get("source_id") == actual_mapped["source_id"]
            and row.get("local_id") == actual_mapped["local_id"])
        self._check("destination_action_reached_web_notification_onclick", {
            "fixture_call": injected, "local_id": actual_mapped["local_id"],
            "source_id": actual_mapped["source_id"], "key": source_action["key"],
            "web_events": self.chrome_page.evaluate("window.studyEvents"),
            "path": ["destination D-Bus ActionInvoked", "destination client", "private Unix socket",
                     "source ActionInvoked", "Chrome Web Notification onclick"],
        }, source_action["key"] == "default"
            and "onclick" in self.chrome_page.evaluate("window.studyEvents"))

        self.chrome_page.evaluate("window.studyNotification.close(); true")
        close_request = self._wait_event(
            self.source_events, lambda row: row.get("event") == "close-request"
            and row.get("source_id") == actual_mapped["source_id"])
        close_submit = self._wait_event(
            self.client_events, lambda row: row.get("event") == "close-submit"
            and row.get("source_id") == actual_mapped["source_id"])
        browser_close = self._wait_event(
            self.source_events, lambda row: row.get("event") == "closed"
            and row.get("source_id") == actual_mapped["source_id"])
        self._check("browser_close_roundtrip", {
            "source_close_request": close_request,
            "destination_close_request": close_submit,
            "source_closed_callback": browser_close,
            "web_events": self.chrome_page.evaluate("window.studyEvents"),
        }, browser_close["reason"] == 3)

        # Force a bounded destination acknowledgement delay, then queue a
        # replacement before its first local ID is returned.
        first_id = self._source_notify("study replacement one", "Old body")
        second_id = self._source_notify("study replacement two", "New body", first_id)
        self._check("source_replacement_identity", {"first": first_id, "second": second_id},
                    first_id == second_id)
        until(lambda: len([row for row in records(self.destination_events)
                           if row.get("event") == "notify"
                           and row.get("summary") in ("study replacement one",
                                                        "study replacement two")]) == 2)
        self._wait_event(self.source_events, lambda row: row.get("event") == "mapped"
                         and row.get("source_id") == second_id)
        replacement_rows = [row for row in records(self.destination_events)
                            if row.get("event") == "notify"
                            and row.get("summary") in ("study replacement one", "study replacement two")]
        client_timeline = [row for row in records(self.client_events)
                           if row.get("source_id") == second_id
                           and ((row.get("event") == "queue-enqueued"
                                 and row.get("replaces_source_id") == second_id)
                                or row.get("event") == "mapped")]
        replacement_queued_before_first_map = (
            len(client_timeline) >= 2
            and client_timeline[0].get("event") == "queue-enqueued"
            and client_timeline[0].get("replaces_source_id") == second_id
            and client_timeline[1].get("event") == "mapped"
        )
        self._check("replacement_before_first_mapping", {
            "destination_notifications": replacement_rows,
            "destination_queue_timeline": client_timeline,
        },
                    len(replacement_rows) == 2
                    and replacement_rows[0]["local_id"] == replacement_rows[1]["local_id"]
                    and replacement_rows[1]["replaces_id"] == replacement_rows[0]["local_id"]
                    and replacement_queued_before_first_map)

        close_before_map = self._source_notify("study close before map", "Closed promptly")
        self._dbus_call(self.address, NAME, "CloseNotification", "u", close_before_map)
        race_closed = self._wait_event(self.source_events, lambda row: row.get("event") == "closed"
                                       and row.get("source_id") == close_before_map)
        close_race_rows = [row for row in records(self.destination_events)
                           if row.get("event") == "close"
                           and row.get("local_id") == race_closed["local_id"]]
        client_close_timeline = [row for row in records(self.client_events)
                                 if row.get("source_id") == close_before_map
                                 and row.get("event") in ("queue-enqueued", "mapped")]
        source_close_timeline = [row for row in records(self.source_events)
                                 if row.get("source_id") == close_before_map
                                 and row.get("event") in ("notify", "close-request", "mapped")]
        close_queued_before_map = (
            len(client_close_timeline) >= 2
            and client_close_timeline[0].get("event") == "queue-enqueued"
            and client_close_timeline[0].get("message_type") == "notify"
            and client_close_timeline[1].get("event") == "queue-enqueued"
            and client_close_timeline[1].get("message_type") == "close"
            and source_close_timeline.index(next(row for row in source_close_timeline
                                                 if row.get("event") == "close-request"))
            < source_close_timeline.index(next(row for row in source_close_timeline
                                               if row.get("event") == "mapped"))
        )
        self._check("close_before_destination_id_race", {
            "source_id": close_before_map, "closed_callback": race_closed,
            "destination_close": close_race_rows,
            "destination_queue_timeline": client_close_timeline,
            "source_event_timeline": source_close_timeline,
        }, race_closed["reason"] == 3 and bool(close_race_rows) and close_queued_before_map)

        callback_results = {}
        for name, member, reason in (("dismiss", "Dismiss", 2), ("expiry", "Expire", 1)):
            source_id = self._source_notify("study " + name, "callback")
            mapped = self._wait_event(self.source_events, lambda row: row.get("event") == "mapped"
                                      and row.get("source_id") == source_id)
            self._dbus_call(self.destination_address, "org.remotechrome.Study", member,
                            "u", mapped["local_id"])
            callback = self._wait_event(self.source_events, lambda row: row.get("event") == "closed"
                                        and row.get("source_id") == source_id)
            callback_results[name] = {"source_id": source_id, "local_id": mapped["local_id"],
                                      "reason": callback["reason"]}
            if callback["reason"] != reason:
                raise AssertionError(name + " callback returned the wrong close reason")
        self._check("destination_dismissal_and_expiry_callbacks", callback_results,
                    callback_results["dismiss"]["reason"] == 2
                    and callback_results["expiry"]["reason"] == 1)

        self.results["source_events"] = records(self.source_events)
        self.results["client_events"] = records(self.client_events)
        self.results["destination_events"] = records(self.destination_events)
        self.results["visual_acceptance"] = {
            "native_backend_selected": True,
            "actual_web_notification_handler_reached": True,
            "desktop_visible_user_click": False,
            "action_source": "private destination fixture daemon TriggerAction(local_id, key)",
        }

    def cleanup(self):
        if self.chrome_page:
            try:
                self.chrome_page.close()
            except Exception:
                pass
        if self.chrome_browser:
            try:
                self.chrome_browser.close()
            except Exception:
                pass
        if self.page_server:
            self.page_server.shutdown()
            self.page_server.server_close()
        super().cleanup()
        self.results["aggregate_exit_code"] = 0 if not self.results.get("error") else 1
        if self.output_path:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_text(json.dumps(self.results, indent=2, ensure_ascii=False) + "\n",
                                        encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",
                        help="write evidence JSON (default: fresh study scratch/results.json)")
    parser.add_argument("--delay-ms", type=int, default=250,
                        help="fixture-only destination Notify delay for ordering-race checks")
    args = parser.parse_args(argv)
    study = RoundTripStudy(args.output, args.delay_ms)
    error = None
    try:
        study.run()
    except Exception as caught:
        error = caught
        study.results["error"] = repr(caught)
        import traceback
        traceback.print_exc()
    finally:
        study.cleanup()
    if error:
        return 1
    return 0


if __name__ == "__main__":
    if "--role" in sys.argv:
        sys.exit(run_child(sys.argv[1:]))
    sys.exit(main())
