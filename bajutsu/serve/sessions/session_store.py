"""The seam opaque login-session ids are issued and validated through."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol, runtime_checkable

from .principal import HUMAN, Principal, PrincipalKind


@runtime_checkable
class SessionStore(Protocol):
    """Issues and validates opaque login-session ids, optionally bound to an identity."""

    def issue(
        self,
        identity: str | None = None,
        *,
        expires_at: datetime | None = None,
        org: str | None = None,
        kind: PrincipalKind = HUMAN,
    ) -> str:
        """Mint and remember a new opaque session id, optionally bound to *identity*.

        *expires_at* pins this one session's expiry instead of taking the store-level time-to-live
        (`BAJUTSU_SESSION_TTL`). A machine session needs it because its lifetime is capped by the
        presented token's own `exp` (BE-0414 unit 1): the exchange only improves on reusing that
        token if what it mints is shorter-lived. *org* and *kind* record what the gate then reads
        per request without re-deriving it.

        *expires_at* must be timezone-aware, and passing one already at or behind now mints no
        usable session: the id comes back, but it never validates — whether because no store held
        it or because the row it wrote is already expired. Callers that compute a cap decide
        whether that is worth an answer; `oidc_exchange` refuses ahead of this rather than handing
        a pipeline a cookie that 401s on its next call.
        """

    def valid(self, sid: str) -> bool:
        """Whether *sid* is a known, live session."""

    def identity(self, sid: str) -> str | None:
        """The identity bound to *sid* (e.g. a GitHub login), or None if it has none / is unknown."""

    def principal(self, sid: str) -> Principal | None:
        """Who *sid* belongs to, or None when it is unknown or expired (BE-0414 unit 1).

        The fuller read behind `identity`: the request gate needs the principal *kind* to decide
        which gate governs the request at all, and a machine session's org to scope what it may
        reach — neither of which an identity string is allowed to encode by convention.
        """

    def revoke_identities(self, identities: Iterable[str]) -> int:
        """Drop every live session bound to one of *identities*; returns how many were dropped.

        Retiring an org has to reach the sessions its members already hold (BE-0375): a soft delete
        turns away their *next* sign-in, but a cookie issued before it keeps acting as that tenant
        until it expires. Sessions carrying no identity (a shared-token login) are never touched —
        they belong to no org.
        """
