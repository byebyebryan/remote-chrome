# Usage

Typical browser session:

```bash
remote-chrome launch remote-host
```

If remote Chrome is already running, close it first or launch with explicit
cleanup:

```bash
remote-chrome launch remote-host --kill-existing
```

When the remote browser asks for a security key:

```bash
remote-chrome yubikey run remote-host
```

After login finishes, press Ctrl-C in the YubiKey forwarding terminal.

To launch Chrome and keep YubiKey forwarding in the same tmux session:

```bash
remote-chrome launch remote-host --with-yubikey
```

That creates a `chrome` tmux window and a `yubikey` tmux window.

Stop Chrome when done:

```bash
remote-chrome stop remote-host
```

For one-off sessions without tmux:

```bash
remote-chrome launch remote-host --foreground
```
