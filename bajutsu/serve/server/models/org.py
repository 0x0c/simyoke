"""The organizations table: the tenant every other row is scoped to."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from ._functions import _created_at
from ._shared import _JSON
from .base import Base


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[str] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    created_at: Mapped[datetime] = _created_at()
    # The org's membership (BE-0375): who may sign in as this org and who among them may write —
    # `OrgConfig`'s own `members` / `github_orgs` / `github_teams` / `editor_teams`, relocated here
    # from the `orgs:` block so an admin can edit them without a redeploy. Null on a row that
    # predates the move (or one `ensure_org` created at sign-in), which `orgs_from_db` reads as
    # empty; `targets` stays in config and gets no column.
    members: Mapped[list[str] | None] = mapped_column(_JSON, default=None)
    github_orgs: Mapped[list[str] | None] = mapped_column(_JSON, default=None)
    github_teams: Mapped[list[str] | None] = mapped_column(_JSON, default=None)
    editor_teams: Mapped[list[str] | None] = mapped_column(_JSON, default=None)
    # The machine roster (BE-0414 unit 2): the repositories whose CI jobs may exchange an OIDC
    # token for a machine session acting as this org. A column rather than config-only, for the
    # reason the membership columns above are: once a database is wired `orgs_from_db` is the org
    # model, and the exchange requires a database — so a config-only field would be permanently
    # empty on exactly the deployments that can use it. Every writer dumps the validated model, so
    # a stored entry is always the object form — the bare `"<owner>/<repo>"` string is a config
    # spelling, never a stored one — and it is parsed back on read rather than trusted
    # (`orgs._stored_repositories`).
    allowed_repositories: Mapped[list[Any] | None] = mapped_column(_JSON, default=None)
    # When this row's membership was seeded from a bound config's `orgs:` entry — the per-row
    # cutover marker (BE-0375). Null means "not yet seeded"; set means the database owns this org's
    # membership from then on, so a later `orgs:` edit can never overwrite what an admin set. A
    # timestamp rather than a flag for the same reason `deleted_at` is one: it records when.
    membership_seeded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # Soft-delete (BE-0375), the same shape `runs` uses: a deleted org drops out of sign-in
    # resolution and the admin list, but its row stays so the users / runs / secrets /
    # provider_settings / audit_log foreign keys that still point at it stay intact.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # The config source this org last bound (BE-0404 unit 1) — the durable memory a hosted replica
    # reads to recover a bundle it did not itself receive, and the one a session with no binding of
    # its own inherits on its first request (BE-0393 unit 6). An `upload` record carries either a
    # single `sha256` or BE-0268's `artifacts` triple, exactly as `restore_uploaded_config` reads it;
    # a `git` or `file` record carries the locator `config_spec_from_record` turns back into a
    # `--config` value. Every bind writes here. It began upload-only, because recovering bytes the
    # replica never received was the path BE-0404 set out to preserve — but BE-0393's Motivation
    # names the Git spec and the file-browser pick alongside the upload as the things a member redoes
    # every session, so the column answers "what this org last bound". One record, not a list: an org
    # that needs several bundles addressable holds them as artifacts and composes one (BE-0268). Null
    # until the org binds something.
    config_source: Mapped[dict[str, Any] | None] = mapped_column(_JSON, default=None)
