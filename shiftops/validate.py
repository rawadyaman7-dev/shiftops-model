"""
Boundary checks, run before the solver ever sees a scenario.

A mixed-integer programme answers the question it was given. If the question is
malformed it still answers, confidently, and the answer looks like a schedule.
Every check here exists because its absence produced a plausible-looking wrong
roster at some point:

  * a daily cap below the longest shift silently makes that shift unassignable,
    and coverage falls by half with no error anywhere;
  * an employee with no availability record is treated as available for anything,
    which is the most dangerous default in the system;
  * demand in an hour the branch is barely open for can never be met, and shows
    up for ever after as the model underperforming.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .scenario import HOUR_FROM, HOUR_TO, Scenario, Window


class Finding(BaseModel):
    level: Literal["error", "warning", "note"]
    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"[{self.level}] {self.code}: {self.message}"


def validate(scn: Scenario) -> list[Finding]:
    """Everything wrong or suspicious about a scenario, worst first."""
    out: list[Finding] = []
    p = scn.params
    days = sorted(scn.dates)

    # --- demand present and sane
    have_demand = scn.demand_by_date
    missing = [d for d in days if d not in have_demand]
    if missing:
        out.append(Finding(
            level="error", code="demand.missing",
            message=f"{len(missing)} day(s) in the horizon have no demand: "
                    f"{missing[0]}{' and others' if len(missing) > 1 else ''}"))
    for day in scn.demand:
        if any(v < 0 for v in day.required):
            out.append(Finding(level="error", code="demand.negative",
                               message=f"negative requirement on {day.on}"))

    # --- shift shapes against the daily cap
    for t in scn.templates:
        if t.hours > p.max_daily_hours + 1e-9:
            out.append(Finding(
                level="error", code="template.too_long",
                message=f"template {t.index} ({t.label}, {t.hours:g} h) is longer "
                        f"than the {p.max_daily_hours:g} h daily cap, so it can "
                        "never be assigned to anyone"))
        if t.hours > 24 - p.min_rest_hours + 1e-9:
            out.append(Finding(
                level="warning", code="template.vs_rest",
                message=f"template {t.index} ({t.hours:g} h) leaves less than "
                        f"{p.min_rest_hours:g} h of rest in a 24-hour period"))

    # --- the most dangerous default in the system
    for e in scn.employees:
        covering = [r for r in scn.availability
                    if r.employee_id == e.id and any(r.covers(d) for d in days)]
        if not covering:
            out.append(Finding(
                level="warning", code="availability.absent",
                message=f"{e.display} has no availability record covering this "
                        "horizon and will be treated as available for anything"))
            continue
        if p.respect_declared_hours and all(r.declared_hours <= 0 for r in covering):
            out.append(Finding(
                level="warning", code="availability.zero_hours",
                message=f"{e.display} declared zero hours, so the planner can "
                        "never roster them while declared hours are respected"))
        if all(all(w is Window.OFF for w in r.weekday_windows) for r in covering):
            out.append(Finding(
                level="warning", code="availability.never",
                message=f"{e.display} is unavailable on every weekday"))

    # --- demand the catalogue physically cannot reach
    for hour in range(HOUR_FROM, HOUR_TO):
        best = max((scn.cover(t, hour) for t in scn.templates), default=0.0)
        if best >= 1.0 - 1e-9:
            continue
        wanted = [(d.on, d.at(hour)) for d in scn.demand if d.at(hour) > 0]
        if not wanted:
            continue
        total = sum(v for _, v in wanted)
        ceiling = best * len(scn.employees)
        worst = max(v for _, v in wanted)
        if best == 0.0:
            out.append(Finding(
                level="error", code="demand.uncoverable",
                message=f"{hour:02d}:00 asks for staff on {len(wanted)} day(s) "
                        f"({total:g} barista-hours in all) and no shift shape in "
                        "the catalogue covers any of that hour"))
        elif worst > ceiling:
            out.append(Finding(
                level="warning", code="demand.beyond_catalogue",
                message=f"{hour:02d}:00 asks for up to {worst:g} barista-hours but "
                        f"the best-placed shape only delivers {best:.2f} of that "
                        "hour each; check the demand model against the branch's "
                        "opening time"))
        else:
            out.append(Finding(
                level="note", code="demand.partial_hour",
                message=f"{hour:02d}:00 is only partly inside any shift "
                        f"({best:.2f} of the hour at best), so {total:g} "
                        "barista-hours there need more heads than the demand "
                        "figure suggests"))

    # --- parameter combinations that quietly disagree
    if p.max_daily_hours > 24 - p.min_rest_hours + 1e-9:
        out.append(Finding(
            level="warning", code="params.daily_vs_rest",
            message=f"a {p.max_daily_hours:g} h day leaves less than the "
                    f"{p.min_rest_hours:g} h rest the same rule demands"))
    if p.fairness_mode == "relative":
        zero = [e.display for e in scn.employees
                if scn.declared_hours(e.id, days[0]) <= 0]
        if zero:
            out.append(Finding(
                level="warning", code="fairness.no_denominator",
                message="relative fairness divides by declared hours, and "
                        f"{len(zero)} person(s) declared none; they are left out "
                        "of the comparison entirely"))
    if p.fairness_mode != "off":
        shortest = min(t.hours for t in scn.templates)
        if p.fairness_mode == "absolute" and p.fairness_delta < shortest:
            out.append(Finding(
                level="warning", code="fairness.finer_than_a_shift",
                message=f"a {p.fairness_delta:g} h tolerance is finer than the "
                        f"shortest shift ({shortest:g} h), so it cannot be met "
                        "except by giving people identical rosters"))

    # --- locks that cannot be honoured
    tpl = {t.index: t for t in scn.templates}
    for lock in scn.locks:
        if lock.template_index not in tpl:
            out.append(Finding(level="error", code="lock.unknown_template",
                               message=f"lock on {lock.on} names template "
                                       f"{lock.template_index}, which does not exist"))
            continue
        if not scn.alpha(lock.employee_id, lock.on, tpl[lock.template_index]):
            out.append(Finding(
                level="error", code="lock.unavailable",
                message=f"locked assignment on {lock.on} gives a shift to someone "
                        "the availability data says cannot work it"))

    order = {"error": 0, "warning": 1, "note": 2}
    return sorted(out, key=lambda f: (order[f.level], f.code))


def errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "error"]


class ScenarioRejected(ValueError):
    def __init__(self, findings: list[Finding]):
        self.findings = findings
        super().__init__("; ".join(f.message for f in findings))


def require_valid(scn: Scenario) -> list[Finding]:
    """Raise on anything that would make the answer meaningless; return the rest."""
    findings = validate(scn)
    bad = errors(findings)
    if bad:
        raise ScenarioRejected(bad)
    return findings
