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

When the remote browser asks for a security key, forward the local YubiKey
along with the Chrome session:

```bash
remote-chrome remote-host --with-yubikey
```

That creates a `chrome` tmux window and a `yubikey` tmux window. Closing the
remote Chrome browser only exits the `chrome` window; the `yubikey` window and
forwarding keep running until you stop the whole session:

```bash
remote-chrome stop remote-host
```

`stop` is the explicit teardown that kills the session and tears down forwarding
together.

For one-off sessions without tmux:

```bash
remote-chrome launch remote-host --foreground
```
