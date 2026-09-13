"""Common Test Report Format (CTRF) export (BE-0161).

A pure, deterministic projection of the run's result model into a CTRF document
(https://ctrf.io/) — the same input as `junit_xml()`, a richer output beside it. CTRF is an
open-standard JSON schema (`reportFormat: "CTRF"`, a `results` object of `tool` / `summary` /
`tests`) that a growing ecosystem — PR-comment reporters, dashboards, flaky-test analytics —
reads without per-tool adapters, so Bajutsu's structured extras (steps, engine, device,
attachments) survive the trip in a way JUnit XML can't carry.

Being a post-verdict serialization of already-decided results, it sits outside the
determinism-first contract by construction: no LLM, and writing it can't move the verdict.
"""

from __future__ import annotations

from datetime import datetime

from bajutsu import __version__
from bajutsu.common.orchestrator import RunResult, StepOutcome
from bajutsu.common.report.manifest import _details, _matrix
from bajutsu.common.run_meta.id import parse_run_id_timestamp

# The CTRF spec version this projection targets; the vendored test schema is pinned to it.
SPEC_VERSION = "0.0.0"

# Artifact `kind` → MIME content type. Unknown kinds fall back to a safe octet-stream, so a new
# evidence kind still exports (as an opaque attachment) rather than breaking the document.
_ARTIFACT_MIME = {
    "video": "video/mp4",
    "screenshot": "image/png",
    "deviceLog": "text/plain",
    "elements": "application/json",
    "network": "application/json",
    "appTrace": "application/json",
}
_DEFAULT_MIME = "application/octet-stream"


def _content_type(kind: str) -> str:
    return _ARTIFACT_MIME.get(kind, _DEFAULT_MIME)


def _run_started(run_id: str) -> datetime | None:
    """The run's start parsed from the runId, or None if it isn't a timestamp.

    Delegates to the shared run-id contract (BE-0200) so the format lives in one place. None
    (e.g. a test runId) leaves the derived `start`/`stop`/`timestamp` at their fallbacks rather
    than fabricating a value.
    """
    return parse_run_id_timestamp(run_id)


def _ms(seconds: float) -> int:
    """Seconds → whole milliseconds (CTRF's duration unit)."""
    return int(seconds * 1000)


def _status(ok: bool) -> str:
    """The only two states Bajutsu emits; CTRF's other states stay at zero."""
    return "passed" if ok else "failed"


def _test_name(r: RunResult) -> str:
    """The scenario name, suffixed with the engine on a cross-browser cell (BE-0076)."""
    return f"{r.scenario} [{r.engine}]" if r.engine else r.scenario


def _device(r: RunResult) -> str:
    """The model + runtime label for the CTRF `device` field, e.g. `iPhone 15 (iOS 17.2)`."""
    if r.device_name and r.device_runtime:
        return f"{r.device_name} ({r.device_runtime})"
    return r.device_name or r.device_runtime


def _step(s: StepOutcome) -> dict[str, object]:
    """One CTRF step — name/status only at the top level, everything richer into `extra`.

    A CTRF step allows just `{name, status, extra}`, so Bajutsu's duration / reason / per-step
    assertions / artifacts (all richer than the schema's top level) are preserved under `extra`.
    """
    extra: dict[str, object] = {"index": s.index, "duration": _ms(s.duration_s)}
    if s.reason:
        extra["reason"] = s.reason
    if s.assertion_results:
        extra["assertions"] = [
            {"ok": a.ok, "kind": a.kind, "detail": a.detail, "reason": a.reason}
            for a in s.assertion_results
        ]
    if s.artifacts:
        extra["artifacts"] = [{"name": a.name, "kind": a.kind} for a in s.artifacts]
    return {"name": s.action, "status": _status(s.ok), "extra": extra}


def _attachments(r: RunResult) -> list[dict[str, object]]:
    """Scenario-level artifacts as CTRF attachments, MIME-typed by kind.

    Paths stay run-directory relative, matching how `manifest.json` records them.
    """
    return [
        {"name": a.name, "contentType": _content_type(a.kind), "path": a.name} for a in r.artifacts
    ]


