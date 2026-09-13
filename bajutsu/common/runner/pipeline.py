"""Run every scenario through a device pool and write the run's report artifacts."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bajutsu.common.doctor import Score
    from bajutsu.common.drivers import base

from bajutsu.common.assertions import (
    AssertionResult,
    EvalContext,
    GoldenContext,
    SchemaContext,
    VisualContext,
    VisualEvidence,
)
from bajutsu.common.backends import (
    capabilities_for_run,
    device_replacement_supported,
    erase_precondition_supported,
)
from bajutsu.common.cancellation import CANCELLED_FAILURE, CancelSource, not_cancelled
from bajutsu.common.capability import capability_preflight
from bajutsu.common.config import Effective
from bajutsu.common.devices import errors as device_errors
from bajutsu.common.drivers import tracing
from bajutsu.common.drivers.base import BackendCrashError
from bajutsu.common.evidence import Artifact
from bajutsu.common.evidence.network import NetworkExchange, _no_transitions
from bajutsu.common.evidence.redaction import Redactor
from bajutsu.common.evidence.sink import RunArtifactWriter, prepare_run_dir
from bajutsu.common.orchestrator import (
    AlertGuardConfig,
    Clock,
    MailboxReader,
    ProgressFn,
    RunResult,
    SkippedCapture,
    push_interruption_policy,
    run_scenario,
    sanitize_source_stem,
    scenario_slug,
)
from bajutsu.common.orchestrator.evidence_rules import requested_intervals
from bajutsu.common.orchestrator.types import _no_network
from bajutsu.common.report import (
    ScenarioPlanSource,
    git_revision,
    run_provenance,
    scenario_render_inputs,
    scenario_source_meta,
    write_report,
)
from bajutsu.common.runner.mailbox import build_mailbox_reader
from bajutsu.common.runner.recovery import (
    CrashRecoveryBudget,
    RunCrashRecoveryBudget,
    _default_crash_recovery_budget,
    _default_crash_retries,
    _default_run_crash_recovery_budget,
)
from bajutsu.common.runner.types import AlertGuardFor, Lease, LeaseFn
from bajutsu.common.scenario import (
    Scenario,
    UncoveredSystemAlertLocale,
    dump_scenario_file,
    redact_totp_secrets,
)

# Re-exported from `recovery` (BE-0334): the crash-retry count/budget bookkeeping now lives there so
# the on-device conformance harness drives the same recovery and the two cannot drift. Kept importable
# here for callers (and tests) that read the defaults through the pipeline's public surface.
__all__ = [
    "_default_crash_recovery_budget",
    "_default_crash_retries",
    "_default_run_crash_recovery_budget",
]

_logger = logging.getLogger(__name__)

# Subdirectory of a scenario's own evidence directory holding the backend's crash evidence — its
# captured output, and the host crash report for a process that faulted (BE-0421). Named once here
# because the failed scenario's failure string points at it as well as writing into it.
_CRASH_DIAGNOSTICS_DIR = "crash-diagnostics"


def _evidence_sid(i: int, s: Scenario) -> str:
    """The `NN-slug` evidence-dir id for scenario `s` at run-order index `i` (BE-0417).

    The slug is `s`'s own source file's stem, sanitized for safe use in an unescaped HTML
    attribute / URL path segment, or `scenario_slug(s.name)` for a scenario with no known source
    file (e.g. one built directly, outside the file loaders).
    """
    slug = (
        sanitize_source_stem(s.source_stem) if s.source_stem is not None else scenario_slug(s.name)
    )
    return f"{i:02d}-{slug}"


def _resolve_now(clock: Clock | None) -> Callable[[], float]:
    """The monotonic-seconds callable a run's clock resolves to, real time when `clock` is None.

    The one place this resolution lives, so every `_ScenarioRunner._now` call across the run reads
    the same clock — `RunCrashRecoveryBudget` needs no clock of its own; each `run_one` call times its
    own retry loop locally (via `_now`) and only ever reports a finished elapsed span to it.
    """
    return clock.now if clock is not None else time.monotonic


def _write_network(
    timed: list[tuple[NetworkExchange, float]],
    writer: RunArtifactWriter,
    sid: str,
    *,
    wall_offset_s: float,
    provider: str = "collector",
) -> Artifact | None:
    """Write a scenario's observed exchanges to <sid>/network.json (redacted).

    Each exchange gets an absolute wall-clock `startedAt` (epoch seconds), on the same footing as a
    step's `started_at` (BE-0348): the collector stamps a monotonic receive time, which
    `wall_offset_s` (`RunResult.wall_offset_s`) converts through the scenario's own anchor pair, and
    the receive time is ≈ completion, so the start is `wall(received) - duration`. A report subtracts
    `RunResult.video_anchor_s` at render time to place the exchange on the recording's timeline.
    `wall_offset_s` is keyword-only: it and `RunResult.video_anchor_s` are both floats of similar
    magnitude a caller could otherwise pass in the wrong slot with no type error.

    Goes through the sink's exchange entry point so BE-0130's default header masking runs inside the
    boundary rather than being left to this caller (BE-0331).
    """
    if not timed:
        return None
    data: list[dict[str, Any]] = []
    for ex, received in timed:
        d = ex.model_dump(by_alias=True, exclude_none=True)
        d["startedAt"] = round(received + wall_offset_s - (ex.duration_ms or 0.0) / 1000.0, 3)
        data.append(d)
    name = f"{sid}/network.json"
    writer.write_exchanges(name, data)
    return Artifact(name, "network", provider)


@dataclass(frozen=True)
class _ScenarioRunner:
    """One run's shared context, applied to each scenario in turn (BE-0172).

    Promoted from the ``run_one`` closure that lived inside ``run_all``: the values every scenario
    needs — the resolved config, the lease factory, the per-run redactor / mailbox / capability set,
    and the output knobs — are explicit read-only fields instead of captured free variables. This
    makes ``run_one`` legible and unit-testable in isolation, and makes explicit exactly which state
    each worker touches: the runner is frozen (no attribute rebinding) and holds no per-scenario
    mutable state — each ``run_one`` keeps its scenario state local — so it is shared across
    ``ThreadPoolExecutor`` workers as-is, precisely as the closure's captured state was.
    """

    eff: Effective
    lease: LeaseFn
    redactor: Redactor
    mailbox: MailboxReader | None
    caps: frozenset[str] | None
    total: int
    clock: Clock | None = None
    alert_guard: AlertGuardConfig | None = None
    alert_guard_for: AlertGuardFor | None = None
    run_dir: Path | None = None
    bindings: Mapping[str, str] | None = None
    progress: ProgressFn | None = None
    baselines_dir: Path | None = None
    schemas_dir: Path | None = None
    actuator: str | None = None
    golden_context: GoldenContext | None = None
    # The run's resolved udid spec (the provider's `udid_spec`): a WebDriver URL routes the run to the
    # live XCUITest environment, so the preflight below narrows to that transport's set — keyed on the
    # same signal `environment_for` routes on (BE-0238). "booted" (the default) is never a URL.
    udid_spec: str = "booted"
    # Per-scenario actuator resolver (BE-0240): when set, each scenario's actuator (and thus its
    # capability set for the preflight below) is resolved from the scenario itself, rather than one
    # fixed `actuator` for the whole run. The pool's `lease()` resolves the *same* pure function, so
    # the actuator preflighted here is the one the lease builds. None keeps the fixed-`actuator` path
    # (the cross-browser matrix, tests driving a lease directly).
    resolve_actuator: Callable[[Scenario], str] | None = None
    # Emit the app's entry-screen convention score once per run (the first scenario's freshly launched
    # driver), folding `doctor`'s Ready/Partial/Blocked tell into the run so CI needs no separate
    # `doctor` invocation that would cold-spawn a second XCUITest runner. Purely diagnostic and off the
    # verdict path (prime directive 1): it never changes a scenario's result or the run's exit code.
    # None (the default) keeps every existing run silent.
    on_score: Callable[[Score], None] | None = None
    # How many times to re-run a scenario whose backend crashed mid-run (base.BackendCrashError)
    # before failing it — the dead lease is discarded and a fresh one leased (a cold respawn) each
    # retry. A crash is backend infrastructure, not a verdict (prime directive 1); bounding the
    # retries keeps a genuinely crash-inducing scenario failing loudly (BE-0049). 0 disables it.
    # The retry replays the *whole* scenario on a respawned (not erased) app, so it is safe only for
    # scenarios idempotent up to the crash point — one with a persistent side effect before the
    # crash (e.g. a server-side write) can fail, or pass against the wrong state, on replay.
    crash_retries: int = 1
    # A wall-clock ceiling (seconds) on the total time this scenario may spend respawning after a
    # backend crash — see `_default_crash_recovery_budget`. None (the default) is unbounded: the
    # count-based `crash_retries` is then the only cap, unchanged. Set, it stops recovery once the
    # budget is spent so a never-recovering runner can't burn crash_retries x the cold-startup ceiling.
    crash_recovery_budget: float | None = None
    # One `RunCrashRecoveryBudget` shared across every scenario in the run (constructed once in
    # `run_all`, ahead of every `run_one`), so a device that keeps degrading fails the run once its
    # cumulative recovery time is spent, rather than each new scenario silently re-spending its own
    # `crash_recovery_budget` against the same device. Its own `.budget` field (not a second field
    # here) is what the failure message below reads, so there is one source of truth for the
    # configured seconds rather than two fields a direct construction could desync. The default is an
    # unbounded budget on the real clock — the "no run-level cap" case every existing caller (and the
    # `_ScenarioRunner` test that builds one directly) keeps unchanged; `run_all` overrides it with
    # one built on the run's own clock whenever a caller opts in (see
    # `_default_run_crash_recovery_budget`).
    run_crash_budget: RunCrashRecoveryBudget = field(
        default_factory=lambda: RunCrashRecoveryBudget(None)
    )
    # Whether a crash-triggered retry (attempt > 1) may force `preconditions.erase=True`. True (the
    # default) matches every existing caller. `bajutsu run`'s `--no-erase` (an operator override of
    # every scenario's `preconditions.erase`, applied by `_filter_scenarios` in
    # `bajutsu/cli/commands/run.py` before a scenario ever reaches here) is the one signal this
    # forced retry must still honor: by the time a scenario reaches `run_one`, `_filter_scenarios` has
    # already resolved `preconditions.erase` to a concrete bool for every scenario (most commonly
    # `False`, the built-in default nobody asked for), so a guard reading that field can no longer
    # distinguish "the operator explicitly asked to keep the device as-is" from "nobody said
    # anything" — this flag carries the pre-resolution CLI signal (`erase is not False`) instead, so
    # `--no-erase` still means what it says even on a crash-triggered retry.
    force_erase_on_retry: bool = True
    # Whether this run has been asked to stop (BE-0370). Read once per scenario below, before the
    # first lease is attempted, and handed down to `run_scenario` so the step loop and its condition
    # waits notice a cancel at their own safe boundaries.
    cancelled: CancelSource = not_cancelled
    # `bajutsu run --trace-driver` (BE-0415): opens a `tracing` context per scenario and writes
    # `<sid>/driver_trace.json`. Diagnostic only, off by default, never on the verdict path.
    trace_driver: bool = False
    # Latches once `_maybe_emit_score` has fired, so a backend-crash retry of scenario 0 (which
    # re-enters `_run_on_lease` on a respawned app — BE-0049) does not re-score and emit a second
    # grade: the score is a once-per-run tell, not a per-attempt one. A mutable field on a frozen
    # dataclass (its state is toggled, never rebound); an Event so the latch is also safe under the
    # `workers>1` thread pool.
    _scored: threading.Event = field(default_factory=threading.Event)

    def _maybe_emit_score(self, i: int, driver: base.Driver) -> None:
        """Score the just-launched app's entry screen once, on the first scenario (best-effort).

        Runs only for the first scenario and only when an `on_score` sink is set, and latches after
        the first attempt, so at most one score is emitted per run (per engine in a matrix) — a
        backend-crash retry of scenario 0 re-launches the app but does not re-score. A `query()` fault
        here is diagnostic noise, never a run failure — it is logged and swallowed so the deterministic
        verdict is untouched.
        """
        if i != 0 or self.on_score is None or self._scored.is_set():
            return
        # Latch before emitting: a crash retry must not re-score even if the sink or `query()` below
        # faults (the fault is swallowed) — at-most-once is the contract, not one-success-guaranteed.
        self._scored.set()
        # Lazy import: `doctor` pulls in the platform lifecycle, which imports `namespace_of` back from
        # it — a module-level import here would risk a cycle, and the default (no `on_score`) path must
        # not pay for loading it at all.
        from bajutsu.common.doctor import score

        try:
            self.on_score(
                score(
                    driver.query(),
                    self.eff.id_namespaces,
                    ok_coverage=self.eff.doctor_thresholds.ok_coverage,
                    fail_coverage=self.eff.doctor_thresholds.fail_coverage,
                )
            )
        except Exception as exc:  # diagnostic only — the run's verdict must not depend on it
            _logger.debug("entry-screen convention score failed: %s", exc, exc_info=True)

    def _artifacts(self) -> RunArtifactWriter | None:
        """The run's artifact sink, or None when this run keeps no run directory at all."""
        return None if self.run_dir is None else RunArtifactWriter(self.run_dir, self.redactor)

    def _now(self) -> float:
        """Monotonic seconds from the injected clock, or the real clock when none is wired.

        Used to meter the crash-recovery wall-clock budget; the injected clock is the seam that lets a
        test drive that budget deterministically (no real respawn delay).
        """
        return _resolve_now(self.clock)()

    def run_one(self, i: int, s: Scenario) -> RunResult:
        """Run one scenario on a freshly leased device and return its result.

        Args:
            i: The scenario's zero-based index, used for its ordered `NN-slug` evidence dir.
            s: The scenario to run.
        """
        sid = _evidence_sid(i, s)
        if not self.trace_driver:
            return self._run_one_impl(i, s, sid)
        # BE-0415: opened *before* `_run_one_impl` leases a device, so the driver it constructs
        # (`self.lease(...)` -> `launch_driver` -> `backends.make_driver`) sees a trace already open
        # at construction time — see `tracing`'s module docstring and `XcuitestDriver`/`AdbDriver`'s
        # own construction-time checks. Flushed once per scenario, across every crash-recovery
        # retry, in a `finally` so a scenario that raises past `_run_one_impl` still leaves its
        # partial trace on disk — exactly the crash case this feature exists to help diagnose.
        with tracing.open_trace() as trace_ctx:
            try:
                return self._run_one_impl(i, s, sid)
            finally:
                writer = self._artifacts()
                if writer is not None:
                    try:
                        writer.write_json(
                            f"{sid}/driver_trace.json",
                            tracing.trace_document(s.name, trace_ctx),
                        )
                    except OSError as exc:
                        # Diagnostic only, like `--zip` / `--evidence-store`: a failed trace write
                        # is warned about, never allowed to end the run or displace the scenario's
                        # own in-flight exception.
                        _logger.warning(
                            "scenario %s: writing driver_trace.json failed (%s)", s.name, exc
                        )

    # Genuinely long: the per-scenario run on the deterministic run path. Splitting it carries real
    # behavioral risk, so it belongs to BE-0386's ratchet steps rather than the PR that sets the
    # ceiling.
    def _run_one_impl(  # noqa: C901, PLR0912, PLR0915
        self, i: int, s: Scenario, sid: str
    ) -> RunResult:
        """`run_one`'s actual body.

        Runs once per crash-recovery attempt, inside — or, when `--trace-driver` is off, without —
        the open trace `run_one` wraps it in.
        """
        # A cancel that landed before this scenario started (BE-0370). It is failed rather than
        # dropped, so `run_all` still returns exactly one result per scenario in declaration order
        # and nothing downstream needs to know cancellation happened at all — an operator who
        # cancels early in a long suite sees every scenario that never ran counted as a failure too,
        # the accepted consequence of treating a cancelled run as failed at all.
        if self.cancelled():
            if self.progress is not None:
                self.progress(f"✘ scenario {i + 1}/{self.total}: {s.name} (cancelled)")
            return RunResult(
                scenario=s.name,
                ok=False,
                steps=[],
                backend=self.actuator or "",
                sid=sid,
                failure=CANCELLED_FAILURE,
            )
        if self.progress is not None:
            self.progress(f"▶ scenario {i + 1}/{self.total}: {s.name}")
        # Resolve this scenario's actuator and capability set. With a per-scenario resolver (BE-0240)
        # the cheapest actuator the scenario can run on is chosen here; with none, the run's fixed
        # `actuator`/`caps` are used (today's path). Either way the preflight fails a scenario the
        # actuator can't run *before* any device is leased (BE-0082 fail-fast).
        actuator = self.actuator
        caps = self.caps
        if self.resolve_actuator is not None:
            try:
                actuator = self.resolve_actuator(s)
            except RuntimeError as exc:
                # No iOS actuator is even available (e.g. no xcodebuild and idb absent): a clean
                # per-scenario failure, not a crash that aborts the whole run.
                if self.progress is not None:
                    self.progress(f"✘ scenario {i + 1}/{self.total}: {s.name} ({exc})")
                return RunResult(
                    scenario=s.name, ok=False, steps=[], backend="", sid=sid, failure=str(exc)
                )
            caps = capabilities_for_run(actuator, self.eff, self.udid_spec)
        if caps is not None and (reasons := capability_preflight.unsupported(s, caps)):
            if self.progress is not None:
                self.progress(
                    f"✘ scenario {i + 1}/{self.total}: {s.name} (unsupported on {actuator})"
                )
            return RunResult(
                scenario=s.name,
                ok=False,
                steps=[],
                backend=actuator or "",
                sid=sid,
                failure=f"unsupported on backend '{actuator}': {'; '.join(reasons)}",
            )
        # Once an earlier scenario has established that recovery is not going to work on this host,
        # every later scenario must fail this fast — checked here, before the first lease of this
        # scenario is even attempted, not only inside the crash-retry loop's own `except` below.
        # Without this, a device that has already demonstrated it cannot recover still gets one full
        # cold-spawn attempt per remaining scenario (each paying up to a full readiness ceiling plus
        # its own device-recovery ladder) before the job's own CI `timeout-minutes` cancels it — the
        # same undiagnosable-cancellation outcome the run-level budget exists to turn into a loud,
        # fast failure, just moved one scenario later.
        #
        # Deliberately the latch, not the weaker `exhausted()`: the accumulated total bills recovery
        # time whether the scenario that spent it ultimately passed or failed, so a single
        # slow-but-successful recovery (a device replacement that takes a while and then works) can
        # cross the budget on its own — that says the device took a while, not that it is broken.
        # Latching on bare `exhausted()` would fail every remaining scenario on a device that had
        # just proven it *can* recover. The latch is only set once a scenario's own loop has actually
        # ended in failure for a reason that says otherwise (both set below, at those exact
        # failures), which is the real evidence — a cheap threadsafe read either way, so this costs
        # nothing on the common, unbounded-budget path.
        #
        # Read as a cause rather than a bool because the two causes are not interchangeable to an
        # operator, and because a run latched by a timeout may have no budget at all: formatting an
        # unset one here raised a `TypeError` out of `run_one` and discarded the run's verdicts
        # (BE-0374).
        if (given_up_cause := self.run_crash_budget.given_up_cause()) is not None:
            if self.progress is not None:
                self.progress(
                    f"✘ scenario {i + 1}/{self.total}: {s.name} "
                    f"(crash recovery abandoned: {given_up_cause})"
                )
            never_leased_failure = f"backend crash recovery skipped: {given_up_cause}, so this scenario was never leased"
            return RunResult(
                scenario=s.name,
                ok=False,
                steps=[],
                backend=actuator or "",
                sid=sid,
                failure=never_leased_failure,
                # No device was ever leased, so no recording was ever attempted — disclose why the
                # report's video player is empty instead of leaving it looking like a plain miss
                # (BE-0020's channel, reused here rather than left to a bare "no recording"). Only
                # when the scenario actually asked for video (BE-0028's opt-in): `SkippedCapture` means
                # "requested but unavailable", so a scenario with no capturePolicy/`capture` naming
                # video must stay silent here the same way `requested_intervals` itself does.
                skipped_captures=(
                    [SkippedCapture(kind="video", reason=never_leased_failure)]
                    if "video" in requested_intervals(s, self.eff.capture)
                    else []
                ),
            )
        # Backend-crash recovery: a mid-scenario runner/host crash (base.BackendCrashError) is
        # backend infrastructure, not a verdict — discard the dead lease, lease a fresh device (a
        # cold respawn), and re-run the whole scenario from the start, bounded by `crash_retries`.
        # A scenario that crashes every attempt exhausts the budget and fails loudly (BE-0049).
        try:
            handler = (
                self.alert_guard_for(s) if self.alert_guard_for is not None else self.alert_guard
            )
        except UncoveredSystemAlertLocale as exc:
            # A `systemAlertHandling.rules` entry names a prompt this scenario's locale has no known
            # labels for — the same fail-loudly-before-any-device-work choice `handleSystemAlert`'s
            # own `prompt`/`choice` resolution makes, surfaced here as a clean per-scenario failure
            # rather than an uncaught exception that would abort the whole run.
            if self.progress is not None:
                self.progress(f"✘ scenario {i + 1}/{self.total}: {s.name} ({exc})")
            return RunResult(
                scenario=s.name,
                ok=False,
                steps=[],
                backend=actuator or "",
                sid=sid,
                failure=str(exc),
            )
        last_crash: BackendCrashError | None = None
        # The newest crash whose own `partial_artifacts` is non-empty — kept apart from `last_crash`
        # because the *last* crash is not necessarily the one that recorded anything: a later attempt
        # can crash during lease bring-up, before `run_scenario` (and its interval finalize) is even
        # entered, leaving that attempt's own `partial_artifacts` at its `None` default while an
        # earlier attempt's recording still sits on disk.
        partial_artifacts: list[Any] = []
        # The lease the most recent mid-run crash ran on (BE-0421). Kept separately from `lz`, which is
        # reset to None per attempt: a loop that ends on a lease-time crash, or on a forced-erase
        # prep that timed out, leaves `lz` empty while the environment behind an *earlier* attempt
        # still holds the crash evidence this scenario failed for.
        crashed_lz: Lease | None = None
        # The count + wall-clock retry decision; Unit 2 of BE-0334 wires the conformance harness onto
        # the same helper so the two recovery paths cannot drift. The wall-clock deadline it holds is set at the
        # *first* crash (so the first respawn is never blocked — a genuine one-off is still ridden
        # out) and caps the total time a never-recovering runner can spend paying a fresh cold-startup
        # ceiling per respawn. An unset budget keeps the count as the only cap.
        budget = CrashRecoveryBudget(self.crash_retries, self.crash_recovery_budget, self._now)
        budget_spent = False
        run_budget_spent = False
        # Whether a cancel request is what stopped this scenario's crash recovery (BE-0370), reported
        # in the failure below so the report says why recovery ended rather than blaming a budget.
        cancelled_recovery = False
        # The device-preparation timeout that stopped this scenario's crash recovery, if one did
        # (BE-0374). Kept rather than just flagged: it names the command and the deadline it
        # exceeded, which is what tells an operator reading the report that the host was wedged
        # rather than that a scenario failed for unexplained reasons.
        device_timeout: device_errors.DeviceTimeout | None = None
        # Local to this call, never shared: `run_crash_budget` bills only the seconds *this*
        # scenario's own loop actually spends recovering (started at its first crash, in the
        # `finally` below), not wall-clock elapsed since some earlier scenario's crash — see
        # `RunCrashRecoveryBudget`'s own docstring for why a shared start-of-episode timestamp would
        # be unsafe under `workers > 1`.
        recovery_started: float | None = None
        # Whether this scenario may still escalate a crash retry above the forced erase to a
        # replacement device (BE-0354). An erase resets the device's data, so it recovers the app-data
        # corruption class; it was measured not to clear a Simulator whose capture services have
        # wedged, and the erased device came back wedged. Two signals select the rung: an erase that
        # was already tried and crashed again, and a video-start confirmation that stalled — the
        # earliest symptom of exactly that degradation. Only the Simulator XCUITest route on an
        # unpinned run with an `appPath` can serve it, so everywhere else this stays False and the
        # erase rung is unchanged. At most one replacement per scenario, so a device that keeps
        # crashing cannot mint a `bajutsu-recovered-*` device per attempt.
        #
        # The two opt-outs the erase rung honors are folded in here rather than left to the
        # escalation condition below, because a replacement resets strictly *more* than an erase
        # does — a blank device carries no app data at all. Reading them only where `forced_erase` is
        # computed would leave the stall signal free to swap the device out from under a scenario
        # that declared `reinstall: overwrite` to keep its data, or from under an operator who asked
        # for `--no-erase`, which is the opposite of what either opt-out means. A `forced_erase`
        # attempt already implies both, so folding them in loses nothing on that path.
        can_replace = (
            self.force_erase_on_retry
            and s.preconditions.reinstall != "overwrite"
            and device_replacement_supported(actuator, self.eff, self.udid_spec)
        )
        replaced = False
        try:
            for attempt in range(1, budget.total_attempts + 1):
                if self.trace_driver:
                    # Stamped *before* this attempt leases (and constructs) a driver — a cold
                    # respawn's own readiness round trips happen inside `self.lease(...)` below, so
                    # stamping any later would misattribute them to the previous attempt. `run_one`
                    # always opens a trace before calling here whenever `self.trace_driver` is True.
                    trace_ctx = tracing.current_trace()
                    assert trace_ctx is not None
                    trace_ctx.attempt = attempt
                # Reset per attempt: the crash handler reads it to reach the lease's own signals, and
                # a lease-time crash must not be judged on the previous attempt's lease.
                lz: Lease | None = None
                # A retry forces the device recovery `erase: true` would give: respawning onto the
                # device that just wedged reproduces the crash. Skipped on `reinstall: overwrite`
                # (the scenario needs its app data preserved), on `not self.force_erase_on_retry`
                # (the operator's `--no-erase`), and wherever forcing `erase` would itself raise
                # (`erase_precondition_supported`, which would abort the whole run). `erase: false`
                # is NOT a skip signal: the CLI has already collapsed it to a concrete bool by here.
                # An escalated attempt keeps this forced erase; the environment serving a replacement
                # drops it. Every one of these choices is argued in BE-0353.
                retry_scenario = s
                if (
                    attempt > 1
                    and self.force_erase_on_retry
                    and s.preconditions.reinstall != "overwrite"
                    and erase_precondition_supported(actuator, self.eff, self.udid_spec)
                ):
                    retry_scenario = s.model_copy(
                        update={"preconditions": s.preconditions.model_copy(update={"erase": True})}
                    )
                forced_erase = retry_scenario is not s
                # Lease *inside* the try so a crash during bring-up — the launch/readiness gate, not only a
                # scenario step — is caught by the same recovery. `self.lease` runs launch_driver, whose
                # `await_ready` surfaces a BackendCrashError when the resident runner answers /health at
                # cold spawn and then crashes on the first readiness query, before any step runs; without
                # this, that crash escaped the loop and failed the whole run. `_run_on_lease` releases the
                # lease in its own `finally` (on a mid-step crash too, so the dead lease is never leaked); a
                # lease-time crash leaves no lease to release (the pool tears down its own failed lease), and
                # the retry leases afresh — a cold respawn, since the pool drops the dead warm runner.
                try:
                    try:
                        lz = self.lease(self.eff, retry_scenario)
                    except device_errors.DeviceError as exc:
                        # A failed forced-erase prep (`simctl.DeviceError`/`adb.DeviceError`, e.g. the
                        # device rejected `erase`/`shutdown`/`boot`) is not a `BackendCrashError`, so it
                        # would otherwise escape this loop's own `except` below and abort the whole run
                        # past `run_all` — losing every already-passed scenario's verdict, worse than
                        # the bare in-place respawn this forced retry replaces. Degrade to that bare
                        # respawn instead, exactly like today whenever erase isn't forced; that lease's
                        # own faults (a `BackendCrashError`, or another `DeviceError`) are handled the
                        # same way a bare respawn's already are — the latter still propagates and ends
                        # the run, since nothing about *that* path changed. An escalated attempt
                        # degrades through the same branch and for the same reason: creating a device
                        # can fail on a host with no runtimes left, and losing every passed scenario's
                        # verdict over that is worse than retrying onto the device this run has. The
                        # degradation is logged because the fault is otherwise dropped here.
                        #
                        # A *timeout* on this forced-erase lease is the one fault that reasoning does
                        # not cover — see BE-0374's Motivation for the argument (a refusal is evidence
                        # about one operation; a timeout is evidence the service behind every
                        # operation stopped answering) and its Alternatives considered for why this
                        # stays scoped to the forced-erase lease alone. Fail this one scenario instead
                        # of degrading, keeping every verdict already earned.
                        if isinstance(exc, device_errors.DeviceTimeout) and forced_erase:
                            device_timeout = exc
                            _logger.warning(
                                "scenario %s: forced-erase prep timed out (%s); "
                                "failing the scenario and latching the run",
                                s.name,
                                exc,
                            )
                            # Leaves the retry loop, so the `finally` below still bills this
                            # scenario's own recovery time and the failure block still reports it.
                            break
                        if not forced_erase:
                            raise
                        _logger.warning(
                            "scenario %s: forced-erase prep failed (%s); "
                            "degrading to a bare respawn",
                            s.name,
                            exc,
                        )
                        lz = self.lease(self.eff, s)
                    if self.trace_driver:
                        # BE-0415: wraps the `Lease` this attempt just returned, not anything inside
                        # `pool.py` — `device_pool`'s warm-driver cache stores the raw driver
                        # separately from the `Lease` object, so wrapping there would leak a wrapped
                        # driver into that cache and double-wrap it on the next lease. Mutating this
                        # already-returned `Lease.driver` never touches that cache. (The context's
                        # `attempt` number is stamped at the top of this loop, before the lease that
                        # just returned `lz` — not here — so a cold respawn's own readiness round
                        # trips are attributed to the attempt that actually made them.)
                        lz.driver = tracing.TracingDriver(lz.driver)
                    if attempt > 1:
                        _logger.info(
                            "scenario %s: backend respawned and recovered on attempt %d/%d",
                            s.name,
                            attempt,
                            budget.total_attempts,
                        )
                        if self.progress is not None:
                            self.progress(
                                f"✔ scenario {i + 1}/{self.total}: {s.name} — backend respawned "
                                f"and recovered (attempt {attempt}/{budget.total_attempts})"
                            )
                    if recovery_started is not None:
                        # Recovery ended when the lease came back; what follows is the scenario's own
                        # work, not recovery. A mid-step crash below re-arms the clock.
                        self.run_crash_budget.add_recovery_time(self._now() - recovery_started)
                        recovery_started = None
                    return self._run_on_lease(lz, handler, i, s, sid)
                except BackendCrashError as crash:
                    last_crash = crash
                    if crash.partial_artifacts:
                        partial_artifacts = crash.partial_artifacts
                    if lz is not None:
                        crashed_lz = lz
                    if recovery_started is None:
                        recovery_started = self._now()
                    run_exhausted = self.run_crash_budget.exhausted()
                    decision = budget.on_crash(attempt)
                    # The count would allow another respawn but the scenario's own wall-clock budget,
                    # or the run-level one shared with every scenario, is spent: distinct end states
                    # from "attempts exhausted", surfaced in the failure below. `run_budget_spent` is
                    # true only when the run-level budget is *why* recovery stopped — if the count or
                    # the scenario's own budget already said no more retries, that stays the reported
                    # cause rather than a run-level exhaustion that happened to coincide with it.
                    budget_spent = decision.budget_spent
                    run_budget_spent = run_exhausted and decision.will_retry
                    will_retry = decision.will_retry and not run_exhausted
                    if will_retry and self.cancelled():
                        # A retry leases afresh — on XCUITest a cold respawn, and a forced erase on top
                        # — and that bring-up can outlive the grace window the canceller is waiting
                        # out, which would let the run be killed before `_assemble_report` wrote a
                        # manifest: exactly the silent gap BE-0370 removes, reintroduced on the crash
                        # path. So a cancelled run stops recovering and fails this scenario now,
                        # keeping every verdict it has already reached.
                        will_retry, cancelled_recovery = False, True
                    if will_retry and can_replace and not replaced and lz is not None:
                        # The two signals that say the device itself is degraded past what an erase
                        # clears. A crash whose attempt already ran on an erased device has spent that
                        # remedy; a stalled video start says the capture pipeline was not producing
                        # before the scenario even began, which is the degradation class the erase was
                        # observed not to clear, so it escalates from the first crash. The request is
                        # made on the crashed lease because the environment behind it is the one the
                        # pool keeps for this device — the next lease's bring-up is what serves it. A
                        # lease-time crash leaves no lease to ask, so it keeps the erase rung.
                        # Unlike every rung below it, this remedy is bound to one *device* and lands
                        # on a later lease, which is sound only because `can_replace` implies an
                        # unpinned run: that is a pool of one device served by one worker, so the next
                        # lease is necessarily this same device (see `device_replacement_supported`).
                        # `replaced` is set on the request rather than on an observed swap, so a
                        # request the pool dropped (an evicted warm environment) spends the
                        # allowance — deliberately conservative, since the allowance exists to bound
                        # how many devices a scenario can mint, and the attempt still gets the erase.
                        stalled = lz.video_start_stalled()
                        if forced_erase or stalled:
                            lz.request_device_replacement()
                            replaced = True
                            _logger.warning(
                                "scenario %s: %s; escalating the retry to a replacement device",
                                s.name,
                                "the video recording never confirmed it started"
                                if stalled
                                else "a forced-erase retry crashed again",
                            )
                    _logger.warning(
                        "scenario %s: backend crashed mid-run (attempt %d/%d)%s: %s",
                        s.name,
                        attempt,
                        budget.total_attempts,
                        ", respawning and retrying" if will_retry else "",
                        crash,
                    )
                    if self.progress is not None and will_retry:
                        self.progress(
                            f"⟳ scenario {i + 1}/{self.total}: {s.name} — backend crashed mid-run, "
                            f"respawning and retrying (attempt {attempt}/{budget.total_attempts})"
                        )
                    if not will_retry:
                        # Either cap reached — stop before leasing again. Breaking (vs. letting the range
                        # run out) is what lets a budget cut recovery short with retries still on the clock.
                        break
        finally:
            # Bill this scenario's own recovery time once its loop is done (pass or fail) — a no-op
            # lock acquisition avoided entirely for the common case where the backend never crashed.
            if recovery_started is not None:
                self.run_crash_budget.add_recovery_time(self._now() - recovery_started)
        # Recovery is over and the scenario never passed: the crash is not a one-off, so surface it as
        # an honest failure — distinguishing "ran out of attempts" from either budget running out, and
        # a host that stopped answering from a backend that crashed.
        if self.progress is not None:
            reason = (
                "device preparation timed out"
                if device_timeout is not None
                else "backend crashed mid-run"
            )
            self.progress(f"✘ scenario {i + 1}/{self.total}: {s.name} ({reason})")
        if device_timeout is not None:
            # Tested before the budget branches because it is the decisive cause: the retry loop
            # stopped on the timeout itself, not on a cap being reached, so no cap is what this
            # scenario failed for. The latch is set unconditionally, unlike the budget path just
            # below, which qualifies it on the budget having been the binding constraint: a budget is
            # a measurement a slow-but-successful recovery can also trip, whereas a device command
            # that never returned is a direct observation that the service stopped answering. A
            # wedged host belongs to the host, not to the scenario that happened to meet it, so
            # establishing it once here is what keeps every later scenario from spending its own
            # deadline rediscovering it.
            self.run_crash_budget.mark_given_up(
                "an earlier scenario's device preparation timed out"
            )
            failure = (
                "the device preparation for a backend-crash retry timed out, so this scenario was "
                f"failed rather than retried onto a wedged host ({attempt} attempt(s) into this "
                f"scenario; recovering from: {last_crash}): {device_timeout}"
            )
        elif cancelled_recovery:
            # Led by the exact `CANCELLED_FAILURE` spelling, so a consumer that groups cancelled
            # scenarios still recognizes this one, and followed by the crash that was being recovered
            # from, which is evidence no other exit path can supply.
            failure = (
                f"{CANCELLED_FAILURE}: the backend crashed mid-run and the run was cancelled before "
                f"it could recover ({attempt} attempt(s) into this scenario): {last_crash}"
            )
        elif run_budget_spent:
            # This scenario failed *because* the run-level budget was the binding constraint — the
            # real evidence (unlike bare `exhausted()`) that the device is not recovering, so every
            # later scenario's own pre-lease check above now fails fast instead of each paying its
            # own first attempt against the same device. `run_budget_spent` implies `exhausted()`
            # answered True, which it can only do on a budget that is set, so `:g` has a number here.
            self.run_crash_budget.mark_given_up(
                "an earlier scenario exhausted the run-level crash-recovery budget of "
                f"{self.run_crash_budget.budget:g}s"
            )
            failure = (
                "backend crashed mid-run and the run-level crash-recovery budget of "
                f"{self.run_crash_budget.budget:g}s is exhausted ({attempt} attempt(s) into this "
                f"scenario; the budget is shared across every scenario in this run): {last_crash}"
            )
        elif budget_spent:
            failure = (
                f"backend crashed mid-run and did not recover within the "
                f"{self.crash_recovery_budget:g}s crash-recovery budget "
                f"(spent respawning across {attempt} attempt(s)): {last_crash}"
            )
        else:
            failure = (
                f"backend crashed mid-run and did not recover across "
                f"{budget.total_attempts} attempts: {last_crash}"
            )
        # Every branch above (the `if`/`elif` chain and this trailing `else`) is reached only after a
        # crash: the `except BackendCrashError` above is the sole path that leaves the loop without
        # returning (`break`) or that lets it run out of attempts, and it sets `last_crash` — which
        # the type-checker cannot see from here.
        assert last_crash is not None
        if crashed_lz is not None:
            failure += self._write_crash_artifacts(crashed_lz, s, sid)
        # `partial_artifacts` names the newest attempt that actually finalized a recording before
        # crashing (not necessarily the last attempt — a later one can crash during lease bring-up,
        # before `run_scenario`'s own finalize ever runs). Only the video is attached: it is what the
        # report's always-visible media area surfaces, whereas a missing `deviceLog`/`appTrace`
        # simply omits its own tab and has no confusing empty state to fix. A recovered
        # `deviceLog`/`appTrace` is dropped here rather than attached — out of scope.
        recovered = next((a for a in partial_artifacts if a.kind == "video"), None)
        # `SkippedCapture` means "requested but unavailable" (BE-0020) — gate it on the scenario
        # having actually asked for video (BE-0028's opt-in) the same way `requested_intervals`
        # itself does, so an ordinary crashed scenario that never enabled video stays silent here
        # instead of claiming a recording went missing that was never going to exist.
        wanted_video = "video" in requested_intervals(s, self.eff.capture)
        return RunResult(
            scenario=s.name,
            ok=False,
            steps=[],
            backend=actuator or "",
            sid=sid,
            failure=failure,
            artifacts=[recovered] if recovered is not None else [],
            # Disclose the gap only when video was requested and no recording survived the crash —
            # otherwise either the video attached above already answers "where is the recording", or
            # this scenario was never going to have one, and a skip entry would falsely claim it was.
            skipped_captures=(
                [SkippedCapture(kind="video", reason=failure)]
                if wanted_video and recovered is None
                else []
            ),
        )

    def _write_crash_artifacts(self, lz: Lease, s: Scenario, sid: str) -> str:
        """Copy the crashed backend's own evidence into this scenario's directory (BE-0421).

        Called once, where the retry loop has already given up: a scenario that recovered passes, and a
        passing scenario needs no crash report. Returns the trailer to append to the failure string, so
        a contributor reading why the scenario failed is pointed at the evidence instead of having to
        already know it exists — empty when there was nothing to copy.

        Every write goes through `RunArtifactWriter.write_text` rather than `write_bytes`, so content
        this pipeline did not author still crosses the free-text scrub (BE-0331). A failed write is
        logged and nothing more: a diagnostic artifact must never turn an already-decided failure into
        a different, unrelated one.
        """
        writer = self._artifacts()
        if writer is None:
            return ""
        directory: Path | None = None
        for name, content in lz.crash_artifacts():
            try:
                # The directory comes from the write itself rather than being composed a second time,
                # so the failure string names the file that actually landed.
                written = writer.write_text(
                    f"{sid}/{_CRASH_DIAGNOSTICS_DIR}/{name}", content.decode(errors="replace")
                )
            except OSError as exc:
                _logger.warning(
                    "scenario %s: writing the crash artifact %s failed (%s)", s.name, name, exc
                )
            else:
                directory = written.parent
        if directory is None:
            return ""
        return f"; the backend's own crash evidence is in {directory}"

    def _run_on_lease(
        self, lz: Lease, handler: AlertGuardConfig | None, i: int, s: Scenario, sid: str
    ) -> RunResult:
        """Run one scenario on an already-leased device and return its result.

        Raises `base.BackendCrashError` straight through when the backend crashes mid-scenario: the
        lease is dead, so `run_one` discards it and re-runs the scenario on a fresh one. Every other
        outcome — pass, assertion failure, unsupported action — comes back as a `RunResult`.
        """
        try:
            # Hand the backend this scenario's own answer for a prompt that interrupts one of its
            # interactions, before any of them run. The resident runner outlives a scenario, so this
            # is set per scenario rather than per lease — including to *empty* when the scenario
            # disables the guard, so it never inherits the previous scenario's policy.
            push_interruption_policy(lz.driver, handler)
            # Score the entry screen before the scenario mutates it — the app is freshly launched here,
            # exactly what a standalone `doctor` probe would see, but on the lease this run already holds.
            self._maybe_emit_score(i, lz.driver)
            if lz.collector is not None:
                lz.collector.clear()
            writer = self._artifacts()
            # Build visual context for scenario-level visual assertions (expect).
            vc: VisualContext | None = None
            if self.baselines_dir is not None and writer is not None:
                vc = VisualContext(
                    # The driver writes this screenshot itself, so the sink reserves its path and
                    # `capture_actual` records the bytes as uninspected (BE-0331).
                    screenshot_path=writer.reserve(f"{sid}/visual-actual.png"),
                    baselines_dir=self.baselines_dir,
                    writer=writer,
                    prefix=sid,
                    default_compare=self.eff.visual_compare,
                )
            sc = (
                SchemaContext(schemas_dir=self.schemas_dir)
                if self.schemas_dir is not None
                else None
            )
            # Best-effort device screen bounds for golden frame sanity (BE-0006):
            # a query() failure here must not block non-golden scenarios.
            gc_with_screen = self.golden_context
            if self.golden_context is not None and self.golden_context.screen is None:
                try:
                    from bajutsu.common.drivers.elements import screen_size_from_elements

                    sw, sh = screen_size_from_elements(lz.driver.query())
                    gc_with_screen = GoldenContext(
                        goldens_dir=self.golden_context.goldens_dir, screen=(0.0, 0.0, sw, sh)
                    )
                except Exception as exc:  # best-effort; _eval_golden falls back
                    _logger.debug(
                        "screen-bounds probe for golden framing failed: %s", exc, exc_info=True
                    )
            result = run_scenario(
                lz.driver,
                s,
                self.clock,
                sink=lz.sink,
                alert_guard=handler,
                scenario_id=sid,
                network=(lz.collector.snapshot if lz.collector is not None else _no_network),
                relaunch=lz.relaunch,
                bindings=self.bindings,
                control=lz.control,
                progress=self.progress,
                ctx=EvalContext(visual=vc, schema=sc, golden=gc_with_screen),
                mailbox=self.mailbox,
                webview_bridge=lz.webview_bridge,
                transitions=(
                    lz.collector.transitions_snapshot_timed
                    if lz.collector is not None
                    else _no_transitions
                ),
                # Config-level interrupts first, then the scenario's own (BE-0314): an app-wide
                # interstitial handler composes with a per-scenario addition, the config-then-scenario
                # order the systemAlertHandling default already follows.
                interrupts=[*self.eff.run_defaults.interrupts, *s.interrupts],
                # The locale this scenario runs under — the same value the lease pinned the
                # Simulator's system language to, so a `handleSystemAlert` naming a prompt and a
                # choice resolves to the label SpringBoard is actually rendering (BE-0320).
                locale=s.preconditions.resolved_locale(self.eff.locale),
                # The config's baseline capture guarantee (`defaults.capture`), applied on top of
                # every step alongside capturePolicy rules and inline `capture:` tokens.
                capture=self.eff.capture,
                # A cancel request reaches the step loop and its condition waits (BE-0370), so this
                # scenario stops at its next safe boundary and comes back as an ordinary failure.
                cancelled=self.cancelled,
                # The collector itself, not one of its callables: the control channel (BE-0365) is
                # the one direction that runs *into* the app, so it has no snapshot to hand over.
                # A collector that carries no channel is caught at the command, not skipped here.
                channel=lz.collector,
                # The target's own `launchEnv`, merged underneath the scenario's own the same way
                # the launch itself merges them, so a target-level touch-marker pin is seen here
                # exactly as the app that launched sees it, not read as if the scenario pinned
                # nothing (BE-0365 unit 3).
                target_launch_env=self.eff.launch_env,
            )
            result.sid = sid  # the evidence-dir slug, so the matrix links to the real dir (BE-0076)
            result.device = lz.udid  # attribute the scenario to the device that ran it
            result.device_name = lz.device_name  # for the report's Environment tab
            result.device_runtime = lz.device_runtime
            result.skipped_captures = list(lz.skipped_captures)  # disclose evidence gaps (BE-0020)
            if lz.collector is not None and writer is not None:
                art = _write_network(
                    lz.collector.snapshot_timed(),
                    writer,
                    sid,
                    wall_offset_s=result.wall_offset_s,
                    provider=lz.collector_provider,
                )
                if art is not None:
                    result.artifacts.append(art)
            if self.progress is not None:
                mark = "✔" if result.ok else "✘"
                self.progress(
                    f"{mark} scenario {i + 1}/{self.total}: {s.name} ({result.duration_s:.1f}s)"
                )
            return result
        finally:
            lz.release()


