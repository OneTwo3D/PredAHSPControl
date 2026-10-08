# M3 shadow optimiser — replay on winter 2025/26

Generated 2026-10-08 by `tools/m3_replay.py`. One 24 h plan per day from 18:00, 2025-11-01 … 2026-03-30. Comfort range 20.0–22.0 °C. Measured outdoor temperature used as a perfect forecast; preferred 21 °C 07–09 and 18–24 (soft); fixed tariff (7.6p 00–05, 34.87p otherwise; export 2p/12p); no VPP events. **All figures are model estimates for comparing schedules, not measured savings.**

## Battery- and export-aware prices (default)

150 days.

| Schedule | Cost p/day | kWh/day | Days below 20 °C min | Mean lowest room °C | Shortfall vs 21 °C periods (K·h/day) | Starts/day |
|---|---|---|---|---|---|---|
| Actual (incl. night setback) | 47.6 | 4.00 | 109 | 19.61 | 4.16 | 0.9 |
| Simple timer (21.5 °C in periods, 20.5 °C otherwise) | 48.0 | 4.17 | 23 | 20.24 | 2.08 | 0.4 |
| Optimised | 50.5 | 4.77 | 3 | 20.58 | 0.63 | 0.1 |

Optimised vs simple timer: -2.5 p/day (-5.3 %). Mean optimised setpoint night (00–05) 21.88 °C, day (08–16) 20.72 °C. Runtime median 0.58 s, max 0.86 s.

## Raw import tariff

150 days.

| Schedule | Cost p/day | kWh/day | Days below 20 °C min | Mean lowest room °C | Shortfall vs 21 °C periods (K·h/day) | Starts/day |
|---|---|---|---|---|---|---|
| Actual (incl. night setback) | 137.2 | 4.00 | 109 | 19.61 | 4.16 | 0.9 |
| Simple timer (21.5 °C in periods, 20.5 °C otherwise) | 132.9 | 4.17 | 23 | 20.24 | 2.08 | 0.4 |
| Optimised | 96.1 | 4.04 | 3 | 20.19 | 0.80 | 0.5 |

Optimised vs simple timer: +36.8 p/day (+27.7 %). Mean optimised setpoint night (00–05) 21.93 °C, day (08–16) 20.53 °C. Runtime median 0.58 s, max 0.81 s.

## Notes

- The actual schedule falls below the new 20 °C minimum on most nights (old setback ≈ 19.3 °C), so it
  is cheaper but not comparable on comfort; the simple timer is the fair reference.
- Thermostat hysteresis, RT-modulation gain and cycling are model priors until calibrated from this
  winter's data (M3 tuning step).
