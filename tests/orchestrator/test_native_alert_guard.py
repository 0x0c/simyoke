"""Tests for the reactive native system-alert guard path (BE-0315).

The reactive guard clears SpringBoard prompts automatically, preferring a deterministic native path
built on BE-0316's primitives — `system_alert_labels()` (a read of BE-0316's `/systemAlert/query`)
to see the alert's buttons, then `handle_system_alert()` to tap a policy-named one — over the vision
fallback. Exercised against `FakeDriver`, which advertises `HANDLE_SYSTEM_ALERT` and can be seeded
with alert buttons, so nothing here needs a Simulator; the on-device confirmation is a separate lane.
"""

from __future__ import annotations

import logging
from typing import cast

import pytest
from conftest import guard_rule

from bajutsu.common.drivers import base
from bajutsu.common.drivers.fake import FakeDriver
from bajutsu.common.orchestrator import AlertEvent, AlertGuardConfig
from bajutsu.common.orchestrator.types import ResolvedAlertRule, match_alert_rule
from bajutsu.common.scenario import Wait


class _LogicalClock:
    """A clock whose only motion is `sleep` advancing logical time (no real waiting)."""

    def __init__(self) -> None:
        self._t = 0.0

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += seconds


class _Incapable:
    """A driver stub without the HANDLE_SYSTEM_ALERT capability (for the incapable branch)."""

    def capabilities(self) -> set[str]:
        return set()


class _NonNativeDriver(FakeDriver):
    """A web/Android-shaped backend stand-in: capable of everything `FakeDriver` is, minus
    `HANDLE_SYSTEM_ALERT` — for asserting the in-tree dismiss path stays native-only."""

    def capabilities(self) -> set[str]:
        return super().capabilities() - {base.Capability.HANDLE_SYSTEM_ALERT}


def _button(label: str) -> base.Element:
    return {
        "identifier": None,
        "label": label,
        "traits": ["button"],
        "value": None,
        "frame": (0, 0, 10, 10),
        "nativeZ": None,
    }


def _fake_with_alert(labels: list[str], react: object = None) -> FakeDriver:
    driver = FakeDriver([], react=react)  # type: ignore[arg-type]
    driver.system_alert_buttons = [_button(label) for label in labels]
    return driver


# --- match_alert_rule -------------------------------------------------------------------------------

_NOTIF_RULE = ResolvedAlertRule(
    identifying_labels=frozenset({"Allow", "Don't Allow"}), tap_label="Allow"
)
_TRACKING_RULE = ResolvedAlertRule(
    identifying_labels=frozenset({"Allow", "Ask App Not to Track"}),
    tap_label="Ask App Not to Track",
)


def test_match_alert_rule_identifies_the_prompt_by_its_full_label_pair() -> None:
    # The tracking prompt's alert carries both of its own labels, so the tracking rule matches even
    # though "Allow" alone is shared with the notifications prompt.
    assert (
        match_alert_rule([_TRACKING_RULE], ["Allow", "Ask App Not to Track"])
        == "Ask App Not to Track"
    )


def test_match_alert_rule_none_when_only_the_shared_label_is_present() -> None:
    # "Allow" alone cannot identify which of two prompts is on screen.
    assert match_alert_rule([_TRACKING_RULE], ["Allow", "Cancel"]) is None


def test_match_alert_rule_returns_the_first_matching_rule_in_order() -> None:
    assert match_alert_rule([_NOTIF_RULE, _TRACKING_RULE], ["Allow", "Don't Allow"]) == "Allow"
    assert (
        match_alert_rule([_NOTIF_RULE, _TRACKING_RULE], ["Allow", "Ask App Not to Track"])
        == "Ask App Not to Track"
    )


def test_match_alert_rule_none_when_no_rules_or_no_match() -> None:
    assert match_alert_rule([], ["Allow", "Don't Allow"]) is None
    assert match_alert_rule([_NOTIF_RULE], ["Weird Button"]) is None
    assert match_alert_rule([_NOTIF_RULE], []) is None  # nothing on screen identifies nothing


def test_match_alert_rule_requires_each_identifying_label_exactly_once() -> None:
    # Two buttons carrying the same label cannot uniquely identify the prompt, so the rule is
    # declined rather than resolved to whichever button matched first (determinism first, mirroring
    # resolve_unique).
    assert match_alert_rule([_NOTIF_RULE], ["Allow", "Allow", "Don't Allow"]) is None


def test_match_alert_rule_skips_an_ambiguous_rule_and_takes_the_next_one() -> None:
    # A duplicated label disqualifies only the rule that names it: the scan continues rather than
    # stopping at the first rule it could not resolve, so a second rule the same alert identifies
    # unambiguously still answers it.
    ambiguous = ResolvedAlertRule(identifying_labels=frozenset({"OK"}), tap_label="OK")
    unambiguous = ResolvedAlertRule(identifying_labels=frozenset({"Cancel"}), tap_label="Cancel")
    assert match_alert_rule([ambiguous, unambiguous], ["OK", "OK", "Cancel"]) == "Cancel"
    assert match_alert_rule([ambiguous], ["OK", "OK"]) is None


def test_match_alert_rule_declines_a_rule_whose_excluded_label_is_on_the_alert() -> None:
    # Two prompts can share their whole identifying pair — iOS 26.5's save sheet and its credit-card
    # sibling are both "Save"/"Not Now" — and only a third button tells them apart. `excluded_labels`
    # is how the save rule declines the card sheet: the pair is present, so without the exclusion it
    # would match and answer a prompt it was never written for (BE-0406).
    save = ResolvedAlertRule(
        identifying_labels=frozenset({"Save", "Not Now"}),
        tap_label="Not Now",
        excluded_labels=frozenset({"Never for This Card"}),
    )
    assert match_alert_rule([save], ["Save", "Not Now", "Never for This Card"]) is None
    assert match_alert_rule([save], ["Save", "Not Now"]) == "Not Now"


# --- AlertGuardConfig.probe_native ------------------------------------------------------------------


def test_probe_native_incapable_backend() -> None:
    guard = AlertGuardConfig()
    assert guard.probe_native(_Incapable()) == ("incapable", None, [])  # type: ignore[arg-type]


def test_probe_native_absent_when_no_alert() -> None:
    guard = AlertGuardConfig()
    assert guard.probe_native(FakeDriver([])) == ("absent", None, [])  # capable, no alert seeded


def test_probe_native_dismisses_a_named_button() -> None:
    guard = AlertGuardConfig(rules=[guard_rule("Allow")])
    driver = _fake_with_alert(["Don't Allow", "Allow"])
    state, event, _ = guard.probe_native(driver)
    assert state == "dismissed"
    assert event == AlertEvent(label="Allow")
    # It tapped through BE-0316's handle_system_alert with the picked label.
    assert ("handle_system_alert", ({"label": "Allow"}, 0.0)) in driver.actions


def test_probe_native_does_not_fall_back_to_dismissive_defaults() -> None:
    # BE-0406 made `rules` the whole policy, so a scenario declaring none gets no dismissal at all —
    # not even for a button as ordinary as "Don't Allow", which the removed built-in fallback used
    # to tap on generic English vocabulary alone, rather than on a rule naming the prompt.
    guard = AlertGuardConfig()  # no rules → nothing this guard may answer
    driver = _fake_with_alert(["Don't Allow", "Allow"])
    assert guard.probe_native(driver) == ("unhandled", None, ["Don't Allow", "Allow"])
    assert driver.actions == []  # and nothing was tapped on the way to that answer


def test_probe_native_taps_the_choice_of_the_rule_that_identifies_the_prompt() -> None:
    # The decision is the rule's, not the buttons' own wording: the tracking prompt is identified by
    # its full label pair and answered with the choice its rule names, though "Allow" sits beside it.
    guard = AlertGuardConfig(rules=[_TRACKING_RULE])
    driver = _fake_with_alert(["Allow", "Ask App Not to Track"])
    state, event, _ = guard.probe_native(driver)
    assert state == "dismissed"
    assert event == AlertEvent(label="Ask App Not to Track")


def test_probe_native_leaves_an_alert_no_rule_identifies_alone() -> None:
    # A configured guard is no licence over every prompt: the notifications alert is not the one this
    # scenario's rule describes, so it is reported rather than answered — neither with that rule's
    # own choice nor with a dismissive-looking button read off the alert (BE-0406).
    guard = AlertGuardConfig(rules=[_TRACKING_RULE])
    driver = _fake_with_alert(
        ["Don't Allow", "Allow"]
    )  # notifications prompt: no rule identifies it
    assert guard.probe_native(driver) == ("unhandled", None, ["Don't Allow", "Allow"])
    assert driver.actions == []


