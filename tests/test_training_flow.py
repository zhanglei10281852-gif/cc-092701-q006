from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.clock import FrozenClock
from app.database import get_connection
from app.training.service import TrainingFlowService


# 读图 -> 计算 -> 模拟 -> 教师复核 的四步数控实训流程。
def flow_payload(*, flow_code: str = "cnc-training", policy: dict | None = None) -> dict:
    policy = policy or {}
    steps = [
        {"code": "read", "name": "读图", "depends_on": [], **_policy(policy, "read")},
        {"code": "calc", "name": "计算", "depends_on": ["read"], **_policy(policy, "calc")},
        {"code": "sim", "name": "模拟", "depends_on": ["calc"], **_policy(policy, "sim")},
        {"code": "review", "name": "教师复核", "depends_on": ["sim"], **_policy(policy, "review")},
    ]
    return {"flow_code": flow_code, "course_code": "CNC-101", "name": "数控车削实训", "steps": steps}


def _policy(policy: dict, code: str) -> dict:
    return policy.get(code, {})


@pytest.fixture()
def service(tmp_path: Path, monkeypatch) -> TrainingFlowService:
    db_path = tmp_path / "training.db"
    monkeypatch.setenv("TOWNSHIP_DATABASE_PATH", str(db_path))
    from app.database import close_connection
    close_connection()
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
    return TrainingFlowService(get_connection(), clock)


def start(service: TrainingFlowService, key: str = "class-2026-01", payload: dict | None = None) -> dict:
    service.create_flow(payload or flow_payload(), "academic-affairs")
    return service.start_instance((payload or flow_payload())["flow_code"], key)


def node(view: dict, code: str) -> dict:
    return next(item for item in view["nodes"] if item["step_code"] == code)


# ---------------- 一次性提交的写入校验 ----------------

def test_reject_cycle(service):
    payload = flow_payload(flow_code="cycle-flow")
    payload["steps"][0]["depends_on"] = ["review"]  # read <- review 形成环
    with pytest.raises(Exception) as exc:
        service.create_flow(payload, "academic-affairs")
    assert "环路" in str(exc.value)
    with pytest.raises(Exception):
        service.get_flow("cycle-flow")


def test_reject_dangling_reference(service):
    payload = flow_payload(flow_code="dangling-flow")
    payload["steps"][1]["depends_on"] = ["read", "ghost"]
    with pytest.raises(Exception) as exc:
        service.create_flow(payload, "academic-affairs")
    assert "不存在" in str(exc.value)


def test_reject_duplicate_step_code(service):
    payload = flow_payload(flow_code="dup-flow")
    payload["steps"].append(dict(payload["steps"][0]))
    # pydantic 层或服务层都会拒绝；这里直接走服务的字典入口模拟重复编码。
    from app.training.schemas import FlowSubmit
    with pytest.raises(Exception):
        FlowSubmit.model_validate(payload)


def test_valid_dag_persists_topological_order(service):
    detail = service.create_flow(flow_payload(), "academic-affairs")
    codes = [step["code"] for step in detail["steps"]]
    assert codes.index("read") < codes.index("calc") < codes.index("sim") < codes.index("review")
    assert detail["reused"] is False


def test_duplicate_submission_reuses_flow(service):
    first = service.create_flow(flow_payload(), "academic-affairs")
    second = service.create_flow(flow_payload(), "academic-affairs")
    assert first["id"] == second["id"]
    assert second["reused"] is True


def test_duplicate_submission_with_changed_definition_rejected(service):
    service.create_flow(flow_payload(), "academic-affairs")
    changed = flow_payload()
    changed["steps"][-1]["name"] = "教师终审改名"
    with pytest.raises(Exception) as exc:
        service.create_flow(changed, "academic-affairs")
    assert "不一致" in str(exc.value)


# ---------------- 依赖门禁：只有依赖满足才可领取 ----------------

def test_only_dependencies_satisfied_steps_are_claimable(service):
    inst = start(service)
    assert inst["claimable_codes"] == ["read"]
    claimed = service.claim(inst["id"], "student-a", None)
    assert claimed["step_code"] == "read" and claimed["status"] == "running"
    # read 尚未完成，calc 不能领取。
    assert service.list_claimable(inst["id"]) == []
    service.complete(inst["id"], "read", "student-a", {"drawing": "ok"}, "")
    view = service.get_instance(inst["id"])
    assert node(view, "calc")["claimable"] is True
    assert node(view, "sim")["status"] == "waiting_deps"
    assert [item["step_code"] for item in service.list_claimable(inst["id"])] == ["calc"]


