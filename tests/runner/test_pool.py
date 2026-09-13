"""Tests for the device pool and the per-device relauncher."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from _runner import _eff, _el, _web_eff

from bajutsu.common.backend_cli import simctl
from bajutsu.common.config import Effective
from bajutsu.common.drivers import base
from bajutsu.common.drivers.fake import FakeDriver, FakeNetworkCollector
from bajutsu.common.drivers.webview import WebViewBridge
from bajutsu.common.evidence import FileSink
from bajutsu.common.evidence.network import NetworkCollector, NetworkExchange, ScreenTransition
from bajutsu.common.platform_lifecycle import ProvisionProfile
from bajutsu.common.runner import ReadinessResult, device_pool, device_relauncher
from bajutsu.common.scenario import Relaunch, Scenario


def test_relauncher_relaunches_with_locale_and_overrides() -> None:
    calls: list[tuple[list[str], Mapping[str, str] | None]] = []

    def fake_run(args: list[str], env: Mapping[str, str] | None = None) -> str:
        calls.append((args, env))
        return ""

    # Scenario locale (ja_JP) overrides the app/config default (en_US from _eff()).
    scn = Scenario.model_validate(
        {"name": "a", "preconditions": {"locale": "ja_JP"}, "steps": [{"tap": {"id": "ok"}}]}
    )
    driver = FakeDriver([_el("home.title", "H"), _el("ok", "OK")])  # 2 elems -> ready immediately
    # extra_env (the device's collector url) must survive the relaunch.
    relaunch = device_relauncher(
        "UDID-1", env_run=fake_run, extra_env={"BAJUTSU_COLLECTOR": "http://127.0.0.1:9"}
    )(_eff(), scn, driver)
    relaunch(Relaunch(env={"K": "V"}, args=["--fresh"]))

    assert any(
        c[0] == ["xcrun", "simctl", "terminate", "UDID-1", "com.example.demo"] for c in calls
    )
    launch, launch_env = next(c for c in calls if "launch" in c[0])
    assert "--fresh" in launch  # per-relaunch arg
    # Locale forced via app launch args, scenario locale winning.
    assert launch[launch.index("-AppleLocale") + 1] == "ja_JP"
    assert "(ja)" in launch
    # The collector url survives the relaunch and the per-relaunch env override is applied
    # (both reach the app via the SIMCTL_CHILD_ child-env channel).
    assert launch_env is not None
    assert launch_env.get("SIMCTL_CHILD_BAJUTSU_COLLECTOR") == "http://127.0.0.1:9"
    assert launch_env.get("SIMCTL_CHILD_K") == "V"


def _scn(name: str) -> Scenario:
    return Scenario.model_validate({"name": name, "steps": [{"tap": {"id": "ok"}}]})


def test_device_pool_per_device_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pool of >1 devices gives each leased scenario its own collector (distinct url),
    interval-recording sink (bound to the udid), and device control — the three features
    that used to drop in parallel."""
    calls: list[tuple[list[str], Mapping[str, str] | None]] = []

    def fake_run(args: list[str], extra_env: Mapping[str, str] | None = None) -> str:
        calls.append((args, extra_env))
        return ""

    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver",
        lambda actuator, udid: FakeDriver([_el("home", "H"), _el("ok", "OK")]),
    )

    lease, shutdown = device_pool(
        ["UDID-A", "UDID-B"],
        ["fake"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=fake_run,
    )
    la = lb = None
    try:
        la = lease(_eff(), _scn("a"))
        lb = lease(_eff(), _scn("b"))
        # Distinct collectors on distinct ports (no shared single-loopback receiver).
        assert isinstance(la.collector, NetworkCollector)
        assert isinstance(lb.collector, NetworkCollector)
        assert isinstance(la.sink, FileSink) and isinstance(lb.sink, FileSink)
        assert la.collector is not lb.collector
        assert la.collector.port != lb.collector.port
        # Per-device sink bound to the leased udid -> interval evidence works in parallel.
        assert la.sink.udid == "UDID-A" and lb.sink.udid == "UDID-B"
        # Device control present per device, routing to the leased udid.
        assert la.control is not None and lb.control is not None
        la.control.set_location(35.0, 139.0)
        assert any(
            c[0] == ["xcrun", "simctl", "location", "UDID-A", "set", "35.0,139.0"] for c in calls
        )
        # Each device's own collector url is forwarded to its lease as the launch env (how it
        # reaches the app is the backend's concern — the fake backend launches no process, so this
        # asserts the per-device wiring, not a simctl child-env, which the relauncher test covers).
        assert la.collector.port != lb.collector.port
    finally:
        if la is not None:
            la.release()
        if lb is not None:
            lb.release()
        shutdown()
    # shutdown() stops every device's collector.
    assert la is not None and lb is not None
    assert isinstance(la.collector, NetworkCollector)
    assert isinstance(lb.collector, NetworkCollector)
    assert la.collector._server is None and lb.collector._server is None


def test_device_pool_lease_blocks_until_a_held_device_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The free queue is the pool's back-pressure: with more workers than devices, a lease for a
    device that is all checked out blocks on `free.get()` until a holder releases one — and a
    release (the happy path *and*, per the failure-path tests, an aborted lease) returns the udid so
    the waiting lease can proceed. A never-returned udid would block that worker forever."""
    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver",
        lambda actuator, udid: FakeDriver([_el("home", "H"), _el("ok", "OK")]),
    )
    lease, shutdown = device_pool(
        ["UDID-1"],  # a single device, so a second concurrent lease must wait
        ["fake"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    first = lease(_eff(), _scn("a"))  # holds the only device
    released = False
    acquired: list[str] = []
    entered = threading.Event()

    def worker() -> None:
        entered.set()
        lz = lease(_eff(), _scn("b"))  # blocks: the free queue is empty
        acquired.append(lz.udid)
        lz.release()

    # Daemon so a worker still blocked on `free.get()` (an assertion failed before the release below)
    # never wedges interpreter teardown; the finally then releases the held device to unblock it.
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        assert entered.wait(1.0)  # the worker reached the blocking lease
        t.join(0.2)
        assert t.is_alive() and acquired == []  # still blocked while the device is held
        first.release()  # returns UDID-1 to the free queue
        released = True
        t.join(2.0)
        assert not t.is_alive() and acquired == ["UDID-1"]  # the freed device unblocked the lease
    finally:
        if not released:
            first.release()  # unblock a still-waiting worker so it can't leak past this test
        t.join(2.0)
        shutdown()


def test_device_pool_wires_readiness_and_provenance_into_the_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lease folds the launch readiness outcome and this scenario's BE-0049 provenance into the
    sink, so a first-wait timeout diagnostic can state them (BE-0231 Unit 1)."""
    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver",
        lambda actuator, udid: FakeDriver([_el("home", "H"), _el("ok", "OK")]),  # 2 → count signal
    )
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["fake"],
        _eff(),
        Path("runs"),
        available=lambda b: True,
        env_run=lambda args, extra_env: "",
    )
    lz = None
    try:
        lz = lease(_eff(), _scn("a"))
        assert isinstance(lz.sink, FileSink)
        assert lz.sink.readiness is not None
        assert lz.sink.readiness.signal == "count"
        assert lz.sink.provenance is not None
        scenario_hash = lz.sink.provenance["scenarioHash"]
        assert isinstance(scenario_hash, str) and scenario_hash.startswith("sha256:")
        assert "toolVersion" in lz.sink.provenance
    finally:
        if lz is not None:
            lz.release()
        shutdown()


def test_device_pool_labels_leased_simulator(monkeypatch: pytest.MonkeyPatch) -> None:
    # The pool reads the simulator catalog once and tags each lease with its device model /
    # OS runtime, so the report's Environment tab can name the simulator a scenario ran on.
    catalog = json.dumps(
        {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-17-2": [
                    {"udid": "UDID-A", "name": "iPhone 15"}
                ],
            }
        }
    )

    def fake_run(args: list[str], extra_env: Mapping[str, str] | None = None) -> str:
        return catalog if args == simctl.list_devices_cmd() else ""

    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver",
        lambda actuator, udid: FakeDriver([_el("home", "H"), _el("ok", "OK")]),
    )
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["fake"],
        _eff(),
        Path("runs"),
        available=lambda b: True,
        env_run=fake_run,
    )
    try:
        lz = lease(_eff(), _scn("a"))
        assert lz.device_name == "iPhone 15" and lz.device_runtime == "iOS 17.2"
    finally:
        shutdown()


