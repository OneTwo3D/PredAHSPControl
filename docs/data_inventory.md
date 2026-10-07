# Data inventory and first offline findings (winter 2025/26)

Source: HA long-term statistics (hourly mean/min/max, counters), retrieved read-only on 7 October 2026. Raw exports are kept outside the repository; only aggregates are recorded here.

## Retention

| Store | Coverage |
|---|---|
| Raw recorder states | present at 60 days back, absent at 120 days → retention between 60 and 120 days |
| Hourly long-term statistics | full hourly coverage 2025-09-01 → today for all `measurement` sensors; kWh counters from 2025-10-19 |
| Binary states (compressor, defrost, DHW) and `meters_starts_compressor` | **no long-term statistics** → not available for last winter |

Action: keep raw retention ≥ 120 days (or add InfluxDB) and give `sensor.bridge0_meters_starts_compressor` a `state_class` (customize: `total_increasing`) so compressor starts are kept.

## Season summary (heating counters, 1 kWh resolution)

| Month | Ti °C | To °C | Heat kWh | Heating SCOP | DHW COP | Backup heater kWh |
|---|---|---|---|---|---|---|
| Oct 25 (from 19th) | 21.2 | 12.7 | 126 | 3.50 | 2.47 | 0 |
| Nov 25 | 20.7 | 9.8 | 430 | 3.28 | 2.13 | 0 |
| Dec 25 | 20.3 | 8.2 | 539 | 3.10 | 2.10 | 0 |
| Jan 26 | 20.5 | 5.7 | 728 | 3.02 | 2.21 | 0 |
| Feb 26 | 20.6 | 8.3 | 479 | 3.40 | 2.47 | 0 |
| Mar 26 | 20.6 | 9.8 | 375 | 3.15 | 2.46 | 0 |
| Apr 26 | 20.9 | 13.2 | 142 | 3.46 | 2.81 | 0 |

## Building model (daily regression, Nov–Mar, n = 149 days)

`Q_heat = UA·(Ti − To) − Q_gains + C·dTi/dt`

| Parameter | Estimate | Comment |
|---|---|---|
| UA | **94 ± 3 W/K** | well identified (R² = 0.90, RMSE 102 W daily mean) |
| Net internal/solar gains | 440 ± 34 W | balance point ≈ 4.5 K below room temperature |
| C | 3.0 ± 0.8 kWh/K | weakly identified from daily data; refine with hourly/raw data |
| Heat demand | ≈ 1.0 kW at 5 °C, ≈ 1.7 kW at −2 °C | small, well-insulated building load |

## Emitters (852 full-run hours without DHW)

- Mean LWT 29.4 °C, RWT 27.5 °C, **ΔT only 1.9 K** at 7.1 L/min, ≈ 930 W delivered.
- Radiator model `Q = K·(MWT − Ti)^n`: K ≈ 63 W/K^1.3 (IQR 60–66); free fit n = 1.38. Equivalent rated output ≈ **10 kW at ΔT50** vs 1.7 kW design load → emitters are ~6× oversized, so very low LWT is possible.

## Heat-pump behaviour

| To band | Days | Heating COP (daily median) | Heat kWh/day | Compressor h/day | Mean LWT setpoint | Hours with a start/stop |
|---|---|---|---|---|---|---|
| ≤ 3 °C | 9 | 2.80 | 32 | 18 | 29.1 | 23 % |
| 3–6 °C | 29 | 3.00 | 26 | 18 | 27.6 | 15 % |
| 6–9 °C | 48 | 3.33 | 17 | 12 | 26.1 | 39 % |
| 9–12 °C | 40 | 3.25 | 13 | 8 | 25.2 | 72 % |
| 12–16 °C | 23 | 4.00 | 7 | 4 | 25.2 | 61 % |

- Minimum modulated output ≈ 0.7–0.9 kW (5th percentile / median of full-run hours). Building demand falls below this at To ≳ 8 °C → **on/off cycling is unavoidable for most of the season**.
- COP ≈ 3.0–3.3 at LWT 26–29 °C is low for such water temperatures. Likely contributors: cycling losses, high pump flow / low ΔT, and power measurement including circulation pump. To verify with raw data this winter.

## Existing control pattern

- Room setpoint schedule (Jan): 19.3 °C 23:00–04:00, 20.1 °C 04:00–07:00, 21.0 °C 07:00–22:00.
- **Overnight −6 K deviation** (23:00–05:00) in Oct–Jan (508 h), set by a native/Onecta schedule. This coincides with the off-peak tariff window (≈ 00:00–05:00), i.e. heating is reduced exactly when energy is cheapest, then recovers during peak rate.
- Room temperature (Jan): minimum 19.6 °C at 03:00, maximum 21.1 °C at 21:00.

## Implications for the plan

1. **Actuator choice.** The unit runs in RT mode with ±5 K LWT modulation. The LWT deviation shifts the curve that RT modulation then adjusts, and the 25 °C LWT floor removes all downward authority at To ≳ 9 °C. The room setpoint (`climate.bridge0_room_room_heating`, 0.5 K steps) is likely the more effective lever in this installation. Decision needed before M3.
2. **Objective emphasis.** Above ~6–8 °C the controllable quantity is *when and how long* the compressor runs (fewer, longer cycles, placed in cheap/PV periods), not LWT. Below ~6 °C, continuous low-LWT operation matters.
3. **Tariff timing.** The current overnight setback works against the 00:00–05:00 off-peak window; first shadow scenario to evaluate.
4. **Hydraulics.** ΔT 1.9 K at 7 L/min suggests the pump flow is higher than needed. A flow/ΔT target review is an installer question, outside the controller's scope.
5. **Data gaps for M1.** No compressor starts, defrost or DHW states in last winter's LTS; DHW hours are inferred from DHW kWh counters (1 kWh resolution).
