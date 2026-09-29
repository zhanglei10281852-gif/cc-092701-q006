from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.cnc.service import CncFlowService
from app.core.clock import FrozenClock
from app.database import close_connection, get_connection


def chain_payload(flow_code: str = "cnc-mill-101", *, on_fail="block", on_cancel="block", on_redo="reopen", max_attempts=3):
    return {
        "flow_code": flow_code,
        "name": "数控铣削实训",
        "steps": [
            {"code": "read", "name": "读图", "max_attempts": max_attempts},
            {"code": "calc", "name": "计算", "max_attempts": max_attempts},
            {"code": "simulate", "name": "模拟", "max_attempts": max_attempts},
            {"code": "review", "name": "教师复核", "max_attempts": max_attempts},
        ],
        "dependencies": [
            {"step": "calc", "depends_on": "read"},
            {"step": "simulate", "depends_on": "calc"},
            {"step": "review", "depends_on": "simulate"},
        ],
        "policy": {"on_fail": on_fail, "on_cancel": on_cancel, "on_redo": on_redo},
        "created_by": "dean",
    }


def diamond_payload(flow_code="diamond", **policy):
    policy = {"on_fail": "block", "on_cancel": "block", "on_redo": "reopen", **policy}
    return {
        "flow_code": flow_code,
        "name": "分支实训",
        "steps": [
            {"code": "a", "name": "甲"},
            {"code": "b", "name": "乙"},
            {"code": "c", "name": "丙"},
            {"code": "d", "name": "汇"},
        ],
        "dependencies": [
            {"step": "b", "depends_on": "a"},
            {"step": "c", "depends_on": "a"},
            {"step": "d", "depends_on": "b"},
            {"step": "d", "depends_on": "c"},
        ],
        "policy": policy,
    }


@pytest.fixture()
def service(client):
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
    return CncFlowService(get_connection(), clock), clock


def node(run: dict, code: str) -> dict:
    return next(node for node in run["nodes"] if node["step_code"] == code)


# --------------------------------------------------------------- 定义校验
def test_reject_cycle_orphan_and_duplicates(service):
    svc, _ = service
    cyclic = chain_payload("cycle-flow")
    cyclic["dependencies"].append({"step": "read", "depends_on": "review"})
    with pytest.raises(Exception) as exc:
        svc.submit_flow(cyclic)
    assert "环路" in str(exc.value)

    orphan_up = chain_payload("orphan-up")
    orphan_up["dependencies"].append({"step": "calc", "depends_on": "ghost"})
    with pytest.raises(Exception) as exc:
        svc.submit_flow(orphan_up)
    assert "不存在的上游" in str(exc.value)

    orphan_down = chain_payload("orphan-down")
    orphan_down["dependencies"].append({"step": "phantom", "depends_on": "read"})
    with pytest.raises(Exception) as exc:
        svc.submit_flow(orphan_down)
    assert "不存在的下游" in str(exc.value)

    duplicate_step = chain_payload("dup-step")
    duplicate_step["steps"].append({"code": "read", "name": "重复读图"})
    with pytest.raises(Exception) as exc:
        svc.submit_flow(duplicate_step)
    assert "重复" in str(exc.value)

    duplicate_edge = chain_payload("dup-edge")
    duplicate_edge["dependencies"].append({"step": "calc", "depends_on": "read"})
    with pytest.raises(Exception) as exc:
        svc.submit_flow(duplicate_edge)
    assert "重复" in str(exc.value)

    self_edge = chain_payload("self-edge")
    self_edge["dependencies"].append({"step": "read", "depends_on": "read"})
    with pytest.raises(Exception) as exc:
        svc.submit_flow(self_edge)
    assert "自身" in str(exc.value)


def test_submit_flow_persists_topological_order(service):
    svc, _ = service
    view = svc.submit_flow(chain_payload())
    assert view["reused"] is False
    assert view["topological_order"] == ["read", "calc", "simulate", "review"]
    assert len(view["definition_digest"]) == 64


