"""Row/detail data for the merged Result table (steps, network exchanges, expectations)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bajutsu.common.drivers.actuation import Actuation
from bajutsu.common.evidence import step_view
from bajutsu.common.orchestrator import AlertEvent, RunResult
from bajutsu.common.report.format import (
    _ACTION_META,
    Part,
    _as_float,
    _gnum,
    _read_json,
    _status_class,
    _truncate,
    video_seconds,
)
from bajutsu.common.report.from_grouping import grouped_provenance
from bajutsu.common.report.richtext import (
    _assert_parts,
    _step_desc_parts,
)

# --- detail / row data (the merged Result table) ---


def _step_detail(step_def: dict[str, Any] | None, from_: str | None = None) -> dict[str, Any]:
    """The 'detail' cell content for a planned step.

    Tokenized parts (or a nested assert table), plus the optional step name, capture tags, and
    `from:` provenance.

    `from_` is the already-grouped provenance to show (None when this step continues a run of the
    same phrase), not the step's raw `from:` — the caller dedupes consecutive equal values.
    """
    empty: dict[str, Any] = {"kind": "parts", "parts": [], "name": None, "caps": [], "from_": None}
    if step_def is None:
        return empty
    action = next((k for k in _ACTION_META if k in step_def), None)
    if action is None:
        return empty
    name = step_def.get("name")
    caps = step_def.get("capture") or []
    if action == "assert":
        return {
            "kind": "asserts",
            "rows": [_assert_parts(a) for a in step_def["assert"]],
            "name": name,
            "caps": caps,
            "from_": from_,
        }
    return {
        "kind": "parts",
        "parts": _step_desc_parts(action, step_def[action]),
        "name": name,
        "caps": caps,
        "from_": from_,
    }


def _action_data(step_def: dict[str, Any] | None, out_action: str | None) -> dict[str, str] | None:
    if step_def is not None:
        action = next((k for k in _ACTION_META if k in step_def), None)
        if action is not None:
            label, cls = _ACTION_META[action]
            return {"label": label, "cls": cls}
    if out_action:
        return {"label": out_action, "cls": ""}
    return None


def _tree_row(e: dict[str, Any]) -> dict[str, Any]:
    """One captured element rendered as a row for the in-report element viewer.

    `rect` carries the raw frame (points) so the viewer can highlight it on the screenshot.
    """
    frame = e.get("frame")
    fr = ""
    rect: dict[str, str] | None = None
    if isinstance(frame, (list, tuple)) and len(frame) == 4:
        x, y, w, h = frame
        fr = f"{_gnum(x)}, {_gnum(y)} · {_gnum(w)}×{_gnum(h)}"
        rect = {"x": _gnum(x), "y": _gnum(y), "w": _gnum(w), "h": _gnum(h)}
    val = e.get("value")
    return {
        "id": e.get("identifier") or "",
        "label": e.get("label") or "",
        "value": "" if val is None else str(val),
        "traits": " ".join(e.get("traits") or []),
        "frame": fr,
        "rect": rect,
    }


def _screen_rect(elements: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """The screen extent in points — the bounding box of every element frame.

    The element viewer maps a hovered frame onto the (full-screen) screenshot as a percentage of
    this, so it needs no device scale. The JS refines the height from the screenshot's true pixel
    size, so a long scrolling list does not distort the mapping.
    """
    w = h = 0.0
    for e in elements:
        fr = e.get("frame")
        if isinstance(fr, (list, tuple)) and len(fr) == 4:
            x, y, fw, fh = (_as_float(v) for v in fr)
            w, h = max(w, x + fw), max(h, y + fh)
    if w <= 0 or h <= 0:
        return None, None
    return _gnum(w), _gnum(h)


def _view_data(out: Any, run_dir: Path | None) -> dict[str, Any]:
    # One screenshot and one tree per step, resolved by `step_view` — the same choice the serve
    # editor's picker and the triage context make, so no consumer disagrees about which screen a
    # step "is". The candidates are filtered to the files that are actually there: a report
    # re-rendered from a stored run (`serve/artifacts.py`) can name a screenshot the store no longer
    # holds, and choosing it would emit a broken `<img>` and an element viewer with nothing to draw
    # frames on, while the `before.png` beside it sits unused.
    view = step_view(
        ((a.kind, a.name, a.depicts) for a in out.artifacts),
        exists=None if run_dir is None else lambda name: (run_dir / name).exists(),
    )
    # Embed the captured elements inline so the report shows them in an overlay (no
    # new tab), matching how logs/network are embedded for offline (file://) viewing.
    tree_rows: list[dict[str, Any]] | None = None
    screen_w = screen_h = None
    if view.elements is not None and run_dir is not None:
        data = _read_json(run_dir, view.elements)
        if isinstance(data, list):
            els = [e for e in data if isinstance(e, dict)]
            tree_rows = [_tree_row(e) for e in els]
            # The extent goes out only for a pair that describes one screen. It is the whole input
            # to the viewer's frame mapping, so withholding it is all `report.js` needs to draw no
            # frame at all (`tvHighlight` hides the box unless the extent is positive) — the honest
            # rendering when the frames would land on pixels they never described.
            if view.paired:
                screen_w, screen_h = _screen_rect(els)
    return {
        "shot": view.screenshot,
        "tree": view.elements,
        "tree_rows": tree_rows,
        "tree_count": len(tree_rows) if tree_rows is not None else 0,
        "screen_w": screen_w,
        "screen_h": screen_h,
        "unpaired": not view.paired,
        "alt": f"step {out.index} result",
    }


def _step_run_row(
    i: int,
    step_def: dict[str, Any] | None,
    out: Any,
    run_dir: Path | None,
    at: float,
    from_: str | None = None,
    line: int | None = None,
) -> dict[str, Any]:
    """One executed step's row. `at` is its already-derived seconds into the recording (BE-0348).

    `line` is the step's original 1-based line number in the scenario file, when the caller could
    recover one (`html.scenario_source_meta`) — None wherever it could not.
    """
    at_text = f"{at:.1f}s"
    # The step's own end instant — its `before`/`after` moment in the recording, not the
    # scenario-level `before`/`after` phases this table sits beside. A second jump target only
    # when it reads differently from the start: a near-instant tap would otherwise show two
    # identical-looking buttons. Computed once: the button's visible text and its seek target
    # must agree, and a second copy of this arithmetic could drift from this one silently.
    end_s = at + max(0.0, out.duration_s)
    end_text = f"{end_s:.1f}s"
    # The step's own elapsed time — how long the action itself took, shown at the end of the `at`
    # cell so a slow step (a long `wait`, a sluggish gesture) is visible without opening the
    # recording. Gated the same way as `at_end`: a near-instant step would otherwise show a noisy
    # "(0.0s)" on every row.
    elapsed = f"{end_s - at:.1f}s" if end_text != at_text else None
    return {
        "rowcls": f"srow {'ok' if out.ok else 'ng'}",
        "data_t": f"{at:.3f}",
        "data_t_end": f"{end_s:.3f}" if end_text != at_text else None,
        "title": f"jump to {at:.1f}s in the recording",
        "num": str(i),
        "numcls": None,
        "line": line,
        "result": {"cls": "ok" if out.ok else "ng", "text": "PASS" if out.ok else "FAIL"},
        "action": _action_data(step_def, out.action),
        "detail": _step_detail(step_def, from_),
        "at": at_text,
        "elapsed": elapsed,
        "at_end": end_text if end_text != at_text else None,
        "view": _view_data(out, run_dir),
        "reason": out.reason if (not out.ok and out.reason) else None,
        "expand": None,
        "alerts": _alert_rows(out.alerts),
        "actuations": _actuation_rows(out.actuations),
        "dropped_actuations": out.dropped_actuations,
        "generated": out.generated,
    }


def _alert_rows(alerts: list[AlertEvent]) -> list[dict[str, Any]]:
    """One display row per dismissed prompt, including its kind (an alert vs. a notification banner)."""
    return [{"label": a.label, "kind": a.kind} for a in alerts]


def _actuation_rows(actuations: list[Actuation]) -> list[dict[str, Any]]:
    """One display row per actuation: the gesture, its geometry, and the channel that carried it.

    A record with no coordinate (a handle-based iOS tap, an Android device-side gesture) shows its
    resolved frame instead, so a reader always sees *where* — and `via` says whether the number is the
    point that was sent or the bounds the far side resolved from.
    """
    rows = []
    for a in actuations:
        points = " → ".join(f"({_gnum(x)}, {_gnum(y)})" for x, y in a.points)
        frame = (
            f"[{_gnum(a.frame[0])}, {_gnum(a.frame[1])} {_gnum(a.frame[2])}×{_gnum(a.frame[3])}]"
            if a.frame is not None
            else ""
        )
        params = [
            f"{name} {_gnum(v)}"
            for name, v in (("for", a.duration_s), ("scale", a.scale), ("rad", a.radians))
            if v is not None
        ]
        rows.append(
            {
                "gesture": a.gesture,
                "via": a.via,
                # Shown for a frame too, not only a point: a frame is in a coordinate space as much as
                # a coordinate is, and on a WebView record that space is the WebView's own — the
                # difference between a comparable number and a misleading one.
                "unit": a.unit if (a.points or a.frame) else "",
                "points": points,
                "frame": frame,
                "target": a.target or "",
                "params": " · ".join(params),
                # A refused attempt (a stale handle, a declined device-side gesture) must not read as
                # one that landed; `None` means the channel gave no separate answer.
                "refused": a.accepted is False,
                # Why this element rather than the one the selector named. Empty on the ordinary
                # path, so a reader sees the token only where a driver really substituted.
                "substitution": a.substitution or "",
            }
        )
    return rows


def _step_skip_row(
    i: int, step_def: dict[str, Any] | None, from_: str | None = None, line: int | None = None
) -> dict[str, Any]:
    return {
        "rowcls": "skip",
        "data_t": None,
        "data_t_end": None,
        "title": None,
        "num": str(i),
        "numcls": None,
        "line": line,
        "result": {"cls": "", "text": "—"},
        "action": _action_data(step_def, None),
        "detail": _step_detail(step_def, from_),
        "at": "",
        "elapsed": None,
        "at_end": None,
        "view": None,
        "reason": None,
        "expand": None,
    }


def _nx_pairs(d: dict[str, Any], fields: list[tuple[str, str]]) -> list[tuple[str, dict[str, Any]]]:
    """Build (label, value) pairs for an exchange's collapsible settings table.

    Built from a list of (label, key) — tokens for scalars, header lists, and body blocks.
    """
    pairs: list[tuple[str, dict[str, Any]]] = []
    for label, key in fields:
        v = d.get(key)
        if key == "method":
            pairs.append((label, {"kind": "parts", "parts": [("kw", str(v or "req"))]}))
        elif key == "endpoint":
            ep = str(d.get("url") or d.get("path") or "")
            if ep:
                pairs.append((label, {"kind": "parts", "parts": [("str", ep)]}))
        elif key == "status" and v is not None:
            pairs.append((label, {"kind": "parts", "parts": [("num", str(v))]}))
        elif key == "durationMs" and isinstance(v, (int, float)) and not isinstance(v, bool):
            pairs.append((label, {"kind": "parts", "parts": [("num", f"{v:.0f} ms")]}))
        elif key in ("requestHeaders", "responseHeaders") and isinstance(v, dict) and v:
            pairs.append((label, {"kind": "headers", "pairs": list(v.items())}))
        elif key in ("requestBody", "responseBody") and isinstance(v, str) and v:
            pairs.append((label, {"kind": "body", "text": _truncate(v)}))
    return pairs


def _exchange_summary(d: dict[str, Any], fallback: str) -> list[Part]:
    endpoint = str(d.get("url") or d.get("path") or "")
    return [("str", endpoint)] if endpoint else [("kw", fallback)]


def _request_row(d: dict[str, Any], at: float) -> dict[str, Any]:
    """A request row.

    Its detail cell is just the endpoint (a click target); the full settings table renders in a
    separate full-width row below (so it gets the whole width instead of the cramped detail column).
    """
    method = str(d.get("method") or "req")
    pairs = _nx_pairs(
        d,
        [
            ("method", "method"),
            ("endpoint", "endpoint"),
            ("headers", "requestHeaders"),
            ("body", "requestBody"),
        ],
    )
    return {
        "rowcls": "nrow xrow",
        "data_t": None,
        "title": None,
        "num": "→",
        "numcls": "nix",
        "result": None,
        "action": {"label": method, "cls": "act-net"},
        "detail": {
            "kind": "nxsummary",
            "summary": _exchange_summary(d, method),
            "name": None,
            "caps": [],
        },
        "expand": {"pairs": pairs},
        "at": f"{at:.1f}s",
        "view": None,
        "reason": None,
    }


def _response_row(d: dict[str, Any], at: float) -> dict[str, Any]:
    status = d.get("status")
    pairs = _nx_pairs(
        d,
        [
            ("status", "status"),
            ("duration", "durationMs"),
            ("headers", "responseHeaders"),
            ("body", "responseBody"),
        ],
    )
    return {
        "rowcls": "nrow xrow",
        "data_t": None,
        "title": None,
        "num": "←",
        "numcls": "nix",
        "result": {
            "cls": _status_class(status),
            "text": str(status) if status is not None else "—",
        },
        "action": {"label": "response", "cls": "act-net"},
        "detail": {
            "kind": "nxsummary",
            "summary": _exchange_summary(d, "response"),
            "name": None,
            "caps": [],
        },
        "expand": {"pairs": pairs},
        "at": f"{at:.1f}s",
        "view": None,
        "reason": None,
    }


def _merged_rows(
    r: RunResult,
    plan: list[dict[str, Any]],
    exchanges: list[dict[str, Any]],
    run_dir: Path | None,
    step_lines: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Step rows plus the observed exchanges (split request/response) interleaved by time offset.

    Steps and exchanges both record absolute wall-clock instants, so the recording-relative seconds
    the timeline sorts and displays on are derived here, once, against `r.video_anchor_s` (BE-0348).

    `step_lines[i]`, when given, is step *i*'s original line number in the scenario file; it is
    looked up positionally, same as `plan[i]`.

    Not-run steps trail at the end in plan order.
    """
    by_index = {s.index: s for s in r.steps}
    total = max(len(plan), len(r.steps))
    # Provenance to display per step, grouped in plan order so a run of identical consecutive
    # `from:` is labeled once (BE-0044); each step keeps its own value regardless of time sorting.
    shown_from = grouped_provenance(
        [(plan[i].get("from") if i < len(plan) else None) for i in range(total)]
    )

    def line(i: int) -> int | None:
        return step_lines[i] if step_lines and i < len(step_lines) else None

    timed: list[tuple[float, int, dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []
    for i in range(total):
        step_def = plan[i] if i < len(plan) else None
        out = by_index.get(i)
        if out is None:
            skipped.append(_step_skip_row(i, step_def, shown_from[i], line(i)))
        else:
            at = video_seconds(out.started_at, video_anchor_s=r.video_anchor_s)
            timed.append(
                (at, 0, _step_run_row(i, step_def, out, run_dir, at, shown_from[i], line(i)))
            )
    for d in exchanges:
        t0 = video_seconds(_as_float(d.get("startedAt")), video_anchor_s=r.video_anchor_s)
        dur_s = _as_float(d.get("durationMs")) / 1000.0
        timed.append((t0, 1, _request_row(d, t0)))
        timed.append((t0 + dur_s, 2, _response_row(d, t0 + dur_s)))
    timed.sort(key=lambda x: (x[0], x[1]))
    return [row for _, _, row in timed] + skipped


def _phase_rows(
    outcomes: list[Any],
    plan: list[dict[str, Any]],
    video_anchor_s: float,
    run_dir: Path | None,
) -> list[dict[str, Any]]:
    """A `before` / `after` phase's step rows (BE-0392), in the order the phase ran them.

    A plain list, not `_merged_rows`: the network exchanges are interleaved into the scenario's own
    timeline once, and repeating them beside setup and teardown would double-count them. Rows pair
    with their definition by the outcome's own `index`, the same way `_merged_rows` does. That
    inherits `_merged_rows`' limit as well: a container step (`if` / `forEach` / `web`) numbers its
    nested steps from the same counter, so the rows after it pair with the wrong definition and the
    container's own successors render with none. Not-run steps trail in plan order, the same
    disclosure `_merged_rows` gives: a phase that stopped partway through must show which steps never
    started rather than an unexplained short list.
    """
    shown_from = grouped_provenance([d.get("from") for d in plan])
    by_index = {out.index: out for out in outcomes}
    rows: list[dict[str, Any]] = []
    for i in range(max(len(plan), len(outcomes))):
        step_def = plan[i] if i < len(plan) else None
        from_ = shown_from[i] if i < len(shown_from) else None
        out = by_index.get(i)
        if out is None:
            rows.append(_step_skip_row(i, step_def, from_))
            continue
        at = video_seconds(out.started_at, video_anchor_s=video_anchor_s)
        rows.append(_step_run_row(i, step_def, out, run_dir, at, from_))
    return rows


def _after_rows(
    r: RunResult,
    rules: list[dict[str, Any]],
    run_dir: Path | None,
) -> list[dict[str, Any]]:
    """The `after` phase's step rows (BE-0392), each lined up with the rule that declared it.

    Only the rules the run dispatched contribute — `r.after_verdict` says which, since the failure
    string can no longer say once a cleanup step's own reason has been folded into it. Within a
    dispatched rule the outcomes are consumed in order, and the phase stops a rule at its first
    failing step, so the rest of that rule's steps (and every rule left when a cancelled run's
    teardown budget ran out) show as not-run rather than silently vanishing.

    One shape this ordering cannot resolve: a container step (`if` / `forEach` / `web`) inside a rule
    records its nested steps' outcomes in the same list, so the rows after it in that rule pair with
    the wrong definition. Neither `_phase_rows` nor `_merged_rows` resolves it either — they key on
    the outcome's own `index`, which a container's nested steps also consume — so every step table
    shares the shift rather than this one alone. The verdict, the JUnit body, and the CTRF record are
    unaffected; only the attribution in these blocks is.
    """
    rows: list[dict[str, Any]] = []
    cursor = 0
    for rule in rules:
        on = str(rule.get("on") or "")
        if on != "always" and on != r.after_verdict:
            continue
        steps = rule.get("steps") or []
        shown_from = grouped_provenance([d.get("from") for d in steps])
        stopped = False
        for i, step_def in enumerate(steps):
            # The `#` cell carries the rule's outcome word, so a reader can tell teardown that ran
            # unconditionally from teardown this run's verdict selected without a second table. An
            # executed step is numbered by its own outcome index — the phase numbers its steps once
            # across every rule — so the HTML, the JUnit body, and the evidence directory name the
            # same step. A step that never ran has no such number, and says so.
            if stopped or cursor >= len(r.after_outcomes):
                row = _step_skip_row(i, step_def, shown_from[i])
                row["num"] = f"{on}·—"
            else:
                out = r.after_outcomes[cursor]
                cursor += 1
                at = video_seconds(out.started_at, video_anchor_s=r.video_anchor_s)
                row = _step_run_row(i, step_def, out, run_dir, at, shown_from[i])
                row["num"] = f"{on}·{out.index}"
                stopped = not out.ok
            rows.append(row)
    return rows


def _preconditions_rows(definition: dict[str, Any] | None) -> list[tuple[str, str]]:
    pre = (definition or {}).get("preconditions") or {}
    rows: list[tuple[str, str]] = []
    if "erase" in pre:
        rows.append(("erase", "true" if pre["erase"] else "false"))
    if pre.get("deeplink"):
        rows.append(("deeplink", str(pre["deeplink"])))
    if pre.get("locale"):
        rows.append(("locale", str(pre["locale"])))
    if pre.get("setup"):
        rows.append(("setup", str(pre["setup"])))
    rows += [(str(k), str(v)) for k, v in (pre.get("launchEnv") or {}).items()]
    if pre.get("launchArgs"):
        rows.append(("launchArgs", " ".join(pre["launchArgs"])))
    return rows


def _visual_row(ev: Any, ok: bool) -> dict[str, Any] | None:
    """The baseline/actual/diff image strip for a `visual` expectation.

    `ev` is the AssertionResult.visual evidence (run-dir-relative image paths). The Approve button
    (functional only under `serve`) is offered whenever the comparison did not pass.
    """
    if ev is None:
        return None
    sid = ev.actual.rsplit("/", 1)[0] if "/" in ev.actual else ""
    return {
        "baseline": ev.baseline,
        "actual": ev.actual,
        "diff": ev.diff,
        "diff_pct": f"{ev.diff_pct:.2f}%" if ev.diff_pct is not None else None,
        "missing": ev.missing,
        "approvable": not ok,
        "baseline_name": ev.baseline_name,
        "sid": sid,
        "engine": ev.engine,
        "element_scoped": ev.element_scoped,  # comparison cropped to one element (BE-0171)
        "masked_selectors": ev.masked_selectors,  # selectors that masked a region (BE-0171)
    }


def _expects_data(r: RunResult, definition: dict[str, Any] | None) -> dict[str, Any] | None:
    planned = (definition or {}).get("expect") or []
    if r.expect_results:
        rows: list[dict[str, Any]] = []
        for i, a in enumerate(r.expect_results):
            if i < len(planned):
                kind, target, comp = _assert_parts(planned[i])
            else:
                kind, target, comp = a.kind, [("", a.detail)], []
            cls = "ok" if a.ok else "ng"
            rows.append(
                {
                    "rowcls": cls,
                    "stcls": cls,
                    "status": "PASS" if a.ok else "FAIL",
                    "kind": kind,
                    "target": target,
                    "comp": comp,
                    "reason": a.reason if not a.ok else None,
                    "visual": _visual_row(a.visual, a.ok),
                }
            )
        return {
            "label": "expectations",
            "rows": rows,
            "alerts": _alert_rows(r.expect_alerts),
            "actuations": _actuation_rows(r.expect_actuations),
            "dropped_expect_actuations": r.dropped_expect_actuations,
        }
    if not planned:
        return None
    rows = []
    for a in planned:
        kind, target, comp = _assert_parts(a)
        rows.append(
            {
                "rowcls": "skip",
                "stcls": "",
                "status": "—",
                "kind": kind,
                "target": target,
                "comp": comp,
                "reason": None,
            }
        )
    return {"label": "expectations (not evaluated)", "rows": rows}
