"""Talk to the resident runner over the loopback channel, with a timeout chosen per call."""

from __future__ import annotations

import http.client
import json
import logging
import select
import socket
import time
from collections.abc import Callable, Mapping
from typing import Any

from bajutsu.common.drivers import base

from ._conn_state import _ConnState
from ._health_wait import _HealthWait
from ._reply import _Reply
from ._shared import _OK, _TIPKIT_DISMISS_REGION, TransportFn
from ._transport_failure import _TransportFailure
from .xcuitest_channel_error import XcuitestChannelError
from .xcuitest_runner_crash_error import XcuitestRunnerCrashError

# Socket timeout for a single runner *read* request (GET). BE-0105 replaced the per-attribute
# `/elements` walk (~10s+ per screen) with one `app.snapshot()`, so the 60s stopgap is reverted to a
# bounded window: generous enough for a cold first snapshot (XCUITest waits for the app to idle),
# tight enough that a wedged runner fails loudly rather than hanging. A transient read blip is
# absorbed by the BE-0207 retry, so this stays tight.
_SOCKET_TIMEOUT_SECONDS = 15

# Socket timeout for a single actuation *write* request (POST). A write synthesizes a real UI event —
# a two-finger gesture on a loaded CI host can take longer than a read — and BE-0207 must NOT re-issue
# a write after delivery (double-actuation risk), so a write cannot lean on the retry the way a read
# does. It gets ONE longer but still bounded window instead: enough headroom for a slow actuation on a
# contended host, while a genuinely wedged runner still fails loudly rather than hanging. Kept ≤ the
# job's per-step budget by a wide margin.
_ACTUATION_TIMEOUT_SECONDS = 30

# How long a kept-alive connection (BE-0407 Unit 11) may sit idle before `_raw_http_transport` forces
# a reconnect regardless of what `_is_stale`'s peek sees. `HTTPServer.swift`'s own idle timeout
# (`HTTPServer.defaultReceiveTimeout`, `SO_RCVTIMEO`) is 10s; the peek narrows but cannot close the
# race against it (see `_is_stale`'s docstring), so this bounds the reused connection's age from the
# other end instead. Half the server's window: comfortably past the spacing between ordinary driver
# calls (so keep-alive still pays off for the common case this PR exists to speed up), while leaving
# 5s of margin between "we decided to reuse it" and the actual `conn.request(...)` — far more than
# scheduling jitter on a live host ever needs — so that by the time the write lands, the server's own
# timer cannot plausibly have fired yet.
_KEEPALIVE_IDLE_RECONNECT_SECONDS = 5.0


# Bounded retry for a *transient* transport hiccup (BE-0207), beside the per-attempt window above:
# `_SOCKET_TIMEOUT_SECONDS` still bounds each single attempt (a wedged runner fails fast per try),
# and these bound how many times a recoverable blip is re-issued before the loud failure. Kept small
# so a genuinely wedged runner is not retried for an unbounded stretch.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5  # exponential per retry: 0.5s, 1.0s, … between attempts

# How long a mid-run crash-recovery (BE-0287) waits for a crashed runner to come back before failing
# loudly. A different concern from the transient retry above, which bounds a sub-second blip: a crash
# can leave the runner gone far longer than that as it relaunches, so this budget is generous enough to
# ride that out yet still bounded — a runner that is truly gone fails the run rather than hanging it.
_RECOVERY_TIMEOUT_SECONDS = 60

# How often the recovery wait re-asks the runner's liveness while it polls `/health` (BE-0360). The
# `/health` probe runs every 100ms, but the liveness check reads the runner's capture from a private
# offset, so asking it at the probe interval would cost 600 file reads across one window to learn of
# the death at most 900ms sooner — a difference that does not matter against a 60s window. Once a
# second bounds the wasted wait to about a second past the moment the death becomes observable.
_LIVENESS_POLL_SECONDS = 1.0
_TIPKIT_TIP_CONTAINER = "TipView"


