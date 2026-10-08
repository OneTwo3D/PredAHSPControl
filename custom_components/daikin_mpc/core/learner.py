"""Bounded recursive least-squares learner for the building energy balance.

Daily model: ``Q_mean = UA·(Ti − To) − G + C·ΔTi/24`` with Q in W, UA in W/K, G in W, C in Wh/K and
ΔTi the change in daily mean room temperature to the next day.

The learner starts from priors (the offline fit) with a covariance, applies a forgetting factor so it
can track slow change, limits how far each parameter may move per update, and rejects days whose
residual is implausibly large. Parameter uncertainty is the posterior standard deviation; it only
shrinks when the data actually excite the parameter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .thermal_model import ThermalParams

STATE_VERSION = 1


@dataclass
class LearnerConfig:
    forgetting: float = 0.995  # per day; effective memory ≈ 200 days
    noise_w: float = 110.0  # daily mean-power noise (offline RMSE ≈ 100 W)
    max_rel_change: dict[str, float] = field(
        default_factory=lambda: {"ua": 0.02, "gains": 0.10, "c": 0.05}
    )  # per update (day)
    bounds: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {"ua": (30.0, 400.0), "gains": (-500.0, 2000.0), "c": (500.0, 30000.0)}
    )
    outlier_sigma: float = 4.0
    min_heat_kwh: float = 1.0  # days with less heat do not identify UA


@dataclass
class UpdateResult:
    accepted: bool
    reason: str
    residual_w: float = float("nan")


class BuildingLearner:
    NAMES = ("ua", "gains", "c")

    def __init__(
        self, prior: ThermalParams, prior_sd: tuple[float, float, float], cfg: LearnerConfig | None = None
    ):
        self.cfg = cfg or LearnerConfig()
        self.theta = np.array([prior.ua_w_per_k, prior.gains_w, prior.c_wh_per_k], dtype=float)
        self.p = np.diag(np.square(np.asarray(prior_sd, dtype=float)))
        self.updates = 0
        self.rejected = 0
        self.last_reason = "no data yet"

    @property
    def params(self) -> ThermalParams:
        return ThermalParams(float(self.theta[0]), float(self.theta[2]), float(self.theta[1]))

    @property
    def sd(self) -> dict[str, float]:
        return {n: float(math.sqrt(max(v, 0.0))) for n, v in zip(self.NAMES, np.diag(self.p), strict=True)}

    def update_day(
        self, ti: float, to: float, heat_kwh: float, dti_next: float, day_h: float = 24.0
    ) -> UpdateResult:
        """Update with one complete day of ``day_h`` hours (23/25 on daylight-saving change days).

        Returns whether it was accepted and why not.
        """
        if not all(math.isfinite(v) for v in (ti, to, heat_kwh, dti_next, day_h)) or not 20 <= day_h <= 28:
            return self._reject("non-finite input")
        if heat_kwh < self.cfg.min_heat_kwh:
            return self._reject("no heating that day")
        x = np.array([ti - to, -1.0, dti_next / day_h])
        y = heat_kwh * 1000.0 / day_h
        lam = self.cfg.forgetting
        r = self.cfg.noise_w**2
        p_pred = self.p / lam
        s = float(x @ p_pred @ x) + r
        resid = y - float(x @ self.theta)
        if abs(resid) > self.cfg.outlier_sigma * math.sqrt(s):
            return self._reject(f"outlier (residual {resid:.0f} W)", resid)
        k = p_pred @ x / s
        new = self.theta + k * resid
        # rate limits and bounds
        for i, n in enumerate(self.NAMES):
            lim = self.cfg.max_rel_change[n] * max(abs(self.theta[i]), 1.0)
            new[i] = float(np.clip(new[i], self.theta[i] - lim, self.theta[i] + lim))
            lo, hi = self.cfg.bounds[n]
            new[i] = float(np.clip(new[i], lo, hi))
        self.theta = new
        self.p = (np.eye(3) - np.outer(k, x)) @ p_pred
        self.p = 0.5 * (self.p + self.p.T)
        self.updates += 1
        self.last_reason = "accepted"
        return UpdateResult(True, "accepted", resid)

    def set_capacity(self, c_wh_per_k: float, sd_wh_per_k: float) -> bool:
        """Take C from a better-identified source (free-cooling nights), replacing the daily estimate.

        The daily balance identifies C poorly; the night estimate becomes authoritative for it. C's
        covariance with UA and gains is reset (they come from different data).
        """
        lo, hi = self.cfg.bounds["c"]
        if not (math.isfinite(c_wh_per_k) and math.isfinite(sd_wh_per_k) and sd_wh_per_k > 0):
            return False
        self.theta[2] = float(np.clip(c_wh_per_k, lo, hi))
        self.p[2, :] = 0.0
        self.p[:, 2] = 0.0
        self.p[2, 2] = sd_wh_per_k**2
        return True

    def _reject(self, reason: str, resid: float = float("nan")) -> UpdateResult:
        self.rejected += 1
        self.last_reason = reason
        return UpdateResult(False, reason, resid)

    # --- persistence -------------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "theta": [float(v) for v in self.theta],
            "p": [[float(v) for v in row] for row in self.p],
            "updates": self.updates,
            "rejected": self.rejected,
        }

    def load_dict(self, d: dict[str, Any]) -> bool:
        """Restore state; returns False (keeping priors) if the data are invalid."""
        try:
            if d.get("version") != STATE_VERSION:
                return False
            theta = np.asarray(d["theta"], dtype=float)
            p = np.asarray(d["p"], dtype=float)
            if (
                theta.shape != (3,)
                or p.shape != (3, 3)
                or not (np.isfinite(theta).all() and np.isfinite(p).all())
            ):
                return False
            for i, n in enumerate(self.NAMES):
                lo, hi = self.cfg.bounds[n]
                if not lo <= theta[i] <= hi:
                    return False
            # a covariance must be symmetric positive semi-definite, or later updates take sqrt(<0)
            if not np.allclose(p, p.T, rtol=1e-6, atol=1e-6) or np.linalg.eigvalsh(0.5 * (p + p.T)).min() < 0:
                return False
            updates, rejected = int(d.get("updates", 0)), int(d.get("rejected", 0))
            if updates < 0 or rejected < 0:
                return False
        except (AttributeError, KeyError, TypeError, ValueError, np.linalg.LinAlgError):
            return False
        self.theta, self.p = theta, 0.5 * (p + p.T)
        self.updates, self.rejected = updates, rejected
        return True
