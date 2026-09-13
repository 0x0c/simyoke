"""Panel data for the report tabs.

Result, Network, Device Log, App Trace, plus the per-scenario assembly that ties the rows and
panels together.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bajutsu.common.evidence import Artifact
from bajutsu.common.orchestrator import RunResult
from bajutsu.common.report.format import (
    _LOG_MAX_LINES,
    _artifact,
    _fmt_duration,
    _read_json,
    _read_lines,
    _status_class,
    _truncate,
    video_seconds,
)
from bajutsu.common.report.rows import (
    _after_rows,
    _expects_data,
    _merged_rows,
    _phase_rows,
    _preconditions_rows,
)

# --- panel data (Result / Network / Device Log / App Trace) ---


def _exchange_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _domain_allowed(host: str, domains: list[str]) -> bool:
    """No filter -> every exchange; otherwise the host must equal a listed domain or be a subdomain.

    `api.example.com` is allowed by `example.com`.
    """
    if not domains:
        return True
    host = host.lower()
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def _video_skip_reason(r: RunResult) -> str | None:
    """Why no video artifact reached this result, when the run itself disclosed one (BE-0020).

    Distinguishes a scenario a backend simply never records video for (no disclosure — the media
    area's ordinary "no recording") from one where a recording was expected but lost, e.g. to a
    mid-run backend crash — so the report says why instead of looking like a plain miss.
    """
    return next((sc.reason for sc in r.skipped_captures if sc.kind == "video"), None)


def _result_panel(
    r: RunResult,
    definition: dict[str, Any] | None,
    source: str | None,
    exchanges: list[dict[str, Any]],
    run_dir: Path | None,
    step_lines: list[int] | None,
) -> dict[str, Any]:
    plan = (definition or {}).get("steps") or []
    return {
        "kind": "result",
        "key": "steps",
        "label": "Result",
        "source": source,
        "preconditions": _preconditions_rows(definition),
        # The lifecycle phases render as their own blocks (BE-0392), never folded into the numbered
        # steps: a reviewer can tell setup and teardown from the scenario under test at a glance,
        # which a `preconditions.setup` prelude spliced into `steps` gives no way to do.
        "beforerows": _phase_rows(
            r.before_outcomes, (definition or {}).get("before") or [], r.video_anchor_s, run_dir
        ),
        "steprows": _merged_rows(r, plan, exchanges, run_dir, step_lines),
        "afterrows": _after_rows(r, (definition or {}).get("after") or [], run_dir),
        "expects": _expects_data(r, definition),
    }


def _environment_panel(r: RunResult) -> dict[str, Any]:
    """The simulator the scenario ran on — device model / OS / actuator / udid — shown beside Result.

    Unknown fields (e.g. the fake driver names no device) are omitted.
    """
    sim: list[tuple[str, str]] = []
    if r.device_name:
        sim.append(("device", r.device_name))
    if r.device_runtime:
        sim.append(("OS", r.device_runtime))
    if r.backend:
        sim.append(("actuator", r.backend))
    if r.device:
        sim.append(("udid", r.device))
    skips = [{"kind": sc.kind, "reason": sc.reason} for sc in r.skipped_captures]
    return {"kind": "env", "key": "env", "label": "Environment", "sim": sim, "skips": skips}


def _network_item(d: dict[str, Any], video_anchor_s: float) -> dict[str, Any]:
    """One exchange's panel entry.

    `video_anchor_s` turns the exchange's absolute `startedAt` into the recording-relative `at`
    label the Result timeline also shows (BE-0348).
    """
    method = str(d.get("method") or "")
    status = d.get("status")
    target = str(d.get("path") or d.get("url") or "")
    dur = d.get("durationMs")
    started = d.get("startedAt")
    sections: list[dict[str, Any]] = []
    url = str(d.get("url") or "")
    if url and url != target:
        sections.append({"kind": "line", "label": "url", "text": url, "cls": ""})
    rh = d.get("requestHeaders")
    if isinstance(rh, dict) and rh:
        sections.append({"kind": "kv", "label": "request headers", "pairs": list(rh.items())})
    rb = d.get("requestBody")
    if isinstance(rb, str) and rb:
        sections.append({"kind": "pre", "label": "request body", "text": _truncate(rb)})
    sh = d.get("responseHeaders")
    if isinstance(sh, dict) and sh:
        sections.append({"kind": "kv", "label": "response headers", "pairs": list(sh.items())})
    sb = d.get("responseBody")
    if isinstance(sb, str) and sb:
        sections.append({"kind": "pre", "label": "response body", "text": _truncate(sb)})
    err = d.get("error")
    if err:
        sections.append({"kind": "line", "label": "error", "text": str(err), "cls": "err"})
    return {
        "method": method,
        "target": target,
        "at": f"{video_seconds(float(started), video_anchor_s=video_anchor_s):.1f}s"
        if isinstance(started, (int, float)) and not isinstance(started, bool)
        else "",
        "status": str(status) if status is not None else "—",
        "status_cls": _status_class(status),
        "dur": f"{float(dur):.0f} ms"
        if isinstance(dur, (int, float)) and not isinstance(dur, bool)
        else "",
        "mocked": bool(d.get("mocked")),
        "sections": sections,
    }


def _network_panel(art: Artifact, data: Any, video_anchor_s: float) -> dict[str, Any]:
    # `data` is the already-parsed network.json (or None) — read once in `_scenario_data` and
    # shared with the result timeline, so a body-carrying network.json isn't parsed twice.
    if not isinstance(data, list) or not data:
        return {
            "kind": "network",
            "key": "net",
            "label": "Network",
            "empty": True,
            "link": art.name,
        }
    items = [_network_item(d, video_anchor_s) for d in data if isinstance(d, dict)]
    return {
        "kind": "network",
        "key": "net",
        "label": "Network",
        "empty": False,
        "link": art.name,
        "count": len(items),
        "plural": "exchange" if len(items) == 1 else "exchanges",
        "exchanges": items,
    }


def _log_panel(run_dir: Path | None, art: Artifact) -> dict[str, Any]:
    lines, total = _read_lines(run_dir, art.name, _LOG_MAX_LINES) if run_dir else (None, 0)
    if lines is None:
        return {"kind": "log", "key": "log", "label": "Device Log", "link": art.name, "lines": None}
    shown = len(lines)
    note = f"showing last {shown} of {total} lines · " if total > shown else ""
    return {
        "kind": "log",
        "key": "log",
        "label": "Device Log",
        "link": art.name,
        "lines": lines,
        "shown": shown,
        "note": note,
    }


def _trace_panel(run_dir: Path | None, art: Artifact) -> dict[str, Any]:
    data = _read_json(run_dir, art.name) if run_dir else None
    if not isinstance(data, list) or not data:
        return {
            "kind": "trace",
            "key": "trace",
            "label": "App Trace",
            "link": art.name,
            "empty": True,
        }
    rows = [
        (
            str(d.get("name", "")),
            str(d.get("durationMs", "")),
            str(d.get("begin", "")),
            str(d.get("end", "")),
        )
        for d in data
        if isinstance(d, dict)
    ]
    return {
        "kind": "trace",
        "key": "trace",
        "label": "App Trace",
        "link": art.name,
        "empty": False,
        "rows": rows,
    }


def _scenario_data(
    r: RunResult,
    run_dir: Path | None,
    definition: dict[str, Any] | None,
    source: str | None,
    source_file: str | None = None,
    step_lines: list[int] | None = None,
) -> dict[str, Any]:
    video = _artifact(r, "video")
    net = _artifact(r, "network")
    net_data = _read_json(run_dir, net.name) if (net is not None and run_dir is not None) else None
    all_exchanges = (
        [d for d in net_data if isinstance(d, dict)] if isinstance(net_data, list) else []
    )
    net_filter = ((definition or {}).get("network") or {}).get("filter") or {}
    domains = net_filter.get("domains") or []
    step_exchanges = [
        d
        for d in all_exchanges
        if _domain_allowed(_exchange_host(str(d.get("url") or "")), domains)
    ]
    panels: list[dict[str, Any]] = [
        _result_panel(r, definition, source, step_exchanges, run_dir, step_lines),
        _environment_panel(r),
    ]
    if net is not None:
        panels.append(_network_panel(net, net_data, r.video_anchor_s))
    dev = _artifact(r, "deviceLog")
    if dev is not None:
        panels.append(_log_panel(run_dir, dev))
    trace = _artifact(r, "appTrace")
    if trace is not None:
        panels.append(_trace_panel(run_dir, trace))
    return {
        "name": r.scenario,
        "ok": r.ok,
        "backend": r.backend,
        "device": r.device,
        "open": not r.ok,
        "description": (definition or {}).get("description"),
        "source_file": source_file,
        "duration": _fmt_duration(r.duration_s),
        "video": video.name if video else None,
        "video_note": None if video else _video_skip_reason(r),
        "panels": panels,
    }
