# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。

## 异常规则只读沙箱

调整超长签到、重叠活动和负向修正阈值前，可在只读沙箱中预估新规则产生的候选异常数量，与当前生产规则对比。沙箱在**创建时冻结事件快照和当时生效的生产规则版本**，此后与生产数据更新和规则发布完全隔离；运行只在快照上计算，不创建正式案件、不触发通知。

| 操作 | 接口 |
| --- | --- |
| 创建沙箱 | `POST /api/plans/{plan_version}/rule-sandboxes` |
| 运行（可重入） | `POST /api/rule-sandboxes/{sandbox_id}/run` |
| 查询状态/结果 | `GET /api/rule-sandboxes/{sandbox_id}` |
| 比较差异 | `GET /api/rule-sandboxes/{sandbox_id}/comparison` |
| 采纳（生成待审批发布单） | `POST /api/rule-sandboxes/{sandbox_id}/adopt` |
| 发布审批 | `POST /api/rule-publish-requests/{request_id}/review` |
| 手动清理 | `DELETE /api/rule-sandboxes/{sandbox_id}` |
| 批量清理到期沙箱 | `POST /api/rule-sandboxes/cleanup/expired` |

要点：

- 草案阈值字段为 `overlong_seconds`、`overlap_min_seconds`、`negative_correction_seconds`，沙箱默认有效期 7 天（最长 90 天）。
- 并发运行通过数据库租约保证计算只发生一次：`completed` 重入直接返回旧结果，租约有效时其他执行者收到 409，租约过期（前执行者崩溃）后可被接管重启。
- 采纳不改变生产规则，只生成 `pending` 发布审批单；审批通过才发布新规则版本并退役旧版本，到期沙箱的审批会被自动驳回。
- 沙箱测试覆盖并发运行、崩溃后接管重启、规则版本隔离、数据更新隔离、期限过期与清理。
