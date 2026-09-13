"""Tests for the CTRF (Common Test Report Format) export (BE-0161).

The exporter is a pure projection of the run result model; these tests validate its output
against the vendored official CTRF JSON schema (`ctrf.schema.json`, specVersion 0.0.0, from
https://github.com/ctrf-io/ctrf) and check that the mapped fields round-trip.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from _report import _json_obj
from jsonschema import Draft7Validator

from bajutsu.common.assertions import AssertionResult
from bajutsu.common.evidence import Artifact
from bajutsu.common.orchestrator import AlertEvent, RunResult, SkippedCapture, StepOutcome
from bajutsu.common.report import ctrf_json

_SCHEMA = json.loads((Path(__file__).parent / "ctrf.schema.json").read_text(encoding="utf-8"))


def _validate(doc: dict[str, object]) -> None:
    """Fail with every schema violation at once (clearer than the first-error default)."""
    # `e.path` is a deque of mixed str keys / int indices; stringify each element so the sort key is
    # always orderable (comparing a str to an int would raise and hide the real violations).
    errors = sorted(
        Draft7Validator(_SCHEMA).iter_errors(doc), key=lambda e: [str(p) for p in e.path]
    )
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def _passing() -> RunResult:
    return RunResult(
        scenario="login",
        ok=True,
        steps=[
            StepOutcome(index=0, action="tap", ok=True, reason="", duration_s=0.5),
            StepOutcome(index=1, action="type", ok=True, reason="", duration_s=0.25),
        ],
        expect_results=[AssertionResult(ok=True, kind="exists", detail="home.title")],
        backend="fake",
        duration_s=1.5,
        artifacts=[Artifact(name="00-login/scenario.mp4", kind="video", provider="simctl")],
        device_name="iPhone 15",
        device_runtime="iOS 17.2",
    )


def _failing() -> RunResult:
    return RunResult(
        scenario="checkout",
        ok=False,
        steps=[StepOutcome(index=0, action="tap", ok=False, reason="element not found")],
        expect_results=[AssertionResult(ok=False, kind="exists", detail="cart", reason="no match")],
        failure="step 0 tap: element not found",
        backend="fake",
        duration_s=0.8,
    )


def test_serial_run_validates_against_ctrf_schema() -> None:
    doc = ctrf_json("20260704-101500", [_passing(), _failing()])
    _validate(doc)


def test_report_and_spec_identity() -> None:
    doc = ctrf_json("20260704-101500", [_passing()])
    assert doc["reportFormat"] == "CTRF"
    assert doc["specVersion"] == "0.0.0"
    assert doc["generatedBy"] == "bajutsu"
    results = doc["results"]
    assert results["tool"]["name"] == "bajutsu"  # type: ignore[index]


def test_summary_counts_and_duration() -> None:
    doc = ctrf_json("20260704-101500", [_passing(), _failing()])
    summary = doc["results"]["summary"]  # type: ignore[index]
    assert summary["tests"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["skipped"] == summary["pending"] == summary["other"] == 0
    # Σ duration_s x 1000, in milliseconds.
    assert summary["duration"] == int((1.5 + 0.8) * 1000)
    # start derives from the runId wall-clock; stop = start + total duration.
    assert summary["stop"] == summary["start"] + summary["duration"]


def test_summary_duration_sums_seconds_before_truncating() -> None:
    # duration is Σ(duration_s) x 1000, computed once — not Σ(per-scenario truncated ms), which would
    # lose up to ~1ms per scenario. Two sub-millisecond scenarios must still contribute their sum.
    r1 = RunResult(scenario="a", ok=True, steps=[], backend="fake", duration_s=0.0006)
    r2 = RunResult(scenario="b", ok=True, steps=[], backend="fake", duration_s=0.0007)
    summary = ctrf_json("20260704-101500", [r1, r2])["results"]["summary"]  # type: ignore[index]
    assert summary["duration"] == 1  # int(0.0013 * 1000); per-scenario truncation would give 0


def test_summary_start_from_run_id() -> None:
    # The runId is a UTC wall-clock stamp, so it must be parsed as UTC (not the host timezone) —
    # otherwise summary.start is off by the host's offset. An unparseable id falls back to 0.
    expected = int(datetime(2026, 7, 4, 10, 15, 0, tzinfo=UTC).timestamp() * 1000)
    assert ctrf_json("20260704-101500", [_passing()])["results"]["summary"]["start"] == expected  # type: ignore[index]
    assert ctrf_json("run1", [_passing()])["results"]["summary"]["start"] == 0  # type: ignore[index]


def test_test_case_fields_round_trip() -> None:
    doc = ctrf_json("20260704-101500", [_passing(), _failing()])
    tests = doc["results"]["tests"]  # type: ignore[index]
    passing, failing = tests[0], tests[1]
    assert passing["name"] == "login"
    assert passing["status"] == "passed"
    assert passing["duration"] == 1500  # 1.5s → ms
    assert failing["status"] == "failed"
    assert failing["message"] == "step 0 tap: element not found"
    assert "step 0 tap" in failing["trace"]


def test_steps_project_name_status_with_rich_fields_in_extra() -> None:
    doc = ctrf_json("20260704-101500", [_passing()])
    steps = doc["results"]["tests"][0]["steps"]  # type: ignore[index]
    assert [s["name"] for s in steps] == ["tap", "type"]
    assert [s["status"] for s in steps] == ["passed", "passed"]
    # CTRF steps allow only name/status/extra, so the richer per-step data lands in step.extra.
    assert steps[0]["extra"]["duration"] == 500


def test_attachments_carry_mime_content_type() -> None:
    doc = ctrf_json("20260704-101500", [_passing()])
    attachments = doc["results"]["tests"][0]["attachments"]  # type: ignore[index]
    assert attachments[0]["path"] == "00-login/scenario.mp4"
    assert attachments[0]["contentType"] == "video/mp4"


def test_browser_and_device_fields() -> None:
    doc = ctrf_json("20260704-101500", [_passing()])
    test = doc["results"]["tests"][0]  # type: ignore[index]
    assert "iPhone 15" in test["device"]
    assert "iOS 17.2" in test["device"]


def _engine(scenario: str, engine: str, ok: bool) -> RunResult:
    return RunResult(
        scenario=scenario,
        ok=ok,
        steps=[StepOutcome(index=0, action="tap", ok=ok)],
        engine=engine,
        backend="playwright",
        sid=f"00-{scenario}",
        duration_s=0.3,
        failure=None if ok else "mismatch",
    )


def test_matrix_run_one_test_per_cell_validates() -> None:
    # A --browsers run concatenates the per-engine passes (BE-0076): one RunResult per cell.
    results = [
        _engine("login", "chromium", True),
        _engine("login", "webkit", False),
    ]
    doc = ctrf_json("20260704-101500", results)
    _validate(doc)
    tests = doc["results"]["tests"]  # type: ignore[index]
    assert doc["results"]["summary"]["tests"] == 2  # type: ignore[index]
    # The engine is carried in the test name and the browser field, mirroring junit's classname.
    assert tests[0]["browser"] == "chromium"
    assert "chromium" in tests[0]["name"]
    assert tests[1]["status"] == "failed"


def test_matrix_aggregate_lives_in_results_extra() -> None:
    results = [_engine("login", "chromium", True), _engine("login", "webkit", False)]
    doc = ctrf_json("20260704-101500", results)
    matrix = doc["results"]["extra"]["matrix"]  # type: ignore[index]
    assert matrix["engines"] == ["chromium", "webkit"]
    assert matrix["scenarios"] == ["login"]


def test_single_engine_run_has_no_matrix_extra() -> None:
    doc = ctrf_json("20260704-101500", [_passing()])
    # No engine axis → no matrix surplus (the extra block is omitted, not an empty matrix).
    extra = _json_obj(doc["results"]).get("extra")
    assert extra is None or "matrix" not in extra


def test_tool_version_from_provenance() -> None:
    doc = ctrf_json("20260704-101500", [_passing()], provenance={"toolVersion": "9.9.9"})
    assert doc["results"]["tool"]["version"] == "9.9.9"  # type: ignore[index]


def test_environment_commit_from_provenance() -> None:
    doc = ctrf_json("20260704-101500", [_passing()], provenance={"gitRevision": "abc123"})
    _validate(doc)
    assert doc["results"]["environment"]["commit"] == "abc123"  # type: ignore[index]


def test_environment_omitted_without_stored_commit() -> None:
    # No live host info is injected, so with nothing stored the optional block is omitted entirely
    # (keeping the export a pure function of stored data — reproducible across machines).
    doc = ctrf_json("20260704-101500", [_passing()])
    assert "environment" not in doc["results"]  # type: ignore[operator]


def test_timestamp_derived_from_run_id_not_wall_clock() -> None:
    # The document timestamp comes from the run's UTC start, not `now()`, so a re-render is stable;
    # an unparseable runId omits the optional field rather than fabricating one.
    doc = ctrf_json("20260704-101500", [_passing()])
    assert doc["timestamp"] == datetime(2026, 7, 4, 10, 15, 0, tzinfo=UTC).isoformat()
    assert "timestamp" not in ctrf_json("run1", [_passing()])


def test_export_is_reproducible() -> None:
    # A pure projection of the stored run: identical input yields byte-identical JSON, which is what
    # lets `bajutsu report` regenerate ctrf.json deterministically (BE-0068).
    args = ("20260704-101500", [_passing(), _failing()])
    assert json.dumps(ctrf_json(*args)) == json.dumps(ctrf_json(*args))


def test_attachment_content_type_maps_known_kinds_and_defaults_unknown() -> None:
    r = RunResult(
        scenario="s",
        ok=True,
        steps=[],
        backend="fake",
        artifacts=[
            Artifact(name="s/device.log", kind="deviceLog", provider="simctl"),
            Artifact(name="s/heap.bin", kind="heapdump", provider="x"),  # unmapped kind
        ],
    )
    attachments = ctrf_json("20260704-101500", [r])["results"]["tests"][0]["attachments"]  # type: ignore[index]
    assert attachments[0]["contentType"] == "text/plain"
    # An unknown kind still exports, as an opaque octet-stream, rather than breaking the document.
    assert attachments[1]["contentType"] == "application/octet-stream"


def test_step_extra_carries_reason_assertions_and_artifacts() -> None:
    step = StepOutcome(
        index=3,
        action="tap",
        ok=False,
        reason="not found",
        duration_s=0.2,
        assertion_results=[AssertionResult(ok=False, kind="exists", detail="x", reason="no match")],
        artifacts=[Artifact(name="03/after.png", kind="screenshot", provider="driver")],
    )
    r = RunResult(scenario="s", ok=False, steps=[step], failure="boom", backend="fake")
    extra = ctrf_json("20260704-101500", [r])["results"]["tests"][0]["steps"][0]["extra"]  # type: ignore[index]
    assert extra["index"] == 3
    assert extra["reason"] == "not found"
    assert extra["assertions"] == [
        {"ok": False, "kind": "exists", "detail": "x", "reason": "no match"}
    ]
    assert extra["artifacts"] == [{"name": "03/after.png", "kind": "screenshot"}]


def test_test_extra_carries_bajutsu_surplus() -> None:
    r = RunResult(
        scenario="s",
        ok=True,
        steps=[],
        expect_results=[AssertionResult(ok=True, kind="exists", detail="home.title")],
        backend="xcuitest",
        sid="00-s",
        expect_alerts=[AlertEvent(label="Not Now")],
        skipped_captures=[SkippedCapture(kind="network", reason="no eligible backend")],
    )
    extra = ctrf_json("20260704-101500", [r])["results"]["tests"][0]["extra"]  # type: ignore[index]
    assert extra["backend"] == "xcuitest"
    assert extra["sid"] == "00-s"
    assert extra["expect"] == [{"ok": True, "kind": "exists", "detail": "home.title", "reason": ""}]
    assert extra["expectAlerts"] == [{"label": "Not Now", "kind": "alert"}]
    assert extra["skippedCaptures"] == [{"kind": "network", "reason": "no eligible backend"}]


def test_device_field_partial_and_absent() -> None:
    def _device_of(name: str, runtime: str) -> object:
        r = RunResult(
            scenario="s",
            ok=True,
            steps=[],
            backend="fake",
            device_name=name,
            device_runtime=runtime,
        )
        return ctrf_json("20260704-101500", [r])["results"]["tests"][0].get("device", "<absent>")  # type: ignore[index]

    assert _device_of("iPhone 15", "iOS 17.2") == "iPhone 15 (iOS 17.2)"
    assert _device_of("iPhone 15", "") == "iPhone 15"
    assert _device_of("", "iOS 17.2") == "iOS 17.2"
    # Neither set → the device key is omitted rather than emitted as an empty string.
    assert _device_of("", "") == "<absent>"


# --- wiring: run and `bajutsu report` both emit ctrf.json ---


def test_write_report_emits_valid_ctrf_json(tmp_path: Path) -> None:
    from bajutsu.common.report import write_report

    run_dir = tmp_path / "20260704-101500"
    write_report(run_dir, run_dir.name, [_passing()])
    _validate(json.loads((run_dir / "ctrf.json").read_text(encoding="utf-8")))


def test_report_command_regenerates_ctrf(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from bajutsu.cli import app
    from bajutsu.common.report import write_report
    from bajutsu.common.scenario import dump_scenario_file, load_scenarios

    scenarios = load_scenarios("- name: login\n  steps:\n    - tap: { id: a }\n")
    run_dir = tmp_path / "runs" / "20260704-101500"
    run_dir.mkdir(parents=True)
    (run_dir / "scenario.yaml").write_text(dump_scenario_file(scenarios), encoding="utf-8")
    write_report(run_dir, run_dir.name, [_passing()])
    (run_dir / "ctrf.json").unlink()  # a run baked before the exporter existed

    result = CliRunner().invoke(app, ["report", str(run_dir)])
    assert result.exit_code == 0, result.output
    # `bajutsu report` re-emits ctrf.json for a past run from its stored model (BE-0068).
    _validate(json.loads((run_dir / "ctrf.json").read_text(encoding="utf-8")))
