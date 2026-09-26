"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_naive() -> datetime:
    """新表统一存储 naive UTC，避免 SQLite 字符串时间排序混入时区后缀。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class RuleSandbox(Base):
    """只读异常规则沙箱：固定事件与规则草案，隔离运行候选异常。"""

    __tablename__ = "rule_sandboxes"

    sandbox_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)

    # 创建时冻结的事件快照与事件上界（数据更新隔离）。
    event_snapshot: Mapped[list] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 创建时生效的生产规则版本与阈值（规则版本隔离）。
    production_rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    production_thresholds: Mapped[dict] = mapped_column(JSON, nullable=False)
    draft_thresholds: Mapped[dict] = mapped_column(JSON, nullable=False)

    # 运行结果（候选异常不进入正式案件）。
    production_findings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    draft_findings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    comparison: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # 运行租约：支持重入、并发去重与崩溃后接管重启。
    run_lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    run_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 沙箱结果有期限。
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ran_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    adopted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow_naive
    )

    __table_args__ = (
        Index("ix_rule_sandboxes_plan_status", "plan_version", "status"),
        CheckConstraint("event_count >= 0", name="ck_sandboxes_event_count_nonneg"),
        CheckConstraint("attempts >= 0", name="ck_sandboxes_attempts_nonneg"),
    )


class RulePublishRequest(Base):
    """沙箱草案采纳后的发布审批单：审批通过才会生效。"""

    __tablename__ = "rule_publish_requests"

    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    sandbox_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    proposed_thresholds: Mapped[dict] = mapped_column(JSON, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow_naive
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RuleRelease(Base):
    """已发布生效的异常规则版本（每个培养方案至多一条 active）。"""

    __tablename__ = "rule_releases"

    rule_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    thresholds: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    published_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow_naive
    )

    __table_args__ = (
        Index("ix_rule_releases_plan_status", "plan_version", "status"),
    )
