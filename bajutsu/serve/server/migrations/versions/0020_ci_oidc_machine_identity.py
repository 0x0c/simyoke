"""give a CI job an identity of its own: machine sessions and their roster (BE-0414 units 1-2)

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on Postgres, portable JSON on the SQLite gate — the same variant `models._JSON` picks.
_JSON = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    # Who a session belongs to beyond its identity string (unit 1). Both null on existing rows and
    # no backfill runs: a session issued before this migration is a human one, which is exactly
    # what a null `kind` reads as (`sessions.kind_from_stored`), and only a machine session ever
    # carries an org here.
    op.add_column("sessions", sa.Column("org", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("kind", sa.String(), nullable=True))
    # The org's machine roster (unit 2), beside the human membership columns 0015-0017 added. Null
    # until an operator opts a repository in, which `orgs_from_db` reads as admitting nobody — so
    # the machine path stays off on every existing deployment until someone turns it on.
    op.add_column("orgs", sa.Column("allowed_repositories", _JSON, nullable=True))
    # The spent-token table. A cache by role and a table by necessity: the hosted control plane is
    # several replicas over one database, so single-use has to be decided somewhere they share.
    # The primary key *is* the rule — two replicas racing one token cannot both insert it.
    op.create_table(
        "oidc_jti",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    # The sweep each exchange runs deletes by expiry, so it reads this index rather than the table.
    op.create_index("ix_oidc_jti_expires_at", "oidc_jti", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_oidc_jti_expires_at", table_name="oidc_jti")
    op.drop_table("oidc_jti")
    # `batch_alter_table` for the same reason 0019 needed it: SQLite has no in-place DROP, so
    # alembic rebuilds the table, while Postgres takes the plain ALTER underneath the same call.
    with op.batch_alter_table("orgs") as batch:
        batch.drop_column("allowed_repositories")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("kind")
        batch.drop_column("org")
