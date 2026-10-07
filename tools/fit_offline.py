"""Offline identification of building, emitter and heat-pump models from exported statistics.

Usage::

    python tools/fit_offline.py --data data/lts_hour.csv --report docs/offline_fit_report.md

The report contains only aggregate parameters and error metrics (no raw household data).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dataset as ds

from custom_components.daikin_mpc.core import emitter_model, heatpump_model, thermal_model
from custom_components.daikin_mpc.core.intervals import IntervalClass

COP_EDGES = np.array([-10.0, 0.0, 3.0, 6.0, 9.0, 12.0, 16.0])


def heating_days(d: pd.DataFrame) -> pd.DataFrame:
    return d.dropna(subset=["ti", "to", "heat_kwh", "elec_kwh", "dti_next"]).query("heat_kwh > 0")


def fit_all(h: pd.DataFrame, start: str, end: str, to_col: str) -> dict[str, object]:
    d = heating_days(ds.season(ds.daily(h, to_col=to_col), start, end))
    train, test = ds.split_weeks(d.index)

    full = thermal_model.fit_daily_energy_balance(d.ti, d.to, d.heat_kwh, d.dti_next)
    tr = thermal_model.fit_daily_energy_balance(
        d.ti[train], d.to[train], d.heat_kwh[train], d.dti_next[train]
    )

    hs = ds.season(h, start, end)
    q_hour = hs["heat_w_mean"].clip(lower=0).where(hs["cls"] != IntervalClass.DHW.value)
    ok = q_hour.notna() & hs.ti_mean.notna() & hs[to_col].notna()
    c_hourly, ua_hourly, g_hourly = thermal_model.fit_hourly_dynamics(
        hs.ti_mean[ok].to_numpy(), hs[to_col][ok].to_numpy(), q_hour[ok].to_numpy(), window_h=6
    )

    fr = hs[hs.cls == IntervalClass.HEATING_FULL.value]
    q_hyd = emitter_model.hydronic_heat_w(fr.flow_mean, fr.lwt_mean, fr.rwt_mean)
    rad = emitter_model.fit(q_hyd, (fr.lwt_mean + fr.rwt_mean) / 2, fr.ti_mean)
    qmin = heatpump_model.min_output_w(fr.heat_w_mean.to_numpy())

    cop = heatpump_model.fit_cop_curve(d.to, d.heat_kwh, d.elec_kwh, COP_EDGES)
    cop_tr = heatpump_model.fit_cop_curve(d.to[train], d.heat_kwh[train], d.elec_kwh[train], COP_EDGES)

    # Held-out daily electricity prediction from the train-week fits.
    p = tr.params
    q_pred_w = np.maximum(0.0, p.ua_w_per_k * (d.ti[test] - d.to[test]) - p.gains_w)
    cop_pred = np.array([cop_tr.at(t)[0] for t in d.to[test]])
    e_pred = q_pred_w * 24 / 1000 / cop_pred
    e_err = e_pred - d.elec_kwh[test]
    heat_err = q_pred_w * 24 / 1000 - d.heat_kwh[test]

    hourly_counts = hs.cls.value_counts().to_dict()
    return {
        "window": f"{start} … {end}",
        "outdoor_sensor": to_col.removesuffix("_mean"),
        "days": len(d),
        "train_days": int(train.sum()),
        "test_days": int(test.sum()),
        "ua": (full.params.ua_w_per_k, full.ua_se),
        "gains": (full.params.gains_w, full.gains_se),
        "c_kwh": (full.params.c_wh_per_k / 1000, full.c_se / 1000),
        "r2": full.r2,
        "rmse_w": full.rmse_w,
        "c_hourly_kwh": c_hourly / 1000,
        "ua_hourly": ua_hourly,
        "gains_hourly": g_hourly,
        "rad_k": rad.params.k_w_per_kn,
        "rad_k_iqr": rad.k_iqr,
        "rad_n_free": rad.n_free,
        "rad_rated_kw": rad.params.rated_output_w() / 1000,
        "rad_samples": rad.n_samples,
        "qmin_w": qmin,
        "q_median_full_w": float(fr.heat_w_mean.median()),
        "full_run": {
            "lwt": float(fr.lwt_mean.mean()),
            "rwt": float(fr.rwt_mean.mean()),
            "flow": float(fr.flow_mean.mean()),
            "q": float(q_hyd.mean()),
        },
        "cop": cop,
        "hourly_counts": hourly_counts,
        "test_elec_mae": float(np.abs(e_err).mean()),
        "test_elec_bias": float(e_err.mean()),
        "test_elec_mean": float(d.elec_kwh[test].mean()),
        "test_heat_mae": float(np.abs(heat_err).mean()),
        "test_heat_mean": float(d.heat_kwh[test].mean()),
        "required_mwt": {
            t: rad.params.required_mwt_c(
                max(0.0, full.params.ua_w_per_k * (20.5 - t) - full.params.gains_w), 20.5
            )
            for t in (-3, 0, 5, 10)
        },
    }


def render(r: dict[str, object]) -> str:
    cop = r["cop"]
    assert isinstance(cop, heatpump_model.CopCurve)
    ua, ua_se = r["ua"]  # type: ignore[misc]
    g, g_se = r["gains"]  # type: ignore[misc]
    c, c_se = r["c_kwh"]  # type: ignore[misc]
    fr = r["full_run"]  # type: ignore[assignment]
    lines = [
        "# Offline fit report (M1)",
        "",
        f"Generated {date.today().isoformat()} by `tools/fit_offline.py` from hourly long-term statistics, "
        f"window {r['window']}, outdoor sensor `{r['outdoor_sensor']}`. Aggregates only.",
        "",
        "## Building (daily energy balance)",
        "",
        "| Parameter | Value |",
        "|---|---|",
        f"| UA | {ua:.1f} ± {ua_se:.1f} W/K |",
        f"| Net gains | {g:.0f} ± {g_se:.0f} W |",
        f"| C | {c:.1f} ± {c_se:.1f} kWh/K |",
        f"| Fit | R² {r['r2']:.3f}, RMSE {r['rmse_w']:.0f} W (daily mean), {r['days']} days |",
        f"| Hourly-dynamics cross-check | C {r['c_hourly_kwh']:.1f} kWh/K, UA {r['ua_hourly']:.0f} W/K (biased low by radiator lag), gains {r['gains_hourly']:.0f} W |",
        "",
        f"Held-out check (fit on even ISO weeks, {r['train_days']} days; test on odd weeks, {r['test_days']} days):",
        f"daily heat MAE {r['test_heat_mae']:.1f} kWh (mean {r['test_heat_mean']:.1f}); "
        f"daily heating electricity MAE {r['test_elec_mae']:.2f} kWh, bias {r['test_elec_bias']:+.2f} kWh "
        f"(mean {r['test_elec_mean']:.1f} kWh). Counters have 1 kWh resolution.",
        "",
        "## Radiators",
        "",
        f"- Full-run hours without DHW: {r['rad_samples']}; mean LWT {fr['lwt']:.1f} °C, RWT {fr['rwt']:.1f} °C, "
        f"flow {fr['flow']:.1f} L/min, hydronic heat {fr['q']:.0f} W.",
        f"- K = {r['rad_k']:.1f} W/K^1.3 (IQR {r['rad_k_iqr'][0]:.1f}–{r['rad_k_iqr'][1]:.1f}), free-fit n = {r['rad_n_free']:.2f}, "  # type: ignore[index]
        f"rated output ≈ {r['rad_rated_kw']:.1f} kW at ΔT50.",
        "- Mean water temperature needed to hold 20.5 °C: "
        + ", ".join(f"{t:+d} °C → {v:.1f} °C" for t, v in r["required_mwt"].items()),  # type: ignore[attr-defined]
        "",
        "## Heat pump",
        "",
        f"- Minimum modulated output (5th percentile of full-run hours) {r['qmin_w']:.0f} W; median {r['q_median_full_w']:.0f} W.",
        "",
        "| Outdoor (energy-weighted) °C | COP (monotone) | Heat kWh | Elec kWh |",
        "|---|---|---|---|",
        *[
            f"| {t:.1f} | {v:.2f} | {h:.0f} | {e:.0f} |"
            for t, v, h, e in zip(cop.centres_c, cop.cop, cop.heat_kwh, cop.elec_kwh, strict=True)
        ],
        "",
        "## Interval classes (hours)",
        "",
        ", ".join(f"{k}: {v}" for k, v in r["hourly_counts"].items()),  # type: ignore[attr-defined]
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/lts_hour.csv")
    ap.add_argument("--start", default="2025-11-01")
    ap.add_argument("--end", default="2026-03-31")
    ap.add_argument("--outdoor", default="to_mean", choices=["to_mean", "to_ecowitt_mean"])
    ap.add_argument("--report", default="docs/offline_fit_report.md")
    ap.add_argument("--json", default="data/offline_fit.json")
    a = ap.parse_args(argv)
    h = ds.load_hourly(a.data)
    r = fit_all(h, a.start, a.end, a.outdoor)
    Path(a.report).write_text(render(r))
    cop = r["cop"]
    assert isinstance(cop, heatpump_model.CopCurve)
    Path(a.json).parent.mkdir(parents=True, exist_ok=True)
    Path(a.json).write_text(
        json.dumps(
            {
                "ua": r["ua"][0],
                "gains": r["gains"][0],
                "c_wh": r["c_kwh"][0] * 1000,
                "c_hourly_wh": r["c_hourly_kwh"] * 1000,  # type: ignore[index]
                "qmin_w": r["qmin_w"],
                "cop_centres": cop.centres_c,
                "cop": cop.cop,
            },
            indent=2,
        )
    )
    print(Path(a.report).read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
