# Headless notification study and acceptance index

Reconciled on **2026-10-03** against launcher commit `61f26bd`. The notification
endpoint is implemented; the dated feasibility report and roadmap below describe
earlier decisions, not pending implementation tasks. Current behavior is defined
in [usage](../../usage.md#notifications), [configuration](../../configuration.md),
and [architecture](../../architecture.md#notification-paths-and-protocol).

## Roadmap reconciliation

| Original milestone | Current disposition | Evidence |
| --- | --- | --- |
| 0: bidirectional action feasibility | Implemented and exercised with a disposable browser and private fixture | [Milestone 0 results](milestone0-results.json), commit `ff5ae7f` |
| 1: owned endpoint/protocol | Implemented in the single-file launcher; body/actions and optional markup negotiated by contract | [Implementation record](implementation.txt), protocol tests |
| 2: ownership/startup/cleanup | Implemented, with later hardening for identity checks, races, reset ordering, and interrupted startup | Launcher and lifecycle/generated-bootstrap tests through `61f26bd` |
| 3: diagnostics/regression/docs | Implemented: status, visible doctor probe, isolated tests, user docs | [Contributing](../../../CONTRIBUTING.md), current operational docs |
| 4: real-host acceptance/release | Bounded managed-session and desktop checks recorded; broader acceptance remains limited as described below | [Host results](host-results.json), [implementation and acceptance record](implementation.txt) |

At the `61f26bd` checkpoint, `scripts/check` passed 179 shell, 18 protocol, and
17 generated-bootstrap tests (214 total).
[CI run 37163950431](https://github.com/byebyebryan/remote-chrome/actions/runs/37163950431)
passed for that commit. Earlier counts and commit references in the historical
records remain evidence of their own checkpoints.

Automatic desktop takeover, multiple simultaneous owned headless endpoints,
images, persistence, inline replies, activation tokens, replay, and automatic
transport reconnect remain outside the supported scope. Original effort
estimates are historical estimates, not a forecast for work still outstanding.

## Acceptance boundaries

The 2026-10-02 record includes real managed SSH/Waypipe launches using the
installed launcher and a disposable Chrome profile, secure storage, supported
markup, a Starship/DMS notification and default click returning to the browser,
controlled reset, and exact cleanup.

Office handoff used Snap's normal user notification bus with Quickshell on a
disposable virtual compositor. It verified selected incoming stop, name release,
daemon takeover, and local delivery. This is not a full acceptance run in a
physical office desktop session. Failed runs were retained alongside successful
runs in the evidence.

The established daily browser profile was preserved. These records do not
establish acceptance on that profile after upgrade, unattended cold-boot SSH
reachability, wallet unlock after reboot, or fresh physical FIDO access after
reboot. CI uses fixtures and cannot establish those host/hardware properties.
Fresh YubiKey recovery observations also do not retroactively expand the
notification experiment's acceptance scope.

Source checkout, installed launcher, active bootstrap, CI, and manual acceptance
must be checked separately. Updating both launchers leaves existing browser
sessions intact; a deliberate reset activates a new bootstrap. For rollback,
choose a reviewed compatible commit after exact controlled cleanup, and verify
both installed artifacts. The rollback commit named in the historical record
belongs to that experiment, not a standing rollback recommendation.

## Historical records

| File | Role |
| --- | --- |
| [report.txt](report.txt) | Initial feasibility findings and compatibility references at `9c9256f` |
| [results.json](results.json) | Captured feasibility run evidence |
| [roadmap.txt](roadmap.txt) | Original approved milestone design and release boundary |
| [milestone0-results.json](milestone0-results.json) | Captured action-roundtrip prototype evidence |
| [implementation.txt](implementation.txt) | Dated implementation, host acceptance, and release checkpoints |
| [host-results.json](host-results.json) | Captured managed-host acceptance, including failed attempts |

JSON evidence is intentionally tracked. Reproduction produces fresh scratch
results; promote a new, reviewed record deliberately rather than overwriting a
historical snapshot. Study-local Python caches are ignored.

## Manual tools

These tools are retained for reproducibility. They are not installed or run by
the launcher or CI. The isolated tools use private buses, virtual compositors,
and disposable profiles; their `--password-store=basic` browser setting is
fixture-only and is never used by the production bootstrap.

| Tool | Scope and output |
| --- | --- |
| [study.py](study.py) and [endpoint.py](endpoint.py) | Initial private-bus backend experiment; results/logs in a fresh `/tmp/remote-chrome-notification-study-*` directory |
| [action_roundtrip.py](action_roundtrip.py) | Bidirectional prototype with fixture-injected actions; defaults to fresh scratch `results.json`; `--output PATH` explicitly selects another evidence path |
| [host_acceptance.py](host_acceptance.py) | Runs on the browser host with `--display-host` and `--browser-host` SSH aliases; uses the installed launcher and real user bus; writes fresh scratch evidence |
| [office_acceptance.py](office_acceptance.py) | Takes a disposable host-acceptance session name; performs real incoming stop/name handoff with a virtual Quickshell desktop |
| [acceptance_pointer.c](acceptance_pointer.c) | Optional virtual-pointer actuator; build prerequisites and fresh screenshot/target validation are described in the implementation record |

Read the [feasibility prerequisites](report.txt) before the isolated study:

```bash
python3 docs/studies/headless-notifications/study.py
python3 docs/studies/headless-notifications/action_roundtrip.py
```

For the real-host tools, inspect their help and the
[controlled-acceptance procedure](implementation.txt) before running them:

```bash
python3 docs/studies/headless-notifications/host_acceptance.py --help
python3 docs/studies/headless-notifications/office_acceptance.py --help
```

Real-host acceptance changes the selected browser session and normal user-bus
owner. It requires an isolated acceptance target with no existing host-keyed
YubiKey state, even when launched with `--no-yubikey`. Do not use a daily active
session as the acceptance target. The office tool rejects unexpected ownership
or outgoing managed sessions before performing its handoff.
