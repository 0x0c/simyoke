import Foundation
import ObjCExceptionCatcher
import OpenAPIRuntime

/// The generated `APIProtocol` implemented against `ElementProviding` (Unit 3 of the OpenAPI
/// migration). Its whole job is translating each operation's generated `Input`/`Output` to and from
/// the provider; the actuation logic itself is unchanged from `Router`, which this replaces
/// endpoint by endpoint.
///
/// The hand-rolled server carries two load-bearing invariants. `serialized(_:)` below preserves the
/// first — XCUITest operations never run concurrently (BE-0323) — now that handlers are `async`.
///
/// The second, BE-0287's cap of eight concurrently-handled connections, is **not** reimplemented
/// here, because nothing in this type accepts a connection: it is the transport's to enforce. Unit 4
/// discharged that obligation by construction rather than by carrying anything over — `RunnerServer`
/// serves these operations through `LegacyBackedTransport`, which accepts nothing of its own, so
/// `HTTPServer`'s `DispatchSemaphore(value: 8)` still bounds exactly what it always bounded. Unit 5
/// is where the bound has to be re-established deliberately, because its listener accepts its own
/// connections.
final class APIHandler: APIProtocol {
    private let provider: ElementProviding
    private let store = SnapshotStore()
    /// A separate handle store for SpringBoard alert buttons (BE-0316), so their handles never
    /// collide with the app tree's and a `/systemAlert/query` never disturbs the app snapshot.
    private let alertStore = SnapshotStore()

    /// Serializes every XCUITest-touching operation, and absorbs the blocking main-thread hop.
    ///
    /// This one queue replaces `Router`'s `actuationLock` and carries the same guarantee, for the
    /// same reason: `app.snapshot()` / `app.screenshot()` / an `XCUIElement` interaction pumps the
    /// main run loop while it waits on the app over XPC, and that spin drains the main dispatch
    /// queue. Two operations dispatched to main concurrently would therefore re-enter — the second
    /// XCUITest call starting on the main thread *inside* the first — and XCUITest is not
    /// re-entrant, so the XCTest host aborts (BE-0323). Being serial, the queue means a second
    /// operation never even enqueues onto main while the first is in flight.
    ///
    /// It is a dedicated queue rather than a lock taken inline so that the `async` caller suspends
    /// instead of blocking. Under Unit 4's transport that caller is one of `HTTPServer`'s eight
    /// connection threads, which `LegacyBackedTransport` blocks for the life of the request either
    /// way — so the choice costs nothing on this stack, and it is what Unit 5 needs: once the
    /// listener is Hummingbird the caller is a shared event loop, and blocking that in
    /// `DispatchQueue.main.sync` would stall every other connection on it, including the
    /// `GET /health` poll the driver uses to tell "runner busy" from "runner dead" — exactly the
    /// distinction BE-0287's concurrent server exists to keep. `health` below never touches this
    /// queue, so it answers during a long gesture on either stack.
    ///
    /// Internal rather than `private` so `APIHandlerConcurrencyTests` can assert the one property of
    /// this invariant that is observable in-process — that the queue admits a single operation at a
    /// time. A probe on the provider side cannot see it: the work runs inside
    /// `DispatchQueue.main.sync`, which serializes whether or not this queue does.
    let operations = DispatchQueue(label: "bajutsu.runner.operations", qos: .userInitiated)

    init(provider: ElementProviding) {
        self.provider = provider
    }

    // MARK: - Interruption policy

    /// Stores which button the interruption monitor presses. Deliberately off `operations`: it
    /// touches no XCUITest state, only the shared store the monitor reads, and it must stay
    /// answerable while a long interaction is in flight — that interaction is exactly the one an
    /// alert may be interrupting.
    func setInterruptionPolicy(
        _ input: Operations.setInterruptionPolicy.Input
    ) async throws -> Operations.setInterruptionPolicy.Output {
        guard case let .json(body) = input.body else {
            return .badRequest(
                .init(body: .json(.init(status: .error, message: "expected a JSON body")))
            )
        }
        InterruptionPolicyStore.shared.setPolicy(
            InterruptionPolicy(
                rules: body.rules.map { InterruptionRule(identify: $0.identify, tap: $0.tap) },
                governs: body.governs
            )
        )
        return .ok(.init(body: .json(.init(status: .ok))))
    }

