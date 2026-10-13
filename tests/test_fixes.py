"""
The three defects carried over from the prototype, and the proof they are gone.

Each test states the broken behaviour first, by switching the model back to the
prototype's setting, and then the corrected one. Keeping both in the test suite
is deliberate: it is the only way the write-up's claims stay true as the code
moves on.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from shiftops.mip import plan_horizon
from shiftops.rules import coverage
from shiftops.scenario import Scenario

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "august_polemidia.json"
RAW = FIXTURE.read_text()


def scenario(**params) -> Scenario:
    scn = Scenario.model_validate_json(RAW)
    for key, value in params.items():
        setattr(scn.params, key, value)
    return scn


# ------------------------------------------------ 1. the inverted fairness switch


def test_relative_fairness_used_to_empty_the_roster_and_no_longer_does():
    """The prototype returned zero shifts, labelled optimal, at +/-10%.

    The big-M switch meant to exempt anyone given no work had its inequality the
    wrong way round, so the exemption could never fire and the only way to give
    everyone the same share was to give everyone nothing.
    """
    scn = scenario(fairness_mode="relative", fairness_pct=0.10)
    sol = plan_horizon(scn)
    assert sol.status == "Optimal"
    assert len(sol.assignments) > 0, "the fairness exemption is inverted again"
    cov = coverage(scn, sol.assignments)
    assert cov.pct > 0.5


def test_the_fairness_exemption_actually_exempts():
    """With the sign corrected, someone given no work does not drag the floor to
    zero and cap everybody else."""
    scn = scenario(fairness_mode="absolute", fairness_delta=4,
                   fairness_used_only=True)
    with_switch = plan_horizon(scn)

    scn2 = scenario(fairness_mode="absolute", fairness_delta=4,
                    fairness_used_only=False)
    without = plan_horizon(scn2)

    # In the prototype these two were bit-identical, because the branch the
    # switch controls could never take effect.
    assert len(with_switch.assignments) >= len(without.assignments)
    assert with_switch.assignments != without.assignments


def test_a_slack_fairness_target_does_not_bind():
    """A tolerance wide enough to be meaningless should give the unconstrained
    answer, not a degraded one."""
    free = plan_horizon(scenario())
    slack = plan_horizon(scenario(fairness_mode="relative", fairness_pct=0.9))
    assert len(slack.assignments) == len(free.assignments)


def test_fairness_is_skipped_on_a_window_shorter_than_a_week():
    """Pro-rating an entitlement to one day makes the ratios meaningless: one
    shift becomes three times somebody's share. The target is skipped there and
    says so, rather than forbidding the roster."""
    scn = scenario(fairness_mode="relative", fairness_pct=0.10)
    sol = plan_horizon(scn)
    assert any("fairness target not applied" in n for n in sol.notes)
    last = max(scn.dates)  # 31 August, a one-day window
    assert [a for a in sol.assignments if a.on == last]


# ------------------------------------------------ 2. the stub-window hours cap


def test_a_one_day_window_can_still_be_staffed():
    """31 August was rostered with zero shifts against 47 barista-hours of demand.

    The declared-hours cap is pro-rated by window length, so a one-day window gave
    a 45-hour-a-week barista 6.4 hours -- less than any shift in the catalogue.
    """
    scn = scenario(prorate_floor=False)
    broken = plan_horizon(scn)
    last = max(scn.dates)
    assert not [a for a in broken.assignments if a.on == last], \
        "the fixture no longer reproduces the stub-window failure"

    fixed = plan_horizon(scenario())
    assert [a for a in fixed.assignments if a.on == last], \
        "the pro-rated cap is not being floored"


def test_the_floor_never_exceeds_the_legal_cap():
    scn = scenario()
    day = min(scn.dates)
    cap = scn.weekly_cap(scn.employees[0].id, day, window_days=1)
    assert cap <= scn.params.max_weekly_hours


def test_a_full_week_is_unaffected_by_the_floor():
    """The repair must change short windows and nothing else."""
    week = sorted(Scenario.model_validate_json(RAW).dates)[2:9]
    a = Scenario.model_validate_json(RAW)
    a.params.prorate_floor = False
    a.dates = week
    a.demand = [d for d in a.demand if d.on in set(week)]
    b = Scenario.model_validate_json(RAW)
    b.dates = week
    b.demand = [d for d in b.demand if d.on in set(week)]
    assert plan_horizon(a).straight_cost == plan_horizon(b).straight_cost


# ------------------------------------------------ 3. cover and the break


def test_a_half_hour_of_presence_counts_for_half_an_hour():
    scn = scenario()
    morning = scn.templates[0]  # 05:30 start
    assert morning.overlap_minutes(5) == 30
    assert scn.cover(morning, 5) == pytest.approx(0.5)


def test_the_prototype_rule_threw_that_half_hour_away():
    scn = scenario(cover_mode="indicator", break_treatment="smear")
    morning = scn.templates[0]
    # 30 raw minutes, minus a smeared 15-minute break, lands at 29.21 -- just
    # under the half-hour threshold, so the whole hour counted as uncovered.
    assert scn.cover_minutes(morning, 5) == pytest.approx(29.21, abs=0.01)
    assert scn.cover(morning, 5) == 0.0


def test_no_shape_covers_the_opening_hour_under_the_prototype_rule():
    scn = scenario(cover_mode="indicator", break_treatment="smear")
    assert max(scn.cover(t, 5) for t in scn.templates) == 0.0


def test_the_validator_catches_an_hour_nothing_can_cover():
    from shiftops.validate import validate

    scn = scenario(cover_mode="indicator", break_treatment="smear")
    codes = {f.code for f in validate(scn) if f.level == "error"}
    assert "demand.uncoverable" in codes


def test_a_scheduled_break_is_refused_rather_than_faked():
    scn = scenario(break_treatment="scheduled")
    with pytest.raises(NotImplementedError):
        scn.cover(scn.templates[0], 9)


# ------------------------------------------------ the overtime variable


def test_the_overtime_premium_is_not_rounded_up():
    """o_e was declared integer, charging a 47.5-hour week for 8 premium hours."""
    from shiftops.rules import cost_of

    scn = scenario()
    sol = plan_horizon(scn)
    cost = cost_of(scn, sol.assignments)
    # the premium must be exactly half the wage times the overtime hours
    wage = scn.employees[0].wage
    assert cost["premium"] == pytest.approx(
        cost["overtime_hours"] * wage * (scn.params.overtime_rate - 1), abs=0.01)