def with_lifecycle_phases(eff: Effective, scenarios: list[Scenario]) -> list[Scenario]:
    """Fold the target config's `before` / `after` into each scenario's own (BE-0392).

    The two merge in opposite orders. `before` is config-then-scenario, like `interrupts`: the
    app-wide prelude seeds the state this scenario's own setup then builds on. `after` is
    scenario-then-config, so this scenario releases the record it created before the app-wide
    teardown closes around it — the last-acquired-first-released order a fixture-based teardown pair
    gives.

    Applied to the scenario itself rather than passed beside it, so the scenario the runner executes
    and the scenario whose Before / After blocks the report renders are one object. Handing the
    merged lists to the runner separately would leave the report pairing an app-wide step's outcome
    with the scenario's own step definition, and drop an app-wide `after` rule's outcomes entirely.
    """
    if not eff.run_defaults.before and not eff.run_defaults.after:
        return scenarios  # nothing app-wide to fold in; keep the caller's own objects
    return [
        s.model_copy(
            update={
                "before": [*eff.run_defaults.before, *s.before],
                "after": [*s.after, *eff.run_defaults.after],
            }
        )
        for s in scenarios
    ]


def run_all(
    eff: Effective,
    scenarios: list[Scenario],
    lease: LeaseFn,
    clock: Clock | None = None,
    alert_guard: AlertGuardConfig | None = None,
    alert_guard_for: AlertGuardFor | None = None,
    run_dir: Path | None = None,
    workers: int = 1,
    bindings: Mapping[str, str] | None = None,
    secret_values: list[str] | None = None,
    progress: ProgressFn | None = None,
    baselines_dir: Path | None = None,
    schemas_dir: Path | None = None,
    actuator: str | None = None,
    resolve_actuator: Callable[[Scenario], str] | None = None,
    golden_context: GoldenContext | None = None,
    lease_udid_spec: str = "booted",
    on_score: Callable[[Score], None] | None = None,
    crash_retries: int | None = None,
    crash_recovery_budget: float | None = None,
    run_crash_recovery_budget: float | None = None,
    force_erase_on_retry: bool = True,
    cancelled: CancelSource = not_cancelled,
    trace_driver: bool = False,
) -> list[RunResult]:
    """Run every scenario, each on a freshly leased device, and return one result per scenario.

    `lease(eff, scenario)` blocks until a device is free, launches the app, and returns a `Lease`
    bundling the live driver with that device's evidence sink / relaunch / control / network
    collector; `lease.release()` afterwards terminates the app and returns the device to the pool.
    A lease's collector, when present, has its exchanges cleared per scenario, exposed to `request`
    assertions, and written to `<sid>/network.json` (redacted with `secret_values`).

    Args:
        eff: The resolved target config (drives redaction, backend, launch).
        scenarios: The scenarios to run; results come back in this declaration order.
        lease: Leases a device and launches the app for one scenario (a single-device run is a pool
            of one).
        clock: Injectable time source for condition waits, so tests need no real sleeps. None uses
            the real clock.
        alert_guard: A single alert-guard handler, used by tests.
        alert_guard_for: Picks each scenario's alert-guard handler (honoring its `systemAlertHandling`);
            takes precedence over `alert_guard`.
        run_dir: Where per-scenario artifacts (network.json, visual diffs) are written. None skips
            writing them.
        workers: Concurrent scenarios; >1 hands each worker its own device + per-device resources,
            so the loop keeps no shared mutable state.
        bindings: `secrets.<name>` → value substitutions applied to step inputs.
        secret_values: The raw secret values to redact from evidence.
        progress: Receives one-line progress messages (the web UI streams these). None is silent.
        baselines_dir: Baseline images for `visual` assertions. None disables visual comparison.
        schemas_dir: Directory the `responseSchema` assertions' schema files resolve against. None
            disables them.
        actuator: The single selected actuator (e.g. `xcuitest` / `playwright`); when given, each scenario
            is preflighted against its static capability set and failed up front if it needs a
            capability the actuator lacks (BE-0082). None skips the fixed preflight (a lease driven
            directly in tests, or when `resolve_actuator` chooses per scenario instead).
        resolve_actuator: Per-scenario actuator resolver (BE-0240); when given, each scenario's
            actuator — and thus the capability set it is preflighted against — is resolved from the
            scenario's own steps (cheapest sufficient), instead of the one fixed `actuator`. Mutually
            exclusive with `actuator` (passing both raises): the CLI's single-engine path and `audit`
            pass this, the cross-browser matrix passes `actuator`.
        golden_context: Goldens directory for `golden` assertions (BE-0006). None disables them.
        lease_udid_spec: The run's resolved udid spec (the provider's `udid_spec`). A WebDriver URL
            routes the run to the live XCUITest environment, so the preflight narrows to that
            transport's set (BE-0238) — the same `is_webdriver_endpoint` signal `environment_for`
            routes on. "booted" (the default) is never a URL, so the local path is unchanged.
        on_score: Sink for the app's entry-screen convention score, emitted once from the first
            scenario's freshly launched driver (the `run --score` inline of `doctor`'s grade). None
            (the default) scores nothing; diagnostic only, never on the verdict path.
        crash_retries: How many times to re-run a scenario whose backend crashed mid-run
            (`base.BackendCrashError`) on a fresh device before failing it. A crash is backend
            infrastructure, not a verdict; the default (None) reads `BAJUTSU_CRASH_RETRIES` — 1 when
            unset — so a loaded CI lane can raise the budget without a code change, while a scenario
            that crashes every attempt still fails loudly once it is spent (BE-0049). 0 disables the
            recovery. The replay re-runs the whole scenario on a respawned (not erased) app, so it is
            safe only for scenarios idempotent up to the crash point.
        crash_recovery_budget: A wall-clock ceiling (seconds) on the total time one scenario may spend
            respawning after a crash, on top of `crash_retries` (the count). None reads
            `BAJUTSU_CRASH_RECOVERY_BUDGET` — unset is unbounded (count is the only cap, unchanged).
            It stops recovery once spent so a never-recovering runner can't burn crash_retries x the
            cold-startup ceiling and blow a job's timeout; the first respawn is never blocked, so a
            genuine one-off is still ridden out.
        run_crash_recovery_budget: A wall-clock ceiling (seconds) on the total time crash recovery may
            spend across this one `run_all` call, not just one scenario. Note the scoping: the
            cross-browser matrix (`run_matrix_and_report`) runs `run_all` once per engine, so each
            engine pass gets its own full budget rather than sharing one. None reads
            `BAJUTSU_RUN_CRASH_RECOVERY_BUDGET` — unset is unbounded, unchanged from before this
            parameter existed. `crash_recovery_budget` resets for every new scenario, so a device that
            keeps degrading pays it again and again; this bounds the cumulative spend instead, so the
            run fails loudly once it is exhausted rather than each scenario silently re-spending its
            own budget until an external timeout cancels the job. The first respawn anywhere in the
            run is never blocked, the same never-block-the-first-respawn rule
            `crash_recovery_budget` already follows per scenario.
        force_erase_on_retry: Whether a crash-triggered retry (attempt > 1) may force
            `preconditions.erase=True`, the same recovery a scenario already gets by declaring
            `erase: true` (see `_ScenarioRunner.run_one`). True (the default) preserves every
            existing caller's behavior. `bajutsu run --no-erase` passes False here, carrying the
            operator's explicit opt-out past `_filter_scenarios`'s per-scenario resolution — the CLI
            resolves every scenario's `preconditions.erase` to a concrete bool before `run_all` ever
            sees it, so that field alone cannot distinguish "the operator asked to keep the device"
            from "nobody said anything" by the time a retry decides whether to force it.
        cancelled: Reports whether this run has been asked to stop (BE-0370). A scenario the request
            reaches — at a step boundary, inside a condition wait's poll, or before it was leased at
            all — comes back as `RunResult(ok=False, failure="cancelled")`, so the caller still
            receives one result per scenario and writes an ordinary failed run's report. The default
            never cancels, leaving every existing caller unchanged.
        trace_driver: `bajutsu run --trace-driver` (BE-0415): write one `<sid>/driver_trace.json`
            per scenario, recording every Python<->driver call — the driver method invoked, its
            host-device round trips, and (on Android) which fell back to a subprocess — attributed
            to the step it happened during. False (the default) writes nothing; diagnostic only,
            never on the verdict path.

    Returns:
        One result per scenario, in the same order as `scenarios`.
    """
    # `actuator` (one fixed actuator) and `resolve_actuator` (per-scenario, BE-0240) are two ways to
    # answer the same question; passing both is a caller bug. Fail loudly rather than silently letting
    # the resolver win and discarding the fixed actuator/caps (prime directive 2).
    if actuator is not None and resolve_actuator is not None:
        raise ValueError("pass either actuator or resolve_actuator to run_all, not both")
    # The target config's own lifecycle phases, folded in once here (BE-0392). `run_and_report` and
    # `run_matrix_and_report` apply the same helper to what they hand the report, so the run and the
    # report read one effective scenario rather than two lists that could drift.
    scenarios = with_lifecycle_phases(eff, scenarios)
    redactor = Redactor(eff.redact, values=secret_values)
    # One mailbox reader for the whole run (it's per-target, not per-device): the `email` step polls
    # it, with ${secrets.*} in the url/headers resolved from the same secret bindings (BE-0046).
    mailbox = build_mailbox_reader(eff.mailbox, bindings or {})
    # Preflight: a backend's capability set is (near-)static, so a scenario that needs a capability
    # the actuator lacks (e.g. simctl device control on a real iOS device — BE-0238) is failed
    # here — before any device is leased — instead of mid-run after partial device work
    # (BE-0082). `capabilities_for_run` applies the run's one config-driven narrowing (real-device
    # XCUITest). Skipped when no actuator is passed (tests that drive a lease directly), so the
    # gesture handler's own check still backstops it.
    caps = capabilities_for_run(actuator, eff, lease_udid_spec) if actuator is not None else None

    # None means "read the lane's `BAJUTSU_RUN_CRASH_RECOVERY_BUDGET` (else unbounded)"; an explicit
    # value (a test, or a caller that pins it) wins. Resolved once, ahead of the `_ScenarioRunner` that
    # shares one `RunCrashRecoveryBudget` built on it across every scenario in the run.
    resolved_run_crash_recovery_budget = (
        run_crash_recovery_budget
        if run_crash_recovery_budget is not None
        else _default_run_crash_recovery_budget()
    )

    runner = _ScenarioRunner(
        eff=eff,
        lease=lease,
        redactor=redactor,
        mailbox=mailbox,
        caps=caps,
        total=len(scenarios),
        clock=clock,
        alert_guard=alert_guard,
        alert_guard_for=alert_guard_for,
        run_dir=run_dir,
        bindings=bindings,
        progress=progress,
        baselines_dir=baselines_dir,
        schemas_dir=schemas_dir,
        actuator=actuator,
        resolve_actuator=resolve_actuator,
        golden_context=golden_context,
        udid_spec=lease_udid_spec,
        on_score=on_score,
        # None means "read the lane's `BAJUTSU_CRASH_RETRIES` (else the default)"; an explicit int
        # (a test, or a caller that pins it) still wins.
        crash_retries=crash_retries if crash_retries is not None else _default_crash_retries(),
        # Same None-reads-the-env shape: unset falls to `BAJUTSU_CRASH_RECOVERY_BUDGET` (else None,
        # unbounded); an explicit value (a test, or a caller that pins it) wins.
        crash_recovery_budget=(
            crash_recovery_budget
            if crash_recovery_budget is not None
            else _default_crash_recovery_budget()
        ),
        run_crash_budget=RunCrashRecoveryBudget(resolved_run_crash_recovery_budget),
        force_erase_on_retry=force_erase_on_retry,
        cancelled=cancelled,
        trace_driver=trace_driver,
    )
    if workers > 1:
        # >1 hands each worker its own device + per-device resources; the runner is frozen and
        # holds no per-scenario mutable state, so sharing it across workers adds none.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda pair: runner.run_one(*pair), list(enumerate(scenarios))))
    return [runner.run_one(i, s) for i, s in enumerate(scenarios)]


