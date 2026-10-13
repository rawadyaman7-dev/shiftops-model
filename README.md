# ShiftOps — the scheduling model

The optimisation model behind the adaptive F&B workforce scheduling platform:
a two-stage mixed-integer programme that plans rosters, and a rule-based engine
that repairs a published week after a disruption.

Pilot: Clock Coffee, Polemidia branch, Cyprus. Clock Coffee shared a month of
operating data to help the work; their roster was never built against these rules
and was never meant to be.

```
                    ┌──────────────────────────┐
   employee edits   │   availability record    │   dated, never overwritten
   their days  ───► │  effective_from / _to    │
                    └────────────┬─────────────┘
                                 │  derived at solve time, never stored
                                 ▼
        α(e,d,t)  may this person work this shape on this day
        q(e)      how many hours they offered
                                 │
                    ┌────────────▼─────────────┐
                    │  Scenario  →  Solution   │   the only thing that crosses
                    └────────────┬─────────────┘   the boundary
                                 │
            ┌────────────────────┼────────────────────┐
            ▼                    ▼                    ▼
      MCP server            HTTP service            tests
   (live bridge to       (shiftops.api, for      (the model is the
    the demo page)        a real deployment)       same in all three)
```

## Why the parameter cannot fall out of step

The question this design answers: *if an employee changes their availability,
does the corresponding model parameter change too?*

It cannot fail to, because **there is no second copy.** `α(e, d, t)` and `q(e)`
are functions of the availability record, computed at the moment of solving —
see `Scenario.alpha` and `Scenario.declared_hours`. Nothing caches them, so
nothing can disagree with them.

The second half is that availability records are **dated**. Editing availability
closes the old record and opens a new one rather than overwriting it, so:

- planning next week sees the new answer;
- explaining the roster issued last week still sees the old one.

`tests/test_contract.py::test_availability_edit_does_not_rewrite_the_past` is
that guarantee, written down.

## Quick start

```bash
pip install -e .
python -m shiftops.cli plan tests/fixtures/august_polemidia.json
```

```
Optimal  154 shifts over 31 days (2026-08-01 to 2026-08-31)
  demand met   97.1%  (42.5 of 1475 barista-hours short)
  cost         EUR 7335.12  (straight 7234.12 + premium 101.00 on 40 h)
  people       12, 76-189.5 h
  unfilled     1 of 155 positions
  breaches     0
  solved in    212 ms  (parameters pilot-2026-08)
```

Against the roster Clock Coffee actually ran in August, measured the same way:
**97.1% of demand met against their 93.6%, with zero rule breaches against their
34.** (Their roster scores 90.7% under the prototype's old cover rule and 93.6%
under this one — both rosters move together when the yardstick changes, which is
the point of measuring them with the same instrument.)

Other things worth trying:

```bash
# reproduce the browser prototype exactly, defects and all
python -m shiftops.cli plan tests/fixtures/august_polemidia.json --legacy

# the daily cap the branch wanted, which outlaws their own main shift
python -m shiftops.cli plan tests/fixtures/august_polemidia.json --set max_daily_hours=9

# check a scenario without spending a solve on it
python -m shiftops.cli validate tests/fixtures/august_polemidia.json
```

## Connecting it to the demo

A published Claude artifact **cannot call an arbitrary web server** — external
hosts are blocked, which is why the offline build of the demo has to carry a
3.5 MB solver inside it. There is one outbound path: MCP servers on the viewer's
own machine.

**Live bridge, today:**

```bash
python -m shiftops.mcp_server
```

Add it to the Claude desktop app's MCP configuration as `shiftops`, and publish
the demo declaring `host:shiftops`. The browser keeps the interface, Python keeps
the model, and there is only one model. It answers only while that app is open on
the machine running the process — right for a demo, wrong for a link you send to
a client.

**For a link you send to a client:**

```bash
pip install -e '.[api]'
uvicorn shiftops.api:app --reload
```

The same handlers over HTTP, with the front end rebuilt as an ordinary web page
the service serves. `shiftops/api.py` lists what is still missing before that
faces anyone real.

## What is in here

| | |
|---|---|
| `shiftops/scenario.py` | the contract: sets, parameters, derived availability, solution |
| `shiftops/mip.py` | both MIP stages and the planner |
| `shiftops/repair.py` | the rule-based cover engine for a published week |
| `shiftops/rules.py` | compliance audit, coverage, cost — applied to any roster |
| `shiftops/validate.py` | boundary checks, run before the solver sees anything |
| `shiftops/service.py` | the handlers, written once |
| `shiftops/mcp_server.py` | MCP transport |
| `shiftops/api.py` | HTTP transport |
| `reference/` | the browser prototype's solver, kept to prove equivalence |

The formal statement of the model — sets, variables, objective, every constraint
with the section of Cypriot law it comes from — is in the formulation document
that accompanies this repository.

