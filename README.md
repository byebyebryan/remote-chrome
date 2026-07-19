# remote-chrome

`remote-chrome` starts Google Chrome on a remote Linux host over Waypipe and can
temporarily forward a local YubiKey to that host for WebAuthn/FIDO prompts.

The common use case is a browser profile that must live on another machine, but
still needs a physical security key attached to the local machine.

## Requirements

Local host:

- `bash`
- `ssh`
- `waypipe`
- `tmux`
- `usbip`
- `sudo` access for `modprobe`, `usbip bind`, and cleanup

Remote host:

- `ssh` access
- `google-chrome-stable`
- `usbip`
- `sudo` access for `modprobe vhci-hcd` and `usbip attach`
- `libfido2` for optional `fido2-token` verification

On Arch-family systems:

```bash
sudo pacman -S --needed waypipe tmux usbip
ssh remote-host 'sudo pacman -S --needed usbip libfido2'
```

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

The default tmux session name is `remote-chrome-HOST`.

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

## Forward A YubiKey

For an interactive WebAuthn prompt, use foreground mode. It starts USB/IP
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
