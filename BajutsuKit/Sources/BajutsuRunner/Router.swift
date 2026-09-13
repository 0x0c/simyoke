import Foundation
import ObjCExceptionCatcher

/// The hand-rolled router BE-0381 replaces. `RunnerServer` no longer serves through it: every route
/// is a generated handler as of Unit 4. It stays until Unit 5 because it is what the migration is
/// checked *against* — `APIHandlerParityTests`, `TransportParityTests`, and the reply-side half of
/// `ContractConformanceTests` all read the shipped behavior off this implementation and compare. Its
/// deletion and theirs are the same change. `ContractConformanceTests`'
/// `testGeneratedRequestTypesDecodeTheDriversBodies` is *not* part of it: it pins the generated
/// request types against the bodies the Python driver sends and never touches this type, so it
/// outlives the comparisons.
final class Router {
    private let provider: ElementProviding
    private let store = SnapshotStore()
    // A separate handle store for SpringBoard alert buttons (BE-0316), so their handles never
    // collide with the app tree's and a `/systemAlert/query` never disturbs the app snapshot.
    private let alertStore = SnapshotStore()
    // Serializes every XCUITest-touching operation so no two run — or *re-enter* — concurrently.
    // `app.snapshot()` / `app.screenshot()` / an `XCUIElement` interaction pumps the main run loop
    // internally while it waits on the app over XPC, and that run-loop spin drains the main dispatch
    // queue. Two concurrent `DispatchQueue.main.sync` handlers (e.g. a scenario `/elements` and an
    // evidence `/screenshot`, which the concurrent HTTP server delivers at once) would then re-enter —
    // the second XCUITest call starts on the main thread *inside* the first's snapshot — and XCUITest
    // is not re-entrant, so the XCTest host aborts (the CI-only mid-run crash: slower/contended hosts
    // widen the re-entrancy window). Held on the *connection* thread before dispatching to main, this
    // lock means a second operation never even enqueues onto main while the first is in flight, so the
    // run-loop spin has nothing re-entrant to drain. `/health` never takes it (it touches no XCUITest
    // state), so it stays answerable during a long operation — the concurrent server's whole purpose.
    private let actuationLock = NSLock()

    init(provider: ElementProviding) {
        self.provider = provider
    }

    func handle(_ request: HTTPRequest) -> HTTPResponse {
        switch (request.method, request.path) {
        case ("GET", "/health"):
            return handleHealth()
        case ("GET", "/elements"):
            return handleElements()
        case ("GET", "/screen"):
            return handleScreen()
        case ("POST", "/tap"):
            return handleTap(request)
        case ("POST", "/isHittable"):
            return handleIsHittable(request)
        case ("POST", "/gesture"):
            return handleGesture(request)
        case ("POST", "/swipe"):
            return handleSwipe(request)
        case ("POST", "/scroll"):
            return handleScroll(request)
        case ("POST", "/type"):
            return handleType(request)
        case ("POST", "/deleteText"):
            return handleDeleteText(request)
        case ("POST", "/selectAll"):
            return tapResultResponse(onMainCatching(self.provider.selectAll))
        case ("POST", "/copy"):
            return tapResultResponse(onMainCatching(self.provider.copySelection))
        case ("POST", "/setPickerValue"):
            return handleSetPickerValue(request)
        case ("POST", "/interruptionPolicy"):
            return handleSetInterruptionPolicy(request)
        case ("POST", "/interruptionPolicy/drain"):
            let drained = InterruptionPolicyStore.shared.drain()
            return .json(
                200,
                [
                    "labels": drained.tapped, "unmatched": drained.declined,
                    "banners": drained.banners,
                ]
            )
        case ("POST", "/systemAlert/query"):
            return handleSystemAlertQuery()
        case ("POST", "/systemAlert/tap"):
            return handleSystemAlertTap(request)
        case ("GET", "/screenshot"):
            return handleScreenshot()
        default:
            return .error(404, "unknown endpoint")
        }
    }

    /// Stores which button the interruption monitor presses (see `InterruptionPolicy`). Like
    /// `/health`, it never takes `actuationLock`: it touches no XCUITest state, and it has to stay
    /// answerable while the interaction an alert is interrupting is still in flight.
    private func handleSetInterruptionPolicy(_ request: HTTPRequest) -> HTTPResponse {
        guard let body = request.body,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else {
            return .error(400, "missing or invalid JSON body")
        }
        // `governs` decides whether a declined alert gets reported at all (BE-0406 Unit 2b), so a
        // missing or mistyped value is rejected rather than defaulting to `false` — silently
        // reinstating, for this legacy transport alone, the unrecorded grant the field exists to
        // close. The generated `APIHandler` path already enforces this via `openapi.yaml`'s
        // `required: [rules, governs]`; this mirrors that here.
        guard let governs = json["governs"] as? Bool else {
            return .error(400, "missing or invalid \"governs\"")
        }
        let rules: [InterruptionRule] = (json["rules"] as? [[String: Any]] ?? []).compactMap {
            guard let identify = $0["identify"] as? [String], let tap = $0["tap"] as? String else {
                return nil
            }
            return InterruptionRule(identify: identify, tap: tap)
        }
        InterruptionPolicyStore.shared.setPolicy(InterruptionPolicy(rules: rules, governs: governs))
        return .json(200, ["status": "ok"])
    }

