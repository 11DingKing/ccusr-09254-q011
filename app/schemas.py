"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


class DraftRulesIn(BaseModel):
    """规则草案：三项异常阈值（秒），判定条件为严格大于。"""

    overlong_checkin_seconds: int = Field(..., ge=0)
    overlap_seconds: int = Field(..., ge=0)
    negative_correction_seconds: int = Field(..., ge=0)


class SandboxCreateIn(BaseModel):
    sandbox_id: str = Field(..., min_length=1, max_length=128)
    draft_rules: DraftRulesIn
    ttl_seconds: int | None = Field(None, gt=0)


class SandboxOut(BaseModel):
    sandbox_id: str
    plan_version: str
    draft_rules: dict[str, Any]
    event_cutoff_id: str | None
    event_count: int
    created_at: datetime
    expires_at: datetime
    expired: bool
    latest_run_id: str | None
    latest_run_status: str | None


class RunCreateIn(BaseModel):
    run_id: str | None = Field(None, min_length=1, max_length=128)
    lease_seconds: int | None = Field(None, gt=0)


class RunOut(BaseModel):
    run_id: str
    sandbox_id: str
    plan_version: str
    status: str
    attempt: int
    reused: bool
    production_rule_version: str
    draft_rule_version: str
    production_candidate_count: int
    draft_candidate_count: int
    production_candidates: list[dict[str, Any]]
    draft_candidates: list[dict[str, Any]]
    diff: dict[str, Any] | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class SandboxDiffOut(BaseModel):
    sandbox_id: str
    plan_version: str
    run_id: str
    production_rule_version: str
    draft_rule_version: str
    event_cutoff_id: str | None
    generated_at: datetime | None
    diff: dict[str, Any]


class AdoptionCreateIn(BaseModel):
    requested_by: str = Field(..., min_length=1, max_length=128)
    rule_version: str | None = Field(None, min_length=1, max_length=128)


class AdoptionDecisionIn(BaseModel):
    decision: Literal["approved", "rejected"]
    decided_by: str = Field(..., min_length=1, max_length=128)
    reason: str = ""


class AdoptionOut(BaseModel):
    adoption_id: str
    sandbox_id: str
    run_id: str
    plan_version: str
    status: str
    requested_by: str
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None
    rule_version: str
    rule_set: dict[str, Any]
    created_at: datetime


class ProductionRulesOut(BaseModel):
    plan_version: str
    source: str
    adoption_id: str | None
    rules: dict[str, Any]


class CleanupOut(BaseModel):
    removed_sandbox_ids: list[str]
    removed_count: int
