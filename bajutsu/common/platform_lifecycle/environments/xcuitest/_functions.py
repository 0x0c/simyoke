"""Cold-spawn the resident runner, wait it ready, and tear its process group down."""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import shlex
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from bajutsu.common.backend_cli import simctl
from bajutsu.common.config import XcuitestConfig
from bajutsu.common.drivers.zorder import ZOrderResponder, ZOrderSource
from bajutsu.common.platform_lifecycle.environments._bundled_runner import (
    bundled_products_dir,
    bundled_runner_build_info,
    bundled_runner_is_stale,
    ensure_bundled_runner_fresh,
    materialize,
)

from ._attempt_failure import _AttemptFailure
from ._recovery import _Recovery
from ._shared import _logger

if TYPE_CHECKING:
    from ._spawned import _Spawned

# A cold XCTest-host launch that never binds its port is a transient blip (the class BE-0207 absorbs
# at the transport layer). One retry absorbs a one-off cold-start blip; a repeatable failure — a
# broken build, signature, or app — fails every attempt and still stops the gate (BE-0049). Bounded
# to a single retry: two attempts total. The startup ceiling is shared across the attempts by default
# (`_spawn_cold_with_retry`) — a slow "health never ready" attempt has spent it, and a second full
# wait against an unchanged device would double the worst case for no new information — but a retry
# that follows a device repair restarts the ceiling, since the device it spawns onto has demonstrably
# come back up.
_COLD_SPAWN_ATTEMPTS = 2

# Between health probes during the cold-spawn wait, re-check the `xcodebuild` handle this often — a
# condition wait (no fixed sleep that ignores the condition), matching the driver's own /health poll.
_COLD_POLL_SECONDS = 0.1

# The captured lines that say the XCTest run reached its end, so this runner will never bind its
# port. `xcodebuild` outlives its own test run by a long way: when the app launch itself times out,
# the suite can report failure long before the process exits, so the liveness check (which watches
# the *process*) sees nothing and the wait runs out the whole ceiling for no reason — exactly the
# failure BE-0319's retry exists to absorb, and the one it could never reach without this marker.
# Reading the terminal marker out of the capture ends the wait as soon as the run actually ended.
# Both outcomes end the run — a suite that passed has exited too — so neither can be a runner still
# on its way up.
#
# `Selected tests` is the *restarted* run's root suite, and matching it is what lets this probe see
# the case it most needs to: when XCTest's own watchdog judges the in-Simulator host unresponsive it
# logs "Restarting after unexpected exit, crash, or test timeout", relaunches, re-runs zero tests, and
# ends — under `Selected tests`, never `All tests`. The port is dead from that moment, but with only
# the `All tests` spellings matched the probe answered "still running" forever, so `_runner_alive`
# kept reporting the runner alive and crash recovery polled a dead port for its whole window (the
# fault-injection lane's own captures show exactly this). Widening the family is safe because all
# four spellings are *terminal*: a root suite that reported passed or failed has ended whatever it is
# named, so the port is dead either way. Do not lean instead on `_spawn_runner` passing no
# `-only-testing`: the `.xctestrun` a per-target `xcuitest.build` (or a prebuilt `testRunner`) hands
# over can carry `OnlyTestIdentifiers` and report under `Selected tests` on a perfectly healthy run —
# which is exactly why only the terminal spellings may join this family. A `started` line, or the
# "Restarting after unexpected exit" line itself, would abort a runner still on its way up.
_RUN_ENDED_MARKERS = (
    b"Test Suite 'All tests' failed",
    b"Test Suite 'All tests' passed",
    b"Test Suite 'Selected tests' failed",
    b"Test Suite 'Selected tests' passed",
)

# `XCUIApplication.launch()` giving up on the app under test — the dominant CI signature, and the one
# that says the *device* is degraded rather than the build broken. Read for the diagnostic only: the
# recovery ladder keys on the failure *kind* (any run-ended attempt reboots, marker or not), not on
# this text, which precedes the `_RUN_ENDED_MARKERS` line that actually ends the wait.
_LAUNCH_TIMEOUT_MARKER = b"Timed out attempting to launch"

