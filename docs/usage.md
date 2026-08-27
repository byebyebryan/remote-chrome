# Usage

Typical browser session:

```bash
remote-chrome remote-host
```

Successful detached launches print the exact commands for attaching, checking
status, and stopping the resulting session. They can also be run directly:

```bash
remote-chrome attach remote-host
remote-chrome status remote-host
remote-chrome doctor remote-host
remote-chrome stop remote-host
```

`attach` attaches normally outside tmux and switches the current client when
it is already running inside tmux.

Repeating the bare `remote-chrome HOST` command resets the existing managed
session for that host. It does not attempt to judge whether an old Waypipe
stream is healthy after suspend; the repeated command is the deliberate request
to recreate it. `launch` is the fresh-session form and refuses an existing tmux
session. If remote Chrome is already running, you are asked before it is killed.
Confirm to kill it, or use
`--allow-existing` to launch anyway:

```bash
remote-chrome launch remote-host --allow-existing
```

Use `reset HOST` to force the same managed restart explicitly. `reset` without
a host selects the sole default managed session and refuses when several exist.
It preserves the recorded canonical Waypipe command and, when present, restores
managed YubiKey forwarding; pre-1.3 sessions may fall back to a direct pane
command, while wrapped pane metadata is never decoded. It does not transparently
resume an old Waypipe stream.

By default, the launcher detects a local YubiKey matching the configured USB
vendor/product (default `1050:0407`). If one is present, forwarding is expected;
the local and remote USB/IP module/sudo preflight must pass before tmux or Chrome
starts. If no key is present, Chrome remains a normal Chrome-only session and
does not require the USB/IP tools.

When the remote browser asks for a security key, you can force forwarding (and
fail loudly if the key or prerequisites are unavailable):

```bash
remote-chrome remote-host --with-yubikey
```

To explicitly bypass detection and forwarding, use:

```bash
remote-chrome remote-host --no-yubikey
```

Forwarding starts in a `yubikey` tmux window first. Bootstrap has a separate
bounded timeout (30 seconds by default), then the launcher waits up to 15
seconds after attach until the exact configured device enumerates as a FIDO/hidraw device,
then creates the `chrome` window. Preferred readiness is verified by
`fido2-token -L`; if only udev/hidraw metadata is available, output labels the
result USB-only. Both paths require the configured vendor/product and
FIDO/security-token metadata, so an OTP keyboard hidraw interface or unrelated
FIDO key cannot satisfy readiness. A child failure or timeout prints its pane/log diagnostics,
removes the failed session, and rolls back the exact resources acquired.

Closing the remote Chrome browser only exits the `chrome` window; the `yubikey`
window and forwarding keep running until you stop the whole session:

```bash
remote-chrome stop remote-host
```

`stop` is the explicit teardown that kills the session and tears down forwarding
together. It also cleans provisional state left by an interrupted setup.
Run `remote-chrome stop` with no host to stop every default managed tmux session
(`remote-chrome-*` by default) and every YubiKey forwarding using the default
runtime state path. It leaves an exact-name `remote-chrome` session and
unrelated tmux sessions untouched; use `stop HOST` for a custom `--session`
name or YubiKey control socket.
Remote forwarding requires passwordless scoped sudo for `modprobe vhci-hcd`,
`usbip attach`, `usbip port`, and `usbip detach`; some hosts expose USB/IP port
records only to root. The local and remote `timeout` command (from coreutils)
is also required when forwarding, so every remote USB/IP operation remains
bounded; Chrome-only launches do not require the USB/IP or timeout prerequisites.

The child forwarding process waits up to 15 seconds after attach by default.
Detached mode gives the parent an additional 5-second readiness grace period
for the final state write and an in-flight SSH probe before declaring failure.

`status remote-host` reports session windows plus managed YubiKey phase and
readiness, if state exists. It also reports a provisional setup lock before the
first state write. Run `remote-chrome status` without a host for a read-only
overview of all default managed tmux sessions, forwarding state files,
provisional locks, and standard-runtime orphan `usbipd` PID files.

