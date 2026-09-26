"""沙箱编排：创建、运行、比较、采纳与清理。

沙箱是只读的——所有计算只读取沙箱自有的事件副本，只写沙箱自有表；
不会触发正式通知，也不会创建或修改正式异常案件。采纳只登记审批
申请，生产规则的变更必须经过发布审批（decide）后才会生效。
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from . import repository_sandbox as repo
from .compliance.anomaly_rules import (
    BUILTIN_PRODUCTION_RULES,
    DRAFT_VERSION_LABEL,
    RuleSet,
    detect_candidates,
    diff_candidates,
    validate_draft_thresholds,
)
from .core.replay import replay
from .repository import get_plan, max_event_id
from .services import PlanNotFoundError

DEFAULT_TTL_SECONDS = 7 * 24 * 3600
MAX_TTL_SECONDS = 90 * 24 * 3600
DEFAULT_LEASE_SECONDS = 300
MAX_LEASE_SECONDS = 3600
_RUN_WAIT_TIMEOUT_SECONDS = 10.0
_RUN_WAIT_INTERVAL_SECONDS = 0.05


class SandboxNotFoundError(Exception):
    pass


class SandboxExpiredError(Exception):
    pass


class SandboxConflictError(Exception):
    pass


class RunConflictError(Exception):
    pass


class RunNotFoundError(Exception):
    pass


class RunNotSucceededError(Exception):
    pass


class AdoptionNotFoundError(Exception):
    pass


class AdoptionStateError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_sandbox(db: Session, sandbox_id: str):
    sandbox = repo.get_sandbox(db, sandbox_id)
    if sandbox is None:
        raise SandboxNotFoundError(f"sandbox '{sandbox_id}' does not exist")
    return sandbox


def _ensure_not_expired(sandbox, now: datetime) -> None:
    if _as_utc(sandbox.expires_at) <= now:
        raise SandboxExpiredError(
            f"sandbox '{sandbox.sandbox_id}' expired at "
            f"{_as_utc(sandbox.expires_at).isoformat()}"
        )


def resolve_production_rules(db: Session, plan_version: str) -> RuleSet:
    """当前生产规则：最近一次已批准的采纳申请，否则为内置版本。"""
    adoption = repo.latest_approved_adoption(db, plan_version)
    if adoption is not None:
        return RuleSet.from_dict(adoption.rule_set)
    return BUILTIN_PRODUCTION_RULES


def _sandbox_view(db: Session, sandbox, now: datetime) -> dict[str, Any]:
    latest = repo.latest_run(db, sandbox.sandbox_id)
    return {
        "sandbox_id": sandbox.sandbox_id,
        "plan_version": sandbox.plan_version,
        "draft_rules": dict(sandbox.draft_rules),
        "event_cutoff_id": sandbox.event_cutoff_id,
        "event_count": repo.count_sandbox_events(db, sandbox.sandbox_id),
        "created_at": _as_utc(sandbox.created_at),
        "expires_at": _as_utc(sandbox.expires_at),
        "expired": _as_utc(sandbox.expires_at) <= now,
        "latest_run_id": latest.run_id if latest is not None else None,
        "latest_run_status": latest.status if latest is not None else None,
    }


def create_sandbox(
    db: Session,
    *,
    plan_version: str,
    sandbox_id: str,
    draft_rules: dict[str, Any],
    ttl_seconds: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """创建沙箱：固定当前事件集合与规则草案。同名重入返回原沙箱。"""
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    thresholds = validate_draft_thresholds(draft_rules)
    ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    if ttl <= 0 or ttl > MAX_TTL_SECONDS:
        raise ValueError(f"ttl_seconds 必须在 1 到 {MAX_TTL_SECONDS} 之间")

    now = _utcnow()
    cutoff = max_event_id(db, plan_version)
    sandbox, created = repo.create_sandbox_with_events(
        db,
        sandbox_id=sandbox_id,
        plan_version=plan_version,
        draft_rules=thresholds,
        event_cutoff_id=cutoff,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl),
    )
    assert sandbox is not None
    if not created:
        if sandbox.plan_version != plan_version or dict(sandbox.draft_rules) != thresholds:
            raise SandboxConflictError(
                f"sandbox '{sandbox_id}' already exists with different parameters"
            )
    return _sandbox_view(db, sandbox, _utcnow()), created


def list_sandboxes(db: Session, plan_version: str) -> list[dict[str, Any]]:
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    now = _utcnow()
    return [_sandbox_view(db, s, now) for s in repo.list_sandboxes(db, plan_version)]


def get_sandbox_view(db: Session, sandbox_id: str) -> dict[str, Any]:
    sandbox = _require_sandbox(db, sandbox_id)
    return _sandbox_view(db, sandbox, _utcnow())


def _run_view(run, *, reused: bool) -> dict[str, Any]:
    production_candidates = run.production_candidates or []
    draft_candidates = run.draft_candidates or []
    return {
        "run_id": run.run_id,
        "sandbox_id": run.sandbox_id,
        "plan_version": run.plan_version,
        "status": run.status,
        "attempt": run.attempt,
        "reused": reused,
        "production_rule_version": run.production_rule_version,
        "draft_rule_version": DRAFT_VERSION_LABEL,
        "production_candidate_count": len(production_candidates),
        "draft_candidate_count": len(draft_candidates),
        "production_candidates": production_candidates,
        "draft_candidates": draft_candidates,
        "diff": run.diff,
        "error": run.error,
        "created_at": _as_utc(run.created_at),
        "finished_at": _as_utc(run.finished_at) if run.finished_at else None,
    }


def _wait_for_run(db: Session, run_id: str):
    """并发运行时短暂等待在途工作者完成，随后返回最新行。

    每轮先 rollback 放弃旧读快照，否则 SQLite 事务内看不到其他
    连接的新提交，会永远等不到状态变化。
    """
    deadline = time.monotonic() + _RUN_WAIT_TIMEOUT_SECONDS
    while True:
        db.rollback()
        run = repo.get_run(db, run_id, fresh=True)
        if run is None or run.status != "running":
            return run
        if time.monotonic() >= deadline:
            return run
        time.sleep(_RUN_WAIT_INTERVAL_SECONDS)


def _compute_run(db: Session, sandbox, production: RuleSet) -> dict[str, Any]:
    """在沙箱固定的事件副本上分别应用生产规则与草案并求差（纯读取）。"""
    plan = get_plan(db, sandbox.plan_version)
    assert plan is not None
    events = repo.load_sandbox_events(db, sandbox.sandbox_id)
    state = replay(
        events,
        plan_version=sandbox.plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )
    draft = RuleSet(version=DRAFT_VERSION_LABEL, **dict(sandbox.draft_rules))
    production_candidates = detect_candidates(state, production)
    draft_candidates = detect_candidates(state, draft)
    return {
        "production_candidates": production_candidates,
        "draft_candidates": draft_candidates,
        "diff": diff_candidates(production_candidates, draft_candidates),
    }


def start_run(
    db: Session,
    *,
    sandbox_id: str,
    run_id: str | None = None,
    lease_seconds: int | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """启动或重入一次运行。

    - 已成功的同一 run_id 直接返回原结果（重入幂等）；
    - 运行中且租约有效的由其他工作者持有，等待其完成；
    - 失败或租约过期（工作者崩溃）时被接管重算，attempt 自增。
    """
    sandbox = _require_sandbox(db, sandbox_id)
    now = _utcnow()
    _ensure_not_expired(sandbox, now)

    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    owner = owner or f"worker-{uuid.uuid4().hex[:8]}"
    lease = DEFAULT_LEASE_SECONDS if lease_seconds is None else int(lease_seconds)
    if lease <= 0 or lease > MAX_LEASE_SECONDS:
        raise ValueError(f"lease_seconds 必须在 1 到 {MAX_LEASE_SECONDS} 之间")
    lease_expires = now + timedelta(seconds=lease)

    production = resolve_production_rules(db, sandbox.plan_version)
    run, created = repo.insert_run(
        db,
        run_id=run_id,
        sandbox_id=sandbox_id,
        plan_version=sandbox.plan_version,
        production_rule_version=production.version,
        production_rules=production.to_dict(),
        draft_rules=dict(sandbox.draft_rules),
        lease_owner=owner,
        lease_expires_at=lease_expires,
    )
    assert run is not None

    if not created:
        if run.sandbox_id != sandbox_id:
            raise RunConflictError(
                f"run '{run_id}' belongs to sandbox '{run.sandbox_id}'"
            )
        if run.status == "succeeded":
            return _run_view(run, reused=True)
        if run.status == "running" and _as_utc(run.lease_expires_at) > now:
            waited = _wait_for_run(db, run_id)
            if waited is not None and waited.status == "succeeded":
                return _run_view(waited, reused=True)
        db.rollback()  # 让接管 UPDATE 基于最新快照
        reclaimed = repo.reclaim_run(
            db,
            run_id=run_id,
            lease_owner=owner,
            now=_utcnow(),
            lease_expires_at=_utcnow() + timedelta(seconds=lease),
        )
        if reclaimed is None:
            waited = _wait_for_run(db, run_id)
            if waited is not None and waited.status == "succeeded":
                return _run_view(waited, reused=True)
            raise RunConflictError(f"run '{run_id}' is held by another worker")
        run = reclaimed
        # 接管者沿用运行创建时固定的生产规则，保持记录自洽。
        production = RuleSet.from_dict(run.production_rules)

    try:
        result = _compute_run(db, sandbox, production)
    except Exception as exc:
        repo.complete_run(
            db,
            run_id=run_id,
            lease_owner=owner,
            status="failed",
            error=str(exc)[:1000],
            finished_at=_utcnow(),
        )
        raise

    finished = repo.complete_run(
        db,
        run_id=run_id,
        lease_owner=owner,
        status="succeeded",
        production_candidates=result["production_candidates"],
        draft_candidates=result["draft_candidates"],
        diff=result["diff"],
        finished_at=_utcnow(),
    )
    if finished is None:
        raise RunConflictError(f"run '{run_id}' lease was lost during execution")
    return _run_view(finished, reused=False)


def get_run_view(db: Session, sandbox_id: str, run_id: str) -> dict[str, Any]:
    _require_sandbox(db, sandbox_id)
    run = repo.get_run(db, run_id)
    if run is None or run.sandbox_id != sandbox_id:
        raise RunNotFoundError(
            f"run '{run_id}' for sandbox '{sandbox_id}' does not exist"
        )
    return _run_view(run, reused=False)


def get_diff(db: Session, sandbox_id: str) -> dict[str, Any]:
    """比较视图：最近一次成功运行中草案与生产规则的候选差异。"""
    sandbox = _require_sandbox(db, sandbox_id)
    _ensure_not_expired(sandbox, _utcnow())
    run = repo.latest_succeeded_run(db, sandbox_id)
    if run is None:
        raise RunNotSucceededError(
            f"sandbox '{sandbox_id}' has no succeeded run yet"
        )
    return {
        "sandbox_id": sandbox_id,
        "plan_version": run.plan_version,
        "run_id": run.run_id,
        "production_rule_version": run.production_rule_version,
        "draft_rule_version": DRAFT_VERSION_LABEL,
        "event_cutoff_id": sandbox.event_cutoff_id,
        "generated_at": _as_utc(run.finished_at) if run.finished_at else None,
        "diff": run.diff,
    }


def adopt_sandbox(
    db: Session,
    *,
    sandbox_id: str,
    requested_by: str,
    rule_version: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """采纳沙箱草案：登记待审批申请，不改动生产规则或正式案件。"""
    sandbox = _require_sandbox(db, sandbox_id)
    _ensure_not_expired(sandbox, _utcnow())
    run = repo.latest_succeeded_run(db, sandbox_id)
    if run is None:
        raise RunNotSucceededError(
            f"sandbox '{sandbox_id}' has no succeeded run yet"
        )
    requested_by = requested_by.strip()
    if not requested_by:
        raise ValueError("requested_by 不能为空")
    version = (rule_version or f"sandbox-{sandbox_id}-v1").strip()
    if not version:
        raise ValueError("rule_version 不能为空")

    rule_set = RuleSet(version=version, **dict(sandbox.draft_rules)).to_dict()
    adoption, created = repo.insert_adoption(
        db,
        adoption_id=f"adopt-{sandbox_id}",
        sandbox_id=sandbox_id,
        run_id=run.run_id,
        plan_version=sandbox.plan_version,
        requested_by=requested_by,
        rule_version=version,
        rule_set=rule_set,
    )
    assert adoption is not None
    return _adoption_view(adoption), created


def _adoption_view(adoption) -> dict[str, Any]:
    return {
        "adoption_id": adoption.adoption_id,
        "sandbox_id": adoption.sandbox_id,
        "run_id": adoption.run_id,
        "plan_version": adoption.plan_version,
        "status": adoption.status,
        "requested_by": adoption.requested_by,
        "decided_by": adoption.decided_by,
        "decided_at": _as_utc(adoption.decided_at) if adoption.decided_at else None,
        "decision_reason": adoption.decision_reason,
        "rule_version": adoption.rule_version,
        "rule_set": dict(adoption.rule_set),
        "created_at": _as_utc(adoption.created_at),
    }


def get_adoption_view(db: Session, sandbox_id: str) -> dict[str, Any]:
    _require_sandbox(db, sandbox_id)
    adoption = repo.get_adoption_for_sandbox(db, sandbox_id)
    if adoption is None:
        raise AdoptionNotFoundError(
            f"sandbox '{sandbox_id}' has no adoption request"
        )
    return _adoption_view(adoption)


def decide_adoption(
    db: Session,
    *,
    sandbox_id: str,
    decision: str,
    decided_by: str,
    reason: str = "",
) -> dict[str, Any]:
    """发布审批：批准后该草案成为当前生产规则，拒绝则关闭申请。"""
    _require_sandbox(db, sandbox_id)
    adoption = repo.get_adoption_for_sandbox(db, sandbox_id)
    if adoption is None:
        raise AdoptionNotFoundError(
            f"sandbox '{sandbox_id}' has no adoption request"
        )
    decided_by = decided_by.strip()
    if not decided_by:
        raise ValueError("decided_by 不能为空")
    updated = repo.decide_adoption(
        db,
        adoption_id=adoption.adoption_id,
        decision=decision,
        decided_by=decided_by,
        decided_at=_utcnow(),
        reason=reason.strip(),
    )
    if updated is None:
        raise AdoptionStateError(
            f"adoption '{adoption.adoption_id}' was already decided"
        )
    return _adoption_view(updated)


def get_production_rules_view(db: Session, plan_version: str) -> dict[str, Any]:
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    adoption = repo.latest_approved_adoption(db, plan_version)
    rules = resolve_production_rules(db, plan_version)
    return {
        "plan_version": plan_version,
        "source": "adoption" if adoption is not None else "builtin",
        "adoption_id": adoption.adoption_id if adoption is not None else None,
        "rules": rules.to_dict(),
    }


def cleanup_expired(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """清理到期沙箱及其运行、事件副本与采纳申请。"""
    moment = now or _utcnow()
    removed = repo.list_expired_sandbox_ids(db, moment)
    for sandbox_id in removed:
        repo.delete_sandbox(db, sandbox_id)
    return {"removed_sandbox_ids": removed, "removed_count": len(removed)}


def delete_sandbox(db: Session, sandbox_id: str) -> None:
    if not repo.delete_sandbox(db, sandbox_id):
        raise SandboxNotFoundError(f"sandbox '{sandbox_id}' does not exist")
