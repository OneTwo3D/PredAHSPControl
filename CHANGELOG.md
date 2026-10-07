# Changelog

Each `## [x.y.z]` section becomes the GitHub release notes for tag `vx.y.z`, created automatically by
`.github/workflows/ci.yml` after tests pass on the default branch. Bump
`custom_components/daikin_mpc/manifest.json` `version` and add a section here for every release.
0.2.0 was never tagged (superseded by 0.2.1 the same day).

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
