"""The `handleSystemAlert` step's wait, moved out of the XCUITest driver (BE-0406 Unit 1).

The driver now queries SpringBoard once and taps; the waiting is a condition wait the orchestrator
owns, so the reactive guard can clear a declared interruption *while* the step waits instead of only
after it has already spent its timeout. Exercised against `FakeDriver`, which advertises
`HANDLE_SYSTEM_ALERT` and can be seeded with alert buttons, so nothing here needs a Simulator.
"""

from __future__ import annotations

import pytest
from conftest import guard_rule

from bajutsu.common.cancellation import RunCancelled
from bajutsu.common.drivers import base
from bajutsu.common.drivers.fake import FakeDriver
from bajutsu.common.orchestrator import AlertEvent, AlertGuardConfig, _do_action
from bajutsu.common.orchestrator.types import ResolvedAlertRule
from bajutsu.common.orchestrator.waits import wait_for_system_alert
from bajutsu.common.scenario import Step, load_scenarios


class _LogicalClock:
    """A clock whose only motion is `sleep` advancing logical time (no real waiting)."""

    def __init__(self) -> None:
        self._t = 0.0

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += seconds


def _button(label: str, identifier: str | None = None) -> base.Element:
    return {
        "identifier": identifier,
        "label": label,
        "traits": ["button"],
        "value": None,
        "frame": (0, 0, 10, 10),
        "nativeZ": None,
    }


def _notification_rule() -> ResolvedAlertRule:
    """The notification prompt's own rule, denying it — `_resolve_rules`' English output."""
    return ResolvedAlertRule(
        identifying_labels=frozenset({"Allow", "Don’t Allow"}), tap_label="Don’t Allow"
    )


def _clear_the_sheet(driver: FakeDriver, kind: str, arg: object) -> None:
    """Script iOS's save-password sheet: the guard's tap clears it, and only then is the step's own
    prompt raised — the stacking the motivating case is built from."""
    if kind == "tap" and arg == {"label": "Not Now", "traits": ["button"]}:
        driver.screen = []
        driver.system_alert_buttons = [_button("Allow"), _button("Don’t Allow")]


class _Incapable(FakeDriver):
    """A backend that does not advertise HANDLE_SYSTEM_ALERT, shaped like the real ones.

    Every such driver (web, Android, the live grid) raises `UnsupportedAction` from
    `handle_system_alert` as the mid-run backstop and reports no alert buttons at all.
    """

    def capabilities(self) -> set[str]:
        return super().capabilities() - {base.Capability.HANDLE_SYSTEM_ALERT}

    def handle_system_alert(self, sel: base.Selector, timeout: float) -> None:
        raise base.UnsupportedAction("handleSystemAlert is iOS-only")


# --- the wait itself --------------------------------------------------------------------------


def test_the_step_waits_for_a_prompt_that_appears_on_a_later_poll() -> None:
    # The wait the driver used to own. Nothing is up on the first read; the prompt lands part way
    # through the step's timeout and the step taps it, rather than failing on that first empty read.
    driver = FakeDriver([])
    clock = _LogicalClock()

    original = driver.system_alert_labels

    def appear_after_half_a_second() -> list[str]:
        if clock.now() >= 0.5:
            driver.system_alert_buttons = [_button("Allow"), _button("Don’t Allow")]
        return original()

    driver.system_alert_labels = appear_after_half_a_second  # type: ignore[method-assign]

    ok, reason = wait_for_system_alert(driver, {"label": "Allow"}, 5.0, clock)

    assert ok, reason
    assert ("handle_system_alert", ({"label": "Allow"}, 0.0)) in driver.actions


def test_a_zero_timeout_reads_once_and_gives_up() -> None:
    # The shape the guard's own tap already used: the caller knows an alert is up, so no poll is
    # owed. A step that names a prompt nothing raised must not silently become a zero-length wait.
    driver = FakeDriver([])
    ok, reason = wait_for_system_alert(driver, {"label": "Allow"}, 0.0, _LogicalClock())

    assert not ok
    assert "no system alert appeared within 0.0s" in reason


