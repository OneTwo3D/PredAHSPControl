import numpy as np
import pytest

from custom_components.daikin_mpc.core.heatpump_model import _pava, fit_cop_curve, min_output_w


def test_pava_monotone():
    out = _pava(np.array([3.0, 2.5, 3.5, 3.2, 4.0]), np.ones(5))
    assert np.all(np.diff(out) >= -1e-12)


def test_cop_curve_monotone_and_ratio_of_sums():
    rng = np.random.default_rng(4)
    to = rng.uniform(-3, 15, 300)
    elec = rng.uniform(2, 10, 300)
    cop_true = 2.8 + 0.08 * to
    heat = elec * cop_true * rng.normal(1, 0.08, 300)
    c = fit_cop_curve(to, heat, elec, np.array([-5, 0, 5, 10, 16.0]))
    assert all(b >= a for a, b in zip(c.cop, c.cop[1:], strict=False))
    v, extrap = c.at(5.0)
    assert v == pytest.approx(2.8 + 0.4, abs=0.25) and not extrap
    assert c.at(-15.0)[1] is True


def test_min_output_requires_samples():
    with pytest.raises(ValueError):
        min_output_w(np.ones(5))
    assert min_output_w(np.linspace(600, 1600, 101), 0.05) == pytest.approx(650.0)
