# Managed background sessions

## Contract

New detached launches use a transient systemd user service. One supervisor owns
the Waypipe/SSH lifetime, notification listener and optional USB forwarding.
There is no permanent remote-chrome daemon and no automatic browser restart.
The main and USB SSH connections send keepalives after 15 seconds of silence
and stop after three unanswered checks (approximately 45 seconds while awake).
These are per-command options, not changes to global SSH or server policy.
The browser transport uses its own SSH connection rather than a shared control
master, so it owns its failure timing and TCP observations.
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
  exit ends the session. Idle traffic and brief interruptions are allowed;
  persistent SSH unresponsiveness ends the transport through its keepalive
  policy. A broken USB control connection stops forwarding independently.
  A missing listener or confirmed refusal on the same safe control socket is
  treated as dead. Unsafe, inaccessible, changed or timed-out socket evidence
  remains unknown and does not authorize cleanup of a possible replacement.
- Forwarding intent is separate from cleanup debt. Removal or `usb stop` latches
  intent off, including after initially required forwarding. A later reset
  does not require or automatically re-export that key. A fresh required launch
  still requires the key, and `usb start` explicitly enables forwarding again.
- A systemd post-stop hook retries exact generation-owned cleanup even when
  the supervisor crashes. Unreachable remote state remains unresolved rather
  than being silently declared absent. A hard reboot cannot execute cleanup.
- New cleanup attempts share a 30-second deadline. Notification and bootstrap
  cleanup run before potentially stalled USB cleanup. The service stop limit
  is 45 seconds per stop phase and the CLI request has a 65-second outer limit.
  A timed-out helper and its descendants are terminated; ownership ledgers
  retain every unconfirmed obligation. There is no background retry loop.
  Nested cleanup `timeout` requests use `--foreground` to remain in the
  worker's process group, so an in-flight SSH child cannot escape that group
  kill. Shared daemons created before cleanup remain outside the worker.
- Transport outcome and its first end timestamp are retained separately from
  cleanup attempts/completion. Unexpected exits remain visible after successful
  cleanup. One best-effort local failure notice supplies the exact reset command;
  normal browser closure, requested stop and physical key removal stay quiet.
- New bootstraps publish their launch generation in incoming-session metadata.
  The supervisor retains the generated remote socket when its SSH child is
  observable. Reset can use that state after local processes exit, verifies the
  remote generation, boot and process identities, and refuses ambiguous ownership.
  Remote inspection/termination requests each have a ten-second deadline.
  Reset captures the local record's generation once. Lease acquisition checks
  that generation under the record lock; subsequent reads and local stop use
  the same generation. A concurrent replacement makes the old reset refuse
  further action instead of switching to the new session.
  The generation is checked again after confirmation before remote TERM.
  Readers reopen a displaced, unlinked inode after an atomic save, with a
  bounded retry and all private-file/hardlink checks repeated on the new path.
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
  Status/monitor also retain ended failed sessions as needing recovery, without
  counting them as active or relabelling old observations as fresh. Reset accepts
  retained ended records and restores transport, browser, diagnostic and
  notification settings. Bare HOST recovers a retained failure; normal ended
  sessions keep the ordinary fresh-launch behavior. Hostless reset selects an
  active session first, then an unambiguous retained failure.

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
An isolated real SSH server and drop proxy exercise idle keepalives and bounded
loss detection without changing system SSH configuration or host networking.
Responsive SSH with frozen rendering still requires a manual reset. Runtime
records are per boot; recovery of launch settings across a full reboot is not
provided by the operational journal.
