"""ORM model for run metadata."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="运行主键")
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="所属会话 ID（threads_meta.thread_id）")
    assistant_id: Mapped[str | None] = mapped_column(String(128), comment="使用的 Assistant ID（自定义智能体名）；为 NULL 表示默认 lead agent")
    user_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="发起本次运行的用户 ID")
    workspace_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        comment="所属 workspace；PR5 期间 nullable（回填中），PR5 0003 迁移后改 NOT NULL",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        comment='运行状态："pending" / "running" / "success" / "error" / "timeout" / "interrupted"',
    )
    model_name: Mapped[str | None] = mapped_column(String(128), comment="本次运行的主模型名（来自 config.yaml.models[*].name）")
    multitask_strategy: Mapped[str] = mapped_column(
        String(20),
        default="reject",
        comment='并发策略：同一 thread 已有运行时怎么处理（"reject" / "interrupt" / "rollback" / "enqueue"）',
    )
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, comment="运行级元数据（JSON），如 channel/source 等")
    kwargs_json: Mapped[dict] = mapped_column(JSON, default=dict, comment="提交运行时的额外参数（JSON），如 thinking_enabled、tool 配置等")
    error: Mapped[str | None] = mapped_column(Text, comment="运行失败时的错误文本；成功时为 NULL")

    message_count: Mapped[int] = mapped_column(default=0, comment="本次运行产生的消息总数（便利字段，避免列表页查 RunEventStore）")
    first_human_message: Mapped[str | None] = mapped_column(Text, comment="首条用户消息文本预览（用于列表展示）")
    last_ai_message: Mapped[str | None] = mapped_column(Text, comment="末条 AI 消息文本预览（用于列表展示）")

    total_input_tokens: Mapped[int] = mapped_column(default=0, comment="累计输入 token 数（运行结束时由 RunJournal 落盘）")
    total_output_tokens: Mapped[int] = mapped_column(default=0, comment="累计输出 token 数")
    total_tokens: Mapped[int] = mapped_column(default=0, comment="累计 token 总数 = input + output")
    llm_call_count: Mapped[int] = mapped_column(default=0, comment="累计 LLM 调用次数")
    lead_agent_tokens: Mapped[int] = mapped_column(default=0, comment="主 agent 自身消耗的 token 数")
    subagent_tokens: Mapped[int] = mapped_column(default=0, comment="子 agent（task 工具委派）消耗的 token 数")
    middleware_tokens: Mapped[int] = mapped_column(default=0, comment="中间件（如 summarization、title）消耗的 token 数")

    follow_up_to_run_id: Mapped[str | None] = mapped_column(String(64), comment="续接的上一次运行 ID（用于'重新生成'/'继续'等链式调用）")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), comment="最近更新时间（UTC，写入时自动更新）")

    __table_args__ = (
        Index("ix_runs_thread_status", "thread_id", "status"),
        {"comment": "运行（一次完整 agent 执行）的元数据 + 累计 token 指标"},
    )
