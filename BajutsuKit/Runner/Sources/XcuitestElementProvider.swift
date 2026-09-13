import XCTest
import BajutsuRunner

/// The concrete `ElementProviding` (BE-0019): the only XCTest-touching piece of the runner.
///
/// Walks `XCUIApplication` into the normalized `Element` shape the Python driver expects
/// (identifier / label / value / traits / frame, matching what the backend-agnostic `Element`
/// produces) and actuates the exact `XCUIElement` a snapshot handle maps back to.
///
/// BE-0105 makes the query cheap: instead of materializing an `XCUIElement` per node and reading
/// each attribute over its own XCUITest round-trip (elements × attributes ≈ 600 trips for one
/// screen), `queryElements()` takes **one** `app.snapshot()` and reads every attribute from that
/// tree. The trade-off is that snapshot nodes are values, not tappable elements, so each element's
/// backing records its identity and root-relative position path. `tap` / `gesture` re-derive the
/// live `XCUIElement` from that position path — one cheap resolution — and fall back to a narrow
/// identity query only when the path no longer resolves, which also reaches system-owned sheets
/// whose snapshot hierarchy cannot be replayed through live direct-child queries.
/// Backs a SpringBoard alert button by its ordinal within `springboard.alerts.buttons` (BE-0316).
///
/// The out-of-process alert is not part of the app snapshot the `PositionPathBacking` walk records,
/// so its buttons address by ordinal instead: `querySystemAlertButtons` reads them in order, and
/// `tapSystemAlertButton` re-derives the same live element by that ordinal. A permission prompt
/// carries a fixed, small set of buttons, so the ordinal is stable between the query and the tap.
private final class SystemAlertButtonBacking {
    let ordinal: Int
    init(ordinal: Int) { self.ordinal = ordinal }
}

final class XcuitestElementProvider: ElementProviding {
    private let app: XCUIApplication
    // A second, on-demand handle for SpringBoard — which owns the out-of-process permission prompt
    // (BE-0316) — built lazily so every other query and tap stays scoped to the app under test.
    private lazy var springboard = XCUIApplication(bundleIdentifier: springboardBundleID)
    // A third handle for the process that draws `SFSafariViewController` (BE-0396), built lazily for
    // the same reason: a run that never opens a browser never touches it.
    private lazy var safariViewService = XCUIApplication(bundleIdentifier: Self.safariViewServiceID)

    private static let safariViewServiceID = "com.apple.SafariViewService"

    // The application window's frame, in points, read once and cached (BE-0407 Unit 7). The runner
    // never rotates the device and the window fills the screen for the whole resident lease — see
    // `screenSize()`'s own rationale — so this is stable for as long as this provider exists, and
    // re-reading it live on every `tap` / `isHittable` bought nothing but a repeated round trip.
    // Guarded against caching `.zero`, though: `app.frame` reads `.zero` while the app is not yet
    // (or no longer) foreground, and this provider outlives a warm resume's own terminate + launch
    // — locking in a `.zero` seen at the wrong moment would poison `screenSize()` and permanently
    // defeat the `isHittable` guard below for the rest of the lease, where a live read self-corrects
    // on the very next call. Read live until a genuine frame is observed once, then fixed forever.
    private var _appFrame: CGRect?
    private var appFrame: CGRect {
        if let cached = _appFrame { return cached }
        let frame = app.frame
        if frame != .zero { _appFrame = frame }
        return frame
    }

    init(app: XCUIApplication) {
        self.app = app
    }

