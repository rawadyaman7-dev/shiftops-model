"""
The two-stage mixed-integer programme.

Stage 1 decides, for one day, how many shifts of each shape to run against the
hourly requirement. Stage 2 decides, for one window, who works them.

Both stages are solved with HiGHS -- the same solver the browser prototype runs
compiled to WebAssembly -- so the two implementations can be compared objective
for objective rather than hoped to agree.

Three things here are deliberately different from that prototype, and each is a
corrected defect rather than a design choice:

  * the big-M switch in the fairness constraints had its inequality inverted, so
    the exemption for anyone given no work could never fire. Left as it was, the
    relative form returns a roster of zero shifts labelled optimal.
  * the overtime variable was declared integer, which over-charges the premium on
    any half-integral week.
  * cover was an all-or-nothing indicator with a half-hour threshold, which makes
    a whole hour of demand uncoverable when a shift starts on the half hour. See
    Scenario.cover.
"""

from __future__ import annotations

import time
from datetime import date

import highspy

from .scenario import (
    HOUR_FROM,
    HOUR_TO,
    Assignment,
    Lock,
    Scenario,
    Solution,
    Template,
    Unfilled,
)

INF = highspy.kHighsInf


def _new_solver(scn: Scenario) -> highspy.Highs:
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.setOptionValue("time_limit", float(scn.params.time_limit))
    h.setOptionValue("mip_rel_gap", float(scn.params.mip_gap))
    return h


def _total(terms):
    """Sum a list of HiGHS expressions without relying on int + expression."""
    if not terms:
        return None
    acc = terms[0]
    for t in terms[1:]:
        acc = acc + t
    return acc


def _status(h: highspy.Highs) -> str:
    return h.modelStatusToString(h.getModelStatus())


# =========================================================== stage 1


class Stage1Result:
    def __init__(self, on: date, counts: dict[int, int], short: float, spare: float,
                 objective: float, status: str):
        self.on = on
        self.counts = counts
        self.short = short
        self.spare = spare
        self.objective = objective
        self.status = status

    @property
    def positions(self) -> list[int]:
        out: list[int] = []
        for idx, n in sorted(self.counts.items()):
            out.extend([idx] * n)
        return out

    @property
    def hours(self) -> float:
        return 0.0  # filled in by the caller, which knows the templates


def solve_stage1(scn: Scenario, on: date) -> Stage1Result:
    """How many shifts of each shape to run on one day."""
    p = scn.params
    templates = scn.templates
    headcount = scn.headcount(on)
    cap = max(1, headcount)
    mean_wage = sum(e.wage for e in scn.employees) / len(scn.employees)
    demand = scn.demand_by_date.get(on)

    h = _new_solver(scn)
    obj_terms = []

    n = {}
    y = {}
    slack_short: dict[int, object] = {}
    slack_spare: dict[int, object] = {}
    for t in templates:
        n[t.index] = h.addVariable(lb=0, ub=cap, type=highspy.HighsVarType.kInteger,
                                   name=f"n_{t.index}")
        y[t.index] = h.addVariable(lb=0, ub=1, type=highspy.HighsVarType.kInteger,
                                   name=f"y_{t.index}")
        obj_terms.append((t.hours * mean_wage + p.shift_penalty) * n[t.index])
        h.addConstr(n[t.index] - cap * y[t.index] <= 0, name=f"act_{t.index}")

    for hour in range(HOUR_FROM, HOUR_TO):
        need = demand.at(hour) if demand else 0.0
        contributions = [
            scn.cover(t, hour) * n[t.index]
            for t in templates
            if scn.cover(t, hour) > 0
        ]
        if not contributions and need == 0:
            continue
        short = h.addVariable(lb=0, ub=max(need, 0.0), name=f"sh_{hour}")
        spare = h.addVariable(lb=0, ub=50, name=f"sp_{hour}")
        slack_short[hour] = short
        slack_spare[hour] = spare
        lhs = _total(contributions)
        expr = (lhs + short - spare) if lhs is not None else (short - spare)
        h.addConstr(expr == need, name=f"cov_{hour}")
        obj_terms.append(p.short_penalty * short)
        obj_terms.append(p.spare_penalty * spare)

    distinct = _total([y[t.index] for t in templates])
    if distinct is not None:
        h.addConstr(distinct <= p.max_distinct_shifts, name="kcap")
    if p.cap_by_headcount:
        opened = _total([n[t.index] for t in templates])
        if opened is not None:
            h.addConstr(opened <= headcount, name="hcap")

    h.minimize(_total(obj_terms))

    counts = {t.index: int(round(h.variableValue(n[t.index]))) for t in templates}
    short_total = sum(h.variableValue(v) for v in slack_short.values())
    spare_total = sum(h.variableValue(v) for v in slack_spare.values())

    return Stage1Result(on, counts, round(short_total, 6), round(spare_total, 6),
                        h.getObjectiveValue(), _status(h))


