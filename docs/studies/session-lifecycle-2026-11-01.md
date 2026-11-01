# Managed session acceptance — November 1, 2026

Launcher 1.11.0 replaces tmux for new background sessions. The deterministic
suite exercises private records, process and generation ownership, duplicate
launch, stop/reset leases, provisional USB setup, cleanup failures, local device
removal, public commands and status/monitor presentation.

Disposable user-service acceptance on Snap used the candidate launcher with
private runtime/state directories and a `sleep` transport. It verified normal
transport exit, explicit stop, forced supervisor SIGKILL and post-stop cleanup.
A simulated local USB device disappeared while the supervisor and transport
continued; reinsertion left forwarding off. Each exact fixture service ended
before the check returned. Ordinary requested stops exit successfully.

A separate public-command check exercised launch, journal logs, reset with a
new generation, independent USB start/status/stop, simulated removal and final
session stop. It used a fake Waypipe executable, simulated sysfs device, and
stubbed remote preflight/setup. It made no real SSH, privileged USB/IP, browser
profile or display changes. Installed-command checks repeat these boundaries
on Snap, Starship and Carbon during deployment.

These checks establish supervisor and command contracts, not physical token
behavior or real Chrome window closure. Closing the last window relies on the
managed bootstrap disabling Chrome background-app mode. Physical unplug and
last-window closure remain daily-use acceptance items. The existing Carbon to
Snap tunnel remains on its original backend until its next deliberate reset.

Video/compression defaults and diagnostic retention remain unchanged. Lifecycle
events are recorded automatically when diagnostics are enabled; operational
stdout/stderr is read from the user journal through `remote-chrome logs HOST`.
