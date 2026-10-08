# Thermal capacity (C): correction and free-cooling learner

## Problem (8 Oct 2026)

With space heating off, the model predicted the room to cool from 22.3 °C (7 Oct 12:30) to ≈ 18 °C by the
next morning; it reached 21.4 °C. The building time constant τ = C/UA was 32 h (C = 3.0 kWh/K from the
winter daily energy balance, 3.0 ± 0.8, poorly identified: with heating on the room temperature barely
changes from day to day).

## Evidence from no-heating nights (22:00–06:00, compressor off, hourly LTS 2025-09 … 2026-10)

| Subset | Hours | Mean Ti − To | τ | C at UA = 94 W/K | Night gains |
|---|---|---|---|---|---|
| All | 2100 | 9.2 K | 77 h | 7.3 kWh/K | 73 W |
| Ti − To ≥ 6 K | 1646 | 10.8 K | 59 h | 5.5 kWh/K | 346 W |
| Oct–Nov 2025 | 325 | 9.8 K | 52 h | 4.9 kWh/K | 340 W |

Night of 7/8 Oct replayed (heating off, measured outdoor temperature): RMSE 1.22 K with C = 3.0, 0.47 K
with C = 5.5. The remaining error is the evening: the room rose 0.35 K between 19:00 and 21:00 without
heating (cooking, occupancy), which constant gains cannot represent; after 23:00 the cooling rate matches
(measured −0.13 K/h, model −0.15 K/h).

## Changes (0.5.0)

- Prior C = **5.5 kWh/K** (was 3.0). Changed priors now apply while nothing has been learned yet.
- **Free-cooling capacity learner** (`core/capacity_learner.py`): regresses the hourly cooling rate on
  Ti − To over clean night hour pairs (start 22:00–04:00, compressor off in both hours and the hour before,
  no DHW/defrost, Ti − To ≥ 6 K); slope = UA/C, intercept = G_night/C. Forgetting by elapsed time
  (0.98/day ≈ 50 days). An estimate is used once ≥ 4 nights, ≥ 30 pairs, spread of Ti − To ≥ 1.5 K and
  relative uncertainty ≤ 30 %; C then follows it (UA and gains stay with the daily learner).

## Historical check (learner run over 2025-09 … 2026-10)

First usable estimate on 1 Nov 2025 after 13 nights. Heating season (end of month): Nov 7.3, Dec 7.6,
Jan 6.3, Feb 5.2, Mar 8.1 kWh/K (τ 55–86 h). From April the estimate withholds itself (mild nights,
sun-charged fabric give biased slopes; mild nights are excluded and old data fades), so the last
heating-season value is kept. A pair-count memory or no ΔT filter let spring data drift C to 10–12 kWh/K;
both were rejected for that reason.

## Effect on the M3 replay (winter 2025/26)

Optimised vs simple timer: raw tariff +17 % → **+28 %**; battery-aware −7.6 % → −5.3 % (the optimiser buys
more comfort than the timer delivers). A house that holds heat longer makes pre-heating in cheap hours
more valuable.
