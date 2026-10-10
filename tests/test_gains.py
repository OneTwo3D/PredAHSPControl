"""Measured heat gains: household electricity, sun, inverter losses, tank (core/gains.py)."""

import json
import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.daikin_mpc.core.accumulator import DayRecord, HourRecord
from custom_components.daikin_mpc.core.capacity_learner import CapacityLearner
from custom_components.daikin_mpc.core.engine import EngineConfig, ShadowEngine
from custom_components.daikin_mpc.core.gains import (
    GainsBreakdown,
    GainsConfig,
    GainsForecaster,
    gains_basis,
    gains_complete,
    household_electricity_w,
    inverter_loss_w,
    measured_gains,
    parse_solcast,
    pv_at,
)
from custom_components.daikin_mpc.core.learner import BuildingLearner
from custom_components.daikin_mpc.core.telemetry import Reading, Role, Snapshot, validate
from custom_components.daikin_mpc.core.thermal_model import ThermalParams

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def vs(t=T0, **vals):
    """Validated snapshot with only the given roles (gains need no heat-pump roles)."""
    return validate(Snapshot(t, {Role(k): Reading(v) for k, v in vals.items()}))


def full(t, **vals):
    base = {
        Role.TI: 20.5,
        Role.TO: 5.0,
        Role.LWT: 30.0,
        Role.RWT: 28.0,
        Role.HZ: 30.0,
        Role.FLOW: 7.0,
        Role.HEAT_KWH: 100.0,
        Role.ELEC_KWH: 30.0,
        Role.ROOM_SET: 21.0,
        Role.LWT_SET: 29.0,
        Role.HEATING_ENABLED: 1.0,
        Role.HEARTBEAT: 1.0,
    }
    base.update({Role(k): v for k, v in vals.items()})
    return validate(Snapshot(t, {r: Reading(v) for r, v in base.items()}))


# --- household electricity ------------------------------------------------------------------
def test_household_subtracts_heat_pump_ev_and_outside_loads():
    s = vs(house_w=900.0, ext_w=300.0, elec_w=250.0, ev_w=100.0, outside_w=110.0)
    assert household_electricity_w(s) == pytest.approx(390.0)  # external meter preferred
    s = vs(house_w=900.0, elec_w=250.0)
    assert household_electricity_w(s) == pytest.approx(650.0)  # Daikin reading as fallback
    assert household_electricity_w(vs(house_w=100.0, ext_w=300.0)) == 0.0  # never negative
    assert household_electricity_w(vs(ext_w=300.0)) is None  # no house sensor


def test_household_counts_nothing_by_default_but_scales_when_configured():
    s = vs(house_w=900.0, ext_w=300.0)
    assert measured_gains(s, GainsConfig()).household_w == 0.0
    assert measured_gains(s, GainsConfig(household_factor=0.5)).household_w == pytest.approx(300.0)


# --- inverter losses ------------------------------------------------------------------------
def test_measured_inverter_loss_wins_over_the_battery_estimate():
    cfg = GainsConfig(battery_loss_fraction=0.05)
    assert inverter_loss_w(vs(inverter_loss_w=140.0, battery_w=-2000.0), cfg) == pytest.approx(140.0)
    assert inverter_loss_w(vs(battery_w=-2000.0), cfg) == pytest.approx(100.0)  # |W| × fraction
    assert inverter_loss_w(vs(battery_w=2000.0), cfg) == pytest.approx(100.0)
    assert inverter_loss_w(vs(), cfg) is None
    # a loss sensor reading 0 is a measurement, not "missing": no fallback to the estimate
    assert inverter_loss_w(vs(inverter_loss_w=0.0, battery_w=3000.0), cfg) == 0.0


def test_configured_but_invalid_loss_sensor_is_unknown_not_the_estimate():
    s = vs(inverter_loss_w=-50.0, battery_w=1000.0)  # below the role's range -> invalid
    assert s.get(Role.INVERTER_LOSS_W) is None
    # the estimate misses the standby consumption: mixing it in would change the learned base gains
    assert inverter_loss_w(s, GainsConfig(battery_loss_fraction=0.04)) is None
    assert not gains_complete(s, GainsConfig())


