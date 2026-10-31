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
remote-chrome diagnostics snap --since 20m --export /tmp/chrome-incident.jsonl
```

Mark is optional. It stores a timestamp against the most recent local attempt
seen within five minutes, without a text field. An approximate incident time is
also sufficient. Summary queries work after the session ends, for windows up
to 30 days (`s`, `m`, `h`, `d`). `--session` narrows the selection; otherwise
all matching attempts to that target are included.

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
Exports are outside automatic retention. Queries have a 32 MiB input limit
per endpoint and report a refusal if the selected history exceeds it; shorten
the window. Export is a request to copy those technical observations, including
hostnames, addresses and process identities. Review it before sharing.

## Recorded data

| Observation | Frequency |
| --- | --- |
| Attempt ID, endpoint role, launcher/kernel/FFmpeg ABI, transport policy, owner and collector identity | Startup; identifying context accompanies each record |
| Selected SSH TCP bytes, retransmissions, RTT/variation, queues and socket identity | Every 2 seconds |
| Owned Waypipe/SSH/Chrome process CPU and RSS | Every 2 seconds |
| Existing validated codec context snapshots | On change, checked every 2 seconds |
| Local route/link state and selected peer's passive Tailscale status | Every 30 seconds, written on change |
| Start, reset linkage, owner exit, collection failure/gaps, counter/clock discontinuities and optional mark | Events |

Records use schema version 1 JSONL, UTC wall time, monotonic time and boot ID.
Selection may inspect argv/environment locally, but only allowlisted technical
fields are persisted. URLs, page text, screenshots, notification text, full
argv and environment dumps are excluded. Codec snapshots describe successful
hardware/software contexts, not frames or measured presentation latency.

TCP values cover the selected **SSH connection**, including its Waypipe and
notification forwards. Shared SSH masters can also carry unrelated channels.
If the viewer cannot prove a unique owned TCP socket, its TCP measurement is
unavailable. Browser-side four-tuple observations remain connection-level data;
they do not prove that a multiplexed connection carries only this attempt.
Missing fields and ambiguous identities are unavailable rather than zero.
Counters are differenced only for the same socket identity; first samples and
counter resets do not become traffic spikes.

CPU is a per-process interval percentage, where one fully busy core is 100%.
Processes that exit between samples may be missed. RSS values are summed in
the machine-readable summary and can double-count shared pages. Process trees
are capped at 512 entries with an explicit limit flag. Two-second sampling can
miss short hitches. These observations can support a network explanation but
cannot by themselves establish the cause of a visible hitch.

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
