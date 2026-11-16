"""Radiator constant K: default refitted, learned online from steady space heating."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.daikin_mpc.core.engine import EngineConfig, ShadowEngine
from custom_components.daikin_mpc.core.radiator_learner import RadiatorLearner
from custom_components.daikin_mpc.core.telemetry import Role

from .test_live_core import snap

T0 = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
TRUE_K = 90.0


def _heating(t, k=TRUE_K, dhw=0.0, defrost=0.0, hz=40.0, flow=12.0):
    lwt, rwt, ti = 30.0, 26.0, 20.5
    q = k * ((lwt + rwt) / 2 - ti) ** 1.3
    return snap(
        t, lwt=lwt, rwt=rwt, ti=ti, hz=hz, flow=flow, heat_w=q, dhw_active=dhw, defrost=defrost, heartbeat=1.0
    )


def test_default_radiator_constant_is_the_refitted_value():
    assert EngineConfig().radiator_k == pytest.approx(84.4)


def test_learner_median_is_robust_and_needs_enough_samples():
    rl = RadiatorLearner()
    for _ in range(rl.min_samples - 1):
        assert rl.add(90.0 * 7.5**1.3, 28.0, 20.5)
    assert rl.estimate() is None
    rl.add(90.0 * 7.5**1.3, 28.0, 20.5)
    for _ in range(5):  # odd samples do not move the median
        rl.add(10 * 90.0 * 7.5**1.3 / 10 * 2, 28.0, 20.5)
    assert rl.estimate() == pytest.approx(90.0)
    assert not rl.add(500.0, 22.0, 20.5)  # MWT − Ti too small


def test_learner_round_trip_and_window():
    rl = RadiatorLearner(window=10, min_samples=3)
    for i in range(15):
        rl.add((80 + i) * 8**1.3, 28.5, 20.5)
    assert len(rl.samples) == 10
    back = RadiatorLearner(window=10, min_samples=3)
    assert back.load_dict(json.loads(json.dumps(rl.to_dict())))
    assert back.estimate() == pytest.approx(rl.estimate())
    assert not RadiatorLearner(n=1.2).load_dict(rl.to_dict())  # other exponent: not comparable


def _run(eng, start, minutes, **kw):
    t = start
    for _ in range(minutes // 5):
        eng.process(_heating(t, **kw), None, {}, None)
        t += timedelta(minutes=5)
    return t


def test_engine_learns_k_from_steady_space_heating_only():
    eng = ShadowEngine(EngineConfig())
    t = _run(eng, T0, 8 * 60, dhw=1.0)  # hot water: ignored
    t = _run(eng, t, 8 * 60, defrost=1.0)  # defrost: ignored
    assert eng.radiator_learner.samples == []
    _run(eng, t, 10 * 60)  # steady space heating
    assert eng.radiator_learner.estimate() == pytest.approx(TRUE_K, rel=1e-3)
    assert eng.plant.radiator.k_w_per_kn == pytest.approx(TRUE_K, rel=1e-3)


def test_engine_skips_the_start_of_a_run():
    eng = ShadowEngine(EngineConfig())
    t = T0
    for _ in range(20):  # 15-minute runs with pauses: never 20 min continuous
        t = _run(eng, t, 15)
        t = _run(eng, t, 15, hz=0.0)
    assert eng.radiator_learner.samples == []


def test_engine_restores_learned_k_from_saved_state():
    eng = ShadowEngine(EngineConfig())
    _run(eng, T0, 10 * 60)
    state = json.loads(json.dumps(eng.to_dict()))
    fresh = ShadowEngine(EngineConfig())
    assert fresh.load_dict(state) == []
    assert fresh.plant.radiator.k_w_per_kn == pytest.approx(TRUE_K, rel=1e-3)
    legacy = {k: v for k, v in state.items() if k != "radiator"}
    old = ShadowEngine(EngineConfig())
    assert old.load_dict(legacy) == []
    assert old.plant.radiator.k_w_per_kn == pytest.approx(84.4)


def test_learning_disabled_keeps_the_prior():
    eng = ShadowEngine(EngineConfig(learning_enabled=False))
    _run(eng, T0, 10 * 60)
    assert eng.radiator_learner.samples == []
    assert eng.plant.radiator.k_w_per_kn == pytest.approx(84.4)
    assert Role.HEAT_W  # mapped role used by the learner
