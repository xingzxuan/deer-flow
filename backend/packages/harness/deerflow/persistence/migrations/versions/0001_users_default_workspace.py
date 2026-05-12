"""users.default_workspace_id column + FK to workspaces

Revision ID: 0001_users_default_workspace
Revises: None
Create Date: 2026-05-12

First Alembic revision for the DeerFlow application schema. Adds
`users.default_workspace_id` so a newly-registered user can be sent
back to their default workspace on next login without consulting the
memberships table.

Existing deployments created `users` via `metadata.create_all()` without
this column; running `alembic upgrade head` on those DBs will simply add
the column (ON DELETE SET NULL FK), no data backfill needed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Alembic identifiers.
revision: str = "0001_users_default_workspace"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("default_workspace_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_users_default_workspace",
            "workspaces",
            ["default_workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_default_workspace", type_="foreignkey")
        batch.drop_column("default_workspace_id")
