# Usage

Typical browser session:

```bash
remote-chrome remote-host
```

`launch` is an explicit alias for the same behavior. If remote Chrome is already
running, you are asked before it is killed. Confirm to kill it, or use
`--allow-existing` to launch anyway:

```bash
remote-chrome launch remote-host --allow-existing
```

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
Remote forwarding requires passwordless scoped sudo for `modprobe vhci-hcd`,
`usbip attach`, `usbip port`, and `usbip detach`; some hosts expose USB/IP port
records only to root. The local and remote `timeout` command (from coreutils)
is also required when forwarding, so every remote USB/IP operation remains
bounded; Chrome-only launches do not require the USB/IP or timeout prerequisites.

The child forwarding process waits up to 15 seconds after attach by default.
Detached mode gives the parent an additional 5-second readiness grace period
for the final state write and an in-flight SSH probe before declaring failure.

`status remote-host` reports session windows plus managed YubiKey phase and
readiness, if state exists. Status is read-only.

For one-off sessions without tmux:

```bash
remote-chrome launch remote-host --foreground
```

Foreground mode follows the same preflight and readiness wait, and Ctrl-C/TERM/
HUP cleanup detaches remotely, closes the owned tunnel, unbinds only the
recorded bus ID, and stops only a tool-owned `usbipd`.
