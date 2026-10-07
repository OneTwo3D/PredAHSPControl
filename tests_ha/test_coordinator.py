"""Coordinator, options and sensor behaviour that the review (round 6) found untested."""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.json import json_dumps
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_integration import STATES, _set_states

from custom_components.daikin_mpc.const import DOMAIN, SUGGESTED
from custom_components.daikin_mpc.core.telemetry import Role
from custom_components.daikin_mpc.diagnostics import async_get_config_entry_diagnostics

HEARTBEAT = "sensor.bridge0_mode_date_time_daikin"
REC = "sensor.daikin_mpc_recommended_room_setpoint"


async def _setup(hass: HomeAssistant, states: bool = True) -> MockConfigEntry:
    if states:
        _set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={k: v for k, v in SUGGESTED.items() if v in STATES})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_recommendation_cleared_when_telemetry_fails_and_replanned_on_recovery(
    hass: HomeAssistant, weather_calls
) -> None:
    entry = await _setup(hass)
    coord = entry.runtime_data
    assert coord.recommendation is not None and hass.states.get(REC).state not in ("unknown", "unavailable")

    hass.states.async_set(HEARTBEAT, "unavailable")
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert coord.recommendation is None and coord.data.recommendation is None
    st = hass.states.get(REC)
    assert st.state == "unknown" and "plan" not in st.attributes

    hass.states.async_set(HEARTBEAT, "We 2026-10-07 10:00")
    await coord.async_refresh()  # immediately, not after the 15-minute throttle
    await hass.async_block_till_done()
    assert coord.recommendation is not None and "plan" in hass.states.get(REC).attributes


async def test_optimiser_throttle_and_retry_after_failure(hass: HomeAssistant, weather_calls) -> None:
    entry = await _setup(hass)
    coord = entry.runtime_data
    snap = coord._snapshot(dt_util.now())
    calls: list[int] = []
    sentinel = coord.recommendation

    def fake(*_a, **_k):
        calls.append(1)
        return sentinel

    t0 = dt_util.now()
    coord._last_optimise = None
    with patch.object(coord.engine, "recommend", side_effect=fake):
        for minutes, expected in ((0, 1), (5, 1), (60, 2)):
            await coord._async_optimise(t0 + timedelta(minutes=minutes), snap, True)
            assert len(calls) == expected

    def fail_once(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return sentinel

    calls.clear()
    coord._last_optimise, coord.recommendation = None, None
    with patch.object(coord.engine, "recommend", side_effect=fail_once):
        await coord._async_optimise(t0, snap, True)
        assert coord.recommendation is None and coord.optimiser_error == "boom"
        await coord._async_optimise(t0 + timedelta(minutes=5), snap, True)  # retried, not throttled
        assert len(calls) == 2 and coord.recommendation is sentinel and coord.optimiser_error is None


async def test_startup_before_entities_exist_recovers_without_reload(
    hass: HomeAssistant, weather_calls
) -> None:
    entry = await _setup(hass, states=False)
    st = hass.states.get("sensor.daikin_mpc_status")
    assert st is not None and st.state == "telemetry_incomplete"
    assert entry.runtime_data.recommendation is None
    _set_states(hass)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.daikin_mpc_status").state == "ok"
    assert entry.runtime_data.recommendation is not None


async def test_units_converted_end_to_end(hass: HomeAssistant, weather_calls) -> None:
    entry = await _setup(hass)
    coord = entry.runtime_data
    hass.states.async_set("sensor.bridge0_sensors_temperature_room", "68.72", {"unit_of_measurement": "°F"})
    hass.states.async_set("sensor.kwh_meter_power", "0.42", {"unit_of_measurement": "kW"})
    hass.states.async_set(
        "sensor.bridge0_meters_energy_produced_compressor_heating", "10730000", {"unit_of_measurement": "Wh"}
    )
    snap = coord._snapshot(dt_util.now())
    assert abs(snap.readings[Role.TI].value - 20.4) < 1e-9
    assert abs(snap.readings[Role.EXT_W].value - 420.0) < 1e-9
    assert abs(snap.readings[Role.HEAT_KWH].value - 10730.0) < 1e-9

    hass.states.async_set("sensor.bridge0_sensors_temperature_room", "20.4", {"unit_of_measurement": "dK"})
    await coord.async_refresh()
    await hass.async_block_till_done()
    st = hass.states.get("sensor.daikin_mpc_status")
    assert st.state == "telemetry_incomplete" and "unsupported unit" in st.attributes["issues"]["ti"]


async def test_options_reject_bad_comfort_periods(hass: HomeAssistant, weather_calls) -> None:
    entry = await _setup(hass)
    base = {
        "learning_enabled": True,
        "prior_ua_w_per_k": 94,
        "prior_gains_w": 440,
        "prior_c_kwh_per_k": 3.0,
        "room_min_c": 20.0,
        "room_max_c": 22.0,
        "cost_basis": "battery",
        "fallback_import_tariff": "00:00-05:00=7.6, 05:00-24:00=34.87",
        "fallback_export_tariff": "00:00-24:00=12",
    }
    for text, error in (("07:00-09:00", "bad_periods"), ("07:00-09:00=23", "comfort_outside_range")):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**base, "comfort_periods": text}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"comfort_periods": error}
    assert "comfort_periods" not in entry.options


async def test_plan_times_keep_their_origin_and_diagnostics(hass: HomeAssistant, weather_calls) -> None:
    entry = await _setup(hass)
    coord = entry.runtime_data
    rec = coord.recommendation
    assert rec is not None and rec.start is not None
    later = dt_util.now() + timedelta(minutes=5)
    with patch("custom_components.daikin_mpc.coordinator.dt_util.now", return_value=later):
        await coord.async_refresh()  # throttled: same plan, newer status time
        await hass.async_block_till_done()
    assert coord.recommendation is rec
    plan = hass.states.get(REC).attributes["plan"]
    assert plan[0]["start"] == rec.start.replace(second=0, microsecond=0).isoformat()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["mapping"] == dict(entry.data)
    assert diag["engine_state"]["version"] == 1 and "learner" in diag["engine_state"]
    assert diag["status"] is not None and diag["status"]["telemetry_ok"] is True
    json_dumps(diag)  # must be serialisable for the download
