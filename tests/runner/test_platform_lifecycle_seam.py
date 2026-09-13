"""Tests for the per-platform Environment seam (BE-0009 Phase 0).

The iOS simctl sequence is exercised through `launch_driver` in test_launch.py; these cover the
seam itself: the factory's actuator→Environment selection and the web/fake lifecycles that the
single-fork refactor folded behind the Protocol.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _runner import _eff, _ios_eff, _web_eff

from bajutsu.common.backend_cli import simctl
from bajutsu.common.drivers import base
from bajutsu.common.drivers.fake import FakeDriver
from bajutsu.common.platform_lifecycle import (
    AndroidEnvironment,
    FakeEnvironment,
    WebEnvironment,
    XcuitestEnvironment,
    environment_for,
)
from bajutsu.common.scenario import Preconditions, Relaunch, Scenario


def _capture_group_signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record the `(pgid, signal)` pairs a discard sends, without signalling this host's processes.

    The runner is terminated by process group (so the XCTest-host children `xcodebuild` spawned go
    with it), and a fake process's pid would name some *real* group on the test host — so both the
    lookup and the signal are replaced here.
    """
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr("os.getpgid", lambda pid: 4242)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: signalled.append((pgid, sig)))
    return signalled


def test_environment_for_selects_by_actuator() -> None:
    assert isinstance(environment_for("xcuitest", "UDID"), XcuitestEnvironment)
    assert isinstance(environment_for("playwright", "UDID"), WebEnvironment)
    assert isinstance(environment_for("fake", "UDID"), FakeEnvironment)


def test_every_environment_satisfies_both_lease_surfaces() -> None:
    # BE-0197 splits the one Protocol into a run-lease surface and a crawl-lease surface so each
    # command declares only the methods it uses; every concrete environment still satisfies both
    # (and the combined `Environment`), which is what lets `environment_for` feed either consumer.
    from bajutsu.common.platform_lifecycle import CrawlEnvironment, Environment, RunEnvironment

    for actuator in ("xcuitest", "playwright", "fake", "adb"):
        env = environment_for(actuator, "UDID")
        assert isinstance(env, RunEnvironment)
        assert isinstance(env, CrawlEnvironment)
        assert isinstance(env, Environment)


def test_only_the_xcuitest_simulator_backend_captures_anything_for_a_crash() -> None:
    # BE-0421: the pipeline copies a crashed backend's own evidence into the failed scenario's
    # directory without branching on the platform, which holds only because every environment answers.
    # A fresh environment has nothing to hand over — the capture happens where a crash is observed —
    # and on every platform but the Simulator XCUITest backend that stays true for the whole run.
    for actuator in ("xcuitest", "playwright", "fake", "adb"):
        assert environment_for(actuator, "UDID").take_crash_snapshot()() == []


def test_captures_video_is_true_for_the_simctl_backed_devices() -> None:
    # The `record` bug BE-0256 fixes: the simctl-backed iOS device (xcuitest) can record a
    # scenario-wide video, so `captures_video` reads it from the Environment seam rather than a
    # per-actuator name test. A platform that genuinely cannot record (fake: no device; web: captured
    # by other means) stays False.
    assert environment_for("xcuitest", "UDID").captures_video() is True
    assert environment_for("fake", "UDID").captures_video() is False
    assert environment_for("playwright", "UDID").captures_video() is False
    assert environment_for("adb", "UDID").captures_video() is False


def test_resolve_device_routes_through_the_platform_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each platform resolves a device handle its own way behind the seam (BE-0256): the iOS family
    # via simctl, Android via adb, web (no device) as a passthrough — no actuator string at the call
    # site.
    monkeypatch.setattr(simctl, "resolve_udid", lambda udid, run=None: f"simctl:{udid}")
    from bajutsu.common.backend_cli import adb

    monkeypatch.setattr(adb, "resolve_serial", lambda serial, run=None: f"adb:{serial}")

    assert environment_for("xcuitest", "UDID").resolve_device("booted") == "simctl:booted"
    assert environment_for("xcuitest", "UDID").resolve_device("booted") == "simctl:booted"
    assert environment_for("adb", "UDID").resolve_device("emulator-5554") == "adb:emulator-5554"
    assert environment_for("playwright", "UDID").resolve_device("anything") == "anything"


