"""
Compliance and coverage, measured the same way for any roster.

Nothing here knows whether a roster came from the optimiser, from the repair
engine or from a client's spreadsheet. That is the point: the only way to say
honestly that one roster is more compliant than another is to measure both with
the same instrument.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from .scenario import HOUR_FROM, HOUR_TO, Assignment, Breach, Coverage, Scenario


def _week_of(d: date) -> int:
    """Ordinal of the Monday of this date's calendar week."""
    return d.toordinal() - d.weekday()


def audit(scn: Scenario, rows: list[Assignment]) -> list[Breach]:
    """Every way a roster breaks the limits, listed one by one."""
    p = scn.params
    out: list[Breach] = []
    by_emp: dict[int, list[Assignment]] = defaultdict(list)
    for r in rows:
        by_emp[r.employee_id].append(r)

    for eid, shifts in sorted(by_emp.items()):
        shifts = sorted(shifts, key=lambda a: (a.on, a.start_min))
        cover_end = None
        cover_row = None
        by_day: dict[date, float] = defaultdict(float)
        by_week: dict[int, dict] = defaultdict(lambda: {"hours": 0.0, "days": set()})

        for r in shifts:
            start = r.on.toordinal() * 1440 + r.start_min
            end = r.on.toordinal() * 1440 + r.end_min
            if cover_row is not None:
                gap = (start - cover_end) / 60
                if gap < 0:
                    out.append(Breach(
                        rule="overlap", employee_id=eid, value=round(-gap, 2),
                        detail=f"{cover_row.on} {cover_row.start_min // 60:02d}:00 "
                               f"overlaps {r.on} by {round(-gap, 2)} h"))
                elif gap < p.min_rest_hours:
                    out.append(Breach(
                        rule="rest", employee_id=eid, value=round(gap, 2),
                        detail=f"only {round(gap, 2)} h between {cover_row.on} and {r.on}"))
            if cover_end is None or end > cover_end:
                cover_end, cover_row = end, r
            by_day[r.on] += r.hours
            w = by_week[_week_of(r.on)]
            w["hours"] += r.hours
            w["days"].add(r.on)

        for d, hrs in sorted(by_day.items()):
            if hrs > p.max_daily_hours + 1e-9:
                out.append(Breach(rule="daily", employee_id=eid, value=round(hrs, 2),
                                  detail=f"{round(hrs, 2)} h on {d}"))
        for wk, info in sorted(by_week.items()):
            if info["hours"] > p.max_weekly_hours + 1e-9:
                out.append(Breach(rule="weekly", employee_id=eid,
                                  value=round(info["hours"], 2),
                                  detail=f"{round(info['hours'], 2)} h in the week of "
                                         f"{date.fromordinal(wk)}"))
            if len(info["days"]) > p.max_days_per_week:
                out.append(Breach(rule="days", employee_id=eid,
                                  value=len(info["days"]),
                                  detail=f"{len(info['days'])} days in the week of "
                                         f"{date.fromordinal(wk)}"))
    return out


def coverage(scn: Scenario, rows: list[Assignment]) -> Coverage:
    """Hour by hour, how much of the requirement this roster actually meets."""
    tpl = {t.index: t for t in scn.templates}
    by_date: dict[date, list[Assignment]] = defaultdict(list)
    for r in rows:
        by_date[r.on].append(r)

    required = delivered = short = spare = 0.0
    per_day: dict[date, float] = {}

    for day in scn.demand:
        on_day = by_date.get(day.on, [])
        day_short = 0.0
        for hour in range(HOUR_FROM, HOUR_TO):
            want = day.at(hour)
            have = sum(scn.cover(tpl[a.template_index], hour) for a in on_day)
            required += want
            delivered += min(have, want)
            if have < want:
                short += want - have
                day_short += want - have
            else:
                spare += have - want
        per_day[day.on] = round(day_short, 4)

    return Coverage(required=round(required, 4), delivered=round(delivered, 4),
                    short=round(short, 4), spare=round(spare, 4), by_date=per_day)


def cost_of(scn: Scenario, rows: list[Assignment]) -> dict[str, float]:
    """Straight time, overtime hours and the premium, by calendar week.

    The prototype's dashboard showed straight time while its objective minimised
    straight time plus the premium. Both are reported here so they cannot drift.
    """
    p = scn.params
    wages = {e.id: e.wage for e in scn.employees}
    by_week: dict[tuple[int, int], float] = defaultdict(float)
    for r in rows:
        by_week[(_week_of(r.on), r.employee_id)] += r.hours

    straight = overtime_hours = premium = 0.0
    for (_wk, eid), hours in by_week.items():
        wage = wages.get(eid, 0.0)
        over = max(0.0, hours - p.overtime_threshold)
        straight += hours * wage
        overtime_hours += over
        premium += over * wage * (p.overtime_rate - 1)

    return {
        "straight": round(straight, 2),
        "overtime_hours": round(overtime_hours, 2),
        "premium": round(premium, 2),
        "total": round(straight + premium, 2),
    }


def spread(rows: list[Assignment]) -> dict[str, float]:
    """How unevenly the hours fell, which is what a fairness target aims at."""
    hours: dict[int, float] = defaultdict(float)
    for r in rows:
        hours[r.employee_id] += r.hours
    if not hours:
        return {"people": 0, "min": 0.0, "max": 0.0, "range": 0.0}
    values = list(hours.values())
    return {
        "people": len(values),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "range": round(max(values) - min(values), 2),
    }
