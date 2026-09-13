"""The XCUITest lifecycle: prepare the device, then bring up a resident runner on it."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, cast

from bajutsu.common import backends, stall_diagnostics
from bajutsu.common.backend_cli import simctl
from bajutsu.common.config import Effective, require_ios
from bajutsu.common.devices import os as device_os
from bajutsu.common.devices.os import DeviceOS
from bajutsu.common.drivers import base
from bajutsu.common.drivers.zorder import ZOrderSource
from bajutsu.common.platform_lifecycle.environments._bundled_runner import _products_digest
from bajutsu.common.platform_lifecycle.environments.ios import _DeviceEnvironment
from bajutsu.common.scenario import Preconditions

from ._attempt_failure import _AttemptFailure
from ._functions import (
    _allocate_port,
    _destination,
    _diagnostic_reports_dir,
    _max_warm_reuses,
    _never_ended,
    _no_recovery,
    _patch_xctestrun_env,
    _recovery_timeout,
    _reported_pid,
    _reports_since,
    _resolve_runner,
    _respawn_timeout,
    _run_ended_probe,
    _runner_host_bundle_ids,
    _runner_startup_timeout,
    _spawn_cold_with_retry,
    _terminate_process_group,
    _zorder_client,
    effective_device_type,
)
from ._recovery import _Recovery
from ._shared import _logger
from ._spawned import _Spawned

# Overrides the directory the runner subprocess's combined stdout/stderr is captured into, one file
# per cold spawn. Capture is on by default (BE-0319 unit 1): a startup failure or mid-run crash is
# diagnosable without a human pre-arming this, so the variable now only redirects the capture
# directory. A default (env-unset) capture goes to `_DEFAULT_RUNNER_LOG_DIR` and is pruned on
# teardown; an explicit directory is kept, since the operator asked for it.
_RUNNER_LOG_ENV = "BAJUTSU_XCUITEST_RUNNER_LOG"

# Where a default (env-unset) capture goes — a run-scoped temporary area teardown can prune, so a
# passing run leaves nothing behind while a failing one is still diagnosable.
_DEFAULT_RUNNER_LOG_DIR = Path(tempfile.gettempdir()) / "bajutsu-xcuitest-runner"

# Lines of captured runner output to fold into the crash warning and the startup-failure error —
# enough to show the tail of an `xcodebuild` failure without dumping the whole (verbose) log.
_RUNNER_LOG_TAIL_LINES = 20

# Names the directory `xcodebuild` writes its XCTest result bundle into, one per spawn (BE-0361
# unit 1). The bundle records what testmanagerd itself saw — the precise XCTest failure, its
# timestamps, and any attachments — which the captured stdout above only paraphrases. Unset (the
# default) leaves the spawn argv exactly as it was, so this costs nothing until CI opts in; set, the
# variable *is* the operator asking for the bundles, so every one is kept (unlike the runner log
# above, whose env-unset default capture teardown prunes).
_RESULT_BUNDLE_ENV = "BAJUTSU_XCUITEST_RESULT_BUNDLES"

# Lines of captured runner output copied into the failed scenario's own evidence directory (BE-0421).
# Deliberately far above `_RUNNER_LOG_TAIL_LINES`: this artifact exists precisely because the 20-line
# hint folded into the crash warning was too short to diagnose from, and it is read the same streaming
# way, so the larger cap costs a longer `deque` and nothing else.
_CRASH_LOG_TAIL_LINES = 500

# Reports one crash episode may copy, mirroring BE-0361's own per-capture caps: a runner that
# crash-loops on one host leaves a report per fault, and one scenario's evidence must stay bounded
# regardless. The name-and-time match below usually leaves exactly one, so this only bounds the case
# where several concurrent workers' `xcodebuild` processes faulted within the same window.
_MAX_CRASH_REPORTS = 3


# Probing a *warm* runner before reuse (BE-0291): a live runner answers /health at once, so this only
# bounds the wedged case — a runner that crashed after repeated app.launch() cycles must be detected
# quickly and respawned, not waited on for the cold ceiling.
_WARM_HEALTH_TIMEOUT = 10.0


class XcuitestEnvironment(_DeviceEnvironment):
    """The XCUITest lifecycle: simctl device prep then a resident runner on the Simulator, or the
    same runner without simctl prep on a real device (BE-0019, real-device targeting BE-0238).

    The simctl sequence (erase / boot / install) is the standard iOS Simulator prep. The difference from
    the previous coordinate-CLI approach is how the app is driven: instead of launching the app via
    simctl and actuating over a coordinate CLI, we start an
    `xcodebuild test-without-building` subprocess that runs the BajutsuRunner XCTest target — the
    runner launches the app, starts an HTTP server on localhost, and Python drives it through the
    `XcuitestDriver` channel.
    """

    def __init__(
        self,
        actuator: str,
        udid: str,
        env_run: simctl.RunFn = simctl.real_run,
        *,
        respawn: bool = False,
    ) -> None:
        super().__init__(actuator, udid, env_run)
        # This *fresh* environment was built by the pool for a mid-run respawn — the pool already
        # cold-spawned this device once this run and had to build a new environment for it (the
        # failed-resume eviction path, `cached is None`). A cold start here gets the tighter respawn
        # readiness ceiling. The far more common crash path keeps the *same* environment (a mid-run
        # crash leaves the dead resident warm-cached, so the retry reuses this instance and respawns
        # cold in place) — `_cold_spawned_before` below catches that. See `_respawn_timeout` /
        # `_spawn_cold`. Both default False for a genuine first bring-up, which keeps the cold ceiling.
        self._respawn = respawn
        # True once this instance has cold-spawned at least once: a *second* `_spawn_cold` on the same
        # environment is an in-place respawn (its warm resident died, so `start` discards it and
        # re-spawns cold), so it too takes the respawn ceiling — the ceiling must not depend only on
        # `_respawn`, which a reused instance built at first bring-up never has set.
        self._cold_spawned_before = False
        self._runner_proc: subprocess.Popen[bytes] | None = None
        # Wall-clock seconds at which `_runner_proc` was spawned (BE-0421). `Popen` exposes no start
        # time of its own, and a crash report's modification time is what tells this run's report apart
        # from an unrelated `xcodebuild` invocation's left on the same host. Wall clock, not monotonic,
        # because it is compared against a file's `st_mtime`.
        self._runner_spawned_at: float = 0.0
        self._runner_port: int = 0
        self._patched_runner: Path | None = None
        # Where the current runner's captured output went; a mid-run-crash warning and a startup
        # failure both point at it (`_runner_log_hint`). Capture is on by default (BE-0319 unit 1).
        self._runner_log: Path | None = None
        # True when `_runner_log` is a default (env-unset) capture teardown should prune; False when
        # it is an explicit `BAJUTSU_XCUITEST_RUNNER_LOG` directory the operator asked to keep.
        self._runner_log_ephemeral = False
        # The current spawn's run-ended probe over that capture, shared by the cold-spawn gate and the
        # mid-run liveness predicate (BE-0354); the neutral probe until a spawn wires a real one.
        self._run_ended: Callable[[], str | None] = _never_ended
        # BE-0291: True once a Simulator `start` has left a runner the pool should keep warm across
        # leases. A real-device start (BE-0238) never sets it — warm reuse targets only the Simulator
        # runner's cold startup — so the pool tears such an environment down per lease, unchanged.
        self._reusable = False
        # The locale this device's SpringBoard is currently pinned to (BE-0320), or None when nothing
        # has pinned it yet. Set only by a cold `_prepare_simulator` that verified the write landed,
        # and compared before a warm reuse — a scenario running under a different locale must not be
        # served by a runner whose SpringBoard is still rendering the previous one.
        self._pinned_locale: str | None = None
        # The digest of the app bundle last installed on this device (BE-0407 Unit 14), or None
        # when nothing tracked is known to be there — a fresh environment, right after an erase, or
        # right after an explicit uninstall. Compared before a `reinstall: overwrite` install to skip
        # a no-op reinstall of an unchanged binary; `reinstall: clean` never reads it; it always
        # uninstalls first, which already requires a fresh install regardless of digest.
        self._installed_app_digest: str | None = None
        # How many times the current runner has been reused warm (BE-0287): reset on a cold spawn,
        # incremented on each warm resume, and capped by `_max_warm_reuses()` so the runner is
        # respawned cold before it accumulates enough app.launch() cycles to crash mid-scenario.
        self._warm_reuses = 0
        # The app the runner launches, remembered on the Simulator path so a discard can terminate it
        # (`_terminate_app_under_test`). None until the first spawn, and on a real device, where
        # simctl does not apply.
        self._bundle_id: str | None = None
        # The XCTRunner apps of the .xctestrun this environment spawned, read out of its plist so a
        # discard can terminate the runner app itself (`_terminate_runner_app`). Empty until the
        # first spawn, and on a real device, where simctl does not apply.
        self._runner_bundle_ids: tuple[str, ...] = ()
        # This device's `deviceTypeIdentifier` and runtime identifier, captured while it is healthy
        # so a replacement can be cloned from it after it vanishes (`_replace_vanished_device`).
        self._device_type_id: str | None = None
        self._device_runtime_id: str | None = None
        # The udid this environment started on, once a vanished device forced a replacement — the flag
        # `replaced_device()` reports the swap by, so the pool can re-key what it holds per device.
        self._replaced_from: str | None = None
        # Set by `request_device_replacement` when the run pipeline escalates a crash retry above the
        # forced erase (BE-0354); consumed by the next `start`, which swaps the device before it preps.
        self._replacement_requested = False
        # The in-app `nativeZ` responder client for the launch this environment last started
        # (BE-0355); None until a `start` sees a port in its launch env.
        self._zorder: ZOrderSource | None = None
        # What the last observed mid-run crash captured, for the failed scenario's own evidence
        # directory (BE-0421): the runner log's tail, read eagerly at the moment of the crash, and the
        # criteria that identify the crashed process's macOS crash report, frozen at that same moment.
        # Both are snapshots rather than live reads, because the crashed lease is released back to the
        # pool before the retry loop gives up — so a later lease on this same device can have respawned
        # a runner, and cleared `_runner_log` / `_runner_proc`, before anyone reads these back.
        self._last_crash_artifacts: list[tuple[str, bytes]] = []
        # `(spawn timestamp, pid)` of the crashed runner, or None when no crash has been observed.
        self._last_crash_report_match: tuple[float, int] | None = None
        # Whether the current spawn's crash has already been captured. Reset per spawn, so each crash
        # is snapshotted once however many sites observe it: `end_lease` sees a crash first, and the
        # next bring-up's discard would otherwise re-capture the same dead runner — regenerating
        # evidence the releasing lease has already taken ownership of, for a later scenario to inherit.
        self._crash_snapshotted = False

    def start(
        self,
        eff: Effective,
        pre: Preconditions,
        *,
        extra_env: Mapping[str, str] | None = None,
        record_video_dir: Path | None = None,  # noqa: ARG002  # Environment shape
        permissions: Mapping[str, str] | None = None,
    ) -> base.Driver:
        ios = require_ios(eff)
        xcfg = ios.xcuitest
        device_type = effective_device_type(xcfg)
        # The app answers `nativeZ` on the port this run injected, so the client is built from the
        # same launch env the app will read (BE-0355). Held on the environment rather than threaded
        # through the spawn path, so a warm resume reuses the responder its own launch set up.
        self._zorder = _zorder_client(extra_env)
        # Read once and cleared here rather than where it is honored, so no `start` can leave a stale
        # escalation behind for a later lease — including the real-device route below, which returns
        # before the rung and has no simctl to mint a device through anyway.
        replace_device = self._replacement_requested
        self._replacement_requested = False

        if device_type == "device":
            # A real device is not managed through simctl: it is already powered on, its build is
            # installed out of band, and `simctl privacy` cannot reach it. The simctl-only
            # preconditions it cannot honour fail loudly here (real-device install / permissions
            # are BE-0238 Unit 2/3) rather than silently no-op'ing — determinism first.
            if pre.erase:
                raise simctl.DeviceError(
                    "erase is a simctl operation and does not apply to a real device "
                    "(xcuitest.deviceType: device)"
                )
            if ios.app_path:
                raise simctl.DeviceError(
                    "installing appPath through simctl does not apply to a real device "
                    "(xcuitest.deviceType: device); install the app and its device-build test "
                    "runner out of band"
                )
            if permissions:
                raise simctl.DeviceError(
                    "permission grants use simctl and do not apply to a real device "
                    "(xcuitest.deviceType: device)"
                )
            return self._spawn_cold(eff, pre, device_type, extra_env, permissions)

        # A pending escalation (BE-0354) is served before anything else touches the device: the run
        # pipeline asked for a replacement because an erase was already tried on this one and did not
        # clear the degradation, so preparing or reusing the degraded device first would only spend
        # the remedy the escalation exists to skip. The swap leaves the environment on a device that
        # has never run anything, which is a cold spawn by construction — no warm runner to reuse.
        if replace_device:
            # The discard's `terminate` is bounded now (BE-0363), and on *this* device the wedge it
            # would report is the reason the escalation exists — so raising here would consume the
            # one remedy left, with no second chance: `_replacement_requested` was already cleared
            # above, so the retry lease would take the ordinary path below and fail the same way.
            # The swap itself keeps every other timeout loud, and `_replace_degraded_device` makes
            # the same call for its own shutdown two lines down. Logged rather than suppressed, like
            # every other caller that absorbs this timeout: `terminate` and `shutdown` are different
            # commands, so the one below is not guaranteed to wedge too, and swallowing this one
            # silently would lose the very diagnosis BE-0363 exists to produce.
            try:
                self._discard_runner()
            except simctl.DeviceTimeout as exc:
                _logger.warning(
                    "discarding the runner on Simulator %s: %s; replacing it anyway",
                    self._udid,
                    exc,
                )
            self._replace_degraded_device(eff)
            # The erase is dropped *here*, where the swap actually happened, rather than by the
            # caller that asked for it: a device this method just created has nothing to erase, and
            # honoring the precondition would pay a second shutdown-and-boot cycle on it for no state
            # change. Deciding it caller-side would mean predicting this branch — and a request that
            # never reached this instance (a lease whose environment the pool had already evicted)
            # would then leave the retry with neither remedy.
            return self._spawn_cold(
                eff, pre.model_copy(update={"erase": False}), device_type, extra_env, permissions
            )

        # Simulator: reuse a healthy warm runner across leases (BE-0291). `erase` shuts the Simulator
        # down (killing the runner), so a scenario that erases forces a cold respawn; a wedged runner
        # is a cache miss too (Unit 4), costing one extra cold start rather than the run. A scenario
        # under a different locale than the one this device's SpringBoard is pinned to forces a cold
        # respawn for the same reason (BE-0320): only a cold spawn re-pins and reboots, so reusing
        # the warm runner would run the scenario against the previous scenario's language. The reuse
        # budget (`_max_warm_reuses`) forces a cold respawn *before* the runner accumulates enough
        # app.launch() cycles to crash mid-scenario (BE-0287) — a proactive refresh, checked before the
        # health probe so a spent runner skips straight to a cold spawn.
        if (
            not pre.erase
            and self._pinned_locale == pre.resolved_locale(eff.locale)
            and self._warm_reuses < _max_warm_reuses()
            and (driver := self._healthy_resident_driver()) is not None
        ):
            return self._resume_warm(eff, pre, extra_env, permissions, driver)
        self._discard_runner()  # drop any dead / lingering / reuse-spent runner before a fresh spawn
        return self._spawn_cold(eff, pre, device_type, extra_env, permissions)

    def _spawn_cold(
        self,
        eff: Effective,
        pre: Preconditions,
        device_type: str,
        extra_env: Mapping[str, str] | None,
        permissions: Mapping[str, str] | None,
    ) -> base.Driver:
        """Bring the runner up from cold: simctl prep (Simulator only), then spawn `xcodebuild`.

        The simctl device prep runs once on the healthy path; only the `xcodebuild` spawn is retried
        (BE-0319 unit 4). `_spawn_cold_with_retry` awaits readiness with a liveness check (unit 3),
        retries a one-off cold-start blip once, discards every failed attempt (no leaked subprocess —
        the leak BE-0290 prevents), and on a repeatable failure fails loudly with each attempt's
        captured tail. Between attempts on a Simulator it also repairs the device
        (`_recover_between_attempts`), which is the one path that runs the prep a second time.
        """
        ios = require_ios(eff)
        self._bundle_id = ios.bundle_id if device_type != "device" else None
        if device_type != "device":
            self._prepare_simulator(eff, pre, permissions, cold=True)

        # The runner launches the app via XCUIApplication.launch(). Preconditions are forwarded
        # through env vars: the runner reads BAJUTSU_LAUNCH_ENV_* and sets them on
        # launchEnvironment, BAJUTSU_LAUNCH_ARGS as launchArguments, and opens BAJUTSU_DEEPLINK.
        launch_env, launch_args = self._launch_params(eff, pre, extra_env)
        runner_path = _resolve_runner(ios.xcuitest, device_type)
        forwarded_base = {
            # One generic runner drives whatever app the run targets, so it launches this
            # bundle id via XCUIApplication(bundleIdentifier:) rather than its own target app.
            "BAJUTSU_BUNDLE_ID": ios.bundle_id,
            **{f"BAJUTSU_LAUNCH_ENV_{k}": v for k, v in launch_env.items()},
            "BAJUTSU_LAUNCH_ARGS": json.dumps(launch_args),
        }
        if pre.deeplink is not None:
            forwarded_base["BAJUTSU_DEEPLINK"] = pre.deeplink

        def spawn() -> _Spawned:
            return self._spawn_runner(runner_path, forwarded_base, device_type)

        # A cold `xcodebuild test-without-building` spins up the XCTest host and launches the app
        # before the runner's server answers /health; on a loaded CI runner that first start well
        # exceeds the 10s default, so give it generous headroom (a warm start still returns at once).
        # A respawn (the Simulator already booted, the app installed) gets the tighter respawn ceiling
        # when the lane sets one, so a dead runner surfaces fast instead of paying the full cold budget.
        # A respawn is either a fresh env the pool built for one (`_respawn`) or *this* env cold-spawning
        # a second time in place — its warm resident died, `start` discarded it, and we are re-spawning
        # (`_cold_spawned_before`), the common mid-run-crash path a reused instance takes. But `erase`
        # shuts the Simulator down (this method's `_prepare_simulator` then reboots and reinstalls), so a
        # post-erase spawn is a genuine first-boot cold start — not a respawn onto a live Simulator — and
        # must keep the full cold ceiling even though this instance has cold-spawned before.
        is_respawn = (self._respawn or self._cold_spawned_before) and not pre.erase
        respawn_ceiling = _respawn_timeout() if is_respawn else None
        timeout = respawn_ceiling if respawn_ceiling is not None else _runner_startup_timeout()

        # A real device has no simctl to recover through — it is powered on out of band — so it keeps
        # the plain retry. Only the Simulator gets the recovery ladder.
        def recover(failure: _AttemptFailure) -> _Recovery | None:
            return self._recover_between_attempts(failure, eff, pre, permissions)

        spawned = _spawn_cold_with_retry(
            spawn, timeout=timeout, recover=_no_recovery if device_type == "device" else recover
        )
        # A later cold spawn on this same instance is an in-place respawn, so tighten its ceiling too.
        self._cold_spawned_before = True
        # Only the Simulator runner is kept warm; a real-device runner is torn down per lease.
        self._reusable = device_type != "device"
        self._warm_reuses = 0  # a fresh XCTest session: the app.launch()-cycle count starts over
        return spawned.driver

    def _recover_between_attempts(
        self,
        failure: _AttemptFailure,
        eff: Effective,
        pre: Preconditions,
        permissions: Mapping[str, str] | None,
    ) -> _Recovery:
        """Repair the Simulator a failed cold attempt leaves behind, so the retry spawns onto a live device.

        Times the repair proper and checks it against `_recovery_timeout()` once the rung returns — a
        bound on a *slow* recovery, not a hard ceiling: the budget is consulted only between rungs,
        and a rung whose call itself wedges (the exact CoreSimulator degradation this ladder exists
        to recover from) is interrupted by the per-call `simctl` deadline instead, surfacing as a
        device fault rather than a hang (BE-0363). Covers every rung that does return promptly,
        including the two that deliberately change nothing: the probe that opens the ladder is itself a
        subprocess, so a host slow enough to blow the bound merely answering `simctl list` is a host the
        run must give up on, whatever the rung then decided.

        Deliberately excludes the re-prep (`_finish_repair`) that a reboot or replacement earns: that
        prep is the same erase/locale-pin/install work the first bring-up already runs with no bound
        of its own, so a device that demonstrably came back should not fail the run over how long its
        reinstall took.

        Raises:
            DeviceError: if the device cannot be repaired — no replacement can be created, or the repair
                overran `_recovery_timeout()`. A device that will not come back is a device fault, not a
                flaky spawn, so it fails the run rather than funding another doomed attempt.
        """
        started = time.monotonic()
        recovery = self._recovery_rung(failure, eff)
        self._check_recovery_budget(started, recovery.note)
        if recovery.fresh_budget is not None:
            # Only a reboot or a replacement earns a fresh budget, and both leave the device
            # freshly booted but not yet re-prepared — the state the caller's own cold spawn
            # is about to run `xcodebuild` against.
            self._finish_repair(eff, pre, permissions)
        return recovery

    def _recovery_rung(
        self,
        failure: _AttemptFailure,
        eff: Effective,
    ) -> _Recovery:
        """Probe the device and run the one rung its state and this failure call for.

        The rung is chosen by what the attempt failed *on*, because the failures differ in what they
        say about the device. A device simctl no longer lists has to be replaced outright; an
        `xcodebuild` that exited on its own says nothing about the device, so the app it may have left
        running is all there is to clean up; an app-launch timeout or a wait that reached its ceiling
        says the device stopped honouring automation, which only a reboot clears.

        Both repairs that actually change the device earn the same fresh ceiling. A **reboot** ends with
        the device booted (`bootstatus` waited for it) and `_finish_repair` about to reinstall onto it; a
        **replacement** is a device that has never run anything. Either way `_finish_repair`'s reinstall
        is about to run `xcodebuild test-without-building` against a device in a genuine first-boot
        state — fresh CoreSimulator caches, a restarted SpringBoard, no prior XCTest host this boot — the
        same state `_spawn_cold` already gives the full cold ceiling on its own erase path regardless of
        this instance's respawn history (see the `is_respawn` comment above). Handing a rebooted respawn
        only the tighter respawn ceiling it started on would size that first bring-up for a warm reuse it
        is not. A reboot that could not even confirm the device left `Booted` earns neither: nothing
        changed, so it carries a note and no fresh budget like the two do-nothing rungs below.

        Returns the note and fresh readiness ceiling `_spawn_cold_with_retry` folds into its diagnostics
        and budget; a rung that changed nothing about the device carries a note and no fresh budget.
        """
        probe = simctl.device_available(self._udid, self._run)
        if probe is False:
            note = self._replace_vanished_device(eff)
            return _Recovery(note, fresh_budget=_runner_startup_timeout())
        if probe is None:
            # The listing itself failed, so nothing is known about the device. Repairing on a guess
            # could reboot a healthy device — or replace one that never vanished — so this rung
            # deliberately does nothing beyond what the discard already did.
            return _Recovery("could not probe the device; left it as it is")
        if failure.kind == "process-exit":
            # `xcodebuild` gave up by itself and fast, which is the transient blip BE-0319's retry was
            # written for. The discard has already terminated the app, so the device needs nothing.
            return _Recovery("xcodebuild exited on its own; device left booted")
        return self._reboot_device()

    def _check_recovery_budget(self, started: float, note: str) -> None:
        """Fail the run when a recovery rung overran its wall bound.

        Checked after the rung rather than inside it: the simctl steps are blocking calls this cannot
        preempt, so the bound catches a device that took absurdly long to come back rather than
        cutting a boot short. A rung that overran has spent the budget the retry would need anyway.

        Raises:
            DeviceError: if the rung took longer than `_recovery_timeout()`.
        """
        spent = time.monotonic() - started
        if spent > _recovery_timeout():
            raise simctl.DeviceError(
                f"Simulator recovery exceeded {_recovery_timeout()}s (spent {spent:.1f}s): {note}"
            )

    def _await_boot(self) -> None:
        """Block until the device this environment holds has finished booting (BE-0359).

        `simctl boot` returns once the boot has been *requested*, so every caller that boots a device
        and then uses it needs this: installing an app or starting `xcodebuild` against a SpringBoard
        that is still coming up is what produces the `Timed out attempting to launch app` signature
        the recovery ladder above exists to repair after the fact. The wait is deliberately unbounded,
        like the install and permission steps beside it — a device that takes 80 seconds to come up
        has not failed, and BE-0363 owns the question of a deadline for `simctl` calls as a whole.

        Private to this class because `Env.boot()` suppresses its own failure: a future caller that
        boots outside these call sites stays outside the wait, the same boundary the recovery ladder's
        two rungs already lived within.

        Raises:
            DeviceError: if `bootstatus` itself fails — a device that will not finish booting.
        """
        try:
            self._run(simctl.bootstatus_cmd(self._udid), None)
        except subprocess.CalledProcessError as exc:
            raise simctl.device_error(exc) from exc

    def _reboot_device(self) -> _Recovery:
        """Shut the Simulator down and boot it back up. `_finish_repair` re-establishes scenario state.

        `Env.shutdown()` suppresses its own failure — though no longer a deadline it exceeded, which
        BE-0363 lets out so this rung raises rather than reporting a reboot it never performed —
        right for the benign "already shutting down"
        case it was written for, but a CoreSimulator wedged enough to stop honouring automation is
        exactly where `simctl shutdown` itself fails. Left unchecked, that failure is invisible: `boot`
        no-ops on a device that never left `Booted`, `bootstatus -b` sees it already booted and returns
        at once, and the retry would spawn onto the same still-wedged device with a fresh ceiling
        instead of the exhausted shared one — the pre-recovery stall, plus one extra ceiling of wall
        time. Reading the device's booted state back after `shutdown` is what tells a real reboot from
        a no-op, the same read-back `_pin_system_locale` already does for a `defaults write` that can
        exit 0 without surviving the shutdown. The read-back is itself three-valued, like
        `device_available`: the same wedged host that makes `shutdown` no-op can also make `simctl
        list devices booted` fail outright, and an unreadable listing confirms a reboot no more than
        a listing that still shows the device up does.
        """
        e = simctl.Env(self._udid, run=self._run)
        e.shutdown()
        if simctl.device_booted(self._udid, self._run) is not False:
            _logger.warning(
                "Simulator %s did not shut down; the reboot rung had no effect", self._udid
            )
            return _Recovery(f"{self._udid} would not shut down; left as it is")
        e.boot()
        self._await_boot()
        _logger.warning("rebooted Simulator %s after a failed cold runner spawn", self._udid)
        return _Recovery(f"rebooted {self._udid}", fresh_budget=_runner_startup_timeout())

    def _finish_repair(
        self, eff: Effective, pre: Preconditions, permissions: Mapping[str, str] | None
    ) -> None:
        """Re-prepare the device a reboot or replacement just brought back, outside the recovery bound.

        The erase / locale-pin / install cycle this runs is exactly what the first cold bring-up
        already pays with no timeout of its own (`_spawn_cold`), so holding the repaired device to
        the same policy here — rather than folding it into `_check_recovery_budget` — keeps a device
        that genuinely recovered from failing the run over a slow but successful reinstall.
        """
        try:
            self._prepare_simulator(eff, pre, permissions, cold=True)
        except subprocess.CalledProcessError as exc:
            raise simctl.device_error(exc) from exc

    def request_device_replacement(self) -> None:
        """Escalate the next `start` to a replacement device (BE-0354).

        The run pipeline calls this when a crash retry that already forced an erase crashed again, or
        when the attempt's video-start confirmation stalled — both say the degradation lives in the
        device's services rather than its data, which an erase resets and a replacement does not
        inherit. Recorded rather than acted on: the swap belongs to the next bring-up, where
        `replaced_device` then reports it and the pool re-keys everything it holds by udid.
        """
        self._replacement_requested = True

    def _replace_degraded_device(self, eff: Effective) -> None:
        """Move this environment onto a fresh Simulator, quarantining the degraded one (BE-0354).

        The rung above the crash retry's forced erase. An erase resets the device's data, so it
        recovers the app-data corruption class; the failure this serves is the other one — a Simulator
        whose capture services have wedged under a runner whose HTTP server still answers, which the
        erase was measured not to clear. A device that has never run anything cannot inherit that
        state, and the machinery to mint one already exists for the vanished-device rung.

        The degraded device is shut down and, because the pool follows `replaced_device` onto the
        replacement, never freed back to the queue — the same quarantine a vanished device gets today.
        The shutdown is best effort (`Env.shutdown` suppresses its own failure, and a deadline it
        exceeded is suppressed here, since BE-0363 deliberately lets that one out of the module): a
        CoreSimulator wedged enough to refuse or to hang on it is exactly why the run is leaving this
        device, so failing here would only replace one loud failure with a less useful one — and
        would abandon a replacement already confirmed creatable. Unlike `_reboot_device`, which keeps
        the timeout because the ladder still has a rung to choose, this site is past that decision.
        It runs only once a replacement is known to be
        creatable, so a host that cannot mint one leaves the degraded device up for the caller's
        fallback rather than turned off on the way to a loud failure.

        Raises:
            DeviceError: as `_replacement_target` does, before anything about the device changes.
        """
        old = self._udid
        # Trigger-neutral wording: the escalation also fires on a stalled video start, from the
        # *first* crash, where no erase was ever forced — and this clause reaches the operator on the
        # path where no replacement could be made, so it must not claim one was.
        device_type = self._replacement_target(eff, why="needs replacing after a crash")
        try:
            simctl.Env(old, run=self._run).shutdown()
        except simctl.DeviceTimeout as exc:
            _logger.warning("quarantining Simulator %s: %s; replacing it anyway", old, exc)
        note = self._create_replacement(device_type)
        # The spawn that follows is a genuine first bring-up — a device just created and booted, with
        # no app installed and no XCTest host this boot — so it earns the full cold readiness ceiling,
        # not the tighter respawn one this environment's history would otherwise select. Exactly the
        # reasoning `_spawn_cold`'s own erase path already applies.
        self._respawn = self._cold_spawned_before = False
        _logger.warning(
            # Which signal selected this rung is the pipeline's to log; saying "after a forced erase"
            # here would misreport the stall-triggered path, which escalates from the first crash.
            "Simulator %s could not be recovered in place; shut it down and %s",
            old,
            note,
        )

    def _replace_vanished_device(self, eff: Effective) -> str:
        """Create a Simulator to take the place of one simctl no longer lists. `_finish_repair` preps it.

        Observed on CI as an `xcodebuild` exiting with "Unable to find a device matching the provided
        destination specifier" while the host's whole iOS device set had gone. Retrying onto a device
        that no longer exists cannot work, so the run continues on a replacement — and `bootstatus`
        runs here so a fresh device's first boot is paid before `_finish_repair`'s prep rather than
        inside the next attempt's readiness ceiling.

        Raises:
            DeviceError: as `_replacement_target` does — no device type to clone, or no `appPath`.
        """
        old = self._udid
        note = self._create_replacement(self._replacement_target(eff, why="is gone"))
        _logger.warning("Simulator %s vanished from CoreSimulator; %s", old, note)
        return f"{old} vanished; {note}"

    def _replacement_target(self, eff: Effective, *, why: str) -> str:
        """The device type a replacement would be cloned from, or a loud failure. Changes nothing.

        Held apart from `_create_replacement` so both rungs can find out whether a replacement is
        possible *before* they touch the device they are leaving: the crash-retry rung shuts the
        degraded device down, and turning it off on the way to a failure would leave the caller's
        fallback worse off than no escalation at all. `why` names the caller's case, since an operator
        reading either message needs to know which rung ran.

        Raises:
            DeviceError: if no replacement can be created — chiefly a host that lost its iOS runtimes
                along with the device, where there is nothing left to run on — or if the target
                configures no `appPath`, since a blank replacement would have no app to install.
        """
        old = self._udid
        # A replacement is a blank device, so without an `appPath` to install onto it the retry has
        # nothing to launch: say so here rather than spending the create, the boot and a full cold
        # ceiling proving it.
        if require_ios(eff).app_path is None:
            raise simctl.DeviceError(
                f"Simulator {old} {why} and this target configures no appPath, so a replacement "
                "device would have no app to launch; set appPath so the recovery can install it, "
                "or bring a fresh Simulator up with the app installed and re-run"
            )
        device_type = self._replacement_device_type(eff)
        if device_type is None:
            raise simctl.DeviceError(
                f"Simulator {old} {why} and no device type matching {eff.device} is available to "
                "replace it; the host's Simulator runtimes may be gone, or the configured device "
                "name doesn't exactly match a simctl device type"
            )
        return device_type

    def _create_replacement(self, device_type: str) -> str:
        """Create, boot, and adopt a fresh Simulator of `device_type`; the diagnostic note.

        Shared by both rungs that replace a device — the vanished-device rung of a failed cold spawn
        (BE-0344) and the crash retry's escalation above the forced erase (BE-0354) — so the naming,
        the runtime cloning, and the `replaced_device` bookkeeping the pool re-keys on cannot drift
        between them. `bootstatus` runs here so a fresh device's first boot is paid before the
        caller's prep rather than inside a readiness ceiling.

        The replacement **outlives the run**: nothing here or in the pool's teardown deletes it. That is
        deliberate on two counts. It is a healthy device a later run can simply lease, where deleting it
        would make the next run pay another creation on a host that has already shown it loses devices;
        and it is the evidence that this happened at all, which a run that deleted its own replacement
        would leave only in a log line. The cost is one new `bajutsu-recovered-*` device per *run* that
        replaces a device — not one per loss, since nothing here or later adopts an
        existing replacement: the `booted` alias self-heals (a replacement is left booted, so it resolves
        next time), but a config or `--udid` pinned to a permanently-vanished device mints a fresh,
        identically-named replacement on every run instead of converging on the one already created.
        That residue is also why the crash-retry rung is scoped to an unpinned run (see
        `bajutsu/common/backends.py`'s `device_replacement_supported`).
        Cleared by `xcrun simctl delete unavailable`, by deleting the `bajutsu-recovered-*` devices —
        which the name below makes greppable — or by re-pointing the pinned config at the replacement.

        Raises:
            DeviceError: if the fresh device's first boot never completes.
        """
        old = self._udid
        # The model comes first because two consumers read a device's name as its human model: the
        # report's device row, and `serve`'s capability inventory, which takes the `iphone` / `ipad`
        # class token out of it by substring. The `bajutsu-recovered-<udid>` suffix is what lets an
        # operator reading `simctl list` afterwards tell which recovery minted which device.
        name = f"{simctl.device_type_label(device_type)} (bajutsu-recovered-{old})"
        # Pinning the replaced device's own runtime keeps the replacement on the same iOS version a
        # scenario was written against; `create_device` retries unpinned if that runtime is itself
        # gone, so this only trades away version fidelity in the case it has to.
        requested_runtime = self._device_runtime_id
        replacement = simctl.create_device(
            device_type, self._run, name=name, runtime=requested_runtime
        )
        self._udid = replacement
        self._replaced_from = old
        self._pinned_locale = None  # a fresh device: nothing has pinned its SpringBoard yet
        self._installed_app_digest = None  # a fresh device: nothing is installed on it yet
        # Cleared, not set: `create_device` falls back to an unpinned create when the pinned runtime
        # is gone, so what it got is not necessarily what was asked for. `_finish_repair`'s prep
        # re-reads both from the replacement itself.
        self._device_type_id = self._device_runtime_id = None
        self._await_boot()
        return (
            f"created replacement {replacement} "
            f"({device_type}, requested runtime {requested_runtime or 'any'})"
        )

    def _replacement_device_type(self, eff: Effective) -> str | None:
        """The device type a replacement is created from, or None when no matching type is available.

        Prefers the vanished device's own type, so the replacement is the device the run was written
        against; falls back to the configured model, then — only for an iPhone target — to whichever
        iPhone this host's Xcode ships, since the config may name a model a later Xcode dropped and
        any iPhone beats failing the run. The fallback is scoped to an iPhone target because it is not
        scoped to a device *class*: `eff.device` matches simctl's device-type name exactly, and an
        iPad's name carries parentheses (`iPad Pro (12.9-inch) (6th generation)`), so a near-miss is
        ordinary rather than exotic — substituting an iPhone for a missed iPad would finish the run on
        a layout the scenario was never written against, silently.
        """
        if self._device_type_id is not None:
            return self._device_type_id
        configured = simctl.device_type_identifier(eff.device, self._run)
        if configured is not None:
            return configured
        if "iphone" not in eff.device.lower():
            return None
        return simctl.newest_iphone_device_type(self._run)

    def _spawn_runner(
        self, runner_path: Path, forwarded_base: Mapping[str, str], device_type: str
    ) -> _Spawned:
        """Spawn one `xcodebuild test-without-building` runner and hand back its liveness handles.

        Allocates a fresh port per attempt (so successive respawns leave separate capture files),
        patches the .xctestrun with the forwarded env, captures the runner's output, and builds the
        channel driver. Returns a `_Spawned` reading from this environment's just-set state — the
        surviving attempt's state is the environment's, which warm reuse and teardown then own.
        """
        self._runner_port = _allocate_port()
        forwarded = {"BAJUTSU_RUNNER_PORT": str(self._runner_port), **forwarded_base}
        # `xcodebuild` does not pass its own environment through to the test-runner process
        # inside the Simulator, so the runner reads these from the .xctestrun's per-target
        # TestingEnvironmentVariables instead. Patch a private copy and run that.
        self._patched_runner = _patch_xctestrun_env(runner_path, forwarded)
        # Read the runner app's own bundle id off the same plist while it is resolved, so a discard
        # can terminate the guest process `xcodebuild`'s process group cannot reach. Scoped to the
        # Simulator like `_bundle_id`: a real device has no simctl to terminate through.
        self._runner_bundle_ids = (
            _runner_host_bundle_ids(runner_path) if device_type != "device" else ()
        )
        runner_out = self._open_runner_output()
        # One probe per spawn over this attempt's capture (the port keys the file), so a retry starts
        # from an empty offset on its own log. Built before the driver below, which reads it through
        # `_runner_alive`, and shared with the cold gate — a second instance would race this one for
        # the marker (BE-0354).
        self._run_ended = _run_ended_probe(self._runner_log)
        bundle = self._result_bundle_path()
        try:
            proc = subprocess.Popen(
                [  # noqa: S607 — xcodebuild resolved on PATH; requires Xcode
                    "xcodebuild",
                    "test-without-building",
                    "-xctestrun",
                    str(self._patched_runner),
                    "-destination",
                    # Simulator vs real device (BE-0238); `_destination` validates the udid inline
                    # before it lands on the argv, the same defense-in-depth simctl applies.
                    _destination(device_type, self._udid),
                    *(
                        (
                            "-resultBundlePath",
                            str(bundle),
                            # `-collect-test-diagnostics` defaults to `on-failure`, and on a failure
                            # that means `xcodebuild` embeds a whole sysdiagnose — a Simulator
                            # `system.logarchive` — under the bundle's `Staging/…/Diagnostics/`.
                            # Measured at 163 MB for one spawn, which is both the artifact size and a
                            # disk write on a host that had 189 MB of memory left. BE-0361 rejected
                            # exactly that collection in favour of targeted extracts, so asking for a
                            # bundle must not smuggle it back in: the runner log here plus the CI
                            # action's own bounded `simctl diagnose` already cover the failure.
                            "-collect-test-diagnostics",
                            "never",
                        )
                        if bundle is not None
                        else ()
                    ),
                ],
                env={**os.environ, **forwarded},
                stdout=runner_out,
                # Fold stderr into the same sink so a crash's cause is captured in order.
                stderr=subprocess.STDOUT,
                # Own process group, so teardown reaches the XCTest-host plumbing `xcodebuild`
                # spawned rather than only `xcodebuild` itself — a signal that stops at the parent
                # leaves children holding the device's automation session (`_discard_runner`). The
                # trade: this also takes the runner out of the CLI's foreground process group, so a
                # terminal Ctrl-C no longer reaches `xcodebuild` directly — cleanup then depends on
                # Python's own exception handling (`_spawn_cold_with_retry`'s and `lease()`'s
                # `except BaseException`, `run.py`'s `finally: shutdown()`), which still runs
                # `_discard_runner`'s sweep on a single interrupt or any other exception, covering the
                # common case. A bare `SIGTERM` is covered as of BE-0370: `bajutsu run` answers it
                # by asking the run to stop at its next safe boundary, so the same Python cleanup
                # runs and this teardown happens. What remains uncovered is a second interrupt
                # landing mid-teardown, a `SIGKILL`, and a `SIGTERM` that outlives the grace period
                # BE-0370's handler enforces — each would orphan `xcodebuild` and its children in
                # their own session, a narrower version of the wedged-Simulator failure this unit
                # exists to clear, left as a known gap rather than closed with signal-handling
                # machinery this module does not otherwise need.
                start_new_session=True,
            )
        except OSError as exc:
            raise simctl.DeviceError(f"failed to start xcodebuild: {exc}") from exc
        finally:
            # `Popen` dups the fd into the child at spawn, so the parent's copy is no longer needed;
            # closing on the error path too means a failed spawn never leaks the log handle.
            runner_out.close()
        self._runner_proc = proc
        self._runner_spawned_at = time.time()
        self._crash_snapshotted = False
        _logger.info("xcuitest runner output → %s", self._runner_log)

        driver = backends.make_driver(
            self._actuator,
            self._udid,
            runner_port=self._runner_port,
            runner_alive=self._runner_alive,
            on_stall=self._capture_stall,
            device_os=self._device_os(),
            zorder=self._zorder,
        )
        # `log_tail` / `discard` reach live environment state (`self._runner_log` / `self._runner_proc`);
        # they are valid only until the next `spawn()` overwrites it, which the strictly sequential
        # retry loop (spawn → await → log_tail → discard → next spawn) guarantees. A failed cold
        # attempt keeps its log (`keep_log`) and skips the mid-run-crash warning (its reason is
        # already in the raised error).
        return _Spawned(
            driver=driver,
            ready=cast(base.BackendLifecycle, driver).health_ready,
            poll=proc.poll,
            log_tail=self._runner_log_hint,
            discard=lambda: self._discard_runner(warn_on_crash=False, keep_log=True),
            run_ended=self._run_ended,
        )

    def _resume_warm(
        self,
        eff: Effective,
        pre: Preconditions,
        extra_env: Mapping[str, str] | None,
        permissions: Mapping[str, str] | None,
        driver: base.Driver,
    ) -> base.Driver:
        """Reuse the live runner: re-prep the device and relaunch the app, skipping the spawn (BE-0291).

        The same app-only restart `device_relauncher` does within a lease — terminate, relaunch with
        this scenario's env / args / locale, and open its deeplink — now applied across leases, so the
        runner (which drives whatever app is launched and holds no scenario state) is reused. The
        caller has already confirmed the runner is healthy (and `_reusable` is already set) and that the
        scenario does not erase, so the per-scenario device reset (`reinstall` / permissions) still runs
        before the app launches and a reused runner never weakens the isolation a cold lease gives
        (Unit 2). `driver` is the channel the health probe already built on the runner's port, returned
        as-is; the app-readiness wait is launch_driver's, the same as the cold path.
        """
        ios = require_ios(eff)
        self._prepare_simulator(eff, pre, permissions, cold=False)
        launch_env, launch_args = self._launch_params(eff, pre, extra_env)
        e = simctl.Env(self._udid, run=self._run)
        try:
            e.terminate(ios.bundle_id)
            e.launch(ios.bundle_id, launch_args, launch_env)
            if pre.deeplink is not None:
                e.openurl(pre.deeplink)
        except subprocess.CalledProcessError as exc:
            raise simctl.device_error(exc) from exc
        self._warm_reuses += (
            1  # one more app.launch() cycle on this runner (toward the reuse budget)
        )
        return driver

    def _prepare_simulator(
        self,
        eff: Effective,
        pre: Preconditions,
        permissions: Mapping[str, str] | None,
        *,
        cold: bool,
    ) -> None:
        """The simctl device prep shared by the cold spawn and the warm resume.

        `cold` runs the full device reset (erase → boot); a warm resume skips it — the Simulator is
        already booted under the live runner, and `erase` would shut it down (so a warm resume never
        carries erase). Both reinstall the app and (re)apply permissions, so a reused runner starts
        each scenario from the same known state a cold lease does (BE-0291 Unit 2).
        """
        ios = require_ios(eff)
        e = simctl.Env(self._udid, run=self._run)
        try:
            if cold:
                if pre.erase:
                    e.shutdown()
                    e.erase()
                    # The device just lost every app it had; nothing tracked survives to compare
                    # against (BE-0407 Unit 14).
                    self._installed_app_digest = None
                e.boot()
                # Both the cold bring-up and the ladder's re-preparation pass through here, so this
                # one wait covers every caller that boots and then installs onto the device.
                self._await_boot()
                # Remember what kind of device this is while it is still listed: a replacement is
                # cloned from this type and runtime, and by the time one is needed the device is gone.
                if self._device_type_id is None:
                    resolved = simctl.device_type_of(self._udid, self._run)
                    self._device_type_id, self._device_runtime_id = resolved or (None, None)
                self._pin_system_locale(e, pre.resolved_locale(eff.locale))
            clean_reinstall = pre.reinstall == "clean" and not pre.erase
            if ios.app_path:
                app_path = Path(ios.app_path)
                if not app_path.exists():
                    raise simctl.DeviceError(
                        f"appPath not found: {ios.app_path} (build the app first)"
                    )
                if clean_reinstall:
                    e.uninstall(ios.bundle_id)
                    # The uninstall above always requires a fresh install right after it, whatever
                    # was tracked before — nothing survives to compare against.
                    self._installed_app_digest = None
                # `reinstall: overwrite` keeps the app's data container (BE-0407 Unit 14): if the
                # bundle put there is byte-identical to what this environment already installed,
                # `install` would rewrite the same binary over itself for nothing. `reinstall: clean`
                # never reads this — it always installs fresh right after the uninstall above, so
                # computing a digest for it would only pay a tree hash that buys nothing.
                digest = _products_digest(app_path) if pre.reinstall == "overwrite" else None
                if digest is None or digest != self._installed_app_digest:
                    e.install(ios.app_path)
                    self._installed_app_digest = digest
            if clean_reinstall:
                # Neither `uninstall` nor `install` touches TCC.db — verified on-device, only
                # `erase` above does — so `clean` must reset permissions itself, the same way
                # `adb.Env.clear` resets grants on the equivalent Android path (also run at this
                # level, independent of whether an install happened this lease). A target with no
                # `appPath` here (an already-installed build a provider handed over) still needs
                # this: the bundle it names was installed by some earlier lease, and `clean` still
                # promises a known permission state for it.
                e.reset_permissions(ios.bundle_id)
            # Set permission state after install (the grant targets an installed bundle) but before
            # the app launches, so a prompt never blocks it (BE-0276). `clean` resets TCC itself just
            # above; `reinstall: overwrite` never resets permissions on its own (nor does `erase`
            # re-grant anything, nor does the digest skip above change this) — a scenario that must
            # start from a known state under `overwrite` names every service it cares about here.
            if permissions:
                e.apply_permissions(ios.bundle_id, permissions)
        except subprocess.CalledProcessError as exc:
            raise simctl.device_error(exc) from exc

    def _pin_system_locale(self, e: simctl.Env, locale: str) -> None:
        """Force the Simulator's *system* language to `locale`, so SpringBoard renders it too (BE-0320).

        `locale` reaches the app through its own launch arguments (`simctl.locale_args`), but
        SpringBoard — which owns the permission prompts `handleSystemAlert` taps by label — is a
        separate process those arguments never reach. Writing the device's global domain needs a
        booted device (`simctl spawn`), and a running SpringBoard does not pick the value up live,
        so a write is followed by one more boot cycle before the caller installs and launches. The
        common case (already pinned) costs one read and no extra boot.

        The read-back after the reboot is what makes the pin a fact rather than a hope: a `defaults
        write` can exit 0 and still not survive the shutdown. Only a confirmed pin is remembered —
        `_pinned_locale` gates warm reuse, so recording an unconfirmed one would carry the doubt
        across every later lease instead of re-checking on the next cold spawn.

        One failure that read-back cannot see is a reboot that never happened, which is why the
        shutdown is confirmed separately (BE-0359). `Env.shutdown()` suppresses its own failure, so a
        CoreSimulator wedged enough to refuse it leaves SpringBoard on the old language while the
        plist reads back exactly what was written — the value the write changed, not the one
        SpringBoard loaded. Reading the device's booted state back is the only thing that separates
        the two, three-valued as `_reboot_device` reads it, and a device that did not go down leaves
        the pin unrecorded. It only ever downgrades a confirmation: a definite *mis*match still fails
        the run below, since a device reading back another locale is wrong however it got there. The
        boot and its wait run either way, because an unreadable listing may well be a device that
        *did* shut down, and the caller is about to install onto it.

        Raises:
            DeviceError: if the reboot demonstrably left the device on another locale — the run would
                otherwise proceed against an alert language nothing predicts.
        """
        self._pinned_locale = None  # not pinned until the write below is confirmed
        if not e.pin_system_locale(locale):
            self._pinned_locale = locale  # the read already confirmed it; nothing was written
            return
        # The only line that says the pin fired. Without it a CI log cannot answer whether a job even
        # reaches the reboot below, which is what decides how much of this path applies there.
        _logger.info(
            "pinning Simulator %s's system locale to %r; rebooting so SpringBoard renders it "
            "(BE-0320)",
            self._udid,
            locale,
        )
        e.shutdown()
        # Read back before the boot, while a refused shutdown is still distinguishable: `boot` is
        # about to make the device booted either way.
        went_down = simctl.device_booted(self._udid, self._run) is False
        e.boot()
        self._await_boot()
        confirmed = e.system_locale_matches(locale)
        if confirmed is False:
            raise simctl.DeviceError(
                f"failed to pin the Simulator's system locale to {locale!r}; "
                "system-alert button labels would not be deterministic (BE-0320)"
            )
        if confirmed is None:
            # Nothing was observed to be wrong, so the run proceeds — but the pin is unconfirmed, so
            # it is not recorded: the next lease cold-spawns and re-checks rather than reusing a
            # runner on the strength of a write we could not read back.
            _logger.warning(
                "could not read the Simulator's global domain back after pinning it to %r; "
                "the run continues, but warm-runner reuse is disabled until a spawn confirms it "
                "(BE-0320)",
                locale,
            )
            return
        if not went_down:
            # The read-back just passed on a device whose restart was never confirmed: it reads the
            # value the write changed, not the one SpringBoard loaded, so it confirms the write
            # rather than the pin. Nothing is known to be wrong, so the run proceeds — but an
            # unconfirmed pin is not recorded, exactly as an unreadable one above is not. The wording
            # below says "could not confirm" rather than "did not", because `went_down` folds an
            # unreadable listing in with a refused shutdown and only the latter is a wedged device.
            _logger.warning(
                "could not confirm Simulator %s shut down after its system locale was pinned to "
                "%r, so SpringBoard may still render the previous language; the run continues, but "
                "warm-runner reuse is disabled until a spawn confirms the pin (BE-0359)",
                self._udid,
                locale,
            )
            return
        self._pinned_locale = locale

    def _launch_params(
        self, eff: Effective, pre: Preconditions, extra_env: Mapping[str, str] | None
    ) -> tuple[dict[str, str], list[str]]:
        """The launch env and args for this scenario (scenario locale overrides the config default)."""
        launch_env = {**eff.launch_env, **pre.launch_env, **(extra_env or {})}
        launch_args = [
            *eff.launch_args,
            *pre.launch_args,
            *simctl.locale_args(pre.resolved_locale(eff.locale)),
        ]
        return launch_env, launch_args

    def _runner_alive(self) -> bool:
        """Whether the runner can still answer on its port: its process runs *and* its test run has not ended.

        The crash-recovery layer reads this to split a recoverable blip from a dead runner: a runner
        that cannot come back will never answer `/health` again on its port, so recovery fails fast
        instead of polling it for the whole window (a runner merely unreachable but alive stays
        BE-0287's recoverable case). `poll()` is `None` while the process runs, an exit code once it
        has ended.

        The process handle alone is not enough (BE-0354). `xcodebuild` outlives its own test run by a
        long way: after a mid-run crash, XCTest restarts the in-Simulator host and re-runs zero tests,
        so the suite reports its result and the parent lives on — the blind spot BE-0305's
        fault-injection measurements recorded, where every recovery episode waits out its full window
        on a runner whose port will never bind again. The capture already names that state, and the
        cold-spawn gate has string-matched the same markers since BE-0319, so the probe reading it
        answers here too. It is the *same* probe instance the gate uses, latched: this predicate is
        re-asked throughout each recovery episode — once when the crash is declared and then once a
        second while the recovery wait runs (BE-0360) — while the probe advances a private offset and
        reports a marker only from the window that first contains it. A future Xcode that rewords the
        markers degrades this to the process-only check it replaces, never to a false "gone".
        """
        if self._runner_proc is None or self._runner_proc.poll() is not None:
            return False
        return self._run_ended() is None

    def _capture_stall(self) -> None:
        """Let the channel's crash declaration capture the device state behind it (BE-0361 unit 2).

        The environment supplies this rather than the driver reaching for it, because the udid the
        capture screenshots is here and deliberately never reaches the channel — the same shape
        `_runner_alive` already uses. Naming the trigger is this side's business too, so the channel
        never passes a string that becomes a directory. Opt-in and bounded on the other side; unset,
        it does nothing.
        """
        stall_diagnostics.capture("runner-crash", stall_diagnostics.simulator_probes(self._udid))

    def _device_os(self) -> DeviceOS | None:
        """The parsed OS version of the device this environment drives, for the driver (BE-0358).

        Derived from the runtime identifier the cold prep already captures for device cloning, so it
        costs no extra `simctl` call and follows a device replacement (which clears it, and whose
        `_finish_repair` re-reads it from the replacement). None on a real device, and before the
        first cold prep has read one: the driver then reports no OS rather than a guessed one.
        """
        if self._device_runtime_id is None:
            return None
        return device_os.parse(simctl.runtime_label(self._device_runtime_id))

    def _healthy_resident_driver(self) -> base.Driver | None:
        """The driver for the warm runner if it is up and answering `/health`, else None (BE-0291 Unit 4).

        A dead process, or a live one that fails a bounded `/health` probe, returns None: the caller
        respawns cold. The known failure is the runner crashing after repeated `app.launch()` cycles
        (docs/architecture.md), so this stays cheap and never waits the cold ceiling. The probed driver
        is returned (not rebuilt) so a warm resume reuses this same channel on the runner's port.
        """
        if self._runner_proc is None or self._runner_proc.poll() is not None:
            return None
        from bajutsu.common.drivers.xcuitest import XcuitestChannelError

        driver = backends.make_driver(
            self._actuator,
            self._udid,
            runner_port=self._runner_port,
            runner_alive=self._runner_alive,
            on_stall=self._capture_stall,
            device_os=self._device_os(),
            zorder=self._zorder,
        )
        try:
            cast(base.BackendLifecycle, driver).await_ready(timeout=_WARM_HEALTH_TIMEOUT)
        except XcuitestChannelError:
            return None  # wedged / unreachable — treat as a cache miss and respawn
        return driver

    def _open_runner_output(self) -> IO[bytes]:
        """Open the sink for the runner subprocess's combined output, capturing by default (BE-0319 unit 1).

        A cold spawn always captures, so the first CI flake is diagnosable without a human pre-arming
        `BAJUTSU_XCUITEST_RUNNER_LOG`; that variable now only *overrides the directory*. An env-unset
        capture goes to `_DEFAULT_RUNNER_LOG_DIR` and is marked ephemeral (teardown prunes it), while
        an explicit directory is kept. Sets `_runner_log` to the file it opened and returns it as the
        sink for `Popen`'s `stdout`.
        """
        log_dir_env = os.environ.get(_RUNNER_LOG_ENV)
        self._runner_log_ephemeral = not log_dir_env
        log_dir = Path(log_dir_env) if log_dir_env else _DEFAULT_RUNNER_LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        # Port keys the file to this spawn (a fresh ephemeral port each cold start), so successive
        # respawns on one device leave separate logs rather than overwriting the crashed one.
        self._runner_log = log_dir / f"runner-{self._udid}-{self._runner_port}.log"
        return self._runner_log.open("wb")

    def _result_bundle_path(self) -> Path | None:
        """Where this spawn's XCTest result bundle goes, or `None` when the operator didn't ask (BE-0361).

        Keyed like the runner log above — a fresh ephemeral port per spawn — so a respawn never
        overwrites the crashed predecessor's bundle. `xcodebuild` refuses to start at all when the
        path already exists ("Result bundle at path ... already exists"), which would turn a
        diagnostics feature into a spawn failure the one time an ephemeral port is recycled onto the
        same device, so a leftover at this exact key is cleared first. That is not the retention
        policy leaking: it can only ever remove a bundle whose spawn is long gone.
        """
        bundle_dir_env = os.environ.get(_RESULT_BUNDLE_ENV)
        if not bundle_dir_env:
            return None
        # Validated here, not only on the `-destination` argv below: this is a *new* boundary where the
        # udid composes a path that feeds a recursive delete, and the real-device path never runs the
        # simctl prep that would otherwise have vetted it.
        udid = simctl.validated_udid(self._udid)
        bundle = Path(bundle_dir_env) / f"result-{udid}-{self._runner_port}.xcresult"
        try:
            bundle.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(bundle, ignore_errors=True)
            if bundle.exists():
                # `ignore_errors` also hides a removal that did not happen — a plain file at this key,
                # or a tree `xcodebuild` wrote that is no longer removable. `xcodebuild` then refuses to
                # start at all, which is the spawn failure this clearing exists to prevent, so degrade
                # to "no bundles" rather than hand it an argv it will reject.
                _logger.warning("xcuitest: cannot clear the stale result bundle at %s", bundle)
                return None
        except OSError as exc:
            # An opt-in diagnostic must never be what fails a spawn — an operator typo naming a file
            # or a read-only mount degrades to "no bundles", the way the stall capture already does.
            _logger.warning("xcuitest: cannot prepare a result bundle at %s (%s)", bundle, exc)
            return None
        _logger.info("xcuitest runner result bundle → %s", bundle)
        return bundle

    def _runner_log_hint(self) -> str:
        """A trailer for the crash warning and the startup-failure error: the captured log's path and tail."""
        if self._runner_log is None:
            return ""
        tail = ""
        try:
            # Keep only the last N lines without materializing the whole (high-volume) capture: deque
            # streams the file line by line and drops all but its tail.
            with self._runner_log.open(errors="replace") as fh:
                tail = "".join(deque(fh, maxlen=_RUNNER_LOG_TAIL_LINES)).rstrip("\n")
        except OSError:
            pass  # the log may not exist yet on a spawn that failed before writing
        return f"; see {self._runner_log}" + (f"\n{tail}" if tail else "")

    def _runner_crashed(self) -> bool:
        """Whether the runner this environment holds died on its own, rather than at our request.

        The same two signals `_runner_alive` declares a mid-run crash on, asked from the teardown side:
        the `xcodebuild` leader exited, *or* its capture carries the marker saying the XCTest run
        ended while the process lingers. The second is the shape a stalled screenshot service
        produces — the dominant one in CI — and it leaves `poll()` answering `None` throughout, so a
        check that only looked at the process would miss exactly the crash this capture exists for.
        The run-ended probe latches (BE-0354), so asking it here does not consume the answer
        `_runner_alive` already read.
        """
        if self._runner_proc is None:
            return False
        return self._runner_proc.poll() is not None or self._run_ended() is not None

    def _capture_crash_if_observed(self) -> None:
        """Snapshot this spawn's crash, at most once however many sites observe it (BE-0421).

        Two do: the lease's own release, which is where a crashed runner is first let go, and any
        discard that later finds one. Capturing twice would regenerate evidence the releasing lease has
        already taken ownership of, leaving it on this environment for the next scenario on the device
        to be handed under its own id.
        """
        if not self._crash_snapshotted and self._runner_crashed():
            self._snapshot_crash_artifacts()

    def _snapshot_crash_artifacts(self) -> None:
        """Capture the just-crashed runner's evidence, at the moment the crash is observed (BE-0421).

        Called where a crashed runner is first let go — the lease's own release, and any discard that
        finds one — so the capture is already taken before the pipeline puts this device back in the
        pool. A `crash_artifacts()` that read the live fields lazily would race a concurrent worker
        whose next lease reuses this same environment instance (the pool keys its warm cache by udid)
        and respawns a runner on it, clearing both fields before the crashed scenario's retry loop
        ever gives up and asks.

        The two pieces have different windows, so they are captured differently. The runner log is
        read *here*, as a bounded tail, because the file it names can be pruned and the field
        re-pointed within milliseconds. Pruned genuinely: BE-0319's retention keeps an ephemeral
        capture only for the *exited* crash, so for a runner that lingers past its ended test run the
        copy taken here is the only thing that survives the next bring-up. The macOS crash report is
        only *identified* here —
        `ReportCrash` writes and symbolicates it asynchronously, after the faulting process is gone,
        so listing for it now would usually find nothing. Freezing the criteria is what lets the
        lookup wait: by the time `crash_artifacts()` runs, the live spawn timestamp and process handle
        may belong to a later spawn, but the frozen pair still names the one that crashed.
        """
        self._crash_snapshotted = True
        artifacts: list[tuple[str, bytes]] = []
        if self._runner_log is not None:
            try:
                with self._runner_log.open("r", errors="replace") as fh:
                    tail = "".join(deque(fh, maxlen=_CRASH_LOG_TAIL_LINES))
            except OSError as exc:
                # Best-effort throughout: a capture that failed to start writing, or a log already
                # gone, costs this one entry — never the failure being diagnosed.
                _logger.debug("xcuitest: cannot read the crashed runner's capture (%s)", exc)
            else:
                artifacts.append((self._runner_log.name, tail.encode()))
        self._last_crash_artifacts = artifacts
        self._last_crash_report_match = (
            (self._runner_spawned_at, self._runner_proc.pid)
            if self._runner_proc is not None
            else None
        )

    def take_crash_snapshot(self) -> Callable[[], list[tuple[str, bytes]]]:
        """Move the last observed crash's evidence to the releasing lease (BE-0421).

        Taken, not read: this environment is kept warm per device, so a snapshot left here would be
        visible to the next scenario that leases the device, and cleared by it. Handing ownership over
        at release makes the evidence lease-local, which is what keeps a `workers > 1` run from
        crossing one scenario's crash with another's — the same rule `video_start_stalled` follows.

        The thunk closes over the values taken here and still defers the `.ips` lookup to its own
        call, which is the whole reason the criteria were frozen rather than the bytes read: the
        pipeline asks after the retry loop has given up, which is the delay `ReportCrash` needs. The
        deferral is safe precisely because the frozen pair is now the thunk's, not the environment's.
        """
        artifacts, match = self._last_crash_artifacts, self._last_crash_report_match
        self._last_crash_artifacts, self._last_crash_report_match = [], None
        if match is None:
            return lambda: list(artifacts)
        return lambda: [*artifacts, *self._crash_reports(*match)]

    def _crash_reports(self, spawned_at: float, pid: int) -> list[tuple[str, bytes]]:
        """The `.ips` reports macOS wrote for the crashed `xcodebuild`, newest first and bounded.

        Matched on name and modification time, so an unrelated `xcodebuild` invocation's report left
        on the same host is not swept up. A report whose own JSON names a `pid` is additionally
        matched against the crashed one, which separates the several concurrent `xcodebuild` processes
        a multi-worker host runs; one that cannot be parsed is still taken on name and time alone,
        since a report format this cannot read is exactly when the raw file is worth having.
        """
        # A store that cannot be located, is missing — every platform but macOS — or is unreadable on a
        # sandboxed CI runner all read as empty rather than raising: no report is a first-class answer.
        reports_dir = _diagnostic_reports_dir()
        if reports_dir is None:
            return []
        reports: list[tuple[str, bytes]] = []
        for path in _reports_since(reports_dir, "xcodebuild-*.ips", spawned_at):
            try:
                content = path.read_bytes()
            except OSError as exc:
                _logger.debug("xcuitest: cannot read the crash report %s (%s)", path, exc)
                continue
            reported = _reported_pid(content)
            if reported is not None and reported != pid:
                continue
            reports.append((path.name, content))
            if len(reports) == _MAX_CRASH_REPORTS:
                break
        return reports

    def _discard_runner(self, *, warn_on_crash: bool = True, keep_log: bool = False) -> None:
        """Terminate the runner process and remove its patched .xctestrun (kills the warm resident).

        `warn_on_crash` logs the mid-run-crash diagnostic when the process had already exited on its
        own — right for a resident runner that vanished mid-run (the known app.launch()-cycle crash),
        but wrong for a cold-spawn startup failure, whose reason is already folded into the raised
        error, so `_spawn_cold_with_retry` clears it. `keep_log` leaves the capture on disk for a
        failed cold attempt (the evidence a loud failure points at); teardown of a healthy runner
        prunes it (BE-0319). A mid-run crash keeps its capture too — `warn_on_crash`'s hint tells the
        operator to "see <path>", so pruning that same file in this call would point at evidence that
        no longer exists.

        Teardown reaches the whole process group, then the app under test, then the runner app,
        because what this discards is handed straight to another spawn on the same device: a runner
        whose children survived, an app left mid-launch, or an XCTRunner still holding the device's
        automation session is state the next attempt inherits. That includes a
        leader that already exited on its own: `start_new_session` (`_spawn_runner`) makes it its own
        process group leader, so an XCTest-host child can outlive it and keep holding the device's
        automation session even though `xcodebuild` is gone — exactly the state a following spawn
        attempt would then have to spawn onto. The sweep runs unconditionally, even though `poll()`
        here may not be the call that first reaped the leader (`_runner_alive`, the warm-resume health
        check, and `start()`'s own reuse probe all poll the same handle, and each is the dominant path
        into this branch) — while any XCTest-host child outlives the leader, POSIX keeps the leader's
        pid reserved as that child's process-group id, so it cannot have been recycled regardless of
        who reaped it or when; only once the group is *empty* — the case where the sweep has nothing
        left to reach anyway — is the pid free to be reused, the same narrow window the `else` branch
        below already accepts unconditionally via `_terminate_process_group`.
        """
        # Before any of the teardown below moves the state a capture reads. Gated on `warn_on_crash`
        # rather than on the `crashed` flag computed further down: a cold-spawn-failure discard is
        # explicitly not a mid-run crash, and must not overwrite a real crash's evidence with its own
        # mid-retry-loop — while a runner that lingers past its ended test run, which that flag does
        # not see at all, is the very shape this capture exists for (BE-0421).
        if warn_on_crash:
            self._capture_crash_if_observed()
        crashed = False
        if self._runner_proc is not None:
            exited = self._runner_proc.poll()
            if exited is not None:
                # The leader is already gone — it exited on its own, not at our request. For a
                # resident runner that is the known app.launch()-cycle crash (see `_MAX_WARM_REUSES`
                # above for what this repeatedly surfaced in CI); log it (with the captured output)
                # so a run that died on a `Connection refused` shows *why* the channel vanished.
                crashed = warn_on_crash
                if warn_on_crash:
                    _logger.warning(
                        "xcuitest runner exited on its own (code %s) — a mid-run crash%s",
                        exited,
                        self._runner_log_hint(),
                    )
                # No terminate() — the leader's pid is already reaped — but `start_new_session` made it
                # its own group leader, so that pid is still a valid pgid for as long as any child
                # survives it: sweep whatever XCTest-host children outlived the leader. A sweep of an
                # already-empty group raises ProcessLookupError, suppressed like every other discard step.
                with contextlib.suppress(OSError):
                    os.killpg(self._runner_proc.pid, signal.SIGKILL)
            else:
                _terminate_process_group(self._runner_proc)
            self._runner_proc = None
        # The capture this probe reads may be pruned below, and the next spawn wires its own.
        self._run_ended = _never_ended
        # A `finally`, because the two terminates now let a `simctl` timeout through (BE-0363): this
        # discard's own bookkeeping — the capture, the patched .xctestrun, the reuse flag — must
        # still complete, or surfacing a wedged device would cost a leaked temp file each time.
        try:
            self._terminate_app_under_test()
            self._terminate_runner_app()
        finally:
            self._release_log(keep=keep_log or crashed)  # after the hint above has read the tail
            if self._patched_runner is not None:
                self._patched_runner.unlink(missing_ok=True)
                self._patched_runner = None
            self._reusable = False

    def _terminate_app_under_test(self) -> None:
        """Best-effort `simctl terminate` of the app the runner launched (Simulator only).

        The Swift runner `_exit`s on a pre-serving failure rather than unwinding XCTest, so nothing
        else brings the app down: an app left mid-launch by the timeout that failed one attempt is
        exactly what the next attempt would call `launch()` on again. Failures here are ignored —
        the common case is an app that is not running, and a discard must never fail — except a
        `simctl` call that exceeded its own deadline, which says the device is wedged rather than
        the app absent, and so is exactly what this teardown must not swallow (BE-0363).
        """
        if self._bundle_id is None:
            return
        try:
            simctl.Env(self._udid, run=self._run).terminate(self._bundle_id)
        except simctl.DeviceTimeout:
            raise
        except (subprocess.CalledProcessError, simctl.DeviceError, OSError):
            pass  # not running, or the device is already gone — a discard must not fail on either

    def _terminate_runner_app(self) -> None:
        """Best-effort `simctl terminate` of the XCTRunner app itself (Simulator only).

        The process-group sweep above cannot reach this one: `launchd_sim` starts the runner app
        inside the Simulator, so it is a guest process in no host process group, and it survives the
        `xcodebuild` that asked for it — still holding the automation session `testmanagerd` handed
        it. Every cold spawn that leaves one behind narrows what the next spawn can obtain, which is
        one way a device reaches the state where the runner never comes up at all. The ids come from
        the resolved `.xctestrun` (`_runner_host_bundle_ids`) rather than the bundled runner's known
        id, so an explicit `xcuitest.testRunner` is cleaned up just as readily. Failures are ignored
        for the same reason as the app under test: the common case is one that is not running — and
        a deadline exceeded is excluded for that same reason, being a wedged device rather than an
        absent app (BE-0363).
        """
        for bundle_id in self._runner_bundle_ids:
            try:
                simctl.Env(self._udid, run=self._run).terminate(bundle_id)
            except simctl.DeviceTimeout:
                raise
            except (subprocess.CalledProcessError, simctl.DeviceError, OSError):
                continue

    def _release_log(self, *, keep: bool) -> None:
        """Drop the reference to the current capture, pruning a default (env-unset) one unless kept.

        A default capture exists only to diagnose a flake — its tail is folded into the crash warning
        / startup error before this runs — so a healthy run prunes it and leaves nothing behind. A
        failed cold attempt, and a mid-run crash (`_discard_runner`'s `keep_log or crashed`), both keep
        it, so the full log survives as evidence past the 20-line tail already shown; an explicit
        `BAJUTSU_XCUITEST_RUNNER_LOG` directory is always kept, since the operator asked for it
        (BE-0319 unit 1).

        A kept default capture is logged here at the moment it is kept: `_spawn_cold_with_retry`
        folds a failed attempt's path into the raised error only when *every* attempt fails: a retry
        that then succeeds raises nothing, so without this line that attempt's file becomes untracked
        the instant this environment's `_runner_log` moves on to the next attempt — orphaned in
        `_DEFAULT_RUNNER_LOG_DIR` with nothing pointing at it.
        """
        if self._runner_log is not None and self._runner_log_ephemeral:
            if keep:
                _logger.info(
                    "xcuitest runner: kept a failed attempt's capture → %s", self._runner_log
                )
            else:
                self._runner_log.unlink(missing_ok=True)
        self._runner_log = None

    def has_reusable_resident(self) -> bool:
        return self._reusable  # BE-0291: a Simulator start left a warm runner the pool should keep

    def replaced_device(self) -> str | None:
        # The udid this environment is on now, once a vanished device forced a replacement. Reported
        # unconditionally after the swap (not cleared per lease): the pool compares it against the
        # udid it leased, so a pool that has already adopted the replacement reads no further change.
        return self._udid if self._replaced_from is not None else None

    def end_lease(self, driver: base.Driver, eff: Effective) -> None:
        # A crashed runner's evidence is captured *here*, because this is the last moment it is still
        # this scenario's: the pool frees the device right after, and the next lease can reuse this
        # same environment and respawn over the crashed runner's state before the crashed scenario's
        # retry loop has even given up and asked for it (BE-0421). The discard that would otherwise
        # notice runs at that next bring-up, which is already too late.
        self._capture_crash_if_observed()
        # Nothing to clear on the healthy path: the pool takes the snapshot off this environment right
        # after this returns (`take_crash_snapshot`), so a lease never inherits a previous one's.
        # Keep the warm runner alive for the next lease on this device; terminate only the app, the
        # same per-scenario cleanup a cold lease does (BE-0291). The pool tears the runner down later
        # (run-set end / actuator switch) via teardown.
        super().teardown(driver, eff)

    def teardown(self, driver: base.Driver, eff: Effective) -> None:
        self._discard_runner()
        super().teardown(driver, eff)
