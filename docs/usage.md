# Usage and troubleshooting

The display host runs the launcher and supplies the desktop/key; the browser
host runs Chrome. For `remote-chrome snap` on Starship, these are Starship and
Snap respectively. [Host setup](../README.md#requirements),
[configuration](configuration.md), and [architecture](architecture.md) cover
prerequisites, defaults, and implementation contracts.

## Launch and inspect

```bash
remote-chrome remote-host
remote-chrome attach remote-host
remote-chrome status remote-host
remote-chrome doctor remote-host
remote-chrome stop remote-host
```

Detached tmux mode is the default. Successful launches print commands with the
exact host and session name. The default name is `remote-chrome-HOST`, with
awkward characters replaced by underscores: `remote.example` becomes
`remote-chrome-remote_example`. `attach` switches the current client inside
tmux and attaches normally outside it.

The bare command starts a new session if none exists. If the selected managed
session exists, it resets that session without asking for restart confirmation.
This is a deliberate browser restart; save unsaved in-page work.

Use explicit `launch` for a fresh session and to select new settings:

```bash
remote-chrome launch remote-host --no-yubikey
remote-chrome launch remote-host --session work-browser -- --profile-directory=Default
```

`launch` refuses an existing tmux session. A repeated bare `HOST` command keeps
the recorded browser arguments, notification configuration, and YubiKey mode;
new launch flags or browser/notification defaults do not replace those settings.
Runtime timeout and path settings still govern the invocation. Stop the
selected session and launch again to change its launch settings:

```bash
remote-chrome stop remote-host --session work-browser
remote-chrome launch remote-host --session work-browser --no-notifications
```

Chrome has one instance per user-data directory. A new invocation can delegate
to the browser already running on the remote desktop, even with `--new-window`.
The launcher asks before killing matching existing browser processes. Use
`--yes` only after saving their work. Use `--allow-existing` only when those
processes use a different `--user-data-dir`:

```bash
remote-chrome launch remote-host --allow-existing -- --user-data-dir=/path/to/separate-profile
```

Pass browser arguments after `--`. `--password-store` overrides are rejected;
the secure bootstrap supplies `--password-store=gnome-libsecret` after a
successful Safe Storage lookup.

For a foreground session:

```bash
remote-chrome launch remote-host --foreground --with-yubikey
```

Foreground mode uses the same secure bootstrap and forwarding readiness checks.
Browser exit, Ctrl-C, TERM, and HUP clean its owned resources. In detached mode,
closing Chrome can leave the `yubikey` window and forwarding running; use `stop`
for the whole session.

## Reset

```bash
remote-chrome reset remote-host
remote-chrome reset remote-host --session work-browser
```

Reset restarts the exact managed Chrome/Waypipe stream and any recorded YubiKey
forwarding. The normal `chrome` and `yubikey` windows can both be present. Reset
without a host selects the sole session under the configured default prefix;
when several exist, specify a host or `--session NAME`.

Reset preserves the canonical launch command and YubiKey mode: automatic,
required, or disabled. Forwarding state supplies the USB ID and port. Older
sessions without mode metadata use automatic detection; pre-1.3 sessions may
fall back to a direct pane command. Wrapped pane metadata is never decoded.

The launcher validates the selected pane, SSH reverse socket, remote process
group, and runtime artifacts before stopping Chrome. It refuses ambiguous or
unreachable ownership. `--yes` skips restart or confirmed-absence warnings,
while ownership checks still apply. Reset recreates the stream rather than
resuming it transparently.

After exact teardown, reset reruns forwarding preflight before starting the
replacement listener and forwarding session. A failed preflight can therefore
leave the old browser stopped; inspect diagnostics and retry after fixing the
prerequisite. The replacement notification listener becomes ready before the
new Chrome pane starts. Failed or interrupted listener startup rolls back its
exact process/socket, including interruption before state publication.

For stale forwarding from Starship to Snap, run `remote-chrome reset snap` on
Starship. Updating the installed launcher does not update an active bootstrap;
update both hosts before a deliberate reset activates new behavior.

## YubiKey forwarding and hub recovery

Automatic detection looks for the configured USB ID (`1050:0407` by default).
A detected key makes forwarding a prerequisite; no detected key permits a
Chrome-only launch without USB/IP tools. Select the mode explicitly for a fresh
session:

```bash
remote-chrome launch remote-host --with-yubikey
remote-chrome launch remote-host --no-yubikey
```

Before tmux or Chrome starts, forwarding preflight checks commands, scoped sudo,
running kernels/module trees, `usbip-host`, and `vhci-hcd`. After a kernel
upgrade, a missing module tree usually requires rebooting that host; the
launcher never installs modules or reboots it.

Detached setup starts in the `yubikey` window. The parent allows a separate
bootstrap phase (30 seconds by default), then readiness after attach (15
seconds), plus a small final grace period (5 seconds). These are separate
limits; 15 seconds is not the deadline for the whole launch. See
[timeouts](configuration.md#timeouts-and-polling).

Preferred readiness uses `fido2-token -L` as the browser user. If unavailable,
the matching accessible udev/hidraw device provides a lower-confidence USB-only
result. Both require the configured USB ID, FIDO metadata, and read/write
access; an OTP keyboard interface or unrelated key cannot satisfy readiness.
A failed child or timeout prints pane/log diagnostics and rolls back the
resources acquired by that attempt.

For headless access and stale reverse listeners after a display-host reboot,
follow [the host setup instructions](../README.md#yubikey-forwarding). While
forwarded, the key belongs to the browser host and may be unavailable locally.

If a display-powered USB hub goes dark, the helper waits for the key to return
to the recorded physical USB port with the configured USB ID. It checks every
five seconds, validates the original session and SSH tunnel, and attempts
recovery only after confirming that the old remote import is absent. Chrome
stays running. It does not detach a surviving import or select another port.

A failed recovery preserves the ledger and reports a manual reset fallback
instead of repeatedly requesting sudo or rebuilding an uncertain tunnel.
Network failures and moving the key to a different port still require manual
reset. The five-second hub check is separate from the one-second readiness
poll configured by `REMOTE_CHROME_YUBIKEY_POLL_INTERVAL`.

## Stop and desktop handoff

On the display host, stop one session and its forwarding:

```bash
remote-chrome stop remote-host
remote-chrome stop remote-host --session work-browser
```

Bare `stop` handles default-prefix outgoing tmux sessions, default-runtime
YubiKey ledgers, recorded notification listeners, and tracked incoming sessions:

```bash
remote-chrome stop
```

Run this on Snap when returning to Snap's desktop while Chrome is still
remoted from Starship. Each live incoming record gets one SSH cleanup request
to its display host, with a two-second request limit. Accepted cleanup finishes
independently there. If needed, Snap signals the exact recorded local browser
bootstrap so its normal Chrome/proxy/notification cleanup runs. Dead records
and records from previous boots are discarded.

Incoming tracking is best effort and does not block launch. It adds no
background service or automatic retry. Failed display-host cleanup leaves that
host's forwarding ledger available for a later local `stop`. `status` shows
incoming records without changing them.

An older untracked session can be stopped explicitly from the browser host:

```bash
remote-chrome stop snap --from starship
```

The target SSH name must match the original launch. Omit the target to request
all default managed sessions on Starship; add `--session NAME` for a custom
session. This explicit command requires noninteractive SSH and returns failure
if the request or cleanup fails.

Hostless teardown selects tmux names beginning with the configured prefix plus
`-`; it preserves an exact-name `remote-chrome` session and unrelated sessions.
For a custom session outside that prefix, specify both `HOST` and
`--session NAME`. For a custom YubiKey control socket, reuse its
`REMOTE_CHROME_YUBIKEY_SOCKET` setting with `stop HOST`; hostless discovery only
scans default-runtime YubiKey paths.

Cleanup attempts remote detach, tunnel close, local unbind, and owned-daemon
cleanup even when an earlier step fails. It matches the exact loopback server,
USB/IP port, and bus ID. Missing `vhci_hcd` after a remote reboot proves the old
import is gone; unreachable or unreadable state remains uncertain. Only an
exactly verified tool-owned `usbipd` can be stopped, and other exports or
`REMOTE_CHROME_STOP_USBIPD=0` retain it.

Unresolved forwarding cleanup returns nonzero and keeps state/logs for retry.
A later launch can reconcile a `cleanup-failed` ledger only when its resources
are demonstrably gone or intentionally retained. Active forwarding must be
stopped explicitly. Advisory remote notification socket removal has a
two-second SSH deadline plus one second of kill grace; failure does not prevent
remaining cleanup.

## Notifications

Notifications are enabled by default. The selected path depends on ownership
of `org.freedesktop.Notifications` on the browser host's normal user bus:

| Browser-host service | Behavior |
| --- | --- |
| Existing desktop daemon | Preserve its owner and mirror matching notifications informationally; clicks/buttons are not returned |
| No owner and compatible display daemon/bindings | Start a session-owned headless endpoint; route notifications, actions, replacements, and closes in both directions |
| Another managed headless endpoint | Reject competing ownership; stop that session or disable notifications for the new launch |
| Missing bindings, unsupported capabilities, or failed readiness | Warn and continue secure Chrome startup; Chrome may choose its own notification windows |

Disable notifications or broaden the default app-name allowlist for a fresh
launch:

```bash
remote-chrome launch remote-host --no-notifications
REMOTE_CHROME_NOTIFICATIONS=0 remote-chrome launch remote-host
REMOTE_CHROME_NOTIFICATION_APPS=chrome,chromium remote-chrome launch remote-host
```

The allowlist contains comma-separated, case-insensitive substrings. `chrome`
is the default; `*` matches all apps. Keep values free of spaces for the
foreground argument path. Matching uses the supplied app name, not Chrome PID
ownership. Existing-daemon mirroring can therefore include other matching apps
and duplicate notifications across multiple relay sessions. The owned endpoint
occupies the whole user-bus service name; notifications outside its allowlist
are rejected. It is intended for this managed browser workflow.

The headless path negotiates `body` and `actions`, plus optional `body-markup`.
Compatibility follows the D-Bus contract and capabilities, without a Chrome
version gate. Default clicks and offered action buttons return to the originating
notification. Inline replies, remote icon files/images, activation tokens,
persistence, offline replay, and automatic transport reconnect are unsupported.

Bodies retain `<b>`, `<i>`, `<u>`, newlines, and `<br>` when markup is supported.
Other tags and all attributes are removed; literal text/entities are escaped
for display. Summaries remain plain text. This preserves supported markup
received from D-Bus; HTML already escaped by Chrome's Web Notification handling
cannot be recovered as formatting. Without markup capability, bodies become
plain text.

The endpoint never replaces an existing owner and supports one managed
headless session per user bus. It releases the name when the owning bootstrap
exits, resets, stops, or loses transport. Run `remote-chrome stop` on the browser
host before its desktop daemon acquires the name; automatic desktop takeover
is outside the current scope. Chrome that already chose its built-in fallback
needs a deliberate reset to select the native path after setup is fixed.

Notification state and logs are on the display host:
`${XDG_RUNTIME_DIR:-/tmp}/remote-chrome-notify-<session>.state` and `.state.log`.
`status HOST` reports backend, capabilities, helper ownership, transport, and
recent failures. An older active bootstrap can report `unreported` until reset.
Malformed records are discarded individually. Exact listener cleanup also
handles startup failure before its state file exists.

## Diagnostics and recovery

`status HOST` performs read-only probes of tmux, recorded USB/IP state, daemon
identity, tunnel, actual remote readiness, and notification ownership. It sends
no notifications and returns nonzero for stale, failed, or unreachable checks.
`status` without a host is the managed-state overview.

`doctor HOST` checks display/commands, SSH, secure-session dependencies, and
USB/IP module prerequisites. It does not load modules, unlock a wallet, perform
a Safe Storage lookup, or change USB/IP state. With a live relay it sends a
**visible notification probe** and checks delivery; this does not test a user
click callback.

Inspect pane output on the display host, substituting the printed session name:

```bash
tmux capture-pane -pt remote-chrome-remote-host:chrome
tmux capture-pane -pt remote-chrome-remote-host:yubikey
```

For failed authentication, distinguish stored `ready` state from a fresh
`fido2-token -L` as the Chrome user and the exact attachment in `sudo usbip port`
on the browser host. If the physical hub is off, restore power first; if
automatic recovery cannot establish ownership, save browser work and reset.

If cleanup fails, retry the same scoped `stop HOST` with its custom session and
socket settings. Do not delete a ledger merely because a host is unreachable:
it is the ownership evidence for later safe recovery.

Secure startup can prompt for wallet unlock after a cold boot. Unlock and retry
if canceled. Pre-login networking, a fresh wallet unlock, and fresh FIDO access
are separate acceptance checks. The
[study index](studies/headless-notifications/README.md#acceptance-boundaries)
states which real-host scenarios have been recorded.
