"""The iOS driver: a resident XCUITest runner behind the common `Driver` seam (BE-0019)."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from bajutsu.common.devices.os import DeviceOS
from bajutsu.common.drivers import base, tracing
from bajutsu.common.drivers.actuation import Actuation, ActuationLog, Drained
from bajutsu.common.drivers.zorder import ZOrderSource

from ._drain_carry import _DrainCarry
from ._functions import (
    _as_float,
    _await_health,
    _http_transport,
    _parse_drain_fold,
    _parse_tap_drain_fold,
    _tip_is_up,
    _to_element,
)
from ._health_wait import _HealthWait
from ._reply import _Reply
from ._shared import _OK, _TIPKIT_DISMISS_REGION, TransportFn
from .xcuitest_channel_error import XcuitestChannelError

_STALE = "stale"  # the resolved handle no longer maps to a live element (the screen changed)
_NOT_FOUND = "not-found"  # the runner could not act on the handle (no matching live element)
_NOT_HITTABLE = "not-hittable"  # the element is live but not reachable at its own point right now
# `setPickerValue` only: the wheel was resolved and adjusted, but never showed the requested value —
# it has no such row (BE-0356). Distinct from `_NOT_FOUND`, whose "no actuatable element" message
# names the selector and would misreport a perfectly resolved, live wheel.
_VALUE_NOT_FOUND = "value-not-found"

# Bounded re-resolution retry for a STALE actuation handle (BE-0289), held separate from BE-0207's
# transport retry above even though it starts at the same values: the two loops bound different
# things, so re-tuning one must not silently move the other. The re-query round-trip is the
# condition wait, not a fixed sleep; this backoff only spaces the attempts.
_STALE_MAX_ATTEMPTS = 3
# exponential per retry: 0.5s, 1.0s, … between re-resolve attempts
_STALE_BACKOFF_BASE_SECONDS = 0.5

# iOS reports every frame and coordinate in points, so that is the space stamped on this backend's
# actuation records.
_UNIT = "point"


class XcuitestDriver:
    """Driver for the iOS Simulator via a resident XCUITest runner (semantic, identifier-based)."""

    name = "xcuitest"

    # Capabilities: a semantic tap (by handle, no coordinates), native condition waiting, and
    # two-finger gestures. No NETWORK — network evidence comes from
    # the app-side collector (BE-0020 boundary), not the actuator. The whole device-control family
    # (`DEVICE_CONTROL_ALL`) and the permission grants because xcuitest shares the iOS Simulator
    # lifecycle, which wires a real simctl-backed `DeviceControl` for its runs too (BE-0128;
    # per-operation tokens since BE-0212). This is the *static* set; a real device (`deviceType:
    # device`) drops the simctl-backed capabilities at run time via `backends.capabilities_for_run`,
    # since simctl reaches only the Simulator (BE-0238). A class constant so the preflight (BE-0082)
    # reads it via backends.capabilities_for without constructing a driver.
    CAPABILITIES = (
        frozenset(
            {
                base.Capability.QUERY,
                base.Capability.ELEMENTS,
                base.Capability.SCREENSHOT,
                base.Capability.SEMANTIC_TAP,
                base.Capability.CONDITION_WAIT,
                base.Capability.MULTI_TOUCH,
                base.Capability.TEXT_SELECTION,
                base.Capability.HANDLE_SYSTEM_ALERT,
                base.Capability.PICKER_WHEEL,
                base.Capability.HANDLE_TIPKIT_TIP,
            }
        )
        | base.DEVICE_CONTROL_ALL
        | base.IOS_PERMISSION_CAPABILITIES
    )

    def __init__(
        self,
        *,
        transport: TransportFn | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        runner_alive: Callable[[], bool] | None = None,
        on_stall: Callable[[], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        device_os: DeviceOS | None = None,
        zorder: ZOrderSource | None = None,
    ) -> None:
        # The parsed OS version of the device this drives (BE-0358), or None when the environment
        # could not name one. Nothing here branches on it yet — it exists so a driver-level failure
        # can name the OS it happened on, and so the first genuinely per-OS decision has one route
        # to read. Set per construction, not per lease, so it follows a mid-run device replacement
        # instead of going stale.
        self.device_os = device_os
        if transport is not None:
            # A test fake serves both roles: it has no BE-0207 retry to distinguish away.
            self._transport = transport
            self._probe_transport = transport
        else:
            # `runner_alive` lets crash-recovery fail fast on a runner that cannot come back; the
            # environment supplies its liveness check, None keeps BE-0287's recovery. `on_stall` is
            # the environment's diagnostics capture (BE-0361), an observer of the same crash
            # declaration.
            self._transport, self._probe_transport = _http_transport(
                host, port, runner_alive=runner_alive, on_stall=on_stall
            )
        # Every `/tap` reply's own folded drain (BE-0407 Unit 6) that `drain_interruptions()` has not
        # yet reported, accumulated rather than replaced: the runner drains its store as part of
        # answering *every* `/tap` (`withDrain`, BajutsuKit), so two taps between one caller's drains
        # each contribute genuinely new, non-overlapping data, and neither may be dropped in favor of
        # the other. `carry.is_current` is the separate question of whether this carry is *still the
        # complete answer* — true only while the most recent driver call was that very tap; any other
        # call since (a query, a stale-retry's re-resolution, another actuation) may itself have let
        # something new interrupt, so `drain_interruptions()` must ask the wire and merge it with
        # what is already carried rather than trusting the carry alone. Tracked by wrapping
        # `self._transport` below rather than at each of `tap` / `double_tap` / `long_press` /
        # `tap_point`'s call sites, so every path to `/tap` — present and future — is covered by one
        # rule instead of several copies that could drift apart.
        self._drain_carry = _DrainCarry()
        _channel_transport = self._transport
        carry = (
            self._drain_carry
        )  # captured below instead of `self` — see `_DrainCarry`'s docstring

        def _tracking_transport(method: str, path: str, body: Mapping[str, Any] | None) -> _Reply:
            reply = _channel_transport(method, path, body)
            if path != "/tap":
                # This call may itself have let something new interrupt, or (the stale-retry
                # re-resolution case) run *between* the tap the carry describes and its own retry, so
                # the carry can no longer stand alone as the complete answer — but it is not
                # discarded: `drain_interruptions()` still merges it with a fresh wire drain.
                carry.is_current = False
                return reply
            # `withDrain` (BajutsuKit) folds the runner's own drain into *every* `/tap` reply,
            # whatever its actuation status — a stale or not-hittable tap still ran the fold, since
            # an interruption can land mid-actuation regardless of whether the tap itself lands.
            # `None` (as opposed to a present-but-empty fold) means a runner that predates Unit 6 —
            # nothing to carry, and the wire must still be asked for this tap's own window too.
            fold = _parse_tap_drain_fold(reply.raw)
            if fold is None:
                carry.is_current = False
                return reply
            carry.drained = carry.drained.merged_with(fold)
            carry.is_current = True
            return reply

        self._transport = _tracking_transport
        # BE-0415: one more optional wrap, folded in only when a trace is already open at
        # construction time. `--trace-driver` is a whole-run flag, so a driver built while no trace
        # is open never traces for the rest of its lifetime either (including warm-resident reuse
        # across scenarios) — checked once here instead of per call.
        if tracing.current_trace() is not None:
            _traceable_transport = self._transport

            def _traced_transport(method: str, path: str, body: Mapping[str, Any] | None) -> _Reply:
                ctx = tracing.current_trace()
                if ctx is None:
                    return _traceable_transport(method, path, body)
                started_at = time.time()
                t0 = time.perf_counter()
                response: dict[str, Any] | None = None
                try:
                    reply = _traceable_transport(method, path, body)
                    if method == "POST":
                        response = {"status": reply.status}
                    return reply
                finally:
                    ctx.record(
                        "transport",
                        f"{method} {path}",
                        started_at,
                        time.perf_counter() - t0,
                        response,
                    )

            self._transport = _traced_transport
        # Injectable so the stale re-resolution backoff (BE-0289) adds no wall time under test.
        self._sleep = sleep
        # The device screen size (BE-0326), fetched once from the runner; fixed for a run.
        self._screen: base.Point | None = None
        # What this driver actually actuated, drained per step by the run loop.
        self._actuations = ActuationLog()
        # The raw `GET /elements` body behind the last query (`base.RawSourceProvider`, the `rawTree`
        # capture kind), kept undecoded: `last_raw_source()` is read only on the rare step that actually
        # requests `rawTree` capture, so decoding here on every query — the common, capture-off case —
        # would be pure waste. None until the first read. No `parsed_input`: unlike adb's resident
        # channel, nothing here narrows the runner's own reply before it becomes `elements`.
        self._raw_bytes: bytes | None = None
        # The in-app responder that measures `nativeZ` (BE-0355). None when the run allocated no
        # port, or when the app never links BajutsuKit — either way every element keeps `None`.
        self._zorder = zorder

    # --- the channel ---

    def _query_with_handles(
        self, *, apply_native_z: bool = True
    ) -> tuple[list[base.Element], dict[int, str]]:
        """A snapshot plus a map from each element's object identity to its handle.

        Keyed by `id()` of the returned dicts: `resolve_unique` returns one of these very objects, so
        the resolved element's handle is an O(1) identity lookup — the element is acted on by the
        exact handle the runner minted for it, never re-resolved on the runner side.

        Args:
            apply_native_z: Whether to pay the `/zorder` round trip (BE-0407 Unit 10). `nativeZ` is
                diagnostic only — nothing in selector resolution reads it (`resolve_unique`'s
                `_collapse_identical_duplicates` deliberately omits it) — so a caller that only
                resolves a handle to act on and discards the rest of the tree gains nothing from it.
                `False` for those internal resolutions; `True` (the default) for `query()`, whose
                result reaches evidence and the serve read API.
        """
        reply = self._transport("GET", "/elements", None)
        self._raw_bytes = reply.raw
        elements, handles = self._parse_elements(reply)
        if apply_native_z:
            self._apply_native_z(elements)
        return elements, handles

    def _apply_native_z(self, elements: list[base.Element]) -> None:
        """Fill each element's `nativeZ` from the app's own reading of where it sits (BE-0355).

        Read here rather than in `_to_element` because the answer comes from a second channel — the
        app itself, not the runner — and must be asked for the same moment this query describes.
        An identifier the runner reports more than once names no single element, so it takes no
        position: the alternative is handing one element another's reading, which is exactly the
        wrong-but-authoritative value this field exists to avoid.
        """
        if self._zorder is None:
            return
        positions = self._zorder.positions()
        if not positions:
            return
        seen: dict[str, int] = {}
        for el in elements:
            identifier = el["identifier"]
            if identifier is None:
                continue
            seen[identifier] = seen.get(identifier, 0) + 1
        for el in elements:
            identifier = el["identifier"]
            if identifier is None or seen.get(identifier, 0) != 1:
                continue
            el["nativeZ"] = positions.get(identifier)

    @staticmethod
    def _parse_elements(reply: _Reply) -> tuple[list[base.Element], dict[int, str]]:
        """Turn a runner element reply into elements plus an identity→handle map (BE-0105).

        Shared by the app-tree query (`/elements`) and the SpringBoard alert query
        (`/systemAlert/query`, BE-0316): both mint a handle per element the same way, so both feed
        `resolve_unique` and then act by the exact handle the runner minted.
        """
        elements: list[base.Element] = []
        handles: dict[int, str] = {}
        for item in reply.elements or []:
            handle = item.get("handle")
            if not handle:  # a missing handle is a malformed response, not a coercible empty string
                raise XcuitestChannelError(f"runner returned an element without a handle: {item!r}")
            el = _to_element(item)
            elements.append(el)
            handles[id(el)] = str(handle)
        return elements, handles

    def _resolve_handle(self, sel: base.Selector) -> tuple[str, base.Element]:
        """Resolve *sel* to a unique element Python-side; return its snapshot handle and the element.

        The element travels with the handle so the caller can record what it actuated without paying a
        second `/elements` round trip for the same resolution.

        Raises:
            ElementNotFound: Nothing matched.
            AmbiguousSelector: Several elements matched, with no `index` to disambiguate. Both are
                raised before any actuation request is sent.
        """
        elements, handles = self._query_with_handles(apply_native_z=False)
        el = base.resolve_unique(elements, sel)
        return handles[id(el)], el

    def _actuate(
        self,
        path: str,
        body: Mapping[str, Any],
        sel: base.Selector,
        *,
        gesture: str,
        element: base.Element,
        substitution: str | None = None,
    ) -> None:
        # A `stale` reply means the handle no longer maps to a live element, from one of two
        # pre-actuation points: the runner's `store.lookup` returns `stale` before touching anything
        # when the screen re-snapshotted, or the interaction itself raised an element-resolution
        # failure ("No matches found") that the runner catches and reports as `stale`
        # (Router.onMainCatching). Both precede event synthesis — XCUITest resolves the element, and
        # raises if it is gone, *before* it synthesizes the tap/gesture — so re-issuing cannot
        # double-actuate. Re-query and re-actuate while the same selector still resolves uniquely
        # (BE-0289). Zero/many matches raise ElementNotFound / AmbiguousSelector out of
        # `_resolve_handle` and fail immediately, spending no further attempts.
        request: dict[str, Any] = dict(body)
        for attempt in range(1, _STALE_MAX_ATTEMPTS + 1):
            # Recorded per attempt, before the transport answers: a step that failed to actuate still
            # shows what it aimed at, and a stale-retried gesture shows both the element that went
            # stale and the one that was finally actuated. `points` stays empty — the runner picks the
            # touch point on the far side of the handle, so the driver has no coordinate to state.
            self._actuations.record(
                Actuation(
                    gesture=gesture,
                    via="handle",
                    unit=_UNIT,
                    frame=element["frame"],
                    target=element["identifier"],
                    duration_s=_as_float(body.get("duration")),
                    scale=_as_float(body.get("scale")),
                    radians=_as_float(body.get("radians")),
                    substitution=substitution,
                )
            )
            reply = self._transport("POST", path, request)
            # Stamp the attempt just recorded with the runner's answer, so a stale-retried gesture
            # does not leave several identical records with nothing saying which one landed.
            self._actuations.settle(reply.status == _OK)
            if reply.status == _OK:
                return
            if reply.status == _STALE:
                if attempt == _STALE_MAX_ATTEMPTS:
                    raise base.ElementNotFound(f"element vanished (stale handle): {sel!r}")
                if attempt > 1:
                    # The first retry re-queries at once (BE-0407 Unit 12): the re-query below is
                    # itself a wait, on a screen that may already have settled by the time the
                    # `stale` reply came back, so a fixed sleep first only adds wall time. A second
                    # stale in a row is different — the same re-query just failed to catch up once
                    # already — so backoff still applies from here on.
                    self._sleep(_STALE_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
                request["handle"], element = self._resolve_handle(sel)
                continue
            if reply.status == _NOT_FOUND:
                raise base.ElementNotFound(f"no actuatable element for: {sel!r}")
            if reply.status == _NOT_HITTABLE:
                # Distinct from `_STALE`: the element is live and correctly resolved, but XCTest's own
                # `isHittable` refuses it (covered by another element, or offscreen) — not a race to
                # retry here, so it surfaces once as a tappability failure. Any bounded recovery
                # (a scroll) happens above the driver, in the orchestrator.
                raise base.ElementNotTappable(f"element resolved but not hittable: {sel!r}")
            if reply.status == _VALUE_NOT_FOUND:
                # `setPickerValue` alone returns this, and its request body carries the value, so the
                # failure names what never landed without threading it through every other actuation.
                # A `SelectorError`, like `select_option`'s absent-option raise on the web backend, so
                # the run loop's existing selector-failure handling covers it.
                raise base.ElementNotFound(
                    f"picker wheel has no value {request.get('value')!r}: {sel!r}"
                )
            # Any other status (e.g. an "error" from a 500 / malformed response) is a runner failure,
            # not a test outcome — fail loudly rather than masking it as element-not-found.
            raise XcuitestChannelError(
                f"runner error actuating {path} (status={reply.status}): {sel!r}"
            )

    # --- Driver Protocol ---

    def drain_actuations(self) -> Drained:
        """The concrete actuations performed since the last drain (`ActuationReporter`)."""
        return self._actuations.drain()

    def last_raw_source(self) -> base.RawSource | None:
        """The raw `GET /elements` body behind the last query (`base.RawSourceProvider`).

        Decoded here, not at query time: this is read only on the rare step that requests `rawTree`
        capture, so paying the decode on every query — the common, capture-off case — would be waste.
        """
        if self._raw_bytes is None:
            return None
        return base.RawSource(text=self._raw_bytes.decode("utf-8"), suffix=".json")

    def query(self) -> list[base.Element]:
        elements, _ = self._query_with_handles()
        return elements

    def tap(self, sel: base.Selector) -> None:
        handle, el = self._resolve_handle(sel)
        try:
            self._actuate("/tap", {"handle": handle}, sel, gesture="tap", element=el)
        except base.ElementNotTappable as refused:
            self._tap_sole_reachable_descendant(sel, refused)

    def _tap_sole_reachable_descendant(
        self, sel: base.Selector, refused: base.ElementNotTappable
    ) -> None:
        """Tap the one reachable named descendant of a refused target, or re-raise naming the rest.

        iOS can report a container inflated over the control it wraps — a SwiftUI `Stepper` whose
        accessibility element spans its whole form row — and refuse a tap on the container while the
        control inside it is perfectly reachable. Where exactly one named descendant is reachable,
        there is no choice to make and the tap goes there. Where none or several are, there is a
        choice, so this re-raises rather than making it: an author cannot predict which of two
        equally reachable children a driver would pick, and picking one anyway is the guess prime
        directive 2 forbids. The message then names the candidates, so the author can select one
        directly instead of reading "not hittable" about an element plainly on screen.

        Scoped to `tap` on purpose. A long-press or a two-finger gesture redirected to a child is a
        different intent, not the same intent reaching its target.

        Raises:
            ElementNotTappable: Chained from *refused*, so the original refusal stays the cause.
        """
        elements, handles = self._query_with_handles(apply_native_z=False)
        # Re-resolved from a fresh tree rather than reusing the refused element: the refusal may have
        # been the first sign of a screen still settling, and a candidate list read off a stale
        # snapshot could offer an element that has since moved out of the container.
        target = base.resolve_unique(elements, sel)
        candidates = base.redirect_candidates(elements, target)
        if not candidates or len(candidates) > base.MAX_REDIRECT_CANDIDATES:
            raise refused
        # `redirect_candidates` only ever returns named elements, so the identifier is never None
        # here; binding it in the comprehension is what lets the selector below stay typed.
        reachable = [
            (el, name)
            for el in candidates
            if (name := el["identifier"]) is not None and self._is_hittable(handles[id(el)])
        ]
        if len(reachable) != 1:
            named = ", ".join(repr(el["identifier"]) for el in candidates)
            raise base.ElementNotTappable(
                f"element resolved but not hittable: {sel!r} — "
                f"{len(reachable)} of its {len(candidates)} named descendants are reachable "
                f"({named}), so none of them is the one this tap meant"
            ) from refused
        child, child_id = reachable[0]
        # Actuated by the child's own id, not the container's: `_actuate` re-resolves from `sel` on a
        # stale retry, and passing the container's selector would silently undo the redirect there.
        self._actuate(
            "/tap",
            {"handle": handles[id(child)]},
            {"id": child_id},
            gesture="tap",
            element=child,
            substitution="soleHittableDescendant",
        )

    def _is_hittable(self, handle: str) -> bool:
        """Whether the runner reports this handle reachable right now — the probe behind the redirect.

        Unlike `is_tappable`, this takes a handle already resolved from the caller's own snapshot, so
        the two never disagree about which element was asked about. A `stale` or `not-found` reply
        reads as unreachable: the candidate came from the very query this handle did, so either answer
        means the screen moved under the probe and the offer is no longer one to make.
        """
        reply = self._transport("POST", "/isHittable", {"handle": handle})
        if reply.status == _OK:
            return True
        if reply.status in (_STALE, _NOT_FOUND, _NOT_HITTABLE):
            return False
        raise XcuitestChannelError(f"runner error checking isHittable (status={reply.status})")

    def is_tappable(self, sel: base.Selector) -> bool:
        """Whether `sel` resolves to a unique element XCTest's own `isHittable` reports as reachable.

        A pure query, unlike `tap`/`double_tap`/`long_press`: it never actuates and never retries a
        `stale` reply, so a caller (the scroll-recovery loop) can call it repeatedly with no side
        effects, resolving fresh from the current screen every time. Not found means "not tappable"
        (`False`), matching every other backend's convention for a target not yet in the tree, rather
        than propagating; an ambiguous selector still raises `AmbiguousSelector` immediately, since
        occlusion is a different question from selector ambiguity. A `stale` handle (the screen
        changed between resolving and asking) also reads as `False` rather than retried, since the
        caller re-resolves fresh on its own next call. Any other status is a genuine runner/channel
        problem, not a test outcome, and raises loudly (`XcuitestChannelError`) exactly as `_actuate`
        does for the same class of reply, rather than being folded into a misleadingly clean `False`.
        """
        try:
            handle, _el = self._resolve_handle(sel)
        except base.ElementNotFound:
            return False
        reply = self._transport("POST", "/isHittable", {"handle": handle})
        if reply.status == _OK:
            return True
        if reply.status in (_STALE, _NOT_FOUND, _NOT_HITTABLE):
            return False
        raise XcuitestChannelError(
            f"runner error checking isHittable (status={reply.status}): {sel!r}"
        )

    def double_tap(self, sel: base.Selector) -> None:
        handle, el = self._resolve_handle(sel)
        self._actuate("/tap", {"handle": handle, "taps": 2}, sel, gesture="doubleTap", element=el)

    def long_press(self, sel: base.Selector, duration: float) -> None:
        handle, el = self._resolve_handle(sel)
        self._actuate(
            "/tap",
            {"handle": handle, "duration": duration},
            sel,
            gesture="longPress",
            element=el,
        )

    def tap_point(self, p: base.Point) -> None:
        # A raw coordinate tap (system alerts and the like), the one path with no element/handle.
        self._actuations.record(Actuation(gesture="tap", via="coordinate", unit=_UNIT, points=(p,)))
        reply = self._transport("POST", "/tap", {"point": [p[0], p[1]]})
        if reply.status != _OK:
            raise XcuitestChannelError(f"coordinate tap failed ({reply.status}) at {p}")

    def pinch(self, sel: base.Selector, scale: float) -> None:
        handle, el = self._resolve_handle(sel)
        self._actuate(
            "/gesture",
            {"handle": handle, "kind": "pinch", "scale": scale},
            sel,
            gesture="pinch",
            element=el,
        )

    def rotate(self, sel: base.Selector, radians: float) -> None:
        handle, el = self._resolve_handle(sel)
        self._actuate(
            "/gesture",
            {"handle": handle, "kind": "rotate", "radians": radians},
            sel,
            gesture="rotate",
            element=el,
        )

    def swipe(self, frm: base.Point, to: base.Point) -> None:
        self._actuations.record(
            Actuation(gesture="swipe", via="coordinate", unit=_UNIT, points=(frm, to))
        )
        reply = self._transport("POST", "/swipe", {"from": [frm[0], frm[1]], "to": [to[0], to[1]]})
        if reply.status != _OK:
            raise XcuitestChannelError(f"swipe failed ({reply.status})")

    def viewport(self) -> base.Point:
        # The device screen size from the resident runner (BE-0326). The flattened element tree
        # excludes the app window and can hold buffered off-screen ScrollView children, so
        # `screen_size_from_elements` overshoots the screen — a `scroll` stop condition off that would
        # judge an off-screen center as on-screen and drive the gesture off-screen. Cached for the run.
        if self._screen is None:
            reply = self._transport("GET", "/screen", None)
            if reply.status != _OK or reply.size is None:
                raise XcuitestChannelError(f"screen size unavailable ({reply.status})")
            self._screen = reply.size
        return self._screen

    def scroll(self, frm: base.Point, to: base.Point) -> None:
        # A non-inertial scroll (BE-0326): the resident runner's `/scroll` holds the drag at its end
        # before lifting, so the scroll view settles where the gesture left it rather than flinging
        # past the target — the contract the `scroll` action's bounded re-query loop relies on. A
        # plain `/swipe` lifts with residual velocity, so iOS carries the content onward.
        self._actuations.record(
            Actuation(gesture="scroll", via="coordinate", unit=_UNIT, points=(frm, to))
        )
        reply = self._transport("POST", "/scroll", {"from": [frm[0], frm[1]], "to": [to[0], to[1]]})
        if reply.status != _OK:
            raise XcuitestChannelError(f"scroll failed ({reply.status})")

    def select_option(self, sel: base.Selector, option: str) -> None:  # noqa: ARG002  # Driver shape
        raise base.UnsupportedAction(
            "selectOption は <select> を持つ web バックエンド専用; iOS ネイティブに <select> はない"
        )

    def set_picker_value(self, sel: base.Selector, value: str) -> None:
        # Handle-based like `tap`, not coordinate-based like `swipe` / `drag` (BE-0356): the runner
        # calls `adjust(toPickerWheelValue:)` on the element XCTest already resolved, so the wheel
        # lands on the named row rather than wherever a drag of a guessed distance happens to stop.
        # `value` never reaches the actuation record — like a typed string, it can hold a resolved
        # secret (`drivers/actuation.py`).
        handle, el = self._resolve_handle(sel)
        self._actuate(
            "/setPickerValue",
            {"handle": handle, "value": value},
            sel,
            gesture="setPickerValue",
            element=el,
        )

    def handle_system_alert(self, sel: base.Selector, timeout: float) -> None:  # noqa: ARG002  # Driver shape
        # Query the alert once and tap the button `sel` names (BE-0316). The alert is out-of-process,
        # so the runner queries a second, on-demand `XCUIApplication` for `com.apple.springboard` and
        # mints a handle per alert button, exactly as it does for the app's own tree. Resolution
        # stays Python-side in `resolve_unique`, so the same zero / ambiguous / index discipline
        # every selector follows decides which button is tapped — no screenshot, no vision model.
        #
        # Waiting for the prompt to appear is no longer done here (BE-0406). It is a condition wait,
        # and the orchestrator owns those: `wait_for_system_alert` polls to the step's deadline and
        # drives the reactive guard between reads, which this loop could not — an alert the scenario
        # had declared held the screen for the whole call and the step failed for a prompt it never
        # saw. `timeout` stays on the signature so every backend keeps one shape; every caller now
        # passes zero.
        buttons, handles = self._parse_elements(self._transport("POST", "/systemAlert/query", {}))
        if not buttons:
            raise base.ElementNotFound(f"no system alert is showing: {sel!r}")
        el = base.resolve_unique(buttons, sel)
        # Handle-based like every other actuation here, and out of the app's own coordinate space, so
        # no point. `target` is usually unset: a SpringBoard button is addressed by visible label and
        # generally carries no identifier, and the label is a redaction risk the record won't take.
        self._actuations.record(
            Actuation(
                gesture="systemAlert",
                via="handle",
                unit=_UNIT,
                frame=el["frame"],
                target=el["identifier"],
            )
        )
        reply = self._transport("POST", "/systemAlert/tap", {"handle": handles[id(el)]})
        if reply.status != _OK:
            # The alert vanished between query and tap (dismissed itself, or the button moved off).
            raise base.ElementNotFound(
                f"system alert button vanished before tap (status={reply.status}): {sel!r}"
            )

    def system_alert_labels(self) -> list[str]:
        """The current SpringBoard alert's button labels, or [] when none is up (BE-0315).

        A single, non-blocking read reusing BE-0316's `/systemAlert/query` (the same route
        `handle_system_alert` resolves against) — the reactive guard reads it to decide whether a
        prompt is showing and which button its policy should tap. Unlabeled buttons are dropped:
        the policy resolves by visible label.
        """
        buttons, _ = self._parse_elements(self._transport("POST", "/systemAlert/query", {}))
        return [label for b in buttons if (label := b["label"])]

    def set_interruption_policy(
        self, rules: Sequence[tuple[frozenset[str], str]], governs: bool
    ) -> None:
        """Push the button policy the runner's interruption monitor applies.

        XCUITest resolves an alert that interrupts one of its interactions before synthesizing it,
        and with no monitor registered answers with the alert's own default button — granting a
        permission the scenario may have refused, with nothing in the report. The rules pushed here
        are the ones `AlertGuardConfig` already resolved from the scenario's own `rules`, so the
        choice stays on this side and the runner only applies it. `push_interruption_policy` sends
        only the rules this surface can actually meet: an alert raised inside the application's own
        process never interrupts an XCUITest interaction, so its rules are dropped (BE-0406).

        `governs` is true for any scenario whose `systemAlertHandling` is on, independent of whether
        any rule survived that drop — a real declaration filtered down to nothing this surface can
        act on is not the same as no declaration at all, and only the latter should keep the silent
        grant an absent monitor gives (BE-0406 Unit 2b).

        A rule's identifying labels are sent as a sorted list so the request is byte-stable across
        runs — the set is order-free, and a stable body keeps a replayed request comparable.
        """
        reply = self._transport(
            "POST",
            "/interruptionPolicy",
            {
                "rules": [{"identify": sorted(identify), "tap": tap} for identify, tap in rules],
                "governs": governs,
            },
        )
        if reply.status != _OK:
            # A runner that did not store the policy answers the next interrupting alert with
            # XCUITest's own default button, which is the silent grant this method exists to end —
            # and `_decode` turns a non-200 into a `status="error"` reply rather than raising, so a
            # runner build without this route (a stale `runner-build`, a mixed-version device) would
            # otherwise sail through and leave the store empty with no signal at all. Loud, like
            # every other write here.
            raise XcuitestChannelError(f"setting the interruption policy failed ({reply.status})")

    def drain_interruptions(self) -> base.DrainedInterruptions:
        """What the runner's interruption monitor tapped, declined and swiped away since the last drain.

        Whatever this driver's own `/tap` replies have already carried (`self._drain_carry`,
        BE-0407 Unit 6, accumulated by `_tracking_transport` in `__init__`) is never dropped, only
        ever reported and cleared here — the round trip below is skipped entirely exactly when the
        carry is still current (the most recent driver call was that very tap, so nothing could have
        interrupted since that the fold does not already cover); otherwise the wire is asked too, and
        the two are merged, since the carry may hold something from an earlier tap that a later query
        or retry made "no longer provably current" without making it any less real.
        """
        carry = self._drain_carry
        carried = carry.drained
        if carry.is_current:
            carry.is_current = False
            carry.drained = base.DrainedInterruptions.empty()
            return carried
        # Cleared only once the wire has answered *and* its fold parsed: a drain that raises must
        # leave the carry intact, or whatever it held (a genuinely tapped/declined label the fold
        # already captured) is gone from the driver with nothing left to recover it from — exactly
        # on the failure path where the eventual report needs it most.
        reply = self._transport("POST", "/interruptionPolicy/drain", {})
        fresh = _parse_drain_fold(reply.raw)
        carry.drained = base.DrainedInterruptions.empty()
        return carried.merged_with(fresh)

    def dismiss_blocking_tip(self, tree: list[base.Element] | None = None) -> bool:
        """Dismiss a showing TipKit tip via its dismiss region; False when no tip is up.

        A tip is recognized by the dismiss scrim *and* the tip's own container together, so an app's
        own popover — a `confirmationDialog` installs the identical scrim — is left alone.

        Args:
            tree: A snapshot the caller already holds, used only to rule a tip out. Absence is the
                overwhelmingly common case and this is asked on every wait poll, so answering it off
                the caller's tree keeps a guarded wait's query count unchanged. A tip found there is
                still re-queried before acting: a handle is only valid from the snapshot that minted
                it, and *this* driver's `_query_with_handles` is what mints one.

        Returns:
            Whether a tip was found and dismissed.

        Raises:
            AmbiguousSelector: Several nodes claimed the dismiss region — a shape TipKit should never
                produce, so it fails loudly rather than picking one (prime directive 2). The
                container is only ever tested for presence, so several of those are not an error.
        """
        sel: base.Selector = {"id": _TIPKIT_DISMISS_REGION}
        if tree is not None and not _tip_is_up(tree):
            return False
        elements, handles = self._query_with_handles(apply_native_z=False)
        # Re-checked against the fresh tree either way: with no hint this is the only check, and with
        # one the tip may have closed itself in between (a plain False, not an error).
        if not _tip_is_up(elements):
            return False
        el = base.resolve_unique(elements, sel)
        try:
            self._actuate("/tap", {"handle": handles[id(el)]}, sel, gesture="tap", element=el)
        except base.ElementNotFound:
            # The tip closed itself between that snapshot and this tap — TipKit dismisses on its own
            # rules, so the window is a live race rather than a defect. "The tip is gone" is what a
            # caller asked about, so it reads as no dismissal, not as an error: raising here would
            # surface `PopoverDismissRegion` — an identifier no author wrote — as a wait's failure
            # reason, and would overwrite the real reason on the post-failure path. An
            # `AmbiguousSelector` from `resolve_unique` above still propagates.
            return False
        return True

    def back(self) -> None:
        # iOS has no hardware back: tap the OS navigation back button. Reuses `tap` rather than
        # re-issuing the actuate call (BE-0210).
        self.tap({"id": base.OS_BACK_BUTTON})

    def type_text(self, text: str) -> None:
        # `text` is deliberately absent from the record — not even its length (see `actuation.py`).
        self._actuations.record(Actuation(gesture="typeText", via="focused", unit=_UNIT))
        reply = self._transport("POST", "/type", {"text": text})
        if reply.status != _OK:
            raise XcuitestChannelError(f"type failed ({reply.status})")

    def delete_text(self, count: int) -> None:
        # A run of backspaces on the focused field (BE-0265); XCUIElement types the delete key natively.
        self._actuations.record(Actuation(gesture="deleteText", via="focused", unit=_UNIT))
        reply = self._transport("POST", "/deleteText", {"count": count})
        if reply.status != _OK:
            raise XcuitestChannelError(f"deleteText failed ({reply.status})")

    def select_all(self) -> None:
        self._actuations.record(Actuation(gesture="selectAll", via="focused", unit=_UNIT))
        reply = self._transport("POST", "/selectAll", {})
        if reply.status != _OK:
            raise XcuitestChannelError(f"selectAll failed ({reply.status})")

    def copy_selection(self) -> None:
        self._actuations.record(Actuation(gesture="copy", via="focused", unit=_UNIT))
        reply = self._transport("POST", "/copy", {})
        if reply.status != _OK:
            raise XcuitestChannelError(f"copy failed ({reply.status})")

    def wait_for(self, sel: base.Selector) -> bool:
        """Single-shot: whether `sel` matches the current screen (BE-0118).

        Delegates to the shared `base.default_wait_for` so the four backends share one body; the
        deadline poll lives in `base.wait_until`, so the timeout is honoured identically (BE-0251).
        """
        return base.default_wait_for(self, sel)

    def screenshot(self, path: str) -> None:
        reply = self._transport("GET", "/screenshot", None)
        if reply.status != _OK or reply.png is None:
            # Fail loudly rather than writing an empty / non-PNG artifact on a runner error.
            raise XcuitestChannelError(f"screenshot failed (status={reply.status})")
        Path(path).write_bytes(reply.png)

    def capabilities(self) -> set[str]:
        return set(self.CAPABILITIES)

    # --- lifecycle ---

    def await_ready(self, timeout: float = 10.0, poll: float = 0.1) -> None:
        """Block until the runner's loopback server answers `GET /health` with `ready`.

        A bounded condition wait: it polls `/health` (no fixed sleep that ignores the condition) and
        fails loudly (`XcuitestChannelError`) on timeout rather than hanging, so "the runner never
        came up" is a clear run failure.
        """
        if _await_health(self._transport, timeout=timeout, poll=poll) is not _HealthWait.READY:
            raise XcuitestChannelError(
                f"xcuitest runner did not come up within {timeout}s (health never ready)"
            )

    def health_ready(self) -> bool:
        """One `GET /health` probe: `True` if the runner answers `ready`, `False` if not up yet (BE-0319).

        A single non-blocking check (a zero-budget `_await_health`: it probes once and returns),
        unlike `await_ready`'s bounded poll loop. Uses `_probe_transport` — the raw, single-attempt
        transport, the same one the crash-recovery health poll uses — rather than `_transport`, whose
        BE-0207 retry would silently turn a "single probe" into up to `_MAX_ATTEMPTS` attempts with
        backoff (over a second) each call; the cold-spawn liveness wait that watches the `xcodebuild`
        process between probes needs each one fast, since it owns its own loop and timing. Reuses the
        driver's one definition of the health-wire contract — the endpoint, the `ready` sentinel, and
        which transport errors read as not-ready — rather than restating it.
        """
        return _await_health(self._probe_transport, timeout=0.0) is _HealthWait.READY