# =========================================================== stage 2


def _same_day_rest(a: Template, b: Template) -> float:
    """Hours of rest available if one person works both shapes on one day."""
    gap = max(b.start_min - a.end_min, a.start_min - b.end_min) / 60
    wrap = 24 - (max(a.end_min, b.end_min) - min(a.start_min, b.start_min)) / 60
    return max(gap, wrap)


def solve_stage2(scn: Scenario, positions: dict[date, list[int]]) -> Solution:
    """Who works the positions stage 1 opened, across the whole window."""
    p = scn.params
    days = sorted(scn.dates)
    day_no = {d: d.toordinal() for d in days}
    tpl = {t.index: t for t in scn.templates}
    emps = scn.employees
    window_days = len(days)

    # how many of each shape on each day
    need: dict[tuple[date, int], int] = {}
    for d in days:
        for idx in positions.get(d, []):
            need[(d, idx)] = need.get((d, idx), 0) + 1

    h = _new_solver(scn)
    obj = []
    x: dict[tuple[int, date, int], object] = {}
    z: dict[tuple[int, date], object] = {}
    unfilled_vars: dict[tuple[date, int], object] = {}

    # --- staffing equation, one per (day, shape)
    for (d, idx), count in sorted(need.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        t = tpl[idx]
        terms = []
        for e in emps:
            if not scn.alpha(e.id, d, t):
                continue
            v = h.addVariable(lb=0, ub=1, type=highspy.HighsVarType.kInteger,
                              name=f"x_{e.id}_{d.isoformat()}_{idx}")
            x[(e.id, d, idx)] = v
            terms.append(v)
            obj.append(e.wage * t.hours * v)
        unfilled = h.addVariable(lb=0, ub=count, type=highspy.HighsVarType.kInteger,
                                 name=f"v_{d.isoformat()}_{idx}")
        unfilled_vars[(d, idx)] = unfilled
        lhs = _total(terms)
        h.addConstr(((lhs + unfilled) if lhs is not None else unfilled) == count,
                    name=f"st_{d.isoformat()}_{idx}")
        obj.append(p.short_penalty * t.hours * unfilled)

    # --- per employee
    for e in emps:
        mine = [(d, idx, tpl[idx]) for (eid, d, idx) in x if eid == e.id]
        if not mine:
            continue

        for d in days:
            todays = [(x[(e.id, d, idx)], tpl[idx]) for (eid, dd, idx) in x
                      if eid == e.id and dd == d]
            if not todays:
                continue
            zv = h.addVariable(lb=0, ub=1, type=highspy.HighsVarType.kInteger,
                               name=f"z_{e.id}_{d.isoformat()}")
            z[(e.id, d)] = zv
            hours_today = _total([t.hours * v for v, t in todays])
            h.addConstr(hours_today <= p.max_daily_hours,
                        name=f"dh_{e.id}_{d.isoformat()}")
            for v, _t in todays:
                h.addConstr(v - zv <= 0, name=f"lk_{e.id}_{d.isoformat()}_{_t.index}")
            for i in range(len(todays)):
                for j in range(i + 1, len(todays)):
                    (va, ta), (vb, tb) = todays[i], todays[j]
                    if _same_day_rest(ta, tb) < p.min_rest_hours:
                        h.addConstr(va + vb <= 1,
                                    name=f"sr_{e.id}_{d.isoformat()}_{ta.index}_{tb.index}")

        # weekly hours: the legal cap and the hours they offered, whichever binds
        cap = scn.weekly_cap(e.id, days[0], window_days)
        week_hours = _total([t.hours * x[(e.id, d, idx)] for d, idx, t in mine])
        h.addConstr(week_hours <= cap, name=f"wk_{e.id}")

        zs = [z[(e.id, d)] for d in days if (e.id, d) in z]
        if zs:
            h.addConstr(_total(zs) <= p.max_days_per_week, name=f"dy_{e.id}")

        # rest between consecutive days, and across the window boundary
        for d, idx, t in mine:
            if d == days[0] and e.id in scn.carry_in:
                rest = (day_no[d] * 1440 + t.start_min - scn.carry_in[e.id]) / 60
                if rest < p.min_rest_hours:
                    h.addConstr(x[(e.id, d, idx)] == 0,
                                name=f"cr_{e.id}_{d.isoformat()}_{idx}")
            for d2, idx2, t2 in mine:
                if (d2 - d).days != 1:
                    continue
                rest = ((day_no[d2] * 1440 + t2.start_min)
                        - (day_no[d] * 1440 + t.end_min)) / 60
                if rest < p.min_rest_hours:
                    h.addConstr(x[(e.id, d, idx)] + x[(e.id, d2, idx2)] <= 1,
                                name=f"rs_{e.id}_{d.isoformat()}_{idx}_{idx2}")

        # overtime above the threshold. Continuous: an integer here would charge
        # a 47.5-hour week for 8 premium hours instead of 7.5.
        o = h.addVariable(lb=0, ub=p.max_weekly_hours, name=f"o_{e.id}")
        h.addConstr(week_hours - o <= p.overtime_threshold, name=f"ot_{e.id}")
        obj.append(e.wage * (p.overtime_rate - 1) * o)

    fairness_note = _add_fairness(h, scn, x, z, obj, days, tpl, window_days)
    _add_locks(h, scn, x)

    t0 = time.time()
    h.minimize(_total(obj))
    ms = int((time.time() - t0) * 1000)

    assignments, unfilled_out = [], []
    hours_by_emp: dict[int, float] = {}
    straight = 0.0
    for (eid, d, idx), var in x.items():
        if h.variableValue(var) > 0.5:
            t = tpl[idx]
            assignments.append(Assignment(on=d, employee_id=eid, template_index=idx,
                                          start_min=t.start_min, end_min=t.end_min,
                                          hours=t.hours))
            hours_by_emp[eid] = hours_by_emp.get(eid, 0.0) + t.hours
            straight += t.hours * scn.employee_by_id[eid].wage
    for (d, idx), count in need.items():
        value = h.variableValue(unfilled_vars[(d, idx)])
        for _ in range(int(round(value))):
            unfilled_out.append(Unfilled(on=d, template_index=idx, hours=tpl[idx].hours))

    assignments.sort(key=lambda a: (a.on, a.start_min, a.employee_id))
    return Solution(
        status=_status(h),
        assignments=assignments,
        unfilled=unfilled_out,
        objective=round(h.getObjectiveValue(), 6),
        straight_cost=round(straight, 2),
        hours_by_employee={k: round(v, 4) for k, v in sorted(hours_by_emp.items())},
        solve_ms=ms,
        params_version=p.version,
        horizon=days,
        notes=[fairness_note] if fairness_note else [],
    )


def _add_fairness(h, scn: Scenario, x, z, obj, days, tpl, window_days: int) -> str | None:
    """Returns a note when the target was deliberately not applied.

    Fairness is a statement about a week's work. Applied to a window shorter than
    a week it says something absurd instead: the entitlement is pro-rated to a
    fraction of a shift, so one person taking one shift lands at three times
    their share while everyone else sits at zero, and the constraint then forbids
    the roster rather than balancing it. On a short window the target is skipped
    and the fact is recorded.

    Measuring fairness over a rolling four weeks instead of the solve window
    would remove the special case altogether; that is the right long-term shape
    and is not implemented.
    """
    p = scn.params
    if p.fairness_mode == "off":
        return None
    if window_days < 7:
        return (f"fairness target not applied to this {window_days}-day window: "
                "balancing hours needs a whole week")

    def hours_expr(eid: int):
        terms = [tpl[idx].hours * v for (e2, d, idx), v in x.items() if e2 == eid]
        return _total(terms)

    def worked_switch(eid: int):
        """w = 1 exactly when this person is given work."""
        zs = [z[(eid, d)] for d in days if (eid, d) in z]
        if not zs:
            return None
        w = h.addVariable(lb=0, ub=1, type=highspy.HighsVarType.kInteger, name=f"w_{eid}")
        for zv in zs:
            h.addConstr(zv - w <= 0, name=f"fa_{eid}_{id(zv)}")
        h.addConstr(_total(zs) - w >= 0, name=f"fb_{eid}")
        return w

    if p.fairness_mode == "absolute":
        BIG = 300.0
        hmax = h.addVariable(lb=0, ub=200, name="Hmax")
        hmin = h.addVariable(lb=0, ub=200, name="Hmin")
        any_bound = False
        for e in scn.employees:
            expr = hours_expr(e.id)
            if expr is None:
                continue
            h.addConstr(expr - hmax <= 0, name=f"f1_{e.id}")
            w = worked_switch(e.id) if p.fairness_used_only else None
            if w is not None:
                # The exemption, with the inequality the right way round: when
                # w = 0 the row is slack, when w = 1 the lower bound binds.
                h.addConstr(expr - hmin - BIG * w >= -BIG, name=f"f2_{e.id}")
            else:
                h.addConstr(expr - hmin >= 0, name=f"f2_{e.id}")
            any_bound = True
        if any_bound:
            h.addConstr(hmax - hmin <= p.fairness_delta, name="f3")

    elif p.fairness_mode == "relative":
        BIG = 10.0
        rmax = h.addVariable(lb=0, ub=10, name="Rmax")
        rmin = h.addVariable(lb=0, ub=10, name="Rmin")
        any_bound = False
        for e in scn.employees:
            expr = hours_expr(e.id)
            if expr is None:
                continue
            req = scn.declared_hours(e.id, days[0]) * (window_days / 7)
            if req <= 0:
                continue
            h.addConstr(expr - req * rmax <= 0, name=f"g1_{e.id}")
            w = worked_switch(e.id)
            if w is not None:
                h.addConstr(expr - req * rmin - BIG * req * w >= -BIG * req,
                            name=f"g2_{e.id}")
            else:
                h.addConstr(expr - req * rmin >= 0, name=f"g2_{e.id}")
            any_bound = True
        if any_bound:
            h.addConstr(rmax - rmin <= p.fairness_pct, name="g3")


def _add_locks(h, scn: Scenario, x) -> None:
    for k, lock in enumerate(scn.locks):
        key = (lock.employee_id, lock.on, lock.template_index)
        if key in x:
            h.addConstr(x[key] == 1, name=f"lock_{k}")


# =========================================================== the planner


def plan_window(scn: Scenario) -> Solution:
    """Both stages over one window. The window must be a calendar week or less."""
    positions: dict[date, list[int]] = {}
    opened: dict[int, int] = {}
    s1_short = 0.0
    for d in sorted(scn.dates):
        r = solve_stage1(scn, d)
        positions[d] = r.positions
        s1_short += r.short
        for idx, count in r.counts.items():
            opened[idx] = opened.get(idx, 0) + count
    sol = solve_stage2(scn, positions)
    sol.positions_opened = sum(len(v) for v in positions.values())
    sol.templates_opened = {k: v for k, v in sorted(opened.items())}
    if s1_short:
        sol.notes.append(
            f"stage 1 could not cover {round(s1_short, 2)} barista-hours with the "
            "shapes in the catalogue"
        )
    return sol


def plan_horizon(scn: Scenario) -> Solution:
    """The whole horizon, solved one calendar week at a time.

    The weekly limits are written over the solve window, so the window has to be
    a week: solving a month in one pass would apply the 48-hour cap once across
    the month. Rest is carried across each seam.
    """
    all_assign: list[Assignment] = []
    all_unfilled: list[Unfilled] = []
    opened: dict[int, int] = {}
    carry: dict[int, int] = dict(scn.carry_in)
    ms = 0
    positions = 0
    notes: list[str] = []
    status = "Optimal"

    for week in scn.weeks():
        sub = scn.slice(week)
        sub.carry_in = carry
        r = plan_window(sub)
        all_assign.extend(r.assignments)
        all_unfilled.extend(r.unfilled)
        ms += r.solve_ms
        positions += r.positions_opened
        notes.extend(r.notes)
        for k, v in r.templates_opened.items():
            opened[k] = opened.get(k, 0) + v
        if r.status != "Optimal":
            status = r.status
        last = week[-1]
        carry = {}
        for a in r.assignments:
            if a.on == last:
                end = a.on.toordinal() * 1440 + a.end_min
                carry[a.employee_id] = max(carry.get(a.employee_id, 0), end)

    hours: dict[int, float] = {}
    straight = 0.0
    for a in all_assign:
        hours[a.employee_id] = hours.get(a.employee_id, 0.0) + a.hours
        straight += a.hours * scn.employee_by_id[a.employee_id].wage

    return Solution(
        status=status,
        assignments=sorted(all_assign, key=lambda a: (a.on, a.start_min, a.employee_id)),
        unfilled=all_unfilled,
        straight_cost=round(straight, 2),
        hours_by_employee={k: round(v, 4) for k, v in sorted(hours.items())},
        positions_opened=positions,
        templates_opened=opened,
        solve_ms=ms,
        params_version=scn.params.version,
        horizon=sorted(scn.dates),
        notes=sorted(set(notes)),
    )
