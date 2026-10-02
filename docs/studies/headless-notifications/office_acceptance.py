#!/usr/bin/env python3
"""Test office handoff on the real user bus without stopping other sessions.

Run on the browser host with the disposable host_acceptance.py session name.
The installed bare stop command sees only an owned copy of that session's
incoming record. Quickshell runs on a disposable virtual display, but owns the
normal user notification bus after the managed helper releases it.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


NAME = "org.freedesktop.Notifications"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session")
    args = parser.parse_args()
    if not args.session.startswith("remote-chrome-acceptance-"):
        parser.error("use a disposable host-acceptance session")
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "unix:path=" + str(runtime / "bus"))
    root = Path(tempfile.mkdtemp(prefix="remote-chrome-office-acceptance-"))
    private_runtime = root / "runtime"
    private_runtime.mkdir(mode=0o700)
    env = {**os.environ, "XDG_RUNTIME_DIR": str(private_runtime),
           "DBUS_SESSION_BUS_ADDRESS": address, "WAYLAND_DISPLAY": "wayland-acceptance"}
    processes = []
    streams = []
    evidence = {"root": str(root), "session": args.session, "checks": []}

    def run(command, **kwargs):
        return subprocess.run(command, capture_output=True, text=True, timeout=15, **kwargs)

    def bus(method, signature, *values):
        result = run(["busctl", "--user", "--json=short", "call", "org.freedesktop.DBus",
                      "/org/freedesktop/DBus", "org.freedesktop.DBus", method, signature, *values])
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)["data"][0]

    def check(name, value):
        evidence["checks"].append({"name": name, "value": value})
        (root / "results.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence["checks"][-1]), flush=True)

    def wait(predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise RuntimeError("acceptance wait timed out")

    def spawn(label, command):
        stream = (root / (label + ".log")).open("w")
        streams.append(stream)
        process = subprocess.Popen(command, env=env, stdout=stream, stderr=stream, start_new_session=True)
        processes.append(process)
        return process

    try:
        records = []
        for path in runtime.glob("remote-chrome-incoming-*.state"):
            if path.is_symlink() or path.stat().st_uid != os.getuid():
                continue
            fields = dict(line.split("\t", 1) for line in path.read_text().splitlines())
            if fields.get("session") == args.session:
                records.append((path, fields))
        if len(records) != 1:
            raise RuntimeError("expected one exact incoming acceptance record")
        state, fields = records[0]
        bootstrap_pid = fields["pid"]
        if not bootstrap_pid.isdigit():
            raise RuntimeError("invalid incoming bootstrap identity")
        owned_artifacts = [runtime / name for name in (
            "remote-chrome-notify-status-" + bootstrap_pid + ".state",
            "remote-chrome-notify-forwarder-" + bootstrap_pid + ".py",
            "remote-chrome-notify-helper-" + bootstrap_pid + ".log",
            "remote-chrome-dbus-proxy-" + bootstrap_pid + ".sock")]
        owner = bus("GetNameOwner", "s", NAME)
        owner_pid = bus("GetConnectionUnixProcessID", "s", owner)
        if fields.get("notify_backend") != "helper" or str(owner_pid) != fields.get("notify_pid"):
            raise RuntimeError("normal bus is not owned by the selected test helper")
        guard = run(["bash", "-c", 'source "$1"; chrome_managed_sessions', "acceptance", "remote-chrome"])
        if guard.returncode or guard.stdout.strip():
            raise RuntimeError("browser host has outgoing managed sessions; bare stop would affect them")
        copy = private_runtime / state.name
        copy.write_text(state.read_text())
        copy.chmod(0o600)
        check("selected_helper", {"pid": owner_pid, "owner": owner})
        spawn("compositor", ["dbus-run-session", "--", "kwin_wayland", "--virtual",
            "--no-lockscreen", "--no-global-shortcuts", "--no-kactivities", "--socket",
            "wayland-acceptance", "--width", "800", "--height", "600"])
        wait(lambda: (private_runtime / "wayland-acceptance").exists(), timeout=12)
        qml = root / "shell.qml"
        qml.write_text('import Quickshell\nimport Quickshell.Services.Notifications\n'
            'Scope { NotificationServer { bodySupported: true; bodyMarkupSupported: true; '
            'actionsSupported: true; onNotification: notification => { '
            'notification.tracked = true; console.log("office-delivery:" + notification.summary); } } }\n')
        desktop = spawn("quickshell", ["quickshell", "--path", str(qml)])
        time.sleep(0.5)
        if desktop.poll() is not None:
            raise RuntimeError("Quickshell exited before handoff")
        if bus("GetNameOwner", "s", NAME) != owner:
            raise RuntimeError("Quickshell unexpectedly replaced the active helper")
        check("desktop_waits_for_release", True)
        start = time.monotonic()
        result = run(["remote-chrome", "stop"], env=env)
        check("bare_stop", {"code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                            "seconds": round(time.monotonic() - start, 3)})
        if result.returncode:
            raise RuntimeError("isolated incoming stop failed")
        wait(lambda: bus("NameHasOwner", "s", NAME) and bus("GetNameOwner", "s", NAME) != owner)
        new_owner = bus("GetNameOwner", "s", NAME)
        if bus("GetConnectionUnixProcessID", "s", new_owner) != desktop.pid:
            raise RuntimeError("unexpected daemon acquired the normal bus")
        check("quickshell_acquired_normal_bus", {"owner": new_owner, "pid": desktop.pid})
        marker = "remote-chrome office acceptance " + root.name.rsplit("-", 1)[-1]
        result = run(["notify-send", "--app-name=remote-chrome acceptance", marker, "Ordinary local notification"])
        if result.returncode:
            raise RuntimeError(result.stderr)
        wait(lambda: "office-delivery:" + marker in (root / "quickshell.log").read_text())
        check("ordinary_local_notification", True)
        wait(lambda: not state.exists() and not copy.exists())
        check("incoming_record_removed", True)
        wait(lambda: all(not path.exists() for path in owned_artifacts))
        check("owned_source_artifacts_removed", True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
        for stream in streams:
            stream.close()
        released = not bus("NameHasOwner", "s", NAME)
        check("test_desktop_released_normal_bus", released)
        if processes and not released:
            raise RuntimeError("test desktop did not release the normal notification name")


if __name__ == "__main__":
    main()
