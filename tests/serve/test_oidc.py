"""Tests for OIDC token verification (BE-0414 unit 1).

Offline by construction: the key set is generated in-process and handed to `JwksCache` through its
injectable `fetch` seam, so every case — a good token, a forged algorithm, an unreachable issuer —
runs with no network and no Simulator, on the same fast gate as the rest of the Python core.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from bajutsu.serve.oidc import (
    GITHUB_ACTIONS,
    PROVIDERS,
    JwksCache,
    OidcConfig,
    OidcError,
    OidcProvider,
    verify,
)

ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "https://bajutsu.example.com"
_JWKS_URI = f"{ISSUER}/.well-known/jwks"


def _key(kid: str = "k1") -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": kid, "alg": "RS256"}, private=True)


def _fetcher(*keys: RSAKey, fail: bool = False) -> Any:
    """A `Fetch` returning the discovery document and the public key set of *keys*, counting calls
    so a test can assert the refresh floor actually bounds the outbound reads."""
    public = KeySet([RSAKey.import_key(k.as_dict(private=False)) for k in keys]).as_dict()

    def fetch(url: str) -> bytes:
        fetch.calls = getattr(fetch, "calls", 0) + 1  # type: ignore[attr-defined]
        if fail:
            raise OSError("issuer unreachable")
        if url.endswith("/.well-known/openid-configuration"):
            return json.dumps({"jwks_uri": _JWKS_URI}).encode()
        return json.dumps(public).encode()

    return fetch


def _config(**overrides: Any) -> OidcConfig:
    defaults: dict[str, Any] = {
        "provider": GITHUB_ACTIONS,
        "audience": AUDIENCE,
        "issuer": ISSUER,
        "max_token_age": 300,
        "session_ttl": 900,
    }
    return OidcConfig(**{**defaults, **overrides})


def _token(key: RSAKey, *, now: float | None = None, **claims: Any) -> str:
    now = int(time.time() if now is None else now)
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "jti": "jti-1",
        "repository": "acme/app",
        "repository_owner": "acme",
        "ref": "refs/heads/main",
        "job_workflow_ref": "acme/app/.github/workflows/e2e.yml@refs/heads/main",
        **claims,
    }
    return jwt.encode({"alg": "RS256", "kid": key.kid}, payload, key)


def test_a_well_formed_token_verifies_to_its_workload() -> None:
    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))
    result = verify(_token(key), _config(), cache)
    assert result.workload.repository == "acme/app"
    assert result.workload.ref == "refs/heads/main"
    assert result.workload.environment is None  # conditional claim, absent unless the job uses one
    assert result.jti == "jti-1"
    assert result.expires_at.tzinfo is not None


def test_the_immutable_subject_format_changes_nothing() -> None:
    """A repository on the immutable-subject rollout authorizes on its `repository` claim exactly
    as an older one does — the whole reason BE-0414 refuses to parse `sub`."""
    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))
    token = _token(key, sub="repo:acme@123456/app@456789:ref:refs/heads/main")
    assert verify(token, _config(), cache).workload.repository == "acme/app"


def test_a_token_for_another_audience_is_refused() -> None:
    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))
    with pytest.raises(OidcError):
        verify(_token(key, aud="https://someone-else.example.com"), _config(), cache)


def test_a_token_from_another_issuer_is_refused() -> None:
    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))
    with pytest.raises(OidcError):
        verify(_token(key, iss="https://evil.example.com"), _config(), cache)


def test_an_expired_token_is_refused() -> None:
    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))
    stale = time.time() - 10_000
    with pytest.raises(OidcError):
        verify(_token(key, now=stale), _config(), cache)


def test_a_token_older_than_the_ceiling_is_refused_while_still_unexpired() -> None:
    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))
    now = time.time()
    # `exp` is 300s past `iat`, so at +200s the token is live by its own clock but past a 60s
    # serve-side ceiling — the bound BE-0414 adds because no provider documents a lifetime.
    token = _token(key, now=now - 200)
    verify(token, _config(max_token_age=3600), cache)  # accepted under a generous ceiling
    with pytest.raises(OidcError, match="older than"):
        verify(token, _config(max_token_age=60), cache)


@pytest.mark.parametrize("jti", [None, "", 42, {"a": 1}])
def test_a_token_without_a_usable_jti_is_refused(jti: object) -> None:
    """Fail closed rather than skip the replay cache. The absent and empty forms are caught by the
    claims registry; a *numeric* one reaches `verify`'s own guard, which is the only thing between
    a token with no spendable identifier and an unbounded replay window."""
    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))
    with pytest.raises(OidcError):
        verify(_token(key, jti=jti), _config(), cache)


@pytest.mark.parametrize("iat", [None, "not-a-number"])
def test_a_token_without_a_usable_iat_is_refused(iat: object) -> None:
    """The serve-side age ceiling has nothing to measure from without one."""
    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))
    with pytest.raises(OidcError):
        verify(_token(key, iat=iat), _config(), cache)


def test_a_token_with_no_repository_claim_is_refused() -> None:
    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))
    with pytest.raises(OidcError, match="repository"):
        verify(_token(key, repository=None), _config(), cache)


def test_a_broken_signature_is_refused() -> None:
    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))
    token = _token(key)
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(OidcError):
        verify(tampered, _config(), cache)


def test_a_token_signed_by_an_unadvertised_key_is_refused() -> None:
    advertised, rogue = _key("k1"), _key("k2")
    cache = JwksCache(ISSUER, fetch=_fetcher(advertised))
    with pytest.raises(OidcError):
        verify(_token(rogue), _config(), cache)


def _claims(**overrides: Any) -> dict[str, Any]:
    """A claim set that passes every check but the signature, so a forgery test can only fail on
    the algorithm. Without this the forged tokens below were refused on a missing `jti` whether or
    not `verify` pinned an algorithm at all."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "jti": "forged",
        "repository": "acme/app",
        **overrides,
    }


