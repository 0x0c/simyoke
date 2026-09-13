"""`bajutsu serve` — a local web UI to author scenarios and run them.

A Tier-1 convenience (authoring / operation), **never part of the CI gate**.  Two top-level
tabs over the CLI: **Record** authors a scenario from a natural-language goal (``python -m
bajutsu record …``), streaming the agent's turn-by-turn progress and writing the result under
the scenarios dir; **Replay** runs a scenario (``python -m bajutsu run …``) and shows its
self-contained ``report.html``.  Each request spawns the CLI on a background thread, streams
its output, and the produced ``runs/<id>/`` tree is served so the report's relative asset
links resolve.  The default transport is stdlib-only — the same ``ThreadingHTTPServer`` approach
as the network collector ([[network]]); ``--asgi`` instead serves the same UI/API as a FastAPI app
over uvicorn (the ``server`` extra), the transport the hosted backend uses (BE-0015).

Split into submodules:

* **helpers** — pure query/path/validation functions (no server state)
* **commands** — CLI command builders (the ``python -m bajutsu …`` argv a request spawns)
* **state** — the ``ServeState`` container and its ``Job``/``StoreBundle`` value types
* **jobs** — the run/cancel/build execution lifecycle that mutates a ``Job``
* **handler** — HTTP request handler, ``make_server``, and the embedded SPA
"""

from __future__ import annotations

import logging
import os
import sys
from functools import partial
from pathlib import Path
from typing import Any

from bajutsu.common.config_source import _bajutsu_cache_root
from bajutsu.common.run_meta.object_store import EvidenceTarget
from bajutsu.serve import gate, oplog
from bajutsu.serve.artifacts import Artifact, ArtifactStore, LocalArtifactStore
from bajutsu.serve.batch_bootstrap import bajutsu_source_root, register_batch_providers
from bajutsu.serve.commands import (
    _int,
    crawl_command,
    record_command,
    run_command,
    triage_command,
)
from bajutsu.serve.executor import LocalExecutor, RunExecutor
from bajutsu.serve.handler import make_server
from bajutsu.serve.helpers import (
    _scenario_path,
    list_crawl_runs,
    list_fs,
    list_runs,
    list_scenarios,
    list_simulators,
    list_targets,
    load_serve_config_file,
    mask_secret,
    parse_byte_range,
    range_reply,
    scenario_out_path,
    target_build_info,
    target_scenarios_dir,
    unique_scenario_path,
    valid_backend,
    valid_run_id,
    valid_udid,
)
from bajutsu.serve.jobs import cancel_job, run_job
from bajutsu.serve.launchagent import launchagent_plist
from bajutsu.serve.logbus import InMemoryLogBus, LogBus
from bajutsu.serve.oidc import GITHUB_ACTIONS, PROVIDERS, OidcConfig
from bajutsu.serve.operations.config import (
    restore_org_binding,
    restore_persisted_provider_settings,
    seed_orgs_from_bound_config,
)
from bajutsu.serve.orgs import DEFAULT_ORG
from bajutsu.serve.provider_store import LocalProviderSettingsStore
from bajutsu.serve.scenarios import (
    LocalScenarioScope,
    LocalScenarioStore,
    ScenarioScope,
    ScenarioStore,
)
from bajutsu.serve.secrets import SecretStore
from bajutsu.serve.sessions import InMemorySessionStore
from bajutsu.serve.state import (
    Job,
    Popen,
    ProviderSettingsManager,
    ServeState,
    SessionManager,
    StoreBundle,
)

__all__ = [
    "SERVE_BACKENDS",
    "Artifact",
    "ArtifactStore",
    "InMemoryLogBus",
    "Job",
    "LocalArtifactStore",
    "LocalExecutor",
    "LocalScenarioScope",
    "LocalScenarioStore",
    "LogBus",
    "MissingServerExtra",
    "Popen",
    "RunExecutor",
    "ScenarioScope",
    "ScenarioStore",
    "ServeState",
    "_int",
    "_scenario_path",
    "cancel_job",
    "crawl_command",
    "launchagent_plist",
    "list_crawl_runs",
    "list_fs",
    "list_runs",
    "list_scenarios",
    "list_simulators",
    "list_targets",
    "load_serve_config_file",
    "make_server",
    "mask_secret",
    "parse_byte_range",
    "range_reply",
    "record_command",
    "run_command",
    "run_job",
    "scenario_out_path",
    "serve",
    "target_build_info",
    "target_scenarios_dir",
    "triage_command",
    "unique_scenario_path",
    "valid_backend",
    "valid_run_id",
    "valid_udid",
]


# The serve backends `_build_state` can assemble — the source of truth the CLI validates against.
# `local` runs in-process; `server` wires the hosted seams (DB-backed queue/log bus + object storage).
SERVE_BACKENDS: tuple[str, ...] = ("local", "server")

_logger = logging.getLogger(__name__)


