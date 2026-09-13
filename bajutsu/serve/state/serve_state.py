"""The server's whole mutable state, as one value the route handlers read."""

from __future__ import annotations

import logging
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import InitVar, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bajutsu.common.backend_cli import simctl as _simctl
from bajutsu.common.run_meta.object_store import EvidenceTarget, ObjectStore
from bajutsu.serve.artifacts import ArtifactStore, LocalArtifactStore
from bajutsu.serve.baselines import BaselineStore, LocalBaselineStore
from bajutsu.serve.executor import LocalExecutor, RunExecutor
from bajutsu.serve.helpers import load_serve_config_file
from bajutsu.serve.logbus import InMemoryLogBus, LogBus
from bajutsu.serve.oidc import JwksCache
from bajutsu.serve.orgs import targets_for_org
from bajutsu.serve.scenarios import LocalScenarioStore, ScenarioStore
from bajutsu.serve.secrets import EnvSecretStore, SecretStore
from bajutsu.serve.uploads import Upload

from ._functions import _scenarios_dir_for
from ._shared import _DEFAULT_ORG
from .capture_session import CaptureSession
from .config_binding import ConfigBinding
from .job import Job
from .job_registry import JobRegistry
from .provider_settings_manager import ProviderSettingsManager
from .session_manager import SessionManager
from .store_bundle import StoreBundle

if TYPE_CHECKING:
    from bajutsu.serve.server.db import Repository

Popen = Callable[..., Any]


# How many (session, org) bindings one process keeps before evicting the least recently bound
# (BE-0393 unit 2). A member holds one slot per org they act as, so this is thousands of concurrent
# members on one process — far past what a serve deployment fans out to — while still bounding a
# process that never sees those sessions end.
MAX_SESSION_BINDINGS = 2048


