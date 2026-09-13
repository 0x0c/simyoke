"""Tests for the report manifest.json and JUnit XML."""

from __future__ import annotations

import json
from pathlib import Path

from _report import _failing, _json_obj, _json_str, _passing, _scenarios

from bajutsu.common.orchestrator import AlertEvent, RunResult, StepOutcome
from bajutsu.common.report import junit_xml, manifest_dict, write_report


def test_manifest_structure() -> None:
    m = manifest_dict("run1", [_passing()])
    assert m["runId"] == "run1"
    assert m["ok"] is True
    scenarios = m["scenarios"]
    assert isinstance(scenarios, list)
    assert scenarios[0]["scenario"] == "s1"
    assert scenarios[0]["ok"] is True
    assert scenarios[0]["steps"][0]["action"] == "tap"


def test_manifest_overall_ok_is_and() -> None:
    assert manifest_dict("r", [_passing(), _failing()])["ok"] is False


def test_manifest_records_backend() -> None:
    # run_scenario stamps each result with the driver it ran (here the fake driver),
    # and the manifest summarizes the run's actuator at top level.
    m = manifest_dict("run1", [_passing()])
    assert m["backend"] == "fake"
    assert _scenarios(m)[0]["backend"] == "fake"


def test_manifest_joins_distinct_backends_across_scenarios() -> None:
    # BE-0240: per-scenario actuator selection lets scenarios in one run differ; the top-level
    # backend joins the distinct actuators (ordered-unique) that actually drove them.
    results = [
        RunResult(scenario="a", ok=True, steps=[], backend="adb"),
        RunResult(scenario="b", ok=True, steps=[], backend="fake"),
        RunResult(scenario="c", ok=True, steps=[], backend="adb"),
    ]
    assert manifest_dict("run1", results)["backend"] == "adb, fake"


# --- run provenance & version stamping (BE-0049, the longitudinal-flakiness prerequisite) ---


def test_run_provenance_hashes_the_scenario_deterministically() -> None:
    from bajutsu.common.report.manifest import run_provenance

    yaml = "- name: s\n  steps:\n    - tap: { id: a }\n"
    p1 = run_provenance(yaml, git_revision=None)
    p2 = run_provenance(yaml, git_revision=None)
    assert p1["scenarioHash"] == p2["scenarioHash"]  # same content → same fingerprint
    assert _json_str(p1["scenarioHash"]).startswith("sha256:")
    # a different scenario fingerprints differently
    assert run_provenance(yaml + "\n", git_revision=None)["scenarioHash"] != p1["scenarioHash"]


def test_run_provenance_records_the_tool_version() -> None:
    from bajutsu import __version__
    from bajutsu.common.report.manifest import run_provenance

    assert run_provenance("x", git_revision=None)["toolVersion"] == __version__


def test_run_provenance_includes_git_revision_only_when_known() -> None:
    from bajutsu.common.report.manifest import run_provenance

    assert run_provenance("x", git_revision="abc123")["gitRevision"] == "abc123"
    # an unresolvable revision (not a git checkout) omits the key rather than recording null
    assert "gitRevision" not in run_provenance("x", git_revision=None)


def test_manifest_records_provenance_block() -> None:
    from bajutsu.common.report.manifest import run_provenance

    prov = run_provenance("- name: s\n  steps: []\n", git_revision="deadbeef")
    m = manifest_dict("run1", [_passing()], provenance=prov)
    assert m["provenance"] == prov
    assert m["ok"] is True  # provenance is metadata; it never changes the verdict


def test_manifest_omits_provenance_when_absent() -> None:
    assert "provenance" not in manifest_dict("run1", [_passing()])


def test_run_provenance_records_the_git_config_source() -> None:
    # A run whose config came from a Git source records which commit it executed (BE-0063), so the
    # manifest states the exact repo@sha behind a branch-based run.
    from bajutsu.common.report.manifest import run_provenance

    src = {"host": "github.com", "owner": "acme", "repo": "tests", "ref": "main", "sha": "deadbeef"}
    prov = run_provenance("x", git_revision=None, config_source=src)
    assert prov["configSource"] == src


def test_run_provenance_omits_config_source_for_a_local_config() -> None:
    from bajutsu.common.report.manifest import run_provenance

    assert "configSource" not in run_provenance("x", git_revision=None)


