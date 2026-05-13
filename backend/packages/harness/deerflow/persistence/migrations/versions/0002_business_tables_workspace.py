"""Business tables: nullable workspace_id + FK to workspaces

Revision ID: 0002_business_tables_workspace
Revises: 0001_users_default_workspace
Create Date: 2026-05-13

Stage 0 PR5 step 1/2 (the second step lives in revision 0003).

This revision adds a *nullable* ``workspace_id`` column to the four
business tables that need tenancy scoping:

  * ``threads_meta``
  * ``runs``
  * ``feedback``
  * ``run_events``

The column is nullable here on purpose — running ``upgrade`` on a
database with existing rows leaves those rows with ``workspace_id = NULL``
until ``scripts/backfill_workspace_id.py`` populates them. Once the
backfill finishes, revision 0003 flips the column to ``NOT NULL`` and
adds the threads_meta ``(workspace_id, thread_id)`` UNIQUE index.

A composite index ``idx_threads_meta_workspace_user_updated`` is added
on ``threads_meta`` to support the common "list a workspace's threads
for a user, newest first" access pattern that PR6 routers will rely on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_business_tables_workspace"
down_revision: str | None = "0001_users_default_workspace"
branch_labels: str | None = None
depends_on: str | None = None


# Tables that receive the new column. The order matters only for human
# readability in migration logs — there are no inter-table data deps
# during ALTER, and the FK references workspaces (introduced in PR3) which
# is already present at this point.
_BUSINESS_TABLES = ("threads_meta", "runs", "feedback", "run_events")


def upgrade() -> None:
    for table in _BUSINESS_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("workspace_id", sa.String(36), nullable=True))
            batch.create_foreign_key(
                f"fk_{table}_workspace_id",
                "workspaces",
                ["workspace_id"],
                ["id"],
                ondelete="CASCADE",
            )

    op.create_index(
        "idx_threads_meta_workspace_user_updated",
        "threads_meta",
        ["workspace_id", "user_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_threads_meta_workspace_user_updated", table_name="threads_meta")
    for table in reversed(_BUSINESS_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"fk_{table}_workspace_id", type_="foreignkey")
            batch.drop_column("workspace_id")
