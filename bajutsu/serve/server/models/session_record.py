"""The sessions table: one row per issued login session."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from .base import Base


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(primary_key=True)
    identity: Mapped[str | None] = mapped_column(default=None)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Who the session belongs to beyond its identity string (BE-0414 unit 1). `kind` is "machine"
    # for a session minted at the OIDC exchange and "human" (or NULL, on a row predating these
    # columns) otherwise; the request gate branches on it, since a human and a pipeline are
    # governed by different gates. `org` is the tenant a machine session acts as, resolved once at
    # the exchange from the token's claims rather than re-read per request from a user row it has
    # none of — NULL for a human session, whose org comes from that row.
    org: Mapped[str | None] = mapped_column(default=None)
    kind: Mapped[str | None] = mapped_column(default=None)
