from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable


class TrainingRepository:
    """封装实训流程定义、实例节点与事件的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 流程定义 ----

    def flow_by_code(self, flow_code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM training_flows WHERE flow_code=?", (flow_code,)).fetchone()

    def flow_by_id(self, flow_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM training_flows WHERE id=?", (flow_id,)).fetchone()

    def insert_flow(self, *, flow_code: str, course_code: str, name: str, definition_digest: str, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO training_flows(flow_code,course_code,name,definition_digest,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (flow_code, course_code, name, definition_digest, created_by, now, now),
        )
        return dict(self.flow_by_id(cursor.lastrowid))  # type: ignore[arg-type]

    def insert_steps(self, flow_id: int, steps: list[dict[str, Any]], now: str) -> None:
        rows = [
            (
                flow_id,
                step["code"],
                step["name"],
                step.get("stage", ""),
                index,
                json.dumps(list(step.get("depends_on", [])), ensure_ascii=False),
                step.get("on_upstream_cancelled", "block"),
                step.get("on_upstream_failed", "block"),
                now,
            )
            for index, step in enumerate(steps)
        ]
        self.connection.executemany(
            "INSERT INTO training_flow_steps(flow_id,code,name,stage,ordinal,depends_on_json,on_upstream_cancelled,on_upstream_failed,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )

    def list_steps(self, flow_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM training_flow_steps WHERE flow_id=? ORDER BY ordinal,id", (flow_id,)).fetchall()
        return [dict(row) for row in rows]

    # ---- 流程实例 ----

    def insert_instance(self, *, flow_id: int, business_key: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO training_instances(flow_id,business_key,status,created_at,updated_at) VALUES(?,?,'active',?,?)",
            (flow_id, business_key, now, now),
        )
        return dict(self.connection.execute("SELECT * FROM training_instances WHERE id=?", (cursor.lastrowid,)).fetchone())

    def instance_by_id(self, instance_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT i.*, f.flow_code, f.course_code, f.name AS flow_name FROM training_instances i JOIN training_flows f ON f.id=i.flow_id WHERE i.id=?",
            (instance_id,),
        ).fetchone()

    def instance_by_business_key(self, flow_id: int, business_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT i.*, f.flow_code, f.course_code, f.name AS flow_name FROM training_instances i JOIN training_flows f ON f.id=i.flow_id WHERE i.flow_id=? AND i.business_key=?",
            (flow_id, business_key),
        ).fetchone()

    def insert_nodes(self, instance_id: int, steps: Iterable[dict[str, Any]], now: str) -> None:
        rows = [
            (
                instance_id,
                step["id"],
                index,
                "waiting_deps" if json.loads(step["depends_on_json"]) else "ready",
                # 入口步骤立即可领取，其余等待依赖，available_at 留空直到放行。
                now if not json.loads(step["depends_on_json"]) else "",
                now,
                now,
            )
            for index, step in enumerate(steps)
        ]
        self.connection.executemany(
            "INSERT INTO training_nodes(instance_id,step_id,ordinal,status,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            rows,
        )

    # ---- 节点 ----

    _NODE_SELECT = (
        "SELECT n.*, s.code AS step_code, s.name AS step_name, s.stage, s.depends_on_json, "
        "s.on_upstream_cancelled, s.on_upstream_failed "
        "FROM training_nodes n JOIN training_flow_steps s ON s.id=n.step_id "
        "WHERE n.instance_id=?"
    )

    def nodes(self, instance_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(self._NODE_SELECT + " ORDER BY n.ordinal,n.id", (instance_id,)).fetchall()
        return [dict(row) for row in rows]

    def node_by_code(self, instance_id: int, code: str) -> sqlite3.Row | None:
        return self.connection.execute(self._NODE_SELECT + " AND s.code=?", (instance_id, code)).fetchone()

    def claimable_nodes(self, instance_id: int, step_codes: list[str] | None) -> list[dict[str, Any]]:
        sql = self._NODE_SELECT + " AND n.status='ready'"
        params: list[Any] = [instance_id]
        if step_codes:
            placeholders = ",".join("?" for _ in step_codes)
            sql += f" AND s.code IN ({placeholders})"
            params.extend(step_codes)
        sql += " ORDER BY n.ordinal,n.id"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def add_node_event(self, *, instance_id: int, node_id: int, step_code: str, event_type: str, actor: str, reason: str, before: str, after: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO training_node_events(instance_id,node_id,step_code,event_type,actor,reason,before_status,after_status,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (instance_id, node_id, step_code, event_type, actor, reason, before, after, "{}", now),
        )
