"""The authentication cluster carved out of the server state (BE-0248)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bajutsu.serve.oidc import OidcConfig
from bajutsu.serve.sessions import (
    HUMAN,
    InMemorySessionStore,
    Principal,
    PrincipalKind,
    SessionStore,
)

if TYPE_CHECKING:
    from datetime import datetime

    from bajutsu.serve.server.oauth import OAuthClient


@dataclass
class SessionManager:
    """The authentication cluster carved out of `ServeState` (BE-0248): the shared token, the login
    sessions, and the GitHub OAuth configuration, plus the "is this request authenticated, and as
    whom" methods that read them. Grouping them behind one boundary answers that question in one
    place, exactly as `JobRegistry` (BE-0198) did for job registration.

    `token` is the optional shared token (None = open, the loopback-only legacy behavior; once
    `oauth` is set it narrows to worker traffic, BE-0313); a login exchanges it for an opaque session
    id held by `sessions` — the token itself never lives in the browser. `sessions` is a swappable
    `SessionStore` seam (in-memory by default; a server backend swaps in a database-backed store,
    BE-0015 7b / BE-0106). `oauth` is the GitHub OAuth client (None = OAuth not configured); sign-in and the
    viewer/editor role then follow GitHub org and Team membership (`authz.py`, BE-0313), and
    `oauth_admin_teams` are the server-wide GitHub Teams (each `"<github-org>/<team-slug>"`) whose
    members are admin — a member of any of them also clears the sign-in gate itself, not only the
    admin role, so an admin can still sign in and repoint a broken `orgs:` config even when no org
    lists their GitHub organization. Annotated as a tuple rather than a list so `mypy` rejects a
    caller handing over a collection it still holds a reference to — the same "don't alias a
    caller-owned collection" concern `JobRegistry._register` handles by copying. The OAuth fields are
    fixed at server construction and never change after, so they travel with the token/session state
    they gate.
    """

    token: str | None = None
    sessions: SessionStore = field(default_factory=InMemorySessionStore)
    oauth: OAuthClient | None = None
    oauth_admin_teams: tuple[str, ...] = ()
    # The OIDC settings a CI job's token is verified against (BE-0414), None when the deployment
    # configures no expected audience — which disables the machine caller shape outright rather
    # than falling back to accepting whichever audience a token carries. It sits here beside
    # `oauth` because it is the third way a caller becomes authenticated, fixed at construction
    # like the two above it.
    oidc: OidcConfig | None = None

    def check_token(self, candidate: str) -> bool:
        """Constant-time compare of a presented token against the configured one."""
        return self.token is not None and secrets.compare_digest(candidate, self.token)

    def issue_session(
        self,
        identity: str | None = None,
        *,
        expires_at: datetime | None = None,
        org: str | None = None,
        kind: PrincipalKind = HUMAN,
    ) -> str:
        """Mint and remember a new opaque session id (returned to set as a cookie at login),
        optionally bound to *identity* (the GitHub login from an OAuth login).

        The OIDC exchange mints its machine session through this same call (BE-0414), passing the
        cap its token's `exp` imposes plus the org and kind the gate reads back per request.
        """
        return self.sessions.issue(identity, expires_at=expires_at, org=org, kind=kind)

    def valid_session(self, sid: str) -> bool:
        """Whether *sid* is a known, live session id."""
        return self.sessions.valid(sid)

    def principal(self, sid: str) -> Principal | None:
        """Who *sid* belongs to, or None when it is unknown or expired (BE-0414)."""
        return self.sessions.principal(sid)
