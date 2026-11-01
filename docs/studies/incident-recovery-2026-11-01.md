# Operational history and cleanup attribution

Launcher 1.11.1 closes two gaps found after the managed-session rollout.

## Historical operational output

Previously `logs HOST` selected the service from the latest runtime record.
Reset replaced that record; reboot removed it. Earlier journal output could
survive without a public command to select it.

History now uses the deterministic session hash and complete generation in
existing journal unit fields. It includes manager-only launches that never
produced process output. No command, environment or credential copy is needed
in a separate persistent index. Default output is the last 100 retained lines
across generations; time windows and explicit generation selection narrow it.
`--follow` stays with one generation, including across a later reset.

Isolated real user-service acceptance created two short-lived marker writers,
then queried them with a nonexistent runtime directory. Listing and combined
history found both launches; generation and timestamp windows isolated the
first one. This checks loss of runtime state without rebooting a host. Journal
persistence and retention remain host policy; missing journal bytes cannot be
reconstructed. Existing 1.11.0 journal history needs no session restart.

## Cleanup state

The resource helper reports USB, notification and bootstrap failures separately.
The session keeps all failed resources for retry and does not assign a
notification/bootstrap error to unused or successfully cleaned USB forwarding.
Unknown or older helper results remain explicitly unresolved. Older running
supervisors keep their original cleanup behavior until a deliberate relaunch.

Failure details reach status, monitor attention, lifecycle summaries and raw
exports. Lifecycle events from new launches also record their generation,
linking diagnostic attempts to operational journal selection.

## Validation and limits

The check runner passes 448 checks, including historical selection without
runtime state, session/generation isolation, time validation, legacy pane
capture, partial cleanup, public stop retry, and diagnostic/report preservation.
Disposable journal writers do not use Chrome, Waypipe or USB/IP. The prior
physical-token removal and real last-window-close acceptance items remain
daily-use checks; this pass changes no transport or codec policy.