    func queryElements() -> [ElementSnapshot] {
        // One accessibility round-trip for the whole attribute-bearing tree; the tabs a coordinate backend collapses
        // into an opaque "Tab Bar" group surface here as individual buttons, the point of the richer
        // actuator. A snapshot failure yields an empty screen rather than a crash — the run fails
        // loudly downstream when nothing resolves.
        guard let root = try? app.snapshot() else { return [] }
        let rootNode = SnapshotNodeAdapter(root)
        // `safariViewService.state` is a cross-process (XPC) query, paid even when the app under
        // test never opens a browser. The app's own snapshot already answers, for free, whether a
        // browser boundary is even on screen right now (BE-0407 Unit 9) — only when one is does the
        // XPC probe below get a chance to matter.
        guard containsBrowserViewBoundary(in: rootNode), safariViewService.state == .runningForeground
        else {
            return flattenSnapshot(root: rootNode)
        }
        // A presented `SFSafariViewController` is drawn by another process, and the app's own
        // snapshot reports it differently per iOS version: through iOS 18 it mirrors the whole
        // browser subtree, from iOS 26 it stops at the remote-view boundary and reports nothing
        // below it. Reading the browser from the service that owns it makes the two versions agree —
        // one tree, complete on both — and the mirror is pruned so the versions that do carry it
        // don't report every browser element twice, which would read as an ambiguous selector.
        let appElements = flattenSnapshot(root: rootNode) {
            ($0.nodeIdentifier ?? "").hasPrefix(BrowserChrome.browserViewIDPrefix)
        }
        guard let browserRoot = try? safariViewService.snapshot() else {
            // The prune only pays off when the service's own tree replaces the mirror. Without that
            // tree in hand, report the app's own walk whole: on the iOS versions that do mirror the
            // browser, returning the pruned walk would hand back a screen the browser was cut out
            // of, and a scenario would time out on elements the app was still carrying.
            return flattenSnapshot(root: rootNode)
        }
        // The two versions also name the browser's own chrome differently in one place, which
        // `normalizeBrowserChrome` repairs so a scenario's selector travels between them (BE-0396).
        let browser = SnapshotNodeAdapter(browserRoot)
        let browserElements = normalizeBrowserChrome(
            flattenSnapshot(root: browser, in: .safariViewService), root: browser
        )
        // An alert the *app* raises over the browser — iOS's "Save Password" offer after a web
        // sign-in is the one that matters — is drawn by the app process and mirrored into the
        // service's tree as well, so concatenating the two walks reports each of its buttons twice.
        // The identifier-prefix prune above cannot reach it: the alert is not browser chrome and
        // carries no `browserViewIDPrefix`. A duplicate like that is one control seen twice rather
        // than an ambiguity — the same reading `uniquelyIdentifiedElement` already applies to
        // XCUITest's duplicate registration of an alert button — but the *tree* must say so too,
        // because a selector resolves over the tree: left in, the pair fails `resolve_unique` and
        // the guard's in-tree dismissal correctly declines to guess, so the alert is never cleared.
        // Matched on the whole reported identity, frame included: two genuinely distinct controls do
        // not share every attribute *and* occupy the same rectangle, so this drops mirrors and
        // nothing else.
        let appIdentities = Set(appElements.map(Self.identity))
        return appElements + browserElements.filter { !appIdentities.contains(Self.identity($0)) }
    }

    /// Everything the tree reports about one element, as a comparable key — the test for "the same
    /// control reported twice" in `queryElements`.
    private static func identity(_ element: ElementSnapshot) -> String {
        let frame = element.frame
        return [
            element.identifier ?? "", element.label ?? "", element.value ?? "",
            element.traits.joined(separator: ","),
            "\(frame.x),\(frame.y),\(frame.width),\(frame.height)",
        ].joined(separator: "\u{1F}")
    }

    func screenSize() -> (width: Double, height: Double) {
        // The application window's frame in points — the coordinate space the snapshot frames use — so
        // an on-screen element's frame center falls within it (BE-0326). The window fills the screen
        // and its frame is stable regardless of a scroll view's buffered off-screen children, unlike
        // the element tree's extent. (`XCUIScreen` has no frame/bounds; `appFrame` is the window.)
        (Double(appFrame.width), Double(appFrame.height))
    }

