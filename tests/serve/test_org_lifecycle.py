"""Database-backed org lifecycle and membership for serve (BE-0375).

What these lock: the database, not the `orgs:` block, decides who signs in as which org once one is
wired; a configuration that fails to load no longer denies anyone there, while a database that
cannot be read says so with a 5xx instead of blaming a user's GitHub membership; a target's identity
is `(org, target)`, so two orgs may each claim one name; the four admin `/api/orgs…` endpoints
create, re-member, and retire a tenant, each audited; the backfill seeds each org row exactly once
and a later configuration edit cannot overwrite it; and the admin-Team bypass still admits a sign-in
against an empty table so the first org can be created at all.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _shared import log_field

from bajutsu.serve import operations as ops
from bajutsu.serve.authz import _target_forbidden
from bajutsu.serve.operations.config import seed_orgs_from_bound_config
from bajutsu.serve.orgs import identity_matches_org, org_for_identity, orgs_from_db, parse_orgs
from bajutsu.serve.server.oauth import Identity, OAuthClient
from bajutsu.serve.state import ServeState, SessionManager

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine

    from bajutsu.serve.server.db import Repository

# Two orgs that each claim a target named `checkout` — the collision an admin who creates tenants
# from the web UI cannot see coming, and `default` left owning the one target neither claims.
_COLLIDING_YAML = """
targets:
  checkout: { bundleId: com.example.checkout }
  spare: { bundleId: com.example.spare }

orgs:
  acme:
    members: [alice]
    githubOrgs: [acme-gh]
    githubTeams: [acme-gh/qa]
    editorTeams: [acme-gh/scenario-maintainers]
    targets: [checkout]
  globex:
    members: [bob]
    targets: [checkout]