def test_cannot_claim_upstream_not_ready(service):
    inst = start(service)
    with pytest.raises(Exception) as exc:
        service.claim(inst["id"], "student-a", ["review"])
    assert "可领取" in str(exc.value)


def test_diamond_join_waits_for_all_parents(service):
    payload = flow_payload(flow_code="diamond")
    # read -> calc, read -> sim；calc+sim -> review（双依赖汇合）
    payload["steps"][2]["depends_on"] = ["read"]
    payload["steps"][3]["depends_on"] = ["calc", "sim"]
    service.create_flow(payload, "academic-affairs")
    inst = service.start_instance(payload["flow_code"], "k-1")
    service.claim(inst["id"], "a", None)
    service.complete(inst["id"], "read", "a", {}, "")
    claimable = {item["step_code"] for item in service.list_claimable(inst["id"])}
    assert claimable == {"calc", "sim"}
    service.claim(inst["id"], "a", ["calc"])
    service.complete(inst["id"], "calc", "a", {}, "")
    view = service.get_instance(inst["id"])
    assert node(view, "review")["status"] == "waiting_deps"
    service.claim(inst["id"], "b", ["sim"])
    service.complete(inst["id"], "sim", "b", {}, "")
    assert service.list_claimable(inst["id"])[0]["step_code"] == "review"


# ---------------- 等待原因 ----------------

def test_wait_reason_explains_pending_and_blocking(service):
    inst = start(service)
    view = service.get_instance(inst["id"])
    assert node(view, "calc")["wait_reason"].startswith("等待上游 read")
    service.claim(inst["id"], "student-a", None)
    service.fail(inst["id"], "read", "student-a", "尺寸标注读错")
    view = service.get_instance(inst["id"])
    calc = node(view, "calc")
    assert calc["status"] == "waiting_deps"
    assert "failed" in calc["wait_reason"]
    assert calc["blocking_dependencies"][0]["step_code"] == "read"


# ---------------- 上游取消 / 失败 / 重做的策略传播 ----------------

def _policy_flow(service, **overrides):
    policy = {
        "calc": {"on_upstream_failed": "block", "on_upstream_cancelled": "block"},
        "sim": {"on_upstream_failed": "block", "on_upstream_cancelled": "block"},
        "review": {"on_upstream_failed": "block", "on_upstream_cancelled": "block"},
    }
    policy.update(overrides)
    return flow_payload(policy=policy)


def test_failure_blocks_downstream_by_default(service):
    payload = _policy_flow(service, calc={"on_upstream_failed": "block"})
    service.create_flow(payload, "academic-affairs")
    inst = service.start_instance(payload["flow_code"], "k-block")
    service.claim(inst["id"], "a", None)
    service.fail(inst["id"], "read", "a", "读不懂图纸")
    view = service.get_instance(inst["id"])
    assert node(view, "calc")["status"] == "waiting_deps"
    assert service.list_claimable(inst["id"]) == []


def test_cancel_with_skip_policy_cascades(service):
    payload = _policy_flow(service,
                           calc={"on_upstream_cancelled": "skip"},
                           sim={"on_upstream_cancelled": "skip"},
                           review={"on_upstream_cancelled": "skip"})
    service.create_flow(payload, "academic-affairs")
    inst = service.start_instance(payload["flow_code"], "k-skip")
    service.claim(inst["id"], "a", None)
    service.cancel(inst["id"], "read", "teacher", "课程取消")
    view = service.get_instance(inst["id"])
    assert node(view, "calc")["status"] == "skipped"
    assert node(view, "sim")["status"] == "skipped"
    assert node(view, "review")["status"] == "skipped"
    assert view["status"] == "completed"


def test_failure_with_skip_policy_skips_chain(service):
    skip = {"on_upstream_failed": "skip", "on_upstream_cancelled": "skip"}
    payload = _policy_flow(service, calc=skip, sim=skip, review=skip)
    service.create_flow(payload, "academic-affairs")
    inst = service.start_instance(payload["flow_code"], "k-skip-fail")
    service.claim(inst["id"], "a", None)
    service.fail(inst["id"], "read", "a", "图纸缺失")
    view = service.get_instance(inst["id"])
    assert node(view, "calc")["status"] == "skipped"
    assert node(view, "review")["status"] == "skipped"
    assert view["status"] == "aborted"


