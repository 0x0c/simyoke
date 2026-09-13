"""Tests for the serve seam-assembly factory + the uvicorn (ASGI) transport (BE-0015, D2).

`serve` separates two choices: the **transport** (the stdlib server, or the FastAPI app over
uvicorn — `--asgi`) and the **backend** (which seam implementations the ServeState carries —
`--backend`, only `local` until the hosted backings land). `_build_state` is the seam-assembly
factory both share; `make_asgi_server` runs the FastAPI app as a real ASGI server. These tests
exercise the factory and a real uvicorn server (beyond the TestClient coverage in
test_server_app.py), so the actual serving path — middleware, routing — is checked end to end.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
from _shared import log_field, patch_gcs_client, project

from bajutsu import serve as srv
from bajutsu.serve.server.artifacts import ObjectStorageArtifactStore


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _state(tmp_path: Path) -> srv.ServeState:
    _scn, cfg, runs = project(tmp_path)
    return srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
    )


def test_build_state_assembles_local_seams(tmp_path: Path) -> None:
    state = _state(tmp_path)
    assert isinstance(state, srv.ServeState)
    assert isinstance(state.executor, srv.LocalExecutor)
    assert isinstance(state.logbus, srv.InMemoryLogBus)
    assert isinstance(state.artifacts, srv.LocalArtifactStore)
    assert isinstance(state.scenarios, srv.LocalScenarioStore)


def test_build_state_rejects_unknown_backend(tmp_path: Path) -> None:
    _scn, cfg, runs = project(tmp_path)
    # An unknown backend fails loudly rather than silently running local.
    with pytest.raises(ValueError, match="backend"):
        srv._build_state(
            runs_dir=runs,
            config=cfg,
            scenarios_dir=None,
            root=tmp_path,
            baselines_dir=None,
            max_concurrent=4,
            token=None,
            backend="bogus",
        )


def test_build_state_server_wires_the_hosted_seams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet

    from bajutsu.serve.server.artifacts import ObjectStorageArtifactStore
    from bajutsu.serve.server.baselines import ObjectBaselineStore
    from bajutsu.serve.server.db_executor import DbQueueExecutor
    from bajutsu.serve.server.post_completion_logbus import PostCompletionLogBus
    from bajutsu.serve.server.scenarios import StorageScenarioStore
    from bajutsu.serve.server.secrets import DbSecretStore

    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("BAJUTSU_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert isinstance(state.executor, DbQueueExecutor)
    assert isinstance(state.logbus, PostCompletionLogBus)
    assert isinstance(state.artifacts, ObjectStorageArtifactStore)
    assert isinstance(state.scenarios, StorageScenarioStore)
    assert isinstance(state.baselines, ObjectBaselineStore)
    assert isinstance(state.secrets, DbSecretStore)  # encrypted per-org store (BE-0136)
    scope = state.scenarios.scope("demo")
    assert scope is not None
    # The scenario lives on `demo`'s configured local dir (`project`'s fixture), never in the fake
    # bucket: list/read resolve it from there without touching object storage (BE-0324).
    assert [s["file"] for s in scope.list()] == ["smoke.yaml"]
    assert scope.read("smoke.yaml") == (_scn / "smoke.yaml").read_text(encoding="utf-8")


def test_build_state_local_has_no_repository(tmp_path: Path) -> None:
    # The system of record is server-only; local never has one, so its behavior is unchanged.
    assert _state(tmp_path).repository is None


def test_build_state_server_has_no_repository_without_a_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The database is optional on the server backend: with BAJUTSU_DATABASE_URL unset the repository
    # stays None, so the existing server backing keeps working until a database is configured.
    monkeypatch.delenv("BAJUTSU_DATABASE_URL", raising=False)
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.repository is None


def test_build_state_server_wires_the_repository_when_a_database_url_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With BAJUTSU_DATABASE_URL set, the server backend assembles a SqlRepository (SQLite here, so
    # it builds on the gate without a live Postgres).
    from cryptography.fernet import Fernet

    from bajutsu.serve.server.db import SqlRepository

    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("BAJUTSU_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("BAJUTSU_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert isinstance(state.repository, SqlRepository)


def test_build_state_server_seeds_the_job_id_counter_past_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A process restart calls `_build_state` again against the same database. Without seeding the
    # id counter from the highest existing row, the fresh JobRegistry starts back at "1" — already
    # taken by the previous process's job — and the first post-restart dispatch fails with a
    # duplicate primary-key error on the `jobs` table.
    from cryptography.fernet import Fernet

    from bajutsu.serve.server.db import engine_from_url
    from bajutsu.serve.server.models import Base

    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    db_url = f"sqlite:///{tmp_path / 'serve.db'}"
    monkeypatch.setenv("BAJUTSU_DATABASE_URL", db_url)
    monkeypatch.setenv("BAJUTSU_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    engine = engine_from_url(db_url)
    Base.metadata.create_all(engine)  # migrations already applied, in the real deployment

    _scn, cfg, runs = project(tmp_path)

    def _build() -> srv.ServeState:
        return srv._build_state(
            runs_dir=runs,
            config=cfg,
            scenarios_dir=None,
            root=tmp_path,
            baselines_dir=None,
            max_concurrent=4,
            token=None,
            backend="server",
        )

    before_restart = _build()
    assert before_restart.repository is not None
    job = before_restart.job_registry.register(srv.Job(cmd=["bajutsu", "run"]))
    before_restart.repository.enqueue_job(job.id, org_id="", spec={})

    after_restart = _build()
    assert after_restart.repository is not None
    new_job = after_restart.job_registry.register(srv.Job(cmd=["bajutsu", "run"]))
    assert new_job.id != job.id
    after_restart.repository.enqueue_job(new_job.id, org_id="", spec={})  # no duplicate-key error


def test_build_state_server_requires_a_secrets_key_with_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A database-backed server persists operator secrets encrypted, so the master key must be
    # provisioned — assembly fails loudly rather than degrading to plaintext-in-memory (BE-0136).
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_DATABASE_URL", "sqlite://")
    monkeypatch.delenv("BAJUTSU_SECRETS_KEY", raising=False)
    _scn, cfg, runs = project(tmp_path)
    with pytest.raises(ValueError, match="BAJUTSU_SECRETS_KEY"):
        srv._build_state(
            runs_dir=runs,
            config=cfg,
            scenarios_dir=None,
            root=tmp_path,
            baselines_dir=None,
            max_concurrent=4,
            token=None,
            backend="server",
        )


def test_build_state_local_uses_in_memory_sessions(tmp_path: Path) -> None:
    # Local sessions stay in-memory (a restart drops them), so its behavior is unchanged.
    from bajutsu.serve.sessions import InMemorySessionStore

    assert isinstance(_state(tmp_path).auth.sessions, InMemorySessionStore)


def test_build_state_server_uses_sql_sessions_when_db_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet

    from bajutsu.serve.server.sessions import SqlSessionStore

    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("BAJUTSU_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("BAJUTSU_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert isinstance(state.auth.sessions, SqlSessionStore)


def test_build_state_server_falls_back_to_in_memory_sessions_without_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bajutsu.serve.sessions import InMemorySessionStore

    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    monkeypatch.delenv("BAJUTSU_DATABASE_URL", raising=False)
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert isinstance(state.auth.sessions, InMemorySessionStore)


def test_build_state_local_has_no_oauth(tmp_path: Path) -> None:
    # OAuth is server-only; local never has it (token auth only), so behavior is unchanged.
    state = _state(tmp_path)
    assert state.auth.oauth is None
    assert state.auth.oauth_admin_teams == ()


def test_build_state_server_wires_oauth_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bajutsu.serve.server.oauth import GitHubOAuthClient

    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("BAJUTSU_OAUTH_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("BAJUTSU_OAUTH_GITHUB_CLIENT_SECRET", "secret")
    monkeypatch.setenv(
        "BAJUTSU_OAUTH_GITHUB_REDIRECT_URI", "https://app.example/api/oauth/callback"
    )
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert isinstance(state.auth.oauth, GitHubOAuthClient)


def test_build_state_server_parses_the_admin_teams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The admin role follows one or more server-wide GitHub Teams, named by the comma-separated
    # BAJUTSU_OAUTH_ADMIN_TEAMS.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("BAJUTSU_OAUTH_ADMIN_TEAMS", "acme-gh/ops, other-gh/root")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.auth.oauth_admin_teams == ("acme-gh/ops", "other-gh/root")


def _setenv_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAJUTSU_OAUTH_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("BAJUTSU_OAUTH_GITHUB_CLIENT_SECRET", "secret")
    monkeypatch.setenv(
        "BAJUTSU_OAUTH_GITHUB_REDIRECT_URI", "https://app.example/api/oauth/callback"
    )


def _delenv_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    # The mirror image of _setenv_oauth: a "no warning" test's premise is that OAuth isn't wired
    # (or that a name isn't set), which the ambient shell can silently falsify for a maintainer who
    # exports these for their own deployment -- pin the premise instead of inheriting it.
    for var in (
        "BAJUTSU_OAUTH_GITHUB_CLIENT_ID",
        "BAJUTSU_OAUTH_GITHUB_CLIENT_SECRET",
        "BAJUTSU_OAUTH_GITHUB_REDIRECT_URI",
        "BAJUTSU_OAUTH_ADMIN_TEAM",
        "BAJUTSU_OAUTH_ADMIN_TEAMS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_build_state_server_warns_on_a_half_configured_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A single missing/mistyped GitHub OAuth var collapses `oauth` to None -- indistinguishable
    # from a deliberate token-auth-only backend to the three checks below, which all gate on
    # `oauth is not None`. Not a lockout (the shared-token login re-enables on this same `None`),
    # but a silent, unintended fallback the operator needs to know about, so it needs its own check,
    # gated the opposite way.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _delenv_oauth(monkeypatch)
    monkeypatch.setenv("BAJUTSU_OAUTH_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("BAJUTSU_OAUTH_GITHUB_CLIENT_SECRET", "secret")
    # BAJUTSU_OAUTH_GITHUB_REDIRECT_URI deliberately left unset.
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.auth.oauth is None
    err = capsys.readouterr().err
    assert "GitHub OAuth is only partly configured" in err
    # No BAJUTSU_SERVE_TOKEN either (token=None above), so there is no shared-token fallback to fall
    # back to -- both transports skip the auth+RBAC gate outright on `token is None`, serving every
    # endpoint unauthenticated. The message must name THIS failure mode, not the reassuring one.
    assert "no token is configured either" in err
    assert "unauthenticated" in err
    # Name the actual unset variable, not just the whole triple -- a typo'd variable NAME (e.g.
    # _REDIRECT_URL for _REDIRECT_URI) would otherwise leave all three look present in a quick .env
    # read, sending the operator to the OAuth app registration instead of their own .env.
    assert "BAJUTSU_OAUTH_GITHUB_REDIRECT_URI" in err
    assert "is unset" in err
    assert len(state.startup_warnings) == 1
    check, msg = state.startup_warnings[0]
    assert check == "oauth_half_configured"
    assert "GitHub OAuth is only partly configured" in msg


def test_build_state_server_warns_on_a_half_configured_oauth_naming_every_unset_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two of the three vars unset -- the grammar branch ("are unset," not "is unset") and the
    # join must name both, not just whichever one a single-var test happens to cover.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _delenv_oauth(monkeypatch)
    monkeypatch.setenv("BAJUTSU_OAUTH_GITHUB_CLIENT_ID", "cid")
    # _CLIENT_SECRET and _REDIRECT_URI both deliberately left unset.
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.auth.oauth is None
    err = capsys.readouterr().err
    assert "BAJUTSU_OAUTH_GITHUB_CLIENT_SECRET, BAJUTSU_OAUTH_GITHUB_REDIRECT_URI are unset" in err


def test_build_state_server_warns_on_a_half_configured_oauth_with_a_token_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The other half of the same check: WITH a shared token configured, `POST /api/login` really
    # does re-enable itself on this same `oauth is None` -- the message must name that fallback
    # instead, not the "unauthenticated" one above.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _delenv_oauth(monkeypatch)
    monkeypatch.setenv("BAJUTSU_OAUTH_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("BAJUTSU_OAUTH_GITHUB_CLIENT_SECRET", "secret")
    # BAJUTSU_OAUTH_GITHUB_REDIRECT_URI deliberately left unset.
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token="shared-secret",
        backend="server",
    )
    assert state.auth.oauth is None
    err = capsys.readouterr().err
    assert "the shared-token login is enabled instead" in err
    assert "unauthenticated" not in err


def test_build_state_server_does_not_warn_when_oauth_is_fully_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A deliberate token-auth-only deployment (no GitHub OAuth vars at all) must not be told its
    # OAuth is "partly configured" -- none of it is configured, on purpose.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _delenv_oauth(monkeypatch)
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.auth.oauth is None
    assert "GitHub OAuth is only partly configured" not in capsys.readouterr().err
    assert state.startup_warnings == ()


def test_build_state_server_warns_on_the_retired_singular_admin_team_var(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A deployment still on the old BAJUTSU_OAUTH_ADMIN_TEAM (no BAJUTSU_OAUTH_ADMIN_TEAMS) would
    # otherwise lose every admin silently on this hard cutover — it must warn loudly instead.
    # OAuth must actually be wired (all three GitHub vars set), since the warning fires only then.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _setenv_oauth(monkeypatch)
    monkeypatch.delenv("BAJUTSU_OAUTH_ADMIN_TEAMS", raising=False)
    monkeypatch.setenv("BAJUTSU_OAUTH_ADMIN_TEAM", "acme-gh/ops")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.auth.oauth_admin_teams == ()
    err = capsys.readouterr().err
    assert "BAJUTSU_OAUTH_ADMIN_TEAMS is empty" in err
    assert "BAJUTSU_OAUTH_ADMIN_TEAM is retired" in err
    # Collected for serve() to re-emit through oplog once logging is live, not just printed --
    # both fired here, so both must be on the state, not just whichever one this test happened to
    # grep stderr for.
    assert len(state.startup_warnings) == 2
    checks = {c for c, _m in state.startup_warnings}
    assert checks == {"admin_teams_empty", "admin_team_retired_name"}
    assert any("BAJUTSU_OAUTH_ADMIN_TEAMS is empty" in m for _c, m in state.startup_warnings)
    assert any("BAJUTSU_OAUTH_ADMIN_TEAM is retired" in m for _c, m in state.startup_warnings)
    # The actual re-emission through oplog once logging is live -- `serve()` itself isn't
    # exercised by this suite, so this is the one thing that actually drives
    # `_emit_startup_warnings` and would catch a rename/drop of "server.startup_warning" from
    # `oplog.EVENTS` (which `log_event` would otherwise only surface as a boot-time ValueError on
    # the first deployment that actually has a warning to re-emit) (BE-0352).
    with caplog.at_level(logging.WARNING):
        srv._emit_startup_warnings(state)
    records = [r for r in caplog.records if getattr(r, "event", None) == "server.startup_warning"]
    assert len(records) == 2
    assert all(r.levelno == logging.WARNING for r in records)
    # Each record carries its own stable `check`, distinct from the free-text message -- the field
    # an operator's alert should key on, per the record above.
    assert {log_field(r, "check") for r in records} == checks


def test_emit_startup_warnings_reemits_each_entry_under_its_own_event_and_check(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A standalone test of the seam itself, independent of any particular startup check's spelling
    # or of the retired-singular-var test whose coverage this used to ride on -- so a rename/drop of
    # "server.startup_warning" from oplog.EVENTS has a direct test to fail here even if every startup
    # check above changes shape or is deleted outright (e.g. the day BAJUTSU_OAUTH_ADMIN_TEAM itself
    # is dropped from the codebase).
    state = srv.ServeState(
        runs_dir=tmp_path / "runs",
        startup_warnings=(
            ("admin_teams_empty", "BAJUTSU_OAUTH_ADMIN_TEAMS is empty"),
            ("oauth_half_configured", "GitHub OAuth is only partly configured"),
        ),
    )
    with caplog.at_level(logging.WARNING):
        srv._emit_startup_warnings(state)
    records = [r for r in caplog.records if getattr(r, "event", None) == "server.startup_warning"]
    assert len(records) == 2
    assert all(r.levelno == logging.WARNING for r in records)
    assert {log_field(r, "check") for r in records} == {
        "admin_teams_empty",
        "oauth_half_configured",
    }
    assert {r.getMessage() for r in records} == {
        "BAJUTSU_OAUTH_ADMIN_TEAMS is empty",
        "GitHub OAuth is only partly configured",
    }


def test_emit_startup_warnings_is_a_no_op_with_nothing_to_warn_about(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Local serve, or a server deployment with nothing misconfigured -- the empty-tuple default
    # nothing above exercises directly.
    state = srv.ServeState(runs_dir=tmp_path / "runs")
    with caplog.at_level(logging.WARNING):
        srv._emit_startup_warnings(state)
    assert not any(getattr(r, "event", None) == "server.startup_warning" for r in caplog.records)


def test_build_state_server_warns_when_admin_teams_was_never_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The likelier miss than the retired name: a deployment that never set either variable (a
    # fresh OAuth setup, or an upgrade that dropped the old name without adding the new one) --
    # falls through both the old guard's own condition and the malformed-entry check, since an
    # empty list has no entries to be malformed.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _setenv_oauth(monkeypatch)
    monkeypatch.delenv("BAJUTSU_OAUTH_ADMIN_TEAM", raising=False)
    monkeypatch.delenv("BAJUTSU_OAUTH_ADMIN_TEAMS", raising=False)
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.auth.oauth_admin_teams == ()
    err = capsys.readouterr().err
    assert "BAJUTSU_OAUTH_ADMIN_TEAMS is empty" in err
    assert "retired" not in err


def test_build_state_server_without_oauth_does_not_warn_about_admin_teams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Token-auth-only server backend: BAJUTSU_OAUTH_ADMIN_TEAMS has no meaning without OAuth wired,
    # so the empty-list warning must stay quiet rather than tell every such deployment it has no
    # admin -- the guard's first operand, which no other test in this file exercises.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _delenv_oauth(monkeypatch)
    _scn, cfg, runs = project(tmp_path)
    srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert "BAJUTSU_OAUTH_ADMIN_TEAMS" not in capsys.readouterr().err


def test_build_state_server_without_oauth_does_not_warn_on_malformed_admin_teams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same gating for the malformed-entry check: without OAuth wired, BAJUTSU_OAUTH_ADMIN_TEAMS
    # decides nothing, so a stale malformed value left in the environment must not warn either.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _delenv_oauth(monkeypatch)
    monkeypatch.setenv("BAJUTSU_OAUTH_ADMIN_TEAMS", "acme-gh/ops other-gh/root")
    _scn, cfg, runs = project(tmp_path)
    srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert "BAJUTSU_OAUTH_ADMIN_TEAMS" not in capsys.readouterr().err


def test_build_state_server_without_oauth_does_not_warn_on_the_retired_admin_team_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The third OAuth-gated check, and the only one with no no-warn test of its own: a token-auth-
    # only backend still carrying the retired singular name must stay quiet too, since the value
    # decides nothing there. Without this, dropping `oauth is not None` from that check is green.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _delenv_oauth(monkeypatch)
    monkeypatch.setenv("BAJUTSU_OAUTH_ADMIN_TEAM", "acme-gh/ops")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert "retired" not in capsys.readouterr().err
    assert state.startup_warnings == ()


def test_build_state_server_warns_on_the_retired_var_even_when_admin_teams_is_also_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The likelier partial-rename mistake: an operator upgrading adds BAJUTSU_OAUTH_ADMIN_TEAMS with
    # the Team they remember but leaves the old singular var (naming a *different* Team) set. The
    # old Team's members silently stop being admin -- with the new list non-empty, the empty-list
    # check below never fires, so this needs its own unconditional check on the retired var alone.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _setenv_oauth(monkeypatch)
    monkeypatch.setenv("BAJUTSU_OAUTH_ADMIN_TEAM", "acme-gh/legacy")
    monkeypatch.setenv("BAJUTSU_OAUTH_ADMIN_TEAMS", "acme-gh/ops")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.auth.oauth_admin_teams == ("acme-gh/ops",)  # the new name wins, the old is ignored
    err = capsys.readouterr().err
    assert "BAJUTSU_OAUTH_ADMIN_TEAM is retired" in err
    assert "BAJUTSU_OAUTH_ADMIN_TEAMS is empty" not in err


def test_build_state_server_warns_on_a_malformed_admin_teams_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A space- or semicolon-separated value parses to one entry that still contains "/" — a bare
    # "/" count would pass it — yet it can never match a real "<github-org>/<team-slug>": that
    # silently loses every admin the same way the retired singular name does, so it must warn too.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _setenv_oauth(monkeypatch)
    monkeypatch.setenv("BAJUTSU_OAUTH_ADMIN_TEAMS", "acme-gh/ops other-gh/root")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.auth.oauth_admin_teams == ("acme-gh/ops other-gh/root",)
    err = capsys.readouterr().err
    assert "will never match" in err
    # Pin the `check` discriminator, not just the message: it is the field docs/self-hosting.md and
    # .env.example both tell an operator to alert on, and nothing else in the suite names it.
    assert any(c == "admin_teams_malformed" for c, _m in state.startup_warnings)
    # This single entry is the WHOLE list -- entirely malformed, the same total lockout
    # `admin_teams_empty` warns about (oauth_callback's `admin_teams_unusable` treats them
    # identically) -- so the message must name that consequence too, not just the syntax note
    # (BE-0352).
    assert "no login will have admin access" in err


def test_build_state_server_warns_on_an_empty_side_or_inner_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Each of these has exactly one "/" (so a bare count would pass it) but can never match a
    # real "<github-org>/<team-slug>": an empty org half, an empty slug half, and a slug half with
    # a stray leading space that `.strip()` on the whole entry doesn't remove.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _setenv_oauth(monkeypatch)
    monkeypatch.setenv("BAJUTSU_OAUTH_ADMIN_TEAMS", "acme-gh/,/ops,acme-gh/ ops")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.auth.oauth_admin_teams == ("acme-gh/", "/ops", "acme-gh/ ops")
    err = capsys.readouterr().err
    assert "will never match" in err
    for entry in ("acme-gh/", "/ops", "acme-gh/ ops"):
        assert repr(entry) in err  # all three shapes named, not just whichever one the regex caught


def test_build_state_server_does_not_warn_on_an_uppercase_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # GitHub always lowercases a Team's slug, so an operator who copies the Team name as shown in
    # the UI (title case) writes an entry whose case differs from the stored slug -- but
    # `in_admin_team` lowercases both sides before comparing, so this entry matches a real Team
    # `acme-gh/ops` perfectly. Warning here would tell the operator to "fix" a working config, and
    # teach them to ignore this warning for the one list where a genuinely broken entry hides.
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _setenv_oauth(monkeypatch)
    monkeypatch.setenv("BAJUTSU_OAUTH_ADMIN_TEAMS", "acme-gh/Ops")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.auth.oauth_admin_teams == ("acme-gh/Ops",)
    assert "will never match" not in capsys.readouterr().err


def test_build_state_server_parses_the_per_user_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("BAJUTSU_MAX_CONCURRENT_PER_USER", "3")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.max_concurrent_per_user == 3


def test_build_state_server_parses_the_per_org_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("BAJUTSU_MAX_CONCURRENT_PER_ORG", "5")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.max_concurrent_per_org == 5


def test_build_state_server_has_no_oauth_without_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BAJUTSU_OAUTH_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setenv("BAJUTSU_REDIS_URL", "redis://localhost:6379")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert state.auth.oauth is None


def test_build_state_server_requires_a_server_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BAJUTSU_SERVER_STORE", raising=False)
    _scn, cfg, runs = project(tmp_path)
    with pytest.raises(ValueError, match="BAJUTSU_SERVER_STORE"):
        srv._build_state(
            runs_dir=runs,
            config=cfg,
            scenarios_dir=None,
            root=tmp_path,
            baselines_dir=None,
            max_concurrent=4,
            token=None,
            backend="server",
        )


def test_build_state_server_without_extras_raises_a_clear_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With an extra missing, the server assembly surfaces one install hint naming the extras — not
    # a raw ImportError for whichever module loaded first. We block a sub-import the server path
    # needs (the object-store client) so ImportError fires before any env check.
    import sys

    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    monkeypatch.setitem(sys.modules, "boto3", None)
    _scn, cfg, runs = project(tmp_path)
    with pytest.raises(srv.MissingServerExtra, match="extra"):
        srv._build_state(
            runs_dir=runs,
            config=cfg,
            scenarios_dir=None,
            root=tmp_path,
            baselines_dir=None,
            max_concurrent=4,
            token=None,
            backend="server",
        )


def test_build_state_server_without_the_gcs_extra_names_it_in_the_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The generic install hint predates GCS support and only ever needed to cover boto3 (bundled in
    # the `server` extra) — it must also name `gcs` now that a gs:// BAJUTSU_SERVER_STORE is a real,
    # separately-installed path (BE-0204), or the printed command doesn't actually fix the problem.
    import sys

    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "gs://bkt")
    monkeypatch.setitem(sys.modules, "google", None)
    _scn, cfg, runs = project(tmp_path)
    with pytest.raises(srv.MissingServerExtra, match="gcs"):
        srv._build_state(
            runs_dir=runs,
            config=cfg,
            scenarios_dir=None,
            root=tmp_path,
            baselines_dir=None,
            max_concurrent=4,
            token=None,
            backend="server",
        )


def test_build_state_server_rejects_a_malformed_server_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A malformed BAJUTSU_SERVER_STORE names the actual setting, not the sibling --evidence-store
    # wording `evidence_target_from_uri` raises internally (BE-0204).
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "bkt")  # no s3:// or gs:// scheme
    _scn, cfg, runs = project(tmp_path)
    with pytest.raises(ValueError, match="BAJUTSU_SERVER_STORE 'bkt' is invalid"):
        srv._build_state(
            runs_dir=runs,
            config=cfg,
            scenarios_dir=None,
            root=tmp_path,
            baselines_dir=None,
            max_concurrent=4,
            token=None,
            backend="server",
        )


def test_build_state_server_reraises_internal_import_bugs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed bajutsu.* import is a real bug, not a missing extra: it must surface unchanged rather
    # than be rewritten into the install hint (which would hide the traceback). Only third-party
    # extras get the hint.
    def boom(**_kwargs: object) -> srv.ServeState:
        raise ImportError("cannot import name 'X'", name="bajutsu.serve.server.db")

    monkeypatch.setattr(srv, "_build_server_state", boom)
    _scn, cfg, runs = project(tmp_path)
    with pytest.raises(ImportError) as caught:
        srv._build_state(
            runs_dir=runs,
            config=cfg,
            scenarios_dir=None,
            root=tmp_path,
            baselines_dir=None,
            max_concurrent=4,
            token=None,
            backend="server",
        )
    assert not isinstance(caught.value, srv.MissingServerExtra)
    assert "extra" not in str(caught.value)


def test_build_state_server_normalizes_a_prefix_without_a_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A prefix without a trailing slash must not fuse into "tenantartifacts/" / "tenantscenarios/".
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt/tenant")  # no trailing slash
    monkeypatch.setenv("BAJUTSU_S3_REGION", "auto")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    # The artifact store keys a run-relative path under "<prefix>artifacts/", with the slash added.
    assert isinstance(state.artifacts, ObjectStorageArtifactStore)
    assert state.artifacts._key("r1/report.html") == "tenant/artifacts/r1/report.html"


def test_build_state_server_wires_a_gcs_object_store_from_a_gs_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A gs:// BAJUTSU_SERVER_STORE builds the server backend on GCSObjectStore instead of S3 (BE-0204)
    # — a fake storage.Client keeps the real object_store_from_uri path off the network.
    from bajutsu.common.run_meta.object_store import GCSObjectStore

    patch_gcs_client(monkeypatch)
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "gs://bucket/tenant")
    _scn, cfg, runs = project(tmp_path)
    state = srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )
    assert isinstance(state.artifacts, ObjectStorageArtifactStore)
    assert isinstance(state.artifacts._store, GCSObjectStore)
    assert isinstance(state.artifacts, ObjectStorageArtifactStore)
    assert state.artifacts._key("r1/report.html") == "tenant/artifacts/r1/report.html"


def test_asgi_server_serves_the_app_over_a_real_socket(tmp_path: Path) -> None:
    state = _state(tmp_path)
    port = _free_port()
    server = srv.make_asgi_server(state, host="127.0.0.1", port=port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        assert server.started, "uvicorn server did not start"
        resp = httpx.get(f"http://127.0.0.1:{port}/api/runs", timeout=5)
        assert resp.status_code == 200 and resp.json() == []
        # The hardening middleware runs on the real server too (not just under TestClient).
        assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "uvicorn server did not shut down"


def test_sse_route_delivers_keepalive_over_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End to end over a real uvicorn server: an idle (running, not closed) job's /events stream
    # delivers the log frame and periodic `:keepalive` comments, so a proxy won't drop the idle
    # connection (B). Breaking out closes the socket — exercising the route's disconnect path.
    from bajutsu.serve.server import app as app_module

    monkeypatch.setattr(app_module, "_SSE_KEEPALIVE", 0.05)
    state = _state(tmp_path)
    state.jobs["w"] = srv.Job(id="w", cmd=[])
    state.logbus.publish(
        "w", "line one\n"
    )  # a line, but the job is NOT closed -> stream stays open
    port = _free_port()
    server = srv.make_asgi_server(state, host="127.0.0.1", port=port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        assert server.started
        seen_line = seen_keepalive = False
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/jobs/w/events", timeout=5) as r:
            for line in r.iter_lines():
                if "data: line one" in line:
                    seen_line = True
                if line.startswith(":keepalive"):
                    seen_keepalive = True
                if seen_line and seen_keepalive:
                    break
        assert seen_line and seen_keepalive
    finally:
        state.logbus.close("w")
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "uvicorn server did not shut down"


def _delenv_oidc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the premise rather than inherit it, the same reason `_delenv_oauth` exists: a maintainer
    who exports these for their own deployment would otherwise silently falsify a "no warning" case."""
    for var in (
        "BAJUTSU_OIDC_AUDIENCE",
        "BAJUTSU_OIDC_PROVIDER",
        "BAJUTSU_OIDC_ISSUER",
        "BAJUTSU_OIDC_MAX_TOKEN_AGE",
        "BAJUTSU_OIDC_SESSION_TTL",
    ):
        monkeypatch.delenv(var, raising=False)