def test_probe_native_never_matches_a_rule_the_surface_cannot_reach() -> None:
    # An in-tree-only rule's identifying labels are ordinary vocabulary a real SpringBoard alert
    # could coincidentally offer — savePassword's 26.5 shape is just "Save" / "Not Now" — so this
    # surface must filter on `native` rather than trust that no SpringBoard alert ever matches.
    # Without the filter this alert would be answered through `handle_system_alert` for a prompt
    # that path can never actually reach (BE-0406 review finding).
    in_tree_only = ResolvedAlertRule(
        identifying_labels=frozenset({"Save", "Not Now"}),
        tap_label="Not Now",
        native=False,
        in_tree=True,
    )
    guard = AlertGuardConfig(rules=[in_tree_only])
    driver = _fake_with_alert(["Save", "Not Now"])
    assert guard.probe_native(driver) == ("unhandled", None, ["Save", "Not Now"])
    assert driver.actions == []


def test_probe_native_unhandled_when_no_candidate_resolves() -> None:
    # The buttons come back with the state (BE-0402): nothing will clear this alert, so they are all
    # a blocked step or wait has left to name what stopped it.
    guard = AlertGuardConfig(rules=[guard_rule("Allow")])
    driver = _fake_with_alert(["Weird Button"])
    assert guard.probe_native(driver) == ("unhandled", None, ["Weird Button"])


def test_probe_native_treats_a_dismiss_race_as_absent() -> None:
    # TOCTOU: the alert vanishes between the presence query and the tap, so handle_system_alert
    # raises ElementNotFound. That is a benign self-resolved race — reported as absent, not a failure.
    class _RaceDriver(FakeDriver):
        def handle_system_alert(self, sel: base.Selector, timeout: float) -> None:
            raise base.ElementNotFound("the alert vanished before the tap")

    driver = _RaceDriver([])
    driver.system_alert_buttons = [_button("Allow")]
    guard = AlertGuardConfig(rules=[guard_rule("Allow")])
    assert guard.probe_native(driver) == ("absent", None, [])


# --- AlertGuardConfig.__call__ (native only since BE-0402) ------------------------------------------


def test_call_returns_the_native_event_and_leaves_no_note() -> None:
    guard = AlertGuardConfig(rules=[guard_rule("Allow")])
    assert guard(_fake_with_alert(["Allow"])) == AlertEvent(label="Allow")
    assert guard.blocked_note == ""  # the screen is unblocked; nothing to report


def test_call_reports_an_unnamed_alert_instead_of_guessing_at_it() -> None:
    # BE-0402: no rule or candidate label names this button, and nothing here will guess where to
    # tap. The step keeps failing — but with the alert named, which is the whole point.
    guard = AlertGuardConfig(rules=[guard_rule("Allow")])
    assert guard(_fake_with_alert(["Weird Button"])) is None
    assert "unhandled system alert" in guard.blocked_note
    assert "Weird Button" in guard.blocked_note


def test_call_leaves_no_note_on_an_incapable_backend() -> None:
    # No native query ran, so nothing was observed. A note here would claim a block the guard never
    # saw, on every failed step of every non-iOS backend.
    guard = AlertGuardConfig()
    assert guard(_Incapable()) is None  # type: ignore[arg-type]
    assert guard.blocked_note == ""


def test_call_clears_a_stale_note_once_the_alert_is_gone() -> None:
    # The note states the *latest* observation, never that a block was ever seen: a scenario runs its
    # steps against one config, so a sticky note would mislabel every later failure in it.
    guard = AlertGuardConfig(rules=[guard_rule("Allow")])
    assert guard(_fake_with_alert(["Weird Button"])) is None
    assert guard.blocked_note
    assert guard(FakeDriver([])) is None
    assert guard.blocked_note == ""


# --- the mid-wait gate on the native path -----------------------------------------------------------


def _for_wait(target_id: str, timeout: float) -> Wait:

    return Wait.model_validate({"for": {"id": target_id}, "timeout": timeout})


def test_gate_dismisses_natively_mid_wait_and_records_the_alert() -> None:
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"

    def react(d: FakeDriver, kind: str, _arg: object) -> None:
        if kind == "handle_system_alert":
            d.system_alert_buttons = []  # the alert cleared
            d.screen = [target]  # and the awaited element is revealed

    driver = _fake_with_alert(["Allow"], react=react)
    guard = AlertGuardConfig(rules=[guard_rule("Allow")])
    alerts: list[AlertEvent] = []
    ok, reason, _tree = _wait(
        driver, _for_wait("ready", 30.0), _LogicalClock(), alert_guard=guard, alerts=alerts
    )
    assert ok and reason == ""
    assert alerts == [AlertEvent(label="Allow")]


def test_gate_absent_native_alert_debounces_a_transient_collapse() -> None:
    # "absent" means the native query saw no *SpringBoard* alert. A single transient collapsed frame
    # under it must not fire the vision path — the debounce filters that false positive (BE-0315 /
    # BE-0269). A *persistent* non-SpringBoard collapse is the separate case the next test covers.
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"

    class _OneFrameCollapse(FakeDriver):
        def __init__(self) -> None:
            super().__init__([])  # capable; no alert seeded, so the native probe returns "absent"
            self._polls = 0

        def query(self) -> list[base.Element]:
            self._polls += 1
            return [] if self._polls == 1 else [target]  # one collapsed frame, then app UI

    # poll_interval=1.0 rate-limits only the native query; the collapsed-tree debounce still samples
    # every _POLL, so one transient frame that clears before the debounce is filtered regardless.
    guard = AlertGuardConfig(poll_interval=1.0)
    ok, reason, _tree = _wait(
        _OneFrameCollapse(), _for_wait("ready", 30.0), _LogicalClock(), alert_guard=guard, alerts=[]
    )
    assert ok and reason == ""  # revealed after one transient collapse; vision never called


def test_gate_absent_native_alert_reports_a_persistent_collapse_it_cannot_name() -> None:
    # "absent" only rules out a *SpringBoard* alert: an action sheet or a WKWebView JS dialog the
    # native query cannot enumerate reads as absent too while still collapsing the tree. Nothing can
    # clear that since BE-0402, so the wait runs to its own timeout — but the collapsed-tree proxy's
    # observation rides along on the failure, hedged, because no query ever named a button here.
    from bajutsu.common.orchestrator.waits import _wait

    driver = FakeDriver(
        []
    )  # capable, collapsed, no SpringBoard alert seeded → probe returns "absent"
    # poll_interval=1.0 (the realistic default) but a tight 2s budget: the collapsed-tree proxy must
    # sample every _POLL, not once per interval, or its observation would first land at ~3x
    # poll_interval and miss this budget entirely (the BE-0269 latency PR #1330 locked in).
    guard = AlertGuardConfig(poll_interval=1.0)
    ok, reason, _tree = _wait(
        driver, _for_wait("ready", 2.0), _LogicalClock(), alert_guard=guard, alerts=[]
    )
    assert not ok
    assert "wait timeout: for" in reason
    assert "the screen appears blocked" in reason  # hedged: no button was ever enumerated
    assert "buttons:" not in reason


def test_gate_polls_the_native_query_on_its_own_interval_not_every_tick() -> None:
    # The native query is rate-limited to one per poll_interval, decoupled from the 50ms condition
    # poll, so it does not roughly double the single-threaded runner's load (BE-0315).
    from bajutsu.common.orchestrator.waits import _wait

    probes = {"n": 0}

    class _CountingProbe(FakeDriver):
        def system_alert_labels(self) -> list[str]:
            probes["n"] += 1
            return []  # never an alert, so the wait runs to its full budget

    # App UI is visible (a non-collapsed tree), so the collapsed-tree vision fallback never enters —
    # this isolates the native probe's cadence from the "absent + collapsed" fallback path.
    app_ui = _button("home")
    guard = AlertGuardConfig(poll_interval=1.0)
    ok, _reason, _tree = _wait(
        _CountingProbe([app_ui]),
        _for_wait("never", 2.0),
        _LogicalClock(),
        alert_guard=guard,
        alerts=[],
    )
    assert not ok
    # ~40 condition polls over the 2s budget, but the native query fires about once per second:
    # a two-sided bound proves it re-fires on the interval (not just once) yet not every tick.
    assert 2 <= probes["n"] <= 4


