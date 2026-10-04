# remote-chrome

[![CI](https://github.com/byebyebryan/remote-chrome/actions/workflows/ci.yml/badge.svg)](https://github.com/byebyebryan/remote-chrome/actions/workflows/ci.yml)

`remote-chrome` runs Chrome on another Linux machine over Waypipe, displays its
notifications locally, and can temporarily forward a local YubiKey for
WebAuthn/FIDO prompts. The browser profile stays on the remote machine.

The **display host** runs the command and has the Wayland desktop and physical
key. The **browser host** runs Chrome. For `remote-chrome snap` on Starship,
Starship is the display host and Snap is the browser host.

This README covers installation and host setup. See:

- [Usage and troubleshooting](docs/usage.md) for launch, reset, handoff, and cleanup.
- [Configuration](docs/configuration.md) for options and environment defaults.
- [Architecture](docs/architecture.md) for lifecycle ownership and notification contracts.
- [Notification study and acceptance index](docs/studies/headless-notifications/README.md)
  for dated evidence, roadmap reconciliation, and remaining acceptance limits.
- [Contributing](CONTRIBUTING.md) for development dependencies and checks.

## Requirements

### SSH and browser transport

Configure SSH from the display host to the browser host. Detached launches and
YubiKey forwarding require noninteractive authentication through a key, agent,
or existing control connection. `launch --foreground` can use interactive SSH
for Chrome transport; forwarding still needs noninteractive SSH.

| Host | Required commands and environment |
| --- | --- |
| Display | `bash`, `ssh`, `waypipe`, a graphical Wayland session, and `tmux` for detached mode |
| Browser | `bash`, `waypipe`, `xdg-dbus-proxy`, `secret-tool` (libsecret), `ksecretd` (KWallet/Secret Service), `busctl` (systemd), and the selected Chrome executable |
| Both, with notifications enabled | `python3` |
| Both, for interactive headless notifications | Python D-Bus and GLib bindings (`python-dbus` and `python-gobject` on Arch; `python3-dbus` and `python3-gi` on Debian) |
| Display, for existing-daemon informational forwarding | `notify-send` (libnotify); `busctl` detects body markup support |
| Display, for bounded notification diagnostics, cleanup, and incoming stop requests | `timeout` (coreutils) |
| Browser, for `stop` at desktop handoff | `tmux`, `ssh`, and `timeout`; SSH back to the display host enables its cleanup request |

Interactive notifications also need a display-host notification daemon that
advertises body and action support. Missing bindings or unsupported capabilities
produce a warning and let the secure browser launch continue. Disabling
notifications removes their Python requirement.

Chrome starts with `--ozone-platform=wayland --disable-gpu
--disable-features=Vulkan --new-window`. Waypipe and the browser are installed by
the user; the launcher does not install packages.

### Secure browser storage

Every launch uses a filtered proxy to the browser host's normal user session
bus. It permits Secret Service and notifications while hiding desktop portals,
so the GTK file chooser travels over Waypipe. Before Chrome starts, a Safe
Storage lookup must succeed; secret output is discarded. Chrome always receives
`--password-store=gnome-libsecret`, and caller overrides are rejected.

A missing key/service or canceled wallet prompt aborts launch. Unlock the wallet
and retry. An existing Secret Service owner is preserved; otherwise the
bootstrap owns and cleans the `ksecretd` it starts. See the
[secure-session contract](docs/architecture.md#secure-browser-bootstrap) and
[browser identity configuration](docs/configuration.md#browser-and-session).

### YubiKey forwarding

Forwarding is automatic when a local key matches the configured USB ID
(`1050:0407` by default). Chrome-only sessions do not need USB/IP tools.

| Host | Additional forwarding requirements |
| --- | --- |
| Display | `usbip`, `ss` (iproute2), `timeout` (coreutils), and sudo for `modprobe usbip-host`, `usbip bind`, `usbip unbind`, and starting/stopping `usbipd` |
| Browser | `usbip`, `timeout`, passwordless scoped sudo for `modprobe vhci-hcd`, `usbip attach`, `usbip port`, and `usbip detach` |
| Browser, for verified FIDO readiness | `fido2-token` (libfido2) and read/write access to the matching FIDO hidraw device as the Chrome user |

Without `fido2-token`, an accessible matching udev/hidraw device is a
lower-confidence USB-only fallback. A detected or explicitly required key makes
forwarding a launch prerequisite: failed module, sudo, SSH, or readiness checks
abort before Chrome starts.

USB/IP gives the browser host access to the key. The reverse SSH listener binds
to browser-host loopback, but the display host's `usbipd` may listen on network
interfaces, depending on the installed daemon. Use trusted hosts and inspect
the daemon's exposure. See [Security policy](SECURITY.md).

#### Headless remote hosts

On a desktop, systemd-logind normally grants token access to the active seat
through `uaccess`. After a headless reboot, that seat may belong to a greeter
such as `sddm`, leaving an SSH-launched browser unable to open the FIDO device.

For a dedicated browser host, use a narrow system group and udev rule. This
example covers the default USB ID; adjust it for a different key:

```bash
getent group remote-chrome >/dev/null || sudo groupadd --system remote-chrome
sudo usermod --append --groups remote-chrome "$USER"
sudo tee /etc/udev/rules.d/99-remote-chrome-yubikey.rules >/dev/null <<'EOF'
SUBSYSTEM=="hidraw", KERNEL=="hidraw*", ATTRS{idVendor}=="1050", ATTRS{idProduct}=="0407", ENV{ID_FIDO_TOKEN}=="1", GROUP="remote-chrome", MODE="0660"
EOF
sudo udevadm control --reload-rules
```

Reconnect the forwarded device (or reboot), then start a new SSH/Chrome session
so the supplementary group is present. As the Chrome user, `fido2-token -L`
must list the exact forwarded key. Graphical autologin is unnecessary.

#### Expire reverse tunnels after a forwarding-host reboot

A hard reboot cannot close the display host's SSH connection. The browser
host's SSH server may retain the reverse listener until TCP times out, blocking
a later bind to port `3240`.

On a dedicated browser host, create
`/etc/ssh/sshd_config.d/60-remote-chrome-keepalive.conf`, replacing the account
and stable display-host address (for example, its Tailscale IP):

```text
Match User <remote-user> Address <forwarding-host-ip>
    ClientAliveInterval 30
    ClientAliveCountMax 2
Match all
```

Validate and reload without dropping healthy sessions:

```bash
sudo sshd -t
sudo systemctl reload sshd
```

Matching stale listeners normally expire in roughly a minute. The launcher
also checks for a port collision before stopping existing Chrome and leaves
Chrome untouched when the forwarding port is occupied.

## Installation

### Arch package examples

On the display host, install transport and notification dependencies:

```bash
sudo pacman -S --needed openssh waypipe tmux coreutils python python-dbus python-gobject libnotify
```

On the browser host:

```bash
sudo pacman -S --needed openssh waypipe tmux coreutils xdg-dbus-proxy libsecret systemd python python-dbus python-gobject
```

Install Chrome and the package supplying `ksecretd` through the appropriate
channels for that host. If forwarding is wanted, also install `usbip` and
`coreutils` on both hosts, `iproute2` on the display host, and `libfido2` on the
browser host. Configure scoped sudo and FIDO access as described above.

### Install the launcher

Install the launcher on **both hosts** so incoming stop requests can ask the
display host to clean up. Clone the repository:

```bash
git clone https://github.com/byebyebryan/remote-chrome.git
cd remote-chrome
```

Link an editable checkout, or install a standalone copy:

```bash
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/bin/remote-chrome" "$HOME/.local/bin/remote-chrome"
```

```bash
install -Dm755 bin/remote-chrome "$HOME/.local/bin/remote-chrome"
```

Ensure `$HOME/.local/bin` is on `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v remote-chrome
remote-chrome --help
```

## Everyday workflow

On the display host:

```bash
remote-chrome remote-host
remote-chrome status remote-host
remote-chrome attach remote-host
remote-chrome reset remote-host
remote-chrome stop remote-host
```

Repeating `remote-chrome HOST` resets its existing managed session. Reset
restarts Chrome and forwarding using the recorded settings, so save in-page
work first. To change settings, stop and explicitly `launch` again; adding
launch options to a repeated bare command does not reconfigure that session.

At the browser host's desktop, run `remote-chrome stop` to stop tracked incoming
sessions and release an owned notification endpoint. It makes a bounded
best-effort cleanup request to each display host and stops the exact local
browser bootstrap if needed.

Notifications are enabled by default. An existing browser-host daemon uses
informational mirroring; an otherwise headless bus can use the session-owned
endpoint for clicks and closes. This follows D-Bus contracts and capability
negotiation, with no Chrome version pin. See
[notification behavior](docs/usage.md#notifications).

For diagnostics, `status` sends no notifications; `doctor HOST` checks
prerequisites and sends a **visible notification probe** when a relay is live.
See [diagnostics and recovery](docs/usage.md#diagnostics-and-recovery).

Updating installed launchers leaves active sessions running. Update both hosts
before a deliberate reset activates a new bootstrap. Unattended reboot
reachability, wallet unlock, and fresh FIDO access require separate host
acceptance; installing the launcher alone does not establish them.

## Development and license

Run `./scripts/check` with the dependencies in [Contributing](CONTRIBUTING.md).
Graphical and hardware acceptance are separate from the isolated CI suites.

Released under the [MIT License](LICENSE).