    // `isHittable` reads `false` both for "covered" and for "offscreen" (Apple's own docs say so) —
    // but only the former is `tap` / `isHittable(backingElement:)`'s question; a not-yet-scrolled-to
    // target is a `scroll` question (docs/drivers.md), not this one. Tested on the same point a
    // following tap would actually land on (the frame's center), matching the web guard's own
    // point-based question (`_point_hits`, playwright.py) rather than a frame-overlap test:
    // `intersects` would still guard a target straddling the fold whose center has already scrolled
    // off, reaching `isHittable` for a question this check does not ask, and reading differently
    // from web for the identical screen. `appFrame` is the stable window/screen bounds
    // `screenSize()` above already uses for the same viewport-vs-content-extent distinction. A
    // single shared predicate, not one copy per caller, so `tap` and `isHittable(backingElement:)` —
    // which the recovery loop requires to agree — cannot drift apart on this question.
    private func centerIsOnScreen(_ el: XCUIElement) -> Bool {
        let f = el.frame
        return appFrame.contains(CGPoint(x: f.midX, y: f.midY))
    }

    func tap(backingElement: AnyObject, taps: Int, duration: TimeInterval) -> TapResult {
        guard let backing = backingElement as? PositionPathBacking else { return .notFound }
        guard let el = liveElement(for: backing) else { return .stale }
        if centerIsOnScreen(el) {
            guard el.isHittable else { return .notHittable }
        }
        // A browser element is actuated at its own point rather than through the element (BE-0396).
        // `XCUIElement.tap()` reaches the page content across the process boundary but is silently
        // dropped by the browser's own chrome — a resolved, hittable Close button simply does not
        // dismiss — whereas the coordinate tap the element's frame yields lands on both. The frame
        // is read live here, not from the snapshot, so a browser still animating in is tapped where
        // it now is; the coordinate space is the app's own, which the guard above already tests
        // against `appFrame`.
        //
        // BE-0407 Unit 8 proposed generalizing this route to every element, on the premise that
        // `liveElement(for:)`'s own verification above made the coordinate substitution free of
        // staleness risk. Two review rounds found that premise incomplete: `el.isHittable` above
        // checks XCUITest's own hit point for the element (which honours a custom
        // `accessibilityActivationPoint` and can differ from the frame's geometric center under
        // partial occlusion), while a coordinate tap always lands on that geometric center — a
        // silent `.ok` for a tap that actually landed on whatever covers the center, on an element
        // whose real hit point was clear. Reverted to the Safari-only route pending a design that
        // reconciles the two points, rather than risking a silent mis-tap on every element.
        let target: Tappable = backing.root == .safariViewService
            ? coordinate(Double(el.frame.midX), Double(el.frame.midY))
            : el
        if duration > 0 {
            target.press(forDuration: duration)
        } else if taps >= 2 {
            target.doubleTap()
        } else {
            target.tap()
        }
        return .ok
    }

    func isHittable(backingElement: AnyObject) -> TapResult {
        guard let backing = backingElement as? PositionPathBacking else { return .notFound }
        guard let el = liveElement(for: backing) else { return .stale }
        guard centerIsOnScreen(el) else { return .ok }
        return el.isHittable ? .ok : .notHittable
    }

    func tapPoint(x: Double, y: Double) -> TapResult {
        coordinate(x, y).tap()
        return .ok
    }