def _server_state(tmp_path: Path) -> srv.ServeState:
    _scn, cfg, runs = project(tmp_path)
    return srv._build_state(
        runs_dir=runs,
        config=cfg,
        scenarios_dir=None,
        root=tmp_path,
        baselines_dir=None,
        max_concurrent=4,
        token=None,
        backend="server",
    )


def test_build_state_warns_when_oidc_is_configured_without_a_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The OIDC exchange spends each token's `jti` in the database to make single-use hold across
    replicas, so with none configured it refuses every exchange. `docs/self-hosting.md` promises
    the operator hears that at boot rather than one failed pipeline run at a time (BE-0414)."""
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    monkeypatch.delenv("BAJUTSU_DATABASE_URL", raising=False)
    _delenv_oauth(monkeypatch)
    _delenv_oidc(monkeypatch)
    monkeypatch.setenv("BAJUTSU_OIDC_AUDIENCE", "https://bajutsu.example.com")

    state = _server_state(tmp_path)

    # Keyed on `check=`, not the message text, for the reason `ServeState.startup_warnings`'
    # own comment gives: an alert keys on the stable discriminator, and the prose can reword.
    assert any(check == "oidc_without_database" for check, _msg in state.startup_warnings)
    assert state.auth.oidc is not None  # configured, just unusable until a database appears


def test_build_state_warns_when_oidc_is_configured_without_joserfc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verification imports `joserfc` lazily, so a deployment missing the `oauth` extra would
    otherwise meet it as a 500 per exchange with no `oidc.denied` behind it (BE-0414)."""
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    _delenv_oauth(monkeypatch)
    _delenv_oidc(monkeypatch)
    monkeypatch.setenv("BAJUTSU_OIDC_AUDIENCE", "https://bajutsu.example.com")
    monkeypatch.setattr(srv, "_joserfc_installed", lambda: False)

    state = _server_state(tmp_path)

    assert any(check == "oidc_without_joserfc" for check, _msg in state.startup_warnings)
    assert any(
        "uv sync --extra oauth" in msg
        for check, msg in state.startup_warnings
        if check == "oidc_without_joserfc"
    ), "the warning must name the fix, not just the symptom"


def test_build_state_is_quiet_about_oidc_when_none_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No expected audience means the caller shape is off, which is the ordinary deployment — it
    must not be warned at about a feature it never asked for."""
    monkeypatch.setenv("BAJUTSU_SERVER_STORE", "s3://bkt")
    _delenv_oauth(monkeypatch)
    _delenv_oidc(monkeypatch)

    state = _server_state(tmp_path)

    assert state.auth.oidc is None
    assert not [c for c, _m in state.startup_warnings if c.startswith("oidc_")]