    /// Hands back what the monitor tapped, declined and swiped away since the last drain, so the
    /// driver can report them — a tapped label as an `AlertEvent`, a declined button list as an
    /// `UndeclaredInterruption` (BE-0406 Unit 2b), a swiped banner as an `AlertEvent` of its own
    /// kind (BE-0416).
    func drainInterruptions(
        _ input: Operations.drainInterruptions.Input
    ) async throws -> Operations.drainInterruptions.Output {
        let drained = InterruptionPolicyStore.shared.drain()
        let reply = Components.Schemas.InterruptionsReply(
            labels: drained.tapped, unmatched: drained.declined, banners: drained.banners
        )
        return .ok(.init(body: .json(reply)))
    }

    // MARK: - Reads

    func health(_ input: Operations.health.Input) async throws -> Operations.health.Output {
        // Deliberately does not touch `operations`: it reports whether the runner is serving, which
        // must stay answerable while a long gesture holds the main thread.
        .ok(.init(body: .json(.init(status: .ready))))
    }

    func queryElements(
        _ input: Operations.queryElements.Input
    ) async throws -> Operations.queryElements.Output {
        // A raised snapshot failure (UI in flux) maps to an empty screen, the same as the provider's
        // `try? app.snapshot()` handles a *thrown* one (BE-0105): the run fails loudly downstream
        // when nothing resolves, and the runner keeps serving rather than aborting on the raise.
        let elements = await caught([]) { self.provider.queryElements() }
        return .ok(.init(body: .json(elementsReply(store: store, elements: elements))))
    }

    func screen(_ input: Operations.screen.Input) async throws -> Operations.screen.Output {
        let size = await serialized { self.provider.screenSize() }
        return .ok(.init(body: .json(.init(width: size.width, height: size.height))))
    }

    func screenshot(
        _ input: Operations.screenshot.Input
    ) async throws -> Operations.screenshot.Output {
        guard let png = await caught(Data?.none, self.provider.screenshot) else {
            return .internalServerError(
                .init(body: .json(.init(status: .error, message: "screenshot failed")))
            )
        }
        return .ok(.init(body: .png(.init(png))))
    }

    // MARK: - Actuation

    func tap(_ input: Operations.tap.Input) async throws -> Operations.tap.Output {
        let request: Components.Schemas.TapRequest
        switch input.body { case .json(let body): request = body }
        // `point` wins over `handle`, matching the hand-rolled router: a coordinate tap is the one
        // actuation path with no element handle.
        if let point = request.point, point.count == 2 {
            let result = await caughtActuation { self.provider.tapPoint(x: point[0], y: point[1]) }
            return .ok(.init(body: .json(withDrain(actuationReply(result)))))
        }
        guard let handle = request.handle else {
            return .badRequest(.init(body: .json(.init(status: .error, message: "missing handle or point"))))
        }
        let taps = max(request.taps ?? 1, 1)
        let duration = max(request.duration ?? 0, 0)
        let reply = await actOnHandle(store, handle) { snapshot in
            self.provider.tap(backingElement: snapshot.backingElement, taps: taps, duration: duration)
        }
        return .ok(.init(body: .json(withDrain(reply))))
    }

    /// A pure query, unlike `tap`: it reuses the same handle-resolution outcomes but reports
    /// `.ok`/`.notHittable` for a still-live handle without acting on it, so the driver's
    /// `scroll_until_tappable` stop condition can poll it repeatedly with no side effects.
    func isHittable(
        _ input: Operations.isHittable.Input
    ) async throws -> Operations.isHittable.Output {
        let request: Components.Schemas.HandleRequest
        switch input.body { case .json(let body): request = body }
        return .ok(.init(body: .json(await actOnHandle(store, request.handle) { snapshot in
            self.provider.isHittable(backingElement: snapshot.backingElement)
        })))
    }

    func gesture(_ input: Operations.gesture.Input) async throws -> Operations.gesture.Output {
        let request: Components.Schemas.GestureRequest
        switch input.body { case .json(let body): request = body }
        let scale = request.scale ?? 1.0
        let radians = request.radians ?? 0.0
        let kind = request.kind.rawValue
        return .ok(.init(body: .json(await actOnHandle(store, request.handle) { snapshot in
            self.provider.gesture(
                backingElement: snapshot.backingElement, kind: kind, scale: scale, radians: radians
            )
        })))
    }

    func swipe(_ input: Operations.swipe.Input) async throws -> Operations.swipe.Output {
        let request: Components.Schemas.DragRequest
        switch input.body { case .json(let body): request = body }
        guard request.from.count == 2, request.to.count == 2 else {
            return .badRequest(
                .init(body: .json(.init(status: .error, message: "missing or invalid from/to coordinates")))
            )
        }
        let result = await caughtActuation {
            self.provider.swipe(
                fromX: request.from[0], fromY: request.from[1],
                toX: request.to[0], toY: request.to[1]
            )
        }
        return .ok(.init(body: .json(actuationReply(result))))
    }

