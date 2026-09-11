"""The session store over a sessions table, on Postgres or SQLite (BE-0106)."""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from bajutsu.serve.sessions import HUMAN, Principal, PrincipalKind

from ._shared import _DEFAULT_TTL

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from bajutsu.serve.server.models import SessionRecord


class SqlSessionStore:
    """SessionStore backed by a Postgres (or SQLite) sessions table (BE-0106).

    Replaces `RedisSessionStore`: sessions survive a restart and span replicas exactly as the Redis
    store did, with no second stateful service. Expiry is enforced on read; the engine is injected
    so a test can hand in an in-memory SQLite."""

    def __init__(self, engine: Engine, *, ttl: int = _DEFAULT_TTL) -> None:
        self._engine = engine
        self._ttl = ttl

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _ensure_aware(dt: datetime) -> datetime:
        # SQLite returns naive datetimes; Postgres returns aware ones.
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt

    def issue(
        self,
        identity: str | None = None,
        *,
        expires_at: datetime | None = None,
        org: str | None = None,
        kind: PrincipalKind = HUMAN,
    ) -> str:
        from sqlalchemy.orm import Session

        from bajutsu.serve.server.models import SessionRecord

        sid = secrets.token_urlsafe(32)
        # A caller's own expiry wins over the store-level time-to-live, never extends it: a machine
        # session is capped by its token's `exp` (BE-0414 unit 1), and the cap is the whole reason
        # the exchange is an improvement on re-presenting that token.
        store_expiry = self._now() + timedelta(seconds=self._ttl)
        expires = store_expiry if expires_at is None else min(expires_at, store_expiry)
        with Session(self._engine) as session:
            session.add(
                SessionRecord(id=sid, identity=identity, expires_at=expires, org=org, kind=kind)
            )
            session.commit()
        return sid

    def valid(self, sid: str) -> bool:
        return self._live(sid) is not None

    def identity(self, sid: str) -> str | None:
        row = self._live(sid)
        return row.identity if row is not None else None

    def principal(self, sid: str) -> Principal | None:
        row = self._live(sid)
        if row is None:
            return None
        return Principal.from_stored(row.identity, row.org, row.kind)

    def _live(self, sid: str) -> SessionRecord | None:
        """The row *sid* names while it is still live. Expiry is enforced on read; nothing sweeps
        an expired row, it simply stops validating."""
        from sqlalchemy.orm import Session

        from bajutsu.serve.server.models import SessionRecord

        with Session(self._engine) as session:
            row = session.get(SessionRecord, sid)
            if row is None or self._ensure_aware(row.expires_at) < self._now():
                return None
            session.expunge(row)
            return row

    def revoke_identities(self, identities: Iterable[str]) -> int:
        from sqlalchemy import delete
        from sqlalchemy.orm import Session

        from bajutsu.serve.server.models import SessionRecord

        wanted = list(set(identities))
        if not wanted:
            return 0
        # Rows are removed rather than expired in place: a revoked session must not come back if a
        # clock moves, and `valid`/`identity` both read the row before checking its expiry.
        with Session(self._engine) as session:
            result = session.execute(
                delete(SessionRecord).where(SessionRecord.identity.in_(wanted))
            )
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0)
