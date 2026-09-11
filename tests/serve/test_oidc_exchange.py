"""Tests for the OIDC exchange endpoint and the machine session it mints (BE-0414 unit 1).

The third caller shape, end to end: a CI job presents its OIDC token once, `serve` verifies it and
mints a short-lived machine session, and every later call in the pipeline is an ordinary session
request. Offline throughout — the issuer's keys are generated in-process and injected through
`JwksCache`'s `fetch` seam, so no test reaches the network.

What a machine session may *do* is BE-0414 unit 3's endpoint allowlist. Until it lands a machine
principal is refused everywhere, which these tests lock in both backends: the exchange is testable
on its own, and nothing it mints can reach an endpoint the allowlist has not yet opened.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _shared import _serve
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from sqlalchemy import Engine

from bajutsu.serve import operations as ops
from bajutsu.serve.oidc import GITHUB_ACTIONS, JwksCache, OidcConfig
from bajutsu.serve.sessions import MACHINE, InMemorySessionStore
from bajutsu.serve.state import ServeState, SessionManager

ISSUER = GITHUB_ACTIONS.issuer
AUDIENCE = "https://bajutsu.example.com"
_ORGS_YAML = """
targets: {}

orgs:
  acme:
    members: [alice]
    allowedRepositories:
      - acme/app
      - repository: acme/web
        environment: production
  globex:
    members: [bob]
    allowedRepositories: [shared/ci]
  shared:
    allowedRepositories: [shared/ci]