def _test_extra(r: RunResult) -> dict[str, object]:
    """Bajutsu surplus with no first-class CTRF home, kept under the test's `extra`."""
    extra: dict[str, object] = {"backend": r.backend}
    if r.sid:
        extra["sid"] = r.sid
    # The lifecycle phases (BE-0392) sit beside `steps` rather than inside it, so a consumer reading
    # only `steps` would miss a run whose sole failure was in setup or teardown. CTRF has one `steps`
    # array per test and no phase concept, so they ride `extra` the way `expect` already does.
    if r.before_outcomes:
        extra["before"] = [_step(s) for s in r.before_outcomes]
    if r.after_outcomes:
        extra["after"] = [_step(s) for s in r.after_outcomes]
    if r.expect_results:
        extra["expect"] = [
            {"ok": a.ok, "kind": a.kind, "detail": a.detail, "reason": a.reason}
            for a in r.expect_results
        ]
    if r.expect_alerts:
        extra["expectAlerts"] = [{"label": a.label, "kind": a.kind} for a in r.expect_alerts]
    if r.skipped_captures:
        extra["skippedCaptures"] = [
            {"kind": c.kind, "reason": c.reason} for c in r.skipped_captures
        ]
    return extra


def _test(r: RunResult) -> dict[str, object]:
    test: dict[str, object] = {
        "name": _test_name(r),
        "status": _status(r.ok),
        "duration": _ms(r.duration_s),
        "steps": [_step(s) for s in r.steps],
        "extra": _test_extra(r),
    }
    if not r.ok:
        test["message"] = r.failure or "failed"
        test["trace"] = _details(r)
    if r.engine:
        test["browser"] = r.engine
    if device := _device(r):
        test["device"] = device
    if attachments := _attachments(r):
        test["attachments"] = attachments
    return test


def _environment(provenance: dict[str, object] | None) -> dict[str, object] | None:
    """The run environment, drawn only from stored data so the export stays reproducible.

    Just the commit from provenance today — no live host info, which would make `ctrf.json` vary
    across machines and re-renders of the same run. None when there's nothing to record, so the
    optional block is omitted rather than emitted empty.
    """
    if provenance and (commit := provenance.get("gitRevision")):
        return {"commit": commit}
    return None


def _summary(started: datetime | None, results: list[RunResult]) -> dict[str, object]:
    passed = sum(1 for r in results if r.ok)
    # Convert the summed seconds to ms once (not per-scenario), so the total isn't undercounted by
    # the per-scenario truncation each `tests[].duration` incurs.
    duration = _ms(sum(r.duration_s for r in results))
    start = int(started.timestamp() * 1000) if started else 0
    return {
        "tests": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "skipped": 0,
        "pending": 0,
        "other": 0,
        "start": start,
        "stop": start + duration,
        "duration": duration,
    }


def ctrf_json(
    run_id: str,
    results: list[RunResult],
    *,
    provenance: dict[str, object] | None = None,
) -> dict[str, object]:
    """Project the run's result model into a CTRF document (BE-0161).

    Reads the same in-memory model the JUnit/HTML reporters build, so iOS, web, and future
    backends export identically with no per-app branching. The output is a pure function of the
    stored run — no live host state — so `bajutsu report` regenerates it byte-for-byte (BE-0068).
    `provenance` is the manifest's run-identity stamp (BE-0049); its `toolVersion` / `gitRevision`
    feed `tool.version` and `environment.commit`, and it is None for a run without the stamp.

    Args:
        run_id: The run's `YYYYmmdd-HHMMSS` id (UTC), used for `summary.start` and `timestamp`.
        results: One `RunResult` per scenario — or per engine x scenario cell on a matrix run,
            each becoming a CTRF test with the engine in its name and `browser`.
        provenance: The manifest provenance block, or None.

    Returns:
        The CTRF document as a plain dict, ready to serialize beside `manifest.json`.
    """
    started = _run_started(run_id)
    tool: dict[str, object] = {"name": "bajutsu"}
    tool["version"] = (provenance or {}).get("toolVersion") or __version__
    inner: dict[str, object] = {
        "tool": tool,
        "summary": _summary(started, results),
        "tests": [_test(r) for r in results],
    }
    # Environment is optional; include it only when there is stored data to record (a commit).
    if (environment := _environment(provenance)) is not None:
        inner["environment"] = environment
    # The engine x scenario matrix (BE-0076) has no first-class CTRF home; carry the aggregate under
    # `results.extra`, omitted for a single-engine / iOS run (which keeps the plain shape).
    if (matrix := _matrix(results)) is not None:
        inner["extra"] = {"matrix": matrix}
    doc: dict[str, object] = {
        "reportFormat": "CTRF",
        "specVersion": SPEC_VERSION,
        "generatedBy": "bajutsu",
        "results": inner,
    }
    # `timestamp` (RFC 3339) is derived from the run's UTC start, not wall-clock now, so a re-render
    # stays reproducible; omitted when the runId isn't a timestamp rather than fabricated.
    if started is not None:
        doc["timestamp"] = started.isoformat()
    return doc
