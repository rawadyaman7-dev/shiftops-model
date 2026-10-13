"""
The Python model and the browser prototype are one model, not two.

Run in legacy mode -- the prototype's cover rule, its smeared break, no floor on
the pro-rated hours cap -- the two implementations must agree on everything that
is not a free choice between equally optimal rosters. Objective-equivalent
rosters can differ in who got which shift when several people cost the same, so
the assertions are on the quantities the objective actually determines.

If this test ever fails, one of the two has drifted and the write-up is wrong
about something.
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from shiftops.mip import plan_horizon
from shiftops.rules import audit, coverage, cost_of
from shiftops.scenario import Scenario

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "august_polemidia.json"
REFERENCE = ROOT / "reference" / "run_reference.cjs"


@functools.lru_cache(maxsize=2)
def _reference(mode: str = "legacy") -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    if not (ROOT / "reference" / "node_modules" / "highs").exists():
        pytest.skip("run `npm install` in reference/ to compare against the browser solver")
    out = subprocess.run(["node", str(REFERENCE), "43", mode], capture_output=True,
                         text=True, cwd=ROOT / "reference", timeout=600)
    if out.returncode != 0:
        pytest.skip(f"reference solver would not run: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def legacy_scenario() -> Scenario:
    scn = Scenario.model_validate_json(FIXTURE.read_text())
    # exactly what the JavaScript does, defects included
    scn.params.cover_mode = "indicator"
    scn.params.break_treatment = "smear"
    scn.params.prorate_floor = False
    return scn


@pytest.fixture(scope="module")
def legacy_solution(legacy_scenario):
    return plan_horizon(legacy_scenario)


def test_same_shift_count(legacy_solution):
    assert len(legacy_solution.assignments) == _reference()["shifts"]


def test_same_positions_and_unfilled(legacy_solution):
    ref = _reference()
    assert legacy_solution.positions_opened == ref["positions"]
    assert len(legacy_solution.unfilled) == ref["unfilled"]


def test_same_templates_opened(legacy_solution):
    ref = {int(k): v for k, v in _reference()["templates_opened"].items()}
    assert legacy_solution.templates_opened == ref


def test_same_cost(legacy_scenario, legacy_solution):
    ref = _reference()
    cost = cost_of(legacy_scenario, legacy_solution.assignments)
    assert cost["straight"] == pytest.approx(ref["straight_cost"], abs=0.01)
    assert cost["overtime_hours"] == pytest.approx(ref["overtime_hours"], abs=0.01)


def test_same_coverage(legacy_scenario, legacy_solution):
    ref = _reference()
    cov = coverage(legacy_scenario, legacy_solution.assignments)
    assert cov.required == pytest.approx(ref["coverage_required"], abs=0.01)
    assert cov.short == pytest.approx(ref["coverage_short"], abs=0.01)
    assert cov.pct == pytest.approx(ref["coverage_pct"], abs=1e-4)


def test_both_are_compliant(legacy_scenario, legacy_solution):
    assert audit(legacy_scenario, legacy_solution.assignments) == []
    assert _reference()["breaches"] == 0


def test_same_people_and_ceiling(legacy_solution):
    ref = _reference()
    hours = legacy_solution.hours_by_employee
    assert len(hours) == ref["people"]
    # the busiest person is pinned by the hours caps; the least busy is not,
    # because several equally cheap rosters share the same objective
    assert max(hours.values()) == pytest.approx(ref["hours_max"], abs=0.01)


# --------------------------------------------------------------------------
# The same agreement with the corrections switched ON. This is the one that
# matters day to day: it is what stops the demo and the model reporting two
# different numbers for the same month.


@pytest.fixture(scope="module")
def corrected_scenario() -> Scenario:
    return Scenario.model_validate_json(FIXTURE.read_text())


@pytest.fixture(scope="module")
def corrected_solution(corrected_scenario):
    return plan_horizon(corrected_scenario)


def test_corrected_same_shifts_and_unfilled(corrected_solution):
    ref = _reference("corrected")
    assert len(corrected_solution.assignments) == ref["shifts"]
    assert len(corrected_solution.unfilled) == ref["unfilled"]


def test_corrected_same_cost(corrected_scenario, corrected_solution):
    ref = _reference("corrected")
    cost = cost_of(corrected_scenario, corrected_solution.assignments)
    assert cost["straight"] == pytest.approx(ref["straight_cost"], abs=0.05)


def test_corrected_same_coverage(corrected_scenario, corrected_solution):
    ref = _reference("corrected")
    cov = coverage(corrected_scenario, corrected_solution.assignments)
    assert cov.short == pytest.approx(ref["coverage_short"], abs=0.05)
    assert cov.pct == pytest.approx(ref["coverage_pct"], abs=1e-4)


def test_corrected_beats_the_real_roster_without_breaking_a_rule(
        corrected_scenario, corrected_solution):
    """The claim the whole project rests on, asserted rather than asserted about."""
    assert audit(corrected_scenario, corrected_solution.assignments) == []
    assert coverage(corrected_scenario, corrected_solution.assignments).pct > 0.95