def test_the_alg_none_forgery_is_refused() -> None:
    """An unsigned token naming `alg: none`, with an otherwise perfect claim set — so the refusal
    can only come from the algorithm allowlist."""
    import base64

    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "none", "kid": key.kid}).encode())
    unsigned = f"{header}.{b64(json.dumps(_claims()).encode())}."
    with pytest.raises(OidcError, match="signature"):
        verify(unsigned, _config(), cache)


def test_the_hs256_confusion_forgery_is_refused() -> None:
    """A token signed with HMAC over the issuer's *public* key bytes — the forgery an algorithm
    read from the token's own header would accept. Its claims are otherwise valid, so only the
    pinned RS256 allowlist can be what refuses it."""
    from joserfc.jwk import OctKey

    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))
    public_bytes = json.dumps(key.as_dict(private=False), sort_keys=True).encode()
    forged = jwt.encode(
        {"alg": "HS256", "kid": key.kid}, _claims(), OctKey.import_key(public_bytes)
    )
    with pytest.raises(OidcError, match="signature"):
        verify(forged, _config(), cache)


def test_a_garbled_token_is_refused_before_any_fetch() -> None:
    fetch = _fetcher(_key())
    cache = JwksCache(ISSUER, fetch=fetch)
    with pytest.raises(OidcError, match="well-formed"):
        verify("not-a-jwt", _config(), cache)
    assert getattr(fetch, "calls", 0) == 0


def test_an_unreachable_issuer_refuses_rather_than_verifying() -> None:
    cache = JwksCache(ISSUER, fetch=_fetcher(fail=True))
    with pytest.raises(OidcError, match="signing keys"):
        verify(_token(_key()), _config(), cache)


def test_an_expired_cache_that_cannot_refresh_refuses_rather_than_trusting_stale_keys() -> None:
    key = _key()
    calls: list[str] = []
    good = _fetcher(key)

    def fetch(url: str) -> bytes:
        calls.append(url)
        if len(calls) > 2:  # discovery + jwks succeed once, then the issuer goes away
            raise OSError("issuer unreachable")
        return bytes(good(url))

    cache = JwksCache(ISSUER, fetch=fetch, ttl=100, min_refresh=0)
    now = time.time()
    verify(_token(key, now=now), _config(), cache, now=now)
    with pytest.raises(OidcError, match="signing keys"):
        verify(_token(key, now=now + 200), _config(), cache, now=now + 200)


