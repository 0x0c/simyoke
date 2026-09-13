"""Wait on a condition rather than a clock — never a fixed sleep (prime directive 2)."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence

from bajutsu.common import assertions
from bajutsu.common.cancellation import CancelSource, RunCancelled, not_cancelled
from bajutsu.common.drivers import base
from bajutsu.common.evidence.network import TransitionSource, _no_transitions
from bajutsu.common.orchestrator.types import (
    AlertEvent,
    AlertGuardConfig,
    Clock,
    NetworkSource,
    UndeclaredInterruption,
    _no_network,
    selector_names_button,
    undeclared_interruption_note,
)
from bajutsu.common.scenario import Gone, Wait, WaitRequest

from ._alert_guard_gate import _AlertGuardGate
from ._heartbeat import _Heartbeat
from ._shared import WaitTick, _logger
from .wait_trace import WaitTrace

_POLL = 0.05
_SETTLE_POLLS = 2  # consecutive unchanged polls that count as "settled" (tree-diff fallback)
# Quiescence window for the signal-based settle path (BE-0310): once no further screen-transition
# has been reported for this long, the last transition is taken as finished. Short by design —
# `viewDidAppear` already fires *after* the appearance transition completes, so this only smooths
# over a chained transition posting more than one report in quick succession, not a whole
# animation's duration.
_TRANSITION_QUIESCENCE = 0.3
# Seconds of consecutive `ElementNotTappable` declines `_dismiss_from_tree` tolerates for one
# showing of a label before it stops attempting the tap: unlike `ElementNotFound` (the button left
# the tree, so the next poll can't re-match it) and `AmbiguousSelector` (guarded by the uniqueness
# pre-check above it), a genuinely stuck obstruction — a scrim that never lifts, an `elevation`
# false positive — has neither property, so without its own bound this decline would re-issue a real
# actuation attempt for the rest of the wait. Clock-based, for the reason `_TREE_RETAP_DELAY` records
# for itself: what is being waited out is a presentation animation measured in seconds (a UIKit sheet
# ~0.35-0.5s, an Android dialog enter ~0.25s+). A poll count cannot express that here anyway, because
# `_dismiss_from_tree` is paced by `guard.poll_interval` rather than `_POLL` (see `_observe_native`)
# and that interval is configurable per scenario, target, and flag (BE-0177). Twice the default
# `poll_interval` rather than the animation's own horizon, because the give-up is checked *before*
# the tap: at one interval the very first attempt would exhaust the budget, leaving a transient scrim
# no retry at all. That is also why the horizon is *derived* from the interval rather than fixed. A
# scenario may tune `pollInterval` upwards (the save-password one sets 5), and a fixed 2s would
# then put the second pass past the horizon before it ever ran — reinstating the zero-retry case
# this value exists to avoid. The floor keeps the animation horizon intact at short intervals.
_TREE_DISMISS_DECLINE_GIVEUP_FLOOR = 2.0


# A lane may raise the floor under a wait's ceiling: a condition wait returns the instant it is
# satisfied, so a larger ceiling never slows a fast backend — it only gives a slow environment
# (e.g. the CI x86_64 software-rendered emulator) time to draw before the step is failed. Set by
# the Android e2e lane so the shared scenarios' `timeout: 5` need not be retuned per backend.
_FLOOR_ENV = "BAJUTSU_MIN_WAIT_TIMEOUT"


# The `handleSystemAlert` step's own read of the alert's buttons, on its own wall clock (BE-0406).
# The cadence the XCUITest driver's polling loop paid before that wait moved here, so the step
# notices its target prompt no slower than it did — and deliberately independent of the guard's
# `poll_interval`, which paces a cross-process probe a scenario may widen on purpose
# (`pollInterval: 5`, to keep stacked prompts up across one probe). Coupling the two would put that
# widened gap between the step and its own prompt.
_SYSTEM_ALERT_POLL = 0.2

# What the step's own tap passes the driver: query once and tap if the button is there, else fail
# fast. The waiting is this loop's job, not the driver's.
_STEP_TAP_TIMEOUT = 0.0


# How long a post-dismiss settle may spend before its caller proceeds anyway. Its own budget, not an
# alias of `_TREE_RETAP_DELAY` above: the two happen to agree on the horizon at which any real
# dismiss animation is over, but they pace different things, and tuning the in-tree re-tap cadence
# must not silently shorten this. It is a cap, not a cost — the common case returns after two
# unchanged polls (~0.1s). Two screens spend it in full: one that never holds still, and one
# carrying no identifiers at all, which `_wait_settled`'s tree-diff branch can never call stable
# (an unlabelled build, a page with no test ids). There it degrades to a bounded delay that buys
# only the animation window, which is still the right outcome for the retry that follows.
_DISMISS_SETTLE_TIMEOUT = 1.0


def describe_wait(w: Wait) -> str:
    """A human-readable description of what a wait is blocked on, for live progress.

    Renders the condition as `key=value` — `for id='home.title'`, `until gone id='spinner'`,
    `until request GET /login`, `until settled` / `until screenChanged` — reusing the assertion
    report's `sel_str` so a pending line and an assertion detail render a selector the same way.
    (`_wait`'s timeout reason prints the raw selector dict, so it is not byte-identical to this.)
    """
    if w.for_ is not None:
        return f"for {assertions.sel_str(w.for_)}"
    if isinstance(w.until, Gone):
        return f"until gone {assertions.sel_str(w.until.gone)}"
    if isinstance(w.until, WaitRequest):
        return f"until request {assertions.request_label(w.until.request)}"
    return f"until {w.until}"  # "settled" | "screenChanged"


def _decline_giveup(poll_interval: float) -> float:
    """Seconds `_dismiss_from_tree` tolerates `ElementNotTappable` on one showing of a label.

    Twice the cadence the path is paced at, floored at the animation horizon: the bound is
    checked before the tap, so anything under two intervals spends itself on the first attempt.
    """
    return max(_TREE_DISMISS_DECLINE_GIVEUP_FLOOR, 2 * poll_interval)


def _tree_signature(elements: list[base.Element]) -> tuple[tuple[str | None, str | None], ...]:
    """A cheap identity for one poll's screen, used to tell a tap that did nothing from one that did.

    `_dismiss_from_tree` matches identifier-less buttons, and `shows_app_ui`'s docstring records that
    a whole app can legitimately have none (the label/coordinate-driven `-noax` shape). So a label
    still matching after a tap is *not* by itself evidence the prompt is still up — an app-authored
    button carrying the same label, revealed once the sheet closed, matches just as well, and
    re-tapping that navigates the app under test and fails the step for an unrelated reason.

    A tap the app never acted on leaves the screen byte-identical; a tap that dismissed a sheet does
    not. Comparing this signature is what makes "the tap did not land" a measured claim rather than
    an assumption. Labels and identifiers rather than frames, so an animation settling a few pixels
    does not read as a changed screen.
    """
    return tuple((el["label"], el["identifier"]) for el in elements)


def _timeout_floor() -> float:
    raw = os.environ.get(_FLOOR_ENV)
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        raise ValueError(f"{_FLOOR_ENV}={raw!r} is not a valid float") from None


def _effective_timeout(w: Wait) -> float:
    return max(w.timeout, _timeout_floor())


def _exists(elements: list[base.Element], sel: base.Selector) -> bool:
    return len(base.find_all(elements, sel)) >= 1


def _with_block_note(reason: str, gate: _AlertGuardGate | None) -> str:
    """The wait's timeout reason, plus what the guard last saw blocking the screen (BE-0402).

    Read only at the moment a timeout is reported, and only from the gate's latest observation, so a
    prompt that appeared and resolved mid-wait leaves nothing behind. Without it a `wait` stuck
    behind a prompt no rule identifies reports only the element that never appeared.
    """
    if gate is None or not gate.blocked_note:
        return reason
    return f"{reason} \u2014 {gate.blocked_note}"


def _adaptive_sleep(clock: Clock, before: float) -> None:
    """Sleep only the remainder of _POLL after subtracting time already spent (e.g. in query).

    When `driver.query()` is backed by a subprocess (a device-tree dump ≈ 100-300ms or more), the call
    itself already provides sufficient delay and an additional fixed sleep is wasteful."""
    elapsed = clock.now() - before
    remaining = _POLL - elapsed
    if remaining > 0:
        clock.sleep(remaining)


def wait_for_system_alert(
    driver: base.Driver,
    sel: base.Selector,
    timeout: float,
    clock: Clock,
    *,
    alert_guard: AlertGuardConfig | None = None,
    alerts: list[AlertEvent] | None = None,
    cancelled: CancelSource = not_cancelled,
) -> tuple[bool, str]:
    """Wait for the system alert `sel` names and tap it, clearing declared interruptions meanwhile.

    The `handleSystemAlert` step's wait, which BE-0406 moved out of the XCUITest driver so the
    reactive guard can act while it runs. Before, the driver polled SpringBoard to its own deadline
    and nothing else could intervene inside that call, so a prompt the scenario had already declared
    — iOS's save-password alert, raised into the app's own process and invisible to this query —
    held the screen for the step's whole timeout, and the step failed naming the permission prompt
    it never saw rather than the alert that was actually up.

    Each poll reads the step's own target first and only then hands the tree to the guard. That
    order is what keeps the two from answering the same prompt when a scenario holds a `rules` entry
    for it with the opposite choice; the gate closes the rest of that window itself, since it is
    given `sel` and declines an alert `sel` names (`probe_native`'s `"reserved"`).

    Args:
        timeout: The step's own deadline. Zero reads once and gives up, so a caller that already
            knows a prompt is up pays no poll.
        alert_guard: The scenario's reactive guard, when the run has one. Without it this is a plain
            condition wait — the shape `record`'s replay gets.
        alerts: The step's outcome list, which the guard appends each prompt it dismissed to.
        cancelled: Consulted once per poll, right where the deadline is, so a cancelled run is
            noticed within one tick instead of actuating the device for the rest of the timeout
            (BE-0370). It raises rather than returning a verdict: the prompt neither appeared nor
            timed out, and the scenario is over either way.

    Raises:
        base.UnsupportedAction: the backend does not advertise `HANDLE_SYSTEM_ALERT`.
        RunCancelled: the run was cancelled while this step was waiting.

    Returns:
        `(ok, reason)`. The reason names what the step actually saw: no alert at all, an alert whose
        buttons `sel` did not name, one offering `sel`'s label twice, or — through the guard's own
        note — a prompt that held the screen and nothing could clear.
    """
    if base.Capability.HANDLE_SYSTEM_ALERT not in driver.capabilities():
        # Preflight rejects the step before any device work; this is the mid-run backstop, and it
        # has to stay immediate. Polling a backend that can never see a system alert would spend
        # the step's whole timeout to arrive at the same refusal. The driver's own message names
        # the backend, so ask it first and only fall back to a generic refusal — a driver that
        # returns here would otherwise pass a step nothing answered.
        driver.handle_system_alert(sel, _STEP_TAP_TIMEOUT)
        raise base.UnsupportedAction(
            f"handleSystemAlert needs a backend advertising HANDLE_SYSTEM_ALERT: {sel!r}"
        )
    deadline = clock.now() + timeout
    gate = (
        _AlertGuardGate(
            driver=driver,
            clock=clock,
            guard=alert_guard,
            alerts=alerts if alerts is not None else [],
            reserved=sel,
        )
        if alert_guard is not None
        else None
    )
    last_read: float | None = None
    # The buttons the step's *latest* read saw, for the timeout to name — reassigned on every read,
    # including an empty one. Keeping the last non-empty list instead would report an alert the
    # guard has since cleared as still on screen, turning "no alert appeared at all" into "an alert
    # was up that your selector missed": the very distinction this reason exists to draw.
    seen: list[str] = []
    # Whether the latest read found `sel`'s label on the alert more than once. Reported separately
    # because it is a different fault from a selector that matched nothing — the author named a
    # button the alert really offers, twice — and because the wait polls on rather than failing at
    # once, so nothing else would ever say so (determinism first: never tap whichever matched first).
    ambiguous = False
    while True:
        t0 = clock.now()
        if last_read is None or t0 - last_read >= _SYSTEM_ALERT_POLL:
            last_read = t0
            seen = driver.system_alert_labels()
            ambiguous = False
            # Decided from the labels already in hand: `handle_system_alert` issues its own
            # cross-process query, so tapping speculatively would double this step's query rate for
            # the whole time an interruption the step is not waiting for holds the screen.
            if selector_names_button(sel, seen):
                try:
                    driver.handle_system_alert(sel, _STEP_TAP_TIMEOUT)
                except base.ElementNotFound:
                    # The alert closed itself between this read and the tap. Not the step's verdict:
                    # keep polling to the deadline, so a benign race costs one poll rather than the
                    # step (`probe_native`'s own treatment of the same window).
                    pass
                except base.AmbiguousSelector:
                    # The other half of that race: the alert is still up and now offers `sel`'s
                    # label twice. Declines rather than tapping whichever matched first, and polls
                    # on — a duplicate that a redraw resolves costs one poll, and one that does not
                    # is named in the timeout below.
                    ambiguous = True
                else:
                    return True, ""
            elif isinstance(driver, base.InterruptionPolicyTarget):
                # `sel`'s alert is not the one this read just saw — but a governing policy's
                # monitor may have already answered it between two polls, or even before this
                # wait's first one: XCUITest can resolve an out-of-process alert on any query,
                # including `gate.observe` below and this step's own pre-act reads, and
                # `_reserve_declared_alert` pushes a rule for this exact selector so the monitor
                # recognizes it (BE-0406 Unit 2b review finding). Without this check, `seen` empty
                # here reads as "no alert ever appeared" when the alert was in fact answered
                # correctly, just not by this loop's own tap — the very silence Unit 2b exists to
                # end, reintroduced by the mechanism meant to close it. A backend without the
                # opt-in, or one nothing has pushed a policy to, drains nothing and falls through
                # unchanged.
                drained = driver.drain_interruptions()
                matched = [label for label in drained.tapped if selector_names_button(sel, [label])]
                if alerts is not None:
                    # A tapped label that is not `sel`'s own is some other declared rule's alert,
                    # resolved by the monitor while this step happened to be polling — draining it
                    # here consumes it from the store, so it must be recorded now or the report
                    # loses it entirely (the end-of-step drain will find nothing left to read). A
                    # notification banner swiped away during the same poll is drained here too, for
                    # the identical reason: it can never be `sel`'s own alert (BE-0416), so it always
                    # belongs in `unrelated`'s company rather than the matched-alert branch below.
                    unrelated = [label for label in drained.tapped if label not in matched]
                    alerts.extend(AlertEvent(label=label) for label in unrelated)
                    alerts.extend(
                        AlertEvent(label=text, kind="notificationBanner")
                        for text in drained.banners
                    )
                if matched:
                    if alerts is not None:
                        alerts.append(AlertEvent(label=matched[0]))
                    return True, ""
                if drained.declined:
                    # Some other alert interrupted a query during this same wait and nothing
                    # declared it — not this step's own prompt, but still a fact the run must not
                    # swallow (BE-0406 Unit 2b): the step fails naming it, the same way any other
                    # drain site does.
                    return False, undeclared_interruption_note(
                        [UndeclaredInterruption(buttons=buttons) for buttons in drained.declined]
                    )
        if gate is not None:
            gate.observe(driver.query())
        if cancelled():
            raise RunCancelled
        if clock.now() >= deadline:
            return False, _with_block_note(
                _alert_timeout_reason(sel, timeout, seen, ambiguous), gate
            )
        _adaptive_sleep(clock, t0)


def _alert_timeout_reason(
    sel: base.Selector, timeout: float, seen: Sequence[str], ambiguous: bool
) -> str:
    """What the `handleSystemAlert` step saw, for the timeout it is about to report (BE-0406).

    `seen` is the step's latest read of the alert's buttons, so an empty one means no alert was up
    at the deadline rather than that none ever was — the guard's own note, appended by the caller,
    is what names a prompt that came and went or one nothing could clear.
    """
    if not seen:
        return f"no system alert appeared within {timeout}s: {sel!r}"
    offered = ", ".join(seen)
    if ambiguous:
        return (
            f"system alert button {sel!r} is ambiguous and stayed ambiguous for {timeout}s "
            f"(the alert on screen offered: {offered}) — add index to pick one"
        )
    return (
        f"no system alert button matching {sel!r} appeared within {timeout}s "
        f"(the alert on screen offered: {offered})"
    )


# Genuinely long: the wait state machine on the deterministic run path. Splitting it carries real
# behavioral risk, so it belongs to BE-0386's ratchet steps rather than the PR that sets the
# ceiling.
def _wait(  # noqa: C901, PLR0912
    driver: base.Driver,
    w: Wait,
    clock: Clock,
    network: NetworkSource = _no_network,
    *,
    trace: WaitTrace | None = None,
    alert_guard: AlertGuardConfig | None = None,
    alerts: list[AlertEvent] | None = None,
    on_tick: WaitTick | None = None,
    transitions: TransitionSource = _no_transitions,
    on_interrupt_poll: Callable[[list[base.Element]], bool] | None = None,
    cancelled: CancelSource = not_cancelled,
) -> tuple[bool, str, list[base.Element] | None]:
    """Condition wait. Polls query() (or the observed network) until satisfied instead
    of a fixed sleep.

    When `trace` is given (a `for` wait only), each poll is recorded into it so a timeout can be
    diagnosed from artifacts (BE-0231 Unit 1); it never changes the wait's outcome.

    When `alert_guard` is given, the branches a system alert can *stall* — `for`, `settled`, and
    `screenChanged` (where a collapsed tree keeps the condition unmet and would otherwise burn the
    whole timeout) — drive the guard mid-wait, then resume polling against the *same* `deadline`. On
    an iOS backend the guard queries SpringBoard natively on its own interval (BE-0315, reusing
    BE-0316's primitive); elsewhere it watches the already-fetched tree for the collapsed-tree
    signature of a blocking prompt (BE-0269). The condition check still decides pass/fail; the guard
    only accelerates recovery, and dismissed alerts are appended to `alerts` (the step's outcome
    list) for the report. A block it cannot clear is not acted on at all (BE-0402) — it is appended
    to the timeout this returns, so the failure names the alert instead of only the element that
    never appeared. `gone` is guarded
    too. It was not, on the reasoning that a collapsed tree already satisfies "gone" and returns at
    once — true of a SpringBoard prompt, which covers the app and empties its tree, but only of
    those. A prompt drawn *inside* the app's own process collapses nothing and instead **adds** its
    buttons to the tree, so a `gone` wait on one of them sits unsatisfied for its whole timeout with
    nothing to clear it. iOS's "Save Password" alert is exactly that shape, which is how the gap
    surfaced. `request` polls the network, not the screen, so it is still not guarded.

    When `on_interrupt_poll` is given, it is called with each poll's already-fetched tree — after
    the wait's own condition is checked, so it fires only while the wait is still blocked — so a
    scenario's `interrupts` handlers can clear an interstitial screen mid-wait (BE-0314). Like the
    alert guard, it rides on the poll the wait already performs (zero extra query) and resumes
    against the *same* `deadline`; the `gone`/`request` branches are not hooked (a collapsed tree
    already satisfies `gone`, and `request` polls the network, not the screen). A `True` return ends
    the wait immediately (skipping the `deadline` check) rather than burning the rest of the
    timeout: an interrupt's own recovery `steps` can fail, and that failure is already decided by
    the first poll that hits it, so polling on would only turn a fast, loud failure into a slow one.
    The caller (the run loop) knows the real reason and overrides the placeholder this returns.

    When `on_tick` is given, a throttled "still waiting …" line is emitted while the wait is pending:
    once on entry — so even an instantly-satisfied wait surfaces its condition — then every
    `_TICK_INTERVAL` until it resolves. It is display only and never affects the outcome.

    `transitions` (BE-0310) is the `settled` branch's read-only screen-transition signal; the
    default reports none, so `settled` keeps its unchanged tree-diff behavior unless a caller passes
    a real source.

    `cancelled` (BE-0370) is consulted once per poll, right where the deadline is, so a wait blocked
    on a condition notices a cancelled run within one polling tick instead of burning the rest of
    its timeout. It raises `RunCancelled` rather than returning a verdict: the condition is neither
    satisfied nor timed out, and the scenario is over either way. The condition check comes first, so
    a wait already satisfied on that poll still passes.

    Returns `(ok, reason, tree)` where `tree` is the last screen the wait queried — the settled
    device state, since nothing actuates in a wait. The caller reuses it as the step's `after`
    snapshot instead of re-querying (BE-0259). It is `None` for the `request` variant, which polls
    the observed network rather than the tree, so there is no screen read to hand back.
    """
    timeout = _effective_timeout(w)
    start = clock.now()
    deadline = start + timeout
    # Give the gate a real list to record into even when the caller passed none (e.g. a direct
    # _wait() unit test), so the record-the-event path has no None branch. `is not None`, not
    # `or []`: an empty list the caller *did* pass is falsy but must still be the one appended to.
    gate = (
        _AlertGuardGate(
            driver=driver,
            clock=clock,
            guard=alert_guard,
            alerts=alerts if alerts is not None else [],
        )
        if alert_guard is not None
        else None
    )
    hb = _Heartbeat(on_tick, deadline) if on_tick is not None else None
    if hb is not None:
        # Fire once up front so the awaited condition is shown even for a wait that resolves on its
        # first poll (the common fast case), before any per-loop tick has had a chance to run.
        hb.tick(start)
    if w.for_ is not None:
        target = w.for_.as_selector()
        if trace is not None:
            trace.target = str(target)
            trace.timeout_s = timeout
        while True:
            t0 = clock.now()
            elements = driver.query()
            if trace is not None:
                trace.polls += 1
                if elements and trace.first_nonempty_s is None:
                    trace.first_nonempty_s = t0 - start
            if _exists(elements, target):
                return True, "", elements
            if gate is not None:
                gate.observe(elements)
            if on_interrupt_poll is not None and on_interrupt_poll(elements):
                return False, "interrupt recovery failed", elements
            if cancelled():
                raise RunCancelled
            if clock.now() >= deadline:
                if trace is not None:
                    trace.elements_at_timeout = len(elements)
                return (
                    False,
                    _with_block_note(f"wait timeout: for {target} ({timeout}s)", gate),
                    elements,
                )
            if hb is not None:
                hb.tick(clock.now())
            _adaptive_sleep(clock, t0)
    if isinstance(w.until, Gone):
        target = w.until.gone.as_selector()
        while True:
            t0 = clock.now()
            elements = driver.query()
            if not _exists(elements, target):
                return True, "", elements
            # Guarded like `for` (see the docstring): a prompt the app draws in its own process does
            # not collapse the tree, it adds to it, so "gone" stays false until something clears the
            # prompt — and only the guard will. Observed after the condition, so a wait already
            # satisfied never actuates.
            if gate is not None:
                gate.observe(elements)
            if cancelled():
                raise RunCancelled
            if clock.now() >= deadline:
                return (
                    False,
                    _with_block_note(f"wait timeout: gone {target} ({timeout}s)", gate),
                    elements,
                )
            if hb is not None:
                hb.tick(clock.now())
            _adaptive_sleep(clock, t0)
    if isinstance(w.until, WaitRequest):
        req = w.until.request
        need = req.count if req.count is not None else 1
        while True:
            t0 = clock.now()
            if assertions.count_matching(network(), req) >= need:
                return True, "", None
            if cancelled():
                raise RunCancelled
            if clock.now() >= deadline:
                label = assertions.request_label(req)
                return False, f"wait timeout: request {label} ({timeout}s)", None
            if hb is not None:
                hb.tick(clock.now())
            _adaptive_sleep(clock, t0)
    if w.until == "settled":
        return _wait_settled(
            driver, deadline, clock, gate, hb, transitions, on_interrupt_poll, start, cancelled
        )
    # until == "screenChanged"
    before = driver.query()
    if gate is not None:
        gate.observe(before)
    while True:
        t0 = clock.now()
        current = driver.query()
        if current != before:
            return True, "", current
        if gate is not None:
            gate.observe(current)
        if on_interrupt_poll is not None and on_interrupt_poll(current):
            return False, "interrupt recovery failed", current
        if cancelled():
            raise RunCancelled
        if clock.now() >= deadline:
            return (
                False,
                _with_block_note(f"wait timeout: screenChanged ({timeout}s)", gate),
                current,
            )
        if hb is not None:
            hb.tick(clock.now())
        _adaptive_sleep(clock, t0)


def _wait_settled(
    driver: base.Driver,
    deadline: float,
    clock: Clock,
    gate: _AlertGuardGate | None = None,
    hb: _Heartbeat | None = None,
    transitions: TransitionSource = _no_transitions,
    on_interrupt_poll: Callable[[list[base.Element]], bool] | None = None,
    start: float = 0.0,
    cancelled: CancelSource = not_cancelled,
) -> tuple[bool, str, list[base.Element]]:
    """Wait until a non-empty screen stops changing (transition/animation finished).

    When `transitions` has reported a screen-transition event *since this wait began*
    (`events[-1][1] >= start`), settled is a positive signal — no further transition reported for
    `_TRANSITION_QUIESCENCE` — rather than an inference from tree reads; see
    `_wait_settled_by_signal`. This is re-checked on every poll, not only at entry: `viewDidAppear`'s
    report is POSTed fire-and-forget *after* the appearance animation, so for the canonical
    tap → navigate → `settled` step it lands a few hundred ms into the wait, not before it — the wait
    switches onto the signal path the instant that report arrives, mirroring the readiness gate's
    per-tick re-read. The since-start guard mirrors the one the readiness gate applies to the same
    signal (BE-0310): a transition left over from a *prior* step (the collector is scenario-scoped,
    not per-wait) predates `start`, so it is ignored rather than settling this wait instantly and
    missing the current step's own transition. Until a since-start transition is observed (the app
    doesn't link the observer, or its report is still in flight), this runs the original tree-diff
    behavior, which waits the animation out: a blank/collapsed tree (e.g. a screen mid-render, or one
    covered by a system alert) is never treated as settled, and settled is two consecutive unchanged
    polls with an identified element. Both paths are best-effort: timing out
    just proceeds with the current screen — a settle is a stabilization hint, not a correctness
    assertion, so it never fails the step. When `gate` is given, a screen that stays collapsed (a
    system alert) is cleared mid-settle rather than burning the whole timeout (BE-0269). When `hb` is
    given, it emits the throttled "still waiting …" progress line while settling. Returns the last
    queried tree so the caller can reuse it as the step's `after` snapshot (BE-0259).

    A `True` from `on_interrupt_poll` ends the settle immediately (BE-0314) — a failed interrupt
    recovery is a decided outcome the caller (the run loop) fails the step on, so polling toward
    settled would only delay a failure that best-effort settling would otherwise mask. `cancelled`
    raises `RunCancelled` out of the settle the same way it does out of every other wait branch
    (BE-0370): settling is best-effort, but a cancelled run has nothing left to settle *for*.
    """
    previous = driver.query()
    if gate is not None:
        gate.observe(previous)
    stable = 0
    while stable < _SETTLE_POLLS:
        # A qualifying transition can land mid-wait, not only before it: `viewDidAppear`'s
        # fire-and-forget report arrives *after* the appearance animation, so for the canonical
        # tap → navigate → `settled` step it lands a few hundred ms into this wait rather than at
        # entry. Re-consult every poll — like the readiness gate — and switch to the signal path the
        # instant a since-start transition appears; until then the tree-diff loop below waits the
        # animation out. A left-over transition from a prior step predates `start`, so it is ignored.
        events = transitions()
        if events and events[-1][1] >= start:
            return _wait_settled_by_signal(
                driver,
                deadline,
                clock,
                gate,
                hb,
                transitions,
                events[-1][1],
                on_interrupt_poll,
                cancelled,
            )
        if clock.now() >= deadline:
            return True, "", previous
        # After the deadline return, not before it: a settle never fails a step, so that return is a
        # *pass*, and checking first would turn a settle that had already finished into a cancelled
        # failure — the retroactive verdict change every other branch's ordering rules out.
        if cancelled():
            raise RunCancelled
        t0 = clock.now()
        current = driver.query()
        if gate is not None:
            gate.observe(current)
        if on_interrupt_poll is not None and on_interrupt_poll(current):
            return False, "interrupt recovery failed", current
        if current == previous and any(el["identifier"] for el in current):
            stable += 1
        else:
            stable, previous = 0, current
        if hb is not None:
            hb.tick(clock.now())
        _adaptive_sleep(clock, t0)
    return True, "", previous


def _wait_settled_by_signal(
    driver: base.Driver,
    deadline: float,
    clock: Clock,
    gate: _AlertGuardGate | None,
    hb: _Heartbeat | None,
    transitions: TransitionSource,
    last: float,
    on_interrupt_poll: Callable[[list[base.Element]], bool] | None = None,
    cancelled: CancelSource = not_cancelled,
) -> tuple[bool, str, list[base.Element]]:
    """The signal-based settle path (BE-0310): quiescence since the last observed transition.

    "No further screen-change transition reported for `_TRANSITION_QUIESCENCE`" is a positive "the
    last transition has finished and no new one started," not "two reads happened to match" — the
    window restarts each time a fresh transition is observed. `last` is the most recent transition's
    receive time, already fetched by the caller (`_wait_settled`) to confirm at least one had been
    reported. A collector only ever appends in receive order, so it stays non-empty and its final
    element is always the newest — later reads take `transitions()[-1][1]` rather than scanning for
    a max.

    A `True` from `on_interrupt_poll` ends the settle immediately (BE-0314), same as the tree-diff
    fallback above — the signal path is still a settle loop over `driver.query()`, so a scenario's
    `interrupts` handlers apply here too, not only when no transition signal is available.

    With neither a `gate` nor an `on_interrupt_poll` registered, nothing consumes a mid-window read
    (BE-0407 Unit 5): the window is a positive "no new transition," decided from `transitions()`
    alone, so this skips the device round trip on every tick and queries exactly once, right when a
    caller finally needs the settled tree — at quiescence, or at the deadline.
    """
    # Diagnostic only (BE-0310 Unit 5): confirms the signal path actually decided settled on a real
    # device, so on-device verification needs no extra instrumentation to observe it.
    _logger.debug(
        "settled via the screen-transition signal (quiescence=%ss)", _TRANSITION_QUIESCENCE
    )
    watched = gate is not None or on_interrupt_poll is not None
    current: list[base.Element] | None = None

    def observed() -> list[base.Element]:
        """One poll: read the tree and let the alert gate see it."""
        tree = driver.query()
        if gate is not None:
            gate.observe(tree)
        return tree

    def settled_tree() -> list[base.Element]:
        return current if current is not None else driver.query()

    if watched:
        current = observed()
    while clock.now() - last < _TRANSITION_QUIESCENCE:
        if clock.now() >= deadline:
            return True, "", settled_tree()
        # Below the deadline return for the same reason as the tree-diff path above: that return is a
        # pass, and a settle already finished must not become a cancelled failure.
        if cancelled():
            raise RunCancelled
        if current is not None and on_interrupt_poll is not None and on_interrupt_poll(current):
            return False, "interrupt recovery failed", current
        t0 = clock.now()
        last = transitions()[-1][1]
        if watched:
            current = observed()
        if hb is not None:
            hb.tick(clock.now())
        _adaptive_sleep(clock, t0)
    return True, "", settled_tree()


def settle_after_alert_dismiss(
    driver: base.Driver,
    clock: Clock,
    *,
    transitions: TransitionSource = _no_transitions,
    cancelled: CancelSource = not_cancelled,
) -> None:
    """Let the screen the guard just uncovered stop moving before the caller acts on it.

    The alert guard's two one-shot call sites — the end-of-step dismiss and the `expect` retry —
    re-run the step body (or re-evaluate the assertions) the moment a dismissal comes back, and a
    prompt that has been *tapped* is not yet a prompt that is *gone*: iOS is still animating the
    sheet away, and the screen it covered is still rendering. The retry then reads a tree in motion
    and can fail for that reason alone, which reads as the step failing on its own merits.

    Best-effort throughout, so it never turns into a verdict of its own: it is a condition wait on
    the tree holding still (never a fixed sleep), and reaching `_DISMISS_SETTLE_TIMEOUT` simply
    returns, leaving the retry to decide. Cancellation still propagates, as it does out of every
    other wait (BE-0370) — a cancelled run has nothing left to settle for.
    """
    start = clock.now()
    _wait_settled(
        driver,
        start + _DISMISS_SETTLE_TIMEOUT,
        clock,
        transitions=transitions,
        start=start,
        cancelled=cancelled,
    )
