# remote-chrome

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

- `usbip`
- a local YubiKey visible in `usbip list -l`
- `sudo` access for `modprobe usbip-host`, `usbip bind`, `usbip unbind`, and
  starting/stopping `usbipd`

Remote host requirements:

- `usbip`
- passwordless remote `sudo` for `modprobe vhci-hcd`, `usbip attach`, and
  `usbip detach`
- `libfido2` for optional `fido2-token` verification

Passwordless remote `sudo` is a hard requirement for YubiKey forwarding. The
remote commands run over noninteractive SSH and cannot complete a sudo prompt.

### Arch Example

On Arch-family systems:

```bash
sudo pacman -S --needed waypipe tmux usbip
ssh remote-host 'sudo pacman -S --needed waypipe usbip libfido2'
```

Install Google Chrome on the remote host through the appropriate channel for
that machine.

## Install

Clone the repo and put `bin` on your `PATH`, or copy `bin/remote-chrome` to a
directory already on your `PATH`.

```bash
git clone https://github.com/byebyebryan/remote-chrome.git
export PATH="$PWD/remote-chrome/bin:$PATH"
```

## Launch Chrome

Start Chrome on the remote host in a detached tmux session:

```bash
remote-chrome launch remote-host
```

The host can also be the first argument:

```bash
remote-chrome remote-host
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
process and stops before launching if one is found. Use one of these options:

```bash
remote-chrome launch remote-host --kill-existing
remote-chrome launch remote-host --kill-existing --yes
remote-chrome launch remote-host --allow-existing
```

Use `--kill-existing` only after saving anything important in the remote browser.
Use `--allow-existing` only when you know the existing browser process uses a
different `--user-data-dir`.

## Forward A YubiKey

For an interactive WebAuthn prompt, foreground mode starts USB/IP
forwarding and cleans up when you press Ctrl-C:

```bash
remote-chrome yubikey run remote-host
```

Leave that terminal open while completing the browser prompt. Touch the physical
YubiKey locally when Chrome asks for it.

The explicit start/stop form is available when needed:

```bash
remote-chrome yubikey start remote-host
remote-chrome yubikey status remote-host
remote-chrome yubikey stop remote-host
```

While forwarding is active, the YubiKey is attached to the remote host, so local
apps may not be able to use it.

You can also start YubiKey forwarding with the Chrome tmux session:

```bash
remote-chrome launch remote-host --with-yubikey
```

This creates a second tmux window named `yubikey` in the same session. Stopping
the tmux session sends the forwarding process a signal, and it detaches/unbinds
the YubiKey during cleanup.

If you prefer an explicit prompt when a local YubiKey is detected:

```bash
remote-chrome launch remote-host --ask-yubikey
```

The tool does not auto-forward a detected YubiKey by default. Forwarding gives
the remote host access to the USB device, so it stays opt-in.

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
```

If `modprobe usbip-host` fails locally after a kernel upgrade, reboot so the
running kernel matches `/lib/modules`.

## Security Notes

USB/IP gives the remote host access to the forwarded USB device. Only forward to
hosts you trust.

The script uses an SSH reverse tunnel bound to `127.0.0.1` on the remote side.
The local `usbipd` daemon may still listen on the local host's network
interfaces while forwarding is active, depending on your distro's `usbipd`
behavior. Run `remote-chrome yubikey stop HOST` after use, or use
`remote-chrome yubikey run HOST` and stop it with Ctrl-C.