def test_an_unknown_kid_costs_one_fetch_per_refresh_interval() -> None:
    key = _key("k1")
    fetch = _fetcher(key)
    cache = JwksCache(ISSUER, fetch=fetch, min_refresh=60)
    now = time.time()
    verify(_token(key), _config(), cache, now=now)
    baseline = fetch.calls
    rogue = _key("unknown-kid")
    for offset in range(5):  # a flood of forged headers, all inside one refresh interval
        with pytest.raises(OidcError):
            verify(_token(rogue), _config(), cache, now=now + offset)
    assert fetch.calls == baseline  # no fetch at all this interval
    # Once the interval has elapsed one refresh is attempted — discovery + jwks — and that one
    # attempt then covers the next interval's worth of unknown ids too.
    for offset in (61, 62, 63):
        with pytest.raises(OidcError):
            verify(_token(rogue), _config(), cache, now=now + offset)
    assert fetch.calls - baseline == 2


def test_a_rotated_key_is_picked_up_on_the_next_refresh() -> None:
    old, new = _key("k1"), _key("k2")
    published = [old]

    def fetch(url: str) -> bytes:
        return bytes(_fetcher(*published)(url))

    cache = JwksCache(ISSUER, fetch=fetch, min_refresh=0)
    now = time.time()
    verify(_token(old), _config(), cache, now=now)
    published.append(new)
    assert verify(_token(new), _config(), cache, now=now).workload.repository == "acme/app"


def test_a_plain_http_issuer_is_refused() -> None:
    cache = JwksCache("http://token.example.com", fetch=_fetcher(_key()))
    with pytest.raises(OidcError, match="HTTPS"):
        verify(_token(_key()), _config(issuer="http://token.example.com"), cache)


def test_the_provider_table_maps_claim_names_without_touching_verification() -> None:
    """The seam BE-0414 leaves for a second CI platform: a provider is a table of claim names, so
    adding one changes no signature, lifetime, or replay check."""
    gitlab = OidcProvider(
        name="gitlab",
        issuer=ISSUER,
        repository_claim="project_path",
        environment_claim="environment",
        ref_claim="ref",
        workflow_ref_claim="ci_config_ref_uri",
    )
    key = _key()
    cache = JwksCache(ISSUER, fetch=_fetcher(key))
    token = _token(
        key,
        repository=None,
        project_path="acme/app",
        environment="production",
        ci_config_ref_uri="gitlab.example.com/acme/app//.gitlab-ci.yml@refs/heads/main",
    )
    workload = verify(token, _config(provider=gitlab), cache).workload
    assert workload.repository == "acme/app"
    assert workload.environment == "production"
    assert workload.workflow_ref.endswith(".gitlab-ci.yml@refs/heads/main")  # type: ignore[union-attr]


def test_github_actions_is_the_registered_default_provider() -> None:
    assert PROVIDERS["github-actions"] is GITHUB_ACTIONS
    assert GITHUB_ACTIONS.issuer == ISSUER


# --- the deployment's own configuration (BE-0414 unit 1) --------------------------------------


