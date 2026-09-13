"""A system prompt the guard dismissed so a blocked step could proceed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AlertEventKind = Literal["alert", "notificationBanner"]


@dataclass
class AlertEvent:
    """A system prompt the guard dismissed so a blocked step/expect could proceed.

    Recorded on the outcome (StepOutcome.alerts / RunResult.expect_alerts) and surfaced in
    the report, so a step that only passed on a retry isn't shown as if nothing had blocked
    it. `label` is the button the guard tapped (e.g. "Not Now"); empty when the locator
    named none.

    `kind` tells an alert dismissal apart from a foreground notification banner swiped away at
    interruption time (BE-0416). Without it the two are indistinguishable in the report: a banner
    carries no button, so its `label` is the notification's own text rather than anything tapped,
    and a reader would have no way to tell that from an alert whose locator named no button. It
    defaults to the alert case, so every dismissal that predates the banner path reads unchanged."""

    label: str = ""
    kind: AlertEventKind = "alert"
