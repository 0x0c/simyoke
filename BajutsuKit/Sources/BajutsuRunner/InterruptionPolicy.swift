import Foundation

/// One rule of the interruption policy: the prompt's identifying labels, and the label to tap on it.
///
/// The Python mirror is `ResolvedAlertRule` (`bajutsu/common/orchestrator/types.py`). Matching
/// requires the prompt's *full* label set, since a single shared label (e.g. "Allow") cannot tell
/// two covered prompts apart.
public struct InterruptionRule: Sendable, Equatable {
    public let identify: [String]
    public let tap: String

    public init(identify: [String], tap: String) {
        self.identify = identify
        self.tap = tap
    }
}

/// Which button to press on an out-of-process alert that interrupts an XCUITest interaction.
///
/// This is a *mechanism*, not a policy: every label in it is resolved on the Python side from the
/// scenario's own `systemAlertHandling.rules`. Only the rules this surface can actually meet are
/// sent — an alert raised inside the application's own process never interrupts an XCUITest
/// interaction, so its rules are dropped rather than matched here (BE-0406). What lives in Swift is
/// only the matching discipline: a rule wins when every one of its identifying labels is on the
/// alert exactly once. "Exactly once", not merely present, so an alert with two identically labelled
/// buttons never resolves to whichever matched first (determinism first).
///
/// `governs` is true for any scenario whose guard is on, independent of whether any rule survived
/// the drop above (BE-0406 Unit 2b): a real declaration filtered down to nothing this surface can
/// act on is not the same as no declaration at all, and only the latter — an absent guard — should
/// still leave a declined alert unreported.
///
/// It exists because XCUITest resolves an interrupting alert *before* it synthesizes any element
/// interaction, and with no monitor registered it falls back to its own default handler, which taps
/// the alert's **default** button — granting a permission the scenario may have refused, invisibly.
/// A monitor that declines to act cannot replace that handler: XCUITest verifies the alert is gone
/// and, finding it still up, re-invokes the monitor on the very next interaction, which in a
/// resident runner is an unbounded loop. So the monitor has to actually answer, and this is what
/// tells it how — and when it cannot, `governs` is what tells the caller whether that decline is
/// worth reporting.
public struct InterruptionPolicy: Sendable, Equatable {
    public let rules: [InterruptionRule]
    public let governs: Bool

    public init(rules: [InterruptionRule] = [], governs: Bool = false) {
        self.rules = rules
        self.governs = governs
    }

    /// The label to tap on an alert offering `buttons`, or nil when no rule identifies it.
    ///
    /// Nil is the signal to decline the interruption rather than guess. Declining hands the alert
    /// back to XCUITest's default handler — no worse than before for a prompt no scenario
    /// described, and never a loop, because that handler does clear the alert. Since BE-0406 Unit 2b
    /// there is no built-in fallback here: an alert no rule identifies is always declined, and
    /// `governs` decides only whether that decline gets recorded.
    public func label(for buttons: [String]) -> String? {
        func presentExactlyOnce(_ label: String) -> Bool {
            buttons.filter { $0 == label }.count == 1
        }
        for rule in rules where rule.identify.allSatisfy(presentExactlyOnce) {
            return rule.tap
        }
        return nil
    }
}

/// The identifier SpringBoard gives a foreground notification banner (BE-0416 Unit 1).
///
/// Measured on iOS 26.5: the banner is a SpringBoard element carrying this identifier, with the
/// notification's own text as its label and a single descendant button — `ShortLook.Platter.Content
/// .Seamless` — whose label is that same text. That button is the *open the notification's app*
/// affordance, so nothing here may tap it; a banner is cleared by an upward swipe instead.
public let notificationBannerIdentifier = "NotificationShortLookView"