def test_web_environment_requires_base_url() -> None:
    eff = _web_eff(base_url=None)
    with pytest.raises(simctl.DeviceError, match="baseUrl"):
        WebEnvironment("playwright").start(eff, Preconditions())


def test_web_environment_navigates_then_returns_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    class _WebDriver(FakeDriver):
        name = "web"

        def __init__(self) -> None:
            super().__init__([])
            self.navigated = False

        def navigate(self) -> None:
            self.navigated = True

        def query(self) -> list[base.Element]:
            return []

    web = _WebDriver()
    monkeypatch.setattr("bajutsu.common.backends.make_driver", lambda *a, **k: web)
    eff = _web_eff(base_url="https://app.test")
    driver = WebEnvironment("playwright").start(eff, Preconditions())
    assert driver is web
    assert web.navigated is True  # the web "launch" is navigate()


def test_web_relaunch_forwards_id_namespaces_to_await_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The web `relaunch` lifecycle path must thread `idNamespaces` into the readiness gate too, so a
    # target does not behave inconsistently between its crawl_reset and relaunch paths.
    from dataclasses import replace as dc_replace

    seen: dict[str, object] = {}

    def fake_await_ready(driver: object, *a: object, **kw: object) -> None:
        seen.update(kw)

    monkeypatch.setattr("bajutsu.common.platform_lifecycle.readiness.await_ready", fake_await_ready)

    class _WebDriver(FakeDriver):
        name = "web"

        def navigate(self) -> None:
            pass

        def query(self) -> list[base.Element]:
            return []

    eff = dc_replace(_web_eff(base_url="https://app.test"), id_namespaces=["app"])
    scn = Scenario.model_validate({"name": "a", "steps": [{"tap": {"id": "ok"}}]})
    relaunch = WebEnvironment("playwright").relauncher(eff, scn, _WebDriver())
    relaunch(Relaunch())
    assert seen.get("id_namespaces") == ["app"]


def test_fake_environment_runs_no_lifecycle() -> None:
    # No device lifecycle (no env_run, no simctl): it just yields the fake driver.
    driver = FakeEnvironment("fake", "UDID").start(_eff(), Preconditions())
    assert driver.query() == []  # the real fake driver, constructed without any device step


def test_fake_environment_rejects_permissions() -> None:
    # No mechanism to apply `permissions` (BE-0276); preflight normally rejects this first, but a
    # caller that drives a lease directly (capabilities=None in runner/pipeline.py) bypasses it —
    # this is the runtime backstop, the same shape as an unsupported gesture.
    with pytest.raises(base.UnsupportedAction):
        FakeEnvironment("fake", "UDID").start(
            _eff(), Preconditions(), permissions={"camera": "grant"}
        )


def test_web_environment_rejects_permissions() -> None:
    eff = _web_eff(base_url="https://app.test")
    with pytest.raises(base.UnsupportedAction):
        WebEnvironment("playwright").start(eff, Preconditions(), permissions={"camera": "grant"})


def test_ios_environment_surfaces_a_failing_step_as_device_error() -> None:
    import subprocess

    def fake_run(args: list[str], extra_env: object = None) -> str:
        if args[:3] == ["xcrun", "simctl", "erase"]:
            raise subprocess.CalledProcessError(1, args, output="", stderr="boom")
        return ""

    with pytest.raises(simctl.DeviceError):
        XcuitestEnvironment("xcuitest", "UDID", env_run=fake_run).start(
            _eff(), Preconditions(erase=True)
        )


# --- The lease-shaping capabilities the pool used to branch on `is_web` for (BE-0009 Slice 2) --- #


def test_network_observation_strategy_is_per_platform() -> None:
    # Web observes by hooking the live driver/page; the device backends use an external receiver the
    # app reports to. This capability — not the actuator name — is what the pool branches on now.
    assert WebEnvironment("playwright").observes_network_via_driver() is True
    assert XcuitestEnvironment("xcuitest", "UDID").observes_network_via_driver() is False
    assert FakeEnvironment("fake", "UDID").observes_network_via_driver() is False


