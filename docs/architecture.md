# Architecture and lifecycle contracts

The runtime is the single executable [bin/remote-chrome](../bin/remote-chrome).
It embeds the remote Bash bootstrap and Python notification helpers; nothing
under `docs/studies/` is a runtime dependency. [Usage](usage.md) covers commands,
and [configuration](configuration.md) defines public defaults.

## Hosts and ownership

The display host owns the launcher, tmux session, Waypipe/SSH client, local
notification listener, and physical YubiKey export. The browser host owns the
remote bootstrap, Chrome child, filtered D-Bus proxy, USB/IP import, and any
notification/Secret Service helper created by that bootstrap.

Detached mode normally has a `chrome` window and, when forwarding, a `yubikey`
window. Foreground mode uses the same readiness/cleanup paths without tmux.
The YubiKey helper remains alive to own its lease and monitor hub reconnects;
incoming tracking and notification delivery introduce no permanent system
service.

Ownership comes from recorded process start times, exact commands, socket/file
identity, attempt tokens, session metadata, and boot identity where applicable.
A matching name or PID alone is insufficient. Runtime artifacts must have the
expected type and owner. Unsafe paths are rejected and preserved.

## Secure browser bootstrap

The bootstrap runs inside the browser host's Waypipe environment. It connects
`xdg-dbus-proxy` to the normal user session bus, normalizing a bare socket path
to `unix:path=...`. Only `org.freedesktop.secrets` and
`org.freedesktop.Notifications` are exposed. Desktop portals stay hidden so the
GTK chooser is rendered over Waypipe.

An existing Secret Service owner is reused. Otherwise, the bootstrap starts
`ksecretd` and records its exact identity. It looks up the selected browser's
Safe Storage key with `secret-tool`; output is discarded. Successful lookup is
required before running Chrome with `--password-store=gnome-libsecret`. There
is no automatic basic-storage fallback.

Cleanup handles normal exit, failed startup, INT, TERM, and HUP. It cleans only
the proxy and helpers acquired by that bootstrap, preserving pre-existing
services. Overlapping teardown signals cannot truncate owned cleanup. A
nonzero Chrome status is preserved; unresolved owned cleanup makes an otherwise
successful browser exit nonzero.

## Launch, reset, and teardown

Fresh launch validates runtime paths and prerequisites, including required
USB/IP preflight, before stopping an existing browser. Forwarding attachment
must pass actual FIDO readiness before Chrome is created. Notification listener startup is
advisory for ordinary failures, with exact rollback even when interrupted
before a state file can be published.

Reset selects a recorded canonical Waypipe command, verifies its exact SSH
reverse socket and remote process group, and recreates it in the same tmux
session. It preserves recorded launch settings. After teardown, forwarding
preflight runs again, followed by replacement listener startup and session
creation. Listener readiness comes before the replacement Chrome pane.
Ambiguous identity or unreachable evidence prevents destructive reset.

Forwarding cleanup attempts every applicable resource and retains its ledger
when any ownership or remote-state check remains unresolved. A later cleanup
can retry; a failed probe does not establish absence. Hub recovery uses the
same identities, requires confirmed absence of the old remote import, and
preserves Chrome. It neither moves the key to another port nor reconnects an
uncertain SSH tunnel automatically.

Each new browser bootstrap records its origin and identity for incoming stop.
On the browser host, bare `stop` sends a bounded cleanup request to each live
display host, then stops the exact local bootstrap if needed. Stale boot/process
records can be discarded. This is best-effort handoff, with no background retry.

## Notification paths and protocol

The filtered bus permission makes a service reachable; it does not create one.
When a real daemon already owns the browser user's notification name, the
bootstrap preserves it and mirrors allowlisted calls informationally. When no
owner exists, an owned endpoint can acquire the name atomically without
replacement or queueing, after negotiating with the display daemon. One such
endpoint is supported per browser user bus.

