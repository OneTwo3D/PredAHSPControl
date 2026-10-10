"""Internal and solar heat gains from measured sources.

Heat released inside the thermal envelope, beyond the heat pump's heat:

* household electricity: house load − heat pump − EV charger − loads outside the envelope (loft IT),
  of which a share ``household_factor`` warms the room (the rest leaves via drains, extractor, other
  rooms; hourly free-running data 2025/26: 0.68 ± 0.19);
* solar radiation through the windows: ``solar_factor`` W per W/m² (winter daily balance: 1.5 ± 0.3);
* battery/inverter conversion losses (Solis, inside the house): ``battery_loss_fraction`` of the battery
  power (Predbat losses: ≈ 4.5 % per direction);
* hot-water tank standing loss: ``tank_ua_w_per_k`` × (tank − room) (180 L cylinder ≈ 1.5 W/K).

The building model's own gains parameter then holds only the unmeasured rest (people, gas/other appliances)
— the "base" gains. Heat-pump and EV-charging losses and the loft IT are outside the envelope and excluded.

For forecasts, household electricity and battery losses follow learned hour-of-day profiles, the tank
term is held at its current value, and the sun follows the Solcast PV forecast converted to radiation
with a learned ratio (station radiation ÷ forecast PV power). Without a forecast, no future sun is assumed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .telemetry import Role, ValidatedSnapshot
from .timeutil import elapsed_s

STATE_VERSION = 1
GAIN_ROLES = (Role.SOLAR, Role.HOUSE_W, Role.EV_W, Role.OUTSIDE_W, Role.BATTERY_W, Role.TANK_C)


@dataclass(frozen=True)
class GainsConfig:
    household_factor: float = 0.7
    solar_factor: float = 1.5  # W per W/m²
    battery_loss_fraction: float = 0.045
    tank_ua_w_per_k: float = 1.5


@dataclass(frozen=True)
class GainsBreakdown:
    household_w: float = 0.0
    solar_w: float = 0.0
    battery_w: float = 0.0
    tank_w: float = 0.0

    @property
    def total_w(self) -> float:
        return self.household_w + self.solar_w + self.battery_w + self.tank_w

    def as_dict(self) -> dict[str, float]:
        return {
            "household_w": round(self.household_w, 1),
            "solar_w": round(self.solar_w, 1),
            "battery_w": round(self.battery_w, 1),
            "tank_w": round(self.tank_w, 1),
            "total_w": round(self.total_w, 1),
        }


def household_electricity_w(s: ValidatedSnapshot) -> float | None:
    """Electricity used inside the envelope: house load minus heat pump, EV charger and outside loads."""
    house = s.get(Role.HOUSE_W)
    if house is None:
        return None
    hp = s.get(Role.EXT_W)
    if hp is None:
        hp = s.get(Role.ELEC_W)
    out = house - (hp or 0.0) - (s.get(Role.EV_W) or 0.0) - (s.get(Role.OUTSIDE_W) or 0.0)
    return max(0.0, out)


def measured_gains(s: ValidatedSnapshot, cfg: GainsConfig) -> GainsBreakdown:
    """Gains now from the mapped sources; an unmapped or invalid source contributes nothing."""
    ti = s.get(Role.TI)
    household = household_electricity_w(s)
    sol = s.get(Role.SOLAR)
    bat = s.get(Role.BATTERY_W)
    tank = s.get(Role.TANK_C)
    return GainsBreakdown(
        household_w=cfg.household_factor * household if household is not None else 0.0,
        solar_w=cfg.solar_factor * max(0.0, sol) if sol is not None else 0.0,
        battery_w=cfg.battery_loss_fraction * abs(bat) if bat is not None else 0.0,
        tank_w=cfg.tank_ua_w_per_k * max(0.0, tank - ti) if tank is not None and ti is not None else 0.0,
    )


@dataclass
class GainsForecaster:
    """Hour-of-day profiles (household, battery |P|) and the Solcast → radiation ratio."""

    cfg: GainsConfig = field(default_factory=GainsConfig)
    alpha: float = 0.05  # profile update weight per sample (≈ 20 samples ≈ several days per hour slot)
    household: dict[int, float] = field(default_factory=dict)  # hour -> W (before household_factor)
    battery: dict[int, float] = field(default_factory=dict)  # hour -> |W|
    solar_ratio: float = 0.13  # W/m² per W of forecast PV (station 299 W/m² vs Solcast 2.24 kW, 10 Oct 2026)
    ratio_samples: int = 0

    def learn(self, s: ValidatedSnapshot, pv_forecast_w: float | None) -> None:
        """Update the profiles from one snapshot, and the ratio when sun is measured and forecast."""
        hod = s.time.hour
        household = household_electricity_w(s)
        if household is not None:
            self.household[hod] = _ema(self.household.get(hod), household, self.alpha)
        bat = s.get(Role.BATTERY_W)
        if bat is not None:
            self.battery[hod] = _ema(self.battery.get(hod), abs(bat), self.alpha)
        sol = s.get(Role.SOLAR)
        if sol is not None and pv_forecast_w is not None and pv_forecast_w > 300 and sol > 20:
            ratio = sol / pv_forecast_w
            if 0.01 <= ratio <= 2.0:
                self.solar_ratio = _ema(self.solar_ratio, ratio, 0.02)
                self.ratio_samples += 1

    def forecast(
        self,
        times: Sequence[datetime],
        now: GainsBreakdown,
        pv_forecast: list[tuple[datetime, float]] | None,
        measured_solar_w_m2: float | None,
    ) -> list[float]:
        """Extra gains (W) at each time (step mid-points, local time).

        Within the first hour the measured household/battery values are used; later the hour-of-day
        profiles (falling back to the measured value). The tank term is held. Sun: Solcast × ratio;
        without Solcast, the measured radiation for the first hour and none afterwards.
        """
        out: list[float] = []
        t0 = times[0] if times else None
        for t in times:
            ahead_h = elapsed_s(t0, t) / 3600 if t0 is not None else 0.0
            hod = t.hour
            if ahead_h < 1.0:
                household, battery = now.household_w, now.battery_w
            else:
                hh = self.household.get(hod)
                household = self.cfg.household_factor * hh if hh is not None else now.household_w
                bb = self.battery.get(hod)
                battery = self.cfg.battery_loss_fraction * bb if bb is not None else now.battery_w
            pv = pv_at(pv_forecast, t) if pv_forecast else None
            if pv is not None:
                solar = self.cfg.solar_factor * self.solar_ratio * pv
            elif ahead_h < 1.0 and measured_solar_w_m2 is not None:
                solar = self.cfg.solar_factor * max(0.0, measured_solar_w_m2)
            else:
                solar = 0.0
            out.append(household + battery + now.tank_w + solar)
        return out

    # --- persistence -----------------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "household": {str(k): v for k, v in self.household.items()},
            "battery": {str(k): v for k, v in self.battery.items()},
            "solar_ratio": self.solar_ratio,
            "ratio_samples": self.ratio_samples,
        }

    def load_dict(self, d: dict[str, Any]) -> bool:
        try:
            if d.get("version") != STATE_VERSION:
                return False
            hh = {int(k): float(v) for k, v in dict(d.get("household") or {}).items()}
            bb = {int(k): float(v) for k, v in dict(d.get("battery") or {}).items()}
            ratio = float(d.get("solar_ratio", 0.13))
            n = int(d.get("ratio_samples", 0))
            if not all(
                0 <= k < 24 and math.isfinite(v) and 0 <= v <= 30000 for k, v in (*hh.items(), *bb.items())
            ):
                return False
            if not (math.isfinite(ratio) and 0.01 <= ratio <= 2.0) or n < 0:
                return False
        except (AttributeError, TypeError, ValueError):
            return False
        self.household, self.battery, self.solar_ratio, self.ratio_samples = hh, bb, ratio, n
        return True


def _ema(prev: float | None, x: float, a: float) -> float:
    return x if prev is None else (1 - a) * prev + a * x


def pv_at(series: list[tuple[datetime, float]], t: datetime) -> float | None:
    """Forecast PV power (W) of the 30-minute period containing ``t``; None outside the series."""
    for start, w in series:
        dt = elapsed_s(start, t)
        if 0 <= dt < 1800:
            return w
    return None


def parse_solcast(attrs: Sequence[object]) -> list[tuple[datetime, float]]:
    """Solcast ``detailedForecast`` lists → sorted (period start, PV W). Malformed items are skipped."""
    out: list[tuple[datetime, float]] = []
    for lst in attrs:
        if not isinstance(lst, list):
            continue
        for it in lst:
            if not isinstance(it, dict):
                continue
            try:
                t = datetime.fromisoformat(str(it.get("period_start")))
                kw = float(it.get("pv_estimate"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if t.tzinfo is not None and math.isfinite(kw) and 0 <= kw <= 100:
                out.append((t, kw * 1000.0))
    return sorted(out)