def test_only_android_mirrors_the_collector_port_onto_the_device() -> None:
    # Android's `adb reverse tcp:<port> tcp:<port>` binds the same number inside the guest, so its
    # collector port must be free on both sides and comes from a reserved band. Everything else
    # shares the host's loopback (or has no device at all) and keeps the OS-chosen port, so the band
    # is scoped to the one backend that needs it rather than applied to every device lane.
    assert AndroidEnvironment("adb", "SER").mirrors_collector_port_on_device() is True
    assert XcuitestEnvironment("xcuitest", "UDID").mirrors_collector_port_on_device() is False
    assert WebEnvironment("playwright").mirrors_collector_port_on_device() is False
    assert FakeEnvironment("fake", "UDID").mirrors_collector_port_on_device() is False


def test_devices_and_web_record_video_up_front_but_the_fake_does_not() -> None:
    # Video capture is wired before launch so the app's cold start is recorded: web binds it to the
    # browser context at creation, and Android starts recording before the app launches. XCUITest's
    # app launch lives inside the xcodebuild runner spawn, a separate path that still records on
    # demand (no regression — idb, the only backend with the up-front path, is retired by BE-0290).
    # The fake backend has no device to record, so it stays on the on-demand default too.
    assert WebEnvironment("playwright").records_video_up_front() is True
    assert AndroidEnvironment("adb", "SER").records_video_up_front() is True
    assert XcuitestEnvironment("xcuitest", "UDID").records_video_up_front() is False
    assert FakeEnvironment("fake", "UDID").records_video_up_front() is False


def test_device_catalog_is_empty_for_web_and_read_for_devices() -> None:
    assert WebEnvironment("playwright").device_catalog() == {}

    import json

    catalog = json.dumps(
        {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-17-2": [{"udid": "U", "name": "X"}]}}
    )
    seen = XcuitestEnvironment(
        "xcuitest",
        "U",
        env_run=lambda args, extra_env: catalog if args == simctl.list_devices_cmd() else "",
    ).device_catalog()
    assert seen.get("U") == {"name": "X", "runtime": "iOS 17.2"}


def test_web_hook_collector_wires_the_scenarios_mocks() -> None:
    from bajutsu.common.scenario import Scenario

    class _Collector:
        def snapshot(self) -> list[object]:
            return []

        def snapshot_timed(self) -> list[object]:
            return []

        def transitions_snapshot_timed(self) -> list[object]:
            return []

        def clear(self) -> None:
            pass

        def stop(self) -> None:
            pass

    # Not a `FakeDriver` subclass, unlike the other stubs here: `network_collector` returns this
    # file's own `_Collector`, which would clash with the base's narrower return type.
    class _WebDriver:
        name = "web"

        def __init__(self) -> None:
            self.mocks: object = "unset"

        def network_collector(self, mocks: object = None) -> _Collector:
            self.mocks = mocks
            return _Collector()

    driver = _WebDriver()
    scn = Scenario.model_validate(
        {"name": "a", "mocks": [{"match": {"path": "/x"}}], "steps": [{"tap": {"id": "ok"}}]}
    )
    collector = WebEnvironment("playwright").hook_collector(driver, scn)  # type: ignore[arg-type]
    assert isinstance(collector, _Collector)
    assert driver.mocks == scn.mocks  # this scenario's mocks reached the page hook


def test_web_controller_is_none_and_teardown_closes_the_browser() -> None:
    class _WebDriver(FakeDriver):
        name = "web"

        def __init__(self) -> None:
            super().__init__([])
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    web = WebEnvironment("playwright")
    assert web.controller(_eff()) is None  # no simctl device control on web
    driver = _WebDriver()
    web.teardown(driver, _eff())
    assert driver.closed == 1  # release tears the browser down


def test_device_teardown_terminates_the_app() -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], extra_env: object = None) -> str:
        calls.append(args)
        return ""

    from bajutsu.common.drivers.fake import FakeDriver

    XcuitestEnvironment("xcuitest", "UDID-1", env_run=fake_run).teardown(FakeDriver([]), _eff())
    assert ["xcrun", "simctl", "terminate", "UDID-1", "com.example.demo"] in calls