# --- cross-browser matrix (BE-0076 Phase 2): pure aggregation of per-engine verdicts ---


def _engine_result(scenario: str, engine: str, *, ok: bool) -> RunResult:
    """A per-engine RunResult tagged with its rendering engine (what a `--browsers` pass produces)."""
    return RunResult(
        scenario=scenario,
        ok=ok,
        steps=[],
        backend="playwright",
        engine=engine,
        failure=None if ok else f"failed on {engine}",
    )


def test_manifest_has_no_matrix_block_for_single_engine() -> None:
    # A single-engine run (no `engine` tag) keeps exactly today's shape — no matrix machinery.
    m = manifest_dict("r", [_passing()])
    assert "matrix" not in m


def test_manifest_matrix_aggregates_per_engine_verdicts() -> None:
    # chromium passes "login", webkit fails it: the matrix is a pure aggregation of those verdicts.
    results = [
        _engine_result("login", "chromium", ok=True),
        _engine_result("login", "webkit", ok=False),
    ]
    m = manifest_dict("r", results)
    matrix = _json_obj(m["matrix"])
    assert matrix["engines"] == ["chromium", "webkit"]
    assert matrix["scenarios"] == ["login"]
    cells = matrix["cells"]
    assert cells["login"]["chromium"]["ok"] is True
    assert cells["login"]["webkit"]["ok"] is False
    assert cells["login"]["webkit"]["failure"] == "failed on webkit"


def test_manifest_matrix_keeps_both_cells_for_same_named_scenarios() -> None:
    # Two distinct scenarios that happen to share a `name:` (nothing enforces uniqueness across a
    # multi-file run) must not collide in the matrix: each keeps its own row per engine instead of
    # the second silently overwriting the first's cell.
    results = [
        _engine_result("login", "chromium", ok=True),
        _engine_result("login", "chromium", ok=False),
        _engine_result("login", "webkit", ok=True),
        _engine_result("login", "webkit", ok=False),
    ]
    m = manifest_dict("r", results)
    matrix = _json_obj(m["matrix"])
    assert matrix["scenarios"] == ["login", "login (2)"]
    cells = matrix["cells"]
    assert cells["login"]["chromium"]["ok"] is True
    assert cells["login"]["webkit"]["ok"] is True
    assert cells["login (2)"]["chromium"]["ok"] is False
    assert cells["login (2)"]["chromium"]["failure"] == "failed on chromium"
    assert cells["login (2)"]["webkit"]["ok"] is False
    assert cells["login (2)"]["webkit"]["failure"] == "failed on webkit"


def test_manifest_matrix_disambiguated_label_skips_a_colliding_literal_name() -> None:
    # A third scenario whose literal name is "login (2)" must not collide with the synthesized
    # label already handed to the second "login" — that would silently wipe the second "login"'s
    # cell and render two identical "login (2)" rows (both bugs the disambiguation exists to avoid).
    results = [
        _engine_result("login", "chromium", ok=True),
        _engine_result("login", "chromium", ok=False),
        _engine_result("login (2)", "chromium", ok=True),
    ]
    m = manifest_dict("r", results)
    matrix = _json_obj(m["matrix"])
    assert matrix["scenarios"] == ["login", "login (2)", "login (2) (2)"]
    cells = matrix["cells"]
    assert cells["login"]["chromium"]["ok"] is True
    assert cells["login (2)"]["chromium"]["ok"] is False
    assert cells["login (2) (2)"]["chromium"]["ok"] is True


def test_manifest_matrix_keeps_flat_engine_tagged_scenarios() -> None:
    # The v1 shape is kept: `scenarios` stays the flat, engine-tagged result list.
    results = [
        _engine_result("login", "chromium", ok=True),
        _engine_result("login", "webkit", ok=False),
    ]
    m = manifest_dict("r", results)
    assert [(s["scenario"], s["engine"]) for s in _scenarios(m)] == [
        ("login", "chromium"),
        ("login", "webkit"),
    ]


def test_manifest_matrix_ok_is_all_must_pass() -> None:
    # Green only if every engine passes every scenario; one engine failing fails the run.
    all_pass = [
        _engine_result("login", "chromium", ok=True),
        _engine_result("login", "webkit", ok=True),
    ]
    assert manifest_dict("r", all_pass)["ok"] is True
    one_fails = [
        _engine_result("login", "chromium", ok=True),
        _engine_result("login", "webkit", ok=False),
    ]
    assert manifest_dict("r", one_fails)["ok"] is False


