"""ORM model for external users (end-user identities passed through a service account)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class ExternalUserRow(Base):
    __tablename__ = "external_users"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="external_user 主键，UUID 字符串（36 字符）",
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属 workspace（冗余存储——可经 service_account 间接得到，但直接存以加速 workspace-scope 查询）；workspace 删除时级联",
    )
    service_account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("service_accounts.id", ondelete="CASCADE"),
        nullable=False,
        comment="passthrough 的 service_account；service_account 删除时级联",
    )
    external_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="终端调用方传入的 X-External-User-Id（最多 128 字符；推荐 UUID / opaque token，不要塞 PII）",
    )
    display_name: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="可选显示名（如 'alice@customer.com'）；仅用于 admin UI 展示，不参与鉴权",
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="任意 JSON 附属信息（plan tier / region / 自定义 tag）；Stage 1 由 upsert 调用方写入",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="首次见到该 external_id 的时间（UTC）",
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最近一次该 external_id 触发请求的时间（UTC）；Stage 1 鉴权层每次更新",
    )

    __table_args__ = (
        UniqueConstraint("service_account_id", "external_id", name="uq_external_users_sa_external"),
        {
            "comment": (
                "终端用户身份表（passthrough 模式下的 end-user）。每行由 service_account 的鉴权中间件 upsert——同一 "
                "(service_account_id, external_id) 组合只存一行。workspace_id 冗余存储以加速跨 SA 的 workspace-scope 聚合查询。"
                "Stage 0 仅落 schema；Stage 1 起接 upsert / 配额聚合。"
            )
        },
    )