    private func handleHealth() -> HTTPResponse {
        .json(200, ["status": "ready"])
    }

    // The device screen size, for the `scroll` action's on-screen stop condition (BE-0326). The
    // element tree excludes the app window and can hold buffered off-screen ScrollView children, so
    // the Python side cannot infer the true viewport from the tree — it reads it here instead.
    private func handleScreen() -> HTTPResponse {
        let size = onMain { self.provider.screenSize() }
        return .json(200, ["width": size.width, "height": size.height])
    }

    private func handleElements() -> HTTPResponse {
        // A raised snapshot failure (UI in flux) maps to an empty screen, the same as the provider's
        // `try? app.snapshot()` handles a *thrown* one (BE-0105): the run fails loudly downstream when
        // nothing resolves, and the runner keeps serving rather than aborting on the raise.
        let elements = caughtOnMain([]) { self.provider.queryElements() }
        return elementsResponse(store: store, elements: elements)
    }

    // The SpringBoard system-alert query (BE-0316): the same element+handle contract as `/elements`,
    // but sourced from the out-of-process permission prompt and keyed into `alertStore`. Empty when no
    // alert is up, so the Python driver polls it against the step's timeout — a condition wait.
    private func handleSystemAlertQuery() -> HTTPResponse {
        let elements = caughtOnMain([]) { self.provider.querySystemAlertButtons() }
        return elementsResponse(store: alertStore, elements: elements)
    }

    private func handleSystemAlertTap(_ request: HTTPRequest) -> HTTPResponse {
        guard let body = request.body,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else {
            return .error(400, "missing or invalid JSON body")
        }
        guard let handle = json["handle"] as? String else {
            return .error(400, "missing handle")
        }
        switch alertStore.lookup(handle: handle) {
        case .found(let snapshot):
            let result = onMainCatching {
                self.provider.tapSystemAlertButton(backingElement: snapshot.backingElement)
            }
            return tapResultResponse(result)
        case .stale:
            return .json(200, ["status": "stale"])
        case .notFound:
            return .json(200, ["status": "not-found"])
        }
    }

    /// Serialize a fresh element snapshot into the `{status, elements:[…handle…]}` reply both the app
    /// tree (`/elements`) and the SpringBoard alert (`/systemAlert/query`) share (BE-0316).
    private func elementsResponse(store: SnapshotStore, elements: [ElementSnapshot]) -> HTTPResponse {
        let entries = store.refreshSnapshot(elements: elements)
        let jsonElements: [[String: Any]] = entries.map { entry in
            var dict: [String: Any] = [
                "traits": entry.snapshot.traits,
                "frame": [
                    entry.snapshot.frame.x,
                    entry.snapshot.frame.y,
                    entry.snapshot.frame.width,
                    entry.snapshot.frame.height,
                ],
                "handle": entry.handle,
            ]
            dict["identifier"] = entry.snapshot.identifier
            dict["label"] = entry.snapshot.label
            dict["value"] = entry.snapshot.value
            return dict
        }
        return .json(200, ["status": "ok", "elements": jsonElements])
    }

    private func handleTap(_ request: HTTPRequest) -> HTTPResponse {
        guard let body = request.body,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else {
            return .error(400, "missing or invalid JSON body")
        }

        if let rawPoint = json["point"] as? [Any], rawPoint.count == 2,
           let px = (rawPoint[0] as? NSNumber)?.doubleValue,
           let py = (rawPoint[1] as? NSNumber)?.doubleValue {
            let result = onMainCatching { self.provider.tapPoint(x: px, y: py) }
            return tapResultResponse(result)
        }

        guard let handle = json["handle"] as? String else {
            return .error(400, "missing handle or point")
        }

        let taps = max((json["taps"] as? NSNumber)?.intValue ?? 1, 1)
        let duration = max((json["duration"] as? NSNumber)?.doubleValue ?? 0, 0)

        switch store.lookup(handle: handle) {
        case .found(let snapshot):
            let result = onMainCatching {
                self.provider.tap(backingElement: snapshot.backingElement, taps: taps, duration: duration)
            }
            return tapResultResponse(result)
        case .stale:
            return .json(200, ["status": "stale"])
        case .notFound:
            return .json(200, ["status": "not-found"])
        }
    }

