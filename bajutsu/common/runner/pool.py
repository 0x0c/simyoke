"""The device pool.

Lease a device per scenario (a single-device run is a pool of one). The per-platform lease shape —
relaunch, device control, network observation, teardown — comes from the `Environment` seam, so the
pool never branches on the actuator name (BE-0009 Phase 0).
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from pathlib import Path

from bajutsu.common.backend_cli import simctl
from bajutsu.common.backends import (
    default_available,
    resolve_evidence_providers,
    select_actuator,
    select_actuator_for_scenario,
)
from bajutsu.common.backends import make_driver as _make_driver
from bajutsu.common.config import Effective
from bajutsu.common.drivers import base
from bajutsu.common.drivers.webview import WebViewBridge
from bajutsu.common.evidence import FileSink
from bajutsu.common.evidence.network import Collector, NetworkCollector, _no_transitions
from bajutsu.common.evidence.redaction import Redactor
from bajutsu.common.evidence.sink import RunArtifactWriter
from bajutsu.common.orchestrator import DeviceControl, RelaunchFn
from bajutsu.common.orchestrator.evidence_rules import requested_intervals

# `device_control` / `device_relauncher` live with the platform lifecycle now; re-exported so
# `from bajutsu.common.runner import device_control, device_relauncher` keeps its import unchanged.
from bajutsu.common.platform_lifecycle import (
    ProvisionProfile,
    RunEnvironment,
    device_control,
    device_relauncher,
    environment_for,
)
from bajutsu.common.report import git_revision, run_provenance
from bajutsu.common.runner.launch import launch_driver
from bajutsu.common.runner.recovery import guarded_teardown
from bajutsu.common.runner.types import Lease, LeaseFn, _no_crash_artifacts
from bajutsu.common.scenario import Scenario, dump_scenario_file, redact_totp_secrets

__all__ = ["device_control", "device_pool", "device_relauncher"]

_logger = logging.getLogger(__name__)


def _alloc_webview_bridge(
    lease_env: object,
) -> tuple[WebViewBridge | None, int | None]:
    """Allocate a WebView bridge for platforms that need one (iOS, not web).

    Returns (bridge, port) or (None, None) when the platform doesn't use the bridge.
    """
    if getattr(lease_env, "observes_network_via_driver", lambda: False)():
        return None, None
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return WebViewBridge(port=port), port


def _alloc_zorder(lease_env: object) -> tuple[int, str] | None:
    """Reserve the port and per-run secret the in-app `nativeZ` responder answers on (BE-0355).

    One per lease, like the WebView bridge, so parallel devices never contend. The responder is a
    listener inside the app under test and iOS loopback is not isolated between apps, so it is given
    a fresh secret to require rather than left open to any co-resident process.
    """
    if getattr(lease_env, "observes_network_via_driver", lambda: False)():
        return None
    import secrets
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port, secrets.token_urlsafe(16)


# C901 and PLR0915 fold each nested function's count into the function enclosing it, so this score
# measures the pool closures defined below, not branching here. Ruff bounds each of those on its
# own, so the exemption loses no signal (BE-0386).
def device_pool(  # noqa: C901, PLR0915
    udids: list[str],
    backends: list[str],
    eff: Effective,
    run_dir: Path,
    *,
    network: bool = False,
    log_predicate: str | None = None,
    log_subsystem: str | None = None,
    secret_values: list[str] | None = None,
    provision: ProvisionProfile | None = None,
    available: Callable[[str], bool] = default_available,
    env_run: simctl.RunFn = simctl.real_run,
    make_driver: Callable[..., base.Driver] = _make_driver,
    evidence_providers: Callable[
        [list[str], str, Callable[[str], bool]], tuple[dict[str, str], dict[str, str]]
    ] = resolve_evidence_providers,
) -> tuple[LeaseFn, Callable[[], None]]:
    """A pool of N≥1 devices for (parallel) runs.

    `lease(eff, scenario)` leases a free udid (blocking until one frees up), launches the app
    pointed at that device's own network collector, and returns a `Lease` whose evidence sink
    (interval recordings under `run_dir`), relaunch, and device control are all bound to the leased
    device; `Lease.release()` terminates the app and returns the udid to the pool. A single-device
    run is just a pool of one, so network collection / interval evidence / device control work the
    same whether `workers` is 1 or N. The only shared state is the thread-safe free-device queue and
    the collectors / catalog maps, whose only in-lease writes re-key one exclusively-leased device
    onto its replacement, so leases need no lock.

    Args:
        udids: The devices to pool; the web backend ignores these (one browser lane).
        backends: Requested platforms/actuators; the first available one is selected.
        eff: The resolved target config.
        run_dir: Where each lease's interval evidence is written.
        network: Observe network traffic — iOS starts one HTTP collector per device up front; web
            hooks the page per lease.
        log_predicate: An `os_log` predicate scoping captured device logs. None captures none.
        log_subsystem: The app log subsystem to capture. None captures none.
        secret_values: Raw secret values to redact from evidence.
        provision: The device provider's readiness report (BE-0236) — a cloud device handed over
            already booted / with the app installed lets the environment skip that setup. None (the
            local provider's inert profile) runs the full per-platform bring-up, unchanged.
        available: Actuator-availability probe, injectable for tests.
        env_run: The subprocess runner for simctl, injectable for tests.
        make_driver: Builds a backend's driver; injectable so a test can supply a read-only
            evidence provider's driver (BE-0020).
        evidence_providers: Resolves the read-only evidence provider per gap kind (BE-0020),
            injectable for tests; defaults to the same-platform resolver.

    Returns:
        A `(lease, shutdown)` pair: `lease` leases a device for one scenario; `shutdown` stops every
        device's collector.
    """
    # Actuator selection is now per scenario (BE-0240): `lease()` resolves the cheapest actuator each
    # scenario can run on. Pool-level facts (device catalog, network-observation strategy, the
    # pre-started collectors) are *platform*-level — identical for every actuator on the platform — so a
    # representative `pool_actuator`
    # (availability-only, no scenario) reads them off one environment; nothing below branches on the
    # actuator name.
    pool_actuator = select_actuator(backends, available)
    pool_env: RunEnvironment = environment_for(
        pool_actuator, udids[0] if udids else "", env_run, provision=provision
    )
    # Resolve the device model / OS once up front (static per device) so each result can name the
    # simulator it ran on in the report; best-effort, so a missing catalog just omits it. A
    # driver-observed platform (web) has no device catalog.
    catalog = pool_env.device_catalog()
    # Resolve the git revision once (a subprocess) and reuse it across leases; the per-scenario
    # BE-0049 provenance stamp below folds it in so a first-wait timeout diagnostic is self-contained
    # (BE-0231 Unit 1). The full run manifest still recomputes the same stamp post-run.
    git_rev = git_revision()
    free: queue.Queue[str] = queue.Queue()
    for udid in udids:
        free.put(udid)
    # One collector per device (its own reserved-band port, not an OS ephemeral one — see
    # `start_bridgeable`), started up front and reused across leases (cleared per scenario by the
    # run loop). If a start fails mid-setup, stop the ones already started so we don't leak
    # listening sockets. Only the external-receiver path (the device backends) pre-starts these; a
    # driver-observed platform (web) has no up-front receiver and hooks its collector to the page
    # built per lease instead. `run/cli.py`'s `_channel_available_for` mirrors this condition to
    # decide whether BE-0365's control channel can be armed for a scenario, so a change to which
    # runs pre-start a collector has to be reflected there too.
    collectors: dict[str, NetworkCollector] = {}
    if network and not pool_env.observes_network_via_driver():
        try:
            for udid in udids:
                collector = NetworkCollector()
                # Only a backend whose bridge mirrors this number onto the device (Android's `adb
                # reverse`) constrains which port is usable; the reserved band is scoped to it so a
                # platform sharing the host's loopback keeps the OS-chosen port it always had.
                if pool_env.mirrors_collector_port_on_device():
                    collector.start_bridgeable()
                else:
                    collector.start()
                collectors[udid] = collector
        except Exception:
            # A socket already stopped for one device must not stop the rollback of the rest, or
            # replace the original bind failure the operator needs to see — the same reasoning as
            # every other collector-stop site this module guards (BE-0342). Each holds a port from
            # the reserved band, not an OS ephemeral one, so a socket left open here is held for the
            # rest of this (possibly long-lived `serve`) process.
            for started_udid, started_collector in collectors.items():
                guarded_teardown(
                    started_collector.stop,
                    mid_run=True,
                    what=f"stopping the collector on {started_udid} after a failed start",
                )
            raise

    # One warm resident per device, kept across leases so its cold startup is paid once per device
    # rather than once per scenario (BE-0291) — the same "start it once, reuse it" shape as the
    # per-device collectors above. Keyed by udid and tagged with the actuator it was started for, so
    # a scenario that resolves to a *different* actuator on the device tears the warm one down before
    # the new actuator's environment starts (the one-actuator-per-device rule, BE-0240). Only an
    # environment that reports `has_reusable_resident()` (the Simulator XCUITest runner) is cached;
    # every other backend leaves this empty and its per-lease teardown unchanged. Access needs no
    # lock for the same reason the free queue and collectors don't: a udid is leased exclusively (it
    # is out of `free` for the whole lease), so this device's entry is only ever touched by that
    # lease, and `shutdown()` runs after every worker has joined.
    warm: dict[str, tuple[str, RunEnvironment, base.Driver]] = {}

    # Which udids this run has already cold-spawned at least once, so a *fresh* env built for one is a
    # respawn. This covers only the eviction path: a warm resume that fails drops the resident
    # (`warm.pop` below), so the retry is a cache miss (`cached is None`) that builds a fresh env — with
    # no instance history, it needs this signal to take the tighter respawn ceiling. The far more
    # common mid-run-crash path keeps the *same* env (the dead resident stays warm-cached, the retry
    # reuses it and respawns cold in place), which the env's own `_cold_spawned_before` catches instead.
    # Scoped to this pool closure, so a long-lived `serve` process (many runs) never carries one run's
    # spawn history into the next run's first cold start. Same no-lock reasoning as `warm`/`free`: a
    # udid is leased exclusively for the whole lease.
    ever_spawned: set[str] = set()

    # The first *wiring* defect (not an expected process failure — `guarded_teardown` already warns
    # those away) a lease's own end-of-lease teardown hit, across every lease this pool ever ran.
    # `release()` cannot raise it directly: that would replace the scenario's own result and skip
    # `free.put(udid)`, the exact leak this item exists to close. So it stashes the defect here
    # instead and `shutdown()` raises it once the run's last release and every warm/collector
    # teardown are done — the same "swallow to finish the work, surface once it's safe to" shape
    # `shutdown()`'s own sweep already uses. Without this, a teardown that is *structurally*
    # impossible (a coding bug, not a runner that happened to be gone) would warn once per scenario
    # forever and never fail a run — for a backend with no warm resident (web, adb), this is the
    # *only* teardown that ever runs, so nothing else could catch it (BE-0342). Concurrent releases
    # across leases (BE-0009: the pool serves `ThreadPoolExecutor` workers) write this under a lock;
    # `shutdown()` reads it only after every worker has joined, so it needs none there.
    lease_defect_lock = threading.Lock()
    lease_defect: Exception | None = None

    def _record_lease_defect(udid: str, exc: Exception) -> None:
        nonlocal lease_defect
        with lease_defect_lock:
            if lease_defect is None:
                lease_defect = exc
            else:
                _logger.error(
                    "tearing down the environment on %s at the lease's end failed",
                    udid,
                    exc_info=exc,
                )

    # C901 and PLR0915 fold each nested function's count into the function enclosing it, so this
    # score measures the lease-lifecycle closures defined below, not branching here. Ruff bounds
    # each of those on its own, so the exemption loses no signal (BE-0386).
    def lease(eff: Effective, scenario: Scenario) -> Lease:  # noqa: PLR0915
        udid = free.get()
        # Resolve the actuator for *this* scenario — the cheapest one its own steps can run on
        # (BE-0240). The single-actuator-per-device rule (DESIGN §3.3/§5) is unchanged; its unit
        # narrows from "one CLI invocation" to "one scenario execution" — still exactly one actuator
        # on the leased device at any instant, never a mid-scenario swap.
        actuator = select_actuator_for_scenario(backends, scenario, available)
        # Reuse this device's warm resident when the scenario resolves to the same actuator; on an
        # actuator switch, tear the warm one down before the new actuator's environment starts (the
        # pool now owns that teardown — BE-0291 Unit 3). A cache miss builds a fresh environment.
        cached = warm.get(udid)
        if cached is not None and cached[0] != actuator:
            _cached_actuator, cached_env, cached_driver = warm.pop(udid)
            # Guarded like every other teardown site this module shares through `guarded_teardown`
            # (the failed-resume eviction below, `release()`'s own sites, and `shutdown()`'s loops):
            # if the cached runner already crashed between leases, `_discard_runner()`'s `terminate()`
            # can raise `ProcessLookupError` (an `OSError`). This runs before the `try` below, so
            # anything `guarded_teardown` re-raises propagates out of `lease()` with `udid` never
            # returned to `free`, leaking the device for the rest of the run — a wiring defect must
            # not escape here either (`mid_run=True`), the same reasoning as the failed-resume
            # eviction below (BE-0342).
            guarded_teardown(
                lambda: cached_env.teardown(cached_driver, eff),
                mid_run=True,
                what=f"tearing down the warm runner on {udid} for an actuator switch",
            )
            cached = None
        # A fresh env built for a udid already brought up once this run is a respawn (a prior warm
        # resume failed and evicted the resident), so it gets the tighter respawn readiness ceiling. A
        # cache hit reuses the warm env, which self-detects an in-place respawn (`_cold_spawned_before`),
        # so `respawn` there is moot — this signal is only for the fresh-env eviction path.
        is_respawn = cached is None and udid in ever_spawned
        lease_env: RunEnvironment = (
            cached[1]
            if cached is not None
            else environment_for(actuator, udid, env_run, provision=provision, respawn=is_respawn)
        )
        # A same-platform, read-only provider for an evidence kind this actuator can't supply
        # (BE-0020), resolved per scenario now that the actuator is. Today `network` is covered by web
        # (native) and both iOS actuators (the app-side `BAJUTSU_COLLECTOR`), so this resolves to
        # nothing in production; it activates when a platform gains a network-native actuator.
        providers, _skipped = evidence_providers(backends, actuator, available)
        network_provider = providers.get("network") if network else None
        # The collector to stop on release (the web page hook, or a BE-0020 fallback) — not the
        # pre-started HTTP receivers, which are reused and stopped in shutdown(). Released on a setup
        # failure too, so one launch failure neither leaks a socket nor starves later leases.
        release_collector: Collector | None = None

        # Teardown for the device-side collector bridge (Android's `adb reverse`, BE-0283); a no-op on
        # platforms that need none. Released on failure too, so a failed launch never leaks a tunnel.
        def release_bridge() -> None:
            pass

        # The environment/driver this attempt launched, remembered so the `except` below can tear it
        # down even on a backend that keeps no warm resident (`warm[udid]` only ever holds one for
        # XCUITest) — a failure raised after `launch_driver` returns but before `Lease` is built
        # (`adopt_replacement`'s own `device_catalog()` shelling out, `hook_collector`, `relauncher`,
        # `controller`, the `FileSink` construction) would otherwise leak it, with `free.put(udid)`
        # handing the lane to the next lease on top of it (BE-0342).
        launched: tuple[RunEnvironment, base.Driver] | None = None

        # This lease's own video-start confirmation outcome (BE-0354), reported by the sink below and
        # read back by the crash retry on the `Lease`. Local to the lease like every other per-lease
        # resource here, so a `workers > 1` run never reads one scenario's stall on another's retry.
        video_start_stalled = False

        def note_video_start_stall() -> None:
            nonlocal video_start_stalled
            video_start_stalled = True

        def adopt_replacement() -> None:
            """Follow the environment onto a device it had to replace, re-keying what this pool holds.

            The XCUITest Simulator lifecycle creates a replacement when CoreSimulator stops listing
            the leased device, and when a crash retry escalates to one because the device could not be
            recovered in place (BE-0354). Everything the pool keys by udid — the per-device collector, the
            warm-resident cache, the evidence sink's simctl captures, the result's device
            attribution, and which udid returns to the free queue — would otherwise keep naming a
            device that no longer exists. The old udid is deliberately never freed again: the
            replacement takes its place in the pool, which is what quarantines the dead one.

            Idempotent, because it is reached from both the success and the failure path: once `udid`
            is the replacement, the environment reports no further change.
            """
            nonlocal udid
            replacement = lease_env.replaced_device()
            if replacement is None or replacement == udid:
                return
            # Cause-neutral: the environment has already logged which rung replaced the device, and
            # naming one of them here would point an operator at the wrong failure class whenever the
            # other ran.
            _logger.warning(
                "device %s was replaced mid-lease; this run continues on %s", udid, replacement
            )
            # The collector is a host-side receiver the device reaches over the loopback, so it needs
            # no restart — only the key a later lease on this device looks it up by. Writing to
            # `collectors` / `catalog` here needs no lock for the same reason `warm` doesn't: this
            # lease exclusively holds the old udid, and the replacement was minted moments ago, so no
            # other lease can be touching either key.
            if (moved := collectors.pop(udid, None)) is not None:
                collectors[replacement] = moved
            # Anything cached under the dead udid can never be resumed; drop it before the key moves.
            warm.pop(udid, None)
            ever_spawned.discard(udid)
            # Move `udid` before the catalog re-fetch, which shells out and can itself fail (BE-0342):
            # a caller that guards this whole function against that failure must still see the *live*
            # replacement in `udid` afterwards, even missing its catalog metadata, rather than the dead
            # device this lease started on — the difference between a queue that hands the next lease
            # a working device and one that hands it a device that no longer exists.
            udid = replacement
            catalog[replacement] = lease_env.device_catalog().get(replacement, {})

        try:
            # Film the whole scenario only when its capture policy asks for video, and only where
            # capture is wired before launch (so the app's cold start is recorded): web binds it to
            # the browser context at creation, Android starts recording before the app
            # launches. Either way the temp dir must exist before the driver is built.
            record_video_dir: Path | None = None
            if lease_env.records_video_up_front() and "video" in requested_intervals(scenario):
                # The recorder writes into this staging dir itself, so the sink reserves it; the
                # finished recording crosses redaction when the sink finalizes it (BE-0331).
                record_video_dir = RunArtifactWriter(
                    run_dir, Redactor(eff.redact, values=secret_values)
                ).reserve("_video_tmp")
                record_video_dir.mkdir(parents=True, exist_ok=True)
            # A device backend points the app at its pre-started HTTP collector via launch env; a
            # driver-observed platform has no such env (it observes natively) and hooks its collector
            # from the live page after launch. A read-only fallback provider (BE-0020), when resolved,
            # supplies the collector instead — its own driver observes the same app.
            collector: Collector | None
            collector_provider = "collector"
            if not lease_env.observes_network_via_driver():
                if network_provider is not None:
                    fallback = make_driver(network_provider, udid).network_collector()  # type: ignore[attr-defined]
                    collector = release_collector = fallback
                    collector_provider = f"{network_provider} (fallback)"
                else:
                    collector = collectors.get(udid)
            else:
                collector = None  # resolved after launch from the live page
            extra_env: dict[str, str] = {}
            if isinstance(collector, NetworkCollector):
                extra_env["BAJUTSU_COLLECTOR"] = f"http://127.0.0.1:{collector.port}"
                extra_env["BAJUTSU_COLLECTOR_TOKEN"] = collector.token
                # Make the host collector reachable from the leased device before launch (Android
                # tunnels the port with `adb reverse`; iOS shares the loopback and no-ops) — BE-0283.
                release_bridge = lease_env.bridge_collector(collector.port)
            webview_bridge, webview_port = _alloc_webview_bridge(lease_env)
            if webview_port is not None:
                extra_env["BAJUTSU_WEBVIEW_PORT"] = str(webview_port)
            zorder = _alloc_zorder(lease_env)
            if zorder is not None:
                extra_env["BAJUTSU_ZORDER_PORT"] = str(zorder[0])
                extra_env["BAJUTSU_ZORDER_TOKEN"] = zorder[1]
            driver, readiness = launch_driver(
                udid,
                eff,
                actuator,
                scenario.preconditions,
                env_run,
                extra_env,
                record_video_dir,
                # Start on the same environment we tear down: a stateful backend (XCUITest's resident
                # runner) must be terminated by the instance that spawned it (BE-0240).
                environment=lease_env,
                permissions=scenario.permissions,
                # The screen-transition signal (BE-0310): only the app-side collector receives it (an
                # app linking BajutsuKit's observer reports to it), so a driver-observed platform or a
                # scenario with no collector keeps the default (no signal, tree-diff fallback).
                transitions=(
                    collector.transitions_snapshot_timed
                    if isinstance(collector, NetworkCollector)
                    else _no_transitions
                ),
            )
            # Before anything else is keyed by it: `start` may have moved this lease onto a
            # replacement device, and every udid-keyed structure below must name the device that
            # actually ran. Remember what this attempt launched first, so the `except` below can tear
            # it down even when `adopt_replacement()` itself is what raises.
            launched = (lease_env, driver)
            adopt_replacement()
            # This device has now been brought up at least once this run, so a later cache-miss lease
            # on it (after a crash evicts the warm resident) is a respawn — see `is_respawn` above.
            ever_spawned.add(udid)
            # Keep this device's resident warm for the next lease when the environment holds one
            # (the Simulator XCUITest runner); the next same-actuator lease resumes it instead of
            # spawning a fresh runner (BE-0291). Every other backend reports False and is not cached.
            if lease_env.has_reusable_resident():
                warm[udid] = (actuator, lease_env, driver)
            sink = FileSink(
                run_dir,
                udid=udid,
                log_predicate=log_predicate,
                log_subsystem=log_subsystem,
                redact=eff.redact,
                secrets=secret_values,
                # A web or Android lane supplies its own interval evidence (Playwright console / page
                # errors; adb logcat — Android's video now takes the prestart/adopt path below); the
                # iOS backend has no such method, so this is None there and the simctl path is used.
                driver_interval=getattr(driver, "driver_interval", None),
                # Video the environment already began before the app launched (Android, so
                # the cold start is recorded); the sink adopts it instead of starting one on demand.
                prestarted_intervals=lease_env.prestarted_intervals(),
                # Carried so a first-wait timeout diagnostic can state whether the readiness gate had
                # passed and on which signal, stamped with this scenario's BE-0049 provenance so the
                # evidence survives a rerun-to-green (BE-0231 Unit 1). The `scenarioHash` here
                # fingerprints this one scenario, without the file-level `description` the run
                # manifest's hash folds in when present, so it can diverge from the manifest's hash
                # even for a single-scenario run (see docs/evidence.md).
                readiness=readiness,
                provenance=run_provenance(
                    dump_scenario_file([redact_totp_secrets(scenario)]), git_revision=git_rev
                ),
                on_video_start_stall=note_video_start_stall,
            )
            # A driver-observed platform hooks its collector to the live page now (and fulfils this
            # scenario's mocks); a fresh context per lease scopes its traffic, mirroring the device's
            # per-scenario collector clear. It is stopped on release.
            if lease_env.observes_network_via_driver() and network:
                collector = release_collector = lease_env.hook_collector(driver, scenario)
                # Native observation by the selected actuator, not the app-side receiver. Naming the
                # actuator keeps provenance accurate if another driver-observed actuator is added;
                # today this is "playwright".
                collector_provider = actuator
            relaunch: RelaunchFn = lease_env.relauncher(eff, scenario, driver, extra_env=extra_env)
            control: DeviceControl | None = lease_env.controller(eff)

            # This lease's own copy of whatever crash evidence its environment captured, taken off that
            # environment at the end of `release()` below (BE-0421). Lease-local for the reason
            # `video_start_stalled` is: the environment is kept warm per device, so evidence left on it
            # would be readable — and clearable — by whichever scenario leases the device next.
            crash_evidence: Callable[[], list[tuple[str, bytes]]] = _no_crash_artifacts

            def release() -> None:
                nonlocal crash_evidence
                # Runs from `run_one`'s `finally`, so a teardown hiccup anywhere below (an
                # already-gone tunnel, socket, app, or device) must not replace the scenario's own
                # result or skip `free.put(udid)` — the same reasoning as the actuator switch above
                # and the failed-lease site below (`shutdown()`'s two loops take `mid_run=False`
                # instead — they own the deferred `raise defect` that finishes the cleanup first).
                guarded_teardown(
                    release_bridge,  # tear the device-side collector tunnel down first (BE-0283)
                    mid_run=True,
                    what=f"tearing down the collector bridge on {udid} at the lease's end",
                )
                if release_collector is not None:
                    guarded_teardown(
                        release_collector.stop,
                        mid_run=True,
                        what=f"stopping the collector on {udid} at the lease's end",
                    )
                # Keep a warm resident alive for the next lease (`end_lease` terminates only the app);
                # otherwise the ordinary full teardown. This is the same predicate the pool cached the
                # env on above, so a kept-warm env is exactly one still held in `warm` (BE-0291).
                reusable = lease_env.has_reusable_resident()
                ended = False

                def _end_lease() -> None:
                    nonlocal ended
                    if reusable:
                        lease_env.end_lease(driver, eff)
                    else:
                        lease_env.teardown(driver, eff)
                    ended = True

                try:
                    # `mid_run=False` here, unlike every other site in this function: an expected
                    # process failure is still just warned (that branch of `guarded_teardown` runs
                    # regardless of `mid_run`), but a *wiring* defect re-raises instead of warning, so
                    # it can be caught below and stashed in `lease_defect` rather than lost to a log
                    # line no one is watching (BE-0342).
                    guarded_teardown(
                        _end_lease,
                        mid_run=False,
                        what=f"tearing down the environment on {udid} at the lease's end",
                    )
                except Exception as exc:
                    _record_lease_defect(udid, exc)
                if reusable and not ended:
                    # `end_lease` only kills the app; a resident whose `end_lease` did not finish
                    # must not be resumed, so drop it from `warm` (the next lease then cold-spawns
                    # instead of inheriting an app whose teardown never completed) — and fall back to
                    # the full teardown so the runner process it still owns doesn't leak, since a
                    # dropped `warm` entry is invisible to `shutdown()`'s own sweep (BE-0342).
                    warm.pop(udid, None)
                    guarded_teardown(
                        lambda: lease_env.teardown(driver, eff),
                        mid_run=True,
                        what=f"discarding the runner on {udid} after a failed end-of-lease teardown",
                    )

                def _take_crash_snapshot() -> None:
                    nonlocal crash_evidence
                    crash_evidence = lease_env.take_crash_snapshot()

                # After every teardown above, because those are what let the environment observe a
                # crashed runner, and before `free.put` hands the device to the next lease (BE-0421).
                # Guarded like every other site here, even though today's implementation cannot raise:
                # this call sits ahead of `free.put`, so a hiccup in a future implementation that does
                # real work here (the protocol explicitly allows it) must not skip it and strand the
                # device on the next lease's `free.get()`.
                guarded_teardown(
                    _take_crash_snapshot,
                    mid_run=True,
                    what=f"capturing the crash snapshot on {udid} at the lease's end",
                )
                free.put(udid)

            meta = catalog.get(udid, {})
            return Lease(
                driver=driver,
                sink=sink,
                relaunch=relaunch,
                control=control,
                collector=collector,
                release=release,
                udid=udid,
                device_name=meta.get("name", ""),
                device_runtime=meta.get("runtime", ""),
                collector_provider=collector_provider,
                webview_bridge=webview_bridge,
                # Both are read by the crash retry after this lease has already been released: the
                # request lands on the environment the pool keeps warm for this device, so the *next*
                # lease's `start` serves it, and `adopt_replacement` above then re-keys everything
                # this pool holds onto the device it created (BE-0354).
                request_device_replacement=lease_env.request_device_replacement,
                video_start_stalled=lambda: video_start_stalled,
                # Read after release, like the two above, but out of this lease's own copy rather than
                # off the shared environment — `release()` moved it there (BE-0421).
                # The lambda is load-bearing: `crash_evidence` is rebound by `release()`, so binding
                # the name directly would freeze this lease on the no-op it starts at.
                crash_artifacts=lambda: crash_evidence(),  # noqa: PLW0108
            )
        except BaseException:
            # A failed launch must not leak the collector tunnel (BE-0283) or the collector itself —
            # and, same as the teardown below, a hiccup tearing either down must not replace the
            # *original* launch error that has to propagate via the `raise` below (BE-0342).
            guarded_teardown(
                release_bridge,
                mid_run=True,
                what=f"tearing down the collector bridge on {udid} after a failed lease",
            )
            if release_collector is not None:
                guarded_teardown(
                    release_collector.stop,
                    mid_run=True,
                    what=f"stopping the collector on {udid} after a failed lease",
                )
            # A warm resident whose resume failed must not be reused next lease: drop it and tear it
            # down so the retry respawns cold rather than reusing a half-broken runner (BE-0291). This
            # is best-effort cleanup on the failure path — the *original* launch error is what must
            # propagate (via the `raise` below), so a teardown hiccup is logged, never re-raised. It
            # runs before the replacement is adopted below, because a resident cached by an earlier
            # lease is keyed by the udid this lease started on.
            stale = warm.pop(udid, None)
            # A backend that keeps no warm resident (web / Android / adb / fake) never populates
            # `warm[udid]`, so a failure raised after `launch_driver` returned falls back to what this
            # attempt itself launched — the same environment `stale` would have named had this backend
            # cached one (BE-0342).
            doomed = (stale[1], stale[2]) if stale is not None else launched
            if doomed is not None:
                dead_env, dead_driver = doomed
                # A leaked runner is the same risk here as at this module's other teardown sites
                # (the actuator switch above, `release()`'s own sites, and `shutdown()`'s loops); the
                # original launch error still propagates via the `raise` below, so a teardown hiccup
                # (mid_run=True) must not mask it (BE-0342).
                guarded_teardown(
                    lambda: dead_env.teardown(dead_driver, eff),
                    mid_run=True,
                    what=f"tearing down the environment on {udid} after a failed lease",
                )
            # A replacement made before the failure is still adopted, so the queue gets the live device
            # back for the next lease instead of the one that vanished. This call can itself shell out
            # via `device_catalog()` and fail the same way the one after `launch_driver` did — guarded
            # so a second failure here doesn't replace the *original* launch error, or skip
            # `free.put`/the `raise` below and strand the device: a hang forever on the next lease's
            # `free.get()` rather than a lost device (BE-0342).
            guarded_teardown(
                adopt_replacement,
                mid_run=True,
                what=f"adopting {udid}'s replacement after a failed lease",
            )
            free.put(udid)
            raise

    def shutdown() -> None:
        # The run set is over: terminate every warm resident the pool kept across leases (BE-0291 Unit
        # 3 — ownership moved from the lease to the pool). An expected teardown failure on one device
        # (the app already gone, xcrun unreachable) is logged and skipped so the rest — and the
        # collector sockets below — still come down. A wiring defect must still fail loudly, but not
        # before the rest of the sweep and the collector sockets have come down: `mid_run=False`'s
        # usual immediate propagation would otherwise leak every later device's runner and every
        # collector socket. Only the *first* such defect is what `raise defect` below reports; a
        # later one is not silently dropped either — it gets its own log line (BE-0342).
        defect: Exception | None = None
        for udid, (_actuator, env, driver) in warm.items():

            def _tear_warm(env: RunEnvironment = env, driver: base.Driver = driver) -> None:
                env.teardown(driver, eff)

            try:
                guarded_teardown(
                    _tear_warm,
                    mid_run=False,
                    what=f"tearing down the warm runner on {udid}",
                )
            except Exception as exc:
                if defect is None:
                    defect = exc
                else:
                    _logger.exception(
                        "tearing down the warm runner on %s failed", udid, exc_info=exc
                    )
        warm.clear()
        # A collector socket failing to stop is the same risk as a device's teardown failing: it must
        # not stop the sweep, and it must not silently swallow a defect already held from above. Also
        # routed through `guarded_teardown`, so an expected process failure here (the socket already
        # gone) is warned like the device loop's, not treated as a defect of its own.
        for udid, collector in collectors.items():
            try:
                guarded_teardown(
                    collector.stop,
                    mid_run=False,
                    what=f"stopping the collector on {udid}",
                )
            except Exception as exc:
                if defect is None:
                    defect = exc
                else:
                    _logger.exception("stopping the collector on %s failed", udid, exc_info=exc)
        # A lease's own end-of-lease wiring defect happened before any of this sweep — `release()`
        # stashed it instead of raising it, so it is this run's *first* defect even though it is
        # checked last here; anything this sweep found is a later one, logged rather than dropped.
        if lease_defect is not None:
            if defect is not None:
                _logger.error("tearing down a warm runner or collector failed", exc_info=defect)
            defect = lease_defect
        if defect is not None:
            raise defect

    return lease, shutdown
