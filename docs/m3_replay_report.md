# M3 shadow optimiser — replay on winter 2025/26

Generated 2026-10-07 by `tools/m3_replay.py`. One 24 h plan per day from 18:00, 2025-11-01 … 2026-03-30. Comfort range 20.0–22.0 °C. Measured outdoor temperature used as a perfect forecast; preferred 21 °C 07–09 and 18–24 (soft); fixed tariff (7.6p 00–05, 34.87p otherwise; export 2p/12p); no VPP events. **All figures are model estimates for comparing schedules, not measured savings.**

## Battery- and export-aware prices (default)

150 days.

| Schedule | Cost p/day | kWh/day | Days below 20 °C min | Mean lowest room °C | Shortfall vs 21 °C periods (K·h/day) | Starts/day |
|---|---|---|---|---|---|---|
| Actual (incl. night setback) | 56.7 | 4.83 | 133 | 19.13 | 4.60 | 1.0 |
| Simple timer (21.5 °C in periods, 20.5 °C otherwise) | 55.7 | 4.86 | 51 | 20.02 | 2.00 | 0.4 |
| Optimised | 59.8 | 5.51 | 4 | 20.60 | 0.36 | 0.1 |

Optimised vs simple timer: -4.1 p/day (-7.3 %). Mean optimised setpoint night (00–05) 21.61 °C, day (08–16) 20.93 °C. Runtime median 0.22 s, max 0.35 s.

## Raw import tariff

150 days.

| Schedule | Cost p/day | kWh/day | Days below 20 °C min | Mean lowest room °C | Shortfall vs 21 °C periods (K·h/day) | Starts/day |
|---|---|---|---|---|---|---|
| Actual (incl. night setback) | 160.7 | 4.83 | 133 | 19.13 | 4.60 | 1.0 |
| Simple timer (21.5 °C in periods, 20.5 °C otherwise) | 153.5 | 4.86 | 51 | 20.02 | 2.00 | 0.4 |
| Optimised | 123.0 | 4.88 | 4 | 20.15 | 0.58 | 0.8 |

Optimised vs simple timer: +30.5 p/day (+19.9 %). Mean optimised setpoint night (00–05) 21.87 °C, day (08–16) 20.50 °C. Runtime median 0.21 s, max 0.36 s.

## Notes

- The actual schedule falls below the new 20 °C minimum on most nights (old setback ≈ 19.3 °C), so it
  is cheaper but not comparable on comfort; the simple timer is the fair reference.
- Thermostat hysteresis, RT-modulation gain and cycling are model priors until calibrated from this
  winter's data (M3 tuning step).