    /// A pure query, unlike `handleTap`: reuses the same handle-resolution outcomes (`.stale` /
    /// `.notFound`), but reports `.ok`/`.notHittable` for a still-live handle without acting on it —
    /// so the driver's `scroll_until_tappable` stop condition can poll this repeatedly with no
    /// side effects.
    private func handleIsHittable(_ request: HTTPRequest) -> HTTPResponse {
        guard let body = request.body,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else {
            return .error(400, "missing or invalid JSON body")
        }
        guard let handle = json["handle"] as? String else {
            return .error(400, "missing handle")
        }
        switch store.lookup(handle: handle) {
        case .found(let snapshot):
            let result = onMainCatching {
                self.provider.isHittable(backingElement: snapshot.backingElement)
            }
            return tapResultResponse(result)
        case .stale:
            return .json(200, ["status": "stale"])
        case .notFound:
            return .json(200, ["status": "not-found"])
        }
    }

    private static let knownGestureKinds: Set<String> = ["pinch", "rotate"]

    private func handleGesture(_ request: HTTPRequest) -> HTTPResponse {
        guard let body = request.body,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else {
            return .error(400, "missing or invalid JSON body")
        }
        guard let handle = json["handle"] as? String else {
            return .error(400, "missing handle")
        }
        guard let kind = json["kind"] as? String, Self.knownGestureKinds.contains(kind) else {
            return .error(400, "missing or unknown gesture kind")
        }
        let scale = (json["scale"] as? NSNumber)?.doubleValue ?? 1.0
        let radians = (json["radians"] as? NSNumber)?.doubleValue ?? 0.0

        switch store.lookup(handle: handle) {
        case .found(let snapshot):
            let result = onMainCatching {
                self.provider.gesture(
                    backingElement: snapshot.backingElement, kind: kind, scale: scale, radians: radians
                )
            }
            return tapResultResponse(result)
        case .stale:
            return .json(200, ["status": "stale"])
        case .notFound:
            return .json(200, ["status": "not-found"])
        }
    }

    private func handleSwipe(_ request: HTTPRequest) -> HTTPResponse {
        guard let body = request.body,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else {
            return .error(400, "missing or invalid JSON body")
        }
        guard let rawFrom = json["from"] as? [Any], rawFrom.count == 2,
              let fx = (rawFrom[0] as? NSNumber)?.doubleValue,
              let fy = (rawFrom[1] as? NSNumber)?.doubleValue,
              let rawTo = json["to"] as? [Any], rawTo.count == 2,
              let tx = (rawTo[0] as? NSNumber)?.doubleValue,
              let ty = (rawTo[1] as? NSNumber)?.doubleValue else {
            return .error(400, "missing or invalid from/to coordinates")
        }
        let result = onMainCatching { self.provider.swipe(fromX: fx, fromY: fy, toX: tx, toY: ty) }
        return tapResultResponse(result)
    }

    // A directional scroll (BE-0326). Same coordinates as `/swipe`, but the provider drags
    // non-inertially — holding at the end before lift so the scroll view settles where the gesture
    // left it, rather than flinging past the target with momentum.
    private func handleScroll(_ request: HTTPRequest) -> HTTPResponse {
        guard let body = request.body,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else {
            return .error(400, "missing or invalid JSON body")
        }
        guard let rawFrom = json["from"] as? [Any], rawFrom.count == 2,
              let fx = (rawFrom[0] as? NSNumber)?.doubleValue,
              let fy = (rawFrom[1] as? NSNumber)?.doubleValue,
              let rawTo = json["to"] as? [Any], rawTo.count == 2,
              let tx = (rawTo[0] as? NSNumber)?.doubleValue,
              let ty = (rawTo[1] as? NSNumber)?.doubleValue else {
            return .error(400, "missing or invalid from/to coordinates")
        }
        let result = onMainCatching { self.provider.scroll(fromX: fx, fromY: fy, toX: tx, toY: ty) }
        return tapResultResponse(result)
    }

    private func handleType(_ request: HTTPRequest) -> HTTPResponse {
        guard let body = request.body,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else {
            return .error(400, "missing or invalid JSON body")
        }
        guard let text = json["text"] as? String else {
            return .error(400, "missing text")
        }
        let result = onMainCatching { self.provider.typeText(text) }
        return tapResultResponse(result)
    }

    private func handleDeleteText(_ request: HTTPRequest) -> HTTPResponse {
        guard let body = request.body,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else {
            return .error(400, "missing or invalid JSON body")
        }
        guard let count = (json["count"] as? NSNumber)?.intValue, count > 0 else {
            return .error(400, "missing or non-positive count")
        }
        return tapResultResponse(onMainCatching { self.provider.deleteText(count: count) })
    }

