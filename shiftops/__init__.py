"""ShiftOps — the scheduling model.

Two engines, deliberately separate: a two-stage mixed-integer programme that
plans a week that has not been published, and a rule-based engine that repairs
one shift inside a week that has. They share the legal limits and nothing else.

Everything crosses the boundary as a Scenario or a Solution; see
`shiftops.scenario`, which is also where the rule that keeps the platform and
the model in step lives -- availability is dated, and the model's parameters are
derived from it rather than stored alongside it.
"""

__version__ = "0.1.0"

from .scenario import Params, Scenario, Solution  # noqa: F401
