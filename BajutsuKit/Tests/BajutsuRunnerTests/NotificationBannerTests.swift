import XCTest
@testable import BajutsuRunner

/// The banner half of the interruption monitor (BE-0416). Recognition is what keeps a notification
/// banner off the alert path entirely — reaching `label(for:)` is what used to fail an otherwise
/// passing step — and the store is what carries the swipe into the run's report.
final class NotificationBannerTests: XCTestCase {
    override func setUp() {
        super.setUp()
        InterruptionPolicyStore.shared.setPolicy(InterruptionPolicy())
    }

    func testRecognizesSpringBoardsBannerIdentifier() {
        // Measured identical on iOS 18.6 and 26.5 (BE-0416 Unit 1).
        XCTAssertTrue(isNotificationBanner(identifier: "NotificationShortLookView"))
    }

    func testDoesNotRecognizeAnAlertOrAnEmptyIdentifier() {
        XCTAssertFalse(isNotificationBanner(identifier: ""))
        XCTAssertFalse(isNotificationBanner(identifier: "ShortLook.Platter.Content.Seamless"))
    }

    func testABannersOwnButtonCanNeverIdentifyARule() {
        // Why recognition has to happen before the policy is consulted: a banner's only button is
        // the notification's own text, so a governed run reached `label(for:)`, found nothing, and
        // recorded an undeclared interruption that failed the step (BE-0416 Unit 1, measured).
        let policy = InterruptionPolicy(
            rules: [InterruptionRule(identify: ["Allow", "Don't Allow"], tap: "Don't Allow")],
            governs: true
        )
        XCTAssertNil(policy.label(for: ["now, Probe Banner 1, some notification body."]))
    }

    func testDrainReportsSwipedBannersApartFromTappedLabels() {
        let store = InterruptionPolicyStore.shared
        store.record("Not Now")
        store.recordBanner("Ready for Apple Intelligence")
        let drained = store.drain()
        XCTAssertEqual(drained.tapped, ["Not Now"])
        XCTAssertEqual(drained.banners, ["Ready for Apple Intelligence"])
        XCTAssertTrue(drained.declined.isEmpty)
    }

    func testDrainClearsTheBannersItReported() {
        let store = InterruptionPolicyStore.shared
        store.recordBanner("first")
        XCTAssertEqual(store.drain().banners, ["first"])
        XCTAssertTrue(store.drain().banners.isEmpty)
    }

    func testSettingAPolicyDropsBannersQueuedUnderThePreviousOne() {
        // Same reason the tapped and declined queues are cleared: what is pending belongs to
        // whichever scenario set the policy it happened under.
        let store = InterruptionPolicyStore.shared
        store.recordBanner("from the previous scenario")
        store.setPolicy(InterruptionPolicy(rules: [], governs: true))
        XCTAssertTrue(store.drain().banners.isEmpty)
    }

    func testBannersAreOrderedOldestFirst() {
        let store = InterruptionPolicyStore.shared
        store.recordBanner("first")
        store.recordBanner("second")
        XCTAssertEqual(store.drain().banners, ["first", "second"])
    }
}
