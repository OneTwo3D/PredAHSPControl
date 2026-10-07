"""Review round 9: attacks on the two accounting rules, plus further test gaps."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.daikin_mpc.core.accumulator import DayAggregator, DayRecord, HourRecord
from custom_components.daikin_mpc.core.emitter_model import RadiatorParams
from custom_components.daikin_mpc.core.engine import EngineConfig, ShadowEngine
from custom_components.daikin_mpc.core.predictor import PlantParams, plant_step
from custom_components.daikin_mpc.core.thermal_model import ThermalParams

from .test_live_core import snap

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
P = PlantParams(RadiatorParams(63.1, 1.3))
B = ThermalParams(94.0, 3000.0, 440.0)


def _days(eng):
    return {} if eng.last_day is None else {eng.last_day.day: eng.last_day}


def test_saved_state_is_detached_from_later_updates():
    eng, seen, heat = ShadowEngine(EngineConfig()), {}, 100.0
    t = T0
    for i in range(12 * 12 - 1):  # until 11:50, so the next sample stays in the same hour
        t = T0 + timedelta(minutes=5 * i)
        heat = 100 + i / 12
        eng.process(snap(t, heat_kwh=heat), None)
    saved = eng.to_dict()  # captured ...
    frozen = json.dumps(saved, sort_keys=True)
    eng.process(snap(t + timedelta(minutes=5), heat_kwh=heat + 1 / 12), None)  # ... updated before writing
    assert json.dumps(saved, sort_keys=True) == frozen  # the capture is a detached snapshot
    new = ShadowEngine(EngineConfig())
    assert new.load_dict(json.loads(json.dumps(saved))) == []
    for i in range(1, 12 * 14):
        tt = t + timedelta(minutes=5 * i)
        new.process(snap(tt, heat_kwh=heat + i / 12), None)
        seen.update(_days(new))
    assert seen["2026-11-01"].heat_kwh == pytest.approx(24.0)  # not 24.083 (counted twice)


def test_meter_reset_makes_the_date_incomplete():
    eng, seen = ShadowEngine(EngineConfig()), {}
    for i in range(12 * 26):
        t = T0 + timedelta(minutes=5 * i)
        v = 100 + i / 12 if i < 12 * 12 else (i - 12 * 12) / 12  # counter resets to 0 at 12:00
        eng.process(snap(t, heat_kwh=v), None)
        seen.update(_days(eng))
    assert "2026-11-01" not in seen


def test_clock_going_back_makes_the_date_incomplete():
    eng, seen = ShadowEngine(EngineConfig()), {}
    heat = 100.0
    times = [T0 + timedelta(minutes=5 * i) for i in range(12 * 12)]
    times += [T0 + timedelta(hours=11, minutes=5 * i) for i in range(12 * 15)]  # back to 11:00
    for t in times:
        heat += 1 / 12
        eng.process(snap(t, heat_kwh=heat), None)
        seen.update(_days(eng))
    assert "2026-11-01" not in seen


def test_learning_disabled_changes_nothing():
    eng = ShadowEngine(EngineConfig(learning_enabled=False))
    before = json.dumps({k: v for k, v in eng.to_dict().items() if k in ("learner", "cop_learner")})
    eng._on_day(DayRecord("2026-11-01", 24, 20.5, 5.0, 30.0, 10.0, 0, 0, 11.0, 11.0, 20.0))
    eng._on_day(DayRecord("2026-11-02", 24, 21.0, 4.0, 32.0, 11.0, 0, 0, 12.0, 12.0, 80.0))
    after = json.dumps({k: v for k, v in eng.to_dict().items() if k in ("learner", "cop_learner")})
    assert before == after and eng.learner.updates == 0


def test_rt_modulation_is_bounded_both_ways():
    base = 25.0
    _, _, q_hi = plant_step(19.0, True, 5.0, 22.0, base, True, B, P, 0.25)  # error +3 K
    lwt_hi = base + P.rt_modulation_max_k
    assert q_hi <= P.radiator.output_w(lwt_hi - P.flow_return_dt_k / 2, 19.0) + 1e-6
    _, _, q_lo = plant_step(20.9, True, 5.0, 21.0, 40.0, True, B, P, 0.25)
    assert (
        q_lo
        >= P.radiator.output_w(max(P.min_lwt_c, 40.0 - P.rt_modulation_max_k) - P.flow_return_dt_k / 2, 20.9)
        - 1e-6
    )


def test_thermostat_switches_exactly_at_the_hysteresis_limits():
    _, running, q = plant_step(21.0 + P.hysteresis_off_k, True, 5.0, 21.0, 30.0, True, B, P, 0.25)
    assert not running and q == 0
    _, running, q = plant_step(21.0 - P.hysteresis_on_k, False, 5.0, 21.0, 30.0, True, B, P, 0.25)
    assert running and q > 0


@pytest.mark.parametrize(("off_hours", "expect"), [(0, None), (1, None), (2, None), (3, 80.0)])
def test_standby_needs_three_off_hours(off_hours, expect):
    agg = DayAggregator()
    agg._first_partial = False
    start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    for k in range(24):
        cls = "off" if k < off_hours else "heating_full"
        agg.add(
            HourRecord(
                start + timedelta(hours=k),
                1.0,
                {"ti": 20.5, "to": 5.0, "ext_w": 80.0},
                0.0,
                30.0,
                7.0,
                {"heat_kwh": 1.0, "ext_kwh": 0.3, "dhw_elec_kwh": 0.0},
                False,
                False,
                cls,
            )
        )
    day = agg.add(
        HourRecord(
            start + timedelta(hours=24),
            1.0,
            {"ti": 20.5, "to": 5.0},
            0.0,
            30.0,
            7.0,
            {"heat_kwh": 1.0},
            False,
            False,
            "heating_full",
        )
    )
    assert day is not None and (day.standby_w == pytest.approx(expect) if expect else day.standby_w is None)
