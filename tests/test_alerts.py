"""Tests for the system-alert guard, the Claude locator, and the orchestrator hook."""

from __future__ import annotations

import json
import struct
from pathlib import Path

from conftest import GUARD_LABEL, AlertingDriver, FakeBackend, FakeBlock, ShotDriver, guard_rule

from bajutsu.common.agents.alerts import AlertDecision, ClaudeAlertLocator, SystemAlertGuard
from bajutsu.common.agents.protocols import Proposal
from bajutsu.common.ai.base import AnyTool, ImagePart, TextPart
from bajutsu.common.drivers import base
from bajutsu.common.drivers.fake import FakeDriver
from bajutsu.common.evidence import FileSink
from bajutsu.common.orchestrator import AlertEvent, AlertGuardConfig, run_scenario
from bajutsu.common.scenario import Step, load_scenarios
from bajutsu.record.loop import record as record_loop


def _window(w: float = 402.0, h: float = 874.0) -> base.Element:
    return {
        "identifier": None,
        "label": "App",
        "traits": ["application"],
        "value": None,
        "frame": (0.0, 0.0, w, h),
        "nativeZ": None,
    }


class StubLocator:
    def __init__(self, decision: AlertDecision) -> None:
        self.decision = decision
        self.seen: list[tuple[bytes, str | None]] = []

    def locate(self, screenshot_png: bytes, instruction: str | None) -> AlertDecision:
        self.seen.append((screenshot_png, instruction))
        return self.decision


# --- SystemAlertGuard ---


def test_guard_taps_normalized_point_when_present() -> None:
    driver = ShotDriver([_window(402.0, 874.0)])
    guard = SystemAlertGuard(
        StubLocator(AlertDecision(present=True, x=0.5, y=0.25, label="Not Now"))
    )
    # The dismissal returns an AlertEvent carrying the tapped button (for the report).
    assert guard.dismiss(driver) == AlertEvent(label="Not Now")
    # normalized (0.5, 0.25) maps to point space (402, 874)
    assert ("tap_point", (201.0, 218.5)) in driver.actions
    assert any(a[0] == "screenshot" for a in driver.actions)


def test_guard_noop_when_absent() -> None:
    driver = ShotDriver([_window()])
    assert SystemAlertGuard(StubLocator(AlertDecision(present=False))).dismiss(driver) is None
    assert not any(a[0] == "tap_point" for a in driver.actions)


def test_guard_passes_instruction_to_locator() -> None:
    driver = ShotDriver([_window()])
    locator = StubLocator(AlertDecision(present=False))
    SystemAlertGuard(locator, instruction="tap Save").dismiss(driver)
    assert locator.seen[0][1] == "tap Save"


class _RaisingLocator:
    """A locator that fails (e.g. no API key / a transient API error)."""

    def locate(self, screenshot_png: bytes, instruction: str | None) -> AlertDecision:
        raise RuntimeError("no ANTHROPIC_API_KEY")


def test_guard_is_best_effort_when_locator_raises() -> None:
    # The guard is on by default, so a failing locator must no-op (return None), never crash a run.
    driver = ShotDriver([_window()])
    assert SystemAlertGuard(_RaisingLocator()).dismiss(driver) is None
    assert not any(a[0] == "tap_point" for a in driver.actions)


def test_guard_noop_when_no_screenshot() -> None:
    driver = FakeDriver([_window()])  # base FakeDriver writes no bytes -> no screenshot
    locator = StubLocator(AlertDecision(present=True, x=0.5, y=0.5))
    assert SystemAlertGuard(locator).dismiss(driver) is None
    assert locator.seen == []  # never reached the locator


# --- ClaudeAlertLocator (fake AI backend, BE-0104) ---


def _resolve_alert(inp: dict[str, object]) -> FakeBackend:
    """A fake backend whose single tool call is the alert locator's `resolve_alert`."""
    return FakeBackend(FakeBlock("resolve_alert", dict(inp)))


def _png(width: int, height: int) -> bytes:
    """A minimal PNG whose IHDR advertises the given pixel size (enough for png_size)."""
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr


