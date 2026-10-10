# Energy sensor clean-up plan (Oct 2026)

Goal: one accurate, consistent set of power (W) and energy (kWh) sensors, used by every consumer
(Predbat, PredAI, Predheat, the Daikin MPC integration, the HA Energy dashboard and the dashboards).
All in one package file `packages/energy.yaml`; faulty helpers are **replaced in place** (same entity
IDs, so dashboards and statistics continue).

## 1. What is measured where (verified 1–10 Oct 2026)

| Quantity | Best source | Notes |
|---|---|---|
| Grid (+import / −export) | `sensor.smart_meter_electricity_power`, counters `…_energy_import` / `…_energy_export` | Glow/Hildebrand local MQTT, ~10–20 s updates, billing-grade counters; power integral = import − export to 1 %. |
| Grid, Solis CT | `sensor.solis_meter_active_power` (+ = export) | ≈ −0.94 × smart meter + 53 W: 6 % scale error + offset → **fallback only**. |
| Solis AC output (+ to house / − charging from grid) | `sensor.solis_active_power` | The only AC-side Solis signal; no energy counter → integral. |
| Solis PV (DC, "garage" array) | `sensor.solis_pv_total_power`, `sensor.solis_power_generation_today` | `pv_production_garage` is a duplicate of the latter. |
| Battery (DC, + discharge) | `sensor.battery_power` (template, correct), `solis_battery_charge/discharge_today` | Agrees with the SOC trend. |
| Roof GoodWe GW2000 (AC) | −`sensor.kwh_meter_power_2`, `kwh_meter_energy_export_2` − `…import_2` | HomeWizard meter; better than the GoodWe's own figure (`inverter_gw2000_ss`, −4 %). |
| Shed micro-inverter (AC) | `sensor.shed_power_ac`, integral `pv_production_shed_total` | |
| Heat pump (whole unit) | `sensor.kwh_meter_power`, `kwh_meter_energy_import` | HomeWizard meter 1; ≈ 15–50 W standby. Source of `ashp_daily_electricity`. |
| Heat pump split heating / DHW | ESPAltherma `bridge0_meters_electricity_consumed_compressor_{heating,dhw}` (Daikin estimates) | Used only for the **ratio**; the meter gives the total. |
| EV charger | `sensor.hypervolt_ev_power` | 0 today. |
| Loft IT (outside envelope) | `sensor.roof_loft_it_power` | Part of the house load. |

Topology check: the Solis CT and the smart meter see the same point (GW2000 and shed are not between
them), so **house load = grid + Solis AC + roof + shed** (AC side). Losses of the Solis
(conversion + standby) = Solis PV + battery − Solis AC: ~100–250 W while the battery works,
≈ 3–4 kWh/day — real heat inside the envelope, but **not** household load.

## 2. Faults found in the current helpers

| Helper | Formula today | Fault |
|---|---|---|
| `sensor.pv_power` | Solis PV − `kwh_meter_power_2` + `sensor.shed_inverter_power_ac` | `shed_inverter_power_ac` no longer exists → shed (~1 kWh/day) missing. |
| `sensor.solis_power` | −`solis_meter_active_power` | Grid from the less accurate Solis CT. |
| `sensor.house_power` | `battery_power` + `solis_power` + `pv_power` | DC side → includes Solis losses (+250 W when the battery charges/discharges hard); Solis-CT grid; no shed. |
| `sensor.total_house_consumption` | PV kWh − charged + discharged + smart import − **Solis** export | Mixes meters; DC battery → includes losses (9 Oct: 15.3 vs 11.8 kWh real); midnight glitch (31 → −11 kWh, filtered by Predbat). |
| `sensor.house_load_today` | YAML template (not readable via API) | Duplicate; unused. |
| `sensor.ashp_heating_power_consumption_annually` | YAML template | Shows 48 521 kWh — broken. |
| Energy dashboard grid export | `solis_grid_export_today` | Solis CT instead of smart meter. |
| Predbat `export_today`, `grid_power` | Solis CT | as above. |
| PredAI / Predbat heat-pump subtraction | PredAI: all HP; Predbat: heating only | Inconsistent. |

## 3. Canonical set (`packages/energy.yaml`)

Power (W, `state_class: measurement`, `availability` templates so missing inputs give *unavailable*,
never a silent 0):

| Entity | Definition | Replaces |
|---|---|---|
| `sensor.solis_power` | **Grid power** (+import): smart meter; if unavailable, `(53 − solis_meter_active_power) / 0.94` | in place (same meaning, better source) |
| `sensor.pv_power` | Solis PV + roof (−kwh_meter_power_2) + shed | in place (adds shed) |
| `sensor.battery_power` | unchanged | — |
| `sensor.house_power` | grid + Solis AC + roof + shed (≥ 0) | in place (AC side) |
| `sensor.solis_loss_power` *(new)* | Solis PV + battery − Solis AC (≥ 0) | — |
| `sensor.heat_pump_power` *(new alias)* | `kwh_meter_power` | — |
| `sensor.heat_pump_dhw_power` *(new)* | heat-pump power while the Daikin makes DHW (3-way valve / DHW demand signal) | — |
| `sensor.heat_pump_heating_power` *(new)* | heat-pump power − DHW power | — |
| `sensor.house_power_excl_heat_pump` *(new)* | house − heat pump | — |
| `sensor.house_power_excl_heating` *(new)* | house − heat-pump heating (DHW stays in) | — |