    func gesture(backingElement: AnyObject, kind: String, scale: Double, radians: Double) -> TapResult {
        guard let backing = backingElement as? PositionPathBacking else { return .notFound }
        guard let el = liveElement(for: backing) else { return .stale }
        let actuate: () -> Void
        switch kind {
        case "pinch":
            // velocity sign must match the scale direction (zoom in vs out) or XCUITest rejects it.
            actuate = { el.pinch(withScale: CGFloat(scale), velocity: scale >= 1 ? 1 : -1) }
        case "rotate":
            actuate = { el.rotate(CGFloat(radians), withVelocity: 1) }
        default:
            return .notFound
        }
        // XCUITest occasionally synthesizes a two-finger gesture the Simulator drops before the app's
        // recognizer fires, leaving no effect and a red `expect`. Re-issuing helps only when a landing
        // is *observable*, because pinch and rotate are accumulating: re-applying one that already
        // landed zooms/rotates again. The single app-agnostic signal a landing left is the actuated
        // element's own value — and only an app that mirrors its gesture result there (as the showcase
        // GestureView does) exposes it; a plain image / map / scroll view does not. So the retry is
        // opt-in: a scenario whose target self-mirrors sets BAJUTSU_GESTURE_RETRY=1 in its launchEnv,
        // and the retry re-issues until `el.value` moves (nil = unreadable, never a landing). Without
        // the opt-in the gesture is actuated exactly once — the deterministic default for an arbitrary
        // app, where re-applying an accumulating gesture a drop-dependent number of times would make
        // the magnitude non-deterministic (prime directive 2).
        if RunnerServer.forwardedLaunchEnvironment["BAJUTSU_GESTURE_RETRY"] == "1" {
            actuateUntilStateChanges(
                maxAttempts: Self.maxGestureAttempts,
                signature: { el.value as? String },
                actuate: actuate
            )
        } else {
            actuate()
        }
        return .ok
    }

    /// The most times `gesture` re-issues a dropped pinch/rotate before giving up (a genuinely
    /// no-op gesture then still returns, and the run's `expect` fails loudly rather than looping).
    private static let maxGestureAttempts = 4

    func swipe(fromX: Double, fromY: Double, toX: Double, toY: Double) -> TapResult {
        coordinate(fromX, fromY).press(forDuration: 0.1, thenDragTo: coordinate(toX, toY))
        return .ok
    }

    /// How fast the scroll drag traverses, in points per second — the whole of what makes the
    /// gesture non-inertial (BE-0400).
    ///
    /// A drag traversed fast enough lifts with the momentum that speed implies, and iOS carries the
    /// scroll view on past the endpoints: measured on an iPhone 17 Pro (iOS 26.5), `.default`
    /// overshot a request by +133 points on a full-size step and by +225 on a small one, leaving a
    /// floor of ~269 points below which no step travelled however little it asked for.
    ///
    /// The speed that fails first is the one a *short* drag fails at, not a long one, which is why
    /// this is set well under where a full-size step still looks correct. At 400 an 875-point
    /// viewport's 0.6 step was exact while its 0.05 step flung 5x; at 300 the 0.05 step was exact
    /// while a 17-point drag flung 9x and a 26-point one flung on some repeats and not others. At
    /// 200 and at 100, every step from 17 points to 525 came back exactly 10.0 points short of its
    /// request, and the two speeds agreed to the point. Small steps are precisely what `scroll`'s `amount` and the overshoot recovery ask
    /// for, so the value is chosen where they hold, at a cost of ~0.9s on a full-size step.
    ///
    /// The drag is the whole gesture. An earlier `thenHoldForDuration`, meant to settle the view
    /// before lift, changed nothing at 0.0, 0.3, or 1.0 seconds, at this speed or at `.default`, so
    /// it is not part of the contract and is not spent.
    private static let scrollVelocity = XCUIGestureVelocity(rawValue: 200)

    /// How long the scroll drag presses before it starts travelling. Held at the value the gesture
    /// has always used: dropping it to zero measurably widened the shortfall below (BE-0400).
    private static let scrollPressDuration: TimeInterval = 0.1

