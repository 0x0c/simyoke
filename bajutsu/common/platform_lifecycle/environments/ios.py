"""The device-style lifecycle base shared by the simctl family.

`_DeviceEnvironment` holds every lease-shaping method the simctl family (XCUITest, fake) share; each
concrete environment adds only its own `start` sequence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from bajutsu.common.backend_cli import simctl
from bajutsu.common.config import Effective, require_ios
from bajutsu.common.drivers import base
from bajutsu.common.evidence import intervals
from bajutsu.common.evidence.network import Collector
from bajutsu.common.orchestrator import DeviceControl, RelaunchFn
from bajutsu.common.platform_lifecycle import readiness
from bajutsu.common.platform_lifecycle.device_control import device_control
from bajutsu.common.platform_lifecycle.relaunchers import device_relauncher
from bajutsu.common.scenario import Scenario
from bajutsu.crawl import AliveCheck, ClearBlocking, Recover, Reset


class _DeviceEnvironment:
    """The device-style lifecycle: the iOS Simulator (`simctl`) backend and the fake test backend,
    which mimics the same shape without a real device.

    Only `start` differs between them — the fake runs no device sequence — so every lease-shaping
    method (catalog, relaunch, control, teardown, the external-receiver network strategy) lives here.
    """

    def __init__(
        self,
        actuator: str,
        udid: str,
        env_run: simctl.RunFn = simctl.real_run,
    ) -> None:
        self._actuator = actuator
        self._udid = udid
        self._run = env_run

    def resolve_device(self, udid: str) -> str:
        return simctl.resolve_udid(udid, self._run)

    def prestarted_intervals(self) -> list[intervals.Interval]:
        """Interval captures begun during `start()`, before the app launched, for the sink to adopt.

        Always empty here: no subclass starts a capture before the app launches. XCUITest records
        video on demand instead; the fake and live-WebDriver routes record none at all
        (`captures_video`). See `records_video_up_front`.
        """
        return []

    def captures_video(self) -> bool:
        return True  # a simctl-backed device records a scenario-wide video via a simctl interval

    def device_catalog(self) -> dict[str, dict[str, str]]:
        return simctl.device_catalog(self._run)

    def observes_network_via_driver(self) -> bool:
        return False  # the app reports to an external collector via BAJUTSU_COLLECTOR

    def mirrors_collector_port_on_device(self) -> bool:
        return False  # the Simulator shares the Mac's loopback: nothing binds the port device-side

    def records_video_up_front(self) -> bool:
        return False  # simctl records on demand

    def hook_collector(self, driver: base.Driver, scenario: Scenario) -> Collector:
        raise NotImplementedError("device backends observe network via an external receiver")

    def bridge_collector(self, port: int) -> Callable[[], None]:  # noqa: ARG002  # Environment shape
        return lambda: None  # the Simulator shares the Mac's loopback; nothing to bridge

    def relauncher(
        self,
        eff: Effective,
        scenario: Scenario,
        driver: base.Driver,
        *,
        extra_env: Mapping[str, str] | None = None,
    ) -> RelaunchFn:
        return device_relauncher(self._udid, self._run, extra_env)(eff, scenario, driver)

    def controller(self, eff: Effective) -> DeviceControl | None:
        return device_control(self._udid, require_ios(eff).bundle_id, self._run)

    def teardown(self, driver: base.Driver, eff: Effective) -> None:  # noqa: ARG002  # Environment shape
        simctl.Env(self._udid, run=self._run).terminate(require_ios(eff).bundle_id)

    def has_reusable_resident(self) -> bool:
        # fake spawns no resident to amortize; XcuitestEnvironment overrides (BE-0291).
        return False

    def request_device_replacement(self) -> None:
        # Only the XCUITest Simulator lifecycle can mint a device (it overrides this); fake brings no
        # device up, and the live WebDriver route drives a reserved device it does not own, so both
        # ignore the request and keep the bare respawn the crash retry already gives them (BE-0354).
        return None

    def replaced_device(self) -> str | None:
        # Only the XCUITest Simulator lifecycle replaces a vanished device (it overrides this); fake
        # brings no device up, so the leased udid is always the one it ran on.
        return None

    def take_crash_snapshot(self) -> Callable[[], list[tuple[str, bytes]]]:
        # Only the XCUITest Simulator lifecycle owns a runner process that can crash and leave
        # evidence behind (it overrides this); fake spawns none, and the live WebDriver route drives a
        # device whose automation process it does not own. A real, callable no-op thunk rather than a
        # null, the same shape `bridge_collector` above already returns (BE-0421).
        return list

    def end_lease(self, driver: base.Driver, eff: Effective) -> None:
        # No warm resident here, so a lease's end is just its full teardown (BE-0291).
        self.teardown(driver, eff)

    def has_devices(self) -> bool:
        return True

    def plan_lanes(self, udid_arg: str, workers: int) -> list[str]:
        udids = [self.resolve_device(u.strip()) for u in udid_arg.split(",") if u.strip()]
        return udids[: max(1, min(workers, len(udids)))]

    def crawl_reset(self, eff: Effective) -> Reset:
        # Return to a clean start the way `run` reaches any state: relaunch (not a full erase) so each
        # frontier revisit stays fast; the engine then replays the shortest path from the entry.
        e = simctl.Env(self._udid, run=self._run)
        bundle_id = require_ios(eff).bundle_id

        def reset(driver: base.Driver) -> None:
            e.terminate(bundle_id)
            e.launch(bundle_id, [*eff.launch_args, *simctl.locale_args(eff.locale)], eff.launch_env)
            readiness.await_ready(driver, ready_sel=eff.ready_when, id_namespaces=eff.id_namespaces)

        return reset

    def crawl_aliveness(self) -> AliveCheck | None:
        return None  # the engine reads the accessibility tree for device crash detection

    def crawl_recover(self) -> Recover | None:
        return None  # no in-lane recovery: a wedged device surfaces as a DeviceError

    def crawl_dialog_clearer(self) -> ClearBlocking | None:
        return None  # OS prompts are handled by the optional alert guard, wired by the CLI
