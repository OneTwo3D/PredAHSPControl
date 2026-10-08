"""Thermal capacity from free-cooling nights.

With the heat pump off, the single-node building model reduces to::

    dTi/dt = G_night / C − (UA / C) · (Ti − To)

so the hourly cooling rate is a straight line in the indoor–outdoor difference: the slope is ``UA / C``
(the inverse time constant), the intercept ``G_night / C``. The daily energy balance identifies UA well
but C poorly (with heating on, the room temperature barely changes from day to day), whereas a night
without heating measures the time constant directly. With UA from the building learner, ``C = UA / slope``
in the same (heat-counter) units as the rest of the model.

Only clean night hours are used: pairs of consecutive hours starting 22:00–04:00 local time, compressor
off in both and in the hour before (radiators no longer releasing heat), no DHW, no defrost, good
coverage. Evenings (cooking, occupancy) and daytime (sun) are excluded. Weighted least-squares sums are
kept with exponential forgetting; an estimate is only offered once the data identify it (enough hours
and nights, enough spread in the temperature difference, slope clearly positive).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .accumulator import HourRecord
from .intervals import IntervalClass
from .timeutil import elapsed_s

STATE_VERSION = 1
NIGHT_START_HOURS = frozenset({22, 23, 0, 1, 2, 3, 4})  # pair start; the pair ends by 06:00


@dataclass
class CapacityEstimate:
    c_wh_per_k: float
    sd_wh_per_k: float
    tau_h: float
    gains_night_w: float
    pairs: float
    nights: int


@dataclass
class CapacityLearner:
    # Forgetting by elapsed time, not by pair count: summer gives few usable nights, and a pair-based
    # memory would let spring data (sun-charged fabric, slower cooling) dominate into the autumn.
    forgetting_per_day: float = 0.98  # ≈ 50-day memory
    min_pairs: float = 30.0
    min_nights: int = 4
    min_spread_k: float = 1.5  # standard deviation of Ti − To across the pairs
    # Only pairs with a clear indoor–outdoor difference: on mild (summer) nights the loss is small and
    # heat stored by the sun in the fabric dominates, which biases the slope (data 2025/26: C drifted
    # to 11 kWh/K with negative night gains when such nights were included).
    min_dt_k: float = 6.0
    max_rel_sd: float = 0.3
    n: float = 0.0
    sx: float = 0.0
    sy: float = 0.0
    sxx: float = 0.0
    sxy: float = 0.0
    syy: float = 0.0
    nights: int = 0
    last_night: str | None = None  # local date on which the last counted night began
    last_time: datetime | None = None  # time of the last accepted pair (for forgetting)

    @staticmethod
    def clean_pair(before: HourRecord, h0: HourRecord, h1: HourRecord) -> tuple[float, float] | None:
        """(Ti − To, ΔTi per hour) for a usable pair ``h0`` → ``h1``, else None."""
        hours = (before, h0, h1)
        if not (elapsed_s(before.start, h0.start) == 3600 and elapsed_s(h0.start, h1.start) == 3600):
            return None
        if h0.start.hour not in NIGHT_START_HOURS:
            return None
        for h in hours:
            # compressor known to be off all hour (NaN = unknown is rejected too)
            if h.hz_max != 0 or h.dhw or h.defrost or h.cls == IntervalClass.INVALID.value:
                return None
        for h in (h0, h1):
            if h.coverage < 0.75 or "ti" not in h.means or "to" not in h.means:
                return None
            if h.deltas_kwh.get("dhw_heat_kwh", 0.0) > 0 or h.deltas_kwh.get("heat_kwh", 0.0) > 0:
                return None
        x = (h0.means["ti"] + h1.means["ti"]) / 2 - (h0.means["to"] + h1.means["to"]) / 2
        y = h1.means["ti"] - h0.means["ti"]
        if not (math.isfinite(x) and math.isfinite(y)) or abs(y) > 2.0:
            return None
        return x, y

    def add(self, before: HourRecord, h0: HourRecord, h1: HourRecord) -> bool:
        """Add one hour pair if it is a clean free-cooling pair; returns whether it was used."""
        pair = self.clean_pair(before, h0, h1)
        if pair is None or pair[0] < self.min_dt_k:
            return False
        return self._add(h0, *pair)

    def _add(self, h0: HourRecord, x: float, y: float) -> bool:
        days = 0.0 if self.last_time is None else max(0.0, elapsed_s(self.last_time, h0.start) / 86400)
        lam = self.forgetting_per_day**days
        self.last_time = h0.start
        self.n, self.sx, self.sy = self.n * lam + 1, self.sx * lam + x, self.sy * lam + y
        self.sxx, self.sxy, self.syy = self.sxx * lam + x * x, self.sxy * lam + x * y, self.syy * lam + y * y
        # a night belongs to the date it began on (hours after midnight count for the evening before)
        night = h0.start.date() if h0.start.hour >= 22 else h0.start.date() - timedelta(days=1)
        if night.isoformat() != self.last_night:
            self.nights += 1
            self.last_night = night.isoformat()
        return True

    def estimate(self, ua_w_per_k: float) -> CapacityEstimate | None:
        """C from the cooling slope and the current UA, once the data identify it."""
        if self.n < self.min_pairs or self.nights < self.min_nights or not ua_w_per_k > 0:
            return None
        mx, my = self.sx / self.n, self.sy / self.n
        vxx = self.sxx / self.n - mx * mx
        if vxx <= 0 or math.sqrt(vxx) < self.min_spread_k:
            return None
        cov = self.sxy / self.n - mx * my
        slope = -cov / vxx  # y = −slope·x + b
        if slope <= 0:
            return None
        b = my + slope * mx
        resid = max(self.syy / self.n - my * my - cov * cov / vxx, 0.0)
        sd_slope = math.sqrt(resid / max(self.n - 2, 1.0) / vxx)
        c = ua_w_per_k / slope
        # floor: the single-node model itself is only approximate, so C is never "exactly" known
        sd_c = max(ua_w_per_k * sd_slope / slope**2, 0.05 * c)
        if not math.isfinite(c) or sd_c > self.max_rel_sd * c:
            return None
        return CapacityEstimate(c, sd_c, 1 / slope, b * c, self.n, self.nights)

    # --- persistence -----------------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "sums": [self.n, self.sx, self.sy, self.sxx, self.sxy, self.syy],
            "nights": self.nights,
            "last_night": self.last_night,
            "last_time": self.last_time.isoformat() if self.last_time else None,
        }

    def load_dict(self, d: dict[str, Any]) -> bool:
        try:
            if d.get("version") != STATE_VERSION:
                return False
            n, sx, sy, sxx, sxy, syy = (float(v) for v in d["sums"])
            nights = int(d.get("nights", 0))
            last = d.get("last_night")
            lt = d.get("last_time")
            last_time = datetime.fromisoformat(str(lt)) if lt else None
            if last_time is not None and last_time.tzinfo is None:
                return False
            if not all(math.isfinite(v) for v in (n, sx, sy, sxx, sxy, syy)):
                return False
            if n < 0 or sxx < 0 or syy < 0 or nights < 0:
                return False
            if n > 0 and (
                sxx * n < sx * sx - 1e-6 * max(1.0, sx * sx) or syy * n < sy * sy - 1e-6 * max(1.0, sy * sy)
            ):
                return False  # impossible sums (negative variance)
            if last is not None:
                date.fromisoformat(str(last))
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
        self.n, self.sx, self.sy, self.sxx, self.sxy, self.syy = n, sx, sy, sxx, sxy, syy
        self.nights, self.last_night = nights, None if last is None else str(last)
        self.last_time = last_time
        return True