def test_gate_dismisses_an_app_attached_sheet_from_the_tree_without_vision() -> None:
    # A system-owned prompt that is never SpringBoard-enumerable (e.g. iOS's Save Password sheet,
    # attached to the app's own accessibility tree) reads as "absent" on every native probe forever,
    # yet its own labeled, identifier-less buttons keep `shows_app_ui` from seeing a collapsed tree
    # either — both mid-wait detectors used to sit idle for the whole timeout on exactly this shape.
    # The in-tree label match (BE-0316's gap) must clear it within a poll or two, no vision call.
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"
    prompt_button = _button(
        "今はしない"
    )  # identifier-less, like a real system-owned sheet's button

    def react(d: FakeDriver, kind: str, arg: object) -> None:
        if kind == "tap" and arg == {"label": "今はしない", "traits": ["button"]}:
            d.screen = [target]  # dismissing the sheet reveals the awaited element

    driver = FakeDriver([prompt_button], react=react)  # capable; no SpringBoard alert seeded
    guard = AlertGuardConfig(
        rules=[guard_rule("今はしない"), guard_rule("Not Now")], poll_interval=1.0
    )
    alerts: list[AlertEvent] = []
    clock = _LogicalClock()
    ok, reason, _tree = _wait(
        driver, _for_wait("ready", 30.0), clock, alert_guard=guard, alerts=alerts
    )
    assert ok and reason == ""
    assert clock.now() < 1.0  # cleared at tree-poll latency, not anywhere near the 30s timeout
    assert alerts == [AlertEvent(label="今はしない")]


def test_dismiss_from_tree_taps_a_showing_at_most_once() -> None:
    # A real dismiss isn't instant: a fading-out button can linger in the tree for a poll or two
    # after the tap before the screen actually updates. Without its own guard, `_dismiss_from_tree`
    # runs every `_POLL` with no cooldown, so it would re-match and re-tap the same button on every
    # one of those polls — over-counting one dismissal into several `AlertEvent`s and actuating the
    # app repeatedly (a second tap can land on whatever is underneath a fading sheet).
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"
    prompt_button = _button("今はしない")

    class _LingeringDismiss(FakeDriver):
        def __init__(self) -> None:
            super().__init__([prompt_button])
            self._polls_since_tap: int | None = None

        def tap(self, sel: base.Selector) -> None:
            super().tap(sel)
            self._polls_since_tap = 0

        def query(self) -> list[base.Element]:
            if self._polls_since_tap is not None:
                self._polls_since_tap += 1
                if self._polls_since_tap >= 3:  # the animation finishes; the screen updates
                    return [target]
            return list(self.screen)

    driver = _LingeringDismiss()
    guard = AlertGuardConfig(rules=[guard_rule("今はしない")], poll_interval=1.0)
    alerts: list[AlertEvent] = []
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 2.0), _LogicalClock(), alert_guard=guard, alerts=alerts
    )
    assert ok
    tap_sel = {"label": "今はしない", "traits": ["button"]}
    assert driver.actions.count(("tap", tap_sel)) == 1  # tapped exactly once
    assert alerts == [AlertEvent(label="今はしない")]  # exactly one dismissal recorded, not several


def test_dismiss_from_tree_retries_a_delivered_tap_that_did_not_clear_the_prompt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The counterpart to the lingering-fade case above, and the one it cannot be told apart from at
    # the first poll: a tap the runner *accepts* can still leave the prompt up. Measured on iOS —
    # testmanagerd logged `touch down`/`touch up` at the target's exact centre and
    # `confirmed by TouchEventsCompleted`, yet the app never acted on the touch (PR #1686). Where the
    # fade clears the label within a poll or two, this never does, so the two differ only in what the
    # tree does *after* the tap.
    #
    # `_tree_dismiss_pending` arms on a tap that merely returned without raising, and re-arms only
    # once the tree stops matching that label — which an unacted-on tap never causes. So one such tap
    # disarms the in-tree path for the whole remaining wait: the sheet stays up, nothing retries, and
    # the step burns its full timeout with a dismissal recorded as if it had worked. Like
    # `ElementNotTappable`, this retries under a bound (`_TREE_DISMISS_MAX_TAPS`) rather than either
    # giving up after one attempt or hammering the device for the rest of the wait.
    from bajutsu.common.orchestrator.waits import _TREE_DISMISS_MAX_TAPS, _wait

    prompt_button = _button("今はしない")

    class _IneffectiveTap(FakeDriver):
        """Accepts the tap as a real runner does, but the prompt it targets never clears."""

        def __init__(self) -> None:
            super().__init__([prompt_button])
            self.tap_calls = 0

        def tap(self, sel: base.Selector) -> None:
            self.tap_calls += 1
            super().tap(sel)  # delivered and recorded; `screen` deliberately left unchanged

    driver = _IneffectiveTap()
    guard = AlertGuardConfig(rules=[guard_rule("今はしない")], poll_interval=1.0)
    alerts: list[AlertEvent] = []
    # A 30s budget, far longer than `_TREE_DISMISS_MAX_TAPS` taps spaced by `_TREE_RETAP_DELAY`, so
    # what stops the retries here is the tap ceiling rather than the wait running out: no retry at all
    # taps exactly once, an unbounded one ~30 times over this wait, and only the bound gives 3.
    with caplog.at_level(logging.WARNING, logger="bajutsu.common.orchestrator.waits"):
        ok, _reason, _tree = _wait(
            driver, _for_wait("ready", 30.0), _LogicalClock(), alert_guard=guard, alerts=alerts
        )
    assert not ok  # "ready" never appears either way, so the wait still times out on its own
    assert driver.tap_calls == _TREE_DISMISS_MAX_TAPS
    # The dismissal is recorded on the tap — the only moment it can be, since nothing there tells a
    # tap that lands from one the app never acts on — then withdrawn once the ceiling proves the
    # prompt never cleared. `AlertEvent` means a prompt the guard *dismissed*, so leaving it would
    # make the report contradict the warning below: timed out with the sheet still up, reported as
    # cleared. The retried actuations stay visible in the driver's own log (`tap_calls` above).
    assert alerts == []
    # Disclosed once, not silently: the wait is about to burn its whole budget, and without this the
    # timeout would read as "the awaited element never rendered" rather than "the prompt never left".
    gave_up = [r for r in caplog.records if "in-tree alert dismiss gave up" in r.message]
    assert len(gave_up) == 1
    # And the same disclosure rides the failure, not only the log (BE-0402). This is the one case the
    # collapsed-tree proxy cannot cover for: an app-attached sheet keeps the tree looking like
    # ordinary app UI, so without the note here the timeout would say nothing about the prompt at all.
    # Not the "unhandled" wording: this label *did* resolve and was tapped, and telling the author no
    # candidate was named would send them to add a label they already wrote.
    assert "a system prompt the guard could not clear is still up (button: 今はしない)" in _reason
    assert "unhandled system alert" not in _reason


def test_dismiss_from_tree_retry_that_lands_clears_the_prompt_and_passes_the_wait() -> None:
    # The payoff the retry exists for, which the never-clears test above cannot show: the second tap
    # *does* land, so the wait passes at retry latency instead of burning its timeout. Without this,
    # a change that left the retry inert — an off-by-one in the ceiling, a delay that never elapses
    # under the real clock — would keep every other in-tree test green while the sheet stays up on
    # device, since they all either tap once successfully or never clear at all.
    from bajutsu.common.orchestrator.waits import _TREE_RETAP_DELAY, _wait

    target = _button("R")
    target["identifier"] = "ready"
    prompt_button = _button("今はしない")

    class _SecondTapLands(FakeDriver):
        """The first tap is accepted but ignored by the app; the second actually dismisses."""

        def __init__(self) -> None:
            super().__init__([prompt_button])
            self.tap_calls = 0

        def tap(self, sel: base.Selector) -> None:
            self.tap_calls += 1
            super().tap(sel)
            if self.tap_calls >= 2:
                self.screen = [target]  # this one lands

    driver = _SecondTapLands()
    guard = AlertGuardConfig(rules=[guard_rule("今はしない")], poll_interval=1.0)
    alerts: list[AlertEvent] = []
    clock = _LogicalClock()
    ok, reason, _tree = _wait(
        driver, _for_wait("ready", 30.0), clock, alert_guard=guard, alerts=alerts
    )
    assert ok and reason == ""
    assert driver.tap_calls == 2  # the retry is what cleared it
    # Cleared one retry in, nowhere near the 30s budget — the recovery came from the retry, not from
    # the wait outlasting the prompt.
    assert clock.now() < _TREE_RETAP_DELAY * 2
    assert alerts == [AlertEvent(label="今はしない")]  # one prompt, one dismissal


