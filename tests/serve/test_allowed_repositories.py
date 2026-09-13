"""Tests for an org's machine roster, `allowedRepositories` (BE-0414 unit 2).

Which pipeline may act as which tenant is decided here, on the discrete claims a verified token
carries — never on a parse of `sub`, and never on a prefix. Pure data plus exact comparisons, so
these run on the fast gate with no database, no network, and no LLM anywhere near the decision.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bajutsu.serve.oidc import WorkloadClaims
from bajutsu.serve.orgs import AllowedRepository, OrgConfig, orgs_declaring_membership, parse_orgs


def _workload(**overrides: object) -> WorkloadClaims:
    defaults: dict[str, object] = {
        "repository": "acme/app",
        "environment": None,
        "ref": "refs/heads/main",
        "workflow_ref": "acme/app/.github/workflows/e2e.yml@refs/heads/main",
    }
    return WorkloadClaims(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_a_bare_name_is_the_short_form_of_an_unbounded_entry() -> None:
    org = OrgConfig.model_validate({"allowedRepositories": ["acme/app"]})
    assert org.allowed_repositories == [AllowedRepository(repository="acme/app")]
    assert org.admits_workload(_workload())


def test_an_unlisted_repository_is_refused() -> None:
    org = OrgConfig.model_validate({"allowedRepositories": ["acme/app"]})
    assert not org.admits_workload(_workload(repository="acme/other"))


def test_an_org_listing_nothing_admits_nothing() -> None:
    assert not OrgConfig().admits_workload(_workload())


def test_a_repository_that_merely_shares_a_prefix_is_refused() -> None:
    """The classic failure this rules out: `acme/app` prefix-matches `acme/app-evil`, so anything
    weaker than exact equality would admit a repository the operator never listed."""
    org = OrgConfig.model_validate({"allowedRepositories": ["acme/app"]})
    assert not org.admits_workload(_workload(repository="acme/app-evil"))
    assert not org.admits_workload(_workload(repository="acme/ap"))
    assert not org.admits_workload(_workload(repository="notacme/app"))


def test_an_environment_bound_refuses_a_differing_and_an_absent_claim() -> None:
    org = OrgConfig.model_validate(
        {"allowedRepositories": [{"repository": "acme/app", "environment": "production"}]}
    )
    assert org.admits_workload(_workload(environment="production"))
    assert not org.admits_workload(_workload(environment="staging"))
    # `environment` is conditional — emitted only when the job references one — so a job declaring
    # none must not escape the narrowing.
    assert not org.admits_workload(_workload(environment=None))


def test_a_ref_bound_and_a_workflow_ref_bound_each_narrow_their_own_axis() -> None:
    by_ref = OrgConfig.model_validate(
        {"allowedRepositories": [{"repository": "acme/app", "ref": "refs/heads/main"}]}
    )
    assert by_ref.admits_workload(_workload())
    assert not by_ref.admits_workload(_workload(ref="refs/heads/wip"))

    by_workflow = OrgConfig.model_validate(
        {
            "allowedRepositories": [
                {
                    "repository": "acme/app",
                    "workflowRef": "acme/app/.github/workflows/e2e.yml@refs/heads/main",
                }
            ]
        }
    )
    assert by_workflow.admits_workload(_workload())
    assert not by_workflow.admits_workload(
        _workload(workflow_ref="acme/app/.github/workflows/other.yml@refs/heads/main")
    )


def test_bounds_on_one_entry_never_reach_another() -> None:
    """One org can list a repository that runs under a reviewed environment beside one that does
    not, so no second org has to exist purely to hold the unbounded entry."""
    org = OrgConfig.model_validate(
        {
            "allowedRepositories": [
                {"repository": "acme/app", "environment": "production"},
                "acme/tools",
            ]
        }
    )
    assert org.admits_workload(_workload(repository="acme/app", environment="production"))
    assert not org.admits_workload(_workload(repository="acme/app", environment=None))
    assert org.admits_workload(_workload(repository="acme/tools", environment=None))


def test_one_repository_can_be_listed_by_several_orgs() -> None:
    """A shared pipeline repository testing apps owned by different teams — the shape that forces
    the exchange request to name its org rather than letting serve infer one."""
    orgs = parse_orgs(
        {
            "acme": {"allowedRepositories": ["shared/ci"]},
            "globex": {"allowedRepositories": ["shared/ci"]},
            "initech": {"allowedRepositories": ["initech/app"]},
        }
    )
    workload = _workload(repository="shared/ci")
    assert [name for name, oc in orgs.items() if oc.admits_workload(workload)] == ["acme", "globex"]


@pytest.mark.parametrize(
    "entry",
    ["acme", "acme/", "/app", "acme/app/extra", "acme app", "", "acme /app"],
)
def test_a_malformed_name_is_refused_at_parse_time(entry: str) -> None:
    """Loudly, not silently: exact-equality matching means a malformed entry would otherwise sit in
    the config matching nothing, reading as a grant that never grants."""
    with pytest.raises(ValidationError):
        OrgConfig.model_validate({"allowedRepositories": [entry]})


def test_an_unknown_key_on_an_entry_is_refused() -> None:
    with pytest.raises(ValidationError):
        OrgConfig.model_validate(
            {"allowedRepositories": [{"repository": "acme/app", "envrionment": "production"}]}
        )


def test_the_machine_roster_alone_counts_as_a_declared_roster() -> None:
    """Or a config-only org whose sole roster is `allowedRepositories` would never be seeded into
    the database, leaving its pipelines locked out with nothing saying why."""
    orgs = parse_orgs(
        {
            "acme": {"allowedRepositories": ["acme/app"]},
            "targets-only": {"targets": ["ios"]},
        }
    )
    assert orgs_declaring_membership(orgs) == ["acme"]


def test_the_machine_roster_sits_beside_the_human_one_without_granting_a_role() -> None:
    org = OrgConfig.model_validate(
        {"members": ["alice"], "editorTeams": ["acme/qa"], "allowedRepositories": ["acme/app"]}
    )
    assert org.members == ["alice"]
    assert org.admitting_teams() == ["acme/qa"]  # a repository admits no human
    assert org.admits_workload(_workload())


def test_an_empty_bound_is_refused_rather_than_left_inert() -> None:
    """The same silently-inert grant the name check prevents, one field over: a claim that arrives
    empty reads as absent, so `environment: ""` would match nothing while looking like a
    narrowing that works."""
    for field in ("environment", "ref", "workflowRef"):
        for empty in ("", "   "):
            with pytest.raises(ValidationError):
                OrgConfig.model_validate(
                    {"allowedRepositories": [{"repository": "acme/app", field: empty}]}
                )


def test_the_repository_name_is_matched_case_insensitively() -> None:
    """GitHub cannot hold both `Acme/App` and `acme/app`, so an operator whose casing differs from
    the claim means the same repository — and every other GitHub name in this model is already
    compared case-insensitively."""
    org = OrgConfig.model_validate({"allowedRepositories": ["Acme/MyApp"]})
    assert org.admits_workload(_workload(repository="acme/myapp"))
    assert org.admits_workload(_workload(repository="Acme/MyApp"))
    assert not org.admits_workload(_workload(repository="acme/myapp2"))


def test_the_bounds_stay_case_sensitive() -> None:
    """A ref and a workflow path are not GitHub names; `refs/heads/Main` is a different ref."""
    org = OrgConfig.model_validate(
        {"allowedRepositories": [{"repository": "acme/app", "ref": "refs/heads/main"}]}
    )
    assert org.admits_workload(_workload(ref="refs/heads/main"))
    assert not org.admits_workload(_workload(ref="refs/heads/Main"))


def test_a_name_that_merely_case_folds_equal_is_refused() -> None:
    """`str.lower`, not `str.casefold`, for the reason `in_teams` already argues: full case
    folding equates names GitHub keeps distinct, so a repository whose name only *folds* equal to
    a listed one would clear the gate — a match here buys the right to act as the org."""
    org = OrgConfig.model_validate({"allowedRepositories": ["acme/file"]})
    assert org.admits_workload(_workload(repository="ACME/FILE"))  # plain ASCII case: the same
    assert not org.admits_workload(_workload(repository="acme/ﬁle"))  # U+FB01 'ﬁ' ligature
    assert not org.admits_workload(_workload(repository="acme/gruße"))