def test_the_step_reads_the_alert_at_its_own_cadence_not_the_guards() -> None:
    # The two rates are decoupled (BE-0406): a scenario widening `pollInterval` so stacked prompts
    # stay up across one cross-process probe must not also slow how fast the step sees its own
    # prompt. With a five-second guard interval, a prompt landing at 0.5s is still answered.
    driver = FakeDriver([])
    clock = _LogicalClock()
    original = driver.system_alert_labels

    def appear_after_half_a_second() -> list[str]:
        if clock.now() >= 0.5:
            driver.system_alert_buttons = [_button("Allow"), _button("Don’t Allow")]
        return original()

    driver.system_alert_labels = appear_after_half_a_second  # type: ignore[method-assign]
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")], poll_interval=5.0)

    ok, reason = wait_for_system_alert(
        driver, {"label": "Allow"}, 30.0, clock, alert_guard=guard, alerts=[]
    )

    assert ok, reason
    assert clock.now() < 5.0  # answered well inside one guard probe interval


def test_a_button_that_vanishes_between_the_read_and_the_tap_costs_one_poll_not_the_step() -> None:
    # The time-of-check/time-of-use race `probe_native` already treats as benign: the alert closed
    # itself between this poll's read and the tap. The step keeps polling, and the prompt it was
    # placed for is answered on a later poll.
    driver = FakeDriver([])
    driver.system_alert_buttons = [_button("Allow")]
    clock = _LogicalClock()
    raised: list[int] = []

    real_handle = driver.handle_system_alert

    def vanish_once(sel: base.Selector, timeout: float) -> None:
        if not raised:
            raised.append(1)
            raise base.ElementNotFound("gone")
        real_handle(sel, timeout)

    driver.handle_system_alert = vanish_once  # type: ignore[method-assign]

    ok, reason = wait_for_system_alert(driver, {"label": "Allow"}, 5.0, clock)

    assert ok, reason
    assert raised == [1]


def test_an_ambiguous_button_declines_rather_than_tapping_the_first_match() -> None:
    # The other half of that race, and determinism first: two buttons carrying the step's label
    # resolve to nothing, so the step times out naming what it saw rather than tapping either.
    driver = FakeDriver([])
    # Distinct frames, so the two stay two: identical elements collapse into one candidate, which
    # would resolve uniquely and defeat the point of the test.
    left, right = _button("Allow"), _button("Allow")
    right["frame"] = (100, 0, 10, 10)
    driver.system_alert_buttons = [left, right]

    ok, reason = wait_for_system_alert(driver, {"label": "Allow"}, 0.5, _LogicalClock())

    assert not ok
    # A duplicate a redraw would resolve costs one poll, so the wait polls on; one still there at
    # the deadline is a scenario-authoring fault, and the reason has to say which of the two it is
    # rather than reading as a selector that matched nothing.
    assert "is ambiguous and stayed ambiguous" in reason
    assert "Allow, Allow" in reason
    assert not [a for a in driver.actions if a[0] == "handle_system_alert"]


def test_an_incapable_backend_refuses_at_once_instead_of_spending_the_timeout() -> None:
    # Preflight already rejects the step on such a backend; this is the mid-run backstop, and it has
    # to stay immediate — polling a backend that can never see a system alert would burn the step's
    # whole timeout to arrive at the same refusal.
    clock = _LogicalClock()
    with pytest.raises(base.UnsupportedAction):
        wait_for_system_alert(_Incapable([]), {"label": "Allow"}, 30.0, clock)
    assert clock.now() == 0.0


# --- what the timeout says --------------------------------------------------------------------


