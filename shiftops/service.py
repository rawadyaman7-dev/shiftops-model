"""
The handlers, written once and served over any transport.

Nothing in this module knows whether it was reached over MCP, over HTTP or from a
test. Each handler takes plain JSON-shaped data and returns the same, so the
choice of bridge to the front end is a packaging decision rather than an
architectural one.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .mip import plan_horizon, plan_window
from .repair import WorkedShift, cover_candidates, repair
from .rules import audit, cost_of, coverage, spread
from .scenario import Assignment, Params, Scenario
from .validate import ScenarioRejected, validate


def _as_scenario(payload: dict[str, Any] | Scenario) -> Scenario:
    return payload if isinstance(payload, Scenario) else Scenario.model_validate(payload)


def _as_rows(rows: list[dict[str, Any]] | list[Assignment]) -> list[Assignment]:
    return [r if isinstance(r, Assignment) else Assignment.model_validate(r) for r in rows]


def _report(scn: Scenario, rows: list[Assignment]) -> dict[str, Any]:
    """The same summary for any roster, whoever produced it."""
    cov = coverage(scn, rows)
    breaches = audit(scn, rows)
    return {
        "shifts": len(rows),
        "coverage": {
            "required": cov.required,
            "delivered": cov.delivered,
            "short": cov.short,
            "spare": cov.spare,
            "pct": round(cov.pct, 6),
        },
        "cost": cost_of(scn, rows),
        "spread": spread(rows),
        "breaches": [b.model_dump() for b in breaches],
        "breach_count": len(breaches),
    }


# ----------------------------------------------------------------- handlers


def handle_validate(scenario: dict[str, Any]) -> dict[str, Any]:
    """Check a scenario before anyone spends a solve on it."""
    scn = _as_scenario(scenario)
    findings = validate(scn)
    return {
        "ok": not any(f.level == "error" for f in findings),
        "findings": [f.model_dump() for f in findings],
    }


def handle_plan(scenario: dict[str, Any], strict: bool = True) -> dict[str, Any]:
    """Plan a horizon. Calendar weeks are solved in turn, rest carried across."""
    scn = _as_scenario(scenario)
    findings = validate(scn)
    blocking = [f for f in findings if f.level == "error"]
    if blocking and strict:
        raise ScenarioRejected(blocking)

    sol = plan_horizon(scn) if len(scn.dates) > 7 or len(scn.weeks()) > 1 \
        else plan_window(scn)
    report = _report(scn, sol.assignments)
    return {
        "status": sol.status,
        "assignments": [a.model_dump(mode="json") for a in sol.assignments],
        "unfilled": [u.model_dump(mode="json") for u in sol.unfilled],
        "positions_opened": sol.positions_opened,
        "templates_opened": sol.templates_opened,
        "hours_by_employee": sol.hours_by_employee,
        "solve_ms": sol.solve_ms,
        "notes": sol.notes,
        "provenance": {
            "params_version": scn.params.version,
            "horizon": [d.isoformat() for d in sorted(scn.dates)],
            "branch_id": scn.branch_id,
            "availability_records": len(scn.availability),
        },
        "findings": [f.model_dump() for f in findings],
        **report,
    }


def handle_audit(scenario: dict[str, Any],
                 roster: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure any roster -- ours, theirs, last year's -- on the same yardstick."""
    scn = _as_scenario(scenario)
    return _report(scn, _as_rows(roster))


def handle_repair(scenario: dict[str, Any], on: str, template_index: int,
                  roster: list[dict[str, Any]], history: list[dict[str, Any]],
                  absent_employee_id: int | None = None) -> dict[str, Any]:
    """One disrupted shift inside a published week, repaired by rule."""
    scn = _as_scenario(scenario)
    rows = _as_rows(roster)
    hist = [WorkedShift.model_validate(h) for h in history]
    result = repair(scn, date.fromisoformat(on), template_index, rows, hist,
                    absent_employee_id)
    return {
        "date": on,
        "template_index": template_index,
        "resolved": result["resolved"],
        "explanation": result["explanation"],
        "chosen": result["chosen"].model_dump(mode="json") if result["chosen"] else None,
        "candidates": [c.model_dump(mode="json") for c in result["all"]],
    }


def handle_candidates(scenario: dict[str, Any], on: str, template_index: int,
                      roster: list[dict[str, Any]], history: list[dict[str, Any]],
                      absent_employee_id: int | None = None) -> dict[str, Any]:
    """The ranked candidate list without choosing for the manager."""
    scn = _as_scenario(scenario)
    cands = cover_candidates(scn, date.fromisoformat(on), template_index,
                             _as_rows(roster),
                             [WorkedShift.model_validate(h) for h in history],
                             absent_employee_id)
    return {"candidates": [c.model_dump(mode="json") for c in cands]}


def handle_params_schema() -> dict[str, Any]:
    """Every parameter, its default and what it means -- for the settings panel."""
    schema = Params.model_json_schema()
    return {
        "defaults": Params().model_dump(mode="json"),
        "schema": schema,
    }


HANDLERS = {
    "validate_scenario": handle_validate,
    "plan": handle_plan,
    "audit_roster": handle_audit,
    "repair_shift": handle_repair,
    "cover_candidates": handle_candidates,
    "params_schema": handle_params_schema,
}
