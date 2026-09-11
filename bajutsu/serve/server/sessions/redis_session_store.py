"""The session store over Redis keys with a TTL, so finished sessions expire themselves."""

from __future__ import annotations

import json
import math
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime

from bajutsu.serve.sessions import HUMAN, MACHINE, Principal, PrincipalKind

from ._shared import _DEFAULT_TTL
from .redis_like import RedisLike

_SESSION = "bajutsu:session:"  # Redis key prefix for a login-session id


class RedisSessionStore:
    """SessionStore backed by Redis keys with a TTL, so finished sessions self-expire.

    The key's value carries the session's principal as JSON since BE-0414 unit 1 — a machine
    session needs its org and kind alongside the identity. A value written before that is a bare
    identity string (or empty for none) and is still read as the human session it was, so a store
    holding live keys across an upgrade does not sign everybody out.
    """

    def __init__(self, redis: RedisLike, *, ttl: int = _DEFAULT_TTL) -> None:
        self._redis = redis
        self._ttl = ttl

    def issue(
        self,
        identity: str | None = None,
        *,
        expires_at: datetime | None = None,
        org: str | None = None,
        kind: PrincipalKind = HUMAN,
    ) -> str:
        sid = secrets.token_urlsafe(32)
        ttl = self._ttl
        if expires_at is not None:
            # Redis expires the key itself, so the caller's cap becomes the key's own lifetime —
            # never longer than the store-level one.
            # Rounded *up*, so a cap less than a second away still mints the second Redis needs
            # to hold a key at all — `issue` promises to remember what it returns, and a caller
            # whose cap is in the future must not get an id that was never stored.
            ttl = min(ttl, math.ceil((expires_at - datetime.now(UTC)).total_seconds()))
        if ttl <= 0:
            # A cap already at or behind now: write nothing, so the id this returns was never a
            # live session. Redis rejects a non-positive TTL outright, and rounding one up would
            # hand back a credential the cap said not to mint. `SessionStore.issue` documents it.
            return sid
        value = json.dumps({"identity": identity, "org": org, "kind": kind})
        self._redis.setex(f"{_SESSION}{sid}", ttl, value)
        return sid

    def valid(self, sid: str) -> bool:
        return bool(self._redis.exists(f"{_SESSION}{sid}"))

    def identity(self, sid: str) -> str | None:
        principal = self.principal(sid)
        return principal.identity if principal is not None else None

    def principal(self, sid: str) -> Principal | None:
        raw = self._redis.get(f"{_SESSION}{sid}")
        return None if raw is None else _principal(_decode(raw))

    def revoke_identities(self, identities: Iterable[str]) -> int:
        wanted = set(identities)
        if not wanted:
            return 0
        # The identity is the key's *value*, so there is no index to look it up by — every live
        # session key has to be read. Acceptable because revocation is a rare admin action (retiring
        # an org), and the alternative is leaving this store unable to revoke at all, which is the
        # hole BE-0375 closed for the two stores a deployment actually runs.
        doomed = []
        for key in self._redis.scan_iter(f"{_SESSION}*"):
            name = key.decode() if isinstance(key, bytes) else str(key)
            raw = self._redis.get(name)
            if raw is None:
                continue
            if _principal(_decode(raw)).identity in wanted:
                doomed.append(name)
        if doomed:
            self._redis.delete(*doomed)
        return len(doomed)


def _decode(raw: object) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)


def _principal(value: str) -> Principal:
    """The stored value as a principal, reading a pre-BE-0414 bare identity string as a human one.

    The legacy branch is gated on the value not *looking* like a record, rather than on failing to
    parse as one: a corrupted record would otherwise fall through it and read as a human session
    carrying the raw blob as its identity — and a human session on a database-less deployment
    skips the role gate entirely, which is the full access `forbidden_for_machine` exists to deny.
    A GitHub login cannot begin with `{`, so nothing legitimate is caught by this test.
    """
    if not value.startswith("{"):
        return Principal(identity=value or None)
    try:
        stored = json.loads(value)
    except ValueError:
        stored = None
    if not isinstance(stored, dict):
        # Well-formed-looking but unreadable: fail closed onto the narrower kind rather than
        # handing back a human principal built out of a blob nobody wrote.
        return Principal(identity=None, org=None, kind=MACHINE)
    # A missing `kind` reads as *machine* here, not human as it does for a SQL row: a NULL column
    # is a row written before these existed, but a value that is JSON at all was written by this
    # code, which always records one. Its absence is corruption, so it meets the narrower gate.
    kind = stored.get("kind", MACHINE)
    return Principal.from_stored(stored.get("identity"), stored.get("org"), kind)
