"""Run one scenario's steps to a verdict — the deterministic core of `run`."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import replace

from bajutsu.common import assertions
from bajutsu.common.assertions import AssertionResult, EvalContext
from bajutsu.common.cancellation import (
    CANCELLED_FAILURE,
    CancelSource,
    RunCancelled,
    cancelled_teardown_seconds,
    grace_seconds,
    not_cancelled,
)
from bajutsu.common.drivers import base
from bajutsu.common.drivers.actuation import Actuation
from bajutsu.common.drivers.webview import DomSource
from bajutsu.common.evidence import Artifact, EvidenceSink, NullSink, intervals
from bajutsu.common.evidence.network import (
    Collector,
    InAppCapability,
    TransitionSource,
    _no_transitions,
)
from bajutsu.common.mailbox import extract_value, select
from bajutsu.common.orchestrator.actions import _do_action, handle_system_alert_selector
from bajutsu.common.orchestrator.control_channel import ControlChannelError, capability_suspended
from bajutsu.common.orchestrator.evidence_rules import _extract_stable_key, requested_intervals
from bajutsu.common.orchestrator.substitution import _interp_asserts
from bajutsu.common.orchestrator.types import (
    AlertEvent,
    AlertGuardConfig,
    Clock,
    DeviceControl,
    MailboxReader,
    NetworkSource,
    ProgressFn,
    RealClock,
    RelaunchFn,
    RunResult,
    SelectionState,
    StepOutcome,
    UndeclaredInterruption,
    WallClock,
    _no_network,
    drain_actuations,
    drain_interruptions,
    scenario_slug,
    undeclared_interruption_note,
)
from bajutsu.common.orchestrator.waits import (
    WaitTick,
    WaitTrace,
    _adaptive_sleep,
    _timeout_floor,
    _wait,
    settle_after_alert_dismiss,
    wait_for_system_alert,
)
from bajutsu.common.scenario import (
    AfterRule,
    Assertion,
    Email,
    Extract,
    ForEach,
    If,
    Interrupt,
    Scenario,
    Selector,
    Step,
    interp,
)

from ._loop_config import _LoopConfig
from ._shared import _ExecSteps, _logger
from ._step_counter import _StepCounter
from .step_loop_state import StepLoopState

# How often `email` re-polls the mailbox. Unlike the UI's 50 ms `_POLL`, each tick is a remote HTTP
# request to a (often rate-limited / metered) provider, so it polls about once a second.
_EMAIL_POLL = 1.0


# Assertion kinds whose result a tree re-read cannot change: the clipboard and screenshot are read
# once, and the network kinds have their own `wait until: request`. Waiting on one of these would
# only idle to the deadline, so the poll stops as soon as every *still-failing* assertion is one of
# them. Any other (tree-derived) kind — value / label / exists / count / state / golden, and any
# future UI kind — keeps the wait, which is the read-race this poll exists to close.
_READ_ONCE_KINDS = frozenset(
    {"clipboard", "visual", "request", "responseSchema", "requestSequence", "event"}
)

# Mid-wait TipKit dismisses allowed within one step, for the same reason as the two ceilings above.
# TipKit dismisses on its own rules, so "the scrim tap did not clear the tip" is a state the guard has
# to survive rather than one it can rule out. Unbounded, a guarded wait would then synthesize a tap
# every tick for its whole timeout, each recorded as an actuation, and land one on whatever is
# underneath the moment the tip finally closes on its own. Bounded, "the dismiss didn't take" degrades
# to the step's ordinary failure or timeout, exactly as a mis-set `interrupts` entry does. One hook is
# built per step and shared by both retries, so the total per step is this plus the end-of-step
# dismiss's own single attempt — the same composition the alert guard already documents for
# _GUARD_MAX_ATTEMPTS.
_TIP_MAX_DISMISSES = 2


def _fail_reason(results: list[AssertionResult]) -> str:
    return "; ".join(r.reason for r in results if not r.ok)


def _poll_asserts(
    driver: base.Driver,
    asserts: list[Assertion],
    network: NetworkSource,
    clock: Clock,
    *,
    ctx: EvalContext,
    cancelled: CancelSource = not_cancelled,
) -> tuple[list[AssertionResult], list[base.Element]]:
    """Evaluate `asserts` as a condition wait: re-read the tree until it passes or the deadline.

    Polling `query()` until `passed()` — bounded by a wall-clock deadline, never a fixed sleep — is
    what keeps a fast read (the resident channel, ~0.1s) no more flaky than a slow one (`uiautomator
    dump`, ~2.4s, which incidentally waited out an action's async-mirrored value: a value an action
    mirrors into the tree can land a beat after the action returns, as Compose recomposes the
    `content-desc` asynchronously). The wait budget is the lane's wait floor
    (`BAJUTSU_MIN_WAIT_TIMEOUT`), the same knob every other condition wait honors — so it is zero
    (a single read, today's behavior) on lanes that don't set it, and the Android e2e lane's 15s
    where the race lives. Only the UI tree goes stale, so only it is re-read; the caller takes the
    screenshot and reads the clipboard once, and the poll ends the moment nothing a tree re-read
    could fix is still failing (`_READ_ONCE_KINDS`).

    Returns the final results and the last tree read, so a step-level caller can reuse that settled
    tree as its `after` snapshot instead of re-querying (BE-0299 Unit 1 / BE-0259).

    `cancelled` (BE-0370) raises `RunCancelled` out of the poll, like every other condition wait, once
    the results are in — so an `assert` already satisfied on that poll still passes.
    """
    deadline = clock.now() + _timeout_floor()
    while True:
        t0 = clock.now()
        tree = driver.query()
        results = assertions.evaluate(tree, asserts, network(), ctx=ctx)
        if assertions.passed(results) or clock.now() >= deadline:
            return results, tree
        if cancelled():
            raise RunCancelled
        if all(r.ok or r.kind in _READ_ONCE_KINDS for r in results):
            return results, tree  # only read-once assertions are left failing; a re-read can't help
        _adaptive_sleep(clock, t0)


def _evaluate_expect(
    driver: base.Driver,
    expect: list[Assertion],
    network: NetworkSource,
    clock: Clock,
    *,
    ctx: EvalContext,
) -> list[AssertionResult]:
    """Evaluate the trailing `expect` block as a condition wait (BE-0245), via `_poll_asserts`.

    The scenario-level `expect` needs only the assertion results, not the settled tree, so it drops
    the tree `_poll_asserts` also returns.
    """
    results, _ = _poll_asserts(driver, expect, network, clock, ctx=ctx)
    return results


def _settle_extract_read(
    driver: base.Driver,
    extracts: Mapping[str, Extract],
    clock: Clock,
    *,
    initial: list[base.Element] | None = None,
    actuated_at: float | None = None,
) -> list[base.Element]:
    """Read the post-step tree, polling until the properties `extract` reads stop changing.

    An `extract` has no assertion to satisfy — it copies a value out — so this is the settle-shaped
    sibling of `_poll_asserts`: it stops when two consecutive reads share the extract projection
    (`_extract_stable_key`), or the same wall-clock deadline (`BAJUTSU_MIN_WAIT_TIMEOUT`) elapses.
    With no wait floor the budget is zero, so it reads exactly once — today's single-read behavior on
    every lane that does not set the floor (BE-0299 Unit 3).

    `actuated_at` anchors an actuation-anchored barrier (BE-0332 Unit 1): on a backend that declares a
    `read_lag()`, two agreeing reads are not trusted until they also postdate `actuated_at` by that
    budget. On Android the tree can keep publishing the pre-tap value for a beat, so the first reads
    after an action agree with each other on a value the action already superseded (`extract.yaml`
    binds the counter's previous value); the barrier holds the poll across that window. It is passed
    only on the mutating path, where an actuation this step is what a read must postdate — a seeded
    (`initial=`) read did not actuate, so it has no window to wait out. A backend reporting no lag,
    and any read with no `actuated_at`, keep the plain two-agreeing-reads settle byte-for-byte.

    The backend's read mark (`ReadOrderProvider`, BE-0332 Unit 3) deliberately does **not** release this
    poll early, though it did until `smoke (adb)` reproduced the very failure BE-0332 set out to close
    (`step 4 (assert_): expected equals='2' but actual='3'`). The mark answers "an accessibility event
    postdates the gesture", and this poll needs "the property I am copying out has been republished".
    They are not the same question, because one gesture produces several events: Compose publishes the
    tapped button's own event before the `Text` mirroring the new count recomposes. A read taken between
    the two postdates the tap and still carries the previous value, and the read after it agrees — so
    the mark and the two-agreeing-reads test both pass on a stale pair. Ordering is the right question
    for the driver's own catch-up barrier, which waits on *frames* the mark does speak for; it is the
    wrong question for a *value*. So the wall-clock budget stays this poll's only release, and a lag
    exceeding the lane's floor is simply never met and falls through to the latest read.

    `initial`, when given, is the seed a non-mutating step (`assert` / `wait`) already settled on: it
    is taken as the first sample so the poll refines that seed in place rather than re-reading it,
    which is why this is applied at that earlier read site — the seed short-circuits `_ScreenRead`, so
    the poll cannot live there for a seeded step. A mutating step passes no seed and reads fresh.
    """
    lag = driver.read_lag() if isinstance(driver, base.ReadLagProvider) else 0.0
    barrier = actuated_at + lag if actuated_at is not None else None
    deadline = clock.now() + _timeout_floor()
    tree = initial if initial is not None else driver.query()
    key = _extract_stable_key(tree, extracts)
    while clock.now() < deadline:
        t0 = clock.now()
        next_tree = driver.query()
        next_key = _extract_stable_key(next_tree, extracts)
        settled = next_key == key
        # `barrier` is the wall-clock ceiling the wait falls through at, and the only thing separating
        # a pair that agrees because the value settled from one that agrees because both reads landed
        # before it republished. The docstring above says why the read mark cannot stand in for it.
        if settled and (barrier is None or clock.now() >= barrier):
            return next_tree
        tree = next_tree
        if not settled:
            key = next_key
        _adaptive_sleep(clock, t0)
    # Deadline hit while the extract projection was still moving (or still inside the read-lag window):
    # return the latest read (best-effort, like the driver `_settle`), and say so, so a later assert
    # failing on a still-propagating value is traceable to an un-settled extract rather than looking
    # inexplicable (BE-0299 Unit 3).
    _logger.debug(
        "extract settle: projection still changing at the wait deadline; using latest read"
    )
    return tree


def _clipboard_for(block: list[Assertion], control: DeviceControl | None) -> str | None:
    """The device pasteboard, read once when `block` has a `clipboard` assertion; None otherwise.

    None when no `clipboard` assertion is present, when no device-control channel is available
    (fake driver / parallel run), or when the read itself fails (`simctl pbpaste` errored). In every
    None case a `clipboard` assertion fails cleanly via `evaluate` rather than aborting the run —
    the read is a verification input, not a scenario step."""
    if control is None or not any(a.clipboard is not None for a in block):
        return None
    try:
        return control.get_clipboard()
    except (OSError, subprocess.CalledProcessError):
        return None


def _do_email(
    email: Email,
    clock: Clock,
    mailbox: MailboxReader | None,
    bindings: dict[str, str] | None,
    cancelled: CancelSource = not_cancelled,
) -> tuple[bool, str]:
    """Poll the mailbox until a matching message arrives, then extract its value into `vars.*`.

    A condition wait bounded by `email.timeout` (never a fixed sleep): it baselines the ids present
    at the start so only mail arriving *after* counts (skew-free), then re-fetches until a match or
    the deadline. A missing mailbox, a timeout, or a matched message whose body the regex can't hit
    is a clean failure — never a silent wrong value. `mailbox.fetch` raising `SelectorError` (an
    unreachable / non-2xx endpoint) propagates to the caller's handler, which records it as a failure.

    `cancelled` (BE-0370) raises `RunCancelled` out of the poll, like every other condition wait. This
    wait needs the check as much as any: `email.timeout` is whatever the scenario asked for — a wait
    for a one-time password commonly runs to a minute or more — so a cancelled run stuck here could
    otherwise outlive the grace window and be killed before it wrote its manifest.
    """
    if mailbox is None:
        return False, "email: no mailbox configured (set targets.<name>.mailbox)"
    if bindings is None:  # defensive: the run loop always passes a dict for a step
        return True, ""
    deadline = clock.now() + email.timeout
    baseline = frozenset(m.id for m in mailbox.fetch(email.timeout))
    while True:
        remaining = deadline - clock.now()
        if remaining <= 0:
            return False, f"email: no matching message within {email.timeout:g}s"
        # Bound each fetch by the time left, so a single hung request can't overrun email.timeout.
        picked = select(mailbox.fetch(remaining), email.match, baseline)
        if picked is not None:
            value = extract_value(picked.body, email.extract)
            if value is None:
                return False, "email: matched a message but extract regex did not match its body"
            bindings[f"vars.{email.extract.var}"] = value
            return True, ""
        if cancelled():
            raise RunCancelled
        clock.sleep(min(_EMAIL_POLL, deadline - clock.now()))


def _run_step_body(
    driver: base.Driver,
    step: Step,
    kind: str,
    clock: Clock,
    network: NetworkSource,
    relaunch: RelaunchFn | None = None,
    bindings: dict[str, str] | None = None,
    control: DeviceControl | None = None,
    mailbox: MailboxReader | None = None,
    ctx: EvalContext | None = None,
    wait_trace: WaitTrace | None = None,
    selection: SelectionState | None = None,
    alert_guard: AlertGuardConfig | None = None,
    alerts: list[AlertEvent] | None = None,
    on_wait_tick: WaitTick | None = None,
    transitions: TransitionSource = _no_transitions,
    on_interrupt_poll: Callable[[list[base.Element]], bool] | None = None,
    cancelled: CancelSource = not_cancelled,
) -> tuple[bool, str, list[AssertionResult], list[base.Element] | None]:
    """Execute one step's effect, returning (ok, reason, assertion_results, snapshot).

    ``snapshot`` is the settled tree a non-mutating step (`assert`, `wait`) already queried to
    evaluate itself; the caller reuses it as the step's `after` instead of re-querying (BE-0259). It
    is ``None`` for steps that mutate the screen (`tap`, `type`, …) or read no tree (`email`,
    `wait until: request`), so the post-step read falls back to a fresh query for exactly the steps
    where "before" and "after" may differ.

    The caller is responsible for interpolation (``_interp_step``) before
    calling this function. ``wait_trace``, when given for a wait step, records the poll timeline so a
    timeout is diagnosable from artifacts (BE-0231 Unit 1). ``alert_guard``/``alerts``, when given
    for a ``wait`` or ``handleSystemAlert`` step, drive the alert guard while that step's own wait
    runs (BE-0269, BE-0406); other step kinds ignore them. ``on_interrupt_poll``, when given for a
    wait step, is passed to ``_wait`` so a scenario's ``interrupts`` handlers can clear an
    interstitial screen mid-wait (BE-0314). ``cancelled`` reaches the four step kinds that poll —
    ``wait``, ``handleSystemAlert``, ``assert``, and ``email`` — so each notices a cancelled run
    within one polling tick (BE-0370)."""
    try:
        if kind == "wait":
            assert step.wait is not None
            ok, reason, tree = _wait(
                driver,
                step.wait,
                clock,
                network,
                trace=wait_trace,
                alert_guard=alert_guard,
                alerts=alerts,
                on_tick=on_wait_tick,
                transitions=transitions,
                on_interrupt_poll=on_interrupt_poll,
                cancelled=cancelled,
            )
            return ok, reason, [], tree
        if kind == "handle_system_alert":
            assert step.handle_system_alert is not None
            # Handled here rather than through `_do_action` for the same reason `wait` is: the wait
            # needs the clock and the scenario's alert guard, and the action-handler signature
            # carries neither (BE-0406).
            ok, reason = wait_for_system_alert(
                driver,
                handle_system_alert_selector(step),
                step.handle_system_alert.timeout,
                clock,
                alert_guard=alert_guard,
                alerts=alerts,
                cancelled=cancelled,
            )
            if ok and selection is not None:
                # `_do_action` invalidates the live selection after every action but `select` and
                # `copy` (BE-0265), and this branch bypasses it. The step actuates the device, so a
                # `copy` after it must fail for want of a selection rather than copy whatever the
                # tap left — and only on a tap that landed, matching `_do_action`, which skips the
                # invalidation when its handler raises.
                selection.invalidate()
            return ok, reason, [], None
        if kind == "email":
            assert step.email is not None
            ok, reason = _do_email(step.email, clock, mailbox, bindings, cancelled)
            return ok, reason, [], None
        if kind == "assert_":
            assert step.assert_ is not None
            clip = _clipboard_for(step.assert_, control)
            # A step-level assert sees only golden + clipboard: no per-step screenshot is taken, so
            # `visual` / `responseSchema` have no fresh input here (they run at scenario `expect`).
            # Drop them from the bundled context to preserve that behavior (BE-0250 Unit 2).
            step_ctx = replace(ctx or EvalContext(), visual=None, schema=None, clipboard=clip)
            # A condition wait, not a single snapshot: a value the prior action mirrors into the tree
            # a beat late is caught, the same race the trailing `expect` already closes (BE-0299
            # Unit 2). Zero-budget (no wait floor) reads exactly once, as before.
            results, tree = _poll_asserts(
                driver, step.assert_, network, clock, ctx=step_ctx, cancelled=cancelled
            )
            ok = assertions.passed(results)
            return ok, "" if ok else _fail_reason(results), results, tree
        _do_action(driver, step, relaunch, control, bindings, selection)
        # Four branches return from this block; hoisting only the last into an `else` would suggest
        # the other three are not on the success path.
        return True, "", [], None  # noqa: TRY300
    except (
        base.SelectorError,
        base.ElementNotTappable,
        base.UnsupportedAction,
        NotImplementedError,
    ) as e:
        return False, str(e), [], None


def _resolve_video_start_offset(
    video_interval: intervals.Interval | None, scenario_start: float
) -> float:
    """The correction the report's video anchor (`RunResult.video_anchor_s`) is offset by.

    Resolved from the *finished* recording where it can be: `Interval.measured_start` is
    the recorder's own answer — the instant it was stopped, minus the duration the finalized file
    states — so it names the moment the first frame was captured rather than the moment some side
    signal fired. It is trusted in both directions, because a recording whose footage begins after
    `scenario_start` is an ordinary outcome of a start that lagged its confirmation, not the
    anomaly the `true_start` branch below treats it as; the report's own `max(0.0, …)` floor keeps
    an early step on the recording.

    `video_interval.true_start` (confirmed or driver-stamped) is the fallback for a recording whose
    duration could not be read. It may precede or follow `scenario_start` — a prestarted device
    recording begins before it, an on-demand iOS recording's confirmation wait completes just
    before it — so this offset places the anchor near the video's origin instead of at the moment
    `scenario_start` happened to be stamped. `0.0` (no correction) both when no confirmed
    `true_start` exists and when the resolved offset is positive: a video starting *after*
    `scenario_start` is not a case that branch expects in production (see BE-0346's Motivation), so
    it is surfaced with a warning rather than trusted. The guard is one-sided by construction: a
    *negative* offset is trusted unconditionally, so a stale `true_start` — necessarily an older
    instant than `scenario_start`, and so always negative — is not caught here.

    Mixing the two time sources is deliberate but load-bearing: both `measured_start` and
    `true_start` are raw `time.monotonic()` instants, so `clock` must share that epoch
    (`RealClock`). A clock with a different origin makes this offset — and so every video-relative
    second a report derives from the anchor — meaningless rather than merely shifted.
    """
    if video_interval is None:
        return 0.0
    if video_interval.measured_start is not None:
        return video_interval.measured_start - scenario_start
    if video_interval.true_start is None:
        return 0.0
    offset = video_interval.true_start - scenario_start
    if offset > 0:
        _logger.warning(
            "video true_start (%s) is after scenario_start (%s) for kind=%s provider=%s; this is "
            "not expected in production, so the video-sync correction is skipped for this "
            "scenario rather than trusted",
            video_interval.true_start,
            scenario_start,
            video_interval.kind,
            video_interval.provider,
        )
        return 0.0
    return offset


def _dispatch_after(
    rules: list[AfterRule],
    failure: str | None,
    run_steps: Callable[[list[Step], CancelSource], str | None],
    clock: Clock,
    cancelled: CancelSource,
) -> tuple[str | None, str]:
    """Run the `after` rules this run's verdict selects; return the run's composed failure (BE-0392).

    Entries run in declaration order — interleaved, not grouped by `on` — so the scenario-then-config
    merge order holds across the whole phase rather than only within one outcome group. The verdict
    is fixed before the first entry runs: an entry's own failure never re-dispatches the ones after
    it. A failing entry does not stop the phase either, since skipping the remaining cleanup is the
    outcome teardown exists to avoid.

    Args:
        failure: The run's failure so far, or None if it was passing. A failing entry becomes the
            failure on a passing run and is appended to it otherwise, so the reason a reader sees
            first stays the original cause rather than a symptom of the cleanup it triggered.
        run_steps: Runs one entry's steps under the `CancelSource` handed to it, returning that
            entry's failure or None.
        cancelled: The run's own cancel source, used only when the run was *not* cancelled.

    Returns:
        The run's failure after the phase, and the verdict it dispatched on — the report needs the
        latter to know which rules ran, which `failure` alone can no longer say once a cleanup
        step's own reason has been folded into it.
    """
    verdict = "success" if failure is None else "error"
    # A run that is shutting down cannot be bounded by `cancelled`: that source is latched, so its
    # first read inside this phase would raise and no cleanup step would run at all. Give the phase
    # its own deadline instead — cleanup gets a bounded chance to run without pushing the shutdown
    # tail past the window `serve` waits before an unconditional kill. The latch is read here rather
    # than inferred from `failure` alone, because a cancel arriving after the last step's boundary
    # check leaves `steps`/`expect` to finish and `failure` unset (BE-0370 keeps that scenario's real
    # verdict) while the process is shutting down all the same.
    bounded = cancelled() or (failure is not None and failure.startswith(CANCELLED_FAILURE))
    phase_cancelled = cancelled
    if bounded:
        deadline = clock.now() + cancelled_teardown_seconds(grace_seconds())

        def phase_cancelled() -> bool:
            return clock.now() >= deadline

    for rule in rules:
        if rule.on != "always" and rule.on != verdict:
            continue
        try:
            reason = run_steps(rule.steps, phase_cancelled)
        except RunCancelled:
            if bounded:
                # The teardown budget above, spent. Abandoning the rest is the designed bound, not a
                # new failure: a run cancelled early already carries that as its reason, and one
                # cancelled after its last boundary keeps the real verdict BE-0370 gives it. Either
                # way the After block's not-run rows are what disclose the abandoned entries.
                _logger.debug("after: teardown budget spent; abandoning the remaining entries")
                break
            # A cancel that arrived *during* teardown. On a passing run the failure must still lead
            # with the cancellation spelling downstream reads (BE-0370).
            if failure is None:
                return CANCELLED_FAILURE, verdict
            return f"{failure}; after: {CANCELLED_FAILURE}", verdict
        if reason is not None:
            failure = f"after: {reason}" if failure is None else f"{failure}; after: {reason}"
    return failure, verdict


def _hides_touch_markers(
    scenario: Scenario, target_launch_env: Mapping[str, str] | None = None
) -> bool:
    """Whether this scenario's `visual` capture has to hide the in-app touch markers (BE-0365).

    Both launch-env keys, never either alone: the markers are what would land in the compared image,
    and the channel is the only way to take them back out of it. `run --touch-markers` sets the pair
    only on the screenshot-comparing scenarios that can carry the channel, and never on one whose
    verdict reads no screenshot, so a scenario drawing markers without the channel — one that pinned
    the marker key itself on a run that armed no channel for it, say — is left alone rather than
    failed for a capture it never asked bajutsu to correct.

    `target_launch_env` is the target's own `launchEnv`, which the launch merges *underneath* the
    scenario's (`environments/xcuitest.py`'s `_launch_params`); this predicate merges the same two
    layers, in the same order, before reading either key — so a target pinning
    `BAJUTSU_TOUCH_MARKERS` for every scenario is seen here exactly as the app that actually launched
    sees it, matching `_apply_touch_markers` (`run/cli.py`), which reads the same merge. A caller
    that passes `None` (a test constructing a scenario directly) sees the unchanged
    scenario-launch-env-only behavior.
    """
    env = {**(target_launch_env or {}), **scenario.preconditions.launch_env}
    return env.get("BAJUTSU_TOUCH_MARKERS") == "1" and env.get("BAJUTSU_CONTROL_CHANNEL") == "1"


def _capture_visual_actual(
    ctx: EvalContext,
    driver: base.Driver,
    *,
    channel: Collector | None,
    hide_markers: bool,
    cancelled: CancelSource,
) -> None:
    """Capture the image this scenario's `visual` assertions read, marker-free when it must be."""
    if ctx.visual is None:
        return
    if not hide_markers:
        ctx.visual.capture_actual(driver)
        return
    # The suspension is acknowledged at both edges, so the shutter fires against a screen the app
    # confirmed is clear of markers rather than one it was merely asked to clear (BE-0365 unit 3).
    with capability_suspended(channel, InAppCapability.TOUCH_VISUALIZATION, cancelled=cancelled):
        ctx.visual.capture_actual(driver)


def run_scenario(
    driver: base.Driver,
    scenario: Scenario,
    clock: Clock | None = None,
    sink: EvidenceSink | None = None,
    alert_guard: AlertGuardConfig | None = None,
    scenario_id: str | None = None,
    network: NetworkSource = _no_network,
    relaunch: RelaunchFn | None = None,
    bindings: Mapping[str, str] | None = None,
    control: DeviceControl | None = None,
    progress: ProgressFn | None = None,
    ctx: EvalContext | None = None,
    mailbox: MailboxReader | None = None,
    webview_bridge: DomSource | None = None,
    transitions: TransitionSource = _no_transitions,
    interrupts: list[Interrupt] | None = None,
    locale: str | None = None,
    wall_clock: WallClock = time.time,
    capture: list[str] | None = None,
    cancelled: CancelSource = not_cancelled,
    channel: Collector | None = None,
    target_launch_env: Mapping[str, str] | None = None,
) -> RunResult:
    """Run one scenario deterministically, firing capturePolicy rules into `sink`.

    Heavy scenario-wide intervals (video / deviceLog / appTrace) are opt-in (BE-0028): the sink
    starts only the interval kinds the scenario actually requests (`requested_intervals`) before
    the first step and finalizes them after verification, attaching them to the result. A scenario
    that requests none records no intervals; the instant baseline still fires every step.

    If a step fails and `alert_guard` clears a blocking condition (e.g. dismisses a
    system alert), the step is retried once before being recorded as a failure.

    `transitions` (BE-0310) is the read-only screen-transition signal a `wait until: settled` step
    consults in place of tree-diff polling; the default reports none, so a caller that doesn't pass
    one (most callers, and every non-iOS backend) sees the unchanged tree-diff behavior.

    `wall_clock` (BE-0348) is read exactly once, beside `clock.now()`, to form the anchor pair every
    recorded timestamp is derived from. Every timing *decision* still reads `clock` alone, so a
    backward wall-clock jump can never shorten a wait or a duration.

    `capture` (the resolved `Effective.capture`, config's `defaults.capture`) is a baseline
    guarantee applied on top of every step, alongside `capturePolicy` rules and inline
    `capture:` tokens — the default is empty, so a caller that doesn't pass one (a test
    constructing a scenario directly) sees the unchanged capturePolicy/inline-only behavior.

    The scenario's `before` / `after` lifecycle phases (BE-0392) are read off `scenario` itself,
    not passed alongside it the way `interrupts` is: `runner.pipeline` folds the target config's own
    phases into each scenario before the run, so the scenario this function executes and the scenario
    the report renders are the same object. A caller that builds a `Scenario` directly therefore gets
    exactly the phases it declared.

    `channel` (BE-0365) is the run's collector, carried here only so a `visual` verdict can hide the
    in-app touch markers for the capture it compares and restore them after. It is `None` on every
    caller that has no collector, and that is *not* inert: the toggle is attempted whenever this
    scenario's effective launch env — `target_launch_env` merged with the scenario's own, the same
    order the launch itself merges them in — sets both `BAJUTSU_TOUCH_MARKERS` and
    `BAJUTSU_CONTROL_CHANNEL` to `"1"` and its `expect` phase has a `visual` capture to take — which a
    scenario or target pinning the pair reaches whether or not `run --touch-markers` was passed — so
    a `None` channel there fails the scenario loudly rather than skipping the suspension.
    `target_launch_env` is the target's own `launchEnv` (`Effective.launch_env`); a caller that omits
    it (a test constructing a scenario directly) sees only the scenario's own launch env, as before.

    `cancelled` (BE-0370) makes a cancelled run land as an ordinary failed scenario: it is read at
    each step boundary and inside the poll loops that back every condition wait, and the resulting
    `RunCancelled` is turned into `failure: "cancelled"` here. The trailing `expect` re-check is
    deliberately left to finish — a scenario whose every step passed gets its real verdict rather
    than a cancellation label, and that block is bounded by the wait floor (zero on every lane that
    doesn't raise it).

    Unlike every other exception this catches, `base.BackendCrashError` is never turned into a
    `failure` here — it propagates, so the run pipeline's own crash-retry loop can see it. The
    interval finalize still runs first (this function's own `finally`), so a recording that was in
    flight when the backend died may already be finalized on disk; that result is attached to the
    exception itself (`crash.partial_artifacts`) before it propagates, so the pipeline's
    exhausted-retry `RunResult` can still show a video that *was* captured on the doomed attempt.
    """
    clock = clock or RealClock()
    sink = sink or NullSink()
    ctx = ctx or EvalContext()
    sid = scenario_id or scenario_slug(scenario.name)
    hide_markers = _hides_touch_markers(scenario, target_launch_env)
    recordings = sink.start_scenario_intervals(sid, requested_intervals(scenario, capture))
    wants_screen_changed = any(r.on.event == "screenChanged" for r in scenario.capture_policy)
    outcomes: list[StepOutcome] = []
    before_outcomes: list[StepOutcome] = []
    after_outcomes: list[StepOutcome] = []
    after_verdict = ""
    # One counter for the whole `after` phase: it runs one `run_phase` call per dispatched rule, and
    # a per-call counter would restart each rule at zero, colliding their evidence `step_id`s.
    after_counter = _StepCounter()
    expect_results: list[AssertionResult] = []
    expect_alerts: list[AlertEvent] = []
    # An alert that interrupted one of `expect`'s own queries and matched no `rules` entry — filled by
    # every drain this phase runs, and failing the phase outright once anything lands here (BE-0406
    # Unit 2b), unlike `expect_alerts` above which never fails anything on its own.
    expect_undeclared: list[UndeclaredInterruption] = []
    # The guard's expect-phase dismissing tap: the one actuation that happens outside the step loop, so
    # it is drained here rather than left in the driver's log with no step to carry it (see BE-0315's
    # `expect_alerts` beside it).
    expect_actuations: list[Actuation] = []
    # What the guard saw blocking the screen during the `expect` retry and could not clear (BE-0402).
    expect_block_note = ""
    failure: str | None = None
    artifacts: list[Artifact] = []
    # The anchor pair: a monotonic instant every in-run duration is measured from, and the wall-clock
    # instant it corresponds to. Read back to back so the two describe the same moment as closely as
    # the platform allows — `wall_offset_s` is their difference, and every recorded timestamp is
    # `t + wall_offset_s` for a monotonic instant `t` from this run (the step loop below and
    # `pipeline.py`'s network write both spell the conversion exactly that way).
    scenario_start = clock.now()
    scenario_wall_start = wall_clock()
    wall_offset_s = scenario_wall_start - scenario_start
    # The offset this interval's recording implies is resolved once it is finalized, in the `finally`
    # below — the exact answer is the finished file's own duration, which does not exist yet here.
    video_interval = next((r for r in recordings if r.kind == "video"), None)
    # Mutable bindings: extract steps populate vars.* during the run; scenario-level
    # expect sees the accumulated values.
    live_bindings: dict[str, str] = dict(bindings or {})

    def run_phase(
        steps: list[Step],
        phase_outcomes: list[StepOutcome],
        phase: str,
        phase_cancelled: CancelSource,
        counter: _StepCounter | None = None,
    ) -> str | None:
        # Every phase shares `live_bindings`, so a `before` step's `vars.*` reaches `steps` and an
        # `after` step can address what `steps` captured; everything else is per-phase.
        return _run_steps(
            driver,
            scenario,
            steps,
            clock,
            sink,
            alert_guard,
            wants_screen_changed,
            phase_outcomes,
            wall_offset_s,
            sid,
            network,
            relaunch,
            live_bindings,
            control,
            progress,
            mailbox,
            ctx,
            webview_bridge,
            transitions,
            interrupts,
            locale,
            capture,
            phase_cancelled,
            phase,
            counter,
        )

    try:
        try:
            try:
                if scenario.before:
                    # A precondition for the scenario, not a step within it: its failure skips `steps`
                    # and `expect` outright, the way an unsatisfiable `preconditions` already fails a
                    # scenario before this function is reached at all.
                    reason = run_phase(list(scenario.before), before_outcomes, "before", cancelled)
                    if reason is not None:
                        failure = "before: " + reason
                if failure is None:
                    failure = run_phase(scenario.steps, outcomes, "", cancelled)
                if failure is None and scenario.expect:
                    expect = _interp_asserts(scenario.expect, live_bindings)
                    clip = _clipboard_for(expect, control)
                    _capture_visual_actual(
                        ctx, driver, channel=channel, hide_markers=hide_markers, cancelled=cancelled
                    )
                    expect_results = _evaluate_expect(
                        driver, expect, network, clock, ctx=replace(ctx, clipboard=clip)
                    )
                    # A prompt the backend answered or declined while it was interrupting one of
                    # `expect`'s own queries. Outside the failure branch below: an `expect` that passed
                    # *because* the interruption was answered still has a dismissal to report, and a
                    # declined one fails the phase outright regardless of what the assertions found. The
                    # step loop's own drain has already run and does not cover this phase's queries, and
                    # the next scenario's `setPolicy` clears the buffer outright, so this is the only
                    # chance to read what happened here.
                    expect_drained = drain_interruptions(driver)
                    expect_alerts.extend(expect_drained.alerts)
                    expect_undeclared.extend(expect_drained.undeclared)
                    if not assertions.passed(expect_results) and alert_guard is not None:
                        event = alert_guard(driver)
                        if event is None and alert_guard.blocked_note:
                            # The guard saw a prompt it could not clear (BE-0402 leaves an alert no rule
                            # identifies alone rather than guessing where to tap). Name it on the
                            # `expect` failure below, which would otherwise report only the assertion
                            # that never held.
                            expect_block_note = alert_guard.blocked_note
                        if event is not None:
                            expect_alerts.append(event)
                            expect_actuations.extend(drain_actuations(driver).records)
                            # The prompt has been tapped, not yet cleared: let the sheet finish leaving
                            # and the screen it covered finish rendering, so the retry below judges the
                            # assertions against a still tree rather than one mid-animation (BE-0406).
                            settle_after_alert_dismiss(
                                driver, clock, transitions=transitions, cancelled=cancelled
                            )
                            _capture_visual_actual(
                                ctx,
                                driver,
                                channel=channel,
                                hide_markers=hide_markers,
                                cancelled=cancelled,
                            )
                            # Re-read the clipboard too: clearing the block may have let the app update the
                            # pasteboard, so the retry must compare against the fresh value, not the stale one.
                            clip = _clipboard_for(expect, control)
                            expect_results = _evaluate_expect(
                                driver, expect, network, clock, ctx=replace(ctx, clipboard=clip)
                            )  # retry once
                        # The guard's own probe just now, and the retry's queries when one ran, can
                        # each be interrupted too, and nothing else drains this phase again afterwards
                        # (BE-0406 Unit 2b). Outside the `event is not None` branch above so a probe
                        # that declined without clearing anything (`event is None`) is still covered.
                        retry_drained = drain_interruptions(driver)
                        expect_alerts.extend(retry_drained.alerts)
                        expect_undeclared.extend(retry_drained.undeclared)
                    if not assertions.passed(expect_results):
                        failure = "expect: " + _fail_reason(expect_results)
                        if expect_block_note:
                            failure += f" \u2014 {expect_block_note}"
                    if expect_undeclared:
                        # Overrides whatever `failure` above holds, even None when `expect` otherwise
                        # passed: an interruption no rule named is evidence the scenario's assumptions
                        # were wrong regardless of what the assertions checked afterward (BE-0406 Unit
                        # 2b). Appended to an existing failure rather than replacing it, so the
                        # assertion mismatch's own detail is not lost alongside the alert that caused it.
                        note = undeclared_interruption_note(expect_undeclared)
                        failure = f"{failure} \u2014 {note}" if failure else "expect: " + note
            except ControlChannelError as exc:
                # A command that could not be shown to have taken effect fails the scenario rather than
                # letting it proceed on an app state bajutsu never established (BE-0365). It lands as an
                # ordinary failure, so the verdict stays machine-checkable and the cause is in the report.
                failure = f"control channel: {exc}"
            except RunCancelled:
                # A cancelled run is a failed run, not a silent gap: the scenario the cancel interrupted
                # (or one whose first boundary was already past it) fails with the one spelling
                # downstream reads, and the `finally` below still finalizes its intervals — so the
                # report and the manifest are written exactly as they are for any other failure
                # (BE-0370).
                failure = CANCELLED_FAILURE
            if scenario.after:
                # Reached on every path out of `steps`/`expect`, the cancelled one included — the same
                # reason the `finally` below finalizes unconditionally.
                failure, after_verdict = _dispatch_after(
                    list(scenario.after),
                    failure,
                    lambda steps, src: run_phase(
                        steps, after_outcomes, "after", src, after_counter
                    ),
                    clock,
                    cancelled,
                )
        finally:
            artifacts = sink.finish_scenario_intervals(sid, recordings)
            # After the finalize, not before it: stopping the recording is what lets its own duration
            # place its origin, which is a measurement rather than the start-confirmation proxy a
            # scenario-start resolution would have to settle for (the correction BE-0346 introduced).
            video_start_offset = _resolve_video_start_offset(video_interval, scenario_start)
    except base.BackendCrashError as crash:
        # The `finally` above already ran, so a recording that was in flight when the backend
        # died is already finalized on disk and named in `artifacts` — attach it to the crash
        # itself before it propagates. The pipeline's exhausted-retry `RunResult` never reaches
        # the `return` below, so this is the only way a video that *was* captured on a doomed
        # attempt still reaches the report instead of a bare "video unavailable" disclosure.
        crash.partial_artifacts = artifacts
        raise

    return RunResult(
        scenario=scenario.name,
        ok=failure is None,
        steps=outcomes,
        expect_results=expect_results,
        failure=failure,
        artifacts=artifacts,
        backend=getattr(driver, "name", ""),
        duration_s=max(0.0, clock.now() - scenario_start),
        video_anchor_s=scenario_wall_start + video_start_offset,
        wall_offset_s=wall_offset_s,
        expect_alerts=expect_alerts,
        expect_actuations=expect_actuations,
        before_outcomes=before_outcomes,
        after_outcomes=after_outcomes,
        after_verdict=after_verdict,
    )


def _dismiss_blocking_tip(driver: base.Driver, scenario: Scenario) -> bool:
    """Clear a TipKit tip blocking the screen, when this scenario opted in; True if one was cleared.

    Deliberately narrow: it fires only after a step already failed, so a passing run never pays a
    query for it, and a scenario that did not ask keeps today's behavior exactly. The driver decides
    what identifies a tip, so no backend-specific selector reaches this layer.
    """
    if not scenario.ios_tip_kit_handling:
        return False
    if base.Capability.HANDLE_TIPKIT_TIP not in driver.capabilities():
        return False
    return driver.dismiss_blocking_tip()


def _tip_poll_hook(
    driver: base.Driver,
    scenario: Scenario,
    interrupt_poll: Callable[[list[base.Element]], bool] | None,
) -> Callable[[list[base.Element]], bool] | None:
    """Compose the mid-wait TipKit dismiss onto a step's interrupt-poll hook, or return it unchanged.

    Dismissing while the wait is still blocked is what keeps most runs off the post-failure retry
    path: the tip goes as soon as a poll sees it, and the wait's remaining budget is spent on the
    target rather than on a screen nothing can reach. The dismiss never ends the wait — `True` is
    reserved for "a recovery failed", so reporting one here would abort a wait that is now free to
    succeed. The poll's tree is passed down so the overwhelmingly common "no tip" answer needs no
    query of its own; only an actual tip costs the driver a fresh snapshot to mint a handle from.
    """
    if not scenario.ios_tip_kit_handling:
        return interrupt_poll
    if base.Capability.HANDLE_TIPKIT_TIP not in driver.capabilities():
        return interrupt_poll

    dismissed = 0

    def poll(elements: list[base.Element]) -> bool:
        nonlocal dismissed
        # The poll's own tree is handed to the driver, so ruling a tip out — the case on nearly every
        # tick — costs no query and a guarded wait polls at its usual rate.
        if dismissed < _TIP_MAX_DISMISSES and driver.dismiss_blocking_tip(elements):
            dismissed += 1
        return interrupt_poll(elements) if interrupt_poll is not None else False

    return poll


def _run_if(
    driver: base.Driver,
    if_block: If,
    network: NetworkSource,
    bindings: dict[str, str],
    exec_steps: _ExecSteps,
) -> tuple[bool, str]:
    """Evaluate the condition (with interpolation) and run the matching branch."""
    interp_condition = _interp_asserts([if_block.condition], bindings)[0]
    elements = driver.query()
    results = assertions.evaluate(elements, [interp_condition], network())
    branch = if_block.then if assertions.passed(results) else (if_block.else_ or [])
    if not branch:
        return True, ""
    failure = exec_steps(branch, driver)
    return (True, "") if failure is None else (False, failure)


def _run_for_each(
    driver: base.Driver,
    loop: ForEach,
    bindings: dict[str, str],
    exec_steps: _ExecSteps,
) -> tuple[bool, str]:
    """Iterate over elements matching the (interpolated) selector."""
    sel_dict = interp.interpolate(loop.sel.model_dump(by_alias=True), bindings)
    sel = Selector.model_validate(sel_dict).as_selector()
    elements = driver.query()
    matched = base.find_all(elements, sel)
    for el in matched:
        ident = el.get("identifier")
        if not ident:
            return False, f"forEach: matched element has no identifier (label={el.get('label')!r})"
        bindings[f"vars.{loop.as_}"] = ident
        failure = exec_steps(loop.steps, driver)
        if failure is not None:
            return False, failure
    return True, ""


def _run_steps(
    driver: base.Driver,
    scenario: Scenario,
    steps: list[Step],
    clock: Clock,
    sink: EvidenceSink,
    alert_guard: AlertGuardConfig | None,
    wants_screen_changed: bool,
    outcomes: list[StepOutcome],
    wall_offset_s: float,
    sid: str,
    network: NetworkSource,
    relaunch: RelaunchFn | None = None,
    bindings: dict[str, str] | None = None,
    control: DeviceControl | None = None,
    progress: ProgressFn | None = None,
    mailbox: MailboxReader | None = None,
    ctx: EvalContext | None = None,
    webview_bridge: DomSource | None = None,
    transitions: TransitionSource = _no_transitions,
    interrupts: list[Interrupt] | None = None,
    locale: str | None = None,
    capture: list[str] | None = None,
    cancelled: CancelSource = not_cancelled,
    phase: str = "",
    counter: _StepCounter | None = None,
) -> str | None:
    """Run one phase's step loop, appending outcomes; return the failure string or None.

    ``steps`` is the list to run — the scenario's own, or a `before` / `after` hook's (BE-0392).
    Each phase gets its own `StepLoopState`, so its steps are numbered from zero and reported as
    their own block, while ``bindings`` stays the one dict every phase shares.

    ``counter`` continues an already-started phase's numbering. The `after` phase runs one call per
    dispatched rule, so without it every rule would restart at zero — two rules' first steps would
    then claim the same evidence `step_id` and overwrite each other's screenshots.

    ``bindings`` is a mutable dict (guaranteed by ``run_scenario``) — extract
    steps add ``vars.*`` entries so that subsequent steps and scenario-level
    ``expect`` can reference them."""
    assert bindings is not None
    state = StepLoopState(counter=counter or _StepCounter(), outcomes=outcomes, bindings=bindings)
    cfg = _LoopConfig(
        driver=driver,
        scenario=scenario,
        clock=clock,
        sink=sink,
        alert_guard=alert_guard,
        wants_screen_changed=wants_screen_changed,
        wall_offset_s=wall_offset_s,
        sid=sid,
        network=network,
        relaunch=relaunch,
        control=control,
        progress=progress,
        mailbox=mailbox,
        ctx=ctx,
        webview_bridge=webview_bridge,
        transitions=transitions,
        interrupts=interrupts,
        locale=locale,
        capture=capture,
        phase=phase,
        cancelled=cancelled,
    )
    # Imported in the body, not at module load: `_StepRunner` calls six helpers from this module,
    # so rule 5 breaks the cycle the split creates on the single edge back into it.
    from ._step_runner import _StepRunner

    result = _StepRunner(state, cfg).exec_steps(steps, driver)
    _logger.debug("%s: %d runner-issued screen reads (BE-0234)", sid, state.total_reads)
    # No end-of-run safety capture here: every step that acts shoots its own `after.png` in
    # `_handle_action`, so the net only reached the step that returns before acting at all, where it
    # paired post-run pixels with a pre-action tree (BE-0341, "Later revision").
    return result