def test_the_timeout_names_the_alert_whose_buttons_the_selector_did_not_match() -> None:
    # The failure this unit exists to make readable. Before, every timeout read "no system alert
    # appeared", whether nothing was up or a different prompt held the screen the whole time.
    driver = FakeDriver([])
    driver.system_alert_buttons = [_button("Save Password"), _button("Not Now")]

    ok, reason = wait_for_system_alert(driver, {"label": "Allow"}, 0.5, _LogicalClock())

    assert not ok
    assert "no system alert button matching {'label': 'Allow'}" in reason
    assert "the alert on screen offered: Save Password, Not Now" in reason


def test_the_timeout_carries_the_guards_note_for_a_prompt_nothing_could_clear() -> None:
    # An undeclared alert held the screen for the wait. The step's own read sees it too, and the
    # guard's note names it as one nothing will answer — the third case the reason distinguishes.
    driver = FakeDriver([])
    driver.system_alert_buttons = [_button("Trust This Computer?"), _button("Trust")]
    guard = AlertGuardConfig(rules=[_notification_rule()], poll_interval=0.1)

    ok, reason = wait_for_system_alert(
        driver, {"label": "Allow"}, 0.5, _LogicalClock(), alert_guard=guard, alerts=[]
    )

    assert not ok
    assert "an unhandled system alert is blocking the screen" in reason
    assert "Trust This Computer?" in reason


def test_an_alert_the_guard_cleared_is_not_reported_as_still_on_screen() -> None:
    # The reason states the step's *latest* read, not the last non-empty one. A guard that cleared
    # an interruption early and a prompt the app then never raised is case 1 — no alert appeared —
    # and reporting the cleared alert's buttons would send the author after a prompt the guard
    # already answered correctly.
    def clear_the_alert(d: FakeDriver, kind: str, _arg: object) -> None:
        if kind == "handle_system_alert":
            d.system_alert_buttons = []

    driver = FakeDriver([], react=clear_the_alert)
    driver.system_alert_buttons = [_button("Allow"), _button("Don’t Allow")]
    guard = AlertGuardConfig(rules=[_notification_rule()], poll_interval=0.1)
    alerts: list[AlertEvent] = []

    ok, reason = wait_for_system_alert(
        driver, {"label": "Trust"}, 1.0, _LogicalClock(), alert_guard=guard, alerts=alerts
    )

    assert not ok
    assert alerts == [AlertEvent(label="Don’t Allow")]  # the guard did clear it
    assert "no system alert appeared within 1.0s" in reason
    assert "Don’t Allow" not in reason


def test_a_cancelled_run_ends_the_wait_within_one_poll() -> None:
    # BE-0370's invariant, which the driver's own loop never had to honor: a run stopped mid-step
    # must not keep polling and actuating the device for the rest of the step's timeout.
    driver = FakeDriver([])
    clock = _LogicalClock()

    with pytest.raises(RunCancelled):
        wait_for_system_alert(
            driver, {"label": "Allow"}, 30.0, clock, cancelled=lambda: clock.now() >= 0.5
        )

    assert clock.now() < 1.0


# --- the interruption monitor may already have answered this step's own reservation ------------


def test_the_wait_recognizes_an_alert_the_monitor_already_tapped() -> None:
    # `_reserve_declared_alert` (BE-0406 Unit 2b) pushes this step's own selector to the runner's
    # interruption monitor before the wait starts, so the monitor can answer it between two of this
    # loop's own polls — or even before the first one, since `gate.observe` below issues a query on
    # every tick. `system_alert_labels` staying empty throughout means the step's own poll never
    # sees the alert; without checking the drain, that would read as "no alert ever appeared" even
    # though it was answered correctly, just not by this loop's own tap (review finding).
    driver = FakeDriver([])
    driver.interruptions_to_drain = ["Allow"]
    alerts: list[AlertEvent] = []

    ok, reason = wait_for_system_alert(
        driver, {"label": "Allow"}, 5.0, _LogicalClock(), alerts=alerts
    )

    assert ok, reason
    assert alerts == [AlertEvent(label="Allow")]
    # Nothing here calls `handle_system_alert` itself — the monitor answered it, not this loop.
    assert not [a for a in driver.actions if a[0] == "handle_system_alert"]