def _session_ttl_from_env(raw: str | None, default: int) -> int:
    """Parse ``BAJUTSU_SESSION_TTL`` (seconds) defensively — operator-facing config deserves a clear
    error, not a bare ValueError. Unset/empty falls back to *default*; non-integer or non-positive
    values are rejected with a message naming the variable."""
    if not raw:
        return default
    try:
        ttl = int(raw)
    except ValueError:
        raise ValueError(
            f"BAJUTSU_SESSION_TTL must be a whole number of seconds, got {raw!r}"
        ) from None
    if ttl <= 0:
        raise ValueError(f"BAJUTSU_SESSION_TTL must be a positive number of seconds, got {ttl}")
    return ttl


def _max_concurrent_from_env(raw: str | None, *, var: str) -> int:
    """Parse a non-negative concurrency cap from *var* (unset/empty/0 = unlimited). A clear error
    beats a bare ValueError for operator-facing config; negatives and non-integers are rejected.
    Shared by the per-user (BE-0015 7c-3) and per-org (BE-0016 Tier B fairness) caps."""
    if not raw:
        return 0
    try:
        n = int(raw)
    except ValueError:
        raise ValueError(f"{var} must be a whole number, got {raw!r}") from None
    if n < 0:
        raise ValueError(f"{var} must be >= 0, got {n}")
    return n


_DEFAULT_RUN_RETENTION_DAYS = 30


def _run_retention_from_env(raw: str | None) -> int:
    """Parse ``BAJUTSU_RUN_RETENTION_DAYS`` — days a soft-deleted run stays in the trash before the
    lazy sweep purges it (BE-0239). Unset/empty falls back to the 30-day default; a value <= 0
    disables the automatic purge (trash kept until a manual purge); a non-integer is rejected with a
    message naming the variable, like the other operator-facing knobs."""
    if not raw:
        return _DEFAULT_RUN_RETENTION_DAYS
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"BAJUTSU_RUN_RETENTION_DAYS must be a whole number of days, got {raw!r}"
        ) from None


_DEFAULT_OIDC_MAX_TOKEN_AGE = 300  # seconds since `iat` past which a token is refused
_DEFAULT_OIDC_SESSION_TTL = 900  # seconds a machine session lives, capped by the token's `exp`


def _oidc_from_env() -> OidcConfig | None:
    """The deployment's OIDC settings, or None when it configures no expected audience (BE-0414).

    The audience is the one check an operator must not skip: a workflow chooses its own, so a
    deployment accepting any would accept a token minted for an unrelated service. Unset therefore
    disables the whole caller shape rather than defaulting to "accept whatever arrives" — the
    endpoint is not even opened (`gate.is_open`). The issuer defaults to the selected provider's
    own and is overridable for a self-hosted installation (GitHub Enterprise Server).
    """
    audience = os.environ.get("BAJUTSU_OIDC_AUDIENCE")
    if not audience:
        return None
    # `or`, not a `get` default: `get` substitutes only when the variable is *absent*, so a
    # blank `BAJUTSU_OIDC_PROVIDER=` — how a docker-compose entry or an unset Helm value
    # arrives — would reach `PROVIDERS.get("")` and fail the boot telling the operator the
    # value they deliberately left empty is wrong. Every sibling read here treats empty as unset.
    name = os.environ.get("BAJUTSU_OIDC_PROVIDER") or GITHUB_ACTIONS.name
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ValueError(f"BAJUTSU_OIDC_PROVIDER must be one of {sorted(PROVIDERS)}, got {name!r}")
    return OidcConfig(
        provider=provider,
        audience=audience,
        issuer=os.environ.get("BAJUTSU_OIDC_ISSUER") or provider.issuer,
        max_token_age=_positive_seconds_from_env(
            "BAJUTSU_OIDC_MAX_TOKEN_AGE", _DEFAULT_OIDC_MAX_TOKEN_AGE
        ),
        session_ttl=_positive_seconds_from_env(
            "BAJUTSU_OIDC_SESSION_TTL", _DEFAULT_OIDC_SESSION_TTL
        ),
    )


def _positive_seconds_from_env(var: str, default: int) -> int:
    """A positive whole number of seconds from *var* — the same defensive parse, and the same
    variable-naming error, the other operator-facing duration knobs above get."""
    raw = os.environ.get(var)
    if not raw:
        return default
    try:
        seconds = int(raw)
    except ValueError:
        raise ValueError(f"{var} must be a whole number of seconds, got {raw!r}") from None
    if seconds <= 0:
        raise ValueError(f"{var} must be a positive number of seconds, got {seconds}")
    return seconds


def _joserfc_installed() -> bool:
    """Whether the JOSE library the OIDC exchange verifies with is importable (BE-0414).

    Checked without importing it, so the boot-time probe does not itself pull an `oauth`-extra
    dependency onto the default path the import guard protects.
    """
    from importlib.util import find_spec

    try:
        return find_spec("joserfc") is not None
    except (ImportError, ValueError):  # a partially-installed or shadowed package
        return False


