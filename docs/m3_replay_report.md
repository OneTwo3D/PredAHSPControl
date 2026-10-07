# M3 shadow optimiser — replay on winter 2025/26

Generated 2026-10-07 by `tools/m3_replay.py`. One 24 h plan per day from 18:00, 2025-11-01 … 2026-03-30. Comfort range 20.0–22.0 °C. Measured outdoor temperature used as a perfect forecast; preferred 21 °C 07–09 and 18–24 (soft); fixed tariff (7.6p 00–05, 34.87p otherwise; export 2p/12p); no VPP events. **All figures are model estimates for comparing schedules, not measured savings.**

## Battery- and export-aware prices (default)

150 days.

| Schedule | Cost p/day | kWh/day | Days below 20 °C min | Mean lowest room °C | Shortfall vs 21 °C periods (K·h/day) | Starts/day |
|---|---|---|---|---|---|---|
| Actual (incl. night setback) | 51.5 | 4.39 | 133 | 19.13 | 4.61 | 1.0 |
| Simple timer (21.5 °C in periods, 20.5 °C otherwise) | 50.9 | 4.44 | 51 | 20.02 | 2.00 | 0.4 |
| Optimised | 55.1 | 5.07 | 4 | 20.62 | 0.35 | 0.1 |

Optimised vs simple timer: -4.2 p/day (-8.3 %). Mean optimised setpoint night (00–05) 21.62 °C, day (08–16) 21.00 °C. Runtime median 0.21 s, max 0.31 s.

## Raw import tariff

150 days.

| Schedule | Cost p/day | kWh/day | Days below 20 °C min | Mean lowest room °C | Shortfall vs 21 °C periods (K·h/day) | Starts/day |
|---|---|---|---|---|---|---|
| Actual (incl. night setback) | 145.7 | 4.39 | 133 | 19.13 | 4.61 | 1.0 |
| Simple timer (21.5 °C in periods, 20.5 °C otherwise) | 140.0 | 4.44 | 51 | 20.02 | 2.00 | 0.4 |
| Optimised | 116.1 | 4.56 | 4 | 20.22 | 0.54 | 0.7 |

Optimised vs simple timer: +23.9 p/day (+17.1 %). Mean optimised setpoint night (00–05) 21.87 °C, day (08–16) 20.55 °C. Runtime median 0.20 s, max 0.29 s.

## Notes

- The actual schedule falls below the new 20 °C minimum on most nights (old setback ≈ 19.3 °C), so it
  is cheaper but not comparable on comfort; the simple timer is the fair reference.
- Thermostat hysteresis, RT-modulation gain and cycling are model priors until calibrated from this
  winter's data (M3 tuning step).
