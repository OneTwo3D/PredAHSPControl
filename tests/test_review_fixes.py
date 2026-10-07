"""Regression tests for the adversarial review (docs/review_2026-10.md)."""

import http.server
import itertools
import sys
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.daikin_mpc.core.accumulator import DayAggregator, DayRecord, HourAccumulator
from custom_components.daikin_mpc.core.cost_model import parse_rate_series
from custom_components.daikin_mpc.core.emitter_model import RadiatorParams
from custom_components.daikin_mpc.core.engine import EngineConfig, ShadowEngine
from custom_components.daikin_mpc.core.learner import BuildingLearner
from custom_components.daikin_mpc.core.optimiser import (
    OptimiserConfig,
    PlanInputs,
    evaluate,
    optimise,
    recommend,
)
from custom_components.daikin_mpc.core.predictor import PlantParams
from custom_components.daikin_mpc.core.telemetry import Reading, Role, Snapshot, validate
from custom_components.daikin_mpc.core.thermal_model import ThermalParams
from custom_components.daikin_mpc.core.timeutil import add_hours, day_length_h, elapsed_s
from custom_components.daikin_mpc.core.units import temperature_c, to_core_unit

from .test_live_core import snap

LON = ZoneInfo("Europe/London")
B = ThermalParams(94.0, 3000.0, 440.0)
P = PlantParams(RadiatorParams(63.1, 1.3))


# --- time zones / daylight saving -------------------------------------------------------
def test_time_arithmetic_across_autumn_change():
    a = datetime(2026, 10, 25, 1, 55, tzinfo=LON)  # BST
    b = datetime(2026, 10, 25, 1, 0, tzinfo=LON, fold=1)  # GMT, five minutes later
    assert elapsed_s(a, b) == 300
    assert add_hours(a, 1).utcoffset() == timedelta(0)
    assert day_length_h(date(2026, 10, 25), LON) == 25 and day_length_h(date(2026, 3, 29), LON) == 23


def test_accumulator_keeps_the_repeated_hour_separate():
    acc = HourAccumulator()
    t0 = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # 01:00 BST
    out = []
    for i in range(37):  # 3 h in 5-minute steps, local times converted from UTC
        out += acc.add(snap(add_hours(t0, i * 5 / 60).astimezone(LON), heat_kwh=100 + i * 0.1))
    assert len(out) == 3  # 01:00 BST, 01:00 GMT (a different hour), 02:00 GMT
    assert all(h.coverage == pytest.approx(55 / 60) for h in out[1:])
    assert out[0].start.hour == out[1].start.hour == 1
    assert out[0].start.utcoffset() != out[1].start.utcoffset()


def test_learner_uses_actual_day_length():
    a = BuildingLearner(B, (10.0, 80.0, 1500.0))
    b = BuildingLearner(B, (10.0, 80.0, 1500.0))
    a.update_day(20.5, 5.0, 24.0, 0.0, 24.0)  # 1 kW for 24 h
    b.update_day(20.5, 5.0, 25.0, 0.0, 25.0)  # 1 kW for 25 h: same physics
    assert np.allclose(a.theta, b.theta)


# --- counters across gaps ---------------------------------------------------------------
def test_counter_increment_across_midnight_gap_is_not_used_for_learning():
    acc, days = HourAccumulator(), DayAggregator()
    t = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    recs = []
    heat = 100.0
    for i in range(12 * 23):  # 23 h of data
        heat += 0.1
        for h in acc.add(snap(t + timedelta(minutes=5 * i), heat_kwh=heat)):
            recs.append(days.add(h))
    # restart: 2 h outage across midnight, counter advanced by 24 kWh meanwhile
    t2 = t + timedelta(hours=25)
    heat += 24.0
    out = []
    for i in range(12 * 30):
        heat += 0.1
        for h in acc.add(snap(t2 + timedelta(minutes=5 * i), heat_kwh=heat)):
            out.append(h)
            recs.append(days.add(h))
    assert all(h.deltas_kwh["heat_kwh"] < 2 for h in out)  # the 24 kWh is not booked into one hour
    assert any(h.counter_gap for h in out)
    assert not any(r is not None and r.day == "2026-11-03" for r in recs)


