"""Room-temperature and heating-energy forecast for the current (native) Daikin behaviour.

Shadow-mode forecast: predicts what the house will do if the Daikin keeps running as it does now.
Heat is delivered only while the room thermostat calls (hysteresis band around the setpoint) and space
heating is enabled. While running, the heat reaching the room follows the radiator model at the requested
LWT (never below the plant minimum), capped at the heat pump's maximum. When the radiators take less
than the heat pump's minimum output the compressor cycles on water temperature; averaged over the
cycle the heat delivered is still what the radiators emit, so no minimum-output floor is applied to
the heat (cycling losses are inside the measured COP). Electricity is heat / COP(To).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .emitter_model import RadiatorParams
from .thermal_model import ThermalParams, step

MIN_LWT_C = 25.0  # plant minimum achievable leaving-water temperature


@dataclass(frozen=True)
class PlantParams:
    radiator: RadiatorParams
    q_min_w: float = 680.0
    q_max_w: float = 4300.0
    flow_return_dt_k: float = 2.0
    hysteresis_on_k: float = 1.0  # starts when Ti <= setpoint - this
    hysteresis_off_k: float = 0.5  # stops when Ti >= setpoint + this
    min_lwt_c: float = MIN_LWT_C
    # Daikin RT mode raises/lowers the requested LWT with the room-temperature error (field settings
    # 8-05/8-06: modulation on, max 5 K). Gain is a prior until learned from data.
    rt_modulation_gain_k_per_k: float = 5.0
    rt_modulation_max_k: float = 5.0


@dataclass(frozen=True)
class Forecast:
    step_h: float
    ti_c: list[float]  # len = steps + 1, first is the start value
    heat_wh: list[float]  # per step
    elec_wh: list[float]  # per step
    running: list[bool]  # per step

    def ti_at(self, hours: float) -> float:
        i = round(hours / self.step_h)
        return self.ti_c[min(max(i, 0), len(self.ti_c) - 1)]

    def energy_kwh(self, hours: float) -> tuple[float, float]:
        n = min(len(self.heat_wh), round(hours / self.step_h))
        return sum(self.heat_wh[:n]) / 1000, sum(self.elec_wh[:n]) / 1000


def plant_step(
    ti: float,
    running: bool,
    to: float,
    sp: float,
    lwt_set: float,
    heating_enabled: bool,
    building: ThermalParams,
    plant: PlantParams,
    step_h: float,
) -> tuple[float, bool, float]:
    """Advance one step: thermostat decision, heat delivered, room temperature.

    Returns ``(new room temperature, thermostat calling, heat delivered in W)``.
    """
    if heating_enabled:
        if running and ti >= sp + plant.hysteresis_off_k:
            running = False
        elif not running and ti <= sp - plant.hysteresis_on_k:
            running = True
    else:
        running = False
    q = 0.0
    if running:
        mod = plant.rt_modulation_gain_k_per_k * (sp - ti)
        mod = max(-plant.rt_modulation_max_k, min(plant.rt_modulation_max_k, mod))
        lwt = max(lwt_set + mod, plant.min_lwt_c)
        mwt = lwt - plant.flow_return_dt_k / 2
        q = min(plant.q_max_w, plant.radiator.output_w(mwt, ti))
    return step(ti, to, q, step_h, building), running, q


def forecast(
    ti0_c: float,
    running0: bool,
    to_c: Sequence[float],
    setpoint_c: Sequence[float],
    lwt_set_c: Sequence[float],  # weather-curve LWT without RT modulation
    heating_enabled: bool,
    building: ThermalParams,
    plant: PlantParams,
    cop: Callable[[float], float],
    step_h: float = 0.25,
) -> Forecast:
    """Simulate ``len(to_c)`` steps. All sequences are per step."""
    ti = ti0_c
    running = running0 and heating_enabled
    out_t, out_q, out_e, out_r = [ti], [], [], []
    for to, sp, lwt_set in zip(to_c, setpoint_c, lwt_set_c, strict=True):
        ti, running, q = plant_step(ti, running, to, sp, lwt_set, heating_enabled, building, plant, step_h)
        out_t.append(ti)
        out_q.append(q * step_h)
        out_e.append(q * step_h / max(cop(to), 1.0) if q > 0 else 0.0)
        out_r.append(running)
    return Forecast(step_h, out_t, out_q, out_e, out_r)