class MissingServerExtra(ImportError):
    """A server-backend optional extra (FastAPI/uvicorn, the database, object storage) is not
    installed.

    Carries the install hint and lets the CLI exit cleanly. Distinct from a plain ImportError so a
    genuine `bajutsu.*` import bug is never mistaken for a missing dependency and swallowed."""


def _build_state(
    *,
    runs_dir: Path,
    config: Path | None,
    scenarios_dir: Path | None,
    root: Path | None,
    baselines_dir: Path | None,
    max_concurrent: int,
    token: str | None,
    upload_exec: str = "sandbox",
    evidence: EvidenceTarget | None = None,
    allow_remote_build: bool = False,
    backend: str = "local",
    cwd: Path | None = None,
    config_provenance: dict[str, str] | None = None,
    themes_dir: Path | None = None,
    default_theme: str | None = None,
) -> ServeState:
    """Assemble the `ServeState` for *backend* — the one place the serve seams are wired.

    ``local`` runs in-process (thread executor, in-memory log bus, filesystem artifacts, on-disk
    scenarios). ``server`` wires the hosted seams (a database-backed queue + log bus, object-storage
    artifacts + scenarios) from the environment. An unknown backend fails loudly rather than
    silently falling back to local. The transport (stdlib vs uvicorn) is a separate choice."""
    if backend not in SERVE_BACKENDS:
        raise ValueError(
            f"unknown serve backend: {backend!r} (available: {', '.join(SERVE_BACKENDS)})"
        )
    resolved_baselines = baselines_dir or (
        scenarios_dir / "baselines" if scenarios_dir else Path("baselines")
    )
    if backend == "server":
        # The server backend's seams (FastAPI/uvicorn, object storage, the database) live behind the
        # optional extras and import lazily. If any is missing, surface one clear install hint
        # rather than a raw ImportError naming whichever module happened to load first.
        try:
            return _build_server_state(
                runs_dir=runs_dir,
                config=config,
                scenarios_dir=scenarios_dir,
                root=root or Path.cwd(),
                baselines_dir=resolved_baselines,
                max_concurrent=max_concurrent,
                token=token,
                upload_exec=upload_exec,
                evidence=evidence,
                allow_remote_build=allow_remote_build,
            )
        except ImportError as e:
            # Only a missing third-party extra earns the install hint. A failed `bajutsu.*` import is
            # a real bug, not a missing dependency — re-raise it so its own traceback survives.
            if e.name and e.name.split(".")[0] == "bajutsu":
                raise
            raise MissingServerExtra(
                "the server backend needs its optional extras — install with: "
                "pip install 'bajutsu[server,worker,db,gcs]' (gcs is only needed if "
                "BAJUTSU_SERVER_STORE or --evidence-store uses gs://)"
            ) from e
    return ServeState(
        runs_dir=runs_dir,
        config=config,
        scenarios_dir=scenarios_dir,
        root=root or Path.cwd(),
        baselines_dir=resolved_baselines,
        # Uploaded bundles (BE-0073) extract under the shared Bajutsu cache root — serve-owned,
        # never the browse `--root`, so an uploaded tree can't overwrite the operator's files. A
        # sibling of the Git source's own checkout cache (BE-0243), so a hosted deployment that
        # already provisions one writable cache root for Git needn't provision a second for uploads.
        uploads_dir=_bajutsu_cache_root() / "uploads",
        max_concurrent=max_concurrent,
        auth=SessionManager(token=token),
        upload_exec=upload_exec,
        evidence=evidence,
        allow_remote_build=allow_remote_build,
        run_retention_days=_run_retention_from_env(os.environ.get("BAJUTSU_RUN_RETENTION_DAYS")),
        cwd=cwd or Path.cwd(),
        config_provenance=config_provenance,
        themes_dir=themes_dir,
        default_theme=default_theme,
        # Persist the Settings panel's provider/model/effort to a serve-owned file (a sibling of
        # runs_dir), so a saved choice survives a restart (BE-0184). Construction only wires the
        # store; `serve()` restores from it on boot, once logging is live.
        providers=ProviderSettingsManager(
            store=LocalProviderSettingsStore(runs_dir.parent / "provider-settings.json")
        ),
    )