@dataclass
class ServeState:
    runs_dir: Path
    # Init-only: folded into `binding` by `__post_init__`, so `ServeState(config=…)` keeps working
    # while there is no ambient `state.config` left to read (BE-0393 unit 1). Read the bound value
    # through `binding` instead.
    config: InitVar[Path | None] = None
    scenarios_dir: Path | None = None  # override; default is the selected app's configured dir
    root: Path = field(default_factory=Path.cwd)  # the file browser's browse ceiling
    # where `visual` baselines live (and where Approve promotes to); serve() defaults it to
    # <scenarios_dir>/baselines.
    baselines_dir: Path = field(default_factory=lambda: Path("baselines"))
    # Root for uploaded-bundle extraction (BE-0073), never the browse `--root`, so an uploaded tree
    # can't overwrite the operator's files. Each org's entries live under their own sub-path, keyed
    # by content sha256 (BE-0243) — a durable, reusable cache in front of the object store, not a
    # disposable per-bind sandbox. serve() defaults it onto the shared `~/.cache/bajutsu/` root.
    uploads_dir: Path = field(default_factory=lambda: Path("uploads"))
    # Drop-in theme directory (BE-0191 unit 2): scanned once at startup, its `*.css` folded into the
    # inlined theme stylesheet. None (the default / no `--themes`) means only the built-in themes.
    themes_dir: Path | None = None
    # The `ui.default_theme` initial selection, read from the startup config; None follows the OS.
    default_theme: str | None = None
    # Init-only, like `config` above: `ServeState(cwd=…)` seeds the binding's `cwd`, and readers go
    # through `binding.cwd`.
    cwd: InitVar[Path | None] = None
    # Init-only seeds for the rest of the launch binding, so `serve()` and the tests can construct a
    # Git-sourced or bundle-sourced state in one call as before.
    config_provenance: InitVar[dict[str, str] | None] = None
    upload: InitVar[Upload | None] = None
    git_config_from_api: InitVar[bool] = False
    config_org: InitVar[str | None] = None
    # The deployment's own binding: the configuration `serve` started with (BE-0393 unit 1). Unit 2
    # made it the **fallback** — a session with no binding of its own, and a caller with no session at
    # all, read this one. Never read it directly from a request path; ask `binding_for`.
    binding: ConfigBinding = field(init=False)
    # A binding per login session and acting org (BE-0393 unit 2). A bind made in one session is
    # visible to that session alone, so two members of one org can work at once and one switching
    # configurations does not move the ground under another's run. The acting org joins the key
    # because a session may change which org it acts as, and target ownership rides on the org: a
    # binding that outlived an org change would hand one org's targets to a request acting as
    # another. Ordered and capped so a process accumulating quiet sessions evicts the oldest binding
    # rather than growing without limit.
    bindings: OrderedDict[tuple[str, str], ConfigBinding] = field(init=False)
    _bindings_lock: threading.Lock = field(init=False)
    # The (session, org) pairs a restore has already been tried for (BE-0393 unit 6), whether it
    # bound anything or not. A failed restore is not retried, so a repeatedly unreachable source does
    # not re-fetch on every request; bounded and evicted like `bindings`, since a process that never
    # sees its sessions end would otherwise remember every pair forever.
    _restore_tried: OrderedDict[tuple[str, str], None] = field(init=False)
    # serve's launch directory, captured at construction (see __post_init__) before a config bind can
    # repoint the binding's `cwd`. Runs off a Git/upload bind still land their tree here
    # (BE-0063/BE-0073).
    base_cwd: Path = field(init=False)
    # Root the cloud-batch (Device Farm) test package is built from. Device Farm's
    # APPIUM_PYTHON_TEST_PACKAGE validation needs Bajutsu's own `tests/` and `pyproject.toml` at the
    # package root, and its test spec `pip install`s that root — so the package must be rooted at the
    # Bajutsu source tree, not the config's own directory (`cwd`, which BE-0242 points at the config).
    # serve() sets it to the source root when serving from a checkout; None falls back to `cwd` (the
    # in-process test model, where the config, scenarios, and source all sit in one tmp tree).
    devicefarm_package_root: Path | None = None
    # Policy for an uploaded bundle's launchServer command (and the latent mockServer.cmd, once it is
    # wired) (BE-0090): deny | reuse | sandbox. Default `sandbox` runs it in a throwaway container,
    # never on the serve host; it applies only to upload-sourced configs (a local/Git config is
    # operator-trusted and ungoverned). serve() sets it from --upload-exec / BAJUTSU_UPLOAD_EXEC.
    upload_exec: str = "sandbox"
    # Host-header allowlist (BE-0121): the hostnames a request's `Host` may name, set by
    # `make_server` from the bound interface. Empty — a wildcard bind, whose reachable names can't be
    # enumerated — disables the check; a loopback/named bind enforces its own names, closing the
    # DNS-rebinding path to endpoints like /api/apikey.
    allowed_hosts: frozenset[str] = frozenset()
    # Opt-in to run an API-bound Git config's `build:` command on the host (BE-0121). Off by default;
    # serve() sets it from --allow-remote-build / BAJUTSU_ALLOW_REMOTE_BUILD.
    allow_remote_build: bool = False
    # Whether this is a hosted deployment (the server backend), the single source of truth for
    # deployment-aware config sourcing (BE-0108). The server backend sets it True where it wires its
    # hosted seams; the local backend (stdlib serve, including a self-hosted single Mac) never does,
    # so the file browser stays offered locally and is removed — UI and server-side — when hosted.
    hosted: bool = False
    # Whether this deployment's transport serves the `local_only` routes (`serve.routes`) — the
    # `/api/capture/*` family among them. The stdlib server registers every route, so the default
    # holds there; `make_app` clears it, since the FastAPI transport skips them. Distinct from
    # `hosted`, which the local `serve --asgi` leaves False while still going through `make_app`:
    # that deployment keeps the file browser and its host paths but serves no capture route, so the
    # boot read's `capture` capability is keyed to this field rather than to `hosted` (issue 1721).
    serves_local_routes: bool = True
    popen: Popen = subprocess.Popen
    # How a created job gets executed. Defaults to in-process threads (LocalExecutor); a server
    # backend swaps in a queue-based executor without touching the handler or run_job (BE-0015).
    executor: RunExecutor = field(default_factory=LocalExecutor)
    # Live-log delivery. In-memory buffer by default; a server backend swaps in `PostCompletionLogBus`
    # (BE-0106), which serves any replica any job's `/events` from the jobs table + object storage.
    logbus: LogBus = field(default_factory=InMemoryLogBus)
    # Run-artifact reads. Filesystem-confined by default; a server backend swaps in an
    # object-storage store (set after construction) that may serve signed-URL redirects (BE-0015).
    artifacts: ArtifactStore = field(init=False)
    # Scenario resolution. Confined to the app's scenarios dir by default; a server backend swaps
    # in a per-project store (set after construction) that resolves by id (BE-0015).
    scenarios: ScenarioStore = field(init=False)
    # Visual-regression baselines. Filesystem-confined by default; a server backend swaps in an
    # object-storage store (set after construction) (BE-0015).
    baselines: BaselineStore = field(init=False)
    # Operator secrets (the Claude API key today). Write-once: set/describe only, no plaintext read
    # an HTTP handler can reach (BE-0136). Default holds the value in the process env (in memory, as
    # before); a server backend with a database swaps in an encrypted per-org store.
    secrets: SecretStore = field(init=False)
    # The system of record (BE-0015 7a). None until a database is wired: local never has one, and a
    # server backend assigns a SqlRepository only when BAJUTSU_DATABASE_URL is set, so behavior is
    # unchanged without one. Annotated as a string (lazy) so the default path never loads SQLAlchemy.
    repository: Repository | None = None
    simctl: _simctl.RunFn = (
        _simctl.real_run
    )  # runs `xcrun simctl …` (booting devices, listing them)
    # Cap on concurrently-running run/record jobs so one caller can't monopolize the scarce device
    # (BE-0051). <= 0 means unlimited; serve() sets it from --max-concurrent-runs (default 4).
    max_concurrent: int = 4
    # Per-user cap on concurrent jobs (BE-0015 7c-3), so one OAuth user can't starve the pool. <= 0
    # means unlimited (the default); a server backend sets it from BAJUTSU_MAX_CONCURRENT_PER_USER.
    # Applies only to jobs that carry an actor (an OAuth identity); token/anonymous jobs are exempt.
    max_concurrent_per_user: int = 0
    # Per-org cap on concurrent jobs (BE-0016 Tier B pool fairness), so one tenant can't monopolize
    # the scarce Mac pool even when its users each stay under the per-user cap. <= 0 = unlimited (the
    # default), so a single-tenant deploy (every job in the default org) is unchanged; a server
    # backend sets it from BAJUTSU_MAX_CONCURRENT_PER_ORG. Every job carries an org, so this needs no
    # exemption — an operator opts in only when running multiple orgs.
    max_concurrent_per_org: int = 0
    # Authentication cluster (BE-0248): the shared token, the login sessions, and the GitHub OAuth
    # configuration + the "authenticated as whom" methods, carved into `SessionManager` (BE-0051 /
    # BE-0015 7b). `ServeState` holds one and the transport/authz layers read through `state.auth`.
    auth: SessionManager = field(default_factory=SessionManager)
    # (check, msg) pairs `_build_server_state` already printed to stderr (nothing is configured that
    # early), re-emitted through `oplog` once `serve()` calls `_configure_oplog` -- *check* is a
    # stable discriminator (e.g. "admin_teams_empty") carried as its own field, so an operator's alert
    # keys on `check=` rather than substring-matching *msg*, which can reword out from under it. Empty
    # on local serve (BE-0352).
    startup_warnings: tuple[tuple[str, str], ...] = ()
    # The OIDC issuer's signing keys, fetched on the first exchange and re-read on a bounded
    # schedule (BE-0414 unit 1). Lives here, not in the operation, because a cache rebuilt per
    # request bounds nothing: the refresh floor is what keeps a flood of unknown key ids costing
    # one outbound fetch per interval instead of one per call. None until the first exchange, and
    # on every deployment that configures no OIDC audience.
    oidc_keys: JwksCache | None = None
    # Per-org store factory (BE-0015 multi-tenancy). None on local serve (one tenant); a server
    # backend sets a closure that builds object stores prefixed for the given org. `for_org` falls
    # back to the default stores when unset, so local behavior is unchanged.
    org_stores: Callable[[str], StoreBundle] | None = None
    # Restores an org's remembered configuration into a session's empty slot (BE-0393 unit 6), by
    # calling `rebind` itself. Injected rather than imported, like the store seams above, because the
    # materialization lives in `operations` and `operations` imports this module. None — the default,
    # and every deployment with no config memory — leaves `binding_for` answering with the fallback.
    restore_binding: Callable[[str, str], None] | None = None
    capture: CaptureSession | None = None
    # Per-org AI provider settings (BE-0229), carved into `ProviderSettingsManager` (BE-0248): the
    # in-memory selection map, its durable local store, and the copy-on-read/copy-on-write methods
    # the Settings panel reads/writes. `ServeState` holds one and the operations layer reaches it as
    # `state.providers`; `for_org(default)` exposes its `store` as the bundle's `provider_settings`
    # seam so the operations layer reads/writes it uniformly with the hosted per-org store.
    providers: ProviderSettingsManager = field(default_factory=ProviderSettingsManager)
    # The in-flight `ant auth login` subprocess (BE-0175), or None when no sign-in is running. Held
    # between the POST that starts it and the GET that polls it, so a second click doesn't spawn a
    # duplicate. Local serve only — a hosted deployment refuses the operation, so this stays None
    # there. Spawned through `popen` (the injectable seam above) so tests never exec the real CLI.
    # `ant_login_lock` makes the check-terminate-spawn sequence atomic: serve is a ThreadingHTTPServer,
    # so two concurrent POSTs must not both see None and each spawn a CLI (a leaked, unsupersedable proc).
    ant_login_proc: Any = None
    ant_login_lock: threading.Lock = field(default_factory=threading.Lock)
    # Where completed runs' evidence is uploaded (BE-0110). None = no evidence store configured (the
    # default; the upload-urls endpoint then hands back no URLs). serve() builds it from
    # --evidence-store / BAJUTSU_EVIDENCE_STORE; the server holds the credentials so a worker uploads
    # via presigned PUT URLs without any of its own.
    evidence: EvidenceTarget | None = None
    # The hosted object store + tenant base prefix the control plane signs worker upload/download
    # URLs against (BE-0160): the worker holds no cloud credentials, so it asks for a presigned URL
    # per file and reads/writes over plain HTTP. None/"" on local serve (no remote worker) — the
    # worker signing endpoints and the lease then return no URLs, like `evidence` when unset. A
    # server backend sets both where it wires its per-org object stores.
    object_store: ObjectStore | None = None
    object_store_prefix: str = ""
    # Days a soft-deleted run stays in the trash before the lazy sweep purges it (BE-0239). The
    # retention window that makes soft-delete non-instantly-destructive. <= 0 disables the automatic
    # purge (trash is kept until a manual purge). serve() sets it from BAJUTSU_RUN_RETENTION_DAYS.
    run_retention_days: int = 30
    # The job registry (BE-0198): owns the in-flight jobs, the id sequence, and the concurrency-cap
    # enforcement. Built in __post_init__ once `logbus` is resolved; `ServeState` forwards the
    # registration/counting surface to it and exposes `jobs` as a read-through of its dict.
    job_registry: JobRegistry = field(init=False)

    def __post_init__(
        self,
        config: Path | None,
        cwd: Path | None,
        config_provenance: dict[str, str] | None,
        upload: Upload | None,
        git_config_from_api: bool,
        config_org: str | None,
    ) -> None:
        # Fold the init-only seeds into the one binding value (BE-0393 unit 1). `cwd` defaults here
        # rather than on the field so the launch binding and `base_cwd` below read the same launch
        # directory even when the caller passed none.
        launch_cwd = cwd if cwd is not None else Path.cwd()
        self.binding = ConfigBinding(
            config=config,
            cwd=launch_cwd,
            provenance=config_provenance,
            upload=upload,
            git_from_api=git_config_from_api,
            org=config_org,
        )
        self.bindings = OrderedDict()
        self._restore_tried = OrderedDict()
        self._bindings_lock = threading.Lock()
        # serve's own launch directory, captured before any config bind repoints the binding's `cwd`
        # at a Git checkout / uploaded bundle. A run off such a bind writes its tree into
        # `base_cwd/runs_dir` (serve's store), not under the transient checkout/bundle
        # (BE-0063/BE-0073).
        self.base_cwd = launch_cwd
        # Anchor a relative runs_dir / baselines_dir at serve's launch cwd (Path.cwd(), which serve
        # never changes) so each store, the run subprocess's `--runs-dir` / `--baselines`, and the
        # manifest reads in `jobs`/`triage` all resolve to one directory. Without this a subdir config
        # repoints `cwd` to the config's dir (BE-0242): the run then writes under `cwd/runs` while the
        # store reads `<launch>/runs`, so a just-finished replay's `report.html` — or a visual
        # baseline read/write — targets the wrong tree. An already-absolute dir (server/worker, an
        # explicit `--runs`/`--baselines`, tests) is left untouched.
        if not self.runs_dir.is_absolute():
            self.runs_dir = Path.cwd() / self.runs_dir
        if not self.baselines_dir.is_absolute():
            self.baselines_dir = Path.cwd() / self.baselines_dir
        # `artifacts`/`scenarios` are init=False so existing ServeState(...) calls don't change;
        # default them to the local stores here (a server backend overwrites them afterwards).
        self.artifacts = LocalArtifactStore(self.runs_dir)
        # Resolve the dir lazily through a closure so a config opened from the UI later is reflected.
        # The resolver takes the requesting session and org, which `scope` supplies per call — the
        # closure itself is built once, with no handler in scope (BE-0393 unit 2).
        self.scenarios = LocalScenarioStore(
            lambda target, session, org: _scenarios_dir_for(self, target, session, org)
        )
        self.baselines = LocalBaselineStore(self.baselines_dir)
        # The local secret store holds the value in this process's env; the name->env-var mapping is
        # resolved lazily so a config bound later (its `ai.keyEnv`, BE-0097) is reflected.
        self.secrets = EnvSecretStore(self._env_var_for_secret)
        # `logbus` is resolved by now (a plain field), so the registry can capture it: it is never
        # reassigned after construction, so the registry's reference stays the live bus.
        self.job_registry = JobRegistry(logbus=self.logbus, finished_ids=self._finished_job_ids)

    @property
    def jobs(self) -> dict[str, Job]:
        """The in-flight jobs, read through to the registry (BE-0198). Kept so existing lookups
        (`state.jobs.get(id)`) read unchanged now that the registry owns the dict. Treat it as
        read-only: register a job through `register` / `try_register` so its id assignment and cap
        check run under the registry's lock — inserting here directly bypasses that enforcement."""
        return self.job_registry.jobs

    def org_of(self, actor: str | None) -> str:
        """The org of *actor*, read from their persisted user row (assigned at login). The single
        `default` org without a database, without an identity, or for an unknown user (BE-0015)."""
        if self.repository is None or not actor:
            return _DEFAULT_ORG
        return self.repository.user_org(actor) or _DEFAULT_ORG

    def eligible_orgs(self, actor: str | None) -> dict[str, str]:
        """Every org *actor* may act as, each mapped to the role they hold there.

        The set the header's org selector offers and the switch endpoint authorizes against. Written
        at sign-in from the login's GitHub memberships, except for a server-wide admin, whose set is
        every live org: an admin is admitted by their admin Team rather than by any org's membership
        (BE-0352), so a stored set would be empty, and one stored at sign-in would miss an org they
        create afterwards on the Orgs page. Empty without a database, without an identity, and for a
        user no org's membership admits — the fail-closed answer, since a caller that cannot tell
        which orgs are allowed must allow none.
        """
        if self.repository is None or not actor:
            return {}
        repository = self.repository
        if repository.user_role(actor) == "admin":
            return {org.id: "admin" for org in repository.list_orgs()}
        return repository.list_user_orgs(actor)

    def _env_var_for_secret(self, name: str) -> str:
        """The env var the local secret store reads/writes for logical secret *name* (BE-0136).

        The Claude API key honors the bound config's ``ai.keyEnv`` (BE-0097); the `claude-code`
        provider's OAuth token maps to its fixed CLI variable (BE-0215) — the `claude` CLI names it,
        so it is not config-overridable like the SDK key; the Git config-source credential maps to
        the bajutsu-owned ``BAJUTSU_GIT_CONFIG_TOKEN`` (BE-0224), which the in-process private-repo
        fetch reads — deliberately *not* ``GITHUB_TOKEN`` so clearing the UI credential never pops an
        operator's own exported token. Any other name is a scenario-declared secret (BE-0274): its
        `secrets:` entry already *is* an environment-variable name (BE-0032), so it maps to itself —
        not through ``active_key_env``, which would overwrite the AI key's var. Imported lazily to
        avoid a cycle with the operations layer, which imports this module."""
        from bajutsu.common.ai.claude_code import OAUTH_TOKEN_ENV
        from bajutsu.common.config_source import GIT_CONFIG_TOKEN_ENV
        from bajutsu.serve.operations.config import (
            AI_API_KEY_SECRET,
            AI_CLAUDE_CODE_TOKEN_SECRET,
            GIT_CONFIG_TOKEN_SECRET,
            active_key_env,
        )

        if name == AI_CLAUDE_CODE_TOKEN_SECRET:
            return OAUTH_TOKEN_ENV
        if name == GIT_CONFIG_TOKEN_SECRET:
            return GIT_CONFIG_TOKEN_ENV
        if name == AI_API_KEY_SECRET:
            return active_key_env(self)
        return name

    def rebind(self, session: str | None, org: str, binding: ConfigBinding) -> None:
        """Make *binding* what *session*, acting as *org*, is bound to (BE-0393 unit 2).

        A bind is visible to the session that made it and to no other, so two members of one org can
        work at once and one switching configurations does not move the ground under another's run.
        What a colleague's next session inherits is the org's remembered configuration, not this
        slot.

        A caller with no session — a shared-token or CI request — replaces the deployment's fallback
        instead, which is the pre-unit-2 behavior and the only binding such a caller could mean.

        The map is bounded: a process that accumulates quiet sessions evicts the least recently bound
        slot rather than growing without limit. Eviction costs that session its own binding and
        nothing else — the next request re-reads the fallback. Not *quite* as a session that never
        bound anything does, since unit 6 restores into an empty slot on first use and an evicted
        session has already used its one attempt; it stays on the fallback rather than restoring
        again. Bounded, rare, and self-limiting, which is what best-effort buys.
        """
        # The origin is stamped here rather than passed in: this is where a value's slot is decided,
        # so a binder cannot label one thing and land it somewhere else (unit 7).
        if session is None:
            self.binding = replace(binding, origin="deployment")
            return
        with self._bindings_lock:
            self.bindings[session, org] = replace(binding, origin="session")
            self.bindings.move_to_end((session, org))
            while len(self.bindings) > MAX_SESSION_BINDINGS:
                self.bindings.popitem(last=False)

    def drop_revoked_bindings(self) -> int:
        """Drop every binding whose session the store no longer knows; returns how many went.

        A revoked session cannot *reach* its slot — the request adapters resolve an unknown cookie to
        None, so such a request already reads the fallback — but the slot would sit until eviction
        pushed it out. Retiring an org revokes its members' sessions (BE-0375), so this runs there.

        Asks the store which sessions are still live rather than being handed the revoked ids: the
        database-backed store revokes with a bulk delete and knows only how many rows went, so making
        it report ids would widen the seam for a sweep that is cheap on the one path that needs it —
        a rare admin action over a map bounded by `MAX_SESSION_BINDINGS` (BE-0393 unit 2).
        """
        with self._bindings_lock:
            keys = list(self.bindings)
        # Asked outside the lock: on a hosted deployment `valid_session` is a database round trip, and
        # holding the lock across up to `MAX_SESSION_BINDINGS` of them would block every concurrent
        # `binding_for`. A slot bound between the snapshot and the delete is still live, so membership
        # is re-checked below rather than trusted from the snapshot.
        dead = [key for key in keys if not self.auth.valid_session(key[0])]
        with self._bindings_lock:
            gone = [key for key in dead if key in self.bindings]
            for key in gone:
                del self.bindings[key]
        return len(gone)

    def binding_for(self, session: str | None, org: str) -> ConfigBinding:
        """The configuration a request from *session*, acting as *org*, is bound to (BE-0393 unit 2).

        The session's own binding when it has one; failing that, the org's remembered configuration
        restored into the slot on this first use (unit 6); failing that, the deployment's fallback —
        initially the configuration `serve` started with, whose `orgs:` block partitions its targets
        between orgs exactly as an operator wrote it, and replaceable only by a bind that carries no
        session. A caller with no session (a shared-token or CI request, which carries no login
        cookie) reads the fallback and restores nothing: it is acting on the deployment, not inside a
        member's session.

        The restore happens here, on first need, rather than at sign-in, for two reasons the item
        gives: sign-in must not wait on a network fetch of a Git source or a bundle, and the org a
        session acts as can change without a sign-in. Keying it to "this session has no binding for
        this org yet" covers both entry paths without naming either — and it does mean the request
        that finds the empty slot pays for the fetch.

        This is the only way a request path should reach the bound configuration. Reading
        `state.binding` from one would restore the ambient default this unit exists to remove.
        """
        if session is None:
            return self.binding
        key = (session, org)
        with self._bindings_lock:
            bound = self.bindings.get(key)
            if bound is not None:
                return bound
            restore = self.restore_binding
            if restore is None or key in self._restore_tried:
                return self.binding
            self._restore_tried[key] = None
            while len(self._restore_tried) > MAX_SESSION_BINDINGS:
                self._restore_tried.popitem(last=False)
        # Outside the lock: a restore fetches a Git subtree or a bundle's bytes, and holding the lock
        # across that would stall every concurrent read. Marked as tried before releasing it, so two
        # concurrent first requests fetch at most once between them.
        try:
            restore(session, org)
        except Exception:
            # Best-effort, and never fails the request that happened to be first (unit 6): a moved
            # file, an unreachable repository, or an evicted bundle leaves the session with no
            # binding of its own and the reason in the log — the same posture the org seeding at
            # startup takes with a failed database read.
            logging.getLogger(__name__).warning(
                "could not restore org %s's remembered configuration for this session",
                org,
                exc_info=True,
            )
        with self._bindings_lock:
            restored = self.bindings.get(key)
            if restored is None:
                return self.binding
            # A restore binds through the ordinary binder, which stamps `session` on its way in, so
            # the slot is relabelled here — the one place that knows the value arrived by
            # inheritance rather than by the member's own choice. A bind the member makes *during*
            # the fetch window is mislabelled `inherited` until their next bind: a label, and
            # already inside the window where that bind loses to the restore anyway.
            inherited = replace(restored, origin="inherited")
            self.bindings[key] = inherited
            return inherited

    def for_org(self, org: str) -> StoreBundle:
        """The storage seams scoped to *org*. A server backend prefixes each org's objects; local
        serve has a single tenant, so this is just the default stores (BE-0015 multi-tenancy).

        Takes no session: every seam here is org-scoped. A scenarios dir *is* session-scoped, because
        it is resolved against the bound configuration, but the seam that learns the session is the
        scenario store's own `scope` — a request reaches that with a session in hand, while the store
        itself is swapped in once per deployment (BE-0393 unit 2).
        """
        if self.org_stores is not None:
            return self.org_stores(org)
        return StoreBundle(
            self.artifacts,
            self.scenarios,
            self.baselines,
            self.secrets,
            self.providers.store,
        )

    def active_jobs(self) -> int:
        """How many spawned jobs are still running (not yet finished). Delegates to the registry."""
        return self.job_registry.active_jobs()

    def in_flight_by_org(self) -> dict[str, int]:
        """Running jobs grouped by org, for the ``/metrics`` endpoint (BE-0169). Delegates to the
        registry."""
        return self.job_registry.in_flight_by_org()

    def register(self, job: Job) -> Job:
        """Assign *job* its id + live-log bus and store it. Delegates to the registry (BE-0198)."""
        return self.job_registry.register(self._freeze_binding(job))

    def try_register(self, job: Job, *, device_budget: int = 0) -> Job | None:
        """Register *job* only if under the concurrency caps, forwarding this state's configured caps
        to the registry, which counts and inserts atomically under one lock (BE-0051). *device_budget*
        is the per-target cloud-batch device cap (BE-0336 Unit 4); unlike the state-wide caps it is
        per-target, so the dispatcher resolves it from the request's config and passes it per call."""
        return self.job_registry.try_register(
            self._freeze_binding(job),
            max_concurrent=self.max_concurrent,
            max_concurrent_per_user=self.max_concurrent_per_user,
            max_concurrent_per_org=self.max_concurrent_per_org,
            max_concurrent_batch=device_budget,
        )

    def _finished_job_ids(self, job_ids: list[str]) -> set[str]:
        """Which of *job_ids* the system of record says are finished — the registry's cap-release
        probe (`JobRegistry._release_finished`).

        A deployment that dispatches to remote workers (`DbQueueExecutor`) never learns a job's end
        in process, so the jobs table is the only place its terminal status exists. Reads
        `self.repository` per call rather than closing over it, so a repository wired after
        construction is still consulted. None finished without one: local serve runs every job
        in-process and observes each one finish, and an id with no row is a job registered but not
        yet enqueued (`_register_and_dispatch` registers first), which is still running.
        """
        repository = self.repository
        if repository is None:
            return set()
        return repository.finished_job_ids(job_ids)

    def _freeze_binding(self, job: Job) -> Job:
        """Capture the working directory *job* will spawn against, at registration (BE-0393 unit 2).

        A job's `--config` is already on its command line, so the configuration a run parses is fixed
        at dispatch. Its working directory was not: `Job.cwd` defaulted to None and no dispatcher set
        it, so the spawn read the *live* binding at `popen` time — and a rebind between registration
        and spawn, a window a device boot or an on-demand build can hold open, repointed a run that
        had already been accepted. Frozen here, at the one point every dispatcher goes through, rather
        than in each of them, so a sixth dispatcher cannot forget.

        An explicit `job.cwd` is left alone: a worker rebuilds a job from its spec with no captured
        directory and resolves its own workspace at spawn, which is that fallback's remaining use.
        """
        if job.cwd is None:
            job.cwd = self.binding.cwd
        return job

    def bind_upload(self, upload: Upload, session: str | None) -> None:
        """Make *upload* the active binding (BE-0073), replacing whatever was bound.

        The whole binding is replaced rather than mutated field by field, so no reader can observe a
        bundle paired with the previous source's `cwd` or provenance (BE-0393 unit 1). A bundle is not
        a Git source, so it carries no commit provenance and is governed by `upload_exec` rather than
        the Git trust flag. The owning org is read off the bundle rather than stamped by the caller
        afterwards: every caller set it to exactly this, and a caller that forgot left the previous
        owner's partition in place (BE-0375).

        Bound for *session* alone (BE-0393 unit 2), so an upload does not repoint a colleague's run.
        The org is the bundle's own, which is also the org half of the key: a bundle is uploaded *as*
        an org, so no other org's slot could be the one it belongs in.
        """
        self.rebind(
            session,
            upload.org,
            ConfigBinding(config=upload.config, cwd=upload.root, upload=upload, org=upload.org),
        )

    def targets_for(self, org: str, session: str | None = None) -> list[str]:
        """The targets *org* may reach under the configuration *session* is bound to (BE-0015,
        BE-0375, BE-0393 unit 2).

        The one place ownership is decided, so a fourth reader cannot answer it differently from the
        three that exist (the target list, the cross-org guard, and the per-org scenario store): a
        configuration bound through the API belongs to the org that bound it, and only the launch
        configuration's own `orgs:` block partitions targets between orgs. Empty when no
        configuration is bound or it cannot be read.

        *session* is the request asking. Ownership is a property of the binding, so two sessions of
        one org bound to different configurations see different targets — and a session with no
        binding of its own sees the deployment's, whose `orgs:` block partitions as the operator
        wrote it. A caller with no session in hand (boot, a shared-token request) passes None and
        gets that same deployment-wide answer.
        """
        binding = self.binding_for(session, org)
        parsed = load_serve_config_file(binding.config)
        if parsed is None:
            return []
        return targets_for_org(parsed[1], parsed[0].targets, org, bound_by=binding.org)
