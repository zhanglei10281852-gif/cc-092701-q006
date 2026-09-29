from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.cnc.schemas import (
    FlowSubmit,
    RecoverRequest,
    RunStart,
    StepCancel,
    StepClaim,
    StepComplete,
    StepFailure,
    StepRedo,
)
from app.cnc.service import CncFlowService

router = APIRouter(prefix="/api/cnc", tags=["数控实训流程"])


def service() -> CncFlowService:
    return CncFlowService()


@router.post("/flows")
def submit_flow(payload: FlowSubmit):
    view = service().submit_flow(payload.model_dump())
    return JSONResponse(status_code=200 if view.get("reused") else 201, content=view)


@router.get("/flows")
def list_flows():
    return {"items": service().list_flows()}


@router.get("/flows/{flow_code}")
def get_flow(flow_code: str):
    return service().get_flow(flow_code)


@router.post("/flows/{flow_code}/runs", status_code=201)
def start_run(flow_code: str, payload: RunStart):
    return service().start_run(flow_code, payload.run_code, payload.created_by)


@router.get("/runs/{run_id}")
def get_run(run_id: int):
    return service().get_run(run_id)


@router.post("/runs/{run_id}/claim")
def claim(run_id: int, payload: StepClaim):
    run = service().claim(run_id, payload.worker_id, payload.step_code, payload.lease_seconds)
    return {"run": run}


@router.post("/runs/{run_id}/steps/{step_code}/complete")
def complete(run_id: int, step_code: str, payload: StepComplete):
    return service().complete(run_id, step_code, payload.worker_id, payload.result, payload.note)


@router.post("/runs/{run_id}/steps/{step_code}/fail")
def fail(run_id: int, step_code: str, payload: StepFailure):
    return service().fail(run_id, step_code, payload.worker_id, payload.error_code, payload.message, payload.retryable)


@router.post("/runs/{run_id}/steps/{step_code}/cancel")
def cancel(run_id: int, step_code: str, payload: StepCancel):
    return service().cancel(run_id, step_code, payload.actor, payload.reason)


@router.post("/runs/{run_id}/steps/{step_code}/redo")
def redo(run_id: int, step_code: str, payload: StepRedo):
    return service().redo(run_id, step_code, payload.actor, payload.reason)


@router.post("/recovery/expired-leases")
def recover_expired(payload: RecoverRequest):
    return service().recover_expired(payload.actor)
