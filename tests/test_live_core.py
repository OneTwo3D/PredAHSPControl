from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from custom_components.daikin_mpc.core.accumulator import DayAggregator, HourAccumulator
from custom_components.daikin_mpc.core.emitter_model import RadiatorParams
from custom_components.daikin_mpc.core.engine import EngineConfig, ShadowEngine
from custom_components.daikin_mpc.core.learner import BuildingLearner
from custom_components.daikin_mpc.core.predictor import PlantParams, forecast
from custom_components.daikin_mpc.core.telemetry import Reading, Role, Snapshot, validate
from custom_components.daikin_mpc.core.thermal_model import ThermalParams

T0 = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)


def snap(t, **vals):
    base = {
        Role.TI: 20.5,
        Role.TO: 5.0,
        Role.LWT: 30.0,
        Role.RWT: 28.0,
        Role.HZ: 30.0,
        Role.FLOW: 7.0,
        Role.HEAT_KWH: 100.0,
        Role.ELEC_KWH: 30.0,
        Role.DHW_HEAT_KWH: 10.0,
        Role.ROOM_SET: 21.0,
        Role.LWT_SET: 29.0,
        Role.HEATING_ENABLED: 1.0,
    }
    base.update({Role(k): v for k, v in vals.items()})
    return validate(Snapshot(t, {r: Reading(v) for r, v in base.items()}))


# --- telemetry ---------------------------------------------------------------------------
def test_heartbeat_and_change_only_publishing():
    # unchanged counters are not stale on their own; a stale heartbeat makes the snapshot incomplete
    old = {r: Reading(1.0, age_s=86400) for r in (Role.HEAT_KWH, Role.ELEC_KWH, Role.HZ)}
    ok = {Role.TI: Reading(20.0), Role.TO: Reading(5.0), Role.LWT: Reading(30.0), Role.RWT: Reading(28.0)}
    good = validate(Snapshot(T0, {**ok, **old, Role.HEARTBEAT: Reading(1.0, 60)}))
    assert good.complete and not good.issues
    bad = validate(Snapshot(T0, {**ok, **old, Role.HEARTBEAT: Reading(1.0, 3600)}))
    assert not bad.complete and "stale" in bad.issues[Role.HEARTBEAT]


def test_validation_flags_sentinel_stale_and_missing():
    s = validate(
        Snapshot(
            T0, {Role.TI: Reading(20.0), Role.LWT: Reading(-127.996), Role.TO: Reading(5.0, age_s=4 * 3600)}
        )
    )
    assert s.issues[Role.LWT].startswith("out of range")
    assert s.issues[Role.TO].startswith("stale")
    assert s.issues[Role.HZ] == "not configured"
    assert not s.complete


# --- accumulator -------------------------------------------------------------------------
def test_hour_record_means_counters_and_reset():
    acc = HourAccumulator()
    out = []
    heat = 100.0
    for i in range(13):  # 00:00 .. 01:00 every 5 min
        t = T0 + timedelta(minutes=5 * i)
        heat = heat + 0.1 if i != 6 else 0.05  # meter reset at 00:30
        out += acc.add(snap(t, ti=20.0 + (i < 6), heat_kwh=heat))
    assert len(out) == 1
    h = out[0]
    assert h.coverage == pytest.approx(55 / 60)
    assert 20.0 < h.means["ti"] < 21.0
    assert h.deltas_kwh["heat_kwh"] == pytest.approx(0.1 * 5 + 0.05 + 0.1 * 5, abs=1e-9)
    assert h.cls == "heating_full"


def test_dhw_flag_marks_hour():
    acc = HourAccumulator()
    out = []
    for i in range(13):
        out += acc.add(snap(T0 + timedelta(minutes=5 * i), dhw_active=1.0 if i == 3 else 0.0))
    assert out[0].cls == "dhw"


def test_day_needs_coverage():
    acc, days = HourAccumulator(), DayAggregator()
    recs = []
    for i in range(12 * 30):  # 30 hours of 5-min samples
        for h in acc.add(snap(T0 + timedelta(minutes=5 * i), heat_kwh=100 + i * 0.1)):
            d = days.add(h)
            if d:
                recs.append(d)
    assert len(recs) == 1 and recs[0].hours >= 22
    assert recs[0].heat_kwh == pytest.approx(0.1 * 12 * 24, rel=0.02)


