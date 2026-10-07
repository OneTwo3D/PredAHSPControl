"""Review round 8: coherent energy accounting across restarts/re-mapping, heating switch, standby, tests."""

import json
from datetime import UTC, datetime

import pytest

from custom_components.daikin_mpc.core.accumulator import DayRecord
from custom_components.daikin_mpc.core.cop_learner import CopLearner
from custom_components.daikin_mpc.core.cost_model import CostProvider, parse_tariff
from custom_components.daikin_mpc.core.engine import EngineConfig, ShadowEngine
from custom_components.daikin_mpc.core.optimiser import OptimiserConfig
from custom_components.daikin_mpc.core.telemetry import Reading, Role, Snapshot, validate
from custom_components.daikin_mpc.core.timeutil import add_hours

from .test_live_core import snap

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def _run(hours, *, at=None, action=None):
    eng = ShadowEngine(EngineConfig())
    heat, seen = 100.0, {}
    for i in range(int(hours * 12) + 1):
        t = add_hours(T0, i / 12)
        if at is not None and t == at:
            eng, heat = action(eng, heat)
        if i:
            heat += 1 / 12
        eng.process(snap(t, heat_kwh=heat), None)
        if eng.last_day is not None:
            seen[eng.last_day.day] = eng.last_day
    return seen


def _restart_with_corrupt_hour(eng, heat):
    state = json.loads(json.dumps(eng.to_dict()))
    del state["hour_acc"]["acc"]["sums"]["ti"]
    new = ShadowEngine(EngineConfig())
    assert any("energy accounting" in w for w in new.load_dict(state))
    return new, heat


def _remap_heat_meter(eng, heat):
    eng.hours._last_counter.pop("heat_kwh", None)  # what the coordinator does on re-mapping
    eng.hours.counter_time.pop("heat_kwh", None)
    return eng, heat + 5000.0


def test_rejected_saved_hour_discards_the_whole_accounting_state():
    seen = _run(50, at=datetime(2026, 11, 1, 12, 55, tzinfo=UTC), action=_restart_with_corrupt_hour)
    assert "2026-11-01" not in seen  # partial energy never reaches learning
    assert seen["2026-11-02"].heat_kwh == pytest.approx(24.0)  # the next full day counts again


def test_remap_exactly_at_midnight_breaks_the_closing_day_only():
    seen = _run(50, at=datetime(2026, 11, 2, 0, 0, tzinfo=UTC), action=_remap_heat_meter)
    assert "2026-11-01" not in seen  # its 23:55-00:00 interval was not measured
    assert seen["2026-11-02"].heat_kwh == pytest.approx(24.0)  # new baseline exactly at midnight


def test_remap_mid_day_breaks_that_day():
    seen = _run(50, at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC), action=_remap_heat_meter)
    assert "2026-11-01" not in seen and seen["2026-11-02"].heat_kwh == pytest.approx(24.0)


def test_unreadable_heating_switch_makes_snapshot_incomplete():
    s = snap(T0, hz=0.0, ti=20.0)
    readings = {r: Reading(v) for r, v in s.values.items()}
    readings[Role.HEARTBEAT] = Reading(1.0)  # kept apart from values by validation
    readings[Role.HEATING_ENABLED] = Reading(None)
    v = validate(Snapshot(T0, readings))
    assert not v.complete
    eng = ShadowEngine(EngineConfig())
    st = eng.process(v, None)
    assert st.forecast is None
    assert eng.recommend(v, None, CostProvider(parse_tariff("00:00-24:00=10")), OptimiserConfig()) is None
    # unmapped (not configured) heating switch: assumed on, as before
    del readings[Role.HEATING_ENABLED]
    assert validate(Snapshot(T0, readings)).complete


def test_standby_learned_once_per_day():
    eng = ShadowEngine(EngineConfig())
    eng.cop_learner.standby_w = 20.0
    eng._on_day(DayRecord("2026-11-02", 24, 20.5, 12.0, 0.0, 0.0, 0, 0, 1.92, 1.92, 80.0))
    assert eng.cop_learner.standby_w == pytest.approx(0.9 * 20 + 0.1 * 80)


def test_recommendation_start_state_follows_the_demand_signal():
    eng = ShadowEngine(EngineConfig())
    cost = CostProvider(parse_tariff("00:00-24:00=10"))
    cfg = OptimiserConfig(horizon_h=1, setpoints=(21.0,), room_min_c=20.0, room_max_c=22.0)
    # compressor still running but the thermostat has stopped calling: no heat in the first hour
    s = snap(T0, ti=21.4, room_set=21.0, hz=30.0, heating_demand=0.0)
    rec = eng.recommend(s, None, cost, cfg)
    assert rec is not None and rec.baseline.energy_kwh() == 0
    # compressor off but the thermostat calls: heating from the start
    s = snap(T0, ti=20.0, room_set=21.0, hz=0.0, heating_demand=1.0)
    rec = eng.recommend(s, None, cost, cfg)
    assert rec is not None and rec.baseline.elec_kwh[0] > 0


@pytest.mark.parametrize("day_h", [23.0, 25.0])
def test_standby_removed_for_the_actual_day_length(day_h):
    from custom_components.daikin_mpc.core.heatpump_model import CopCurve

    cl = CopLearner(CopCurve((5.0,), (3.0,), (), ()))
    cl.standby_w = 100.0
    assert cl.update_day(5.0, 30.0, 10.0 + 0.1 * day_h, None, day_h)
    assert sum(cl.elec) == pytest.approx(10.0) and sum(cl.heat) / sum(cl.elec) == pytest.approx(3.0)