# --- breakdown ------------------------------------------------------------------------------
def test_breakdown_terms_and_total():
    cfg = GainsConfig(household_factor=0.5, solar_factor=2.0, tank_ua_w_per_k=1.5)
    s = vs(ti=20.0, house_w=700.0, ext_w=100.0, solar_radiation=50.0, inverter_loss_w=150.0, tank_c=50.0)
    g = measured_gains(s, cfg)
    assert (g.household_w, g.solar_w, g.inverter_w, g.tank_w) == pytest.approx((300.0, 100.0, 150.0, 45.0))
    assert g.total_w == pytest.approx(595.0)
    assert g.as_dict() == {
        "household_w": 300.0,
        "solar_w": 100.0,
        "inverter_w": 150.0,
        "tank_w": 45.0,
        "total_w": 595.0,
    }


def test_tank_needs_room_temperature_and_never_cools():
    cfg = GainsConfig()
    assert measured_gains(vs(tank_c=50.0), cfg).tank_w == 0.0  # no room temperature
    assert measured_gains(vs(ti=21.0, tank_c=18.0), cfg).tank_w == 0.0  # colder tank adds nothing


def test_unmapped_sources_contribute_nothing():
    assert measured_gains(vs(ti=20.0), GainsConfig()) == GainsBreakdown()


# --- forecaster -----------------------------------------------------------------------------
def _learned(cfg=None):
    fc = GainsForecaster(cfg or GainsConfig(household_factor=0.5))
    for d in range(30):
        for h in range(24):
            t = T0.replace(hour=0) + timedelta(days=d, hours=h)
            fc.learn(vs(t, house_w=400.0 + 10 * h, ext_w=0.0, inverter_loss_w=100.0 + h), None)
    return fc


def test_profiles_learn_by_hour_of_day():
    fc = _learned()
    assert fc.household[5] == pytest.approx(450.0, rel=0.01)
    assert fc.inverter[5] == pytest.approx(105.0, rel=0.01)


def test_forecast_uses_now_first_then_profiles_and_holds_the_tank():
    cfg = GainsConfig(household_factor=0.5)
    fc = _learned(cfg)
    now = GainsBreakdown(household_w=999.0, inverter_w=888.0, tank_w=30.0)
    times = [T0.replace(hour=3, minute=30) + timedelta(hours=i) for i in range(3)]
    out = fc.forecast(times, now, None, None)
    assert out[0] == pytest.approx(999.0 + 888.0 + 30.0)  # first hour: measured values
    assert out[1] == pytest.approx(0.5 * 440.0 + 104.0 + 30.0, rel=0.01)  # 04:30 profiles
    assert out[2] == pytest.approx(0.5 * 450.0 + 105.0 + 30.0, rel=0.01)


def test_forecast_without_profile_keeps_the_measured_value():
    fc = GainsForecaster(GainsConfig())
    now = GainsBreakdown(inverter_w=120.0)
    out = fc.forecast([T0 + timedelta(hours=i) for i in range(3)], now, None, None)
    assert out == pytest.approx([120.0, 120.0, 120.0])


def test_solar_from_solcast_ratio_or_measured_radiation_in_the_first_hour_only():
    cfg = GainsConfig(solar_factor=1.5)
    fc = GainsForecaster(cfg, solar_ratio=0.1)
    times = [T0 + timedelta(minutes=10), T0 + timedelta(hours=2)]
    pv = [(T0, 2000.0), (T0 + timedelta(hours=2), 1000.0)]
    assert fc.forecast(times, GainsBreakdown(), pv, 300.0) == pytest.approx([300.0, 150.0])
    # no Solcast: measured radiation for the first hour, nothing afterwards
    assert fc.forecast(times, GainsBreakdown(), None, 300.0) == pytest.approx([450.0, 0.0])


