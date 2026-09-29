from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from app.cnc.models import FlowDefinition, build_definition, definition_canonical, definition_digest
from app.cnc.repository import CncRepository, ensure_schema
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError

# 不允许人工取消的终态（成功后如需返工使用 redo）
_NON_CANCELLABLE = {"succeeded", "failed", "skipped"}


class CncFlowService:
    """数控实训步骤依赖流程：定义提交、领取门控与上游终态传播。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = CncRepository(connection) if connection is not None else None
        ensure_schema()

    def _repo(self, connection: sqlite3.Connection) -> CncRepository:
        return CncRepository(connection)

    # ------------------------------------------------------------------ 定义
    def submit_flow(self, payload: dict[str, Any]) -> dict[str, Any]:
        definition = build_definition(payload)
        digest = definition_digest(definition)
        now = to_storage(self.clock.now())
        with self._transaction() as connection:
            repository = self._repo(connection)
            existing = repository.flow_by_code(definition.flow_code)
            if existing is not None:
                # 重复提交复用原流程；同码不同结构视为冲突
                if existing["definition_digest"] != digest:
                    raise ConflictError(
                        "流程编码已存在且结构不同",
                        context={"flow_code": definition.flow_code, "existing_digest": existing["definition_digest"]},
                    )
                return self._flow_view(repository, existing, reused=True)
            flow_id = repository.insert_flow(
                flow_code=definition.flow_code,
                name=definition.name,
                policy=asdict(definition.policy),
                digest=digest,
                definition=definition_canonical(definition),
                created_by=definition.created_by,
                now=now,
            )
            for seq, code in enumerate(definition.order):
                step = definition.step_map[code]
                repository.insert_step(flow_id, code=code, name=step.name, seq=seq, max_attempts=step.max_attempts)
            for seq, (upstream, downstream) in enumerate(definition.edges):
                repository.insert_edge(flow_id, upstream, downstream, seq)
            row = repository.flow_by_id(flow_id)
            return self._flow_view(repository, row, reused=False)

    def list_flows(self) -> list[dict[str, Any]]:
        repository = self.repository or self._repo(self._require_connection())
        items = repository.list_flows()
        for item in items:
            item["policy"] = _loads(item["policy_json"])
        return items

    def get_flow(self, flow_code: str) -> dict[str, Any]:
        repository = self.repository or self._repo(self._require_connection())
        row = repository.flow_by_code(flow_code)
        if row is None:
            raise NotFoundError("实训流程不存在")
        return self._flow_view(repository, row, reused=False)

    def _flow_view(self, repository: CncRepository, row: sqlite3.Row, *, reused: bool) -> dict[str, Any]:
        definition = _definition_of(row)
        runs = [dict(run) for run in repository.runs_for_flow(row["id"])]
        return {
            "id": row["id"],
            "flow_code": row["flow_code"],
            "name": row["name"],
            "policy": asdict(definition.policy),
            "definition_digest": row["definition_digest"],
            "reused": reused,
            "steps": [
                {"code": step.code, "name": step.name, "max_attempts": step.max_attempts}
                for step in definition.steps
            ],
            "dependencies": [
                {"depends_on": upstream, "step": downstream} for upstream, downstream in definition.edges
            ],
            "topological_order": list(definition.order),
            "runs": runs,
        }

    # ------------------------------------------------------------------ 实例
    def start_run(self, flow_code: str, run_code: str, created_by: str = "") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with self._transaction() as connection:
            repository = self._repo(connection)
            flow = repository.flow_by_code(flow_code)
            if flow is None:
                raise NotFoundError("实训流程不存在")
            if repository.run_by_code(flow["id"], run_code) is not None:
                raise ConflictError("该流程下实训实例编码已存在")
            definition = _definition_of(flow)
            run_id = repository.insert_run(flow["id"], run_code, created_by, now)
            for seq, code in enumerate(definition.order):
                parents = definition.parents[code]
                if parents:
                    repository.insert_run_step(
                        run_id, code=code, state="waiting",
                        reason=f"等待上游：{'、'.join(parents)}", seq=seq, available_at="", now=now,
                    )
                else:
                    repository.insert_run_step(
                        run_id, code=code, state="ready", reason="无依赖，可领取",
                        seq=seq, available_at=now, now=now,
                    )
            repository.add_event(run_id, step_code="", action="run.start", actor=created_by, detail={"run_code": run_code}, now=now)
            return self._run_view(repository, flow, run_id)

    def get_run(self, run_id: int) -> dict[str, Any]:
        repository = self.repository or self._repo(self._require_connection())
        run = repository.run_by_id(run_id)
        if run is None:
            raise NotFoundError("实训实例不存在")
        flow = repository.flow_by_id(run["flow_id"])
        return self._run_view(repository, flow, run_id)

    def _run_view(self, repository: CncRepository, flow: sqlite3.Row, run_id: int) -> dict[str, Any]:
        definition = _definition_of(flow)
        run = repository.run_by_id(run_id)
        now_value = to_storage(self.clock.now())
        rows = repository.run_steps(run_id)
        states = {row["step_code"]: row["state"] for row in rows}
        nodes: list[dict[str, Any]] = []
        for row in rows:
            code = row["step_code"]
            waiting_reasons: list[dict[str, str]] = []
            if row["state"] in {"waiting", "ready", "leased"}:
                waiting_reasons = self._waiting_reasons(definition, states, code, row["state"])
            nodes.append({
                "step_code": code,
                "name": definition.step_map[code].name,
                "state": row["state"],
                "claimable": row["state"] == "ready"
                and bool(row["available_at"]) and row["available_at"] <= now_value,
                "attempt_count": row["attempt_count"],
                "max_attempts": definition.step_map[code].max_attempts,
                "lease_owner": row["lease_owner"],
                "lease_expires_at": row["lease_expires_at"],
                "available_at": row["available_at"],
                "updated_at": row["updated_at"],
                "last_error_code": row["last_error_code"],
                "last_error_message": row["last_error_message"],
                "state_reason": row["state_reason"],
                "waiting_reasons": waiting_reasons,
                "depends_on": list(definition.parents[code]),
            })
        return {
            "run_id": run_id,
            "flow_code": flow["flow_code"],
            "run_code": run["run_code"],
            "status": run["status"],
            "policy": asdict(definition.policy),
            "nodes": nodes,
            "events": repository.events(run_id),
        }

    @staticmethod
    def _waiting_reasons(definition: FlowDefinition, states: dict[str, str], code: str, state: str) -> list[dict[str, str]]:
        reasons: list[dict[str, str]] = []
        for parent in definition.parents[code]:
            pstate = states.get(parent, "waiting")
            if pstate == "succeeded":
                continue
            reasons.append({"step": parent, "state": pstate, "reason": _BLOCK_REASON.get(pstate, f"上游步骤 {parent} 尚未成功（{pstate}）")})
        if state == "ready" and not reasons:
            text = "无依赖，可领取" if not definition.parents[code] else "依赖全部满足，等待领取"
            reasons.append({"step": code, "state": "ready", "reason": text})
        return reasons

    # ------------------------------------------------------------------ 领取
    def claim(self, run_id: int, worker_id: str, step_code: str | None, lease_seconds: int) -> dict[str, Any] | None:
        now_value = self.clock.now()
        now = to_storage(now_value)
        expires = to_storage(now_value + timedelta(seconds=lease_seconds))
        with self._transaction() as connection:
            repository = self._repo(connection)
            run = repository.run_by_id(run_id)
            if run is None:
                raise NotFoundError("实训实例不存在")
            if run["status"] != "active":
                return None
            target: sqlite3.Row | None = None
            if step_code:
                candidate = repository.run_step(run_id, step_code)
                if candidate is None:
                    raise NotFoundError("步骤不存在")
                if candidate["state"] == "ready" and candidate["available_at"] and candidate["available_at"] <= now:
                    target = candidate
            else:
                claimable = repository.claimable_steps(run_id, now)
                target = claimable[0] if claimable else None
            if target is None:
                return None
            connection.execute(
                "UPDATE cnc_run_steps SET state='leased',attempt_count=attempt_count+1,lease_owner=?,lease_expires_at=?,available_at='',updated_at=? WHERE id=? AND state='ready'",
                (worker_id, expires, now, target["id"]),
            )
            repository.add_event(run_id, step_code=target["step_code"], action="step.claim", actor=worker_id,
                                 detail={"lease_seconds": lease_seconds, "lease_expires_at": expires}, now=now)
            flow = repository.flow_by_id(run["flow_id"])
            return self._run_view(repository, flow, run_id)

    def complete(self, run_id: int, step_code: str, worker_id: str, result: dict[str, Any], note: str) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with self._transaction() as connection:
            repository = self._repo(connection)
            run, flow, step = self._load_owned(repository, run_id, step_code, worker_id)
            connection.execute(
                "UPDATE cnc_run_steps SET state='succeeded',state_reason='执行成功',lease_owner='',lease_expires_at='',last_error_code='',last_error_message='',result_json=?,updated_at=? WHERE id=?",
                (_dumps(result), now, step["id"]),
            )
            repository.add_event(run_id, step_code, action="step.complete", actor=worker_id,
                                 detail={"note": note, "result_keys": sorted(result.keys())}, now=now)
            self._reconcile(connection, repository, flow, run_id, now)
            self._refresh_run_status(repository, run_id, now)
            return self._run_view(repository, flow, run_id)

    def fail(self, run_id: int, step_code: str, worker_id: str, error_code: str, message: str, retryable: bool) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with self._transaction() as connection:
            repository = self._repo(connection)
            run, flow, step = self._load_owned(repository, run_id, step_code, worker_id)
            definition = _definition_of(flow)
            max_attempts = definition.step_map[step_code].max_attempts
            can_retry = retryable and int(step["attempt_count"]) < max_attempts
            if can_retry:
                delay = min(300, 2 ** max(0, int(step["attempt_count"]) - 1))
                available = to_storage(now_value + timedelta(seconds=delay))
                connection.execute(
                    "UPDATE cnc_run_steps SET state='ready',state_reason=?,lease_owner='',lease_expires_at='',available_at=?,last_error_code=?,last_error_message=?,updated_at=? WHERE id=?",
                    (f"失败待重试（{error_code}）", available, error_code, message[:2000], now, step["id"]),
                )
                repository.add_event(run_id, step_code, action="step.fail_retry", actor=worker_id,
                                     detail={"error_code": error_code, "retry_after_seconds": delay}, now=now)
            else:
                connection.execute(
                    "UPDATE cnc_run_steps SET state='failed',state_reason=?,lease_owner='',lease_expires_at='',available_at='',last_error_code=?,last_error_message=?,updated_at=? WHERE id=?",
                    (f"失败终态（{error_code}）", error_code, message[:2000], now, step["id"]),
                )
                repository.add_event(run_id, step_code, action="step.fail", actor=worker_id,
                                     detail={"error_code": error_code, "message": message[:500]}, now=now)
                self._reconcile(connection, repository, flow, run_id, now)
            self._refresh_run_status(repository, run_id, now)
            return self._run_view(repository, flow, run_id)

    def cancel(self, run_id: int, step_code: str, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with self._transaction() as connection:
            repository = self._repo(connection)
            run = repository.run_by_id(run_id)
            if run is None:
                raise NotFoundError("实训实例不存在")
            flow = repository.flow_by_id(run["flow_id"])
            step = repository.run_step(run_id, step_code)
            if step is None:
                raise NotFoundError("步骤不存在")
            if step["state"] in _NON_CANCELLABLE:
                raise ConflictError(f"步骤处于 {step['state']} 终态，不能取消；如需返工请使用重做")
            connection.execute(
                "UPDATE cnc_run_steps SET state='cancelled',state_reason=?,lease_owner='',lease_expires_at='',available_at='',updated_at=? WHERE id=?",
                (f"人工取消：{reason}", now, step["id"]),
            )
            repository.add_event(run_id, step_code, action="step.cancel", actor=actor, detail={"reason": reason}, now=now)
            self._reconcile(connection, repository, flow, run_id, now)
            self._refresh_run_status(repository, run_id, now)
            return self._run_view(repository, flow, run_id)

    def redo(self, run_id: int, step_code: str, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with self._transaction() as connection:
            repository = self._repo(connection)
            run = repository.run_by_id(run_id)
            if run is None:
                raise NotFoundError("实训实例不存在")
            flow = repository.flow_by_id(run["flow_id"])
            definition = _definition_of(flow)
            step = repository.run_step(run_id, step_code)
            if step is None:
                raise NotFoundError("步骤不存在")
            if step["state"] == "leased":
                raise ConflictError("步骤正在执行，不能重做")
            policy = definition.policy
            if policy.on_redo == "reopen":
                descendants = _descendants(definition, step_code)
                leased_descendants = [
                    code for code in descendants
                    if (row := repository.run_step(run_id, code)) is not None and row["state"] == "leased"
                ]
                if leased_descendants:
                    raise ConflictError(
                        "后继步骤正在执行，不能重做上游",
                        context={"leased": leased_descendants},
                    )
                # 收回重做节点及其全部后继，统一回到等待态，随后按依赖重新派生
                codes = [step_code, *descendants]
                placeholders = ",".join("?" for _ in codes)
                connection.execute(
                    f"UPDATE cnc_run_steps SET state='waiting',state_reason='上游重做，等待重新派生',lease_owner='',lease_expires_at='',available_at='',last_error_code='',last_error_message='',result_json='{{}}',updated_at=? WHERE run_id=? AND step_code IN ({placeholders})",
                    (now, run_id, *codes),
                )
            else:  # block：仅重做该步骤，后继保持原状，由教师逐个人工处理
                connection.execute(
                    "UPDATE cnc_run_steps SET state='waiting',state_reason='按策略阻断，后继不自动重新开放',lease_owner='',lease_expires_at='',available_at='',last_error_code='',last_error_message='',result_json='{}',updated_at=? WHERE id=?",
                    (now, step["id"]),
                )
            repository.add_event(run_id, step_code, action="step.redo", actor=actor,
                                 detail={"reason": reason, "policy": policy.on_redo}, now=now)
            # 重做节点本身无依赖满足问题之外的就绪判断：若它没有上游则直接就绪
            self._reconcile(connection, repository, flow, run_id, now)
            self._refresh_run_status(repository, run_id, now)
            return self._run_view(repository, flow, run_id)

    def recover_expired(self, actor: str = "recovery-worker") -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        recovered: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        with self._transaction() as connection:
            repository = self._repo(connection)
            for step in repository.expired_leased(now):
                run = repository.run_by_id(step["run_id"])
                flow = repository.flow_by_id(run["flow_id"])
                definition = _definition_of(flow)
                max_attempts = definition.step_map[step["step_code"]].max_attempts
                if int(step["attempt_count"]) < max_attempts:
                    connection.execute(
                        "UPDATE cnc_run_steps SET state='ready',state_reason='租约过期，重新可领取',lease_owner='',lease_expires_at='',available_at=?,updated_at=? WHERE id=?",
                        (now, now, step["id"]),
                    )
                    repository.add_event(step["run_id"], step["step_code"], action="step.lease_recover", actor=actor,
                                         detail={"outcome": "ready"}, now=now)
                    recovered.append({"run_id": step["run_id"], "step": step["step_code"]})
                else:
                    connection.execute(
                        "UPDATE cnc_run_steps SET state='failed',state_reason='租约过期且重试耗尽',lease_owner='',lease_expires_at='',available_at='',last_error_code='lease_expired',last_error_message='工作者租约已过期',updated_at=? WHERE id=?",
                        (now, step["id"]),
                    )
                    repository.add_event(step["run_id"], step["step_code"], action="step.lease_recover", actor=actor,
                                         detail={"outcome": "failed"}, now=now)
                    self._reconcile(connection, repository, flow, step["run_id"], now)
                    self._refresh_run_status(repository, step["run_id"], now)
                    failed.append({"run_id": step["run_id"], "step": step["step_code"]})
        return {"recovered": recovered, "failed": failed}

    # ------------------------------------------------------------------ 内部
    def _reconcile(self, connection: sqlite3.Connection, repository: CncRepository, flow: sqlite3.Row, run_id: int, now: str) -> None:
        """按拓扑顺序重新派生所有 waiting 节点：升级 ready 或按策略 skip/阻断。

        只处理 waiting 节点；ready/leased/终态节点不在此处变更，从而保证
        “依赖全部满足才可领取”的不变量不被破坏。
        """
        definition = _definition_of(flow)
        rows = {row["step_code"]: row for row in repository.run_steps(run_id)}
        for code in definition.order:
            row = rows[code]
            if row["state"] != "waiting":
                continue
            parent_states = [rows[p]["state"] for p in definition.parents[code]]
            target, reason = self._derive(definition, parent_states, code)
            if target == "skipped":
                connection.execute(
                    "UPDATE cnc_run_steps SET state='skipped',state_reason=?,available_at='',updated_at=? WHERE id=?",
                    (reason, now, row["id"]),
                )
                rows[code] = dict(rows[code]) | {"state": "skipped"}
                repository.add_event(run_id, code, action="step.propagate_skip", actor="system", detail={"reason": reason}, now=now)
            elif target == "ready":
                # _derive 仅在所有上游都成功（或本就无上游）时给出 ready
                connection.execute(
                    "UPDATE cnc_run_steps SET state='ready',state_reason=?,available_at=?,updated_at=? WHERE id=?",
                    (reason, now, now, row["id"]),
                )
                rows[code] = dict(rows[code]) | {"state": "ready"}
            else:
                connection.execute("UPDATE cnc_run_steps SET state_reason=?,updated_at=? WHERE id=?", (reason, now, row["id"]))

    @staticmethod
    def _derive(definition: FlowDefinition, parent_states: list[str], code: str) -> tuple[str, str]:
        """返回 (期望状态, 原因)。期望状态仅可能为 ready/skipped/waiting。"""
        policy = definition.policy
        labels = {p: s for p, s in zip(definition.parents[code], parent_states)}
        if any(s == "skipped" for s in parent_states):
            skipped = [p for p, s in labels.items() if s == "skipped"]
            return "skipped", f"上游 {('、'.join(skipped))} 已跳过，后继级联跳过"
        if any(s == "failed" for s in parent_states):
            failed = [p for p, s in labels.items() if s == "failed"]
            if policy.on_fail == "skip":
                return "skipped", f"上游 {('、'.join(failed))} 失败，按课程策略跳过后继"
            return "waiting", f"上游 {('、'.join(failed))} 失败，按课程策略阻断后继"
        if any(s == "cancelled" for s in parent_states):
            cancelled = [p for p, s in labels.items() if s == "cancelled"]
            if policy.on_cancel == "skip":
                return "skipped", f"上游 {('、'.join(cancelled))} 取消，按课程策略跳过后继"
            return "waiting", f"上游 {('、'.join(cancelled))} 取消，按课程策略阻断后继"
        pending = [p for p, s in labels.items() if s != "succeeded"]
        if pending:
            return "waiting", f"等待上游：{'、'.join(pending)}"
        return "ready", "依赖全部满足，可领取"

    def _refresh_run_status(self, repository: CncRepository, run_id: int, now: str) -> None:
        rows = repository.run_steps(run_id)
        states = [row["state"] for row in rows]
        if all(s == "succeeded" for s in states):
            repository.set_run_status(run_id, "completed", now)
        elif not any(s in {"ready", "leased"} for s in states):
            # 没有可领取或执行中的步骤：要么全部终态，要么被失败/取消终态永久阻断
            repository.set_run_status(run_id, "aborted", now)
        else:
            repository.set_run_status(run_id, "active", now)

    @staticmethod
    def _load_owned(repository: CncRepository, run_id: int, step_code: str, worker_id: str):
        run = repository.run_by_id(run_id)
        if run is None:
            raise NotFoundError("实训实例不存在")
        flow = repository.flow_by_id(run["flow_id"])
        step = repository.run_step(run_id, step_code)
        if step is None:
            raise NotFoundError("步骤不存在")
        if step["state"] != "leased" or step["lease_owner"] != worker_id:
            raise ConflictError("步骤未由当前工作者持有，无法回执")
        return run, flow, step

    def _transaction(self):
        from app.database import transaction

        if self.connection is not None:
            return _bound_transaction(self.connection)
        return transaction(immediate=True)

    def _require_connection(self) -> sqlite3.Connection:
        from app.database import get_connection

        if self.connection is None:
            self.connection = get_connection()
            self.repository = CncRepository(self.connection)
        return self.connection


# 工作者持有时的等待原因文案
_BLOCK_REASON = {
    "waiting": "上游步骤仍在等待其依赖",
    "ready": "上游步骤已就绪，等待领取执行",
    "leased": "上游步骤正在执行",
    "failed": "上游步骤失败，按课程策略阻断或跳过",
    "cancelled": "上游步骤已取消，按课程策略阻断或跳过",
    "skipped": "上游步骤已跳过，本步骤无法满足依赖",
}


def _descendants(definition: FlowDefinition, code: str) -> list[str]:
    result: list[str] = []
    stack = list(definition.children.get(code, ()))
    seen: set[str] = set()
    while stack:
        current = stack.pop(0)
        if current in seen:
            continue
        seen.add(current)
        result.append(current)
        stack.extend(definition.children.get(current, ()))
    # 按拓扑序返回，保证先重置近邻再重置远端
    order_index = {c: i for i, c in enumerate(definition.order)}
    return sorted(result, key=order_index.get)


def _dumps(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str) -> dict[str, Any]:
    import json

    return json.loads(value)


def _definition_of(flow: sqlite3.Row) -> FlowDefinition:
    import json

    payload = json.loads(flow["definition_json"])
    payload.setdefault("flow_code", flow["flow_code"])
    payload.setdefault("name", flow["name"])
    return build_definition(payload)


class _bound_transaction:
    """复用外部注入连接时的即时事务上下文。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
