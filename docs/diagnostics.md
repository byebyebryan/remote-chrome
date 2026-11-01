# Session diagnostics

Diagnostics records technical observations during ordinary use, so a later
investigation can compare network conditions with process load and codec
selection. It starts on a deliberate launch or reset. Installing a new launcher
or running `status` does not attach a collector to an existing tunnel.

Enable it in the display host's data-only configuration file,
`${XDG_CONFIG_HOME:-~/.config}/remote-chrome/config`:

```ini
diagnostics=on
diagnostics_retention_days=30
diagnostics_max_bytes=1073741824
```

The standalone default is `off`. Managed Snap, Starship and Carbon select `on`.
`REMOTE_CHROME_DIAGNOSTICS=off` overrides the config for a new launch. Detached
reset preserves its recorded diagnostics policy, alongside its canonical
transport command; legacy sessions without that policy use current defaults.
Retention accepts 1–30 days and 8 MiB–1 GiB. The cap is shared across all roles
and sessions belonging to this user on each host. It does not allocate 1 GiB.

## Investigating an incident

Run these on the **display host** where the tunnel was launched:

```bash
remote-chrome mark snap
remote-chrome diagnostics snap --since 20m
remote-chrome diagnostics snap --session work-browser --since 2h
remote-chrome diagnostics snap --at 2026-10-31T14:00:00-07:00 --window 5m --timeline
remote-chrome diagnostics snap --since 7d
remote-chrome diagnostics snap --watch 60s
remote-chrome diagnostics snap --probe 30s --since 5m
remote-chrome diagnostics snap --since 20m --export /tmp/chrome-incident.jsonl
```

Mark is optional. It stores a timestamp against the most recent local attempt
seen within five minutes, without a text field. An approximate incident time is
also sufficient. Summary queries work after the session ends, for windows up
to 30 days (`s`, `m`, `h`, `d`). `--session` narrows the selection; otherwise
all matching attempts to that target are included.

`--at` requires an ISO timestamp with a timezone; `--window` is the total
centered window, clipped at the present. `--timeline` adds aligned UTC buckets
of eligible TCP RTT, directional traffic, queues, retransmissions and CPU/pressure. Different
hosts' monotonic clocks are never subtracted. New peers return a wall-clock
offset estimate with conservative uncertainty including SSH and query time;
older peers report alignment unavailable.

`--watch [DURATION]` is an explicit foreground view of local files, refreshed
every five seconds for at most one hour. It does not repeatedly SSH, send probes
or control Chrome. It cannot be combined with `--at`, `--export` or `--probe`.

`--probe [DURATION]` explicitly requests one ICMP packet per second for at most
120 seconds to the latest observed owned TCP peer address. It requires recent
local connection evidence. Packet counts and ping RTT are stored as a separate
`probe` event. Timeout/unavailable ping is not reported as a measured zero.
A probe can stimulate the path; ICMP results are separate from application
TCP timing, retransmissions and frame pacing.

The summary prints each endpoint and attempt separately: sample availability,
RTT/variation, peak traffic, interval retransmissions, queues, process CPU,
codec observations, passive path changes and lifecycle/gap events. One bounded
SSH request reads browser-host history; local results remain useful if that
host is unreachable or its installed helper is older. The browser does not need
to SSH back to the display host. The default summary does not copy raw history
between hosts. `status HOST` reports local recording state, latest sample and
storage location alongside the existing transport status.

Explicit export creates a new private file containing only records matching
the selected incident window, target, originating host and optional session.
It refuses an existing output file or an export inside the managed store.
Exports are outside automatic retention. Raw exports retain a 32 MiB input
limit per endpoint and report a refusal if exceeded; shorten the window.
Normal analysis streams within the capped store instead of loading raw history
into memory. Export is a request to copy those technical observations, including
hostnames, addresses and process identities. Review it before sharing.

## Recording health

