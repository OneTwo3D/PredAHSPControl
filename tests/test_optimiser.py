from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from custom_components.daikin_mpc.core.cost_model import (
    DEFAULT_EXPORT_TARIFF,
    DEFAULT_TARIFF,
    CostProvider,
    efficiency_from_predbat,
    parse_rate_series,
    parse_tariff,
)
from custom_components.daikin_mpc.core.emitter_model import RadiatorParams
from custom_components.daikin_mpc.core.optimiser import OptimiserConfig, PlanInputs, evaluate, recommend
from custom_components.daikin_mpc.core.predictor import PlantParams
from custom_components.daikin_mpc.core.thermal_model import ThermalParams

B = ThermalParams(94.0, 3000.0, 440.0)
P = PlantParams(RadiatorParams(63.1, 1.3))
COP = lambda t: float(np.interp(t, [1.4, 4.7, 7.6, 10.2, 12.9], [2.93, 3.25, 3.38, 3.52, 3.53]))  # noqa: E731
T0 = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
N = 96


def inputs(rates, ti0=21.0, enabled=True, to=3.0, base=21.0):
    to_steps = [to] * N
    lwt = [max(25.0, 47 - 22 * (x + 23) / 33) for x in to_steps]
    return PlanInputs(T0, ti0, False, base, enabled, to_steps, lwt, rates, [base] * 24)


def rates_for(cost: CostProvider) -> list[float]:
    return [cost.marginal_rate(T0 + timedelta(minutes=15 * i + 7)) for i in range(N)]


# --- cost model ----------------------------------------------------------------------
def test_parse_tariff_wrap_and_coverage():
    t = parse_tariff("23:00-05:00=7, 05:00-23:00=30")
    assert any(p.start_min == 1380 for p in t)
    with pytest.raises(ValueError):
        parse_tariff("00:00-05:00=7.6")


def test_efficiency_from_predbat_losses():
    assert efficiency_from_predbat(0.03, 0.03, 0.03) == pytest.approx(0.97**4)


def test_marginal_rate_battery_export_and_vpp():
    z = T0.tzinfo
    imp = parse_rate_series(
        {
            "2026-01-15T00:00:00+00:00": 7.6,
            "2026-01-15T05:00:00+00:00": 34.87,
            "2026-01-15T19:00:00+00:00": 134.87,
            "2026-01-15T20:00:00+00:00": 34.87,
            "2026-01-15T23:30:00+00:00": 34.87,
        }
    )
    exp = parse_rate_series(
        {
            "2026-01-15T00:00:00+00:00": 2.0,
            "2026-01-15T05:00:00+00:00": 12.0,
            "2026-01-15T19:00:00+00:00": 150.0,
            "2026-01-15T20:00:00+00:00": 12.0,
            "2026-01-15T23:30:00+00:00": 12.0,
        }
    )
    cp = CostProvider(
        parse_tariff(DEFAULT_TARIFF),
        parse_tariff(DEFAULT_EXPORT_TARIFF),
        round_trip_efficiency=0.885,
        series=imp,
        export_series=exp,
    )
    at = lambda h: datetime(2026, 1, 15, h, 15, tzinfo=z)  # noqa: E731
    assert cp.marginal_rate(at(2)) == pytest.approx(7.6)  # cheap import
    assert cp.marginal_rate(at(12)) == pytest.approx(12.0)  # export opportunity > storage cost
    assert cp.marginal_rate(at(19)) == pytest.approx(134.87)  # VPP: export 150 capped by import 134.87
    raw = CostProvider(parse_tariff(DEFAULT_TARIFF), basis="tariff", series=imp)
    assert raw.marginal_rate(at(12)) == pytest.approx(34.87)
    # beyond Predbat's horizon -> fixed fallback
    assert cp.tariff_rate(datetime(2026, 1, 17, 2, 0, tzinfo=z)) == pytest.approx(7.6)


# --- optimiser -----------------------------------------------------------------------
def test_comfort_range_is_held():
    cp = CostProvider(
        parse_tariff(DEFAULT_TARIFF), parse_tariff(DEFAULT_EXPORT_TARIFF), round_trip_efficiency=0.885
    )
    r = recommend(inputs(rates_for(cp)), B, P, COP)
    assert r.plan.feasible
    assert r.plan.min_ti_c >= 19.95 and r.plan.max_ti_c <= 22.05
    assert all(20.0 <= sp <= 22.0 for sp in r.plan.setpoints_c)
    assert r.plan.cost_p <= r.baseline.cost_p + 1e-6 or not r.baseline.feasible


def test_raw_tariff_shifts_heat_into_cheap_window():
    cp = CostProvider(parse_tariff(DEFAULT_TARIFF), basis="tariff")
    r = recommend(inputs(rates_for(cp)), B, P, COP)
    night = [sp for h, sp in enumerate(r.plan.setpoints_c) if (18 + h) % 24 < 5]
    day = [sp for h, sp in enumerate(r.plan.setpoints_c) if 8 <= (18 + h) % 24 < 16]
    assert max(night) > min(day)  # preheat at 7.6p, coast at 34.87p
    assert r.saving_p > 0


def test_vpp_event_is_avoided_by_preheating():
    flat = [12.0] * N
    event = flat.copy()
    for k in range(4 * 4, 6 * 4):  # 22:00-24:00 VPP window: 134.87p
        event[k] = 134.87
    r_flat = recommend(inputs(flat), B, P, COP)
    r_evt = recommend(inputs(event), B, P, COP)
    e_flat = sum(r_flat.plan.elec_kwh[16:24])
    e_evt = sum(r_evt.plan.elec_kwh[16:24])
    assert e_evt < e_flat
    assert r_evt.plan.feasible


def test_heating_disabled_plans_nothing():
    r = recommend(inputs([12.0] * N, enabled=False), B, P, COP)
    assert r.plan.energy_kwh() == 0
    assert "switched off" in r.reason


def test_infeasible_is_reported():
    weak = PlantParams(RadiatorParams(5.0, 1.3))
    r = recommend(inputs([12.0] * N, ti0=19.0, to=-10.0), B, weak, COP)
    assert not r.plan.feasible
    assert "cannot be held" in r.reason


def test_evaluate_matches_optimiser_path():
    cp = CostProvider(parse_tariff(DEFAULT_TARIFF), basis="tariff")
    inp = inputs(rates_for(cp))
    r = recommend(inp, B, P, COP)
    again = evaluate(r.plan.setpoints_c, inp, B, P, COP, OptimiserConfig())
    assert again.cost_p == pytest.approx(r.plan.cost_p)
