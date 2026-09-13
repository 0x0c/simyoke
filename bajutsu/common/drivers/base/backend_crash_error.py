"""The error raised when a backend's driver process crashed and could not be recovered."""

from __future__ import annotations

from typing import Any


class BackendCrashError(RuntimeError):
    """The backend's driver process crashed mid-scenario and could not be recovered in place.

    Distinct from a test outcome and from a transient blip a driver's own retry absorbs: it names an
    honest "the backend died" — the resident XCUITest runner's XCTest host, an adb server, a browser
    process — where the crash outlived the driver's in-place recovery budget, so the current
    scenario's state is gone. The run pipeline treats it as backend infrastructure, not a verdict
    (prime directive 1): it discards the dead lease, leases a fresh device (a cold respawn), and
    re-runs the whole scenario from the start, bounded — a genuinely crash-inducing app still fails
    loudly once the retries are spent, so flakiness is never absorbed into a pass (BE-0049). Backends
    raise a subclass (e.g. `XcuitestRunnerCrashError`); the pipeline catches this base so the recovery
    stays backend-agnostic (prime directive 3).
    """

    # Filled in by `orchestrator.run_scenario` before this propagates out of a crashed scenario: its
    # own interval finalize still runs first (in its `finally`), so a video/deviceLog/appTrace
    # recording that was in flight when the backend died may already be on disk. The crash-retry loop
    # in `runner.pipeline` reads this back when building the exhausted-retry `RunResult`, so a
    # recording that *was* captured on the doomed attempt still reaches the report instead of a bare
    # "unavailable" disclosure. Stays `None` for a crash raised before `run_scenario` was ever
    # entered (e.g. during lease bring-up) — there was never a recording to recover.
    #
    # Typed as `list[Any]` (really `list[evidence.Artifact]`) rather than importing the real type:
    # `evidence` depends on `drivers.base` (BackendCrashError's own package), never the reverse — the
    # module-layering `make lint-imports` enforces — so this module cannot name that type at all,
    # `TYPE_CHECKING` guard or not (import-linter still walks those).
    partial_artifacts: list[Any] | None = None
