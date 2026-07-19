# Usage

Typical browser session:

```bash
remote-chrome launch remote-host
```

When the remote browser asks for a security key:

```bash
remote-chrome yubikey run remote-host
```

After login finishes, press Ctrl-C in the YubiKey forwarding terminal.

Stop Chrome when done:

```bash
remote-chrome stop remote-host
```

For one-off sessions without tmux:

```bash
remote-chrome launch remote-host --foreground
```