def test_short_restart_keeps_counter_increments():
    acc = HourAccumulator()
    t = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    acc.add(snap(t, heat_kwh=100.0))
    acc.add(snap(t + timedelta(minutes=5), heat_kwh=100.5))
    acc2 = HourAccumulator()
    acc2._last_counter, acc2.counter_time = dict(acc._last_counter), dict(acc.counter_time)
    acc2.add(snap(t + timedelta(minutes=8), heat_kwh=100.6))
    out = acc2.add(snap(t + timedelta(hours=1, minutes=1), heat_kwh=100.7))
    # 0.1 kWh at 10:08 (restart within 20 min) + the 10:08-11:01 increment's share before 11:00 (52/53)
    assert out[0].deltas_kwh["heat_kwh"] == pytest.approx(0.1 + 0.1 * 52 / 53)


# --- telemetry --------------------------------------------------------------------------
def test_missing_heartbeat_makes_snapshot_incomplete():
    s = snap(datetime(2026, 11, 2, tzinfo=UTC))
    readings = {r: Reading(v) for r, v in s.values.items() if r is not Role.HEARTBEAT}
    v = validate(Snapshot(s.time, readings))
    assert not v.complete and v.issues[Role.HEARTBEAT] == "not configured"


def test_units_are_converted_or_rejected():
    assert to_core_unit(Role.HEAT_KWH, 1500.0, "Wh") == 1.5
    assert to_core_unit(Role.ELEC_W, 0.42, "kW") == pytest.approx(420.0)
    assert to_core_unit(Role.TO, 41.0, "°F") == pytest.approx(5.0)
    assert to_core_unit(Role.TI, 21.0, None) == 21.0
    assert temperature_c(41.0, "°F") == pytest.approx(5.0)
    with pytest.raises(ValueError):
        to_core_unit(Role.HEAT_KWH, 1.0, "GJ")
    v = validate(
        Snapshot(datetime(2026, 11, 2, tzinfo=UTC), {Role.TI: Reading(None, 0, "unsupported unit 'x'")})
    )
    assert v.issues[Role.TI] == "unsupported unit 'x'"


# --- engine -----------------------------------------------------------------------------
def test_dhw_does_not_shift_the_lwt_offset():
    eng = ShadowEngine(EngineConfig())
    t = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    for i in range(12):
        eng.process(snap(t + timedelta(minutes=5 * i), dhw_active=1.0, lwt_set=50.0, ti=20.0), None)
    assert eng.lwt_offset_k == 0.0


@pytest.mark.parametrize(
    "state",
    [
        {"version": 1, "learner": None},
        {
            "version": 1,
            "learner": {"version": 1, "theta": [94, 440, 3000], "p": [[1, 0, 0]] * 3, "updates": "x"},
        },
        {"version": 1, "errors": {"a": []}},
        {"version": 1, "errors": [1, 2]},
        {"version": 1, "pending_day": {"day": "x"}},
        {"version": 1, "last_counters": {"heat_kwh": "nan?"}},
        {"version": 1, "cop_learner": None},
    ],
)
def test_corrupt_state_never_raises(state):
    eng = ShadowEngine(EngineConfig())
    eng.load_dict(state)
    assert eng.learner.params.ua_w_per_k == 94.0


def test_non_psd_covariance_is_rejected():
    lr = BuildingLearner(B, (10.0, 80.0, 1500.0))
    bad = {"version": 1, "theta": [94.0, 440.0, 3000.0], "p": [[1, 100, 0], [100, 1, 0], [0, 0, 1]]}
    assert not lr.load_dict(bad)


def test_nonconsecutive_days_do_not_update_the_building_model():
    eng = ShadowEngine(EngineConfig())
    eng._on_day(DayRecord("2026-11-02", 24, 20.5, 5.0, 20.0, 6.0, 0, 0))
    eng._on_day(DayRecord("2026-11-04", 24, 21.5, 5.0, 20.0, 6.0, 0, 0))
    assert eng.learner.updates == 0 and eng.learner.rejected == 0


def test_forecast_ignores_past_points_and_keeps_the_anchor():
    t = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    fc = [
        (t - timedelta(hours=1), 0.0),
        (t, 1.0),
        (t + timedelta(hours=1), 2.0),
        (t + timedelta(hours=2), float("nan")),
    ]
    steps, src = ShadowEngine._to_steps(t, 5.0, fc)
    assert src == "weather" and steps[0] == pytest.approx(5.0 + (2.0 - 5.0) * 0.125)


def test_predbat_attribute_of_wrong_type_falls_back():
    assert parse_rate_series(["a", "b"]) is None and parse_rate_series("x") is None


