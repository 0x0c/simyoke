**English** · [日本語](BE-0416-ios-notification-banner-swipe-dismiss-ja.md)

# BE-0416 — Swipe away an interrupting iOS notification banner reactively

<!-- BE-METADATA -->
| Field | Value |
|---|---|
| Proposal | [BE-0416](BE-0416-ios-notification-banner-swipe-dismiss.md) |
| Author | [@0x0c](https://github.com/0x0c) |
| Status | **In progress** |
| Tracking issue | [Search](https://github.com/bajutsu-e2e/bajutsu/issues?q=is%3Aissue+label%3Aroadmap-tracking+in%3Atitle+"BE-0416") |
| Implementing PR | [#1975](https://github.com/bajutsu-e2e/bajutsu/pull/1975) (units 1, 4, 6, 7) |
| Topic | Platform support |
| Related | [BE-0177](../BE-0177-run-behavior-target-config/BE-0177-run-behavior-target-config.md), [BE-0314](../BE-0314-scenario-interrupt-handlers/BE-0314-scenario-interrupt-handlers.md), [BE-0315](../BE-0315-ios-native-system-alert-handling/BE-0315-ios-native-system-alert-handling.md), [BE-0399](../BE-0399-ios-system-alert-interruption-policy/BE-0399-ios-system-alert-interruption-policy.md), [BE-0406](../BE-0406-system-alert-declared-prompts/BE-0406-system-alert-declared-prompts.md) |
<!-- /BE-METADATA -->

## Introduction

Bajutsu already clears two kinds of interstitial iOS screen without asking a model. An interstitial
the application's own accessibility tree can see gets a condition-and-recovery entry in the
`interrupts` field ([BE-0314](../BE-0314-scenario-interrupt-handlers/BE-0314-scenario-interrupt-handlers.md)).
An operating-system alert the tree cannot see gets a native SpringBoard query and a per-prompt
policy ([BE-0315](../BE-0315-ios-native-system-alert-handling/BE-0315-ios-native-system-alert-handling.md),
[BE-0399](../BE-0399-ios-system-alert-interruption-policy/BE-0399-ios-system-alert-interruption-policy.md),
[BE-0406](../BE-0406-system-alert-declared-prompts/BE-0406-system-alert-declared-prompts.md)). Neither
path reaches a third kind of interstitial: the banner iOS raises over the running app when a push or
local notification arrives while the app is in the foreground. This proposal adds a native presence
query and a swipe-based dismiss action for that banner, wired into the reactive-guard shape BE-0315
established for system alerts, so a step's tap lands on the element the scenario names instead of on
a banner the scenario never declared.

## Motivation

A foreground notification banner can appear at any point in a run, and it can sit directly over the
element a step is about to tap. In a real run, the banner has no fixed trigger a scenario can place a
check after: a push notification's arrival depends on a server, and even a local notification an app
schedules for itself lands relative to wall-clock time, not relative to any step. A step whose target
happens to sit under the banner's frame can therefore tap the banner instead of the target, or resolve
against whichever element XCUITest's own hit-testing picks once the overlap is present — a defect that
reads as flakiness, since nothing in the run's report records that a banner ever appeared.

The gap is structural, not incidental. A process outside the application under test draws the banner,
the same separation BE-0315's motivation measured for a SpringBoard alert, so the `interrupts` field's
condition — evaluated only against the application's own tree — has nothing to check against. And the
banner carries no button: the deterministic dismiss BE-0315 built for a system alert resolves a button
by its label, but a person clears a real banner with an upward swipe, not a tap, and tapping it instead
opens the notification's own app and navigates the run away from the scenario it was running. The
gesture this proposal needs already exists in Bajutsu — `swipe` (documented in
[`docs/scenarios.md`](../../docs/scenarios.md) in both a directional and a coordinate form) — but no
existing mechanism knows where the banner is or when to reach for it.

A later reader can tell whether this proposal arrived by running a scenario that raises the banner with
a `push` step immediately before a tap, so the banner's measured frame overlaps that tap's target by
construction rather than by timing. Before this proposal, that tap is unreliable: where it lands
depends on how the two frames overlap at the moment XCUITest resolves it. After this proposal, the
guard clears the banner before the tap executes, and the tap lands on the target.

## Detailed design

### Unit 1 — the banner's accessibility surface, measured

Measured on dedicated Simulators against a throwaway host app that raises its own foreground banner:
iPhone 17 Pro on iOS 26.5, and iPhone 16 Pro on iOS 18.6. iOS 26.3 and 26.4 sit between the two
confirmed versions and were not measured separately. The two endpoints agreed on every answer below,
identifier and subtree included.

| Question | Measured answer |
|---|---|
| Which process exposes the banner | `com.apple.springboard`, the same process a system alert comes from. The application's own tree shows nothing at all. |
| What frame or identifier the banner offers | A SpringBoard element carrying `identifier: NotificationShortLookView`, whose label is the notification's full text. Measured frame `{{8, 58.7}, {386, 78.7}}` on iOS 26.5. Its subtree holds `ShortLook.Platter` and, sharing the same frame, one hittable `Button` — `ShortLook.Platter.Content.Seamless` — whose label is that same text. |
| What a second concurrent banner enumerates | Nothing new: iOS coalesces concurrent banners, so the query finds exactly one `NotificationShortLookView` and one frame. Unit 2's "topmost by frame" ordering has no case to resolve. |
| Whether a banner reaches XCUITest's interruption monitor | Yes, on an *interaction*. A plain query never invokes the monitor and never clears the banner, however many times it runs. |
| Whether a handler can swipe and confirm before returning `true` | Yes. A handler that swiped by the element's own frame, confirmed the banner gone, and returned `true` was invoked exactly once; the interrupted tap then landed on its intended target, with the application still in the foreground. |
| Whether declining reproduces BE-0399's reinvocation loop | No. A declining monitor hands the banner to XCUITest's own handler, which clears it, and the monitor is not invoked again. A monitor that returns `true` *without* clearing does loop — reproduced by capping a throwaway test monitor at six invocations rather than letting it run to the unbounded failure BE-0399 measured. |

Two further measurements, outside the six questions, reshape the design below.

**What today costs is latency, not a misdirected tap.** An undisturbed tap took 0.58s. The same tap
under a banner took **9.18s**, with no monitor and with BE-0399's declining monitor alike: XCUITest
waits the banner out rather than pressing anything. A handler that swipes brought that to **3.72s**.
The banner never received the tap in any arrangement measured, and XCUITest's own handler never
pressed the banner's button either — the application's `didReceive` delegate never fired — so the
"tapping the banner opens its app" hazard the Motivation names is a hazard of *this item's own
implementation*, not a description of today's behavior.

The same measurement retires the two claims beside that one. A step does not "tap the banner instead
of the target". Nor does it "resolve against whichever element XCUITest's own hit-testing picks".
Both are what the proposal assumed before Unit 1 ran. The interrupted tap landed on its intended
target in every arrangement above. With both claims retired, the item's own falsification criterion
goes too. It watches for that same landing, so it reads the same before and after and distinguishes
nothing. What separates before and after sits elsewhere. Unit 1's measurement established two
justifications instead. One is the spurious step defect measured next. The other is the corrupted
`after.png` and visual-regression capture Unit 8 exists to reach.

**A banner already fails a passing step whenever `systemAlertHandling` is on.** BE-0399's monitor
asks the interrupting element for `alert.buttons`. A banner answers with exactly one button whose
label is the notification's own text, so no rule can ever identify it, `recordDeclined` fires, and
BE-0406 Unit 2b turns that into a step failure that overrides an `expect` that passed. Measured
directly: `wouldRecordDeclined=true`, with the notification's body text standing in for the buttons
the run had expected. Nothing in this item created that defect, and shipping Unit 4 is what removes
it.

A persistent banner — the style iOS uses for the "Ready for Apple Intelligence" notification, and
the case that prompted this item — was measured separately by setting `alertType` to `2` in the
Simulator's own `VersionedSectionInfo.plist`. Such a banner stays up indefinitely, confirmed past
30s. XCUITest nonetheless cleared it and landed the tap, in 3.53s. The latency gap therefore closes
on a persistent banner while the spurious failure remains, which is what makes the failure, rather
than the latency, this item's primary justification.

### Unit 2 — a deterministic presence query

Add a driver method that reports whether a notification banner is currently showing and, when one
is, the on-screen frame Unit 3's swipe needs. This mirrors the shape of BE-0315's
`system_alert_labels()`: a thin, non-blocking read that reports a fact and decides nothing. The
method sits behind its own capability token, the way `HANDLE_SYSTEM_ALERT` gates BE-0315's query:
only the iOS XCUITest backend advertises it at first, and a backend without it reports absence
rather than an error. Unit 1 settled the query's shape — it matches `NotificationShortLookView`
against SpringBoard's whole tree and reports at most one banner, since iOS coalesces concurrent ones
— and measured its cost at ~32ms when no banner is up, against ~15ms for the existing
`springboard.alerts.firstMatch.exists` probe beside it.

Unit 1 also narrowed what this query is *for*. It cannot serve the interruption path Unit 4 takes,
because that path already holds the banner element. What it serves is Unit 8's proactive poll, which
is the one thing the interruption path cannot do: clear a banner that is merely sitting on screen
while nothing interacts with it.

### Unit 3 — a deterministic swipe-dismiss action

Add a driver action that swipes the banner away, anchored to the frame Unit 2 reports rather than a
fixed screen coordinate, so the gesture holds across device sizes. The direction matches how a
person dismisses a real banner: upward, ending at an on-screen point above the banner's own frame
rather than past the screen's edge, where SpringBoard claims the drag as its own notification-shade
gesture instead. The action reuses the coordinate machinery `swipe`'s existing driver implementation
already has, rather than adding a second gesture primitive; because that machinery resolves a point
as an offset from the application's own origin, this unit also states how the frame Unit 2 reports —
measured in SpringBoard's coordinate space — converts into it.

Like Unit 2, this unit now serves Unit 8's proactive poll alone. Unit 4's monitor path performs the
same gesture inside the runner, against the element XCUITest handed it, without a driver round trip.

### Unit 4 — the interruption-monitor path

Unit 1's fourth and fifth measurements selected this path, and Unit 1's second extra finding made it
the unit that carries the item's value: a banner reaching BE-0399's alert path fails a step that
would otherwise pass.

The runner's existing interruption monitor gains a branch for a notification banner, taken *ahead of
the policy* rather than inside it. Recognition is by identifier: Unit 1 measured
`NotificationShortLookView` stable across iOS 18.6 and 26.5, while the element's type is an
undocumented raw value and its one button's label varies per notification and per locale. The branch
sits ahead of the `governs` check because neither alert outcome fits a banner — a governing policy
records it as an undeclared interruption and fails the step, and an ungoverned one leaves XCUITest
to wait the banner out.

On that branch the monitor swipes the banner upward by its own frame, then confirms the clearance
before claiming the interruption. Confirmation is what keeps the claim honest: Unit 1 reproduced the
reinvocation loop BE-0399 warned about, so a monitor that claimed a banner it had not cleared would
take the resident runner down. An unconfirmed swipe declines instead, handing the banner back to
XCUITest's own handler, which Unit 1 measured does clear it.

The dismissal reaches the report through the existing drain, as an `AlertEvent` — a dismissal, not
an `UndeclaredInterruption`, since nothing a scenario could declare would identify a banner. Because
the banner carries no button, that record needs a field of its own to be legible: `AlertEvent`
carries only `label`, so a banner dismissal would otherwise arrive as an alert whose locator named
no button. This unit adds an optional discriminator to `AlertEvent`, defaulting to today's alert
case, and renders it in the run's report.

**No config or scenario toggle.** The proposal called for one, on the shape BE-0177 established for
`systemAlertHandling`. Unit 1 removed the case for it. A scenario cannot observe a banner — `expect`
runs against the application's own tree, which Unit 1 confirmed never shows one — so no scenario can
be broken by clearing it, and a toggle would carry no known use. Should one appear, Unit 8 is where
it belongs, since the proactive poll is the half that spends time on every step.

### Unit 5 — showcase fixture and on-device verification

Add a showcase scenario that raises the banner with a `push` step
([`docs/scenarios.md`](../../docs/scenarios.md), `simctl push`) placed immediately before the tap under
test, so the banner's arrival is requested at a step boundary rather than at wall-clock timing. Assert
both that the tap lands on that target and that the run's report carries the banner-dismissal
discriminator Unit 4 adds. `simctl push` returns once the payload reaches the device, not once the
banner is actually on screen. A run in which the banner never appeared would otherwise pass on the tap
alone, exercising nothing; asserting on the discriminator closes that gap. Raising the banner is
app-side work this unit owns: the
showcase apps present no foreground banner today —
[`demos/showcase/scenarios/push.yaml`](../../demos/showcase/scenarios/push.yaml) records that its
`push` leaves the foreground UI unchanged — so this unit also adds the
`UNUserNotificationCenterDelegate` foreground presentation the banner needs, and answers the
notification-authorization prompt in the fixture with a `handleSystemAlert` step, since `permissions`
cannot pre-grant notification authorization on iOS ([`docs/scenarios.md`](../../docs/scenarios.md)).
The tap under test must sit inside the frame Unit 1 measured — `{{8, 58.7}, {386, 78.7}}` on a
6.3-inch device — so the overlap holds by construction; if no showcase control does, this unit adds
one. The off-Simulator gate cannot prove a native swipe against a real banner, so this unit must
exercise the scenario on a booted Simulator.

### Unit 6 — docs

Document the banner branch in [`docs/scenarios.md`](../../docs/scenarios.md) alongside `interrupts`
and `systemAlertHandling`, extending BE-0314's existing comparison of when to reach for each
mechanism, and mirror it in [`docs/ja/scenarios.md`](../../docs/ja/scenarios.md). Record the branch
and its placement ahead of the `governs` check in
[`docs/architecture.md`](../../docs/architecture.md) and its Japanese mirror, as BE-0113 requires of
a change to behavior those pages describe. There is no CLI flag to document, since Unit 4 adds no
toggle.

### Unit 7 — tests

For Unit 4: recognition of SpringBoard's banner identifier, and its rejection of an alert's; that a
banner's own button can never identify a rule, the condition that used to fail a passing step; the
store reporting a swiped banner apart from a tapped label, clearing it once drained, ordering
oldest-first, and dropping what a previous scenario queued; the drain endpoint carrying the banners
over both the generated and the legacy transport; the driver reading them, and reading none from a
runner that predates them; a `/tap` fold without them still counting as a fold; the orchestrator
mapping them to `AlertEvent`s of their own kind and never to an `UndeclaredInterruption`; a banner
landing on the step it interrupted without failing it; and the report telling the two kinds apart in
both the manifest round trip and the rendered HTML.

For Units 2, 3, and 8 when they land: a fake driver whose presence query flips between polls, the
guard clearing the banner before a step's own actuation, and the capability gate leaving a backend
without it unchanged.

### Unit 8 — the proactive poll, for what the monitor cannot reach

Unit 1 measured that a plain query never invokes the interruption monitor and never clears the
banner. Everything Bajutsu does that is not an interaction therefore runs with the banner still on
screen — a step's `after.png`, and with it every visual-regression comparison, which a banner
overlapping the captured region corrupts outright. Unit 4 closes nothing here.

This unit arms a guard that polls Unit 2's presence query on the bounded interval BE-0315 already
established for the SpringBoard probe, and clears the banner through Unit 3's action the moment one
is found. It is where a config- and scenario-level toggle belongs if one is wanted, following the
same config-then-scenario, flag-overridable precedence
([BE-0177](../BE-0177-run-behavior-target-config/BE-0177-run-behavior-target-config.md)) that
`systemAlertHandling` established, since this half spends time on every step rather than only on an
interrupted one.

### Prime directives preserved

- **AI never judges.** Recognition is an identifier comparison and the dismissal is a swipe; this
  item adds no new AI surface.
- **Determinism first.** No fixed sleep. The monitor confirms the swipe by bounded re-observation of
  the banner's absence, and never waits out the banner's own auto-dismiss timeout — which Unit 1
  measured is exactly what XCUITest does when nothing answers.
- **App-agnostic.** The branch is a generic runner mechanism keyed on SpringBoard's own identifier;
  no per-app code is added.

## Alternatives considered

- **Route the banner through `interrupts`.** Rejected on the same premise BE-0315's motivation
  established for a system alert: a process outside the application under test draws the banner, so
  `interrupts`' condition — evaluated only against the application's own tree — has nothing to check
  against.
- **Dismiss the banner with a tap instead of a swipe.** Rejected: the banner's one button is the
  *open the notification's app* affordance, which would carry the run away from the scenario under
  test. A swipe removes the banner without that side effect. Unit 1 measured that XCUITest's own
  default handler never presses that button either, so this rules out a choice open to *this item*
  rather than describing what happens today.
- **Add an AI-vision fallback for a case the native query cannot resolve, mirroring the vision guard
  system alerts once had.** Rejected under prime directive 1. BE-0402 already removed the equivalent
  fallback from `run`'s system-alert path for the same reason: the banner's presence and frame are
  exactly the kind of fact a native query answers, with no judgment call for a model to make.
- **Let the banner auto-dismiss on its own timeout instead of swiping it away.** Rejected under prime
  directive 2 (no fixed sleep). Unit 1 measured what that choice costs, because it is what XCUITest
  already does when nothing answers: 9.18s per interrupted interaction against 0.58s undisturbed. A
  banner set to the persistent style never auto-dismisses at all.

## Progress

> Keep this current as work proceeds. The checklist mirrors the MECE work breakdown in
> *Detailed design* (one box per unit of work); the log records what changed and when
> (oldest first), linking the PRs.

- [x] Unit 1 — the banner's accessibility surface, measured on iOS 18.6 and 26.5. The answers, and
      the two findings that reshaped Units 2–4, are recorded in *Detailed design* above.
- [ ] Unit 2 — deterministic presence query (`Driver` method reporting the banner's frame or absence).
      Scope narrowed by Unit 1: it serves Unit 8's proactive poll, not Unit 4.
- [ ] Unit 3 — deterministic swipe-dismiss action anchored to the measured frame. Narrowed the same
      way.
- [x] Unit 4 — the interruption-monitor path: recognize the banner ahead of the policy, swipe it by
      its own frame, confirm the clearance before claiming the interruption, and report it as an
      `AlertEvent` under a kind of its own. Lands without the config/scenario toggle the proposal
      called for — see *Unit 4* above for why Unit 1 removed the case for it.
- [ ] Unit 5 — showcase fixture, including the app-side foreground banner presentation and a tap
      target inside the banner's frame, and on-device verification.
- [x] Unit 6 — docs (`docs/scenarios.md`, `docs/architecture.md`, and both `docs/ja/` mirrors).
- [x] Unit 7 — tests for what Unit 4 landed. The Unit 2/3/8 half remains with those units.
- [ ] Unit 8 — the proactive poll, for the banner a run never interacts its way past: the corrupted
      `after.png` and visual-regression capture Unit 1 measured the monitor cannot reach.

Log:

- 2026-09-11 — [#1975](https://github.com/bajutsu-e2e/bajutsu/pull/1975) — Unit 1 measured on dedicated Simulators (iPhone 17 Pro / iOS 26.5, iPhone 16 Pro /
  iOS 18.6), and Units 2–4 rewritten against what it found. Unit 4 landed: the runner's interruption
  monitor now recognizes a notification banner ahead of the alert policy, swipes it away by its own
  measured frame, confirms the clearance before claiming the interruption, and reports it as an
  `AlertEvent` carrying a `notificationBanner` kind. That removes the defect Unit 1 measured, where a
  banner arriving during a run with `systemAlertHandling` on was recorded as an undeclared
  interruption and failed an otherwise-passing step, naming the notification's body text among the
  buttons the run had expected.

## References

- [BE-0314](../BE-0314-scenario-interrupt-handlers/BE-0314-scenario-interrupt-handlers.md) — the
  `interrupts` field, and why its in-tree condition cannot reach a system-owned overlay.
- [BE-0315](../BE-0315-ios-native-system-alert-handling/BE-0315-ios-native-system-alert-handling.md) —
  the native presence-query and reactive-guard shape this proposal reuses for a banner instead of an
  alert.
- [BE-0399](../BE-0399-ios-system-alert-interruption-policy/BE-0399-ios-system-alert-interruption-policy.md) —
  the interruption monitor and its ordering, which Unit 1 must measure against the banner before
  Unit 4 can reuse it.
- [BE-0406](../BE-0406-system-alert-declared-prompts/BE-0406-system-alert-declared-prompts.md) — the
  per-prompt declaration this item's single config toggle is simpler than, since a banner has no
  distinct prompts to declare.
- [BE-0177](../BE-0177-run-behavior-target-config/BE-0177-run-behavior-target-config.md) — the
  config-then-scenario, flag-overridable precedence Unit 4's toggle follows.
- [`docs/scenarios.md`](../../docs/scenarios.md) — the existing `swipe` step whose coordinate machinery
  Unit 3 reuses, and the `push` step Unit 5's fixture uses to raise the banner deterministically.
- Apple, [`XCTestCase.addUIInterruptionMonitor(withDescription:handler:)`](https://developer.apple.com/documentation/xctest/xctestcase/adduiinterruptionmonitor(withdescription:handler:)) —
  the monitor Unit 4's interruption path would reuse, cited in BE-0314 and BE-0399 as the same prior
  art.