def test_dismiss_from_tree_does_not_retry_when_the_tap_moved_the_screen() -> None:
    # The retry must not fire when the tap plainly *did* land. `_dismiss_from_tree` matches
    # identifier-less buttons, and a whole app can legitimately have none (`shows_app_ui`'s `-noax`
    # shape), so a dismissed sheet can reveal an app-authored button carrying the very same label.
    # Re-tapping that would actuate the app under test — navigating it mid-wait and failing the step
    # for an unrelated reason — and would end in a warning claiming a prompt is up when none is.
    # The screen having changed since the tap is what separates the two cases.
    from bajutsu.common.orchestrator.waits import _wait

    prompt_button = _button("今はしない")
    app_button = _button("今はしない")  # identifier-less, app-authored, same label
    content = _button("Content")
    content["identifier"] = "home.content"

    class _RevealsSameLabelAppButton(FakeDriver):
        def __init__(self) -> None:
            super().__init__([prompt_button])
            self.tap_calls = 0

        def tap(self, sel: base.Selector) -> None:
            self.tap_calls += 1
            super().tap(sel)
            self.screen = [app_button, content]  # sheet gone; the app's own button still matches

    driver = _RevealsSameLabelAppButton()
    guard = AlertGuardConfig(rules=[guard_rule("今はしない")], poll_interval=1.0)
    alerts: list[AlertEvent] = []
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 5.0), _LogicalClock(), alert_guard=guard, alerts=alerts
    )
    assert not ok  # "ready" never appears; the wait times out on its own
    assert driver.tap_calls == 1  # the app's button was never tapped, despite matching every poll
    assert alerts == [AlertEvent(label="今はしない")]  # the real dismissal stands


def test_dismiss_from_tree_records_a_second_showing_after_an_untapped_other_label() -> None:
    # A second label can appear and go without ever being tapped — here it collides with an
    # identically labelled in-app button, so the uniqueness pre-check declines before any tap. That
    # showing must not leave the *first* label pending: if it did, the first label reappearing would
    # compare equal to the stale pending, `first_tap` would be False, and a genuine second dismissal
    # would be tapped but never recorded — under-reporting the run's prompts.
    from bajutsu.common.orchestrator.waits import _POLL, _wait

    target = _button("R")
    target["identifier"] = "ready"
    save = _button("今はしない")
    other = _button("あとで")  # system-owned, but collides with the app button below
    app_other = _button("あとで")
    app_other["identifier"] = "screen.home.button.later"

    class _TwoShowings(FakeDriver):
        """`今はしない` → an untappable `あとで` → `今はしない` again, then the awaited screen."""

        def __init__(self) -> None:
            super().__init__([save])
            self.taps: list[str] = []
            self.other_polls = 0

        def tap(self, sel: base.Selector) -> None:
            super().tap(sel)
            label = str(sel["label"])
            self.taps.append(label)
            # Each dismissal of the save sheet reveals the next screen in the sequence.
            self.screen = [other, app_other] if len(self.taps) == 1 else [target]

        def query(self) -> list[base.Element]:
            # The colliding pair clears on its own, putting the save sheet back up — a second,
            # genuine showing that was never tapped in between. It has to stay up for longer than
            # one `poll_interval` to be part of the story: the in-tree path is paced by that
            # interval, so a showing that came and went inside one would simply never be observed.
            if self.screen and self.screen[0] is other:
                self.other_polls += 1
                if self.other_polls > int(1.0 / _POLL) + 1:
                    self.screen = [save]
                return [other, app_other]
            return list(self.screen)

    driver = _TwoShowings()
    guard = AlertGuardConfig(
        rules=[guard_rule("今はしない"), guard_rule("あとで")], poll_interval=1.0
    )
    alerts: list[AlertEvent] = []
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 5.0), _LogicalClock(), alert_guard=guard, alerts=alerts
    )
    assert ok
    assert driver.taps == ["今はしない", "今はしない"]  # the ambiguous `あとで` was never tapped
    # Both showings recorded — the second is a distinct prompt, not a retry of the first.
    assert alerts == [AlertEvent(label="今はしない"), AlertEvent(label="今はしない")]


def test_dismiss_from_tree_declines_on_not_yet_tappable_then_dismisses() -> None:
    # A sheet's own scrim can still cover its button while the presentation animation finishes, the
    # platform's hit-test (`isHittable` / `topmost_at_point`) reading the button as unreachable until
    # it settles. `ElementNotTappable` here is the same benign, self-resolved race `ElementNotFound`
    # and `AmbiguousSelector` already forgive — not a reason to fail the wait. One decline is what a
    # real presentation animation now costs: the in-tree path runs once per `poll_interval`, so the
    # retry arrives a full second later, well past a UIKit sheet's ~0.35-0.5s or an Android dialog's
    # ~0.25s+. `_TREE_DISMISS_DECLINE_GIVEUP` sits at twice that interval precisely so this retry
    # happens at all — a give-up horizon equal to the interval would spend itself on the first
    # attempt and leave the prompt up.
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"
    prompt_button = _button("今はしない")

    class _NotYetTappableDismiss(FakeDriver):
        def __init__(self) -> None:
            super().__init__([prompt_button])
            self.tap_calls = 0

        def tap(self, sel: base.Selector) -> None:
            self.tap_calls += 1
            if self.tap_calls < 2:  # the scrim is still animating away
                raise base.ElementNotTappable("covered by the sheet's own scrim")
            super().tap(sel)
            self.screen = [target]  # the animation finishes; the screen updates

    driver = _NotYetTappableDismiss()
    guard = AlertGuardConfig(rules=[guard_rule("今はしない")], poll_interval=1.0)
    alerts: list[AlertEvent] = []
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 3.0), _LogicalClock(), alert_guard=guard, alerts=alerts
    )
    assert ok
    assert driver.tap_calls == 2  # declined once, then dismissed on the next interval
    assert alerts == [AlertEvent(label="今はしない")]


def test_dismiss_from_tree_stops_retrying_a_permanently_covered_button() -> None:
    # Unlike the transient scrim above, a genuinely stuck obstruction (a scrim that never lifts, an
    # `elevation` false positive) must not re-issue a real actuation attempt for the rest of the
    # wait: `_decline_giveup` bounds how long this label's tap keeps being retried before the wait
    # falls back to its own timeout, the same shape the vision guard's attempt ceiling already uses
    # for a persistent false positive. Bounding it in seconds rather than polls is what keeps the
    # bound meaningful at any `poll_interval`, and deriving it *from* that interval is what keeps it
    # meaningful at a long one: the bound is checked before the tap, so a fixed horizon shorter than
    # two intervals would spend itself on the first attempt and never retry at all.
    from bajutsu.common.orchestrator.waits import _decline_giveup, _wait

    prompt_button = _button("今はしない")

    class _PermanentlyCoveredDismiss(FakeDriver):
        def __init__(self) -> None:
            super().__init__([prompt_button])
            self.tap_calls = 0

        def tap(self, sel: base.Selector) -> None:
            self.tap_calls += 1
            raise base.ElementNotTappable("covered by a scrim that never lifts")

    driver = _PermanentlyCoveredDismiss()
    guard = AlertGuardConfig(rules=[guard_rule("今はしない")], poll_interval=1.0)
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 3.0), _LogicalClock(), alert_guard=guard, alerts=[]
    )
    assert not ok  # "ready" never appears; the wait times out on its own deadline
    # Attempted at t=0 and t=1.0 (one `poll_interval` apart), then declined outright at t=2.0 once
    # the give-up horizon is reached — bounded, not one attempt per poll for the whole 3s wait.
    assert driver.tap_calls == 2
    assert _decline_giveup(1.0) == 2.0  # the horizon the count above is derived from
    # And the give-up says what it gave up on (BE-0402), like the tap-budget one: an obstructed sheet
    # keeps its own labelled buttons in the tree, so nothing else on this path would report it — and
    # in the same "could not clear" wording, since the label resolved here too.
    assert "a system prompt the guard could not clear is still up (button: 今はしない)" in _reason
    assert "unhandled system alert" not in _reason


def test_dismiss_from_tree_declines_on_an_in_app_label_collision() -> None:
    # A system-owned identifier-less button and an app-authored one share a rule's label:
    # `match_alert_rule` resolves uniquely over the identifier-less subset, but the whole-tree tap
    # sees both and must decline rather than tap the wrong one (determinism first). A *persistent*
    # collision (unlike a vanish race) must decline before ever attempting the tap: the collision
    # never clears, so `_tree_dismiss_pending` (only armed on a successful tap) never guards it, and
    # a decline reached only via `except AmbiguousSelector` would re-issue the on-device tap every
    # `_POLL` for the rest of the wait — count `tap()` calls directly, not `driver.actions`, since a
    # raised `AmbiguousSelector` never reaches `_record` either way.
    from bajutsu.common.orchestrator.waits import _wait

    prompt_button = _button("Not Now")  # identifier-less, system-owned
    app_button = _button("Not Now")
    app_button["identifier"] = "screen.home.button.not-now"

    class _CountingTapDriver(FakeDriver):
        def __init__(self) -> None:
            super().__init__([prompt_button, app_button])
            self.tap_calls = 0

        def tap(self, sel: base.Selector) -> None:
            self.tap_calls += 1
            super().tap(sel)

    driver = _CountingTapDriver()  # native-capable; no SpringBoard alert
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")], poll_interval=1.0)
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 0.2), _LogicalClock(), alert_guard=guard, alerts=[]
    )
    assert not ok
    assert driver.actions == []  # ambiguous → declined, nothing tapped
    assert driver.tap_calls == 0  # declined before attempting the tap, not caught after