# --- optimiser --------------------------------------------------------------------------
def _inp(hours, ti0, rate, running=False, to=3.0):
    n = hours * 4
    lwt = [max(25.0, 47 - 22 * (to + 23) / 33)] * n
    return PlanInputs(
        datetime(2026, 1, 15, 18, tzinfo=UTC),
        ti0,
        running,
        21.0,
        True,
        [to] * n,
        lwt,
        rate * n if isinstance(rate, list) else [rate] * n,
        [21.0] * hours,
    )


def test_hard_limits_win_at_any_price():
    cfg = OptimiserConfig(horizon_h=1)
    inp = _inp(1, 20.05, 10_000.0)
    plan = optimise(inp, B, P, lambda _: 3.0, cfg)
    feasible = [sp for sp in cfg.setpoints if evaluate([sp], inp, B, P, lambda _: 3.0, cfg).feasible]
    assert feasible and plan.feasible and plan.setpoints_c[0] in feasible


def test_input_length_mismatch_is_an_error():
    inp = _inp(24, 21.0, 10.0)
    short = PlanInputs(**{**inp.__dict__, "rate_p": inp.rate_p[:48]})
    assert len(optimise(short, B, P, lambda _: 3.0).setpoints_c) == 12  # horizon = shortest input
    with pytest.raises(ValueError):
        optimise(PlanInputs(**{**inp.__dict__, "rate_p": inp.rate_p[:3]}), B, P, lambda _: 3.0)
    with pytest.raises(ValueError):
        recommend(PlanInputs(**{**inp.__dict__, "baseline_setpoint_c": [21.0] * 12}), B, P, lambda _: 3.0)


def test_dp_is_close_to_exhaustive_search():
    cfg = OptimiserConfig(horizon_h=3)
    n = 12
    rates = [30.0] * 4 + [150.0] * 4 + [30.0] * 4
    inp = PlanInputs(
        datetime(2026, 1, 15, 18, tzinfo=UTC),
        20.0594805808,
        True,
        21.0,
        True,
        [0.0] * n,
        [max(25.0, 47 - 22 * 23 / 33)] * n,
        rates,
        [21.0] * 3,
    )
    cop = lambda _: 3.0  # noqa: E731
    dp = optimise(inp, B, P, cop, cfg)
    best = min(
        (evaluate(list(sps), inp, B, P, cop, cfg) for sps in itertools.product(cfg.setpoints, repeat=3)),
        key=lambda r: (r.violation_kh > 1e-6, r.objective_p),
    )
    assert dp.feasible == best.feasible
    assert dp.objective_p <= best.objective_p * 1.01  # merging is an approximation: within 1 %


def test_schedule_label_marks_next_day():
    inp = _inp(24, 21.0, 10.0)
    rec = recommend(inp, B, P, lambda _: 3.0, OptimiserConfig(setpoints=(21.0,), room_min_c=20.0))
    assert rec.details["blocks"] == ["18:00–18:00 next day 21.0 °C"] and rec.start == inp.start


# --- tools: HA client -------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


def test_ha_client_refuses_webhooks_and_redirects(monkeypatch):
    import ha_client

    seen: list[str | None] = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)

    class Redirect(Target):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/collect")
            self.end_headers()

    src = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    for s in (target, src):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    monkeypatch.setenv("HA_URL", f"http://127.0.0.1:{src.server_port}")
    monkeypatch.setenv("HA_TOKEN", "dummy-token")
    try:
        with pytest.raises(PermissionError):
            ha_client.rest_get("/api/webhook/change_heating")
        with pytest.raises(PermissionError):
            ha_client.rest_get("/api/states")
        assert seen == []  # the token never reached the redirect target
    finally:
        src.shutdown()
        target.shutdown()


# --- round 2 ----------------------------------------------------------------------------
def test_dp_keeps_the_path_needed_for_hard_limits():
    eng = ShadowEngine(EngineConfig())
    b = ThermalParams(178.35, 3178.78, 191.99)
    to = [t for t in (7.0563, 13.9977, -6.5047, -7.3516) for _ in range(4)]
    inp = PlanInputs(
        datetime(2026, 1, 15, 18, tzinfo=UTC),
        21.6943,
        True,
        21.0,
        True,
        to,
        [eng.wc_lwt(x) for x in to],
        [35.0] * 16,
        [21.0] * 4,
    )
    plan = optimise(inp, b, eng.plant, eng.cop, OptimiserConfig(horizon_h=4))
    assert plan.feasible and plan.min_ti_c >= 20.0