# --- The crawl-facing seams the CLI used to branch on `actuator == "playwright"` for (Slice 3) --- #


def test_crawl_health_seams_are_web_only() -> None:
    # The crawl's web crash signal, dialog auto-clear, and wedged-browser recovery exist only on web;
    # the device backends read the accessibility tree (engine default) and have no such seams. The CLI
    # now asks the Environment for these instead of naming the actuator.
    web = WebEnvironment("playwright")
    assert web.crawl_aliveness() is not None
    assert web.crawl_recover() is not None
    assert web.crawl_dialog_clearer() is not None
    for device in (XcuitestEnvironment("xcuitest", "U"), FakeEnvironment("fake", "U")):
        assert device.crawl_aliveness() is None
        assert device.crawl_recover() is None
        assert device.crawl_dialog_clearer() is None


def test_web_crawl_seams_drive_the_web_driver() -> None:
    class _WebDriver(FakeDriver):
        name = "web"

        def __init__(self) -> None:
            super().__init__([])
            self.relaunched = 0
            self.errored = False

        def pop_dialogs(self) -> list[str]:
            return ["alert: hi"]

        def relaunch(self) -> None:
            self.relaunched += 1

        def pop_page_errors(self) -> list[str]:
            return ["boom"] if self.errored else []

        def last_nav_status(self) -> int | None:
            return 200

    web = WebEnvironment("playwright")
    driver = _WebDriver()

    clear = web.crawl_dialog_clearer()
    assert clear is not None
    assert clear(driver) == ["alert: hi"]  # delegates to pop_dialogs

    recover = web.crawl_recover()
    assert recover is not None
    recover(driver)
    assert driver.relaunched == 1  # delegates to the driver's relaunch

    alive = web.crawl_aliveness()
    assert alive is not None
    element: base.Element = {
        "identifier": "root",
        "label": None,
        "traits": [],
        "value": None,
        "frame": (0.0, 0.0, 1.0, 1.0),
        "nativeZ": None,
    }
    assert alive(driver, [element]) is True  # no page error, 2xx nav, a rendered element → alive
    driver.errored = True
    assert alive(driver, [element]) is False  # a JS error → not alive (delegates to web_is_alive)


def test_web_crawl_reset_makes_a_fresh_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # The web "reset to a clean start" is a fresh BrowserContext (the erase equivalent), then a
    # readiness wait. (await_ready is stubbed so the test doesn't poll a fake driver.)
    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.readiness.await_ready", lambda *a, **k: None
    )

    class _WebDriver(FakeDriver):
        name = "web"

        def __init__(self) -> None:
            super().__init__([])
            self.reset = 0

        def reset_context(self) -> None:
            self.reset += 1

    driver = _WebDriver()
    WebEnvironment("playwright").crawl_reset(_eff())(driver)
    assert driver.reset == 1


def test_device_crawl_reset_relaunches_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    # The device "reset" is a relaunch (terminate then launch), not a full erase — fast per visit.
    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.readiness.await_ready", lambda *a, **k: None
    )
    calls: list[list[str]] = []

    def fake_run(args: list[str], extra_env: object = None) -> str:
        calls.append(args)
        return ""

    from bajutsu.common.drivers.fake import FakeDriver

    XcuitestEnvironment("xcuitest", "U-1", env_run=fake_run).crawl_reset(_eff())(FakeDriver([]))
    assert ["xcrun", "simctl", "terminate", "U-1", "com.example.demo"] in calls
    assert any(a[:3] == ["xcrun", "simctl", "launch"] and "U-1" in a for a in calls)


def test_plan_lanes_sizes_web_and_device_pools() -> None:
    # Web has no devices, so the worker count alone sizes the browser-lane set; a device pool resolves
    # the --udid list and caps the workers to it. The CLI no longer special-cases the web string.
    web = WebEnvironment("playwright")
    assert web.plan_lanes("booted", 3) == ["web", "web", "web"]
    assert web.plan_lanes("booted", 0) == ["web"]  # at least one lane

    device = XcuitestEnvironment(
        "xcuitest", ""
    )  # explicit udids resolve to themselves (no simctl call)
    assert device.plan_lanes("U1,U2", 5) == ["U1", "U2"]  # capped to the pool
    assert device.plan_lanes("U1,U2,U3", 2) == ["U1", "U2"]  # capped to the workers


