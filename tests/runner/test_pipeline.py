"""Tests for run_all / run_and_report (scenarios + leases -> results + report artifacts)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from _runner import _eff, _el, _failing_lease, _fake_driver, _ios_eff, _lease
from conftest import AlertingDriver, guard_rule

from bajutsu.common.backend_cli import simctl
from bajutsu.common.config import Effective, XcuitestConfig
from bajutsu.common.doctor import Score
from bajutsu.common.drivers import base, tracing
from bajutsu.common.drivers.fake import FakeDriver
from bajutsu.common.evidence import NullSink
from bajutsu.common.evidence.network import NetworkExchange, ScreenTransition
from bajutsu.common.orchestrator import RunResult, sanitize_source_stem, scenario_slug
from bajutsu.common.orchestrator.types._functions import _MAX_SLUG_CHARS
from bajutsu.common.report.format import video_seconds
from bajutsu.common.runner import Lease, run_all, run_and_report, run_matrix_and_report
from bajutsu.common.scenario import Scenario


def test_run_all() -> None:
    scenarios = [
        Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]}),
        Scenario.model_validate({"name": "b", "steps": [{"tap": {"id": "missing"}}]}),
    ]
    results = run_all(_eff(), scenarios, _lease)
    assert [r.ok for r in results] == [True, False]


def test_on_score_emits_the_entry_screen_grade_once() -> None:
    # `run --score`: the app's entry screen is scored once per run — from the first scenario's freshly
    # launched driver — so CI reads doctor's Ready/Partial/Blocked tell without a second cold spawn.
    scenarios = [
        Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]}),
        Scenario.model_validate({"name": "b", "steps": [{"tap": {"id": "ok"}}]}),
    ]
    scores: list[Score] = []
    run_all(_eff(), scenarios, _lease, workers=2, on_score=scores.append)
    # Exactly once for the whole run (index 0), even with parallel workers; `_fake_driver`'s single
    # id-carrying button grades Ready.
    assert len(scores) == 1
    assert scores[0].grade == "Ready"


def test_on_score_defaults_to_not_scoring() -> None:
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    scored = False

    def _sink(_s: Score) -> None:
        nonlocal scored
        scored = True

    # The default call (no on_score) is unchanged; passing the sink is what opts in.
    run_all(_eff(), scenarios, _lease)
    assert scored is False
    run_all(_eff(), scenarios, _lease, on_score=_sink)
    assert scored is True


def test_on_score_failure_never_breaks_the_run() -> None:
    # The score is diagnostic and off the verdict path (prime directive 1): a sink that raises — or a
    # query fault behind it — is swallowed, so the scenario's own machine verdict still stands.
    def boom(_s: Score) -> None:
        raise RuntimeError("score sink blew up")

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_eff(), scenarios, _lease, on_score=boom)
    assert results[0].ok


def _crashing_driver() -> base.Driver:
    """A driver whose `tap` raises `BackendCrashError` — models a mid-run resident-runner crash
    (the real failure surfaced as `POST /tap failed: … crashed mid-run`)."""

    class _Crashing(FakeDriver):
        def tap(self, sel: base.Selector) -> None:
            raise base.BackendCrashError("runner crashed mid-run (test)")

    return _Crashing([_el("ok", "OK", ["button"])])


def _crash_then_ok_lease() -> tuple[Callable[[Effective, Scenario], Lease], list[str]]:
    """A lease factory whose first lease crashes and the rest pass, recording each lease's release."""
    state = {"n": 0}
    events: list[str] = []

    def _release(n: int) -> Callable[[], None]:
        def release() -> None:
            events.append(f"release-{n}")

        return release

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        n = state["n"]
        driver = _crashing_driver() if n == 1 else _fake_driver()
        return Lease(
            driver=driver,
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=_release(n),
        )

    return lease, events


def test_run_all_recovers_a_scenario_whose_backend_crashed() -> None:
    # A mid-scenario backend crash (BackendCrashError) is infrastructure, not a verdict: the pipeline
    # discards the dead lease, leases a fresh one (a cold respawn), and re-runs the scenario. Here the
    # first lease crashes and the second passes, so the scenario passes — with both leases released.
    lease, events = _crash_then_ok_lease()
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_eff(), scenarios, lease)
    assert [r.ok for r in results] == [True]  # recovered on the fresh-lease retry
    assert events == ["release-1", "release-2"]  # the dead lease and the live one both released


def test_on_score_emits_once_even_when_scenario_zero_crashes_and_recovers() -> None:
    # The score is a once-per-run tell, not per-attempt. When scenario 0 crashes mid-run and recovers
    # on a respawned app (BE-0049), `_run_on_lease` is re-entered and would re-score the fresh launch —
    # but the latch keeps the sink firing exactly once, so CI reads a single grade, not one per retry.
    # (The crash is in `tap`; scoring uses `query()`, so attempt 1 scores before the step crashes.)
    lease, _events = _crash_then_ok_lease()
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    scores: list[Score] = []
    results = run_all(_eff(), scenarios, lease, on_score=scores.append)
    assert results[0].ok  # recovered on the fresh-lease retry
    assert len(scores) == 1  # latched: the retry's fresh launch is not re-scored


def test_run_all_fails_a_scenario_that_crashes_every_attempt() -> None:
    # A scenario whose backend crashes on every attempt exhausts the retry budget and fails loudly —
    # flakiness is never absorbed into a pass (BE-0049). Exactly crash_retries + 1 attempts run.
    leases = 0

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        nonlocal leases
        leases += 1
        return Lease(
            driver=_crashing_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    messages: list[str] = []
    results = run_all(_eff(), scenarios, lease, crash_retries=2, progress=messages.append)
    assert not results[0].ok and leases == 3  # crash_retries=2 → 3 attempts, all crashed
    assert "crashed mid-run" in (results[0].failure or "")
    # The final, non-retried attempt must not claim a retry that never happens (would mislead an
    # operator watching progress into expecting a fourth attempt that the budget doesn't allow).
    assert "respawning" not in messages[-1]
    assert sum("respawning" in m for m in messages) == 2  # only the 2 attempts that did retry


def test_run_all_crash_retries_zero_disables_recovery() -> None:
    # crash_retries=0: a single attempt, no respawn — a crash fails the scenario at once.
    leases = 0

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        nonlocal leases
        leases += 1
        return Lease(
            driver=_crashing_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_eff(), scenarios, lease, crash_retries=0)
    assert not results[0].ok and leases == 1  # no retry


def test_crash_retries_default_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # The on-device lane raises the budget via BAJUTSU_CRASH_RETRIES without a code change; unset (or
    # invalid) keeps the default 1, and 0 disables recovery. An explicit run_all arg still wins.
    from bajutsu.common.runner.pipeline import _default_crash_retries

    monkeypatch.delenv("BAJUTSU_CRASH_RETRIES", raising=False)
    assert _default_crash_retries() == 1  # unset -> the pre-knob default
    monkeypatch.setenv("BAJUTSU_CRASH_RETRIES", "2")
    assert _default_crash_retries() == 2
    monkeypatch.setenv("BAJUTSU_CRASH_RETRIES", "0")
    assert _default_crash_retries() == 0  # explicit opt-out of recovery
    monkeypatch.setenv("BAJUTSU_CRASH_RETRIES", "not-a-number")
    assert _default_crash_retries() == 1  # invalid -> the default, never a crash


def test_run_all_honors_the_crash_retries_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End to end: with the env raised to 2 (three attempts) and no explicit arg, a lease that crashes
    # at bring-up on every attempt is leased exactly three times before failing loudly.
    monkeypatch.setenv("BAJUTSU_CRASH_RETRIES", "2")
    leases = 0

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        nonlocal leases
        leases += 1
        raise base.BackendCrashError("runner crashed during the readiness gate (test)")

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_eff(), scenarios, lease)  # crash_retries unset -> reads the env (2)
    assert not results[0].ok and leases == 3  # env budget 2 -> three attempts


def test_run_all_forces_erase_on_a_crash_triggered_retry() -> None:
    # A crash-triggered retry now forces the same `erase` precondition a scenario already gets by
    # declaring `erase: true`, instead of a bare in-place respawn onto the very device that just
    # crashed it — the first (crashing) attempt sees the scenario's own precondition unmodified, the
    # second (retried) attempt sees `erase` forced on.
    state = {"n": 0}
    erase_seen: list[bool | None] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        erase_seen.append(scenario.preconditions.erase)
        if state["n"] == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_eff(), scenarios, lease)
    assert results[0].ok
    assert erase_seen == [None, True]  # attempt 1 unmodified, attempt 2 forces erase


def test_run_all_forces_erase_on_a_crash_triggered_retry_against_an_xcuitest_simulator() -> None:
    # The production route this unit was written for: XCUITest against a Simulator (no
    # `xcuitest.deviceType: device`, no live WebDriver udid spec). The test above passes
    # `actuator=None`, which short-circuits `erase_precondition_supported` at its very first branch
    # (`actuator != "xcuitest" -> True`) without ever exercising `xcuitest_targets_real_device` /
    # `is_webdriver_endpoint` — so it proves nothing about the iOS lane specifically. This pins the
    # positive half of that guard on the one route the whole item exists for.
    state = {"n": 0}
    erase_seen: list[bool | None] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        erase_seen.append(scenario.preconditions.erase)
        if state["n"] == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_ios_eff(), scenarios, lease, actuator="xcuitest")
    assert results[0].ok
    assert erase_seen == [None, True]  # attempt 1 unmodified, attempt 2 forces erase


def test_run_all_degrades_to_a_bare_respawn_when_the_forced_erase_lease_itself_fails() -> None:
    # A forced-erase retry's own lease can fail with a device-level fault (`simctl.DeviceError` /
    # `adb.DeviceError`, e.g. the Simulator rejected `erase`/`shutdown`/`boot`) rather than a
    # `BackendCrashError`. Uncaught, that would escape run_one's own `except BackendCrashError` and
    # abort the whole run past `run_all` — losing every already-passed scenario's verdict, worse than
    # the bare in-place respawn this forced retry replaces. It must instead degrade to that bare
    # respawn, exactly like a scenario that never forced erase at all.
    state = {"n": 0}
    erase_seen: list[bool | None] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        erase_seen.append(scenario.preconditions.erase)
        if state["n"] == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        if state["n"] == 2:
            assert scenario.preconditions.erase is True  # the forced-erase attempt
            raise simctl.DeviceError("simctl erase failed (test)")
        assert scenario.preconditions.erase is None  # degraded to a bare respawn, erase not forced
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_ios_eff(), scenarios, lease, actuator="xcuitest", crash_retries=2)
    assert results[0].ok
    assert erase_seen == [None, True, None]
    assert state["n"] == 3


# --- escalating a crash retry to a replacement device (BE-0354) --- #
#
# The rung above the forced erase. An erase resets the device's data and was measured not to clear a
# Simulator whose capture services had wedged — the erased device came back wedged — so a retry that
# already spent that remedy, or one whose video never confirmed it started, asks the lease's
# environment for a device that has never run anything and drops the erase it would otherwise force.


def _replaceable_eff() -> Effective:
    """An unpinned Simulator XCUITest target with an `appPath` — the one route that can be replaced."""
    return _ios_eff(app_path="/nonexistent/App.app")


def _escalating_lease(
    *, crashes: int, stalled: bool = False
) -> tuple[Callable[[Effective, Scenario], Lease], list[bool | None], list[int]]:
    """A lease factory whose first `crashes` attempts crash mid-scenario, recording what each asked for.

    Returns the factory, the `preconditions.erase` each attempt was leased with, and the attempt
    numbers that asked their environment for a replacement device.
    """
    state = {"n": 0}
    erase_seen: list[bool | None] = []
    replacements: list[int] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        n = state["n"]
        erase_seen.append(scenario.preconditions.erase)
        return Lease(
            driver=_crashing_driver() if n <= crashes else _fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
            video_start_stalled=lambda: stalled,
            request_device_replacement=lambda n=n: replacements.append(n),  # type: ignore[misc]
        )

    return lease, erase_seen, replacements


