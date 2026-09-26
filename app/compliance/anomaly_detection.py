"""异常规则与候选异常的纯函数计算。

沙箱只读使用本模块：输入为固定的事件快照与规则阈值，输出候选异常列表，
不落正式案件、不触发通知。所有函数均为确定性纯函数，便于重入与重算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

# 当前生产规则的内置版本号；RuleRelease 表存在生效版本时以表中为准。
PRODUCTION_RULE_VERSION = "builtin:production-v1"

RULE_TYPE_OVERLONG = "overlong_checkin"
RULE_TYPE_OVERLAP = "overlapping_activity"
RULE_TYPE_NEGATIVE = "negative_correction"


@dataclass(frozen=True)
class RuleThresholds:
    """异常规则阈值（秒）。"""

    overlong_seconds: int
    overlap_min_seconds: int
    negative_correction_seconds: int

    @classmethod
    def production_default(cls) -> "RuleThresholds":
        return cls(
            overlong_seconds=12 * 3600,
            overlap_min_seconds=60,
            negative_correction_seconds=3600,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "overlong_seconds": self.overlong_seconds,
            "overlap_min_seconds": self.overlap_min_seconds,
            "negative_correction_seconds": self.negative_correction_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleThresholds":
        return cls(
            overlong_seconds=int(data["overlong_seconds"]),
            overlap_min_seconds=int(data["overlap_min_seconds"]),
            negative_correction_seconds=int(data["negative_correction_seconds"]),
        )


def validate_thresholds(values: dict[str, Any]) -> RuleThresholds:
    """校验规则草案，非法阈值抛 ValueError。"""
    rule = RuleThresholds(
        overlong_seconds=int(values["overlong_seconds"]),
        overlap_min_seconds=int(values["overlap_min_seconds"]),
        negative_correction_seconds=int(values["negative_correction_seconds"]),
    )
    for name, value in (
        ("overlong_seconds", rule.overlong_seconds),
        ("overlap_min_seconds", rule.overlap_min_seconds),
        ("negative_correction_seconds", rule.negative_correction_seconds),
    ):
        if value < 0:
            raise ValueError(f"{name} 必须为非负整数")
    return rule


def _parse_interval(payload: dict[str, Any]) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(str(payload["check_in_at"]))
    end = datetime.fromisoformat(str(payload["check_out_at"]))
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("签到时间必须包含时区")
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if end <= start:
        raise ValueError("签到结束时间必须晚于开始时间")
    return start, end


def _finding(
    *,
    key: str,
    rule_type: str,
    student_id: str,
    event_ids: Sequence[str],
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "key": key,
        "rule_type": rule_type,
        "student_id": student_id,
        "event_ids": list(event_ids),
        "details": details,
    }


def detect_anomalies(
    events: Iterable[dict[str, Any]], thresholds: RuleThresholds
) -> list[dict[str, Any]]:
    """对一组事件快照执行候选异常检测，返回按 anomaly_key 排序的去重列表。"""
    findings: list[dict[str, Any]] = []
    checkins_by_student: dict[str, list[tuple[str, datetime, datetime]]] = {}

    for event in events:
        event_type = event["event_type"]
        student_id = event["student_id"]
        event_id = event["event_id"]
        payload = event.get("payload") or {}

        if event_type == "checkin":
            start, end = _parse_interval(payload)
            checkins_by_student.setdefault(student_id, []).append(
                (event_id, start, end)
            )
            raw_seconds = int((end - start).total_seconds())
            if raw_seconds > thresholds.overlong_seconds:
                findings.append(
                    _finding(
                        key=f"overlong:{event_id}",
                        rule_type=RULE_TYPE_OVERLONG,
                        student_id=student_id,
                        event_ids=[event_id],
                        details={
                            "seconds": raw_seconds,
                            "threshold_seconds": thresholds.overlong_seconds,
                        },
                    )
                )
        elif event_type == "leave_correction":
            adjustment = int(payload.get("adjustment_seconds", 0))
            if (
                adjustment < 0
                and -adjustment >= thresholds.negative_correction_seconds
            ):
                findings.append(
                    _finding(
                        key=f"negative:{event_id}",
                        rule_type=RULE_TYPE_NEGATIVE,
                        student_id=student_id,
                        event_ids=[event_id],
                        details={
                            "adjustment_seconds": adjustment,
                            "threshold_seconds": thresholds.negative_correction_seconds,
                        },
                    )
                )

    for student_id, records in checkins_by_student.items():
        ordered = sorted(records, key=lambda item: (item[1], item[0]))
        for i in range(len(ordered)):
            id_a, start_a, end_a = ordered[i]
            for j in range(i + 1, len(ordered)):
                id_b, start_b, end_b = ordered[j]
                overlap_start = max(start_a, start_b)
                overlap_end = min(end_a, end_b)
                overlap_seconds = int(
                    (overlap_end - overlap_start).total_seconds()
                ) if overlap_end > overlap_start else 0
                if overlap_seconds >= thresholds.overlap_min_seconds:
                    first_id, second_id = sorted((id_a, id_b))
                    findings.append(
                        _finding(
                            key=f"overlap:{first_id}|{second_id}",
                            rule_type=RULE_TYPE_OVERLAP,
                            student_id=student_id,
                            event_ids=[first_id, second_id],
                            details={
                                "overlap_seconds": overlap_seconds,
                                "threshold_seconds": thresholds.overlap_min_seconds,
                            },
                        )
                    )

    findings.sort(key=lambda item: item["key"])
    return findings


def _student_changes(
    production: Sequence[dict[str, Any]],
    draft: Sequence[dict[str, Any]],
    added_keys: set[str],
    removed_keys: set[str],
) -> list[dict[str, Any]]:
    students = {f["student_id"] for f in production} | {
        f["student_id"] for f in draft
    }
    prod_by_student: dict[str, int] = {}
    draft_by_student: dict[str, int] = {}
    for f in production:
        prod_by_student[f["student_id"]] = prod_by_student.get(f["student_id"], 0) + 1
    for f in draft:
        draft_by_student[f["student_id"]] = draft_by_student.get(f["student_id"], 0) + 1

    changes = []
    draft_by_key = {f["key"]: f for f in draft}
    prod_by_key = {f["key"]: f for f in production}
    for student_id in sorted(students):
        added = [
            draft_by_key[key]["rule_type"]
            for key in added_keys
            if draft_by_key[key]["student_id"] == student_id
        ]
        removed = [
            prod_by_key[key]["rule_type"]
            for key in removed_keys
            if prod_by_key[key]["student_id"] == student_id
        ]
        changes.append(
            {
                "student_id": student_id,
                "production_count": prod_by_student.get(student_id, 0),
                "draft_count": draft_by_student.get(student_id, 0),
                "added": added,
                "removed": removed,
            }
        )
    return changes


def compare_findings(
    *,
    production_findings: Sequence[dict[str, Any]],
    draft_findings: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """比较两套规则的候选异常集合差异。"""
    prod_by_key = {f["key"]: f for f in production_findings}
    draft_by_key = {f["key"]: f for f in draft_findings}
    added_keys = set(draft_by_key) - set(prod_by_key)
    removed_keys = set(prod_by_key) - set(draft_by_key)
    unchanged_keys = set(prod_by_key) & set(draft_by_key)

    added = [draft_by_key[key] for key in sorted(added_keys)]
    removed = [prod_by_key[key] for key in sorted(removed_keys)]

    return {
        "added": added,
        "removed": removed,
        "counts": {
            "production": len(prod_by_key),
            "draft": len(draft_by_key),
            "added": len(added),
            "removed": len(removed),
            "unchanged": len(unchanged_keys),
        },
        "student_changes": _student_changes(
            production_findings, draft_findings, added_keys, removed_keys
        ),
    }