    func scroll(fromX: Double, fromY: Double, toX: Double, toY: Double) -> TapResult {
        // Unlike `swipe` (a plain press-and-drag traversed at `.default`, which lifts with residual
        // velocity so iOS flings the scroll view onward), traverse slowly enough that the content
        // stops where the gesture ended. The content still falls ~10 points short of the request —
        // the pan recognizer's slop, which the driver conformance suite's realized-travel tolerance
        // names rather than the gesture compensating for one device's constant.
        coordinate(fromX, fromY).press(
            forDuration: Self.scrollPressDuration,
            thenDragTo: coordinate(toX, toY),
            withVelocity: Self.scrollVelocity,
            // No hold: the only press overload that takes a velocity also requires a hold duration,
            // and every duration measured the same, so this spends none.
            thenHoldForDuration: 0
        )
        return .ok
    }

    func typeText(_ text: String) -> TapResult {
        // BE-0407 Unit 15 proposed pasting via the system pasteboard instead of per-character
        // synthesis. An on-device run of `text_editing.yaml` (the scenario pairing `type` with
        // `copy`/`clipboard`) showed why not: `app.typeKey("v", modifierFlags: .command)` triggers
        // iOS's cross-app "Allow Paste" consent alert on *every* paste — "\"BajutsuRunnerUITests-
        // Runner\" would like to paste from \"Showcase SwiftUI\" — Do you want to allow this?" — which
        // blocks the main thread indefinitely with no button this runner's own interruption
        // monitor is registered to dismiss, timing out `POST /type` and crashing the runner. Reverted
        // to plain per-character synthesis; a paste route would need either a way to suppress or
        // auto-answer that consent alert, or confirmation it does not fire on some other iOS version,
        // before it can land.
        app.typeText(text)
        return .ok
    }

