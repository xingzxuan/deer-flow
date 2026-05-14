"""ORM model for API keys (credentials owned by a service account)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class ApiKeyRow(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="API key 主键，UUID 字符串（36 字符）",
    )
    service_account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("service_accounts.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属 service_account；service_account 删除时级联清掉所有 api_key",
    )
    key_prefix: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        unique=True,
        comment="公开 prefix（如 'dfk_live_abc12345'），可在审计日志 / UI 中打印；全局唯一，撤销后亦不复用以避免审计混淆",
    )
    key_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="完整 token 的 sha-256 hex（64 字符；预留 128 以兼容未来更长哈希），plaintext token 仅在创建时返回给调用方",
    )
    name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="key 的人类可读标签（如 'ci pipeline' / 'frontend prod'），同 service_account 内不强制唯一",
    )
    scopes: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
        default="",
        comment="scope 列表，逗号分隔字符串（如 'threads:read,threads:write'）；用 String 而非 PG text[] 以保 SQLite dev 兼容，Stage 2 切纯 PG 后可平滑迁",
    )
    rate_limit_rpm: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="每分钟请求数限制；NULL 表示走该 service_account 的默认限速（Stage 1 起生效）",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="过期时间（UTC）；NULL = 不过期",
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最近一次成功鉴权时间（UTC）；Stage 1 鉴权中间件每次更新",
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="撤销时间（UTC）；NULL = 仍然有效。被撤销的 key 不删行（保留审计），但鉴权层据此拒绝",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="创建时间（UTC）",
    )

    __table_args__ = (
        Index("idx_api_keys_sa", "service_account_id"),
        # 部分索引：只索引活跃 key（revoked_at IS NULL），鉴权热路径走 prefix lookup，
        # 撤销后的 key 不进活跃索引以减小热索引大小。SQLite + Postgres 均支持
        # WHERE 子句的部分索引；双驱动维护两套等价 where 表达式。
        Index(
            "idx_api_keys_active",
            "key_prefix",
            sqlite_where=text("revoked_at IS NULL"),
            postgresql_where=text("revoked_at IS NULL"),
        ),
        {
            "comment": (
                "API key 表（headless API 凭证）。每行属于唯一 service_account；key_prefix 全局唯一可在日志中打印，"
                "key_hash 是完整 token 的 sha-256，plaintext token 只在创建时返给调用方。撤销保留行（revoked_at 非空），"
                "活跃 key 走部分索引 idx_api_keys_active 加速鉴权热路径。Stage 0 仅落 schema；Stage 1 起接鉴权 + 限速。"
            )
        },
    )