def test_only_device_platforms_have_devices() -> None:
    assert WebEnvironment("playwright").has_devices() is False
    assert XcuitestEnvironment("xcuitest", "U").has_devices() is True
    assert FakeEnvironment("fake", "U").has_devices() is True


# --- BE-0019: XcuitestEnvironment ---


def test_xcuitest_environment_requires_test_runner_in_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no testRunner configured, resolution falls back to the wheel-bundled runner (BE-0292);
    # this test asserts the *no-bundle* case, so stub the bundle probe to None — otherwise a dev who
    # has staged the bundle locally (`make runner-bundle`) would have `start()` spawn a real runner
    # instead of raising, making the gate depend on the ambient tree.
    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.environments.xcuitest._functions.bundled_products_dir",
        lambda: None,
    )
    xe = XcuitestEnvironment("xcuitest", "UDID", env_run=lambda a, extra_env: "")
    with pytest.raises(simctl.DeviceError, match="testRunner"):
        xe.start(_eff(), Preconditions())


def test_xcuitest_environment_start_launches_runner_and_creates_driver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:

    from bajutsu.common.config import XcuitestConfig

    simctl_calls: list[list[str]] = []

    def fake_run(args: list[str], extra_env: object = None) -> str:
        simctl_calls.append(args)
        return ""

    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.environments.xcuitest.xcuitest_environment._allocate_port",
        lambda: 54321,
    )

    popen_calls: list[dict[str, Any]] = []

    class FakePopen:
        def __init__(self, cmd: list[str], **kwargs: object) -> None:
            popen_calls.append({"cmd": cmd, "kwargs": kwargs})

        def poll(self) -> int | None:
            return None  # alive: the cold-spawn liveness check (BE-0319) never trips

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr("subprocess.Popen", FakePopen)

    class FakeXcuitestDriver(FakeDriver):
        name = "xcuitest"
        ready_called = False

        def health_ready(self) -> bool:
            self.ready_called = True  # the cold spawn probes readiness here now (BE-0319)
            return True

        def await_ready(self, **kw: object) -> None: ...

        def query(self) -> list[base.Element]:
            return []

        def capabilities(self) -> set[str]:
            return set()

    fake_driver = FakeXcuitestDriver()
    make_driver_calls: list[dict[str, Any]] = []

    def mock_make_driver(*a: object, **k: object) -> FakeXcuitestDriver:
        make_driver_calls.append({"args": a, "kwargs": k})
        return fake_driver

    monkeypatch.setattr("bajutsu.common.backends.make_driver", mock_make_driver)

    import plistlib
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".xctestrun") as f:
        plistlib.dump({"__xctestrun_metadata__": {"FormatVersion": 1}, "T": {}}, f)
        f.flush()
        eff = _ios_eff(xcuitest=XcuitestConfig(testRunner=f.name), app_path=None)
        xe = XcuitestEnvironment("xcuitest", "UDID-1", env_run=fake_run)
        driver = xe.start(eff, Preconditions())

    assert driver is fake_driver
    assert fake_driver.ready_called
    assert len(popen_calls) == 1
    assert popen_calls[0]["cmd"][0] == "xcodebuild"
    assert "UDID-1" in str(popen_calls[0]["cmd"])
    popen_env = popen_calls[0]["kwargs"].get("env", {})
    assert isinstance(popen_env, dict) and popen_env.get("BAJUTSU_RUNNER_PORT") == "54321"
    assert make_driver_calls[0]["kwargs"].get("runner_port") == 54321