def test_no_audience_leaves_oidc_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset is off, never "accept whichever audience arrives" — the one check an operator must
    not be able to skip by omission."""
    from bajutsu.serve import _oidc_from_env

    monkeypatch.delenv("BAJUTSU_OIDC_AUDIENCE", raising=False)
    assert _oidc_from_env() is None
    monkeypatch.setenv("BAJUTSU_OIDC_AUDIENCE", "")
    assert _oidc_from_env() is None


def test_an_audience_configures_github_actions_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bajutsu.serve import _oidc_from_env

    monkeypatch.setenv("BAJUTSU_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.delenv("BAJUTSU_OIDC_PROVIDER", raising=False)
    monkeypatch.delenv("BAJUTSU_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("BAJUTSU_OIDC_MAX_TOKEN_AGE", raising=False)
    monkeypatch.delenv("BAJUTSU_OIDC_SESSION_TTL", raising=False)
    config = _oidc_from_env()
    assert config is not None
    assert config.provider is GITHUB_ACTIONS
    assert config.audience == AUDIENCE
    assert config.issuer == GITHUB_ACTIONS.issuer
    assert (config.max_token_age, config.session_ttl) == (300, 900)


def test_the_issuer_is_overridable_for_a_self_hosted_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub Enterprise Server issues from its own hostname, so the provider's default issuer is
    a default and not a constant."""
    from bajutsu.serve import _oidc_from_env

    monkeypatch.setenv("BAJUTSU_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("BAJUTSU_OIDC_ISSUER", "https://ghes.example.com/_services/token")
    config = _oidc_from_env()
    assert config is not None
    assert config.issuer == "https://ghes.example.com/_services/token"


def test_an_unknown_provider_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from bajutsu.serve import _oidc_from_env

    monkeypatch.setenv("BAJUTSU_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("BAJUTSU_OIDC_PROVIDER", "jenkins")
    with pytest.raises(ValueError, match="BAJUTSU_OIDC_PROVIDER"):
        _oidc_from_env()


def test_the_duration_knobs_are_parsed_defensively(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same variable-naming error the other operator-facing durations give, rather than a bare
    ValueError from deep inside a constructor."""
    from bajutsu.serve import _oidc_from_env

    monkeypatch.setenv("BAJUTSU_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("BAJUTSU_OIDC_MAX_TOKEN_AGE", "600")
    monkeypatch.setenv("BAJUTSU_OIDC_SESSION_TTL", "60")
    config = _oidc_from_env()
    assert config is not None
    assert (config.max_token_age, config.session_ttl) == (600, 60)

    for bad in ("5m", "abc", "1.5"):
        monkeypatch.setenv("BAJUTSU_OIDC_SESSION_TTL", bad)
        with pytest.raises(ValueError, match="BAJUTSU_OIDC_SESSION_TTL"):
            _oidc_from_env()
    for nonpositive in ("0", "-1"):
        monkeypatch.setenv("BAJUTSU_OIDC_SESSION_TTL", nonpositive)
        with pytest.raises(ValueError, match="positive"):
            _oidc_from_env()


def test_the_jwks_url_may_not_be_downgraded_to_plain_http() -> None:
    """The key set is the whole root of trust, so it may not be read over a channel an
    intermediary can rewrite — including when an HTTPS issuer's own discovery document points at
    one. Without this the `_https` guard on the second fetch can be deleted with the suite green."""

    def fetch(url: str) -> bytes:
        if url.endswith("/.well-known/openid-configuration"):
            return json.dumps({"jwks_uri": "http://evil.example.com/keys"}).encode()
        raise AssertionError("the plain-HTTP key set must never be fetched")

    cache = JwksCache(ISSUER, fetch=fetch)
    # Named, not folded into the generic "could not be fetched": a discovery document that points
    # the root of trust at plain HTTP is an attack or a misconfiguration, not a transient blip a
    # cached key set should paper over. The exchange still answers the caller its usual refusal.
    with pytest.raises(OidcError, match="HTTPS"):
        verify(_token(_key()), _config(), cache)


def test_a_live_key_set_survives_the_issuer_answering_with_an_error_body() -> None:
    """The failure the cache exists to absorb. `KeySet.import_key_set` reports a JSON error body
    as `KeyError`, not `ValueError`, so a narrow `except` let it escape as a 500 and skip the
    cached keys entirely — the one blip the fallback was written for."""
    key = _key()
    broken = {"on": False}

    def fetch(url: str) -> bytes:
        if url.endswith("/.well-known/openid-configuration"):
            return json.dumps({"jwks_uri": _JWKS_URI}).encode()
        if broken["on"]:
            return json.dumps({"error": "rate limited"}).encode()
        return bytes(_fetcher(key)(url))

    cache = JwksCache(ISSUER, fetch=fetch, ttl=600, min_refresh=0)
    now = time.time()
    verify(_token(key), _config(), cache, now=now)
    broken["on"] = True
    # A reload is forced (unknown kid) and fails; the still-live cached set must keep working.
    with pytest.raises(OidcError):
        verify(_token(_key("rogue")), _config(), cache, now=now + 1)
    assert verify(_token(key), _config(), cache, now=now + 2).workload.repository == "acme/app"


def test_a_cold_cache_and_an_unusable_key_set_refuse_rather_than_raise() -> None:
    """Same body, but with nothing cached: a clean refusal, never an unhandled 500."""

    def fetch(url: str) -> bytes:
        if url.endswith("/.well-known/openid-configuration"):
            return json.dumps({"jwks_uri": _JWKS_URI}).encode()
        return json.dumps({"error": "rate limited"}).encode()

    with pytest.raises(OidcError, match="signing keys"):
        verify(_token(_key()), _config(), JwksCache(ISSUER, fetch=fetch))


def test_a_discovery_document_naming_no_jwks_uri_refuses() -> None:
    cache = JwksCache(ISSUER, fetch=lambda _url: json.dumps({"issuer": ISSUER}).encode())
    with pytest.raises(OidcError, match="signing keys"):
        verify(_token(_key()), _config(), cache)


def test_a_cold_cache_stays_refused_for_the_refresh_interval_after_a_failure() -> None:
    """Deliberate, and the reason it is pinned here: the refresh floor bounds outbound fetches
    during an issuer outage, so recovery is visible only once the interval elapses. Refusing fast
    beats one 5s-timeout fetch per request, and the window is the operator's to tune."""
    key = _key()
    up = {"yes": False}

    def fetch(url: str) -> bytes:
        if not up["yes"]:
            raise OSError("issuer down")
        return bytes(_fetcher(key)(url))

    cache = JwksCache(ISSUER, fetch=fetch, min_refresh=60)
    now = time.time()
    with pytest.raises(OidcError, match="signing keys"):
        verify(_token(key), _config(), cache, now=now)
    up["yes"] = True
    with pytest.raises(OidcError, match="signing keys"):
        verify(_token(key), _config(), cache, now=now + 1)  # recovered, but inside the floor
    assert verify(_token(key), _config(), cache, now=now + 61).workload.repository == "acme/app"


def test_an_audience_configured_empty_is_refused_by_the_type() -> None:
    """Absence is what disables the caller shape, so a config object carrying an empty audience
    would be a deployment accepting whichever audience a token names."""
    with pytest.raises(ValueError, match="audience"):
        _config(audience="")
    for bad in ({"max_token_age": 0}, {"session_ttl": -1}):
        with pytest.raises(ValueError, match="positive"):
            _config(**bad)


def test_the_joserfc_probe_answers_without_importing_it() -> None:
    """The boot-time check that a deployment configuring OIDC can actually verify a token. It
    probes rather than imports, so the check itself cannot pull an `oauth`-extra dependency onto
    the default path the import guard protects."""
    import sys

    from bajutsu.serve import _joserfc_installed

    assert _joserfc_installed() is True  # the gate installs the oauth extra
    # `find_spec` raising (a shadowed or half-installed package) must read as "not installed"
    # rather than escaping into the boot path.
    original = sys.modules.get("joserfc")
    sys.modules["joserfc"] = None  # type: ignore[assignment]
    try:
        assert _joserfc_installed() is False
    finally:
        if original is None:
            del sys.modules["joserfc"]
        else:
            sys.modules["joserfc"] = original


def test_a_redirect_to_plain_http_is_refused_even_from_an_https_url() -> None:
    """`urlopen` follows redirects and permits an https -> http hop, so validating only the URL we
    ask for would leave the root of trust readable over a rewritable channel. The URL actually
    served is what has to be HTTPS."""
    from bajutsu.serve.oidc import _https

    with pytest.raises(OidcError, match="HTTPS"):
        _https("http://evil.example.com/keys")
    assert _https("https://token.example.com/keys") == "https://token.example.com/keys"
