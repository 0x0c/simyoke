"""Tests for the SessionStore seam (BE-0015 7b-1, BE-0106).

`InMemorySessionStore` is the local default — sessions live in one process, so a restart drops them.
`RedisSessionStore` is the legacy server implementation (kept for reference); `SqlSessionStore` is its
replacement (BE-0106): sessions in the same Postgres the system of record already uses, so no Redis
is needed. Both server stores survive a restart and span replicas. The redis client / SQL engine are
injected, so in-memory fakes (a dict for Redis, SQLite for SQL) drive the contract — no live
Redis or Postgres on the gate. The `SqlSessionStore` cases run against in-memory SQLite in the gate
and, behind the `postgres` marker, against a real Postgres service in the serve-db.yml lane
(BE-0309)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine

from bajutsu.serve.server.models import Base
from bajutsu.serve.server.sessions import _DEFAULT_TTL, RedisSessionStore, SqlSessionStore
from bajutsu.serve.sessions import (
    HUMAN,
    MACHINE,
    InMemorySessionStore,
    Principal,
    kind_from_stored,
)


def test_in_memory_issue_then_valid() -> None:
    store = InMemorySessionStore()
    sid = store.issue()
    assert store.valid(sid)


def test_in_memory_unknown_is_invalid() -> None:
    assert not InMemorySessionStore().valid("nope")


def test_in_memory_binds_and_reads_identity() -> None:
    store = InMemorySessionStore()
    sid = store.issue("alice")
    assert store.identity(sid) == "alice"
    # a token login carries no identity; an unknown id has none either
    assert store.identity(store.issue()) is None
    assert store.identity("nope") is None


def test_in_memory_ids_are_unique_and_opaque() -> None:
    store = InMemorySessionStore()
    a, b = store.issue(), store.issue()
    assert a != b
    assert len(a) > 20  # secrets.token_urlsafe(32) is not a short, guessable id


class FakeRedis:
    """The slice of a redis client RedisSessionStore uses, in memory. Records TTLs so a test can
    assert each session key self-expires."""

    def __init__(self) -> None:
        self._kv: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def setex(self, key: str, seconds: int, value: str) -> object:
        self._kv[key] = value
        self.ttls[key] = seconds
        return True

    def exists(self, key: str) -> int:
        return 1 if key in self._kv else 0

    def get(self, key: str) -> bytes | None:
        v = self._kv.get(key)
        return v.encode() if v is not None else None

    def scan_iter(self, match: str) -> list[bytes]:
        prefix = match.rstrip("*")
        return [k.encode() for k in self._kv if k.startswith(prefix)]

    def delete(self, *keys: str) -> int:
        gone = [k for k in keys if self._kv.pop(k, None) is not None]
        return len(gone)


def test_redis_issue_then_valid() -> None:
    store = RedisSessionStore(FakeRedis())
    sid = store.issue()
    assert store.valid(sid)


def test_redis_unknown_is_invalid() -> None:
    assert not RedisSessionStore(FakeRedis()).valid("nope")


def test_redis_binds_and_reads_identity() -> None:
    store = RedisSessionStore(FakeRedis())
    assert store.identity(store.issue("bob")) == "bob"
    # a token login carries no identity; an unknown id has none
    assert store.identity(store.issue()) is None
    assert store.identity("nope") is None


def test_redis_issue_sets_the_injected_ttl() -> None:
    redis = FakeRedis()
    RedisSessionStore(redis, ttl=123).issue()
    assert list(redis.ttls.values()) == [123]


def test_redis_issue_uses_the_default_ttl() -> None:
    redis = FakeRedis()
    RedisSessionStore(redis).issue()
    assert list(redis.ttls.values()) == [_DEFAULT_TTL]


def test_session_ttl_from_env_parses_and_validates() -> None:
    from bajutsu.serve import _session_ttl_from_env

    assert _session_ttl_from_env(None, 99) == 99  # unset -> default
    assert _session_ttl_from_env("", 99) == 99  # empty -> default
    assert _session_ttl_from_env("3600", 99) == 3600
    for bad in ("7d", "abc", "1.5"):
        with pytest.raises(ValueError, match="BAJUTSU_SESSION_TTL"):
            _session_ttl_from_env(bad, 99)
    for nonpos in ("0", "-5"):
        with pytest.raises(ValueError, match="positive"):
            _session_ttl_from_env(nonpos, 99)


# ---------------------------------------------------------------------------
# SqlSessionStore (BE-0106) — sessions in Postgres (SQLite on the gate)
# ---------------------------------------------------------------------------


def _sql_store(serve_engine: Callable[..., Engine], ttl: int = 3600) -> SqlSessionStore:
    engine = serve_engine()
    Base.metadata.create_all(engine)
    return SqlSessionStore(engine, ttl=ttl)


def test_sql_issue_then_valid(serve_engine: Callable[..., Engine]) -> None:
    store = _sql_store(serve_engine)
    sid = store.issue()
    assert store.valid(sid)


def test_sql_unknown_is_invalid(serve_engine: Callable[..., Engine]) -> None:
    assert not _sql_store(serve_engine).valid("nope")


def test_sql_binds_and_reads_identity(serve_engine: Callable[..., Engine]) -> None:
    store = _sql_store(serve_engine)
    assert store.identity(store.issue("carol")) == "carol"
    assert store.identity(store.issue()) is None
    assert store.identity("nope") is None


def test_sql_ids_are_unique_and_opaque(serve_engine: Callable[..., Engine]) -> None:
    store = _sql_store(serve_engine)
    a, b = store.issue(), store.issue()
    assert a != b
    assert len(a) > 20


def test_sql_expired_session_is_invalid(serve_engine: Callable[..., Engine]) -> None:
    store = _sql_store(serve_engine, ttl=-1)
    sid = store.issue()
    assert not store.valid(sid)
    assert store.identity(sid) is None


# revoke_identities (BE-0375) — retiring an org has to reach the sessions its members already hold.
# Every store implements it, since which one is wired is a deployment choice the operation cannot
# see: a hole in any of them would be a retired tenant still acting through a live cookie.


def test_in_memory_revoke_drops_only_the_named_identities() -> None:
    store = InMemorySessionStore()
    bob, alice = store.issue(identity="bob"), store.issue(identity="alice")
    anonymous = store.issue()  # a shared-token login carries no identity, so it belongs to no org
    assert store.revoke_identities(["bob", "never-signed-in"]) == 1
    assert not store.valid(bob)
    assert store.valid(alice) and store.valid(anonymous)


def test_in_memory_revoke_of_nothing_is_a_no_op() -> None:
    # Retiring an org with no recorded user must not walk the whole session map, nor report a drop.
    store = InMemorySessionStore()
    live = store.issue(identity="bob")
    assert store.revoke_identities([]) == 0
    assert store.valid(live)


def test_redis_revoke_drops_only_the_named_identities() -> None:
    # The identity is the key's value, not an index, so this store has to read every session key.
    store = RedisSessionStore(FakeRedis())
    bob, alice = store.issue(identity="bob"), store.issue(identity="alice")
    anonymous = store.issue()
    assert store.revoke_identities(["bob"]) == 1
    assert not store.valid(bob)
    assert store.valid(alice) and store.valid(anonymous)


def test_sql_revoke_drops_only_the_named_identities(
    serve_engine: Callable[..., Engine],
) -> None:
    store = _sql_store(serve_engine)
    bob, alice = store.issue(identity="bob"), store.issue(identity="alice")
    anonymous = store.issue()
    assert store.revoke_identities(["bob"]) == 1
    assert not store.valid(bob)
    assert store.valid(alice) and store.valid(anonymous)


def test_sql_revoke_removes_the_row_rather_than_expiring_it(
    serve_engine: Callable[..., Engine],
) -> None:
    # Removed, not expired in place: `valid` and `identity` both read the row before checking its
    # expiry, so a revoked session must not be able to come back if a clock moves.
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from bajutsu.serve.server.models import SessionRecord

    store = _sql_store(serve_engine)
    store.issue(identity="bob")
    assert store.revoke_identities(["bob"]) == 1
    with Session(store._engine) as session:
        assert list(session.scalars(select(SessionRecord))) == []


# --- BE-0414 unit 1: a per-session expiry, an org, and a principal kind -----------------------
#
# A machine session is a third caller shape beside the human session and the shared token: it
# carries an identity like a session, it is not a person, and its lifetime is capped by the OIDC
# token it was exchanged for. Every store records the same three things, since which one a
# deployment wires is not something the exchange can see.


def _stores(serve_engine: Callable[..., Engine]) -> list[Any]:
    return [InMemorySessionStore(), RedisSessionStore(FakeRedis()), _sql_store(serve_engine)]


def test_every_store_defaults_a_session_to_a_human_principal(
    serve_engine: Callable[..., Engine],
) -> None:
    for store in _stores(serve_engine):
        principal = store.principal(store.issue("dana"))
        assert principal == Principal(identity="dana", org=None, kind=HUMAN), type(store)
        assert store.principal("nope") is None, type(store)


def test_every_store_records_a_machine_principal_and_its_org(
    serve_engine: Callable[..., Engine],
) -> None:
    expires = datetime.now(UTC) + timedelta(minutes=15)
    for store in _stores(serve_engine):
        sid = store.issue("repo:acme/app", expires_at=expires, org="acme", kind=MACHINE)
        assert store.principal(sid) == Principal(
            identity="repo:acme/app", org="acme", kind=MACHINE
        ), type(store)
        # The gate reads the kind, but a machine session is still a session: it validates, and it
        # is revocable by identity like any other (`repo:` cannot collide with a GitHub login).
        assert store.valid(sid) and store.identity(sid) == "repo:acme/app", type(store)
        assert store.revoke_identities(["repo:acme/app"]) == 1, type(store)
        assert not store.valid(sid), type(store)


def test_every_store_enforces_a_per_session_expiry(
    serve_engine: Callable[..., Engine],
) -> None:
    """The whole reason the exchange beats re-presenting the token: what it mints is shorter-lived.
    `InMemorySessionStore` enforced no expiry at all before BE-0414 — it does now."""
    past = datetime.now(UTC) - timedelta(seconds=1)
    for store in _stores(serve_engine):
        sid = store.issue("repo:acme/app", expires_at=past, org="acme", kind=MACHINE)
        assert not store.valid(sid), type(store)
        assert store.principal(sid) is None, type(store)


def test_a_per_session_expiry_never_extends_the_store_level_one(
    serve_engine: Callable[..., Engine],
) -> None:
    beyond = datetime.now(UTC) + timedelta(days=365)
    sql = _sql_store(serve_engine, ttl=60)
    sid = sql.issue("repo:acme/app", expires_at=beyond, org="acme", kind=MACHINE)
    from sqlalchemy.orm import Session

    from bajutsu.serve.server.models import SessionRecord

    with Session(sql._engine) as session:
        row = session.get(SessionRecord, sid)
        assert row is not None
        capped = row.expires_at.replace(tzinfo=row.expires_at.tzinfo or UTC)
        assert capped < datetime.now(UTC) + timedelta(seconds=120)

    redis = FakeRedis()
    RedisSessionStore(redis, ttl=60).issue(
        "repo:acme/app", expires_at=beyond, org="acme", kind=MACHINE
    )
    assert list(redis.ttls.values()) == [60]


def test_a_human_session_keeps_its_previous_lifetime(
    serve_engine: Callable[..., Engine],
) -> None:
    """Passing no expiry is the unchanged path: the SQL/Redis stores take their own time-to-live,
    and an in-memory session still lives as long as the process does."""
    redis = FakeRedis()
    RedisSessionStore(redis, ttl=321).issue("dana")
    assert list(redis.ttls.values()) == [321]
    memory = InMemorySessionStore()
    assert memory.valid(memory.issue("dana"))


def test_a_session_row_predating_the_columns_reads_as_human(
    serve_engine: Callable[..., Engine],
) -> None:
    """No migration backfills `kind`, so every session live across the upgrade holds NULL — and a
    session issued before machine sessions existed is a human one."""
    store = _sql_store(serve_engine)
    sid = store.issue("dana")
    from sqlalchemy.orm import Session

    from bajutsu.serve.server.models import SessionRecord

    with Session(store._engine) as session:
        row = session.get(SessionRecord, sid)
        assert row is not None
        row.kind = None
        session.commit()
    assert store.principal(sid) == Principal(identity="dana", org=None, kind=HUMAN)


def test_an_unrecognized_stored_kind_reads_as_a_machine(
    serve_engine: Callable[..., Engine],
) -> None:
    """Fail closed: an unrecognized kind is governed by the narrower gate, not handed a human's
    role gate — which for an identity with no user row would default to viewer."""
    assert kind_from_stored("something-else") == MACHINE
    assert kind_from_stored(None) == HUMAN
    assert kind_from_stored(HUMAN) == HUMAN


def test_redis_reads_a_pre_be_0414_bare_identity_value(serve_engine: Callable[..., Engine]) -> None:
    """A Redis store holding keys written before the value became JSON must not sign everyone out."""
    redis = FakeRedis()
    redis.setex("bajutsu:session:legacy", 60, "dana")
    redis.setex("bajutsu:session:legacy-anon", 60, "")
    store = RedisSessionStore(redis)
    assert store.principal("legacy") == Principal(identity="dana", org=None, kind=HUMAN)
    assert store.principal("legacy-anon") == Principal(identity=None, org=None, kind=HUMAN)
    assert store.revoke_identities(["dana"]) == 1


def test_a_corrupted_redis_record_is_refused_as_a_machine_not_read_as_a_human() -> None:
    """A record that looks like one but will not parse must not fall through the legacy
    bare-identity branch: that would make it a *human* session carrying the blob as its identity,
    and a human session on a database-less deployment skips the role gate entirely."""
    redis = FakeRedis()
    redis.setex("bajutsu:session:broken", 60, '{"identity": "repo:acme/app", ')  # truncated
    principal = RedisSessionStore(redis).principal("broken")
    assert principal is not None
    assert principal.kind == MACHINE and principal.identity is None


def test_a_stored_record_with_non_string_fields_narrows_rather_than_coercing(
    serve_engine: Callable[..., Engine],
) -> None:
    """`json.loads` hands back `Any`, so nothing upstream proves these are the types the dataclass
    declares. A coerced value can match something; an absent one cannot."""
    redis = FakeRedis()
    redis.setex(
        "bajutsu:session:odd", 60, json.dumps({"identity": 123, "org": ["a"], "kind": "machine"})
    )
    principal = RedisSessionStore(redis).principal("odd")
    assert principal == Principal(identity=None, org=None, kind=MACHINE)


def test_an_unrecognized_stored_kind_is_reported_not_just_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only a session that *was* human can carry one — the exchange always writes "machine" — so
    this refuses a person wholesale. Without a log line it is an unexplained 403 on every
    endpoint with nothing anywhere naming the cause."""
    with caplog.at_level(logging.WARNING):
        assert kind_from_stored("from-a-newer-version") == MACHINE
    assert any("not one this version knows" in r.message for r in caplog.records)


def test_a_redis_record_missing_its_kind_reads_as_a_machine() -> None:
    """Unlike a NULL SQL column, which predates these fields, a Redis value that is JSON at all
    was written by code that always records a kind — so its absence is corruption, and corruption
    meets the narrower gate."""
    redis = FakeRedis()
    redis.setex("bajutsu:session:odd", 60, json.dumps({"identity": "alice", "org": "acme"}))
    principal = RedisSessionStore(redis).principal("odd")
    assert principal is not None and principal.kind == MACHINE