`status HOST` describes the current local viewer attempt recorded in tmux.
It prints the invoked launcher version separately from the collector version
and record schema found in that attempt's evidence. Installing a launcher does
not replace a running collector. A collector that predates the command gets an
advisory about the next deliberate launch/reset; the version difference alone
does not degrade its recording state.

The recording state is `active` only with a fresh sample (under eight seconds)
and exact live collector and owner identities on the current boot. A live owner
or collector without fresh samples is `degraded`; known ended identities are
`off`. An explicit recorded off policy stays off even if older history exists.
A session without a recorded attempt identity or usable process metadata is
`unknown`; status does not select a recent unrelated attempt. These diagnostic
states are advisory and do
not change the existing transport checks' exit status.

The recent health view covers at most the last five minutes. It shows declared
TCP/process/host/path sampling intervals where recorded, the observed median
sample interval, sample and TCP availability counts, ACK-tracking coverage,
RTT-eligible samples, gaps and recorded TCP-unavailability reasons. Observed
intervals use consecutive non-carried samples from the same attempt and boot,
with monotonic timestamps; reported gaps and socket discontinuities break the
sequence. Intervals over 60 seconds are excluded. These are retained
observations, not a promise of continuous coverage or an expected-sample score.

Zero eligible RTT samples during idle activity differs from absent ACK fields.
The latter makes freshness unavailable, even with a newer collector on a
kernel/tool path that lacks those fields. Older minute summaries without field
coverage remain unknown rather than contributing measured zeros. Context
carried from an earlier chunk supplies metadata, never a fresh sample.

Status opens the store read-only, without creating files or pruning history.
Record reads and fallback chunk-header identity reads share a 32 MiB input
budget; status reports partial coverage if it exhausts that budget. It adds no
SSH request, probe, service or collector. Header lookup can identify a stale
attempt without scanning its old record bodies.

Historical `diagnostics` reports show the query helper version separately from
the collector versions, schemas, cadence and field coverage for each endpoint
and attempt in the requested window. Minute summaries preserve this context
and coverage for long queries. Older helpers and summaries remain readable;
missing metadata stays unknown. Historical context does not claim that the
collector is currently running. Local and browser-host reports use the existing
single bounded SSH request.

## Network status

Both `remote-chrome status` and `remote-chrome status HOST` show a local network
summary for each exact managed viewer attempt over the last five minutes. The
overview includes it beneath each session; the host-specific command also shows
observed transport and recording health. Add `--verbose` for individual checks,
recorded parameters, process identities, collector versions, and byte totals.
This works with already recorded
history immediately after a launcher update, without restarting the tunnel.

The summary shows observed RX/TX average rates in Mbps (KiB/s or B/s for light
traffic), peak combined RX+TX rate, retransmissions, maximum send/unsent queues, and
ACK-progress TCP RTT p50/p95 with TCP deviation p95. Rates average only intervals
with recorded counter deltas, including valid idle intervals; the observed
duration is shown instead of treating gaps as zero traffic. RX and TX are from
the display host's SSH socket and include its forwarded channels, rather than
all traffic on a network interface or only encoded video bytes.

TCP deviation is a delay-variation proxy, not a measurement of frame pacing,
one-way jitter or input latency. Idle RTT estimates do not supply fresh delay
observations. Missing counters, absent ACK freshness fields, explicit-off
recording and unknown attempts stay unavailable rather than becoming zero.
Samples must belong to the selected attempt, session, target and current boot.
Sample age, stale state, TCP coverage, gaps and partial-read warnings remain
visible. These observations do not assign an overall network quality score.

The view reuses local bounded, read-only history. It adds no network probe or
SSH request; `diagnostics HOST --since 5m` remains the way to compare both
endpoints and investigate incidents in more detail.


## Recorded data

