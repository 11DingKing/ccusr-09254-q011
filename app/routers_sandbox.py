"""沙箱 API：创建、运行、比较、采纳与清理。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from . import services, services_sandbox
from .db import get_db
from .schemas import (
    AdoptionCreateIn,
    AdoptionDecisionIn,
    AdoptionOut,
    CleanupOut,
    ProductionRulesOut,
    RunCreateIn,
    RunOut,
    SandboxCreateIn,
    SandboxDiffOut,
    SandboxOut,
)

router = APIRouter(prefix="/api")

_NOT_FOUND = {
    services_sandbox.SandboxNotFoundError,
    services_sandbox.RunNotFoundError,
    services_sandbox.AdoptionNotFoundError,
    services.PlanNotFoundError,
}
_CONFLICT = {
    services_sandbox.SandboxConflictError,
    services_sandbox.RunConflictError,
    services_sandbox.RunNotSucceededError,
    services_sandbox.AdoptionStateError,
}


def _translate(exc: Exception) -> HTTPException:
    if type(exc) in _NOT_FOUND:
        return HTTPException(status_code=404, detail=str(exc))
    if type(exc) is services_sandbox.SandboxExpiredError:
        return HTTPException(status_code=410, detail=str(exc))
    if type(exc) in _CONFLICT:
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/plans/{plan_version}/sandboxes",
    response_model=SandboxOut,
    status_code=status.HTTP_201_CREATED,
)
def create_sandbox(
    plan_version: str,
    body: SandboxCreateIn,
    response: Response,
    db: Session = Depends(get_db),
) -> Any:
    """创建只读沙箱：固定当前事件集合与规则草案，结果有期限。"""
    try:
        view, created = services_sandbox.create_sandbox(
            db,
            plan_version=plan_version,
            sandbox_id=body.sandbox_id,
            draft_rules=body.draft_rules.model_dump(),
            ttl_seconds=body.ttl_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - 统一翻译领域错误
        raise _translate(exc) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return view


@router.get("/plans/{plan_version}/sandboxes", response_model=list[SandboxOut])
def list_sandboxes(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services_sandbox.list_sandboxes(db, plan_version)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.get(
    "/plans/{plan_version}/rules/production",
    response_model=ProductionRulesOut,
)
def get_production_rules(plan_version: str, db: Session = Depends(get_db)) -> Any:
    """查看当前生产规则（内置版本或最近批准的采纳申请）。"""
    try:
        return services_sandbox.get_production_rules_view(db, plan_version)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.get("/sandboxes/{sandbox_id}", response_model=SandboxOut)
def get_sandbox(sandbox_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services_sandbox.get_sandbox_view(db, sandbox_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.delete(
    "/sandboxes/{sandbox_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_sandbox(sandbox_id: str, db: Session = Depends(get_db)) -> Response:
    try:
        services_sandbox.delete_sandbox(db, sandbox_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/sandboxes/{sandbox_id}/runs",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
)
def start_run(
    sandbox_id: str,
    body: RunCreateIn,
    response: Response,
    db: Session = Depends(get_db),
) -> Any:
    """启动或重入一次运行；崩溃或失败的运行会被接管重算。"""
    try:
        view = services_sandbox.start_run(
            db,
            sandbox_id=sandbox_id,
            run_id=body.run_id,
            lease_seconds=body.lease_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    if view["reused"]:
        response.status_code = status.HTTP_200_OK
    return view


@router.get("/sandboxes/{sandbox_id}/runs/{run_id}", response_model=RunOut)
def get_run(sandbox_id: str, run_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services_sandbox.get_run_view(db, sandbox_id, run_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.get("/sandboxes/{sandbox_id}/diff", response_model=SandboxDiffOut)
def get_diff(sandbox_id: str, db: Session = Depends(get_db)) -> Any:
    """比较：最近一次成功运行中草案与生产规则的候选异常差异。"""
    try:
        return services_sandbox.get_diff(db, sandbox_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.post(
    "/sandboxes/{sandbox_id}/adoptions",
    response_model=AdoptionOut,
    status_code=status.HTTP_201_CREATED,
)
def adopt_sandbox(
    sandbox_id: str,
    body: AdoptionCreateIn,
    response: Response,
    db: Session = Depends(get_db),
) -> Any:
    """采纳草案：登记待审批申请，不改动生产规则或正式案件。"""
    try:
        view, created = services_sandbox.adopt_sandbox(
            db,
            sandbox_id=sandbox_id,
            requested_by=body.requested_by,
            rule_version=body.rule_version,
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return view


@router.get("/sandboxes/{sandbox_id}/adoptions", response_model=AdoptionOut)
def get_adoption(sandbox_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services_sandbox.get_adoption_view(db, sandbox_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.post(
    "/sandboxes/{sandbox_id}/adoptions/decision",
    response_model=AdoptionOut,
)
def decide_adoption(
    sandbox_id: str,
    body: AdoptionDecisionIn,
    db: Session = Depends(get_db),
) -> Any:
    """发布审批：批准后草案成为当前生产规则，拒绝则关闭申请。"""
    try:
        return services_sandbox.decide_adoption(
            db,
            sandbox_id=sandbox_id,
            decision=body.decision,
            decided_by=body.decided_by,
            reason=body.reason,
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.post("/sandboxes/cleanup", response_model=CleanupOut)
def cleanup_sandboxes(db: Session = Depends(get_db)) -> Any:
    """清理所有到期沙箱及其运行、事件副本与采纳申请。"""
    return services_sandbox.cleanup_expired(db)
