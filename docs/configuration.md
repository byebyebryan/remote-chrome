# Configuration

Configure launches on the display host. Flags override the corresponding
launch defaults. Reset keeps the recorded compression, browser settings,
notification settings, and YubiKey mode; stop and explicitly `launch` again to
change them. Runtime timeout/path settings still govern each invocation. See
[usage](usage.md).

## Launch options

| Option | Purpose |
| --- | --- |
| `--session NAME` | Select a tmux session; also accepted by attach, status, reset, and stop |
| `--foreground` (`--no-tmux`) | Run transport and cleanup in the launching terminal |
| `--chrome-command CMD` | Select a remote executable name or path, without shell syntax |
| `--compression MODE` | Select Waypipe compression: `zstd=1` (default) or `lz4` |
| `--transport MODE` | `lossless` (standalone default) or `video` (managed fleet default) |
| `--video-codecs MODE` | `auto` hardware preference or `software` codecs |
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

## Transport and compression

| Variable | Default | Meaning |
| --- | --- | --- |
| `REMOTE_CHROME_COMPRESSION` | `zstd=1` | Waypipe compression; accepts `zstd=1` or `lz4`; `--compression` takes precedence |
| `REMOTE_CHROME_TRANSPORT` | Configuration, then `lossless` | Select `video` or `lossless` |
| `REMOTE_CHROME_VIDEO_CODECS` | Configuration, then `auto` | Select `auto` or `software` |
| `REMOTE_CHROME_CONFIG` | `$XDG_CONFIG_HOME/remote-chrome/config`, or `~/.config/remote-chrome/config` | Optional data file with `transport=` and `video_codecs=` entries |

Chezmoi selects video on Snap, Starship, and Carbon. Video requires the
coinstalled `waypipe-remote-chrome` package on both ends, enables Chrome GPU
rendering, and uses H.264 at `bpf=250000` (250,000 bits per frame, not bits per
second). The [October 30 bitrate sweep](studies/video-bitrate-2026-10-30/README.md)
records the measurements and the later decision to prioritize network headroom
over the sharper 500k setting. Large ARGB/ABGR
surfaces are treated as opaque; this mode discards their transparency and uses
lossy 4:2:0 video.
Small alpha surfaces remain on the lossless path. Stock Waypipe is retained.

Launcher 1.6.0 checks both custom binaries' compiled FFmpeg bindings before a
video launch or video reset stops an existing browser. Missing libraries,
incompatible symbols, an older package without the compatibility query, or an
unreachable peer prevent that restart. Rebuild/update the named host's managed
package before retrying. Compatible library updates within the required ABI do
not require a rebuild. The launcher never installs packages itself.

The browser-host package selects NVIDIA when an accessible NVIDIA render node
is present. Viewers use their compositor's GPU, as Waypipe requires. Snap
normally uses NVIDIA encoding, Starship NVIDIA decoding, and Carbon Intel
decoding. Browser hosts without NVIDIA use software encoding; Intel encoding
is excluded because it produced incorrect colors in qualification. Intel's
`ANV_DEBUG=video-decode` is scoped to the custom Waypipe process. Unsupported
hardware codecs retain Waypipe's software fallback. Software video still uses
Vulkan for buffer conversion; use lossless mode to disable Waypipe GPU usage.

```bash
remote-chrome launch snap --video-codecs software
remote-chrome launch snap --transport lossless
```

Flags override environment variables, which override the data file. Reset
replays the original transport and codec policy. Stop and launch a fresh
session to select a different policy; a package or configuration update does
not restart an existing session. Hardware selection is evaluated each time the
recorded custom Waypipe command runs. Sessions recorded at either `bpf=250000`
or the former `bpf=500000` default remain resettable at that bitrate. Launcher
1.6.1 uses 250k for fresh video launches; stop and freshly launch a 500k session
to adopt the lower target.

Vulkan validation mode remains a diagnostic limitation of the tested hardware
video path: NVIDIA reported video-image/profile errors, and hardware decoding
stalled under `--debug`. Default launches omit validation. Do not interpret a
successful ordinary launch as a validation-clean result.

`status HOST` reports recorded transport settings separately from live codec
observations. The custom package writes bounded process/start-time/boot-bound
snapshots when codec contexts first succeed, change backend, or retire; it
does not write per-frame logs. Hardware, software and mixed observations refer
to active video contexts, not a GPU preference flag. No active observed video
buffers, older packages, or absent telemetry are reported as unknown. Software
use with hardware preference is a warning, while a verified missing or
ambiguous transport is degraded. Updating a package can leave a live process
using the previous executable; status identifies that separately.

Video snapshots live in the private directory
`${XDG_RUNTIME_DIR:-/tmp}/remote-chrome-waypipe-<uid>`. Only matching live process
identities are read. Snapshots carry no browser URLs or page content and never
authorize cleanup. `waypipe-remote-chrome --remote-chrome-info` provides a
GPU-free JSON compatibility query. `doctor HOST` includes this check but retains
its existing visible notification probe; ordinary status sends no test notification.

Fresh launches explicitly use Zstd level 1. Both hosts need Waypipe built with
support for the selected codec. To use LZ4 for a new session:

```bash
remote-chrome launch remote-host --compression lz4
# Or set a default for launches from this shell:
export REMOTE_CHROME_COMPRESSION=lz4
```

Reset and repeated bare `HOST` commands replay the recorded compression choice.
Older sessions without an explicit codec retain Waypipe's implicit default.
Stop and explicitly launch again to change an existing session's compression;
updating the launcher does not change a running stream.

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

`REMOTE_CHROME_USBIP_HOST_SYSFS`, `REMOTE_CHROME_MODULE_SYSFS_ROOT`,
`REMOTE_CHROME_MODULES_ROOT`, `REMOTE_CHROME_VHCI_HCD_DRIVER_SYSFS`, and
`REMOTE_CHROME_HIDRAW_ROOT` are test filesystem selectors. The module and VHCI
selectors also apply when set in the browser host's environment. Attempt-token
and `REMOTE_CHROME_ORIGIN_*` variables are internal lifecycle metadata;
ordinary launch configuration should not set them.