def _build_server_state(
    *,
    runs_dir: Path,
    config: Path | None,
    scenarios_dir: Path | None,
    root: Path,
    baselines_dir: Path,
    max_concurrent: int,
    token: str | None,
    upload_exec: str = "sandbox",
    evidence: EvidenceTarget | None = None,
    allow_remote_build: bool = False,
) -> ServeState:
    """Wire the hosted seams from the environment (the single-tenant server backend, BE-0015/BE-0106).

    Postgres (``BAJUTSU_DATABASE_URL``) backs the job queue, sessions, and the system of record
    (BE-0106); one bucket named by ``BAJUTSU_SERVER_STORE`` (``s3://bucket/prefix`` or
    ``gs://bucket/prefix``, BE-0204) holds artifacts and scenarios — the same URI-based selector
    ``--evidence-store`` uses, kept as its own independent setting. Server extras (SQLAlchemy,
    boto3, google-cloud-storage) are imported lazily so the default path stays SDK-free."""
    import os

    from bajutsu.serve.authz import ADMIN_TEAM_ENTRY_RE, admin_teams_unusable
    from bajutsu.serve.server.artifacts import ObjectStorageArtifactStore
    from bajutsu.serve.server.baselines import ObjectBaselineStore
    from bajutsu.serve.server.db import engine_from_url, repository_from_env
    from bajutsu.serve.server.db_executor import DbQueueExecutor
    from bajutsu.serve.server.oauth import GitHubOAuthClient
    from bajutsu.serve.server.object_store import artifact_prefix, object_store_from_env, org_prefix
    from bajutsu.serve.server.post_completion_logbus import PostCompletionLogBus
    from bajutsu.serve.server.provider_store import DbProviderSettingsStore
    from bajutsu.serve.server.scenarios import (
        LocalTreeScenarioStorage,
        ObjectScenarioStorage,
        StorageScenarioStore,
    )
    from bajutsu.serve.server.secrets import DbSecretStore, fernet_from_env
    from bajutsu.serve.server.sessions import _DEFAULT_TTL, SqlSessionStore

    server_store = object_store_from_env()
    if server_store is None:
        raise ValueError(
            "BAJUTSU_SERVER_STORE is required for --backend=server "
            "(e.g. s3://bucket/prefix or gs://bucket/prefix)"
        )
    store, prefix = server_store
    db_url = os.environ.get("BAJUTSU_DATABASE_URL")
    _db_engine = engine_from_url(db_url) if db_url else None
    # The master key that encrypts operator secrets at rest (BE-0136). A database-backed server
    # persists secrets in the `secrets` table, so it must be provisioned — fail loudly at startup
    # rather than silently degrade to plaintext-in-memory. Without a database, secrets stay in the
    # process env (the local shape), so no key is needed.
    _secrets_fernet = fernet_from_env()
    if _db_engine is not None and _secrets_fernet is None:
        raise ValueError("BAJUTSU_SECRETS_KEY is required for --backend=server with a database")
    # GitHub OAuth login is optional: wired only when all three OAuth vars are set, else None (token
    # auth only). Once configured, sign-in and the viewer/editor role follow GitHub org/Team
    # membership (BE-0313); `BAJUTSU_OAUTH_ADMIN_TEAMS` names the server-wide admin Teams (a member of
    # any of them also clears the sign-in gate itself, so an admin can still sign in and repoint a
    # broken `orgs:` config).
    cid = os.environ.get("BAJUTSU_OAUTH_GITHUB_CLIENT_ID")
    secret = os.environ.get("BAJUTSU_OAUTH_GITHUB_CLIENT_SECRET")
    redirect = os.environ.get("BAJUTSU_OAUTH_GITHUB_REDIRECT_URI")
    oauth = (
        GitHubOAuthClient(client_id=cid, client_secret=secret, redirect_uri=redirect)
        if cid and secret and redirect
        else None
    )
    oauth_admin_teams = tuple(
        t.strip() for t in os.environ.get("BAJUTSU_OAUTH_ADMIN_TEAMS", "").split(",") if t.strip()
    )
    # Collected alongside each `print` below (nothing is configured to route through `oplog` this
    # early) so `serve()` can re-emit them once logging is live — see `ServeState.startup_warnings`.
    # Each entry pairs a stable *check* name with the human-readable *msg*, so an operator's alert can
    # key on `check=` -- a plain structured field `oplog` passes through as-is, not one it validates
    # the way it validates an `event` name -- instead of substring-matching free text that rewords
    # out from under it (BE-0352).
    startup_warnings: list[tuple[str, str]] = []

    def _warn(check: str, msg: str) -> None:
        # Print *and* collect in one call, so a later check can't do one without the other and
        # leave `server.startup_warning` silently missing a condition -- stderr would still show
        # the warning (a manual check passes) while the oplog-keyed alert never fires for it.
        print(f"bajutsu serve: {msg}", file=sys.stderr)  # noqa: T201
        startup_warnings.append((check, msg))

    # A half-configured deployment (one of the three BAJUTSU_OAUTH_GITHUB_* vars unset) falls back
    # to token auth silently -- or, with no BAJUTSU_SERVE_TOKEN either, to no auth at all -- while
    # the operator believes OAuth is gating it. This check is deliberately the opposite gate from
    # the three below it, which each read `oauth is None` as "token-auth-only, deliberately". Name
    # which of the two shapes this deployment fell into, since *token* is already a parameter here
    # (BE-0352).
    if oauth is None and any((cid, secret, redirect)):
        # Name which variable(s) are unset -- the three are all in scope here, and a typo'd variable
        # NAME (e.g. _REDIRECT_URL for _REDIRECT_URI) leaves all three look present in a quick .env
        # read; naming the actual unset one ends that hunt on the first line of the log.
        unset = [
            name
            for name, value in (
                ("BAJUTSU_OAUTH_GITHUB_CLIENT_ID", cid),
                ("BAJUTSU_OAUTH_GITHUB_CLIENT_SECRET", secret),
                ("BAJUTSU_OAUTH_GITHUB_REDIRECT_URI", redirect),
            )
            if not value
        ]
        fallback = (
            "the shared-token login is enabled instead"
            if token is not None
            else "no token is configured either, so the request gate is skipped entirely and every "
            "endpoint is served unauthenticated"
        )
        msg = (
            "GitHub OAuth is only partly configured — all three of BAJUTSU_OAUTH_GITHUB_CLIENT_ID, "
            f"_CLIENT_SECRET and _REDIRECT_URI must be set, but {', '.join(unset)} "
            f"{'is' if len(unset) == 1 else 'are'} unset; OAuth is off, so every GitHub sign-in "
            f"will 404 and {fallback}"
        )
        _warn("oauth_half_configured", msg)
    # Never lose an admin quietly to the rename itself: a deployment that adds the new plural name
    # but leaves the old singular one set (the likelier migration mistake — an operator remembers
    # one Team, not that the old name must go) has that Team silently drop out of both the role and
    # the sign-in gate, with `oauth_admin_teams` non-empty so the check below never fires. This check
    # is independent of whether the list ends up empty, so it runs even when it isn't.
    if oauth is not None and os.environ.get("BAJUTSU_OAUTH_ADMIN_TEAM"):
        msg = (
            "BAJUTSU_OAUTH_ADMIN_TEAM is retired and no longer read — rename it to "
            "BAJUTSU_OAUTH_ADMIN_TEAMS (comma-separated), folding in every Team it named"
        )
        _warn("admin_team_retired_name", msg)
    # Never lose every admin quietly. An empty list with OAuth wired means no login can ever
    # resolve to admin — whether because the rename's hard cutover (BE-0313's own precedent for
    # retiring BAJUTSU_OAUTH_ADMINS) left only the retired singular name set, or because the new
    # name was simply never set at all — and since this same list is now a sign-in credential, that
    # deployment also has no admin left to sign in and repair a broken `orgs:` block.
    if oauth is not None and not oauth_admin_teams:
        msg = (
            "BAJUTSU_OAUTH_ADMIN_TEAMS is empty, so no login will have admin access and no admin "
            "can sign in to repair a broken `orgs:` block"
        )
        _warn("admin_teams_empty", msg)
    malformed = [t for t in oauth_admin_teams if not ADMIN_TEAM_ENTRY_RE.fullmatch(t)]
    if oauth is not None and malformed:
        msg = (
            'BAJUTSU_OAUTH_ADMIN_TEAMS entries must each be "<github-org>/<team-slug>"; '
            f"these will never match: {malformed}"
        )
        # `oauth_callback` treats an entirely-malformed list as the same total lockout as an empty
        # one (`admin_teams_unusable`) -- match that severity here too, or the worst shape (every
        # entry malformed) gets the mildest boot message: a syntax note about one entry, not the
        # "no login will have admin access" consequence `admin_teams_empty`'s message names, which
        # never fires here since the list isn't empty (BE-0352).
        if admin_teams_unusable(oauth_admin_teams):
            msg += (
                " -- no entry is well-formed, so no login will have admin access and no admin can "
                "sign in to repair a broken `orgs:` block"
            )
        _warn("admin_teams_malformed", msg)

    repo = repository_from_env()
    # A CI job's own credential (BE-0414): wired only when an expected audience is configured, and
    # useless without a database, since the exchange spends each token's `jti` in the shared
    # system of record. Warn rather than fail — a deployment may be mid-setup — but say plainly
    # that the endpoint will refuse every exchange until the database is there.
    oidc = _oidc_from_env()
    if oidc is not None and _db_engine is None:
        _warn(
            "oidc_without_database",
            "BAJUTSU_OIDC_AUDIENCE is set but no BAJUTSU_DATABASE_URL is — the OIDC exchange "
            "spends each token's 'jti' in the database to make it single-use across replicas, so "
            "every exchange will be refused until one is configured",
        )
    if oidc is not None and not _joserfc_installed():
        # Verification imports `joserfc` lazily, so a deployment missing the extra would otherwise
        # discover it as a 500 per exchange with no `oidc.denied` behind it. Say it once at boot,
        # where an operator is looking, rather than once per pipeline run where nobody is.
        _warn(
            "oidc_without_joserfc",
            "BAJUTSU_OIDC_AUDIENCE is set but 'joserfc' is not installed — it ships with the "
            "'oauth' extra (`uv sync --extra oauth`), and without it every OIDC exchange fails",
        )

    state = ServeState(
        runs_dir=runs_dir,
        config=config,
        scenarios_dir=scenarios_dir,
        root=root,
        baselines_dir=baselines_dir,
        max_concurrent=max_concurrent,
        max_concurrent_per_user=_max_concurrent_from_env(
            os.environ.get("BAJUTSU_MAX_CONCURRENT_PER_USER"),
            var="BAJUTSU_MAX_CONCURRENT_PER_USER",
        ),
        max_concurrent_per_org=_max_concurrent_from_env(
            os.environ.get("BAJUTSU_MAX_CONCURRENT_PER_ORG"),
            var="BAJUTSU_MAX_CONCURRENT_PER_ORG",
        ),
        upload_exec=upload_exec,
        evidence=evidence,
        # Uploaded bundles (BE-0073) extract under the shared Bajutsu cache root, a sibling of the
        # Git source's own checkout cache — the server backend previously left this at ServeState's
        # bare `Path("uploads")` default (BE-0243), never overriding it here.
        uploads_dir=_bajutsu_cache_root() / "uploads",
        # The hosted object store the control plane signs worker upload/download URLs against, and
        # its tenant base prefix (BE-0160). The worker never receives these credentials — only signed
        # URLs — so it needs no cloud SDK of its own.
        object_store=store,
        object_store_prefix=prefix,
        allow_remote_build=allow_remote_build,
        run_retention_days=_run_retention_from_env(os.environ.get("BAJUTSU_RUN_RETENTION_DAYS")),
        hosted=True,  # the server backend is a hosted deployment: drop the file browser (BE-0108)
        executor=DbQueueExecutor(repo) if repo is not None else LocalExecutor(),
        logbus=(
            PostCompletionLogBus(
                repo,
                artifacts_fn=lambda org: ObjectStorageArtifactStore(
                    store, prefix=artifact_prefix(org_prefix(prefix, org))
                ),
            )
            if repo is not None
            else InMemoryLogBus()
        ),
        repository=repo,
        # The authentication cluster (BE-0248): the shared token, the login-session store (a
        # DB-backed one when a database is wired so sessions survive restarts, else in-memory), and
        # the GitHub OAuth client + the server-wide admin Teams (BE-0313).
        auth=SessionManager(
            token=token,
            sessions=(
                SqlSessionStore(
                    _db_engine,
                    ttl=_session_ttl_from_env(os.environ.get("BAJUTSU_SESSION_TTL"), _DEFAULT_TTL),
                )
                if _db_engine is not None
                else InMemorySessionStore()
            ),
            oauth=oauth,
            oauth_admin_teams=oauth_admin_teams,
            oidc=oidc,
        ),
        startup_warnings=tuple(startup_warnings),
    )
    if repo is not None:
        # A restart otherwise reissues job ids from "1" while the DB-backed `jobs` table keeps
        # every id the previous process handed out, so the first dispatch after restart collides
        # on a primary key already taken. Only the control plane mints ids (a worker's ServeState
        # builds its `Job` straight from the leased spec, bypassing the registry entirely), so this
        # is the only construction site that needs the seed.
        state.job_registry.seed_seq(repo.max_job_id())

    # Build the object-storage seams per org (BE-0015 multi-tenancy): each org's artifacts/
    # scenarios/baselines live under its own key prefix, and its scenario store only acknowledges
    # the targets that org owns. The scenario targets are read from the live config, so a config
    # opened later is reflected.
    def _apps_for_org(org: str, session: str | None, _org: str) -> list[str]:
        """`ScenarioStorage`'s (session, org) shape with the bundle's own org already bound.

        The org the storage passes is the one the scope was asked for, which is the org this bundle
        was built for — so it is ignored in favour of the bound one.
        """
        return _org_apps(session, org)

    def _org_apps(session: str | None, org: str) -> list[str]:
        # The target set follows the configuration the *asking session* is bound to (BE-0393 unit 2),
        # so a target only that session's config declares is still a known app.
        return state.targets_for(org, session)

    def make_bundle(org: str) -> StoreBundle:
        base = org_prefix(prefix, org)
        # Secrets are encrypted per org in the database when one is wired; without a database the
        # server keeps the local shape (value in the process env), matching how the executor/logbus
        # fall back to their in-process defaults above (BE-0136). The startup guard above already
        # rejected "database wired but no master key", so the process-env fallback is only ever the
        # no-database case — asserted here so softening that guard can't silently turn this into a
        # plaintext-in-memory store for a database-backed deployment.
        if _db_engine is not None:
            assert _secrets_fernet is not None  # guaranteed by the BAJUTSU_SECRETS_KEY check above
            secrets: SecretStore = DbSecretStore(_db_engine, org, _secrets_fernet)
        else:
            secrets = state.secrets  # the process-env local store built in __post_init__
        # The per-org AI provider selection (BE-0229): DB-backed when a database is wired, so a saved
        # choice survives a restart per org; None without one, so the selection is session-only
        # in-memory (still resolved per org, just not persisted) — the same no-database fallback the
        # secret store takes above. Not sensitive, so it is stored in the clear.
        provider_settings = (
            DbProviderSettingsStore(_db_engine, org) if _db_engine is not None else None
        )
        # Scenarios are read from the bound config's local-tree dir, not this org's object-storage
        # prefix (BE-0324): a Git- or zip-sourced scenario is already extracted onto the control
        # plane's disk by the time a request arrives, so `list`/`read` resolve it from there. The
        # object storage still backs `save` (the hosted web editor / a `record` job) — the one
        # purpose `LocalTreeScenarioStorage` holds it for.
        # `_org_apps` takes (session, org); the bundle is built per org, so the org is bound here and
        # only the session is left for the storage to supply per call.
        object_scenarios = ObjectScenarioStorage(store, partial(_apps_for_org, org), prefix=base)
        return StoreBundle(
            artifacts=ObjectStorageArtifactStore(store, prefix=artifact_prefix(base)),
            scenarios=StorageScenarioStore(LocalTreeScenarioStorage(state, object_scenarios)),
            baselines=ObjectBaselineStore(store, prefix=base),
            secrets=secrets,
            provider_settings=provider_settings,
        )

    state.org_stores = make_bundle
    # The default-org bundle backs the bare ServeState fields, so code paths that don't resolve an
    # org (and local-parity tests) keep working.
    default = make_bundle(DEFAULT_ORG)
    state.artifacts, state.scenarios, state.baselines, state.secrets = (
        default.artifacts,
        default.scenarios,
        default.baselines,
        default.secrets,
    )
    return state


