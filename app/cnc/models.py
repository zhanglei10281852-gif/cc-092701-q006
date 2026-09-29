from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import ValidationError


@dataclass(frozen=True, slots=True)
class Step:
    code: str
    name: str
    max_attempts: int = 1


@dataclass(frozen=True, slots=True)
class Policy:
    on_fail: str = "block"
    on_cancel: str = "block"
    on_redo: str = "reopen"

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "Policy":
        payload = payload or {}
        return cls(
            on_fail=str(payload.get("on_fail", "block")),
            on_cancel=str(payload.get("on_cancel", "block")),
            on_redo=str(payload.get("on_redo", "reopen")),
        )


@dataclass(frozen=True, slots=True)
class FlowDefinition:
    flow_code: str
    name: str
    steps: tuple[Step, ...]
    edges: tuple[tuple[str, str], ...]  # (upstream, downstream)
    policy: Policy
    created_by: str = ""
    # 上游 -> 下游
    children: dict[str, tuple[str, ...]] = field(default_factory=dict, compare=False)
    # 下游 -> 上游
    parents: dict[str, tuple[str, ...]] = field(default_factory=dict, compare=False)
    order: tuple[str, ...] = field(default_factory=(), compare=False)

    @property
    def step_map(self) -> dict[str, Step]:
        return {step.code: step for step in self.steps}


def build_definition(payload: dict[str, Any]) -> FlowDefinition:
    """校验原始提交并构造不可变流程定义。

    写入前拒绝：重复步骤、自依赖、重复依赖、孤立引用、环路。
    """
    steps_raw = payload.get("steps") or []
    if not steps_raw:
        raise ValidationError("流程至少包含一个步骤")

    seen: set[str] = set()
    steps: list[Step] = []
    for item in steps_raw:
        code = str(item.get("code", "")).strip()
        if not code:
            raise ValidationError("步骤编码不能为空")
        if code in seen:
            raise ValidationError(f"步骤编码重复：{code}", context={"step": code})
        seen.add(code)
        steps.append(Step(code=code, name=str(item.get("name", "")).strip(), max_attempts=int(item.get("max_attempts", 1))))

    known = set(seen)
    edges: list[tuple[str, str]] = []
    edge_seen: set[tuple[str, str]] = set()
    for dep in payload.get("dependencies") or []:
        upstream = str(dep.get("depends_on", "")).strip()
        downstream = str(dep.get("step", "")).strip()
        if upstream not in known:
            raise ValidationError(f"依赖引用了不存在的上游步骤：{upstream}", context={"step": upstream})
        if downstream not in known:
            raise ValidationError(f"依赖引用了不存在的下游步骤：{downstream}", context={"step": downstream})
        if upstream == downstream:
            raise ValidationError(f"步骤不能依赖自身：{downstream}", context={"step": downstream})
        edge = (upstream, downstream)
        if edge in edge_seen:
            raise ValidationError(f"重复的步骤关系：{upstream} -> {downstream}", context={"edge": list(edge)})
        edge_seen.add(edge)
        edges.append(edge)

    order = _topological_order(known, edges)

    parents: dict[str, list[str]] = {code: [] for code in known}
    children: dict[str, list[str]] = {code: [] for code in known}
    for upstream, downstream in edges:
        parents[downstream].append(upstream)
        children[upstream].append(downstream)

    return FlowDefinition(
        flow_code=str(payload["flow_code"]).strip(),
        name=str(payload["name"]).strip(),
        steps=tuple(steps),
        edges=tuple(edges),
        policy=Policy.from_payload(payload.get("policy")),
        created_by=str(payload.get("created_by") or ""),
        children={code: tuple(values) for code, values in children.items()},
        parents={code: tuple(values) for code, values in parents.items()},
        order=tuple(order),
    )


def _topological_order(nodes: set[str], edges: list[tuple[str, str]]) -> list[str]:
    indegree = {code: 0 for code in nodes}
    adjacency: dict[str, list[str]] = {code: [] for code in nodes}
    for upstream, downstream in edges:
        adjacency[upstream].append(downstream)
        indegree[downstream] += 1

    # 稳定排序：每次取编号最小的入度为零节点，保证执行边界确定、可复现
    ready = sorted(code for code, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        code = ready.pop(0)
        order.append(code)
        for child in adjacency[code]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort()

    if len(order) != len(nodes):
        cyclic = sorted(code for code, degree in indegree.items() if degree > 0)
        raise ValidationError("步骤关系中存在环路", context={"steps": cyclic})
    return order


def definition_canonical(definition: FlowDefinition) -> dict[str, Any]:
    """与提交顺序无关的规范化结构，用于计算幂等摘要。"""
    return {
        "flow_code": definition.flow_code,
        "name": definition.name,
        "steps": [
            {"code": step.code, "name": step.name, "max_attempts": step.max_attempts}
            for step in sorted(definition.steps, key=lambda s: s.code)
        ],
        "dependencies": [
            {"depends_on": upstream, "step": downstream}
            for upstream, downstream in sorted(definition.edges)
        ],
        "policy": {
            "on_fail": definition.policy.on_fail,
            "on_cancel": definition.policy.on_cancel,
            "on_redo": definition.policy.on_redo,
        },
    }


def definition_digest(definition: FlowDefinition) -> str:
    text = json.dumps(definition_canonical(definition), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
