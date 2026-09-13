"""One drain's record from the interruption monitor: what it tapped, declined, and swiped away."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DrainedInterruptions:
    """One drain's worth of what the interruption monitor did, split by what it did.

    `declined` is the button lists of alerts the policy governed but no rule identified — a monitor
    that declines still has to answer XCUITest's alert (its own default button, unchanged), so this
    is not a second dismissal outcome, only a record of what the tap was never asked to be. Reported
    so the caller can fail the step/expect that met one, naming what was on screen, rather than
    letting the run continue as if nothing had answered on the scenario's behalf (BE-0406 Unit 2b).

    `banners` is the notification text of each foreground notification banner the monitor swiped
    away (BE-0416). Kept apart from `tapped` because the two dismissals are not interchangeable: a
    tapped label names the button the scenario's own policy chose, while a banner has no button to
    name and no policy to choose it — it is answered on every run, since nothing a scenario can
    declare would identify one.
    """

    tapped: list[str]
    declined: list[list[str]]
    banners: list[str]

    @classmethod
    def empty(cls) -> DrainedInterruptions:
        """A drain that carried nothing — and the reset every carry is cleared to."""
        return cls(tapped=[], declined=[], banners=[])

    def merged_with(self, later: DrainedInterruptions) -> DrainedInterruptions:
        """This drain followed by `later`, oldest first within each field."""
        return DrainedInterruptions(
            tapped=self.tapped + later.tapped,
            declined=self.declined + later.declined,
            banners=self.banners + later.banners,
        )
