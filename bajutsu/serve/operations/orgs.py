"""Org-lifecycle serve operations (BE-0375 unit 5): the four `/api/orgs…` endpoints.

An org is `serve`'s multi-tenancy unit, and once a database is wired it is the database — not the
`orgs:` block — that decides who signs in as which org. These operations are how an admin creates a
tenant, replaces its membership, and retires it, without editing the deployment's configuration and
redeploying. Every one is admin-only (`authz.required_role`) and audited; each needs a repository,
since a database-less `serve` is single-user by construction and has no tenant boundary to
administer. All logic is deterministic — a membership list is data, and the sign-in gate that reads
it is the same machine-checkable comparison it was; no LLM enters the path.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from bajutsu.serve import oplog
from bajutsu.serve.authz import _record_audit
from bajutsu.serve.orgs import DEFAULT_ORG, AllowedRepository
from bajutsu.serve.state import ServeState

_logger = logging.getLogger(__name__)

# A database is what makes an org more than a name, so every operation here needs one. 400 rather
# than 404: the endpoint exists, this deployment just isn't shaped to serve it. The reason is named
# separately because the boot
# read reports it too, as the `orgs` capability's reason (#1721) — one string, so the flag the UI
# gates on and the answer it would have got can never say different things.
NO_ORG_STORE_ERROR = "org management needs a database"
_NO_ORG_STORE = ({"error": NO_ORG_STORE_ERROR}, 400)

# `slug` is the org's id, and that id is already carried as `org_id` on every user, run, secret, and
# audit row — so it goes in URLs and object-storage key prefixes. Keep it to what is safe in both.
_SLUG_MAX = 64


def _validate_slug(slug: str) -> str | None:
    """The reason *slug* is unusable as an org id, or None when it is fine."""
    if not slug:
        return "slug is required"
    if len(slug) > _SLUG_MAX:
        return f"slug must be at most {_SLUG_MAX} characters"
    # Lowercase alphanumerics plus `-`/`_`: the org id becomes a path segment and an object-storage
    # key prefix, so a slash, a dot-segment, or whitespace would be a traversal or a collision
    # rather than a naming preference. Rejected loudly instead of normalized, so an admin gets back
    # the org they asked for (determinism first).
    if not all(c.isascii() and (c.isalnum() or c in "-_") for c in slug):
        return "slug must contain only ASCII letters, digits, '-' and '_'"
    if not slug.islower() and any(c.isalpha() for c in slug):
        return "slug must be lowercase"
    return None


def _string_list(value: Any, field: str) -> tuple[list[str] | None, str | None]:
    """*value* as a list of non-empty strings, or a reason it is not one."""
    if value is None:
        return [], None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None, f"{field} must be a list of strings"
    entries = [v.strip() for v in value]
    if any(not v for v in entries):
        return None, f"{field} must not contain an empty entry"
    return entries, None


def _allowed_repositories(body: dict[str, Any]) -> tuple[list[Any] | None, str | None]:
    """The org's machine roster from *body*, validated, or a reason it is not one (BE-0414 unit 2).

    An absent key returns `(None, None)`: the roster is left as it stands rather than replaced with
    nothing. The four human fields above take the opposite default deliberately — a caller that
    predates *this* field would otherwise revoke every pipeline's access to the org and be told the
    update succeeded, the same silent-strip failure the retired `editorTeam` key is refused over.
    Sending the key, `[]` included, replaces the roster as one unit like everything else.
    """
    if "allowedRepositories" not in body:
        return None, None
    value = body["allowedRepositories"]
    if not isinstance(value, list):
        return None, "allowedRepositories must be a list"
    try:
        # Validated here, not at the exchange: a malformed entry rejected on write is one an admin
        # can fix, where one stored and rejected on read is a pipeline failing for an invisible
        # reason. Round-tripped through the model so what lands in the row is the canonical shape.
        return [
            AllowedRepository.model_validate(entry).model_dump(by_alias=True) for entry in value
        ], None
    except ValidationError as e:
        return None, f"allowedRepositories is not valid: {e.errors()[0].get('msg', e)}"


def list_orgs_view(state: ServeState, *, actor: str | None = None) -> tuple[Any, int]:  # noqa: ARG001  # uniform operation signature
    """Every live org with its membership — the Orgs page's list and the source its edit form fills.

    The rosters themselves, not just their sizes: the membership form replaces the four human
    fields as one unit, so it has to start from the current values or the first save would silently
    empty what it never showed. `allowedRepositories` travels with them but is the exception —
    omitting it on a save leaves it alone (BE-0414 unit 2), so a client that never showed it cannot
    strip it. Only an admin can reach this (`authz.required_role`), which is the same
    tier that could already read the `orgs:` block through `GET /api/config/content`.
    """
    if state.repository is None:
        return _NO_ORG_STORE
    repository = state.repository
    return [
        {
            "slug": org.slug,
            "name": org.name,
            "members": org.members,
            "githubOrgs": org.github_orgs,
            "githubTeams": org.github_teams,
            "editorTeams": org.editor_teams,
            # The machine roster travels with them (BE-0414 unit 2), by the same argument the
            # docstring makes for the human ones: a form that never showed it could not send it
            # back. Omitting it on a save leaves it alone, so an older client is safe either way.
            "allowedRepositories": org.allowed_repositories,
            # The fallback an unmatched sign-in resolves to is listed (an admin admitted by the
            # bypass is sitting in it, and hiding that would hide where their own work lands) but is
            # not a tenant: all three mutations refuse it, so the page marks it rather than offering
            # controls that only ever answer 409.
            "reserved": org.slug == DEFAULT_ORG,
        }
        for org in repository.list_orgs()
    ], 200


def create_org(
    state: ServeState, body: dict[str, Any], *, actor: str | None = None
) -> tuple[Any, int]:
    """Create an org from ``{slug, name}``, with empty membership.

    A fresh org admits nobody until an admin adds to it — membership is never inherited from a
    configuration entry, because the new row is marked seeded at creation, so a later `orgs:` entry
    for the same slug can never overwrite what an admin sets here. It also owns no target until an
    `orgs:` entry names some: target ownership stays in configuration (BE-0375 unit 1).
    """
    if state.repository is None:
        return _NO_ORG_STORE
    slug = str(body.get("slug") or "").strip()
    invalid = _validate_slug(slug)
    if invalid is not None:
        return {"error": invalid}, 400
    if slug == DEFAULT_ORG:
        # `serve` hardcodes this slug as the org an unmatched sign-in falls into — the admin-Team
        # bypass's landing place, and the fallback `targets_for_org` decides by the literal string
        # before it reads any entry. A real tenant created here would silently take that namespace,
        # and `delete_org` refuses the slug outright, so nothing could undo it through this API.
        return {
            "error": f"{DEFAULT_ORG!r} is reserved as the sign-in fallback and cannot be created"
        }, 409
    name = str(body.get("name") or "").strip() or slug
    if not state.repository.create_org(slug=slug, name=name):
        # Taken by a live org, or by a soft-deleted one still holding the UNIQUE slug. Say both, so
        # an admin who deleted this slug earlier isn't left hunting an org the list doesn't show —
        # reactivating a retired org is deliberately not something this endpoint does.
        return {
            "error": f"an org named {slug!r} already exists (a deleted org keeps its slug)"
        }, 409
    _record_audit(state, actor, state.org_of(actor), "org.create", slug, {"name": name})
    return {"slug": slug, "name": name}, 200


def update_org_membership(
    state: ServeState, slug: str, body: dict[str, Any], *, actor: str | None = None
) -> tuple[Any, int]:
    """Replace an org's ``{members, githubOrgs, githubTeams, editorTeams}`` as one unit.

    The same granularity a configuration edit already had, rather than per-entry add/remove: an
    admin sees the whole roster and sends back the whole roster, so two concurrent edits can't
    interleave into a membership neither of them asked for. Takes effect on the next sign-in, like
    every other membership change (BE-0313 recomputes on every login).
    """
    if state.repository is None:
        return _NO_ORG_STORE
    if slug == DEFAULT_ORG:
        # The same reservation `create_org` and `delete_org` apply, and for the same reason: giving
        # this slug a roster makes `identity_matches_org` place those logins in it, so the fallback
        # an unmatched sign-in resolves to becomes a real tenant — exactly what refusing to create it
        # prevents. The row is listed and reachable (a bypass sign-in's `ensure_org` creates it), so
        # without this guard the reservation held on two verbs out of three.
        return {
            "error": f"{DEFAULT_ORG!r} is the sign-in fallback; its membership is not editable"
        }, 409
    if state.repository.get_org(slug) is None:
        return {"error": f"no org named {slug!r}"}, 404
    members, invalid = _string_list(body.get("members"), "members")
    if invalid is not None:
        return {"error": invalid}, 400
    github_orgs, invalid = _string_list(body.get("githubOrgs"), "githubOrgs")
    if invalid is not None:
        return {"error": invalid}, 400
    github_teams, invalid = _string_list(body.get("githubTeams"), "githubTeams")
    if invalid is not None:
        return {"error": invalid}, 400
    editor_teams, invalid = _string_list(body.get("editorTeams"), "editorTeams")
    if invalid is not None:
        return {"error": invalid}, 400
    allowed_repositories, invalid = _allowed_repositories(body)
    if invalid is not None:
        return {"error": invalid}, 400
    if "editorTeam" in body:
        # The retired singular field is refused here, not folded in the way the `orgs:` block's own
        # key is (`OrgConfig._fold_retired_editor_team`). `_string_list` reads a missing `editorTeams`
        # as an empty list, so a caller still on the old name would replace this org's roster with one
        # granting nobody write access — and get a 200 back saying so. The two surfaces differ in what
        # refusing costs: one request an operator can fix here, every login of the deployment there.
        # Loud rather than normalized, like `_validate_slug` above (determinism first).
        return {"error": "editorTeam is retired; send editorTeams as a list of Teams instead"}, 400
    # Narrowed by the four error returns above.
    assert members is not None and github_orgs is not None
    assert github_teams is not None and editor_teams is not None
    if not state.repository.set_org_membership(
        slug,
        members=members,
        github_orgs=github_orgs,
        github_teams=github_teams,
        editor_teams=editor_teams,
        allowed_repositories=allowed_repositories,
    ):
        return {"error": f"no org named {slug!r}"}, 404
    _record_audit(
        state,
        actor,
        state.org_of(actor),
        "org.membership.update",
        slug,
        # The logins themselves are the point of the entry — "who could sign in as this tenant, from
        # when" is exactly what an audit of a membership change has to answer.
        # Which repositories may act as this tenant is the same question for a machine that the
        # logins answer for a person, so it belongs in the entry too — but only when this request
        # actually set it, or the log would read as a change to a roster nobody touched.
        {
            "members": members,
            "githubOrgs": github_orgs,
            "githubTeams": github_teams,
            "editorTeams": editor_teams,
            **(
                {"allowedRepositories": allowed_repositories}
                if allowed_repositories is not None
                else {}
            ),
        },
    )
    return {
        "slug": slug,
        "members": members,
        "githubOrgs": github_orgs,
        "githubTeams": github_teams,
        "editorTeams": editor_teams,
        **(
            {"allowedRepositories": allowed_repositories}
            if allowed_repositories is not None
            else {}
        ),
    }, 200


def delete_org(state: ServeState, slug: str, *, actor: str | None = None) -> tuple[Any, int]:
    """Retire an org: it stops admitting sign-ins and drops out of the list, but keeps its row.

    A soft delete, because `users`, `runs`, `secrets`, `provider_settings`, and `audit_log` all
    still hold foreign keys on the org's id — including this deletion's own audit entry, which
    would have nothing left to point at. Its history stays queryable: an admin action removes a
    tenant's ability to act, not the record of what it already did.

    Refused for the `default` org outright, which `serve` hardcodes as the fallback an unmatched
    bypass sign-in resolves to regardless of table state — deleting it would only leave a retired
    org users keep landing on.
    """
    if state.repository is None:
        return _NO_ORG_STORE
    if slug == DEFAULT_ORG:
        return {
            "error": f"the {DEFAULT_ORG!r} org is the sign-in fallback and cannot be deleted"
        }, 409
    if state.repository.get_org(slug) is None:
        return {"error": f"no org named {slug!r}"}, 404
    # Read the roster before the delete so the revocation below has it, then retire the org first:
    # the row is what turns away the *next* sign-in, so it must land even if session cleanup fails.
    members = state.repository.list_org_user_ids(slug)
    if not state.repository.soft_delete_org(slug, at=datetime.now(UTC)):
        return {"error": f"no org named {slug!r}"}, 404
    # A soft delete alone reaches only future sign-ins: `users.org_id` still names the retired slug,
    # so a cookie issued before it would keep listing that tenant's runs, triggering new ones, and
    # reading its secrets until it expired. Retiring an org used to mean a config edit plus a
    # redeploy, and the restart dropped every session as a side effect; making it an in-process
    # admin action removes that incidental revocation, so this does it deliberately (BE-0375).
    revoked = state.auth.sessions.revoke_identities(members)
    # Their configuration bindings go with the sessions that held them (BE-0393 unit 2): a revoked
    # cookie already reads the fallback, so this reclaims the slots rather than closing a hole.
    state.drop_revoked_bindings()
    _record_audit(
        state, actor, state.org_of(actor), "org.delete", slug, {"sessionsRevoked": revoked}
    )
    return {"ok": True, "slug": slug, "sessionsRevoked": revoked}, 200


def set_active_org(
    state: ServeState, body: dict[str, Any], *, actor: str | None = None
) -> tuple[Any, int]:
    """Make ``{org}`` the caller's active org — the tenant every other tab is scoped to.

    A login whose GitHub memberships match several orgs is offered all of them in the header, and
    this is how it picks one. Unlike the three operations above it is not an admin action: it moves
    the caller between orgs that already admit them, and grants nothing their last sign-in did not
    already establish. Like every membership change (BE-0313 recomputes on each login), a revocation
    since then takes effect at their next sign-in — so until then a switch into the revoked org is
    still admitted, the same latency `update_org_membership` already documents.

    Authorized against the set written at sign-in (`ServeState.eligible_orgs`), so an org the caller
    is not a member of is refused rather than silently ignored, and an empty set refuses everything.
    The role travels with the tenant, because a role is per-org: an org's `editorTeams` promotes a
    member inside that org alone. The choice survives later sign-ins, which is what distinguishes it
    from the org the membership ranking resolves.
    """
    if state.repository is None:
        return _NO_ORG_STORE
    if not actor:
        # No signed-in identity: a local or shared-token session acts as `default` for everything,
        # so there is no per-user row to move and nothing a switch could mean.
        return {"error": "switching orgs needs a signed-in identity"}, 403
    slug = str(body.get("org") or "").strip()
    if not slug:
        return {"error": "org is required"}, 400
    eligible = state.eligible_orgs(actor)
    if slug not in eligible:
        # Deliberately the same answer for "no such org" and "not a member of it": the caller may
        # not act as either, and distinguishing them would disclose which tenants a deployment has
        # to someone with no business knowing — the reason the roster itself is admin-only.
        return {"error": f"you are not a member of org {slug!r}"}, 403
    previous = state.org_of(actor)
    if not state.repository.select_active_org(actor, slug, role=eligible[slug]):
        return {"error": f"no user record for {actor!r}"}, 404
    oplog.log_event(
        _logger,
        "org.switch",
        f"{actor} switched from {previous} to {slug}",
        actor=actor,
        level=logging.INFO,
    )
    # Recorded against the destination, which is the tenant the actor's next action lands in — the
    # question a later reader of the audit log is asking. The origin travels in the detail, so the
    # move is reconstructable from either side.
    _record_audit(state, actor, slug, "org.switch", slug, {"from": previous})
    return {"ok": True, "org": slug, "role": eligible[slug]}, 200
