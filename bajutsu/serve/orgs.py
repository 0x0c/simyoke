"""The config-declared org model for `serve` (BE-0015 multi-tenancy, BE-0129).

Hosting is a `serve` concern the deterministic core does not model, so the `orgs:` block and its
resolution helpers live here rather than in `bajutsu/config`. `load_serve_config` parses a raw
config once, splitting it into the core `Config` (org-agnostic) and the org model the serve auth /
storage layer resolves against.

That model has two producers (BE-0375): `parse_orgs` from the `orgs:` block, and `orgs_from_db`
from the database a hosted deployment runs against. They yield the same `{name: OrgConfig}` shape,
so every resolution helper below is unchanged by which one a deployment reads — only `targets`
differs, since target ownership stays in configuration either way.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import Field, ValidationError, model_validator

from bajutsu.common import _yaml
from bajutsu.common.config import Config, _Model, parse_config_dict
from bajutsu.serve.oidc import WorkloadClaims

if TYPE_CHECKING:  # keeps the default serve/CLI path free of `serve.server` (server/__init__.py)
    from bajutsu.serve.server.db import Repository

_logger = logging.getLogger(__name__)

# An `allowedRepositories` name: exactly `"<owner>/<repo>"`, with neither half empty and no stray
# whitespace. Validated rather than accepted as free text because matching is exact equality — a
# malformed entry would otherwise sit in the config matching nothing, reading as a grant that is
# silently inert.
_REPOSITORY_RE = re.compile(r"[^\s/]+/[^\s/]+")


class AllowedRepository(_Model):
    """One entry of an org's `allowedRepositories` (BE-0414 unit 2).

    Written either as the bare `"<owner>/<repo>"` string or as an object carrying that same name
    plus its own narrowing bounds, so one org can list a repository that runs under a deployment
    environment beside one that does not — without a second org existing only to hold the
    unbounded entry.

    Anyone who can merge a workflow change to a listed repository can mint a token from it, so the
    name alone means "whoever can write that repository's workflows may act as this org". The
    optional bounds tighten that to a reviewed environment, a ref, or one workflow file.

    Attributes:
        repository: The `"<owner>/<repo>"` name, compared to the token's own repository claim by
            exact equality. A *mutable* name, consciously: an immutable numeric id would close the
            name-recycling exposure but leaves a configuration nobody can read, so the operator
            carries the duty to update an entry whose repository is renamed, transferred, or
            deleted — and to revoke its outstanding machine sessions.
        environment: When set, the job must have declared this deployment environment. A GitHub
            Environment can require reviewers before a job runs, which is the tightest bound
            available to a repository that also takes outside contributions.
        ref: When set, the git ref the run must have been triggered on.
        workflow_ref: When set, the workflow definition the job must have run from — GitHub
            Actions' `job_workflow_ref` claim, named provider-neutrally here because the bound
            means the same thing on every platform that emits one.
    """

    repository: str
    environment: str | None = None
    ref: str | None = None
    workflow_ref: str | None = Field(default=None, alias="workflowRef")

    @model_validator(mode="before")
    @classmethod
    def _accept_a_bare_name(cls, data: Any) -> Any:
        """Let an entry needing no bound stay the one-line string it reads best as."""
        return {"repository": data} if isinstance(data, str) else data

    @model_validator(mode="after")
    def _check_the_entry(self) -> AllowedRepository:
        if not _REPOSITORY_RE.fullmatch(self.repository):
            raise ValueError(
                f'allowedRepositories entries must each be "<owner>/<repo>", got {self.repository!r}'
            )
        # An empty bound is the same silently-inert grant the name check above exists to prevent,
        # one field over: `admits` treats a bound as active whenever it is not None, while a claim
        # that arrives empty reads as absent (`oidc._text`) — so `environment: ""` would match
        # nothing at all while looking in the config exactly like a narrowing that works.
        for field, value in (
            ("environment", self.environment),
            ("ref", self.ref),
            ("workflowRef", self.workflow_ref),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"an allowedRepositories {field} bound must not be empty")
        return self

    def admits(self, workload: WorkloadClaims) -> bool:
        """Whether this entry admits the pipeline *workload* names.

        Exact equality throughout, never a prefix test: `acme/app` prefix-matches `acme/app-evil`,
        so a substring comparison would admit a repository the operator never listed. A configured
        bound refuses an absent claim as firmly as a differing one — `environment` is emitted only
        when the job references one, so anything weaker would let a job declaring no environment
        escape the narrowing entirely.

        The repository name is compared case-insensitively, because GitHub itself treats owner and
        repository names that way: `Acme/App` and `acme/app` cannot both exist, so an operator who
        types the casing differently from the claim means the same repository — and a
        case-sensitive test would answer them with an entry that admits nobody.

        `str.lower`, not `str.casefold`, for the reason `in_teams` below spells out at length:
        full case folding equates names GitHub keeps distinct, so a repository whose name merely
        *folds* equal to a listed one would clear this gate. Lowercasing equates nothing beyond
        ASCII case, which is all GitHub's own case-insensitivity implies.

        The bounds stay case-sensitive: a ref and a workflow path are not GitHub names.
        """
        return (
            workload.repository.lower() == self.repository.lower()
            and (self.environment is None or workload.environment == self.environment)
            and (self.ref is None or workload.ref == self.ref)
            and (self.workflow_ref is None or workload.workflow_ref == self.workflow_ref)
        )


class OrgConfig(_Model):
    """One tenant under `orgs.<name>` (BE-0015 multi-tenancy).

    Holds the GitHub logins that belong to it (`members`), the GitHub orgs whose members belong to
    it (`github_orgs`), and/or the GitHub Teams whose direct members belong to it (`github_teams`),
    plus the targets it owns. A target named in no org falls back to the single `default` org.
    `editor_teams` (BE-0313) names the flat GitHub Teams, each as `"<github-org>/<team-slug>"`, whose
    direct members are promoted to editor within this org; an empty list leaves every member of the
    org at viewer.

    Each `github_teams` entry has that same `"<github-org>/<team-slug>"` shape and admits its direct
    members to this org at viewer, so a deployment can grant a single Team access without granting
    its whole GitHub organization — the narrower unit a GitHub organization's own structure already
    models. `editor_teams` admits as well as promotes: a Team whose members may write is a Team whose
    members may sign in, and requiring it to be repeated under `github_teams` would make "may write
    but cannot log in" a configuration an operator can write by accident.

    A list rather than the single Team it started as (BE-0375 unit 9), for the reason
    `BAJUTSU_OAUTH_ADMIN_TEAMS` is one: `github_orgs` and `github_teams` are lists because one org
    may span several GitHub organizations, and a single `editor_teams` slot cannot then name a
    writing Team per organization — the only ways out being to merge Teams on GitHub's side or to
    keep one roster by hand, which is the manual maintenance BE-0313 removed.
    """

    members: list[str] = Field(default_factory=list)
    github_orgs: list[str] = Field(default_factory=list, alias="githubOrgs")
    github_teams: list[str] = Field(default_factory=list, alias="githubTeams")
    editor_teams: list[str] = Field(default_factory=list, alias="editorTeams")
    targets: list[str] = Field(default_factory=list)
    # The repositories whose continuous-integration (CI) jobs may exchange an OIDC token for a
    # machine session acting as this org (BE-0414 unit 2). A *machine* roster, deliberately beside
    # the human one rather than folded into it: `members` and the Team lists grant a person a role,
    # and a pipeline gets no role at all — only the endpoint allowlist a machine principal carries.
    allowed_repositories: list[AllowedRepository] = Field(
        default_factory=list, alias="allowedRepositories"
    )

    @model_validator(mode="before")
    @classmethod
    def _fold_retired_editor_team(cls, data: Any) -> Any:
        """Fold the retired singular `editorTeam` key into `editor_teams` (BE-0375 unit 9).

        Accepted rather than rejected, and folded in rather than preferred one way or the other: this
        model is `extra="forbid"`, so an un-renamed key would raise out of `parse_orgs`, and
        `load_serve_config_file` answers a parse failure with *no* org model — every login of a
        deployment that missed one key would be turned away under "user not allowed", the silent
        lockout BE-0352's retired-name warning exists to prevent. `BAJUTSU_OAUTH_ADMIN_TEAM` could
        take that route because an unread environment variable still leaves a config that loads.

        A deployment that sets both keys (the likelier partial rename — an operator adds the plural
        name and leaves the singular one behind) keeps both Teams, so neither spelling silently
        loses the role it was written to grant.
        """
        if not isinstance(data, dict) or "editorTeam" not in data:
            return data
        data = dict(data)
        retired = data.pop("editorTeam")
        existing = data.get("editorTeams")
        # `editorTeam: ""` and a missing key both meant "no editor Team" before the rename, so an
        # empty value folds to nothing rather than to an entry that matches no Team but does make
        # `orgs_declaring_membership` count this org as having a roster.
        if retired is None or (isinstance(retired, str) and not retired.strip()):
            return data
        if existing is None or isinstance(existing, list):
            data["editorTeams"] = [*(existing or []), retired]
        return data

    def admitting_teams(self) -> list[str]:
        """Every Team whose direct members this org admits — `github_teams` plus `editor_teams`.

        One accessor, so the sign-in gate and every "does this org declare a membership" check read
        the same union and cannot disagree about whether `editor_teams` alone admits anyone.
        """
        return [*self.github_teams, *self.editor_teams]

    def admits_workload(self, workload: WorkloadClaims) -> bool:
        """Whether any `allowedRepositories` entry admits the pipeline *workload* names.

        An org listing nothing admits nothing, so the machine path stays off until an operator
        opts a repository in — the same posture as an unconfigured audience disabling the caller
        shape outright.
        """
        return any(entry.admits(workload) for entry in self.allowed_repositories)


# The single tenant every unassigned user and target falls into.
DEFAULT_ORG = "default"


def in_teams(teams: Sequence[str], wanted: Iterable[str]) -> bool:
    """Whether any of *teams* is one of *wanted*, the single Team-membership test behind the sign-in
    gate, the editor role, and the server-wide admin Team — so a Team that admits a login and the
    role that login resolves to can never drift apart.

    Lowercased on both sides, since GitHub resolves an org login and a Team slug
    case-insensitively and `identity.teams` reports GitHub's own casing either way. `str.lower`
    rather than `str.casefold`: full case folding equates names GitHub keeps distinct (`gruß` and
    `gruss` can be two Teams of one organization), and since BE-0375 unit 8 a match buys sign-in, so
    anyone able to create the folded-equal Team would clear the gate. Lowercasing equates nothing
    beyond ASCII case, which is what GitHub's own slug lowercasing implies; it never turns an empty
    Team name into a match, and preserves the nested-Team guarantee, which rests on exact string
    equality of the full `"<github-org>/<team-slug>"` (BE-0352).
    """
    lowered = {t.lower() for t in wanted}
    return any(team.lower() in lowered for team in teams)


def _match_orgs(
    orgs: dict[str, OrgConfig], login: str, github_orgs: list[str], teams: Sequence[str]
) -> list[str]:
    """Every org *login* belongs to, best match first — empty when no declared org admits it.

    The one place the three membership axes are ranked, so the sign-in gate and the placement below
    can never admit a login into one org while resolving it to another. An explicit `members` entry
    wins, then an intersection with some org's `github_orgs`, then direct membership in one of an
    org's admitting Teams (`githubTeams` or its `editorTeams`). Teams rank last so that adding one to
    an org never relocates a login an existing `members`/`githubOrgs` entry already placed.

    Order within an axis is deterministic but source-dependent; see `org_for_identity`. An org
    matching on two axes appears once, at its best rank.

    Empty rather than `[DEFAULT_ORG]` for "matched nothing", so a login no entry lists stays
    distinguishable from one an org literally named `default` lists as a member.
    """
    user_orgs = set(github_orgs)
    matched: list[str] = [org for org, oc in orgs.items() if login in oc.members]
    matched += [
        org
        for org, oc in orgs.items()
        if org not in matched and user_orgs.intersection(oc.github_orgs)
    ]
    matched += [
        org
        for org, oc in orgs.items()
        if org not in matched and in_teams(teams, oc.admitting_teams())
    ]
    return matched


def identity_matches_org(
    orgs: dict[str, OrgConfig], login: str, github_orgs: list[str], teams: Sequence[str]
) -> bool:
    """Whether *login* (with GitHub memberships *github_orgs* and *teams*) belongs to any declared
    org (BE-0313).

    True when the login is an explicit `members` entry, a member of some org's `github_orgs`, or a
    direct member of one of some org's admitting Teams. The sign-in gate consults this before
    `org_for_identity`, whose plain `str` return can't tell a login that matched nothing from one
    that legitimately resolved to `default` — and a deployment may name an org literally `default`.
    An empty `orgs` mapping — no `orgs:` block, or a config that failed to load — matches nobody, so
    this gate alone admits no login; `oauth_callback` admits a configured admin Team's members
    alongside it, so a deployment can still recover from a missing or broken block.

    *teams* has no default, and neither does `org_for_identity`'s: a caller that could omit the
    Team axis is exactly how the gate and the placement would come to consult different axes again,
    and it would type-check clean while silently denying or misplacing a Team-admitted login.
    """
    return bool(_match_orgs(orgs, login, github_orgs, teams))


def orgs_for_identity(
    orgs: dict[str, OrgConfig], login: str, github_orgs: list[str], teams: Sequence[str]
) -> list[str]:
    """Every org a user logging in as *login* may act as, best match first.

    The full ranked list behind `org_for_identity`'s single answer, so a login belonging to several
    orgs can be offered the choice between them instead of being pinned to the head. Empty for a
    login no declared org admits — including one admitted by the admin-Team bypass, whose eligible
    set is decided by that bypass rather than by any org's membership.
    """
    return _match_orgs(orgs, login, github_orgs, teams)


def preferred_org(eligible: Sequence[str]) -> str:
    """The org a login lands in when it has picked none: the best match, else `DEFAULT_ORG`.

    The one place the "head of the ranking, else the fallback" rule lives, so the sign-in placement
    and `org_for_identity` cannot come to disagree about what an empty eligible set means.
    """
    return eligible[0] if eligible else DEFAULT_ORG


def org_for_identity(
    orgs: dict[str, OrgConfig], login: str, github_orgs: list[str], teams: Sequence[str]
) -> str:
    """The org for a user logging in as *login* with the given GitHub *github_orgs* and *teams*
    memberships (BE-0015).

    An explicit `members` listing wins; otherwise the first org whose `github_orgs` intersects the
    user's GitHub orgs; otherwise the first org one of whose admitting Teams the user is a direct
    member of; otherwise `default`.

    "First" is deterministic but source-dependent, which matters only when two orgs name the same
    GitHub organization or Team: `parse_orgs` preserves the order the `orgs:` block declares them in,
    while `orgs_from_db` iterates slug order (`list_orgs` sorts by it). Both are stable — the same
    login resolves the same way on every sign-in — but a deployment holding such an overlap can see
    the tie-break move once, at the conversion to the database (BE-0375). The tie-break decides only
    where such a login *starts*: `orgs_for_identity` hands back the whole ranked list, and a user who
    picks another org from it keeps that pick across sign-ins.

    Sign-in composes the same two steps itself — `orgs_for_identity` for the list it stores, then
    `preferred_org` for the placement — because it needs the list either way. This is the one-call
    form of that answer, for a caller holding an identity and wanting only the org.
    """
    return preferred_org(_match_orgs(orgs, login, github_orgs, teams))


def targets_for_org(
    orgs: dict[str, OrgConfig],
    targets: Iterable[str],
    org: str,
    *,
    bound_by: str | None = None,
) -> list[str]:
    """The targets belonging to *org*, restricted to *targets* actually declared under `targets:`.

    An org that lists an undeclared target name doesn't conjure a runnable target. For `default`,
    that's every declared target no org claims.

    *bound_by* names the org that bound this configuration through the API — an uploaded bundle, a
    composed triple, or a Git source (BE-0375). The `orgs:` block is then not consulted at all: the
    bundle was uploaded *as* that org, so every target it declares is that org's and no other org's.
    Reading ownership out of a file the deployment does not control is the same trust problem this
    item already refused for membership, and it fails silently — an entry claiming a target for an
    org the reader is not in leaves them a target list that is simply empty, with nothing said. The
    launch configuration passes None and keeps the `orgs:`-declared ownership, which is the
    multi-tenant deployment shape an operator writes by hand.
    """
    declared = list(targets)
    if bound_by is not None:
        return declared if org == bound_by else []
    if org == DEFAULT_ORG:
        claimed = {a for oc in orgs.values() for a in oc.targets}
        return [a for a in declared if a not in claimed]
    oc = orgs.get(org)
    return [a for a in oc.targets if a in declared] if oc else []


def parse_orgs(orgs_block: object) -> dict[str, OrgConfig]:
    """Validate a raw `orgs:` mapping into `{name: OrgConfig}`.

    A missing/`null` block (or an empty mapping) yields `{}`. Any other present-but-non-mapping
    value (a string, number, or list) is a config error, not silently ignored — so a malformed
    `orgs:` fails loudly rather than collapsing to single-tenant.
    """
    if orgs_block is None:
        return {}
    if not isinstance(orgs_block, dict):
        raise ValueError("orgs: must be a mapping of org name to its config")  # noqa: TRY004  # invalid external payload, not a caller type error
    return {name: OrgConfig.model_validate(body or {}) for name, body in orgs_block.items()}


def orgs_from_db(repository: Repository) -> dict[str, OrgConfig]:
    """The org model read from the database — `parse_orgs`'s shape from a second source (BE-0375).

    Assembles the identical `{name: OrgConfig}` mapping the `orgs:` block produces, keyed by each
    row's id (the same string `state.org_of` hands back as `org_id`), so every membership consumer
    resolves against it unchanged. `targets` is always empty: an org's target ownership stays in
    configuration, resolved through `targets_for_org` against the config-parsed model.

    Unlike `load_serve_config_file`, a read failure propagates rather than collapsing to an empty
    mapping. An empty mapping means "no org matched"; a database `serve` cannot read must answer
    with an error naming the database, not deny every user by blaming their GitHub membership.
    """
    return {
        # Built through the field aliases, the same names the `orgs:` block itself uses, so this
        # producer and `parse_orgs` construct the identical model from the identical key names.
        row.id: OrgConfig(
            members=list(row.members),
            githubOrgs=list(row.github_orgs),
            githubTeams=list(row.github_teams),
            editorTeams=list(row.editor_teams),
            allowedRepositories=_stored_repositories(row.id, row.allowed_repositories),
        )
        for row in repository.list_orgs()
    }


def _stored_repositories(org: str, entries: Sequence[Any]) -> list[AllowedRepository]:
    """The machine-roster entries of a stored org row, dropping any that no longer validate.

    Unlike the `orgs:` block, which fails loudly at parse because an operator is there to fix it,
    a malformed row here must not raise: `orgs_from_db` builds the model the *human* sign-in path
    resolves against (BE-0375), so one bad entry — a direct database edit, a rollback, a future
    writer — would otherwise turn every person's login into a denial as well as every pipeline's.

    Dropping fails closed in the direction that matters: the machine roster only ever *grants*, so
    an entry nobody can read admits nobody, which is what an unreadable grant has to mean.
    """
    kept: list[AllowedRepository] = []
    for entry in entries:
        try:
            kept.append(AllowedRepository.model_validate(entry))
        except ValidationError:
            _logger.warning(
                "org %s has an unreadable allowedRepositories entry; it admits nothing: %r",
                org,
                entry,
            )
    return kept


def orgs_declaring_membership(orgs: dict[str, OrgConfig]) -> list[str]:
    """The entries that declare a roster — human or machine (BE-0375, widened by BE-0414 unit 2).

    An entry carrying only `targets` is the end state a database-backed deployment is meant to
    reach, since target ownership stays in configuration, so it is never one of these — which is
    what lets the caller warn about the rest without firing forever on a correct configuration.
    `allowedRepositories` counts as a roster for the same reason the human lists do: an entry that
    declares one and never gets seeded would leave its pipelines locked out with nothing saying why.
    """
    return [
        name
        for name, oc in orgs.items()
        if oc.members or oc.github_orgs or oc.admitting_teams() or oc.allowed_repositories
    ]


def seed_orgs_from_config(repository: Repository, orgs: dict[str, OrgConfig]) -> list[str]:
    """Seed each config-declared org's membership into the database (BE-0375).

    Run once, at startup, against an `orgs` table that holds no row at all — the caller owns that
    condition. One boot converts a configuration-only deployment; afterwards the database is the
    sole author of its own roster. `seed_org_membership` still skips a row already marked seeded or
    soft-deleted, so a partially converted table (a passive row an earlier sign-in left behind)
    cannot be seeded twice.

    An entry declaring only `targets` is skipped rather than seeded, so the cutover marker is never
    spent on a roster nobody wrote. That entry is legitimate in both directions: it is the end state
    the docs recommend *after* the conversion, and paring the config down before the converting boot
    must not be the irreversible act of locking every org at "admits nobody". Its row is therefore
    left uncreated — target ownership resolves from the configuration, not the table.

    Returns the names of the entries that declared membership and were nonetheless *not* seeded, so
    the caller can tell an operator that configuration no longer decides them.
    """
    stale: list[str] = []
    for name in orgs_declaring_membership(orgs):
        oc = orgs[name]
        seeded = repository.seed_org_membership(
            name,
            slug=name,
            name=name,
            members=list(oc.members),
            github_orgs=list(oc.github_orgs),
            github_teams=list(oc.github_teams),
            editor_teams=list(oc.editor_teams),
            allowed_repositories=[e.model_dump(by_alias=True) for e in oc.allowed_repositories],
        )
        if not seeded:
            stale.append(name)
    return stale


def load_serve_config(text: str) -> tuple[Config, dict[str, OrgConfig]]:
    """Parse a raw config into the core `Config` plus its org model (BE-0129).

    The document is parsed once: `serve` validates the `orgs:` block locally, while
    `parse_config_dict` builds the org-agnostic `Config` (dropping `orgs:` itself).
    """
    data = _yaml.safe_load(text) or {}
    orgs = parse_orgs(data.get("orgs") if isinstance(data, dict) else None)
    return parse_config_dict(data), orgs
