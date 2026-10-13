"""
The contract between the platform and the model.

A Scenario is everything the solver is allowed to know. A Solution is everything
it produces. Nothing else crosses the boundary, which is what makes the model
testable, swappable and reproducible.

The single most important rule in this file: **derived parameters are never
stored.** The model's alpha(e, d, t) -- may this person work this shift on this
day -- and q(e) -- how many hours they offered -- are computed from the
availability records at the moment of solving. There is no second copy to fall
out of date when an employee edits their availability. The platform stores what
the employee said; the model derives what that means.

The second rule: everything an employee or a manager can change carries a
validity period. A roster issued on the 5th must still be explicable on the
20th, after the availability behind it has been edited twice.
"""

from __future__ import annotations

from datetime import date, timedelta
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Hours of the day the model reasons about: 04:00 up to but not including 24:00.
HOUR_FROM, HOUR_TO = 4, 24
N_HOURS = HOUR_TO - HOUR_FROM

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class Window(str, Enum):
    """The part of a day a person has offered to work."""

    OFF = "off"
    MORNING = "morning"
    EVENING = "evening"
    ANY = "any"


# ----------------------------------------------------------------- shift shapes


class Template(BaseModel):
    """A shift shape: when it starts, when it ends. Not an instance of a shift."""

    index: int
    label: str
    start_min: int = Field(ge=0, lt=24 * 60, description="minutes from midnight")
    end_min: int = Field(gt=0, le=24 * 60)

    @model_validator(mode="after")
    def _ordered(self) -> "Template":
        if self.end_min <= self.start_min:
            raise ValueError(
                f"template {self.index} ({self.label}) ends at or before it starts; "
                "overnight shapes are not supported yet"
            )
        return self

    @property
    def hours(self) -> float:
        return (self.end_min - self.start_min) / 60

    def overlap_minutes(self, hour: int) -> float:
        """Raw minutes of clock hour `hour` this template is present for."""
        lo = max(self.start_min, hour * 60)
        hi = min(self.end_min, (hour + 1) * 60)
        return max(0.0, hi - lo)


# -------------------------------------------------------------------- people


class Employee(BaseModel):
    id: int
    name: str | None = None
    wage: float = Field(gt=0, description="EUR per hour, straight time")
    grade: str = "barista"

    @property
    def display(self) -> str:
        return self.name or f"#{self.id}"


class AvailabilityRecord(BaseModel):
    """What one person offered, and the period over which they offered it.

    `effective_from` is inclusive, `effective_to` exclusive. A record with no
    `effective_to` is the current one. Editing availability does not rewrite the
    old record: it closes it and opens a new one, so a roster issued last week
    can still be explained by the availability that was true last week.
    """

    employee_id: int
    effective_from: date
    effective_to: date | None = None
    weekday_windows: list[Window] = Field(
        min_length=7,
        max_length=7,
        description="one window per weekday, Monday first",
    )
    declared_hours: float = Field(ge=0, description="hours a week they offered")
    source: Literal["employee", "manager", "inferred"] = "employee"

    def covers(self, on: date) -> bool:
        if on < self.effective_from:
            return False
        return self.effective_to is None or on < self.effective_to


class Absence(BaseModel):
    """An approved day off, holiday or emergency absence."""

    employee_id: int
    on: date
    kind: Literal["holiday", "sick", "emergency", "unavailable"] = "unavailable"
    status: Literal["pending", "approved", "declined"] = "approved"


# -------------------------------------------------------------------- demand


class DemandDay(BaseModel):
    """Required staffing for one day, hour by hour.

    Read as barista-hours in each clock hour, not as a headcount. The two agree
    whenever a shift covers a whole hour, and the barista-hour reading is the one
    that lets a shift present for half an hour count for half an hour.
    """

    on: date
    required: list[float] = Field(min_length=N_HOURS, max_length=N_HOURS)
    note: str | None = None

    def at(self, hour: int) -> float:
        return self.required[hour - HOUR_FROM]


# ---------------------------------------------------------------- parameters