def test_the_wait_records_but_does_not_finish_on_a_monitor_tap_for_a_different_alert() -> None:
    # A tap the drain reports that does not name `sel`'s own label is some other declared rule's
    # alert, resolved by the monitor while this step happened to be polling for its own — not this
    # step's own success, but still a dismissal the report must not lose: this poll's own drain call
    # consumes it from the store, so the end-of-step drain will find nothing left to read.
    driver = FakeDriver([])
    driver.interruptions_to_drain = ["Not Now"]
    alerts: list[AlertEvent] = []

    ok, reason = wait_for_system_alert(
        driver, {"label": "Allow"}, 0.5, _LogicalClock(), alerts=alerts
    )

    assert not ok
    assert "no system alert appeared" in reason  # sel's own prompt never appeared
    assert alerts == [AlertEvent(label="Not Now")]
    assert not [a for a in driver.actions if a[0] == "handle_system_alert"]


def test_the_wait_records_a_banner_swiped_away_mid_wait_without_finishing_on_it() -> None:
    # This is the one drain site in the codebase that bypasses the `drain_interruptions()` helper
    # and reads `driver.drain_interruptions()` directly, so it has to fold `drained.banners` into
    # `alerts` on its own — a banner can never be `sel`'s own alert (BE-0416), so without this the
    # drain here would consume it from the store and the end-of-step drain would find nothing left.
    driver = FakeDriver([])
    driver.banners_to_drain = ["Ready for Apple Intelligence"]
    alerts: list[AlertEvent] = []

    ok, reason = wait_for_system_alert(
        driver, {"label": "Allow"}, 0.5, _LogicalClock(), alerts=alerts
    )

    assert not ok
    assert "no system alert appeared" in reason  # sel's own prompt never appeared
    assert alerts == [AlertEvent(label="Ready for Apple Intelligence", kind="notificationBanner")]
    assert not [a for a in driver.actions if a[0] == "handle_system_alert"]


def test_the_wait_fails_on_an_undeclared_interruption_the_monitor_declined_mid_wait() -> None:
    # A different alert interrupted a query during this same wait and no rule identified it — the
    # monitor declined it, and that is a fact the run must not swallow (BE-0406 Unit 2b), even
    # though it has nothing to do with the prompt this step itself is waiting for.
    driver = FakeDriver([])
    driver.interruptions_declined_to_drain = [["Save", "Not Now"]]

    ok, reason = wait_for_system_alert(driver, {"label": "Allow"}, 5.0, _LogicalClock(), alerts=[])

    assert not ok
    assert "undeclared system alert" in reason
    assert "Save" in reason
    assert "Not Now" in reason


# --- the step and the guard do not compete for the same prompt ---------------------------------


def test_the_guard_leaves_the_prompt_the_step_is_waiting_on_alone() -> None:
    # A scenario may hold a reactive rule for the very prompt the step is placed to answer, with the
    # opposite choice. Whichever party read the alert first would decide it, so the guard is given
    # the step's selector and declines an alert that selector names.
    driver = FakeDriver([])
    driver.system_alert_buttons = [_button("Allow"), _button("Don’t Allow")]
    guard = AlertGuardConfig(rules=[_notification_rule()], poll_interval=0.1)
    alerts: list[AlertEvent] = []

    ok, reason = wait_for_system_alert(
        driver, {"label": "Allow"}, 5.0, _LogicalClock(), alert_guard=guard, alerts=alerts
    )

    assert ok, reason
    # The step's own grant, and only it: the guard's `deny` rule never fired on this alert.
    taps = [arg for kind, arg in driver.actions if kind == "handle_system_alert"]
    assert taps == [({"label": "Allow"}, 0.0)]
    assert alerts == []