# Carried between probes so a marker split across two reads is still matched; one byte short of the
# longest marker is all the overlap that can hide one.
_RUN_ENDED_OVERLAP = max(len(m) for m in (*_RUN_ENDED_MARKERS, _LAUNCH_TIMEOUT_MARKER)) - 1


# Cold `xcodebuild test-without-building` startup (XCTest host boot + app launch before the runner's
# server answers /health) routinely exceeds the driver's 10s default on a loaded CI runner; a warm
# start still returns as soon as /health is ready, so this only raises the ceiling for the cold case.
# Overridable per lane so a contended CI host can extend the ceiling without a code change.
_RUNNER_STARTUP_TIMEOUT = 120.0
_RUNNER_STARTUP_TIMEOUT_ENV = "BAJUTSU_XCUITEST_STARTUP_TIMEOUT"


# A *respawn* — a cold spawn on a device this run already brought up once, because a mid-run crash
# evicted its warm resident — is not the first bring-up: the Simulator is booted and the app
# installed, so the slow parts of `_RUNNER_STARTUP_TIMEOUT` (first boot + install + the initial
# `xcodebuild test-without-building` host spin-up) are already paid. Waiting the full cold ceiling
# again on a respawn only lets a dead runner burn minutes before a crash surfaces (the between-attempt
# recovery budget in pipeline.py cannot cut a single respawn's readiness wait short). This overrides
# the ceiling for respawns only; unset keeps the cold ceiling, so a lane not opting in is unchanged.
_RESPAWN_TIMEOUT_ENV = "BAJUTSU_XCUITEST_RESPAWN_TIMEOUT"


# How long the between-attempts device recovery may spend before the run gives up on the device
# (`_check_recovery_budget`). Generous enough for the slowest rung — creating a Simulator and waiting
# out its first boot — but bounded, because a device that takes longer than this to come back is not
# coming back, and a retry funded out of the remaining job time would only fail later.
_RECOVERY_TIMEOUT = 180.0
_RECOVERY_TIMEOUT_ENV = "BAJUTSU_XCUITEST_RECOVERY_TIMEOUT"

# Empirical cap, not a documented platform limit: each warm reuse re-attaches the XCTest automation
# session to a freshly launched app, and that session can destabilize after enough app.launch()
# cycles on a slower/contended host, even with the offending main-thread work in `BajutsuScreen`'s
# `viewDidAppear` hook moved off-thread (`BajutsuNet.postJSON`). This bound stays as defense-in-depth
# for that reactive case, which the BE-0291 warm probe only detects after the fact. Bounding the
# reuse count makes the respawn *proactive*: after this many warm reuses, `start` respawns the runner
# cold (a fresh XCTest session) before the next launch can tip it over, so a run never hits the
# mid-scenario crash. A cold spawn resets the count. Kept below "a handful" with headroom;
# overridable per lane for on-device tuning without a code change. 0 disables warm reuse entirely
# (always cold).
_MAX_WARM_REUSES = 3
_MAX_WARM_REUSES_ENV = "BAJUTSU_XCUITEST_MAX_WARM_REUSES"


