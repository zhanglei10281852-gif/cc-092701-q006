from __future__ import annotations

import json
import sqlite3
from typing import Any

# 运行期状态机：
#   waiting   依赖未满足，不可领取
#   ready     依赖全部满足，可领取
#   leased    已被领取，正在执行
#   succeeded 执行成功（终态）
#   failed    执行失败且重试耗尽（终态）
#   cancelled 上游取消传播或人工取消（终态）
#   skipped   上游终态按策略跳过（终态）
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "skipped"}
CLAIMABLE_STATE = "ready"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cnc_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cnc_flow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id INTEGER NOT NULL REFERENCES cnc_flows(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    seq INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    UNIQUE(flow_id, code)
);
CREATE TABLE IF NOT EXISTS cnc_flow_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id INTEGER NOT NULL REFERENCES cnc_flows(id) ON DELETE CASCADE,
    upstream_code TEXT NOT NULL,
    downstream_code TEXT NOT NULL,
    seq INTEGER NOT NULL,
    UNIQUE(flow_id, upstream_code, downstream_code)
);
CREATE TABLE IF NOT EXISTS cnc_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id INTEGER NOT NULL REFERENCES cnc_flows(id) ON DELETE RESTRICT,
    run_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','aborted')),
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(flow_id, run_code)
);
CREATE TABLE IF NOT EXISTS cnc_run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES cnc_runs(id) ON DELETE CASCADE,
    step_code TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('waiting','ready','leased','succeeded','failed','cancelled','skipped')),
    state_reason TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    available_at TEXT NOT NULL DEFAULT '',
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, step_code)
);
CREATE INDEX IF NOT EXISTS idx_cnc_run_steps_claim ON cnc_run_steps(state, available_at, seq, id);
CREATE TABLE IF NOT EXISTS cnc_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES cnc_runs(id) ON DELETE CASCADE,
    step_code TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cnc_run_events_run ON cnc_run_events(run_id, id);
"""


def ensure_schema() -> None:
    from app.database import get_connection

    get_connection().executescript(SCHEMA)


class CncRepository:
    """封装数控实训流程的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 流程定义 ----
    def flow_by_code(self, flow_code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM cnc_flows WHERE flow_code=?", (flow_code,)).fetchone()

    def flow_by_id(self, flow_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM cnc_flows WHERE id=?", (flow_id,)).fetchone()

    def list_flows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM cnc_flows ORDER BY id").fetchall()]

    def insert_flow(
        self,
        *,
        flow_code: str,
        name: str,
        policy: dict[str, Any],
        digest: str,
        definition: dict[str, Any],
        created_by: str,
        now: str,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO cnc_flows(flow_code,name,policy_json,definition_digest,definition_json,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (flow_code, name, json.dumps(policy, ensure_ascii=False, sort_keys=True), digest,
             json.dumps(definition, ensure_ascii=False, sort_keys=True), created_by, now, now),
        )
        return int(cursor.lastrowid)

    def insert_step(self, flow_id: int, *, code: str, name: str, seq: int, max_attempts: int) -> None:
        self.connection.execute(
            "INSERT INTO cnc_flow_steps(flow_id,code,name,seq,max_attempts) VALUES(?,?,?,?,?)",
            (flow_id, code, name, seq, max_attempts),
        )

    def insert_edge(self, flow_id: int, upstream: str, downstream: str, seq: int) -> None:
        self.connection.execute(
            "INSERT INTO cnc_flow_edges(flow_id,upstream_code,downstream_code,seq) VALUES(?,?,?,?)",
            (flow_id, upstream, downstream, seq),
        )

    def flow_steps(self, flow_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM cnc_flow_steps WHERE flow_id=? ORDER BY seq,id", (flow_id,)).fetchall()

    def flow_edges(self, flow_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM cnc_flow_edges WHERE flow_id=? ORDER BY seq,id", (flow_id,)).fetchall()

    def runs_for_flow(self, flow_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM cnc_runs WHERE flow_id=? ORDER BY id", (flow_id,)).fetchall()

    # ---- 运行实例 ----
    def run_by_id(self, run_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM cnc_runs WHERE id=?", (run_id,)).fetchone()

    def run_by_code(self, flow_id: int, run_code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM cnc_runs WHERE flow_id=? AND run_code=?", (flow_id, run_code)).fetchone()

    def insert_run(self, flow_id: int, run_code: str, created_by: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO cnc_runs(flow_id,run_code,status,created_by,created_at,updated_at) VALUES(?,?,'active',?,?,?)",
            (flow_id, run_code, created_by, now, now),
        )
        return int(cursor.lastrowid)

    def set_run_status(self, run_id: int, status: str, now: str) -> None:
        self.connection.execute("UPDATE cnc_runs SET status=?,updated_at=? WHERE id=?", (status, now, run_id))

    def insert_run_step(self, run_id: int, *, code: str, state: str, reason: str, seq: int, available_at: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO cnc_run_steps(run_id,step_code,state,state_reason,seq,available_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (run_id, code, state, reason, seq, available_at, now),
        )

    def run_steps(self, run_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM cnc_run_steps WHERE run_id=? ORDER BY seq,id", (run_id,)).fetchall()

    def run_step(self, run_id: int, step_code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM cnc_run_steps WHERE run_id=? AND step_code=?", (run_id, step_code)).fetchone()

    def claimable_steps(self, run_id: int, now: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM cnc_run_steps WHERE run_id=? AND state='ready' AND available_at<>'' AND available_at<=? ORDER BY seq,id",
            (run_id, now),
        ).fetchall()

    def expired_leased(self, now: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM cnc_run_steps WHERE state='leased' AND lease_expires_at<>'' AND lease_expires_at<?",
            (now,),
        ).fetchall()

    def add_event(self, run_id: int, step_code: str, *, action: str, actor: str, detail: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO cnc_run_events(run_id,step_code,action,actor,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (run_id, step_code, action, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    def events(self, run_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM cnc_run_events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()]
