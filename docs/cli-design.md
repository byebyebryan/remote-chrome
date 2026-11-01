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
boxed borders, animation, or cursor movement are needed for one-shot commands.
The explicit `monitor` command has its own full-screen behavior below.
Below 60 columns, ordinary rows move values below labels. Group rows
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

## Live monitor

`remote-chrome monitor` opens a local session overview; arrows or `j`/`k` select
a session and Enter opens its dashboard. `remote-chrome monitor HOST` opens the
same default session as `status HOST`; `--session NAME` targets a custom one.
An absent requested session remains absent until that exact session appears.
Tab switches between dashboard and overview. The monitor never launches,
resets, attaches to, or stops a tunnel.

The dashboard keeps identity and recorded transport at the top. Observed codec
use has its own check age. Traffic and TCP Delay appear together, followed by
Delivery and Recording; small history graphs follow. Below 76 columns the
groups stack. Arrows, `j`/`k`, Page Up/Down and Home scroll a short terminal;
the title and controls remain visible. Resize recomputes the layout.

Illustrative layout (values are observations, not health thresholds):

```text
remote-chrome / snap / LIVE
remote-chrome-snap / attempt 5575e9d2
Recorded transport: h264,hw,bpf=250000 / Display carbon
Codecs checked 8s ago: Decode hardware / Encode hardware

TRAFFIC / latest interval + 5m      TCP DELAY / ACK progress
RX latest        1.2 Mbps           RTT last ACK     30 ms
TX latest        12 KiB/s           ACK age          0.7s old
5m avg RX        800 KiB/s          5m RTT p50/p95   29 ms / 54 ms
5m avg TX        8 KiB/s            Deviation* last  8 ms
5m peak RX+TX    3 Mbps             5m dev p95*      41 ms

DELIVERY                           RECORDING
Retrans latest   0                 Recorder         active
Retrans / 5m     2                 Sample           fresh / 0.7s old
Send queue now   0 B               Coverage / 5m    298s / TCP 299/300
Unsent now       0 B               Gaps / 5m        1
5m max queue     12 KiB            Activity latest  active

HISTORY / 5m / oldest -> now / blanks = no eligible observation
RX+TX |...small bars...| 3 Mbps max
RTT   |...small bars...| 54 ms max
Dev*  |...small bars...| 41 ms max
* TCP deviation is a variation proxy; RTT uses ACK progress.
```

Latest means the last recorded interval, not an instantaneous probe. Its age is
visible in Recording. After eight seconds, latest traffic/queues/retransmission
increments become unavailable. The last ACK-qualified RTT and deviation retain
their separate age and an explicit `not fresh` label; five-minute percentiles
remain historical observations. History uses five-second buckets: combined
traffic is an interval-weighted average; RTT/deviation use the maximum eligible
estimate. Each graph scales independently to its displayed maximum. Blank bins
mean no eligible observations; recorded idle zeros have a visible baseline bar.
Graph compression takes the maximum observed bin in each column. Sub-bucket
gaps remain represented by the coverage and gap fields; graphs do not assert
continuous coverage.

Local recording reads refresh about once a second; local tmux inventory every
five seconds. Both run in bounded subprocesses so disk or tmux stalls cannot
block input. Only the selected dashboard checks codec state, through the same
Waypipe telemetry used by status: one bounded local/remote check per 30 seconds,
with its own timestamp and an outer ten-second deadline. The overview adds no
SSH checks. No extra collector, active probe, or diagnostic write is created.
Existing `diagnostics --watch` stays a separate textual report.

Space freezes the displayed snapshot and its ages; `FROZEN` makes clear that
recording continues. Space resumes live display. Overview selection is disabled
while frozen; Tab returns to the live session list. `q`, Escape, Ctrl-C, TERM
and HUP restore the terminal and cancel only the monitor's observer process
groups. Cursor state, echo and terminal attributes are restored after an error.
Redirected streams and `TERM=dumb` receive a clear error and `status` alternative.
Python's standard-library curses module is required; no third-party Python
dependency is added. `--plain` uses ASCII graphs and no colors; `--color` and
`NO_COLOR` retain their usual precedence.

The selection key includes exact session, target, attempt, recording policy,
transport and pane identity. Changed identities invalidate data and codec
caches, cancel old checks, and show a session-change notice. Missing, disabled,
unknown, stale, partial and ended recordings stay explicit. Failed inventory
reads never masquerade as a confirmed empty inventory. Recording reads retain
the existing 32 MiB budget, current-boot filtering, and exact viewer
origin/session/attempt restrictions. No health score is inferred.