# --- learner -----------------------------------------------------------------------------
def test_learner_converges_from_wrong_prior_with_rate_limits():
    true = ThermalParams(100.0, 3000.0, 400.0)
    lrn = BuildingLearner(ThermalParams(80.0, 3000.0, 400.0), (15.0, 80.0, 1500.0))
    rng = np.random.default_rng(5)
    first = None
    for _ in range(300):
        to = rng.uniform(-2, 14)
        ti = 20.5 + rng.normal(0, 0.2)
        dti = rng.normal(0, 0.3)
        q = true.ua_w_per_k * (ti - to) - true.gains_w + true.c_wh_per_k * dti / 24 + rng.normal(0, 80)
        before = lrn.params.ua_w_per_k
        lrn.update_day(ti, to, q * 24 / 1000, dti)
        if first is None:
            first = abs(lrn.params.ua_w_per_k - before)
    assert first <= 0.02 * 80 + 1e-9  # rate limit respected
    assert lrn.params.ua_w_per_k == pytest.approx(100, abs=5)
    assert lrn.sd["ua"] < 15


def test_learner_rejects_outliers_and_no_heat_days():
    lrn = BuildingLearner(ThermalParams(94.0, 3000.0, 440.0), (10.0, 80.0, 1500.0))
    assert not lrn.update_day(20.5, 5.0, 0.2, 0.0).accepted
    assert not lrn.update_day(20.5, 5.0, 200.0, 0.0).accepted
    assert lrn.update_day(20.5, 5.0, 24 * (94 * 15.5 - 440) / 1000, 0.0).accepted


def test_learner_state_roundtrip_and_corrupt_rejected():
    lrn = BuildingLearner(ThermalParams(94.0, 3000.0, 440.0), (10.0, 80.0, 1500.0))
    d = lrn.to_dict()
    other = BuildingLearner(ThermalParams(50.0, 3000.0, 0.0), (10.0, 80.0, 1500.0))
    assert other.load_dict(d) and other.params == lrn.params
    bad = dict(d, theta=[float("nan"), 1, 1])
    assert not other.load_dict(bad)


# --- predictor ---------------------------------------------------------------------------
B = ThermalParams(94.0, 3000.0, 440.0)
PL = PlantParams(RadiatorParams(63.1, 1.3))


def test_heating_disabled_house_cools():
    fc = forecast(21.0, False, [5.0] * 24, [21.0] * 24, [30.0] * 24, False, B, PL, lambda _: 3.0, 0.25)
    assert fc.ti_c[-1] < 21.0 and sum(fc.elec_wh) == 0


def test_thermostat_holds_band_and_uses_energy():
    n = 4 * 24
    fc = forecast(19.0, False, [3.0] * n, [21.0] * n, [30.0] * n, True, B, PL, lambda _: 3.0, 0.25)
    assert min(fc.ti_c[4 * 6 :]) >= 19.8 and max(fc.ti_c) <= 21.6  # recovers within 6 h via RT modulation
    heat, elec = fc.energy_kwh(24)
    assert heat == pytest.approx(3 * elec)
    assert 10 < heat < 40


def test_lwt_floor_applied():
    no_mod = PlantParams(RadiatorParams(63.1, 1.3), rt_modulation_gain_k_per_k=0.0)
    a = forecast(19.0, True, [12.0] * 4, [21.0] * 4, [20.0] * 4, True, B, no_mod, lambda _: 3.0)
    b = forecast(19.0, True, [12.0] * 4, [21.0] * 4, [25.0] * 4, True, B, no_mod, lambda _: 3.0)
    assert a.heat_wh == b.heat_wh


# --- engine ------------------------------------------------------------------------------
def test_engine_runs_scores_predictions_and_persists():
    eng = ShadowEngine(EngineConfig())
    st = None
    for i in range(12 * 30):
        t = T0 + timedelta(minutes=5 * i)
        st = eng.process(
            snap(t, heat_kwh=100 + i * 0.1, ti=20.5),
            [(t + timedelta(hours=h), 5.0) for h in range(1, 25)],
            {"predheat_1h": (1, 20.4)},
        )
    assert st is not None and st.telemetry_ok and st.forecast is not None and st.forecast_source == "weather"
    assert st.errors["mpc_1h"]["n"] > 0 and st.errors["predheat_1h"]["n"] > 0
    assert st.lwt_required_c is not None and st.lwt_required_c >= 25.0
    d = eng.to_dict()
    eng2 = ShadowEngine(EngineConfig())
    assert eng2.load_dict(d) == []
    assert eng2.learner.params == eng.learner.params
    assert eng2.load_dict({"version": 99}) != []


def test_engine_never_exposes_commands():
    # The shadow engine has no actuator API at all.
    assert not [
        n for n in dir(ShadowEngine) if any(w in n.lower() for w in ("write", "command", "set_", "actuat"))
    ]
