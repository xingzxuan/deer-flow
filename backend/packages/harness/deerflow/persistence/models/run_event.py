"""ORM model for run events."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class RunEventRow(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, comment="自增主键")
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属会话 ID（threads_meta.thread_id）")
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属运行 ID（runs.run_id）")
    user_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="会话所有者；为 NULL 表示鉴权引入之前的历史数据，新写入由 auth 中间件填充，启动期 orphan 迁移会回填存量",
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="事件子类型（具体含义由 category 决定，如 ai_message_chunk、tool_call、run_started）")
    category: Mapped[str] = mapped_column(String(16), nullable=False, comment='事件大类："message" 消息 / "trace" 追踪 / "lifecycle" 生命周期')
    content: Mapped[str] = mapped_column(Text, default="", comment="事件文本内容（消息体、错误、状态字符串等）")
    event_metadata: Mapped[dict] = mapped_column(JSON, default=dict, comment="事件结构化元数据（JSON），随 event_type 而异")
    seq: Mapped[int] = mapped_column(nullable=False, comment="在 thread 内的全局递增序号；与 thread_id 组合唯一")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), comment="创建时间（UTC）")

    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_events_thread_seq"),
        Index("ix_events_thread_cat_seq", "thread_id", "category", "seq"),
        Index("ix_events_run", "thread_id", "run_id", "seq"),
        {"comment": "运行事件流（消息/追踪/生命周期事件按 seq 顺序追加，是消息回放与审计的真源）"},
    )
