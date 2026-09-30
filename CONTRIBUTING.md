# Contributing

Bug reports and focused pull requests are welcome.

## Before Submitting

- Describe the local and remote Linux distributions involved.
- Say whether the problem affects Chrome transport, YubiKey forwarding, or
  both.
- Include the command used and relevant output with hostnames, usernames, SSH
  details, and hardware-token secrets removed.
- Report security issues privately according to [SECURITY.md](SECURITY.md).

## Development

Install ShellCheck and tmux, then run:

```bash
./scripts/check
```

Changes to command parsing or lifecycle behavior should include a focused test
in `tests/remote-chrome_test.sh`. Tests must mock privileged and remote
operations; they must not bind a real USB device, start forwarding, or alter a
remote host.

The real tmux regression check uses its own socket and an empty configuration;
it never attaches to or changes the developer's tmux server.

Keep commits small and use an imperative summary such as `fix: clean up a
failed tunnel`.
