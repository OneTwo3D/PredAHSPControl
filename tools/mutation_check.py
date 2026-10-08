"""Mutation check from review round 6.

Re-introduces each fixed defect / previously untested behaviour and confirms at least one test fails
("killed"). Run from the repository root::

    CORE_PY=.venv/bin/python HA_PY=.venv-ha/bin/python python tools/mutation_check.py [name filter ...]

``CORE_PY`` runs ``tests/``; ``HA_PY`` must have pytest-homeassistant-custom-component for ``tests_ha/``.
Files are restored after each mutation.
"""

import os
import pathlib
import subprocess
import sys

CORE = [
    os.environ.get("CORE_PY", sys.executable),
    "-m",
    "pytest",
    "-q",
    "-x",
    "tests",
    "-p",
    "no:cacheprovider",
]
HA = [
    os.environ.get("HA_PY", sys.executable),
    *("-m", "pytest", "-q", "-x", "-o", "asyncio_mode=auto", "tests_ha", "-p", "no:cacheprovider"),
]
C = "custom_components/daikin_mpc/"
M = [
    (
        "A1 first_partial not persisted",
        C + "core/accumulator.py",
        'self._first_partial = True if day is None else bool(d.get("first_partial", True))',
        "self._first_partial = day is None",
        CORE,
    ),
    (
        "A2/R7-2 late counter start ignored",
        C + "core/accumulator.py",
        "any(h.counter_gap or h.counter_restart for h in self._hours)",
        "any(h.counter_gap for h in self._hours)",
        CORE,
    ),
    (
        "A3/B5 day length forced 24",
        C + "core/accumulator.py",
        "length_h = day_length_h(date.fromisoformat(self._day), self._tz)",
        "length_h = 24.0",
        CORE,
    ),
    # "A3 tz from restored hour" retired: superseded by passing the named zone on restore (R7-3)
    ("A4 single fold", C + "core/cost_model.py", "for fold in (0, 1):", "for fold in (0,):", CORE),
    (
        "A4 wall-clock window",
        C + "core/cost_model.py",
        "return elapsed_s(start, x) >= 0 and elapsed_s(x, t) > 0",
        "return start <= x < t",
        CORE,
    ),
    (
        "A5 hours without temps accepted",
        C + "core/accumulator.py",
        'raise ValueError("usable hour without temperature means")',
        "pass",
        CORE,
    ),
    (
        "B1 WS origin check off",
        "tools/ha_client.py",
        "if (final.scheme, final.netloc) != urllib.parse.urlsplit(ws_url)[:2]:",
        "if False:",
        CORE,
    ),
    (
        "B2 stale rec kept",
        C + "coordinator.py",
        "            self.recommendation = None\n            self._last_optimise = None\n            return",
        "            self._last_optimise = None\n            return",
        HA,
    ),
    (
        "B3 no throttle",
        C + "coordinator.py",
        "        ):\n            return\n        self._last_optimise = now",
        "        ):\n            pass\n        self._last_optimise = now",
        HA,
    ),
    (
        "B3 throttle after failure",
        C + "coordinator.py",
        "            self.recommendation is not None\n            and self._last_optimise is not None",
        "            self._last_optimise is not None",
        HA,
    ),
    (
        "B5 errors dropped on load",
        C + "core/engine.py",
        "        self.errors = errors\n",
        "        self.errors = {}\n",
        CORE,
    ),
    (
        "B6 cost units",
        C + "core/optimiser.py",
        "list(setpoints_per_hour), tis, elec, energy, obj,",
        "list(setpoints_per_hour), tis, elec, energy / 1000, obj,",
        CORE,
    ),
    (
        "B7 Predbat offset ignored",
        C + "core/cost_model.py",
        "        if t.tzinfo is not None and math.isfinite(r):\n            out.append((t, r))",
        "        if t.tzinfo is not None and math.isfinite(r):\n            out.append((t.replace(tzinfo=UTC), r))",
        CORE,
    ),
    (
        "B8 no unit conversion",
        C + "coordinator.py",
        'return Reading(to_core_unit(role, value, st.attributes.get("unit_of_measurement")), age)',
        "return Reading(value, age)",
        HA,
    ),
    (
        "B9 heating forced on",
        C + "core/engine.py",
        "                bool(heating_enabled if heating_enabled is not None else True),",
        "                True,",
        CORE,
    ),
    ("B10 outdoor coverage ignored", "tools/dataset.py", '& (g[to_col].count() >= d["day_h"] - 2)', "", CORE),
    (
        "B11 comfort validation off",
        C + "config_flow.py",
        "                for p in parse_periods(str(user_input[CONF_COMFORT_PERIODS])):",
        "                for p in ():",
        HA,
    ),
    (
        "B12 plan time from status",
        C + "sensor.py",
        "start = (r.start or s.time).replace(second=0, microsecond=0)",
        "start = s.time.replace(second=0, microsecond=0)",
        HA,
    ),
    (
        "B12 diagnostics empty",
        C + "diagnostics.py",
        '    return {\n        "mapping"',
        '    return {}\n    return {\n        "mapping"',
        HA,
    ),
    (
        "R7-1 sums/weights consistency off",
        C + "core/accumulator.py",
        "            if set(weights) != set(sums):",
        "            if False:",
        CORE,
    ),
    ("R7-3 zone not restored", C + "core/engine.py", "return ZoneInfo(str(key))", "return None", CORE),
    (
        "R7-4 offset changes ignored",
        C + "core/cost_model.py",
        "if off != prev and inside(",
        "if False and inside(",
        CORE,
    ),
    (
        "R7-5 restart flag off",
        C + "core/accumulator.py",
        "a.counter_restart |= new_baseline and not at_midnight",
        "a.counter_restart |= False",
        CORE,
    ),
    ("R7-B1 no capacity cap", C + "core/predictor.py", "min(plant.q_max_w, ", "max(0.0, ", CORE),
    (
        "R7-B2 small decrease accepted",
        C + "core/accumulator.py",
        "                    v = prev  # small decrease",
        "                    pass  # small decrease",
        CORE,
    ),
    ("R7-B3 no C learning", C + "core/learner.py", "dti_next / day_h])", "0.0])", CORE),
    (
        "R7-B4 expiry off by one",
        C + "core/cost_model.py",
        "t - series[i][0] < self.series_slot",
        "t - series[i][0] <= self.series_slot",
        CORE,
    ),
    # "R8-1 accounting restored piecemeal" is equivalent: a rejected state leaves a fresh DayAggregator,
    # which withholds its first (partial) date anyway, so leaked baselines cannot reach learning.
    (
        "R8-2 closing day kept after midnight re-map",
        C + "core/accumulator.py",
        "                old.counter_restart = True",
        "                pass",
        CORE,
    ),
    (
        "R8-3 unknown heating switch treated as on",
        C + "core/telemetry.py",
        "            and Role.HEATING_ENABLED not in self.issues\n",
        "",
        CORE,
    ),
    (
        "R8-4 standby learned twice",
        C + "core/engine.py",
        "day.heating_ext_kwh, None, day.length_h",
        "day.heating_ext_kwh, day.standby_w, day.length_h",
        CORE,
    ),
    (
        "R8-B1 start state from compressor",
        C + "core/engine.py",
        "running0=self._calling(s, s.get(Role.HZ)),",
        "running0=bool(s.get(Role.HZ)),",
        CORE,
    ),
    ("R8-B2 standby over 24 h", C + "core/cop_learner.py", "sb * day_h / 1000.0", "sb * 24.0 / 1000.0", CORE),
    (
        "R9-1 live dicts in saved state",
        C + "core/accumulator.py",
        '"sums": dict(a.sums),',
        '"sums": a.sums,',
        CORE,
    ),
    (
        "R9-1 live deltas in saved state",
        C + "core/accumulator.py",
        '"deltas": dict(a.deltas),',
        '"deltas": a.deltas,',
        CORE,
    ),
    (
        "R9-2 reset not a discontinuity",
        C + "core/accumulator.py",
        "                    new_baseline = True",
        "                    pass",
        CORE,
    ),
    (
        "R9-3 clock going back accepted",
        C + "core/accumulator.py",
        "if self._last_time is not None and elapsed_s(self._last_time, s.time) <= 0:",
        "if False:",
        CORE,
    ),
    (
        "R9-B1 learning switch ignored",
        C + "core/engine.py",
        "        if not self.cfg.learning_enabled:\n",
        "        if False:\n",
        CORE,
    ),
    (
        "R9-B2 RT modulation unbounded above",
        C + "core/predictor.py",
        "min(plant.rt_modulation_max_k, mod)",
        "mod",
        CORE,
    ),
    (
        "R9-B3 hysteresis off at strict >",
        C + "core/predictor.py",
        "ti >= sp + plant.hysteresis_off_k",
        "ti > sp + plant.hysteresis_off_k",
        CORE,
    ),
    (
        "R9-B4 standby from one off hour",
        C + "core/accumulator.py",
        "if len(off) >= 3:",
        "if len(off) >= 1:",
        CORE,
    ),
    (
        "C-1 mild nights accepted",
        C + "core/capacity_learner.py",
        "if pair is None or pair[0] < self.min_dt_k:",
        "if pair is None:",
        CORE,
    ),
    (
        "C-2 any hour of day",
        C + "core/capacity_learner.py",
        "if h0.start.hour not in NIGHT_START_HOURS:",
        "if False:",
        CORE,
    ),
    (
        "C-3 no forgetting",
        C + "core/capacity_learner.py",
        "lam = self.forgetting_per_day**days",
        "lam = 1.0",
        CORE,
    ),
    (
        "C-4 hour before not checked",
        C + "core/capacity_learner.py",
        "        for h in hours:\n",
        "        for h in (h0, h1):\n",
        CORE,
    ),
    (
        "C-5 estimate not applied",
        C + "core/engine.py",
        "self.learner.set_capacity(est.c_wh_per_k, est.sd_wh_per_k)",
        "False",
        CORE,
    ),
    (
        "C-6 stale priors kept",
        C + "core/engine.py",
        "            self.learner = self._new_learner()\n",
        "            pass\n",
        CORE,
    ),
]
only = sys.argv[1:]
res = []
for name, f, old, new, cmd in M:
    if only and not any(o in name for o in only):
        continue
    p = pathlib.Path(f)
    src = p.read_text()
    if src.count(old) != 1:
        res.append((name, f"PATTERN x{src.count(old)}"))
        continue
    p.write_text(src.replace(old, new))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        res.append((name, "killed" if r.returncode != 0 else "SURVIVED"))
    finally:
        p.write_text(src)
for n, r in res:
    print(f"{r:10s} {n}")