    func scroll(_ input: Operations.scroll.Input) async throws -> Operations.scroll.Output {
        let request: Components.Schemas.DragRequest
        switch input.body { case .json(let body): request = body }
        guard request.from.count == 2, request.to.count == 2 else {
            return .badRequest(
                .init(body: .json(.init(status: .error, message: "missing or invalid from/to coordinates")))
            )
        }
        let result = await caughtActuation {
            self.provider.scroll(
                fromX: request.from[0], fromY: request.from[1],
                toX: request.to[0], toY: request.to[1]
            )
        }
        return .ok(.init(body: .json(actuationReply(result))))
    }

    // MARK: - Text editing

    func typeText(_ input: Operations.typeText.Input) async throws -> Operations.typeText.Output {
        let request: Components.Schemas.TypeRequest
        switch input.body { case .json(let body): request = body }
        let text = request.text
        let result = await caughtActuation { self.provider.typeText(text) }
        return .ok(.init(body: .json(actuationReply(result))))
    }

    func deleteText(
        _ input: Operations.deleteText.Input
    ) async throws -> Operations.deleteText.Output {
        let request: Components.Schemas.DeleteTextRequest
        switch input.body { case .json(let body): request = body }
        let count = request.count
        guard count > 0 else {
            return .badRequest(
                .init(body: .json(.init(status: .error, message: "missing or non-positive count")))
            )
        }
        let result = await caughtActuation { self.provider.deleteText(count: count) }
        return .ok(.init(body: .json(actuationReply(result))))
    }

    func selectAll(_ input: Operations.selectAll.Input) async throws -> Operations.selectAll.Output {
        let result = await caughtActuation(self.provider.selectAll)
        return .ok(.init(body: .json(actuationReply(result))))
    }

    func copySelection(
        _ input: Operations.copySelection.Input
    ) async throws -> Operations.copySelection.Output {
        let result = await caughtActuation(self.provider.copySelection)
        return .ok(.init(body: .json(actuationReply(result))))
    }

    /// Move a picker wheel to the row carrying `value` (BE-0356). Unlike the other actuations this
    /// can report `value-not-found`: the wheel resolved and moved, but never showed the value.
    func setPickerValue(
        _ input: Operations.setPickerValue.Input
    ) async throws -> Operations.setPickerValue.Output {
        let request: Components.Schemas.PickerValueRequest
        switch input.body { case .json(let body): request = body }
        let value = request.value
        return .ok(.init(body: .json(await actOnHandle(store, request.handle) { snapshot in
            self.provider.setPickerValue(backingElement: snapshot.backingElement, value: value)
        })))
    }

    // MARK: - System alerts

    /// The SpringBoard system-alert query (BE-0316): the same element+handle contract as
    /// `queryElements`, but sourced from the out-of-process permission prompt and keyed into
    /// `alertStore`. Empty when no alert is up, so the driver polls it against the step's timeout.
    func querySystemAlert(
        _ input: Operations.querySystemAlert.Input
    ) async throws -> Operations.querySystemAlert.Output {
        let buttons = await caught([]) { self.provider.querySystemAlertButtons() }
        return .ok(.init(body: .json(elementsReply(store: alertStore, elements: buttons))))
    }

    func tapSystemAlert(
        _ input: Operations.tapSystemAlert.Input
    ) async throws -> Operations.tapSystemAlert.Output {
        let request: Components.Schemas.HandleRequest
        switch input.body { case .json(let body): request = body }
        return .ok(.init(body: .json(await actOnHandle(alertStore, request.handle) { snapshot in
            self.provider.tapSystemAlertButton(backingElement: snapshot.backingElement)
        })))
    }

    // MARK: - Shared shaping

    /// Serialize a fresh element snapshot into the `{status, elements:[…handle…]}` reply both the
    /// app tree and the SpringBoard alert query share (BE-0316).
    private func elementsReply(
        store: SnapshotStore, elements: [ElementSnapshot]
    ) -> Components.Schemas.ElementsReply {
        .init(
            status: .ok,
            elements: store.refreshSnapshot(elements: elements).map { entry in
                .init(
                    traits: entry.snapshot.traits,
                    frame: [
                        entry.snapshot.frame.x, entry.snapshot.frame.y,
                        entry.snapshot.frame.width, entry.snapshot.frame.height,
                    ],
                    handle: entry.handle,
                    identifier: entry.snapshot.identifier,
                    label: entry.snapshot.label,
                    value: entry.snapshot.value
                )
            }
        )
    }

