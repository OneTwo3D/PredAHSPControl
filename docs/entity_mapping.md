# Entity mapping (M0 discovery, read-only)

Discovered 7 October 2026 via the HA REST/WebSocket API (HA 2026.9.3, Europe/London). 1,628 entities. Nothing was written.

Status legend: **V** verified from live state/attributes · **?** needs confirmation · **✗** unusable.

## Daikin — P1P2MQTT (`bridge0`)

### Telemetry

| Role | Entity | Unit / class | Status / notes |
|---|---|---|---|
| Room temperature (Daikin RT sensor) | `sensor.bridge0_sensors_temperature_room` | °C, measurement | V. Identical to `_room_wall`; this is the thermostat in the north-facing living room |
| Outdoor temperature | `sensor.bridge0_sensors_temperature_outside` | °C, measurement | V (`_outside_unit` also available) |
| Leaving water | `sensor.bridge0_sensors_temperature_r1t_hp2gas_water` | °C, measurement | V. **Use R1T**: `r2t_leaving_water` permanently reads −128 °C (✗) |
| Return water | `sensor.bridge0_sensors_temperature_r4t_return_water` | °C, measurement | V |
| Flow | `sensor.bridge0_sensors_flow` | L/min, measurement | V |
| Requested LWT | `sensor.bridge0_lwt_lwt_setpoint` | °C | V. Clamped at 25 °C minimum (field setting 9-01) |
| Current heating deviation | `sensor.bridge0_lwt_deviation_heating` | °C | V |
| Room heating setpoint | `sensor.bridge0_room_room_heating_setpoint` | °C | V |
| Compressor on | `binary_sensor.bridge0_mode_compressor` | – | V (no long-term statistics: binary) |
| Thermostat heating demand | `binary_sensor.bridge0_mode_valve_zone_main` | – | V (test 7 Oct 2026): on 0 s after the setpoint exceeded room temperature, pump +10 s, compressor +4 min; off with demand |
| Space heating enabled | `switch.bridge0_mode_altherma_on`; `binary_sensor.bridge0_unknown_climate_active_q4` follows it | – | V: q4 is *not* demand |
| 3-way valve | `binary_sensor.bridge0_dhw_valve_dhw_tank` | – | V: on = tank position (rest position), off = radiators |
| Compressor frequency | `sensor.bridge0_mode_compressor_rpm` | Hz, measurement | V. Usable for run/partial-hour detection in LTS |
| Defrost | `binary_sensor.bridge0_mode_defrost_active` | – | V (no LTS) |
| DHW running | `binary_sensor.bridge0_dhw_dhw_demand` | – | V: on exactly during DHW runs (e.g. 03:52–04:15 daily, matches compressor). `_dhw_dhw` = DHW function enabled (always on), `_dhw_valve_dhw_tank` always on, `_dhw_dhw_related_q` always off — not usable |
| Electrical power | `sensor.bridge0_power_consumption_heatpump` | W | V (includes pump/controls, ?) |
| Thermal power | `sensor.bridge0_power_production_heatpump` | W | V. Equals flow·cp·ΔT (bridge-calculated, not independent) |
| Backup heater power | `sensor.bridge0_power_consumption_buh` | W | V. Zero all winter |
| Heating elec/heat counters | `sensor.bridge0_meters_electricity_consumed_compressor_heating`, `…energy_produced_compressor_heating` | kWh, total_increasing | V. **1 kWh resolution**; LTS start 2025-10-19 |
| DHW elec/heat counters | `…consumed_compressor_dhw`, `…produced_compressor_dhw` | kWh | V |
| Backup heater counters | `…consumed_backup_heating`, `…consumed_backup_dhw` | kWh | V |
| Compressor hours | `sensor.bridge0_meters_hours_compressor_heating` / `_dhw` | h | V |
| Compressor starts | `sensor.bridge0_meters_starts_compressor` | – | V, but **no state_class → no LTS** |
| Faults | `sensor.bridge0_mode_errorcode1/2`, `_errorsubcode`; Onecta `binary_sensor.altherma_*_is_in_error_state_2` | – | ? need "no error" value |
| Write budget | `number.bridge0_bridge_write_budget` (100), `number.bridge0_bridge_write_budget_period` (60), `sensor.bridge0_bridge_writes_refused_budget`, `sensor.bridge0_bridge_writes_refused_busy`, `sensor.bridge0_bridge_error_budget` | – | V present; budget/period semantics ? |

### Writable candidates (none to be used before M4)

