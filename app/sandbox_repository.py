"""只读异常规则沙箱的持久化操作。

运行租约通过条件 UPDATE 原子获取，保证多线程/多进程并发运行时只有一个
执行者；租约过期后可被其他执行者接管，从而支持任务重入与崩溃重启。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import RulePublishRequest, RuleRelease, RuleSandbox


def _naive_utc(value: datetime) -> datetime:
    """SQLite 以 naive 字符串存储时间，绑定参数需统一为 naive UTC 才能比较。"""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def get_sandbox(db: Session, sandbox_id: str) -> RuleSandbox | None:
    return db.get(RuleSandbox, sandbox_id)


def insert_sandbox(db: Session, values: dict[str, Any]) -> RuleSandbox | None:
    values = dict(values)
    if "expires_at" in values:
        values["expires_at"] = _naive_utc(values["expires_at"])
    stmt = sqlite_insert(RuleSandbox).values(**values)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["sandbox_id"]
    ).returning(RuleSandbox.sandbox_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(RuleSandbox, values["sandbox_id"])


def acquire_run_lease(
    db: Session,
    *,
    sandbox_id: str,
    owner: str,
    now: datetime,
    lease_expires_at: datetime,
) -> bool:
    """原子获取运行租约。

    允许获取的条件：沙箱处于 ready/failed，或处于 running 但原租约已过期
    （前执行者崩溃，接管重启）。
    """
    stmt = (
        update(RuleSandbox)
        .where(RuleSandbox.sandbox_id == sandbox_id)
        .where(
            (RuleSandbox.status.in_(("ready", "failed")))
            | (
                (RuleSandbox.status == "running")
                & (RuleSandbox.run_lease_expires_at.is_not(None))
                & (RuleSandbox.run_lease_expires_at < _naive_utc(now))
            )
        )
        .values(
            status="running",
            run_lease_owner=owner,
            run_lease_expires_at=_naive_utc(lease_expires_at),
            attempts=RuleSandbox.attempts + 1,
            last_error=None,
        )
    )
    rowcount = db.execute(stmt).rowcount
    db.commit()
    return rowcount == 1


def complete_run(
    db: Session,
    *,
    sandbox_id: str,
    owner: str,
    now: datetime,
    production_findings: list[dict[str, Any]],
    draft_findings: list[dict[str, Any]],
    comparison: dict[str, Any],
) -> bool:
    """仅租约持有者可以提交运行结果。"""
    stmt = (
        update(RuleSandbox)
        .where(RuleSandbox.sandbox_id == sandbox_id)
        .where(RuleSandbox.run_lease_owner == owner)
        .where(RuleSandbox.status == "running")
        .values(
            status="completed",
            run_lease_owner=None,
            run_lease_expires_at=None,
            ran_at=_naive_utc(now),
            production_findings=production_findings,
            draft_findings=draft_findings,
            comparison=comparison,
            last_error=None,
        )
    )
    rowcount = db.execute(stmt).rowcount
    db.commit()
    return rowcount == 1


def fail_run(
    db: Session,
    *,
    sandbox_id: str,
    owner: str,
    now: datetime,
    error: str,
) -> bool:
    stmt = (
        update(RuleSandbox)
        .where(RuleSandbox.sandbox_id == sandbox_id)
        .where(RuleSandbox.run_lease_owner == owner)
        .where(RuleSandbox.status == "running")
        .values(
            status="failed",
            run_lease_owner=None,
            run_lease_expires_at=None,
            last_error=error[:1000],
        )
    )
    rowcount = db.execute(stmt).rowcount
    db.commit()
    return rowcount == 1


def mark_expired(db: Session, *, sandbox_id: str, now: datetime) -> bool:
    stmt = (
        update(RuleSandbox)
        .where(RuleSandbox.sandbox_id == sandbox_id)
        .where(RuleSandbox.expires_at <= _naive_utc(now))
        .where(RuleSandbox.status.in_(("ready", "running", "failed")))
        .values(status="expired")
    )
    rowcount = db.execute(stmt).rowcount
    db.commit()
    return rowcount == 1


def mark_adopted(db: Session, *, sandbox_id: str, now: datetime) -> None:
    stmt = (
        update(RuleSandbox)
        .where(RuleSandbox.sandbox_id == sandbox_id)
        .where(RuleSandbox.adopted_at.is_(None))
        .values(adopted_at=_naive_utc(now))
    )
    db.execute(stmt)
    db.commit()


def list_expired_sandboxes(
    db: Session, *, now: datetime, plan_version: str | None = None
) -> list[RuleSandbox]:
    stmt = select(RuleSandbox).where(RuleSandbox.expires_at <= _naive_utc(now))
    if plan_version is not None:
        stmt = stmt.where(RuleSandbox.plan_version == plan_version)
    return list(db.execute(stmt).scalars().all())


def delete_sandbox(db: Session, *, sandbox_id: str, now: datetime) -> bool:
    row = db.get(RuleSandbox, sandbox_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def get_publish_request(
    db: Session, request_id: str
) -> RulePublishRequest | None:
    return db.get(RulePublishRequest, request_id)


def get_publish_request_for_sandbox(
    db: Session, sandbox_id: str
) -> RulePublishRequest | None:
    stmt = select(RulePublishRequest).where(
        RulePublishRequest.sandbox_id == sandbox_id
    )
    return db.execute(stmt).scalar_one_or_none()


def insert_publish_request(
    db: Session, values: dict[str, Any]
) -> RulePublishRequest | None:
    stmt = sqlite_insert(RulePublishRequest).values(**values)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["sandbox_id"]
    ).returning(RulePublishRequest.request_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(RulePublishRequest, values["request_id"])


def decide_publish_request(
    db: Session,
    *,
    request_id: str,
    status: str,
    reviewed_by: str,
    review_reason: str,
    now: datetime,
) -> bool:
    """条件更新审批单状态，仅 pending 可流转，天然防重复审批。"""
    stmt = (
        update(RulePublishRequest)
        .where(RulePublishRequest.request_id == request_id)
        .where(RulePublishRequest.status == "pending")
        .values(
            status=status,
            reviewed_by=reviewed_by,
            review_reason=review_reason[:1000],
            reviewed_at=_naive_utc(now),
        )
    )
    rowcount = db.execute(stmt).rowcount
    db.commit()
    return rowcount == 1


def get_active_release(db: Session, plan_version: str) -> RuleRelease | None:
    stmt = (
        select(RuleRelease)
        .where(RuleRelease.plan_version == plan_version)
        .where(RuleRelease.status == "active")
        .order_by(RuleRelease.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def insert_release_and_retire_previous(
    db: Session,
    *,
    rule_version: str,
    plan_version: str,
    thresholds: dict[str, Any],
    source_request_id: str,
    published_by: str,
) -> RuleRelease:
    """发布新版本：旧生效版本转 retired，再写入唯一的 active 版本。"""
    db.execute(
        update(RuleRelease)
        .where(RuleRelease.plan_version == plan_version)
        .where(RuleRelease.status == "active")
        .values(status="retired")
    )
    db.execute(
        sqlite_insert(RuleRelease)
        .values(
            rule_version=rule_version,
            plan_version=plan_version,
            thresholds=thresholds,
            status="active",
            source_request_id=source_request_id,
            published_by=published_by,
        )
        .on_conflict_do_nothing(index_elements=["rule_version"])
    )
    db.commit()
    row = db.get(RuleRelease, rule_version)
    assert row is not None
    return row
