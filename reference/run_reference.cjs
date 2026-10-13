/* Run the browser prototype's solver on the pilot month and print a summary
   the Python test suite can compare against, so the two implementations are
   one model rather than two that are hoped to agree.

   Usage:  node reference/run_reference.cjs [branchId]
   Needs:  npm install            (inside reference/)
*/
const path = require('path');
const fs = require('fs');

const S = require('./solver.js');
const seed = JSON.parse(fs.readFileSync(path.join(__dirname, 'seed.json'), 'utf8'));
const branchId = Number(process.argv[2] || 43);
// Which configuration to run. `legacy` reproduces the behaviour this solver had
// before the port uncovered its defects; `corrected` is what it does now. The
// Python model must agree with it in both.
const MODE = (process.argv[3] || 'legacy');
const PARAMS = MODE === 'legacy'
  ? { cover_mode: 'indicator', break_treatment: 'smear', prorate_floor: false }
  : {};
const br = seed.branches.find(b => b.id === branchId);
if (!br) { console.error('no such branch: ' + branchId); process.exit(2); }

let Module;
try {
  Module = require('highs');
} catch (e) {
  console.error(JSON.stringify({ error: 'highs not installed; run `npm install` in reference/' }));
  process.exit(3);
}

const weeks = [];
{
  let cur = [];
  seed.dates.forEach(iso => {
    if (S.DOW(iso) === 0 && cur.length) { weeks.push(cur); cur = []; }
    cur.push(iso);
  });
  if (cur.length) weeks.push(cur);
}

(async () => {
  const highs = await Module();
  let all = [], carry = {}, unfilled = 0, positions = 0, ms = 0;
  const opened = {};

  for (const days of weeks) {
    const r = S.planWeek(highs, br, days, br.demand, PARAMS, { avail: {}, carryEnd: carry });
    all = all.concat(r.assign);
    unfilled += r.unfilled.length;
    positions += r.positions;
    ms += r.ms;
    days.forEach(d => r.stage1[d].counts.forEach((c, i) => { opened[i] = (opened[i] || 0) + c; }));
    carry = {};
    r.assign.filter(a => a.date === days[days.length - 1]).forEach(a => {
      const e = S.dayNo(a.date) * 1440 + a.end;
      if (!carry[a.emp] || e > carry[a.emp]) carry[a.emp] = e;
    });
  }

  const rows = all.map(a => ({ date: a.date, emp: a.emp, start: a.start, end: a.end, hours: a.hours }));
  const cov = S.coverage(rows, br, br.demand, PARAMS);
  const au = S.audit(rows, PARAMS);

  const hours = {};
  rows.forEach(r => { hours[r.emp] = (hours[r.emp] || 0) + r.hours; });
  const straight = rows.reduce((s, r) => {
    const e = br.employees.find(x => x.id === r.emp);
    return s + r.hours * (e ? e.cost : 0);
  }, 0);

  const byWeek = {};
  rows.forEach(r => {
    const w = S.dayNo(r.date) - S.DOW(r.date);
    byWeek[w] = byWeek[w] || {};
    byWeek[w][r.emp] = (byWeek[w][r.emp] || 0) + r.hours;
  });
  let overtime = 0;
  Object.values(byWeek).forEach(x => Object.values(x).forEach(h => {
    overtime += Math.max(0, h - 40);
  }));

  const round = (x, n) => Math.round(x * 10 ** n) / 10 ** n;
  console.log(JSON.stringify({
    branch: branchId,
    mode: MODE,
    shifts: rows.length,
    positions,
    unfilled,
    templates_opened: opened,
    straight_cost: round(straight, 2),
    overtime_hours: round(overtime, 2),
    coverage_required: cov.need,
    coverage_short: cov.short,
    coverage_pct: round(cov.pct, 6),
    breaches: au.total,
    people: Object.keys(hours).length,
    hours_min: round(Math.min(...Object.values(hours)), 2),
    hours_max: round(Math.max(...Object.values(hours)), 2),
    solve_ms: ms,
  }, null, 1));
})();
