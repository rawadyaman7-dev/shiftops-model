"""
The model as an MCP server, so a page can call it live.

A published Claude artifact cannot reach an arbitrary web server -- external hosts
are blocked -- but it can call MCP servers running on the viewer's own machine.
Running this module is therefore the shortest path from the demo to a real solver:
the browser keeps the interface, Python keeps the model, and there is only one
model.

    python -m shiftops.mcp_server

Then add it to the Claude desktop app's MCP configuration as a server named
`shiftops`, and publish the artifact declaring `host:shiftops`. It answers only
while the app is open on the machine running this process, which makes it right
for a demo and wrong for a link you send to a client -- that is what the HTTP
transport in `shiftops.api` is for.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from .service import (
    handle_audit,
    handle_candidates,
    handle_params_schema,
    handle_plan,
    handle_repair,
    handle_validate,
)

server = MCPServer(
    name="shiftops",
    instructions=(
        "The ShiftOps workforce scheduling model: a two-stage mixed-integer "
        "programme for planning rosters, and a rule-based engine for repairing "
        "a published week after a disruption. Every tool takes a scenario -- the "
        "branch, its people, their availability, the demand forecast and the "
        "parameter set -- and nothing else. Availability is read from dated "
        "records, so planning next week and explaining last week give different "
        "and correct answers."
    ),
)


def _dumps(value: Any) -> str:
    return json.dumps(value, indent=1, default=str)


@server.tool()
def validate_scenario(scenario: dict) -> str:
    """Check a scenario for anything that would make the answer meaningless.

    Catches a daily cap shorter than the longest shift, people with no
    availability record, demand in hours no shift shape can reach, and fairness
    tolerances finer than a single shift.
    """
    return _dumps(handle_validate(scenario))


@server.tool()
def plan(scenario: dict, strict: bool = True) -> str:
    """Plan a horizon with the two-stage MIP, solved one calendar week at a time.

    Returns the assignments, what could not be filled, coverage against the
    demand forecast, cost including the overtime premium, every rule breach, and
    the provenance needed to reproduce the result later.
    """
    return _dumps(handle_plan(scenario, strict=strict))


@server.tool()
def audit_roster(scenario: dict, roster: list[dict]) -> str:
    """Measure any roster against the rules and the demand forecast.

    Works on a roster this model produced and on one it did not, which is the
    only honest way to compare them.
    """
    return _dumps(handle_audit(scenario, roster))


@server.tool()
def repair_shift(scenario: dict, on: str, template_index: int, roster: list[dict],
                 history: list[dict], absent_employee_id: int | None = None) -> str:
    """Repair one disrupted shift in a week that is already published.

    Changes exactly one shift, by the branch's own cover rules, and says why.
    Re-planning the week with the MIP instead would move people who had nothing
    to do with the disruption.
    """
    return _dumps(handle_repair(scenario, on, template_index, roster, history,
                                absent_employee_id))


@server.tool()
def cover_candidates(scenario: dict, on: str, template_index: int,
                     roster: list[dict], history: list[dict],
                     absent_employee_id: int | None = None) -> str:
    """Rank everyone who could take a shift, without choosing for the manager.

    Each candidate carries the reasons they are eligible or not, what the shift
    would cost, and whether it pushes them past the hours they asked for.
    """
    return _dumps(handle_candidates(scenario, on, template_index, roster, history,
                                    absent_employee_id))


@server.tool()
def params_schema() -> str:
    """Every parameter the model takes, with its default and its meaning."""
    return _dumps(handle_params_schema())


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