/// Whether an interrupting element is a foreground notification banner rather than an alert.
///
/// Kept beside the policy, and matched on the identifier alone, because the two facts that would
/// otherwise distinguish a banner are both unusable: its element type is an undocumented raw value,
/// and its only button's label is the notification's own text, which varies per notification and per
/// locale.
///
/// The distinction matters twice over. A banner reaching `InterruptionPolicy.label(for:)` can never
/// match a rule — the scenario declares prompts by button label, and a banner has no such button —
/// so before this existed a governed run recorded it as an *undeclared interruption* and failed an
/// otherwise-passing step, naming the notification's body text as a button it had expected to find
/// (BE-0416 Unit 1, measured). And a banner is the one interruption XCUITest's own default handler
/// cannot press a button on: it waits the banner out instead, which is why an interrupted
/// interaction with an auto-dismissing banner cost ~9s against ~0.6s undisturbed. A persistent-style
/// banner never auto-dismisses; Unit 1 measured XCUITest clearing that one by some other means, in
/// ~3.5s — so the wait-it-out account, and the ~9s it explains, hold for the auto-dismissing case.
public func isNotificationBanner(identifier: String) -> Bool {
    identifier == notificationBannerIdentifier
}

/// SpringBoard's own bundle identifier, shared by every site that opens an `XCUIApplication` handle
/// onto it — the system-alert query, the banner query, and the banner's own dismiss swipe.
public let springboardBundleID = "com.apple.springboard"

/// The live interruption policy, plus what the monitor has tapped, declined, and swiped away since
/// the last drain.
///
/// A shared store rather than a value threaded through `ElementProviding`, because the two sides
/// that need it never meet: the policy arrives on a server thread (`POST /interruptionPolicy`),
/// while the monitor that reads it runs on XCTest's own thread whenever an interaction is
/// interrupted. Every access is under one lock for that reason.
///
/// The drained tapped labels are what lets the Python side report an interruption-time dismissal as
/// an `AlertEvent`, so a prompt answered here is not silently missing from the run's report. The
/// drained declined button lists do the same for an alert nothing answered on the scenario's behalf
/// (BE-0406 Unit 2b), and the drained banners do it for a foreground notification banner swiped away
/// (BE-0416): the failure mode this whole mechanism exists to end covers all three.
public final class InterruptionPolicyStore: @unchecked Sendable {
    public static let shared = InterruptionPolicyStore()

    private let lock = NSLock()
    private var _policy = InterruptionPolicy()
    private var _tapped: [String] = []
    private var _declined: [[String]] = []
    private var _banners: [String] = []

    public init() {}

    public var policy: InterruptionPolicy {
        lock.lock()
        defer { lock.unlock() }
        return _policy
    }

    /// Replaces the policy. Also clears the pending drain: what's queued belongs to whichever
    /// scenario set the policy it happened under, so carrying it into the next one would misreport
    /// it.
    public func setPolicy(_ policy: InterruptionPolicy) {
        lock.lock()
        defer { lock.unlock() }
        _policy = policy
        _tapped = []
        _declined = []
        _banners = []
    }

    public func record(_ label: String) {
        lock.lock()
        defer { lock.unlock() }
        _tapped.append(label)
    }

    /// Records the buttons of an alert `governs` covered but no rule identified, before declining.
    public func recordDeclined(_ buttons: [String]) {
        lock.lock()
        defer { lock.unlock() }
        _declined.append(buttons)
    }

    /// Records a notification banner the monitor swiped away, by the notification's own text.
    ///
    /// Separate from `record` because the two are not the same event: `record` names the *button*
    /// the policy chose, while a banner has none and is identified by its content. The Python side
    /// keeps them apart in the report for the same reason (BE-0416).
    public func recordBanner(_ label: String) {
        lock.lock()
        defer { lock.unlock() }
        _banners.append(label)
    }

    /// Returns what was tapped, declined and swiped away since the last drain, and clears all three.
    public func drain() -> (tapped: [String], declined: [[String]], banners: [String]) {
        lock.lock()
        defer { lock.unlock() }
        let tapped = _tapped
        let declined = _declined
        let banners = _banners
        _tapped = []
        _declined = []
        _banners = []
        return (tapped, declined, banners)
    }
}
