from __future__ import annotations

import json
import sqlite3
from collections import defaultdict, deque
from typing import Any

from app.compute.service import digest
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.training.repository import TrainingRepository

# 节点状态机：
# waiting_deps 依赖未全部满足（被阻断/等待中），不可领取
# ready       依赖全部满足，可领取
# running     已被领取执行
# succeeded   成功完成（terminal）
# failed      执行失败（terminal，可重做）
# skipped     因上游未满足按 skip 策略自动跳过（terminal，可重做）
# cancelled   被取消（terminal，可重做）
TERMINAL_STATUSES = {"succeeded", "failed", "skipped", "cancelled"}
UNMET_STATUSES = {"failed", "cancelled", "skipped"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS training_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_code TEXT NOT NULL UNIQUE,
    course_code TEXT NOT NULL,
    name TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS training_flow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id INTEGER NOT NULL REFERENCES training_flows(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    on_upstream_cancelled TEXT NOT NULL DEFAULT 'block' CHECK(on_upstream_cancelled IN ('block','skip','reopen')),
    on_upstream_failed TEXT NOT NULL DEFAULT 'block' CHECK(on_upstream_failed IN ('block','skip','reopen')),
    created_at TEXT NOT NULL,
    UNIQUE(flow_id, code)
);
CREATE TABLE IF NOT EXISTS training_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id INTEGER NOT NULL REFERENCES training_flows(id) ON DELETE RESTRICT,
    business_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','aborted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(flow_id, business_key)
);
CREATE TABLE IF NOT EXISTS training_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES training_instances(id) ON DELETE CASCADE,
    step_id INTEGER NOT NULL REFERENCES training_flow_steps(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting_deps','ready','running','succeeded','failed','skipped','cancelled')),
    available_at TEXT NOT NULL DEFAULT '',
    lease_owner TEXT NOT NULL DEFAULT '',
    claimed_at TEXT NOT NULL DEFAULT '',
    output_json TEXT NOT NULL DEFAULT '{}',
    last_reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(instance_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_training_nodes_status ON training_nodes(instance_id,status,ordinal);
CREATE TABLE IF NOT EXISTS training_node_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES training_instances(id) ON DELETE CASCADE,
    node_id INTEGER NOT NULL REFERENCES training_nodes(id) ON DELETE CASCADE,
    step_code TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    before_status TEXT NOT NULL DEFAULT '',
    after_status TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_training_events_node ON training_node_events(node_id,id);
"""


def ensure_schema(connection: sqlite3.Connection | None = None) -> None:
    (connection or get_connection()).executescript(SCHEMA)


def _canonical_definition(payload: dict[str, Any]) -> dict[str, Any]:
    steps = [
        {
            "code": step["code"],
            "name": step["name"],
            "stage": step.get("stage", ""),
            "depends_on": sorted(dict.fromkeys(step.get("depends_on", []))),
            "on_upstream_cancelled": step.get("on_upstream_cancelled", "block"),
            "on_upstream_failed": step.get("on_upstream_failed", "block"),
        }
        for step in payload["steps"]
    ]
    return {"course_code": payload["course_code"], "name": payload["name"], "steps": steps}


def _validate_references(steps: list[dict[str, Any]]) -> None:
    """拒绝重复步骤编码、自依赖和指向不存在步骤的孤立引用。"""
    codes = [step["code"] for step in steps]
    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    if duplicates:
        raise ValidationError("流程内步骤编码不能重复", context={"duplicates": duplicates})
    known = set(codes)
    for step in steps:
        deps = step.get("depends_on", [])
        if step["code"] in deps:
            raise ValidationError(f"步骤 {step['code']} 不能依赖自身", context={"step": step["code"]})
        unknown = sorted({dep for dep in deps if dep not in known})
        if unknown:
            raise ValidationError(
                f"步骤 {step['code']} 引用了不存在的上游步骤",
                context={"step": step["code"], "unknown": unknown},
            )


def _topological_order(steps: list[dict[str, Any]]) -> list[str]:
    """Kahn 拓扑排序；存在环路时抛出 ValidationError。"""
    deps_by_code: dict[str, set[str]] = {
        step["code"]: set(step.get("depends_on", [])) for step in steps
    }
    dependents: dict[str, set[str]] = defaultdict(set)
    for code, deps in deps_by_code.items():
        for dep in deps:
            dependents[dep].add(code)
    indegree = {code: len(deps) for code, deps in deps_by_code.items()}
    # 按声明顺序入队，保证拓扑顺序稳定，执行边界不漂移。
    queue = deque(step["code"] for step in steps if indegree[step["code"]] == 0)
    ordered: list[str] = []
    while queue:
        code = queue.popleft()
        ordered.append(code)
        for dependent in sorted(dependents[code]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if len(ordered) != len(deps_by_code):
        cycle = _find_cycle(deps_by_code)
        raise ValidationError("步骤依赖存在环路，无法写入流程", context={"cycle": cycle})
    return ordered


def _find_cycle(deps_by_code: dict[str, set[str]]) -> list[str]:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(code: str) -> list[str] | None:
        state[code] = 1
        stack.append(code)
        for dep in sorted(deps_by_code.get(code, set())):
            if state.get(dep, 0) == 0:
                found = visit(dep)
                if found:
                    return found
            elif state[dep] == 1:
                start = stack.index(dep)
                return stack[start:] + [dep]
        stack.pop()
        state[code] = 2
        return None

    for node in sorted(deps_by_code):
        if state.get(node, 0) == 0:
            found = visit(node)
            if found:
                return found
    return []


class TrainingFlowService:
    """数控实训流程：一次性提交步骤关系、依赖门禁领取、上游事件传播。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema(self.connection)
        self.repository = TrainingRepository(self.connection)

    # ---------- 流程定义 ----------

    def create_flow(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        canonical = _canonical_definition(payload)
        steps = canonical["steps"]
        _validate_references(steps)
        order = _topological_order(steps)  # 拒绝环路
        order_index = {code: idx for idx, code in enumerate(order)}
        steps.sort(key=lambda item: order_index[item["code"]])
        definition_digest = digest(canonical)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = TrainingRepository(connection)
            existing = repository.flow_by_code(payload["flow_code"])
            if existing is not None:
                # 重复提交：定义一致则复用原流程，否则拒绝。
                if existing["definition_digest"] != definition_digest:
                    raise ConflictError("流程编码已存在且步骤定义不一致，不能覆盖")
                return self._flow_detail(repository, existing["id"], reused=True)
            flow = repository.insert_flow(
                flow_code=payload["flow_code"], course_code=canonical["course_code"],
                name=canonical["name"], definition_digest=definition_digest,
                created_by=actor, now=now,
            )
            repository.insert_steps(flow["id"], steps, now)
            return self._flow_detail(repository, flow["id"], reused=False)

    def get_flow(self, flow_code: str) -> dict[str, Any]:
        flow = self.repository.flow_by_code(flow_code)
        if flow is None:
            raise NotFoundError("实训流程不存在")
        return self._flow_detail(self.repository, flow["id"], reused=False)

    @staticmethod
    def _flow_detail(repository: TrainingRepository, flow_id: int, *, reused: bool) -> dict[str, Any]:
        flow = dict(repository.flow_by_id(flow_id))
        step_rows = repository.list_steps(flow_id)
        steps: list[dict[str, Any]] = []
        for row in step_rows:
            item = dict(row)
            item["depends_on"] = json.loads(item.pop("depends_on_json"))
            steps.append(item)
        flow["steps"] = steps
        flow["reused"] = reused
        return flow

    # ---------- 流程实例 ----------

    def start_instance(self, flow_code: str, business_key: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = TrainingRepository(connection)
            flow = repository.flow_by_code(flow_code)
            if flow is None:
                raise NotFoundError("实训流程不存在")
            existing = repository.instance_by_business_key(flow["id"], business_key)
            if existing is not None:
                # 重复启动复用原实例，不另起执行边界。
                return self.instance_view(existing["id"], repository=repository)
            instance = repository.insert_instance(flow_id=flow["id"], business_key=business_key, now=now)
            steps = repository.list_steps(flow["id"])
            repository.insert_nodes(instance["id"], steps, now)
            return self.instance_view(instance["id"], repository=repository)

    def get_instance(self, instance_id: int) -> dict[str, Any]:
        return self.instance_view(instance_id, repository=self.repository)

    def list_claimable(self, instance_id: int, step_codes: list[str] | None = None) -> list[dict[str, Any]]:
        with transaction(immediate=True) as connection:
            repository = TrainingRepository(connection)
            self._require_instance(repository, instance_id)
            self._propagate(repository, instance_id)
            return repository.claimable_nodes(instance_id, step_codes)

    # ---------- 节点生命周期 ----------

    def claim(self, instance_id: int, worker_id: str, step_codes: list[str] | None) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = TrainingRepository(connection)
            self._require_instance(repository, instance_id)
            self._propagate(repository, instance_id)
            candidates = repository.claimable_nodes(instance_id, step_codes)
            if not candidates:
                raise ConflictError("当前没有依赖全部满足、可领取的步骤")
            node = candidates[0]
            cursor = connection.execute(
                "UPDATE training_nodes SET status='running',lease_owner=?,claimed_at=?,updated_at=?,version=version+1 "
                "WHERE id=? AND status='ready'",
                (worker_id, now, now, node["id"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("步骤已被其他执行者领取或状态已变化")
            self._event(connection, instance_id, node["id"], node["step_code"], "claimed", worker_id, "领取步骤", "ready", "running", now)
            return self._node_view(connection, instance_id, node["step_code"])

    def complete(self, instance_id: int, code: str, worker_id: str, output: dict[str, Any], note: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = TrainingRepository(connection)
            node = self._require_node(repository, instance_id, code)
            self._require_owner(node, worker_id)
            self._set_status(connection, repository, instance_id, node, "succeeded", now,
                            event_type="completed", actor=worker_id, reason=note or "步骤完成",
                            output=output)
            self._propagate(repository, instance_id)
            return self._node_view(connection, instance_id, code)

    def fail(self, instance_id: int, code: str, worker_id: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = TrainingRepository(connection)
            node = self._require_node(repository, instance_id, code)
            self._require_owner(node, worker_id)
            self._set_status(connection, repository, instance_id, node, "failed", now,
                            event_type="failed", actor=worker_id, reason=reason)
            self._propagate(repository, instance_id)
            return self._node_view(connection, instance_id, code)

    def cancel(self, instance_id: int, code: str, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = TrainingRepository(connection)
            node = self._require_node(repository, instance_id, code)
            if node["status"] not in {"ready", "waiting_deps", "running"}:
                raise ConflictError(f"步骤当前状态 {node['status']} 不允许取消")
            self._set_status(connection, repository, instance_id, node, "cancelled", now,
                            event_type="cancelled", actor=actor, reason=reason,
                            clear_lease=True)
            self._propagate(repository, instance_id)
            return self._node_view(connection, instance_id, code)

    def release(self, instance_id: int, code: str, worker_id: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = TrainingRepository(connection)
            node = self._require_node(repository, instance_id, code)
            self._require_owner(node, worker_id)
            target_status = "ready" if self._deps_satisfied(repository, instance_id, node) else "waiting_deps"
            self._set_status(connection, repository, instance_id, node, target_status, now,
                            event_type="released", actor=worker_id, reason=reason,
                            clear_lease=True)
            self._propagate(repository, instance_id)
            return self._node_view(connection, instance_id, code)

    def redo(self, instance_id: int, code: str, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = TrainingRepository(connection)
            node = self._require_node(repository, instance_id, code)
            if node["status"] in {"running", "ready"}:
                raise ConflictError(f"步骤当前状态 {node['status']} 无需重做")
            # 先令后继节点失效，再把目标节点本身放回可领取状态。
            self._invalidate_descendants(connection, repository, instance_id, node["step_code"], actor, now)
            target_status = "ready" if self._deps_satisfied(repository, instance_id, node) else "waiting_deps"
            self._set_status(connection, repository, instance_id, node, target_status, now,
                            event_type="redone", actor=actor, reason=reason,
                            clear_lease=True, clear_output=True)
            self._propagate(repository, instance_id)
            return self._node_view(connection, instance_id, code)

    # ---------- 依赖传播（确定性、可恢复） ----------

    def _propagate(self, repository: TrainingRepository, instance_id: int) -> None:
        """按拓扑序自上而下重算节点状态直到稳定；纯持久化、重启后不漂移。"""
        steps = repository.nodes(instance_id)
        nodes = {item["step_code"]: item for item in steps}
        order = self._order_codes(steps)
        # 多轮直到无变化，处理 skip 的链式传播。
        for _ in range(len(order) + 1):
            changed = False
            for code in order:
                node = nodes[code]
                deps = json.loads(node["depends_on_json"])
                if not deps:
                    continue
                dep_nodes = [nodes[dep] for dep in deps]
                new_status = self._derive_status(node, dep_nodes)
                if new_status is not None and new_status != node["status"]:
                    event_type, reason = self._transition_event(node["status"], new_status, dep_nodes)
                    self._apply_derived(repository.connection, instance_id, node, new_status, event_type, reason)
                    node["status"] = new_status
                    changed = True
            if not changed:
                return
        raise ConflictError("依赖状态传播未能收敛")  # 理论不可达（DAG 已校验）

    @staticmethod
    def _derive_status(node: dict[str, Any], dep_nodes: list[dict[str, Any]]) -> str | None:
        status = node["status"]
        if all(dep["status"] == "succeeded" for dep in dep_nodes):
            # 上游全部成功：放行被阻断或曾被跳过的节点。
            return "ready" if status in {"waiting_deps", "skipped"} else None
        failed = [dep for dep in dep_nodes if dep["status"] == "failed"]
        unmet = [dep for dep in dep_nodes if dep["status"] in UNMET_STATUSES]
        if unmet:
            policy = node["on_upstream_failed"] if failed else node["on_upstream_cancelled"]
            if policy == "skip":
                return "skipped" if status in {"waiting_deps", "ready", "running"} else None
            if policy == "reopen":
                if status == "succeeded":
                    return "waiting_deps"  # 失效已完成结果，等上游重做成功后自动放行
                if status in {"ready", "running"}:
                    return "waiting_deps"
                return None
            # block：阻断尚未完成的节点，已完成/终态节点保留。
            if status in {"waiting_deps", "ready", "running"}:
                return "waiting_deps"
            return None
        # 上游仍在执行（ready/running/waiting_deps）：收回尚未执行节点的可领取资格。
        if status == "ready":
            return "waiting_deps"
        return None

    def _invalidate_descendants(self, repository_conn: sqlite3.Connection, repository: TrainingRepository,
                                instance_id: int, root_code: str, actor: str, now: str) -> None:
        """重做某节点时，其全部后继当前都不再满足依赖：收回领取并作废已产出的结果。

        后继最终是阻断、跳过还是重新开放，由随后的 `_propagate` 按课程策略解释。
        """
        nodes = {item["step_code"]: item for item in repository.nodes(instance_id)}
        children: dict[str, list[str]] = defaultdict(list)
        for code, node in nodes.items():
            for dep in json.loads(node["depends_on_json"]):
                children[dep].append(code)
        queue = deque(children.get(root_code, []))
        seen: set[str] = set()
        while queue:
            code = queue.popleft()
            if code in seen:
                continue
            seen.add(code)
            node = nodes[code]
            if node["status"] != "waiting_deps":
                produced = node["status"] in {"succeeded", "skipped"}
                self._set_status(repository_conn, repository, instance_id, node, "waiting_deps", now,
                                 event_type="reopened", actor=actor,
                                 reason=f"上游步骤 {root_code} 重做，后继结果作废并重新等待依赖",
                                 clear_lease=True, clear_output=produced)
                node["status"] = "waiting_deps"
            queue.extend(children.get(code, []))

    @staticmethod
    def _transition_event(before: str, after: str, dep_nodes: list[dict[str, Any]]) -> tuple[str, str]:
        if after == "ready":
            return ("unblocked", "上游依赖全部满足，进入可领取状态") if before == "waiting_deps" else \
                   ("unskipped", "上游已重做完成，撤销自动跳过，重新可领取")
        if after == "skipped":
            culprit = next((dep for dep in dep_nodes if dep["status"] in UNMET_STATUSES), dep_nodes[0])
            return "skipped", f"上游步骤 {culprit['step_code']} 状态为 {culprit['status']}，按课程策略自动跳过"
        if after == "waiting_deps":
            if before == "running":
                return "blocked", "上游未满足，收回执行中的步骤"
            pending = [dep for dep in dep_nodes if dep["status"] not in UNMET_STATUSES and dep["status"] != "succeeded"]
            if pending:
                names = "、".join(f"{dep['step_code']}({dep['status']})" for dep in pending)
                return "blocked", f"等待上游步骤完成：{names}"
            culprit = next((dep for dep in dep_nodes if dep["status"] in UNMET_STATUSES), None)
            if culprit is not None:
                return "reopened", f"上游步骤 {culprit['step_code']} 未满足（{culprit['status']}），按策略阻断/重新开放"
            return "blocked", "依赖尚未全部满足"
        return "blocked", "依赖状态变化"

    # ---------- 视图与等待原因 ----------

    def instance_view(self, instance_id: int, *, repository: TrainingRepository) -> dict[str, Any]:
        instance = repository.instance_by_id(instance_id)
        if instance is None:
            raise NotFoundError("实训流程实例不存在")
        nodes = repository.nodes(instance_id)
        by_code = {node["step_code"]: node for node in nodes}
        node_views: list[dict[str, Any]] = []
        counts: dict[str, int] = defaultdict(int)
        for node in nodes:
            view = self._node_dict(node, by_code)
            node_views.append(view)
            counts[node["status"]] += 1
        edges = [
            {"source": dep, "target": node["step_code"]}
            for node in nodes for dep in json.loads(node["depends_on_json"])
        ]
        result = dict(instance)
        result.update({
            "nodes": node_views,
            "edges": edges,
            "claimable_codes": [node["step_code"] for node in nodes if node["status"] == "ready"],
            "status_counts": dict(counts),
        })
        if not any(node["status"] not in TERMINAL_STATUSES for node in nodes):
            result["status"] = "completed" if all(node["status"] in {"succeeded", "skipped", "cancelled"} for node in nodes) else "aborted"
        return result

    @staticmethod
    def _node_dict(node: dict[str, Any], by_code: dict[str, dict[str, Any]]) -> dict[str, Any]:
        deps = json.loads(node["depends_on_json"])
        blocking: list[dict[str, Any]] = []
        for dep_code in deps:
            dep = by_code.get(dep_code)
            if dep is None or dep["status"] == "succeeded":
                continue
            if dep["status"] in UNMET_STATUSES:
                policy = node["on_upstream_failed"] if dep["status"] == "failed" else node["on_upstream_cancelled"]
                relation = "unmet"
            else:
                policy = ""
                relation = "pending"
            blocking.append({
                "step_code": dep_code,
                "upstream_status": dep["status"] if dep else "missing",
                "relation": relation,
                "policy": policy,
            })
        wait_reason = TrainingFlowService._wait_reason(node, blocking)
        return {
            "step_code": node["step_code"],
            "step_name": node["step_name"],
            "stage": node["stage"],
            "status": node["status"],
            "claimable": node["status"] == "ready",
            "lease_owner": node["lease_owner"],
            "available_at": node["available_at"],
            "depends_on": deps,
            "blocking_dependencies": blocking,
            "wait_reason": wait_reason,
            "last_reason": node["last_reason"],
            "output": json.loads(node["output_json"]) if node["output_json"] else {},
            "version": node["version"],
        }

    @staticmethod
    def _wait_reason(node: dict[str, Any], blocking: list[dict[str, Any]]) -> str | None:
        status = node["status"]
        if status == "ready":
            return None
        if status == "running":
            return f"已由 {node['lease_owner']} 领取执行"
        if status == "succeeded":
            return None
        if status == "failed":
            return node["last_reason"] or "步骤执行失败，等待重做"
        if status == "cancelled":
            return node["last_reason"] or "步骤已取消"
        if status == "skipped":
            unmet = [item for item in blocking if item["relation"] == "unmet"]
            if unmet:
                item = unmet[0]
                policy_text = {"block": "阻断", "skip": "跳过", "reopen": "重新开放"}.get(item["policy"], item["policy"])
                return f"上游 {item['step_code']} {item['upstream_status']}，按策略（{policy_text}）链路跳过本步骤"
            return node["last_reason"] or "已被自动跳过"
        # waiting_deps
        unmet = [item for item in blocking if item["relation"] == "unmet"]
        pending = [item for item in blocking if item["relation"] == "pending"]
        parts: list[str] = []
        if unmet:
            item = unmet[0]
            policy_text = {"block": "阻断", "skip": "跳过", "reopen": "重新开放"}.get(item["policy"], item["policy"])
            parts.append(f"上游 {item['step_code']} {item['upstream_status']}，按策略{policy_text}，暂不开放")
        if pending:
            parts.append("等待上游 " + "、".join(f"{d['step_code']}({d['upstream_status']})" for d in pending) + " 完成")
        return "；".join(parts) if parts else "依赖尚未全部满足"

    def _node_view(self, connection: sqlite3.Connection, instance_id: int, code: str) -> dict[str, Any]:
        repository = TrainingRepository(connection)
        nodes = repository.nodes(instance_id)
        by_code = {item["step_code"]: item for item in nodes}
        return self._node_dict(by_code[code], by_code)

    # ---------- 基础设施 ----------

    @staticmethod
    def _order_codes(node_rows: list[dict[str, Any]]) -> list[str]:
        deps = {row["step_code"]: json.loads(row["depends_on_json"]) for row in node_rows}
        order: list[str] = []
        visited: set[str] = set()

        def visit(code: str) -> None:
            if code in visited:
                return
            visited.add(code)
            for dep in sorted(deps.get(code, [])):
                visit(dep)
            order.append(code)

        for code in sorted(deps):
            visit(code)
        return order

    def _deps_satisfied(self, repository: TrainingRepository, instance_id: int, node: dict[str, Any]) -> bool:
        nodes = {item["step_code"]: item for item in repository.nodes(instance_id)}
        deps = json.loads(node["depends_on_json"])
        return all(nodes[dep]["status"] == "succeeded" for dep in deps)

    @staticmethod
    def _require_instance(repository: TrainingRepository, instance_id: int) -> None:
        if repository.instance_by_id(instance_id) is None:
            raise NotFoundError("实训流程实例不存在")

    @staticmethod
    def _require_node(repository: TrainingRepository, instance_id: int, code: str) -> dict[str, Any]:
        node = repository.node_by_code(instance_id, code)
        if node is None:
            raise NotFoundError("步骤不存在")
        return dict(node)

    @staticmethod
    def _require_owner(node: dict[str, Any], worker_id: str) -> None:
        if node["status"] != "running":
            raise ConflictError(f"步骤当前状态 {node['status']}，未被领取执行")
        if node["lease_owner"] != worker_id:
            raise ConflictError("步骤未由当前执行者持有")

    def _set_status(self, connection: sqlite3.Connection, repository: TrainingRepository, instance_id: int,
                    node: dict[str, Any], new_status: str, now: str, *, event_type: str, actor: str,
                    reason: str, output: dict[str, Any] | None = None,
                    clear_lease: bool = False, clear_output: bool = False) -> None:
        sets = ["status=?", "updated_at=?", "version=version+1", "last_reason=?"]
        params: list[Any] = [new_status, now, reason]
        if new_status == "ready":
            sets.append("available_at=?")
            params.append(now)
        else:
            sets.append("available_at=''")
        if clear_lease or new_status != "running":
            sets.append("lease_owner=''")
            sets.append("claimed_at=''")
        if output is not None:
            sets.append("output_json=?")
            params.append(json.dumps(output, ensure_ascii=False, sort_keys=True))
        if clear_output:
            sets.append("output_json='{}'")
        params.append(node["id"])
        before = node["status"]
        connection.execute(f"UPDATE training_nodes SET {','.join(sets)} WHERE id=?", tuple(params))
        self._event(connection, instance_id, node["id"], node["step_code"], event_type, actor, reason, before, new_status, now)

    def _apply_derived(self, connection: sqlite3.Connection, instance_id: int, node: dict[str, Any],
                       new_status: str, event_type: str, reason: str) -> None:
        now = to_storage(self.clock.now())
        sets = ["status=?", "updated_at=?", "version=version+1", "last_reason=?"]
        params: list[Any] = [new_status, now, reason]
        if new_status == "ready":
            sets.append("available_at=?")
            params.append(now)
        else:
            sets.append("available_at=''")
        if new_status not in {"running"}:
            sets.append("lease_owner=''")
            sets.append("claimed_at=''")
        if new_status in {"waiting_deps", "skipped"}:
            sets.append("output_json='{}'")
        params.append(node["id"])
        connection.execute(f"UPDATE training_nodes SET {','.join(sets)} WHERE id=?", tuple(params))
        self._event(connection, instance_id, node["id"], node["step_code"], event_type, "", reason, node["status"], new_status, now)

    @staticmethod
    def _event(connection: sqlite3.Connection, instance_id: int, node_id: int, code: str,
               event_type: str, actor: str, reason: str, before: str, after: str, now: str) -> None:
        connection.execute(
            "INSERT INTO training_node_events(instance_id,node_id,step_code,event_type,actor,reason,before_status,after_status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (instance_id, node_id, code, event_type, actor, reason, before, after, now),
        )