def test_failure_skip_does_not_leap_when_downstream_blocks_cancel(service):
    # calc 失败自动跳过，但 sim 仅在“取消”时阻断：calc 已 skip（取消类），sim 应保持阻断而非跳过。
    payload = _policy_flow(service,
                           calc={"on_upstream_failed": "skip", "on_upstream_cancelled": "block"},
                           sim={"on_upstream_failed": "skip", "on_upstream_cancelled": "block"},
                           review={"on_upstream_failed": "block", "on_upstream_cancelled": "block"})
    service.create_flow(payload, "academic-affairs")
    inst = service.start_instance(payload["flow_code"], "k-no-leap")
    service.claim(inst["id"], "a", None)
    service.fail(inst["id"], "read", "a", "图纸缺失")
    view = service.get_instance(inst["id"])
    assert node(view, "calc")["status"] == "skipped"
    assert node(view, "sim")["status"] == "waiting_deps"


def test_reopen_policy_invalidates_completed_successor(service):
    # calc 对上游失败采用 reopen：read 失败后 calc 即便已完成也会被收回。
    payload = _policy_flow(service, calc={"on_upstream_failed": "reopen"})
    service.create_flow(payload, "academic-affairs")
    inst = service.start_instance(payload["flow_code"], "k-reopen")
    service.claim(inst["id"], "a", None)
    service.complete(inst["id"], "read", "a", {}, "")
    service.claim(inst["id"], "b", ["calc"])
    service.complete(inst["id"], "calc", "b", {"value": 1}, "")
    # 教师让 read 重做，calc(reopen) 应作废并回到等待。
    service.redo(inst["id"], "read", "teacher", "图纸版本更新")
    view = service.get_instance(inst["id"])
    assert node(view, "read")["status"] == "ready"
    assert node(view, "calc")["status"] == "waiting_deps"
    assert node(view, "calc")["output"] == {}


def test_redo_then_succeed_reopens_chain(service):
    payload = _policy_flow(service,
                           calc={"on_upstream_failed": "reopen"},
                           sim={"on_upstream_failed": "skip"})
    service.create_flow(payload, "academic-affairs")
    inst = service.start_instance(payload["flow_code"], "k-redo-chain")
    service.claim(inst["id"], "a", None)
    service.fail(inst["id"], "read", "a", "临时错误")
    view = service.get_instance(inst["id"])
    # calc 是 reopen：上游失败时回到 waiting；sim 尚未运行保持 waiting。
    assert node(view, "calc")["status"] == "waiting_deps"
    service.redo(inst["id"], "read", "teacher", "允许重做")
    service.claim(inst["id"], "a", None)
    service.complete(inst["id"], "read", "a", {"fixed": True}, "")
    view = service.get_instance(inst["id"])
    assert node(view, "calc")["status"] == "ready"


def test_mixed_policy_block_then_skip(service):
    # calc 阻断，sim 在 calc 失败时跳过，review 对 sim 跳过继续阻断。
    payload = _policy_flow(service,
                           calc={"on_upstream_failed": "block"},
                           sim={"on_upstream_failed": "skip"},
                           review={"on_upstream_cancelled": "skip", "on_upstream_failed": "block"})
    service.create_flow(payload, "academic-affairs")
    inst = service.start_instance(payload["flow_code"], "k-mixed")
    service.claim(inst["id"], "a", None)
    service.complete(inst["id"], "read", "a", {}, "")
    service.claim(inst["id"], "b", ["calc"])
    service.fail(inst["id"], "calc", "b", "超差")
    view = service.get_instance(inst["id"])
    # sim 依赖 calc 失败 -> skip；review 依赖 sim，但 sim 是 skipped（非 failed），
    # review 的取消策略为 skip、失败策略为 block：skipped 归为取消类，应跳过。
    assert node(view, "sim")["status"] == "skipped"
    assert node(view, "review")["status"] == "skipped"


# ---------------- 重启后执行边界不漂移 ----------------

def test_state_survives_service_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "restart.db"
    monkeypatch.setenv("TOWNSHIP_DATABASE_PATH", str(db_path))
    from app.database import close_connection
    close_connection()
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
    svc = TrainingFlowService(get_connection(), clock)
    svc.create_flow(flow_payload(), "academic-affairs")
    inst = svc.start_instance("cnc-training", "persist-1")
    svc.claim(inst["id"], "a", None)
    svc.complete(inst["id"], "read", "a", {"drawing": "v1"}, "")
    close_connection()

    # 模拟服务重启：新建连接与服务实例。
    svc2 = TrainingFlowService(get_connection(), FrozenClock(datetime(2026, 9, 29, 9, 0, tzinfo=UTC)))
    view = svc2.get_instance(inst["id"])
    assert node(view, "read")["status"] == "succeeded"
    assert [c["step_code"] for c in svc2.list_claimable(inst["id"])] == ["calc"]
    # 传播是幂等的：重复查询不产生新的领取边界漂移。
    again = svc2.get_instance(inst["id"])
    assert node(again, "calc")["status"] == "ready"
    assert node(again, "review")["status"] == "waiting_deps"