`status remote-host` performs read-only checks for current tmux windows,
recorded USB/IP ownership, the owned `usbipd` process, SSH control socket, and
remote YubiKey readiness. It returns nonzero with actionable output when the
session is stale or degraded. `status` without a host remains the managed-state
overview. `doctor remote-host` checks local display and
commands, detached SSH plus remote Waypipe/Chrome and secure-session
dependencies, and USB/IP module prerequisites without loading modules, looking
up a wallet secret, unlocking a wallet, or changing USB/IP state.

## Secure session and GTK chooser

Launches send an embedded bootstrap through the direct `waypipe --no-gpu ssh
HOST` pane command. The bootstrap connects `xdg-dbus-proxy` to the normal remote
user session bus (`DBUS_SESSION_BUS_ADDRESS=/run/user/1000/bus`, normalized to
`unix:path=`) and enables only `--talk=org.freedesktop.secrets` under
`--filter`. `org.freedesktop.portal.Desktop` is not visible through that proxy,
so Chromium's GTK file chooser travels over Waypipe instead of using a remote
desktop portal.

The bootstrap first reuses an existing `org.freedesktop.secrets` owner. If none
exists, it starts `ksecretd` inside the Waypipe environment and records that
exact process for cleanup. It then runs the Safe Storage preflight with output
discarded:

```text
secret-tool lookup application chrome xdg:schema chrome_libsecret_os_crypt_password_v2
```

Only a successful lookup allows Chrome to start. A missing key/service, a
canceled KWallet prompt, or proxy/startup failure aborts before Chrome; the
launcher never silently enables Chrome's basic/password-file fallback. It
always supplies `--password-store=gnome-libsecret` and rejects user-supplied
`--password-store` arguments. Proxy sockets and owned `ksecretd` are cleaned on
success, failure, `INT`, `TERM`, and `HUP`; a pre-existing Secret Service is
never killed. A nonzero Chrome status is preserved; a successful Chrome exit
becomes nonzero if exact owned cleanup remains unresolved.

The first launch after a cold boot may prompt for KWallet unlock. Canceling the
prompt is a secure failure; unlock the wallet and retry. Unattended reboot also
requires a pre-login network connection so SSH can reach the remote host;
the current working assumption is that wired networking provides this path, but
unattended reboot reachability has not been reboot-tested and remains a risk.
Wi-Fi/NetworkManager remediation is deferred.

Google Chrome uses `application=chrome` with
`xdg:schema=chrome_libsecret_os_crypt_password_v2`. Chromium maps to
`application=chromium` and
`xdg:schema=chromium_libsecret_os_crypt_password_v2`. These Chromium values are
the conventional libsecret mapping and can be overridden if an existing
profile uses different metadata. For an unknown custom executable, set both
values explicitly or the launcher fails with guidance:

```bash
export REMOTE_CHROME_SECRET_APPLICATION=my-browser
export REMOTE_CHROME_SECRET_SCHEMA=my_browser_libsecret_os_crypt_password_v2
remote-chrome launch remote-host --chrome-command /opt/my-browser
```

Remote secure-session requirements are `bash`, `waypipe`, `xdg-dbus-proxy`,
`secret-tool`, `ksecretd`, `busctl`, and the selected Chrome executable.

Cleanup attempts every applicable resource in order. If remote detach, tunnel
close, local unbind, or owned-daemon cleanup remains unresolved, `stop` returns
nonzero and keeps the recovery state/log so a later `stop HOST` can retry. A
daemon retained by `REMOTE_CHROME_STOP_USBIPD=0` or other USB/IP exports is
reported and left for safe hostless reconciliation; pre-existing or
unverified daemons are never stopped.

For one-off sessions without tmux:

```bash
remote-chrome launch remote-host --foreground
```

Foreground mode follows the same preflight and readiness wait, and Ctrl-C/TERM/
HUP cleanup detaches remotely, closes the owned tunnel, unbinds only the
recorded bus ID, and stops only a tool-owned `usbipd`.