def test_a_forced_erase_retry_that_crashes_again_escalates_to_a_replacement_device() -> None:
    # The measured occurrence: the forced-erase retry reproduced the first attempt to the letter. The
    # attempt after it asks for a device that has never run anything. The erase it also carries is
    # dropped by the environment that serves the swap, not here — deciding it here would mean
    # predicting that environment, and an attempt whose request never landed would get neither remedy.
    lease, erase_seen, replacements = _escalating_lease(crashes=2)
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_replaceable_eff(), scenarios, lease, actuator="xcuitest", crash_retries=2)
    assert results[0].ok
    assert erase_seen == [None, True, True]
    assert replacements == [2]  # only the forced-erase attempt's crash escalated


def test_a_scenario_asks_for_at_most_one_replacement_device() -> None:
    # A device that keeps crashing must not mint a `bajutsu-recovered-*` device per attempt: the
    # residue is per run, and a host that degrades a freshly created device is not one more device
    # away from working.
    lease, _erase_seen, replacements = _escalating_lease(crashes=3, stalled=True)
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_replaceable_eff(), scenarios, lease, actuator="xcuitest", crash_retries=3)
    assert results[0].ok
    assert replacements == [1]


def test_a_stalled_video_start_escalates_from_the_first_crash() -> None:
    # The video-start confirmation timing out identifies the capture-pipeline degradation an erase was
    # observed not to clear, so it selects the replacement rung directly rather than waiting for the
    # erase to be tried and fail first.
    lease, _erase_seen, replacements = _escalating_lease(crashes=1, stalled=True)
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_replaceable_eff(), scenarios, lease, actuator="xcuitest")
    assert results[0].ok
    assert replacements == [1]


def test_a_scenario_keeping_its_app_data_is_never_moved_to_a_blank_device() -> None:
    # `reinstall: overwrite` is the scenario's declaration that it needs its app's data container
    # across a lease, which is why the retry skips the forced erase. A replacement resets strictly
    # more — a blank device carries no app data at all — so the stall signal must not reach past that
    # opt-out and re-run the scenario against state it said it depends on.
    lease, erase_seen, replacements = _escalating_lease(crashes=1, stalled=True)
    scenarios = [
        Scenario.model_validate(
            {
                "name": "a",
                "preconditions": {"reinstall": "overwrite"},
                "steps": [{"tap": {"id": "ok"}}],
            }
        )
    ]
    results = run_all(_replaceable_eff(), scenarios, lease, actuator="xcuitest")
    assert results[0].ok
    assert replacements == []
    assert erase_seen == [None, None]  # a bare respawn, exactly as before this rung existed


def test_no_erase_also_opts_out_of_the_replacement_rung() -> None:
    # `bajutsu run --no-erase` is the operator asking for the device to be left as it is. Swapping it
    # out entirely is the opposite of that, so the stall signal must not reach past the flag either.
    lease, erase_seen, replacements = _escalating_lease(crashes=1, stalled=True)
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(
        _replaceable_eff(), scenarios, lease, actuator="xcuitest", force_erase_on_retry=False
    )
    assert results[0].ok
    assert replacements == []
    assert erase_seen == [None, None]


def test_a_target_with_no_app_path_keeps_the_erase_rung() -> None:
    # A replacement is a blank device, so a target with no `appPath` has nothing to install onto one.
    # Asking there would spend the attempt on a `DeviceError` that degrades to a bare respawn —
    # *below* the erase rung it displaced — so the route is excluded before anything is asked.
    lease, erase_seen, replacements = _escalating_lease(crashes=1, stalled=True)
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_ios_eff(app_path=None), scenarios, lease, actuator="xcuitest")
    assert results[0].ok
    assert replacements == []
    assert erase_seen == [None, True]


def test_a_lease_time_crash_keeps_the_erase_rung() -> None:
    # A crash during bring-up leaves no lease to reach the environment through, so there is nothing to
    # ask — the retry keeps the forced erase rather than silently dropping it for a request nobody got.
    state = {"n": 0}
    erase_seen: list[bool | None] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        erase_seen.append(scenario.preconditions.erase)
        if state["n"] < 3:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        return _lease(eff, scenario)

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_ios_eff(), scenarios, lease, actuator="xcuitest", crash_retries=2)
    assert results[0].ok
    assert erase_seen == [None, True, True]


def test_a_pinned_run_keeps_the_erase_rung_rather_than_replacing_the_named_device() -> None:
    # A replacement would silently move the run off the device the operator named with `--udid`, and
    # mint the per-run `bajutsu-recovered-*` residue BE-0344 documents for exactly the pinned case.
    lease, erase_seen, replacements = _escalating_lease(crashes=1, stalled=True)
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(
        _replaceable_eff(), scenarios, lease, actuator="xcuitest", lease_udid_spec="1234-ABCD"
    )
    assert results[0].ok
    assert replacements == []
    assert erase_seen == [None, True]  # the strongest rung this route keeps


def test_a_non_ios_run_never_requests_a_replacement() -> None:
    # Only the Simulator XCUITest lifecycle can mint a device; every other route ignores the request,
    # so the pipeline must not suppress the erase it would otherwise force.
    lease, erase_seen, replacements = _escalating_lease(crashes=1, stalled=True)
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_eff(), scenarios, lease)
    assert results[0].ok
    assert replacements == []
    assert erase_seen == [None, True]


def test_run_all_still_propagates_a_device_error_from_a_bare_lease() -> None:
    # A `DeviceError` from a lease that never forced erase (attempt 1, or the degraded bare respawn
    # above once *it* also fails) is unrelated to this item's forced-erase retry, so it keeps its
    # pre-existing behavior: it is not swallowed, it propagates out of run_all.
    def lease(eff: Effective, scenario: Scenario) -> Lease:
        raise simctl.DeviceError("appPath not found (test)")

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    with pytest.raises(simctl.DeviceError):
        run_all(_ios_eff(), scenarios, lease, actuator="xcuitest")


def test_a_timeout_on_a_non_forced_erase_lease_still_propagates() -> None:
    # BE-0374 scopes the timeout split to the forced-erase (and BE-0354-escalated) lease alone — see
    # its "Alternatives considered" on why the ordinary lease path is a separate question. A
    # `DeviceTimeout` on the scenario's very first lease, which never forces erase, must therefore
    # keep the pre-existing fail-loud-and-abort-the-run behavior rather than failing just this
    # scenario and latching the run for every scenario after it.
    def lease(eff: Effective, scenario: Scenario) -> Lease:
        raise simctl.DeviceTimeout("device operation timed out after 60s: xcrun simctl boot ABC")

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    with pytest.raises(simctl.DeviceTimeout):
        run_all(_ios_eff(), scenarios, lease, actuator="xcuitest")


# --- a device preparation that timed out is not a device that refused (BE-0374) --- #
#
# The degradation just above rests on a claim about a device that *answered*: its refusal is evidence
# about one operation, so the scenario deserves the bare respawn it would have had before the forced
# erase existed. A device that never answered gave no evidence about the operation at all — only that
# the service behind every operation stopped serving — so the same treatment assumes exactly what the
# timeout disproved. These pin the split, and the run-level latch that follows from it.


def test_a_timed_out_forced_erase_prep_fails_the_scenario_instead_of_degrading() -> None:
    # The forced-erase retry's own lease times out rather than failing. Degrading would lease again
    # *without* the forced erase, which is one of the conditions that put the attempt on the
    # warm-resident reuse path — itself full of the very `simctl` calls that just stalled. So it
    # resolves one of two ways and both are worse than failing here: a second timeout no handler
    # catches (the run aborts, every earned verdict lost), or a verdict published from a host that had
    # just failed to answer `shutdown`. No third lease may be attempted.
    state = {"n": 0}
    erase_seen: list[bool | None] = []
    messages: list[str] = []
    crash = base.BackendCrashError("runner crashed during the readiness gate (test)")

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        erase_seen.append(scenario.preconditions.erase)
        if state["n"] == 1:
            raise crash
        raise simctl.DeviceTimeout(
            "device operation timed out after 60s: xcrun simctl shutdown ABC"
            " (this host's CoreSimulator may be wedged)"
        )

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(
        _ios_eff(), scenarios, lease, actuator="xcuitest", crash_retries=2, progress=messages.append
    )
    assert not results[0].ok
    assert erase_seen == [None, True]  # the degraded bare respawn was never leased
    failure = results[0].failure or ""
    assert "timed out" in failure
    assert "xcrun simctl shutdown ABC" in failure  # the command and deadline reach the operator
    assert "60s" in failure
    assert str(crash) in failure  # the crash the retry was recovering from is not dropped
    assert "device preparation timed out" in messages[-1]  # the timeout's own progress wording


def test_an_ordinary_device_fault_still_degrades_after_the_timeout_split() -> None:
    # The other half of the split, asserted from this item's side: a device that answered by refusing
    # keeps the bare respawn the degradation was written for. Nothing about the refusal case changed,
    # so a split that swept it up too would be a regression this item never argued for.
    state = {"n": 0}
    erase_seen: list[bool | None] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        erase_seen.append(scenario.preconditions.erase)
        if state["n"] == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        if state["n"] == 2:
            raise simctl.DeviceError("Unable to erase in current state: Booted")
        return _lease(eff, scenario)

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_ios_eff(), scenarios, lease, actuator="xcuitest", crash_retries=2)
    assert results[0].ok
    assert erase_seen == [None, True, None]  # degraded to the bare respawn, exactly as before


def test_a_timed_out_replacement_device_lease_fails_the_scenario_too() -> None:
    # BE-0354's escalated rung reaches this same handler with the forced erase still on it, so the
    # split governs it as well. A `simctl create`/`clone` that never returns is the same wedge as a
    # `shutdown` that never returns and earns the same treatment — pinned here rather than inferred
    # from the forced-erase case generalising. A second scenario asserts the third of the item's three
    # things at once for this rung too: the run-level latch, not just the scenario's own failure.
    state = {"n": 0}
    erase_seen: list[bool | None] = []
    replacements: list[int] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        n = state["n"]
        erase_seen.append(scenario.preconditions.erase)
        if n == 3:  # the attempt after the escalation was requested
            raise simctl.DeviceTimeout("device operation timed out after 120s: xcrun simctl clone")
        if n > 3:  # scenario "b" must never be leased at all
            raise AssertionError("scenario b was leased despite the run-level latch")
        return Lease(
            driver=_crashing_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
            video_start_stalled=lambda: False,
            request_device_replacement=lambda n=n: replacements.append(n),  # type: ignore[misc]
        )

    scenarios = [
        Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]}),
        Scenario.model_validate({"name": "b", "steps": [{"tap": {"id": "ok"}}]}),
    ]
    results = run_all(_replaceable_eff(), scenarios, lease, actuator="xcuitest", crash_retries=3)
    assert replacements == [2]  # the forced-erase retry crashed again and escalated
    assert not results[0].ok and not results[1].ok
    assert erase_seen == [None, True, True]  # no fourth, degraded lease
    assert "timed out" in (results[0].failure or "")
    assert "never leased" in (results[1].failure or "")  # the escalated timeout latches the run too


def test_a_timed_out_preparation_latches_the_run_with_no_budget_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A wedged CoreSimulator is a property of the host, not of the scenario that met it, so every
    # later scenario must fail before its own first lease rather than spend its own deadline
    # rediscovering the wedge. `BAJUTSU_RUN_CRASH_RECOVERY_BUDGET` is unset here — the default — which
    # is also the case that used to format a `None` budget with `:g` and raise a `TypeError` out of
    # `run_one`, aborting `run_all` and discarding every verdict the run had earned. Cleared explicitly
    # rather than merely assumed, so a lane that exports this variable does not silently stop
    # exercising the unset-budget path this test exists to pin.
    monkeypatch.delenv("BAJUTSU_RUN_CRASH_RECOVERY_BUDGET", raising=False)
    state = {"n": 0}

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        if state["n"] == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        raise simctl.DeviceTimeout(
            "device operation timed out after 60s: xcrun simctl shutdown ABC"
        )

    scenarios = [
        Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]}),
        Scenario.model_validate({"name": "b", "steps": [{"tap": {"id": "ok"}}]}),
    ]
    results = run_all(_ios_eff(), scenarios, lease, actuator="xcuitest", crash_retries=2)
    assert not results[0].ok and not results[1].ok
    assert state["n"] == 2  # scenario b was never leased at all
    assert "timed out" in (results[1].failure or "")
    assert "never leased" in (results[1].failure or "")
    # The latch names the wedge, not a budget this run never had.
    assert "budget" not in (results[1].failure or "")


