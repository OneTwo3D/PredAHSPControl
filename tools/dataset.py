"""Load exported long-term statistics into hourly and daily pandas frames (local time)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_components.daikin_mpc.core.intervals import (
    INVALID_TEMPERATURE_BELOW_C,
    IntervalClass,
    IntervalSummary,
    classify,
)

TZ = "Europe/London"
COUNTERS = ("heat_kwh", "elec_kwh", "dhw_heat_kwh", "dhw_elec_kwh", "buh_kwh")
HOUR_METERS = ("run_h", "dhw_run_h")
TEMPS = ("ti", "to", "to_ecowitt", "lwt", "rwt", "tank_c")


def load_hourly(csv_path: str | Path) -> pd.DataFrame:
    """Wide hourly frame. Columns ``<key>_<field>`` plus per-hour counter increments ``d_<key>``."""
    raw = pd.read_csv(csv_path, parse_dates=["time"])
    raw["time"] = raw["time"].dt.tz_convert(TZ)
    wide = raw.pivot_table(index="time", columns="key", values=["mean", "min", "max", "sum", "state"])
    wide.columns = [f"{k}_{f}" for f, k in wide.columns]
    h = wide.sort_index().asfreq("h")
    for t in TEMPS:
        for f in ("mean", "min", "max"):
            col = f"{t}_{f}"
            if col in h:
                h.loc[h[col] <= INVALID_TEMPERATURE_BELOW_C, col] = np.nan
    for c in COUNTERS:
        # Prefer the statistics ``sum`` (HA adjusts it for meter resets); ``state`` drops the energy
        # counted after a reset.
        src = f"{c}_sum" if f"{c}_sum" in h else f"{c}_state"
        if src in h:
            h[f"d_{c}"] = h[src].diff().clip(lower=0)
    for c in HOUR_METERS:
        if f"{c}_max" in h:
            h[f"d_{c}"] = h[f"{c}_max"].diff().clip(lower=0)
    h["cls"] = [
        classify(
            IntervalSummary(
                compressor_hz_min=r.get("hz_min", np.nan),
                compressor_hz_max=r.get("hz_max", np.nan),
                dhw_heat_kwh=r.get("d_dhw_heat_kwh", np.nan),
                dhw_run_h=r.get("d_dhw_run_h", np.nan),
                backup_kwh=r.get("d_buh_kwh", np.nan),
                flow_l_min_min=r.get("flow_min", np.nan),
                ti_c=r.get("ti_mean", np.nan),
                to_c=r.get("to_mean", np.nan),
            )
        ).value
        for r in h.to_dict("records")
    ]
    return h


def daily(h: pd.DataFrame, to_col: str = "to_mean") -> pd.DataFrame:
    """Daily aggregates; counter sums require ≥ 22 valid hours."""
    g = h.resample("D")
    d = pd.DataFrame(
        {
            "ti": g["ti_mean"].mean(),
            "to": g[to_col].mean(),
            "room_set": g["room_set_mean"].mean(),
            "lwt_set": g["lwt_set_mean"].mean(),
            "hours": g["ti_mean"].count(),
            "day_h": g.size().astype(float),  # 23/25 on daylight-saving change days
        }
    )
    for c in (*COUNTERS, *HOUR_METERS):
        if f"d_{c}" in h:
            d[c] = g[f"d_{c}"].sum(min_count=22)
    d["dti_next"] = d["ti"].shift(-1) - d["ti"]
    d["dhw_hours"] = g["cls"].apply(lambda s: int((s == IntervalClass.DHW.value).sum()))
    return d


def season(df: pd.DataFrame, start: str = "2025-11-01", end: str = "2026-03-31") -> pd.DataFrame:
    return df.loc[start:end]


def split_weeks(index: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic train/test split by ISO-week parity (both get cold and mild weeks)."""
    wk = index.isocalendar().week.to_numpy()
    return wk % 2 == 0, wk % 2 == 1