Energy (kWh; integral `method: left`, `max_sub_interval: 1 min`; utility meters for daily values):

| Entity | Definition | Replaces |
|---|---|---|
| `sensor.house_energy_total` *(new)* | integral of `house_power` | — |
| `sensor.total_house_consumption` | daily utility meter of `house_energy_total` | in place (no losses, no glitch) |
| `sensor.solis_loss_energy_daily` *(new)* | daily of integral of `solis_loss_power` | — |
| `sensor.heat_pump_heating_energy_daily`, `…_dhw_energy_daily` *(new)* | daily of the split integrals; checked against `ashp_daily_electricity` | fixes `ashp_*_power_consumption_*` |
| `sensor.house_load_excl_heating_daily` *(new)* | daily of integral of `house_power_excl_heating` | — |
| `pv_total_energy`, `energy_import_export`, smart-meter dailies | unchanged (correct) | — |

## 4. Who uses what

| Consumer | Setting | New source |
|---|---|---|
| Predbat | `load_today` | `sensor.total_house_consumption` (fixed) |
| | `load_power` | `sensor.house_power` (fixed); drop `load_power_1` (bypass ≈ 0) |
| | `import_today` / `export_today` | smart-meter import / **export** daily |
| | `pv_today` / `pv_power` | `pv_total_energy` / `pv_power` (fixed) |
| | `grid_power` | `sensor.solis_power` (now smart-meter grid; check sign / `grid_power_invert`) |
| | `car_charging_energy` | `sensor.heat_pump_heating_energy_daily` (Predheat models heating; DHW stays in the load) |
| PredAI | `sensors: total_house_consumption`, `subtract` | subtract `heat_pump_heating_energy_daily` (same rule as Predbat) — or retire PredAI (its forecast is not used) |
| Predheat | heat energy | unchanged |
| Daikin MPC integration | `HOUSE_W` | `sensor.house_power` |
| | `BATTERY_W` (losses) | `sensor.solis_loss_power` measured directly (code change: role becomes "inverter losses", fraction 1.0) |
| | `EXT_W`, `EV_W`, `OUTSIDE_W` | `kwh_meter_power`, `hypervolt_ev_power`, `roof_loft_it_power` |
| Energy dashboard | grid | smart-meter import + **export** dailies |
| | solar | Solis, roof, shed (unchanged; drop nothing) |
| | battery | Solis counters (unchanged) |
| | devices | heat pump heating, heat pump DHW, **Solis losses**, EV → "home" untracked = real household rest |
| Dashboards | solis-control, predbat-control | keep working (same entity IDs, same meaning) |

## 5. Migration (each step reversible)

1. **Backup** (HA full backup) and note the current Predbat/PredAI/Energy settings.
2. **Phase A – add new sensors only** (`packages/energy.yaml` with the *new* entities; enable
   `homeassistant: packages: !include_dir_named packages`). Nothing existing changes.
3. **Validate 3–7 days** (read-only script): daily `house_energy_total` vs counter balance
   (import − export + roof + shed + Solis PV + discharged − charged − losses), heat-pump split vs
   `ashp_daily_electricity`, fallback grid vs smart meter.
4. **Phase B – replace in place**: delete the UI helpers `house_power`, `pv_power`, `solis_power`,
   `total_house_consumption` (Settings → Helpers) and in the same restart add their YAML versions
   (`default_entity_id` + `unique_id`) → same entity IDs, statistics continue (values before the
   switch are the old, inflated ones).
5. **Repoint consumers** (§4): Predbat `apps.yaml`, PredAI, Energy dashboard, integration options.
6. **Clean-up**: remove `house_load_today`, `pv_production_garage` (duplicate), the broken
   `ashp_*_power_consumption_*` YAML templates (after the new split is confirmed), and the stale
   `shed_inverter_power_ac` reference.
7. **Integration v0.6.0**: map `HOUSE_W`, switch battery gains to measured Solis losses, finish
   Option B (household factor from clean data).

## 6. Decisions (10 Oct 2026)

* PredAI is **retired** (its forecast was not used by Predbat); remove the add-on and
  `sensor.total_house_consumption_prediction`.
* The YAML templates `ashp_{heating,dhw}_power_consumption_*` are **replaced** by utility meters on
  the metered split (`heat_pump_{heating,dhw}_energy_*`; renamed to the ashp_* IDs in phase B).
* DHW split signal: `binary_sensor.bridge0_dhw_dhw_demand` (the 3-way valve rests on "tank" when
  idle, so it cannot separate DHW from standby). 4–10 Oct: 6.8 kWh DHW, 2.3 kWh standby.
* Phase A package: `docs/ha/packages/energy.yaml` (all templates rendered live without errors).

## 7. Progress

* 10 Oct 2026: phase A package installed (28 entities live).
* 10 Oct 2026: Energy dashboard updated: grid export -> smart meter; devices: heat pump (parent
  `ashp_daily_electricity`) with heating / hot water / standby sub-devices, and Solis inverter losses.
