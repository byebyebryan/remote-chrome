# remote-chrome

[![CI](https://github.com/byebyebryan/remote-chrome/actions/workflows/ci.yml/badge.svg)](https://github.com/byebyebryan/remote-chrome/actions/workflows/ci.yml)

`remote-chrome` starts Google Chrome on a remote Linux host over Waypipe and can
temporarily forward a local YubiKey to that host for WebAuthn/FIDO prompts.

The common use case is a browser profile that must live on another machine, but
still needs a physical security key attached to the local machine.

## Requirements

### SSH

You need SSH access from the local host to the remote host.

- `remote-chrome launch --foreground HOST` can use normal interactive SSH.
- The default detached tmux launch requires noninteractive SSH, such as an SSH
  key, agent, or an already-open SSH control connection.
- YubiKey forwarding requires noninteractive SSH because USB/IP setup runs
  multiple remote commands.

### Chrome Over Waypipe

Local host requirements:

- `bash`
- `ssh`
- `waypipe`
- `tmux`
- a graphical Wayland session

Remote host requirements:

- `waypipe`
- `google-chrome-stable`

The launcher starts Chrome with:

```bash
--ozone-platform=wayland --disable-gpu --disable-features=Vulkan --new-window
```

Waypipe is intentionally kept as a prerequisite rather than installed by this
script. Package names and setup differ enough across distros that automatic
installation would add churn and surprising behavior.

### YubiKey Forwarding

Local host requirements:

- `usbip` (only when a configured key is detected or forwarding is forced)
- `ss` from `iproute2` (only when forwarding)
- `timeout` from `coreutils` (only when forwarding)
- a local YubiKey with the configured vendor/product (default `1050:0407`)
- `sudo` access for `modprobe usbip-host`, `usbip bind`, `usbip unbind`, and
  starting/stopping `usbipd`

Remote host requirements:

- `usbip`
- `timeout` from `coreutils` for bounded USB/IP list/attach/detach operations
- passwordless remote `sudo` for `modprobe vhci-hcd`, `usbip attach`,
  `usbip port`, and `usbip detach`
- `libfido2` for preferred `fido2-token` verification (a lower-confidence
  hidraw/udev check is used when it is unavailable)

Passwordless remote `sudo` is a hard requirement for YubiKey forwarding. The
remote commands run over noninteractive SSH and cannot complete a sudo prompt.

### Remote Host Setup (Arch)

On an Arch-family remote host, install the remote-side packages:

```bash
ssh remote-host 'sudo pacman -S --needed waypipe usbip coreutils libfido2'
```

Install Google Chrome on the remote host through the appropriate channel for
that machine.

## Local Setup

On an Arch-family local host, install the runtime dependencies:

```bash
sudo pacman -S --needed waypipe tmux usbip iproute2 coreutils
```

Clone the repository:

```bash
git clone https://github.com/byebyebryan/remote-chrome.git
cd remote-chrome
```

For an editable checkout, link the launcher into the user-local executable
directory:

```bash
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/bin/remote-chrome" "$HOME/.local/bin/remote-chrome"
```

Alternatively, install a standalone copy:

```bash
install -Dm755 bin/remote-chrome "$HOME/.local/bin/remote-chrome"
```

Make sure `$HOME/.local/bin` is on `PATH`. Add this to the appropriate shell
startup file if it is not already configured:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Verify the local setup:

```bash
command -v remote-chrome
remote-chrome --help
```

When forwarding is expected, check the configured key with `usbip list -l`.
Chrome-only sessions do not require a YubiKey or the `usbip` tools.

## Launch Chrome

Start Chrome on the remote host in a detached tmux session:

```bash
remote-chrome remote-host
```

The explicit `launch` subcommand is equivalent:

```bash
remote-chrome launch remote-host
```

The default tmux session name is `remote-chrome-HOST`, with characters that are
awkward for tmux targets replaced by underscores. For example, `remote.example`
becomes `remote-chrome-remote_example`.

Check or stop the session:

```bash
remote-chrome status remote-host
remote-chrome stop remote-host
```

Pass extra Chrome arguments after `--`:

```bash
remote-chrome launch remote-host -- --profile-directory=Default
```

Run in the foreground instead of tmux:

```bash
remote-chrome launch remote-host --foreground
```

Detached tmux mode is the default because it keeps Chrome and Waypipe alive if
the launching terminal exits, and it gives you a stable place to inspect logs:

```bash
tmux attach -t remote-chrome-remote-host
tmux capture-pane -pt remote-chrome-remote-host:chrome
```

A user systemd service would also work, but tmux keeps this tool dependency-light
and easy to inspect.

### Existing Remote Chrome Processes

Chrome has single-instance behavior per user-data directory. If Chrome is
already running on the remote host, a new invocation can delegate to that
existing browser process, which means the tab or window opens in the remote
host's normal desktop session instead of the Waypipe session.

`--new-window` does not fully solve this because the existing browser process
can still handle the request.

By default, `remote-chrome launch` checks for an existing remote Chrome browser
process and asks before killing it. Confirm the prompt to kill it, or answer no
to cancel the launch. Use these options to change that:

```bash
remote-chrome launch remote-host --allow-existing
remote-chrome launch remote-host --yes
```

Use `--allow-existing` only when you know the existing browser process uses a
different `--user-data-dir`. Use `--yes` to skip the prompt and always kill the
existing browser, after saving anything important in it.

## Forward A YubiKey

Launches use a tri-state YubiKey mode. By default, the launcher checks local
sysfs for the configured USB vendor/product and automatically forwards a
detected key. A detected key makes forwarding an expected prerequisite; module,
sudo, SSH, or readiness failures abort the launch with diagnostics. If no key is
detected, Chrome starts without the USB/IP prerequisites.

Force forwarding (and fail if the key or prerequisites are unavailable) with:

```bash
remote-chrome remote-host --with-yubikey
```

Explicitly skip detection and forwarding, even when a key is connected, with:

```bash
remote-chrome remote-host --no-yubikey
```

For a foreground session, forwarding starts before Chrome, waits until the
remote key enumerates as FIDO/hidraw, and cleans up when Chrome exits:

```bash
remote-chrome launch remote-host --foreground --with-yubikey
```

While forwarding is active, the YubiKey is attached to the remote host, so local
apps may not be able to use it. Detached mode starts the `yubikey` tmux window,
waits for readiness (up to 15 seconds by default), and only then creates the
`chrome` window. A timeout or child failure removes the session and rolls back
the exact resources it acquired.

Closing the remote Chrome browser only exits the `chrome` tmux window. The
`yubikey` window and forwarding keep running until you stop the whole session.
Stopping the session is the explicit teardown for both:

```bash
remote-chrome stop HOST
```

That sends the forwarding process a signal, and it detaches/unbinds the YubiKey
during cleanup.

To stop every default managed session and every YubiKey forwarding using the
default runtime state path on the local machine, omit the host:

```bash
remote-chrome stop
```

This only kills tmux sessions named with the configured
`REMOTE_CHROME_SESSION_PREFIX` followed by `-` (by default,
`remote-chrome-*`), so it leaves an exact-name `remote-chrome` session and
unrelated tmux sessions alone. YubiKey state files are cleaned independently,
which also tears down forwarding whose tmux session has already disappeared.
Use `stop HOST` for a custom `--session` name that does not use the default
prefix, or when using a custom YubiKey control socket.

The tool records provisional lifecycle state, the exact local bus ID, and any
`usbipd` process it started in a state file beside the SSH control socket.
Cleanup handles interrupted setup, only detaches/unbinds that recorded device,
never stops a pre-existing `usbipd`, and leaves a tool-started daemon running
while other USB/IP exports still exist. `status HOST` reports tmux windows and
the managed YubiKey phase/readiness without changing anything.

## Configuration

Chrome defaults:

```bash
REMOTE_CHROME_COMMAND=google-chrome-stable
REMOTE_CHROME_SESSION_PREFIX=remote-chrome
```

YubiKey defaults:

```bash
REMOTE_CHROME_YUBIKEY_USB_ID=1050:0407
REMOTE_CHROME_USBIP_PORT=3240
REMOTE_CHROME_YUBIKEY_SOCKET=${XDG_RUNTIME_DIR:-/tmp}/remote-chrome-yubikey-remote-host.sock
REMOTE_CHROME_STOP_USBIPD=1
REMOTE_CHROME_YUBIKEY_TIMEOUT=15
REMOTE_CHROME_YUBIKEY_BOOTSTRAP_TIMEOUT=30
REMOTE_CHROME_YUBIKEY_READY_GRACE=5
```

Set `REMOTE_CHROME_STOP_USBIPD=0` to leave a tool-started daemon running after
cleanup.

The readiness timeout starts after remote USB/IP attach; detached mode also has
a separate bounded bootstrap timeout for module loading, daemon startup,
binding, tunnel setup, and attach. The parent launcher allows a small additional
readiness grace period (5 seconds by default) after the child timeout so a final
state write or in-flight SSH probe can be observed. Exact remote detach may use
`sudo usbip port` because some hosts restrict USB/IP port visibility to root.

Before tmux or Chrome starts, the launcher checks the running local and remote
kernels, their `/lib/modules/<kernel>` trees, `usbip-host`/`vhci-hcd`, command
prerequisites, and required sudo paths. Module mismatch diagnostics include the
running kernel and available module directories and recommend rebooting after a
kernel upgrade. The launcher never installs packages, copies modules, or
reboots automatically. `fido2-token -L` is preferred for readiness; when only
the hidraw/udev fallback succeeds, output is labeled USB-only.

## Security Notes

USB/IP gives the remote host access to the forwarded USB device. Only forward to
hosts you trust.

The script uses an SSH reverse tunnel bound to `127.0.0.1` on the remote side.
The local `usbipd` daemon may still listen on the local host's network
interfaces while forwarding is active, depending on your distro's `usbipd`
behavior. `remote-chrome stop HOST` tears the forwarding down together with the
Chrome session.

## Development

Install ShellCheck and run the repository checks:

```bash
sudo pacman -S --needed shellcheck
./scripts/check
```

The check script runs Bash syntax validation, ShellCheck, the command-level
test suite, and `git diff --check`. The same checks run in GitHub Actions.

## License

Released under the [MIT License](LICENSE).