def test_xcuitest_environment_applies_permissions_before_the_runner_launches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # BE-0276: xcuitest's simctl-backed pre-launch sequence applies `permissions` before the
    # xcodebuild runner process (and thus the app) ever starts.
    from bajutsu.common.config import XcuitestConfig

    simctl_calls: list[list[str]] = []

    def fake_run(args: list[str], extra_env: object = None) -> str:
        simctl_calls.append(args)
        return ""

    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.environments.xcuitest.xcuitest_environment._allocate_port",
        lambda: 54321,
    )

    popen_started_after_privacy: list[bool] = []

    class FakePopen:
        def __init__(self, cmd: list[str], **kwargs: object) -> None:
            popen_started_after_privacy.append(
                any(c[:3] == ["xcrun", "simctl", "privacy"] for c in simctl_calls)
            )

        def poll(self) -> int | None:
            return None  # alive: the cold-spawn liveness check (BE-0319) never trips

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr("subprocess.Popen", FakePopen)

    class FakeXcuitestDriver(FakeDriver):
        name = "xcuitest"

        def health_ready(self) -> bool:
            return True

        def await_ready(self, **kw: object) -> None: ...

    monkeypatch.setattr("bajutsu.common.backends.make_driver", lambda *a, **k: FakeXcuitestDriver())

    import plistlib
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".xctestrun") as f:
        plistlib.dump({"__xctestrun_metadata__": {"FormatVersion": 1}, "T": {}}, f)
        f.flush()
        eff = _ios_eff(xcuitest=XcuitestConfig(testRunner=f.name), app_path=None)
        xe = XcuitestEnvironment("xcuitest", "UDID-1", env_run=fake_run)
        xe.start(eff, Preconditions(), permissions={"camera": "grant"})

    assert popen_started_after_privacy == [True]
    assert any(c[:3] == ["xcrun", "simctl", "privacy"] and c[4] == "grant" for c in simctl_calls)


def test_xcuitest_environment_teardown_stops_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    import signal

    class FakeProc:
        pid = 777

        def poll(self) -> int | None:
            return None  # alive: _discard_runner then terminates it

        def terminate(self) -> None:
            raise AssertionError("the group signal is the terminate path, not the bare process")

        def wait(self, timeout: float | None = None) -> int:
            return 0

    calls: list[list[str]] = []

    def fake_run(args: list[str], extra_env: object = None) -> str:
        calls.append(args)
        return ""

    signalled = _capture_group_signals(monkeypatch)
    xe = XcuitestEnvironment("xcuitest", "UDID-1", env_run=fake_run)
    xe._runner_proc = FakeProc()  # type: ignore[assignment]

    from bajutsu.common.drivers.fake import FakeDriver

    xe.teardown(FakeDriver([]), _eff())
    # The group gets SIGTERM, then SIGKILL even though the leader exited within the grace: the
    # leader's exit says nothing about the XCTest-host children this signals for.
    assert signalled == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert ["xcrun", "simctl", "terminate", "UDID-1", "com.example.demo"] in calls


def test_discard_reaps_a_leader_that_outlives_the_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An `xcodebuild` finalizing a failed run can outlive SIGTERM. The group is killed either way, so
    # what this pins is the second `wait`: the killed leader is reaped rather than left a zombie.
    import signal
    import subprocess

    waits: list[float | None] = []

    class StubbornProc:
        pid = 778

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired("xcodebuild", timeout or 0)
            return 0

    signalled = _capture_group_signals(monkeypatch)
    xe = XcuitestEnvironment("xcuitest", "UDID-1", env_run=lambda _a, _e: "")
    xe._runner_proc = StubbornProc()  # type: ignore[assignment]
    xe._discard_runner(warn_on_crash=False)
    assert signalled == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert waits == [5, 5]  # the grace, then the reap after the kill
    assert xe._runner_proc is None


