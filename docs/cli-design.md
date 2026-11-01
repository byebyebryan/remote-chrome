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

Host status uses a short identity and transport header, followed by four
measurement groups: Traffic, TCP Delay, Delivery, and Recording. The groups expose
observed average RX/TX, combined peak, TCP RTT p50/p95, TCP deviation p95,
retransmissions, queues, recorded duration, sample age, coverage and gaps.
TCP deviation is explicitly a variation proxy. No overall network score or
claim about frame/input latency is inferred from these measurements.

Missing data stays unavailable; idle traffic can be zero. Stale, partial,
disabled, untracked, and absent states remain distinct. Queries retain exact
attempt/session/current-boot filtering and existing read budgets. A running
tmux session and successful liveness checks do not prove smooth delivery.

Traffic identifies observed average RX/TX and combined peak. Delay labels its
percentiles and keeps the ACK-progress restriction and TCP deviation's variation
proxy explanation visible. Delivery labels queue values as maxima. Recording
shows recorder state, sample freshness, recorded seconds, TCP availability and
gaps together, once. An older-recorder advisory is a quiet footer; stale,
missing, partial or degraded observations remain explicit attention states.
Values use about three significant figures for scanning; logs, exports and
verbose output retain their existing precision and measurement semantics.

Illustrative 80-column measurement layout:

```text
  NETWORK / local SSH / last 5 minutes

  TRAFFIC / observed average     TCP DELAY
  RX           220 B/s           RTT median       29 ms
  TX           10.3 B/s          RTT p95          54 ms
  Peak RX+TX   7.36 KiB/s        Deviation p95*   41 ms

  DELIVERY                 RECORDING
  Retransmits      0       Recorder   active / fresh 0.2s old
  Max send queue   0 B     Coverage   300s / TCP 300/300
  Max unsent       0 B     Gaps       0

  * TCP deviation is a variation proxy; RTT uses ACK-progress samples.
```

Hostless status remains a local overview: one short section per managed session,
its exact traffic/delay/retransmission/recording summary, and the existing forwarding/incoming inventory.
It must not add per-session SSH requests. `--verbose` retains detailed checks,
PIDs, paths, recorded parameters, and collector-version evidence.
The overview can show the recorded transport preference, but must not claim
observed hardware encoding or decoding without codec snapshots.
Empty forwarding, orphan-daemon and incoming inventories use short `None`
rows. Nonempty inventories retain their headings and exact records.

## Terminal behavior

Pair Traffic with TCP Delay, then Delivery with Recording when their actual
content fits the terminal. Otherwise stack those same groups in reading order.
Align labels and emphasize values; leave a blank line between group pairs. No
boxed borders, full-screen UI, animation, cursor movement, or persistent monitor
is needed. Below 60 columns, ordinary rows move values below labels. Group rows
also stack labels and values when needed to fit. Commands wrap naturally without
inserting characters into their shell syntax. Redirected output preserves the
grouping without decoration and uses 80 columns unless COLUMNS is supplied.

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