def test_device_pool_single_device_keeps_full_features(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pool of one is the single-device path: collector + interval sink + control, all on."""
    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver",
        lambda actuator, udid: FakeDriver([_el("home", "H"), _el("ok", "OK")]),
    )
    lease, shutdown = device_pool(
        ["UDID-1"],
        ["fake"],
        _eff(),
        Path("runs"),
        network=True,
        log_subsystem="com.example.demo",
        available=lambda b: True,
        env_run=lambda args, extra_env: "",
    )
    try:
        lz = lease(_eff(), _scn("a"))
        assert lz.collector is not None  # network collection in a pool of one
        assert isinstance(lz.sink, FileSink)
        assert lz.sink.udid == "UDID-1"  # interval evidence bound to the device
        assert lz.control is not None  # device control available
        assert lz.relaunch is not None  # relaunch wired to the device
        lz.release()
    finally:
        shutdown()


def test_device_pool_no_network_has_no_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-network: the pool builds no collectors and injects no collector url."""
    calls: list[tuple[list[str], Mapping[str, str] | None]] = []

    def fake_run(args: list[str], extra_env: Mapping[str, str] | None = None) -> str:
        calls.append((args, extra_env))
        return ""

    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver",
        lambda actuator, udid: FakeDriver([_el("home", "H"), _el("ok", "OK")]),
    )
    lease, shutdown = device_pool(
        ["UDID-1"],
        ["fake"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=fake_run,
    )
    try:
        lz = lease(_eff(), _scn("a"))
        assert lz.collector is None
        launch_envs = [e for args, e in calls if "launch" in args]
        assert all("SIMCTL_CHILD_BAJUTSU_COLLECTOR" not in (e or {}) for e in launch_envs)
        lz.release()
    finally:
        shutdown()


def test_device_pool_stops_started_collectors_when_one_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a collector fails to start mid-setup, the ones already started must be stopped so
    the pool doesn't leak listening sockets."""
    started: list[object] = []

    class FlakyCollector:
        count = 0

        def __init__(self) -> None:
            FlakyCollector.count += 1
            self._idx = FlakyCollector.count
            self.stopped = False

        def start(self) -> None:
            if self._idx == 2:  # the second device's collector fails to bind
                raise OSError("port in use")
            started.append(self)

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("bajutsu.common.runner.pool.NetworkCollector", FlakyCollector)
    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver", lambda actuator, udid: FakeDriver([])
    )

    with pytest.raises(OSError, match="port in use"):
        device_pool(
            ["UDID-A", "UDID-B"],
            ["fake"],
            _eff(),
            Path("runs"),
            network=True,
            available=lambda b: True,
            env_run=lambda args, extra_env: "",
        )
    assert len(started) == 1 and started[0].stopped  # type: ignore[attr-defined]