def test_locator_normalizes_pixel_coordinates() -> None:
    backend = _resolve_alert({"present": True, "x": 374, "y": 1611, "label": "Not Now"})
    decision = ClaudeAlertLocator(backend=backend).locate(_png(1206, 2622), "tap Save")
    assert decision.present is True and decision.label == "Not Now"
    assert abs(decision.x - 374 / 1206) < 1e-6
    assert abs(decision.y - 1611 / 2622) < 1e-6
    request = backend.requests[0]
    assert isinstance(request.tool_choice, AnyTool)
    content = request.messages[0].content
    assert any(isinstance(c, ImagePart) for c in content)
    text = next(c.text for c in content if isinstance(c, TextPart))
    assert "1206x2622" in text and "tap Save" in text


def test_locator_absent_decision() -> None:
    decision = ClaudeAlertLocator(backend=_resolve_alert({"present": False})).locate(
        _png(10, 10), None
    )
    assert decision.present is False


def test_locator_redacts_instruction_before_send() -> None:
    # BE-0047: the (possibly user-supplied) --alert-vision-instruction is redacted before it reaches the
    # model. The screenshot beside it is sent as-is — images cannot be pixel-masked.
    from bajutsu.common.evidence.redaction import Redactor
    from bajutsu.common.scenario import Redact

    backend = _resolve_alert({"present": False})
    redactor = Redactor(Redact(), values=["sk-secret-token"])
    ClaudeAlertLocator(backend=backend, redactor=redactor).locate(
        _png(10, 10), "tap Save then enter sk-secret-token"
    )
    text = next(c.text for c in backend.requests[0].messages[0].content if isinstance(c, TextPart))
    assert "sk-secret-token" not in text
    assert "[REDACTED]" in text


# --- orchestrator on_blocked retry ---

_TAP_GO = "- name: t\n  steps:\n    - tap: { id: go }\n"


def test_on_blocked_retries_step_after_recovery() -> None:
    target: base.Element = {
        "identifier": "go",
        "label": "Go",
        "traits": ["button"],
        "value": None,
        "frame": (0.0, 0.0, 10.0, 10.0),
        "nativeZ": None,
    }
    # Empty screen -> the tap fails to resolve, and a SpringBoard prompt is what is covering it.
    driver = AlertingDriver(on_dismiss=lambda d: setattr(d, "screen", [target]))

    result = run_scenario(
        driver,
        load_scenarios(_TAP_GO)[0],
        alert_guard=AlertGuardConfig(rules=[guard_rule()]),
    )
    assert result.ok is True
    assert ("tap", {"id": "go"}) in driver.actions
    # The dismissal is recorded on the retried step's outcome (for the report).
    assert result.steps[0].alerts == [AlertEvent(label=GUARD_LABEL)]


def test_the_end_of_step_retry_waits_for_the_uncovered_screen_to_settle() -> None:
    """BE-0406: a prompt that was tapped is not yet a prompt that is gone.

    The retry actuates, so running it against a tree still animating the sheet away lets the step
    fail for a reason that is not its own. This driver renders the target only on the third read
    after the dismissal — a screen that is still moving — so the retry passes only if something
    waited for it to hold still first.
    """
    target: base.Element = {
        "identifier": "go",
        "label": "Go",
        "traits": ["button"],
        "value": None,
        "frame": (0.0, 0.0, 10.0, 10.0),
        "nativeZ": None,
    }
    other: base.Element = {**target, "identifier": "mid", "label": "Mid"}

    class SlowReveal(AlertingDriver):
        """Two more frames of motion after the dismissal, then the target."""

        def __init__(self) -> None:
            super().__init__(on_dismiss=lambda d: setattr(d, "reads_left", 2))
            self.reads_left = -1

        def query(self) -> list[base.Element]:
            if self.reads_left > 0:
                self.reads_left -= 1
                # A different tree each read, so no pair of reads can count as settled yet.
                return [{**other, "label": f"Mid {self.reads_left}"}]
            if self.reads_left == 0:
                self.screen = [target]
            return super().query()

    driver = SlowReveal()
    result = run_scenario(
        driver,
        load_scenarios(_TAP_GO)[0],
        alert_guard=AlertGuardConfig(rules=[guard_rule()]),
    )
    assert result.ok is True
    assert ("tap", {"id": "go"}) in driver.actions


