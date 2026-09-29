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

课程任务运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。数控实训的读图、计算、模拟、教师复核等步骤编排接口使用 `/api/training` 前缀。

## 数控实训步骤流程

一次实训被拆成若干步骤（如读图 → 计算 → 模拟 → 教师复核），步骤之间的依赖关系通过一次请求整体提交，平台据此做依赖门禁领取与上游事件传播，避免教务员逐项推进时上游不合格、后继仍被领取的问题。

- **一次性提交**：`POST /api/training/flows?actor=...` 同时提交全部步骤及其 `depends_on`。写入前校验并拒绝三类非法定义：步骤编码重复、依赖指向不存在的步骤（孤立引用）、依赖成环；合法定义按拓扑序持久化。同一 `flow_code` 以相同定义重复提交会复用原流程（响应 `reused=true`），定义不一致则返回冲突。
- **依赖门禁领取**：`POST /api/training/instances/{id}/claim` 只会领取依赖**全部成功**的步骤（`ready`），上游仍在执行或未满足时后继保持 `waiting_deps`，不会被领取；支持菱形多依赖汇合。
- **课程策略传播**：每个步骤可声明上游取消/失败时的解释策略 `on_upstream_cancelled`、`on_upstream_failed`，取值为 `block`（阻断后继）、`skip`（自动跳过）、`reopen`（收回并重新开放，已完成结果作废）。上游取消、失败或重做（`/redo`）后，后继节点按策略在同一即时事务内确定性传播。
- **等待原因查询**：`GET /api/training/instances/{id}` 返回每个节点的 `status`、`claimable`、`blocking_dependencies` 与人类可读的 `wait_reason`，说明它在等待哪个上游、按何种策略被阻断或跳过。
- **执行边界不漂移**：节点状态、领取租约与传播事件全部即时落库；传播按拓扑序自顶向下重算并收敛，重启服务后领取边界保持一致，重复启动实例（`business_key` 相同）复用原实例。


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
app/training/      数控实训步骤流程：DAG 校验、依赖门禁领取与上游事件传播
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