def test_discard_sweeps_the_group_even_when_the_leader_exits_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gap a leader-gated escalation leaves open: `xcodebuild` can unwind promptly on the SIGTERM
    # while an XCTest-host child ignores it and keeps holding the device's automation session — the
    # state the next spawn attempt must not inherit. So the SIGKILL sweep is unconditional, not an
    # escalation the leader's prompt exit can skip.
    import signal

    class PromptProc:
        pid = 781

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0  # the leader is gone within the grace; its children may not be

    signalled = _capture_group_signals(monkeypatch)
    xe = XcuitestEnvironment("xcuitest", "UDID-1", env_run=lambda _a, _e: "")
    xe._runner_proc = PromptProc()  # type: ignore[assignment]
    xe._discard_runner(warn_on_crash=False)
    assert signalled == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_discard_survives_a_process_group_that_is_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A discard runs on the failure path, where raising would mask the error that caused it. A group
    # that has already been reaped must therefore be tolerated, not propagated — including on the
    # unconditional sweep, which by design often finds nothing left to signal.
    signals: list[str] = []

    class GoneProc:
        pid = 779

        def poll(self) -> int | None:
            return None  # still looks alive to us; the group is already gone

        def terminate(self) -> None:
            signals.append("terminate")
            raise ProcessLookupError("gone")

        def kill(self) -> None:
            signals.append("kill")
            raise ProcessLookupError("gone")

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr("os.getpgid", lambda pid: (_ for _ in ()).throw(ProcessLookupError("gone")))
    xe = XcuitestEnvironment("xcuitest", "UDID-1", env_run=lambda _a, _e: "")
    xe._runner_proc = GoneProc()  # type: ignore[assignment]
    xe._discard_runner(warn_on_crash=False)  # must not raise
    # With no group id to read, both signals fall back to the process itself, and both are suppressed.
    assert signals == ["terminate", "kill"]
    assert xe._runner_proc is None


@pytest.mark.parametrize(
    "killpg_error",
    [ProcessLookupError("gone"), PermissionError("not ours"), OSError("something else")],
)
def test_a_group_signal_that_does_not_land_falls_back_to_the_process(
    monkeypatch: pytest.MonkeyPatch, killpg_error: OSError
) -> None:
    # Whatever the reason `killpg` did not reach the group, the signal has not been delivered — so
    # every failure falls through to the process itself. Narrowing the fallback to the two *expected*
    # errors would let an unexpected one leave the runner alive on a device the next attempt spawns on.
    signals: list[str] = []

    class Proc:
        pid = 782

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            signals.append("terminate")

        def kill(self) -> None:
            signals.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr("os.getpgid", lambda pid: 4242)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: (_ for _ in ()).throw(killpg_error))
    xe = XcuitestEnvironment("xcuitest", "UDID-1", env_run=lambda _a, _e: "")
    xe._runner_proc = Proc()  # type: ignore[assignment]
    xe._discard_runner(warn_on_crash=False)
    assert signals == ["terminate", "kill"]


def test_spawn_cold_discards_a_never_ready_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # BE-0290 / BE-0319: a runner that spawns but never answers /health (process alive) must be
    # discarded before start() raises — a single-use environment (doctor / serve via read_session)
    # never spawns again to reclaim it, so an unguarded failure would orphan the live xcodebuild
    # subprocess. A "health never ready" attempt spends the whole shared startup budget, so it earns
    # no retry (a second full-ceiling wait would double the worst case and blow a job's
    # timeout-minutes): exactly one attempt, its live runner discarded, then a loud failure.
    import plistlib
    import signal
    import tempfile

    from bajutsu.common.config import XcuitestConfig
    from bajutsu.common.drivers.xcuitest import XcuitestChannelError

    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.environments.xcuitest.xcuitest_environment._allocate_port",
        lambda: 12345,
    )
    # Shrink the cold-startup ceiling so a never-ready wait fails in one poll rather than 120s.
    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.environments.xcuitest._functions._RUNNER_STARTUP_TIMEOUT",
        0.02,
    )
    monkeypatch.delenv("BAJUTSU_XCUITEST_RUNNER_LOG", raising=False)
    signalled = _capture_group_signals(monkeypatch)

    class FakePopen:
        pid = 780

        def __init__(self, cmd: list[str], **kwargs: object) -> None:
            pass

        def poll(self) -> int | None:
            return None  # alive but never binds: the wait times out and the group is signalled

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            pass

    monkeypatch.setattr("subprocess.Popen", FakePopen)

    class _NeverReadyDriver:
        name = "xcuitest"

        def health_ready(self) -> bool:
            return False  # /health never answers ready

        def await_ready(self, **_kw: object) -> None: ...

    monkeypatch.setattr("bajutsu.common.backends.make_driver", lambda *a, **k: _NeverReadyDriver())

    with tempfile.NamedTemporaryFile(suffix=".xctestrun") as f:
        plistlib.dump({"__xctestrun_metadata__": {"FormatVersion": 1}, "T": {}}, f)
        f.flush()
        eff = _ios_eff(xcuitest=XcuitestConfig(testRunner=f.name), app_path=None)
        xe = XcuitestEnvironment("xcuitest", "UDID-1", env_run=lambda _a, _e: "")
        with pytest.raises(XcuitestChannelError, match="did not come up"):
            xe.start(eff, Preconditions())

    # One attempt's live runner was discarded, not orphaned: its group took SIGTERM then the sweep.
    assert signalled == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert xe._runner_proc is None  # the last _discard_runner cleared the handle


