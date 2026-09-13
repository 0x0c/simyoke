import Foundation
import XCTest
@testable import BajutsuRunner

/// Unit 4 flips the driver from the hand-rolled `Router` to the generated path endpoint by
/// endpoint. These tests are the evidence that flip is safe: each drives **both** implementations
/// from the same provider state and the same request, then compares the JSON they produce. A
/// difference in any field, status string, or optional-versus-absent key fails here rather than on
/// a device.
///
/// The comparison is on decoded JSON rather than raw bytes because `JSONSerialization` (the legacy
/// path) and `JSONEncoder` (the generated path) do not agree on key order, which is not part of the
/// contract. Everything that *is* part of the contract — which keys exist, and what they hold — is
/// compared exactly.
final class APIHandlerParityTests: XCTestCase {
    // MARK: - Harness

    /// The legacy reply for *method* and *path*, as a JSON object.
    private func legacy(
        _ method: String, _ path: String, body: [String: Any]? = nil, provider: FakeElementProvider
    ) throws -> [String: Any] {
        let data = try body.map { try JSONSerialization.data(withJSONObject: $0) }
        let response = Router(provider: provider).handle(
            HTTPRequest(method: method, path: path, body: data)
        )
        return try XCTUnwrap(
            JSONSerialization.jsonObject(with: response.body) as? [String: Any],
            "legacy \(method) \(path) did not return a JSON object"
        )
    }

    /// The generated reply for the same operation, encoded back to JSON so the two are comparable.
    private func generated<T: Encodable>(_ value: T) throws -> [String: Any] {
        let encoder = JSONEncoder()
        return try XCTUnwrap(
            JSONSerialization.jsonObject(with: encoder.encode(value)) as? [String: Any]
        )
    }

    /// `NSDictionary` equality is deep and order-independent, which is exactly the comparison the
    /// wire contract calls for.
    private func assertSame(
        _ lhs: [String: Any], _ rhs: [String: Any], _ what: String,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        XCTAssertEqual(
            lhs as NSDictionary, rhs as NSDictionary,
            "\(what): generated reply differs from the legacy one", file: file, line: line
        )
    }

    /// `/tap`'s generated reply, minus the interruption-drain fold (BE-0407 Unit 6) that `Router`
    /// never gains — a deliberate difference the wire-shape parity below is not testing for.
    /// `TapDrainFoldTests` is where that fold itself is pinned.
    private func withoutDrainFold(_ payload: [String: Any]) -> [String: Any] {
        var payload = payload
        payload["labels"] = nil
        payload["unmatched"] = nil
        payload["banners"] = nil
        return payload
    }

    private func elementProvider(_ snapshots: [ElementSnapshot]) -> FakeElementProvider {
        let provider = FakeElementProvider()
        provider.elementsToReturn = snapshots
        return provider
    }

    private static let sample = ElementSnapshot(
        identifier: "home.title", label: "Home", value: "v",
        traits: ["staticText"], frame: (10, 20, 100, 44), backingElement: NSObject()
    )
    private static let bare = ElementSnapshot(
        identifier: nil, label: nil, value: nil,
        traits: [], frame: (0, 0, 1, 1), backingElement: NSObject()
    )

    // MARK: - Reads

    func testHealthParity() async throws {
        let provider = FakeElementProvider()
        let new = try await APIHandler(provider: provider).health(.init())
        guard case .ok(let ok) = new, case .json(let payload) = ok.body else {
            return XCTFail("unexpected health output")
        }
        assertSame(try legacy("GET", "/health", provider: provider), try generated(payload), "/health")
    }

    func testScreenParity() async throws {
        let provider = FakeElementProvider()
        provider.screenSizeValue = (width: 393, height: 852)
        let new = try await APIHandler(provider: provider).screen(.init())
        guard case .ok(let ok) = new, case .json(let payload) = ok.body else {
            return XCTFail("unexpected screen output")
        }
        // Also pins that neither side invents a `status` key here.
        assertSame(try legacy("GET", "/screen", provider: provider), try generated(payload), "/screen")
    }