    private func actuationReply(_ result: TapResult) -> Components.Schemas.ActuationReply {
        switch result {
        case .ok: return .init(status: .ok)
        case .stale: return .init(status: .stale)
        case .notFound: return .init(status: .not_hyphen_found)
        case .notHittable: return .init(status: .not_hyphen_hittable)
        case .valueNotFound: return .init(status: .value_hyphen_not_hyphen_found)
        }
    }

    /// Fold the interruption monitor's drain into *reply* (BE-0407 Unit 6) — `tap` alone calls this,
    /// so a step whose last driver call was a tap skips the separate `/interruptionPolicy/drain`
    /// round trip its own drain otherwise costs. Every other actuation (`isHittable`, `gesture`,
    /// `setPickerValue`, …) keeps `actuationReply`'s plain reply undrained, and the driver still
    /// drains explicitly wherever a step's last call was not a tap. Checked unconditional on the
    /// tap's own outcome, matching the standalone endpoint: an interruption can land mid-actuation
    /// regardless of whether the tap itself lands.
    ///
    /// Always attaches `labels` and `unmatched`, empty arrays included, even though the reply schema
    /// marks them optional: their *presence* is itself the driver's signal that this runner folds a
    /// drain into `/tap` at all (BE-0407 Unit 6), so a present-but-empty pair must read differently
    /// from an absent one — a runner predating this fold. `banners` rides along on the same reply
    /// (BE-0416) but carries no such signal of its own: the driver's presence test still keys on the
    /// original two fields alone, since a runner between Unit 6 and BE-0416 folds those and omits
    /// `banners`. `APIHandlerParityTests` accounts for the resulting, deliberate difference from the
    /// legacy `Router`, which never gains these fields at all.
    private func withDrain(
        _ reply: Components.Schemas.ActuationReply
    ) -> Components.Schemas.ActuationReply {
        let drained = InterruptionPolicyStore.shared.drain()
        var reply = reply
        reply.labels = drained.tapped
        reply.unmatched = drained.declined
        reply.banners = drained.banners
        return reply
    }

    /// Resolve *handle* against *store* and, only on a live match, run *work* against its element.
    /// A stale or unknown handle is reported without touching XCUITest at all, exactly as before.
    private func actOnHandle(
        _ store: SnapshotStore, _ handle: String,
        _ work: @escaping @Sendable (ElementSnapshot) -> TapResult
    ) async -> Components.Schemas.ActuationReply {
        switch store.lookup(handle: handle) {
        case .found(let snapshot):
            return actuationReply(await caughtActuation { work(snapshot) })
        case .stale:
            return .init(status: .stale)
        case .notFound:
            return .init(status: .not_hyphen_found)
        }
    }

    // MARK: - Main-thread hop

    /// Run *work* on the main thread, serialized against every other XCUITest operation.
    ///
    /// The caller suspends rather than blocking: only `operations`' own thread waits on the main
    /// thread, so an event loop is never held (see `operations`).
    private func serialized<T: Sendable>(_ work: @escaping @Sendable () -> T) async -> T {
        await withCheckedContinuation { continuation in
            operations.async {
                var result: T!
                DispatchQueue.main.sync { result = work() }
                continuation.resume(returning: result)
            }
        }
    }

    /// `serialized`, returning *fallback* if the work raises an `NSException`.
    ///
    /// An `XCUIElement` interaction — or an `app.snapshot()` query — raises an `NSException` when
    /// the UI is in flux ("No matches found", a failed or timed-out snapshot). Uncaught it unwinds
    /// past Swift and aborts the runner, so every later request gets "connection refused";
    /// `continueAfterFailure` does not help, because this is a raised exception rather than a
    /// recorded soft failure. Catching it here keeps the runner serving and reports a deterministic
    /// fallback the caller turns into a normal reply. The caught reason goes to the runner's stderr,
    /// which `BAJUTSU_XCUITEST_RUNNER_LOG` captures.
    private func caught<T: Sendable>(
        _ fallback: T, _ work: @escaping @Sendable () -> T
    ) async -> T {
        await serialized {
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
    /// The coordinate/keyboard actuations the driver does not retry surface `.stale` as a loud
    /// failure, so they carry no double-actuation risk either.
    private func caughtActuation(_ work: @escaping @Sendable () -> TapResult) async -> TapResult {
        await caught(.stale, work)
    }
}