    func deleteText(count: Int) -> TapResult {
        // Type the delete key `count` times on the focused field; XCUITest maps `.delete` to a real
        // backspace, so this is agnostic to what the field held (BE-0265). The orchestrator focuses
        // the field first, so the deletes land in it.
        app.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: count))
        return .ok
    }

    func selectAll() -> TapResult {
        // Cmd+A selects the focused field's whole content — the hardware-keyboard shortcut the
        // Simulator honors (BE-0265).
        app.typeKey("a", modifierFlags: .command)
        return .ok
    }

    func copySelection() -> TapResult {
        // Cmd+C copies the active selection to the clipboard, read back by the `clipboard` assertion.
        app.typeKey("c", modifierFlags: .command)
        return .ok
    }

    func setPickerValue(backingElement: AnyObject, value: String) -> TapResult {
        guard let backing = backingElement as? PositionPathBacking else { return .notFound }
        guard let el = liveElement(for: backing) else { return .stale }
        // `liveElement(for:)` deliberately excludes `value` from its identity check, precisely because
        // a wheel legitimately changes value between the snapshot and the actuation — which is what
        // makes it safe to reuse here.
        el.adjust(toPickerWheelValue: value)
        // The call reports nothing: it returns Void, never throws, and a value it could not reach is
        // recorded as a soft XCTIssue that `RunnerUITest.continueAfterFailure` swallows (BE-0356). So
        // read the wheel back, bounded, and decide here — otherwise a wheel with no such row would
        // answer `ok` and the run would assert against whatever value it happened to stop on.
        let landed = settlesTo(
            value, maxSamples: Self.maxPickerValueSamples, sample: { el.value as? String }
        )
        return landed ? .ok : .valueNotFound
    }

    /// The most times `setPickerValue` re-reads the wheel before calling the value absent. A wheel
    /// decelerating through rows needs more than one look, and `settlesTo` spends two of these
    /// confirming the value holds, so the cap leaves room for a few passing rows before the run that
    /// counts. Each read is a real query, so this bounds observations rather than standing in for a
    /// sleep (BE-0356).
    private static let maxPickerValueSamples = 5

    func querySystemAlertButtons() -> [ElementSnapshot] {
        // Read the buttons of whatever SpringBoard alert is up, in order; empty when no alert is
        // present, which the Python driver polls against the step's timeout. `alerts.buttons.count`
        // alone is already a full SpringBoard query, paid on every poll regardless of whether one is
        // up — the overwhelmingly common case. `firstMatch.exists` answers the same "is one up at
        // all" question and short-circuits on the first match rather than counting every button
        // (BE-0407 Unit 13), so it never runs the enumeration below for nothing. A permission prompt
        // has a couple of buttons, so reading each one's label/frame directly (rather than one
        // whole-tree snapshot) is cheap, and the ordinal is the tappable backing.
        guard springboard.alerts.firstMatch.exists else { return [] }
        let buttons = springboard.alerts.buttons
        let count = buttons.count
        return (0..<count).map { i in
            let button = buttons.element(boundBy: i)
            return ElementSnapshot(
                identifier: nil,
                label: nonEmpty(button.label),
                value: nil,
                traits: ["button"],  // base.Trait.BUTTON
                frame: frameTuple(button.frame),
                backingElement: SystemAlertButtonBacking(ordinal: i)
            )
        }
    }

    func tapSystemAlertButton(backingElement: AnyObject) -> TapResult {
        guard let backing = backingElement as? SystemAlertButtonBacking else { return .notFound }
        let button = springboard.alerts.buttons.element(boundBy: backing.ordinal)
        guard button.exists else { return .stale }  // the alert dismissed itself between query and tap
        button.tap()
        return .ok
    }

    func screenshot() -> Data? {
        app.screenshot().pngRepresentation
    }

    // MARK: - Helpers

    /// Recover the live `XCUIElement` for a snapshot backing, or nil if the screen no longer matches.
    ///
    /// The recorded position path is the primary path: it is a single element resolution, not a
    /// re-walk of the whole tree, so it keeps the per-interaction cost off the ~600-round-trip scale
    /// the class's one-`snapshot()` design (BE-0105) exists to avoid. When it resolves and the
    /// attributes still match, that element wins.
    ///
    /// A narrow flat query is the recovery step, reached only when the position path fails — the
    /// element moved, or its snapshot child indices cannot be replayed through a live hierarchy with
    /// different system-owned wrapper nodes, as happens for the iOS Save Password sheet and for a
    /// presented `UIAlertController`. The query must yield one identity match, or several that also
    /// agree on value and frame — XCUITest registers an alert's button twice at one place, and that
    /// pair is one control rather than an ambiguity. Matches that disagree on either, and anonymous
    /// elements, have no identity to recover by, so a position-path miss on them is a genuine stale.
    /// Frame is deliberately excluded from the recorded-against-live identity check (BE-0287), and
    /// value with it, for its own reason — a slider or text field legitimately changes value between
    /// the snapshot and the tap; both decide only between candidates read from one live query.
    private func liveElement(for backing: PositionPathBacking) -> XCUIElement? {
        let root = application(for: backing.root)
        let el = element(at: backing.path, from: root)
        // One `el.snapshot()` (BE-0407 Unit 7) stands in for the `exists` check and every attribute
        // this compares against `backing.recorded`: a snapshot throws exactly where `exists` would
        // read false, and its properties are already in hand rather than each its own round trip —
        // the ~7 XCUITest round trips this resolution used to cost on every normal-path actuation.
        if let snapshot = try? el.snapshot(), attributesMatch(
            recorded: backing.recorded,
            current: recordedAttributes(of: snapshot, includingValue: false)
        ) {
            return el
        }
        return uniquelyIdentifiedElement(matching: backing.recorded, in: root)
    }

    /// The application handle an element's position path is relative to (BE-0396). Both recovery
    /// steps below must query the same one the snapshot was read from: the app's own tree does not
    /// carry the browser's elements from iOS 26, so resolving a browser element against it would
    /// report a perfectly live control as stale.
    private func application(for root: ElementRoot) -> XCUIApplication {
        switch root {
        case .app: return app
        case .safariViewService: return safariViewService
        }
    }

    /// Resolve one semantic identity without depending on snapshot hierarchy shape.
    private func uniquelyIdentifiedElement(
        matching recorded: RecordedAttributes, in root: XCUIApplication
    ) -> XCUIElement? {
        guard recorded.identifier != nil || recorded.label != nil else { return nil }
        var query = root.descendants(matching: .any)
        if let identifier = recorded.identifier {
            query = query.matching(identifier: identifier)
        }
        if let label = recorded.label {
            query = query.matching(NSPredicate(format: "label == %@", label))
        }
        let candidates = query.allElementsBoundByIndex.filter { $0.exists }
        let attributes = candidates.map { recordedAttributes(of: $0, includingValue: true) }
        guard let index = resolvableMatchingIndex(recorded: recorded, candidates: attributes) else {
            return nil
        }
        return candidates[index]
    }

    /// Read the identity fields for the flat-query recovery path (`uniquelyIdentifiedElement`) — the
    /// rare case, reached only once the position path has already missed.
    ///
    /// Always called with `includingValue: true` there: `resolvableMatchingIndex`'s group check needs
    /// `value` alongside identifier/label/traits/frame, the same fields the host's
    /// `_collapse_identical_duplicates` keys on. `includingValue` carries no default on purpose, so a
    /// caller states its choice and the compiler asks a new one rather than silently reporting `value`
    /// as `nil` throughout — which `resolvableMatchingIndex` would read as every candidate agreeing,
    /// collapsing the ambiguity the field exists to preserve. Unlike `identifier` and `label`, `value`
    /// is read straight off the optional rather than through `nonEmpty`: the host's key keeps an
    /// absent value and an empty one apart (the reply omits the key entirely for `nil`, so Python sees
    /// `None` against `""`), and `flattenSnapshot` records it unnormalized as well. Normalizing here
    /// alone would collapse a pair those two keep apart.
    ///
    /// Each property here is its own XCUITest round trip on a live `XCUIElement` — acceptable on this
    /// path since it runs only on a position-path miss, unlike the overload below that the common,
    /// normal-path resolution uses instead.
    private func recordedAttributes(
        of el: XCUIElement, includingValue: Bool
    ) -> RecordedAttributes {
        RecordedAttributes(
            identifier: nonEmpty(el.identifier),
            label: nonEmpty(el.label),
            value: includingValue ? el.value as? String : nil,
            traits: traitTokens(
                elementType: el.elementType, isEnabled: el.isEnabled, isSelected: el.isSelected
            ),
            frame: frameTuple(el.frame)
        )
    }

    /// The same identity fields, off an already-fetched `el.snapshot()` (BE-0407 Unit 7) rather than a
    /// live `XCUIElement` — every field below is already in hand, at the cost of the one round trip
    /// the snapshot itself paid, rather than one round trip per property. `liveElement(for:)` calls
    /// this with `includingValue: false` on every actuation that resolves normally: the
    /// recorded-against-live identity check deliberately excludes `value` (a slider or text field
    /// legitimately changes value between the snapshot and the tap), so there is nothing for a caller
    /// on this path to ask for — unlike the flat-query recovery overload above, this one is never
    /// called with `true`.
    private func recordedAttributes(
        of snapshot: any XCUIElementSnapshot, includingValue: Bool
    ) -> RecordedAttributes {
        RecordedAttributes(
            identifier: nonEmpty(snapshot.identifier),
            label: nonEmpty(snapshot.label),
            value: includingValue ? snapshot.value as? String : nil,
            traits: traitTokens(
                elementType: snapshot.elementType,
                isEnabled: snapshot.isEnabled,
                isSelected: snapshot.isSelected
            ),
            frame: frameTuple(snapshot.frame)
        )
    }

    /// Resolve a root-relative index path back to an `XCUIElement` by descending direct children —
    /// the inverse of the position path `flattenSnapshot` records over `app.snapshot()`.
    private func element(at path: PositionPath, from root: XCUIApplication) -> XCUIElement {
        path.reduce(root as XCUIElement) { parent, index in
            parent.children(matching: .any).element(boundBy: index)
        }
    }

    /// An absolute screen point as an `XCUICoordinate` (offset from the app's origin).
    private func coordinate(_ x: Double, _ y: Double) -> XCUICoordinate {
        app.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0))
            .withOffset(CGVector(dx: x, dy: y))
    }
}