| Observation | Frequency |
| --- | --- |
| Attempt ID, endpoint role, launcher/kernel/FFmpeg ABI, transport policy, owner and collector identity | Startup; identifying context accompanies each record |
| Selected SSH TCP bytes/ACK progress, retransmissions, RTT/variation/minimum, unsent/unacknowledged data, queues, congestion window, timeout/backoff and identity | Every 1 second |
| Owned Waypipe/SSH/Chrome process CPU and RSS | Every 2 seconds |
| Host CPU/memory/I/O pressure, load, interface bytes/errors/drops | Every 5 seconds |
| Existing validated codec context snapshots | On change, checked every 2 seconds |
| Route/link state, physical route to a reported direct endpoint, and selected peer's passive Tailscale status | Every 10 seconds, written on change |
| Existing Wi-Fi signal/rate/retry/failure observations and viewer Niri output mode | Every 10 seconds, written on change |
| Start, reset linkage, owner exit, collection failure/gaps, counter/clock discontinuities and optional mark | Events |
| Compact statistics and incident observations | Each minute and orderly collector exit |

New records use schema version 2 JSONL with a context header in every chunk.
Headers preserve identities, boot ID and the latest technical context. Compact
records retain wall and monotonic timestamps; the reader reconstructs UTC and
identity. Schema 1 history remains readable, and existing collectors accept
the new filenames during overlapping use.
Selection may inspect argv/environment locally, but only allowlisted technical
fields are persisted. URLs, page text, screenshots, notification text, full
argv and environment dumps are excluded. Codec snapshots describe successful
hardware/software contexts, not frames or measured presentation latency.

Wi-Fi reads existing association statistics without scans or elevated
permissions. Unsupported or permission-denied values remain unavailable.
SSID and raw AP MAC addresses are excluded; a process-salted association token
identifies a roam within an attempt. Interface counters are host-wide and may
include unrelated applications. Niri records contain allowlisted output
mode/refresh fields, not window titles. Output mode availability is context,
not a universally reliable DPMS power measurement.

The primary TCP reader uses an exact, unprivileged Linux SOCK_DIAG query and
length-checks TCP_INFO. Known zero counters remain zero; absent newer fields
remain unavailable. A bounded `ss` fallback records provenance and handles its
intentionally suppressed zero retransmission counter. Other omitted optional
text fields remain unavailable.

TCP values cover the selected **SSH connection**, including its Waypipe and
notification forwards. Shared SSH masters can also carry unrelated channels.
If the viewer cannot prove a unique owned TCP socket, its TCP measurement is
unavailable. Browser-side four-tuple observations remain connection-level data;
they do not prove that a multiplexed connection carries only this attempt.
Missing fields and ambiguous identities are unavailable rather than zero.
Counters are differenced only for the same socket identity; first samples and
counter resets do not become traffic spikes.

RTT/variation are TCP's smoothed estimates, not raw packet delay or video
jitter. The eligible RTT distribution requires acknowledged-byte progress and
a recent ACK indicator. Idle/outstanding/unknown activity is reported
separately so stale idle estimates cannot dominate a percentile. Schema 1 RTT
is labeled legacy with freshness unavailable. Results returned by older helpers
that do not report ACK eligibility carry the same freshness limitation, even
when the querying launcher is newer. Histograms use 1 ms bins capped
at 60000 ms; percentiles are approximate. Traffic is observed TCP data volume,
not available capacity or a bandwidth speed test.

## Incident analysis

One bounded streaming analyzer supports raw queries, minute summaries and the
live view. It reports observations, not an automatic root cause:

- RTT above both 2.5 times the path baseline and 30 ms above it.
- TCP deviation above both 15 ms and half the baseline RTT.
- Retransmission counter increases, including the first known zero-to-one.
- Outstanding data without acknowledged-byte progress for at least 2 seconds.
- Reported path/physical-route changes, Wi-Fi association changes/TX failures,
  interface error/drop increases, CPU pressure and recording gaps.

RTT rules warm up with 20 eligible observations. The rolling baseline uses the
lower tenth percentile of the last 300 eligible observations and resets on
path changes and socket discontinuities. An isolated retransmission is
recovery activity, not proof of packet loss or visible harm. Receiver-window
and send-buffer limitation counters remain in raw records for deeper analysis.
No flag means none in the available evidence; it does not establish that a
session was hitch-free.