def test_run_all_skips_forced_erase_when_scenario_declares_reinstall_overwrite() -> None:
    # `reinstall: overwrite` is a scenario's explicit declaration that it needs its app's data
    # container preserved across a lease. Forcing `erase` on a retry would silently wipe exactly the
    # state such a scenario was written to keep, so the retry keeps today's bare in-place respawn.
    state = {"n": 0}
    erase_seen: list[bool | None] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        erase_seen.append(scenario.preconditions.erase)
        if state["n"] == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [
        Scenario.model_validate(
            {
                "name": "a",
                "preconditions": {"reinstall": "overwrite"},
                "steps": [{"tap": {"id": "ok"}}],
            }
        )
    ]
    results = run_all(_eff(), scenarios, lease)
    assert results[0].ok
    assert erase_seen == [None, None]  # never forced, on either attempt


def test_run_all_forces_erase_even_when_preconditions_erase_is_already_false() -> None:
    # A scenario reaching here with `preconditions.erase is False` is indistinguishable from "the CLI
    # already resolved an unset scenario to the target's default" — `_filter_scenarios`
    # (`bajutsu/cli/commands/run.py`) always leaves every scenario with a concrete bool before
    # `run_all` ever sees it, and that default is `False` unless a target config opts in. That is the
    # *common* production case, not an edge case, so a guard on `erase is False` would silently
    # disable this whole unit on the one path it was written for. Only `reinstall: overwrite` skips
    # the forced erase — it alone actually protects app data, since `reinstall`'s own default
    # `"clean"` wipes it regardless of `erase`, so a bare `erase: false` never protected anything a
    # forced retry needs to respect.
    state = {"n": 0}
    erase_seen: list[bool | None] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        erase_seen.append(scenario.preconditions.erase)
        if state["n"] == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [
        Scenario.model_validate(
            {"name": "a", "preconditions": {"erase": False}, "steps": [{"tap": {"id": "ok"}}]}
        )
    ]
    results = run_all(_eff(), scenarios, lease)
    assert results[0].ok
    assert erase_seen == [False, True]  # attempt 1 unmodified, attempt 2 forces erase regardless


def test_run_all_honors_an_explicit_no_erase_override_on_a_crash_triggered_retry() -> None:
    # `force_erase_on_retry=False` is what `bajutsu run --no-erase` passes (bajutsu/cli/commands/run.py):
    # the operator's explicit opt-out, captured ahead of `_filter_scenarios` resolving every scenario's
    # `preconditions.erase` to a concrete bool. Unlike a bare `erase: false` on the scenario itself
    # (which the test above shows does NOT skip the forced retry, since it is indistinguishable from
    # "nobody asked"), this flag must still be honored — an operator who explicitly asked to keep the
    # device as-is should not have it silently erased mid-run.
    state = {"n": 0}
    erase_seen: list[bool | None] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        erase_seen.append(scenario.preconditions.erase)
        if state["n"] == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [
        Scenario.model_validate(
            {"name": "a", "preconditions": {"erase": False}, "steps": [{"tap": {"id": "ok"}}]}
        )
    ]
    results = run_all(_eff(), scenarios, lease, force_erase_on_retry=False)
    assert results[0].ok
    assert erase_seen == [False, False]  # never forced — the operator opted out


def test_run_all_skips_forced_erase_on_a_real_device() -> None:
    # A real device (`xcuitest.deviceType: device`) raises loudly on any `erase` precondition
    # (`XcuitestEnvironment.start`) instead of honoring it — simctl cannot reach a physical device.
    # Forcing `erase` on a crash-triggered retry there would raise past this loop's own
    # `except BackendCrashError` and abort the whole run, not just fail the one scenario.
    state = {"n": 0}
    erase_seen: list[bool | None] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        erase_seen.append(scenario.preconditions.erase)
        if state["n"] == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    dev = _ios_eff(xcuitest=XcuitestConfig(testRunner="Runner.xctestrun", deviceType="device"))
    results = run_all(dev, scenarios, lease, actuator="xcuitest")
    assert results[0].ok
    assert erase_seen == [None, None]  # never forced on a real device


def test_run_all_skips_forced_erase_on_the_live_webdriver_route() -> None:
    # The live WebDriver endpoint (an http(s):// udid spec) also raises loudly on any `erase`
    # precondition (`XcuitestLiveEnvironment.start`) — same reasoning as the real-device case above.
    state = {"n": 0}
    erase_seen: list[bool | None] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        erase_seen.append(scenario.preconditions.erase)
        if state["n"] == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(
        _ios_eff(),
        scenarios,
        lease,
        actuator="xcuitest",
        lease_udid_spec="https://example.test/wd/hub",
    )
    assert results[0].ok
    assert erase_seen == [None, None]  # never forced on the live WebDriver route


def test_run_all_recovers_when_the_lease_itself_crashes_at_bringup() -> None:
    # A backend crash during the LEASE — the launch/readiness gate, not a scenario step — must be
    # recovered by the same retry. The resident runner can answer /health at cold spawn and then crash
    # on the first readiness query, before any step runs, so `self.lease` (which runs launch_driver)
    # raises BackendCrashError; the retry leases afresh (a cold respawn) and the scenario passes. Guards
    # the fix that moved the lease inside the crash-retry try — before it, a lease-time crash escaped
    # the loop and failed the whole run.
    state = {"n": 0}
    leased: list[int] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        n = state["n"]
        if n == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        leased.append(n)
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_eff(), scenarios, lease)
    assert [r.ok for r in results] == [
        True
    ]  # recovered even though the crash happened at lease time
    assert leased == [2]  # the second lease (a cold respawn) served the run


def test_run_all_fails_when_the_lease_crashes_every_attempt() -> None:
    # A lease that crashes at bring-up on every attempt exhausts the budget and fails loudly, exactly
    # like a step-time crash — the lease-time path honors the same bound (BE-0049), never looping.
    leases = 0

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        nonlocal leases
        leases += 1
        raise base.BackendCrashError("runner crashed during the readiness gate (test)")

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_eff(), scenarios, lease, crash_retries=1)
    assert not results[0].ok and leases == 2  # crash_retries=1 → 2 attempts, both crashed at lease
    assert "crashed mid-run" in (results[0].failure or "")


