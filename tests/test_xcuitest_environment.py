"""Tests for the XCUITest environment's target selection (BE-0238 Unit 1).

BE-0019 drove only the Simulator; BE-0238 generalises the same `xcodebuild test-without-building`
driving layer to a real iOS device, where the only difference is the `-destination` platform and
that the simctl device-prep (erase / boot / install / permissions) does not apply. The Simulator's
own boot path still needs Xcode, so it stays off the gate; here the target-selection logic and the
loud refusal of simctl-only operations against a real device are exercised without one — the
`xcodebuild`/toolchain boundary is the sanctioned fake point (the same one BE-0019 fakes).
"""

from __future__ import annotations

import os
import plistlib
import re
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from bajutsu.common import backends, stall_diagnostics
from bajutsu.common.backend_cli import simctl
from bajutsu.common.config import Effective, load_config, resolve
from bajutsu.common.devices.os import DeviceOS
from bajutsu.common.drivers.fake import FakeDriver
from bajutsu.common.drivers.xcuitest import XcuitestChannelError
from bajutsu.common.platform_lifecycle.environments.xcuitest import (
    _MAX_WARM_REUSES,
    _MAX_WARM_REUSES_ENV,
    _RECOVERY_TIMEOUT,
    _RESPAWN_TIMEOUT_ENV,
    _RESULT_BUNDLE_ENV,
    _RUNNER_STARTUP_TIMEOUT,
    _RUNNER_STARTUP_TIMEOUT_ENV,
    _WARM_HEALTH_TIMEOUT,
    XcuitestEnvironment,
    _AttemptFailure,
    _await_cold_runner,
    _destination,
    _diagnostic_reports_dir,
    _Recovery,
    _recovery_timeout,
    _reports_since,
    _respawn_timeout,
    _run_ended_probe,
    _runner_host_bundle_ids,
    _runner_startup_timeout,
    _spawn_cold_with_retry,
    _Spawned,
)

# `_allocate_port` is read from the environment module's own globals since BE-0411 split the
# package, so the package's re-export is not what the spawn resolves.
from bajutsu.common.platform_lifecycle.environments.xcuitest import (
    xcuitest_environment as xcuitest_env_impl,
)
from bajutsu.common.scenario import Preconditions

_DEVICE_UDID = "00008030-000A1B2C3D4E"  # a physical-device id shape (not a simctl UUID)


def _device_eff(*, app_path: str | None = None, test_runner: str | None = None) -> Effective:
    lines = ["targets:", "  s:", "    bundleId: com.x"]
    if app_path is not None:
        lines.append(f"    appPath: {app_path}")
    lines += ["    xcuitest:", "      deviceType: device"]
    if test_runner is not None:
        lines.append(f"      testRunner: {test_runner}")
    return resolve(load_config("\n".join(lines) + "\n"), "s")


# --- the destination string (pure) --- #


class _NamedDriver(FakeDriver):
    """A `FakeDriver` carrying a name, so a retry test can assert *which* attempt's driver came back
    without standing a bare string in a `Driver` slot (BE-0388)."""

    def __init__(self, name: str = "spawned") -> None:
        super().__init__([])
        self.name = name


def test_destination_targets_the_simulator_by_default() -> None:
    assert _destination("simulator", "ABC123") == "platform=iOS Simulator,id=ABC123"


def test_destination_targets_a_real_device() -> None:
    # A real device drops the "Simulator" suffix; xcodebuild then addresses the attached device.
    assert _destination("device", _DEVICE_UDID) == f"platform=iOS,id={_DEVICE_UDID}"


def test_destination_validates_the_udid() -> None:
    # A leading-dash id would be read by xcodebuild as an option — refuse it (the shared device_id
    # policy `validated_udid` enforces, applied to a real-device id the same as a simulator one).
    with pytest.raises(simctl.DeviceError):
        _destination("device", "-rf")


# --- real-device start(): skip simctl, refuse simctl-only operations loudly --- #


def test_start_on_a_real_device_refuses_simctl_install() -> None:
    # A real device installs its build out of band; asking the simctl installer to place `appPath`
    # must fail loudly (BE-0238 Unit 2/3), never silently skip (determinism first).
    env = XcuitestEnvironment("xcuitest", _DEVICE_UDID)
    with pytest.raises(simctl.DeviceError, match="real device"):
        env.start(_device_eff(app_path="build/App.app"), Preconditions())


def test_start_on_a_real_device_refuses_permission_grants() -> None:
    env = XcuitestEnvironment("xcuitest", _DEVICE_UDID)
    with pytest.raises(simctl.DeviceError, match="real device"):
        env.start(_device_eff(), Preconditions(), permissions={"camera": "yes"})


def test_start_on_a_real_device_refuses_erase() -> None:
    env = XcuitestEnvironment("xcuitest", _DEVICE_UDID)
    with pytest.raises(simctl.DeviceError, match="real device"):
        env.start(_device_eff(), Preconditions(erase=True))


def test_start_on_a_real_device_targets_the_device_and_skips_simctl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # The happy real-device path: no simctl call, and the xcodebuild destination is the device.
    # Faked at the process boundary (Popen) and the driver factory — the runner needs Xcode + a
    # device, which the gate has neither of.
    runner = tmp_path / "Runner.xctestrun"
    with runner.open("wb") as f:
        plistlib.dump({"Target": {"TestingEnvironmentVariables": {}}}, f)

    simctl_calls: list[list[str]] = []

    def _record_run(argv: list[str], extra_env: Mapping[str, str] | None = None) -> str:
        simctl_calls.append(argv)
        return ""

    captured: dict[str, list[str]] = {}

    class _FakeProc:
        def poll(self) -> int | None:
            return None  # alive: the cold-spawn liveness check (BE-0319) never trips

        def terminate(self) -> None: ...
        def wait(self, timeout: float | None = None) -> int:
            return 0

    def _fake_popen(argv: list[str], **_kw: Any) -> _FakeProc:
        captured["argv"] = argv
        return _FakeProc()

    class _FakeDriver:
        def await_ready(self, timeout: float) -> None: ...
        def health_ready(self) -> bool:
            return True  # the cold runner answers /health at once (BE-0319)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(backends, "make_driver", lambda *_a, **_k: _FakeDriver())

    env = XcuitestEnvironment("xcuitest", _DEVICE_UDID, env_run=_record_run)
    env.start(_device_eff(test_runner=str(runner)), Preconditions())

    assert f"platform=iOS,id={_DEVICE_UDID}" in captured["argv"]
    assert simctl_calls == []  # a real device is never touched through simctl
    # A real-device runner is torn down per lease, never kept warm: this is the one guard keeping the
    # pool's warm cache (runner/pool.py) from reusing a real device's runner across scenarios, so an
    # inverted condition here would silently skip its per-lease teardown (BE-0291).
    assert not env.has_reusable_resident()