# How many *consecutive* mid-run crashes the recovery layer rides out before failing loudly. Recovery
# re-issues an idempotent call once the runner is back; a runner that crashes *again* on that re-issue
# used to propagate uncaught and fail the run on the second crash. Retrying the recovery a bounded
# number of times rides out a runner that flaps across a few calls, while a runner that never
# stabilizes still fails the run rather than looping forever. Each attempt re-uses `recovery_timeout`
# for its health wait, so the worst case stays bounded.
_MAX_CRASH_RECOVERIES = 3

# How many *consecutive* hung calls (the request reached the runner, no response ever came) the
# recovery loop rides out before calling the automation session wedged (BE-0354). Three: the original
# call plus two post-recovery re-issues. One healthy state shares the hang's surface — BE-0323
# serialized the runner's XCUITest operations while `/health` deliberately bypasses that
# serialization, so a long operation can hold the lock while a concurrent read times out — but a
# single lock-holder cannot span two post-recovery windows, because its own call fails its own retry
# ladder first. A read still hanging then is not flapping, it is wedged, and only the pipeline's
# device-level retry can help it, so the channel hands over instead of spending its remaining
# recovery cycles and their health waits proving the same dead end.
_MAX_HUNG_CALLS = 3


def _timeout_for(method: str) -> float:
    """Per-attempt socket timeout for a channel call, chosen by its idempotency class.

    Reads (`GET`) get the tight `_SOCKET_TIMEOUT_SECONDS` and lean on the BE-0207 retry to absorb a
    transient blip; a write (`POST`) cannot be retried after delivery, so it gets the longer, still
    bounded `_ACTUATION_TIMEOUT_SECONDS` to tolerate a slow actuation on a loaded host.
    """
    return _SOCKET_TIMEOUT_SECONDS if method == "GET" else _ACTUATION_TIMEOUT_SECONDS


def _tip_is_up(tree: list[base.Element]) -> bool:
    """Whether `tree` shows a TipKit tip: the dismiss scrim and the tip's container together."""
    return bool(base.find_all(tree, {"id": _TIPKIT_DISMISS_REGION})) and bool(
        base.find_all(tree, {"id": _TIPKIT_TIP_CONTAINER})
    )