def test_a_reserved_alert_leaves_no_hedged_block_note_behind() -> None:
    # A reserved alert still covers the app, so the gate's collapsed-tree proxy would read the
    # screen as blocked on every poll between the guard's own probes and record its label-less
    # "something may be blocking the screen" note. On a failure that already names the alert's
    # buttons, that note contradicts the sentence it is appended to.
    driver = FakeDriver([])
    # Two identically named buttons, so the step reserves the alert but never resolves a tap on it
    # and the wait runs to its deadline with the alert up throughout.
    left, right = _button("Allow"), _button("Allow")
    right["frame"] = (100, 0, 10, 10)
    driver.system_alert_buttons = [left, right]
    guard = AlertGuardConfig(rules=[_notification_rule()], poll_interval=0.7)

    ok, reason = wait_for_system_alert(
        driver, {"label": "Allow"}, 2.0, _LogicalClock(), alert_guard=guard, alerts=[]
    )

    assert not ok
    assert "is ambiguous and stayed ambiguous" in reason
    assert "the screen appears blocked" not in reason
    assert "unhandled system alert" not in reason


def test_the_guard_clears_a_declared_interruption_while_the_step_waits() -> None:
    # The motivating case (BE-0406). iOS's save-password alert is raised into the app's own process,
    # so `springboard.alerts` never sees it and the step's own read reports nothing; it holds the
    # screen, and the permission prompt the step was placed for is never raised while it is up.
    # Before this unit, no guard could run inside the driver's wait and the step timed out naming a
    # prompt it never saw. Now the guard clears the sheet from the tree mid-wait and the prompt the
    # step is waiting for lands.
    driver = FakeDriver([_button("Not Now")], react=_clear_the_sheet)
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")], poll_interval=0.1)
    alerts: list[AlertEvent] = []

    ok, reason = wait_for_system_alert(
        driver, {"label": "Allow"}, 5.0, _LogicalClock(), alert_guard=guard, alerts=alerts
    )

    assert ok, reason
    assert alerts == [AlertEvent(label="Not Now")]
    assert ("handle_system_alert", ({"label": "Allow"}, 0.0)) in driver.actions


def test_the_end_of_step_guard_leaves_a_failed_steps_own_alert_alone() -> None:
    # A failed `handleSystemAlert` step is the one case the end-of-step guard skips outright
    # (BE-0406). `wait_for_system_alert` already drove this same guard, reserved against the step's
    # own selector, for the step's whole timeout; a second, unreserved probe here could tap the
    # step's own alert through the guard's looser fallback policy — deciding, on the step's behalf,
    # the very prompt it was placed to answer, and overwriting its specific reason with the generic
    # one a doomed retry against the now-cleared screen produces.
    from bajutsu.common.orchestrator import run_scenario

    driver = FakeDriver([])
    # "Allow" and "Allow Once" both satisfy `labelMatches: "Allow.*"`, so the step's own tap stays
    # ambiguous for its whole timeout; "Don't Allow" is one of the guard's default dismissive labels
    # and would resolve uniquely if the end-of-step guard were allowed to probe unreserved.
    driver.system_alert_buttons = [_button("Allow"), _button("Allow Once"), _button("Don't Allow")]

    result = run_scenario(
        driver,
        load_scenarios(
            "- name: t\n"
            "  steps:\n"
            "    - handleSystemAlert: { sel: { labelMatches: 'Allow.*' }, timeout: 0.3 }\n"
        )[0],
        alert_guard=AlertGuardConfig(),
    )

    assert not result.ok
    reason = result.steps[0].reason or ""
    # The step's own specific diagnosis survives — the guard never got a chance to overwrite it
    # with a doomed retry's generic "no system alert appeared".
    assert "is ambiguous and stayed ambiguous" in reason
    assert "no system alert appeared" not in reason
    # Nothing was tapped on the step's behalf, and nothing was recorded as dismissed.
    assert result.steps[0].alerts == []
    assert not [a for a in driver.actions if a[0] == "tap"]


