"""ORM model for service accounts (non-human principals inside a workspace)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class ServiceAccountRow(Base):
    __tablename__ = "service_accounts"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="服务账号主键，UUID 字符串（36 字符），与 users.id 类型对齐",
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属 workspace；workspace 删除时级联清掉所有 service_account（连同其 api_keys / external_users）",
    )
    name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="服务账号显示名（同 workspace 内不强制唯一；Stage 1 可由 admin UI 重复使用同名 + 不同 key）",
    )
    role: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="member",
        comment='服务账号在 workspace 内的角色字符串：Stage 0 仅支持 "member"；Stage 2 RBAC 打开 "admin"/"viewer"。用 String(16) 而非 enum 以便未来扩枚举值不动 schema',
    )
    identity_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="collapsed",
        comment=(
            '身份模式三态："collapsed"（所有调用 collapse 到该 service_account；不记录 external_user）/'
            ' "external_passthrough"（每次调用必带 X-External-User-Id，写入 external_users 表）/'
            ' "both"（带就写、不带就 collapse）。Stage 1 API key 鉴权层据此分流'
        ),
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        comment='状态："active"（正常）/ "suspended"（admin 暂停）/ "deleted"（软删；保留审计）',
    )
    created_by: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        comment="创建者 user_id；删除该 user 时 RESTRICT 阻拦（必须先转移或删除该 user 名下所有 service_account）",
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

    __table_args__ = (
        Index("idx_service_accounts_workspace", "workspace_id", "status"),
        {
            "comment": (
                "服务账号表（headless API 的非人身份）。每个 service_account 属于唯一 workspace；"
                "通过 api_keys 表的 API key 鉴权调用 Gateway；identity_mode 控制是否在 external_users 表"
                "记录终端用户身份。Stage 0 仅落 schema；Stage 1 起接 API key 鉴权 + 路由 scope 升级。"
            )
        },
    )
