# Configuration

Configure launches on the display host. Flags override the corresponding
launch defaults. Reset keeps the recorded browser/notification settings and
YubiKey mode; stop and explicitly `launch` again to change them. Runtime
timeout/path settings still govern each invocation. See [usage](usage.md).

## Launch options

| Option | Purpose |
| --- | --- |
| `--session NAME` | Select a tmux session; also accepted by attach, status, reset, and stop |
| `--foreground` (`--no-tmux`) | Run transport and cleanup in the launching terminal |
| `--chrome-command CMD` | Select a remote executable name or path, without shell syntax |
| `--with-yubikey` / `--no-yubikey` | Require forwarding / skip detection and forwarding |
| `--yubikey-usb-id ID` | Select the local USB vendor/product |
| `--yubikey-port PORT` | Set the USB/IP TCP port |
| `--allow-existing` | Permit an existing remote browser; use only with a separate user-data directory |
| `--yes` (`-y`) | Skip the prompt before killing existing remote browsers; for reset, skip restart/confirmed-absence warnings |
| `--no-notifications` | Disable the relay and owned endpoint for this launch |
| `-- CHROME_ARGS...` | Pass browser arguments after the launcher options |

`doctor` accepts the browser command and YubiKey selection options.
`stop --from SOURCE` requests cleanup on a display host from the browser host.
Use `remote-chrome --help` for the command synopsis. Ownership checks still
apply with `--yes`, and `--password-store` browser overrides are rejected.

## Browser and session

| Variable | Default | Meaning |
| --- | --- | --- |
| `REMOTE_CHROME_COMMAND` | `google-chrome-stable` | Remote executable |
| `REMOTE_CHROME_SESSION_PREFIX` | `remote-chrome` | Prefix for default `PREFIX-HOST` tmux names and hostless discovery |
| `REMOTE_CHROME_SECRET_APPLICATION` | Derived from executable | Safe Storage application identity |
| `REMOTE_CHROME_SECRET_SCHEMA` | Derived from executable | Safe Storage `xdg:schema` identity |

Built-in identities:

| Executable | Application | Schema |
| --- | --- | --- |
| `google-chrome-stable`, `google-chrome`, `chrome` | `chrome` | `chrome_libsecret_os_crypt_password_v2` |
| `chromium`, `chromium-browser` | `chromium` | `chromium_libsecret_os_crypt_password_v2` |

Set both Secret Service variables together to override the mapping. An unknown
custom executable requires this explicit pair:

```bash
export REMOTE_CHROME_SECRET_APPLICATION=my-browser
export REMOTE_CHROME_SECRET_SCHEMA=my_browser_libsecret_os_crypt_password_v2
remote-chrome launch remote-host --chrome-command /opt/my-browser
```

## Notifications

| Variable | Default | Meaning |
| --- | --- | --- |
| `REMOTE_CHROME_NOTIFICATIONS` | `1` | Enable notifications; `0`, `no`, `off`, or `false` disable them (also `NO`, `OFF`, `FALSE`) |
| `REMOTE_CHROME_NOTIFICATION_APPS` | `chrome` | Comma-separated, case-insensitive app-name substrings; `*` matches all |

Keep allowlist terms free of spaces. Disabling notifications leaves the filtered
bus permission in place but starts no relay/owned endpoint. See
[notification behavior and limits](usage.md#notifications).

## USB/IP and runtime paths

| Variable | Default | Meaning |
| --- | --- | --- |
| `REMOTE_CHROME_YUBIKEY_USB_ID` | `1050:0407` | Local key vendor/product |
| `REMOTE_CHROME_USBIP_PORT` | `3240` | TCP port used by daemon, reverse tunnel, and remote clients; range 1–65535 |
| `REMOTE_CHROME_YUBIKEY_SOCKET` | `${XDG_RUNTIME_DIR:-/tmp}/remote-chrome-yubikey-<host>.sock` | SSH control socket; host characters are sanitized |
| `REMOTE_CHROME_STOP_USBIPD` | `1` | Set `0` to retain a tool-started daemon after cleanup |

For a non-default port, scoped remote sudo must permit
`usbip --tcp-port PORT attach`. The default port uses `usbip attach`.

Custom socket parents are created privately when absent and must be owned
directories. Reuse the same setting with `status HOST` and `stop HOST`; hostless
YubiKey discovery scans default-runtime paths. State and logs live beside the
socket as `.state` and `.state.log`. `XDG_RUNTIME_DIR` controls runtime storage
on each host; it falls back to `/tmp`. See the
[artifact map](architecture.md#runtime-artifacts).

## Timeouts and polling

Use positive whole seconds for timeout settings, except the tunnel readiness
setting described below. These control individual stages, not a single deadline
for the whole launch or stop.

| Variable | Default | Scope |
| --- | --- | --- |
| `REMOTE_CHROME_YUBIKEY_BOOTSTRAP_TIMEOUT` | `30` | Detached parent wait for forwarding setup to reach attachment/readiness |
| `REMOTE_CHROME_YUBIKEY_TIMEOUT` | `15` | FIDO readiness after attach |
| `REMOTE_CHROME_YUBIKEY_READY_GRACE` | `5` | Additional detached parent grace after the readiness window |
| `REMOTE_CHROME_YUBIKEY_POLL_INTERVAL` | `1` | Readiness polling interval; does not change the five-second hub recovery check |
| `REMOTE_CHROME_SSH_TIMEOUT` | `5` | Whole YubiKey SSH command/control request, plus one second of kill grace; invalid/zero values use `5` |
| `REMOTE_CHROME_USBIP_TIMEOUT` | `5` | USB/IP daemon readiness and remote list/attach operations |
| `REMOTE_CHROME_USBIP_STOP_TIMEOUT` | `3` | Owned `usbipd` termination wait |
| `REMOTE_CHROME_TUNNEL_TIMEOUT` | `5` | Number of tunnel readiness attempts, one second apart; each SSH request has its own deadline |
| `REMOTE_CHROME_YUBIKEY_CLEANUP_LOCK_TIMEOUT` | `30` | Wait for another cleanup caller; hub recovery uses `0` to avoid blocking |
| `REMOTE_CHROME_RESET_TIMEOUT` | `5` | Remote process-group termination polling during reset, not a whole SSH request deadline |

Notification startup/cleanup and incoming stop use separate fixed bounded
waits. `REMOTE_CHROME_SSH_TIMEOUT` governs YubiKey requests, not every Chrome
transport or diagnostic command. Longer configured stages can outlast a single
SSH request deadline.

`REMOTE_CHROME_USBIP_HOST_SYSFS`, `REMOTE_CHROME_MODULES_ROOT`, and
`REMOTE_CHROME_HIDRAW_ROOT` are test filesystem selectors. Attempt-token and
`REMOTE_CHROME_ORIGIN_*` variables are internal lifecycle metadata; ordinary
launch configuration should not set them.