    func testElementsParity() async throws {
        for (name, snapshots) in [
            ("populated", [Self.sample]),
            ("optional fields absent", [Self.bare]),
            ("empty", []),
        ] as [(String, [ElementSnapshot])] {
            let new = try await APIHandler(provider: elementProvider(snapshots)).queryElements(.init())
            guard case .ok(let ok) = new, case .json(let payload) = ok.body else {
                return XCTFail("unexpected elements output")
            }
            assertSame(
                try legacy("GET", "/elements", provider: elementProvider(snapshots)),
                try generated(payload), "/elements (\(name))"
            )
        }
    }

    func testSystemAlertQueryParity() async throws {
        let provider = FakeElementProvider()
        provider.systemAlertButtons = [Self.sample]
        let other = FakeElementProvider()
        other.systemAlertButtons = [Self.sample]
        let new = try await APIHandler(provider: provider).querySystemAlert(.init(body: .json(.init())))
        guard case .ok(let ok) = new, case .json(let payload) = ok.body else {
            return XCTFail("unexpected systemAlert/query output")
        }
        assertSame(
            try legacy("POST", "/systemAlert/query", body: [:], provider: other),
            try generated(payload), "/systemAlert/query"
        )
    }

    // MARK: - Actuation

    /// The five statuses are the contract's whole vocabulary, and the driver matches them as
    /// literals, so each has to survive the port unchanged. `/tap` never yields `.valueNotFound`
    /// on a device — only `/setValue` does (BE-0356) — but the reply mapping is shared, so driving
    /// it here covers the status without a second endpoint's worth of scaffolding.
    func testTapParityAcrossEveryStatus() async throws {
        for result in [TapResult.ok, .stale, .notFound, .notHittable, .valueNotFound] {
            let forNew = elementProvider([Self.sample]); forNew.tapResult = result
            let forLegacy = elementProvider([Self.sample]); forLegacy.tapResult = result

            let handler = APIHandler(provider: forNew)
            _ = try await handler.queryElements(.init())  // mint a handle
            let handle = try await firstHandle(handler)
            let new = try await handler.tap(.init(body: .json(.init(handle: handle))))
            guard case .ok(let ok) = new, case .json(let payload) = ok.body else {
                return XCTFail("unexpected tap output")
            }

            let router = Router(provider: forLegacy)
            let legacyHandle = try legacyFirstHandle(router)
            let response = router.handle(
                HTTPRequest(
                    method: "POST", path: "/tap",
                    body: try JSONSerialization.data(withJSONObject: ["handle": legacyHandle])
                )
            )
            let legacyJSON = try XCTUnwrap(
                JSONSerialization.jsonObject(with: response.body) as? [String: Any]
            )
            assertSame(legacyJSON, withoutDrainFold(try generated(payload)), "/tap (\(result))")
        }
    }

    func testCoordinateTapParity() async throws {
        let provider = FakeElementProvider()
        let new = try await APIHandler(provider: provider).tap(.init(body: .json(.init(point: [12.5, 34]))))
        guard case .ok(let ok) = new, case .json(let payload) = ok.body else {
            return XCTFail("unexpected tap output")
        }
        assertSame(
            try legacy("POST", "/tap", body: ["point": [12.5, 34]], provider: FakeElementProvider()),
            withoutDrainFold(try generated(payload)), "/tap (coordinate)"
        )
        XCTAssertEqual(provider.tapPointCalls.count, 1, "the coordinate path must still reach the provider")
    }

