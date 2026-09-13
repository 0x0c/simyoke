"""Tests for the HTML report screenshot/element-viewer, tree, and video."""

from __future__ import annotations

import json
from pathlib import Path

from _report import _el, _passing

from bajutsu.common.evidence import Artifact
from bajutsu.common.orchestrator import RunResult, SkippedCapture, StepOutcome
from bajutsu.common.report import html_report


def test_screenshot_or_tree_button_opens_element_viewer_and_arrows_navigate() -> None:
    # Clicking a step's screenshot — or the "tree" button — opens the element viewer; ← / → walk
    # the steps of the current scenario, looping. Inside the viewer, clicking the screenshot
    # enlarges it full-screen (a #imgz lightbox), where ← / → also walk the steps' screenshots.
    out = html_report("run1", [_passing()])
    assert "closest('.treebtn') || e.target.closest('.shot')" in out  # both open the element viewer
    assert (
        'id="imgz"' in out and "openImg(imEl.getAttribute('src'))" in out
    )  # viewer screenshot → enlarge
    assert "imgzSync" in out  # the enlarged view's ← / → drive the viewer and mirror its screenshot
    assert "ArrowLeft" in out and "ArrowRight" in out  # arrow keys walk the steps
    # navigation is scoped to one scenario (details.scn) and wraps at the ends
    assert "tvScopeFor" in out and "details.scn" in out and "% tvScope.length" in out


def test_step_click_seeks_without_autoplay() -> None:
    # Clicking a step seeks the recording but never starts playback on a paused video.
    # Playback is started only from the explicit play/pause control, never the seek path.
    out = html_report("run9", [_passing()])
    assert "v.currentTime = t;" in out  # step-row click seeks
    assert "if(v.paused) v.play();" in out  # play() is reachable only via the button
    # The seek handler stays seek-only (it has no .play() of its own).
    assert "Seek only" in out


def test_html_embeds_scenario_video() -> None:
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[],
        expect_results=[],
        artifacts=[Artifact("00-s1/scenario.mp4", "video", "simctl")],
    )
    out = html_report("run9", [r])
    assert "<video " in out
    assert 'src="00-s1/scenario.mp4"' in out
    # A scenario with no video artifact embeds no player. The bare "<video" substring (no
    # trailing space) is not enough on its own — the inlined stylesheet mentions "<video>" in a
    # comment on every page, video or not.
    assert "<video " not in html_report("run9", [_passing()])


def test_html_discloses_why_the_video_is_missing_on_a_backend_crash() -> None:
    # A scenario whose backend crashed mid-run and never recovered carries no video artifact
    # (the recording died with the lease), but the pipeline discloses the gap as a `SkippedCapture`
    # (BE-0020) — the report must say why the player is empty, not fall back to the generic
    # "no recording" it shows for a scenario that never captures video at all.
    r = RunResult(
        scenario="s1",
        ok=False,
        steps=[],
        expect_results=[],
        failure="backend crashed mid-run and did not recover across 2 attempts: boom",
        skipped_captures=[
            SkippedCapture(
                kind="video",
                reason="backend crashed mid-run and did not recover across 2 attempts: boom",
            )
        ],
    )
    out = html_report("run9", [r])
    assert "<video " not in out
    assert "video unavailable: backend crashed mid-run and did not recover" in out
    # The same disclosure also lands in the Environment tab's "skipped evidence" table (BE-0020).
    assert "skipped evidence" in out


def test_html_step_rows_carry_video_offset() -> None:
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(index=0, action="tap", ok=True, duration_s=0.2, started_at=0.0),
            StepOutcome(index=1, action="wait", ok=True, duration_s=1.1, started_at=1.5),
        ],
        expect_results=[],
        artifacts=[Artifact("00-s1/scenario.mp4", "video", "simctl")],
    )
    out = html_report("run1", [r])
    # Each step row is clickable and tagged with its offset into the recording…
    assert "class='srow ok' data-t='0.000'" in out
    assert "data-t='1.500'" in out
    # …and the JS seeks the video and highlights the playing step.
    assert "v.currentTime = t" in out
    assert "timeupdate" in out and "playing" in out


def test_html_step_row_carries_its_own_end_instant_when_it_differs_from_the_start() -> None:
    # A step long enough that its end reads differently from its start (rounded to .1s) gets a
    # second jump target — the `data-t-end` the row carries, and the "→" button report.js turns
    # it into.
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[StepOutcome(index=0, action="wait", ok=True, duration_s=1.1, started_at=1.5)],
        expect_results=[],
        artifacts=[Artifact("00-s1/scenario.mp4", "video", "simctl")],
    )
    out = html_report("run1", [r])
    assert "data-t='1.500'" in out and "data-t-end='2.600'" in out
    assert ">→2.6s<" in out