def test_junit_keys_engine_into_classname() -> None:
    # CI sees chromium.login and webkit.login as distinct cases.
    xml = junit_xml(
        [
            _engine_result("login", "chromium", ok=True),
            _engine_result("login", "webkit", ok=False),
        ]
    )
    assert 'classname="bajutsu.chromium"' in xml
    assert 'classname="bajutsu.webkit"' in xml


def test_junit_single_engine_classname_stays_bajutsu() -> None:
    # No engine tag → today's classname, so a non-matrix run is unchanged.
    assert 'classname="bajutsu"' in junit_xml([_passing()])
    assert "bajutsu." not in junit_xml([_passing()])


def test_junit_pass_and_fail() -> None:
    ok_xml = junit_xml([_passing()])
    assert 'tests="1"' in ok_xml
    assert 'failures="0"' in ok_xml
    assert "<failure" not in ok_xml

    bad_xml = junit_xml([_failing()])
    assert 'failures="1"' in bad_xml
    assert "<failure" in bad_xml


def test_write_report(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run3"
    manifest_path = write_report(run_dir, "run3", [_passing(), _failing()])
    assert manifest_path.exists()
    assert (run_dir / "junit.xml").exists()
    assert (run_dir / "report.html").exists()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["runId"] == "run3"
    assert data["ok"] is False
    assert len(data["scenarios"]) == 2


def test_manifest_records_scenario_duration() -> None:
    r = RunResult(scenario="s1", ok=True, steps=[], duration_s=2.5)
    assert _scenarios(manifest_dict("run1", [r]))[0]["duration_s"] == 2.5


def test_manifest_persists_video_anchor_s() -> None:
    # video_anchor_s is now an absolute wall-clock instant, so it means the same thing after the run
    # as during it — and a report needs it to derive a step's video-relative seconds from the
    # absolute started_at beside it (BE-0348). Unlike before this item, it must reach the persisted
    # manifest.
    r = RunResult(scenario="s1", ok=True, steps=[], video_anchor_s=1_700_000_000.5)
    scenario = _scenarios(manifest_dict("run1", [r]))[0]
    assert scenario["video_anchor_s"] == 1_700_000_000.5


def test_manifest_excludes_wall_offset_s() -> None:
    # wall_offset_s is scenario_wall_start - scenario_start: a delta that converts *this run's*
    # time.monotonic() instants to wall-clock ones. No monotonic instant survives into the manifest
    # for a later reader to convert with it, so — unlike video_anchor_s, which is itself already an
    # absolute instant — it stays excluded, the same way video_anchor_s used to be before BE-0348. A
    # non-default value here proves the exclusion is real, not merely a coincidence of an unset field
    # looking absent.
    from dataclasses import fields

    r = RunResult(scenario="s1", ok=True, steps=[], wall_offset_s=123456.789)
    scenario = _scenarios(manifest_dict("run1", [r]))[0]
    assert "wall_offset_s" not in scenario
    # Exactly this one field is missing — not a different field excluded by mistake, and not this
    # field plus others dropped by a broader (over-eager) exclusion.
    assert {f.name for f in fields(RunResult)} - set(scenario) == {"wall_offset_s"}


def test_manifest_records_device_environment() -> None:
    r = _passing()
    r.device, r.device_name, r.device_runtime = "SIM-1", "iPhone 15", "iOS 17.2"
    scenario = _scenarios(manifest_dict("run1", [r]))[0]
    assert scenario["device"] == "SIM-1"
    assert scenario["device_name"] == "iPhone 15"
    assert scenario["device_runtime"] == "iOS 17.2"


def test_manifest_records_dismissed_alerts() -> None:
    # asdict captures the dismissals so the manifest (the source of truth) carries them too.
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[StepOutcome(index=0, action="tap", ok=True, alerts=[AlertEvent(label="Not Now")])],
        expect_alerts=[AlertEvent(label="Allow")],
    )
    m = manifest_dict("run1", [r])
    scenario = _scenarios(m)[0]
    assert scenario["steps"][0]["alerts"] == [{"label": "Not Now", "kind": "alert"}]
    assert scenario["expect_alerts"] == [{"label": "Allow", "kind": "alert"}]
