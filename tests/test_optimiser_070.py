"""0.7.0 optimiser: night minimum, Predbat-flow battery pricing, stored-heat value, clock-aligned blocks."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.daikin_mpc.core.cost_model import CostProvider, parse_tariff
from custom_components.daikin_mpc.core.emitter_model import RadiatorParams
from custom_components.daikin_mpc.core.optimiser import (
    OptimiserConfig,
    PlanInputs,
    evaluate,
    heat_value_p_per_k,
    recommend,
)
from custom_components.daikin_mpc.core.predictor import PlantParams
from custom_components.daikin_mpc.core.thermal_model import ThermalParams

B = ThermalParams(75.7, 5000.0, 111.0)
P = PlantParams(RadiatorParams(84.4, 1.3))
COP = lambda _t: 3.0  # noqa: E731
T0 = datetime(2026, 11, 20, 22, 0, tzinfo=UTC)
N = 4 * 12  # 12 h of 15-min steps


def _inp(**kw):
    base = dict(
        start=T0,
        ti0_c=20.3,
        running0=False,
        current_setpoint_c=20.0,
        heating_enabled=True,
        to_c=[4.0] * N,
        lwt_set_c=[30.0] * N,
        rate_p=[34.87] * 8 + [7.6] * 20 + [34.87] * 20,
        baseline_setpoint_c=[19.0] * 13,
    )
    base.update(kw)
    return PlanInputs(**base)


CFG = OptimiserConfig(horizon_h=12, setpoints=(19.0, 19.5, 20.0, 20.5, 21.0, 21.5, 22.0), min_floor_c=19.0)


def test_night_minimum_makes_a_setback_schedule_comparable():
    night = [19.0 if 8 <= k < 32 else 20.0 for k in range(N)]  # 00:00-06:00 at 19
    flat = evaluate([19.0] * 12, _inp(), B, P, COP, CFG)
    timed = evaluate([19.0] * 12, _inp(min_c=night), B, P, COP, CFG)
    assert flat.violation_kh > timed.violation_kh  # the setback is only "wrong" outside the night


def test_optimiser_accepts_setpoints_down_to_the_floor():
    with pytest.raises(ValueError):
        OptimiserConfig(setpoints=(19.0, 20.0))  # below room_min without a floor
    assert OptimiserConfig(setpoints=(19.0, 20.0), min_floor_c=19.0).min_floor_c == 19.0


def test_terminal_value_charges_a_plan_for_draining_the_house():
    inp = _inp()
    warm = evaluate([21.0] * 12, inp, B, P, COP, CFG)
    cold = evaluate([19.0] * 12, inp, B, P, COP, CFG)
    vpk = heat_value_p_per_k(inp, B, COP, N)
    assert vpk == pytest.approx(5.0 * 7.6 / 3.0)
    assert warm.terminal_value_p == pytest.approx(vpk * (warm.ti_c[-1] - inp.ti0_c))
    assert cold.terminal_value_p < warm.terminal_value_p
    rec = recommend(inp, B, P, COP, CFG)
    assert rec.saving_p == pytest.approx(
        (rec.baseline.cost_p - rec.baseline.terminal_value_p) - (rec.plan.cost_p - rec.plan.terminal_value_p)
    )


def test_first_block_runs_to_the_next_whole_hour():
    inp = _inp(start=T0 + timedelta(minutes=19), first_block_steps=3)  # 22:19 -> 23:00 ≈ 3 steps
    r = evaluate([20.0] * 12, inp, B, P, COP, CFG)
    assert r.block_steps[0] == 3 and set(r.block_steps[1:]) == {4}
    rec = recommend(inp, B, P, COP, CFG)
    assert rec.details["blocks"][0].startswith("22:19")


def _flows():
    t = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
    imp, exp = [], []
    ci = ce = 0.0
    for i in range(48):  # 30-min slots over a day
        s = t + timedelta(minutes=30 * i)
        imp.append((s, ci))
        exp.append((s, ce))
        h = s.hour
        if h < 5:
            ci += 1.0  # night charging
        elif 17 <= h < 19:
            ci += 0.4  # battery empty: evening grid import
        if 11 <= h < 13:
            ce += 0.5  # midday export
    return imp, exp


def test_battery_basis_prices_slots_by_predbat_flows():
    imp, exp = _flows()
    cost = CostProvider(
        parse_tariff("00:00-05:00=7.6, 05:00-24:00=34.87"),
        parse_tariff("00:00-24:00=12"),
        "battery",
        round_trip_efficiency=0.885,
        import_flow=imp,
        export_flow=exp,
    )
    at = lambda h, m=10: cost.marginal_rate(datetime(2026, 11, 20, h, m, tzinfo=UTC))  # noqa: E731
    assert at(2) == pytest.approx(7.6)  # cheap window: grid
    assert at(12) == pytest.approx(12.0)  # export hour: displaced export
    assert at(18) == pytest.approx(34.87)  # battery empty: grid at peak
    assert at(9) == pytest.approx(7.6 / 0.885)  # battery, recharged at night
    no_flows = CostProvider(
        parse_tariff("00:00-05:00=7.6, 05:00-24:00=34.87"), parse_tariff("00:00-24:00=12"), "battery", 0.885
    )
    assert no_flows.marginal_rate(datetime(2026, 11, 20, 9, 10, tzinfo=UTC)) == pytest.approx(12.0)
    assert "Predbat flows" in cost.source and "Predbat flows" not in no_flows.source
