"""Offline re-implementation of Predheat's heating simulation, for replay and calibration.

Written independently from the published equations of Predheat (springfall2008/batpred,
``apps/predbat/predheat.py``) so that parameter sets can be evaluated against recorded data. It
models: a thermostat with hysteresis, water-volume temperature, flow-temperature-driven output
modulation between min/max power, radiator emission via ΔT correction tables, a COP that scales
with outdoor temperature, and the building as a single capacity. Behavioural differences from the
upstream code are bugs; check upstream when Predheat changes.

Units: W, Wh/K, °C, litres, minutes for the 5-minute step.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np

from .regression import FloatArray

STEP_MIN = 5
WATTS_TO_DEGREES = 1.16  # W·h per litre per K (as used by Predheat)

DEFAULT_DELTA_CORRECTION: dict[int, float] = {
    75: 1.69,
    70: 1.55,
    65: 1.41,
    60: 1.27,
    55: 1.13,
    50: 1.0,
    45: 0.87,
    40: 0.75,
    35: 0.63,
    30: 0.51,
    25: 0.41,
    20: 0.3,
    15: 0.21,
    10: 0.12,
    5: 0.05,
    0: 0.0,
}
DEFAULT_HEAT_PUMP_EFFICIENCY: dict[int, float] = {
    -20: 2.10,
    -18: 2.15,
    -16: 2.2,
    -14: 2.25,
    -12: 2.3,
    -10: 2.4,
    -8: 2.5,
    -6: 2.6,
    -4: 2.7,
    -2: 2.8,
    0: 2.9,
    2: 3.1,
    4: 3.3,
    6: 3.6,
    8: 3.8,
    10: 3.9,
    12: 4.1,
    14: 4.3,
    16: 4.3,
    18: 4.3,
    20: 4.3,
}


def fill_table(table: Mapping[int, float]) -> dict[int, float]:
    """Linearly interpolate an integer-keyed table onto every integer key between its min and max."""
    if not table:
        return {}
    keys = sorted(table)
    xs = np.arange(keys[0], keys[-1] + 1)
    ys = np.interp(xs, keys, [table[k] for k in keys])
    return {int(x): round(float(y), 4) for x, y in zip(xs, ys, strict=True)}


@dataclass(frozen=True)
class PredheatConfig:
    """Subset of the ``predheat:`` section of Predbat's ``apps.yaml`` (heat-pump mode)."""

    heat_loss_watts: float
    heat_loss_degrees: float
    heat_gain_static: float
    heat_output: float
    heat_volume: float
    heat_max_power: float
    heat_min_power: float
    heat_cop: float
    flow_difference_target: float
    hysteresis: float
    hysteresis_off: float
    weather_compensation: Mapping[int, float] | None = None
    heat_pump_efficiency: Mapping[int, float] = field(default_factory=dict)
    delta_correction: Mapping[int, float] = field(default_factory=dict)

    @property
    def watt_per_degree(self) -> float:
        """Building heat capacity in Wh/K as implied by Predheat's parameters."""
        return self.heat_loss_watts / self.heat_loss_degrees


@dataclass(frozen=True)
class SimResult:
    internal_c: FloatArray  # indoor temperature at the end of each step
    electric_wh: FloatArray  # electrical energy per step
    heat_out_w: FloatArray  # heat-pump output per step
    heating_on: FloatArray  # thermostat state per step (0/1)


class PredheatSim:
    def __init__(self, cfg: PredheatConfig) -> None:
        self.cfg = cfg
        # User tables are merged into the defaults (as Predheat does), then gap-filled.
        self.delta = fill_table({**DEFAULT_DELTA_CORRECTION, **cfg.delta_correction})
        self.eff = fill_table({**DEFAULT_HEAT_PUMP_EFFICIENCY, **cfg.heat_pump_efficiency})
        self.eff_max = max(self.eff.values())
        self.wc = fill_table(cfg.weather_compensation) if cfg.weather_compensation else None

    def run(
        self,
        internal0_c: float,
        volume0_c: float,
        heating_active: bool,
        external_c: FloatArray,
        target_c: FloatArray,
        flow_temp_c: FloatArray | None = None,
    ) -> SimResult:
        """Simulate ``len(external_c)`` steps of ``STEP_MIN`` minutes.

        ``flow_temp_c`` is only used when no weather-compensation table is configured.
        """
        c = self.cfg
        n = len(external_c)
        dt_h = STEP_MIN / 60.0
        internal, volume, heating_on = float(internal0_c), float(volume0_c), bool(heating_active)
        out_t = np.empty(n)
        out_e = np.zeros(n)
        out_q = np.zeros(n)
        out_on = np.zeros(n)
        for i in range(n):
            ext = float(external_c[i])
            target = float(target_c[i])
            diff_inside = target - internal
            if i > 0:
                if heating_on and diff_inside <= -c.hysteresis:
                    heating_on = False
                elif not heating_on and diff_inside >= c.hysteresis_off:
                    heating_on = True

            loss_wh = (c.heat_loss_watts * (internal - ext) - c.heat_gain_static) * dt_h
            if heating_on:
                out_temp = min(max(int(ext + 0.5), -20), 20)
                if self.wc is not None:
                    flow = self.wc.get(out_temp, max(self.wc.values()))
                elif flow_temp_c is not None:
                    flow = float(flow_temp_c[i])
                else:
                    raise ValueError("flow temperature required without weather compensation")
                q_out = 0.0
                if volume < flow:
                    pct = min(flow - volume, c.flow_difference_target) / c.flow_difference_target
                    q_out = min(c.heat_max_power, max(c.heat_min_power, c.heat_max_power * pct))
                cop_adjust = self.eff.get(out_temp, self.eff_max) / self.eff_max
                out_e[i] = q_out / (c.heat_cop * cop_adjust) * dt_h
                out_q[i] = q_out
                volume += q_out / WATTS_TO_DEGREES / c.heat_volume * dt_h
            out_on[i] = 1.0 if heating_on else 0.0

            delta = min(max(int(volume - internal), 0), 75)
            rad_w = c.heat_output * self.delta.get(delta, 1.0)
            volume -= rad_w / WATTS_TO_DEGREES / c.heat_volume * dt_h
            loss_wh -= rad_w * dt_h
            internal -= loss_wh / c.watt_per_degree
            out_t[i] = internal
        return SimResult(out_t, out_e, out_q, out_on)
