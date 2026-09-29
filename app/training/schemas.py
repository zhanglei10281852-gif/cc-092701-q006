from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# 依赖未满足（上游取消/失败）时课程策略对后继节点的解释。
OnUnmetPolicy = Literal["block", "skip", "reopen"]


class StepDefinition(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    stage: str = Field(default="", max_length=64)
    # 该步骤依赖的上游步骤编码；空列表表示入口步骤。
    depends_on: list[str] = Field(default_factory=list)
    # 某个上游未满足（取消/失败）时如何解释本步骤：阻断 / 跳过 / 重新开放。
    on_upstream_cancelled: OnUnmetPolicy = "block"
    on_upstream_failed: OnUnmetPolicy = "block"


class FlowSubmit(BaseModel):
    flow_code: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    course_code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    steps: list[StepDefinition] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _validate_shape(self) -> "FlowSubmit":
        codes = [step.code for step in self.steps]
        if len(codes) != len(set(codes)):
            raise ValueError("流程内步骤编码不能重复")
        known = set(codes)
        for step in self.steps:
            unknown = [dep for dep in step.depends_on if dep not in known]
            if unknown:
                raise ValueError(f"步骤 {step.code} 引用了不存在的上游：{', '.join(sorted(set(unknown)))}")
            if step.code in step.depends_on:
                raise ValueError(f"步骤 {step.code} 不能依赖自身")
        return self


class ClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    step_codes: list[str] = Field(default_factory=list, max_length=500)


class InstanceStart(BaseModel):
    business_key: str = Field(min_length=1, max_length=120)


class CompleteRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    output: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=2000)


class FailRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)


class RedoRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)


class CancelRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)


class ReleaseRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="释放未完成领取", max_length=2000)