def run_and_report(
    eff: Effective,
    scenarios: list[Scenario],
    lease: LeaseFn,
    runs_dir: Path,
    run_id: str,
    clock: Clock | None = None,
    alert_guard: AlertGuardConfig | None = None,
    alert_guard_for: AlertGuardFor | None = None,
    workers: int = 1,
    bindings: Mapping[str, str] | None = None,
    secret_values: list[str] | None = None,
    source_name: str | None = None,
    description: str | None = None,
    plan_sources: Mapping[str, ScenarioPlanSource] | None = None,
    progress: ProgressFn | None = None,
    baselines_dir: Path | None = None,
    schemas_dir: Path | None = None,
    actuator: str | None = None,
    resolve_actuator: Callable[[Scenario], str] | None = None,
    config_source: dict[str, str] | None = None,
    exec_provenance: dict[str, str | None] | None = None,
    label: str | None = None,
    golden_context: GoldenContext | None = None,
    lease_udid_spec: str = "booted",
    on_score: Callable[[Score], None] | None = None,
    force_erase_on_retry: bool = True,
    cancelled: CancelSource = not_cancelled,
    trace_driver: bool = False,
) -> tuple[list[RunResult], Path]:
    """Run the scenarios, then write the run's artifacts under `runs_dir/run_id`.

    Wraps `run_all` and persists the report: `manifest.json`, JUnit XML, and the executed
    `scenario.yaml` (so a run is re-runnable / reviewable).

    Beyond `run_all`'s arguments (`force_erase_on_retry`, `cancelled`, and `trace_driver` pass
    straight through — see their docstrings there), `runs_dir` + `run_id` locate this run's artifact directory
    (`runs_dir/run_id`),
    `source_name` / `description` are recorded in the report, `plan_sources` (keyed by declared
    scenario name — `run/cli.py`'s `_load_scenarios`) feeds the report's verbatim YAML / per-step
    line numbers, and `config_source` — the Git source the config came from (BE-0063), or None for
    a local config — is stamped into the manifest's provenance so a branch-based run states the
    exact commit it executed.

    Returns:
        The per-scenario results and the path to the written `manifest.json`.
    """
    run_dir = runs_dir / run_id
    results = run_all(
        eff,
        scenarios,
        lease,
        clock,
        alert_guard=alert_guard,
        alert_guard_for=alert_guard_for,
        run_dir=run_dir,
        workers=workers,
        bindings=bindings,
        secret_values=secret_values,
        progress=progress,
        baselines_dir=baselines_dir,
        schemas_dir=schemas_dir,
        actuator=actuator,
        resolve_actuator=resolve_actuator,
        golden_context=golden_context,
        lease_udid_spec=lease_udid_spec,
        on_score=on_score,
        force_erase_on_retry=force_erase_on_retry,
        cancelled=cancelled,
        trace_driver=trace_driver,
    )
    manifest = _assemble_report(
        with_lifecycle_phases(eff, scenarios),
        results,
        run_dir,
        run_id,
        description=description,
        source_name=source_name,
        plan_sources=plan_sources,
        secret_values=secret_values,
        config_source=config_source,
        exec_provenance=exec_provenance,
        target=eff.target,
        label=label,
    )
    return results, manifest


