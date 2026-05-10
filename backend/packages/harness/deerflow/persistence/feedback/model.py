"""ORM model for user feedback on runs."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class FeedbackRow(Base):
    __tablename__ = "feedback"

    __table_args__ = (
        UniqueConstraint("thread_id", "run_id", "user_id", name="uq_feedback_thread_run_user"),
        {"comment": "用户对运行结果的反馈（点赞/点踩 + 文字评论），(thread, run, user) 唯一"},
    )

    feedback_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="反馈主键")
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="关联的运行 ID（runs.run_id）")
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="关联的会话 ID（threads_meta.thread_id）")
    user_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="反馈作者；为 NULL 表示历史无主数据")
    message_id: Mapped[str | None] = mapped_column(String(64), comment="可选的 RunEventStore 事件 ID；为 NULL 表示针对整次运行而非单条消息")
    rating: Mapped[int] = mapped_column(nullable=False, comment="评分：+1 点赞，-1 点踩")
    comment: Mapped[str | None] = mapped_column(Text, comment="可选的文字评论")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), comment="创建时间（UTC）")
