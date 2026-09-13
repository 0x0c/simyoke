import Foundation
import XCTest
import BajutsuRunner

/// The resident XCUITest runner (BE-0019). Python's `XcuitestEnvironment` starts this via
/// `xcodebuild test-without-building`; it launches the app under test by the forwarded bundle id,
/// serves the loopback actuation endpoints, and stays alive until the Python side tears it down.
final class RunnerUITest: XCTestCase {
    // Set once the server has bound its port. Before that, any recorded failure is a *startup*
    // failure (see `record(_:)`); after it, a recorded soft failure is an operational blip that
    // `continueAfterFailure` tolerates.
    private var serving = false

    override func setUpWithError() throws {
        // The runner is a resident server handling many operations over one long-lived test method,
        // so a single soft XCUITest failure (e.g. a pinch/rotate on a small element that XCUITest
        // flags but still performs) must not end the test and tear the server down — that would
        // leave every later request with "connection refused". This covers only *recorded* soft
        // failures; a *raised* NSException (an interaction or an `app.snapshot()` query that fails
        // when the screen is in flux — "No matches found", a failed snapshot) unwinds past this and
        // would abort the runner regardless. `APIHandler` catches that at every handler boundary
        // (`caught`: actuation → stale, query → empty screen, screenshot → 500), so the two
        // together keep the runner serving; a genuinely failed operation still surfaces to the Python
        // side through its response status.
        continueAfterFailure = true

        // Take the answer to an out-of-process system prompt away from XCUITest. Before it
        // synthesizes any element interaction — and before some application-level queries — XCUITest
        // resolves whatever alert is interrupting; with no monitor registered it falls back to its
        // own default handler, which taps the alert's **default** button. Measured on iOS 18.6, that
        // grants the notification authorization request ("Allow") the moment any tap lands while the
        // request is up: the opposite of the scenario's own policy, invisible to the run (no step
        // fails, no `AlertEvent` is reported), and intermittent, since it depends on an interaction
        // coinciding with the prompt.
        //
        // The monitor answers instead, by the labels `InterruptionPolicy` carries — every one of
        // them resolved on the Python side from the scenario's `systemAlertHandling` and pushed over
        // `POST /interruptionPolicy`. It must actually press a button rather than merely claim the
        // alert: XCUITest verifies the interruption cleared, and a monitor that returns `true` while
        // the alert is still up is re-invoked on the very next interaction — in a resident runner
        // that serves queries continuously, an unbounded loop that takes the runner down with it
        // (measured). Declining (`false`) when no rule identifies this alert is therefore the only
        // safe fallback: it hands the alert back to XCUITest's default handler, which is what
        // happened before this monitor existed and does clear it. Since BE-0406 Unit 2b, a decline
        // the policy `governs` is recorded before it happens, so the step or `expect` that met it
        // can fail by name instead of continuing as if nothing had answered on the scenario's
        // behalf.
        _ = addUIInterruptionMonitor(withDescription: "bajutsu: the scenario's policy answers system alerts") { alert in
            // Answered before the policy is consulted at all — in particular before the `governs`
            // guard, since neither branch below is right for a banner (see `isNotificationBanner`,
            // BE-0416): a governed run would record it as an undeclared interruption and fail a
            // passing step, an ungoverned one would leave XCUITest to wait it out.
            if isNotificationBanner(identifier: alert.identifier) {
                return Self.swipeAwayNotificationBanner(alert)
            }
            let policy = InterruptionPolicyStore.shared.policy
            guard policy.governs else { return false }
            let buttons = alert.buttons
            let labels = (0..<buttons.count).map { buttons.element(boundBy: $0).label }
            guard let label = policy.label(for: labels) else {
                // A `labels` carrying no real button text is the same benign race `button.exists`
                // guards below: the alert lost the race with its own dismissal between XCUITest
                // flagging the interruption and this query — `buttons.count` can already reflect
                // the vanished alert while each individual `.label` resolves to `""`, so this
                // checks every element rather than the array's emptiness alone. Recording it would
                // fail an otherwise-passing step over nothing, naming no buttons at all (BE-0406
                // Unit 2b).
                if labels.contains(where: { !$0.isEmpty }) {
                    InterruptionPolicyStore.shared.recordDeclined(labels)
                }
                return false
            }
            guard let ordinal = labels.firstIndex(of: label) else { return false }
            let button = buttons.element(boundBy: ordinal)
            guard button.exists else { return false }
            button.tap()
            // Recorded so the driver can report this as an `AlertEvent`: a prompt answered here
            // being missing from the report is the failure this mechanism exists to end.
            InterruptionPolicyStore.shared.record(label)
            return true
        }
    }

