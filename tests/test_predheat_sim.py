import numpy as np
import pytest

from custom_components.daikin_mpc.core.predheat_sim import PredheatConfig, PredheatSim, fill_table

CFG = PredheatConfig(
    heat_loss_watts=94,
    heat_loss_degrees=0.03,
    heat_gain_static=440,
    heat_output=10630,
    heat_volume=121,
    heat_max_power=4000,
    heat_min_power=700,
    heat_cop=3.8,
    flow_difference_target=5,
    hysteresis=1.0,
    hysteresis_off=1.0,
    weather_compensation={-40: 47.0, -23: 47.0, 10: 25.0, 20: 25.0},
)
N = 288


def run(cfg=CFG, ext=5.0, target=21.0, ti0=20.0):
    return PredheatSim(cfg).run(ti0, ti0, True, np.full(N, ext), np.full(N, target))


def test_fill_table_interpolates():
    t = fill_table({0: 0.0, 10: 1.0})
    assert t[5] == pytest.approx(0.5) and len(t) == 11


def test_no_energy_when_warm_and_above_target():
    r = PredheatSim(CFG).run(23.0, 23.0, False, np.full(N, 18.0), np.full(N, 20.0))
    assert r.electric_wh.sum() == 0.0


def test_colder_weather_and_higher_loss_use_more_energy():
    from dataclasses import replace

    assert run(ext=0.0).electric_wh.sum() > run(ext=8.0).electric_wh.sum()
    assert run(replace(CFG, heat_loss_watts=150)).electric_wh.sum() > run().electric_wh.sum()


def test_user_efficiency_table_overrides_defaults():
    from dataclasses import replace

    cfg = replace(CFG, heat_pump_efficiency={t: 3.0 for t in range(-20, 21, 2)})
    assert PredheatSim(cfg).eff_max == pytest.approx(3.0)
