# 职业教学任务运营服务

这是一个面向职业院校教务团队、授课教师和课程管理员的 Python 后端服务，用于管理课程任务模板、学员提交、执行队列、教师工作者、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/compute-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

课程任务运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。数控实训步骤依赖流程位于 `/api/cnc`。

## 数控实训步骤流程

一次实训可一次性提交为带依赖关系的步骤图（读图 → 计算 → 模拟 → 教师复核），接口前缀 `/api/cnc`：

- `POST /api/cnc/flows`：一次性提交步骤与依赖。写入前拒绝重复步骤、重复依赖、自依赖、孤立引用与环路；提交顺序无关，按规范化摘要判重——同结构重复提交复用原流程（返回 `200` 且 `reused=true`），同编码不同结构返回 `409`。
- `POST /api/cnc/flows/{flow_code}/runs`：基于已定义流程创建一次实训实例。仅当某步骤的全部上游都成功时它才进入 `ready`（可领取），否则保持 `waiting` 且不可领取；`GET /api/cnc/runs/{run_id}` 为每个节点返回 `waiting_reasons`，逐项说明被哪个上游、以何种状态阻塞。
- `POST /api/cnc/runs/{id}/claim`：按拓扑顺序领取一个就绪步骤（也可指定 `step_code`），通过租约与即时事务保证同一节点不会被并发重复领取。
- `complete / fail / cancel / redo`：上游进入终态后按课程策略解释后继：
  - `policy.on_fail`：`block`（阻断，后继保持等待）或 `skip`（级联跳过）；
  - `policy.on_cancel`：`block` 或 `skip`；
  - `policy.on_redo`：`reopen`（重做节点并收回全部后继重新派生）或 `block`（仅重做该步骤，后继不动）。
  - 可重试失败按退避窗口重新开放，重试耗尽才转终态；`POST /api/cnc/recovery/expired-leases` 处理租约过期。
- 流程定义、节点状态、租约与事件全部持久化到 SQLite；重启服务后实例、租约归属与可领取边界不漂移（恢复边界仅由数据库时间戳与注入时钟决定）。

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/cnc/           数控实训步骤依赖流程：定义校验、领取门控与上游终态传播
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
