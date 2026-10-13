"""
Repairing one disrupted shift inside a week that is already published.

The optimiser is for weeks that have not gone out yet. Once people have arranged
their lives around a roster, the mathematically optimal response to one absence
is usually a different roster for everybody, which is not a response at all. So a
published week is repaired by the branch's own rules: who is legal, who is free,
who has done this time of day recently, and then who is cheapest.

Exactly one shift changes. Everyone else's week is untouched, and the reason is
recorded.

The legal tests here are the same ones the MIP enforces. The rest -- the
experience band, the lookback, the ranking -- are the branch's own and every one
of them is meant to be argued with, which is why they are parameters.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel

from .scenario import Assignment, Scenario, Template


class WorkedShift(BaseModel):
    """A shift someone actually worked, used to judge experience."""

    employee_id: int
    on: date
    start_min: int
    end_min: int


class Candidate(BaseModel):
    employee_id: int
    display: str
    eligible: bool
    blocked_by_experience_only: bool = False
    blockers: list[str] = []
    notes: list[str] = []

    times_in_band: int = 0
    last_in_band: date | None = None
    marginal_cost: float = 0.0
    overtime_hours: float = 0.0
    beyond_declared: float = 0.0
    week_hours: float = 0.0
    week_days: int = 0
    declared: float = 0.0
    tightest_rest: float | None = None


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def cover_candidates(
    scn: Scenario,
    on: date,
    template_index: int,
    roster: list[Assignment],
    history: list[WorkedShift],
    absent_employee_id: int | None = None,
) -> list[Candidate]:
    """Everyone who could take this shift, ranked, with the reasons either way."""
    p = scn.params
    tpl: Template = next(t for t in scn.templates if t.index == template_index)
    weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][on.weekday()]

    slot_start = on.toordinal() * 1440 + tpl.start_min
    slot_end = on.toordinal() * 1440 + tpl.end_min
    since = on - timedelta(days=p.cover_lookback_days)
    week_start = on - timedelta(days=on.weekday())
    week_end = week_start + timedelta(days=6)

    out: list[Candidate] = []
    for e in scn.employees:
        blockers: list[str] = []
        notes: list[str] = []

        in_band = [
            w for w in history
            if w.employee_id == e.id
            and since <= w.on < on
            and abs(w.start_min - tpl.start_min) <= p.cover_band_minutes
        ]
        no_experience = not in_band

        if absent_employee_id is not None and e.id == absent_employee_id:
            blockers.append("this is their own shift")
        if scn.is_absent(e.id, on):
            blockers.append("away that day")

        window = scn.window_on(e.id, on)
        if window.value == "off":
            blockers.append(f"does not work {weekday}s")
        elif not scn.window_fits(window, tpl):
            blockers.append(f"{weekday}s are {window.value}s only for them")

        same_day = [r for r in roster if r.employee_id == e.id and r.on == on]
        if same_day:
            blockers.append(f"already on at {_hhmm(same_day[0].start_min)}")

        tightest: float | None = None
        for r in roster:
            if r.employee_id != e.id:
                continue
            rs = r.on.toordinal() * 1440 + r.start_min
            re = r.on.toordinal() * 1440 + r.end_min
            gap = None
            if re <= slot_start:
                gap = (slot_start - re) / 60
            elif rs >= slot_end:
                gap = (rs - slot_end) / 60
            if gap is not None and (tightest is None or gap < tightest):
                tightest = gap
        if tightest is not None and tightest < p.min_rest_hours:
            blockers.append(f"only {round(tightest, 1)} h rest")

        week = [r for r in roster
                if r.employee_id == e.id and week_start <= r.on <= week_end]
        week_hours = sum(r.hours for r in week)
        week_days = len({r.on for r in week})

        # The legal weekly cap is a hard stop. The hours someone asked for are a
        # preference: cover may go past them, said out loud, and they may decline.
        declared = scn.declared_hours(e.id, on)
        asked = declared if (p.respect_declared_hours and declared > 0) else p.max_weekly_hours
        after = week_hours + tpl.hours
        beyond = 0.0
        if after > p.max_weekly_hours + 1e-9:
            blockers.append(f"would reach {round(after, 1)} h, past the "
                            f"{p.max_weekly_hours:g} h weekly limit")
        elif after > asked + 1e-9:
            beyond = round(after - asked, 2)
            notes.append(f"{beyond:g} h more than the {asked:g} h they asked for")
        if week_days + 1 > p.max_days_per_week:
            blockers.append(f"would be a {week_days + 1}th day that week")

        over = (max(0.0, after - p.overtime_threshold)
                - max(0.0, week_hours - p.overtime_threshold))
        cost = tpl.hours * e.wage + over * e.wage * (p.overtime_rate - 1)
        if over > 0:
            notes.append(f"{round(over, 1)} h of it at the overtime rate")

        if no_experience:
            msg = (f"has not worked this time of day in the last "
                   f"{p.cover_lookback_days} days")
            (blockers if p.cover_require_experience else notes).append(msg)
        if beyond and not p.cover_allow_beyond_asked:
            blockers.append(f"past the {asked:g} h they asked for")

        # Someone stopped only by the experience rule is still worth showing: the
        # manager may know something the last thirty days do not.
        exp_only = len(blockers) == 1 and no_experience and p.cover_require_experience

        out.append(Candidate(
            employee_id=e.id, display=e.display,
            eligible=not blockers, blocked_by_experience_only=exp_only,
            blockers=blockers, notes=notes,
            times_in_band=len(in_band),
            last_in_band=in_band[-1].on if in_band else None,
            marginal_cost=round(cost, 2), overtime_hours=round(over, 2),
            beyond_declared=beyond, week_hours=round(week_hours, 2),
            week_days=week_days, declared=round(asked, 2),
            tightest_rest=None if tightest is None else round(tightest, 2),
        ))

    def sort_key(c: Candidate):
        if p.cover_rank == "cost":
            primary = c.marginal_cost
        elif p.cover_rank == "load":
            primary = c.week_hours
        else:
            primary = 0 if c.times_in_band else 1
        return (
            0 if c.eligible else 1,
            0 if c.blocked_by_experience_only else 1,
            primary,
            1 if c.beyond_declared else 0,
            c.marginal_cost,
            c.week_hours,
            -c.times_in_band,
        )

    return sorted(out, key=sort_key)


def repair(
    scn: Scenario,
    on: date,
    template_index: int,
    roster: list[Assignment],
    history: list[WorkedShift],
    absent_employee_id: int | None = None,
) -> dict:
    """The one change this disruption calls for, or an honest empty answer."""
    ranked = cover_candidates(scn, on, template_index, roster, history,
                              absent_employee_id)
    eligible = [c for c in ranked if c.eligible]
    fallback = [c for c in ranked if c.blocked_by_experience_only]
    chosen = eligible[0] if eligible else None
    return {
        "date": on,
        "template_index": template_index,
        "chosen": chosen,
        "eligible": eligible,
        "experience_only": fallback,
        "all": ranked,
        "resolved": chosen is not None,
        "explanation": (
            f"{chosen.display} covers: "
            + (f"worked this time of day {chosen.times_in_band}x since "
               f"{chosen.last_in_band}" if chosen.times_in_band else "available and legal")
            + f", {chosen.week_hours:g} h that week, costs EUR {chosen.marginal_cost:g}"
            if chosen else
            "nobody can legally cover this shift; it needs the hours limit relaxed, "
            "someone brought in from another branch, or the shift shortened"
        ),
    }
