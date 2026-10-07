# Electricity and heat measurement check (winter 2025/26, Nov–Mar, hourly LTS)

External reference meter: `sensor.kwh_meter_energy_import` / `sensor.kwh_meter_power` (accurate; includes
DHW and standby). Daikin internal values via P1P2MQTT.

## Electricity

| Quantity | Season total | vs external |
|---|---|---|
| External meter | 953 kWh | — |
| Daikin kWh counters (`…electricity_consumed_total`: heating 797 + DHW 182 + backup 0) | 977 kWh | +2.5 % (monthly −1 … +6 %) |
| Integral of instantaneous `sensor.bridge0_power_consumption_heatpump` | 829 kWh | −13 % |

- **Standby** (compressor off for the whole hour, 1,081 h): external 19 W (P10 16, P90 27); internal reports 0 W.
  ≈ 0.45 kWh/day, ≈ 69 kWh over the season.
- **While running** (full-run hours) the internal power reads ≈ 35–45 W below the external meter at every
  compressor speed: an offset, not a scaling error (pump/controls/standby not included). During DHW the
  two agree (1171 vs 1173 W).
- Daily: external ≈ 0.85 × Daikin counters + 0.81 kWh/day (the constant ≈ standby + pump).

**Conclusion:** the Daikin kWh counters are good for daily/seasonal electricity (±3 %), but they omit standby and
round to 1 kWh. The instantaneous internal power is biased low by ≈ 40 W while running and by the full
standby when off. Use the external meter for electricity, the Daikin counters/flags only to split heating vs DHW.

## Heat (unresolved discrepancy)

| Quantity (hours without DHW) | Season total |
|---|---|
| Daikin heating heat counter (`…energy_produced_compressor_heating`) | 2,502 kWh |
| Integral of P1P2 `sensor.bridge0_power_production_heatpump` (flow × (R1T − R4T)) | 1,538 kWh (0.61×) |

The two heat measurements differ by ≈ 40 %. With flow−return ΔT of only ≈ 1.9 K, a 0.7 K sensor offset
explains the gap, so the hydronic value is fragile; on the other hand the radiators' datasheet rating
(10.6 kW at ΔT50, from apps.yaml) agrees with the emitter fit made from the hydronic value (10.2 kW), not
with the counter (which would imply ≈ 16 kW). Until resolved:

- COP is uncertain: per counters ≈ 3.0 seasonal; per hydronic heat and external meter ≈ 2.3–2.5 in full-run hours.
- The building UA (94 W/K) and gains are scaled by the same factor as the heat source used. Electricity
  forecasts are **not** affected, because heat and COP come from the same counter and the ratio was validated
  directly against measured electricity (Predheat replay, `docs/predheat_replay_report.md`).

Proposed independent check: DHW cycles, where the tank heat gain (volume × 4.18 kJ/kg·K × ΔT of R5T) can be
compared with both heat measurements. Needs the cylinder volume.