| Function | Entity | Bounds | Notes |
|---|---|---|---|
| LWT heating deviation | `climate.bridge0_lwt_abs_heating` (friendly name "LWT Deviation_Heating") | −10…+10, step 1 | Primary actuator in v1 plan. `current_temperature` reports −128 (ignore) |
| Room heating setpoint | `climate.bridge0_room_room_heating` | 16…26, step 0.5 | **Relevant because the unit runs in RT mode** (see below) |
| DHW setpoint | `climate.bridge0_dhw_dhw_setpoint` | 30…60, step 1 | M6 only |
| DHW boost | `switch.bridge0_dhw_dhw_boost` | – | M6 only, semantics ? |
| Must never be touched | `button.bridge0_mode_daikin_defrost_request`, `button.bridge0_fieldsettings_daikin_restart_careful`, field-setting numbers/selects, `number.bridge0_bridge_write_budget`, `switch.bridge0_mode_altherma_on` | – | Safety-excluded |

### Relevant native configuration (read from field settings)

| Setting | Value | Implication |
|---|---|---|
| Control mode (`select.bridge0_fieldsettings_rt_lwt`, C-07) | **RT** (Daikin room-thermostat control) | Daikin uses its own room sensor to switch heating and modulate LWT |
| RT modulation (`select.bridge0_fieldsettings_rt_modulation`, 8-05) | Modulation, max 5 K (8-06) | Daikin already adds up to ±5 K room feedback on top of the WD curve |
| Setpoint mode (`select.bridge0_mode_program_wd_abs`) | WD+prog | Weather-dependent with schedule |
| Preset (`select.bridge0_mode_preset_mode`) | Schedule; comfort 21.0, eco 19.0 | Room setpoint schedule |
| Room temperature hysteresis (9-0C) | 1.0 K | |
| Overshoot | 2 | |
| Heating LWT range (9-01 / 9-00) | 25 … 47 °C | **25 °C is also the plant's minimum achievable LWT** (confirmed); clamps the requested LWT for To ≥ ~9 °C |
| DHW reheat (6-0D), disinfection (2-00…2-04) | reheat 2; disinfection day 2, start 3, 55 °C, 45 min | M6, must be preserved |
| DHW priority (C-00) | 1 | |
| Quiet mode | Auto | |
| WD curve raw values (0-00…1-03) | decoded values look implausible (e.g. 1, 25) | ? verify decoding against the HCI/Madoka menu |

## Daikin — Onecta cloud integration (`altherma_*_2`)

Second, cloud-based path to the same unit: `climate.altherma_leaving_water_offset_2`, `climate.altherma_room_temperature_2`, `water_heater.altherma_2`, schedules `select.altherma_climatecontrol_schedule_2` (User defined 1) and `select.altherma_domestichotwatertank_schedule_2`. Reports `control_mode = roomTemperature`, `setpoint_mode = weatherDependent`, rate limit 200/day.

Risk: two independent writers to the same unit. The MPC command gateway must be the only automated writer; Onecta schedules may change setpoints underneath it and must be treated as "native schedule" input.

## Predbat (add-on, v9.2.0)

- Mode `select.predbat_mode` = Control charge & discharge; `switch.predbat_predheat_enable` = on; `sensor.predbat_load_ml_forecast` = active.
- Economics: `sensor.predbat_marginal_energy_costs` (p), `binary_sensor.predbat_low_rate_slot`, `binary_sensor.predbat_marginal_rate_now_*`, PV forecast `sensor.predbat_pv_*`, SOC max `sensor.predbat_soc_max_calculated` (26.2 kWh), reserve `input_number.predbat_set_reserve_min` (21 %).
- Tariff windows set by automations `automation.set_off_peak_rate` / `automation.set_peak_tariff` (off-peak ≈ 00:00–05:00 local).
- ? Remaining 118 `predbat.*` entities (plan, rates, SOC forecast attributes) to map in M1.

## Predheat (inside Predbat)

`predheat.heat_energy` (kWh, attribute `external`: list of `{last_updated, energy}` at 10-min steps, cumulative), `predheat.internal_temp` (+`_h1/_h2/_h8`, attribute `results`), `predheat.target_temp` 21.0, `predheat.volume_temp`, `predheat.cost*`. Configuration lives in `apps.yaml`, not visible through the API.

## Weather

`weather.forecast_home` (Met.no) and `weather.openweathermap`. Forecast via `weather.get_forecasts`.

## Existing automations touching heating

`automation.heating_switch_on/off` (input_boolean.heating_switch; last used June 2025). No automation writes the LWT deviation. The overnight −6 K deviation seen in the history (below) therefore comes from a native or Onecta schedule. ?
