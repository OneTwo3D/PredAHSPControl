"""Review round 7: code defects (part A) and further mutation-backed test gaps (part B)."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.daikin_mpc.core.accumulator import HourAccumulator
from custom_components.daikin_mpc.core.cost_model import CostProvider, parse_tariff
from custom_components.daikin_mpc.core.emitter_model import RadiatorParams
from custom_components.daikin_mpc.core.engine import EngineConfig, ShadowEngine
from custom_components.daikin_mpc.core.learner import BuildingLearner
from custom_components.daikin_mpc.core.predictor import PlantParams, plant_step
from custom_components.daikin_mpc.core.telemetry import Reading, Role, Snapshot, validate
from custom_components.daikin_mpc.core.thermal_model import ThermalParams
from custom_components.daikin_mpc.core.timeutil import add_hours

from .test_live_core import snap

LON = ZoneInfo("Europe/London")


def _run(start, hours, *, restart_at=None, heat_missing_until=None, remap_at=None):
    eng = ShadowEngine(EngineConfig())
    heat, seen = 100.0, {}
    for i in range(int(hours * 12) + 1):
        t = add_hours(start, i / 12)
        if restart_at is not None and t == restart_at:
            state = json.loads(json.dumps(eng.to_dict()))
            eng = ShadowEngine(EngineConfig())
            assert eng.load_dict(state) == []
        if remap_at is not None and t == remap_at:
            # what the coordinator does when the heat counter is mapped to another meter
            eng.hours._last_counter.pop("heat_kwh", None)
            eng.hours.counter_time.pop("heat_kwh", None)
            heat += 5000.0  # the new meter has a different absolute reading
        if i:
            heat += 1 / 12
        s = snap(t, heat_kwh=heat)
        if heat_missing_until is not None and t < heat_missing_until:
            readings = {r: Reading(v) for r, v in s.values.items()}
            readings[Role.HEAT_KWH] = Reading(None)
            s = validate(Snapshot(t, readings))
        eng.process(s, None)
        if eng.last_day is not None:
            seen[eng.last_day.day] = eng.last_day
    return seen, eng


# --- part A -----------------------------------------------------------------------------------
def test_inconsistent_saved_hour_is_rejected_and_never_installed():
    _, eng = _run(datetime(2026, 11, 1, 0, 0, tzinfo=UTC), 0.5)
    state = json.loads(json.dumps(eng.to_dict()))
    del state["hour_acc"]["acc"]["sums"]["ti"]
    other = ShadowEngine(EngineConfig())
    warn = other.load_dict(state)
    assert any("current hour" in w for w in warn)
    other.process(snap(datetime(2026, 11, 1, 1, 5, tzinfo=UTC)), None)  # rollover must not raise


def test_late_counter_at_start_makes_the_day_incomplete():
    seen, _ = _run(
        datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
        26,
        heat_missing_until=datetime(2026, 11, 1, 0, 30, tzinfo=UTC),
    )
    assert "2026-11-01" not in seen


@pytest.mark.parametrize(("day", "length"), [("2026-10-25", 25), ("2026-03-29", 23)])
def test_dst_length_survives_a_restart_just_after_midnight(day, length):
    d = datetime.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, tzinfo=LON)
    restart = datetime(d.year, d.month, d.day + 1, 0, 30, tzinfo=LON)
    seen, _ = _run(start, length + 2, restart_at=restart)
    assert seen[day].length_h == length and seen[day].heat_kwh == pytest.approx(length)


def test_spring_change_keeps_the_surviving_part_of_a_cheap_period():
    c = CostProvider(parse_tariff("00:00-01:30=35, 01:30-02:30=1, 02:30-24:00=35"), None, "tariff")
    assert c.charge_rate(datetime(2026, 3, 29, 12, 0, tzinfo=LON)) == 1.0  # 02:00-02:30 BST happened


def test_remapped_counter_mid_day_makes_the_day_incomplete():
    seen, _ = _run(
        datetime(2026, 11, 1, 0, 0, tzinfo=UTC), 26, remap_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    )
    assert "2026-11-01" not in seen
    plain, _ = _run(datetime(2026, 11, 1, 0, 0, tzinfo=UTC), 26)
    assert plain["2026-11-01"].heat_kwh == pytest.approx(24.0)


# --- part B -----------------------------------------------------------------------------------
def test_heat_output_is_capped_at_the_heat_pump_maximum():
    p = PlantParams(RadiatorParams(63.1, 1.3))
    b = ThermalParams(94.0, 3000.0, 440.0)
    ti, running, q = plant_step(20.0, True, 0.0, 22.0, 50.0, True, b, p, 0.25)
    assert running and q == pytest.approx(p.q_max_w)
    expected = 20.0 + (p.q_max_w - b.ua_w_per_k * 20.0 + b.gains_w) * 0.25 / b.c_wh_per_k
    assert ti == pytest.approx(expected, rel=1e-3)


def test_small_counter_decrease_is_noise():
    acc = HourAccumulator()
    t = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    out = []
    for i, v in enumerate((100.0, 99.9, 100.0, 100.0)):
        out += acc.add(snap(t + timedelta(minutes=5 * i), heat_kwh=v))
    out += acc.add(snap(t + timedelta(hours=1, minutes=1), heat_kwh=100.0))
    assert out[0].deltas_kwh["heat_kwh"] == pytest.approx(0.0)


def test_thermal_capacity_is_learned():
    lr = BuildingLearner(ThermalParams(94.0, 3000.0, 440.0), (10.0, 80.0, 1500.0))
    c0 = lr.params.c_wh_per_k
    assert lr.update_day(20.5, 5.0, 30.0, 1.0, 24.0).accepted
    assert c0 < lr.params.c_wh_per_k <= c0 * 1.05 + 1e-9


def test_predbat_series_expires_exactly_after_its_last_slot():
    t0 = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
    flat = parse_tariff("00:00-24:00=35")
    c = CostProvider(flat, flat, "tariff", series=[(t0, 1.0)], export_series=[(t0, 1.0)])
    just_before = t0 + timedelta(minutes=29, seconds=59)
    at = t0 + timedelta(minutes=30)
    assert c.tariff_rate(just_before) == 1.0 and c.tariff_rate(at) == 35.0
    assert c.export_rate(just_before) == 1.0 and c.export_rate(at) == 35.0


def test_fresh_aggregator_withholds_a_first_date_that_starts_mid_day():
    from custom_components.daikin_mpc.core.accumulator import DayAggregator, HourRecord

    def hour(t):
        return HourRecord(
            t, 1.0, {"ti": 20.5, "to": 5.0}, 30.0, 30.0, 7.0, {"heat_kwh": 1.0}, False, False, "heating_full"
        )

    t = datetime(2026, 11, 1, 1, 0, tzinfo=UTC)  # starts at 01:00, energy of 00:00-01:00 unknown
    agg = DayAggregator()
    agg.add(hour(t))
    restored = DayAggregator()
    restored.load_dict(json.loads(json.dumps(agg.to_dict())))  # the flag survives a restart
    out = [restored.add(hour(t + timedelta(hours=k))) for k in range(1, 24)]
    assert not any(out)


def test_repeated_autumn_hour_checked_in_both_folds():
    # cheap 01:15-01:45; the window starts after the BST occurrence, so only the GMT one is inside it
    c = CostProvider(parse_tariff("00:00-01:15=35, 01:15-01:45=1, 01:45-24:00=35"), None, "tariff")
    assert c.charge_rate(datetime(2026, 10, 26, 0, 50, tzinfo=LON)) == 1.0