def test_solar_ratio_learns_only_from_clear_signals():
    fc = GainsForecaster(GainsConfig())
    r0 = fc.solar_ratio
    fc.learn(vs(solar_radiation=10.0), 2000.0)  # dark: ignored
    fc.learn(vs(solar_radiation=300.0), 200.0)  # tiny forecast: ignored
    fc.learn(vs(solar_radiation=300.0), None)  # no forecast: ignored
    assert fc.solar_ratio == r0 and fc.ratio_samples == 0
    for _ in range(400):
        fc.learn(vs(solar_radiation=400.0), 2000.0)
    assert fc.solar_ratio == pytest.approx(0.2, rel=0.01) and fc.ratio_samples == 400


def test_implausible_solar_ratio_is_ignored():
    fc = GainsForecaster(GainsConfig())
    fc.learn(vs(solar_radiation=1400.0), 400.0)  # ratio 3.5 > 2
    assert fc.ratio_samples == 0


# --- Solcast helpers ------------------------------------------------------------------------
def test_pv_at_covers_the_half_hour_period():
    pv = [(T0, 1000.0), (T0 + timedelta(minutes=30), 2000.0)]
    assert pv_at(pv, T0) == 1000.0
    assert pv_at(pv, T0 + timedelta(minutes=29, seconds=59)) == 1000.0
    assert pv_at(pv, T0 + timedelta(minutes=30)) == 2000.0
    assert pv_at(pv, T0 + timedelta(hours=1)) is None
    assert pv_at(pv, T0 - timedelta(seconds=1)) is None


def test_parse_solcast_skips_bad_items_and_converts_kw():
    good = {"period_start": "2026-11-02T12:00:00+00:00", "pv_estimate": 1.5}
    attrs = [
        [
            {"period_start": "2026-11-02T12:30:00+00:00", "pv_estimate": 2.0},
            good,
            {"period_start": "2026-11-02T13:00:00", "pv_estimate": 1.0},  # naive time
            {"period_start": "bad", "pv_estimate": 1.0},
            {"period_start": "2026-11-02T13:30:00+00:00", "pv_estimate": "x"},
            {"period_start": "2026-11-02T14:00:00+00:00", "pv_estimate": -1.0},
            {"period_start": "2026-11-02T14:30:00+00:00", "pv_estimate": math.nan},
            "not a dict",
        ],
        "not a list",
    ]
    out = parse_solcast(attrs)
    assert out == [(T0, 1500.0), (T0 + timedelta(minutes=30), 2000.0)]


# --- persistence ----------------------------------------------------------------------------
def test_round_trip():
    fc = _learned()
    fc.solar_ratio, fc.ratio_samples = 0.2, 7
    other = GainsForecaster(GainsConfig())
    assert other.load_dict(json.loads(json.dumps(fc.to_dict())))
    assert other.household == pytest.approx(fc.household) and other.inverter == pytest.approx(fc.inverter)
    assert (other.solar_ratio, other.ratio_samples) == (0.2, 7)


def test_unreleased_version_1_state_is_not_loaded():
    fc = GainsForecaster(GainsConfig())
    v1 = {"version": 1, "household": {"3": 400.0}, "battery": {"3": 2000.0}, "solar_ratio": 0.2}
    assert not fc.load_dict(v1) and fc.inverter == {} and fc.household == {}


@pytest.mark.parametrize(
    "patch",
    [
        {"version": 99},
        {"household": {"24": 1.0}},
        {"household": {"-1": 1.0}},
        {"inverter": {"3": -1.0}},
        {"inverter": {"3": "nan"}},
        {"inverter": {"3": 40000.0}},
        {"solar_ratio": 5.0},
        {"solar_ratio": "x"},
        {"ratio_samples": -1},
        {"household": ["not", "a", "dict"]},
    ],
)
def test_invalid_state_is_rejected_and_leaves_the_forecaster_unchanged(patch):
    fc = _learned()
    before = (dict(fc.household), dict(fc.inverter), fc.solar_ratio, fc.ratio_samples)
    bad = {**fc.to_dict(), **patch}
    assert not fc.load_dict(bad)
    assert (fc.household, fc.inverter, fc.solar_ratio, fc.ratio_samples) == before


