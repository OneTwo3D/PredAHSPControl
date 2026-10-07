"""Integration tests against an in-memory Home Assistant (no live installation)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.daikin_mpc.const import CONF_WEATHER, DOMAIN, SUGGESTED

STATES = {
    "sensor.bridge0_sensors_temperature_room": "20.4",
    "sensor.bridge0_sensors_temperature_outside": "5.0",
    "sensor.bridge0_sensors_temperature_r1t_hp2gas_water": "30.0",
    "sensor.bridge0_sensors_temperature_r4t_return_water": "28.0",
    "sensor.bridge0_sensors_flow": "7.0",
    "sensor.bridge0_power_production_heatpump": "900",
    "sensor.bridge0_power_consumption_heatpump": "300",
    "sensor.bridge0_mode_compressor_rpm": "30",
    "sensor.bridge0_lwt_lwt_setpoint": "29",
    "sensor.bridge0_room_room_heating_setpoint": "21.0",
    "sensor.bridge0_meters_energy_produced_compressor_heating": "10730",
    "sensor.bridge0_meters_electricity_consumed_compressor_heating": "3031",
    "sensor.bridge0_meters_energy_produced_compressor_dhw": "3043",
    "sensor.bridge0_meters_electricity_consumed_backup_heating": "0",
    "binary_sensor.bridge0_mode_defrost_active": "off",
    "binary_sensor.bridge0_dhw_dhw_demand": "off",
    "switch.bridge0_mode_altherma_on": "on",
    "binary_sensor.bridge0_mode_valve_zone_main": "on",
    "sensor.bridge0_mode_date_time_daikin": "We 2026-10-07 09:46",
    "sensor.kwh_meter_power": "420",
    "sensor.kwh_meter_energy_import": "3909.6",
    "sensor.bridge0_meters_electricity_consumed_compressor_dhw": "1252",
    "weather.forecast_home": "cloudy",
    "predheat.internal_temp_h1": "20.3",
    "predheat.internal_temp_h8": "20.0",
}


def _set_states(hass: HomeAssistant) -> None:
    for e, v in STATES.items():
        hass.states.async_set(e, v)


@pytest.fixture
def weather_calls(hass: HomeAssistant):
    calls = []

    async def handler(call) -> ServiceResponse:
        calls.append(call)
        now = dt_util.utcnow()
        return {
            "weather.forecast_home": {
                "forecast": [
                    {"datetime": (now + timedelta(hours=h)).isoformat(), "temperature": 5.0 - 0.1 * h}
                    for h in range(1, 49)
                ]
            }
        }

    hass.services.async_register("weather", "get_forecasts", handler, supports_response=SupportsResponse.ONLY)
    return calls


async def test_config_flow_prefills_and_creates_entry(hass: HomeAssistant) -> None:
    _set_states(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.daikin_mpc.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {k: v for k, v in SUGGESTED.items() if v in STATES}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_WEATHER] == "weather.forecast_home"


async def test_config_flow_rejects_missing_entity(hass: HomeAssistant) -> None:
    _set_states(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    data = {k: v for k, v in SUGGESTED.items() if v in STATES}
    data["ti"] = "sensor.does_not_exist"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"ti": "entity_not_found"}


async def test_setup_creates_sensors_and_only_reads(hass: HomeAssistant, weather_calls) -> None:
    _set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={k: v for k, v in SUGGESTED.items() if v in STATES})
    entry.add_to_hass(hass)

    calls = []
    hass.bus.async_listen(
        EVENT_CALL_SERVICE, lambda ev: calls.append((ev.data["domain"], ev.data["service"]))
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done()

    # Only the read-only weather forecast action may be called.
    assert set(calls) <= {("weather", "get_forecasts")}
    assert weather_calls

    status = hass.states.get("sensor.daikin_mpc_status")
    assert status is not None and status.state == "ok"
    t1 = hass.states.get("sensor.daikin_mpc_predicted_room_temperature_1h")
    assert t1 is not None and 15 < float(t1.state) < 25
    ua = hass.states.get("sensor.daikin_mpc_heat_loss_coefficient")
    assert ua is not None and float(ua.state) == pytest.approx(94.0)
    e = hass.states.get("sensor.daikin_mpc_predicted_heat_pump_electricity_24h")
    assert e is not None and float(e.state) >= 0 and e.attributes["source"] == "weather"
    assert len(e.attributes["hourly"]) == 24
    assert e.attributes["standby_w"] == pytest.approx(19.0)
    assert float(e.state) >= 0.019 * 24 - 1e-6  # includes standby
    assert hass.states.get("sensor.daikin_mpc_standby_power") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unavailable_sensor_reports_incomplete(hass: HomeAssistant, weather_calls) -> None:
    _set_states(hass)
    hass.states.async_set("sensor.bridge0_sensors_temperature_r1t_hp2gas_water", "-127.996")
    entry = MockConfigEntry(domain=DOMAIN, data={k: v for k, v in SUGGESTED.items() if v in STATES})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    st = hass.states.get("sensor.daikin_mpc_status")
    assert st.state == "telemetry_incomplete"
    assert "lwt" in st.attributes["issues"]


async def test_options_flow(hass: HomeAssistant, weather_calls) -> None:
    _set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={k: v for k, v in SUGGESTED.items() if v in STATES})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "learning_enabled": False,
            "prior_ua_w_per_k": 100,
            "prior_gains_w": 400,
            "prior_c_kwh_per_k": 3.5,
            "room_min_c": 20.0,
            "room_max_c": 22.0,
            "comfort_periods": "07:00-09:00=21, 18:00-24:00=21",
            "cost_basis": "battery",
            "fallback_import_tariff": "00:00-05:00=7.6, 05:00-24:00=34.87",
            "fallback_export_tariff": "00:00-05:00=2.0, 05:00-24:00=12.0",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.engine.cfg.learning_enabled is False


async def test_reconfigure_adds_missing_roles_and_keeps_entry(hass: HomeAssistant, weather_calls) -> None:
    _set_states(hass)
    minimal = {
        k: v for k, v in SUGGESTED.items() if v in STATES and k not in ("ext_w", "ext_kwh", "dhw_elec_kwh")
    }
    entry = MockConfigEntry(domain=DOMAIN, data=minimal)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    data = {k: v for k, v in SUGGESTED.items() if v in STATES}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data["ext_w"] == "sensor.kwh_meter_power"
    assert entry.data["predheat_h1"] == "predheat.internal_temp_h1"


async def test_predheat_fields_accepted_while_predbat_is_down(hass: HomeAssistant) -> None:
    _set_states(hass)
    hass.states.async_remove("predheat.internal_temp_h1")
    hass.states.async_remove("predheat.internal_temp_h8")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    data = {k: v for k, v in SUGGESTED.items() if v in STATES}
    with patch("custom_components.daikin_mpc.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["predheat_h1"] == "predheat.internal_temp_h1"


async def test_stale_predheat_forecast_is_not_scored(hass: HomeAssistant, weather_calls) -> None:
    _set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={k: v for k, v in SUGGESTED.items() if v in STATES})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coord = entry.runtime_data
    assert "predheat_1h" in coord._external()
    with patch(
        "custom_components.daikin_mpc.coordinator.dt_util.utcnow",
        return_value=dt_util.utcnow() + timedelta(hours=1),
    ):
        assert coord._external() == {}


async def test_options_reject_bad_tariff_and_range(hass: HomeAssistant, weather_calls) -> None:
    _set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={k: v for k, v in SUGGESTED.items() if v in STATES})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "learning_enabled": True,
            "prior_ua_w_per_k": 94,
            "prior_gains_w": 440,
            "prior_c_kwh_per_k": 3.0,
            "room_min_c": 22.0,
            "room_max_c": 21.0,
            "comfort_periods": "07:00-09:00=21",
            "cost_basis": "battery",
            "fallback_import_tariff": "00:00-05:00=7.6",
            "fallback_export_tariff": "00:00-24:00=12",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["room_max_c"] == "room_range"
    assert result["errors"]["fallback_import_tariff"] == "bad_tariff"


async def test_recommendation_uses_predbat_rates(hass: HomeAssistant, weather_calls) -> None:
    _set_states(hass)
    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    rates = {(now + timedelta(hours=h)).isoformat(): (7.6 if h % 24 < 5 else 34.87) for h in range(0, 48)}
    export = {(now + timedelta(hours=h)).isoformat(): 12.0 for h in range(0, 48)}
    hass.states.async_set("predbat.rates", "7.6", {"results": rates})
    hass.states.async_set("predbat.rates_export", "12.0", {"results": export})
    for e in (
        "input_number.predbat_battery_loss",
        "input_number.predbat_battery_loss_discharge",
        "input_number.predbat_inverter_loss",
    ):
        hass.states.async_set(e, "0.03")
    entry = MockConfigEntry(domain=DOMAIN, data={k: v for k, v in SUGGESTED.items() if v in STATES})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rec = hass.states.get("sensor.daikin_mpc_recommended_room_setpoint")
    assert rec is not None and 20.0 <= float(rec.state) <= 22.0
    assert rec.attributes["cost_source"].startswith("Predbat rates")
    assert len(rec.attributes["plan"]) == 24
    assert hass.states.get("sensor.daikin_mpc_expected_saving_24h") is not None
    assert hass.states.get("sensor.daikin_mpc_recommendation").state
