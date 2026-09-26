"""异常规则引擎的边界、确定性与差异计算测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.compliance.anomaly_rules import (
    BUILTIN_PRODUCTION_RULES,
    RuleSet,
    detect_candidates,
    diff_candidates,
    validate_draft_thresholds,
)
from app.core.replay import Event, EventType, replay

RULES = RuleSet(
    version="test-v1",
    overlong_checkin_seconds=3600,
    overlap_seconds=1800,
    negative_correction_seconds=600,
)


def _checkin(event_id, student, start, end, plan="P1"):
    return Event(
        event_id=event_id,
        plan_version=plan,
        event_type=EventType.CHECKIN,
        student_id=student,
        payload={"check_in_at": start, "check_out_at": end},
        created_at=datetime(2024, 3, 15, tzinfo=timezone.utc),
    )


def _correction(event_id, student, seconds, plan="P1"):
    return Event(
        event_id=event_id,
        plan_version=plan,
        event_type=EventType.LEAVE_CORRECTION,
        student_id=student,
        payload={"adjustment_seconds": seconds, "reason": "修正"},
        created_at=datetime(2024, 3, 15, tzinfo=timezone.utc),
    )


def _state(events):
    return replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
    )


def test_threshold_boundary_is_strictly_greater():
    events = [
        # 恰好 3600 秒不算超长，3601 秒才算。
        _checkin("E-1", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _checkin("E-2", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:01+08:00"),
        # 重叠恰好 1800 秒不算，1801 秒才算（签到本身都不超长）。
        _checkin("E-3", "S3", "2024-03-15T08:00:00+08:00", "2024-03-15T08:40:00+08:00"),
        _checkin("E-4", "S3", "2024-03-15T08:10:00+08:00", "2024-03-15T08:50:00+08:00"),
        _checkin("E-5", "S6", "2024-03-15T08:00:00+08:00", "2024-03-15T08:40:01+08:00"),
        _checkin("E-6", "S6", "2024-03-15T08:10:00+08:00", "2024-03-15T08:50:01+08:00"),
        # 负向修正绝对值恰好 600 不算，601 才算。
        _correction("E-7", "S4", -600),
        _correction("E-8", "S5", -601),
    ]
    keys = [c["key"] for c in detect_candidates(_state(events), RULES)]
    assert keys == [
        "negative_correction:S5:E-8",
        "overlapping_activity:S6:E-5+E-6",
        "overlong_checkin:S2:E-2",
    ]


def test_overlap_pair_is_reported_once_with_sorted_events():
    events = [
        _checkin("E-2", "S1", "2024-03-15T09:00:00+08:00", "2024-03-15T11:00:00+08:00"),
        _checkin("E-1", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
    ]
    candidates = detect_candidates(_state(events), RULES)
    overlaps = [c for c in candidates if c["rule"] == "overlapping_activity"]
    assert len(overlaps) == 1
    assert overlaps[0]["event_ids"] == ["E-1", "E-2"]
    assert overlaps[0]["detail"]["overlap_seconds"] == 3600


def test_positive_correction_is_never_flagged():
    events = [_correction("E-1", "S1", 7200)]
    assert detect_candidates(_state(events), RULES) == []


def test_detection_is_deterministic_across_input_order():
    base = [
        _checkin("E-1", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
        _correction("E-2", "S2", -3601),
        _checkin("E-3", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
    ]
    first = detect_candidates(_state(base), RULES)
    second = detect_candidates(_state(list(reversed(base))), RULES)
    assert first == second


def test_diff_candidates_reports_added_removed_kept():
    production = [
        {"key": "a", "rule": "r"},
        {"key": "b", "rule": "r"},
        {"key": "c", "rule": "r"},
    ]
    draft = [
        {"key": "b", "rule": "r"},
        {"key": "c", "rule": "r"},
        {"key": "d", "rule": "r"},
    ]
    diff = diff_candidates(production, draft)
    assert diff["production_count"] == 3
    assert diff["draft_count"] == 3
    assert diff["added"] == ["d"]
    assert diff["removed"] == ["a"]
    assert diff["kept"] == ["b", "c"]
    assert diff["kept_count"] == 2


def test_validate_draft_thresholds():
    assert validate_draft_thresholds(
        {
            "overlong_checkin_seconds": 3600,
            "overlap_seconds": 0,
            "negative_correction_seconds": 600,
        }
    ) == {
        "overlong_checkin_seconds": 3600,
        "overlap_seconds": 0,
        "negative_correction_seconds": 600,
    }
    with pytest.raises(ValueError):
        validate_draft_thresholds({"overlong_checkin_seconds": 1})
    with pytest.raises(ValueError):
        validate_draft_thresholds(
            {
                "overlong_checkin_seconds": -1,
                "overlap_seconds": 0,
                "negative_correction_seconds": 0,
            }
        )
    with pytest.raises(ValueError):
        validate_draft_thresholds(
            {
                "overlong_checkin_seconds": True,
                "overlap_seconds": 0,
                "negative_correction_seconds": 0,
            }
        )


def test_builtin_production_rules_shape():
    data = BUILTIN_PRODUCTION_RULES.to_dict()
    assert data["version"] == "builtin-v1"
    assert RuleSet.from_dict(data) == BUILTIN_PRODUCTION_RULES