# --- engine wiring --------------------------------------------------------------------------
def test_engine_records_measured_gains_per_hour_and_forecasts_them():
    eng = ShadowEngine(EngineConfig())
    for m in range(0, 125, 5):
        st = eng.process(full(T0 + timedelta(minutes=m), inverter_loss_w=150.0, tank_c=50.0), None)
    assert st.gains_sources == ("inverter_loss_w", "tank_c")
    assert st.gains_now.inverter_w == pytest.approx(150.0)
    assert eng.last_hour is not None and eng.last_hour.means["gains_w"] == pytest.approx(150.0 + 1.5 * 29.5)
    steps = eng._gains_steps(full(T0 + timedelta(hours=2), inverter_loss_w=150.0), T0, 4)
    assert steps is not None and all(x >= 150.0 for x in steps)


def test_engine_without_gain_sources_adds_nothing():
    eng = ShadowEngine(EngineConfig())
    for m in range(0, 125, 5):
        st = eng.process(full(T0 + timedelta(minutes=m)), None)
    assert st.gains_sources == () and "gains_w" not in eng.last_hour.means
    assert eng._gains_steps(full(T0), T0, 4) is None


def test_daily_balance_counts_measured_gains_as_heat_input(monkeypatch):
    eng = ShadowEngine(EngineConfig())
    seen = []
    monkeypatch.setattr(eng.learner, "update_day", lambda *a, **k: seen.append((*a, k)))
    d1 = DayRecord("2026-11-01", 24, 20.0, 5.0, 20.0, 6.0, 0, 0, extra_gains_w=250.0, length_h=24.0)
    d2 = DayRecord("2026-11-02", 24, 20.5, 5.0, 20.0, 6.0, 0, 0)
    eng._on_day(d1)
    eng._on_day(d2)
    assert len(seen) == 1
    ti, to, heat_kwh, dti, length, kw = seen[0]
    # heat-pump heat and measured gains passed separately (the "no heating" check uses heat only)
    assert heat_kwh == pytest.approx(20.0) and kw["gains_kwh"] == pytest.approx(250.0 * 24 / 1000)
    assert (ti, to, dti, length) == pytest.approx((20.0, 5.0, 0.5, 24.0))


# --- review round 10 ------------------------------------------------------------------------
LON = ZoneInfo("Europe/London")


def test_unavailable_exclusion_makes_household_unknown():
    """R10-7: a configured but unavailable heat-pump/EV/outside meter must not count as 0 W."""
    base = {Role.HOUSE_W: Reading(3000.0), Role.EXT_W: Reading(None)}
    s = validate(Snapshot(T0, base))
    assert household_electricity_w(s) is None
    assert not gains_complete(s, GainsConfig())
    for role in (Role.EV_W, Role.OUTSIDE_W):
        s = validate(
            Snapshot(T0, {Role.HOUSE_W: Reading(3000.0), Role.EXT_W: Reading(500.0), role: Reading(None)})
        )
        assert household_electricity_w(s) is None


def test_completeness_requires_every_configured_source():
    cfg = GainsConfig()
    ok = validate(
        Snapshot(T0, {Role.TI: Reading(20.0), Role.TANK_C: Reading(50.0), Role.SOLAR: Reading(10.0)})
    )
    assert gains_complete(ok, cfg)
    no_sun = validate(
        Snapshot(T0, {Role.TI: Reading(20.0), Role.TANK_C: Reading(50.0), Role.SOLAR: Reading(None)})
    )
    assert not gains_complete(no_sun, cfg)
    no_room = validate(Snapshot(T0, {Role.TANK_C: Reading(50.0)}))
    assert not gains_complete(no_room, cfg)
    no_battery = validate(Snapshot(T0, {Role.BATTERY_W: Reading(None)}))
    assert not gains_complete(no_battery, cfg)
    # a failing battery sensor does not matter while the measured loss is available
    loss_ok = validate(Snapshot(T0, {Role.INVERTER_LOSS_W: Reading(100.0), Role.BATTERY_W: Reading(None)}))
    assert gains_complete(loss_ok, cfg)