## Two engines, deliberately separate

| | Planning | Repair |
|---|---|---|
| When | a week that is not published yet | a disruption inside a week already out |
| Method | the two-stage MIP, solved to optimality | the branch's own cover rules |
| Scope | replans everyone | changes one shift, nobody else moves |
| Shared | the legal limits | the legal limits |

Re-solving a published week is the wrong answer to one absence: the optimal
response is usually a different roster for everybody, which is not a response at
all.

## Three defects corrected from the prototype

Each is reproducible by switching the model back — see `tests/test_fixes.py`.

**1. The fairness constraint had an inverted inequality.** The big-M switch meant
to exempt anyone given no work had its sign the wrong way round, so the exemption
could never fire. The relative form therefore returned a roster of **zero shifts,
labelled optimal**, at any tolerance under about 20%. Corrected, ±10% delivers
78% coverage. The `fairness_used_only` setting did nothing at all until this was
fixed.

**2. A one-day planning window could not be staffed.** The declared-hours cap is
pro-rated by window length, so a one-day window capped a 45-hour-a-week barista at
6.4 hours — shorter than any shift in the catalogue. 31 August was rostered with
**zero shifts** against 47 barista-hours of demand. `prorate_floor` lifts the
pro-rated figure to the longest shift, which is the smallest cap that can mean
anything.

**3. Cover was an all-or-nothing indicator with a smeared break.** A shift
starting at 05:30 contributes exactly 30 raw minutes to the 05:00 hour; the
smeared 15-minute break dropped that to 29.21, below the half-hour threshold, so
**no shape in the catalogue covered the opening hour** and stage 1 simply booked
the shortfall. Cover is now fractional — half an hour of presence counts as half
an hour — and the break no longer reduces cover at all, on the reading that a
colleague covers the floor for fifteen minutes.

That last point deserves its own note, because the obvious fix is wrong. Keeping
the smeared break *and* making cover fractional makes every hour of a long shift
deliver 0.974 baristas, so a requirement of three is unmeetable by three shifts
and coverage falls to 78.8%. The three honest treatments are in
`Params.break_treatment`: `ignore` (the default), `smear` (the prototype's, kept
for comparison) and `scheduled` (the correct one, needing the whole model at
15-minute granularity, and refused rather than faked).

A fourth, smaller correction: the overtime variable was declared integer, which
charged a 47.5-hour week for 8 premium hours instead of 7.5.

## The prototype and this model are one model

`tests/test_equivalence.py` runs the browser solver and this one on the same
month and asserts they agree, with the corrections switched off:

```
145 shifts · 155 positions · 10 unfilled · EUR 6812.45 straight · 40 h overtime
88.47% of demand · 0 breaches · templates opened {0: 93, 1: 62, 2: 0, 3: 0}
```

To run that comparison:

```bash
cd reference && npm install && cd ..
python -m pytest tests/ -q
```

The same comparison runs again with the corrections switched **on**, which is
what stops the demo and the model reporting two different numbers for the same
month:

```
154 shifts · 1 unfilled · EUR 7234.13 straight · 97.12% of demand · 0 breaches
```

Without `npm install` those eleven tests skip and the other twenty-one still run.

## What this does not do yet

Stated plainly, because a model's limits belong next to its results.

- **Weekly rest** (Organisation of Working Time Law s.6) is approximated as "at
  most six days a week". The 24 uninterrupted hours the law asks for are not
  modelled.
- **The 48-hour week is enforced per week**, not averaged over four months as
  s.7 permits. This is stricter than the law and will be wrong in a peak season.
- **Night work** (s.9) is absent. No shape at Polemidia crosses midnight; a
  24-hour branch would need it.
- **Fairness is still a hard constraint.** Corrected, it no longer empties a
  roster, but a target that cannot be met should degrade rather than forbid —
  it belongs in the objective as a penalised deviation. It is also skipped on any
  window shorter than a week, where pro-rated entitlements make the ratios
  meaningless; measuring over a rolling four weeks would remove that special case.
- **One grade, one wage.** Stage 1 prices labour at the branch mean, which is
  exact at Polemidia and will misorder shapes at a branch with mixed grades.
- **No persistence.** The caller supplies the whole scenario on every request.
  A real deployment needs a database behind it, authentication, and a queue for
  solves that outlast a request timeout.
- **The template catalogue is fitted in-sample**, on the same month the model is
  evaluated on. Rebuild it from earlier months before claiming four shapes are
  enough.
- **Demand is quantised to whole baristas per hour**, so a ±10% change in trade
  is invisible to the model. The 05:00 hour asks for two to three baristas while
  the branch opens at 05:30, which the validator now flags.

## Licence

Internal to the project. Clock Coffee's data is not redistributable; the fixture
in `tests/fixtures/` carries employee identifiers and wages and should not leave
the team.