"""


def _threaded(tmp_path: Path) -> Callable[..., Engine]:
    """A `serve_engine`-shaped factory backed by a file rather than `:memory:`.

    The HTTP cases below serve on a background thread, and SQLite's in-memory database is
    per-connection — so the server thread would open a fresh, empty one and find no tables. The
    exchange itself is exercised against the parametrized fixture (Postgres included) above;
    these cases are about the gate, so a file is enough.
    """
    from sqlalchemy import create_engine

    def factory(*, foreign_keys: bool = False) -> Engine:
        return create_engine(f"sqlite:///{tmp_path / 'serve.db'}")

    return factory


def _key(kid: str = "k1") -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": kid, "alg": "RS256"}, private=True)


def _fetch_for(key: RSAKey) -> Callable[[str], bytes]:
    public = KeySet([RSAKey.import_key(key.as_dict(private=False))]).as_dict()

    def fetch(url: str) -> bytes:
        if url.endswith("/.well-known/openid-configuration"):
            return json.dumps({"jwks_uri": f"{ISSUER}/.well-known/jwks"}).encode()
        return json.dumps(public).encode()

    return fetch


def _token(key: RSAKey, **claims: Any) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "jti": f"jti-{now}-{claims.get('repository', 'acme/app')}",
        "repository": "acme/app",
        "repository_owner": "acme",
        "ref": "refs/heads/main",
        "job_workflow_ref": "acme/app/.github/workflows/e2e.yml@refs/heads/main",
        **claims,
    }
    return jwt.encode({"alg": "RS256", "kid": key.kid}, payload, key)


def _state(
    serve_engine: Callable[..., Engine],
    tmp_path: Path,
    key: RSAKey,
    *,
    oidc: bool = True,
    database: bool = True,
    **config: Any,
) -> ServeState:
    from bajutsu.serve.operations.config import seed_orgs_from_bound_config
    from bajutsu.serve.server.db import SqlRepository
    from bajutsu.serve.server.models import Base
    from bajutsu.serve.server.sessions import SqlSessionStore

    path = tmp_path / "bajutsu.config.yaml"
    path.write_text(_ORGS_YAML, encoding="utf-8")
    settings = (
        OidcConfig(
            provider=GITHUB_ACTIONS,
            audience=AUDIENCE,
            issuer=ISSUER,
            max_token_age=config.pop("max_token_age", 300),
            session_ttl=config.pop("session_ttl", 900),
        )
        if oidc
        else None
    )
    repository = sessions = None
    if database:
        engine = serve_engine(foreign_keys=True)
        Base.metadata.create_all(engine)
        repository = SqlRepository(engine)
        sessions = SqlSessionStore(engine)
    state = ServeState(
        runs_dir=tmp_path / "runs",
        config=path,
        root=tmp_path,
        cwd=tmp_path,
        repository=repository,
        auth=SessionManager(
            token="s3cret",
            oidc=settings,
            sessions=sessions if sessions is not None else InMemorySessionStore(),
        ),
        # The injection point for an offline issuer: the operation builds one lazily only when the
        # state carries none, so pre-seeding it is how a test keeps the key fetch in-process.
        oidc_keys=JwksCache(ISSUER, fetch=_fetch_for(key)),
    )
    if database:
        seed_orgs_from_bound_config(state)
    return state


# --- the exchange itself ----------------------------------------------------------------------


def test_a_listed_repository_exchanges_into_the_org_it_named(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    key = _key()
    state = _state(serve_engine, tmp_path, key)
    payload, status, sid = ops.oidc_exchange(state, _token(key), "acme")
    assert status == 200 and sid is not None
    assert payload == {"ok": True, "org": "acme", "repository": "acme/app"}
    principal = state.auth.principal(sid)
    assert principal is not None
    # A reserved identity form no GitHub login can collide with (a login cannot contain "/"), and
    # non-None so the session is revocable at all — `revoke_identities` never touches one with none.
    assert principal.identity == "repo:acme/app"
    assert principal.org == "acme" and principal.kind == MACHINE
    assert state.auth.sessions.revoke_identities(["repo:acme/app"]) == 1


def test_naming_an_org_selects_and_never_grants(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """The request says which org to check the claims against; that org's own roster is what
    admits it. Otherwise naming an org would be the grant, not the check."""
    key = _key()
    state = _state(serve_engine, tmp_path, key)
    _payload, status, sid = ops.oidc_exchange(state, _token(key), "globex")
    assert status == 403 and sid is None


def test_one_repository_can_serve_several_orgs(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """A shared pipeline repository testing apps owned by different teams — the shape that forces
    the request to name its org rather than letting serve infer one."""
    key = _key()
    state = _state(serve_engine, tmp_path, key)
    for org in ("globex", "shared"):
        payload, status, sid = ops.oidc_exchange(
            state, _token(key, repository="shared/ci", jti=f"jti-{org}"), org
        )
        assert status == 200 and sid is not None, org
        assert payload["org"] == org


def test_an_exchange_naming_no_org_is_refused(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    key = _key()
    state = _state(serve_engine, tmp_path, key)
    payload, status, sid = ops.oidc_exchange(state, _token(key), "")
    assert status == 400 and sid is None and "org is required" in payload["error"]


def test_an_unlisted_repository_and_a_prefix_match_are_both_refused(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    key = _key()
    state = _state(serve_engine, tmp_path, key)
    for repository in ("acme/other", "acme/app-evil"):
        _payload, status, sid = ops.oidc_exchange(
            state, _token(key, repository=repository, jti=repository), "acme"
        )
        assert status == 403 and sid is None, repository


def test_a_per_entry_environment_bound_applies_to_its_own_entry_only(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    key = _key()
    state = _state(serve_engine, tmp_path, key)
    # `acme/web` is listed under an environment bound; a token declaring none is refused.
    _payload, status, _sid = ops.oidc_exchange(
        state, _token(key, repository="acme/web", jti="web-none"), "acme"
    )
    assert status == 403
    _payload, status, sid = ops.oidc_exchange(
        state,
        _token(key, repository="acme/web", environment="production", jti="web-prod"),
        "acme",
    )
    assert status == 200 and sid is not None
    # `acme/app` is listed with no bound and still exchanges, so the bound never leaked to the org.
    assert ops.oidc_exchange(state, _token(key, jti="app"), "acme")[1] == 200


def test_a_token_for_another_audience_is_refused(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    key = _key()
    state = _state(serve_engine, tmp_path, key)
    token = _token(key, aud="https://someone-else.example.com")
    _payload, status, sid = ops.oidc_exchange(state, token, "acme")
    assert status == 403 and sid is None


def test_a_token_is_exchanged_once_and_replay_is_refused(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    key = _key()
    state = _state(serve_engine, tmp_path, key)
    token = _token(key)
    assert ops.oidc_exchange(state, token, "acme")[1] == 200
    _payload, status, sid = ops.oidc_exchange(state, token, "acme")
    assert status == 403 and sid is None


def test_replay_is_refused_across_replicas(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """The reason the replay cache is a table: a hosted control plane is several replicas over one
    database, so a captured token presented to the second must be refused by the first's write."""
    from bajutsu.serve.server.db import SqlRepository

    key = _key()
    first = _state(serve_engine, tmp_path, key)
    assert first.repository is not None
    engine = first.repository._engine  # type: ignore[attr-defined]
    second = _state(serve_engine, tmp_path, key)
    second.repository = SqlRepository(engine)  # a second replica on the same database

    token = _token(key)
    assert ops.oidc_exchange(first, token, "acme")[1] == 200
    assert ops.oidc_exchange(second, token, "acme")[1] == 403


