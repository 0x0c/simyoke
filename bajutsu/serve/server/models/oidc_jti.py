"""The spent-token table: one row per OIDC `jti` an exchange has already consumed."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from .base import Base


class OidcJti(Base):
    """One OIDC token identifier already spent at the exchange (BE-0414 unit 1).

    Reads as a cache and is a table like any other, for a reason the hosted shape forces: the
    control plane runs as several replicas over one database, so a per-process cache would fall to
    a replay against the second replica and leave the serve-side age ceiling as the only real
    bound. It costs one write per job, since a token is exchanged once and every later call in the
    pipeline presents the minted session instead.

    `expires_at` is the presented token's own `exp`, but the row outlives it: the sweep in
    `SqlRepository.spend_oidc_jti` deletes only past `exp` plus the clock skew the lifetime checks
    allow, since a token inside that window is still acceptable and must still be refused here.
    """

    __tablename__ = "oidc_jti"

    # The `jti` claim itself as the primary key — the uniqueness constraint *is* the single-use
    # rule, so two replicas racing the same token cannot both win.
    id: Mapped[str] = mapped_column(primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