def test_frozen_bridge_counters_do_not_hide_the_gap():
    acc = HourAccumulator()
    t = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
    out = []
    heat = 100.0
    for i in range(12 * 4):  # 22:00 .. 02:00
        ti = t + timedelta(minutes=5 * i)
        frozen = 18 <= i < 30  # 23:30 .. 00:30 bridge frozen, HA keeps the old value
        if not frozen:
            heat += 0.1
        else:
            heat_frozen = heat
        s = snap(ti, heat_kwh=heat_frozen if frozen else heat)
        if frozen:
            s = validate(
                Snapshot(
                    ti, {**{r: Reading(v) for r, v in s.values.items()}, Role.HEARTBEAT: Reading(1.0, 3600)}
                )
            )
        out += acc.add(s)
    assert any(h.counter_gap for h in out)


def test_gap_across_midnight_drops_the_source_day_too():
    acc, days = HourAccumulator(), DayAggregator()
    t = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    recs = []
    heat = 100.0
    for i in range(12 * 50):
        ti = t + timedelta(minutes=5 * i)
        heat += 1 / 12
        if datetime(2026, 11, 1, 23, 30, tzinfo=UTC) <= ti < datetime(2026, 11, 2, 0, 30, tzinfo=UTC):
            continue  # outage
        for h in acc.add(snap(ti, heat_kwh=heat)):
            r = days.add(h)
            if r:
                recs.append(r.day)
    assert "2026-11-01" not in recs and "2026-11-02" not in recs


def test_corrupt_cop_and_pending_day_state_rejected():
    eng = ShadowEngine(EngineConfig())
    st = eng.to_dict()
    st["cop_learner"]["standby_w"] = -10000
    warn = eng.load_dict(st)
    assert eng.standby_w == EngineConfig().standby_w and any("COP" in w for w in warn)
    st = eng.to_dict()
    st["pending_day"] = DayRecord("2026-11-02", 24, 21.0, 5.0, 20.0, 6.0, 0, 0).to_dict() | {"ti": "21.0"}
    eng.load_dict(st)
    assert isinstance(eng.pending_day.ti, float)  # normalised, so later arithmetic cannot fail
    st["pending_day"]["ti"] = "hot"
    eng2 = ShadowEngine(EngineConfig())
    assert any("pending day" in w for w in eng2.load_dict(st)) and eng2.pending_day is None


def test_feasibility_and_reported_range_agree():
    inp = PlanInputs(
        datetime(2026, 1, 15, 18, tzinfo=UTC),
        22.01,
        False,
        21.0,
        False,
        [5.0] * 4,
        [30.0] * 4,
        [10.0] * 4,
        [21.0],
    )
    r = evaluate([21.0], inp, B, P, lambda _: 3.0, OptimiserConfig(horizon_h=1))
    assert r.feasible == (r.min_ti_c >= 20.0 and r.max_ti_c <= 22.0)


# --- round 3 ----------------------------------------------------------------------------
def test_stale_bridge_across_midnight_drops_the_source_day_before_recovery():
    acc, days = HourAccumulator(), DayAggregator()
    t = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    stale_from, stale_to = datetime(2026, 11, 1, 23, 30, tzinfo=UTC), datetime(2026, 11, 2, 1, 30, tzinfo=UTC)
    recs = []
    heat = 100.0
    for i in range(12 * 50):
        ti = t + timedelta(minutes=5 * i)
        stale = stale_from <= ti < stale_to
        if not stale:
            heat += 1 / 12
        s = snap(ti, heat_kwh=heat)
        if stale:
            s = validate(
                Snapshot(
                    ti, {**{r: Reading(v) for r, v in s.values.items()}, Role.HEARTBEAT: Reading(1.0, 3600)}
                )
            )
        for h in acc.add(s):
            r = days.add(h)
            if r:
                recs.append(r.day)
    assert "2026-11-01" not in recs and "2026-11-02" not in recs


def test_battery_storage_cost_uses_predbat_rates_when_present():
    from custom_components.daikin_mpc.core.cost_model import CostProvider, parse_tariff

    t0 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
    series = [(t0 + timedelta(hours=h), 15.0 if h % 24 < 5 else 40.0) for h in range(48)]
    cost = CostProvider(parse_tariff("00:00-05:00=7.6, 05:00-24:00=35"), None, "battery", 0.9, series)
    assert cost.marginal_rate(t0 + timedelta(hours=12)) == pytest.approx(15.0 / 0.9)


