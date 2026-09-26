"""只读异常规则沙箱的应用服务。

职责边界：
- 创建时冻结事件快照与当时生效的生产规则版本，之后与生产数据更新隔离；
- 运行只在快照上计算候选异常，不创建正式案件、不写通知；
- 采纳仅生成发布审批单，审批通过后才发布新规则版本；
- 所有状态变更基于数据库条件更新，任务可重入、可在崩溃后重启接管。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from .compliance.anomaly_detection import (
    PRODUCTION_RULE_VERSION,
    RuleThresholds,
    compare_findings,
    detect_anomalies,
    validate_thresholds,
)
from .repository import get_plan, load_events, max_event_id
from . import sandbox_repository as repo

# 运行租约时长：执行者崩溃后，超过此时长的租约可被接管。
RUN_LEASE_SECONDS = 300
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
MAX_TTL_SECONDS = 90 * 24 * 3600


class SandboxError(ValueError):
    """沙箱业务约束错误。"""


class SandboxNotFoundError(LookupError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite 不保留时区，读取出来的 naive 时间按 UTC 解释。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _production_rule(
    db: Session, plan_version: str
) -> tuple[str, RuleThresholds]:
    release = repo.get_active_release(db, plan_version)
    if release is None:
        return PRODUCTION_RULE_VERSION, RuleThresholds.production_default()
    return release.rule_version, RuleThresholds.from_dict(release.thresholds)


def _serialize_event(event: Any) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type.value
        if hasattr(event.event_type, "value")
        else str(event.event_type),
        "student_id": event.student_id,
        "payload": dict(event.payload),
    }


def create_sandbox(
    db: Session,
    *,
    plan_version: str,
    draft_thresholds: dict[str, Any],
    created_by: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    sandbox_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    plan = get_plan(db, plan_version)
    if plan is None:
        raise SandboxNotFoundError(
            f"plan version '{plan_version}' is not registered"
        )
    actor = created_by.strip()
    if not actor:
        raise SandboxError("created_by 不能为空")

    draft = validate_thresholds(draft_thresholds)
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise SandboxError(
            f"ttl_seconds 必须为正数且不超过 {MAX_TTL_SECONDS}"
        )

    instant = (now or _utcnow()).astimezone(timezone.utc)
    events = [_serialize_event(e) for e in load_events(db, plan_version)]
    cutoff = max_event_id(db, plan_version)
    production_version, production = _production_rule(db, plan_version)

    sandbox_id = sandbox_id or f"SB-{uuid4().hex[:16]}"
    row = repo.insert_sandbox(
        db,
        values={
            "sandbox_id": sandbox_id,
            "plan_version": plan_version,
            "status": "ready",
            "created_by": actor,
            "event_snapshot": events,
            "event_cutoff_id": cutoff,
            "event_count": len(events),
            "production_rule_version": production_version,
            "production_thresholds": production.to_dict(),
            "draft_thresholds": draft.to_dict(),
            "expires_at": instant + timedelta(seconds=ttl_seconds),
        },
    )
    if row is None:
        raise SandboxError(f"sandbox '{sandbox_id}' 已存在")
    return serialize_sandbox(row)


def _get_required(db: Session, sandbox_id: str):
    row = repo.get_sandbox(db, sandbox_id)
    if row is None:
        raise SandboxNotFoundError(f"sandbox '{sandbox_id}' 不存在")
    return row


def _expire_if_due(db: Session, row, now: datetime) -> bool:
    """惰性过期：结果到期后标记 expired。返回是否已过期。"""
    if (
        row.status in ("ready", "running", "failed")
        and _aware(row.expires_at) <= now
    ):
        if repo.mark_expired(db, sandbox_id=row.sandbox_id, now=now):
            db.refresh(row)
        return True
    return row.status == "expired"


def run_sandbox(
    db: Session,
    *,
    sandbox_id: str,
    worker_id: str = "default-worker",
    now: datetime | None = None,
) -> dict[str, Any]:
    """运行（或重入）沙箱计算。

    - completed：直接返回既有结果，不重复计算；
    - running 且租约有效：抛出 SandboxBusyError，调用方可稍后重试；
    - running 且租约过期：视为前执行者崩溃，当前执行者接管重启；
    - failed：允许重新运行。
    """
    instant = (now or _utcnow()).astimezone(timezone.utc)
    row = _get_required(db, sandbox_id)
    if _expire_if_due(db, row, instant):
        raise SandboxExpiredError(f"sandbox '{sandbox_id}' 已过期")

    if row.status == "completed":
        return serialize_sandbox(row)

    owner = worker_id.strip() or "default-worker"
    lease_until = instant + timedelta(seconds=RUN_LEASE_SECONDS)
    acquired = repo.acquire_run_lease(
        db,
        sandbox_id=sandbox_id,
        owner=f"{owner}:{uuid4().hex[:8]}",
        now=instant,
        lease_expires_at=lease_until,
    )
    if not acquired:
        db.refresh(row)
        if row.status == "completed":
            # 并发情况下另一执行者已完成：直接返回既有结果（重入语义）。
            return serialize_sandbox(row)
        if row.status == "running":
            raise SandboxBusyError(
                f"sandbox '{sandbox_id}' 正在由 {row.run_lease_owner} 运行"
            )
        if _expire_if_due(db, row, instant):
            raise SandboxExpiredError(f"sandbox '{sandbox_id}' 已过期")
        raise SandboxBusyError(f"sandbox '{sandbox_id}' 当前不可运行")

    db.refresh(row)
    lease_owner = row.run_lease_owner
    try:
        production = RuleThresholds.from_dict(row.production_thresholds)
        draft = RuleThresholds.from_dict(row.draft_thresholds)
        production_findings = detect_anomalies(row.event_snapshot, production)
        draft_findings = detect_anomalies(row.event_snapshot, draft)
        comparison = compare_findings(
            production_findings=production_findings,
            draft_findings=draft_findings,
        )
    except Exception as exc:  # 检测失败保留现场，允许重试
        repo.fail_run(
            db,
            sandbox_id=sandbox_id,
            owner=lease_owner,
            now=instant,
            error=f"{type(exc).__name__}: {exc}",
        )
        db.refresh(row)
        raise SandboxError(f"沙箱运行失败: {exc}") from exc

    ok = repo.complete_run(
        db,
        sandbox_id=sandbox_id,
        owner=lease_owner,
        now=instant,
        production_findings=production_findings,
        draft_findings=draft_findings,
        comparison=comparison,
    )
    db.refresh(row)
    if not ok:
        # 运行期间被标记过期（极端时钟情况）：结果不应被采纳。
        if row.status == "expired":
            raise SandboxExpiredError(f"sandbox '{sandbox_id}' 已过期")
        raise SandboxError("沙箱结果提交失败，请重试")
    return serialize_sandbox(row)


def get_sandbox_state(
    db: Session, *, sandbox_id: str, now: datetime | None = None
) -> dict[str, Any]:
    instant = (now or _utcnow()).astimezone(timezone.utc)
    row = _get_required(db, sandbox_id)
    _expire_if_due(db, row, instant)
    db.refresh(row)
    return serialize_sandbox(row)


def get_comparison(
    db: Session, *, sandbox_id: str, now: datetime | None = None
) -> dict[str, Any]:
    instant = (now or _utcnow()).astimezone(timezone.utc)
    row = _get_required(db, sandbox_id)
    if _expire_if_due(db, row, instant):
        raise SandboxExpiredError(f"sandbox '{sandbox_id}' 已过期")
    db.refresh(row)
    if row.status != "completed" or row.comparison is None:
        raise SandboxNotRunError(
            f"sandbox '{sandbox_id}' 尚未完成运行（当前状态: {row.status}）"
        )
    return {
        "sandbox_id": row.sandbox_id,
        "plan_version": row.plan_version,
        "status": row.status,
        "expires_at": row.expires_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "production_rule_version": row.production_rule_version,
        "event_cutoff_id": row.event_cutoff_id,
        "event_count": row.event_count,
        "comparison": row.comparison,
    }


def adopt_sandbox(
    db: Session,
    *,
    sandbox_id: str,
    requested_by: str,
    request_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """采纳草案：只创建待审批发布单，不改变生产规则。"""
    instant = (now or _utcnow()).astimezone(timezone.utc)
    row = _get_required(db, sandbox_id)
    if _expire_if_due(db, row, instant):
        raise SandboxExpiredError(f"sandbox '{sandbox_id}' 已过期，不能采纳")
    db.refresh(row)
    actor = requested_by.strip()
    if not actor:
        raise SandboxError("requested_by 不能为空")
    if row.status != "completed":
        raise SandboxError(
            f"sandbox '{sandbox_id}' 未完成运行（当前状态: {row.status}），不能采纳"
        )

    existing = repo.get_publish_request_for_sandbox(db, sandbox_id)
    if existing is not None:
        return serialize_publish_request(existing)

    request_id = request_id or f"RR-{uuid4().hex[:16]}"
    created = repo.insert_publish_request(
        db,
        values={
            "request_id": request_id,
            "sandbox_id": sandbox_id,
            "plan_version": row.plan_version,
            "status": "pending",
            "proposed_thresholds": dict(row.draft_thresholds),
            "requested_by": actor,
        },
    )
    if created is None:
        existing = repo.get_publish_request_for_sandbox(db, sandbox_id)
        assert existing is not None
        return serialize_publish_request(existing)
    return serialize_publish_request(created)


def review_publish_request(
    db: Session,
    *,
    request_id: str,
    approve: bool,
    reviewed_by: str,
    review_reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """发布审批：只有 approve 才发布新版本并退役旧版本。"""
    instant = (now or _utcnow()).astimezone(timezone.utc)
    request = repo.get_publish_request(db, request_id)
    if request is None:
        raise SandboxNotFoundError(f"publish request '{request_id}' 不存在")
    actor = reviewed_by.strip()
    reason = review_reason.strip()
    if not actor or not reason:
        raise SandboxError("审批必须记录审批人和原因")
    if request.status != "pending":
        raise SandboxError(
            f"审批单已是 '{request.status}' 状态，不能重复审批"
        )

    new_status = "approved" if approve else "rejected"
    if approve:
        sandbox = repo.get_sandbox(db, request.sandbox_id)
        if sandbox is None or _aware(sandbox.expires_at) <= instant:
            # 草案依据已失效，不允许发布。
            repo.decide_publish_request(
                db,
                request_id=request_id,
                status="rejected",
                reviewed_by=actor,
                review_reason=f"沙箱已过期，自动驳回：{reason}",
                now=instant,
            )
            db.refresh(request)
            return serialize_publish_request(request)

        rule_version = f"published:{request_id}"
        repo.insert_release_and_retire_previous(
            db,
            rule_version=rule_version,
            plan_version=request.plan_version,
            thresholds=dict(request.proposed_thresholds),
            source_request_id=request_id,
            published_by=actor,
        )
        repo.mark_adopted(db, sandbox_id=request.sandbox_id, now=instant)

    repo.decide_publish_request(
        db,
        request_id=request_id,
        status=new_status,
        reviewed_by=actor,
        review_reason=reason,
        now=instant,
    )
    db.refresh(request)
    return serialize_publish_request(request)


def get_publish_request_state(
    db: Session, *, request_id: str
) -> dict[str, Any]:
    request = repo.get_publish_request(db, request_id)
    if request is None:
        raise SandboxNotFoundError(f"publish request '{request_id}' 不存在")
    return serialize_publish_request(request)


def cleanup_sandbox(
    db: Session, *, sandbox_id: str, now: datetime | None = None
) -> bool:
    """手动清理单个沙箱；存在进行中审批单时拒绝清理。"""
    instant = (now or _utcnow()).astimezone(timezone.utc)
    row = _get_required(db, sandbox_id)
    pending = repo.get_publish_request_for_sandbox(db, sandbox_id)
    if pending is not None and pending.status == "pending":
        raise SandboxError("沙箱存在待审批发布单，不能清理")
    if row.status == "running" and (
        lease := _aware(row.run_lease_expires_at)
    ) and lease > instant:
        raise SandboxError("沙箱正在运行，不能清理")
    return repo.delete_sandbox(db, sandbox_id=sandbox_id, now=instant)


def cleanup_expired(
    db: Session,
    *,
    plan_version: str | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """批量清理已到期沙箱；有待审批发布单的保留，避免审批悬空。"""
    instant = (now or _utcnow()).astimezone(timezone.utc)
    rows = repo.list_expired_sandboxes(db, now=instant, plan_version=plan_version)
    removed = 0
    skipped = 0
    for row in rows:
        pending = repo.get_publish_request_for_sandbox(db, row.sandbox_id)
        if pending is not None and pending.status == "pending":
            skipped += 1
            continue
        if repo.delete_sandbox(db, sandbox_id=row.sandbox_id, now=instant):
            removed += 1
    return {"removed": removed, "skipped_pending": skipped}


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def serialize_sandbox(row: Any) -> dict[str, Any]:
    return {
        "sandbox_id": row.sandbox_id,
        "plan_version": row.plan_version,
        "status": row.status,
        "created_by": row.created_by,
        "event_count": row.event_count,
        "event_cutoff_id": row.event_cutoff_id,
        "production_rule_version": row.production_rule_version,
        "production_thresholds": dict(row.production_thresholds),
        "draft_thresholds": dict(row.draft_thresholds),
        "production_findings": list(row.production_findings or []),
        "draft_findings": list(row.draft_findings or []),
        "comparison": dict(row.comparison) if row.comparison is not None else None,
        "attempts": row.attempts,
        "last_error": row.last_error,
        "expires_at": _dt(row.expires_at),
        "ran_at": _dt(row.ran_at),
        "adopted_at": _dt(row.adopted_at),
        "created_at": _dt(row.created_at),
    }


def serialize_publish_request(row: Any) -> dict[str, Any]:
    return {
        "request_id": row.request_id,
        "sandbox_id": row.sandbox_id,
        "plan_version": row.plan_version,
        "status": row.status,
        "proposed_thresholds": dict(row.proposed_thresholds),
        "requested_by": row.requested_by,
        "reviewed_by": row.reviewed_by,
        "review_reason": row.review_reason,
        "created_at": _dt(row.created_at),
        "reviewed_at": _dt(row.reviewed_at),
    }


class SandboxBusyError(SandboxError):
    pass


class SandboxExpiredError(SandboxError):
    pass


class SandboxNotRunError(SandboxError):
    pass