def test_duplicate_submission_reuses_original_flow(service):
    svc, _ = service
    first = svc.submit_flow(chain_payload())
    # 调换步骤与依赖的提交顺序，结构不变
    reordered = chain_payload()
    reordered["steps"] = list(reversed(reordered["steps"]))
    reordered["dependencies"] = list(reversed(reordered["dependencies"]))
    second = svc.submit_flow(reordered)
    assert second["id"] == first["id"]
    assert second["reused"] is True
    assert second["definition_digest"] == first["definition_digest"]


def test_same_code_different_structure_conflicts(service):
    from app.core.errors import ConflictError

    svc, _ = service
    svc.submit_flow(chain_payload("shared-code"))
    changed = chain_payload("shared-code")
    changed["steps"].append({"code": "extra", "name": "附加步骤"})
    changed["dependencies"].append({"step": "extra", "depends_on": "review"})
    with pytest.raises(ConflictError):
        svc.submit_flow(changed)


# --------------------------------------------------------------- 门控与领取
def test_only_dependencies_satisfied_become_claimable(service):
    svc, _ = service
    svc.submit_flow(chain_payload())
    run = svc.start_run("cnc-mill-101", "class-2026-A", "teacher")
    assert node(run, "read")["state"] == "ready"
    assert node(run, "read")["claimable"] is True
    for code in ("calc", "simulate", "review"):
        assert node(run, code)["state"] == "waiting"
        assert node(run, code)["claimable"] is False

    # 不能领取未就绪的步骤
    assert svc.claim(run["run_id"], "student-1", "calc", 60) is None

    claimed = svc.claim(run["run_id"], "student-1", None, 60)
    assert node(claimed, "read")["state"] == "leased"
    # 第二个工作者无法重复领取同一步骤
    assert svc.claim(run["run_id"], "student-2", "read", 60) is None

    completed = svc.complete(run["run_id"], "read", "student-1", {"drawing": "ok"}, "读图完成")
    calc = node(completed, "calc")
    assert calc["state"] == "ready" and calc["claimable"] is True
    assert node(completed, "simulate")["state"] == "waiting"


def test_waiting_reasons_explain_each_node(service):
    svc, clock = service
    svc.submit_flow(diamond_payload())
    run = svc.start_run("diamond", "run-1")
    reasons = {item["step"]: item["reason"] for item in node(run, "b")["waiting_reasons"]}
    assert reasons["a"].startswith("上游步骤已就绪")

    run = svc.claim(run["run_id"], "w", "a", 60)
    reasons = {item["step"]: item["state"] for item in node(run, "b")["waiting_reasons"]}
    assert reasons["a"] == "leased"

    run = svc.complete(run["run_id"], "a", "w", {}, "")
    assert node(run, "b")["state"] == "ready"
    assert node(run, "b")["waiting_reasons"][0]["reason"].startswith("依赖全部满足")


def test_diamond_requires_all_upstreams(service):
    svc, _ = service
    svc.submit_flow(diamond_payload())
    run = svc.start_run("diamond", "run-1")
    run = svc.complete(svc.claim(run["run_id"], "w", "a", 60)["run_id"], "a", "w", {}, "")
    run = svc.complete(svc.claim(run["run_id"], "w", "b", 60)["run_id"], "b", "w", {}, "")
    # 仅 b 成功，c 尚未成功，汇合点 d 仍等待
    assert node(run, "d")["state"] == "waiting"
    waiting = {item["step"]: item["state"] for item in node(run, "d")["waiting_reasons"]}
    assert waiting == {"c": "ready"}
    run = svc.complete(svc.claim(run["run_id"], "w", "c", 60)["run_id"], "c", "w", {}, "")
    assert node(run, "d")["state"] == "ready"
    run = svc.complete(svc.claim(run["run_id"], "w", "d", 60)["run_id"], "d", "w", {}, "")
    assert run["status"] == "completed"


# --------------------------------------------------------------- 失败策略
def test_fail_policy_blocks_downstream(service):
    svc, _ = service
    svc.submit_flow(chain_payload(on_fail="block", max_attempts=1))
    run = svc.start_run("cnc-mill-101", "run-block")
    run = svc.claim(run["run_id"], "w", "read", 60)
    run = svc.fail(run["run_id"], "read", "w", "drawing_error", "读图错误", False)
    assert node(run, "read")["state"] == "failed"
    assert node(run, "calc")["state"] == "waiting"
    assert "阻断" in node(run, "calc")["state_reason"]
    assert run["status"] == "aborted"


