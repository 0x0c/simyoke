"""The `run` lease surface: produce a freshly launched app and drive its per-lease shape."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from bajutsu.common.config import Effective
from bajutsu.common.drivers import base
from bajutsu.common.evidence import intervals
from bajutsu.common.evidence.network import Collector
from bajutsu.common.orchestrator import DeviceControl, RelaunchFn
from bajutsu.common.scenario import Preconditions, Scenario


@runtime_checkable
class RunEnvironment(Protocol):
    """The `run` lease surface: produce a freshly-launched app and drive its per-lease shape.

    `start` owns the entire per-run startup for a platform, so the caller need not know whether that
    means a `simctl` device sequence or a fresh browser context — it gets back a driver bound to the
    launched app (not yet polled for readiness; the runner does that). `permissions` (BE-0276) is
    applied before the app process starts, so a known permission's runtime prompt never appears; a
    platform without a mechanism for it (web, fake) raises `UnsupportedAction` if asked to apply
    one — preflight already rejects a scenario naming an unsupported service before `start` is ever
    called, so this is only the runtime backstop for a caller that bypasses it. The remaining methods describe
    the differences the pool used to branch on the actuator name for: how the device handle resolves,
    how network is observed, whether video can be captured, whether video must be wired before launch,
    and the per-scenario relaunch / device control / teardown. This is the narrower surface the `run`
    pipeline (`runner/pool.py`, `runner/launch.py`) holds; the module docstring's "not applicable"
    contract governs how a platform declines each method.
    """

    def start(
        self,
        eff: Effective,
        pre: Preconditions,
        *,
        extra_env: Mapping[str, str] | None = None,
        record_video_dir: Path | None = None,
        permissions: Mapping[str, str] | None = None,
    ) -> base.Driver: ...

    def resolve_device(self, udid: str) -> str:
        """Resolve *udid* to a concrete device handle for this platform.

        The seam that replaces the CLI/doctor layer testing the actuator string to pick a resolver
        (BE-0256): the iOS family resolves through `simctl`, Android through `adb`, and web has no
        device so it returns *udid* unchanged. The environment already *is* its platform, so it needs
        no actuator argument — that is exactly the string this method removes from its callers.
        """

    def captures_video(self) -> bool:
        """Whether `record` captures a scenario-wide screen video while authoring on this platform.

        A distinct axis from `records_video_up_front` (which asks *when* capture is wired, not
        *whether* the platform can capture at all): the simctl-backed device platform (xcuitest)
        records via a simctl interval, web captures by other means during replay, and the fake
        backend has no device to record. The `record` command reads this instead of a
        per-actuator name test, which historically special-cased the coordinate backend and silently
        never captured under xcuitest (BE-0256).
        """

    def device_catalog(self) -> dict[str, dict[str, str]]:
        """Static device metadata (model / OS) keyed by udid.

        Returns `{}` for a platform with no device (web) — a first-class "no devices", not an
        unimplemented stub; the caller always invokes this and reads the empty map as the answer.
        """

    def observes_network_via_driver(self) -> bool:
        """Whether network is observed by hooking the live driver (web) rather than an external
        receiver the app reports to (the device backends). Gates `hook_collector`."""

    def records_video_up_front(self) -> bool:
        """Whether video capture must be wired before launch — so the app's cold start is recorded —
        rather than on demand after launch. True for web (its context records at creation) and for
        Android (it starts recording before the app launches). XCUITest records on demand instead,
        and the fake and live-WebDriver routes capture no video at all. Gates `start`'s
        `record_video_dir` handling, and thus whether `prestarted_intervals` can be non-empty."""

    def prestarted_intervals(self) -> list[intervals.Interval]:
        """Interval captures `start` began before the app launched, for the sink to adopt and finalize.

        Android starts the scenario video before launch so the cold start is recorded, then hands
        the running capture over here for the sink to adopt (`intervals.adopt`) rather than start a
        fresh one on demand. Empty on a backend that records on demand instead (XCUITest), captures
        no video at all (the fake and live-WebDriver routes), or wires its up-front recording
        through the driver instead (web binds it to the browser context)."""

    def hook_collector(self, driver: base.Driver, scenario: Scenario) -> Collector:
        """The page-hooked collector for a driver-observed platform, with this scenario's mocks wired
        in.

        Gated raise: the runner calls this *only when* `observes_network_via_driver()` is `True`, so a
        platform that returns `False` there may leave this raising `NotImplementedError` — the check
        makes the raise unreachable. This is the only Protocol method permitted to raise.
        """

    def bridge_collector(self, port: int) -> Callable[[], None]:
        """Make the host's network collector on `127.0.0.1:port` reachable from the leased device.

        No-op implementation: the return type carries no null, so a platform with nothing to bridge
        returns a real, callable no-op thunk (`lambda: None`) rather than `None` or a raise.

        Called only on the external-receiver path (a `NetworkCollector` was pre-started and its URL
        injected), right before launch, and the returned thunk is invoked when the lease releases. The
        iOS Simulator shares the Mac's loopback, so most platforms need nothing and return a no-op; the
        Android emulator's loopback is its own, so `AndroidEnvironment` tunnels the port back with
        `adb reverse` (BE-0283). Returns the teardown thunk (removes the tunnel), never `None`.
        """

    def mirrors_collector_port_on_device(self) -> bool:
        """Whether `bridge_collector` requires the *device* to bind the collector's host port too.

        True only for the Android emulator, whose `adb reverse tcp:<port> tcp:<port>` mirrors the one
        number onto the guest — so the port must be free on both sides, and the pool draws it from a
        reserved band (`NetworkCollector.start_bridgeable`) instead of letting the OS pick. Every
        platform that shares the host's loopback (the iOS Simulator) bridges nothing and binds
        nothing on the device, so it keeps the OS-chosen port and is unaffected by the band.
        """

    def relauncher(
        self,
        eff: Effective,
        scenario: Scenario,
        driver: base.Driver,
        *,
        extra_env: Mapping[str, str] | None = None,
    ) -> RelaunchFn:
        """The scenario's `relaunch` function (app restart on a device; re-navigate on web)."""

    def controller(self, eff: Effective) -> DeviceControl | None:
        """Device control for the leased device.

        Returns `None` on a platform without one (web) — a first-class "no device control" the runner
        interprets, not an unimplemented stub.
        """

    def teardown(self, driver: base.Driver, eff: Effective) -> None:
        """Per-release app teardown: terminate the app (device) or close the browser (web).

        The full teardown, including any resident process the environment owns (XCUITest's
        `xcodebuild` runner). The pool calls this at the moments it owns runner termination
        (BE-0291): a run-set's end, an actuator switch on a device, and a warm resident that failed
        to resume — as well as the ordinary per-lease release of a platform with no warm resident.
        """

    def has_reusable_resident(self) -> bool:
        """Whether `start` left a resident process the pool should keep warm across leases (BE-0291).

        A predicate read *after* `start`: `True` means this environment holds a resident (XCUITest's
        `xcodebuild test-without-building` runner on a Simulator) whose cold startup is worth
        amortizing, so the pool caches this environment for the device and reuses it — a later
        same-actuator lease resumes the resident via `start` (app relaunch only) instead of spawning
        a new one, and the lease releases through `end_lease` rather than `teardown`. Default `False`
        (no resident to reuse — every platform but the Simulator XCUITest backend), so the pool's
        cache never activates and the per-lease teardown is unchanged.
        """

    def end_lease(self, driver: base.Driver, eff: Effective) -> None:
        """Release one lease while keeping a warm resident alive (BE-0291).

        Called instead of `teardown` when the pool is keeping this environment's resident warm for
        the next lease on the device: it does the per-scenario cleanup (terminate the app) but leaves
        the resident running. Default: delegate to `teardown` — a platform with no warm resident
        (`has_reusable_resident()` is `False`) is never kept warm, so its `end_lease` and `teardown`
        are the same release.
        """

    def request_device_replacement(self) -> None:
        """Ask this environment to run its next `start` on a replacement device (BE-0354).

        The rung above the crash retry's forced erase: an erase resets the device's data, but a
        Simulator whose capture services have wedged comes back wedged, so the run pipeline escalates
        to a device that has never run anything. Recorded rather than acted on, because the swap must
        land on the *next* bring-up, where `replaced_device` then reports it and the pool re-keys what
        it holds per device.

        Default: a no-op. Every platform but the Simulator XCUITest backend ignores the request, so
        each keeps the strongest retry it has today and the pipeline needs no per-platform branch.
        """

    def take_crash_snapshot(self) -> Callable[[], list[tuple[str, bytes]]]:
        """Hand the releasing lease the crash evidence this environment captured, and forget it.

        Called by the pool once, as the lease releases, and the returned thunk is what the run
        pipeline reads at a crash-exhausted `RunResult` — so a scenario that recovered within its
        retry budget never asks. Each pair the thunk yields is an artifact name and its bytes,
        written into that scenario's own evidence directory under `crash-diagnostics/` (BE-0421):
        the environment holds the only handles that name the crashed runner, and the pipeline holds
        the only writer that can cross the redaction boundary.

        Ownership *moves*, which is the whole point of returning a thunk rather than the bytes. The
        capture happens where the crash is observed, because the crashed lease is back in the pool
        before the retry loop gives up; but the environment then outlives the scenario — the pool
        keeps it warm per device — so evidence left behind here would be readable, and erasable, by
        whichever scenario leases the device next. Taking it at release makes it lease-local, the
        same rule `video_start_stalled` follows for exactly the same `workers > 1` reason. The thunk
        may still defer work of its own; what it must not do is read state a later lease can change.

        Default: a thunk answering `[]`. Every platform but the Simulator XCUITest backend captures
        nothing for a crash, so the pipeline's write step is an empty iteration and needs no
        per-platform branch.
        """

    def replaced_device(self) -> str | None:
        """The device this environment moved to when `start` replaced a vanished one, else None.

        Read *after* `start`, like `has_reusable_resident`: the XCUITest Simulator lifecycle creates a
        replacement when CoreSimulator has stopped listing the leased device, because retrying onto a
        device that no longer exists cannot work. The pool keys leases, collectors, evidence capture,
        and its warm cache by udid, so a swap it did not hear about would leave all of them naming a
        device that is gone. Default `None` (the leased device is the one that ran) — every platform
        but that one.
        """
