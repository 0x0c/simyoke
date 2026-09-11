**English** · [日本語](BE-0414-ci-oidc-machine-identity-ja.md)

# BE-0414 — Authenticate a CI job to serve with a GitHub Actions OIDC token

<!-- BE-METADATA -->
| Field | Value |
|---|---|
| Proposal | [BE-0414](BE-0414-ci-oidc-machine-identity.md) |
| Author | [@paihu](https://github.com/paihu) |
| Status | **In progress** |
| Tracking issue | [Search](https://github.com/bajutsu-e2e/bajutsu/issues?q=is%3Aissue+label%3Aroadmap-tracking+in%3Atitle+"BE-0414") |
| Implementing PR | [#1986](https://github.com/bajutsu-e2e/bajutsu/pull/1986) (units 1-2) |
| Topic | Hosting the web UI |
| Related | [BE-0313](../BE-0313-github-org-team-rbac/BE-0313-github-org-team-rbac.md), [BE-0051](../BE-0051-serve-hardening-for-hosting/BE-0051-serve-hardening-for-hosting.md), [BE-0015](../BE-0015-web-ui-public-hosting/BE-0015-web-ui-public-hosting.md) |
<!-- /BE-METADATA -->

## Introduction

A hosted `bajutsu serve` authenticates two kinds of caller today. A human signs in through GitHub
OAuth (Open Authorization) and gets an identity, which the role gate then checks per endpoint
([BE-0313](../BE-0313-github-org-team-rbac/BE-0313-github-org-team-rbac.md)). A worker presents the
shared token ([BE-0051](../BE-0051-serve-hardening-for-hosting/BE-0051-serve-hardening-for-hosting.md))
and gets no identity at all.

A continuous-integration (CI) job is neither. It cannot complete a browser sign-in, and on a
deployment with OAuth configured the shared token no longer reaches any endpoint outside worker
traffic. This item gives such a job an identity of its own: it presents the OpenID Connect (OIDC)
token GitHub Actions issues to a workflow once, to a dedicated exchange endpoint. serve verifies
that token there and mints a short-lived machine session in its place, and the repository named in
the token's claims decides which org that session acts as. Access is scoped to what a machine
needs — never to a human role.

## Motivation

BE-0313 replaced serve's login allowlist with GitHub's own organization and Team membership, and
narrowed the shared token to worker traffic in the same change. That was the right move for people:
an identity the role gate can check beats a secret that grants everything. It left machines with no
replacement, and `docs/self-hosting.md` records the consequence plainly — a deployment that scripted
non-worker endpoints with the token loses that path once OAuth is on.

The hole is visible in the request gate. `_gate` in `bajutsu/serve/handler.py` authenticates a
request, then splits on whether it carries an identity. An OAuth session has one, so
`forbidden_for_role` checks it. A bearer-token request has none, and its own comment says what
follows: it "stays full-access (the operator credential)". Serve therefore knows exactly two
shapes, and a CI job fits neither.

| Caller | Credential | Identity | Access |
|---|---|---|---|
| Human | GitHub OAuth session | GitHub login | role-gated: viewer / editor / admin |
| Worker | shared token | none | full, on worker routes |
| CI job | *(none available)* | — | — |

Handing CI the shared token where it still works does not close that hole either, for two reasons
beyond the OAuth case. A shared secret cannot say *which* pipeline acted, so an audit trail records
"the token" and stops there. And it is identity-less, so it arrives with full access — a credential
in every pipeline that can rebind an org's configuration or read a config body with embedded
secrets.

Concretely, five endpoints a CI pipeline has reason to call are closed to it: `POST
/api/artifacts/binary` and `GET /api/artifacts/exists` require admin, `POST /api/run` requires
editor. The job-scoped binary artifact override proposal, a sibling of this item, wants exactly that
sequence — upload a build, then dispatch a run naming it — and can only assume a credential an OAuth
deployment has none of. A pipeline publishing a configuration or scenario tree alongside its build
needs two more admin-tier routes, `POST /api/artifacts/config` and `POST /api/artifacts/scenarios`,
so the allowlist below covers all three artifact kinds, not the binary alone. That pipeline is the
consumer of what it publishes, not a courier staging bytes for a human to compose afterwards: it
runs a named scenario against the very triple it just uploaded. Unit 3 says how the triple becomes
effective without the org-wide rebind `POST /api/compose` performs.

**Verifiable outcome.** A GitHub Actions workflow with `permissions: id-token: write` and **no
repository secret** dispatches a run against an OAuth-configured deployment, and the run lands in
the org the workflow's repository belongs to. A workflow in a repository the org does not list is
refused with 403. And the run's audit record names that repository, rather than recording only that
a token was presented.

## Detailed design

The work is mutually exclusive and collectively exhaustive (MECE) across four units: verifying the
token, mapping its claims to an org, deciding what a machine may do, and the tests plus
documentation.

### Unit 1 — Exchange the token once for a machine session

The pipeline this item targets calls three endpoints in sequence: `GET /api/artifacts/exists`, then
`POST /api/artifacts/binary`, then `POST /api/run`. Verifying an OIDC token on every request, and
refusing a `jti` already seen, would refuse that sequence at its second call — the token's `jti` is
already spent by the first, so a per-request scheme with single-use replay cannot serve this
pipeline. Serve instead verifies the token exactly once, at a dedicated exchange endpoint, `POST
/api/oidc/exchange`, and mints a short-lived **machine session** that every later call presents
instead. Every check below still runs in full, just once, at the exchange rather than on every
request.

GitHub Actions issues a signed JSON Web Token (JWT) to any workflow job that declares
`permissions: id-token: write`, requested from `$ACTIONS_ID_TOKEN_REQUEST_URL` with
`$ACTIONS_ID_TOKEN_REQUEST_TOKEN` — both available once the job declares that permission, with no
GitHub App, OAuth app, or identity-provider registration involved on GitHub's side. The job sends the
result to the exchange endpoint as a bearer token, and serve verifies the signature there before
trusting any claim:

- **Issuer.** `iss` must equal the configured issuer, `https://token.actions.githubusercontent.com`
  for GitHub-hosted Actions.
- **Signature.** RS256 against the issuer's JSON Web Key Set (JWKS), discovered from
  `/.well-known/openid-configuration` and cached with a time-to-live so an exchange does not fetch
  keys. The verifier passes an explicit algorithms allowlist of RS256, refusing a token whose header
  names a different one — the classic `alg: none` / HS256-confusion forgery. A JWKS entry matches
  only when its key type is RSA and its advertised algorithm agrees. A missing `kid` triggers a
  refresh capped by a refresh-interval floor (or a negative cache of missing ids), so a stream of
  random `kid`s costs one outbound fetch per interval, not one per call — an exposure now confined
  to the exchange endpoint alone (Unit 3). The fetch carries a timeout, since the gate runs
  synchronously in the stdlib backend, and a key set that cannot be fetched, with an expired cache,
  refuses the exchange rather than trusting a stale one; a signature that still fails to verify fails
  closed too.
- **Audience.** `aud` must equal a value the **deployment** configures — the one check an operator
  must not skip. The workflow chooses its own audience (`core.getIDToken(audience)` takes it as an
  argument), so a deployment that accepts any `aud` accepts a token minted for an unrelated service.
  A deployment with no expected `aud` configured disables the OIDC caller shape entirely: an exchange
  presenting an OIDC token is refused outright, never falling back to accepting any audience — fail
  closed, not operator discipline.
- **Lifetime.** `exp`, `nbf`, and `iat`, with a small skew allowance, matching the 60-second
  backdating `bajutsu/common/github/app.py` already applies when it signs an App JWT. A token
  carrying no `iat` is refused outright: the serve-side age ceiling below has nothing to measure
  from — the same fail-closed rule as a missing `jti`.
- **Replay.** GitHub documents no numeric token lifetime, so a captured token is replayable for its
  whole window. Serve spends each token's `jti` (documented as a unique identifier) in a replay cache
  bounded by that token's `exp`, and refuses a token past a serve-side age ceiling independent of
  `exp`; a token carrying no `jti` is refused outright, the same fail-closed rule Unit 2 applies to a
  missing `environment` claim. The cache lives in the shared system of record — the `Repository`
  seam — the same database the encrypted per-org secret store writes to through its own
  `SecretStore` seam — because `docs/self-hosting.md` documents the hosted control plane as multiple
  replicas sharing that store. A per-process cache would fall to a replay against a second replica,
  leaving the age ceiling as the only real bound. It costs one write per job, since an exchange
  happens once per job rather than once per call.

Verification uses [`joserfc`](https://jose.authlib.org/), a maintained JOSE (JavaScript Object
Signing and Encryption) implementation, declared directly in the `oauth` extra: `authlib` began
depending on it only in 1.7.0, so the extra's `authlib>=1.3` floor does not guarantee it, and today's
lock has it only because that lock pins 1.7.2. The verification itself — the JWKS fetch, its cache,
and the checks above — lives in its own module, imported lazily once OIDC is configured, so
`bajutsu/serve/gate.py` keeps only the machine-session policy decision (Unit 3): `bajutsu/serve/__init__.py`
imports `gate` unconditionally, and `joserfc` ships only with the `oauth` extra, so a module-level
import there would break `import bajutsu.serve` on every base install. Unit 4 adds `joserfc` to
`tests/serve/test_import_guard.py`'s `FORBIDDEN` set, which lists `authlib` today but not `joserfc`.

**The session store gains what this design needs, and refuses what it cannot enforce.**
`SessionStore.issue()` takes no expiry argument today — time-to-live is store-level
(`BAJUTSU_SESSION_TTL`, seven days) — a token's own `exp` cannot be expressed by that. So Unit 1 adds
a per-session expiry to the Protocol and all three implementations (`in_memory_session_store.py`,
`server/sessions/sql_session_store.py`, `server/sessions/redis_session_store.py`), plus two record
columns beside `id` / `identity` / `expires_at` — the resolved **org** and **principal kind**, what
Unit 3 reads per request. `InMemorySessionStore` enforces no expiry at all (`return sid in
self._sessions`), so **the exchange is refused outright on a store that cannot enforce one** — the
same fail-closed posture as an unconfigured `aud` — narrowing the OIDC caller shape to
OAuth-configured, database-backed deployments. The minted session's lifetime also never exceeds the
token's own `exp`: the exchange only improves on reusing the token if the credential it mints is
shorter-lived.

The `sessions` columns above and the `jti` replay cache are both schema changes, and ship with an
Alembic revision under `bajutsu/serve/server/migrations/versions/` the way every prior addition
did — the replay cache included, since it reads as a cache but is a table like any other. Without
it an existing deployment upgrades to code selecting columns its `sessions` table lacks and writing
rows to a table that does not exist, failing at the exchange on exactly the database-backed
deployments this design narrows itself to.

**What the exchange buys, and what it does not.** Both are bearer credentials over TLS, so resistance
to a captured one is roughly unchanged — the honest difference is lifetime, which is why the cap
above matters. Four things do change: the session can be **revoked** (Unit 3's identity for it makes
that possible) where serve cannot revoke a GitHub-issued token; JWT verification and the JWKS fetch
leave the per-request path, reachable pre-authentication on one endpoint rather than every request;
single-use `jti` becomes possible at all, turning a stolen token from a silent success into a
**visible failure** — the attacker's exchange spends the `jti`, so the legitimate job's then fails
loudly; and the minted session is worthless to any other service, unlike a token whose `aud` another
might also accept. The cost is one more endpoint, a session lifetime to get right, and a second
credential in the pipeline. After the exchange, a machine request carries no JWT — it takes the
same session lookup every human request already takes, against a session minted by the same
`state.auth.issue_session()` that mints a human's on the OAuth callback. What OAuth disables is
`authz.login()`, the token sign-in; the exchange takes its place for a machine.

### Unit 2 — Map the claims to an org, on the claims themselves

`OrgConfig` (`bajutsu/serve/orgs.py`) already declares who belongs to an org: `members`,
`githubOrgs`, `githubTeams`, `editorTeams`, and the `targets` it owns. This item adds
`allowedRepositories`, each entry an `"<owner>/<repo>"` string (optionally an object narrowing that
entry further — see below). **The exchange request names the org it
wants, always.** The exchange succeeds when that org's `allowedRepositories` lists the token's
repository, and is refused when it does not. That is the whole rule.

Naming the org **selects, it never grants**: the request says which org to check the claim against,
and the check is what admits it. That keeps `worker_lease`'s rule — the org from a validated source,
never from the caller's word — one level up.

Requiring the name is what lets one repository serve several orgs, a real shape: a shared pipeline
repository testing apps owned by different teams. Inferring the org would have to forbid that, or
pick a winner among the orgs listing the repository. A human resolves such an ambiguity afterwards,
through the header's org selector backed by `eligible_orgs`, because a person can see where they
landed and switch; a pipeline sees nothing, so the same after-the-fact choice would drop it into one
of several tenants silently. Naming the org in the workflow states the target where it is already
written down, and leaves one membership test. The machine session records it, so it is settled at
the exchange rather than re-derived per request.

**`allowedRepositories` keeps `"<owner>/<repo>"` names — a consciously accepted trade-off, not a
passing caveat.** `repository` is a *mutable* name, and name recycling is precisely why GitHub
introduced immutable subjects: a deleted, renamed, or transferred repository frees its name, and
whoever claims it next can mint tokens matching the listed entry. This item keeps the named form,
with an operator duty to update or remove an entry the moment its repository changes — the entry
admits the name, not the repository.

**Configuration binds at exchange time only.** Under a per-request design, removing an entry — or
tightening an `environment` / `ref` bound — took effect on the next call. With a one-time exchange,
an outstanding machine session keeps acting as that org, under the old bound, until it expires: the
real blast radius of the name-recycling exposure above. The operator duty is therefore not only to
remove or update the entry, but to also revoke outstanding machine sessions for it, using the
identity Unit 3 gives each one (`repo:<owner>/<repo>`).

**Match the discrete claims, never a parse of `sub`.** The token carries `repository` and
`repository_owner`, plus `repository_id` and `repository_owner_id`. Authorization compares
`repository` by exact equality. Two facts make reading `sub` the wrong choice:

- **The `sub` format has already changed for new repositories.** A repository created after 15 July
  2026 gets an immutable subject embedding numeric ids
  (`repo:octo-org@123456/octo-repo@456789:ref:refs/heads/main`); GitHub also moves renames and
  transfers after that date to the immutable format, so a listed repository can change format with
  nobody opting in. Older repositories keep the previous shape unless they opt in, and immutable
  subjects are unavailable on GitHub Enterprise Server — a deployment parsing `sub` would have to
  handle every shape and end up mis-parsing one.
- **`sub` is customizable.** A repository can redefine which claims compose the subject through an
  `include_claim_keys` array; an organization can only template it, leaving a repository already
  using OIDC unaffected unless that repository opts in. Authority sits with the repository, not the
  organization, so what `sub` contains is not serve's to assume.

Exact equality also rules out a prefix match, which is the classic failure here: `repo:acme/app`
prefix-matches `repo:acme/app-evil`, so a substring test would admit a repository the operator never
listed.

**The repository is not the whole trust boundary.** Anyone who can merge a workflow change to a
listed repository can mint a token from it, so `allowedRepositories` alone means "whoever can write
that repository's workflows may dispatch as this org". A deployment needing a tighter bound narrows
further on claims the token already carries: `environment` (a GitHub Environment can require
reviewers before a job runs), `ref`, or `job_workflow_ref` — set **per entry**, not for the org as a
whole. An `allowedRepositories` entry is either the plain `"<owner>/<repo>"` string or an object
carrying that same name plus its own `environment` / `ref` / `job_workflow_ref` bound, so one org can
list a repository that runs under a GitHub Environment next to one that does not, with no second org
needed just to hold the unbounded entry. `environment` is a *conditional* claim, emitted only when
the job references one, so a configured bound must refuse a token whose claim is absent, the same as
one whose value differs — otherwise a job declaring no environment escapes the narrowing entirely.

One hazard belongs in the documentation rather than the code: a pull request from a fork does not
receive `id-token: write` by default, but a `pull_request_target` workflow runs in the base
repository's context and can, as does a repository setting that sends write tokens to fork pull
requests. A deployment listing such a repository should narrow by `environment`, so a token can never
be minted from a run an outside contributor influenced.

### Unit 3 — A machine principal, not a human role

The machine session the exchange mints is a **third caller shape**, beside the OAuth session and the
shared token. It carries an identity, like a session, and it is not a person, so no viewer / editor /
admin rank describes it. What it may do is an explicit allowlist of endpoints:

| Allowed | Refused |
|---|---|
| `POST /api/artifacts/{config,scenarios,binary}` | `POST /api/config`, `POST /api/compose` (rebinding the org's active configuration — see below for what a pipeline uses instead) |
| `GET /api/artifacts/exists` | `GET /api/config/content` (a config body may embed secrets) |
| `POST /api/run`, and reading runs in its org | `POST /api/apikey`, `POST /api/claudecodetoken` (operator secrets) |
| | `/api/orgs*` (who may sign in and write) |

**Publishing an artifact is half of what a pipeline needs, and `POST /api/compose` is not the other
half.** A real pipeline uploads a config and a scenario tree alongside its build and then runs a
named scenario against all three, so the three upload routes above leave it holding bytes it has no
allowed call to make effective — `bind_artifact` binds nothing on its own. `POST /api/compose` is
nevertheless the wrong way to close that: it binds the org's **active** configuration and writes the
org's remembered one through `remember_org_config_source`, so a member's next session inherits
whatever the last pipeline composed, and concurrent jobs of one repository contend for a single
binding rather than each running what it asked for. Both are the reasons the sibling job-scoped
binary override gives for refusing a rebind before every dispatch, and they apply to a triple
exactly as they apply to a binary.

The pipeline's triple therefore becomes effective the way that sibling makes a binary effective: as
a **per-job artifact reference named at dispatch**, materialized for the run and binding nothing.
`materialize_composition` already assembles a triple into a content-addressed tree independently of
`state.bind_upload` and `remember_org_config_source`, so a per-job triple needs no new composition
machinery and no binding slot — which is what lets a machine session keep the sessionless
`binding_for` path above. This item does not specify that field; it depends on the sibling widening
its `binaryArtifact` override from the binary leg to the triple. That widening is the one this item
contradicts in the sibling's Alternatives, which rejects it on the premise that CI holds config and
scenarios fixed while varying only the binary — a premise this item's motivating case does not meet.

`POST /api/oidc/exchange` itself sits outside this allowlist: it is the entry point, so it requires
no prior serve credential — only a verifiable OIDC token. Nobody should read the table above as
gating the way in; it governs what an already-minted machine session may do, not how a token
becomes one. Reaching it at all needs a separate change: `gate.is_open`'s POST arm
(`bajutsu/serve/gate.py`) is exactly `path == _LOGIN_PATH` today, so an unauthenticated request to
any other path returns 401 before any verification runs. Unit 3 adds `/api/oidc/exchange` to that
arm, enabled only once OIDC is configured, so the route stays closed on a deployment with no
expected `aud` — the same fail-closed rule as above.

**The identity it carries.** A machine session must carry a non-`None` identity to be revocable at
all — `revoke_identities` revokes by identity string, and its docstring says a session carrying none
is never touched. Give it a reserved form no GitHub login can collide with: a login cannot contain
`/`, so `repo:<owner>/<repo>` (not a `users.id` — see the audit entry below). `revoke_identities`'s
only caller today is org retirement; Unit 3 owns exposing a revocation path so an operator can revoke
a repository's outstanding machine sessions without retiring its org. That granularity is per
repository, not per job — every session a repository mints shares one identity, so a revocation ends
its concurrent pipelines too, and that shared identity is exactly what Unit 2's operator duty relies
on. Org retirement itself needs widening for the same reason: `delete_org` revokes the roster
`list_org_user_ids` returns, which reads the `users` table, and a machine has no row there — so
retiring an org must also revoke the machine sessions bound to it, or they keep acting as the
retired tenant until they expire, the exact leak BE-0375 added that revocation to close.

Run reads are **org**-scoped, not per-actor: the read paths take an `org_id` and never filter on
`created_by`, the nullable foreign key to `users.id` that already exists on `runs`
(`bajutsu/serve/server/models/run.py`). A machine reads its org's runs, including ones it did not
start. `bajutsu/serve/jobs.py` already writes `created_by` only when the actor is a user that
exists — its own comment notes that a token / CI run has no actor. A machine-dispatched run
therefore leaves the column null for free, the second foreign key a synthetic `users` row would
otherwise need to satisfy. Per-run attribution beyond that would be its own separate change.

The allowlist is what makes this safe to hand to a pipeline: four of the endpoints it opens are
admin today, and granting a machine the admin *rank* would come with config rebinding and secret
reads attached. Naming endpoints instead keeps the machine's reach to publishing an artifact and
starting a run — the whole of what a pipeline needs — while `required_role` keeps deciding what a
human needs; this allowlist governs a machine principal and nothing else, so neither gate widens the
other.

The allowlist applies to a machine principal unconditionally, with or without a database wired.
`forbidden_for_role` returns *allowed* when `state.repository is None` ("DB-less = full access",
`bajutsu/serve/authz.py`). An allowlist conditioned on the database the same way would inherit that
fail-open. No machine principal can exist on a database-less deployment in the first place — Unit
1's exchange refuses one — so this is belt-and-braces, not a live path.

The shared policy both backends enforce lives in `bajutsu/serve/gate.py` (BE-0253), not in `_gate`
in `handler.py` alone, so the stdlib handler and the FastAPI app cannot diverge on security posture —
its own docstring calls the prior duplication "a latent path to a security-relevant divergence with
nothing checking for it", and `bajutsu/serve/server/app.py` mirrors `handler.py` line for line. Unit
3's machine-session allowlist lands in `gate.py` alongside the human role gate, so both backends get
the machine principal from one place; Unit 1's token verification stays in its own lazily-imported
module (above), reached only by the exchange endpoint. The machine session is authenticated exactly
like a human one — a valid session cookie, and `gate.is_authorized` already returns True for any of
those — so the new branch sits beside `forbidden_for_role`, keyed on the principal kind the session
record now carries (Unit 1): `forbidden_for_role` must not run for a machine principal, since an
unknown user defaults to viewer and viewer refuses `POST /api/run`; the endpoint allowlist decides
instead, and the principal kind is what keeps a human session from ever reaching it.

**The org travels with the machine session, not through `org_of`.** Today every `start_*` endpoint,
plus `bind_artifact` / `artifact_exists` (`bajutsu/serve/operations/upload.py`) and the run-read call
sites in `bajutsu/serve/operations/reads.py` — every endpoint the allowlist opens — resolve the org
through `state.org_of(actor)`, which reads the actor's *persisted user row*. A machine has no row, so
`org_of` would answer `default` for every one of those calls whatever `allowedRepositories` says: a
cross-tenant hole on the very routes the allowlist opens first. (`Repository.get_run(run_id)`
compounds it — `session.get(Run, run_id)` with no `org_id` — so a per-run read built on it carries no
tenant boundary of its own.) Every allowlisted operation instead reads the org from the machine
session, resolved once at exchange (Unit 1). A machine session also resolves its configuration by
calling `binding_for` with no session, never taking a per-session binding slot: that is the
sessionless path `binding_for` already documents for a CI request, which reads the deployment's
fallback and restores nothing. BE-0393 unit 2 sized the slot map for members
(`MAX_SESSION_BINDINGS`, "thousands of concurrent members"), and its restore is a Git or bundle
fetch paid once per session, so one session per job would both pay that fetch per job and evict
members' slots.

Minting a synthetic `users` row to carry that org instead brings its own cost: it would carry a role
column too, reopening the viewer-default fork above, and the machine would show up in the roster
`/api/orgs` discloses.

For the same reason, the audit record does not gain the repository as an *actor*. `actor_id` is a
nullable foreign key to `users.id` (`bajutsu/serve/server/models/audit_log.py`), and minting a
synthetic row to meet that constraint carries the cost described above. Unlike `created_by`, which
`jobs.py` already guards on the actor having a user row, `_record_audit` passes its actor straight
through to `actor_id`, so Unit 3 changes that seam to write null for a machine principal rather than
a login with no `users` row behind it — keeping the entry itself, since `_record_audit` returns
early on a falsy actor (`bajutsu/serve/authz.py:408`) and simply passing none would drop the row
instead of nulling the column; the repository goes into the audit entry's own detail payload
instead. That answers "which pipeline started this run" without a fake user row.

### Unit 4 — Tests and documentation

The exchange endpoint's verification is covered with no network and no Simulator, by injecting the
JWKS and a locally signed token:

- A token signed by the configured key, with the configured `aud` and a listed repository, is
  exchanged for a machine session that acts as that repository's org.
- A token whose `aud` is anything else is refused at the exchange, including one otherwise valid for
  a listed repository.
- A token from an unlisted repository is refused with 403, and a token whose `repository` merely
  shares a prefix with a listed entry is refused too.
- A repository listed under two orgs exchanges into whichever of them the request names, so one
  shared pipeline repository can serve both.
- A request naming an org whose `allowedRepositories` does not list the token's repository is
  refused, so naming an org selects and never grants.
- An exchange request naming no org is refused, rather than inferring one.
- A token in the immutable-subject format authorizes on its `repository` claim, so the two `sub`
  shapes behave identically.
- An expired token, a token with a `kid` still absent from the JWKS after the bounded refresh, a
  token with a broken signature, and an unreachable JWKS with an expired cache are each refused
  rather than trusting a stale key set.
- A token whose header names `none`, and one naming HS256 signed with the RSA public key bytes, are
  both refused — the algorithm comes from configuration, never from the token.
- A token exchanged twice is accepted once; single-use holds across replicas, so a second replica
  refuses the same `jti` the first replica already consumed, not merely a token re-presented to the
  same one. A token older than the serve-side maximum age is refused even while its own `exp` is
  still in the future. A token carrying no `jti` is refused outright rather than skipping the cache.
  A token carrying no `iat` is refused outright too, rather than exchanging with no origin for the
  age ceiling to measure from.
- A deployment that configures no expected `aud` refuses an otherwise valid OIDC token at the
  exchange, rather than accepting whichever audience it carries — so does one whose session store
  cannot enforce a per-session expiry, `InMemorySessionStore` included. The minted machine session's
  lifetime never exceeds the presented token's own `exp`.
- `import bajutsu.serve` pulls in no `joserfc`, the same way it already pulls in none of the other
  server-only dependencies the import guard's `FORBIDDEN` set lists.
- A machine session reaches every endpoint on the allowlist and is refused on `POST /api/config`,
  `POST /api/compose`, `GET /api/config/content`, and the operator-secret endpoints. A refused
  `POST /api/compose` leaves the org's active binding and its remembered configuration untouched,
  so a member's next session inherits nothing from a pipeline. That holds the
  same way with no database wired. `POST /api/oidc/exchange` itself needs no prior session or token
  to be reached — only a verifiable OIDC token.
- Both backends (`handler.py` and `server/app.py`) enforce the machine session identically, since
  both go through `gate.py`.
- A deployment configuring an `environment`, `ref`, or `job_workflow_ref` bound refuses an otherwise
  valid token whose corresponding claim differs. For `environment` alone, it also refuses one
  whose claim is absent.
- An org listing one repository under an `environment` bound and another with none exchanges both:
  a bound applies to its own entry only, never to the org as a whole.
- A machine's artifact upload (`bind_artifact`) and its exists-probe (`artifact_exists`) resolve the
  org its repository is listed under, not `default`, on a multi-org deployment — and so does a
  machine-dispatched run and a run read.
- The audit record for a machine-dispatched run names the repository in its detail payload, with
  `actor_id` written as null rather than a login no `users` row backs — on an artifact upload as
  well as a run, since `bind_artifact` audits too.
- Retiring an org revokes its outstanding machine sessions, not only the sessions of the members
  `list_org_user_ids` returns.

`docs/self-hosting.md` and its Japanese mirror gain a section covering:

- the workflow permission and the two environment variables;
- the `aud` an operator must configure, and that omitting it disables the OIDC caller shape
  entirely;
- the exchange endpoint, the machine session it mints, and that the session's time-to-live is
  capped by the presented token's own `exp`;
- the `allowedRepositories` shape, that the exchange request names the org to check against, and the
  operator duty to update an entry — and revoke outstanding machine sessions for it — when its
  repository is renamed, transferred, or deleted, or when a narrowing tightens;
- the optional `environment` / `ref` / `job_workflow_ref` narrowing, including an `environment`
  bound refusing an absent claim the same as a differing one;
- the `pull_request_target` and fork-pull-request write-token hazards;
- that every later call in the pipeline presents the minted session, never the OIDC token itself.

A worked workflow snippet belongs there too: the GitHub-side setup is two lines, and the rest is
serve configuration.

## Alternatives considered

| Alternative | Why we did not take it |
|---|---|
| Verify the OIDC token on every request instead of exchanging it once | The target pipeline calls several endpoints in sequence, so a single-use `jti` would refuse every call after the first; dropping single-use to allow reuse leaves only an age ceiling. Per-request verification also keeps the JWKS fetch and JWT parsing reachable pre-authentication on every route rather than one, and cannot revoke a credential serve did not mint. |
| Issue a long-lived per-org CI token from the settings panel | Simpler, and it reintroduces the secret this item exists to avoid: a token to store in every pipeline, to rotate by hand, and to leak from a log. It also cannot say which repository acted — the identity gap is the point, not just the storage. |
| Reuse the shared token for CI, reopening what BE-0313 narrowed | Reverses a deliberate decision. The shared token is identity-less and full-access, so restoring it for non-worker endpoints hands every pipeline the ability to rebind a config and read secrets. |
| Give a CI identity the admin role | Four of the five endpoints CI needs are admin today, so the rank looks like a fit. It carries config rebinding, `GET /api/config/content`, and the operator-secret endpoints with it — none of which a pipeline needs, all of which it would then hold. |
| Authorize on the `sub` claim | The format has already changed for new repositories under an immutable-id rollout, and it is customizable per repository. Its shape is not serve's to assume. A prefix match on it also admits a repository whose name merely extends a listed one. The discrete claims say the same thing without either problem. |
| Authorize on the immutable numeric claims (`repository_id` / `repository_owner_id`) instead of `repository` | Those claims are immutable and unaffected by subject customization, closing the name-recycling exposure outright. The cost: a configuration nobody can read or write from the repository name alone. Adding an entry needs a lookup, and auditing config gives no hint which repository an entry means. The named form stays for that legibility, with the operator duty above as its price. |
| Use a GitHub App installation token instead of OIDC | Bajutsu already signs App JWTs for the private-repo config source (BE-0224), so the machinery is familiar. It needs an App registered and a private key distributed to the deployment, and the resulting token authenticates the App rather than the pipeline — back to a shared secret with a coarser identity. |
| Accept OIDC from any issuer out of the gate | GitLab, Buildkite, and CircleCI all issue OIDC tokens, and per-org issuer configuration would generalize this. The claim names differ per provider, so a general mapping is a larger design than the case in hand; scoping to GitHub Actions first leaves the issuer configurable and defers the mapping. |

## Progress

> Keep this current as work proceeds. The checklist mirrors the MECE work breakdown in
> *Detailed design* (one box per unit of work); the log records what changed and when
> (oldest first), linking the PRs.

- [x] Unit 1 — Exchange the token for a machine session at `POST /api/oidc/exchange`. It verifies
      issuer, JWKS with a bounded `kid` refresh and a pinned RS256 algorithm allowlist,
      deployment-configured `aud` (fail closed if unset), and lifetime. A `jti` replay defense shared
      across replicas through the `Repository` seam (refusing a token with no `jti`), a per-session
      expiry on `SessionStore.issue()` capped by the token's own `exp` and refused outright on a store
      that cannot enforce it, an org and principal-kind column on the session record, an Alembic
      revision for those columns and the replay table, `joserfc`
      declared directly in the `oauth` extra, and its verification kept in its own lazily-imported
      module so `gate.py` stays free of it.
- [x] Unit 2 — `allowedRepositories` on `OrgConfig`, checked against the discrete claims for the org
      the exchange request names, with the optional per-entry `environment` / `ref` /
      `job_workflow_ref` narrowing and an `environment` bound refusing an absent claim.
- [ ] Unit 3 — The machine session (identity `repo:<owner>/<repo>`, revocable) and its endpoint
      allowlist in `bajutsu/serve/gate.py`, added to `gate.is_open`'s POST arm and enforced
      unconditionally regardless of the database, the verified org carried on the machine session
      rather than read through `org_of` (run reads included), `_record_audit` writing null for a
      machine principal with the repository recorded in the audit entry's detail payload, a
      revocation path for a repository's outstanding sessions, and org retirement widened to revoke
      the machine sessions bound to the retired org.
- [ ] Unit 4 — Tests for each seam, including the cross-replica `jti` replay test, the DB-less
      exchange refusal, and the import-guard check for `joserfc`, and the self-hosting documentation.

Log:

- [#1986](https://github.com/bajutsu-e2e/bajutsu/pull/1986) — Units 1 and 2. Added
  `POST /api/oidc/exchange` and the verification behind it (`bajutsu/serve/oidc.py`): JWKS
  discovery with a bounded-refresh cache, a pinned RS256 allowlist, issuer / audience / lifetime
  with a 60s skew, a serve-side age ceiling, mandatory `iat` and `jti`, and a single-use `jti`
  spent through the `Repository` seam so replay is refused across replicas. `SessionStore` gained
  a per-session expiry, an org and a principal kind across all three implementations, plus a
  `Principal` read narrowed in one place. Added `allowedRepositories` on `OrgConfig` with
  exact-equality matching on the discrete claims and optional per-entry `environment` / `ref` /
  `workflowRef` bounds. Alembic revision 0020 carries the two session columns, the org column and
  the `oidc_jti` table.

  Three deviations from this item's text, each because the literal reading does not work.
  `allowedRepositories` needed a database column as well as the config field, since a DB-backed
  deployment reads its org model from `orgs_from_db` and the exchange requires a database — a
  config-only field would be empty on exactly the deployments that can use it. The route was added
  to `gate.is_open` here rather than in unit 3, or unit 1 would be unreachable and untestable.
  And instead of refusing the exchange on a store that cannot enforce a per-session expiry,
  `InMemorySessionStore` gained one; the database requirement reaches the same narrowing with less
  code. The per-entry bound is spelled `workflowRef` rather than `job_workflow_ref`, matching the
  camelCase of the other config keys and staying provider-neutral.

  Verification is provider-independent: `OidcProvider` is a table of claim names, so a second CI
  platform is one entry rather than a redesign. Units 3 and 4 remain, so a minted machine session
  is refused on every endpoint for now — `gate.forbidden_for_machine` is the single seam unit 3
  replaces, and the deny-all is what keeps a machine session out of the role gate's viewer default
  in the meantime.

## References

- [BE-0313 — GitHub org membership and Team-based RBAC for serve](../BE-0313-github-org-team-rbac/BE-0313-github-org-team-rbac.md)
  — the item that gave humans an identity and narrowed the shared token to worker traffic, leaving
  the machine gap this item fills.
- [BE-0051 — Serve hardening for hosting (auth, input validation)](../BE-0051-serve-hardening-for-hosting/BE-0051-serve-hardening-for-hosting.md)
  — the shared token and the request gate this item adds a third caller shape to.
- [BE-0015 — Public hosting of the web UI](../BE-0015-web-ui-public-hosting/BE-0015-web-ui-public-hosting.md)
  — the multi-tenancy and role model the machine allowlist sits beside.
- [BE-0224 — GitHub private-repo config authentication](../BE-0224-github-private-repo-config-auth/BE-0224-github-private-repo-config-auth.md)
  — a separate config-source concern that also signs a JWT (App JWTs, with `cryptography`), unrelated
  to this item's own `joserfc` dependency in the `oauth` extra.
- [BE-0160 — Credential-free worker uploads via presigned URLs](../BE-0160-worker-credential-free-uploads/BE-0160-worker-credential-free-uploads.md)
  — the same "hold no long-lived credential" posture, applied to the worker.
- [BE-0268 — Composable upload artifacts](../BE-0268-composable-upload-artifacts/BE-0268-composable-upload-artifacts.md)
  — the triple and `materialize_composition`, which assembles one independently of any binding.
- The job-scoped binary artifact override proposal — the sibling this item's allowlist depends on
  for making a pipeline's uploaded triple effective, and whose Alternatives currently reject
  widening that override past the binary leg on a premise this item's motivating case does not meet.
- [OpenID Connect reference — GitHub Docs](https://docs.github.com/en/actions/reference/security/oidc)
  — the issuer, the claim set, the immutable-subject rollout, and `include_claim_keys`.
- [OpenID Connect — GitHub Docs](https://docs.github.com/en/actions/concepts/security/openid-connect)
  — what `permissions: id-token: write` grants and how a job requests a token.
- [Using OpenID Connect in cloud providers — GitHub Docs](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-cloud-providers)
  — the request environment variables, and why the trust conditions live on the verifying side.
