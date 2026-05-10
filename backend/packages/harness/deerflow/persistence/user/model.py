"""ORM model for the users table.

Lives in the harness persistence package so it is picked up by
``Base.metadata.create_all()`` alongside ``threads_meta``, ``runs``,
``run_events``, and ``feedback``. Using the shared engine means:

- One SQLite/Postgres database, one connection pool
- One schema initialisation codepath
- Consistent async sessions across auth and persistence reads
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="用户主键，UUID 字符串（36 字符），跨数据库可移植")
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True, comment="登录邮箱，全局唯一")
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="本地账户的密码哈希；OAuth-only 用户为 NULL")
    system_role: Mapped[str] = mapped_column(String(16), nullable=False, default="user", comment='系统角色："admin" 或 "user"；用字符串以便未来扩展角色而不必 ALTER TABLE')
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="账户创建时间（UTC）",
    )
    oauth_provider: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="OAuth 提供商名（如 google/github）；本地账户为 NULL")
    oauth_id: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="OAuth 提供商内的用户 ID；与 oauth_provider 组合需唯一")
    needs_setup: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否需要完成首次设置（admin 自动创建后改密码/邮箱）")
    token_version: Mapped[int] = mapped_column(nullable=False, default=0, comment="JWT 令牌版本号；自增即吊销该用户所有旧令牌")

    __table_args__ = (
        Index(
            "idx_users_oauth_identity",
            "oauth_provider",
            "oauth_id",
            unique=True,
            sqlite_where=text("oauth_provider IS NOT NULL AND oauth_id IS NOT NULL"),
        ),
        {"comment": "用户账户表（本地密码登录 + OAuth 联合登录）"},
    )