def run_matrix_and_report(
    eff: Effective,
    scenarios: list[Scenario],
    engines: list[str],
    run_pass: Callable[[str, Path], list[RunResult]],
    runs_dir: Path,
    run_id: str,
    *,
    source_name: str | None = None,
    description: str | None = None,
    plan_sources: Mapping[str, ScenarioPlanSource] | None = None,
    secret_values: list[str] | None = None,
    config_source: dict[str, str] | None = None,
    exec_provenance: dict[str, str | None] | None = None,
    label: str | None = None,
    cancelled: CancelSource = not_cancelled,
) -> tuple[list[RunResult], Path]:
    """Run the scenarios once per engine, then assemble ONE report at `runs_dir/run_id` (BE-0076).

    The cross-browser fan-out: a loop over `engines`, each a full pass. `run_pass(engine, run_dir)`
    runs the selected scenarios for one engine against its own pool, writing that engine's evidence
    under `run_dir` (the caller hands it `runs_dir/run_id/<engine>`, prefixing the existing `NN-slug`
    layout so two engines never collide); its results are tagged with `engine` here. The passes'
    tagged results are concatenated into one flat list and written as a single manifest / JUnit /
    report — the manifest's `matrix` block aggregates the per-engine verdicts, and `ok` is
    all-must-pass across every engine x scenario (pure aggregation, no LLM).

    `cancelled` (BE-0370) is read between passes: each one first builds a whole `device_pool`, so a
    cancel during the first engine would otherwise still pay every remaining engine's bring-up and
    teardown before the run could finish, overrunning the grace period. Every scenario of an engine
    that never ran is failed as cancelled rather than left out, so the matrix still names every
    requested engine and `ok` aggregates a cancelled run to False — dropping those engines instead
    would let a first pass that happened to be green aggregate to a `PASS` for a run that never
    finished.

    Returns:
        The concatenated per-engine results and the path to the written `manifest.json`.
    """
    run_dir = runs_dir / run_id
    # Owner-only up front (BE-0131): each engine pass writes under run_dir/<engine>, and the sink
    # that creates *that* directory would materialize this one above it at the ambient umask.
    prepare_run_dir(runs_dir, run_id)
    results: list[RunResult] = []
    for index, engine in enumerate(engines):
        if cancelled():
            # The scenarios of every engine left are failed as cancelled, not dropped. An engine
            # missing from `results` takes its scenarios' verdicts with it, and the manifest's
            # all-must-pass `ok` then aggregates only the engines that did run — so a cancel landing
            # after a green first pass would record a `PASS` for a run that never finished, the
            # silent-gap failure this item removes, inverted. Synthesizing them keeps the aggregation
            # pure and the matrix complete: a cell that says `cancelled` states plainly that the axis
            # was requested and never executed.
            _logger.info(
                "run cancelled: failing the remaining engine pass(es) %s as cancelled",
                ", ".join(engines[index:]),
            )
            for skipped in engines[index:]:
                results.extend(_cancelled_pass(scenarios, skipped))
            break
        passed = run_pass(engine, run_dir / engine)
        for r in passed:
            r.engine = engine  # tag each verdict with its rendering engine for the matrix
            _reroot_evidence(r, engine)  # its evidence lives under <engine>/ in the one report
        results.extend(passed)
    manifest = _assemble_report(
        with_lifecycle_phases(eff, scenarios),
        results,
        run_dir,
        run_id,
        description=description,
        source_name=source_name,
        plan_sources=plan_sources,
        secret_values=secret_values,
        config_source=config_source,
        exec_provenance=exec_provenance,
        target=eff.target,
        label=label,
    )
    return results, manifest