The owned endpoint implements `/org/freedesktop/Notifications` and
`org.freedesktop.Notifications`: server information, capability discovery,
`Notify`, `CloseNotification`, `ActionInvoked`, and `NotificationClosed`.
Interactive readiness requires body/actions support and successful protocol
calls. `body-markup` is optional. Browser versions are diagnostic evidence,
never selection gates. The endpoint can process other app names through its
allowlist, but occupies a user-wide service name and is scoped to this browser
workflow.

A per-session reverse SSH Unix socket carries bounded JSON records. Existing
daemon mirroring is one-way. The owned endpoint uses the bidirectional v1
protocol:

| Direction | Records |
| --- | --- |
| Browser to display | `notify`, `close` |
| Display to browser | `mapped`, `action`, `closed` |

Session identity and generation scope the connection; source/local IDs and
update revisions scope callbacks. Stale actions or closes cannot invalidate a
newer replacement. Disconnect releases the owned source name and closes only
the destination notifications mapped to that connection. There is no offline
replay, persistent notification store, or automatic reconnect.

Same-user processes can access the relay socket, so records are untrusted.
Framing, queues, active notifications, content size, and delivery rate are
bounded. Delivery never interpolates content into a shell. The sanitizer allows
only basic `b`, `i`, `u`, and line-break markup without attributes; summaries
remain plain text. Icon files/images, inline replies, persistence, activation
tokens, and automatic desktop takeover are outside the supported contract.

## Runtime artifacts

Runtime storage uses `${XDG_RUNTIME_DIR:-/tmp}` on each owning host unless a
specific path is noted. `<session>` and `<host>` components are sanitized.
Artifacts are ownership evidence, not a stable API for manual editing.

| Host | Artifact | Purpose |
| --- | --- | --- |
| Display | `remote-chrome-<session>.bootstrap.sh` | Generated bootstrap passed to the remote Waypipe environment |
| Display | `remote-chrome-notify-<session>.state` and `.state.log` | Listener identity, sockets, diagnostics/delivery activity |
| Display | `remote-chrome-notify-<token>.sock` | Private listener socket |
| Browser | `/tmp/remote-chrome-notify-<token>.sock` | Reverse SSH notification socket |
| Display | `remote-chrome-yubikey-<host>.sock` | SSH USB/IP control socket, or configured custom path |
| Display | `<control socket>.state`, `.state.log`, `.state.lock`, `.state.cleanup.lock` | Forwarding ledger, diagnostics, provisional ownership, and serialized cleanup |
| Display | `remote-chrome-usbipd-<port>.pid` | Tool-started daemon ownership, possibly root-owned |
| Browser | `remote-chrome-incoming-<random>.state` | Origin, boot, bootstrap identity, and notification status for incoming handoff |
| Browser | `remote-chrome-dbus-proxy-<pid>.sock` | Filtered session-bus proxy |
| Browser | `remote-chrome-notify-forwarder-<pid>.py`, `remote-chrome-notify-helper-<pid>.log`, `remote-chrome-notify-status-<pid>.state` | Embedded endpoint/mirror helper and readiness evidence |

Forwarding ledgers survive unresolved cleanup for safe retry. Notification
cleanup is advisory; remote socket removal is bounded and best effort.
Upgrading source/installed launchers does not replace an already running
bootstrap. A deliberate reset activates it.

## Verification boundaries

`scripts/check` runs shell lifecycle tests, private D-Bus protocol tests, and
generated-bootstrap tests. They exercise real private tmux/D-Bus helpers while
mocking SSH, privileged operations, hardware, and the browser. Passing them
establishes deterministic contracts, not real desktop appearance, physical
token access, or cold-boot reachability.

The [study index](studies/headless-notifications/README.md) separates dated
experimental evidence, managed real-host acceptance, and remaining checks.
Source commit, installed launcher, active bootstrap, CI, and manual acceptance
are distinct states; report them separately when diagnosing or deploying.