// MARK: - Actuation surface

/// The tap operations `XCUIElement` and `XCUICoordinate` already share, so `tap` picks its target
/// once — the element itself, or the point its frame yields for a browser element (BE-0396) — and
/// keeps one copy of the taps / duration branch rather than one per target kind.
private protocol Tappable {
    func tap()
    func doubleTap()
    func press(forDuration duration: TimeInterval)
}

extension XCUIElement: Tappable {}
extension XCUICoordinate: Tappable {}

// MARK: - Snapshot bridging

/// Bridge XCTest's snapshot into the pure `SnapshotNode` the flatten walk consumes, so the whole tree
/// comes from a single `app.snapshot()` (BE-0105) with attributes normalized the same way the
/// per-element walk did. `XCUIElementSnapshot` is a protocol, not a concrete type, so it is wrapped
/// rather than conformed by extension.
private struct SnapshotNodeAdapter: SnapshotNode {
    private let snapshot: any XCUIElementSnapshot

    init(_ snapshot: any XCUIElementSnapshot) {
        self.snapshot = snapshot
    }

    var nodeIdentifier: String? { nonEmpty(snapshot.identifier) }
    var nodeLabel: String? { nonEmpty(snapshot.label) }
    var nodeValue: String? { snapshot.value as? String }
    var nodeTraits: [String] {
        traitTokens(
            elementType: snapshot.elementType, isEnabled: snapshot.isEnabled, isSelected: snapshot.isSelected
        )
    }
    var nodeFrame: (x: Double, y: Double, width: Double, height: Double) { frameTuple(snapshot.frame) }
    var nodeChildren: [SnapshotNode] { snapshot.children.map(SnapshotNodeAdapter.init) }
}