def _configure_oplog(state: ServeState) -> None:
    """Install the operational-logging channel for a serve process (BE-0055).

    Serve defaults to structured JSON; the process-lifetime redactor is seeded with the secrets
    that exist at startup (operator token, OAuth client secret, API key) so they can never reach a
    log line. Per-run ``${secrets.X}`` values are masked separately, run-scoped, on the worker.
    """
    from bajutsu.serve import oplog

    static = (
        state.auth.token,
        os.environ.get("ANTHROPIC_API_KEY"),
        os.environ.get("BAJUTSU_SERVE_TOKEN"),
        os.environ.get("BAJUTSU_OAUTH_GITHUB_CLIENT_SECRET"),
    )
    oplog.configure(
        fmt=os.environ.get("BAJUTSU_LOG_FORMAT") or "json",
        level=os.environ.get("BAJUTSU_LOG_LEVEL") or "INFO",
        secrets=tuple(v for v in static if v),
    )


def _emit_startup_warnings(state: ServeState) -> None:
    """Re-emit `_build_server_state`'s startup warnings (already printed to stderr, since nothing was
    configured that early) through the now-live log sink, under the registered
    `"server.startup_warning"` event. A separately-callable boot seam, like its two siblings below,
    rather than an inline loop in `serve()` — so a rename or drop of that event name from
    `oplog.EVENTS` has a direct test to fail, instead of only surfacing as a boot-time `ValueError`
    on the first deployment that actually has a startup warning to re-emit. Every check shares this
    one event name, so each entry also carries its own stable `check` field (`"oauth_half_configured"`,
    `"admin_team_retired_name"`, `"admin_teams_empty"`, `"admin_teams_malformed"`) — an operator's
    alert keys on `check=`, not on substring-matching a message that can reword out from under it. A
    no-op when `state.startup_warnings` is empty (local serve, or a server deployment with nothing to
    warn about) (BE-0352)."""
    for check, msg in state.startup_warnings:
        oplog.log_event(_logger, "server.startup_warning", msg, level=logging.WARNING, check=check)