def test_duplicate_start_reuses_instance(service):
    service.create_flow(flow_payload(), "academic-affairs")
    first = service.start_instance("cnc-training", "same-class")
    second = service.start_instance("cnc-training", "same-class")
    assert first["id"] == second["id"]


def test_full_happy_path_completes(service):
    inst = start(service)
    for code, worker in [("read", "s1"), ("calc", "s2"), ("sim", "s3"), ("review", "t1")]:
        service.claim(inst["id"], worker, [code])
        service.complete(inst["id"], code, worker, {}, "")
    view = service.get_instance(inst["id"])
    assert view["status"] == "completed"
    assert view["claimable_codes"] == []


# ---------------- HTTP 接口 ----------------

def _flow_json(flow_code: str = "api-flow") -> dict:
    payload = flow_payload(flow_code=flow_code)
    payload["steps"][1]["on_upstream_cancelled"] = "skip"
    payload["steps"][2]["on_upstream_cancelled"] = "skip"
    payload["steps"][3]["on_upstream_cancelled"] = "skip"
    return payload


def test_http_flow_lifecycle_and_wait_reason(client):
    created = client.post("/api/training/flows?actor=teacher", json=_flow_json())
    assert created.status_code == 201, created.text
    assert created.json()["reused"] is False

    # 重复提交复用原流程。
    duplicate = client.post("/api/training/flows?actor=teacher", json=_flow_json())
    assert duplicate.status_code == 201 and duplicate.json()["reused"] is True

    started = client.post("/api/training/flows/api-flow/instances", json={"business_key": "team-7"})
    assert started.status_code == 201, started.text
    instance_id = started.json()["id"]

    detail = client.get(f"/api/training/instances/{instance_id}").json()
    calc = next(n for n in detail["nodes"] if n["step_code"] == "calc")
    assert calc["claimable"] is False
    assert "等待上游 read" in calc["wait_reason"]

    claimed = client.post(f"/api/training/instances/{instance_id}/claim", json={"worker_id": "s1"})
    assert claimed.status_code == 200 and claimed.json()["step_code"] == "read"

    # 上游未完成时领取后继被拒。
    blocked = client.post(f"/api/training/instances/{instance_id}/claim", json={"worker_id": "s2", "step_codes": ["calc"]})
    assert blocked.status_code == 409

    assert client.post(f"/api/training/instances/{instance_id}/nodes/read/complete",
                       json={"worker_id": "s1", "output": {"drawing": "ok"}}).status_code == 200
    claimable = client.get(f"/api/training/instances/{instance_id}/claimable").json()["items"]
    assert [n["step_code"] for n in claimable] == ["calc"]


def test_http_validation_errors(client):
    cyclic = _flow_json("bad-cycle")
    cyclic["steps"][0]["depends_on"] = ["review"]
    assert client.post("/api/training/flows?actor=teacher", json=cyclic).status_code == 422

    dangling = _flow_json("bad-dangling")
    dangling["steps"][1]["depends_on"] = ["read", "missing-step"]
    # pydantic 模型或服务层校验，均映射为 422。
    assert client.post("/api/training/flows?actor=teacher", json=dangling).status_code == 422

    dup = _flow_json("bad-dup")
    dup["steps"].append(dict(dup["steps"][0]))
    assert client.post("/api/training/flows?actor=teacher", json=dup).status_code == 422


def test_http_cancel_skip_cascade(client):
    client.post("/api/training/flows?actor=teacher", json=_flow_json("cancel-flow"))
    instance_id = client.post("/api/training/flows/cancel-flow/instances", json={"business_key": "c1"}).json()["id"]
    client.post(f"/api/training/instances/{instance_id}/claim", json={"worker_id": "s1"})
    cancelled = client.post(f"/api/training/instances/{instance_id}/nodes/read/cancel",
                            json={"actor": "teacher", "reason": "课程取消"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    detail = client.get(f"/api/training/instances/{instance_id}").json()
    assert {n["step_code"]: n["status"] for n in detail["nodes"] if n["step_code"] != "read"} == {
        "calc": "skipped", "sim": "skipped", "review": "skipped",
    }
    skipped_review = next(n for n in detail["nodes"] if n["step_code"] == "review")
    assert "跳过" in skipped_review["wait_reason"]