def test_fail_policy_skips_downstream(service):
    svc, _ = service
    svc.submit_flow(chain_payload(on_fail="skip", max_attempts=1))
    run = svc.start_run("cnc-mill-101", "run-skip")
    run = svc.claim(run["run_id"], "w", "read", 60)
    run = svc.fail(run["run_id"], "read", "w", "drawing_error", "读图错误", False)
    assert node(run, "calc")["state"] == "skipped"
    assert node(run, "simulate")["state"] == "skipped"
    assert node(run, "review")["state"] == "skipped"
    assert run["status"] == "aborted"


def test_retryable_failure_reopens_step_with_backoff(service):
    svc, clock = service
    svc.submit_flow(chain_payload(max_attempts=3))
    run = svc.start_run("cnc-mill-101", "run-retry")
    run = svc.claim(run["run_id"], "w", "read", 60)
    run = svc.fail(run["run_id"], "read", "w", "transient", "临时错误", True)
    read = node(run, "read")
    assert read["state"] == "ready" and read["attempt_count"] == 1
    assert read["available_at"] > read["updated_at"]
    # 退避窗口内不可领取
    assert svc.claim(run["run_id"], "w2", "read", 60) is None
    clock.advance(seconds=2)
    run = svc.claim(run["run_id"], "w2", "read", 60)
    assert node(run, "read")["lease_owner"] == "w2"


# --------------------------------------------------------------- 取消策略
def test_cancel_block_and_skip(service):
    svc, _ = service
    svc.submit_flow(chain_payload("flow-block", on_cancel="block"))
    run = svc.start_run("flow-block", "r1")
    run = svc.claim(run["run_id"], "w", "read", 60)
    run = svc.cancel(run["run_id"], "read", "teacher", "课程取消")
    assert node(run, "read")["state"] == "cancelled"
    assert node(run, "calc")["state"] == "waiting"
    assert "阻断" in node(run, "calc")["state_reason"]

    svc.submit_flow(chain_payload("flow-skip", on_cancel="skip"))
    run2 = svc.start_run("flow-skip", "r1")
    run2 = svc.claim(run2["run_id"], "w", "read", 60)
    run2 = svc.cancel(run2["run_id"], "read", "teacher", "课程取消")
    assert node(run2, "calc")["state"] == "skipped"
    assert node(run2, "review")["state"] == "skipped"


# --------------------------------------------------------------- 重做策略
def test_redo_reopen_resets_descendants(service):
    svc, _ = service
    svc.submit_flow(chain_payload(on_redo="reopen"))
    run = svc.start_run("cnc-mill-101", "run-redo")
    for code in ("read", "calc", "simulate", "review"):
        run = svc.claim(run["run_id"], "w", code, 60)
        run = svc.complete(run["run_id"], code, "w", {}, "")
    assert run["status"] == "completed"

    run = svc.redo(run["run_id"], "calc", "teacher", "计算参数需返工")
    assert run["status"] == "active"
    assert node(run, "simulate")["state"] == "waiting"
    assert node(run, "review")["state"] == "waiting"
    assert node(run, "read")["state"] == "succeeded"  # 上游不受影响
    # 调和后 calc 的上游 read 已成功，calc 立即重新开放；其后继仍等待
    assert node(run, "calc")["state"] == "ready"
    assert node(run, "simulate")["state"] == "waiting"


def test_redo_block_keeps_descendants(service):
    svc, _ = service
    svc.submit_flow(chain_payload(on_redo="block"))
    run = svc.start_run("cnc-mill-101", "run-redo-block")
    for code in ("read", "calc"):
        run = svc.complete(svc.claim(run["run_id"], "w", code, 60)["run_id"], code, "w", {}, "")
    run = svc.redo(run["run_id"], "read", "teacher", "重新读图")
    assert node(run, "read")["state"] == "ready"
    assert node(run, "calc")["state"] == "succeeded"  # block 策略不收回后继


