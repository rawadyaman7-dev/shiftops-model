"""Turn the prototype's seed.json into a Scenario in the new contract.

Run once to build the test fixture. The prototype stores availability as a single
current value; here it becomes one record effective from before the horizon, which
is the shape the platform will write once employees can edit their own.
"""

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shiftops.scenario import (  # noqa: E402
    AvailabilityRecord,
    DemandDay,
    Employee,
    Params,
    Scenario,
    Template,
    Window,
)

WINDOW = {"off": Window.OFF, "morning": Window.MORNING,
          "evening": Window.EVENING, "any": Window.ANY}


def build(seed_path: str, branch_id: int, out_path: str) -> Scenario:
    seed = json.loads(Path(seed_path).read_text())
    br = next(b for b in seed["branches"] if b["id"] == branch_id)
    dates = [date.fromisoformat(d) for d in seed["dates"]]
    effective = min(dates).replace(day=1)

    templates = [
        Template(index=t["i"], label=t["label"], start_min=t["start"], end_min=t["end"])
        for t in br["templates"]
    ]

    employees, availability = [], []
    for e in br["employees"]:
        employees.append(Employee(id=e["id"], name=e.get("name"), wage=e["cost"]))
        av = e.get("avail", {})
        win7 = av.get("win7")
        if win7:
            windows = [WINDOW[w] for w in win7]
        else:
            base = WINDOW.get(av.get("window", "any"), Window.ANY)
            days = av.get("days", [1] * 7)
            windows = [base if d else Window.OFF for d in days]
        availability.append(
            AvailabilityRecord(
                employee_id=e["id"],
                effective_from=effective,
                weekday_windows=windows,
                declared_hours=float(av.get("hours", 0) or 0),
                source="inferred",
            )
        )

    demand = [
        DemandDay(on=date.fromisoformat(iso), required=[float(v) for v in d["R"]])
        for iso, d in sorted(br["demand"].items())
    ]

    scn = Scenario(
        branch_id=branch_id,
        branch_name=br.get("name", ""),
        dates=dates,
        templates=templates,
        employees=employees,
        availability=availability,
        demand=demand,
        params=Params(version="pilot-2026-08"),
    )
    Path(out_path).write_text(scn.model_dump_json(indent=1))
    return scn


if __name__ == "__main__":
    seed = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/app/seed.json"
    out = sys.argv[2] if len(sys.argv) > 2 else "tests/fixtures/august_polemidia.json"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    s = build(seed, 43, out)
    print(f"{out}: {len(s.dates)} days, {len(s.employees)} employees, "
          f"{len(s.templates)} templates, {len(s.demand)} demand days")
