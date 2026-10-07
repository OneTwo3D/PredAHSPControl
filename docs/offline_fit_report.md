# Offline fit report (M1)

Generated 2026-10-07 by `tools/fit_offline.py` from hourly long-term statistics, window 2025-11-01 … 2026-03-31, outdoor sensor `to`. Aggregates only.

## Building (daily energy balance)

| Parameter | Value |
|---|---|
| UA | 94.2 ± 2.7 W/K |
| Net gains | 440 ± 34 W |
| C | 3.0 ± 0.8 kWh/K |
| Fit | R² 0.896, RMSE 102 W (daily mean), 149 days |
| Hourly-dynamics cross-check | C 3.4 kWh/K, UA 72 W/K (biased low by radiator lag), gains 441 W |

Held-out check (fit on even ISO weeks, 74 days; test on odd weeks, 75 days):
daily heat MAE 1.8 kWh (mean 16.8); daily heating electricity MAE 0.75 kWh, bias +0.37 kWh (mean 5.3 kWh). Counters have 1 kWh resolution.

## Radiators

- Full-run hours without DHW: 852; mean LWT 29.4 °C, RWT 27.5 °C, flow 7.1 L/min, hydronic heat 932 W.
- K = 63.1 W/K^1.3 (IQR 59.6–66.1), free-fit n = 1.38, rated output ≈ 10.2 kW at ΔT50.
- Mean water temperature needed to hold 20.5 °C: -3 °C → 33.5 °C, +0 °C → 31.9 °C, +5 °C → 29.0 °C, +10 °C → 25.8 °C

## Heat pump

- Minimum modulated output (5th percentile of full-run hours) 682 W; median 899 W.

| Outdoor (energy-weighted) °C | COP (monotone) | Heat kWh | Elec kWh |
|---|---|---|---|
| 1.4 | 2.79 | 290 | 104 |
| 4.6 | 3.01 | 744 | 247 |
| 7.6 | 3.28 | 835 | 254 |
| 10.2 | 3.28 | 517 | 158 |
| 12.9 | 3.81 | 164 | 43 |

## Interval classes (hours)

heating_partial: 1529, off: 1090, heating_full: 852, dhw: 152