def test_device_pool_completes_the_start_rollback_despite_a_stop_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-0342: the start-rollback loop is a collector-stop site like the others this module guards —
    an `OSError` stopping the *first* device's collector must not stop the rollback of the rest, or
    replace the *original* bind failure the operator needs to see with a socket-close error instead."""
    stopped: list[int] = []

    class FlakyCollector:
        count = 0

        def __init__(self) -> None:
            FlakyCollector.count += 1
            self._idx = FlakyCollector.count

        def start(self) -> None:
            if self._idx == 3:  # the third device's collector fails to bind
                raise OSError("port in use")

        def stop(self) -> None:
            stopped.append(self._idx)
            if self._idx == 1:  # the first device's rollback stop fails too
                raise OSError("socket already gone")

    monkeypatch.setattr("bajutsu.common.runner.pool.NetworkCollector", FlakyCollector)
    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver", lambda actuator, udid: FakeDriver([])
    )

    with (
        caplog.at_level(logging.WARNING, logger="bajutsu.common.runner.recovery"),
        pytest.raises(OSError, match="port in use"),  # the original bind failure, not masked
    ):
        device_pool(
            ["UDID-A", "UDID-B", "UDID-C"],
            ["fake"],
            _eff(),
            Path("runs"),
            network=True,
            available=lambda b: True,
            env_run=lambda args, extra_env: "",
        )
    assert stopped == [1, 2]  # the second device's rollback still ran despite the first's failure
    assert "UDID-A" in caplog.text  # the swallowed stop failure was logged, not silent


@pytest.mark.parametrize("mirrors", [False, True])
def test_device_pool_reserves_a_bridgeable_port_only_where_the_device_mirrors_it(
    monkeypatch: pytest.MonkeyPatch, mirrors: bool
) -> None:
    """Which start the pool calls follows the platform, not the fact that a collector exists.

    Android mirrors the collector's port onto the guest with `adb reverse`, so its port must come
    from the reserved band; a platform sharing the host's loopback binds nothing device-side and
    keeps the OS-chosen port it always had.
    """
    calls: list[str] = []

    class RecordingCollector:
        def start(self) -> int:
            calls.append("start")
            return 41000

        def start_bridgeable(self) -> int:
            calls.append("start_bridgeable")
            return 6800

        def stop(self) -> None:
            pass

    monkeypatch.setattr("bajutsu.common.runner.pool.NetworkCollector", RecordingCollector)
    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver", lambda actuator, udid: FakeDriver([])
    )
    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.environments.fake.FakeEnvironment."
        "mirrors_collector_port_on_device",
        lambda self: mirrors,
    )

    _lease, shutdown = device_pool(
        ["UDID-A"],
        ["fake"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=lambda args, extra_env: "",
    )
    try:
        assert calls == (["start_bridgeable"] if mirrors else ["start"])
    finally:
        shutdown()


class _StubCollector(FakeNetworkCollector):
    """A minimal `Collector` for the web lease test (the real one needs a Playwright page)."""

    def __init__(self, *, fail_stop: bool = False) -> None:
        super().__init__([])
        self.stopped = False
        self.fail_stop = fail_stop

    def snapshot(self) -> list[NetworkExchange]:
        return []

    def snapshot_timed(self) -> list[tuple[NetworkExchange, float]]:
        return []

    def transitions_snapshot_timed(self) -> list[tuple[ScreenTransition, float]]:
        return []

    def clear(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True
        if self.fail_stop:
            raise OSError("socket already closed")


class _FakeWeb(FakeDriver):
    """A fake web driver: a FakeDriver plus the web-only navigate()/close() lifecycle."""

    def __init__(self, screen: list[base.Element], *, fail_collector_stop: bool = False) -> None:
        super().__init__(screen)
        self.navigated = 0
        self.closed = 0
        self.collector_mocks: object = "unset"
        self.collector: _StubCollector | None = None
        self.fail_collector_stop = fail_collector_stop

    def navigate(self) -> None:
        self.navigated += 1

    def close(self) -> None:
        self.closed += 1

    def network_collector(self, mocks: object = None) -> _StubCollector:
        self.collector_mocks = mocks
        self.collector = _StubCollector(fail_stop=self.fail_collector_stop)
        return self.collector


class _RecordingEnv:
    """A fake RunEnvironment that records the actuator it was built for and whether it was started
    and torn down — enough of the lease seam to prove per-scenario selection and per-lease teardown.
    """

    def __init__(
        self,
        actuator: str,
        udid: str,
        provision: object = None,
        *,
        fail_start: bool = False,
        fail_relauncher: bool = False,
        reusable: bool = False,
        raise_on_teardown: bool = False,
        teardown_error: BaseException | None = None,
        raise_on_end_lease: bool = False,
        end_lease_error: BaseException | None = None,
        replacement: str | None = None,
        catalog: dict[str, dict[str, str]] | None = None,
        fail_bridge_teardown: bool = False,
        fail_device_catalog: bool = False,
    ) -> None:
        self.actuator = actuator
        self.udid = udid
        self.provision = provision  # the ProvisionProfile device_pool threaded through (BE-0236)
        self.started = False
        self.torn = False
        # A device this env replaced during `start` (None: the leased device is the one that
        # ran, which is every platform but the XCUITest Simulator's vanished-device path).
        self.replacement = replacement
        # What `device_catalog()` reports for this env — a replacement re-fetches the catalog from
        # the *lease* env (pool.py's `adopt_replacement`), not the pool-init one, so this is separate
        # from any catalog the pool itself was built with.
        self.catalog = catalog or {}
        self.fail_start = fail_start
        # A failure *after* `start` returns a driver but before `lease()` finishes building the
        # `Lease` — mimics `hook_collector`/`relauncher`/`controller` raising on a real backend
        # (BE-0342).
        self.fail_relauncher = fail_relauncher
        # BE-0291: a fake warm resident. `reusable` makes the pool cache and reuse this instance
        # across leases; the counters record how the pool released it (kept warm vs full teardown).
        # `raise_on_teardown` mimics an expected simctl teardown failure (the app already gone);
        # `teardown_error`, when set, raises that exception instead — a wiring defect rather than an
        # expected process failure (BE-0342).
        self.reusable = reusable
        self.raise_on_teardown = raise_on_teardown
        self.teardown_error = teardown_error
        # The same expected-failure/wiring-defect shape as `teardown`, but kept independent of it:
        # `end_lease` raising means the app's teardown never completed, so the resident it belongs to
        # must be evicted rather than resumed — a distinct failure from `teardown`'s, which several
        # tests need to fail independently (e.g. a lease releases warm cleanly, and only the later
        # full teardown at `shutdown()` fails) (BE-0342).
        self.raise_on_end_lease = raise_on_end_lease
        self.end_lease_error = end_lease_error
        # How many times the crash retry asked this env to swap its device (BE-0354).
        self.replacement_requests = 0
        self.snapshots_taken = 0
        # `bridge_collector`'s returned teardown raising mimics `adb reverse --remove` on a device
        # that already dropped off the bus (BE-0342).
        self.fail_bridge_teardown = fail_bridge_teardown
        # `device_catalog()` raising mimics `adopt_replacement`'s own subprocess call failing after
        # `start` already produced a driver (BE-0342).
        self.fail_device_catalog = fail_device_catalog
        self.start_count = 0
        self.end_lease_count = 0
        # BE-0283 bridge recording: the port bridged, whether it was already bridged when start ran
        # (i.e. before launch), and whether its teardown thunk fired.
        self.bridged_port: int | None = None
        self.bridged_before_launch = False
        self.bridge_torn = False

    def start(self, eff: Effective, pre: object, **_: object) -> base.Driver:
        self.bridged_before_launch = self.bridged_port is not None
        self.start_count += 1
        if self.fail_start:
            raise RuntimeError("launch failed")
        self.started = True
        return FakeDriver([_el("home", "H"), _el("ok", "OK")])  # 2 elems -> ready on count

    def device_catalog(self) -> dict[str, dict[str, str]]:
        if self.fail_device_catalog:
            raise subprocess.CalledProcessError(1, ["xcrun", "simctl", "list", "devices"])
        return self.catalog

    def observes_network_via_driver(self) -> bool:
        return False

    def mirrors_collector_port_on_device(self) -> bool:
        return True  # stands in for Android, the backend whose bridge mirrors the port

    def bridge_collector(self, port: int) -> Callable[[], None]:
        self.bridged_port = port

        def remove() -> None:
            self.bridge_torn = True
            if self.fail_bridge_teardown:
                raise subprocess.CalledProcessError(1, ["adb", "reverse", "--remove"])

        return remove

    def records_video_up_front(self) -> bool:
        return False

    def prestarted_intervals(self) -> list[object]:
        return []  # this fake records on demand: nothing begun before launch

    def relauncher(
        self, eff: Effective, scenario: Scenario, driver: base.Driver, **_: object
    ) -> Callable[[object], None]:
        if self.fail_relauncher:
            raise RuntimeError("post-launch failure")
        return lambda opts: None

    def controller(self, eff: Effective) -> None:
        return None

    def has_reusable_resident(self) -> bool:
        return self.reusable

    def request_device_replacement(self) -> None:
        # Counted, not acted on: the pool only has to hand the crash retry a way to reach it.
        self.replacement_requests += 1

    def replaced_device(self) -> str | None:
        # The udid this env moved to when `start` had to replace a vanished device. Settable
        # per instance so a test can drive the pool's re-keying without a Simulator.
        return self.replacement

    def take_crash_snapshot(self) -> Callable[[], list[tuple[str, bytes]]]:
        # Named after the udid, so a test can tell which environment's capture the lease reached, and
        # counted so a test can pin that the pool takes it exactly once, at release.
        self.snapshots_taken += 1
        return lambda: [(f"runner-{self.udid}.log", b"crashed")]

    def end_lease(self, driver: base.Driver, eff: Effective) -> None:
        if self.end_lease_error is not None:
            raise self.end_lease_error
        if self.raise_on_end_lease:
            raise subprocess.CalledProcessError(1, ["xcrun", "simctl", "terminate"])
        self.end_lease_count += 1  # kept warm: the pool released the lease without a full teardown

    def teardown(self, driver: base.Driver, eff: Effective) -> None:
        self.torn = True
        if self.teardown_error is not None:
            raise self.teardown_error
        if self.raise_on_teardown:
            raise subprocess.CalledProcessError(1, ["xcrun", "simctl", "terminate"])


# A per-scenario actuator resolver standing in for BE-0240: it returns one of two *real* actuators
# keyed off the scenario, so the pool's env lifecycle is exercised across an actuator change even
# though every real platform is single-actuator (BE-0290). A plain tap →
# `adb`, a pinch → `xcuitest`; both are known to `capabilities_for`, and the pool treats them as an
# opaque pair — it is the switch, not the platforms, that this drives.
def _fake_resolve(backends: list[str], scenario: object, available: object = None) -> str:
    steps = getattr(scenario, "steps", [])
    return "xcuitest" if any(getattr(s, "pinch", None) is not None for s in steps) else "adb"


def test_device_pool_resolves_actuator_per_scenario_and_tears_down_its_own_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE-0240: each scenario leases the actuator its own steps resolve to, and the environment that
    *starts* a lease is the one that tears it down — so a stateful backend's resident runner is
    terminated by the instance that spawned it."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(actuator, udid, provision)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    monkeypatch.setattr("bajutsu.common.runner.pool.select_actuator_for_scenario", _fake_resolve)

    pinch = Scenario.model_validate(
        {"name": "p", "steps": [{"pinch": {"sel": {"id": "m"}, "scale": 2.0}}]}
    )
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        tap_lease = lease(_eff(), _scn("tap"))
        adb_env = created[-1]  # the lease env, not the pool env
        assert adb_env.actuator == "adb" and adb_env.started
        tap_lease.release()
        assert adb_env.torn  # the SAME instance that started tears down (BE-0240)

        pinch_lease = lease(_eff(), pinch)
        xc_env = created[-1]
        assert xc_env.actuator == "xcuitest" and xc_env.started  # resolved to the other actuator
        pinch_lease.release()
        assert xc_env.torn
    finally:
        shutdown()


def test_device_pool_gives_each_lease_a_distinct_webview_bridge_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that bridges the WebView inspector (iOS, which does not observe network via the
    driver) gets a fresh host port per lease, bound from the OS ephemeral range. Two concurrent
    leases on a pool must therefore never share a port — a collision would cross two devices' inspector
    traffic onto one tunnel. Each lease exposes its bridge, so assert the two ports differ."""

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        # observes_network_via_driver() is False here, so the pool allocates a WebView bridge (the iOS
        # shape), unlike web where the driver owns the page and no bridge is reserved.
        return _RecordingEnv(actuator, udid, provision)

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A", "UDID-B"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    la = lb = None
    try:
        la = lease(_eff(), _scn("a"))
        lb = lease(_eff(), _scn("b"))
        assert isinstance(la.webview_bridge, WebViewBridge)
        assert isinstance(lb.webview_bridge, WebViewBridge)
        assert la.webview_bridge.port != lb.webview_bridge.port  # no cross-device port collision
    finally:
        if la is not None:
            la.release()
        if lb is not None:
            lb.release()
        shutdown()


def test_device_pool_marks_a_cold_respawn_after_the_first_bring_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first cold spawn of a device is a first bring-up (respawn=False); a later cache-miss lease on
    # the same device — the warm resident died and was evicted, as a mid-run crash does — is a respawn
    # (respawn=True), so the environment picks the tighter readiness ceiling. A non-reusable fake env
    # is cache-missed every lease, reproducing that "cold-spawn a device already brought up" state.
    respawns: list[bool] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        respawns.append(respawn)
        return _RecordingEnv(actuator, udid, provision)  # reusable=False: never cached, always cold

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        lease(_eff(), _scn("one")).release()
        lease(_eff(), _scn("two")).release()
    finally:
        shutdown()
    # The pool builds one representative environment up front (device catalog, never a lease —
    # respawn=False), then the two leases: the first is a first bring-up, the second a respawn.
    assert respawns == [False, False, True]