def _as_float(value: Any) -> float | None:
    """A request body's numeric parameter as a float, or None when absent (for the actuation record)."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _to_element(item: Mapping[str, Any]) -> base.Element:
    """Normalize one `GET /elements` item into an `Element`.

    The `handle` is dropped: it is a channel address, not a selector field, so matching is unaffected.
    """
    frame = item.get("frame") or (0.0, 0.0, 0.0, 0.0)
    return {
        "identifier": item.get("identifier"),
        "label": item.get("label"),
        "value": item.get("value"),
        "traits": list(item.get("traits") or []),
        "frame": (float(frame[0]), float(frame[1]), float(frame[2]), float(frame[3])),
        # The runner's reply carries no z signal; a measured position arrives from the in-app
        # responder and is matched in by `_apply_native_z` (BE-0355). Absent until then.
        "nativeZ": None,
    }


def _decode(path: str, status_code: int, body: bytes) -> _Reply:
    """Decode a raw runner response into a `_Reply`.

    `/screenshot` returns raw PNG bytes; every other endpoint returns a small JSON object with a
    `status` (and, for `/elements`, an `elements` array). A non-200 still carries the server's
    `status` when present, so `not-found` / `stale` reach the driver as outcomes rather than as a
    transport error. Pure (no socket) so the wire format is unit-tested directly.
    """
    if path == "/screenshot":
        return _Reply(status=_OK if status_code == 200 else "error", png=body)
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise XcuitestChannelError(f"runner returned non-JSON for {path}: {body!r}") from exc
    status = data.get("status") or (_OK if status_code == 200 else "error")
    elements = data.get("elements")
    # `GET /screen` (BE-0326) carries the viewport as width/height; absent on every other endpoint.
    width, height = data.get("width"), data.get("height")
    size = (float(width), float(height)) if width is not None and height is not None else None
    return _Reply(status=str(status), elements=elements, size=size, raw=body)


def _string_list(value: object) -> list[str]:
    """A reply field read as a list of strings — empty unless it really is a list."""
    return [str(item) for item in value] if isinstance(value, list) else []


def _parse_drain_fold(raw: bytes | None) -> base.DrainedInterruptions:
    """Read `labels`/`unmatched`/`banners` off a reply body, empty for any that is absent or unparseable.

    For the standalone `/interruptionPolicy/drain` reply. Its schema has always required
    `labels`/`unmatched`, so for those "absent" only ever means a body-less or malformed reply — never
    a meaningful distinction from "present but empty". `banners` (BE-0416) collapses the same way for
    a different reason, given at the fold below: a runner predating it legitimately omits the field,
    so this reader must not be tightened into rejecting or warning on its absence.
    `_parse_tap_drain_fold` is the sibling reader for a `/tap` reply, where that distinction *is*
    meaningful (BE-0407 Unit 6) and is not safe to collapse the same way.
    """
    if not raw:
        return base.DrainedInterruptions.empty()
    body = json.loads(raw)
    unmatched = body.get("unmatched")
    declined = (
        [_string_list(group) for group in unmatched if isinstance(group, list)]
        if isinstance(unmatched, list)
        else []
    )
    # A runner predating BE-0416 omits `banners` entirely; an empty list is the same answer as far as
    # any caller is concerned, since such a runner never swiped a banner away to report.
    return base.DrainedInterruptions(
        tapped=_string_list(body.get("labels")),
        declined=declined,
        banners=_string_list(body.get("banners")),
    )


def _parse_tap_drain_fold(raw: bytes | None) -> base.DrainedInterruptions | None:
    """The optional drain fold folded into a `/tap` reply (BE-0407 Unit 6), or `None` when absent.

    Unlike the standalone drain endpoint, `/tap`'s fold fields are optional in its schema — always
    present on a runner that supports Unit 6 (`withDrain` folds every tap's own drain in, empty
    arrays included), and always absent on one that predates it, such as a pinned older
    `testRunner` build (`targets.<name>.xcuitest.testRunner`). Absence, not a merely empty fold, is
    therefore what says "this runner never drained anything for this tap" — trusting an empty fold as
    if it meant the same thing would let a pre-Unit-6 runner's real interruptions go unreported the
    caller believes it already asked for.

    `banners` (BE-0416) is deliberately *not* part of that presence test: a runner between Unit 6 and
    BE-0416 folds the other two and omits it, and reading its absence as "no fold at all" would send
    every tap back to the wire for a drain the reply already answered.
    """
    if not raw:
        return None
    body = json.loads(raw)
    if "labels" not in body and "unmatched" not in body:
        return None
    return _parse_drain_fold(raw)


def _is_retry_eligible(method: str, *, delivered: bool) -> bool:
    """Whether a failed attempt is safe to re-issue (BE-0207, BE-0287).

    A failure before the request reached the runner is safe for any method — the runner never acted.
    Once the request was delivered, only idempotent reads may be retried; re-sending a side-effecting
    write after a response timeout could double-apply the action. Idempotency is keyed on the HTTP
    method: the runner's channel is REST-shaped, so every read is a `GET` (`/elements`, `/screenshot`,
    `/health`) and every actuation a `POST` — and the conservative direction is safe, since a request
    wrongly judged non-idempotent merely fails loudly instead of risking a double actuation.
    """
    return not delivered or method == "GET"


def _with_retry(inner: TransportFn, *, sleep: Callable[[float], None] = time.sleep) -> TransportFn:
    """Wrap *inner* with a bounded retry + exponential backoff over transient transport failures.

    Only a `_TransportFailure` is retried, and only when `_is_retry_eligible`; a decoded outcome
    (`stale` / `not-found`) is a `_Reply`, never an exception, so it is returned untouched and never
    retried — retrying an outcome would be the flakiness-by-absorption BE-0049 rejects. On exhaustion
    the loud `XcuitestRunnerCrashError` (a subclass of `XcuitestChannelError`) is raised, so the
    deterministic verdict is preserved: only a recoverable blip is absorbed. Each retry is logged, so a
    retried-then-passed run stays visible.
    """
    logger = logging.getLogger("bajutsu.xcuitest.channel")

    def transport(method: str, path: str, body: Mapping[str, Any] | None) -> _Reply:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return inner(method, path, body)
            except _TransportFailure as exc:
                if attempt == _MAX_ATTEMPTS or not _is_retry_eligible(
                    method, delivered=exc.delivered
                ):
                    # A blip outlived the transient budget (or a delivered write cannot be re-issued):
                    # signal it as a crash, tagged so the BE-0287 recovery layer can decide whether the
                    # call is safe to re-issue. Still an XcuitestChannelError, so a bare `_with_retry`
                    # (no recovery wrapper) fails just as loudly as before.
                    raise XcuitestRunnerCrashError(
                        f"runner channel {method} {path} failed: {exc}",
                        method=method,
                        delivered=exc.delivered,
                        hung=exc.hung,
                    ) from exc
                logger.warning(
                    "runner channel %s %s failed (attempt %d/%d), retrying: %s",
                    method,
                    path,
                    attempt,
                    _MAX_ATTEMPTS,
                    exc,
                )
                sleep(_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        raise AssertionError(  # pragma: no cover - the loop returns or raises on every iteration
            "unreachable: the retry loop returns on success or raises on the final attempt"
        )

    return transport


def _await_health(
    transport: TransportFn,
    *,
    timeout: float,
    poll: float = 0.1,
    runner_alive: Callable[[], bool] | None = None,
    liveness_poll: float = _LIVENESS_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> _HealthWait:
    """Poll `GET /health` until the runner answers `ready`, reporting how the wait ended within *timeout*.

    A bounded condition wait (no fixed sleep that ignores the condition): `READY` the moment the runner
    is ready, `TIMED_OUT` if the deadline passes first. A channel failure while the runner is down is
    swallowed and re-polled, so "not accepting connections yet" reads as not-ready, not as an error.
    Shared by `await_ready` (startup) and the crash-recovery layer (mid-run), which differ only in the
    transport and timeout they poll with.

    `runner_alive`, when the caller supplies it, is re-asked while the wait runs and ends it with
    `GONE` as soon as it reports the runner unable to come back (BE-0360). The verdict is a fact that
    *changes* during the window — a crashing runner's `xcodebuild` exit and its suite's result line
    both follow the crash — so sampling it only before the wait would wait out the very failure the
    fast-fail exists for. It is asked *after* the `/health` probe, so a runner that answers `ready` on
    the same poll where its capture first shows the marker still counts as recovered: a runner that is
    serving is serving, whatever its log says. Absent (the startup caller, whose spawn retry owns its
    own liveness check), the wait is exactly what it was.
    """
    start = clock()
    deadline = start + timeout
    # The crash-recovery caller asks the same question immediately before this wait, so the first
    # in-loop ask is scheduled one interval out rather than duplicating it on the very first poll.
    next_liveness = start + liveness_poll
    while True:
        try:
            if transport("GET", "/health", None).status == "ready":
                return _HealthWait.READY
        except (XcuitestChannelError, _TransportFailure):
            pass  # runner not accepting connections yet; keep probing until the deadline
        now = clock()
        if runner_alive is not None and now >= next_liveness:
            next_liveness = now + liveness_poll
            if not runner_alive():
                return _HealthWait.GONE
        if now >= deadline:
            return _HealthWait.TIMED_OUT
        sleep(poll)


def _observe_stall(hook: Callable[[], None], logger: logging.Logger) -> None:
    """Let the diagnostics hook look at a declared crash, absorbing whatever it does (BE-0361).

    A capture runs on the failure path it documents, so a broken hook must cost that path a log line
    and nothing else — never the exception that would replace the crash diagnostic the caller is
    about to raise.

    The hook takes no argument: naming the trigger (and therefore the directory the capture writes) is
    the environment's business, not the channel's, so nothing the channel passes can reach a path.
    """
    try:
        hook()
    except Exception:
        # With the traceback: a capture that regresses would otherwise log the same opaque line on
        # every CI failure, leaving the diagnostics themselves undiagnosable.
        logger.warning(
            "stall diagnostics hook failed; the crash diagnosis is unaffected", exc_info=True
        )


def _runner_gone_mid_run(
    method: str, path: str, crash: XcuitestRunnerCrashError
) -> XcuitestRunnerCrashError:
    """The crash diagnostic for a runner that will never answer on its port again.

    One wording for two moments the same fact can be observed: the runner was already gone when the
    crash was declared, or its death became observable while the recovery wait ran (BE-0360).
    """
    return XcuitestRunnerCrashError(
        f"runner channel {method} {path} failed: the runner is gone mid-run — its process exited or "
        "its test run already ended (it will not recover on this port)",
        method=method,
        delivered=crash.delivered,
    )


def _with_crash_recovery(
    inner: TransportFn,
    *,
    health: Callable[[float], _HealthWait],
    runner_alive: Callable[[], bool] | None = None,
    on_stall: Callable[[], None] | None = None,
    recovery_timeout: float = _RECOVERY_TIMEOUT_SECONDS,
    max_recoveries: int = _MAX_CRASH_RECOVERIES,
    max_hung_calls: int = _MAX_HUNG_CALLS,
) -> TransportFn:
    """Wrap *inner* so a mid-run runner crash surfaces deterministically, not as a lost gesture (BE-0287).

    The BE-0207 retry seam (*inner*) absorbs a sub-second blip; a crash outlives its budget and raises
    `XcuitestRunnerCrashError`. This layer catches that and decides by the same `delivered` split the
    seam already draws. An idempotent read — or a write that never reached the runner — waits for the
    runner to come back (via *health*, the bounded `/health` poll) and re-issues, because re-reading is
    safe. The re-issue is itself protected: a runner that crashes *again* on the re-issued call is
    recovered anew, up to `max_recoveries` consecutive crashes, so a flapping runner is ridden out
    instead of failing the run on the second crash. A write that may already have been delivered is
    never re-sent (double-actuation risk) and fails with a distinct crash diagnostic, so the run
    stops on an honest "the runner died
    mid-gesture" rather than a misleading `actual='idle'`. Every crash — recovered or not — is logged as
    visibly as the retry seam logs a retried blip (BE-0287 Unit 4), so a crashed-and-recovered run is
    never indistinguishable from one that never crashed.

    `/health` itself passes straight through: it is the probe recovery leans on, so wrapping it would
    recurse (and block a startup `await_ready` for the whole recovery window on a runner not yet up).

    A *wedged automation session* is split out of that flap-riding (BE-0354): when the same call keeps
    **hanging** — reaching the runner and never being answered — across `max_hung_calls` consecutive
    crashes while `/health` keeps replying, the runner's HTTP server is fine and the machinery behind
    it is not. No amount of re-issuing can fix that, so the crash is raised at once with its own
    diagnostic and the pipeline's device-level retry takes over. A connection-level failure (refused,
    or reset mid-response) is the genuinely crashing runner this loop was built for and keeps riding.

    `runner_alive` splits recovery on whether the runner can still come back: when the environment
    supplies its liveness check and it reports the runner **gone** — the `xcodebuild` process exited,
    or its XCTest run already ended and left the parent lingering (BE-0354) — nothing will answer
    `/health` on this port again (nothing respawns it mid-recovery), so recovery fails fast instead of
    polling the dead port for the whole window — the pipeline's crash recovery then leases a fresh
    device and re-runs the scenario. Absent (a test fake) or reporting the runner alive, it changes
    nothing: an alive-but-unreachable runner stays BE-0287's recoverable case and waits out *health*.

    That question is asked twice over: here, before the wait, and again by *health* while the wait runs
    (BE-0360). Sampling it only here would catch just the runner already gone when the crash was
    declared, while the ordinary mid-run crash — whose `xcodebuild` exit and suite result line both
    follow it — became observable a moment later and was waited out in full. A wait that ends on that
    verdict is reported with the same "gone" diagnostic as the early exit, not as a window waited out.

    `on_stall`, when the environment supplies it, is called once per declared crash — before recovery
    decides anything, so ahead of both liveness samples above — and a bounded capture of the Simulator
    and host state therefore runs while that state still exists (BE-0361). It is an observer: its own
    failure is swallowed, and neither it nor anything it returns reaches the crash verdict below.
    """
    logger = logging.getLogger("bajutsu.xcuitest.channel")

    def transport(method: str, path: str, body: Mapping[str, Any] | None) -> _Reply:
        if path == "/health":
            return inner(method, path, body)
        recoveries = 0
        hangs = 0
        while True:
            try:
                return inner(method, path, body)
            except XcuitestRunnerCrashError as crash:
                # Consecutive, so a single hang between two connection-level crashes never accrues
                # toward the wedge verdict — only a call that keeps being accepted and never answered.
                hangs = hangs + 1 if crash.hung else 0
                logger.warning(
                    "runner channel %s %s: the runner became unreachable past the retry budget — a mid-run crash: %s",
                    method,
                    path,
                    crash,
                )
                if on_stall is not None:
                    _observe_stall(on_stall, logger)
                if not _is_retry_eligible(method, delivered=crash.delivered):
                    raise XcuitestRunnerCrashError(
                        f"runner channel {method} {path} failed after delivery: the runner did not confirm "
                        "the write, which may have been lost and cannot be safely re-applied (mid-run crash)",
                        method=method,
                        delivered=crash.delivered,
                    ) from crash
                if hangs >= max_hung_calls:
                    # The runner accepted this call `hangs` times and answered none while `/health`
                    # kept replying: its automation session is wedged, not flapping. Only a
                    # device-level remedy can clear that, so hand over now instead of spending the
                    # remaining recovery cycles — each a timeout ladder plus a health wait — on it.
                    raise XcuitestRunnerCrashError(
                        f"runner channel {method} {path} failed: the call reached the runner and hung "
                        f"{hangs} times while /health kept answering — a wedged automation session, "
                        "which no re-issue can clear (mid-run crash)",
                        method=method,
                        delivered=crash.delivered,
                        hung=True,
                    ) from crash
                recoveries += 1
                if recoveries > max_recoveries:
                    # The runner keeps crashing on each re-issue: it is not a single flake but a runner
                    # that never stabilizes, so fail loudly rather than loop. Distinct from the
                    # "did not recover" (health never came back) diagnostic below.
                    raise XcuitestRunnerCrashError(
                        f"runner channel {method} {path} failed: the runner crashed {recoveries} times and "
                        f"stayed unstable past the {max_recoveries}-recovery budget (mid-run crash)",
                        method=method,
                        delivered=crash.delivered,
                    ) from crash
                if runner_alive is not None and not runner_alive():
                    # The runner is gone for good — its process exited, or its XCTest run ended and
                    # only the `xcodebuild` parent lingers (BE-0354). Either way nothing will answer
                    # `/health` on this port again, so polling the recovery window would only wait out
                    # an inevitable failure. Fail fast with a distinct diagnostic; the pipeline's crash
                    # recovery then leases a fresh device and re-runs the scenario. A runner merely
                    # unreachable (alive) skips this and waits out `health` below, so BE-0287's
                    # recoverable case is unchanged. Kept as an early exit even though *health* now
                    # re-asks the same question (BE-0360): it costs one call, and it spares a runner
                    # already gone here a poll interval it does not need.
                    raise _runner_gone_mid_run(method, path, crash) from crash
                waited = health(recovery_timeout)
                if waited is _HealthWait.GONE:
                    # The same death, observed a moment later: the `xcodebuild` exit and the suite's
                    # result line both *follow* the crash, so the ordinary mid-run crash becomes
                    # observable during the wait rather than before it (BE-0360). Report it the way a
                    # runner found gone before the wait is reported, not as a window waited out.
                    raise _runner_gone_mid_run(method, path, crash) from crash
                if waited is not _HealthWait.READY:
                    raise XcuitestRunnerCrashError(
                        f"runner channel {method} {path} failed: the runner crashed mid-run and did not "
                        f"recover within {recovery_timeout}s",
                        method=method,
                        delivered=crash.delivered,
                    ) from crash
                logger.warning(
                    "runner channel %s %s: the runner recovered from a mid-run crash; re-issuing the "
                    "idempotent call (recovery %d/%d)",
                    method,
                    path,
                    recoveries,
                    max_recoveries,
                )
                # Loop to re-issue. A re-issue that crashes again is caught here and recovered anew, up
                # to max_recoveries, so a runner that flaps across consecutive calls is ridden out
                # instead of failing the run on the second crash. `_is_retry_eligible` still gates every
                # attempt, so a delivered write is never re-sent even once.

    return transport


def _is_stale(conn: http.client.HTTPConnection) -> bool:
    """Whether *conn*'s socket has already been closed by the peer since the last call (BE-0407 Unit 11).

    The runner's own idle timeout (`HTTPServer.swift`'s `SO_RCVTIMEO`) can close a kept-alive
    connection between two calls with nothing on this side to notice until the next attempt to use
    it — and by then, whether that attempt's write actually reached the runner first is genuinely
    ambiguous (TCP can accept a write into the local send buffer before the peer's earlier close is
    detected), which is exactly the ambiguity a POST must never guess through (double-actuation
    risk). A zero-timeout `select` plus a non-consuming peek answers the question *before* any byte
    of the next request is sent, so the ordinary idle-close case reconnects cleanly — indistinguishable
    from the very first call — rather than surfacing as an ambiguous send/receive failure to sort out
    after the fact.

    The peek narrows the window rather than closing it on its own: `SO_RCVTIMEO` can still fire
    between this check and the `conn.request(...)` that follows it, in which case the write lands on
    a socket the runner has already torn down. `delivered` is `True` by then, so a `GET` is simply
    re-issued on a fresh connection by `_with_retry`, while a `POST` is refused by `_is_retry_eligible`
    and fails loudly (delivery is genuinely unknown, so re-sending it would be the same double-
    actuation risk above) rather than getting the clean reconnect this check gives the common case.
    `_raw_http_transport` closes the remaining gap from the other end: it forces a reconnect once a
    connection has sat idle past `_KEEPALIVE_IDLE_RECONNECT_SECONDS`, so a connection old enough for
    this peek to matter is never old enough for the peer's 10s timer to have plausibly fired between
    the peek and the send.
    """
    sock = conn.sock
    if sock is None:
        return True
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        return bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def _raw_http_transport(host: str, port: int) -> TransportFn:
    """One HTTP attempt to the runner's loopback server, tagging failures for the retry seam (BE-0207).

    A failure before the socket is confirmed open (a fresh `connect()`, or `_is_stale`'s check that a
    reused one still is) means the request never reached the runner (`delivered` stays `False`); any
    later failure — a partial send or a response-side timeout — may have reached the runner
    (`delivered` is `True`). `_with_retry` and the BE-0287 crash-recovery use that split to decide
    what is safe to re-issue, so the flip is deliberately conservative: a write whose bytes may have
    started reaching the runner is never re-sent (a double-actuation risk), it fails loudly instead.

    The connection itself is kept open and reused across calls (BE-0407 Unit 11) rather than a fresh
    one paid for every call: `state` carries it (or `None`, before the first call and again after any
    failure discards it) across this closure's calls, since the driver issues them one at a time and
    holds no other reference to the socket. `HTTPServer.swift` answers in kind, keeping its own end of
    the connection open. A discard-and-reconnect on the next call is `_with_retry`'s job, unchanged;
    this only changes when a connection is *opened* — never how a failure once open is handled.
    """
    state = _ConnState()

    def transport(method: str, path: str, body: Mapping[str, Any] | None) -> _Reply:
        # One `app.snapshot()` per `/elements` (BE-0105), so the bounded read window still covers a
        # cold first snapshot; a write gets the longer actuation window (`_timeout_for`) since it
        # can't be retried after delivery — both still fail a wedged runner in a reasonable window.
        timeout = _timeout_for(method)
        conn = state.conn
        idle_too_long = (
            conn is not None
            and state.last_success_at is not None
            and time.monotonic() - state.last_success_at > _KEEPALIVE_IDLE_RECONNECT_SECONDS
        )
        if conn is not None and (idle_too_long or _is_stale(conn)):
            conn.close()
            conn = None
            state.conn = None
        delivered = False
        succeeded = False
        try:  # pragma: no cover - exercised on-device against the real runner, not on the gate
            if conn is None:
                conn = http.client.HTTPConnection(host, port, timeout=timeout)
                conn.connect()  # split from send: a connect failure is safe to re-issue, a send failure isn't
                state.conn = conn
            else:
                # A reused connection keeps whatever timeout its last call set otherwise — a read
                # reusing a connection an actuation last used must not inherit the longer window.
                assert conn.sock is not None
                conn.sock.settimeout(timeout)
            delivered = True  # the socket is confirmed open; a later send/read failure may have reached the runner
            payload = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            reply = _decode(path, resp.status, resp.read())
        except OSError as exc:  # pragma: no cover - see above
            # A socket timeout on an open connection is the hang BE-0354 keys on; a refused connect
            # or a reset mid-response is the runner going away, which recovery still rides out.
            raise _TransportFailure(
                str(exc), delivered=delivered, hung=delivered and isinstance(exc, TimeoutError)
            ) from exc
        else:
            succeeded = True
            # Recorded now, not at the top of this call: it must reflect when the runner last
            # answered, so the next call's idle gap is measured from here.
            state.last_success_at = time.monotonic()
            return reply
        finally:
            # A connection any failure touched — an `OSError` above, or a non-`OSError` decode failure
            # from a response `_is_stale` could not have caught — is never reused; `succeeded` is the
            # single source of truth for that, so nothing here depends on which exception type fired.
            # Closed, not merely dropped: the `_TransportFailure` raised above keeps this frame — and
            # so this socket, and the runner connection slot behind it — alive for as long as the
            # crash-recovery layer holds the error it becomes.
            if not succeeded:
                state.conn = None
                if conn is not None:
                    conn.close()

    return transport


def _http_transport(
    host: str,
    port: int,
    *,
    runner_alive: Callable[[], bool] | None = None,
    on_stall: Callable[[], None] | None = None,
) -> tuple[TransportFn, TransportFn]:
    """The real transport, plus the raw single-attempt transport used for fast health probes.

    Two layers over the raw socket: BE-0207's `_with_retry` smooths a sub-second blip, and BE-0287's
    `_with_crash_recovery` rides out a mid-run crash (idempotent re-issue) or fails loudly on a write it
    must not re-send. Both the crash-recovery health poll and the cold-spawn liveness probe
    (`XcuitestDriver.health_ready`, BE-0319) need probing to stay fast, not retried: `_with_retry`
    re-issues a down connection up to `_MAX_ATTEMPTS` times with backoff, so routing a "single-shot"
    probe through it would silently cost over a second per call instead of one quick attempt — the raw
    transport is returned alongside the wrapped one so both callers can reuse this same instance.

    `runner_alive`, when the environment supplies its liveness check, lets crash-recovery fail fast on
    a runner that cannot come back — its process exited, or its XCTest run already ended (BE-0354) —
    rather than polling the dead port for the whole recovery window. It reaches both places that ask
    the question: the check before the wait, and the wait itself, which re-asks it as the window runs
    (BE-0360). Absent, recovery is exactly BE-0287's. `on_stall` is the environment's bounded
    diagnostics capture (BE-0361), an observer of the same crash declaration. Both are keyword-only,
    so two adjacent optional callbacks cannot be swapped at a call site.
    """
    raw = _raw_http_transport(host, port)
    wrapped = _with_crash_recovery(
        _with_retry(raw),
        health=lambda timeout: _await_health(raw, timeout=timeout, runner_alive=runner_alive),
        runner_alive=runner_alive,
        on_stall=on_stall,
    )
    return wrapped, raw