    // Set a picker wheel to a named row (BE-0356). `handleTap`'s store-lookup handle shape, over the
    // app tree's `store` like every other element actuation, plus a string payload like `handleType`'s
    // `text` — which resolves no handle of its own, since it types into whatever holds focus.
    private func handleSetPickerValue(_ request: HTTPRequest) -> HTTPResponse {
        guard let body = request.body,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else {
            return .error(400, "missing or invalid JSON body")
        }
        guard let handle = json["handle"] as? String else {
            return .error(400, "missing handle")
        }
        guard let value = json["value"] as? String else {
            return .error(400, "missing value")
        }
        switch store.lookup(handle: handle) {
        case .found(let snapshot):
            let result = onMainCatching {
                self.provider.setPickerValue(backingElement: snapshot.backingElement, value: value)
            }
            return tapResultResponse(result)
        case .stale:
            return .json(200, ["status": "stale"])
        case .notFound:
            return .json(200, ["status": "not-found"])
        }
    }

    private func handleScreenshot() -> HTTPResponse {
        guard let png = caughtOnMain(Data?.none, self.provider.screenshot) else {
            return .error(500, "screenshot failed")
        }
        return .png(png)
    }

    private func tapResultResponse(_ result: TapResult) -> HTTPResponse {
        switch result {
        case .ok: return .json(200, ["status": "ok"])
        case .stale: return .json(200, ["status": "stale"])
        case .notFound: return .json(200, ["status": "not-found"])
        case .notHittable: return .json(200, ["status": "not-hittable"])
        case .valueNotFound: return .json(200, ["status": "value-not-found"])
        }
    }

    private func onMain<T>(_ work: @escaping () -> T) -> T {
        // Already on main (a direct call, or a nested one from inside another operation's work): run
        // in place. Taking `actuationLock` here would deadlock a nested call against the outer hold,
        // and there is nothing to serialize against — the main thread runs one block at a time.
        if Thread.isMainThread { return work() }
        // Off the main thread (an HTTP connection handler): serialize on the connection side *before*
        // dispatching to main, so a second XCUITest operation never enqueues onto the main queue while
        // the first is mid-flight and pumping the run loop — the re-entrancy that aborts the XCTest
        // host (see `actuationLock`). The lock is released only after the main-thread work returns.
        actuationLock.lock()
        defer { actuationLock.unlock() }
        var result: T!
        DispatchQueue.main.sync { result = work() }
        return result
    }

    /// Run `work` on the main thread, returning `fallback` if it raises an `NSException`.
    ///
    /// An `XCUIElement` interaction — or an `app.snapshot()` query — raises an `NSException` when the
    /// UI is in flux ("No matches found", a failed/timed-out snapshot). Uncaught it unwinds past Swift
    /// and aborts the resident runner's serve method, so every later request gets "connection
    /// refused"; `continueAfterFailure` does not help, because this is a raised exception, not a
    /// recorded soft failure. Catching it here keeps the runner serving and reports a deterministic
    /// fallback the caller turns into a normal reply. The caught reason (the XCUITest diagnostic the
    /// shim preserves) goes to the runner's stderr, which `BAJUTSU_XCUITEST_RUNNER_LOG` captures.
    private func caughtOnMain<T>(_ fallback: T, _ work: @escaping () -> T) -> T {
        onMain {
            var result = fallback
            do {
                try ObjCExceptionCatcher.catchException { result = work() }
            } catch {
                FileHandle.standardError.write(
                    Data("bajutsu runner: handler raised, reporting fallback: \(error.localizedDescription)\n".utf8)
                )
                return fallback
            }
            return result
        }
    }

    /// A caught actuation, reported as `.stale` on a raised interaction failure.
    ///
    /// `.stale` is safe for the driver's retried actuations (tap/gesture): XCUITest resolves the
    /// element — and raises if it is gone — before synthesizing any event, so a caught interaction
    /// failure precedes any side effect and the re-resolve-and-retry (BE-0289) cannot double-actuate.
    /// The coordinate/keyboard actuations the driver does not retry (`tapPoint`/`swipe`/`type`/…)
    /// surface `.stale` as a loud failure, so they carry no double-actuation risk either.
    ///
    /// `/setPickerValue` is the first route where a raise can arrive *after* its actuation — the
    /// value read-back runs once `adjust(toPickerWheelValue:)` has already moved the wheel — so the
    /// "precedes any side effect" argument above does not cover it. It is safe only because
    /// `adjust(toPickerWheelValue:)` is idempotent, and because the read-back catches its own raises
    /// (`settlesTo`) rather than letting them reach here at all. A future read-back-shaped route must
    /// re-establish one of those two, not inherit this justification.
    private func onMainCatching(_ work: @escaping () -> TapResult) -> TapResult {
        caughtOnMain(.stale, work)
    }
}
