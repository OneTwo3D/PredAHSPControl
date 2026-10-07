"""Radiator emitter model ``Q = K · max(0, MWT − Ti)^n``.

``MWT`` is the mean water temperature ``(LWT + RWT)/2`` in °C, ``Q`` in W. EN 442 radiators have
``n ≈ 1.3``. The inverse gives the water temperature needed to deliver a heat demand, which is the
core quantity for low-and-slow control.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .regression import FloatArray, ols

DEFAULT_N = 1.3


@dataclass(frozen=True)
class RadiatorParams:
    k_w_per_kn: float
    n: float = DEFAULT_N

    def output_w(self, mwt_c: float, ti_c: float) -> float:
        return float(self.k_w_per_kn * max(0.0, mwt_c - ti_c) ** self.n)

    def required_mwt_c(self, q_w: float, ti_c: float) -> float:
        """Mean water temperature needed to emit ``q_w`` watts into a room at ``ti_c``."""
        if q_w <= 0:
            return ti_c
        return float(ti_c + (q_w / self.k_w_per_kn) ** (1.0 / self.n))

    def rated_output_w(self, delta_t_k: float = 50.0) -> float:
        return float(self.k_w_per_kn * delta_t_k**self.n)


@dataclass(frozen=True)
class RadiatorFit:
    params: RadiatorParams
    k_iqr: tuple[float, float]
    n_free: float
    n_samples: int


def fit(
    q_w: FloatArray, mwt_c: FloatArray, ti_c: FloatArray, n: float = DEFAULT_N, min_dt_k: float = 3.0
) -> RadiatorFit:
    """Fit K for fixed ``n`` (robust median) and report a free log-space estimate of ``n``.

    Use only steady, full-run intervals without DHW, where the hydronic heat equals emitted heat.
    """
    q = np.asarray(q_w, float)
    dt = np.asarray(mwt_c, float) - np.asarray(ti_c, float)
    ok = np.isfinite(q) & np.isfinite(dt) & (dt > min_dt_k) & (q > 0)
    if ok.sum() < 10:
        raise ValueError("not enough valid samples for emitter fit")
    k = q[ok] / dt[ok] ** n
    r = ols(np.c_[np.log(dt[ok]), np.ones(ok.sum())], np.log(q[ok]))
    return RadiatorFit(
        params=RadiatorParams(float(np.median(k)), n),
        k_iqr=(float(np.quantile(k, 0.25)), float(np.quantile(k, 0.75))),
        n_free=float(r.coef[0]),
        n_samples=int(ok.sum()),
    )


def hydronic_heat_w(
    flow_l_per_min: FloatArray,
    lwt_c: FloatArray,
    rwt_c: FloatArray,
    cp_j_per_kg_k: float = 4180.0,
    density_kg_per_l: float = 1.0,
) -> FloatArray:
    """Heat from flow and temperature drop: ``ṁ·cp·(LWT − RWT)`` in W (flow in L/min)."""
    m_kg_s = np.asarray(flow_l_per_min, float) * density_kg_per_l / 60.0
    return np.asarray(
        m_kg_s * cp_j_per_kg_k * (np.asarray(lwt_c, float) - np.asarray(rwt_c, float)), dtype=float
    )