def test_dismiss_from_tree_dismisses_despite_a_non_button_label_collision() -> None:
    # A bare `{"label": label}` selector resolves via `matches()` (base.py), which matches on
    # `label` alone and ignores `traits` — so a non-button element sharing the exact text (a static
    # caption drawn next to the sheet, a header) would make a trait-unscoped tap ambiguous despite
    # the intended button being uniquely named among buttons. Scoping both the pre-check and the tap
    # itself to `traits: [BUTTON]` (mirroring the `buttons` filter `label` was resolved against)
    # means a same-labeled caption never blocks dismissal at all.
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"
    prompt_button = _button("Not Now")  # identifier-less, system-owned
    caption = _button("Not Now")
    caption["traits"] = ["staticText"]  # not a button; must not block the tap below

    def react(d: FakeDriver, kind: str, arg: object) -> None:
        if kind == "tap" and arg == {"label": "Not Now", "traits": ["button"]}:
            d.screen = [target]

    driver = FakeDriver(
        [prompt_button, caption], react=react
    )  # native-capable; no SpringBoard alert
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")], poll_interval=1.0)
    alerts: list[AlertEvent] = []
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 0.2), _LogicalClock(), alert_guard=guard, alerts=alerts
    )
    assert ok
    assert alerts == [AlertEvent(label="Not Now")]


def test_dismiss_from_tree_never_matches_an_in_app_button_carrying_an_identifier() -> None:
    # An app screen with its own button that happens to share a policy label (e.g. a real in-app
    # "Not Now") must never be tapped by the guard — only a system-owned, identifier-less button can
    # match, same restriction `system_alert_labels()` already assumes for a genuine SpringBoard alert.
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"
    app_button = _button("Not Now")
    app_button["identifier"] = "screen.home.button.not-now"  # an app-authored button, not a prompt

    driver = FakeDriver([app_button])  # capable; no SpringBoard alert seeded; never reveals "ready"
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")], poll_interval=1.0)
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 0.2), _LogicalClock(), alert_guard=guard, alerts=[]
    )
    assert not ok  # the in-app button was never tapped, so "ready" never appears
    assert driver.actions == []  # confirms no tap was issued against it


def test_dismiss_from_tree_never_fires_on_a_non_native_backend() -> None:
    # The in-tree match is native-only (the app-attached-sheet case it targets is an iOS one): a
    # web/Android-shaped backend must keep its pre-existing behavior untouched — only the
    # collapsed-tree + vision path (BE-0269) can act there, never the fast in-tree tap.
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"
    prompt_button = _button("今はしない")

    def react(d: FakeDriver, kind: str, arg: object) -> None:
        if kind == "tap" and arg == {"label": "今はしない"}:
            d.screen = [target]  # would reveal "ready" if the in-tree path fired (it must not)

    driver = _NonNativeDriver([prompt_button], react=react)
    guard = AlertGuardConfig(
        rules=[guard_rule("今はしない"), guard_rule("Not Now")], poll_interval=1.0
    )
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 0.2), _LogicalClock(), alert_guard=guard, alerts=[]
    )
    assert not ok  # never tapped, so "ready" never appears; the wait runs to its full budget
    assert driver.actions == []


def test_dismiss_from_tree_never_fires_on_generic_dismissive_vocabulary_alone() -> None:
    # "Cancel" is generic English UI vocabulary a real app screen can legitimately show, and since
    # BE-0406 no built-in list of such words is a policy this guard resolves from at all. The fast
    # in-tree path must stay off unless the scenario declared a rule of its own; a ruleless guard
    # falls back to the collapsed-tree report, same as before this path existed.
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"
    cancel_button = _button("Cancel")  # identifier-less; no rule names it

    def react(d: FakeDriver, kind: str, arg: object) -> None:
        if kind == "tap" and arg == {"label": "Cancel"}:
            d.screen = [target]  # would reveal "ready" if the in-tree path fired (it must not)

    driver = FakeDriver([cancel_button], react=react)  # native-capable; no rules configured
    guard = AlertGuardConfig(poll_interval=1.0)  # rules=[] (default)
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 0.2), _LogicalClock(), alert_guard=guard, alerts=[]
    )
    assert not ok
    assert driver.actions == []


def test_dismiss_from_tree_never_fires_on_a_springboard_only_rule() -> None:
    # A rule's `AlertSurfaces` record says where its prompt can appear, and only an `in_tree` one
    # arms the tree match (BE-0406). A scenario declaring, say, the notifications prompt alone — pure
    # SpringBoard — must not thereby license a tap on any identifier-less button of that name in the
    # app's own tree: `tree_rules` is empty, so the path never arms.
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"
    prompt_button = _button("Not Now")  # identifier-less, and named by the rule below

    def react(d: FakeDriver, kind: str, arg: object) -> None:
        if kind == "tap" and arg == {"label": "Not Now", "traits": ["button"]}:
            d.screen = [target]  # would reveal "ready" if the in-tree path fired (it must not)

    driver = FakeDriver([prompt_button], react=react)  # native-capable; no SpringBoard alert
    guard = AlertGuardConfig(rules=[guard_rule("Not Now", in_tree=False)], poll_interval=1.0)
    assert guard.tree_rules == []
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 0.2), _LogicalClock(), alert_guard=guard, alerts=[]
    )
    assert not ok  # never tapped, so "ready" never appears
    assert driver.actions == []


def test_dismiss_from_tree_fires_on_the_same_rule_marked_in_tree() -> None:
    # The other half of the pair above, and what makes it an arming test rather than a driver quirk:
    # the identical prompt, tree and wait, with the rule's own `in_tree` flag as the only difference,
    # is cleared within a poll.
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"
    prompt_button = _button("Not Now")

    def react(d: FakeDriver, kind: str, arg: object) -> None:
        if kind == "tap" and arg == {"label": "Not Now", "traits": ["button"]}:
            d.screen = [target]

    driver = FakeDriver([prompt_button], react=react)
    guard = AlertGuardConfig(rules=[guard_rule("Not Now", in_tree=True)], poll_interval=1.0)
    alerts: list[AlertEvent] = []
    ok, reason, _tree = _wait(
        driver, _for_wait("ready", 5.0), _LogicalClock(), alert_guard=guard, alerts=alerts
    )
    assert ok and reason == ""
    assert alerts == [AlertEvent(label="Not Now")]


def test_gate_unhandled_native_alert_names_it_in_the_wait_timeout() -> None:
    # An alert is up but no policy label resolves (unknown button). BE-0402 leaves it alone rather
    # than asking a model where to tap — and the wait, which would otherwise report only the element
    # that never appeared, names the alert and the buttons the probe actually read.
    from bajutsu.common.orchestrator.waits import _wait

    driver = _fake_with_alert(["Weird Button"])  # capable; alert stays up (never dismissed)
    guard = AlertGuardConfig(rules=[guard_rule("Allow")], poll_interval=1.0)
    ok, reason, _tree = _wait(
        driver, _for_wait("never", 30.0), _LogicalClock(), alert_guard=guard, alerts=[]
    )
    assert not ok
    assert reason.startswith("wait timeout: for")
    assert "an unhandled system alert is blocking the screen (buttons: Weird Button)" in reason
    assert driver.actions == []  # and nothing was tapped on the way there


def test_gate_drops_the_note_once_the_prompt_goes_away() -> None:
    # A prompt that appears and resolves itself mid-wait must leave nothing behind: the note states
    # the latest observation, so a later, unrelated timeout never blames an alert long gone. The app
    # is on screen throughout, so the collapsed-tree proxy has nothing of its own to say either.
    from bajutsu.common.orchestrator.waits import _wait

    class _SelfResolving(FakeDriver):
        probes = 0

        def system_alert_labels(self) -> list[str]:
            self.probes += 1
            return ["Weird Button"] if self.probes < 3 else []

    app = _button("Home")
    app["identifier"] = "screen.home"
    driver = _SelfResolving([app])
    guard = AlertGuardConfig(rules=[guard_rule("Allow")], poll_interval=0.05)
    ok, reason, _tree = _wait(
        driver, _for_wait("never", 1.0), _LogicalClock(), alert_guard=guard, alerts=[]
    )
    assert not ok
    assert reason.startswith("wait timeout: for")
    assert "\u2014" not in reason  # no note appended: the prompt resolved itself mid-wait


