# Predheat calibration (offline, winter 2025/26)

> **Update (M1):** the values below were validated by replaying Predheat over 150 winter days
> (`tools/predheat_replay.py`, report in `docs/predheat_replay_report.md`). The **recommended values are
> the "fitted" set in that report**, which supersede the first-pass table below where they differ
> (`heat_loss_degrees` 0.0315, `heat_min_power` 680, `heat_cop` 3.81 with a full `heat_pump_efficiency`
> table). Hold-out result versus current settings: daily heating-electricity error 0.98 → 0.84 kWh/day,
> bias +0.40 → −0.11 kWh/day, hourly room-temperature RMSE 1.90 → 1.16 K, 24 h room error 2.56 → 1.16 K.
>
> A further "calibrated" set fits room temperature better (RMSE 0.75 K) but pushes several parameters to
> their search limits and over-predicts electricity by ~0.5 kWh/day; since Predbat consumes the energy
> forecast it is **not** recommended. The remaining room-temperature error is structural: Predheat does
> not model the Daikin's RT modulation or the overnight deviation schedule.
>
> `hysteresis`/`hysteresis_off` stay at 1.0. The `heating_energy` fix below still applies.
>
> **`volume_temp` note (verified in Predheat/Predbat source):** Predheat reads `volume_temp` with `get_arg("volume_temp", …)` *without* `domain="predheat"`, and Predbat's `get_arg` then looks only at the **top level** of `pred_bat:` (or HA config), so a `volume_temp:` inside the `predheat:` block is ignored. Per the Predheat docs the sensor must measure radiator water *near the room thermostat*, not at the heat pump, so the Daikin return sensor (R4T) is **not** a suitable substitute (earlier suggestion withdrawn). Use the living-room Zigbee radiator sensor, placed at the top level of `pred_bat:`, once it is back online; until then Predheat falls back to its simulated `input_number.predbat_next_volume_temp`.

## First pass (superseded where noted)

Based on the daily/hourly fits in `docs/data_inventory.md` and the current `predheat:` section of `apps.yaml` (shared 7 October 2026). Predheat semantics verified against `apps/predbat/predheat.py` (batpred `main`):

- `heat_loss_watts` = UA (W/K); `watt_per_degree = heat_loss_watts / heat_loss_degrees` is the building heat capacity C (Wh/K).
- `heat_energy` / `$external` is **cumulative electrical** energy (`heat_power_out / (heat_cop · cop_adjust)`), keyed every 10 min.
- COP: `heat_cop × heat_pump_efficiency[outdoor °C] / max(table)`.
- Heat output modulates between `heat_min_power` and `heat_max_power` in proportion to `(flow_temp − volume_temp) / flow_difference_target`.

Suggestions only. Nothing has been changed in HA; apply manually and compare forecast error over the following weeks.

## Parameter comparison

| Setting | Current | Fitted / observed | Suggested | Confidence | Effect of current value |
|---|---|---|---|---|---|
| `heat_loss_watts` | 156 | UA 94 ± 3 W/K (daily balance, R² 0.90) | **95** | high | Heat demand over-predicted by ~65 % at design conditions |
| `heat_gain_static` | 400 | 440 ± 34 W | 420 | medium | fine |
| `heat_loss_degrees` | 0.0194 → C = 8.0 kWh/K | C ≈ 3.0 kWh/K (daily 3.0 ± 0.8; hourly 2.7–3.2) | **0.030** (→ C ≈ 3.2 kWh/K) | medium | Room responds ~2.5× too slowly in the forecast |
| `heat_output` | 10630 | K·50^1.3 ≈ 10.2 kW (K ≈ 63, n ≈ 1.3–1.38) | keep 10630 | high | consistent with radiator data |
| `heat_volume` | 121 | not identifiable from data | keep | – | – |
| `heat_max_power` | 4000 | EDLA04E2V3, 4 kW class | keep | medium | – |
| `heat_min_power` | 500 | min. modulated output ≈ 680 W (P5), median 900 W | **750** | medium | Under-predicts cycling in mild weather |
| `heat_cop` + efficiency table | 3.68 with default table (≈ 2.5 at 0 °C, 2.95 at 5 °C, 3.35 at 10 °C) | measured daily COP 2.8 (≤ 3 °C), 3.0 (3–6), 3.3 (6–12), 4.0 (12–16) | `heat_cop: 4.0` + table below | medium (1 kWh counter resolution) | Slightly pessimistic in cold weather |
| `flow_difference_target` | 5 | observed flow−return ΔT ≈ 1.9 K | 3 | low | Predheat-internal tuning parameter; check forecast shape |
| `hysteresis` / `hysteresis_off` | 1.0 / 1.0 | Daikin room hysteresis 9-0C = 1.0 | keep | medium | – |
| `weather_compensation` | −23: 47, 10: 25 | observed LWT setpoint 29.1 (≤ 3 °C) … 25.2 (≥ 9 °C) | keep (matches); note RT modulation adds ±5 K | medium | – |

Suggested `heat_pump_efficiency` (outdoor °C → COP, normalised by its maximum; values are measured daily medians, smoothed):

```yaml
    heat_cop: 4.0
    heat_pump_efficiency:
      -10: 2.3
      -5: 2.5
      0: 2.75
      3: 2.85
      5: 3.0
      8: 3.3
      10: 3.3
      12: 3.6
      14: 4.0
      20: 4.0
```

## Input entities

| Setting | Current entity | Status | Suggestion |
|---|---|---|---|
| `external_temperature` | `sensor.ecowitt_outdoor_temperature` | valid | keep |
| `internal_temperature` | `sensor.bridge0_sensors_temperature_room` | valid | keep |
| `target_temperature` | `sensor.bridge0_room_room_heating_setpoint` | valid | keep |
| `heating_energy` | `sensor.ashp_daily_electricity` | valid, but **includes DHW** (today 0.94 kWh = 0.12 heating + 0.82 DHW) | `sensor.ashp_heating_power_consumption_daily` (heating only, same sensor already used as `car_charging_energy` filter) |
| `heating_active` | `binary_sensor.bridge0_unknown_climate_active_q4` | exists; meaning undocumented ("unknown") | verify against compressor/thermostat demand this winter; alternative `binary_sensor.bridge0_mode_compressor` |
| `volume_temp` | `sensor.ashp_flow_temperature_living_room_temperature` | **unavailable since 26 Sep** (Zigbee sensor offline/battery); also ignored inside `predheat:` | fix the sensor and move the key to the top level of `pred_bat:` (see note above) |

## Predbat interaction

- `load_forecast: predheat.heat_energy$external` with `car_charging_energy: sensor.ashp_heating_power_consumption_daily` removes heating from historical load and adds the Predheat forecast: consistent, no double counting for space heating. DHW remains in the historical base load, which is acceptable while Predheat models space heating only.
- `load_ml_enable: true`: confirm that Predbat's ML load model applies the same `car_charging_energy` filtering; otherwise space heating is counted twice (ML history + Predheat).

## Next step (M1)

Replay Predheat's simulation with current vs suggested values against this winter's measurements once live data exists (or on last winter's hourly LTS), and report daily-energy and room-temperature error before/after.
