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


def test_reconciliation_counts_only_continuous_space_heating_hours():
    """Codex review PR #1 round 3: hours with DHW counter activity (flag missing), backup heat or no
    continuous heating must not enter the heat-counter / heat-power ratio."""
    from custom_components.daikin_mpc.core.accumulator import HourRecord

    eng = ShadowEngine(EngineConfig())

    def hour(cls, deltas, heat_w=1000.0, dhw=False):
        h = HourRecord(
            T0, 1.0, {"ti": 20.5, "to": 5.0, "heat_w": heat_w}, 30, 40, 12, deltas, False, dhw, cls
        )
        h.mean_cover = {"ti": 1.0, "to": 1.0, "heat_w": 1.0}
        return h

    eng._reconcile_heat(hour("heating_full", {"heat_kwh": 1.0, "dhw_heat_kwh": 0.0}))
    eng._reconcile_heat(hour("heating_full", {"heat_kwh": 1.0, "dhw_heat_kwh": 0.6}))  # DHW flag missing
    eng._reconcile_heat(hour("heating_full", {"heat_kwh": 1.0, "buh_kwh": 0.5}))  # backup heater
    eng._reconcile_heat(hour("heating_partial", {"heat_kwh": 0.4}))  # not continuous
    eng._reconcile_heat(hour("off", {"heat_kwh": 0.0}, heat_w=-10.0))
    assert eng._heat_counter_kwh == pytest.approx(1.0)
    assert eng._heat_power_kwh == pytest.approx(1.0)


def test_reconciliation_needs_heat_power_covering_the_counter_hour():
    """Codex review PR #1 round 4: the counter delta covers the whole hour; a heat-power mean from only
    part of it (sensor outage) must not enter the ratio."""
    from custom_components.daikin_mpc.core.accumulator import HourRecord

    eng = ShadowEngine(EngineConfig())
    means = {"ti": 20.5, "to": 5.0, "heat_w": 1000.0}
    part = HourRecord(T0, 1.0, means, 30, 40, 12, {"heat_kwh": 1.0}, False, False, "heating_full")
    part.mean_cover = {"heat_w": 0.4}
    eng._reconcile_heat(part)
    assert eng._heat_counter_kwh == 0.0
    full = HourRecord(T0, 1.0, means, 30, 40, 12, {"heat_kwh": 1.0}, False, False, "heating_full")
    full.mean_cover = {"heat_w": 0.95}
    eng._reconcile_heat(full)
    assert eng._heat_counter_kwh == pytest.approx(1.0)
    back = HourRecord.from_dict(json.loads(json.dumps(full.to_dict())))
    assert back.mean_cover == full.mean_cover


def test_hour_records_report_per_mean_coverage():
    from custom_components.daikin_mpc.core.accumulator import HourAccumulator

    acc = HourAccumulator()
    out = []
    for i in range(14):  # heat_w only in the first half of the hour
        t = T0 + timedelta(minutes=5 * i)
        vals = {"heat_w": 1000.0} if i < 6 else {}
        out += acc.add(snap(t, **vals))
    h = out[0]
    assert h.mean_cover["ti"] == pytest.approx(h.coverage, abs=0.01)
    assert h.mean_cover["heat_w"] < 0.6


def test_whole_kwh_counter_plateaus_do_not_bias_the_ratio():
    """Codex review PR #1 round 5: at 0.8 kW a whole-kWh counter steps in only some hours; dropping the
    zero-step hours inflated the ratio to ~1.25 with an unbiased sensor."""
    from custom_components.daikin_mpc.core.accumulator import HourRecord

    eng = ShadowEngine(EngineConfig())
    true_kwh = 0.0
    for i in range(40):  # 40 h at 0.8 kW
        before = int(true_kwh)
        true_kwh += 0.8
        h = HourRecord(
            T0 + timedelta(hours=i),
            1.0,
            {"ti": 20.5, "to": 5.0, "heat_w": 800.0},
            30,
            40,
            12,
            {"heat_kwh": float(int(true_kwh) - before)},
            False,
            False,
            "heating_full",
        )
        h.mean_cover = {"heat_w": 1.0}
        eng._reconcile_heat(h)
    assert eng.heat_ratio() == pytest.approx(1.0, abs=0.04)


def test_calibration_is_forgotten_when_its_sources_change():
    """Codex review PR #1 round 5: remapping the heat-power sensor or heat counter resets the heat ratio
    and the radiator samples learned from the old source."""
    from custom_components.daikin_mpc.core.engine import forget_recalibrated

    eng = ShadowEngine(EngineConfig())
    _run(eng, T0, 30 * 60)
    state = {**eng.to_dict(), "calibration_sources": {"heat_w": "sensor.raw", "heat_kwh": "sensor.c"}}
    same = forget_recalibrated(dict(state), {"heat_w": "sensor.raw", "heat_kwh": "sensor.c"})
    assert "radiator" in same and "heat_reconcile_kwh" in same
    changed = forget_recalibrated(dict(state), {"heat_w": "sensor.corrected", "heat_kwh": "sensor.c"})
    assert "radiator" not in changed and "heat_reconcile_kwh" not in changed
    legacy = {k: v for k, v in state.items() if k != "calibration_sources"}  # saved before sources existed
    assert "radiator" not in forget_recalibrated(legacy, {"heat_w": "sensor.raw", "heat_kwh": "sensor.c"})
    fresh = ShadowEngine(EngineConfig())
    assert fresh.load_dict(changed) == []
    assert fresh.heat_ratio() is None and fresh.radiator_learner.samples == []


def test_late_counter_share_is_added_to_the_reconciled_hour():
    """Codex review PR #1 round 6: a pre-boundary share booked late (stale first sample of the next hour)
    must reach the reconciliation of the hour it belongs to."""
    from custom_components.daikin_mpc.core.accumulator import HourRecord

    eng = ShadowEngine(EngineConfig())

    def hour(i, dq, before=None):
        h = HourRecord(
            T0 + timedelta(hours=i),
            1.0,
            {"ti": 20.5, "to": 5.0, "heat_w": 1000.0},
            30,
            40,
            12,
            {"heat_kwh": dq},
            False,
            False,
            "heating_full",
        )
        h.mean_cover = {"heat_w": 1.0}
        h.deltas_before_kwh = before or {}
        return h

    eng._on_hour(hour(0, 0.8))  # its last 0.2 kWh arrives late, with the next hour
    eng._on_hour(hour(1, 0.8, before={"heat_kwh": 0.2}))
    assert eng._heat_counter_kwh == pytest.approx(1.8)


def test_weather_curve_is_forgotten_when_its_inputs_change():
    from custom_components.daikin_mpc.core.engine import forget_recalibrated

    srcs = {"heat_w": "a", "heat_kwh": "b"}
    wc_old = {"to": "sensor.daikin_outside", "ti": "t", "room_set": "s", "lwt_set": "l"}
    state = {
        "wc_learner": {"version": 1},
        "lwt_actual_offset_k": 2.0,
        "radiator": {},
        "heat_reconcile_kwh": [1, 1],
        "calibration_sources": srcs,
        "wc_sources": wc_old,
    }
    kept = forget_recalibrated(dict(state), srcs, dict(wc_old))
    assert "wc_learner" in kept and "radiator" in kept
    moved = forget_recalibrated(dict(state), srcs, {**wc_old, "to": "sensor.ecowitt_outdoor_temperature"})
    assert "wc_learner" not in moved and "radiator" in moved
