"""ORM model for thread metadata."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class ThreadMetaRow(Base):
    __tablename__ = "threads_meta"

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="会话主键（LangGraph thread_id）")
    assistant_id: Mapped[str | None] = mapped_column(String(128), index=True, comment="关联的 Assistant ID（自定义智能体名）；为 NULL 表示默认 lead agent")
    user_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="会话所有者；为 NULL 表示历史无主数据")
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属 workspace。PR5 引入时 nullable 用于回填；alembic 0003 + PR6 仓储接入完成后 NOT NULL",
    )
    display_name: Mapped[str | None] = mapped_column(String(256), comment="会话显示名（自动生成的标题或用户手改）")
    status: Mapped[str] = mapped_column(String(20), default="idle", comment='会话状态："idle" 空闲 / "busy" 正在产出')
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, comment="任意扩展元数据（JSON）")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), comment="最近更新时间（UTC，写入时自动更新）")

    __table_args__ = (
        # Workspace-scoped "list threads of this user, newest first" index;
        # added by alembic 0002. Mirrored on the ORM side so create_all()
        # produces the same shape on fresh dev databases.
        Index("idx_threads_meta_workspace_user_updated", "workspace_id", "user_id", "updated_at"),
        {"comment": "会话元数据（每个 LangGraph thread 的概要信息）"},
    )
