# Contributing

Bug reports and focused pull requests are welcome.

## Before Submitting

- Describe the local and remote Linux distributions involved.
- Say whether the problem affects Chrome transport, YubiKey forwarding,
  notifications, or more than one area. Distinguish the display host (desktop
  and physical key) from the browser host (Chrome and profile).
- Include the command used and relevant output with hostnames, usernames, SSH
  details, and hardware-token secrets removed.
- Report security issues privately according to [SECURITY.md](SECURITY.md).

## Development

The check runner needs `shellcheck`, `tmux`, `python3`, `dbus-daemon`,
`xdg-dbus-proxy`, and Python D-Bus/GLib bindings. On Arch:

```bash
sudo pacman -S --needed shellcheck tmux python python-dbus python-gobject dbus xdg-dbus-proxy
```

On Debian/Ubuntu (matching the CI dependency set):

```bash
sudo apt-get install --yes shellcheck tmux python3 python3-dbus python3-gi dbus-daemon xdg-dbus-proxy
```

Then run:

```bash
./scripts/check
```

The same runner is used by GitHub Actions. It checks Bash syntax, ShellCheck,
three test suites, and `git diff --check`:

| Suite | Contract |
| --- | --- |
| `tests/remote-chrome_test.sh` | CLI, tmux identity, launch/reset/stop, forwarding readiness/recovery, signal rollback, and unsafe runtime paths |
| `tests/headless_notifications_test.py` | Real private-bus endpoint/listener negotiation, actions, replacements, closes, malformed input, delivery races, and disconnect cleanup |
| `tests/secure_notifications_test.py` | Generated secure bootstrap, notification-name ownership, dependency failures, helper cleanup, and overlapping teardown signals |

Changes to lifecycle or protocol behavior should include a focused regression
in the relevant suite. Tests must mock privileged and remote operations; they
must not bind a real USB device, start real forwarding, or alter a remote host.

The real tmux checks use private sockets and empty configurations. Notification
tests use private D-Bus daemons, temporary runtime directories, a fixture daemon,
and a fake browser. No graphical session, real Chrome, Waypipe, physical key,
or live user-bus owner is needed for the check runner. It disables Python cache
writes; caches created by manual study imports are also ignored.

## Documentation and host acceptance

Keep installation/setup in [README.md](README.md), operational guidance in
[usage](docs/usage.md), defaults in [configuration](docs/configuration.md), and
lifecycle contracts in [architecture](docs/architecture.md). Check examples
against the parser and defaults against the launcher before updating them.

The [notification study index](docs/studies/headless-notifications/README.md)
separates original plans, immutable JSON evidence, later implementation, and
remaining manual checks. Keep historical counts and host observations dated;
do not turn a fixture result into a desktop or hardware acceptance claim.
Study tools run manually and are separate from CI/runtime dependencies. Their
default output is fresh scratch evidence, not a checked-in snapshot.

For deployment, distinguish source commit, installed artifact, and active
bootstrap on both hosts. Updating the launcher does not restart Chrome. Save
browser work before a controlled reset, and validate actual FIDO access as the
Chrome user rather than relying on stored readiness. Real desktop click,
office-session, and cold-boot checks need explicit controlled host acceptance.

For documentation-only changes, validate links/anchors, examples/defaults, and
`git diff --check`. Run relevant code checks for any changed harness or helper;
do not run real-host acceptance merely to validate documentation.

Keep commits small and use an imperative summary such as `fix: clean up a
failed tunnel`.
