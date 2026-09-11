"""Verify a continuous-integration (CI) job's OpenID Connect (OIDC) token (BE-0414 unit 1).

A CI job can complete no browser sign-in, and once GitHub OAuth is configured the shared token
reaches only worker traffic (BE-0313) — so a pipeline has no credential at all. This module is the
verifying half of the answer: the job presents the OIDC token its platform issues it, `serve`
verifies that token here, and the exchange endpoint mints a short-lived machine session in its
place. Verification runs exactly once per job, at the exchange, never per request.

**Provider-independent by construction.** Everything that decides whether a token is *genuine* —
the JSON Web Key Set (JWKS) discovery and cache, the RS256 signature, `iss` / `aud` / `exp` /
`nbf` / `iat`, the serve-side age ceiling, and the single-use `jti` — is the same for every
platform that issues OIDC tokens. What differs is only the *names* of the claims that say which
pipeline is calling: GitHub Actions writes `repository` and `job_workflow_ref`, GitLab writes
`project_path` and `ci_config_ref_uri`. `OidcProvider` is therefore a table of claim names, not a
class hierarchy, and `PROVIDERS` holds one entry per supported platform — which is what BE-0414's
Alternatives deferred when it scoped the item to GitHub Actions first.

`joserfc` ships only with the `oauth` extra, so it is imported inside `verify` rather than at
module scope — the same shape `bajutsu/common/github/app.py` uses for `cryptography` and
`GitHubOAuthClient` uses for `authlib`, and what keeps `import bajutsu.serve` free of it
(`tests/serve/test_import_guard.py`).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only; joserfc is an `oauth`-extra import
    from joserfc.jwk import KeySet

# Tolerate the same 60s of clock skew `bajutsu/common/github/app.py` backdates an App JWT by.
_CLOCK_SKEW_SECONDS = 60
# How long a fetched key set is trusted without re-reading it, and the floor between two fetches.
# The floor is what bounds the cost of an unknown `kid`: a stream of random ones costs one outbound
# fetch per interval rather than one per call.
_JWKS_TTL_SECONDS = 600
_JWKS_MIN_REFRESH_SECONDS = 60
# The gate runs synchronously in the stdlib backend, so an unresponsive issuer must not hold a
# worker thread open indefinitely.
_FETCH_TIMEOUT_SECONDS = 5

# "GET this URL, return the body" — injectable so the whole flow tests offline, mirroring the
# `Fetch` seam `bajutsu/common/github/app.py` already uses for the App-token exchange.
Fetch = Callable[[str], bytes]

_logger = logging.getLogger(__name__)


class OidcError(Exception):
    """An OIDC token was refused. The message names the check that failed, for the operator log —
    the caller answers the pipeline with a generic refusal rather than echoing it back."""


@dataclass(frozen=True)
class WorkloadClaims:
    """Which pipeline presented the token, in provider-independent terms.

    Attributes:
        repository: The `"<owner>/<repo>"` name an org's `allowedRepositories` lists.
        environment: The deployment environment the job declared, or None when it declared none.
            Conditional on every provider that emits it at all, which is why a configured bound
            must refuse an absent claim as firmly as a differing one.
        ref: The git ref the run was triggered on.
        workflow_ref: The workflow definition the job ran from (GitHub Actions'
            `job_workflow_ref`), which pins the file rather than the repository.
    """

    repository: str
    environment: str | None = None
    ref: str | None = None
    workflow_ref: str | None = None


@dataclass(frozen=True)
class OidcProvider:
    """One CI platform's OIDC dialect: its issuer and the claims that name the calling pipeline.

    A table rather than a subclass because every provider difference seen so far is a claim
    *name* — the signature, lifetime and replay checks are identical across platforms.
    """

    name: str
    issuer: str
    repository_claim: str
    environment_claim: str | None = None
    ref_claim: str | None = None
    workflow_ref_claim: str | None = None

    def workload(self, claims: Mapping[str, Any]) -> WorkloadClaims:
        """Map this provider's raw claims onto the provider-independent shape.

        Raises:
            OidcError: The token carries no usable repository claim, so there is nothing for
                `allowedRepositories` to match and the exchange cannot resolve an org.
        """
        repository = _text(claims.get(self.repository_claim))
        if repository is None:
            raise OidcError(f"the token carries no {self.repository_claim!r} claim")
        return WorkloadClaims(
            repository=repository,
            environment=_text(claims.get(self.environment_claim))
            if self.environment_claim
            else None,
            ref=_text(claims.get(self.ref_claim)) if self.ref_claim else None,
            workflow_ref=(
                _text(claims.get(self.workflow_ref_claim)) if self.workflow_ref_claim else None
            ),
        )


GITHUB_ACTIONS = OidcProvider(
    name="github-actions",
    issuer="https://token.actions.githubusercontent.com",
    # The discrete claims, never a parse of `sub`: `sub`'s format already changed for repositories
    # created after 15 July 2026 (immutable numeric ids), and a repository can redefine which
    # claims compose it through `include_claim_keys` — so its shape is not serve's to assume.
    repository_claim="repository",
    environment_claim="environment",
    ref_claim="ref",
    workflow_ref_claim="job_workflow_ref",
)

#: Every CI platform `serve` can verify a token from, by the name `BAJUTSU_OIDC_PROVIDER` selects.
PROVIDERS: Mapping[str, OidcProvider] = {GITHUB_ACTIONS.name: GITHUB_ACTIONS}


@dataclass(frozen=True)
class OidcConfig:
    """The deployment's OIDC settings, present only once an operator configures an audience.

    Attributes:
        provider: The platform whose tokens this deployment accepts.
        audience: The `aud` a token must carry. The workflow chooses its own audience, so a
            deployment that accepted any would accept a token minted for an unrelated service —
            which is why there is no default and no expected audience disables OIDC outright.
        issuer: The `iss` a token must carry, normally the provider's own. Overridable for a
            self-hosted installation (GitHub Enterprise Server issues from its own hostname).
        max_token_age: Seconds since `iat` past which a token is refused regardless of its own
            `exp` — the providers document no numeric lifetime, so this is serve's own bound.
        session_ttl: Seconds a minted machine session lives, never beyond the token's own `exp`.
    """

    provider: OidcProvider
    audience: str
    issuer: str
    max_token_age: int
    session_ttl: int

    def __post_init__(self) -> None:
        # The env parser enforces these on the one production path, but the type is what every
        # other construction site — a test, a future CLI flag — is checked against. An empty
        # audience matters most: absence is what *disables* the caller shape, so a config object
        # that carries one is a deployment accepting whichever audience a token happens to name.
        if not self.audience:
            raise ValueError("an OIDC audience is required; an unset one disables OIDC instead")
        if self.max_token_age <= 0 or self.session_ttl <= 0:
            raise ValueError("OIDC max_token_age and session_ttl must both be positive")


@dataclass(frozen=True)
class VerifiedToken:
    """A token that passed every check, reduced to what the exchange needs from it."""

    workload: WorkloadClaims
    jti: str
    expires_at: datetime


class JwksCache:
    """The issuer's signing keys, discovered once and re-read on a bounded schedule.

    Shared across requests and therefore across threads (the stdlib backend serves each request on
    its own), so every read of the cached set is taken under the lock.
    """

    def __init__(
        self,
        issuer: str,
        *,
        fetch: Fetch | None = None,
        ttl: int = _JWKS_TTL_SECONDS,
        min_refresh: int = _JWKS_MIN_REFRESH_SECONDS,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._fetch = fetch if fetch is not None else _fetch_url
        self._ttl = ttl
        self._min_refresh = min_refresh
        self._lock = threading.Lock()
        self._keys: KeySet | None = None
        self._fetched_at = 0.0
        self._attempted_at = float("-inf")

    def keys(self, kid: str | None, *, now: float) -> KeySet:
        """The key set to verify against, refreshing it when it is stale or lacks *kid*.

        A refresh is attempted at most once per refresh interval, so an unknown `kid` — the shape a
        flood of forged headers takes — costs one outbound fetch per interval rather than one per
        call. When the cached set is still live it is returned even if the floor blocked the
        refresh; the signature check then fails on the missing key, which is the same refusal.

        Raises:
            OidcError: There is no live key set and none could be fetched, so the token is refused
                rather than verified against an expired one.
        """
        with self._lock:
            cached = self._keys
            if (
                cached is not None
                and now - self._fetched_at < self._ttl
                and (kid is None or _carries(cached, kid))
            ):
                return cached
            if now - self._attempted_at >= self._min_refresh:
                self._attempted_at = now
                self._reload(now)
            refreshed = self._keys
            if refreshed is None or now - self._fetched_at >= self._ttl:
                raise OidcError("the issuer's signing keys could not be fetched")
            return refreshed

    def _reload(self, now: float) -> None:
        """Re-read the issuer's key set, leaving the previous one in place on failure — a live
        cache outlasts a blip, and an expired one is refused by `keys` rather than trusted."""
        from joserfc.errors import JoseError
        from joserfc.jwk import KeySet

        try:
            document = json.loads(
                self._fetch(_https(f"{self._issuer}/.well-known/openid-configuration"))
            )
            jwks_uri = _text(document.get("jwks_uri")) if isinstance(document, dict) else None
            if jwks_uri is None:
                _logger.warning("the OIDC issuer's discovery document names no 'jwks_uri'")
                return
            self._keys = KeySet.import_key_set(json.loads(self._fetch(_https(jwks_uri))))
            self._fetched_at = now
        except (OSError, LookupError, TypeError, ValueError, JoseError) as e:
            # A network failure, a non-JSON body, or a body that is well-formed JSON but not a
            # key set all mean the same thing here: nothing new to trust, so `keys` decides
            # whether what we already hold still counts. The tuple is
            # this wide because `KeySet.import_key_set` reports a bad body as `KeyError`,
            # `TypeError`, or a `JoseError` — none of which is a `ValueError`, so catching only
            # the obvious ones let an issuer answering with a JSON *error* body escape as a 500
            # and skip the very cache this fallback exists to use.
            _logger.warning(
                "could not refresh the OIDC issuer's key set: %s: %s", type(e).__name__, e
            )
            return


def verify(
    token: str, config: OidcConfig, cache: JwksCache, *, now: float | None = None
) -> VerifiedToken:
    """Verify *token* against *config* and return what the exchange needs from it.

    Every check runs here, once, so no later request in the pipeline re-presents the token: the
    RS256 signature against the issuer's keys, the configured issuer and audience, the token's own
    lifetime with a small skew allowance, and the presence of the `iat` and `jti` the age ceiling
    and the replay cache measure from. Spending the `jti` is the caller's job — it needs the
    system of record, which this module deliberately does not reach.

    Raises:
        OidcError: Any check failed. The token is refused; nothing partial is returned.
    """
    from joserfc import jwt
    from joserfc.errors import JoseError

    if now is None:
        now = time.time()
    kid = _text(_header(token).get("kid"))
    keys = cache.keys(kid, now=now)
    try:
        # The algorithm comes from configuration, never from the token: an explicit RS256 allowlist
        # is what refuses the classic `alg: none` and HS256-confusion forgeries, where a header
        # names a symmetric algorithm and the "secret" is the issuer's own public key bytes.
        decoded = jwt.decode(token, keys, algorithms=["RS256"])
    except JoseError as e:
        raise OidcError(f"the token's signature could not be verified: {e}") from e
    claims = decoded.claims
    try:
        jwt.JWTClaimsRegistry(
            now=int(now),
            leeway=_CLOCK_SKEW_SECONDS,
            iss={"essential": True, "value": config.issuer},
            aud={"essential": True, "value": config.audience},
            exp={"essential": True},
            iat={"essential": True},
            jti={"essential": True},
        ).validate(claims)
    except JoseError as e:
        raise OidcError(f"the token's claims were refused: {e}") from e
    issued_at, expires = _epoch(claims, "iat"), _epoch(claims, "exp")
    if now - issued_at > config.max_token_age + _CLOCK_SKEW_SECONDS:
        # Independent of `exp`: no provider documents a numeric token lifetime, so a captured token
        # would otherwise be replayable for a window serve does not choose.
        raise OidcError("the token is older than this deployment accepts")
    jti = _text(claims.get("jti"))
    if jti is None:
        # Fail closed rather than skipping the replay cache: without an identifier to spend, a
        # captured token is replayable for its whole remaining window.
        raise OidcError("the token carries no 'jti' claim")
    return VerifiedToken(
        workload=config.provider.workload(claims),
        jti=jti,
        expires_at=datetime.fromtimestamp(expires, tz=UTC),
    )


def _carries(keys: KeySet, kid: str) -> bool:
    """Whether *keys* holds the key *kid* names — the question that decides a bounded refresh."""
    from joserfc.errors import JoseError

    try:
        keys.get_by_kid(kid)
    except JoseError:
        return False
    return True


def _epoch(claims: Mapping[str, Any], name: str) -> float:
    """A numeric date claim the registry already proved present, as a float."""
    value = claims.get(name)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise OidcError(f"the token's {name!r} claim is not a number")
    return float(value)


def _header(token: str) -> dict[str, Any]:
    """The token's unverified JOSE header, read only to pick the key its `kid` names.

    Nothing here is trusted: the signature is checked against that key afterwards, and the
    algorithm is taken from configuration rather than from this header.
    """
    segment = token.partition(".")[0]
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        header = json.loads(raw)
    except (ValueError, binascii.Error) as e:
        raise OidcError("the token is not a well-formed JWT") from e
    if not isinstance(header, dict):
        raise OidcError("the token is not a well-formed JWT")
    return header


def _text(value: object) -> str | None:
    """A claim as a non-empty string, or None — a claim of any other type is treated as absent
    rather than coerced, so a bound never matches a number that stringifies to its value."""
    return value if isinstance(value, str) and value else None


def _https(url: str) -> str:
    """Refuse a non-HTTPS issuer or `jwks_uri`: the key set is the whole root of trust here, so it
    may not be read over a channel an intermediary can rewrite."""
    if not url.startswith("https://"):
        raise OidcError(f"the OIDC issuer must be served over HTTPS, got {url!r}")
    return url


def _fetch_url(url: str) -> bytes:
    """The real discovery/JWKS read. `_https` has already rejected a non-HTTPS URL.

    The URL actually served is re-checked, because `urlopen` follows redirects and its redirect
    handler permits an `https` → `http` hop: validating only the URL we asked for would leave the
    root of trust readable over a channel an intermediary can rewrite, which is exactly what
    `_https` is there to prevent.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
        _https(response.url)
        return bytes(response.read())
