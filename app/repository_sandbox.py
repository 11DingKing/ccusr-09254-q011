"""沙箱相关持久化：只写沙箱自有表，绝不触碰生产事件、冻结或通知。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import Sandbox, SandboxAdoption, SandboxEvent, SandboxRun


def create_sandbox_with_events(
    db: Session,
    *,
    sandbox_id: str,
    plan_version: str,
    draft_rules: dict[str, Any],
    event_cutoff_id: str | None,
    created_at: datetime,
    expires_at: datetime,
) -> tuple[Sandbox | None, bool]:
    """创建沙箱并在同一事务内固定事件副本；已存在时返回原沙箱。"""
    stmt = (
        sqlite_insert(Sandbox)
        .values(
            sandbox_id=sandbox_id,
            plan_version=plan_version,
            draft_rules=draft_rules,
            event_cutoff_id=event_cutoff_id,
            created_at=created_at,
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=["sandbox_id"])
        .returning(Sandbox.sandbox_id)
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    if inserted is None:
        db.rollback()
        return db.get(Sandbox, sandbox_id), False

    event_stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    if event_cutoff_id is not None:
        event_stmt = event_stmt.where(EventModel.event_id <= event_cutoff_id)
    else:
        event_stmt = event_stmt.where(False)
    for row in db.execute(event_stmt).scalars().all():
        db.add(
            SandboxEvent(
                sandbox_id=sandbox_id,
                event_id=row.event_id,
                plan_version=row.plan_version,
                student_id=row.student_id,
                event_type=row.event_type,
                payload=dict(row.payload),
                created_at=row.created_at,
            )
        )
    db.commit()
    return db.get(Sandbox, sandbox_id), True


def get_sandbox(db: Session, sandbox_id: str) -> Sandbox | None:
    return db.get(Sandbox, sandbox_id)


def list_sandboxes(db: Session, plan_version: str) -> list[Sandbox]:
    stmt = (
        select(Sandbox)
        .where(Sandbox.plan_version == plan_version)
        .order_by(Sandbox.created_at, Sandbox.sandbox_id)
    )
    return list(db.execute(stmt).scalars().all())


def list_expired_sandbox_ids(db: Session, now: datetime) -> list[str]:
    stmt = (
        select(Sandbox.sandbox_id)
        .where(Sandbox.expires_at <= now)
        .order_by(Sandbox.sandbox_id)
    )
    return list(db.execute(stmt).scalars().all())


def count_sandbox_events(db: Session, sandbox_id: str) -> int:
    stmt = select(SandboxEvent.id).where(SandboxEvent.sandbox_id == sandbox_id)
    return len(db.execute(stmt).scalars().all())


def load_sandbox_events(db: Session, sandbox_id: str) -> list[CoreEvent]:
    stmt = (
        select(SandboxEvent)
        .where(SandboxEvent.sandbox_id == sandbox_id)
        .order_by(SandboxEvent.event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [
        CoreEvent(
            event_id=row.event_id,
            plan_version=row.plan_version,
            event_type=EventType(row.event_type),
            student_id=row.student_id,
            payload=dict(row.payload),
            created_at=row.created_at,
        )
        for row in rows
    ]


def delete_sandbox(db: Session, sandbox_id: str) -> bool:
    """级联删除沙箱及其运行、事件副本与采纳申请。"""
    db.execute(delete(SandboxEvent).where(SandboxEvent.sandbox_id == sandbox_id))
    db.execute(delete(SandboxRun).where(SandboxRun.sandbox_id == sandbox_id))
    db.execute(
        delete(SandboxAdoption).where(SandboxAdoption.sandbox_id == sandbox_id)
    )
    result = db.execute(delete(Sandbox).where(Sandbox.sandbox_id == sandbox_id))
    db.commit()
    return (result.rowcount or 0) > 0


def insert_run(
    db: Session,
    *,
    run_id: str,
    sandbox_id: str,
    plan_version: str,
    production_rule_version: str,
    production_rules: dict[str, Any],
    draft_rules: dict[str, Any],
    lease_owner: str,
    lease_expires_at: datetime,
) -> tuple[SandboxRun | None, bool]:
    """登记运行；run_id 冲突时不覆盖，调用方读取已有记录实现重入。"""
    stmt = (
        sqlite_insert(SandboxRun)
        .values(
            run_id=run_id,
            sandbox_id=sandbox_id,
            plan_version=plan_version,
            status="running",
            production_rule_version=production_rule_version,
            production_rules=production_rules,
            draft_rules=draft_rules,
            attempt=1,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
        )
        .on_conflict_do_nothing(index_elements=["run_id"])
        .returning(SandboxRun.run_id)
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return db.get(SandboxRun, run_id), False
    return db.get(SandboxRun, run_id), True


def get_run(db: Session, run_id: str, *, fresh: bool = False) -> SandboxRun | None:
    if fresh:
        return db.get(SandboxRun, run_id, populate_existing=True)
    return db.get(SandboxRun, run_id)


def reclaim_run(
    db: Session,
    *,
    run_id: str,
    lease_owner: str,
    now: datetime,
    lease_expires_at: datetime,
) -> SandboxRun | None:
    """接管失败或租约过期的运行（崩溃重启路径），attempt 自增。"""
    stmt = (
        update(SandboxRun)
        .where(SandboxRun.run_id == run_id)
        .where(
            (SandboxRun.status == "failed")
            | (
                (SandboxRun.status == "running")
                & (SandboxRun.lease_expires_at <= now)
            )
        )
        .values(
            status="running",
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
            attempt=SandboxRun.attempt + 1,
            error=None,
        )
    )
    result = db.execute(stmt)
    db.commit()
    if (result.rowcount or 0) == 0:
        return None
    return db.get(SandboxRun, run_id)


def complete_run(
    db: Session,
    *,
    run_id: str,
    lease_owner: str,
    status: str,
    finished_at: datetime,
    production_candidates: list[dict[str, Any]] | None = None,
    draft_candidates: list[dict[str, Any]] | None = None,
    diff: dict[str, Any] | None = None,
    error: str | None = None,
) -> SandboxRun | None:
    """由租约持有者收尾运行；租约不符时拒绝写入，避免旧工作者覆盖。"""
    stmt = (
        update(SandboxRun)
        .where(SandboxRun.run_id == run_id)
        .where(SandboxRun.lease_owner == lease_owner)
        .where(SandboxRun.status == "running")
        .values(
            status=status,
            production_candidates=production_candidates,
            draft_candidates=draft_candidates,
            diff=diff,
            error=error,
            finished_at=finished_at,
        )
    )
    result = db.execute(stmt)
    db.commit()
    if (result.rowcount or 0) == 0:
        return None
    return db.get(SandboxRun, run_id)


def latest_run(db: Session, sandbox_id: str) -> SandboxRun | None:
    stmt = (
        select(SandboxRun)
        .where(SandboxRun.sandbox_id == sandbox_id)
        .order_by(SandboxRun.created_at.desc(), SandboxRun.run_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def latest_succeeded_run(db: Session, sandbox_id: str) -> SandboxRun | None:
    stmt = (
        select(SandboxRun)
        .where(SandboxRun.sandbox_id == sandbox_id)
        .where(SandboxRun.status == "succeeded")
        .order_by(SandboxRun.created_at.desc(), SandboxRun.run_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def insert_adoption(
    db: Session,
    *,
    adoption_id: str,
    sandbox_id: str,
    run_id: str,
    plan_version: str,
    requested_by: str,
    rule_version: str,
    rule_set: dict[str, Any],
) -> tuple[SandboxAdoption | None, bool]:
    """登记采纳申请；同一沙箱只保留一条，重复调用返回原记录。"""
    stmt = (
        sqlite_insert(SandboxAdoption)
        .values(
            adoption_id=adoption_id,
            sandbox_id=sandbox_id,
            run_id=run_id,
            plan_version=plan_version,
            status="pending",
            requested_by=requested_by,
            rule_version=rule_version,
            rule_set=rule_set,
        )
        .on_conflict_do_nothing(index_elements=["sandbox_id"])
        .returning(SandboxAdoption.adoption_id)
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        stmt2 = select(SandboxAdoption).where(SandboxAdoption.sandbox_id == sandbox_id)
        return db.execute(stmt2).scalars().first(), False
    return db.get(SandboxAdoption, inserted), True


def get_adoption_for_sandbox(
    db: Session, sandbox_id: str
) -> SandboxAdoption | None:
    stmt = select(SandboxAdoption).where(SandboxAdoption.sandbox_id == sandbox_id)
    return db.execute(stmt).scalars().first()


def decide_adoption(
    db: Session,
    *,
    adoption_id: str,
    decision: str,
    decided_by: str,
    decided_at: datetime,
    reason: str,
) -> SandboxAdoption | None:
    """审批采纳申请；只有 pending 状态可被决定，保证审批只发生一次。"""
    stmt = (
        update(SandboxAdoption)
        .where(SandboxAdoption.adoption_id == adoption_id)
        .where(SandboxAdoption.status == "pending")
        .values(
            status=decision,
            decided_by=decided_by,
            decided_at=decided_at,
            decision_reason=reason,
        )
    )
    result = db.execute(stmt)
    db.commit()
    if (result.rowcount or 0) == 0:
        return None
    return db.get(SandboxAdoption, adoption_id)


def latest_approved_adoption(
    db: Session, plan_version: str
) -> SandboxAdoption | None:
    stmt = (
        select(SandboxAdoption)
        .where(SandboxAdoption.plan_version == plan_version)
        .where(SandboxAdoption.status == "approved")
        .order_by(SandboxAdoption.created_at.desc(), SandboxAdoption.adoption_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()
