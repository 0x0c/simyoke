"""Tests for loading a persisted run back into the report renderer (BE-0068).

The renderer must be a pure function of data stored in the run dir, so a finished run can be
re-rendered offline. These pin the round-trip (manifest -> RunResults without loss), version
tolerance (an older manifest still loads), and that the manifest carries the render model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bajutsu.common.assertions import AssertionResult, VisualEvidence
from bajutsu.common.drivers.actuation import Actuation
from bajutsu.common.evidence import Artifact
from bajutsu.common.orchestrator import AlertEvent, RunResult, SkippedCapture, StepOutcome
from bajutsu.common.report.load import load_run, results_from_manifest
from bajutsu.common.report.manifest import manifest_dict


def _result() -> RunResult:
    return RunResult(
        scenario="checkout",
        ok=False,
        steps=[
            StepOutcome(
                index=0,
                action="tap home.start",
                ok=True,
                duration_s=0.5,
                started_at=1_700_000_000.25,
                assertion_results=[AssertionResult(ok=True, kind="exists", detail="home.title")],
                artifacts=[Artifact("0/shot.png", "screenshot", "simctl")],
                alerts=[AlertEvent("Allow")],
                actuations=[
                    Actuation(
                        gesture="tap",
                        via="coordinate",
                        unit="point",
                        points=((60.0, 120.0),),
                        frame=(20.0, 100.0, 80.0, 40.0),
                        target="home.start",
                    ),
                    # A handle-based record: no point, and a duration — so the round trip is exercised
                    # for both shapes, including the `None`s JSON keeps as nulls.
                    Actuation(
                        gesture="longPress",
                        via="handle",
                        unit="point",
                        target="home.start",
                        duration_s=0.7,
                    ),
                ],
            ),
            StepOutcome(index=1, action="tap pay", ok=False, reason="not found"),
        ],
        expect_results=[
            AssertionResult(
                ok=False,
                kind="visual",
                detail="home",
                reason="diff 3%",
                visual=VisualEvidence("home", "1/visual-actual.png", diff_pct=3.0, missing=False),
            )
        ],
        failure="pay missing",
        artifacts=[Artifact("network.json", "network", "collector")],
        backend="xcuitest",
        device="UDID-1",
        device_name="iPhone 17 Pro",
        device_runtime="iOS 26",
        duration_s=2.5,
        video_anchor_s=1_700_000_000.0,
        # wall_offset_s is deliberately left at its default: manifest_dict excludes it (BE-0348 —
        # see test_manifest.py's test_manifest_excludes_wall_offset_s), so a non-default value here
        # would make this round-trip test fail for a reason unrelated to what it checks.
        # One of each kind, so the discriminator that tells a swiped banner from a dismissed alert
        # (BE-0416) is exercised by the round trip rather than only its default.
        expect_alerts=[
            AlertEvent("Dismiss"),
            AlertEvent("Ready for Apple Intelligence", kind="notificationBanner"),
        ],
        expect_actuations=[
            Actuation(gesture="systemAlert", via="handle", unit="point", target="alert.dismiss")
        ],
        skipped_captures=[SkippedCapture("video", "no eligible backend")],
    )


def test_round_trip_through_manifest_is_lossless() -> None:
    original = [_result()]
    # go through JSON the way the run dir does, then back
    data = json.loads(json.dumps(manifest_dict("r1", original)))
    assert results_from_manifest(data) == original


def test_manifest_carries_schema_version_and_source_name() -> None:
    data = manifest_dict("r1", [_result()], source_name="smoke.yaml")
    # bumped for the optional top-level `target` / `label` stamps (BE-0404)
    assert data["schemaVersion"] == 10
    assert data["sourceName"] == "smoke.yaml"


def test_loads_a_legacy_manifest_without_schema_version() -> None:
    # a run baked before versioning: no schemaVersion / sourceName, no newer fields
    legacy = {
        "runId": "old",
        "ok": True,
        "backend": "xcuitest",
        "scenarios": [{"scenario": "smoke", "ok": True, "steps": []}],
    }
    [r] = results_from_manifest(legacy)
    assert r.scenario == "smoke" and r.ok is True and r.steps == []


def test_ignores_unknown_newer_fields() -> None:
    # a manifest from a newer version with a field this code doesn't know must not crash
    data = manifest_dict("r1", [_result()])
    data["scenarios"][0]["futureField"] = "ignored"  # type: ignore[index]
    assert results_from_manifest(data)[0].scenario == "checkout"


def test_load_run_normalizes_malformed_scenario_to_valueerror(tmp_path: Path) -> None:
    # A manifest present but a corrupt scenario.yaml is "malformed" — load_run raises ValueError
    # (not a bare yaml.YAMLError), honoring its one documented malformed-input type so callers can
    # catch a single type for "can't load this run" (BE-0068 serve render-on-view falls back then).
    run = tmp_path / "r1"
    run.mkdir()
    (run / "manifest.json").write_text('{"runId": "r1", "scenarios": []}', encoding="utf-8")
    (run / "scenario.yaml").write_text("{ bad: yaml ::", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed run model"):
        load_run(run)


def test_load_run_missing_file_raises_oserror(tmp_path: Path) -> None:
    # A missing run (no manifest.json) stays an OSError, distinct from malformed content.
    with pytest.raises(OSError, match="manifest"):
        load_run(tmp_path / "nope")


def test_a_malformed_actuation_record_is_dropped_whole_not_field_by_field() -> None:
    # Per-field degradation would be worse than useless here: a swipe whose second point is corrupt
    # would reconstruct as a plausible one-point gesture, and a corrupt `frame` would become None —
    # which in this schema *means* "the driver resolved no element". Dropping the record keeps every
    # surviving record trustworthy.
    data = manifest_dict("r1", [_result()])
    step = data["scenarios"][0]["steps"][0]  # type: ignore[index]
    step["actuations"] = [
        {"gesture": "swipe", "via": "coordinate", "unit": "pixel", "points": [[1, 2], ["a", "b"]]},
        {"gesture": "tap", "via": "coordinate", "unit": "pixel", "frame": [1, 2, 3]},
        {"via": "coordinate", "unit": "pixel"},  # no gesture at all
        {"gesture": "tap", "via": "coordinate", "unit": "pixel", "points": [[9, 9]]},
    ]

    [restored] = results_from_manifest(data)

    # Only the well-formed record survives; the three damaged ones are gone, not half-reconstructed.
    assert [(a.gesture, a.points) for a in restored.steps[0].actuations] == [("tap", ((9.0, 9.0),))]


def test_a_substitution_survives_the_round_trip() -> None:
    # The token is the only signal that the element actuated is not the one the selector named, so a
    # report re-rendered offline has to still carry it.
    original = [_result()]
    original[0].steps[0].actuations.append(
        Actuation(
            gesture="tap",
            via="handle",
            unit="point",
            target="log.count-Increment",
            substitution="soleHittableDescendant",
        )
    )
    data = json.loads(json.dumps(manifest_dict("r1", original)))
    assert results_from_manifest(data) == original


def test_an_older_manifest_without_substitution_loads_as_none() -> None:
    # v6 and earlier carry no such key, and its absence means exactly what a reader should conclude:
    # no substitution happened.
    data = manifest_dict("r1", [_result()])
    step = data["scenarios"][0]["steps"][0]  # type: ignore[index]
    step["actuations"] = [{"gesture": "tap", "via": "handle", "unit": "point"}]

    [restored] = results_from_manifest(data)

    assert restored.steps[0].actuations[0].substitution is None


def test_a_malformed_substitution_degrades_without_dropping_the_record() -> None:
    # Unlike geometry, a damaged token cannot make a record read as a different gesture — dropping
    # the whole record over it would lose the coordinate evidence for no gain.
    data = manifest_dict("r1", [_result()])
    step = data["scenarios"][0]["steps"][0]  # type: ignore[index]
    step["actuations"] = [
        {"gesture": "tap", "via": "handle", "unit": "point", "substitution": 7},
    ]

    [restored] = results_from_manifest(data)

    assert len(restored.steps[0].actuations) == 1
    assert restored.steps[0].actuations[0].substitution is None


def test_a_generated_value_survives_the_round_trip() -> None:
    # The recorded value is the run's only trace of what a `generate` step produced (BE-0377), so a
    # report re-rendered offline has to still carry it.
    original = [_result()]
    original[0].steps[0].generated = "k7fq2xzp"
    data = json.loads(json.dumps(manifest_dict("r1", original)))
    assert results_from_manifest(data) == original


def test_an_older_manifest_without_generated_loads_as_none() -> None:
    # v7 and earlier carry no such key, and its absence means what a reader should conclude: this
    # step produced no value.
    data = manifest_dict("r1", [_result()])
    step = data["scenarios"][0]["steps"][0]  # type: ignore[index]
    del step["generated"]

    [restored] = results_from_manifest(data)

    assert restored.steps[0].generated is None


def test_a_loader_side_drop_is_disclosed_through_dropped_actuations() -> None:
    # A truncated log discloses itself through `dropped_actuations` when a *driver* truncates it
    # (BE-0332 Unit 4's own reasoning for why a log line is not evidence). A record that instead
    # arrives damaged and is dropped by the loader must add to that same field, not vanish into a
    # warning line the run directory never keeps — the report and the trace only look at the field.
    data = manifest_dict("r1", [_result()])
    step = data["scenarios"][0]["steps"][0]  # type: ignore[index]
    step["actuations"] = [
        {"gesture": "tap", "via": "coordinate", "unit": "pixel", "points": [["a", "b"]]},
        {"gesture": "tap", "via": "coordinate", "unit": "pixel", "points": [[9, 9]]},
    ]
    step["dropped_actuations"] = 2  # the run's own truncation count, already recorded

    [restored] = results_from_manifest(data)

    # The loader's one fresh drop adds to the two the run already disclosed, rather than replacing or
    # ignoring them.
    assert restored.steps[0].dropped_actuations == 3


def test_a_wrong_typed_dropped_actuations_count_does_not_crash_the_render() -> None:
    # `dropped_actuations` is itself a disclosure mechanism: a wrong-typed value stored under it (a
    # hand-edited or corrupted manifest) must degrade to "nothing extra disclosed", not raise out of
    # `results_from_manifest` and take the whole report down with it.
    data = json.loads(json.dumps(manifest_dict("r1", [_result()])))
    step = data["scenarios"][0]["steps"][0]
    step["dropped_actuations"] = "oops"

    [restored] = results_from_manifest(data)

    assert restored.steps[0].dropped_actuations == 0


def test_a_negative_dropped_actuations_count_degrades_to_zero() -> None:
    # A count is disclosure, not just a type: a negative value is exactly as nonsensical to render
    # ("+-1 actuation(s) missing") as a wrong-typed one, so it must degrade the same way.
    data = json.loads(json.dumps(manifest_dict("r1", [_result()])))
    step = data["scenarios"][0]["steps"][0]
    step["dropped_actuations"] = -1

    [restored] = results_from_manifest(data)

    assert restored.steps[0].dropped_actuations == 0


def test_a_malformed_expect_actuation_is_disclosed_through_its_own_dropped_count() -> None:
    # `expect_actuations` is the one other place an `Actuation` list lives on the manifest (the
    # alert-guard's expect-phase dismissing tap, which belongs to no step), so a damaged record there
    # must be disclosed the same way a step's own dropped actuation already is, not silently vanish.
    data = manifest_dict("r1", [_result()])
    data["scenarios"][0]["expect_actuations"] = [  # type: ignore[index]
        {"gesture": "tap", "via": "coordinate", "unit": "point", "points": [["a", "b"]]},
        {"gesture": "tap", "via": "coordinate", "unit": "point", "points": [[9, 9]]},
    ]
    data["scenarios"][0]["dropped_expect_actuations"] = 2  # type: ignore[index]

    [restored] = results_from_manifest(data)

    # The loader's one fresh drop adds to the two the run already disclosed, rather than replacing or
    # ignoring them.
    assert len(restored.expect_actuations) == 1
    assert restored.dropped_expect_actuations == 3


def test_a_wrong_typed_scalar_field_degrades_to_none_not_a_string() -> None:
    # `target` / `accepted` / `duration_s` / `scale` / `radians` carry no runtime check of their own
    # today, so a corrupt value among them reconstructs into an `Actuation` whose field holds a string
    # where `bool | None` or `float | None` is declared — mypy accepts it because `known` is `Any`.
    # Downstream that reads as a refused attempt landing (`accepted is False` is `False` for the string
    # "no") or a bogus duration rendered verbatim. Each field must instead degrade to the same "not
    # recorded" `None` a corrupt geometry field already gets.
    data = manifest_dict("r1", [_result()])
    step = data["scenarios"][0]["steps"][0]  # type: ignore[index]
    step["actuations"] = [
        {
            "gesture": "tap",
            "via": "coordinate",
            "unit": "pixel",
            "accepted": "no",
            "duration_s": "1.5s",
        }
    ]

    [restored] = results_from_manifest(data)

    [record] = restored.steps[0].actuations
    assert (record.accepted, record.duration_s) == (None, None)


def test_one_malformed_record_does_not_fail_the_whole_render() -> None:
    # A missing required key used to raise a bare dataclass TypeError out of the loader, taking down
    # the entire run's report for one bad entry.
    data = manifest_dict("r1", [_result()])
    data["scenarios"][0]["steps"][0]["actuations"] = [{"nothing": "useful"}]  # type: ignore[index]

    [restored] = results_from_manifest(data)

    assert restored.scenario == "checkout"
    assert restored.steps[0].actuations == []
