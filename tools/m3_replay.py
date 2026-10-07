"""Replay the M3 shadow optimiser over last winter (model estimates, not measured savings).

For each day in the window, a 24 h plan is made at 18:00 from the measured room temperature, with the
measured outdoor temperature as a perfect forecast and the native weather curve. Three schedules are
simulated with the same building/heat-pump model and compared:

* ``actual``    – the room-setpoint schedule that was really used (incl. the old night setback),
* ``fixed``     – a constant setpoint at the comfort minimum + 0.5 K (no setback),
* ``optimised`` – the optimiser's plan (comfort range 20–22 °C by default).

Prices: last winter's Predbat rate history is not available, so the configured tariff is used (no VPP
events). Both price bases are reported.

Usage::

    python tools/m3_replay.py --data data/lts_hour.csv --report docs/m3_replay_report.md
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dataset as ds

from custom_components.daikin_mpc.core.cost_model import (
    DEFAULT_EXPORT_TARIFF,
    DEFAULT_TARIFF,
    CostProvider,
    efficiency_from_predbat,
    parse_tariff,
)
from custom_components.daikin_mpc.core.engine import EngineConfig, ShadowEngine
from custom_components.daikin_mpc.core.optimiser import OptimiserConfig, PlanInputs, evaluate, optimise

STEPS_PER_H = 4


def day_inputs(
    h: pd.DataFrame, start: pd.Timestamp, eng: ShadowEngine, cost: CostProvider
) -> PlanInputs | None:
    hh = h.loc[start : start + pd.Timedelta(hours=23)]
    if len(hh) < 24 or hh[["ti_mean", "to_mean", "room_set_mean"]].isna().any().any():
        return None
    x_hour = np.arange(24) * STEPS_PER_H + STEPS_PER_H / 2
    x = np.arange(24 * STEPS_PER_H)
    to = np.interp(x, x_hour, hh.to_mean.to_numpy()).tolist()
    times = [start.to_pydatetime() + timedelta(minutes=15 * i + 7) for i in range(len(to))]
    return PlanInputs(
        start=start.to_pydatetime(),
        ti0_c=float(hh.ti_mean.iloc[0]),
        running0=bool(hh.hz_mean.iloc[0] > 0),
        current_setpoint_c=float(round(hh.room_set_mean.iloc[0] * 2) / 2),
        heating_enabled=True,
        to_c=to,
        lwt_set_c=[eng.wc_lwt(v) for v in to],
        rate_p=[cost.marginal_rate(t) for t in times],
        baseline_setpoint_c=[float(round(v * 2) / 2) for v in hh.room_set_mean],
    )


def run(h: pd.DataFrame, start: str, end: str, basis: str, cfg: OptimiserConfig) -> pd.DataFrame:
    eng = ShadowEngine(EngineConfig())
    cost = CostProvider(
        parse_tariff(DEFAULT_TARIFF),
        parse_tariff(DEFAULT_EXPORT_TARIFF),
        basis=basis,
        round_trip_efficiency=efficiency_from_predbat(0.03, 0.03, 0.03),
    )
    rows = []
    for d in pd.date_range(start, end, freq="D", tz=ds.TZ):
        inp = day_inputs(h, d + pd.Timedelta(hours=18), eng, cost)
        if inp is None:
            continue
        args = (eng.learner.params, eng.plant, eng.cop, cfg)
        actual = evaluate(inp.baseline_setpoint_c, inp, *args)
        fixed = evaluate([cfg.room_min_c + 0.5] * 24, inp, *args)
        t0 = time.perf_counter()
        opt = optimise(inp, *args)
        rt = time.perf_counter() - t0
        rows.append(
            {
                "day": d.date(),
                "to": float(np.mean(inp.to_c)),
                **{f"{k}_cost": r.cost_p for k, r in (("actual", actual), ("fixed", fixed), ("opt", opt))},
                **{
                    f"{k}_kwh": r.energy_kwh()
                    for k, r in (("actual", actual), ("fixed", fixed), ("opt", opt))
                },
                **{f"{k}_min": r.min_ti_c for k, r in (("actual", actual), ("fixed", fixed), ("opt", opt))},
                **{f"{k}_starts": r.starts for k, r in (("actual", actual), ("fixed", fixed), ("opt", opt))},
                "opt_night_sp": float(np.mean(opt.setpoints_c[6:11])),  # 00:00-05:00
                "opt_day_sp": float(np.mean(opt.setpoints_c[14:22])),  # 08:00-16:00
                "runtime_s": rt,
            }
        )
    return pd.DataFrame(rows)


def summary(df: pd.DataFrame, cfg: OptimiserConfig) -> list[str]:
    def below(col: str) -> int:
        return int((df[col] < cfg.room_min_c - 0.05).sum())

    lines = [
        "| Schedule | Cost p/day | kWh/day | Days below comfort min | Mean lowest room °C | Starts/day |",
        "|---|---|---|---|---|---|",
    ]
    for k, name in (
        ("actual", "Actual (incl. night setback)"),
        ("fixed", f"Fixed {cfg.room_min_c + 0.5:.1f} °C"),
        ("opt", "Optimised"),
    ):
        lines.append(
            f"| {name} | {df[f'{k}_cost'].mean():.1f} | {df[f'{k}_kwh'].mean():.2f} | {below(f'{k}_min')} "
            f"| {df[f'{k}_min'].mean():.2f} | {df[f'{k}_starts'].mean():.1f} |"
        )
    lines.append("")
    lines.append(
        f"Optimised vs fixed: {df.fixed_cost.mean() - df.opt_cost.mean():+.1f} p/day "
        f"({100 * (1 - df.opt_cost.sum() / df.fixed_cost.sum()):+.1f} %). Mean optimised setpoint "
        f"night (00–05) {df.opt_night_sp.mean():.2f} °C, day (08–16) {df.opt_day_sp.mean():.2f} °C. "
        f"Runtime median {df.runtime_s.median():.2f} s, max {df.runtime_s.max():.2f} s."
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/lts_hour.csv")
    ap.add_argument("--start", default="2025-11-01")
    ap.add_argument("--end", default="2026-03-30")
    ap.add_argument("--report", default="docs/m3_replay_report.md")
    a = ap.parse_args(argv)
    h = ds.load_hourly(a.data)
    cfg = OptimiserConfig()
    out = [
        "# M3 shadow optimiser — replay on winter 2025/26",
        "",
        f"Generated {date.today().isoformat()} by `tools/m3_replay.py`. One 24 h plan per day from 18:00, "
        f"{a.start} … {a.end}. Comfort range {cfg.room_min_c}–{cfg.room_max_c} °C. Measured outdoor "
        "temperature used as a perfect forecast; fixed tariff (7.6p 00–05, 34.87p otherwise; export 2p/12p); "
        "no VPP events. **All figures are model estimates for comparing schedules, not measured savings.**",
        "",
    ]
    for basis, title in (
        ("battery", "Battery- and export-aware prices (default)"),
        ("tariff", "Raw import tariff"),
    ):
        df = run(h, a.start, a.end, basis, cfg)
        out += [f"## {title}", "", f"{len(df)} days.", "", *summary(df, cfg), ""]
    out += [
        "## Notes",
        "",
        "- The actual schedule falls below the new 20 °C minimum on most nights (old setback ≈ 19.3 °C), so it",
        "  is cheaper but not comparable on comfort; the fixed schedule is the fair reference.",
        "- Thermostat hysteresis, RT-modulation gain and cycling are model priors until calibrated from this",
        "  winter's data (M3 tuning step).",
        "",
    ]
    Path(a.report).write_text("\n".join(out))
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