def test_device_pool_reuses_a_warm_resident_across_scenarios(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE-0291: an environment that holds a warm resident (the Simulator XCUITest runner) is built
    once per device and reused across every same-actuator lease — the runner's cold startup is paid
    once per device, not once per scenario. Each release keeps it warm (`end_lease`, not a full
    teardown); the pool tears it down once at the run-set's end."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(actuator, udid, provision, reusable=True)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        for _ in range(3):
            lz = lease(_eff(), _scn("s"))
            lz.release()
        # created[0] is the pool's representative env (built up front, never leased); after it, ONE
        # lease environment served all three scenarios — the runner was not respawned per scenario.
        assert len(created) == 2
        env = created[1]
        assert env.start_count == 3  # resumed each lease (same instance), not a fresh spawn
        assert env.end_lease_count == 3  # every release kept the resident warm
        assert not env.torn  # never fully torn down mid-run
    finally:
        shutdown()
    assert env.torn  # torn down once at the run-set's end — ownership is the pool's (Unit 3)


def test_device_pool_evicts_a_warm_resident_whose_end_lease_did_not_finish(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-0342: `end_lease` terminates only the app, so a failure there (the same shape as an
    already-gone `xcrun simctl terminate`) leaves it unclear whether the app under test is really
    down. `release()` still swallows the failure into a warning rather than failing the run, but the
    resident is dropped from `warm` so the next lease respawns cold instead of resuming an
    environment whose app teardown never completed."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(actuator, udid, provision, reusable=True)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        first = lease(_eff(), _scn("a"))
        env = created[1]  # created[0] is the pool's representative env
        env.raise_on_end_lease = True
        with caplog.at_level(logging.WARNING, logger="bajutsu.common.runner.recovery"):
            first.release()  # must not raise
        assert "at the lease's end" in caplog.text  # logged, not silent
        # The runner process is actually discarded too — a dropped `warm` entry alone would leak
        # it, since it becomes invisible to `shutdown()`'s own sweep.
        assert env.torn
        # The failed-to-end resident was evicted, so the next lease builds a fresh environment
        # rather than resuming the one whose app teardown never completed.
        retry = lease(_eff(), _scn("b"))
        assert len(created) == 3 and created[2] is not env and created[2].started
        retry.release()
    finally:
        shutdown()


@pytest.mark.parametrize(
    "teardown_error",
    [
        pytest.param(None, id="clean"),
        pytest.param(AttributeError("no close on this driver"), id="wiring-defect"),  # BE-0342
    ],
)
def test_device_pool_actuator_switch_tears_down_the_warm_resident(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    teardown_error: BaseException | None,
) -> None:
    """BE-0291 Unit 3: when the next scenario on a device resolves to a different actuator, the warm
    resident is torn down before the new actuator's environment starts — the one-actuator-per-device
    rule (BE-0240) still holds, so a warm runner is never inherited across an actuator switch. A
    wiring defect on that teardown (BE-0342) must not escape either: this site runs before `lease()`'s
    own `try`, so anything it re-raises would leak `udid` out of `free` and hang every later lease on
    this device instead of just warning and proceeding with the switch."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(actuator, udid, provision, reusable=True)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    monkeypatch.setattr("bajutsu.common.runner.pool.select_actuator_for_scenario", _fake_resolve)
    pinch = Scenario.model_validate(
        {"name": "p", "steps": [{"pinch": {"sel": {"id": "m"}, "scale": 2.0}}]}
    )
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        tap = lease(_eff(), _scn("tap"))  # resolves to the cheap actuator
        tap.release()
        adb_env = created[
            1
        ]  # created[0] is the pool's representative env; [1] is the tap lease env
        assert adb_env.actuator == "adb" and not adb_env.torn  # kept warm after release
        adb_env.teardown_error = teardown_error
        with caplog.at_level(logging.WARNING, logger="bajutsu.common.runner.recovery"):
            pinch_lease = lease(_eff(), pinch)  # resolves to the other actuator
        assert adb_env.torn  # the warm resident was torn down on the actuator switch
        if teardown_error is not None:
            assert "for an actuator switch" in caplog.text  # swallowed into a warning, not silent
        xc_env = created[-1]
        assert (
            xc_env.actuator == "xcuitest" and len(created) == 3
        )  # a fresh env for the new actuator
        pinch_lease.release()
        # The device is still usable — a leaked `udid` would hang this on `free.get()`.
        tap_again = lease(_eff(), _scn("tap-again"))
        tap_again.release()
    finally:
        shutdown()


@pytest.mark.parametrize(
    "teardown_error",
    [
        pytest.param(
            None, id="expected-process-failure"
        ),  # CalledProcessError, via raise_on_teardown
        pytest.param(AttributeError("close"), id="wiring-defect"),  # BE-0342
    ],
)
def test_device_pool_evicts_and_tears_down_a_warm_resident_whose_resume_fails(
    monkeypatch: pytest.MonkeyPatch, teardown_error: BaseException | None
) -> None:
    """BE-0291: if a warm resident's resume fails, it must not be reused — the pool drops it from the
    cache and tears it down so the next lease respawns cold rather than reusing a half-broken runner,
    and the device is returned so a retry can lease it. Neither an expected process failure on
    teardown (`CalledProcessError`) nor a wiring defect (BE-0342) may replace the resume failure the
    caller needs to see, or skip returning the device to `free`."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(actuator, udid, provision, reusable=True)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        first = lease(_eff(), _scn("a"))
        first.release()
        warm_env = created[1]  # cached after the first lease
        assert not warm_env.torn
        warm_env.fail_start = True  # its next resume fails
        warm_env.raise_on_teardown = teardown_error is None  # and its eviction teardown also errors
        warm_env.teardown_error = teardown_error
        with pytest.raises(RuntimeError, match="launch failed"):
            lease(
                _eff(), _scn("b")
            )  # the *original* resume failure propagates, not the teardown one
        assert warm_env.torn  # the stale warm env was evicted from the cache and torn down
        # The device was returned, and the warm entry dropped, so a retry leases a fresh environment.
        retry = lease(_eff(), _scn("c"))
        assert len(created) == 3 and created[2] is not warm_env and created[2].started
        retry.release()
    finally:
        shutdown()


def test_device_pool_tears_down_a_non_reusable_env_when_the_lease_fails_after_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE-0342: on a backend that keeps no warm resident (web / Android / adb / fake), `warm[udid]`
    is never populated — so a failure raised after `launch_driver` returns but before `Lease` is
    built (`hook_collector`/`relauncher`/`controller`/the sink) must still tear the environment it
    just launched down, and still return the device, rather than leaking both onto the next lease."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        # `created[0]` is `device_pool`'s own representative `pool_env`, built before any lease; only
        # the first per-lease env (`created[1]`) fails — a fresh non-reusable env every call otherwise
        # never converges (nothing caches it for a retry to build on, unlike the warm-resident case).
        env = _RecordingEnv(actuator, udid, provision, fail_relauncher=len(created) == 1)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["android"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        with pytest.raises(RuntimeError, match="post-launch failure"):
            lease(_eff(), _scn("a"))
        failed_env = created[1]  # created[0] is the pool's own representative env
        assert failed_env.started and failed_env.torn  # torn down despite never reaching `warm`
        # The device was still returned, so a retry leases cleanly instead of hanging on `free.get()`.
        retry = lease(_eff(), _scn("b"))
        assert len(created) == 3 and created[2] is not failed_env and created[2].started
        retry.release()
    finally:
        shutdown()


def test_device_pool_tears_down_the_launched_env_when_adopt_replacement_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE-0342: `adopt_replacement()` runs between `launch_driver` returning and `Lease` being built,
    and its own `device_catalog()` call can shell out and fail — the same class of failure the
    `launched` fallback exists to survive. `launched` must be recorded *before* that call runs, or a
    failure inside it finds neither `stale` nor `launched` set and leaks the environment/driver
    `launch_driver` already produced."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        # `created[0]` is the pool's own representative env; only the first per-lease env replaces
        # its device and fails fetching the replacement's catalog.
        env = _RecordingEnv(
            actuator,
            udid,
            provision,
            replacement="UDID-A-REPLACEMENT" if len(created) == 1 else None,
            fail_device_catalog=len(created) == 1,
        )
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["android"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        with pytest.raises(subprocess.CalledProcessError):
            lease(_eff(), _scn("a"))
        failed_env = created[1]  # created[0] is the pool's own representative env
        assert failed_env.started and failed_env.torn  # torn down despite the mid-adoption failure
        # The device was still returned, so a retry leases cleanly instead of hanging on `free.get()`.
        retry = lease(_eff(), _scn("b"))
        assert len(created) == 3 and created[2] is not failed_env and created[2].started
        retry.release()
    finally:
        shutdown()


def test_device_pool_tears_down_the_bridge_when_the_lease_fails_after_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE-0342: the `except` block's `release_bridge()` guard is a teardown site this item adds that
    no other test drives to failure — a bare `release_bridge()` call left in its place would still
    leave the whole suite green. A device that dropped off the bus mid-lease makes `bridge_collector`'s
    teardown thunk raise the same `CalledProcessError` a real `adb reverse --remove` would; that must
    not replace the *original* post-launch failure or skip `free.put(udid)` behind it. (The
    neighboring `release_collector.stop()` guard stays untested here: this backend's pre-started
    collector never sets `release_collector`, so it is not reachable from this path.)"""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        # `created[0]` is the pool's own representative env; only the first per-lease env fails its
        # relauncher (so the except block runs) and its bridge teardown (so that guard fires too).
        env = _RecordingEnv(
            actuator,
            udid,
            provision,
            fail_relauncher=len(created) == 1,
            fail_bridge_teardown=len(created) == 1,
        )
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["android"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        with pytest.raises(RuntimeError, match="post-launch failure"):
            lease(_eff(), _scn("a"))
        failed_env = created[1]  # created[0] is the pool's own representative env
        assert (
            failed_env.bridge_torn and failed_env.torn
        )  # both guards ran despite the bridge failure
        # The device was still returned, so a retry leases cleanly instead of hanging on `free.get()`.
        retry = lease(_eff(), _scn("b"))
        assert len(created) == 3 and created[2] is not failed_env and created[2].started
        retry.release()
    finally:
        shutdown()


def test_device_pool_shutdown_tears_down_every_warm_device_despite_a_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-0291: at the run-set's end the pool tears down every device's warm resident; an expected
    teardown failure on one device is logged and skipped, so the others still come down."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(
            actuator, udid, provision, reusable=True, raise_on_teardown=(udid == "UDID-A")
        )
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A", "UDID-B"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    la = lease(_eff(), _scn("a"))
    la.release()
    lb = lease(_eff(), _scn("b"))
    lb.release()
    warm_a = next(e for e in created if e.udid == "UDID-A" and e.start_count)
    warm_b = next(e for e in created if e.udid == "UDID-B" and e.start_count)
    with caplog.at_level(logging.WARNING, logger="bajutsu.common.runner.recovery"):
        shutdown()  # UDID-A's teardown raises; it must not abort UDID-B's or the collector cleanup
    assert warm_a.torn and warm_b.torn  # both warm residents were torn down
    assert "UDID-A" in caplog.text  # the swallowed teardown failure was logged, not silent


def test_device_pool_shutdown_completes_the_sweep_before_a_wiring_defect_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE-0342: a wiring defect on one device's teardown must still fail `shutdown()` loudly — but
    only after every other device's warm resident has been torn down, not instead of it. `mid_run`'s
    usual immediate propagation would otherwise leak `UDID-B`'s runner on `UDID-A`'s defect."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(
            actuator,
            udid,
            provision,
            reusable=True,
            teardown_error=AttributeError("no close on this driver") if udid == "UDID-A" else None,
        )
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A", "UDID-B"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    la = lease(_eff(), _scn("a"))
    la.release()
    lb = lease(_eff(), _scn("b"))
    lb.release()
    warm_a = next(e for e in created if e.udid == "UDID-A" and e.start_count)
    warm_b = next(e for e in created if e.udid == "UDID-B" and e.start_count)
    with pytest.raises(AttributeError, match="no close on this driver"):
        shutdown()
    assert warm_a.torn and warm_b.torn  # the sweep completed for both before the defect propagated


def test_device_pool_shutdown_logs_every_defect_past_the_first(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-0342: when more than one device's teardown raises a wiring defect, `shutdown()` still
    raises only the first — but a later one is not silently dropped; it gets its own log line rather
    than vanishing with no trace."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(
            actuator, udid, provision, reusable=True, teardown_error=RuntimeError(f"broken {udid}")
        )
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A", "UDID-B"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    la = lease(_eff(), _scn("a"))
    la.release()
    lb = lease(_eff(), _scn("b"))
    lb.release()
    warm_a = next(e for e in created if e.udid == "UDID-A" and e.start_count)
    warm_b = next(e for e in created if e.udid == "UDID-B" and e.start_count)
    with (
        caplog.at_level(logging.ERROR, logger="bajutsu.common.runner.pool"),
        pytest.raises(RuntimeError, match="broken UDID-A"),  # only the first defect propagates
    ):
        shutdown()
    assert warm_a.torn and warm_b.torn  # both were still torn down
    assert "UDID-B" in caplog.text  # the second defect was logged, not lost


def test_device_pool_shutdown_stops_the_collector_before_a_warm_residents_wiring_defect_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE-0342: `shutdown()`'s two loops — warm residents, then collectors — defer a wiring defect
    past the *whole* sweep, not just the rest of its own loop. Every device-loop test above builds
    with `network=False` (`collectors` empty); every collector-loop test below never leases
    (`warm` empty) — so neither pins the case that actually spans both loops: a warm resident's
    defect must still let the collector loop run to completion before `raise defect` propagates it."""
    stopped: list[str] = []
    original_stop = NetworkCollector.stop

    def recording_stop(self: NetworkCollector) -> None:
        stopped.append("UDID-A")
        original_stop(self)

    monkeypatch.setattr(NetworkCollector, "stop", recording_stop)
    monkeypatch.setattr(
        "bajutsu.common.runner.pool.environment_for",
        lambda actuator, udid, env_run=None, *, provision=None, respawn=False: _RecordingEnv(
            actuator,
            udid,
            provision,
            reusable=True,
            teardown_error=AttributeError("no close on this driver"),
        ),
    )
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    leased = lease(_eff(), _scn("a"))
    leased.release()  # kept warm (reusable=True), not torn down until shutdown()
    with pytest.raises(AttributeError, match="no close on this driver"):
        shutdown()
    assert stopped == ["UDID-A"]  # the collector loop still ran before the defect propagated


def _stop_failing_collector(fail: BaseException) -> type:
    class _FailingStopCollector:
        def start_bridgeable(self) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            raise fail

    return _FailingStopCollector


def test_device_pool_shutdown_swallows_an_expected_collector_stop_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-0342: the collector-stop loop is routed through `guarded_teardown`, the same as the
    device-teardown loop above it — a socket already gone (`OSError`) is an expected process
    failure, warned and skipped, not a defect that fails `shutdown()`."""
    monkeypatch.setattr(
        "bajutsu.common.runner.pool.NetworkCollector",
        _stop_failing_collector(OSError("socket gone")),
    )
    monkeypatch.setattr(
        "bajutsu.common.runner.pool.environment_for",
        lambda actuator, udid, env_run=None, *, provision=None, respawn=False: _RecordingEnv(
            actuator, udid, provision
        ),
    )
    _, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    with caplog.at_level(logging.WARNING, logger="bajutsu.common.runner.recovery"):
        shutdown()  # must not raise
    assert "UDID-A" in caplog.text  # the expected failure was logged, not silent


def test_device_pool_shutdown_completes_the_collector_sweep_before_a_wiring_defect_propagates(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-0342: unlike an expected process failure, a wiring defect on one device's collector must
    not skip the others' `stop()`, and must still fail `shutdown()` loudly — but only the first such
    defect propagates; a later one is logged by udid rather than silently dropped."""
    monkeypatch.setattr(
        "bajutsu.common.runner.pool.NetworkCollector",
        _stop_failing_collector(AttributeError("no stop on this collector")),
    )
    monkeypatch.setattr(
        "bajutsu.common.runner.pool.environment_for",
        lambda actuator, udid, env_run=None, *, provision=None, respawn=False: _RecordingEnv(
            actuator, udid, provision
        ),
    )
    _, shutdown = device_pool(
        ["UDID-A", "UDID-B"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    with (
        caplog.at_level(logging.ERROR, logger="bajutsu.common.runner.pool"),
        pytest.raises(AttributeError, match="no stop on this collector"),  # only the first
    ):
        shutdown()
    assert "UDID-B" in caplog.text  # the second collector's defect was logged, not lost


@pytest.mark.parametrize(
    "reusable",
    [
        pytest.param(False, id="teardown"),  # the `else lease_env.teardown(...)` arm
        pytest.param(True, id="end_lease"),  # the `end_lease` arm, XCUITest's warm-resident path
    ],
)
def test_device_pool_release_swallows_an_expected_process_failure_and_still_frees_the_device(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reusable: bool,
) -> None:
    """BE-0342: `release()` runs from the run pipeline's `finally`, so an expected process failure on
    its teardown (a runner already gone, an unreachable `xcrun`) may not replace the scenario's own
    result or skip returning the device to `free` — like the actuator-switch and failed-lease sites.
    Pinned over both the plain `teardown` arm and the `end_lease` arm a warm resident takes — the
    latter also falls back to a full teardown, since `end_lease` not finishing means the app was
    never confirmed down (see
    `test_device_pool_evicts_a_warm_resident_whose_end_lease_did_not_finish` above). Unlike a *wiring*
    defect (below), this class is only ever warned, never deferred to `shutdown()`."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(actuator, udid, provision, reusable=reusable)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        first = lease(_eff(), _scn("a"))
        env = created[1]  # created[0] is the pool's representative env
        if reusable:
            # The `end_lease` arm fails; its fallback teardown (unset error/flag) then completes
            # cleanly, so `env.torn` still ends up True via that fallback.
            env.raise_on_end_lease = True
        else:
            env.raise_on_teardown = True
        with caplog.at_level(logging.WARNING, logger="bajutsu.common.runner.recovery"):
            first.release()  # must not raise
        assert env.torn  # the plain arm's own teardown, or the end_lease arm's fallback teardown
        assert "at the lease's end" in caplog.text  # logged, not silent
        # The device was still returned — a leaked udid would hang this on `free.get()`.
        retry = lease(_eff(), _scn("b"))
        assert len(created) == 3 and created[2] is not env and created[2].started
        retry.release()
    finally:
        shutdown()  # no lease defect was stashed — must not raise


@pytest.mark.parametrize(
    "reusable",
    [
        pytest.param(False, id="teardown"),
        pytest.param(True, id="end_lease"),
    ],
)
def test_device_pool_release_defers_a_lease_teardown_wiring_defect_to_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    reusable: bool,
) -> None:
    """BE-0342: unlike an expected process failure (above), a *wiring* defect on a lease's own
    end-of-lease teardown cannot raise from `release()` either — the same reasoning, it would replace
    the scenario's own result and skip `free.put(udid)` — but silently warning it away forever would
    let a teardown that is *structurally* broken (not merely a runner that happened to be gone) ship
    unnoticed: for a backend with no warm resident (web, adb) it is the only teardown that ever runs,
    so nothing else would ever catch it. `release()` stashes it instead, and `shutdown()` raises it
    once every lease this run held has been released."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(actuator, udid, provision, reusable=reusable)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    first = lease(_eff(), _scn("a"))
    env = created[1]  # created[0] is the pool's representative env
    if reusable:
        env.end_lease_error = AttributeError("close")
    else:
        env.teardown_error = AttributeError("close")
    first.release()  # must not raise despite the wiring defect
    assert env.torn  # the plain arm's own teardown, or the end_lease arm's fallback teardown
    # The device was still returned — a leaked udid would hang this on `free.get()` — even though
    # the defect it hit is still owed a loud failure, deferred to `shutdown()` below.
    retry = lease(_eff(), _scn("b"))
    assert len(created) == 3 and created[2] is not env and created[2].started
    retry.release()  # a later, defect-free release must not clear the earlier stashed defect
    with pytest.raises(AttributeError, match="close"):
        shutdown()


def test_device_pool_logs_a_second_lease_teardown_defect_rather_than_dropping_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-0342: when more than one lease's own teardown hits a wiring defect, `shutdown()` still
    raises only the first — the same "first raised, rest logged" shape its own sweep already uses —
    but a later one is not silently dropped either; it gets its own log line."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(
            actuator, udid, provision, teardown_error=RuntimeError(f"broken {udid}")
        )
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A", "UDID-B"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    la = lease(_eff(), _scn("a"))
    lb = lease(_eff(), _scn("b"))
    la.release()  # the first lease-teardown defect this run hits
    with caplog.at_level(logging.ERROR, logger="bajutsu.common.runner.pool"):
        lb.release()  # the second — logged here rather than silently dropped
    with pytest.raises(RuntimeError, match="broken UDID-A"):  # only the first propagates
        shutdown()
    assert "UDID-B" in caplog.text


def test_device_pool_does_not_cache_a_non_reusable_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE-0291: an environment with no warm resident (web / android) is untouched — released by
    the full teardown, never kept warm, and rebuilt fresh each lease, exactly as before."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(actuator, udid, provision, reusable=False)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        a = lease(_eff(), _scn("a"))
        a.release()
        env_a = created[1]  # created[0] is the pool's representative env; [1] is lease a's env
        assert env_a.torn and env_a.end_lease_count == 0  # full teardown, never kept warm
        b = lease(_eff(), _scn("b"))
        assert len(created) == 3  # a fresh environment each lease — no reuse
        b.release()
    finally:
        shutdown()


def test_device_pool_bridges_the_collector_before_launch_and_tears_it_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE-0283: for an external-receiver backend (Android), the pool makes the host collector
    reachable from the device before launch (`adb reverse`) and releases the tunnel with the lease."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(actuator, udid, provision)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["android"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        leased = lease(_eff(), _scn("a"))
        env = created[-1]  # the lease env
        assert isinstance(leased.collector, NetworkCollector)
        assert env.bridged_port == leased.collector.port  # tunnels the pre-started collector's port
        assert env.bridged_before_launch  # established BEFORE the app launched
        assert not env.bridge_torn
        leased.release()
        assert env.bridge_torn  # the tunnel is released with the lease
    finally:
        shutdown()


def test_device_pool_release_swallows_a_bridge_teardown_failure_and_still_frees_the_device(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-0342: `release_bridge()` runs first in `release()`, so a failure tearing down the
    device-side tunnel (a device that dropped off the bus) must not skip the rest of `release()` or
    `free.put(udid)` behind it — the same leak this item's other teardown sites guard against."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(actuator, udid, provision, fail_bridge_teardown=True)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["android"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        leased = lease(_eff(), _scn("a"))
        env = created[-1]
        with caplog.at_level(logging.WARNING, logger="bajutsu.common.runner.recovery"):
            leased.release()  # must not raise despite the bridge teardown failing
        assert env.bridge_torn and env.torn  # the rest of release() still ran
        assert "at the lease's end" in caplog.text  # logged, not silent
        # The device was still returned — a leaked udid would hang this on `free.get()`.
        retry = lease(_eff(), _scn("b"))
        retry.release()
    finally:
        shutdown()


def test_device_pool_releases_the_bridge_when_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch failure after the bridge is up must not leak the tunnel (BE-0283)."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(actuator, udid, provision, fail_start=True)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["android"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        with pytest.raises(RuntimeError, match="launch failed"):
            lease(_eff(), _scn("a"))
        env = created[-1]
        assert env.bridged_port is not None  # the bridge was established...
        assert env.bridge_torn  # ...and torn down on the failure path
    finally:
        shutdown()


def test_device_pool_threads_provision_to_pool_and_lease_environments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE-0236: a `ProvisionProfile` handed to `device_pool` reaches every environment it builds — the
    representative pool env *and* each per-lease env — so a cloud provider's already-booted /
    pre-installed device skips the bring-up it doesn't need. Guards against a call site in `pool.py`
    silently dropping `provision=provision`."""
    created: list[_RecordingEnv] = []

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        env = _RecordingEnv(actuator, udid, provision)
        created.append(env)
        return env

    monkeypatch.setattr("bajutsu.common.runner.pool.environment_for", fake_env_for)

    profile = ProvisionProfile(
        boot_ready=True, app_preinstalled=True
    )  # non-default: cloud handover
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
        provision=profile,
    )
    try:
        assert created[0].provision is profile  # the pool env saw exactly the profile passed in
        leased = lease(_eff(), _scn("tap"))
        assert created[-1].provision is profile  # and so did the per-lease env
        leased.release()
    finally:
        shutdown()


def _eff_web() -> Effective:
    return _web_eff(base_url="http://x/index.html")


def test_device_pool_web_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """The web lane: no simctl catalog/control/collector; the driver owns the browser, so
    launch == navigate, relaunch == re-navigate, and release == close."""
    fakes: list[_FakeWeb] = []

    def fake_make_driver(
        actuator: str,
        udid: str,
        base_url: str | None = None,
        headless: bool = True,
        browser: str = "chromium",
        device_mode: str = "desktop",
        record_video_dir: object = None,
    ) -> base.Driver:
        assert actuator == "playwright"
        assert base_url == "http://x/index.html"  # threaded from eff.base_url
        assert headless is True  # threaded from eff.headless (default headless)
        assert browser == "chromium"  # threaded from eff.browser (default engine, BE-0076)
        assert device_mode == "desktop"  # threaded from eff.device_mode (default, BE-0228)
        d = _FakeWeb([_el("home.title", "H"), _el("ok", "OK")])
        fakes.append(d)
        return d

    monkeypatch.setattr("bajutsu.common.backends.make_driver", fake_make_driver)
    lease, shutdown = device_pool(
        ["web"], ["web"], _eff_web(), Path("runs"), network=False, available=lambda b: True
    )
    try:
        leased = lease(_eff_web(), _scn("a"))
        assert leased.control is None  # no simctl device control
        assert leased.collector is None  # network off for web
        assert isinstance(leased.sink, FileSink)
        assert leased.sink.udid == "web"
        assert fakes[0].navigated == 1  # launch == navigate to base_url
        assert leased.relaunch is not None
        leased.relaunch(Relaunch())  # re-navigate, no device restart
        assert fakes[0].navigated == 2
        leased.release()  # tears the browser down
        assert fakes[0].closed == 1
    finally:
        shutdown()


def test_device_pool_web_lease_builds_a_page_hooked_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--network` on web: no up-front HTTP receiver; the lease hooks a collector to the live
    page and threads this scenario's mocks into it (BE-0054). The collector satisfies the
    `Collector` protocol and is stopped on release."""
    from bajutsu.common.evidence.network import Collector

    fakes: list[_FakeWeb] = []

    def fake_make_driver(
        actuator: str,
        udid: str,
        base_url: str | None = None,
        headless: bool = True,
        browser: str = "chromium",
        device_mode: str = "desktop",
        record_video_dir: object = None,
    ) -> base.Driver:
        d = _FakeWeb([_el("home", "H"), _el("ok", "OK")])
        fakes.append(d)
        return d

    monkeypatch.setattr("bajutsu.common.backends.make_driver", fake_make_driver)
    lease, shutdown = device_pool(
        ["web"], ["web"], _eff_web(), Path("runs"), network=True, available=lambda b: True
    )
    try:
        scn = Scenario.model_validate(
            {"name": "a", "mocks": [{"match": {"path": "/x"}}], "steps": [{"tap": {"id": "ok"}}]}
        )
        leased = lease(_eff_web(), scn)
        assert isinstance(leased.collector, Collector)  # protocol-satisfying
        assert leased.collector is fakes[0].collector  # the page-hooked collector, not an HTTP one
        assert fakes[0].collector_mocks == scn.mocks  # this scenario's mocks were wired in
        leased.release()
        assert fakes[0].collector is not None and fakes[0].collector.stopped is True
    finally:
        shutdown()


def test_device_pool_release_swallows_a_page_hooked_collector_stop_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-0342: `release_collector.stop()` in `release()` (the page-hooked collector, not the
    up-front HTTP receiver `shutdown()` sweeps separately) must not skip `free.put(udid)` either —
    an already-closed socket there is an expected failure, warned and swallowed like the rest of
    `release()`'s teardown chain."""
    fakes: list[_FakeWeb] = []

    def fake_make_driver(
        actuator: str,
        udid: str,
        base_url: str | None = None,
        headless: bool = True,
        browser: str = "chromium",
        device_mode: str = "desktop",
        record_video_dir: object = None,
    ) -> base.Driver:
        d = _FakeWeb([_el("home", "H"), _el("ok", "OK")], fail_collector_stop=True)
        fakes.append(d)
        return d

    monkeypatch.setattr("bajutsu.common.backends.make_driver", fake_make_driver)
    lease, shutdown = device_pool(
        ["web"], ["web"], _eff_web(), Path("runs"), network=True, available=lambda b: True
    )
    try:
        leased = lease(_eff_web(), _scn("a"))
        with caplog.at_level(logging.WARNING, logger="bajutsu.common.runner.recovery"):
            leased.release()  # must not raise despite the collector failing to stop
        assert fakes[0].collector is not None and fakes[0].collector.stopped is True
        assert "at the lease's end" in caplog.text  # logged, not silent
        # The device was still returned — a leaked udid would hang this on `free.get()`.
        retry = lease(_eff_web(), _scn("b"))
        retry.release()
    finally:
        shutdown()


def test_device_pool_web_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # A web app with no baseUrl fails cleanly at launch (simctl.DeviceError), not deep in Playwright.
    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver",
        lambda actuator, udid, base_url=None: FakeDriver([]),
    )
    eff_no_url = _web_eff(base_url=None)
    lease, shutdown = device_pool(
        ["web"], ["web"], eff_no_url, Path("runs"), network=False, available=lambda b: True
    )
    try:
        with pytest.raises(simctl.DeviceError, match="baseUrl"):
            lease(eff_no_url, _scn("a"))
    finally:
        shutdown()


def test_device_pool_uses_a_resolved_network_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # When a same-platform read-only provider is resolved (BE-0020), its collector supplies network
    # instead of the actuator's app-side one, and the lease's provenance names it as a fallback.
    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver",
        lambda actuator, udid: FakeDriver([_el("home", "H"), _el("ok", "OK")]),
    )
    ex = NetworkExchange(method="GET", path="/items", status=200)
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["fake"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
        make_driver=lambda actuator, udid: FakeDriver(exchanges=[ex]),
        evidence_providers=lambda backends, actuator, available: ({"network": "fake"}, {}),
    )
    lz = None
    try:
        lz = lease(_eff(), _scn("a"))
        assert lz.collector_provider == "fake (fallback)"
        assert lz.collector is not None and lz.collector.snapshot() == [ex]
    finally:
        if lz is not None:
            lz.release()
        shutdown()


def test_device_pool_releases_resources_when_launch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # If launch_driver raises after the fallback collector is built (BE-0020), the lease must stop
    # that collector and return the udid to the pool, so one failure neither leaks a socket nor
    # starves later leases. A flaky launch fails once; the retry must then lease the freed device
    # (a never-returned udid would block free.get() forever).
    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver",
        lambda actuator, udid: FakeDriver([_el("home", "H"), _el("ok", "OK")]),
    )

    class _RecordingCollector(FakeNetworkCollector):
        def __init__(self) -> None:
            super().__init__([])
            self.stopped = False

        def snapshot(self) -> list[NetworkExchange]:
            return []

        def snapshot_timed(self) -> list[tuple[NetworkExchange, float]]:
            return []

        def transitions_snapshot_timed(self) -> list[tuple[ScreenTransition, float]]:
            return []

        def clear(self) -> None:
            pass

        def stop(self) -> None:
            self.stopped = True

    built: list[_RecordingCollector] = []

    class _Provider(FakeDriver):
        def network_collector(self, mocks: object = None) -> _RecordingCollector:
            c = _RecordingCollector()
            built.append(c)
            return c

    launches = {"n": 0}

    def flaky_launch(*args: object, **kwargs: object) -> tuple[base.Driver, ReadinessResult]:
        launches["n"] += 1
        if launches["n"] == 1:
            raise simctl.DeviceError("boot failed")
        return FakeDriver([_el("home", "H"), _el("ok", "OK")]), ReadinessResult(True, "count", 0.0)

    monkeypatch.setattr("bajutsu.common.runner.pool.launch_driver", flaky_launch)

    lease, shutdown = device_pool(
        ["UDID-A"],
        ["fake"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
        make_driver=lambda actuator, udid: _Provider(),
        evidence_providers=lambda backends, actuator, available: ({"network": "fake"}, {}),
    )
    lz = None
    try:
        with pytest.raises(simctl.DeviceError, match="boot failed"):
            lease(_eff(), _scn("a"))
        # The collector built for the failed attempt was stopped (no leaked socket).
        assert len(built) == 1 and built[0].stopped is True
        # The device was returned: a retry leases it (would block forever otherwise).
        lz = lease(_eff(), _scn("a"))
        assert lz.udid == "UDID-A"
    finally:
        if lz is not None:
            lz.release()
        shutdown()


def test_device_pool_network_lease_defaults_to_collector_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no fallback resolved (today's iOS), the app-side collector supplies network and the
    # provenance stays "collector".
    monkeypatch.setattr(
        "bajutsu.common.backends.make_driver",
        lambda actuator, udid: FakeDriver([_el("home", "H"), _el("ok", "OK")]),
    )
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["fake"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    lz = None
    try:
        lz = lease(_eff(), _scn("a"))
        assert lz.collector_provider == "collector"
    finally:
        if lz is not None:
            lz.release()
        shutdown()


# --- following a lease onto a replaced device --- #
#
# The XCUITest Simulator lifecycle creates a replacement when CoreSimulator stops listing the leased
# device. The pool keys leases, collectors, evidence capture and its warm cache by udid, so it has to
# follow — otherwise all of them keep naming a device that is gone.


def _replacing_env_factory(
    created: list[_RecordingEnv],
    *,
    replacement: str | None,
    reusable: bool = True,
    fail_start: bool = False,
    replacement_catalog: dict[str, dict[str, str]] | None = None,
    fail_device_catalog: bool = False,
) -> Callable[..., _RecordingEnv]:
    """An `environment_for` whose *lease* environments report a device replacement during start."""

    def fake_env_for(
        actuator: str,
        udid: str,
        env_run: object = None,
        *,
        provision: object = None,
        respawn: bool = False,
    ) -> _RecordingEnv:
        # The first env built is the pool's representative (never leased), so it reports no swap.
        env = _RecordingEnv(
            actuator,
            udid,
            provision,
            reusable=reusable,
            fail_start=fail_start and bool(created),
            replacement=replacement if created else None,
            catalog=replacement_catalog if created else None,
            fail_device_catalog=fail_device_catalog and bool(created),
        )
        created.append(env)
        return env

    return fake_env_for


def test_device_pool_follows_a_lease_onto_a_replacement_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_RecordingEnv] = []
    monkeypatch.setattr(
        "bajutsu.common.runner.pool.environment_for",
        _replacing_env_factory(
            created,
            replacement="UDID-NEW",
            replacement_catalog={"UDID-NEW": {"name": "iPhone 17 Pro", "runtime": "iOS 26.0"}},
        ),
    )
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        lz = lease(_eff(), _scn("s"))
        # The result names the device that actually ran the scenario, not the one that vanished.
        assert lz.udid == "UDID-NEW"
        # The catalog re-key follows the replacement too, or the report would attribute the scenario
        # to a device whose model/runtime row reads as blank rather than as a bug (BE-XXXX unit 5).
        assert lz.device_name == "iPhone 17 Pro" and lz.device_runtime == "iOS 26.0"
        lz.release()
        # The replacement took the vanished device's place in the pool, so the next lease gets it and
        # the dead udid is never handed out again.
        second = lease(_eff(), _scn("s2"))
        assert second.udid == "UDID-NEW"
        second.release()
    finally:
        shutdown()


def test_device_pool_hands_the_lease_its_environments_replacement_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # BE-0354: the crash retry asks through the `Lease`, after that lease has already been released,
    # and what it must reach is the environment the pool keeps for the device — the one whose *next*
    # `start` serves the swap. Anything else (a fresh environment, the pool's representative) would
    # take the request and drop it.
    created: list[_RecordingEnv] = []
    monkeypatch.setattr(
        "bajutsu.common.runner.pool.environment_for",
        _replacing_env_factory(created, replacement=None, reusable=True),
    )
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        lz = lease(_eff(), _scn("s"))
        lz.release()
        lz.request_device_replacement()
        assert created[-1].replacement_requests == 1
        # Nothing recorded a video, so the stall signal is a first-class False rather than unknown.
        assert lz.video_start_stalled() is False
        # The crash capture is read back after release through the same environment, and for the same
        # reason: the crash it holds belongs to this device (BE-0421).
        assert lz.crash_artifacts() == [("runner-UDID-A.log", b"crashed")]
        # Taken off the environment once, as the lease released — not read off it on demand, which
        # under `workers > 1` would cross this scenario's crash with the next lease's (BE-0421).
        assert created[-1].snapshots_taken == 1
    finally:
        shutdown()


def test_device_pool_frees_the_replacement_when_the_lease_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A replacement made before the failure is still adopted: the queue must get the live device back,
    # or every later lease would spawn onto the one that vanished.
    created: list[_RecordingEnv] = []
    monkeypatch.setattr(
        "bajutsu.common.runner.pool.environment_for",
        _replacing_env_factory(created, replacement="UDID-NEW", fail_start=True),
    )
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        with pytest.raises(RuntimeError, match="launch failed"):
            lease(_eff(), _scn("s"))
        # The next lease is handed the replacement, proving the failure path freed that one.
        assert created[-1].udid == "UDID-A"
        with pytest.raises(RuntimeError, match="launch failed"):
            lease(_eff(), _scn("s2"))
        assert created[-1].udid == "UDID-NEW"
    finally:
        shutdown()


def test_device_pool_frees_the_live_replacement_even_when_adopting_it_also_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-0342: when `env.start` itself raises (as above), the success path's own `adopt_replacement()`
    call is never reached, so the `except` block's call is the *first* one to see the replacement — and
    its `device_catalog()` re-fetch can fail there too, the same way it can on the success path. Left
    unguarded, that second failure would replace the *original* launch error and skip `free.put`/the
    `raise` below, hanging the next lease on `free.get()` forever instead of merely losing the catalog
    metadata for a device that is otherwise perfectly leasable."""
    created: list[_RecordingEnv] = []
    monkeypatch.setattr(
        "bajutsu.common.runner.pool.environment_for",
        _replacing_env_factory(
            created, replacement="UDID-NEW", fail_start=True, fail_device_catalog=True
        ),
    )
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        with (
            caplog.at_level(logging.WARNING, logger="bajutsu.common.runner.recovery"),
            pytest.raises(RuntimeError, match="launch failed"),  # the original error, not masked
        ):
            lease(_eff(), _scn("s"))
        assert (
            "adopting" in caplog.text and "replacement" in caplog.text
        )  # swallowed, not left to hang
        # The next lease is handed the *live* replacement, not the dead device — a leaked dead udid
        # would either hang this on `free.get()` or spawn straight back onto the vanished device. This
        # factory's every leased env also fails to start, so the second lease fails too; what matters
        # is which udid `environment_for` was called with for it.
        with pytest.raises(RuntimeError, match="launch failed"):
            lease(_eff(), _scn("s2"))
        assert created[-1].udid == "UDID-NEW"
    finally:
        shutdown()


def test_device_pool_re_keys_the_collector_onto_the_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The collector is a host-side receiver the device reaches over the loopback, so it needs no
    # restart — but a later lease looks it up by udid, so the key has to move with the device.
    created: list[_RecordingEnv] = []
    monkeypatch.setattr(
        "bajutsu.common.runner.pool.environment_for",
        _replacing_env_factory(created, replacement="UDID-NEW"),
    )
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=True,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        first = lease(_eff(), _scn("s"))
        port = first.collector.port  # type: ignore[union-attr]
        first.release()
        second = lease(_eff(), _scn("s2"))
        # Same receiver, now found under the replacement's udid: it was re-keyed, not re-created. The
        # udid assertion is what makes this discriminating — a collector left under the dead key would
        # leave this lease with none at all.
        assert second.udid == "UDID-NEW"
        assert isinstance(second.collector, NetworkCollector) and second.collector.port == port
        second.release()
    finally:
        shutdown()


def test_device_pool_leaves_every_key_alone_when_no_device_was_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The guard: an environment reporting no swap (every platform but that one path) must leave the
    # pool's bookkeeping exactly as it was.
    created: list[_RecordingEnv] = []
    monkeypatch.setattr(
        "bajutsu.common.runner.pool.environment_for",
        _replacing_env_factory(created, replacement=None),
    )
    lease, shutdown = device_pool(
        ["UDID-A"],
        ["ios"],
        _eff(),
        Path("runs"),
        network=False,
        available=lambda b: True,
        env_run=lambda *a, **k: "",
    )
    try:
        for _ in range(2):
            lz = lease(_eff(), _scn("s"))
            assert lz.udid == "UDID-A"
            lz.release()
        # One lease environment served both scenarios: the warm cache was never dropped and re-keyed.
        assert len(created) == 2
    finally:
        shutdown()
