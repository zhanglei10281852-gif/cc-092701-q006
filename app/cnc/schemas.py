from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 上游发生取消、失败、重做时，课程策略如何解释后继节点：
# block=阻断  skip=跳过  reopen=重新开放
FailAction = Literal["block", "skip"]
CancelAction = Literal["block", "skip"]
RedoAction = Literal["reopen", "block"]


class CoursePolicy(BaseModel):
    """课程策略：上游步骤出现终态时后继节点的解释方式。"""

    on_fail: FailAction = "block"
    on_cancel: CancelAction = "block"
    on_redo: RedoAction = "reopen"


class StepDefinition(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    max_attempts: int = Field(default=1, ge=1, le=10)


class DependencyDefinition(BaseModel):
    # step 依赖 depends_on（depends_on 是上游步骤）
    step: str = Field(min_length=2, max_length=64)
    depends_on: str = Field(min_length=2, max_length=64)


class FlowSubmit(BaseModel):
    flow_code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=2, max_length=120)
    steps: list[StepDefinition] = Field(min_length=1, max_length=100)
    dependencies: list[DependencyDefinition] = Field(default_factory=list, max_length=500)
    policy: CoursePolicy = Field(default_factory=CoursePolicy)
    created_by: str = Field(default="", max_length=120)


class RunStart(BaseModel):
    run_code: str = Field(min_length=2, max_length=64)
    created_by: str = Field(default="", max_length=120)


class StepClaim(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    step_code: str | None = Field(default=None, max_length=64)
    lease_seconds: int = Field(default=300, ge=5, le=3600)


class StepComplete(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    result: dict = Field(default_factory=dict)
    note: str = Field(default="", max_length=1000)


class StepFailure(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    error_code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool = False


class StepCancel(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class StepRedo(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class RecoverRequest(BaseModel):
    actor: str = Field(default="recovery-worker", max_length=120)