# --- the in-tree dismiss is gated on a fresh "no SpringBoard alert" answer ---------------------


def test_dismiss_from_tree_is_withheld_while_a_springboard_alert_is_up() -> None:
    # The regression this locks in. The save-password prompt is an *app-process* sheet, so it shows
    # up in the poll's own tree and `_dismiss_from_tree` is the only path that clears it; the
    # notification request beside it is a SpringBoard alert the app's tree cannot see. `Driver.tap`
    # resolves an element, and XCUITest answers whatever out-of-process alert is interrupting before
    # it synthesizes such an interaction — with its own default handler, which taps the alert's
    # *default* button ("Allow"), the opposite of the guard's least-destructive policy and invisible
    # to the report. So while the native probe says an alert is up, the in-tree tap must not be
    # issued at all: the SpringBoard alert is answered natively first, by the scenario's policy.
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"
    save_sheet = _button("Not Now")  # identifier-less, the app-attached sheet's own button

    class _AlertOverSheet(FakeDriver):
        """A SpringBoard alert stands over an app sheet until the native path taps its button."""

        def __init__(self) -> None:
            super().__init__([save_sheet])
            # Both prompts are up: the sheet in the tree, the permission request in SpringBoard.
            self.system_alert_buttons = [_button("Don't Allow"), _button("Allow")]
            self.tapped: list[str] = []

        def tap(self, sel: base.Selector) -> None:
            self.tapped.append(str(sel["label"]))
            super().tap(sel)
            self.screen = [target]

        def handle_system_alert(self, sel: base.Selector, timeout: float) -> None:
            super().handle_system_alert(sel, timeout)
            self.system_alert_buttons = []  # answered by policy; the sheet is now uncovered

    driver = _AlertOverSheet()
    # One `systemAlertHandling.instruction` covers both prompts, as an author would write it: the
    # permission request's refusal first, then the sheet's own dismissal. Each path resolves the one
    # candidate its own surface carries.
    guard = AlertGuardConfig(
        rules=[guard_rule("Don't Allow"), guard_rule("Not Now")], poll_interval=1.0
    )
    alerts: list[AlertEvent] = []
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 5.0), _LogicalClock(), alert_guard=guard, alerts=alerts
    )
    assert ok
    # The order is the whole point: the SpringBoard alert is refused natively, and only afterwards
    # is the app-attached sheet cleared from the tree. Never the reverse, and never both at once.
    assert alerts == [AlertEvent(label="Don't Allow"), AlertEvent(label="Not Now")]
    assert driver.tapped == ["Not Now"]  # the in-tree tap fired once, after the alert was gone


def test_dismiss_from_tree_is_paced_by_the_native_probe_not_by_the_poll() -> None:
    # The native probe is rate-limited to `poll_interval` (a per-`_POLL` SpringBoard query would
    # roughly double the runner's single-main-thread load, BE-0315), so on every other poll the gate
    # has no current answer about SpringBoard at all. Gating the in-tree tap on a probe that ran
    # *this* poll is what stops those polls tapping blind — and the observable consequence is the
    # spacing: a tap that never clears the prompt repeats once per `poll_interval`, not once per
    # `_TREE_RETAP_DELAY`.
    #
    # The interval has to exceed that delay for the assertion to mean anything. At the default 1.0
    # the two coincide and the retap delay alone would produce the same timings, so this drives a
    # 2.0 interval: ungated, the second tap lands 1.0s after the first.
    from itertools import pairwise

    from bajutsu.common.orchestrator.waits import _wait

    prompt_button = _button("Not Now")

    class _TapNeverLands(FakeDriver):
        """The prompt stays up however often it is tapped — an unactioned tap (measured on iOS)."""

        def __init__(self) -> None:
            super().__init__([prompt_button])
            self.tap_times: list[float] = []

        def tap(self, sel: base.Selector) -> None:
            self.tap_times.append(clock.now())
            super().tap(sel)  # the screen never changes; the prompt stays in the tree

    clock = _LogicalClock()
    driver = _TapNeverLands()
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")], poll_interval=2.0)
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 10.0), clock, alert_guard=guard, alerts=[]
    )
    assert not ok  # "ready" never appears; the wait times out on its own deadline
    assert len(driver.tap_times) > 1, driver.tap_times
    gaps = [b - a for a, b in pairwise(driver.tap_times)]
    assert all(gap >= 2.0 for gap in gaps), gaps


def test_dismiss_from_tree_still_runs_when_no_springboard_alert_is_up() -> None:
    # The gate withholds the in-tree tap, it does not retire it: on a poll whose own native probe
    # reported no SpringBoard alert, the app-attached sheet is cleared exactly as before.
    from bajutsu.common.orchestrator.waits import _wait

    target = _button("R")
    target["identifier"] = "ready"
    sheet_button = _button("Not Now")

    class _SheetOnly(FakeDriver):
        def __init__(self) -> None:
            super().__init__([sheet_button])
            self.tapped: list[str] = []

        def tap(self, sel: base.Selector) -> None:
            self.tapped.append(str(sel["label"]))
            super().tap(sel)
            self.screen = [target]

    driver = _SheetOnly()
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")], poll_interval=1.0)
    alerts: list[AlertEvent] = []
    ok, _reason, _tree = _wait(
        driver, _for_wait("ready", 5.0), _LogicalClock(), alert_guard=guard, alerts=alerts
    )
    assert ok
    assert driver.tapped == ["Not Now"]
    assert alerts == [AlertEvent(label="Not Now")]


def test_dismiss_from_tree_waits_out_the_retap_delay_at_a_short_poll_interval() -> None:
    # `_TREE_RETAP_DELAY` still governs the gap between two taps on one showing, independently of the
    # `poll_interval` the gate now paces the in-tree path by. A scenario that tunes `pollInterval`
    # below that delay gets several in-tree passes inside one dismiss animation, and every pass but
    # the first must decline: re-tapping while a sheet is still fading out lands on whatever is
    # underneath it. So the taps are spaced by the delay, not by the interval, and stop at
    # `_TREE_DISMISS_MAX_TAPS` rather than continuing for the rest of the wait.
    from itertools import pairwise

    from bajutsu.common.orchestrator.waits import _TREE_DISMISS_MAX_TAPS, _TREE_RETAP_DELAY, _wait

    prompt_button = _button("Not Now")

    class _TapNeverLands(FakeDriver):
        def __init__(self) -> None:
            super().__init__([prompt_button])
            self.tap_times: list[float] = []

        def tap(self, sel: base.Selector) -> None:
            self.tap_times.append(clock.now())
            super().tap(sel)  # the screen never changes; the prompt stays in the tree

    clock = _LogicalClock()
    driver = _TapNeverLands()
    # A fifth of the retap delay: five in-tree passes fit inside one, so a gap of `poll_interval`
    # between taps would be plainly visible in the timings below.
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")], poll_interval=0.2)
    ok, _reason, _tree = _wait(driver, _for_wait("ready", 5.0), clock, alert_guard=guard, alerts=[])
    assert not ok  # "ready" never appears; the wait times out on its own deadline
    assert len(driver.tap_times) == _TREE_DISMISS_MAX_TAPS
    gaps = [b - a for a, b in pairwise(driver.tap_times)]
    assert all(gap >= _TREE_RETAP_DELAY for gap in gaps), gaps


# --- the interruption policy pushed to the backend ---------------------------------------------


def test_push_interruption_policy_hands_the_backend_the_guard_s_own_rules() -> None:
    # The decision stays here: what the backend receives is the scenario's own resolved rules, plus
    # `governs=True`, so the runner reports (rather than guesses at) any alert none of them identify.
    # Without this the backend answers an alert that interrupts one of its own interactions with the
    # alert's *default* button, which is the opposite of the least-destructive policy and reaches no
    # report.
    from bajutsu.common.orchestrator import push_interruption_policy

    driver = FakeDriver([])
    rule = ResolvedAlertRule(
        identifying_labels=frozenset({"Allow", "Don't Allow"}), tap_label="Don't Allow"
    )
    push_interruption_policy(driver, AlertGuardConfig(rules=[rule]))
    assert driver.interruption_policy == ([({"Allow", "Don't Allow"}, "Don't Allow")], True)


def test_push_interruption_policy_still_governs_without_rules() -> None:
    # A scenario that declares no rules of its own still governs — a guard being on at all is what
    # decides whether a declined alert gets reported, independent of whether any rule survived the
    # in-tree-only drop below (BE-0406 Unit 2b): a real declaration filtered down to nothing this
    # surface can act on is not the same as no declaration at all.
    from bajutsu.common.orchestrator import push_interruption_policy

    driver = FakeDriver([])
    push_interruption_policy(driver, AlertGuardConfig())
    assert driver.interruption_policy == ([], True)


