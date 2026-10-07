"""1R1C building thermal model.

``C · dTi/dt = Q_heat + Q_gains − UA · (Ti − To)``

with ``UA`` in W/K, ``C`` in Wh/K, heat flows in W and time in hours. The step uses the exact
solution for piecewise-constant inputs, so it is stable for any time step.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from .regression import FloatArray, ols

MODEL_VERSION = 1


@dataclass(frozen=True)
class ThermalParams:
    """Building parameters. ``c_wh_per_k / 1000`` gives kWh/K."""

    ua_w_per_k: float
    c_wh_per_k: float
    gains_w: float

    def __post_init__(self) -> None:
        if not (self.ua_w_per_k > 0 and self.c_wh_per_k > 0 and math.isfinite(self.gains_w)):
            raise ValueError(f"invalid thermal parameters: {self}")

    @property
    def time_constant_h(self) -> float:
        return self.c_wh_per_k / self.ua_w_per_k

    def serialize(self) -> dict[str, Any]:
        return {"version": MODEL_VERSION, **asdict(self)}

    @classmethod
    def deserialize(cls, data: dict[str, Any]) -> ThermalParams:
        if data.get("version") != MODEL_VERSION:
            raise ValueError(f"unsupported thermal model version {data.get('version')}")
        return cls(float(data["ua_w_per_k"]), float(data["c_wh_per_k"]), float(data["gains_w"]))


def step(ti_c: float, to_c: float, q_heat_w: float, dt_h: float, p: ThermalParams) -> float:
    """Advance indoor temperature by ``dt_h`` hours with constant inputs (exact solution)."""
    if dt_h < 0:
        raise ValueError("dt_h must be non-negative")
    ti_eq = to_c + (q_heat_w + p.gains_w) / p.ua_w_per_k
    return ti_eq + (ti_c - ti_eq) * math.exp(-dt_h / p.time_constant_h)


def simulate(
    ti0_c: float, to_c: FloatArray, q_heat_w: FloatArray, dt_h: float, p: ThermalParams
) -> FloatArray:
    """Simulate indoor temperature; returns ``len(to_c) + 1`` values starting with ``ti0_c``."""
    out = np.empty(len(to_c) + 1)
    out[0] = ti0_c
    for i, (to, q) in enumerate(zip(to_c, q_heat_w, strict=True)):
        out[i + 1] = step(out[i], float(to), float(q), dt_h, p)
    return out


@dataclass(frozen=True)
class ThermalFit:
    params: ThermalParams
    ua_se: float
    c_se: float
    gains_se: float
    r2: float
    rmse_w: float
    n: int

    @property
    def c_identified(self) -> bool:
        """True when C is distinguishable from zero with reasonable precision (rel. error < 50 %)."""
        return self.c_se < 0.5 * self.params.c_wh_per_k


def fit_daily_energy_balance(
    ti_mean_c: FloatArray,
    to_mean_c: FloatArray,
    heat_kwh: FloatArray,
    dti_next_k: FloatArray,
) -> ThermalFit:
    """Fit UA, gains and C from daily energy balance.

    Per day: ``Q_mean = UA·(Ti − To) − G + C·ΔTi/24`` with ``Q_mean`` the mean heat input in W and
    ``ΔTi`` the change in daily mean indoor temperature to the next day (K). Inputs must already be
    filtered to valid heating days (no missing counters, DHW heat excluded).
    """
    q_w = np.asarray(heat_kwh, float) * 1000.0 / 24.0
    x = np.c_[
        np.asarray(ti_mean_c) - np.asarray(to_mean_c), -np.ones(len(q_w)), np.asarray(dti_next_k) / 24.0
    ]
    r = ols(x, q_w)
    ua, g, c = (float(v) for v in r.coef)
    if c <= 0:
        # C not identifiable from these data: report a nominal value with infinite uncertainty.
        c, c_se = 1.0, float("inf")
    else:
        c_se = float(r.stderr[2])
    return ThermalFit(
        params=ThermalParams(ua, c, g),
        ua_se=float(r.stderr[0]),
        c_se=c_se,
        gains_se=float(r.stderr[1]),
        r2=r.r2,
        rmse_w=r.rmse,
        n=r.n,
    )


def fit_hourly_dynamics(
    ti_c: FloatArray, to_c: FloatArray, q_heat_w: FloatArray, window_h: int = 6
) -> tuple[float, float, float]:
    """Estimate (C [Wh/K], UA [W/K], gains [W]) from ``window_h``-hour indoor temperature changes.

    ``C·ΔTi/Δt = Q − UA·(Ti − To) + G`` regressed with ΔTi over ``window_h`` hours and Q averaged over
    the same window. Radiator thermal lag biases UA low; use it chiefly for C.
    """
    ti = np.asarray(ti_c, float)
    to = np.asarray(to_c, float)
    q = np.asarray(q_heat_w, float)
    n = len(ti) - window_h
    if n <= 3:
        raise ValueError("not enough samples")
    dti = (ti[window_h:] - ti[:-window_h]) / window_h
    q_win = np.convolve(q, np.ones(window_h) / window_h, mode="valid")[:n]
    x = np.c_[q_win, -(ti[:n] - to[:n]), np.ones(n)]
    ok = np.isfinite(x).all(axis=1) & np.isfinite(dti)
    r = ols(x[ok], dti[ok])
    a, b, g = (float(v) for v in r.coef)
    c = 1.0 / a
    return c, b * c, g * c
