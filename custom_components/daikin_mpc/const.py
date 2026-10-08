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
    Role.DHW_ACTIVE.value: "binary_sensor.bridge0_dhw_dhw_demand",  # dhw_dhw = DHW enabled, always on
    Role.HEATING_ENABLED.value: "switch.bridge0_mode_altherma_on",
    Role.HEATING_DEMAND.value: "binary_sensor.bridge0_mode_valve_zone_main",
    Role.HEARTBEAT.value: "sensor.bridge0_mode_date_time_daikin",
    Role.EXT_W.value: "sensor.kwh_meter_power",
    Role.EXT_KWH.value: "sensor.kwh_meter_energy_import",
    Role.DHW_ELEC_KWH.value: "sensor.bridge0_meters_electricity_consumed_compressor_dhw",
    CONF_WEATHER: "weather.forecast_home",
    CONF_PREDHEAT_H1: "predheat.internal_temp_h1",
    CONF_PREDHEAT_H8: "predheat.internal_temp_h8",
}

DEFAULT_PRIORS: Final = {CONF_UA: 94.0, CONF_GAINS: 440.0, CONF_C: 5.5}

# M3 shadow optimiser
CONF_ROOM_MIN: Final = "room_min_c"
CONF_ROOM_MAX: Final = "room_max_c"
CONF_COST_BASIS: Final = "cost_basis"
CONF_TARIFF_IMPORT: Final = "fallback_import_tariff"
CONF_TARIFF_EXPORT: Final = "fallback_export_tariff"
CONF_COMFORT_PERIODS: Final = "comfort_periods"
DEFAULT_OPTIMISER: Final = {
    CONF_ROOM_MIN: 20.0,
    CONF_ROOM_MAX: 22.0,
    CONF_COST_BASIS: "battery",
    CONF_TARIFF_IMPORT: "00:00-05:00=7.6, 05:00-24:00=34.87",
    CONF_TARIFF_EXPORT: "00:00-05:00=2.0, 05:00-24:00=12.0",
    CONF_COMFORT_PERIODS: "07:00-09:00=21, 18:00-24:00=21",
}
# Predbat entities read for prices and losses (published by the Predbat add-on while it runs)
PREDBAT_RATES: Final = "predbat.rates"
PREDBAT_RATES_EXPORT: Final = "predbat.rates_export"
PREDBAT_LOSSES: Final = (
    "input_number.predbat_battery_loss",
    "input_number.predbat_battery_loss_discharge",
    "input_number.predbat_inverter_loss",
)
OPTIMISE_INTERVAL: Final = timedelta(minutes=15)
