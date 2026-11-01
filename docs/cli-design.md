# CLI presentation

The daily workflow stays `remote-chrome HOST`, followed by browser use. This
pass improves the receipt and inspection commands without changing launch,
reset, stop, forwarding, transport, or recording policy.

## Information hierarchy

1. Identify the browser host and the outcome in ordinary words.
2. State where Chrome runs and where its windows appear.
3. Show the selected video policy and diagnostic policy at launch; show
   observed transport and recording state in status.
4. Give copyable, exact commands for inspection, restart, and stop.
5. Keep implementation details in `--verbose`; surface failed checks even
   without it.

Example launch receipt (illustrative host names):

```text
remote-chrome · snap

  ✓ Background session started

  Browser      snap
  Display      carbon
  Video        H.264 · hardware preferred
  Diagnostics  enabled · 30 days · 1 GiB cap

  Inspect      remote-chrome status snap
  Restart      remote-chrome reset snap
  Stop         remote-chrome stop snap
```

`Session started` means the tmux session was created. It does not establish
that the browser rendered a window. `Hardware preferred` is a policy, not an
observed encoder. Recording being enabled does not establish a live collector.
Launch adds no readiness polling, network probes, or waits for samples.

Codec snapshots establish hardware/software use. Open render nodes do not prove
which GPU a codec uses, so compact status says `hardware` and leaves the node
inventory in verbose output.

Reset uses the same receipt with `Background session restarted` and a visible
warning about possible lost in-page state. Its transport and diagnostic rows
describe the replayed command and preserved policy, not today's defaults.
Repeating bare `HOST` remains an intentional reset; its warning stays visible.
Stop reports success only after its existing cleanup checks finish. Failures
retain their exact explanation and exit status.

Default session commands omit redundant `--session`; custom-session commands
always include it. Every argument remains shell quoted. Attach and internal
session information remain available through verbose receipts.

## Status

Host status groups identity, transport, local five-minute network history,
recording health, and forwarding/notification checks. Network rows expose
observed average RX/TX, combined peak, TCP RTT p50/p95, TCP deviation p95,
retransmissions, queues, recorded duration, sample age, coverage and gaps.
TCP deviation is explicitly a variation proxy. No overall network score or
claim about frame/input latency is inferred from these measurements.

Missing data stays unavailable; idle traffic can be zero. Stale, partial,
disabled, untracked, and absent states remain distinct. Queries retain exact
attempt/session/current-boot filtering and existing read budgets. A running
tmux session and successful liveness checks do not prove smooth delivery.

Hostless status remains a local overview: one section per managed session,
its exact network history, and the existing forwarding/incoming inventory.
It must not add per-session SSH requests. `--verbose` retains detailed checks,
PIDs, paths, recorded parameters, and collector-version evidence.

## Terminal behavior

Use a single column, generous spacing, and aligned labels. No boxed borders,
full-screen UI, animation, cursor movement, or persistent monitor is needed.
Below 60 columns, values move below labels. Commands wrap naturally without
inserting characters into their shell syntax.

Color is automatic on a terminal: cyan identity/sections, green confirmed
success, amber attention, red failure, and dim secondary labels. Words carry
the meaning independently. `--color auto|always|never` overrides detection;
`--plain` disables both color and glyphs. `NO_COLOR` disables automatic color,
and `TERM=dumb` disables automatic decoration. UTF-8 terminals can use the
small standard glyph set `✓`, `!`, `×`, `·`; no icon font is required.
Redirected output uses the same layout in plain text.

Presentation options work before or after the subcommand, but never consume
browser arguments after `--` or values of existing options. Internal commands
and machine-readable diagnostics exports retain their existing protocol.
Terminal controls in displayed values are escaped; arguments used for actions
are untouched. Prompts and warnings remain on stderr. Verbose output remains
human output, not a promised machine-readable format.

## Delivery and review

The presentation layer receives explicit labels and values from checks; it
does not infer state by parsing the old human messages. Embedded codec and
diagnostic helpers supply explicit presentation rows from their existing
structured observations. Collection and rendering stay separate.

Review a fresh launch, reset, clean stop, no-session overview, custom session,
missing host, degraded checks, stale/partial/missing network history, idle RTT,
hardware fallback, narrow terminal, redirected output, and verbose output.
Check color/UTF-8 detection in a real PTY; check option and browser-argument
boundaries. Run the complete regression suite and inspect installed status on
the fleet without restarting active tunnels. Preserve prior payloads, package
and config state, and unrelated chezmoi work during deployment.
