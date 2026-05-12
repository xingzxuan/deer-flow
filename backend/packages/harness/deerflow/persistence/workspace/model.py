"""ORM model for workspaces (multi-tenant scope)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class WorkspaceRow(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="工作空间主键，UUID 字符串（36 字符），与 users.id 类型对齐",
    )
    name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="工作空间显示名（用户注册时默认 <email 前缀>'s Workspace）",
    )
    slug: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        unique=True,
        comment="URL 标识（^[a-z0-9](-?[a-z0-9])*$，3-32 字符）；全局唯一，DB 存小写",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        comment='状态："active"（正常）/ "suspended"（平台 admin 暂停）/ "deleted"（软删）',
    )
    owner_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        comment="所有者 user_id；与 workspace_memberships 中 role='owner' 行严格一致（事务保证）；删除 owner 时 RESTRICT 阻拦（必须先转让所有权）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="创建时间（UTC）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        comment="最近更新时间（UTC，写入时自动更新）",
    )

    __table_args__ = ({"comment": ("工作空间表（多租户隔离粒度单位）。每个用户注册时自动建一个 1 人 workspace，owner 即注册者。团队订阅时 workspace 可有多个 member。")},)
