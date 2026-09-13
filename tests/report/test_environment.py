"""Tests for the HTML report environment tab, device, alerts, and backend."""

from __future__ import annotations

from _report import _passing, _scenarios

from bajutsu.common.drivers.actuation import Actuation
from bajutsu.common.orchestrator import AlertEvent, RunResult, SkippedCapture, StepOutcome
from bajutsu.common.report import html_report, manifest_dict


def test_html_environment_tab_shows_simulator() -> None:
    # Each scenario gets an Environment tab (beside Result) naming the simulator it ran on:
    # device model, OS runtime, actuator, and udid.
    r = _passing()
    r.device, r.device_name, r.device_runtime = "SIM-ABC123", "iPhone 15", "iOS 17.2"
    out = html_report("run1", [r])
    assert 'data-tab="env"' in out and 'data-panel="env"' in out
    assert ">Environment</button>" in out  # the tab label
    assert '<span class="deflbl">simulator</span>' in out
    assert '<td class="pk">device</td><td>iPhone 15</td>' in out
    assert '<td class="pk">OS</td><td>iOS 17.2</td>' in out
    assert '<td class="pk">actuator</td><td>fake</td>' in out
    assert '<td class="pk">udid</td><td>SIM-ABC123</td>' in out
    # Result stays the active first tab; Environment is a sibling, not the default.
    assert out.index('data-tab="steps"') < out.index('data-tab="env"')


def test_html_preconditions_render_in_result_tab() -> None:
    # Preconditions render in the Result tab (the steps panel), ahead of the Environment tab.
    definition = {
        "name": "s1",
        "steps": [{"tap": {"id": "a"}}],
        "preconditions": {"locale": "ja_JP", "launchEnv": {"SAMPLE_SEED": "5"}},
    }
    out = html_report("run9", [_passing()], definitions=[definition])
    assert '<details class="pre"' in out
    assert '<td class="pk">locale</td><td>ja_JP</td>' in out
    assert '<td class="pk">SAMPLE_SEED</td><td>5</td>' in out
    # In the Result panel, which precedes the Environment panel.
    assert out.index('<details class="pre"') < out.index('data-panel="env"')


def test_html_shows_dismissed_system_alert() -> None:
    # A step that only passed after the guard cleared a system prompt shows the dismissal
    # as a sub-row, so a retried step is not silently rendered as "just passing".
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(
                index=0, action="tap", ok=True, started_at=0.0, alerts=[AlertEvent(label="Not Now")]
            )
        ],
        expect_results=[],
        artifacts=[],
    )
    out = html_report("run1", [r], definitions=[{"name": "s1", "steps": [{"tap": {"id": "a"}}]}])
    assert "class='alertrow'" in out
    assert 'act-alert">system alert' in out
    assert '<span class="tk str">“Not Now”</span>' in out
    # A step with no dismissal renders no alert row.
    assert "class='alertrow'" not in html_report("run1", [_passing()])


def test_html_tells_a_swiped_notification_banner_apart_from_a_dismissed_alert() -> None:
    # A banner carries no button, so its label is the notification's own text. Rendered under the
    # alert wording it would read as a system alert dismissed by pressing that text — which is why
    # the event carries a kind at all (BE-0416).
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(
                index=0,
                action="tap",
                ok=True,
                started_at=0.0,
                alerts=[
                    AlertEvent(label="Ready for Apple Intelligence", kind="notificationBanner")
                ],
            )
        ],
        expect_results=[],
        artifacts=[],
    )
    out = html_report("run1", [r], definitions=[{"name": "s1", "steps": [{"tap": {"id": "a"}}]}])
    assert 'act-alert">notification banner</span> swiped away' in out
    assert 'act-alert">system alert' not in out
    assert '<span class="tk str">“Ready for Apple Intelligence”</span>' in out


def test_html_shows_expect_phase_dismissed_alert() -> None:
    # A prompt cleared right before the scenario-level expect re-checked is noted under
    # the expectations table (it belongs to no single step).
    r = _passing()  # carries an evaluated expectation
    r.expect_alerts = [AlertEvent(label="Allow")]
    out = html_report("run1", [r])
    assert 'class="alertnote"' in out
    assert 'act-alert">system alert' in out
    assert "dismissed before re-checking" in out
    assert '<span class="tk str">“Allow”</span>' in out
    # No expect-phase dismissal -> no note.
    assert 'class="alertnote"' not in html_report("run1", [_passing()])


def test_html_tells_a_swiped_banner_apart_from_a_dismissed_alert_in_the_expect_phase_note() -> None:
    # The step-row and expect-phase notes share one macro (`alertline`); this pins the second call
    # site independently, so the two cannot silently diverge on a later edit.
    r = _passing()
    r.expect_alerts = [AlertEvent(label="Ready for Apple Intelligence", kind="notificationBanner")]
    out = html_report("run1", [r])
    assert 'act-alert">notification banner</span> swiped away before re-checking' in out
    assert 'act-alert">system alert' not in out
    assert '<span class="tk str">“Ready for Apple Intelligence”</span>' in out