def test_push_interruption_policy_omits_a_rule_the_backend_can_never_meet() -> None:
    # This surface exists for an alert in *another* process interrupting an XCUITest interaction, so
    # a prompt raised into the application's own process never reaches it. Pushing such a rule anyway
    # would not merely be untidy: the Swift side matches by subset, so an in-tree-only shape would
    # re-open on the runner the collision `excluded_labels` closes here (BE-0406).
    from bajutsu.common.orchestrator import push_interruption_policy

    driver = FakeDriver([])
    reachable = guard_rule("Don't Allow", identifying=("Allow", "Don't Allow"))
    in_tree_only = guard_rule("Not Now", identifying=("Save", "Not Now"), native=False)
    push_interruption_policy(driver, AlertGuardConfig(rules=[reachable, in_tree_only]))
    assert driver.interruption_policy is not None
    assert driver.interruption_policy[0] == [({"Allow", "Don't Allow"}, "Don't Allow")]


def test_push_interruption_policy_refuses_a_reachable_rule_carrying_an_exclusion_set() -> None:
    # No such shape exists today — every excluded one is in-tree-only, and dropped above — so this
    # fails loudly rather than letting a later addition reach the runner with its exclusion silently
    # discarded, which is exactly the subset-match collision the drop avoids.
    from bajutsu.common.orchestrator import push_interruption_policy

    excluding = ResolvedAlertRule(
        identifying_labels=frozenset({"Save", "Not Now"}),
        tap_label="Not Now",
        excluded_labels=frozenset({"Never for This Card"}),
        native=True,
    )
    driver = FakeDriver([])
    with pytest.raises(ValueError, match="exclusion set"):
        push_interruption_policy(driver, AlertGuardConfig(rules=[excluding]))
    assert driver.interruption_policy is None  # refused outright, not half-pushed


def test_push_interruption_policy_clears_it_when_the_scenario_disables_the_guard() -> None:
    # `systemAlertHandling: false` must not inherit the previous scenario's policy from the resident
    # runner, so the push happens with an empty, non-governing policy rather than being skipped. Not
    # governing is what keeps this the one case that still leaves a declined alert unreported —
    # declaring nothing is the author's own choice, not an omission this mechanism exists to catch.
    from bajutsu.common.orchestrator import push_interruption_policy

    driver = FakeDriver([])
    push_interruption_policy(driver, None)
    assert driver.interruption_policy == ([], False)


def test_drain_interruptions_reports_what_the_backend_answered_as_alert_events() -> None:
    # A prompt answered inside the backend's interruption handling is still a prompt this run
    # dismissed; reporting it is what keeps that dismissal out of the silence the mechanism exists
    # to end.
    from bajutsu.common.orchestrator import drain_interruptions

    driver = FakeDriver([])
    driver.interruptions_to_drain = ["Don't Allow", "Not Now"]
    drained = drain_interruptions(driver)
    assert drained.alerts == [AlertEvent(label="Don't Allow"), AlertEvent(label="Not Now")]
    assert drained.undeclared == []
    again = drain_interruptions(driver)
    assert again.alerts == []  # drained, not repeated onto the next step


def test_drain_interruptions_reports_what_the_backend_declined_as_undeclared_interruptions() -> (
    None
):
    # A declined alert is not a dismissal — nothing answered it on the scenario's behalf — so it is
    # reported through the separate `undeclared` list, buttons intact, for the caller to fail by name
    # rather than let the interruption pass in silence (BE-0406 Unit 2b).
    from bajutsu.common.orchestrator import drain_interruptions
    from bajutsu.common.orchestrator.types import UndeclaredInterruption

    driver = FakeDriver([])
    driver.interruptions_declined_to_drain = [["Save", "Not Now"], ["Allow", "Don't Allow"]]
    drained = drain_interruptions(driver)
    assert drained.alerts == []
    assert drained.undeclared == [
        UndeclaredInterruption(buttons=["Save", "Not Now"]),
        UndeclaredInterruption(buttons=["Allow", "Don't Allow"]),
    ]
    again = drain_interruptions(driver)
    assert again.undeclared == []  # drained, not repeated


def test_drain_interruptions_reports_a_swiped_banner_as_its_own_kind_of_alert_event() -> None:
    # A banner cleared at interruption time is a dismissal, so it belongs in `alerts` — but under a
    # kind of its own, since its `label` is the notification's text rather than a button anything
    # pressed (BE-0416). Without the discriminator the report shows it as an alert dismissal naming
    # a button that never existed.
    from bajutsu.common.orchestrator import drain_interruptions

    driver = FakeDriver([])
    driver.interruptions_to_drain = ["Not Now"]
    driver.banners_to_drain = ["Ready for Apple Intelligence"]
    drained = drain_interruptions(driver)
    assert drained.alerts == [
        AlertEvent(label="Not Now"),
        AlertEvent(label="Ready for Apple Intelligence", kind="notificationBanner"),
    ]
    again = drain_interruptions(driver)
    assert again.alerts == []  # drained, not repeated onto the next step


def test_a_swiped_banner_is_never_an_undeclared_interruption() -> None:
    # The defect BE-0416 Unit 1 measured: a banner reaching the alert path could match no rule, so a
    # governed run recorded it as undeclared and failed an otherwise-passing step, naming the
    # notification's body text as a button. No scenario can declare a banner — it offers no button to
    # identify — so failing over one would fail every run a notification happened to land in.
    from bajutsu.common.orchestrator import drain_interruptions

    driver = FakeDriver([])
    driver.banners_to_drain = ["Ready for Apple Intelligence"]
    assert drain_interruptions(driver).undeclared == []


def test_a_gone_wait_is_guarded_so_an_in_app_prompt_can_be_cleared() -> None:
    # `gone` went unguarded on the reasoning that a blocking prompt collapses the tree, which already
    # satisfies "gone". That holds for a SpringBoard prompt and only for those: iOS's "Save Password"
    # alert is drawn in the app's own process, so it collapses nothing and *adds* its buttons to the
    # tree. A `gone` wait on one of them then sits unsatisfied for its whole timeout with nothing to
    # clear it — measured on-device before this branch was guarded.
    from bajutsu.common.orchestrator.waits import _wait

    prompt_button = _button("Not Now")

    class _AppOwnedPrompt(FakeDriver):
        def __init__(self) -> None:
            super().__init__([_button("Sign In"), prompt_button])
            self.tapped: list[str] = []

        def tap(self, sel: base.Selector) -> None:
            self.tapped.append(str(sel["label"]))
            super().tap(sel)
            self.screen = [_button("Sign In")]  # the alert closes

    driver = _AppOwnedPrompt()
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")], poll_interval=1.0)
    alerts: list[AlertEvent] = []
    ok, _reason, _tree = _wait(
        driver,
        Wait.model_validate({"until": {"gone": {"label": "Not Now"}}, "timeout": 10.0}),
        _LogicalClock(),
        alert_guard=guard,
        alerts=alerts,
    )
    assert ok  # the guard cleared it, so "gone" became true well inside the timeout
    assert driver.tapped == ["Not Now"]
    assert alerts == [AlertEvent(label="Not Now")]


def test_the_interruption_policy_is_skipped_on_a_backend_without_the_opt_in() -> None:
    # A narrow opt-in: only XCUITest interposes on an interaction this way, so a backend that does
    # not implement it is never asked and contributes no events — the run is otherwise unchanged.
    from bajutsu.common.orchestrator import drain_interruptions, push_interruption_policy

    class _NoOptIn:
        """A backend stub carrying neither half of the opt-in (a web / Android shape)."""

    driver = cast("base.Driver", _NoOptIn())
    push_interruption_policy(driver, AlertGuardConfig())
    drained = drain_interruptions(driver)
    assert drained.alerts == []
    assert drained.undeclared == []


# --- the end-of-step / expect guard's own in-tree dismissal --------------------------------------


def test_the_end_of_step_guard_clears_an_app_owned_prompt_from_the_tree() -> None:
    # Measured on iOS 26.3/26.4/26.5: the save-password alert can arrive *after* a scenario's last
    # wait has returned, so the only guard left to meet it is the end-of-step / expect one. Its
    # native probe reads `springboard.alerts` and sees nothing there, and the vision fallback no-ops
    # without a credential — so without this the prompt covered the screen and `expect` read a
    # covered tree.
    prompt_button = _button("Not Now")
    driver = FakeDriver([_button("Sign In"), prompt_button])
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")])
    assert guard(driver) == AlertEvent(label="Not Now")
    assert driver.actions and driver.actions[-1][0] == "tap"


