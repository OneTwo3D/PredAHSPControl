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

### DHW cross-check (77 summer DHW runs, 180 L cylinder)

| Quantity | Total |
|---|---|
| Tank heat from one sensor (180 L × rise of R5T, 42.7 → 46.2 °C) | 56 kWh |
| P1P2 hydronic heat | 133 kWh |
| Daikin DHW heat counter | 155 kWh |
| External electricity (minus standby) | 55 kWh |

A single tank sensor cannot see stratification (it implies COP ≈ 1, impossible), so the tank figure is not
usable. At DHW flow/return ΔT ≈ 4.5 K the hydronic value and the counter agree within 17 %, whereas in space
heating (ΔT ≈ 1.9 K) they differ by 63 %: consistent with a sensor offset of roughly 0.5–0.7 K that matters
only at small ΔT, plus the counter reading ≈ 15 % high. The circulation pump stops with the compressor, so
the offset cannot be measured directly. Best estimate for space-heating COP on the external-meter basis:
2.5–3.5, i.e. the counter-based values should be treated as an upper bound.

### Electricity split

Heating electricity over the season: external meter minus Daikin DHW electricity = 764 kWh, versus the
Daikin heating counter 803 kWh (+5 %). The integration therefore learns COP from the external meter.

Originally proposed independent check: DHW cycles, where the tank heat gain (volume × 4.18 kJ/kg·K × ΔT of R5T) can be
compared with both heat measurements. Needs the cylinder volume.