def test_html_step_row_omits_the_end_instant_when_rounding_matches_the_start() -> None:
    # A near-instant action's end rounds to the same display text as its start — no second jump
    # target, since it would seek to (in effect) the same instant the row's own start already does.
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[StepOutcome(index=0, action="tap", ok=True, duration_s=0.02, started_at=1.5)],
        expect_results=[],
        artifacts=[Artifact("00-s1/scenario.mp4", "video", "simctl")],
    )
    out = html_report("run1", [r])
    assert "data-t='1.500'" in out
    # Single-quoted: the row's own attribute, not the (double-quoted) JS source that builds the
    # seekbar's range-bar markup for rows that *do* have one, which is present on every page.
    assert "data-t-end='" not in out
    # The class name alone is a substring of the inlined stylesheet's own rules (present on every
    # page); the rendered button element is what actually depends on this step's own duration.
    assert 'class="stepjump stepjump-end"' not in out


def test_html_step_row_shows_its_elapsed_time_at_the_end_of_the_at_cell_when_it_differs() -> None:
    # A step long enough for its duration to round to something other than 0.0s shows that
    # duration in parentheses at the end of the `at` cell, after any second jump target
    # (`at_end`), so a slow step is visible without opening the recording.
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[StepOutcome(index=0, action="wait", ok=True, duration_s=1.1, started_at=1.5)],
        expect_results=[],
        artifacts=[Artifact("00-s1/scenario.mp4", "video", "simctl")],
    )
    out = html_report("run1", [r])
    assert 'class="stepdur">(1.1s)</span>' in out
    # It trails the second jump target (`at_end`), not the row's own `at` time. Slice from the
    # `at` cell itself: "stepjump-end" is also a substring of the inlined stylesheet's own rule,
    # which precedes every row and would otherwise make this comparison meaningless.
    atcell = out[out.index("class='atcell'") :]
    assert atcell.index("stepjump-end") < atcell.index('class="stepdur"')


def test_html_step_row_omits_elapsed_time_when_rounding_matches_the_start() -> None:
    # A near-instant action shows no elapsed time — the same rounding rule that omits the second
    # jump target, so a fast tap doesn't show a noisy "(0.0s)" on every row.
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[StepOutcome(index=0, action="tap", ok=True, duration_s=0.02, started_at=1.5)],
        expect_results=[],
        artifacts=[Artifact("00-s1/scenario.mp4", "video", "simctl")],
    )
    out = html_report("run1", [r])
    assert 'class="stepdur"' not in out


def test_html_derives_the_video_offset_from_absolute_timestamps() -> None:
    # A step records the absolute instant it began; the seek offset is derived here, at render time,
    # by subtracting the scenario's video anchor (BE-0348) — so an improved anchor makes an already
    # recorded run seek correctly without re-running it. The anchor precedes the first step by 2.5s
    # (a prestarted recording), which is what pushes that step off 0.0.
    anchor = 1_700_000_000.0
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(index=0, action="tap", ok=True, duration_s=0.2, started_at=anchor + 2.5),
            StepOutcome(index=1, action="wait", ok=True, duration_s=1.1, started_at=anchor + 4.0),
        ],
        expect_results=[],
        artifacts=[Artifact("00-s1/scenario.mp4", "video", "simctl")],
        video_anchor_s=anchor,
    )
    out = html_report("run1", [r])
    assert "class='srow ok' data-t='2.500'" in out
    assert "data-t='4.000'" in out
    # The raw epoch must never reach the page — that would seek the player past the end of any clip.
    assert str(int(anchor)) not in out


def test_html_step_offsets_survive_a_run_recorded_before_the_anchor_was_persisted() -> None:
    # A pre-BE-0348 manifest carries already-relative `started_at` values and no `video_anchor_s`,
    # so the anchor reconstructs at 0.0 and the derivation returns them unchanged (BE-0068's spirit:
    # an older run still renders rather than failing).
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[StepOutcome(index=0, action="tap", ok=True, started_at=1.5)],
        expect_results=[],
        artifacts=[Artifact("00-s1/scenario.mp4", "video", "simctl")],
    )
    assert "data-t='1.500'" in html_report("run1", [r])