def make_asgi_server(state: ServeState, host: str = "127.0.0.1", port: int = 8765) -> Any:
    """A uvicorn ``Server`` running the FastAPI control-plane app over *state*. uvicorn and the app
    (FastAPI) are imported lazily — only when the ASGI transport is selected — so the default path
    stays server-free (`tests/serve/test_import_guard.py`). (Return type is Any so the module
    needn't import uvicorn to annotate it.)"""
    try:
        import uvicorn

        from bajutsu.serve.server.app import make_app
    except ImportError as e:
        raise ImportError(
            "the 'server' extra is required for --asgi (FastAPI + uvicorn) — "
            "install with: pip install 'bajutsu[server]'"
        ) from e

    # Derive the Host allowlist from the bound interface, exactly as make_server does for the stdlib
    # transport, so the ASGI gate enforces the same DNS-rebinding defense (BE-0121).
    state.allowed_hosts = gate.allowed_hosts(host)
    return uvicorn.Server(
        uvicorn.Config(make_app(state), host=host, port=port, log_level="warning")
    )


def serve(
    host: str,
    port: int,
    scenarios_dir: Path | None,
    config: Path | None,
    runs_dir: Path,
    root: Path | None = None,
    baselines_dir: Path | None = None,
    max_concurrent: int = 4,
    token: str | None = None,
    *,
    upload_exec: str = "sandbox",
    evidence: EvidenceTarget | None = None,
    allow_remote_build: bool = False,
    asgi: bool = False,
    backend: str = "local",
    cwd: Path | None = None,
    config_provenance: dict[str, str] | None = None,
    themes_dir: Path | None = None,
    default_theme: str | None = None,
) -> None:
    state = _build_state(
        runs_dir=runs_dir,
        config=config,
        scenarios_dir=scenarios_dir,
        root=root,
        baselines_dir=baselines_dir,
        max_concurrent=max_concurrent,
        token=token,
        upload_exec=upload_exec,
        evidence=evidence,
        allow_remote_build=allow_remote_build,
        backend=backend,
        cwd=cwd,
        config_provenance=config_provenance,
        themes_dir=themes_dir,
        default_theme=default_theme,
    )
    # No startup sweep of uploads_dir: every entry is now a content-addressed cache keyed by its
    # sha256 (BE-0243), as valid a cache hit moments after a restart as one extracted just before
    # it — the same reason nothing sweeps the Git source's own checkout cache at startup either.
    _configure_oplog(state)
    # After `_configure_oplog`, the same placement `restore_persisted_provider_settings` below uses,
    # and for the same reason: a warning from before logging was live must still reach the live sink.
    _emit_startup_warnings(state)
    # Restore the operator's last-saved provider/model/effort before the first request, so a restart
    # reflects it rather than resetting to the launch environment (BE-0184). After `_configure_oplog`
    # so a malformed-file warning reaches the live log sink; a no-op when nothing is persisted (BE-0101).
    restore_persisted_provider_settings(state)
    # Seed the launch config's `orgs:` block into the database, once per org row (BE-0375), so a
    # deployment upgrading from config-only org membership keeps admitting exactly who it did
    # before the database became the source. Shares the boot placement above for the same reason —
    # its "this entry is no longer read" warning must reach the live log sink — and re-runs at every
    # config rebind; a no-op without a database or a loadable config.
    seed_orgs_from_bound_config(state)
    # Wire the lazy restore (BE-0393 unit 6), so a session with no binding of its own inherits the
    # configuration its org last bound on that session's first request. Nothing runs here: the seam
    # fires on a slot miss, and stays inert on a deployment with no database to remember into.
    state.restore_binding = partial(restore_org_binding, state)
    # Where the cloud-batch (Device Farm) package roots — see ServeState.devicefarm_package_root.
    state.devicefarm_package_root = bajutsu_source_root()
    # Fill the batch-provider registry from the environment (BE-0336): with DEVICEFARM_PROJECT_ARN set,
    # cloud-batch dispatch reaches AWS Device Farm; with it unset the registry stays empty and a
    # mis-dispatched cloud-batch job fails loud at resolve() rather than silently vanishing.
    # A broken wiring (missing boto3, bad credentials) must not take down the whole serve process —
    # the failure belongs at dispatch, not at boot. Leave the provider unregistered and say so.
    try:
        for kind in register_batch_providers():
            print(f"cloud-batch dispatch enabled: {kind}")  # noqa: T201
    except Exception as exc:  # an optional feature must never block the bind
        _logger.warning("cloud-batch dispatch disabled at startup", exc_info=True)
        print(f"note: cloud-batch dispatch is off — {exc}")  # noqa: T201
    hint = str(config) if config else "open a config.yml in the UI"
    if not gate.allowed_hosts(host):
        # A wildcard bind can't enumerate its reachable hostnames, so the Host allowlist is off
        # (BE-0121). Say so, rather than silently downgrading the DNS-rebinding defense — CSRF stays
        # the cross-origin guard, and a non-loopback bind already requires a token.
        print(  # noqa: T201
            f"note: Host-header enforcement is off for the wildcard bind {host!r}; "
            "CSRF remains the cross-origin defense (BE-0121)"
        )
    if asgi:
        # The FastAPI app over uvicorn — the transport the hosted backend will use; runnable now
        # with the local backend (a single-process ASGI server) so the path is exercised before
        # the hosted seams land.
        print(f"bajutsu serve (asgi) → http://{host}:{port}  (config: {hint} · Ctrl-C to stop)")  # noqa: T201
        make_asgi_server(state, host, port).run()
        return
    server = make_server(state, host, port)
    bound = server.server_address[1]
    print(f"bajutsu serve → http://{host}:{bound}  (config: {hint} · Ctrl-C to stop)")  # noqa: T201
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")  # noqa: T201
    finally:
        server.shutdown()
        server.server_close()