def test_hours_record_gains_only_while_complete():
    """R10-2: an outage marks the hour incomplete instead of booking 0 W."""
    eng = ShadowEngine(EngineConfig())
    for m in range(0, 125, 5):
        loss = 150.0 if m % 10 == 5 else None
        st = eng.process(full(T0 + timedelta(minutes=m), inverter_loss_w=loss), None)
    h = eng.last_hour
    assert 0.3 < h.means["gains_ok"] < 0.7
    assert h.means["gains_w"] == pytest.approx(150.0)  # averaged over complete samples only
    assert st.gains_complete is False


@pytest.mark.parametrize(("coverage", "learned"), [(None, True), (1.0, True), (0.96, True), (0.9, False)])
def test_day_needs_full_gains_coverage(monkeypatch, coverage, learned):
    eng = ShadowEngine(EngineConfig())
    seen = []
    monkeypatch.setattr(eng.learner, "update_day", lambda *a, **k: seen.append(a))
    d1 = DayRecord("2026-11-01", 24, 20.0, 5.0, 20.0, 6.0, 0, 0, extra_gains_w=250.0, gains_coverage=coverage)
    eng._on_day(d1)
    eng._on_day(DayRecord("2026-11-02", 24, 20.5, 5.0, 20.0, 6.0, 0, 0))
    assert bool(seen) is learned


def test_no_heating_check_ignores_measured_gains():
    """R10-8: a day without heat-pump heat stays rejected however large the measured gains."""
    lr = BuildingLearner(ThermalParams(90.0, 5500.0, 70.0), (10.0, 80.0, 1500.0))
    r = lr.update_day(20.0, 10.0, 0.0, 0.0, 24.0, gains_kwh=6.0)
    assert not r.accepted and r.reason == "no heating that day" and lr.updates == 0
    assert lr.update_day(20.0, 5.0, 20.0, 0.0, 24.0, gains_kwh=6.0).accepted


def _night_hours(n, gains_fn, ua=90.0, c=5500.0, base=70.0):
    """Free-cooling nights where measured gains vary with the indoor-outdoor difference."""
    out = []
    for d in range(n):
        to = 2.0 + 10.0 * ((d * 7) % 10) / 9
        ti = 21.5
        t = datetime(2026, 11, 1, 18, tzinfo=UTC) + timedelta(days=d)
        for k in range(13):
            g = gains_fn(ti - to)
            means = {"ti": ti, "to": to, "gains_w": g, "gains_ok": 1.0}
            out.append(
                HourRecord(
                    t + timedelta(hours=k),
                    1.0,
                    means,
                    0.0,
                    0.0,
                    0.0,
                    {"heat_kwh": 0.0, "dhw_heat_kwh": 0.0},
                    False,
                    False,
                    "off",
                )
            )
            ti += (base + g - ua * (ti - to)) / c
    return out


def test_capacity_learner_removes_measured_gains_from_the_cooling_slope():
    """R10-3: gains correlated with the temperature difference no longer bias C."""
    hours = _night_hours(12, lambda dt: 100.0 + 30.0 * dt)
    cl = CapacityLearner()
    for i in range(2, len(hours)):
        cl.add(hours[i - 2], hours[i - 1], hours[i], ua=90.0)
    est = cl.estimate(90.0)
    assert est is not None and est.c_wh_per_k == pytest.approx(5500.0, rel=0.05)
    assert est.gains_night_w == pytest.approx(70.0, abs=25)  # the unmeasured base only


def test_capacity_learner_skips_pairs_with_incomplete_gains_or_no_ua():
    hours = _night_hours(1, lambda dt: 100.0)
    i = next(i for i, h in enumerate(hours) if h.start.hour == 22)
    cl = CapacityLearner()
    assert not cl.add(hours[i - 1], hours[i], hours[i + 1])  # gains recorded but no UA to remove them
    hours[i].means["gains_ok"] = 0.5
    assert not cl.add(hours[i - 1], hours[i], hours[i + 1], ua=90.0)
    hours[i].means["gains_ok"] = 1.0
    assert cl.add(hours[i - 1], hours[i], hours[i + 1], ua=90.0)


