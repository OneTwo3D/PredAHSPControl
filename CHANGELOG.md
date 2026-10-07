# Changelog

Each `## [x.y.z]` section becomes the GitHub release notes for tag `vx.y.z`, created automatically by
`.github/workflows/ci.yml` after tests pass on the default branch. Bump
`custom_components/daikin_mpc/manifest.json` `version` and add a section here for every release.
0.2.0 was never tagged (superseded by 0.2.1 the same day).

## [0.4.0] - 2026-10-07

Fixes from an adversarial code review (Codex), see `docs/review_2026-10.md`.

### Fixed
- **Daylight saving (clocks go back 25 Oct):** all elapsed-time arithmetic now in UTC. Previously the
  repeated 01:00 hour merged into one, sample intervals went negative, forecasts and plan times shifted by
  an hour, and 23/25-hour days were treated as 24 h in the building learner and offline fit.
- **Meter counters after a restart/outage:** increments across a gap are no longer booked into the first
  hour after it; across midnight they are dropped and that day is not used for learning (counter
  timestamps are now persisted).
- **Flow-temperature offset** is learned only during space heating (DHW and defrost had shifted it by
  several K).
- **Building learner** only pairs consecutive days.
- **Hard comfort limits are now strict:** plans are ranked by time outside 20–22 °C first, then cost, so
  no price can buy a violation; "feasible" no longer has a hidden 0.05 K tolerance.
- **Standby no longer counted twice** in the electricity forecast: the COP now excludes standby (prior
  recomputed: 3.06 at 1 °C … 4.82 at 13 °C); standby is added once. Learned COP data is reset (new basis).
- **Units:** entities in Wh/MWh, kW, °F/K, L/h or m³/h are converted; unsupported units are reported
  instead of misread. Weather forecasts use the entity's temperature unit; NaN or implausible values are
  ignored.
- Weather points in the past no longer distort the outdoor-temperature forecast.
- Malformed Predbat rate attributes fall back to the configured tariff instead of failing the update.
- Corrupt stored state never prevents start-up (each section falls back to priors; covariance checked).
- Recommendations are cleared as soon as telemetry becomes invalid, and recomputed on the next poll
  instead of after 15 minutes (e.g. after HA restarts before the bridge is back); plan times refer to
  when the plan was made. Schedule text marks the next day ("18:00–18:00 next day").
- Optimiser validates input lengths (no crash on short price/forecast series).

### Changed
- **The bridge heartbeat entity is now required** (P1P2MQTT publishes on change only; without it a frozen
  bridge cannot be detected). Map it via *Reconfigure* if not set.

### Tools
- `ha_client.py`: exact read-only endpoint allowlist (the `/api/` prefix admitted GET webhooks);
  redirects refused so the token is never forwarded.
- `ha_export.py`: naive dates are Europe/London (option `--tz`), not the machine's time zone.
- Offline fit: hourly windows never span gaps or DHW exclusions; counter increments from statistics
  `sum` (reset-adjusted); actual day lengths. Replays handle DST days; Predheat replay notes that
  `fitted`/`calibrated` hold-out errors are optimistic (physical parameters fitted on all days).

## [0.3.1] - 2026-10-07

### Added
- **Preferred temperature periods** (option, default `07:00-09:00=21, 18:00-24:00=21`): the optimiser aims
  for these temperatures (soft target); outside them only the lowest allowed temperature (default 20 °C)
  applies. Hard limits 20–22 °C unchanged.

### Changed
- Default lowest room temperature 20.0 °C (was 20.5 °C).
- Replay compares against a simple timer schedule and reports the shortfall against the preferred periods.

## [0.3.0] - 2026-10-07

### Added — M3 shadow optimiser (recommendations only, nothing is sent to the heat pump)
- Every 15 minutes a 24 h plan of hourly room setpoints (0.5 °C steps) that minimises electricity cost
  while keeping the room between **20.0 and 22.0 °C** (configurable). No fixed night setback: lower night
  setpoints only when they save money.
