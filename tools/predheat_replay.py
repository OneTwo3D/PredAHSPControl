"""Replay Predheat against recorded days and calibrate its parameters.

Each heating day is simulated from local midnight for 24 h with *measured* outdoor temperature and
the measured room-setpoint schedule (perfect weather forecast, isolating model error), starting from
the measured room and water temperatures. Simulated heating electricity and room temperature are
compared with the meters.

Parameter sets compared:

* ``current``    – values from apps.yaml (``tools/predheat_current.json``)
* ``fitted``     – physically fitted values from ``tools/fit_offline.py`` (UA, gains, C, COP, min output)
* ``calibrated`` – ``fitted`` plus a pattern search on Predheat-internal parameters, trained on even ISO weeks

All sets are evaluated on the odd-week hold-out days.

Usage::

    python tools/predheat_replay.py --data data/lts_hour.csv --report docs/predheat_replay_report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dataset as ds

from custom_components.daikin_mpc.core.predheat_sim import (
    DEFAULT_HEAT_PUMP_EFFICIENCY,
    STEP_MIN,
    PredheatConfig,
    PredheatSim,
)

STEPS_PER_HOUR = 60 // STEP_MIN
# Objective scaling: 0.5 kWh/day of electricity error is weighted like 0.5 K of hourly room-temperature RMSE.
ENERGY_SCALE_KWH = 0.5
TEMP_SCALE_K = 0.5

# Only Predheat-internal parameters are tuned. UA, gains and the COP curve stay at their physically
# fitted values: a free search trades them against each other (e.g. low UA with low COP) and gives
# implausible values that would not extrapolate. C (via heat_loss_degrees) is weakly identified, so
# it is tuned within ±50 % of the fitted value.
TUNABLE: dict[str, tuple[float, float]] = {  # name: (lower, upper)
    "heat_loss_degrees": (0.02, 0.065),
    "heat_min_power": (500.0, 1500.0),
    "flow_difference_target": (1.0, 10.0),
    "hysteresis": (0.1, 1.5),
    "hysteresis_off": (0.1, 1.5),
}
REPORTED = (
    "heat_loss_watts",
    "heat_loss_degrees",
    "heat_gain_static",
    "heat_min_power",
    "flow_difference_target",
    "heat_cop",
    "hysteresis",
    "hysteresis_off",
)


def load_config(path: Path) -> PredheatConfig:
    raw: dict[str, Any] = json.loads(path.read_text())
    for tbl in ("weather_compensation", "heat_pump_efficiency", "delta_correction"):
        if raw.get(tbl):
            raw[tbl] = {int(k): float(v) for k, v in raw[tbl].items()}
    raw.pop("_comment", None)
    return PredheatConfig(**raw)


class Day:
    __slots__ = ("date", "elec_kwh", "ext", "heating_active", "internal0", "target", "ti_hourly", "volume0")

    def __init__(self, day: pd.Timestamp, h: pd.DataFrame, to_col: str, elec_kwh: float) -> None:
        self.date = day
        hh = h.loc[day : day + pd.Timedelta(hours=23)]
        # Hourly means sit at the hour centres; interpolate to 5-minute steps.
        x_hour = np.arange(24) * STEPS_PER_HOUR + STEPS_PER_HOUR / 2
        x = np.arange(24 * STEPS_PER_HOUR)
        self.ext = np.interp(x, x_hour, hh[to_col].interpolate().bfill().ffill().to_numpy())
        self.target = np.repeat(hh.room_set_mean.ffill().bfill().round(1).to_numpy(), STEPS_PER_HOUR)
        self.ti_hourly = hh.ti_mean.to_numpy()
        self.internal0 = float(hh.ti_mean.iloc[0])
        mwt = (hh.lwt_mean.iloc[0] + hh.rwt_mean.iloc[0]) / 2
        self.volume0 = float(mwt) if np.isfinite(mwt) else self.internal0
        self.heating_active = bool(hh.hz_mean.iloc[0] > 0)
        self.elec_kwh = elec_kwh


def build_days(h: pd.DataFrame, start: str, end: str, to_col: str) -> list[Day]:
    d = ds.season(ds.daily(h, to_col=to_col), start, end).dropna(subset=["elec_kwh", "ti", "to"])
    d = d[d.hours >= 24]
    return [Day(t, h, to_col, float(e)) for t, e in d.elec_kwh.items()]


def evaluate(cfg: PredheatConfig, days: list[Day]) -> dict[str, float]:
    sim = PredheatSim(cfg)
    e_err, t_sq, t6, t24 = [], [], [], []
    for d in days:
        r = sim.run(d.internal0, d.volume0, d.heating_active, d.ext, d.target)
        e_err.append(r.electric_wh.sum() / 1000 - d.elec_kwh)
        ti_sim = r.internal_c.reshape(24, STEPS_PER_HOUR).mean(axis=1)
        err = ti_sim - d.ti_hourly
        t_sq.append(np.nanmean(err**2))
        t6.append(abs(err[5]))
        t24.append(abs(err[23]))
    e = np.asarray(e_err)
    t_rmse = float(np.sqrt(np.nanmean(t_sq)))
    mae = float(np.abs(e).mean())
    return {
        "days": len(days),
        "elec_mae": mae,
        "elec_bias": float(e.mean()),
        "elec_mean": float(np.mean([d.elec_kwh for d in days])),
        "ti_rmse": t_rmse,
        "ti_mae_6h": float(np.nanmean(t6)),
        "ti_mae_24h": float(np.nanmean(t24)),
        "objective": mae / ENERGY_SCALE_KWH + t_rmse / TEMP_SCALE_K,
    }


def pattern_search(cfg: PredheatConfig, days: list[Day], iters: int = 6) -> PredheatConfig:
    """Bounded coordinate pattern search with shrinking relative steps."""
    best, best_j = cfg, evaluate(cfg, days)["objective"]
    step = 0.25
    for _ in range(iters):
        improved = True
        while improved:
            improved = False
            for name, (lo, hi) in TUNABLE.items():
                cur = float(getattr(best, name))
                for factor in (1 + step, 1 - step):
                    cand = replace(best, **{name: float(np.clip(cur * factor, lo, hi))})
                    j = evaluate(cand, days)["objective"]
                    if j < best_j - 1e-6:
                        best, best_j, improved = cand, j, True
                        break
        step /= 2
    return best


def efficiency_table(cop_centres: list[float], cop: list[float]) -> dict[int, float]:
    """Full even-keyed table −20…20 °C so no upstream default value survives the merge.

    Inside the measured range values are interpolated. Below the coldest measured bin the COP is
    extrapolated with the *shape* of Predheat's default curve (scaled to match at the coldest bin)
    rather than held flat, which would be optimistic; above the warmest bin it is held flat.
    """
    t_lo, c_lo = cop_centres[0], cop[0]
    d_lo = float(
        np.interp(
            t_lo,
            sorted(DEFAULT_HEAT_PUMP_EFFICIENCY),
            [DEFAULT_HEAT_PUMP_EFFICIENCY[k] for k in sorted(DEFAULT_HEAT_PUMP_EFFICIENCY)],
        )
    )
    table = {}
    for t in range(-20, 21, 2):
        v = (
            c_lo * DEFAULT_HEAT_PUMP_EFFICIENCY[t] / d_lo
            if t < t_lo
            else float(np.interp(t, cop_centres, cop))
        )
        table[t] = round(v, 2)
    return table


def fitted_config(cur: PredheatConfig, fit: dict[str, Any]) -> PredheatConfig:
    eff = efficiency_table(fit["cop_centres"], fit["cop"])
    c_wh = fit["c_wh"]
    return replace(
        cur,
        heat_loss_watts=round(fit["ua"]),
        heat_gain_static=round(fit["gains"] / 10) * 10,
        heat_loss_degrees=round(fit["ua"] / c_wh, 4),
        heat_min_power=round(fit["qmin_w"] / 10) * 10,
        heat_pump_efficiency=eff,
        heat_cop=max(eff.values()),
    )


def yaml_snippet(cfg: PredheatConfig) -> str:
    lines = [
        f"    heat_loss_watts: {cfg.heat_loss_watts:.0f}",
        f"    heat_loss_degrees: {cfg.heat_loss_degrees:.4f}",
        f"    heat_gain_static: {cfg.heat_gain_static:.0f}",
        f"    heat_min_power: {cfg.heat_min_power:.0f}",
        f"    flow_difference_target: {cfg.flow_difference_target:.1f}",
        f"    heat_cop: {cfg.heat_cop:.2f}",
        f"    hysteresis: {cfg.hysteresis:.2f}",
        f"    hysteresis_off: {cfg.hysteresis_off:.2f}",
    ]
    if cfg.heat_pump_efficiency:
        lines.append("    heat_pump_efficiency:")
        lines += [f"      {k}: {v}" for k, v in sorted(cfg.heat_pump_efficiency.items())]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/lts_hour.csv")
    ap.add_argument("--current", default=str(Path(__file__).with_name("predheat_current.json")))
    ap.add_argument("--fit", default="data/offline_fit.json")
    ap.add_argument("--start", default="2025-11-01")
    ap.add_argument("--end", default="2026-03-31")
    ap.add_argument(
        "--outdoor", default="to_ecowitt_mean", help="Predheat is configured with the Ecowitt sensor"
    )
    ap.add_argument("--report", default="docs/predheat_replay_report.md")
    a = ap.parse_args(argv)

    h = ds.load_hourly(a.data)
    days = build_days(h, a.start, a.end, a.outdoor)
    idx = pd.DatetimeIndex([d.date for d in days])
    tr_mask, te_mask = ds.split_weeks(idx)
    train = [d for d, m in zip(days, tr_mask, strict=True) if m]
    test = [d for d, m in zip(days, te_mask, strict=True) if m]

    current = load_config(Path(a.current))
    fitted = fitted_config(current, json.loads(Path(a.fit).read_text()))
    calibrated = pattern_search(fitted, train)

    sets = {"current": current, "fitted": fitted, "calibrated": calibrated}
    res = {k: {"train": evaluate(c, train), "test": evaluate(c, test)} for k, c in sets.items()}

    rows = [
        "| Set | Elec MAE kWh/day | Elec bias kWh/day | Room RMSE K (hourly) | Room MAE @6 h K | Room MAE @24 h K |",
        "|---|---|---|---|---|---|",
    ]
    for k in sets:
        t = res[k]["test"]
        rows.append(
            f"| {k} | {t['elec_mae']:.2f} | {t['elec_bias']:+.2f} | {t['ti_rmse']:.2f} | "
            f"{t['ti_mae_6h']:.2f} | {t['ti_mae_24h']:.2f} |"
        )
    par = ["| Parameter | current | fitted | calibrated |", "|---|---|---|---|"]
    for name in REPORTED:
        par.append(f"| `{name}` | " + " | ".join(f"{getattr(c, name):.4g}" for c in sets.values()) + " |")
    par.append(
        "| implied C (kWh/K) | " + " | ".join(f"{c.watt_per_degree / 1000:.1f}" for c in sets.values()) + " |"
    )

    # Predbat consumes Predheat's *energy* forecast, so the recommendation is the set with the lowest
    # hold-out electricity error; room-temperature error is reported for information.
    rec = min(sets, key=lambda k: res[k]["test"]["elec_mae"])
    at_bounds = [
        n
        for n, (lo, hi) in TUNABLE.items()
        if np.isclose(getattr(calibrated, n), lo, rtol=1e-3)
        or np.isclose(getattr(calibrated, n), hi, rtol=1e-3)
    ]

    report = "\n".join(
        [
            "# Predheat replay and calibration (M1)",
            "",
            f"Generated {date.today().isoformat()} by `tools/predheat_replay.py`. Window {a.start} … {a.end}, "
            f"{len(days)} complete days ({len(train)} train / {len(test)} test, split by ISO-week parity). "
            f"Outdoor sensor `{a.outdoor.removesuffix('_mean')}`, measured (perfect forecast). Mean measured heating "
            f"electricity on test days: {res['current']['test']['elec_mean']:.2f} kWh/day.",
            "",
            "## Hold-out error",
            "",
            *rows,
            "",
            "## Parameters",
            "",
            *par,
            "",
            f"Calibrated parameters at a search bound (structural mismatch, treat with caution): "
            f"{', '.join(f'`{n}`' for n in at_bounds) or 'none'}.",
            "",
            f"## Recommended `predheat:` values ({rec} set: lowest hold-out electricity error)",
            "",
            "```yaml",
            yaml_snippet(sets[rec]),
            "```",
            "",
            "Limitations: Predheat does not model the Daikin's RT modulation, the overnight deviation schedule or DHW "
            "interruptions; the daily counters have 1 kWh resolution; hourly room temperatures are means.",
            "",
        ]
    )
    Path(a.report).write_text(report)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