def test_solar_forecast_needs_a_radiation_sensor():
    """R10-4: Solcast alone must not add sun that the measured gains never contained."""
    fc = GainsForecaster(GainsConfig(), solar_ratio=0.1)
    pv = [(T0, 2000.0)]
    assert fc.forecast([T0], GainsBreakdown(), pv, None, solar=False) == [0.0]
    eng = ShadowEngine(EngineConfig())
    eng.pv_forecast = [(T0 + timedelta(minutes=30 * i), 2000.0) for i in range(8)]
    snap = full(T0, inverter_loss_w=100.0)
    eng.process(snap, None, pv_forecast=eng.pv_forecast)
    steps = eng._gains_steps(snap, T0, 4)
    assert steps == pytest.approx([100.0] * 4)


def test_derived_power_sources_go_stale():
    """R10-5: house and inverter-loss readings frozen for hours are rejected."""
    s = validate(
        Snapshot(
            T0,
            {
                Role.HOUSE_W: Reading(800.0, age_s=3 * 3600),
                Role.INVERTER_LOSS_W: Reading(100.0, age_s=3 * 3600),
            },
        )
    )
    assert s.get(Role.HOUSE_W) is None and "stale" in s.issues[Role.HOUSE_W]
    assert s.get(Role.INVERTER_LOSS_W) is None
    fresh = validate(Snapshot(T0, {Role.EV_W: Reading(0.0, age_s=5 * 86400)}))
    assert fresh.get(Role.EV_W) == 0.0  # slow sources keep no age limit


def test_basis_change_restarts_gain_learning():
    """R10-1: base gains learned on another basis are not reused next to the newly measured source."""
    eng = ShadowEngine(EngineConfig(gains_w=70.0))
    eng.learner.updates, eng.learner.theta[1] = 5, 440.0
    eng.gains_fc.inverter = {3: 999.0}
    eng.capacity.n = 10.0
    eng.pending_day = DayRecord("2026-11-01", 24, 20.0, 5.0, 20.0, 6.0, 0, 0)
    eng.process(full(T0, inverter_loss_w=150.0), None)
    assert eng.learner.params.gains_w == pytest.approx(70.0)
    assert eng.gains_fc.inverter == {12: 150.0} or 3 not in eng.gains_fc.inverter
    assert eng.capacity.n == 0 and eng.pending_day is None
    assert eng._basis_changed_day == "2026-11-02"
    # same mapping again: nothing restarts
    eng.learner.theta[1] = 300.0
    eng.process(full(T0 + timedelta(minutes=5), inverter_loss_w=150.0), None)
    assert eng.learner.params.gains_w == pytest.approx(300.0)
    # persisted: a restart with the same mapping keeps the learning
    other = ShadowEngine(EngineConfig(gains_w=70.0))
    assert other.load_dict(json.loads(json.dumps(eng.to_dict()))) == []
    other.learner.theta[1] = 300.0
    other.process(full(T0 + timedelta(minutes=10), inverter_loss_w=150.0), None)
    assert other.learner.params.gains_w == pytest.approx(300.0) and other._basis_changed_day == "2026-11-02"
    # another source added: restart again
    other.process(full(T0 + timedelta(days=1), inverter_loss_w=150.0, tank_c=50.0), None)
    assert other.learner.params.gains_w == pytest.approx(70.0) and other._basis_changed_day == "2026-11-03"


def test_first_start_with_gains_keeps_unlearned_priors():
    eng = ShadowEngine(EngineConfig(gains_w=70.0))
    eng.process(full(T0, inverter_loss_w=150.0), None)
    assert eng._basis_changed_day is None and eng.learner.params.gains_w == pytest.approx(70.0)


def test_day_of_basis_change_is_not_learned(monkeypatch):
    eng = ShadowEngine(EngineConfig())
    eng._basis_changed_day = "2026-11-01"
    seen = []
    monkeypatch.setattr(eng.learner, "update_day", lambda *a, **k: seen.append(a))
    eng._on_day(DayRecord("2026-11-01", 24, 20.0, 5.0, 20.0, 6.0, 0, 0))
    eng._on_day(DayRecord("2026-11-02", 24, 20.5, 5.0, 20.0, 6.0, 0, 0))
    assert not seen
    eng._on_day(DayRecord("2026-11-03", 24, 20.5, 5.0, 20.0, 6.0, 0, 0))
    assert len(seen) == 1


