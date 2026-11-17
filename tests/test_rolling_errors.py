"""0.7.1: prediction-error sensors show a rolling (~7-day) error; the all-time average stays an attribute.

The all-time average over ~900 predictions moved by ~0.1 % per new prediction and stayed dominated by
the pre-0.6.6 model, so model fixes did not show for weeks.
"""

import json

import pytest

from custom_components.daikin_mpc.core.engine import EngineConfig, ErrorStats, ShadowEngine


def test_recent_error_follows_new_predictions_quickly():
    e = ErrorStats()
    for _ in range(900):
        e.add(-0.5)  # old model: biased
    for _ in range(168):
        e.add(0.1)  # improved model, a week of hourly predictions
    assert e.mae == pytest.approx((900 * 0.5 + 168 * 0.1) / 1068)  # all-time barely moves (0.45)
    assert e.mae_recent is not None and e.mae_recent < 0.3  # rolling shows most of the change
    assert e.bias_recent is not None and e.bias_recent > e.bias


def test_recent_error_needs_a_few_predictions():
    e = ErrorStats()
    for _ in range(5):
        e.add(0.2)
    assert e.mae_recent is None
    e.add(0.2)
    assert e.mae_recent == pytest.approx(0.2)


def test_saved_errors_round_trip_and_legacy_three_values():
    eng = ShadowEngine(EngineConfig())
    eng.errors["mpc_3h"] = ErrorStats()
    for _ in range(20):
        eng.errors["mpc_3h"].add(0.3)
    state = json.loads(json.dumps(eng.to_dict()))
    fresh = ShadowEngine(EngineConfig())
    fresh.load_dict(state)
    assert fresh.errors["mpc_3h"].mae_recent == pytest.approx(0.3)
    state["errors"]["mpc_3h"] = [900, 320.0, -165.0]  # saved by <= 0.7.0: no rolling part
    old = ShadowEngine(EngineConfig())
    old.load_dict(state)
    assert old.errors["mpc_3h"].mae == pytest.approx(320 / 900)
    assert old.errors["mpc_3h"].mae_recent is None  # rolling starts fresh with the current model
