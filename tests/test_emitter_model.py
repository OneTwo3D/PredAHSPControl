import numpy as np
import pytest

from custom_components.daikin_mpc.core.emitter_model import RadiatorParams, fit, hydronic_heat_w


def test_required_mwt_inverts_output():
    p = RadiatorParams(63.0, 1.3)
    mwt = p.required_mwt_c(1000.0, 20.0)
    assert p.output_w(mwt, 20.0) == pytest.approx(1000.0)
    assert p.required_mwt_c(0.0, 20.0) == 20.0


def test_fit_recovers_k_and_n():
    rng = np.random.default_rng(3)
    ti = 20.0 + rng.normal(0, 0.3, 500)
    mwt = ti + rng.uniform(4, 15, 500)
    q = 60.0 * (mwt - ti) ** 1.35 * rng.normal(1, 0.03, 500)
    f = fit(q, mwt, ti, n=1.35)
    assert f.params.k_w_per_kn == pytest.approx(60.0, rel=0.03)
    assert f.n_free == pytest.approx(1.35, abs=0.05)


def test_hydronic_heat_units():
    # 6 L/min, 5 K drop -> 0.1 kg/s * 4180 * 5 = 2090 W
    assert hydronic_heat_w(np.array([6.0]), np.array([35.0]), np.array([30.0]))[0] == pytest.approx(2090.0)
