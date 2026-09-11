"""The initial Alembic migration must build the same schema as the ORM metadata (BE-0015 7a-2) —
a guard against the migration drifting from models.py. It compares a per-table schema signature
(columns + types + nullability, foreign keys, unique constraints), not just the set of table names,
so a column or constraint that drifts is caught too.

The upgrade/downgrade tests are parametrized over both dialects (BE-0309): the fast gate runs them
against a throwaway SQLite file, and the serve-db.yml lane reruns the same assertions against a real
Postgres service — the only place the dialect-specific migration code (0010's `postgresql` FK branch
and the JSONB column variants) actually executes. Running against Postgres requires both
`BAJUTSU_TEST_POSTGRES_URL` and `BAJUTSU_REQUIRE_POSTGRES` to be set (the URL alone skips, so a
developer who accidentally exports the URL against a non-throwaway database can't silently destroy
data via the `DROP SCHEMA public CASCADE` reset)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.config import Config

# Shared with the serve_engine fixture in tests/conftest.py: constants, dialect params, and the
# double opt-in URL resolver have a single source of truth there so both suites stay in sync.
from conftest import _DIALECTS, _require_postgres_url
from sqlalchemy import (
    Column,
    ForeignKey,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import Engine

import bajutsu.serve.server as server_pkg
from bajutsu.serve.server.models import Base


def _alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(server_pkg.__file__).parent / "migrations"))
    return cfg


def _schema_signature(engine: Engine) -> dict[str, Any]:
    """Per-table (columns+types+nullability, foreign keys, unique constraints) for every table but
    Alembic's own bookkeeping one — enough to catch column/constraint drift, not just a missing
    table."""
    insp = inspect(engine)
    signature: dict[str, Any] = {}
    for table in insp.get_table_names():
        if table == "alembic_version":
            continue
        columns = {(c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns(table)}
        foreign_keys = {
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in insp.get_foreign_keys(table)
        }
        uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints(table)}
        signature[table] = (columns, foreign_keys, uniques)
    return signature


def _load_migration(name: str) -> ModuleType:
    """Load a migration module by filename stem (e.g. '0010_run_project_fk_set_null')."""
    path = Path(server_pkg.__file__).parent / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _reset_schema(url: str) -> None:
    """Drop every table so an upgrade or `create_all` starts from an empty database. The SQLite
    branch uses a throwaway file per test (fresh), but the shared Postgres service persists across
    tests, so each test must clear it explicitly to avoid leftover tables from a prior parameter."""
    engine = create_engine(url)
    try:
        if engine.dialect.name == "postgresql":
            with engine.begin() as conn:
                conn.execute(text("DROP SCHEMA public CASCADE"))
                conn.execute(text("CREATE SCHEMA public"))
        else:
            meta = MetaData()
            meta.reflect(bind=engine)
            meta.drop_all(engine)
    finally:
        engine.dispose()


@pytest.fixture
def migration_db_url(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """A clean, empty database URL for the requested dialect, wired into `BAJUTSU_DATABASE_URL` so
    the Alembic config resolves it. SQLite uses a throwaway file; Postgres uses the service from
    `BAJUTSU_TEST_POSTGRES_URL`. Both env vars must be set together: the URL without the flag skips
    rather than running the destructive schema reset, so a developer who accidentally exports the URL
    against a non-throwaway database can't silently destroy data. The dedicated lane sets both.
    A missing URL with the flag set fails loudly so the lane can't report a false green."""
    if request.param == "postgresql":
        url = _require_postgres_url()
    else:
        url = f"sqlite:///{tmp_path / 'm.db'}"
    _reset_schema(url)
    monkeypatch.setenv("BAJUTSU_DATABASE_URL", url)
    return url


def test_project_id_fk_name_reflects_the_correct_constraint() -> None:
    # Exercises the reflection helper from migration 0010 against a SQLite schema where the FK
    # is created with an explicit name — simulating the Postgres auto-name the migration assumes.
    # Guards that the constrained_columns filter and name extraction work correctly.
    meta = MetaData()
    Table("projects", meta, Column("id", String, primary_key=True))
    Table(
        "runs",
        meta,
        Column("id", String, primary_key=True),
        Column("project_id", String, ForeignKey("projects.id", name="runs_project_id_fkey")),
    )
    engine = create_engine("sqlite://")
    meta.create_all(engine)

    mod = _load_migration("0010_run_project_fk_set_null")
    with engine.connect() as conn:
        name = mod._project_id_fk_name(conn)
    assert name == "runs_project_id_fkey"