def test_redo_rejected_when_descendant_leased(service):
    from app.core.errors import ConflictError

    svc, _ = service
    svc.submit_flow(chain_payload(on_redo="reopen"))
    run = svc.start_run("cnc-mill-101", "run-lease")
    run = svc.complete(svc.claim(run["run_id"], "w", "read", 60)["run_id"], "read", "w", {}, "")
    run = svc.claim(run["run_id"], "w", "calc", 60)
    with pytest.raises(ConflictError):
        svc.redo(run["run_id"], "read", "teacher", "读图返工")


# --------------------------------------------------------------- 重启边界
def test_state_persists_across_restart_without_boundary_drift(service):
    svc, clock = service
    svc.submit_flow(chain_payload(max_attempts=2))
    run = svc.start_run("cnc-mill-101", "run-restart")
    run = svc.claim(run["run_id"], "worker-a", "read", 10)

    # 模拟服务重启：关闭并重新打开连接、重建服务实例
    close_connection()
    restarted = CncFlowService(get_connection(), clock)
    view = restarted.get_run(run["run_id"])
    assert node(view, "read")["state"] == "leased"
    assert node(view, "read")["lease_owner"] == "worker-a"
    assert restarted.claim(run["run_id"], "worker-b", None, 10) is None

    # 租约未到期：恢复不改变边界
    clock.advance(seconds=5)
    assert restarted.recover_expired()["recovered"] == []
    assert node(restarted.get_run(run["run_id"]), "read")["state"] == "leased"

    # 租约到期且仍有重试次数：确定地回到 ready，不漂到失败
    clock.advance(seconds=6)
    result = restarted.recover_expired()
    assert result["recovered"] == [{"run_id": run["run_id"], "step": "read"}]
    view = restarted.get_run(run["run_id"])
    assert node(view, "read")["state"] == "ready"
    assert node(view, "calc")["state"] == "waiting"


def test_restart_resubmit_reuses_flow(service):
    svc, _ = service
    first = svc.submit_flow(chain_payload())
    close_connection()
    restarted = CncFlowService(get_connection(), FrozenClock(datetime(2026, 9, 29, 9, 0, tzinfo=UTC)))
    again = restarted.submit_flow(chain_payload())
    assert again["id"] == first["id"] and again["reused"] is True


# --------------------------------------------------------------- HTTP 层
def test_api_rejects_cycle_and_reuses_flow(client):
    payload = chain_payload("api-flow")
    ok = client.post("/api/cnc/flows", json=payload)
    assert ok.status_code == 201, ok.text
    reused = client.post("/api/cnc/flows", json=payload)
    assert reused.status_code == 200 and reused.json()["reused"] is True

    bad = chain_payload("api-cycle")
    bad["dependencies"].append({"step": "read", "depends_on": "review"})
    rejected = client.post("/api/cnc/flows", json=bad)
    assert rejected.status_code == 422
    assert "环路" in rejected.json()["error"]["message"]


def test_api_end_to_end_gating_and_recovery(client):
    client.post("/api/cnc/flows", json=chain_payload("api-e2e", max_attempts=1))
    started = client.post("/api/cnc/flows/api-e2e/runs", json={"run_code": "section-1", "created_by": "teacher"})
    assert started.status_code == 201
    run_id = started.json()["run_id"]

    claim = client.post(f"/api/cnc/runs/{run_id}/claim", json={"worker_id": "s1", "lease_seconds": 60})
    assert claim.json()["run"] is not None
    fail = client.post(
        f"/api/cnc/runs/{run_id}/steps/read/fail",
        json={"worker_id": "s1", "error_code": "bad_drawing", "message": "尺寸标注缺失", "retryable": False},
    )
    assert fail.status_code == 200
    body = fail.json()
    assert next(n["state"] for n in body["nodes"] if n["step_code"] == "read") == "failed"
    assert next(n["state"] for n in body["nodes"] if n["step_code"] == "calc") == "waiting"
    detail = client.get(f"/api/cnc/runs/{run_id}").json()
    actions = [event["action"] for event in detail["events"]]
    assert "step.claim" in actions and "step.fail" in actions
