"""The process-local session store — a restart drops every session."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from .principal import HUMAN, Principal, PrincipalKind


@dataclass(frozen=True)
class _Entry:
    """One live session: who it belongs to, and when it stops counting."""

    principal: Principal
    expires_at: datetime | None


class InMemorySessionStore:
    """Sessions in a process-local map (the pre-7b behavior) — a restart drops them, so the user
    simply logs in again. Maps each id to the principal it belongs to (an identity of None for a
    shared-token login).

    A session expires only when one was asked for at `issue`. A human session passes none and so
    lives until the process does, exactly as before; a machine session (BE-0414 unit 1) passes the
    cap its OIDC token's own `exp` imposes, and this store enforces it — a credential whose whole
    point is being short-lived must not outlive its bound just because a deployment has no
    database.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        identity: str | None = None,
        *,
        expires_at: datetime | None = None,
        org: str | None = None,
        kind: PrincipalKind = HUMAN,
    ) -> str:
        sid = secrets.token_urlsafe(32)
        entry = _Entry(Principal(identity=identity, org=org, kind=kind), expires_at)
        with self._lock:
            self._sessions[sid] = entry
        return sid

    def valid(self, sid: str) -> bool:
        return self._live(sid) is not None

    def identity(self, sid: str) -> str | None:
        entry = self._live(sid)
        return entry.principal.identity if entry is not None else None

    def principal(self, sid: str) -> Principal | None:
        entry = self._live(sid)
        return entry.principal if entry is not None else None

    def _live(self, sid: str) -> _Entry | None:
        """The session *sid* names while it is still live. An expired one is dropped on the way
        past rather than left to accumulate — this store has no sweep of its own."""
        with self._lock:
            entry = self._sessions.get(sid)
            if entry is None:
                return None
            if entry.expires_at is not None and entry.expires_at <= datetime.now(UTC):
                del self._sessions[sid]
                return None
            return entry

    def revoke_identities(self, identities: Iterable[str]) -> int:
        wanted = set(identities)
        if not wanted:
            return 0
        with self._lock:
            doomed = [
                sid for sid, entry in self._sessions.items() if entry.principal.identity in wanted
            ]
            for sid in doomed:
                del self._sessions[sid]
        return len(doomed)
