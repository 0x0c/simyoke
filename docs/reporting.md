**English** · [日本語](ja/reporting.md)

# Reporting (manifest.json / JUnit / CTRF / HTML)

> One run executes one or more scenarios (`list[RunResult]`). Bajutsu writes their results in four
> formats. `manifest.json` is the **single source of truth** for the report and for CI (continuous integration).
>
> Implementation: `bajutsu/common/report/` (a package, split by stage: `format` → `manifest` / `richtext` → `rows` / `panels` → `html`).

Related: [the run results in run-loop](run-loop.md#run-results-data-structures) · [evidence](evidence.md)

---

## Output layout

```
runs/<runId>/
├── manifest.json     # the step → outcome correlation (single source of truth)
├── junit.xml         # CI integration (1 scenario = 1 testcase)
├── ctrf.json         # Common Test Report Format (richer CI consumers: PR comments, dashboards)
├── report.html       # self-contained HTML (no external assets)
└── <sid>/            # one scenario's evidence (when using FileSink)
    └── <stepId>/     # per-step evidence
        ├── before.png    # screenshot, before the step acts
        ├── after.png     # screenshot, after it acts
        ├── elements.json # query() dump
        ├── segment.mp4   # video (interval)
        └── device.log    # deviceLog (interval)
```

The CLI assigns `runId` as `YYYYMMDD-HHMMSS`. `bajutsu/common/run_meta/id.py`
([BE-0200](../roadmaps/BE-0200-run-id-contract/BE-0200-run-id-contract.md)) mints it once, so every
call site shares one format. `sid` is `{NN}-{slug}`: a zero-padded run-order index plus the stem of
the scenario's own source file (`login_flow.yaml` → `login_flow`), or a slug of the scenario's
`name:` field when no source file is known — a scenario built directly rather than loaded from disk
([BE-0417](../roadmaps/BE-0417-scenario-result-folder-naming/BE-0417-scenario-result-folder-naming.md)).
`stepId` is `step.name` or `step<i>`.

Bajutsu caps both of those slugs at 60 characters
([BE-0420](../roadmaps/BE-0420-scenario-slug-length-cap/BE-0420-scenario-slug-length-cap.md)). A
source-file stem keeps its Unicode letters, so the cap counts characters rather than bytes.
American Standard Code for Information Interchange (ASCII) text takes one byte per character.
Japanese text can take more. Counting characters, not bytes, keeps the two truncating to the same
length. A scenario whose file name or `name:` field runs past the budget still runs to completion.
Its evidence directory merely keeps the recognizable lead of the
name. `Scenario.name` stays untouched, so `manifest.json` and `report.html` carry the full name.
Truncation can make two long names produce the same slug. In `runs/<runId>/<sid>/` the run-order
index in front of the slug still tells them apart.

## manifest.json

`RunResult` and its parts are all dataclasses, so `manifest_dict` drops the step / expect results
verbatim — minus `wall_offset_s`, an in-run-only conversion constant with no meaning once persisted
(see below). Every other timestamp is an absolute wall-clock instant (epoch seconds), so it still
means the same thing after the run that produced it exits
(see [evidence](evidence.md#interval-evidence-video--devicelog--apptrace)).

```json
{
  "runId": "20260605-101530",
  "ok": true,
  "backend": "xcuitest",
  "scenarios": [
    {
      "scenario": "onboard, log in, and increment the counter",
      "ok": true,
      "backend": "xcuitest",
      "steps": [
        {
          "index": 5, "action": "tap", "ok": true, "reason": "",
          "duration_s": 0.12,
          "assertion_results": [],
          "artifacts": [
            { "name": "before.png", "kind": "screenshot", "provider": "driver",
              "depicts": "xcuitest:before" },
            { "name": "after.png", "kind": "screenshot", "provider": "driver",
              "depicts": "xcuitest:after" }
          ]
        }
      ],
      "expect_results": [
        { "ok": true, "kind": "value", "detail": "value equals='2': id='counter.value'", "reason": "" }
      ],
      "failure": null
    }
  ]
}
```

- `ok` (top): true if every scenario is ok.
- `backend`: the actuator that drove the run (`xcuitest`, or `fake` in tests). One actuator is fixed
  per run, so the top-level value is normally a single name; each scenario also carries its own
  `backend` ([drivers](drivers.md#backend-selection-and-the-actuator)).
- `steps[].duration_s`: each step's timing. A run measures every elapsed time on the monotonic
  clock. A wall clock can jump backward mid-run — a Network Time Protocol (NTP) correction, a manual
  clock change — and would corrupt a duration or a timeout, so a run reads the wall clock once per
  scenario and uses it for the recorded timestamps alone.
- `steps[].started_at`: the absolute wall-clock instant (epoch seconds) the step began, with no
  video correction folded in. A viewer subtracts `video_anchor_s` (below) at render time to get the
  seconds to seek the recording to. [report.html](#reporthtml) shows that number as the **steps**
  table's `at` column. Storing the raw instant, rather than the already-corrected offset, is what
  lets a viewer recompute the offset from a saved run — say, after a fix to how a run resolves its
  anchor — instead of forcing a re-run of the scenario
  ([BE-0348](../roadmaps/BE-0348-absolute-timestamp-recording/BE-0348-absolute-timestamp-recording.md)).
- `video_anchor_s` (per scenario): the absolute wall-clock instant the scenario's video started,
  measured from the finished recording's own duration where that duration is a wall-clock measure,
  and otherwise from the recording's confirmed start signal
  ([evidence](evidence.md#interval-evidence-video--devicelog--apptrace)). Every viewer subtracts it.
  A run recorded before `schemaVersion` 6 carries no anchor. Its `started_at` values are already
  video-relative, so a viewer reads the missing anchor as `0.0` and renders them unchanged.
- `wall_offset_s` (in-run only, not persisted): what the run adds to one of its own monotonic
  instants to reach a wall-clock epoch, so it can carry a network exchange's monotonic receive time
  onto the same anchor `video_anchor_s` uses. It never survives into the manifest — no monotonic
  instant does either, so a later reader would have nothing left to convert with it.
- `steps[].artifacts`: the provenance of evidence captured for that step
  ([evidence](evidence.md#artifact-provenance-provider)).
- `steps[].actuations`: what the driver actually did to the screen during that step — the coordinate a
  tap sent, the endpoints a swipe travelled, the channel that carried each gesture. This is the
  `actionLog` evidence kind, inherent in the manifest rather than a file
  ([evidence](evidence.md#actionlog--what-each-step-actually-did-to-the-screen)). A run recorded before
  `schemaVersion` 5 carries none. `expect_actuations` holds the same for the scenario-level `expect`
  re-check, where the system-alert guard can actuate with no step to attribute it to. From
  `schemaVersion` 7 a record may also carry `substitution`, naming why the element actuated is not
  the one the driver's default rule would have named; an older run carries none, which reads the
  same way as its absence today.
- `network.json`'s `startedAt` (one file per scenario, not shown in the manifest above): each
  observed exchange's absolute start, on the same footing as `steps[].started_at` and derived
  through the same scenario anchor, so a viewer subtracts `video_anchor_s` from both — see
  [report.html](#reporthtml) for how the two are interleaved into one timeline.
- `failure`: a summary on failure (e.g. `"step 3 (tap): no match: {...}"`). null on success.
- `provenance` (top, optional): a run-identity stamp ([BE-0049](../roadmaps/BE-0049-determinism-flakiness-audit/BE-0049-determinism-flakiness-audit.md))
  — `scenarioHash` (a `sha256:` fingerprint of the executed `scenario.yaml`), `toolVersion`
  (`bajutsu.__version__`), `gitRevision` (the commit, present only when the run is inside a git
  checkout), and — when the config came from a Git source ([BE-0063](../roadmaps/BE-0063-git-config-source/BE-0063-git-config-source.md)) —
  `configSource` (`{ host, owner, repo, ref, sha }`, the exact commit a branch-based run executed).
  It groups accumulated runs by identity, so a verdict that flips while the fingerprint is
  unchanged is **true flakiness** rather than an edited scenario. Pure metadata — it never enters
  `ok`. (`schemaVersion` is `3` or higher once this block can appear — it is `10` today.)
- `target` (top, optional): the target the run ran, so "the Android target passes while the iOS
  target fails" is computable from stored data ([BE-0404](../roadmaps/BE-0404-collapse-project-layer/BE-0404-collapse-project-layer.md)).
  One run resolves one target, so it sits beside `backend` rather than on each scenario. `serve`
  mirrors it onto the run row and ranks targets against each other from it. Omitted when the run
  named no target. (`schemaVersion` is `10` or higher once this key can appear.)
- `label` (top, optional): the run-history partition — the bound config's own name, or the operator's
  `run --label` override. Opaque metadata: never parsed, never matched against config, and never an
  input to `ok`. It is what keeps two configs' runs readable apart after a `serve` restart. Omitted
  when the run carried no label. (`schemaVersion` is `10` or higher once this key can appear.)
- `idb` (top, optional, legacy): older manifests may carry an `idb_companion` / client version
  block (BE-0005). It was retired with the idb backend (BE-0290) and is no longer written; an old
  manifest that still has it loads fine, since an unknown top-level key is ignored.
- `matrix` (top, optional): the cross-browser engine × scenario grid, present only on a
  `bajutsu run --browsers` run ([BE-0076](../roadmaps/BE-0076-web-cross-browser-engines/BE-0076-web-cross-browser-engines.md)).
  `scenarios` stays the flat result list, each entry tagged with its `engine`; `matrix` is
  `{ engines, scenarios, cells: { "<scenario>": { "<engine>": { ok, sid, failure } } } }` — a pure
  aggregation of those per-engine verdicts (the report renders it as a grid). `ok` is all-must-pass
  across every engine × scenario. Omitted for a single-engine / iOS run. Nothing enforces a unique
  `name:` across a multi-file run, so a second scenario sharing an earlier one's name gets its own
  row labeled with a `(2)`, `(3)`, … suffix rather than overwriting the first's cell. (`schemaVersion`
  is `4` or higher once this block can appear.)

## junit.xml

For CI integration. **one scenario = one `<testcase>`.** A failing scenario gets a `<failure>`, whose
`text` lists each step / expect's ok/FAIL and reason. On a `--browsers` matrix run the engine is
keyed into the case (`classname="bajutsu.<engine>"`), so CI sees `chromium.login` and `webkit.login`
as distinct cases (BE-0076); a single-engine run keeps `classname="bajutsu"`.

```xml
<testsuite name="bajutsu" tests="2" failures="1">
  <testcase name="..." classname="bajutsu"/>
  <testcase name="..." classname="bajutsu">
    <failure message="step 1 (tap): ...">step 0 tap: ok
step 1 tap: FAIL no match: {...}</failure>
  </testcase>
</testsuite>
```

## ctrf.json

The [Common Test Report Format (CTRF)](https://ctrf.io/) export ([BE-0161](../roadmaps/BE-0161-ctrf-report-export/BE-0161-ctrf-report-export.md)):
an open-standard JSON test report that a growing ecosystem reads without per-tool adapters — the
`ctrf-io` GitHub Actions (PR-comment / job-summary reporters), cross-tool dashboards, and
flaky-test analytics. Where JUnit XML strips a run down to name / time / a failure blob, CTRF carries
Bajutsu's structured detail (per-step outcomes, the engine and device, artifacts as first-class
attachments) that JUnit has no place for. It is a **pure projection of `manifest.json`** — the same
data, a new shape beside `junit.xml` — so it adds no bookkeeping, and, written after the verdict,
it cannot change that verdict (no LLM, no effect on pass/fail).

The document is `{ reportFormat: "CTRF", specVersion, generatedBy, timestamp, results }`, where
`results` holds `tool` / `summary` / `tests` (+ optional `environment` / `extra`):

```json
{
  "reportFormat": "CTRF",
  "specVersion": "0.0.0",
  "generatedBy": "bajutsu",
  "results": {
    "tool": { "name": "bajutsu", "version": "…" },
    "summary": { "tests": 2, "passed": 1, "failed": 1, "skipped": 0, "pending": 0, "other": 0,
                 "start": 1717581300000, "stop": 1717581302300, "duration": 2300 },
    "tests": [
      { "name": "login", "status": "passed", "duration": 1500,
        "steps": [{ "name": "tap", "status": "passed", "extra": { "duration": 500 } }],
        "browser": "chromium", "device": "iPhone 15 (iOS 17.2)",
        "attachments": [{ "name": "00-login/scenario.mp4", "contentType": "video/mp4", "path": "00-login/scenario.mp4" }] }
    ]
  }
}
```

- `summary.duration` and each `tests[].duration` are milliseconds (Σ / per-scenario `duration_s`),
  the field CTRF consumers key on, and exact. `summary.start` and the document `timestamp` derive
  from the `YYYYMMDD-HHMMSS` runId, parsed as UTC (the runId is stamped in UTC); `stop = start +
  duration`. Absolute per-test start/stop are deferred (they need an absolute per-scenario epoch, an
  optional follow-up) so they are omitted rather than approximated. The export carries no live host
  state, so `bajutsu report` regenerates the same run's `ctrf.json` byte-for-byte.
- `tests[].status` is `passed` / `failed` — the only two states a Bajutsu run emits; the other CTRF
  counts stay `0`.
- A CTRF `step` allows only `{ name, status, extra }`, so a step's richer data (duration, reason,
  per-step assertions, artifacts) lands in `step.extra` — a consumer that renders just name/status
  sees a clean list, and Bajutsu-aware tooling can read the extras.
- Attachment `contentType` comes from an artifact-`kind` → MIME map (`video`→`video/mp4`,
  `screenshot`→`image/png`, `deviceLog`→`text/plain`, `elements`/`network`/`appTrace`→`application/json`),
  defaulting to `application/octet-stream`; `path` stays run-directory relative like the manifest.
- On a `--browsers` matrix run each engine × scenario cell is one CTRF test — the engine in the test
  `name` and the `browser` field (mirroring JUnit's `classname`) — and the engine × scenario grid is
  carried under `results.extra.matrix`. Bajutsu's other surplus (`sid`, `expect` results, alerts,
  `skipped_captures`) lives under the per-test `extra`.
- Since CTRF is projected from the already-redacted manifest, it inherits the same secret scrubbing
  ([BE-0047](../roadmaps/BE-0047-ai-data-sovereignty/BE-0047-ai-data-sovereignty.md)); no raw secret reaches it.

### Consuming ctrf.json in CI

`ctrf.json` sits beside `junit.xml`, so wiring it into a CI job is a single consumer step. For
example, the `ctrf-io/github-test-reporter` action turns it into a PR comment / job summary:

```yaml
- uses: ctrf-io/github-test-reporter@v1
  with:
    report-path: runs/*/ctrf.json
  if: always()
```

## report.html

A self-contained HTML for humans (inline CSS, no external assets). The header shows the run id and
overall PASS/FAIL. Below that sits the **scenario file name** (`source_name`), the whole suite's
label. `source_name` names the whole suite. A lone `--scenario` file gives its own name, and a
multi-file or whole-dir run gives the containing directory's name instead. The **file-level
`description`** follows when present.

Each scenario row's summary shows the **scenario name**. It also shows the scenario's **own
originating file name** (`source_files`). This name is distinct from the header's whole-suite
label. A `bajutsu run` that read the scenario off disk sets `source_files`; an offline re-render
does not. When set, the **scenario-level `description`** sits beside the name. A run thus surfaces
scenario name, file name, and description throughout.

Bajutsu merges the scenario definition and its execution into one **Steps tab**. It has labelled sections (preconditions / **steps** /
**expectations**), each a table. The **steps** table: `#` / `result` (a PASS/FAIL pill in its own
column) / `action` (a colored badge) / `detail` (the target description) / `at` / `view` (screenshot +
an **in-report element-tree viewer**: the captured elements open in an in-page overlay, no new tab) /
`reason`. The `#` column also carries the step's own line number in the scenario file when
available. For example, it can read `2:15`. A `setup`/component expansion that changed the step
list's shape drops it instead. A wrong line would mislead more than none. The `at` cell also shows
the step's own elapsed time in parentheses, at the cell's end. For example, `3.2s (1.1s)`. That
parenthetical shows whenever the duration rounds to something other than 0.0s. A slow `wait` or a
sluggish gesture is then visible without opening the recording. The screenshot shown is the post-action `after.png`
([evidence](evidence.md#interval-evidence-video--devicelog--apptrace)). Every step that acts records
one. A step that fails before acting records none, and shows its `before.png` instead. Hovering an
element in the viewer highlights its frame on the screenshot. That frame comes from `elements.json`,
which a step re-reads after acting. Frame and pixels then describe the same moment. One
exception: a non-mutating step (`assert`, `wait`) reuses the tree it settled on (BE-0259). That tree
precedes the screenshot rather than following it. The serve editor's element picker resolves a
step to the
same image, for the same reason. Where the image and the tree describe *different* screens — a
[`web` block](scenarios.md#web-entering-a-webviews-dom)'s native screenshot beside its WebView tree,
or a stored run that has lost its `after.png` — the viewer shows both and draws no frames, and the
tree button says why ([evidence](evidence.md#which-screen-an-artifact-shows-depicts)). The serve
editor offers no image for such a step at all, since every click there would resolve to an element
that was never at those pixels. In the detail, identifiers (`#home.title`) and literal constants (`“text”`,
numbers) are rendered as subtly-styled inline tokens — visually distinct from the solid
action/assert badges, so variables and constants are distinguishable at a glance. An `assert` step's
checks become a **nested table**, one row per assertion split into `kind` / `target` / `comparison`
cells (instead of a hard-to-read `a; b; c` line). Steps that never ran (execution stops at the first
failure) still appear, marked as skipped. **Observed network exchanges are interleaved into the
steps** in time order (each placed by its offset from the scenario start): a row with the HTTP method
as a neutral badge, the status in the `result` column, and the exchange's settings (method / endpoint
/ status / duration / headers) as a **nested table** in the detail cell. The scenario's `network.filter.domains` (by URL host) filters which requests appear; the Network tab still lists them all.
The **preconditions** table is collapsible (key / value).
The **expectations** table uses parallel columns `result` / `kind` (badge) / `target` (the checked
selector, e.g. `#counter.value`) / `comparison` (e.g. `== “2”`) / `reason`, with the same id/constant
tokens. A **Rich / YAML toggle** switches the same tab between this structured view and the raw
scenario YAML. For a freshly baked `bajutsu run` report, that YAML is the scenario's own text, as
authored. Comments and formatting stay intact: the report slices this text straight from the
source file. It never re-serializes the parsed model. A literal `totp.secret` still gets masked in
place. A scenario whose `setup`/component expansion changed its step count keeps this verbatim
text — still accurate as authored — but loses the `#` column's line numbers, which the expansion
would leave pointing at the wrong steps. A `data`/`dataFile`-driven scenario always shows the
structured re-dump instead. It never shows the verbatim text. Every row shares one authored
template. Substituting `${row.*}` per row leaves the step count unchanged. A verbatim slice would
show the same unsubstituted template for every row. It would never show what that row actually
ran. An offline re-render (`bajutsu report`, below) falls back to the structured re-dump it always
used: the original file's raw text was never persisted into the run.

A `visual` expectation renders an **interactive baseline-vs-actual comparator** beneath its row,
with four modes: **Swipe** (drag a divider to wipe between the two), **Onion** (a slider cross-fades
actual over baseline), **Blend** (`mix-blend-mode: difference` — identical pixels go black, changed pixels are highlighted),
and **Diff** (the machine's precomputed pixel diff, with the assertion's `exclude`
regions masked — present only when the check failed). A `diff <pct>%` badge accompanies it, or a
`no baseline yet` badge on a first run (when only the actual screenshot exists). When the check did
not pass, an **Approve as baseline** button promotes the captured screenshot into the baselines dir;
it `POST`s `/api/approve` and so works only when the report is opened through `bajutsu serve` (it is
hidden for a report opened from disk). The CLI twin is [`bajutsu approve`](cli.md#approve).

Failing rows have a red background. Clicking a step seeks the recording to that step **without
auto-playing** (a paused video stays paused; a playing one keeps playing) — the seek target is the
step's `started_at` minus `video_anchor_s` (the **steps** table's `at` column). That anchor is the
recording's own measured origin, or its best-known real start where no measurement was possible —
not the raw moment the scenario's step loop began — so the seek lands on what the row shows
([evidence](evidence.md#interval-evidence-video--devicelog--apptrace)). One visible consequence: for
a video-capturing Android or web scenario, that derived seconds-into-the-recording value can exceed
the scenario's own `duration_s`, because the two measure different things on purpose — the video's
timeline starts before the run's own step loop does, while `duration_s` measures the loop itself.
That is expected, not a sign either number is wrong. Clicking a step's
screenshot opens a full-size lightbox; **← / →** (or the on-screen arrows) then walk through every
screenshot in the run, across scenario boundaries, with a caption showing the scenario, step, and
position. The run's actuator backend is shown as a `driver: <backend>` chip in the header and a small
badge on each scenario row.
Device Log / App Trace remain separate tabs.

## Write API

```python
def write_report(run_dir, run_id, results, definitions=None, sources=None, source_name=None, description=None, provenance=None, writer=None, target=None, label=None, source_files=None, step_lines=None) -> Path  # all 4 formats; definitions = per-scenario dict, sources = raw YAML, source_name = scenario file name (whole suite), description = file-level description; source_files / step_lines = each scenario's own file name and steps' original line numbers, when a live run recovered them; provenance = run-identity stamp (BE-0049)
def write_html_and_junit(run_dir, run_id, results, definitions=None, sources=None, source_name=None, description=None, provenance=None, writer=None, source_files=None, step_lines=None) -> None  # the regenerable half (report.html + junit.xml + ctrf.json), leaving manifest.json untouched — used by re-render; provenance feeds the CTRF tool/environment fields
def manifest_dict(run_id, results, *, source_name=None, provenance=None, target=None, label=None) -> dict  # the versioned render model (schemaVersion); the manifest source (for tests / inspection)
def run_provenance(scenario_yaml, *, git_revision, config_source=None) -> dict  # the run-identity stamp: scenarioHash + toolVersion + optional gitRevision (BE-0049) + optional configSource (BE-0063)
def ctrf_json(run_id, results, *, provenance=None) -> dict  # the CTRF projection of the result model (BE-0161); provenance feeds tool.version / environment.commit
def junit_xml(results) -> str
def html_report(run_id, results, run_dir=None, definitions=None, sources=None, source_name=None, description=None, source_files=None, step_lines=None) -> str
def scenario_render_inputs(scenarios, plan_sources=None) -> tuple[list[dict], list[str]]  # (definitions, sources); shared by the bake and the re-render — `sources` is each scenario's own verbatim YAML when `plan_sources` (keyed by declared scenario name) recovers one, else the structured re-dump as before
def scenario_source_meta(scenarios, plan_sources=None) -> tuple[list[str | None], list[list[int] | None]]  # (source_files, step_lines), aligned with `scenario_render_inputs`'s `sources`
```

`runner.run_and_report` calls this `write_report` and returns `(results, manifest_path)` to the CLI
([run-loop](run-loop.md#runner-the-run-pipeline)). The CLI exits 0 if every scenario passes, 1 on
failure.

## Regenerating a report (BE-0068)

The report is a **pure rendering of data stored in the run dir**, so a finished run can be
re-rendered offline with the current template — without re-executing it. `manifest.json` is the
**versioned** (`schemaVersion`), lossless render model; `report.load` is its inverse —
`results_from_manifest()` reconstructs the `RunResult`s, and `load_run(run_dir)` recovers the whole
render model (outcomes from `manifest.json`, the scenario plan from `scenario.yaml`). `bajutsu
report <run>` ([cli](cli.md#report)) rewrites `report.html` + `junit.xml` + `ctrf.json` from it. Re-rendering only
re-presents recorded outcomes — it never re-runs an assertion or changes a verdict — and an older
run renders with any newer-only section shown as "not captured" rather than invented.
