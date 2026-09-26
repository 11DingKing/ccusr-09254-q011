"""只读异常规则沙箱的 API 与隔离/并发测试。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from tests.conftest import TestSessionLocal, SHANGHAI_PLAN

from app import sandbox_service
from app.compliance.anomaly_detection import (
    RuleThresholds,
    detect_anomalies,
)
from app.models import Event as EventModel, Freeze, RulePublishRequest, RuleRelease

PROD = RuleThresholds.production_default().to_dict()


def _checkin(eid, student, start, end, activity_id="A1", activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": activity_id,
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _correction(eid, student, seconds, reason="r"):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": reason},
    }


def _seed_events(client, pv, events):
    resp = client.post(f"/api/plans/{pv}/events", json={"events": events})
    assert resp.status_code == 201, resp.text


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text
    return SHANGHAI_PLAN["plan_version"]


def _draft(**overrides):
    values = dict(PROD)
    values.update(overrides)
    return values


def _create_sandbox(client, pv, draft=None, sandbox_id="SB-1", ttl=None):
    body: dict = {
        "draft_thresholds": draft or _draft(),
        "created_by": "dean-chen",
        "sandbox_id": sandbox_id,
    }
    if ttl is not None:
        body["ttl_seconds"] = ttl
    resp = client.post(f"/api/plans/{pv}/rule-sandboxes", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _run(client, sandbox_id="SB-1", expected=200):
    resp = client.post(f"/api/rule-sandboxes/{sandbox_id}/run", json={})
    assert resp.status_code == expected, resp.text
    return resp.json() if expected < 400 else resp


# --------------------------------------------------------------------------
# 纯检测函数
# --------------------------------------------------------------------------


def test_detect_covers_three_anomaly_types():
    events = [
        # 超长签到：13 小时 > 12 小时生产阈值
        _checkin(
            "E-01", "S1",
            "2024-03-15T07:00:00+08:00",
            "2024-03-15T20:00:00+08:00",
        ),
        # 重叠活动：与下一条签到重叠 30 分钟
        _checkin(
            "E-02", "S2",
            "2024-03-16T08:00:00+08:00",
            "2024-03-16T10:00:00+08:00",
            activity_id="A2",
        ),
        _checkin(
            "E-03", "S2",
            "2024-03-16T09:30:00+08:00",
            "2024-03-16T11:00:00+08:00",
            activity_id="A3",
        ),
        # 大额负向修正：-3600 达到阈值
        _correction("E-04", "S3", -3600),
        # 不触发：小额负向修正
        _correction("E-05", "S3", -60),
    ]
    findings = detect_anomalies(events, RuleThresholds.production_default())
    by_type: dict[str, list] = {}
    for f in findings:
        by_type.setdefault(f["rule_type"], []).append(f)
    assert len(by_type["overlong_checkin"]) == 1
    assert by_type["overlong_checkin"][0]["event_ids"] == ["E-01"]
    assert len(by_type["overlapping_activity"]) == 1
    assert by_type["overlapping_activity"][0]["event_ids"] == ["E-02", "E-03"]
    assert len(by_type["negative_correction"]) == 1
    assert by_type["negative_correction"][0]["event_ids"] == ["E-04"]


# --------------------------------------------------------------------------
# 创建 / 运行 / 比较
# --------------------------------------------------------------------------


def test_create_run_compare_lifecycle(client):
    pv = _create_plan(client)
    _seed_events(
        client, pv,
        [
            _checkin(
                "E-01", "S1",
                "2024-03-15T07:00:00+08:00",
                "2024-03-15T15:00:00+08:00",
            ),  # 8h：生产阈值 12h 不触发，草案 6h 触发
            _correction("E-02", "S1", -1800),  # 生产阈值 3600 不触发，草案 900 触发
        ],
    )
    sb = _create_sandbox(
        client, pv,
        draft=_draft(overlong_seconds=6 * 3600, negative_correction_seconds=900),
    )
    assert sb["status"] == "ready"
    assert sb["event_count"] == 2
    assert sb["event_cutoff_id"] == "E-02"
    assert sb["production_rule_version"] == "builtin:production-v1"
    assert sb["production_findings"] == []

    run = _run(client)
    assert run["status"] == "completed"
    assert run["attempts"] == 1
    prod_keys = {f["key"] for f in run["production_findings"]}
    draft_keys = {f["key"] for f in run["draft_findings"]}
    assert prod_keys == set()
    assert draft_keys == {"overlong:E-01", "negative:E-02"}

    cmp_resp = client.get("/api/rule-sandboxes/SB-1/comparison")
    assert cmp_resp.status_code == 200, cmp_resp.text
    body = cmp_resp.json()
    assert body["comparison"]["counts"] == {
        "production": 0,
        "draft": 2,
        "added": 2,
        "removed": 0,
        "unchanged": 0,
    }
    added_keys = {item["key"] for item in body["comparison"]["added"]}
    assert added_keys == {"overlong:E-01", "negative:E-02"}
    assert body["comparison"]["removed"] == []


def test_comparison_before_run_returns_409(client):
    pv = _create_plan(client)
    _create_sandbox(client, pv)
    resp = client.get("/api/rule-sandboxes/SB-1/comparison")
    assert resp.status_code == 409


def test_run_is_idempotent_and_read_only(client, db):
    pv = _create_plan(client)
    _seed_events(
        client, pv,
        [_correction("E-01", "S1", -3600)],
    )
    _create_sandbox(client, pv, draft=_draft(negative_correction_seconds=600))
    first = _run(client)
    second = _run(client)  # 重入：completed 直接返回既有结果
    assert second["attempts"] == 1
    assert second["ran_at"] == first["ran_at"]
    assert second["draft_findings"] == first["draft_findings"]

    # 运行不触碰正式数据：无案件/通知落库所需的发布记录、无冻结、事件原样。
    assert db.query(RuleRelease).count() == 0
    assert db.query(RulePublishRequest).count() == 0
    assert db.query(Freeze).count() == 0
    assert db.query(EventModel).count() == 1


# --------------------------------------------------------------------------
# 并发运行
# --------------------------------------------------------------------------


def test_concurrent_runs_only_one_executes(client):
    pv = _create_plan(client)
    _seed_events(
        client, pv,
        [
            _checkin(
                "E-01", "S1",
                "2024-03-15T07:00:00+08:00",
                "2024-03-15T20:00:00+08:00",
            )
        ],
    )
    _create_sandbox(client, pv, draft=_draft(overlong_seconds=6 * 3600))

    statuses: list[int] = []
    results: list[dict] = []
    lock = threading.Lock()

    def _worker(idx: int):
        session = TestSessionLocal()
        try:
            try:
                out = sandbox_service.run_sandbox(
                    session, sandbox_id="SB-1", worker_id=f"worker-{idx}"
                )
                code = 200
            except sandbox_service.SandboxBusyError:
                code = 409
                out = None
            with lock:
                statuses.append(code)
                if out is not None:
                    results.append(out)
        finally:
            session.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 只有一个执行者实际计算；输掉的线程要么 409，要么在对方完成后以重入
    # 语义拿到同一份结果（200），二者都合法。
    assert set(statuses) <= {200, 409}
    assert 200 in statuses
    final = client.get("/api/rule-sandboxes/SB-1").json()
    assert final["status"] == "completed"
    assert final["attempts"] == 1  # 关键：计算只发生一次
    assert len(final["draft_findings"]) == 1
    assert all(
        out["ran_at"] == final["ran_at"] and out["attempts"] == 1
        for out in results
    )


# --------------------------------------------------------------------------
# 任务重入与崩溃重启
# --------------------------------------------------------------------------


def test_expired_lease_allows_takeover_and_restart(client, db):
    pv = _create_plan(client)
    _seed_events(client, pv, [_correction("E-01", "S1", -7200)])
    _create_sandbox(client, pv, draft=_draft(negative_correction_seconds=600))

    # 模拟前执行者崩溃：留下 running 状态与已过期租约。
    from app import sandbox_repository as repo

    t0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
    assert repo.acquire_run_lease(
        db,
        sandbox_id="SB-1",
        owner="dead-worker:abcd1234",
        now=t0,
        lease_expires_at=t0 + timedelta(seconds=300),
    )
    stuck = client.get("/api/rule-sandboxes/SB-1").json()
    assert stuck["status"] == "running"

    # 租约未过期时，其他执行者不能抢占。
    session = TestSessionLocal()
    try:
        try:
            sandbox_service.run_sandbox(
                session, sandbox_id="SB-1", worker_id="backup", now=t0
            )
            raised = None
        except sandbox_service.SandboxBusyError as exc:
            raised = exc
        assert raised is not None
    finally:
        session.close()

    # 租约过期后接管重启，结果正常产出，attempts 累计为 2。
    session2 = TestSessionLocal()
    try:
        out = sandbox_service.run_sandbox(
            session2,
            sandbox_id="SB-1",
            worker_id="backup",
            now=t0 + timedelta(seconds=301),
        )
    finally:
        session2.close()
    assert out["status"] == "completed"
    assert out["attempts"] == 2
    assert {f["key"] for f in out["draft_findings"]} == {"negative:E-01"}


# --------------------------------------------------------------------------
# 数据更新隔离
# --------------------------------------------------------------------------


def test_sandbox_is_isolated_from_later_data_updates(client):
    pv = _create_plan(client)
    _seed_events(client, pv, [_correction("E-01", "S1", -7200)])
    _create_sandbox(client, pv, draft=_draft(negative_correction_seconds=600))

    # 沙箱创建后生产数据继续变化：新增本应触发草案的负向修正。
    _seed_events(client, pv, [_correction("E-99", "S2", -7200)])

    run = _run(client)
    assert run["event_count"] == 1  # 仍是创建时的快照
    assert run["event_cutoff_id"] == "E-01"
    assert {f["key"] for f in run["draft_findings"]} == {"negative:E-01"}

    # 比较接口同样基于冻结快照。
    cmp_body = client.get("/api/rule-sandboxes/SB-1/comparison").json()
    assert cmp_body["event_count"] == 1
    assert cmp_body["comparison"]["counts"]["draft"] == 1


# --------------------------------------------------------------------------
# 规则版本隔离
# --------------------------------------------------------------------------


def _publish_new_rule(client, pv, draft, sandbox_id, request_id):
    """通过 沙箱→运行→采纳→审批 的完整流程发布新版本。"""
    _create_sandbox(client, pv, draft=draft, sandbox_id=sandbox_id)
    _run(client, sandbox_id)
    adopt = client.post(
        f"/api/rule-sandboxes/{sandbox_id}/adopt",
        json={"requested_by": "dean-chen", "request_id": request_id},
    )
    assert adopt.status_code == 201, adopt.text
    review = client.post(
        f"/api/rule-publish-requests/{request_id}/review",
        json={"approve": True, "reviewed_by": "provost-li", "review_reason": "ok"},
    )
    assert review.status_code == 200, review.text
    assert review.json()["status"] == "approved"
    return review.json()


def test_sandbox_pins_production_rule_version_at_creation(client):
    pv = _create_plan(client)
    _seed_events(client, pv, [_correction("E-01", "S1", -1800)])

    # SB-A 在旧生产版本下创建（阈值 3600，-1800 不触发）。
    sb_a = _create_sandbox(
        client, pv, draft=_draft(negative_correction_seconds=600), sandbox_id="SB-A"
    )
    assert sb_a["production_rule_version"] == "builtin:production-v1"

    # 发布更严格的生产规则（阈值降到 600）。
    _publish_new_rule(
        client, pv,
        draft=_draft(negative_correction_seconds=600),
        sandbox_id="SB-PUB",
        request_id="RR-1",
    )

    # SB-A 运行时仍按冻结的旧生产阈值计算，production 侧为 0 条。
    run_a = _run(client, "SB-A")
    assert run_a["production_rule_version"] == "builtin:production-v1"
    assert run_a["production_findings"] == []
    assert {f["key"] for f in run_a["draft_findings"]} == {"negative:E-01"}
    cmp_a = client.get("/api/rule-sandboxes/SB-A/comparison").json()
    assert cmp_a["production_rule_version"] == "builtin:production-v1"

    # 新创建的 SB-B 看到的是新发布版本，两侧结果一致（草案未再变更）。
    sb_b = _create_sandbox(
        client, pv, draft=_draft(negative_correction_seconds=600), sandbox_id="SB-B"
    )
    assert sb_b["production_rule_version"] == "published:RR-1"
    run_b = _run(client, "SB-B")
    assert {f["key"] for f in run_b["production_findings"]} == {"negative:E-01"}
    assert run_b["comparison"]["counts"]["added"] == 0
    assert run_b["comparison"]["counts"]["unchanged"] == 1


def test_adopt_requires_completed_run(client):
    pv = _create_plan(client)
    _seed_events(client, pv, [_correction("E-01", "S1", -7200)])
    _create_sandbox(client, pv, draft=_draft())
    resp = client.post(
        "/api/rule-sandboxes/SB-1/adopt", json={"requested_by": "dean-chen"}
    )
    assert resp.status_code == 400


def test_adopt_is_idempotent_and_waits_for_approval(client, db):
    pv = _create_plan(client)
    _seed_events(client, pv, [_correction("E-01", "S1", -7200)])
    _create_sandbox(client, pv, draft=_draft(negative_correction_seconds=600))
    _run(client)

    first = client.post(
        "/api/rule-sandboxes/SB-1/adopt",
        json={"requested_by": "dean-chen", "request_id": "RR-9"},
    )
    assert first.status_code == 201
    # 重复采纳返回同一张审批单，不重复建单。
    second = client.post(
        "/api/rule-sandboxes/SB-1/adopt",
        json={"requested_by": "dean-chen", "request_id": "RR-OTHER"},
    )
    assert second.status_code == 201
    assert second.json()["request_id"] == "RR-9"
    assert db.query(RuleRelease).count() == 0  # 未审批前生产规则不变

    # 驳回：不发布。
    review = client.post(
        "/api/rule-publish-requests/RR-9/review",
        json={"approve": False, "reviewed_by": "provost-li", "review_reason": "再议"},
    )
    assert review.json()["status"] == "rejected"
    assert db.query(RuleRelease).count() == 0

    # 不能重复审批。
    again = client.post(
        "/api/rule-publish-requests/RR-9/review",
        json={"approve": True, "reviewed_by": "provost-li", "review_reason": "x"},
    )
    assert again.status_code == 400


# --------------------------------------------------------------------------
# 期限与清理
# --------------------------------------------------------------------------


def test_expired_sandbox_cannot_run_adopt_or_compare(client, db):
    pv = _create_plan(client)
    _seed_events(client, pv, [_correction("E-01", "S1", -7200)])

    # 直接通过服务层注入创建时刻，得到一个已到期的沙箱。
    created = datetime(2024, 1, 1, tzinfo=timezone.utc)
    sandbox_service.create_sandbox(
        db,
        plan_version=pv,
        draft_thresholds=_draft(negative_correction_seconds=600),
        created_by="dean-chen",
        ttl_seconds=3600,
        sandbox_id="SB-OLD",
        now=created,
    )
    later = created + timedelta(hours=2)

    session = TestSessionLocal()
    try:
        for call in (
            lambda: sandbox_service.run_sandbox(session, sandbox_id="SB-OLD", now=later),
            lambda: sandbox_service.adopt_sandbox(
                session, sandbox_id="SB-OLD", requested_by="dean-chen", now=later
            ),
            lambda: sandbox_service.get_comparison(
                session, sandbox_id="SB-OLD", now=later
            ),
        ):
            try:
                call()
                raised = None
            except sandbox_service.SandboxExpiredError as exc:
                raised = exc
            assert raised is not None
    finally:
        session.close()

    # GET 惰性标记过期。
    got = client.get("/api/rule-sandboxes/SB-OLD")
    assert got.status_code == 200
    assert got.json()["status"] == "expired"

    # 已到期沙箱可清理。
    deleted = client.delete("/api/rule-sandboxes/SB-OLD")
    assert deleted.status_code == 204
    assert client.get("/api/rule-sandboxes/SB-OLD").status_code == 404


def test_cleanup_expired_batch_and_skips_pending(client, db):
    pv = _create_plan(client)
    _seed_events(client, pv, [_correction("E-01", "S1", -7200)])
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)

    sandbox_service.create_sandbox(
        db, plan_version=pv, draft_thresholds=_draft(), created_by="u",
        sandbox_id="SB-EXPIRED-1", ttl_seconds=60, now=t0,
    )
    # 第二个到期沙箱已产生待审批单：清理时必须保留。
    sandbox_service.create_sandbox(
        db, plan_version=pv, draft_thresholds=_draft(negative_correction_seconds=600),
        created_by="u", sandbox_id="SB-EXPIRED-2", ttl_seconds=60, now=t0,
    )
    sandbox_service.run_sandbox(db, sandbox_id="SB-EXPIRED-2", now=t0)
    sandbox_service.adopt_sandbox(
        db, sandbox_id="SB-EXPIRED-2", requested_by="u", now=t0
    )
    # 未到期沙箱不应被清理（创建于当前真实时间，TTL 一天）。
    sandbox_service.create_sandbox(
        db, plan_version=pv, draft_thresholds=_draft(), created_by="u",
        sandbox_id="SB-FRESH", ttl_seconds=86400,
    )

    result = client.post("/api/rule-sandboxes/cleanup/expired")
    assert result.status_code == 200, result.text
    assert result.json() == {"removed": 1, "skipped_pending": 1}

    assert client.get("/api/rule-sandboxes/SB-EXPIRED-1").status_code == 404
    assert client.get("/api/rule-sandboxes/SB-EXPIRED-2").status_code == 200
    assert client.get("/api/rule-sandboxes/SB-FRESH").status_code == 200


def test_running_sandbox_cannot_be_deleted(client):
    pv = _create_plan(client)
    _seed_events(client, pv, [_correction("E-01", "S1", -7200)])
    _create_sandbox(client, pv)
    # 人为挂上一个有效的 running 租约。
    from app import sandbox_repository as repo

    now = datetime.now(timezone.utc)
    with TestSessionLocal() as session:
        assert repo.acquire_run_lease(
            session,
            sandbox_id="SB-1",
            owner="w:x",
            now=now,
            lease_expires_at=now + timedelta(minutes=5),
        )
    resp = client.delete("/api/rule-sandboxes/SB-1")
    assert resp.status_code == 400


def test_approve_after_sandbox_expiry_is_rejected(client, db):
    pv = _create_plan(client)
    _seed_events(client, pv, [_correction("E-01", "S1", -7200)])
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    sandbox_service.create_sandbox(
        db, plan_version=pv, draft_thresholds=_draft(negative_correction_seconds=600),
        created_by="u", sandbox_id="SB-TTL", ttl_seconds=3600, now=t0,
    )
    sandbox_service.run_sandbox(db, sandbox_id="SB-TTL", now=t0)
    req = sandbox_service.adopt_sandbox(
        db, sandbox_id="SB-TTL", requested_by="u", request_id="RR-TTL", now=t0
    )
    assert req["status"] == "pending"

    reviewed = sandbox_service.review_publish_request(
        db,
        request_id="RR-TTL",
        approve=True,
        reviewed_by="provost-li",
        review_reason="late approve",
        now=t0 + timedelta(hours=2),
    )
    assert reviewed["status"] == "rejected"
    assert db.query(RuleRelease).count() == 0
