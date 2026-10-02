#!/usr/bin/env python3
"""Explicit real-host acceptance using one disposable managed Chrome session.

Run on the browser host; --display-host and --browser-host are SSH aliases.
Uses the installed launcher and real secure-storage bootstrap. No existing
profile is read or modified. Input commands: notify, events, close, reset, stop.
This is an acceptance tool, not a runtime dependency or CI test.
"""
import argparse
import http.server
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.dont_write_bytecode = True
from study import Page, WebSocket, until


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display-host", required=True)
    parser.add_argument("--browser-host", required=True)
    args = parser.parse_args()
    for host in (args.display_host, args.browser_host):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host):
            parser.error("use a plain SSH host alias")
    root = Path(tempfile.mkdtemp(prefix="remote-chrome-host-acceptance-"))
    session = "remote-chrome-acceptance-" + root.name.rsplit("-", 1)[-1]
    profile = root / "profile"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    evidence = {"root": str(root), "session": session, "origin": origin,
                "display_host": args.display_host, "browser_host": args.browser_host,
                "events": []}
    browser = page = None
    launched = False

    def ssh(command):
        # Use Bash for generated shell syntax; Zsh expands unquoted =targets.
        remote_command = shlex.join(["bash", "-c", command])
        return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
            args.display_host, remote_command], capture_output=True, text=True, timeout=20)

    def emit(event, **fields):
        row = {"time": time.time(), "event": event, **fields}
        evidence["events"].append(row)
        (root / "results.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(row), flush=True)

    def connect():
        nonlocal browser, page
        for client in (browser, page):
            if client:
                client.close()
        until(lambda: (profile / "DevToolsActivePort").exists(), timeout=15)
        port, path = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
        browser = WebSocket(f"ws://127.0.0.1:{port}{path}")
        browser.call("Browser.setPermission", {"permission": {"name": "notifications"},
            "setting": "granted", "origin": origin})
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as stream:
            targets = json.load(stream)
        target = next(item for item in targets if item["type"] == "page" and item["url"].startswith(origin))
        page = WebSocket(target["webSocketDebuggerUrl"])
        until(lambda: page.evaluate("document.readyState === 'complete' && "
            "location.href.startsWith(" + json.dumps(origin + "/") + ") && "
            "document.title === 'Isolated notification study'"))
        page.evaluate("window.acceptanceEvents=[]; document.title='remote-chrome acceptance'; true")

    try:
        remote_env = ssh("systemctl --user show-environment")
        if remote_env.returncode:
            raise RuntimeError(remote_env.stderr)
        selected = [line for line in remote_env.stdout.splitlines()
                    if line.split("=", 1)[0] in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")]
        # Stop/reset recovery state is keyed by SSH target. Refuse a target
        # sharing an existing hardware-forwarding lease, even with --no-yubikey.
        guard = ssh(shlex.join(["env", *selected, "bash", "-c",
            'source "$1"; yk_prepare_for_launch "$2"; '
            'if [ -e "$yk_state_file" ] || [ -e "$yk_attempt_lock" ]; then exit 1; fi; '
            'if tmux has-session -t "=$3" 2>/dev/null; then exit 1; fi; '
            'notify_set_runtime_paths "$3"; '
            'if [ -e "$notify_state_file" ] || [ -e "$notify_local_socket" ]; then exit 1; fi',
            "acceptance", "remote-chrome", args.browser_host, session]))
        if guard.returncode:
            raise RuntimeError("acceptance target/session guard failed; use an unshared SSH address and session: "
                               + guard.stderr.strip())
        command = shlex.join(["env", *selected, "remote-chrome", "launch", args.browser_host,
            "--session", session, "--no-yubikey", "--allow-existing", "--",
            "--user-data-dir=" + str(profile), "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-sync", "--disable-component-update", origin])
        launched = True
        result = ssh(command)
        emit("launch", code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        if result.returncode:
            raise RuntimeError("managed acceptance launch failed")
        connect()
        emit("ready", **{k: evidence[k] for k in ("root", "session", "origin")})
        for line in sys.stdin:
            command = line.strip()
            if command == "notify":
                summary = "remote-chrome acceptance " + session.rsplit("-", 1)[-1]
                page.evaluate("window.acceptanceEvents=window.acceptanceEvents||[]; "
                    "window.acceptanceNotification=new Notification(" + json.dumps(summary) +
                    ", {body:'Click this notification to verify the round trip.', requireInteraction:true, tag:'acceptance'});"
                    "window.acceptanceNotification.onclick=()=>{window.acceptanceEvents.push('onclick');"
                    "document.body.textContent='Notification click reached Chrome';};"
                    "window.acceptanceNotification.onclose=()=>window.acceptanceEvents.push('onclose'); true")
                emit("notification-created", summary=summary)
            elif command == "events":
                emit("web-events", values=page.evaluate("window.acceptanceEvents"))
            elif command == "close":
                page.evaluate("window.acceptanceNotification.close(); true")
                emit("notification-close-requested")
            elif command == "reset":
                # Remove only our stale debug-port file before the controlled restart.
                (profile / "DevToolsActivePort").unlink(missing_ok=True)
                result = ssh(shlex.join(["env", *selected, "remote-chrome", "reset", args.browser_host,
                                        "--session", session, "--yes"]))
                emit("reset", code=result.returncode, stdout=result.stdout, stderr=result.stderr)
                if result.returncode:
                    raise RuntimeError("acceptance reset failed")
                connect()
                emit("reset-ready")
            elif command == "stop":
                break
            else:
                emit("unknown-command", command=command)
    finally:
        cleanup_errors = []
        for client in (page, browser):
            if client:
                try:
                    client.close()
                except Exception as error:
                    cleanup_errors.append("debug connection close: " + str(error))
        try:
            if launched:
                try:
                    present = ssh(shlex.join(["tmux", "has-session", "-t", "=" + session]))
                    ownership = ssh(shlex.join(["tmux", "show-options", "-qv", "-t",
                                                "=" + session + ":", "@remote-chrome-command"]))
                    if present.returncode == 0 and (ownership.returncode or
                            "--user-data-dir=" + str(profile) not in ownership.stdout):
                        cleanup_errors.append("session command changed; refusing unrelated cleanup")
                    elif present.returncode and not (present.returncode == 1 and any(text in present.stderr for text in
                            ("can't find session", "session not found", "no server running", "error connecting"))):
                        cleanup_errors.append("could not verify session ownership for cleanup")
                    else:
                        result = ssh(shlex.join(["remote-chrome", "stop", args.browser_host, "--session", session]))
                        emit("stop", code=result.returncode, stdout=result.stdout, stderr=result.stderr)
                        if result.returncode:
                            cleanup_errors.append("exact session stop failed")
                except subprocess.TimeoutExpired:
                    emit("stop-unavailable", session=session,
                         recovery="run the exact stop command on the display host")
                    cleanup_errors.append("display host cleanup timed out")
        finally:
            server.shutdown()
            server.server_close()
        if cleanup_errors:
            emit("cleanup-failed", errors=cleanup_errors)
            raise RuntimeError("; ".join(cleanup_errors))


if __name__ == "__main__":
    main()