def test_the_run_loop_hands_the_step_the_scenarios_guard() -> None:
    # The wiring, end to end: `_run_step_body` passes `alert_guard`/`alerts` for this step kind as it
    # already did for `wait`, so a run — not only a direct call — clears the interruption mid-step
    # and reports the dismissal on the step's own outcome.
    from bajutsu.common.orchestrator import run_scenario

    driver = FakeDriver([_button("Not Now")], react=_clear_the_sheet)

    result = run_scenario(
        driver,
        load_scenarios(
            "- name: t\n  steps:\n    - handleSystemAlert: { sel: { label: Allow }, timeout: 5 }\n"
        )[0],
        alert_guard=AlertGuardConfig(rules=[guard_rule("Not Now")], poll_interval=0.1),
    )

    assert result.ok, result.failure
    assert result.steps[0].alerts == [AlertEvent(label="Not Now")]


def test_the_step_invalidates_a_live_text_selection() -> None:
    # BE-0265's contract, which `_do_action` enforces for every action but `select` and `copy`. The
    # run loop runs this step's wait itself rather than through `_do_action`, so it has to keep the
    # contract: the step actuates the device, and a `copy` after it must fail for want of a
    # selection rather than copy whatever the alert's tap left behind.
    from bajutsu.common.orchestrator import run_scenario
    from bajutsu.common.scenario import load_scenarios

    driver = FakeDriver([_button("Note", identifier="note")])
    driver.system_alert_buttons = [_button("Allow"), _button("Don’t Allow")]

    result = run_scenario(
        driver,
        load_scenarios(
            "- name: t\n"
            "  steps:\n"
            "    - select: { into: { id: note } }\n"
            "    - handleSystemAlert: { sel: { label: Allow }, timeout: 0 }\n"
            "    - copy: {}\n"
        )[0],
    )

    assert not result.ok
    assert "copy requires an active selection" in (result.steps[2].reason or "")


# --- the replay path -----------------------------------------------------------------------------


def _replay_step(timeout: float) -> Step:
    return load_scenarios(
        f"- name: t\n"
        f"  steps:\n"
        f"    - handleSystemAlert: {{ sel: {{ label: Allow }}, timeout: {timeout} }}\n"
    )[0].steps[0]


def test_the_replay_path_runs_the_same_wait_without_a_guard() -> None:
    # `record`'s replay dispatches through the action registry, which the run loop bypasses. Both
    # reach the same wait, so a replayed step keeps its condition-wait semantics; only the
    # scenario's reactive guard, which replay has none of, is absent.
    driver = FakeDriver([])
    driver.system_alert_buttons = [_button("Allow"), _button("Don’t Allow")]

    _do_action(driver, _replay_step(0))

    assert ("handle_system_alert", ({"label": "Allow"}, 0.0)) in driver.actions


def test_the_replay_path_reports_the_waits_own_reason_when_no_prompt_appears() -> None:
    # The registry's handler returns nothing, so the wait's verdict has to become an exception for
    # the caller to see it. It carries the reason the wait composed, not a bare "element not found".
    driver = FakeDriver([])

    with pytest.raises(base.ElementNotFound, match=r"no system alert appeared within 0\.0s"):
        _do_action(driver, _replay_step(0))


def test_the_fake_driver_refuses_a_single_shot_call_with_no_alert_showing() -> None:
    # The fake mirrors the real backend's post-BE-0406 shape: one query, no wait. A caller that
    # reaches it with nothing on screen is told so, rather than being made to wait for a prompt.
    driver = FakeDriver([])

    with pytest.raises(base.ElementNotFound, match="no system alert is showing"):
        driver.handle_system_alert({"label": "Allow"}, 0.0)
