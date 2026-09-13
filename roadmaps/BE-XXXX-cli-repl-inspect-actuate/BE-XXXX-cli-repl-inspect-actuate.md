**English** · [日本語](BE-XXXX-cli-repl-inspect-actuate-ja.md)

# BE-XXXX — Interactive REPL for element-tree inspection and id-based actuation

<!-- BE-METADATA -->
| Field | Value |
|---|---|
| Proposal | [BE-XXXX](BE-XXXX-cli-repl-inspect-actuate.md) |
| Author | [@0x0c](https://github.com/0x0c) |
| Status | **Proposal** |
| Tracking issue | [Search](https://github.com/bajutsu-e2e/bajutsu/issues?q=is%3Aissue+label%3Aroadmap-tracking+in%3Atitle+"BE-XXXX") |
| Topic | Authoring experience |
<!-- /BE-METADATA -->

## Introduction

`bajutsu repl` opens a read-eval-print loop (REPL): a manual command shell against a running
target. An operator launches the
app once, then types one command at a time. `tree` reads the current screen's element tree.
`tap <id>` acts on one of its elements. The operator reads each result before choosing the next
command. The shell sits beside `record` (goal-directed AI authoring) and `crawl` (autonomous
exploration) as a
third way to reach a target through the same
[`Driver`](../../docs/glossary.md#driver-backend-actuator-platform) interface those two commands
already use. Unlike `record` and `crawl`, `repl` asks no large language model (LLM) anything and
writes no scenario. `repl` is a thin loop over `query()` and the actuation methods every
[backend](../../docs/glossary.md#driver-backend-actuator-platform) already implements, so
XCUITest, adb, and Playwright gain the shell for free with no per-backend read or actuation
code — only the launch and exit paths carry branches beyond that shared surface (see *Detailed
design*).

## Motivation

Finding out which [selector](../../docs/glossary.md#scenario-authoring) will resolve to which
element costs more than the question deserves today. An operator can read the tree by eye in
Xcode's Accessibility Inspector or in a browser's devtools. That reading is backend-specific.
It also skips the normalized `id` / `label` / `traits` fields a scenario step actually matches
against. `bajutsu doctor` reads the live screen from the command line already, but it scores
convention (id coverage, off-namespace ids) against a driver it probes once and tears down; it
never lists the tree for reading, and it cannot act on an id. Inside Bajutsu, `record` and `crawl`
both drive the app through an AI call, and both produce output shaped for their own artifact — a
scenario, a screen map — rather than one query answered right away. The `serve` web UI's Author
view comes closer: its `/api/capture/start` and `/api/capture/resolve` endpoints boot a live
driver and turn a click on its live screenshot into a selector with no AI call
([BE-0262](../../roadmaps/BE-0262-serve-author-live-step-picker/BE-0262-serve-author-live-step-picker.md)).
That picker still runs inside a browser tab, built for choosing one step to add to a scenario, and
it never answers whether a typed id resolves at all. None of these gives a direct, backend-agnostic
answer from a command line to one question: what does the current screen look like, and what
happens when one of its ids is tapped.

`repl` answers that question directly. `tree` prints the current element tree. `tap <id>` acts on
one of its elements. A second `tree` shows what changed, with no AI round trip, no scenario file,
and no per-backend inspector to learn. A selector that fails to match during authoring is a
common outcome: the id carries a typo, another element covers it, or it appears after a wait.
Diagnosing that today means writing a candidate scenario step, running it, and reading the
manifest's captured tree afterward. `repl` shortens that loop to typing `tree` and `tap` against a
running app. Once this ships, an operator launches `bajutsu repl --target <name>`, runs `tree`,
reads the visible elements' ids, taps one of them, and watches the screen change. That loop takes
seconds, in place of a run-and-read-the-report cycle.

## Detailed design

`bajutsu repl --target <name> [--udid <id>] [--backend <list>] [--erase/--no-erase]
[--headed/--no-headed] [--browser <engine>] [--config <path>]` launches the app. Target
resolution and backend selection reuse the same shared CLI helpers `record` already calls —
`_load_effective_with_source` and `_select_actuator_or_exit`
(`bajutsu/cli/_shared.py`) — and device bring-up reuses `launch_driver`
(`bajutsu/common/runner/launch.py`), the same combination `record` and `crawl` use to resolve a
`udid` (skipped for the `playwright` actuator) and boot the device before handing off a driver.
Ahead of that launch, `repl` brings the target's own server up where the config declares
`launchServer`, through the `_start_launch_server_or_exit` helper (`bajutsu/cli/_shared.py`) that
`run`, `record`, `crawl`, and `audit` already share, and stops it on exit through `atexit` the way
`record` and `crawl` do. Without that step a web target serving its `baseUrl` from `launchServer`
opens the browser on a host that is not listening, and every `tree` reads the error page.
`--headed`/`--no-headed` and `--browser` are web-only. Both reuse a shared helper from
`bajutsu/cli/_shared.py`: `_with_headed`, which `record`, `crawl`, and `run` already call, and
`_resolve_browser`, which `record` and `run` call (`crawl` exposes no `--browser`). They matter for
this shell in particular, because a headless browser leaves an operator with no screen to watch
change. On launch, `repl` prints the resolved backend and target, then a `bajutsu>` prompt.

The v1 command set stays small and id-first:

| Command | Behavior |
|---|---|
| `tree [--json]` | `driver.settled_query()` when the driver implements `SettledReadProvider` (adb), else `driver.query()` — rendered as a table of `id` / `label` / `traits` / `value` / `frame` (or as JSON) |
| `find <substring>` | the same tree, filtered to rows whose `id` or `label` contains `<substring>` |
| `tap <id>` | `driver.tap({"id": "<id>"})` |
| `type <id> <text>` | tap `<id>` to focus it, then `driver.type_text("<text>")` |
| `back` | `driver.back()` |
| `screenshot [path]` | `driver.screenshot(path)`, auto-named when `path` is omitted |
| `help` | list the commands above |
| `exit` / `quit` | leave the shell; a Simulator- or device-backed app (local `xcuitest`, `adb`) keeps running, while any session this process itself owns is closed through the same call its `Environment.teardown` makes — the web backend's browser (`cast(base.BackendLifecycle, driver).close()`, as `WebEnvironment.teardown` does; `Driver` itself declares no `close()`) and, on the `--udid https://…` live route, the WebDriver session `XcuitestLiveEnvironment.teardown` deletes, which would otherwise stay reserved on the grid until it expires |

Every command that reads or resolves against the tree asks for an actuation-grade read the same
way `run`'s own handlers do: through `SettledReadProvider.settled_query()`
(`bajutsu/common/drivers/base/settled_read_provider.py`) where a driver implements it, or a plain
`query()` where a driver's own reads are already good enough to actuate from. The read-lag barrier
([BE-0332](../../roadmaps/BE-0332-read-lag-barrier/BE-0332-read-lag-barrier.md)) is what makes
`settled_query()` safe on adb: it keeps a `tree` issued right after a `tap` from reading a stale,
pre-actuation snapshot. `resolve_unique`'s existing zero-or-many-matches contract fails a command
right away with the same `ElementNotFound` / `AmbiguousSelector` message `run` would raise. Neither
path guesses which element the operator meant (prime directive 2 — determinism first). `tap` calls
`driver.tap()` directly rather than `run`'s own `_tap_with_recovery`
(`bajutsu/common/orchestrator/actions/handlers/gestures.py`), so a target another element covers
raises `ElementNotTappable` in `repl` where `run` would first retry a bounded scroll and succeed —
a deliberate v1 gap, not a bug to route around silently (see *Alternatives considered*).

`tap` and `type` address an element by `id` alone. That is narrower than the full
[selector](../../docs/glossary.md#scenario-authoring) syntax `run` accepts (`id`, `idMatches`,
`label`, `labelMatches`, `traits`, `value`, `within`, `index`), on purpose: it keeps this first
version small enough to review, and it matches how the shell gets used. `tree` already shows every
element's `label` and `traits`, so an operator reads the row and types its id. An element with no
`id` can still be addressed by `label` or `traits` in a full selector, but `repl`'s id-only v1
cannot reach it. A control genuinely absent from the tree — most often an individual tab in a
no-id app's tab bar — is a separate problem `record`'s vision fallback exists to close
(`docs/recording.md`), and `repl` does not attempt it: adding vision would put an AI call back into
a tool built to avoid one. Extending `tap` and `type` to the rest of `Selector`'s fields
(`idMatches`, `label`, `labelMatches`, `traits`, `value`, `within`, `index`) is a natural follow-up,
scoped separately, once the shell itself has shipped.

Gestures (`swipe`, `scroll`, `pinch`, `rotate`) and platform-specific actions (`set_picker_value`,
`select_option`) stay out of v1. `tap`, `type_text`, `back`, and `screenshot` cover what an
operator reaches for first when confirming a selector or walking a flow by hand. The remaining
`Driver` methods are plain additions once the command-parsing shape is settled. Adding every one
of them at once would widen this item past a single reviewable change.

`repl` lives in its own top-level package, `bajutsu/repl/`, beside `record/` and `crawl/` (a row
for it joins `docs/architecture.md`'s module table, so `make lint-module-map` keeps passing). Its
command parsing, `tree` / `find` rendering, and `ElementNotFound` / `AmbiguousSelector` /
`ElementNotTappable` surfaces get `FakeDriver`-backed tests in the fast suite, the same way `run`'s
own handlers are tested, so the new module clears the per-file coverage floor
(`coverage-floors.json`) from its first PR rather than needing a follow-up.

## Alternatives considered

- **Read the tree with a platform-native tool** (Xcode's Accessibility Inspector, browser
  devtools). Rejected: each tool is backend-specific, so the answer it gives does not come through
  the one backend-agnostic [`Driver`](../../docs/glossary.md#driver-backend-actuator-platform)
  seam the rest of Bajutsu reads the tree with. None of them shows the normalized `id` / `label` /
  `traits` fields a Bajutsu selector matches against, so an id read there is not guaranteed to be
  the id `run` would resolve.
- **Use `serve`'s Author live step picker instead of a new command.** Rejected for this item:
  `/api/capture/start` / `/api/capture/resolve`
  ([BE-0262](../../roadmaps/BE-0262-serve-author-live-step-picker/BE-0262-serve-author-live-step-picker.md))
  already boot a live, AI-free driver and turn a click on its screen into a selector, but through a
  browser picker built for choosing one step to add to a scenario. It never answers whether a typed
  id resolves, and it leaves no path for acting on one directly from a terminal, with no browser
  tab and no editor open — the narrower need this item exists for.
- **Route `repl`'s `tap` through `run`'s `_tap_with_recovery`.** Rejected for v1: surfacing the
  driver's own `ElementNotTappable` is the more useful answer while diagnosing a selector, though
  what it names varies by backend — adb, the live XCUITest route, and `FakeDriver` name the covering
  element through `base.raise_if_covered` and the web backend names it through its own hit result,
  while the XCUITest Simulator driver reports only `element resolved but not hittable`; an operator
  who wants the recovery writes the explicit `scroll` step in the scenario and taps there. The cost
  is that `repl` reports a failure where `run` would recover, so the two can disagree on a covered
  target — the gap *Detailed design* calls out.
- **Add a "manual mode" flag to `record` instead of a new command.** Rejected: `record`'s loop is
  built around `ClaudeAgent` proposing actions from a screenshot, and it always ends by writing a
  scenario. Bolting a human-typed command path onto that loop would tangle an AI-driven path and a
  non-AI one in one module. `repl` never writes a scenario, on purpose, so a separate command
  keeps both simpler to read.
- **Accept a full selector (`label`, `labelMatches`, `index`) for `tap`/`type` from the start.**
  Rejected for v1 in favor of id-only addressing (see *Detailed design*). The smaller surface
  already answers the question behind this item, and it lands as one reviewable change rather
  than one that also has to settle selector-syntax parsing on a command line.
- **A non-interactive mode that reads commands from stdin or a file.** Rejected for this item. The
  interactive shell alone proves the command set before any scripting surface gets built on top
  of it. A batch mode is a follow-up, scoped separately, once that command set is settled.

## Progress

> Keep this current as work proceeds. The checklist mirrors the MECE work breakdown in
> *Detailed design* (one box per unit of work); the log records what changed and when
> (oldest first), linking the PRs.

- [ ] `bajutsu repl` command scaffold under the new `bajutsu/repl/` package:
  `_load_effective_with_source` / `_select_actuator_or_exit` / `_start_launch_server_or_exit` /
  `launch_driver` reuse (with the launch server stopped on exit via `atexit`),
  `--headed`/`--no-headed`/`--browser`, the `bajutsu>` prompt loop, `help` / `exit` / `quit`
  (including `cast(base.BackendLifecycle, driver).close()` on the web backend's exit and
  `XcuitestLiveEnvironment.teardown`'s WebDriver-session close on the `--udid https://…` live
  route).
- [ ] `tree` / `tree --json` / `find <substring>`, reusing `settled_query()` / `query()` and the
  read-lag-barrier path.
- [ ] `tap <id>` / `type <id> <text>`, surfacing `ElementNotFound` / `AmbiguousSelector` /
  `ElementNotTappable` the same way `run` does.
- [ ] `back` / `screenshot [path]`.
- [ ] `FakeDriver`-backed tests in the fast suite: command parsing, `tree` / `find` rendering, and
  the `ElementNotFound` / `AmbiguousSelector` / `ElementNotTappable` surfaces.
- [ ] `docs/cli.md` and `docs/ja/cli.md` reference sections, plus the CLI inventories `repl` makes
  stale — the CLI-verbs table in `docs/glossary.md` (and `docs/ja/glossary.md`), the command
  lists in `docs/architecture.md` ([BE-0113](../../roadmaps/BE-0113-design-doc-realignment/BE-0113-design-doc-realignment.md)),
  and `bajutsu/repl/`'s own row in `docs/architecture.md`'s module table (`make lint-module-map`).

## References

- [`Driver`](../../docs/glossary.md#driver-backend-actuator-platform) protocol —
  `bajutsu/common/drivers/base/driver.py`
- [`Selector`](../../docs/glossary.md#scenario-authoring) — `bajutsu/common/scenario/models/selector.py`
- `SettledReadProvider` — `bajutsu/common/drivers/base/settled_read_provider.py`
- `BackendLifecycle` — `bajutsu/common/drivers/base/backend_lifecycle.py`
- `_start_launch_server_or_exit` — `bajutsu/cli/_shared.py`
- `XcuitestLiveEnvironment` — `bajutsu/common/platform_lifecycle/environments/xcuitest_live.py`
- `record` and `crawl` — the two existing Tier 1 authoring paths `repl` sits beside (`docs/cli.md`)
- [BE-0332 — read-lag barrier](../../roadmaps/BE-0332-read-lag-barrier/BE-0332-read-lag-barrier.md)
- [BE-0262 — live step-picking and target-scoped runs in the Author editor](../../roadmaps/BE-0262-serve-author-live-step-picker/BE-0262-serve-author-live-step-picker.md)
