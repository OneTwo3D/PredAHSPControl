"""Online fit of the unit's own LWT setpoint: ``LWT_set = a + b·To + g·(setpoint − Ti)``.

``a + b·To`` is the native weather curve, ``g`` the room-thermostat (RT) modulation. Fitted by weighted
least squares with exponential forgetting (one sample per space-heating step), lightly regularised
towards the prior so a short or one-sided data set cannot produce nonsense. The estimate is only used
once enough samples exist and the outdoor temperature has varied enough to identify the slope.

Why: the hard-coded curve (−23 → 47, 10 → 25 °C) and an RT gain of 5 K/K did not match the unit; the
Nov 2026 regression over 6,403 space-heating minutes gave 30.6 − 0.58·To + 0.655·(setpoint − Ti).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

STATE_VERSION = 1


@dataclass
class WeatherCurveLearner:
    prior: tuple[float, float, float] = (30.6, -0.58, 0.65)  # intercept °C, slope K/K, RT gain K/K
    prior_weight: tuple[float, float, float] = (1.0, 5.0, 2.0)  # ridge strength (in samples)
    forgetting: float = 0.9995  # per sample (5-min steps: memory ~2,000 samples ≈ a week of heating)
    min_samples: float = 288.0  # ~one day of heating steps before the estimate is used
    min_to_var: float = 1.0  # K²: outdoor temperature must have varied to identify the slope
    err_clip_k: float = 3.0
    bounds: tuple[tuple[float, float], ...] = ((15.0, 60.0), (-1.5, 0.0), (0.0, 5.0))
    s_xx: list[list[float]] = field(default_factory=lambda: [[0.0] * 3 for _ in range(3)])
    s_xy: list[float] = field(default_factory=lambda: [0.0] * 3)

    def add(self, to_c: float, err_k: float, lwt_set_c: float) -> bool:
        if not all(math.isfinite(v) for v in (to_c, err_k, lwt_set_c)):
            return False
        x = (1.0, to_c, max(-self.err_clip_k, min(self.err_clip_k, err_k)))
        lam = self.forgetting
        for i in range(3):
            self.s_xy[i] = lam * self.s_xy[i] + x[i] * lwt_set_c
            for j in range(3):
                self.s_xx[i][j] = lam * self.s_xx[i][j] + x[i] * x[j]
        return True

    @property
    def n(self) -> float:
        return self.s_xx[0][0]

    def estimate(self) -> tuple[float, float, float] | None:
        n = self.n
        if n < self.min_samples:
            return None
        mean_to = self.s_xx[0][1] / n
        if self.s_xx[1][1] / n - mean_to**2 < self.min_to_var:
            return None
        r = np.diag(self.prior_weight)
        a = np.array(self.s_xx) + r
        b = np.array(self.s_xy) + r @ np.array(self.prior)
        try:
            theta = np.linalg.solve(a, b)
        except np.linalg.LinAlgError:
            return None
        out = [float(min(hi, max(lo, v))) for v, (lo, hi) in zip(theta, self.bounds, strict=True)]
        return out[0], out[1], out[2]

    def to_dict(self) -> dict[str, Any]:
        # copies: HA serialises saved state later, in a worker thread, while samples keep arriving
        return {"version": STATE_VERSION, "s_xx": [list(r) for r in self.s_xx], "s_xy": list(self.s_xy)}

    def load_dict(self, d: dict[str, Any]) -> bool:
        if d.get("version") != STATE_VERSION:
            return False
        xx = [[float(v) for v in row] for row in d.get("s_xx") or []]
        xy = [float(v) for v in d.get("s_xy") or []]
        if len(xx) != 3 or any(len(r) != 3 for r in xx) or len(xy) != 3:
            return False
        if not all(math.isfinite(v) for v in (*xy, *(v for r in xx for v in r))) or xx[0][0] < 0:
            return False
        self.s_xx, self.s_xy = xx, xy
        return True