_RunnerTier = Literal["misconfigured", "explicit", "device", "bundled"]


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the runner's process group, then SIGKILL whatever is left of it; never raises.

    `xcodebuild` spawns the XCTest-host plumbing that drives the device, so signalling only the
    parent leaves those children alive and holding the device's automation session — the state a
    following spawn attempt then has to spawn onto. The runner gets its own process group
    (`start_new_session` in `_spawn_runner`) precisely so this can reach all of it, and every step is
    suppressed: a discard runs on the failure path, where raising would mask the real error.

    The closing SIGKILL is unconditional rather than an escalation the parent's exit can skip, because
    the parent's exit says nothing about its children: `xcodebuild` can unwind promptly on the SIGTERM
    while an XCTest-host child ignores it and keeps the automation session, which is exactly what this
    exists to prevent. The group id is read once while the leader is alive, since reaping the leader
    makes it unreadable; sweeping a group that is already empty raises `ProcessLookupError`, which is
    suppressed along with the rest.
    """
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except OSError:
        pgid = None  # already reaped, or no group to read — fall back to the process itself

    def signal_group(sig: int) -> None:
        """Signal the whole group, falling back to the process alone when the group is unreachable."""
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
            except OSError:
                # Gone, not ours, or unsignallable for any other reason — every case is a group this
                # signal did not reach, so all of them fall through to the process itself. Narrowing
                # this to the expected errors would let an unexpected one leave the runner alive.
                pass
            else:
                return
        with contextlib.suppress(OSError):
            proc.terminate() if sig == signal.SIGTERM else proc.kill()

    signal_group(signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5)
    signal_group(signal.SIGKILL)
    # Reap the parent so it does not linger as a zombie until this process exits.
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5)


def _zorder_client(extra_env: Mapping[str, str] | None) -> ZOrderSource | None:
    """The `nativeZ` responder client for a launch, or None when this run allocated no port.

    The port and token come from the same launch env the app reads (BE-0355), so host and app agree
    without a second channel to keep in step.
    """
    port = (extra_env or {}).get("BAJUTSU_ZORDER_PORT")
    token = (extra_env or {}).get("BAJUTSU_ZORDER_TOKEN")
    if not port or not token:
        return None
    return ZOrderResponder(port=int(port), token=token)


def _allocate_port() -> int:
    """Bind an ephemeral port on localhost and return it.

    The socket is closed immediately so the runner can bind it; the window for another process to
    grab the port is negligible on localhost.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _runner_startup_timeout() -> float:
    """The cold-runner startup ceiling in seconds, from the env override or the default."""
    raw = os.environ.get(_RUNNER_STARTUP_TIMEOUT_ENV)
    if not raw:
        return _RUNNER_STARTUP_TIMEOUT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _RUNNER_STARTUP_TIMEOUT


def _recovery_timeout() -> float:
    """The device-recovery wall bound in seconds, from the env override or the default."""
    raw = os.environ.get(_RECOVERY_TIMEOUT_ENV)
    if not raw:
        return _RECOVERY_TIMEOUT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _RECOVERY_TIMEOUT


def _respawn_timeout() -> float | None:
    """The respawn readiness ceiling (s) from the env, or None (fall back to the cold ceiling) unset/invalid.

    Non-positive or unparseable reads as None (use the cold ceiling): the override only ever *tightens*
    a respawn's wait, never removes readiness waiting altogether.
    """
    raw = os.environ.get(_RESPAWN_TIMEOUT_ENV)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _max_warm_reuses() -> int:
    """The warm-reuse budget before a proactive cold respawn, from the env override or the default."""
    raw = os.environ.get(_MAX_WARM_REUSES_ENV)
    if not raw:
        return _MAX_WARM_REUSES
    try:
        return max(0, int(raw))
    except ValueError:
        return _MAX_WARM_REUSES


def _never_ended() -> str | None:
    """The neutral run-ended probe: a spawn with no capture to read can only be judged by its process."""
    return None


def _diagnostic_reports_dir() -> Path | None:
    """Where macOS's `ReportCrash` writes a per-user crash report, or None when it cannot be located.

    Resolved on demand rather than at import (BE-0421): `Path.home()` raises when no home directory
    can be resolved — a container started against a uid with no passwd entry and no `HOME` — and this
    module is imported on every platform, including the Linux lanes that have no report store to read
    in the first place. An unresolvable home reads as "no reports", the same first-class answer a
    missing directory already gives.
    """
    try:
        home = Path.home()
    except RuntimeError:
        return None
    return home / "Library" / "Logs" / "DiagnosticReports"


def _reports_since(reports_dir: Path, pattern: str, since: float) -> list[Path]:
    """Crash reports matching *pattern* in *reports_dir* modified at or after *since*, newest first.

    The name-and-time half of BE-0421's crash-report match: the pattern keeps an unrelated process's
    report out, and *since* — the crashed runner's spawn timestamp — keeps an earlier invocation's
    report on the same host out. Never raises: `Path.glob` answers a directory that is missing (every
    platform but macOS has none) or unreadable with no entries at all, and an entry that vanishes
    between the listing and its `stat` is skipped, since a report store is a live directory.
    """
    found: list[tuple[float, Path]] = []
    for path in reports_dir.glob(pattern):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= since:
            found.append((mtime, path))
    return [path for _, path in sorted(found, key=lambda pair: pair[0], reverse=True)]