@pytest.mark.parametrize("migration_db_url", _DIALECTS, indirect=True)
def test_initial_migration_matches_the_orm_schema(migration_db_url: str) -> None:
    from alembic import command

    command.upgrade(_alembic_config(), "head")
    migrated_engine = create_engine(migration_db_url)
    try:
        migrated = _schema_signature(migrated_engine)
    finally:
        # Dispose before `_reset_schema` drops the Postgres `public` schema, so this test invocation
        # fully owns its connections and no pooled connection lingers during the DDL.
        migrated_engine.dispose()

    _reset_schema(migration_db_url)
    fresh = create_engine(migration_db_url)
    try:
        Base.metadata.create_all(fresh)
        assert migrated == _schema_signature(fresh)
    finally:
        fresh.dispose()


@pytest.mark.parametrize("migration_db_url", _DIALECTS, indirect=True)
def test_0017_carries_a_single_editor_team_into_the_list(migration_db_url: str) -> None:
    # The one migration in this series that moves data, not just columns (BE-0375 unit 9): an
    # `editor_team` an admin set through the API lives nowhere but this row, since the write stamps
    # `membership_seeded_at` and no later startup reseeds it from the `orgs:` block. Dropping the
    # column without the copy would demote every editor of such an org to viewer, silently.
    from alembic import command

    cfg = _alembic_config()
    command.upgrade(cfg, "0016")
    engine = create_engine(migration_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO orgs (id, slug, name, editor_team) "
                    "VALUES ('acme', 'acme', 'Acme', 'acme-gh/scenario-maintainers')"
                )
            )
            # A row that never had one must not gain an entry the copy invented.
            conn.execute(
                text("INSERT INTO orgs (id, slug, name) VALUES ('globex', 'globex', 'Globex')")
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0017")
    engine = create_engine(migration_db_url)
    try:
        with engine.connect() as conn:
            rows: dict[str, str] = dict(
                conn.execute(text("SELECT id, editor_teams FROM orgs")).all()  # type: ignore[arg-type]
            )
        # SQLite hands back the stored JSON text; Postgres decodes JSONB into Python. Compare
        # through `json.loads` on the string form so one assertion covers both dialects.
        acme = rows["acme"]
        assert (json.loads(acme) if isinstance(acme, str) else acme) == [
            "acme-gh/scenario-maintainers"
        ]
        assert rows["globex"] is None
    finally:
        engine.dispose()

    # The way back carries the first entry, so downgrading an org that never gained a second Team is
    # lossless — the only direction an operator can reverse this migration in without losing a role.
    command.downgrade(cfg, "0016")
    engine = create_engine(migration_db_url)
    try:
        with engine.connect() as conn:
            back: dict[str, str] = dict(
                conn.execute(text("SELECT id, editor_team FROM orgs")).all()
            )
        assert back == {"acme": "acme-gh/scenario-maintainers", "globex": None}
    finally:
        engine.dispose()


@pytest.mark.parametrize("migration_db_url", _DIALECTS, indirect=True)
def test_downgrade_base_removes_the_tables(migration_db_url: str) -> None:
    from alembic import command

    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(migration_db_url)
    try:
        remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()
    assert remaining == set()


@pytest.mark.parametrize("migration_db_url", _DIALECTS, indirect=True)
def test_0020_carries_live_sessions_and_orgs_through_the_table_rebuild(
    migration_db_url: str,
) -> None:
    """0020's downgrade drops columns, which SQLite can only do by rebuilding the table. Every
    other migration test starts from an empty database, so a rebuild that silently dropped live
    rows — signing every user out, or losing an org's membership — would go unnoticed."""
    from alembic import command

    cfg = _alembic_config()
    command.upgrade(cfg, "0019")
    engine = create_engine(migration_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO orgs (id, slug, name, members) "
                    "VALUES ('acme', 'acme', 'Acme', '[]')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO sessions (id, identity, expires_at) "
                    "VALUES ('s1', 'alice', '2099-01-01 00:00:00')"
                )
            )
        command.upgrade(cfg, "0020")
        with engine.begin() as conn:
            # Additive and unbackfilled: the live session stays live and reads as human, and the
            # org admits no pipeline until an operator lists one.
            assert [tuple(r) for r in conn.execute(text("SELECT org, kind FROM sessions"))] == [
                (None, None)
            ]
            assert [
                tuple(r) for r in conn.execute(text("SELECT allowed_repositories FROM orgs"))
            ] == [(None,)]
        command.downgrade(cfg, "0019")
        with engine.begin() as conn:
            assert [tuple(r) for r in conn.execute(text("SELECT id, identity FROM sessions"))] == [
                ("s1", "alice")
            ]
            assert [tuple(r) for r in conn.execute(text("SELECT id FROM orgs"))] == [("acme",)]
    finally:
        engine.dispose()