def test_a_real_device_never_enters_the_recovery_ladder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A real device is powered on out of band and is not listed by `simctl list devices`, so a probe
    # would read it as "gone" and mint a Simulator to replace it — finishing the run on a device the
    # target never named. The ladder is Simulator-only, and this is what says so.
    simctl_calls: list[list[str]] = []

    def _run(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        simctl_calls.append(argv)
        return ""

    class _DeadProc:
        pid = 9999

        def poll(self) -> int:
            return 70  # exits at once: both attempts fail, so `recover` runs in between

        def wait(self, timeout: float | None = None) -> int:
            return 0

    class _Driver:
        def health_ready(self) -> bool:
            return False  # /health never answers ready

        def await_ready(self, timeout: float = 10.0) -> None: ...

    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: _DeadProc())
    monkeypatch.setattr(backends, "make_driver", lambda *_a, **_k: _Driver())
    _patch_group_signals(monkeypatch)
    env = XcuitestEnvironment("xcuitest", _DEVICE_UDID, env_run=_run)
    with pytest.raises(XcuitestChannelError, match="did not come up"):
        env.start(_device_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert simctl_calls == []  # no probe, no reboot, and above all no `simctl create`
    assert env.replaced_device() is None


# --- the live-route boundary: an Appium endpoint routes around the udid machinery (BE-0238) --- #


def test_destination_still_rejects_a_url_as_a_udid() -> None:
    # `_destination` itself is unchanged: a URL passed to it directly is still rejected by the shared
    # device_id policy (its `//` is outside the charset). This is the defense-in-depth guard — the live
    # route now keeps a real endpoint away from `_destination` entirely (see the routing test below),
    # so this only fires if a URL ever reached the simctl / xcodebuild udid machinery by mistake.
    with pytest.raises(simctl.DeviceError, match="invalid udid"):
        _destination("device", "http://grid.local:4723")


def test_appium_lease_endpoint_routes_to_the_live_environment() -> None:
    # The live transport (Slice A) closes the Unit 4 boundary: the endpoint the `appium` provider
    # yields no longer flows into `_destination`. `environment_for` recognises the `http(s)://` udid
    # spec and returns the live WebDriver environment, which drives the reserved device off the simctl
    # / xcodebuild path — so the endpoint reaches the WebDriver session, never the udid machinery
    # (BE-0238).
    from bajutsu.common.platform_lifecycle.environments.xcuitest_live import XcuitestLiveEnvironment
    from bajutsu.common.platform_lifecycle.factories import environment_for
    from bajutsu.common.runner import device_provider as dp

    cfg = load_config(
        "targets:\n  s:\n    bundleId: com.x\n    xcuitest:\n      deviceType: device\n"
        "    deviceProvider:\n      kind: appium\n      endpoint: http://grid.local:4723\n"
    )
    lease = dp.acquire_device(resolve(cfg, "s"), "booted")
    assert lease.udid_spec == "http://grid.local:4723"  # the endpoint, not the --udid flag
    env = environment_for("xcuitest", lease.udid_spec)
    assert isinstance(env, XcuitestLiveEnvironment)
    # The live environment passes the endpoint through instead of resolving it through simctl.
    assert env.resolve_device(lease.udid_spec) == "http://grid.local:4723"


# --- warm runner reuse across leases on a Simulator (BE-0291) --- #
#
# The Simulator's own boot needs Xcode, so it stays off the gate; the `xcodebuild`/toolchain boundary
# (Popen), the driver factory, and simctl are the sanctioned fake points (as in the BE-0019 tests
# above). These exercise the reuse *logic* — spawn once, resume, respawn on erase / a wedged runner —
# without a Simulator.


def _sim_eff(*, test_runner: str, app_path: str | None = None) -> Effective:
    lines = ["targets:", "  s:", "    bundleId: com.x"]
    if app_path is not None:
        lines.append(f"    appPath: {app_path}")
    lines += ["    xcuitest:", f"      testRunner: {test_runner}"]
    return resolve(load_config("\n".join(lines) + "\n"), "s")


class _FakeProc:
    """A fake runner process: `poll()` reports liveness, `terminate()`/`kill()` end it.

    Each instance registers itself in `_FAKE_PROCS` under a unique pid so `_patch_group_signals` can
    route a group signal back to it — the discard signals the runner's *process group*, not the bare
    process, so a fake that only implemented `terminate()` would never see the signal at all.
    """

    _next_pid = 9000

    def __init__(self) -> None:
        self.alive = True
        self.terminated = (
            False  # observed by the mid-run-crash test (a dead runner is not signalled)
        )
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        _FAKE_PROCS[self.pid] = self

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.alive = False


_FAKE_PROCS: dict[int, _FakeProc] = {}


def _patch_group_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the discard's process-group signals to the `_FakeProc` they name.

    Every fake process is its own group leader here (`getpgid` is the identity), so a SIGTERM to the
    group ends exactly the fake that was spawned. Without this the real `os.killpg` would signal
    whatever group on the test host happens to carry the fake's pid.
    """
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    def _killpg(pgid: int, sig: int) -> None:
        proc = _FAKE_PROCS.get(pgid)
        if proc is None:
            raise ProcessLookupError(pgid)
        proc.terminate() if sig == signal.SIGTERM else proc.kill()

    monkeypatch.setattr("os.killpg", _killpg)


def _write_runner(tmp_path: Path, *, host_bundle_id: str | None = None) -> Path:
    """A minimal `.xctestrun`; `host_bundle_id` adds the XCTRunner app id a real one always carries."""
    runner = tmp_path / "Runner.xctestrun"
    target: dict[str, Any] = {"TestingEnvironmentVariables": {}}
    if host_bundle_id is not None:
        target["TestHostBundleIdentifier"] = host_bundle_id
    with runner.open("wb") as f:
        plistlib.dump({"Target": target}, f)
    return runner


def _globals_plist(locale: str | None) -> str:
    """The device's global preference domain as `defaults export` renders it (BE-0320).

    `None` models a device that carries no pinned language — an unwritten domain — which is what a
    fresh fake device starts from.
    """
    if locale is None:
        return plistlib.dumps({}).decode()
    language, _, _ = locale.partition("_")
    return plistlib.dumps({"AppleLanguages": [language], "AppleLocale": locale}).decode()


# What each faked `make_driver` call was handed, newest last. Module-level rather than another
# element of `_fake_toolchain`'s return tuple, which some thirty existing tests already unpack.
DRIVER_KWARGS: list[dict[str, object]] = []


def _fake_toolchain(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wedged: dict[str, bool] | None = None,
    system_locale: dict[str, str | None] | None = None,
    export_fails: bool = False,
    stays_booted_after_shutdown: bool = False,
) -> tuple[list[list[str]], list[list[str]], simctl.RunFn]:
    """Fake Popen (the runner), the driver factory, and simctl; return (popen log, simctl log, run).

    The returned `run` is the fake simctl runner to hand the environment as `env_run`. `wedged`, when
    given, makes the driver's *warm* health probe (`_WARM_HEALTH_TIMEOUT`) raise while `wedged["v"]`
    is True, so a test can wedge the reused runner; the cold-startup `await_ready` (the long timeout)
    always succeeds, so a respawn still comes up.

    The kwargs each `make_driver` call received are recorded into the module-level `DRIVER_KWARGS`
    (reset per fixture), so a test can assert on the callbacks the environment injects.

    `system_locale`, when given, models the device's global preference domain in `["v"]` — the fake
    device answers `defaults export` from it and a `defaults write` updates it, so a test can drive
    the BE-0320 pin the way a real Simulator would answer. Omitted, the domain reads as unwritten and
    every cold spawn pins it. `export_fails` instead makes every read of that domain fail, modelling
    a device whose pin can be written but never confirmed.

    The fake device also tracks whether it is booted, so the pin's own post-shutdown read-back
    (BE-0359) sees what a real device would. `stays_booted_after_shutdown` models a CoreSimulator
    wedged enough that `simctl shutdown` silently no-ops — the case that read-back exists to catch.
    """
    popen_argvs: list[list[str]] = []
    simctl_calls: list[list[str]] = []
    DRIVER_KWARGS.clear()
    domain: dict[str, str | None] = system_locale if system_locale is not None else {"v": None}
    booted = {"v": True}  # the leased device starts booted, as one handed over by the pool is

    def _popen(argv: list[str], **_kw: Any) -> _FakeProc:
        popen_argvs.append(argv)
        return _FakeProc()

    class _Driver:
        def await_ready(self, timeout: float = 10.0) -> None:
            if wedged is not None and wedged["v"] and timeout == _WARM_HEALTH_TIMEOUT:
                raise XcuitestChannelError("wedged")

        def health_ready(self) -> bool:
            return True  # the cold runner answers /health at once (BE-0319 unit 3)

    def _run(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        simctl_calls.append(argv)
        if argv[2:3] == ["erase"]:
            domain["v"] = None  # a real erase wipes the device's preferences with everything else
        if argv[2:3] == ["shutdown"] and not stays_booted_after_shutdown:
            booted["v"] = False
        if argv[2:3] in (["boot"], ["bootstatus"]):
            booted["v"] = True
        if argv[2:5] == ["list", "devices", "booted"]:
            return _device_json(["UDID"] if booted["v"] else [])
        if argv[2:3] == ["list"]:
            # The device listing a cold prep reads to record what kind of device this is, so a
            # replacement can later be cloned from it.
            return _device_json(["UDID"])
        if argv[4:7] == ["defaults", "export", "-globalDomain"]:
            if export_fails:
                raise subprocess.CalledProcessError(1, argv, stderr="device not booted")
            return _globals_plist(domain["v"])
        if argv[4:8] == ["defaults", "write", "-globalDomain", "AppleLocale"]:
            domain["v"] = argv[-1]
        return ""

    def _make_driver(*_a: object, **kwargs: Any) -> _Driver:
        # Recorded, not discarded: the environment injects `runner_alive` and BE-0361's `on_stall`
        # here, and a swallowed **kwargs would let either wiring be deleted with the suite still green.
        DRIVER_KWARGS.append(kwargs)
        return _Driver()

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(backends, "make_driver", _make_driver)
    _patch_group_signals(monkeypatch)
    return popen_argvs, simctl_calls, _run


def test_start_reuses_a_healthy_runner_across_leases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The core amortization: a second lease on the same device resumes the live runner (app relaunch
    # only) instead of spawning a second `xcodebuild` — the runner's cold startup is paid once.
    popen_argvs, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    env.start(eff, Preconditions())  # cold: spawn the runner
    assert env.has_reusable_resident()
    env.start(eff, Preconditions())  # warm: reuse it
    assert len(popen_argvs) == 1  # the runner was spawned once and reused (BE-0291)


# --- pinning the Simulator's own system language (BE-0320) --- #
#
# SpringBoard owns the permission prompts `handleSystemAlert` taps by label, and no app launch
# argument reaches it — so a cold spawn writes the device's global preference domain and reboots,
# and a warm reuse is gated on the resolved locale still matching. Same fake points as above.


def _sim_eff_locale(*, test_runner: str, locale: str) -> Effective:
    cfg = (
        f"defaults:\n  locale: {locale}\n"
        f"targets:\n  s:\n    bundleId: com.x\n    xcuitest:\n      testRunner: {test_runner}\n"
    )
    return resolve(load_config(cfg), "s")


def _verbs(simctl_calls: list[list[str]]) -> list[str]:
    return [c[2] for c in simctl_calls if c[:2] == ["xcrun", "simctl"]]


def test_cold_spawn_pins_the_system_locale_and_reboots_for_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A device on another language is written and rebooted: `simctl spawn` needs a booted device, and
    # a running SpringBoard does not pick a global-domain write up live, so the value only reaches
    # the alert text on the *next* boot. The write must land before the app is installed and launched.
    domain: dict[str, str | None] = {"v": "en_US"}
    _, simctl_calls, run = _fake_toolchain(monkeypatch, system_locale=domain)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(
        _sim_eff_locale(test_runner=str(_write_runner(tmp_path)), locale="ja_JP"), Preconditions()
    )

    assert domain["v"] == "ja_JP"  # the device now carries the configured locale
    assert simctl.system_locale_cmds("UDID", "ja_JP")[0] in simctl_calls
    # boot (the initial one) -> bootstatus (waiting it out, BE-0359) -> list (recording the device
    # type a replacement would be cloned from) -> spawn (the read, then the two writes) -> shutdown
    # -> list (confirming the device went down) -> boot (the one that re-renders SpringBoard) ->
    # bootstatus (waiting that one out too) -> spawn (the read-back that verifies it took) -> privacy
    # (the default `reinstall: clean` resets permissions on every prepare, not just a warm resume).
    assert _verbs(simctl_calls) == [
        "boot",
        "bootstatus",
        "list",
        "spawn",
        "spawn",
        "spawn",
        "shutdown",
        "list",
        "boot",
        "bootstatus",
        "spawn",
        "privacy",
    ]


def test_cold_spawn_skips_the_extra_boot_when_the_device_already_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The common case — a Simulator already on the configured locale, or one an earlier spawn pinned.
    # Reading the domain is enough to know, so a cold spawn pays one read instead of a second boot.
    _, simctl_calls, run = _fake_toolchain(monkeypatch, system_locale={"v": "en_US"})
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(
        _sim_eff_locale(test_runner=str(_write_runner(tmp_path)), locale="en_US"), Preconditions()
    )

    assert _verbs(simctl_calls).count("boot") == 1  # no second boot cycle
    assert not any("write" in c for c in simctl_calls)


def test_a_locale_change_forces_a_cold_respawn_rather_than_a_stale_warm_reuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Only a cold spawn re-pins and reboots, so reusing the warm runner for a scenario under another
    # locale would run it against the previous scenario's SpringBoard language — the very accident
    # this item removes. The mismatch is a cache miss, exactly as `erase` and a wedged runner are.
    domain: dict[str, str | None] = {"v": "en_US"}
    popen_argvs, simctl_calls, run = _fake_toolchain(monkeypatch, system_locale=domain)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff_locale(test_runner=str(_write_runner(tmp_path)), locale="en_US")

    env.start(eff, Preconditions())
    env.start(eff, Preconditions(locale="ja_JP"))  # the per-scenario override wins over the config

    assert len(popen_argvs) == 2  # the locale change forced a fresh runner
    assert domain["v"] == "ja_JP"
    assert simctl.system_locale_cmds("UDID", "ja_JP")[1] in simctl_calls


def test_the_same_locale_still_reuses_the_warm_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The gate keys on the *resolved* locale, so a scenario whose override merely restates the
    # target's own `locale` is not a mismatch — it still resumes warm (BE-0291 amortization intact).
    popen_argvs, _, run = _fake_toolchain(monkeypatch, system_locale={"v": "en_US"})
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff_locale(test_runner=str(_write_runner(tmp_path)), locale="en_US")

    env.start(eff, Preconditions())
    env.start(eff, Preconditions(locale="en_US"))

    assert len(popen_argvs) == 1


def test_a_pin_that_does_not_take_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A device that reads back another locale after the write and reboot would run the scenario
    # against an alert language nothing predicts — fail rather than proceed (determinism first).
    def run(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        return _globals_plist("en_US") if "export" in argv else ""

    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff_locale(test_runner=str(_write_runner(tmp_path)), locale="ja_JP")
    with pytest.raises(simctl.DeviceError, match="BE-0320"):
        env.start(eff, Preconditions())


def test_an_erasing_cold_spawn_re_pins_the_wiped_domain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `erase` wipes the device's preferences, so the pin has to run *after* it — moving the pin above
    # the erase would leave the second scenario on whatever language the wiped device boots with.
    domain: dict[str, str | None] = {"v": "ja_JP"}
    _, simctl_calls, run = _fake_toolchain(monkeypatch, system_locale=domain)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff_locale(test_runner=str(_write_runner(tmp_path)), locale="ja_JP")

    env.start(eff, Preconditions(erase=True))

    assert domain["v"] == "ja_JP"  # re-pinned after the wipe, not left to the boot default
    verbs = _verbs(simctl_calls)
    assert verbs.index("erase") < verbs.index("spawn")


def test_an_unconfirmable_pin_runs_on_but_is_not_remembered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A device whose global domain cannot be read back after the reboot is not an observed mismatch,
    # so the run proceeds — but the pin is unconfirmed, so it must not be recorded: warm reuse is
    # gated on it, and remembering it would carry the doubt across every later lease.
    popen_argvs, _, run = _fake_toolchain(monkeypatch, export_fails=True)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff_locale(test_runner=str(_write_runner(tmp_path)), locale="ja_JP")

    env.start(eff, Preconditions())  # no raise: nothing was observed to be wrong
    assert env._pinned_locale is None
    env.start(eff, Preconditions())
    assert (
        len(popen_argvs) == 2
    )  # the unconfirmed pin blocked warm reuse, so the next lease is cold


# --- waiting for the boot to finish before using the device (BE-0359) --- #
#
# `simctl boot` returns once the boot has been *requested*; `bootstatus -b` is what waits for it to
# finish. These pin the ordering, which is the whole behaviour: a wait that runs after the install
# would be no wait at all.


def test_the_cold_prep_waits_for_the_boot_before_it_uses_the_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Installing the app or starting `xcodebuild` against a SpringBoard that is still coming up is
    # what produces the `Timed out attempting to launch app` signature the recovery ladder repairs
    # after the fact — so the wait lands immediately after the boot, ahead of everything that uses
    # the device (the device-type listing, the locale pin, the install).
    _, simctl_calls, run = _fake_toolchain(monkeypatch, system_locale={"v": "en_US"})
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(
        _sim_eff_locale(test_runner=str(_write_runner(tmp_path)), locale="en_US"), Preconditions()
    )

    verbs = _verbs(simctl_calls)
    assert verbs[:2] == ["boot", "bootstatus"]


def test_an_erasing_cold_prep_waits_for_the_boot_that_follows_the_erase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The widest window: an erased device boots from a genuine first-boot state, and BE-0353 puts
    # exactly that path on every crash-triggered retry.
    _, simctl_calls, run = _fake_toolchain(monkeypatch, system_locale={"v": "en_US"})
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(
        _sim_eff_locale(test_runner=str(_write_runner(tmp_path)), locale="en_US"),
        Preconditions(erase=True),
    )

    verbs = _verbs(simctl_calls)
    assert verbs[:4] == ["shutdown", "erase", "boot", "bootstatus"]


def test_a_boot_that_never_completes_fails_the_run_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The wait is the only step that can report a device which never finishes booting, so a
    # `bootstatus` that fails must surface as a device fault rather than a raw subprocess error.
    def run(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        if argv[2:3] == ["bootstatus"]:
            raise subprocess.CalledProcessError(1, argv, stderr="Unable to boot device")
        return ""

    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    with pytest.raises(simctl.DeviceError, match="Unable to boot device"):
        env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())


def test_the_locale_pin_waits_for_its_reboot_before_reading_the_value_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The pin's own reboot is the second unwaited boot: without the wait the read-back — and the
    # caller's install after it — run against a device that is still starting.
    domain: dict[str, str | None] = {"v": "en_US"}
    _, simctl_calls, run = _fake_toolchain(monkeypatch, system_locale=domain)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    with caplog.at_level("INFO"):
        env.start(
            _sim_eff_locale(test_runner=str(_write_runner(tmp_path)), locale="ja_JP"),
            Preconditions(),
        )

    verbs = _verbs(simctl_calls)
    # The pin's reboot is the second `boot`; the read-back is the `spawn` that follows it.
    reboot = verbs.index("boot", verbs.index("shutdown"))
    assert verbs[reboot : reboot + 3] == ["boot", "bootstatus", "spawn"]
    assert env._pinned_locale == "ja_JP"
    # The one line that says the pin fired at all. Nothing else in a CI log distinguishes a job that
    # reaches this reboot from one whose device already carried the locale.
    assert "pinning Simulator UDID's system locale to 'ja_JP'" in caplog.text


def test_a_pin_whose_shutdown_was_refused_is_not_remembered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `Env.shutdown()` suppresses its own failure and `bootstatus -b` returns at once on a device
    # that never left `Booted`, so a wedged CoreSimulator would otherwise hand back a "ready" device
    # and a confirmed pin while SpringBoard still renders the old language. The plist cannot tell:
    # it reads the value the write changed, not the one SpringBoard loaded. Only the booted state can.
    popen_argvs, _, run = _fake_toolchain(
        monkeypatch, system_locale={"v": "en_US"}, stays_booted_after_shutdown=True
    )
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff_locale(test_runner=str(_write_runner(tmp_path)), locale="ja_JP")

    env.start(eff, Preconditions())  # no raise: nothing was observed to be wrong
    assert env._pinned_locale is None
    env.start(eff, Preconditions())
    assert (
        len(popen_argvs) == 2
    )  # the unconfirmed pin blocked warm reuse, so the next lease is cold


def test_start_respawns_the_runner_when_the_scenario_erases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `erase` shuts the Simulator down (killing the runner), so it cannot reuse a warm one — the
    # scenario respawns cold. The reset still runs before the app launches, keeping isolation (Unit 2).
    popen_argvs, simctl_calls, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    env.start(eff, Preconditions())
    env.start(eff, Preconditions(erase=True))
    assert len(popen_argvs) == 2  # the erase forced a fresh runner
    assert any(c[:3] == ["xcrun", "simctl", "erase"] for c in simctl_calls)  # the device was erased


def test_start_respawns_a_wedged_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # BE-0291 Unit 4: a warm runner that fails its bounded /health probe (the known crash after
    # repeated app.launch() cycles) is a cache miss — the next lease respawns cold rather than losing
    # the run. One scenario's fault costs one extra cold start.
    wedged = {"v": False}
    popen_argvs, _, run = _fake_toolchain(monkeypatch, wedged=wedged)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    env.start(eff, Preconditions())
    wedged["v"] = True  # the runner wedges after the first lease
    env.start(eff, Preconditions())
    assert len(popen_argvs) == 2  # the wedged runner was discarded and a fresh one spawned


def test_end_lease_keeps_the_runner_but_teardown_terminates_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `end_lease` releases a lease while keeping the warm runner (only the app is terminated); the
    # pool's later `teardown` is what actually kills it (BE-0291 ownership on the pool).
    _, simctl_calls, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    driver = env.start(eff, Preconditions())
    proc = env._runner_proc

    simctl_calls.clear()
    env.end_lease(driver, eff)
    assert env._runner_proc is proc and isinstance(proc, _FakeProc) and proc.alive  # untouched
    assert env.has_reusable_resident()
    assert any(
        c[:3] == ["xcrun", "simctl", "terminate"] for c in simctl_calls
    )  # only the app ended

    env.teardown(driver, eff)
    assert env._runner_proc is None and not proc.alive  # teardown kills the runner
    assert not env.has_reusable_resident()


def test_start_respawns_cold_before_the_app_launch_cycle_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # BE-0287: the resident runner crashes after a handful of app.launch() cycles
    # (docs/architecture.md). The BE-0291 warm probe only detects an *already*-crashed runner, so the
    # crash still lands mid-scenario. Bounding the reuse count respawns the runner cold *before* the
    # crash threshold: after `_MAX_WARM_REUSES` warm reuses (the runner stays healthy throughout), the
    # next lease spawns a fresh runner rather than tipping the live one over.
    popen_argvs, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    for _ in range(_MAX_WARM_REUSES + 1):  # cold spawn, then `_MAX_WARM_REUSES` warm reuses
        env.start(eff, Preconditions())
    assert len(popen_argvs) == 1  # one runner across the whole reuse budget (all warm, all healthy)
    env.start(eff, Preconditions())  # budget spent → proactive cold respawn, not another reuse
    assert len(popen_argvs) == 2
    assert env._warm_reuses == 0  # the fresh runner's cycle count started over


def test_max_warm_reuses_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A lane can retune the reuse budget without a code change; 0 disables warm reuse entirely (every
    # lease is cold), the safe fallback if a runner proves to crash sooner than the default tolerates.
    monkeypatch.setenv(_MAX_WARM_REUSES_ENV, "0")
    popen_argvs, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    env.start(eff, Preconditions())
    env.start(eff, Preconditions())
    assert len(popen_argvs) == 2  # no warm reuse: each lease spawns cold


def test_runner_startup_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # A contended CI host can extend the cold-start ceiling without a code change (the ios-e2e
    # workflow raises it to 300s); a blank or malformed value falls back to the compiled default.
    monkeypatch.delenv(_RUNNER_STARTUP_TIMEOUT_ENV, raising=False)
    assert _runner_startup_timeout() == _RUNNER_STARTUP_TIMEOUT
    monkeypatch.setenv(_RUNNER_STARTUP_TIMEOUT_ENV, "300")
    assert _runner_startup_timeout() == 300.0
    monkeypatch.setenv(_RUNNER_STARTUP_TIMEOUT_ENV, "not-a-number")
    assert _runner_startup_timeout() == _RUNNER_STARTUP_TIMEOUT


def test_respawn_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # The respawn readiness ceiling only tightens a respawn's wait, so unset / non-positive / malformed
    # all read as None (fall back to the cold ceiling) — never as zero, which would remove the wait.
    monkeypatch.delenv(_RESPAWN_TIMEOUT_ENV, raising=False)
    assert _respawn_timeout() is None  # unset -> use the cold ceiling for respawns too
    monkeypatch.setenv(_RESPAWN_TIMEOUT_ENV, "90")
    assert _respawn_timeout() == 90.0
    monkeypatch.setenv(_RESPAWN_TIMEOUT_ENV, "0")
    assert _respawn_timeout() is None  # non-positive -> cold ceiling, not "no readiness wait"
    monkeypatch.setenv(_RESPAWN_TIMEOUT_ENV, "not-a-number")
    assert _respawn_timeout() is None


def test_respawn_uses_the_tighter_readiness_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A first bring-up pays the full cold-start ceiling; a respawn (the Simulator already booted, the
    # app installed) pays the tighter respawn ceiling, so a dead runner surfaces fast instead of
    # hanging out the whole cold budget. Both go through `_spawn_cold_with_retry`; only the timeout
    # differs, which is what a mid-run respawn's readiness wait is bounded by.
    monkeypatch.setenv(_RUNNER_STARTUP_TIMEOUT_ENV, "300")
    monkeypatch.setenv(_RESPAWN_TIMEOUT_ENV, "90")
    _, _, run = _fake_toolchain(monkeypatch)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))

    seen: list[float] = []
    original = _spawn_cold_with_retry

    def spy(*args: Any, **kwargs: Any) -> _Spawned:
        seen.append(kwargs["timeout"])
        return original(*args, **kwargs)

    # Patch the module-level name `_spawn_cold` resolves against — its own module's globals since
    # BE-0411 split the package, not the package's re-export.
    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.environments.xcuitest.xcuitest_environment._spawn_cold_with_retry",
        spy,
    )

    XcuitestEnvironment("xcuitest", "UDID", env_run=run, respawn=False).start(eff, Preconditions())
    XcuitestEnvironment("xcuitest", "UDID", env_run=run, respawn=True).start(eff, Preconditions())
    assert seen == [300.0, 90.0]  # first bring-up: cold ceiling; respawn: the tighter one


def test_in_place_respawn_uses_the_tighter_readiness_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The common mid-run-crash path keeps the SAME environment: the dead resident stays warm-cached, so
    # the retry reuses this instance and `start` respawns cold *in place*. That in-place respawn must
    # also take the tighter ceiling — the env self-detects it via `_cold_spawned_before`, because the
    # pool's `respawn` flag on a reused instance was fixed to False at first bring-up (see the PR
    # review: without this, the most common recovery path silently paid the full 300s cold ceiling).
    monkeypatch.setenv(_RUNNER_STARTUP_TIMEOUT_ENV, "300")
    monkeypatch.setenv(_RESPAWN_TIMEOUT_ENV, "90")
    _, _, run = _fake_toolchain(monkeypatch)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))

    seen: list[float] = []
    original = _spawn_cold_with_retry

    def spy(*args: Any, **kwargs: Any) -> _Spawned:
        seen.append(kwargs["timeout"])
        return original(*args, **kwargs)

    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.environments.xcuitest.xcuitest_environment._spawn_cold_with_retry",
        spy,
    )

    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)  # respawn=False: a first bring-up
    env.start(eff, Preconditions())  # first cold spawn: full ceiling
    assert env._runner_proc is not None
    env._runner_proc.alive = False  # type: ignore[attr-defined]  # the runner crashed mid-run
    env.start(eff, Preconditions())  # same instance respawns cold in place: the tighter ceiling
    assert seen == [300.0, 90.0]


def test_erase_forced_cold_spawn_keeps_the_full_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `erase` shuts the Simulator down, so the cold spawn after it is a genuine first-boot start (reboot
    # + reinstall), not a respawn onto a live Simulator — it must keep the full cold ceiling even though
    # this instance has cold-spawned before (`_cold_spawned_before`). Guards the respawn ceiling from
    # wrongly tightening a real cold boot, which needs the whole budget.
    monkeypatch.setenv(_RUNNER_STARTUP_TIMEOUT_ENV, "300")
    monkeypatch.setenv(_RESPAWN_TIMEOUT_ENV, "90")
    _, _, run = _fake_toolchain(monkeypatch)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))

    seen: list[float] = []
    original = _spawn_cold_with_retry

    def spy(*args: Any, **kwargs: Any) -> _Spawned:
        seen.append(kwargs["timeout"])
        return original(*args, **kwargs)

    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.environments.xcuitest.xcuitest_environment._spawn_cold_with_retry",
        spy,
    )

    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(eff, Preconditions())  # first cold spawn: full ceiling
    env.start(eff, Preconditions(erase=True))  # erase reboots the Simulator: a genuine cold start
    assert seen == [300.0, 300.0]  # the erase spawn keeps the full ceiling, not the respawn one


def test_start_respawns_a_dead_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # BE-0291 Unit 4: a runner whose process has exited (crashed) between leases is discarded before
    # any /health probe (the `poll()` fast path) — the next lease respawns cold.
    popen_argvs, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    env.start(eff, Preconditions())
    assert env._runner_proc is not None
    env._runner_proc.alive = False  # type: ignore[attr-defined]  # the runner process exited
    env.start(eff, Preconditions())
    assert len(popen_argvs) == 2  # the dead runner was discarded and a fresh one spawned


def test_discarding_a_crashed_runner_warns_and_sweeps_its_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The diagnostic seam: a runner that exited on its own (the known app.launch()-cycle crash) is
    # discarded without a terminate() — the leader's pid is already reaped — and logs a mid-run-crash
    # warning so a run that died on a `Connection refused` shows *why* the channel went away. It is
    # still signalled at the group level, though: an XCTest-host child can outlive the leader and keep
    # holding the device's automation session, so the crashed branch sweeps the group by pid-as-pgid.
    _, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    proc = env._runner_proc
    assert proc is not None
    proc.alive = False  # type: ignore[attr-defined]  # the runner crashed mid-run
    log = env._runner_log
    assert log is not None and log.exists()
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr("os.killpg", lambda pgid, sig: signalled.append((pgid, sig)))
    with caplog.at_level("WARNING"):
        env._discard_runner()
    assert not proc.terminated  # type: ignore[attr-defined]  # terminate() is never called: the
    # leader's pid is already reaped, so there is nothing to signal directly
    assert signalled == [(proc.pid, signal.SIGKILL)]  # the group is swept instead
    assert "exited on its own" in caplog.text
    # A review finding this PR caught: the warning tells the operator to "see <path>" — that file
    # must still exist, or the pointer resolves to nothing. A mid-run crash keeps its ephemeral
    # capture (parity with a failed cold-spawn attempt), never prunes it.
    assert f"see {log}" in caplog.text
    assert log.exists()


def test_a_discarded_cold_attempt_terminates_the_app_under_test(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The other half of BE-XXXX's motivation: an app left mid-launch by the timeout that failed one
    # attempt is exactly what the next attempt would call `launch()` on again, so a discard on the
    # retry path — no teardown in the picture — must terminate the app under test itself, not merely
    # the runner process.
    _, simctl_calls, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    simctl_calls.clear()
    env._discard_runner(warn_on_crash=False, keep_log=True)
    assert ["xcrun", "simctl", "terminate", "UDID", "com.x"] in simctl_calls


def test_a_discard_terminates_the_xctrunner_app_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The runner app runs inside the Simulator under launchd_sim, so it is in no host process group
    # and the discard's `killpg` sweep cannot touch it — it outlives `xcodebuild` still holding the
    # automation session, and the ~5 cold spawns an ios-e2e job makes can stack up that many of them.
    # Its id comes off the .xctestrun, so an explicit testRunner is terminated like the bundled one.
    _, simctl_calls, run = _fake_toolchain(monkeypatch)
    runner = _write_runner(tmp_path, host_bundle_id="com.bajutsu.common.runner.uitests.xctrunner")
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(runner)), Preconditions())
    simctl_calls.clear()
    env._discard_runner(warn_on_crash=False, keep_log=True)
    assert [
        "xcrun",
        "simctl",
        "terminate",
        "UDID",
        "com.bajutsu.common.runner.uitests.xctrunner",
    ] in simctl_calls
    assert [
        "xcrun",
        "simctl",
        "terminate",
        "UDID",
        "com.x",
    ] in simctl_calls  # and the app, as before


def test_a_hung_terminate_is_not_absorbed_by_a_discard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The discard absorbs a failing `terminate` because the common case is an app that is not
    # running. A `terminate` that never returns is the opposite case — a wedged CoreSimulator — and
    # absorbing it here would leave the discard path exactly as silent as it was before BE-0363.
    _, _, run = _fake_toolchain(monkeypatch)
    runner = _write_runner(tmp_path, host_bundle_id="com.bajutsu.common.runner.uitests.xctrunner")
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(runner)), Preconditions())

    def hung(argv: list[str], extra_env: Mapping[str, str] | None = None) -> str:
        if argv[2:3] == ["terminate"]:
            raise simctl.DeviceTimeout(f"device operation timed out after 60s: {' '.join(argv)}")
        return run(argv, extra_env)

    env._run = hung
    patched = env._patched_runner
    assert patched is not None and patched.exists()
    log = env._runner_log
    assert log is not None and log.exists()
    with pytest.raises(simctl.DeviceTimeout):
        env._discard_runner(warn_on_crash=False, keep_log=False)
    # Surfacing the wedge must not cost the discard's own bookkeeping: a leaked patched .xctestrun
    # and an orphaned capture per discard are what skipping it would buy.
    assert not patched.exists()
    assert not log.exists()
    assert env._runner_log is None
    assert env.has_reusable_resident() is False
    # The runner app's own terminate is narrowed the same way, which the raise above hides — the
    # app under test is terminated first, so it never reaches this one.
    with pytest.raises(simctl.DeviceTimeout):
        env._terminate_runner_app()


def test_a_failing_terminate_is_still_absorbed_by_a_discard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The other side of the narrowing above: the failures those suppressions were written for stay
    # absorbed, so a discard whose `terminate` fails is still a no-op rather than a fault. A
    # `CalledProcessError` never reaches that handler — `simctl.Env.terminate` suppresses it a layer
    # below — so what it actually catches is a host-level `OSError` (a fork that fails, an `xcrun`
    # that has gone) and a plain `simctl.DeviceError`.
    _, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())

    def _failing(exc: Exception) -> simctl.RunFn:
        def fake(argv: list[str], extra_env: Mapping[str, str] | None = None) -> str:
            if argv[2:3] == ["terminate"]:
                raise exc
            return run(argv, extra_env)

        return fake

    env._run = _failing(OSError("fork failed"))
    env._discard_runner(warn_on_crash=False, keep_log=True)

    env._run = _failing(simctl.DeviceError("device operation failed (exit 1)"))
    env._discard_runner(warn_on_crash=False, keep_log=True)


def test_end_lease_leaves_the_xctrunner_app_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The warm resident *is* that runner app (BE-0291): terminating it per lease would kill the
    # reuse this environment exists to provide, turning every lease back into a cold spawn. Only a
    # discard — which has already given the runner up — reaches it.
    _, simctl_calls, run = _fake_toolchain(monkeypatch)
    runner = _write_runner(tmp_path, host_bundle_id="com.bajutsu.common.runner.uitests.xctrunner")
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(runner))
    driver = env.start(eff, Preconditions())
    simctl_calls.clear()
    env.end_lease(driver, eff)
    terminated = [c[4] for c in simctl_calls if c[:3] == ["xcrun", "simctl", "terminate"]]
    assert terminated == ["com.x"]  # the app under test only — the runner app stays up


def test_a_discard_survives_an_xctestrun_without_a_host_bundle_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A .xctestrun that names no runner app (a stripped or hand-written one) must not cost the
    # discard its other work: nothing is terminated for the runner, and the app under test still is.
    _, simctl_calls, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    simctl_calls.clear()
    env._discard_runner(warn_on_crash=False, keep_log=True)
    terminated = [c[4] for c in simctl_calls if c[:3] == ["xcrun", "simctl", "terminate"]]
    assert terminated == ["com.x"]


def test_a_real_device_discard_never_terminates_through_simctl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A real device is not managed through simctl at all (BE-0238), so the runner-app terminate is
    # scoped to the Simulator exactly like the app-under-test one: a discard there stays silent.
    simctl_calls: list[list[str]] = []

    def _run(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        simctl_calls.append(argv)
        return ""

    class _FakeDriver:
        def await_ready(self, timeout: float) -> None: ...
        def health_ready(self) -> bool:
            return True

    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: _FakeProc())
    monkeypatch.setattr(backends, "make_driver", lambda *_a, **_k: _FakeDriver())
    _patch_group_signals(monkeypatch)
    runner = _write_runner(tmp_path, host_bundle_id="com.bajutsu.common.runner.uitests.xctrunner")
    env = XcuitestEnvironment("xcuitest", _DEVICE_UDID, env_run=_run)
    env.start(_device_eff(test_runner=str(runner)), Preconditions())
    env._discard_runner(warn_on_crash=False, keep_log=True)
    assert simctl_calls == []


def test_runner_host_bundle_ids_reads_every_target_once(tmp_path: Path) -> None:
    # Several test targets in one file can share a runner app, and the metadata key is not a target:
    # the ids are deduplicated and the metadata skipped, so a discard issues one terminate per app.
    runner = tmp_path / "Multi.xctestrun"
    with runner.open("wb") as f:
        plistlib.dump(
            {
                "__xctestrun_metadata__": {"FormatVersion": 1},
                "A": {"TestHostBundleIdentifier": "com.a.xctrunner"},
                "B": {"TestHostBundleIdentifier": "com.b.xctrunner"},
                "C": {"TestHostBundleIdentifier": "com.a.xctrunner"},
                "D": {"TestingEnvironmentVariables": {}},  # a target naming no runner app
            },
            f,
        )
    assert _runner_host_bundle_ids(runner) == ("com.a.xctrunner", "com.b.xctrunner")


def test_runner_host_bundle_ids_is_quiet_on_an_unreadable_plist(tmp_path: Path) -> None:
    # This feeds a discard, which must never fail; a missing or malformed file yields no ids.
    assert _runner_host_bundle_ids(tmp_path / "absent.xctestrun") == ()
    junk = tmp_path / "junk.xctestrun"
    junk.write_bytes(b"not a plist")
    assert _runner_host_bundle_ids(junk) == ()


def test_runner_output_is_captured_when_the_env_var_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # BAJUTSU_XCUITEST_RUNNER_LOG overrides the capture directory (capture is on by default since
    # BE-0319); the crash warning then points at the file under that directory, and — unlike a
    # default capture — it is kept, not pruned (see the ephemeral / kept teardown tests below).
    _, _, run = _fake_toolchain(monkeypatch)
    log_dir = tmp_path / "runner-logs"
    monkeypatch.setenv("BAJUTSU_XCUITEST_RUNNER_LOG", str(log_dir))
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert env._runner_log is not None and env._runner_log.parent == log_dir
    assert env._runner_log.exists()  # the sink file was opened for the spawn
    assert (
        "see" in env._runner_log_hint()
    )  # the hint points at the captured log, not at the env var


def test_the_spawn_hands_the_channel_a_stall_capture_bound_to_this_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # BE-0361 unit 2: the udid the capture screenshots deliberately never reaches the channel, so the
    # environment injects a callback closed over it — the same shape `runner_alive` uses. Without this
    # wiring the runner-crash trigger captures nothing, and every other test would still pass.
    _, _, run = _fake_toolchain(monkeypatch)
    captured: list[tuple[str, str | None]] = []
    # `simulator_probes` hands back its udid, so the recorded pair names the device the probes would
    # have screenshotted as well as the trigger that fired.
    monkeypatch.setattr(stall_diagnostics, "simulator_probes", lambda udid=None: udid)
    monkeypatch.setattr(
        stall_diagnostics, "capture", lambda reason, probes: captured.append((reason, probes))
    )
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())

    hook = DRIVER_KWARGS[-1]["on_stall"]
    assert callable(hook)
    hook()
    # The channel passes nothing: naming the trigger — and so the directory the capture writes — stays
    # on this side, where the udid also lives.
    assert captured == [("runner-crash", "UDID")]


def test_result_bundle_is_requested_only_when_the_env_var_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # BE-0361 unit 1: the spawn argv gains `-resultBundlePath` exactly when
    # BAJUTSU_XCUITEST_RESULT_BUNDLES names a directory. Unset is the shipped default for every lane
    # but the iOS one, so the argv must be byte-for-byte what it was before this feature existed.
    monkeypatch.delenv(_RESULT_BUNDLE_ENV, raising=False)
    popen_argvs, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert "-resultBundlePath" not in popen_argvs[0]

    bundles = tmp_path / "bundles"
    monkeypatch.setenv(_RESULT_BUNDLE_ENV, str(bundles))
    popen_argvs, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    argv = popen_argvs[0]
    assert "-resultBundlePath" in argv
    bundle = Path(argv[argv.index("-resultBundlePath") + 1])
    # Keyed like the runner log — udid plus this spawn's fresh ephemeral port — so a respawn never
    # overwrites the crashed predecessor's bundle.
    assert bundle.parent == bundles
    assert bundle.name == f"result-UDID-{env._runner_port}.xcresult"


def test_a_requested_bundle_declines_xcodebuilds_own_sysdiagnose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Measured on CI: with a bundle path and nothing else, `xcodebuild` honoured its
    # `-collect-test-diagnostics on-failure` default and embedded a Simulator `system.logarchive`,
    # taking one spawn's bundle to 163 MB. BE-0361 rejected whole-archive collection in favour of
    # targeted extracts, so the two flags travel together or the rejected alternative comes back in
    # through the feature that was supposed to be cheap.
    monkeypatch.setenv(_RESULT_BUNDLE_ENV, str(tmp_path / "bundles"))
    popen_argvs, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    argv = popen_argvs[0]
    assert argv[argv.index("-collect-test-diagnostics") + 1] == "never"

    # And it is scoped to the bundle: with no bundle requested there is no argv change at all.
    monkeypatch.delenv(_RESULT_BUNDLE_ENV, raising=False)
    popen_argvs, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert "-collect-test-diagnostics" not in popen_argvs[0]


def test_result_bundle_path_is_cleared_before_the_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `xcodebuild` refuses to start at all when the result bundle path already exists, so a recycled
    # ephemeral port would turn this diagnostics feature into a spawn failure. A leftover at the
    # exact key is cleared first; nothing else in the directory is touched.
    bundles = tmp_path / "bundles"
    monkeypatch.setenv(_RESULT_BUNDLE_ENV, str(bundles))
    _, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    monkeypatch.setattr(xcuitest_env_impl, "_allocate_port", lambda: 4242)
    stale = bundles / "result-UDID-4242.xcresult"
    stale.mkdir(parents=True)
    (stale / "Info.plist").write_bytes(b"leftover")
    keep = bundles / "result-UDID-9999.xcresult"
    keep.mkdir(parents=True)

    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert not stale.exists()
    assert keep.exists()  # another spawn's bundle is never collateral


def test_a_leftover_that_survives_the_clearing_degrades_to_no_bundles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `rmtree(ignore_errors=True)` returns cleanly when the removal did *not* happen — a plain file at
    # this key is the reachable case. Handing `xcodebuild` the argv anyway would make it refuse to
    # start, which is the very spawn failure the clearing exists to prevent.
    bundles = tmp_path / "bundles"
    monkeypatch.setenv(_RESULT_BUNDLE_ENV, str(bundles))
    popen_argvs, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    monkeypatch.setattr(xcuitest_env_impl, "_allocate_port", lambda: 4242)
    bundles.mkdir(parents=True)
    (bundles / "result-UDID-4242.xcresult").write_text("not a directory")

    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert "-resultBundlePath" not in popen_argvs[0]


def test_the_result_bundle_path_refuses_an_unsafe_device_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A *new* boundary where the udid composes a path — and that path feeds a recursive delete, so it
    # gets the same shared device-id policy the `-destination` argv applies. The real-device path never
    # runs the simctl prep that would otherwise have vetted the id.
    monkeypatch.setenv(_RESULT_BUNDLE_ENV, str(tmp_path / "bundles"))
    _, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "../../escape", env_run=run)
    with pytest.raises(simctl.DeviceError):
        env._result_bundle_path()
    assert not (tmp_path / "bundles").exists()  # nothing was created on the way to refusing


def test_an_unusable_result_bundle_directory_never_fails_the_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An opt-in diagnostic must not be what reddens a lane: an operator typo naming a file, or a
    # read-only mount, degrades to "no bundles" — the argv falls back to exactly what it is unset.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    monkeypatch.setenv(_RESULT_BUNDLE_ENV, str(blocker))
    popen_argvs, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert "-resultBundlePath" not in popen_argvs[0]


def test_warm_resume_reapplies_the_per_scenario_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # BE-0291 Unit 2: a reused runner still gets the full per-scenario reset before the app relaunch —
    # reinstall, permission grants, terminate + relaunch, and the deeplink — so reuse never weakens
    # the isolation a cold lease gives. Asserting spawn count alone would miss a skipped reset.
    popen_argvs, simctl_calls, run = _fake_toolchain(monkeypatch)
    app = tmp_path / "App.app"
    app.mkdir()
    cfg = (
        f"targets:\n  s:\n    bundleId: com.x\n    appPath: {app}\n"
        f"    xcuitest:\n      testRunner: {_write_runner(tmp_path)}\n"
    )
    eff = resolve(load_config(cfg), "s")
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(eff, Preconditions())  # cold spawn
    simctl_calls.clear()
    env.start(
        eff, Preconditions(deeplink="myapp://open"), permissions={"camera": "grant"}
    )  # warm resume
    assert len(popen_argvs) == 1  # no respawn — the runner was reused
    verbs = [c[2] for c in simctl_calls if len(c) >= 3 and c[:2] == ["xcrun", "simctl"]]
    # the per-scenario reset ran on the warm path, before the app relaunch:
    assert "install" in verbs  # app reinstalled (reinstall=clean → uninstall + install)
    assert "privacy" in verbs  # the camera permission was granted via `simctl privacy`
    assert "terminate" in verbs and "launch" in verbs  # the app was restarted
    assert "openurl" in verbs  # the deeplink was opened


def test_clean_reinstall_resets_permissions_even_when_the_scenario_names_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `simctl install`/`uninstall` never touch TCC.db (verified on-device) — only `erase` does — so
    # without an explicit reset, a `clean` reinstall on a warm runner would let a permission an
    # earlier scenario granted or revoked for this bundle id leak into a scenario that names none of
    # its own, contradicting this method's "same known state a cold lease does" promise (BE-0291
    # Unit 2). `permissions=None` here is the point: no `grant`/`revoke` call should be the reason a
    # `privacy` call appears.
    popen_argvs, simctl_calls, run = _fake_toolchain(monkeypatch)
    app = tmp_path / "App.app"
    app.mkdir()
    cfg = (
        f"targets:\n  s:\n    bundleId: com.x\n    appPath: {app}\n"
        f"    xcuitest:\n      testRunner: {_write_runner(tmp_path)}\n"
    )
    eff = resolve(load_config(cfg), "s")
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(eff, Preconditions())  # cold spawn
    simctl_calls.clear()
    env.start(eff, Preconditions())  # warm resume, reinstall defaults to "clean", no permissions
    assert len(popen_argvs) == 1  # no respawn — the runner was reused
    reset_calls = [c for c in simctl_calls if c[2:3] == ["privacy"] and c[4:6] == ["reset", "all"]]
    assert reset_calls, f"expected a `simctl privacy reset all` call, got: {simctl_calls}"
    reset_call = reset_calls[0]
    assert reset_call[-1] == "com.x"  # scoped to this bundle id, not every app on the device
    verbs = _verbs(simctl_calls)
    # uninstall, then install, then reset — the reset targets the bundle this `clean` reinstall just
    # (re)installed, the same order `adb.Env`'s uninstall → install → `pm clear` mirror uses. All
    # three indices come from `verbs` (not a mix with `simctl_calls`): `permissions` is `None` here,
    # so the first (and only) `privacy` verb is unambiguously this reset, already pinned above to
    # `reset all … com.x` rather than a `grant`/`revoke`.
    assert verbs.index("uninstall") < verbs.index("install") < verbs.index("privacy")


def test_clean_reinstall_resets_permissions_with_no_app_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A provider that hands over an already-installed build (no `appPath` here, BE-0236) skips
    # install/uninstall entirely — but the bundle it names was installed by some earlier lease, and
    # `clean` still promises that lease a known permission state, so the reset must not be nested
    # inside the `appPath` branch the way the install/uninstall calls are.
    popen_argvs, simctl_calls, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))  # app_path=None
    env.start(eff, Preconditions())  # cold spawn
    simctl_calls.clear()
    env.start(eff, Preconditions())  # warm resume, reinstall defaults to "clean", no permissions
    assert len(popen_argvs) == 1  # no respawn — the runner was reused
    assert "install" not in _verbs(simctl_calls)  # nothing to (re)install without an appPath
    reset_calls = [c for c in simctl_calls if c[2:3] == ["privacy"] and c[4:6] == ["reset", "all"]]
    assert reset_calls, f"expected a reset even with no appPath, got: {simctl_calls}"
    assert reset_calls[0][-1] == "com.x"


def test_overwrite_reinstall_does_not_reset_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `overwrite` deliberately keeps the app's data container across scenarios (it exists precisely
    # to preserve state, predating BE-0407) — permission state is part of that same "keep what's
    # there" contract, so unlike `clean`, this mode must not reset TCC (a scenario relying on a
    # permission an earlier one granted, e.g. to test the already-granted path, would otherwise
    # regress).
    popen_argvs, simctl_calls, run = _fake_toolchain(monkeypatch)
    app = tmp_path / "App.app"
    app.mkdir()
    cfg = (
        f"targets:\n  s:\n    bundleId: com.x\n    appPath: {app}\n"
        f"    xcuitest:\n      testRunner: {_write_runner(tmp_path)}\n"
    )
    eff = resolve(load_config(cfg), "s")
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(eff, Preconditions(reinstall="overwrite"))  # cold spawn
    simctl_calls.clear()
    env.start(eff, Preconditions(reinstall="overwrite"))  # warm resume, still overwrite
    assert len(popen_argvs) == 1  # no respawn — the runner was reused (the case this guards)
    reset_calls = [c for c in simctl_calls if c[2:3] == ["privacy"] and c[4:6] == ["reset", "all"]]
    assert not reset_calls, f"overwrite must not reset permissions, got: {simctl_calls}"


def test_warm_resume_skips_reinstall_under_overwrite_when_the_bundle_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # BE-0407 Unit 14: `reinstall: overwrite` keeps the app's data container, so reinstalling a
    # byte-identical bundle buys nothing but the install's own cost. `reinstall: clean` (the
    # default, covered above) is unaffected — it always uninstalls first, which always requires a
    # fresh install after it regardless of the bundle's digest.
    popen_argvs, simctl_calls, run = _fake_toolchain(monkeypatch)
    app = tmp_path / "App.app"
    app.mkdir()
    (app / "App").write_bytes(b"binary")
    cfg = (
        f"targets:\n  s:\n    bundleId: com.x\n    appPath: {app}\n"
        f"    xcuitest:\n      testRunner: {_write_runner(tmp_path)}\n"
    )
    eff = resolve(load_config(cfg), "s")
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(eff, Preconditions(reinstall="overwrite"))  # cold spawn
    simctl_calls.clear()
    env.start(eff, Preconditions(reinstall="overwrite"))  # warm resume, same bundle
    assert len(popen_argvs) == 1  # no respawn — the runner was reused
    verbs = [c[2] for c in simctl_calls if len(c) >= 3 and c[:2] == ["xcrun", "simctl"]]
    assert "install" not in verbs  # the digest matched what this environment already put there
    assert "terminate" in verbs and "launch" in verbs  # the app still restarts every scenario


def test_warm_resume_reinstalls_under_overwrite_when_the_bundle_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    popen_argvs, simctl_calls, run = _fake_toolchain(monkeypatch)
    app = tmp_path / "App.app"
    app.mkdir()
    (app / "App").write_bytes(b"binary")
    cfg = (
        f"targets:\n  s:\n    bundleId: com.x\n    appPath: {app}\n"
        f"    xcuitest:\n      testRunner: {_write_runner(tmp_path)}\n"
    )
    eff = resolve(load_config(cfg), "s")
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(eff, Preconditions(reinstall="overwrite"))  # cold spawn
    (app / "Added.bundle").mkdir()
    (app / "Added.bundle" / "payload").write_bytes(b"x")  # a rebuilt bundle: the digest shifts
    simctl_calls.clear()
    env.start(eff, Preconditions(reinstall="overwrite"))  # warm resume, changed bundle
    assert len(popen_argvs) == 1  # no respawn — the runner was reused
    verbs = [c[2] for c in simctl_calls if len(c) >= 3 and c[:2] == ["xcrun", "simctl"]]
    assert "install" in verbs  # the rebuilt bundle was reinstalled


def test_runner_output_is_captured_by_default_and_is_ephemeral(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # BE-0319 unit 1: capture is on by default (env unset), so the first CI flake is diagnosable
    # without a human pre-arming BAJUTSU_XCUITEST_RUNNER_LOG. The default capture is ephemeral — a
    # healthy discard prunes it — while a run leaves nothing behind, matching "teardown prunes".
    _, _, run = _fake_toolchain(monkeypatch)
    monkeypatch.delenv("BAJUTSU_XCUITEST_RUNNER_LOG", raising=False)
    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.environments.xcuitest.xcuitest_environment._DEFAULT_RUNNER_LOG_DIR",
        tmp_path / "default-logs",
    )
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    driver = env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    log = env._runner_log
    assert log is not None and log.exists()  # captured even with the env var unset
    assert env._runner_log_ephemeral  # a default capture is marked for pruning
    env.teardown(driver, _sim_eff(test_runner=str(_write_runner(tmp_path))))
    assert not log.exists()  # teardown pruned the ephemeral default capture


def test_an_explicit_capture_directory_is_kept_on_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The env-var override is the operator asking to keep the capture: it is not ephemeral, so
    # teardown leaves it in place (unlike the default capture above).
    _, _, run = _fake_toolchain(monkeypatch)
    log_dir = tmp_path / "kept-logs"
    monkeypatch.setenv("BAJUTSU_XCUITEST_RUNNER_LOG", str(log_dir))
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    driver = env.start(eff, Preconditions())
    log = env._runner_log
    assert log is not None and log.parent == log_dir and not env._runner_log_ephemeral
    env.teardown(driver, eff)
    assert log.exists()  # an operator-chosen directory is kept, never pruned


def test_a_kept_default_capture_is_logged_at_the_moment_it_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A retry that then succeeds never raises, so `_spawn_cold_with_retry`'s folded diagnostics (only
    # built when *every* attempt fails) never mention the failed attempt's own capture. Without this
    # info line, that file becomes untracked the instant `_runner_log` moves on to the next attempt —
    # orphaned in `_DEFAULT_RUNNER_LOG_DIR` with nothing pointing at it.
    _, _, run = _fake_toolchain(monkeypatch)
    monkeypatch.delenv("BAJUTSU_XCUITEST_RUNNER_LOG", raising=False)
    monkeypatch.setattr(
        "bajutsu.common.platform_lifecycle.environments.xcuitest.xcuitest_environment._DEFAULT_RUNNER_LOG_DIR",
        tmp_path / "default-logs",
    )
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    log = env._runner_log
    assert log is not None and log.exists()
    with caplog.at_level("INFO"):
        env._discard_runner(warn_on_crash=False, keep_log=True)  # the failed-attempt discard path
    assert log.exists()  # kept, not pruned — same as a repeatable failure's evidence
    assert "kept a failed attempt's capture" in caplog.text
    assert str(log) in caplog.text  # discoverable: the path is named, not just "kept something"


def test_runner_log_hint_shows_the_bounded_tail_of_the_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # BE-0319 unit 2's actual tail extraction: the hint shows the last _RUNNER_LOG_TAIL_LINES of the
    # (high-volume) capture plus the file path — enough to show *why* the runner never answered
    # without dumping the whole log. The loud startup error folds this same hint in.
    _, _, run = _fake_toolchain(monkeypatch)
    monkeypatch.setenv("BAJUTSU_XCUITEST_RUNNER_LOG", str(tmp_path / "logs"))
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert env._runner_log is not None
    env._runner_log.write_text("".join(f"line-{i}\n" for i in range(50)))
    hint = env._runner_log_hint()
    assert f"see {env._runner_log}" in hint  # the path is named
    assert "line-49" in hint and "line-30" in hint  # the last 20 lines are shown
    assert "line-29" not in hint and "line-00" not in hint  # earlier lines are dropped


def test_a_repeatable_cold_spawn_failure_fails_loudly_and_keeps_the_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # End-to-end over the real _spawn_cold: a runner whose `xcodebuild` exits at once (health never
    # ready, process dead) fails fast on both attempts and raises loudly — no 120s dead-wait, no
    # misleading "mid-run crash" warning (its reason is in the error), and both attempts' captured
    # logs are kept on disk as evidence past the 20-line tail (BE-0319 units 1/3/4).
    module = "bajutsu.common.platform_lifecycle.environments.xcuitest"
    monkeypatch.setattr(f"{module}._functions._RUNNER_STARTUP_TIMEOUT", 0.05)
    monkeypatch.setattr(f"{module}.xcuitest_environment._DEFAULT_RUNNER_LOG_DIR", tmp_path / "logs")
    monkeypatch.delenv("BAJUTSU_XCUITEST_RUNNER_LOG", raising=False)

    class _DeadProc:
        pid = (
            12345  # a real Popen always has one; the discard's group sweep reads it even when dead
        )

        def poll(self) -> int:
            return 71  # the xcodebuild process exited immediately — never bound its port

        def terminate(self) -> None: ...
        def wait(self, timeout: float | None = None) -> int:
            return 71

        def kill(self) -> None: ...

    class _Driver:
        def health_ready(self) -> bool:
            return False  # /health never answers ready

        def await_ready(self, timeout: float = 10.0) -> None: ...

    def _run(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        # A device already on the configured locale, so the BE-0320 pin is a no-op here and this
        # test still exercises only the cold-spawn retry it is about.
        return _globals_plist("en_US") if "export" in argv else ""

    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: _DeadProc())
    monkeypatch.setattr(backends, "make_driver", lambda *_a, **_k: _Driver())
    _patch_group_signals(
        monkeypatch
    )  # the crashed branch sweeps by pgid; never signal a real group
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=_run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    with (
        caplog.at_level("WARNING"),
        pytest.raises(XcuitestChannelError, match="did not come up") as excinfo,
    ):
        env.start(eff, Preconditions())
    message = str(excinfo.value)
    assert "attempt 1/2" in message and "attempt 2/2" in message  # exactly two attempts
    assert "exited (code 71)" in message  # the fail-fast reason reached the loud error
    assert "mid-run crash" not in caplog.text  # the cold-spawn path never claims a mid-run crash
    # The capture is kept, not pruned, so the failure has on-disk evidence past the 20-line tail. (A
    # per-attempt file is port-keyed; both attempts fail-fast, so at least the last one survives.)
    assert list((tmp_path / "logs").glob("runner-*.log"))  # the ephemeral capture was kept


# --- BE-0319: the cold-spawn liveness wait + single retry, off-device via injection --- #
#
# The "await readiness with a liveness check and a bounded retry" seam (units 3-4) is factored so it
# runs without a Simulator: `_Spawned` is a bundle of callables, so a test drives the wait and the
# retry with fakes - the same isolation the channel tests use by injecting a fake transport.


def _fake_spawned(
    *,
    ready: Any,
    poll: Any = lambda: None,
    tail: str = "",
    discard: Any = lambda: None,
    run_ended: Any = lambda: None,
) -> _Spawned:
    return _Spawned(
        driver=_NamedDriver(),
        ready=ready,
        poll=poll,
        log_tail=lambda: tail,
        discard=discard,
        run_ended=run_ended,
    )


def test_await_cold_runner_returns_none_once_ready() -> None:
    spawned = _fake_spawned(ready=lambda: True)
    assert (
        _await_cold_runner(spawned, timeout=1.0, poll=0.0, sleep=lambda _s: None, clock=lambda: 0.0)
        is None
    )


def test_await_cold_runner_ready_wins_even_if_the_process_has_since_exited() -> None:
    # The probe order is load-bearing: a runner that answered /health is up regardless of its process
    # state, so `ready()` is checked before `poll()`. A health server that answered and then had its
    # `xcodebuild` wrapper exit is a success, not a spurious "the process exited" failure.
    spawned = _fake_spawned(ready=lambda: True, poll=lambda: 71)
    assert (
        _await_cold_runner(spawned, timeout=1.0, poll=0.0, sleep=lambda _s: None, clock=lambda: 0.0)
        is None
    )


def test_await_cold_runner_fails_fast_when_the_xcodebuild_process_exits() -> None:
    # BE-0319 unit 3: a dead xcodebuild aborts the wait at once with its exit code, rather than
    # probing a dead port for the remaining budget — the huge timeout here is never spent.
    spawned = _fake_spawned(ready=lambda: False, poll=lambda: 71)
    failure = _await_cold_runner(
        spawned, timeout=999.0, poll=0.0, sleep=lambda _s: None, clock=lambda: 0.0
    )
    assert failure is not None and "exited (code 71)" in failure.detail
    # The kind is what the recovery ladder keys on, so it must not depend on parsing the prose.
    assert failure.kind == "process-exit"


def test_run_ended_probe_reports_nothing_while_the_run_is_still_going(tmp_path: Path) -> None:
    log = tmp_path / "runner.log"
    log.write_bytes(b"Running tests...\n    t = 0.81s Launch com.example.app\n")
    assert _run_ended_probe(log)() is None


def test_run_ended_probe_detects_the_marker_appended_after_an_earlier_clean_probe(
    tmp_path: Path,
) -> None:
    # The real shape: the capture grows under the probe, so a marker written *after* a probe that saw
    # a healthy log must still be found by the next one.
    log = tmp_path / "runner.log"
    log.write_bytes(b"Running tests...\n")
    probe = _run_ended_probe(log)
    assert probe() is None
    with log.open("ab") as fh:
        fh.write(b"Test Suite 'All tests' failed at 2026-07-30 05:22:30.761.\n")
    reason = probe()
    assert reason is not None and "Test Suite 'All tests' failed" in reason


def test_run_ended_probe_finds_a_marker_split_across_two_reads(tmp_path: Path) -> None:
    # Each probe reads only what was appended, so a marker straddling two reads would be invisible
    # without the carried overlap.
    log = tmp_path / "runner.log"
    marker = b"Test Suite 'All tests' failed"
    log.write_bytes(marker[:10])
    probe = _run_ended_probe(log)
    assert probe() is None
    with log.open("ab") as fh:
        fh.write(marker[10:] + b" at 2026-07-30 05:22:30.761.\n")
    assert probe() is not None


def test_run_ended_probe_sticks_the_launch_timeout_cause_across_reads(tmp_path: Path) -> None:
    # The launch timeout is logged well before the suite reports failure (BE-XXXX unit 1's premise),
    # so the two markers rarely land in the same read window — the flag has to persist across probe
    # calls to still name the cause, the signal a reader needs to tell "this device needs rebooting"
    # from "this build is broken".
    log = tmp_path / "runner.log"
    log.write_bytes(b"")
    probe = _run_ended_probe(log)
    with log.open("ab") as fh:
        fh.write(
            b"<unknown>:0: error: Failed to launch com.example.app: "
            b"Timed out attempting to launch app.\n"
        )
    assert probe() is None  # the app launch timed out, but the xctest run has not ended yet
    with log.open("ab") as fh:
        fh.write(b"Test Suite 'All tests' failed at 2026-07-30 05:22:30.761.\n")
    reason = probe()
    assert reason is not None and "after the app launch timed out" in reason


def test_run_ended_probe_names_no_cause_without_a_launch_timeout(tmp_path: Path) -> None:
    # The negative case: a suite failure with no preceding launch-timeout marker gets no cause suffix,
    # so the sticky flag pins the specific signature rather than firing on every run-ended failure.
    log = tmp_path / "runner.log"
    log.write_bytes(b"Test Suite 'All tests' failed at 2026-07-30 05:22:30.761.\n")
    reason = _run_ended_probe(log)()
    assert reason is not None and "after the app launch timed out" not in reason


def test_run_ended_probe_is_quiet_when_the_capture_does_not_exist(tmp_path: Path) -> None:
    # A spawn that failed before writing anything must not be judged by a missing file.
    assert _run_ended_probe(tmp_path / "absent.log")() is None
    assert _run_ended_probe(None)() is None


def test_run_ended_probe_keeps_reporting_the_marker_it_already_found(tmp_path: Path) -> None:
    # BE-0354: the probe advances a private offset, so an unlatched one would answer "ended" from the
    # window that first held the marker and "still running" from every window after it. The mid-run
    # liveness predicate re-asks throughout each recovery episode — once at the crash and then once a
    # second while the recovery wait runs (BE-0360) — which is exactly where that would flip a dead
    # runner back to alive and hand it the full health wait again.
    log = tmp_path / "runner.log"
    log.write_bytes(b"Test Suite 'All tests' failed at 2026-08-09 23:38:12.001.\n")
    probe = _run_ended_probe(log)
    first = probe()
    assert first is not None
    with log.open("ab") as fh:
        fh.write(b"more output the runner keeps producing\n")
    assert probe() == first


def test_run_ended_probe_reads_the_watchdog_restarts_selected_tests_suite(tmp_path: Path) -> None:
    # The capture a real wedged runner leaves behind, and the one the `All tests` spellings alone
    # could not read. XCTest's watchdog judges the unresponsive in-Simulator host a test timeout,
    # relaunches it, re-runs zero tests, and ends — reporting under `Selected tests`, so a probe
    # matching only `All tests` answers "still running" about a port that is already dead. Taken
    # verbatim (bar the elision) from the fault-injection lane's own runner capture.
    log = tmp_path / "runner.log"
    log.write_bytes(
        b"Restarting after unexpected exit, crash, or test timeout; summary will include totals "
        b"from previous launches.\n"
        b"Test Suite 'Selected tests' started at 2026-08-11 17:42:16.444.\n"
        b"Test Suite 'RunnerUITest' passed at 2026-08-11 17:42:16.446.\n"
        b"Test Suite 'Selected tests' passed at 2026-08-11 17:42:16.447.\n"
        b"** BUILD INTERRUPTED **\n"
    )
    reason = _run_ended_probe(log)()
    assert reason is not None and "Test Suite 'Selected tests' passed" in reason


def test_run_ended_probe_waits_for_a_terminal_line_before_calling_a_restart_ended(
    tmp_path: Path,
) -> None:
    # The guard on the markers above, and the reason only *terminal* spellings may join that family:
    # what ends a run is `passed` / `failed`, never the suite name and never the restart itself.
    #
    # Two ways a live runner reports under `Selected tests`. A `.xctestrun` carrying
    # `OnlyTestIdentifiers` — which a per-target `xcuitest.build` or a prebuilt `testRunner` can hand
    # over, outside this module's control — names a perfectly healthy run that way from its first
    # line. And a restarted run re-runs the *remaining* tests rather than zero of them whenever the
    # relaunch succeeds, binding its port during that window. Latching on either would have the cold
    # gate discard a runner that was about to serve, and `_runner_alive` call a live runner gone
    # mid-call — so the probe stays quiet until a terminal line actually lands.
    log = tmp_path / "runner.log"
    log.write_bytes(
        b"Test Suite 'Selected tests' started at 2026-08-11 17:40:08.199.\n"
        b"Test Suite 'BajutsuRunnerUITests.xctest' started at 2026-08-11 17:40:08.200.\n"
        b"Test Suite 'RunnerUITest' started at 2026-08-11 17:40:08.201.\n"
        b"Restarting after unexpected exit, crash, or test timeout; summary will include totals "
        b"from previous launches.\n"
        b"Test Suite 'Selected tests' started at 2026-08-11 17:40:12.201.\n"
    )
    probe = _run_ended_probe(log)
    assert probe() is None
    # The `failed` half of the restarted family — the watchdog capture above reports `passed` only
    # because it re-ran zero tests, so this is the spelling a restart that re-runs and fails lands on.
    with log.open("ab") as fh:
        fh.write(b"Test Suite 'Selected tests' failed at 2026-08-11 17:40:31.884.\n")
    reason = probe()
    assert reason is not None and "Test Suite 'Selected tests' failed" in reason


def test_runner_alive_reports_gone_once_the_test_run_ended(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The blind spot BE-0305 measured: XCTest restarts the in-Simulator host after a crash and re-runs
    # zero tests, so the suite reports its result while `xcodebuild` lives on. Reading the process
    # handle alone keeps answering "alive", and every recovery episode then waits out its full window
    # on a port that will never bind again.
    _, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert env._runner_alive()  # the process runs and the capture shows a live run
    assert env._runner_log is not None
    with env._runner_log.open("ab") as fh:
        fh.write(b"Test Suite 'All tests' failed at 2026-08-09 23:38:12.001.\n")
    assert not env._runner_alive()
    # Latched, so the answer holds for the *next* recovery episode too rather than flipping back.
    assert not env._runner_alive()


def test_a_fresh_spawn_starts_the_run_ended_probe_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # One probe per spawn, keyed to that spawn's own capture: a respawn after a run that ended must
    # not inherit the previous runner's marker and declare the new one dead before it has served a call.
    wedged = {"v": True}  # the warm resident fails its health probe, so the next lease spawns cold
    _, _, run = _fake_toolchain(monkeypatch, wedged=wedged)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    env.start(eff, Preconditions())
    first_log = env._runner_log
    assert first_log is not None
    with first_log.open("ab") as fh:
        fh.write(b"Test Suite 'All tests' failed at 2026-08-09 23:38:12.001.\n")
    assert not env._runner_alive()
    env.start(eff, Preconditions())
    assert env._runner_log != first_log  # a genuinely new spawn, with its own capture
    assert env._runner_alive()


def test_await_cold_runner_fails_fast_when_the_test_run_ended_though_the_process_lives() -> None:
    # The gap the process-liveness check alone cannot see: `xcodebuild` finished its test run (the
    # app-launch timeout on the ios-e2e lane) but lingers, so `poll()` stays None and the wait would
    # otherwise run to the ceiling. The capture's terminal marker ends it at once — the huge timeout
    # here is never spent.
    spawned = _fake_spawned(
        ready=lambda: False, poll=lambda: None, run_ended=lambda: "the xctest run ended (x)"
    )
    # The clock advances per probe, so a wait that ignored the marker would end at the ceiling
    # instead of spinning — the detection's absence fails this loudly rather than hanging the gate.
    now = 0.0

    def clock() -> float:
        nonlocal now
        now += 100.0
        return now

    failure = _await_cold_runner(
        spawned, timeout=999.0, poll=0.0, sleep=lambda _s: None, clock=clock
    )
    assert failure is not None and "the xctest run ended" in failure.detail
    assert failure.kind == "run-ended"
    assert now == 100.0  # only the deadline was read: the marker ended the wait on the first round


def test_await_cold_runner_ready_wins_over_an_ended_run() -> None:
    # Probe order again: a runner answering /health is up, so a marker that landed in the same round
    # (a suite line racing the health server) must not turn a live runner into a failure.
    spawned = _fake_spawned(ready=lambda: True, run_ended=lambda: "the xctest run ended (x)")
    assert (
        _await_cold_runner(spawned, timeout=1.0, poll=0.0, sleep=lambda _s: None, clock=lambda: 0.0)
        is None
    )


def test_await_cold_runner_times_out_when_never_ready_and_process_alive() -> None:
    # A runner whose process stays alive but never binds its port fails at the deadline (the
    # `health never ready` case), driven by the injected clock so the gate spends no wall time.
    ticks = iter([0.0, 0.0, 0.3])  # deadline = 0.0 + 0.2; the second poll is past it
    spawned = _fake_spawned(ready=lambda: False, poll=lambda: None)
    failure = _await_cold_runner(
        spawned, timeout=0.2, poll=0.0, sleep=lambda _s: None, clock=lambda: next(ticks)
    )
    assert failure is not None and "health never ready within 0.2s" in failure.detail
    assert failure.kind == "never-ready"


def test_spawn_cold_retries_once_then_succeeds() -> None:
    # BE-0319 unit 4: a one-off cold-start blip (the first attempt's process dies) is absorbed by a
    # single retry; the second attempt comes up and its driver is returned. Exactly two spawns.
    spawns = 0
    discards: list[int] = []

    def spawn() -> _Spawned:
        nonlocal spawns
        spawns += 1
        n = spawns
        first = n == 1
        return _Spawned(
            driver=_NamedDriver(f"driver-{n}"),
            ready=(lambda: not first),  # first attempt never becomes ready
            poll=(lambda: 1 if first else None),  # first attempt's process died
            log_tail=lambda: "",
            discard=lambda: discards.append(n),
        )

    result = _spawn_cold_with_retry(
        spawn, timeout=1.0, poll=0.0, sleep=lambda _s: None, clock=lambda: 0.0
    )
    assert result.driver.name == "driver-2"
    assert spawns == 2 and discards == [
        1
    ]  # the failed first attempt discarded, the live second kept


def test_spawn_cold_does_not_retry_after_a_timeout() -> None:
    # With nothing about the device changed (the default no-op recovery), the startup ceiling is a
    # *total* budget shared across attempts: a first attempt that spends the whole ceiling on "health
    # never ready" (process alive, never binds its port) leaves nothing for a retry, so exactly one
    # attempt runs and it fails loudly. A second full-ceiling wait against the same device would
    # double the worst case (300s → 600s on the ios-e2e lane) for no new information. A recovery that
    # repaired the device does earn a fresh ceiling — see the fresh-budget test below.
    spawns = 0

    def spawn() -> _Spawned:
        nonlocal spawns
        spawns += 1
        return _Spawned(
            driver=_NamedDriver(f"driver-{spawns}"),
            ready=lambda: False,  # never binds its port
            poll=lambda: None,  # the process stays alive — the timeout path, not fail-fast
            log_tail=lambda: "",
            discard=lambda: None,
        )

    # outer deadline 0.0+0.2; attempt 1's own wait crosses at 0.3 (health never ready); the retry
    # check then sees the shared budget spent (0.3 > 0.2) and stops before a second spawn.
    ticks = iter([0.0, 0.0, 0.0, 0.3, 0.3])
    with pytest.raises(XcuitestChannelError) as excinfo:
        _spawn_cold_with_retry(
            spawn, timeout=0.2, poll=0.0, sleep=lambda _s: None, clock=lambda: next(ticks)
        )
    assert spawns == 1  # a spent budget yields no second attempt
    assert "health never ready" in str(excinfo.value)


def test_spawn_cold_retries_after_an_ended_run_because_it_leaves_budget() -> None:
    # The residual BE-0319 left behind, closed. A Simulator app-launch timeout used to hold the wait
    # at the ceiling with the process alive, spending the shared budget and so making the retry
    # structurally unreachable — the one failure the retry existed to absorb. Detecting the ended run
    # returns while the budget is nearly intact, so the second attempt actually runs.
    spawns = 0

    def spawn() -> _Spawned:
        nonlocal spawns
        spawns += 1
        first = spawns == 1
        return _Spawned(
            driver=_NamedDriver(f"driver-{spawns}"),
            ready=(lambda: not first),
            poll=lambda: None,  # the process lingers throughout: only the marker ends attempt 1
            log_tail=lambda: "",
            discard=lambda: None,
            run_ended=(
                lambda: "the xctest run ended (Test Suite 'All tests' failed)" if first else None
            ),
        )

    # Every probe costs wall time, so a wait that ignored the marker would still reach the ceiling
    # and spend the budget — this fails loudly (rather than spinning) if the detection is lost.
    now = 0.0

    def clock() -> float:
        nonlocal now
        now += 10.0
        return now

    result = _spawn_cold_with_retry(
        spawn, timeout=300.0, poll=0.0, sleep=lambda _s: None, clock=clock
    )
    assert result.driver.name == "driver-2" and spawns == 2


def test_spawn_cold_fails_loudly_after_exactly_two_attempts_with_both_tails() -> None:
    # A repeatable failure (a broken build) fails every attempt and still stops the gate (BE-0049);
    # the loud error carries each attempt's captured tail (unit 2), and there are exactly two — the
    # retry is bounded to one, never unbounded.
    spawns = 0

    def spawn() -> _Spawned:
        nonlocal spawns
        spawns += 1
        n = spawns
        return _Spawned(
            driver=_NamedDriver(),
            ready=lambda: False,
            poll=lambda: 65,  # xcodebuild exited on every attempt
            log_tail=lambda: f"\n<<tail-{n}>>",
            discard=lambda: None,
        )

    with pytest.raises(XcuitestChannelError) as excinfo:
        _spawn_cold_with_retry(
            spawn, timeout=1.0, poll=0.0, sleep=lambda _s: None, clock=lambda: 0.0
        )
    message = str(excinfo.value)
    assert spawns == 2  # bounded to a single retry: exactly two attempts, no more
    assert "attempt 1/2" in message and "attempt 2/2" in message
    assert "<<tail-1>>" in message and "<<tail-2>>" in message  # each attempt's tail is folded in
    assert "exited (code 65)" in message  # the dead-process reason (unit 3) reaches the error


def test_a_wedged_discard_never_replaces_the_exception_in_flight(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The discard that runs while an exception propagates now terminates through simctl, so it can
    # raise a `DeviceTimeout` of its own (BE-0363). That fault must never take the propagating
    # exception's place: the CLI prints `str(exc)`, not a chained traceback, so replacing it would
    # lose the reason the teardown ran at all — and a KeyboardInterrupt would stop reaching the
    # operator. Logged instead, so the wedge is still visible.
    def spawn() -> _Spawned:
        def wedged_discard() -> None:
            raise simctl.DeviceTimeout(
                "device operation timed out after 60s: xcrun simctl terminate"
            )

        def ready() -> bool:
            # Deliberately outside the `DeviceError` hierarchy (which subclasses `RuntimeError`), so
            # a discard fault that took its place would fail this test on the type, not only the text.
            raise ValueError("<<the original failure>>")

        return _Spawned(
            driver=_NamedDriver(),
            ready=ready,
            poll=lambda: None,
            log_tail=lambda: "",
            discard=wedged_discard,
        )

    # The propagating exception is the original one, by type and by message — not the DeviceTimeout.
    with (
        caplog.at_level("WARNING"),
        pytest.raises(ValueError, match="<<the original failure>>"),
    ):
        _spawn_cold_with_retry(
            spawn, timeout=1.0, poll=0.0, sleep=lambda _s: None, clock=lambda: 0.0
        )
    assert "timed out" in caplog.text  # the swallowed discard fault is still traceable


def test_a_wedged_discard_between_attempts_keeps_every_attempt_s_diagnostic() -> None:
    # The per-attempt discard can now raise too, and escaping bare would throw away the diagnostics
    # this function exists to build (unit 2) — the operator would learn that simctl timed out and
    # nothing about why the runner never came up. Folded in exactly like an unrepairable device is.
    def spawn() -> _Spawned:
        def wedged_discard() -> None:
            raise simctl.DeviceTimeout(
                "device operation timed out after 60s: xcrun simctl terminate"
            )

        return _Spawned(
            driver=_NamedDriver(),
            ready=lambda: False,
            poll=lambda: 65,  # xcodebuild exited: a classified failure with a tail to preserve
            log_tail=lambda: "\n<<tail-1>>",
            discard=wedged_discard,
        )

    with pytest.raises(simctl.DeviceError) as excinfo:
        _spawn_cold_with_retry(
            spawn, timeout=1.0, poll=0.0, sleep=lambda _s: None, clock=lambda: 0.0
        )
    message = str(excinfo.value)
    assert "attempt 1/2" in message and "<<tail-1>>" in message  # what the attempt observed
    assert "discard after attempt 1 failed" in message and "timed out" in message


# --- device recovery between cold-spawn attempts --- #
#
# BE-0319's retry isolates every *host*-side resource per attempt (port, .xctestrun, capture) but
# hands the retry the same device the first attempt failed on. These cover the seam that repairs the
# device in between, and what that does to the retry's budget.


def _failing_spawn(counter: list[int], *, ready_on: int | None = None) -> Callable[[], _Spawned]:
    """A spawn whose Nth attempt is the first to come up (never, when `ready_on` is None)."""

    def spawn() -> _Spawned:
        counter.append(1)
        n = len(counter)
        return _Spawned(
            driver=_NamedDriver(f"driver-{n}"),
            ready=lambda: n == ready_on,
            poll=lambda: None,  # alive throughout: the ended run is what fails each attempt
            log_tail=lambda: "",
            discard=lambda: None,
            run_ended=(
                lambda: (
                    None
                    if n == ready_on
                    else "the xctest run ended (Test Suite 'All tests' failed) after the app launch "
                    "timed out"
                )
            ),
        )

    return spawn


def test_recovery_gets_the_classified_failure_and_runs_between_attempts() -> None:
    # The rung is chosen from the failure's `kind`, so a recovery never has to parse the prose.
    spawns: list[int] = []
    seen: list[str] = []

    def recover(failure: _AttemptFailure) -> _Recovery | None:
        seen.append(failure.kind)
        return _Recovery("rebooted it")

    result = _spawn_cold_with_retry(
        _failing_spawn(spawns, ready_on=2),
        timeout=300.0,
        recover=recover,
        poll=0.0,
        sleep=lambda _s: None,
        clock=lambda: 0.0,
    )
    assert result.driver.name == "driver-2"
    assert seen == ["run-ended"]  # recovery ran once, between the two attempts


def test_a_repaired_device_earns_a_fresh_ceiling_even_after_a_spent_budget() -> None:
    # The heart of the fix. An app-launch timeout leaves a degraded device, not spare seconds, so the
    # shared budget alone makes the retry unreachable exactly when it is most needed. A recovery that
    # rebooted or replaced the device restarts the ceiling, because the next attempt runs against a
    # device that demonstrably came back up.
    spawns: list[int] = []
    now = 0.0

    def clock() -> float:
        nonlocal now
        now += 100.0  # every probe is expensive: attempt 1 spends the whole 200s ceiling
        return now

    result = _spawn_cold_with_retry(
        _failing_spawn(spawns, ready_on=2),
        timeout=200.0,
        recover=lambda _f: _Recovery("rebooted it", fresh_budget=200.0),
        poll=0.0,
        sleep=lambda _s: None,
        clock=clock,
    )
    assert result.driver.name == "driver-2" and len(spawns) == 2


def test_a_recovery_that_changed_nothing_keeps_the_shared_budget() -> None:
    # The other half of that contract: a rung that only observed the device (the probe itself failed,
    # or an `xcodebuild` that exited on its own) must not buy a second full wait.
    spawns: list[int] = []
    ticks = iter([0.0, 0.0, 0.0, 0.3, 0.3])
    with pytest.raises(XcuitestChannelError) as excinfo:
        _spawn_cold_with_retry(
            _failing_spawn(spawns),
            timeout=0.2,
            recover=lambda _f: _Recovery("could not probe the device; left it as it is"),
            poll=0.0,
            sleep=lambda _s: None,
            clock=lambda: next(ticks),
        )
    assert len(spawns) == 1
    # The note still reaches the error, so a reader sees the rung ran and chose to do nothing.
    assert "recovery after attempt 1: could not probe the device" in str(excinfo.value)


def test_recovery_notes_are_folded_into_the_loud_failure() -> None:
    spawns: list[int] = []
    with pytest.raises(XcuitestChannelError) as excinfo:
        _spawn_cold_with_retry(
            _failing_spawn(spawns),
            timeout=300.0,
            recover=lambda _f: _Recovery("UDID-old vanished; created replacement UDID-new"),
            poll=0.0,
            sleep=lambda _s: None,
            clock=lambda: 0.0,
        )
    message = str(excinfo.value)
    assert "attempt 1/2" in message and "attempt 2/2" in message
    assert "recovery after attempt 1: UDID-old vanished; created replacement UDID-new" in message
    # No recovery is attempted after the *last* attempt: there is no further spawn to prepare for.
    assert "recovery after attempt 2" not in message


def test_a_device_that_cannot_be_repaired_fails_the_run() -> None:
    # A host that lost its Simulator runtimes is a device fault, not a flaky spawn: the recovery
    # raises and the error propagates, rather than funding another attempt that cannot work.
    spawns: list[int] = []

    def recover(_f: _AttemptFailure) -> _Recovery | None:
        raise simctl.DeviceError("no iPhone device type is available to replace it")

    with pytest.raises(simctl.DeviceError, match="no iPhone device type"):
        _spawn_cold_with_retry(
            _failing_spawn(spawns),
            timeout=300.0,
            recover=recover,
            poll=0.0,
            sleep=lambda _s: None,
            clock=lambda: 0.0,
        )
    assert len(spawns) == 1  # the second attempt was never spawned


def test_diagnostics_survive_when_the_device_cannot_be_repaired() -> None:
    # A device fault must not swallow what got the run there: attempt 1's classified reason (the
    # operator's only signal for *why* the runner never answered, BE-0319 unit 2) would otherwise
    # vanish behind the bare DeviceError `recover` raises.
    spawns: list[int] = []

    def recover(_f: _AttemptFailure) -> _Recovery | None:
        raise simctl.DeviceError("no iPhone device type is available to replace it")

    with pytest.raises(simctl.DeviceError) as excinfo:
        _spawn_cold_with_retry(
            _failing_spawn(spawns),
            timeout=300.0,
            recover=recover,
            poll=0.0,
            sleep=lambda _s: None,
            clock=lambda: 0.0,
        )
    message = str(excinfo.value)
    assert "attempt 1/2" in message  # the classified failure that led to the unrepairable device
    assert "recovery after attempt 1 failed: no iPhone device type" in message


def test_an_oserror_from_recovery_is_folded_in_too() -> None:
    # Every rung reaches simctl through a subprocess call, which can raise OSError (a fork that
    # fails, an xcrun that has gone) as well as CalledProcessError — and nothing on the reboot/
    # replace paths converts it. A bare OSError escaping here would drop attempt 1's classified
    # reason and log tail exactly like the DeviceError case above.
    spawns: list[int] = []

    def recover(_f: _AttemptFailure) -> _Recovery | None:
        raise OSError("cannot fork")

    with pytest.raises(simctl.DeviceError) as excinfo:
        _spawn_cold_with_retry(
            _failing_spawn(spawns),
            timeout=300.0,
            recover=recover,
            poll=0.0,
            sleep=lambda _s: None,
            clock=lambda: 0.0,
        )
    message = str(excinfo.value)
    assert "attempt 1/2" in message
    assert "recovery after attempt 1 failed: cannot fork" in message
    assert len(spawns) == 1  # the second attempt was never spawned


# --- the ladder itself: which rung a classified failure picks, against a fake simctl --- #
#
# These drive `_recover_between_attempts` directly: it is the decision the rest of the recovery hangs
# on, and calling it without a spawn keeps each case to the simctl argvs it is actually about.


def _eff_for_ladder(*, app_path: str | None = None) -> Effective:
    # No appPath by default, so the re-prepare exercises boot / locale / permissions without needing
    # a built app; the replace-rung tests that must get past the appPath guard pass one.
    return _sim_eff(test_runner="/nonexistent.xctestrun", app_path=app_path)


def _device_json(udids: list[str], *, device_type: str = "com.apple.x.iPhone-17-Pro") -> str:
    import json

    return json.dumps(
        {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                    {"udid": u, "deviceTypeIdentifier": device_type, "state": "Booted"}
                    for u in udids
                ]
            }
        }
    )


def _ladder_run(
    present: list[str],
    *,
    created: str = "UDID-NEW",
    devicetypes: list[dict[str, str]] | None = None,
    stays_booted_after_shutdown: bool = False,
    booted_listing_fails: bool = False,
) -> tuple[list[list[str]], simctl.RunFn]:
    """A fake simctl that lists `present` as available and mints `created` on `simctl create`.

    `devicetypes` overrides the fake `list devicetypes` response — the default holds only
    "iPhone 17 Pro", so a caller exercising the configured-model fallback tier (rather than the
    iPhone-fallback tier, which that single entry cannot tell apart) must pass its own.

    `stays_booted_after_shutdown`, when True, models a CoreSimulator wedged enough that `simctl
    shutdown` silently no-ops — the case `_reboot_device`'s post-shutdown read-back exists to catch.
    False (the default) lets shutdown/boot/bootstatus behave as a healthy host would, so `present`'s
    devices start out booted and stay that way except across a real shutdown.

    `booted_listing_fails`, when True, makes `simctl list devices booted` itself fail — the host too
    wedged even to answer that probe, the case `simctl.device_booted` reads as unknown rather than
    "not booted" (BE-0359).
    """
    import json

    calls: list[list[str]] = []
    booted = set(present)
    types = devicetypes or [
        {
            "name": "iPhone 17 Pro",
            "identifier": "com.apple.x.iPhone-17-Pro",
            "productFamily": "iPhone",
        }
    ]

    def run(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        calls.append(argv)
        verb = argv[2:3]
        if verb == ["list"] and argv[3:4] == ["devicetypes"]:
            return json.dumps({"devicetypes": types})
        if verb == ["list"] and argv[3:5] == ["devices", "booted"]:
            if booted_listing_fails:
                raise OSError("simctl not found")
            return _device_json([u for u in present if u in booted])
        if verb == ["list"]:
            return _device_json(present)
        if verb == ["create"]:
            present.append(created)
            return f"{created}\n"
        if verb == ["shutdown"]:
            if not stays_booted_after_shutdown:
                booted.discard(argv[3])
            return ""
        if verb in (["boot"], ["bootstatus"]):
            booted.add(argv[3])
            return ""
        if argv[4:7] == ["defaults", "export", "-globalDomain"]:
            return _globals_plist("en_US")
        return ""

    return calls, run


def _verb_seq(calls: list[list[str]]) -> list[str]:
    return [c[2] for c in calls if c[:2] == ["xcrun", "simctl"]]


def test_a_process_exit_leaves_the_booted_device_alone() -> None:
    # An `xcodebuild` that exited on its own says nothing about the device, and the discard has already
    # terminated the app — so the cheapest rung is the right one, and it buys no fresh budget.
    calls, run = _ladder_run(["UDID"])
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    recovery = env._recover_between_attempts(
        _AttemptFailure("process-exit", "exited (code 65)"),
        _eff_for_ladder(),
        Preconditions(),
        None,
    )
    assert recovery is not None and recovery.fresh_budget is None
    assert _verb_seq(calls) == ["list"]  # probed, nothing more


def test_an_app_launch_timeout_reboots_and_re_prepares_the_device() -> None:
    # The dominant flake: the device stopped honouring automation, which only a reboot clears. The
    # re-prepare is what puts the app and its permissions back where a scenario expects them.
    calls, run = _ladder_run(["UDID"])
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    recovery = env._recover_between_attempts(
        _AttemptFailure("run-ended", "the xctest run ended after the app launch timed out"),
        _eff_for_ladder(),
        Preconditions(),
        None,
    )
    assert recovery is not None and recovery.fresh_budget == _runner_startup_timeout()
    # "list" after "shutdown" is the booted-udids read-back confirming the shutdown actually took.
    # The trailing pair is the re-prepare's own boot, waited out like the rung's own (BE-0359).
    assert _verb_seq(calls)[:7] == [
        "list",
        "shutdown",
        "list",
        "boot",
        "bootstatus",
        "boot",
        "bootstatus",
    ]
    assert "rebooted UDID" in recovery.note


def test_a_device_that_will_not_shut_down_earns_no_fresh_budget() -> None:
    # `Env.shutdown()` suppresses its own failure, and a CoreSimulator wedged enough to stop
    # honouring automation is exactly where `simctl shutdown` itself fails — so confirm the device
    # actually left `Booted` before claiming a reboot and granting a fresh ceiling on that basis.
    # Left unchecked, the retry would spawn onto the same still-wedged device with a fresh budget
    # instead of the exhausted shared one: the pre-recovery stall, plus one extra ceiling of time.
    calls, run = _ladder_run(["UDID"], stays_booted_after_shutdown=True)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    recovery = env._recover_between_attempts(
        _AttemptFailure("run-ended", "the xctest run ended after the app launch timed out"),
        _eff_for_ladder(),
        Preconditions(),
        None,
    )
    assert recovery is not None and recovery.fresh_budget is None
    assert "would not shut down" in recovery.note
    # No boot, no bootstatus, no re-prep: a device nothing changed earns no further work either.
    assert _verb_seq(calls) == ["list", "shutdown", "list"]


def test_an_unreadable_booted_listing_also_earns_no_fresh_budget() -> None:
    # `simctl.device_booted` is three-valued like `device_available`: the same wedged host that
    # makes `shutdown` no-op can also make `simctl list devices booted` fail outright, and an
    # unreadable listing confirms a reboot no more than one that still shows the device up does —
    # `booted_udids`' empty-on-any-failure result would collapse that into "not booted" and wrongly
    # grant a fresh budget onto the still-wedged device.
    calls, run = _ladder_run(["UDID"], booted_listing_fails=True)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    recovery = env._recover_between_attempts(
        _AttemptFailure("run-ended", "the xctest run ended after the app launch timed out"),
        _eff_for_ladder(),
        Preconditions(),
        None,
    )
    assert recovery is not None and recovery.fresh_budget is None
    assert "would not shut down" in recovery.note
    assert _verb_seq(calls) == ["list", "shutdown", "list"]


def test_a_vanished_device_is_replaced_and_reported_to_the_pool(tmp_path: Path) -> None:
    # The exit-70 case: simctl no longer lists the device, so retrying onto it cannot work. The run
    # continues on a replacement, and `replaced_device()` is how the pool learns to re-key by it.
    app = tmp_path / "App.app"
    app.mkdir()
    calls, run = _ladder_run([])  # the leased device is gone from the listing
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    recovery = env._recover_between_attempts(
        _AttemptFailure("run-ended", "the xctest run ended"),
        _eff_for_ladder(app_path=str(app)),
        Preconditions(),
        None,
    )
    assert recovery is not None and recovery.fresh_budget == _runner_startup_timeout()
    assert env._udid == "UDID-NEW" and env.replaced_device() == "UDID-NEW"
    # Created, then booted to completion *before* the prep, so the first boot is not charged to the
    # next attempt's readiness ceiling. The listings ahead of `create` are the probe and the
    # device-type lookup (this environment never cold-prepped, so it captured no type to clone).
    seq = _verb_seq(calls)
    assert seq[0] == "list" and "create" in seq
    assert seq[seq.index("create") + 1] == "bootstatus"
    assert "vanished" in recovery.note and "UDID-NEW" in recovery.note


def test_both_driver_construction_sites_hand_over_the_parsed_device_os(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # BE-0358: the OS is derived from the runtime identifier the cold prep already captures for
    # device cloning, so it costs no extra simctl call — and it is passed at the warm resume too,
    # because a device replacement can move a lease onto a different Simulator mid-run and both
    # sites build a fresh driver afterwards.
    _, _, run = _fake_toolchain(monkeypatch)
    handed: list[object] = []

    class _Driver:
        def await_ready(self, timeout: float = 10.0) -> None: ...

        def health_ready(self) -> bool:
            return True

    def _make_driver(*_a: object, **kwargs: Any) -> _Driver:
        handed.append(kwargs.get("device_os"))
        return _Driver()

    monkeypatch.setattr(backends, "make_driver", _make_driver)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    env.start(eff, Preconditions())  # cold spawn
    env.start(eff, Preconditions())  # warm resume — the second construction site

    assert len(handed) == 2
    assert handed == [DeviceOS("ios", 26, 0, "iOS 26.0")] * 2
    assert [o.label for o in handed if isinstance(o, DeviceOS)] == ["iOS 26.0"] * 2


def test_a_replacement_clones_the_type_captured_while_the_device_was_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The device type is recorded at the first cold prep, because by the time a replacement is needed
    # the device is no longer there to ask — so the replacement is the model the run was written
    # against rather than whatever the host happens to offer.
    _, simctl_calls, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert simctl.list_all_devices_cmd() in simctl_calls
    assert env._device_type_id == "com.apple.x.iPhone-17-Pro"
    # The runtime comes from the same listing, for free, so the replacement can clone the OS version
    # too, not just the model.
    assert env._device_runtime_id == "com.apple.CoreSimulator.SimRuntime.iOS-26-0"

    # With a type already captured, the replacement clones it and never consults `list devicetypes`.
    app = tmp_path / "App.app"
    app.mkdir()
    calls, replace_run = _ladder_run([])
    env._run = replace_run
    env._recover_between_attempts(
        _AttemptFailure("run-ended", "ended"),
        _eff_for_ladder(app_path=str(app)),
        Preconditions(),
        None,
    )
    # The name leads with the model (the report's device row and `serve`'s `iphone`/`ipad` capability
    # token both read it as one) and carries the replaced udid so recoveries stay tellable apart.
    assert (
        simctl.create_cmd(
            "iPhone 17 Pro (bajutsu-recovered-UDID)",
            "com.apple.x.iPhone-17-Pro",
            "com.apple.CoreSimulator.SimRuntime.iOS-26-0",
        )
        in calls
    )
    assert not any(c[2:4] == ["list", "devicetypes"] for c in calls)


def test_a_replacement_re_reads_its_actual_type_and_runtime(tmp_path: Path) -> None:
    # create_device retries unpinned when the requested runtime is gone, so a replacement can land on
    # a different iOS version than _device_runtime_id named — caching the *requested* type/runtime
    # after create would leave the environment believing it got what it asked for. Clearing both
    # instead lets _finish_repair's prep (which runs `device_type_of` when _device_type_id is None)
    # re-read them from the device that actually exists.
    app = tmp_path / "App.app"
    app.mkdir()
    _calls, run = _ladder_run([])  # the leased device is gone; devicetypes default to iPhone 17 Pro
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    # Seed a stale capture, as if an earlier cold prep had recorded a different runtime.
    env._device_type_id = "com.apple.x.iPhone-17-Pro"
    env._device_runtime_id = "com.apple.CoreSimulator.SimRuntime.iOS-19-0"
    env._recover_between_attempts(
        _AttemptFailure("run-ended", "ended"),
        _eff_for_ladder(app_path=str(app)),
        Preconditions(),
        None,
    )
    # Re-read from the replacement (iOS 26.0, per _device_json), not left at the stale iOS 19.0.
    assert env._device_runtime_id == "com.apple.CoreSimulator.SimRuntime.iOS-26-0"
    assert env._device_type_id == "com.apple.x.iPhone-17-Pro"


def test_a_probe_that_could_not_run_changes_nothing() -> None:
    # A host too sick to list its devices must not have a device replaced on that evidence.
    calls: list[list[str]] = []

    def run(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        calls.append(argv)
        raise OSError("xcrun unavailable")

    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    recovery = env._recover_between_attempts(
        _AttemptFailure("never-ready", "health never ready"),
        _eff_for_ladder(),
        Preconditions(),
        None,
    )
    assert recovery is not None and recovery.fresh_budget is None
    assert env._udid == "UDID" and env.replaced_device() is None
    assert "could not probe" in recovery.note


def test_a_vanished_device_with_no_replaceable_type_fails_loudly(tmp_path: Path) -> None:
    # A host that lost its runtimes along with the device: nothing is left to run on, so this is a
    # device fault rather than another doomed attempt.
    import json

    app = tmp_path / "App.app"
    app.mkdir()

    def run(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        if argv[2:4] == ["list", "devicetypes"]:
            return json.dumps({"devicetypes": []})  # no iPhone type at all
        if argv[2:3] == ["list"]:
            return _device_json([])
        return ""

    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    with pytest.raises(simctl.DeviceError, match="no device type matching iPhone 15 is available"):
        env._recover_between_attempts(
            _AttemptFailure("run-ended", "ended"),
            _eff_for_ladder(app_path=str(app)),
            Preconditions(),
            None,
        )


def test_a_vanished_device_with_no_app_path_fails_loudly() -> None:
    # A replacement is a blank device; without an appPath to install onto it, the retry would spawn
    # onto a device with no app to launch. Fail before paying the create/boot/re-prep proving that.
    calls, run = _ladder_run([])  # the leased device is gone from the listing
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    with pytest.raises(simctl.DeviceError, match="configures no appPath"):
        env._recover_between_attempts(
            _AttemptFailure("run-ended", "ended"),
            _eff_for_ladder(),  # no appPath
            Preconditions(),
            None,
        )
    assert not any(
        c[2:3] == ["create"] for c in calls
    )  # no device was minted for nothing to run on


def test_a_replacement_prefers_the_configured_model_over_the_newest_iphone(tmp_path: Path) -> None:
    # Tier 2 of the fallback ladder (the configured device model) sits between tier 1 (the vanished
    # device's own captured type) and tier 3 (whichever iPhone this host ships) — and only shows up
    # when the host has more than one iPhone type, since a single-entry listing can't tell "the
    # configured model happens to be newest" from "tier 3 fired instead of tier 2".
    app = tmp_path / "App.app"
    app.mkdir()
    devicetypes = [
        {"name": "iPhone 15", "identifier": "com.apple.x.iPhone-15", "productFamily": "iPhone"},
        {
            "name": "iPhone 17 Pro",
            "identifier": "com.apple.x.iPhone-17-Pro",
            "productFamily": "iPhone",
        },
    ]
    calls, run = _ladder_run([], devicetypes=devicetypes)  # the leased device is gone
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env._recover_between_attempts(
        _AttemptFailure("run-ended", "ended"),
        _eff_for_ladder(app_path=str(app)),  # defaults.device is "iPhone 15"
        Preconditions(),
        None,
    )
    created = next(c for c in calls if c[2:3] == ["create"])
    # The configured identifier, not the newest-iPhone fallback — tier 2 wins over tier 3.
    assert created[4] == "com.apple.x.iPhone-15"


def test_an_ipad_target_is_never_replaced_with_an_iphone(tmp_path: Path) -> None:
    # "Any iPhone beats failing" only holds for an iPhone target. `device_type_identifier` matches
    # simctl's device-type name exactly, and iPad names carry parentheses (e.g. "iPad Pro
    # (12.9-inch) (6th generation)"), so an exact-match miss is ordinary rather than exotic —
    # substituting an iPhone for a missed iPad would finish the run on a layout the scenario was
    # never written against, silently. This must fail loudly instead.
    app = tmp_path / "App.app"
    app.mkdir()
    cfg = (
        'defaults:\n  device: "iPad Pro (12.9-inch) (6th generation)"\n'
        f"targets:\n  s:\n    bundleId: com.x\n    appPath: {app}\n"
        "    xcuitest:\n      testRunner: /nonexistent.xctestrun\n"
    )
    ipad_eff = resolve(load_config(cfg), "s")
    calls, run = _ladder_run([])  # devicetypes lists only "iPhone 17 Pro" — never an iPad match
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    with pytest.raises(
        simctl.DeviceError,
        match=re.escape("no device type matching iPad Pro (12.9-inch) (6th generation)"),
    ):
        env._recover_between_attempts(
            _AttemptFailure("run-ended", "ended"),
            ipad_eff,
            Preconditions(),
            None,
        )
    assert not any(c[2:3] == ["create"] for c in calls)  # no iPhone replacement was minted


# --- the crash retry's replacement rung (BE-0354) --- #
#
# Above BE-0353's forced erase: an erase resets the device's data, and the wedge observed in CI lives
# in the device's capture services, so the erased device came back wedged. The run pipeline asks for a
# replacement, and the next bring-up serves it with the same creation path the vanished-device rung
# uses — leaving the degraded device shut down and, because the pool follows `replaced_device`,
# quarantined.


def _replacement_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[list[list[str]], XcuitestEnvironment, Effective]:
    """A Simulator environment whose simctl can mint a replacement, plus the config to `start` it."""
    app = tmp_path / "App.app"
    app.mkdir()
    _fake_toolchain(monkeypatch)  # Popen, the driver factory, and the discard's group signals
    calls, run = _ladder_run(["UDID"])
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)), app_path=str(app))
    return calls, env, eff


def test_a_requested_replacement_swaps_the_device_before_the_next_bring_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The rung itself: the escalated lease runs on a device that has never run anything, and
    # `replaced_device()` is how the pool learns to re-key everything it holds by udid.
    calls, env, eff = _replacement_env(monkeypatch, tmp_path)
    env.start(eff, Preconditions())
    env.request_device_replacement()
    del calls[:]
    env.start(eff, Preconditions())
    assert env._udid == "UDID-NEW" and env.replaced_device() == "UDID-NEW"
    seq = _verb_seq(calls)
    # The degraded device is shut down before the replacement is minted — the quarantine — and the
    # new device's first boot is paid here rather than inside the next readiness ceiling.
    assert seq.index("shutdown") < seq.index("create") < seq.index("bootstatus")
    assert calls[seq.index("shutdown")][3] == "UDID"


def test_a_replacement_bring_up_never_resumes_the_warm_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A resident on the degraded device cannot serve a scenario on the replacement, and the swap is a
    # cold start by construction — so the escalation must bypass the warm-reuse probe entirely, even
    # though the runner it holds would still answer /health.
    popen_argvs, _, run = _fake_toolchain(monkeypatch)
    app = tmp_path / "App.app"
    app.mkdir()
    _, ladder_run = _ladder_run(["UDID"])
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)), app_path=str(app))
    env.start(eff, Preconditions())
    env.request_device_replacement()
    env._run = ladder_run  # the ladder's simctl can mint a device
    env.start(eff, Preconditions())
    assert len(popen_argvs) == 2  # a second `xcodebuild`, not a warm resume
    assert env._udid == "UDID-NEW"


def test_a_replacement_request_is_served_once_and_then_forgotten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The request names one escalation, not a mode: a later lease that nothing escalated must not
    # keep minting `bajutsu-recovered-*` devices for every scenario that follows.
    calls, env, eff = _replacement_env(monkeypatch, tmp_path)
    env.start(eff, Preconditions())
    env.request_device_replacement()
    env.start(eff, Preconditions())
    del calls[:]
    env.start(eff, Preconditions())
    assert env._udid == "UDID-NEW"  # still on the first replacement
    assert "create" not in _verb_seq(calls)


def test_a_degraded_device_that_refuses_to_shut_down_is_still_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A CoreSimulator wedged enough to refuse `simctl shutdown` is exactly why the run is leaving this
    # device, so failing the escalation on that would replace one loud failure with a less useful one.
    app = tmp_path / "App.app"
    app.mkdir()
    _fake_toolchain(monkeypatch)
    calls, run = _ladder_run(["UDID"], stays_booted_after_shutdown=True)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)), app_path=str(app))
    env.start(eff, Preconditions())
    env.request_device_replacement()
    del calls[:]
    env.start(eff, Preconditions())
    assert env._udid == "UDID-NEW"
    assert "create" in _verb_seq(calls)


def test_a_degraded_device_whose_shutdown_hangs_is_still_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The same reasoning as the refusal above, for the wedge BE-0363 made visible: a `simctl
    # shutdown` that exceeds its deadline propagates out of `Env.shutdown` now, and this site is past
    # the ladder's decision point — a replacement was already confirmed creatable, so raising here
    # would abandon it and fail the run on the very device the escalation exists to leave behind.
    app = tmp_path / "App.app"
    app.mkdir()
    _fake_toolchain(monkeypatch)
    calls, run = _ladder_run(["UDID"])

    def hanging_shutdown(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        if argv[2:4] == ["shutdown", "UDID"]:
            calls.append(argv)
            raise simctl.DeviceTimeout("device operation timed out after 60s: " + " ".join(argv))
        return str(run(argv, env))

    env = XcuitestEnvironment("xcuitest", "UDID", env_run=hanging_shutdown)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)), app_path=str(app))
    env.start(eff, Preconditions())
    env.request_device_replacement()
    del calls[:]
    env.start(eff, Preconditions())
    assert env._udid == "UDID-NEW"
    assert "create" in _verb_seq(calls)


def test_a_degraded_device_whose_discard_hangs_is_still_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The escalation discards the runner before it swaps the device, and on this device a hung
    # `terminate` is the wedge the escalation exists to abandon. Raising there would also be
    # unrecoverable: the request is consumed at the top of `start`, so the retry lease would take the
    # ordinary path and hang on the same call.
    app = tmp_path / "App.app"
    app.mkdir()
    _fake_toolchain(monkeypatch)
    calls, run = _ladder_run(["UDID"])

    def hanging_terminate(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        if argv[2:4] == ["terminate", "UDID"]:
            calls.append(argv)
            raise simctl.DeviceTimeout("device operation timed out after 60s: " + " ".join(argv))
        return str(run(argv, env))

    env = XcuitestEnvironment("xcuitest", "UDID", env_run=hanging_terminate)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)), app_path=str(app))
    env.start(eff, Preconditions())
    env.request_device_replacement()
    del calls[:]
    with caplog.at_level("WARNING"):
        env.start(eff, Preconditions())
    assert env._udid == "UDID-NEW"
    assert "create" in _verb_seq(calls)
    # Absorbed, never silent: this is the one caller whose own `shutdown` may still return, so a
    # suppressed timeout here would be the only place the wedge left no trace at all.
    assert "discarding the runner on Simulator UDID" in caplog.text


def test_a_replacement_bring_up_drops_the_erase_it_was_asked_to_carry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The crash retry keeps forcing the erase, because deciding otherwise would mean predicting this
    # branch — an attempt whose request never reached this instance would then get neither remedy. So
    # the drop happens here, where the swap actually happened: a device just created has nothing to
    # erase, and honoring the precondition would pay a second shutdown-and-boot cycle for no change.
    calls, env, eff = _replacement_env(monkeypatch, tmp_path)
    env.start(eff, Preconditions())
    env.request_device_replacement()
    del calls[:]
    env.start(eff, Preconditions(erase=True))
    assert env._udid == "UDID-NEW"
    seq = _verb_seq(calls)
    # The one shutdown is the degraded device's quarantine, before the create; nothing erases the
    # replacement afterwards.
    assert "erase" not in seq[seq.index("create") :]


def test_a_replacement_that_cannot_be_created_leaves_the_device_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A blank device with no app to install has nothing to launch, so the rung refuses — and it must
    # refuse *before* shutting the degraded device down, or the caller's fallback would be left
    # retrying onto a device this escalation turned off on its way to failing.
    _fake_toolchain(monkeypatch)
    calls, run = _ladder_run(["UDID"])
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))  # no appPath
    env.start(eff, Preconditions())
    env.request_device_replacement()
    del calls[:]
    # The wording names *this* rung, so an operator knows which one ran, and stays neutral about
    # which signal selected it — the stall-triggered path escalates with no erase ever forced.
    with pytest.raises(simctl.DeviceError, match="needs replacing after a crash"):
        env.start(eff, Preconditions())
    assert "shutdown" not in _verb_seq(calls) and "create" not in _verb_seq(calls)


def test_a_real_device_start_clears_a_replacement_request_without_serving_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A real device is powered on out of band and has no simctl to mint through, so the request must
    # not survive into a later `start` that could act on it.
    _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=lambda argv, e: "")
    cfg = (
        "targets:\n  s:\n    bundleId: com.x\n"
        "    xcuitest:\n      deviceType: device\n"
        f"      testRunner: {_write_runner(tmp_path)}\n"
    )
    device_eff = resolve(load_config(cfg), "s")
    env.request_device_replacement()
    env.start(device_eff, Preconditions())
    assert env._replacement_requested is False
    assert env.replaced_device() is None


def test_a_recovery_that_overran_its_bound_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # A device that takes longer than the bound to come back is not coming back, and a retry funded
    # out of the remaining job time would only fail later.
    monkeypatch.setenv("BAJUTSU_XCUITEST_RECOVERY_TIMEOUT", "0")
    _calls, run = _ladder_run(["UDID"])
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    with pytest.raises(simctl.DeviceError, match="recovery exceeded"):
        env._recover_between_attempts(
            _AttemptFailure("run-ended", "ended"),
            _eff_for_ladder(),
            Preconditions(),
            None,
        )


def test_recovery_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAJUTSU_XCUITEST_RECOVERY_TIMEOUT", "42")
    assert _recovery_timeout() == 42.0
    monkeypatch.setenv("BAJUTSU_XCUITEST_RECOVERY_TIMEOUT", "not-a-number")
    assert _recovery_timeout() == _RECOVERY_TIMEOUT  # an unparseable override keeps the default


def test_the_recovery_bound_excludes_the_unbounded_reprep(monkeypatch: pytest.MonkeyPatch) -> None:
    # A slow-but-successful re-prep (the erase/locale-pin/install cycle `_finish_repair` runs) must
    # not fail a run whose device demonstrably came back: `_check_recovery_budget` times only the
    # repair proper — the shutdown/boot/bootstatus this rung runs before handing off to it.
    monkeypatch.setenv("BAJUTSU_XCUITEST_RECOVERY_TIMEOUT", "5")
    clock = {"t": 0.0}
    boots_seen = {"n": 0}
    _calls, base_run = _ladder_run(["UDID"])

    def run(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        out = base_run(argv, env)
        if argv[2:3] == ["boot"]:
            boots_seen["n"] += 1
            if boots_seen["n"] == 2:  # the boot inside _finish_repair, not the repair itself
                clock["t"] += 1000.0  # far past the 5s bound, were it ever counted here
        return out

    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    recovery = env._recover_between_attempts(
        _AttemptFailure("run-ended", "the xctest run ended after the app launch timed out"),
        _eff_for_ladder(),
        Preconditions(),
        None,
    )
    assert recovery is not None and recovery.fresh_budget == _runner_startup_timeout()
    assert boots_seen["n"] == 2  # confirms _finish_repair actually ran rather than being skipped


def test_a_rebooted_device_earns_the_full_cold_ceiling_even_on_a_tight_respawn() -> None:
    # A reboot ends the same way an erase does — a genuine first-boot state, fresh CoreSimulator
    # caches, restarted SpringBoard, no XCTest host run this boot — the state `_spawn_cold`'s own
    # erase-path precedent already gives the full cold ceiling regardless of respawn history. A
    # rebooted respawn earns the same, not the tighter respawn ceiling the failing attempt started on.
    _calls, run = _ladder_run(["UDID"])
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    recovery = env._recover_between_attempts(
        _AttemptFailure("run-ended", "ended"),
        _eff_for_ladder(),
        Preconditions(),
        None,
    )
    assert recovery is not None and recovery.fresh_budget == _runner_startup_timeout()


def test_a_replacement_device_earns_the_full_cold_ceiling(tmp_path: Path) -> None:
    # A device that has never run anything is a genuine first bring-up: its very first
    # `xcodebuild test-without-building` pays the whole spin-up.
    app = tmp_path / "App.app"
    app.mkdir()
    _calls, run = _ladder_run([])  # the leased device is gone, so a replacement is created
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    recovery = env._recover_between_attempts(
        _AttemptFailure("run-ended", "ended"),
        _eff_for_ladder(app_path=str(app)),
        Preconditions(),
        None,
    )
    assert recovery is not None and recovery.fresh_budget == _runner_startup_timeout()


@pytest.mark.parametrize(
    ("present", "kind"),
    [
        (["UDID"], "process-exit"),  # the rung that deliberately leaves a booted device alone
        ([], "run-ended"),  # ... and the replace rung, for contrast
    ],
)
def test_the_recovery_bound_covers_a_rung_that_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, present: list[str], kind: str
) -> None:
    # The probe that opens the ladder is itself a subprocess, so a host slow enough to blow the bound
    # merely answering `simctl list` must fail the run whatever the rung then decided — otherwise the
    # "whole ladder is bounded" contract holds only on the paths that happen to repair something.
    monkeypatch.setenv("BAJUTSU_XCUITEST_RECOVERY_TIMEOUT", "0")
    app = tmp_path / "App.app"
    app.mkdir()
    _calls, run = _ladder_run(list(present))
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    with pytest.raises(simctl.DeviceError, match="recovery exceeded"):
        env._recover_between_attempts(
            _AttemptFailure(kind, "why"),  # type: ignore[arg-type]
            _eff_for_ladder(app_path=str(app)),
            Preconditions(),
            None,
        )


def test_an_unknown_probe_is_still_reported_within_the_bound() -> None:
    # The same do-nothing rung, on a host answering promptly: it reports its note and no fresh budget
    # rather than failing, so the bound above is what distinguishes a sick host from a quiet one.
    def run(argv: list[str], env: Mapping[str, str] | None = None) -> str:
        raise OSError("xcrun unavailable")

    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    recovery = env._recover_between_attempts(
        _AttemptFailure("never-ready", "health never ready"),
        _eff_for_ladder(),
        Preconditions(),
        None,
    )
    assert recovery.fresh_budget is None and "could not probe" in recovery.note


def test_a_replacements_name_keeps_its_capability_token_and_report_row(tmp_path: Path) -> None:
    # Two consumers read a device's name as its human model, and a name of ours that dropped the model
    # would break both silently: `serve`'s capability inventory takes the `iphone` / `ipad` class token
    # out of it by substring (a missing token means the hosted router never leases the device for a job
    # that asked for `iphone`), and the report renders it as the device row.
    from bajutsu.serve.capabilities import _device_class_token

    app = tmp_path / "App.app"
    app.mkdir()
    # A udid longer than 8 characters, so a truncated suffix and the full one are distinguishable —
    # the point of the suffix is letting an operator match a "UDID-... vanished" log line against
    # `simctl list` afterwards, which an 8-character prefix cannot support.
    old_udid = "2A6DC5A9-CE8C-4BC5-959D-F98D5F4BD9AA"
    calls, run = _ladder_run([])
    env = XcuitestEnvironment("xcuitest", old_udid, env_run=run)
    env._recover_between_attempts(
        _AttemptFailure("run-ended", "ended"),
        _eff_for_ladder(app_path=str(app)),
        Preconditions(),
        None,
    )
    created = next(c for c in calls if c[2:3] == ["create"])
    name = created[3]
    assert _device_class_token(name) == "iphone"
    assert "iPhone" in name  # the report's device row shows a model a reader recognizes
    assert (
        f"bajutsu-recovered-{old_udid}" in name
    )  # the *whole* vanished udid, not a truncated prefix


# --- copying the crashed runner's own evidence into the failed scenario (BE-0421) --- #
#
# A scenario whose crash-recovery retries all exhausted used to report only that the runner died. Two
# pieces of direct evidence exist on disk at that moment — the runner's captured output, and the
# `.ips` report macOS writes for a process that faulted — and the pipeline now copies both into that
# scenario's own evidence directory. Both halves are snapshotted where the crash is *observed*, since
# the crashed lease is back in the pool before the retry loop gives up.

_XCUITEST_MODULE = "bajutsu.common.platform_lifecycle.environments.xcuitest.xcuitest_environment"

_IPS_HEADER = b'{"app_name":"xcodebuild","bug_type":"309"}'


def _write_ips(directory: Path, name: str, *, pid: int | None, when: float) -> Path:
    """A stub `.ips` crash report in *directory*, dated *when*, naming *pid* unless it is None."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = b'{"exception":{"signal":"SIGSEGV"}}' if pid is None else b'{"pid":%d}' % pid
    path = directory / name
    path.write_bytes(_IPS_HEADER + b"\n" + payload)
    os.utime(path, (when, when))
    return path


def _crashed_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[XcuitestEnvironment, Path]:
    """An environment with a spawned-then-crashed runner, its capture and report store under *tmp_path*.

    Returned before the discard, so each test writes the reports it wants the deferred sweep to find
    and then calls `_discard_runner()` itself — the point where the snapshot is taken.
    """
    _, _, run = _fake_toolchain(monkeypatch)
    monkeypatch.delenv("BAJUTSU_XCUITEST_RUNNER_LOG", raising=False)
    monkeypatch.setattr(
        f"{_XCUITEST_MODULE}._DEFAULT_RUNNER_LOG_DIR",
        tmp_path / "runner-logs",
    )
    reports = tmp_path / "DiagnosticReports"
    monkeypatch.setattr(f"{_XCUITEST_MODULE}._diagnostic_reports_dir", lambda: reports)
    _patch_group_signals(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert env._runner_log is not None
    env._runner_log.write_text("first line\nthe crash reason\n", encoding="utf-8")
    assert env._runner_proc is not None
    env._runner_proc.alive = False  # type: ignore[attr-defined]  # the runner crashed mid-run
    return env, reports


def test_a_mid_run_crash_copies_the_runner_log_and_the_matching_crash_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The observable outcome of BE-0421: both pieces of evidence reach the caller as named bytes, and
    # the report is found on the *deferred* sweep — written here after the discard has already run,
    # which is exactly the ordering `ReportCrash`'s asynchronous write imposes.
    env, reports = _crashed_runner(monkeypatch, tmp_path)
    log_name = env._runner_log.name if env._runner_log is not None else ""
    pid = env._runner_proc.pid if env._runner_proc is not None else 0
    spawned_at = env._runner_spawned_at
    env._discard_runner()
    _write_ips(reports, "xcodebuild-2026-09-12-103045.ips", pid=pid, when=spawned_at + 1)
    captured = dict(env.take_crash_snapshot()())
    assert captured[log_name] == b"first line\nthe crash reason\n"
    assert b'"pid":%d' % pid in captured["xcodebuild-2026-09-12-103045.ips"]


def test_a_crash_report_predating_the_spawn_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A macOS host accumulates reports, so an `xcodebuild` invocation from an hour ago leaves one that
    # names the same process and says nothing about this crash. The frozen spawn timestamp is what
    # keeps it out.
    env, reports = _crashed_runner(monkeypatch, tmp_path)
    log_name = env._runner_log.name if env._runner_log is not None else ""
    pid = env._runner_proc.pid if env._runner_proc is not None else 0
    spawned_at = env._runner_spawned_at
    env._discard_runner()
    _write_ips(reports, "xcodebuild-old.ips", pid=pid, when=spawned_at - 3600)
    assert [name for name, _ in env.take_crash_snapshot()()] == [log_name]  # the runner log alone


def test_a_crash_report_naming_a_different_process_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A multi-worker host runs several `xcodebuild` processes at once, so name and time alone can match
    # a sibling worker's fault. The frozen pid separates them.
    env, reports = _crashed_runner(monkeypatch, tmp_path)
    pid = env._runner_proc.pid if env._runner_proc is not None else 0
    spawned_at = env._runner_spawned_at
    env._discard_runner()
    _write_ips(reports, "xcodebuild-sibling.ips", pid=pid + 1, when=spawned_at + 1)
    assert "xcodebuild-sibling.ips" not in dict(env.take_crash_snapshot()())


def test_a_crash_report_whose_json_cannot_be_read_is_still_taken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A report format this cannot parse is precisely when the raw file is worth having, so an
    # unreadable header degrades to the name-and-time match rather than dropping the evidence.
    env, reports = _crashed_runner(monkeypatch, tmp_path)
    spawned_at = env._runner_spawned_at
    env._discard_runner()
    path = reports / "xcodebuild-unparsable.ips"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not json at all\nnor here either\n")
    os.utime(path, (spawned_at + 1, spawned_at + 1))
    assert "xcodebuild-unparsable.ips" in dict(env.take_crash_snapshot()())


def test_the_crash_report_sweep_is_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # One scenario's evidence must stay bounded however hard the host is crash-looping, mirroring
    # BE-0361's own per-capture caps. The newest reports are the ones kept.
    env, reports = _crashed_runner(monkeypatch, tmp_path)
    now = env._runner_spawned_at + 1
    env._discard_runner()
    for i in range(xcuitest_env_impl._MAX_CRASH_REPORTS + 3):
        _write_ips(reports, f"xcodebuild-{i}.ips", pid=None, when=now + i)
    ips = [name for name, _ in env.take_crash_snapshot()() if name.endswith(".ips")]
    assert len(ips) == xcuitest_env_impl._MAX_CRASH_REPORTS
    assert ips[0] == f"xcodebuild-{xcuitest_env_impl._MAX_CRASH_REPORTS + 2}.ips"  # newest first


def test_a_crash_with_no_report_still_yields_the_runner_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The common shape: the runner's channel stopped answering without the host process faulting, so
    # macOS wrote nothing. The capture is best-effort per entry, never all-or-nothing — and a host that
    # has no report store at all (any platform but macOS) lists as empty here, not as a failure.
    env, reports = _crashed_runner(monkeypatch, tmp_path)
    log_name = env._runner_log.name if env._runner_log is not None else ""
    env._discard_runner()
    assert not reports.exists()
    assert [name for name, _ in env.take_crash_snapshot()()] == [log_name]


def test_a_crash_report_that_cannot_be_read_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An entry the sweep matched but cannot read costs that one report, never the runner log beside it:
    # this whole capture exists to explain a failure, so it must not become one.
    env, reports = _crashed_runner(monkeypatch, tmp_path)
    pid = env._runner_proc.pid if env._runner_proc is not None else 0
    spawned_at = env._runner_spawned_at
    env._discard_runner()
    (reports / "xcodebuild-unreadable.ips").mkdir(parents=True)  # matches the glob, refuses a read
    _write_ips(reports, "xcodebuild-fine.ips", pid=pid, when=spawned_at + 1)
    captured = dict(env.take_crash_snapshot()())
    assert "xcodebuild-fine.ips" in captured
    assert "xcodebuild-unreadable.ips" not in captured


def test_a_crash_whose_capture_is_unreadable_yields_nothing_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A spawn that failed before writing a byte leaves no capture to read. A diagnostic must never be
    # what fails the discard that is already reporting a crash.
    env, _reports = _crashed_runner(monkeypatch, tmp_path)
    assert env._runner_log is not None
    env._runner_log.unlink()
    env._discard_runner()
    assert env.take_crash_snapshot()() == []


def test_a_cold_spawn_failure_discard_leaves_a_real_crashs_evidence_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `warn_on_crash=False` takes the same `exited is not None` branch for a failure that is explicitly
    # not a mid-run crash — the cold-spawn-failure discard. Gating on `exited` rather than on `crashed`
    # would let such a discard, mid-retry-loop, overwrite the crash evidence with its own.
    env, _reports = _crashed_runner(monkeypatch, tmp_path)
    env._discard_runner()
    crashed = env.take_crash_snapshot()
    assert crashed()  # the real crash was captured, and this lease now owns it
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert env._runner_proc is not None
    env._runner_proc.alive = False  # type: ignore[attr-defined]
    env._discard_runner(warn_on_crash=False, keep_log=True)
    assert (
        env.take_crash_snapshot()() == []
    )  # the cold-spawn failure contributed nothing of its own


def test_a_second_crash_on_one_environment_belongs_to_the_next_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Ownership moves at `take_crash_snapshot()`, not at the crash itself, so a second crash on the
    # same warm environment can never reach the first scenario's evidence: the first lease has already
    # taken its copy by the time a second scenario gets a chance to lease the device. Not calling
    # `take_crash_snapshot()` between the two crashes (unlike `test_taking_the_snapshot_moves_it_off_
    # the_shared_environment`) is the point — it pins that the *cache itself* holds only the second
    # crash's evidence once the second has happened, regardless of whether anyone read the first.
    env, _reports = _crashed_runner(monkeypatch, tmp_path)
    env._discard_runner()
    env.start(_sim_eff(test_runner=str(_write_runner(tmp_path))), Preconditions())
    assert env._runner_log is not None
    env._runner_log.write_text("the second scenario's crash\n", encoding="utf-8")
    assert env._runner_proc is not None
    env._runner_proc.alive = False  # type: ignore[attr-defined]
    env._discard_runner()
    assert dict(env.take_crash_snapshot()()).popitem()[1] == b"the second scenario's crash\n"


def test_an_environment_that_never_crashed_captures_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A healthy teardown is not a crash, so a passing scenario's environment has nothing to hand over
    # — and every other platform's environment answers the same way by default.
    _, _, run = _fake_toolchain(monkeypatch)
    env = XcuitestEnvironment("xcuitest", "UDID", env_run=run)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    driver = env.start(eff, Preconditions())
    env.teardown(driver, eff)
    assert env.take_crash_snapshot()() == []


def test_a_report_that_vanishes_mid_listing_is_skipped(tmp_path: Path) -> None:
    # `DiagnosticReports` is a live directory: `ReportCrash` writes into it while the sweep reads, and a
    # dangling entry left by a report the system has already rotated away must cost that one entry
    # rather than the whole lookup.
    reports = tmp_path / "DiagnosticReports"
    kept = _write_ips(reports, "xcodebuild-kept.ips", pid=None, when=time.time())
    (reports / "xcodebuild-gone.ips").symlink_to(reports / "never-existed.ips")
    assert _reports_since(reports, "xcodebuild-*.ips", 0.0) == [kept]


def test_a_crash_with_no_capture_at_all_still_identifies_its_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Capture is on by default, but the field holding it is nullable — a spawn discarded before
    # `_open_runner_output` ran has none. The two halves are independent, so the report is still found.
    env, reports = _crashed_runner(monkeypatch, tmp_path)
    pid = env._runner_proc.pid if env._runner_proc is not None else 0
    spawned_at = env._runner_spawned_at
    env._runner_log = None
    env._snapshot_crash_artifacts()
    _write_ips(reports, "xcodebuild-only.ips", pid=pid, when=spawned_at + 1)
    assert [name for name, _ in env.take_crash_snapshot()()] == ["xcodebuild-only.ips"]


def test_releasing_a_crashed_lease_captures_before_the_device_goes_back_to_the_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The real path a crashed scenario takes, and the one the whole eager-snapshot argument rests on:
    # the pipeline's only action on a mid-run crash is to release the lease, which reaches `end_lease`
    # — not a discard. The discard that would otherwise notice runs at the *next* bring-up, by which
    # time a concurrent worker's lease can already have respawned over the crashed runner's state, and
    # a run with no retries left never reaches one at all.
    env, _reports = _crashed_runner(monkeypatch, tmp_path)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    log_name = env._runner_log.name if env._runner_log is not None else ""
    env.end_lease(FakeDriver([]), eff)
    assert [name for name, _ in env.take_crash_snapshot()()] == [log_name]


def test_a_runner_that_lingers_past_its_ended_test_run_is_captured_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The dominant CI shape, and the one a process check alone cannot see: XCTest ends the run and
    # `xcodebuild` lives on, so `poll()` keeps answering None while `_runner_alive` has already
    # declared the crash off the capture's own marker. Capturing only the exited case would miss
    # exactly the failure `docs/ci.md` opens by describing.
    env, _reports = _crashed_runner(monkeypatch, tmp_path)
    assert env._runner_proc is not None and env._runner_log is not None
    env._runner_proc.alive = True  # type: ignore[attr-defined]  # the leader outlives its test run
    env._runner_log.write_text(
        "Timed out while requesting screenshot\nTest Suite 'All tests' failed\n", encoding="utf-8"
    )
    assert env._runner_alive() is False  # the crash is declared off the marker, not off the process
    log_name = env._runner_log.name
    env.end_lease(FakeDriver([]), _sim_eff(test_runner=str(_write_runner(tmp_path))))
    captured = dict(env.take_crash_snapshot()())
    assert "Test Suite 'All tests' failed" in captured[log_name].decode()


def test_taking_the_snapshot_moves_it_off_the_shared_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The environment outlives the scenario — the pool keeps it warm per device — so evidence left on
    # it would be readable by, and erasable by, whichever scenario leases the device next. Taking it
    # moves ownership to the releasing lease: the thunk keeps answering, and a second take (the next
    # lease's own release, crash or no crash) finds nothing to hand over or to destroy.
    env, _reports = _crashed_runner(monkeypatch, tmp_path)
    eff = _sim_eff(test_runner=str(_write_runner(tmp_path)))
    env.end_lease(FakeDriver([]), eff)
    first = env.take_crash_snapshot()
    assert first()  # this lease's own crash, now this lease's to keep
    env.start(eff, Preconditions())  # the next scenario leases the same warm environment
    env.end_lease(FakeDriver([]), eff)  # and ends healthy
    assert env.take_crash_snapshot()() == []  # it inherits nothing
    assert first()  # and the crashed scenario can still read its own, after that healthy lease


def test_the_report_store_is_located_on_demand_and_tolerates_an_unresolvable_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Resolved per lookup rather than at import, because this module loads on every platform — the web
    # and Android lanes included — and `Path.home()` raises on a container started against a uid with
    # no passwd entry and no HOME. Evaluating it at import would make that an `import bajutsu` failure
    # on a host that has no crash-report store to read in the first place.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert _diagnostic_reports_dir() == tmp_path / "Library" / "Logs" / "DiagnosticReports"

    def no_home(cls: type[Path]) -> Path:
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(Path, "home", classmethod(no_home))
    assert _diagnostic_reports_dir() is None  # reads as "no reports", never as a failure
