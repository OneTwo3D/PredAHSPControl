"""Constants for the Daikin MPC integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from .core.telemetry import Role

DOMAIN: Final = "daikin_mpc"
UPDATE_INTERVAL: Final = timedelta(minutes=5)
WEATHER_REFRESH: Final = timedelta(minutes=30)
STORAGE_VERSION: Final = 1
SAVE_DELAY_S: Final = 600

CONF_WEATHER: Final = "weather"
CONF_PREDHEAT_H1: Final = "predheat_h1"
CONF_PREDHEAT_H8: Final = "predheat_h8"
CONF_LEARNING: Final = "learning_enabled"
CONF_UA: Final = "prior_ua_w_per_k"
CONF_GAINS: Final = "prior_gains_w"
CONF_C: Final = "prior_c_kwh_per_k"

# Defaults verified for the reference installation (docs/entity_mapping.md). They are only *suggested*
# in the config flow; the user confirms or changes every mapping.
SUGGESTED: Final[dict[str, str]] = {
    Role.TI.value: "sensor.bridge0_sensors_temperature_room",
    Role.TO.value: "sensor.bridge0_sensors_temperature_outside",
    Role.LWT.value: "sensor.bridge0_sensors_temperature_r1t_hp2gas_water",
    Role.RWT.value: "sensor.bridge0_sensors_temperature_r4t_return_water",
    Role.FLOW.value: "sensor.bridge0_sensors_flow",
    Role.HEAT_W.value: "sensor.bridge0_power_production_heatpump",
    Role.ELEC_W.value: "sensor.bridge0_power_consumption_heatpump",
    Role.HZ.value: "sensor.bridge0_mode_compressor_rpm",
    Role.LWT_SET.value: "sensor.bridge0_lwt_lwt_setpoint",
    Role.ROOM_SET.value: "sensor.bridge0_room_room_heating_setpoint",
    Role.HEAT_KWH.value: "sensor.bridge0_meters_energy_produced_compressor_heating",
    Role.ELEC_KWH.value: "sensor.bridge0_meters_electricity_consumed_compressor_heating",
    Role.DHW_HEAT_KWH.value: "sensor.bridge0_meters_energy_produced_compressor_dhw",
    Role.BUH_KWH.value: "sensor.bridge0_meters_electricity_consumed_backup_heating",
    Role.DEFROST.value: "binary_sensor.bridge0_mode_defrost_active",
    Role.DHW_ACTIVE.value: "binary_sensor.bridge0_dhw_dhw",
    Role.HEATING_ENABLED.value: "switch.bridge0_mode_altherma_on",
    Role.HEARTBEAT.value: "sensor.bridge0_mode_date_time_daikin",
    CONF_WEATHER: "weather.forecast_home",
    CONF_PREDHEAT_H1: "predheat.internal_temp_h1",
    CONF_PREDHEAT_H8: "predheat.internal_temp_h8",
}

DEFAULT_PRIORS: Final = {CONF_UA: 94.0, CONF_GAINS: 440.0, CONF_C: 3.0}