def test_orphan_cop_bin_rejected():
    from custom_components.daikin_mpc.core.cop_learner import CopLearner

    eng = ShadowEngine(EngineConfig())
    d = eng.cop_learner.to_dict()
    d["heat"][3] = 100.0
    assert not CopLearner(eng.cop_prior).load_dict(d)


def test_offline_daily_totals_need_anchored_boundaries():
    import dataset as ds
    import pandas as pd

    idx = pd.date_range("2026-01-10", periods=72, freq="h", tz=ds.TZ)
    base = {"ti_mean": 20.0, "to_mean": 5.0, "room_set_mean": 21.0, "lwt_set_mean": 30.0, "cls": "off"}

    def totals(mask):
        h = pd.DataFrame({"heat_kwh_sum": np.arange(72, dtype=float), **base}, index=idx)
        h["heat_kwh_sum"] = h.heat_kwh_sum.where(mask)
        ds._increments(h, "heat_kwh_sum", "heat_kwh")
        return ds.daily(h)["heat_kwh"].tolist()

    assert totals(idx != idx[30])[1] == 24.0  # same-day gap recovered
    assert np.isnan(totals(idx >= idx[36])[1])  # series starts mid-day
    assert np.isnan(totals(idx < idx[60])[2])  # series ends mid-day


# --- round 4 ----------------------------------------------------------------------------
def test_increment_before_midnight_stays_on_its_day():
    acc = HourAccumulator()
    t = datetime(2026, 11, 2, 23, 0, tzinfo=UTC)
    out = []
    for i in range(25):
        out += acc.add(snap(t + timedelta(minutes=5 * i), heat_kwh=100.0 if i < 12 else 101.0))
    assert out[0].deltas_kwh["heat_kwh"] == pytest.approx(1.0)  # 23:55-00:00 belongs to 2 Nov
    assert out[1].deltas_kwh["heat_kwh"] == pytest.approx(0.0)


def test_unavailable_counter_at_midnight_withholds_the_day():
    acc, days = HourAccumulator(), DayAggregator()
    t = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    recs = []
    heat = 100.0
    for i in range(12 * 50):
        ti = t + timedelta(minutes=5 * i)
        heat += 1 / 12
        missing = datetime(2026, 11, 1, 23, 55, tzinfo=UTC) <= ti < datetime(2026, 11, 2, 2, 0, tzinfo=UTC)
        s = snap(ti, heat_kwh=heat)
        if missing:
            readings = {r: Reading(v) for r, v in s.values.items()}
            readings[Role.HEAT_KWH] = Reading(None)
            s = validate(Snapshot(ti, readings))
        for h in acc.add(s):
            r = days.add(h)
            if r:
                recs.append(r.day)
    assert "2026-11-01" not in recs


def test_expired_predbat_series_does_not_set_the_storage_cost():
    from custom_components.daikin_mpc.core.cost_model import CostProvider, parse_tariff

    t0 = datetime(2026, 1, 14, 0, 0, tzinfo=UTC)
    series = [(t0 + timedelta(hours=h), 15.0) for h in range(24)]  # ended 15 Jan 00:00
    cost = CostProvider(
        parse_tariff("00:00-05:00=7.6, 05:00-24:00=35"),
        parse_tariff("00:00-24:00=12"),
        "battery",
        0.9,
        series,
    )
    noon = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    assert cost.tariff_rate(noon) == 35.0
    assert cost.marginal_rate(noon) == pytest.approx(12.0)  # max(7.6 / 0.9, 12)


def test_truncated_last_day_rejected_offline():
    import dataset as ds
    import pandas as pd

    idx = pd.date_range("2026-01-10 00:00", "2026-01-11 22:00", freq="h", tz=ds.TZ)
    base = {"ti_mean": 20.0, "to_mean": 5.0, "room_set_mean": 21.0, "lwt_set_mean": 30.0, "cls": "off"}
    h = pd.DataFrame({"heat_kwh_sum": np.arange(len(idx), dtype=float), **base}, index=idx)
    ds._increments(h, "heat_kwh_sum", "heat_kwh")
    d = ds.daily(h)
    assert np.isnan(d["heat_kwh"].iloc[1]) and d["day_h"].iloc[1] == 24 and np.isnan(d["dti_next"].iloc[0])