def _cancelled_pass(scenarios: list[Scenario], engine: str) -> list[RunResult]:
    """One cancelled result per scenario for an engine pass a cancel stopped from ever starting.

    The same shape `_ScenarioRunner.run_one` gives a scenario the cancel reached before its first
    lease (BE-0370), so the report needs no notion of an engine that was skipped: it sees one verdict
    per requested engine x scenario, as it does for a run that finished.
    """
    return [
        RunResult(
            scenario=s.name,
            ok=False,
            steps=[],
            backend="",
            engine=engine,
            sid=_evidence_sid(i, s),
            failure=CANCELLED_FAILURE,
        )
        for i, s in enumerate(scenarios)
    ]


def _reroot_evidence(r: RunResult, engine: str) -> None:
    """Prefix a matrix result's run-dir-relative evidence paths with `<engine>/` (BE-0076).

    Each engine pass writes its evidence under `run_dir/<engine>/<sid>/`, but the artifact and
    visual-image paths are recorded relative to that pass's own `run_dir` (`<sid>/…`). The matrix
    assembles ONE report at the top `run_dir`, so every such path is re-rooted under the engine
    subtree here — otherwise the report's video / network / log / diff links resolve to the wrong
    directory. A no-op for paths already absent (None).
    """

    def artifact(a: Artifact) -> Artifact:
        return replace(a, name=f"{engine}/{a.name}")

    def visual(v: VisualEvidence | None) -> VisualEvidence | None:
        if v is None:
            return None
        return replace(
            v,
            actual=f"{engine}/{v.actual}",
            baseline=f"{engine}/{v.baseline}" if v.baseline else v.baseline,
            diff=f"{engine}/{v.diff}" if v.diff else v.diff,
        )

    def assertion(a: AssertionResult) -> AssertionResult:
        return replace(a, visual=visual(a.visual))

    r.artifacts = [artifact(a) for a in r.artifacts]
    r.expect_results = [assertion(a) for a in r.expect_results]
    for step in r.steps:
        step.artifacts = [artifact(a) for a in step.artifacts]
        step.assertion_results = [assertion(a) for a in step.assertion_results]