// MARK: - Attribute normalization (shared by the snapshot walk and the tap-time re-check)

private func nonEmpty(_ s: String) -> String? {
    s.isEmpty ? nil : s
}

private func frameTuple(_ f: CGRect) -> (x: Double, y: Double, width: Double, height: Double) {
    (Double(f.origin.x), Double(f.origin.y), Double(f.size.width), Double(f.size.height))
}

private func traitTokens(
    elementType: XCUIElement.ElementType, isEnabled: Bool, isSelected: Bool
) -> [String] {
    var out = [typeName(elementType)]
    if !isEnabled { out.append("notEnabled") }  // base.Trait.NOT_ENABLED
    if isSelected { out.append("selected") }  // base.Trait.SELECTED
    return out
}

/// Map `XCUIElement.ElementType` to the same lower-camel token the backend-agnostic trait vocabulary uses
/// (`AXButton` -> `button`), so a `traits:` selector resolves identically across backends.
private func typeName(_ t: XCUIElement.ElementType) -> String {
    switch t {
    case .button: return "button"
    case .staticText: return "staticText"
    case .cell: return "cell"
    case .tabBar: return "tabBar"
    case .navigationBar: return "navigationBar"
    case .toolbar: return "toolbar"
    case .image: return "image"
    case .textField: return "textField"
    case .secureTextField: return "secureTextField"
    case .searchField: return "searchField"
    case .textView: return "textView"
    case .switch: return "switch"
    case .link: return "link"
    case .slider: return "slider"
    case .table: return "table"
    case .collectionView: return "collectionView"
    case .scrollView: return "scrollView"
    case .alert: return "alert"
    case .sheet: return "sheet"
    case .pageIndicator: return "pageIndicator"
    case .segmentedControl: return "segmentedControl"
    case .picker: return "picker"
    case .pickerWheel: return "pickerWheel"
    case .keyboard: return "keyboard"
    case .other: return "other"
    default: return "other"
    }
}
