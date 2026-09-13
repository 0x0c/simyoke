"""The accumulator a driver folds its tap results into between drains (BE-0407)."""

from __future__ import annotations

from dataclasses import dataclass, field

from bajutsu.common.drivers import base


@dataclass
class _DrainCarry:
    """Mutable holder for a driver's `/tap`-fold accumulator (BE-0407 Unit 6).

    A plain object the `_tracking_transport` closure captures, rather than `self` directly: closing
    over `self` there would make `XcuitestDriver` hold a reference to a closure that in turn
    references `XcuitestDriver` — a cycle only the cyclic garbage collector breaks, deferring a
    superseded driver's cleanup (and the connection Unit 11 now keeps open behind it) to whenever
    that collector next runs, rather than the moment its last caller-side reference drops. Holding
    the carry here instead keeps the closure's only capture a value with no path back to the driver,
    so plain reference counting reclaims a driver — and closes its socket — as soon as the caller
    that replaced it drops its own reference.
    """

    drained: base.DrainedInterruptions = field(default_factory=base.DrainedInterruptions.empty)
    is_current: bool = False