class Params(BaseModel):
    """Limits, prices and policy. Versioned, so a roster can name the set that
    produced it."""

    version: str = "unversioned"

    # --- legal and operational limits
    min_rest_hours: float = 11.0  # CY Organisation of Working Time Law s.4
    max_daily_hours: float = 13.0  # implied by the 11-hour rest rule
    max_weekly_hours: float = 48.0  # s.7, as a hard per-week cap
    max_days_per_week: int = 6  # s.6, as a day count
    break_minutes: float = 15.0  # s.5
    break_threshold_hours: float = 6.0
    # How the statutory break enters the model.
    #   ignore -- the break does not reduce cover, on the reading that a colleague
    #             covers the floor for fifteen minutes. The default, and the only
    #             one of the three that is both coherent and cheap.
    #   smear  -- the prototype's rule: the break is spread evenly over the shift,
    #             so every hour delivers slightly less than one barista. Kept for
    #             comparison. It makes a requirement of three baristas unmeetable
    #             by three shifts, which is arguably true and certainly not what
    #             the demand model means.
    #   scheduled -- the honest model: the break occupies a real quarter of an
    #             hour that somebody else must cover. Needs the whole model at
    #             15-minute granularity; not implemented.
    break_treatment: Literal["ignore", "smear", "scheduled"] = "ignore"

    # --- shift structure
    max_distinct_shifts: int = 5
    cap_by_headcount: bool = True

    # --- prices
    short_penalty: float = 60.0  # EUR per barista-hour uncovered
    spare_penalty: float = 2.0  # EUR per barista-hour of surplus
    shift_penalty: float = 1.5  # EUR per shift opened
    overtime_threshold: float = 40.0
    overtime_rate: float = 1.5

    # --- the hours people asked for
    respect_declared_hours: bool = True
    plan_reserve: float = Field(default=0.0, ge=0, lt=1)

    # --- fairness. A target, not a rule, so off unless asked for.
    fairness_mode: Literal["off", "absolute", "relative"] = "off"
    fairness_delta: float = 10.0
    fairness_pct: float = 0.10
    fairness_used_only: bool = True

    # --- repairing a published week
    cover_band_minutes: int = 60
    cover_lookback_days: int = 30
    cover_require_experience: bool = True
    cover_allow_beyond_asked: bool = True
    cover_rank: Literal["rule", "cost", "load"] = "rule"

    # --- modelling choices that the JavaScript prototype got wrong.
    # Both default to the corrected behaviour; the legacy values exist so the two
    # implementations can be compared like for like.
    cover_mode: Literal["fractional", "indicator"] = "fractional"
    prorate_floor: bool = True

    # --- solver
    time_limit: float = 20.0
    mip_gap: float = 0.0

    @field_validator("max_daily_hours")
    @classmethod
    def _daily_vs_rest(cls, v: float, info) -> float:
        return v


# -------------------------------------------------------------------- scenario


class Lock(BaseModel):
    """An assignment a human has already committed to. The solver may not undo it."""

    employee_id: int
    on: date
    template_index: int
    reason: str = ""


