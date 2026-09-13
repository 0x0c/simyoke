import Foundation
import XCTest
@testable import BajutsuRunner

/// Unit 4 points the runner's socket at the generated handlers (BE-0381), and this is the
/// over-the-wire half of the evidence that the flip is safe. Each test drives the **live server** —
/// accept loop, byte-by-byte parser, `LegacyBackedTransport`, the generated handler, and the
/// response framing — then compares what came back against what the legacy `Router` produces for
/// the same request and the same provider state.
///
/// `APIHandlerParityTests` already compares the two handler implementations in isolation. What only
/// this suite can catch is what lives between them and the socket: an operation that never
/// registered, a status code the transport dropped on the way out, or a reply framed with the wrong
/// content type.
final class TransportParityTests: XCTestCase {
    private var provider: FakeElementProvider!
    private var server: RunnerServer!
    private var port: UInt16!

    /// The reference stack: a second provider, scripted identically, behind the legacy router. It is
    /// separate from the live server's because each mints handles into its own `SnapshotStore`.
    private var legacyProvider: FakeElementProvider!
    private var legacyRouter: Router!

    override func setUpWithError() throws {
        try super.setUpWithError()
        provider = FakeElementProvider()
        legacyProvider = FakeElementProvider()
        legacyRouter = Router(provider: legacyProvider)
        server = RunnerServer(provider: provider)
        port = try server.start()
    }

    override func tearDown() {
        server.stop()
        super.tearDown()
    }

    // MARK: - Harness

    private struct Reply {
        let statusCode: Int
        let contentType: String?
        let body: Data
    }

    /// Script both stacks alike, so the request's path through the runner is the only difference.
    private func script(_ apply: (FakeElementProvider) -> Void) {
        apply(provider)
        apply(legacyProvider)
    }