def test_end_of_step_alert_guard_retry_preserves_correct_before_after_evidence(
    tmp_path: Path,
) -> None:
    """The end-of-step alert-guard retry must not corrupt the report's evidence (BE-0341
    non-regression): every step's recorded tree is the settled post-action one, so neither the
    retried step nor the one after it is left describing the screen the failed first attempt
    found."""
    go: base.Element = {
        "identifier": "go",
        "label": "Go",
        "traits": ["button"],
        "value": None,
        "frame": (0.0, 0.0, 10.0, 10.0),
        "nativeZ": None,
    }
    nxt: base.Element = {
        "identifier": "next",
        "label": "Next",
        "traits": ["button"],
        "value": None,
        "frame": (0.0, 0.0, 10.0, 10.0),
        "nativeZ": None,
    }
    # Empty screen -> the first tap fails to resolve; answering the prompt reveals both targets.
    driver = AlertingDriver(on_dismiss=lambda d: setattr(d, "screen", [go, nxt]))

    yaml = "- name: t\n  steps:\n    - tap: { id: go }\n    - tap: { id: next }\n"
    run_dir = tmp_path / "run1"
    result = run_scenario(
        driver,
        load_scenarios(yaml)[0],
        alert_guard=AlertGuardConfig(rules=[guard_rule()]),
        sink=FileSink(run_dir),
    )
    assert result.ok is True, result.failure

    def _els(step_index: int) -> list[dict[str, object]]:
        art = next(a for a in result.steps[step_index].artifacts if a.kind == "elements")
        els = json.loads((run_dir / art.name).read_text(encoding="utf-8"))
        assert isinstance(els, list)
        return els

    # step0's tree is its post-action one: the screen the retried tap actually left behind, with
    # both targets present — never the empty screen its failed first attempt found.
    assert {e["identifier"] for e in _els(0)} == {"go", "next"}
    # step1's own tree likewise reflects the post-retry, settled state.
    assert {e["identifier"] for e in _els(1)} == {"go", "next"}
    # The pre-attempt screen is not lost: step0 still records its own pre-action `before.png`.
    assert any(a.name.endswith("before.png") for a in result.steps[0].artifacts)


def test_end_of_step_alert_guard_retry_on_the_last_step_still_gets_a_final_capture(
    tmp_path: Path,
) -> None:
    """The scenario's last step still gets its post-action screenshot after an end-of-step
    alert-guard retry — reflecting the retried, successful attempt, not the failed first one — and
    its `elements.json` describes that same settled screen (BE-0341)."""
    go: base.Element = {
        "identifier": "go",
        "label": "Go",
        "traits": ["button"],
        "value": None,
        "frame": (0.0, 0.0, 10.0, 10.0),
        "nativeZ": None,
    }
    # Empty screen -> the tap fails to resolve until the prompt covering it is answered.
    driver = AlertingDriver(on_dismiss=lambda d: setattr(d, "screen", [go]))

    run_dir = tmp_path / "run1"
    result = run_scenario(
        driver,
        load_scenarios(_TAP_GO)[0],
        alert_guard=AlertGuardConfig(rules=[guard_rule()]),
        sink=FileSink(run_dir),
    )
    assert result.ok is True, result.failure
    names = {a.name for a in result.steps[0].artifacts}
    # The step gets both halves of its evidence, even though it took a retry to get there.
    assert any(name.endswith("before.png") for name in names)
    assert any(name.endswith("after.png") for name in names)
    els = json.loads(
        (
            run_dir / next(a.name for a in result.steps[0].artifacts if a.kind == "elements")
        ).read_text(encoding="utf-8")
    )
    # elements.json is the post-action tree, paired with the `after.png` beside it: the retried
    # tap's settled screen, not the empty one the failed first attempt found.
    assert [e["identifier"] for e in els] == ["go"]


def test_failure_stands_without_handler() -> None:
    driver = FakeDriver([])
    result = run_scenario(driver, load_scenarios(_TAP_GO)[0])
    assert result.ok is False


def _el(identifier: str) -> base.Element:
    return {
        "identifier": identifier,
        "label": identifier,
        "traits": ["staticText"],
        "value": None,
        "frame": (0.0, 0.0, 10.0, 10.0),
        "nativeZ": None,
    }


