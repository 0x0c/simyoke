"""SQLAlchemy ORM models for the hosted backend's system of record (BE-0015 7a).

Imported only when a database-backed `Repository` is assembled (see `db.py`), never on the default
`serve`/CLI path — so SQLAlchemy stays behind the optional `db` extra. `org_id` threads through
every table so 7c's per-org scoping and quotas can filter on it; only the variable manifest summary
and audit detail use JSON (JSONB on Postgres, plain JSON on SQLite), keeping the relational core in
ordinary columns."""

from ._functions import _created_at as _created_at
from ._shared import _JSON as _JSON
from .audit_log import AuditLog
from .base import Base
from .job_record import JobRecord
from .oidc_jti import OidcJti
from .org import Org
from .provider_settings_row import ProviderSettingsRow
from .run import Run
from .secret import Secret
from .session_record import SessionRecord
from .user import User
from .user_org import UserOrg
from .worker_record import WorkerRecord

__all__ = [
    "AuditLog",
    "Base",
    "JobRecord",
    "OidcJti",
    "Org",
    "ProviderSettingsRow",
    "Run",
    "Secret",
    "SessionRecord",
    "User",
    "UserOrg",
    "WorkerRecord",
]