    func testDragAndTextParity() async throws {
        let cases: [(String, String, [String: Any], () async throws -> [String: Any])] = [
            ("/swipe", "/swipe", ["from": [1, 2], "to": [3, 4]], {
                let out = try await APIHandler(provider: FakeElementProvider())
                    .swipe(.init(body: .json(.init(from: [1, 2], to: [3, 4]))))
                guard case .ok(let ok) = out, case .json(let p) = ok.body else { throw ParityError.shape }
                return try self.generated(p)
            }),
            ("/scroll", "/scroll", ["from": [1, 2], "to": [3, 4]], {
                let out = try await APIHandler(provider: FakeElementProvider())
                    .scroll(.init(body: .json(.init(from: [1, 2], to: [3, 4]))))
                guard case .ok(let ok) = out, case .json(let p) = ok.body else { throw ParityError.shape }
                return try self.generated(p)
            }),
            ("/type", "/type", ["text": "hello"], {
                let out = try await APIHandler(provider: FakeElementProvider())
                    .typeText(.init(body: .json(.init(text: "hello"))))
                guard case .ok(let ok) = out, case .json(let p) = ok.body else { throw ParityError.shape }
                return try self.generated(p)
            }),
            ("/deleteText", "/deleteText", ["count": 3], {
                let out = try await APIHandler(provider: FakeElementProvider())
                    .deleteText(.init(body: .json(.init(count: 3))))
                guard case .ok(let ok) = out, case .json(let p) = ok.body else { throw ParityError.shape }
                return try self.generated(p)
            }),
            ("/selectAll", "/selectAll", [:], {
                let out = try await APIHandler(provider: FakeElementProvider())
                    .selectAll(.init(body: .json(.init())))
                guard case .ok(let ok) = out, case .json(let p) = ok.body else { throw ParityError.shape }
                return try self.generated(p)
            }),
            ("/copy", "/copy", [:], {
                let out = try await APIHandler(provider: FakeElementProvider())
                    .copySelection(.init(body: .json(.init())))
                guard case .ok(let ok) = out, case .json(let p) = ok.body else { throw ParityError.shape }
                return try self.generated(p)
            }),
        ]
        for (name, path, body, run) in cases {
            assertSame(
                try legacy("POST", path, body: body, provider: FakeElementProvider()),
                try await run(), name
            )
        }
    }

    /// `/isHittable` shares `tap`'s resolution outcomes but must not act, so it is compared across
    /// the hittable and covered results as well as the two handle-resolution failures.
    func testIsHittableParityAcrossEveryStatus() async throws {
        for result in [TapResult.ok, .notHittable, .stale, .notFound] {
            let forNew = elementProvider([Self.sample]); forNew.isHittableResult = result
            let forLegacy = elementProvider([Self.sample]); forLegacy.isHittableResult = result

            let handler = APIHandler(provider: forNew)
            let handle = try await firstHandle(handler)
            let new = try await handler.isHittable(.init(body: .json(.init(handle: handle))))
            guard case .ok(let ok) = new, case .json(let payload) = ok.body else {
                return XCTFail("unexpected isHittable output")
            }

            let router = Router(provider: forLegacy)
            let legacyHandle = try legacyFirstHandle(router)
            let response = router.handle(
                HTTPRequest(
                    method: "POST", path: "/isHittable",
                    body: try JSONSerialization.data(withJSONObject: ["handle": legacyHandle])
                )
            )
            assertSame(
                try XCTUnwrap(JSONSerialization.jsonObject(with: response.body) as? [String: Any]),
                try generated(payload), "/isHittable (\(result))"
            )
            XCTAssertTrue(forNew.tapCalls.isEmpty, "isHittable must query, never actuate")
        }
    }

    /// `/gesture` is the one operation whose kind the legacy router validates itself. Both
    /// well-formed kinds are compared here; the unknown-kind case is deliberately absent, because
    /// the schema's `enum` moves that rejection into the generated decoder — see
    /// `testUnknownGestureKindIsRejectedBeforeTheHandler`.
    func testGestureParity() async throws {
        for (kind, payloadKind) in [("pinch", Components.Schemas.GestureRequest.kindPayload.pinch),
                                    ("rotate", .rotate)] {
            let forNew = elementProvider([Self.sample])
            let forLegacy = elementProvider([Self.sample])
            let handler = APIHandler(provider: forNew)
            let handle = try await firstHandle(handler)
            let new = try await handler.gesture(
                .init(body: .json(.init(handle: handle, kind: payloadKind, scale: 2.0)))
            )
            guard case .ok(let ok) = new, case .json(let payload) = ok.body else {
                return XCTFail("unexpected gesture output")
            }

            let router = Router(provider: forLegacy)
            let legacyHandle = try legacyFirstHandle(router)
            let response = router.handle(
                HTTPRequest(
                    method: "POST", path: "/gesture",
                    body: try JSONSerialization.data(
                        withJSONObject: ["handle": legacyHandle, "kind": kind, "scale": 2.0]
                    )
                )
            )
            assertSame(
                try XCTUnwrap(JSONSerialization.jsonObject(with: response.body) as? [String: Any]),
                try generated(payload), "/gesture (\(kind))"
            )
            XCTAssertEqual(forNew.gestureCalls.first?.kind, kind)
            XCTAssertEqual(forNew.gestureCalls.first?.scale, 2.0)
        }
    }