def _assemble_report(
    scenarios: list[Scenario],
    results: list[RunResult],
    run_dir: Path,
    run_id: str,
    *,
    source_name: str | None = None,
    description: str | None = None,
    plan_sources: Mapping[str, ScenarioPlanSource] | None = None,
    secret_values: list[str] | None = None,
    config_source: dict[str, str] | None = None,
    exec_provenance: dict[str, str | None] | None = None,
    target: str | None = None,
    label: str | None = None,
) -> Path:
    """Write the run's report artifacts under `run_dir` from its (possibly engine-tagged) results.

    The shared report-writing tail of `run_and_report` and `run_matrix_and_report`: the executed
    `scenario.yaml`, the provenance stamps, and `manifest.json` / `junit.xml` / `report.html`.

    Every one of them goes through a sink carrying the run's bound secret values, so a literal that
    reached a run-level artifact (an assertion's expected/actual text in the manifest or the HTML) is
    masked on the way in rather than rewritten afterwards (BE-0331). The scenario definitions already
    hold tokens, not values, so this only ever catches result text. `plan_sources` feeds the report's
    YAML tab from each scenario's own on-disk text instead of a re-dump — the sink's own
    scrub (above) still runs over it like every other artifact, on top of the structural TOTP-secret
    masking `scenario_sources` already applied when it read the file.
    """
    # Snapshot for evidence with literal `totp.secret` seeds masked (BE-0152) — a `${secrets.*}`
    # reference is kept and its resolved value is masked by the sink below.
    snapshot = [redact_totp_secrets(s) for s in scenarios]
    # The merged Result tab renders each scenario as a structured view (definitions) with a toggle
    # to the raw YAML (sources). The same helper feeds the offline re-render, so the two match
    # whenever `plan_sources` is unavailable there too (an older run, or one BE-cli didn't produce).
    definitions, sources = scenario_render_inputs(snapshot, plan_sources)
    source_files, step_lines = scenario_source_meta(snapshot, plan_sources)
    writer = RunArtifactWriter(run_dir, Redactor(None, values=secret_values))
    # Keep the executed scenario alongside its results (re-runnable / reviewable).
    scenario_yaml = dump_scenario_file(snapshot, description)
    writer.write_text("scenario.yaml", scenario_yaml)
    # Stamp the run's identity (scenario fingerprint + tool/git version) so accumulated runs can be
    # grouped to tell true flakiness from an edited scenario (BE-0049); pure metadata, never a verdict.
    provenance = run_provenance(
        scenario_yaml, git_revision=git_revision(), config_source=config_source
    )
    # Record what the upload-execution policy did with this run's launchServer command (BE-0090) —
    # denied / reused / sandboxed, and (when sandboxed) the image — so "what did this run execute,
    # and what was suppressed?" stays answerable. None for an ungoverned (local/Git) run.
    if exec_provenance is not None:
        provenance["uploadExec"] = exec_provenance
    return write_report(
        run_dir,
        run_id,
        results,
        definitions,
        sources,
        source_name=source_name,
        description=description,
        provenance=provenance,
        writer=writer,
        target=target,
        label=label,
        source_files=source_files,
        step_lines=step_lines,
    )
