"""The persistence seam the control plane reads and writes through."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from ._shared import DEFAULT_LEASE_MAX_ATTEMPTS, DEFAULT_RUN_LIMIT
from .job_metrics import JobMetrics
from .leased_job import LeasedJob
from .org_record import OrgRecord
from .run_record import RunRecord


@runtime_checkable
class Repository(Protocol):
    """Persistence for the control plane. 7a covers runs; identity/audit land in 7b/7c."""

    def record_run(self, run: RunRecord) -> None:
        """Insert *run*, or update it in place when its id already exists (e.g. a status change)."""

    def get_run(self, run_id: str) -> RunRecord | None:
        """The run with *run_id*, or None if there is none."""

    def list_runs(
        self,
        *,
        org_id: str,
        label: str | None = None,
        target: str | None = None,
        limit: int | None = DEFAULT_RUN_LIMIT,
        include_deleted: bool = False,
    ) -> list[RunRecord]:
        """An org's runs, newest first, capped at *limit*; ``None`` means unbounded.

        *label* and *target* narrow the result to that partition and that target when given
        (BE-0404 units 2 and 4); an unlabeled run matches every *label*, since it belongs to no
        partition. Soft-deleted runs are excluded unless *include_deleted* (BE-0239).
        """

    def soft_delete_run(
        self, run_id: str, *, org_id: str, deleted_by: str | None, at: datetime
    ) -> bool:
        """Mark the org's run *run_id* trashed at *at* by *deleted_by* (BE-0239). True when a live
        run was trashed, False when there was none or it was already trashed (org-scoped, so another
        org's run is untouched — a not-found)."""

    def restore_run(self, run_id: str, *, org_id: str) -> bool:
        """Clear the org's run *run_id*'s soft-delete marker (BE-0239). True when a trashed run was
        restored, False otherwise."""

    def purge_run(self, run_id: str, *, org_id: str) -> bool:
        """Delete the org's run *run_id* row outright (BE-0239). True when a row was removed. The
        audit-log entry keyed on the run id survives, so "who purged run X, when" stays answerable
        without this row."""

    def list_deleted_runs(self, *, org_id: str, before: datetime) -> list[RunRecord]:
        """The org's soft-deleted runs trashed at or before *before* — the retention sweep's DB-side
        eligibility scan (BE-0239). Reaches a run trashed only in the DB (soft-deleted before any
        evidence upload, so it never got a store tombstone) that the store-side scan misses."""

    def ensure_org(self, org_id: str, *, slug: str, name: str) -> None:
        """Create the org if it does not exist yet (idempotent) — 7c-1's single default org.

        Deliberately still create-only (BE-0375): sign-in and job completion call it on every
        request with no membership to pass, so widening it into a create-or-update would let the
        next sign-in clear membership an admin set. `seed_org_membership` writes membership instead.
        """

    def list_orgs(self, *, include_deleted: bool = False) -> list[OrgRecord]:
        """Every org, ordered by slug; soft-deleted ones only with *include_deleted* (BE-0375)."""

    def get_org(self, org_id: str, *, include_deleted: bool = False) -> OrgRecord | None:
        """The org with *org_id*, or None — a soft-deleted one only with *include_deleted*."""

    def create_org(self, *, slug: str, name: str) -> bool:
        """Create an admin-managed org with empty membership, marked seeded (BE-0375).

        The row's id is its slug, matching what every existing writer already carries as `org_id`.
        Marked seeded at creation so no later `orgs:` entry for the same slug can overwrite the
        membership an admin sets. False when the slug is already taken — including by a soft-deleted
        row, which still occupies the UNIQUE constraint; reactivating one is a separate operation
        this seam does not offer.
        """

    def set_org_config_source(self, org_id: str, source: dict[str, Any]) -> bool:
        """Remember *source* as the configuration this org last bound. False when there is no such
        live org.

        One record per org, overwritten by each bind — a second bind replaces the first locator
        rather than accumulating a named list, which is the project layer returning under another
        name. Every bind writes it, not only an upload one; `Org.config_source` records why.
        """

    def set_org_membership(
        self,
        org_id: str,
        *,
        members: list[str],
        github_orgs: list[str],
        github_teams: list[str],
        editor_teams: list[str],
        allowed_repositories: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Replace a live org's membership as one unit (BE-0375). False when there is no such org.

        Stamps `membership_seeded_at` when it is not yet set: an API write is a cutover event just
        as creation is, so no later `orgs:` entry can seed over what an admin set here.

        *allowed_repositories* is the exception to "as one unit": None leaves the machine roster
        (BE-0414 unit 2) untouched, and only an explicit list — `[]` included — replaces it. The
        human roster is safe to replace wholesale because every caller predating a field sends the
        others; a caller predating *this* field would otherwise revoke every pipeline's access and
        get a 200 back saying so, the failure `editorTeam`'s loud refusal exists to prevent.
        """

    def seed_org_membership(
        self,
        org_id: str,
        *,
        slug: str,
        name: str,
        members: list[str],
        github_orgs: list[str],
        github_teams: list[str],
        editor_teams: list[str],
        allowed_repositories: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Seed an org's membership from a bound config's `orgs:` entry, once (BE-0375).

        Creates the row when it does not exist, or fills in one `ensure_org` left holding nothing
        but an id, a slug, and a name; either way it stamps `membership_seeded_at`, after which the
        database owns that org's membership and this is a no-op. Returns whether it seeded. A row
        already marked seeded, and a soft-deleted one, are both left alone — retired, not unseeded.
        """

    def spend_oidc_jti(self, jti: str, *, expires_at: datetime) -> bool:
        """Claim *jti* as spent, returning False when it already was (BE-0414 unit 1).

        The single-use rule behind the OIDC exchange, in the shared system of record rather than
        in a process: a hosted control plane runs several replicas over one database, and a
        per-process cache would let a captured token be replayed against a second replica. A row is
        swept only once it is past *expires_at* by more than the clock skew the lifetime checks
        allow, since a token inside that window is still acceptable and must still be refused here
        — so the table stays bounded with no schedule of its own.
        """

    def soft_delete_org(self, org_id: str, *, at: datetime) -> bool:
        """Mark the org deleted at *at* (BE-0375). False when there is none, or it already was.

        A soft delete, not a row removal: `users`, `runs`, `secrets`, `provider_settings`, and
        `audit_log` still hold foreign keys on this id — including the delete's own audit entry.
        """

    def list_org_user_ids(self, org_id: str) -> list[str]:
        """Every user id recorded under *org_id* — whose sessions retiring the org revokes (BE-0375).

        Read before the soft delete, not after: the delete leaves `users.org_id` pointing at the
        retired slug, so the set is the same either way, but reading first keeps the caller from
        depending on that.
        """

    def upsert_user(
        self, user_id: str, *, org_id: str, github_login: str, email: str, role: str = "editor"
    ) -> None:
        """Insert the user, or update it in place when its id already exists (an OAuth re-login),
        setting its *role* (recomputed from policy each login, BE-0015 7c-2).

        A re-login that lands the user in a *different* org also clears the marker saying they picked
        their active org themselves: the pick was for the org they have left.
        """

    def user_role(self, user_id: str) -> str | None:
        """The user's role (viewer/editor/admin), or None if there is no such user."""

    def user_org(self, user_id: str) -> str | None:
        """The user's org id, or None if there is no such user (BE-0015 multi-tenancy)."""

    def set_user_orgs(self, user_id: str, memberships: dict[str, str]) -> None:
        """Replace the set of orgs *user_id* may act as, mapping each org id to its role.

        A wholesale replacement rather than a merge, so losing a GitHub organization or Team takes
        effect on the next sign-in with no data migration — the same self-healing rule the role
        itself follows (BE-0313). An empty mapping clears the set.
        """

    def list_user_orgs(self, user_id: str) -> dict[str, str]:
        """The orgs *user_id* may act as, each mapped to the role held there. Empty for an unknown
        user, and for one whose only admission came from the admin-Team bypass (BE-0352), which no
        org's membership records."""

    def user_selected_org(self, user_id: str) -> str | None:
        """The org *user_id* picked themselves, or None when the active org was merely resolved for
        them at sign-in. Sign-in preserves a picked org and re-resolves an unpicked one, so the two
        cases must stay distinguishable."""

    def select_active_org(self, user_id: str, org_id: str, *, role: str) -> bool:
        """Make *org_id* the user's active org with *role*, and mark the choice as theirs.

        False when there is no such user. Authorization is the caller's: this writes the choice it
        is given, the way `upsert_user` writes the role it is given.
        """

    def record_audit(
        self, *, org_id: str, actor_id: str | None, action: str, target: str, detail: dict[str, Any]
    ) -> None:
        """Append an audit-log entry — who did what to which target, and when (server clock)."""

    def enqueue_job(
        self, job_id: str, org_id: str, spec: dict[str, Any], capabilities: Iterable[str] = ()
    ) -> None:
        """Insert a job with status ``queued`` and its required-capability routing key (BE-0166)."""

    def max_job_id(self) -> int:
        """The highest numeric job id persisted so far, or 0 if the table is empty.

        `JobRegistry` assigns ids from an in-process counter that restarts at 0 on every process
        restart, while the ``jobs`` table survives it — so a fresh process reissues ids already
        taken and its first insert hits a duplicate-key error. The control plane seeds its counter
        from this at startup so a restart resumes past every id already on disk.
        """

    def register_worker(self, worker_id: str, capabilities: Iterable[str]) -> None:
        """Record what *worker_id* can serve and that it is live now (BE-0166 routing).

        Called on every lease poll — including an empty-queue poll — so an idle worker still refreshes
        its liveness and keeps counting toward what the pool can route (else its jobs would look
        unroutable). Idempotent upsert keyed by *worker_id*.
        """

    def lease_job(self, worker_id: str, capabilities: Iterable[str] = ()) -> LeasedJob | None:
        """Atomically lease the oldest queued job *worker_id* can serve, or return None (BE-0166).

        A job is a candidate only when its required-capability set is a subset of *capabilities* —
        so a worker never leases a job it cannot run. A job no live worker can serve simply stays
        queued (surfaced as unroutable via `metrics_snapshot`), never leased to an incompatible one.
        """

    def touch_worker(self, worker_id: str) -> None:
        """Refresh *worker_id*'s liveness without changing its capabilities (BE-0166 routing).

        A worker polls `lease` only between jobs, so a worker busy on a run longer than the lease
        timeout would otherwise age out of the live set and make its capability's queued jobs look
        unroutable. The heartbeat calls this so a busy worker stays counted as live. A no-op if the
        worker has no registry row yet (it registers on its first lease before any heartbeat).
        """

    def heartbeat_job(self, job_id: str, worker_id: str) -> bool:
        """Renew a lease's timer, returning False when *worker_id* no longer owns the live lease.

        The worker calls this on an interval during a run so a legitimately long run is not
        reclaimed; a False answer tells the worker its lease was reclaimed (or the job finished) and
        it should stop.
        """

    def reclaim_expired_leases(
        self, timeout: timedelta, *, max_attempts: int = DEFAULT_LEASE_MAX_ATTEMPTS
    ) -> list[str]:
        """Re-queue leases with no heartbeat within *timeout*; fail the ones past *max_attempts*.

        Returns the ids re-queued (available again). A worker that dies mid-run stops heart-beating,
        so its lease ages past the timeout and returns to ``queued`` for another worker — but a
        poison job that keeps killing its worker is failed once it hits the attempt cap.
        """

    def complete_job(
        self, job_id: str, result: dict[str, Any], *, worker_id: str | None = None
    ) -> bool:
        """Mark a still-leased job ``done`` with its *result*; False if it is no longer leasable.

        A reclaimed, re-leased, or already-finished job rejects the write (when *worker_id* is
        given, only that leaseholder may complete it), so a stale worker never overwrites the winner.
        """

    def fail_job(self, job_id: str, error: str, *, worker_id: str | None = None) -> bool:
        """Mark a still-leased job ``failed`` with *error*; False if it is no longer leasable (see
        `complete_job`)."""

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Return the job's status, result, org_id, current lease holder (``leased_by``), and
        ``spec``, or None if it does not exist.

        The spec rides along because a worker's result carries no `actor` / `label` of its own:
        `worker_result` records the finished run under the identity and run-history partition the
        control plane resolved at enqueue. The row is loaded whole either way, so it costs a dict
        copy — unlike a per-id `get_job` in a loop, which `finished_job_ids` exists to avoid."""

    def finished_job_ids(self, job_ids: Iterable[str]) -> set[str]:
        """Which of *job_ids* have reached a terminal state (``done`` / ``failed``).

        The control plane asks this on every dispatch and every metrics scrape to release the
        concurrency-cap slots of jobs that finished on a worker, so it is one narrow read for the
        whole set rather than a row per id: `spec` carries a run's materials (its scenario and config
        text), which a `get_job` per id would ship every time — the same reason `metrics_snapshot`
        selects the columns it needs. An id with no row is simply absent from the answer.
        """

    def metrics_snapshot(self) -> JobMetrics:
        """A one-pass aggregate of the jobs table for the ``/metrics`` endpoint (BE-0169)."""

    def save_batch_run_arn(self, job_id: str, run_arn: str) -> None:
        """Persist the scheduled Device Farm run ARN for *job_id* (BE-0336 Unit 5).

        A worker records the ARN the moment the cloud-batch run is scheduled, so a worker that
        re-leases the job after a restart resumes polling that run rather than resubmitting. A no-op
        if the job row is gone.
        """

    def load_batch_run_arn(self, job_id: str) -> str | None:
        """The scheduled Device Farm run ARN persisted for *job_id*, or None (BE-0336 Unit 5).

        None when the run is not yet scheduled, or the job does not exist — the caller then submits a
        fresh run rather than resuming.
        """
