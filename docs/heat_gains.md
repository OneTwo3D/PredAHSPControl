# Heat gains (v0.6.0)

The building model balances heat input against losses: `C·dTi/dt = Q_heatpump + G − UA·(Ti − To)`.
Up to v0.5.0 `G` was one learned constant ("internal gains", 440 W). From v0.6.0 the heat that can be
measured is counted separately, and the learned constant only covers the rest (**base gains**: people,
cooking and other unmetered heat).

| Source | Input (role) | Counted as heat | Why |
|---|---|---|---|
| Inverter/battery losses | `inverter_loss_w` = `sensor.solis_loss_power` (PV + battery − AC output) | 100 % | Solis inside the heated house. Winter daily balance: 0.81 ± 0.40 of it reaches the room, consistent with all of it. Winter mean 239 W (≈ 100 W standby + ≈ 5 % of throughput). |
| Sun through the windows | `solar_radiation` (weather station) | 1.5 W per W/m² | Winter daily balance 1.5 ± 0.3. |
| Hot-water cylinder | `tank_c` | 1.5 W/K × (tank − room) | 180 L cylinder inside the house. |
| Household electricity | `house_w` − heat pump − EV − loads outside (loft IT) | **0 %** (option) | See below. |
| Battery estimate | `battery_w` | 4.5 % of \|battery W\| | Only without `inverter_loss_w`; misses the inverter's standby. |

`house_w` must be the AC-side house load (`sensor.house_power` from `docs/ha/packages/energy.yaml`):
grid + Solis AC output + roof and shed PV. The old helper (PV and battery measured before the inverter)
contained the Solis losses, which would then have been counted twice.

## Why household electricity counts 0 %

Winter 2025/26, 91 clean heating days, daily energy balance
`heat + sun + losses = UA·(Ti − To) − base − k·household`:

| k (household) | UA (W/K) | Base gains (W) | Daily fit error (W) |
|---|---|---|---|
| free fit: 0.03 ± 0.08 | 90.0 | — | 85 |
| 0 | 90.2 | 90 | 85 |
| 0.3 | 89.2 | −41 | 97 |
| 0.7 | 87.9 | −216 | 141 |

The day-to-day variation of household electricity (mean ≈ 390 W inside the envelope, sd 170 W) has no
measurable effect on the heat demand: heat from cooking, washing, drying and showers largely leaves through
drains, extractor and ventilation. Its average share is part of the base gains. A share of 0.7 (an earlier
estimate from the faulty `house_power`) makes the base gains negative and the fit clearly worse. The
factor stays an option and will be learned once enough clean `house_power` data exist.

## Priors

With sun, tank and measured inverter losses as heat input: **UA 90 W/K, base gains 70 W** (fit: 50–90 W);
these are the defaults when an inverter-loss sensor is configured, otherwise 94 W/K and 440 W (all gains
in one constant, as before). Saved options win: existing installations set *Heat loss coefficient* 90 and
*Base internal gains* 70 under Configure when mapping the gain inputs (the learner then refines them).

## Consistency rules

- **Complete or nothing.** Each hour records whether every configured gain source was available; the
  gains are only booked while complete. A day with less than 95 % coverage is not learned from — booking
  0 W for a failed sensor would make the learned base gains absorb the missing heat.
- **One basis.** The learned base gains mean "all heat not measured". When the configured sources or
  factors change, the base gains restart from the prior, the profiles and the free-cooling regression
  start again, and the day of the change is not learned from.
- **No double counting of exclusions.** If a configured heat-pump, EV or outside-load meter is
  unavailable, household electricity is unknown (not "house load minus nothing").
- **Free-cooling nights** remove the measured gains with the current UA before fitting the cooling
  slope, so their night-time variation (e.g. battery use) does not bias the thermal capacity.
- House load and inverter losses older than 2 h are stale (their meters report every few seconds).

## Forecast

- Inverter losses and household electricity: measured value for the first hour, then learned hour-of-day
  profiles (EMA, ≈ several days per slot).
- Tank: held at the current value.
- Sun: Solcast PV forecast × learned ratio (station radiation ÷ forecast PV, prior 0.13 W/m² per W);
  without Solcast, the measured radiation for the first hour and none afterwards.

## Sensors

*Heat gains now* (W) with attributes `household_w`, `solar_w`, `inverter_w`, `tank_w`, `sources`;
*Base internal gains* (W) is the learned unmeasured rest.
