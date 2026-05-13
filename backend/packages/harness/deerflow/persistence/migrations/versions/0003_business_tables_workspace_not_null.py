"""Business tables: workspace_id NOT NULL + UNIQUE(workspace_id, thread_id)

Revision ID: 0003_business_tables_workspace_not_null
Revises: 0002_business_tables_workspace
Create Date: 2026-05-13

Stage 0 PR5 step 2/2. Flips ``workspace_id`` on the four business
tables to ``NOT NULL`` and adds the threads_meta ``(workspace_id,
thread_id)`` UNIQUE index promised in workspace-schema-design §4.

**Refuses to upgrade** if any of the four tables still has rows with
``workspace_id IS NULL`` — the operator must run
``scripts/backfill_workspace_id.py`` first. Doing the NOT NULL ALTER
with stragglers in place would either fail (Postgres) or silently
corrupt SQLite tables via ``batch_alter_table`` rebuilds.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_business_tables_workspace_not_null"
down_revision: str | None = "0002_business_tables_workspace"
branch_labels: str | None = None
depends_on: str | None = None

_BUSINESS_TABLES = ("threads_meta", "runs", "feedback", "run_events")


class _BackfillRequiredError(RuntimeError):
    """Raised when null workspace_id rows remain at the start of upgrade."""


def upgrade() -> None:
    conn = op.get_bind()
    for table in _BUSINESS_TABLES:
        # Quoted identifier is fine here — table names are constants in this
        # module, no operator input reaches the SQL.
        count = conn.execute(sa.text(f"SELECT count(*) FROM {table} WHERE workspace_id IS NULL")).scalar() or 0
        if count > 0:
            raise _BackfillRequiredError(
                f"Cannot ALTER {table}.workspace_id to NOT NULL: {count} row(s) still have workspace_id=NULL. Run `python scripts/backfill_workspace_id.py` first.",
            )

    for table in _BUSINESS_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column("workspace_id", existing_type=sa.String(36), nullable=False)

    op.create_index(
        "idx_threads_meta_workspace_thread",
        "threads_meta",
        ["workspace_id", "thread_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("idx_threads_meta_workspace_thread", table_name="threads_meta")
    for table in reversed(_BUSINESS_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.alter_column("workspace_id", existing_type=sa.String(36), nullable=True)
