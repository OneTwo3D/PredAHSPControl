# M3 shadow optimiser — replay on winter 2025/26

Generated 2026-10-07 by `tools/m3_replay.py`. One 24 h plan per day from 18:00, 2025-11-01 … 2026-03-30. Comfort range 20.0–22.0 °C. Measured outdoor temperature used as a perfect forecast; fixed tariff (7.6p 00–05, 34.87p otherwise; export 2p/12p); no VPP events. **All figures are model estimates for comparing schedules, not measured savings.**

## Battery- and export-aware prices (default)

150 days.

| Schedule | Cost p/day | kWh/day | Days below comfort min | Mean lowest room °C | Starts/day |
|---|---|---|---|---|---|
| Actual (incl. night setback) | 56.7 | 4.83 | 133 | 19.13 | 1.0 |
| Fixed 20.5 °C | 50.6 | 4.65 | 53 | 19.98 | 0.3 |
| Optimised | 45.8 | 4.43 | 4 | 20.03 | 0.2 |

Optimised vs fixed: +4.8 p/day (+9.5 %). Mean optimised setpoint night (00–05) 21.16 °C, day (08–16) 20.22 °C. Runtime median 0.19 s, max 0.34 s.

## Raw import tariff

150 days.

| Schedule | Cost p/day | kWh/day | Days below comfort min | Mean lowest room °C | Starts/day |
|---|---|---|---|---|---|
| Actual (incl. night setback) | 160.7 | 4.83 | 133 | 19.13 | 1.0 |
| Fixed 20.5 °C | 129.9 | 4.65 | 53 | 19.98 | 0.3 |
| Optimised | 100.9 | 4.43 | 4 | 20.02 | 0.9 |

Optimised vs fixed: +29.0 p/day (+22.4 %). Mean optimised setpoint night (00–05) 21.56 °C, day (08–16) 20.23 °C. Runtime median 0.20 s, max 0.34 s.

## Notes

- The actual schedule falls below the new 20 °C minimum on most nights (old setback ≈ 19.3 °C), so it
  is cheaper but not comparable on comfort; the fixed schedule is the fair reference.
- Thermostat hysteresis, RT-modulation gain and cycling are model priors until calibrated from this
  winter's data (M3 tuning step).