def test_html_shows_step_screenshot_and_tree(tmp_path: Path) -> None:
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(
                index=0,
                action="tap",
                ok=True,
                started_at=0.0,
                artifacts=[
                    Artifact("00-s1/step0/after.png", "screenshot", "driver"),
                    Artifact("00-s1/step0/elements.json", "elements", "driver"),
                ],
            ),
        ],
        expect_results=[],
        artifacts=[],
    )
    step_dir = tmp_path / "00-s1" / "step0"
    step_dir.mkdir(parents=True)
    (step_dir / "elements.json").write_text(
        json.dumps([_el("home.title", "Welcome", ["staticText"])]), encoding="utf-8"
    )
    (step_dir / "after.png").write_bytes(b"PNG")
    out = html_report("run1", [r], tmp_path)
    # the step's screenshot thumbnail and its element viewer are shown
    assert 'class="shot"' in out and 'src="00-s1/step0/after.png"' in out
    # the element tree opens in-report (no new tab): a button + inline embedded data,
    # rendered into the #tv overlay rather than linking out to the json file.
    assert 'class="elnk treebtn"' in out
    assert 'target="_blank"' not in out
    assert "home.title" in out and "Welcome" in out
    assert 'id="tv"' in out and "tvFilter" in out
    # the screenshot and the tree button both open the element viewer (not the old "lb" preview); it
    # shows the step's own info above the table.
    assert 'id="lb"' not in out and "openLightbox" not in out
    assert 'class="tv-step"' in out
    # the ◀ N/M ▶ step controls are built below the element list (in JS), and the element filter
    # sits in its own band below the step info (not in the head).
    assert "tv-treenav" in out and "tv-prev" in out and "tv-next" in out
    assert "tv-pos" in out and "(tvIndex + 1) + '/' + tvScope.length" in out  # the N/M counter
    assert 'class="tv-filter"' in out


def test_html_tree_rows_carry_frame_for_screenshot_highlight(tmp_path: Path) -> None:
    # Each element row embeds its raw frame (points) and the table the screen rect, so
    # the viewer can highlight the hovered element's location on the screenshot.
    el = {**_el("home.cta", "Buy", ["button"]), "frame": (12.0, 40.0, 100.0, 36.0)}
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(
                index=0,
                action="tap",
                ok=True,
                started_at=0.0,
                artifacts=[
                    Artifact("00-s1/step0/after.png", "screenshot", "driver"),
                    Artifact("00-s1/step0/elements.json", "elements", "driver"),
                ],
            ),
        ],
        expect_results=[],
        artifacts=[],
    )
    step_dir = tmp_path / "00-s1" / "step0"
    step_dir.mkdir(parents=True)
    (step_dir / "elements.json").write_text(json.dumps([el]), encoding="utf-8")
    (step_dir / "after.png").write_bytes(b"PNG")
    out = html_report("run1", [r], tmp_path)
    # the row carries the frame; the table carries the screen extent (bbox: 112x76)
    assert 'class="tvrow" data-x="12" data-y="40" data-w="100" data-h="36"' in out
    assert 'data-sw="112" data-sh="76"' in out
    # the highlight overlay + frame wrapper are wired in JS/CSS
    assert "tv-hl" in out and "tv-shotframe" in out
    # …and the shot those frames are drawn onto is really there. `tv-hl` / `tv-shotframe` are static
    # strings the self-contained HTML embeds whether or not a screenshot resolved, so without this
    # the on-disk filter could empty the test unnoticed (review follow-up).
    assert 'class="shot"' in out and 'src="00-s1/step0/after.png"' in out


def _one_step_report(
    tmp_path: Path, artifacts: list[Artifact], *, missing: set[str] | None = None
) -> str:
    """Render a one-step report whose step recorded *artifacts* and one framed element.

    Every screenshot the step recorded is written to disk, since the report picks among the files
    that are actually there; name one in *missing* to model a run whose store lost it.
    """
    el = {**_el("home.cta", "Buy", ["button"]), "frame": (12.0, 40.0, 100.0, 36.0)}
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(index=0, action="tap", ok=True, started_at=0.0, artifacts=artifacts),
        ],
        expect_results=[],
        artifacts=[],
    )
    step_dir = tmp_path / "00-s1" / "step0"
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / "elements.json").write_text(json.dumps([el]), encoding="utf-8")
    for a in artifacts:
        if a.kind == "screenshot" and a.name not in (missing or set()):
            (tmp_path / a.name).write_bytes(b"PNG")
    return html_report("run1", [r], tmp_path)