Context before the requested window is carried forward with its observation
time. Queries preserve endpoints and attempts separately. Each summary retains
up to 200 incident flags; the human report shows the latest 20 and states when
earlier retained flags are omitted. It separately warns when the retained list
reaches its cap. Narrow the window for more detail. Use the
timeline alongside CPU/pressure observations and events. Exact input-to-display
latency and frame pacing need further instrumentation; sampling can miss
shorter hitches.

CPU is a per-process interval percentage, where one fully busy core is 100%.
Processes that exit between samples may be missed. RSS values are summed in
the machine-readable summary and can double-count shared pages. Process trees
are capped at 512 entries with an explicit limit flag. These observations can
support a network explanation but cannot by themselves establish the cause of
a visible hitch.

Tailscale observations use cached local status, with no ping, netcheck or
bandwidth test. A home DERP region alone does not prove relay traffic. The
recorder preserves an unknown path when the passive data cannot identify it.

## Storage and failure behavior

Each endpoint writes locally under
`${XDG_STATE_HOME:-~/.local/state}/remote-chrome/diagnostics/`. The browser's
collector is embedded in the existing secure bootstrap sent over Waypipe SSH;
there is no permanent service, recurring SSH polling or additional runtime
module installation. It stops when the captured owner PID/starttime exits or
changes. Reset creates a fresh shared attempt ID and links its predecessor.
The collector never signals the browser or authorizes transport cleanup.

The diagnostics directory is mode 0700; files are mode 0600. Directory traversal,
foreign ownership, unsafe permissions, symlinks and hardlinked log files are
refused. The existing `remote-chrome` state parent may be readable but must be
owned and not writable by others. A host-wide nonblocking lock serializes
quota decisions. Each open chunk holds a separate lock and reserves 4 MiB of
the quota without preallocation. Chunks close at 4 MiB or one hour; closed
chunks from still-active sessions are eligible for cleanup.

Cleanup removes aged chunks and, if necessary, the oldest closed chunks until
the next reservation fits. It never unlinks an open writer. If active writers
occupy the available quota, the collector skips recording and reports a gap
when writing resumes. Filesystem/tool failures are advisory and do not prevent
Chrome, notifications or forwarding from running. Tool calls have bounded
time and output, and missing optional tools remain unavailable.

Pruning runs during recording and on later launch/diagnostics operations.
An open chunk can retain up to one hour of older observations at the retention
boundary. When no collector or command runs, cleanup is lazy; there is no timer.
The 1 GiB cap applies to the managed log store, excluding explicit exports and
pre-existing notification/USB-IP operational logs. A directory-entry limit
also prevents unbounded numbers of tiny files.

Raw detail and minute summaries share that cap and the 30-day retention limit.
Aged closed chunks are removed first. Under quota pressure, closed raw detail
is removed before minute summaries, preserving longer context where possible.
Reports show actual retained chunk coverage and size: 30 days is a limit, not
a guarantee. Queries longer than two hours prefer minute summaries per attempt,
with raw fallback for older attempts and the latest unfinished minute. Boundary minutes may overlap the requested
window; the output labels the resolution. `--resolution raw` or `minute`
overrides selection. Raw and minute samples are never counted twice. Queries
declare partial results if bounded scanning or attempt limits are reached.

## Deployment and acceptance

Select the new launcher before applying diagnostics keys: older launchers
reject unknown configuration keys. Keep the preceding payload for rollback,
remove the new keys before selecting it, and let the user deliberately reset
or launch when ready. Current tunnels retain their existing bootstrap.

Fixture checks cover lifecycle, reset policy, retention, concurrency and
missing/unsafe observations. A short passive sampler sanity check establishes
that collection works and has bounded cost; it is not a transport comparison.
Daily use supplies the soak. No new bitrate, codec or compression matrix is
required for diagnostics delivery.