def test_on_blocked_retries_expect_after_recovery() -> None:
    # A system alert can cover the screen exactly when expect runs; the guard must
    # clear it there too, not only during steps.
    here, later = _el("here"), _el("later")
    # 'later' is missing until the alert is dismissed.
    driver = AlertingDriver([here], label="Allow", on_dismiss=lambda d: d.screen.append(later))

    yaml = (
        "- name: e\n"
        "  steps:\n    - wait: { for: { id: here }, timeout: 1 }\n"
        "  expect:\n    - exists: { id: later }\n"
    )
    result = run_scenario(
        driver, load_scenarios(yaml)[0], alert_guard=AlertGuardConfig(rules=[guard_rule("Allow")])
    )
    assert result.ok is True
    # The expect-phase dismissal is recorded on the run result (not on any step).
    assert result.expect_alerts == [AlertEvent(label="Allow")]


# --- undeclared interruptions (BE-0406 Unit 2b) ---


def test_undeclared_interruption_fails_a_step_that_otherwise_passed() -> None:
    # XCUITest answers an alert that interrupted this step's own interaction with its own default
    # button regardless of anything here — nothing changes that tap — but the step that met it must
    # fail by name, naming the buttons, rather than read as an ordinary pass.
    target: base.Element = {
        "identifier": "go",
        "label": "Go",
        "traits": ["button"],
        "value": None,
        "frame": (0.0, 0.0, 10.0, 10.0),
        "nativeZ": None,
    }
    driver = FakeDriver([target])
    driver.interruptions_declined_to_drain = [["Allow", "Don't Allow"]]

    result = run_scenario(driver, load_scenarios(_TAP_GO)[0])
    assert ("tap", {"id": "go"}) in driver.actions  # the tap itself succeeded
    assert result.ok is False
    assert result.steps[0].ok is False
    assert "undeclared system alert" in result.steps[0].reason
    assert "Allow" in result.steps[0].reason
    assert "Don't Allow" in result.steps[0].reason


def test_a_notification_banner_lands_on_the_step_it_interrupted_without_failing_it() -> None:
    # The whole point of BE-0416: a banner the runner swiped away during a step is reported on that
    # step — so a reader can see what was on screen — while the step itself passes. Before it, the
    # same banner arrived through the undeclared path and failed this step outright.
    target: base.Element = {
        "identifier": "go",
        "label": "Go",
        "traits": ["button"],
        "value": None,
        "frame": (0.0, 0.0, 10.0, 10.0),
        "nativeZ": None,
    }
    driver = FakeDriver([target])
    driver.banners_to_drain = ["Ready for Apple Intelligence"]

    result = run_scenario(driver, load_scenarios(_TAP_GO)[0])
    assert result.ok is True
    assert result.steps[0].ok is True
    assert result.steps[0].alerts == [
        AlertEvent(label="Ready for Apple Intelligence", kind="notificationBanner")
    ]


def test_undeclared_interruptions_are_appended_to_a_step_s_own_failure_and_all_named() -> None:
    # A step that already failed for its own reason keeps that reason: the alert note is appended,
    # not substituted, so a wait's own timeout detail is not lost alongside the alert that caused it.
    # And every interruption drained this poll is named, not only the first, so an author does not
    # pay a whole extra run to learn about the second one.
    driver = FakeDriver([])  # "go" never appears, so the tap fails to resolve
    driver.interruptions_declined_to_drain = [["Allow", "Don't Allow"], ["Save", "Not Now"]]

    result = run_scenario(driver, load_scenarios(_TAP_GO)[0])
    assert result.ok is False
    reason = result.steps[0].reason
    assert "一致なし" in reason  # the original "no match" detail survives
    assert "undeclared system alert" in reason
    assert "Allow" in reason
    assert "Don't Allow" in reason
    assert "Save" in reason
    assert "Not Now" in reason


_EXPECT_ONLY = "- name: e\n  steps: []\n  expect:\n    - exists: { id: here }\n"


def test_undeclared_interruption_fails_expect_that_otherwise_passed() -> None:
    driver = FakeDriver([_el("here")])
    driver.interruptions_declined_to_drain = [["Save", "Not Now"]]

    result = run_scenario(driver, load_scenarios(_EXPECT_ONLY)[0])
    assert result.expect_results and all(r.ok for r in result.expect_results)  # the assertion held
    assert result.ok is False
    assert result.failure is not None
    assert "undeclared system alert" in result.failure
    assert "Save" in result.failure
    assert "Not Now" in result.failure