def _reported_pid(report: bytes) -> int | None:
    """The process id an `.ips` crash report names, or `None` when it names none this can read.

    An `.ips` file is two JSON documents: a one-line header naming the process and its version, then
    the payload carrying the fault itself. The pid lives in the payload on the reports measured, but
    the header is checked first because it is the cheaper and more stable of the two. Returning `None`
    is a real answer — the caller then matches on name and time alone (BE-0421).
    """
    header, _, payload = report.partition(b"\n")
    for document in (header, payload):
        try:
            parsed = json.loads(document)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(parsed, dict) and isinstance(pid := parsed.get("pid"), int):
            return pid
    return None


def _run_ended_probe(log_path: Path | None) -> Callable[[], str | None]:
    """Watch the growing capture for the marker that ends an XCTest run; the reason, or `None` while it runs.

    The companion to the process-liveness check in `_await_cold_runner`: that one catches an
    `xcodebuild` that *exits*, this one an `xcodebuild` that finished its test run and lingers. Only
    the bytes appended since the previous probe are read, so polling the capture stays cheap however
    verbose it grows.

    A run that ended because the app never came to the foreground names that in its reason
    (`_LAUNCH_TIMEOUT_MARKER`): it is the signature of a degraded Simulator, and saying so is what
    lets a reader tell "this device needs rebooting" from "this build is broken" — the recovery ladder
    itself keys on the failure *kind*, not on this text.

    The verdict **latches** (BE-0354), because two consumers pulse differently. The cold gate reads
    each window once and stops at the first marker, so an edge-triggered answer suffices for it; the
    mid-run liveness predicate (`XcuitestEnvironment._runner_alive`) is level-triggered, re-asked
    throughout each recovery episode — once when the crash is declared and then once a second while
    the recovery wait runs (BE-0360) — and an unlatched probe would answer "ended" on whichever ask
    first saw the marker and "still running" for every ask after it. Both share one probe instance per
    spawn — the marker lives in a single stream of bytes, so a second, independent instance would race
    this one for it.
    """
    if log_path is None:
        return _never_ended
    offset = 0
    carry = b""
    launch_timed_out = False
    latched: str | None = None

    def probe() -> str | None:
        nonlocal offset, carry, launch_timed_out, latched
        if latched is not None:
            return latched
        try:
            with log_path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
                offset = fh.tell()
        except OSError:
            return None  # the capture may not exist yet on a spawn that failed before writing
        if not chunk:
            return None
        window = carry + chunk
        # Sticky: the launch timeout is logged before the suite reports failure, so the two markers
        # rarely land in the same read window.
        launch_timed_out = launch_timed_out or _LAUNCH_TIMEOUT_MARKER in window
        for marker in _RUN_ENDED_MARKERS:
            if marker in window:
                cause = " after the app launch timed out" if launch_timed_out else ""
                latched = (
                    f"the xctest run ended ({marker.decode()}){cause} "
                    "before the runner bound its port"
                )
                return latched
        carry = window[-_RUN_ENDED_OVERLAP:]
        return None

    return probe


