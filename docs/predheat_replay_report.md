# Predheat replay and calibration (M1)

Generated 2026-10-07 by `tools/predheat_replay.py`. Window 2025-11-01 … 2026-03-31, 150 complete days (74 train / 76 test, split by ISO-week parity). Outdoor sensor `to_ecowitt`, measured (perfect forecast). Mean measured heating electricity on test days: 5.14 kWh/day.

## Hold-out error

| Set | Elec MAE kWh/day | Elec bias kWh/day | Room RMSE K (hourly) | Room MAE @6 h K | Room MAE @24 h K |
|---|---|---|---|---|---|
| current | 0.98 | +0.40 | 1.90 | 0.65 | 2.56 |
| fitted | 0.84 | -0.11 | 1.16 | 0.82 | 1.16 |
| calibrated | 0.92 | +0.49 | 0.75 | 0.32 | 0.79 |

## Parameters

| Parameter | current | fitted | calibrated |
|---|---|---|---|
| `heat_loss_watts` | 156 | 94 | 94 |
| `heat_loss_degrees` | 0.0194 | 0.0315 | 0.02 |
| `heat_gain_static` | 400 | 440 | 440 |
| `heat_min_power` | 500 | 680 | 1500 |
| `flow_difference_target` | 5 | 5 | 1 |
| `heat_cop` | 3.68 | 3.81 | 3.81 |
| `hysteresis` | 1 | 1 | 1.5 |
| `hysteresis_off` | 1 | 1 | 0.1091 |
| implied C (kWh/K) | 8.0 | 3.0 | 4.7 |

Calibrated parameters at a search bound (structural mismatch, treat with caution): `heat_loss_degrees`, `heat_min_power`, `flow_difference_target`, `hysteresis`.

## Recommended `predheat:` values (fitted set: lowest hold-out electricity error)

```yaml
    heat_loss_watts: 94
    heat_loss_degrees: 0.0315
    heat_gain_static: 440
    heat_min_power: 680
    flow_difference_target: 5.0
    heat_cop: 3.81
    hysteresis: 1.00
    hysteresis_off: 1.00
    heat_pump_efficiency:
      -20: 1.93
      -18: 1.97
      -16: 2.02
      -14: 2.07
      -12: 2.11
      -10: 2.2
      -8: 2.3
      -6: 2.39
      -4: 2.48
      -2: 2.57
      0: 2.66
      2: 2.83
      4: 2.97
      6: 3.14
      8: 3.28
      10: 3.28
      12: 3.63
      14: 3.81
      16: 3.81
      18: 3.81
      20: 3.81
```

Limitations: Predheat does not model the Daikin's RT modulation, the overnight deviation schedule or DHW interruptions; the daily counters have 1 kWh resolution; hourly room temperatures are means.
