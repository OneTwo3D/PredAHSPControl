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
    ("A2 anchored ignored", C + "core/accumulator.py", "!= (0, 0) or not h.anchored", "!= (0, 0)", CORE),
    (
        "A3/B5 day length forced 24",
        C + "core/accumulator.py",
        "length_h = day_length_h(date.fromisoformat(self._day), self._tz)",
        "length_h = 24.0",
        CORE,
    ),
    (
        "A3 tz from restored hour",
        C + "core/accumulator.py",
        "        # live hours carry the installation's named zone (needed for 23/25-hour day lengths)\n        self._tz = h.start.tzinfo\n",
        "        self._tz = self._tz or h.start.tzinfo\n",
        CORE,
    ),
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
