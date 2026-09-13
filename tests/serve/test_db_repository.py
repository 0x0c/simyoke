"""The `Repository` seam (BE-0015 7a): the run round-trip, org-scoped listing, the label/target
partitions (BE-0404), and the env-driven factory. Most tests build their schema through the
`serve_engine` fixture, which runs them against in-memory SQLite in the fast gate and, behind the
`postgres` marker, against a real Postgres service in the serve-db.yml lane (BE-0309).
orgs/users/audit_log are tested elsewhere in this file (7b/7c)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _shared import StubArtifactStore
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from bajutsu import serve as srv
from bajutsu.serve.server.db import (
    RunRecord,
    SqlRepository,
    engine_from_url,
    repository_from_env,
)
from bajutsu.serve.server.db_executor import DbQueueExecutor
from bajutsu.serve.server.models import AuditLog, Base, Org, User
from bajutsu.serve.server.post_completion_logbus import PostCompletionLogBus


def _repo(serve_engine: Callable[..., Engine]) -> SqlRepository:
    engine = serve_engine()
    Base.metadata.create_all(engine)
    return SqlRepository(engine)


def _repo_fk(serve_engine: Callable[..., Engine]) -> SqlRepository:
    """Like `_repo(serve_engine)` but with FK enforcement on.

    Needed for tests that verify ON DELETE behaviour (e.g. SET NULL on project deletion). SQLite
    only enacts it under ``PRAGMA foreign_keys=ON``, which `serve_engine(foreign_keys=True)` sets;
    Postgres enforces it natively. Callers must satisfy *all* FKs, so an org row must be created
    via ``ensure_org`` before inserting projects or runs.
    """
    engine = serve_engine(foreign_keys=True)
    Base.metadata.create_all(engine)
    return SqlRepository(engine)


def _engine_repo(serve_engine: Callable[..., Engine]) -> tuple[Engine, SqlRepository]:
    engine = serve_engine()
    Base.metadata.create_all(engine)
    return engine, SqlRepository(engine)


def _seed_orgs(repo: SqlRepository, *org_ids: str) -> None:
    """Create the org rows a test's runs reference.

    `_repo` leaves SQLite's FKs off, so a run can name an org that was never inserted;
    Postgres enforces the org_id FK, so the parent org must exist first. Seeding it keeps each
    calling test dialect-agnostic.
    """
    for org_id in org_ids:
        repo.ensure_org(org_id, slug=org_id, name=org_id)


def test_ensure_org_is_idempotent(serve_engine: Callable[..., Engine]) -> None:
    engine, repo = _engine_repo(serve_engine)
    repo.ensure_org("default", slug="default", name="Default")
    repo.ensure_org("default", slug="default", name="Default")  # again — no duplicate, no error
    with Session(engine) as s:
        orgs = list(s.scalars(select(Org)))
    assert len(orgs) == 1
    assert orgs[0].slug == "default"


def test_upsert_user_inserts_then_updates_in_place(serve_engine: Callable[..., Engine]) -> None:
    engine, repo = _engine_repo(serve_engine)
    repo.ensure_org("default", slug="default", name="Default")
    email = "alice@users.noreply.github.com"
    repo.upsert_user("alice", org_id="default", github_login="alice", email=email)
    repo.upsert_user("alice", org_id="default", github_login="alice", email=email)
    with Session(engine) as s:
        users = list(s.scalars(select(User)))
    assert len(users) == 1
    assert users[0].github_login == "alice"
    assert users[0].org_id == "default"


def test_upsert_user_defaults_to_editor(serve_engine: Callable[..., Engine]) -> None:
    # The default role matches the policy default (an allowlisted user can run), so model /
    # migration / upsert agree and no caller accidentally persists an over-restrictive viewer.
    _engine, repo = _engine_repo(serve_engine)
    repo.ensure_org("default", slug="default", name="Default")
    repo.upsert_user("a", org_id="default", github_login="a", email="a@x")
    assert repo.user_role("a") == "editor"


def test_upsert_user_stores_and_updates_the_role(serve_engine: Callable[..., Engine]) -> None:
    _engine, repo = _engine_repo(serve_engine)
    repo.ensure_org("default", slug="default", name="Default")
    repo.upsert_user("a", org_id="default", github_login="a", email="a@x", role="admin")
    assert repo.user_role("a") == "admin"
    repo.upsert_user("a", org_id="default", github_login="a", email="a@x", role="viewer")
    assert repo.user_role("a") == "viewer"  # a re-login recomputes the role
    assert repo.user_role("nobody") is None


def test_user_org_returns_the_users_org(serve_engine: Callable[..., Engine]) -> None:
    _engine, repo = _engine_repo(serve_engine)
    repo.ensure_org("acme", slug="acme", name="Acme")
    repo.upsert_user("a", org_id="acme", github_login="a", email="a@x")
    assert repo.user_org("a") == "acme"
    assert repo.user_org("nobody") is None


def test_record_audit_appends_a_row_with_actor_and_detail(
    serve_engine: Callable[..., Engine],
) -> None:
    engine, repo = _engine_repo(serve_engine)
    repo.ensure_org("default", slug="default", name="Default")
    repo.upsert_user(
        "alice", org_id="default", github_login="alice", email="a@users.noreply.github.com"
    )
    repo.record_audit(
        org_id="default",
        actor_id="alice",
        action="run",
        target="demo/smoke.yaml",
        detail={"workers": 2},
    )
    with Session(engine) as s:
        rows = list(s.scalars(select(AuditLog)))
    assert len(rows) == 1
    assert rows[0].action == "run"
    assert rows[0].target == "demo/smoke.yaml"
    assert rows[0].actor_id == "alice"
    assert rows[0].detail == {"workers": 2}


def test_record_then_get_round_trips(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    _seed_orgs(repo, "o1")
    repo.record_run(
        RunRecord(id="r1", org_id="o1", status="done", ok=True, summary={"passed": 3, "failed": 0})
    )
    got = repo.get_run("r1")
    assert got is not None
    assert got.id == "r1"
    assert got.org_id == "o1"
    assert got.status == "done"
    assert got.ok is True
    assert got.summary == {"passed": 3, "failed": 0}


def test_get_missing_returns_none(serve_engine: Callable[..., Engine]) -> None:
    assert _repo(serve_engine).get_run("nope") is None


def test_list_runs_filters_by_org_and_orders_newest_first(
    serve_engine: Callable[..., Engine],
) -> None:
    repo = _repo(serve_engine)
    _seed_orgs(repo, "o1", "o2")
    base = datetime(2026, 1, 1, tzinfo=UTC)
    repo.record_run(RunRecord(id="a", org_id="o1", status="done", created_at=base.replace(hour=1)))
    repo.record_run(RunRecord(id="b", org_id="o1", status="done", created_at=base.replace(hour=3)))
    repo.record_run(RunRecord(id="c", org_id="o2", status="done", created_at=base.replace(hour=2)))
    assert [r.id for r in repo.list_runs(org_id="o1")] == ["b", "a"]


def test_list_runs_respects_limit(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    _seed_orgs(repo, "o1")
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(5):
        repo.record_run(
            RunRecord(id=f"r{i}", org_id="o1", status="done", created_at=base.replace(minute=i))
        )
    assert len(repo.list_runs(org_id="o1", limit=2)) == 2


def test_summary_json_roundtrips_a_nested_value(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    _seed_orgs(repo, "o1")
    summary = {"counts": {"passed": 2, "failed": 1}, "steps": [{"name": "tap", "ok": True}]}
    repo.record_run(RunRecord(id="r1", org_id="o1", status="done", summary=summary))
    got = repo.get_run("r1")
    assert got is not None
    assert got.summary == summary


def test_record_run_is_idempotent_by_id(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    _seed_orgs(repo, "o1")
    repo.record_run(RunRecord(id="r1", org_id="o1", status="running"))
    repo.record_run(RunRecord(id="r1", org_id="o1", status="done", ok=True))
    got = repo.get_run("r1")
    assert got is not None
    assert got.status == "done"
    assert got.ok is True
    assert len(repo.list_runs(org_id="o1")) == 1


def test_engine_from_url_builds_a_usable_engine() -> None:
    engine = engine_from_url("sqlite://")
    Base.metadata.create_all(engine)
    assert SqlRepository(engine).get_run("absent") is None


def test_repository_from_env_is_none_without_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAJUTSU_DATABASE_URL", raising=False)
    assert repository_from_env() is None


def test_repository_from_env_builds_a_sql_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAJUTSU_DATABASE_URL", "sqlite://")
    assert isinstance(repository_from_env(), SqlRepository)


def test_repository_from_env_rejects_a_non_numeric_lease_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BAJUTSU_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("BAJUTSU_LEASE_TIMEOUT_SECONDS", "soon")
    with pytest.raises(ValueError, match="BAJUTSU_LEASE_TIMEOUT_SECONDS"):
        repository_from_env()


def test_repository_from_env_rejects_a_non_positive_attempt_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BAJUTSU_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("BAJUTSU_LEASE_MAX_ATTEMPTS", "0")
    with pytest.raises(ValueError, match="BAJUTSU_LEASE_MAX_ATTEMPTS"):
        repository_from_env()


def test_repository_from_env_rejects_a_non_finite_lease_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BAJUTSU_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("BAJUTSU_LEASE_TIMEOUT_SECONDS", "inf")  # slips past a bare `<= 0` check
    with pytest.raises(ValueError, match="finite"):
        repository_from_env()


# ---------------------------------------------------------------------------
# Job queue methods (BE-0106)
# ---------------------------------------------------------------------------


def test_enqueue_then_lease_returns_the_spec(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    spec = {"cmd": ["bajutsu", "run"], "job_id": "j1"}
    repo.enqueue_job("j1", org_id="o1", spec=spec)
    leased = repo.lease_job("worker-1")
    assert leased is not None
    assert leased.id == "j1"
    assert leased.spec == spec


def test_lease_returns_none_when_queue_is_empty(serve_engine: Callable[..., Engine]) -> None:
    assert _repo(serve_engine).lease_job("worker-1") is None


def test_max_job_id_is_zero_when_the_table_is_empty(serve_engine: Callable[..., Engine]) -> None:
    assert _repo(serve_engine).max_job_id() == 0


def test_max_job_id_returns_the_highest_numeric_id(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    repo.enqueue_job("2", org_id="o1", spec={})
    repo.enqueue_job("10", org_id="o1", spec={})
    repo.enqueue_job("3", org_id="o1", spec={})
    assert repo.max_job_id() == 10


def test_max_job_id_ignores_non_numeric_ids(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    repo.enqueue_job("abc", org_id="o1", spec={})
    assert repo.max_job_id() == 0


def test_max_job_id_returns_zero_when_the_jobs_table_does_not_exist_yet(
    serve_engine: Callable[..., Engine],
) -> None:
    # ServeState can be constructed before this database's migration that creates `jobs` has run —
    # seeding must not crash startup, just fall back to the pre-seeding behaviour (start at 0).
    engine = serve_engine()  # deliberately skip Base.metadata.create_all
    assert SqlRepository(engine).max_job_id() == 0


def test_lease_takes_oldest_first(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    repo.enqueue_job("j1", org_id="o1", spec={"n": 1})
    repo.enqueue_job("j2", org_id="o1", spec={"n": 2})
    first = repo.lease_job("w1")
    assert first is not None and first.id == "j1"
    second = repo.lease_job("w2")
    assert second is not None and second.id == "j2"
    assert repo.lease_job("w3") is None


def test_complete_job_stores_result(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    repo.enqueue_job("j1", org_id="o1", spec={"cmd": []})
    repo.lease_job("w1")
    result = {"ok": True, "run_id": "r1", "summary": {"passed": 3}}
    repo.complete_job("j1", result=result)
    got = repo.get_job("j1")
    assert got is not None
    assert got["status"] == "done"
    assert got["result"] == result


def test_fail_job_stores_error(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    repo.enqueue_job("j1", org_id="o1", spec={"cmd": []})
    repo.lease_job("w1")
    repo.fail_job("j1", error="crash")
    got = repo.get_job("j1")
    assert got is not None
    assert got["status"] == "failed"
    assert got["result"]["error"] == "crash"


def test_get_job_returns_none_for_missing(serve_engine: Callable[..., Engine]) -> None:
    assert _repo(serve_engine).get_job("nope") is None


# ---------------------------------------------------------------------------
# DbQueueExecutor (BE-0106)
# ---------------------------------------------------------------------------


def test_db_executor_inserts_a_queued_job(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    state = srv.ServeState(runs_dir=Path("/tmp/runs"))
    job = state.register(srv.Job(cmd=["bajutsu", "run"], udids=["U1"]))
    DbQueueExecutor(repo).dispatch(state, job)
    info = repo.get_job(job.id)
    assert info is not None
    assert info["status"] == "queued"
    leased = repo.lease_job("w1")
    assert leased is not None and leased.id == job.id


# ---------------------------------------------------------------------------
# PostCompletionLogBus (BE-0106)
# ---------------------------------------------------------------------------


class _FakeArtifactStore(StubArtifactStore):
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self._files = files or {}

    def open_bytes(self, path: str) -> bytes | None:
        return self._files.get(path)


def test_post_completion_logbus_yields_log_after_done(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    repo.enqueue_job("j1", org_id="o1", spec={"cmd": []})
    repo.lease_job("w1")
    repo.complete_job("j1", result={"ok": True, "runId": "20260702-1"})
    artifacts = _FakeArtifactStore({"20260702-1/console.log": b"line 1\nline 2\n"})
    bus = PostCompletionLogBus(repo, lambda _org: artifacts, poll_interval=0.01)
    lines = list(bus.stream("j1"))
    assert "line 1\n" in lines
    assert "line 2\n" in lines


def test_post_completion_logbus_heartbeats_while_queued(
    serve_engine: Callable[..., Engine],
) -> None:
    repo = _repo(serve_engine)
    repo.enqueue_job("j1", org_id="o1", spec={"cmd": []})
    bus = PostCompletionLogBus(repo, poll_interval=0.01)
    it = bus.stream("j1", timeout=1.0)
    hb = next(it)
    assert hb is None  # heartbeat while still queued (timeout set → heartbeats emitted)


def test_post_completion_logbus_final_returns_result(serve_engine: Callable[..., Engine]) -> None:
    repo = _repo(serve_engine)
    repo.enqueue_job("j1", org_id="o1", spec={"cmd": []})
    bus = PostCompletionLogBus(repo, poll_interval=0.01)
    assert bus.final("j1") is None

    repo.lease_job("w1")
    repo.complete_job("j1", result={"ok": True, "runId": "r1"})
    final = bus.final("j1")
    assert final is not None
    import json

    assert json.loads(final)["ok"] is True


# --- BE-0414 unit 1: the OIDC replay cache, in the shared system of record --------------------


def _jti_repo(serve_engine: Callable[..., Engine]) -> tuple[Any, Engine]:
    from bajutsu.serve.server.db import SqlRepository
    from bajutsu.serve.server.models import Base

    engine = serve_engine()
    Base.metadata.create_all(engine)
    return SqlRepository(engine), engine


def test_a_jti_is_spent_once(serve_engine: Callable[..., Engine]) -> None:
    repository, _engine = _jti_repo(serve_engine)
    expires = datetime.now(UTC) + timedelta(minutes=5)
    assert repository.spend_oidc_jti("jti-1", expires_at=expires) is True
    assert repository.spend_oidc_jti("jti-1", expires_at=expires) is False
    assert repository.spend_oidc_jti("jti-2", expires_at=expires) is True


def test_single_use_holds_across_replicas(serve_engine: Callable[..., Engine]) -> None:
    """The reason this is a table and not a per-process cache: a hosted control plane is several
    replicas over one database, so a captured token replayed at the second must still be refused."""
    from bajutsu.serve.server.db import SqlRepository

    first, engine = _jti_repo(serve_engine)
    second = SqlRepository(engine)  # a separate replica against the same database
    expires = datetime.now(UTC) + timedelta(minutes=5)
    assert first.spend_oidc_jti("jti-1", expires_at=expires) is True
    assert second.spend_oidc_jti("jti-1", expires_at=expires) is False


def test_spending_sweeps_rows_no_token_can_still_use(
    serve_engine: Callable[..., Engine],
) -> None:
    """The table stays bounded with no schedule of its own — but a row is kept for as long as the
    token it names could still be presented, which is `exp` plus the clock skew the lifetime
    checks allow. Sweeping at `now` instead would drop a row while its token was still acceptable,
    leaving single-use resting on the exchange's separate born-dead guard rather than on this
    table."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from bajutsu.serve.server.models import OidcJti

    repository, engine = _jti_repo(serve_engine)
    now = datetime.now(UTC)
    repository.spend_oidc_jti("long-gone", expires_at=now - timedelta(minutes=5))
    repository.spend_oidc_jti("just-expired", expires_at=now - timedelta(seconds=1))
    repository.spend_oidc_jti("live", expires_at=now + timedelta(minutes=5))
    with Session(engine) as session:
        kept = sorted(r.id for r in session.scalars(select(OidcJti)))
    assert kept == ["just-expired", "live"]


