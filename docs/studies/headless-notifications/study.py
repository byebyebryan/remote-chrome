#!/usr/bin/env python3
"""Isolated, reproducible headless notification feasibility experiment.

Uses a private D-Bus, virtual Wayland compositor, disposable Chrome profiles,
and a capture-only notify-send. Never connects to the live desktop bus.
"""

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
NAME = "org.freedesktop.Notifications"
OBJECT = "/org/freedesktop/Notifications"


def until(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise TimeoutError("study condition did not become true")


def records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


class WebSocket:
    """Minimal stdlib CDP client: local-only, masked text frames, ping handling."""

    def __init__(self, url):
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        if parts.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("CDP endpoint must be local")
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=8)
        nonce = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {nonce}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode())
        response = bytearray()
        while not response.endswith(b"\r\n\r\n"):
            response.extend(self.read(1))
        expected = base64.b64encode(hashlib.sha1(
            (nonce + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if b" 101 " not in response.split(b"\r\n")[0] or expected not in response:
            raise RuntimeError("invalid CDP WebSocket handshake")
        self.sequence = 0

    def read(self, size):
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise EOFError("CDP connection closed")
            data += chunk
        return data

    def send(self, data, opcode=1):
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        if len(data) < 126:
            header += bytes([0x80 | len(data)])
        elif len(data) < 65536:
            header += b"\xfe" + struct.pack("!H", len(data))
        else:
            header += b"\xff" + struct.pack("!Q", len(data))
        self.sock.sendall(header + mask + bytes(value ^ mask[i % 4]
                                               for i, value in enumerate(data)))

    def receive(self):
        chunks = []
        while True:
            first, second = self.read(2)
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self.read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.read(8))[0]
            mask = self.read(4) if second & 128 else None
            data = self.read(size)
            if mask:
                data = bytes(value ^ mask[i % 4] for i, value in enumerate(data))
            opcode = first & 15
            if opcode == 8:
                raise EOFError("CDP WebSocket closed")
            if opcode == 9:
                self.send(data, 10)
                continue
            if opcode == 10:
                continue
            chunks.append(data)
            if first & 128:
                return json.loads(b"".join(chunks))

    def call(self, method, params=None):
        self.sequence += 1
        self.send(json.dumps({"id": self.sequence, "method": method,
                              "params": params or {}}).encode())
        while True:
            reply = self.receive()
            if reply.get("id") == self.sequence:
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression,
                                                "returnByValue": True})
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def close(self):
        self.sock.close()


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        content = b"<!doctype html><title>Isolated notification study</title>Study"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_):
        pass


