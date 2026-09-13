# Continuous integration

This document covers two separate topics:

1. **CI for this repo** — guard the tool itself (`.github/workflows/`).
2. **Running bajutsu in *your* app's CI** — a composite action + recipe you reuse.

## This repo's CI

| Workflow | Runner | When | What |
|---|---|---|---|
| [`ci.yml`](../.github/workflows/ci.yml) (`check` job) | Linux | push to `main`, every PR (pull request) | the full `make check` gate on Python 3.13 — lockfile freshness (`uv lock --check`), formatting (`ruff format --check`), lint (`ruff`), shell lint (`shellcheck`), workflow lint (`actionlint`), types (`mypy bajutsu demos scripts`, plus `mypy --allow-untyped-defs tests` — BE-0388), and `pytest` with a coverage floor (`--cov-fail-under=89`). The logic layer needs no Simulator, so it is fast and cheap |
| [`mcp-wire.yml`](../.github/workflows/mcp-wire.yml) | Linux | manual + PRs touching `bajutsu/mcp/**`, the `bajutsu mcp` CLI, or the wire test | the **MCP wire round-trip** lane (BE-0301) — one job, **wire (stdio)**, that starts `bajutsu mcp --transport stdio` as a real subprocess and drives it with the `mcp` SDK's `fastmcp.Client` over stdio (`pytest tests/test_mcp_wire.py -m mcp_wire -n0`). It exercises the JSON-RPC framing, tool-schema advertisement, and resource-URI encoding that the in-process `tests/test_mcp.py` never touches: list tools, round-trip a `bajutsu_doctor` / `bajutsu_run` call, and read a run-evidence resource, all against the `fake` backend (so `bajutsu_doctor` runs real device-free logic in the server; `bajutsu_run` spawns a real `bajutsu run` whose verdict is environment-dependent, so the round-trip asserts only that the verdict line survives the transport). The `mcp_wire` marker keeps the suite out of the fast gate (pyproject `addopts` `not mcp_wire`); a real subprocess + stdio IPC is more timing-sensitive than an in-process call, so it lands as a **non-gating** per-PR signal and is promoted to required once stable — the precedent BE-0282's `network (playwright)` set |
| [`web-e2e.yml`](../.github/workflows/web-e2e.yml) | Linux | manual + every PR + merge queue (required `E2E (web)` check) | the **web (Playwright) backend** lane (BE-0279), six jobs against a headless Chromium: **smoke (playwright)** runs the `demos/web` scenarios (`make -C demos/web e2e`); **dogfood (serve UI)** drives Bajutsu's own serve SPA (BE-0058); **conformance (playwright)** runs the BE-0114 driver contract against the real browser; **network (playwright)** drives the real network path — `page.route` interception, `requestfinished` capture, the `mocked` flag, and redaction of really-captured evidence (`make -C demos/web e2e-network`, BE-0282); **codegen (playwright)** generates a native Playwright test from a scenario and runs it with the real `@playwright/test` runner (`make -C demos/web codegen-e2e`, BE-0293) — the web twin of `ios-e2e.yml`'s `codegen (xcuitest)`; **onboarding (doctor / provision)** exercises `bajutsu.common.provisioning.provision` end to end against a genuinely browserless host, then re-checks `doctor`'s environment gate after provisioning (BE-0304). **No Mac / Simulator**, so it proves the core is platform-neutral. All six are path-gated by the same `changes` detector (`scripts/e2e_changes.py`, `E2E_LANE=web`); the five deterministic, host-independent jobs — every job but **onboarding** — feed the always-reporting `E2E (web)` job, the required check, while **onboarding** stays a per-PR signal (BE-0304); a PR that can't affect the web path skips the browser jobs and the gate passes. **network (playwright)** and **codegen (playwright)** each landed as a per-PR signal first and, having proven stable in CI, are now promoted into `E2E (web)`'s `needs:` — **network** the web twin of `android-e2e.yml`'s already-gating `network (adb)`, **codegen** the web twin of `ios-e2e.yml`'s already-gating `codegen (xcuitest)` |
| [`dependency-audit.yml`](../.github/workflows/dependency-audit.yml) | Linux | manual + weekly + push to `main` / PRs touching `pyproject.toml` / `uv.lock` | audit the locked dependency graph (`uv export` → `pip-audit --no-deps`) against the advisory DB. The result is a function of the lockfile and the DB, so it runs on a dependency change and on a weekly schedule that catches advisories newly disclosed against unchanged pins |
| [`swift.yml`](../.github/workflows/swift.yml) | macOS | push to `main` + PRs touching `BajutsuKit/**` | `swift build` + `swift test` for [BajutsuKit](../BajutsuKit). Unit-tests the pure-Foundation logic (request matching / mock parsing) with no Simulator — the on-device interception itself is covered by `ios-e2e.yml` |
| [`ios-e2e.yml`](../.github/workflows/ios-e2e.yml) | macOS | manual + every PR + merge queue (required `E2E (iOS)` check) | the **iOS (XCUITest) backend** lane, ten macOS jobs against the showcase, all on the XCUITest backend (the resident BajutsuRunner). A **build (app + runner)** job compiles both showcase apps (SwiftUI + UIKit) and the resident runner once and uploads them as a single `ios-build` artifact; the consumer jobs download and install those products rather than rebuilding, so the cold Swift build is paid once rather than once per job. **run (xcuitest)** is the homogeneous gating lane — the scenarios that were once the separate `smoke`, `gestures`, `permission`, and `runner-actuation` jobs, folded into one `bajutsu run` that warm-reuses the resident runner across them (BE-0291) — capped at one reuse per runner on this lane (`BAJUTSU_XCUITEST_MAX_WARM_REUSES: 1`, to curb a mid-run runner crash on contended CI hosts), so the twelve scenario documents that run — every scenario the seven files hold, since BE-0315 made the notification grant native so nothing is `ai`-tagged to exclude — pay six cold `xcodebuild test-without-building` startups rather than one — two more than the four separate jobs they replaced summed to, though each of those jobs also paid its own Swift build and boot, which the shared `build` job above now pays once for the whole lane: the Stable catalog (`smoke.yaml`), the pinch/rotate two-finger gesture (`gestures_multitouch.yaml`, BE-0019), the runner channel's `/type` (`search.yaml`) and `/swipe` + `/back` (`notices.yaml`, BE-0281), each button of a presented `UIAlertController` (`alert.yaml` — the only scenario reaching `uniquelyIdentifiedElement`'s duplicate-group branch, which `swift test` cannot compile), the deterministic `permissions` field plus, since BE-0315, the native reactive `systemAlertHandling` notification grant (`permission.yaml`, BE-0276 location + BE-0315 notification, both now on the gate) and the proactive `handleSystemAlert` step (`permission_system_alert.yaml`, BE-0316, tapping the SpringBoard notification prompt's "Allow" button by accessibility query, no vision model); `run` runs each scenario to its own verdict and reddens the gate if any fails, so the per-scenario pass/fail moves from job names into the run's report. **actuation (xcuitest)** is the lane's non-gating landing pad. It drives on-device XCUITest actuation not yet promoted into `run` (BE-0281) — `back` (`navigation.yaml`), device control (`setLocation` / clipboard / `push`, `device.yaml` + `push.yaml`) on the Stable launch tab, and the four text-editing actuators (`text_editing.yaml`) — and the scenario-authoring features on a third actuating backend (BE-0285): `extract` capturing the Log tab's live counter (`extract.yaml`), `forEach` iterating the five Stable rows while the loop body pushes and pops a detail so the tree mutates between iterations (`foreach.yaml`), data-driven rows (`data_driven.yaml`), and `relaunch` (`relaunch.yaml`). Those four are the shared showcase files, unchanged (BE-0221), and are listed ahead of the device-control scenarios so neither a delivered notification nor an OS-level grant precedes them. Last in the list is the lane's only genuine cross-process paste (`paste_system_alert.yaml`, BE-0369): `setClipboard` seeds the pasteboard from outside the app, so the app's own read raises iOS's paste-consent prompt, which `handleSystemAlert: { prompt: paste }` answers by a native accessibility query — granted and denied, each under the target's locale and again under `locale: ja_JP`, the pair that checks the label lookup against a SpringBoard rendering a language the run itself is not written in — new on-device coverage, so it earns its stability here before any promotion into `run`. **golden (xcuitest)** runs the BE-0006 element-tree golden over XCUITest (`golden.yaml`) — the iOS twin of `android-e2e.yml`'s `golden (adb)`. **codegen (xcuitest)** generates a native XCUITest from a scenario (`make -C demos/showcase ui-test`) and runs it with `xcodebuild` (no bajutsu / AI at test time); it compiles the UITests scheme, a different product than the resident runner, so it builds its own and is not a `build`-artifact consumer. **bundled-runner (xcuitest)** runs a smoke scenario against both the SwiftUI and UIKit a11y apps with the runner resolved from the wheel bundle rather than a config `testRunner` (BE-0292), proving the runner is app-agnostic. **conformance (xcuitest)** runs the driver conformance suite (BE-0114) on-device against the XCUITest backend; a Simulator infrastructure fault during the suite (a resident-runner crash, `base.BackendCrashError`) is recovered the way `bajutsu run` recovers one — re-leasing a fresh device and re-running the affected test, bounded per test by the retry count and wall-clock respawn budget (`BAJUTSU_CRASH_RETRIES` / `BAJUTSU_CRASH_RECOVERY_BUDGET`) — so this required check reddens on a real contract violation, not a host flake, while a crash every attempt still fails loudly; every respawn is counted into the uploaded `conformance-recovery-report` artifact (BE-0334). A wedged CoreSimulator is the host's fault, but not a respawn's to repair: rebuilding the device is made of the very `simctl` calls that just stalled. So the suite names it a host fault in the job log and counts it into the same artifact instead of retrying it, and it resolves the app's data container once per lease rather than once per test, giving that one read a second attempt when the first exceeds its deadline. A stall that second attempt absorbs reddens nothing, so it is counted there too as its own leading indicator — the host that wedges the check later is this one getting worse (BE-0378). **fault-injection (xcuitest)** signals the runner's own host process instead of driving a healthy one (BE-0305): a brief `SIGSTOP` hangs the channel the way a wedged runner does and must be absorbed by the transient retry (BE-0207), a freeze past the retry budget must be ridden out by crash recovery (BE-0287), and a `SIGKILL` must end the read on a crash diagnosis naming a mid-run runner fault rather than an unrelated timeout — each case lifting its fault on the channel's own log record that it reached the layer under test, never on a guessed delay; landed as a per-PR signal and promoted into `E2E (iOS)`'s `needs:` once the runner-HTTP fix that had been reddening it landed, 64 runs without a failure since. **visual (xcuitest)** pixel-compares the Stable catalog against the committed `baselines_ios/` baseline (`make -C demos/showcase e2e-visual`), masking the status bar and the "Liquid Glass" tab bar; its `e2e-visual` rebuilds the app itself, so like `codegen` it is not a `build`-artifact consumer. **network (xcuitest)** drives the real network path over the iOS transport (`make -C demos/showcase e2e-network`, BE-0282) — the iOS twin of `web-e2e.yml`'s `network (playwright)` and `android-e2e.yml`'s `network (adb)`: BajutsuKit's in-app `URLProtocol` answers a mocked `POST /post` with 201 (a live server would answer 200) and reports each exchange to the collector on loopback, then a second scenario issues an unstubbed catalog `GET` asserted only to have been observed, and `demos/showcase/network/assert_network_evidence.py` asserts the persisted `network.json` — the mocked exchange's `Authorization` header and `password` body field masked, no raw secret anywhere in the file, and the unstubbed exchange carrying `mocked` false so an over-broad mock matcher cannot claim traffic nothing stubbed. The masking check is the gap BE-0282 exists for: every pure redaction test feeds the algorithm a hand-built exchange, so whether a really captured credential is masked in shipped evidence is observed nowhere else on iOS. It drives a `make` target that builds what it needs, so like `codegen` and `visual` it is not a `build`-artifact consumer. The lane carried a two-device **pool (xcuitest)** job (BE-0298) until that job was withdrawn: it booted a second Simulator and asserted the device pool's isolation claim across one `bajutsu run --workers 2`, and it returned that verdict once in five runs — the other four collapsed on the host rather than on any pool check (`SimRenderServer` crashing on its own dispatch queue, a wedged CoreSimulator timing out `simctl uninstall`, the runner channel becoming unreachable mid-run), each red run spending about half an hour of a runner billed at 10x, and cutting the video, the touch markers, and half the scenarios moved the collapse earlier without removing it. The claim is now checked against real concurrent devices by `pool (adb)` on the Android lane alone, and on this lane by the fast suite's bookkeeping proof (`tests/runner/test_pool.py`) alone. A shared `setup-ios-toolchain` composite action collapses the Xcode / uv / xcodegen / Simulator-boot steps every macOS job repeats (the `build` job passes `boot: false`, since it needs no Simulator). All nine jobs are path-gated by the same `changes` detector; `build`, `run`, `codegen`, `bundled-runner`, `conformance`, and `fault-injection` additionally feed a single always-reporting `E2E (iOS)` job, the required check. `actuation`, `golden`, `visual`, and `network` run under the same path gate but are deliberately excluded from `E2E (iOS)`'s `needs:`: newly-wired XCUITest actuation lands as a per-PR signal first (the Simulator lane has a flakiness history, BE-0218) and is promoted to the gate only once stable, the element-tree `golden` is deterministic and host-independent so its drift surfaces as a per-PR signal, a `visual` pixel baseline is host-specific (the Simulator renderer varies by Xcode / device / OS), and `fault-injection` breaks the runner on purpose, so it lands as a signal first on the same BE-0282 path, and `network` is itself newly-wired on-device coverage so it lands as a signal first too — the path its own web twin took before joining `E2E (web)` — so each surfaces a drift or a flake as a signal on its own job (visual's captured screenshot uploads as `ios-e2e-visual-run` to re-record the baseline from) rather than blocking a merge |
| [`android-e2e.yml`](../.github/workflows/android-e2e.yml) | Linux | manual + every PR + merge queue (required `E2E (android)` check) | the **Android (adb) backend** lane (BE-0208), eight per-concern jobs mirroring the iOS and web lanes' job splits, each booting its own x86_64 API 34 AVD under KVM (`reactivecircus/android-emulator-runner`), plus a ninth **warm gradle cache (adb)** job with no AVD that builds the Compose + resident-server APKs that `smoke`/`golden`/`conformance`/`fault-injection`/`pool` all build, up front, so those five jobs' own unchanged builds restore a warm Gradle dependency + build cache instead of resolving/compiling cold five times over. It skips Views: `smoke` alone needs it, and warming it here would make the other four wait (a job-level `needs:`) on a Views compile none of them uses, so `smoke`'s own Views build stays cold, same as before this job existed. `actions/cache` can't share a still-unsaved cache between jobs running concurrently in the same run, so this one runs first and those five declare `needs: warm-gradle-cache`; none of them passes `cache-read-only` explicitly — they rely on the composite action's own safe default (`true`, read-only), the multi-job cache-sharing pattern `gradle/actions` documents. `network`/`visual`/`codegen` are deliberately excluded from that `needs:` (`codegen` additionally needs the Compose AndroidTest/instrumentation variant, which warm-gradle-cache never builds): a job-level `needs:` would block even their own unrelated AVD-cache step until the build finished, costing those lighter jobs more than a cache hit could save. A miss or an outright failure there only costs time, never correctness, so every consumer job's `if:` uses `!cancelled()` (not `always()`, which would also survive this file's own `concurrency: cancel-in-progress` and keep a superseded run's jobs dispatching) rather than inheriting a hard `needs` dependency on its success, and it never joins `E2E (android)`'s `needs:`: **smoke (adb)** builds the Compose + Views showcase APKs and runs the Stable-tab scenarios — the core id/tap/type/value flows plus a push/pop back-navigation flow — through `--backend android` (`make -C demos/showcase/android e2e`); **golden (adb)** runs the on-device golden element-tree check for the Compose Stable catalog (`make -C demos/showcase/android e2e-golden`, BE-0006 / BE-0208 unit 4), then re-runs it with the resident channel forced off (`make -C demos/showcase/android e2e-fallback`, BE-0245) so both read channels are proven to agree; **network (adb)** drives the BE-0283 network-capture assertion — a `request` step observes real emulator traffic reported through BajutsuAndroid's interceptor over the `adb reverse` collector bridge; **conformance (adb)** runs the driver conformance suite (BE-0114) against the real adb backend — the Android twin of `ios-e2e.yml`'s `conformance (xcuitest)`; **fault-injection (adb)** puts the display to sleep so the real read source serves a genuinely empty accessibility tree, checking that `CoordinateTreeDriver`'s transient-empty retry rides over the real condition its fast-suite test can only fabricate, and that an empty outliving the retry budget fails loudly (`make -C demos/showcase/android e2e-fault-injection`, BE-0305 — now on the required aggregate check, below); **visual (adb)** runs the pixel VRT (below); **pool (adb)** is the lane's only two-emulator job (BE-0298) — it boots a second instance of its cached AVD with `-read-only` from inside the emulator-runner's own step (`scripts/android_pool_e2e.sh`, since that action has no two-emulator mode and its emulator lives only for that step's length), runs four state-neutral scenarios through one `bajutsu run --workers 2` (`make -C demos/showcase/android e2e-pool`), and asserts the same isolation claim with `scripts/assert_pool_isolation.py`; both instances run at `-memory 3072 -cores 1` and with `-read-only`, the flag that lets two emulator processes share one AVD — the job's own emulator carries it too, since an instance holding the AVD read-write locks it and refuses the second outright; `-read-only` also disables snapshot load, so this is the one job on the lane that always cold-boots both devices, and its own AVD cache entry buys the AVD's creation rather than a resume — and it is keyed on the narrower `pool` output rather than the lane-wide one. **No Mac / Simulator** — the third backend's Linux twin of the iOS and web e2e lanes. Path-gated by a `changes` detector (`scripts/e2e_changes.py`, `E2E_LANE=android`), feeding the required `E2E (android)` aggregator (BE-0279). The AVD is x86_64 (not the local validation's arm64) so KVM accelerates it on the x86_64 runner; the golden's baseline is recorded on arm64 yet passes on x86_64 because the comparison is field-level with a tolerant frame check. The sheet/cover flows (`components`, `modals`) are included by raising the condition-wait ceiling for this lane only — `make -C demos/showcase/android e2e` exports `BAJUTSU_MIN_WAIT_TIMEOUT` (default 15s), a floor under each wait's own timeout — because the software-rendered emulator draws a modal slower than the shared scenarios' 5s waits allow. A condition wait returns the instant it is satisfied, so the larger ceiling is a safe upper bound, not a fixed delay, and the shared scenarios stay untouched (their `timeout: 5` is the same on every backend). The deep-scroll flows (`controls`, `notices`) join the lane too: both reveal a far target with `scroll` (BE-0326) — `controls` the segmented-control value node below the buttons, `notices` a list row well below the fold — a bounded, non-inertial step that re-queries the tree and stops the moment the target is on-screen, rather than a swipe chain tuned to a fixed distance; a fixed-distance chain scrolls far less of Android's dense screen (2400px) than of iOS's (~900pt), so `scroll`'s re-query removes the need to retune the distance per backend (BE-0208 unit 5). `system` and `modals` reveal their far targets the same way as of this change (`sys.paste.value` on the Permissions tab, `log.dialog.value` on the Log tab), so four of the lane's flows now rest on `scroll` rather than a tuned swipe chain. The single-touch gesture flow (`gestures`) joins too: the adb driver drives a double-tap with a raw `sendevent` touch sequence on a rooted emulator (the `e2e` target runs `adb root` first), firing both taps inside the platform double-tap window that a per-tap `input` JVM overran; on a non-rooted device it falls back to `input tap` unchanged. Its reveal swipes rest on unit 5's other half: the default directional swipe travels a screen *fraction* (`_SWIPE_FRACTION`, `bajutsu/common/orchestrator/actions/handlers/gestures.py`), so it covers the same proportion of Android's dense screen (2400px) as of iOS's (~900pt) — a fixed coordinate count did not (BE-0208 unit 5). The multi-touch gesture flow (`gestures_multitouch`) joins too (BE-0232): the adb driver drives a pinch / rotate as a raw two-slot `sendevent` sweep on the rooted emulator — two contacts moving together across interleaved frames — so the shared scenario that iOS runs on XCUITest runs unchanged on Android; unlike the single-touch double-tap there is no `input` fallback (a two-finger gesture cannot be approximated), so it requires root and fails loudly otherwise. The runtime-permission flow (`permission`) joins the lane too (BE-0208 unit 6, exercising BE-0210's up-front grant): it is the **same** `permission.yaml` the iOS lane runs — the grant mechanism lives in config, not the scenario, so one file serves both. `showcase-compose` grants `POST_NOTIFICATIONS` up front (`grantPermissions` → `pm grant` at lease time), so Android's `RequestPermission` contract short-circuits to granted with no dialog; the scenario's `systemAlertHandling` guard therefore never fires here, keeping the flow deterministic (no LLM, no fixed sleep) on the lane (on iOS, where notifications can't be pre-granted, that guard taps "Allow" instead). The device-control flow (`device`) joins the lane too (BE-0208 unit 5): it overrides the GPS location (`emu geo fix`), round-trips the clipboard, and re-asserts the settled screen — the **same** `device.yaml` the iOS lane runs (unified across iOS/Android, since `setLocation` and the clipboard are advertised on both; the iOS-only `push` half lives in `push.yaml`), run on the Stable launch tab. Both `setLocation` and `clipboard` of the device-control family are exercised: `cmd clipboard` is a silent no-op on-device and since Android 10 only the foreground app may touch the clipboard, so the clipboard runs through an in-app receiver the showcase embeds from `BajutsuAndroid` (BE-0233) — the seed/read-back is the strong assertion PR #934 wanted. The interrupt-handler flow (`interrupts`) joins the lane too (BE-0314): a syntax demo whose `interrupts` entry never actually fires against the real app, so the flow underneath is the same stable→horse→favorite one `firstlook` already runs. What it adds over `firstlook` is BE-0314's *check* path against a live tree: a `wait` rides its own poll tree at no extra cost, and a bare act step pays one extra query for the guard (since this scenario declares no `screenChanged` policy), so a never-matching handler is proven on a real device to disturb nothing and cost one bounded read per act — which no fast-suite fake can show. Its ids carry both dotted SPEC and underscore Android Views forms (BE-0221), so it runs unmodified. The `visual (adb)` job runs a pixel visual-regression check for the Compose Stable catalog (`make -C demos/showcase/android e2e-visual`, BE-0208 unit 4): unlike the element-tree golden, a pixel baseline is host-specific — the x86_64 software renderer (swiftshader) and a local arm64 emulator diverge per pixel — so this baseline is recorded on this x86_64 lane and committed (`demos/showcase/scenarios/visual/baselines_android/`), not on arm64; the top status bar is masked so the wall clock never churns the comparison. The `uiautomator (codegen)` job is the codegen output path (`make -C demos/showcase/android e2e-codegen`, BE-0294) — the Android twin of `ios-e2e.yml`'s `codegen (xcuitest)`: it re-generates a native UI Automator (Kotlin) test from `codegen_android.yaml`, then Gradle's `connectedAndroidTest` builds the Compose a11y app + instrumentation APKs, installs both, and runs the generated test against the emulator with no bajutsu / adb driver / AI at test time; regenerating before the build means a stale check-in cannot mask an emitter or `androidx.test.uiautomator` API drift. The deterministic host-independent jobs — `smoke (adb)`, `conformance (adb)`, `network (adb)`, and `fault-injection (adb)` — feed the always-reporting `E2E (android)` aggregator, the required check (BE-0279); `golden (adb)`, `visual (adb)`, `uiautomator (codegen)`, and `pool (adb)` are deliberately excluded from its `needs:` and stay per-PR signals (the element-tree golden can drift with an upstream dependency, a pixel baseline is host-specific, and the codegen and two-emulator lanes land as signals first per the BE-0282 precedent), the same gate boundary iOS's `E2E (iOS)` draws. `fault-injection (adb)` walked that same path to its end: 73 runs for one failure — the emulator never coming up, not anything the lane asserts — against `conformance (adb)`'s four in 67 over the same span, which promoted it out of signal status and into the aggregator's `needs:`, where a regression in the retry's real detection blocks a merge instead of surfacing as a red a merge can ignore (BE-0305). `tests/test_e2e_gate_needs.py` pins each aggregator's composition on the fast suite, that every job a gate `needs:` has its own result read — a failed dependency must not pass as the path-skip the aggregators count as green — and, by running the verdict script itself, that each of those results actually reddens the check |
| [`devicefarm.yml`](../.github/workflows/devicefarm.yml) | Linux | **manual only** (`workflow_dispatch`) | the **AWS Device Farm batch submit** (BE-0235) — builds the showcase Compose APK, packages Bajutsu + config + scenarios, and hands them to [`scripts/devicefarm_submit.py`](../scripts/devicefarm_submit.py), which uploads a custom-environment test spec that runs `bajutsu run --backend adb` on Device Farm's host, polls the run, downloads the artifacts, and surfaces **Bajutsu's own manifest verdict** (never Device Farm's classification). It is CI-side glue outside the deterministic core, so no LLM touches the verdict; it is `workflow_dispatch` only (never on push/PR, not a required check). Auth is a short-lived AWS credential from GitHub OIDC (`AWS_DEVICEFARM_ROLE_ARN`) scoped to a `devicefarm` Environment, with the project / device-pool ARNs in repository variables; with any unset the job is a green no-op, so it stays dormant until an operator wires up an account. The real-account serial-resolution PoC is a documented human procedure (see [AWS Device Farm](devicefarm.md)), deliberately kept off the deterministic gate |
| [`ai-smoke.yml`](../.github/workflows/ai-smoke.yml) | Linux | **manual only** (`workflow_dispatch`) | the **live-model** lane: two jobs, both **non-gating** signals (the BE-0282 precedent). **smoke (direct Anthropic API)** (BE-0300) calls a real provider through Bajutsu's own adapter code (`bajutsu.ai.anthropic.AnthropicBackend`) with a trivial forced-tool prompt and asserts only that the reply lands as a populated, parseable neutral `MessageResponse` (`pytest tests/test_ai_backend_live_smoke.py -m live -n0`). Every other adapter test drives a hand-written `FakeAnthropic`, so nothing else re-observes the live API's actual shape; it is a transport-and-schema check, never a model-quality one. **accuracy (system-alert guard)** (BE-0308) asks the other live question: not whether the transport round-trips, but whether the answer is right. It shows the vision alert locator (`bajutsu.agents.alerts.ClaudeAlertLocator`) real iOS system dialogs captured from the showcase app and asserts that the coordinates it returns land inside the correct dismiss control's frame (`pytest tests/test_real_model_alerts.py -m live -n0`) — the guard's own safety claim, which every other test of the guard takes on faith by handing it an `AlertDecision` its author typed. The dialogs are committed captures under `tests/fixtures/be0308/`: a notification prompt, a location prompt, iOS's cross-process paste consent, and the showcase app's own delete confirmation, whose destructive button the guard must not reach for at all. They need no Simulator, so only the model call is live, and a deterministic check that each capture is self-consistent runs on every gate without a credential. Neither job puts an LLM on the `run` / CI verdict (prime directive 1): both exercise the AI authoring periphery alone. The `live` marker keeps both suites out of the fast gate (pyproject `addopts` `not live`). Both are `workflow_dispatch` only — never on push/PR, so a fork-triggered run can never see the credential (the boundary `devicefarm.yml` draws). Auth is the `ANTHROPIC_API_KEY` repository secret scoped to the `ai-smoke` Environment; with it unset every test skips itself and both jobs are green no-ops, so the lane stays dormant until an operator wires the secret up. Only the direct Anthropic API adapter is wired: Bedrock needs a live AWS role and `ant` a signed-in OAuth CLI seat, neither realistically a CI secret — their `-m live` tests still run locally / manually |

### Which E2E checks gate a merge (BE-0279)

Each backend lane — iOS (`E2E (iOS)`), Android (`E2E (android)`), and web (`E2E (web)`) — carries one
always-reporting aggregator that is the lane's required status check; per-backend aggregators keep
attribution, so a red check names the backend that broke. **A check gates a merge if, and only if, it
is deterministic and host-independent.** A check whose result depends on the host or on an upstream
dependency stays a non-required signal — it still runs and surfaces a drift on its own job, but it
never blocks a merge:

- **Pixel visual-regression (VRT)** — the `visual` jobs. A pixel baseline is host-specific (a
  Simulator or emulator renderer varies by OS, device, and toolchain), so a drift is unrelated to any
  Bajutsu change. Excluded from the aggregators' `needs:`.
- **Element-tree golden** — the iOS/Android `golden` jobs. Deterministic, but on Android the tree is
  read through an upstream on-device server whose drift could redden it independently of any Bajutsu
  change, so a golden drift is best surfaced as a per-PR signal rather than a merge blocker.
  Excluded from the aggregators' `needs:`.

Because a required check skipped by a `paths:` filter stays pending forever and blocks the merge, none
of the lanes path-gate at the trigger. Each triggers on every PR (and the merge queue) and a `changes`
job path-gates the heavy jobs instead, running [`scripts/e2e_changes.py`](../scripts/e2e_changes.py)
with `E2E_LANE=ios|android|web` (the per-lane relevance filter, unit-tested in
`tests/test_e2e_changes.py`). The aggregator runs `if: always()`, so a path-skip reports as a pass and
an unrelated PR is neither run nor blocked. Adding a new required aggregator to `main`'s
branch-protection ruleset is an out-of-repo administrative step, done by a maintainer with the exact
check name.

The filter inverts its default for `bajutsu/` (BE-0333). Rather than a hand-kept positive list that a
file must join to be seen — where anything unlisted silently fired nothing — the shared core sweeps
the *whole* package and carves out only what is explicitly classified: the periphery no lane exercises
(`_PERIPHERY_EXCLUSIONS`, each entry carrying the reason it is out — the analytics / analysis stacks,
the MCP server, the AI adapters, the GitHub and cloud integrations, and the individual periphery
modules of the mixed `agents` / `crawl` / `cli.commands` packages) and the per-backend leaves each
lane reclaims (`_LANE_CLAIMED`). A file named in neither is swept in and fires all three lanes until
somebody classifies it — a wasted job, the safe direction — instead of the silent under-trigger the
positive list produced. That default retires a whole class of miss: a top-level module split into a
package (`bajutsu/config`, `bajutsu/platform_lifecycle` both drifted that way, skipping the fleet the
lane exists to exercise), a new top-level module or CLI command, and files the run path imports but
the list never named — `bajutsu/common/report/`, the manifest writer every run invokes, chief among them —
now all fire because a package and a module match the sweep alike.

Several tests keep the classification honest. `test_run_path_closure_is_gated_or_excluded` walks the
run path's static `ast` import closure and fails if any file it reaches is neither gated by a lane nor
a classified periphery entry — so a module the run path *starts* importing that nobody classified
fails `make check` rather than surfacing months later as a mysteriously green required check.
`test_periphery_exclusion_paths_exist` and `test_every_plain_literal_path_in_the_filter_exists` resolve
every exclusion entry and every plain path against the tree, so a rename or a deletion fails the gate
instead of leaving a pattern matching nothing. And the shared core sweeps the two per-backend
directories (`bajutsu/common/drivers/`, `bajutsu/platform_lifecycle/environments/`) minus the leaves each lane
names, with a test asserting no file under either fires zero lanes and another asserting each
`_LANE_CLAIMED` leaf is reclaimed by at least one lane — so a newly added backend module over-fires
rather than going unseen, and a leaf carved out but claimed by nobody fails the gate. The inverted
default's over-fire cost was measured before it shipped: across the last 80 merged pull requests it
fired identically to the old positive list on all three lanes (`scripts/e2e_overfire_report.py`).

The `changes` job narrows one step further for the one case it can prove safe (BE-0322): a change
confined to a lane's scenario files fires only the jobs that declare a changed scenario, rather than
the whole lane. The filter reads the `scenarios:` each iOS job declares in the workflow — and, for
the `codegen` and `visual` jobs, the scenarios their `demos/showcase/Makefile` targets run (BE-0338),
since those two name their scenarios in a Makefile target rather than a workflow input — so the map
from a changed scenario to the jobs that load it never drifts from what those jobs run, and emits —
alongside `relevant` — a `shared` flag and an `affected` job array the jobs guard on. A test pins the
Makefile-read attribution against those targets, so a target gaining or losing a scenario fails the
gate unless the map moves with it. Every other case still fires the whole lane: a shared-code change
(driver / runner / app / workflow code that can affect any scenario), the `conformance` dimension job
that declares no scenario subset (it drives the whole driver-conformance harness), and a lane whose
jobs are not scenario-keyed at all (Android and web). The decision over-selects toward the whole
lane, so it never skips a job a change could have broken, and — reading only the `git` diff, the
workflow's own declarations, and those Makefile targets — puts no LLM on the path and has no bearing
on any run's pass/fail verdict.

The dev tools live in the `dev` dependency group, so the Linux job runs `uv sync --group
dev` then `uv run --no-sync …` (plain `uv run` would re-sync to the default set and drop
them). The gate mirrors [`make check`](../Makefile) and the [`pre-push`](../.githooks/pre-push)
hook step-for-step; every check except `actionlint` and `gitleaks` (two tools CI installs
separately, each step skipping with a notice when its tool is absent) runs identically on a fresh
clone via `uv` alone, which is what makes "green locally" predict "green in CI". The skill-drift
check is in the `uv` half, not the exceptions: `apm-cli` is a `dev` dependency (BE-0390), so
`make lint-skills` has no skip branch.

### Every job declares a timeout

Every job under [`.github/workflows/`](../.github/workflows/) sets `timeout-minutes`. When a job
declares none, GitHub's own default waits 360 minutes before cancelling it, so a stalled job holds a
runner and spends Actions minutes for most of a day before a red check appears. The web lane has
already paid that cost: GitHub cancelled `conformance (playwright)` and `codegen (playwright)` at
almost four hours apiece, against the two to seven minutes each one takes when it passes.

A job whose bound follows from its own arithmetic sets its own value and gives the reasoning in a
comment beside it: 180 minutes for the AWS Device Farm submission, 60 for the iOS scenario jobs, 30
for the Android `smoke` job and 25 for the other Android scenario jobs, 20 for `claude-review`, 15
for the Android Gradle-cache warm-up and for each of `ai-smoke`'s two live-model jobs, and 10 for
`mcp-wire`. Every job carrying no such reasoning takes the repository default
of **30 minutes**. That default clears the slowest successful job we have measured, CodeQL's Swift
analysis at about 20 minutes, and still turns a stall into a red check inside the half hour.

Two jobs carry no `timeout-minutes` of their own: the ones in
[`docs-refresh.yml`](../.github/workflows/docs-refresh.yml) and
[`roadmap-refresh.yml`](../.github/workflows/roadmap-refresh.yml) that call the reusable
[`refresh.yml`](../.github/workflows/refresh.yml) workflow, where GitHub does not accept the key at
all. The 30-minute bound on `refresh.yml`'s own job covers both callers instead.

### What a failing E2E job collects (BE-0361, BE-0367)

The hardest E2E failures are the ones where nothing crashes. On iOS the resident XCUITest runner —
the XCTest host `bajutsu` spawns with `xcodebuild test-without-building` and drives over a loopback
Hypertext Transfer Protocol (HTTP) channel — reports `Timed out while requesting screenshot`, ends
its test method, and leaves the Python side to declare a mid-run crash. On Android the resident UI
Automator server — the on-device process the adb backend reads the element tree from — stops
answering, or the emulator's own renderer wedges, or the job runs out its `timeout-minutes`. No
process died in any of those, so a crash-report sweep finds nothing, and the state that would say
whether the device's render service wedged or the host starved it disappears before anyone opens the
job.

Both lanes answer that question the same way, in three layers split by what each one can see: inside
`bajutsu`, because the running process is the one thing that knows *when* a stall happens; from CI
about the device, which only something holding that device can read; and over time about the host,
which only a sampler running alongside the job can record. Every artifact lands under
`runs/diagnostics/`, which the jobs already upload — with one exception, the per-scenario crash
evidence below, which lands in the failing scenario's own evidence directory instead, because it
belongs to that one scenario rather than to the job. None of it reaches a verdict: the collection
writes files and nothing else, and the deterministic assertions still decide pass/fail.

One environment variable arms the first layer on both backends. `BAJUTSU_STALL_DIAGNOSTICS` names
the directory captures are written into and deliberately carries no backend prefix, so a single
operator setting turns the hook on for whichever backend a job drives. What a capture *reads* is
per backend, because the two share no command — a Simulator answers to `simctl` and a macOS host to
`vm_stat`, an emulator answers to `adb` and a Linux host to `top` — so each backend contributes its
own set of probes to one shared capture, which owns everything else: the opt-in gate, a wall-clock
budget per capture, a budget of two captures per trigger, and the summary each probe's outcome is
written to. The budget is per *trigger* so that one frequent trigger cannot
spend a shared budget before the crash the capture exists to explain ever arrives. The video trigger
was that frequent one: it fired on every scenario of these runners, green runs included, because the
confirmation it fired from watched the wrong signal. It no longer does, but a crash-looping run
still has more than one trigger competing, so the split stays. Unset — which it is everywhere but these two
lanes — the hook does nothing at all.

#### The iOS lane

Every iOS job that boots a Simulator except `codegen` collects the three layers into `runs/`;
`codegen`'s sole artifact is its `.xcresult`, so the collection has no `runs/` directory to ride
there.

- **Inside `bajutsu`.** `BAJUTSU_XCUITEST_RESULT_BUNDLES` gives every runner spawn a
  `-resultBundlePath`, so `runs/runner-logs/result-<udid>-<port>.xcresult` records what testmanagerd
  itself saw — the precise XCTest failure and its timestamps — rather than the paraphrase the
  captured stdout carries. The same argv pins `-collect-test-diagnostics never`: left at its
  `on-failure` default, `xcodebuild` embeds a Simulator `system.logarchive` in the bundle, measured
  at 163 MB for a single spawn, which is the whole-archive collection the targeted extracts below
  exist to replace. The stall capture fires the moment the channel declares a mid-run crash or a
  `recordVideo` produces no bytes, writing a timed `simctl` screenshot, `sample` output for the
  rendering processes, and a `ps` / `vm_stat` snapshot into
  `runs/diagnostics/stalls/stall-NN-<reason>-<pid>/`.
- **Inside `bajutsu`, per failing scenario, with nothing to opt into** (BE-0421). A scenario whose
  crash-recovery retries all exhausted gets a `crash-diagnostics/` subdirectory in its *own*
  evidence directory, `runs/<run_id>/<sid>/`, holding the tail of the crashed runner's captured
  output — far more of it than the twenty lines the crash warning folds in, but still bounded — and,
  whenever macOS wrote one for the faulted `xcodebuild` process, its `.ips` crash report; the
  scenario's failure string names that directory, so nobody has to already know it exists. This is
  the local-run and per-scenario counterpart to the sweep below: that one tars up the whole job's
  report store, which cannot say which crash belongs to which of a dozen scenarios, and runs in CI
  only — a developer reproducing the crash with a local `bajutsu run` got nothing from it at all.
  Both halves are best-effort: a report `ReportCrash` never wrote, or a `DiagnosticReports`
  directory this host does not have, leaves that entry out rather than failing anything. The runner
  output is collected for the two crash shapes the runner's own liveness signals name — `xcodebuild`
  exited, or it lingers past a test run its capture says has ended. A channel that simply stopped
  answering while the process runs on and writes no such marker is the wedged-Simulator shape
  BE-0354 escalates instead, and this directory stays empty for it.
- **From CI, about the Simulator and CoreSimulator**, through the
  [`collect-ios-diagnostics`](../.github/actions/collect-ios-diagnostics/action.yml) composite action
  every Simulator-driving job calls. Its cheap tier runs on every job: the tail of
  `CoreSimulator.log`, the booted device's own CoreSimulator log directory, a crash-report sweep
  widened past `.ips` / `.crash` to the hang, spin, and jetsam reports this failure class actually
  leaves, and a host snapshot (`sw_vers`, `sysctl`, `xcodebuild -version`, `simctl list`) that keys
  each red run against a runner image and hardware generation. Its heavy tier runs when the job's own
  run step failed, and not otherwise: `xcrun simctl diagnose`, and two targeted unified-log extracts.
- **Over time, about the host**, from the same action's `start` phase: a background sampler appends
  `top`, `vm_stat`, and `memory_pressure` to `runs/diagnostics/host-telemetry.log` every 20 seconds,
  and a one-shot render probe records how long a screenshot takes and whether a five-second
  `recordVideo` produces any bytes at all. The sampler is an observer outside every run loop, so its
  interval is a sampling cadence and not a wait a verdict depends on. It ranks `top`'s rows by
  resident size rather than CPU, because `top -l 1` differences nothing and so reports every process
  at 0.0% — sorting by CPU there sorts on a constant, and the first collection's rows never once
  included the app under test.
- **Before anything ran**, also from `start`: one bounded `ps aux` plus `vm_stat` land in
  `runs/diagnostics/ps-baseline.txt`. The stall captures snapshot `ps` at a stall, which answers what
  was resident when it broke and cannot answer whether that differed from normal — the first
  collection had to settle that question from a launch-env argument instead of a measurement. Both
  this and the render probe are written once per job, so the second `start` a job may make never
  overwrites the pre-run reading with a mid-job one.

We split the unified-log extract in two on purpose, and the reason is worth stating because the
opposite is the natural assumption. The Simulator's guest processes that serve screenshots —
`SpringBoard`, `backboardd`, and `testmanagerd` — do **not** write to the host's unified log.
Measured on a booted device, a host-side `log show` filtered on those process names returns its
header and nothing else. The guest's entries live in the device's own log store, so the action runs a
second `log show` inside the guest through `simctl spawn`, and keeps the host-side extract for the
CoreSimulator service processes that genuinely do log there. A single host-side extract would have
produced an empty file — the same empty-by-construction artifact this collection replaced.

#### The Android lane

Every job that boots an Android Virtual Device (AVD) collects the same three layers, `uiautomator
(codegen)` included. That job is where the collection matters most, not least: every path it uploads
is produced by Gradle *running the generated test*, so a failure before that point — a wedged AVD
boot, a codegen or build error, the job's own `timeout-minutes` — left `if-no-files-found: ignore`
to drop the artifact silently, and a job that failed outside its own test uploaded nothing at all.
Unlike the six `bajutsu run` jobs it runs no adb driver at test time, so the resident-read trigger
cannot fire there; its Layer 1 hook is the host-side recorder
([`screenrecord.py`](../demos/showcase/android/screenrecord.py)) confirming its recording is
producing bytes, which is what a wedged renderer — this lane's known flake — fails to do.

- **Inside `bajutsu`.** The stall capture fires at two moments, both narrow on purpose. The first is
  the resident channel's hierarchy-read fallback, where the driver gives up on the channel and
  degrades to the `uiautomator dump` subprocess for the rest of the lease — the point at which the
  read channel is gone rather than momentarily noisy. The second is a check that the device-side
  `screenrecord` is producing bytes rather than merely running, which separates a wedged renderer
  from a recording that never started. Either moment writes a host `ps` and `top` snapshot,
  `dumpsys SurfaceFlinger --latency`, and a `logcat` tail into
  `runs/diagnostics/stalls/stall-NN-<reason>-<pid>/`. The actuation path is excluded deliberately:
  two of the resident channel's own errors fire during perfectly healthy runs — an older server that
  has no actuation endpoint, and a lost response the driver treats as having landed rather than
  actuating twice — so capturing there would spend the per-trigger budget before a genuine stall
  could use it.
- **From CI, about the emulator**, through
  [`scripts/collect_android_diagnostics.sh`](../scripts/collect_android_diagnostics.sh). This half is
  a script rather than a composite action, and the constraint behind that is the sharpest way the
  Android lane diverges from the iOS one. Every device-touching step in the lane runs *inside* a
  `reactivecircus/android-emulator-runner` step, which boots the AVD, runs its `script:`, and kills
  it when that step ends. An ordinary step placed after it — and a step-level `if: failure()` gate —
  would therefore run with no device attached and collect nothing at all. So the sweep is invoked
  from the tail of each job's own `script:` and keyed off the run command's own exit code, the same
  shape the lane's `poll_cpuinfo` poller already uses to survive a failing run. Its cheap tier runs
  on every job: a full-buffer `logcat -d` dump — which, unlike the per-scenario `deviceLog` interval
  evidence, reads the device's own retained ring buffer and so still has something to show for a job
  that failed before any scenario's capture began — plus `dumpsys meminfo`, `getprop`, and
  `adb devices -l` for the environment snapshot that keys a red run against an API level and ABI.
  Its heavy tier runs when the run itself failed, and not otherwise: `adb bugreport`, Android's own
  comprehensive collector, and a rooted pull of `/data/tombstones` (native crash reports) and
  `/data/anr/` (Application Not Responding (ANR) traces) — the two crash-report classes the iOS
  sweep gets for free from the host's own diagnostic reports, but which live device-side here.
  The sweep bounds itself as well as each command, for the same reason the in-process capture
  carries a budget above its per-probe timeouts: on the wedged-device failure this exists to
  document, every `adb` read hangs to its full ceiling, so per-command ceilings alone would let the
  collection spend most of a job's `timeout-minutes` and turn a reportable failure into a cancelled
  job holding a truncated collection. A read the deadline cuts off says so in its own file, since
  "the budget ran out" and "this read found nothing" are different answers about the device.
- **Over time, about the host**, from the
  [`collect-android-diagnostics`](../.github/actions/collect-android-diagnostics/action.yml)
  composite action, whose `start` phase before each job's emulator step launches a background
  sampler appending `top` and `free` output to `runs/diagnostics/host-telemetry.log` about every 20
  seconds, and whose `collect` phase after it stops the sampler. Reading only the Linux runner is
  what lets this layer be a step at all, and bracketing the emulator step from outside is what lets
  it cover a job whose emulator step died outright.

The host sampler coexists with the lane's existing `poll_cpuinfo` input rather than replacing it,
because the two measure different things from different sides: `poll_cpuinfo` reads the guest's view
over adb, the sampler reads the host's own load. That difference is also why the sampler is always on
where `poll_cpuinfo` is opt-in — a host-side sample every 20 seconds issues no adb traffic and
samples a tenth as often, so it lacks the observer effect that keeps `poll_cpuinfo` off by default.

The two lanes keep separate composite actions on purpose. `collect-ios-diagnostics` reads `simctl`,
CoreSimulator, and the macOS unified log; `collect-android-diagnostics` reads the Linux host. The
underlying tools share no surface, so a single action would be a branch on the backend rather than
shared logic. What the two do share is the design — the cheap/heavy split, the `start` / `collect`
telemetry phases — and, inside `bajutsu`, the capture itself.

**Reading what a red lane collected.** None of this appears in `gh run view --log-failed`: it is
uploaded inside the failing job's own `runs/` artifact, so a session diagnosing from the log alone
never sees it. The [`investigate-ci-failure`](../.apm/skills/investigate-ci-failure/SKILL.md) agent
skill downloads that artifact and matches what it holds against the symptoms recorded in
[`known-ci-failure-patterns.md`](../.apm/skills/investigate-ci-failure/references/known-ci-failure-patterns.md),
classifying the failure as a known infrastructure flake, a real defect, or something still
unclassified. [`pr-followup`](../.apm/skills/pr-followup/SKILL.md) runs it before touching a fix, so
a host flake is not answered with a code change and a regression is not waved through as a flake.
The skill reports; it never edits, pushes, or re-runs.

## Running bajutsu in your app's CI

> bajutsu is pre-release (unpublished). Until it is on PyPI, vendor it (a submodule or a
> checkout) and run the action from that checkout — the action runs `uv sync` against
> bajutsu's `pyproject.toml`.

bajutsu produces CI-ready output: a `junit.xml`, a self-contained `report.html`,
a `0` / `1` exit code, and — inside Actions — failure **annotations** + a job **summary**
(see below). On a macOS runner, follow these two steps:

1. **Build and install your app** (and the XCUITest runner) onto a booted Simulator (this varies
   per app, so it stays yours — `xcodebuild` + `xcrun simctl install`).
2. **Run bajutsu** with the [`bajutsu-e2e`](../.github/actions/bajutsu-e2e/action.yml)
   composite action — it syncs deps, runs your scenarios (with `run --score`, so the log carries
   the entry-screen convention grade), and uploads the run (report + screenshots + video +
   `network.json`) as an artifact. The XCUITest backend needs no pip extra — its runner is driven
   over HTTP and `xcodebuild` ships with Xcode on the runner.

```yaml
jobs:
  e2e:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - uses: maxim-lobanov/setup-xcode@v1
        with: { xcode-version: latest-stable }
      - uses: astral-sh/setup-uv@v6
        with: { enable-cache: true }

      # --- build + install your app (your build, on a booted Simulator) ---
      - run: xcodebuild -scheme MyApp -destination 'generic/platform=iOS Simulator' -derivedDataPath dd build
      - id: sim
        run: |
          udid=$(xcrun simctl create ci "iPhone 16")
          xcrun simctl boot "$udid"; xcrun simctl bootstatus "$udid" -b
          echo "udid=$udid" >> "$GITHUB_OUTPUT"
      - run: xcrun simctl install "${{ steps.sim.outputs.udid }}" dd/Build/Products/Debug-iphonesimulator/MyApp.app

      # --- run bajutsu ---
      - uses: your-org/bajutsu/.github/actions/bajutsu-e2e@main
        with:
          scenarios: e2e/*.yaml
          app: myapp
          udid: ${{ steps.sim.outputs.udid }}
```

### Failure annotations + job summary

When `GITHUB_ACTIONS` is set, `bajutsu run` emits a `::error::` annotation per failed
scenario (shown inline on the PR) and appends a PASS/FAIL table to `$GITHUB_STEP_SUMMARY`.
No flag needed — it auto-detects the Actions environment.

### Notes

- **JUnit**: `junit.xml` is written next to the report; feed it to a test-reporter action
  (e.g. `dorny/test-reporter`) for an inline test view.
- **Determinism**: use scenario [`mocks`](network.md#deterministic-mocks) to stub the
  network so runs do not depend on a live server.
- **Convention score**: the composite action passes `run --score`, so the run prints the app's
  entry-screen convention grade (`Ready` / `Partial` / `Blocked`, the same score `doctor` reports)
  to stderr, computed from the run's own first launch. It is diagnostic only — never on the pass/fail
  path — and folding it into the run avoids a separate `doctor` step that would cold-spawn a second
  XCUITest runner. Run `bajutsu doctor` locally for the fuller preflight (runnability + capability
  checks); a hard env/permission runnability gate in CI (`xcodebuild` / Xcode presence) is future work.
- **Affected-step selection**: [`bajutsu impact`](cli.md#impact) reports which scenario steps a `git`
  diff is likely to affect, so a pipeline can order those steps first for fast feedback. The safe
  default is additive — run the whole suite, just affected-first. Narrowing a pre-merge run down to the
  affected set is opt-in and sound only with both safeguards `impact` documents: fall back to the full
  suite whenever the report is **incomplete** (`--json`'s `complete` is `false`, i.e. the diff carried
  a change that maps to no referenced id), and still run the full deterministic suite at a coarser
  cadence (on merge, nightly, or at release). The pass/fail verdict always stays with `run`.
