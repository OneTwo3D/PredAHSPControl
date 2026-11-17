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


def _heating(t, k=TRUE_K, dhw=0.0, defrost=0.0, hz=40.0, flow=12.0, heat_kwh=100.0):
    lwt, rwt, ti = 30.0, 26.0, 20.5
    q = k * ((lwt + rwt) / 2 - ti) ** 1.3
    return snap(
        t,
        lwt=lwt,
        rwt=rwt,
        ti=ti,
        hz=hz,
        flow=flow,
        heat_w=q,
        dhw_active=dhw,
        defrost=defrost,
        heartbeat=1.0,
        heat_kwh=heat_kwh,
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


_COUNTER = {"kwh": 100.0}


def _run(eng, start, minutes, **kw):
    """Feed heating samples; the heat counter advances with the (true) heat, in whole kWh like Daikin's."""
    t = start
    for _ in range(minutes // 5):
        s = _heating(t, heat_kwh=float(int(_COUNTER["kwh"])), **kw)
        if (kw.get("hz", 40.0) or 0) > 0 and not kw.get("dhw"):
            _COUNTER["kwh"] += (s.get(Role.HEAT_W) or 0.0) * 5 / 60 / 1000
        eng.process(s, None, {}, None)
        t += timedelta(minutes=5)
    return t


def test_engine_learns_k_from_steady_space_heating_only():
    eng = ShadowEngine(EngineConfig())
    t = _run(eng, T0, 8 * 60, dhw=1.0)  # hot water: ignored
    t = _run(eng, t, 8 * 60, defrost=1.0)  # defrost: ignored
    assert eng.radiator_learner.samples == []
    _run(eng, t, 30 * 60)  # steady space heating (the first ~16 h reconcile the heat sensor)
    # within the whole-kWh resolution of the heat counter used to reconcile the sensor
    assert eng.radiator_learner.estimate() == pytest.approx(TRUE_K, rel=0.03)
    assert eng.plant.radiator.k_w_per_kn == pytest.approx(eng.radiator_learner.estimate())


def test_engine_skips_the_start_of_a_run():
    eng = ShadowEngine(EngineConfig())
    t = T0
    for _ in range(20):  # 15-minute runs with pauses: never 20 min continuous
        t = _run(eng, t, 15)
        t = _run(eng, t, 15, hz=0.0)
    assert eng.radiator_learner.samples == []


def test_engine_restores_learned_k_from_saved_state():
    eng = ShadowEngine(EngineConfig())
    _run(eng, T0, 30 * 60)
    state = json.loads(json.dumps(eng.to_dict()))
    fresh = ShadowEngine(EngineConfig())
    assert fresh.load_dict(state) == []
    assert fresh.plant.radiator.k_w_per_kn == pytest.approx(eng.plant.radiator.k_w_per_kn)
    assert fresh.heat_ratio() == pytest.approx(eng.heat_ratio())
    legacy = {k: v for k, v in state.items() if k != "radiator"}
    old = ShadowEngine(EngineConfig())
    assert old.load_dict(legacy) == []
    assert old.plant.radiator.k_w_per_kn == pytest.approx(84.4)


def test_learning_disabled_keeps_the_prior():
    eng = ShadowEngine(EngineConfig(learning_enabled=False))
    _run(eng, T0, 30 * 60)
    assert eng.radiator_learner.samples == []
    assert eng.plant.radiator.k_w_per_kn == pytest.approx(84.4)
    assert Role.HEAT_W  # mapped role used by the learner


def test_biased_heat_sensor_is_reconciled_with_the_heat_counter():
    """Codex review PR #1: the default HEAT_W (raw bridge flow x dT) reads ~1/3 low. K is learned only
    once the sensor is reconciled with the heat counter, and from the reconciled heat."""
    eng = ShadowEngine(EngineConfig())
    t, heat = T0, 1000.0
    for _ in range(12 * 30):  # 30 h of steady heating, counter steps in whole kWh like Daikin's
        s = _heating(t)
        q_true = s.get(Role.HEAT_W)
        heat += q_true * 5 / 60 / 1000
        biased = snap(
            t,
            lwt=30.0,
            rwt=26.0,
            ti=20.5,
            hz=40.0,
            flow=12.0,
            heat_w=q_true * 0.66,
            heat_kwh=float(int(heat)),
            heartbeat=1.0,
        )
        eng.process(biased, None, {}, None)
        t += timedelta(minutes=5)
    assert eng.heat_ratio() == pytest.approx(1 / 0.66, rel=0.05)
    assert eng.radiator_learner.estimate() == pytest.approx(TRUE_K, rel=0.05)


def test_k_is_not_learned_before_the_sensor_is_reconciled():
    eng = ShadowEngine(EngineConfig())
    _run(eng, T0, 3 * 60)  # 3 h: far less than 20 kWh compared
    assert eng.heat_ratio() is None
    assert eng.radiator_learner.samples == []
    assert eng.plant.radiator.k_w_per_kn == pytest.approx(84.4)
