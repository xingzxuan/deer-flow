"""ORM model for workspace memberships (which user is in which workspace, with what role)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class WorkspaceMembershipRow(Base):
    __tablename__ = "workspace_memberships"

    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
        comment="所属 workspace；workspace 删除时级联清掉成员记录",
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        comment="成员 user_id；用户删除时级联清掉成员记录",
    )
    role: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment='角色字符串：Stage 0 仅写 "owner"；Stage 2 RBAC 打开 "admin"/"member"。用 String(16) 而非 enum 以便未来加 "viewer"/"auditor" 不动 schema',
    )
    invited_by: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="邀请人 user_id（Stage 2 invitation 流程才写）；邀请人被删时此字段清空（不影响成员记录本身）",
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="加入 workspace 的时间（UTC）",
    )

    __table_args__ = (
        # 倒查索引：列出 user 所属的所有 workspace（/auth/me 用）。
        # 复合主键 (workspace_id, user_id) 的前导列是 workspace_id，
        # 所以"按 user_id 查"需要单独的索引。
        Index("idx_workspace_memberships_user", "user_id", "workspace_id"),
        # 一个 workspace 严格 1 个 owner —— partial unique on role='owner'。
        # SQLite + Postgres 都支持 WHERE 子句的 partial unique；双驱动
        # 维护两套等价 where 表达式。
        Index(
            "idx_one_owner_per_workspace",
            "workspace_id",
            unique=True,
            sqlite_where=text("role = 'owner'"),
            postgresql_where=text("role = 'owner'"),
        ),
        {"comment": ("工作空间成员表（multi-tenant RBAC）。复合 PK (workspace_id, user_id)；每个 workspace 必有恰好 1 个 owner（partial unique 约束保证）。Stage 0 仅写 owner；Stage 2 RBAC 打开 admin/member。")},
    )