"""


# The config a bind site brings with it: one org the seeded state has never heard of, so the row
# can only come from that bind's own seed.
_ORGS_ONLY_YAML = "targets: {}\norgs:\n  initech:\n    members: [peter]\n"


class _FakeOAuth:
    """The slice of the OAuth flow `oauth_callback` drives, in memory — no GitHub call."""

    def __init__(
        self, login: str, orgs: list[str] | None = None, teams: list[str] | None = None
    ) -> None:
        self._login, self._orgs, self._teams = login, orgs or [], teams or []

    def authorize_url(self, state: str) -> str:
        return f"https://github.test/?state={state}"

    def fetch_identity(self, code: str) -> Identity:
        return Identity(login=self._login, orgs=list(self._orgs), teams=list(self._teams))


def _config(tmp_path: Path, body: str = _COLLIDING_YAML) -> Path:
    path = tmp_path / "bajutsu.config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _state(
    serve_engine: Callable[..., Engine],
    tmp_path: Path,
    *,
    body: str = _COLLIDING_YAML,
    oauth: OAuthClient | None = None,
    admin_teams: list[str] | None = None,
    seed: bool = True,
    **extra: Any,
) -> ServeState:
    """A database-backed serve, seeded from its bound config the way `serve()` seeds at startup."""
    from bajutsu.serve.server.db import SqlRepository
    from bajutsu.serve.server.models import Base

    # `foreign_keys=True` so the SQLite parameter enforces the same foreign keys Postgres always
    # does — `audit_log.org_id` among them, which every audited operation here writes.
    engine = serve_engine(foreign_keys=True)
    Base.metadata.create_all(engine)
    state = ServeState(
        runs_dir=tmp_path / "runs",
        config=_config(tmp_path, body),
        cwd=tmp_path,
        repository=SqlRepository(engine),
        auth=SessionManager(oauth=oauth, oauth_admin_teams=tuple(admin_teams or ())),
        **extra,  # the bind-site tests below wire `root` / `uploads_dir` / `object_store`
    )
    if seed:
        seed_orgs_from_bound_config(state)
    return state


def _sign_in(state: ServeState) -> tuple[Any, int, str | None]:
    return ops.oauth_callback(state, code="ok", state_param="s", state_cookie="s")


def _admin(state: ServeState) -> str:
    """The admin the audited operations below act as, holding the user row a sign-in leaves behind.

    An audit entry carries foreign keys on both its actor and that actor's org, and an actor with no
    user row resolves to the `default` org (`ServeState.org_of`) — an org this deployment never
    created. A real sign-in cannot produce either dangling key, since `oauth_callback` calls
    `ensure_org` and then `upsert_user` before it ever issues a session, so an actor fabricated here
    needs the same row or the operation 500s on any database that enforces the keys.
    """
    assert state.repository is not None
    state.repository.upsert_user("root", org_id="acme", github_login="root", email="root@x")
    return "root"


# --- unit 1: the database is a second producer of the same org model -------------------------


def test_orgs_from_db_resolves_exactly_as_the_equivalent_orgs_block(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # The seeded table must answer every resolution question the `orgs:` block answers, or the
    # cutover would quietly change who belongs where. Only `targets` differs, and deliberately:
    # target ownership stays in configuration, so a database-sourced org carries none.
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    from_config = parse_orgs(
        {
            "acme": {
                "members": ["alice"],
                "githubOrgs": ["acme-gh"],
                "githubTeams": ["acme-gh/qa"],
                "editorTeams": ["acme-gh/scenario-maintainers"],
            },
            "globex": {"members": ["bob"]},
        }
    )
    from_db = orgs_from_db(state.repository)
    assert from_db == from_config
    for login, github_orgs, teams in (
        ("alice", [], []),
        ("dave", ["acme-gh"], []),
        ("erin", [], ["acme-gh/qa"]),
        ("frank", [], ["acme-gh/scenario-maintainers"]),
        ("stranger", ["other-gh"], ["other-gh/team"]),
    ):
        assert identity_matches_org(from_db, login, github_orgs, teams) == identity_matches_org(
            from_config, login, github_orgs, teams
        )
        assert org_for_identity(from_db, login, github_orgs, teams) == org_for_identity(
            from_config, login, github_orgs, teams
        )


def test_orgs_from_db_reads_a_row_with_no_membership_as_empty(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # A row that predates the membership columns, or one `ensure_org` created at sign-in, holds NULL
    # rather than `[]` — the model's Python-side default never reached it. Reading that as `None`
    # would break `identity_matches_org`'s membership scan on the first such row.
    state = _state(serve_engine, tmp_path, body="targets:\n  checkout: { bundleId: com.x }\n")
    assert state.repository is not None
    state.repository.ensure_org("legacy", slug="legacy", name="legacy")
    assert orgs_from_db(state.repository)["legacy"].members == []
    assert orgs_from_db(state.repository)["legacy"].github_orgs == []


# --- unit 2 and 3: one source, chosen once, and what a failure of it looks like ----------------


def test_sign_in_survives_a_config_that_cannot_load(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # The failure this item exists to remove: an unreadable configuration used to collapse the org
    # roster to empty and deny every non-admin sign-in, on a deployment whose database already knew
    # exactly who they were.
    state = _state(serve_engine, tmp_path, oauth=_FakeOAuth("alice"))
    state.binding = replace(state.binding, config=tmp_path / "gone.yaml")  # never written
    _payload, status, sid = _sign_in(state)
    assert status == 200 and sid is not None
    assert state.repository is not None
    assert state.repository.user_org("alice") == "acme"


def test_an_unreadable_database_is_a_5xx_not_a_denial(
    serve_engine: Callable[..., Engine], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # `orgs_from_db` propagates where `load_serve_config_file` fails closed, so an outage of ours
    # answers with an error naming our store rather than telling a legitimate user they don't belong
    # — a 403 would send them to their GitHub admin for a fault only ours can fix.
    class _BrokenRepository:
        def __getattr__(self, name: str) -> Any:
            raise OSError("database is down")

    state = _state(serve_engine, tmp_path, oauth=_FakeOAuth("alice"))
    state.repository = _BrokenRepository()
    with caplog.at_level(logging.WARNING):
        payload, status, sid = _sign_in(state)
    assert status == 503 and sid is None
    assert "unavailable" in payload["error"]
    record = next(
        r for r in caplog.records if getattr(r, "event", None) == "oauth.store_unavailable"
    )
    # The exception type, never its message: a driver's error text can carry the database URL.
    assert "OSError" in record.getMessage()
    assert "database is down" not in record.getMessage()
    # And under no other name: an alert on `oauth.denied` at WARNING reports total admin lockout,
    # so an outage of ours reaching it would page the operator with the wrong diagnosis.
    assert not any(getattr(r, "event", None) == "oauth.denied" for r in caplog.records)


def test_a_membershipless_orgs_table_is_the_operator_actionable_denial(
    serve_engine: Callable[..., Engine], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The three config-shaped causes collapse into one on this source, and it keeps the WARNING the
    # config-shaped ones had: a roster nobody belongs to is the operator's problem, not the user's.
    # The second sign-in is the point: the first one's `ensure_org` leaves a passive `default` row
    # behind, so a WARNING keyed on "the table is empty" would go quiet from here on while the
    # deployment still admits nobody but admin-Team members.
    state = _state(
        serve_engine,
        tmp_path,
        body="targets:\n  checkout: { bundleId: com.x }\n",  # no orgs: block, so nothing to seed
        oauth=_FakeOAuth("mallory", teams=["ops-gh/root"]),
        admin_teams=["ops-gh/root"],
    )
    for _ in range(2):
        caplog.clear()
        with caplog.at_level(logging.INFO):
            _payload, status, _sid = _sign_in(state)
        assert status == 200  # the admin-Team bypass, not the roster, admitted them
        record = next(r for r in caplog.records if getattr(r, "event", None) == "oauth.login")
        assert "no org in the orgs table declares any membership yet" in record.getMessage()
        assert "serve config" not in record.getMessage()  # no file decides this deployment's roster
        assert record.levelno == logging.WARNING
    assert state.repository is not None
    assert state.repository.get_org("default") is not None  # the row the first sign-in left


def test_a_database_less_deployment_still_reads_the_orgs_block(tmp_path: Path) -> None:
    # Unchanged for the deployment shape this item does not touch: no repository, so the `orgs:`
    # block still gates sign-in exactly as BE-0313 left it.
    state = ServeState(
        runs_dir=tmp_path / "runs",
        config=_config(tmp_path),
        auth=SessionManager(oauth=_FakeOAuth("alice")),
    )
    assert _sign_in(state)[1] == 200
    state.auth.oauth = _FakeOAuth("stranger", orgs=["unrelated-gh"])
    assert _sign_in(state)[1] == 403


# --- unit 4: a target's identity is (org, target) ---------------------------------------------


def test_two_orgs_may_each_claim_a_target_of_the_same_name(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # Before this item, config order awarded `checkout` to `acme` and forbade `globex` every
    # operation on it — while still listing it for `globex`, so the symptom read as a permissions
    # bug rather than the name clash it is.
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    for login, org in (("alice", "acme"), ("bob", "globex")):
        state.repository.upsert_user(login, org_id=org, github_login=login, email=f"{login}@x")
        assert _target_forbidden(state, org, "checkout") is False
        assert [t["name"] for t in ops.list_targets_payload(state, actor=login)[0]] == ["checkout"]


def test_the_default_org_keeps_every_unclaimed_target(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # Asked through `targets_for_org`, not by reading an `orgs:` entry: ownership is not symmetrical
    # — `default` owns whatever no entry claims, through a fallback keyed on its literal slug, and a
    # deployment typically declares no `default` entry at all. Reading the entry would forbid
    # `default` every target it reaches today.
    state = _state(serve_engine, tmp_path)
    assert _target_forbidden(state, "default", "spare") is False
    assert _target_forbidden(state, "default", "checkout") is True
    assert _target_forbidden(state, "acme", "spare") is True


def test_an_org_literally_named_default_owns_nothing_it_declares(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # `targets_for_org` decides `default` by its literal slug before it ever looks up an entry, so a
    # deployment that declares an org *named* `default` gets the unclaimed-target fallback and not
    # the targets its own entry lists. Resolving through that function therefore forbids such an org
    # a target it declares, where the retired `org_for_target` allowed it — a change that only makes
    # the two agree: `targets_for_org` already refused to list the target either way, so the old
    # pairing showed an empty list and authorized a target that was not in it.
    state = _state(
        serve_engine,
        tmp_path,
        body=(
            "targets:\n  checkout: { bundleId: com.x }\n  spare: { bundleId: com.y }\n"
            "orgs:\n  default:\n    members: [alice]\n    targets: [checkout]\n"
        ),
    )
    assert _target_forbidden(state, "default", "checkout") is True
    # Its unclaimed-target fallback is untouched: `spare` is named by no entry, so `default` keeps it.
    assert _target_forbidden(state, "default", "spare") is False
    assert [t["name"] for t in ops.list_targets_payload(state, actor=None)[0]] == ["spare"]


def test_an_uploaded_bundle_belongs_to_the_org_that_bound_it(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # The failure this closes, reported from a real deployment: a bundle uploaded as `sansaninc`
    # whose `orgs:` claims its one target for `sansaninc` left the uploader with an *empty* target
    # list, because the reader's own org was not the one the file named and `targets_for_org` then
    # gave them the targets nobody claimed — none. The bundle was uploaded *as* an org; that is who
    # owns it, and the file's `orgs:` block decides nothing (BE-0375).
    import hashlib
    import io
    import zipfile

    body = (
        "targets:\n  docs: { baseUrl: 'https://example.test/', backend: [web] }\n"
        "orgs:\n  sansaninc:\n    githubOrgs: [sansaninc]\n    targets: [docs]\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bajutsu.config.yaml", body)
    blob = buf.getvalue()
    zip_path = tmp_path / "bundle.zip"
    zip_path.write_bytes(blob)

    state = _state(serve_engine, tmp_path, uploads_dir=tmp_path / "uploads")
    assert state.repository is not None
    state.repository.upsert_user("kazu", org_id="acme", github_login="kazu", email="k@x")
    _, status = ops.bind_upload_config(
        state, zip_path, "bundle.zip", sha256=hashlib.sha256(blob).hexdigest(), actor="kazu"
    )
    assert status == 200
    # `acme` bound it, so `acme` owns its target — even though the file names `sansaninc`.
    assert state.binding.org == "acme"
    assert state.targets_for("acme") == ["docs"]
    assert [t["name"] for t in ops.list_targets_payload(state, actor="kazu")[0]] == ["docs"]
    # And nobody else does, including the org the file claims it for and the fallback.
    assert state.targets_for("sansaninc") == []
    assert state.targets_for("default") == []
    assert _target_forbidden(state, "globex", "docs") is True


def test_a_git_bound_config_belongs_to_the_org_that_bound_it(
    serve_engine: Callable[..., Engine], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same rule for a Git source: an arbitrary repository and ref is no more the deployment's own
    # content than an uploaded zip, and BE-0121 already says so of the same file's `build:`.
    from bajutsu.common.config_source import Materialized

    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    state.repository.upsert_user("kazu", org_id="acme", github_login="kazu", email="k@x")
    checkout = tmp_path / "gitsrc"
    checkout.mkdir()
    git_config = checkout / "bajutsu.config.yaml"
    git_config.write_text(
        "targets:\n  docs: { baseUrl: 'https://example.test/', backend: [web] }\n"
        "orgs:\n  someone-else:\n    targets: [docs]\n",
        encoding="utf-8",
    )
    # Patched by dotted path rather than through a second import of a module this file already
    # imports a name from: `bind_git_config` resolves `materialize` in its own module globals, so the
    # target has to be that module's attribute either way, and the string form says so without the
    # two-import shape an analyzer reads as redundant.
    monkeypatch.setattr(
        "bajutsu.serve.operations.config.materialize",
        lambda spec, **kw: Materialized(git_config, checkout, "sha"),
    )
    assert ops.bind_git_config(state, "github:acme/repo@main", actor="kazu")[1] == 200
    assert state.binding.org == "acme"
    assert state.targets_for("acme") == ["docs"]
    assert state.targets_for("someone-else") == []


def test_the_launch_config_still_partitions_targets_by_its_orgs_block(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # The operator's own launch configuration is unchanged: it is the multi-tenant deployment shape
    # BE-0015 defines, written by hand, so its `orgs:` block keeps deciding who owns what.
    state = _state(serve_engine, tmp_path)
    assert state.binding.org is None
    assert state.targets_for("acme") == ["checkout"]
    assert state.targets_for("globex") == ["checkout"]  # both claim it (BE-0375 unit 4)
    assert state.targets_for("default") == ["spare"]  # the one neither claims


def test_rebinding_the_launch_config_drops_the_previous_owner(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # A bind replaces the whole binding, so a source that names no owner cannot inherit the last
    # bind's org — the same "a forgotten call must fail safe, not silently persist" shape the seed
    # call sites were bitten by twice, now unrepresentable rather than merely handled (BE-0393).
    state = _state(serve_engine, tmp_path, root=tmp_path)
    state.binding = replace(state.binding, org="acme")
    (tmp_path / "next.config.yaml").write_text(_COLLIDING_YAML, encoding="utf-8")
    assert ops.bind_config(state, "next.config.yaml")[1] == 200
    assert state.binding.org is None


# --- unit 5: the admin API ---------------------------------------------------------------------


def test_every_org_endpoint_is_admin_only() -> None:
    # An org's membership decides who else can sign in and write, so no verb here is below admin —
    # the list included, since it discloses one tenant's roster to another.
    assert ops.required_role("GET", "/api/orgs") == "admin"
    assert ops.required_role("POST", "/api/orgs") == "admin"
    assert ops.required_role("POST", "/api/orgs/acme/membership") == "admin"
    assert ops.required_role("DELETE", "/api/orgs/acme") == "admin"


def test_create_then_re_member_an_org_and_audit_both(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    admin = _admin(state)

    payload, status = ops.create_org(state, {"slug": "initech", "name": "Initech"}, actor=admin)
    assert status == 200 and payload["slug"] == "initech"
    # A fresh org admits nobody: membership is never inherited from anywhere.
    created = next(o for o in ops.list_orgs_view(state, actor=admin)[0] if o["slug"] == "initech")
    assert created == {
        "slug": "initech",
        "name": "Initech",
        "members": [],
        "githubOrgs": [],
        "githubTeams": [],
        "editorTeams": [],
        "allowedRepositories": [],  # nor any pipeline (BE-0414 unit 2)
        "reserved": False,
    }

    _payload, status = ops.update_org_membership(
        state,
        "initech",
        {
            "members": ["peter"],
            "githubOrgs": ["initech-gh"],
            "githubTeams": ["initech-gh/qa"],
            "editorTeams": ["initech-gh/leads"],
        },
        actor=admin,
    )
    assert status == 200
    orgs = orgs_from_db(state.repository)
    assert orgs["initech"].members == ["peter"]
    assert orgs["initech"].github_teams == ["initech-gh/qa"]
    assert orgs["initech"].editor_teams == ["initech-gh/leads"]
    # The roster an admin sets is a sign-in roster: a member of either Team belongs to `initech`
    # from the next sign-in, without a redeploy — the point of moving membership into the database.
    assert org_for_identity(orgs, "milton", [], ["initech-gh/qa"]) == "initech"
    assert org_for_identity(orgs, "bill", [], ["initech-gh/leads"]) == "initech"

    assert _audit_actions(state.repository) == ["org.create", "org.membership.update"]

    # A caller still on the retired singular field is refused, not quietly obeyed: `editorTeams` is
    # absent from that body, which the shared list validator reads as an empty list, so obeying it
    # would strip this org's write access and answer 200. The roster is left as it was.
    payload, status = ops.update_org_membership(
        state,
        "initech",
        {"members": ["peter"], "editorTeam": "initech-gh/leads"},
        actor=admin,
    )
    assert status == 400 and "editorTeam is retired" in payload["error"]
    assert orgs_from_db(state.repository)["initech"].editor_teams == ["initech-gh/leads"]


# --- BE-0414 unit 2: the machine roster travels the same paths as the human one ---------------


def test_the_machine_roster_round_trips_through_the_database(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """A DB-backed deployment reads its org model from `orgs_from_db`, and the exchange requires a
    database — so `allowedRepositories` is dead unless it survives the round trip."""
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    admin = _admin(state)
    ops.create_org(state, {"slug": "initech", "name": "Initech"}, actor=admin)

    _payload, status = ops.update_org_membership(
        state,
        "initech",
        {
            "members": ["peter"],
            "allowedRepositories": [
                "initech/app",
                {"repository": "initech/web", "environment": "production"},
            ],
        },
        actor=admin,
    )
    assert status == 200
    org = orgs_from_db(state.repository)["initech"]
    assert [e.repository for e in org.allowed_repositories] == ["initech/app", "initech/web"]
    assert org.allowed_repositories[0].environment is None
    assert org.allowed_repositories[1].environment == "production"


def test_omitting_the_machine_roster_leaves_it_alone(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """A client predating the field would otherwise revoke every pipeline's access and be told the
    update succeeded — the silent strip `editorTeam`'s loud refusal exists to prevent."""
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    admin = _admin(state)
    ops.create_org(state, {"slug": "initech", "name": "Initech"}, actor=admin)
    ops.update_org_membership(
        state, "initech", {"allowedRepositories": ["initech/app"]}, actor=admin
    )

    _payload, status = ops.update_org_membership(
        state, "initech", {"members": ["peter"], "editorTeams": ["initech-gh/leads"]}, actor=admin
    )
    assert status == 200
    org = orgs_from_db(state.repository)["initech"]
    assert org.members == ["peter"]  # the human roster still replaces as one unit
    assert [e.repository for e in org.allowed_repositories] == ["initech/app"]

    # An explicit empty list is how an admin actually clears it.
    ops.update_org_membership(state, "initech", {"allowedRepositories": []}, actor=admin)
    assert orgs_from_db(state.repository)["initech"].allowed_repositories == []