def test_gains_basis_tracks_sources_and_relevant_factors():
    cfg = GainsConfig()
    a = gains_basis(vs(inverter_loss_w=100.0, battery_w=50.0), cfg)
    assert a == gains_basis(vs(inverter_loss_w=100.0, battery_w=50.0), GainsConfig(battery_loss_fraction=0.1))
    assert a != gains_basis(vs(inverter_loss_w=100.0), cfg)
    assert gains_basis(vs(battery_w=50.0), cfg) != gains_basis(
        vs(battery_w=50.0), GainsConfig(battery_loss_fraction=0.1)
    )
    assert gains_basis(vs(ti=20.0), cfg) == ""


@pytest.mark.parametrize(
    "patch", [{"inverter": {"14": 30000.0}}, {"ratio_samples": math.inf}, {"ratio_samples": 1.5}]
)
def test_round10_invalid_state(patch):
    """R10-9/10: inverter profile within 0-3000 W; sample count finite and whole."""
    fc = GainsForecaster(GainsConfig())
    assert not fc.load_dict({**fc.to_dict(), **patch})


def test_profile_is_an_average_not_the_last_sample():
    fc = GainsForecaster(GainsConfig(), alpha=0.5)
    fc.learn(vs(house_w=100.0, inverter_loss_w=100.0), None)
    fc.learn(vs(house_w=300.0, inverter_loss_w=300.0), None)
    assert fc.household[12] == pytest.approx(200.0) and fc.inverter[12] == pytest.approx(200.0)


def test_low_solar_ratio_is_ignored():
    fc = GainsForecaster(GainsConfig())
    fc.learn(vs(solar_radiation=21.0), 3000.0)  # ratio 0.007 < 0.01
    assert fc.ratio_samples == 0


def test_solcast_above_100_kw_is_rejected():
    out = parse_solcast([[{"period_start": "2026-11-02T12:00:00+00:00", "pv_estimate": 101.0}]])
    assert out == []


def test_pv_at_and_handoff_across_the_autumn_clock_change():
    """Absolute time, not wall clock: the repeated 01:00-02:00 hour on 25 Oct 2026 (Europe/London)."""
    first = datetime(2026, 10, 25, 1, 15, tzinfo=LON)  # BST (00:15 UTC)
    second = datetime(2026, 10, 25, 1, 15, fold=1, tzinfo=LON)  # GMT (01:15 UTC)
    pv = [
        (datetime(2026, 10, 25, 0, 0, tzinfo=UTC), 1000.0),
        (datetime(2026, 10, 25, 1, 0, tzinfo=UTC), 3000.0),
    ]
    assert pv_at(pv, first) == 1000.0 and pv_at(pv, second) == 3000.0
    fc = GainsForecaster(GainsConfig())
    fc.inverter = dict.fromkeys(range(24), 50.0)
    now = GainsBreakdown(inverter_w=200.0)
    # wall clock 01:15 -> 01:15 (fold) is a full hour later: the profile takes over at the second one
    times = [(first.astimezone(UTC) + timedelta(minutes=15 * i)).astimezone(LON) for i in range(4)] + [second]
    assert fc.forecast(times, now, None, None) == pytest.approx([200.0] * 4 + [50.0])


def test_pv_at_across_the_spring_clock_change():
    t = datetime(2026, 3, 29, 2, 30, tzinfo=LON)  # does not exist; resolves to 01:30 UTC
    pv = [(datetime(2026, 3, 29, 1, 30, tzinfo=UTC), 500.0)]
    assert pv_at(pv, datetime(2026, 3, 29, 3, 0, tzinfo=LON)) is None
    assert pv_at(pv, datetime(2026, 3, 29, 1, 30, tzinfo=UTC)) == 500.0
    assert t.utcoffset() is not None
