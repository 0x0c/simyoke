"""Shared types for the run pipeline: the per-scenario device Lease and the injected callables.

No run logic here, so launch / pool / pipeline can all import it without a cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from bajutsu.common.config import Effective
from bajutsu.common.drivers import base
from bajutsu.common.drivers.webview import DomSource
from bajutsu.common.evidence import EvidenceSink
from bajutsu.common.evidence.network import Collector
from bajutsu.common.orchestrator import AlertGuardConfig, DeviceControl, RelaunchFn, SkippedCapture
from bajutsu.common.scenario import Scenario

# Builds the in-scenario relaunch function for a scenario (given its live driver).
RelaunchFactory = Callable[[Effective, Scenario, base.Driver], RelaunchFn]

# Selects the alert-guard config for one scenario (None = no guard for it). The CLI sets this so
# each scenario's `systemAlertHandling` (default on, optional button policy / poll interval) decides
# whether — and how — the guard runs; the orchestrator stays oblivious to the per-scenario choice.
AlertGuardFor = Callable[[Scenario], AlertGuardConfig | None]


def _no_replacement() -> None:
    """The neutral replacement request: a lease whose platform cannot mint a device (BE-0354)."""
    return


def _never_stalled() -> bool:
    """The neutral video-start signal: a lease that recorded no video, so none could stall (BE-0354)."""
    return False


def _no_crash_artifacts() -> list[tuple[str, bytes]]:
    """The neutral crash capture: a lease whose platform captures nothing for a crash (BE-0421)."""
    return []


@dataclass
class Lease:
    """A leased device for one scenario run.

    It bundles the live driver with the per-device resources bound to that device. `release()`
    terminates the app and returns the device to the pool.

    `collector` is None when network collection is off; otherwise it observes the leased
    device's traffic (iOS: the app POSTs to an HTTP receiver; web: Playwright events),
    cleared per scenario by the run loop.
    """

    driver: base.Driver
    sink: EvidenceSink
    relaunch: RelaunchFn | None
    control: DeviceControl | None
    collector: Collector | None
    release: Callable[[], None]
    udid: str = ""  # the leased device, recorded on each RunResult (parallel-split attribution)
    # The leased device's model / OS runtime, recorded on the result for the report's
    # Environment tab (empty when the simulator catalog couldn't be read).
    device_name: str = ""
    device_runtime: str = ""
    # Provenance for the network artifact: "collector" (the actuator's own app-side receiver) or
    # "<backend> (fallback)" when a same-platform read-only provider supplied it (BE-0020).
    collector_provider: str = "collector"
    # Evidence kinds no eligible backend could supply, disclosed per scenario (BE-0020).
    skipped_captures: list[SkippedCapture] = field(default_factory=list)
    # WebView bridge for hybrid apps (BE-0037). None when the app has no WebView or the platform
    # doesn't support the bridge (e.g. web/Playwright — it already has DOM access).
    webview_bridge: DomSource | None = None
    # Ask this lease's environment to bring the *next* lease up on a replacement device (BE-0354). The
    # crash retry calls it when an erase has already been tried and did not clear the degradation, so
    # the next attempt lands on a device that cannot have inherited it. A no-op on every platform that
    # cannot mint a device, which is why the pipeline can call it without branching on the backend.
    request_device_replacement: Callable[[], None] = _no_replacement
    # Whether this lease's video capture never confirmed it started writing (BE-0354). The earliest
    # symptom of the wedged capture pipeline, and advisory only: it selects the crash retry's recovery
    # rung and never fails an attempt on its own. Always False where no video was recorded.
    video_start_stalled: Callable[[], bool] = _never_stalled
    # The files this lease's environment captured for a backend crash it could not recover from
    # (BE-0421), as `(artifact name, bytes)` pairs. Read once, after the crash retry loop has given up,
    # and written into the failed scenario's own evidence directory — so a scenario that recovered
    # never asks, and a platform that captures nothing returns `[]` and leaves the write a no-op.
    crash_artifacts: Callable[[], list[tuple[str, bytes]]] = _no_crash_artifacts


# Leases a free device for one scenario (blocking until one frees up): launches the app
# and returns the Lease the run loop drives, then release()s.
LeaseFn = Callable[[Effective, Scenario], Lease]
