# Managed background sessions

## Contract

New detached launches use a transient systemd user service. One supervisor owns
the Waypipe/SSH lifetime, notification listener and optional USB forwarding.
There is no permanent remote-chrome daemon and no automatic browser restart.
Closing the launching terminal leaves the session running. Closing the last
managed Chrome window ends the transport and cleans up forwarding. Closing one
of several windows leaves the session running. Managed Chrome disables its
background-app mode so window closure can end the browser process.

Physical removal of the recorded local USB device stops forwarding only.
It does not close Chrome, wait for a replacement key, or automatically export
a reinserted key. Hub power loss follows the same rule. Failed network/FIDO
probes are not evidence of physical removal. Forwarding can be started and
stopped independently with `remote-chrome usb start|stop HOST`.

Required forwarding remains a startup prerequisite. A forwarding failure after
Chrome starts does not terminate Chrome. Cleanup attempts all owned resources;
unconfirmed detach keeps the existing recovery ledger and a visible warning.
PID, process start, boot, attempt, socket and file identities remain authoritative.

## Runtime design

- A private per-session record replaces tmux options as the source of launch
  settings, generation, service identity, supervisor identity and USB state.
  Records are data, never sourced shell code. Updates are atomic and locked.
  A stop/reset lease prevents a competing launch from entering between teardown and
  replacement; dead leases carry no authority.
- The launcher completes USB setup before background handoff. This keeps the
  shared root-owned usbipd outside the browser service's process group, so
  retaining it for other exports remains possible.
- The supervisor observes its transport child and the exact local USB device.
  A local removal starts bounded, noninteractive USB cleanup. Actual transport
  exit ends the session; a slow or temporarily stalled connection does not.
- A systemd post-stop hook retries exact generation-owned cleanup even when
  the supervisor crashes. Unreachable remote state remains unresolved rather
  than being silently declared absent. A hard reboot cannot execute cleanup.
- Background sudo must be noninteractive. Scope passwordless permissions to
  the required USB/IP cleanup operations; do not depend on a terminal's cached
  authentication. Foreground launch remains available for debugging.
- Session lifecycle events enter the existing diagnostics store and share its
  configured 30-day / 1-GiB maximum retention. Operational stdout/stderr is
  available through `logs [HOST] [--since 2h]` across retained launches, and
  follows journal retention. `logs --list` and `--generation ID` select past
  launches without relying on runtime records, including after reboot when
  journal history survives. `--follow` stays with its selected generation.
- Cleanup records identify USB, notification and bootstrap failures separately.
  Each owned resource is attempted even when another fails. Successfully
  cleaned or unused USB remains off after a different resource fails. Unknown
  helper results retain uncertainty and do not claim unused USB has failed.
- `status`, `monitor`, `stop`, `reset`, bare HOST and incoming stop discover
  both new sessions and legacy tmux sessions. Existing sessions are preserved;
  their next deliberate reset adopts the new backend. `attach` still works for
  legacy sessions and explains `logs` for new sessions.

## Acceptance gates

1. Private-record safety, duplicate launch, generation replacement, process
   reuse, unsafe paths and concurrent stop/reset/USB changes.
2. Normal transport exit, supervisor failure, terminal hangup and stop all
   clean up only their own resources; partial cleanup preserves retry state.
3. Unplug and hub disappearance stop USB without affecting the transport;
   replug stays off; network/FIDO failure cannot masquerade as removal.
4. Forwarding-only start/stop, failed setup and browser exit during setup or
   cleanup; no unattended privilege prompt.
5. Existing secure bootstrap, reset identity, notifications, diagnostics,
   status and monitor regression suites remain green.
   Historical journal selection works without runtime state, isolates session
   and generation, and preserves legacy pane capture. Partial cleanup retains
   accurate resource state and can be retried through the public stop path.
6. Disposable real user-service checks exercise normal exit and forced
   supervisor failure without touching a real key, browser profile or tunnel.
7. Publish a checksum-pinned launcher and apply only its selector/payload on
   Snap, Starship and Carbon; verify installed bytes and preserve active
   sessions, packages, configuration and unrelated source changes.

Physical token removal and real Chrome window closure remain distinct from
mocked hardware and disposable process acceptance; report those boundaries.