class Scenario(BaseModel):
    """Everything the model is allowed to know about one branch and one horizon."""

    branch_id: int
    branch_name: str = ""
    dates: list[date] = Field(min_length=1)
    templates: list[Template] = Field(min_length=1)
    employees: list[Employee] = Field(min_length=1)
    availability: list[AvailabilityRecord] = []
    absences: list[Absence] = []
    demand: list[DemandDay] = []
    params: Params = Params()
    locks: list[Lock] = []

    # The end of each person's last shift in the window before this one, as an
    # absolute timestamp in minutes. Without it the window boundary is a loophole
    # in the rest rule.
    carry_in: dict[int, int] = {}

    # Dates already published, which the planner must not silently replan.
    published: list[date] = []

    # --- lookups, built once

    @property
    def employee_by_id(self) -> dict[int, Employee]:
        return {e.id: e for e in self.employees}

    @property
    def demand_by_date(self) -> dict[date, DemandDay]:
        return {d.on: d for d in self.demand}

    # --- the derived parameters, computed and never stored

    def availability_on(self, employee_id: int, on: date) -> AvailabilityRecord | None:
        """The record that was in force for this person on this date.

        If an employee edits their availability, the platform closes the old
        record and opens a new one. Planning next week therefore sees the new
        answer, and explaining last week's published roster still sees the old
        one. Nothing has to be kept in step by hand.
        """
        best: AvailabilityRecord | None = None
        for r in self.availability:
            if r.employee_id == employee_id and r.covers(on):
                if best is None or r.effective_from > best.effective_from:
                    best = r
        return best

    def window_on(self, employee_id: int, on: date) -> Window:
        rec = self.availability_on(employee_id, on)
        if rec is None:
            return Window.ANY
        return rec.weekday_windows[on.weekday()]

    def declared_hours(self, employee_id: int, on: date) -> float:
        rec = self.availability_on(employee_id, on)
        return rec.declared_hours if rec else 0.0

    def is_absent(self, employee_id: int, on: date) -> bool:
        return any(
            a.employee_id == employee_id and a.on == on and a.status == "approved"
            for a in self.absences
        )

    def window_fits(self, window: Window, tpl: Template) -> bool:
        if window is Window.OFF:
            return False
        if window is Window.MORNING:
            return tpl.end_min <= 16 * 60
        if window is Window.EVENING:
            return tpl.start_min >= 12 * 60
        return True

    def alpha(self, employee_id: int, on: date, tpl: Template) -> bool:
        """May this person work this template on this day?

        This is the model's availability parameter. It is a function, not a
        stored field, so it cannot disagree with the availability record it is
        derived from.
        """
        if self.is_absent(employee_id, on):
            return False
        return self.window_fits(self.window_on(employee_id, on), tpl)

    def headcount(self, on: date) -> int:
        """How many people could work anything at all on this day."""
        return sum(
            1
            for e in self.employees
            if any(self.alpha(e.id, on, t) for t in self.templates)
        )

    def weekly_cap(self, employee_id: int, on: date, window_days: int) -> float:
        """The hours this person may be planned for across a window of this length.

        Two different things meet here and must not be confused. `max_weekly_hours`
        is law. `declared_hours` is a preference -- what the person offered -- and
        the planner respects it, while the repair engine is allowed to exceed it
        with the shortfall said out loud.

        Pro-rating the preference by the window length is right in the middle of a
        week and wrong at its edges: a one-day window would cap a 45-hour-a-week
        barista at 6.4 hours, which is shorter than any shift, so nobody can be
        assigned anything. `prorate_floor` lifts the pro-rated figure to the
        longest shift in the catalogue, which is the smallest cap that can mean
        anything at all.
        """
        p = self.params
        legal = p.max_weekly_hours
        declared = self.declared_hours(employee_id, on)
        if not p.respect_declared_hours or declared <= 0:
            return legal
        prorated = declared * (1 - p.plan_reserve) * (window_days / 7)
        if p.prorate_floor:
            longest = max(t.hours for t in self.templates)
            prorated = max(prorated, min(longest, legal))
        return min(legal, prorated)

    # --- cover arithmetic

    def cover_minutes(self, tpl: Template, hour: int) -> float:
        """Minutes of clock hour `hour` this template delivers, net of its break."""
        raw = tpl.overlap_minutes(hour)
        p = self.params
        if p.break_treatment == "scheduled":
            raise NotImplementedError(
                "a scheduled break needs the model at 15-minute granularity; "
                "use 'ignore' until that lands"
            )
        if raw == 0 or p.break_treatment == "ignore":
            return raw
        if tpl.hours <= p.break_threshold_hours:
            return raw
        return raw * (1 - p.break_minutes / (tpl.hours * 60))

    def cover(self, tpl: Template, hour: int) -> float:
        """How much of one barista-hour this template contributes to this hour.

        `fractional` credits the minutes actually worked: a shift running from
        05:30 delivers half a barista-hour into the 05:00 hour. `indicator` is the
        prototype's rule -- a template counts fully if it covers at least half the
        hour and not at all otherwise -- kept so the two implementations can be
        compared. The indicator rule rounds both ways: it credits a shift present
        for 31 minutes with a whole hour, and credits one present for 29 with
        nothing. Combined with a smeared break it has a knife edge, because a
        shift starting on the half hour contributes exactly 30 raw minutes, the
        break drops that to 29.21, and a whole hour of demand becomes uncoverable
        by anything in the catalogue.
        """
        m = self.cover_minutes(tpl, hour)
        if self.params.cover_mode == "indicator":
            return 1.0 if m >= 30 else 0.0
        return m / 60

    def weeks(self) -> list[list[date]]:
        """Split the horizon on calendar-week boundaries.

        Every weekly constraint in the model is written over the solve window, so
        the window has to be a calendar week. Solving a month in one window would
        apply the 48-hour cap once across the whole month.
        """
        out: list[list[date]] = []
        cur: list[date] = []
        for d in sorted(self.dates):
            if d.weekday() == 0 and cur:
                out.append(cur)
                cur = []
            cur.append(d)
        if cur:
            out.append(cur)
        return out

    def slice(self, days: list[date]) -> "Scenario":
        """The same scenario narrowed to a sub-window, carrying everything else."""
        return self.model_copy(
            update={
                "dates": list(days),
                "demand": [d for d in self.demand if d.on in set(days)],
            }
        )


# -------------------------------------------------------------------- solution


class Assignment(BaseModel):
    on: date
    employee_id: int
    template_index: int
    start_min: int
    end_min: int
    hours: float


class Unfilled(BaseModel):
    on: date
    template_index: int
    hours: float


class Breach(BaseModel):
    rule: Literal["rest", "daily", "weekly", "days", "overlap"]
    employee_id: int
    detail: str
    value: float


class Coverage(BaseModel):
    required: float
    delivered: float
    short: float
    spare: float
    by_date: dict[date, float] = {}

    @property
    def pct(self) -> float:
        return self.delivered / self.required if self.required else 1.0


class Solution(BaseModel):
    """What the model produced, and enough provenance to reproduce it."""

    status: str
    assignments: list[Assignment] = []
    unfilled: list[Unfilled] = []
    breaches: list[Breach] = []
    coverage: Coverage | None = None

    objective: float = 0.0
    straight_cost: float = 0.0
    overtime_hours: float = 0.0
    total_cost: float = 0.0

    positions_opened: int = 0
    templates_opened: dict[int, int] = {}
    hours_by_employee: dict[int, float] = {}
    solve_ms: int = 0

    # provenance: which parameter set, over which horizon, at what moment
    params_version: str = "unversioned"
    horizon: list[date] = []
    notes: list[str] = []


def iso_days(first: date, last: date) -> list[date]:
    out, d = [], first
    while d <= last:
        out.append(d)
        d += timedelta(days=1)
    return out