- Prices from Predbat (`predbat.rates`, `predbat.rates_export`, including VPP events) with the configured
  tariff as fallback; round-trip efficiency from Predbat's loss settings.
- Battery- and export-aware pricing (default): energy outside the cheap window is valued at
  `min(import, max(cheap rate / efficiency, export rate))`, i.e. the opportunity cost of not exporting.
  Raw import tariff selectable.
- New sensors: *Recommended room setpoint* (with the hourly plan), *Expected saving 24h* (model estimate vs
  the current schedule), *Recommendation* (plain-language reason).
- Options: lowest/highest room temperature, price basis, fallback import/export tariffs.
- `tools/m3_replay.py` and `docs/m3_replay_report.md`: replay on winter 2025/26.

## [0.2.7] - 2026-10-07

### Fixed
- Predheat comparison ignores Predheat values older than 30 minutes, so a disabled or stopped Predheat
  (whose last states remain in HA) is no longer scored as if it were a current forecast.

## [0.2.6] - 2026-10-07

### Changed
- Plain-language names and help texts for every field in the setup and Reconfigure forms
  (e.g. *Room thermostat asking for heat* instead of *Thermostat heating demand*). No functional change.

## [0.2.5] - 2026-10-07

### Added
- Optional *Thermostat heating demand* role (suggested `binary_sensor.bridge0_mode_valve_zone_main`,
  verified in a live test): the forecast starts from the actual thermostat state instead of inferring it
  from the compressor (which lags demand by several minutes and cycles on water temperature).
  Existing installs: Reconfigure; the field is pre-filled.

## [0.2.4] - 2026-10-07

### Fixed
- Suggested "DHW active" entity is now `binary_sensor.bridge0_dhw_dhw_demand` (on only while the cylinder
  is being heated). `binary_sensor.bridge0_dhw_dhw` means "DHW enabled", is always on, and made every hour
  count as DHW. Existing installs: Reconfigure and change the *DHW active* field.

## [0.2.3] - 2026-10-07

### Fixed
- Setup/reconfigure no longer rejects the Predheat comparison entities when Predbat is not running
  (its `predheat.*` states only exist while Predbat runs); they are always pre-filled.

## [0.2.2] - 2026-10-07

### Added
- **Reconfigure** (Settings → Devices & services → Daikin MPC → ⋮ → Reconfigure): edit the entity mapping
  in place without losing learned state. Unmapped optional roles are pre-filled with verified defaults.

### Fixed
- Predheat comparison entities are entered as text, because `predheat.*` states are not registered
  entities and the entity picker could drop them.

## [0.2.1] - 2026-10-07

### Added
- Optional external heat-pump meter roles (`ext_w`, `ext_kwh`) and the Daikin DHW electricity counter
  (`dhw_elec_kwh`), pre-filled in the setup form.
- Live COP-by-outdoor-temperature learner on the external-meter basis (includes standby and pump),
  monotone, with forgetting and fallback to the prior curve.
- Standby power learned from compressor-off hours; new `sensor.daikin_mpc_standby_power`.

### Changed
- `sensor.daikin_mpc_predicted_heat_pump_electricity_24h` (renamed) now includes standby; attributes
  `space_heating_kwh_24h`, `standby_kwh_24h`, `cop_source`.
- COP prior recomputed from winter 2025/26 with the external meter (2.93 at 1 °C … 3.53 at 13 °C).

### Upgrade note
Remove and re-add the integration so the new optional entities appear in the setup form.

## [0.2.0] - 2026-10-07

### Added
- First Home Assistant release (shadow mode, observation only, no writes to the heat pump).
- Config/options flow with verified, pre-filled entity mapping; 5-minute coordinator; bridge heartbeat.
- Hour/day aggregation, bounded daily learner for heat loss, gains and thermal capacity.
- 24 h room-temperature and heating-electricity forecast (thermostat gating, RT modulation, 25 °C LWT floor).
- Self-scoring of 1/3/6 h forecasts, including Predheat comparison; persistence; diagnostics.
