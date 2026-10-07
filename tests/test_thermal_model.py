import math

import numpy as np
import pytest

from custom_components.daikin_mpc.core.thermal_model import (
    ThermalParams,
    fit_daily_energy_balance,
    simulate,
    step,
)

P = ThermalParams(ua_w_per_k=100.0, c_wh_per_k=3000.0, gains_w=400.0)


def test_equilibrium_is_fixed_point():
    ti_eq = 5.0 + (1000.0 + 400.0) / 100.0
    assert step(ti_eq, 5.0, 1000.0, 3.0, P) == pytest.approx(ti_eq)


def test_colder_outside_cools_and_heat_warms():
    assert step(20.0, 0.0, 0.0, 1.0, P) < step(20.0, 10.0, 0.0, 1.0, P)
    assert step(20.0, 5.0, 2000.0, 1.0, P) > step(20.0, 5.0, 0.0, 1.0, P)


def test_higher_ua_more_loss_and_higher_c_slower():
    hi_ua = ThermalParams(200.0, 3000.0, 400.0)
    hi_c = ThermalParams(100.0, 9000.0, 400.0)
    base = step(20.0, 0.0, 0.0, 1.0, P)
    assert step(20.0, 0.0, 0.0, 1.0, hi_ua) < base
    assert abs(step(20.0, 0.0, 0.0, 1.0, hi_c) - 20.0) < abs(base - 20.0)


def test_step_independent_of_subdivision():
    one = step(20.0, 0.0, 500.0, 2.0, P)
    many = simulate(20.0, np.zeros(8), np.full(8, 500.0), 0.25, P)[-1]
    assert one == pytest.approx(many, abs=1e-9)


def test_units_first_order_response():
    # Free cooling for one time constant closes 63.2 % of the gap to equilibrium.
    tau = P.time_constant_h
    ti_eq = 0.0 + 400.0 / 100.0
    assert step(20.0, 0.0, 0.0, tau, P) == pytest.approx(ti_eq + (20.0 - ti_eq) * math.exp(-1))


def test_invalid_params_rejected():
    with pytest.raises(ValueError):
        ThermalParams(-1.0, 3000.0, 0.0)


def test_serialize_roundtrip():
    assert ThermalParams.deserialize(P.serialize()) == P


def _synthetic_days(n, rng, excite=True):
    to = rng.uniform(-2, 14, n) if excite else np.full(n, 8.0) + rng.normal(0, 0.05, n)
    ti = 20.5 + rng.normal(0, 0.3, n)
    dti = np.r_[np.diff(ti), 0.0]
    q = P.ua_w_per_k * (ti - to) - P.gains_w + P.c_wh_per_k * dti / 24
    heat_kwh = (q + rng.normal(0, 60, n)) * 24 / 1000
    return ti, to, heat_kwh, dti


def test_fit_recovers_identifiable_parameters():
    rng = np.random.default_rng(1)
    f = fit_daily_energy_balance(*_synthetic_days(150, rng))
    assert f.params.ua_w_per_k == pytest.approx(100, rel=0.05)
    assert f.params.gains_w == pytest.approx(400, abs=60)
    assert abs(f.params.ua_w_per_k - 100) < 3 * f.ua_se + 1


def test_poorly_excited_data_has_large_uncertainty():
    rng = np.random.default_rng(2)
    good = fit_daily_energy_balance(*_synthetic_days(150, rng))
    poor = fit_daily_energy_balance(*_synthetic_days(150, rng, excite=False))
    assert poor.ua_se > 5 * good.ua_se
