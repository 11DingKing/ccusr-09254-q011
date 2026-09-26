"""异常规则引擎：超长签到、重叠活动与负向修正的纯函数判定。

该模块不读写数据库、不发送通知、不修改案件，只对输入的重放状态做
确定性计算，供生产管线与只读沙箱共用。规则以版本化的阈值集合表示，
同一组事件与同一套规则永远产出相同的候选异常列表。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..core.replay import ReplayState


# 内置生产规则版本：没有任何已批准规则版本时作为当前生产规则。
BUILTIN_PRODUCTION_VERSION = "builtin-v1"

# 沙箱规则草案在运行记录中使用的伪版本标签。
DRAFT_VERSION_LABEL = "draft"


@dataclass(frozen=True)
class RuleSet:
    """一套异常阈值规则。阈值均为秒，判定条件为“严格大于”。"""

    version: str
    overlong_checkin_seconds: int
    overlap_seconds: int
    negative_correction_seconds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "overlong_checkin_seconds": self.overlong_checkin_seconds,
            "overlap_seconds": self.overlap_seconds,
            "negative_correction_seconds": self.negative_correction_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuleSet":
        return cls(
            version=str(data["version"]),
            overlong_checkin_seconds=int(data["overlong_checkin_seconds"]),
            overlap_seconds=int(data["overlap_seconds"]),
            negative_correction_seconds=int(data["negative_correction_seconds"]),
        )


BUILTIN_PRODUCTION_RULES = RuleSet(
    version=BUILTIN_PRODUCTION_VERSION,
    overlong_checkin_seconds=8 * 3600,
    overlap_seconds=0,
    negative_correction_seconds=4 * 3600,
)


def validate_draft_thresholds(data: Mapping[str, Any]) -> dict[str, int]:
    """校验并规范化规则草案阈值，缺失或非法时抛出 ValueError。"""
    required = (
        "overlong_checkin_seconds",
        "overlap_seconds",
        "negative_correction_seconds",
    )
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"规则草案缺少阈值字段: {', '.join(missing)}")
    normalized: dict[str, int] = {}
    for key in required:
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"阈值 {key} 必须是非负整数秒")
        if value < 0:
            raise ValueError(f"阈值 {key} 不能为负数")
        normalized[key] = value
    return normalized


def _candidate(
    rule: str,
    student_id: str,
    event_ids: Iterable[str],
    detail: Mapping[str, Any],
) -> dict[str, Any]:
    ids = sorted(event_ids)
    return {
        "key": f"{rule}:{student_id}:{'+'.join(ids)}",
        "rule": rule,
        "student_id": student_id,
        "event_ids": ids,
        "detail": dict(detail),
    }


def detect_candidates(state: ReplayState, rules: RuleSet) -> list[dict[str, Any]]:
    """对重放状态应用规则，返回按 key 排序的候选异常列表（纯函数）。"""
    candidates: list[dict[str, Any]] = []
    for student_id in sorted(state.students):
        progress = state.students[student_id]

        # 超长签到：单次签到时长严格大于阈值。
        for record in progress.checkins:
            if record.seconds > rules.overlong_checkin_seconds:
                candidates.append(
                    _candidate(
                        "overlong_checkin",
                        student_id,
                        [record.event_id],
                        {
                            "activity_id": record.activity_id,
                            "activity_type": record.activity_type,
                            "seconds": record.seconds,
                            "threshold_seconds": rules.overlong_checkin_seconds,
                        },
                    )
                )

        # 重叠活动：同一学员任意两次签到的时间交集严格大于阈值。
        ordered = sorted(
            progress.checkins, key=lambda r: (r.start_utc, r.end_utc, r.event_id)
        )
        for index, first in enumerate(ordered):
            for second in ordered[index + 1 :]:
                if second.start_utc >= first.end_utc:
                    break
                overlap_end = min(first.end_utc, second.end_utc)
                overlap_seconds = int(
                    (overlap_end - second.start_utc).total_seconds()
                )
                if overlap_seconds > rules.overlap_seconds:
                    candidates.append(
                        _candidate(
                            "overlapping_activity",
                            student_id,
                            [first.event_id, second.event_id],
                            {
                                "overlap_seconds": overlap_seconds,
                                "threshold_seconds": rules.overlap_seconds,
                                "activity_ids": sorted(
                                    {first.activity_id, second.activity_id}
                                ),
                            },
                        )
                    )

        # 负向修正：请假修正秒数为负且绝对值严格大于阈值。
        for adjustment in progress.adjustments:
            if adjustment.seconds < 0 and (
                -adjustment.seconds
            ) > rules.negative_correction_seconds:
                candidates.append(
                    _candidate(
                        "negative_correction",
                        student_id,
                        [adjustment.event_id],
                        {
                            "adjustment_seconds": adjustment.seconds,
                            "threshold_seconds": rules.negative_correction_seconds,
                            "reason": adjustment.reason,
                        },
                    )
                )

    candidates.sort(key=lambda c: c["key"])
    return candidates


def diff_candidates(
    production: list[dict[str, Any]], draft: list[dict[str, Any]]
) -> dict[str, Any]:
    """比较两套候选异常，给出新增、解除与保留的 key 及计数。"""
    production_keys = {c["key"] for c in production}
    draft_keys = {c["key"] for c in draft}
    added = sorted(draft_keys - production_keys)
    removed = sorted(production_keys - draft_keys)
    kept = sorted(production_keys & draft_keys)
    return {
        "production_count": len(production_keys),
        "draft_count": len(draft_keys),
        "added": added,
        "removed": removed,
        "kept": kept,
        "added_count": len(added),
        "removed_count": len(removed),
        "kept_count": len(kept),
    }