def test_a_just_expired_token_still_cannot_be_replayed(
    serve_engine: Callable[..., Engine],
) -> None:
    """The property the sweep window exists for: a token inside the clock-skew allowance is still
    refused a second time by this table alone, with no help from any caller-side guard."""
    repository, _engine = _jti_repo(serve_engine)
    just_expired = datetime.now(UTC) - timedelta(seconds=1)
    assert repository.spend_oidc_jti("j", expires_at=just_expired) is True
    assert repository.spend_oidc_jti("j", expires_at=just_expired) is False


def test_seeding_without_a_machine_roster_leaves_the_column_alone(
    serve_engine: Callable[..., Engine],
) -> None:
    """Both membership writers now read `allowed_repositories=None` the same way — "leave it" —
    so the seam cannot mean opposite things depending on which one a caller reached."""
    repository, _engine = _jti_repo(serve_engine)
    repository.ensure_org("acme", slug="acme", name="Acme")
    assert repository.set_org_membership(
        "acme",
        members=["alice"],
        github_orgs=[],
        github_teams=[],
        editor_teams=[],
        allowed_repositories=[{"repository": "acme/app"}],
    )
    # A later seed that names no roster must not wipe the one an admin set. The row is already
    # marked seeded, so this is a no-op overall — the unseeded path is asserted below.
    assert (
        repository.seed_org_membership(
            "acme",
            slug="acme",
            name="Acme",
            members=["bob"],
            github_orgs=[],
            github_teams=[],
            editor_teams=[],
        )
        is False
    )
    org = repository.get_org("acme")
    assert org is not None
    assert org.allowed_repositories == [{"repository": "acme/app"}]

    # And on a genuinely unseeded row, omitting it leaves the column null rather than writing one.
    repository.ensure_org("globex", slug="globex", name="Globex")
    assert repository.seed_org_membership(
        "globex",
        slug="globex",
        name="Globex",
        members=["carol"],
        github_orgs=[],
        github_teams=[],
        editor_teams=[],
    )
    seeded = repository.get_org("globex")
    assert seeded is not None
    assert seeded.members == ["carol"] and seeded.allowed_repositories == []
