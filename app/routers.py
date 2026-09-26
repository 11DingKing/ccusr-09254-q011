"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from . import sandbox_service, services
from .db import get_db
from .schemas import (
    CleanupResultOut,
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    PublishRequestOut,
    PublishReviewIn,
    SandboxAdoptIn,
    SandboxComparisonOut,
    SandboxCreateIn,
    SandboxOut,
    SandboxRunIn,
    SnapshotOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 只读异常规则沙箱
# ---------------------------------------------------------------------------


def _sandbox_error_status(exc: Exception) -> int:
    if isinstance(exc, sandbox_service.SandboxNotFoundError):
        return 404
    if isinstance(exc, sandbox_service.SandboxExpiredError):
        return 410
    if isinstance(
        exc, (sandbox_service.SandboxBusyError, sandbox_service.SandboxNotRunError)
    ):
        return 409
    return 400


@router.post(
    "/plans/{plan_version}/rule-sandboxes",
    response_model=SandboxOut,
    status_code=status.HTTP_201_CREATED,
)
def create_rule_sandbox(
    plan_version: str, body: SandboxCreateIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return sandbox_service.create_sandbox(
            db,
            plan_version=plan_version,
            draft_thresholds=body.draft_thresholds.model_dump(),
            created_by=body.created_by,
            ttl_seconds=body.ttl_seconds
            if body.ttl_seconds is not None
            else sandbox_service.DEFAULT_TTL_SECONDS,
            sandbox_id=body.sandbox_id,
        )
    except (sandbox_service.SandboxError, sandbox_service.SandboxNotFoundError) as exc:
        raise HTTPException(
            status_code=_sandbox_error_status(exc), detail=str(exc)
        ) from exc


@router.post(
    "/rule-sandboxes/{sandbox_id}/run",
    response_model=SandboxOut,
)
def run_rule_sandbox(
    sandbox_id: str, body: SandboxRunIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return sandbox_service.run_sandbox(
            db, sandbox_id=sandbox_id, worker_id=body.worker_id
        )
    except (sandbox_service.SandboxError, sandbox_service.SandboxNotFoundError) as exc:
        raise HTTPException(
            status_code=_sandbox_error_status(exc), detail=str(exc)
        ) from exc


@router.get("/rule-sandboxes/{sandbox_id}", response_model=SandboxOut)
def read_rule_sandbox(sandbox_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return sandbox_service.get_sandbox_state(db, sandbox_id=sandbox_id)
    except (sandbox_service.SandboxError, sandbox_service.SandboxNotFoundError) as exc:
        raise HTTPException(
            status_code=_sandbox_error_status(exc), detail=str(exc)
        ) from exc


@router.get(
    "/rule-sandboxes/{sandbox_id}/comparison",
    response_model=SandboxComparisonOut,
)
def read_rule_sandbox_comparison(
    sandbox_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return sandbox_service.get_comparison(db, sandbox_id=sandbox_id)
    except (sandbox_service.SandboxError, sandbox_service.SandboxNotFoundError) as exc:
        raise HTTPException(
            status_code=_sandbox_error_status(exc), detail=str(exc)
        ) from exc


@router.post(
    "/rule-sandboxes/{sandbox_id}/adopt",
    response_model=PublishRequestOut,
    status_code=status.HTTP_201_CREATED,
)
def adopt_rule_sandbox(
    sandbox_id: str, body: SandboxAdoptIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return sandbox_service.adopt_sandbox(
            db,
            sandbox_id=sandbox_id,
            requested_by=body.requested_by,
            request_id=body.request_id,
        )
    except (sandbox_service.SandboxError, sandbox_service.SandboxNotFoundError) as exc:
        raise HTTPException(
            status_code=_sandbox_error_status(exc), detail=str(exc)
        ) from exc


@router.delete("/rule-sandboxes/{sandbox_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule_sandbox(sandbox_id: str, db: Session = Depends(get_db)) -> Response:
    try:
        removed = sandbox_service.cleanup_sandbox(db, sandbox_id=sandbox_id)
    except (sandbox_service.SandboxError, sandbox_service.SandboxNotFoundError) as exc:
        raise HTTPException(
            status_code=_sandbox_error_status(exc), detail=str(exc)
        ) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="sandbox not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/rule-sandboxes/cleanup/expired",
    response_model=CleanupResultOut,
)
def cleanup_expired_rule_sandboxes(
    plan_version: str | None = None, db: Session = Depends(get_db)
) -> Any:
    return sandbox_service.cleanup_expired(db, plan_version=plan_version)


@router.get(
    "/rule-publish-requests/{request_id}",
    response_model=PublishRequestOut,
)
def read_publish_request(request_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return sandbox_service.get_publish_request_state(db, request_id=request_id)
    except sandbox_service.SandboxNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/rule-publish-requests/{request_id}/review",
    response_model=PublishRequestOut,
)
def review_publish_request(
    request_id: str, body: PublishReviewIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return sandbox_service.review_publish_request(
            db,
            request_id=request_id,
            approve=body.approve,
            reviewed_by=body.reviewed_by,
            review_reason=body.review_reason,
        )
    except (sandbox_service.SandboxError, sandbox_service.SandboxNotFoundError) as exc:
        raise HTTPException(
            status_code=_sandbox_error_status(exc), detail=str(exc)
        ) from exc
