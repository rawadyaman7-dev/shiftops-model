"""
The contract itself: derived parameters, and availability that remembers when.

These tests exist because of one question -- "if an employee changes their
availability, does the parameter change too?" -- and the answer the design gives:
there is no second copy to change. The parameter is a function of the
availability record, so it cannot disagree with it, and the record is dated, so
last week's published roster is still explicable after this week's edit.
"""

from __future__ import annotations

from datetime import date

import pytest

from shiftops.scenario import (
    AvailabilityRecord,
    DemandDay,
    Employee,
    Params,
    Scenario,
    Template,
    Window,
)

MON, TUE = date(2026, 8, 3), date(2026, 8, 4)
LATER_MON = date(2026, 8, 10)


def _scenario(records: list[AvailabilityRecord], **params) -> Scenario:
    return Scenario(
        branch_id=1,
        dates=[MON, TUE, LATER_MON],
        templates=[
            Template(index=0, label="Morning", start_min=330, end_min=900),
            Template(index=1, label="Noon", start_min=900, end_min=1440),
        ],
        employees=[Employee(id=7, name="Ada", wage=5.05)],
        availability=records,
        demand=[DemandDay(on=d, required=[2.0] * 20) for d in (MON, TUE, LATER_MON)],
        params=Params(**params),
    )


def test_availability_edit_does_not_rewrite_the_past():
    """The question that started this: editing availability must not change what
    was true when a roster was issued."""
    original = AvailabilityRecord(
        employee_id=7, effective_from=date(2026, 7, 1), effective_to=LATER_MON,
        weekday_windows=[Window.ANY] * 7, declared_hours=40,
    )
    edited = AvailabilityRecord(
        employee_id=7, effective_from=LATER_MON,
        weekday_windows=[Window.MORNING] * 7, declared_hours=20,
    )
    scn = _scenario([original, edited])
    evening = scn.templates[1]

    # the week that was already published still sees the old answer
    assert scn.alpha(7, MON, evening) is True
    assert scn.declared_hours(7, MON) == 40

    # the week being planned sees the new one
    assert scn.alpha(7, LATER_MON, evening) is False
    assert scn.declared_hours(7, LATER_MON) == 20


def test_the_parameter_is_derived_not_stored():
    """Changing the record changes the parameter, with nothing to keep in step."""
    rec = AvailabilityRecord(
        employee_id=7, effective_from=date(2026, 7, 1),
        weekday_windows=[Window.ANY] * 7, declared_hours=40,
    )
    scn = _scenario([rec])
    morning = scn.templates[0]
    assert scn.alpha(7, MON, morning) is True

    # the employee says Mondays are off, and nothing else is touched
    rec.weekday_windows[0] = Window.OFF
    assert scn.alpha(7, MON, morning) is False
    assert scn.alpha(7, TUE, morning) is True


def test_an_approved_absence_removes_availability_for_that_day_only():
    from shiftops.scenario import Absence

    scn = _scenario([AvailabilityRecord(
        employee_id=7, effective_from=date(2026, 7, 1),
        weekday_windows=[Window.ANY] * 7, declared_hours=40)])
    scn.absences.append(Absence(employee_id=7, on=MON, status="approved"))
    assert scn.alpha(7, MON, scn.templates[0]) is False
    assert scn.alpha(7, TUE, scn.templates[0]) is True


def test_a_pending_absence_does_not():
    from shiftops.scenario import Absence

    scn = _scenario([AvailabilityRecord(
        employee_id=7, effective_from=date(2026, 7, 1),
        weekday_windows=[Window.ANY] * 7, declared_hours=40)])
    scn.absences.append(Absence(employee_id=7, on=MON, status="pending"))
    assert scn.alpha(7, MON, scn.templates[0]) is True


def test_windows_admit_the_right_shapes():
    scn = _scenario([AvailabilityRecord(
        employee_id=7, effective_from=date(2026, 7, 1),
        weekday_windows=[Window.MORNING, Window.EVENING, Window.ANY, Window.OFF,
                         Window.ANY, Window.ANY, Window.ANY],
        declared_hours=40)])
    morning, noon = scn.templates
    assert scn.alpha(7, MON, morning) is True        # Monday is mornings
    assert scn.alpha(7, MON, noon) is False
    assert scn.alpha(7, TUE, noon) is True           # Tuesday is evenings
    assert scn.alpha(7, TUE, morning) is False


def test_no_record_means_available_and_the_validator_says_so():
    from shiftops.validate import validate

    scn = _scenario([])
    assert scn.alpha(7, MON, scn.templates[0]) is True
    codes = {f.code for f in validate(scn)}
    assert "availability.absent" in codes


def test_weeks_split_on_monday():
    scn = _scenario([])
    scn.dates = [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3), date(2026, 8, 4)]
    weeks = scn.weeks()
    assert [len(w) for w in weeks] == [2, 2]
    assert weeks[1][0] == date(2026, 8, 3)


def test_params_version_travels_with_the_solution():
    scn = _scenario([AvailabilityRecord(
        employee_id=7, effective_from=date(2026, 7, 1),
        weekday_windows=[Window.ANY] * 7, declared_hours=40)], version="v3")
    from shiftops.mip import plan_horizon

    sol = plan_horizon(scn)
    assert sol.params_version == "v3"
    assert sol.horizon == sorted(scn.dates)