    /// Clear an interrupting notification banner with an upward swipe, and report whether it went.
    ///
    /// A swipe, not a tap: the banner's one button opens the notification's own app, which would
    /// navigate the run away from the scenario under test. The gesture is anchored to the banner's
    /// measured frame rather than a fixed screen coordinate, so it holds across device sizes, and it
    /// ends just above that frame rather than past the screen's edge, where the drag would become
    /// SpringBoard's own top-edge gesture instead.
    ///
    /// Returning `true` claims the interruption, and XCUITest re-invokes a monitor that claimed one
    /// it did not actually clear — the same unbounded reinvocation loop BE-0399 measured for an
    /// alert (above), reproduced for a banner while measuring BE-0416 by capping a throwaway test
    /// monitor at six invocations rather than letting it run to the failure BE-0399 hit. So the
    /// clearance is *confirmed* before claiming it, and an unconfirmed swipe declines instead,
    /// handing the banner back to XCUITest's own handler, which does clear it.
    ///
    /// A decline here still needs to be *legible*, or it reproduces in miniature the invisibility
    /// this whole mechanism exists to end: nothing calls `recordDeclined` for a banner (that path
    /// classifies as an `UndeclaredInterruption` and would fail the step, the outcome BE-0416 removes
    /// for a banner), so an unconfirmed swipe is logged instead — observable without being fatal.
    private static func swipeAwayNotificationBanner(_ banner: XCUIElement) -> Bool {
        let frame = banner.frame
        guard frame.height > 0 else {
            logUnconfirmedBanner("the interrupting element reported an empty frame")
            return false
        }
        // Read before the swipe: once the banner is gone its label resolves to empty, and the label
        // is the only thing that identifies the dismissal in the run's report.
        let label = banner.label
        let springboard = XCUIApplication(bundleIdentifier: springboardBundleID)
        let origin = springboard.coordinate(withNormalizedOffset: .zero)
        let from = origin.withOffset(CGVector(dx: frame.midX, dy: frame.midY))
        let to = origin.withOffset(
            CGVector(dx: frame.midX, dy: max(bannerSwipeTopMargin, frame.minY - bannerSwipeTravel))
        )
        // Re-checked immediately before the gesture, mirroring the alert branch's own `button.exists`
        // guard above: the banner can lose the race with its own auto-dismissal between the frame
        // read and this press, and a flick delivered at its former screen position would then land on
        // whatever the application under test draws there instead — a gesture the scenario never
        // asked for.
        guard banner.exists else {
            logUnconfirmedBanner("the banner disappeared before the swipe could be sent")
            return false
        }
        from.press(forDuration: bannerSwipePressDuration, thenDragTo: to)

        let remaining = springboard.descendants(matching: .any)
            .matching(identifier: notificationBannerIdentifier)
        let deadline = Date().addingTimeInterval(bannerClearanceTimeout)
        repeat {
            if !remaining.firstMatch.exists {
                InterruptionPolicyStore.shared.recordBanner(label)
                return true
            }
        } while Date() < deadline
        logUnconfirmedBanner("the banner was still on screen \(bannerClearanceTimeout)s after the swipe")
        return false
    }

    private static func logUnconfirmedBanner(_ reason: String) {
        FileHandle.standardError.write(
            Data("bajutsu runner: notification banner swipe unconfirmed (\(reason)) — declining to XCUITest's own handler\n".utf8)
        )
    }

    /// How far above the banner's own top edge the swipe ends. Measured sufficient on iOS 26.5;
    /// the gesture only has to carry the banner into its dismissal, not off the screen.
    private static let bannerSwipeTravel: CGFloat = 20

    /// The highest point the swipe may end at. SpringBoard claims a drag that starts or ends within
    /// a few points of the top edge as its own notification-shade gesture, which would pull the
    /// shade down instead of dismissing the banner.
    private static let bannerSwipeTopMargin: CGFloat = 8

    /// How long the gesture presses before it starts travelling — half the `swipe` path's own 0.1s,
    /// so the drag reads as a dismissal rather than a long press. It is not what makes the gesture a
    /// flick: `press(forDuration:thenDragTo:)` takes no velocity, so the traversal runs at XCUITest's
    /// `.default` speed either way. The knob that would change that is `withVelocity:`, which `scroll`
    /// passes precisely because it is "the whole of what makes the gesture non-inertial"
    /// (`XcuitestElementProvider.scrollVelocity`, BE-0400).
    private static let bannerSwipePressDuration: TimeInterval = 0.05