class _AdvancingClock:
    """A clock whose `now()` moves only when the test advances it (or via `sleep`).

    Lets a respawn's wall-clock cost be injected deterministically — no real delay — so the
    crash-recovery budget can be exercised in a unit test.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_run_all_stops_respawning_once_the_crash_recovery_budget_is_spent() -> None:
    # `crash_retries` caps the retry *count*; the wall-clock budget caps the *time*. A runner that
    # crashes and never comes back makes each cold respawn pay a fresh startup ceiling, so a generous
    # count would burn count x that ceiling — enough to blow a job's timeout with a silent hang. With a
    # 300s budget and each respawn "taking" 200s, recovery stops once the budget is spent — well before
    # the 5-retry count — and fails loudly naming the budget (a crash is never absorbed into a pass —
    # BE-0049).
    clock = _AdvancingClock()
    leases = 0

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        nonlocal leases
        leases += 1
        clock.advance(200.0)  # each cold respawn's readiness wait burns 200s of wall-clock
        raise base.BackendCrashError("runner crashed during the readiness gate (test)")

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(
        _eff(), scenarios, lease, clock=clock, crash_retries=5, crash_recovery_budget=300.0
    )
    # Deadline is set at the first crash: t=200 + 300 = 500. Attempt 2 crashes at t=400 (<500, retry);
    # attempt 3 at t=600 (≥500, budget spent → stop). So 3 leases, not the 6 the count alone allows.
    assert not results[0].ok and leases == 3
    assert "crash-recovery budget" in (results[0].failure or "")


def test_run_all_crash_recovery_budget_still_rides_out_a_fast_one_off() -> None:
    # The budget bites only when respawns are slow. A one-off crash whose respawn comes back at once
    # (no wall-clock burned) is still recovered — the scenario passes even under a tight budget, because
    # the budget clock never advances. Guards against the budget switching off genuine one-off recovery.
    clock = _AdvancingClock()
    state = {"n": 0}

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        if state["n"] == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(
        _eff(), scenarios, lease, clock=clock, crash_retries=2, crash_recovery_budget=1.0
    )
    assert results[0].ok and state["n"] == 2  # recovered on the first respawn, clock never advanced


def test_crash_recovery_budget_default_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A lane caps respawn wall-clock via BAJUTSU_CRASH_RECOVERY_BUDGET without a code change; unset,
    # non-positive, or invalid all read as unbounded (the count stays the only cap) — never as zero,
    # which would be "no recovery at all", crash_retries=0's job, not this knob's.
    from bajutsu.common.runner.pipeline import _default_crash_recovery_budget

    monkeypatch.delenv("BAJUTSU_CRASH_RECOVERY_BUDGET", raising=False)
    assert _default_crash_recovery_budget() is None  # unset -> unbounded
    monkeypatch.setenv("BAJUTSU_CRASH_RECOVERY_BUDGET", "300")
    assert _default_crash_recovery_budget() == 300.0
    monkeypatch.setenv("BAJUTSU_CRASH_RECOVERY_BUDGET", "0")
    assert _default_crash_recovery_budget() is None  # non-positive -> unbounded, not "no recovery"
    monkeypatch.setenv("BAJUTSU_CRASH_RECOVERY_BUDGET", "-5")
    assert _default_crash_recovery_budget() is None
    monkeypatch.setenv("BAJUTSU_CRASH_RECOVERY_BUDGET", "not-a-number")
    assert _default_crash_recovery_budget() is None  # invalid -> unbounded, never a crash


def test_run_all_honors_the_crash_recovery_budget_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End to end: with the env set and no explicit arg, run_all reads the env budget and stops the
    # respawn loop once the wall-clock is spent — the same None-reads-the-env wiring as crash_retries.
    monkeypatch.setenv("BAJUTSU_CRASH_RECOVERY_BUDGET", "300")
    clock = _AdvancingClock()
    leases = 0

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        nonlocal leases
        leases += 1
        clock.advance(200.0)
        raise base.BackendCrashError("runner crashed during the readiness gate (test)")

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_eff(), scenarios, lease, clock=clock, crash_retries=5)  # budget arg unset
    assert not results[0].ok and leases == 3  # env budget 300 -> stopped after ~one slow respawn
    assert "crash-recovery budget" in (results[0].failure or "")


def test_run_all_stops_respawning_once_the_run_crash_recovery_budget_is_spent() -> None:
    # The run-level budget accumulates *actual recovery time spent*, not wall-clock elapsed since some
    # earlier crash: scenario "a" crashes once, and its successful respawn costs 60s of real recovery
    # time (billed in full once its loop concludes). That alone does not latch anything — a
    # slow-but-successful recovery says the device works, not that it is broken — so scenario "b"
    # still gets its own first attempt. Only once "b" *also* crashes, with the budget already spent, is
    # its own retry denied and the run-level budget marked as genuinely given up on.
    clock = _AdvancingClock()
    calls = 0

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        if calls == 2:
            clock.advance(60.0)  # scenario a's one respawn took 60s
            return Lease(
                driver=_fake_driver(),
                sink=NullSink(),
                relaunch=None,
                control=None,
                collector=None,
                release=lambda: None,
            )
        raise base.BackendCrashError("runner crashed during the readiness gate (test)")

    scenarios = [
        Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]}),
        Scenario.model_validate({"name": "b", "steps": [{"tap": {"id": "ok"}}]}),
    ]
    results = run_all(
        _eff(), scenarios, lease, clock=clock, crash_retries=5, run_crash_recovery_budget=50.0
    )
    assert results[0].ok  # scenario a recovered; its 60s of recovery time is now billed in full
    assert not results[1].ok
    assert calls == 3  # a: 2 leases; b: leased once, denied its retry by the already-spent budget
    assert "run-level crash-recovery budget" in (results[1].failure or "")


def test_run_all_run_crash_recovery_budget_latch_skips_every_remaining_scenario() -> None:
    # Once a scenario has actually *failed* because the run-level budget was the binding constraint,
    # that must stay latched for the rest of the run — a device that has demonstrated it cannot
    # recover should not still get one full cold-spawn attempt per remaining scenario before the job's
    # own `timeout-minutes` cancels it. Three scenarios: "a" recovers successfully but spends the whole
    # budget doing it (not yet a latch by itself); "b" then crashes and is denied its own retry,
    # marking the budget given up on; "c" must be skipped entirely, never leased at all.
    clock = _AdvancingClock()
    calls = 0

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        if calls == 2:
            clock.advance(60.0)
            return Lease(
                driver=_fake_driver(),
                sink=NullSink(),
                relaunch=None,
                control=None,
                collector=None,
                release=lambda: None,
            )
        raise base.BackendCrashError("runner crashed during the readiness gate (test)")

    scenarios = [
        Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]}),
        Scenario.model_validate({"name": "b", "steps": [{"tap": {"id": "ok"}}]}),
        Scenario.model_validate({"name": "c", "steps": [{"tap": {"id": "ok"}}]}),
    ]
    results = run_all(
        _eff(), scenarios, lease, clock=clock, crash_retries=5, run_crash_recovery_budget=50.0
    )
    assert results[0].ok
    assert not results[1].ok and not results[2].ok
    assert calls == 3  # a: 2 leases (recovers); b: 1 lease (denied its retry); c: never leased
    assert "run-level crash-recovery budget" in (results[1].failure or "")
    assert "run-level crash-recovery budget" in (results[2].failure or "")
    assert "never leased" in (results[2].failure or "")


def test_run_all_run_crash_recovery_budget_does_not_fail_a_scenario_after_a_slow_but_successful_recovery() -> (
    None
):
    # The exact false-positive a bare `exhausted()` latch would cause: scenario "a" crashes once, and
    # its respawn succeeds after spending the *whole* budget (a device replacement that took a while
    # and then worked) — the device has just proven it recovers, not that it is broken. Scenario "b",
    # which never crashes at all, must still run and pass normally: `given_up_cause()` only latches
    # once a scenario's own loop has actually *failed* because of the run-level budget, which never
    # happens here.
    clock = _AdvancingClock()
    calls = 0

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        nonlocal calls
        calls += 1
        if scenario.name == "a" and calls == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        if scenario.name == "a":
            clock.advance(610.0)  # a slow device replacement that ultimately succeeds
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [
        Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]}),
        Scenario.model_validate({"name": "b", "steps": [{"tap": {"id": "ok"}}]}),
    ]
    results = run_all(
        _eff(), scenarios, lease, clock=clock, crash_retries=5, run_crash_recovery_budget=600.0
    )
    assert results[0].ok  # a recovered, spending the whole 600s budget doing it
    assert results[1].ok  # b never crashed and must not be denied a lease it never asked to retry


def test_run_all_run_crash_recovery_budget_ignores_healthy_scenarios_between_crashes() -> None:
    # A long stretch of scenarios that never crash must not itself erode the run-level budget — only
    # time actually billed via a completed recovery episode counts. Guards the exact bug an earlier,
    # deadline-based design had: a budget measured as wall-clock elapsed since the run's first crash
    # would have let a later, unrelated one-off crash get blocked outright even though almost none of
    # the budget had genuinely been spent recovering.
    clock = _AdvancingClock()

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        if scenario.name == "healthy":
            clock.advance(10_000.0)  # this scenario's real run took a long time; no crash involved
        else:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [
        Scenario.model_validate({"name": "healthy", "steps": [{"tap": {"id": "ok"}}]}),
        Scenario.model_validate({"name": "late-crash", "steps": [{"tap": {"id": "ok"}}]}),
    ]
    results = run_all(
        _eff(), scenarios, lease, clock=clock, crash_retries=5, run_crash_recovery_budget=50.0
    )
    assert results[0].ok
    assert not results[1].ok  # every attempt crashes (the lease always raises for "late-crash")
    # No real recovery time had been billed yet when "late-crash" made its own first attempt — the
    # 10,000s the healthy scenario burned must play no part — so it exhausts on its own retry count,
    # never on the run-level budget.
    assert "run-level crash-recovery budget" not in (results[1].failure or "")
    assert "did not recover across" in (results[1].failure or "")


def test_run_all_run_crash_recovery_budget_never_blocks_the_very_first_crash() -> None:
    # The run's very first crash always sees an empty accumulator, so it is never blocked by even a
    # near-zero run-level budget — the same never-block-the-first-respawn rule `crash_recovery_budget`
    # already follows per scenario, now shared across the whole run.
    clock = _AdvancingClock()
    state = {"n": 0}

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        if state["n"] == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(
        _eff(), scenarios, lease, clock=clock, crash_retries=2, run_crash_recovery_budget=0.001
    )
    assert results[0].ok and state["n"] == 2


def test_run_crash_recovery_budget_default_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bajutsu.common.runner.pipeline import _default_run_crash_recovery_budget

    monkeypatch.delenv("BAJUTSU_RUN_CRASH_RECOVERY_BUDGET", raising=False)
    assert _default_run_crash_recovery_budget() is None  # unset -> unbounded
    monkeypatch.setenv("BAJUTSU_RUN_CRASH_RECOVERY_BUDGET", "900")
    assert _default_run_crash_recovery_budget() == 900.0
    monkeypatch.setenv("BAJUTSU_RUN_CRASH_RECOVERY_BUDGET", "0")
    assert _default_run_crash_recovery_budget() is None  # non-positive -> unbounded
    monkeypatch.setenv("BAJUTSU_RUN_CRASH_RECOVERY_BUDGET", "not-a-number")
    assert _default_run_crash_recovery_budget() is None  # invalid -> unbounded, never a crash


def test_run_all_honors_the_run_crash_recovery_budget_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End to end: with the env set and no explicit arg, run_all reads the env's run-level budget and
    # stops the respawn loop once the accumulated recovery time is spent — the same None-reads-the-env
    # wiring as crash_recovery_budget. Two scenarios, matching
    # test_run_all_stops_respawning_once_the_run_crash_recovery_budget_is_spent: a single scenario can
    # never see its *own* in-progress spend (billed only once its own loop ends), so a one-scenario
    # version of this test could never actually observe the run-level budget as the reported cause.
    monkeypatch.setenv("BAJUTSU_RUN_CRASH_RECOVERY_BUDGET", "50")
    clock = _AdvancingClock()
    calls = 0

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        if calls == 2:
            clock.advance(60.0)
            return Lease(
                driver=_fake_driver(),
                sink=NullSink(),
                relaunch=None,
                control=None,
                collector=None,
                release=lambda: None,
            )
        raise base.BackendCrashError("runner crashed during the readiness gate (test)")

    scenarios = [
        Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]}),
        Scenario.model_validate({"name": "b", "steps": [{"tap": {"id": "ok"}}]}),
    ]
    results = run_all(
        _eff(), scenarios, lease, clock=clock, crash_retries=5
    )  # run_crash_recovery_budget arg unset
    assert results[0].ok
    assert not results[1].ok
    assert "run-level crash-recovery budget" in (results[1].failure or "")


def test_scenario_runner_runs_one_in_isolation() -> None:
    """`_ScenarioRunner.run_one` runs a single scenario without run_all's setup (BE-0172).

    The promotion's payoff: the per-scenario runner is unit-testable directly, its shared context
    passed as explicit fields rather than reconstructed from all of `run_all`.
    """
    from bajutsu.common.evidence.redaction import Redactor
    from bajutsu.common.runner.pipeline import _ScenarioRunner

    runner = _ScenarioRunner(
        eff=_eff(),
        lease=_lease,
        redactor=Redactor(None),
        mailbox=None,
        caps=None,
        total=2,
    )
    ok = runner.run_one(0, Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]}))
    bad = runner.run_one(
        1, Scenario.model_validate({"name": "b", "steps": [{"tap": {"id": "missing"}}]})
    )
    assert ok.ok and ok.sid == "00-a"
    assert not bad.ok and bad.sid == "01-b"


def test_sanitize_source_stem_leaves_dots_dashes_and_underscores_unchanged() -> None:
    assert sanitize_source_stem("login_flow") == "login_flow"
    assert sanitize_source_stem("v1.2-final") == "v1.2-final"


def test_sanitize_source_stem_leaves_japanese_characters_unchanged() -> None:
    # Fullwidth/Japanese characters are ordinary in this codebase's own scenario names (CLAUDE.md);
    # a stem built from one must stay identifiable, not collapse to a run of underscores.
    assert sanitize_source_stem("決済フロー") == "決済フロー"


def test_sanitize_source_stem_replaces_only_the_unsafe_characters() -> None:
    assert sanitize_source_stem("login#1") == "login_1"
    assert sanitize_source_stem("a?b") == "a_b"
    assert sanitize_source_stem("a??b") == "a__b"  # one replacement per unsafe character


# --- the shared slug length cap (BE-0420) ---

# The kind of name `record` auto-generates with no `--out`: the recording's whole goal, verbatim.
_LONG_GOAL = (
    "log in with a saved card, confirm the checkout total matches the cart, "
    "and check the confirmation email arrives"
)


def test_scenario_slug_caps_an_overlong_name_without_a_trailing_hyphen() -> None:
    slug = scenario_slug(_LONG_GOAL)
    assert len(slug) <= _MAX_SLUG_CHARS
    assert slug == "log-in-with-a-saved-card-confirm-the-checkout-total-matches"
    assert not slug.endswith("-")


def test_scenario_slug_drops_a_hyphen_the_cut_leaves_dangling() -> None:
    # 59 alphanumerics then a separator: the character slice lands exactly on the hyphen.
    assert scenario_slug("a" * 59 + " tail") == "a" * 59


def test_sanitize_source_stem_caps_an_overlong_ascii_stem() -> None:
    stem = "checkout_" * 20
    capped = sanitize_source_stem(stem)
    assert len(capped) == _MAX_SLUG_CHARS
    assert capped == stem[:_MAX_SLUG_CHARS]


def test_sanitize_source_stem_caps_a_multibyte_stem_by_character_count_not_bytes() -> None:
    """An overlong Japanese stem is capped by character count, not by its UTF-8 byte length (BE-0420).

    `決済フロー` is 3 bytes per character, so a byte-oriented cap would give this stem far fewer
    characters than an equally long ASCII one. This pins that the cap does not do that.
    """
    capped = sanitize_source_stem("ab" + "決済フロー" * 12)
    assert len(capped) == _MAX_SLUG_CHARS
    assert len(capped.encode("utf-8")) > _MAX_SLUG_CHARS
    assert capped == "ab" + "決済フロー" * 11 + "決済フ"


def test_sanitize_source_stem_never_returns_empty_for_a_non_empty_stem() -> None:
    """A non-empty stem always keeps at least its first character (BE-0420).

    A character-count cap makes this trivially true for any budget of at least one, unlike a
    byte-oriented cap, which could drop a multi-byte character's every byte. Kept as a regression
    guard on the no-fallback claim in `sanitize_source_stem`'s docstring.
    """
    assert sanitize_source_stem("決") == "決"
    assert sanitize_source_stem("決済フロー" * 10)


def test_recorded_scenario_with_no_out_flag_yields_a_sid_within_the_cap() -> None:
    """The `record`-with-no-`--out` path: a verbose goal becomes a file name, then an evidence dir.

    `scenario_out_name` itself stays uncapped (the item's *Not doing*) — a long `*.yaml` name is
    legible on disk and blocks nothing; what must stay bounded is the directory every later run of
    that file creates.
    """
    from bajutsu.common.runner.pipeline import _evidence_sid
    from bajutsu.serve.helpers import scenario_out_name

    scenario = Scenario.model_validate({"name": _LONG_GOAL, "steps": [{"tap": {"id": "ok"}}]})
    scenario.set_source_stem(Path(scenario_out_name(_LONG_GOAL)).stem)

    sid = _evidence_sid(0, scenario)
    assert len(sid) <= len("00-") + _MAX_SLUG_CHARS
    assert sid == "00-log_in_with_a_saved_card__confirm_the_checkout_total_matches"


def test_slugs_colliding_after_truncation_still_get_distinct_sids() -> None:
    """Truncation adds no collision the run-order prefix does not already resolve (BE-0420).

    Why the item adds no duplicate-suffix counter: every `sid` `_evidence_sid` builds carries the
    `{i:02d}-` index, which is what keeps two scenarios apart within a run — capped slug or not.
    """
    from bajutsu.common.runner.pipeline import _evidence_sid

    shared = "checkout_" * 20
    first = Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})
    second = Scenario.model_validate({"name": "b", "steps": [{"tap": {"id": "ok"}}]})
    first.set_source_stem(shared + "_one")
    second.set_source_stem(shared + "_two")

    assert sanitize_source_stem(shared + "_one") == sanitize_source_stem(shared + "_two")
    assert _evidence_sid(0, first) != _evidence_sid(1, second)


def test_scenario_runner_sid_prefers_source_stem_over_name_and_sanitizes_it() -> None:
    """`run_one`'s `sid` uses the source file's stem, not `name:`, sanitized through the real call (BE-0417).

    The stem carries a `#` so this actually exercises `sanitize_source_stem` at the call site —
    a stem with no unsafe characters would pass identically whether or not that call is wired in.
    """
    from bajutsu.common.evidence.redaction import Redactor
    from bajutsu.common.runner.pipeline import _ScenarioRunner

    runner = _ScenarioRunner(
        eff=_eff(), lease=_lease, redactor=Redactor(None), mailbox=None, caps=None, total=1
    )
    scenario = Scenario.model_validate(
        {"name": "Login succeeds with a valid password", "steps": [{"tap": {"id": "ok"}}]}
    )
    scenario.set_source_stem("login#1")
    result = runner.run_one(0, scenario)
    assert result.sid == "00-login_1"


def test_preflight_fails_unsupported_scenario_before_leasing() -> None:
    # A selectOption (native <select>) needs the web-only selectOption capability, which xcuitest
    # lacks — the preflight fails the scenario up front, so the lease (device work) is never reached
    # (BE-0082).
    scenarios = [
        Scenario.model_validate(
            {"name": "z", "steps": [{"selectOption": {"sel": {"id": "m"}, "option": "x"}}]}
        )
    ]

    def lease_must_not_run(eff: Effective, s: Scenario) -> Lease:
        raise AssertionError("lease must not be called when the preflight rejects the scenario")

    results = run_all(_eff(), scenarios, lease_must_not_run, actuator="xcuitest")
    assert len(results) == 1 and not results[0].ok
    assert results[0].backend == "xcuitest"
    assert "selectOption" in (results[0].failure or "")


def test_preflight_allows_supported_scenario_on_xcuitest() -> None:
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_eff(), scenarios, _lease, actuator="xcuitest")
    assert results[0].ok


def test_real_device_narrowing_reaches_the_preflight_from_run_all() -> None:
    # BE-0238 Unit 3: the fixed-`actuator` call site must thread `eff` into `capabilities_for_run`,
    # so a real iOS device drops the simctl-backed capabilities. A setLocation scenario is skipped up
    # front (no lease), guarding against a refactor that reintroduces the eff-less `capabilities_for`.
    scenarios = [
        Scenario.model_validate(
            {"name": "loc", "steps": [{"setLocation": {"lat": 1.0, "lon": 2.0}}]}
        )
    ]

    def lease_must_not_run(eff: Effective, s: Scenario) -> Lease:
        raise AssertionError("lease must not be called when the preflight rejects the scenario")

    dev = _ios_eff(xcuitest=XcuitestConfig(testRunner="Runner.xctestrun", deviceType="device"))
    results = run_all(dev, scenarios, lease_must_not_run, actuator="xcuitest")
    assert len(results) == 1 and not results[0].ok
    assert results[0].backend == "xcuitest"
    assert "deviceControl.setLocation" in (results[0].failure or "")


def test_simulator_still_leases_a_device_control_scenario_on_xcuitest() -> None:
    # The narrowing is real-device-only: on the Simulator the same setLocation scenario clears the
    # preflight and reaches the lease (device work) — the counterpart that proves the wiring narrows
    # nothing by default. Asserting the lease is reached (not the fake's runtime outcome) keeps the
    # test about the preflight, not FakeDriver's device-control support.
    scenarios = [
        Scenario.model_validate(
            {"name": "loc", "steps": [{"setLocation": {"lat": 1.0, "lon": 2.0}}]}
        )
    ]
    leased: list[str] = []

    def recording_lease(eff: Effective, s: Scenario) -> Lease:
        leased.append(s.name)
        return _lease(eff, s)

    sim = _ios_eff(xcuitest=XcuitestConfig(testRunner="Runner.xctestrun", deviceType="simulator"))
    results = run_all(sim, scenarios, recording_lease, actuator="xcuitest")
    assert leased == ["loc"]
    assert "deviceControl.setLocation" not in (results[0].failure or "")


def test_resolve_actuator_preflights_per_scenario_and_fails_fast() -> None:
    # BE-0240: with a per-scenario resolver, the scenario's own actuator decides the capability set.
    # A selectOption resolved to xcuitest fails the preflight up front (xcuitest lacks it) — no lease.
    scenarios = [
        Scenario.model_validate(
            {"name": "z", "steps": [{"selectOption": {"sel": {"id": "m"}, "option": "x"}}]}
        )
    ]

    def lease_must_not_run(eff: Effective, s: Scenario) -> Lease:
        raise AssertionError("lease must not be called when the preflight rejects the scenario")

    results = run_all(_eff(), scenarios, lease_must_not_run, resolve_actuator=lambda s: "xcuitest")
    assert len(results) == 1 and not results[0].ok
    assert results[0].backend == "xcuitest" and "selectOption" in (results[0].failure or "")


def test_resolve_actuator_escalates_to_a_capable_actuator() -> None:
    # BE-0240: the same pinch resolved to xcuitest clears the preflight (xcuitest has multiTouch), so
    # the scenario is leased and executed rather than failed up front. (It still fails on the fake
    # driver at the pinch step — a runtime miss, not the capability rejection we're asserting is gone.)
    scenarios = [
        Scenario.model_validate(
            {"name": "z", "steps": [{"pinch": {"sel": {"id": "m"}, "scale": 2.0}}]}
        )
    ]
    leased: list[str] = []

    def lease(eff: Effective, s: Scenario) -> Lease:
        leased.append(s.name)
        return _lease(eff, s)

    results = run_all(_eff(), scenarios, lease, resolve_actuator=lambda s: "xcuitest")
    assert leased == ["z"]  # the lease was reached: no capability fail-fast
    assert "unsupported on backend" not in (results[0].failure or "")


def test_run_all_rejects_both_actuator_and_resolve_actuator() -> None:
    # BE-0240: the fixed actuator and the per-scenario resolver answer the same question; passing
    # both is a caller bug, failed loudly rather than silently letting the resolver win.
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    with pytest.raises(ValueError, match="not both"):
        run_all(
            _eff(), scenarios, _lease, actuator="xcuitest", resolve_actuator=lambda s: "xcuitest"
        )


def test_resolve_actuator_no_available_actuator_fails_cleanly() -> None:
    # BE-0240: when no iOS actuator is even available the resolver raises; the pipeline turns that
    # into a clean per-scenario failure (no lease, no crash aborting the whole run).
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]

    def resolver(s: Scenario) -> str:
        raise RuntimeError("no available actuator among ['xcuitest']")

    def lease_must_not_run(eff: Effective, s: Scenario) -> Lease:
        raise AssertionError("lease must not be called when no actuator is available")

    results = run_all(_eff(), scenarios, lease_must_not_run, resolve_actuator=resolver)
    assert len(results) == 1 and not results[0].ok
    assert results[0].backend == "" and "no available actuator" in (results[0].failure or "")


def test_run_all_parallel_preserves_order_and_releases() -> None:
    scenarios = [
        Scenario.model_validate({"name": n, "steps": [{"tap": {"id": "ok"}}]})
        for n in ("a", "b", "c")
    ]
    released: list[str] = []

    def lease(eff: Effective, s: Scenario) -> Lease:
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: released.append(s.name),
        )

    results = run_all(_eff(), scenarios, lease, workers=2)
    assert [r.scenario for r in results] == ["a", "b", "c"]  # order preserved despite concurrency
    assert all(r.ok for r in results)
    assert len(released) == 3 and set(released) == {"a", "b", "c"}  # every leased device released


def test_run_all_releases_after_each_scenario() -> None:
    scenarios = [
        Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]}),
        Scenario.model_validate({"name": "b", "steps": [{"tap": {"id": "ok"}}]}),
    ]
    released: list[str] = []

    def lease(eff: Effective, s: Scenario) -> Lease:
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: released.append(s.name),
        )

    run_all(_eff(), scenarios, lease)
    assert released == ["a", "b"]  # release runs after every scenario, including the last


def test_run_all_hands_the_lease_s_collector_to_run_scenario_as_the_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collector itself crosses into `run_scenario`, not one of its callables (BE-0365).

    The control channel is the one direction that runs *into* the app, so it has no snapshot to
    read off — a stray edit that dropped the kwarg, or passed something other than the lease's own
    collector, would leave the scenario's `visual` captures uncorrected without failing any other
    pipeline test.
    """
    from bajutsu.common.evidence.network import NetworkCollector
    from bajutsu.common.orchestrator import run_scenario as original_run_scenario

    seen: list[object] = []

    def spying_run_scenario(*args: object, **kwargs: object) -> object:
        seen.append(kwargs.get("channel"))
        return original_run_scenario(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("bajutsu.common.runner.pipeline.run_scenario", spying_run_scenario)

    collector = NetworkCollector()
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]

    def lease(eff: Effective, s: Scenario) -> Lease:
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=collector,
            release=lambda: None,
        )

    run_all(_eff(), scenarios, lease)
    assert seen == [collector]