def test_a_malformed_machine_roster_is_refused_on_write(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    admin = _admin(state)
    ops.create_org(state, {"slug": "initech", "name": "Initech"}, actor=admin)

    for bad in (["not-a-repo"], "initech/app", [{"repository": "initech/app", "nope": 1}]):
        payload, status = ops.update_org_membership(
            state, "initech", {"allowedRepositories": bad}, actor=admin
        )
        assert status == 400, bad
        assert "allowedRepositories" in payload["error"]
    assert orgs_from_db(state.repository)["initech"].allowed_repositories == []


def test_the_machine_roster_is_audited_and_listed(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """Which repositories may act as a tenant is the machine half of "who could sign in as this
    tenant, from when" — so it belongs in the audit entry, and in the list a form fills from."""
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    admin = _admin(state)
    ops.create_org(state, {"slug": "initech", "name": "Initech"}, actor=admin)
    ops.update_org_membership(
        state, "initech", {"allowedRepositories": ["initech/app"]}, actor=admin
    )

    listed = next(o for o in ops.list_orgs_view(state, actor=admin)[0] if o["slug"] == "initech")
    assert listed["allowedRepositories"] == [
        {"repository": "initech/app", "environment": None, "ref": None, "workflowRef": None}
    ]
    detail = _audit_detail(state.repository, "org.membership.update")
    assert detail["allowedRepositories"][0]["repository"] == "initech/app"


def test_the_machine_roster_seeds_from_the_orgs_block(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """A config-only org whose sole roster is `allowedRepositories` converts like any other, rather
    than being skipped as "declares nothing" and leaving its pipelines locked out."""
    body = "targets: {}\norgs:\n  ci-only:\n    allowedRepositories: [acme/app]\n"
    state = _state(serve_engine, tmp_path, body=body)
    assert state.repository is not None
    org = orgs_from_db(state.repository)["ci-only"]
    assert [e.repository for e in org.allowed_repositories] == ["acme/app"]


def test_creating_the_default_org_is_refused(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # `default` is where an unmatched sign-in lands — the admin-Team bypass's own landing place —
    # and `targets_for_org` decides it by the literal slug before reading any entry. A real tenant
    # created here would silently take that namespace, and `delete_org` refuses the slug, so nothing
    # could undo it through the API. Refused at creation instead, which is the reversible end.
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    payload, status = ops.create_org(state, {"slug": "default"}, actor=_admin(state))
    assert status == 409
    assert "reserved" in payload["error"]
    assert state.repository.get_org("default", include_deleted=True) is None


def test_retiring_an_org_revokes_its_members_live_sessions(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # A soft delete alone reaches only the *next* sign-in: `users.org_id` still names the retired
    # slug, so a cookie issued before it would keep acting as that tenant until it expired. Retiring
    # used to mean a config edit plus a redeploy, and the restart dropped every session as a side
    # effect; an in-process admin action has to do it deliberately (BE-0375).
    state = _state(serve_engine, tmp_path, oauth=_FakeOAuth("bob"))
    assert state.repository is not None
    _payload, status, sid = _sign_in(state)  # bob belongs to globex
    assert status == 200 and sid is not None
    assert state.auth.valid_session(sid)
    other = state.auth.issue_session(identity="alice")  # a different org: must survive
    anonymous = state.auth.issue_session()  # a shared-token login belongs to no org

    payload, status = ops.delete_org(state, "globex", actor=_admin(state))
    assert status == 200 and payload["sessionsRevoked"] == 1
    assert not state.auth.valid_session(sid)
    assert state.auth.valid_session(other) and state.auth.valid_session(anonymous)


def test_the_default_orgs_membership_is_not_editable(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # The reservation has to hold on all three verbs, not two. A bypass sign-in's `ensure_org`
    # creates a live `default` row, so the Orgs page lists it and its Membership form could reach
    # it — and a roster there makes `identity_matches_org` place those logins in the fallback, which
    # is the very thing refusing to create it prevents.
    state = _state(
        serve_engine,
        tmp_path,
        oauth=_FakeOAuth("root", teams=["ops-gh/admins"]),
        admin_teams=["ops-gh/admins"],
    )
    assert state.repository is not None
    assert _sign_in(state)[1] == 200  # the bypass admits root and lands them in `default`
    assert state.repository.user_org("root") == "default"
    payload, status = ops.update_org_membership(
        state, "default", {"members": ["mallory"]}, actor="root"
    )
    assert status == 409 and "fallback" in payload["error"]
    assert orgs_from_db(state.repository)["default"].members == []
    # Listed, not hidden: the admin is sitting in it, so the page marks it instead of concealing it.
    listed = {o["slug"]: o for o in ops.list_orgs_view(state, actor="root")[0]}
    assert listed["default"]["reserved"] is True
    assert listed["acme"]["reserved"] is False


def test_creating_an_org_rejects_a_taken_slug_and_a_slug_that_is_not_a_safe_id(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    state = _state(serve_engine, tmp_path)
    assert ops.create_org(state, {"slug": "acme"}, actor="root")[1] == 409  # seeded above
    for bad in ("", "ACME", "a/b", "..", "a b", "x" * 65):
        assert ops.create_org(state, {"slug": bad}, actor="root")[1] == 400, bad


def test_deleting_an_org_retires_it_without_removing_its_row(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # A soft delete: `users`, `runs`, `secrets`, `provider_settings`, and `audit_log` all still hold
    # foreign keys on this id — including the deletion's own audit entry.
    state = _state(serve_engine, tmp_path, oauth=_FakeOAuth("bob"))
    assert state.repository is not None
    admin = _admin(state)
    assert _sign_in(state)[1] == 200  # bob belongs to globex while it is live

    assert ops.delete_org(state, "globex", actor=admin)[1] == 200
    assert [o["slug"] for o in ops.list_orgs_view(state, actor=admin)[0]] == ["acme"]
    assert state.repository.get_org("globex") is None
    assert state.repository.get_org("globex", include_deleted=True) is not None
    # Retired means "admits nobody", so bob's next sign-in is turned away rather than resolved.
    assert _sign_in(state)[1] == 403
    # ...and the slug stays taken, so nothing silently reactivates it.
    assert ops.create_org(state, {"slug": "globex"}, actor=admin)[1] == 409


def test_deleting_the_default_org_is_refused(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    # `default` is the fallback an unmatched bypass sign-in resolves to regardless of table state,
    # so retiring it would only leave a dead org users keep landing on.
    state.repository.ensure_org("default", slug="default", name="default")
    assert ops.delete_org(state, "default", actor="root")[1] == 409
    assert ops.delete_org(state, "nosuch", actor="root")[1] == 404


def test_the_org_endpoints_need_a_database(tmp_path: Path) -> None:
    # A database-less serve is single-user by construction: no tenant boundary to administer, and
    # nowhere to keep what an admin would set.
    state = ServeState(runs_dir=tmp_path / "runs", config=_config(tmp_path))
    assert ops.list_orgs_view(state)[1] == 400
    assert ops.create_org(state, {"slug": "initech"})[1] == 400
    assert ops.update_org_membership(state, "acme", {})[1] == 400
    assert ops.delete_org(state, "acme")[1] == 400


def test_the_config_read_reports_the_callers_own_org(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # Every tab is silently scoped to the caller's org — runs, evidence, secrets, the project list —
    # so the boot read names it, and the header shows it. The caller's own login and org are not a
    # disclosure: they presented the one and the other follows from it. The roster stays behind the
    # admin-only `GET /api/orgs`.
    state = _state(serve_engine, tmp_path, oauth=_FakeOAuth("bob"))
    assert _sign_in(state)[1] == 200  # bob belongs to globex
    payload, status = ops.config_info(state, actor="bob")
    assert status == 200
    assert (payload["actor"], payload["org"]) == ("bob", "globex")


def test_the_config_read_names_no_org_without_an_identity(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # A local or shared-token session has no identity, and `org_of` would answer `default` for
    # everyone. Reporting that would put a tenant badge on a single-user serve that has no tenants,
    # so both fields stay None and the header keeps the shape it had.
    state = _state(serve_engine, tmp_path)
    payload, _status = ops.config_info(state)
    assert payload["actor"] is None and payload["org"] is None


# --- unit 6: seed once, then the database owns it ----------------------------------------------


def test_the_backfill_seeds_once_and_a_later_config_edit_cannot_undo_it(
    serve_engine: Callable[..., Engine], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    ops.update_org_membership(state, "acme", {"members": ["zoe"]}, actor=_admin(state))

    # An operator edits `orgs:` and rebinds. The row is already past cutover, so the edit does not
    # reach it — and the entry is reported so they learn the file no longer decides this.
    assert state.binding.config is not None
    state.binding.config.write_text(
        _COLLIDING_YAML.replace("members: [alice]", "members: [alice, mallory]"), encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING):
        seed_orgs_from_bound_config(state)
    assert orgs_from_db(state.repository)["acme"].members == ["zoe"]
    record = next(
        r for r in caplog.records if getattr(r, "event", None) == "org.membership.ignored"
    )
    assert "acme" in record.getMessage()
    assert log_field(record, "check") == "orgs_membership_ignored"


def test_seeding_reports_nothing_for_an_entry_that_only_declares_targets(
    serve_engine: Callable[..., Engine], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Target ownership stays in configuration, so a `targets:`-only entry is the expected end state
    # after the cutover. Warning on a merely non-empty `orgs:` block would fire forever on a
    # correctly configured deployment, or push an operator to empty it and lose that ownership.
    state = _state(serve_engine, tmp_path)
    assert state.binding.config is not None
    state.binding.config.write_text(
        "targets:\n  checkout: { bundleId: com.x }\n"
        "orgs:\n  acme:\n    targets: [checkout]\n  globex:\n    targets: [checkout]\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        seed_orgs_from_bound_config(state)
    assert not [r for r in caplog.records if getattr(r, "event", None) == "org.membership.ignored"]


def test_a_targets_only_entry_is_left_unseeded_so_a_restored_roster_still_seeds(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # The docs tell an operator to pare an entry down to `targets:` and say nothing about when, so
    # doing it before the first seeded start must stay recoverable. Seeding such an entry would
    # spend its cutover marker on an empty roster and lock the org at "admits nobody" for good, with
    # `org.membership.ignored` deliberately silent about it. No row is created either: target
    # ownership resolves from the configuration, never from the table.
    state = _state(
        serve_engine,
        tmp_path,
        body="targets:\n  checkout: { bundleId: com.x }\norgs:\n  acme:\n    targets: [checkout]\n",
    )
    assert state.repository is not None
    assert orgs_from_db(state.repository) == {}

    assert state.binding.config is not None
    state.binding.config.write_text(
        "targets:\n  checkout: { bundleId: com.x }\n"
        "orgs:\n  acme:\n    members: [alice]\n    targets: [checkout]\n",
        encoding="utf-8",
    )
    seed_orgs_from_bound_config(state)
    assert orgs_from_db(state.repository)["acme"].members == ["alice"]


def test_seeding_is_skipped_once_the_table_holds_any_org(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # One boot converts a configuration-only deployment; after that the database is the sole author
    # of its own roster (BE-0375). A later `orgs:` entry — an edit, or a restart carrying one — must
    # not add a tenant behind an admin's back, so a table holding any org at all stops the seed.
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    assert state.binding.config is not None
    state.binding.config.write_text(
        _COLLIDING_YAML + "  initech:\n    members: [peter]\n", encoding="utf-8"
    )
    seed_orgs_from_bound_config(state)
    assert "initech" not in orgs_from_db(state.repository)


def test_seeding_is_skipped_when_every_org_has_been_retired(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # A retired org still counts as a roster the database authored: a table emptied by soft-deleting
    # every tenant must not read as "never converted" and let the config repopulate it on the next
    # restart, which would revive by another name what an admin deliberately retired.
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    admin = _admin(state)
    for slug in ("acme", "globex"):
        assert ops.delete_org(state, slug, actor=admin)[1] == 200
    assert orgs_from_db(state.repository) == {}
    seed_orgs_from_bound_config(state)
    assert orgs_from_db(state.repository) == {}


def test_seeding_still_reports_config_entries_that_declare_membership(
    serve_engine: Callable[..., Engine], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The skip must not silence the operator's one signal that a field they are still editing
    # stopped being read. Past the conversion every entry declaring membership is reported, which is
    # strictly more than the pre-conversion case reported (only the entries that failed to seed).
    state = _state(serve_engine, tmp_path)
    with caplog.at_level(logging.WARNING):
        seed_orgs_from_bound_config(state)
    record = next(
        r for r in caplog.records if getattr(r, "event", None) == "org.membership.ignored"
    )
    assert "acme" in record.getMessage() and "globex" in record.getMessage()
    assert log_field(record, "check") == "orgs_membership_ignored"


# Each test below drives one *real* API bind operation. None of them may seed: such a bind accepts a
# configuration whose content the deployment does not own — BE-0121 says as much of the same file's
# `build:` — and a seeded row outlives the bind, so rebinding away would no longer revoke the grant
# the way re-reading the file on every sign-in used to (BE-0375). Only `serve()`'s launch config
# seeds, and only into an empty table.


def _unconverted(serve_engine: Callable[..., Engine], tmp_path: Path, **extra: Any) -> ServeState:
    """A database-backed serve whose `orgs` table is still empty, so nothing but the bind could
    create a row — otherwise these tests would pass on the empty-table guard rather than on the
    absence of a seed call."""
    state = _state(serve_engine, tmp_path, body="targets: {}\n", seed=False, **extra)
    assert state.repository is not None
    assert state.repository.list_orgs(include_deleted=True) == []
    return state


def test_binding_a_config_from_the_file_browser_does_not_seed(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    state = _unconverted(serve_engine, tmp_path, root=tmp_path)
    assert state.repository is not None
    (tmp_path / "next.config.yaml").write_text(_ORGS_ONLY_YAML, encoding="utf-8")
    _, status = ops.bind_config(state, "next.config.yaml")
    assert status == 200
    assert orgs_from_db(state.repository) == {}


def test_binding_a_git_config_does_not_seed(
    serve_engine: Callable[..., Engine], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bajutsu.common.config_source import Materialized

    state = _unconverted(serve_engine, tmp_path)
    assert state.repository is not None
    checkout = tmp_path / "gitsrc"
    checkout.mkdir()
    git_config = checkout / "bajutsu.config.yaml"
    git_config.write_text(_ORGS_ONLY_YAML, encoding="utf-8")
    # Patched by dotted path rather than through a second import of a module this file already
    # imports a name from: `bind_git_config` resolves `materialize` in its own module globals, so the
    # target has to be that module's attribute either way, and the string form says so without the
    # two-import shape an analyzer reads as redundant.
    monkeypatch.setattr(
        "bajutsu.serve.operations.config.materialize",
        lambda spec, **kw: Materialized(git_config, checkout, "sha"),
    )
    _, status = ops.bind_git_config(state, "github:acme/repo@main")
    assert status == 200
    # The scenario this closes: a config fetched from an arbitrary repo and ref declaring
    # `initech: {members: [peter]}` would otherwise grant peter a permanent sign-in that rebinding
    # away could not revoke.
    assert orgs_from_db(state.repository) == {}


def test_binding_an_uploaded_zip_does_not_seed(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    import hashlib
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bajutsu.config.yaml", _ORGS_ONLY_YAML)
    blob = buf.getvalue()
    zip_path = tmp_path / "bundle.zip"
    zip_path.write_bytes(blob)

    state = _unconverted(serve_engine, tmp_path, uploads_dir=tmp_path / "uploads")
    assert state.repository is not None
    _, status = ops.bind_upload_config(
        state, zip_path, "bundle.zip", sha256=hashlib.sha256(blob).hexdigest()
    )
    assert status == 200
    assert orgs_from_db(state.repository) == {}


def test_restoring_an_uploaded_config_does_not_seed(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    from _shared import FakeObjectStore

    from bajutsu.serve.operations.upload import restore_uploaded_config

    sha256 = "c" * 64
    cached = tmp_path / "uploads" / "acme" / sha256  # an org-scoped cache hit: nothing to fetch
    cached.mkdir(parents=True)
    (cached / "bajutsu.config.yaml").write_text(_ORGS_ONLY_YAML, encoding="utf-8")
    state = _unconverted(
        serve_engine,
        tmp_path,
        uploads_dir=tmp_path / "uploads",
        object_store=FakeObjectStore(),  # without one, the operation returns None before binding
    )
    assert state.repository is not None
    result = restore_uploaded_config(state, {"kind": "upload", "sha256": sha256}, org="acme")
    assert result is not None and result[1] == 200
    assert orgs_from_db(state.repository) == {}


def test_an_org_created_through_the_api_is_never_seeded_over(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # Marked seeded at creation, so a later `orgs:` entry for the same slug cannot overwrite the
    # membership an admin set — the overwrite the per-row marker exists to prevent.
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    admin = _admin(state)
    ops.create_org(state, {"slug": "initech"}, actor=admin)
    ops.update_org_membership(state, "initech", {"members": ["peter"]}, actor=admin)
    assert state.binding.config is not None
    state.binding.config.write_text(
        _COLLIDING_YAML + "  initech:\n    members: [mallory]\n", encoding="utf-8"
    )
    seed_orgs_from_bound_config(state)
    assert orgs_from_db(state.repository)["initech"].members == ["peter"]


def test_a_membership_edit_on_an_unseeded_row_is_never_seeded_over(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # An admin can reach a row the backfill never marked — one `ensure_org` created at sign-in, one
    # predating the migration, or one left unseeded because the config failed to load at boot. The
    # edit itself is the cutover for that row, so a later `orgs:` entry for the same slug must not
    # replace what the admin set.
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    state.repository.ensure_org("legacy", slug="legacy", name="legacy")
    edit = ops.update_org_membership(state, "legacy", {"members": ["zoe"]}, actor=_admin(state))
    assert edit[1] == 200
    assert state.binding.config is not None
    state.binding.config.write_text(
        _COLLIDING_YAML + "  legacy:\n    members: [mallory]\n", encoding="utf-8"
    )
    seed_orgs_from_bound_config(state)
    assert orgs_from_db(state.repository)["legacy"].members == ["zoe"]


def test_a_sign_in_after_the_cutover_never_clears_membership(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # `ensure_org` stays the idempotent create it always was: it runs on every sign-in with no
    # membership to pass, so widening it into a create-or-update would empty an admin's edit on the
    # next login — the same overwrite arriving through a different door.
    state = _state(serve_engine, tmp_path, oauth=_FakeOAuth("alice"))
    assert state.repository is not None
    assert _sign_in(state)[1] == 200
    assert orgs_from_db(state.repository)["acme"].members == ["alice"]


def test_a_soft_deleted_org_is_never_reseeded(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # Retired, not merely unseeded: a backfill that revived a deleted org would undo an admin action
    # at the next restart, with nothing in the configuration file saying so.
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    assert ops.delete_org(state, "globex", actor=_admin(state))[1] == 200
    seed_orgs_from_bound_config(state)
    assert state.repository.get_org("globex") is None


def test_seeding_survives_an_unreadable_database(
    serve_engine: Callable[..., Engine], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Seeding is a convergence step that re-runs at the next startup or rebind, so a database blip
    # must not fail a bind that otherwise succeeded. Reported loudly, never swallowed silently.
    class _BrokenRepository:
        def __getattr__(self, name: str) -> Any:
            raise OSError("database is down")

    state = _state(serve_engine, tmp_path)
    state.repository = _BrokenRepository()
    with caplog.at_level(logging.WARNING):
        seed_orgs_from_bound_config(state)
    record = next(r for r in caplog.records if getattr(r, "event", None) == "org.seed.failed")
    assert log_field(record, "check") == "orgs_seed_failed"


# --- unit 7: the admin-Team bypass answers the empty-table case --------------------------------


def test_the_admin_team_bypass_admits_a_sign_in_against_an_empty_table_and_creates_the_first_org(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    # The bootstrap this item leans on rather than re-solving: with no `Org` row at all, the one
    # piece of tenancy data deliberately left in the environment is what lets anyone in to create
    # the first one. No chicken-and-egg.
    state = _state(
        serve_engine,
        tmp_path,
        body="targets:\n  checkout: { bundleId: com.x }\n",
        oauth=_FakeOAuth("root", teams=["ops-gh/root"]),
        admin_teams=["ops-gh/root"],
    )
    assert state.repository is not None
    assert state.repository.list_orgs() == []
    assert _sign_in(state)[1] == 200
    assert state.repository.user_role("root") == "admin"

    assert ops.create_org(state, {"slug": "initech"}, actor="root")[1] == 200
    ops.update_org_membership(state, "initech", {"members": ["peter"]}, actor="root")
    state.auth.oauth = _FakeOAuth("peter")
    assert _sign_in(state)[1] == 200
    assert state.repository.user_org("peter") == "initech"


def _audit_actions(repository: Repository) -> list[str]:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from bajutsu.serve.server.models import AuditLog

    engine = repository._engine  # type: ignore[attr-defined]
    with Session(engine) as session:
        return [row.action for row in session.scalars(select(AuditLog).order_by(AuditLog.action))]


def _audit_detail(repository: Repository, action: str) -> dict[str, Any]:
    """The detail payload of the one entry recording *action*."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from bajutsu.serve.server.models import AuditLog

    engine = repository._engine  # type: ignore[attr-defined]
    with Session(engine) as session:
        row = session.scalars(select(AuditLog).where(AuditLog.action == action)).one()
        return dict(row.detail)


def test_soft_delete_stamps_the_moment_it_happened(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    at = datetime(2026, 8, 14, tzinfo=UTC)
    assert state.repository.soft_delete_org("globex", at=at) is True
    # Already retired: a second delete is a clean False, not a re-stamp of the original moment.
    assert state.repository.soft_delete_org("globex", at=datetime.now(UTC)) is False
    retired = state.repository.get_org("globex", include_deleted=True)
    assert retired is not None and retired.deleted_at is not None


def test_one_unreadable_machine_roster_entry_does_not_lock_out_every_human(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """`orgs_from_db` builds the model the *human* sign-in path resolves against, so a malformed
    stored entry that raised would turn every person's login into a denial — and `authz`'s own
    `except Exception` would report it as a database outage, hiding the real cause."""
    from sqlalchemy.orm import Session

    from bajutsu.serve.server.models import Org

    state = _state(serve_engine, tmp_path)
    assert state.repository is not None
    admin = _admin(state)
    ops.create_org(state, {"slug": "initech", "name": "Initech"}, actor=admin)
    ops.update_org_membership(
        state,
        "initech",
        {"members": ["peter"], "allowedRepositories": ["initech/app"]},
        actor=admin,
    )
    # A direct write nothing validated — a rollback, a future writer, an operator with psql.
    engine = state.repository._engine  # type: ignore[attr-defined]
    with Session(engine) as session:
        row = session.get(Org, "initech")
        assert row is not None
        row.allowed_repositories = [{"repository": "no-slash"}, "initech/app"]
        session.commit()

    orgs = orgs_from_db(state.repository)
    assert orgs["initech"].members == ["peter"]  # the human roster is untouched
    # The unreadable entry admits nobody; the readable one beside it still works.
    assert [e.repository for e in orgs["initech"].allowed_repositories] == ["initech/app"]