def _await_cold_runner(
    spawned: _Spawned,
    *,
    timeout: float,
    poll: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> _AttemptFailure | None:
    """Wait for the runner to answer `/health` while watching its process; `None` if ready, else why not.

    A bounded condition wait (BE-0319 unit 3): each round probes `spawned.ready()` first — a runner
    that came up wins regardless of the other two — then `spawned.poll()`, so a runner that died
    during startup aborts at once with its exit code rather than probing a dead port for the
    remaining budget, and finally `spawned.run_ended()`, which catches the failure the exit code
    cannot: an `xcodebuild` whose test run has ended but whose process lingers (see
    `_RUN_ENDED_MARKERS`). Returns `None` once ready, else the classified failure the caller folds
    the captured tail onto and picks a recovery rung from.
    """
    deadline = clock() + timeout
    while True:
        if spawned.ready():
            return None
        exit_code = spawned.poll()
        if exit_code is not None:
            return _AttemptFailure(
                "process-exit",
                f"the xcodebuild process exited (code {exit_code}) before the runner bound its port",
            )
        ended = spawned.run_ended()
        if ended is not None:
            return _AttemptFailure("run-ended", ended)
        if clock() >= deadline:
            return _AttemptFailure("never-ready", f"health never ready within {timeout}s")
        sleep(poll)


def _no_recovery(failure: _AttemptFailure) -> _Recovery | None:  # noqa: ARG001  # _RecoverFn shape
    """The neutral recovery: nothing to repair (a real device, or a caller that opts out)."""
    return None


def _spawn_cold_with_retry(
    spawn: Callable[[], _Spawned],
    *,
    timeout: float,
    recover: Callable[[_AttemptFailure], _Recovery | None] = _no_recovery,
    attempts: int = _COLD_SPAWN_ATTEMPTS,
    poll: float = _COLD_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> _Spawned:
    """Spawn the cold runner, await readiness with a liveness check, recover the device, and retry once.

    A cold XCTest-host launch that never binds its port is a transient infrastructure blip (BE-0207's
    class); a single retry absorbs a one-off cold-start blip, while a repeatable failure — a broken
    build, signature, or app — fails every attempt and still stops the gate, preserving BE-0049's
    "flakiness is never tolerated by absorption". Each failed attempt is discarded (no leaked
    subprocess) and its captured tail folded into the final loud `XcuitestChannelError` (unit 2), so
    the run-failing error shows *why* the runner never answered, not merely that it did not.

    Between attempts `recover` gets the classified failure and may repair the device it spawns onto:
    the retry BE-0319 added isolated every host-side resource per attempt — port, `.xctestrun`,
    capture — but never the device, so a Simulator whose app launch had just timed out was handed to
    the retry in exactly the state that had defeated the first attempt. What `recover` returns
    decides both the retry's budget and what the failing error says:

    - A `_Recovery` carrying a `fresh_budget` (the device was rebooted, or replaced outright) restarts
      the ceiling, because the next attempt runs against a device that has demonstrably come back up.
      This is what makes the dominant flake recoverable at all: an app-launch timeout ends the *first*
      attempt fast, but what it leaves behind is a degraded device, not spare seconds.
    - A `_Recovery` whose `fresh_budget` is `None` reports a rung that inspected the device and left it
      as it was, so its note reaches the diagnostics while the *shared* budget stands: the first attempt
      got the whole ceiling and a later one gets only what it left unspent. A "health never ready"
      attempt leaves nothing, so no retry follows it — a second full wait against an unchanged device
      would double the worst case for no new information.
    - `None` reports no recovery at all (`_no_recovery`, for a real device or a caller that opts out),
      which keeps the shared budget and adds no note.

    Worst-case wall time is therefore `attempts` ceilings plus whatever `recover` spends repairing the
    device, which `_recovery_timeout()` bounds — plus, when a repair earns a fresh budget, the same
    unbounded re-prep the first cold bring-up already pays before this loop even starts. A lane's
    startup-timeout headroom should be set against that worst case.
    """
    from bajutsu.common.drivers.xcuitest import XcuitestChannelError

    deadline = clock() + timeout
    diagnostics: list[str] = []
    for n in range(1, attempts + 1):
        remaining = deadline - clock()
        # First attempt always runs (with the full budget); a later one only if a fast failure or a
        # device repair left budget for it — an unchanged device that spent the whole ceiling gets no
        # wasteful second wait.
        if n > 1 and remaining <= 0:
            break
        spawned = spawn()
        try:
            failure = _await_cold_runner(
                spawned, timeout=max(0.0, remaining), poll=poll, sleep=sleep, clock=clock
            )
        except BaseException:
            # An unexpected failure while awaiting must not leak the just-spawned runner (the leak
            # BE-0290 prevents); discard it before propagating. A device fault raised by the discard
            # itself is only logged: the exception already in flight is the reason we are here, and
            # replacing it would hide it — including a KeyboardInterrupt, which must still reach the
            # operator.
            try:
                spawned.discard()
            except simctl.DeviceError as discard_exc:
                _logger.warning("discarding the xcuitest runner failed: %s", discard_exc)
            raise
        if failure is None:
            if n > 1:
                _logger.info(
                    "xcuitest runner unresponsive, respawned and recovered on attempt %d/%d (%s)",
                    n,
                    attempts,
                    "; ".join(diagnostics),
                )
            return spawned
        diagnostics.append(f"attempt {n}/{attempts}: {failure.detail}{spawned.log_tail()}")
        try:
            spawned.discard()
        except simctl.DeviceError as exc:
            # A discard that hits a wedged device must not throw away what the attempts observed;
            # fold it in exactly as an unrepairable device is folded in below.
            diagnostics.append(f"discard after attempt {n} failed: {exc}")
            raise simctl.DeviceError(
                "xcuitest runner did not come up:\n" + "\n".join(diagnostics)
            ) from exc
        if n == attempts:
            break  # no further attempt to prepare a device for
        # Recovery runs after the discard, so it acts on a device this run no longer holds a runner
        # on. It raises rather than returns when the device cannot be repaired at all (a host whose
        # Simulator runtimes are gone), which is a device fault, not a flaky spawn.
        try:
            recovery = recover(failure)
        except (simctl.DeviceError, OSError) as exc:
            # An unrepairable device still deserves every attempt's classified reason and captured
            # tail — that diagnostic is this function's whole point (unit 2) — so it is folded in
            # rather than lost behind the bare DeviceError an operator would otherwise see alone.
            # OSError is caught alongside DeviceError because every rung's simctl call can raise it
            # (a fork that fails with EAGAIN/ENOMEM, an xcrun that has gone) and nothing on the
            # reboot/replace paths converts it — the same host degradation this ladder recovers from.
            diagnostics.append(f"recovery after attempt {n} failed: {exc}")
            raise simctl.DeviceError(
                "xcuitest runner did not come up:\n" + "\n".join(diagnostics)
            ) from exc
        if recovery is not None:
            diagnostics.append(f"recovery after attempt {n}: {recovery.note}")
            if recovery.fresh_budget is not None:
                deadline = clock() + recovery.fresh_budget
    raise XcuitestChannelError("xcuitest runner did not come up:\n" + "\n".join(diagnostics))


def _destination(device_type: str, udid: str) -> str:
    """Build the `xcodebuild -destination` for a Simulator or a real device (BE-0238).

    Both run the same `test-without-building`; only the platform differs — the Simulator's
    `iOS Simulator` vs a real device's `iOS`. `validated_udid` applies the shared device_id policy
    (chiefly: an id never leads with `-`, which xcodebuild would read as an option) to either id.
    """
    platform = "iOS" if device_type == "device" else "iOS Simulator"
    return f"platform={platform},id={simctl.validated_udid(udid)}"


def effective_device_type(xcfg: XcuitestConfig | None) -> str:
    """The target's `xcuitest.deviceType`, defaulting to `"simulator"` when unconfigured.

    The one place this default lives, so `XcuitestEnvironment.start` and `runner_source`'s caller
    (BE-0292's doctor disclosure) read the same value instead of each re-deriving it.
    """
    return xcfg.device_type if xcfg is not None else "simulator"


def _classify_runner(
    xcfg: XcuitestConfig | None, device_type: str
) -> tuple[_RunnerTier, str | None, str | None]:
    """Which runner-resolution tier applies: an explicit testRunner, else build, else the bundle.

    The one place the precedence lives, so `_resolve_runner` (which acts on the tier) and
    `runner_source` (which only discloses it, BE-0292) can't drift apart. Returns the tier plus
    `(test_runner, build)` when the tier is `"explicit"` (both `None` otherwise, since only that
    tier needs them).
    """
    test_runner = xcfg.test_runner if xcfg is not None else None
    build = xcfg.build if xcfg is not None else None
    if test_runner is None and build is not None:
        # `build` only ever refreshes the file at `testRunner` (see below); without that path there
        # is nowhere for its output to land, so this is a misconfiguration, not a request for the
        # bundled default.
        return "misconfigured", None, None
    if test_runner is not None:
        return "explicit", test_runner, build
    if device_type == "device":
        return "device", None, None
    return "bundled", None, None


def _resolve_runner(xcfg: XcuitestConfig | None, device_type: str) -> Path:
    """Resolve the `.xctestrun` to run: an explicit testRunner, else its build, else the bundle.

    Precedence keeps explicit config above the default. A configured `testRunner` is used, built on
    demand via `build` when the file is missing. With neither configured, a Simulator run falls back
    to the wheel-bundled generic runner (BE-0292), materialized into a writable cache; a real device
    instead fails loudly, since its runner must be signed (BE-0288) and is not bundled. In a dev
    checkout, `ensure_bundled_runner_fresh` rebuilds that fallback first when BajutsuKit's own source
    has moved past it, so this tier never silently serves a stale bundle.
    """
    tier, test_runner, build = _classify_runner(xcfg, device_type)

    if tier == "misconfigured":
        # Fail loudly rather than silently ignoring the configured build.
        raise simctl.DeviceError("xcuitest.build requires xcuitest.testRunner (the path it builds)")

    if tier == "explicit":
        assert test_runner is not None  # guaranteed by _classify_runner's "explicit" tier
        runner_path = Path(test_runner)
        if not runner_path.exists() and build:
            try:
                subprocess.run(shlex.split(build), check=True)
            except (subprocess.CalledProcessError, OSError) as exc:
                raise simctl.DeviceError(f"xcuitest build command failed: {build}") from exc
        if not runner_path.exists():
            raise simctl.DeviceError(f"xcuitest testRunner not found: {test_runner}")
        return runner_path

    if tier == "device":
        raise simctl.DeviceError(
            "xcuitest.deviceType: device requires an explicit xcuitest.testRunner "
            "(a real-device runner must be signed and is not bundled; see BE-0288)"
        )
    ensure_bundled_runner_fresh()
    products = bundled_products_dir()
    if products is None:
        raise simctl.DeviceError(
            "xcuitest backend requires xcuitest.testRunner in the target config "
            "(no bundled runner is present in this build)"
        )
    try:
        return materialize(products)
    except OSError as exc:
        raise simctl.DeviceError(
            f"failed to materialize the bundled xcuitest runner: {exc}"
        ) from exc


def runner_source(xcfg: XcuitestConfig | None, device_type: str) -> str:
    """Which runner-resolution tier a target would use, without acting on it (BE-0292).

    Shares `_resolve_runner`'s precedence via `_classify_runner` rather than re-deriving it, so
    `doctor` can disclose the source without running a configured `build` command or materializing
    the bundled runner into the cache.
    """
    tier, test_runner, build = _classify_runner(xcfg, device_type)

    if tier == "misconfigured":
        return "misconfigured: xcuitest.build requires xcuitest.testRunner"
    if tier == "explicit":
        assert test_runner is not None  # guaranteed by _classify_runner's "explicit" tier
        if Path(test_runner).exists():
            return f"testRunner: {test_runner}"
        if build:
            return f"testRunner: {test_runner} (missing, built on demand via: {build})"
        return f"testRunner: {test_runner} (missing, no build configured)"
    if tier == "device":
        return "none: xcuitest.deviceType: device requires an explicit testRunner"
    if bundled_products_dir() is None:
        return "none: no bundled runner in this build (set xcuitest.testRunner)"
    return "bundled (wheel-shipped Simulator runner)"


def _major(version: str) -> str:
    """The leading numeric component of a version like ``16.0`` or ``18.2`` — its major."""
    return version.split(".", 1)[0].strip()


def bundled_runner_toolchain_warning(
    build_info: Mapping[str, str] | None,
    host_xcode: str | None,
    host_sdk: str | None,
) -> str | None:
    """Warn when the host toolchain differs from the one the bundled runner was built against.

    The bundled runner is a compiled artifact tied to the Xcode and Simulator SDK it was built with
    (BE-0292); a host on a different major version can fail to launch it with an opaque `xcodebuild`
    error. Comparing majors keys the warning to that breaking case while staying quiet across the
    point releases that stay compatible. Returns a one-line message naming the `testRunner` / `build`
    overrides as the escape hatch, or `None` when there is nothing recorded, nothing on the host to
    compare, or the majors agree. Pure disclosure: no gate, no LLM (prime directive 1).
    """
    if not build_info:
        return None

    def _mismatch(label: str, built: str | None, host: str | None) -> str | None:
        if built and host and _major(built) != _major(host):
            return f"{label} {built} (bundled runner) vs {host} (host)"
        return None

    mismatches = [
        m
        for m in (
            _mismatch("Xcode", build_info.get("xcode"), host_xcode),
            _mismatch("iphonesimulator SDK", build_info.get("sdk"), host_sdk),
        )
        if m
    ]
    if not mismatches:
        return None
    return (
        "bundled runner toolchain mismatch: "
        + "; ".join(mismatches)
        + " — if it fails to launch, set xcuitest.testRunner or xcuitest.build to build a "
        "matching runner"
    )


def bundled_runner_toolchain_note(
    xcfg: XcuitestConfig | None,
    device_type: str,
    host_toolchain: Callable[[], tuple[str | None, str | None]],
) -> str | None:
    """A toolchain-mismatch note, but only when the target resolves to the bundled runner (BE-0292).

    Shares `_classify_runner`'s precedence so the note is confined to the bundled tier; an explicit
    `testRunner` or a device target (whose runner is not the bundled one) never warns. `host_toolchain`
    is a `() -> (xcode, sdk)` probe called lazily — only after the tier gate passes — so a target with
    an explicit runner pays no subprocess cost. Delegates the version comparison to
    `bundled_runner_toolchain_warning`.
    """
    tier, _, _ = _classify_runner(xcfg, device_type)
    if tier != "bundled":
        return None
    host_xcode, host_sdk = host_toolchain()
    return bundled_runner_toolchain_warning(bundled_runner_build_info(), host_xcode, host_sdk)


def bundled_runner_staleness_note(xcfg: XcuitestConfig | None, device_type: str) -> str | None:
    """A "bundle is stale" note, but only when the target resolves to the bundled runner (BE-0292).

    Shares `_classify_runner`'s precedence with `bundled_runner_toolchain_note`, so the note is
    confined the same way. Pure disclosure: reads the staged `build-info.json` and computes
    `source_hash()`, but never calls `ensure_bundled_runner_fresh`'s rebuild.
    """
    tier, _, _ = _classify_runner(xcfg, device_type)
    if tier != "bundled":
        return None
    if not bundled_runner_is_stale():
        return None
    return "bundled runner is stale — will rebuild on next run"


def _runner_host_bundle_ids(runner_path: Path) -> tuple[str, ...]:
    """The bundle ids of the XCTRunner apps a `.xctestrun`'s test targets are hosted by.

    Each target names its own runner app as `TestHostBundleIdentifier` (the built test bundle's id
    with `.xctrunner` appended), so reading it here covers the bundled runner and an explicit
    `xcuitest.testRunner` alike — neither path has to know the other's id. Duplicates are dropped,
    since several targets in one file can share a runner app. Best-effort: a plist that cannot be
    read yields nothing rather than failing the spawn that asked, the same posture as the terminate
    this feeds (in practice `_patch_xctestrun_env` has already parsed the same file by then).
    """
    try:
        with runner_path.open("rb") as f:
            plist = plistlib.load(f)
    except (OSError, ValueError):
        return ()
    ids: list[str] = []
    for key, target in plist.items():
        if key == "__xctestrun_metadata__" or not isinstance(target, dict):
            continue
        host_id = target.get("TestHostBundleIdentifier")
        if isinstance(host_id, str) and host_id and host_id not in ids:
            ids.append(host_id)
    return tuple(ids)


def _patch_xctestrun_env(runner_path: Path, forwarded: Mapping[str, str]) -> Path:
    """Write a copy of the .xctestrun with *forwarded* merged into each target's env.

    `xcodebuild` does not propagate its own environment into the Simulator test-runner
    process, so the runner reads `BAJUTSU_*` from `TestingEnvironmentVariables` (the runner
    process's env) instead. Returns the temp copy's path; the caller unlinks it on teardown.
    """
    with runner_path.open("rb") as f:
        plist = plistlib.load(f)
    for key, target in plist.items():
        if key == "__xctestrun_metadata__" or not isinstance(target, dict):
            continue
        env_vars = dict(target.get("TestingEnvironmentVariables") or {})
        env_vars.update(forwarded)
        target["TestingEnvironmentVariables"] = env_vars
    # `__TESTROOT__` in the plist resolves relative to the .xctestrun's own directory, so the
    # patched copy must sit beside the original (next to the built products) to still find them.
    fd, path = tempfile.mkstemp(suffix=".xctestrun", dir=str(runner_path.parent))
    with os.fdopen(fd, "wb") as f:
        plistlib.dump(plist, f)
    return Path(path)