    private func wire(_ method: String, _ path: String, json: [String: Any]? = nil) throws -> Reply {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port!)\(path)")!)
        request.httpMethod = method
        if let json {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: json)
        }
        return try send(request)
    }

    private func send(_ request: URLRequest) throws -> Reply {
        let arrived = XCTestExpectation(description: "\(request.httpMethod ?? "") \(request.url?.path ?? "")")
        var reply: Reply?
        URLSession.shared.dataTask(with: request) { data, response, _ in
            if let http = response as? HTTPURLResponse {
                reply = Reply(
                    statusCode: http.statusCode,
                    contentType: http.value(forHTTPHeaderField: "Content-Type"),
                    body: data ?? Data()
                )
            }
            arrived.fulfill()
        }.resume()
        wait(for: [arrived], timeout: 5)
        return try XCTUnwrap(reply, "the runner returned no HTTP response")
    }

    private func reference(_ method: String, _ path: String, json: [String: Any]? = nil) throws -> Reply {
        let body = try json.map { try JSONSerialization.data(withJSONObject: $0) }
        let response = legacyRouter.handle(HTTPRequest(method: method, path: path, body: body))
        return Reply(
            statusCode: response.statusCode, contentType: response.contentType, body: response.body
        )
    }

    /// Compare a live reply against the legacy one on everything the driver can observe.
    ///
    /// The body is compared as decoded JSON rather than as bytes, because `JSONSerialization` and
    /// `JSONEncoder` do not agree on key order and order is not part of the contract. The content
    /// type is compared on its media type alone, for the reason
    /// `testJSONRepliesGainACharsetParameter` records.
    private func assertSame(
        _ live: Reply, _ legacy: Reply, _ what: String,
        ignoringKeys: Set<String> = [],
        file: StaticString = #filePath, line: UInt = #line
    ) throws {
        XCTAssertEqual(live.statusCode, legacy.statusCode, "\(what): status code", file: file, line: line)
        XCTAssertEqual(
            mediaType(live.contentType), mediaType(legacy.contentType),
            "\(what): content type", file: file, line: line
        )
        let liveBody = try JSONSerialization.jsonObject(
            with: live.body, options: [.mutableContainers]
        ) as? NSMutableDictionary
        let legacyBody = try JSONSerialization.jsonObject(
            with: legacy.body, options: [.mutableContainers]
        ) as? NSMutableDictionary
        for key in ignoringKeys {
            liveBody?.removeObject(forKey: key)
            legacyBody?.removeObject(forKey: key)
        }
        XCTAssertEqual(liveBody, legacyBody, "\(what): reply body", file: file, line: line)
    }

    private func mediaType(_ header: String?) -> String? {
        header?.split(separator: ";").first.map { $0.trimmingCharacters(in: .whitespaces).lowercased() }
    }

    private static let sample = ElementSnapshot(
        identifier: "submit", label: "Submit", value: "v",
        traits: ["button"], frame: (10, 20, 80, 44), backingElement: NSObject()
    )

    /// A handle minted by each stack's own `/elements`, since each keys into its own store.
    private func handles() throws -> (live: String, legacy: String) {
        (try firstHandle(of: try wire("GET", "/elements")),
         try firstHandle(of: try reference("GET", "/elements")))
    }

    private func alertHandles() throws -> (live: String, legacy: String) {
        (try firstHandle(of: try wire("POST", "/systemAlert/query", json: [:])),
         try firstHandle(of: try reference("POST", "/systemAlert/query", json: [:])))
    }

    private func firstHandle(of reply: Reply) throws -> String {
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: reply.body) as? [String: Any])
        let elements = try XCTUnwrap(json["elements"] as? [[String: Any]])
        return try XCTUnwrap(elements.first?["handle"] as? String)
    }

    // MARK: - The eighteen endpoints

    func testReadsServeIdenticallyOverTheWire() throws {
        script { $0.elementsToReturn = [Self.sample] }
        script { $0.screenSizeValue = (width: 393, height: 852) }

        try assertSame(try wire("GET", "/health"), try reference("GET", "/health"), "/health")
        try assertSame(try wire("GET", "/elements"), try reference("GET", "/elements"), "/elements")
        try assertSame(try wire("GET", "/screen"), try reference("GET", "/screen"), "/screen")
    }

    /// An element carrying none of the three optional attributes: the reply must omit those keys
    /// rather than emit nulls, which is what tells the driver `None` from `""`.
    func testElementsOmitsAbsentOptionalsOverTheWire() throws {
        script {
            $0.elementsToReturn = [
                ElementSnapshot(
                    identifier: nil, label: nil, value: nil,
                    traits: [], frame: (0, 0, 1, 1), backingElement: NSObject()
                ),
            ]
        }
        try assertSame(try wire("GET", "/elements"), try reference("GET", "/elements"), "/elements (bare)")
    }

    /// The handle-addressed group, driven across the whole status vocabulary the driver matches as
    /// literals. A drifted string here is the exact failure the OpenAPI contract exists to prevent.
    func testHandleAddressedActuationsServeIdenticallyOverTheWire() throws {
        for result in [TapResult.ok, .stale, .notFound, .notHittable, .valueNotFound] {
            script {
                $0.elementsToReturn = [Self.sample]
                $0.tapResult = result
                $0.isHittableResult = result
                $0.setPickerValueResult = result
            }
            let handle = try handles()

            try assertSame(
                try wire("POST", "/tap", json: ["handle": handle.live, "taps": 2, "duration": 0.5]),
                try reference("POST", "/tap", json: ["handle": handle.legacy, "taps": 2, "duration": 0.5]),
                "/tap (\(result))",
                // `/tap`'s interruption-drain fold (BE-0407 Unit 6) is a deliberate difference from
                // the legacy `Router`, which never gains it — pinned separately in `TapDrainFoldTests`.
                ignoringKeys: ["labels", "unmatched", "banners"]
            )
            try assertSame(
                try wire("POST", "/isHittable", json: ["handle": handle.live]),
                try reference("POST", "/isHittable", json: ["handle": handle.legacy]),
                "/isHittable (\(result))"
            )
            try assertSame(
                try wire("POST", "/gesture", json: ["handle": handle.live, "kind": "pinch", "scale": 2.0]),
                try reference("POST", "/gesture", json: ["handle": handle.legacy, "kind": "pinch", "scale": 2.0]),
                "/gesture (\(result))"
            )
            try assertSame(
                try wire("POST", "/setPickerValue", json: ["handle": handle.live, "value": "Tokyo"]),
                try reference("POST", "/setPickerValue", json: ["handle": handle.legacy, "value": "Tokyo"]),
                "/setPickerValue (\(result))"
            )
        }
    }

    /// The tap arguments have to survive the generated decoder, not just the reply shape: a `taps`
    /// or `duration` lost on the way in would still answer `ok`.
    func testTapArgumentsReachTheProviderOverTheWire() throws {
        script { $0.elementsToReturn = [Self.sample] }
        _ = try wire("POST", "/tap", json: ["handle": try handles().live, "taps": 2, "duration": 0.5])
        XCTAssertEqual(provider.tapCalls.count, 1)
        XCTAssertEqual(provider.tapCalls.first?.taps, 2)
        XCTAssertEqual(provider.tapCalls.first?.duration, 0.5)
    }

    func testCoordinateAndDragActuationsServeIdenticallyOverTheWire() throws {
        try assertSame(
            try wire("POST", "/tap", json: ["point": [12.5, 34]]),
            try reference("POST", "/tap", json: ["point": [12.5, 34]]),
            "/tap (coordinate)",
            ignoringKeys: ["labels", "unmatched", "banners"]
        )
        XCTAssertEqual(provider.tapPointCalls.count, 1, "the coordinate path must reach the provider")
        try assertSame(
            try wire("POST", "/swipe", json: ["from": [1, 2], "to": [3, 4]]),
            try reference("POST", "/swipe", json: ["from": [1, 2], "to": [3, 4]]),
            "/swipe"
        )
        try assertSame(
            try wire("POST", "/scroll", json: ["from": [1, 2], "to": [3, 4]]),
            try reference("POST", "/scroll", json: ["from": [1, 2], "to": [3, 4]]),
            "/scroll"
        )
        XCTAssertEqual(provider.swipeCalls.count, 1)
        XCTAssertEqual(provider.scrollCalls.count, 1)
    }

    func testTextEditingServesIdenticallyOverTheWire() throws {
        try assertSame(
            try wire("POST", "/type", json: ["text": "hello"]),
            try reference("POST", "/type", json: ["text": "hello"]),
            "/type"
        )
        try assertSame(
            try wire("POST", "/deleteText", json: ["count": 3]),
            try reference("POST", "/deleteText", json: ["count": 3]),
            "/deleteText"
        )
        // The driver posts `{}` to the two that take no parameters, so that is what is sent here.
        try assertSame(
            try wire("POST", "/selectAll", json: [:]),
            try reference("POST", "/selectAll", json: [:]),
            "/selectAll"
        )
        try assertSame(
            try wire("POST", "/copy", json: [:]),
            try reference("POST", "/copy", json: [:]),
            "/copy"
        )
        XCTAssertEqual(provider.typeCalls, ["hello"])
        XCTAssertEqual(provider.deleteTextCalls, [3])
        XCTAssertEqual(provider.selectAllCalls, 1)
        XCTAssertEqual(provider.copyCalls, 1)
    }

    /// The alert pair resolves against a store of its own (BE-0316), so reading a handle minted by
    /// the query back through the tap is what would catch the wrong store surviving the port.
    func testSystemAlertPairServesIdenticallyOverTheWire() throws {
        script { $0.systemAlertButtons = [Self.sample] }
        try assertSame(
            try wire("POST", "/systemAlert/query", json: [:]),
            try reference("POST", "/systemAlert/query", json: [:]),
            "/systemAlert/query"
        )
        let handle = try alertHandles()
        try assertSame(
            try wire("POST", "/systemAlert/tap", json: ["handle": handle.live]),
            try reference("POST", "/systemAlert/tap", json: ["handle": handle.legacy]),
            "/systemAlert/tap"
        )
        XCTAssertEqual(provider.systemAlertTapCalls.count, 1, "the tap must reach the alert provider")
    }

    func testInterruptionPolicyPairServesIdenticallyOverTheWire() throws {
        // The `Router` half of this pair is hand-written rather than generated, so nothing else
        // holds the two stacks to the same answer for it. The policy the driver pushes decides
        // which button an interrupting alert receives, and a stack that quietly disagreed would
        // hand the next prompt back to XCUITest's own default handler.
        let policy: [String: Any] = [
            "rules": [["identify": ["Allow", "Don't Allow"], "tap": "Don't Allow"]],
            "governs": true,
        ]
        try assertSame(
            try wire("POST", "/interruptionPolicy", json: policy),
            try reference("POST", "/interruptionPolicy", json: policy),
            "/interruptionPolicy"
        )
        try assertSame(
            try wire("POST", "/interruptionPolicy/drain", json: [:]),
            try reference("POST", "/interruptionPolicy/drain", json: [:]),
            "/interruptionPolicy/drain"
        )
    }

    func testInterruptionPolicyDrainReportsWhatTheMonitorTapped() throws {
        // The drain is the only way a prompt answered at interruption time reaches the report, so an
        // empty drain and a lost one look alike from the driver — this pins that what the monitor
        // records comes back, once, and that a second drain is empty.
        InterruptionPolicyStore.shared.setPolicy(InterruptionPolicy(governs: true))
        InterruptionPolicyStore.shared.record("Not Now")
        let reply = try wire("POST", "/interruptionPolicy/drain", json: [:])
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: reply.body) as? [String: Any])
        XCTAssertEqual(json["labels"] as? [String], ["Not Now"])
        XCTAssertEqual(json["unmatched"] as? [[String]], [])
        let second = try wire("POST", "/interruptionPolicy/drain", json: [:])
        let secondJSON = try XCTUnwrap(
            JSONSerialization.jsonObject(with: second.body) as? [String: Any]
        )
        XCTAssertEqual(secondJSON["labels"] as? [String], [])
    }

    func testInterruptionPolicyDrainReportsWhatTheMonitorDeclined() throws {
        // A decline is not a dismissal — nothing answered it — so it comes back through the separate
        // `unmatched` field, one button list per declined alert, letting the driver fail the step or
        // `expect` that met it by name instead of continuing as if nothing had happened (BE-0406
        // Unit 2b).
        InterruptionPolicyStore.shared.setPolicy(InterruptionPolicy(governs: true))
        InterruptionPolicyStore.shared.recordDeclined(["Save", "Not Now"])
        let reply = try wire("POST", "/interruptionPolicy/drain", json: [:])
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: reply.body) as? [String: Any])
        XCTAssertEqual(json["labels"] as? [String], [])
        XCTAssertEqual(json["unmatched"] as? [[String]], [["Save", "Not Now"]])
        let second = try wire("POST", "/interruptionPolicy/drain", json: [:])
        let secondJSON = try XCTUnwrap(
            JSONSerialization.jsonObject(with: second.body) as? [String: Any]
        )
        XCTAssertEqual(secondJSON["unmatched"] as? [[String]], [])
    }

    func testInterruptionPolicyDrainReportsBannersApartFromTappedLabels() throws {
        // A swiped banner is a dismissal, but not one any rule chose, so it comes back through its
        // own field rather than joining `labels` — where it would reach the report as a button that
        // was never pressed (BE-0416). It is reported whether or not the policy governs: a banner
        // offers nothing for a rule to identify, so nothing about it is the scenario's to declare.
        InterruptionPolicyStore.shared.setPolicy(InterruptionPolicy(governs: false))
        InterruptionPolicyStore.shared.recordBanner("Ready for Apple Intelligence")
        let reply = try wire("POST", "/interruptionPolicy/drain", json: [:])
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: reply.body) as? [String: Any])
        XCTAssertEqual(json["banners"] as? [String], ["Ready for Apple Intelligence"])
        XCTAssertEqual(json["labels"] as? [String], [])
        XCTAssertEqual(json["unmatched"] as? [[String]], [])
        let second = try wire("POST", "/interruptionPolicy/drain", json: [:])
        let secondJSON = try XCTUnwrap(
            JSONSerialization.jsonObject(with: second.body) as? [String: Any]
        )
        XCTAssertEqual(secondJSON["banners"] as? [String], [])
    }

    func testScreenshotServesRawPNGOverTheWire() throws {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        script { $0.screenshotData = png }
        let live = try wire("GET", "/screenshot")
        XCTAssertEqual(live.statusCode, 200)
        XCTAssertEqual(live.body, png, "the PNG must reach the driver byte for byte")
        XCTAssertEqual(mediaType(live.contentType), "image/png")
    }

    func testScreenshotFailureServesIdenticallyOverTheWire() throws {
        script { $0.screenshotData = nil }
        try assertSame(
            try wire("GET", "/screenshot"), try reference("GET", "/screenshot"), "/screenshot (failure)"
        )
    }

    // MARK: - Rejections

    func testUnknownPathIsStillA404() throws {
        try assertSame(
            try wire("GET", "/nope"), try reference("GET", "/nope"), "unknown path"
        )
    }

    /// A known path reached by the wrong method resolves no route, which is the same `default` case
    /// the legacy switch fell into.
    func testKnownPathWithTheWrongMethodIsStillA404() throws {
        try assertSame(
            try wire("GET", "/tap"), try reference("GET", "/tap"), "wrong method"
        )
    }

    /// A body the schema rejects is answered 400, as the legacy router answered its own parse
    /// failures — the message text differs, but the driver reads only `status`.
    func testMalformedBodyIsRejectedAsAJSONError() throws {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port!)/type")!)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data(#"{"text": 42}"#.utf8)
        let live = try send(request)

        XCTAssertEqual(live.statusCode, 400)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: live.body) as? [String: Any])
        XCTAssertEqual(json["status"] as? String, "error", "the driver decodes every reply as JSON")
        XCTAssertNotNil(json["message"] as? String)
        XCTAssertTrue(provider.typeCalls.isEmpty, "a rejected body must not reach the provider")
    }

    /// A deliberate wire difference, pinned so it is not mistaken for a regression: the generated
    /// serializer appends `charset=utf-8` to every JSON reply, where `Router` sent a bare
    /// `application/json`. It is safe because the driver never reads the header — `_decode` in
    /// `bajutsu/common/drivers/xcuitest.py` takes only the status code and the body — and `/screenshot`,
    /// the one reply whose type the driver does branch on by path, is unaffected.
    func testJSONRepliesGainACharsetParameter() throws {
        XCTAssertEqual(try wire("GET", "/health").contentType, "application/json; charset=utf-8")
        XCTAssertEqual(try reference("GET", "/health").contentType, "application/json")
    }
}
