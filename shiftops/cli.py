"""A command line over the same handlers, for trying things without a front end.

    python -m shiftops.cli validate tests/fixtures/august_polemidia.json
    python -m shiftops.cli plan     tests/fixtures/august_polemidia.json
    python -m shiftops.cli plan     tests/fixtures/august_polemidia.json --legacy
    python -m shiftops.cli params
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .service import handle_params_schema, handle_plan, handle_validate
from .validate import ScenarioRejected

LEGACY = {"cover_mode": "indicator", "break_treatment": "smear", "prorate_floor": False}


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def _print_findings(findings: list[dict]) -> None:
    for f in findings:
        mark = {"error": "!!", "warning": " !", "note": "  "}[f["level"]]
        print(f"  {mark} {f['code']}: {f['message']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shiftops", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="check a scenario without solving it")
    p_val.add_argument("scenario")

    p_plan = sub.add_parser("plan", help="solve a scenario")
    p_plan.add_argument("scenario")
    p_plan.add_argument("--legacy", action="store_true",
                        help="reproduce the browser prototype, defects included")
    p_plan.add_argument("--json", action="store_true", help="print the whole result")
    p_plan.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="override one parameter, repeatable")

    sub.add_parser("params", help="print every parameter and its default")

    args = parser.parse_args(argv)

    if args.command == "params":
        print(json.dumps(handle_params_schema()["defaults"], indent=1))
        return 0

    scenario = _load(args.scenario)

    if args.command == "validate":
        result = handle_validate(scenario)
        print("ok" if result["ok"] else "REJECTED")
        _print_findings(result["findings"])
        return 0 if result["ok"] else 1

    params = scenario.setdefault("params", {})
    if args.legacy:
        params.update(LEGACY)
    for pair in args.set:
        key, _, value = pair.partition("=")
        try:
            params[key] = json.loads(value)
        except json.JSONDecodeError:
            params[key] = value

    # --legacy deliberately reproduces a configuration the validator rejects,
    # so it is allowed through with the findings printed rather than raised.
    try:
        result = handle_plan(scenario, strict=not args.legacy)
    except ScenarioRejected as exc:
        print("scenario rejected:", file=sys.stderr)
        _print_findings([f.model_dump() for f in exc.findings])
        return 1

    if args.json:
        print(json.dumps(result, indent=1, default=str))
        return 0

    cov, cost, sp = result["coverage"], result["cost"], result["spread"]
    print(f"{result['status']}  {result['shifts']} shifts over "
          f"{len(result['provenance']['horizon'])} days "
          f"({result['provenance']['horizon'][0]} to {result['provenance']['horizon'][-1]})")
    print(f"  demand met   {cov['pct'] * 100:.1f}%  "
          f"({cov['short']:g} of {cov['required']:g} barista-hours short)")
    print(f"  cost         EUR {cost['total']:.2f}  "
          f"(straight {cost['straight']:.2f} + premium {cost['premium']:.2f} "
          f"on {cost['overtime_hours']:g} h)")
    print(f"  people       {sp['people']}, {sp['min']:g}-{sp['max']:g} h")
    print(f"  unfilled     {len(result['unfilled'])} of {result['positions_opened']} positions")
    print(f"  breaches     {result['breach_count']}")
    print(f"  solved in    {result['solve_ms']} ms  "
          f"(parameters {result['provenance']['params_version']})")
    for note in result["notes"]:
        print(f"  note         {note}")
    findings = [f for f in result["findings"] if f["level"] != "note"]
    if args.legacy and findings:
        print("  (reproducing the prototype: the configuration below is one this "
              "model would otherwise refuse)")
    if findings:
        print("  checks:")
        _print_findings(findings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