def test_run_all_hands_the_target_s_launch_env_to_run_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`eff.launch_env` crosses into `run_scenario` as `target_launch_env` (BE-0365 unit 3 gap).

    A target's own `launchEnv` (e.g. a `BAJUTSU_TOUCH_MARKERS` pin in `targets.<name>.launchEnv`)
    is merged *underneath* each scenario's own at launch (`environments/xcuitest.py`'s
    `_launch_params`). `_hides_touch_markers` has to see the same merge, or a target-level pin
    would look to it like a scenario that pinned nothing — a stray edit that dropped this kwarg
    would silently reopen that gap without failing any other pipeline test.
    """
    from dataclasses import replace

    from bajutsu.common.orchestrator import run_scenario as original_run_scenario

    seen: list[object] = []

    def spying_run_scenario(*args: object, **kwargs: object) -> object:
        seen.append(kwargs.get("target_launch_env"))
        return original_run_scenario(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("bajutsu.common.runner.pipeline.run_scenario", spying_run_scenario)

    eff = replace(_eff(), launch_env={"BAJUTSU_TOUCH_MARKERS": "1"})
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]

    run_all(eff, scenarios, _lease)
    assert seen == [{"BAJUTSU_TOUCH_MARKERS": "1"}]


def test_run_all_alert_guard_for_selects_per_scenario() -> None:
    # The factory picks each scenario's guard from its systemAlertHandling: the guarded scenario
    # recovers from a blocked tap and passes; the one that disabled it fails.
    from bajutsu.common.orchestrator import AlertGuardConfig

    scenarios = [
        Scenario.model_validate(
            {"name": "guarded", "systemAlertHandling": True, "steps": [{"tap": {"id": "later"}}]}
        ),
        Scenario.model_validate(
            {"name": "bare", "systemAlertHandling": False, "steps": [{"tap": {"id": "later"}}]}
        ),
    ]

    def alert_guard_for(s: Scenario) -> AlertGuardConfig | None:
        # `false` is the whole off form since BE-0401; a mapping (or an absent key) means on.
        return None if s.system_alert_handling is False else AlertGuardConfig(rules=[guard_rule()])

    # Answering the prompt reveals the tap's target, so the guarded scenario recovers and passes.
    def lease(eff: Effective, scenario: Scenario) -> Lease:
        return Lease(
            driver=AlertingDriver(
                on_dismiss=lambda d: setattr(d, "screen", [_el("later", "Later", ["button"])])
            ),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    results = run_all(_eff(), scenarios, lease, alert_guard_for=alert_guard_for)
    assert [r.ok for r in results] == [True, False]


def test_run_all_alert_guard_for_uncovered_locale_fails_the_scenario_not_the_run() -> None:
    # A `systemAlertHandling.rules` entry naming a prompt this scenario's locale has no known labels
    # for raises while the guard is still under construction (before any lease is even taken) — the
    # run must turn that into one clean failed RunResult per scenario, not an uncaught exception that
    # would abort every remaining scenario too.
    from bajutsu.common.orchestrator import AlertGuardConfig
    from bajutsu.common.scenario.system_alerts import UncoveredSystemAlertLocale

    scenarios = [
        Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]}),
        Scenario.model_validate({"name": "b", "steps": [{"tap": {"id": "ok"}}]}),
    ]

    def alert_guard_for(s: Scenario) -> AlertGuardConfig | None:
        if s.name == "a":
            raise UncoveredSystemAlertLocale("no labels for language 'fr'")
        return None

    results = run_all(_eff(), scenarios, _lease, alert_guard_for=alert_guard_for)
    assert [r.ok for r in results] == [False, True]  # "a" fails cleanly; "b" still runs
    assert "no labels for language 'fr'" in (results[0].failure or "")


def test_run_all_attributes_each_scenario_to_its_device() -> None:
    scenarios = [
        Scenario.model_validate({"name": n, "steps": [{"tap": {"id": "ok"}}]}) for n in ("a", "b")
    ]

    def lease(eff: Effective, s: Scenario) -> Lease:
        return Lease(
            driver=_fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
            udid=f"DEV-{s.name}",
        )

    results = run_all(_eff(), scenarios, lease, workers=2)
    # Each result records the device that ran it, so the report can show the parallel split.
    assert {r.scenario: r.device for r in results} == {"a": "DEV-a", "b": "DEV-b"}


def test_run_and_report(tmp_path: Path) -> None:
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results, manifest = run_and_report(_eff(), scenarios, _lease, tmp_path / "runs", "run1")
    assert results[0].ok
    assert manifest.exists()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["runId"] == "run1"
    assert (tmp_path / "runs" / "run1" / "junit.xml").exists()
    # The executed scenario is kept alongside its results.
    scn_file = tmp_path / "runs" / "run1" / "scenario.yaml"
    assert scn_file.exists() and "name: a" in scn_file.read_text(encoding="utf-8")
    # The run is stamped with provenance (BE-0049): a fingerprint of the executed scenario YAML
    # (taken pre-redaction) plus the tool version, so accumulated runs group by identity. With no
    # secret_values here nothing is scrubbed, so the stamp also equals a hash of the saved file.
    import hashlib

    from bajutsu import __version__

    prov = data["provenance"]
    expected = "sha256:" + hashlib.sha256(scn_file.read_text(encoding="utf-8").encode()).hexdigest()
    assert prov["scenarioHash"] == expected
    assert prov["toolVersion"] == __version__
    assert "configSource" not in prov  # a local config records no Git source


def test_run_and_report_forwards_on_score(tmp_path: Path) -> None:
    # `run_and_report` threads `--score`'s sink through to the pipeline, so the CLI's `--score` reaches
    # the first lease's grade.
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    scores: list[Score] = []
    run_and_report(_eff(), scenarios, _lease, tmp_path / "runs", "run1", on_score=scores.append)
    assert len(scores) == 1 and scores[0].grade == "Ready"


# --- cross-browser matrix run (BE-0076 Phase 2): run-per-engine -> assemble -> report-once ---


def test_run_matrix_and_report_writes_one_report_with_a_matrix(tmp_path: Path) -> None:
    # Two engines, one scenario: each engine pass writes its evidence under run_dir/<engine>, and
    # the run assembles ONE manifest whose matrix aggregates the per-engine verdicts.
    scenarios = [Scenario.model_validate({"name": "login", "steps": [{"tap": {"id": "ok"}}]})]
    seen: list[tuple[str, Path]] = []

    def run_pass(engine: str, run_dir: Path) -> list[RunResult]:
        seen.append((engine, run_dir))
        # webkit fails the scenario; chromium passes it — a machine-detected incompatibility.
        return run_all(
            _eff(), scenarios, _lease if engine == "chromium" else _failing_lease, run_dir=run_dir
        )

    results, manifest = run_matrix_and_report(
        _eff(), scenarios, ["chromium", "webkit"], run_pass, tmp_path / "runs", "run1"
    )
    # Each engine pass was handed its own run_dir/<engine> subtree, in order.
    assert seen == [
        ("chromium", tmp_path / "runs" / "run1" / "chromium"),
        ("webkit", tmp_path / "runs" / "run1" / "webkit"),
    ]
    # Results are concatenated and tagged with their engine.
    assert [(r.scenario, r.engine, r.ok) for r in results] == [
        ("login", "chromium", True),
        ("login", "webkit", False),
    ]
    # ONE report at run_dir (no per-engine manifest); its matrix block aggregates both verdicts.
    assert manifest == tmp_path / "runs" / "run1" / "manifest.json"
    assert not (tmp_path / "runs" / "run1" / "chromium" / "manifest.json").exists()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["ok"] is False  # all-must-pass: webkit's failure fails the whole run
    matrix = data["matrix"]
    assert matrix["engines"] == ["chromium", "webkit"]
    assert matrix["cells"]["login"]["chromium"]["ok"] is True
    assert matrix["cells"]["login"]["webkit"]["ok"] is False
    # The matrix cell points at the engine-prefixed evidence dir the pass wrote under.
    assert matrix["cells"]["login"]["chromium"]["sid"] == "chromium/00-login"


def test_run_matrix_and_report_owns_the_top_run_dir_before_the_first_engine_pass(
    tmp_path: Path,
) -> None:
    # BE-0131 by way of BE-0331: every write now goes through a sink, which creates the directory it
    # writes into owner-only. A matrix run is the one case that does not cover the top run dir — the
    # first sink creates `run_dir/<engine>`, and `mkdir(parents=True)` would leave the run dir above
    # it at the ambient umask, so every engine's evidence would sit under a world-readable parent.
    import stat

    scenarios = [Scenario.model_validate({"name": "login", "steps": [{"tap": {"id": "ok"}}]})]
    modes: list[int] = []

    def run_pass(engine: str, run_dir: Path) -> list[RunResult]:
        results = run_all(_eff(), scenarios, _lease, run_dir=run_dir)
        modes.append(stat.S_IMODE((tmp_path / "runs" / "run1").stat().st_mode))
        return results

    run_matrix_and_report(
        _eff(), scenarios, ["chromium", "webkit"], run_pass, tmp_path / "runs", "run1"
    )
    # Owner-only already during the first pass, not only once the report is assembled.
    assert modes == [0o700, 0o700]


def test_reroot_evidence_prefixes_paths_with_engine() -> None:
    # Each engine pass writes evidence under <engine>/<sid>/, but artifact/visual paths are recorded
    # relative to that pass's run_dir. The matrix assembles one report at the top run_dir, so the
    # paths must be re-rooted under the engine subtree or the report's links resolve wrong (BE-0076).
    from bajutsu.common.assertions import AssertionResult, VisualEvidence
    from bajutsu.common.evidence import Artifact
    from bajutsu.common.orchestrator import StepOutcome
    from bajutsu.common.runner.pipeline import _reroot_evidence

    r = RunResult(
        scenario="login",
        ok=True,
        steps=[
            StepOutcome(
                index=0,
                action="tap",
                ok=True,
                artifacts=[Artifact("00-login/after.png", "screenshot", "driver")],
            )
        ],
        artifacts=[Artifact("00-login/video.webm", "video", "collector")],
        expect_results=[
            AssertionResult(
                ok=True,
                kind="visual",
                detail="",
                visual=VisualEvidence(
                    baseline_name="home.png",
                    actual="00-login/visual-actual.png",
                    baseline="00-login/visual-baseline.png",
                    diff="00-login/visual-diff.png",
                ),
            )
        ],
    )
    _reroot_evidence(r, "webkit")
    assert r.artifacts[0].name == "webkit/00-login/video.webm"
    assert r.steps[0].artifacts[0].name == "webkit/00-login/after.png"
    v = r.expect_results[0].visual
    assert v is not None
    assert v.actual == "webkit/00-login/visual-actual.png"
    assert v.baseline == "webkit/00-login/visual-baseline.png"
    assert v.diff == "webkit/00-login/visual-diff.png"


def test_run_matrix_and_report_green_only_when_every_engine_passes(tmp_path: Path) -> None:
    scenarios = [Scenario.model_validate({"name": "login", "steps": [{"tap": {"id": "ok"}}]})]

    def run_pass(engine: str, run_dir: Path) -> list[RunResult]:
        return run_all(_eff(), scenarios, _lease, run_dir=run_dir)

    _, manifest = run_matrix_and_report(
        _eff(), scenarios, ["chromium", "firefox", "webkit"], run_pass, tmp_path / "runs", "run1"
    )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["ok"] is True  # every engine passed every scenario


def test_run_and_report_records_git_config_source(tmp_path: Path) -> None:
    # A run from a Git config source stamps which repo@sha it executed into the manifest (BE-0063).
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    src = {"host": "github.com", "owner": "acme", "repo": "tests", "ref": "main", "sha": "deadbeef"}
    _, manifest = run_and_report(
        _eff(), scenarios, _lease, tmp_path / "runs", "run1", config_source=src
    )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["provenance"]["configSource"] == src


def test_run_and_report_records_upload_exec_decision(tmp_path: Path) -> None:
    # BE-0090: an upload-governed run stamps the launchServer policy decision into the manifest.
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    decision: dict[str, str | None] = {
        "decision": "sandboxed",
        "field": "launchServer",
        "source": "dockerImage",
        "image": "img",
    }
    _, manifest = run_and_report(
        _eff(), scenarios, _lease, tmp_path / "runs", "run1", exec_provenance=decision
    )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["provenance"]["uploadExec"] == decision


def test_run_and_report_omits_upload_exec_for_ungoverned_run(tmp_path: Path) -> None:
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    _, manifest = run_and_report(_eff(), scenarios, _lease, tmp_path / "runs", "run1")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert "uploadExec" not in data["provenance"]  # None decision → no key


def test_git_revision_maps_failure_and_blank_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # The subprocess is an external dependency, so it's the one place a stub is warranted. A
    # non-zero exit, a thrown error, and a 0-exit-but-blank stdout (a shimmed `git`) all mean
    # "unknown revision" — None, never an empty stamp. The helper lives with run_provenance
    # (report.manifest) so the pool's wait-timeout diagnostic and the report share it (BE-0231).
    from bajutsu.common.report import manifest

    def fake(
        result: subprocess.CompletedProcess[str] | Exception,
    ) -> Callable[..., subprocess.CompletedProcess[str]]:
        def run(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
            if isinstance(result, Exception):
                raise result
            return result

        return run

    monkeypatch.setattr(subprocess, "run", fake(subprocess.CompletedProcess([], 128, "", "fatal")))
    assert manifest.git_revision() is None  # not a repo
    monkeypatch.setattr(subprocess, "run", fake(subprocess.CompletedProcess([], 0, "   \n", "")))
    assert manifest.git_revision() is None  # 0 exit but blank stdout → unknown, not ""
    monkeypatch.setattr(subprocess, "run", fake(FileNotFoundError("git")))
    assert manifest.git_revision() is None  # git absent
    monkeypatch.setattr(subprocess, "run", fake(subprocess.CompletedProcess([], 0, "abc123\n", "")))
    assert manifest.git_revision() == "abc123"  # normal: trimmed sha


def test_run_and_report_forwards_baselines_dir(tmp_path: Path) -> None:
    # A visual expect with the baselines dir forwarded builds a VisualContext, so a missing
    # baseline reports "baseline not found" — not "no visual context" (the unforwarded bug).
    scenarios = [
        Scenario.model_validate(
            {
                "name": "vis",
                "steps": [{"tap": {"id": "ok"}}],
                "expect": [{"visual": {"baseline": "home.png"}}],
            }
        )
    ]
    results, _ = run_and_report(
        _eff(),
        scenarios,
        _lease,
        tmp_path / "runs",
        "run1",
        baselines_dir=tmp_path / "baselines",
    )
    ev = results[0].expect_results[0]
    assert ev.kind == "visual"
    assert "baseline not found" in ev.reason
    assert ev.visual is not None and ev.visual.missing


def test_run_and_report_forwards_schemas_dir(tmp_path: Path) -> None:
    # A responseSchema expect with schemas_dir forwarded builds a SchemaContext, so the failure
    # gets past the "no schema context" guard (here it fails later, on no matching exchange) —
    # proving the dir was threaded, not dropped.
    scenarios = [
        Scenario.model_validate(
            {
                "name": "rs",
                "steps": [{"tap": {"id": "ok"}}],
                "expect": [
                    {"responseSchema": {"request": {"path": "/api/items"}, "schema": "items.json"}}
                ],
            }
        )
    ]
    results, _ = run_and_report(
        _eff(), scenarios, _lease, tmp_path / "runs", "run1", schemas_dir=tmp_path / "schemas"
    )
    ev = results[0].expect_results[0]
    assert ev.kind == "responseSchema"
    assert "no schema context" not in ev.reason  # context was forwarded


def test_run_and_report_scrubs_secret_values_from_artifacts(tmp_path: Path) -> None:
    """The run-level scrub is the final safety net: a secret that reaches result text (here a
    failing assertion's expected value, interpolated from a binding) must not survive into any
    written artifact, even though the scenario definition only ever holds the token."""
    secret = "S3CR3T-TOKEN"
    scenarios = [
        Scenario.model_validate(
            {
                "name": "a",
                "steps": [{"tap": {"id": "ok"}}],
                "expect": [{"value": {"sel": {"id": "ok"}, "equals": "${secrets.token}"}}],
            }
        )
    ]
    results, _ = run_and_report(
        _eff(),
        scenarios,
        _lease,
        tmp_path / "runs",
        "run1",
        bindings={"secrets.token": secret},
        secret_values=[secret],
    )
    # The assertion failed, so the secret value really did reach the in-memory result text.
    assert not results[0].ok
    assert results[0].failure is not None and secret in results[0].failure
    # ...but it is scrubbed out of every written artifact.
    run_dir = tmp_path / "runs" / "run1"
    for name in ("manifest.json", "junit.xml", "scenario.yaml"):
        assert secret not in (run_dir / name).read_text(encoding="utf-8")


def test_run_and_report_masks_literal_totp_seed_in_artifacts(tmp_path: Path) -> None:
    # A literal base32 TOTP seed written into a scenario is durable credential material, not a
    # one-time code — it must never survive into the run's evidence bundle (BE-0152).
    seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    scenarios = [
        Scenario.model_validate(
            {"name": "a", "steps": [{"totp": {"secret": seed, "into": {"var": "code"}}}]}
        )
    ]
    run_and_report(_eff(), scenarios, _lease, tmp_path / "runs", "run1")
    run_dir = tmp_path / "runs" / "run1"
    for name in ("scenario.yaml", "manifest.json", "report.html"):
        assert seed not in (run_dir / name).read_text(encoding="utf-8")
    assert "<redacted>" in (run_dir / "scenario.yaml").read_text(encoding="utf-8")


def test_run_and_report_keeps_totp_reference_and_scrubs_the_resolved_seed(tmp_path: Path) -> None:
    # A `${secrets.*}` reference stays in the snapshot (reviewable, and not itself the seed), while
    # its resolved value never reaches any artifact — confirming BE-0032 already covers the
    # resolved case that BE-0152's snapshot masking complements.
    seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    scenarios = [
        Scenario.model_validate(
            {
                "name": "a",
                "steps": [{"totp": {"secret": "${secrets.SEED}", "into": {"var": "code"}}}],
            }
        )
    ]
    run_and_report(
        _eff(),
        scenarios,
        _lease,
        tmp_path / "runs",
        "run1",
        bindings={"secrets.SEED": seed},
        secret_values=[seed],
    )
    run_dir = tmp_path / "runs" / "run1"
    assert "${secrets.SEED}" in (run_dir / "scenario.yaml").read_text(encoding="utf-8")
    for name in ("scenario.yaml", "manifest.json", "report.html"):
        assert seed not in (run_dir / name).read_text(encoding="utf-8")


def test_run_and_report_writes_owner_only_artifacts(tmp_path: Path) -> None:
    # BE-0131: a fresh run's directory and its sensitive files (scenario.yaml, network.json) must
    # land owner-only (0700 dir, 0600 files), not world-readable under the ambient umask — evidence
    # can carry secrets, and a shared CI runner is exactly where another local account can read it.
    import stat

    from bajutsu.common.evidence import FileSink

    run_dir = tmp_path / "runs" / "run1"
    ex = NetworkExchange(method="GET", path="/items", status=200)
    scn = Scenario.model_validate(
        {"name": "net", "steps": [{"assert": [{"request": {"method": "GET", "path": "/items"}}]}]}
    )

    def lease(eff: Effective, s: Scenario) -> Lease:
        return Lease(
            driver=FakeDriver([_el("ok", "OK")]),
            sink=FileSink(run_dir),
            relaunch=None,
            control=None,
            collector=_ConstantCollector([ex]),
            release=lambda: None,
        )

    run_and_report(_eff(), [scn], lease, tmp_path / "runs", "run1")

    def mode(p: Path) -> int:
        return stat.S_IMODE(p.stat().st_mode)

    assert mode(run_dir) == 0o700
    assert mode(run_dir / "scenario.yaml") == 0o600
    net = run_dir / "00-net" / "network.json"
    assert net.exists() and mode(net) == 0o600


def test_write_network_stamps_the_given_provider(tmp_path: Path) -> None:
    from bajutsu.common.evidence.redaction import Redactor
    from bajutsu.common.evidence.sink import RunArtifactWriter
    from bajutsu.common.runner.pipeline import _write_network

    ex = NetworkExchange(method="GET", path="/a", status=200)
    art = _write_network(
        [(ex, 1.0)],
        RunArtifactWriter(tmp_path, Redactor(None)),
        "00-s",
        wall_offset_s=0.0,
        provider="fake (fallback)",
    )
    assert art is not None and art.provider == "fake (fallback)"


def test_write_network_started_at_is_an_absolute_wall_clock_instant(tmp_path: Path) -> None:
    # The collector stamps a monotonic receive time; `wall_offset_s` converts it through the
    # scenario's own anchor pair, so network.json records the absolute instant the exchange started
    # (received + offset - duration) rather than an already-relative number (BE-0348). No clamp: an
    # absolute epoch has no floor to clamp to, and the report applies its own at render time.
    from bajutsu.common.evidence.redaction import Redactor
    from bajutsu.common.evidence.sink import RunArtifactWriter
    from bajutsu.common.runner.pipeline import _write_network

    ex = NetworkExchange(method="GET", path="/a", status=200, durationMs=250.0)
    art = _write_network(
        [(ex, 1.0)],
        RunArtifactWriter(tmp_path, Redactor(None)),
        "00-s",
        wall_offset_s=1_700_000_000.0,
    )
    assert art is not None
    data = json.loads((tmp_path / "00-s" / "network.json").read_text(encoding="utf-8"))
    assert data[0]["startedAt"] == 1_700_000_000.75


def test_network_json_anchors_to_the_video_corrected_start(tmp_path: Path) -> None:
    # The scenario's own stamp must not leak into network.json: an exchange's absolute `startedAt`
    # and the scenario's `video_anchor_s` must place it on the same timeline the steps are on, so
    # that the report's render-time derivation reproduces the video-corrected seconds. Proven here
    # by a video whose confirmed `true_start` precedes scenario_start, so an uncorrected anchor
    # would yield a different number than the one derived.
    from bajutsu.common.evidence import FileSink
    from bajutsu.common.evidence.intervals import Interval

    run_dir = tmp_path / "runs" / "run1"
    prestarted_video = tmp_path / "prestart.mp4"
    prestarted_video.write_bytes(b"clip")
    ex = NetworkExchange(method="GET", path="/items", status=200)
    scn = Scenario.model_validate(
        {
            "name": "net",
            "capturePolicy": [{"on": {"result": "error"}, "capture": ["video"]}],
            "steps": [{"assert": [{"request": {"method": "GET", "path": "/items"}}]}],
        }
    )

    def lease(eff: Effective, s: Scenario) -> Lease:
        return Lease(
            driver=FakeDriver([]),
            sink=FileSink(
                run_dir,
                prestarted_intervals=[
                    Interval(kind="video", path=prestarted_video, true_start=-2.5)
                ],
            ),
            relaunch=None,
            control=None,
            collector=_ConstantCollector([ex]),
            release=lambda: None,
        )

    results, _ = run_and_report(
        _eff(), [scn], lease, tmp_path / "runs", "run1", clock=_AdvancingClock()
    )
    data = json.loads((run_dir / "00-net" / "network.json").read_text(encoding="utf-8"))
    # scenario_start is 0.0 on this clock (nothing here calls sleep), so the exchange's own absolute
    # start coincides with the scenario's wall anchor and an uncorrected anchor would derive 0.0. The
    # corrected anchor is 2.5s earlier (video_anchor_s == scenario_wall_start + (-2.5)), so the
    # rendered offset is 2.5 — the same number the pre-BE-0348 in-flight calculation produced.
    # Approximate to the millisecond: network.json rounds `startedAt` to 3 decimals (as it always
    # has), which now quantizes an epoch-magnitude value rather than a small offset.
    assert video_seconds(
        data[0]["startedAt"], video_anchor_s=results[0].video_anchor_s
    ) == pytest.approx(2.5, abs=1e-3)


class _ConstantCollector:
    """A Collector that always reports the same exchanges (clear is a no-op) — test scaffolding so
    provenance/threading can be checked without live traffic during a fake run (BE-0020)."""

    def __init__(self, exchanges: list[NetworkExchange]) -> None:
        self._ex = list(exchanges)

    def snapshot(self) -> list[NetworkExchange]:
        return list(self._ex)

    def snapshot_timed(self) -> list[tuple[NetworkExchange, float]]:
        return [(e, 0.0) for e in self._ex]

    def transitions_snapshot_timed(self) -> list[tuple[ScreenTransition, float]]:
        return []

    def clear(self) -> None:
        pass

    def stop(self) -> None:
        pass


def test_run_all_threads_collector_provider_and_discloses_skips(tmp_path: Path) -> None:
    from bajutsu.common.evidence import FileSink
    from bajutsu.common.orchestrator import SkippedCapture

    ex = NetworkExchange(method="GET", path="/items", status=200)
    scn = Scenario.model_validate(
        {"name": "net", "steps": [{"assert": [{"request": {"method": "GET", "path": "/items"}}]}]}
    )

    def lease(eff: Effective, s: Scenario) -> Lease:
        return Lease(
            driver=FakeDriver([_el("ok", "OK")]),
            sink=FileSink(tmp_path),
            relaunch=None,
            control=None,
            collector=_ConstantCollector([ex]),
            release=lambda: None,
            collector_provider="fake (fallback)",
            skipped_captures=[SkippedCapture("video", "no provider")],
        )

    r = run_all(_eff(), [scn], lease, run_dir=tmp_path)[0]
    assert r.ok
    assert [s.kind for s in r.skipped_captures] == ["video"]
    net = [a for a in r.artifacts if a.kind == "network"]
    assert net and net[0].provider == "fake (fallback)"
    assert (tmp_path / net[0].name).exists()


def test_pipeline_uses_the_single_orchestrator_no_op_network_source() -> None:
    # The runner shares the orchestrator's one no-op NetworkSource rather than owning a copy, so
    # the default "no network was collected" value lives in one place (BE-0251).
    from bajutsu.common.orchestrator.types import _no_network
    from bajutsu.common.runner import pipeline, types

    # `pipeline` re-exports the helper rather than owning a second copy, and this identity check is
    # what pins that. Strict mode's implicit-reexport rule hides an imported name, so the read is
    # suppressed by code rather than the assertion weakened.
    assert pipeline._no_network is _no_network  # type: ignore[attr-defined]
    assert _no_network() == []
    assert not hasattr(types, "_no_net")


def test_the_run_resolves_a_system_alert_prompt_against_the_scenario_locale() -> None:
    # BE-0320: the pipeline hands the run loop the same locale the lease pinned the Simulator's
    # system language to, so `handleSystemAlert`'s intent form taps the label SpringBoard renders.
    # The per-scenario `locale` override wins over the target config's, exactly as the launch
    # arguments and the Simulator pin resolve it.
    driver = FakeDriver([_el("ok", "OK", ["button"])])
    driver.system_alert_buttons = [
        {
            "identifier": None,
            "label": label,
            "traits": ["button"],
            "value": None,
            "frame": (0.0, 0.0, 10.0, 10.0),
            "nativeZ": None,
        }
        for label in ("許可", "許可しない")
    ]

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        return Lease(
            driver=driver,
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenario = Scenario.model_validate(
        {
            "name": "grant",
            "preconditions": {"locale": "ja_JP"},  # overrides `_eff()`'s en_US
            "steps": [
                {"handleSystemAlert": {"prompt": "notifications", "choice": "grant", "timeout": 5}}
            ],
        }
    )

    results = run_all(_eff(), [scenario], lease)

    assert [r.ok for r in results] == [True], results[0].failure
    assert driver.actions == [("handle_system_alert", ({"label": "許可"}, 0.0))]


def test_trace_driver_writes_driver_trace_json(tmp_path: Path) -> None:
    # `bajutsu run --trace-driver` (BE-0415): one `<sid>/driver_trace.json` per scenario, attributing
    # every `driver`-category record to the step it happened during. The `fake` backend produces no
    # `transport`/`subprocess` records (no host-device round trip to measure), so this proves the
    # `driver`-category wrap and the file's own shape; `tests/test_driver_tracing.py` covers the
    # transport/subprocess wraps directly against `XcuitestDriver`/`AdbDriver`.
    scenario = Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})
    run_dir = tmp_path / "runs" / "run1"
    results = run_all(_eff(), [scenario], _lease, run_dir=run_dir, trace_driver=True)
    assert results[0].ok, results[0].failure
    trace_path = run_dir / results[0].sid / "driver_trace.json"
    assert trace_path.is_file()
    doc = json.loads(trace_path.read_text(encoding="utf-8"))
    assert doc["scenario"] == "a"
    assert doc["steps"]  # one entry for the scenario's one `tap` step
    driver_records = [r for r in doc["records"] if r["category"] == "driver"]
    assert any(r["name"] == "tap" for r in driver_records)
    assert all(r["step"] == "00:tap" for r in driver_records)
    # `TracingDriver` must preserve `ActuationReporter` too (not just the two capability protocols
    # BE-0407's trace_run.py measured): missing it silently drops every step's own actuation
    # evidence for the whole run once `--trace-driver` is on.
    assert results[0].steps[0].actuations


def test_trace_driver_off_by_default_writes_no_file(tmp_path: Path) -> None:
    scenario = Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})
    run_dir = tmp_path / "runs" / "run1"
    results = run_all(_eff(), [scenario], _lease, run_dir=run_dir)
    assert results[0].ok, results[0].failure
    assert not (run_dir / results[0].sid / "driver_trace.json").exists()


def test_trace_driver_attributes_records_to_their_crash_recovery_attempt(tmp_path: Path) -> None:
    # One `TraceContext` spans every crash-recovery attempt, but each attempt's own `StepLoopState`
    # restarts its step counter at 0 — so without an explicit attempt number, attempt 1's crashed
    # "00:tap" and attempt 2's recovered "00:tap" would be indistinguishable in the trace.
    lease, _events = _crash_then_ok_lease()
    scenario = Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})
    run_dir = tmp_path / "runs" / "run1"
    results = run_all(_eff(), [scenario], lease, run_dir=run_dir, trace_driver=True)
    assert results[0].ok, results[0].failure
    doc = json.loads((run_dir / results[0].sid / "driver_trace.json").read_text(encoding="utf-8"))
    tap_records = [r for r in doc["records"] if r["name"] == "tap"]
    assert {r["attempt"] for r in tap_records} == {1, 2}
    assert {s["attempt"] for s in doc["steps"]} == {1, 2}


def test_trace_driver_stamps_the_attempt_before_leasing() -> None:
    # The attempt number must be stamped *before* `self.lease(...)` runs: that call is what
    # constructs the attempt's own driver (a cold respawn on attempt >= 2), and a real
    # `XcuitestDriver`/`AdbDriver` installs its transport/subprocess wraps at construction time —
    # stamped any later, a respawn's own readiness round trips would be misattributed to the
    # previous attempt. `FakeDriver` does no such construction-time wrapping, so this checks the
    # context's `attempt` value at the moment `lease()` itself is called, directly.
    seen_attempts: list[int | None] = []

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        ctx = tracing.current_trace()
        seen_attempts.append(None if ctx is None else ctx.attempt)
        driver = _crashing_driver() if len(seen_attempts) == 1 else _fake_driver()
        return Lease(
            driver=driver,
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    scenario = Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})
    results = run_all(_eff(), [scenario], lease, trace_driver=True)
    assert results[0].ok, results[0].failure
    assert seen_attempts == [1, 2]


def test_trace_driver_write_failure_is_warned_about_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Diagnostic only, like `--zip` / `--evidence-store`: a failed `driver_trace.json` write must
    # not end the run or displace the scenario's own in-flight result/exception.
    from bajutsu.common.evidence.sink import RunArtifactWriter

    def _boom(self: RunArtifactWriter, name: str, data: object) -> Path:
        raise OSError("disk full (test)")

    monkeypatch.setattr(RunArtifactWriter, "write_json", _boom)
    scenario = Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})
    run_dir = tmp_path / "runs" / "run1"
    results = run_all(_eff(), [scenario], _lease, run_dir=run_dir, trace_driver=True)
    assert results[0].ok, results[0].failure
    assert not (run_dir / results[0].sid / "driver_trace.json").exists()


# --- the failed scenario's own copy of the backend's crash evidence (BE-0421) --- #
#
# A scenario whose crash-recovery retries all exhausted used to report only that the backend died. The
# environment behind the crashed lease holds the runner's captured output and, on a genuine process
# fault, the host crash report; the pipeline copies both into that scenario's evidence directory and
# points the failure string at them. It asks exactly once, where the retry loop has given up.

_CRASH_EVIDENCE = [("runner.log", b"the runner's last words\n"), ("xcodebuild.ips", b'{"pid":1}')]


def _crash_evidence_lease(*, crashes: int) -> Callable[[Effective, Scenario], Lease]:
    """A lease factory whose first `crashes` attempts crash, each carrying the same crash evidence."""
    state = {"n": 0}

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        return Lease(
            driver=_crashing_driver() if state["n"] <= crashes else _fake_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
            crash_artifacts=lambda: list(_CRASH_EVIDENCE),
        )

    return lease


def test_a_crash_exhausted_scenario_keeps_the_backends_own_crash_evidence(tmp_path: Path) -> None:
    # The observable outcome BE-0421 exists for: the evidence lands beside the screenshots and element
    # trees the same run already produced, and the failure string names the directory, so a contributor
    # reading why the scenario failed never has to already know it is there.
    run_dir = tmp_path / "runs" / "run1"
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(
        _eff(), scenarios, _crash_evidence_lease(crashes=2), crash_retries=1, run_dir=run_dir
    )
    directory = run_dir / "00-a" / "crash-diagnostics"
    assert (directory / "runner.log").read_text(encoding="utf-8") == "the runner's last words\n"
    assert (directory / "xcodebuild.ips").exists()
    assert str(directory) in (results[0].failure or "")


def test_a_recovered_scenario_keeps_no_crash_evidence(tmp_path: Path) -> None:
    # A scenario that recovers within its retry budget passes, and a passing scenario needs no crash
    # report — copying after each attempt would leave it carrying evidence for a failure its own final
    # result no longer reports.
    run_dir = tmp_path / "runs" / "run1"
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(
        _eff(), scenarios, _crash_evidence_lease(crashes=1), crash_retries=1, run_dir=run_dir
    )
    assert results[0].ok
    assert not (run_dir / "00-a" / "crash-diagnostics").exists()


def test_a_backend_that_captures_nothing_leaves_the_failure_untouched(tmp_path: Path) -> None:
    # Android, the web backend, and the fake one all return `[]`, so the write step is an empty
    # iteration and the failure string says exactly what it said before this feature existed.
    run_dir = tmp_path / "runs" / "run1"
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        return Lease(
            driver=_crashing_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
        )

    results = run_all(_eff(), scenarios, lease, crash_retries=0, run_dir=run_dir)
    assert not (run_dir / "00-a" / "crash-diagnostics").exists()
    failure = results[0].failure or ""
    assert "crashed mid-run" in failure and "crash-diagnostics" not in failure


def test_a_failed_crash_evidence_write_does_not_displace_the_scenarios_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A diagnostic artifact must never turn an already-decided failure into a different, unrelated one:
    # a full disk or a permissions error is warned about, and the scenario still fails for the crash it
    # actually failed for.
    from bajutsu.common.evidence.sink import RunArtifactWriter

    def refuse(self: RunArtifactWriter, name: str, text: str) -> Path:
        raise OSError("no space left on device (test)")

    monkeypatch.setattr(RunArtifactWriter, "write_text", refuse)
    run_dir = tmp_path / "runs" / "run1"
    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    with caplog.at_level("WARNING"):
        results = run_all(
            _eff(), scenarios, _crash_evidence_lease(crashes=1), crash_retries=0, run_dir=run_dir
        )
    failure = results[0].failure or ""
    assert "crashed mid-run" in failure and "crash-diagnostics" not in failure
    assert "writing the crash artifact runner.log failed" in caplog.text


def test_a_lease_time_crash_keeps_the_earlier_attempts_crash_evidence(tmp_path: Path) -> None:
    # `lz` is reset to None at the top of every attempt, so a loop whose last attempt crashed during
    # bring-up — before there was a lease to hold — would otherwise drop the evidence an earlier
    # attempt's environment is still holding for the very crash this scenario failed for.
    run_dir = tmp_path / "runs" / "run1"
    state = {"n": 0}

    def lease(eff: Effective, scenario: Scenario) -> Lease:
        state["n"] += 1
        if state["n"] > 1:
            raise base.BackendCrashError("runner crashed during the readiness gate (test)")
        return Lease(
            driver=_crashing_driver(),
            sink=NullSink(),
            relaunch=None,
            control=None,
            collector=None,
            release=lambda: None,
            crash_artifacts=lambda: list(_CRASH_EVIDENCE),
        )

    scenarios = [Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})]
    results = run_all(_eff(), scenarios, lease, crash_retries=1, run_dir=run_dir)
    assert not results[0].ok
    directory = run_dir / "00-a" / "crash-diagnostics"
    assert (directory / "runner.log").exists()
    assert str(directory) in (results[0].failure or "")
