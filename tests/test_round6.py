"""Review round 6: code defects (part A) and mutation-backed test gaps (part B) for the core and tools."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.daikin_mpc.core.accumulator import DayAggregator, HourRecord
from custom_components.daikin_mpc.core.cost_model import CostProvider, parse_rate_series, parse_tariff
from custom_components.daikin_mpc.core.emitter_model import RadiatorParams
from custom_components.daikin_mpc.core.engine import EngineConfig, ShadowEngine
from custom_components.daikin_mpc.core.optimiser import OptimiserConfig, PlanInputs, evaluate, recommend
from custom_components.daikin_mpc.core.predictor import PlantParams
from custom_components.daikin_mpc.core.thermal_model import ThermalParams
from custom_components.daikin_mpc.core.timeutil import add_hours

from .test_live_core import snap

LON = ZoneInfo("Europe/London")
B = ThermalParams(94.0, 3000.0, 440.0)
P = PlantParams(RadiatorParams(63.1, 1.3))


def _run(start: datetime, hours: float, restart_at: datetime | None = None, kwh_per_h: float = 1.0):
    """Feed 5-minute snapshots (heat rising steadily); optional JSON round-trip of the full state."""
    eng = ShadowEngine(EngineConfig())
    heat, seen = 100.0, {}
    for i in range(int(hours * 12) + 1):
        t = add_hours(start, i / 12)
        if restart_at is not None and t == restart_at:
            state = json.loads(json.dumps(eng.to_dict()))
            eng = ShadowEngine(EngineConfig())
            assert eng.load_dict(state) == []
        if i:
            heat += kwh_per_h / 12
        eng.process(snap(t, heat_kwh=heat), None)
        if eng.last_day is not None:
            seen[eng.last_day.day] = eng.last_day
    return seen, eng


# --- part A ---------------------------------------------------------------------------------
def test_restart_keeps_an_incomplete_first_day_withheld():
    start = datetime(2026, 11, 1, 1, 0, tzinfo=UTC)
    plain, _ = _run(start, 30)
    restarted, _ = _run(start, 30, restart_at=datetime(2026, 11, 1, 3, 0, tzinfo=UTC))
    assert "2026-11-01" not in plain and "2026-11-01" not in restarted


def test_fresh_start_inside_the_midnight_hour_is_partial():
    seen, _ = _run(datetime(2026, 11, 1, 0, 30, tzinfo=UTC), 30)
    assert "2026-11-01" not in seen


def test_fresh_start_exactly_at_midnight_is_complete():
    seen, _ = _run(datetime(2026, 11, 1, 0, 0, tzinfo=UTC), 25)
    assert seen["2026-11-01"].heat_kwh == pytest.approx(24.0) and seen["2026-11-01"].length_h == 24


@pytest.mark.parametrize(("day", "length"), [("2026-10-25", 25), ("2026-03-29", 23)])
def test_dst_day_length_survives_a_late_night_restart(day, length):
    d = datetime.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, tzinfo=LON)
    restart = add_hours(datetime(d.year, d.month, d.day, 23, 55, tzinfo=LON), 0)
    plain, _ = _run(start, length + 2)
    restarted, _ = _run(start, length + 2, restart_at=restart)
    for seen in (plain, restarted):
        assert seen[day].length_h == length
        assert seen[day].heat_kwh == pytest.approx(length)


def test_charge_rate_respects_clock_changes():
    spring = CostProvider(parse_tariff("00:00-01:00=35, 01:00-02:00=1, 02:00-24:00=35"), None, "tariff")
    assert spring.charge_rate(datetime(2026, 3, 29, 12, 0, tzinfo=LON)) == 35.0  # 01:00-02:00 never happened
    autumn = CostProvider(parse_tariff("00:00-01:00=35, 01:00-01:30=1, 01:30-24:00=35"), None, "tariff")
    assert autumn.charge_rate(datetime(2026, 10, 26, 0, 45, tzinfo=LON)) == 1.0  # the GMT 01:00-01:30


def test_restored_usable_hour_without_temperatures_is_rejected():
    h = HourRecord(
        datetime(2026, 11, 2, 10, tzinfo=UTC),
        1.0,
        {},
        30.0,
        30.0,
        7.0,
        {"heat_kwh": 1.0},
        False,
        False,
        "heating_full",
    )
    with pytest.raises(ValueError):
        HourRecord.from_dict(h.to_dict())
    agg = DayAggregator()
    with pytest.raises(ValueError):
        agg.load_dict({"day": "2026-11-02", "hours": [h.to_dict()]})


# --- part B ---------------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._replies = [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True, "result": [1]},
        ]

    async def recv(self) -> str:
        return json.dumps(self._replies.pop(0))

    async def send(self, msg: str) -> None:
        self.sent.append(msg)


class _FakeConnect:
    def __init__(self, uri: str, ws: _FakeWs) -> None:
        self.uri, self.ws = uri, ws

    async def __aenter__(self) -> _FakeWs:
        return self.ws

    async def __aexit__(self, *a) -> None:
        return None


@pytest.mark.parametrize("final", ["wss://attacker.example/api/websocket", "ws://ha.example/api/websocket"])
def test_websocket_redirect_refused_before_auth(monkeypatch, final):
    import ha_client
    import websockets

    ws = _FakeWs()
    monkeypatch.setenv("HA_URL", "https://ha.example")
    monkeypatch.setenv("HA_TOKEN", "dummy-token")
    with (
        patch.object(websockets, "connect", lambda *a, **k: _FakeConnect(final, ws)),
        pytest.raises(PermissionError),
    ):
        ha_client.ws_calls([{"type": "get_states"}])
    assert not any("dummy-token" in m for m in ws.sent)


def test_websocket_same_origin_authenticates(monkeypatch):
    import ha_client
    import websockets

    ws = _FakeWs()
    monkeypatch.setenv("HA_URL", "https://ha.example")
    monkeypatch.setenv("HA_TOKEN", "dummy-token")
    with patch.object(
        websockets, "connect", lambda *a, **k: _FakeConnect("wss://ha.example/api/websocket", ws)
    ):
        assert ha_client.ws_calls([{"type": "get_states"}]) == [[1]]
    assert "dummy-token" in ws.sent[0]


def test_full_engine_state_round_trip():
    _, eng = _run(datetime(2026, 11, 1, 0, 0, tzinfo=UTC), 30)
    eng._record_external("x", datetime(2026, 11, 1, tzinfo=UTC), 1.0, 20.0)
    from custom_components.daikin_mpc.core.engine import ErrorStats

    eng.errors["mpc_1h"] = ErrorStats(5, 1.5, -0.5)
    eng.setpoint_profile[7] = 21.0
    state = json.loads(json.dumps(eng.to_dict()))
    other = ShadowEngine(EngineConfig())
    assert other.load_dict(state) == []
    assert json.loads(json.dumps(other.to_dict())) == state


def test_reported_cost_is_energy_times_price():
    rates = [10.0, -5.0, 30.0, 12.0] * 6
    inp = PlanInputs(
        datetime(2026, 1, 15, 18, tzinfo=UTC),
        20.5,
        True,
        21.0,
        True,
        [3.0] * 24,
        [32.0] * 24,
        rates,
        [21.0] * 6,
    )
    r = evaluate([21.5] * 6, inp, B, P, lambda _: 3.0, OptimiserConfig(horizon_h=6))
    assert sum(r.elec_kwh) > 0
    assert r.cost_p == pytest.approx(sum(e * p for e, p in zip(r.elec_kwh, rates, strict=False)))
    rec = recommend(inp, B, P, lambda _: 3.0, OptimiserConfig(horizon_h=6))
    assert rec.saving_p == pytest.approx(rec.baseline.cost_p - rec.plan.cost_p)


def test_predbat_offsets_are_respected():
    series = parse_rate_series({"2026-10-25T01:00:00+01:00": 10.0, "2026-10-25T01:00:00+00:00": 20.0})
    assert series is not None and series[0][1] == 10.0 and series[1][1] == 20.0
    cost = CostProvider(parse_tariff("00:00-24:00=99"), None, "tariff", series=series)
    assert cost.tariff_rate(datetime(2026, 10, 25, 0, 30, tzinfo=UTC)) == 10.0  # 01:30 BST
    assert cost.tariff_rate(datetime(2026, 10, 25, 1, 0, tzinfo=LON, fold=1)) == 20.0  # 01:00 GMT
    assert cost.tariff_rate(datetime(2026, 10, 25, 1, 40, tzinfo=UTC)) == 99.0  # last slot expired


def test_heating_disabled_through_the_engine():
    eng = ShadowEngine(EngineConfig())
    st = eng.process(snap(datetime(2026, 11, 2, 10, tzinfo=UTC), heating_enabled=0.0, hz=30.0, ti=19.0), None)
    assert st.forecast is not None
    assert sum(st.forecast.heat_wh) == 0 and sum(st.forecast.elec_wh) == 0 and not any(st.forecast.running)
    from custom_components.daikin_mpc.core.cost_model import DEFAULT_TARIFF

    rec = eng.recommend(
        snap(datetime(2026, 11, 2, 10, tzinfo=UTC), heating_enabled=0.0, hz=30.0, ti=19.0),
        None,
        CostProvider(parse_tariff(DEFAULT_TARIFF)),
        OptimiserConfig(),
    )
    assert rec is not None and rec.plan.energy_kwh() == 0 and "switched off" in rec.reason


@pytest.mark.parametrize(("day", "rows"), [("2026-01-10", 24), ("2026-03-29", 23), ("2026-10-25", 25)])
def test_offline_outdoor_coverage_and_threshold(day, rows):
    import dataset as ds
    import pandas as pd

    idx = pd.date_range(f"{day} 00:00", periods=rows + 48, freq="h", tz=ds.TZ)
    h = pd.DataFrame(
        {
            "heat_kwh_sum": np.arange(len(idx), dtype=float),
            "ti_mean": 20.0,
            "to_mean": 5.0,
            "room_set_mean": 21.0,
            "lwt_set_mean": 30.0,
            "cls": "off",
        },
        index=idx,
    )
    nxt = h.index >= h.index[0] + pd.Timedelta(hours=rows)
    nxt &= h.index < h.index[0] + pd.Timedelta(hours=rows + 24)

    def daily(mask_to=None, missing=0):
        x = h.copy()
        if mask_to is not None:
            x.loc[mask_to, "to_mean"] = np.nan
        if missing:
            x.loc[x.index[rows + 2 : rows + 2 + missing], "ti_mean"] = np.nan
        ds._increments(x, "heat_kwh_sum", "heat_kwh")
        return ds.daily(x)

    d = daily(mask_to=nxt & (h.index != h.index[rows + 12]))  # second day: one outdoor reading
    assert np.isnan(d["to"].iloc[1]) and np.isnan(d["heat_kwh"].iloc[1]) and np.isnan(d["dti_next"].iloc[0])
    assert np.isfinite(daily(missing=2)["ti"].iloc[1])  # day length − 2 valid hours: accepted
    assert np.isnan(daily(missing=3)["ti"].iloc[1])  # one fewer: rejected