def test_the_end_of_step_guard_leaves_the_tree_alone_while_a_springboard_alert_is_up() -> None:
    # The same licence the mid-wait gate needs: XCUITest answers an interrupting out-of-process alert
    # before it synthesizes any element interaction, so an app tap issued while one is up is not this
    # guard's to make. The native path answers that alert first; the tree is next time's business.
    driver = _fake_with_alert(["Don't Allow", "Allow"])
    driver.screen = [_button("Not Now")]
    guard = AlertGuardConfig(rules=[guard_rule("Don't Allow"), guard_rule("Not Now")])
    assert guard(driver) == AlertEvent(label="Don't Allow")  # the SpringBoard alert, natively
    assert not any(action[0] == "tap" for action in driver.actions)


def test_the_end_of_step_guard_declines_an_ambiguous_in_tree_label() -> None:
    # Determinism first, exactly as the mid-wait path: two buttons carrying the configured label is
    # not a prompt this guard may guess at. It reports nothing rather than tapping one.
    driver = FakeDriver([_button("Not Now"), _button("Not Now")])
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")])
    assert guard(driver) is None
    assert not any(action[0] == "tap" for action in driver.actions)


def test_the_end_of_step_guard_stays_off_the_tree_without_scenario_rules() -> None:
    # The in-tree surface is armed only by the scenario's own in-tree rules, never by the built-in
    # dismissive defaults — "Cancel" / "Close" are ordinary UI vocabulary a real screen can show.
    driver = FakeDriver([_button("Cancel")])
    guard = AlertGuardConfig()
    assert guard(driver) is None
    assert not any(action[0] == "tap" for action in driver.actions)


def test_the_end_of_step_guard_declines_when_an_identified_button_shares_the_label() -> None:
    # `match_alert_rule` resolves over the identifier-less subset, so a same-named *identified* app
    # button does not stop it — but the tap sees the whole tree and would be ambiguous. The
    # whole-tree uniqueness pre-check is what catches that, exactly as the mid-wait path's does.
    app_button = _button("Not Now")
    app_button["identifier"] = "screen.home.button.not-now"
    driver = FakeDriver([_button("Not Now"), app_button])
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")])
    assert guard(driver) is None
    assert not any(action[0] == "tap" for action in driver.actions)


def test_the_end_of_step_guard_reports_nothing_when_the_prompt_closes_itself() -> None:
    # The button left the tree between this guard's own read and its tap. Benign: the prompt is gone,
    # which is what the caller wanted, and the step's own outcome still decides the verdict.
    class _VanishingPrompt(FakeDriver):
        def tap(self, sel: base.Selector) -> None:
            raise base.ElementNotFound("the prompt closed itself")

    driver = _VanishingPrompt([_button("Not Now")])
    guard = AlertGuardConfig(rules=[guard_rule("Not Now")])
    assert guard(driver) is None


def test_a_blocked_tap_names_the_alert_in_the_step_s_own_failure() -> None:
    # BE-0402's promise for the *step* call site, which holds no mid-wait gate: a `tap` blocked by an
    # alert no rule or candidate label names would otherwise fail as a bare `element not found`, with
    # nothing to say a prompt was on screen at all.
    from bajutsu.common.orchestrator import run_scenario
    from bajutsu.common.scenario import load_scenarios

    driver = _fake_with_alert(["Weird Button"])  # nothing on screen, and an unnamed prompt over it
    result = run_scenario(
        driver,
        load_scenarios("- name: t\n  steps:\n    - tap: { id: go }\n")[0],
        alert_guard=AlertGuardConfig(rules=[guard_rule("Allow")]),
    )
    assert not result.ok
    reason = result.steps[0].reason or ""
    assert "an unhandled system alert is blocking the screen (buttons: Weird Button)" in reason
    assert result.steps[0].alerts == []  # nothing was dismissed, and nothing claims otherwise


def test_a_blocked_wait_names_the_alert_exactly_once() -> None:
    # A guarded `wait` passes through both call sites: the mid-wait gate appends the note to the
    # timeout it returns, and the end-of-step guard then re-probes the same still-unanswered alert.
    # The note states one observation, so it must be said once — not doubled by the second look.
    from bajutsu.common.orchestrator import run_scenario
    from bajutsu.common.scenario import load_scenarios

    driver = _fake_with_alert(["Weird Button"])
    result = run_scenario(
        driver,
        load_scenarios("- name: t\n  steps:\n    - wait: { for: { id: never }, timeout: 0.3 }\n")[
            0
        ],
        alert_guard=AlertGuardConfig(rules=[guard_rule("Allow")]),
    )
    assert not result.ok
    reason = result.steps[0].reason or ""
    assert reason.count("an unhandled system alert is blocking the screen") == 1


def test_a_blocked_expect_names_the_alert_in_the_scenario_s_own_failure() -> None:
    # The same promise for the `expect` phase, whose retry calls the guard directly too.
    from bajutsu.common.orchestrator import run_scenario
    from bajutsu.common.scenario import load_scenarios

    driver = _fake_with_alert(["Weird Button"])
    result = run_scenario(
        driver,
        load_scenarios(
            "- name: t\n"
            "  steps:\n    - wait: { until: settled, timeout: 0.1 }\n"
            "  expect:\n    - exists: { id: later }\n"
        )[0],
        alert_guard=AlertGuardConfig(rules=[guard_rule("Allow")]),
    )
    assert not result.ok
    assert (result.failure or "").startswith("expect: ")
    assert "an unhandled system alert is blocking the screen (buttons: Weird Button)" in (
        result.failure or ""
    )


def test_a_step_failing_with_no_alert_up_keeps_its_own_bare_reason() -> None:
    # The note is not a blanket suffix on every failure: with no alert on screen there is nothing to
    # report, and a step that failed for its own reasons must not be made to look blocked.
    from bajutsu.common.orchestrator import run_scenario
    from bajutsu.common.scenario import load_scenarios

    result = run_scenario(
        FakeDriver([]),
        load_scenarios("- name: t\n  steps:\n    - tap: { id: go }\n")[0],
        alert_guard=AlertGuardConfig(rules=[guard_rule("Allow")]),
    )
    assert not result.ok
    assert "system alert" not in (result.steps[0].reason or "")


def test_the_decline_give_up_follows_a_tuned_poll_interval() -> None:
    # The bound is checked *before* the tap, so a horizon shorter than two intervals is spent on the
    # first attempt and the label is never re-tapped. A scenario that raises `pollInterval` — the
    # save-password one sets 5 — would hit exactly that with a fixed 2s horizon, which is the
    # zero-retry case the value exists to avoid. Deriving it from the interval keeps the rationale
    # true at every cadence, and the floor keeps the animation horizon at short ones.
    from bajutsu.common.orchestrator.waits import _decline_giveup

    assert _decline_giveup(0.2) == 2.0  # floored at the presentation-animation horizon
    assert _decline_giveup(1.0) == 2.0  # the default cadence: two intervals is the floor exactly
    assert _decline_giveup(5.0) == 10.0  # a tuned interval still buys a retry


def test_probe_native_reports_an_ambiguous_alert_as_unhandled_not_absent() -> None:
    # `AmbiguousSelector` from the tap is not the same race as `ElementNotFound`: the alert is still
    # up, now offering the label twice. Calling that "absent" would tell `_observe_native` no system
    # alert is showing, which is the one thing licensing an in-tree tap — and a tap made under a live
    # alert is what XCUITest answers with its own default button.
    class _AmbiguousOnTap(FakeDriver):
        def handle_system_alert(self, sel: base.Selector, timeout: float) -> None:
            raise base.AmbiguousSelector("the alert offers this label twice")

    driver = _AmbiguousOnTap([])
    driver.system_alert_buttons = [_button("Don't Allow"), _button("Allow")]
    guard = AlertGuardConfig(rules=[guard_rule("Don't Allow")])
    assert guard.probe_native(driver) == ("unhandled", None, ["Don't Allow", "Allow"])


def test_probe_native_still_reports_a_vanished_alert_as_absent() -> None:
    # The other half of the same race keeps its answer: the alert really did go away between the
    # presence query and the tap, so nothing is blocking and the in-tree path may proceed.
    class _VanishedOnTap(FakeDriver):
        def handle_system_alert(self, sel: base.Selector, timeout: float) -> None:
            raise base.ElementNotFound("the alert vanished")

    driver = _VanishedOnTap([])
    driver.system_alert_buttons = [_button("Don't Allow"), _button("Allow")]
    guard = AlertGuardConfig(rules=[guard_rule("Don't Allow")])
    assert guard.probe_native(driver) == ("absent", None, [])
