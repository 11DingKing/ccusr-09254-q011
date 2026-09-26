"""沙箱 API 与编排测试：并发运行、规则版本与数据更新隔离。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

from app import repository_sandbox as repo
from app import services_sandbox
from app.models import Event as EventModel
from app.models import Sandbox, SandboxRun
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

DRAFT_RULES = {
    "overlong_checkin_seconds": 4 * 3600,
    "overlap_seconds": 1800,
    "negative_correction_seconds": 2 * 3600,
}

BUILTIN_RULE_SET = {
    "version": "builtin-v1",
    "overlong_checkin_seconds": 8 * 3600,
    "overlap_seconds": 0,
    "negative_correction_seconds": 4 * 3600,
}


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text
    return SHANGHAI_PLAN["plan_version"]


def _checkin(eid, student, start, end):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": "regular",
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _correction(eid, student, seconds):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": "考勤修正"},
    }


def _seed_events(client, pv):
    """固定一组覆盖三类异常的事件。"""
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T22:00:00+08:00"),
        _checkin("E-02", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T13:00:00+08:00"),
        _checkin("E-03", "S3", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-04", "S3", "2024-03-15T09:00:00+08:00", "2024-03-15T11:00:00+08:00"),
        _correction("E-05", "S4", -18000),
        _correction("E-06", "S5", -10800),
        _checkin("E-07", "S6", "2024-03-15T08:00:00+08:00", "2024-03-15T08:30:00+08:00"),
        _checkin("E-08", "S6", "2024-03-15T08:15:00+08:00", "2024-03-15T08:45:00+08:00"),
    ]
    resp = client.post(f"/api/plans/{pv}/events", json={"events": events})
    assert resp.status_code == 201, resp.text
    assert resp.json()["accepted"] == 8


def _create_sandbox(client, pv, sandbox_id="SB-1", draft=None, ttl=None):
    body = {"sandbox_id": sandbox_id, "draft_rules": draft or DRAFT_RULES}
    if ttl is not None:
        body["ttl_seconds"] = ttl
    return client.post(f"/api/plans/{pv}/sandboxes", json=body)


def _run(client, sandbox_id="SB-1", run_id="run-1"):
    return client.post(f"/api/sandboxes/{sandbox_id}/runs", json={"run_id": run_id})


def _expire_sandbox(db, sandbox_id):
    row = db.get(Sandbox, sandbox_id)
    assert row is not None
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()


def test_sandbox_run_and_diff_against_production(client):
    pv = _create_plan(client)
    _seed_events(client, pv)

    created = _create_sandbox(client, pv)
    assert created.status_code == 201, created.text
    sandbox = created.json()
    assert sandbox["event_cutoff_id"] == "E-08"
    assert sandbox["event_count"] == 8
    assert sandbox["expired"] is False
    assert sandbox["draft_rules"] == DRAFT_RULES

    run = _run(client)
    assert run.status_code == 201, run.text
    result = run.json()
    assert result["status"] == "succeeded"
    assert result["attempt"] == 1
    assert result["reused"] is False
    assert result["production_rule_version"] == "builtin-v1"
    assert result["production_candidate_count"] == 4
    assert result["draft_candidate_count"] == 5

    production_keys = {c["key"] for c in result["production_candidates"]}
    assert production_keys == {
        "overlong_checkin:S1:E-01",
        "overlapping_activity:S3:E-03+E-04",
        "overlapping_activity:S6:E-07+E-08",
        "negative_correction:S4:E-05",
    }

    diff_resp = client.get("/api/sandboxes/SB-1/diff")
    assert diff_resp.status_code == 200, diff_resp.text
    diff = diff_resp.json()
    assert diff["production_rule_version"] == "builtin-v1"
    assert diff["draft_rule_version"] == "draft"
    assert diff["diff"]["production_count"] == 4
    assert diff["diff"]["draft_count"] == 5
    assert diff["diff"]["added"] == [
        "negative_correction:S5:E-06",
        "overlong_checkin:S2:E-02",
    ]
    assert diff["diff"]["removed"] == ["overlapping_activity:S6:E-07+E-08"]
    assert diff["diff"]["kept_count"] == 3


def test_run_is_reentrant(client):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv)

    first = _run(client).json()
    second = _run(client)
    assert second.status_code == 200
    again = second.json()
    assert again["reused"] is True
    assert again["attempt"] == 1
    assert again["diff"] == first["diff"]

    fetched = client.get("/api/sandboxes/SB-1/runs/run-1")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "succeeded"
    assert fetched.json()["draft_candidate_count"] == 5


def test_run_restart_after_crash_reclaims_lease(client, db):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv)

    # 模拟崩溃的工作者：留下一条租约已过期的 running 记录。
    stale, created = repo.insert_run(
        db,
        run_id="run-stale",
        sandbox_id="SB-1",
        plan_version=pv,
        production_rule_version="builtin-v1",
        production_rules=BUILTIN_RULE_SET,
        draft_rules=DRAFT_RULES,
        lease_owner="worker-crashed",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    assert created and stale is not None

    resp = _run(client, run_id="run-stale")
    assert resp.status_code == 201, resp.text
    result = resp.json()
    assert result["status"] == "succeeded"
    assert result["attempt"] == 2
    assert result["draft_candidate_count"] == 5


def test_failed_run_is_retried(client, db):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv)

    repo.insert_run(
        db,
        run_id="run-failed",
        sandbox_id="SB-1",
        plan_version=pv,
        production_rule_version="builtin-v1",
        production_rules=BUILTIN_RULE_SET,
        draft_rules=DRAFT_RULES,
        lease_owner="worker-x",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    repo.complete_run(
        db,
        run_id="run-failed",
        lease_owner="worker-x",
        status="failed",
        error="simulated crash",
        finished_at=datetime.now(timezone.utc),
    )

    resp = _run(client, run_id="run-failed")
    assert resp.status_code == 201, resp.text
    result = resp.json()
    assert result["status"] == "succeeded"
    assert result["attempt"] == 2
    assert result["error"] is None


def test_concurrent_runs_only_one_computes(client, db):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv, sandbox_id="SB-CONC")

    results: list[dict] = []
    lock = threading.Lock()

    def _run():
        session = TestSessionLocal()
        try:
            view = services_sandbox.start_run(
                session, sandbox_id="SB-CONC", run_id="run-conc"
            )
            with lock:
                results.append(view)
        finally:
            session.close()

    threads = [threading.Thread(target=_run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 4
    assert all(r["status"] == "succeeded" for r in results)
    # 只有一个工作者真正计算，其余重入复用结果。
    assert sum(1 for r in results if not r["reused"]) == 1
    assert {r["attempt"] for r in results} == {1}
    assert len({json.dumps(r["diff"], sort_keys=True) for r in results}) == 1

    db.rollback()
    row = db.get(SandboxRun, "run-conc")
    assert row is not None
    assert row.status == "succeeded"
    assert row.attempt == 1


def test_sandbox_isolated_from_event_updates(client):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv)

    # 沙箱创建后又有新事件进入生产。
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-09",
                    "S7",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T18:00:00+08:00",
                )
            ]
        },
    )

    result = _run(client).json()
    assert result["production_candidate_count"] == 4
    assert result["draft_candidate_count"] == 5
    keys = {c["key"] for c in result["draft_candidates"]}
    assert all("S7" not in key for key in keys)

    # 生产快照包含新学员，沙箱事件副本仍固定为 8 条。
    live = client.get(f"/api/plans/{pv}/snapshot").json()
    assert any(s["student_id"] == "S7" for s in live["students"])
    view = client.get("/api/sandboxes/SB-1").json()
    assert view["event_count"] == 8
    assert view["event_cutoff_id"] == "E-08"


def test_rule_version_isolation_and_approval_flow(client):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv)

    first = _run(client).json()
    assert first["production_rule_version"] == "builtin-v1"

    # 采纳只登记申请，生产规则不变。
    adopted = client.post(
        "/api/sandboxes/SB-1/adoptions",
        json={"requested_by": "dean", "rule_version": "college-v2"},
    )
    assert adopted.status_code == 201, adopted.text
    adoption = adopted.json()
    assert adoption["status"] == "pending"
    assert adoption["rule_version"] == "college-v2"

    rules = client.get(f"/api/plans/{pv}/rules/production").json()
    assert rules["source"] == "builtin"
    assert rules["rules"]["version"] == "builtin-v1"

    # 发布审批通过后，草案才成为当前生产规则。
    decided = client.post(
        "/api/sandboxes/SB-1/adoptions/decision",
        json={"decision": "approved", "decided_by": "registrar"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"

    rules = client.get(f"/api/plans/{pv}/rules/production").json()
    assert rules["source"] == "adoption"
    assert rules["rules"]["version"] == "college-v2"
    assert rules["rules"]["overlong_checkin_seconds"] == DRAFT_RULES[
        "overlong_checkin_seconds"
    ]

    # 旧运行仍记录当时的生产规则版本（规则版本隔离）。
    old_run = client.get("/api/sandboxes/SB-1/runs/run-1").json()
    assert old_run["production_rule_version"] == "builtin-v1"

    # 新运行使用新生产规则：草案已发布，差异归零。
    second = _run(client, run_id="run-2").json()
    assert second["production_rule_version"] == "college-v2"
    assert second["diff"]["added_count"] == 0
    assert second["diff"]["removed_count"] == 0
    assert second["diff"]["kept_count"] == 5


def test_adoption_requires_succeeded_run(client):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv)
    resp = client.post(
        "/api/sandboxes/SB-1/adoptions", json={"requested_by": "dean"}
    )
    assert resp.status_code == 409


def test_adoption_decision_only_once_and_rejection(client):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv)
    _run(client)

    client.post("/api/sandboxes/SB-1/adoptions", json={"requested_by": "dean"})
    first = client.post(
        "/api/sandboxes/SB-1/adoptions/decision",
        json={"decision": "rejected", "decided_by": "registrar", "reason": "再评估"},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "rejected"

    second = client.post(
        "/api/sandboxes/SB-1/adoptions/decision",
        json={"decision": "approved", "decided_by": "registrar"},
    )
    assert second.status_code == 409

    rules = client.get(f"/api/plans/{pv}/rules/production").json()
    assert rules["source"] == "builtin"


def test_adoption_is_idempotent(client):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv)
    _run(client)

    first = client.post("/api/sandboxes/SB-1/adoptions", json={"requested_by": "dean"})
    assert first.status_code == 201
    second = client.post("/api/sandboxes/SB-1/adoptions", json={"requested_by": "dean"})
    assert second.status_code == 200
    assert second.json()["adoption_id"] == first.json()["adoption_id"]


def test_expired_sandbox_rejects_operations(client, db):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv, ttl=3600)
    _run(client)

    _expire_sandbox(db, "SB-1")

    assert _run(client, run_id="run-2").status_code == 410
    assert client.get("/api/sandboxes/SB-1/diff").status_code == 410
    adopt = client.post(
        "/api/sandboxes/SB-1/adoptions", json={"requested_by": "dean"}
    )
    assert adopt.status_code == 410

    view = client.get("/api/sandboxes/SB-1").json()
    assert view["expired"] is True


def test_cleanup_removes_only_expired(client, db):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv, sandbox_id="SB-OLD")
    _create_sandbox(client, pv, sandbox_id="SB-NEW")
    _run(client, sandbox_id="SB-OLD", run_id="run-old")
    _run(client, sandbox_id="SB-NEW", run_id="run-new")

    _expire_sandbox(db, "SB-OLD")

    resp = client.post("/api/sandboxes/cleanup")
    assert resp.status_code == 200
    assert resp.json()["removed_sandbox_ids"] == ["SB-OLD"]

    assert client.get("/api/sandboxes/SB-OLD").status_code == 404
    survivor = client.get("/api/sandboxes/SB-NEW").json()
    assert survivor["latest_run_status"] == "succeeded"
    assert client.get("/api/sandboxes/SB-NEW/diff").status_code == 200


def test_delete_sandbox_removes_all_data(client):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv)
    _run(client)

    resp = client.delete("/api/sandboxes/SB-1")
    assert resp.status_code == 204
    assert client.get("/api/sandboxes/SB-1").status_code == 404
    assert client.delete("/api/sandboxes/SB-1").status_code == 404


def test_sandbox_does_not_touch_production_state(client, db):
    pv = _create_plan(client)
    _seed_events(client, pv)
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    live_before = client.get(f"/api/plans/{pv}/snapshot").json()
    frozen_before = client.get(f"/api/plans/{pv}/freezes/F-01").json()

    _create_sandbox(client, pv)
    _run(client)
    client.post("/api/sandboxes/SB-1/adoptions", json={"requested_by": "dean"})
    client.post(
        "/api/sandboxes/SB-1/adoptions/decision",
        json={"decision": "approved", "decided_by": "registrar"},
    )

    # 生产事件、实时快照与冻结快照在整个沙箱生命周期后保持不变。
    db.rollback()
    event_count = db.query(EventModel).count()
    assert event_count == 8
    live_after = client.get(f"/api/plans/{pv}/snapshot").json()
    live_after.pop("generated_at")
    live_before.pop("generated_at")
    assert live_after == live_before
    assert client.get(f"/api/plans/{pv}/freezes/F-01").json() == frozen_before


def test_create_sandbox_reentrant_and_conflicting(client):
    pv = _create_plan(client)
    _seed_events(client, pv)

    first = _create_sandbox(client, pv)
    assert first.status_code == 201
    second = _create_sandbox(client, pv)
    assert second.status_code == 200
    assert second.json()["sandbox_id"] == "SB-1"

    different = dict(DRAFT_RULES, overlap_seconds=60)
    conflict = _create_sandbox(client, pv, draft=different)
    assert conflict.status_code == 409


def test_create_sandbox_validation(client):
    pv = _create_plan(client)

    missing = dict(DRAFT_RULES)
    del missing["overlap_seconds"]
    resp = _create_sandbox(client, pv, draft=missing)
    assert resp.status_code == 422

    negative = dict(DRAFT_RULES, overlap_seconds=-1)
    assert _create_sandbox(client, pv, draft=negative).status_code == 422

    assert _create_sandbox(client, pv, ttl=0).status_code == 422
    assert (
        client.post(
            "/api/plans/NOPE/sandboxes",
            json={"sandbox_id": "SB-X", "draft_rules": DRAFT_RULES},
        ).status_code
        == 404
    )


def test_diff_requires_run(client):
    pv = _create_plan(client)
    _seed_events(client, pv)
    _create_sandbox(client, pv)
    assert client.get("/api/sandboxes/SB-1/diff").status_code == 409


def test_unknown_sandbox_returns_404(client):
    assert client.get("/api/sandboxes/NOPE").status_code == 404
    assert client.post("/api/sandboxes/NOPE/runs", json={}).status_code == 404
    assert client.get("/api/sandboxes/NOPE/diff").status_code == 404