def test_xcuitest_environment_forwards_preconditions_to_runner_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import tempfile
    from dataclasses import replace as dc_replace

    from bajutsu.common.config import XcuitestConfig

    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.environments.xcuitest.xcuitest_environment._allocate_port",
        lambda: 11111,
    )

    popen_calls: list[dict[str, Any]] = []

    class FakePopen:
        def __init__(self, cmd: list[str], **kwargs: object) -> None:
            popen_calls.append({"cmd": cmd, "kwargs": kwargs})

        def poll(self) -> int | None:
            return None  # alive: the cold-spawn liveness check (BE-0319) never trips

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr("subprocess.Popen", FakePopen)

    class FakeDriver:
        name = "xcuitest"

        def health_ready(self) -> bool:
            return True

        def await_ready(self, **kw: object) -> None: ...

    monkeypatch.setattr("bajutsu.common.backends.make_driver", lambda *a, **k: FakeDriver())

    import plistlib

    with tempfile.NamedTemporaryFile(suffix=".xctestrun") as f:
        # A minimal valid .xctestrun: one test target the env is injected into.
        plistlib.dump(
            {
                "__xctestrun_metadata__": {"FormatVersion": 1},
                "RunnerUITests": {"TestingEnvironmentVariables": {"EXISTING": "1"}},
            },
            f,
        )
        f.flush()
        eff = dc_replace(
            _ios_eff(xcuitest=XcuitestConfig(testRunner=f.name), app_path=None),
            launch_env={"APP_ENV": "test", "DEBUG": "1"},
            launch_args=["-verbose"],
            locale="ja_JP",
        )
        pre = Preconditions(
            launchEnv={"SCENARIO_KEY": "val"},
            launchArgs=["-reset"],
            locale="fr_FR",
            deeplink="myapp://home",
        )
        xe = XcuitestEnvironment("xcuitest", "U", env_run=lambda a, extra_env: "")
        xe.start(eff, pre, extra_env={"BAJUTSU_COLLECTOR": "http://127.0.0.1:9999"})

    # xcodebuild does not forward its env into the Simulator, so the forwarded vars are read
    # from the patched .xctestrun's per-target TestingEnvironmentVariables.
    cmd: list[str] = popen_calls[0]["cmd"]
    patched = Path(cmd[cmd.index("-xctestrun") + 1])
    with patched.open("rb") as pf:
        target_env = plistlib.load(pf)["RunnerUITests"]["TestingEnvironmentVariables"]
    patched.unlink(missing_ok=True)

    assert target_env["EXISTING"] == "1"  # existing entries preserved
    # launch_env merged (eff + pre + extra_env) under BAJUTSU_LAUNCH_ENV_ prefix
    assert target_env["BAJUTSU_LAUNCH_ENV_APP_ENV"] == "test"
    assert target_env["BAJUTSU_LAUNCH_ENV_DEBUG"] == "1"
    assert target_env["BAJUTSU_LAUNCH_ENV_SCENARIO_KEY"] == "val"
    assert target_env["BAJUTSU_LAUNCH_ENV_BAJUTSU_COLLECTOR"] == "http://127.0.0.1:9999"
    # launch_args as JSON array (eff + pre + locale)
    args = json.loads(target_env["BAJUTSU_LAUNCH_ARGS"])
    assert "-verbose" in args and "-reset" in args
    assert "-AppleLocale" in args and "fr_FR" in args
    # deeplink
    assert target_env["BAJUTSU_DEEPLINK"] == "myapp://home"
    # bundle id of the app under test (so one generic runner drives any app)
    assert target_env["BAJUTSU_BUNDLE_ID"] == "com.example.demo"
