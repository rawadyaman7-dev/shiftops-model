/* =======================================================================
   ShiftOps solver — the two-stage MIP, as one module.

   Stage 1  chooses how many of each shift template to run on a day, to
            cover the hourly barista requirement at least cost.
   Stage 2  assigns named employees to the positions stage 1 produced,
            under availability, rest, hour and fairness constraints.

   Both stages are written as LP files and solved with HiGHS. The same
   file runs under Node (for validation) and in the browser (in the
   dashboard) — there is no second implementation.
   ======================================================================= */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.SOLVER = factory();
}(typeof self !== 'undefined' ? self : this, function () {
'use strict';

/* ---------------------------------------------------------------- defaults */
const DEFAULTS = {
  // --- cost objective
  overtime_threshold: 40,     // hours a week above which the premium applies
  overtime_rate: 1.5,         // multiplier on the hourly cost for those hours
  short_penalty: 60,          // EUR charged per barista-hour left uncovered
  spare_penalty: 2,           // EUR charged per barista-hour of overstaffing
  shift_penalty: 1.5,         // EUR per shift opened — discourages fragmentation

  // --- legal and operational limits
  max_weekly_hours: 48,       // CY s7, taken as a per-week cap (the stricter proxy)
  // Cyprus sets no universal daily limit. 13 h is what the 11-hour rest rule
  // implies inside any 24 hours. A tighter figure is a policy choice — and at
  // Polemidia anything below 9.5 outlaws the branch's own main shift.
  max_daily_hours: 13,
  min_rest_hours: 11,         // CY s4
  max_days_per_week: 6,       // CY s6, as a day count
  weekly_rest_hours: 24,      // CY s6, as a continuous window
  break_minutes: 15,          // CY s5
  break_threshold_hours: 6,   // shift length at which the break becomes due
  // How the break enters the model.
  //   ignore -- it does not reduce cover, on the reading that a colleague covers
  //             the floor for fifteen minutes. The default.
  //   smear  -- spread evenly over the shift, as this solver used to do. It makes
  //             a requirement of three baristas unmeetable by three shifts.
  // A break scheduled as a real interruption needs the model at 15-minute
  // granularity and is not implemented.
  break_treatment: 'ignore',
  // How a shift counts towards an hour it only partly covers.
  //   fractional -- the minutes actually worked: 05:30-15:00 gives half of 05:00.
  //   indicator  -- all or nothing at the half-hour mark, as this solver used to
  //                 do. It credits 31 minutes with a whole hour and 29 with none.
  cover_mode: 'fractional',

  // --- shift structure
  max_distinct_shifts: 5,     // K in MIP 1
  cap_by_headcount: true,     // never open more shifts than there are people

  // --- fairness. The constraint set calls this an operational TARGET, not a
  //     mandatory rule, so it is off unless the manager turns it on.
  fairness_mode: 'off',       // 'off' | 'absolute' | 'relative'
  fairness_delta: 10,         // hours, for 'absolute'
  fairness_pct: 0.10,         // share of requested hours, for 'relative'
  fairness_used_only: true,   // absolute mode: only bind people who get a shift

  // --- respect the hours each employee said they are available for
  respect_declared_hours: true,
  // Lift the pro-rated declared-hours cap to the longest shift in the catalogue
  // when the window is shorter than a week. Without it a one-day window gives a
  // 45-hour-a-week barista 6.4 hours and the day cannot be staffed at all.
  prorate_floor: true,
  // Plan only this share of those hours, keeping the rest free to absorb a
  // disruption later in the week. 0 plans people right up to what they asked for.
  plan_reserve: 0,

  // --- used by the app rather than the model, but settled in the same place
  notice_hours: 48,              // how far ahead a roster should be published
  budget_fortnight: 3200,        // EUR of barista pay a fortnight is allowed

  // --- covering a disruption inside a week that is already published.
  // These are the branch's own rules, not law, and every one of them is meant
  // to be argued with — which is why they live here rather than in the code.
  cover_band_minutes: 60,        // how close a past shift's start counts as "the same time of day"
  cover_lookback_days: 30,       // how far back that experience is looked for
  cover_require_experience: true,// must they have worked this time of day, or is it only preferred?
  cover_allow_beyond_asked: true,// may cover push someone past the hours they asked for?
  cover_rank: 'rule',            // 'rule' | 'cost' | 'load'

  // --- solver
  time_limit: 20,
  mip_gap: 0.0,
};

/* The branch's own cover rule, as the manager stated it: someone counts as
   experienced on a shift if they have worked one starting within BANDW minutes
   of it at some point in the last WINDOWD days. */
const BANDW = 60, WINDOWD = 30;

/* ------------------------------------------------------------- small utils */
const hhmm = m => String(Math.floor((m % 1440) / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
const DOW = iso => (new Date(iso + 'T00:00:00Z').getUTCDay() + 6) % 7;
const addDays = (iso, k) => { const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + k); return d.toISOString().slice(0, 10); };
const dayNo = iso => Math.round(new Date(iso + 'T00:00:00Z') / 864e5);
const num = x => (Math.round(x * 1e6) / 1e6);
const fmt = x => { const v = num(x); return (v >= 0 ? '+ ' : '- ') + Math.abs(v); };

/** Minutes of template t that fall inside clock hour h. */
function coverMinutes(tpl, h, P) {
  const lo = Math.max(tpl.start, h * 60), hi = Math.min(tpl.end, (h + 1) * 60);
  let m = Math.max(0, hi - lo);
  if (!m || P.break_treatment !== 'smear') return m;
  if (tpl.hours <= P.break_threshold_hours) return m;
  return m * (1 - P.break_minutes / (tpl.hours * 60));
}
/** How much of one barista-hour this template contributes to that hour. */
function covers(tpl, h, P) {
  const m = coverMinutes(tpl, h, P);
  if (P.cover_mode === 'indicator') return m >= 30 ? 1 : 0;
  return m / 60;
}

/* ===================================================================
   STAGE 1 — shift structure for one day
   =================================================================== */
function stage1LP(branch, iso, R, params, headcount) {
  const P = Object.assign({}, DEFAULTS, params);
  const T = branch.templates, H0 = 4, H1 = 24;
  const wage = branch.employees.reduce((s, e) => s + e.cost, 0) / branch.employees.length;
  const obj = [], cons = [], bin = [], gen = [], bnd = [];

  T.forEach((t, i) => {
    obj.push(`${fmt(t.hours * wage + P.shift_penalty)} n_${i}`);
    gen.push(`n_${i}`); bin.push(`y_${i}`);
    bnd.push(` 0 <= n_${i} <= ${Math.max(1, headcount)}`);
    cons.push(` act_${i}: n_${i} - ${Math.max(1, headcount)} y_${i} <= 0`);
  });
  for (let h = H0; h < H1; h++) {
    const need = R[h - H0] || 0;
    const terms = T.map((t, i) => {
      const c = covers(t, h, P);
      return c > 0 ? `${fmt(c)} n_${i}` : null;
    }).filter(Boolean);
    if (!terms.length && !need) continue;
    cons.push(` cov_${h}: ${terms.join(' ') || '0 n_0'} + sh_${h} - sp_${h} = ${need}`);
    obj.push(`+ ${P.short_penalty} sh_${h}`, `+ ${P.spare_penalty} sp_${h}`);
    bnd.push(` 0 <= sh_${h} <= ${need}`, ` 0 <= sp_${h} <= 50`);
  }
  cons.push(` kcap: ${T.map((_, i) => `+ y_${i}`).join(' ')} <= ${P.max_distinct_shifts}`);
  if (P.cap_by_headcount) cons.push(` hcap: ${T.map((_, i) => `+ n_${i}`).join(' ')} <= ${headcount}`);

  return `Minimize\n obj: ${obj.join(' ')}\nSubject To\n${cons.join('\n')}\nBounds\n${bnd.join('\n')}\n`
       + `General\n ${gen.join(' ')}\nBinary\n ${bin.join(' ')}\nEnd\n`;
}

function stage1(highs, branch, iso, R, params, headcount) {
  const P = Object.assign({}, DEFAULTS, params);
  const lp = stage1LP(branch, iso, R, P, headcount);
  const sol = highs.solve(lp, { time_limit: P.time_limit, mip_rel_gap: P.mip_gap });
  const col = k => (sol.Columns[k] ? Math.round(sol.Columns[k].Primal) : 0);
  const positions = [];
  branch.templates.forEach((t, i) => { for (let k = 0; k < col(`n_${i}`); k++) positions.push(i); });
  let short = 0, spare = 0;
  for (let h = 4; h < 24; h++) { short += col(`sh_${h}`); spare += col(`sp_${h}`); }
  return {
    date: iso, status: sol.Status, positions,
    counts: branch.templates.map((_, i) => col(`n_${i}`)),
    hours: positions.reduce((s, i) => s + branch.templates[i].hours, 0),
    short, spare, cost: sol.ObjectiveValue,
  };
}

/* ===================================================================
   STAGE 2 — assign employees to one week of positions
   =================================================================== */
/** The part of the day this person will work on that weekday. */
function windowOn(a, d) {
  if (a.win7 && a.win7[d]) return a.win7[d];
  if (a.days && !a.days[d]) return 'off';
  return a.window || 'any';
}
/** Does that window contain the whole of this shift? */
function windowFits(w, tpl) {
  if (w === 'off') return false;
  if (w === 'morning') return tpl.end <= 16 * 60;
  if (w === 'evening') return tpl.start >= 12 * 60;
  return true;
}
/** Is employee e able to take template t on day iso? */
function available(emp, iso, tpl, avail) {
  const a = Object.assign({}, emp.avail, avail[emp.id] || {});
  if (a.off && a.off.indexOf(iso) >= 0) return 0;          // absence / approved day off
  return windowFits(windowOn(a, DOW(iso)), tpl) ? 1 : 0;
}

function stage2LP(branch, days, posByDay, avail, params, locked, carryEnd) {
  const P = Object.assign({}, DEFAULTS, params);
  const T = branch.templates, E = branch.employees;
  const obj = [], cons = [], bin = [], gen = [], bnd = [];
  const used = {};                                   // which (d,t) pairs exist

  // --- which templates are needed on each day, and how many of each
  days.forEach((iso, di) => {
    const need = {};
    (posByDay[iso] || []).forEach(ti => need[ti] = (need[ti] || 0) + 1);
    Object.keys(need).forEach(ti => { used[di + '_' + ti] = need[ti]; });
  });

  // --- variables and the staffing equation
  Object.keys(used).forEach(key => {
    const [di, ti] = key.split('_').map(Number);
    const terms = [];
    E.forEach((e, ei) => {
      if (!available(e, days[di], T[ti], avail)) return;
      bin.push(`x_${ei}_${di}_${ti}`); terms.push(`+ x_${ei}_${di}_${ti}`);
    });
    gen.push(`u_${di}_${ti}`); bnd.push(` 0 <= u_${di}_${ti} <= ${used[key]}`);
    cons.push(` st_${di}_${ti}: ${terms.join(' ') || '0 u_' + di + '_' + ti} + u_${di}_${ti} = ${used[key]}`);
    obj.push(`+ ${num(P.short_penalty * T[ti].hours)} u_${di}_${ti}`);
  });
  const has = (ei, di, ti) => bin.indexOf(`x_${ei}_${di}_${ti}`) >= 0;
  const binSet = new Set(bin);
  const V = (ei, di, ti) => binSet.has(`x_${ei}_${di}_${ti}`) ? `x_${ei}_${di}_${ti}` : null;

  // --- per employee
  E.forEach((e, ei) => {
    const mine = [];
    days.forEach((iso, di) => {
      const todays = [];
      T.forEach((t, ti) => { const v = V(ei, di, ti); if (v && used[di + '_' + ti]) { todays.push([v, t]); mine.push([v, t, di]); } });
      if (!todays.length) return;
      bin.push(`y_${ei}_${di}`);
      // daily hours, workday linking
      cons.push(` dh_${ei}_${di}: ${todays.map(([v, t]) => `${fmt(t.hours)} ${v}`).join(' ')} <= ${P.max_daily_hours}`);
      todays.forEach(([v]) => cons.push(` lk_${v}: ${v} - y_${ei}_${di} <= 0`));
      // same-day rest: two shifts on one day must still leave 11 h inside the 24 h
      for (let a = 0; a < todays.length; a++) for (let b = a + 1; b < todays.length; b++) {
        const [va, ta] = todays[a], [vb, tb] = todays[b];
        const gap = Math.max(tb.start - ta.end, ta.start - tb.end) / 60;
        const wrap = 24 - (Math.max(ta.end, tb.end) - Math.min(ta.start, tb.start)) / 60;
        if (Math.max(gap, wrap) < P.min_rest_hours) cons.push(` sr_${va}_${vb}: ${va} + ${vb} <= 1`);
      }
    });
    if (!mine.length) return;
    // weekly hours: the legal cap, and the hours the employee said they have
    // The legal cap is law; the hours someone asked for are a preference.
    // Pro-rating that preference by window length is right in the middle of a
    // week and wrong at its edges: a one-day window would cap a 45-hour-a-week
    // barista at 6.4 hours, shorter than any shift, so nobody can be assigned
    // anything at all. The floor lifts it to the longest shift in the catalogue.
    const declared = (avail[e.id] || e.avail).hours;
    let cap = P.max_weekly_hours;
    if (P.respect_declared_hours && declared > 0) {
      let pro = declared * (1 - (P.plan_reserve || 0)) * (days.length / 7);
      if (P.prorate_floor !== false) {
        const longest = Math.max.apply(null, T.map(t => t.hours));
        pro = Math.max(pro, Math.min(longest, P.max_weekly_hours));
      }
      cap = Math.min(P.max_weekly_hours, pro);
    }
    cons.push(` wk_${ei}: ${mine.map(([v, t]) => `${fmt(t.hours)} ${v}`).join(' ')} <= ${num(cap)}`);
    const yd = days.map((_, di) => binSet.has(`y_${ei}_${di}`) || bin.indexOf(`y_${ei}_${di}`) >= 0 ? `+ y_${ei}_${di}` : null).filter(Boolean);
    if (yd.length) cons.push(` dy_${ei}: ${yd.join(' ')} <= ${P.max_days_per_week}`);
    // rest across days, including the shift carried in from the previous week
    days.forEach((iso, di) => T.forEach((t, ti) => {
      const v = V(ei, di, ti); if (!v || !used[di + '_' + ti]) return;
      // carried-in end time
      if (di === 0 && carryEnd && carryEnd[e.id] != null) {
        const rest = (dayNo(iso) * 1440 + t.start - carryEnd[e.id]) / 60;
        if (rest >= 0 && rest < P.min_rest_hours) cons.push(` cr_${v}: ${v} = 0`);
      }
      for (let dj = di + 1; dj < Math.min(days.length, di + 2); dj++) T.forEach((t2, tj) => {
        const w = V(ei, dj, tj); if (!w || !used[dj + '_' + tj]) return;
        const rest = ((dayNo(days[dj]) * 1440 + t2.start) - (dayNo(iso) * 1440 + t.end)) / 60;
        if (rest >= 0 && rest < P.min_rest_hours) cons.push(` rs_${v}_${w}: ${v} + ${w} <= 1`);
      });
    }));
    // overtime above the threshold
    // o_e stays continuous: as an integer it charged a 47.5-hour week for eight
    // premium hours instead of seven and a half.
    bnd.push(` 0 <= o_${ei} <= ${P.max_weekly_hours}`);
    cons.push(` ot_${ei}: ${mine.map(([v, t]) => `${fmt(t.hours)} ${v}`).join(' ')} - o_${ei} <= ${P.overtime_threshold}`);
    obj.push(`+ ${num(e.cost * (P.overtime_rate - 1))} o_${ei}`);
    // straight-time cost
    mine.forEach(([v, t]) => obj.push(`+ ${num(e.cost * t.hours)} ${v}`));
  });

  // --- fairness
  const hoursExpr = ei => {
    const terms = [];
    days.forEach((iso, di) => T.forEach((t, ti) => { const v = V(ei, di, ti); if (v && used[di + '_' + ti]) terms.push(`${fmt(t.hours)} ${v}`); }));
    return terms;
  };
  // Fairness is a statement about a week's work. Pro-rated to a one-day window
  // it says something absurd instead -- one shift lands someone at three times
  // their share -- and then forbids the roster rather than balancing it.
  const fairWindow = days.length >= 7;
  if (P.fairness_mode === 'absolute' && fairWindow) {
    // max - min <= delta, as 2|E| constraints rather than |E|^2 pairwise ones
    const BIG = 300;
    let any = false;
    E.forEach((e, ei) => {
      const h = hoursExpr(ei); if (!h.length) return;
      cons.push(` f1_${ei}: ${h.join(' ')} - Hmax <= 0`);
      if (P.fairness_used_only) {
        // w = 1 when this person works at all; the lower bound only binds then
        const yv = days.map((_, di) => bin.indexOf(`y_${ei}_${di}`) >= 0 ? `y_${ei}_${di}` : null).filter(Boolean);
        if (yv.length) {
          bin.push(`w_${ei}`);
          yv.forEach(y => cons.push(` fa_${y}: ${y} - w_${ei} <= 0`));            // works => w = 1
          cons.push(` fb_${ei}: ${yv.map(y => `+ ${y}`).join(' ')} - w_${ei} >= 0`); // w = 1 => works
          // w = 0 must make this row slack, not impossible. With the inequality
          // the other way round the exemption could never fire, and the relative
          // form below returned an empty roster labelled optimal.
          cons.push(` f2_${ei}: ${h.join(' ')} - Hmin - ${BIG} w_${ei} >= ${-BIG}`);
        } else cons.push(` f2_${ei}: ${h.join(' ')} - Hmin >= 0`);
      } else {
        cons.push(` f2_${ei}: ${h.join(' ')} - Hmin >= 0`);
      }
      any = true;
    });
    if (any) {
      cons.push(` f3: Hmax - Hmin <= ${P.fairness_delta}`);
      bnd.push(' 0 <= Hmax <= 200', ' 0 <= Hmin <= 200');
    }
  } else if (P.fairness_mode === 'relative' && fairWindow) {
    // Everyone gets a similar SHARE of the hours they asked for:
    //   | H_e/req_e  -  H_f/req_f |  <=  pct
    // Written with Rmax/Rmin so it stays 2|E| constraints, and relaxed for
    // anyone who gets no shift at all (asking for hours is not a promise of them).
    const BIG = 10;
    let any = false;
    E.forEach((e, ei) => {
      const h = hoursExpr(ei); if (!h.length) return;
      const req = (avail[e.id] || e.avail).hours * (days.length / 7);
      if (req <= 0) return;
      cons.push(` g1_${ei}: ${h.join(' ')} ${fmt(-req)} Rmax <= 0`);
      const yv = days.map((_, di) => bin.indexOf(`y_${ei}_${di}`) >= 0 ? `y_${ei}_${di}` : null).filter(Boolean);
      if (yv.length) {
        bin.push(`w_${ei}`);
        yv.forEach(y => cons.push(` ga_${y}: ${y} - w_${ei} <= 0`));
        cons.push(` gb_${ei}: ${yv.map(y => `+ ${y}`).join(' ')} - w_${ei} >= 0`);
        cons.push(` g2_${ei}: ${h.join(' ')} ${fmt(-req)} Rmin - ${num(BIG * req)} w_${ei} >= ${num(-BIG * req)}`);
      } else {
        cons.push(` g2_${ei}: ${h.join(' ')} ${fmt(-req)} Rmin >= 0`);
      }
      any = true;
    });
    if (any) {
      cons.push(` g3: Rmax - Rmin <= ${P.fairness_pct}`);
      bnd.push(' 0 <= Rmax <= 10', ' 0 <= Rmin <= 10');
    }
  }

  // --- locked assignments (a swap the manager already approved)
  (locked || []).forEach((l, k) => {
    const di = days.indexOf(l.date); if (di < 0) return;
    const ei = E.findIndex(e => e.id === l.emp); if (ei < 0) return;
    const v = V(ei, di, l.tpl); if (v) cons.push(` lock_${k}: ${v} = 1`);
  });

  const uniq = a => [...new Set(a)];
  return `Minimize\n obj: ${obj.join(' ')}\nSubject To\n${cons.join('\n')}\n`
       + `Bounds\n${uniq(bnd).join('\n')}\nGeneral\n ${uniq(gen).join(' ')}\n`
       + `Binary\n ${uniq(bin).join(' ')}\nEnd\n`;
}

function stage2(highs, branch, days, posByDay, avail, params, locked, carryEnd) {
  const P = Object.assign({}, DEFAULTS, params);
  const lp = stage2LP(branch, days, posByDay, avail, P, locked, carryEnd);
  const t0 = Date.now();
  const sol = highs.solve(lp, { time_limit: P.time_limit, mip_rel_gap: P.mip_gap });
  const T = branch.templates, E = branch.employees;
  const assign = [], unfilled = [];
  const hours = {}, cost = {};
  if (sol.Columns) {
    Object.entries(sol.Columns).forEach(([k, c]) => {
      if (k[0] === 'x' && c.Primal > 0.5) {
        const [, ei, di, ti] = k.split('_').map(Number);
        const e = E[ei], t = T[ti];
        assign.push({ date: days[di], emp: e.id, tpl: ti, start: t.start, end: t.end, hours: t.hours });
        hours[e.id] = (hours[e.id] || 0) + t.hours;
        cost[e.id] = num((cost[e.id] || 0) + t.hours * e.cost);
      }
      if (k[0] === 'u' && c.Primal > 0.5) {
        const [, di, ti] = k.split('_').map(Number);
        for (let n = 0; n < Math.round(c.Primal); n++) unfilled.push({ date: days[di], tpl: ti });
      }
    });
  }
  assign.sort((a, b) => a.date < b.date ? -1 : a.date > b.date ? 1 : a.start - b.start);
  const hv = Object.values(hours);
  return {
    status: sol.Status, ms: Date.now() - t0, objective: sol.ObjectiveValue,
    assign, unfilled, hours, cost,
    totalHours: num(hv.reduce((s, v) => s + v, 0)),
    totalCost: num(Object.values(cost).reduce((s, v) => s + v, 0)),
    gap: hv.length ? num(Math.max(...hv) - Math.min(...hv)) : 0,
    used: hv.length,
  };
}

/* ===================================================================
   Plan a stretch of days end to end
   =================================================================== */
function planWeek(highs, branch, days, demand, params, opts) {
  const P = Object.assign({}, DEFAULTS, params);
  opts = opts || {};
  const avail = opts.avail || {};
  const posByDay = {}, s1 = {};
  days.forEach(iso => {
    const headcount = branch.employees.filter(e =>
      branch.templates.some(t => available(e, iso, t, avail))).length;
    const r = stage1(highs, branch, iso, demand[iso].R, P, headcount);
    s1[iso] = r; posByDay[iso] = r.positions;
  });
  const r2 = stage2(highs, branch, days, posByDay, avail, P, opts.locked, opts.carryEnd);
  return { days, stage1: s1, ...r2,
    positions: days.reduce((s, d) => s + posByDay[d].length, 0),
    plannedHours: num(days.reduce((s, d) => s + s1[d].hours, 0)) };
}

/* ===================================================================
   COVER — repairing one disrupted shift inside a week that is already out.

   Re-solving the MIP would reshuffle people who had nothing to do with the
   disruption, so a published week is repaired with the branch's own rules
   instead: who has worked this time of day recently, who is free, who is
   legal, and then who is cheapest.
   =================================================================== */
function coverCandidates(branch, slot, ctx) {
  const P = Object.assign({}, DEFAULTS, ctx.params);
  const avail = ctx.avail || {};
  const roster = ctx.roster || [];            // every published row, all days
  const history = ctx.history || [];          // {emp, date, start, end} actually worked
  const tpl = branch.templates[slot.tpl];
  const BAND = P.cover_band_minutes, BACK = P.cover_lookback_days;
  const day = slot.date, di = dayNo(day), dw = DOW(day);
  const sStart = di * 1440 + tpl.start, sEnd = di * 1440 + tpl.end;
  const from = addDays(day, -BACK);
  const weekMon = addDays(day, -dw);
  const weekEnd = addDays(weekMon, 6);

  return branch.employees.map(e => {
    const a = Object.assign({}, e.avail, avail[e.id] || {});
    const blocked = [], notes = [];

    // --- the rule the branch already uses: same time band, recently
    const band = history.filter(r => r.emp === e.id && r.date >= from && r.date < day
                                     && Math.abs(r.start - tpl.start) <= BAND);
    const lastBand = band.length ? band[band.length - 1].date : null;
    const noExp = !band.length;

    // --- away
    if (String(e.id) === String(slot.was)) blocked.push('this is their own shift');
    if ((a.off || []).indexOf(day) >= 0) blocked.push('away that day');

    // --- availability as they declared it, day by day
    const DN = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][dw];
    const w = windowOn(a, dw);
    if (w === 'off') blocked.push('does not work ' + DN + 's');
    else if (!windowFits(w, tpl)) blocked.push(DN + 's are ' + w + 's only for them');

    // --- already on that day, at any time
    const sameDay = roster.filter(r => r.emp === e.id && r.date === day);
    if (sameDay.length) blocked.push('already on ' + hhmm(sameDay[0].start));

    // --- rest either side, measured on the published roster
    let tightest = null;
    roster.filter(r => r.emp === e.id).forEach(r => {
      const rs = dayNo(r.date) * 1440 + r.start, re = dayNo(r.date) * 1440 + r.end;
      if (re <= sStart) { const g = (sStart - re) / 60; if (tightest === null || g < tightest) tightest = g; }
      else if (rs >= sEnd) { const g = (rs - sEnd) / 60; if (tightest === null || g < tightest) tightest = g; }
    });
    if (tightest !== null && tightest < P.min_rest_hours)
      blocked.push('only ' + num(tightest) + ' h rest');

    // --- hours and days that week
    const wk = roster.filter(r => r.emp === e.id && r.date >= weekMon && r.date <= weekEnd);
    const wkHours = wk.reduce((s, r) => s + r.hours, 0);
    const wkDays = new Set(wk.map(r => r.date)).size;
    // The weekly legal cap is a hard stop. The hours someone asked for are a
    // preference — cover may go past it, said plainly, and they can still decline.
    const asked = P.respect_declared_hours && a.hours > 0 ? a.hours : P.max_weekly_hours;
    const after = wkHours + tpl.hours;
    let beyondAsked = 0;
    if (after > P.max_weekly_hours)
      blocked.push('would reach ' + num(after) + ' h, past the ' + num(P.max_weekly_hours) + ' h weekly limit');
    else if (after > asked) {
      beyondAsked = num(after - asked);
      notes.push(num(beyondAsked) + ' h more than the ' + num(asked) + ' h they asked for');
    }
    if (wkDays + 1 > P.max_days_per_week) blocked.push('would be a ' + (wkDays + 1) + 'th day');

    // --- what it would cost
    const over = Math.max(0, (wkHours + tpl.hours) - P.overtime_threshold)
               - Math.max(0, wkHours - P.overtime_threshold);
    const cost = num(tpl.hours * e.cost + over * e.cost * (P.overtime_rate - 1));
    if (over > 0) notes.push(num(over) + ' h of it at the overtime rate');
    if (noExp) {
      const msg = 'has not worked this time of day in the last ' + BACK + ' days';
      if (P.cover_require_experience) blocked.push(msg); else notes.push(msg);
    }
    if (beyondAsked && !P.cover_allow_beyond_asked)
      blocked.push('past the ' + num(asked) + ' h they asked for');

    // Someone stopped ONLY by the experience rule is still worth showing: the
    // manager may know something the last 30 days do not.
    const expOnly = blocked.length === 1 && noExp && P.cover_require_experience;
    return {
      emp: e.id, eligible: !blocked.length, expOnly, blocked, notes,
      band: band.length, lastBand, cost, over, beyondAsked,
      wkHours: num(wkHours), wkDays, asked: num(asked), rest: tightest,
    };
  }).sort((x, y) => {
    if (x.eligible !== y.eligible) return x.eligible ? -1 : 1;
    if (x.expOnly !== y.expOnly) return x.expOnly ? -1 : 1;
    if (P.cover_rank === 'cost') {
      if (x.cost !== y.cost) return x.cost - y.cost;
    } else if (P.cover_rank === 'load') {
      if (x.wkHours !== y.wkHours) return x.wkHours - y.wkHours;
    } else {                                                        // 'rule'
      if (!!x.band !== !!y.band) return x.band ? -1 : 1;
    }
    if (!x.beyondAsked !== !y.beyondAsked) return x.beyondAsked ? 1 : -1;
    if (x.cost !== y.cost) return x.cost - y.cost;
    if (x.wkHours !== y.wkHours) return x.wkHours - y.wkHours;
    return y.band - x.band;
  });
}

/* ------------------------------------------------------------ compliance */
/** Audit any roster — ours or the real one — against the same rule set. */
function audit(rows, params) {
  const P = Object.assign({}, DEFAULTS, params);
  const byEmp = {};
  rows.forEach(r => { (byEmp[r.emp] = byEmp[r.emp] || []).push(r); });
  const out = { rest11: [], daily: [], weekly: [], days7: [], overlap: [], weeklyRest: [] };
  Object.entries(byEmp).forEach(([id, list]) => {
    list.sort((a, b) => (dayNo(a.date) * 1440 + a.start) - (dayNo(b.date) * 1440 + b.start));
    let covEnd = -Infinity, covRow = null;
    const byDay = {}, byWeek = {};
    list.forEach(r => {
      const s = dayNo(r.date) * 1440 + r.start, e = dayNo(r.date) * 1440 + r.end;
      if (covRow) {
        const gap = (s - covEnd) / 60;
        if (gap < 0) out.overlap.push({ emp: +id, a: covRow, b: r, v: -gap });
        else if (gap < P.min_rest_hours) out.rest11.push({ emp: +id, a: covRow, b: r, v: num(gap) });
      }
      if (e > covEnd) { covEnd = e; covRow = r; }
      byDay[r.date] = (byDay[r.date] || 0) + r.hours;
      const wk = dayNo(r.date) - DOW(r.date);                   // the Monday of that week
      byWeek[wk] = byWeek[wk] || { h: 0, days: new Set() };
      byWeek[wk].h += r.hours; byWeek[wk].days.add(r.date);
    });
    Object.entries(byDay).forEach(([d, h]) => { if (h > P.max_daily_hours) out.daily.push({ emp: +id, date: d, v: num(h) }); });
    Object.entries(byWeek).forEach(([w, x]) => {
      if (x.h > P.max_weekly_hours) out.weekly.push({ emp: +id, week: +w, v: num(x.h) });
      if (x.days.size > P.max_days_per_week) out.days7.push({ emp: +id, week: +w, v: x.days.size });
    });
  });
  out.total = out.rest11.length + out.daily.length + out.weekly.length + out.days7.length + out.overlap.length;
  return out;
}

/** Hour-by-hour cover a roster delivers, against the requirement. */
function coverage(rows, branch, demand, params) {
  const P = Object.assign({}, DEFAULTS, params);
  const byDate = {};
  rows.forEach(r => (byDate[r.date] = byDate[r.date] || []).push(r));
  let short = 0, spare = 0, need = 0, got = 0;
  const days = {};
  Object.keys(demand).forEach(iso => {
    const R = demand[iso].R, list = byDate[iso] || [];
    let s = 0, p = 0;
    for (let h = 4; h < 24; h++) {
      const want = R[h - 4] || 0;
      let have = 0;
      list.forEach(r => { have += covers({ start: r.start, end: r.end, hours: r.hours }, h, P); });
      need += want; got += Math.min(have, want);
      if (have < want) s += want - have; else p += have - want;
    }
    short += s; spare += p; days[iso] = { short: s, spare: p };
  });
  return { short, spare, need, got, pct: need ? num(got / need) : 1, days };
}

return { DEFAULTS, stage1, stage2, planWeek, audit, coverage, coverCandidates,
         stage1LP, stage2LP,          /* exposed so the model can be inspected */
         available, windowOn, windowFits, covers, hhmm, DOW, addDays, dayNo };
}));
