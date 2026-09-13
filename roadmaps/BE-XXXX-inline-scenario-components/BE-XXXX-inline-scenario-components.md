**English** · [日本語](BE-XXXX-inline-scenario-components-ja.md)

# BE-XXXX — Inline, scenario-local components

<!-- BE-METADATA -->
| Field | Value |
|---|---|
| Proposal | [BE-XXXX](BE-XXXX-inline-scenario-components.md) |
| Author | [@0x0c](https://github.com/0x0c) |
| Status | **Proposal** |
| Tracking issue | [Search](https://github.com/bajutsu-e2e/bajutsu/issues?q=is%3Aissue+label%3Aroadmap-tracking+in%3Atitle+"BE-XXXX") |
| Topic | Scenario authoring features |
<!-- /BE-METADATA -->

## Introduction

Let a scenario file declare an inline `components:` block. Each entry maps a name to a BE-0030
component: a reusable, parameterized step sequence, scoped to that one file. A `use: { component:
<name> }` step in the same file resolves the name directly. A step block reused inside a single
file then needs no separate component file. BE-0030's cross-file `use: { component: <path> }` keeps
working unchanged and still owns reuse that spans a suite.

## Motivation

BE-0030 defines a *component*: a reusable, parameterized sequence of steps. A `use` step expands it
into the caller's step list before the run. A component always lives in its own file: a `use: {
component: <path> }` step resolves that path. Extracting even a short, three-step block still needs
a new file, a name for it, and a relative path from the caller. That fixed cost is the same whether
the component ends up called from one scenario file or from every file in the suite.

That cost is not proportional to the reuse it buys when the reuse stays inside one file. A file that
defines two or three related scenarios often repeats one short step block across those scenarios and
nowhere else — a search box tried with different queries, a form submitted with different field
combinations. Splitting that block into its own file adds a file a reader must open to see what a
`use` step does, for a component called from a single file. An author facing that trade instead
repeats the steps by hand in each scenario: the same duplication BE-0030 already set out to remove
for the cross-file case.

An inline, file-scoped `components:` block removes that trade for the local case. An author defines
the component next to the scenarios that call it, in the same file. A name only one file uses then
adds no entry to the suite's directory listing. Take a scenario file that defines
a component under `components:` and calls it with `use: { component: <name> }` from two scenarios in
that file. At load time it expands to the same step list a hand-duplicated version would produce.
Reading the expanded steps with `bajutsu trace --explain` confirms it, and no second file appears
for the reuse.

## Detailed design

A file-scoped sibling of BE-0030's file-based component. It resolves through the same `use` step and
the same `expand_components` macro (`bajutsu/common/scenario/expand.py`). The run loop still sees
plain, fully-expanded steps alone, so determinism holds.

- **A new `components:` field on the scenario file.** `ScenarioFile`
  (`bajutsu/common/scenario/models/scenario/scenario_file.py`) gains `components: dict[str,
  Component]`, defaulting to empty. Each entry is the same `Component` model BE-0030 already
  validates a standalone component file against (`params` + `steps`). One schema serves both
  forms. A scenario file itself has two on-disk forms (`docs/dsl-grammar.md` §2): a bare list of
  scenarios, or a `{description, scenarios}` mapping. The mapping form alone can carry
  `components:`, so an author declaring one converts a bare-list file first.
- **A name resolves in the file; a path resolves as a file; the two never collide.** `use: {
  component: <ref> }` keeps a single field. The ref's own shape decides how it resolves. A ref
  containing `/` or ending in `.yaml` / `.yml` resolves as a file, the same way BE-0030 already
  resolves it. A bare name with neither marker looks up that key in the current file's own
  `components:` map instead, while expansion is still inside that file's own scope (the next two
  bullets say exactly when that holds). The two forms read as distinct on sight, so a scenario
  file's file-scoped names and a suite's file-based components share the `use` step. This needs
  neither a new field nor a naming convention.
- **A `ComponentResolver` object, so `bajutsu run` and the device-free tools agree.** Two loaders
  expand a scenario file today. `load_expanded_scenarios`
  (`bajutsu/common/scenario/load_expanded.py`) is what `trace --explain` / `audit` / `coverage` /
  `impact` and serve all use; `run/cli.py`'s `_expand_file` builds its own separate `resolve` lambda
  for the deterministic `run` gate. A
  new `ComponentResolver`, factored into `load_expanded.py` (not `expand.py`, which stays
  filesystem-free by design), becomes the one place that builds a `resolve` callable bound to a local
  `components:` map, a `root`, and a `base` directory; it owns the bare-name-vs-path shape test and
  its own per-ref cache, and a sibling method, `for_component_file()`, returns a new
  `ComponentResolver` bound to an empty map for expanding a path-resolved component's own steps.
  `expand_components`'s own `resolve: Callable[[str], Component]` parameter is unchanged — a
  `ComponentResolver` instance simply satisfies it. Both loaders build one and pass it in, so an
  inline `components:` block expands the same way whether a scenario runs through `run` or through a
  device-free reader. The two loaders have already drifted once without it: `load_expanded_scenarios`
  normalizes a malformed component file into `invalid YAML in <file>` (BE-0150), while `run/cli.py`
  lets the same `yaml.YAMLError` escape its `except (OSError, ValueError)` as a raw traceback. The
  shared resolver keeps the normalized form, so `run` gains the clean error too.
- **Crossing into a component file swaps to a sibling resolver, inside the same recursion.**
  `expand_components`'s internal `expand(steps, stack)` (`bajutsu/common/scenario/expand.py`) already
  recurses into a resolved component's substituted steps under the same call. The moment a
  path-shaped ref resolves, that recursive call is handed `resolve.for_component_file()` in place of
  `resolve` — still inside `expand`'s own `stack` and `max_depth` accounting, never a fresh top-level
  call to `expand_components`, which would reset both and turn a real component cycle into a
  `RecursionError` instead of the clean error `expand_components` already raises. A bare name
  resolved under the empty-map sibling is always undefined, because a plain `Component` (`params` +
  `steps`) declares no `components:` for it to consult. Each `ComponentResolver` instance owns its
  own per-ref cache — the per-call cache `expand_components` keeps today, scoped per resolver instead
  of shared across the whole expansion — so the same bare name resolved under two different
  resolvers (inside two different component files, or a scenario file versus a component file) never
  collides. A file-scoped component's own steps expand under the *same* resolver as their caller, so
  a file-scoped component may still `use` another file-scoped one; it may also `use` a file-based
  one, through a path-shaped ref the swap does not touch. A file-based component's own steps carry no
  local map, so they may not `use` a bare name at all.
- **A setup prelude expands its own `use` steps at the call site, before splicing.** A `setup`
  reference (`Preconditions.setup`) also names a scenario-file-shaped document. `apply_setups`
  (`bajutsu/common/scenario/expand.py`) itself is unchanged: it still takes a caller-supplied
  `resolve: Callable[[str], list[Step]]` and splices whatever steps that returns ahead of the calling
  scenario's own. What changes is `run/cli.py`'s own `resolve` lambda — the only caller
  `apply_setups` has, since `load_expanded_scenarios` never applies setups; a device-free reader
  already treats a scenario's `setup` as inert, with or without this item. Instead of returning a
  prelude's steps unexpanded, that lambda loads the prelude's `ScenarioFile`, builds a
  `ComponentResolver` bound to the prelude's own `components:` map, the prelude's own directory, and
  the same suite `root`, runs `expand_components` on the prelude's own steps under it, and returns
  the result. No unexpanded `use` step ever crosses from a prelude into the scenario that includes
  it, so a shared prelude's bare names always resolve against the prelude's own `components:`, never
  against whichever scenario happened to set it as `setup`. A prelude's own path-shaped refs keep
  resolving against the prelude's own directory and stay inside BE-0174's containment, exactly as a
  path-shaped ref resolves anywhere else; no scenario or prelude in this repository writes one today,
  so nothing observable changes for an existing suite.
- **No new path-containment surface.** The loader parses a file-scoped component once, as part of
  the scenario file it already read. Resolving its name never opens a second file, so BE-0174's
  containment check has nothing new to guard here.
- **Scoped to one file.** The loader reads `components:` per file. It never merges the
  map across a suite directory's other files (`load_scenarios_dir` loads each file independently),
  so a name declared in one file stays invisible to another. Reuse across files stays BE-0030's
  file-ref job.

Today, without this item, an author facing that duplication either repeats the steps by hand:

```yaml
# one scenario file — the same three steps copied into every scenario that needs them
scenarios:
  - name: search returns dogs
    steps:
      - type: { text: dog, into: { id: home.search }, submit: true }
    expect:
      - label: { sel: { id: home.status }, equals: "1 result" }
  - name: search returns cats
    steps:
      - type: { text: cat, into: { id: home.search }, submit: true }
    expect:
      - label: { sel: { id: home.status }, equals: "2 results" }
```

or pays BE-0030's file-per-component cost for a block this file alone calls. This item's inline
`components:` block gives a third option — defined and called from the same file:

```yaml
# one scenario file — no separate component file
components:
  search:
    params: [query]
    steps:
      - type: { text: "${params.query}", into: { id: home.search }, submit: true }

scenarios:
  - name: search returns dogs
    steps:
      - use: { component: search, with: { query: dog } }
    expect:
      - label: { sel: { id: home.status }, equals: "1 result" }
  - name: search returns cats
    steps:
      - use: { component: search, with: { query: cat } }
    expect:
      - label: { sel: { id: home.status }, equals: "2 results" }
```

## Alternatives considered

* **A suite-wide implicit registry (bare names searched across every file).** Rejected: the reuse
  this item targets already stays inside one file, so a suite-wide search buys nothing the
  file-scoped rule does not. Searching a whole suite for a bare
  name would blur that boundary back into BE-0030's cross-file job. It would also reopen the
  path-containment question BE-0174 exists to close, for a case a real file already answers.
* **YAML anchors / merge keys for in-file reuse.** Rejected for the same reason BE-0030 rejected them
  for cross-file reuse. An anchor cannot take a named, validated argument the way `with:` binds
  `params`, so a caller cannot vary the reused block per call site without hand-editing the expanded
  text.
* **A distinguishing syntax on `use` (for example `component: "local:<name>"`).** Rejected in favor
  of deciding by the ref's own shape: a path-like ref versus a bare name. Every existing file ref in
  this repository's scenarios already ends in `.yaml` / `.yml`. The shape rule needs no new syntax on
  `use`, and every scenario in this repository keeps working unchanged. One ref shape does change
  meaning: a bare ref naming a component file with no `.yaml` / `.yml` suffix and no `/`. Such a ref
  now fails loudly as an undefined component instead of resolving as a file — a deliberate trade, and
  a low-risk one: no scenario in this repository writes such a ref, though a suite outside it could.

## Progress

> Keep this current as work proceeds. The checklist mirrors the MECE work breakdown in
> *Detailed design* (one box per unit of work); the log records what changed and when
> (oldest first), linking the PRs.

- [ ] Add `components: dict[str, Component]` to `ScenarioFile`
- [ ] Add `ComponentResolver` to `load_expanded.py`: binds a `resolve` callable to a local
      `components:` map, a `root`, and a `base` directory (dispatching a `use` ref by shape: bare
      name → the bound local map, path-like ref → the existing file resolution), owns its own
      per-ref cache, and exposes `for_component_file()` for a sibling bound to an empty map. Wire
      both `load_expanded_scenarios` and `run/cli.py`'s `_expand_file` through it
- [ ] In `expand_components`'s `expand(steps, stack)` recursion, hand the recursive call
      `resolve.for_component_file()` in place of `resolve` the moment a path-shaped ref resolves —
      inside the same `stack` / `max_depth` accounting, not a fresh `expand_components` call — so a
      bare name inside that component's steps always fails as undefined
- [ ] Update `run/cli.py`'s setup `resolve` lambda to load the prelude's `ScenarioFile`, build a
      `ComponentResolver` bound to the prelude's own `components:` map and its own directory, run
      `expand_components` on the prelude's own steps under it, and return the expanded result to
      `apply_setups` (which itself needs no change)
- [ ] Cover it in the fast suite:
      - A file-scoped component expands identically to its hand-duplicated steps.
      - A file-scoped component and a file-based component coexist in one scenario.
      - A bare name undefined in `components:` fails with a clear error.
      - A file-scoped component's name stays invisible from a sibling file in the same suite
        directory.
      - `bajutsu run` and `load_expanded_scenarios` expand the same inline-`components:` scenario
        file identically.
      - A file-scoped component may itself `use` another file-scoped component, and may `use` a
        file-based one.
      - A bare `use` inside a separate component file fails as undefined, even when a same-named
        entry exists in the including scenario file's `components:` map and that file has already
        expanded the same name once.
      - A setup prelude's own bare `use` resolves against the prelude's own `components:`, unaffected
        by a same-named entry in the calling scenario file's map.
      - A malformed component file read through `bajutsu run` reports the same
        `invalid YAML in <file>` message `load_expanded_scenarios` already produces (BE-0150).
- [ ] Update `docs/scenarios.md` (§Components) and `docs/dsl-grammar.md` (the `ScenarioFile`
      production in §2, §6.2, and §6.4 / §6.5 — §6.5's pipeline note "`apply_setups` … (so a prelude
      may itself `use` components)" states the order this item changes) plus their `docs/ja/`
      mirrors

## References

`bajutsu/common/scenario/models/scenario/component.py` — the component model. `bajutsu/common/scenario/expand.py`
(`expand_components`, `apply_setups`) and `bajutsu/common/scenario/load_expanded.py` (where
`ComponentResolver` lives) — the expansion call sites this item unifies behind one resolver.
`bajutsu/run/cli.py` (`_expand_file`, its setup `resolve` lambda) — the other call site, updated to
use the shared resolver.

[BE-0030 — Parameterized shared steps](../BE-0030-parameterized-shared-steps/BE-0030-parameterized-shared-steps.md) —
the file-based component this item adds a scenario-local sibling to.

[BE-0174 — Contain scenario component and data refs within the suite root](../BE-0174-scenario-ref-path-containment/BE-0174-scenario-ref-path-containment.md) —
the containment guarantee a file-scoped component needs no change to, since it never opens a second
file.

[docs/scenarios.md](../../docs/scenarios.md#components-use--reusable-steps),
[docs/dsl-grammar.md](../../docs/dsl-grammar.md#62-components-use--reusable-steps) — the authoring
and normative docs this item updates.
