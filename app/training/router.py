from __future__ import annotations

from fastapi import APIRouter, Query

from app.training.schemas import (
    CancelRequest,
    ClaimRequest,
    CompleteRequest,
    FailRequest,
    FlowSubmit,
    InstanceStart,
    RedoRequest,
    ReleaseRequest,
)
from app.training.service import TrainingFlowService

router = APIRouter(prefix="/api/training", tags=["数控实训流程"])


def service() -> TrainingFlowService:
    return TrainingFlowService()


@router.post("/flows", status_code=201)
def create_flow(payload: FlowSubmit, actor: str = Query(..., min_length=1)):
    return service().create_flow(payload.model_dump(), actor)


@router.get("/flows/{flow_code}")
def get_flow(flow_code: str):
    return service().get_flow(flow_code)


@router.post("/flows/{flow_code}/instances", status_code=201)
def start_instance(flow_code: str, payload: InstanceStart):
    return service().start_instance(flow_code, payload.business_key)


@router.get("/instances/{instance_id}")
def get_instance(instance_id: int):
    return service().get_instance(instance_id)


@router.get("/instances/{instance_id}/claimable")
def list_claimable(instance_id: int, step_codes: list[str] | None = Query(default=None)):
    return {"items": service().list_claimable(instance_id, step_codes)}


@router.post("/instances/{instance_id}/claim")
def claim(instance_id: int, payload: ClaimRequest):
    return service().claim(instance_id, payload.worker_id, payload.step_codes or None)


@router.post("/instances/{instance_id}/nodes/{code}/complete")
def complete(instance_id: int, code: str, payload: CompleteRequest):
    return service().complete(instance_id, code, payload.worker_id, payload.output, payload.note)


@router.post("/instances/{instance_id}/nodes/{code}/fail")
def fail(instance_id: int, code: str, payload: FailRequest):
    return service().fail(instance_id, code, payload.worker_id, payload.reason)


@router.post("/instances/{instance_id}/nodes/{code}/cancel")
def cancel(instance_id: int, code: str, payload: CancelRequest):
    return service().cancel(instance_id, code, payload.actor, payload.reason)


@router.post("/instances/{instance_id}/nodes/{code}/redo", status_code=202)
def redo(instance_id: int, code: str, payload: RedoRequest):
    return service().redo(instance_id, code, payload.actor, payload.reason)


@router.post("/instances/{instance_id}/nodes/{code}/release")
def release(instance_id: int, code: str, payload: ReleaseRequest):
    return service().release(instance_id, code, payload.worker_id, payload.reason)
