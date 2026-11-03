# Clean failure and manual reset

Launcher 1.12.0 gives persistent connection loss a bounded failure path and
retains the state needed for an explicit reset. It does not reconnect or restart
the browser automatically.

## Transport and cleanup

New main/USB SSH connections use 15-second keepalives with three unanswered
checks. Idle connections answer keepalives normally; short interruptions have
a grace period. The main transport disables sharing an existing SSH master,
so that another connection's policy cannot bypass its failure detection.
Persistent silence ends the transport in approximately 45
seconds while the host is awake. Suspend and network changes are not themselves
proof that a connection has failed. A responsive SSH connection with frozen
rendering still needs a manual reset.

The first transport outcome and end time are separate from cleanup state.
Cleanup attempts share 30 seconds, with service/CLI limits outside that budget.
Local notification and bootstrap cleanup precede USB cleanup, so an unreachable
peer cannot prevent them from being attempted. Unfinished USB work retains its
ownership ledger; stop/reset can retry it. No unattended retry loop is added.

The expected USB path remains: authenticate, remove the key, keep browsing.
Removal and explicit USB stop latch forwarding intent off, independently of
cleanup debt and the original startup requirement. A later reset can start
Chrome without the key. Reinsertion does not re-export it. Loss of the USB SSH
master is shown separately from the main browser transport.

## Recovery and presentation

New frozen bootstraps export a launch generation and include it in their
incoming record. The viewer retains the generated Waypipe remote socket when
its SSH child can be observed. Manual reset can use these references after local
processes disappear, validates the remote generation and process identities,
and refuses uncertain ownership. Inspection and termination SSH requests each
have a ten-second deadline. Generation lookup follows Waypipe workers to the
server root, rather than selecting a worker as the owner.

Reset preserves browser arguments, video/compression policy, diagnostics and
notification routing. Bare HOST recovers a retained failure through the same
path; a normal ended session retains fresh-launch behavior. Older records have
conservative fallbacks and cannot authorize unverified remote termination.
Recovery records remain per boot; journal retention does not recreate settings
after a full reboot.

Status and monitor retain ended failures without calling them active or making
old recordings fresh. A best-effort local notice is attempted once for an
unexpected session failure. A replacement generation supersedes an old pending
notice. Normal close, explicit stop and key removal stay quiet. Lifecycle
records include transport exit/end context independently of later cleanup.

## Acceptance

The regression suites cover retained reset without a local SSH owner, removal
after required forwarding, notification restoration after listener-state
cleanup, pending cleanup/deadlines, first-failure preservation, superseded
notices, and stale/wrong remote identities. Real disposable process groups
exercise generation lookup through a worker, exact termination and post-stop
absence while an unrelated process stays running.

A private real OpenSSH server and drop proxy use ephemeral keys with a pinned
private known-hosts file. With accelerated keepalives, the idle connection
survives longer than the failure threshold; dropping traffic without closing
the connection causes SSH to exit and leaves a recoverable record. No system
SSH configuration or host networking is changed.

Public-command acceptance under real user services uses a fake Waypipe and fake
USB sysfs. It checks unplug without closing the browser, unexpected transport
exit, explicit reset, bare-HOST recovery, argument and USB-off preservation,
normal quiet exit and automatic diagnostic events. It sends no real desktop
notice and performs no real USB/IP, remote SSH or browser-profile operation.

Real physical key removal, host suspension and actual browser window closure
remain daily-use acceptance; these fixtures do not claim those observations.
