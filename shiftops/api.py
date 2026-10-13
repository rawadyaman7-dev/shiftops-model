"""
The same handlers over HTTP, for when the platform outgrows the demo.

    uvicorn shiftops.api:app --reload

This is the transport that works for a link you send to someone: a normal web
service, no Claude app required. It is deliberately thin -- every endpoint is one
line of plumbing around a handler in `shiftops.service` -- so that the HTTP and
MCP routes cannot drift apart in behaviour.

What is NOT here yet, and must be before this faces a real client: authentication,
a database behind the scenario (today the caller supplies it on every request),
rate limiting, and a queue for solves that outlast a request timeout. The model is
ready for production; this file is not, and says so.
"""

from __future__ import annotations

from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit(
        "the HTTP transport needs fastapi and uvicorn: pip install '.[api]'"
    ) from exc

from .service import (
    handle_audit,
    handle_candidates,
    handle_params_schema,
    handle_plan,
    handle_repair,
    handle_validate,
)
from .validate import ScenarioRejected

app = FastAPI(
    title="ShiftOps model",
    version="0.1.0",
    description="Two-stage MIP scheduling and rule-based repair.",
)


class PlanRequest(BaseModel):
    scenario: dict[str, Any]
    strict: bool = True


class AuditRequest(BaseModel):
    scenario: dict[str, Any]
    roster: list[dict[str, Any]]


class RepairRequest(BaseModel):
    scenario: dict[str, Any]
    on: str
    template_index: int
    roster: list[dict[str, Any]]
    history: list[dict[str, Any]]
    absent_employee_id: int | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/params")
def params() -> dict[str, Any]:
    return handle_params_schema()


@app.post("/validate")
def validate_endpoint(req: PlanRequest) -> dict[str, Any]:
    return handle_validate(req.scenario)


@app.post("/plan")
def plan_endpoint(req: PlanRequest) -> dict[str, Any]:
    try:
        return handle_plan(req.scenario, strict=req.strict)
    except ScenarioRejected as exc:
        raise HTTPException(status_code=422, detail=[f.model_dump() for f in exc.findings])


@app.post("/audit")
def audit_endpoint(req: AuditRequest) -> dict[str, Any]:
    return handle_audit(req.scenario, req.roster)


@app.post("/repair")
def repair_endpoint(req: RepairRequest) -> dict[str, Any]:
    return handle_repair(req.scenario, req.on, req.template_index, req.roster,
                         req.history, req.absent_employee_id)


@app.post("/candidates")
def candidates_endpoint(req: RepairRequest) -> dict[str, Any]:
    return handle_candidates(req.scenario, req.on, req.template_index, req.roster,
                             req.history, req.absent_employee_id)