def test_html_shows_the_value_a_generate_step_produced() -> None:
    # BE-0377: the run's record shows which value this run used, so a later failure is traceable to
    # the value rather than needing a fixed seed to reproduce.
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(index=0, action="generate", ok=True, started_at=0.0, generated="k7fq2xzp")
        ],
        expect_results=[],
        artifacts=[],
    )
    definition = {
        "name": "s1",
        "steps": [{"generate": {"random": {"string": {"length": 8}}, "into": {"var": "username"}}}],
    }
    out = html_report("run1", [r], definitions=[definition])
    assert "class='genrow'" in out
    assert '<span class="tk str">“k7fq2xzp”</span>' in out
    # Every other action produces no value, so no row.
    assert "class='genrow'" not in html_report("run1", [_passing()])
    # A planned-but-not-run step's row carries no `generated` key at all, and Jinja's Undefined
    # passes an `is not none` guard — so the row must be gated on truthiness, not on None.
    skipped = RunResult(
        scenario="s2",
        ok=False,
        steps=[StepOutcome(index=0, action="tap", ok=False, reason="not found", started_at=0.0)],
        expect_results=[],
        artifacts=[],
    )
    two_planned = {"name": "s2", "steps": [{"tap": {"id": "a"}}, {"tap": {"id": "b"}}]}
    assert "class='genrow'" not in html_report("run2", [skipped], definitions=[two_planned])


def test_html_shows_what_the_step_actuated() -> None:
    # The report's answer to "where did this tap land": the coordinate the driver sent, the element it
    # resolved, and the channel that carried it, as a sub-row under the step.
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(
                index=0,
                action="tap",
                ok=True,
                started_at=0.0,
                actuations=[
                    Actuation(
                        gesture="tap",
                        via="coordinate",
                        unit="pixel",
                        points=((100.0, 150.0),),
                        frame=(0.0, 100.0, 200.0, 100.0),
                        target="stable.submit",
                    )
                ],
            )
        ],
        expect_results=[],
        artifacts=[],
    )
    out = html_report("run1", [r], definitions=[{"name": "s1", "steps": [{"tap": {"id": "a"}}]}])
    assert "class='actrow'" in out
    assert "(100, 150)" in out
    assert 'class="actn-v">via coordinate' in out
    assert '<span class="tk id">stable.submit</span>' in out


def test_html_shows_a_handle_actuation_by_its_frame() -> None:
    # A record with no host coordinate still says *where*: the frame the far side resolved from.
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(
                index=0,
                action="tap",
                ok=True,
                started_at=0.0,
                actuations=[
                    Actuation(
                        gesture="tap",
                        via="handle",
                        unit="point",
                        frame=(4.0, 8.0, 20.0, 12.0),
                        target="ok",
                    )
                ],
            )
        ],
        expect_results=[],
        artifacts=[],
    )
    out = html_report("run1", [r], definitions=[{"name": "s1", "steps": [{"tap": {"id": "a"}}]}])
    assert "[4, 8 20×12]" in out
    assert 'class="actn-v">via handle' in out


def test_html_shows_an_expect_phase_actuation_under_the_expectations() -> None:
    # The guard's dismissing tap during the scenario-level re-check belongs to no step, so it is noted
    # under the expectations table beside its alert — recorded and shown, never dropped.
    r = _passing()
    r.expect_actuations = [
        Actuation(gesture="systemAlert", via="handle", unit="point", target="alert.ok")
    ]
    out = html_report("run1", [r])
    assert 'class="actnote"' in out
    assert '<span class="tk id">alert.ok</span>' in out
    assert 'class="actnote"' not in html_report("run1", [_passing()])


def test_device_shown_in_manifest_and_report() -> None:
    # A parallel run records the device per scenario; the report shows a per-scenario badge
    # and a header "N devices" chip when the work was split across simulators.
    a, b = _passing(), _passing()
    a.device, b.device = "SIM-AAAA", "SIM-BBBB"
    m = manifest_dict("run1", [a, b])
    assert [s["device"] for s in _scenarios(m)] == ["SIM-AAAA", "SIM-BBBB"]
    out = html_report("run1", [a, b])
    assert 'class="dev"' in out and "ran on simulator SIM-AAAA" in out  # per-scenario badge
    assert "2 devices" in out  # header summary chip when split across devices
    # A single-device run omits the header devices chip.
    one = _passing()
    one.device = "SIM-AAAA"
    assert "devices</span>" not in html_report("run1", [one])


def test_html_environment_shows_skipped_captures() -> None:
    r = _passing()
    r.skipped_captures = [
        SkippedCapture(kind="network", reason="no same-platform backend provides network")
    ]
    out = html_report("run1", [r])
    assert "skipped evidence" in out
    assert "network" in out
    assert "no same-platform backend provides network" in out


def test_html_environment_omits_skipped_section_when_empty() -> None:
    r = _passing()
    assert not r.skipped_captures
    out = html_report("run1", [r])
    assert "skipped evidence" not in out


def test_html_environment_no_sim_with_skips_omits_muted_message() -> None:
    # When sim details are absent but skipped captures exist, the panel must NOT
    # show "no environment details captured" — that would contradict the skip rows.
    r = _passing()
    r.device = ""
    r.device_name = ""
    r.device_runtime = ""
    r.backend = ""
    r.skipped_captures = [
        SkippedCapture(kind="network", reason="no same-platform backend provides network")
    ]
    out = html_report("run1", [r])
    assert "no environment details captured" not in out
    assert "skipped evidence" in out


def test_html_shows_backend() -> None:
    # The actuator is shown both as a header chip and a per-scenario badge.
    out = html_report("run9", [_passing()])
    assert "driver: fake" in out
    assert '<span class="drv"' in out  # per-scenario row badge
    # The header chip uses a dedicated class (not the `.drv` row badge) so it sets
    # its own white text (not blue-on-blue) and centers in the flex row.
    assert '<span class="chip dchip"' in out
    assert ".chip.dchip{background:#2c5fb3;color:#fff" in out
    assert "align-items:center" in out and "display:inline-flex;align-items:center" in out
