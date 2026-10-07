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
            _increments(h, src, c)
    for c in HOUR_METERS:
        if f"{c}_max" in h:
            _increments(h, f"{c}_max", c)
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


def _increments(h: pd.DataFrame, src: str, key: str) -> None:
    """Counter increments between consecutive valid readings.

    ``d_<key>``: per-hour increment, NaN after a missing reading (cannot be placed in an hour).
    ``dd_<key>``: increment booked to the hour of the next valid reading, so daily totals keep the
    energy of same-day gaps. ``gapx_<key>``: the gap behind this reading crosses local midnight, so
    neither day's total is complete.
    """
    v = h[src].dropna()
    inc = v.diff().clip(lower=0)
    prev_t = v.index.to_series().shift()
    span = v.index.to_series() - prev_t
    one_h = span == pd.Timedelta(hours=1)
    h[f"d_{key}"] = inc.where(one_h).reindex(h.index)
    h[f"dd_{key}"] = inc.reindex(h.index)
    crosses = (~one_h) & prev_t.notna() & (prev_t.dt.date != v.index.to_series().dt.date)
    h[f"gapx_{key}"] = crosses.reindex(h.index, fill_value=False).astype(bool)
    # the earlier date's total is incomplete too
    src_days = set(prev_t[crosses].dt.date)
    if src_days:
        h.loc[[t.date() in src_days for t in h.index], f"gapx_{key}"] = True


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
        if f"dd_{c}" in h:
            # complete accounting: every increment of the day included (same-day gaps are booked to the
            # next reading); days touched by a gap across midnight are dropped
            total = g[f"dd_{c}"].sum(min_count=1)
            d[c] = total.where(~g[f"gapx_{c}"].any())
    d["dti_next"] = d["ti"].shift(-1) - d["ti"]
    d["dhw_hours"] = g["cls"].apply(lambda s: int((s == IntervalClass.DHW.value).sum()))
    return d


def season(df: pd.DataFrame, start: str = "2025-11-01", end: str = "2026-03-31") -> pd.DataFrame:
    return df.loc[start:end]


def split_weeks(index: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic train/test split by ISO-week parity (both get cold and mild weeks)."""
    wk = index.isocalendar().week.to_numpy()
    return wk % 2 == 0, wk % 2 == 1