    /// How long the swipe's clearance is re-checked before it counts as unconfirmed. A deadline, not
    /// a sample count: a fixed count of SpringBoard queries scales with host speed, so it can decide
    /// "unconfirmed" for a swipe that actually landed on a slow host, or block a fast one for far
    /// longer than the ~32ms per query measured while sizing this. A deadline instead gives the
    /// dismissal animation the same real time on every host, while each poll is still a real
    /// SpringBoard query rather than a sleep (determinism first).
    private static let bannerClearanceTimeout: TimeInterval = 2

    override func record(_ issue: XCTIssue) {
        super.record(issue)
        // A failure recorded before the server binds is a startup failure — chiefly the intermittent
        // iOS 26 launch/attach timeout ("Failed to launch …: Timed out attempting to launch app"),
        // which XCUITest gives its own ~39s ceiling. It leaves the runner unusable: the health server
        // below never binds, and — worse — the `xcodebuild` host does not always exit promptly once
        // the test unwinds, so the Python cold-spawn wait burns its whole 300s ceiling on a runner
        // that will never come up, a stall that earns no retry. Exit at once so the Python side
        // observes a dead process and retries with a fresh cold spawn; the launch timeout is
        // intermittent, so a retry usually lands. Once resident (`serving`), a recorded soft failure
        // is the operational blip `continueAfterFailure` deliberately tolerates and must NOT end the
        // runner (`APIHandler` already contains it at each handler boundary).
        guard !serving else { return }
        FileHandle.standardError.write(
            Data("bajutsu runner: startup failure before the server bound — exiting for a fresh cold spawn: \(issue.compactDescription)\n".utf8)
        )
        _exit(EXIT_FAILURE)
    }

    func testServeUntilTornDown() throws {
        let app: XCUIApplication
        if let bundleId = RunnerServer.forwardedBundleId {
            app = XCUIApplication(bundleIdentifier: bundleId)
        } else {
            app = XCUIApplication()
        }
        for (key, value) in RunnerServer.forwardedLaunchEnvironment {
            app.launchEnvironment[key] = value
        }
        app.launchArguments += RunnerServer.forwardedLaunchArguments

        // Backstop for the rarer shape of the iOS 26 launch flake: a launch that hangs *silently* —
        // `launch()` never returns and never records a failure, so `record(_:)` above never fires and
        // the server below never binds. (The common shape — a launch that times out and *records* a
        // failure at XCUITest's own ~39s ceiling — is caught by `record(_:)`.) Without this, a silent
        // hang would leave the Python cold-spawn wait to burn its whole 300s ceiling with no retry.
        // The watchdog force-exits the runner if `launch()` overruns, turning the hang into the fast
        // process exit the Python side retries with a fresh cold spawn. `launch()` is fast once the
        // XCTest host is up — host boot, the slow part of a cold start, is already done here — so the
        // ceiling clears a healthy launch by a wide margin and fires only on a true hang.
        let launchWatchdog = LaunchWatchdog(timeout: 90)
        app.launch()
        launchWatchdog.disarm()

        let provider = XcuitestElementProvider(app: app)
        let server = RunnerServer(provider: provider)
        let port = try server.startFromEnvironment()
        XCTAssertGreaterThan(port, 0, "runner server did not bind a port")
        // Resident from here: the runner is healthy, so later recorded soft failures are operational
        // and `record(_:)` must stop force-exiting on them.
        serving = true
        defer { server.stop() }

        // Stay resident: pump the main run loop (servicing the server thread's
        // DispatchQueue.main work) until Python terminates the process at teardown.
        while true {
            RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 1))
        }
    }
}

/// Force-exits the runner if `app.launch()` overruns *timeout*, so an intermittent iOS 26 Simulator
/// launch hang becomes the fast process exit the Python cold-spawn retry heals — not a stall that
/// consumes the whole startup budget and earns no retry. `disarm()` is called the instant `launch()`
/// returns, so a healthy launch (well under the ceiling) never trips it.
private final class LaunchWatchdog {
    private let lock = NSLock()
    private var completed = false

    init(timeout: TimeInterval) {
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { [weak self] in
            guard let self else { return }
            self.lock.lock()
            let done = self.completed
            self.lock.unlock()
            guard !done else { return }
            FileHandle.standardError.write(
                Data("bajutsu runner: app.launch() exceeded \(Int(timeout))s — exiting for a fresh cold spawn\n".utf8)
            )
            // `_exit` (not `exit`) so no atexit handler can deadlock on a lock the stuck launch holds.
            _exit(EXIT_FAILURE)
        }
    }

    func disarm() {
        lock.lock()
        completed = true
        lock.unlock()
    }
}