def test_step_shows_the_post_action_screenshot(tmp_path: Path) -> None:
    # A step recording both screenshots shows `after.png` — in the steps-table thumbnail and, since
    # the element viewer reads that same `img.shot`, in the viewer beside the element table.
    # `elements.json` has one fixed name, so the always-on post-step `elements` capture leaves the
    # embedded tree describing the post-action screen; `before.png` would put a hovered element's
    # highlight on pixels from another moment.
    out = _one_step_report(
        tmp_path,
        [
            Artifact("00-s1/step0/before.png", "screenshot", "driver"),
            Artifact("00-s1/step0/elements.json", "elements", "driver"),
            Artifact("00-s1/step0/after.png", "screenshot", "driver"),
            Artifact("00-s1/step0/elements.json", "elements", "driver"),
        ],
    )
    assert 'class="shot" loading="lazy" src="00-s1/step0/after.png"' in out
    assert "before.png" not in out


def test_step_falls_back_when_the_recorded_after_png_is_not_on_disk(tmp_path: Path) -> None:
    # A report re-rendered from a stored run (`serve/artifacts.py`) can name a screenshot the store
    # no longer holds. Choosing it would emit a broken `<img>` and leave the element viewer with
    # nothing to draw frames on, so the choice is made among the files that are there — the same
    # filter the serve editor's picker applies (review follow-up).
    out = _one_step_report(
        tmp_path,
        [
            Artifact("00-s1/step0/before.png", "screenshot", "driver"),
            Artifact("00-s1/step0/elements.json", "elements", "driver"),
            Artifact("00-s1/step0/after.png", "screenshot", "driver"),
        ],
        missing={"00-s1/step0/after.png"},
    )
    assert 'class="shot" loading="lazy" src="00-s1/step0/before.png"' in out


def test_step_falls_back_to_the_pre_action_screenshot_when_no_after_png_exists(
    tmp_path: Path,
) -> None:
    # No `capture` list can suppress `after.png` any more, but one path still records none: a step
    # that fails before it acts (`UncoveredSystemAlertLocale`) returns before the shutter, and
    # nothing fills one in for it afterwards. It keeps showing the pre-step baseline's `before.png`,
    # which matches the pre-action tree recorded beside it. A run recorded before this change lands
    # here too.
    out = _one_step_report(
        tmp_path,
        [
            Artifact("00-s1/step0/before.png", "screenshot", "driver"),
            Artifact("00-s1/step0/elements.json", "elements", "driver"),
        ],
    )
    assert 'class="shot" loading="lazy" src="00-s1/step0/before.png"' in out


def test_step_draws_no_frames_when_its_screenshot_and_tree_describe_two_screens(
    tmp_path: Path,
) -> None:
    # A `web` block screenshots the native driver while its tree comes from the WebView, whose
    # frames are in the WebView's own coordinate space. The image is still shown — it is the only
    # picture of the step there is — but the screen extent is withheld, which is all `report.js`
    # needs to draw no frame (`tvHighlight` hides the box unless the extent is positive).
    out = _one_step_report(
        tmp_path,
        [
            Artifact("00-s1/step0/before.png", "screenshot", "driver", "adb:before"),
            Artifact("00-s1/step0/elements.json", "elements", "driver", "webview:before"),
            Artifact("00-s1/step0/after.png", "screenshot", "driver", "adb:after"),
            Artifact("00-s1/step0/elements.json", "elements", "driver", "webview:after"),
        ],
    )
    assert 'class="shot" loading="lazy" src="00-s1/step0/after.png"' in out
    assert "data-sw=" not in out
    assert "describe different screens" in out  # the tree button says why the frames are absent


def test_step_keeps_its_frames_when_the_pair_describes_one_screen(tmp_path: Path) -> None:
    out = _one_step_report(
        tmp_path,
        [
            Artifact("00-s1/step0/before.png", "screenshot", "driver", "adb:before"),
            Artifact("00-s1/step0/elements.json", "elements", "driver", "adb:before"),
            Artifact("00-s1/step0/after.png", "screenshot", "driver", "adb:after"),
            Artifact("00-s1/step0/elements.json", "elements", "driver", "adb:after"),
        ],
    )
    assert 'class="shot" loading="lazy" src="00-s1/step0/after.png"' in out
    assert 'data-sw="112" data-sh="76"' in out
    assert "describe different screens" not in out