def test_the_minted_session_never_outlives_the_tokens_own_exp(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """Both are bearer credentials over TLS, so what the exchange actually buys is a shorter
    lifetime — which a session outliving its token would give straight back."""
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session

    from bajutsu.serve.server.models import SessionRecord

    key = _key()
    # A session TTL far beyond the token's 300s `exp`: the token's own lifetime must win.
    state = _state(serve_engine, tmp_path, key, session_ttl=86_400)
    _payload, status, sid = ops.oidc_exchange(state, _token(key), "acme")
    assert status == 200 and sid is not None
    assert state.repository is not None
    with Session(state.repository._engine) as session:  # type: ignore[attr-defined]
        row = session.get(SessionRecord, sid)
        assert row is not None
        expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    assert expires <= datetime.now(UTC).replace(microsecond=0) + __import__("datetime").timedelta(
        seconds=301
    )


def test_a_token_already_past_its_exp_mints_no_session(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """The lifetime checks tolerate 60s of clock skew, so a token can pass them with its `exp`
    already behind our clock. The session is capped by that `exp`, so it would be born dead — and
    a 200 carrying a cookie that 401s on the next call is worse than a refusal."""
    key = _key()
    state = _state(serve_engine, tmp_path, key)
    now = int(time.time())
    token = _token(key, iat=now - 30, nbf=now - 30, exp=now - 5, jti="nearly-expired")
    payload, status, sid = ops.oidc_exchange(state, token, "acme")
    assert status == 403 and sid is None
    assert payload["error"] == "the OIDC token was refused"


def test_a_deployment_with_no_expected_audience_refuses_the_exchange(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """Fail closed, never "accept whichever audience it carries": the workflow picks its own, so a
    deployment accepting any would accept a token minted for an unrelated service."""
    key = _key()
    state = _state(serve_engine, tmp_path, key, oidc=False)
    payload, status, sid = ops.oidc_exchange(state, _token(key), "acme")
    assert status == 404 and sid is None and payload["error"] == "oidc not configured"


def test_a_deployment_with_no_database_refuses_the_exchange(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """There is nowhere to spend a `jti` without one, and exchanging without single-use would
    leave the serve-side age ceiling as the only bound on a captured token."""
    key = _key()
    state = _state(serve_engine, tmp_path, key, database=False)
    payload, status, sid = ops.oidc_exchange(state, _token(key), "acme")
    assert status == 400 and sid is None and "needs a database" in payload["error"]


def test_every_refusal_answers_the_same_way(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """A caller probing the endpoint learns only that it failed. The audience mismatch and the
    unlisted repository are exactly the two an attacker would otherwise enumerate; the cause goes
    to the deployment's own log instead."""
    key = _key()
    state = _state(serve_engine, tmp_path, key)
    answers = {
        ops.oidc_exchange(state, _token(key, aud="elsewhere", jti="a"), "acme")[0]["error"],
        ops.oidc_exchange(state, _token(key, repository="acme/nope", jti="b"), "acme")[0]["error"],
        ops.oidc_exchange(state, _token(key, jti="c"), "no-such-org")[0]["error"],
        ops.oidc_exchange(state, "not-a-jwt", "acme")[0]["error"],
    }
    assert len(answers) == 1


# --- the gate: reaching the exchange, and what its session may do -------------------------------


@pytest.mark.parametrize("oidc", [True, False])
def test_the_exchange_is_reachable_only_when_oidc_is_configured(tmp_path: Path, oidc: bool) -> None:
    """It is the machine's way *in*, so it needs no prior serve credential — but only on a
    deployment that configured one. Elsewhere it stays behind the 401 like any other route."""
    key = _key()
    state = _state(_threaded(tmp_path), tmp_path, key, oidc=oidc)
    server, port = _serve(state)
    try:
        status, _headers, _body = _post(port, "/api/oidc/exchange", {"token": "x", "org": "acme"})
        # Configured: reached, and refused on the token (403). Not configured: never reached (401).
        assert status == (403 if oidc else 401)
    finally:
        server.shutdown()
        server.server_close()


def test_the_exchange_sets_a_session_cookie_over_http(tmp_path: Path) -> None:
    key = _key()
    state = _state(_threaded(tmp_path), tmp_path, key)
    server, port = _serve(state)
    try:
        status, headers, _body = _post(
            port, "/api/oidc/exchange", {"token": _token(key), "org": "acme"}
        )
        assert status == 200
        cookie = headers.get("Set-Cookie", "")
        assert cookie.startswith("bajutsu_session=")
        # The pipeline holds an opaque id, never the credential it was exchanged for.
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    finally:
        server.shutdown()
        server.server_close()


def test_a_machine_session_reaches_no_endpoint_yet(tmp_path: Path) -> None:
    """Until unit 3's allowlist lands. Without this the session would fall into the *role* gate,
    which reads an identity with no user row as a viewer — handing a pipeline read access to the
    `default` org through `org_of`."""
    key = _key()
    state = _state(_threaded(tmp_path), tmp_path, key)
    _payload, status, sid = ops.oidc_exchange(state, _token(key), "acme")
    assert status == 200 and sid is not None
    server, port = _serve(state)
    try:
        for path in ("/api/runs", "/api/config", "/api/orgs"):
            code, _headers, _body = _get(port, path, cookie=sid)
            assert code == 403, path
    finally:
        server.shutdown()
        server.server_close()


def test_a_human_session_is_untouched_by_the_machine_gate(tmp_path: Path) -> None:
    """The kind the store recorded is what routes the request, not the shape of an identity string
    — so a person signing in is governed by the role gate exactly as before."""
    key = _key()
    state = _state(_threaded(tmp_path), tmp_path, key)
    sid = state.auth.issue_session()  # the shared-token login: a human session, no identity
    server, port = _serve(state)
    try:
        assert _get(port, "/api/runs", cookie=sid)[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_both_backends_refuse_a_machine_session_identically(tmp_path: Path) -> None:
    """`gate.py` owns the policy so the stdlib handler and the FastAPI app cannot diverge on it —
    the divergence BE-0253 exists to prevent, on a security-relevant branch."""
    from fastapi.testclient import TestClient

    from bajutsu.serve.server.app import make_app

    key = _key()
    state = _state(_threaded(tmp_path), tmp_path, key)
    _payload, status, sid = ops.oidc_exchange(state, _token(key), "acme")
    assert status == 200 and sid is not None
    client = TestClient(make_app(state))
    client.cookies.set("bajutsu_session", sid)
    assert client.get("/api/runs").status_code == 403
    # And the exchange itself is reachable on this backend too, with no prior credential — from a
    # client carrying *no* cookie, or the 403 would be the machine gate's rather than the token's.
    assert (
        TestClient(make_app(state))
        .post("/api/oidc/exchange", json={"token": "x", "org": "acme"})
        .status_code
        == 403
    )


def test_an_open_path_is_reachable_with_a_machine_cookie_on_both_backends(
    tmp_path: Path,
) -> None:
    """`is_open` short-circuits *both* gates, and both backends must agree on that. A pipeline
    reusing one cookie jar re-exchanges to renew past its TTL, so a backend that ran the machine
    gate over the open paths would lock it out permanently on exactly that renewal."""
    from fastapi.testclient import TestClient

    from bajutsu.serve.server.app import make_app

    key = _key()
    state = _state(_threaded(tmp_path), tmp_path, key)
    _payload, status, sid = ops.oidc_exchange(state, _token(key), "acme")
    assert status == 200 and sid is not None

    client = TestClient(make_app(state))
    client.cookies.set("bajutsu_session", sid)
    assert client.get("/").status_code == 200
    # A fresh token each time — the replay cache spends each `jti` once, which is the point.
    renewed = client.post(
        "/api/oidc/exchange", json={"token": _token(key, jti="renew-asgi"), "org": "acme"}
    )
    assert renewed.status_code == 200, "a machine session must be able to renew itself"

    server, port = _serve(state)
    try:
        assert _get(port, "/", cookie=sid)[0] == 200
        code, _headers, _body = _post(
            port,
            "/api/oidc/exchange",
            {"token": _token(key, jti="renew-stdlib"), "org": "acme"},
            cookie=sid,
        )
        assert code == 200
    finally:
        server.shutdown()
        server.server_close()


def test_a_machine_session_posting_a_body_is_refused_and_the_connection_stays_usable(
    tmp_path: Path,
) -> None:
    """The stdlib gate drains a refused request's body before replying, or the unread bytes
    corrupt the next request on a keep-alive connection — the request shape unit 3's allowlist
    opens up, and one no GET can reach."""
    key = _key()
    state = _state(_threaded(tmp_path), tmp_path, key)
    _payload, status, sid = ops.oidc_exchange(state, _token(key), "acme")
    assert status == 200 and sid is not None
    server, port = _serve(state)
    try:
        code, _headers, _body = _post(port, "/api/run", {"scenario": "x" * 5000}, cookie=sid)
        assert code == 403
        # The next request on a fresh connection must still be answered correctly.
        assert _get(port, "/api/runs", cookie=sid)[0] == 403
    finally:
        server.shutdown()
        server.server_close()


def test_the_key_set_cache_is_built_once_and_kept(
    serve_engine: Callable[..., Engine], tmp_path: Path
) -> None:
    """Held on the state, not rebuilt per call: a fresh cache per exchange would fetch the issuer's
    keys every time and make the refresh floor bound nothing."""
    key = _key()
    state = _state(serve_engine, tmp_path, key)
    state.oidc_keys = None  # the real cold-start path, which pre-seeding otherwise hides
    assert ops.oidc_exchange(state, _token(key, jti="one"), "acme")[1] in (200, 403)
    first = state.oidc_keys
    assert first is not None
    ops.oidc_exchange(state, _token(key, jti="two"), "acme")
    assert state.oidc_keys is first


def test_the_fastapi_backend_mints_the_same_machine_session(tmp_path: Path) -> None:
    """Both backends run the exchange through the same operation, so a pipeline pointed at either
    gets the same cookie — the parity `gate.py` and the shared route registry exist to hold."""
    from fastapi.testclient import TestClient

    from bajutsu.serve.server.app import make_app

    key = _key()
    state = _state(_threaded(tmp_path), tmp_path, key)
    client = TestClient(make_app(state))
    response = client.post("/api/oidc/exchange", json={"token": _token(key), "org": "acme"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "org": "acme", "repository": "acme/app"}
    sid = response.cookies.get("bajutsu_session")
    assert sid is not None
    # The same cookie attributes the stdlib backend sets. A CI credential is a worse thing to
    # expose to script than a browser login, and this is exactly the axis BE-0253 exists to hold.
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie and "SameSite=strict" in set_cookie.replace("Strict", "strict")
    principal = state.auth.principal(sid)
    assert principal is not None
    assert principal.kind == MACHINE and principal.org == "acme"


def test_the_fastapi_backend_leaves_the_exchange_closed_with_no_oidc_configured(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from bajutsu.serve.server.app import make_app

    key = _key()
    state = _state(_threaded(tmp_path), tmp_path, key, oidc=False)
    client = TestClient(make_app(state))
    # Never reached: `gate.is_open` leaves the path closed, so the gate answers before the route.
    assert client.post("/api/oidc/exchange", json={"token": "x", "org": "a"}).status_code == 401


def _post(
    port: int, path: str, payload: dict[str, Any], *, cookie: str | None = None
) -> tuple[int, dict[str, str], bytes]:
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if cookie is not None:
        headers["Cookie"] = f"bajutsu_session={cookie}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _get(port: int, path: str, *, cookie: str) -> tuple[int, dict[str, str], bytes]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"Cookie": f"bajutsu_session={cookie}"}
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_the_gate_derives_identity_from_the_same_read_as_the_kind(tmp_path: Path) -> None:
    """The kind and the identity must come from one snapshot of the session.

    Asking separately let a session that stopped validating in between answer None to *both* —
    neither machine nor human — and the request was then served as the identity-less shared-token
    caller, which is full access. A human session refused by the role gate is what discriminates:
    the old code reached `actor_for` -> `identity()` for the login after already reading the
    principal, so a second read would show up here.
    """
    key = _key()
    state = _state(_threaded(tmp_path), tmp_path, key)
    assert state.repository is not None
    # An editor, on an admin-only path: the request is refused inside the gate and never reaches a
    # route handler, whose own `ctx.actor()` would otherwise read the identity legitimately and
    # make the count say nothing about the gate.
    state.repository.upsert_user(
        "dana", org_id="acme", github_login="dana", email="dana@x", role="editor"
    )
    sid = state.auth.issue_session("dana")

    reads: list[str] = []
    inner = state.auth.sessions

    class _Counting:
        """Wraps the store so the gate's own reads are visible, without changing any answer."""

        def __getattr__(self, name: str) -> Any:
            attribute = getattr(inner, name)
            if name not in ("principal", "identity", "valid"):
                return attribute

            def counted(*args: Any, **kwargs: Any) -> Any:
                reads.append(name)
                return attribute(*args, **kwargs)

            return counted

    state.auth.sessions = _Counting()
    server, port = _serve(state)
    try:
        code, _headers, _body = _post(port, "/api/config", {"path": "x"}, cookie=sid)
        assert code == 403, "an editor must be refused POST /api/config"
    finally:
        server.shutdown()
        server.server_close()

    assert reads.count("principal") == 1, reads
    assert "identity" not in reads, reads


def test_the_fastapi_gate_derives_identity_from_the_same_read_too(tmp_path: Path) -> None:
    """The same single-snapshot rule on the hosted backend. Both gates route through `gate.py`, so
    a divergence here is the failure BE-0253 put the policy there to prevent — and the human path
    is the one an earlier review found asserted for only one backend."""
    from fastapi.testclient import TestClient

    from bajutsu.serve.server.app import make_app

    key = _key()
    state = _state(_threaded(tmp_path), tmp_path, key)
    assert state.repository is not None
    state.repository.upsert_user(
        "dana", org_id="acme", github_login="dana", email="dana@x", role="editor"
    )
    sid = state.auth.issue_session("dana")

    reads: list[str] = []
    inner = state.auth.sessions

    class _Counting:
        def __getattr__(self, name: str) -> Any:
            attribute = getattr(inner, name)
            if name not in ("principal", "identity", "valid"):
                return attribute

            def counted(*args: Any, **kwargs: Any) -> Any:
                reads.append(name)
                return attribute(*args, **kwargs)

            return counted

    state.auth.sessions = _Counting()
    client = TestClient(make_app(state))
    client.cookies.set("bajutsu_session", sid)
    assert client.post("/api/config", json={"path": "x"}).status_code == 403

    assert reads.count("principal") == 1, reads
    assert "identity" not in reads, reads
