"""Exchange a CI job's OIDC token for a machine session (BE-0414 unit 1).

The entry point for the third caller shape. A human signs in through GitHub OAuth and gets an
identity the role gate checks; a worker presents the shared token and gets no identity at all; a
continuous-integration (CI) job can do neither, so it presents the OpenID Connect (OIDC) token its
platform issues it — here, once — and gets a short-lived **machine session** to use for every
later call in the pipeline.

Once, not per request, because the pipeline this serves calls several endpoints in sequence: a
single-use `jti` checked on every request would refuse the second call, and dropping single-use to
allow reuse would leave only an age ceiling. Verifying once also keeps the JSON Web Key Set fetch
and the JWT parsing off every route's pre-authentication path, and lets `serve` revoke a credential
it minted — which it can never do to a token GitHub issued.

Deterministic throughout: a signature check, exact-equality claim comparisons, and a primary-key
insert. No LLM is anywhere near this path, nor near the run it eventually dispatches.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from bajutsu.serve import oplog
from bajutsu.serve.oidc import JwksCache, OidcError, verify
from bajutsu.serve.orgs import orgs_from_db
from bajutsu.serve.sessions import MACHINE
from bajutsu.serve.state import ServeState

_logger = logging.getLogger(__name__)

# What a refused exchange tells the caller. Deliberately one string for every cause: the pipeline
# operator reads the reason in the deployment's log, where it is safe, while a caller probing the
# endpoint learns only that it failed — not which check it got past. The audience mismatch and the
# unlisted repository are exactly the two an attacker would otherwise enumerate.
_REFUSED = "the OIDC token was refused"

# The exchange is reachable unauthenticated, and the org it names reaches the refusal log as a
# structured field. An org slug is capped at 64 characters (`operations.orgs`), so anything longer
# names no org that could exist and is refused before it is ever logged.
_MAX_ORG_LENGTH = 64


def oidc_exchange(state: ServeState, token: str, org: str) -> tuple[Any, int, str | None]:
    """Verify *token* and mint a machine session acting as *org*.

    Returns ``(payload, status, session_id | None)``, the same shape `login` and `oauth_callback`
    return, so each backend sets the session cookie exactly as it already does.

    Naming the org **selects, it never grants**: the request says which org to check the token's
    claims against, and that org's own `allowedRepositories` is what admits it. Requiring the name
    is what lets one repository serve several orgs — a shared pipeline repository testing apps
    owned by different teams — which inferring the org would have to forbid or resolve by picking a
    winner. A person resolves such an ambiguity afterwards through the header's org selector; a
    pipeline sees nothing, so the same after-the-fact choice would drop it into one of several
    tenants silently.
    """
    config = state.auth.oidc
    if config is None:
        # No expected audience configured, so the caller shape is off entirely. The route is not
        # even open in that case (`gate.is_open`); this is the second lock on the same door.
        return {"error": "oidc not configured"}, 404, None
    if state.repository is None:
        # The replay cache is a table in the shared system of record, because a hosted control
        # plane is several replicas and a per-process cache would fall to a replay against the
        # second one. With no database there is nowhere to spend a `jti`, so rather than exchange
        # without single-use, refuse — the same fail-closed posture as an unconfigured audience.
        # Logged as well as answered: the boot-time warning fires once, and an operator reading
        # logs a week later needs to see that pipelines are still being turned away.
        oplog.log_event(
            _logger,
            "oidc.denied",
            "the OIDC exchange needs a database to spend each token's 'jti' in",
            level=logging.WARNING,
        )
        return {"error": "oidc exchange needs a database"}, 400, None
    if not org or len(org) > _MAX_ORG_LENGTH:
        # Length-capped because this arrives unauthenticated and is carried into the log below as
        # a structured field: an uncapped value lets a caller write as much as it likes per request.
        return {"error": "org is required"}, 400, None
    try:
        verified = verify(token, config, _cache(state, config.issuer))
    except OidcError as e:
        return _refuse(str(e), org=org)
    workload = verified.workload
    configured = orgs_from_db(state.repository).get(org)
    if configured is None or not configured.admits_workload(workload):
        return _refuse(f"{workload.repository!r} is not listed by org {org!r}", org=org)
    if not state.repository.spend_oidc_jti(verified.jti, expires_at=verified.expires_at):
        # Already spent — a replay, or the losing side of a race between two replicas. Loud rather
        # than silent on purpose: a stolen token turns the legitimate job's own exchange into a
        # visible failure instead of letting the theft succeed unnoticed.
        return _refuse(f"the token's 'jti' was already spent by {workload.repository!r}", org=org)
    expires_at = _expiry(config.session_ttl, verified.expires_at)
    if expires_at <= datetime.now(UTC):
        # The lifetime checks tolerate 60s of clock skew, so a token can pass them while its `exp`
        # is already behind *our* clock — and the session is capped by that `exp`, so it would be
        # born dead. Refuse rather than answer 200 with a cookie that 401s on the pipeline's next
        # call: the same clock judges both, so if one says expired the other cannot disagree.
        return _refuse("the token expires too soon to mint a session from", org=org)
    identity = f"repo:{workload.repository}"
    # A reserved form no GitHub login can collide with — a login cannot contain `/`. It has to be
    # non-None at all for the session to be revocable: `revoke_identities` works by identity, and
    # never touches a session carrying none.
    sid = state.auth.issue_session(identity, expires_at=expires_at, org=org, kind=MACHINE)
    oplog.log_event(
        _logger,
        "oidc.exchange",
        f"minted a machine session for {identity} as {org}",
        repository=workload.repository,
        org=org,
    )
    return {"ok": True, "org": org, "repository": workload.repository}, 200, sid


def _cache(state: ServeState, issuer: str) -> JwksCache:
    """The deployment's key-set cache, built on first use and kept for the life of the process.

    Held on the state rather than rebuilt per request so the bounded refresh actually bounds
    anything — a fresh cache per call would fetch the issuer's keys on every exchange.
    """
    if state.oidc_keys is None:
        state.oidc_keys = JwksCache(issuer)
    return state.oidc_keys


def _expiry(session_ttl: int, token_expiry: datetime) -> datetime:
    """The machine session's expiry: the configured lifetime, never past the token's own `exp`.

    The cap is the whole reason exchanging beats re-presenting the token — both are bearer
    credentials over TLS, so what actually improves is how long a captured one is worth stealing.
    """
    return min(datetime.now(UTC) + timedelta(seconds=session_ttl), token_expiry)


def _refuse(reason: str, *, org: str) -> tuple[Any, int, str | None]:
    """Log why, answer with what. The deployment's operator needs the cause; the caller does not."""
    oplog.log_event(_logger, "oidc.denied", reason, org=org, level=logging.WARNING)
    return {"error": _REFUSED}, 403, None
