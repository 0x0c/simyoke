"""Open a repository from the environment, and convert rows to and from the seam's records."""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .org_record import OrgRecord
from .run_record import RunRecord

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from bajutsu.serve.server.models import Org, Run

    from .sql_repository import SqlRepository


def _to_org(row: Org) -> OrgRecord:
    # The membership columns are nullable — an org row that predates BE-0375, or one `ensure_org`
    # created at sign-in, holds NULL rather than `[]` (the model's `default` is Python-side only, so
    # it never reached those rows). Normalize here, once, so no reader has to.
    return OrgRecord(
        id=row.id,
        slug=row.slug,
        name=row.name,
        members=list(row.members or []),
        github_orgs=list(row.github_orgs or []),
        github_teams=list(row.github_teams or []),
        editor_teams=list(row.editor_teams or []),
        allowed_repositories=list(row.allowed_repositories or []),
        membership_seeded_at=row.membership_seeded_at,
        deleted_at=row.deleted_at,
        created_at=row.created_at,
        config_source=dict(row.config_source) if row.config_source is not None else None,
    )


def _to_record(row: Run) -> RunRecord:
    return RunRecord(
        id=row.id,
        org_id=row.org_id,
        status=row.status,
        created_by=row.created_by,
        ok=row.ok,
        created_at=row.created_at,
        summary=dict(row.summary),
        scenario_hash=row.scenario_hash,
        tool_version=row.tool_version,
        git_revision=row.git_revision,
        device_runtime=row.device_runtime,
        label=row.label,
        target=row.target,
        deleted_at=row.deleted_at,
        deleted_by=row.deleted_by,
    )


def _as_utc(dt: datetime) -> datetime:
    """Read a stored timestamp as UTC-aware. SQLite (the gate) hands back naive datetimes for a
    ``DateTime(timezone=True)`` column, so subtracting a UTC-aware ``now`` would raise; assume UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _age_seconds(now: datetime, then: datetime) -> float:
    return (now - _as_utc(then)).total_seconds()


def engine_from_url(url: str) -> Engine:
    """Build a SQLAlchemy engine for *url* (e.g. ``postgresql://…`` in production, ``sqlite://`` on
    the gate). SQLAlchemy is imported here so the default path never loads it."""
    from sqlalchemy import create_engine

    return create_engine(url)


def _positive_env(name: str, raw: str, *, cast: Any) -> Any:
    """Parse an operator-facing positive-number env var defensively — a clear, variable-named error
    rather than a bare ValueError/TypeError. Non-numeric or non-positive values are rejected."""
    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive number, got {raw!r}") from None
    # NaN/inf slip past `<= 0` (NaN compares False, inf is "positive"), so reject them explicitly —
    # a timedelta(seconds=nan) or an infinite cap is not a well-defined operator setting.
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def repository_from_env() -> SqlRepository | None:
    """A `SqlRepository` from ``BAJUTSU_DATABASE_URL``, or ``None`` when it is unset — so the
    server backend runs without a database until one is configured, and local never has one. The
    schema itself is owned by Alembic (7a-2), not created here."""
    url = os.environ.get("BAJUTSU_DATABASE_URL")
    if not url:
        return None
    kwargs: dict[str, Any] = {}
    if timeout := os.environ.get("BAJUTSU_LEASE_TIMEOUT_SECONDS"):
        kwargs["lease_timeout"] = timedelta(
            seconds=_positive_env("BAJUTSU_LEASE_TIMEOUT_SECONDS", timeout, cast=float)
        )
    if attempts := os.environ.get("BAJUTSU_LEASE_MAX_ATTEMPTS"):
        kwargs["max_attempts"] = _positive_env("BAJUTSU_LEASE_MAX_ATTEMPTS", attempts, cast=int)
    # Imported in the body, not at module load: the repository calls seven helpers from this
    # module, so rule 5 breaks the cycle the split creates on this factory's single edge back in.
    from .sql_repository import SqlRepository

    return SqlRepository(engine_from_url(url), **kwargs)