def test_undeclared_interruption_during_a_declining_probe_still_fails_expect() -> None:
    # The guard's own probe (`system_alert_labels`, inside `probe_native`) can be interrupted too,
    # even on a poll where the guard clears nothing (`event is None`) — the case the retry-drain
    # used to miss, because it lived inside the "the guard cleared something" branch alone
    # (BE-0406 Unit 2b). The original assertion detail must also survive alongside the alert note,
    # appended rather than replaced.
    class _DeclinesDuringProbe(FakeDriver):
        def system_alert_labels(self) -> list[str]:
            self.interruptions_declined_to_drain = [["Save", "Not Now"]]
            return super().system_alert_labels()

    driver = _DeclinesDuringProbe([_el("here")])
    yaml = "- name: e\n  steps: []\n  expect:\n    - exists: { id: later }\n"
    result = run_scenario(driver, load_scenarios(yaml)[0], alert_guard=AlertGuardConfig())
    assert result.ok is False
    assert result.failure is not None
    assert (
        "expected present but was absent" in result.failure
    )  # the original assertion detail survives
    assert "undeclared system alert" in result.failure
    assert "Save" in result.failure
    assert "Not Now" in result.failure


def test_undeclared_interruption_during_the_expect_retry_still_fails_it() -> None:
    # The retry's own queries can be interrupted too, distinct from the matched dismissal that
    # triggered the retry in the first place. Without the second expect-phase drain this seeds, an
    # interruption here would go unreported — nothing else drains this phase after the retry.
    here, later = _el("here"), _el("later")

    def on_dismiss(d: AlertingDriver) -> None:
        d.screen.append(later)
        d.interruptions_declined_to_drain = [["Save", "Not Now"]]

    driver = AlertingDriver([here], label="Allow", on_dismiss=on_dismiss)

    yaml = (
        "- name: e\n"
        "  steps:\n    - wait: { for: { id: here }, timeout: 1 }\n"
        "  expect:\n    - exists: { id: later }\n"
    )
    result = run_scenario(
        driver, load_scenarios(yaml)[0], alert_guard=AlertGuardConfig(rules=[guard_rule("Allow")])
    )
    assert result.expect_results and all(r.ok for r in result.expect_results)  # the assertion held
    assert result.expect_alerts == [
        AlertEvent(label="Allow")
    ]  # the matched dismissal still reports
    assert result.ok is False
    assert result.failure is not None
    assert "undeclared system alert" in result.failure
    assert "Save" in result.failure
    assert "Not Now" in result.failure


# --- record loop + alert guard ---


class _FastClock:
    def now(self) -> float:
        return 0.0

    def sleep(self, seconds: float) -> None:
        pass


class _ScriptAgent:
    def __init__(self, *proposals: Proposal) -> None:
        self._proposals = proposals
        self._i = 0

    def next_action(self, observation: object) -> Proposal:
        proposal = self._proposals[min(self._i, len(self._proposals) - 1)]
        self._i += 1
        return proposal

    def plan(self, goal: str) -> list[str]:
        return []


def test_record_guard_clears_blocking_before_agent_acts() -> None:
    driver = FakeDriver([_window()])  # blocked: a prompt collapsed the tree
    calls = {"n": 0}

    def guard(d: base.Driver) -> AlertEvent | None:
        calls["n"] += 1
        assert isinstance(d, FakeDriver)
        d.screen = [_el("go")]  # dismissing the prompt reveals the app
        return AlertEvent(label="Not Now")

    agent = _ScriptAgent(
        Proposal(steps=[Step.model_validate({"tap": {"id": "go"}})]),
        Proposal(done=True, expect=[]),
    )
    scenario = record_loop(
        driver, "reach go", agent, clock=_FastClock(), with_screenshot=False, alert_guard=guard
    )
    assert calls["n"] >= 1
    assert scenario.steps and scenario.steps[0].tap is not None
    assert scenario.steps[0].tap.id == "go"


def test_record_guard_not_called_when_app_is_visible() -> None:
    driver = FakeDriver([_el("go")])  # actionable already; nothing blocking
    calls = {"n": 0}

    def guard(d: base.Driver) -> AlertEvent | None:
        calls["n"] += 1
        return None  # the guard looked but found nothing dismissible

    agent = _ScriptAgent(
        Proposal(steps=[Step.model_validate({"tap": {"id": "go"}})]),
        Proposal(done=True, expect=[]),
    )
    record_loop(
        driver, "reach go", agent, clock=_FastClock(), with_screenshot=False, alert_guard=guard
    )
    assert calls["n"] == 0
