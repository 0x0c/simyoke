**English** · [日本語](BE-0421-xcuitest-crash-report-scenario-artifact-ja.md)

# BE-0421 — Copy the iOS runner's crash report into the failed scenario's run directory

<!-- BE-METADATA -->
| Field | Value |
|---|---|
| Proposal | [BE-0421](BE-0421-xcuitest-crash-report-scenario-artifact.md) |
| Author | [@0x0c](https://github.com/0x0c) |
| Status | **Implemented** |
| Tracking issue | [Search](https://github.com/bajutsu-e2e/bajutsu/issues?q=is%3Aissue+label%3Aroadmap-tracking+in%3Atitle+"BE-0421") |
| Implementing PR | [#1999](https://github.com/bajutsu-e2e/bajutsu/pull/1999) (units 1-6) |
| Topic | Platform support |
| Related | [BE-0361](../BE-0361-ios-ci-simulator-diagnostics/BE-0361-ios-ci-simulator-diagnostics.md), [BE-0319](../BE-0319-xcuitest-cold-spawn-resilience/BE-0319-xcuitest-cold-spawn-resilience.md), [BE-0415](../BE-0415-driver-call-trace-per-scenario/BE-0415-driver-call-trace-per-scenario.md) |
<!-- /BE-METADATA -->

## Introduction

The resident XCUITest runner sometimes dies mid-scenario, and the run pipeline's crash-recovery
retries sometimes exhaust without a recovery. When that happens, `bajutsu run` builds the
scenario's failure message from the crash error alone. It names the fact that the runner died, but
it carries no runner-log path, no log content, and no mention of the report macOS itself writes for
a process that faults. Two pieces of direct evidence do exist on disk at that moment: the runner's
own captured output, and — when the host process itself faulted — the crash report macOS wrote for
it. Neither reaches the scenario's own run directory (`runs/<run_id>/<sid>/`), where every other
piece of that scenario's evidence already lives. This item copies both into that directory. A
contributor debugging a red scenario then finds the runner's own failure evidence next to the
screenshots and element trees the same run already produced — no digging through warning-level
logs for a path, no re-running the scenario with extra diagnostics enabled just to see what the
runner process actually did.

## Motivation

[BE-0361](../BE-0361-ios-ci-simulator-diagnostics/BE-0361-ios-ci-simulator-diagnostics.md) already
built a layered diagnostics collection for the iOS lanes of continuous integration (CI). Its first
layer — a result bundle per runner spawn, a bounded stall-time probe — is opt-in, behind an
environment variable a CI workflow sets. Its other two layers are not: a composite GitHub Actions
step already runs on every iOS job, unconditionally, sweeping the macOS host's own crash-report and
log stores (`.github/actions/collect-ios-diagnostics/action.yml`). So CI already gathers a runner
crash report today, in the common case.

That collection still leaves two gaps this item closes. First, it is job-wide, not scenario-wide:
it tars up everything `~/Library/Logs/DiagnosticReports` held for the whole job, so a job that ran a
dozen scenarios gives a contributor no way to tell which crash report belongs to which failing
scenario, if more than one crashed. Second, it is CI-only shell code with no counterpart in
`bajutsu` itself, so a developer reproducing a crash with a local `bajutsu run` gets nothing at
all — not even the pointer BE-0319's own runner-log capture already writes to a log file, since
that pointer never reaches the scenario's own failure message or report; it reaches only a
warning-level log line
([`_runner_log_hint`](../../bajutsu/common/platform_lifecycle/environments/xcuitest/xcuitest_environment.py)),
which a failed run may never have surfaced.

The crash report itself exists because XCUITest's host process (`xcodebuild test-without-building`)
is an ordinary macOS process. When it terminates on a fault rather than an orderly exit — as
opposed to exiting with a test-failure code, the far more common shape a stalled screenshot service
produces — the operating system writes a `.ips` crash report under
`~/Library/Logs/DiagnosticReports`, naming the terminating signal and, for system frames, a
symbolicated stack trace. It is the one piece of direct evidence a genuine process fault produces,
narrower and rarer than the ordinary "the channel stopped answering" crash this item's own scope
otherwise covers.

This item's observable outcome: a scenario that fails on an exhausted crash retry gains a
`crash-diagnostics/` subdirectory under its own `runs/<run_id>/<sid>/`, holding a bounded tail of
the runner's captured output and, whenever macOS wrote one for the `xcodebuild` process, its `.ips`
crash report. The scenario's own failure string also names that subdirectory, so a contributor reading
the failure never has to already know it exists. Today neither the subdirectory nor that mention
exists at all.

## Detailed design

### A method every environment defines, following the shape the crash-recovery layer already uses

`RunEnvironment`
([`bajutsu/common/platform_lifecycle/protocols/run_environment.py`](../../bajutsu/common/platform_lifecycle/protocols/run_environment.py))
is a structural `Protocol`. No concrete environment subclasses it, so a method's body there binds
to nobody — every "no-op by default" method on it, `request_device_replacement` and
`replaced_device` (BE-0354) included, is written out separately in each concrete class:
`_DeviceEnvironment` ([`ios.py`](../../bajutsu/common/platform_lifecycle/environments/ios.py),
shared by the Simulator XCUITest backend and the fake test backend), `WebEnvironment`
([`web.py`](../../bajutsu/common/platform_lifecycle/environments/web.py)), and
`AndroidEnvironment`
([`android_environment.py`](../../bajutsu/common/platform_lifecycle/environments/android/android_environment.py)).
This item adds `take_crash_snapshot()` to the protocol's declared shape and to all three of those
classes, each handing back a thunk that answers with nothing — a real, callable no-op rather than a
null, the shape `bridge_collector` already uses on this same surface:

```python
def take_crash_snapshot(self) -> Callable[[], list[tuple[str, bytes]]]:
    """Hand the releasing lease the crash evidence this environment captured, and forget it."""
    return lambda: []
```

It hands back a thunk rather than the bytes because the two halves resolve at different times, and it
*takes* rather than reads because the environment is shared across scenarios while the evidence is one
scenario's — both argued below. `XcuitestEnvironment` overrides it, the same way it already overrides
`_DeviceEnvironment`'s `request_device_replacement`.

### Snapshotting at crash detection, not reading live state later

The pipeline's crash-recovery retry loop, `_run_one_impl`
([`bajutsu/common/runner/pipeline.py`](../../bajutsu/common/runner/pipeline.py)), releases the
crashed lease back to the pool — `free.put(udid)` — inside `_run_on_lease`'s own `finally`,
*before* the loop decides whether to retry. A `crash_artifacts()` that read `self._runner_log` /
`self._runner_proc` lazily, only when the loop finally gives up, would race a concurrent worker on
a multi-device run: that worker's own next lease can reuse this exact environment instance (`pool.py`
keys its warm cache by udid) and respawn a fresh runner on it before this scenario's retry loop ever
reads back the old crash's evidence — clearing `self._runner_log` and `self._runner_proc`
(`_discard_runner`, `xcuitest_environment.py:1142`) and, for a healthy respawn, leaving nothing
crash-related behind at all.

So `XcuitestEnvironment` captures eagerly instead, at the last moment a crashed runner is still this
scenario's. That moment is **`end_lease`**, not the discard: on a mid-run crash the pipeline's only
action is `lz.release()`, which reaches `pool.release()` and, for a Simulator whose resident the pool
keeps warm, `XcuitestEnvironment.end_lease` — which terminates the app and nothing else. The discard
that would otherwise notice the dead runner does not run until the *next* bring-up, inside `start()`,
which is after `free.put(udid)` and never at all on a run whose retries are already spent. A capture
hooked there would be both mistimed and, for `crash_retries: 0`, absent.

`_discard_runner` carries the same hook as a second site, because a real-device lease, an actuator
switch, a pool shutdown, and a failed lease's own cleanup all release through `teardown` instead. Both
sites share one predicate, `_runner_crashed()`, which asks exactly the two signals `_runner_alive`
declares a mid-run crash on: the `xcodebuild` leader exited, *or* its capture carries the marker
saying the XCTest run ended while the process lingers. The second signal is what the older, narrower
formulation of this design missed — it gated on `_discard_runner`'s own `crashed` flag, which is set
only inside the `exited is not None` branch, so the dominant CI shape (a stalled screenshot service,
which leaves `poll()` answering `None` throughout) would have captured nothing at all. The
`_discard_runner` hook stays gated on `warn_on_crash` on top of the predicate, because a
cold-spawn-failure discard (`_spawn_cold_with_retry`, and the `discard=lambda:
self._discard_runner(warn_on_crash=False, keep_log=True)` wired into the driver) is explicitly *not*
a mid-run crash and must not overwrite one's evidence with its own mid-retry-loop.

One crash shape stays out of reach and is declared rather than papered over: a channel that stops
answering while `xcodebuild` runs on and writes no run-ended marker raises a `BackendCrashError` that
neither liveness signal sees. That is the wedged-Simulator degradation BE-0354 escalates to a
replacement device and BE-0361's stall probe already captures on its own trigger, so this item leaves
it to them rather than paying a bounded log read on every lease — green ones included — to cover it.

At each site, before `self._runner_proc = None` clears the handle, two different things get captured
at two different times, because the runner log and the `.ips` crash report have different failure
windows to race:

- **The runner's own captured output**, read *eagerly, right there* as a bounded tail from
  `self._runner_log` — the same streaming `deque(fh, maxlen=...)` read `_runner_log_hint` already
  uses to avoid materializing the whole (high-volume) capture (`xcuitest_environment.py:1134-1137`),
  just against a larger cap than that twenty-line hint, since this artifact exists to exceed it. This
  one races `free.put(udid)`, so it cannot wait.
- **The match criteria for the `xcodebuild test-without-building` process's own macOS crash
  report**, frozen *eagerly, right there* too, but the report itself is read only later. `_spawn_runner`
  already records the process handle as `self._runner_proc` right after `Popen` returns
  (`xcuitest_environment.py:769`); this item adds a spawn timestamp next to it,
  `self._runner_spawned_at = time.time()`, since `Popen` exposes no start time of its own. Both are
  copied into a dedicated pair, `self._last_crash_report_match`, frozen at this exact moment —
  distinct from the live `self._runner_spawned_at` / `self._runner_proc`, which a respawn on this
  same environment instance would otherwise overwrite before anyone reads them back.

Both entries are cached as `self._last_crash_artifacts` (a mix of the already-read log tail and the
frozen match criteria); `crash_artifacts()` reads that cache when the pipeline calls it — the runner
log unchanged, but the `.ips` lookup happens **only then**, against
`self._last_crash_report_match`. That deferral is deliberate: macOS's `ReportCrash` writes a `.ips`
report *asynchronously*, after the faulting process is already gone, and symbolication can take
anywhere from hundreds of milliseconds to several seconds — so listing `DiagnosticReports`
synchronously at the capture site would usually find nothing yet. The pipeline's own call site
runs only once the retry loop has already given up, seconds later and off the respawn path, which
gives `ReportCrash` the time it needs. The frozen match criteria are what make that deferral safe:
by the time `crash_artifacts()` runs, `self._runner_spawned_at` / `self._runner_proc` may already
belong to a different, later spawn, but `self._last_crash_report_match` still names the crashed one.

The lookup itself lists `~/Library/Logs/DiagnosticReports` for a `.ips` file named `xcodebuild-*`
whose modification time falls at or after the frozen spawn timestamp; the name-and-time match keeps
the sweep from picking up an unrelated `xcodebuild` invocation's report left on the same host. When
the file's own JavaScript Object Notation (JSON) header names a `pid`, matching it against the frozen
pid narrows a multi-worker host's several concurrent `xcodebuild` processes further, but a report
whose header cannot be parsed is still taken on the name-and-time match alone. The listing is capped
at the first few matches, mirroring BE-0361's own per-capture caps, so a runner that keeps
crash-looping cannot make one scenario's evidence write unbounded. Both entries stay best-effort
throughout, matching the posture `_result_bundle_path` and `_capture_stall` already take for their
own captures: a missing log, an unreadable `DiagnosticReports` directory (any platform but macOS, or
a sandboxed CI runner with no read permission), or a report that never showed up in time all resolve
to that entry being skipped, never to a raise.

This ordering is what a released, then re-leased, environment cannot undo: the snapshot is already
taken and cached before `free.put(udid)` runs, so a later respawn's own state changes never touch it.

Caching on the environment is only half the story, though, because the environment outlives the
scenario: the pool keeps it warm per device, so evidence left there is visible to — and destroyable
by — whichever scenario leases that device next. Under `workers > 1` that cuts both ways. A second
crash on the shared environment would overwrite a first scenario's snapshot before its retry loop read
it, and a merely *healthy* intervening lease would be enough to strand it, since a scenario's own loop
can spend minutes not reading (a forced-erase prep that hit a `DeviceTimeout`, BE-0374).

So the evidence does not stay on the environment at all: **ownership moves to the releasing lease.**
`take_crash_snapshot()` hands back a thunk and clears what it handed over, and `pool.py`'s `release()`
calls it once, after the teardowns that let the environment observe a crash and before `free.put(udid)`
offers the device to anyone else. The `Lease` then reads its own copy. That is the rule
`video_start_stalled` already follows, and for the same reason `pool.py` states for it: a `workers > 1`
run must never read one scenario's signal on another's retry. The thunk still defers its `.ips` lookup
— what it no longer does is read state a later lease can change.

The capture itself is per spawn, at most once, tracked by `_crash_snapshotted`. Two sites observe one
crash — the release, and any later discard that still finds the dead runner — and a second capture
would regenerate evidence the releasing lease had already taken, putting it back on the environment
for the next scenario to inherit.

### Reaching the scenario's own writer

`_run_one_impl` already holds `sid`, the scenario's own evidence-directory name, and it reaches the
crashed attempt's environment through the `Lease` that attempt ran on. Not through `lz`, though: that
variable is reset to `None` at the top of every attempt, so a loop that ends on a lease-time crash, or
on a forced-erase prep that timed out (BE-0374), would find it empty while an *earlier* attempt's
environment still holds the evidence this scenario failed for. The loop therefore keeps the crashed
lease in a variable of its own, `crashed_lz`, assigned in the `except BackendCrashError` handler.
`Lease` ([`bajutsu/common/runner/types.py`](../../bajutsu/common/runner/types.py)) gains one more
field, given a module-level no-op default the way `relaunch`/`control` neighbors already are:

```python
def _no_crash_artifacts() -> list[tuple[str, bytes]]:
    return []

# on Lease:
crash_artifacts: Callable[[], list[tuple[str, bytes]]] = _no_crash_artifacts
```

`pool.py`'s `lease()` closure holds a lease-local thunk beside the `Lease` it builds, starting at that
same no-op and reassigned by `release()` from `lease_env.take_crash_snapshot()`. Unlike
`request_device_replacement`, which is wired as a bound method reference because the *device* is what
it acts on, this one must not stay bound to the environment: the environment is shared across
scenarios, and the evidence is one scenario's.

The pipeline asks for it once: where the retry loop gives up and builds the scenario's terminal
`RunResult(ok=False, ...)` for a backend crash it could not recover — the `if device_timeout ...
elif ... else:` chain that already sets `failure` for that case. Right after that chain, when
`crashed_lz is not None` and `self._artifacts()` returns a writer, the pipeline calls
`crashed_lz.crash_artifacts()` before returning that `RunResult`. After the chain rather than inside
its crash branches, because a loop that ended on a device-preparation timeout was still recovering
from a crash whose evidence explains it too, and a scenario that never crashed leaves `crashed_lz`
`None` and reaches no write at all. For each `(name, content)` pair it writes through
`writer.write_text(f"{sid}/crash-diagnostics/{name}", content.decode(errors="replace"))` — the same
per-scenario write shape
[BE-0415](../BE-0415-driver-call-trace-per-scenario/BE-0415-driver-call-trace-per-scenario.md)
already uses for `driver_trace.json` — and it uses `write_text` rather than `write_bytes` so the
free-text scrub (BE-0331) still runs over content this item did not author. It also appends the
subdirectory's path to `failure` itself, so the failure string a contributor actually reads points
at the new evidence directly, closing the gap Motivation named. A logger — never a raise — catches
a write problem, such as a full disk or a permissions error, because a diagnostic artifact must
never turn an already-decided failure into a different, unrelated one.

### Why once, not per attempt

BE-0361's stall probe fires at every crash: it investigates whether the *retry itself* is worth
trying. This item fires once, only when the retry loop has already given up. Its own evidence — the
runner's crash — names the same fact on every attempt of one crash-looping scenario. Copying it
after each attempt would leave a recovered scenario carrying crash evidence for a failure its own
final result no longer reports. A scenario that recovers within its retry budget passes, and a
passing scenario needs no crash report.

### Cost on every other backend

Android's and the web backend's environments return `[]` from their own `crash_artifacts()`, so the
pipeline's write step becomes a no-op list iteration, unchanged from today. The iOS backend itself
only ever constructs on macOS, so the `DiagnosticReports` sweep never runs on a Linux host at all.
Nothing in this item changes what a non-macOS run captures.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Gate the capture behind an environment variable, matching BE-0361's `BAJUTSU_XCUITEST_RESULT_BUNDLES` / `BAJUTSU_STALL_DIAGNOSTICS` | This item's own gap is the opposite of BE-0361 layer 1's: CI already gathers a crash report unconditionally (layers 2–3), and what is missing is a local-run counterpart and per-scenario attribution — neither served by an opt-in CI variable. The capture also runs only once a scenario is already ending in failure, so its cost is one bounded directory listing and one file copy — not a standing overhead worth gating. |
| Also copy the `.xcresult` result bundle BE-0361 unit 1 can produce | That bundle is already BE-0361's own opt-in artifact, behind `BAJUTSU_XCUITEST_RESULT_BUNDLES`, and is not bounded the way a `.ips` report is. Making it default-on here would add an unbounded artifact for content the `.ips` report and the runner's own log already summarize. Left for a future item if this one's evidence proves insufficient. |
| Capture at every crash-recovery attempt, not only the one that exhausts the budget | Rejected in *Why once, not per attempt* above. It duplicates for a scenario that eventually recovers. The scenario's crash-exhausted `RunResult` — the one place this evidence explains — exists only once, at the end of the loop. |
| A full `xcrun simctl diagnose`, `log collect`, or `sysdiagnose` sweep on crash | Rejected for the reason BE-0361's own *Alternatives considered* already gave and measured: `simctl diagnose` alone runs to 22–78 MB and about 15 seconds per booted device, and a `.logarchive` of a job's whole window runs to hundreds of megabytes. The process's own `.ips` report already names the terminating signal and, for system frames, a symbolicated stack trace, at a cost of a few kilobytes. |

## Progress

> Keep this current as work proceeds. The checklist mirrors the MECE work breakdown in
> *Detailed design* (one box per unit of work); the log records what changed and when
> (oldest first), linking the PRs.

Log:

- [#1999](https://github.com/bajutsu-e2e/bajutsu/pull/1999) — Units 1-6. Shipped the whole item.
  Two corrections to this design landed with it, both found by the self-review pass: the capture site
  is `end_lease` plus `_discard_runner`, not the discard alone (a mid-run crash releases without one,
  and with `crash_retries: 0` no discard ever runs), and the guard is a `_runner_crashed()` predicate
  rather than the discard's own `crashed` flag, which misses the shape where `xcodebuild` lingers past
  its ended test run. Ownership of the snapshot moves to the releasing lease
  (`take_crash_snapshot`), because the environment is kept warm per device and evidence left on it is
  both readable and erasable by the next scenario to lease that device. The wedged-but-alive crash
  shape is left to BE-0354 / BE-0361 and declared as such in `docs/ci.md` rather than implied covered.

- [x] Unit 1 — `take_crash_snapshot()` on the `RunEnvironment` protocol shape and, returning a thunk
      that answers `[]`, on `_DeviceEnvironment` (ios.py), `WebEnvironment`, and `AndroidEnvironment`.
- [x] Unit 2 — `XcuitestEnvironment`'s override: a spawn timestamp (`self._runner_spawned_at`)
      recorded alongside `self._runner_proc`; a `_runner_crashed()` predicate asking the two signals
      `_runner_alive` declares a crash on (the leader exited, or it lingers past a run its capture says
      has ended); the snapshot taken on that predicate at both sites a crashed runner is let go —
      `end_lease` (the crash release path) and `_discard_runner`, additionally gated on `warn_on_crash`
      so a cold-spawn failure never overwrites a real crash — at most once per spawn
      (`_crash_snapshotted`), and handed to the releasing lease by `take_crash_snapshot()`, which clears
      what it hands over so no later scenario inherits or destroys it. The snapshot is an eager
      bounded-tail runner-log read cached as `self._last_crash_artifacts`, plus the `.ips` match
      criteria (spawn timestamp, pid) frozen into `self._last_crash_report_match` before either can
      be overwritten by a later spawn; the name-and-time `DiagnosticReports` sweep for
      `xcodebuild-*.ips` is deferred to `crash_artifacts()`'s own call so `ReportCrash` has time to
      write and symbolicate, both best-effort and bounded.
- [x] Unit 3 — `Lease.crash_artifacts`, defaulted through a module-level `_no_crash_artifacts`, backed
      by a lease-local thunk that `pool.py`'s `release()` takes off the environment once, after the
      teardowns that let it observe a crash and before `free.put(udid)`.
- [x] Unit 4 — The `pipeline.py` call site: invoked once, at the crash-exhausted `RunResult`, on the
      `crashed_lz` the retry loop remembered rather than the per-attempt `lz`, writing each artifact
      through `RunArtifactWriter.write_text` under `f"{sid}/crash-diagnostics/"` and appending that
      path to the `failure` string, with a write failure logged rather than raised.
- [x] Unit 5 — Docs: the CI diagnostics section BE-0361 added to `docs/ci.md` (and its `docs/ja/`
      mirror) gains a note on this default-on, per-scenario counterpart.
- [x] Unit 6 — Tests: `XcuitestEnvironment`'s crash snapshot against a stubbed `DiagnosticReports`
      directory (the name-and-time match, the pid refinement, the cap, the missing-log and
      missing-report cases); a test that a second crash on a shared environment overwrites the first
      scenario's cached snapshot, pinning the documented limitation; a `pipeline.py` test asserting a
      crash-exhausted scenario's run directory gains `crash-diagnostics/` and its path in `failure`;
      a recovered-scenario test asserting it does not; an Android/web/fake-backend test asserting the
      call site's no-op default is unchanged.

## References

- [BE-0361](../BE-0361-ios-ci-simulator-diagnostics/BE-0361-ios-ci-simulator-diagnostics.md) — the
  CI-scoped diagnostics layers this item complements with a default-on, per-scenario one; also the
  source of the `simctl diagnose` and `log collect` costs *Alternatives considered* cites
- [BE-0319](../BE-0319-xcuitest-cold-spawn-resilience/BE-0319-xcuitest-cold-spawn-resilience.md) —
  the default-on runner-output capture this item copies into the scenario directory
- [BE-0354](../BE-0354-xcuitest-wedge-fastfail-device-replacement/BE-0354-xcuitest-wedge-fastfail-device-replacement.md) —
  `request_device_replacement`, the existing method whose per-class no-op shape this item's
  `crash_artifacts()` follows, and whose device-replacement escalation bounds the narrow
  shared-environment limitation *Detailed design* accepts
- [BE-0415](../BE-0415-driver-call-trace-per-scenario/BE-0415-driver-call-trace-per-scenario.md) —
  the per-scenario `RunArtifactWriter` write shape this item follows
- [`bajutsu/common/platform_lifecycle/environments/xcuitest/xcuitest_environment.py`](../../bajutsu/common/platform_lifecycle/environments/xcuitest/xcuitest_environment.py) —
  `_runner_log`, `_runner_proc`, `_discard_runner`, `_runner_log_hint`, the seams this item reads
- [`bajutsu/common/platform_lifecycle/environments/ios.py`](../../bajutsu/common/platform_lifecycle/environments/ios.py) —
  `_DeviceEnvironment`, whose own `request_device_replacement` no-op this item's default mirrors
- [`bajutsu/common/runner/pipeline.py`](../../bajutsu/common/runner/pipeline.py) — `_run_one_impl`,
  whose crash-exhausted `RunResult` is this item's one call site, and whose `_run_on_lease` releases
  the crashed lease before the retry loop decides whether to retry
- [`bajutsu/common/runner/pool.py`](../../bajutsu/common/runner/pool.py) — the `lease()` closure
  that wires `Lease.crash_artifacts` the same way it wires `request_device_replacement`
- [`bajutsu/common/platform_lifecycle/protocols/run_environment.py`](../../bajutsu/common/platform_lifecycle/protocols/run_environment.py) —
  the protocol `crash_artifacts()` joins, next to `request_device_replacement` and
  `replaced_device`
- [`bajutsu/common/evidence/sink.py`](../../bajutsu/common/evidence/sink.py) — `RunArtifactWriter`,
  the single write boundary (BE-0331) this item's artifacts cross
- [`.github/actions/collect-ios-diagnostics/action.yml`](../../.github/actions/collect-ios-diagnostics/action.yml) —
  BE-0361's already-unconditional `DiagnosticReports` sweep, whose job-wide scope Motivation
  contrasts with this item's per-scenario one