def test_step_with_an_unreadable_tree_shows_the_screenshot_and_no_element_table(
    tmp_path: Path,
) -> None:
    # `elements.json` is written by the run, but a report can be re-rendered from a stored run whose
    # copy was truncated or replaced. Anything but a list of elements yields no embedded table (and
    # so no frames), rather than a half-rendered one.
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(
                index=0,
                action="tap",
                ok=True,
                started_at=0.0,
                artifacts=[
                    Artifact("00-s1/step0/after.png", "screenshot", "driver", "adb:after"),
                    Artifact("00-s1/step0/elements.json", "elements", "driver", "adb:after"),
                ],
            ),
        ],
        expect_results=[],
        artifacts=[],
    )
    step_dir = tmp_path / "00-s1" / "step0"
    step_dir.mkdir(parents=True)
    (step_dir / "elements.json").write_text('{"not": "a list"}', encoding="utf-8")
    (step_dir / "after.png").write_bytes(b"PNG")
    out = html_report("run1", [r], tmp_path)
    assert 'src="00-s1/step0/after.png"' in out
    assert "data-sw=" not in out
    assert 'class="treedata"' not in out


def test_step_whose_elements_have_no_extent_draws_no_frames(tmp_path: Path) -> None:
    # A tree can describe its screen and still place nothing on it: every frame zero-sized, or none
    # at all (a backend that reports no geometry). There is no extent to map percentages against, so
    # the viewer draws no frame — the same rendering an unpaired step gets, for a different reason.
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(
                index=0,
                action="tap",
                ok=True,
                started_at=0.0,
                artifacts=[
                    Artifact("00-s1/step0/after.png", "screenshot", "driver", "adb:after"),
                    Artifact("00-s1/step0/elements.json", "elements", "driver", "adb:after"),
                ],
            ),
        ],
        expect_results=[],
        artifacts=[],
    )
    step_dir = tmp_path / "00-s1" / "step0"
    step_dir.mkdir(parents=True)
    el = {**_el("home.cta", "Buy", ["button"]), "frame": (0.0, 0.0, 0.0, 0.0)}
    (step_dir / "elements.json").write_text(json.dumps([el]), encoding="utf-8")
    (step_dir / "after.png").write_bytes(b"PNG")
    out = html_report("run1", [r], tmp_path)
    assert 'class="treedata"' in out  # the element table is still there
    assert "data-sw=" not in out


def test_step_whose_screenshots_the_run_lost_says_nothing_about_two_screens(
    tmp_path: Path,
) -> None:
    # The manifest names both screenshots but the store holds neither, so the step shows its element
    # table with no image. Frames are absent because there is nothing to draw them on — not because
    # the image and the tree describe different screens — so the tree button must not claim that
    # reason (review follow-up).
    out = _one_step_report(
        tmp_path,
        [
            Artifact("00-s1/step0/before.png", "screenshot", "driver", "adb:before"),
            Artifact("00-s1/step0/after.png", "screenshot", "driver", "adb:after"),
            Artifact("00-s1/step0/elements.json", "elements", "driver", "adb:after"),
        ],
        missing={"00-s1/step0/before.png", "00-s1/step0/after.png"},
    )
    assert 'class="shot"' not in out
    assert 'class="treedata"' in out
    assert "describe different screens" not in out


def test_html_tree_falls_back_to_link_without_run_dir() -> None:
    # Structure-only render (no run_dir → no element data to embed): keep a link.
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(
                index=0,
                action="tap",
                ok=True,
                started_at=0.0,
                artifacts=[
                    Artifact("00-s1/step0/elements.json", "elements", "driver"),
                ],
            ),
        ],
        expect_results=[],
        artifacts=[],
    )
    out = html_report("run1", [r])
    assert 'href="00-s1/step0/elements.json"' in out


def test_view_data_picks_after_png_over_the_earlier_screenshot() -> None:
    # `screenshot.before` and `screenshot.after` both produce kind="screenshot", so the step carries
    # two artifacts of one kind. Position does not decide between them: `after.png` wins wherever it
    # sits in the list, including — as here — after the `before.png` the pre-step baseline wrote.
    r = RunResult(
        scenario="s1",
        ok=True,
        steps=[
            StepOutcome(
                index=0,
                action="tap",
                ok=True,
                started_at=0.0,
                artifacts=[
                    Artifact("00-s1/step0/before.png", "screenshot", "driver"),
                    Artifact("00-s1/step0/after.png", "screenshot", "driver"),
                ],
            ),
        ],
        expect_results=[],
        artifacts=[],
    )
    out = html_report("run1", [r])
    assert 'src="00-s1/step0/after.png"' in out
    assert 'src="00-s1/step0/before.png"' not in out
