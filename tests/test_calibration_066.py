"""0.6.6 calibration: thermostat band, RT modulation and weather curve match the unit (Nov 2026 data).

- Zone valve switches 6-16 Nov: heating starts at setpoint − 0.4 K, never stops below setpoint + 0.9 K.
- Regression over 6,403 space-heating minutes: LWT setpoint = 30.6 − 0.58·To + 0.655·(setpoint − Ti).
- The curve and modulation gain are learned online from the unit's LWT setpoint (WeatherCurveLearner).
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.daikin_mpc.core.emitter_model import RadiatorParams
from custom_components.daikin_mpc.core.engine import EngineConfig, ShadowEngine
from custom_components.daikin_mpc.core.predictor import PlantParams
from custom_components.daikin_mpc.core.wc_learner import WeatherCurveLearner

from .test_live_core import snap

T0 = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)


def test_plant_defaults_match_the_measured_unit():
    p = PlantParams(RadiatorParams(84.4))
    assert p.hysteresis_on_k == pytest.approx(0.4)
    assert p.hysteresis_off_k == pytest.approx(0.9)
    assert p.rt_modulation_gain_k_per_k == pytest.approx(0.65)


def test_default_weather_curve_is_the_measured_line():
    eng = ShadowEngine(EngineConfig())
    assert eng.wc_lwt(0.0) == pytest.approx(30.6, abs=0.05)
    assert eng.wc_lwt(8.0) == pytest.approx(30.6 - 0.58 * 8, abs=0.05)
    assert eng.wc_lwt(10.0) == pytest.approx(25.0)  # the unit's minimum LWT (min_lwt_c)


def test_learner_recovers_curve_and_gain():
    wl = WeatherCurveLearner()
    for i in range(2000):
        to = -2 + (i % 15)
        err = ((i * 7) % 11 - 5) / 5  # setpoint − Ti between −1 and +1
        wl.add(to, err, 32.0 - 0.5 * to + 0.8 * err)
    est = wl.estimate()
    assert est is not None
    a, b, g = est
    assert a == pytest.approx(32.0, abs=0.05)
    assert b == pytest.approx(-0.5, abs=0.01)
    assert g == pytest.approx(0.8, abs=0.02)


def test_learner_needs_samples_and_varied_outdoor_temperature():
    wl = WeatherCurveLearner()
    for _ in range(int(wl.min_samples) - 1):
        wl.add(5.0, 0.0, 27.0)
    assert wl.estimate() is None
    for _ in range(500):  # one outdoor temperature only: slope not identified -> keep defaults
        wl.add(5.0, 0.0, 27.0)
    assert wl.estimate() is None


def test_learner_round_trip():
    wl = WeatherCurveLearner()
    for i in range(400):
        wl.add(-2 + i % 12, ((i % 5) - 2) / 2, 31.0 - 0.6 * (-2 + i % 12) + 0.7 * ((i % 5) - 2) / 2)
    back = WeatherCurveLearner()
    assert back.load_dict(json.loads(json.dumps(wl.to_dict())))
    assert back.estimate() == pytest.approx(wl.estimate())


def _heating(t, to, sp, ti, lwt_set):
    return snap(t, to=to, ti=ti, room_set=sp, lwt_set=lwt_set, hz=40.0, heartbeat=1.0)


def test_engine_learns_curve_and_applies_gain_to_plant():
    eng = ShadowEngine(EngineConfig())
    t = T0
    for i in range(3 * 288):  # three days of 5-min heating samples
        to = 2 + (i % 24) / 3
        ti = 20.0 + ((i * 5) % 9 - 4) / 8
        sp = 20.5
        eng.process(_heating(t, to, sp, ti, 33.0 - 0.7 * to + 1.2 * (sp - ti)), None, {}, None)
        t += timedelta(minutes=5)
    assert eng.wc_lwt(0.0) == pytest.approx(33.0, abs=0.1)
    assert eng.wc_lwt(10.0) == pytest.approx(26.0, abs=0.1)
    assert eng.plant.rt_modulation_gain_k_per_k == pytest.approx(1.2, abs=0.05)
    state = json.loads(json.dumps(eng.to_dict()))
    fresh = ShadowEngine(EngineConfig())
    assert fresh.load_dict(state) == []
    assert fresh.plant.rt_modulation_gain_k_per_k == pytest.approx(1.2, abs=0.05)


def test_old_contaminated_offset_is_not_used():
    eng = ShadowEngine(EngineConfig())
    state = json.loads(json.dumps(eng.to_dict()))
    state.pop("lwt_actual_offset_k")
    state["lwt_offset_k"] = -1.37  # as saved by <= 0.6.5
    fresh = ShadowEngine(EngineConfig())
    fresh.load_dict(state)
    assert fresh.wc_lwt(0.0) == pytest.approx(30.6, abs=0.05)
    assert fresh.lwt_offset_k == pytest.approx(1.8)  # the prior, not the contaminated value


def test_actual_lwt_offset_default_and_learning():
    """Steady space heating 10-16 Nov: actual LWT ran 1.8 K above the unit's setpoint (compressor at its
    minimum speed). The forecast adds a learned offset; the pre-0.6.6 offset key is ignored."""
    eng = ShadowEngine(EngineConfig())
    assert eng.lwt_offset_k == pytest.approx(1.8)
    t = T0
    for _ in range(12 * 12):  # 12 h of steady heating, actual 3 K above setpoint
        eng.process(snap(t, hz=10.0, lwt_set=27.0, lwt=30.0, heartbeat=1.0), None, {}, None)
        t += timedelta(minutes=5)
    assert eng.lwt_offset_k == pytest.approx(3.0, abs=0.1)
    state = json.loads(json.dumps(eng.to_dict()))
    fresh = ShadowEngine(EngineConfig())
    fresh.load_dict(state)
    assert fresh.lwt_offset_k == pytest.approx(eng.lwt_offset_k)
    state.pop("lwt_actual_offset_k")
    old = ShadowEngine(EngineConfig())
    old.load_dict(state)
    assert old.lwt_offset_k == pytest.approx(1.8)


def test_minimum_lwt_readings_do_not_flatten_the_learned_curve():
    """Codex review PR #1: setpoints held at the unit's 25 °C minimum are censored (the curve asks for
    less); fitting them as data flattened the slope and under-predicted cold-weather LWT."""
    eng = ShadowEngine(EngineConfig())
    t = T0
    for i in range(4 * 288):  # four mild days: the unit sits at its 25 °C floor
        to = 10.0 + (i % 60) / 10  # 10-16 °C
        eng.process(_heating(t, to, 20.5, 20.3, 25.0), None, {}, None)
        t += timedelta(minutes=5)
    assert eng.wc_learner.estimate() is None  # nothing uncensored learned
    assert eng.wc_lwt(-5.0) == pytest.approx(30.6 + 0.58 * 5, abs=0.05)  # configured curve kept
    for i in range(2 * 288):  # then real, unclamped readings in colder weather
        to = 2.0 + (i % 50) / 10  # 2-7 °C
        eng.process(_heating(t, to, 20.5, 20.3, 30.6 - 0.58 * to + 0.65 * 0.2), None, {}, None)
        t += timedelta(minutes=5)
    assert eng.wc_lwt(-5.0) == pytest.approx(30.6 + 0.58 * 5, abs=0.3)


def test_actual_lwt_offset_is_added_after_the_minimum_clamp():
    """Codex review PR #1 round 3: the unit clamps its own setpoint (curve + RT modulation) at the
    minimum; the actual water then runs the measured offset above it."""
    from custom_components.daikin_mpc.core.predictor import plant_step
    from custom_components.daikin_mpc.core.thermal_model import ThermalParams

    p = PlantParams(RadiatorParams(84.4), lwt_actual_offset_k=1.8)
    b = ThermalParams(75.7, 5000.0, 111.0)
    # curve at its 25 °C floor, room 0.5 K above setpoint (negative modulation), heating running
    _, _, q = plant_step(21.5, True, 12.0, 21.0, 25.0, True, b, p, 0.25)
    expected_mwt = 25.0 + 1.8 - p.flow_return_dt_k / 2
    assert q == pytest.approx(84.4 * (expected_mwt - 21.5) ** 1.3)


def test_minimum_clamp_applies_after_rt_modulation():
    """Codex review PR #1 round 4: in mild weather the base curve is below 25 °C; the unit clamps
    curve + modulation, so the forecast must get the unclamped curve value."""
    eng = ShadowEngine(EngineConfig())
    assert eng.wc_lwt(15.0) == pytest.approx(25.0)  # displayed value: clamped
    assert eng.wc_lwt(15.0, clamp=False) == pytest.approx(30.6 - 0.58 * 15, abs=0.05)  # 21.9
