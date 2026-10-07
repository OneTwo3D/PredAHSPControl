# Installation (M2, shadow mode)

## What it does

Every 5 minutes the integration reads the mapped entities, validates them, aggregates hours and days,
learns UA / internal gains / thermal capacity from complete days, forecasts room temperature and
heating electricity for 24 h, and scores its own (and Predheat's) room-temperature forecasts against
what actually happened. It calls only `weather.get_forecasts` (read-only). It never writes to the
Daikin, P1P2MQTT, Predbat or any other device.

## Install

HACS cannot install from a **private** GitHub repository (not even as a custom repository). Options:

1. **Make the repository public**, then in HACS → ⋮ → *Custom repositories* add
   `https://github.com/OneTwo3D/PredAHSPControl` with type *Integration*, download *Daikin MPC*,
   restart HA. The repo contains code, documentation and aggregate model results only (no tokens,
   addresses or raw history).
2. **Keep it private and copy manually**: copy `custom_components/daikin_mpc/` into
   `/config/custom_components/daikin_mpc/` (Samba, File editor, Studio Code Server or SSH add-on),
   restart HA. Repeat for updates.

Then *Settings → Devices & services → Add integration → Daikin MPC*. The form is pre-filled with the
verified entities (see `docs/entity_mapping.md`); check and submit. Options (⋮ → *Configure*) hold the
model priors and a learning on/off switch.

## First checks (first hour)

| Entity | Expect |
|---|---|
| `sensor.daikin_mpc_status` | `ok`; attribute `issues` empty. `telemetry_incomplete` lists the problem per role. |
| `sensor.daikin_mpc_predicted_room_temperature_1h/3h/6h` | plausible values near the room temperature |
| `sensor.daikin_mpc_predicted_heating_electricity_24h` | 0 while space heating is switched off; attribute `hourly` has 24 entries, `source: weather` |
| `sensor.daikin_mpc_heat_loss_coefficient` | 94 W/K (prior) until the first complete heating day has been learned |
| `sensor.daikin_mpc_last_hour_class` | after the first full hour: `off`, `heating_full`, `heating_partial`, `dhw` … |
| `sensor.daikin_mpc_prediction_error_1h` | appears after ~1 h; attributes include Predheat's 1 h error for comparison |

Learned state is stored in `/config/.storage/daikin_mpc.<entry_id>` (versioned JSON) and survives
restarts; removing the integration discards it.

## Limits of this release

- Learning updates once per complete day (≥ 22 valid hours) with heating; days without heating do not
  change UA. Thermal capacity moves slowly and stays uncertain until cold, varied weather.
- Hot-water heat is excluded from the building model via the P1P2 heating/DHW counters (1 kWh
  resolution).
- The room-setpoint schedule is learned from observation (hour-of-day profile); Onecta schedule
  changes appear with a delay.
- The heating forecast is not yet published to Predbat (milestone M4).