    /// A contract change the port makes deliberately, recorded so it is not mistaken for a
    /// regression: the legacy router answered `400 {"status":"error","message":"missing or unknown
    /// gesture kind"}` for an unrecognised kind, whereas the schema's `enum: [pinch, rotate]` now
    /// rejects it in the generated decoder, before any handler runs. The rejection still happens —
    /// its body is the transport's, not this handler's.
    func testUnknownGestureKindIsRejectedBeforeTheHandler() throws {
        let body = Data(#"{"handle": "h1", "kind": "wiggle"}"#.utf8)
        XCTAssertThrowsError(
            try JSONDecoder().decode(Components.Schemas.GestureRequest.self, from: body),
            "an unknown kind must not decode"
        )
        let legacyResponse = Router(provider: FakeElementProvider()).handle(
            HTTPRequest(method: "POST", path: "/gesture", body: body)
        )
        XCTAssertEqual(legacyResponse.statusCode, 400, "the legacy router rejected it in the handler")
    }

    /// `/systemAlert/tap` is the only operation that resolves against `alertStore` rather than the
    /// app-tree `store` (BE-0316). Reading a handle minted by the alert query back out through the
    /// tap is what would catch a copy-paste of the wrong store, which no other test covers.
    func testSystemAlertTapParity() async throws {
        let forNew = FakeElementProvider(); forNew.systemAlertButtons = [Self.sample]
        let forLegacy = FakeElementProvider(); forLegacy.systemAlertButtons = [Self.sample]

        let handler = APIHandler(provider: forNew)
        let query = try await handler.querySystemAlert(.init(body: .json(.init())))
        guard case .ok(let queried) = query, case .json(let queryPayload) = queried.body,
              let handle = queryPayload.elements.first?.handle else {
            return XCTFail("the alert query minted no handle")
        }
        let new = try await handler.tapSystemAlert(.init(body: .json(.init(handle: handle))))
        guard case .ok(let ok) = new, case .json(let payload) = ok.body else {
            return XCTFail("unexpected systemAlert/tap output")
        }
        XCTAssertEqual(payload.status, .ok, "a handle from the alert store must resolve, not 404")
        XCTAssertEqual(forNew.systemAlertTapCalls.count, 1, "the tap must reach the alert provider")

        let router = Router(provider: forLegacy)
        let legacyQuery = router.handle(
            HTTPRequest(method: "POST", path: "/systemAlert/query", body: Data("{}".utf8))
        )
        let legacyJSON = try XCTUnwrap(
            JSONSerialization.jsonObject(with: legacyQuery.body) as? [String: Any]
        )
        let legacyHandle = try XCTUnwrap(
            (legacyJSON["elements"] as? [[String: Any]])?.first?["handle"] as? String
        )
        let legacyTap = router.handle(
            HTTPRequest(
                method: "POST", path: "/systemAlert/tap",
                body: try JSONSerialization.data(withJSONObject: ["handle": legacyHandle])
            )
        )
        assertSame(
            try XCTUnwrap(JSONSerialization.jsonObject(with: legacyTap.body) as? [String: Any]),
            try generated(payload), "/systemAlert/tap"
        )
    }

    /// The 200 path of `/screenshot`, which returns PNG bytes rather than JSON, so it is compared
    /// as bytes against what the legacy router wrote.
    func testScreenshotSuccessParity() async throws {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        let forNew = FakeElementProvider(); forNew.screenshotData = png
        let forLegacy = FakeElementProvider(); forLegacy.screenshotData = png

        let new = try await APIHandler(provider: forNew).screenshot(.init())
        guard case .ok(let ok) = new, case .png(let body) = ok.body else {
            return XCTFail("unexpected screenshot output")
        }
        let generatedBytes = try await Data(collecting: body, upTo: 1024)
        let legacyResponse = Router(provider: forLegacy).handle(
            HTTPRequest(method: "GET", path: "/screenshot", body: nil)
        )
        XCTAssertEqual(generatedBytes, legacyResponse.body, "/screenshot bytes differ")
        XCTAssertEqual(legacyResponse.contentType, "image/png")
    }

    /// `/setPickerValue` arrived on `main` while this work was in flight (BE-0356). It is the only
    /// operation that can report `value-not-found`, and the only one whose request carries a value
    /// alongside the handle, so both halves are compared here.
    func testSetPickerValueParityAcrossEveryStatus() async throws {
        for result in [TapResult.ok, .valueNotFound, .stale, .notFound] {
            let forNew = elementProvider([Self.sample]); forNew.setPickerValueResult = result
            let forLegacy = elementProvider([Self.sample]); forLegacy.setPickerValueResult = result

            let handler = APIHandler(provider: forNew)
            let handle = try await firstHandle(handler)
            let new = try await handler.setPickerValue(
                .init(body: .json(.init(handle: handle, value: "Tokyo")))
            )
            guard case .ok(let ok) = new, case .json(let payload) = ok.body else {
                return XCTFail("unexpected setPickerValue output")
            }

            let router = Router(provider: forLegacy)
            let legacyHandle = try legacyFirstHandle(router)
            let response = router.handle(
                HTTPRequest(
                    method: "POST", path: "/setPickerValue",
                    body: try JSONSerialization.data(
                        withJSONObject: ["handle": legacyHandle, "value": "Tokyo"]
                    )
                )
            )
            assertSame(
                try XCTUnwrap(JSONSerialization.jsonObject(with: response.body) as? [String: Any]),
                try generated(payload), "/setPickerValue (\(result))"
            )
            XCTAssertEqual(forNew.setPickerValueCalls.first?.value, "Tokyo",
                           "the requested row value must reach the provider")
        }
    }

    // MARK: - Error paths

    func testBadRequestParity() async throws {
        // A non-positive count is the one 400 both implementations derive from a well-formed body,
        // so it is the 400 that can be compared field-for-field.
        let new = try await APIHandler(provider: FakeElementProvider())
            .deleteText(.init(body: .json(.init(count: 0))))
        guard case .badRequest(let bad) = new, case .json(let payload) = bad.body else {
            return XCTFail("expected a 400 for a non-positive count")
        }
        assertSame(
            try legacy("POST", "/deleteText", body: ["count": 0], provider: FakeElementProvider()),
            try generated(payload), "/deleteText (count 0)"
        )
    }

    func testScreenshotFailureParity() async throws {
        let provider = FakeElementProvider()
        provider.screenshotData = nil
        let new = try await APIHandler(provider: provider).screenshot(.init())
        guard case .internalServerError(let error) = new, case .json(let payload) = error.body else {
            return XCTFail("expected a 500 when the capture fails")
        }
        let other = FakeElementProvider()
        other.screenshotData = nil
        assertSame(
            try legacy("GET", "/screenshot", provider: other), try generated(payload),
            "/screenshot (failure)"
        )
    }

    // MARK: - Helpers

    private enum ParityError: Error { case shape }

    private func firstHandle(_ handler: APIHandler) async throws -> String {
        let out = try await handler.queryElements(.init())
        guard case .ok(let ok) = out, case .json(let payload) = ok.body,
              let handle = payload.elements.first?.handle else { throw ParityError.shape }
        return handle
    }

    private func legacyFirstHandle(_ router: Router) throws -> String {
        let response = router.handle(HTTPRequest(method: "GET", path: "/elements", body: nil))
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: response.body) as? [String: Any])
        let elements = try XCTUnwrap(json["elements"] as? [[String: Any]])
        return try XCTUnwrap(elements.first?["handle"] as? String)
    }
}