class Study:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="remote-chrome-notification-study-"))
        self.processes = []
        self.handles = []
        self.results = {"scratch": str(self.root), "checks": {}, "chrome": None}
        self.env = dict(os.environ)
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
                    "DBUS_STARTER_ADDRESS", "DBUS_STARTER_BUS_TYPE", "SESSION_MANAGER"):
            self.env.pop(key, None)
        for name in ("runtime", "config", "cache", "data"):
            (self.root / name).mkdir(mode=0o700)
        self.address = "unix:path=" + str(self.root / "bus.sock")
        self.env.update(
            XDG_RUNTIME_DIR=str(self.root / "runtime"),
            XDG_CONFIG_HOME=str(self.root / "config"),
            XDG_CACHE_HOME=str(self.root / "cache"),
            XDG_DATA_HOME=str(self.root / "data"),
            DBUS_SESSION_BUS_ADDRESS=self.address,
            XDG_CURRENT_DESKTOP="KDE", XDG_SESSION_TYPE="wayland",
            QT_QPA_PLATFORM="offscreen", LIBGL_ALWAYS_SOFTWARE="1",
            QSG_RHI_BACKEND="software",
        )

    def spawn(self, label, argv, env=None):
        handle = (self.root / (label + ".log")).open("w")
        self.handles.append(handle)
        process = subprocess.Popen(argv, env=env or self.env, stdin=subprocess.DEVNULL,
                                   stdout=handle, stderr=handle, start_new_session=True)
        self.processes.append(process)
        return process

    def stop(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)

    def check(self, name, value):
        self.results["checks"][name] = value
        print(name + "=" + json.dumps(value), flush=True)

    def bus(self, method, signature="", *arguments):
        command = ["busctl", "--address=" + self.address, "--json=short", "call",
                   "org.freedesktop.DBus", "/org/freedesktop/DBus",
                   "org.freedesktop.DBus", method]
        if signature:
            command += [signature, *map(str, arguments)]
        reply = subprocess.run(command, env=self.env, check=True, capture_output=True,
                               text=True, timeout=3)
        return json.loads(reply.stdout)["data"][0]

    def endpoint(self, label, caps="body,actions,body-markup"):
        ready = self.root / (label + ".ready")
        events = self.root / (label + ".jsonl")
        process = self.spawn(label, ["python3", str(HERE / "endpoint.py"),
                                    "--address", self.address, "--events", str(events),
                                    "--ready", str(ready), "--capabilities", caps])
        until(lambda: ready.exists() or process.poll() is not None)
        return process, events, ready

    def chrome(self, label, title, expect_native, after_first=None):
        profile = self.root / ("profile-" + label)
        env = dict(self.env, WAYLAND_DISPLAY="wayland-study",
                   DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(self.root / "proxy.sock"))
        process = self.spawn("chrome-" + label, [
            "google-chrome-stable", "--ozone-platform=wayland", "--disable-gpu",
            "--disable-features=Vulkan", "--password-store=basic",
            "--user-data-dir=" + str(profile), "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1", "--no-first-run",
            "--no-default-browser-check", "--disable-background-networking",
            "--disable-sync", "--disable-component-update", "--disable-breakpad",
            self.origin,
        ], env)
        browser = page = None
        try:
            until(lambda: (profile / "DevToolsActivePort").exists(), timeout=12)
            port, browser_path = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
            browser = WebSocket(f"ws://127.0.0.1:{port}{browser_path}")
            browser.call("Browser.setPermission", {
                "permission": {"name": "notifications"}, "setting": "granted",
                "origin": self.origin,
            })
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as response:
                targets = json.load(response)
            target = next(item for item in targets if item["type"] == "page")
            page = WebSocket(target["webSocketDebuggerUrl"])
            until(lambda: page.evaluate("document.readyState === 'complete'"))
            self.check(label + "_permission", page.evaluate("Notification.permission"))
            page.evaluate("window.studyEvents = []; window.studyNotification = "
                          f"new Notification({json.dumps(title)}, "
                          "{body: 'Native body & <format> test'}); "
                          "for (const name of ['show', 'click', 'close', 'error']) "
                          "window.studyNotification.addEventListener(name, "
                          "() => window.studyEvents.push(name)); true")
            # Native selection is asynchronous; stop after a bounded observation.
            time.sleep(2)
            self.check(label + "_web_events", page.evaluate("window.studyEvents"))
            self.check(label + "_native_calls", self.notifications(title))
            if expect_native:
                event = until(lambda: self.notifications(title))[-1]
                action = subprocess.run([
                    "busctl", "--address=" + self.address, "call", NAME, OBJECT,
                    "org.remotechrome.Study", "TriggerAction", "us", str(event["id"]), "default"
                ], env=self.env, capture_output=True, text=True, timeout=3)
                self.check(label + "_action_rpc_status", action.returncode)
                until(lambda: "click" in page.evaluate("window.studyEvents"))
                self.check(label + "_action_reached_chrome", True)
                page.evaluate("window.studyNotification.close(); true")
                until(lambda: any(row["event"] == "close" for row in records(self.events)))
                self.check(label + "_close_reached_endpoint", True)
            if after_first:
                after_first(page)
            return page.evaluate("window.studyEvents")
        finally:
            if page:
                page.close()
            if browser:
                browser.close()
            self.stop(process)

    def notifications(self, title):
        return [row for row in records(self.events)
                if row["event"] == "notify" and row["summary"] == title]

    def run(self):
        print("scratch=" + str(self.root), flush=True)
        self.results["chrome"] = subprocess.check_output(
            ["google-chrome-stable", "--version"], text=True).strip()
        self.results["launcher_sha256"] = hashlib.sha256(
            (REPO / "bin/remote-chrome").read_bytes()).hexdigest()
        config = self.root / "bus.conf"
        config.write_text(
            '<busconfig><type>session</type><listen>' + self.address + '</listen>'
            '<auth>EXTERNAL</auth><policy context="default">'
            '<allow send_destination="*"/><allow receive_sender="*"/>'
            '<allow own="*"/><allow eavesdrop="true"/></policy></busconfig>')
        self.spawn("bus", ["dbus-daemon", "--nofork", "--config-file=" + str(config)])
        until(lambda: (self.root / "bus.sock").exists())
        self.spawn("kwin", ["kwin_wayland", "--virtual", "--no-lockscreen",
                            "--no-global-shortcuts", "--no-kactivities",
                            "--socket", "wayland-study", "--width", "800", "--height", "600"])
        until(lambda: (self.root / "runtime/wayland-study").exists(), timeout=12)
        self.spawn("proxy", ["xdg-dbus-proxy", self.address, str(self.root / "proxy.sock"),
                             "--filter", "--talk=org.freedesktop.secrets",
                             "--talk=org.freedesktop.Notifications"])
        until(lambda: (self.root / "proxy.sock").exists())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{server.server_port}"
        try:
            # Existing installed formatter, capture-only desktop command.
            fakebin = self.root / "capture-bin"
            fakebin.mkdir()
            (fakebin / "busctl").write_text('#!/usr/bin/env python3\n'
                'print(\'{"type":"as","data":[["body","body-markup","actions"]]}\')\n')
            (fakebin / "notify-send").write_text('#!/usr/bin/env python3\n'
                'import json, os, sys\n'
                'with open(os.environ["STUDY_CAPTURE"], "a") as handle:\n'
                '    handle.write(json.dumps(sys.argv[1:]) + "\\n")\n')
            for path in fakebin.iterdir():
                path.chmod(0o700)
            capture = self.root / "deliveries.jsonl"
            listener_env = dict(self.env, PATH=str(fakebin) + ":" + os.environ["PATH"],
                                STUDY_CAPTURE=str(capture))
            self.spawn("listener", [str(REPO / "bin/remote-chrome"), "_notify-listener",
                                    str(self.root / "relay.sock"), str(self.root / "listener-events.log")],
                       listener_env)
            until(lambda: (self.root / "relay.sock").exists())
            source = (REPO / "bin/remote-chrome").read_text()
            forwarder = source.split("<<'REMOTE_CHROME_NOTIFY_FORWARDER'\n", 1)[1].split(
                "\nREMOTE_CHROME_NOTIFY_FORWARDER", 1)[0]
            (self.root / "forwarder.py").write_text(forwarder)
            self.spawn("forwarder", ["python3", str(self.root / "forwarder.py"),
                                     str(self.root / "relay.sock"), "chrome"])
            time.sleep(0.3)

            endpoint, self.events, _ = self.endpoint("body-only", "body,body-markup")
            self.chrome("body_only", "study body only", False)
            self.check("body_only_capabilities_queried", any(
                row["event"] == "capabilities" for row in records(self.events)))
            self.stop(endpoint)

            # Does starting an endpoint repair a browser that already chose fallback?
            self.events = self.root / "no-owner.jsonl"

            def late_endpoint(page):
                late, self.events, _ = self.endpoint("late-endpoint")
                try:
                    page.evaluate('window.lateNotification = new Notification("study late endpoint", '
                                  '{body: "Endpoint appeared after first notification"}); true')
                    time.sleep(2)
                    self.check("late_endpoint_native_calls", self.notifications("study late endpoint"))
                    self.check("late_endpoint_protocol_queries", records(self.events))
                finally:
                    self.stop(late)

            self.chrome("no_owner", "study no owner", False, late_endpoint)

            endpoint, self.events, ready = self.endpoint("full")
            owner = self.bus("GetNameOwner", "s", NAME)
            self.chrome("full", "study native notification", True)
            until(lambda: capture.exists() and len(records(capture)) > 0)
            self.check("existing_relay_native_delivery", records(capture))

            # Native Chrome escapes webpage text; separately test protocol markup.
            reply = subprocess.run([
                "busctl", "--address=" + self.address, "--", "call", NAME, OBJECT, NAME,
                "Notify", "susssasa{sv}i", "Google Chrome", "0", "", "study markup",
                "<b>Bold</b> <i>Italic</i> <u>Underline</u><br/>A &amp; B", "0", "0", "-1"
            ], env=self.env, capture_output=True, text=True, timeout=3)
            self.check("markup_fixture_rpc_status", reply.returncode)
            until(lambda: len(records(capture)) >= 2)
            self.check("existing_relay_markup_delivery", records(capture)[-1])

            contender, contender_events, _ = self.endpoint("contender")
            contender.wait(timeout=3)
            self.check("second_endpoint_refused", {
                "status": contender.returncode, "events": records(contender_events),
                "owner_unchanged": self.bus("GetNameOwner", "s", NAME) == owner})

            qml = self.root / "shell.qml"
            qml.write_text('import Quickshell\nimport Quickshell.Services.Notifications\n'
                           'Scope { NotificationServer { bodySupported: true; '
                           'bodyMarkupSupported: true; actionsSupported: true; '
                           'onNotification: notification => { notification.tracked = true; } } }\n')
            desktop = self.spawn("quickshell", ["quickshell", "--path", str(qml)])
            time.sleep(1)
            if desktop.poll() is not None:
                raise RuntimeError("isolated Quickshell did not start")
            self.check("quickshell_cannot_preempt_fallback", self.bus("GetNameOwner", "s", NAME) == owner)
            started = time.monotonic()
            self.stop(endpoint)
            until(lambda: self.bus("NameHasOwner", "s", NAME))
            desktop_owner = self.bus("GetNameOwner", "s", NAME)
            self.check("quickshell_acquired_after_fallback_exit", {
                "new_owner": desktop_owner, "seconds": round(time.monotonic() - started, 3),
                "owner_changed": desktop_owner != owner})
            contender, contender_events, _ = self.endpoint("desktop-contender")
            contender.wait(timeout=3)
            self.check("existing_desktop_preserved", {
                "status": contender.returncode, "owner_unchanged":
                self.bus("GetNameOwner", "s", NAME) == desktop_owner})
            self.stop(desktop)
            self.check("refused_endpoint_was_not_queued", not self.bus("NameHasOwner", "s", NAME))
            self.results["endpoint_events"] = records(self.events)

            # Keep a native-enabled browser running across fallback -> desktop ownership.
            transition, self.events, _ = self.endpoint("transition")

            def handoff_running_chrome(page):
                desktop = self.spawn("quickshell-transition", ["quickshell", "--path", str(qml)])
                time.sleep(0.5)
                if desktop.poll() is not None:
                    raise RuntimeError("handoff Quickshell did not start")
                self.stop(transition)
                until(lambda: self.bus("NameHasOwner", "s", NAME))
                page.evaluate('window.handoffNotification = new Notification("study desktop handoff", '
                              '{body: "Same browser after ownership changed"}); true')
                found = until(lambda: [args for args in records(capture)
                                       if "study desktop handoff" in args])
                self.check("running_chrome_survived_desktop_handoff", found)
                self.stop(desktop)

            self.chrome("handoff", "study handoff initial", True, handoff_running_chrome)
        finally:
            server.shutdown()
            server.server_close()

    def cleanup(self):
        for process in reversed(self.processes):
            self.stop(process)
        for handle in self.handles:
            handle.close()
        self.results["owned_processes_exited"] = all(p.poll() is not None for p in self.processes)
        (self.root / "results.json").write_text(json.dumps(self.results, indent=2) + "\n")


if __name__ == "__main__":
    study = Study()
    try:
        study.run()
    except Exception as error:
        study.results["error"] = repr(error)
        raise
    finally:
        study.cleanup()
