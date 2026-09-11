"""An organization as the seam exchanges it: its identity and the membership that decides sign-in."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class OrgRecord:
    """An org as the seam exchanges it — its identity plus the membership that decides sign-in.

    `members` / `github_orgs` / `github_teams` / `editor_teams` mirror `OrgConfig`'s own fields
    (BE-0375); a row that predates the move, or one `ensure_org` created at sign-in, carries empty
    lists throughout.
    `allowed_repositories` is the machine roster beside them (BE-0414 unit 2): the raw
    `allowedRepositories` entries as they are stored — always the object form, since both writers
    dump the validated model — left unparsed here so the seam stays free of the config model.
    `membership_seeded_at` is the per-row cutover marker (set = the database owns this org's
    membership), `deleted_at` the soft-delete marker. `config_source` is the `{kind, locator}` record
    naming the configuration this org last bound (BE-0404 unit 1, widened to every bind by BE-0393
    unit 6), None until it binds one.
    """

    id: str
    slug: str
    name: str
    members: list[str] = field(default_factory=list)
    github_orgs: list[str] = field(default_factory=list)
    github_teams: list[str] = field(default_factory=list)
    editor_teams: list[str] = field(default_factory=list)
    allowed_repositories: list[dict[str, Any]] = field(default_factory=list)
    membership_seeded_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime | None = None
    config_source: dict[str, Any] | None = None
