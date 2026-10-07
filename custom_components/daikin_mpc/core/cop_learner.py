"""Live COP-by-outdoor-temperature learner and standby estimate from an accurate external meter.

Per complete heating day: heat from the Daikin heating counter, electricity from the external meter
minus the Daikin DHW electricity minus standby × day length. The COP therefore covers the running heat
pump including its circulation pump but excludes standby, which is added once, separately, in the
forecasts (and does not depend on the heating schedule). Sums are kept per outdoor-temperature bin with
exponential forgetting and turned into a monotone COP curve; bins with too little energy fall back to
the prior curve.

The heat counter may have a scale error (see docs/meter_check.md). Building UA/gains are learned from
the same counter, so heat × (1/COP) — the electricity forecast — is unaffected by that scale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .heatpump_model import CopCurve, _pava

STATE_VERSION = 2  # 2: standby excluded from the COP basis


@dataclass
class CopLearner:
    prior: CopCurve
    edges_c: tuple[float, ...] = (-15.0, 0.0, 3.0, 6.0, 9.0, 12.0, 18.0)
    forgetting: float = 0.995  # per day
    min_elec_kwh: float = 10.0  # per bin before it overrides the prior
    heat: list[float] = field(default_factory=list)
    elec: list[float] = field(default_factory=list)
    to_w: list[float] = field(default_factory=list)  # electricity-weighted outdoor temperature sum
    standby_w: float | None = None
    days: int = 0

    def __post_init__(self) -> None:
        n = len(self.edges_c) - 1
        self.heat = self.heat or [0.0] * n
        self.elec = self.elec or [0.0] * n
        self.to_w = self.to_w or [0.0] * n

    def update_day(
        self,
        to_c: float,
        heat_kwh: float,
        heating_elec_kwh: float,
        standby_w: float | None,
        day_h: float = 24.0,
        default_standby_w: float = 0.0,
    ) -> bool:
        """``heating_elec_kwh`` includes standby; it is removed here using the learned standby."""
        if standby_w is not None and math.isfinite(standby_w) and 0 <= standby_w <= 200:
            self.standby_w = standby_w if self.standby_w is None else 0.9 * self.standby_w + 0.1 * standby_w
        if not all(math.isfinite(v) for v in (to_c, heat_kwh, heating_elec_kwh, day_h)):
            return False
        sb = self.standby_w if self.standby_w is not None else default_standby_w
        heating_elec_kwh -= sb * day_h / 1000.0
        if heating_elec_kwh < 1.0:
            return False
        cop = heat_kwh / heating_elec_kwh
        if not 1.0 <= cop <= 7.0:
            return False
        lam = self.forgetting
        self.heat = [h * lam for h in self.heat]
        self.elec = [e * lam for e in self.elec]
        self.to_w = [t * lam for t in self.to_w]
        i = int(np.clip(np.searchsorted(self.edges_c, to_c, side="right") - 1, 0, len(self.heat) - 1))
        self.heat[i] += heat_kwh
        self.elec[i] += heating_elec_kwh
        self.to_w[i] += to_c * heating_elec_kwh
        self.days += 1
        return True

    def curve(self) -> tuple[CopCurve, int]:
        """Current curve and the number of bins backed by measurements."""
        centres, cops, weights, learned = [], [], [], 0
        for i, (h, e) in enumerate(zip(self.heat, self.elec, strict=True)):
            if e >= self.min_elec_kwh:
                t = self.to_w[i] / e
                centres.append(t)
                cops.append(h / e)
                weights.append(e)
                learned += 1
        # fill the remaining range from the prior so the curve covers it
        for t, c in zip(self.prior.centres_c, self.prior.cop, strict=True):
            if not any(abs(t - x) < 1.5 for x in centres):
                centres.append(t)
                cops.append(c)
                weights.append(self.min_elec_kwh / 2)
        order = np.argsort(centres)
        xs = [float(centres[k]) for k in order]
        ys = _pava(np.asarray([cops[k] for k in order]), np.asarray([weights[k] for k in order]))
        return CopCurve(tuple(xs), tuple(float(v) for v in ys), (), ()), learned

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "heat": self.heat,
            "elec": self.elec,
            "to_w": self.to_w,
            "standby_w": self.standby_w,
            "days": self.days,
            "edges": list(self.edges_c),
        }

    def load_dict(self, d: dict[str, Any]) -> bool:
        try:
            if d.get("version") != STATE_VERSION or list(d["edges"]) != list(self.edges_c):
                return False
            heat, elec, to_w = ([float(x) for x in d[k]] for k in ("heat", "elec", "to_w"))
            n = len(self.edges_c) - 1
            if not (len(heat) == len(elec) == len(to_w) == n):
                return False
            if not all(math.isfinite(x) and x >= 0 for x in heat + elec) or not all(
                math.isfinite(x) for x in to_w
            ):
                return False
            for h, e, t in zip(heat, elec, to_w, strict=True):
                # each populated bin must give a plausible COP and a mean outdoor temperature in range
                if e > 0 and not (1.0 <= h / e <= 7.0 and -40.0 <= t / e <= 50.0):
                    return False
            sb_raw = d.get("standby_w")
            sb = float(sb_raw) if sb_raw is not None else None
            if sb is not None and not (math.isfinite(sb) and 0 <= sb <= 200):
                return False
            days = int(d.get("days", 0))
            if days < 0:
                return False
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
        self.heat, self.elec, self.to_w = heat, elec, to_w
        self.standby_w, self.days = sb, days
        return True
