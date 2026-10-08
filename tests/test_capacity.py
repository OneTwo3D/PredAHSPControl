"""Thermal capacity from free-cooling nights."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.daikin_mpc.core.accumulator import HourRecord
from custom_components.daikin_mpc.core.capacity_learner import CapacityLearner
from custom_components.daikin_mpc.core.engine import EngineConfig, ShadowEngine

UA, C, G = 94.0, 6500.0, 300.0  # "true" house


def _hour(t, ti, to, *, hz=0.0, dhw=False, cls="off", heat=0.0):
    return HourRecord(
        t, 1.0, {"ti": ti, "to": to}, hz, hz, 0.0, {"heat_kwh": heat, "dhw_heat_kwh": 0.0}, False, dhw, cls
    )


def _nights(n, start=datetime(2026, 11, 1, 18, 0, tzinfo=UTC), ti0=21.5, to_night=(2.0, 12.0), **kw):
    """Hour records of n evenings/nights cooling freely with the 'true' RC parameters."""
    out = []
    for d in range(n):
        to = to_night[0] + (to_night[1] - to_night[0]) * ((d * 7) % 10) / 9  # varied, deterministic
        ti = ti0
        t = start + timedelta(days=d)
        for k in range(13):  # 18:00 .. 06:00
            out.append(_hour(t + timedelta(hours=k), ti, to, **kw))
            ti += (G - UA * (ti - to)) / C
    return out


def _feed(cl, hours):
    used = 0
    for i in range(2, len(hours)):
        used += cl.add(hours[i - 2], hours[i - 1], hours[i])
    return used


def test_recovers_capacity_and_gains_from_clean_nights():
    cl = CapacityLearner()
    used = _feed(cl, _nights(12))
    assert used >= 30
    est = cl.estimate(UA)
    assert est is not None
    assert est.c_wh_per_k == pytest.approx(C, rel=0.05)
    assert est.tau_h == pytest.approx(C / UA, rel=0.05)
    assert est.gains_night_w == pytest.approx(G, abs=40)


def test_only_night_hours_are_used():
    cl = CapacityLearner()
    hours = _nights(12)
    assert all(h.start.hour in range(18, 24) or h.start.hour <= 6 for h in hours)
    used_pairs = [h for i, h in enumerate(hours[1:-1], 1) if cl.clean_pair(hours[i - 1], h, hours[i + 1])]
    assert used_pairs and all(h.start.hour in (22, 23, 0, 1, 2, 3, 4) for h in used_pairs)


@pytest.mark.parametrize(
    "kw", [{"hz": 30.0, "cls": "heating_full"}, {"dhw": True, "cls": "dhw"}, {"heat": 0.5}]
)
def test_heating_or_hot_water_hours_are_rejected(kw):
    cl = CapacityLearner()
    assert _feed(cl, _nights(12, **kw)) == 0 and cl.estimate(UA) is None


def test_compressor_must_be_off_in_the_hour_before():
    hours = _nights(1)
    i = next(i for i, h in enumerate(hours) if h.start.hour == 22)
    running = _hour(
        hours[i - 1].start, hours[i - 1].means["ti"], hours[i - 1].means["to"], hz=30.0, cls="heating_full"
    )
    assert CapacityLearner.clean_pair(hours[i - 1], hours[i], hours[i + 1]) is not None
    assert CapacityLearner.clean_pair(running, hours[i], hours[i + 1]) is None


def test_mild_nights_and_too_few_nights_give_no_estimate():
    cl = CapacityLearner()
    _feed(cl, _nights(12, to_night=(17.0, 19.0)))  # Ti − To below 6 K
    assert cl.n == 0 and cl.estimate(UA) is None
    cl = CapacityLearner()
    _feed(cl, _nights(3))
    assert cl.nights == 3 and cl.estimate(UA) is None  # needs four nights


def test_no_spread_gives_no_estimate():
    cl = CapacityLearner()
    _feed(cl, _nights(12, to_night=(5.0, 5.0)))  # same outdoor temperature every night
    assert cl.estimate(UA) is None


def test_old_data_fades_with_time():
    cl = CapacityLearner()
    _feed(cl, _nights(12))
    n0 = cl.n
    _feed(cl, _nights(1, start=datetime(2027, 2, 1, 18, tzinfo=UTC)))  # ~80 days later
    assert cl.n < 0.3 * n0 + 10


def test_persistence_round_trip_and_rejection():
    cl = CapacityLearner()
    _feed(cl, _nights(12))
    state = json.loads(json.dumps(cl.to_dict()))
    other = CapacityLearner()
    assert other.load_dict(state) and other.estimate(UA) == cl.estimate(UA)
    bad = dict(state, sums=[10.0, 100.0, 0.0, 1.0, 0.0, 0.0])  # sxx·n < sx²: impossible
    assert not CapacityLearner().load_dict(bad)


def test_engine_adopts_the_night_estimate():
    eng = ShadowEngine(EngineConfig())
    assert eng.learner.params.c_wh_per_k == pytest.approx(5500.0)
    for h in _nights(12):
        eng._on_hour(h)
    assert eng.capacity_estimate is not None
    assert eng.learner.params.c_wh_per_k == pytest.approx(C, rel=0.05)
    assert eng.learner.params.ua_w_per_k == pytest.approx(94.0)  # UA and gains untouched
    state = json.loads(json.dumps(eng.to_dict()))
    other = ShadowEngine(EngineConfig())
    assert other.load_dict(state) == []
    assert other.learner.params.c_wh_per_k == pytest.approx(eng.learner.params.c_wh_per_k)


def test_learning_disabled_leaves_capacity_alone():
    eng = ShadowEngine(EngineConfig(learning_enabled=False))
    for h in _nights(12):
        eng._on_hour(h)
    assert eng.capacity.n == 0 and eng.learner.params.c_wh_per_k == pytest.approx(5500.0)


def test_changed_priors_apply_while_nothing_is_learned():
    old = ShadowEngine(EngineConfig(c_wh_per_k=3000.0))
    state = json.loads(json.dumps(old.to_dict()))
    new = ShadowEngine(EngineConfig(c_wh_per_k=5500.0))
    new.load_dict(state)
    assert new.learner.params.c_wh_per_k == pytest.approx(5500.0)  # stored 3000 with 0 updates ignored
